from fastapi import APIRouter, Request
from sqlalchemy import select
from app.platform import executor_service as service
from app.platform.executor_protocol import (
    ExecutorCreate,
    ExecutorUpdate,
    PolicyInput,
    RouteInput,
)
from app.platform.models import Executor
from app.platform.works import require_work
from app.platform.model_protocol import endpoint_parts
from fastapi import HTTPException


def check_profile(body, request):
    if body.api_config is not None:
        try:
            endpoint_parts(
                body.api_config.endpoint,
                test_loopback=request.app.state.settings.environment == "test",
            )
        except ValueError:
            raise HTTPException(
                422, "模型地址须为公网 HTTPS，不能包含凭证或查询参数"
            ) from None


router = APIRouter(prefix="/api/v1", tags=["executors"])


@router.get("/executors")
def listing(request: Request):
    with request.app.state.database.sessions() as session:
        return [
            service.data(
                item, test_mode=request.app.state.settings.environment == "test"
            )
            for item in session.scalars(select(Executor).order_by(Executor.id))
        ]


@router.post("/executors", status_code=201)
def register(body: ExecutorCreate, request: Request):
    check_profile(body, request)
    return {"id": service.register(request.app.state.database, body)}


@router.put("/executors/{executor_id}")
def configure(executor_id: str, body: ExecutorUpdate, request: Request):
    check_profile(body, request)
    return {"version": service.configure(request.app.state.database, executor_id, body)}


@router.get("/works/{work_id}/execution-policy")
def policy(work_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        return service.policy(session, work_id)


@router.put("/works/{work_id}/execution-policy")
def configure_policy(work_id: str, body: PolicyInput, request: Request):
    return {
        "version": service.configure_policy(request.app.state.database, work_id, body)
    }


@router.post("/executor-routing/preview")
def preview(body: RouteInput, request: Request):
    with request.app.state.database.sessions() as session:
        require_work(session, body.work_id)
        return service.evaluate(
            session,
            body.work_id,
            body.task_type,
            body.input_tokens,
            body.max_output_tokens,
        )
