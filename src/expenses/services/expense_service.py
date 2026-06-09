from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Callable

from src.expenses.repositories.expenses_repository import ExpensesRepository
from src.expenses.services.mail_ingestion import ExpensesMailIngestionService
from src.expenses.services.vendor_catalog import VendorCatalogService


class ExpensesService:
    MATERIALIZATION_VERSION = "2026-04-18-expenses-candidate-mail-v2"

    def __init__(
        self,
        *,
        expenses_repository: ExpensesRepository,
        mail_ingestion_service: ExpensesMailIngestionService,
        state_store,
        vendor_catalog_service: VendorCatalogService | None = None,
    ) -> None:
        self.state_store = state_store
        self.expenses_repository = expenses_repository
        self.mail_ingestion_service = mail_ingestion_service
        self.vendor_catalog_service = vendor_catalog_service
        self.progress_listeners: list[Callable[[dict[str, Any]], None]] = []
        self.logger = logging.getLogger(self.__class__.__name__)

    def refresh_expenses(self) -> None:
        if self.expenses_repository.count_source_records() <= 0:
            self._write_views(force_rebuild=False)
            return
        self._materialize_expenses(force_rebuild=False)

    def rebuild_expenses(self) -> None:
        self._materialize_expenses(force_rebuild=True)

    def refresh_views_from_repository(self) -> None:
        self._write_views(force_rebuild=False)

    def reparse_candidate_mails(self) -> dict[str, Any]:
        """Re-run parsing for every stored candidate email and refresh derived views."""

        report = self.mail_ingestion_service.reparse_stored_email_records(lambda _record: True)
        transactions = self._materialize_transactions(self.expenses_repository.list_facts("bank_transaction"))
        self.expenses_repository.replace_transactions(transactions)
        self.expenses_repository.set_metadata("expenses_materialization_version", self.MATERIALIZATION_VERSION)
        self._write_views(force_rebuild=False)
        return {
            **report,
            "transactionCount": len(transactions),
            "updatedAt": datetime.now().astimezone().isoformat(),
        }

    def add_progress_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Register one listener for live expense-job progress updates."""

        if listener not in self.progress_listeners:
            self.progress_listeners.append(listener)

    def switch_expenses_database(self, db_path: Path) -> dict[str, Any]:
        """Switch the active expenses DB, reset job state, and rewrite derived views."""

        next_db_path = Path(db_path)
        before_status = self.expenses_repository.describe_db_path(next_db_path)
        self.expenses_repository.switch_db_path(next_db_path)
        idle_progress = {
            "running": False,
            "label": "Ready",
            "processed": 0,
            "total": 0,
            "percent": 0,
            "providerId": "",
        }
        self._write_rebuild_progress(idle_progress)
        self._write_views(force_rebuild=False)
        return {
            "dbPath": str(next_db_path),
            "hadExistingDb": bool(before_status.get("hasCachedData")),
            "dbStatus": self.expenses_repository.describe_current_db(),
        }

    def set_transaction_ignored(self, transaction_key: str, ignored: bool) -> None:
        self.expenses_repository.set_ignored(transaction_key, ignored)
        self._write_views(force_rebuild=False)

    def reconcile_vendor_mappings(self) -> dict[str, Any]:
        """Re-apply vendor catalog matching to every stored transaction immediately."""

        rows = self.expenses_repository.list_transactions(include_ignored=True)
        if not rows:
            self._write_views(force_rebuild=False)
            return {
                "changedRows": 0,
                "totalRows": 0,
                "updatedAt": datetime.now().astimezone().isoformat(),
            }

        reconciled_rows: list[dict[str, Any]] = []
        changed_rows = 0
        for row in rows:
            updated = self._reconcile_vendor_row(row)
            if self._vendor_mapping_changed(row, updated):
                changed_rows += 1
            reconciled_rows.append(updated)

        if changed_rows > 0:
            self.expenses_repository.upsert_transactions(reconciled_rows, prune_missing=False)
        self._write_views(force_rebuild=False)
        return {
            "changedRows": changed_rows,
            "totalRows": len(rows),
            "updatedAt": datetime.now().astimezone().isoformat(),
        }

    def merge_vendors(self, source_vendor_id: int, target_vendor_id: int, kept_alias: str) -> dict[str, Any]:
        """Soft-merge one vendor into another and rebuild all alias-key aggregates."""

        if self.vendor_catalog_service is None:
            raise RuntimeError("Vendor catalog service is not configured.")
        merge_report = self.vendor_catalog_service.merge_vendors(source_vendor_id, target_vendor_id, kept_alias)
        reconcile_report = self.reconcile_vendor_mappings()
        return {**merge_report, "reconcile": reconcile_report}

    def unmerge_vendor(self, source_vendor_id: int) -> dict[str, Any]:
        """Restore one merged vendor and rebuild all alias-key aggregates."""

        if self.vendor_catalog_service is None:
            raise RuntimeError("Vendor catalog service is not configured.")
        merge_report = self.vendor_catalog_service.unmerge_vendor(source_vendor_id)
        reconcile_report = self.reconcile_vendor_mappings()
        return {**merge_report, "reconcile": reconcile_report}

    def merge_raw_alias(self, vendor_id: int, raw_name: str, *, source: str = "manual") -> dict[str, Any]:
        """Attach one raw alias to one vendor and rebuild alias-key aggregates immediately."""

        if self.vendor_catalog_service is None:
            raise RuntimeError("Vendor catalog service is not configured.")
        self.vendor_catalog_service.add_merge_member(vendor_id, raw_name, source=source)
        vendor = self.vendor_catalog_service.get_vendor(vendor_id) or {}
        reconcile_report = self.reconcile_vendor_mappings()
        return {
            "vendorId": int(vendor.get("vendorId", vendor_id) or vendor_id),
            "canonicalVendor": str(vendor.get("canonicalVendor", "")),
            "canonicalAlias": str(vendor.get("canonicalAlias", "")),
            "aliasKey": str(vendor.get("aliasKey", "")),
            "rawName": raw_name,
            "reconcile": reconcile_report,
        }

    def unmerge_raw_alias(self, vendor_id: int, raw_name: str) -> dict[str, Any]:
        """Detach one raw alias from one vendor and rebuild alias-key aggregates immediately."""

        if self.vendor_catalog_service is None:
            raise RuntimeError("Vendor catalog service is not configured.")
        self.vendor_catalog_service.remove_merge_member(vendor_id, raw_name)
        vendor = self.vendor_catalog_service.get_vendor(vendor_id) or {}
        reconcile_report = self.reconcile_vendor_mappings()
        return {
            "vendorId": int(vendor.get("vendorId", vendor_id) or vendor_id),
            "canonicalVendor": str(vendor.get("canonicalVendor", "")),
            "canonicalAlias": str(vendor.get("canonicalAlias", "")),
            "aliasKey": str(vendor.get("aliasKey", "")),
            "rawName": raw_name,
            "reconcile": reconcile_report,
        }

    def _materialize_expenses(self, *, force_rebuild: bool) -> None:
        if force_rebuild:
            self._write_rebuild_progress(self._scan_progress(force_full=True))
            ingest_report = self.mail_ingestion_service.rebuild_email_records(progress_callback=self._handle_rebuild_progress)
        else:
            ingest_report = self.mail_ingestion_service.refresh_email_records(progress_callback=self._handle_rebuild_progress)

        current_version = self.expenses_repository.get_metadata("expenses_materialization_version")
        changed_records = int(ingest_report.get("changedRecords", 0) or 0)
        needs_materialize = force_rebuild or changed_records > 0 or current_version != self.MATERIALIZATION_VERSION
        if not needs_materialize:
            self._write_rebuild_progress(self._idle_progress())
            return

        self._write_rebuild_progress(self._analytics_progress(force_full=force_rebuild, ingest_report=ingest_report))
        transactions = self._materialize_transactions(self.expenses_repository.list_facts("bank_transaction"))
        prune_missing = force_rebuild or current_version != self.MATERIALIZATION_VERSION
        self.expenses_repository.upsert_transactions(transactions, prune_missing=prune_missing)
        self.expenses_repository.set_metadata("expenses_materialization_version", self.MATERIALIZATION_VERSION)
        self._write_views(force_rebuild=force_rebuild)
        self._write_rebuild_progress(self._idle_progress())

    def _materialize_transactions(self, bank_facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for fact in bank_facts:
            payload   = fact.get("payload", {})
            stamp     = self._transaction_dt(payload) or self._transaction_dt(fact)
            amount    = self._amount(payload)
            direction = str(payload.get("direction", "")).strip().lower()
            if stamp is None or amount is None or direction not in {"debit", "credit"}:
                continue
            raw_counterparty = str(payload.get("counterparty", "")).strip()
            vendor_info      = self._resolve_vendor(raw_counterparty, direction, payload)
            result.append(
                {
                    "transactionKey": self._transaction_key(fact),
                    "providerId": str(fact.get("providerId", "")).strip(),
                    "externalId": str(fact.get("externalId", "")).strip(),
                    "parserId": str(fact.get("parserId", "")).strip(),
                    "bankName": str(payload.get("bankName", "")).strip(),
                    "direction": direction,
                    "amount": amount,
                    "currency": str(payload.get("currency", "INR")).strip() or "INR",
                    "transactionId": str(payload.get("transactionId", "")).strip(),
                    "counterparty": vendor_info["resolvedVendor"],
                    "rawCounterparty": raw_counterparty,
                    "resolvedVendor": vendor_info["resolvedVendor"],
                    "canonicalVendor": vendor_info["canonicalVendor"],
                    "canonicalAlias": vendor_info["canonicalAlias"],
                    "aliasKey": vendor_info["aliasKey"],
                    "category": vendor_info["category"],
                    "subcategory": vendor_info["subcategory"],
                    "vendorMatchSource": vendor_info["vendorMatchSource"],
                    "accountSuffix": str(payload.get("accountSuffix", "")).strip(),
                    "timestamp": str(payload.get("timestamp", "")).strip() or stamp.isoformat(),
                    "year": stamp.year,
                    "month": stamp.month,
                    "title": str(fact.get("title", "")).strip(),
                    "sender": str(fact.get("sender", "")).strip(),
                }
            )
        result.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
        return result

    def _resolve_vendor(self, raw_counterparty: str, direction: str, payload: dict[str, Any]) -> dict[str, str]:
        fallback_vendor = raw_counterparty or str(payload.get("bankName", "Unknown")).strip() or "Unknown"
        fallback_display, fallback_cluster = self._fallback_vendor_identity(fallback_vendor)
        fallback_alias_key = fallback_cluster.split(":", 1)[1] if ":" in fallback_cluster else self._normalize_alias_key(fallback_display)
        fallback        = {
            "resolvedVendor": fallback_display,
            "canonicalVendor": fallback_display,
            "canonicalAlias": fallback_display,
            "aliasKey": fallback_alias_key or "unknown",
            "category": "Income" if direction == "credit" else "Uncategorized",
            "subcategory": "",
            "vendorMatchSource": "fallback",
        }
        if self.vendor_catalog_service is None:
            return fallback
        resolved = self.vendor_catalog_service.resolve_vendor(raw_counterparty)
        if not resolved:
            return fallback
        canonical  = str(resolved.get("canonicalVendor", "")).strip() or fallback_vendor
        alias      = str(resolved.get("canonicalAlias", "")).strip() or canonical
        nickname   = str(resolved.get("nickname", "")).strip() or canonical
        alias_key  = self._normalize_alias_key(alias or canonical) or fallback_alias_key or "unknown"
        categories = list(resolved.get("categories", [])) if isinstance(resolved.get("categories", []), list) else []
        return {
            "resolvedVendor": nickname or alias,
            "canonicalVendor": canonical,
            "canonicalAlias": alias,
            "aliasKey": alias_key,
            "category": str(categories[0] if categories else "Uncategorized").strip() or "Uncategorized",
            "subcategory": str(resolved.get("subcategory", "")).strip(),
            "vendorMatchSource": "vendor_catalog",
        }

    def _reconcile_vendor_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Return one transaction row with refreshed vendor mapping fields."""

        updated = dict(row)
        direction = str(updated.get("direction", "")).strip().lower()
        raw_counterparty = str(updated.get("rawCounterparty", "")).strip()
        if not raw_counterparty:
            for candidate in (
                str(updated.get("counterparty", "")).strip(),
                str(updated.get("canonicalVendor", "")).strip(),
                str(updated.get("resolvedVendor", "")).strip(),
                str(updated.get("bankName", "")).strip(),
            ):
                if candidate:
                    raw_counterparty = candidate
                    break
        vendor_info = self._resolve_vendor(raw_counterparty, direction, updated)
        if raw_counterparty:
            updated["rawCounterparty"] = raw_counterparty
        updated["counterparty"] = vendor_info["resolvedVendor"]
        updated["resolvedVendor"] = vendor_info["resolvedVendor"]
        updated["canonicalVendor"] = vendor_info["canonicalVendor"]
        updated["canonicalAlias"] = vendor_info["canonicalAlias"]
        updated["aliasKey"] = vendor_info["aliasKey"]
        updated["vendorMatchSource"] = vendor_info["vendorMatchSource"]
        updated["category"] = vendor_info["category"]
        updated["subcategory"] = vendor_info["subcategory"]
        return updated

    def _vendor_mapping_changed(self, before: dict[str, Any], after: dict[str, Any]) -> bool:
        """Return whether vendor-resolved fields changed for one transaction row."""

        watched_fields = (
            "counterparty",
            "resolvedVendor",
            "canonicalVendor",
            "canonicalAlias",
            "aliasKey",
            "vendorMatchSource",
            "category",
            "subcategory",
            "rawCounterparty",
        )
        for field in watched_fields:
            if str(before.get(field, "")) != str(after.get(field, "")):
                return True
        return False

    def _vendor_identity(self, row: dict[str, Any]) -> tuple[str, str]:
        canonical_vendor = str(row.get("canonicalVendor", "")).strip() or str(row.get("resolvedVendor", "")).strip()
        alias_key = str(row.get("aliasKey", "")).strip()
        if not alias_key:
            alias_source = str(row.get("canonicalAlias", "")).strip() or canonical_vendor
            alias_key = self._normalize_alias_key(alias_source)
        if canonical_vendor and alias_key:
            return canonical_vendor, alias_key

        raw_value = (
            canonical_vendor
            or str(row.get("counterparty", "")).strip()
            or str(row.get("rawCounterparty", "")).strip()
            or str(row.get("bankName", "Unknown")).strip()
            or "Unknown"
        )
        fallback_vendor, fallback_key = self._fallback_vendor_identity(raw_value)
        fallback_alias_key = fallback_key.split(":", 1)[1] if ":" in fallback_key else self._normalize_alias_key(fallback_vendor)
        return fallback_vendor, fallback_alias_key or "unknown"

    def _fallback_vendor_identity(self, raw_value: str) -> tuple[str, str]:
        tokens     = self._vendor_tokens(raw_value)
        normalized = self._normalize_vendor_token(raw_value)
        if not normalized:
            return "Unknown", "fallback:unknown"

        alias_map = {
            "cred": "Cred",
            "credclub": "Cred",
            "credccbp": "Cred",
            "bigbasket": "BigBasket",
            "bbnow": "BigBasket",
            "blinkit": "Blinkit",
            "swiggy": "Swiggy",
            "zomato": "Zomato",
        }
        for prefix, display in alias_map.items():
            if normalized.startswith(prefix):
                return display, f"fallback:{prefix}"

        if tokens:
            display = " ".join(token.upper() if token.isupper() else token.capitalize() for token in tokens)
            return display, f"fallback:{normalized}"
        return raw_value.strip() or "Unknown", f"fallback:{normalized}"

    def _normalize_vendor_token(self, raw_value: str) -> str:
        return "".join(self._vendor_tokens(raw_value))

    def _normalize_alias_key(self, raw_value: str) -> str:
        """Normalize one canonical alias value for cluster identity and lookup."""

        return "".join(ch.lower() for ch in str(raw_value or "").strip() if ch.isalnum() or ch in {"@", ".", "_", "-"})

    def _vendor_tokens(self, raw_value: str) -> list[str]:
        value = raw_value.strip().lower()
        if not value:
            return []
        if "@" in value:
            value = value.split("@", 1)[0]
        value     = re.sub(r"[^a-z0-9]+", " ", value)
        removable = {
            "upi",
            "bank",
            "axis",
            "axisbank",
            "icici",
            "yesbank",
            "sbi",
            "ybl",
            "ibl",
            "okaxis",
            "oksbi",
            "okhdfcbank",
            "okicici",
            "okyesbank",
        }
        return [token for token in value.split() if token and token not in removable]

    def _write_views(self, *, force_rebuild: bool) -> None:
        rows = self.expenses_repository.list_transactions(include_ignored=True)
        self.state_store.save("expenses.json", self._build_overview_payload(rows, force_rebuild=force_rebuild))
        self.state_store.save("expenses_tab.json", self._build_tab_payload(rows, force_rebuild=force_rebuild))

    def build_analysis_snapshot(
        self,
        *,
        year: int | None = None,
        month: int | None = None,
        include_ignored: bool = True,
        search_text: str = "",
    ) -> dict[str, Any]:
        """Return the bounded analysis payload used after filter changes."""

        rows = self.expenses_repository.list_transactions(include_ignored=True)
        return self._build_tab_payload(
            rows,
            force_rebuild=False,
            selected_year=year,
            selected_month=month,
            include_ignored=include_ignored,
            search_text=search_text,
        )

    def _build_overview_payload(self, rows: list[dict[str, Any]], *, force_rebuild: bool) -> dict[str, Any]:
        now             = datetime.now().astimezone()
        visible_rows    = [row for row in rows if not row.get("ignored")]
        visible_debits  = self._rows_by_direction(visible_rows, "debit")
        visible_credits = self._rows_by_direction(visible_rows, "credit")
        metrics         = self._period_metrics(visible_debits, now)
        vendors         = self._build_vendor_summary(visible_debits)
        return {
            "summary": {key: metrics[key] for key in ("today", "yesterday", "week", "month", "year")},
            "topVendor": {"vendor": vendors[0]["vendor"] if vendors else "None", "amount": float(vendors[0]["amount"]) if vendors else 0.0},
            "recent": [self._serialize_transaction(row, {}) for row in visible_rows[:5]],
            "highlights": {"creditThisMonth": self._period_metrics(visible_credits, now)["month"], "netThisMonth": self._net_flow(visible_debits, visible_credits)["net"]},
            "meta": {"lastRebuiltAt": now.isoformat(), "forceRebuild": force_rebuild, "ignoredCount": len([row for row in rows if row.get("ignored")]), "rebuildProgress": self._current_background_state()},
        }

    def _build_tab_payload(
        self,
        rows: list[dict[str, Any]],
        *,
        force_rebuild: bool,
        selected_year: int | None = None,
        selected_month: int | None = None,
        include_ignored: bool = True,
        search_text: str = "",
    ) -> dict[str, Any]:
        now              = datetime.now().astimezone()
        visible_rows     = list(rows) if include_ignored else [row for row in rows if not row.get("ignored")]
        visible_debits   = self._rows_by_direction([row for row in visible_rows if not row.get("ignored")], "debit")
        visible_credits  = self._rows_by_direction([row for row in visible_rows if not row.get("ignored")], "credit")
        years            = sorted({self._transaction_dt(row).year for row in rows if self._transaction_dt(row)}, reverse=True) or [now.year]
        requested_year   = int(selected_year or 0)
        requested_month  = int(selected_month or 0)
        default_year     = requested_year if requested_year in years else now.year if now.year in years else years[0]
        default_month    = requested_month if 0 <= requested_month <= 12 else now.month
        recurring        = self._build_recurring_patterns(visible_debits)
        recurring_lookup = {f"{item['vendorKey']}|{item['accountKey']}": item for item in recurring}
        selected_rows    = self._selected_rows(visible_rows, default_year, default_month)
        if search_text.strip():
            selected_rows = [row for row in selected_rows if self._row_matches_search(row, search_text)]
        selected_debits  = self._rows_by_direction(selected_rows, "debit")
        selected_credits = self._rows_by_direction(selected_rows, "credit")
        vendor_summary   = self._build_vendor_summary(selected_debits)
        selected_summary = self._build_vendor_summary(selected_debits)
        selected_vendor  = self._selected_top_vendor(selected_summary)
        page_size        = 200
        serialized_rows  = selected_rows[:page_size]
        all_time_summary = self._build_vendor_summary(visible_debits)
        return {
            "filters": {"availableYears": years, "availableMonths": [{"value": 0, "label": "All Months"}] + [{"value": month, "label": datetime(now.year, month, 1).strftime("%b")} for month in range(1, 13)], "defaultYear": default_year, "defaultMonth": default_month, "pageSize": page_size},
            "accounts": self._account_values(rows),
            "transactions": [self._serialize_transaction(row, recurring_lookup) for row in serialized_rows],
            "yearlySeries": [self._build_year_series(year, visible_debits) for year in years],
            "creditYearMonths": self._build_year_series(default_year, visible_credits).get("months", []),
            "vendorSummary": vendor_summary,
            "vendorDirectory": vendor_summary,
            "vendorAutocomplete": [],
            "recurringPatterns": recurring,
            "selectedRange": {"year": default_year, "month": default_month},
            "selectedYearMonths": self._build_year_series(default_year, visible_debits).get("months", []),
            "selectedMonthDaily": self._month_daily_series(selected_debits, default_year, default_month),
            "selectedMonthWeekly": self._month_weekly_series(selected_debits, default_year, default_month),
            "ledgerGroups": [],
            "selectedTopVendor": selected_vendor,
            "selectedInsights": self._selected_insights(selected_debits, selected_summary, recurring, default_year, default_month),
            "allTimeInsights": self._all_time_insights(visible_debits, all_time_summary, recurring),
            "selectedCreditSummary": self._credit_summary(selected_credits),
            "selectedNetFlow": self._net_flow(selected_debits, selected_credits),
            "selectedAverages": self._averages(selected_debits, selected_credits, default_year),
            "allTimeAverages": self._averages(visible_debits, visible_credits, 0),
            "categorySummary": self._category_summary(visible_debits),
            "selectedVendorDetail": {},
            "backgroundState": {"expenses": self._current_background_state()},
            "meta": {"visibleTransactionCount": len([row for row in rows if not row.get("ignored")]), "ignoredTransactionCount": len([row for row in rows if row.get("ignored")]), "loadedTransactionCount": len(serialized_rows), "analysisDataMode": "db-backed-bootstrap", "lastRebuiltAt": now.isoformat(), "forceRebuild": force_rebuild, "materializationVersion": self.MATERIALIZATION_VERSION, "rebuildProgress": self._current_background_state()},
        }

    def _row_matches_search(self, row: dict[str, Any], search_text: str) -> bool:
        """Return whether one serialized transaction matches the ledger search text."""

        query = search_text.strip().lower()
        if not query:
            return True
        fields = (
            "counterparty",
            "rawCounterparty",
            "resolvedVendor",
            "canonicalVendor",
            "canonicalAlias",
            "aliasKey",
            "transactionId",
            "title",
            "sender",
            "bankName",
            "accountSuffix",
        )
        return any(query in str(row.get(field, "")).lower() for field in fields)

    def _account_values(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        accounts = {}
        for row in rows:
            accounts.setdefault(self._account_key(row), {"accountKey": self._account_key(row), "label": self._account_label(row), "bankName": str(row.get("bankName", "")).strip() or "Unknown bank", "accountSuffix": str(row.get("accountSuffix", "")).strip() or "Unknown"})
        return sorted(accounts.values(), key=lambda item: item["label"])

    def _build_year_series(self, year: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
        totals = {month: 0.0 for month in range(1, 13)}
        for row in rows:
            stamp, amount = self._transaction_dt(row), self._amount(row)
            if stamp is not None and amount is not None and stamp.year == year:
                totals[stamp.month] += amount
        return {"year": year, "months": [{"label": datetime(year, month, 1).strftime("%b"), "month": month, "amount": totals[month]} for month in range(1, 13)]}

    def _build_vendor_summary(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets = {}
        for row in rows:
            vendor, vendor_key = self._vendor_identity(row)
            amount = self._amount(row) or 0.0
            bucket = buckets.setdefault(
                vendor_key,
                {
                    "vendor": vendor,
                    "vendorKey": vendor_key,
                    "canonicalVendor": str(row.get("canonicalVendor", vendor)).strip() or vendor,
                    "canonicalAlias": str(row.get("canonicalAlias", vendor)).strip() or vendor,
                    "amount": 0.0,
                    "count": 0,
                    "category": str(row.get("category", "Uncategorized")).strip() or "Uncategorized",
                },
            )
            bucket["amount"] += amount
            bucket["count"] += 1
        result = [
            {
                "vendor": item["vendor"],
                "vendorKey": item["vendorKey"],
                "canonicalVendor": item["canonicalVendor"],
                "canonicalAlias": item["canonicalAlias"],
                "amount": float(item["amount"]),
                "count": int(item["count"]),
                "averageAmount": float(item["amount"] / max(item["count"], 1)),
                "category": item["category"],
            }
            for item in buckets.values()
        ]
        result.sort(key=lambda item: (item["amount"], item["count"], item["vendor"]), reverse=True)
        return result

    def _build_vendor_autocomplete(self, vendor_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build canonical + alias autocomplete entries with canonical-first ordering."""

        result: list[dict[str, Any]] = []
        seen_entries: set[tuple[str, str]] = set()
        canonical_order: list[str] = []
        canonical_items: dict[str, dict[str, Any]] = {}

        def _alias_key(canonical_alias: str, canonical_vendor: str) -> str:
            normalized = self._normalize_alias_key(canonical_alias) or self._normalize_alias_key(canonical_vendor)
            return normalized or "unknown"

        def _upsert_canonical(
            vendor: str,
            canonical_vendor: str,
            canonical_alias: str,
            vendor_key: str,
            category: str,
        ) -> None:
            key = vendor_key.strip() or _alias_key(canonical_alias, canonical_vendor or vendor)
            if key not in canonical_items:
                canonical_items[key] = {
                    "vendor": vendor.strip() or canonical_vendor.strip() or "Unknown",
                    "canonicalVendor": canonical_vendor.strip() or vendor.strip() or "Unknown",
                    "canonicalAlias": canonical_alias.strip() or canonical_vendor.strip() or vendor.strip() or "Unknown",
                    "vendorKey": key,
                    "category": category.strip() or "Uncategorized",
                }
                canonical_order.append(key)
                return
            current = canonical_items[key]
            if current.get("vendor", "Unknown") == "Unknown" and vendor.strip():
                current["vendor"] = vendor.strip()
            if not current.get("canonicalVendor") and canonical_vendor.strip():
                current["canonicalVendor"] = canonical_vendor.strip()
            if (not current.get("canonicalAlias")) and canonical_alias.strip():
                current["canonicalAlias"] = canonical_alias.strip()
            if current.get("category", "Uncategorized") == "Uncategorized" and category.strip():
                current["category"] = category.strip()

        def _append_entry(
            *,
            label: str,
            vendor: str,
            vendor_key: str,
            canonical_vendor: str,
            category: str,
            entry_type: str,
            alias_value: str = "",
        ) -> None:
            clean_label = label.strip()
            if not clean_label:
                return
            normalized_key = vendor_key.strip().lower()
            marker = (clean_label.lower(), normalized_key)
            if marker in seen_entries:
                return
            seen_entries.add(marker)
            clean_vendor = vendor.strip() or canonical_vendor.strip() or "Unknown"
            clean_canonical = canonical_vendor.strip() or clean_vendor
            clean_category = category.strip() or "Uncategorized"
            clean_alias = alias_value.strip()
            display = clean_label if entry_type == "canonical" else f"[ALIAS] {clean_label} -> {clean_vendor}"
            match_parts = [clean_label, clean_vendor, clean_canonical, clean_category]
            if clean_alias:
                match_parts.append(clean_alias)
            result.append(
                {
                    "entryType": entry_type,
                    "displayLabel": display,
                    "matchText": " ".join(part for part in match_parts if part).lower(),
                    "aliasValue": clean_alias if entry_type == "alias" else "",
                    "vendor": clean_vendor,
                    "vendorKey": vendor_key.strip(),
                    "canonicalVendor": clean_canonical,
                    "canonicalAlias": str(canonical_items.get(vendor_key.strip(), {}).get("canonicalAlias", clean_canonical)),
                    "category": clean_category,
                    "searchText": " ".join(part for part in match_parts if part).lower(),
                }
            )

        for item in vendor_summary:
            vendor = str(item.get("vendor", "Unknown")).strip() or "Unknown"
            canonical_vendor = str(item.get("canonicalVendor", "")).strip() or vendor
            canonical_alias = str(item.get("canonicalAlias", "")).strip() or canonical_vendor
            vendor_key = str(item.get("vendorKey", "")).strip() or _alias_key(canonical_alias, canonical_vendor)
            category = str(item.get("category", "Uncategorized")).strip() or "Uncategorized"
            _upsert_canonical(canonical_vendor or vendor, canonical_vendor, canonical_alias, vendor_key, category)

        catalog_rows = self.vendor_catalog_service.list_vendors() if self.vendor_catalog_service is not None else []
        for item in catalog_rows:
            canonical_vendor = str(item.get("canonicalVendor", "")).strip()
            if not canonical_vendor:
                continue
            canonical_alias = str(item.get("canonicalAlias", "")).strip()
            categories = [str(value).strip() for value in item.get("categories", []) if str(value).strip()]
            category = categories[0] if categories else "Uncategorized"
            vendor_display = canonical_vendor
            vendor_key = str(item.get("aliasKey", "")).strip() or _alias_key(canonical_alias, canonical_vendor)
            _upsert_canonical(vendor_display, canonical_vendor, canonical_alias or canonical_vendor, vendor_key, category)

        for key in canonical_order:
            item = canonical_items.get(key)
            if item is None:
                continue
            _append_entry(
                label=str(item.get("canonicalVendor", "Unknown")),
                vendor=str(item.get("canonicalVendor", "Unknown")),
                vendor_key=str(item.get("vendorKey", "")),
                canonical_vendor=str(item.get("canonicalVendor", "")),
                category=str(item.get("category", "Uncategorized")),
                entry_type="canonical",
            )

        for item in catalog_rows:
            canonical_vendor = str(item.get("canonicalVendor", "")).strip()
            if not canonical_vendor:
                continue
            canonical_alias = str(item.get("canonicalAlias", "")).strip() or canonical_vendor
            vendor_key = str(item.get("aliasKey", "")).strip() or _alias_key(canonical_alias, canonical_vendor)
            canonical = canonical_items.get(vendor_key)
            if canonical is None:
                continue
            vendor = str(canonical.get("canonicalVendor", canonical_vendor)).strip() or canonical_vendor
            category = str(canonical.get("category", "Uncategorized")).strip() or "Uncategorized"
            alias_candidates: list[str] = []
            canonical_alias = str(item.get("canonicalAlias", "")).strip()
            nickname = str(item.get("nickname", "")).strip()
            if canonical_alias:
                alias_candidates.append(canonical_alias)
            if nickname:
                alias_candidates.append(nickname)
            for merge_row in item.get("mergeMembers", []):
                raw_name = str(merge_row.get("rawName", "")).strip()
                if raw_name:
                    alias_candidates.append(raw_name)
            for merged_vendor in item.get("mergedVendors", []):
                merged_name = str(merged_vendor.get("canonicalVendor", "")).strip()
                merged_alias = str(merged_vendor.get("canonicalAlias", "")).strip()
                merged_nickname = str(merged_vendor.get("nickname", "")).strip()
                if merged_name:
                    alias_candidates.append(merged_name)
                if merged_alias:
                    alias_candidates.append(merged_alias)
                if merged_nickname:
                    alias_candidates.append(merged_nickname)
                for merge_row in merged_vendor.get("mergeMembers", []):
                    raw_name = str(merge_row.get("rawName", "")).strip()
                    if raw_name:
                        alias_candidates.append(raw_name)
            for alias in alias_candidates:
                _append_entry(
                    label=alias,
                    vendor=vendor,
                    vendor_key=vendor_key,
                    canonical_vendor=canonical_vendor,
                    category=category,
                    entry_type="alias",
                    alias_value=alias,
                )
        return result

    def _build_ledger_groups(self, rows: list[dict[str, Any]], year: int, month: int) -> list[dict[str, Any]]:
        selected                            = self._selected_rows(rows, year, month)
        buckets : dict[str, dict[str, Any]] = {}
        for row in selected:
            stamp = self._transaction_dt(row)
            if stamp is None:
                continue
            group_key = stamp.date().isoformat()
            bucket    = buckets.setdefault(
                group_key,
                {
                    "groupKey": group_key,
                    "groupLabel": stamp.strftime("%d %b"),
                    "rowCount": 0,
                    "debitTotal": 0.0,
                    "latestTimestamp": row.get("timestamp", ""),
                },
            )
            bucket["rowCount"] += 1
            if str(row.get("direction", "")).strip().lower() == "debit":
                bucket["debitTotal"] += float(row.get("amount", 0.0) or 0.0)
            if str(row.get("timestamp", "")) > str(bucket.get("latestTimestamp", "")):
                bucket["latestTimestamp"] = str(row.get("timestamp", ""))
        result = list(buckets.values())
        result.sort(key=lambda item: str(item.get("latestTimestamp", "")), reverse=True)
        return result

    def _build_recurring_patterns(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped = defaultdict(list)
        for row in rows:
            stamp, amount = self._transaction_dt(row), self._amount(row)
            if stamp is not None and amount is not None:
                vendor, vendor_key = self._vendor_identity(row)
                grouped[(vendor_key, self._account_key(row))].append((vendor, stamp, amount))
        result = []
        for (vendor_key, account_key), values in grouped.items():
            values.sort(key=lambda item: item[1])
            if len(values) < 3:
                continue
            gaps         = [(values[idx][1] - values[idx - 1][1]).days for idx in range(1, len(values))]
            cadence_days = max(1, int(median(gaps)))
            avg_amount   = sum(amount for _, _, amount in values) / len(values)
            result.append({"vendor": values[0][0], "vendorKey": vendor_key, "accountKey": account_key, "averageAmount": float(avg_amount), "totalAmount": float(sum(amount for _, _, amount in values)), "transactionCount": len(values), "cadence": self._classify_cadence(cadence_days), "confidence": 0.75, "lastSeen": values[-1][1].isoformat(), "nextExpected": (values[-1][1] + timedelta(days=cadence_days)).isoformat()})
        result.sort(key=lambda item: (item["transactionCount"], item["totalAmount"]), reverse=True)
        return result[:30]

    def _selected_rows(self, rows: list[dict[str, Any]], year: int, month: int) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            stamp = self._transaction_dt(row)
            if stamp is None or stamp.year != year:
                continue
            if month and stamp.month != month:
                continue
            result.append(row)
        return result

    def _month_daily_series(self, rows: list[dict[str, Any]], year: int, month: int) -> list[dict[str, Any]]:
        if month <= 0:
            return []
        days_in_month = 31 if month not in {4, 6, 9, 11} else 30
        if month == 2:
            days_in_month = 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
        buckets = {day: {"label": f"{day:02d}", "amount": 0.0, "count": 0} for day in range(1, days_in_month + 1)}
        for row in rows:
            stamp, amount = self._transaction_dt(row), self._amount(row)
            if stamp is not None and amount is not None:
                buckets[stamp.day]["amount"] += amount
                buckets[stamp.day]["count"] += 1
        return [{"day": day, **payload} for day, payload in buckets.items()]

    def _month_weekly_series(self, rows: list[dict[str, Any]], year: int, month: int) -> list[dict[str, Any]]:
        if month <= 0:
            return []
        buckets = {week: {"label": f"W{week}", "amount": 0.0, "count": 0} for week in range(1, 6)}
        for row in rows:
            stamp, amount = self._transaction_dt(row), self._amount(row)
            if stamp is not None and amount is not None:
                week = min(5, ((stamp.day - 1) // 7) + 1)
                buckets[week]["amount"] += amount
                buckets[week]["count"] += 1
        return [{"week": week, **payload} for week, payload in buckets.items()]

    def _selected_top_vendor(self, vendor_summary: list[dict[str, Any]]) -> dict[str, Any]:
        if not vendor_summary:
            return {"vendor": "None", "vendorKey": "", "amount": 0.0, "count": 0}
        top = vendor_summary[0]
        return {"vendor": str(top.get("vendor", "None")), "vendorKey": str(top.get("vendorKey", "")), "amount": float(top.get("amount", 0.0) or 0.0), "count": int(top.get("count", 0) or 0)}

    def _selected_insights(self, rows: list[dict[str, Any]], vendor_summary: list[dict[str, Any]], recurring: list[dict[str, Any]], year: int, month: int) -> dict[str, Any]:
        daily             = self._month_daily_series(rows, year, month)
        weekly            = self._month_weekly_series(rows, year, month)
        top_by_count      = sorted(vendor_summary, key=lambda item: (item["count"], item["amount"]), reverse=True)
        recurring_vendors = {item.get("vendorKey", "") for item in recurring}
        return {"topVendorByAmount": self._selected_top_vendor(vendor_summary), "topVendorByCount": self._selected_top_vendor(top_by_count), "highestSpendDay": max(daily, key=lambda item: float(item.get("amount", 0.0) or 0.0), default={"label": "-", "amount": 0.0}), "highestSpendWeek": max(weekly, key=lambda item: float(item.get("amount", 0.0) or 0.0), default={"label": "-", "amount": 0.0}), "largestTransaction": self._largest_transaction(rows), "recurringActiveCount": len({self._vendor_key(row) for row in rows if self._vendor_key(row) in recurring_vendors})}

    def _all_time_insights(self, rows: list[dict[str, Any]], vendor_summary: list[dict[str, Any]], recurring: list[dict[str, Any]]) -> dict[str, Any]:
        top_by_count = sorted(vendor_summary, key=lambda item: (item["count"], item["amount"]), reverse=True)
        return {"topVendorByAmount": self._selected_top_vendor(vendor_summary), "topVendorByCount": self._selected_top_vendor(top_by_count), "largestTransaction": self._largest_transaction(rows), "recurringBurden": {"patternCount": len(recurring), "monthlyLikeAmount": float(sum(item.get("averageAmount", 0.0) or 0.0 for item in recurring if item.get("cadence") == "Monthly recurring"))}}

    def _largest_transaction(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"vendor": "None", "amount": 0.0}
        item = max(rows, key=lambda row: float(row.get("amount", 0.0) or 0.0))
        return {"vendor": self._vendor_name(item), "vendorKey": self._vendor_key(item), "amount": float(item.get("amount", 0.0) or 0.0)}

    def _credit_summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {"creditTotal": float(sum(self._amount(row) or 0.0 for row in rows)), "creditCount": len(rows), "topCreditSource": self._selected_top_vendor(self._build_vendor_summary(rows))}

    def _net_flow(self, debit_rows: list[dict[str, Any]], credit_rows: list[dict[str, Any]]) -> dict[str, Any]:
        debit_total  = float(sum(self._amount(row) or 0.0 for row in debit_rows))
        credit_total = float(sum(self._amount(row) or 0.0 for row in credit_rows))
        return {"debit": debit_total, "credit": credit_total, "net": credit_total - debit_total}

    def _averages(self, debit_rows: list[dict[str, Any]], credit_rows: list[dict[str, Any]], year: int) -> dict[str, Any]:
        debit_total   = float(sum(self._amount(row) or 0.0 for row in debit_rows))
        credit_total  = float(sum(self._amount(row) or 0.0 for row in credit_rows))
        debit_amounts = [self._amount(row) or 0.0 for row in debit_rows if self._amount(row) is not None]
        active_days   = len({self._transaction_dt(row).date().isoformat() for row in debit_rows if self._transaction_dt(row) is not None})
        active_weeks  = len({self._transaction_dt(row).strftime("%Y-W%W") for row in debit_rows if self._transaction_dt(row) is not None})
        active_months = len({(self._transaction_dt(row).year, self._transaction_dt(row).month) for row in debit_rows if self._transaction_dt(row) is not None and (year <= 0 or self._transaction_dt(row).year == year)})
        return {"averageTransactionAmount": float(debit_total / max(len(debit_rows), 1)), "medianTransactionAmount": float(median(debit_amounts)) if debit_amounts else 0.0, "averageSpendPerActiveDay": float(debit_total / max(active_days, 1)), "averageSpendPerActiveWeek": float(debit_total / max(active_weeks, 1)), "averageMonthlySpend": float(debit_total / max(active_months, 1)), "averageCreditAmount": float(credit_total / max(len(credit_rows), 1))}

    def _category_summary(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets = defaultdict(lambda: {"amount": 0.0, "count": 0})
        for row in rows:
            amount = self._amount(row)
            if amount is None:
                continue
            label = str(row.get("category", "Uncategorized")).strip() or "Uncategorized"
            buckets[label]["amount"] += amount
            buckets[label]["count"] += 1
        result = [{"label": label, "amount": float(payload["amount"]), "count": int(payload["count"])} for label, payload in buckets.items()]
        result.sort(key=lambda item: item["amount"], reverse=True)
        return result

    def _build_vendor_detail(self, vendor: str, selected_debits: list[dict[str, Any]], all_rows: list[dict[str, Any]], recurring: list[dict[str, Any]], vendor_key: str = "") -> dict[str, Any]:
        if not vendor or vendor == "None":
            return {}
        vendor_rows = [row for row in all_rows if (self._vendor_key(row) == vendor_key if vendor_key else self._vendor_name(row) == vendor)]
        debit_rows  = self._rows_by_direction(vendor_rows, "debit")
        credit_rows = self._rows_by_direction(vendor_rows, "credit")
        if not vendor_rows:
            return {}
        row = vendor_rows[0]
        resolved_vendor_key = vendor_key or self._vendor_key(row)
        vendor_patterns     = [dict(item) for item in recurring if str(item.get("vendorKey", "")).strip() == resolved_vendor_key]
        recurrence          = self._summarize_vendor_recurrence(vendor_patterns)
        return {
            "vendor": vendor,
            "vendorKey": resolved_vendor_key,
            "canonicalVendor": str(row.get("canonicalVendor", vendor)).strip() or vendor,
            "canonicalAlias": str(row.get("canonicalAlias", row.get("canonicalVendor", vendor))).strip() or vendor,
            "aliasKey": str(row.get("aliasKey", resolved_vendor_key)).strip() or resolved_vendor_key,
            "category": str(row.get("category", "Uncategorized")).strip() or "Uncategorized",
            "selectedRangeDebit": float(sum(self._amount(item) or 0.0 for item in selected_debits if (self._vendor_key(item) == vendor_key if vendor_key else self._vendor_name(item) == vendor))),
            "allTimeDebit": float(sum(self._amount(item) or 0.0 for item in debit_rows)),
            "allTimeCredit": float(sum(self._amount(item) or 0.0 for item in credit_rows)),
            "net": float(sum(self._amount(item) or 0.0 for item in credit_rows) - sum(self._amount(item) or 0.0 for item in debit_rows)),
            "transactionCount": len(vendor_rows),
            "averageAmount": float(sum(self._amount(item) or 0.0 for item in vendor_rows) / max(len(vendor_rows), 1)),
            "recurrence": recurrence,
            "recurrencePatterns": vendor_patterns,
        }

    def _summarize_vendor_recurrence(self, patterns: list[dict[str, Any]]) -> dict[str, Any]:
        if not patterns:
            return {}
        preferred = sorted(
            patterns,
            key=lambda item: (
                str(item.get("cadence", "")) != "Monthly recurring",
                -float(item.get("averageAmount", 0.0) or 0.0),
                -int(item.get("transactionCount", 0) or 0),
            ),
        )[0]
        return {
            "cadence": str(preferred.get("cadence", "")),
            "confidence": float(preferred.get("confidence", 0.0) or 0.0),
            "averageAmount": float(preferred.get("averageAmount", 0.0) or 0.0),
            "transactionCount": int(preferred.get("transactionCount", 0) or 0),
            "lastSeen": str(preferred.get("lastSeen", "")),
            "nextExpected": str(preferred.get("nextExpected", "")),
        }

    def _period_metrics(self, rows: list[dict[str, Any]], now: datetime) -> dict[str, float]:
        today           = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday       = today - timedelta(days=1)
        week            = today - timedelta(days=today.weekday())
        prev_week       = week - timedelta(days=7)
        month           = today.replace(day=1)
        prev_month      = datetime(month.year - 1, 12, 1, tzinfo=month.tzinfo) if month.month == 1 else datetime(month.year, month.month - 1, 1, tzinfo=month.tzinfo)
        year            = today.replace(month=1, day=1)
        prev_year       = year.replace(year=year.year - 1)
        result          = {"today": 0.0, "yesterday": 0.0, "week": 0.0, "month": 0.0, "year": 0.0, "weekVsPrevious": 0.0, "monthVsPrevious": 0.0, "yearVsPrevious": 0.0}
        prev_week_total = prev_month_total = prev_year_total = 0.0
        for row in rows:
            stamp, amount = self._transaction_dt(row), self._amount(row)
            if stamp is None or amount is None:
                continue
            if stamp >= today:
                result["today"] += amount
            elif yesterday <= stamp < today:
                result["yesterday"] += amount
            if stamp >= week:
                result["week"] += amount
            elif prev_week <= stamp < week:
                prev_week_total += amount
            if stamp >= month:
                result["month"] += amount
            elif prev_month <= stamp < month:
                prev_month_total += amount
            if stamp >= year:
                result["year"] += amount
            elif prev_year <= stamp < year:
                prev_year_total += amount
        result["weekVsPrevious"] = result["week"] - prev_week_total
        result["monthVsPrevious"] = result["month"] - prev_month_total
        result["yearVsPrevious"] = result["year"] - prev_year_total
        return result

    def _rows_by_direction(self, rows: list[dict[str, Any]], direction: str) -> list[dict[str, Any]]:
        return [row for row in rows if str(row.get("direction", "")).strip().lower() == direction]

    def _classify_cadence(self, days: int) -> str:
        if days <= 2:
            return "Daily recurring"
        if days <= 9:
            return "Weekly recurring"
        if days <= 40:
            return "Monthly recurring"
        if days <= 120:
            return "Quarterly recurring"
        return "Irregular recurring"

    def _serialize_transaction(self, row: dict[str, Any], recurring_lookup: dict[str, dict[str, Any]]) -> dict[str, Any]:
        stamp      = self._transaction_dt(row)
        vendor_key = self._vendor_key(row)
        recurring  = recurring_lookup.get(f"{vendor_key}|{self._account_key(row)}", {})
        return {"transactionKey": row.get("transactionKey", ""), "accountKey": self._account_key(row), "accountLabel": self._account_label(row), "bankName": row.get("bankName", ""), "accountSuffix": row.get("accountSuffix", ""), "vendor": self._vendor_name(row), "vendorKey": vendor_key, "resolvedVendor": row.get("resolvedVendor", ""), "canonicalVendor": row.get("canonicalVendor", ""), "canonicalAlias": row.get("canonicalAlias", ""), "aliasKey": row.get("aliasKey", vendor_key), "category": row.get("category", "Uncategorized"), "subcategory": row.get("subcategory", ""), "direction": row.get("direction", ""), "amount": self._amount(row) or 0.0, "currency": row.get("currency", "INR"), "timestamp": row.get("timestamp", ""), "year": stamp.year if stamp else row.get("year"), "month": stamp.month if stamp else row.get("month"), "monthLabel": stamp.strftime("%b %Y") if stamp else "", "dayLabel": stamp.strftime("%d %b %Y %H:%M") if stamp else row.get("timestamp", ""), "dayOnlyLabel": stamp.strftime("%d %b") if stamp else "", "timeLabel": stamp.strftime("%H:%M") if stamp else "", "transactionId": row.get("transactionId", ""), "title": row.get("title", ""), "sender": row.get("sender", ""), "ignored": bool(row.get("ignored")), "ignoredAt": row.get("ignoredAt", ""), "pattern": recurring.get("cadence", ""), "patternConfidence": recurring.get("confidence", 0.0), "reference": row.get("transactionId", "") or row.get("title", ""), "rawCounterparty": row.get("rawCounterparty", ""), "vendorMatchSource": row.get("vendorMatchSource", ""), "stateLabel": "Ignored" if row.get("ignored") else "Active"}

    def _handle_rebuild_progress(self, payload: dict) -> None:
        total       = int(payload.get("total", 0) or 0)
        processed   = int(payload.get("processed", 0) or 0)
        percent     = 0 if total <= 0 else max(0, min(100, int((processed / max(total, 1)) * 100)))
        stage       = str(payload.get("stage", "")).strip()
        provider_id = str(payload.get("providerId", "")).strip()
        force_full  = bool(payload.get("forceFull"))
        if stage == "completed":
            return
        label       = "Refreshing expenses"
        if stage in {"provider_started", "provider_progress"} and provider_id:
            label = f"Scanning {provider_id}"
        elif stage == "provider_completed" and provider_id:
            label = f"Completed {provider_id}"
        block_ui = force_full
        self._write_rebuild_progress({"running": stage != "completed", "label": label, "processed": processed, "total": total, "percent": percent, "providerId": provider_id, "changedRecords": int(payload.get("changedRecords", 0) or 0), "parsedFacts": int(payload.get("parsedFacts", 0) or 0), "skippedUnchanged": int(payload.get("skippedUnchanged", 0) or 0), "blockUi": block_ui, "forceFull": force_full})

    def _write_rebuild_progress(self, progress: dict) -> None:
        overview         = self.state_store.load("expenses.json")
        tab              = self.state_store.load("expenses_tab.json")
        overview_payload = dict(overview) if isinstance(overview, dict) else {}
        tab_payload      = dict(tab) if isinstance(tab, dict) else {}
        overview_meta    = overview_payload.get("meta", {}) if isinstance(overview_payload.get("meta", {}), dict) else {}
        tab_meta         = tab_payload.get("meta", {}) if isinstance(tab_payload.get("meta", {}), dict) else {}
        overview_payload["meta"] = {**overview_meta, "rebuildProgress": progress}
        tab_payload["meta"] = {**tab_meta, "rebuildProgress": progress}
        tab_payload["backgroundState"] = {"expenses": progress}
        self.state_store.save("expenses.json", overview_payload)
        self.state_store.save("expenses_tab.json", tab_payload)
        self._notify_progress_listeners(progress)

    def _scan_progress(self, *, force_full: bool) -> dict[str, Any]:
        """Return the scan-stage status payload used before and during mailbox reads."""

        return {
            "running": True,
            "label": "Scanning expenses mail",
            "processed": 0,
            "total": 0,
            "percent": 0,
            "providerId": "",
            "changedRecords": 0,
            "parsedFacts": 0,
            "skippedUnchanged": 0,
            "blockUi": force_full,
            "forceFull": force_full,
        }

    def _analytics_progress(self, *, force_full: bool, ingest_report: dict[str, Any]) -> dict[str, Any]:
        """Return the materialization-stage status payload used before analytics recompute."""

        processed = int(ingest_report.get("ingestedCount", 0) or 0)
        changed = int(ingest_report.get("changedRecords", 0) or 0)
        skipped = int(ingest_report.get("skippedUnchanged", 0) or 0)
        parsed = int(ingest_report.get("parsedFacts", 0) or 0)
        return {
            "running": True,
            "label": "Computing analytics",
            "processed": processed,
            "total": processed,
            "percent": 100 if processed > 0 else 0,
            "providerId": "",
            "changedRecords": changed,
            "parsedFacts": parsed,
            "skippedUnchanged": skipped,
            "blockUi": True,
            "forceFull": force_full,
        }

    def _idle_progress(self) -> dict[str, Any]:
        """Return the steady-state status payload once sync work has completed."""

        return {
            "running": False,
            "label": "Ready",
            "processed": 0,
            "total": 0,
            "percent": 0,
            "providerId": "",
            "changedRecords": 0,
            "parsedFacts": 0,
            "skippedUnchanged": 0,
            "blockUi": False,
            "forceFull": False,
        }

    def _current_background_state(self) -> dict[str, Any]:
        try:
            tab = self.state_store.load("expenses_tab.json")
        except Exception:  # noqa: BLE001
            tab = {}
        if isinstance(tab, dict):
            meta = tab.get("meta", {})
            if isinstance(meta, dict) and isinstance(meta.get("rebuildProgress"), dict):
                return dict(meta.get("rebuildProgress"))
        return {"running": False, "label": "Ready", "processed": 0, "total": 0, "percent": 0, "providerId": ""}

    def _notify_progress_listeners(self, progress: dict[str, Any]) -> None:
        """Push one normalized progress payload to live listeners."""

        for listener in list(self.progress_listeners):
            try:
                listener(dict(progress))
            except Exception:  # noqa: BLE001
                self.logger.exception("Expenses progress listener failed progress=%s", progress)

    def _transaction_key(self, fact: dict[str, Any]) -> str:
        payload = fact.get("payload", {}) if isinstance(fact.get("payload", {}), dict) else {}
        bank_name = self._key_token(payload.get("bankName"))
        account_suffix = self._key_token(payload.get("accountSuffix"))
        direction = self._key_token(payload.get("direction")) or "unknown"
        transaction_id = self._key_token(payload.get("transactionId"))
        if transaction_id:
            return "|".join(
                [
                    "txn",
                    bank_name or "unknown_bank",
                    account_suffix or "unknown_account",
                    direction,
                    transaction_id,
                ]
            )

        amount = self._amount(payload)
        stamp = self._transaction_dt(payload) or self._transaction_dt(fact)
        counterparty = self._key_token(payload.get("counterparty"))
        amount_token = f"{amount:.2f}" if amount is not None else "unknown_amount"
        timestamp_token = stamp.isoformat() if stamp is not None else self._key_token(payload.get("timestamp")) or "unknown_timestamp"
        return "|".join(
            [
                "fallback",
                bank_name or "unknown_bank",
                account_suffix or "unknown_account",
                direction,
                amount_token,
                timestamp_token,
                counterparty or "unknown_counterparty",
            ]
        )

    def _account_key(self, row: dict[str, Any]) -> str:
        return f"{(str(row.get('bankName', '')).strip() or 'Unknown bank').lower()}:{str(row.get('accountSuffix', '')).strip() or 'unknown'}"

    def _key_token(self, value: Any) -> str:
        """Normalize one transaction-key segment for dedup."""

        return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")

    def _account_label(self, row: dict[str, Any]) -> str:
        return f"{str(row.get('bankName', '')).strip() or 'Unknown bank'} A/C x{str(row.get('accountSuffix', '')).strip() or 'Unknown'}"

    def _vendor_name(self, row: dict[str, Any]) -> str:
        return self._vendor_identity(row)[0]

    def _vendor_key(self, row: dict[str, Any]) -> str:
        return self._vendor_identity(row)[1]

    def _parse_dt(self, value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _transaction_dt(self, row: dict[str, Any]) -> datetime | None:
        timestamp = str(row.get("timestamp", "")).strip()
        if timestamp:
            parsed = self._parse_dt(timestamp)
            if parsed is not None:
                return parsed
        return self._parse_dt(str(row.get("receivedAt", "")).strip())

    def _amount(self, row: dict[str, Any]) -> float | None:
        value = row.get("amount")
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

