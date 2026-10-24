"""Short transactional task transitions; external calls and paid execution stay closed."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import secrets

from fastapi import HTTPException
from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError

from app.platform.models import (
    Asset,
    AssetRevision,
    AuditEvent,
    CreativeTask,
    Executor,
    TaskAttempt,
    TaskCheckpoint,
    TaskDependency,
    TaskSubmission,
    identifier,
    now,
)
from app.platform.security import hashed, utc
from app.platform.task_protocol import CheckpointInput, ResultEnvelope, TaskInput
from app.platform.works import require_asset, require_work

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
LEASE_SECONDS = 60


def fingerprint(payload):
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def event(session, task, action, **details):
    session.add(
        AuditEvent(
            actor="worker:local_test" if details.pop("worker", False) else "owner",
            action="task." + action,
            target_id=task.id,
            details={"work_id": task.work_id, **details},
        )
    )


def locked_task(session, task_id):
    # A harmless write gives SQLite and PostgreSQL the same serialization point.
    result = session.execute(
        update(CreativeTask)
        .where(CreativeTask.id == task_id)
        .values(version=CreativeTask.version)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(404, "任务不存在")
    return session.get(CreativeTask, task_id, populate_existing=True)


def transition(task, state, reason=None):
    task.state, task.reason = state, reason
    task.version += 1


def current_input(session, task):
    revision = session.get(AssetRevision, task.input_revision_id)
    # Serialize with saving a new asset head; never write novel content here.
    result = session.execute(
        update(Asset)
        .where(Asset.id == revision.asset_id, Asset.head_version == revision.number)
        .values(head_version=Asset.head_version)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def create_task(db, body: TaskInput):
    request_hash = fingerprint(body.model_dump())

    def existing(session):
        item = session.scalar(
            select(CreativeTask).where(
                CreativeTask.work_id == body.work_id,
                CreativeTask.request_key == body.request_key,
            )
        )
        if item is not None and item.request_hash != request_hash:
            raise HTTPException(409, "同一请求标识不能用于不同任务")
        return item

    try:
        with db.sessions.begin() as session:
            previous = existing(session)
            if previous is not None:
                return previous.id
            require_work(session, body.work_id)
            executor = session.get(Executor, body.executor_id)
            if (
                executor is None
                or executor.mode not in {"human", "local_test"}
                or body.task_type not in executor.capabilities
            ):
                raise HTTPException(
                    422, "所选执行者不支持该任务；真实 API 与 Agent 尚未开放"
                )
            revision = session.get(AssetRevision, body.input_revision_id)
            if revision is None:
                raise HTTPException(404, "输入版本不存在")
            asset = require_asset(session, body.work_id, revision.asset_id)
            if asset.head_version != revision.number:
                raise HTTPException(409, "请选择当前已保存的正文版本")
            if len(set(body.dependencies)) != len(body.dependencies):
                raise HTTPException(422, "依赖不能重复")
            for dep_id in body.dependencies:
                dep = session.get(CreativeTask, dep_id) if len(dep_id) <= 32 else None
                if dep is None or dep.work_id != body.work_id:
                    raise HTTPException(422, "依赖任务必须已经存在且属于同一作品")
            task = CreativeTask(
                work_id=body.work_id,
                executor_id=body.executor_id,
                input_revision_id=body.input_revision_id,
                task_type=body.task_type,
                objective=body.objective,
                request_key=body.request_key,
                request_hash=request_hash,
                priority=body.priority,
                max_attempts=body.max_attempts,
                deadline_at=now() + timedelta(seconds=body.deadline_seconds),
            )
            session.add(task)
            session.flush()
            for dep_id in body.dependencies:
                session.add(TaskDependency(task_id=task.id, depends_on_id=dep_id))
            # Only new nodes may refer to existing nodes; dependencies are immutable, so no cycle can be introduced.
            event(
                session,
                task,
                "created",
                executor=task.executor_id,
                input_revision_id=revision.id,
            )
            return task.id
    except IntegrityError:
        with db.sessions() as session:
            previous = existing(session)
            if previous is not None:
                return previous.id
        raise


def latest_checkpoint(session, task_id):
    record = session.scalar(
        select(TaskCheckpoint)
        .join(TaskAttempt, TaskAttempt.id == TaskCheckpoint.attempt_id)
        .where(TaskCheckpoint.task_id == task_id)
        .order_by(TaskAttempt.number.desc(), TaskCheckpoint.sequence.desc())
        .limit(1)
    )
    if record is None:
        return None
    return {
        "id": record.id,
        "attempt_id": record.attempt_id,
        "sequence": record.sequence,
        "step": record.step,
        "payload": record.payload,
    }


def task_envelope(session, task, attempt, token):
    revision = session.get(AssetRevision, task.input_revision_id)
    return {
        "protocol_version": "1.0",
        "task_id": task.id,
        "work_id": task.work_id,
        "attempt_id": attempt.id,
        "executor_id": task.executor_id,
        "task_type": task.task_type,
        "objective": task.objective,
        "lease_token": token,
        "lease_until": utc(attempt.lease_until),
        "deadline_at": utc(attempt.deadline_at),
        "input": {
            "revision_id": revision.id,
            "asset_id": revision.asset_id,
            "sha256": revision.sha256,
            "title": revision.title,
            "content": revision.content,
        },
        "checkpoint": latest_checkpoint(session, task.id),
        "dependency_ids": list(
            session.scalars(
                select(TaskDependency.depends_on_id).where(
                    TaskDependency.task_id == task.id
                )
            )
        ),
        "attempt_number": attempt.number,
        "max_attempts": task.max_attempts,
        "permissions": {
            "candidate_submission": True,
            "write_assets": False,
            "finalize": False,
            "paid_calls": False,
            "external_calls": False,
        },
        "budget": {"reserved_microunits": 0, "accounting": "not_applicable"},
    }


def try_claim(db, task_id, executor_id):
    with db.sessions.begin() as session:
        task = locked_task(session, task_id)
        if (
            task.executor_id != executor_id
            or task.state not in {"QUEUED", "RETRY_WAIT"}
            or utc(task.available_at) > now()
        ):
            return None
        if utc(task.deadline_at) <= now() or task.attempt_count >= task.max_attempts:
            transition(task, "FAILED", "LIMIT_EXCEEDED")
            event(session, task, "blocked", reason=task.reason)
            return None
        dependencies = list(
            session.scalars(
                select(CreativeTask)
                .join(TaskDependency, TaskDependency.depends_on_id == CreativeTask.id)
                .where(TaskDependency.task_id == task.id)
            )
        )
        if any(dep.state in {"FAILED", "CANCELLED"} for dep in dependencies):
            transition(task, "PAUSED", "DEPENDENCY_FAILED")
            event(session, task, "blocked", reason=task.reason)
            return None
        if any(dep.state != "SUCCEEDED" for dep in dependencies):
            return None
        if not current_input(session, task):
            transition(task, "FAILED", "INPUT_STALE")
            event(session, task, "blocked", reason=task.reason)
            return None
        token = secrets.token_urlsafe(32)
        timestamp = now()
        deadline = min(utc(task.deadline_at), timestamp + timedelta(hours=1))
        attempt = TaskAttempt(
            id=identifier(),
            task_id=task.id,
            number=task.attempt_count + 1,
            token_hash=hashed(token),
            lease_until=min(deadline, timestamp + timedelta(seconds=LEASE_SECONDS)),
            deadline_at=deadline,
        )
        session.add(attempt)
        task.active_attempt_id = attempt.id
        task.attempt_count += 1
        transition(task, "RUNNING")
        session.flush()
        event(
            session,
            task,
            "claimed",
            attempt_id=attempt.id,
            worker=executor_id == "local_test",
        )
        return task_envelope(session, task, attempt, token)


def attempt_for(session, task, body):
    attempt = session.get(TaskAttempt, body.attempt_id)
    if (
        attempt is None
        or attempt.task_id != task.id
        or not secrets.compare_digest(
            attempt.token_hash, hashed(body.lease_token.get_secret_value())
        )
    ):
        raise HTTPException(403, "执行凭证无效，请重新读取任务状态")
    return attempt


def active(task, attempt):
    return (
        task.state == "RUNNING"
        and task.active_attempt_id == attempt.id
        and attempt.state == "RUNNING"
        and (
            min(
                utc(attempt.lease_until),
                utc(attempt.deadline_at),
                utc(task.deadline_at),
            )
            > now()
        )
    )


def heartbeat(db, task_id, body):
    with db.sessions.begin() as session:
        task = locked_task(session, task_id)
        attempt = attempt_for(session, task, body)
        if not active(task, attempt):
            raise HTTPException(409, "本次执行已结束或过期，不能续期")
        attempt.lease_until = min(
            utc(task.deadline_at),
            utc(attempt.deadline_at),
            now() + timedelta(seconds=LEASE_SECONDS),
        )
        return {"lease_until": attempt.lease_until}


def checkpoint(db, task_id, body: CheckpointInput):
    data = {"step": body.step, "payload": body.payload}
    signature = fingerprint(data)
    with db.sessions.begin() as session:
        task = locked_task(session, task_id)
        attempt = attempt_for(session, task, body)
        previous = session.scalar(
            select(TaskCheckpoint).where(
                TaskCheckpoint.attempt_id == attempt.id,
                TaskCheckpoint.sequence == body.sequence,
            )
        )
        if previous is not None:
            if previous.fingerprint != signature:
                raise HTTPException(409, "同一检查点序号不能替换已有内容")
            return {"id": previous.id}
        if not active(task, attempt):
            raise HTTPException(409, "本次执行已结束或过期，检查点未保存")
        highest = (
            session.scalar(
                select(func.max(TaskCheckpoint.sequence)).where(
                    TaskCheckpoint.attempt_id == attempt.id
                )
            )
            or 0
        )
        if body.sequence != highest + 1:
            raise HTTPException(409, "检查点序号必须连续递增")
        item = TaskCheckpoint(
            task_id=task.id,
            attempt_id=attempt.id,
            sequence=body.sequence,
            step=body.step,
            payload=body.payload,
            fingerprint=signature,
        )
        session.add(item)
        session.flush()
        event(
            session,
            task,
            "checkpoint",
            checkpoint_id=item.id,
            worker=task.executor_id == "local_test",
        )
        return {"id": item.id}


def receipt(item):
    return {"id": item.id, "disposition": item.disposition, "reason": item.reason}


def submit(db, task_id, body: ResultEnvelope):
    data = body.model_dump(mode="json", exclude={"lease_token"})
    signature = fingerprint(data)
    with db.sessions.begin() as session:
        task = locked_task(session, task_id)
        attempt = attempt_for(session, task, body)
        if body.task_id != task.id or body.executor_id != task.executor_id:
            raise HTTPException(422, "结果与任务或执行者不匹配")
        previous = session.scalar(
            select(TaskSubmission).where(
                TaskSubmission.attempt_id == attempt.id,
                TaskSubmission.submission_key == body.submission_id,
            )
        )
        if previous is not None:
            if previous.fingerprint != signature:
                raise HTTPException(409, "同一结果标识不能提交不同内容")
            return receipt(previous)
        count = session.scalar(
            select(func.count())
            .select_from(TaskSubmission)
            .where(TaskSubmission.attempt_id == attempt.id)
        )
        if count >= 16:
            raise HTTPException(429, "本次执行的结果回执已达到 16 份上限")
        disposition, reason = "ACCEPTED", None
        if not active(task, attempt):
            disposition, reason = "QUARANTINED", "ATTEMPT_INACTIVE"
        elif not current_input(session, task):
            disposition, reason = "QUARANTINED", "INPUT_STALE"
            transition(task, "FAILED", reason)
            attempt.state = "FAILED"
        elif set(body.evidence_revision_ids) - {task.input_revision_id} or any(
            item.kind != task.task_type for item in body.output_artifacts
        ):
            disposition, reason = "QUARANTINED", "OUTPUT_SCOPE_INVALID"
            transition(task, "FAILED", reason)
            attempt.state = "FAILED"
        elif body.result_status == "failed":
            disposition, reason = "FAILED", body.error_code or "INVALID_OUTPUT"
            retry = (
                reason in {"TEMPORARY_ERROR", "LOCAL_EXECUTION_FAILED"}
                and task.executor_id == "local_test"
                and task.attempt_count < task.max_attempts
            )
            transition(task, "RETRY_WAIT" if retry else "FAILED", reason)
            task.available_at = now() + timedelta(seconds=2**task.attempt_count)
            attempt.state = "FAILED"
        elif (
            body.result_status != "success"
            or body.finish_reason != "complete"
            or not body.output_artifacts
            or body.evidence_revision_ids != [task.input_revision_id]
            or any(
                not item.content.strip() or not item.title.strip()
                for item in body.output_artifacts
            )
            or body.error_code
        ):
            disposition, reason = "PARTIAL", "OUTPUT_INCOMPLETE"
            transition(task, "PAUSED", reason)
            attempt.state = "PARTIAL"
        else:
            transition(task, "SUCCEEDED")
            attempt.state = "SUCCEEDED"
        item = TaskSubmission(
            task_id=task.id,
            attempt_id=attempt.id,
            submission_key=body.submission_id,
            fingerprint=signature,
            disposition=disposition,
            reason=reason,
            envelope=data,
        )
        session.add(item)
        session.flush()
        event(
            session,
            task,
            "result_received",
            submission_id=item.id,
            disposition=disposition,
            reason=reason,
            worker=task.executor_id == "local_test",
        )
        return receipt(item)


def control(db, task_id, action, expected_version):
    with db.sessions.begin() as session:
        task = locked_task(session, task_id)
        if task.version != expected_version:
            raise HTTPException(409, "任务状态已变化，请刷新后重试")
        if task.state in TERMINAL:
            raise HTTPException(409, "任务已经结束")
        if action == "resume":
            if (
                task.state != "PAUSED"
                or task.attempt_count >= task.max_attempts
                or utc(task.deadline_at) <= now()
            ):
                raise HTTPException(409, "任务不处于可恢复状态，或已达到次数／期限上限")
            if session.scalar(
                select(CreativeTask.id).where(
                    CreativeTask.id == task_id, dependency_condition(True)
                )
            ):
                raise HTTPException(409, "依赖任务已经失败或取消，请核对依赖后新建任务")
            if not current_input(session, task):
                raise HTTPException(409, "输入正文已经改变，请使用新版本创建任务")
            transition(task, "QUEUED")
            task.available_at = now()
        elif action in {"pause", "cancel"}:
            if task.state == "PAUSED" and action == "pause":
                return {"state": task.state}
            if task.active_attempt_id:
                attempt = session.get(TaskAttempt, task.active_attempt_id)
                if attempt.state == "RUNNING":
                    attempt.state = "CANCELLED" if action == "cancel" else "PAUSED"
            transition(
                task, "CANCELLED" if action == "cancel" else "PAUSED", "USER_REQUEST"
            )
        else:
            raise HTTPException(404, "操作不存在")
        event(session, task, action)
        return {"state": task.state}


def takeover(db, task_id, expected_version):
    with db.sessions.begin() as session:
        task = locked_task(session, task_id)
        attempt = (
            session.get(TaskAttempt, task.active_attempt_id)
            if task.active_attempt_id
            else None
        )
        if (
            task.executor_id != "human"
            or task.version != expected_version
            or attempt is None
            or not active(task, attempt)
        ):
            raise HTTPException(409, "任务状态已变化或已过期，请刷新后恢复")
        token = secrets.token_urlsafe(32)
        attempt.token_hash = hashed(token)
        task.version += 1
        event(session, task, "takeover", attempt_id=attempt.id)
        return task_envelope(session, task, attempt, token)


def dependency_condition(failed_only=False):
    dependency = aliased(CreativeTask)
    return exists(
        select(1)
        .select_from(TaskDependency)
        .join(dependency, dependency.id == TaskDependency.depends_on_id)
        .where(
            TaskDependency.task_id == CreativeTask.id,
            dependency.state.in_(["FAILED", "CANCELLED"])
            if failed_only
            else dependency.state != "SUCCEEDED",
        )
    )


def recover(db, limit=100):
    with db.sessions() as session:
        candidates = list(
            session.scalars(
                select(CreativeTask.id)
                .outerjoin(
                    TaskAttempt, TaskAttempt.id == CreativeTask.active_attempt_id
                )
                .where(
                    CreativeTask.state.not_in(TERMINAL),
                    (
                        (CreativeTask.state.in_(["QUEUED", "RETRY_WAIT"]))
                        & dependency_condition(True)
                    )
                    | (CreativeTask.deadline_at <= now())
                    | (
                        (CreativeTask.state == "RUNNING")
                        & (
                            (TaskAttempt.lease_until <= now())
                            | (TaskAttempt.deadline_at <= now())
                        )
                    ),
                )
                .order_by(CreativeTask.created_at)
                .limit(limit)
            )
        )
    for task_id in candidates:
        with db.sessions.begin() as session:
            task = locked_task(session, task_id)
            if task.state in TERMINAL:
                continue
            failed_dependency = session.scalar(
                select(CreativeTask.id).where(
                    CreativeTask.id == task_id, dependency_condition(True)
                )
            )
            if failed_dependency and task.state in {"QUEUED", "RETRY_WAIT"}:
                transition(task, "PAUSED", "DEPENDENCY_FAILED")
                event(session, task, "blocked", reason=task.reason, worker=True)
                continue
            attempt = (
                session.get(TaskAttempt, task.active_attempt_id)
                if task.active_attempt_id
                else None
            )
            expired = (
                task.state == "RUNNING"
                and attempt is not None
                and min(utc(attempt.lease_until), utc(attempt.deadline_at)) <= now()
            )
            deadline = utc(task.deadline_at) <= now()
            if not expired and not deadline:
                continue
            if attempt is not None and attempt.state == "RUNNING":
                attempt.state = "EXPIRED"
            if deadline or task.attempt_count >= task.max_attempts:
                transition(
                    task,
                    "FAILED",
                    "DEADLINE_EXCEEDED" if deadline else "ATTEMPTS_EXHAUSTED",
                )
            else:
                transition(
                    task,
                    "RETRY_WAIT" if task.executor_id == "local_test" else "PAUSED",
                    "LEASE_EXPIRED",
                )
                task.available_at = now() + timedelta(seconds=2**task.attempt_count)
            event(
                session,
                task,
                "recovered",
                state=task.state,
                reason=task.reason,
                worker=True,
            )
    return len(candidates)


def run_local(db):
    recover(db)
    with db.sessions() as session:
        candidates = list(
            session.scalars(
                select(CreativeTask.id)
                .where(
                    CreativeTask.executor_id == "local_test",
                    CreativeTask.state.in_(["QUEUED", "RETRY_WAIT"]),
                    CreativeTask.available_at <= now(),
                    ~dependency_condition(),
                )
                .order_by(
                    CreativeTask.priority.desc(),
                    CreativeTask.created_at,
                    CreativeTask.id,
                )
                .limit(50)
            )
        )
    for task_id in candidates:
        envelope = try_claim(db, task_id, "local_test")
        if envelope is None:
            continue
        lease = {
            "attempt_id": envelope["attempt_id"],
            "lease_token": envelope["lease_token"],
        }
        content = envelope["input"]["content"]
        prior = envelope["checkpoint"]
        # Only checkpoints produced by this executor's validated step are reusable.
        stats = {
            "sha256": envelope["input"]["sha256"],
            "characters": len(content),
            "paragraphs": len([line for line in content.splitlines() if line.strip()]),
        }
        reused = bool(
            prior and prior["step"] == "local_analysis" and prior["payload"] == stats
        )
        checkpoint(
            db,
            task_id,
            CheckpointInput(**lease, sequence=1, step="local_analysis", payload=stats),
        )
        submit(
            db,
            task_id,
            ResultEnvelope(
                **lease,
                task_id=task_id,
                executor_id="local_test",
                submission_id="local-analysis-result",
                result_status="success",
                finish_reason="complete",
                output_artifacts=[
                    {
                        "kind": "analyze",
                        "title": "本地流程校验结果",
                        "content": f"字符数：{stats['characters']}\n非空段落数：{stats['paragraphs']}\n输入 SHA-256：{stats['sha256']}\n这是确定性流程校验，不是模型创作或质量审查。",
                    }
                ],
                evidence_revision_ids=[envelope["input"]["revision_id"]],
                warnings=["恢复并核对了已有检查点"] if reused else [],
            ),
        )
        return True
    return False
