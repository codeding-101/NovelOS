"""Task-context guards. Caller holds task, then work, before accounting or asset locks."""

import json
from fastapi import HTTPException
from app.platform import context_service
from app.platform.executor_service import lock_work
from app.platform.models import ContextSnapshot, TaskContext


def snapshot(session, task):
    binding = session.get(TaskContext, task.id)
    return session.get(ContextSnapshot, binding.snapshot_id) if binding else None


def lock_bound_work(session, task):
    if session.get(TaskContext, task.id):
        lock_work(session, task.work_id)


def validate_creation(session, body):
    if body.context_snapshot_id is None:
        return None
    lock_work(session, body.work_id)
    item = context_service.require_snapshot(
        session, body.work_id, body.context_snapshot_id
    )
    document = json.loads(item.document)
    if (
        item.input_revision_id != body.input_revision_id
        or document["objective"].strip() != body.objective
    ):
        raise HTTPException(
            409, "任务正文与目标须匹配所选快照；修改目标或正文后请新建快照"
        )
    if item.status != "READY" or not context_service.freshness(session, item)["fresh"]:
        raise HTTPException(409, "上下文快照已阻断或依据变化，请重新组装后创建任务")
    return item


def current(session, task):
    item = snapshot(session, task)
    return item is None or (
        item.status == "READY"
        and context_service.freshness(session, item, lock=True)["fresh"]
    )


def require_current(session, task):
    if not current(session, task):
        raise HTTPException(
            409, "上下文依据已变化，请使用新快照创建任务；旧任务不能换绑"
        )


def delivery(item):
    return (
        {"snapshot_id": item.id, "sha256": item.sha256, "document": item.document}
        if item
        else None
    )


def acknowledgement(envelope):
    value = envelope.get("context")
    return {key: value[key] for key in ("snapshot_id", "sha256")} if value else None


def matches(session, task, body):
    item = snapshot(session, task)
    if item is None:
        return body.protocol_version == "1.0" and body.context_ack is None
    return (
        body.protocol_version == "1.1"
        and body.context_ack is not None
        and body.context_ack.snapshot_id == item.id
        and body.context_ack.sha256 == item.sha256
    )


def summary(session, task):
    item = snapshot(session, task)
    if item is None:
        return None
    return {
        "snapshot_id": item.id,
        "title": item.title,
        "sha256": item.sha256,
        "freshness": context_service.freshness(session, item),
    }
