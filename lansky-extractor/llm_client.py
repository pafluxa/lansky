import json
import logging
from pathlib import Path

import openai

from models import EmailExtractionResult

import config

log = logging.getLogger(__name__)

client = openai.OpenAI(
    base_url=config.LLM_BASE_URL,
    api_key="local",
)

SYSTEM_PROMPT = """You extract financial transactions from Chilean bank notification emails (BCI, Banco de Chile, and others).

Return ONLY a JSON object with this schema: {"transactions": [<transaction>, ...]}

Each transaction object MUST have these fields:
- category: one of "expense", "transfer", "card_payment", "debt_payment"
- date: YYYY-MM-DD format. Convert from DD/MM/YYYY or DD/MM/YY. Two-digit years: assume 20XX.
- time: HH:MM:SS format. If only HH:MM, append :00. If absent, use "00:00:00".
- amount_raw: string. Copy the amount EXACTLY as it appears in the email, including currency symbols, dots, commas, and prefixes. Examples: "$382.738", "USD 10,03", "$22.700", "USD 5,96". Do NOT convert, calculate, or remove any characters.
- currency: "CLP", "USD", or "EUR". $ with dots = CLP. "USD" prefix = USD. "EUR" prefix = EUR.

CATEGORY RULES — follow these strictly:
- "expense": the header says "compra", OR a "Comercio" field is present. These are purchases. Always "expense", even if paid with a credit card.
- "transfer": the header says "transferencia". These are bank transfers between accounts or to other people.
- "card_payment": the header says "pago de tu tarjeta" or "pago de tarjeta". These are payments FROM a checking account TO a credit card to pay down debt. They never have a "Comercio" field.
- "debt_payment": the header says "pago de crédito", "pago de préstamo", or similar loan payments.
- If "Comercio" is present, the category is ALWAYS "expense". No exceptions.

Category-specific fields (include when present, omit when absent):

expense: merchant (str from "Comercio"), card_last4 (str, digits only from "****XXXX"), commerce_type ("nacional" or "internacional" from header), installments (int from "Cuotas")

transfer: direction ("outgoing"), counterparty (str), source_account (str), destination_account (str), destination_bank (str), message (str or null)

card_payment: card_last4 (str from "Tarjeta de crédito"), source_account (str from "Cuenta de origen"), operation_number (str or null)

debt_payment: payee (str), creditor (str or null)

Rules:
- Most emails have one transaction. Return a list of one.
- Do NOT invent values. Omit missing fields.
- Card "****1234" → card_last4 = "1234"
- Cuotas: if "Cuotas" is 0 or absent, set installments to 1. Zero means single payment.
- Ignore footer/boilerplate."""


def _normalize_amounts(data: dict) -> dict:
    """Convert amount_raw (str) → amount (int) for each transaction.

    Chilean conventions:
      CLP: dots are thousands separators, no decimals.
        "$382.738" → 382738
        "$22.700"  → 22700
      USD/EUR: commas are decimal separators (2 decimal places),
        dots are optional thousands separators.
        Stored as cents (×100).
        "USD 10,03" → 1003
        "USD 5,96"  → 596
        "EUR 1.250,00" → 125000
    """
    for tx in data.get("transactions", []):
        raw = tx.pop("amount_raw", None)
        if raw is None:
            # Fallback: if LLM already returned "amount" as int, keep it
            continue

        currency = tx.get("currency", "CLP")

        # Strip currency prefix and whitespace
        cleaned = raw.strip()
        for prefix in ("USD", "EUR", "$", "CLP"):
            cleaned = cleaned.replace(prefix, "")
        cleaned = cleaned.strip()

        if currency == "CLP":
            # Dots are thousands separators, no decimals
            cleaned = cleaned.replace(".", "")
            cleaned = cleaned.replace(",", "")  # safety
            tx["amount"] = int(cleaned)
        else:
            # USD/EUR: dots are thousands, commas are decimals
            # "1.250,00" → "1250.00" → 1250.00 → 125000
            # "10,03" → "10.03" → 10.03 → 1003
            # "5,96" → "5.96" → 5.96 → 596
            cleaned = cleaned.replace(".", "")   # remove thousands dots
            cleaned = cleaned.replace(",", ".")  # comma → decimal point
            tx["amount"] = round(float(cleaned) * 100)

    return data


def extract(email_text: str, bank_name: str = "Bank") -> EmailExtractionResult | None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": email_text},
    ]
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=config.LLM_MODEL,
                temperature=0.0,
                max_tokens=1024,
                messages=messages,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            raw = response.choices[0].message.content or ""
            parsed = _parse_json(raw)
            normalized = _normalize_amounts(parsed)
            for tx in normalized.get("transactions", []):
                tx["bank_name"] = bank_name
            result = EmailExtractionResult.model_validate(normalized)
            log.info("Extracted %d transaction(s) from email", len(result.transactions))
            return result
        except Exception as exc:
            log.warning(
                "Extraction attempt %d failed: %s — raw response: %r",
                attempt + 1,
                exc,
                raw if "raw" in dir() else "<no response>",
            )
    return None


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])