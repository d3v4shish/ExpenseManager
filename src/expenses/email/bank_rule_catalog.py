from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path
from typing import Any


class BankRuleCatalog:
    """Load, merge, validate, and persist bank email rule payloads."""

    def __init__(self, default_rules_path: Path, override_rules_path: Path) -> None:
        self.default_rules_path = Path(default_rules_path)
        self.override_rules_path = Path(override_rules_path)

    def load_defaults(self) -> dict[str, Any]:
        """Return the shipped read-only bank-rule payload."""

        return validate_bank_rule_payload(self._load_json(self.default_rules_path), source_label=str(self.default_rules_path))

    def load_overrides(self) -> dict[str, Any]:
        """Return the user-local override payload, or one empty override set."""

        if not self.override_rules_path.exists():
            return {"banks": []}
        payload = self._load_json(self.override_rules_path)
        return validate_bank_rule_payload(payload, source_label=str(self.override_rules_path))

    def load_effective(self, overrides_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the effective rule payload after applying local overrides."""

        defaults = self.load_defaults()
        overrides = validate_bank_rule_payload(
            overrides_payload if overrides_payload is not None else self.load_overrides(),
            source_label="bank rule overrides",
        )
        return merge_bank_rule_payloads(defaults, overrides)

    def save_overrides(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist one validated user-local override payload atomically."""

        normalized = validate_bank_rule_payload(payload, source_label="bank rule overrides")
        self.override_rules_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.override_rules_path.parent / f"{self.override_rules_path.name}.tmp.{uuid.uuid4().hex}"
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(normalized, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self.override_rules_path)
        return normalized

    def parse_override_text(self, raw_text: str) -> dict[str, Any]:
        """Parse and validate one raw override JSON string."""

        text = str(raw_text or "").strip()
        if not text:
            return {"banks": []}
        payload = json.loads(text)
        return validate_bank_rule_payload(payload, source_label="bank rule overrides")

    def payload_text(self, payload: dict[str, Any]) -> str:
        """Return one stable text rendering used by the debug editor."""

        return json.dumps(payload, indent=2) + "\n"

    def _load_json(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError(f"Bank rule payload must be one object: {path}")
        return payload


def validate_bank_rule_payload(payload: dict[str, Any], *, source_label: str) -> dict[str, Any]:
    """Return one validated bank-rule payload while preserving unknown fields."""

    if not isinstance(payload, dict):
        raise ValueError(f"{source_label} must be one JSON object.")
    result = copy.deepcopy(payload)
    banks = result.get("banks", [])
    if banks in (None, ""):
        banks = []
    if not isinstance(banks, list):
        raise ValueError(f"{source_label} must contain a 'banks' list.")
    seen_ids: set[str] = set()
    normalized_banks: list[dict[str, Any]] = []
    for index, item in enumerate(banks):
        if not isinstance(item, dict):
            raise ValueError(f"{source_label} banks[{index}] must be one object.")
        rule_id = str(item.get("id", "")).strip()
        if not rule_id:
            raise ValueError(f"{source_label} banks[{index}] is missing a non-empty 'id'.")
        if rule_id in seen_ids:
            raise ValueError(f"{source_label} contains duplicate bank rule id '{rule_id}'.")
        seen_ids.add(rule_id)
        normalized_banks.append(copy.deepcopy(item))
    result["banks"] = normalized_banks
    return result


def merge_bank_rule_payloads(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge the shipped defaults with one user-local override payload."""

    merged = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if key == "banks":
            continue
        merged[key] = copy.deepcopy(value)

    default_banks = defaults.get("banks", [])
    override_banks = overrides.get("banks", [])
    override_by_id = {
        str(item.get("id", "")).strip(): copy.deepcopy(item)
        for item in override_banks
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    merged_banks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in default_banks:
        rule_id = str(item.get("id", "")).strip()
        if not rule_id:
            continue
        merged_banks.append(copy.deepcopy(override_by_id.get(rule_id, item)))
        seen_ids.add(rule_id)
    for item in override_banks:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("id", "")).strip()
        if not rule_id or rule_id in seen_ids:
            continue
        merged_banks.append(copy.deepcopy(item))
        seen_ids.add(rule_id)
    merged["banks"] = merged_banks
    return merged


def build_bank_rule_template(*, existing_ids: set[str] | None = None) -> dict[str, Any]:
    """Return one starter declarative bank rule with regex placeholders."""

    existing = {str(item).strip() for item in (existing_ids or set()) if str(item).strip()}
    rule_id = "new_bank"
    suffix = 2
    while rule_id in existing:
        rule_id = f"new_bank_{suffix}"
        suffix += 1
    return {
        "id": rule_id,
        "name": "New Bank",
        "match": {
            "fromContains": [
                "alerts@example-bank.com",
            ],
            "subjectContains": [
                "debit alert",
                "credit alert",
            ],
        },
        "extractor": {
            "type": "declarative",
        },
        "defaults": {
            "bankName": "New Bank",
        },
        "fields": {
            "directionRules": [
                {
                    "type": "debit",
                    "patterns": [
                        "debited",
                        "debit",
                    ],
                },
                {
                    "type": "credit",
                    "patterns": [
                        "credited",
                        "credit",
                    ],
                },
            ],
            "amountPatterns": [
                "inr\\s*([0-9,]+(?:\\.[0-9]{2})?)",
            ],
            "transactionIdPatterns": [
                "transaction(?:\\s+id|\\s+ref(?:erence)?)?\\s*[:\\-]?\\s*([0-9a-z\\-/]+)",
            ],
            "counterpartyPatterns": [
                "upi\\/[a-z0-9]+\\/[0-9a-z]+\\/([a-z0-9 .&_-]+)",
                "paid to\\s+([a-z0-9 .&_-]+)",
                "from\\s+([a-z0-9 .&_-]+)",
            ],
            "accountSuffixPatterns": [
                "account\\s+number\\s*[:\\-]?\\s*x+([0-9]{4,})",
                "a\\/c(?:ount)?(?:\\s+no\\.)?\\s*x+([0-9]{4,})",
            ],
            "dateTimePatterns": [
                "(\\d{2}[-/]\\d{2}[-/]\\d{2,4})\\s+(\\d{2}:\\d{2}:\\d{2})",
                "on\\s+(\\d{2}[-/]\\d{2}[-/]\\d{2,4})\\s+(?:at\\s+)?(\\d{2}:\\d{2}:\\d{2})",
            ],
        },
    }
