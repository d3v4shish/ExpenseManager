from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.expenses.email.extractors.declarative_extractor import DeclarativeBankExtractor


class AxisBankExtractor(DeclarativeBankExtractor):
    extractor_id = "axis"

    def extract(self, record, bank_rule: dict[str, Any]) -> dict[str, Any] | None:
        payload = super().extract(record, bank_rule)
        if payload is None:
            return None
        search_text = str(payload.get("searchText", ""))

        direction        = self._axis_direction(record.title, search_text)
        amount           = self._axis_amount(record.title, search_text)
        timestamp_parts  = self._axis_timestamp(record.received_at, search_text)
        transaction_info = self._axis_transaction_info(search_text)
        account_suffix   = self._axis_account_suffix(record.title, search_text)
        transaction_id   = self._axis_transaction_id(transaction_info)
        counterparty     = self._axis_counterparty(transaction_info)

        if direction:
            payload["direction"] = direction
        if amount is not None:
            payload["amount"] = amount
        if timestamp_parts is not None:
            payload["timestamp"] = timestamp_parts["timestamp"]
            payload["date"] = timestamp_parts["date"]
            payload["time"] = timestamp_parts["time"]
            payload["month"] = timestamp_parts["month"]
            payload["year"] = timestamp_parts["year"]
        if transaction_info:
            payload["transactionInfo"] = transaction_info
        payload["transactionId"] = transaction_id
        if counterparty:
            payload["counterparty"] = counterparty
        if account_suffix:
            payload["accountSuffix"] = account_suffix

        final_direction = str(payload.get("direction", ""))
        final_amount = payload.get("amount")
        final_counterparty = str(payload.get("counterparty", ""))
        final_transaction_id = str(payload.get("transactionId", ""))
        final_account_suffix = str(payload.get("accountSuffix", ""))
        payload["hasTransactionEvidence"] = self.looks_like_real_transaction(final_direction, final_amount, search_text)
        payload["hasRequiredFields"] = self.has_required_transaction_fields(
            final_direction,
            final_amount,
            final_account_suffix,
        )
        payload["confidence"] = self.confidence(final_direction, final_amount, final_counterparty, final_transaction_id)
        return payload

    def _axis_direction(self, title: str, text: str) -> str:
        title_lower = str(title or "").lower()
        if (
            "amount debited:" in text
            or "has been debited" in text
            or "was debited" in title_lower
            or "debit notification from axis bank" in title_lower
            or "debit transaction alert" in title_lower
            or "debited from your a/c" in title_lower
        ):
            return "debit"
        if (
            "amount credited:" in text
            or "has been credited" in text
            or "was credited" in title_lower
            or "credit notification from axis bank" in title_lower
            or "credit transaction alert" in title_lower
            or "credited to your a/c" in title_lower
        ):
            return "credit"
        return ""

    def _axis_amount(self, title: str, text: str) -> float | None:
        for pattern in (
            r"amount debited:\s*inr\s*([0-9,]+(?:\.[0-9]{2})?)",
            r"amount credited:\s*inr\s*([0-9,]+(?:\.[0-9]{2})?)",
            r"inr\s*([0-9,]+(?:\.[0-9]{2})?)\s*was\s+debited",
            r"inr\s*([0-9,]+(?:\.[0-9]{2})?)\s*was\s+credited",
            r"debited\s+with\s+inr\s*([0-9,]+(?:\.[0-9]{2})?)",
            r"credited\s+with\s+inr\s*([0-9,]+(?:\.[0-9]{2})?)",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1).replace(",", ""))
                except ValueError:
                    return None
        subject_text = str(title or "")
        match = re.search(r"inr\s*([0-9,]+(?:\.[0-9]{2})?)", subject_text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                return None
        return None

    def _axis_timestamp(self, received_at: str, text: str) -> dict[str, Any] | None:
        fallback = datetime.fromisoformat(received_at)
        for pattern in (
            r"date\s*(?:&|and)\s*time:\s*(\d{2}-\d{2}-\d{2,4}),\s*(\d{2}:\d{2}:\d{2})\s*ist",
            r"on\s+(\d{2}-\d{2}-\d{2,4})\s+at\s+(\d{2}:\d{2}:\d{2})\s*ist",
            r"on\s+(\d{2}-\d{2}-\d{2,4})\s+(\d{2}:\d{2}:\d{2})\s*ist",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match is None:
                continue
            date_value = match.group(1).strip()
            time_value = match.group(2).strip()
            parsed     = self.parse_date_time_groups([date_value, time_value], fallback)
            if parsed is None:
                continue
            return {
                "timestamp": parsed.isoformat(),
                "date": parsed.date().isoformat(),
                "time": parsed.time().isoformat(),
                "month": parsed.month,
                "year": parsed.year,
            }
        return None

    def _axis_transaction_info(self, text: str) -> str:
        match = re.search(
            r"(?:transaction info:\s*|info[-:\s]+)(upi/p2[am]/[0-9a-z]+/[a-z0-9 .&_-]+)",
            text,
            re.IGNORECASE,
        )
        if match is None:
            return ""
        return re.sub(r"\s+", " ", match.group(1)).strip(" .:-")

    def _axis_transaction_id(self, transaction_info: str) -> str:
        match = re.search(r"\bupi/p2[am]/([0-9]{6,})/", transaction_info, re.IGNORECASE)
        if match is not None:
            return match.group(1).strip()
        return ""

    def _axis_counterparty(self, transaction_info: str) -> str:
        match = re.search(r"\bupi/p2[am]/[0-9]{6,}/([a-z0-9 .&_-]+)", transaction_info, re.IGNORECASE)
        if match is None:
            return ""
        return self.normalize_counterparty(match.group(1))

    def _axis_account_suffix(self, title: str, text: str) -> str:
        match = re.search(r"account number:\s*x+([0-9]{4,})", text, re.IGNORECASE)
        if match is None:
            match = re.search(r"a/c no\.\s*x+([0-9]{4,})", text, re.IGNORECASE)
        if match is None:
            match = re.search(r"from a/c no\.\s*x+([0-9]{4,})", text, re.IGNORECASE)
        if match is None:
            match = re.search(r"a/c no\.\s*x+([0-9]{4,})", str(title or ""), re.IGNORECASE)
        if match is None:
            return ""
        return self.normalize_account_suffix(match.group(1))
