from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, pyqtSignal
from PyQt6.QtGui import QColor


class InsightTableModel(QAbstractTableModel):
    """Virtualized, incrementally loaded insight table model."""

    moreRequested = pyqtSignal(int)
    HEADERS = ("Severity", "Insight", "Actual", "Baseline", "Change", "Period", "State")

    def __init__(self, *, colors: dict[str, QColor], parent=None) -> None:
        super().__init__(parent)
        self.colors = dict(colors)
        self.rows: list[dict[str, Any]] = []
        self.total = 0
        self.loading = False

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else ""
        return super().headerData(section, orientation, role)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(row, index.column())
        if role == Qt.ItemDataRole.ForegroundRole and index.column() in {0, 4}:
            return self.colors.get(str(row.get("severity", "")).lower())
        if role == Qt.ItemDataRole.UserRole:
            return str(row.get("insightKey", ""))
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(row.get("summary", ""))
        return None

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:  # noqa: N802
        return not parent.isValid() and not self.loading and len(self.rows) < self.total

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:  # noqa: N802
        if parent.isValid() or not self.canFetchMore(parent):
            return
        self.loading = True
        self.moreRequested.emit(len(self.rows))

    def reset_page(self, page: dict[str, Any]) -> None:
        self.beginResetModel()
        self.rows = [dict(row) for row in page.get("rows", [])]
        self.total = int(page.get("total", len(self.rows)) or 0)
        self.loading = False
        self.endResetModel()

    def append_page(self, page: dict[str, Any]) -> None:
        incoming = [dict(row) for row in page.get("rows", [])]
        if incoming:
            start = len(self.rows)
            self.beginInsertRows(QModelIndex(), start, start + len(incoming) - 1)
            self.rows.extend(incoming)
            self.endInsertRows()
        self.total = int(page.get("total", len(self.rows)) or 0)
        self.loading = False

    def fail_page(self) -> None:
        self.loading = False

    def insight_key_at(self, row_index: int) -> str:
        if row_index < 0 or row_index >= len(self.rows):
            return ""
        return str(self.rows[row_index].get("insightKey", ""))

    @staticmethod
    def _display(row: dict[str, Any], column: int) -> str:
        currency = str(row.get("currency", "")).strip()
        if column == 0:
            return str(row.get("severity", "")).title()
        if column == 1:
            return str(row.get("title", ""))
        if column == 2:
            return InsightTableModel._amount(row.get("actualValue"), currency)
        if column == 3:
            return InsightTableModel._amount(row.get("baselineValue"), currency)
        if column == 4:
            value = row.get("deltaPercent")
            return "-" if value is None else f"{float(value):+.0f}%"
        if column == 5:
            start, end = str(row.get("periodStart", "")), str(row.get("periodEnd", ""))
            return start if start == end else f"{start} → {end}"
        if column == 6:
            return str(row.get("status", "")).title()
        return ""

    @staticmethod
    def _amount(value: Any, currency: str) -> str:
        if value is None:
            return "-"
        return f"{currency} {float(value):,.2f}".strip()


class EvidenceTableModel(QAbstractTableModel):
    HEADERS = ("Time", "Vendor", "Account", "Direction", "Amount", "Reference")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else ""
        return super().headerData(section, orientation, role)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole or not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        values = (
            str(row.get("timestamp", "")),
            str(row.get("canonicalAlias") or row.get("canonicalVendor") or row.get("counterparty") or "Unknown"),
            f"{row.get('bankName', '')} {row.get('accountSuffix', '')}".strip(),
            str(row.get("direction", "")).title(),
            f"{row.get('currency', '')} {float(row.get('amount', 0.0) or 0.0):,.2f}".strip(),
            str(row.get("transactionId") or row.get("title") or ""),
        )
        return values[index.column()]

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.rows = [dict(row) for row in rows]
        self.endResetModel()


__all__ = ["EvidenceTableModel", "InsightTableModel"]
