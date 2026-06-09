from __future__ import annotations

import re
from typing import Any

from src.expenses.email.extractors.declarative_extractor import DeclarativeBankExtractor
from src.expenses.email.mail_types import SourceRecord


class SbiBankExtractor(DeclarativeBankExtractor):
    extractor_id = "sbi"

    def extract(self, record: SourceRecord, bank_rule: dict[str, Any]) -> dict[str, Any] | None:
        payload = super().extract(record, bank_rule)
        if payload is None:
            return None
        search_text = str(payload.get("searchText", ""))
        if not payload.get("counterparty"):
            for pattern in (
                r"transferred to\s+(?:mr\.?\s+|ms\.?\s+|mrs\.?\s+)?([a-z0-9 .&_-]+?)(?:\.\s+avl\s+bal|\.|$)",
                r"transfer from\s+([a-z0-9 .&_-]+?)(?:\.\s+avl\s+bal|\.|$)",
            ):
                match = re.search(pattern, search_text, re.IGNORECASE)
                if match:
                    payload["counterparty"] = self.normalize_counterparty(match.group(1))
                    break
        payload["confidence"] = self.confidence(
            str(payload.get("direction", "")),
            payload.get("amount"),
            str(payload.get("counterparty", "")),
            str(payload.get("transactionId", "")),
        )
        return payload
