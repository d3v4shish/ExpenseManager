from __future__ import annotations

from pathlib import Path
from typing import Any

from src.app.migrate import migrate_legacy_data
from src.expenses.account_config import (
    account_db_path,
    build_expense_mail_config_snapshot,
    load_email_accounts_config,
    load_local_thunderbird_config,
    migrate_legacy_expenses_db_if_needed,
    normalize_account_email,
    resolve_active_expenses_db_path,
    rewrite_local_thunderbird_config,
    rewrite_managed_thunderbird_provider,
)
from src.expenses.email.thunderbird_profile import discover_thunderbird_profile_accounts

EXPENSE_DATA_CARD_IDS = ("panel.expenses", "panel.expenses_tab")
EXPENSE_RUNTIME_CARD_IDS = ("panel.expenses", "panel.expenses_config", "panel.expenses_debug", "panel.expenses_tab")
EXPENSE_DEBUG_CARD_IDS = ("panel.expenses", "panel.expenses_debug", "panel.expenses_tab")


class ExpensesScreenApi:
    """Expose expense-domain actions to widgets without leaking repositories into UI code."""

    def __init__(
        self,
        *,
        expenses_service,
        expenses_repository,
        vendor_catalog_service,
        bank_rule_catalog=None,
        mail_ingestion_service=None,
        files=None,
    ) -> None:
        """Store the domain services used by the screen widgets."""

        self.expenses_service = expenses_service
        self.expenses_repository = expenses_repository
        self.vendor_catalog_service = vendor_catalog_service
        self.bank_rule_catalog = bank_rule_catalog
        self.mail_ingestion_service = mail_ingestion_service
        self.files = files
        self.window_api = None

    def attach_window(self, window_api) -> None:
        """Bind the active window API after the shell is created."""

        self.window_api = window_api

    def run_refresh(self) -> None:
        """Request the incremental refresh job from the app shell."""

        if self.window_api is not None:
            self.window_api.run_job("expenses.refresh")

    def run_rebuild(self) -> None:
        """Request the rebuild job from the app shell."""

        if self.window_api is not None:
            self.window_api.run_job("expenses.rebuild")

    def get_bank_rule_config_snapshot(self) -> dict[str, Any]:
        """Return the shipped, local, and effective bank-rule payloads."""

        catalog = self._require_bank_rule_catalog()
        defaults = catalog.load_defaults()
        overrides = catalog.load_overrides()
        effective = catalog.load_effective(overrides)
        return {
            "defaults": defaults,
            "overrides": overrides,
            "effective": effective,
            "defaultsText": catalog.payload_text(defaults),
            "overridesText": catalog.payload_text(overrides),
            "effectiveText": catalog.payload_text(effective),
        }

    def get_mail_debug_snapshot(self) -> dict[str, Any]:
        """Return the newest candidate-mail rows plus the active rule config."""

        return {
            "summary": self.expenses_repository.candidate_mail_debug_summary(provider_id="thunderbird_local"),
            "rows": self.expenses_repository.list_candidate_mail_debug_rows(),
            "scriptExtractors": self.script_extractor_status(),
            **self.get_bank_rule_config_snapshot(),
        }

    def script_extractor_status(self) -> dict[str, Any]:
        """Return status for trusted Python email extractor scripts."""

        service = self._require_mail_ingestion_service()
        status_fn = getattr(service, "script_extractor_status", None)
        if not callable(status_fn):
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
        return dict(status_fn())

    def list_candidate_mail_debug_rows(self) -> list[dict[str, Any]]:
        """Return the newest candidate-mail debug rows."""

        return self.expenses_repository.list_candidate_mail_debug_rows()

    def get_candidate_mail_detail(self, source_record_id: int) -> dict[str, Any] | None:
        """Return one stored candidate email with diagnostics and facts."""

        return self.expenses_repository.get_candidate_mail_detail(int(source_record_id))

    def validate_bank_rule_overrides(self, source_record_id: int, raw_text: str) -> dict[str, Any]:
        """Validate one raw override payload against one stored candidate email."""

        catalog = self._require_bank_rule_catalog()
        ingestion = self._require_mail_ingestion_service()
        overrides = catalog.parse_override_text(raw_text)
        effective = catalog.load_effective(overrides)
        detail = ingestion.diagnose_source_record(int(source_record_id), rules_payload=effective)
        return {
            "candidateMatched": bool(detail.get("candidateMatched")),
            "attempts": list(detail.get("attempts", [])),
            "facts": [self._serialize_fact(item) for item in detail.get("facts", [])],
            "effective": effective,
            "effectiveText": catalog.payload_text(effective),
        }

    def save_bank_rule_overrides(self, raw_text: str) -> dict[str, Any]:
        """Persist raw bank-rule overrides and reparse stored candidates."""

        catalog = self._require_bank_rule_catalog()
        overrides = catalog.parse_override_text(raw_text)
        saved = catalog.save_overrides(overrides)
        reparse_report = self.expenses_service.reparse_candidate_mails()
        self._reload_cards(*EXPENSE_DEBUG_CARD_IDS)
        effective = catalog.load_effective(saved)
        return {
            "overrides": saved,
            "overridesText": catalog.payload_text(saved),
            "effectiveText": catalog.payload_text(effective),
            "reparse": reparse_report,
        }

    def reparse_candidate_mails(self) -> dict[str, Any]:
        """Re-run parsing for every stored candidate email and refresh derived views."""

        report = self.expenses_service.reparse_candidate_mails()
        self._reload_cards(*EXPENSE_DEBUG_CARD_IDS)
        return report

    def get_expense_mail_config_snapshot(self) -> dict[str, Any]:
        """Return the current managed Thunderbird selection and active DB status."""

        snapshot = build_expense_mail_config_snapshot(self._require_files(), self.expenses_repository)
        validator = getattr(self, "validate_thunderbird_directory", None)
        if callable(validator):
            snapshot["sourceValidation"] = validator()
        snapshot["scriptExtractors"] = self.script_extractor_status()
        return snapshot

    def discover_thunderbird_accounts(self, profile_path: str) -> dict[str, Any]:
        """Return Thunderbird IMAP accounts discovered from one profile."""

        normalized_profile = str(profile_path or "").strip()
        snapshot = self.get_expense_mail_config_snapshot()
        result = discover_thunderbird_profile_accounts(normalized_profile)
        accounts: list[dict[str, Any]] = []
        for item in result.get("accounts", []):
            account = dict(item)
            account_email = normalize_account_email(str(account.get("email", "")).strip())
            account["email"] = account_email
            account["dbStatus"] = self.describe_expense_account_db(account_email)
            accounts.append(account)

        selected_email = snapshot.get("activeAccountEmail", "")
        selected_account = next((item for item in accounts if item.get("email") == selected_email), None)
        if selected_account is None and accounts:
            selected_account = accounts[0]
            selected_email = str(selected_account.get("email", ""))
        return {
            "profilePath": result.get("profilePath", normalized_profile),
            "accounts": accounts,
            "warnings": list(result.get("warnings", [])),
            "selectedAccountEmail": selected_email,
            "selectedMailboxPath": str(selected_account.get("defaultMailboxRel", "")) if selected_account else "",
        }

    def describe_expense_account_db(self, account_email: str) -> dict[str, Any]:
        """Return the cache/checkpoint summary for one account-specific expenses DB."""

        normalized_email = normalize_account_email(account_email)
        if not normalized_email:
            return {
                "dbPath": "",
                "exists": False,
                "dbSizeBytes": 0,
                "transactionCount": 0,
                "sourceRecordCount": 0,
                "hasCachedData": False,
                "lastCheckpointAt": "",
                "lastReceivedAt": "",
                "materializationVersion": "",
                "providerId": "thunderbird_local",
                "error": "",
            }
        return self.expenses_repository.describe_db_path(account_db_path(self._require_files(), normalized_email))

    def save_expense_mail_config(self, profile_path: str, account_email: str) -> dict[str, Any]:
        """Persist the selected Thunderbird account and switch the active expenses DB."""

        files = self._require_files()
        self._ensure_expenses_jobs_idle()

        normalized_profile = str(profile_path or "").strip()
        normalized_email = normalize_account_email(account_email)
        if not normalized_profile:
            raise ValueError("Select a Thunderbird profile directory first.")
        if not normalized_email:
            raise ValueError("Select one discovered Thunderbird account first.")

        discovery = self.discover_thunderbird_accounts(normalized_profile)
        selected_account = next(
            (item for item in discovery.get("accounts", []) if str(item.get("email", "")).strip() == normalized_email),
            None,
        )
        if selected_account is None:
            raise ValueError("The selected Thunderbird account is no longer available in that profile.")

        email_config = load_email_accounts_config(files)
        local_config = load_local_thunderbird_config(files)
        files.write_user(
            "email_accounts.json",
            rewrite_managed_thunderbird_provider(
                email_config,
                account_email=normalized_email,
                mailbox_path=str(selected_account.get("defaultMailboxRel", "")).strip(),
            ),
        )
        files.write_user_local(
            "thunderbird.json",
            rewrite_local_thunderbird_config(local_config, profile_path=normalized_profile),
        )
        switch_report = self.expenses_service.switch_expenses_database(account_db_path(files, normalized_email))
        return {
            "accountEmail": normalized_email,
            "profilePath": normalized_profile,
            "defaultMailboxRel": str(selected_account.get("defaultMailboxRel", "")).strip(),
            "defaultMailboxAbs": str(selected_account.get("defaultMailboxAbs", "")).strip(),
            **switch_report,
        }

    def import_legacy_data(self, source_root: str) -> dict[str, Any]:
        """Import legacy dashboard data after first paint, then rebind runtime state."""

        files = self._require_files()
        self._ensure_expenses_jobs_idle()

        report = migrate_legacy_data(Path(source_root), files)
        email_config = load_email_accounts_config(files)
        next_db_path = resolve_active_expenses_db_path(files, email_config)
        copied_account_db = migrate_legacy_expenses_db_if_needed(files, next_db_path)
        current_db_path = Path(getattr(self.expenses_repository, "db_path", next_db_path))
        runtime_changed = bool(report.copied_paths) or copied_account_db or current_db_path != next_db_path

        if runtime_changed:
            switch_report = self.expenses_service.switch_expenses_database(next_db_path)
        else:
            switch_report = {
                "dbPath": str(current_db_path),
                "hadExistingDb": bool(self.expenses_repository.describe_current_db().get("hasCachedData")),
                "dbStatus": self.expenses_repository.describe_current_db(),
            }

        return {
            "performed": bool(report.performed),
            "copiedPaths": list(report.copied_paths),
            "skippedPaths": list(report.skipped_paths),
            "notes": list(report.notes),
            "runtimeChanged": runtime_changed,
            "dbCopied": copied_account_db,
            "switchReport": switch_report,
            "snapshot": build_expense_mail_config_snapshot(files, self.expenses_repository),
        }

    def reconcile_vendor_mappings(self) -> dict[str, Any]:
        """Synchronize vendor mappings without touching mounted widgets."""

        return self.expenses_service.reconcile_vendor_mappings()

    def sync_vendor_mappings(self) -> dict[str, Any]:
        """Synchronize vendor mappings immediately and reload mounted cards."""

        report = self.reconcile_vendor_mappings()
        self._reload_cards(*EXPENSE_DATA_CARD_IDS)
        return report

    def toggle_ignored(self, transaction_key: str, ignored: bool) -> None:
        """Toggle the ignored state for one transaction and refresh the mounted cards."""

        self.set_transaction_ignored_state(transaction_key, ignored)
        self._reload_cards(*EXPENSE_DATA_CARD_IDS)

    def set_transaction_ignored_state(self, transaction_key: str, ignored: bool) -> dict[str, Any]:
        """Toggle one transaction row and rebuild derived expense views."""

        self.expenses_service.set_transaction_ignored(transaction_key, ignored)
        return {
            "transactionKey": str(transaction_key or "").strip(),
            "ignored": bool(ignored),
        }

    def get_analysis_snapshot(
        self,
        *,
        year: int | None = None,
        month: int | None = None,
        include_ignored: bool = True,
        search_text: str = "",
    ) -> dict[str, Any]:
        """Return the bounded DB-backed analysis payload for one filter selection."""

        return self.expenses_service.build_analysis_snapshot(
            year=year,
            month=month,
            include_ignored=include_ignored,
            search_text=search_text,
        )

    def list_transactions_page(
        self,
        *,
        year: int | None = None,
        month: int | None = None,
        include_ignored: bool = False,
        search_text: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one bounded transaction page from SQLite."""

        return self.expenses_repository.list_transactions_page(
            year=year,
            month=month,
            include_ignored=include_ignored,
            search_text=search_text,
            limit=limit,
            offset=offset,
        )

    def search_vendor_directory(self, query: str = "", *, limit: int = 40) -> list[dict[str, Any]]:
        """Return vendor autocomplete entries from SQLite."""

        return self.expenses_repository.search_vendor_directory(query, limit=limit)

    def list_vendor_transactions(
        self,
        *,
        alias_key: str = "",
        vendor_name: str = "",
        include_ignored: bool = False,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Return bounded transactions for one vendor detail request."""

        page = self.expenses_repository.list_transactions_page(
            include_ignored=include_ignored,
            search_text=alias_key or vendor_name,
            limit=limit,
        )
        rows = list(page.get("rows", []))
        clean_alias = str(alias_key or "").strip()
        clean_vendor = str(vendor_name or "").strip()
        if clean_alias:
            rows = [row for row in rows if str(row.get("aliasKey", row.get("vendorKey", ""))).strip() == clean_alias]
        elif clean_vendor:
            rows = [
                row
                for row in rows
                if str(row.get("canonicalVendor", row.get("vendor", ""))).strip() == clean_vendor
                or str(row.get("counterparty", "")).strip() == clean_vendor
            ]
        return [self._decorate_vendor_transaction(row) for row in rows]

    def list_ledger_groups(self, *, year: int, month: int, include_ignored: bool, search_text: str) -> list[dict[str, Any]]:
        """Return grouped ledger rows for the current filter selection."""

        return self.expenses_repository.list_ledger_groups(
            year=year,
            month=month,
            include_ignored=include_ignored,
            search_text=search_text,
        )

    def list_ledger_group_rows(
        self,
        *,
        year: int,
        month: int,
        group_key: str,
        include_ignored: bool,
        search_text: str,
    ) -> list[dict[str, Any]]:
        """Return ledger rows for one expanded group."""

        return self.expenses_repository.list_ledger_group_rows(
            year=year,
            month=month,
            group_key=group_key,
            include_ignored=include_ignored,
            search_text=search_text,
        )

    def find_vendor(self, value: str) -> dict[str, Any] | None:
        """Return one vendor record that matches the provided value."""

        if self.vendor_catalog_service is None:
            return None
        return self.vendor_catalog_service.find_vendor(value)

    def validate_thunderbird_directory(self) -> dict[str, Any]:
        """Return whether the current Thunderbird config can resolve mailbox files."""

        if self.mail_ingestion_service is None:
            return {
                "valid": False,
                "message": "Thunderbird mail ingestion is not available in this runtime.",
                "mailboxPaths": [],
            }
        validator = getattr(self.mail_ingestion_service, "validate_thunderbird_directory", None)
        if not callable(validator):
            return {
                "valid": False,
                "message": "Thunderbird directory validation is not available in this runtime.",
                "mailboxPaths": [],
            }
        return validator()

    @staticmethod
    def _decorate_vendor_transaction(row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        vendor = (
            str(payload.get("canonicalVendor", "")).strip()
            or str(payload.get("resolvedVendor", "")).strip()
            or str(payload.get("counterparty", "")).strip()
            or str(payload.get("rawCounterparty", "")).strip()
            or "Unknown"
        )
        alias_key = (
            str(payload.get("aliasKey", "")).strip()
            or str(payload.get("vendorKey", "")).strip()
            or "".join(ch.lower() for ch in vendor if ch.isalnum() or ch in {"@", ".", "_", "-"})
            or "unknown"
        )
        payload["vendor"] = str(payload.get("vendor", "")).strip() or vendor
        payload["vendorKey"] = str(payload.get("vendorKey", "")).strip() or alias_key
        return payload

    def search_vendors(self, query: str) -> list[dict[str, Any]]:
        """Return vendors that match one free-text query."""

        return self.vendor_catalog_service.search_vendors(query)

    def list_vendors(self, query: str = "") -> list[dict[str, Any]]:
        """Return the vendor catalog, optionally filtered by query."""

        return self.vendor_catalog_service.list_vendors(query)

    def get_vendor(self, vendor_id: int) -> dict[str, Any] | None:
        """Return one vendor by id when it exists."""

        return self.vendor_catalog_service.get_vendor(vendor_id)

    def create_vendor(
        self,
        canonical_name: str,
        *,
        canonical_alias: str = "",
        nickname: str = "",
        notes: str = "",
        categories: list[str] | None = None,
    ) -> int:
        """Create one vendor and return the new id."""

        return self.vendor_catalog_service.create_vendor(
            canonical_name,
            canonical_alias=canonical_alias,
            nickname=nickname,
            notes=notes,
            categories=categories or [],
        )

    def update_vendor(
        self,
        vendor_id: int,
        *,
        canonical_name: str,
        canonical_alias: str,
        nickname: str = "",
        notes: str = "",
        categories: list[str] | None = None,
    ) -> None:
        """Update one vendor and its category links."""

        self.vendor_catalog_service.update_vendor(
            vendor_id,
            canonical_name=canonical_name,
            canonical_alias=canonical_alias,
            nickname=nickname,
            notes=notes,
            categories=categories or [],
        )

    def delete_vendor(self, vendor_id: int) -> None:
        """Delete one vendor from the catalog."""

        self.vendor_catalog_service.delete_vendor(vendor_id)

    def add_merge_member(self, vendor_id: int, raw_name: str, *, source: str = "manual") -> None:
        """Attach one raw-name merge row to a vendor."""

        self.vendor_catalog_service.add_merge_member(vendor_id, raw_name, source=source)

    def remove_merge_member(self, vendor_id: int, raw_name: str) -> None:
        """Remove one raw-name merge row from a vendor."""

        self.vendor_catalog_service.remove_merge_member(vendor_id, raw_name)

    def merge_vendors(self, source_vendor_id: int, target_vendor_id: int, kept_alias: str) -> dict[str, Any]:
        """Soft-merge one vendor into another and reconcile transaction mappings."""

        return self.expenses_service.merge_vendors(source_vendor_id, target_vendor_id, kept_alias)

    def unmerge_vendor(self, source_vendor_id: int) -> dict[str, Any]:
        """Restore one previously merged vendor and reconcile transaction mappings."""

        return self.expenses_service.unmerge_vendor(source_vendor_id)

    def merge_raw_alias(self, vendor_id: int, raw_name: str, *, source: str = "manual") -> dict[str, Any]:
        """Attach one raw alias to one canonical vendor and reconcile immediately."""

        return self.expenses_service.merge_raw_alias(vendor_id, raw_name, source=source)

    def unmerge_raw_alias(self, vendor_id: int, raw_name: str) -> dict[str, Any]:
        """Detach one raw alias from one canonical vendor and reconcile immediately."""

        return self.expenses_service.unmerge_raw_alias(vendor_id, raw_name)

    def reindex_vendor_search(self) -> None:
        """Rebuild vendor catalog indexes used by autocomplete and lookup."""

        self.vendor_catalog_service.reindex_vendor_search()

    def save_vendor_rule(
        self,
        *,
        vendor_id: int | None = None,
        canonical_name: str,
        canonical_alias: str = "",
        nickname: str = "",
        notes: str = "",
        categories: list[str] | None = None,
    ) -> int:
        """Create or update one vendor rule using the current vendor form payload."""

        if vendor_id:
            self.update_vendor(
                vendor_id,
                canonical_name=canonical_name,
                canonical_alias=canonical_alias or canonical_name,
                nickname=nickname,
                notes=notes,
                categories=categories or [],
            )
            return vendor_id
        return self.create_vendor(
            canonical_name,
            canonical_alias=canonical_alias or canonical_name,
            nickname=nickname,
            notes=notes,
            categories=categories or [],
        )

    def save_vendor_rule_and_reconcile(
        self,
        *,
        vendor_id: int | None = None,
        canonical_name: str,
        canonical_alias: str = "",
        nickname: str = "",
        notes: str = "",
        categories: list[str] | None = None,
        seed_raw_name: str = "",
    ) -> dict[str, Any]:
        """Persist one vendor rule and rebuild all derived vendor mappings."""

        saved_vendor_id = self.save_vendor_rule(
            vendor_id=vendor_id,
            canonical_name=canonical_name,
            canonical_alias=canonical_alias or canonical_name,
            nickname=nickname,
            notes=notes,
            categories=categories or [],
        )
        seed_raw_name = str(seed_raw_name or "").strip()
        if seed_raw_name and self.find_vendor(seed_raw_name) is None:
            self.add_merge_member(saved_vendor_id, seed_raw_name, source="manual")
        vendor = self.get_vendor(saved_vendor_id) or {}
        reconcile = self.reconcile_vendor_mappings()
        return {
            "action": "created" if not vendor_id else "updated",
            "vendorId": int(vendor.get("vendorId", saved_vendor_id) or saved_vendor_id),
            "canonicalVendor": str(vendor.get("canonicalVendor", canonical_name)).strip() or canonical_name,
            "canonicalAlias": str(vendor.get("canonicalAlias", canonical_alias or canonical_name)).strip() or (canonical_alias or canonical_name),
            "aliasKey": str(vendor.get("aliasKey", "")).strip(),
            "reconcile": reconcile,
        }

    def delete_vendor_and_reconcile(self, vendor_id: int) -> dict[str, Any]:
        """Delete one vendor rule and rebuild all derived vendor mappings."""

        existing = self.get_vendor(vendor_id) or {}
        self.delete_vendor(vendor_id)
        reconcile = self.reconcile_vendor_mappings()
        return {
            "vendorId": int(vendor_id or 0),
            "deletedVendor": str(existing.get("canonicalVendor", "")).strip(),
            "deletedAlias": str(existing.get("canonicalAlias", "")).strip(),
            "reconcile": reconcile,
        }

    def add_vendor_category_and_reconcile(self, vendor_id: int, category: str) -> dict[str, Any]:
        """Attach one shared category tag to the alias cluster and reconcile rows."""

        clean_category = str(category or "").strip()
        if not clean_category:
            raise ValueError("Category is required.")
        self.vendor_catalog_service.add_vendor_category(int(vendor_id), clean_category)
        vendor = self.get_vendor(int(vendor_id)) or {}
        reconcile = self.reconcile_vendor_mappings()
        return {
            "vendorId": int(vendor.get("vendorId", vendor_id) or vendor_id),
            "canonicalVendor": str(vendor.get("canonicalVendor", "")).strip(),
            "canonicalAlias": str(vendor.get("canonicalAlias", "")).strip(),
            "aliasKey": str(vendor.get("aliasKey", "")).strip(),
            "category": clean_category,
            "reconcile": reconcile,
        }

    def remove_vendor_category_and_reconcile(self, vendor_id: int, category: str) -> dict[str, Any]:
        """Remove one shared category tag from the alias cluster and reconcile rows."""

        clean_category = str(category or "").strip()
        if not clean_category:
            raise ValueError("Category is required.")
        self.vendor_catalog_service.remove_vendor_category(int(vendor_id), clean_category)
        vendor = self.get_vendor(int(vendor_id)) or {}
        reconcile = self.reconcile_vendor_mappings()
        return {
            "vendorId": int(vendor.get("vendorId", vendor_id) or vendor_id),
            "canonicalVendor": str(vendor.get("canonicalVendor", "")).strip(),
            "canonicalAlias": str(vendor.get("canonicalAlias", "")).strip(),
            "aliasKey": str(vendor.get("aliasKey", "")).strip(),
            "category": clean_category,
            "reconcile": reconcile,
        }

    def accept_merge_suggestion(self, detail: dict[str, Any], raw_name: str, *, source: str = "fuzzy_accepted") -> dict[str, Any]:
        """Create a missing vendor rule when needed, then merge one raw alias."""

        vendor_id = self._ensure_catalog_vendor_id_for_detail(detail, raw_name=raw_name)
        if vendor_id <= 0:
            raise ValueError("Could not create or resolve vendor before merge.")
        return self.merge_raw_alias(vendor_id, raw_name, source=source)

    def _ensure_catalog_vendor_id_for_detail(self, detail: dict[str, Any], *, raw_name: str = "") -> int:
        """Resolve one catalog vendor id from detail, creating a seed record when missing."""

        if not isinstance(detail, dict):
            return 0

        for candidate in (
            str(detail.get("canonicalVendor", "")).strip(),
            str(detail.get("canonicalAlias", "")).strip(),
            str(detail.get("vendor", "")).strip(),
        ):
            if not candidate:
                continue
            record = self.find_vendor(candidate)
            if record:
                return int(record.get("vendorId", 0) or 0)

        canonical_name = str(detail.get("canonicalVendor", "") or detail.get("vendor", "")).strip()
        canonical_alias = str(detail.get("canonicalAlias", "") or canonical_name).strip()
        if not canonical_name:
            return 0

        lookup_candidates: list[str] = [
            canonical_name,
            canonical_alias,
            str(detail.get("vendor", "")).strip(),
            str(raw_name or "").strip(),
        ]
        transactions = detail.get("transactions", [])
        if isinstance(transactions, list):
            for row in transactions[:5]:
                lookup_candidates.append(str(row.get("rawCounterparty", "")).strip())

        def resolve_existing_vendor_id() -> int:
            seen_candidates: set[str] = set()
            for candidate in lookup_candidates:
                key = candidate.lower()
                if not candidate or key in seen_candidates:
                    continue
                seen_candidates.add(key)
                record = self.find_vendor(candidate)
                if record:
                    return int(record.get("vendorId", 0) or 0)
            return 0

        resolved_vendor_id = resolve_existing_vendor_id()
        if resolved_vendor_id > 0:
            return resolved_vendor_id

        categories = list(detail.get("categories", [])) if isinstance(detail.get("categories", []), list) else []
        try:
            vendor_id = int(
                self.create_vendor(
                    canonical_name,
                    canonical_alias=canonical_alias or canonical_name,
                    categories=categories,
                )
                or 0
            )
        except Exception:
            resolved_vendor_id = resolve_existing_vendor_id()
            if resolved_vendor_id > 0:
                return resolved_vendor_id
            raise
        if vendor_id <= 0:
            return resolve_existing_vendor_id()

        seed_candidates: list[str] = []
        if isinstance(transactions, list):
            for row in transactions[:3]:
                seed_raw = str(row.get("rawCounterparty", "")).strip()
                if seed_raw:
                    seed_candidates.append(seed_raw)
        fallback_seed = str(detail.get("vendor", "")).strip()
        if fallback_seed:
            seed_candidates.append(fallback_seed)

        seen: set[str] = set()
        for seed in seed_candidates:
            key = seed.lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                self.add_merge_member(vendor_id, seed, source="manual_seed")
            except Exception:
                continue
        return vendor_id

    def _require_files(self):
        """Return the runtime file helper required by config-oriented actions."""

        if self.files is None:
            raise RuntimeError("Runtime files are not configured on the screen API.")
        return self.files

    def _require_bank_rule_catalog(self):
        """Return the shared bank-rule catalog required by the debug pane."""

        if self.bank_rule_catalog is None:
            raise RuntimeError("Bank rule catalog is not configured on the screen API.")
        return self.bank_rule_catalog

    def _require_mail_ingestion_service(self):
        """Return the mail-ingestion service required by rule validation."""

        if self.mail_ingestion_service is None:
            raise RuntimeError("Mail ingestion service is not configured on the screen API.")
        return self.mail_ingestion_service

    def _ensure_expenses_jobs_idle(self) -> None:
        """Prevent config switches while refresh or rebuild jobs are running."""

        if self.window_api is None:
            return
        active_jobs = set(getattr(self.window_api, "active_jobs", set()))
        if active_jobs.intersection({"expenses.refresh", "expenses.rebuild"}):
            raise RuntimeError("Wait for the current expenses sync job to finish before switching accounts.")

    def _reload_cards(self, *card_ids: str) -> None:
        """Reload one explicit set of mounted cards when the window API is present."""

        if self.window_api is None:
            return
        callback = getattr(self.window_api, "reload_cards", None)
        if callable(callback):
            callback(card_ids)
            return
        self.window_api.reload_all_cards()

    def _serialize_fact(self, fact) -> dict[str, Any]:
        """Convert one parsed-fact dataclass into a UI-friendly dict."""

        return {
            "factType": str(getattr(fact, "fact_type", "")),
            "parserId": str(getattr(fact, "parser_id", "")),
            "providerId": str(getattr(fact, "source_provider_id", "")),
            "externalId": str(getattr(fact, "source_external_id", "")),
            "payload": dict(getattr(fact, "payload", {})),
            "confidence": float(getattr(fact, "confidence", 0.0) or 0.0),
        }
