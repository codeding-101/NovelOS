from typing import Literal
from pydantic import Field, field_validator, model_validator
from app.platform.task_protocol import Contract, TaskType


class ExecutorSettings(Contract):
    agent_protocol: Literal["offline_v1"] | None = None
    name: str = Field(min_length=1, max_length=128)
    capabilities: list[TaskType] = Field(min_length=1, max_length=5)
    enabled: bool = False
    available: bool = True
    priority: int = Field(strict=True, ge=-100, le=100, default=0)
    currency: Literal["CNY", "USD"] | None = None
    input_rate: int | None = Field(default=None, strict=True, ge=0, le=10**12)
    output_rate: int | None = Field(default=None, strict=True, ge=0, le=10**12)
    context_tokens: int | None = Field(default=None, strict=True, ge=1, le=10**7)
    credential_id: str | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Name is required")
        return value.strip()

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Duplicate capabilities")
        if sum(
            value is not None
            for value in (self.currency, self.input_rate, self.output_rate)
        ) not in {0, 3}:
            raise ValueError("Currency and both prices must be supplied together")
        return self


class ExecutorCreate(ExecutorSettings):
    mode: Literal["api", "agent"]
    request_key: str = Field(min_length=1, max_length=128)


class ExecutorUpdate(ExecutorSettings):
    expected_version: int = Field(strict=True, ge=1)


class PolicyInput(Contract):
    expected_version: int = Field(strict=True, ge=0)
    allowed_ids: list[str] = Field(max_length=100)
    preferred_ids: list[str] = Field(max_length=100)
    auto_local_diagnostics: bool = False

    @model_validator(mode="after")
    def consistent(self):
        if any(
            len(set(values)) != len(values)
            or any(not 1 <= len(value) <= 32 for value in values)
            for values in (self.allowed_ids, self.preferred_ids)
        ):
            raise ValueError("Invalid or duplicate executor ids")
        if set(self.preferred_ids) - set(self.allowed_ids):
            raise ValueError("Preferred executors must be allowed")
        return self


class RouteInput(Contract):
    work_id: str = Field(min_length=1, max_length=32)
    task_type: TaskType
    input_tokens: int = Field(default=1000, strict=True, ge=0, le=10**7)
    max_output_tokens: int = Field(default=1000, strict=True, ge=1, le=10**6)
