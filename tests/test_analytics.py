from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.expenses.repositories.expenses_repository import ExpensesRepository
from src.expenses.services.analytics import AnalyticsService


class AnalyticsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = ExpensesRepository(Path(self.temp_dir.name) / "expenses.db")
        self.service = AnalyticsService(self.repository)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_generate_finds_duplicate_unusual_and_recurring_change(self) -> None:
        rows = [
            self._row(f"txn-{index}", f"2026-{index + 1:02d}-01T10:00:00+00:00", amount)
            for index, amount in enumerate([100, 105, 98, 102, 101, 1000])
        ]
        rows.append(self._row("duplicate", "2026-06-01T10:05:00+00:00", 1000, transaction_id="txn-5"))

        insights = self.service.generate(
            rows,
            now=datetime.fromisoformat("2026-06-02T00:00:00+00:00"),
        )
        types = {item["insightType"] for item in insights}

        self.assertIn("possible_duplicate", types)
        self.assertIn("unusual_amount", types)
        self.assertIn("recurring_amount_change", types)
        duplicate = next(item for item in insights if item["insightType"] == "possible_duplicate")
        self.assertEqual(duplicate["severity"], "high")

    def test_period_spikes_are_currency_isolated_and_explained(self) -> None:
        rows = []
        for month in range(1, 5):
            amount = 4000 if month == 4 else 1000
            rows.append(self._row(f"inr-{month}", f"2026-{month:02d}-05T10:00:00+00:00", amount, category="Food"))
            rows.append(self._row(f"usd-{month}", f"2026-{month:02d}-05T11:00:00+00:00", 10, currency="USD", category="Food"))

        insights = self.service.generate(rows, now=datetime.fromisoformat("2026-04-06T00:00:00+00:00"))
        spikes = [item for item in insights if item["insightType"] in {"category_spike", "vendor_spike"}]

        self.assertTrue(spikes)
        self.assertTrue(all(item["currency"] == "INR" for item in spikes))
        self.assertTrue(all("prior months" in item["summary"] for item in spikes))

    def test_repository_preserves_dismissal_and_resolves_missing_insight(self) -> None:
        payload = {
            "insightKey": "stable-key",
            "insightType": "unusual_amount",
            "severity": "medium",
            "title": "Unusual amount",
            "summary": "Explanation",
            "currency": "INR",
            "actualValue": 1000,
            "baselineValue": 100,
            "deltaPercent": 900,
            "confidence": 0.9,
            "periodStart": "2026-01-01",
            "periodEnd": "2026-01-01",
            "evidenceTransactionKeys": [],
        }
        self.repository.replace_insights([payload], algorithm_version="v1")
        self.repository.set_insight_status("stable-key", "dismissed")
        self.repository.replace_insights([payload], algorithm_version="v1")
        self.assertEqual(self.repository.get_insight("stable-key")["status"], "dismissed")

        report = self.repository.replace_insights([], algorithm_version="v1")
        self.assertEqual(report["resolved"], 1)
        self.assertEqual(self.repository.get_insight("stable-key")["status"], "resolved")

    @staticmethod
    def _row(
        key: str,
        timestamp: str,
        amount: float,
        *,
        transaction_id: str | None = None,
        currency: str = "INR",
        category: str = "Shopping",
    ) -> dict:
        return {
            "transaction_key": key,
            "bank_name": "Test Bank",
            "account_suffix": "1234",
            "direction": "debit",
            "amount": amount,
            "currency": currency,
            "transaction_id": transaction_id if transaction_id is not None else key,
            "counterparty": "Store",
            "resolved_vendor": "Store",
            "canonical_vendor": "Store",
            "canonical_alias": "Store",
            "alias_key": "store",
            "category": category,
            "timestamp": timestamp,
        }


if __name__ == "__main__":
    unittest.main()
