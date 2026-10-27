from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from alembic import command
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.platform import memory_service as memory, story_service as story
from app.platform.api import create_app
from app.platform.budget_service import signature
from app.platform.database import migration_config
from app.platform.models import (
    MemoryDecision,
    MemoryEntry,
    MemoryProposal,
    MemoryStoryState,
)
from platform_fixtures import signed_in
from test_platform_memory import draft, decision, detail, revise_source
from test_platform_tasks import task_input


def state(start=10, end=20, **changes):
    return {
        "subject_key": "人物-林舟",
        "predicate": "职业",
        "value": "船医",
        "valid_from": start,
        "valid_until": end,
        **changes,
    }


def propose(db, body, key, value=None, **changes):
    return memory.create(
        db,
        body.work_id,
        draft(
            body,
            entry_key=key,
            request_key=key,
            story_state=state() if value is None else value,
            **changes,
        ),
    )


def at(db, work_id, instant, subject=None):
    with db.sessions() as session:
        return [
            memory.data(session, row)
            for row in session.scalars(story.at_query(work_id, instant, subject))
        ]


def test_half_open_intervals_candidates_and_unbounded_future(platform):
    _, db = platform
    body = task_input(db)
    first = propose(db, body, "船医阶段")
    assert at(db, body.work_id, 10) == []
    memory.decide(db, body.work_id, first, decision())
    second = propose(db, body, "领航阶段", state(20, None, value="领航员"))
    memory.decide(db, body.work_id, second, decision())
    assert at(db, body.work_id, 9) == []
    assert at(db, body.work_id, 10)[0]["id"] == first
    assert at(db, body.work_id, 19)[0]["id"] == first
    assert at(db, body.work_id, 20)[0]["id"] == second
    assert at(db, body.work_id, 10**12)[0]["story_state"]["value"] == "领航员"
    assert at(db, body.work_id, 10, "其他人") == []
    rejected = propose(db, body, "未采纳", state(1, 5))
    memory.decide(db, body.work_id, rejected, decision("rejected"))
    assert at(db, body.work_id, 2) == []


def test_retroactive_revision_is_distinct_from_story_chronology(platform):
    _, db = platform
    body = task_input(db)
    old = propose(db, body, "职业记录")
    memory.decide(db, body.work_id, old, decision())
    replacement = memory.create(
        db,
        body.work_id,
        draft(
            body,
            entry_key="职业记录",
            request_key="correction",
            base_proposal_id=old,
            story_state=state(5, 15, value="学徒"),
        ),
    )
    memory.decide(db, body.work_id, replacement, decision())
    assert at(db, body.work_id, 17) == []  # Never resurrect the superseded interval.
    assert at(db, body.work_id, 7)[0]["id"] == replacement
    assert detail(db, body.work_id, old)["story_state"] == state()
    memory.decide(db, body.work_id, old, decision())
    assert at(db, body.work_id, 10)[0]["id"] == replacement
    removal = memory.create(
        db,
        body.work_id,
        draft(
            body,
            entry_key="职业记录",
            request_key="remove-time",
            base_proposal_id=replacement,
        ),
    )
    memory.decide(db, body.work_id, removal, decision())
    assert at(db, body.work_id, 7) == []
    assert detail(db, body.work_id, removal)["is_current"]


@pytest.mark.parametrize(
    "overlap", [state(0, 11), state(15, 25), state(11, 19), state(0, None), state()]
)
def test_overlaps_block_even_identical_values_and_can_be_rejected(platform, overlap):
    _, db = platform
    body = task_input(db)
    current = propose(db, body, "现行")
    memory.decide(db, body.work_id, current, decision())
    candidate = propose(db, body, "重叠", overlap)
    row = detail(db, body.work_id, candidate)
    assert row["blockers"] == ["STORY_STATE_OVERLAP"]
    assert row["story_conflicts"][0]["id"] == current
    with pytest.raises(HTTPException, match="STORY_STATE_OVERLAP"):
        memory.decide(db, body.work_id, candidate, decision())
    memory.decide(db, body.work_id, candidate, decision("rejected"))
    assert at(db, body.work_id, 15)[0]["id"] == current


def test_close_open_interval_before_approving_next_state(platform):
    _, db = platform
    body = task_input(db)
    first = propose(db, body, "早期职业", state(0, None))
    memory.decide(db, body.work_id, first, decision())
    later = propose(db, body, "后期职业", state(20, None, value="领航员"))
    assert "STORY_STATE_OVERLAP" in detail(db, body.work_id, later)["blockers"]
    correction = memory.create(
        db,
        body.work_id,
        draft(
            body,
            entry_key="早期职业",
            request_key="close-interval",
            base_proposal_id=first,
            story_state=state(0, 20),
        ),
    )
    memory.decide(db, body.work_id, correction, decision())
    assert detail(db, body.work_id, later)["blockers"] == []
    memory.decide(db, body.work_id, later, decision())
    assert at(db, body.work_id, 19)[0]["id"] == correction
    assert at(db, body.work_id, 20)[0]["id"] == later
    # Expanding the earlier interval again cannot silently overlap the later state.
    expansion = memory.create(
        db,
        body.work_id,
        draft(
            body,
            entry_key="早期职业",
            request_key="expand",
            base_proposal_id=correction,
            story_state=state(0, 25),
        ),
    )
    with pytest.raises(HTTPException, match="STORY_STATE_OVERLAP"):
        memory.decide(db, body.work_id, expansion, decision())


def test_other_slots_and_other_works_are_independent(platform):
    _, db = platform
    body = task_input(db)
    for key, spec in [
        ("职业", state()),
        ("地点", state(predicate="地点", value="港口")),
        ("另一人", state(subject_key="人物-兰英")),
    ]:
        proposal = propose(db, body, key, spec)
        memory.decide(db, body.work_id, proposal, decision())
    other = task_input(db)
    proposal = propose(db, other, "其他作品")
    memory.decide(db, other.work_id, proposal, decision())
    assert len(at(db, body.work_id, 15)) == 3
    assert len(at(db, body.work_id, 15, "人物-林舟")) == 2
    assert len(at(db, other.work_id, 15)) == 1


def test_concurrent_different_entry_approvals_cannot_overlap(platform):
    _, db = platform
    body = task_input(db)
    ids = [propose(db, body, key) for key in ("甲", "乙")]
    barrier = Barrier(2)

    def approve(proposal_id):
        barrier.wait(timeout=10)
        try:
            return memory.decide(db, body.work_id, proposal_id, decision())["id"]
        except HTTPException as error:
            assert "STORY_STATE_OVERLAP" in error.detail
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, ids))
    assert sum(item is not None for item in results) == 1
    assert len(at(db, body.work_id, 15)) == 1


def test_source_freshness_still_applies_and_existing_state_stays_flagged(platform):
    _, db = platform
    body = task_input(db)
    current = propose(db, body, "现行")
    memory.decide(db, body.work_id, current, decision())
    pending = propose(db, body, "后续", state(20, 30))
    revise_source(db, body.input_revision_id)
    assert "SOURCE_CHANGED" in detail(db, body.work_id, pending)["blockers"]
    with pytest.raises(HTTPException):
        memory.decide(db, body.work_id, pending, decision())
    assert at(db, body.work_id, 15)[0]["source_changed"]
    assert at(db, body.work_id, 25) == []


def test_state_and_proposal_roll_back_together_on_audit_failure(platform, monkeypatch):
    _, db = platform
    body = task_input(db)

    def fail(session, *args, **kwargs):
        session.flush()
        raise RuntimeError("after writes")

    monkeypatch.setattr(memory, "audit", fail)
    with pytest.raises(RuntimeError):
        propose(db, body, "rollback")
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MemoryStoryState)) == 0
        assert session.scalar(select(func.count()).select_from(MemoryProposal)) == 0


def test_state_is_immutable_and_payload_changes_are_not_idempotent(platform):
    _, db = platform
    body = task_input(db)
    proposal = propose(db, body, "immutable")
    assert propose(db, body, "immutable") == proposal
    with pytest.raises(HTTPException):
        propose(db, body, "immutable", state(value="另一职业"))
    for sql in (
        "DELETE FROM memory_story_states",
        "UPDATE memory_story_states SET value = 'changed'",
    ):
        with pytest.raises(DBAPIError):
            with db.sessions.begin() as session:
                session.execute(text(sql))


def test_api_permissions_validation_filter_pagination_and_no_inference(platform):
    settings, db = platform
    body = task_input(db)
    prefix = f"/api/v1/works/{body.work_id}/memory"
    with TestClient(create_app(settings, db)) as client:
        assert client.get(prefix + "/timeline?at=10").status_code == 401
        headers = signed_in(client, settings)
        for invalid in [
            state(-1),
            state(True),
            state(10, 10),
            state(20, 10),
            state(0, 10**12 + 1),
            state(value=" "),
            state(subject_key=" "),
            state(predicate=""),
            state(10.5),
            state(valid_from="10"),
            state(extra="field"),
            state(value="\x00"),
        ]:
            payload = draft(body).model_dump() | {"story_state": invalid}
            assert (
                client.post(
                    prefix + "/proposals", json=payload, headers=headers
                ).status_code
                == 422
            )
        payload = draft(body).model_dump() | {"story_state": state()}
        assert client.post(prefix + "/proposals", json=payload).status_code == 403
        assert (
            client.post(
                prefix + "/proposals",
                json=payload,
                headers=headers | {"Origin": "https://other.invalid"},
            ).status_code
            == 403
        )
        created = client.post(prefix + "/proposals", json=payload, headers=headers)
        assert created.status_code == 201
        memory.decide(db, body.work_id, created.json()["id"], decision())
        for suffix in (
            "",
            "?at=-1",
            "?at=true",
            "?at=10.5",
            "?at=1000000000001",
            "?at=10&limit=101",
        ):
            assert client.get(prefix + "/timeline" + suffix).status_code == 422
        assert len(client.get(prefix + "/timeline?at=10").json()) == 1
        assert client.get(prefix + "/timeline?at=10&offset=1").json() == []
        assert client.get(prefix + "/timeline?at=10&subject_key=%25").json() == []
        assert (
            client.get("/api/v1/works/missing/memory/timeline?at=10").status_code == 404
        )
        unstructured = memory.create(
            db, body.work_id, draft(body, entry_key="无时点", request_key="plain")
        )
        memory.decide(db, body.work_id, unstructured, decision())
        assert len(client.get(prefix).json()) == 2
        assert len(client.get(prefix + "/timeline?at=10").json()) == 1
    with TestClient(create_app(settings, db)) as agent:
        assert (
            agent.get(
                prefix + "/timeline?at=10",
                headers={"Authorization": "Bearer agent-token"},
            ).status_code
            == 401
        )


def test_upgrade_preserves_old_hashes_and_unstructured_memory(platform_database):
    _, db = platform_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0011_memory_review")
    body = task_input(db)
    request = draft(body)
    proposal = memory.create(db, body.work_id, request)
    with db.sessions.begin() as session:
        session.add(
            MemoryDecision(
                proposal_id=proposal, outcome="approved", reason="旧版审核", version=1
            )
        )
        session.add(
            MemoryEntry(
                work_id=body.work_id,
                entry_key=request.entry_key,
                proposal_id=proposal,
                version=1,
            )
        )
        assert session.get(MemoryProposal, proposal).request_hash == signature(
            request.model_dump(exclude={"story_state"})
        )
    db.upgrade()
    assert db.require_current() == "0014_task_contexts"
    assert memory.create(db, body.work_id, request) == proposal
    assert detail(db, body.work_id, proposal)["story_state"] is None
    assert at(db, body.work_id, 10) == []
    replacement = memory.create(
        db,
        body.work_id,
        draft(
            body,
            request_key="upgrade-state",
            base_proposal_id=proposal,
            story_state=state(),
        ),
    )
    memory.decide(db, body.work_id, replacement, decision())
    assert at(db, body.work_id, 10)[0]["id"] == replacement
