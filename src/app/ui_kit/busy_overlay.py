from __future__ import annotations

import math

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from src.app.ui_kit.controls import CardFrame


class SpinnerWidget(QWidget):
    """Paint a compact indeterminate spinner for blocking overlays."""

    def __init__(self, theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.setInterval(90)
        self.timer.timeout.connect(self._tick)
        self.setFixedSize(28, 28)

    def start(self) -> None:
        """Start the spinner animation when it is visible."""

        if not self.timer.isActive():
            self.timer.start()

    def stop(self) -> None:
        """Stop the spinner animation when the overlay is hidden."""

        if self.timer.isActive():
            self.timer.stop()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center_x = self.width() / 2.0
        center_y = self.height() / 2.0
        radius = min(self.width(), self.height()) / 2.0 - 3.0
        base = QColor(self.theme.hex("focus_ring", self.theme.hex("accent")))
        for index in range(12):
            angle = math.radians((self.angle + (index * 30)) % 360)
            outer_x = center_x + math.cos(angle) * radius
            outer_y = center_y + math.sin(angle) * radius
            inner_x = center_x + math.cos(angle) * (radius - 8.0)
            inner_y = center_y + math.sin(angle) * (radius - 8.0)
            tone = QColor(base)
            tone.setAlpha(max(30, 255 - (index * 18)))
            painter.setPen(QPen(tone, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(int(inner_x), int(inner_y), int(outer_x), int(outer_y))

    def _tick(self) -> None:
        """Advance the spinner and repaint."""

        self.angle = (self.angle + 30) % 360
        self.update()


class BusyOverlay(QWidget):
    """Block app interaction while a long-running expenses job is active."""

    def __init__(self, theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setVisible(False)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.addStretch(1)

        self.panel = CardFrame(
            bg=self.theme.hex("surface_panel"),
            border=self.theme.hex("focus_ring", self.theme.hex("accent")),
            radius=0,
        )
        self.panel.setFixedWidth(380)
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(18, 16, 18, 16)
        panel_layout.setSpacing(10)

        self.spinner = SpinnerWidget(self.theme, self.panel)
        panel_layout.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)

        self.title_label = QLabel("Updating expenses")
        title_font = QFont(self.theme.font_family(), 11)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setStyleSheet(
            f"QLabel {{ color: {self.theme.hex('text_primary')}; background: transparent; border: none; }}"
        )
        panel_layout.addWidget(self.title_label)

        self.detail_label = QLabel("Please wait while the local expense database finishes syncing.")
        detail_font = QFont(self.theme.font_family(), 9)
        self.detail_label.setFont(detail_font)
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet(
            f"QLabel {{ color: {self.theme.hex('text_secondary')}; background: transparent; border: none; }}"
        )
        panel_layout.addWidget(self.detail_label)

        root.addWidget(self.panel, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addStretch(1)

    def set_progress(self, job_id: str, payload: dict[str, object] | None = None) -> None:
        """Update the overlay labels from one blocking-job progress payload."""

        progress = dict(payload or {})
        label = str(progress.get("label", "") or "").strip()
        if not label:
            label = "Refreshing expenses" if job_id == "expenses.refresh" else "Rebuilding expenses"
        percent = int(progress.get("percent", 0) or 0)
        processed = int(progress.get("processed", 0) or 0)
        total = int(progress.get("total", 0) or 0)

        self.title_label.setText(label)
        if total > 0:
            self.detail_label.setText(
                f"{percent}% complete | {processed}/{total}\nInput is paused until the local expense database is ready."
            )
        else:
            scanned = f"{processed:,} messages scanned so far. " if processed > 0 else ""
            self.detail_label.setText(f"{scanned}Input is paused until the local expense database is ready.")

    def show_overlay(self) -> None:
        """Show the overlay and start capturing input."""

        self.setVisible(True)
        self.raise_()
        self.spinner.start()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.grabKeyboard()
        self.grabMouse()

    def hide_overlay(self) -> None:
        """Hide the overlay and release captured input."""

        self.spinner.stop()
        self.releaseKeyboard()
        self.releaseMouse()
        self.setVisible(False)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        scrim = QColor(self.theme.hex("window_bg"))
        scrim.setAlpha(170)
        painter.fillRect(self.rect(), scrim)
        super().paintEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        event.accept()
