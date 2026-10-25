from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, func, update
from sqlalchemy.exc import DBAPIError

from app.platform.api import create_app
from app.platform import executor_service, task_service, agent_security
from app.platform.executor_protocol import ExecutorCreate, PolicyInput
from app.platform.models import (
    AgentCredential,
    AgentTaskGrant,
    AssetRevision,
    AuditEvent,
    BudgetReservation,
    CreativeTask,
    TaskAttempt,
    TaskSubmission,
    now,
)
from platform_fixtures import signed_in
from test_platform_tasks import task_input, result, expire

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from offline_agent_client import AgentClient, diagnostic  # noqa: E402


def prepare(db):
    executor_id = executor_service.register(
        db,
        ExecutorCreate(
            name="离线 Agent",
            mode="agent",
            agent_protocol="offline_v1",
            capabilities=["analyze"],
            enabled=True,
            request_key="offline-agent",
        ),
    )
    body = task_input(db).model_copy(update={"executor_id": executor_id})
    executor_service.configure_policy(
        db,
        body.work_id,
        PolicyInput(
            expected_version=0, allowed_ids=[executor_id], preferred_ids=[executor_id]
        ),
    )
    task_id = task_service.create_task(db, body)
    return task_id, body


def grant(client, headers, task_id):
    version = client.get(f"/api/v1/tasks/{task_id}").json()["version"]
    response = client.post(
        f"/api/v1/tasks/{task_id}/agent-grants",
        headers=headers,
        json={
            "expected_version": version,
            "name": "测试客户端",
            "expires_seconds": 3600,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth(issued):
    return {"Authorization": "Bearer " + issued["token"]}


def payload(envelope, **changes):
    body = result(envelope, **changes).model_dump(mode="json")
    body["lease_token"] = envelope["lease_token"]
    return body


def test_agent_scope_is_separate_from_owner_and_other_tasks(platform):
    settings, db = platform
    task_id, body = prepare(db)
    with (
        TestClient(create_app(settings, db)) as owner,
        TestClient(create_app(settings, db)) as agent,
    ):
        headers = signed_in(owner, settings)
        issued = grant(owner, headers, task_id)
        path = f"/api/agent/v1/tasks/{task_id}"
        assert owner.get(path).status_code == 401
        assert agent.get(path, headers=auth(issued)).status_code == 200
        for protected in (
            "/api/v1/works",
            "/api/v1/credentials",
            f"/api/v1/tasks/{task_id}",
            "/api/v1/executors",
        ):
            assert agent.get(protected, headers=auth(issued)).status_code == 401
        other = task_service.create_task(
            db, body.model_copy(update={"request_key": "another"})
        )
        assert (
            agent.post(
                f"/api/agent/v1/tasks/{other}/claim", headers=auth(issued)
            ).status_code
            == 403
        )
        assert (
            agent.post(
                path + "/claim",
                headers={**auth(issued), "Origin": "https://foreign.invalid"},
            ).status_code
            == 403
        )
        envelope = agent.post(path + "/claim", headers=auth(issued)).json()
        assert envelope["permissions"]["write_assets"] is False
        assert envelope["permissions"]["paid_calls"] is False
        assert "credential" not in json.dumps(envelope["input"])
        metadata = owner.get(f"/api/v1/tasks/{task_id}/agent-grants").text
        assert issued["token"] not in metadata and "token_hash" not in metadata
        assert (
            owner.post(
                f"/api/v1/tasks/{task_id}/results",
                headers=headers,
                json=payload(envelope),
            ).status_code
            == 403
        )


def test_agent_completion_duplicate_and_usage_rejection(platform):
    settings, db = platform
    task_id, _ = prepare(db)
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        issued = grant(client, headers, task_id)
        path = f"/api/agent/v1/tasks/{task_id}"
        envelope = client.post(path + "/claim", headers=auth(issued)).json()
        paid = payload(envelope)
        paid["usage"] = {"input_tokens": 1}
        assert (
            client.post(path + "/results", headers=auth(issued), json=paid).status_code
            == 422
        )
        received = client.post(
            path + "/results", headers=auth(issued), json=payload(envelope)
        )
        assert (
            received.status_code == 200 and received.json()["disposition"] == "ACCEPTED"
        )
        assert (
            client.post(
                path + "/results", headers=auth(issued), json=payload(envelope)
            ).json()
            == received.json()
        )
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AssetRevision)) == 1
        assert session.scalar(select(func.count()).select_from(TaskSubmission)) == 1
        assert session.scalar(select(func.count()).select_from(BudgetReservation)) == 0
        audits = list(session.scalars(select(AuditEvent)))
        assert any(item.actor == "agent:" + issued["id"] for item in audits)
        assert issued["token"] not in json.dumps([item.details for item in audits])


def test_agent_reconnect_restores_checkpoint_and_rotates_lease(platform):
    settings, db = platform
    task_id, _ = prepare(db)
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        issued = grant(client, headers, task_id)
        path = f"/api/agent/v1/tasks/{task_id}"
        first = client.post(path + "/claim", headers=auth(issued)).json()
        checkpoint = {
            "attempt_id": first["attempt_id"],
            "lease_token": first["lease_token"],
            "sequence": 1,
            "step": "halfway",
            "payload": {"note": "saved"},
        }
        assert (
            client.post(
                path + "/checkpoints", json=checkpoint, headers=auth(issued)
            ).status_code
            == 200
        )
        second = client.post(path + "/claim", headers=auth(issued)).json()
        assert (
            second["attempt_id"] == first["attempt_id"]
            and second["lease_token"] != first["lease_token"]
        )
        assert second["checkpoint"]["payload"] == {"note": "saved"}
        assert (
            client.post(
                path + "/results", json=payload(first), headers=auth(issued)
            ).status_code
            == 403
        )
        expire(db, task_id)
        task_service.recover(db)
        with db.sessions() as session:
            version = session.get(CreativeTask, task_id).version
        task_service.control(db, task_id, "resume", version)
        third = client.post(path + "/claim", headers=auth(issued)).json()
        assert third["attempt_id"] != first["attempt_id"]
        assert third["checkpoint"]["payload"] == {"note": "saved"}
        assert (
            client.post(
                path + "/results", json=payload(second), headers=auth(issued)
            ).json()["disposition"]
            == "QUARANTINED"
        )


def test_revocation_and_expiration_fence_old_grant(platform):
    settings, db = platform
    task_id, _ = prepare(db)
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        issued = grant(client, headers, task_id)
        path = f"/api/agent/v1/tasks/{task_id}"
        envelope = client.post(path + "/claim", headers=auth(issued)).json()
        assert (
            client.delete(
                f"/api/v1/tasks/{task_id}/agent-grants/{issued['id']}", headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                path + "/results", headers=auth(issued), json=payload(envelope)
            ).status_code
            == 401
        )
        with db.sessions() as session:
            task = session.get(CreativeTask, task_id)
            assert task.state == "PAUSED" and task.reason == "AGENT_REVOKED"
        fresh = grant(client, headers, task_id)
        with db.sessions.begin() as session:
            session.get(AgentCredential, fresh["id"]).expires_at = now() - timedelta(
                seconds=1
            )
        assert client.get(path, headers=auth(fresh)).status_code == 401
        assert (
            client.post(
                f"/api/v1/tasks/{task_id}/agent-grants",
                json={"name": "no csrf", "expected_version": task.version},
            ).status_code
            == 403
        )


def test_grant_transaction_recheck_and_immutable_scope(platform):
    settings, db = platform
    task_id, _ = prepare(db)
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        issued = grant(client, headers, task_id)
        with db.sessions() as session:
            principal = agent_security.authenticate_agent(
                session, "Bearer " + issued["token"]
            )
        client.delete(
            f"/api/v1/tasks/{task_id}/agent-grants/{issued['id']}", headers=headers
        )
        # Authentication happened earlier; the mutation must still check revocation in its transaction.
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            task_service.try_claim(db, task_id, principal["executor_id"], principal)
    with pytest.raises(DBAPIError):
        with db.sessions.begin() as session:
            session.execute(
                update(AgentTaskGrant)
                .where(AgentTaskGrant.credential_id == issued["id"])
                .values(task_id="different")
            )


def test_revoke_result_race_has_one_authoritative_outcome(platform):
    settings, db = platform
    task_id, _ = prepare(db)
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        issued = grant(client, headers, task_id)
        envelope = client.post(
            f"/api/agent/v1/tasks/{task_id}/claim", headers=auth(issued)
        ).json()
        barrier = Barrier(2)

        def submit():
            barrier.wait(timeout=10)
            return client.post(
                f"/api/agent/v1/tasks/{task_id}/results",
                headers=auth(issued),
                json=payload(envelope),
            )

        def revoke():
            barrier.wait(timeout=10)
            return client.delete(
                f"/api/v1/tasks/{task_id}/agent-grants/{issued['id']}", headers=headers
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            sent, revoked = pool.submit(submit), pool.submit(revoke)
            sent, revoked = sent.result(), revoked.result()
        assert revoked.status_code == 200
        with db.sessions() as session:
            task = session.get(CreativeTask, task_id)
            if sent.status_code == 200:
                assert (
                    task.state == "SUCCEEDED"
                    and sent.json()["disposition"] == "ACCEPTED"
                )
            else:
                assert sent.status_code in {401, 403} and task.state == "PAUSED"


def test_reference_client_retries_lost_ack_without_second_result(platform, tmp_path):
    settings, db = platform
    task_id, _ = prepare(db)
    with TestClient(create_app(settings, db)) as transport:
        headers = signed_in(transport, settings)
        issued = grant(transport, headers, task_id)
        client = AgentClient("https://testserver", task_id, issued["token"])
        lost = True

        def request(path="", body=None):
            nonlocal lost
            response = transport.request(
                "POST" if body is not None else "GET",
                f"/api/agent/v1/tasks/{task_id}" + path,
                headers=auth(issued),
                json=body,
            )
            assert response.status_code == 200, response.text
            if path == "/results" and lost:
                lost = False
                raise RuntimeError("lost acknowledgement")
            return response.json()

        client.request = request
        state_path = tmp_path / "private-state.json"
        with pytest.raises(RuntimeError):
            diagnostic(client, state_path)
        assert issued["token"] not in state_path.read_text(encoding="utf-8")
        assert diagnostic(client, state_path)["disposition"] == "ACCEPTED"
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(TaskSubmission)) == 1
        assert session.scalar(select(func.count()).select_from(TaskAttempt)) == 1


def test_agent_registration_validation_and_duplicate_grants(platform):
    settings, db = platform
    task_id, body = prepare(db)
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        issued = grant(client, headers, task_id)
        version = client.get(f"/api/v1/tasks/{task_id}").json()["version"]
        response = client.post(
            f"/api/v1/tasks/{task_id}/agent-grants",
            headers=headers,
            json={"name": "second", "expected_version": version},
        )
        assert response.status_code == 409
        bad = {
            "name": "bad paid agent",
            "mode": "agent",
            "agent_protocol": "offline_v1",
            "capabilities": ["analyze"],
            "request_key": "bad",
            "currency": "CNY",
            "input_rate": 1,
            "output_rate": 1,
        }
        assert (
            client.post("/api/v1/executors", headers=headers, json=bad).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/agent/v1/tasks/{task_id}/control/cancel",
                headers=auth(issued),
                json={},
            ).status_code
            == 404
        )
        with db.sessions() as session:
            decision = executor_service.evaluate(session, body.work_id, "analyze")
            assert decision["selected_id"] == body.executor_id


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
    ],
)
def test_reference_client_rejects_unsafe_origins(origin):
    with pytest.raises(ValueError):
        AgentClient(origin, "a" * 32, "b" * 40)


def test_reference_client_does_not_follow_redirect_or_environment_proxy(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    visited = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/capture")
            self.end_headers()

        def log_message(self, *_):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
            monkeypatch.setenv("no_proxy", "")
            client = AgentClient(
                f"http://127.0.0.1:{server.server_port}", "a" * 32, "b" * 40, True
            )
            with pytest.raises(RuntimeError, match="rejected \\(302\\)"):
                client.status()
            assert visited == ["/api/agent/v1/tasks/" + "a" * 32]
        finally:
            server.shutdown()
            thread.join(timeout=5)
