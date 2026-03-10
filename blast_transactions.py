"""
Read email_extractions_*.json and POST the first 10 valid transactions to Lansky.

Usage:
    python blast_transactions.py [--url http://localhost:8000] [--limit 10]
"""

import argparse
import glob
import json
import urllib.request
import urllib.error

LANSKY_URL = "http://localhost:8000"


def to_transaction(record: dict) -> dict | None:
    """Map a single extraction record to a Lansky TransactionRequest dict.
    Returns None if the record should be skipped."""
    ext = record.get("extraction")
    if not ext:
        return None

    category = ext.get("category")
    data = ext.get(category)
    if not data:
        return None

    date = data.get("date")
    time = data.get("time")
    amount = data.get("amount")
    currency = data.get("currency", "CLP")

    # All fields required
    if not date or not time or amount is None or currency not in ("CLP", "USD", "EUR"):
        return None

    amount = int(amount)
    if amount <= 0:
        return None

    if category == "expense":
        from_ = data.get("account") or "unknown"
        to = data.get("merchant") or "unknown"
        direction = "out"

    elif category == "transfer":
        if data.get("direction") == "outgoing":
            direction = "out"
            from_ = f"Banco de Chile {data.get('source_account') or ''}".strip()
            to = data.get("counterparty") or "unknown"
        else:
            direction = "in"
            from_ = data.get("counterparty") or "unknown"
            to = f"Banco de Chile {data.get('destination_account') or ''}".strip()

    elif category == "billing":
        direction = "out"
        from_ = "Banco de Chile"
        to = data.get("payee") or "unknown"

    elif category == "debt_payment":
        direction = "out"
        from_ = "Banco de Chile"
        to = data.get("payee") or data.get("creditor") or "unknown"

    else:
        return None

    return {
        "direction": direction,
        "from": from_,
        "to": to,
        "date": date,
        "time": time,
        "amount": amount,
        "currency": currency,
    }


def post(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url}/api/transactions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=LANSKY_URL)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    files = sorted(glob.glob("email_extractions_*.json"), reverse=True)
    if not files:
        print("No email_extractions_*.json file found.")
        return
    source = files[0]
    print(f"Reading: {source}")

    with open(source) as f:
        records = json.load(f)

    sent = 0
    skipped = 0
    for record in records:
        if sent >= args.limit:
            break
        tx = to_transaction(record)
        if tx is None:
            skipped += 1
            continue
        try:
            resp = post(args.url, tx)
            print(f"  [{sent+1:02d}] {tx['direction'].upper():3s} {tx['amount']:>12,} {tx['currency']} "
                  f"  {tx['to'][:30]:<30s}  {tx['date']}  → id={resp['id'][:8]}…")
            sent += 1
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"  ERROR {e.code}: {body[:120]}")
            skipped += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            skipped += 1

    print(f"\nDone. Sent: {sent}, Skipped (null/invalid): {skipped}")


if __name__ == "__main__":
    main()
