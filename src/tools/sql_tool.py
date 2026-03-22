import sqlite3
import uuid
from datetime import date as _date
from pathlib import Path
from typing import Any

import jellyfish

from src import config


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def insert_transaction(
    id: str,
    direction: str,
    from_: str,
    to: str,
    date: str,
    time: str,
    amount: int,
    currency: str,
    source_type: str = "manual",
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO transactions (id, direction, "from", "to", date, time, amount, currency, source_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (id, direction, from_, to, date, time, amount, currency, source_type),
        )
        conn.commit()


def update_description(id: str, description: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE transactions
            SET description = ?, has_description = 1
            WHERE id = ?
            """,
            (description[:128], id),
        )
        conn.commit()


def fetch_uncategorized() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, direction, "from", "to", date, time, amount, currency, source_type
            FROM transactions
            WHERE has_description = 0
            ORDER BY date DESC, time DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_all() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, direction, "from", "to", date, time, amount, currency,
                   source_type, has_description, description
            FROM transactions
            ORDER BY date DESC, time DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def find_potential_duplicates(
    date: str,
    amount: int,
    merchant: str,
    direction: str,
) -> list[dict[str, Any]]:
    """
    Return existing transactions with the same date and amount whose merchant
    name has Jaro-Winkler similarity >= 0.85 against the given merchant.
    merchant = "to" for direction='out', "from" for direction='in'.
    """
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, direction, "from", "to", date, time, amount, currency,
                   has_description, description
            FROM transactions
            WHERE date = ? AND amount = ?
            """,
            (date, amount),
        ).fetchall()
    results = []
    for r in rows:
        existing_merchant = r["to"] if direction == "out" else r["from"]
        similarity = jellyfish.jaro_winkler_similarity(
            merchant.upper(), existing_merchant.upper()
        )
        if similarity >= 0.85:
            results.append(dict(r))
    return results


def execute_read_query(sql: str) -> list[dict[str, Any]]:
    """Run an arbitrary read-only SELECT against the transactions table."""
    sql_stripped = sql.strip().upper()
    if not sql_stripped.startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed.")
    with _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def insert_instrument(
    id: str,
    type_: str,
    label: str,
    limit_clp: int | None,
    limit_usd: int | None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO instruments (id, type, label, limit_clp, limit_usd)
            VALUES (?, ?, ?, ?, ?)
            """,
            (id, type_, label, limit_clp, limit_usd),
        )
        conn.commit()


def fetch_instruments() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM instruments ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_instrument(instrument_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM instruments WHERE id = ?",
            (instrument_id,),
        ).fetchone()
    return dict(row) if row else None


def insert_debt_item(
    transaction_id: str,
    instrument_id: str,
    total_amount: int,
    currency: str,
    installments: int,
    installment_amt: int,
    purchase_date: str,
) -> str:
    item_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO debt_items
                (id, transaction_id, instrument_id, total_amount, currency,
                 installments, installment_amt, purchase_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, transaction_id, instrument_id, total_amount, currency,
             installments, installment_amt, purchase_date),
        )
        conn.commit()
    return item_id


def insert_payment(
    transaction_id: str,
    instrument_id: str,
    amount: int,
    currency: str,
    payment_date: str,
) -> str:
    payment_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO payments
                (id, transaction_id, instrument_id, amount, currency, payment_date)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (payment_id, transaction_id, instrument_id, amount, currency, payment_date),
        )
        conn.commit()
    return payment_id


def fetch_active_debt(instrument_id: str | None = None) -> list[dict[str, Any]]:
    today = _date.today()
    query = """
        SELECT d.*, i.label AS instrument_label
        FROM debt_items d
        JOIN instruments i ON i.id = d.instrument_id
    """
    params: list[Any] = []
    if instrument_id is not None:
        query += " WHERE d.instrument_id = ?"
        params.append(instrument_id)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        purchase_date = _date.fromisoformat(row["purchase_date"])
        months_elapsed = (
            (today.year - purchase_date.year) * 12
            + (today.month - purchase_date.month)
        )
        remaining = row["installments"] - months_elapsed
        if remaining > 0:
            row["remaining"] = remaining
            result.append(row)
    result.sort(key=lambda x: x["remaining"])
    return result


def fetch_payments_for_period(
    instrument_id: str,
    year: int,
    month: int,
) -> list[dict[str, Any]]:
    period_prefix = f"{year:04d}-{month:02d}%"
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM payments
            WHERE instrument_id = ? AND payment_date LIKE ?
            """,
            (instrument_id, period_prefix),
        ).fetchall()
    return [dict(r) for r in rows]


def compute_period_balance(
    instrument_id: str,
    year: int,
    month: int,
) -> dict[str, Any]:
    active = fetch_active_debt(instrument_id=instrument_id)
    period_debt = sum(row["installment_amt"] for row in active)
    currency = active[0]["currency"] if active else "CLP"
    payments = fetch_payments_for_period(instrument_id, year, month)
    period_payments = sum(p["amount"] for p in payments)
    return {
        "period_debt": period_debt,
        "period_payments": period_payments,
        "balance": period_debt - period_payments,
        "currency": currency,
    }


def compute_total_debt(instrument_id: str | None = None) -> list[dict]:
    if instrument_id is not None:
        instruments = [fetch_instrument(instrument_id)]
        instruments = [i for i in instruments if i is not None]
    else:
        instruments = fetch_instruments()
    result = []
    for inst in instruments:
        active = fetch_active_debt(instrument_id=inst["id"])
        total_outstanding = sum(
            row["remaining"] * row["installment_amt"] for row in active
        )
        currency = active[0]["currency"] if active else "CLP"
        result.append({
            "instrument_id": inst["id"],
            "instrument_label": inst["label"],
            "total_outstanding": total_outstanding,
            "currency": currency,
        })
    return result


def compute_available_credit(instrument_id: str) -> dict[str, Any] | None:
    inst = fetch_instrument(instrument_id)
    if inst is None:
        return None
    if inst["type"] != "credit_card" or inst["limit_clp"] is None:
        return None
    debt_rows = compute_total_debt(instrument_id=instrument_id)
    outstanding = debt_rows[0]["total_outstanding"] if debt_rows else 0
    limit = inst["limit_clp"]
    return {
        "instrument_id": instrument_id,
        "label": inst["label"],
        "limit": limit,
        "outstanding": outstanding,
        "available": limit - outstanding,
        "currency": "CLP",
    }
