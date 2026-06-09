from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Protocol

from src.expenses.email.mail_types import SourceRecord


class BankExtractionHelpers:
    def build_search_text(self, record: SourceRecord) -> str:
        text_body = self._normalize_text(record.payload.get("textBody", ""))
        html_body = self.strip_html(str(record.payload.get("htmlBody", "")))
        combined  = "\n".join(
            part
            for part in [
                self._normalize_text(record.title),
                self._normalize_text(record.sender),
                text_body,
                html_body,
            ]
            if part
        )
        combined = re.sub(r"\s+", " ", combined)
        return combined.strip().lower()

    def extract_direction(self, text: str, fields: dict[str, Any]) -> str:
        for rule in fields.get("directionRules", []):
            direction_type = str(rule.get("type", "")).strip().lower()
            patterns       = [str(item) for item in rule.get("patterns", [])]
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return direction_type
        return ""

    def extract_amount(self, text: str, fields: dict[str, Any]) -> float | None:
        match_value = self.extract_first(text, fields.get("amountPatterns", []))
        if not match_value:
            return None
        try:
            return float(match_value.replace(",", ""))
        except ValueError:
            return None

    def extract_timestamp(self, record: SourceRecord, text: str, fields: dict[str, Any]) -> datetime:
        fallback = datetime.fromisoformat(record.received_at)
        for pattern in fields.get("dateTimePatterns", []):
            match = re.search(str(pattern), text, re.IGNORECASE)
            if match is None:
                continue
            groups = [item for item in match.groups() if item]
            if not groups:
                continue
            parsed = self.parse_date_time_groups(groups, fallback)
            if parsed is not None:
                return parsed
        return fallback

    def parse_date_time_groups(self, groups: list[str], fallback: datetime) -> datetime | None:
        if len(groups) >= 2:
            combined = f"{groups[0]} {groups[1]}".strip()
            for fmt in ("%d/%m/%y %H:%M:%S", "%d-%m-%y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%y %H:%M"):
                try:
                    parsed = datetime.strptime(combined, fmt)
                    return parsed.replace(tzinfo=fallback.tzinfo)
                except ValueError:
                    continue
        for fmt in ("%d/%m/%y", "%d-%m-%y", "%d-%m-%Y", "%d-%b-%Y", "%d-%b-%y"):
            try:
                parsed = datetime.strptime(groups[0], fmt)
                return parsed.replace(
                    hour        = fallback.hour,
                    minute      = fallback.minute,
                    second      = fallback.second,
                    microsecond = 0,
                    tzinfo      = fallback.tzinfo,
                )
            except ValueError:
                continue
        return None

    def extract_first(self, text: str, patterns: list[Any]) -> str:
        for pattern in patterns:
            match = re.search(str(pattern), text, re.IGNORECASE)
            if match is None:
                continue
            value = match.group(1).strip(" .:-")
            if value:
                return re.sub(r"\s+", " ", value).strip()
        return ""

    def normalize_counterparty(self, value: str) -> str:
        if not value:
            return ""
        cleaned = re.sub(
            r"\bif this transaction was not initiated by you\b.*$",
            "",
            value,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\bfor any concerns regarding this transaction\b.*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        return re.sub(r"\s+", " ", cleaned).strip(" .:-").title()

    def normalize_account_suffix(self, value: str) -> str:
        value = re.sub(r"[^0-9]", "", value)
        return value[-4:] if value else ""

    def strip_html(self, value: str) -> str:
        if not value:
            return ""
        text = self._normalize_text(value)
        text = re.sub(r"(?is)<!--.*?-->", " ", text)
        text = re.sub(r"(?is)<(script|style|head|title)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|tr|table|li|ul|ol|td|th|h[1-6])>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        return self._normalize_text(text)

    def looks_like_real_transaction(self, direction: str, amount: float | None, text: str) -> bool:
        if direction and amount is not None:
            return True
        transaction_keywords = (
            "transaction info",
            "upi/p2a/",
            "transferred to",
            "transfer from",
            "credited inr",
            "debited inr",
            "amount debited",
            "amount credited",
        )
        if amount is not None and any(keyword in text for keyword in transaction_keywords):
            return True
        return False

    def has_required_transaction_fields(self, direction: str, amount: float | None, account_suffix: str) -> bool:
        return bool(direction and amount is not None and account_suffix)

    def confidence(self, direction: str, amount: float | None, counterparty: str, transaction_id: str) -> float:
        confidence = 0.6
        if direction:
            confidence += 0.1
        if amount is not None:
            confidence += 0.15
        if counterparty:
            confidence += 0.1
        if transaction_id:
            confidence += 0.05
        return min(confidence, 0.98)

    def _normalize_text(self, value: Any) -> str:
        text = self._decode_html_entities(str(value or ""))
        text = text.replace("\xa0", " ")
        text = text.replace("=\r\n", "")
        text = text.replace("=\n", "")
        text = self._collapse_crlf_runs(text)
        return text

    def _decode_html_entities(self, value: str, *, max_passes: int = 12) -> str:
        text = str(value or "")
        for _ in range(max_passes):
            decoded = html.unescape(text)
            if decoded == text:
                break
            text = decoded
        return text

    def _collapse_crlf_runs(self, value: str) -> str:
        text = str(value or "")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{2,}", "\n", text)
        return text.replace("\n", "\r\n")


class BankExtractor(Protocol):
    extractor_id: str

    def extract(self, record: SourceRecord, bank_rule: dict[str, Any]) -> dict[str, Any] | None:
        ...
