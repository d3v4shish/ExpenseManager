from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtGui import QColor


class TransactionTableModel(QAbstractTableModel):
    """Recycle table cells for arbitrarily large transaction popup result sets."""

    HEADERS = ("Time", "Vendor", "Account", "Type", "Amount", "Category", "Reference", "Source", "State")

    def __init__(self, *, debit_color: QColor, credit_color: QColor, ignored_color: QColor, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[dict[str, Any]] = []
        self.debit_color = debit_color
        self.credit_color = credit_color
        self.ignored_color = ignored_color

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
        if role == Qt.ItemDataRole.ForegroundRole:
            if bool(row.get("ignored")):
                return self.ignored_color
            if index.column() in {3, 4}:
                return self.credit_color if str(row.get("direction", "")).lower() == "credit" else self.debit_color
        if role == Qt.ItemDataRole.UserRole:
            return str(row.get("transactionKey", ""))
        return None

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.rows = [dict(row) for row in rows]
        self.endResetModel()

    def row_at(self, row_index: int) -> dict[str, Any]:
        if row_index < 0 or row_index >= len(self.rows):
            return {}
        return dict(self.rows[row_index])

    @staticmethod
    def _display(row: dict[str, Any], column: int) -> str:
        vendor = str(row.get("canonicalAlias") or row.get("canonicalVendor") or row.get("vendor") or row.get("counterparty") or "Unknown")
        direction = str(row.get("direction", "")).lower()
        values = (
            str(row.get("dayLabel") or row.get("timestamp") or ""),
            vendor,
            f"{row.get('bankName', '')} {row.get('accountSuffix', '')}".strip(),
            "Credit" if direction == "credit" else "Debit",
            f"{row.get('currency', 'INR')} {float(row.get('amount', 0.0) or 0.0):,.2f}",
            str(row.get("category", "Uncategorized")),
            str(row.get("reference") or row.get("transactionId") or row.get("title") or ""),
            str(row.get("providerId") or "Unknown"),
            "Ignored" if bool(row.get("ignored")) else "Active",
        )
        return values[column]


__all__ = ["TransactionTableModel"]
