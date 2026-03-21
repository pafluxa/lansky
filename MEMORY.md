# MEMORY.md — Lansky Project Context

## Who Is Building This

Pedro Fluxá Rojas. Senior Data Scientist / ML Engineer at Thoughtworks Chile. PhD in Astrophysics (PUC Chile). Deep background in HPC (CUDA since 2009, MPI+C), self-hosted infrastructure, and first-principles systems design. Chilean, based in Santiago.

## What Is Lansky

Lansky is a personal finance intelligence system — one component of a larger personal AI ecosystem Pedro is building under the Fluxanet/fluxa.org domain. Other planned components include Funes (documents), Javert (faces), and EDITH (security), but this demo focuses exclusively on Lansky.

### Previous Lansky Versions (context only — do NOT build these)

- **Lansky v1**: Pipeline monitoring iCloud mail for bank transaction emails (BCI, Banco de Chile), parsing with an LLM, pushing to Firefly III, exposed over XMPP via Prosody server. Two scripts: `banking_email_processor.py` + `firefly_mapper.py`. Worked well on live data.
- **Lansky v2 (design)**: Dropped SPADE, moved to a dumb XMPP router + two specialized flow agents + FastMCP tool serving + Ollama native tool calling + LaTeX/pgfplots PDF reports.

### This Demo (Lansky Web Demo)

A fresh, simplified build using:
- **Pydantic-AI** (v1.x) as the agent framework
- **Claude** (`anthropic:claude-sonnet-4-6`) as the intelligence provider
- **FastAPI** for both the web UI and REST ingestion
- **SQLite** for transaction storage
- **NetworkX** for the similarity graph and community detection
- **No XMPP, no Firefly III, no Ollama, no ChromaDB**

## The Core Idea

Categories are emergent, not predefined. Lansky does not have a list of categories. Instead:

1. Transactions arrive as raw data (no category)
2. A weighted similarity graph connects all transactions
3. Community detection (Louvain) finds natural clusters
4. Users label some transactions through conversation
5. When enough labeled nodes exist in a cluster (purity ≥ 0.7, support ≥ 3), the cluster earns a name
6. New transactions are auto-classified based on which labeled cluster they best fit

This means: Lansky learns YOUR categories from YOUR language. It doesn't impose "Food & Dining" or "Transportation" — it learns "almuerzo", "uber", "arriendo" from how you describe your own transactions.

## Pedro's Engineering Preferences

- **Named architectures over scripts**: Everything should have a name and a clear boundary. The graph engine is a module, not a function buried in a route handler.
- **First-principles thinking**: Don't import a library when 20 lines of math will do. The similarity functions are Gaussian kernels and Jaro-Winkler — no need for sklearn or sentence-transformers.
- **Type safety**: Use Pydantic models and type hints everywhere. Pedro appreciates the "if it compiles, it works" philosophy.
- **Clean separation**: Agents are agents. Tools are tools. The graph engine is the graph engine. Don't muddle them.
- **Self-hosted mindset**: Everything runs locally. No cloud services beyond the Claude API call.

## Key Design Decisions Already Made

1. **No ChromaDB** — the graph IS the memory. Similarity is explicit in edge weights, not implicit in vector space.
2. **No predefined categories** — categories emerge from graph partitions + user labels.
3. **description = category** — these are the same field. `has_description` means "has been categorized."
4. **Four similarity dimensions**: date (day-of-month), time (hour-of-day), amount (log-scale), merchant (Jaro-Winkler on from/to). All cheap scalars.
5. **Purity + support gating**: A partition needs ≥70% label agreement AND ≥3 labeled examples before Lansky trusts it.
6. **Conversation Agent is both proactive and reactive**: It surfaces uncategorized transactions AND answers queries.
7. **Transaction Agent is pure Python**: No LLM. Deterministic validation (`datetime.fromisoformat`, amount > 0, non-empty strings) + Jaro-Winkler dedup via `sql_tool.find_potential_duplicates`. Returns `stored` / `rejected` / `duplicate` / `possible_update`.
8. **Web UI is minimal**: HTML + vanilla JS. No React, no build step.

## lansky-extractor (standalone sibling project)

A separate Python project in `lansky-extractor/` that polls iCloud IMAP for BCI bank notification emails, extracts transaction data with a local LLM, and pushes results to Lansky's REST API.

Components:
- `config.py` — env vars: IMAP_HOST/USER/PASSWORD/FOLDER, BCI_SENDER, LLM_BASE_URL/MODEL, LANSKY_API_URL, POLL_INTERVAL_SECONDS
- `preprocessor.py` — parses BCI HTML email → clean key-value text (header phrase + table pairs)
- `llm_client.py` — calls local OpenAI-compatible LLM (llama-server at LLM_BASE_URL), validates response with `EmailExtractionResult` from `src/models/extraction.py`; 1 retry on parse/validation failure
- `pusher.py` — maps extraction types to Lansky transaction payloads, POSTs to `/api/transactions`; for `ExpenseExtraction` also POSTs to `/api/debt_items`; for `CardPaymentExtraction` also POSTs to `/api/payments`
- `extractor.py` — IMAP polling loop: fetch UNSEEN from BCI_SENDER → preprocess → extract → push; marks SEEN only when all pushes succeed; runs as `python lansky-extractor/extractor.py`

Imports `src/models/extraction.py` via `sys.path` injection (lansky-extractor/ is a sibling of src/).

## Related Work by Pedro (for context, not for building)

- **SYNAPSE**: Transformer architecture for vehicle buyback prediction at Mercedes-Benz USA. Two-phase architecture with Chiasma (temporal stratification) and Axon (multi-threaded data pipeline). Solo build.
- **SIGMOID**: AutoML via METIS graph partitioning. Open-sourced from MindsDB. The graph partitioning pattern in Lansky echoes this work.
- **Positronic Framework**: AI ethics framework — thoughts as order-invariant word sets, rules as probabilistic constraints on semantic-space transitions.
- **mabflow**: PID-controlled Multi-Armed Bandit system at ABInBev.

## Conversation Style Notes

Pedro thinks in architectures and named systems. He values precision in language and dislikes hand-waving. When explaining the system, be specific about which component does what. "The graph engine computes..." not "the system figures out..."