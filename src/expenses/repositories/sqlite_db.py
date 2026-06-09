from __future__ import annotations

import sqlite3
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
