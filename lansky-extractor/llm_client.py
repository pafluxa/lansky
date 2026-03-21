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
- amount: integer. For CLP: remove $ and dots ("$382.738" → 382738). For USD: remove "USD", replace comma with dot, multiply by 100 ("USD 10,03" → 1003). For EUR: same as USD.
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


def extract(email_text: str) -> EmailExtractionResult | None:
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
            result = EmailExtractionResult.model_validate(parsed)
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