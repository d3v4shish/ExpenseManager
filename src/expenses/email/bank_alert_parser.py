from __future__ import annotations
import logging
import re
from typing import Any

from src.expenses.email.extractors.axis_extractor import AxisBankExtractor
from src.expenses.email.extractors.declarative_extractor import DeclarativeBankExtractor
from src.expenses.email.extractors.sbi_extractor import SbiBankExtractor
from src.expenses.email.bank_rule_catalog import BankRuleCatalog
from src.expenses.email.mail_types import ParsedFact, SourceRecord


class BankAlertParser:
    """Parse bank alert emails into normalized transaction and expense facts."""

    parser_id = "bank_alert_email"

    def __init__(self, rule_catalog: BankRuleCatalog) -> None:
        """Store the shared bank-rule catalog and extractor set."""

        self.rule_catalog = rule_catalog
        self.logger = logging.getLogger(self.__class__.__name__)
        self._extractors = {
            "declarative": DeclarativeBankExtractor(),
            "sbi": SbiBankExtractor(),
            "axis": AxisBankExtractor(),
        }

    def matches(self, record: SourceRecord) -> bool:
        """Return whether the record looks like a supported bank alert email."""

        return bool(self.diagnose(record).get("candidateMatched"))

    def parse(self, record: SourceRecord) -> list[ParsedFact]:
        """Return the normalized facts emitted from one bank alert email."""

        return list(self.diagnose(record).get("facts", []))

    def diagnose(self, record: SourceRecord, *, rules_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the candidate-match, diagnostics, and parsed facts for one email."""

        if record.record_type != "email":
            return {"candidateMatched": False, "facts": [], "attempts": []}

        sender = record.sender.lower()
        subject = str(record.payload.get("subject", "")).lower()
        bank_rule = self._match_bank_rule(sender, subject, rules_payload=rules_payload)
        if bank_rule is None:
            rule_miss = self._rule_miss_attempt(record)
            if rule_miss is not None:
                return {"candidateMatched": True, "facts": [], "attempts": [rule_miss]}
            return {"candidateMatched": False, "facts": [], "attempts": []}

        payload = self._extract_payload(record, bank_rule)
        bank_name = str(bank_rule.get("name", bank_rule.get("id", "Unknown bank"))).strip() or "Unknown bank"
        rule_id = str(bank_rule.get("id", "")).strip()
        if payload is None:
            self.logger.info(
                "Skipped bank-like email bank=%s source_external_id=%s sender=%s title=%s reason=no_payload",
                bank_name,
                record.external_id,
                record.sender,
                record.title,
            )
            return {
                "candidateMatched": True,
                "facts": [],
                "attempts": [
                    self._diagnostic_attempt(
                        bank_rule=bank_rule,
                        status="failed",
                        reason_code="no_payload",
                        reason_text="The matched rule could not extract any structured payload from this email.",
                        facts_count=0,
                        preview={},
                    )
                ],
            }

        direction = str(payload.get("direction", "")).strip()
        amount = payload.get("amount")
        transaction_id = str(payload.get("transactionId", "")).strip()
        counterparty = str(payload.get("counterparty", "")).strip()
        account_suffix = str(payload.get("accountSuffix", "")).strip()
        timestamp_value = str(payload.get("timestamp", "")).strip()
        has_transaction_evidence = bool(payload.get("hasTransactionEvidence"))
        has_required_fields = bool(payload.get("hasRequiredFields"))
        preview = {
            "bankName": bank_name,
            "direction": direction,
            "amount": amount,
            "transactionId": transaction_id,
            "counterparty": counterparty,
            "accountSuffix": account_suffix,
            "timestamp": timestamp_value,
            "extractorId": str(payload.get("extractorId", "")).strip(),
        }
        self.logger.info(
            "Bank email candidate bank=%s source_external_id=%s sender=%s title=%s direction=%s amount=%s transaction_id=%s counterparty=%s account_suffix=%s has_transaction_evidence=%s has_required_fields=%s extractor=%s",
            bank_name,
            record.external_id,
            record.sender,
            record.title,
            direction or "<unknown>",
            amount,
            transaction_id or "<none>",
            counterparty or "<none>",
            account_suffix or "<none>",
            has_transaction_evidence,
            has_required_fields,
            payload.get("extractorId", "<unknown>"),
        )
        if not has_transaction_evidence or not has_required_fields:
            self.logger.info(
                "Skipped bank-like email bank=%s source_external_id=%s sender=%s title=%s reason=%s",
                bank_name,
                record.external_id,
                record.sender,
                record.title,
                "missing_required_fields" if not has_required_fields else "no_transaction_evidence",
            )
            reason_code = "missing_required_fields" if not has_required_fields else "no_transaction_evidence"
            return {
                "candidateMatched": True,
                "facts": [],
                "attempts": [
                    self._diagnostic_attempt(
                        bank_rule=bank_rule,
                        status="failed",
                        reason_code=reason_code,
                        reason_text=self._reason_text(reason_code, bank_name),
                        facts_count=0,
                        preview=preview,
                    )
                ],
            }

        transaction_payload = {
            "bankId": str(payload.get("bankId", "")).strip(),
            "bankName": bank_name,
            "direction": direction,
            "amount": amount,
            "currency": str(payload.get("currency", "INR")).strip() or "INR",
            "transactionId": transaction_id,
            "counterparty": counterparty,
            "accountSuffix": account_suffix,
            "timestamp": timestamp_value,
            "date": str(payload.get("date", "")).strip(),
            "time": str(payload.get("time", "")).strip(),
            "month": payload.get("month"),
            "year": payload.get("year"),
        }
        confidence = float(payload.get("confidence", 0.8) or 0.8)
        facts = [
            ParsedFact(
                fact_type="bank_transaction",
                parser_id=f"{self.parser_id}:{rule_id or 'unknown'}",
                source_provider_id=record.provider_id,
                source_record_type=record.record_type,
                source_external_id=record.external_id,
                payload=transaction_payload,
                confidence=confidence,
            )
        ]
        if direction == "debit":
            facts.append(
                ParsedFact(
                    fact_type="expense",
                    parser_id=f"{self.parser_id}:{rule_id or 'unknown'}:expense",
                    source_provider_id=record.provider_id,
                    source_record_type=record.record_type,
                    source_external_id=record.external_id,
                    payload={
                        "vendor": counterparty or bank_name,
                        "amount": amount,
                        "currency": "INR",
                        "category": "Bank debit",
                        "status": "debited",
                        "bankName": bank_name,
                        "transactionId": transaction_id,
                        "accountSuffix": account_suffix,
                        "timestamp": timestamp_value,
                    },
                    confidence=confidence,
                )
            )
        self.logger.info(
            "Parsed bank alert bank=%s direction=%s amount=%s transaction_id=%s counterparty=%s source_external_id=%s",
            bank_name,
            direction or "<unknown>",
            amount,
            transaction_id or "<none>",
            counterparty or "<none>",
            record.external_id,
        )
        return {
            "candidateMatched": True,
            "facts": facts,
            "attempts": [
                self._diagnostic_attempt(
                    bank_rule=bank_rule,
                    status="parsed",
                    reason_code="",
                    reason_text="",
                    facts_count=len(facts),
                    preview=preview,
                )
            ],
        }

    def _rules(self, rules_payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return the effective bank-rule list."""

        payload = dict(rules_payload) if isinstance(rules_payload, dict) else self.rule_catalog.load_effective()
        banks = payload.get("banks", [])
        if not isinstance(banks, list):
            return []
        return [item for item in banks if isinstance(item, dict)]

    def _match_bank_rule(self, sender: str, subject: str, *, rules_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return the first bank rule that matches the sender and subject."""

        for bank in self._rules(rules_payload):
            match = bank.get("match", {})
            from_contains = [str(item).lower() for item in match.get("fromContains", [])]
            subject_contains = [str(item).lower() for item in match.get("subjectContains", [])]
            from_ok = any(token in sender for token in from_contains) if from_contains else True
            subject_ok = any(token in subject for token in subject_contains) if subject_contains else True
            if from_ok and subject_ok:
                return bank
        return None

    def _extract_payload(self, record: SourceRecord, bank_rule: dict[str, Any]) -> dict[str, Any] | None:
        """Run the configured extractor and normalize its output."""

        extractor = self._resolve_extractor(bank_rule) or self._extractors["declarative"]
        payload = extractor.extract(record, bank_rule)
        if payload is None:
            return None
        return {**payload, "extractorId": getattr(extractor, "extractor_id", extractor.__class__.__name__)}

    def _resolve_extractor(self, bank_rule: dict[str, Any]):
        """Resolve the configured extractor instance for one bank rule."""

        extractor_config = bank_rule.get("extractor", {})
        if not isinstance(extractor_config, dict):
            return self._extractors.get("declarative")
        extractor_type = str(extractor_config.get("type", "declarative")).strip().lower()
        target = str(extractor_config.get("target", "")).strip().lower()
        if extractor_type in {"class", "function"} and target:
            return self._extractors.get(target)
        return self._extractors.get("declarative")

    def _diagnostic_attempt(
        self,
        *,
        bank_rule: dict[str, Any],
        status: str,
        reason_code: str,
        reason_text: str,
        facts_count: int,
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        """Return one persisted diagnostic attempt payload."""

        return {
            "parserId": self.parser_id,
            "matchedRuleId": str(bank_rule.get("id", "")).strip(),
            "matchedRuleName": str(bank_rule.get("name", bank_rule.get("id", ""))).strip(),
            "parseStatus": status,
            "reasonCode": reason_code,
            "reasonText": reason_text,
            "factsCount": max(0, int(facts_count)),
            "preview": dict(preview),
        }

    def _rule_miss_attempt(self, record: SourceRecord) -> dict[str, Any] | None:
        evidence = self._bank_like_evidence(record)
        if not evidence:
            return None
        sender = str(record.sender or "").strip()
        subject = str(record.payload.get("subject", record.title)).strip()
        body = str(record.payload.get("textBody", "") or record.payload.get("htmlBody", "")).strip()
        return self._diagnostic_attempt(
            bank_rule={"id": "", "name": "Unmatched bank-like email"},
            status="failed",
            reason_code="no_matching_rule",
            reason_text="This email looks like a bank transaction alert, but no active bank rule matched its sender and subject.",
            facts_count=0,
            preview={
                "sender": sender,
                "subject": subject,
                "evidence": evidence,
                "bodySnippet": self._snippet(body),
                "suggestedFromContains": self._sender_suggestions(sender),
                "suggestedSubjectContains": self._subject_suggestions(subject),
            },
        )

    def _bank_like_evidence(self, record: SourceRecord) -> list[str]:
        sender = str(record.sender or "").lower()
        subject = str(record.payload.get("subject", record.title)).lower()
        body = str(record.payload.get("textBody", "") or record.payload.get("htmlBody", "")).lower()
        text = " ".join([sender, subject, body])
        evidence: list[str] = []
        if re.search(r"(?:\binr\b|\brs\.?\b|₹)\s*[0-9]", text):
            evidence.append("amount")
        if re.search(r"\b(?:debited|credited|debit|credit|spent|paid|received)\b", text):
            evidence.append("direction")
        if re.search(r"\b(?:upi|transaction|txn|ref(?:erence)?|a/c|account|card)\b", text):
            evidence.append("transaction_terms")
        if re.search(r"\b(?:bank|alerts?|statement|card|upi|noreply|no-reply)\b", sender):
            evidence.append("sender")
        if re.search(r"\b(?:alert|transaction|debit|credit|payment|spent|received)\b", subject):
            evidence.append("subject")
        if "amount" in evidence and ("direction" in evidence or "transaction_terms" in evidence or "sender" in evidence):
            return evidence
        if len(evidence) >= 3:
            return evidence
        return []

    def _sender_suggestions(self, sender: str) -> list[str]:
        sender_text = str(sender or "").strip().lower()
        match = re.search(r"@([a-z0-9.-]+)", sender_text)
        if match:
            return [match.group(1)]
        clean = re.sub(r"[^a-z0-9@._-]+", " ", sender_text).strip()
        return [clean] if clean else []

    def _subject_suggestions(self, subject: str) -> list[str]:
        text = re.sub(r"[^a-z0-9]+", " ", str(subject or "").strip().lower())
        tokens = [token for token in text.split() if len(token) >= 4 and not token.isdigit()]
        if not tokens:
            return []
        return [" ".join(tokens[:4])]

    def _snippet(self, value: str, *, limit: int = 260) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit].rstrip()}..."

    def _reason_text(self, reason_code: str, bank_name: str) -> str:
        """Return one user-facing reason string for the debug pane."""

        mapping = {
            "missing_required_fields": f"{bank_name} matched, but one or more required fields were missing.",
            "no_transaction_evidence": f"{bank_name} matched, but the extracted payload did not contain transaction evidence.",
            "no_payload": f"{bank_name} matched, but the configured extractor could not read a payload from the email.",
            "no_matching_rule": "This email looks like a bank transaction alert, but no active bank rule matched its sender and subject.",
        }
        return mapping.get(reason_code, reason_code)
