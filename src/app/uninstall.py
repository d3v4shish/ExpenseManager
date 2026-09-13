from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.app.data_management import DataManagementService
from src.app.files import RuntimeFiles
from src.app.theme import AppTheme
from src.app.ui_kit import ThemedMessageBox, configure_ui_kit_theme


def run_uninstall() -> int:
    """Ask the user what to retain and clean app-owned runtime data."""

    app = QApplication.instance() or QApplication([])
    root_dir = Path(__file__).resolve().parents[2]
    files = RuntimeFiles(root_dir)
    theme = AppTheme(files.theme_path())
    configure_ui_kit_theme(theme)
    dialog = ThemedMessageBox(theme)
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setWindowTitle("Uninstall ExpenseManager")
    dialog.setText("Choose what to keep before uninstalling ExpenseManager.")
    dialog.setInformativeText("Generated cache, state, and logs are always removed. Keeping financial data is recommended.")
    keep_button = dialog.addButton("Keep Databases and Config", QMessageBox.ButtonRole.AcceptRole)
    delete_button = dialog.addButton("Delete All Data", QMessageBox.ButtonRole.DestructiveRole)
    dialog.addButton(QMessageBox.StandardButton.Cancel)
    dialog.exec()
    clicked = dialog.clickedButton()
    if clicked not in {keep_button, delete_button}:
        return 2
    keep_important = clicked is keep_button
    DataManagementService(files).prepare_uninstall(keep_important=keep_important)
    complete = ThemedMessageBox(theme)
    complete.setIcon(QMessageBox.Icon.Information)
    complete.setWindowTitle("ExpenseManager Uninstall")
    complete.setText("Data retention choice applied.")
    complete.setInformativeText(
        "Databases and configuration were retained."
        if keep_important
        else "All ExpenseManager user data was deleted by your choice."
    )
    complete.setStandardButtons(QMessageBox.StandardButton.Ok)
    complete.exec()
    app.processEvents()
    return 0


__all__ = ["run_uninstall"]
