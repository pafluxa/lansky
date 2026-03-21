"""Pydantic models for bank email extraction results."""

from typing import Annotated, Literal
from pydantic import BaseModel, Field


class ExtractionBase(BaseModel):
    category: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM:SS
    amount: int  # CLP as integer, USD/EUR as cents (×100)
    currency: Literal["CLP", "USD", "EUR"]
    bank_name: str = "Bank"


class ExpenseExtraction(ExtractionBase):
    category: Literal["expense"]
    merchant: str
    card_last4: str | None = None
    commerce_type: Literal["nacional", "internacional"] | None = None
    installments: int = 1


class TransferExtraction(ExtractionBase):
    category: Literal["transfer"]
    direction: Literal["outgoing", "incoming"] = "outgoing"
    counterparty: str
    source_account: str | None = None
    destination_account: str | None = None
    destination_bank: str | None = None
    message: str | None = None


class CardPaymentExtraction(ExtractionBase):
    category: Literal["card_payment"]
    card_last4: str
    source_account: str | None = None
    operation_number: str | None = None


class DebtPaymentExtraction(ExtractionBase):
    category: Literal["debt_payment"]
    payee: str
    creditor: str | None = None


Extraction = Annotated[
    ExpenseExtraction | TransferExtraction |
    CardPaymentExtraction | DebtPaymentExtraction,
    Field(discriminator="category")
]


class EmailExtractionResult(BaseModel):
    transactions: list[Extraction]
