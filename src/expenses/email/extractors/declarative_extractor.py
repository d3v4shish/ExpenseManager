from __future__ import annotations

from typing import Any

from src.expenses.email.extractors.base import BankExtractionHelpers
from src.expenses.email.mail_types import SourceRecord


class DeclarativeBankExtractor(BankExtractionHelpers):
    extractor_id = "declarative"

    def extract(self, record: SourceRecord, bank_rule: dict[str, Any]) -> dict[str, Any] | None:
        fields          = bank_rule.get("fields", {})
        defaults        = bank_rule.get("defaults", {}) if isinstance(bank_rule.get("defaults", {}), dict) else {}
        normalized_text = self.build_search_text(record)
        direction       = self.extract_direction(normalized_text, fields)
        amount          = self.extract_amount(normalized_text, fields)
        transaction_id  = self.extract_first(normalized_text, fields.get("transactionIdPatterns", []))
        counterparty    = self.normalize_counterparty(self.extract_first(normalized_text, fields.get("counterpartyPatterns", [])))
        account_suffix  = self.normalize_account_suffix(self.extract_first(normalized_text, fields.get("accountSuffixPatterns", [])))
        account_suffix  = account_suffix or self.normalize_account_suffix(str(defaults.get("accountSuffix", "")))
        timestamp       = self.extract_timestamp(record, normalized_text, fields)
        bank_name       = str(defaults.get("bankName", bank_rule.get("name", bank_rule.get("id", "Unknown bank")))).strip() or "Unknown bank"
        return {
            "bankId": str(bank_rule.get("id", "")).strip(),
            "bankName": bank_name,
            "direction": direction,
            "amount": amount,
            "currency": "INR",
            "transactionId": transaction_id,
            "counterparty": counterparty,
            "accountSuffix": account_suffix,
            "timestamp": timestamp.isoformat(),
            "date": timestamp.date().isoformat(),
            "time": timestamp.time().isoformat(),
            "month": timestamp.month,
            "year": timestamp.year,
            "searchText": normalized_text,
            "hasTransactionEvidence": self.looks_like_real_transaction(direction, amount, normalized_text),
            "hasRequiredFields": self.has_required_transaction_fields(direction, amount, account_suffix),
            "confidence": self.confidence(direction, amount, counterparty, transaction_id),
        }
