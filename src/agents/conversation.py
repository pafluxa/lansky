"""
Conversation Agent — user-facing, proactive + reactive.

Proactive mode: on session open, checks for uncategorized transactions and
surfaces them one at a time. When the user replies with a category, the agent
calls set_description to persist it and re-runs the graph to check whether
auto-classification is now possible for other pending transactions.

Reactive mode: answers free-form financial queries via SQL and the graph engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.messages import ModelMessage

from src import config
from src.tools import sql_tool, graph_engine

# MCP server instance — lifecycle managed by FastAPI lifespan in main.py
mcp_server = MCPServerStreamableHTTP(config.MCP_CODE_EXECUTOR_URL)


# ---------------------------------------------------------------------------
# Deps
# ---------------------------------------------------------------------------

@dataclass
class LanskyDeps:
    db_path: str


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are Lansky, a personal finance assistant. You help the user understand and organize their financial transactions.

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


agent = Agent(
    config.MODEL,
    deps_type=LanskyDeps,
    system_prompt=SYSTEM_PROMPT,
    toolsets=[mcp_server],
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@agent.tool
async def get_uncategorized_transactions(ctx: RunContext[LanskyDeps]) -> str:
    """Return all transactions that have not yet been categorized by the user."""
    rows = sql_tool.fetch_uncategorized()
    if not rows:
        return "All transactions are categorized."
    lines = []
    for r in rows:
        merchant = r["to"] if r["direction"] == "out" else r["from"]
        lines.append(
            f"id={r['id']} | {r['direction'].upper()} | {merchant} | "
            f"{r['amount']:,} {r['currency']} | {r['date']}"
        )
    return f"{len(rows)} uncategorized transaction(s):\n" + "\n".join(lines)


@agent.tool
async def set_description(
    ctx: RunContext[LanskyDeps], transaction_id: str, description: str
) -> str:
    """
    Persist a category label for a transaction and run the graph engine to
    check whether any other uncategorized transactions can now be auto-classified.
    Returns a summary of what was labeled and any auto-classifications triggered.
    """
    sql_tool.update_description(transaction_id, description)

    # Run graph engine to find auto-classifiable pending transactions
    pending = sql_tool.fetch_uncategorized()
    auto_labeled: list[str] = []

    if pending:
        nodes = None
        G = None
        partitions = None
        # Lazy-build once and reuse across all pending
        import src.tools.graph_engine as ge
        nodes = ge._load_nodes()
        G = ge.build_graph(nodes)
        partitions = ge.detect_partitions(G, nodes)

        for tx in pending:
            result = ge.classify(tx, nodes=nodes, G=G, partitions=partitions)
            if result.label is not None:
                sql_tool.update_description(tx["id"], result.label)
                merchant = tx["to"] if tx["direction"] == "out" else tx["from"]
                auto_labeled.append(
                    f"Auto-classified '{merchant}' on {tx['date']} as '{result.label}' "
                    f"(confidence {result.confidence:.2f}/4.00). {result.explanation}"
                )

    msg = f"Labeled transaction as '{description}'."
    if auto_labeled:
        msg += "\n\nAuto-classifications triggered:\n" + "\n".join(f"• {a}" for a in auto_labeled)
    return msg


@agent.tool
async def query_transactions(ctx: RunContext[LanskyDeps], sql_query: str) -> str:
    """
    Execute a read-only SELECT query against the transactions table and return
    results as a JSON string. Only SELECT statements are allowed.

    Schema:
      transactions(id, direction, "from", "to", date, time, amount, currency,
                   has_description, description)
    """
    try:
        rows = sql_tool.execute_read_query(sql_query)
        return json.dumps(rows, ensure_ascii=False)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Query failed: {e}"


@agent.tool
async def classify_transaction(ctx: RunContext[LanskyDeps], transaction_id: str) -> str:
    """
    Run the graph engine classifier on a specific transaction and return the
    result including which partition it maps to and the explainability breakdown.
    """
    all_rows = sql_tool.fetch_all()
    target = next((r for r in all_rows if r["id"] == transaction_id), None)
    if target is None:
        return f"Transaction {transaction_id} not found."

    _, result = graph_engine.run(classify_tx=target)
    if result is None:
        return "Could not classify — not enough data."

    return (
        f"Partition: {result.partition_id} | Label: {result.label!r} | "
        f"Confidence: {result.confidence:.3f}/4.000\n"
        f"Dimension scores: {result.dim_scores}\n"
        f"Explanation: {result.explanation}"
    )


# ---------------------------------------------------------------------------
# Session message store (in-memory, single-user demo)
# ---------------------------------------------------------------------------

_message_history: list[ModelMessage] = []


def reset_history() -> None:
    global _message_history
    _message_history = []


async def chat_stream(user_message: str) -> AsyncIterator[str]:
    """
    Stream a response to user_message, maintaining conversation history.

    Uses agent.iter() so that text generated both before and after tool
    calls is streamed — run_stream only captures the first model response node.
    """
    global _message_history
    deps = LanskyDeps(db_path=config.SQLITE_DB_PATH)

    from pydantic_ai._agent_graph import ModelRequestNode

    async with agent.iter(
        user_message,
        deps=deps,
        message_history=_message_history,
    ) as agent_run:
        async for node in agent_run:
            if isinstance(node, ModelRequestNode):
                async with node.stream(agent_run.ctx) as agent_stream:
                    async for chunk in agent_stream.stream_text(delta=True):
                        yield chunk

    _message_history += agent_run.result.new_messages()


async def chat_once(user_message: str) -> str:
    """Non-streaming version for internal use / testing."""
    global _message_history
    deps = LanskyDeps(db_path=config.SQLITE_DB_PATH)
    result = await agent.run(
        user_message,
        deps=deps,
        message_history=_message_history,
    )
    _message_history += result.new_messages()
    return result.output
