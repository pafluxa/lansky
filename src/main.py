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
from src.models.transaction import TransactionRequest, TransactionResponse
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
    has_description INTEGER NOT NULL DEFAULT 0,
    description TEXT DEFAULT NULL
);
"""


def init_db() -> None:
    db_path = Path(config.SQLITE_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(CREATE_TRANSACTIONS_TABLE)
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
