from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from src.app.cli import run_cli


class SourcesCliTests(unittest.TestCase):
    def test_csv_add_list_validate_and_refresh_use_only_temp_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "transactions.csv"
            csv_path.write_text("Date,Amount,Direction,Description\n2026-09-09T10:00:00+05:30,10,debit,Cafe\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"EXPENSE_MANAGER_HOME": str(root / "runtime")}):
                added = self._run(["sources", "add-csv", "--id", "csv", "--path", str(csv_path), "--mapping-json", '{"date":"Date","amount":"Amount","direction":"Direction","description":"Description"}'], root)
                self.assertEqual(added[0], 0)
                self.assertEqual(added[1]["added"]["id"], "csv")
                listed = self._run(["sources", "list"], root)
                self.assertEqual(listed[1]["providers"][0]["type"], "csv")
                self.assertEqual(self._run(["sources", "validate"], root)[0], 0)
                refreshed = self._run(["sources", "refresh"], root)
                self.assertEqual(refreshed[0], 0)
                self.assertTrue(refreshed[1]["materialized"])
                inspected = self._run(["sources", "inspect", "--id", "csv"], root)
                self.assertEqual(inspected[1]["records"][0]["providerId"], "csv")
                disabled = self._run(["sources", "set-enabled", "--id", "csv", "--disabled"], root)
                self.assertFalse(disabled[1]["enabled"])
                validation = self._run(["sources", "validate"], root)
                self.assertEqual(validation[0], 0)
                self.assertIn("validation skipped", validation[1]["providers"][0]["message"])
                enabled = self._run(["sources", "set-enabled", "--id", "csv", "--enabled"], root)
                self.assertTrue(enabled[1]["enabled"])
                deleted = self._run(["sources", "delete-data", "--id", "csv", "--confirm"], root)
                self.assertEqual(deleted[1]["deletedSourceRecords"], 1)

    def test_reconciliation_commands_keep_conflicts_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.csv"
            second = root / "second.csv"
            header = "Date,Amount,Direction,Description,Reference,Account,Bank,Currency\n"
            first.write_text(header + "2026-09-09T10:00:00+05:30,10,debit,Cafe,REF-1,1234,Axis,INR\n", encoding="utf-8")
            second.write_text(header + "2026-09-09T10:00:00+05:30,20,debit,Cafe Updated,REF-1,1234,Axis,INR\n", encoding="utf-8")
            mapping = '{"date":"Date","amount":"Amount","direction":"Direction","description":"Description","reference":"Reference","account":"Account","bank":"Bank","currency":"Currency"}'
            with mock.patch.dict("os.environ", {"EXPENSE_MANAGER_HOME": str(root / "runtime")}):
                self.assertEqual(self._run(["sources", "add-csv", "--id", "first", "--path", str(first), "--mapping-json", mapping], root)[0], 0)
                self.assertEqual(self._run(["sources", "add-csv", "--id", "second", "--path", str(second), "--mapping-json", mapping], root)[0], 0)
                self.assertEqual(self._run(["sources", "refresh"], root)[0], 0)
                listed = self._run(["reconciliation", "list"], root)
                self.assertEqual(listed[0], 0)
                self.assertEqual(listed[1]["policy"]["exactConflictPolicy"], "manual")
                conflict = listed[1]["conflicts"][0]
                self.assertEqual(conflict["status"], "needs_review")
                selected = next(item["signature"] for item in conflict["candidates"] if item["amount"] == 20.0)
                resolved = self._run(["reconciliation", "resolve", "--key", conflict["reconciliationKey"], "--signature", selected], root)
                self.assertEqual(resolved[0], 0)
                self.assertEqual(resolved[1]["conflicts"], 1)
                policy = self._run(["reconciliation", "set-policy", "--mode", "provider_priority", "--prefer-provider", "second"], root)
                self.assertEqual(policy[0], 0)
                self.assertEqual(policy[1]["policy"]["exactConflictPolicy"], "provider_priority")

    def _run(self, args: list[str], root: Path) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = run_cli(["--format", "json", *args], root)
        payload = json.loads(output.getvalue())
        return code, dict(payload.get("result", payload))


if __name__ == "__main__":
    unittest.main()
