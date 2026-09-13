"""Safe, versioned CLI reports over ExpenseManager's local runtime."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from src.expenses.account_config import build_expense_mail_config_snapshot


class ReportService:
    """Expose the GUI's user-visible data without importing UI widgets."""

    SCHEMA_VERSION = "expense-manager-report-v1"
    _SENSITIVE_PARTS = ("body", "payload", "content", "raw", "revision")

    def __init__(self, *, expenses_service, expenses_repository, analytics_service, vendor_catalog_service, settings_service, data_management_service, mail_ingestion_service, files) -> None:
        self.expenses_service = expenses_service
        self.expenses_repository = expenses_repository
        self.analytics_service = analytics_service
        self.vendor_catalog_service = vendor_catalog_service
        self.settings_service = settings_service
        self.data_management_service = data_management_service
        self.mail_ingestion_service = mail_ingestion_service
        self.files = files

    def dashboard(self, *, year: int | None = None, month: int | None = None, currency: str = "", months: int = 6, top: int = 10) -> dict[str, Any]:
        """Return the bounded at-a-glance dashboard in one JSON document."""

        overview = self.overview()
        analytics = self.analytics(year=year, month=month, currency=currency, months=months, top=top)
        insights = self.insights(limit=min(max(top, 1), 100))
        return self._report(
            "dashboard",
            {
                "overview": overview["data"],
                "analytics": analytics["data"],
                "insightInbox": {
                    "unreadCount": self.analytics_service.unread_count(),
                    "top": insights["data"],
                },
                "sources": self.sources()["data"],
                "storage": self.storage()["data"],
            },
            filters={"year": year, "month": month, "currency": currency.upper(), "months": self._months(months), "top": self._top(top)},
        )

    def overview(self) -> dict[str, Any]:
        payload = self.expenses_service.build_overview_snapshot()
        return self._report("overview", payload)

    def analytics(self, *, year: int | None = None, month: int | None = None, currency: str = "", include_ignored: bool = False, search_text: str = "", months: int = 6, top: int = 10) -> dict[str, Any]:
        """Return filters, aggregates, and chart-ready series shown by analysis."""

        snapshot = self.expenses_service.build_analysis_snapshot(
            year=year,
            month=month,
            currency=currency,
            include_ignored=include_ignored,
            search_text=search_text,
        )
        limit = self._top(top)
        data = {
            "filters": snapshot.get("filters", {}),
            "selectedRange": snapshot.get("selectedRange", {}),
            "selectedMetrics": snapshot.get("selectedMetrics", {}),
            "selectedTopVendor": snapshot.get("selectedTopVendor", {}),
            "selectedInsights": snapshot.get("selectedInsights", {}),
            "selectedAverages": snapshot.get("selectedAverages", {}),
            "allTimeInsights": snapshot.get("allTimeInsights", {}),
            "allTimeAverages": snapshot.get("allTimeAverages", {}),
            "selectedCreditSummary": snapshot.get("selectedCreditSummary", {}),
            "selectedNetFlow": snapshot.get("selectedNetFlow", {}),
            "accounts": snapshot.get("accounts", []),
            "monthlyDebit": snapshot.get("selectedYearMonths", []),
            "monthlyCredit": snapshot.get("creditYearMonths", []),
            "dailyDebit": snapshot.get("selectedMonthDaily", []),
            "weeklyDebit": snapshot.get("selectedMonthWeekly", []),
            "previousMonths": self._previous_months(currency=currency, months=months),
            "topVendors": list(snapshot.get("vendorSummary", []))[:limit],
            "categories": list(snapshot.get("categorySummary", []))[:limit],
            "recurringPatterns": list(snapshot.get("recurringPatterns", []))[:limit],
            "meta": snapshot.get("meta", {}),
        }
        return self._report(
            "analytics",
            data,
            filters={"year": year, "month": month, "currency": currency.upper(), "includeIgnored": include_ignored, "search": search_text, "months": self._months(months), "top": limit},
        )

    def transactions(self, *, year: int | None = None, month: int | None = None, currency: str = "", include_ignored: bool = False, search_text: str = "", limit: int = 200, offset: int = 0) -> dict[str, Any]:
        page = self.expenses_repository.list_transactions_page(
            year=year,
            month=month,
            currency=currency,
            include_ignored=include_ignored,
            search_text=search_text,
            limit=limit,
            offset=offset,
        )
        return self._report("transactions", page, filters={"year": year, "month": month, "currency": currency.upper(), "includeIgnored": include_ignored, "search": search_text})

    def transaction(self, transaction_key: str) -> dict[str, Any]:
        rows = self.expenses_repository.list_transactions_by_keys([transaction_key])
        if not rows:
            raise ValueError(f"Transaction '{transaction_key}' was not found.")
        return self._report("transaction", {"transaction": rows[0], "sources": self.expenses_repository.list_transaction_sources(transaction_key)})

    def ledger_groups(self, *, year: int, month: int, currency: str = "", include_ignored: bool = False, search_text: str = "") -> dict[str, Any]:
        groups = self.expenses_repository.list_ledger_groups(year=year, month=month, currency=currency, include_ignored=include_ignored, search_text=search_text)
        return self._report("ledgerGroups", {"groups": groups}, filters={"year": year, "month": month, "currency": currency.upper(), "includeIgnored": include_ignored, "search": search_text})

    def ledger_group(self, *, year: int, month: int, group_key: str, currency: str = "", include_ignored: bool = False, search_text: str = "") -> dict[str, Any]:
        rows = self.expenses_repository.list_ledger_group_rows(year=year, month=month, group_key=group_key, currency=currency, include_ignored=include_ignored, search_text=search_text)
        return self._report("ledgerGroup", {"groupKey": group_key, "rows": rows, "total": len(rows)}, filters={"year": year, "month": month, "currency": currency.upper(), "includeIgnored": include_ignored, "search": search_text})

    def vendors(self, *, query: str = "", limit: int = 100) -> dict[str, Any]:
        catalog = self.vendor_catalog_service.list_vendors(query)
        activity = self.expenses_repository.list_vendor_summary(limit=limit)
        return self._report("vendors", {"catalog": catalog[: self._limit(limit)], "activity": activity})

    def vendor(self, identity: str, *, currency: str = "", include_ignored: bool = False, limit: int = 500) -> dict[str, Any]:
        record = self.vendor_catalog_service.find_vendor(identity) or {}
        vendor_key = str(record.get("aliasKey", "")).strip()
        vendor_name = str(record.get("canonicalVendor", identity)).strip() or identity
        rows = self.expenses_repository.list_transactions_page(currency=currency, include_ignored=include_ignored, search_text=vendor_key or vendor_name, limit=limit).get("rows", [])
        if vendor_key:
            rows = [row for row in rows if str(row.get("aliasKey", "")).strip() == vendor_key]
        all_rows = self.expenses_repository.list_transactions(include_ignored=include_ignored)
        if currency:
            all_rows = [row for row in all_rows if str(row.get("currency", "")).upper() == currency.upper()]
        alias_rows = [row for row in all_rows if (str(row.get("aliasKey", "")).strip() == vendor_key if vendor_key else str(row.get("canonicalVendor", "")).strip() == vendor_name)]
        canonical_rows = [row for row in alias_rows if str(row.get("canonicalVendor", "")).strip() == vendor_name]
        categories = [str(item).strip() for item in record.get("categories", []) if str(item).strip()] if isinstance(record, dict) else []
        category = categories[0] if categories else str((alias_rows[0] if alias_rows else {}).get("category", "Uncategorized") or "Uncategorized")
        category_rows = [row for row in all_rows if str(row.get("category", "Uncategorized") or "Uncategorized") == category]
        data = {
            "vendor": record,
            "identity": {"vendor": vendor_name, "aliasKey": vendor_key},
            "transactions": rows,
            "transactionCount": len(rows),
            "vendorGroup": self._spend_group(canonical_rows, name=vendor_name, categories=categories),
            "aliasGroup": {
                **self._spend_group(alias_rows, name=str(record.get("canonicalAlias", vendor_name) if isinstance(record, dict) else vendor_name), categories=categories),
                "aliasKey": vendor_key,
                "mergeMembers": list(record.get("mergeMembers", [])) if isinstance(record, dict) else [],
                "mergedVendors": list(record.get("mergedVendors", [])) if isinstance(record, dict) else [],
            },
            "categoryGroup": {
                **self._spend_group(category_rows, name=category, categories=categories),
                "categoryName": category,
                "breakdown": self._category_breakdown(category_rows),
            },
            "provenance": {str(row.get("transactionKey", "")): self.expenses_repository.list_transaction_sources(str(row.get("transactionKey", ""))) for row in rows},
        }
        return self._report("vendor", data, filters={"currency": currency.upper(), "includeIgnored": include_ignored, "limit": self._limit(limit)})

    def insights(self, *, statuses: list[str] | None = None, severity: str = "", insight_type: str = "", search_text: str = "", limit: int = 200, offset: int = 0) -> dict[str, Any]:
        page = self.analytics_service.list_insights(statuses=statuses, severity=severity, insight_type=insight_type, search_text=search_text, limit=limit, offset=offset)
        return self._report("insights", page, filters={"statuses": statuses or [], "severity": severity, "type": insight_type, "search": search_text})

    def insight(self, insight_key: str) -> dict[str, Any]:
        detail = self.analytics_service.get_insight(insight_key)
        if detail is None:
            raise ValueError(f"Insight '{insight_key}' was not found.")
        return self._report("insight", detail)

    def sources(self) -> dict[str, Any]:
        try:
            config = self.files.read_user("email_accounts.json")
        except FileNotFoundError:
            config = {"providers": []}
        summaries = self.expenses_repository.source_storage_summaries()
        providers: list[dict[str, Any]] = []
        for item in config.get("providers", []):
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("id", "")).strip()
            provider_type = str(item.get("type", "")).strip().lower()
            validation = self._provider_validation(item)
            providers.append(
                {
                    "id": provider_id,
                    "type": provider_type,
                    "location": str(item.get("path", item.get("profilePath", ""))).strip(),
                    "enabled": bool(item.get("enabled", True)),
                    "validation": validation,
                    "storage": summaries.get(provider_id, {}),
                    "checkpoint": self.expenses_repository.get_provider_checkpoint(provider_id) or {},
                }
            )
        return self._report(
            "sources",
            {
                "providers": providers,
                "supportedTypes": list(self.mail_ingestion_service.provider_registry.supported_types()),
                "duplicates": self.expenses_repository.list_duplicate_candidates(),
                "reconciliation": {
                    "policy": self.expenses_service.get_reconciliation_policy(),
                    "conflicts": self.expenses_service.list_reconciliation_conflicts(),
                },
            },
        )

    def account(self) -> dict[str, Any]:
        snapshot = build_expense_mail_config_snapshot(self.files, self.expenses_repository)
        snapshot["sourceValidation"] = self.mail_ingestion_service.validate_thunderbird_directory()
        return self._report("account", {"active": snapshot, "setup": self.setup_status()["data"]})

    def diagnostics(self, *, provider_id: str = "thunderbird_local", limit: int = 200) -> dict[str, Any]:
        records = self.expenses_repository.list_source_debug_rows(provider_id=provider_id, limit=limit)
        return self._report(
            "diagnostics",
            {
                "providerId": provider_id,
                "summary": self.expenses_repository.candidate_mail_debug_summary(provider_id=provider_id),
                "records": records,
                "journals": self.expenses_repository.list_import_journals(provider_id=provider_id, limit=limit),
                "scriptExtractors": self.mail_ingestion_service.script_extractor_status(),
                "sensitiveDataExcluded": True,
            },
        )

    def settings(self) -> dict[str, Any]:
        return self._report("settings", {"settings": self.settings_service.load(), "guiOnlyActions": ["save settings", "backup", "restore", "cleanup", "database deletion", "uninstall preparation"]})

    def storage(self) -> dict[str, Any]:
        return self._report("storage", self.data_management_service.storage_report())

    def capabilities(self) -> dict[str, Any]:
        return self._report(
            "capabilities",
            {
                "cliReadReports": ["dashboard", "overview", "analytics", "transactions", "ledger groups", "vendors", "insights", "sources", "account", "diagnostics", "settings", "storage"],
                "cliActions": ["sources add-eml", "sources add-csv", "sources add-sms-backup", "sources add-axios-archive", "sources refresh", "sources rebuild", "sources remove", "sources delete-data", "duplicates resolve", "reconciliation list", "reconciliation resolve", "reconciliation set-policy", "templates mine", "templates draft"],
                "guiOnlyActions": ["Thunderbird profile/account selection", "vendor editing", "bank-rule editing", "settings editing", "backup/restore", "storage cleanup", "uninstall retention dialog"],
                "sensitiveDataExcluded": ["email and SMS bodies", "source payloads and revisions", "raw bank-rule JSON", "raw configuration files"],
            },
        )

    def setup_status(self) -> dict[str, Any]:
        try:
            config = self.files.read_user("email_accounts.json")
        except FileNotFoundError:
            config = {"providers": []}
        thunderbird = next((item for item in config.get("providers", []) if isinstance(item, dict) and str(item.get("type", "")).lower() == "thunderbird"), None)
        configured = thunderbird is not None
        return self._report(
            "setupStatus",
            {
                "providers": [
                    {"type": "eml_folder", "setup": "cli_ready"},
                    {"type": "csv", "setup": "cli_ready"},
                    {"type": "sms_backup", "setup": "cli_ready"},
                    {"type": "axios_archive", "setup": "cli_ready"},
                    {
                        "type": "thunderbird",
                        "setup": "configured" if configured else "gui_required",
                        "message": "Launch ExpenseManager gui, open Mail Configuration, choose a Thunderbird profile and account, then run CLI import commands." if not configured else "Thunderbird is configured; validate before importing.",
                    },
                ]
            },
        )

    def _provider_validation(self, provider: dict[str, Any]) -> dict[str, Any]:
        if not bool(provider.get("enabled", True)):
            return {"valid": True, "message": "Source is disabled; validation skipped."}
        try:
            instance = self.mail_ingestion_service.provider_registry.create(provider, self.mail_ingestion_service.thunderbird_local_path)
            return dict(instance.validate())
        except ValueError as exc:
            return {"valid": False, "message": str(exc)}

    def _previous_months(self, *, currency: str, months: int) -> list[dict[str, Any]]:
        count = self._months(months)
        now = datetime.now().astimezone()
        month_keys: list[tuple[int, int]] = []
        year, month = now.year, now.month
        for _ in range(count):
            month_keys.append((year, month))
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        selected_currency = currency.strip().upper()
        totals: dict[tuple[int, int], float] = defaultdict(float)
        for row in self.expenses_repository.list_monthly_debit_totals(currency=selected_currency):
            totals[(int(row["year"]), int(row["month"]))] += float(row["amount"])
        return [{"year": year, "month": month, "label": f"{year:04d}-{month:02d}", "amount": totals[(year, month)]} for year, month in reversed(month_keys)]

    def _spend_group(self, rows: list[dict[str, Any]], *, name: str, categories: list[str]) -> dict[str, Any]:
        """Build the metric/chart fields shared by GUI vendor and category drill-downs."""

        debits = [row for row in rows if str(row.get("direction", "")).lower() == "debit"]
        credits = [row for row in rows if str(row.get("direction", "")).lower() == "credit"]
        month_totals: dict[tuple[int, int], float] = defaultdict(float)
        year_totals: dict[int, float] = defaultdict(float)
        for row in debits:
            year, month = int(row.get("year", 0) or 0), int(row.get("month", 0) or 0)
            amount = float(row.get("amount", 0.0) or 0.0)
            month_totals[(year, month)] += amount
            year_totals[year] += amount
        now = datetime.now().astimezone()
        current_month = month_totals[(now.year, now.month)]
        active_months = max(1, len(month_totals))
        total_debit = float(sum(float(row.get("amount", 0.0) or 0.0) for row in debits))
        total_credit = float(sum(float(row.get("amount", 0.0) or 0.0) for row in credits))
        return {
            "name": name,
            "categories": categories,
            "transactionCount": len(rows),
            "totalDebit": total_debit,
            "totalCredit": total_credit,
            "net": total_credit - total_debit,
            "spendThisMonth": current_month,
            "spendThisYear": year_totals[now.year],
            "activeMonths": active_months,
            "averageSpendPerMonth": total_debit / active_months,
            "monthlyTotals": [
                {"year": year, "month": month, "label": f"{year:04d}-{month:02d}", "amount": amount}
                for (year, month), amount in sorted(month_totals.items())
            ],
            "yearlyTotals": [{"year": year, "amount": amount} for year, amount in sorted(year_totals.items())],
        }

    @staticmethod
    def _category_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"amount": 0.0, "count": 0})
        for row in rows:
            if str(row.get("direction", "")).lower() != "debit":
                continue
            key = str(row.get("canonicalVendor", row.get("counterparty", "Unknown")) or "Unknown")
            totals[key]["amount"] += float(row.get("amount", 0.0) or 0.0)
            totals[key]["count"] += 1
        return [
            {"label": name, "amount": float(values["amount"]), "count": int(values["count"])}
            for name, values in sorted(totals.items(), key=lambda item: (-float(item[1]["amount"]), item[0]))
        ]

    def _report(self, name: str, data: Any, *, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "schemaVersion": self.SCHEMA_VERSION,
            "report": name,
            "generatedAt": datetime.now().astimezone().isoformat(),
            "filters": filters or {},
            "data": self._safe(data),
        }

    def _safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): self._safe(item)
                for key, item in value.items()
                if not any(part in str(key).lower() for part in self._SENSITIVE_PARTS)
            }
        if isinstance(value, list):
            return [self._safe(item) for item in value]
        if isinstance(value, tuple):
            return [self._safe(item) for item in value]
        return value

    @staticmethod
    def _top(value: int) -> int:
        return max(1, min(int(value or 10), 100))

    @staticmethod
    def _limit(value: int) -> int:
        return max(1, min(int(value or 100), 1000))

    @staticmethod
    def _months(value: int) -> int:
        return max(1, min(int(value or 6), 120))
