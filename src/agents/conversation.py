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
import logging
from dataclasses import dataclass
from typing import AsyncIterator

from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.messages import ModelMessage

from src import config
from src.tools import sql_tool, graph_engine

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP tool call logger — logs code submitted to the executor and its output
# ---------------------------------------------------------------------------

async def _log_mcp_tool_call(ctx, call_tool, name: str, args: dict):
    if name == "execute_python":
        code = args.get("code", "")
        log.info("MCP CALL  tool=%s\n--- code ---\n%s\n--- end code ---", name, code)
    else:
        log.info("MCP CALL  tool=%s  args=%s", name, args)
    result = await call_tool(name, args)
    output = str(result)
    log.info("MCP RESULT  tool=%s\n--- output ---\n%s\n--- end output ---", name, output)
    return result


# MCP server instance — lifecycle managed by FastAPI lifespan in main.py
mcp_server = MCPServerStreamableHTTP(
    config.MCP_CODE_EXECUTOR_URL,
    process_tool_call=_log_mcp_tool_call,
    max_retries=3,
)


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
- You can help the user manage their financial instruments (credit cards, loans, mortgages) using the create_instrument tool. When the user mentions a card, loan, or mortgage, offer to register it.
- You can query active installment debt, period balances, total debt, and available credit using the debt tools.
- For currency conversion, the convert_currency tool exists but is not yet functional. Let the user know it's coming soon.
- When reporting debt amounts, always include the instrument label (not just the ID) and the currency.
- UF (Unidad de Fomento) is a Chilean inflation-indexed unit used for mortgages and some loans. 1 UF ≈ 38,000 CLP (varies daily).
"""


_model_settings = (
    {"thinking": {"type": "enabled", "budget_tokens": config.THINKING_BUDGET_TOKENS}}
    if config.ENABLE_THINKING
    else {}
)

if config.ENABLE_THINKING:
    log.info("Extended thinking enabled (budget=%d tokens)", config.THINKING_BUDGET_TOKENS)

agent = Agent(
    config.MODEL,
    deps_type=LanskyDeps,
    system_prompt=SYSTEM_PROMPT,
    toolsets=[mcp_server],
    model_settings=_model_settings,
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@agent.tool
async def get_uncategorized_transactions(ctx: RunContext[LanskyDeps]) -> str:
    """Return all transactions that have not yet been categorized by the user."""
    rows = sql_tool.fetch_uncategorized()
    if not rows:
        log.info("TOOL get_uncategorized_transactions → 0 pending")
        return "All transactions are categorized."
    lines = []
    for r in rows:
        merchant = r["to"] if r["direction"] == "out" else r["from"]
        lines.append(
            f"id={r['id']} | {r['direction'].upper()} | {merchant} | "
            f"{r['amount']:,} {r['currency']} | {r['date']}"
        )
    log.info("TOOL get_uncategorized_transactions → %d pending", len(rows))
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
    log.info("TOOL set_description  id=%s…  label=%r", transaction_id[:8], description)
    sql_tool.update_description(transaction_id, description)

    # Run graph engine to find auto-classifiable pending transactions
    pending = sql_tool.fetch_uncategorized()
    auto_labeled: list[str] = []

    if pending:
        import src.tools.graph_engine as ge
        nodes = ge._load_nodes()
        G = ge.build_graph(nodes)
        partitions = ge.detect_partitions(G, nodes)

        for tx in pending:
            result = ge.classify(tx, nodes=nodes, G=G, partitions=partitions)
            if result.label is not None:
                sql_tool.update_description(tx["id"], result.label)
                merchant = tx["to"] if tx["direction"] == "out" else tx["from"]
                log.info(
                    "GRAPH auto-classified  %s  %s → %r  (confidence %.2f)",
                    merchant, tx["date"], result.label, result.confidence,
                )
                auto_labeled.append(
                    f"Auto-classified '{merchant}' on {tx['date']} as '{result.label}' "
                    f"(confidence {result.confidence:.2f}/4.00). {result.explanation}"
                )

    if not auto_labeled:
        log.info("GRAPH no auto-classifications triggered")

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
    log.info("TOOL query_transactions\n--- sql ---\n%s\n--- end sql ---", sql_query)
    try:
        rows = sql_tool.execute_read_query(sql_query)
        log.info("TOOL query_transactions → %d row(s)", len(rows))
        return json.dumps(rows, ensure_ascii=False)
    except ValueError as e:
        log.warning("TOOL query_transactions  rejected: %s", e)
        return f"Error: {e}"
    except Exception as e:
        log.error("TOOL query_transactions  failed: %s", e)
        return f"Query failed: {e}"


@agent.tool
async def classify_transaction(ctx: RunContext[LanskyDeps], transaction_id: str) -> str:
    """
    Run the graph engine classifier on a specific transaction and return the
    result including which partition it maps to and the explainability breakdown.
    """
    log.info("TOOL classify_transaction  id=%s…", transaction_id[:8])
    all_rows = sql_tool.fetch_all()
    target = next((r for r in all_rows if r["id"] == transaction_id), None)
    if target is None:
        log.warning("TOOL classify_transaction  not found: %s", transaction_id[:8])
        return f"Transaction {transaction_id} not found."

    _, result = graph_engine.run(classify_tx=target)
    if result is None:
        return "Could not classify — not enough data."

    log.info(
        "TOOL classify_transaction → partition=%s  label=%r  confidence=%.3f",
        result.partition_id, result.label, result.confidence,
    )
    return (
        f"Partition: {result.partition_id} | Label: {result.label!r} | "
        f"Confidence: {result.confidence:.3f}/4.000\n"
        f"Dimension scores: {result.dim_scores}\n"
        f"Explanation: {result.explanation}"
    )


@agent.tool
async def query_active_debt(ctx: RunContext[LanskyDeps], instrument_id: str = "") -> str:
    """Return all active debt items (installment purchases and loans
    with remaining payments). Optionally filter by instrument_id.
    Shows remaining installments and monthly obligation."""
    filt = instrument_id if instrument_id else None
    rows = sql_tool.fetch_active_debt(instrument_id=filt)
    if not rows:
        return "No active debt items found."
    lines = []
    for r in rows:
        lines.append(
            f"instrument={r['instrument_label']} | "
            f"amount={r['total_amount']:,} {r['currency']} | "
            f"installments={r['installments']} | "
            f"monthly={r['installment_amt']:,} | "
            f"remaining={r['remaining']} | "
            f"purchase={r['purchase_date']}"
        )
    log.info("TOOL query_active_debt → %d active items", len(rows))
    return f"{len(rows)} active debt item(s):\n" + "\n".join(lines)


@agent.tool
async def query_period_balance(
    ctx: RunContext[LanskyDeps], instrument_id: str, year: int, month: int
) -> str:
    """Compute the balance for a specific instrument in a given month:
    total installments due minus total payments made. Positive balance
    means money still owed."""
    result = sql_tool.compute_period_balance(instrument_id, year, month)
    log.info(
        "TOOL query_period_balance  %s  %d-%02d → debt=%d pay=%d bal=%d",
        instrument_id, year, month,
        result["period_debt"], result["period_payments"], result["balance"],
    )
    return (
        f"Period {year}-{month:02d} for {instrument_id}:\n"
        f"  Installments due: {result['period_debt']:,} {result['currency']}\n"
        f"  Payments made:    {result['period_payments']:,} {result['currency']}\n"
        f"  Balance:          {result['balance']:,} {result['currency']}"
    )


@agent.tool
async def query_total_debt(ctx: RunContext[LanskyDeps], instrument_id: str = "") -> str:
    """Return total outstanding debt (remaining installments × monthly
    amount) across all instruments or a specific one."""
    filt = instrument_id if instrument_id else None
    rows = sql_tool.compute_total_debt(instrument_id=filt)
    if not rows:
        return "No outstanding debt."
    lines = []
    for r in rows:
        lines.append(
            f"{r['instrument_label']}: "
            f"{r['total_outstanding']:,} {r['currency']}"
        )
    total = sum(r["total_outstanding"] for r in rows)
    log.info("TOOL query_total_debt → %d instrument(s), total=%d", len(rows), total)
    return "\n".join(lines) + f"\n\nTotal: {total:,}"


@agent.tool
async def query_available_credit(ctx: RunContext[LanskyDeps], instrument_id: str) -> str:
    """Return available credit for a credit card: cupo minus
    total outstanding debt on that instrument."""
    result = sql_tool.compute_available_credit(instrument_id)
    if result is None:
        return f"Instrument {instrument_id} not found or has no credit limit."
    log.info(
        "TOOL query_available_credit  %s → limit=%d outstanding=%d available=%d",
        instrument_id, result["limit"], result["outstanding"], result["available"],
    )
    return (
        f"{result['label']}:\n"
        f"  Credit limit:  {result['limit']:,} {result['currency']}\n"
        f"  Outstanding:   {result['outstanding']:,} {result['currency']}\n"
        f"  Available:     {result['available']:,} {result['currency']}"
    )


@agent.tool
async def create_instrument(
    ctx: RunContext[LanskyDeps],
    id: str,
    type: str,
    label: str,
    limit_clp: int = 0,
    limit_usd: int = 0,
) -> str:
    """Create a new financial instrument (credit card, loan, or mortgage).
    Example: id='cc:2722', type='credit_card', label='BCI Visa 2722',
    limit_clp=3000000."""
    existing = sql_tool.fetch_instrument(id)
    if existing:
        return f"Instrument {id} already exists: {existing['label']}"
    sql_tool.insert_instrument(
        id=id, type_=type, label=label,
        limit_clp=limit_clp or None, limit_usd=limit_usd or None,
    )
    log.info("TOOL create_instrument  %s  %s  %s", id, type, label)
    return f"Created instrument: {id} ({label})"


@agent.tool
async def convert_currency(
    ctx: RunContext[LanskyDeps], amount: int, from_currency: str, to_currency: str
) -> str:
    """Convert between CLP, USD, EUR, and UF. Currently a placeholder
    — returns an explanation that the tool is not yet connected to
    live exchange rate data."""
    return (
        f"Currency conversion ({amount:,} {from_currency} → {to_currency}) "
        f"is not yet available. To implement this, Lansky needs:\n"
        f"• UF ↔ CLP: daily UF value from CMF API (api.cmfchile.cl)\n"
        f"• CLP ↔ USD/EUR: exchange rates from SII or a forex API\n"
        f"This tool is a placeholder for future implementation."
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
