"""Transaction Agent — dumb ingestion. REST in, SQL insert, done."""
import uuid

from src.models.transaction import TransactionRequest, TransactionResponse
from src.tools import sql_tool


def ingest(req: TransactionRequest) -> TransactionResponse:
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
    )
    return TransactionResponse(id=tx_id)
