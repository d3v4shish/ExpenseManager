from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.app.files import RuntimeFiles


@dataclass
class MigrationReport:
    """Describe the result of one legacy data copy attempt."""

    performed: bool
    source_root: str
    target_root: str
    copied_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def migrate_legacy_data(source_root: Path, files: RuntimeFiles) -> MigrationReport:
    """Return a fresh-start report without importing legacy repo-local data."""

    report = MigrationReport(performed=False, source_root=str(source_root), target_root=str(files.data_dir))
    report.notes.append("legacy import is disabled; configure Thunderbird and rebuild the database")
    return report
