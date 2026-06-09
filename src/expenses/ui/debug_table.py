from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtGui import QColor


class CandidateMailTableModel(QAbstractTableModel):
    """Back the debug candidate table with a lightweight model/view data source."""

    HEADERS = ("Received", "Status", "Rule", "Sender", "Subject")

    def __init__(
        self,
        *,
        format_timestamp: Callable[[str], str],
        status_colors: Mapping[str, QColor],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._format_timestamp = format_timestamp
        self._status_colors = {str(key).strip().lower(): value for key, value in status_colors.items()}
        self._all_rows: list[dict[str, Any]] = []
        self._visible_rows: list[dict[str, Any]] = []
        self._filter_mode = "all"

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._visible_rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if not index.isValid():
            return None
        row = self._visible_rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(row, index.column())
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return self._status_color(str(row.get("parseStatus", "")).strip())
        if role == Qt.ItemDataRole.UserRole:
            return int(row.get("sourceRecordId", 0) or 0)
        return None

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        """Replace the backing row set and reapply the active filter."""

        self._all_rows = [dict(row) for row in rows]
        self._rebuild_visible_rows()

    def set_filter_mode(self, filter_mode: str) -> None:
        """Switch the visible candidate subset."""

        normalized = str(filter_mode or "all").strip().lower() or "all"
        if normalized == self._filter_mode:
            return
        self._filter_mode = normalized
        self._rebuild_visible_rows()

    def source_record_id_at(self, row_index: int) -> int:
        """Return the source record id for one visible row."""

        if row_index < 0 or row_index >= len(self._visible_rows):
            return 0
        return int(self._visible_rows[row_index].get("sourceRecordId", 0) or 0)

    def row_for_source_record_id(self, source_record_id: int) -> int:
        """Return the visible row index for one source record id when present."""

        target = int(source_record_id or 0)
        for index, row in enumerate(self._visible_rows):
            if int(row.get("sourceRecordId", 0) or 0) == target:
                return index
        return -1

    def _rebuild_visible_rows(self) -> None:
        self.beginResetModel()
        if self._filter_mode == "failed":
            self._visible_rows = [row for row in self._all_rows if str(row.get("parseStatus", "")).strip() == "failed"]
        elif self._filter_mode == "rule_miss":
            self._visible_rows = [row for row in self._all_rows if str(row.get("reasonCode", "")).strip() == "no_matching_rule"]
        elif self._filter_mode == "parsed":
            self._visible_rows = [row for row in self._all_rows if str(row.get("parseStatus", "")).strip() == "parsed"]
        else:
            self._visible_rows = list(self._all_rows)
        self.endResetModel()

    def _display_value(self, row: dict[str, Any], column: int) -> str:
        if column == 0:
            return self._format_timestamp(str(row.get("receivedAt", "")).strip())
        if column == 1:
            return self._status_label(row)
        if column == 2:
            return str(row.get("matchedRuleId", "")).strip() or str(row.get("matchedRuleName", "")).strip()
        if column == 3:
            return str(row.get("sender", "")).strip()
        if column == 4:
            return str(row.get("title", "")).strip()
        return ""

    def _status_label(self, row: dict[str, Any]) -> str:
        status = str(row.get("parseStatus", "")).strip()
        if status == "parsed":
            return "Parsed"
        if status == "failed":
            return str(row.get("reasonCode", "")).strip() or "Failed"
        return "Matched"

    def _status_color(self, status: str) -> QColor | None:
        normalized = str(status or "").strip().lower()
        return self._status_colors.get(normalized)
