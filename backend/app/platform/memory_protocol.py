from typing import Literal
from pydantic import Field, field_validator, model_validator
from app.platform.task_protocol import Contract

MemoryKind = Literal[
    "character", "relationship", "world_rule", "event", "foreshadow", "promise"
]


class StoryStateInput(Contract):
    subject_key: str = Field(min_length=1, max_length=128)
    predicate: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=1024)
    valid_from: int = Field(strict=True, ge=0, le=1000000000000)
    valid_until: int | None = Field(default=None, strict=True, ge=1, le=1000000000000)

    @field_validator("subject_key", "predicate", "value")
    @classmethod
    def trimmed(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def interval(self):
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError(
                "End must be greater than start; intervals are [start, end)"
            )
        return self


class ProposalInput(Contract):
    entry_key: str = Field(min_length=1, max_length=128)
    kind: MemoryKind
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=32768)
    source_revision_ids: list[str] = Field(min_length=1, max_length=20)
    base_proposal_id: str | None = Field(default=None, min_length=1, max_length=32)
    submission_id: str | None = Field(default=None, min_length=1, max_length=32)
    request_key: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=604800, strict=True, ge=60, le=604800)
    story_state: StoryStateInput | None = None

    @field_validator("entry_key", "title", "content", "request_key")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank")
        return value

    @field_validator("source_revision_ids")
    @classmethod
    def sources(cls, value):
        if len(set(value)) != len(value) or any(
            not item or len(item) > 32 for item in value
        ):
            raise ValueError("Invalid or duplicate source")
        return sorted(value)


class DecisionInput(Contract):
    outcome: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=4096)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Decision reason required")
        return value
