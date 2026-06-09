from __future__ import annotations

import unittest

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtGui import QColor

from src.expenses.ui.debug_table import CandidateMailTableModel


class CandidateMailTableModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_model_filters_rows_and_exposes_source_ids(self) -> None:
        model = CandidateMailTableModel(
            format_timestamp=lambda value: value or "Never",
            status_colors={
                "parsed": QColor("#00ff00"),
                "failed": QColor("#ffaa00"),
            },
        )
        model.set_rows(
            [
                {
                    "sourceRecordId": 10,
                    "receivedAt": "2026-05-15T12:00:00+05:30",
                    "parseStatus": "parsed",
                    "matchedRuleId": "axis",
                    "sender": "alerts@example.com",
                    "title": "Parsed row",
                },
                {
                    "sourceRecordId": 11,
                    "receivedAt": "2026-05-15T13:00:00+05:30",
                    "parseStatus": "failed",
                    "reasonCode": "no_amount",
                    "sender": "alerts@example.com",
                    "title": "Failed row",
                },
            ]
        )

        self.assertEqual(model.rowCount(), 2)
        self.assertEqual(model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole), "2026-05-15T12:00:00+05:30")
        self.assertEqual(model.data(model.index(1, 1), Qt.ItemDataRole.DisplayRole), "no_amount")
        self.assertEqual(model.source_record_id_at(1), 11)

        model.set_filter_mode("failed")
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.source_record_id_at(0), 11)
        self.assertEqual(model.row_for_source_record_id(10), -1)
        self.assertEqual(model.row_for_source_record_id(11), 0)


if __name__ == "__main__":
    unittest.main()
