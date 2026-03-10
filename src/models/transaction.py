from typing import Literal
from pydantic import BaseModel, Field


class TransactionRequest(BaseModel):
    direction: Literal["in", "out"]
    from_: str = Field(alias="from")
    to: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM:SS
    amount: int
    currency: Literal["CLP", "USD", "EUR"]

    model_config = {"populate_by_name": True}


class Transaction(BaseModel):
    id: str
    direction: Literal["in", "out"]
    from_: str = Field(alias="from")
    to: str
    date: str
    time: str
    amount: int
    currency: Literal["CLP", "USD", "EUR"]
    has_description: bool
    description: str | None

    model_config = {"populate_by_name": True}


class TransactionResponse(BaseModel):
    id: str
    status: str = "inserted"
