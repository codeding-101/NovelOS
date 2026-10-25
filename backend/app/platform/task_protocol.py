"""Task/result contracts for manual, local and offline Agent execution; paid calls stay closed."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

TaskType = Literal["analyze", "draft", "revise", "review", "research"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def portable_json(self):
        encoded = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        if b"\\u0000" in encoded:
            raise ValueError("NUL is not supported")
        return self


class TaskInput(Contract):
    protocol_version: Literal["1.0"] = "1.0"
    work_id: str = Field(min_length=1, max_length=32)
    executor_id: str = Field(min_length=1, max_length=32)
    input_revision_id: str = Field(min_length=1, max_length=32)
    task_type: TaskType
    objective: str = Field(min_length=1, max_length=4000)
    request_key: str = Field(min_length=1, max_length=128)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    max_attempts: int = Field(default=3, ge=1, le=5)
    priority: int = Field(default=0, ge=-10, le=10)
    deadline_seconds: int = Field(default=86400, ge=60, le=604800)

    @field_validator("objective")
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError("Objective is required")
        return value.strip()


class LeaseInput(Contract):
    attempt_id: str = Field(min_length=1, max_length=32)
    lease_token: SecretStr = Field(min_length=16, max_length=128)


class CheckpointInput(LeaseInput):
    sequence: int = Field(ge=1, le=1000)
    step: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    payload: dict = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def bounded(cls, value):
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 32768:
            raise ValueError("Checkpoint exceeds 32 KiB")
        return value


class OutputArtifact(Contract):
    kind: TaskType
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=2 * 1024 * 1024)


class Usage(Contract):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_microunits: int = Field(default=0, ge=0)
    accounting: Literal["not_applicable"] = "not_applicable"

    @model_validator(mode="after")
    def local_only(self):
        if self.input_tokens or self.output_tokens or self.cost_microunits:
            raise ValueError(
                "Paid execution and external usage accounting are not enabled"
            )
        return self


class ResultEnvelope(LeaseInput):
    protocol_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1, max_length=32)
    executor_id: str = Field(min_length=1, max_length=32)
    submission_id: str = Field(min_length=1, max_length=128)
    result_status: Literal["success", "partial", "failed"]
    finish_reason: Literal["complete", "truncated", "error"]
    output_artifacts: list[OutputArtifact] = Field(default_factory=list, max_length=8)
    evidence_revision_ids: list[str] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    usage: Usage = Field(default_factory=Usage)
    error_code: (
        Literal["TEMPORARY_ERROR", "INVALID_OUTPUT", "LOCAL_EXECUTION_FAILED"] | None
    ) = None

    @model_validator(mode="after")
    def bounded_result(self):
        if (
            len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode())
            > 2 * 1024 * 1024
        ):
            raise ValueError("Result exceeds 2 MiB")
        if any(len(item) > 500 for item in self.warnings):
            raise ValueError("Warning too long")
        return self


class ControlInput(Contract):
    expected_version: int = Field(ge=1)


class InputSnapshot(Contract):
    revision_id: str
    asset_id: str
    sha256: str
    title: str
    content: str


class TaskPermissions(Contract):
    candidate_submission: Literal[True] = True
    write_assets: Literal[False] = False
    finalize: Literal[False] = False
    paid_calls: Literal[False] = False
    external_calls: Literal[False] = False


class TaskBudget(Contract):
    reserved_microunits: Literal[0] = 0
    accounting: Literal["not_applicable"] = "not_applicable"


class TaskEnvelope(Contract):
    protocol_version: Literal["1.0"]
    task_id: str
    work_id: str
    attempt_id: str
    executor_id: str
    task_type: TaskType
    objective: str
    lease_token: str = Field(repr=False)
    lease_until: datetime
    deadline_at: datetime
    input: InputSnapshot
    checkpoint: dict | None
    dependency_ids: list[str]
    attempt_number: int
    max_attempts: int
    permissions: TaskPermissions
    budget: TaskBudget
