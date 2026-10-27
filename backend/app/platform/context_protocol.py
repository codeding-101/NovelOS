from pydantic import Field, field_validator
from app.platform.task_protocol import Contract


class MemorySelection(Contract):
    proposal_id: str = Field(min_length=1, max_length=32)
    required: bool = Field(default=True, strict=True)


class SnapshotInput(Contract):
    title: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=4000)
    input_revision_id: str = Field(min_length=1, max_length=32)
    memories: list[MemorySelection] = Field(default_factory=list, max_length=50)
    story_at: int | None = Field(default=None, strict=True, ge=0, le=10**12)
    capacity_bytes: int = Field(strict=True, ge=1, le=8 * 1024 * 1024)
    request_key: str = Field(min_length=1, max_length=128)

    @field_validator("title", "objective", "request_key")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank")
        return value

    @field_validator("memories")
    @classmethod
    def unique(cls, value):
        if len({row.proposal_id for row in value}) != len(value):
            raise ValueError("Memory selections must be unique")
        return value
