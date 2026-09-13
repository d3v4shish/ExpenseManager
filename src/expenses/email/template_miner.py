"""Deterministic, review-first clustering for stored bank-alert emails.

This is intentionally a small Drain-style miner rather than an adaptive parser.
It only produces safe, masked templates and never changes an active bank rule.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from src.expenses.email.bank_rule_catalog import build_bank_rule_template


class BankAlertTemplateMiner:
    """Cluster structurally similar local bank-alert messages deterministically."""

    ALGORITHM_VERSION = "drain-style-v1"
    MAX_RECORDS = 2_000
    MAX_EVIDENCE_IDS = 5
    MIN_SIMILARITY = 0.60
    MAX_LENGTH_DELTA = 8
    MAX_TOKENS = 96
    _TOKEN_PATTERN = re.compile(r"<[^>]+>|[a-z0-9]+(?:[./:@-][a-z0-9]+)*|₹|[^\w\s]", re.IGNORECASE)
    _SAFE_WORDS = {
        "a", "ac", "account", "alert", "amount", "at", "bank", "been", "by", "card", "cash",
        "credit", "credited", "debited", "debit", "for", "from", "has", "id", "inr", "is", "of",
        "on", "paid", "payment", "received", "ref", "reference", "rs", "spent", "to", "transaction",
        "transfer", "txn", "upi", "using", "via", "was", "with", "your",
    }

    def mine(self, records: list[dict[str, Any]], *, min_support: int = 2) -> dict[str, Any]:
        """Return bounded masked template suggestions for stored email records."""

        support = max(1, int(min_support or 2))
        ordered = sorted(
            (dict(record) for record in records if isinstance(record, dict)),
            key=lambda item: (int(item.get("sourceRecordId", 0) or 0), str(item.get("externalId", ""))),
        )[: self.MAX_RECORDS]
        clusters_by_sender: dict[str, list[_TemplateCluster]] = {}
        scanned = 0
        for record in ordered:
            payload = record.get("payload", {}) if isinstance(record.get("payload", {}), dict) else {}
            subject = str(payload.get("subject", record.get("title", ""))).strip()
            body = str(payload.get("textBody", "") or payload.get("htmlBody", "")).strip()
            body_tokens = self._tokens(body)
            subject_tokens = self._tokens(subject)
            if not body_tokens and not subject_tokens:
                continue
            scanned += 1
            sender_key = self._sender_key(str(record.get("sender", "")))
            candidates = clusters_by_sender.setdefault(sender_key, [])
            cluster = self._best_cluster(candidates, subject_tokens, body_tokens)
            if cluster is None:
                cluster = _TemplateCluster(sender_key=sender_key, subject_tokens=subject_tokens, body_tokens=body_tokens)
                candidates.append(cluster)
            cluster.add(record, subject_tokens, body_tokens, evidence_limit=self.MAX_EVIDENCE_IDS)

        templates = [
            self._template_payload(cluster)
            for clusters in clusters_by_sender.values()
            for cluster in clusters
            if cluster.support >= support
        ]
        templates.sort(key=lambda item: (-int(item["support"]), str(item["templateKey"])))
        return {
            "algorithm": self.ALGORITHM_VERSION,
            "scannedRecords": scanned,
            "minSupport": support,
            "maxRecords": self.MAX_RECORDS,
            "templates": templates,
        }

    def build_rule_draft(self, template: dict[str, Any], *, existing_ids: set[str] | None = None) -> dict[str, Any]:
        """Create an editable declarative-rule draft from one mined template."""

        suggestion = dict(template) if isinstance(template, dict) else {}
        key = str(suggestion.get("templateKey", "")).strip()
        sender_hint = str(suggestion.get("senderHint", "")).strip().lower()
        if not key or not sender_hint:
            raise ValueError("A mined template with a key and sender hint is required.")
        existing = {str(value).strip() for value in (existing_ids or set()) if str(value).strip()}
        rule = build_bank_rule_template(existing_ids=existing)
        bank_name = self._bank_name(sender_hint)
        rule_id = self._unique_rule_id(f"mined_{self._slug(sender_hint)}", existing)
        rule["id"] = rule_id
        rule["name"] = bank_name
        rule.setdefault("defaults", {})["bankName"] = bank_name
        rule.setdefault("match", {})["fromContains"] = [sender_hint]
        subject_contains = self._subject_contains(suggestion.get("subjectTemplate", ""))
        if subject_contains:
            rule.setdefault("match", {})["subjectContains"] = subject_contains
        rule["templateMining"] = {
            "algorithm": self.ALGORITHM_VERSION,
            "templateKey": key,
            "support": max(0, int(suggestion.get("support", 0) or 0)),
            "senderHint": sender_hint,
        }
        return rule

    def _best_cluster(
        self,
        clusters: list["_TemplateCluster"],
        subject_tokens: list[str],
        body_tokens: list[str],
    ) -> "_TemplateCluster | None":
        best: _TemplateCluster | None = None
        best_score = 0.0
        for cluster in clusters:
            score = self._similarity(cluster.subject_tokens, subject_tokens, weight=0.30)
            score += self._similarity(cluster.body_tokens, body_tokens, weight=0.70)
            if score < self.MIN_SIMILARITY:
                continue
            if best is None or score > best_score:
                best = cluster
                best_score = score
        return best

    def _similarity(self, template_tokens: list[str], tokens: list[str], *, weight: float) -> float:
        if not template_tokens and not tokens:
            return weight
        if not template_tokens or not tokens or abs(len(template_tokens) - len(tokens)) > self.MAX_LENGTH_DELTA:
            return 0.0
        size = max(len(template_tokens), len(tokens))
        matches = 0
        for expected, actual in zip(template_tokens, tokens):
            if expected == actual or expected in {"<*>", "<NUM>", "<AMOUNT>", "<ID>", "<ACCOUNT>", "<DATE>", "<TIME>"}:
                matches += 1
        return weight * (matches / size)

    def _template_payload(self, cluster: "_TemplateCluster") -> dict[str, Any]:
        subject_template = self._render(cluster.subject_tokens)
        body_template = self._render(cluster.body_tokens)
        digest = hashlib.sha256(f"{cluster.sender_key}\n{subject_template}\n{body_template}".encode("utf-8")).hexdigest()[:20]
        return {
            "templateKey": f"tmpl_{digest}",
            "senderHint": cluster.sender_key,
            "support": cluster.support,
            "subjectTemplate": subject_template,
            "bodyTemplate": body_template,
            "sourceRecordIds": sorted(cluster.source_record_ids)[: self.MAX_EVIDENCE_IDS],
        }

    def _tokens(self, text: str) -> list[str]:
        masked = self._mask_volatile_values(text)
        tokens: list[str] = []
        for match in self._TOKEN_PATTERN.finditer(masked.lower()):
            token = match.group(0)
            if token.startswith("<") and token.endswith(">"):
                tokens.append(token.upper())
            elif token.isalpha() and token not in self._SAFE_WORDS:
                tokens.append("<*>")
            elif re.fullmatch(r"[a-z0-9]{4,}", token) and any(character.isdigit() for character in token):
                tokens.append("<ID>")
            elif token.isdigit():
                tokens.append("<NUM>")
            else:
                tokens.append(token)
            if len(tokens) >= self.MAX_TOKENS:
                break
        return tokens

    @staticmethod
    def _mask_volatile_values(text: str) -> str:
        value = re.sub(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", " <EMAIL> ", str(text or ""))
        value = re.sub(r"\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b", " <DATE> ", value)
        value = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " <TIME> ", value)
        value = re.sub(r"(?i)(?:inr|rs\.?|₹)\s*[0-9][0-9,]*(?:\.\d{1,2})?", " INR <AMOUNT> ", value)
        value = re.sub(r"(?i)(?:a/c|account)(?:\s*(?:no|number))?\s*[:#-]?\s*x+\d{3,}", " ACCOUNT <ACCOUNT> ", value)
        value = re.sub(
            r"(?i)\b(?:upi\s*)?(?:ref(?:erence)?|txn)(?:\s*(?:id|no))?\s*[:#-]?\s*[a-z0-9-]{5,}\b"
            r"|\btransaction\s+(?:id|ref(?:erence)?|no)\s*[:#-]?\s*[a-z0-9-]{5,}\b",
            " REFERENCE <ID> ",
            value,
        )
        value = re.sub(r"(?i)\bx+\d{3,}\b", " <ACCOUNT> ", value)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _render(tokens: list[str]) -> str:
        rendered = " ".join(tokens)
        rendered = re.sub(r"\s+([,.:;!?])", r"\1", rendered)
        rendered = re.sub(r"\(\s+", "(", rendered)
        rendered = re.sub(r"\s+\)", ")", rendered)
        return rendered.strip()

    @staticmethod
    def _sender_key(sender: str) -> str:
        value = str(sender or "").strip().lower()
        match = re.search(r"@([a-z0-9.-]+)", value)
        return match.group(1) if match else re.sub(r"[^a-z0-9.-]+", "", value) or "unknown-sender"

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "bank"

    def _unique_rule_id(self, base: str, existing: set[str]) -> str:
        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    def _bank_name(self, sender_hint: str) -> str:
        stem = sender_hint.split(".", 1)[0]
        words = [word for word in re.split(r"[^a-z0-9]+", stem) if word and word not in {"alert", "alerts", "mail", "noreply", "no"}]
        return " ".join(word.capitalize() for word in words[:3]) or "Mined Bank"

    def _subject_contains(self, subject_template: Any) -> list[str]:
        words = [
            token
            for token in re.findall(r"[a-z]{4,}", str(subject_template or "").lower())
            if token in self._SAFE_WORDS and token not in {"account", "amount", "your"}
        ]
        if not words:
            return []
        return [" ".join(words[:3])]


@dataclass
class _TemplateCluster:
    sender_key: str
    subject_tokens: list[str]
    body_tokens: list[str]
    support: int = 0
    source_record_ids: list[int] = field(default_factory=list)

    def add(
        self,
        record: dict[str, Any],
        subject_tokens: list[str],
        body_tokens: list[str],
        *,
        evidence_limit: int,
    ) -> None:
        self.support += 1
        self.subject_tokens = self._merge_tokens(self.subject_tokens, subject_tokens)
        self.body_tokens = self._merge_tokens(self.body_tokens, body_tokens)
        source_record_id = int(record.get("sourceRecordId", 0) or 0)
        if source_record_id > 0 and len(self.source_record_ids) < evidence_limit:
            self.source_record_ids.append(source_record_id)

    @staticmethod
    def _merge_tokens(current: list[str], incoming: list[str]) -> list[str]:
        if not current:
            return list(incoming)
        if not incoming:
            return list(current)
        length = max(len(current), len(incoming))
        merged: list[str] = []
        for index in range(length):
            prior = current[index] if index < len(current) else "<*>"
            next_value = incoming[index] if index < len(incoming) else "<*>"
            merged.append(prior if prior == next_value else "<*>")
        return merged
