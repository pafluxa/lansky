"""Transaction Agent — validates, deduplicates, and stores incoming transactions."""
import logging
import uuid

from pydantic_ai import Agent, RunContext
from dataclasses import dataclass

from src.models.transaction import TransactionRequest, TransactionResponse
from src.tools import sql_tool

log = logging.getLogger(__name__)


TRANSACTION_AGENT_INSTRUCTIONS = """You are the Lansky ingestion agent. You receive raw transaction data via a structured API call, validate it, and store it in the SQLite database.

## VALIDATION

Before storing anything, validate the incoming transaction:

1. **Sanity checks** (reject with a clear error message if any fail):
   - amount must be positive (> 0). Negative amounts are invalid — direction handles income vs expense.
   - date must be a real calendar date. "2025-02-30" is invalid. Parse it.
   - time must be valid HH:MM:SS. "25:00:00" is invalid.
   - direction must be exactly "in" or "out".
   - currency must be exactly "CLP", "USD", or "EUR".
   - "from" and "to" must be non-empty strings.

2. **Duplicate detection** (use the SQL tool to check):
   - Query for existing transactions with the same date, similar amount (exact match), and same merchant (compare "to" for direction='out', "from" for direction='in' — use fuzzy matching, not exact, because the same merchant might arrive as "ACME CORP" one time and "Acme Corp." the next).
   - If a likely duplicate is found:
     a. If the existing transaction has has_description = false and the duplicate also has no description → reject as duplicate, return the existing transaction's ID.
     b. If the existing transaction has has_description = true and the incoming data is identical → reject as duplicate.
     c. If the existing transaction has has_description = true but the incoming data has a DIFFERENT description in any field (from, to, amount, etc.) → this might be an update. Return a response indicating a potential duplicate was found with ID [existing_id], and describe what differs. Do NOT auto-update. The user or upstream system decides.

## STORAGE

If validation passes and no duplicate is found:
1. Generate a UUID for the id field.
2. Set has_description = false and description = null.
3. Insert the row into the transactions table using the SQL tool.
4. Return: {"id": uuid, "status": "stored"}

## RESPONSE FORMAT

Always return structured JSON:
- Success: {"id": "uuid", "status": "stored"}
- Validation error: {"status": "rejected", "reason": "amount must be positive"}
- Duplicate found: {"status": "duplicate", "existing_id": "uuid", "differences": null}
- Possible update: {"status": "possible_update", "existing_id": "uuid", "differences": {"field": "amount", "old": 12500, "new": 13000}}

Do not communicate with the user. Do not categorize. You are a validation and storage endpoint.
"""


@dataclass
class TransactionDeps:
    pass


agent: Agent[TransactionDeps, TransactionResponse] = Agent(
    "anthropic:claude-sonnet-4-6",
    deps_type=TransactionDeps,
    output_type=TransactionResponse,
    system_prompt=TRANSACTION_AGENT_INSTRUCTIONS,
)


@agent.tool
async def store_transaction(
    ctx: RunContext[TransactionDeps],
    direction: str,
    from_: str,
    to: str,
    date: str,
    time: str,
    amount: int,
    currency: str,
) -> str:
    """Insert a validated transaction into the database. Returns the new UUID."""
    tx_id = str(uuid.uuid4())
    sql_tool.insert_transaction(
        id=tx_id,
        direction=direction,
        from_=from_,
        to=to,
        date=date,
        time=time,
        amount=amount,
        currency=currency,
    )
    merchant = to if direction == "out" else from_
    log.info(
        "INGEST %s  %s  %s  %d %s  %s",
        direction.upper(), merchant, date, amount, currency, tx_id[:8],
    )
    return tx_id


@agent.tool
async def find_duplicates(
    ctx: RunContext[TransactionDeps],
    date: str,
    amount: int,
    merchant: str,
    direction: str,
) -> str:
    """
    Search for existing transactions on the same date with the same amount and
    a similar merchant name (Jaro-Winkler >= 0.85). Returns a JSON list.
    """
    import json
    results = sql_tool.find_potential_duplicates(
        date=date, amount=amount, merchant=merchant, direction=direction
    )
    log.info(
        "DEDUP check  merchant=%r  date=%s  amount=%d → %d candidate(s)",
        merchant, date, amount, len(results),
    )
    return json.dumps(results, ensure_ascii=False)


async def ingest(req: TransactionRequest) -> TransactionResponse:
    """Run the transaction agent on a single incoming request."""
    prompt = (
        f"Process this transaction:\n"
        f"direction={req.direction!r}\n"
        f"from={req.from_!r}\n"
        f"to={req.to!r}\n"
        f"date={req.date!r}\n"
        f"time={req.time!r}\n"
        f"amount={req.amount}\n"
        f"currency={req.currency!r}"
    )
    result = await agent.run(prompt, deps=TransactionDeps())
    log.info("TRANSACTION AGENT result: %s", result.output)
    return result.output
