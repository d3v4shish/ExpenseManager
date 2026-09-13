from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from src.app.cli import EXIT_GUI_SETUP_REQUIRED, run_cli
from src.expenses.bootstrap import build_runtime


class CliReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runtime_home = self.root / "runtime"
        self.env = mock.patch.dict("os.environ", {"EXPENSE_MANAGER_HOME": str(self.runtime_home)})
        self.env.start()
        runtime = build_runtime(self.root)
        runtime.repositories["expenses"].upsert_transactions(
            [
                self._transaction("coffee", "2026-08-10T10:00:00+05:30", "Coffee Shop", "coffee", 120.0),
                self._transaction("groceries", "2026-09-02T09:00:00+05:30", "Grocer", "grocer", 300.0),
            ]
        )

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_dashboard_and_analytics_are_versioned_and_bounded(self) -> None:
        code, payload = self._run(["report", "dashboard", "--months", "2", "--top", "1", "--format", "json"])

        self.assertEqual(code, 0)
        self.assertEqual(payload["schemaVersion"], "expense-manager-report-v1")
        self.assertEqual(payload["report"], "dashboard")
        self.assertEqual(len(payload["data"]["analytics"]["topVendors"]), 1)
        self.assertEqual(len(payload["data"]["analytics"]["previousMonths"]), 2)
        self.assertEqual(payload["data"]["analytics"]["selectedMetrics"]["debitTotal"], 420.0)

    def test_transaction_report_hides_raw_counterparty_and_keeps_provenance_safe(self) -> None:
        code, payload = self._run(["--format", "json", "report", "transactions", "list"])

        self.assertEqual(code, 0)
        row = payload["data"]["rows"][0]
        self.assertNotIn("rawCounterparty", row)
        self.assertEqual(row["canonicalVendor"], "Grocer")

    def test_vendor_report_contains_vendor_alias_and_category_aggregates(self) -> None:
        code, payload = self._run(["report", "vendors", "get", "--identity", "Grocer", "--format", "json"])

        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["vendorGroup"]["totalDebit"], 300.0)
        self.assertEqual(payload["data"]["aliasGroup"]["transactionCount"], 1)
        self.assertEqual(payload["data"]["categoryGroup"]["categoryName"], "Uncategorized")

    def test_setup_guidance_is_non_mutating_and_has_dedicated_exit_code(self) -> None:
        code, payload = self._run(["sources", "add-thunderbird", "--format", "json"])

        self.assertEqual(code, EXIT_GUI_SETUP_REQUIRED)
        thunderbird = next(item for item in payload["data"]["providers"] if item["type"] == "thunderbird")
        self.assertEqual(thunderbird["setup"], "gui_required")
        self.assertIn("ExpenseManager gui", thunderbird["message"])
        self.assertFalse((self.runtime_home / "config" / "email_accounts.json").exists())

    def _run(self, args: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = run_cli(args, self.root)
        return code, json.loads(output.getvalue())

    @staticmethod
    def _transaction(key: str, timestamp: str, vendor: str, alias_key: str, amount: float) -> dict:
        return {
            "transactionKey": key,
            "providerId": "fixture",
            "externalId": key,
            "parserId": "fixture",
            "bankName": "Test Bank",
            "accountSuffix": "1234",
            "direction": "debit",
            "amount": amount,
            "currency": "INR",
            "transactionId": key,
            "counterparty": vendor,
            "rawCounterparty": vendor,
            "resolvedVendor": vendor,
            "canonicalVendor": vendor,
            "canonicalAlias": vendor,
            "aliasKey": alias_key,
            "category": "Uncategorized",
            "subcategory": "",
            "vendorMatchSource": "fixture",
            "timestamp": timestamp,
            "year": int(timestamp[:4]),
            "month": int(timestamp[5:7]),
            "title": key,
            "sender": "alerts@example.com",
        }


if __name__ == "__main__":
    unittest.main()
