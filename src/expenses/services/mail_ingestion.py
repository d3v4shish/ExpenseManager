from __future__ import annotations

import logging
import shutil
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from src.expenses.email.mail_parser_chain import MailParserChain
from src.expenses.email.mail_types import ParsedFact, SourceRecord
from src.expenses.email.thunderbird_profile import discover_default_thunderbird_profiles
from src.expenses.email.thunderbird_reader import ThunderbirdMailboxReader
from src.expenses.repositories.expenses_repository import ExpensesRepository


class ExpensesMailIngestionService:
    """Scan mailbox sources and persist expense-relevant email facts."""

    COOPERATIVE_YIELD_EVERY = 50
    COOPERATIVE_YIELD_SECONDS = 0.001
    WRITE_BATCH_SIZE = 100

    def __init__(
        self,
        *,
        repository: ExpensesRepository,
        parser_chain: MailParserChain,
        config_store,
        thunderbird_local_path: Path,
    ) -> None:
        """Store runtime dependencies for one expenses mailbox ingestion flow."""

        self.repository = repository
        self.parser_chain = parser_chain
        self.config_store = config_store
        self.thunderbird_local_path = thunderbird_local_path
        self.logger = logging.getLogger(self.__class__.__name__)

    def refresh_email_records(
        self,
        progress_callback: Callable[[dict], None] | None = None,
        *,
        force_full: bool = False,
    ) -> dict[str, int]:
        """Incrementally ingest expense-related email records."""

        return self._ingest_records(force_full=force_full, reparse_all=force_full, progress_callback=progress_callback)

    def rebuild_email_records(self, progress_callback: Callable[[dict], None] | None = None) -> dict[str, int]:
        """Rebuild every expense email record with rollback on failure."""

        snapshot_dir = self.repository.create_rebuild_snapshot()
        try:
            self.repository.clear_expense_state()
            return self._ingest_records(force_full=True, reparse_all=True, progress_callback=progress_callback)
        except Exception:
            self.logger.exception("Expenses rebuild failed; restoring previous repository snapshot")
            self.repository.restore_rebuild_snapshot(snapshot_dir)
            raise
        finally:
            shutil.rmtree(snapshot_dir, ignore_errors=True)

    def validate_thunderbird_directory(self) -> dict[str, Any]:
        """Return whether the current Thunderbird config resolves to readable mailbox files."""

        try:
            config = self.config_store.load_user("email_accounts.json")
        except FileNotFoundError:
            return {
                "valid": False,
                "message": "Expense Manager does not have a Thunderbird account configured.",
                "mailboxPaths": [],
                "providers": [],
                "suggestedProfiles": self._suggest_thunderbird_profiles(),
            }

        providers = [item for item in config.get("providers", []) if isinstance(item, dict) and bool(item.get("enabled"))]
        thunderbird_providers = [
            provider
            for provider in providers
            if str(provider.get("type", "")).strip().lower() == "thunderbird"
        ]
        if not thunderbird_providers:
            return {
                "valid": False,
                "message": "Expense Manager does not have an enabled Thunderbird account configured.",
                "mailboxPaths": [],
                "providers": [],
                "suggestedProfiles": self._suggest_thunderbird_profiles(),
            }

        resolved_paths: list[str] = []
        provider_reports: list[dict[str, Any]] = []
        for provider_config in thunderbird_providers:
            reader = ThunderbirdMailboxReader(provider_config, self.thunderbird_local_path)
            report = reader.describe_source()
            provider_reports.append(report)
            resolved_paths.extend(str(path) for path in report.get("mailboxPaths", []) if Path(str(path)).is_file())
        if resolved_paths:
            return {
                "valid": True,
                "message": "",
                "mailboxPaths": resolved_paths,
                "providers": provider_reports,
                "suggestedProfiles": [],
            }
        suggestions = self._suggest_thunderbird_profiles()
        return {
            "valid": False,
            "message": self._thunderbird_validation_message(provider_reports, suggestions),
            "mailboxPaths": [],
            "providers": provider_reports,
            "suggestedProfiles": suggestions,
        }

    def _suggest_thunderbird_profiles(self) -> list[dict[str, Any]]:
        suggestions: list[dict[str, Any]] = []
        for item in discover_default_thunderbird_profiles():
            accounts = [dict(account) for account in item.get("accounts", []) if isinstance(account, dict)]
            if not accounts:
                continue
            suggestions.append(
                {
                    "profilePath": str(item.get("profilePath", "")),
                    "accounts": accounts,
                    "warnings": list(item.get("warnings", [])),
                }
            )
        return suggestions[:5]

    def _thunderbird_validation_message(
        self,
        provider_reports: list[dict[str, Any]],
        suggestions: list[dict[str, Any]],
    ) -> str:
        if not provider_reports:
            return "Expense Manager could not find a valid Thunderbird mailbox directory to parse."
        report = provider_reports[0]
        account_email = str(report.get("accountEmail", "")).strip()
        profile_path = str(report.get("profilePath", "")).strip()
        if profile_path and not bool(report.get("profileExists")):
            return f"Configured Thunderbird profile does not exist for {account_email}: {profile_path}"
        if profile_path and not bool(report.get("prefsExists")):
            return f"Configured Thunderbird profile is missing prefs.js for {account_email}: {profile_path}"
        missing = [str(path) for path in report.get("missingMailboxPaths", []) if str(path).strip()]
        if missing:
            return f"Configured Thunderbird mailbox path does not exist for {account_email}: {missing[0]}"
        if suggestions:
            suggested = str(suggestions[0].get("profilePath", "")).strip()
            return f"No parseable mailbox was found for {account_email}. A Thunderbird profile was found at: {suggested}"
        return f"No parseable Thunderbird mailbox was found for {account_email or 'the configured account'}."

    def reparse_stored_email_records(self, matcher: Callable[[SourceRecord], bool]) -> dict[str, int]:
        """Re-run parsing for stored expense source records that match one predicate."""

        reparsed = 0
        parsed_facts = 0
        for row in self.repository.list_source_records(record_type="email"):
            record = self._source_record_from_row(row)
            if not matcher(record):
                continue
            inspection = self._inspect_candidate(record)
            if not bool(inspection.get("candidateMatched")):
                inspection = {
                    "candidateMatched": False,
                    "facts": [],
                    "attempts": [
                        {
                            "parserId": "bank_alert_email",
                            "matchedRuleId": "",
                            "matchedRuleName": "",
                            "parseStatus": "failed",
                            "reasonCode": "no_match",
                            "reasonText": "Current rules no longer match this stored candidate email.",
                            "factsCount": 0,
                            "preview": {},
                        }
                    ],
                }
            self.repository.replace_parse_attempts_for_source(int(row.get("sourceRecordId", 0) or 0), inspection.get("attempts", []))
            self.repository.replace_facts_for_source(int(row.get("sourceRecordId", 0) or 0), inspection.get("facts", []))
            reparsed += 1
            parsed_facts += len(inspection.get("facts", []))
        return {"reparsedRecords": reparsed, "parsedFacts": parsed_facts}

    def diagnose_source_record(self, source_record_id: int, *, rules_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return one stored candidate email plus live parser diagnostics."""

        row = self.repository.get_source_record(source_record_id)
        if row is None:
            raise ValueError("Candidate email not found.")
        record = self._source_record_from_row(row)
        inspection = self._inspect_candidate(record, rules_payload=rules_payload)
        return {
            **row,
            "candidateMatched": bool(inspection.get("candidateMatched")),
            "attempts": list(inspection.get("attempts", [])),
            "facts": list(inspection.get("facts", [])),
        }

    def script_extractor_status(self) -> dict[str, Any]:
        """Return status for the optional trusted Python script parser."""

        for parser in getattr(self.parser_chain, "_parsers", []):
            status_fn = getattr(parser, "status", None)
            if callable(status_fn) and str(getattr(parser, "parser_id", "")).strip() == "scripted_email":
                return dict(status_fn())
        return {
            "enabled": False,
            "configPath": "",
            "configExists": False,
            "paths": [],
            "loadedCount": 0,
            "scripts": [],
            "loadErrors": [],
            "skippedScripts": [],
        }

    def _ingest_records(
        self,
        *,
        force_full: bool,
        reparse_all: bool,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> dict[str, int]:
        """Run the configured mailbox readers and persist changed expense facts."""

        try:
            config = self.config_store.load_user("email_accounts.json")
        except FileNotFoundError:
            self.logger.info("No email_accounts.json found; skipping expenses email ingestion")
            return {
                "ingestedCount": 0,
                "changedRecords": 0,
                "skippedUnchanged": 0,
                "parsedFacts": 0,
            }

        providers = [item for item in config.get("providers", []) if isinstance(item, dict) and bool(item.get("enabled"))]
        sync = config.get("sync", {})
        lookback_days = int(sync.get("lookbackDays", 14) or 14)
        overlap_hours = int(sync.get("overlapHours", 24) or 24)
        now = datetime.now().astimezone()
        default_since = now - timedelta(days=lookback_days)
        ingested_count = 0
        changed_count = 0
        skipped_unchanged_count = 0
        parsed_count = 0

        for provider_config in providers:
            provider_type = str(provider_config.get("type", "")).strip().lower()
            provider_id = str(provider_config.get("id", "")).strip() or provider_type or "unknown"
            if provider_type != "thunderbird":
                continue

            since = None if force_full else default_since
            if not force_full:
                checkpoint = self.repository.get_provider_checkpoint(provider_id)
                if checkpoint is not None:
                    last_received_at = self._parse_dt(checkpoint.get("lastReceivedAt", ""))
                    if last_received_at is not None:
                        checkpoint_since = last_received_at - timedelta(hours=overlap_hours)
                        if since is None or checkpoint_since > since:
                            since = checkpoint_since

            reader = ThunderbirdMailboxReader(provider_config, self.thunderbird_local_path)
            records = reader.fetch_records(since=since)
            self._log_bank_sender_samples(provider_id, records)

            latest_received_at = None
            provider_processed = 0
            provider_ingested = 0
            provider_changed = 0
            provider_skipped = 0
            provider_parsed = 0
            total_records = len(records)
            pending_results: list[tuple[SourceRecord, list[dict[str, Any]], list[ParsedFact]]] = []

            def flush_pending_results() -> None:
                nonlocal provider_ingested, provider_changed, provider_skipped, provider_parsed
                if not pending_results:
                    return
                results = self.repository.upsert_source_record_parse_results(
                    pending_results,
                    reparse_all=reparse_all,
                )
                for result in results:
                    provider_ingested += 1
                    if bool(result.get("changed")) or reparse_all:
                        provider_changed += 1
                    else:
                        provider_skipped += 1
                    provider_parsed += int(result.get("factsCount", 0) or 0)
                pending_results.clear()

            if progress_callback is not None:
                progress_callback(
                    {
                        "stage": "provider_started",
                        "providerId": provider_id,
                        "processed": 0,
                        "total": total_records,
                        "changedRecords": 0,
                        "skippedUnchanged": 0,
                        "parsedFacts": 0,
                        "forceFull": force_full,
                    }
                )

            for record in records:
                provider_processed += 1
                if not reparse_all:
                    existing = self.repository.find_source_record_identity(record.provider_id, record.record_type, record.external_id)
                    if existing is not None and str(existing.get("contentHash", "")).strip() == record.content_hash:
                        provider_ingested += 1
                        provider_skipped += 1
                        received_at = self._parse_dt(record.received_at)
                        if received_at is not None and (latest_received_at is None or received_at > latest_received_at):
                            latest_received_at = received_at
                        if force_full and total_records and (provider_processed == total_records or provider_processed % 250 == 0):
                            flush_pending_results()
                            self._emit_progress(
                                progress_callback,
                                "provider_progress",
                                provider_id,
                                provider_processed,
                                total_records,
                                provider_changed,
                                provider_skipped,
                                provider_parsed,
                                force_full,
                            )
                        self._cooperative_yield(provider_processed)
                        continue
                inspection = self._inspect_candidate(record)
                if not bool(inspection.get("candidateMatched")):
                    if force_full and total_records and (provider_processed == total_records or provider_processed % 250 == 0):
                        flush_pending_results()
                        self._emit_progress(
                            progress_callback,
                            "provider_progress",
                            provider_id,
                            provider_processed,
                            total_records,
                            provider_changed,
                            provider_skipped,
                            provider_parsed,
                            force_full,
                        )
                    self._cooperative_yield(provider_processed)
                    continue
                facts = list(inspection.get("facts", []))
                attempts = list(inspection.get("attempts", []))
                pending_results.append((record, attempts, facts))
                received_at = self._parse_dt(record.received_at)
                if received_at is not None and (latest_received_at is None or received_at > latest_received_at):
                    latest_received_at = received_at
                if len(pending_results) >= self.WRITE_BATCH_SIZE:
                    flush_pending_results()
                if force_full and total_records and (provider_processed == total_records or provider_processed % 250 == 0):
                    flush_pending_results()
                    self._emit_progress(
                        progress_callback,
                        "provider_progress",
                        provider_id,
                        provider_processed,
                        total_records,
                        provider_changed,
                        provider_skipped,
                        provider_parsed,
                        force_full,
                )
                self._cooperative_yield(provider_processed)

            flush_pending_results()
            if latest_received_at is not None:
                self.repository.update_provider_checkpoint(
                    provider_id,
                    latest_received_at.isoformat(),
                    {
                        "lookbackDays": lookback_days,
                        "overlapHours": overlap_hours,
                        "forceFull": force_full,
                        "reparseAll": reparse_all,
                        "recordsFetched": provider_processed,
                        "candidateRecords": provider_ingested,
                        "recordsChanged": provider_changed,
                        "recordsSkippedUnchanged": provider_skipped,
                    },
                )
            ingested_count += provider_ingested
            changed_count += provider_changed
            skipped_unchanged_count += provider_skipped
            parsed_count += provider_parsed
            self._emit_progress(
                progress_callback,
                "provider_completed",
                provider_id,
                provider_processed,
                total_records,
                provider_changed,
                provider_skipped,
                provider_parsed,
                force_full,
            )

        self._emit_progress(
            progress_callback,
            "completed",
            "",
            ingested_count,
            ingested_count,
            changed_count,
            skipped_unchanged_count,
            parsed_count,
            force_full,
        )
        return {
            "ingestedCount": ingested_count,
            "changedRecords": changed_count,
            "skippedUnchanged": skipped_unchanged_count,
            "parsedFacts": parsed_count,
        }

    def _expense_relevant_facts(self, facts) -> list:
        """Keep only the expense-domain facts used by this app."""

        return [
            fact
            for fact in facts
            if fact.fact_type in {"bank_transaction", "expense"} and str(fact.parser_id).startswith("bank_alert_email")
        ]

    def _inspect_candidate(self, record: SourceRecord, *, rules_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Inspect one email record and keep only expense-domain facts."""

        inspection = self.parser_chain.inspect_record(record, rules_payload=rules_payload)
        return {
            "candidateMatched": bool(inspection.get("candidateMatched")),
            "attempts": list(inspection.get("attempts", [])),
            "facts": self._expense_relevant_facts(list(inspection.get("facts", []))),
        }

    def _emit_progress(
        self,
        progress_callback: Callable[[dict], None] | None,
        stage: str,
        provider_id: str,
        processed: int,
        total: int,
        changed: int,
        skipped: int,
        parsed: int,
        force_full: bool,
    ) -> None:
        """Emit one normalized progress event when a callback is provided."""

        if progress_callback is None:
            return
        progress_callback(
            {
                "stage": stage,
                "providerId": provider_id,
                "processed": processed,
                "total": total,
                "changedRecords": changed,
                "skippedUnchanged": skipped,
                "parsedFacts": parsed,
                "forceFull": force_full,
            }
        )

    def _cooperative_yield(self, processed: int) -> None:
        """Briefly yield long mailbox scans so the GUI thread keeps scrolling smoothly."""

        if processed > 0 and processed % self.COOPERATIVE_YIELD_EVERY == 0:
            time.sleep(self.COOPERATIVE_YIELD_SECONDS)

    def _parse_dt(self, value: str) -> datetime | None:
        """Parse one ISO timestamp when it is valid."""

        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _source_record_from_row(self, row: dict[str, Any]) -> SourceRecord:
        """Rebuild one stored source record into the parser input shape."""

        return SourceRecord(
            provider_id=str(row.get("providerId", "")),
            record_type=str(row.get("recordType", "")),
            external_id=str(row.get("externalId", "")),
            title=str(row.get("title", "")),
            sender=str(row.get("sender", "")),
            received_at=str(row.get("receivedAt", "")),
            payload=dict(row.get("payload", {})),
            content_hash=str(row.get("contentHash", "")),
        )

    def _log_bank_sender_samples(self, provider_id: str, records: list[SourceRecord]) -> None:
        """Log a sender summary for expense-like emails seen in one provider scan."""

        counts = Counter()
        for record in records:
            sender = str(record.sender or "").strip()
            if not self._bank_sender_bucket(sender):
                continue
            counts[sender] += 1
        if not counts:
            return
        self.logger.info("Expenses bank-like email sender summary provider_id=%s senders=%s", provider_id, dict(counts.most_common()))

    def _bank_sender_bucket(self, sender: str) -> str:
        """Classify one sender into a coarse bank-like bucket for logging only."""

        normalized = sender.lower()
        if "axis.bank.in" in normalized or "axisbank.com" in normalized:
            return "axis"
        if "alerts.sbi.bank.in" in normalized or "alerts.sbi.co.in" in normalized:
            return "sbi"
        if "citicorp.com" in normalized:
            return "citi"
        if "icici" in normalized or "hdfc" in normalized or "canara" in normalized or "cred" in normalized:
            return "bank"
        return ""
