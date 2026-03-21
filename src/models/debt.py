"""Pydantic models for the debt tracking system."""

from typing import Literal
from pydantic import BaseModel


class InstrumentRequest(BaseModel):
    id: str
    type: Literal["credit_card", "loan", "mortgage"]
    label: str
    limit_clp: int | None = None
    limit_usd: int | None = None


class InstrumentResponse(BaseModel):
    status: Literal["created", "already_exists"]
    id: str


class DebtItemRequest(BaseModel):
    transaction_id: str
    instrument_id: str
    total_amount: int
    currency: Literal["CLP", "USD", "EUR", "UF"]
    installments: int = 1
    installment_amt: int
    purchase_date: str  # YYYY-MM-DD


class DebtItemResponse(BaseModel):
    status: Literal["created", "rejected"]
    id: str | None = None
    reason: str | None = None


class PaymentRequest(BaseModel):
    transaction_id: str
    instrument_id: str
    amount: int
    currency: Literal["CLP", "USD", "EUR", "UF"]
    payment_date: str  # YYYY-MM-DD


class PaymentResponse(BaseModel):
    status: Literal["created", "rejected"]
    id: str | None = None
    reason: str | None = None
