import logging

import httpx

from models import (
    CardPaymentExtraction,
    DebtPaymentExtraction,
    Extraction,
    ExpenseExtraction,
    TransferExtraction,
)

import config

log = logging.getLogger(__name__)


def push(extraction: Extraction) -> dict | None:
    payload = _build_transaction_payload(extraction)
    try:
        tx_response = _post(f"{config.LANSKY_API_URL}/api/transactions", payload)
    except httpx.HTTPError as exc:
        log.error("Failed to POST transaction: %s", exc)
        return None

    if tx_response.get("status") != "stored":
        log.warning("Transaction not stored: %s", tx_response)

    tx_id = tx_response.get("id")
    if not tx_id:
        return tx_response

    if isinstance(extraction, ExpenseExtraction) and extraction.card_last4 is not None:
        _push_debt_item(extraction, tx_id)
    elif isinstance(extraction, CardPaymentExtraction):
        _push_payment(extraction, tx_id)

    return tx_response


def _build_transaction_payload(e: Extraction) -> dict:
    if isinstance(e, ExpenseExtraction):
        direction = "out"
        from_ = f"CC {e.card_last4}" if e.card_last4 else "Bank"
        to = e.merchant
    elif isinstance(e, TransferExtraction):
        direction = "out" if e.direction == "outgoing" else "in"
        from_ = f"Cuenta {e.source_account}" if e.source_account else "Bank"
        to = e.counterparty
    elif isinstance(e, CardPaymentExtraction):
        direction = "out"
        from_ = f"Cuenta {e.source_account}" if e.source_account else "Bank"
        to = f"CC {e.card_last4}"
    else:
        assert isinstance(e, DebtPaymentExtraction)
        direction = "out"
        from_ = "Bank"
        to = e.payee

    return {
        "direction": direction,
        "from": from_,
        "to": to,
        "date": e.date,
        "time": e.time,
        "amount": e.amount,
        "currency": e.currency,
    }


def _push_debt_item(e: ExpenseExtraction, tx_id: str) -> None:
    instrument_id = f"cc:{e.card_last4}"
    payload = {
        "transaction_id": tx_id,
        "instrument_id": instrument_id,
        "total_amount": e.amount,
        "currency": e.currency,
        "installments": e.installments,
        "installment_amt": -(-e.amount // e.installments),
        "purchase_date": e.date,
    }
    try:
        resp = _post(f"{config.LANSKY_API_URL}/api/debt_items", payload)
        if resp.get("status") == "rejected":
            log.warning(
                "Instrument %s not registered. Debt item not created. "
                "Register it via Lansky chat.",
                instrument_id,
            )
    except httpx.HTTPError as exc:
        log.error("Failed to POST debt_item for tx %s: %s", tx_id, exc)


def _push_payment(e: CardPaymentExtraction, tx_id: str) -> None:
    instrument_id = f"cc:{e.card_last4}"
    payload = {
        "transaction_id": tx_id,
        "instrument_id": instrument_id,
        "amount": e.amount,
        "currency": e.currency,
        "payment_date": e.date,
    }
    try:
        resp = _post(f"{config.LANSKY_API_URL}/api/payments", payload)
        if resp.get("status") == "rejected":
            log.warning(
                "Instrument %s not registered. Payment not created. "
                "Register it via Lansky chat.",
                instrument_id,
            )
    except httpx.HTTPError as exc:
        log.error("Failed to POST payment for tx %s: %s", tx_id, exc)


def _post(url: str, payload: dict) -> dict:
    response = httpx.post(url, json=payload, timeout=10.0)
    response.raise_for_status()
    return response.json()
