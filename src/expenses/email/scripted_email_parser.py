from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from src.expenses.email.mail_types import ParsedFact, SourceRecord


@dataclass(slots=True)
class _ScriptExtractor:
    script_id: str
    path: Path
    module: ModuleType
    order: int
    run_on_parsed: bool
    match: dict[str, list[str]]


class ScriptedEmailParser:
    """Run trusted user-owned Python extractor scripts over matching emails."""

    parser_id = "scripted_email"
    MATCH_KEYS = {
        "from_contains",
        "sender_contains",
        "subject_contains",
        "title_contains",
        "body_contains",
        "text_contains",
        "html_contains",
        "any_contains",
    }

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.enabled = False
        self.paths: list[Path] = []
        self.scripts: list[_ScriptExtractor] = []
        self.load_errors: list[dict[str, str]] = []
        self.skipped_scripts: list[dict[str, str]] = []
        self._load()

    def matches(self, record: SourceRecord) -> bool:
        return bool(self._matching_scripts(record, existing_facts=[]))

    def parse(self, record: SourceRecord) -> list[ParsedFact]:
        return list(self.diagnose(record).get("facts", []))

    def diagnose(
        self,
        record: SourceRecord,
        *,
        rules_payload: dict[str, Any] | None = None,
        existing_facts: list[ParsedFact] | None = None,
    ) -> dict[str, Any]:
        _ = rules_payload
        if record.record_type != "email" or not self.enabled or not self.scripts:
            return {"candidateMatched": False, "facts": [], "attempts": []}

        available_facts = list(existing_facts or [])
        facts: list[ParsedFact] = []
        attempts: list[dict[str, Any]] = []
        for script in self.scripts:
            if available_facts and not script.run_on_parsed:
                continue
            email_payload = self._email_payload(record, available_facts)
            try:
                matched = self._script_matches(script, record, email_payload)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Script extractor match failed path=%s error=%s", script.path, exc)
                continue
            if not matched:
                continue
            script_facts, error_text = self._run_script(script, record, email_payload)
            if script_facts:
                facts.extend(script_facts)
                available_facts.extend(script_facts)
                attempts.append(
                    self._attempt(
                        script,
                        status="parsed",
                        reason_code="",
                        reason_text=error_text,
                        facts_count=len(script_facts),
                    )
                )
                continue
            attempts.append(
                self._attempt(
                    script,
                    status="failed",
                    reason_code="script_no_transactions" if not error_text else "script_error",
                    reason_text=error_text or "The script matched this email but returned no valid transactions.",
                    facts_count=0,
                )
            )
        return {
            "candidateMatched": bool(attempts),
            "facts": facts,
            "attempts": attempts,
        }

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configPath": str(self.config_path),
            "configExists": self.config_path.exists(),
            "paths": [str(path) for path in self.paths],
            "loadedCount": len(self.scripts),
            "scripts": [
                {
                    "id": script.script_id,
                    "path": str(script.path),
                    "order": script.order,
                    "runOnParsed": script.run_on_parsed,
                }
                for script in self.scripts
            ],
            "loadErrors": list(self.load_errors),
            "skippedScripts": list(self.skipped_scripts),
        }

    def _load(self) -> None:
        config = self._load_config()
        self.enabled = bool(config.get("enabled"))
        self.paths = [Path(str(path)).expanduser() for path in config.get("paths", []) if str(path).strip()]
        if not self.enabled:
            return
        for script_path in self._script_paths(self.paths):
            self._load_script(script_path)
        self.scripts.sort(key=lambda item: (item.order, item.script_id, str(item.path)))

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"enabled": False, "paths": []}
        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:  # noqa: BLE001
            self.load_errors.append({"path": str(self.config_path), "error": f"Could not read config: {exc}"})
            return {"enabled": False, "paths": []}
        if not isinstance(payload, dict):
            self.load_errors.append({"path": str(self.config_path), "error": "Config must be one JSON object."})
            return {"enabled": False, "paths": []}
        paths = payload.get("paths", [])
        if paths in (None, ""):
            paths = []
        if not isinstance(paths, list):
            self.load_errors.append({"path": str(self.config_path), "error": "Config 'paths' must be a list."})
            paths = []
        return {"enabled": bool(payload.get("enabled")), "paths": paths}

    def _script_paths(self, paths: list[Path]) -> list[Path]:
        results: list[Path] = []
        for path in paths:
            if path.is_file() and path.suffix == ".py":
                results.append(path)
                continue
            if path.is_dir():
                results.extend(
                    candidate
                    for candidate in sorted(path.glob("*.py"))
                    if candidate.is_file() and not candidate.name.startswith("_")
                )
                continue
            self.load_errors.append({"path": str(path), "error": "Path is not a Python file or directory."})
        return results

    def _load_script(self, path: Path) -> None:
        try:
            module = self._import_script(path)
        except Exception as exc:  # noqa: BLE001
            self.load_errors.append({"path": str(path), "error": f"Import failed: {exc}"})
            return

        if not bool(getattr(module, "ENABLED", True)):
            self.skipped_scripts.append({"path": str(path), "reason": "ENABLED is false."})
            return
        parse_email = getattr(module, "parse_email", None)
        if not callable(parse_email):
            self.load_errors.append({"path": str(path), "error": "Script must define parse_email(email)."})
            return
        match = self._normalize_match(getattr(module, "MATCH", {}))
        if not match and not callable(getattr(module, "matches", None)):
            self.load_errors.append({"path": str(path), "error": "Script must define MATCH or matches(email)."})
            return

        script_id = str(getattr(module, "EXTRACTOR_ID", "") or path.stem).strip() or path.stem
        order = self._int_value(getattr(module, "ORDER", 100), default=100)
        self.scripts.append(
            _ScriptExtractor(
                script_id=script_id,
                path=path,
                module=module,
                order=order,
                run_on_parsed=bool(getattr(module, "RUN_ON_PARSED", False)),
                match=match,
            )
        )

    def _import_script(self, path: Path) -> ModuleType:
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(f"expense_script_extractor_{digest}", path)
        if spec is None or spec.loader is None:
            raise ValueError("Could not create an import spec.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _matching_scripts(self, record: SourceRecord, existing_facts: list[ParsedFact]) -> list[_ScriptExtractor]:
        if not self.enabled or not self.scripts:
            return []
        email_payload = self._email_payload(record, existing_facts)
        scripts: list[_ScriptExtractor] = []
        for script in self.scripts:
            if existing_facts and not script.run_on_parsed:
                continue
            try:
                if self._script_matches(script, record, email_payload):
                    scripts.append(script)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Script extractor match failed path=%s error=%s", script.path, exc)
        return scripts

    def _script_matches(self, script: _ScriptExtractor, record: SourceRecord, email_payload: dict[str, Any]) -> bool:
        matches_fn = getattr(script.module, "matches", None)
        if callable(matches_fn):
            return bool(matches_fn(dict(email_payload)))
        if not script.match:
            return False
        payload = record.payload if isinstance(record.payload, dict) else {}
        subject = str(payload.get("subject", "") or record.title)
        text_body = str(payload.get("textBody", ""))
        html_body = str(payload.get("htmlBody", ""))
        checks = {
            "from_contains": record.sender,
            "sender_contains": record.sender,
            "subject_contains": subject,
            "title_contains": record.title,
            "body_contains": f"{text_body}\n{html_body}",
            "text_contains": text_body,
            "html_contains": html_body,
            "any_contains": f"{record.sender}\n{subject}\n{text_body}\n{html_body}",
        }
        for key, haystack in checks.items():
            tokens = script.match.get(key, [])
            if tokens and not self._contains_any(haystack, tokens):
                return False
        return True

    def _run_script(
        self,
        script: _ScriptExtractor,
        record: SourceRecord,
        email_payload: dict[str, Any],
    ) -> tuple[list[ParsedFact], str]:
        try:
            raw_result = script.module.parse_email(dict(email_payload))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Script extractor parse failed path=%s error=%s", script.path, exc)
            return [], f"Script raised {exc.__class__.__name__}: {exc}"

        transactions = [raw_result] if isinstance(raw_result, dict) else raw_result
        if transactions is None:
            return [], ""
        if not isinstance(transactions, list):
            return [], "parse_email(email) must return a list of transaction dictionaries."

        facts: list[ParsedFact] = []
        errors: list[str] = []
        for index, item in enumerate(transactions):
            if not isinstance(item, dict):
                errors.append(f"item {index} is not an object")
                continue
            try:
                facts.extend(self._facts_from_transaction(script, record, item))
            except ValueError as exc:
                errors.append(f"item {index}: {exc}")
        return facts, "; ".join(errors)

    def _facts_from_transaction(
        self,
        script: _ScriptExtractor,
        record: SourceRecord,
        item: dict[str, Any],
    ) -> list[ParsedFact]:
        direction = str(self._field(item, "direction")).strip().lower()
        if direction not in {"debit", "credit"}:
            raise ValueError("direction must be debit or credit")
        amount = self._amount_value(self._field(item, "amount"))
        account_suffix = self._account_suffix(str(self._field(item, "account_suffix", "accountSuffix")))
        if not account_suffix:
            raise ValueError("account_suffix is required")
        timestamp = str(self._field(item, "timestamp") or record.received_at).strip()
        try:
            stamp = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601 when provided") from exc

        bank_name = str(self._field(item, "bank_name", "bankName") or script.script_id).strip() or script.script_id
        transaction_id = str(self._field(item, "transaction_id", "transactionId")).strip()
        counterparty = str(self._field(item, "counterparty")).strip()
        currency = str(self._field(item, "currency") or "INR").strip() or "INR"
        confidence = self._confidence_value(self._field(item, "confidence"), default=0.85)
        parser_id = f"{self.parser_id}:{script.script_id}"
        payload = {
            "bankId": script.script_id,
            "bankName": bank_name,
            "direction": direction,
            "amount": amount,
            "currency": currency,
            "transactionId": transaction_id,
            "counterparty": counterparty,
            "accountSuffix": account_suffix,
            "timestamp": stamp.isoformat(),
            "date": stamp.date().isoformat(),
            "time": stamp.time().isoformat(),
            "month": stamp.month,
            "year": stamp.year,
        }
        facts = [
            ParsedFact(
                fact_type="bank_transaction",
                parser_id=parser_id,
                source_provider_id=record.provider_id,
                source_record_type=record.record_type,
                source_external_id=record.external_id,
                payload=payload,
                confidence=confidence,
            )
        ]
        if direction == "debit":
            facts.append(
                ParsedFact(
                    fact_type="expense",
                    parser_id=f"{parser_id}:expense",
                    source_provider_id=record.provider_id,
                    source_record_type=record.record_type,
                    source_external_id=record.external_id,
                    payload={
                        "vendor": counterparty or bank_name,
                        "amount": amount,
                        "currency": currency,
                        "category": str(self._field(item, "category") or "Bank debit").strip() or "Bank debit",
                        "status": "debited",
                        "bankName": bank_name,
                        "transactionId": transaction_id,
                        "accountSuffix": account_suffix,
                        "timestamp": stamp.isoformat(),
                    },
                    confidence=confidence,
                )
            )
        return facts

    def _attempt(
        self,
        script: _ScriptExtractor,
        *,
        status: str,
        reason_code: str,
        reason_text: str,
        facts_count: int,
    ) -> dict[str, Any]:
        return {
            "parserId": self.parser_id,
            "matchedRuleId": script.script_id,
            "matchedRuleName": script.path.name,
            "parseStatus": status,
            "reasonCode": reason_code,
            "reasonText": reason_text,
            "factsCount": facts_count,
            "preview": {
                "scriptPath": str(script.path),
                "runOnParsed": script.run_on_parsed,
            },
        }

    def _email_payload(self, record: SourceRecord, existing_facts: list[ParsedFact]) -> dict[str, Any]:
        payload = record.payload if isinstance(record.payload, dict) else {}
        return {
            "provider_id": record.provider_id,
            "record_type": record.record_type,
            "external_id": record.external_id,
            "title": record.title,
            "sender": record.sender,
            "received_at": record.received_at,
            "subject": str(payload.get("subject", "") or record.title),
            "text_body": str(payload.get("textBody", "")),
            "html_body": str(payload.get("htmlBody", "")),
            "payload": dict(payload),
            "existing_facts": [
                {
                    "fact_type": fact.fact_type,
                    "parser_id": fact.parser_id,
                    "payload": dict(fact.payload),
                    "confidence": fact.confidence,
                }
                for fact in existing_facts
            ],
        }

    def _normalize_match(self, value: Any) -> dict[str, list[str]]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, list[str]] = {}
        for key, raw_items in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key not in self.MATCH_KEYS:
                continue
            items = raw_items if isinstance(raw_items, list) else [raw_items]
            tokens = [str(item).strip().lower() for item in items if str(item).strip()]
            if tokens:
                result[normalized_key] = tokens
        return result

    def _contains_any(self, value: str, tokens: list[str]) -> bool:
        haystack = str(value or "").lower()
        return any(token in haystack for token in tokens)

    def _field(self, item: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
        return ""

    def _amount_value(self, value: Any) -> float:
        try:
            return float(str(value).replace(",", "").strip())
        except ValueError as exc:
            raise ValueError("amount must be numeric") from exc

    def _confidence_value(self, value: Any, *, default: float) -> float:
        if value in (None, ""):
            return default
        try:
            return min(max(float(value), 0.0), 1.0)
        except ValueError:
            return default

    def _account_suffix(self, value: str) -> str:
        digits = re.sub(r"[^0-9]", "", str(value or ""))
        return digits[-4:] if digits else ""

    def _int_value(self, value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
