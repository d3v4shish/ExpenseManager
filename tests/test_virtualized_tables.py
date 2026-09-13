from __future__ import annotations

import unittest

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtGui import QColor

from src.expenses.ui.insights_table import InsightTableModel
from src.expenses.ui.transaction_table import TransactionTableModel


class VirtualizedTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_insight_model_requests_next_page_without_creating_widgets(self) -> None:
        model = InsightTableModel(colors={"high": QColor("#ff0000")})
        requested = []
        model.moreRequested.connect(requested.append)
        model.reset_page(
            {
                "rows": [{"insightKey": "one", "severity": "high", "title": "Flag", "status": "new"}],
                "total": 201,
            }
        )

        self.assertTrue(model.canFetchMore())
        model.fetchMore()
        self.assertEqual(requested, [1])
        self.assertFalse(model.canFetchMore())
        self.assertEqual(model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole), "Flag")

    def test_transaction_model_recycles_cells_for_large_row_sets(self) -> None:
        model = TransactionTableModel(
            debit_color=QColor("#ff0000"),
            credit_color=QColor("#00ff00"),
            ignored_color=QColor("#808080"),
        )
        model.set_rows(
            [
                {
                    "transactionKey": f"txn-{index}",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "canonicalVendor": "Vendor",
                    "amount": index,
                    "currency": "INR",
                    "direction": "debit",
                }
                for index in range(10_000)
            ]
        )

        self.assertEqual(model.rowCount(), 10_000)
        self.assertEqual(model.columnCount(), 9)
        self.assertEqual(model.data(model.index(9999, 1), Qt.ItemDataRole.DisplayRole), "Vendor")
        self.assertEqual(model.row_at(9999)["transactionKey"], "txn-9999")


if __name__ == "__main__":
    unittest.main()
