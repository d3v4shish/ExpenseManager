from __future__ import annotations

import unittest

from PyQt6.QtCore import QCoreApplication, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QMainWindow, QWidget

from src.app.window import ExpenseManagerWindow


class _Theme:
    def hex(self, _key: str, default: str = "#222222") -> str:
        return default


class _Files:
    def load_card(self, card_id: str) -> dict[str, str]:
        return {"id": card_id, "widget": "TestWidget"}

    def load_card_data(self, _card: dict[str, str]) -> dict:
        return {}


class _TestWidget(QWidget):
    def __init__(self, _config, _data, _theme, _screen_api, _window_api, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(1800)


class _PopupTestWindow(ExpenseManagerWindow):
    """Minimal shell fixture that exercises the real top-window popup controls."""

    def __init__(self) -> None:
        QMainWindow.__init__(self)
        self.theme = _Theme()
        self.files = _Files()
        self.screen_api = object()
        self.widget_mounts = {"TestWidget": _TestWidget}
        self.card_ids = ("panel.expenses",)
        self.tab_targets = {"expenses_tab": "panel.expenses"}
        self.card_configs = {}
        self.card_widgets = {}
        self.card_visibility = {}
        self._build_layout()

    def resizeEvent(self, event) -> None:  # noqa: N802
        QMainWindow.resizeEvent(self, event)

    def closeEvent(self, event) -> None:  # noqa: N802
        event.accept()


class SetupPopupControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("A non-widget Qt application was already created by an earlier model test.")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.window = _PopupTestWindow()
        self.window.resize(960, 640)
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()

    def test_top_window_exposes_source_and_mail_actions(self) -> None:
        self.assertEqual(self.window.sources_button.text(), "SOURCES")
        self.assertEqual(self.window.mail_config_button.text(), "MAIL CFG")
        self.assertEqual(self.window.settings_button.text(), "SETTINGS")
        self.assertTrue(self.window.centralWidget().isAncestorOf(self.window.sources_button))
        self.assertIn("border-radius: 0px", self.window.sources_button.styleSheet())

    def test_popup_closes_without_changing_dashboard_scroll_position(self) -> None:
        scroll_bar = self.window.scroll_area.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        before = scroll_bar.value()
        observed_titles: list[str] = []

        def close_popup() -> None:
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            observed_titles.append(dialog.windowTitle())
            dialog.reject()

        QTimer.singleShot(0, close_popup)
        self.window.open_mail_config_dialog()

        self.assertEqual(observed_titles, ["Mail Configuration"])
        self.assertEqual(scroll_bar.value(), before)

    def test_sources_button_opens_the_source_selection_popup(self) -> None:
        observed_titles: list[str] = []

        def close_popup() -> None:
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            observed_titles.append(dialog.windowTitle())
            dialog.reject()

        QTimer.singleShot(0, close_popup)
        self.window.sources_button.click()

        self.assertEqual(observed_titles, ["Sources"])

    def test_settings_button_opens_a_popup(self) -> None:
        observed_titles: list[str] = []

        def close_popup() -> None:
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            observed_titles.append(dialog.windowTitle())
            dialog.reject()

        QTimer.singleShot(0, close_popup)
        self.window.settings_button.click()

        self.assertEqual(observed_titles, ["Settings & Storage"])


if __name__ == "__main__":
    unittest.main()
