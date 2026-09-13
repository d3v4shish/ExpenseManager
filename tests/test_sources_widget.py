from __future__ import annotations

import unittest

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

from src.expenses.ui.debug_widget import ExpensesMailDebugWidget
from src.expenses.ui.sources_widget import ReconciliationConflictDialog, SourceSetupDialog


class _Theme:
    def hex(self, _key: str, default: str = "#222222") -> str:
        return default

    def mono_font_family(self) -> str:
        return "monospace"


class SourcesWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("A non-widget Qt application was already created by an earlier model test.")
        cls.app = existing or QApplication([])

    def test_csv_setup_dialog_returns_explicit_mapping(self) -> None:
        dialog = SourceSetupDialog(_Theme())
        dialog.type_combo.setCurrentIndex(1)
        dialog.id_input.setText("bank-csv")
        dialog.path_input.setText("/tmp/transactions.csv")
        dialog.mapping_input.setPlainText('{"date":"Date","amount":"Amount","direction":"Direction"}')
        source = dialog.source_definition()
        self.assertEqual(source["type"], "csv")
        self.assertEqual(source["id"], "bank-csv")
        self.assertEqual(source["mapping"]["amount"], "Amount")

    def test_eml_setup_dialog_exposes_recursive_option(self) -> None:
        dialog = SourceSetupDialog(_Theme())
        dialog.path_input.setText("/tmp/mail")
        source = dialog.source_definition()
        self.assertEqual(source["type"], "eml_folder")
        self.assertEqual(source["id"], "eml-mail")
        self.assertTrue(source["recursive"])
        self.assertFalse(dialog.form.labelForField(dialog.mapping_input).isVisible())
        self.assertFalse(dialog.form.labelForField(dialog.preview_button).isVisible())

    def test_eml_setup_rejects_an_id_that_claims_sms(self) -> None:
        dialog = SourceSetupDialog(_Theme())
        dialog.path_input.setText("/tmp/mail")
        dialog.id_input.setText("phone-sms")
        dialog._mark_id_as_edited(dialog.id_input.text())

        with self.assertRaisesRegex(ValueError, "looks like SMS"):
            dialog.source_definition()

    def test_reconciliation_dialog_exposes_candidate_selection_and_auto_policy_opt_in(self) -> None:
        dialog = ReconciliationConflictDialog(
            {
                "bankName": "Axis",
                "transactionId": "REF-1",
                "timestamp": "2026-09-09T10:00:00+05:30",
                "candidates": [
                    {"signature": "first-value", "amount": 10.0, "currency": "INR", "merchant": "Cafe", "providerIds": ["csv"]},
                    {"signature": "second-value", "amount": 20.0, "currency": "INR", "merchant": "Cafe Updated", "providerIds": ["sms"]},
                ],
            }
        )
        dialog.table.selectRow(1)
        dialog.use_provider_priority.setChecked(True)

        self.assertEqual(dialog.selected_signature(), "second-value")
        self.assertEqual(dialog.selected_provider_id(), "sms")
        self.assertTrue(dialog.use_provider_priority.isChecked())

    def test_debug_widget_keeps_mined_templates_review_only_until_inserted(self) -> None:
        widget = ExpensesMailDebugWidget({"title": "Mail Debug", "datasource": {}}, {}, _Theme(), object(), object())

        widget._handle_template_mining_success(
            {
                "templates": [
                    {
                        "templateKey": "tmpl-1",
                        "senderHint": "axisbank.com",
                        "support": 4,
                        "subjectTemplate": "transaction alert",
                        "bodyTemplate": "your account <ACCOUNT> debited inr <AMOUNT>",
                    }
                ]
            }
        )

        self.assertEqual(widget.mined_template_combo.currentData(), "tmpl-1")
        self.assertTrue(widget.add_mined_button.isEnabled())
        self.assertIn("will not become active", widget.status_message)


if __name__ == "__main__":
    unittest.main()
