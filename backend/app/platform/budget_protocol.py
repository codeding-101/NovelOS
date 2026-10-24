"""Integer accounting units. One currency unit equals 1,000,000 microunits."""

from typing import Annotated, Literal
from pydantic import Field, field_validator
from app.platform.task_protocol import Contract

Units = Annotated[int, Field(strict=True, ge=0, le=10**12)]
Currency = Literal["CNY", "USD"]


class BudgetInput(Contract):
    expected_version: int = Field(strict=True, ge=0)
    currency: Currency
    limit_microunits: Units
    limit_tokens: Units


class ReservationInput(Contract):
    operation_key: str = Field(min_length=1, max_length=128)
    currency: Currency
    reserved_microunits: Units
    reserved_tokens: int = Field(strict=True, gt=0, le=10**12)


class SettlementInput(Contract):
    receipt_key: str = Field(min_length=1, max_length=128)
    actual_microunits: Units
    input_tokens: Units
    output_tokens: Units
    evidence: str = Field(min_length=1, max_length=2000)

    @field_validator("receipt_key", "evidence")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Evidence and receipt are required")
        return value.strip()


class ReconcileInput(SettlementInput):
    expected_version: int = Field(strict=True, ge=1)
