import sqlite3
from pathlib import Path
from typing import Any

import jellyfish

from src import config


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
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
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO transactions (id, direction, "from", "to", date, time, amount, currency)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (id, direction, from_, to, date, time, amount, currency),
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
            SELECT id, direction, "from", "to", date, time, amount, currency
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
                   has_description, description
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
    merchant_col = '"to"' if direction == "out" else '"from"'
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
