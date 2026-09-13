from __future__ import annotations

import hashlib
import logging
import math
import statistics
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

try:  # NumPy is used in packaged builds; the fallback keeps domain tests headless.
    import numpy as np
except ImportError:  # pragma: no cover - exercised only in minimal developer environments
    np = None


class AnalyticsService:
    """Generate deterministic, explainable spend insights on the active account DB."""

    ALGORITHM_VERSION = "expense-insights-v1"

    DEFAULTS = {
        "enabled": True,
        "sensitivity": "normal",
        "minimumHistory": 5,
        "unusualAmountMultiplier": 2.5,
        "robustDeviation": 3.5,
        "comparisonMonths": 6,
        "minimumComparisonMonths": 3,
        "periodSpikePercent": 30.0,
        "recurringChangePercent": 15.0,
        "recurringMinimumConfidence": 0.7,
        "duplicateWindowMinutes": 10,
        "inrTransactionFloor": 500.0,
        "inrPeriodFloor": 1000.0,
    }

    def __init__(self, repository, settings_service=None) -> None:
        self.repository = repository
        self.settings_service = settings_service
        self.logger = logging.getLogger(self.__class__.__name__)

    def recompute(self) -> dict[str, Any]:
        """Rebuild active insights and persist them without losing user state."""

        settings = self._settings()
        if not bool(settings.get("enabled", True)):
            report = self.repository.replace_insights([], algorithm_version=self.ALGORITHM_VERSION)
            return {"enabled": False, "generated": 0, **report}
        rows = self.repository.list_analytics_rows()
        insights = self.generate(rows, settings=settings)
        report = self.repository.replace_insights(insights, algorithm_version=self.ALGORITHM_VERSION)
        self.logger.info("Local analytics recomputed rows=%d insights=%d", len(rows), len(insights))
        return {"enabled": True, "transactions": len(rows), "generated": len(insights), **report}

    def generate(
        self,
        rows: list[dict[str, Any]],
        *,
        settings: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return insight payloads for rows without mutating persistence."""

        config = {**self.DEFAULTS, **dict(settings or {})}
        normalized = [item for row in rows if (item := self._normalize_row(row)) is not None]
        if not normalized:
            return []
        requested_now = now or datetime.now().astimezone()
        reference_now = max(requested_now, max(item["stamp"] for item in normalized))
        insights = [
            *self._duplicate_insights(normalized, config),
            *self._unusual_amount_insights(normalized, config, reference_now),
            *self._period_spike_insights(normalized, config),
            *self._recurring_insights(normalized, config, reference_now),
        ]
        return sorted(
            insights,
            key=lambda item: (
                {"high": 0, "medium": 1, "low": 2}.get(str(item.get("severity")), 3),
                str(item.get("periodEnd", "")),
                str(item.get("insightKey", "")),
            ),
        )

    def list_insights(self, **filters) -> dict[str, Any]:
        return self.repository.list_insights(**filters)

    def get_insight(self, insight_key: str) -> dict[str, Any] | None:
        insight = self.repository.get_insight(insight_key)
        if insight is None:
            return None
        keys = list(insight.get("evidenceTransactionKeys", []))
        insight["evidenceTransactions"] = self.repository.list_transactions_by_keys(keys)
        return insight

    def set_status(self, insight_key: str, status: str) -> dict[str, Any]:
        self.repository.set_insight_status(insight_key, status)
        return self.get_insight(insight_key) or {}

    def unread_count(self) -> int:
        return self.repository.unread_insight_count()

    def _settings(self) -> dict[str, Any]:
        if self.settings_service is None:
            return dict(self.DEFAULTS)
        snapshot = self.settings_service.load()
        analytics = snapshot.get("analytics", {}) if isinstance(snapshot, dict) else {}
        config = {**self.DEFAULTS, **dict(analytics if isinstance(analytics, dict) else {})}
        sensitivity = str(config.get("sensitivity", "normal")).lower()
        if sensitivity == "low":
            config["unusualAmountMultiplier"] = float(config["unusualAmountMultiplier"]) * 1.25
            config["robustDeviation"] = float(config["robustDeviation"]) * 1.20
            config["periodSpikePercent"] = float(config["periodSpikePercent"]) * 1.25
            config["recurringChangePercent"] = float(config["recurringChangePercent"]) * 1.25
        elif sensitivity == "high":
            config["unusualAmountMultiplier"] = float(config["unusualAmountMultiplier"]) * 0.85
            config["robustDeviation"] = float(config["robustDeviation"]) * 0.85
            config["periodSpikePercent"] = float(config["periodSpikePercent"]) * 0.75
            config["recurringChangePercent"] = float(config["recurringChangePercent"]) * 0.75
        return config

    def _duplicate_insights(self, rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
        window = timedelta(minutes=max(1, int(config.get("duplicateWindowMinutes", 10) or 10)))
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[
                (
                    row["account"],
                    row["direction"],
                    row["currency"],
                    round(row["amount"], 2),
                    row["vendorKey"],
                )
            ].append(row)
        result: list[dict[str, Any]] = []
        for candidates in groups.values():
            candidates.sort(key=lambda item: item["stamp"])
            recent: deque[dict[str, Any]] = deque()
            for current in candidates:
                while recent and current["stamp"] - recent[0]["stamp"] > window:
                    recent.popleft()
                if recent:
                    previous = recent[-1]
                    delta = current["stamp"] - previous["stamp"]
                    pair = tuple(sorted((previous["transactionKey"], current["transactionKey"])))
                    same_reference = bool(current["transactionId"] and current["transactionId"] == previous["transactionId"])
                    severity = "high" if same_reference else "medium"
                    summary = (
                        "The same transaction reference appears more than once."
                        if same_reference
                        else f"Two matching charges occurred within {int(delta.total_seconds() // 60) + 1} minute(s)."
                    )
                    result.append(
                        self._insight(
                            key_parts=("duplicate", *pair),
                            insight_type="possible_duplicate",
                            severity=severity,
                            title=f"Possible duplicate at {current['vendor']}",
                            summary=summary,
                            row=current,
                            baseline=current["amount"],
                            delta_percent=0.0,
                            confidence=0.98 if same_reference else 0.75,
                            evidence=list(pair),
                        )
                    )
                recent.append(current)
        return result

    def _unusual_amount_insights(
        self,
        rows: list[dict[str, Any]],
        config: dict[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["direction"] == "debit":
                groups[(row["vendorKey"], row["account"], row["currency"])].append(row)
        result: list[dict[str, Any]] = []
        minimum_history = max(3, int(config.get("minimumHistory", 5) or 5))
        multiplier = float(config.get("unusualAmountMultiplier", 2.5) or 2.5)
        robust_limit = float(config.get("robustDeviation", 3.5) or 3.5)
        recent_cutoff = now - timedelta(days=90)
        for candidates in groups.values():
            candidates.sort(key=lambda item: item["stamp"])
            history: list[float] = []
            for row in candidates:
                if len(history) >= minimum_history and row["stamp"] >= recent_cutoff:
                    median = self._median(history)
                    mad = self._median([abs(value - median) for value in history])
                    scale = max(1.4826 * mad, abs(median) * 0.10, 0.01)
                    robust_score = (row["amount"] - median) / scale
                    floor = float(config.get("inrTransactionFloor", 500.0)) if row["currency"] == "INR" else abs(median) * 0.20
                    if row["amount"] >= median * multiplier and robust_score >= robust_limit and row["amount"] - median >= floor:
                        delta = self._percent_change(row["amount"], median)
                        result.append(
                            self._insight(
                                key_parts=("unusual", row["transactionKey"]),
                                insight_type="unusual_amount",
                                severity="high" if row["amount"] >= median * 4 else "medium",
                                title=f"Unusual amount at {row['vendor']}",
                                summary=f"This charge is {delta:.0f}% above the median of {len(history)} earlier comparable charges.",
                                row=row,
                                baseline=median,
                                delta_percent=delta,
                                confidence=min(0.99, 0.65 + robust_score / 20.0),
                                evidence=[row["transactionKey"]],
                            )
                        )
                history.append(row["amount"])
        return result

    def _period_spike_insights(self, rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
        debits = [row for row in rows if row["direction"] == "debit"]
        if not debits:
            return []
        latest_stamp = max(row["stamp"] for row in debits)
        day_limit = latest_stamp.day
        current_month = (latest_stamp.year, latest_stamp.month)
        comparison_count = max(3, int(config.get("comparisonMonths", 6) or 6))
        minimum_months = max(2, int(config.get("minimumComparisonMonths", 3) or 3))
        spike_percent = float(config.get("periodSpikePercent", 30.0) or 30.0)
        result: list[dict[str, Any]] = []
        for scope_type, value_key in (("category", "category"), ("vendor", "vendorKey")):
            monthly: dict[tuple[str, str, int, int], float] = defaultdict(float)
            evidence: dict[tuple[str, str, int, int], list[str]] = defaultdict(list)
            display_names: dict[tuple[str, str], str] = {}
            for row in debits:
                if row["stamp"].day > day_limit:
                    continue
                scope_value = str(row[value_key])
                key = (scope_value, row["currency"], row["stamp"].year, row["stamp"].month)
                monthly[key] += row["amount"]
                evidence[key].append(row["transactionKey"])
                display_names[(scope_value, row["currency"])] = row["category"] if scope_type == "category" else row["vendor"]
            scopes = {(scope_value, currency) for scope_value, currency, _, _ in monthly}
            for scope_value, currency in scopes:
                current_key = (scope_value, currency, *current_month)
                actual = monthly.get(current_key, 0.0)
                if actual <= 0:
                    continue
                prior_values: list[float] = []
                cursor_year, cursor_month = current_month
                for _ in range(comparison_count):
                    cursor_month -= 1
                    if cursor_month <= 0:
                        cursor_year -= 1
                        cursor_month = 12
                    value = monthly.get((scope_value, currency, cursor_year, cursor_month))
                    if value is not None:
                        prior_values.append(value)
                if len(prior_values) < minimum_months:
                    continue
                baseline = self._median(prior_values)
                delta = self._percent_change(actual, baseline)
                floor = float(config.get("inrPeriodFloor", 1000.0)) if currency == "INR" else abs(baseline) * 0.20
                if delta < spike_percent or actual - baseline < floor:
                    continue
                period_start = latest_stamp.replace(day=1).date().isoformat()
                period_end = latest_stamp.date().isoformat()
                display_name = display_names.get((scope_value, currency), scope_value)
                result.append(
                    self._insight(
                        key_parts=(f"{scope_type}-spike", scope_value, currency, *current_month),
                        insight_type=f"{scope_type}_spike",
                        severity="high" if delta >= spike_percent * 2 else "medium",
                        title=f"{display_name} spending is elevated",
                        summary=f"Month-to-date spend is {delta:.0f}% above the median for the same elapsed period across {len(prior_values)} prior months.",
                        row={
                            "currency": currency,
                            "amount": actual,
                            "stamp": latest_stamp,
                            "transactionKey": evidence[current_key][0],
                        },
                        baseline=baseline,
                        delta_percent=delta,
                        confidence=min(0.95, 0.60 + len(prior_values) * 0.05),
                        evidence=evidence[current_key][:50],
                        period_start=period_start,
                        period_end=period_end,
                    )
                )
        return result

    def _recurring_insights(
        self,
        rows: list[dict[str, Any]],
        config: dict[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["direction"] == "debit":
                groups[(row["vendorKey"], row["account"], row["currency"])].append(row)
        result: list[dict[str, Any]] = []
        minimum_confidence = float(config.get("recurringMinimumConfidence", 0.7) or 0.7)
        change_percent = float(config.get("recurringChangePercent", 15.0) or 15.0)
        for candidates in groups.values():
            candidates.sort(key=lambda item: item["stamp"])
            if len(candidates) < 3:
                continue
            intervals = [
                (current["stamp"] - previous["stamp"]).total_seconds() / 86400.0
                for previous, current in zip(candidates, candidates[1:])
            ]
            cadence = self._median(intervals)
            if cadence < 2 or cadence > 120:
                continue
            interval_mad = self._median([abs(value - cadence) for value in intervals])
            confidence = max(0.0, min(1.0, 1.0 - interval_mad / max(cadence, 1.0)))
            if confidence < minimum_confidence:
                continue
            latest = candidates[-1]
            amount_baseline = self._median([row["amount"] for row in candidates[:-1]])
            amount_delta = abs(self._percent_change(latest["amount"], amount_baseline))
            floor = float(config.get("inrTransactionFloor", 500.0)) if latest["currency"] == "INR" else abs(amount_baseline) * 0.20
            if amount_delta >= change_percent and abs(latest["amount"] - amount_baseline) >= floor:
                result.append(
                    self._insight(
                        key_parts=("recurring-change", latest["transactionKey"]),
                        insight_type="recurring_amount_change",
                        severity="high" if amount_delta >= change_percent * 2 else "medium",
                        title=f"Recurring charge changed at {latest['vendor']}",
                        summary=f"The latest charge differs by {amount_delta:.0f}% from its earlier median.",
                        row=latest,
                        baseline=amount_baseline,
                        delta_percent=self._percent_change(latest["amount"], amount_baseline),
                        confidence=confidence,
                        evidence=[row["transactionKey"] for row in candidates[-4:]],
                    )
                )
            overdue_days = (now - latest["stamp"]).total_seconds() / 86400.0
            if overdue_days > cadence * 1.5:
                result.append(
                    self._insight(
                        key_parts=("recurring-missed", latest["vendorKey"], latest["account"], latest["currency"], latest["stamp"].date()),
                        insight_type="recurring_missed",
                        severity="medium",
                        title=f"Expected charge not seen at {latest['vendor']}",
                        summary=f"The last charge was {overdue_days:.0f} days ago; the observed cadence is about {cadence:.0f} days.",
                        row=latest,
                        baseline=amount_baseline,
                        delta_percent=None,
                        confidence=confidence,
                        evidence=[row["transactionKey"] for row in candidates[-3:]],
                        period_end=now.date().isoformat(),
                    )
                )
        return result

    def _insight(
        self,
        *,
        key_parts: tuple[Any, ...],
        insight_type: str,
        severity: str,
        title: str,
        summary: str,
        row: dict[str, Any],
        baseline: float | None,
        delta_percent: float | None,
        confidence: float,
        evidence: list[str],
        period_start: str = "",
        period_end: str = "",
    ) -> dict[str, Any]:
        stable = "|".join(str(value) for value in key_parts)
        insight_key = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]
        stamp = row["stamp"]
        return {
            "insightKey": insight_key,
            "insightType": insight_type,
            "severity": severity,
            "title": title,
            "summary": summary,
            "currency": str(row.get("currency", "")),
            "actualValue": float(row.get("amount", 0.0) or 0.0),
            "baselineValue": baseline,
            "deltaPercent": delta_percent,
            "confidence": max(0.0, min(1.0, float(confidence))),
            "periodStart": period_start or stamp.date().isoformat(),
            "periodEnd": period_end or stamp.date().isoformat(),
            "evidenceTransactionKeys": list(dict.fromkeys(str(value) for value in evidence if value)),
        }

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
        if "transactionKey" in row:
            try:
                amount = float(row.get("amount"))
                stamp = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(amount) or amount < 0:
                return None
            if stamp.tzinfo is None:
                stamp = stamp.astimezone()
            row["amount"] = amount
            row["stamp"] = stamp
            return row

        try:
            amount = float(row.get("amount"))
            stamp = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(amount) or amount < 0:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()
        vendor = (
            str(row.get("canonical_alias", "")).strip()
            or str(row.get("canonical_vendor", "")).strip()
            or str(row.get("resolved_vendor", "")).strip()
            or str(row.get("counterparty", "")).strip()
            or "Unknown"
        )
        vendor_key = str(row.get("alias_key", "")).strip().lower() or vendor.lower()
        transaction_key = str(row.get("transaction_key", "")).strip()
        transaction_id = str(row.get("transaction_id", "")).strip()
        account = f"{str(row.get('bank_name', '')).strip()}|{str(row.get('account_suffix', '')).strip()}"
        direction = str(row.get("direction", "")).strip().lower()
        currency = str(row.get("currency", "INR")).strip().upper() or "INR"
        category = str(row.get("category", "Uncategorized")).strip() or "Uncategorized"
        row.clear()
        row.update(
            transactionKey=transaction_key,
            transactionId=transaction_id,
            account=account,
            direction=direction,
            amount=amount,
            currency=currency,
            vendor=vendor,
            vendorKey=vendor_key,
            category=category,
            stamp=stamp,
        )
        return row

    @staticmethod
    def _median(values: list[float]) -> float:
        if not values:
            return 0.0
        if np is not None and len(values) >= 512:
            return float(np.median(np.asarray(values, dtype=np.float64)))
        return float(statistics.median(values))

    @staticmethod
    def _percent_change(actual: float, baseline: float) -> float:
        if abs(baseline) < 1e-9:
            return 0.0
        return ((actual - baseline) / abs(baseline)) * 100.0


__all__ = ["AnalyticsService"]
