# CLAUDE.md — Lansky Demo

## What Is This

Lansky is a personal finance intelligence system. This is a **demo build** — minimal, functional, showing the core concept: an AI that learns to categorize financial transactions from user feedback, using a similarity graph instead of predefined taxonomies.

## Architecture Overview

Two independent agents, one shared data layer, one intelligence graph.

```
                  ┌─────────────────────┐
  User ◄────────► │  Conversation Agent  │ ◄──── Web Chat UI (FastAPI + HTML/JS)
                  │  (Pydantic-AI)       │
                  └────────┬────────────┘
                           │ uses tools
                           ▼
                  ┌─────────────────────┐
                  │   Shared Tool Layer  │
                  │  • SQL Tool          │
                  │  • Graph Engine      │
                  │  • MCP Code Executor │
                  └─────────────────────┘
                           ▲
                           │ uses tools
                  ┌────────┴────────────┐
  Webhook ──────► │  Transaction Agent   │ ◄──── REST API (FastAPI)
                  │  (pure Python)       │
                  └─────────────────────┘
```

### Agent 1: Conversation Agent
- **Exposed to user** via web chat UI
- Pydantic-AI agent with `anthropic:claude-sonnet-4-6` as model
- Two modes:
  - **Proactive**: On chat open, checks SQLite for transactions where `has_description = false`. Surfaces them one at a time asking the user to categorize. Example: "I got a charge of $12,500 to 'ACME CORP' on March 8th — what is this?"
  - **Reactive**: Answers financial queries using the SQL tool and graph engine. Example: "How much did I spend on rent this year?"
- Has access to: SQL tool, Graph Engine, MCP Code Executor
- Maintains chat history between requests (message_history pattern from Pydantic-AI)

### Agent 2: Transaction Agent
- **NOT exposed to user** — REST endpoint only
- **Pure Python** — no LLM, no pydantic-ai. Deterministic validation and dedup only.
- Receives raw transaction data via `POST /api/transactions`
- Validates: amount > 0, valid date/time, non-empty from/to
- Dedup: calls `sql_tool.find_potential_duplicates` (Jaro-Winkler ≥ 0.85 on merchant name)
- On clean insert: generates UUID, calls `sql_tool.insert_transaction`, returns `status="stored"`
- Returns `status="duplicate"` or `status="possible_update"` (with field-level diffs) when a match is found
- Request body is a JSON object matching the Transaction schema

### Web UI
- Simple HTML/JS chat interface served by FastAPI
- Streams responses from Conversation Agent
- Single page: chat window + message input
- No framework needed — vanilla JS with fetch API

## Tech Stack

- **Python 3.11+**
- **pydantic-ai** (v1.x) — agent framework. Model: `anthropic:claude-sonnet-4-6`
- **FastAPI** — serves both the chat UI backend and the transaction REST endpoint
- **SQLite** — single `transactions` table (containerized, or local file for demo)
- **NetworkX** — similarity graph + Louvain community detection
- **Jaro-Winkler** (jellyfish or python-Levenshtein) — merchant name similarity
- **MCP Code Executor** — existing container, connected via FastMCP client

## Environment Variables

```
ANTHROPIC_API_KEY=sk-ant-...
SQLITE_DB_PATH=./lansky.db          # path to SQLite database file
MCP_CODE_EXECUTOR_URL=...           # URL of existing MCP code executor container (optional for demo)
```

## Deployment

This runs in Docker on the local workstation (hostname: haddock).

### Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install .
COPY src/ src/
COPY CLAUDE.md MEMORY.md ./
EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Running

```bash
# Build
docker build -t lansky-demo .

# Run — mount a volume for SQLite persistence, pass API key
docker run -d \
  --name lansky \
  -v lansky-data:/app/data \
  -p 8000:8000 \
  --env-file .env \
  lansky-demo
```

### Path Handling

`SQLITE_DB_PATH` must resolve correctly in both contexts:
- **In Docker**: defaults to `/app/data/lansky.db` (inside the mounted volume)
- **Local dev**: defaults to `./lansky.db`

In `config.py`, use:
```python
import os
SQLITE_DB_PATH = os.environ.get("SQLITE_DB_PATH", "/app/data/lansky.db")
```

The `.env` file lives at the project root (NOT copied into the image — passed at runtime via `--env-file`). It must contain at minimum:
```
ANTHROPIC_API_KEY=sk-ant-...
```

### .dockerignore

```
__pycache__
*.pyc
.env
lansky.db
.git
```

## Database Schema

Four tables in SQLite, all created in `init_db()` in `src/main.py`.

```sql
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,             -- UUID as text
    direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),  -- 'in' = income, 'out' = expense
    "from" TEXT NOT NULL,            -- name of money source
    "to" TEXT NOT NULL,              -- name of money sink
    date TEXT NOT NULL,              -- YYYY-MM-DD
    time TEXT NOT NULL,              -- HH:MM:SS
    amount INTEGER NOT NULL,         -- integer amount
    currency TEXT NOT NULL CHECK(currency IN ('CLP', 'USD', 'EUR')),
    has_description INTEGER NOT NULL DEFAULT 0,  -- boolean: 0 = false, 1 = true
    description TEXT DEFAULT NULL    -- max 128 chars, this IS the category
);

CREATE TABLE IF NOT EXISTS instruments (
    id TEXT PRIMARY KEY,             -- e.g. "cc:2722", "loan:hipotecario"
    type TEXT NOT NULL CHECK(type IN ('credit_card', 'loan', 'mortgage')),
    label TEXT NOT NULL,             -- e.g. "BCI Visa 2722"
    limit_clp INTEGER,               -- credit limit in CLP (credit cards only)
    limit_usd INTEGER                -- credit limit in USD (future use)
);

CREATE TABLE IF NOT EXISTS debt_items (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    total_amount INTEGER NOT NULL,
    currency TEXT NOT NULL CHECK(currency IN ('CLP', 'USD', 'EUR', 'UF')),
    installments INTEGER NOT NULL DEFAULT 1,
    installment_amt INTEGER NOT NULL,  -- monthly payment amount
    purchase_date TEXT NOT NULL        -- YYYY-MM-DD
);

CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL CHECK(currency IN ('CLP', 'USD', 'EUR', 'UF')),
    payment_date TEXT NOT NULL         -- YYYY-MM-DD
);
```

**Important**: `description` = category. They are interchangeable. `has_description` = "has been categorized by the user."

**Debt tracking notes**:
- `instruments` = financial instruments (credit cards, loans, mortgages)
- `debt_items` = installment purchases or loan draws, linked to a transaction and instrument
- `payments` = payments made against an instrument (e.g. monthly credit card payment)
- Active debt is computed in Python (not SQL): `remaining = installments - months_elapsed`
- UF (Unidad de Fomento) is supported in debt_items and payments but not in transactions

## Graph Intelligence Engine

This is the core innovation. Categories are **emergent**, not predefined.

### How It Works

1. **Nodes** = transaction IDs (every row in SQLite is a node)
2. **Edges** = weighted similarity between every pair of nodes
3. **Edge weight** = `sim(date) + sim(time) + sim(amount) + sim(merchant)`
4. **Community detection** (Louvain/Leiden via NetworkX) finds natural partitions
5. **Partitions = emergent categories** — most nodes in a cluster share a `description`

### Similarity Functions (all return float 0.0–1.0)

All four similarity functions are cheap scalar computations. NO deep learning, NO embeddings.

- **sim(date)**: Gaussian kernel on day-of-month distance. Periodic transactions (rent on the 1st) cluster. `exp(-((d1 - d2) % 30)^2 / (2 * sigma^2))` where sigma ≈ 3-5.
- **sim(time)**: Gaussian kernel on hour-of-day distance. Morning coffee vs evening groceries. Same formula on hour distance.
- **sim(amount)**: Log-scale proximity. `exp(-|log(a1) - log(a2)|^2 / (2 * sigma^2))`. Amounts of similar magnitude cluster regardless of exact value.
- **sim(merchant)**: Jaro-Winkler similarity on the relevant merchant name. For `direction='out'`, compare `to` fields. For `direction='in'`, compare `from` fields. Pure lexicographic, no ML.

### Partition Labeling — Purity + Minimum Support

A partition earns a label only when BOTH conditions are met:

```python
purity = count(dominant_label) / count(all_described_nodes_in_partition)
support = count(described_nodes_in_partition)

labeled = (purity >= TAU_P) and (support >= N_MIN)
```

**Demo defaults**: `TAU_P = 0.7`, `N_MIN = 3`

Below either threshold, the partition is "unresolved" — Lansky asks, doesn't guess.

### New Transaction Classification

When a new (uncategorized) transaction arrives:

1. Compute edge weights to all existing nodes
2. Determine which partition has the highest aggregate edge weight
3. Check if that partition is labeled (purity + support passed)
4. If yes → auto-categorize, notify user with explanation
5. If no → add to the "ask the user" queue

### Explainability

Decompose the composite edge weight back into its 4 dimensions:

> "I categorized this as 'rent' because the amount ($12,500) and merchant ('ACME CORP') strongly match your previous rent payments, even though the date (15th vs 1st) is unusual."

The explanation should highlight which dimensions contributed most and which were weak.

## Project Structure

```
lansky-demo/
├── CLAUDE.md                    # this file
├── MEMORY.md                    # project context and history
├── pyproject.toml               # dependencies
├── Dockerfile
├── .dockerignore
├── .env                         # ANTHROPIC_API_KEY (not committed, not in image)
│
├── src/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app, lifespan, routes
│   ├── config.py                # env vars, constants (TAU_P, N_MIN, sigmas)
│   │
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── conversation.py      # Conversation Agent (user-facing)
│   │   └── transaction.py       # Transaction Agent (REST ingestion)
│   │
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── sql_tool.py          # Read/write SQLite transactions table
│   │   └── graph_engine.py      # Build graph, run Louvain, classify, explain
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── transaction.py       # Pydantic models for Transaction, API request/response
│   │   ├── debt.py              # Instrument, DebtItem, Payment request/response models
│   │   └── extraction.py        # Bank email extraction models (discriminated union)
│   │
│   └── static/
│       └── index.html           # Chat UI (vanilla HTML/JS)
│
└── tests/                       # if time permits
    └── ...

lansky-extractor/                # standalone IMAP → LLM → API pipeline
├── config.py                    # env vars (IMAP, LLM, API, poll interval)
├── requirements.txt             # imap_tools, beautifulsoup4, httpx, pydantic, openai
├── .env.example                 # template for all required env vars
├── preprocessor.py              # HTML email → clean key-value text for LLM
├── llm_client.py                # calls local OpenAI-compatible LLM, returns EmailExtractionResult
├── pusher.py                    # POSTs extraction results to Lansky REST API
└── extractor.py                 # IMAP polling loop: fetch unseen → preprocess → extract → push
```

## Pydantic-AI Patterns to Follow

### Agent System Prompts

These are the EXACT system prompts to use. Do not improvise or summarize them.

#### Conversation Agent

```python
CONVERSATION_AGENT_INSTRUCTIONS = """You are Lansky, a personal finance assistant. You help the user understand and organize their financial transactions.

You operate in two modes:

## PROACTIVE MODE (default on conversation start)

At the start of every conversation, check for uncategorized transactions (has_description = false) using the SQL tool. If any exist:

1. Present ONE uncategorized transaction at a time. Format:
   "I have an uncategorized transaction: [direction] of [amount] [currency] [from/to context] on [date] at [time]. What would you call this?"
   - For direction='out': say "a payment of [amount] [currency] to [to] from [from]"
   - For direction='in': say "an income of [amount] [currency] from [from] to [to]"
2. Wait for the user's response. Their response IS the category. Store it as the description using the SQL tool and set has_description = true.
3. After storing, check the graph engine for classification results. If the graph now has enough data to auto-classify other uncategorized transactions in the same partition, report it:
   "Based on this, I've also categorized [N] similar transactions as '[category]' — they matched on [explanation from graph engine]."
4. Move to the next uncategorized transaction. If none remain, say:
   "All transactions are categorized. What would you like to know about your finances?"

Keep category prompts concise. Do not lecture the user about budgeting or offer unsolicited financial advice.

## REACTIVE MODE

When the user asks a question about their finances (instead of responding to a categorization prompt), switch to reactive mode:

1. Use the SQL tool to query the transactions table. Write correct SQL — remember the column names: id, direction, "from", "to", date, time, amount, currency, has_description, description.
2. If the query requires computation (aggregation, trends, comparisons), use the SQL tool for aggregation when possible. For complex analysis, use the MCP code executor to run Python.
3. Present results clearly. Use actual numbers. Do not hedge or add unnecessary caveats.
4. If the user's question is ambiguous, ask ONE clarifying question, then answer.

The user can switch between modes freely. If they're in the middle of categorization and ask "how much did I spend this month?", answer the question, then resume categorization.

## RULES

- Never invent transaction data. Only report what exists in the database.
- Never suggest categories. Wait for the user to name them. The whole point is that categories are the user's own language.
- When reporting auto-classifications from the graph engine, ALWAYS include the explanation (which similarity dimensions contributed). Never just say "I classified it" without saying why.
- Keep responses short. This is a chat, not an essay.
- Currency amounts: CLP has no decimals. USD and EUR use two decimals.
- You understand Spanish and English. Match the user's language.
"""
```

### Agent Definition Pattern
```python
from pydantic_ai import Agent, RunContext
from dataclasses import dataclass

@dataclass
class LanskyDeps:
    db_path: str
    # add graph engine instance, etc.

conversation_agent = Agent(
    'anthropic:claude-sonnet-4-6',
    deps_type=LanskyDeps,
    instructions=CONVERSATION_AGENT_INSTRUCTIONS,
)

@conversation_agent.tool
async def query_transactions(ctx: RunContext[LanskyDeps], sql_query: str) -> str:
    """Execute a read-only SQL query against the transactions database."""
    ...

@conversation_agent.tool
async def classify_transaction(ctx: RunContext[LanskyDeps], transaction_id: str) -> str:
    """Run the graph engine classification on a specific transaction. Returns the classification result with explanation, or 'unresolved' if confidence is insufficient."""
    ...

@conversation_agent.tool
async def update_transaction_description(ctx: RunContext[LanskyDeps], transaction_id: str, description: str) -> str:
    """Set the description (category) for a transaction and mark has_description = true. Then trigger graph reclassification for remaining uncategorized transactions in the same partition."""
    ...
```

### Chat History (message_history pattern)
```python
# Store messages in SQLite or in-memory between requests
# Pass to agent on each run:
result = await agent.run(prompt, message_history=previous_messages)
# Save new messages:
new_messages = result.new_messages()
```

### Streaming Responses
```python
async with agent.run_stream(prompt, message_history=messages) as result:
    async for text in result.stream_output(debounce_by=0.01):
        yield text
```

## Implementation Priority (90-minute budget)

1. **[15 min] Scaffold**: pyproject.toml, project structure, FastAPI app skeleton, SQLite init
2. **[15 min] Transaction Agent + REST endpoint**: POST /api/transactions → insert into SQLite
3. **[20 min] Graph Engine**: similarity functions, graph construction, Louvain partitioning, classification + explanation
4. **[20 min] Conversation Agent**: proactive mode (surface uncategorized), reactive mode (answer queries), tool wiring
5. **[10 min] Web UI**: HTML/JS chat page, streaming display
6. **[10 min] Integration test**: POST a few transactions, open chat, categorize them, watch graph learn

## Critical Constraints

- **Do NOT over-engineer**. This is a demo. No auth, no multi-user, no production error handling.
- **SQLite is a file** inside the Docker volume (`/app/data/lansky.db`). For local dev, `./lansky.db`.
- **Graph is rebuilt on demand** or cached in memory. No persistence of graph state — it's derived from SQLite data.
- **MCP Code Executor is optional** for the demo. Wire the tool interface but don't block on it.
- **Keep the web UI dead simple**. A single HTML file with inline JS. No React, no build step.
- **All similarity functions must be pure Python**. No numpy required (nice to have but not necessary).
- **Use `uuid4()` for transaction IDs** when inserting via the REST endpoint.
- **All paths must work inside Docker**. Use `config.SQLITE_DB_PATH`, never hardcode relative paths.
- **The .env file is NOT baked into the image**. It's passed at runtime via `--env-file`.

## Testing the Demo

```bash
# === Local dev ===
SQLITE_DB_PATH=./lansky.db uvicorn src.main:app --reload --port 8000

# === Docker ===
docker build -t lansky-demo .
docker run -d --name lansky -v lansky-data:/app/data -p 8000:8000 --env-file .env lansky-demo

# === Insert test transactions ===
curl -X POST http://localhost:8000/api/transactions \
  -H "Content-Type: application/json" \
  -d '{"direction":"out","from":"checking","to":"ACME CORP","date":"2025-03-01","time":"09:00:00","amount":12500,"currency":"CLP"}'

# === Open the chat UI ===
open http://localhost:8000

# Lansky should ask about uncategorized transactions
# After labeling a few, insert similar ones and watch auto-categorization
```
