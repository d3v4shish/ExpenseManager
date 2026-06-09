from __future__ import annotations

import json
import logging
import re
import sqlite3
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from src.expenses.email.mail_types import ParsedFact, SourceRecord
from src.expenses.repositories.sqlite_db import connect_sqlite


class ExpensesRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.db_path)

    def create_rebuild_snapshot(self) -> Path:
        with self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        snapshot_dir = Path(tempfile.mkdtemp(prefix="expenses_rebuild_snapshot_", dir=self.db_path.parent))
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{self.db_path}{suffix}")
            if source.exists():
                shutil.copy2(source, snapshot_dir / source.name)
        return snapshot_dir

    def restore_rebuild_snapshot(self, snapshot_dir: Path) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        for suffix in ("", "-wal", "-shm"):
            destination = Path(f"{self.db_path}{suffix}")
            source = snapshot_dir / destination.name
            if destination.exists():
                destination.unlink()
            if source.exists():
                shutil.copy2(source, destination)

    def switch_db_path(self, db_path: Path) -> None:
        """Retarget the repository to a different expenses database file."""

        next_path = Path(db_path)
        if next_path == self.db_path:
            self.ensure_schema()
            return
        self.logger.info("Switching expenses repository db_path old=%s new=%s", self.db_path, next_path)
        self.db_path = next_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def describe_current_db(self, *, provider_id: str = "thunderbird_local") -> dict[str, Any]:
        """Return a summary of the currently active expenses DB."""

        return self.describe_db_path(self.db_path, provider_id=provider_id)

    @classmethod
    def describe_db_path(cls, db_path: Path, *, provider_id: str = "thunderbird_local") -> dict[str, Any]:
        """Return a cache/status summary for one expenses DB path without mutating it."""

        path = Path(db_path)
        exists = path.exists()
        summary = {
            "dbPath": str(path),
            "exists": exists,
            "dbSizeBytes": int(path.stat().st_size) if exists else 0,
            "transactionCount": 0,
            "sourceRecordCount": 0,
            "hasCachedData": False,
            "lastCheckpointAt": "",
            "lastReceivedAt": "",
            "materializationVersion": "",
            "providerId": provider_id,
            "error": "",
        }
        if not exists:
            return summary

        try:
            with cls._connect_readonly(path) as conn:
                transaction_count = cls._count_table_rows(conn, "expense_transactions")
                source_record_count = cls._count_table_rows(conn, "expense_source_records")
                materialization_version = cls._metadata_value(conn, "expenses_materialization_version")
                checkpoint = cls._provider_checkpoint(conn, provider_id)
        except sqlite3.Error as exc:
            summary["error"] = str(exc)
            return summary

        summary["transactionCount"] = transaction_count
        summary["sourceRecordCount"] = source_record_count
        summary["hasCachedData"] = bool(transaction_count > 0 or source_record_count > 0)
        summary["materializationVersion"] = materialization_version
        summary["lastCheckpointAt"] = str(checkpoint.get("updatedAt", ""))
        summary["lastReceivedAt"] = str(checkpoint.get("lastReceivedAt", ""))
        return summary

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS expense_transactions (
                    transaction_key TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    parser_id TEXT NOT NULL,
                    bank_name TEXT NOT NULL,
                    account_suffix TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    amount REAL,
                    currency TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    counterparty TEXT NOT NULL,
                    raw_counterparty TEXT NOT NULL DEFAULT '',
                    resolved_vendor TEXT NOT NULL DEFAULT '',
                    canonical_vendor TEXT NOT NULL DEFAULT '',
                    canonical_alias TEXT NOT NULL DEFAULT '',
                    alias_key TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'Uncategorized',
                    subcategory TEXT NOT NULL DEFAULT '',
                    vendor_match_source TEXT NOT NULL DEFAULT 'fallback',
                    timestamp TEXT NOT NULL,
                    year INTEGER,
                    month INTEGER,
                    title TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ignored_transactions (
                    transaction_key TEXT PRIMARY KEY,
                    ignored_at TEXT NOT NULL,
                    FOREIGN KEY(transaction_key) REFERENCES expense_transactions(transaction_key)
                );

                CREATE TABLE IF NOT EXISTS expenses_metadata (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS expense_source_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider_id TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider_id, record_type, external_id)
                );

                CREATE TABLE IF NOT EXISTS expense_parsed_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_record_id INTEGER NOT NULL,
                    fact_type TEXT NOT NULL,
                    parser_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_record_id, fact_type, parser_id, payload_json),
                    FOREIGN KEY(source_record_id) REFERENCES expense_source_records(id)
                );

                CREATE TABLE IF NOT EXISTS expense_parse_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_record_id INTEGER NOT NULL,
                    parser_id TEXT NOT NULL,
                    matched_rule_id TEXT NOT NULL DEFAULT '',
                    matched_rule_name TEXT NOT NULL DEFAULT '',
                    parse_status TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    reason_text TEXT NOT NULL DEFAULT '',
                    facts_count INTEGER NOT NULL DEFAULT 0,
                    preview_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_record_id, parser_id),
                    FOREIGN KEY(source_record_id) REFERENCES expense_source_records(id)
                );

                CREATE TABLE IF NOT EXISTS expense_provider_checkpoints (
                    provider_id TEXT PRIMARY KEY,
                    last_received_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_expense_transactions_timestamp
                ON expense_transactions(timestamp DESC);

                CREATE INDEX IF NOT EXISTS idx_expense_transactions_bank_account
                ON expense_transactions(bank_name, account_suffix);

                CREATE INDEX IF NOT EXISTS idx_expense_transactions_counterparty
                ON expense_transactions(counterparty);

                CREATE INDEX IF NOT EXISTS idx_expense_transactions_direction
                ON expense_transactions(direction, timestamp DESC);

                CREATE INDEX IF NOT EXISTS idx_expense_source_records_received_at
                ON expense_source_records(received_at DESC);

                CREATE INDEX IF NOT EXISTS idx_expense_parsed_facts_type
                ON expense_parsed_facts(fact_type, parser_id);

                CREATE INDEX IF NOT EXISTS idx_expense_parse_attempts_status
                ON expense_parse_attempts(parse_status, reason_code);
                """
            )
            self._ensure_transaction_columns(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_expense_transactions_vendor
                ON expense_transactions(canonical_vendor, resolved_vendor)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_expense_transactions_alias_key
                ON expense_transactions(alias_key, canonical_alias)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_expense_transactions_year_month_timestamp
                ON expense_transactions(year, month, timestamp DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_expense_transactions_year_month_direction
                ON expense_transactions(year, month, direction, timestamp DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_expense_transactions_alias_timestamp
                ON expense_transactions(alias_key, timestamp DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_expense_transactions_category_timestamp
                ON expense_transactions(category, timestamp DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_expense_source_records_provider_received
                ON expense_source_records(provider_id, received_at DESC)
                """
            )
            if self._ensure_transaction_search_index(conn):
                self._backfill_transaction_search_index(conn)
        self.logger.debug("Ensured expenses repository schema db_path=%s", self.db_path)

    def _ensure_transaction_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(expense_transactions)").fetchall()
        }
        additions = {
            "raw_counterparty": "TEXT NOT NULL DEFAULT ''",
            "resolved_vendor": "TEXT NOT NULL DEFAULT ''",
            "canonical_vendor": "TEXT NOT NULL DEFAULT ''",
            "canonical_alias": "TEXT NOT NULL DEFAULT ''",
            "alias_key": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT 'Uncategorized'",
            "subcategory": "TEXT NOT NULL DEFAULT ''",
            "vendor_match_source": "TEXT NOT NULL DEFAULT 'fallback'",
        }
        for column_name, column_sql in additions.items():
            if column_name in existing:
                continue
            conn.execute(f"ALTER TABLE expense_transactions ADD COLUMN {column_name} {column_sql}")

    def _ensure_transaction_search_index(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS expense_transaction_search
                USING fts5(transaction_key UNINDEXED, search_text, tokenize='unicode61')
                """
            )
        except sqlite3.Error as exc:
            self.logger.debug("SQLite FTS5 is unavailable for expense transaction search: %s", exc)
            return False
        return True

    def _backfill_transaction_search_index(self, conn: sqlite3.Connection) -> None:
        if not self._transaction_search_index_available(conn):
            return
        transaction_row = conn.execute("SELECT COUNT(*) AS count FROM expense_transactions").fetchone()
        search_row = conn.execute("SELECT COUNT(*) AS count FROM expense_transaction_search").fetchone()
        transaction_count = int(transaction_row["count"] if transaction_row is not None else 0)
        search_count = int(search_row["count"] if search_row is not None else 0)
        if transaction_count == search_count:
            return
        conn.execute("DELETE FROM expense_transaction_search")
        rows = conn.execute(
            """
            SELECT
                transaction_key, bank_name, account_suffix, transaction_id,
                counterparty, raw_counterparty, resolved_vendor, canonical_vendor,
                canonical_alias, alias_key, category, subcategory, title, sender
            FROM expense_transactions
            """
        ).fetchall()
        self._replace_transaction_search_rows(conn, [dict(row) for row in rows])

    def _transaction_search_index_available(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'expense_transaction_search'
            """
        ).fetchone()
        return row is not None

    def _replace_transaction_search_rows(self, conn: sqlite3.Connection, transactions: list[Any]) -> None:
        if not transactions or not self._transaction_search_index_available(conn):
            return
        rows: list[tuple[str, str]] = []
        for item in transactions:
            transaction_key = str(self._transaction_field(item, "transactionKey", "transaction_key")).strip()
            if not transaction_key:
                continue
            rows.append((transaction_key, self._transaction_search_text(item)))
        if not rows:
            return
        conn.executemany(
            "DELETE FROM expense_transaction_search WHERE transaction_key = ?",
            [(transaction_key,) for transaction_key, _ in rows],
        )
        conn.executemany(
            """
            INSERT INTO expense_transaction_search (transaction_key, search_text)
            VALUES (?, ?)
            """,
            rows,
        )

    def _should_use_transaction_fts(self, conn: sqlite3.Connection, search_text: str) -> bool:
        return bool(self._fts_query(search_text)) and self._transaction_search_index_available(conn)

    @staticmethod
    def _fts_query(search_text: str) -> str:
        tokens = re.findall(r"\w+", str(search_text or "").lower())
        return " ".join(f"{token}*" for token in tokens[:8])

    @staticmethod
    def _transaction_field(item: Any, camel_key: str, snake_key: str) -> Any:
        if isinstance(item, sqlite3.Row):
            item = dict(item)
        if isinstance(item, dict):
            value = item.get(camel_key)
            if value in (None, ""):
                value = item.get(snake_key, "")
            return value
        return ""

    def _transaction_search_text(self, item: Any) -> str:
        fields = [
            self._transaction_field(item, "counterparty", "counterparty"),
            self._transaction_field(item, "rawCounterparty", "raw_counterparty"),
            self._transaction_field(item, "resolvedVendor", "resolved_vendor"),
            self._transaction_field(item, "canonicalVendor", "canonical_vendor"),
            self._transaction_field(item, "canonicalAlias", "canonical_alias"),
            self._transaction_field(item, "aliasKey", "alias_key"),
            self._transaction_field(item, "transactionId", "transaction_id"),
            self._transaction_field(item, "title", "title"),
            self._transaction_field(item, "sender", "sender"),
            self._transaction_field(item, "bankName", "bank_name"),
            self._transaction_field(item, "accountSuffix", "account_suffix"),
            self._transaction_field(item, "category", "category"),
            self._transaction_field(item, "subcategory", "subcategory"),
        ]
        return " ".join(str(value).strip().lower() for value in fields if str(value or "").strip())

    def upsert_transactions(self, transactions: list[dict[str, Any]], *, prune_missing: bool = False) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._connect() as conn:
            for item in transactions:
                conn.execute(
                    """
                    INSERT INTO expense_transactions (
                        transaction_key, provider_id, external_id, parser_id, bank_name, account_suffix,
                        direction, amount, currency, transaction_id, counterparty, raw_counterparty,
                        resolved_vendor, canonical_vendor, canonical_alias, alias_key,
                        category, subcategory, vendor_match_source,
                        timestamp, year, month, title, sender, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(transaction_key) DO UPDATE SET
                        provider_id         = excluded.provider_id,
                        external_id         = excluded.external_id,
                        parser_id           = excluded.parser_id,
                        bank_name           = excluded.bank_name,
                        account_suffix      = excluded.account_suffix,
                        direction           = excluded.direction,
                        amount              = excluded.amount,
                        currency            = excluded.currency,
                        transaction_id      = excluded.transaction_id,
                        counterparty        = excluded.counterparty,
                        raw_counterparty    = excluded.raw_counterparty,
                        resolved_vendor     = excluded.resolved_vendor,
                        canonical_vendor    = excluded.canonical_vendor,
                        canonical_alias     = excluded.canonical_alias,
                        alias_key           = excluded.alias_key,
                        category            = excluded.category,
                        subcategory         = excluded.subcategory,
                        vendor_match_source = excluded.vendor_match_source,
                        timestamp           = excluded.timestamp,
                        year                = excluded.year,
                        month               = excluded.month,
                        title               = excluded.title,
                        sender              = excluded.sender,
                        updated_at          = excluded.updated_at
                    """,
                    (
                        str(item.get("transactionKey", "")).strip(),
                        str(item.get("providerId", "")).strip(),
                        str(item.get("externalId", "")).strip(),
                        str(item.get("parserId", "")).strip(),
                        str(item.get("bankName", "")).strip(),
                        str(item.get("accountSuffix", "")).strip(),
                        str(item.get("direction", "")).strip(),
                        self._coerce_float(item.get("amount")),
                        str(item.get("currency", "INR")).strip() or "INR",
                        str(item.get("transactionId", "")).strip(),
                        str(item.get("counterparty", "")).strip(),
                        str(item.get("rawCounterparty", "")).strip(),
                        str(item.get("resolvedVendor", "")).strip(),
                        str(item.get("canonicalVendor", "")).strip(),
                        str(item.get("canonicalAlias", "")).strip(),
                        str(item.get("aliasKey", "")).strip(),
                        str(item.get("category", "Uncategorized")).strip() or "Uncategorized",
                        str(item.get("subcategory", "")).strip(),
                        str(item.get("vendorMatchSource", "fallback")).strip() or "fallback",
                        str(item.get("timestamp", "")).strip(),
                        self._coerce_int(item.get("year")),
                        self._coerce_int(item.get("month")),
                        str(item.get("title", "")).strip(),
                        str(item.get("sender", "")).strip(),
                        now,
                        now,
                    ),
                )
            self._replace_transaction_search_rows(conn, transactions)
            if prune_missing:
                conn.execute("DROP TABLE IF EXISTS temp.current_expense_keys")
                conn.execute("CREATE TEMP TABLE current_expense_keys (transaction_key TEXT PRIMARY KEY)")
                conn.executemany(
                    "INSERT OR IGNORE INTO current_expense_keys (transaction_key) VALUES (?)",
                    [(str(item.get("transactionKey", "")).strip(),) for item in transactions],
                )
                conn.execute(
                    """
                    DELETE FROM expense_transactions
                    WHERE transaction_key NOT IN (SELECT transaction_key FROM current_expense_keys)
                    """
                )
                conn.execute(
                    """
                    DELETE FROM ignored_transactions
                    WHERE transaction_key NOT IN (SELECT transaction_key FROM expense_transactions)
                    """
                )
                if self._transaction_search_index_available(conn):
                    conn.execute(
                        """
                        DELETE FROM expense_transaction_search
                        WHERE transaction_key NOT IN (SELECT transaction_key FROM expense_transactions)
                        """
                    )
                conn.execute("DROP TABLE IF EXISTS temp.current_expense_keys")

    def replace_transactions(self, transactions: list[dict[str, Any]]) -> None:
        self.upsert_transactions(transactions, prune_missing=True)

    def list_transactions(self, *, include_ignored: bool = True) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = self._fetch_transaction_rows(conn, include_ignored=include_ignored)
        return [self._transaction_dict(row) for row in rows]

    def list_available_years(self) -> list[int]:
        """Return years that have stored transactions, newest first."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT year
                FROM expense_transactions
                WHERE year IS NOT NULL
                ORDER BY year DESC
                """
            ).fetchall()
        return [int(row["year"]) for row in rows if row["year"] is not None]

    def list_transactions_page(
        self,
        *,
        year: int | None = None,
        month: int | None = None,
        include_ignored: bool = False,
        search_text: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one bounded transaction page for UI views."""

        safe_limit = max(1, min(int(limit or 500), 5000))
        safe_offset = max(0, int(offset or 0))
        with self._connect() as conn:
            use_fts_search = self._should_use_transaction_fts(conn, search_text)
            clauses, params = self._transaction_filter_clauses(
                include_ignored=include_ignored,
                year=year,
                month=month,
                search_text=search_text,
                use_fts_search=use_fts_search,
            )
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM expense_transactions et
                LEFT JOIN ignored_transactions it ON it.transaction_key = et.transaction_key
                {where_sql}
                """,
                params,
            ).fetchone()
            rows = self._fetch_transaction_rows(
                conn,
                include_ignored=include_ignored,
                year=year,
                month=month,
                search_text=search_text,
                limit=safe_limit,
                offset=safe_offset,
                use_fts_search=use_fts_search,
            )
        total = int(total_row["count"] if total_row is not None else 0)
        return {
            "rows": [self._transaction_dict(row) for row in rows],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
            "hasMore": safe_offset + safe_limit < total,
        }

    def list_vendor_summary(
        self,
        *,
        year: int | None = None,
        month: int | None = None,
        include_ignored: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return grouped vendor spend directly from SQLite."""

        safe_limit = max(1, min(int(limit or 500), 5000))
        clauses, params = self._transaction_filter_clauses(
            include_ignored=include_ignored,
            year=year,
            month=month,
        )
        clauses.append("lower(et.direction) = 'debit'")
        where_sql = f"WHERE {' AND '.join(clauses)}"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(et.alias_key, ''), lower(et.canonical_alias), lower(et.canonical_vendor), 'unknown') AS vendor_key,
                    COALESCE(NULLIF(et.canonical_alias, ''), NULLIF(et.canonical_vendor, ''), NULLIF(et.resolved_vendor, ''), 'Unknown') AS vendor,
                    COALESCE(NULLIF(et.canonical_vendor, ''), NULLIF(et.resolved_vendor, ''), 'Unknown') AS canonical_vendor,
                    COALESCE(NULLIF(et.canonical_alias, ''), NULLIF(et.canonical_vendor, ''), 'Unknown') AS canonical_alias,
                    COALESCE(NULLIF(et.category, ''), 'Uncategorized') AS category,
                    SUM(COALESCE(et.amount, 0)) AS amount,
                    COUNT(*) AS count
                FROM expense_transactions et
                LEFT JOIN ignored_transactions it ON it.transaction_key = et.transaction_key
                {where_sql}
                GROUP BY vendor_key
                ORDER BY amount DESC, count DESC, vendor COLLATE NOCASE ASC
                LIMIT ?
                """,
                [*params, safe_limit],
            ).fetchall()
        return [
            {
                "vendor": str(row["vendor"] or "Unknown"),
                "vendorKey": str(row["vendor_key"] or "unknown"),
                "canonicalVendor": str(row["canonical_vendor"] or "Unknown"),
                "canonicalAlias": str(row["canonical_alias"] or "Unknown"),
                "amount": float(row["amount"] or 0.0),
                "count": int(row["count"] or 0),
                "averageAmount": float(row["amount"] or 0.0) / max(int(row["count"] or 0), 1),
                "category": str(row["category"] or "Uncategorized"),
            }
            for row in rows
        ]

    def search_vendor_directory(self, query: str = "", *, limit: int = 40) -> list[dict[str, Any]]:
        """Return vendor autocomplete candidates without materializing all transactions."""

        safe_limit = max(1, min(int(limit or 40), 250))
        normalized = str(query or "").strip().lower()
        clauses = ["it.transaction_key IS NULL", "lower(et.direction) = 'debit'"]
        params: list[Any] = []
        if normalized:
            like = f"%{normalized}%"
            clauses.append(
                """
                (
                    lower(coalesce(et.canonical_vendor, '')) LIKE ?
                    OR lower(coalesce(et.canonical_alias, '')) LIKE ?
                    OR lower(coalesce(et.resolved_vendor, '')) LIKE ?
                    OR lower(coalesce(et.raw_counterparty, '')) LIKE ?
                    OR lower(coalesce(et.category, '')) LIKE ?
                )
                """
            )
            params.extend([like] * 5)
        where_sql = f"WHERE {' AND '.join(clauses)}"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(et.alias_key, ''), lower(et.canonical_alias), lower(et.canonical_vendor), 'unknown') AS vendor_key,
                    COALESCE(NULLIF(et.canonical_vendor, ''), NULLIF(et.resolved_vendor, ''), 'Unknown') AS canonical_vendor,
                    COALESCE(NULLIF(et.canonical_alias, ''), NULLIF(et.canonical_vendor, ''), 'Unknown') AS canonical_alias,
                    COALESCE(NULLIF(et.category, ''), 'Uncategorized') AS category,
                    SUM(COALESCE(et.amount, 0)) AS amount,
                    COUNT(*) AS count
                FROM expense_transactions et
                LEFT JOIN ignored_transactions it ON it.transaction_key = et.transaction_key
                {where_sql}
                GROUP BY vendor_key
                ORDER BY
                    CASE
                        WHEN ? = '' THEN 3
                        WHEN lower(COALESCE(NULLIF(et.canonical_alias, ''), NULLIF(et.canonical_vendor, ''), 'Unknown')) = ? THEN 0
                        WHEN lower(COALESCE(NULLIF(et.canonical_vendor, ''), NULLIF(et.resolved_vendor, ''), 'Unknown')) = ? THEN 0
                        WHEN lower(COALESCE(NULLIF(et.canonical_alias, ''), NULLIF(et.canonical_vendor, ''), 'Unknown')) LIKE ? THEN 1
                        WHEN lower(COALESCE(NULLIF(et.canonical_vendor, ''), NULLIF(et.resolved_vendor, ''), 'Unknown')) LIKE ? THEN 1
                        ELSE 2
                    END,
                    amount DESC,
                    count DESC,
                    COALESCE(NULLIF(et.canonical_alias, ''), NULLIF(et.canonical_vendor, ''), 'Unknown') COLLATE NOCASE ASC
                LIMIT ?
                """,
                [
                    *params,
                    normalized,
                    normalized,
                    normalized,
                    f"{normalized}%",
                    f"{normalized}%",
                    safe_limit,
                ],
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            canonical_vendor = str(row["canonical_vendor"] or "Unknown")
            canonical_alias = str(row["canonical_alias"] or canonical_vendor)
            vendor_key = str(row["vendor_key"] or "unknown")
            category = str(row["category"] or "Uncategorized")
            result.append(
                {
                    "entryType": "alias_cluster",
                    "displayLabel": f"{canonical_alias} [ALIAS CLUSTER]",
                    "vendor": canonical_vendor,
                    "vendorKey": vendor_key,
                    "canonicalVendor": canonical_vendor,
                    "canonicalAlias": canonical_alias,
                    "aliasValue": canonical_alias,
                    "category": category,
                    "amount": float(row["amount"] or 0.0),
                    "count": int(row["count"] or 0),
                    "matchText": " ".join([canonical_vendor, canonical_alias, category]).lower(),
                }
            )
        return result

    def list_ledger_groups(
        self,
        *,
        year           : int,
        month          : int,
        include_ignored: bool = False,
        search_text    : str  = "",
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            clauses, params = self._transaction_filter_clauses(
                include_ignored = include_ignored,
                year            = year,
                month           = month,
                search_text     = search_text,
                use_fts_search  = self._should_use_transaction_fts(conn, search_text),
            )
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"""
                SELECT
                    substr(et.timestamp, 1, 10) AS group_key,
                    MAX(et.timestamp) AS latest_timestamp,
                    COUNT(*) AS row_count,
                    SUM(CASE WHEN lower(et.direction) = 'debit' THEN COALESCE(et.amount, 0) ELSE 0 END) AS debit_total
                FROM expense_transactions et
                LEFT JOIN ignored_transactions it ON it.transaction_key = et.transaction_key
                {where_sql}
                GROUP BY substr(et.timestamp, 1, 10)
                ORDER BY latest_timestamp DESC
                """,
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            group_key = str(row["group_key"] or "").strip()
            if not group_key:
                continue
            try:
                stamp = datetime.fromisoformat(f"{group_key}T00:00:00")
                label = stamp.strftime("%d %b")
            except ValueError:
                label = group_key
            result.append(
                {
                    "groupKey": group_key,
                    "groupLabel": label,
                    "rowCount": int(row["row_count"] or 0),
                    "debitTotal": float(row["debit_total"] or 0.0),
                    "latestTimestamp": str(row["latest_timestamp"] or ""),
                }
            )
        return result

    def list_ledger_group_rows(
        self,
        *,
        year           : int,
        month          : int,
        group_key      : str,
        include_ignored: bool = False,
        search_text    : str  = "",
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = self._fetch_transaction_rows(
                conn,
                include_ignored = include_ignored,
                year            = year,
                month           = month,
                group_key       = group_key,
                search_text     = search_text,
                use_fts_search  = self._should_use_transaction_fts(conn, search_text),
            )
        return [self._transaction_dict(row) for row in rows]

    def set_ignored(self, transaction_key: str, ignored: bool) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._connect() as conn:
            if ignored:
                conn.execute(
                    """
                    INSERT INTO ignored_transactions (transaction_key, ignored_at)
                    VALUES (?, ?)
                    ON CONFLICT(transaction_key) DO UPDATE SET ignored_at = excluded.ignored_at
                    """,
                    (transaction_key, now),
                )
            else:
                conn.execute("DELETE FROM ignored_transactions WHERE transaction_key = ?", (transaction_key,))

    def get_metadata(self, key: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT meta_value
                FROM expenses_metadata
                WHERE meta_key = ?
                """,
                (key,),
            ).fetchone()
        return str(row["meta_value"]) if row is not None else ""

    def set_metadata(self, key: str, value: str) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO expenses_metadata (meta_key, meta_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(meta_key) DO UPDATE SET
                    meta_value = excluded.meta_value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def count_source_records(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM expense_source_records").fetchone()
        return int(row["count"] if row is not None else 0)

    def clear_expense_state(self) -> None:
        """Delete the stored expense source, fact, and transaction data."""

        with self._connect() as conn:
            conn.execute("DELETE FROM expense_parsed_facts")
            conn.execute("DELETE FROM expense_parse_attempts")
            conn.execute("DELETE FROM expense_source_records")
            conn.execute("DELETE FROM expense_provider_checkpoints")
            conn.execute("DELETE FROM expense_transactions")
            conn.execute("DELETE FROM ignored_transactions")
            if self._transaction_search_index_available(conn):
                conn.execute("DELETE FROM expense_transaction_search")

    def upsert_source_record(self, record: SourceRecord) -> tuple[int, bool]:
        now          = datetime.now().astimezone().isoformat()
        with self._connect() as conn:
            return self._upsert_source_record(conn, record, now)

    def upsert_source_record_parse_results(
        self,
        items: list[tuple[SourceRecord, list[dict[str, Any]], list[ParsedFact]]],
        *,
        reparse_all: bool = False,
    ) -> list[dict[str, Any]]:
        if not items:
            return []
        now = datetime.now().astimezone().isoformat()
        results: list[dict[str, Any]] = []
        with self._connect() as conn:
            for record, attempts, facts in items:
                source_record_id, changed = self._upsert_source_record(conn, record, now)
                written = bool(changed or reparse_all)
                if written:
                    self._replace_parse_attempts_for_source(conn, source_record_id, attempts, now)
                    self._replace_facts_for_source(conn, source_record_id, facts, now)
                results.append(
                    {
                        "sourceRecordId": source_record_id,
                        "changed": changed,
                        "written": written,
                        "factsCount": len(facts) if written else 0,
                    }
                )
        return results

    def _upsert_source_record(
        self,
        conn: sqlite3.Connection,
        record: SourceRecord,
        now: str,
    ) -> tuple[int, bool]:
        payload_json = json.dumps(record.payload, sort_keys=True)
        existing = conn.execute(
            """
            SELECT id, content_hash
            FROM expense_source_records
            WHERE provider_id = ? AND record_type = ? AND external_id = ?
            """,
            (
                record.provider_id,
                record.record_type,
                record.external_id,
            ),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO expense_source_records (
                    provider_id, record_type, external_id, title, sender, received_at,
                    payload_json, content_hash, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.provider_id,
                    record.record_type,
                    record.external_id,
                    record.title,
                    record.sender,
                    record.received_at,
                    payload_json,
                    record.content_hash,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid), True

        source_record_id = int(existing["id"])
        if str(existing["content_hash"]) == record.content_hash:
            return source_record_id, False

        conn.execute(
            """
            UPDATE expense_source_records
            SET
                title        = ?,
                sender       = ?,
                received_at  = ?,
                payload_json = ?,
                content_hash = ?,
                updated_at   = ?
            WHERE id = ?
            """,
            (
                record.title,
                record.sender,
                record.received_at,
                payload_json,
                record.content_hash,
                now,
                source_record_id,
            ),
        )
        return source_record_id, True

    def find_source_record_identity(self, provider_id: str, record_type: str, external_id: str) -> dict[str, Any] | None:
        """Return the stored candidate identity used to skip unchanged reparses."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id           AS sourceRecordId,
                    provider_id  AS providerId,
                    record_type  AS recordType,
                    external_id  AS externalId,
                    received_at  AS receivedAt,
                    content_hash AS contentHash
                FROM expense_source_records
                WHERE provider_id = ? AND record_type = ? AND external_id = ?
                """,
                (provider_id, record_type, external_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def replace_facts_for_source(self, source_record_id: int, facts: list[ParsedFact]) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._connect() as conn:
            self._replace_facts_for_source(conn, source_record_id, facts, now)

    def _replace_facts_for_source(
        self,
        conn: sqlite3.Connection,
        source_record_id: int,
        facts: list[ParsedFact],
        now: str,
    ) -> None:
        conn.execute("DELETE FROM expense_parsed_facts WHERE source_record_id = ?", (source_record_id,))
        for fact in facts:
            conn.execute(
                """
                INSERT INTO expense_parsed_facts (
                    source_record_id, fact_type, parser_id, payload_json, confidence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_record_id,
                    fact.fact_type,
                    fact.parser_id,
                    json.dumps(fact.payload, sort_keys=True),
                    fact.confidence,
                    now,
                ),
            )

    def replace_parse_attempts_for_source(self, source_record_id: int, attempts: list[dict[str, Any]]) -> None:
        """Replace the stored parser diagnostics for one candidate email."""

        now = datetime.now().astimezone().isoformat()
        with self._connect() as conn:
            self._replace_parse_attempts_for_source(conn, source_record_id, attempts, now)

    def _replace_parse_attempts_for_source(
        self,
        conn: sqlite3.Connection,
        source_record_id: int,
        attempts: list[dict[str, Any]],
        now: str,
    ) -> None:
        conn.execute("DELETE FROM expense_parse_attempts WHERE source_record_id = ?", (source_record_id,))
        for attempt in attempts:
            conn.execute(
                """
                INSERT INTO expense_parse_attempts (
                    source_record_id,
                    parser_id,
                    matched_rule_id,
                    matched_rule_name,
                    parse_status,
                    reason_code,
                    reason_text,
                    facts_count,
                    preview_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_record_id,
                    str(attempt.get("parserId", "")).strip(),
                    str(attempt.get("matchedRuleId", "")).strip(),
                    str(attempt.get("matchedRuleName", "")).strip(),
                    str(attempt.get("parseStatus", "")).strip(),
                    str(attempt.get("reasonCode", "")).strip(),
                    str(attempt.get("reasonText", "")).strip(),
                    int(attempt.get("factsCount", 0) or 0),
                    json.dumps(attempt.get("preview", {}), sort_keys=True),
                    now,
                    now,
                ),
            )

    def list_facts(self, fact_type: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    pf.id,
                    pf.fact_type,
                    pf.parser_id,
                    pf.payload_json,
                    pf.confidence,
                    pf.created_at,
                    sr.provider_id,
                    sr.record_type,
                    sr.external_id,
                    sr.title,
                    sr.sender,
                    sr.received_at
                FROM expense_parsed_facts pf
                JOIN expense_source_records sr ON sr.id = pf.source_record_id
                WHERE pf.fact_type = ?
                ORDER BY sr.received_at DESC, pf.id DESC
                """,
                (fact_type,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "factType": str(row["fact_type"]),
                "parserId": str(row["parser_id"]),
                "payload": json.loads(row["payload_json"]),
                "confidence": float(row["confidence"]),
                "createdAt": str(row["created_at"]),
                "providerId": str(row["provider_id"]),
                "recordType": str(row["record_type"]),
                "externalId": str(row["external_id"]),
                "title": str(row["title"]),
                "sender": str(row["sender"]),
                "receivedAt": str(row["received_at"]),
            }
            for row in rows
        ]

    def list_source_records(self, *, record_type: str | None = None) -> list[dict[str, Any]]:
        query  = """
            SELECT
                id,
                provider_id,
                record_type,
                external_id,
                title,
                sender,
                received_at,
                payload_json,
                content_hash
            FROM expense_source_records
        """
        params: tuple[Any, ...] = ()
        if record_type:
            query += " WHERE record_type = ?"
            params = (record_type,)
        query += " ORDER BY received_at DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "sourceRecordId": int(row["id"]),
                "providerId": str(row["provider_id"]),
                "recordType": str(row["record_type"]),
                "externalId": str(row["external_id"]),
                "title": str(row["title"]),
                "sender": str(row["sender"]),
                "receivedAt": str(row["received_at"]),
                "payload": json.loads(row["payload_json"]),
                "contentHash": str(row["content_hash"]),
            }
            for row in rows
        ]

    def get_source_record(self, source_record_id: int) -> dict[str, Any] | None:
        """Return one stored source record when it exists."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    provider_id,
                    record_type,
                    external_id,
                    title,
                    sender,
                    received_at,
                    payload_json,
                    content_hash
                FROM expense_source_records
                WHERE id = ?
                """,
                (source_record_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "sourceRecordId": int(row["id"]),
            "providerId": str(row["provider_id"]),
            "recordType": str(row["record_type"]),
            "externalId": str(row["external_id"]),
            "title": str(row["title"]),
            "sender": str(row["sender"]),
            "receivedAt": str(row["received_at"]),
            "payload": json.loads(row["payload_json"]),
            "contentHash": str(row["content_hash"]),
        }

    def list_candidate_mail_debug_rows(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return the newest candidate-email rows with parse diagnostics for the debug pane."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    sr.id,
                    sr.provider_id,
                    sr.external_id,
                    sr.title,
                    sr.sender,
                    sr.received_at,
                    pa.parser_id,
                    pa.matched_rule_id,
                    pa.matched_rule_name,
                    pa.parse_status,
                    pa.reason_code,
                    pa.reason_text,
                    pa.facts_count,
                    pa.preview_json,
                    (
                        SELECT COUNT(*)
                        FROM expense_parsed_facts pf
                        WHERE pf.source_record_id = sr.id
                    ) AS parsed_fact_count
                FROM expense_source_records sr
                LEFT JOIN expense_parse_attempts pa
                    ON pa.source_record_id = sr.id
                WHERE sr.record_type = 'email'
                ORDER BY sr.received_at DESC, sr.id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "sourceRecordId": int(row["id"]),
                "providerId": str(row["provider_id"]),
                "externalId": str(row["external_id"]),
                "title": str(row["title"]),
                "sender": str(row["sender"]),
                "receivedAt": str(row["received_at"]),
                "parserId": str(row["parser_id"] or ""),
                "matchedRuleId": str(row["matched_rule_id"] or ""),
                "matchedRuleName": str(row["matched_rule_name"] or ""),
                "parseStatus": str(row["parse_status"] or ""),
                "reasonCode": str(row["reason_code"] or ""),
                "reasonText": str(row["reason_text"] or ""),
                "factsCount": int(row["facts_count"] or 0),
                "parsedFactCount": int(row["parsed_fact_count"] or 0),
                "preview": json.loads(row["preview_json"] or "{}"),
            }
            for row in rows
        ]

    def candidate_mail_debug_summary(self, *, provider_id: str) -> dict[str, Any]:
        """Return summary counts used by the debug pane header."""

        checkpoint = self.get_provider_checkpoint(provider_id) or {}
        with self._connect() as conn:
            counts = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_candidates,
                    SUM(CASE WHEN pa.parse_status = 'parsed' THEN 1 ELSE 0 END) AS parsed_count,
                    SUM(CASE WHEN pa.parse_status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN pa.reason_code = 'no_matching_rule' THEN 1 ELSE 0 END) AS rule_miss_count
                FROM expense_source_records sr
                LEFT JOIN expense_parse_attempts pa ON pa.source_record_id = sr.id
                WHERE sr.record_type = 'email'
                """
            ).fetchone()
            reason_rows = conn.execute(
                """
                SELECT
                    COALESCE(NULLIF(pa.reason_code, ''), 'parsed') AS reason_code,
                    COUNT(*) AS count
                FROM expense_source_records sr
                LEFT JOIN expense_parse_attempts pa ON pa.source_record_id = sr.id
                WHERE sr.record_type = 'email'
                GROUP BY reason_code
                ORDER BY count DESC, reason_code ASC
                LIMIT 8
                """
            ).fetchall()
        return {
            "totalCandidates": int((counts["total_candidates"] if counts is not None else 0) or 0),
            "parsedCount": int((counts["parsed_count"] if counts is not None else 0) or 0),
            "failedCount": int((counts["failed_count"] if counts is not None else 0) or 0),
            "ruleMissCount": int((counts["rule_miss_count"] if counts is not None else 0) or 0),
            "failureReasons": [
                {
                    "reasonCode": str(row["reason_code"] or ""),
                    "count": int(row["count"] or 0),
                }
                for row in reason_rows
            ],
            "lastCandidateAt": str(checkpoint.get("lastReceivedAt", "")),
        }

    def get_candidate_mail_detail(self, source_record_id: int) -> dict[str, Any] | None:
        """Return one candidate email plus diagnostics and parsed facts."""

        source = self.get_source_record(source_record_id)
        if source is None:
            return None
        with self._connect() as conn:
            attempt_rows = conn.execute(
                """
                SELECT
                    parser_id,
                    matched_rule_id,
                    matched_rule_name,
                    parse_status,
                    reason_code,
                    reason_text,
                    facts_count,
                    preview_json,
                    updated_at
                FROM expense_parse_attempts
                WHERE source_record_id = ?
                ORDER BY id ASC
                """,
                (source_record_id,),
            ).fetchall()
            fact_rows = conn.execute(
                """
                SELECT
                    fact_type,
                    parser_id,
                    payload_json,
                    confidence,
                    created_at
                FROM expense_parsed_facts
                WHERE source_record_id = ?
                ORDER BY id ASC
                """,
                (source_record_id,),
            ).fetchall()
        return {
            **source,
            "attempts": [
                {
                    "parserId": str(row["parser_id"]),
                    "matchedRuleId": str(row["matched_rule_id"] or ""),
                    "matchedRuleName": str(row["matched_rule_name"] or ""),
                    "parseStatus": str(row["parse_status"] or ""),
                    "reasonCode": str(row["reason_code"] or ""),
                    "reasonText": str(row["reason_text"] or ""),
                    "factsCount": int(row["facts_count"] or 0),
                    "preview": json.loads(row["preview_json"] or "{}"),
                    "updatedAt": str(row["updated_at"] or ""),
                }
                for row in attempt_rows
            ],
            "facts": [
                {
                    "factType": str(row["fact_type"]),
                    "parserId": str(row["parser_id"]),
                    "payload": json.loads(row["payload_json"]),
                    "confidence": float(row["confidence"]),
                    "createdAt": str(row["created_at"]),
                }
                for row in fact_rows
            ],
        }

    def get_provider_checkpoint(self, provider_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT provider_id, last_received_at, metadata_json, updated_at
                FROM expense_provider_checkpoints
                WHERE provider_id = ?
                """,
                (provider_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "providerId": str(row["provider_id"]),
            "lastReceivedAt": str(row["last_received_at"]),
            "metadata": json.loads(row["metadata_json"]),
            "updatedAt": str(row["updated_at"]),
        }

    def update_provider_checkpoint(self, provider_id: str, last_received_at: str, metadata: dict[str, Any] | None = None) -> None:
        now     = datetime.now().astimezone().isoformat()
        payload = json.dumps(metadata or {}, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO expense_provider_checkpoints (provider_id, last_received_at, metadata_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    last_received_at = excluded.last_received_at,
                    metadata_json    = excluded.metadata_json,
                    updated_at       = excluded.updated_at
                """,
                (provider_id, last_received_at, payload, now),
            )

    @staticmethod
    def _connect_readonly(path: Path) -> sqlite3.Connection:
        """Open one SQLite connection in read-only mode for status inspection."""

        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        """Return whether one table exists in the inspected DB."""

        row = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        return row is not None

    @classmethod
    def _count_table_rows(cls, conn: sqlite3.Connection, table_name: str) -> int:
        """Return one table row count when the table exists."""

        if not cls._table_exists(conn, table_name):
            return 0
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
        return int(row["count"] if row is not None else 0)

    @classmethod
    def _metadata_value(cls, conn: sqlite3.Connection, meta_key: str) -> str:
        """Return one metadata value when the table and row exist."""

        if not cls._table_exists(conn, "expenses_metadata"):
            return ""
        row = conn.execute(
            """
            SELECT meta_value
            FROM expenses_metadata
            WHERE meta_key = ?
            """,
            (meta_key,),
        ).fetchone()
        return str(row["meta_value"]) if row is not None else ""

    @classmethod
    def _provider_checkpoint(cls, conn: sqlite3.Connection, provider_id: str) -> dict[str, str]:
        """Return the provider checkpoint summary when it exists."""

        if not cls._table_exists(conn, "expense_provider_checkpoints"):
            return {}
        row = conn.execute(
            """
            SELECT last_received_at, updated_at
            FROM expense_provider_checkpoints
            WHERE provider_id = ?
            """,
            (provider_id,),
        ).fetchone()
        if row is None:
            return {}
        return {
            "lastReceivedAt": str(row["last_received_at"] or ""),
            "updatedAt": str(row["updated_at"] or ""),
        }

    def _coerce_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _coerce_int(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _fetch_transaction_rows(
        self,
        conn: sqlite3.Connection,
        *,
        include_ignored: bool,
        year           : int | None = None,
        month          : int | None = None,
        group_key      : str        = "",
        search_text    : str        = "",
        limit          : int | None = None,
        offset         : int = 0,
        use_fts_search : bool = False,
    ) -> list[sqlite3.Row]:
        clauses, params = self._transaction_filter_clauses(
            include_ignored = include_ignored,
            year            = year,
            month           = month,
            group_key       = group_key,
            search_text     = search_text,
            use_fts_search  = use_fts_search,
        )
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        paging_sql = ""
        if limit is not None:
            paging_sql = "LIMIT ? OFFSET ?"
            params = [*params, max(1, int(limit)), max(0, int(offset or 0))]
        return conn.execute(
            f"""
            SELECT
                et.transaction_key,
                et.provider_id,
                et.external_id,
                et.parser_id,
                et.bank_name,
                et.account_suffix,
                et.direction,
                et.amount,
                et.currency,
                et.transaction_id,
                et.counterparty,
                et.raw_counterparty,
                et.resolved_vendor,
                et.canonical_vendor,
                et.canonical_alias,
                et.alias_key,
                et.category,
                et.subcategory,
                et.vendor_match_source,
                et.timestamp,
                et.year,
                et.month,
                et.title,
                et.sender,
                it.ignored_at
            FROM expense_transactions et
            LEFT JOIN ignored_transactions it ON it.transaction_key = et.transaction_key
            {where_sql}
            ORDER BY et.timestamp DESC, et.transaction_key DESC
            {paging_sql}
            """,
            params,
        ).fetchall()

    def _transaction_filter_clauses(
        self,
        *,
        include_ignored: bool,
        year           : int | None = None,
        month          : int | None = None,
        group_key      : str        = "",
        search_text    : str        = "",
        use_fts_search : bool       = False,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params : list[Any] = []
        if not include_ignored:
            clauses.append("it.transaction_key IS NULL")
        if year:
            clauses.append("et.year = ?")
            params.append(int(year))
        if month:
            clauses.append("et.month = ?")
            params.append(int(month))
        if group_key:
            clauses.append("substr(et.timestamp, 1, 10) = ?")
            params.append(group_key.strip())
        query = search_text.strip().lower()
        if query:
            fts_query = self._fts_query(query)
            if use_fts_search and fts_query:
                clauses.append(
                    """
                    et.transaction_key IN (
                        SELECT transaction_key
                        FROM expense_transaction_search
                        WHERE expense_transaction_search MATCH ?
                    )
                    """
                )
                params.append(fts_query)
            else:
                like = f"%{query}%"
                clauses.append(
                    """
                    (
                        lower(coalesce(et.counterparty, '')) LIKE ?
                        OR lower(coalesce(et.raw_counterparty, '')) LIKE ?
                        OR lower(coalesce(et.resolved_vendor, '')) LIKE ?
                        OR lower(coalesce(et.canonical_vendor, '')) LIKE ?
                        OR lower(coalesce(et.canonical_alias, '')) LIKE ?
                        OR lower(coalesce(et.alias_key, '')) LIKE ?
                        OR lower(coalesce(et.transaction_id, '')) LIKE ?
                        OR lower(coalesce(et.title, '')) LIKE ?
                        OR lower(coalesce(et.sender, '')) LIKE ?
                        OR lower(coalesce(et.bank_name, '')) LIKE ?
                        OR lower(coalesce(et.account_suffix, '')) LIKE ?
                        OR lower(coalesce(et.category, '')) LIKE ?
                        OR lower(coalesce(et.subcategory, '')) LIKE ?
                    )
                    """
                )
                params.extend([like] * 13)
        return clauses, params

    def _transaction_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "transactionKey": str(row["transaction_key"]),
            "providerId": str(row["provider_id"]),
            "externalId": str(row["external_id"]),
            "parserId": str(row["parser_id"]),
            "bankName": str(row["bank_name"]),
            "accountSuffix": str(row["account_suffix"]),
            "direction": str(row["direction"]),
            "amount": row["amount"],
            "currency": str(row["currency"]),
            "transactionId": str(row["transaction_id"]),
            "counterparty": str(row["counterparty"]),
            "rawCounterparty": str(row["raw_counterparty"]),
            "resolvedVendor": str(row["resolved_vendor"]),
            "canonicalVendor": str(row["canonical_vendor"]),
            "canonicalAlias": str(row["canonical_alias"]),
            "aliasKey": str(row["alias_key"]),
            "category": str(row["category"]),
            "subcategory": str(row["subcategory"]),
            "vendorMatchSource": str(row["vendor_match_source"]),
            "timestamp": str(row["timestamp"]),
            "year": row["year"],
            "month": row["month"],
            "title": str(row["title"]),
            "sender": str(row["sender"]),
            "ignored": row["ignored_at"] is not None,
            "ignoredAt": str(row["ignored_at"] or ""),
        }
