"""Bearer grants are valid only on the Agent gateway and are checked in each transaction."""

import secrets
from fastapi import HTTPException
from sqlalchemy import select, update
from app.platform.models import AgentCredential, AgentTaskGrant, Executor, now
from app.platform.security import hashed, utc


def authenticate_agent(session, header):
    if not header.startswith("Bearer ") or not 32 <= len(header[7:]) <= 128:
        return None
    digest = hashed(header[7:])
    row = session.execute(
        select(AgentCredential, AgentTaskGrant)
        .join(AgentTaskGrant, AgentTaskGrant.credential_id == AgentCredential.id)
        .where(AgentCredential.token_hash == digest)
    ).first()
    if row is None:
        return None
    credential, grant = row
    if credential.revoked_at is not None or utc(credential.expires_at) <= now():
        return None
    return {
        "id": credential.id,
        "token_hash": digest,
        "task_id": grant.task_id,
        "executor_id": grant.executor_id,
    }


def authorize(session, task, principal):
    if principal is None:
        executor = session.get(Executor, task.executor_id)
        if executor.mode in {"agent", "api"}:
            raise HTTPException(403, "外部执行回报必须使用专属入口，不能使用用户会话代报")
        return
    session.execute(
        update(AgentCredential)
        .where(AgentCredential.id == principal["id"])
        .values(revoked_at=AgentCredential.revoked_at)
        .execution_options(synchronize_session=False)
    )
    credential = session.get(AgentCredential, principal["id"], populate_existing=True)
    grant = session.get(AgentTaskGrant, principal["id"])
    if (
        credential is None
        or grant is None
        or credential.revoked_at is not None
        or utc(credential.expires_at) <= now()
        or not secrets.compare_digest(credential.token_hash, principal["token_hash"])
        or grant.task_id != task.id
        or grant.executor_id != task.executor_id
    ):
        raise HTTPException(403, "Agent 授权无效、过期或不属于此任务")
