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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    async with conversation_agent.mcp_server:
        yield


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
    async def generate():
        async for chunk in conversation_agent.chat_stream(req.message):
            yield chunk

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Transaction ingestion
# ---------------------------------------------------------------------------

@app.post("/api/transactions", response_model=TransactionResponse, status_code=201)
def post_transaction(req: TransactionRequest) -> TransactionResponse:
    return transaction_agent.ingest(req)
