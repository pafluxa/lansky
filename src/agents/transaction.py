"""Transaction ingestion — pure-Python validation, dedup, and storage."""
import logging
import uuid
from datetime import date as _date, time as _time

from src.models.transaction import TransactionRequest, TransactionResponse
from src.tools import sql_tool

log = logging.getLogger(__name__)


async def ingest(req: TransactionRequest) -> TransactionResponse:
    # --- Validation ---
    if req.amount <= 0:
        return TransactionResponse(status="rejected", reason="amount must be positive")

    try:
        _date.fromisoformat(req.date)
    except ValueError:
        return TransactionResponse(status="rejected", reason=f"invalid date: {req.date!r}")

    try:
        _time.fromisoformat(req.time)
    except ValueError:
        return TransactionResponse(status="rejected", reason=f"invalid time: {req.time!r}")

    if not req.from_.strip():
        return TransactionResponse(status="rejected", reason='"from" must not be empty')

    if not req.to.strip():
        return TransactionResponse(status="rejected", reason='"to" must not be empty')

    # --- Duplicate detection ---
    merchant = req.to if req.direction == "out" else req.from_
    candidates = sql_tool.find_potential_duplicates(
        date=req.date,
        amount=req.amount,
        merchant=merchant,
        direction=req.direction,
    )

    if candidates:
        existing = candidates[0]
        if not existing["has_description"]:
            return TransactionResponse(status="duplicate", existing_id=existing["id"])

        diffs: dict = {}
        if existing["from"] != req.from_:
            diffs["from"] = {"old": existing["from"], "new": req.from_}
        if existing["to"] != req.to:
            diffs["to"] = {"old": existing["to"], "new": req.to}
        if existing["time"] != req.time:
            diffs["time"] = {"old": existing["time"], "new": req.time}

        if diffs:
            return TransactionResponse(
                status="possible_update",
                existing_id=existing["id"],
                differences=diffs,
            )
        return TransactionResponse(status="duplicate", existing_id=existing["id"])

    # --- Storage ---
    tx_id = str(uuid.uuid4())
    sql_tool.insert_transaction(
        id=tx_id,
        direction=req.direction,
        from_=req.from_,
        to=req.to,
        date=req.date,
        time=req.time,
        amount=req.amount,
        currency=req.currency,
        source_type=req.source_type,
    )
    log.info(
        "INGEST %s  %s  %s  %d %s  %s",
        req.direction.upper(), merchant, req.date, req.amount, req.currency, tx_id[:8],
    )
    return TransactionResponse(status="stored", id=tx_id)
