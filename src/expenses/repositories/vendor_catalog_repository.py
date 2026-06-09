from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from src.expenses.repositories.sqlite_db import connect_sqlite


class VendorCatalogRepository:
    """Persist the expenses vendor catalog in the app-owned database namespace."""

    def __init__(self, db_path: Path) -> None:
        """Store the database path and initialize the vendor catalog schema."""

        self.db_path = db_path
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        """Open one SQLite connection with the shared repository policy."""

        return connect_sqlite(self.db_path)

    def ensure_schema(self) -> None:
        """Create the vendor catalog schema and backfill legacy columns."""

        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_name TEXT NOT NULL UNIQUE,
                    canonical_alias TEXT NOT NULL DEFAULT '',
                    normalized_alias TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'Uncategorized',
                    subcategory TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    merged_into_vendor_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vendor_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(vendor_id, category),
                    FOREIGN KEY(vendor_id) REFERENCES vendors(id)
                );

                CREATE TABLE IF NOT EXISTS vendor_merge_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_id INTEGER NOT NULL,
                    raw_name TEXT NOT NULL,
                    normalized_raw_name TEXT NOT NULL,
                    match_source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    UNIQUE(vendor_id, normalized_raw_name),
                    FOREIGN KEY(vendor_id) REFERENCES vendors(id)
                );

                CREATE TABLE IF NOT EXISTS vendor_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    match_kind TEXT NOT NULL DEFAULT 'exact',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(normalized_alias),
                    FOREIGN KEY(vendor_id) REFERENCES vendors(id)
                );

                CREATE TABLE IF NOT EXISTS vendor_merge_links (
                    source_vendor_id INTEGER PRIMARY KEY,
                    target_vendor_id INTEGER NOT NULL,
                    source_alias_snapshot TEXT NOT NULL DEFAULT '',
                    target_alias_snapshot TEXT NOT NULL DEFAULT '',
                    kept_alias TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_vendor_id) REFERENCES vendors(id),
                    FOREIGN KEY(target_vendor_id) REFERENCES vendors(id)
                );

                CREATE INDEX IF NOT EXISTS idx_vendor_categories_vendor ON vendor_categories(vendor_id);
                CREATE INDEX IF NOT EXISTS idx_vendor_merge_members_vendor ON vendor_merge_members(vendor_id);
                CREATE INDEX IF NOT EXISTS idx_vendor_merge_members_normalized ON vendor_merge_members(normalized_raw_name);
                CREATE INDEX IF NOT EXISTS idx_vendor_merge_members_raw_lower ON vendor_merge_members(LOWER(raw_name));
                CREATE INDEX IF NOT EXISTS idx_vendor_aliases_vendor ON vendor_aliases(vendor_id);
                CREATE INDEX IF NOT EXISTS idx_vendor_merge_links_target ON vendor_merge_links(target_vendor_id);
                """
            )
            self._ensure_vendor_columns(conn)
            self._migrate_legacy_data(conn)
            self._backfill_normalized_aliases(conn)
            self._assert_unique_normalized_aliases(conn)
            self._ensure_search_indexes(conn)
            self._reindex_vendor_search_conn(conn)

    def reindex_vendor_search(self) -> None:
        """Rebuild vendor search indexes for existing databases."""

        with self._connect() as conn:
            self._reindex_vendor_search_conn(conn)

    def _reindex_vendor_search_conn(self, conn: sqlite3.Connection) -> None:
        """Run targeted reindex statements for vendor lookup indexes."""

        for index_name in (
            "idx_vendors_canonical_name_lower",
            "idx_vendors_canonical_alias_lower",
            "idx_vendors_nickname_lower",
            "idx_vendors_normalized_alias_unique",
            "idx_vendors_merged_into_vendor",
            "idx_vendor_merge_members_normalized",
            "idx_vendor_merge_members_raw_lower",
            "idx_vendor_merge_links_target",
        ):
            conn.execute(f"REINDEX {index_name}")

    def _ensure_vendor_columns(self, conn: sqlite3.Connection) -> None:
        """Add missing vendor columns when an older database is reused."""

        existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(vendors)").fetchall()}
        additions = {
            "canonical_alias": "TEXT NOT NULL DEFAULT ''",
            "normalized_alias": "TEXT NOT NULL DEFAULT ''",
            "nickname": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT 'Uncategorized'",
            "subcategory": "TEXT NOT NULL DEFAULT ''",
            "notes": "TEXT NOT NULL DEFAULT ''",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "merged_into_vendor_id": "INTEGER NOT NULL DEFAULT 0",
        }
        for column_name, column_sql in additions.items():
            if column_name in existing:
                continue
            conn.execute(f"ALTER TABLE vendors ADD COLUMN {column_name} {column_sql}")

    def _ensure_search_indexes(self, conn: sqlite3.Connection) -> None:
        """Create vendor search indexes after columns are backfilled."""

        conn.execute("DROP INDEX IF EXISTS idx_vendors_normalized_alias_unique")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vendors_canonical_name_lower ON vendors(LOWER(canonical_name))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vendors_canonical_alias_lower ON vendors(LOWER(canonical_alias))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vendors_nickname_lower ON vendors(LOWER(nickname))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vendors_merged_into_vendor ON vendors(merged_into_vendor_id)")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vendors_normalized_alias_unique
            ON vendors(normalized_alias)
            WHERE COALESCE(merged_into_vendor_id, 0) = 0
            """
        )

    def _backfill_normalized_aliases(self, conn: sqlite3.Connection) -> None:
        """Populate missing normalized_alias values from canonical alias/name."""

        rows = conn.execute(
            """
            SELECT id, canonical_name, canonical_alias, normalized_alias
            FROM vendors
            """
        ).fetchall()
        for row in rows:
            current = str(row["normalized_alias"] or "").strip()
            alias_source = str(row["canonical_alias"] or "").strip() or str(row["canonical_name"] or "").strip()
            normalized = self.normalize_alias(alias_source)
            if normalized and current != normalized:
                conn.execute("UPDATE vendors SET normalized_alias = ? WHERE id = ?", (normalized, int(row["id"])))

    def _assert_unique_normalized_aliases(self, conn: sqlite3.Connection) -> None:
        """Fail fast if multiple vendors share one normalized canonical alias."""

        duplicates = conn.execute(
            """
            SELECT normalized_alias, COUNT(*) AS conflict_count
            FROM vendors
            WHERE normalized_alias <> '' AND COALESCE(merged_into_vendor_id, 0) = 0
            GROUP BY normalized_alias
            HAVING COUNT(*) > 1
            ORDER BY conflict_count DESC, normalized_alias ASC
            """
        ).fetchall()
        if not duplicates:
            return
        sample = ", ".join(str(row["normalized_alias"]) for row in duplicates[:5])
        raise RuntimeError(f"Duplicate normalized vendor aliases found: {sample}")

    def _migrate_legacy_data(self, conn: sqlite3.Connection) -> None:
        """Backfill derived vendor rows from older vendor catalog shapes."""

        now = datetime.now().astimezone().isoformat()
        conn.execute(
            """
            UPDATE vendors
            SET canonical_alias = CASE
                WHEN trim(canonical_alias) = '' THEN COALESCE(NULLIF(trim(nickname), ''), trim(canonical_name))
                ELSE canonical_alias
            END
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO vendor_categories (vendor_id, category, created_at)
            SELECT id, trim(category), ?
            FROM vendors
            WHERE trim(category) <> ''
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO vendor_merge_members (vendor_id, raw_name, normalized_raw_name, match_source, created_at)
            SELECT vendor_id, alias, normalized_alias, 'legacy_alias', ?
            FROM vendor_aliases
            """,
            (now,),
        )

    def list_vendors(self, query: str = "") -> list[dict[str, Any]]:
        """Return vendors filtered by a loose text query."""

        with self._connect() as conn:
            vendor_ids = [
                int(row["id"])
                for row in conn.execute(
                    """
                    SELECT id
                    FROM vendors
                    WHERE COALESCE(merged_into_vendor_id, 0) = 0 AND active = 1
                    ORDER BY canonical_name ASC
                    """
                ).fetchall()
            ]
            records = [self._hydrate_vendor(conn, vendor_id) for vendor_id in vendor_ids]
        if not query.strip():
            return records
        lowered = query.strip().lower()
        normalized = self.normalize_alias(query)
        return [record for record in records if self._vendor_matches_query(record, lowered, normalized)]

    def get_vendor(self, vendor_id: int) -> dict[str, Any] | None:
        """Return one vendor by database id when it exists."""

        with self._connect() as conn:
            row = conn.execute("SELECT id FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
            if row is None:
                return None
            return self._hydrate_vendor(conn, int(row["id"]))

    def find_vendor(self, value: str) -> dict[str, Any] | None:
        """Resolve one vendor by canonical name, alias, nickname, or merged raw name."""

        normalized = self.normalize_alias(value)
        lowered = str(value or "").strip().lower()
        if not normalized and not lowered:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    v.id,
                    CASE
                        WHEN v.normalized_alias = ? THEN 0
                        WHEN LOWER(v.canonical_alias) = ? THEN 1
                        WHEN LOWER(v.canonical_name) = ? THEN 2
                        WHEN LOWER(v.nickname) = ? THEN 3
                        WHEN EXISTS (
                            SELECT 1
                            FROM vendor_merge_members mm
                            WHERE mm.vendor_id = v.id AND mm.normalized_raw_name = ?
                        ) THEN 4
                        ELSE 5
                    END AS match_rank
                FROM vendors v
                WHERE v.normalized_alias = ?
                   OR LOWER(v.canonical_alias) = ?
                   OR LOWER(v.canonical_name) = ?
                   OR LOWER(v.nickname) = ?
                   OR EXISTS (
                        SELECT 1
                        FROM vendor_merge_members mm
                        WHERE mm.vendor_id = v.id AND mm.normalized_raw_name = ?
                   )
                ORDER BY match_rank ASC, v.id ASC
                LIMIT 1
                """,
                (normalized, lowered, lowered, lowered, normalized, normalized, lowered, lowered, lowered, normalized),
            ).fetchone()
            if row is None:
                return None
            return self._hydrate_vendor(conn, self._root_vendor_id(conn, int(row["id"])))

    def create_vendor(
        self,
        canonical_name: str,
        *,
        canonical_alias: str = "",
        nickname: str = "",
        notes: str = "",
        categories: list[str] | None = None,
    ) -> int:
        """Create one vendor and its category links."""

        now = datetime.now().astimezone().isoformat()
        alias = canonical_alias.strip() or canonical_name.strip()
        normalized_alias = self.normalize_alias(alias)
        if not normalized_alias:
            raise ValueError("Canonical alias is required to create a vendor.")
        with self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO vendors (canonical_name, canonical_alias, normalized_alias, nickname, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical_name.strip(),
                        alias,
                        normalized_alias,
                        nickname.strip(),
                        notes.strip(),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Canonical alias already exists: {alias}") from exc
            vendor_id = int(cursor.lastrowid)
            self._replace_categories(conn, vendor_id, categories or [])
        return vendor_id

    def update_vendor(
        self,
        vendor_id: int,
        *,
        canonical_name: str,
        canonical_alias: str,
        nickname: str = "",
        notes: str = "",
        categories: list[str] | None = None,
    ) -> None:
        """Update one vendor and replace its categories."""

        now = datetime.now().astimezone().isoformat()
        cleaned_alias = canonical_alias.strip() or canonical_name.strip()
        normalized_alias = self.normalize_alias(cleaned_alias)
        if not normalized_alias:
            raise ValueError("Canonical alias is required to update a vendor.")
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    UPDATE vendors
                    SET canonical_name = ?, canonical_alias = ?, normalized_alias = ?, nickname = ?, notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        canonical_name.strip(),
                        cleaned_alias,
                        normalized_alias,
                        nickname.strip(),
                        notes.strip(),
                        now,
                        vendor_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Canonical alias already exists: {cleaned_alias}") from exc
            self._replace_categories(conn, vendor_id, categories or [])

    def delete_vendor(self, vendor_id: int) -> None:
        """Delete one vendor and all of its related rows."""

        with self._connect() as conn:
            linked_child = conn.execute(
                "SELECT id FROM vendors WHERE merged_into_vendor_id = ? LIMIT 1",
                (vendor_id,),
            ).fetchone()
            if linked_child is not None:
                raise ValueError("Unmerge child vendors before deleting this vendor.")
            conn.execute("DELETE FROM vendor_categories WHERE vendor_id = ?", (vendor_id,))
            conn.execute("DELETE FROM vendor_merge_members WHERE vendor_id = ?", (vendor_id,))
            conn.execute("DELETE FROM vendor_aliases WHERE vendor_id = ?", (vendor_id,))
            conn.execute("DELETE FROM vendor_merge_links WHERE source_vendor_id = ? OR target_vendor_id = ?", (vendor_id, vendor_id))
            conn.execute("DELETE FROM vendors WHERE id = ?", (vendor_id,))

    def list_vendor_categories(self, vendor_id: int) -> list[str]:
        """Return the current category set for one vendor."""

        with self._connect() as conn:
            return self._cluster_categories(conn, vendor_id)

    def add_vendor_category(self, vendor_id: int, category: str) -> None:
        """Attach one category to a vendor when the value is not empty."""

        value = category.strip()
        if not value:
            return
        with self._connect() as conn:
            categories = self._cluster_categories(conn, vendor_id)
            categories.append(value)
            self._replace_categories(conn, vendor_id, categories)

    def remove_vendor_category(self, vendor_id: int, category: str) -> None:
        """Remove one category from a vendor."""

        with self._connect() as conn:
            normalized = category.strip().lower()
            categories = [item for item in self._cluster_categories(conn, vendor_id) if item.strip().lower() != normalized]
            self._replace_categories(conn, vendor_id, categories)

    def list_merge_members(self, vendor_id: int) -> list[dict[str, Any]]:
        """Return the merged raw-name rows for one vendor."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT raw_name, normalized_raw_name, match_source
                FROM vendor_merge_members
                WHERE vendor_id = ?
                ORDER BY raw_name ASC
                """,
                (vendor_id,),
            ).fetchall()
        return [
            {
                "rawName": str(row["raw_name"]),
                "normalizedRawName": str(row["normalized_raw_name"]),
                "matchSource": str(row["match_source"]),
            }
            for row in rows
        ]

    def add_merge_member(self, vendor_id: int, raw_name: str, *, source: str = "manual") -> None:
        """Attach one raw-name merge row to a vendor."""

        value = raw_name.strip()
        normalized = self.normalize_alias(value)
        if not value or not normalized:
            return
        with self._connect() as conn:
            root_vendor_id = self._root_vendor_id(conn, vendor_id)
            conn.execute(
                """
                DELETE FROM vendor_merge_members
                WHERE normalized_raw_name = ? AND vendor_id <> ?
                """,
                (normalized, root_vendor_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO vendor_merge_members (vendor_id, raw_name, normalized_raw_name, match_source, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    root_vendor_id,
                    value,
                    normalized,
                    source.strip() or "manual",
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def remove_merge_member(self, vendor_id: int, raw_name: str) -> None:
        """Detach one raw-name merge row from a vendor."""

        normalized = self.normalize_alias(raw_name)
        with self._connect() as conn:
            root_vendor_id = self._root_vendor_id(conn, vendor_id)
            conn.execute(
                "DELETE FROM vendor_merge_members WHERE vendor_id = ? AND normalized_raw_name = ?",
                (root_vendor_id, normalized),
            )

    def merge_vendors(self, source_vendor_id: int, target_vendor_id: int, kept_alias: str) -> dict[str, Any]:
        """Soft-merge one vendor into another root vendor and keep one shared alias."""

        with self._connect() as conn:
            source_root_id = self._root_vendor_id(conn, source_vendor_id)
            target_root_id = self._root_vendor_id(conn, target_vendor_id)
            if source_root_id == target_root_id:
                root_record = self._hydrate_vendor(conn, target_root_id)
                return {
                    "sourceVendorId": source_root_id,
                    "targetVendorId": target_root_id,
                    "rootVendorId": target_root_id,
                    "canonicalVendor": str(root_record.get("canonicalVendor", "")),
                    "canonicalAlias": str(root_record.get("canonicalAlias", "")),
                    "aliasKey": str(root_record.get("aliasKey", "")),
                    "changed": False,
                }

            source_row = self._vendor_row(conn, source_root_id)
            target_row = self._vendor_row(conn, target_root_id)
            if source_row is None or target_row is None:
                raise ValueError("Both vendors must exist before merging.")

            chosen_alias = kept_alias.strip() or str(target_row["canonical_alias"] or "").strip() or str(target_row["canonical_name"] or "").strip()
            normalized_alias = self.normalize_alias(chosen_alias)
            if not normalized_alias:
                raise ValueError("Alias to keep is required for vendor merge.")
            self._assert_alias_available(conn, normalized_alias, ignore_vendor_ids={source_root_id, target_root_id})

            now = datetime.now().astimezone().isoformat()
            conn.execute(
                """
                INSERT INTO vendor_merge_links (
                    source_vendor_id,
                    target_vendor_id,
                    source_alias_snapshot,
                    target_alias_snapshot,
                    kept_alias,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_vendor_id) DO UPDATE SET
                    target_vendor_id = excluded.target_vendor_id,
                    source_alias_snapshot = excluded.source_alias_snapshot,
                    target_alias_snapshot = excluded.target_alias_snapshot,
                    kept_alias = excluded.kept_alias,
                    updated_at = excluded.updated_at
                """,
                (
                    source_root_id,
                    target_root_id,
                    str(source_row["canonical_alias"] or source_row["canonical_name"] or "").strip(),
                    str(target_row["canonical_alias"] or target_row["canonical_name"] or "").strip(),
                    chosen_alias,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE vendors
                SET merged_into_vendor_id = ?, active = 0, updated_at = ?
                WHERE id = ?
                """,
                (target_root_id, now, source_root_id),
            )
            conn.execute(
                """
                UPDATE vendors
                SET canonical_alias = ?, normalized_alias = ?, updated_at = ?
                WHERE id = ?
                """,
                (chosen_alias, normalized_alias, now, target_root_id),
            )
            self._replace_categories(conn, target_root_id, self._cluster_categories(conn, target_root_id))

            root_record = self._hydrate_vendor(conn, target_root_id)
            return {
                "sourceVendorId": source_root_id,
                "targetVendorId": target_root_id,
                "rootVendorId": target_root_id,
                "canonicalVendor": str(root_record.get("canonicalVendor", "")),
                "canonicalAlias": str(root_record.get("canonicalAlias", "")),
                "aliasKey": str(root_record.get("aliasKey", "")),
                "changed": True,
            }

    def unmerge_vendor(self, source_vendor_id: int) -> dict[str, Any]:
        """Restore one previously soft-merged vendor as its own active root."""

        with self._connect() as conn:
            source_row = self._vendor_row(conn, source_vendor_id)
            if source_row is None:
                raise ValueError("Source vendor does not exist.")
            target_vendor_id = int(source_row["merged_into_vendor_id"] or 0)
            if target_vendor_id <= 0:
                raise ValueError("Selected vendor is not currently merged.")

            target_row = self._vendor_row(conn, self._root_vendor_id(conn, target_vendor_id))
            if target_row is None:
                raise ValueError("Merged target vendor is missing.")

            merge_link = conn.execute(
                """
                SELECT target_alias_snapshot, kept_alias
                FROM vendor_merge_links
                WHERE source_vendor_id = ?
                """,
                (source_vendor_id,),
            ).fetchone()

            now = datetime.now().astimezone().isoformat()
            source_alias = str(source_row["canonical_alias"] or source_row["canonical_name"] or "").strip()
            source_normalized = self.normalize_alias(source_alias)
            target_alias = str(target_row["canonical_alias"] or target_row["canonical_name"] or "").strip()
            target_normalized = self.normalize_alias(target_alias)
            restored_target_alias = str(merge_link["target_alias_snapshot"] or "").strip() if merge_link is not None else ""
            restored_target_normalized = self.normalize_alias(restored_target_alias)

            if source_normalized and target_normalized and source_normalized == target_normalized:
                fallback_alias = restored_target_alias or str(target_row["canonical_name"] or "").strip()
                fallback_normalized = self.normalize_alias(fallback_alias)
                if not fallback_normalized:
                    raise ValueError("Could not restore a unique alias for the target vendor during unmerge.")
                self._assert_alias_available(conn, fallback_normalized, ignore_vendor_ids={source_vendor_id, int(target_row["id"])})
                conn.execute(
                    """
                    UPDATE vendors
                    SET canonical_alias = ?, normalized_alias = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (fallback_alias, fallback_normalized, now, int(target_row["id"])),
                )

            self._assert_alias_available(conn, source_normalized, ignore_vendor_ids={source_vendor_id})
            conn.execute(
                """
                UPDATE vendors
                SET merged_into_vendor_id = 0, active = 1, updated_at = ?
                WHERE id = ?
                """,
                (now, source_vendor_id),
            )
            conn.execute("DELETE FROM vendor_merge_links WHERE source_vendor_id = ?", (source_vendor_id,))

            root_record = self._hydrate_vendor(conn, int(target_row["id"]))
            source_record = self._hydrate_vendor(conn, source_vendor_id)
            return {
                "sourceVendorId": source_vendor_id,
                "targetVendorId": int(target_row["id"]),
                "rootVendorId": int(target_row["id"]),
                "canonicalVendor": str(root_record.get("canonicalVendor", "")),
                "canonicalAlias": str(root_record.get("canonicalAlias", "")),
                "aliasKey": str(root_record.get("aliasKey", "")),
                "restoredVendor": str(source_record.get("canonicalVendor", "")),
                "restoredAlias": str(source_record.get("canonicalAlias", "")),
                "changed": True,
            }

    def resolve_vendor(self, raw_value: str) -> dict[str, Any] | None:
        """Resolve one raw counterparty string to a vendor catalog row."""

        normalized = self.normalize_alias(raw_value)
        lowered = str(raw_value or "").strip().lower()
        if not normalized and not lowered:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    v.id,
                    CASE
                        WHEN v.normalized_alias = ? THEN 0
                        WHEN EXISTS (
                            SELECT 1
                            FROM vendor_merge_members mm
                            WHERE mm.vendor_id = v.id AND mm.normalized_raw_name = ?
                        ) THEN 1
                        WHEN LOWER(v.canonical_alias) = ? THEN 2
                        WHEN LOWER(v.canonical_name) = ? THEN 3
                        WHEN LOWER(v.nickname) = ? THEN 4
                        ELSE 5
                    END AS match_rank
                FROM vendors v
                WHERE v.normalized_alias = ?
                   OR EXISTS (
                       SELECT 1
                       FROM vendor_merge_members mm
                       WHERE mm.vendor_id = v.id AND mm.normalized_raw_name = ?
                   )
                   OR LOWER(v.canonical_alias) = ?
                   OR LOWER(v.canonical_name) = ?
                   OR LOWER(v.nickname) = ?
                ORDER BY match_rank ASC, v.id ASC
                LIMIT 1
                """,
                (normalized, normalized, lowered, lowered, lowered, normalized, normalized, lowered, lowered, lowered),
            ).fetchone()
            if row is None:
                return None
            return self._hydrate_vendor(conn, self._root_vendor_id(conn, int(row["id"])))

    def search_vendors(self, query: str) -> list[dict[str, Any]]:
        """Return vendors matching one search term."""

        return self.list_vendors(query)

    def upsert_vendor(
        self,
        canonical_name: str,
        *,
        nickname: str = "",
        category: str = "Uncategorized",
        subcategory: str = "",
        notes: str = "",
        active: bool = True,
    ) -> int:
        """Create or update one vendor using the older upsert shape."""

        _ = subcategory, active
        existing = self.find_vendor(canonical_name)
        categories = [category] if category.strip() else []
        if existing:
            self.update_vendor(
                int(existing["vendorId"]),
                canonical_name=canonical_name,
                canonical_alias=str(existing.get("canonicalAlias", "") or canonical_name),
                nickname=nickname or str(existing.get("nickname", "")),
                notes=notes or str(existing.get("notes", "")),
                categories=categories or list(existing.get("categories", [])),
            )
            return int(existing["vendorId"])
        return self.create_vendor(
            canonical_name,
            canonical_alias=canonical_name,
            nickname=nickname,
            notes=notes,
            categories=categories,
        )

    def upsert_alias(self, canonical_name: str, alias: str, *, match_kind: str = "exact", active: bool = True) -> int:
        """Create one vendor when needed and attach one alias row."""

        _ = match_kind, active
        vendor = self.find_vendor(canonical_name)
        if vendor is None:
            vendor_id = self.create_vendor(canonical_name, canonical_alias=canonical_name)
        else:
            vendor_id = int(vendor["vendorId"])
        self.add_merge_member(vendor_id, alias, source="legacy_upsert_alias")
        return vendor_id

    def normalize_alias(self, value: str) -> str:
        """Normalize one raw vendor string for matching."""

        return "".join(ch.lower() for ch in str(value or "").strip() if ch.isalnum() or ch in {"@", ".", "_", "-"})

    def _replace_categories(self, conn: sqlite3.Connection, vendor_id: int, categories: list[str]) -> None:
        """Replace the shared category set for one alias cluster."""

        normalized: list[str] = []
        seen: set[str] = set()
        for item in categories:
            value = str(item or "").strip()
            if not value or value.lower() in seen:
                continue
            normalized.append(value)
            seen.add(value.lower())
        cluster_vendor_ids = self._cluster_vendor_ids(conn, vendor_id)
        if not cluster_vendor_ids:
            cluster_vendor_ids = [int(vendor_id)]
        now = datetime.now().astimezone().isoformat()
        for current_vendor_id in cluster_vendor_ids:
            conn.execute("DELETE FROM vendor_categories WHERE vendor_id = ?", (current_vendor_id,))
            for category in normalized:
                conn.execute(
                    "INSERT OR IGNORE INTO vendor_categories (vendor_id, category, created_at) VALUES (?, ?, ?)",
                    (current_vendor_id, category, now),
                )

    def _hydrate_vendor(self, conn: sqlite3.Connection, vendor_id: int) -> dict[str, Any]:
        """Build one full vendor payload with categories and merge members."""

        row = self._vendor_row(conn, vendor_id)
        if row is None:
            return {}
        root_vendor_id = self._root_vendor_id(conn, vendor_id)
        root_row = row if root_vendor_id == vendor_id else self._vendor_row(conn, root_vendor_id)
        categories = self._cluster_categories(conn, root_vendor_id)
        merge_members = self._merge_members_for_vendor(conn, vendor_id)
        merged_vendors = self._descendant_vendors(conn, vendor_id) if vendor_id == root_vendor_id else []
        return {
            "vendorId": int(row["id"]),
            "canonicalVendor": str(row["canonical_name"]),
            "canonicalAlias": str(row["canonical_alias"]),
            "aliasKey": str(root_row["normalized_alias"] if root_row is not None else row["normalized_alias"]),
            "nickname": str(row["nickname"]),
            "notes": str(row["notes"]),
            "categories": categories,
            "mergeMembers": merge_members,
            "aliases": [item["rawName"] for item in merge_members],
            "active": bool(int(row["active"] or 0)),
            "isMerged": int(row["merged_into_vendor_id"] or 0) > 0,
            "mergedIntoVendorId": int(row["merged_into_vendor_id"] or 0),
            "rootVendorId": int(root_vendor_id),
            "rootCanonicalVendor": str(root_row["canonical_name"] if root_row is not None else row["canonical_name"]),
            "rootCanonicalAlias": str(root_row["canonical_alias"] if root_row is not None else row["canonical_alias"]),
            "mergedVendors": merged_vendors,
            "updatedAt": str(row["updated_at"]),
        }

    def _vendor_row(self, conn: sqlite3.Connection, vendor_id: int) -> sqlite3.Row | None:
        """Return one raw vendor row by id when it exists."""

        return conn.execute(
            """
            SELECT
                id,
                canonical_name,
                canonical_alias,
                normalized_alias,
                nickname,
                notes,
                active,
                merged_into_vendor_id,
                updated_at
            FROM vendors
            WHERE id = ?
            """,
            (vendor_id,),
        ).fetchone()

    def _root_vendor_id(self, conn: sqlite3.Connection, vendor_id: int) -> int:
        """Follow one vendor merge chain until the active root vendor is found."""

        seen: set[int] = set()
        current = int(vendor_id or 0)
        while current > 0 and current not in seen:
            seen.add(current)
            row = conn.execute(
                "SELECT merged_into_vendor_id FROM vendors WHERE id = ?",
                (current,),
            ).fetchone()
            if row is None:
                break
            next_vendor_id = int(row["merged_into_vendor_id"] or 0)
            if next_vendor_id <= 0:
                return current
            current = next_vendor_id
        return int(vendor_id or 0)

    def _categories_for_vendor(self, conn: sqlite3.Connection, vendor_id: int) -> list[str]:
        """Return category labels attached to one vendor row."""

        return [
            str(item["category"])
            for item in conn.execute(
                "SELECT category FROM vendor_categories WHERE vendor_id = ? ORDER BY category ASC",
                (vendor_id,),
            ).fetchall()
        ]

    def _cluster_categories(self, conn: sqlite3.Connection, vendor_id: int) -> list[str]:
        """Return the unioned category set for one alias cluster."""

        categories: list[str] = []
        seen: set[str] = set()
        for current_vendor_id in self._cluster_vendor_ids(conn, vendor_id):
            for item in self._categories_for_vendor(conn, current_vendor_id):
                value = item.strip()
                normalized = value.lower()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                categories.append(value)
        return sorted(categories, key=str.lower)

    def _cluster_vendor_ids(self, conn: sqlite3.Connection, vendor_id: int) -> list[int]:
        """Return every vendor id that belongs to one alias cluster."""

        root_vendor_id = self._root_vendor_id(conn, vendor_id)
        rows = conn.execute(
            """
            SELECT id
            FROM vendors
            WHERE id = ? OR merged_into_vendor_id = ?
            ORDER BY id ASC
            """,
            (root_vendor_id, root_vendor_id),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def _merge_members_for_vendor(self, conn: sqlite3.Connection, vendor_id: int) -> list[dict[str, Any]]:
        """Return explicit raw-name merge members for one vendor row."""

        rows = conn.execute(
            """
            SELECT raw_name, normalized_raw_name, match_source
            FROM vendor_merge_members
            WHERE vendor_id = ?
            ORDER BY raw_name ASC
            """,
            (vendor_id,),
        ).fetchall()
        return [
            {
                "rawName": str(item["raw_name"]),
                "normalizedRawName": str(item["normalized_raw_name"]),
                "matchSource": str(item["match_source"]),
            }
            for item in rows
        ]

    def _descendant_vendors(self, conn: sqlite3.Connection, root_vendor_id: int) -> list[dict[str, Any]]:
        """Return every merged descendant that currently resolves into one root vendor."""

        cluster_categories = self._cluster_categories(conn, root_vendor_id)
        rows = conn.execute(
            """
            SELECT
                id,
                canonical_name,
                canonical_alias,
                normalized_alias,
                nickname,
                notes,
                active,
                merged_into_vendor_id,
                updated_at
            FROM vendors
            WHERE COALESCE(merged_into_vendor_id, 0) <> 0
            ORDER BY canonical_name ASC
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            vendor_id = int(row["id"])
            if self._root_vendor_id(conn, vendor_id) != root_vendor_id:
                continue
            result.append(
                {
                    "vendorId": vendor_id,
                    "canonicalVendor": str(row["canonical_name"]),
                    "canonicalAlias": str(row["canonical_alias"]),
                    "recordAliasKey": str(row["normalized_alias"]),
                    "nickname": str(row["nickname"]),
                    "notes": str(row["notes"]),
                    "categories": list(cluster_categories),
                    "mergeMembers": self._merge_members_for_vendor(conn, vendor_id),
                    "active": bool(int(row["active"] or 0)),
                    "isMerged": int(row["merged_into_vendor_id"] or 0) > 0,
                    "mergedIntoVendorId": int(row["merged_into_vendor_id"] or 0),
                    "updatedAt": str(row["updated_at"]),
                }
            )
        return result

    def _vendor_matches_query(self, vendor: dict[str, Any], lowered: str, normalized: str) -> bool:
        """Return whether one hydrated root vendor matches the provided search text."""

        for value in self._vendor_search_values(vendor):
            text = str(value or "").strip()
            if not text:
                continue
            if lowered and lowered in text.lower():
                return True
            if normalized and normalized in self.normalize_alias(text):
                return True
        return False

    def _vendor_search_values(self, vendor: dict[str, Any]) -> list[str]:
        """Flatten one hydrated vendor record into searchable display and alias terms."""

        values = [
            str(vendor.get("canonicalVendor", "")),
            str(vendor.get("canonicalAlias", "")),
            str(vendor.get("nickname", "")),
            str(vendor.get("notes", "")),
        ]
        values.extend(str(item.get("rawName", "")) for item in vendor.get("mergeMembers", []))
        for child in vendor.get("mergedVendors", []):
            values.extend(
                [
                    str(child.get("canonicalVendor", "")),
                    str(child.get("canonicalAlias", "")),
                    str(child.get("nickname", "")),
                ]
            )
            values.extend(str(item.get("rawName", "")) for item in child.get("mergeMembers", []))
        return values

    def _assert_alias_available(self, conn: sqlite3.Connection, normalized_alias: str, *, ignore_vendor_ids: set[int]) -> None:
        """Ensure one normalized alias does not collide with another active root vendor."""

        if not normalized_alias:
            raise ValueError("Canonical alias is required.")
        placeholders = ",".join("?" for _ in ignore_vendor_ids) or "0"
        params: list[Any] = [normalized_alias]
        params.extend(int(value) for value in ignore_vendor_ids)
        row = conn.execute(
            f"""
            SELECT id
            FROM vendors
            WHERE normalized_alias = ?
              AND COALESCE(merged_into_vendor_id, 0) = 0
              AND id NOT IN ({placeholders})
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is not None:
            raise ValueError(f"Canonical alias already exists: {normalized_alias}")
