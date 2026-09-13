from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.expenses.repositories.expenses_repository import ExpensesRepository
from src.expenses.repositories.sqlite_db import ensure_versioned_schema


class SchemaMigrationTests(unittest.TestCase):
    def test_existing_transaction_survives_versioned_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE legacy_marker (value TEXT)")
                connection.execute("INSERT INTO legacy_marker VALUES ('keep')")

            repository = ExpensesRepository(path)

            with repository._connect() as connection:
                self.assertEqual(connection.execute("SELECT value FROM legacy_marker").fetchone()[0], "keep")
                version = connection.execute(
                    "SELECT version FROM app_schema_versions WHERE component = 'expenses'"
                ).fetchone()[0]
                self.assertEqual(version, ExpensesRepository.SCHEMA_VERSION)

    def test_failed_migration_restores_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollback.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE marker (value TEXT)")
                connection.execute("INSERT INTO marker VALUES ('before')")

            def broken(connection: sqlite3.Connection) -> None:
                connection.execute("UPDATE marker SET value = 'damaged'")
                connection.execute("CREATE TABLE partial (value TEXT)")
                raise RuntimeError("planned failure")

            with self.assertRaises(RuntimeError):
                ensure_versioned_schema(path, component="test", target_version=1, migrate=broken)

            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "before")
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'partial'"
                ).fetchone()
                self.assertIsNone(table)

    def test_v4_source_records_gain_persisted_schema_version_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "v4.db"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE expense_source_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id TEXT NOT NULL,
                        record_type TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL,
                        sender TEXT NOT NULL, received_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                        content_hash TEXT NOT NULL, source_uri TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        UNIQUE(provider_id, record_type, external_id)
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO expense_source_records (provider_id, record_type, external_id, title, sender, received_at, payload_json, content_hash, source_uri, created_at, updated_at) VALUES ('csv', 'csv_transaction', 'row-1', 'Row', 'CSV', '2026-09-09T00:00:00+00:00', '{}', 'hash', '/tmp/a.csv', '2026-09-09T00:00:00+00:00', '2026-09-09T00:00:00+00:00')"
                )
                connection.execute("CREATE TABLE app_schema_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT NOT NULL)")
                connection.execute("INSERT INTO app_schema_versions VALUES ('expenses', 4, '2026-09-09T00:00:00+00:00')")
            repository = ExpensesRepository(path)
            row = repository.list_source_records()[0]
            self.assertEqual(row["externalId"], "row-1")
            self.assertEqual(row["schemaVersion"], 1)


if __name__ == "__main__":
    unittest.main()
