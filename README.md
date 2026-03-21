# Lansky

A personal finance intelligence system that learns your spending categories from conversation, not predefined taxonomies.

## What it does

Lansky ingests financial transactions (from bank email notifications or manual entry), stores them in SQLite, and uses a **similarity graph** with Louvain community detection to automatically classify new transactions based on patterns you teach it through conversation.

Instead of forcing you into generic categories like "Food & Dining", Lansky learns *your* language: "almuerzo", "uber", "arriendo" — whatever you call things.

### Core features

- **Graph-based auto-classification** — four similarity dimensions (merchant name, amount, time-of-day, day-of-month) with Gaussian kernels, community detection via Louvain, and purity-gated labeling
- **Conversational interface** — proactively surfaces uncategorized transactions, learns categories from your responses, explains its classifications
- **Debt tracking** — models credit card installment purchases (cuotas), tracks payments, computes remaining obligations and available credit
- **Email extraction** — standalone IMAP poller that uses a local LLM (via llama-server) to extract transactions from bank notification emails
- **Code execution** — MCP-based Python sandbox for complex financial analysis

## Architecture

```
                    ┌───────────────────────┐
    Browser ──────► │  FastAPI :8000        │
                    │                       │
    POST /api/tx ─► │  Transaction Agent    │ (pure Python, no LLM)
                    │  Conversation Agent   │ (pydantic-ai + Claude)
                    │                       │
                    │  Tools:               │
                    │   ├ SQL tool          │ → SQLite (WAL)
                    │   ├ Graph engine      │ → NetworkX + Louvain
                    │   └ MCP code executor │ → Docker sidecar :3333
                    └───────────────────────┘

    ┌───────────────────────┐
    │  lansky-extractor     │  (standalone)
    │                       │
    │  IMAP poll → preprocess → LLM extract → POST to Lansky API
    │  (imap_tools)  (bs4)    (llama-server)   (httpx)
    └───────────────────────┘
```

## Quick start

### Prerequisites

- Python 3.11+
- Docker and Docker Compose
- An Anthropic API key (for the conversation agent)

### 1. Configure

```bash
cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY
```

### 2. Run with Docker Compose

```bash
docker compose up -d
```

This starts the Lansky API on `:8000` and the MCP code executor on `:3333` (internal only).

### 3. Open the chat

Navigate to [http://localhost:8000](http://localhost:8000). Lansky will greet you and check for uncategorized transactions.

### 4. Ingest transactions

```bash
# Single transaction via curl
curl -X POST http://localhost:8000/api/transactions \
  -H "Content-Type: application/json" \
  -d '{
    "direction": "out",
    "from": "BCI Cuenta 00123456789",
    "to": "APPLE.COM/BILL",
    "date": "2026-03-16",
    "time": "18:15:00",
    "amount": 1003,
    "currency": "USD"
  }'

# Batch from email extractions
python blast_transactions.py --url http://localhost:8000 --limit 50
```

### 5. Register financial instruments (optional, for debt tracking)

In the chat, tell Lansky about your credit cards:

> "I have a BCI credit card ending in 1234 with a "cupo" of 1 million CLP"

Lansky will create the instrument. Future purchases on that card will automatically track installments and payments.

## Local development

```bash
# Install dependencies
pip install -e .

# Run without Docker (no MCP code executor)
SQLITE_DB_PATH=./lansky.db uvicorn src.main:app --reload --port 8000
```

## Email extractor

The `lansky-extractor/` directory contains a standalone IMAP poller for BCI bank notification emails.

### Setup

```bash
cd lansky-extractor
cp .env.example .env
# Edit .env with your IMAP credentials and BCI sender address
pip install -r requirements.txt
```

### Requirements

- A local LLM server (llama-server) running an OpenAI-compatible API
- Recommended model: Qwen3-8B at Q4_K_M quantization (~5 GB VRAM)

```bash
# Start the LLM server
./llama-server \
  -hf Qwen/Qwen3-8B-GGUF:Q4_K_M \
  --jinja -ngl 99 -fa --temp 0.0 \
  -c 4096 -n 2048 \
  --port 8081 --host 127.0.0.1
```

### Run

```bash
python extractor.py
```

Polls IMAP every 15 minutes (configurable via `POLL_INTERVAL_SECONDS`). Extracted transactions are pushed to Lansky's REST API. Credit card purchases automatically create debt items.

## Configuration

All configuration is via environment variables. See `.env.example` for the full list.

### Lansky (main app)

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key for the conversation agent |
| `SQLITE_DB_PATH` | `/app/data/lansky.db` | Path to SQLite database |
| `MODEL` | `anthropic:claude-sonnet-4-6` | pydantic-ai model string |
| `MCP_CODE_EXECUTOR_URL` | `http://code-executor:3333/mcp` | MCP code executor endpoint |
| `ENABLE_THINKING` | `false` | Enable extended thinking for the conversation agent |
| `THINKING_BUDGET_TOKENS` | `5000` | Token budget for extended thinking |

### Email extractor

| Variable | Default | Description |
|---|---|---|
| `IMAP_HOST` | `imap.mail.me.com` | IMAP server |
| `IMAP_USER` | (required) | IMAP username |
| `IMAP_PASSWORD` | (required) | IMAP password (app-specific password) |
| `IMAP_FOLDER` | `INBOX` | IMAP folder to poll |
| `BCI_SENDER` | (required) | Bank notification sender email |
| `LLM_BASE_URL` | `http://127.0.0.1:8081/v1` | Local LLM server URL |
| `LLM_MODEL` | `qwen3-8b` | Model name for the LLM server |
| `LANSKY_API_URL` | `http://127.0.0.1:8000` | Lansky API URL |
| `POLL_INTERVAL_SECONDS` | `900` | Polling interval in seconds |

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Chat UI |
| `POST` | `/api/chat` | Streaming chat (conversation agent) |
| `POST` | `/api/transactions` | Ingest a transaction |
| `POST` | `/api/instruments` | Register a financial instrument |
| `GET` | `/api/instruments` | List all instruments |
| `POST` | `/api/debt_items` | Record a debt item (used by extractor) |
| `POST` | `/api/payments` | Record a payment (used by extractor) |
| `DELETE` | `/api/transactions` | Reset all transactions + chat history (dev only) |

## How the graph engine works

1. Every transaction is a node in a fully-connected weighted graph
2. Edge weights are the sum of four similarity kernels: merchant name (Jaro-Winkler), amount (log-Gaussian), time-of-day (periodic Gaussian), day-of-month (periodic Gaussian)
3. Louvain community detection finds natural clusters (partitions)
4. A partition earns a label when ≥70% of its labeled nodes share a description AND ≥3 nodes are labeled
5. New transactions are classified by aggregate similarity to each labeled partition
6. Classifications include an explanation of which dimensions contributed

## License

MIT
