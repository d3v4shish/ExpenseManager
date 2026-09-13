from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from src.app.data_management import DataManagementService
from src.app.files import RuntimeFiles
from src.app.settings import SettingsService
from src.expenses.email.mail_types import SourceRecord
from src.expenses.repositories.expenses_repository import ExpensesRepository


class DataManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        with patch.dict("os.environ", {"EXPENSE_MANAGER_HOME": str(self.root / "runtime")}):
            self.files = RuntimeFiles(self.root / "app")
        self.service = DataManagementService(self.files)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_settings_round_trip_validates_all_gui_values(self) -> None:
        service = SettingsService(self.files)
        saved = service.save(
            {
                "sync": {"refreshMinutes": 20},
                "logging": {"level": "DEBUG", "maxBytes": 500_000},
                "analytics": {"sensitivity": "high", "minimumHistory": 7},
            }
        )
        self.assertEqual(saved["sync"]["refreshMinutes"], 20)
        self.assertEqual(saved["logging"]["level"], "DEBUG")
        self.assertEqual(saved["analytics"]["minimumHistory"], 7)
        with self.assertRaises(ValueError):
            service.save({"sync": {"refreshMinutes": 0}})

    def test_backup_restore_uses_manifest_checksums_and_sqlite_snapshot(self) -> None:
        self.files.write_user("app_settings.json", {"schemaVersion": 1, "marker": "before"})
        db_path = self.files.db_path("accounts/test.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE sample (value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('before')")

        backup = self.service.create_backup(self.root / "backup")
        inspection = self.service.inspect_backup(backup["path"])
        self.assertFalse(inspection["encrypted"])
        self.assertGreaterEqual(inspection["fileCount"], 2)

        self.files.write_user("app_settings.json", {"schemaVersion": 1, "marker": "after"})
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE sample SET value = 'after'")
        report = self.service.restore_backup(backup["path"])

        self.assertGreaterEqual(report["restoredFiles"], 2)
        self.assertEqual(self.files.read_user("app_settings.json")["marker"], "before")
        with sqlite3.connect(db_path) as connection:
            self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], "before")

    def test_inspection_rejects_path_traversal(self) -> None:
        archive_path = self.root / "malicious.expensemanager-backup"
        manifest = {
            "format": self.service.BACKUP_FORMAT,
            "entries": [{"path": "../escape", "sha256": "invalid"}],
        }
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("../escape", "payload")
        with self.assertRaises(ValueError):
            self.service.inspect_backup(archive_path)

    def test_storage_cleanup_keeps_important_data_by_default(self) -> None:
        self.files.write_user("important.json", {"keep": True})
        self.files.write_state("derived.json", {"generated": True})
        self.files.cache_path("cache.bin").parent.mkdir(parents=True, exist_ok=True)
        self.files.cache_path("cache.bin").write_bytes(b"cache")
        before = self.service.storage_report()
        self.assertGreater(before["totalSizeBytes"], 0)

        report = self.service.prepare_uninstall(keep_important=True)

        self.assertTrue(report["keepImportant"])
        self.assertTrue(self.files.user_path("important.json").exists())
        self.assertFalse(self.files.state_path("derived.json").exists())
        self.assertFalse(self.files.cache_path("cache.bin").exists())

    def test_backup_restore_preserves_source_revisions_and_import_journals(self) -> None:
        repository = ExpensesRepository(self.files.db_path("expenses.db"))
        source_id, _changed = repository.upsert_source_record(
            SourceRecord("csv", "csv_transaction", "row-1", "Row", "CSV", "2026-09-09T00:00:00+00:00", {"sourceUri": "/tmp/transactions.csv"}, "hash-1", "/tmp/transactions.csv")
        )
        journal_id = repository.start_import_journal("csv", force_full=False)
        repository.finish_import_journal(journal_id, status="completed", records_seen=1, records_changed=1)
        backup = self.service.create_backup(self.root / "source-backup")
        repository.delete_source_records("csv")
        self.assertEqual(repository.count_source_records(), 0)
        self.service.restore_backup(backup["path"])
        restored = ExpensesRepository(self.files.db_path("expenses.db"))
        self.assertEqual(restored.count_source_records(), 1)
        self.assertEqual(len(restored.list_source_revisions(source_id)), 1)
        self.assertEqual(restored.list_import_journals(provider_id="csv")[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
