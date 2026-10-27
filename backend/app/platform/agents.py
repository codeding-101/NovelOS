from datetime import timedelta
import secrets
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field
from sqlalchemy import select
from app.platform.models import (
    AgentCredential,
    AgentTaskGrant,
    Executor,
    TaskAttempt,
    now,
)
from app.platform.security import hashed, utc
from app.platform.task_protocol import (
    Contract,
    CheckpointInput,
    LeaseInput,
    ResultEnvelope,
    TaskEnvelope,
)
from app.platform import (
    task_service as tasks,
    executor_service,
    budget_service,
    agent_security,
)

owner_router = APIRouter(
    prefix="/api/v1/tasks/{task_id}/agent-grants", tags=["agent-grants"]
)
router = APIRouter(prefix="/api/agent/v1/tasks/{task_id}", tags=["agent-gateway"])


class GrantInput(Contract):
    expected_version: int = Field(strict=True, ge=1)
    name: str = Field(min_length=1, max_length=128)
    expires_seconds: int = Field(default=3600, strict=True, ge=300, le=86400)


def metadata(credential):
    return {
        "id": credential.id,
        "name": credential.name,
        "expires_at": utc(credential.expires_at),
        "revoked_at": utc(credential.revoked_at) if credential.revoked_at else None,
    }


@owner_router.get("")
def grants(task_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        return [
            metadata(item)
            for item in session.scalars(
                select(AgentCredential)
                .join(
                    AgentTaskGrant, AgentTaskGrant.credential_id == AgentCredential.id
                )
                .where(AgentTaskGrant.task_id == task_id)
                .order_by(AgentCredential.expires_at.desc())
                .limit(32)
            )
        ]


@owner_router.post("", status_code=201)
def issue(task_id: str, body: GrantInput, request: Request):
    with request.app.state.database.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        executor = session.get(Executor, task.executor_id)
        if (
            not executor_service.offline_agent(executor)
            or task.state not in {"QUEUED", "RETRY_WAIT", "PAUSED"}
            or task.version != body.expected_version
        ):
            raise HTTPException(
                409, "只可为未执行的离线 Agent 任务签发令牌，请刷新；运行中请先暂停"
            )
        if not executor_service.ready_for_claim(session, task):
            raise HTTPException(409, "执行者或作品规则阻止执行，请先检查配置")
        rows = list(
            session.scalars(
                select(AgentCredential)
                .join(
                    AgentTaskGrant, AgentTaskGrant.credential_id == AgentCredential.id
                )
                .where(AgentTaskGrant.task_id == task_id)
            )
        )
        if len(rows) >= 32:
            raise HTTPException(409, "任务已达到 32 份授权记录上限")
        if any(
            item.revoked_at is None and utc(item.expires_at) > now() for item in rows
        ):
            raise HTTPException(409, "任务已有有效令牌；如未保存，请先撤销再签发")
        token = "nos_agent_" + secrets.token_urlsafe(32)
        item = AgentCredential(
            name=body.name.strip(),
            token_hash=hashed(token),
            scopes={
                "protocol": "offline_v1",
                "task_id": task.id,
                "executor_id": task.executor_id,
            },
            expires_at=now() + timedelta(seconds=body.expires_seconds),
        )
        if not item.name:
            raise HTTPException(422, "授权名称不能为空")
        session.add(item)
        session.flush()
        session.add(
            AgentTaskGrant(
                credential_id=item.id, task_id=task.id, executor_id=task.executor_id
            )
        )
        task.version += 1
        tasks.event(session, task, "agent_grant_issued", grant_id=item.id)
        return {
            **metadata(item),
            "token": token,
            "task_id": task.id,
            "protocol": "offline_v1",
        }


@owner_router.delete("/{grant_id}")
def revoke(task_id: str, grant_id: str, request: Request):
    with request.app.state.database.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        grant = session.get(AgentTaskGrant, grant_id)
        if grant is None or grant.task_id != task_id:
            raise HTTPException(404, "授权不存在")
        item = session.get(AgentCredential, grant_id)
        if item.revoked_at is None:
            item.revoked_at = now()
            attempt = (
                session.get(TaskAttempt, task.active_attempt_id)
                if task.active_attempt_id
                else None
            )
            if (
                attempt is not None
                and attempt.agent_credential_id == grant_id
                and task.state == "RUNNING"
            ):
                budget_service.abandon_attempt(session, task, attempt.id)
                attempt.state = "REVOKED"
                tasks.transition(task, "PAUSED", "AGENT_REVOKED")
            else:
                task.version += 1
            tasks.event(session, task, "agent_grant_revoked", grant_id=grant_id)
        return {"revoked": True}


def principal(request, task_id):
    agent = getattr(request.state, "agent", None)
    if agent is None or agent["task_id"] != task_id:
        raise HTTPException(403, "此令牌无权访问该任务")
    return agent


@router.get("")
def status(task_id: str, request: Request):
    agent = principal(request, task_id)
    with request.app.state.database.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        agent_security.authorize(session, task, agent)
        return {
            "id": task.id,
            "state": task.state,
            "version": task.version,
            "reason": task.reason,
            "attempt_count": task.attempt_count,
            "checkpoint": tasks.latest_checkpoint(session, task.id),
        }


@router.post("/claim", response_model=TaskEnvelope, response_model_exclude_unset=True)
def claim(task_id: str, request: Request):
    agent = principal(request, task_id)
    result = tasks.try_claim(
        request.app.state.database, task_id, agent["executor_id"], agent
    )
    if result is None:
        raise HTTPException(409, "任务不可领取，请由用户检查状态、期限和规则")
    return result


@router.post("/heartbeat")
def heartbeat(task_id: str, body: LeaseInput, request: Request):
    return tasks.heartbeat(
        request.app.state.database, task_id, body, principal(request, task_id)
    )


@router.post("/checkpoints")
def checkpoint(task_id: str, body: CheckpointInput, request: Request):
    return tasks.checkpoint(
        request.app.state.database, task_id, body, principal(request, task_id)
    )


@router.post("/results")
def result(task_id: str, body: ResultEnvelope, request: Request):
    return tasks.submit(
        request.app.state.database, task_id, body, principal(request, task_id)
    )
