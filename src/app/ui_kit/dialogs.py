from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QLabel, QMessageBox, QPushButton

from src.app.ui_kit.controls import _lighter_hex


def _apply_windows_dark_title_bar(widget) -> None:
    """Ask Windows to use the dark immersive title bar when available."""

    if sys.platform != "win32":
        return
    hwnd = int(widget.winId() or 0)
    if hwnd <= 0:
        return
    value = ctypes.c_int(1)
    size = ctypes.sizeof(value)
    for attribute in (20, 19):
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
                wintypes.HWND(hwnd),
                ctypes.c_uint(attribute),
                ctypes.byref(value),
                ctypes.c_uint(size),
            )
            return
        except Exception:  # noqa: BLE001
            continue


class ThemedMessageBox(QMessageBox):
    """Render one QMessageBox using the app's dark theme."""

    def __init__(self, theme, parent=None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.destructive_buttons: set[QMessageBox.StandardButton] = set()
        self.setFont(QFont(self.theme.font_family(), 10))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._apply_palette()
        self.setStyleSheet(
            f"""
            QMessageBox {{
                background-color: {self.theme.hex("surface_panel")};
                color: {self.theme.hex("text_primary")};
            }}
            QMessageBox QLabel {{
                color: {self.theme.hex("text_primary")};
                background: transparent;
                min-width: 320px;
            }}
            QMessageBox QPushButton {{
                color: {self.theme.hex("text_primary")};
                background-color: {self.theme.hex("surface_panel_alt")};
                border: 1px solid {self.theme.hex("divider", self.theme.hex("border"))};
                border-radius: 0px;
                min-width: 96px;
                min-height: 34px;
                padding: 6px 12px;
            }}
            QMessageBox QPushButton:hover {{
                color: {self.theme.hex("text_primary")};
                background-color: {self.theme.hex("surface_active", self.theme.hex("surface_hover"))};
                border-color: {self.theme.hex("focus_ring", self.theme.hex("accent"))};
            }}
            QMessageBox QPushButton:pressed {{
                color: {self.theme.hex("text_primary")};
                background-color: {_lighter_hex(self.theme.hex("surface_active", self.theme.hex("surface_hover")), 106)};
                border-color: {self.theme.hex("focus_ring", self.theme.hex("accent"))};
            }}
            QMessageBox QPushButton:default {{
                background-color: {self.theme.hex("accent")};
                border-color: {self.theme.hex("accent")};
            }}
            """
        )

    def set_destructive_buttons(self, *buttons: QMessageBox.StandardButton) -> None:
        """Mark one or more standard buttons as destructive."""

        self.destructive_buttons = {button for button in buttons if button is not None}

    def showEvent(self, event) -> None:  # noqa: N802
        """Apply the dark title bar once the native window exists."""

        super().showEvent(event)
        _apply_windows_dark_title_bar(self)
        self._apply_child_styles()

    def _apply_palette(self) -> None:
        """Force a readable dark palette for the dialog and native child controls."""

        palette = QPalette(self.palette())
        palette.setColor(QPalette.ColorRole.Window, QColor(self.theme.hex("surface_panel")))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(self.theme.hex("text_primary")))
        palette.setColor(QPalette.ColorRole.Base, QColor(self.theme.hex("surface_panel_alt")))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(self.theme.hex("surface_panel")))
        palette.setColor(QPalette.ColorRole.Text, QColor(self.theme.hex("text_primary")))
        palette.setColor(QPalette.ColorRole.Button, QColor(self.theme.hex("surface_panel_alt")))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(self.theme.hex("text_primary")))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(self.theme.hex("accent")))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(self.theme.hex("text_primary")))
        self.setPalette(palette)

    def _apply_child_styles(self) -> None:
        """Force label and button styling after QMessageBox builds its child widgets."""

        label_style = (
            "QLabel {"
            f" color: {self.theme.hex('text_primary')};"
            " background: transparent;"
            " border: none;"
            "}"
        )
        informative_style = (
            "QLabel {"
            f" color: {self.theme.hex('text_secondary')};"
            " background: transparent;"
            " border: none;"
            "}"
        )
        button_base = (
            "QPushButton {"
            f" color: {self.theme.hex('text_primary')};"
            f" background-color: {self.theme.hex('surface_panel_alt')};"
            f" border: 1px solid {self.theme.hex('divider', self.theme.hex('border'))};"
            " border-radius: 0px;"
            " min-width: 96px;"
            " min-height: 34px;"
            " padding: 6px 12px;"
            "}"
            "QPushButton:hover {"
            f" color: {self.theme.hex('text_primary')};"
            f" background-color: {self.theme.hex('surface_active', self.theme.hex('surface_hover'))};"
            f" border-color: {self.theme.hex('focus_ring', self.theme.hex('accent'))};"
            "}"
            "QPushButton:pressed {"
            f" color: {self.theme.hex('text_primary')};"
            f" background-color: {_lighter_hex(self.theme.hex('surface_active', self.theme.hex('surface_hover')), 106)};"
            f" border-color: {self.theme.hex('focus_ring', self.theme.hex('accent'))};"
            "}"
        )
        button_primary = (
            "QPushButton {"
            f" color: {self.theme.hex('text_primary')};"
            f" background-color: {self.theme.hex('accent')};"
            f" border: 1px solid {self.theme.hex('accent')};"
            " border-radius: 0px;"
            " min-width: 96px;"
            " min-height: 34px;"
            " padding: 6px 12px;"
            "}"
            "QPushButton:hover {"
            f" color: {self.theme.hex('text_primary')};"
            f" background-color: {_lighter_hex(self.theme.hex('accent'), 106)};"
            f" border-color: {self.theme.hex('focus_ring', self.theme.hex('accent'))};"
            "}"
            "QPushButton:pressed {"
            f" color: {self.theme.hex('text_primary')};"
            f" background-color: {_lighter_hex(self.theme.hex('accent'), 112)};"
            f" border-color: {self.theme.hex('focus_ring', self.theme.hex('accent'))};"
            "}"
        )
        button_destructive = (
            "QPushButton {"
            f" color: {self.theme.hex('text_primary')};"
            f" background-color: {self.theme.hex('surface_panel_alt')};"
            f" border: 1px solid {self.theme.hex('rose')};"
            " border-radius: 0px;"
            " min-width: 96px;"
            " min-height: 34px;"
            " padding: 6px 12px;"
            "}"
            "QPushButton:hover {"
            f" color: {self.theme.hex('text_primary')};"
            f" background-color: {self.theme.hex('surface_active', self.theme.hex('surface_hover'))};"
            f" border-color: {self.theme.hex('rose')};"
            "}"
            "QPushButton:pressed {"
            f" color: {self.theme.hex('text_primary')};"
            f" background-color: {_lighter_hex(self.theme.hex('surface_active', self.theme.hex('surface_hover')), 106)};"
            f" border-color: {self.theme.hex('rose')};"
            "}"
        )

        for label in self.findChildren(QLabel):
            object_name = str(label.objectName() or "").strip().lower()
            label.setPalette(self.palette())
            label.setAutoFillBackground(False)
            label.setStyleSheet(informative_style if "informativelabel" in object_name else label_style)

        for button in self.findChildren(QPushButton):
            button.setPalette(self.palette())
            standard_button = self.standardButton(button)
            is_destructive = standard_button in self.destructive_buttons
            is_primary = standard_button in {
                QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Open,
                QMessageBox.StandardButton.Save,
                QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Apply,
            }
            font = QFont(self.theme.font_family(), 10)
            font.setBold(True)
            button.setFont(font)
            button.setStyleSheet(button_destructive if is_destructive else button_primary if is_primary else button_base)
