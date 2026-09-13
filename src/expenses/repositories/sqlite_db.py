from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path


def connect_sqlite(path: Path, *, timeout_seconds: float = 30.0) -> sqlite3.Connection:
    """Open one SQLite connection with the app repository policy."""

    connection = sqlite3.connect(path, timeout=timeout_seconds)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def ensure_versioned_schema(
    path: Path,
    *,
    component: str,
    target_version: int,
    migrate: Callable[[sqlite3.Connection], None],
) -> None:
    """Run one component migration with an automatic rollback snapshot."""

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed = db_path.exists() and db_path.stat().st_size > 0
    current_version = _read_schema_version(db_path, component) if existed else 0
    if current_version >= target_version:
        with connect_sqlite(db_path) as connection:
            migrate(connection)
        return

    snapshot_dir: Path | None = None
    snapshot_path: Path | None = None
    if existed:
        snapshot_dir = Path(tempfile.mkdtemp(prefix=".expense-manager-migration-", dir=db_path.parent))
        snapshot_path = snapshot_dir / db_path.name
        with connect_sqlite(db_path) as source, sqlite3.connect(snapshot_path) as target:
            source.backup(target)

    try:
        with connect_sqlite(db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            migrate(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_schema_versions (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                INSERT INTO app_schema_versions (component, version, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(component) DO UPDATE SET
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (component, int(target_version)),
            )
            connection.commit()
    except Exception:
        if snapshot_path is not None and snapshot_path.exists():
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{db_path}{suffix}")
                if sidecar.exists():
                    sidecar.unlink()
            os.replace(snapshot_path, db_path)
        elif not existed and db_path.exists():
            db_path.unlink()
        raise
    finally:
        if snapshot_dir is not None:
            shutil.rmtree(snapshot_dir, ignore_errors=True)


def _read_schema_version(path: Path, component: str) -> int:
    """Read a component schema version without creating or changing the DB."""

    try:
        uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_schema_versions'"
            ).fetchone()
            if table is None:
                return 0
            row = connection.execute(
                "SELECT version FROM app_schema_versions WHERE component = ?",
                (component,),
            ).fetchone()
            return int(row[0]) if row is not None else 0
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return 0
