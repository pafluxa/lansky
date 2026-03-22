import logging
import logging.config
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import config
from src.tools import sql_tool
from src.models.transaction import TransactionRequest, TransactionResponse
from src.models.debt import (
    InstrumentRequest, InstrumentResponse,
    DebtItemRequest, DebtItemResponse,
    PaymentRequest, PaymentResponse,
)
from src.agents import transaction as transaction_agent
from src.agents import conversation as conversation_agent

# ---------------------------------------------------------------------------
# Logging — one config, applied once at import time of main.py
# ---------------------------------------------------------------------------

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        }
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    # Quieten noisy third-party loggers
    "loggers": {
        "uvicorn.access": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    },
})

log = logging.getLogger(__name__)

CREATE_TRANSACTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
    "from" TEXT NOT NULL,
    "to" TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL CHECK(currency IN ('CLP', 'USD', 'EUR')),
    source_type TEXT NOT NULL DEFAULT 'manual' CHECK(source_type IN ('expense', 'transfer', 'card_payment', 'debt_payment', 'manual')),
    has_description INTEGER NOT NULL DEFAULT 0,
    description TEXT DEFAULT NULL
);
"""

CREATE_INSTRUMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS instruments (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('credit_card', 'debit_card', 'checking', 'savings', 'loan', 'mortgage')),
    label TEXT NOT NULL,
    limit_clp INTEGER,
    limit_usd INTEGER
);
"""

CREATE_DEBT_ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS debt_items (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    total_amount INTEGER NOT NULL,
    currency TEXT NOT NULL CHECK(currency IN ('CLP', 'USD', 'EUR', 'UF')),
    installments INTEGER NOT NULL DEFAULT 1,
    installment_amt INTEGER NOT NULL,
    purchase_date TEXT NOT NULL
);
"""

CREATE_PAYMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL CHECK(currency IN ('CLP', 'USD', 'EUR', 'UF')),
    payment_date TEXT NOT NULL
);
"""


def init_db() -> None:
    db_path = Path(config.SQLITE_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(CREATE_TRANSACTIONS_TABLE)
        conn.execute(CREATE_INSTRUMENTS_TABLE)
        conn.execute(CREATE_DEBT_ITEMS_TABLE)
        conn.execute(CREATE_PAYMENTS_TABLE)
        conn.commit()
    log.info("DB ready: %s", db_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("Connecting to MCP code executor: %s", config.MCP_CODE_EXECUTOR_URL)
    async with conversation_agent.mcp_server:
        log.info("MCP connection established. Lansky is ready.")
        yield
    log.info("MCP connection closed. Shutting down.")


app = FastAPI(title="Lansky", lifespan=lifespan)

# Static assets (JS, CSS if any) at /static/*
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)


# ---------------------------------------------------------------------------
# Chat UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html = (Path(__file__).parent / "static" / "index.html").read_text()
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Chat API — streaming
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest):
    log.info("USER: %s", req.message[:120])

    async def generate():
        chunks = []
        async for chunk in conversation_agent.chat_stream(req.message):
            chunks.append(chunk)
            yield chunk
        full = "".join(chunks)
        log.info("AGENT:\n%s", full)

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Transaction ingestion
# ---------------------------------------------------------------------------

@app.post("/api/transactions", response_model=TransactionResponse)
async def post_transaction(req: TransactionRequest) -> TransactionResponse:
    return await transaction_agent.ingest(req)


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------

@app.post("/api/instruments", response_model=InstrumentResponse)
async def post_instrument(req: InstrumentRequest) -> InstrumentResponse:
    existing = sql_tool.fetch_instrument(req.id)
    if existing:
        return InstrumentResponse(status="already_exists", id=req.id)
    sql_tool.insert_instrument(req.id, req.type, req.label, req.limit_clp, req.limit_usd)
    return InstrumentResponse(status="created", id=req.id)


@app.get("/api/instruments")
async def get_instruments():
    return sql_tool.fetch_instruments()


# ---------------------------------------------------------------------------
# Debt items
# ---------------------------------------------------------------------------

@app.post("/api/debt_items", response_model=DebtItemResponse)
async def post_debt_item(req: DebtItemRequest) -> DebtItemResponse:
    if not sql_tool.fetch_instrument(req.instrument_id):
        return DebtItemResponse(
            status="rejected",
            reason=f"instrument not found: {req.instrument_id}",
        )
    item_id = sql_tool.insert_debt_item(
        req.transaction_id,
        req.instrument_id,
        req.total_amount,
        req.currency,
        req.installments,
        req.installment_amt,
        req.purchase_date,
    )
    log.info(
        "DEBT_ITEM %s %s %s %sx %s",
        req.instrument_id, req.total_amount, req.currency,
        req.installments, req.purchase_date,
    )
    return DebtItemResponse(status="created", id=item_id)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

@app.post("/api/payments", response_model=PaymentResponse)
async def post_payment(req: PaymentRequest) -> PaymentResponse:
    if not sql_tool.fetch_instrument(req.instrument_id):
        return PaymentResponse(status="rejected", reason="instrument not found")
    payment_id = sql_tool.insert_payment(
        req.transaction_id,
        req.instrument_id,
        req.amount,
        req.currency,
        req.payment_date,
    )
    log.info(
        "PAYMENT %s %s %s %s",
        req.instrument_id, req.amount, req.currency, req.payment_date,
    )
    return PaymentResponse(status="created", id=payment_id)


# ---------------------------------------------------------------------------
# Dev utilities
# ---------------------------------------------------------------------------

@app.delete("/api/transactions")
async def reset_transactions():
    """Wipe all transactions and reset conversation history. Dev/demo use only."""
    db_path = Path(config.SQLITE_DB_PATH)
    with sqlite3.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.execute("DELETE FROM transactions")
        conn.commit()
    conversation_agent.reset_history()
    log.info("RESET: deleted %d transactions, cleared chat history", n)
    return {"deleted": n}
