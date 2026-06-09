from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

EXTRACTOR_ID = "axis_credit_card_spend"
ORDER = 50
RUN_ON_PARSED = False

MATCH = {
    "from_contains": ["alerts@axis.bank.in"],
    "subject_contains": ["spent on credit card"],
}

IST = timezone(timedelta(hours=5, minutes=30))


def parse_email(email):
    subject = str(email.get("subject", "") or email.get("title", ""))
    body = _plain_text(
        "\n".join(
            [
                str(email.get("text_body", "")),
                str(email.get("html_body", "")),
            ]
        )
    )
    text = f"{subject}\n{body}"

    amount = _amount(text)
    account_suffix = _account_suffix(text)
    if amount is None or not account_suffix:
        return []

    timestamp = _timestamp(text) or str(email.get("received_at", ""))
    return [
        {
            "bank_name": "Axis Bank",
            "direction": "debit",
            "amount": amount,
            "currency": "INR",
            "account_suffix": account_suffix,
            "counterparty": _merchant(text),
            "timestamp": timestamp,
            "transaction_id": "",
            "category": "Credit card",
            "confidence": 0.9,
        }
    ]


def _plain_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<(script|style|head|title)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _amount(text: str) -> float | None:
    patterns = (
        r"transaction amount:\s*inr\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        r"inr\s*([0-9,]+(?:\.[0-9]{1,2})?)\s+spent\s+on\s+credit\s+card",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match is None:
            continue
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _merchant(text: str) -> str:
    match = re.search(
        r"merchant name:\s*([a-z0-9 .&_-]+?)(?:\s+(?:axis bank credit card no\.|credit card no\.|date\s*(?:&|and)\s*time:|available limit|total credit limit)|$)",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip(" .:-").upper()


def _account_suffix(text: str) -> str:
    match = re.search(r"credit card no\.\s*x+([0-9]{4,})", text, re.IGNORECASE)
    if match is None:
        return ""
    digits = re.sub(r"[^0-9]", "", match.group(1))
    return digits[-4:]


def _timestamp(text: str) -> str:
    match = re.search(
        r"date\s*(?:&|and)\s*time:\s*(\d{2}-\d{2}-\d{2,4}),\s*(\d{2}:\d{2}:\d{2})\s*ist",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return ""
    date_value = match.group(1)
    time_value = match.group(2)
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%y %H:%M:%S"):
        try:
            return datetime.strptime(f"{date_value} {time_value}", fmt).replace(tzinfo=IST).isoformat()
        except ValueError:
            continue
    return ""
