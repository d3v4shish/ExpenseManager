from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


class DataManagementService:
    """Own backup, restore, storage inspection, cleanup, and uninstall preparation."""

    BACKUP_FORMAT = "expense-manager-backup-v1"
    BACKUP_SUFFIX = ".expensemanager-backup"

    def __init__(self, files, *, app_version: str = "0.2.0") -> None:
        self.files = files
        self.app_version = app_version

    def storage_report(self) -> dict[str, Any]:
        categories = []
        for category, root, important in (
            ("config", self.files.config_dir, True),
            ("data", self.files.data_dir, True),
            ("state", self.files.state_dir, False),
            ("cache", self.files.cache_dir, False),
            ("logs", self.files.logs_dir, False),
        ):
            entries = self._scan_root(Path(root))
            categories.append(
                {
                    "category": category,
                    "path": str(root),
                    "important": important,
                    "sizeBytes": sum(int(item["sizeBytes"]) for item in entries),
                    "fileCount": len(entries),
                    "entries": entries,
                }
            )
        return {
            "categories": categories,
            "totalSizeBytes": sum(int(item["sizeBytes"]) for item in categories),
            "importantSizeBytes": sum(int(item["sizeBytes"]) for item in categories if item["important"]),
            "generatedSizeBytes": sum(int(item["sizeBytes"]) for item in categories if not item["important"]),
            "generatedAt": datetime.now().astimezone().isoformat(),
        }

    def create_backup(self, destination: str | Path) -> dict[str, Any]:
        target = Path(destination).expanduser()
        if target.suffix != self.BACKUP_SUFFIX:
            target = target.with_name(f"{target.name}{self.BACKUP_SUFFIX}")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.files.cache_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix="expense-manager-backup-", dir=self.files.cache_dir))
        temp_archive = target.parent / f".{target.name}.tmp-{os.getpid()}"
        entries: list[dict[str, Any]] = []
        try:
            for root_name, root in (("config", self.files.config_dir), ("data", self.files.data_dir)):
                for source in self._regular_files(Path(root)):
                    relative = source.relative_to(root)
                    archive_name = PurePosixPath(root_name, *relative.parts).as_posix()
                    staged = staging_dir / archive_name
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    if source.suffix == ".db":
                        self._snapshot_database(source, staged)
                    elif source.name.endswith((".db-wal", ".db-shm")):
                        continue
                    else:
                        shutil.copy2(source, staged)
                    entries.append(
                        {
                            "path": archive_name,
                            "sizeBytes": staged.stat().st_size,
                            "sha256": self._sha256(staged),
                        }
                    )
            manifest = {
                "format": self.BACKUP_FORMAT,
                "appVersion": self.app_version,
                "createdAt": datetime.now().astimezone().isoformat(),
                "encrypted": False,
                "entries": entries,
            }
            manifest_path = staging_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.write(manifest_path, "manifest.json")
                for item in entries:
                    archive.write(staging_dir / item["path"], item["path"])
            os.replace(temp_archive, target)
            return {
                "path": str(target),
                "sizeBytes": target.stat().st_size,
                "fileCount": len(entries),
                "encrypted": False,
                "warning": "This backup is not encrypted and contains private financial data.",
            }
        finally:
            if temp_archive.exists():
                temp_archive.unlink()
            shutil.rmtree(staging_dir, ignore_errors=True)

    def inspect_backup(self, source: str | Path) -> dict[str, Any]:
        archive_path = Path(source).expanduser()
        with zipfile.ZipFile(archive_path, "r") as archive:
            self._validate_archive_names(archive.namelist())
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Backup manifest is missing or invalid.") from exc
            if manifest.get("format") != self.BACKUP_FORMAT:
                raise ValueError("Unsupported ExpenseManager backup format.")
            entries = manifest.get("entries", [])
            if not isinstance(entries, list):
                raise ValueError("Backup entry list is invalid.")
            for item in entries:
                name = str(item.get("path", ""))
                if name not in archive.namelist():
                    raise ValueError(f"Backup entry is missing: {name}")
                digest = hashlib.sha256(archive.read(name)).hexdigest()
                if digest != str(item.get("sha256", "")):
                    raise ValueError(f"Backup checksum failed: {name}")
            return {
                "path": str(archive_path),
                "format": manifest["format"],
                "appVersion": str(manifest.get("appVersion", "")),
                "createdAt": str(manifest.get("createdAt", "")),
                "encrypted": bool(manifest.get("encrypted", False)),
                "fileCount": len(entries),
                "sizeBytes": archive_path.stat().st_size,
                "entries": entries,
            }

    def restore_backup(self, source: str | Path) -> dict[str, Any]:
        archive_path = Path(source).expanduser()
        inspection = self.inspect_backup(archive_path)
        self.files.cache_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix="expense-manager-restore-", dir=self.files.cache_dir))
        swapped: list[tuple[Path, Path | None]] = []
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                for item in inspection["entries"]:
                    name = str(item["path"])
                    target = staging_dir / PurePosixPath(name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
            for root_name, runtime_root_value in (("config", self.files.config_dir), ("data", self.files.data_dir)):
                source_root = staging_dir / root_name
                if not source_root.exists():
                    continue
                runtime_root = Path(runtime_root_value)
                runtime_root.parent.mkdir(parents=True, exist_ok=True)
                incoming_root = runtime_root.parent / f".{runtime_root.name}.restore-new-{os.getpid()}"
                rollback_root = runtime_root.parent / f".{runtime_root.name}.restore-old-{os.getpid()}"
                if incoming_root.exists():
                    shutil.rmtree(incoming_root)
                if rollback_root.exists():
                    shutil.rmtree(rollback_root)
                shutil.copytree(source_root, incoming_root)
                old_root: Path | None = None
                if runtime_root.exists():
                    os.replace(runtime_root, rollback_root)
                    old_root = rollback_root
                try:
                    os.replace(incoming_root, runtime_root)
                except Exception:
                    if old_root is not None and old_root.exists():
                        os.replace(old_root, runtime_root)
                    raise
                swapped.append((runtime_root, old_root))
            restored_count = sum(len(category.get("entries", [])) for category in self.storage_report().get("categories", []) if category.get("category") in {"config", "data"})
            for _runtime_root, old_root in swapped:
                if old_root is not None:
                    shutil.rmtree(old_root, ignore_errors=True)
            return {**inspection, "restoredFiles": restored_count}
        except Exception:
            for runtime_root, old_root in reversed(swapped):
                if runtime_root.exists():
                    shutil.rmtree(runtime_root, ignore_errors=True)
                if old_root is not None and old_root.exists():
                    os.replace(old_root, runtime_root)
            raise
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def clear_generated(self, categories: list[str]) -> dict[str, Any]:
        allowed = {
            "state": Path(self.files.state_dir),
            "cache": Path(self.files.cache_dir),
            "logs": Path(self.files.logs_dir),
        }
        selected = list(dict.fromkeys(str(value).strip().lower() for value in categories))
        if not selected or any(value not in allowed for value in selected):
            raise ValueError("Only state, cache, and logs can be cleared as generated data.")
        before = self.storage_report()
        removed: list[str] = []
        for category in selected:
            root = allowed[category]
            self._clear_root(root)
            root.mkdir(parents=True, exist_ok=True)
            removed.append(category)
        after = self.storage_report()
        return {
            "removedCategories": removed,
            "freedBytes": max(0, int(before["totalSizeBytes"]) - int(after["totalSizeBytes"])),
            "storage": after,
        }

    def delete_database(self, path: str | Path) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        data_root = Path(self.files.data_dir).resolve()
        if target.suffix != ".db" or not target.is_relative_to(data_root):
            raise ValueError("Only an ExpenseManager database can be deleted.")
        removed = []
        for candidate in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
            if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
                removed.append({"path": str(candidate), "sizeBytes": candidate.stat().st_size})
                candidate.unlink()
        return {"removed": removed, "freedBytes": sum(item["sizeBytes"] for item in removed)}

    def prepare_uninstall(self, *, keep_important: bool) -> dict[str, Any]:
        generated = self.clear_generated(["state", "cache", "logs"])
        removed_important = False
        if not keep_important:
            self._clear_root(Path(self.files.config_dir))
            self._clear_root(Path(self.files.data_dir))
            removed_important = True
        return {
            "keepImportant": bool(keep_important),
            "removedImportant": removed_important,
            "generatedCleanup": generated,
        }

    @staticmethod
    def _snapshot_database(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source_conn, sqlite3.connect(target) as target_conn:
            source_conn.backup(target_conn)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _validate_archive_names(cls, names: list[str]) -> None:
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"Unsafe backup path: {name}")
            if name != "manifest.json" and path.parts[0] not in {"config", "data"}:
                raise ValueError(f"Unsupported backup path: {name}")

    @classmethod
    def _scan_root(cls, root: Path) -> list[dict[str, Any]]:
        result = []
        for path in cls._regular_files(root):
            try:
                stat = path.stat(follow_symlinks=False)
            except OSError:
                continue
            result.append(
                {
                    "path": str(path),
                    "relativePath": str(path.relative_to(root)),
                    "sizeBytes": int(stat.st_size),
                    "kind": cls._file_kind(path),
                }
            )
        return sorted(result, key=lambda item: (-int(item["sizeBytes"]), str(item["relativePath"])))

    @staticmethod
    def _regular_files(root: Path):
        if not root.exists() or root.is_symlink():
            return []
        return [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]

    @staticmethod
    def _file_kind(path: Path) -> str:
        name = path.name.lower()
        if name.endswith((".db", ".db-wal", ".db-shm")):
            return "database"
        if name.endswith(".log") or ".log." in name:
            return "log"
        if name.endswith(".json"):
            return "json"
        if "backup" in name:
            return "backup"
        return "file"

    @staticmethod
    def _clear_root(root: Path) -> None:
        if not root.exists() or root.is_symlink():
            return
        for child in list(root.iterdir()):
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)


__all__ = ["DataManagementService"]
