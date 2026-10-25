from __future__ import annotations
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from app.platform.models import (
    Asset,
    AssetRevision,
    CreativeTask,
    TaskAttempt,
    TaskDependency,
    TaskSubmission,
    TaskRoutingDecision,
)
from app.platform.task_protocol import (
    CheckpointInput,
    ControlInput,
    LeaseInput,
    ResultEnvelope,
    TaskInput,
    TaskEnvelope,
    TaskSpendingUpdate,
)
from app.platform import task_service as service
from app.platform import budget_service
from app.platform.works import require_work
from app.platform.security import utc

router = APIRouter(prefix="/api/v1", tags=["tasks"])


def task_data(task):
    return {
        key: utc(getattr(task, key))
        if isinstance(getattr(task, key), datetime)
        else getattr(task, key)
        for key in (
            "id",
            "work_id",
            "executor_id",
            "input_revision_id",
            "task_type",
            "objective",
            "state",
            "version",
            "priority",
            "attempt_count",
            "max_attempts",
            "deadline_at",
            "reason",
            "created_at",
        )
    }


@router.post("/tasks", status_code=201)
def create(body: TaskInput, request: Request):
    return {"id": service.create_task(request.app.state.database, body)}


@router.get("/tasks")
def tasks(
    request: Request,
    work_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        return [
            task_data(item)
            for item in session.scalars(
                select(CreativeTask)
                .where(CreativeTask.work_id == work_id)
                .order_by(CreativeTask.created_at.desc(), CreativeTask.id)
                .offset(offset)
                .limit(limit)
            )
        ]


@router.get("/tasks/{task_id}")
def detail(task_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        task = session.get(CreativeTask, task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        revision = session.get(AssetRevision, task.input_revision_id)
        routing = session.get(TaskRoutingDecision, task_id)
        return {
            **task_data(task),
            "spending_budget": budget_service.task_summary(session, task),
            "routing": {
                "requested_executor_id": routing.requested_executor_id,
                "selected_executor_id": routing.selected_executor_id,
                "snapshot": routing.snapshot,
            }
            if routing
            else None,
            "input": {
                "revision_id": revision.id,
                "title": revision.title,
                "sha256": revision.sha256,
                "current": session.get(Asset, revision.asset_id).head_version
                == revision.number,
            },
            "dependencies": [
                {"id": dep.id, "state": dep.state}
                for dep in session.scalars(
                    select(CreativeTask)
                    .join(
                        TaskDependency, TaskDependency.depends_on_id == CreativeTask.id
                    )
                    .where(TaskDependency.task_id == task_id)
                )
            ],
            "attempts": [
                {
                    "id": attempt.id,
                    "number": attempt.number,
                    "state": attempt.state,
                    "lease_until": utc(attempt.lease_until),
                    "deadline_at": utc(attempt.deadline_at),
                }
                for attempt in session.scalars(
                    select(TaskAttempt)
                    .where(TaskAttempt.task_id == task_id)
                    .order_by(TaskAttempt.number)
                )
            ],
            "checkpoint": service.latest_checkpoint(session, task_id),
            "submissions": [
                {
                    **service.receipt(item),
                    "attempt_id": item.attempt_id,
                    "created_at": utc(item.created_at),
                }
                for item in session.scalars(
                    select(TaskSubmission)
                    .where(TaskSubmission.task_id == task_id)
                    .order_by(TaskSubmission.created_at.desc(), TaskSubmission.id)
                    .limit(100)
                )
            ],
        }


@router.put("/tasks/{task_id}/budget")
def configure_task_budget(task_id: str, body: TaskSpendingUpdate, request: Request):
    return budget_service.configure_task(request.app.state.database, task_id, body)


@router.get("/tasks/{task_id}/submissions/{submission_id}")
def submission(task_id: str, submission_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        item = session.get(TaskSubmission, submission_id)
        if item is None or item.task_id != task_id:
            raise HTTPException(404, "候选结果不存在")
        return {
            **service.receipt(item),
            "envelope": item.envelope,
            "finalized": False,
            "applied_to_asset": False,
        }


@router.post("/tasks/{task_id}/claim", response_model=TaskEnvelope)
def claim(task_id: str, request: Request):
    service.recover(request.app.state.database)
    result = service.try_claim(request.app.state.database, task_id, "human")
    if result is None:
        raise HTTPException(409, "任务暂不可领取；请检查状态、依赖、输入版本和次数上限")
    return result


@router.post("/tasks/{task_id}/takeover", response_model=TaskEnvelope)
def takeover(task_id: str, body: ControlInput, request: Request):
    return service.takeover(request.app.state.database, task_id, body.expected_version)


@router.post("/tasks/{task_id}/heartbeat")
def heartbeat(task_id: str, body: LeaseInput, request: Request):
    return service.heartbeat(request.app.state.database, task_id, body)


@router.post("/tasks/{task_id}/checkpoints")
def checkpoint(task_id: str, body: CheckpointInput, request: Request):
    return service.checkpoint(request.app.state.database, task_id, body)


@router.post("/tasks/{task_id}/results")
def result(task_id: str, body: ResultEnvelope, request: Request):
    return service.submit(request.app.state.database, task_id, body)


@router.post("/tasks/{task_id}/control/{action}")
def control(task_id: str, action: str, body: ControlInput, request: Request):
    service.recover(request.app.state.database)
    return service.control(
        request.app.state.database, task_id, action, body.expected_version
    )
