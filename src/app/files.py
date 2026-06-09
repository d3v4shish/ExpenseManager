from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

APP_DIR_NAME = "ExpenseManager"
XDG_APP_DIR_NAME = "expense-manager"
APP_HOME_ENV = "EXPENSE_MANAGER_HOME"


class RuntimeFiles:
    """Resolve app-owned file paths and JSON payloads."""

    def __init__(self, root_dir: Path) -> None:
        """Store the app root and initialize the JSON helpers."""

        self.root_dir = Path(root_dir)
        roots = _runtime_roots()
        self.config_dir = roots["config"]
        self.data_dir = roots["data"]
        self.state_dir = roots["state"]
        self.cache_dir = roots["cache"]
        self.logs_dir = roots["logs"]
        self._state = _JsonStateWriter()
        self._static = _JsonStaticReader()

    def theme_path(self) -> Path:
        """Return the shipped theme file path."""

        return self.root_dir / "assets" / "theme" / "default_theme.json"

    def app_icon_path(self) -> Path:
        """Return the shipped application icon path."""

        return self.root_dir / "assets" / "icons" / "expense_manager_matte.png"

    def app_icon_ico_path(self) -> Path:
        """Return the shipped Windows application icon path."""

        return self.root_dir / "assets" / "icons" / "expense_manager_matte.ico"

    def app_icon_light_path(self) -> Path:
        """Return the shipped light-mode application icon path."""

        return self.root_dir / "assets" / "icons" / "expense_manager_matte_light.png"

    def app_icon_light_ico_path(self) -> Path:
        """Return the shipped light-mode Windows application icon path."""

        return self.root_dir / "assets" / "icons" / "expense_manager_matte_light.ico"

    def card_path(self, card_id: str) -> Path:
        """Return one local card-definition path."""

        return self.root_dir / "src" / "expenses" / "ui" / "cards" / f"{card_id}.json"

    def default_path(self, name: str) -> Path:
        """Return one local default JSON path."""

        return self.root_dir / "src" / "expenses" / "defaults" / name

    def state_path(self, name: str) -> Path:
        """Return one runtime state path."""

        return self.state_dir / "expenses" / name

    def db_path(self, name: str) -> Path:
        """Return one runtime database path."""

        return self.data_dir / "databases" / name

    def cache_path(self, name: str) -> Path:
        """Return one runtime cache path."""

        return self.cache_dir / name

    def log_path(self, name: str = "expense_manager.log") -> Path:
        """Return one runtime log path."""

        return self.logs_dir / name

    def user_path(self, name: str) -> Path:
        """Return one user-owned config path."""

        return self.config_dir / name

    def user_local_path(self, name: str) -> Path:
        """Return one local-only user config path."""

        return self.config_dir / "local" / name

    def read_json(self, path: Path) -> dict[str, Any]:
        """Read one JSON object from disk."""

        return self._static.load(path)

    def read_state(self, name: str) -> dict[str, Any]:
        """Read one runtime state payload with default fallback."""

        state_path = self.state_path(name)
        if state_path.exists():
            return self._state.load(state_path)
        default_path = self.default_path(name)
        if default_path.exists():
            return self._static.load(default_path)
        return {}

    def write_state(self, name: str, payload: dict[str, Any]) -> None:
        """Persist one runtime state payload atomically."""

        self._state.save(self.state_path(name), payload)

    def read_user(self, name: str) -> dict[str, Any]:
        """Read one user config file."""

        return self._static.load(self.user_path(name))

    def read_user_local(self, name: str) -> dict[str, Any]:
        """Read one local-only user config file."""

        return self._static.load(self.user_local_path(name))

    def write_user(self, name: str, payload: dict[str, Any]) -> None:
        """Persist one user config payload atomically."""

        self._state.save(self.user_path(name), payload)

    def write_user_local(self, name: str, payload: dict[str, Any]) -> None:
        """Persist one local-only user config payload atomically."""

        self._state.save(self.user_local_path(name), payload)

    def load_card(self, card_id: str) -> dict[str, Any]:
        """Load one card definition."""

        return self.read_json(self.card_path(card_id))

    def load_card_data(self, card_config: dict[str, Any]) -> dict[str, Any]:
        """Load and merge the runtime datasource for one card."""

        datasource = card_config.get("datasource", {})
        if not isinstance(datasource, dict):
            return {}
        files = datasource.get("files")
        if isinstance(files, list) and files:
            merged: dict[str, Any] = {}
            for name in files:
                if not name:
                    continue
                merged = _deep_merge(merged, self.load_data_file(str(name)))
            return merged
        name = str(datasource.get("file", "")).strip()
        if not name:
            return {}
        return self.load_data_file(name)

    def load_data_file(self, name: str) -> dict[str, Any]:
        """Load one named JSON payload from the correct app-owned namespace."""

        if name.endswith(".db"):
            raise ValueError(f"JSON loader cannot read SQLite datasource: {name}")
        if name == "email_accounts.json":
            user_path = self.user_path(name)
            return self._static.load(user_path) if user_path.exists() else {}
        if name == "thunderbird.json":
            user_local_path = self.user_local_path(name)
            return self._static.load(user_local_path) if user_local_path.exists() else {}
        return self.read_state(name)


class JsonStateStore:
    """Expose runtime JSON state using logical names."""

    def __init__(self, files: RuntimeFiles) -> None:
        """Store the shared runtime file resolver."""

        self.files = files

    def load(self, name: str) -> dict[str, Any]:
        """Load one runtime JSON payload."""

        return self.files.read_state(name)

    def save(self, name: str, payload: dict[str, Any]) -> None:
        """Save one runtime JSON payload."""

        self.files.write_state(name, payload)


class JsonConfigStore:
    """Expose defaults and user config using logical names."""

    def __init__(self, files: RuntimeFiles) -> None:
        """Store the shared runtime file resolver."""

        self.files = files

    def load_default(self, name: str) -> dict[str, Any]:
        """Load one shipped default JSON file."""

        return self.files.read_json(self.files.default_path(name))

    def load_user(self, name: str) -> dict[str, Any]:
        """Load one app-owned user JSON file."""

        return self.files.read_user(name)

    def load_user_local(self, name: str) -> dict[str, Any]:
        """Load one app-owned local-only user JSON file."""

        return self.files.read_user_local(name)

    def save_user(self, name: str, payload: dict[str, Any]) -> None:
        """Persist one app-owned user JSON file."""

        self.files.write_user(name, payload)

    def save_user_local(self, name: str, payload: dict[str, Any]) -> None:
        """Persist one app-owned local-only user JSON file."""

        self.files.write_user_local(name, payload)


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge two nested dictionaries without mutating either input."""

    result = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
            continue
        result[key] = value
    return result


def _runtime_roots() -> dict[str, Path]:
    """Return OS-standard writable app directories."""

    override = str(os.environ.get(APP_HOME_ENV, "")).strip()
    if override:
        home = Path(override).expanduser()
        return {
            "config": home / "config",
            "data": home / "data",
            "state": home / "state",
            "cache": home / "cache",
            "logs": home / "logs",
        }

    home_dir = Path.home()
    if os.name == "nt":
        roaming = Path(os.environ.get("APPDATA") or home_dir / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home_dir / "AppData" / "Local")
        return {
            "config": roaming / APP_DIR_NAME / "config",
            "data": local / APP_DIR_NAME / "data",
            "state": local / APP_DIR_NAME / "state",
            "cache": local / APP_DIR_NAME / "cache",
            "logs": local / APP_DIR_NAME / "logs",
        }

    if sys.platform == "darwin":
        support = home_dir / "Library" / "Application Support" / APP_DIR_NAME
        return {
            "config": support / "config",
            "data": support / "data",
            "state": support / "state",
            "cache": home_dir / "Library" / "Caches" / APP_DIR_NAME,
            "logs": home_dir / "Library" / "Logs" / APP_DIR_NAME,
        }

    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home_dir / ".config")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home_dir / ".local" / "share")
    state_home = Path(os.environ.get("XDG_STATE_HOME") or home_dir / ".local" / "state")
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or home_dir / ".cache")
    state_dir = state_home / XDG_APP_DIR_NAME
    return {
        "config": config_home / XDG_APP_DIR_NAME,
        "data": data_home / XDG_APP_DIR_NAME,
        "state": state_dir,
        "cache": cache_home / XDG_APP_DIR_NAME,
        "logs": state_dir / "logs",
    }


class _JsonStateWriter:
    """Write runtime JSON files atomically with per-path locks."""

    def __init__(self) -> None:
        """Initialize the process-local lock registry."""

        self._lock = threading.RLock()
        self._path_locks: dict[Path, threading.RLock] = {}

    def load(self, path: Path, *, retries: int = 3, retry_delay_seconds: float = 0.02) -> dict[str, Any]:
        """Load one JSON object with a short retry loop for concurrent writers."""

        last_error: Exception | None = None
        lock = self._get_path_lock(path)
        for attempt in range(retries):
            with lock:
                with path.open("r", encoding="utf-8") as handle:
                    try:
                        payload = json.load(handle)
                    except json.JSONDecodeError as exc:
                        last_error = exc
                        payload = None
                if isinstance(payload, dict):
                    return copy.deepcopy(payload)
            if attempt < retries - 1:
                time.sleep(retry_delay_seconds)
        if last_error is not None:
            raise last_error
        return {}

    def save(self, path: Path, payload: dict[str, Any]) -> None:
        """Persist one JSON object using an atomic replace."""

        path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._get_path_lock(path)
        temp_path = path.parent / f"{path.name}.tmp.{uuid.uuid4().hex}"
        with lock:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)

    def _get_path_lock(self, path: Path) -> threading.RLock:
        """Return the process-local lock used for one JSON file path."""

        with self._lock:
            lock = self._path_locks.get(path)
            if lock is None:
                lock = threading.RLock()
                self._path_locks[path] = lock
            return lock


class _JsonStaticReader:
    """Read repo-owned JSON files."""

    def load(self, path: Path) -> dict[str, Any]:
        """Read one JSON object from disk."""

        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            return payload
        return {}
