from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator


class TransactionRequest(BaseModel):
    direction: Literal["in", "out"]
    from_: str = Field(alias="from")
    to: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM:SS
    amount: int
    currency: Literal["CLP", "USD", "EUR"]
    source_type: Literal["expense", "transfer", "card_payment", "debt_payment", "manual"] = "manual"

    model_config = {"populate_by_name": True}

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"invalid date format, expected YYYY-MM-DD: {v!r}")
        return v

    @field_validator("time")
    @classmethod
    def validate_time(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%H:%M:%S")
        except ValueError:
            raise ValueError(f"invalid time format, expected HH:MM:SS: {v!r}")
        return v


class Transaction(BaseModel):
    id: str
    direction: Literal["in", "out"]
    from_: str = Field(alias="from")
    to: str
    date: str
    time: str
    amount: int
    currency: Literal["CLP", "USD", "EUR"]
    source_type: Literal["expense", "transfer", "card_payment", "debt_payment", "manual"] = "manual"
    has_description: bool
    description: str | None

    model_config = {"populate_by_name": True}


class TransactionResponse(BaseModel):
    status: Literal["stored", "rejected", "duplicate", "possible_update"]
    id: str | None = None              # set on "stored"
    reason: str | None = None          # set on "rejected"
    existing_id: str | None = None     # set on "duplicate" / "possible_update"
    differences: dict | None = None    # set on "possible_update"
