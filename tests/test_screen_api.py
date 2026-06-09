from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.app.files import RuntimeFiles
from src.expenses.ui.screen_api import ExpensesScreenApi


class _FakeWindow:
    def __init__(self) -> None:
        self.reload_calls: list[tuple[str, ...]] = []
        self.active_jobs: set[str] = set()

    def reload_cards(self, card_ids) -> None:
        self.reload_calls.append(tuple(card_ids))

    def reload_all_cards(self) -> None:
        self.reload_calls.append(("ALL",))


class _FakeRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def describe_current_db(self, *, provider_id: str = "thunderbird_local") -> dict:
        return {
            "dbPath": str(self.db_path),
            "providerId": provider_id,
            "hasCachedData": False,
        }

    def describe_db_path(self, db_path: Path, *, provider_id: str = "thunderbird_local") -> dict:
        return {
            "dbPath": str(Path(db_path)),
            "providerId": provider_id,
            "hasCachedData": False,
        }


class _FakeExpensesService:
    def __init__(self, repository: _FakeRepository) -> None:
        self.repository = repository
        self.switched_paths: list[Path] = []
        self.ignored_updates: list[tuple[str, bool]] = []
        self.reparse_calls = 0
        self.reconcile_calls = 0

    def switch_expenses_database(self, db_path: Path) -> dict:
        next_path = Path(db_path)
        self.repository.db_path = next_path
        self.switched_paths.append(next_path)
        return {
            "dbPath": str(next_path),
            "hadExistingDb": False,
            "dbStatus": self.repository.describe_current_db(),
        }

    def set_transaction_ignored(self, transaction_key: str, ignored: bool) -> None:
        self.ignored_updates.append((transaction_key, ignored))

    def reparse_candidate_mails(self) -> dict:
        self.reparse_calls += 1
        return {"reparsedRecords": 4}

    def reconcile_vendor_mappings(self) -> dict:
        self.reconcile_calls += 1
        return {"changedRows": 2, "totalRows": 8}


class _FakeBankRuleCatalog:
    def parse_override_text(self, raw_text: str) -> dict:
        return json.loads(raw_text or "{}")

    def save_overrides(self, overrides: dict) -> dict:
        return dict(overrides)

    def load_effective(self, overrides: dict) -> dict:
        return {"banks": list(overrides.get("banks", []))}

    def payload_text(self, payload: dict) -> str:
        return json.dumps(payload, indent=2)


class ExpensesScreenApiTests(unittest.TestCase):
    def test_reload_scopes_stay_targeted(self) -> None:
        repository = _FakeRepository(Path("D:/tmp/expenses.db"))
        expenses_service = _FakeExpensesService(repository)
        screen_api = ExpensesScreenApi(
            expenses_service=expenses_service,
            expenses_repository=repository,
            vendor_catalog_service=None,
            bank_rule_catalog=_FakeBankRuleCatalog(),
            files=None,
        )
        window = _FakeWindow()
        screen_api.attach_window(window)

        screen_api.toggle_ignored("txn-1", True)
        screen_api.sync_vendor_mappings()
        payload = screen_api.save_bank_rule_overrides('{"banks": []}')

        self.assertEqual(expenses_service.ignored_updates, [("txn-1", True)])
        self.assertEqual(expenses_service.reconcile_calls, 1)
        self.assertEqual(expenses_service.reparse_calls, 1)
        self.assertEqual(payload["reparse"]["reparsedRecords"], 4)
        self.assertEqual(
            window.reload_calls,
            [
                ("panel.expenses", "panel.expenses_tab"),
                ("panel.expenses", "panel.expenses_tab"),
                ("panel.expenses", "panel.expenses_debug", "panel.expenses_tab"),
            ],
        )

    def test_import_legacy_data_uses_fresh_start_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "app"
            source_dir = Path(temp_dir) / "source"
            app_home = Path(temp_dir) / "runtime"
            with unittest.mock.patch.dict("os.environ", {"EXPENSE_MANAGER_HOME": str(app_home)}):
                files = RuntimeFiles(root_dir)
            repository = _FakeRepository(files.db_path("expenses.db"))
            expenses_service = _FakeExpensesService(repository)
            screen_api = ExpensesScreenApi(
                expenses_service=expenses_service,
                expenses_repository=repository,
                vendor_catalog_service=None,
                files=files,
            )
            source_dir.mkdir(parents=True, exist_ok=True)

            report = screen_api.import_legacy_data(str(source_dir))

            self.assertFalse(report["runtimeChanged"])
            self.assertFalse(report["performed"])
            self.assertFalse(expenses_service.switched_paths)
            self.assertIn("legacy import is disabled", " ".join(report["notes"]))
            self.assertFalse((root_dir / "user").exists())
            self.assertFalse((root_dir / "var").exists())


if __name__ == "__main__":
    unittest.main()
