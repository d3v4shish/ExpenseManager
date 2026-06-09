from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import re
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal


class VendorDetailSignals(QObject):
    finished = pyqtSignal(str, tuple, dict)
    failed = pyqtSignal(str, tuple, str)


class VendorDetailWorker(QRunnable):
    def __init__(
        self,
        *,
        lookup_vendor,
        vendor_name: str,
        alias_key: str,
        cache_key: tuple[Any, ...],
        all_rows: list[dict[str, Any]],
        recurring_patterns: list[dict[str, Any]],
        selected_year: int,
        selected_month: int,
        dismissed_suggestions: set[str],
        load_rows=None,
    ) -> None:
        super().__init__()
        self.lookup_vendor = lookup_vendor
        self.vendor_name = vendor_name
        self.alias_key = alias_key
        self.cache_key = cache_key
        self.all_rows = [dict(row) for row in all_rows]
        self.load_rows = load_rows
        self.recurring_patterns = [dict(item) for item in recurring_patterns]
        self.selected_year = selected_year
        self.selected_month = selected_month
        self.dismissed_suggestions = set(dismissed_suggestions)
        self.signals = VendorDetailSignals()

    def run(self) -> None:
        try:
            if self.load_rows is not None:
                self.all_rows = [
                    dict(row)
                    for row in self.load_rows(
                        alias_key=self.alias_key,
                        vendor_name=self.vendor_name,
                        include_ignored=False,
                    )
                ]
            detail = self._build_detail()
            self.signals.finished.emit(self.alias_key or self.vendor_name, self.cache_key, detail)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.alias_key or self.vendor_name, self.cache_key, str(exc))

    def _build_detail(self) -> dict[str, Any]:
        alias_identity = self.alias_key.strip()
        vendor_identity = self.vendor_name.strip()
        if not alias_identity and not vendor_identity:
            return {}

        visible = [
            dict(row)
            for row in self.all_rows
            if self._row_matches_vendor(row, alias_identity, vendor_identity)
        ]
        if not visible:
            return {}

        visible.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
        debit_rows = [row for row in visible if str(row.get("direction", "")).strip().lower() == "debit"]
        credit_rows = [row for row in visible if str(row.get("direction", "")).strip().lower() == "credit"]
        range_rows = [
            row
            for row in visible
            if int(row.get("year", 0) or 0) == self.selected_year
            and (self.selected_month <= 0 or int(row.get("month", 0) or 0) == self.selected_month)
        ]

        lead = visible[0]
        vendor_record = self._find_vendor_record(lead)
        categories = list(vendor_record.get("categories", [])) if isinstance(vendor_record, dict) else []
        merge_members = list(vendor_record.get("mergeMembers", [])) if isinstance(vendor_record, dict) else []
        merged_vendors = list(vendor_record.get("mergedVendors", [])) if isinstance(vendor_record, dict) else []
        monthly_series = self._monthly_totals(visible)
        yearly_series = self._yearly_spend_totals(visible)
        month_options = self._available_months(visible)
        selected_month = self._selected_detail_month(month_options)
        selected_month_rows = self._rows_for_month(visible, int(selected_month.get("year", 0) or 0), int(selected_month.get("month", 0) or 0))
        alias_time_series = self._time_series(selected_month_rows)
        suggestions = self._merge_suggestions(lead, vendor_record)
        patterns = [
            pattern
            for pattern in self.recurring_patterns
            if str(pattern.get("vendorKey", "")).strip() == alias_identity
        ]
        recurrence = self._summarize_recurrence(patterns)
        canonical_vendor = vendor_identity or str(
            (vendor_record.get("canonicalVendor") if isinstance(vendor_record, dict) else "")
            or lead.get("canonicalVendor", lead.get("vendor", "Unknown"))
            or lead.get("vendor", "Unknown")
        )
        canonical_alias = str(
            (vendor_record.get("canonicalAlias") if isinstance(vendor_record, dict) else "")
            or lead.get("canonicalAlias", canonical_vendor)
            or canonical_vendor
        )
        alias_key = alias_identity or str(lead.get("aliasKey", "")).strip() or self._normalize_vendor_token(canonical_alias)
        total_debit = sum(float(row.get("amount", 0.0) or 0.0) for row in debit_rows)
        total_credit = sum(float(row.get("amount", 0.0) or 0.0) for row in credit_rows)
        net_amount = total_credit - total_debit
        selected_range_debit = sum(
            float(row.get("amount", 0.0) or 0.0)
            for row in range_rows
            if str(row.get("direction", "")).strip().lower() == "debit"
        )
        active_months = max(1, len(monthly_series))
        average_per_month = total_debit / active_months
        primary_category = str((categories[0] if categories else lead.get("category", "Uncategorized")) or "Uncategorized")
        vendor_rows = [row for row in visible if str(row.get("canonicalVendor", "")).strip() == canonical_vendor]
        category_rows = [
            row
            for row in self.all_rows
            if str(row.get("category", "Uncategorized") or "Uncategorized").strip() == primary_category
        ]
        category_selected_month_rows = self._rows_for_month(
            category_rows,
            int(selected_month.get("year", 0) or 0),
            int(selected_month.get("month", 0) or 0),
        )
        vendor_analytics = self._group_spend_analytics(vendor_rows)
        alias_analytics = self._group_spend_analytics(visible)
        category_analytics = self._group_spend_analytics(category_rows)
        alias_vendors = self._alias_vendor_summary(visible, canonical_vendor, canonical_alias, alias_key)
        known_alias_vendors = {str(item.get("canonicalVendor", "")).strip().lower() for item in alias_vendors}
        if canonical_vendor.strip() and canonical_vendor.strip().lower() not in known_alias_vendors:
            alias_vendors.insert(
                0,
                {
                    "canonicalVendor": canonical_vendor,
                    "canonicalAlias": canonical_alias,
                    "aliasKey": alias_key,
                    "transactionCount": len(vendor_rows),
                    "totalDebit": sum(
                        float(row.get("amount", 0.0) or 0.0)
                        for row in vendor_rows
                        if str(row.get("direction", "")).strip().lower() == "debit"
                    ),
                },
            )
            known_alias_vendors.add(canonical_vendor.strip().lower())
        for merged_vendor in merged_vendors:
            merged_name = str(merged_vendor.get("canonicalVendor", "")).strip()
            if not merged_name or merged_name.lower() in known_alias_vendors:
                continue
            alias_vendors.append(
                {
                    "canonicalVendor": merged_name,
                    "canonicalAlias": str(merged_vendor.get("canonicalAlias", canonical_alias)).strip() or canonical_alias,
                    "aliasKey": alias_key,
                    "transactionCount": 0,
                    "totalDebit": 0.0,
                }
            )
            known_alias_vendors.add(merged_name.lower())
        category_alias_count = len(
            {
                str(row.get("aliasKey", "")).strip()
                or self._normalize_vendor_token(str(row.get("canonicalAlias", "") or row.get("canonicalVendor", "") or ""))
                for row in category_rows
                if str(row.get("aliasKey", "")).strip()
                or str(row.get("canonicalAlias", "")).strip()
                or str(row.get("canonicalVendor", "")).strip()
            }
        )

        vendor_group = {
            "vendorId": int(vendor_record.get("vendorId", 0) or 0) if isinstance(vendor_record, dict) else 0,
            "canonicalVendor": canonical_vendor,
            "nickname": str(vendor_record.get("nickname", "")) if isinstance(vendor_record, dict) else "",
            "notes": str(vendor_record.get("notes", "")) if isinstance(vendor_record, dict) else "",
            "canonicalAlias": canonical_alias,
            "categories": categories,
            "updatedAt": str(vendor_record.get("updatedAt", "")) if isinstance(vendor_record, dict) else "",
            "transactionCount": len(vendor_rows),
            **vendor_analytics,
        }
        alias_group = {
            "canonicalAlias": canonical_alias,
            "aliasKey": alias_key,
            "categories": categories,
            "mergeMembers": merge_members,
            "mergedVendors": merged_vendors,
            "aliasVendors": alias_vendors,
            "mergedVendorCount": len(alias_vendors),
            "transactionCount": len(visible),
            "totalDebit": total_debit,
            "totalCredit": total_credit,
            "net": net_amount,
            "selectedRangeDebit": selected_range_debit,
            "activeMonths": active_months,
            "averagePerMonth": average_per_month,
            "availableMonths": month_options,
            "selectedMonth": selected_month,
            "monthlyTotals": monthly_series,
            "timeSeries": alias_time_series,
            "recurrence": recurrence,
            **alias_analytics,
        }
        category_group = {
            "categoryName": primary_category,
            "categories": categories,
            "categorySummary": self._category_totals(category_rows),
            "aliasCount": category_alias_count,
            "transactionCount": len(category_rows),
            "availableMonths": month_options,
            "selectedMonth": selected_month,
            "monthlyTotals": category_analytics["monthlyTotals"],
            "yearlyTotals": category_analytics["yearlyTotals"],
            "timeSeries": self._time_series(category_selected_month_rows),
            "monthBreakdown": self._category_totals(category_selected_month_rows),
            **category_analytics,
        }

        return {
            "vendor": str(lead.get("vendor", "Unknown")),
            "vendorKey": alias_key,
            "vendorId": int(vendor_record.get("vendorId", 0) or 0) if isinstance(vendor_record, dict) else 0,
            "canonicalVendor": canonical_vendor,
            "canonicalAlias": canonical_alias,
            "aliasKey": alias_key,
            "category": primary_category,
            "categories": categories,
            "subcategory": str(lead.get("subcategory", "") or ""),
            "nickname": str(vendor_record.get("nickname", "")) if isinstance(vendor_record, dict) else "",
            "notes": str(vendor_record.get("notes", "")) if isinstance(vendor_record, dict) else "",
            "mergeMembers": merge_members,
            "mergeSuggestions": suggestions,
            "recurrence": recurrence,
            "recurrencePatterns": patterns,
            "vendorRecord": vendor_record if isinstance(vendor_record, dict) else None,
            "transactionCount": len(visible),
            "totalDebit": total_debit,
            "totalCredit": total_credit,
            "net": net_amount,
            "selectedRangeDebit": selected_range_debit,
            "averagePerMonth": average_per_month,
            "activeMonths": active_months,
            "monthlyTotals": monthly_series,
            "yearlyTransactionCounts": yearly_series,
            "transactions": visible,
            "vendorTransactions": vendor_rows,
            "categoryTransactions": category_rows,
            "vendorGroup": vendor_group,
            "aliasGroup": alias_group,
            "categoryGroup": category_group,
        }

    def _row_matches_vendor(self, row: dict[str, Any], alias_identity: str, vendor_identity: str) -> bool:
        if alias_identity:
            return alias_identity in {
                str(row.get("vendorKey", "")).strip(),
                str(row.get("aliasKey", "")).strip(),
            }
        if not vendor_identity:
            return False
        return vendor_identity in {
            str(row.get("canonicalVendor", "")).strip(),
            str(row.get("resolvedVendor", "")).strip(),
            str(row.get("vendor", "")).strip(),
            str(row.get("counterparty", "")).strip(),
        }

    def _find_vendor_record(self, lead: dict[str, Any]) -> dict[str, Any] | None:
        for candidate in (
            str(lead.get("canonicalVendor", "")).strip(),
            str(lead.get("vendor", "")).strip(),
            str(lead.get("rawCounterparty", "")).strip(),
        ):
            if not candidate:
                continue
            record = self.lookup_vendor(candidate)
            if record:
                return record
        return None

    def _merge_suggestions(self, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> list[dict[str, Any]]:
        base = self._normalize_vendor_token(str(detail.get("canonicalAlias") or detail.get("vendor") or ""))
        if not base:
            return []
        existing = {
            self._normalize_vendor_token(str(item.get("rawName", "")))
            for item in (vendor_record.get("mergeMembers", []) if vendor_record else [])
        }
        buckets: dict[str, dict[str, Any]] = {}
        for row in self.all_rows:
            raw_name = str(row.get("rawCounterparty", "") or row.get("vendor", "")).strip()
            if not raw_name:
                continue
            normalized = self._normalize_vendor_token(raw_name)
            if not normalized or normalized == base or normalized in existing or raw_name in self.dismissed_suggestions:
                continue
            if not (normalized.startswith(base) or base.startswith(normalized) or base in normalized or normalized in base):
                continue
            bucket = buckets.setdefault(
                raw_name,
                {"rawName": raw_name, "count": 0, "amount": 0.0, "reason": f"Normalized match: {normalized}"},
            )
            bucket["count"] += 1
            bucket["amount"] += float(row.get("amount", 0.0) or 0.0)
        result = list(buckets.values())
        result.sort(key=lambda item: (item["count"], item["amount"], item["rawName"]), reverse=True)
        return result

    def _normalize_vendor_token(self, raw_value: str) -> str:
        text = raw_value.strip().lower()
        if "@" in text:
            text = text.split("@", 1)[0]
        text = re.sub(r"[^a-z0-9]+", " ", text)
        tokens = [part for part in text.split() if part and part not in {"upi", "pay", "p2m", "p2a"}]
        return "".join(tokens)

    def _summarize_recurrence(self, patterns: list[dict[str, Any]]) -> dict[str, Any]:
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
        monthly_like = sum(
            float(item.get("averageAmount", 0.0) or 0.0)
            for item in patterns
            if str(item.get("cadence", "")).strip() == "Monthly recurring"
        )
        return {
            "patternCount": len(patterns),
            "cadence": str(preferred.get("cadence", "")),
            "confidence": float(preferred.get("confidence", 0.0) or 0.0),
            "averageAmount": float(preferred.get("averageAmount", 0.0) or 0.0),
            "transactionCount": int(preferred.get("transactionCount", 0) or 0),
            "lastSeen": str(preferred.get("lastSeen", "")),
            "nextExpected": str(preferred.get("nextExpected", "")),
            "monthlyLikeAmount": monthly_like,
        }

    def _available_months(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build sorted year-month options from the selected alias cluster rows."""

        keys = {
            (int(row.get("year", 0) or 0), int(row.get("month", 0) or 0))
            for row in rows
            if int(row.get("year", 0) or 0) > 0 and int(row.get("month", 0) or 0) > 0
        }
        return [
            {"year": year, "month": month, "label": datetime(year, month, 1).strftime("%b %Y")}
            for year, month in sorted(keys)
        ]

    def _selected_detail_month(self, options: list[dict[str, Any]]) -> dict[str, Any]:
        """Choose the active month used by alias and category time-based charts."""

        if not options:
            return {"year": 0, "month": 0, "label": "No month"}
        for item in options:
            if int(item.get("year", 0) or 0) == self.selected_year and int(item.get("month", 0) or 0) == self.selected_month:
                return dict(item)
        return dict(options[-1])

    def _rows_for_month(self, rows: list[dict[str, Any]], year: int, month: int) -> list[dict[str, Any]]:
        """Filter one alias cluster down to a single year-month window."""

        if year <= 0 or month <= 0:
            return []
        return [
            row
            for row in rows
            if int(row.get("year", 0) or 0) == year and int(row.get("month", 0) or 0) == month
        ]

    def _monthly_totals(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Aggregate alias-cluster totals by month for vendor detail charts."""

        buckets: dict[tuple[int, int], dict[str, Any]] = {}
        for row in rows:
            year = int(row.get("year", 0) or 0)
            month = int(row.get("month", 0) or 0)
            if year <= 0 or month <= 0:
                continue
            bucket = buckets.setdefault(
                (year, month),
                {
                    "year": year,
                    "month": month,
                    "label": datetime(year, month, 1).strftime("%b %Y"),
                    "debitAmount": 0.0,
                    "creditAmount": 0.0,
                    "netAmount": 0.0,
                    "count": 0,
                },
            )
            amount = float(row.get("amount", 0.0) or 0.0)
            direction = str(row.get("direction", "")).strip().lower()
            if direction == "credit":
                bucket["creditAmount"] += amount
                bucket["netAmount"] += amount
            else:
                bucket["debitAmount"] += amount
                bucket["netAmount"] -= amount
            bucket["count"] += 1
        result = []
        for key in sorted(buckets):
            item = dict(buckets[key])
            item["amount"] = float(item["debitAmount"])
            result.append(item)
        return result

    def _yearly_spend_totals(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Aggregate debit spend by year for one scoped row set."""

        buckets: dict[int, dict[str, Any]] = {}
        for row in rows:
            year = int(row.get("year", 0) or 0)
            if year <= 0:
                continue
            bucket = buckets.setdefault(
                year,
                {
                    "year": year,
                    "label": str(year),
                    "debitAmount": 0.0,
                    "creditAmount": 0.0,
                    "netAmount": 0.0,
                    "count": 0,
                },
            )
            amount = float(row.get("amount", 0.0) or 0.0)
            direction = str(row.get("direction", "")).strip().lower()
            if direction == "credit":
                bucket["creditAmount"] += amount
                bucket["netAmount"] += amount
            else:
                bucket["debitAmount"] += amount
                bucket["netAmount"] -= amount
            bucket["count"] += 1
        result = []
        for year in sorted(buckets):
            item = dict(buckets[year])
            item["amount"] = float(item["debitAmount"])
            result.append(item)
        return result

    def _yearly_transaction_counts(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Aggregate alias-cluster transaction counts by year."""

        counts: dict[int, int] = defaultdict(int)
        for row in rows:
            year = int(row.get("year", 0) or 0)
            if year > 0:
                counts[year] += 1
        return [{"label": str(year), "year": year, "count": count, "amount": float(count)} for year, count in sorted(counts.items())]

    def _time_series(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Aggregate one month of alias-cluster rows into hour-of-day buckets."""

        buckets = {
            hour: {
                "bucketKey": hour,
                "bucketLabel": f"{hour:02d}:00",
                "debitAmount": 0.0,
                "creditAmount": 0.0,
                "netAmount": 0.0,
                "count": 0,
            }
            for hour in range(24)
        }
        for row in rows:
            timestamp = str(row.get("timestamp", "")).strip()
            if not timestamp:
                continue
            try:
                hour = datetime.fromisoformat(timestamp).hour
            except ValueError:
                continue
            amount = float(row.get("amount", 0.0) or 0.0)
            direction = str(row.get("direction", "")).strip().lower()
            bucket = buckets[hour]
            if direction == "credit":
                bucket["creditAmount"] += amount
                bucket["netAmount"] += amount
            else:
                bucket["debitAmount"] += amount
                bucket["netAmount"] -= amount
            bucket["count"] += 1
        result = []
        for hour in range(24):
            item = dict(buckets[hour])
            item["label"] = item["bucketLabel"]
            item["amount"] = float(item["debitAmount"])
            result.append(item)
        return result

    def _group_spend_analytics(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Build monthly, yearly, and current-period spend metrics for one row scope."""

        debit_rows = [row for row in rows if str(row.get("direction", "")).strip().lower() == "debit"]
        monthly_totals = self._monthly_totals(rows)
        yearly_totals = self._yearly_spend_totals(rows)
        active_months = max(1, len(monthly_totals))
        now = datetime.now()
        spend_this_month = sum(
            float(row.get("amount", 0.0) or 0.0)
            for row in debit_rows
            if int(row.get("year", 0) or 0) == now.year and int(row.get("month", 0) or 0) == now.month
        )
        spend_this_year = sum(
            float(row.get("amount", 0.0) or 0.0)
            for row in debit_rows
            if int(row.get("year", 0) or 0) == now.year
        )
        total_debit = sum(float(row.get("amount", 0.0) or 0.0) for row in debit_rows)
        return {
            "monthlyTotals": monthly_totals,
            "yearlyTotals": yearly_totals,
            "averageSpendPerMonth": float(total_debit / active_months),
            "spendThisMonth": float(spend_this_month),
            "spendThisYear": float(spend_this_year),
            "activeMonths": int(active_months),
        }

    def _alias_vendor_summary(
        self,
        rows: list[dict[str, Any]],
        canonical_vendor: str,
        canonical_alias: str,
        alias_key: str,
    ) -> list[dict[str, Any]]:
        """Summarize the distinct canonical vendors attached to one alias cluster."""

        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            vendor_name = str(row.get("canonicalVendor", row.get("vendor", ""))).strip() or canonical_vendor or "Unknown"
            bucket = buckets.setdefault(
                vendor_name.lower(),
                {
                    "canonicalVendor": vendor_name,
                    "canonicalAlias": canonical_alias,
                    "aliasKey": alias_key,
                    "transactionCount": 0,
                    "totalDebit": 0.0,
                },
            )
            bucket["transactionCount"] += 1
            if str(row.get("direction", "")).strip().lower() == "debit":
                bucket["totalDebit"] += float(row.get("amount", 0.0) or 0.0)
        result = list(buckets.values())
        result.sort(
            key=lambda item: (
                float(item.get("totalDebit", 0.0) or 0.0),
                int(item.get("transactionCount", 0) or 0),
                str(item.get("canonicalVendor", "")),
            ),
            reverse=True,
        )
        return result

    def _category_totals(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Aggregate category totals for the selected alias cluster."""

        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            label = str(row.get("category", "Uncategorized") or "Uncategorized").strip() or "Uncategorized"
            bucket = buckets.setdefault(
                label,
                {
                    "category": label,
                    "label": label,
                    "debitAmount": 0.0,
                    "creditAmount": 0.0,
                    "netAmount": 0.0,
                    "count": 0,
                },
            )
            amount = float(row.get("amount", 0.0) or 0.0)
            direction = str(row.get("direction", "")).strip().lower()
            if direction == "credit":
                bucket["creditAmount"] += amount
                bucket["netAmount"] += amount
            else:
                bucket["debitAmount"] += amount
                bucket["netAmount"] -= amount
            bucket["count"] += 1
        result = [dict(item, amount=float(item["debitAmount"])) for item in buckets.values()]
        result.sort(
            key=lambda item: (
                float(item.get("debitAmount", 0.0) or 0.0),
                int(item.get("count", 0) or 0),
                str(item.get("category", "")),
            ),
            reverse=True,
        )
        return result


class VendorMutationSignals(QObject):
    finished = pyqtSignal(str, dict)
    failed = pyqtSignal(str, str)


class VendorMutationWorker(QRunnable):
    def __init__(self, *, action_id: str, handler) -> None:
        super().__init__()
        self.action_id = action_id
        self.handler = handler
        self.signals = VendorMutationSignals()

    def run(self) -> None:
        try:
            payload = self.handler()
            report = dict(payload) if isinstance(payload, dict) else {"payload": payload}
            self.signals.finished.emit(self.action_id, report)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.action_id, str(exc))


class TransactionMutationSignals(QObject):
    finished = pyqtSignal(str, dict)
    failed = pyqtSignal(str, str)


class TransactionMutationWorker(QRunnable):
    def __init__(self, *, action_id: str, handler) -> None:
        super().__init__()
        self.action_id = action_id
        self.handler = handler
        self.signals = TransactionMutationSignals()

    def run(self) -> None:
        try:
            payload = self.handler()
            report = dict(payload) if isinstance(payload, dict) else {"payload": payload}
            self.signals.finished.emit(self.action_id, report)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.action_id, str(exc))


class LedgerRowsSignals(QObject):
    finished = pyqtSignal(int, tuple, list)
    failed = pyqtSignal(int, tuple, str)


class LedgerRowsWorker(QRunnable):
    def __init__(
        self,
        *,
        load_rows,
        request_id: int,
        cache_key: tuple[Any, ...],
        year: int,
        month: int,
        group_key: str,
        include_ignored: bool,
        search_text: str,
    ) -> None:
        super().__init__()
        self.load_rows = load_rows
        self.request_id = request_id
        self.cache_key = cache_key
        self.year = year
        self.month = month
        self.group_key = group_key
        self.include_ignored = include_ignored
        self.search_text = search_text
        self.signals = LedgerRowsSignals()

    def run(self) -> None:
        try:
            rows = self.load_rows(
                year=self.year,
                month=self.month,
                group_key=self.group_key,
                include_ignored=self.include_ignored,
                search_text=self.search_text,
            )
            self.signals.finished.emit(self.request_id, self.cache_key, rows)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.request_id, self.cache_key, str(exc))


class LedgerGroupsSignals(QObject):
    finished = pyqtSignal(int, tuple, list)
    failed = pyqtSignal(int, tuple, str)


class LedgerGroupsWorker(QRunnable):
    def __init__(
        self,
        *,
        load_groups,
        request_id: int,
        cache_key: tuple[Any, ...],
        year: int,
        month: int,
        include_ignored: bool,
        search_text: str,
    ) -> None:
        super().__init__()
        self.load_groups = load_groups
        self.request_id = request_id
        self.cache_key = cache_key
        self.year = year
        self.month = month
        self.include_ignored = include_ignored
        self.search_text = search_text
        self.signals = LedgerGroupsSignals()

    def run(self) -> None:
        try:
            groups = self.load_groups(
                year=self.year,
                month=self.month,
                include_ignored=self.include_ignored,
                search_text=self.search_text,
            )
            self.signals.finished.emit(self.request_id, self.cache_key, groups)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.request_id, self.cache_key, str(exc))
