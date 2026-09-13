from __future__ import annotations

import re
from typing import Any

from src.expenses.email.mail_types import ParsedFact, SourceRecord


class ImportedTransactionParser:
    """Materialize validated CSV and Axios archive rows without re-parsing text."""

    parser_id = "imported_transaction"

    def matches(self, record: SourceRecord) -> bool:
        return record.record_type in {"csv_transaction", "axios_transaction"}

    def diagnose(self, record: SourceRecord, *, rules_payload=None) -> dict[str, Any]:
        _ = rules_payload
        payload = record.payload.get("transaction", {})
        if not isinstance(payload, dict) or not bool(payload.get("valid", True)):
            return {
                "candidateMatched": record.record_type in {"csv_transaction", "axios_transaction"},
                "facts": [],
                "attempts": [self._attempt("failed", "invalid_row", str(payload.get("error", "Import row is invalid.")))] if record.record_type == "csv_transaction" else [],
            }
        normalized = _normalized_transaction(payload)
        if normalized is None:
            return {"candidateMatched": True, "facts": [], "attempts": [self._attempt("failed", "missing_fields", "Imported transaction is missing amount, direction, or timestamp.")]}
        return {"candidateMatched": True, "facts": [self._fact(record, normalized)], "attempts": [self._attempt("parsed", "", "")]}

    def parse(self, record: SourceRecord) -> list[ParsedFact]:
        return list(self.diagnose(record).get("facts", []))

    def _fact(self, record: SourceRecord, payload: dict[str, Any]) -> ParsedFact:
        return ParsedFact(
            fact_type="bank_transaction",
            parser_id=self.parser_id,
            source_provider_id=record.provider_id,
            source_record_type=record.record_type,
            source_external_id=record.external_id,
            payload=payload,
            confidence=1.0,
        )

    def _attempt(self, status: str, reason_code: str, reason_text: str) -> dict[str, Any]:
        return {"parserId": self.parser_id, "matchedRuleId": "", "matchedRuleName": "", "parseStatus": status, "reasonCode": reason_code, "reasonText": reason_text, "factsCount": 1 if status == "parsed" else 0, "preview": {}}


class SmsBankAlertParser:
    """Conservative local SMS parser for imported Android backup messages."""

    parser_id = "bank_alert_sms"
    _amount = re.compile(r"(?i)(?:inr|rs\.?|₹)\s*([0-9][0-9,]*(?:\.\d{1,2})?)")
    _reference = re.compile(r"(?i)\b(?:upi\s*(?:ref(?:erence)?(?:\s*no\.?)?|id)|utr|rrn|txn(?:\s*(?:id|no\.?))?)\s*[:#-]?\s*([a-z0-9-]{5,})")
    _account = re.compile(r"(?i)(?:a/c|acct|account|card)\s*(?:no\.?)?\s*[x*]+\s*([0-9]{3,6})")
    _merchant = re.compile(r"(?i)\b(?:to|at|merchant)\s+(.+?)(?:\s+(?:on|ref|upi|txn|for)\b|[.;]|$)")

    @staticmethod
    def _bank_name(sender: str) -> str:
        """Normalize common Android sender IDs to the bank labels used by other imports."""

        value = str(sender or "").strip()
        normalized = re.sub(r"[^a-z0-9]", "", value.lower())
        aliases = {
            "axis": "Axis",
            "axisbk": "Axis",
            "sbi": "SBI",
            "sbiinb": "SBI",
            "hdfcbk": "HDFC",
            "icicib": "ICICI",
        }
        return aliases.get(normalized, value or "SMS")

    def matches(self, record: SourceRecord) -> bool:
        return record.record_type == "sms"

    def diagnose(self, record: SourceRecord, *, rules_payload=None) -> dict[str, Any]:
        _ = rules_payload
        if record.record_type != "sms":
            return {"candidateMatched": False, "facts": [], "attempts": []}
        body = str(record.payload.get("body", "")).strip()
        lowered = body.lower()
        direction = "debit" if any(token in lowered for token in ("debited", "spent", "paid", "withdrawn")) else "credit" if any(token in lowered for token in ("credited", "received", "refund")) else ""
        amount_match = self._amount.search(body)
        if not direction or amount_match is None:
            return {"candidateMatched": True, "facts": [], "attempts": [self._attempt("failed", "missing_fields", "SMS did not contain a recognizable direction and INR amount.", body)]}
        amount = float(amount_match.group(1).replace(",", ""))
        reference = self._reference.search(body)
        account = self._account.search(body)
        merchant = self._merchant.search(body)
        payload = {
            "bankName": self._bank_name(record.sender),
            "accountSuffix": account.group(1) if account else "",
            "direction": direction,
            "amount": amount,
            "currency": "INR",
            "transactionId": reference.group(1) if reference else "",
            "counterparty": merchant.group(1).strip() if merchant else "",
            "timestamp": record.received_at,
        }
        fact = ParsedFact("bank_transaction", self.parser_id, record.provider_id, record.record_type, record.external_id, payload, 0.72)
        return {"candidateMatched": True, "facts": [fact], "attempts": [self._attempt("parsed", "", "", body)]}

    def parse(self, record: SourceRecord) -> list[ParsedFact]:
        return list(self.diagnose(record).get("facts", []))

    def _attempt(self, status: str, reason_code: str, reason_text: str, body: str) -> dict[str, Any]:
        return {"parserId": self.parser_id, "matchedRuleId": "", "matchedRuleName": "", "parseStatus": status, "reasonCode": reason_code, "reasonText": reason_text, "factsCount": 1 if status == "parsed" else 0, "preview": {"bodySnippet": body[:240]}}


def _normalized_transaction(payload: dict[str, Any]) -> dict[str, Any] | None:
    direction = str(payload.get("direction", "")).strip().lower()
    try:
        amount = abs(float(payload.get("amount")))
    except (TypeError, ValueError):
        return None
    timestamp = str(payload.get("timestamp", "")).strip()
    if direction not in {"debit", "credit"} or not timestamp:
        return None
    return {
        "bankName": str(payload.get("bankName", "")).strip(),
        "accountSuffix": str(payload.get("accountSuffix", "")).strip(),
        "direction": direction,
        "amount": amount,
        "currency": str(payload.get("currency", "INR")).strip() or "INR",
        "transactionId": str(payload.get("transactionId", "")).strip(),
        "counterparty": str(payload.get("counterparty", payload.get("canonicalVendor", ""))).strip(),
        "timestamp": timestamp,
    }
