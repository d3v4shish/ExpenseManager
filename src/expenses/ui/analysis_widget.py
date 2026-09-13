from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import datetime
import math
import re
from statistics import median
from typing import Any

from PyQt6.QtCore import QRectF, Qt, QThreadPool, QTimer, QStringListModel
from PyQt6.QtGui import QColor, QCursor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCompleter,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.app.ui_kit import BaseWidget, CardFrame, Dot, PillLabel, SectionCard, make_button, make_label
from src.expenses.ui.analysis_workers import (
    AnalysisSnapshotWorker,
    LedgerGroupsWorker,
    LedgerRowsWorker,
    TransactionMutationWorker,
    VendorDetailWorker,
    VendorMutationWorker,
    SourceProvenanceWorker,
)
from src.expenses.ui.transaction_table import TransactionTableModel


def _analysis_hex(theme, role: str) -> str:
    palette = {
        "title": theme.hex("text_primary"),
        "primary": theme.hex("text_primary"),
        "secondary": theme.hex("text_secondary"),
        "muted": theme.hex("text_muted"),
        "disabled": theme.hex("text_disabled", theme.hex("text_muted")),
        "inverse": theme.hex("text_inverse", theme.hex("window_bg")),
        "interactive": theme.hex("blue"),
        "interactive_hover": theme.hex("cyan", theme.hex("blue")),
        "credit": theme.hex("blue"),
        "debit": theme.hex("emerald"),
        "danger": theme.hex("rose"),
        "warning": theme.hex("amber"),
        "info": theme.hex("cyan"),
        "peak": theme.hex("focus_ring", theme.hex("blue")),
        "panel": theme.hex("surface_panel", theme.hex("surface_strong")),
        "panel_alt": theme.hex("surface_panel_alt", theme.hex("card_alt_bg")),
        "active_surface": theme.hex("surface_active", theme.hex("surface_hover")),
        "disabled_surface": theme.hex("surface_disabled", theme.hex("card_alt_bg")),
        "divider": theme.hex("divider", theme.hex("border_soft", theme.hex("border"))),
        "focus": theme.hex("focus_ring", theme.hex("blue")),
    }
    return palette.get(role, theme.hex(role))


def _alpha_hex(color: str, alpha: int) -> str:
    tone = QColor(color)
    tone.setAlpha(max(0, min(255, int(alpha))))
    return tone.name(QColor.NameFormat.HexArgb)


def _analysis_dialog_style(theme) -> str:
    return f"""
    QDialog {{
        background-color: {_analysis_hex(theme, "panel")};
        color: {_analysis_hex(theme, "primary")};
    }}
    QScrollArea {{
        background: transparent;
        border: none;
    }}
    QToolTip {{
        color: {_analysis_hex(theme, "primary")};
        background-color: {_analysis_hex(theme, "panel_alt")};
        border: 1px solid {_analysis_hex(theme, "divider")};
        padding: 4px 6px;
    }}
    """


class MiniBarChart(QWidget):
    def __init__(self, theme, title: str, series: list[dict[str, Any]], amount_color: str | None = None, on_bar_clicked=None, active_value=None, parent=None) -> None:
        super().__init__(parent)
        self.theme        = theme
        self.title        = title
        self.series       = series
        self.amount_color = amount_color or theme.hex("text_muted")
        self.on_bar_clicked = on_bar_clicked
        self._bar_hitboxes : list[tuple[QRectF, dict[str, Any]]] = []
        self.hovered_bar_index = -1
        self.active_value      = active_value
        if callable(on_bar_clicked):
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self.setMouseTracking(True)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setAccessibleName(f"Interactive chart: {title}")
        self.setMinimumHeight(250)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(_analysis_hex(self.theme, "panel_alt")))

        text_color = QColor(_analysis_hex(self.theme, "primary"))
        muted = QColor(_analysis_hex(self.theme, "muted"))
        border = QColor(self.theme.hex("border"))
        accent = QColor(_analysis_hex(self.theme, "interactive"))
        peak_accent = QColor(_analysis_hex(self.theme, "peak"))
        amount_pen = QColor(self.amount_color)

        top_inset = 18
        if self.title:
            painter.setPen(text_color)
            painter.drawText(14, 22, self.title)
            top_inset = 34

        chart_rect = self.rect().adjusted(12, top_inset, -12, -24)
        if not self.series:
            painter.setPen(muted)
            painter.drawText(chart_rect, int(Qt.AlignmentFlag.AlignCenter), "No data")
            return

        painter.setPen(QPen(border, 1))
        painter.drawRect(QRectF(chart_rect))

        values = [float(item.get("amount", 0.0) or 0.0) for item in self.series]
        max_value = max(values) if values else 0.0
        peak_index = values.index(max_value) if values and max_value > 0 else -1
        bar_count = max(1, len(self.series))
        slot_w = chart_rect.width() / bar_count
        bar_w = max(16.0, min(52.0, slot_w * 0.9))
        axis_label_stride = self._label_stride(slot_w, min_pixels=72.0)
        amount_label_stride = self._label_stride(slot_w, min_pixels=40.0)
        base_y = chart_rect.bottom() - 24
        usable_h = max(90.0, chart_rect.height() - 66)

        painter.setPen(QPen(border, 1))
        painter.drawLine(int(chart_rect.left() + 10), int(base_y), int(chart_rect.right() - 10), int(base_y))
        self._draw_grid(painter, chart_rect, base_y)
        self._bar_hitboxes = []

        for index, item in enumerate(self.series):
            amount = float(item.get("amount", 0.0) or 0.0)
            label = str(item.get("label", "") or item.get("month", "") or item.get("day", "") or item.get("week", ""))
            active = self._item_identity(item) == self.active_value
            hovered = index == self.hovered_bar_index
            left = chart_rect.left() + (index * slot_w)
            center = left + (slot_w / 2.0)
            height = 0.0 if max_value <= 0 else (amount / max_value) * usable_h
            bar_x = center - (bar_w / 2.0)
            bar_y = base_y - height
            fill = QColor(peak_accent if index == peak_index else accent)
            fill.setAlpha(255 if active or index == peak_index else 235 if hovered else 205 if amount > 0 else 70)
            highlight = active or hovered or index == peak_index

            painter.setPen(QPen(QColor(_analysis_hex(self.theme, "focus") if highlight else "#00000000"), 1.0) if highlight else Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            bar_rect = QRectF(bar_x, bar_y, bar_w, max(6.0, height))
            painter.drawRect(bar_rect)
            self._bar_hitboxes.append((QRectF(bar_x, chart_rect.top(), bar_w, chart_rect.height()), dict(item)))

            if self._should_draw_sampled_label(index, bar_count, amount_label_stride, highlight=highlight) and (amount > 0 or highlight) and height >= 14.0:
                amount_width = min(chart_rect.width(), max(34.0, min(64.0, slot_w * amount_label_stride)))
                amount_left = self._text_left(center, amount_width, chart_rect.left(), chart_rect.right())
                painter.setPen(amount_pen)
                painter.drawText(
                    int(amount_left),
                    int(max(chart_rect.top() + 8, bar_y - 12)),
                    int(amount_width),
                    12,
                    int(Qt.AlignmentFlag.AlignCenter),
                    self._format_amount(amount),
                )

            if label and self._should_draw_sampled_label(index, bar_count, axis_label_stride, highlight=highlight):
                label_width = min(chart_rect.width(), max(48.0, min(88.0, slot_w * axis_label_stride)))
                label_left = self._text_left(center, label_width, chart_rect.left(), chart_rect.right())
                label_text = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, max(12, int(label_width) - 4))
                painter.setPen(muted)
                painter.drawText(
                    int(label_left),
                    int(base_y + 8),
                    int(label_width),
                    16,
                    int(Qt.AlignmentFlag.AlignCenter),
                    label_text,
                )

    def _draw_grid(self, painter: QPainter, chart_rect: QRectF, base_y: float) -> None:
        """Draw the technical grid pattern behind the bars."""

        minor_pen = QPen(QColor(self.theme.hex("border_soft")), 1)
        major_pen = QPen(QColor(self.theme.hex("border")), 1)

        x = chart_rect.left()
        column = 0
        while x <= chart_rect.right():
            painter.setPen(major_pen if column % 2 == 0 else minor_pen)
            painter.drawLine(int(x), int(chart_rect.top()), int(x), int(base_y))
            x += 40
            column += 1

        y = chart_rect.top()
        row = 0
        while y <= base_y:
            painter.setPen(major_pen if row % 2 == 0 else minor_pen)
            painter.drawLine(int(chart_rect.left()), int(y), int(chart_rect.right()), int(y))
            y += 36
            row += 1

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if callable(self.on_bar_clicked):
            pos = event.position()
            for rect, item in self._bar_hitboxes:
                if rect.contains(pos):
                    self.on_bar_clicked(item)
                    break
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not callable(self.on_bar_clicked) or not self.series:
            super().keyPressEvent(event)
            return
        if event.key() in {Qt.Key.Key_Left, Qt.Key.Key_Right}:
            delta = -1 if event.key() == Qt.Key.Key_Left else 1
            current = self.hovered_bar_index if self.hovered_bar_index >= 0 else 0
            self.hovered_bar_index = max(0, min(len(self.series) - 1, current + delta))
            self.update()
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            index = self.hovered_bar_index if self.hovered_bar_index >= 0 else 0
            self.on_bar_clicked(dict(self.series[index]))
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not callable(self.on_bar_clicked):
            super().mouseMoveEvent(event)
            return
        pos = event.position()
        next_index = -1
        for index, (rect, _) in enumerate(self._bar_hitboxes):
            if rect.contains(pos):
                next_index = index
                break
        if next_index != self.hovered_bar_index:
            self.hovered_bar_index = next_index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self.hovered_bar_index != -1:
            self.hovered_bar_index = -1
            self.update()
        super().leaveEvent(event)

    def _format_amount(self, amount: float) -> str:
        if amount <= 0:
            return "0"
        if amount >= 100000:
            return f"{amount/100000:.1f}L"
        if amount >= 1000:
            return f"{amount/1000:.1f}k"
        return f"{amount:.0f}"

    def _item_identity(self, item: dict[str, Any]) -> Any:
        return item.get("month", item.get("day", item.get("week", item.get("label"))))

    def _label_stride(self, slot_width: float, *, min_pixels: float) -> int:
        """Return the label sampling stride needed to keep dense charts readable."""

        return max(1, int(math.ceil(min_pixels / max(slot_width, 1.0))))

    def _should_draw_sampled_label(self, index: int, bar_count: int, stride: int, *, highlight: bool) -> bool:
        """Draw labels for highlighted bars and a sampled set of baseline ticks."""

        if highlight or stride <= 1:
            return True
        if index in {0, bar_count - 1}:
            return True
        return index % stride == 0

    def _text_left(self, center: float, width: float, left_edge: float, right_edge: float) -> float:
        """Clamp one centered text band so sampled labels can span multiple slots safely."""

        left = center - (width / 2.0)
        if left < left_edge:
            left = left_edge
        if left + width > right_edge:
            left = max(left_edge, right_edge - width)
        return left


class ResponsiveMetricGrid(QWidget):
    """Lay out metric cards with a width-aware column count."""

    def __init__(self, *, min_card_width: int = 550, max_columns: int = 6, parent=None) -> None:
        super().__init__(parent)
        self.min_card_width = min_card_width
        self.max_columns = max_columns
        self._cards: list[QWidget] = []
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setHorizontalSpacing(10)
        self._layout.setVerticalSpacing(10)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_cards(self, cards: list[QWidget]) -> None:
        """Replace the current card set and reflow them immediately."""

        self._cards = list(cards)
        self._reflow()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._reflow()

    def _target_columns(self) -> int:
        width = max(1, self.contentsRect().width())
        if not self._cards:
            return 1
        fitted_columns = max(1, width // self.min_card_width)
        return max(1, min(len(self._cards), self.max_columns, fitted_columns))

    def _clear_layout(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self)

    def _reflow(self) -> None:
        if not self._cards:
            self._clear_layout()
            self.updateGeometry()
            return
        columns = self._target_columns()
        self._clear_layout()
        for index, card in enumerate(self._cards):
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._layout.addWidget(card, index // columns, index % columns)
        for column in range(max(columns, self.max_columns)):
            self._layout.setColumnStretch(column, 0)
        for column in range(columns):
            self._layout.setColumnStretch(column, 1)
        self.updateGeometry()


class ResponsiveChartStrip(QWidget):
    """Lay out the three overview charts with breakpoint-aware rows."""

    def __init__(self, *, one_row_breakpoint: int = 1350, two_row_breakpoint: int = 900, parent=None) -> None:
        super().__init__(parent)
        self.one_row_breakpoint = one_row_breakpoint
        self.two_row_breakpoint = two_row_breakpoint
        self._cards: list[QWidget] = []
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setHorizontalSpacing(10)
        self._layout.setVerticalSpacing(10)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_cards(self, cards: list[QWidget]) -> None:
        """Replace the chart set and reflow immediately."""

        self._cards = list(cards)
        self._reflow()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._reflow()

    def _clear_layout(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self)

    def _reset_stretch(self, columns: int = 3) -> None:
        for column in range(columns):
            self._layout.setColumnStretch(column, 0)

    def _reflow(self) -> None:
        self._clear_layout()
        if not self._cards:
            self.updateGeometry()
            return

        width = max(1, self.contentsRect().width())
        cards = self._cards[:3]
        for card in cards:
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._reset_stretch(3)
        if len(cards) < 3 or width < self.two_row_breakpoint:
            for index, card in enumerate(cards):
                self._layout.addWidget(card, index, 0)
            self._layout.setColumnStretch(0, 1)
            self.updateGeometry()
            return

        if width < self.one_row_breakpoint:
            self._layout.addWidget(cards[0], 0, 0, 1, 2)
            self._layout.addWidget(cards[1], 1, 0)
            self._layout.addWidget(cards[2], 1, 1)
            self._layout.setColumnStretch(0, 1)
            self._layout.setColumnStretch(1, 1)
            self.updateGeometry()
            return

        for index, card in enumerate(cards):
            self._layout.addWidget(card, 0, index)
            self._layout.setColumnStretch(index, 1)
        self.updateGeometry()


class ResponsiveLedgerHeaderControls(QWidget):
    """Lay out the ledger date/search controls inline or in two rows."""

    def __init__(self, *, parent=None) -> None:
        super().__init__(parent)
        self._compact_mode = False
        self._year_widget: QWidget | None = None
        self._month_widget: QWidget | None = None
        self._currency_widget: QWidget | None = None
        self._search_widget: QWidget | None = None
        self._ignored_widget: QWidget | None = None
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setHorizontalSpacing(10)
        self._layout.setVerticalSpacing(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_controls(self, *, year_widget: QWidget, month_widget: QWidget, currency_widget: QWidget, search_widget: QWidget, ignored_widget: QWidget) -> None:
        """Bind the current control widgets and lay them out immediately."""

        self._year_widget = year_widget
        self._month_widget = month_widget
        self._currency_widget = currency_widget
        self._search_widget = search_widget
        self._ignored_widget = ignored_widget
        self._reflow()

    def set_header_compact_mode(self, compact: bool) -> None:
        """Switch between inline and stacked header layouts."""

        compact = bool(compact)
        if self._compact_mode == compact:
            return
        self._compact_mode = compact
        self._reflow()

    def _clear_layout(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self)

    def _reset_stretch(self, columns: int = 5) -> None:
        for column in range(columns):
            self._layout.setColumnStretch(column, 0)

    def _reflow(self) -> None:
        self._clear_layout()
        self._reset_stretch()
        if not all([self._year_widget, self._month_widget, self._currency_widget, self._search_widget, self._ignored_widget]):
            self.updateGeometry()
            return

        if self._compact_mode:
            self._layout.addWidget(self._year_widget, 0, 0)
            self._layout.addWidget(self._month_widget, 0, 1)
            self._layout.addWidget(self._currency_widget, 0, 2)
            self._layout.addWidget(self._ignored_widget, 0, 3, 1, 1, Qt.AlignmentFlag.AlignBottom)
            self._layout.addWidget(self._search_widget, 1, 0, 1, 4)
            self._layout.setColumnStretch(3, 1)
            self.updateGeometry()
            return

        self._layout.addWidget(self._year_widget, 0, 0)
        self._layout.addWidget(self._month_widget, 0, 1)
        self._layout.addWidget(self._currency_widget, 0, 2)
        self._layout.addWidget(self._search_widget, 0, 3)
        self._layout.addWidget(self._ignored_widget, 0, 4, 1, 1, Qt.AlignmentFlag.AlignBottom)
        self._layout.setColumnStretch(3, 1)
        self.updateGeometry()


class ClickableLabel(QLabel):
    def __init__(self, text: str, default_color: str, hover_color: str, callback, *, bold: bool = False, size: int = 10) -> None:
        super().__init__(text)
        self.default_color = default_color
        self.hover_color   = hover_color
        self.callback      = callback
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(text)
        font = self.font()
        font.setBold(bold)
        font.setPointSize(size)
        self.setFont(font)
        self._apply_color(self.default_color)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._apply_color(self.hover_color)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._apply_color(self.default_color)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if callable(self.callback):
            self.callback()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space} and callable(self.callback):
            self.callback()
            event.accept()
            return
        super().keyPressEvent(event)

    def _apply_color(self, color: str) -> None:
        self.setStyleSheet(f"QLabel {{ color: {color}; background: transparent; border: none; padding: 0px; }}")


class ClickableCardFrame(CardFrame):
    def __init__(self, *args, on_click=None, **kwargs) -> None:
        kwargs["hover_effect"] = callable(on_click)
        super().__init__(*args, **kwargs)
        self.on_click = on_click
        if callable(on_click):
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if callable(self.on_click):
            self.on_click()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space} and callable(self.on_click):
            self.on_click()
            event.accept()
            return
        super().keyPressEvent(event)


class TransactionPopupDialog(QDialog):
    def __init__(self, theme, parent=None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setModal(True)
        self.setMinimumSize(920, 620)
        self.setWindowTitle("Transactions")
        self.setStyleSheet(_analysis_dialog_style(theme))
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(14, 14, 14, 14)
        self.root.setSpacing(10)

        self.title_label = make_label("", _analysis_hex(theme, "primary"), 12, True, mono=True)
        self.meta_label  = make_label("", _analysis_hex(theme, "secondary"), 9, False)
        self.meta_label.setWordWrap(True)
        self.root.addWidget(self.title_label)
        self.root.addWidget(self.meta_label)
        self.source_filter = QComboBox()
        self.source_filter.setAccessibleName("Filter transactions by source")
        self.source_filter.currentIndexChanged.connect(self._apply_source_filter)
        self.root.addWidget(self.source_filter)

        self.model = TransactionTableModel(
            debit_color=QColor(_analysis_hex(theme, "debit")),
            credit_color=QColor(_analysis_hex(theme, "credit")),
            ignored_color=QColor(_analysis_hex(theme, "danger")),
        )
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(False)
        self.table.doubleClicked.connect(self._open_vendor)
        self.root.addWidget(self.table, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        self.open_vendor_button = make_button("Open Vendor", theme.hex("accent"), theme.hex("text_primary"), theme.hex("card_alt_bg"))
        self.ignore_button = make_button("Ignore / Restore", theme.hex("border"), theme.hex("text_primary"), theme.hex("card_alt_bg"))
        self.provenance_button = make_button("Source Details", theme.hex("border"), theme.hex("text_primary"), theme.hex("card_alt_bg"))
        self.open_vendor_button.clicked.connect(self._open_vendor)
        self.ignore_button.clicked.connect(self._toggle_ignore)
        self.provenance_button.clicked.connect(self._show_provenance)
        footer.addWidget(self.open_vendor_button)
        footer.addWidget(self.ignore_button)
        footer.addWidget(self.provenance_button)
        footer.addStretch(1)
        close_button = make_button("Close", theme.hex("border"), theme.hex("text_primary"), theme.hex("card_alt_bg"))
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        self.root.addLayout(footer)

        self.on_open_vendor = None
        self.on_toggle_ignore = None
        self.on_show_provenance = None
        self.all_rows: list[dict[str, Any]] = []

    def set_transactions(self, title: str, rows: list[dict[str, Any]], total_label: str, on_open_vendor, on_toggle_ignore, on_show_provenance=None) -> None:
        self.setWindowTitle(title)
        self.title_label.setText(title)
        self.meta_label.setText(f"{len(rows)} row(s) | {total_label}")
        self.on_open_vendor = on_open_vendor
        self.on_toggle_ignore = on_toggle_ignore
        self.on_show_provenance = on_show_provenance
        self.provenance_button.setEnabled(callable(on_show_provenance))
        self.all_rows = [dict(row) for row in rows]
        current = self.source_filter.currentData()
        self.source_filter.blockSignals(True)
        self.source_filter.clear()
        self.source_filter.addItem("All sources", "")
        for provider in sorted({str(row.get("providerId", "")).strip() for row in self.all_rows if str(row.get("providerId", "")).strip()}):
            self.source_filter.addItem(provider, provider)
        index = self.source_filter.findData(current)
        self.source_filter.setCurrentIndex(index if index >= 0 else 0)
        self.source_filter.blockSignals(False)
        self._apply_source_filter()

    def _apply_source_filter(self) -> None:
        provider = str(self.source_filter.currentData() or "")
        visible = [row for row in self.all_rows if not provider or str(row.get("providerId", "")) == provider]
        self.model.set_rows(visible)
        if visible:
            self.table.selectRow(0)

    def _selected_row(self) -> dict[str, Any]:
        selected = self.table.selectionModel().selectedRows()
        return self.model.row_at(selected[0].row()) if selected else {}

    def _open_vendor(self, *_args) -> None:
        row = self._selected_row()
        if not row or not callable(self.on_open_vendor):
            return
        vendor = str(row.get("canonicalVendor") or row.get("vendor") or row.get("counterparty") or "Unknown")
        vendor_key = str(row.get("aliasKey") or row.get("vendorKey") or vendor.lower())
        self.on_open_vendor(vendor, vendor_key)

    def _toggle_ignore(self) -> None:
        row = self._selected_row()
        if not row or not callable(self.on_toggle_ignore):
            return
        key = str(row.get("transactionKey", ""))
        if key:
            self.on_toggle_ignore(key, not bool(row.get("ignored")))
            self.accept()

    def _show_provenance(self) -> None:
        row = self._selected_row()
        key = str(row.get("transactionKey", ""))
        if key and callable(self.on_show_provenance):
            self.on_show_provenance(key)


class LedgerRowCard(QFrame):
    def __init__(self, theme, on_vendor_clicked, on_ignore_clicked, parent=None) -> None:
        super().__init__(parent)
        self.theme                             = theme
        self.on_vendor_clicked                 = on_vendor_clicked
        self.on_ignore_clicked                 = on_ignore_clicked
        self.current_row      : dict[str, Any] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        self.time_label      = make_label("", _analysis_hex(theme, "secondary"), 8, False)
        self.vendor_label    = ClickableLabel("", _analysis_hex(theme, "primary"), _analysis_hex(theme, "interactive_hover"), self._handle_vendor, bold=True, size=9)
        self.account_label   = make_label("", _analysis_hex(theme, "muted"), 8, False)
        self.direction_label = make_label("", _analysis_hex(theme, "debit"), 8, True)
        self.amount_label    = make_label("", _analysis_hex(theme, "primary"), 9, True)
        self.ignored_label   = make_label("Ignored", _analysis_hex(theme, "danger"), 8, False)
        self.ignore_button   = make_button("Ignore", theme.hex("border"), theme.hex("text_primary"), theme.hex("card_alt_bg"))
        self.ignore_button.clicked.connect(self._handle_ignore)
        self.ignore_button.setToolTip("Exclude or restore this transaction from totals, charts, and vendor analytics.")

        layout.addWidget(self.time_label)
        layout.addWidget(self.vendor_label, 1)
        layout.addWidget(self.account_label)
        layout.addWidget(self.direction_label)
        layout.addWidget(self.amount_label)
        layout.addWidget(self.ignored_label)
        layout.addWidget(self.ignore_button)
        self.ignored_label.hide()

    def bind_row(
        self,
        row: dict[str, Any],
        *,
        vendor: str,
        vendor_key: str,
        action_busy: bool = False,
        action_pending: bool = False,
    ) -> None:
        self.current_row = row
        ignored          = bool(row.get("ignored"))
        direction        = str(row.get("direction", "")).strip().lower()
        amount           = float(row.get("amount", 0.0) or 0.0)
        transaction_key  = str(row.get("transactionKey", "")).strip()
        currency         = str(row.get("currency", "INR") or "INR").strip().upper() or "INR"
        amount_label     = f"{currency} {amount:,.0f}" if abs(amount) >= 1 else f"{currency} {amount:,.2f}"
        if direction == "credit":
            amount_label = f"+ {amount_label}"

        self.setStyleSheet(
            "QFrame {"
            f"background: {_alpha_hex(_analysis_hex(self.theme, 'danger') if ignored else _analysis_hex(self.theme, 'panel_alt'), 36 if ignored else 224)};"
            f"border: 1px solid {self.theme.hex('rose') if ignored else self.theme.hex('border')};"
            "border-radius: 0px; }"
        )
        self.time_label.setText(str(row.get("timeLabel", "")))
        self.vendor_label.setText(vendor)
        self.vendor_label.callback = lambda v=vendor, k=vendor_key: self.on_vendor_clicked(v, k)
        self.account_label.setText(str(row.get("accountSuffix", "")))
        self.direction_label.setText("Cr" if direction == "credit" else "Dr")
        self.direction_label.setStyleSheet(
            f"QLabel {{ color: {_analysis_hex(self.theme, 'credit') if direction == 'credit' else _analysis_hex(self.theme, 'debit')}; background: transparent; }}"
        )
        self.amount_label.setText(amount_label)
        self.ignored_label.setVisible(ignored)
        self.ignore_button.setText("Updating..." if action_pending else "Restore" if ignored else "Ignore")
        self.ignore_button.setEnabled(bool(transaction_key) and not action_busy)

    def _handle_vendor(self) -> None:
        if callable(self.vendor_label.callback):
            self.vendor_label.callback()

    def _handle_ignore(self) -> None:
        if not self.current_row:
            return
        transaction_key = str(self.current_row.get("transactionKey", ""))
        ignored         = bool(self.current_row.get("ignored"))
        self.on_ignore_clicked(transaction_key, not ignored)


class ExpensesAnalysisWidget(BaseWidget):
    def __init__(self, widget_config, widget_data, theme, screen_api, window_api, parent=None):
        super().__init__(widget_config, widget_data, theme, screen_api, window_api, parent)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(10)

        self.selected_year                                                            = datetime.now().year
        self.selected_month                                                           = datetime.now().month
        self.selected_currency                                                        = "INR"
        self.selected_page                                                            = 1
        self.page_size                                                                = 200
        self.show_ignored                                                             = False
        self.selected_calendar_day                                                    = ""
        self.ledger_search_text                                                       = ""
        self.pending_ledger_text                                                      = ""
        self.vendor_search_text                                                       = ""
        self.pending_vendor_text                                                      = ""
        self.active_vendor                                                            = ""
        self.active_vendor_key                                                        = ""
        self.active_vendor_alias_label                                                = ""
        self.active_vendor_alias_value                                                = ""
        self.expanded_groups                                                          = set()
        self.group_rows_cache           : dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self.group_widget_pools         : dict[str, list[LedgerRowCard]]              = {}
        self.vendor_lookup              : dict[str, dict[str, Any]]                   = {}
        self.search_input                                                             = None
        self.vendor_search_input                                                      = None
        self.vendor_alias_input                                                       = None
        self.vendor_category_input                                                    = None
        self.vendor_message_label                                                     = None
        self.vendor_feedback_text                                                     = ""
        self.vendor_action_busy                                                       = False
        self.vendor_action_label                                                      = ""
        self.vendor_action_target_vendor                                              = ""
        self.vendor_action_target_key                                                 = ""
        self.vendor_editor_mode                                                       = ""
        self.vendor_form_data           : dict[str, Any]                              = {}
        self.vendor_panel_state                                                       = "idle"
        self.vendor_detail_cache        : dict[tuple[Any, ...], dict[str, Any]]       = {}
        self.vendor_detail_loading_key  : tuple[Any, ...] | None                      = None
        self.loaded_vendor_detail       : dict[str, Any] | None                       = None
        self.vendor_detail_error                                                      = ""
        self.vendor_autocomplete_cache: dict[str, list[dict[str, Any]]]               = {}
        self.section_card                                                             = None
        self.ledger_panel_container                                                   = None
        self.vendor_panel_container                                                   = None
        self.filters_container                                                        = None
        self.summary_container                                                        = None
        self.charts_container                                                         = None
        self.insights_container                                                       = None
        self.year_combo                                                               = None
        self.month_combo                                                              = None
        self.currency_combo                                                           = None
        self.vendor_thread_pool                                                       = QThreadPool.globalInstance()
        self.ledger_rows_thread_pool                                                  = QThreadPool.globalInstance()
        self.ledger_groups_thread_pool                                                = QThreadPool.globalInstance()
        self.transaction_popup                                                       = TransactionPopupDialog(self.theme, self)
        self.vendor_category_entry                                                    = None
        self.alias_category_entry                                                     = None
        self.vendor_notes_input                                                       = None
        self.vendor_name_input                                                        = None
        self.vendor_alias_editor_input                                                = None
        self.vendor_nickname_input                                                    = None
        self.dismissed_merge_suggestions: dict[str, set[str]]                         = {}
        self.ledger_rows_busy                                                         = False
        self.ledger_rows_job_label                                                    = ""
        self.ledger_rows_error                                                        = ""
        self.ledger_action_busy                                                       = False
        self.ledger_action_label                                                      = ""
        self.ledger_action_error                                                      = ""
        self.ledger_action_transaction_key                                            = ""
        self.ledger_groups_cache        : dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self.ledger_groups_busy                                                       = False
        self.ledger_groups_error                                                      = ""
        self._ledger_groups_request_id                                                = 0
        self._active_ledger_groups_request_id                                         = 0
        self._active_ledger_groups_request_key: tuple[Any, ...] | None                = None
        self._ledger_rows_request_id                                                  = 0
        self._active_ledger_rows_request_id                                           = 0
        self._active_ledger_rows_request_key: tuple[Any, ...] | None                  = None
        self._pending_ledger_request: dict[str, Any] | None                           = None
        self.analysis_snapshot_busy                                                  = False
        self.analysis_snapshot_error                                                 = ""
        self._analysis_snapshot_request_id                                           = 0
        self.analysis_snapshot_thread_pool                                           = QThreadPool.globalInstance()

        self.ledger_search_timer = QTimer(self)
        self.ledger_search_timer.setSingleShot(True)
        self.ledger_search_timer.setInterval(180)
        self.ledger_search_timer.timeout.connect(self._apply_ledger_search)

        self.vendor_search_timer = QTimer(self)
        self.vendor_search_timer.setSingleShot(True)
        self.vendor_search_timer.setInterval(180)
        self.vendor_search_timer.timeout.connect(self._apply_vendor_search)

        self._sync_state_from_data(reset=True)
        self._ensure_active_vendor()
        self._render()
        self._ensure_vendor_panel_loaded()

    def apply_reload(self, widget_config: dict[str, Any], widget_data: dict[str, Any]) -> bool:
        self.widget_config   = widget_config
        self.widget_data     = widget_data
        self.datasource_file = self._describe_datasource(widget_config)
        self._sync_state_from_data(reset=False)
        self._reset_ledger_state()
        self._invalidate_vendor_detail_cache()
        self._ensure_active_vendor()
        self._render()
        self._ensure_vendor_panel_loaded()
        self.update()
        self.repaint()
        return True

    def set_job_state(self, job_id: str, running: bool, payload: dict | None = None) -> None:
        if job_id in {"expenses.refresh", "expenses.rebuild"}:
            self._render()

    def _ui_color(self, role: str) -> str:
        return _analysis_hex(self.theme, role)

    def _amount_tone(self, direction: str) -> str:
        return self._ui_color("credit") if direction == "credit" else self._ui_color("debit")

    def _action_button(self, label: str, *, role: str = "neutral", filled: bool = False):
        if role == "neutral":
            button = make_button(label, self._ui_color("divider"), self._ui_color("primary"), self._ui_color("panel_alt"))
        else:
            accent = self._ui_color(role)
            fg = self._ui_color("inverse") if filled else self._ui_color("primary")
            bg = accent if filled else self._ui_color("panel_alt")
            emphasis = "primary" if filled else "neutral"
            button = make_button(label, accent, fg, bg, emphasis=emphasis)
        font = button.font()
        font.setPointSize(9)
        button.setFont(font)
        button.setStyleSheet(
            button.styleSheet()
            + """
            QPushButton {
                min-height: 26px;
                padding: 3px 10px;
            }
            """
        )
        return button

    def _pane_heading_label(self, text: str, *, size: int = 10) -> QLabel:
        label = make_label(text, self._ui_color("secondary"), size, True)
        font = QFont(label.font())
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 98.0)
        label.setFont(font)
        return label

    def _pane_header_frame(self, title: str) -> tuple[QFrame, QHBoxLayout]:
        frame = QFrame()
        frame.setStyleSheet(
            f"""
            QFrame {{
                background-color: {self._ui_color('panel_alt')};
                border: 1px solid {self._ui_color('divider')};
            }}
            """
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        layout.addWidget(self._pane_heading_label(title), 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return frame, layout

    def _plain_meta_label(self, text: str, *, role: str = "secondary") -> QLabel:
        label = make_label(text, self._ui_color(role), 8, False)
        label.setWordWrap(True)
        return label

    def _text_action_button(self, label: str, *, role: str = "danger") -> QWidget:
        accent = QColor(self._ui_color(role)).darker(145).name()
        button = make_button(label, self._ui_color("divider"), accent, self._ui_color("panel"))
        font = button.font()
        font.setPointSize(8)
        font.setBold(False)
        button.setFont(font)
        button.setStyleSheet(
            button.styleSheet()
            + f"""
            QPushButton {{
                color: {accent};
                background-color: transparent;
                border: none;
                min-height: 18px;
                padding: 0px;
                text-align: right;
            }}
            QPushButton:hover {{
                color: {accent};
                background-color: transparent;
                border: none;
            }}
            QPushButton:pressed {{
                color: {accent};
                background-color: transparent;
                border: none;
            }}
            QPushButton:disabled {{
                color: {self._ui_color('disabled')};
                background-color: transparent;
                border: none;
            }}
            """
        )
        return button

    def _apply_dialog_chrome(self, dialog: QDialog) -> None:
        dialog.setStyleSheet(_analysis_dialog_style(self.theme))

    def _dialog_title(self, text: str) -> QLabel:
        return make_label(text, self._ui_color("primary"), 11, True, mono=True)

    def _dialog_note(self, text: str, *, role: str = "secondary", size: int = 8) -> QLabel:
        label = make_label(text, self._ui_color(role), size, False)
        label.setWordWrap(True)
        return label

    def _sync_state_from_data(self, *, reset: bool) -> None:
        filters         = self.widget_data.get("filters", {})
        available_years = list(filters.get("availableYears", [])) or [datetime.now().year]
        default_year    = int(filters.get("defaultYear", datetime.now().year) or datetime.now().year)
        raw_default_month = filters.get("defaultMonth", datetime.now().month)
        default_month   = int(datetime.now().month if raw_default_month is None else raw_default_month)
        available_currencies = [str(value).strip().upper() for value in filters.get("availableCurrencies", []) if str(value).strip()] or ["INR"]
        default_currency = str(filters.get("defaultCurrency", available_currencies[0]) or available_currencies[0]).strip().upper()
        page_size       = int(filters.get("pageSize", 200) or 200)

        if reset or self.selected_year not in available_years:
            self.selected_year = default_year if default_year in available_years else available_years[0]
        if reset:
            self.selected_month = default_month
            self.selected_currency = default_currency

        valid_months = {int(item.get("value", 0) or 0) for item in filters.get("availableMonths", [])}
        if self.selected_month not in valid_months:
            self.selected_month = default_month
        if self.selected_currency not in available_currencies:
            self.selected_currency = default_currency

        self.page_size     = max(25, page_size)
        self.selected_page = max(1, self.selected_page)
        self.vendor_lookup = {}
        for item in self.widget_data.get("vendorAutocomplete", []) or self.widget_data.get("vendorDirectory", []):
            vendor_key = str(item.get("vendorKey", item.get("vendor", ""))).strip()
            if not vendor_key:
                continue
            if vendor_key not in self.vendor_lookup:
                self.vendor_lookup[vendor_key] = item
                continue
            current_type = str(self.vendor_lookup[vendor_key].get("entryType", "canonical")).strip().lower()
            next_type = str(item.get("entryType", "canonical")).strip().lower()
            if current_type != "canonical" and next_type == "canonical":
                self.vendor_lookup[vendor_key] = item

    def _render(self) -> None:
        self.clear_layout(self.root)

        section = SectionCard(
            self.widget_config.get("title", "Ledger Analysis"),
            "",
            bg=self.theme.hex("card_bg"),
            border=self.theme.hex("border"),
        )
        self.section_card = section
        self.root.addWidget(section)

        self.filters_container = self._build_filters()
        section.set_header_aux_widget(self.filters_container, breakpoint=1000)

        loading = self._build_loading_banner()
        if loading is not None:
            section.add_content_widget(loading)

        self.summary_container = QWidget()
        section.add_content_widget(self.summary_container)
        self._render_summary_panel()

        self.charts_container = QWidget()
        section.add_content_widget(self.charts_container)
        self._render_charts_panel()

        self.insights_container = QWidget()
        section.add_content_widget(self.insights_container)
        self._render_insights_panel()

        self.ledger_panel_container = QWidget()
        section.add_content_widget(self.ledger_panel_container)
        self._render_ledger_panel()

        self.vendor_panel_container = QWidget()
        section.add_content_widget(self.vendor_panel_container)
        self._render_vendor_panel()

    def _build_filters(self) -> QWidget:
        year_combo = QComboBox()
        self.year_combo = year_combo
        year_combo.setStyleSheet(self._combo_style())
        for year in self.widget_data.get("filters", {}).get("availableYears", []):
            year_combo.addItem(str(year), year)
        year_index = year_combo.findData(self.selected_year)
        if year_index >= 0:
            year_combo.setCurrentIndex(year_index)
        year_combo.currentIndexChanged.connect(lambda _: self._year_changed(year_combo.currentData()))
        year_box = self._labeled_control("Year", year_combo)

        month_combo = QComboBox()
        self.month_combo = month_combo
        month_combo.setStyleSheet(self._combo_style())
        for item in self.widget_data.get("filters", {}).get("availableMonths", []):
            month_combo.addItem(str(item.get("label", "")), int(item.get("value", 0) or 0))
        month_index = month_combo.findData(self.selected_month)
        if month_index >= 0:
            month_combo.setCurrentIndex(month_index)
        month_combo.currentIndexChanged.connect(lambda _: self._month_changed(month_combo.currentData()))
        month_box = self._labeled_control("Month", month_combo)

        currency_combo = QComboBox()
        self.currency_combo = currency_combo
        currency_combo.setStyleSheet(self._combo_style())
        for currency in self.widget_data.get("filters", {}).get("availableCurrencies", ["INR"]):
            code = str(currency).strip().upper()
            if code:
                currency_combo.addItem(code, code)
        currency_index = currency_combo.findData(self.selected_currency)
        if currency_index >= 0:
            currency_combo.setCurrentIndex(currency_index)
        currency_combo.currentIndexChanged.connect(lambda _: self._currency_changed(currency_combo.currentData()))
        currency_box = self._labeled_control("Currency", currency_combo)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search ledger...")
        self.search_input.setText(self.pending_ledger_text or self.ledger_search_text)
        self.search_input.setStyleSheet(self._input_style())
        self.search_input.textChanged.connect(self._ledger_search_changed)
        search_box = self._labeled_control("Ledger Search", self.search_input)
        search_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.search_input.setMinimumWidth(240)

        ignored_toggle = QCheckBox("Show ignored")
        ignored_toggle.setChecked(self.show_ignored)
        ignored_toggle.setStyleSheet(
            f"QCheckBox {{ color: {self.theme.hex('text_secondary')}; spacing: 6px; }}"
            f"QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {self.theme.hex('border')}; background: {self.theme.hex('card_alt_bg')}; }}"
            f"QCheckBox::indicator:checked {{ background: {self.theme.hex('accent')}; border: 1px solid {self.theme.hex('accent')}; }}"
        )
        ignored_toggle.stateChanged.connect(lambda _: self._ignored_changed(ignored_toggle.isChecked()))
        ignored_toggle.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)

        filters = ResponsiveLedgerHeaderControls()
        filters.set_controls(
            year_widget=year_box,
            month_widget=month_box,
            currency_widget=currency_box,
            search_widget=search_box,
            ignored_widget=ignored_toggle,
        )
        return filters

    def _build_loading_banner(self) -> QWidget | None:
        progress       = self._expense_background()
        total_rows     = int(self.widget_data.get("meta", {}).get("visibleTransactionCount", len(self.widget_data.get("transactions", []))) or 0)
        running        = bool(progress.get("running"))
        initial_update = running and total_rows == 0 and int(progress.get("percent", 0) or 0) < 100
        if not running and not initial_update:
            return None

        card = CardFrame(
            bg=self._ui_color("panel_alt"),
            border=self._ui_color("divider"),
            glow="#00000000",
            radius=0,
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        layout.addWidget(make_label("Sync in progress", self.theme.hex("text_primary"), 11, True))
        label   = str(progress.get("label", "") or "Updating expenses")
        percent = int(progress.get("percent", 0) or 0)
        counts  = f"{int(progress.get('processed', 0) or 0)}/{int(progress.get('total', 0) or 0)}"
        layout.addWidget(make_label(f"{label} | {percent}% | {counts}", self.theme.hex("text_secondary"), 9, False, mono=True))
        layout.addWidget(make_label("The ledger and month-scoped analysis will update as soon as the local expense database finishes syncing.", self.theme.hex("text_muted"), 9, False))
        return card

    def _build_summary(self) -> QGridLayout:
        metrics         = dict(self.widget_data.get("selectedMetrics", {}))
        top_vendor      = dict(self.widget_data.get("selectedTopVendor", {})) or {"vendor": "None", "vendorKey": "", "amount": 0.0}
        all_time_top    = dict(self.widget_data.get("allTimeInsights", {}).get("topVendorByAmount", {})) or self._top_vendor(self._visible_rows(direction="debit"))
        if not all_time_top.get("vendor"):
            all_time_top = {"vendor": "None", "vendorKey": "", "amount": 0.0}
        month_spend     = float(metrics.get("debitTotal", 0.0) or 0.0)
        month_count     = int(metrics.get("transactionCount", 0) or 0)
        active_days     = int(metrics.get("activeDebitDays", 0) or 0)
        avg_active_day  = float(metrics.get("averageActiveDay", 0.0) or 0.0)
        avg_transaction = float(metrics.get("averageDebit", 0.0) or 0.0)

        cards = [
            {
                "title": "Selected Spend",
                "value": self._format_inr(month_spend),
                    "note": "Selected period debit total",
                "tone": self._ui_color("interactive"),
                "on_click": self._show_selected_spend_popup,
                "accented": True,
            },
            {
                "title": "Transactions",
                "value": str(month_count),
                    "note": "Debit and credit rows in selected period",
                "tone": self._ui_color("info"),
                "on_click": self._show_selected_spend_popup,
                "accented": False,
            },
            {
                "title": "Top Vendor",
                "value": top_vendor["vendor"],
                "note": self._format_inr(top_vendor["amount"]),
                "tone": self._ui_color("warning"),
                "on_click": self._summary_vendor_click(top_vendor),
                "accented": True,
            },
            {
                "title": "All-Time Top Vendor",
                "value": all_time_top["vendor"],
                "note": self._format_inr(all_time_top["amount"]),
                "tone": self.theme.hex("accent"),
                "on_click": self._summary_vendor_click(all_time_top),
                "accented": True,
            },
            {
                "title": "Avg Active Day",
                "value": self._format_inr(avg_active_day),
                "note": f"{active_days} active debit day(s)",
                "tone": self._ui_color("danger"),
                "on_click": None,
                "accented": False,
            },
            {
                "title": "Avg Debit",
                "value": self._format_inr(avg_transaction),
                    "note": "Average debit in selected period",
                "tone": self._ui_color("debit"),
                "on_click": None,
                "accented": False,
            },
        ]

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        metric_strip = ResponsiveMetricGrid(min_card_width=240, max_columns=6)
        metric_strip.set_cards(
            [
                self._metric_card(
                    card["title"],
                    card["value"],
                    card["note"],
                    card["tone"],
                    on_click=card["on_click"],
                    accented=bool(card.get("accented")),
                )
                for card in cards
            ]
        )
        layout.addWidget(metric_strip)
        return layout

    def _build_charts(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        yearly_series = list(self.widget_data.get("selectedYearMonths", [])) or self._yearly_series([row for row in self._visible_rows(direction="debit") if self._year(row) == self.selected_year])
        daily_series  = list(self.widget_data.get("selectedMonthDaily", []))
        weekly_series = list(self.widget_data.get("selectedMonthWeekly", []))

        chart_strip = ResponsiveChartStrip(one_row_breakpoint=1350, two_row_breakpoint=900)
        chart_cards = []
        if self.selected_month > 0:
            chart_cards.extend([
                self._chart_card(
                    self._month_title("Daily Debit"),
                    daily_series,
                    on_bar_clicked=self._show_daily_bar_popup,
                    compact=True,
                    chart_height=212,
                ),
                self._chart_card(
                    self._month_title("Weekly Debit"),
                    weekly_series,
                    on_bar_clicked=self._show_weekly_bar_popup,
                    compact=True,
                    chart_height=212,
                ),
            ])
        chart_cards.append(
                self._chart_card(
                    f"Monthly Debit Trend | {self.selected_year}",
                    yearly_series,
                    on_bar_clicked=self._select_month_from_year_chart,
                    active_value=self.selected_month,
                    compact=True,
                    chart_height=212,
                )
        )
        chart_strip.set_cards(chart_cards)
        layout.addWidget(chart_strip)
        return layout

    def _build_insights(self) -> QGridLayout:
        layout = QGridLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)

        top_vendors = list(self.widget_data.get("vendorSummary", []))[:8]
        scoped = self._selected_scoped_metrics()

        top_vendors_card = self._top_vendors_card(top_vendors)
        month_card       = self._month_scoped_kpi_card(scoped)
        peak_card        = self._peak_metrics_card(scoped)
        recurring_card   = self._recurrence_metrics_card(scoped)

        for card in (month_card, peak_card, recurring_card):
            card.setFixedHeight(108)

        layout.addWidget(top_vendors_card, 0, 0, 1, 12)
        layout.addWidget(month_card, 1, 0, 1, 4)
        layout.addWidget(peak_card, 1, 4, 1, 4)
        layout.addWidget(recurring_card, 1, 8, 1, 4)
        for column in range(12):
            layout.setColumnStretch(column, 1)
        return layout

    def _build_ledger(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        if self.selected_month <= 0:
            total_rows = int(self.widget_data.get("selectedMetrics", {}).get("transactionCount", 0) or 0)
            layout.addWidget(self._plain_meta_label(f"All Months {self.selected_year} | {total_rows} row(s)", role="secondary"))
            layout.addWidget(self._year_ledger_summary())
            return layout

        month_rows = self._selected_rows()
        groups     = self._ledger_groups()
        total_rows = sum(int(item.get("rowCount", 0) or 0) for item in groups)
        meta_parts = [self._month_title("Calendar"), f"{total_rows} row(s)"]
        error_text = self.ledger_action_error or self.ledger_rows_error or self.ledger_groups_error
        status_text = self.ledger_action_label or self.ledger_rows_job_label or ("Loading ledger groups" if self.ledger_groups_busy else error_text)
        if status_text:
            meta_parts.append(status_text)
        layout.addWidget(self._plain_meta_label(" | ".join(part for part in meta_parts if part), role="danger" if error_text else "warning" if status_text else "secondary"))

        if self._is_loading_state() and not self.widget_data.get("transactions", []):
            layout.addWidget(self._empty_card("Preparing expenses", "The ledger will fill once the first update finishes."))
            return layout

        if self.ledger_groups_busy and not groups:
            layout.addWidget(self._empty_card("Loading ledger", "Month-scoped ledger groups are loading in the background."))
            return layout

        if self.ledger_groups_error and not groups:
            layout.addWidget(self._empty_card("Ledger load failed", self.ledger_groups_error))
            return layout

        layout.addWidget(self._calendar_ledger(groups))

        if not month_rows and not groups:
            layout.addWidget(self._empty_card("No rows for current month", "The calendar stays month-scoped. Other months can still appear in the year chart above."))
        return layout

    def _year_ledger_summary(self) -> QWidget:
        """Render an actionable month summary when the All Months filter is active."""

        card = CardFrame(bg=self.theme.hex("surface_strong"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        grid = QGridLayout(card)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        months = list(self.widget_data.get("selectedYearMonths", []))
        by_month = {int(item.get("month", 0) or 0): item for item in months}
        for month in range(1, 13):
            item = by_month.get(month, {"month": month, "amount": 0.0})
            label = datetime(self.selected_year, month, 1).strftime("%b")
            month_card = ClickableCardFrame(
                bg=self._ui_color("panel_alt"),
                border=self._ui_color("divider"),
                glow="#00000000",
                radius=0,
                on_click=lambda value=month: self._apply_month_selection(value, partial=False),
            )
            month_card.setAccessibleName(f"Open {label} {self.selected_year} ledger")
            month_layout = QVBoxLayout(month_card)
            month_layout.setContentsMargins(10, 8, 10, 8)
            month_layout.setSpacing(3)
            month_layout.addWidget(make_label(label.upper(), self._ui_color("secondary"), 8, True))
            month_layout.addWidget(make_label(self._format_inr(float(item.get("amount", 0.0) or 0.0)), self._ui_color("primary"), 10, True, mono=True))
            grid.addWidget(month_card, (month - 1) // 4, (month - 1) % 4)
        for column in range(4):
            grid.setColumnStretch(column, 1)
        return card

    def _build_ledger_busy_indicator(self) -> QWidget:
        busy = self.ledger_action_busy or self.ledger_rows_busy or self.ledger_groups_busy
        error_text = self.ledger_action_error or self.ledger_rows_error or self.ledger_groups_error
        label_text = (
            self.ledger_action_label
            or self.ledger_rows_job_label
            or ("Loading ledger groups" if self.ledger_groups_busy else error_text or "Ledger ready")
        )
        color = self.theme.hex("amber") if busy else self.theme.hex("rose") if error_text else self.theme.hex("text_muted")
        pill = CardFrame(
            bg=self._ui_color("active_surface") if (busy or error_text) else self._ui_color("panel_alt"),
            border=color if (busy or error_text) else self._ui_color("divider"),
            glow="#00000000",
            radius=0,
        )
        row = QHBoxLayout(pill)
        row.setContentsMargins(8, 3, 8, 3)
        row.setSpacing(6)
        row.addWidget(Dot(color, 7), 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(make_label(label_text.upper(), self._ui_color("primary"), 8, True), 0, Qt.AlignmentFlag.AlignVCenter)
        return pill

    def _status_pill(self, label_text: str, color: str) -> QWidget:
        """Render a compact status badge for vendor actions."""

        pill = CardFrame(
            bg=self._ui_color("active_surface"),
            border=color,
            glow="#00000000",
            radius=0,
        )
        row = QHBoxLayout(pill)
        row.setContentsMargins(8, 3, 8, 3)
        row.setSpacing(6)
        row.addWidget(Dot(color, 7), 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(make_label((label_text or "STATUS").upper(), self._ui_color("primary"), 8, True), 0, Qt.AlignmentFlag.AlignVCenter)
        return pill

    def _build_vendor_panel(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header_frame, header = self._pane_header_frame("Vendor Detail")
        header.addStretch(1)
        new_button = self._action_button("New Vendor")
        new_button.clicked.connect(self._begin_vendor_create)
        header.addWidget(new_button)
        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else None
        if detail:
            merge_button = self._action_button("Merge Vendor", role="interactive")
            merge_button.clicked.connect(lambda: self._begin_vendor_merge(detail))
            merge_button.setEnabled(not self.vendor_action_busy)
            merge_button.setToolTip("Move this vendor into another canonical vendor and keep the target record active.")
            header.addWidget(merge_button)
            edit_button = self._action_button("Edit Vendor")
            edit_button.clicked.connect(lambda: self._begin_vendor_edit(detail))
            edit_button.setEnabled(not self.vendor_action_busy)
            header.addWidget(edit_button)
            delete_button = self._action_button("Delete Vendor", role="danger")
            delete_button.clicked.connect(lambda: self._delete_vendor_crud(detail))
            delete_button.setEnabled(not self.vendor_action_busy)
            delete_button.setToolTip("Delete the catalog vendor record and then reconcile vendor mappings across the local expense store.")
            header.addWidget(delete_button)
        if self.active_vendor:
            clear_button = self._action_button("Clear Vendor")
            clear_button.clicked.connect(self._clear_vendor)
            header.addWidget(clear_button)
        layout.addWidget(header_frame)

        self.vendor_search_input = QLineEdit()
        self.vendor_search_input.setPlaceholderText("Search vendor names or aliases")
        if bool(self.widget_data.get("meta", {}).get("vendorDirectoryTruncated")):
            self.vendor_search_input.setToolTip(
                "Suggestions show the most active vendors in this range. Press Enter to search the full local vendor directory."
            )
        self.vendor_search_input.setText(self.pending_vendor_text or self.vendor_search_text)
        self.vendor_search_input.setStyleSheet(self._input_style())
        self.vendor_search_input.textChanged.connect(self._vendor_search_changed)
        self.vendor_search_input.returnPressed.connect(self._select_vendor_from_input)
        completer = QCompleter(self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        completer.setModel(QStringListModel(self._vendor_autocomplete_labels(self.vendor_search_input.text()), completer))
        completer.activated.connect(self._select_vendor_from_completion)
        self.vendor_search_input.setCompleter(completer)
        layout.addWidget(self.vendor_search_input)

        if self.vendor_action_busy:
            layout.addWidget(self._status_pill(self.vendor_action_label or "Updating vendor", self.theme.hex("amber")))

        if self.active_vendor_alias_value:
            alias_hint = make_label(
                f"Viewing canonical vendor via alias: {self.active_vendor_alias_value}",
                self.theme.hex("text_secondary"),
                8,
                False,
            )
            alias_hint.setWordWrap(True)
            layout.addWidget(alias_hint)

        if self.vendor_panel_state == "loading":
            layout.addWidget(self._empty_card(self.active_vendor or "Loading vendor", "Loading vendor detail..."))
            return layout
        if self.vendor_panel_state == "error":
            layout.addWidget(self._empty_card(self.active_vendor or "Vendor error", self.vendor_detail_error or "Failed to load vendor detail."))
            return layout
        if not detail and self.vendor_editor_mode != "new":
            layout.addWidget(self._empty_card("No vendor selected", "Select one from the ledger, top vendors, or search."))
            return layout

        vendor_record = detail.get("vendorRecord") if isinstance(detail, dict) else None
        if self.vendor_editor_mode in {"new", "edit"}:
            layout.addWidget(self._vendor_crud_card(detail, vendor_record))
            return layout

        layout.addWidget(self._build_vendor_group(detail))
        layout.addWidget(self._build_alias_group(detail, vendor_record))
        layout.addWidget(self._build_category_group(detail))
        layout.addWidget(self._vendor_crud_card(detail, vendor_record))
        return layout

    def _build_vendor_group(self, detail: dict[str, Any]) -> QWidget:
        """Render the canonical vendor record section without alias-cluster totals."""

        vendor_group = detail.get("vendorGroup", {}) if isinstance(detail.get("vendorGroup", {}), dict) else {}
        categories = [str(item).strip() for item in vendor_group.get("categories", []) if str(item).strip()]
        updated_at = self._format_short_dt(str(vendor_group.get("updatedAt", ""))) if str(vendor_group.get("updatedAt", "")).strip() else "Not tracked"

        card = CardFrame(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        metric_strip = ResponsiveMetricGrid(min_card_width=240)
        metric_strip.set_cards(
            [
                self._metric_card(
                    "Vendor Name",
                    str(vendor_group.get("canonicalVendor", detail.get("canonicalVendor", "Unknown"))),
                    f"Updated {updated_at}",
                    self._ui_color("info"),
                ),
                self._metric_card(
                    "Transactions",
                    str(int(vendor_group.get("transactionCount", 0) or 0)),
                    "Rows matching this canonical vendor only",
                    self._ui_color("interactive"),
                ),
                self._metric_card(
                    "Spend This Month",
                    self._format_inr(float(vendor_group.get("spendThisMonth", 0.0) or 0.0)),
                    "Current calendar-month debit for this vendor identity",
                    self._ui_color("debit"),
                ),
                self._metric_card(
                    "Spend This Year",
                    self._format_inr(float(vendor_group.get("spendThisYear", 0.0) or 0.0)),
                    "Current calendar-year debit for this vendor identity",
                    self.theme.hex("accent"),
                ),
                self._metric_card(
                    "Tags",
                    str(len(categories)),
                    ", ".join(categories) if categories else "No vendor tags configured",
                    self._ui_color("secondary"),
                ),
                self._metric_card(
                    "Alias Reference",
                    str(vendor_group.get("canonicalAlias", detail.get("canonicalAlias", "Unknown"))),
                    "Reference alias stored on the vendor record",
                    self._ui_color("secondary"),
                ),
            ]
        )
        layout.addWidget(metric_strip)

        notes = str(vendor_group.get("notes", "") or "").strip()
        if notes:
            layout.addWidget(self._empty_card("Vendor Notes", notes))
        charts = QGridLayout()
        charts.setContentsMargins(0, 0, 0, 0)
        charts.setHorizontalSpacing(10)
        charts.setVerticalSpacing(10)
        charts.addWidget(
            self._chart_card(
                "Vendor Monthly Spend",
                list(vendor_group.get("monthlyTotals", [])),
                on_bar_clicked=self._show_vendor_scope_monthly_popup,
            ),
            0,
            0,
        )
        charts.addWidget(
            self._chart_card(
                "Vendor Yearly Spend",
                list(vendor_group.get("yearlyTotals", [])),
                amount_color=self.theme.hex("accent"),
                on_bar_clicked=self._show_vendor_scope_yearly_popup,
            ),
            0,
            1,
        )
        layout.addLayout(charts)
        vendor_record = detail.get("vendorRecord") if isinstance(detail.get("vendorRecord", {}), dict) else None
        fuzzy_review = self._build_fuzzy_review_block(detail, vendor_record)
        if fuzzy_review is not None:
            layout.addWidget(fuzzy_review)
        return card

    def _build_alias_group(self, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> QWidget:
        """Render the alias-cluster section with cumulative totals, charts, and merge actions."""

        alias_group = detail.get("aliasGroup", {}) if isinstance(detail.get("aliasGroup", {}), dict) else {}
        categories = [str(item).strip() for item in alias_group.get("categories", []) if str(item).strip()]
        merge_members = list(alias_group.get("mergeMembers", []))
        merged_vendors = list(alias_group.get("mergedVendors", []))
        alias_vendors = list(alias_group.get("aliasVendors", []))
        selected_month = alias_group.get("selectedMonth", {}) if isinstance(alias_group.get("selectedMonth", {}), dict) else {}
        resolved_vendor_id = self._vendor_record_id(vendor_record)
        merge_note = f"{len(merge_members)} raw alias row(s)"

        card = CardFrame(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        heading_frame, _ = self._pane_header_frame("Alias Mapping")
        layout.addWidget(heading_frame)

        alias_transactions = list(detail.get("transactions", []))
        metric_strip = ResponsiveMetricGrid(min_card_width=220, max_columns=6)
        metric_strip.set_cards(
            [
                self._metric_card(
                    "Alias Name",
                    str(alias_group.get("canonicalAlias", detail.get("canonicalAlias", "Unknown"))),
                    self._compact_note_text(f"Alias key {str(alias_group.get('aliasKey', detail.get('aliasKey', 'unknown')))}"),
                    self._ui_color("warning"),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Merged Vendors",
                    str(int(alias_group.get("mergedVendorCount", len(alias_vendors)) or 0)),
                    "Open attached vendors",
                    self._ui_color("interactive"),
                    on_click=lambda vendors=alias_vendors, alias_name=str(alias_group.get("canonicalAlias", detail.get("canonicalAlias", "Alias"))): self._show_alias_vendors_popup(alias_name, vendors),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Total Spend",
                    self._format_inr(float(alias_group.get("totalDebit", 0.0) or 0.0)),
                    "All debit rows",
                    self._ui_color("debit"),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Spend This Month",
                    self._format_inr(float(alias_group.get("spendThisMonth", 0.0) or 0.0)),
                    "Calendar month debit",
                    self._ui_color("debit"),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Avg Spend / Month",
                    self._format_inr(float(alias_group.get("averageSpendPerMonth", 0.0) or 0.0)),
                    self._compact_note_text(f"{int(alias_group.get('activeMonths', 0) or 0)} active month(s)"),
                    self._ui_color("info"),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Transactions",
                    str(int(alias_group.get("transactionCount", 0) or 0)),
                    "Open alias rows",
                    self._ui_color("interactive"),
                    on_click=lambda vendor=str(detail.get("canonicalAlias", detail.get("vendor", "Alias"))), rows=alias_transactions: self._show_vendor_transactions_popup(vendor, rows),
                    compact=True,
                    fixed_height=82,
                ),
            ]
        )
        layout.addWidget(metric_strip)

        category_block = QVBoxLayout()
        category_block.setContentsMargins(0, 0, 0, 0)
        category_block.setSpacing(6)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title_row.addWidget(make_label("Shared categories", self.theme.hex("text_secondary"), 8, True))
        title_row.addWidget(make_label("Applied to this alias and every merged vendor", self._ui_color("secondary"), 8, False))
        title_row.addStretch(1)
        category_block.addLayout(title_row)

        chips_row = QHBoxLayout()
        chips_row.setContentsMargins(0, 0, 0, 0)
        chips_row.setSpacing(6)
        if categories:
            for category in categories:
                chip = make_button(
                    f"{category} x",
                    self._ui_color("divider"),
                    self._ui_color("primary"),
                    self._ui_color("panel_alt"),
                )
                chip.setEnabled(not self.vendor_action_busy and resolved_vendor_id > 0)
                chip.setToolTip("Remove this shared category from the alias cluster and all merged vendors.")
                chip.clicked.connect(lambda _, cat=category, current_detail=dict(detail), current_record=vendor_record: self._remove_alias_category(cat, current_detail, current_record))
                chips_row.addWidget(chip)
        else:
            chips_row.addWidget(make_label("No shared categories configured.", self._ui_color("secondary"), 8, False))
        chips_row.addStretch(1)
        category_block.addLayout(chips_row)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(8)
        self.alias_category_entry = QLineEdit()
        self.alias_category_entry.setPlaceholderText("Add shared alias category")
        self.alias_category_entry.setStyleSheet(self._input_style())
        self.alias_category_entry.setEnabled(not self.vendor_action_busy and resolved_vendor_id > 0)
        input_row.addWidget(self.alias_category_entry, 1)
        add_category = make_button("Add Category", self._ui_color("divider"), self._ui_color("primary"), self._ui_color("panel_alt"))
        add_category.setEnabled(not self.vendor_action_busy and resolved_vendor_id > 0)
        add_category.setToolTip("Attach this category to the alias and all merged vendors in the cluster.")
        add_category.clicked.connect(lambda: self._add_alias_category(detail, vendor_record))
        input_row.addWidget(add_category)
        category_block.addLayout(input_row)
        layout.addLayout(category_block)

        charts = QGridLayout()
        charts.setContentsMargins(0, 0, 0, 0)
        charts.setHorizontalSpacing(10)
        charts.setVerticalSpacing(10)
        charts.addWidget(
            self._chart_card(
                "Alias Monthly Spend",
                list(alias_group.get("monthlyTotals", [])),
                on_bar_clicked=self._show_alias_monthly_bar_popup,
            ),
            0,
            0,
        )
        charts.addWidget(
            self._chart_card(
                "Alias Yearly Spend",
                list(alias_group.get("yearlyTotals", [])),
                amount_color=self._ui_color("peak"),
                on_bar_clicked=self._show_alias_yearly_bar_popup,
            ),
            0,
            1,
        )
        layout.addLayout(charts)

        if merged_vendors:
            layout.addWidget(make_label("Merged vendors", self.theme.hex("text_secondary"), 8, True))
            for merged_vendor in merged_vendors:
                merged_card = CardFrame(bg=self.theme.hex("surface_strong"), border=self._ui_color("divider"), glow="#00000000", radius=0)
                merged_row = QHBoxLayout(merged_card)
                merged_row.setContentsMargins(10, 4, 10, 4)
                merged_row.setSpacing(8)
                merged_name = str(merged_vendor.get("canonicalVendor", "")).strip() or "UNKNOWN"
                merged_alias = str(merged_vendor.get("canonicalAlias", "")).strip() or merged_name
                merged_row.addWidget(make_label(merged_name, self._ui_color("primary"), 8, True, mono=True), 2)
                merged_row.addWidget(make_label(f"Alias {merged_alias}", self.theme.hex("text_secondary"), 8, False), 2)
                merged_row.addWidget(make_label(f"{len(merged_vendor.get('mergeMembers', []))} raw alias row(s)", self.theme.hex("text_secondary"), 8, False), 1)
                merged_row.addStretch(1)
                unmerge_button = self._text_action_button("Unmerge")
                unmerge_button.setEnabled(not self.vendor_action_busy)
                unmerge_button.setToolTip("Remove this merged vendor from the current alias cluster and restore its separate vendor row.")
                unmerge_button.setMaximumWidth(88)
                unmerge_button.clicked.connect(lambda _, item=dict(merged_vendor): self._begin_vendor_unmerge(item))
                merged_row.addWidget(unmerge_button)
                layout.addWidget(merged_card)

        if merge_members:
            layout.addWidget(make_label("Merged alias rows", self.theme.hex("text_secondary"), 8, True))
            merge_member_grid = ResponsiveMetricGrid(min_card_width=260, max_columns=6)
            merge_member_grid.set_cards(
                [self._slim_merge_member_card(member, resolved_vendor_id) for member in merge_members]
            )
            layout.addWidget(merge_member_grid)
        return card

    def _build_category_group(self, detail: dict[str, Any]) -> QWidget:
        """Render the global category metrics and category-spend breakdown."""

        category_group = detail.get("categoryGroup", {}) if isinstance(detail.get("categoryGroup", {}), dict) else {}
        categories = [str(item).strip() for item in category_group.get("categories", []) if str(item).strip()]
        category_summary = list(category_group.get("categorySummary", []))
        month_breakdown = list(category_group.get("monthBreakdown", []))
        selected_month = category_group.get("selectedMonth", {}) if isinstance(category_group.get("selectedMonth", {}), dict) else {}
        category_name = str(category_group.get("categoryName", detail.get("category", "Uncategorized"))) or "Uncategorized"

        card = CardFrame(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        heading_frame, _ = self._pane_header_frame("Category")
        layout.addWidget(heading_frame)

        category_transactions = list(detail.get("categoryTransactions", []))
        metric_strip = ResponsiveMetricGrid(min_card_width=220, max_columns=6)
        metric_strip.set_cards(
            [
                self._metric_card(
                    "Category Name",
                    category_name,
                    "Across the app",
                    self._ui_color("debit"),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Aliases In Category",
                    str(int(category_group.get("aliasCount", 0) or 0)),
                    "Mapped alias clusters",
                    self._ui_color("info"),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Transactions",
                    str(int(category_group.get("transactionCount", 0) or 0)),
                    "Open category rows",
                    self._ui_color("interactive"),
                    on_click=lambda category=category_name, rows=category_transactions: self._show_vendor_transactions_popup(category, rows),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Spend This Month",
                    self._format_inr(float(category_group.get("spendThisMonth", 0.0) or 0.0)),
                    "Calendar month debit",
                    self._ui_color("debit"),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Avg Spend / Month",
                    self._format_inr(float(category_group.get("averageSpendPerMonth", 0.0) or 0.0)),
                    self._compact_note_text(f"{int(category_group.get('activeMonths', 0) or 0)} active month(s)"),
                    self._ui_color("info"),
                    compact=True,
                    fixed_height=82,
                ),
                self._metric_card(
                    "Vendor Tags",
                    str(len(categories)),
                    self._compact_list_note(categories, fallback="No vendor tags"),
                    self._ui_color("secondary"),
                    compact=True,
                    fixed_height=82,
                ),
            ]
        )
        layout.addWidget(metric_strip)

        charts = QGridLayout()
        charts.setContentsMargins(0, 0, 0, 0)
        charts.setHorizontalSpacing(10)
        charts.setVerticalSpacing(10)
        charts.addWidget(
            self._chart_card(
                "Category Monthly Spend",
                list(category_group.get("monthlyTotals", [])),
                on_bar_clicked=self._show_category_scope_monthly_popup,
            ),
            0,
            0,
            1,
            3,
        )
        charts.addWidget(
            self._chart_card(
                "Category Yearly Spend",
                list(category_group.get("yearlyTotals", [])),
                amount_color=self._ui_color("debit"),
                on_bar_clicked=self._show_category_scope_yearly_popup,
            ),
            1,
            0,
        )
        charts.addWidget(
            self._chart_card(
                "Category Breakdown",
                category_summary[:8],
                on_bar_clicked=self._show_category_breakdown_popup,
            ),
            1,
            1,
        )
        charts.addWidget(
            self._chart_card(
                f"Category Month Scope | {str(selected_month.get('label', 'No month'))}",
                month_breakdown[:8],
                amount_color=self._ui_color("debit"),
                on_bar_clicked=self._show_category_month_breakdown_popup,
            ),
            1,
            2,
        )
        for column in range(3):
            charts.setColumnStretch(column, 1)
        layout.addLayout(charts)
        return card

    def _slim_merge_member_card(self, member: dict[str, Any], resolved_vendor_id: int) -> QWidget:
        """Render one compact flat-bento alias-merge card."""

        raw_name = str(member.get("rawName", "")).strip() or "UNKNOWN"
        match_source = str(member.get("matchSource", "manual")).strip().upper() or "MANUAL"

        card = CardFrame(bg=self.theme.hex("surface_strong"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        card.setFixedHeight(70)

        body = QVBoxLayout(card)
        body.setContentsMargins(10, 5, 10, 5)
        body.setSpacing(3)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        title = make_label(raw_name, self._ui_color("primary"), 9, True, mono=True)
        title.setWordWrap(True)
        header.addWidget(title, 1)
        status = make_label("Accepted fuzzy match", self.theme.hex("text_secondary"), 7, True)
        header.addWidget(status, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(header)

        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(5)
        meta.addWidget(make_label("Match source", self.theme.hex("text_muted"), 7, True))
        meta.addWidget(make_label(match_source, self.theme.hex("text_secondary"), 7, True, mono=True))
        meta.addStretch(1)
        body.addLayout(meta)

        body.addStretch(1)

        if resolved_vendor_id > 0:
            remove_button = self._text_action_button("Unmerge")
            remove_button.setFixedHeight(18)
            remove_button.setMaximumWidth(88)
            remove_button.clicked.connect(lambda _, raw=raw_name, vendor_id=resolved_vendor_id: self._remove_merge_member(vendor_id, raw))
            remove_button.setEnabled(not self.vendor_action_busy)
            body.addWidget(remove_button, 0, Qt.AlignmentFlag.AlignRight)

        return card

    def _build_fuzzy_review_block(self, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> QWidget | None:
        """Render fuzzy merge suggestions inside the vendor group."""

        suggestions = self._merge_suggestions(detail, vendor_record)
        resolved_vendor_id = self._vendor_record_id(vendor_record)
        card = CardFrame(bg=self.theme.hex("surface_strong"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)
        header_row.addWidget(make_label("Fuzzy vendor matches", self._ui_color("warning"), 10, True))
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"QFrame {{ background-color: {self.theme.hex('border')}; border: none; }}")
        header_row.addWidget(line, 1)
        header_row.addWidget(make_label(f"{len(suggestions)} candidate name(s)", self.theme.hex("text_secondary"), 9, True))
        layout.addLayout(header_row)

        if not suggestions:
            layout.addWidget(self._empty_card("No vendor-name merge candidates", "Fuzzy vendor-name review is currently clear."))
            return card

        for suggestion in suggestions[:10]:
            row_card = CardFrame(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow="#00000000", radius=0)
            row = QHBoxLayout(row_card)
            row.setContentsMargins(12, 10, 12, 10)
            row.setSpacing(14)
            row.addWidget(self._vendor_status_column("SOURCE_IDENTITY", str(suggestion.get("rawName", "")) or "UNKNOWN", mono=True), 3)
            row.addWidget(self._vendor_status_column("NORMALIZED_MATCH", self._suggestion_match_token(suggestion), value_color=self.theme.hex("blue"), mono=True), 3)
            row.addWidget(self._vendor_status_column("OCCURRENCES", f"{int(suggestion.get('count', 0) or 0)} ROW(S)", value_color=self.theme.hex("emerald"), mono=True), 2)
            row.addWidget(self._vendor_status_column("VOLUME_VALUE", self._format_inr(float(suggestion.get("amount", 0.0) or 0.0)), mono=True), 2)

            actions = QHBoxLayout()
            actions.setContentsMargins(0, 0, 0, 0)
            actions.setSpacing(8)
            if resolved_vendor_id > 0:
                merge_button = self._action_button("MERGE", role="warning", filled=True)
                merge_button.clicked.connect(lambda _, raw=suggestion["rawName"], vendor_id=resolved_vendor_id: self._accept_merge_suggestion(vendor_id, raw))
            else:
                merge_button = self._action_button("CREATE + MERGE", role="warning", filled=True)
                merge_button.clicked.connect(lambda _, raw=suggestion["rawName"], current_detail=dict(detail): self._accept_merge_suggestion_for_detail(current_detail, raw))
            merge_button.setToolTip("Attach this raw vendor name to the active canonical vendor. If no vendor record exists yet, create one first.")
            dismiss_button = self._action_button("DISMISS")
            dismiss_button.clicked.connect(lambda _, raw=suggestion["rawName"], vendor_key=str(detail.get("vendorKey", detail.get("vendor", ""))): self._dismiss_merge_suggestion(vendor_key, raw))
            dismiss_button.setToolTip("Hide this fuzzy match suggestion from the current vendor review list.")
            merge_button.setEnabled(not self.vendor_action_busy)
            dismiss_button.setEnabled(not self.vendor_action_busy)
            actions.addWidget(merge_button)
            actions.addWidget(dismiss_button)
            row.addLayout(actions, 2)
            layout.addWidget(row_card)
        return card

    def _render_vendor_panel(self) -> None:
        if self.vendor_panel_container is None:
            return
        self._mount_panel_layout(self.vendor_panel_container, self._build_vendor_panel())
        self.vendor_panel_container.update()
        self.vendor_panel_container.repaint()

    def _render_filters_panel(self) -> None:
        if self.section_card is None:
            return
        self.filters_container = self._build_filters()
        self.section_card.set_header_aux_widget(self.filters_container, breakpoint=1000)
        self.filters_container.update()

    def _render_summary_panel(self) -> None:
        if self.summary_container is None:
            return
        self._mount_panel_layout(self.summary_container, self._build_summary())
        self.summary_container.update()

    def _render_charts_panel(self) -> None:
        if self.charts_container is None:
            return
        self._mount_panel_layout(self.charts_container, self._build_charts())
        self.charts_container.update()

    def _render_insights_panel(self) -> None:
        if self.insights_container is None:
            return
        self._mount_panel_layout(self.insights_container, self._build_insights())
        self.insights_container.update()

    def _render_ledger_panel(self) -> None:
        if self.ledger_panel_container is None:
            return
        self._mount_panel_layout(self.ledger_panel_container, self._build_ledger())
        self.ledger_panel_container.update()
        self.ledger_panel_container.repaint()

    def _render_month_scoped_sections(self) -> None:
        self._render_filters_panel()
        self._render_summary_panel()
        self._render_charts_panel()
        self._render_insights_panel()
        self._render_ledger_panel()
        self._render_vendor_panel()

    def _mount_panel_layout(self, container: QWidget, panel_layout) -> None:
        host_layout = container.layout()
        if host_layout is None:
            host_layout = QVBoxLayout(container)
            host_layout.setContentsMargins(0, 0, 0, 0)
            host_layout.setSpacing(0)
        else:
            self.clear_layout(host_layout)
        child = QWidget(container)
        child.setLayout(panel_layout)
        host_layout.addWidget(child)

    def _vendor_crud_card(self, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> QWidget:
        card = CardFrame(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title = "Vendor Manager"
        if self.vendor_editor_mode == "new":
            title = "Create Vendor"
        elif self.vendor_editor_mode == "edit":
            title = "Edit Vendor"
        layout.addWidget(make_label(title, self._ui_color("primary"), 10, True))

        if self.vendor_editor_mode in {"new", "edit"}:
            form   = self._current_vendor_form(detail, vendor_record)
            fields = QGridLayout()
            fields.setContentsMargins(0, 0, 0, 0)
            fields.setHorizontalSpacing(8)
            fields.setVerticalSpacing(8)

            self.vendor_name_input = QLineEdit()
            self.vendor_name_input.setText(str(form.get("canonicalVendor", "")))
            self.vendor_name_input.setPlaceholderText("Canonical vendor name")
            self.vendor_name_input.setStyleSheet(self._input_style())
            fields.addWidget(self._labeled_control("Vendor Name", self.vendor_name_input), 0, 0)

            self.vendor_alias_editor_input = QLineEdit()
            self.vendor_alias_editor_input.setText(str(form.get("canonicalAlias", "")))
            self.vendor_alias_editor_input.setPlaceholderText("Canonical alias")
            self.vendor_alias_editor_input.setStyleSheet(self._input_style())
            fields.addWidget(self._labeled_control("Alias", self.vendor_alias_editor_input), 0, 1)

            self.vendor_nickname_input = QLineEdit()
            self.vendor_nickname_input.setText(str(form.get("nickname", "")))
            self.vendor_nickname_input.setPlaceholderText("Nickname / display name")
            self.vendor_nickname_input.setStyleSheet(self._input_style())
            fields.addWidget(self._labeled_control("Nickname", self.vendor_nickname_input), 1, 0)

            self.vendor_notes_input = QLineEdit()
            self.vendor_notes_input.setText(str(form.get("notes", "")))
            self.vendor_notes_input.setPlaceholderText("Notes")
            self.vendor_notes_input.setStyleSheet(self._input_style())
            fields.addWidget(self._labeled_control("Notes", self.vendor_notes_input), 1, 1)
            layout.addLayout(fields)

            categories_wrap = QHBoxLayout()
            categories_wrap.setContentsMargins(0, 0, 0, 0)
            categories_wrap.setSpacing(6)
            for category in form.get("categories", []):
                chip = make_button(f"{category} x", self.theme.hex("border"), self.theme.hex("text_primary"), self.theme.hex("card_alt_bg"))
                chip.clicked.connect(lambda _, cat=category: self._remove_form_category(cat, detail, vendor_record))
                categories_wrap.addWidget(chip)
            categories_wrap.addStretch(1)
            layout.addLayout(categories_wrap)

            category_row = QHBoxLayout()
            category_row.setContentsMargins(0, 0, 0, 0)
            category_row.setSpacing(8)
            self.vendor_category_entry = QLineEdit()
            self.vendor_category_entry.setPlaceholderText("Add category tag")
            self.vendor_category_entry.setStyleSheet(self._input_style())
            category_row.addWidget(self.vendor_category_entry, 1)
            add_category = make_button("Add Category", self.theme.hex("border"), self.theme.hex("text_primary"), self.theme.hex("card_alt_bg"))
            add_category.clicked.connect(lambda: self._add_form_category(detail, vendor_record))
            category_row.addWidget(add_category)
            layout.addLayout(category_row)

            actions = QHBoxLayout()
            actions.setContentsMargins(0, 0, 0, 0)
            actions.setSpacing(8)
            save_button = make_button("Save", self.theme.hex("amber"), self.theme.hex("text_primary"), self.theme.hex("card_alt_bg"))
            save_button.clicked.connect(lambda: self._save_vendor_crud(detail, vendor_record))
            cancel_button = make_button("Cancel", self.theme.hex("border"), self.theme.hex("text_primary"), self.theme.hex("card_alt_bg"))
            cancel_button.clicked.connect(self._cancel_vendor_edit)
            actions.addWidget(save_button)
            actions.addWidget(cancel_button)
            actions.addStretch(1)
            layout.addLayout(actions)
        else:
            _ = self._vendor_record_id(vendor_record)

        self.vendor_message_label = make_label(
            self.vendor_feedback_text or "Manage the selected vendor, its alias, categories, and fuzzy merges here.",
            self._ui_color("secondary"),
            8,
            False,
        )
        self.vendor_message_label.setWordWrap(True)
        layout.addWidget(self.vendor_message_label)
        return card

    def _metric_card(
        self,
        title: str,
        value: str,
        note: str,
        glow: str,
        on_click=None,
        *,
        accented: bool = False,
        compact: bool = False,
        fixed_height: int | None = None,
    ) -> QWidget:
        card_cls = ClickableCardFrame if callable(on_click) else CardFrame
        accent = glow if accented else "#00000000"
        card = (
            card_cls(
                bg=self._ui_color("panel"),
                border=self._ui_color("divider"),
                glow=accent,
                radius=0,
                on_click=on_click,
            )
            if card_cls is ClickableCardFrame
            else card_cls(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow=accent, radius=0)
        )
        if fixed_height is not None:
            card.setFixedHeight(int(fixed_height))
        box = QVBoxLayout(card)
        box.setContentsMargins(9 if compact else 12, 6 if compact else 10, 9 if compact else 12, 6 if compact else 10)
        box.setSpacing(2 if compact else 4)
        box.addWidget(make_label(title.upper(), self.theme.hex("text_secondary"), 7 if compact else 8, True))
        value_label = make_label(value, self._ui_color("primary"), 12 if compact else 15, True, mono=True)
        value_label.setWordWrap(True)
        box.addWidget(value_label)
        note_label = make_label(note, self._ui_color("muted"), 7 if compact else 8, False)
        note_label.setWordWrap(True)
        box.addWidget(note_label)
        box.addStretch(1)
        return card

    def _chart_card(
        self,
        title: str,
        series: list[dict[str, Any]],
        amount_color: str | None = None,
        on_bar_clicked=None,
        active_value=None,
        click_hint: str = "",
        *,
        compact: bool = False,
        chart_height: int | None = None,
    ) -> QWidget:
        card = CardFrame(
            bg=self._ui_color("panel"),
            border=self._ui_color("divider"),
            glow="#00000000",
            radius=0,
        )
        box = QVBoxLayout(card)
        box.setContentsMargins(10 if compact else 12, 8 if compact else 10, 10 if compact else 12, 8 if compact else 10)
        box.setSpacing(6 if compact else 8)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(make_label(title, self._ui_color("secondary"), 8, True))
        header.addStretch(1)
        box.addLayout(header)
        if click_hint:
            box.addWidget(make_label(click_hint, self._ui_color("secondary"), 7 if compact else 8, False))
        chart = MiniBarChart(self.theme, "", series, amount_color, on_bar_clicked=on_bar_clicked, active_value=active_value)
        if chart_height is not None:
            chart.setFixedHeight(int(chart_height))
        else:
            chart.setMinimumHeight(250)
        box.addWidget(chart)
        return card

    def _compact_note_text(self, text: str, *, limit: int = 34) -> str:
        """Trim one note so compact metric cards stay uniform."""

        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned:
            return "-"
        if len(cleaned) <= limit:
            return cleaned
        return f"{cleaned[: max(1, limit - 3)].rstrip()}..."

    def _compact_list_note(self, values: list[str], *, fallback: str, limit: int = 2) -> str:
        """Summarize one string list for compact metric-card notes."""

        cleaned = [str(item).strip() for item in values if str(item).strip()]
        if not cleaned:
            return fallback
        head = cleaned[:limit]
        suffix = f" +{len(cleaned) - limit}" if len(cleaned) > limit else ""
        return f"{', '.join(head)}{suffix}"

    def _top_vendors_card(self, vendors: list[dict[str, Any]]) -> QWidget:
        card = CardFrame(
            bg=self._ui_color("panel"),
            border=self._ui_color("divider"),
            glow="#00000000",
            radius=0,
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        month_label = datetime(self.selected_year, max(1, self.selected_month), 1).strftime("%b %Y") if self.selected_month > 0 else str(self.selected_year)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 7, 12, 7)
        header_layout.setSpacing(6)
        header_layout.addWidget(make_label("Top Vendors", self._ui_color("secondary"), 8, True))
        header_layout.addStretch(1)
        header_layout.addWidget(make_label(month_label, self._ui_color("secondary"), 8, False))
        layout.addWidget(header)

        columns = QWidget()
        columns_layout = QHBoxLayout(columns)
        columns_layout.setContentsMargins(10, 5, 10, 5)
        columns_layout.setSpacing(10)
        columns_layout.addWidget(make_label("Vendor", self._ui_color("secondary"), 7, True), 4)
        columns_layout.addWidget(make_label("Share", self._ui_color("secondary"), 7, True), 3)
        amount_header = make_label("Amount", self._ui_color("secondary"), 7, True, mono=True)
        amount_header.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        columns_layout.addWidget(amount_header, 2)
        rows_header = make_label("Rows", self._ui_color("secondary"), 7, True, mono=True)
        rows_header.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        columns_layout.addWidget(rows_header, 1)
        layout.addWidget(columns)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"QFrame {{ background: {self._ui_color('divider')}; border: none; }}")
        layout.addWidget(divider)

        if not vendors:
            empty = QWidget()
            empty_layout = QHBoxLayout(empty)
            empty_layout.setContentsMargins(12, 12, 12, 12)
            empty_layout.setSpacing(6)
            empty_layout.addWidget(make_label("No vendor spend for this month.", self.theme.hex("text_muted"), 9, False))
            empty_layout.addStretch(1)
            layout.addWidget(empty)
            return card

        max_amount = max(float(item.get("amount", 0.0) or 0.0) for item in vendors) or 1.0
        for item in vendors:
            row_host = QWidget()
            row_host.setFixedHeight(34)
            row_layout = QHBoxLayout(row_host)
            row_layout.setContentsMargins(10, 4, 10, 4)
            row_layout.setSpacing(10)

            vendor_name = str(item.get("vendor", "Unknown"))
            vendor_key = str(item.get("vendorKey", vendor_name))
            row_layout.addWidget(
                ClickableLabel(
                    vendor_name,
                    self._ui_color("primary"),
                    self._ui_color("interactive_hover"),
                    lambda vendor=vendor_name, key=vendor_key: self._set_active_vendor(vendor, key),
                    bold=True,
                    size=9,
                ),
                4,
            )

            row_layout.addWidget(self._relative_spend_bar(float(item.get("amount", 0.0) or 0.0), max_amount), 3)

            amount_label = make_label(
                self._format_inr(float(item.get("amount", 0.0) or 0.0)),
                self._ui_color("primary"),
                10,
                True,
                mono=True,
            )
            amount_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(amount_label, 2)

            count_label = make_label(f"{int(item.get('count', 0) or 0)}x", self._ui_color("interactive"), 8, True, mono=True)
            count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(count_label, 1)
            layout.addWidget(row_host)

            row_divider = QFrame()
            row_divider.setFixedHeight(1)
            row_divider.setStyleSheet(f"QFrame {{ background: {self._ui_color('divider')}; border: none; }}")
            layout.addWidget(row_divider)
        return card

    def _month_scoped_kpi_card(self, scoped: dict[str, Any]) -> QWidget:
        """Render largest/median debit metrics in a compact KPI block."""

        card = CardFrame(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addWidget(make_label("Month snapshot", self.theme.hex("text_secondary"), 9, True))

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        grid.addWidget(make_label("Largest debit", self.theme.hex("text_muted"), 8, True), 0, 0)
        grid.addWidget(make_label("Median debit", self.theme.hex("text_muted"), 8, True), 0, 1)
        grid.addWidget(make_label(self._format_inr(scoped['largest_amount']), self._ui_color("primary"), 12, True, mono=True), 1, 0)
        grid.addWidget(make_label(self._format_inr(scoped['median_amount']), self.theme.hex("emerald"), 12, True, mono=True), 1, 1)
        grid.addWidget(make_label(scoped["largest_vendor"], self.theme.hex("amber"), 9, True, mono=True), 2, 0)
        layout.addLayout(grid)
        return card

    def _peak_metrics_card(self, scoped: dict[str, Any]) -> QWidget:
        """Render peak-day and peak-week metrics in one indigo block."""

        card = CardFrame(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        grid.addWidget(make_label(f"Peak day ({scoped['peak_day_label']})", self.theme.hex("text_muted"), 8, True), 0, 0)
        grid.addWidget(make_label(f"Peak week ({scoped['peak_week_label']})", self.theme.hex("text_muted"), 8, True), 0, 1)
        grid.addWidget(make_label(self._format_inr(scoped['peak_day_amount']), self._ui_color("primary"), 12, True, mono=True), 1, 0)
        grid.addWidget(make_label(self._format_inr(scoped['peak_week_amount']), self._ui_color("primary"), 12, True, mono=True), 1, 1)
        layout.addLayout(grid)
        return card

    def _recurrence_metrics_card(self, scoped: dict[str, Any]) -> QWidget:
        """Render recurrence and monthly-burden KPIs in one emerald block."""

        card = CardFrame(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addWidget(make_label("Recurring spend", self.theme.hex("text_muted"), 9, True))

        recurring_line = self._format_inr(scoped['recurring_amount'])
        layout.addWidget(make_label(recurring_line, self._ui_color("primary"), 12, True, mono=True))
        layout.addWidget(make_label(scoped["recurring_label"], self.theme.hex("emerald"), 10, True, mono=True))

        split = QFrame()
        split.setFixedHeight(1)
        split.setStyleSheet(f"QFrame {{ background: {self._ui_color('divider')}; border: none; }}")
        layout.addWidget(split)

        burden_row = QHBoxLayout()
        burden_row.setContentsMargins(0, 0, 0, 0)
        burden_row.setSpacing(8)
        burden_row.addWidget(make_label("Monthly burden", self.theme.hex("text_muted"), 9, True), 2)
        burden_row.addWidget(make_label(self._format_inr(scoped['monthly_burden']), self._ui_color("primary"), 11, True, mono=True), 2)
        burden_row.addWidget(make_label(f"Across {scoped['active_recurring_count']} vendors", self.theme.hex("text_secondary"), 9, False), 2)
        layout.addLayout(burden_row)
        return card

    def _relative_spend_bar(self, amount: float, max_amount: float) -> QWidget:
        """Render a relative spend bar used by top-vendor rows."""

        host = QWidget()
        host_layout = QHBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)

        track = QFrame()
        track.setFixedHeight(8)
        track.setStyleSheet(
            f"QFrame {{ background: {self._ui_color('panel_alt')}; border: 1px solid {self._ui_color('divider')}; border-radius: 0px; }}"
        )
        track_layout = QHBoxLayout(track)
        track_layout.setContentsMargins(0, 0, 0, 0)
        track_layout.setSpacing(0)

        fill = QFrame()
        fill.setFixedHeight(6)
        fill.setStyleSheet(
            f"QFrame {{ background: {self._ui_color('interactive')}; border: none; border-radius: 0px; }}"
        )
        ratio = 0 if max_amount <= 0 else max(0.02, min(1.0, amount / max_amount))
        fill_weight = int(ratio * 1000)
        track_layout.addWidget(fill, fill_weight)
        track_layout.addStretch(max(1, 1000 - fill_weight))

        host_layout.addWidget(track)
        return host

    def _calendar_ledger(self, groups: list[dict[str, Any]]) -> QWidget:
        card = CardFrame(bg=self.theme.hex("surface_strong"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        weekday_header = QGridLayout()
        weekday_header.setContentsMargins(0, 0, 0, 0)
        weekday_header.setHorizontalSpacing(6)
        weekday_header.setVerticalSpacing(4)
        for index, label in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
            weekday_header.addWidget(make_label(label, self._ui_color("secondary"), 7, True), 0, index)
        layout.addLayout(weekday_header)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)

        group_lookup = {str(item.get("groupKey", "")).strip(): item for item in groups}
        year         = self.selected_year
        month        = self.selected_month
        first_weekday, days_in_month = calendar.monthrange(year, month)
        offset       = first_weekday  # monthrange uses Monday=0
        total_slots  = ((offset + days_in_month + 6) // 7) * 7

        week_row = 0
        for week_start in range(0, total_slots, 7):
            for day_offset in range(7):
                slot = week_start + day_offset
                day_number = slot - offset + 1
                if 1 <= day_number <= days_in_month:
                    group_key = f"{year:04d}-{month:02d}-{day_number:02d}"
                    item      = group_lookup.get(group_key)
                    widget    = self._calendar_day_cell(day_number, group_key, item)
                else:
                    widget = self._calendar_empty_cell()
                grid.addWidget(widget, week_row, day_offset)
            week_row += 1
        layout.addLayout(grid)
        return card

    def _calendar_day_cell(self, day_number: int, group_key: str, group: dict[str, Any] | None) -> QWidget:
        clickable = group is not None
        card_cls  = ClickableCardFrame if clickable else CardFrame
        if clickable:
            card = card_cls(
                bg=self._ui_color("active_surface") if self.selected_calendar_day == group_key else self.theme.hex("surface_strong"),
                border=self._ui_color("focus") if self.selected_calendar_day == group_key else self._ui_color("divider"),
                glow=self._ui_color("interactive") if self.selected_calendar_day == group_key else "#00000000",
                radius=0,
                on_click=lambda key=group_key, day=day_number: self._open_calendar_day_popup(key, day),
            )
        else:
            card = card_cls(bg=self.theme.hex("surface_strong"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        card.setFixedHeight(44)
        outer = QHBoxLayout(card)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(6)

        day_chip = ClickableLabel(str(day_number), self._ui_color("primary"), self._ui_color("interactive_hover"), lambda key=group_key, day=day_number: self._open_calendar_day_popup(key, day), bold=True, size=8) if clickable else make_label(str(day_number), self._ui_color("primary"), 8, True)
        outer.addWidget(day_chip, 0, Qt.AlignmentFlag.AlignVCenter)

        row_count = int(group.get("rowCount", 0) or 0) if group else 0
        if row_count:
            outer.addWidget(make_label(f"{row_count}x", self._ui_color("secondary"), 7, False), 0, Qt.AlignmentFlag.AlignVCenter)

        debit_total = float(group.get("debitTotal", 0.0) or 0.0) if group else 0.0
        outer.addStretch(1)
        total_label = make_label(
            self._format_inr(debit_total),
            self._ui_color("primary") if debit_total else self._ui_color("muted"),
            7,
            True,
        )
        outer.addWidget(total_label, 0, Qt.AlignmentFlag.AlignVCenter)
        return card

    def _calendar_empty_cell(self) -> QWidget:
        card = CardFrame(bg=self.theme.hex("surface_strong"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        card.setFixedHeight(44)
        return card

    def _calendar_day_preview(self, group_key: str) -> list[str]:
        rows = self._month_day_rows(group_key)
        if not rows:
            return []
        previews = []
        for row in rows[:2]:
            vendor_name, _ = self._vendor_identity(row)
            previews.append(f"{row.get('timeLabel', '')}  {vendor_name}  {self._format_inr(self._amount(row))}")
        return previews

    def _selected_calendar_group(self, groups: list[dict[str, Any]]) -> dict[str, Any] | None:
        group_lookup = {str(item.get("groupKey", "")).strip(): item for item in groups}
        if self.selected_calendar_day and self.selected_calendar_day in group_lookup:
            return group_lookup[self.selected_calendar_day]
        if groups:
            return groups[0]
        return None

    def _calendar_day_detail(self, group: dict[str, Any]) -> QWidget:
        group_key = str(group.get("groupKey", "")).strip()
        rows      = self._sorted_rows_desc(self._ledger_group_rows(group_key))
        card = CardFrame(bg=self._ui_color("panel"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout    = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(make_label(f"{group.get('groupLabel', group_key)}", self._ui_color("primary"), 10, True))
        header.addWidget(PillLabel(f"{len(rows)} row(s)", self.theme.hex("text_secondary"), self._ui_color("divider"), self._ui_color("panel_alt")))
        header.addWidget(PillLabel(self._format_inr(sum(self._amount(row) for row in rows if self._direction(row) == "debit")), self._ui_color("interactive"), self._ui_color("divider"), self._ui_color("panel_alt")))
        header.addStretch(1)
        layout.addLayout(header)

        if not rows:
            layout.addWidget(self._empty_card("No transactions", "No rows matched for this day under the current search/filter."))
            return card

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        columns = self._adaptive_columns(base_width=240, minimum=3, maximum=6)
        for index, row in enumerate(rows):
            grid.addWidget(self._ledger_tile(row), index // columns, index % columns)
        layout.addLayout(grid)
        return card

    def _ledger_tile(self, row: dict[str, Any]) -> QWidget:
        card = CardFrame(bg=self.theme.hex("surface_strong"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        top.addWidget(make_label(str(row.get("timeLabel", "")), self._ui_color("secondary"), 8, False))
        top.addStretch(1)
        top.addWidget(make_label("Cr" if self._direction(row) == "credit" else "Dr", self._amount_tone(self._direction(row)), 8, True))
        layout.addLayout(top)

        vendor_name, vendor_key = self._vendor_identity(row)
        layout.addWidget(ClickableLabel(vendor_name, self._ui_color("primary"), self._ui_color("interactive_hover"), lambda v=vendor_name, k=vendor_key: self._set_active_vendor(v, k), bold=True, size=9))
        layout.addWidget(make_label(self._format_inr(self._amount(row)), self._ui_color("primary"), 10, True))
        meta = make_label(f"{row.get('accountSuffix', '')} | {row.get('reference', '-')}", self._ui_color("muted"), 8, False)
        meta.setWordWrap(True)
        layout.addWidget(meta)
        return card

    def _ledger_group(self, group: dict[str, Any]) -> QWidget:
        group_key = str(group.get("groupKey", "")).strip()
        expanded  = group_key in self.expanded_groups
        card = CardFrame(bg=self.theme.hex("surface_soft"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout    = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        toggle = self._action_button("-" if expanded else "+")
        toggle.setFixedWidth(34)
        body        = QWidget()
        body_layout = QGridLayout(body)
        body_layout.setContentsMargins(0, 4, 0, 0)
        body_layout.setHorizontalSpacing(8)
        body_layout.setVerticalSpacing(8)
        body.hide()
        toggle.clicked.connect(lambda _, key=group_key, widget=body, grid=body_layout, button=toggle: self._toggle_group(key, widget, grid, button))
        header.addWidget(toggle)
        header.addWidget(make_label(str(group.get("groupLabel", group_key)), self._ui_color("primary"), 9, True))
        header.addWidget(make_label(f"{int(group.get('rowCount', 0) or 0)} row(s)", self._ui_color("secondary"), 8, False))
        header.addStretch(1)
        header.addWidget(make_label(self._format_inr(float(group.get("debitTotal", 0.0) or 0.0)), self._ui_color("secondary"), 9, True))
        layout.addLayout(header)
        layout.addWidget(body)
        if expanded:
            self._expand_group(group_key, body, body_layout, toggle)
        return card

    def _vendor_transactions_columns(self, rows: list[dict[str, Any]]) -> QWidget:
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(0)

        columns = self._adaptive_columns(base_width=260, minimum=3, maximum=6)
        column_layouts: list[QVBoxLayout] = []
        column_hosts  : list[QWidget]     = []
        for column in range(columns):
            column_host = QWidget()
            column_box  = QVBoxLayout(column_host)
            column_box.setContentsMargins(0, 0, 0, 0)
            column_box.setSpacing(6)
            grid.addWidget(column_host, 0, column)
            column_hosts.append(column_host)
            column_layouts.append(column_box)

        for index, row in enumerate(rows):
            column_layouts[index % columns].addWidget(self._vendor_transaction_row(row))

        for column_box in column_layouts:
            column_box.addStretch(1)
        return host

    def _vendor_transaction_row(self, row: dict[str, Any]) -> QWidget:
        row_widget = QFrame()
        row_widget.setStyleSheet(
            "QFrame {"
            f"background: {_alpha_hex(self._ui_color('panel_alt'), 120)};"
            "border: none;"
            f"border-bottom: 1px solid {self.theme.hex('border')};"
            "border-radius: 0px; }"
        )
        layout = QVBoxLayout(row_widget)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        top.addWidget(make_label(str(row.get("timeLabel", "")), self._ui_color("secondary"), 8, False))
        top.addStretch(1)
        top.addWidget(make_label("Cr" if self._direction(row) == "credit" else "Dr", self._amount_tone(self._direction(row)), 8, True))
        top.addWidget(make_label(self._format_inr(self._amount(row)), self._ui_color("primary"), 9, True))
        layout.addLayout(top)

        meta = make_label(f"{row.get('dayOnlyLabel', '')} | {row.get('accountSuffix', '')} | Ref {row.get('reference', '-')}", self._ui_color("secondary"), 8, False)
        meta.setWordWrap(True)
        layout.addWidget(meta)
        canonical_vendor, _ = self._vendor_identity(row)
        canonical = make_label(f"Vendor: {canonical_vendor}", self._ui_color("secondary"), 8, False)
        canonical.setWordWrap(True)
        layout.addWidget(canonical)
        raw_source = str(row.get("rawCounterparty", "")).strip()
        if raw_source:
            source = make_label(f"Source alias: {raw_source}", self._ui_color("interactive"), 8, False)
            source.setWordWrap(True)
            layout.addWidget(source)
        return row_widget

    def _popup_transaction_card(self, row: dict[str, Any]) -> QWidget:
        card = CardFrame(bg=self.theme.hex("surface_strong"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        layout.addWidget(make_label(self._popup_stamp_label(row), self._ui_color("secondary"), 8, False))
        vendor_name, vendor_key = self._vendor_identity(row)
        vendor_label = ClickableLabel(vendor_name, self._ui_color("primary"), self._ui_color("interactive_hover"), lambda v=vendor_name, k=vendor_key: self._open_vendor_from_popup(v, k), bold=True, size=9)
        vendor_label.setWordWrap(True)
        layout.addWidget(vendor_label, 1)
        layout.addWidget(make_label("Cr" if self._direction(row) == "credit" else "Dr", self._amount_tone(self._direction(row)), 8, True))
        layout.addWidget(make_label(self._format_inr(self._amount(row)), self._ui_color("primary"), 10, True))
        raw_source = str(row.get("rawCounterparty", "")).strip()
        source_meta = f" | Src {raw_source}" if raw_source else ""
        meta = make_label(f"{row.get('accountSuffix', '')} | Ref {row.get('reference', '-')}{source_meta}", self._ui_color("muted"), 8, False)
        meta.setWordWrap(True)
        layout.addWidget(meta)
        return card

    def _show_selected_spend_popup(self) -> None:
        month_rows   = self._selected_rows()
        debit_rows   = self._sorted_rows_desc([row for row in month_rows if self._direction(row) == "debit"])
        month_title  = datetime(self.selected_year, max(self.selected_month, 1), 1).strftime("%b %Y") if self.selected_month > 0 else str(self.selected_year)
        total_amount = sum(self._amount(row) for row in debit_rows)
        self._show_transaction_popup(f"Selected Spend | {month_title}", debit_rows, total_amount)

    def _show_daily_bar_popup(self, item: dict[str, Any]) -> None:
        day_number = int(item.get("day", 0) or 0)
        if day_number <= 0 or self.selected_month <= 0:
            return
        rows = self._month_day_rows(f"{self.selected_year:04d}-{self.selected_month:02d}-{day_number:02d}", direction="debit")
        title = datetime(self.selected_year, self.selected_month, day_number).strftime("Daily Spend | %d %b %Y")
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_weekly_bar_popup(self, item: dict[str, Any]) -> None:
        week_number = int(item.get("week", 0) or 0)
        if week_number <= 0 or self.selected_month <= 0:
            return
        rows = [
            row
            for row in self._selected_rows()
            if self._direction(row) == "debit"
            and ((self._timestamp(row).day - 1) // 7 + 1 if self._timestamp(row) else 0) == week_number
        ]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"Weekly Spend | W{week_number} | {datetime(self.selected_year, self.selected_month, 1).strftime('%b %Y')}"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _select_month_from_year_chart(self, item: dict[str, Any]) -> None:
        month_value = int(item.get("month", 0) or 0)
        if month_value <= 0 or month_value == self.selected_month:
            return
        self._apply_month_selection(month_value, partial=True)

    def _show_alias_monthly_bar_popup(self, item: dict[str, Any]) -> None:
        """Open all alias-cluster transactions for the clicked month."""

        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else {}
        if not isinstance(detail, dict) or not detail:
            return
        year = int(item.get("year", 0) or 0)
        month = int(item.get("month", 0) or 0)
        if year <= 0 or month <= 0:
            return
        rows = [
            row
            for row in detail.get("transactions", [])
            if int(row.get("year", 0) or 0) == year and int(row.get("month", 0) or 0) == month
        ]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"{detail.get('canonicalAlias', detail.get('vendor', 'Alias'))} | {datetime(year, month, 1).strftime('%b %Y')}"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_alias_yearly_bar_popup(self, item: dict[str, Any]) -> None:
        """Open all alias-cluster transactions for the clicked year."""

        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else {}
        if not isinstance(detail, dict) or not detail:
            return
        year = int(item.get("year", 0) or 0)
        if year <= 0:
            return
        rows = [row for row in detail.get("transactions", []) if int(row.get("year", 0) or 0) == year]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"{detail.get('canonicalAlias', detail.get('vendor', 'Alias'))} | {year}"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_vendor_scope_monthly_popup(self, item: dict[str, Any]) -> None:
        """Open vendor-record scoped rows for one clicked month."""

        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else {}
        if not isinstance(detail, dict) or not detail:
            return
        vendor_name = str(detail.get("vendorGroup", {}).get("canonicalVendor", detail.get("canonicalVendor", ""))).strip()
        year = int(item.get("year", 0) or 0)
        month = int(item.get("month", 0) or 0)
        if not vendor_name or year <= 0 or month <= 0:
            return
        source_rows = list(detail.get("vendorTransactions", detail.get("transactions", [])))
        rows = [
            row
            for row in source_rows
            if int(row.get("year", 0) or 0) == year
            and int(row.get("month", 0) or 0) == month
        ]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"{vendor_name} | {datetime(year, month, 1).strftime('%b %Y')}"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_vendor_scope_yearly_popup(self, item: dict[str, Any]) -> None:
        """Open vendor-record scoped rows for one clicked year."""

        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else {}
        if not isinstance(detail, dict) or not detail:
            return
        vendor_name = str(detail.get("vendorGroup", {}).get("canonicalVendor", detail.get("canonicalVendor", ""))).strip()
        year = int(item.get("year", 0) or 0)
        if not vendor_name or year <= 0:
            return
        source_rows = list(detail.get("vendorTransactions", detail.get("transactions", [])))
        rows = [
            row
            for row in source_rows
            if int(row.get("year", 0) or 0) == year
        ]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"{vendor_name} | {year}"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_alias_time_bar_popup(self, item: dict[str, Any]) -> None:
        """Open one selected month/hour slice for the active alias cluster."""

        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else {}
        if not isinstance(detail, dict) or not detail:
            return
        alias_group = detail.get("aliasGroup", {}) if isinstance(detail.get("aliasGroup", {}), dict) else {}
        selected_month = alias_group.get("selectedMonth", {}) if isinstance(alias_group.get("selectedMonth", {}), dict) else {}
        year = int(selected_month.get("year", 0) or 0)
        month = int(selected_month.get("month", 0) or 0)
        hour = int(item.get("bucketKey", -1) if item.get("bucketKey", None) is not None else item.get("hour", -1) or -1)
        if year <= 0 or month <= 0 or hour < 0:
            return
        rows = [
            row
            for row in detail.get("transactions", [])
            if int(row.get("year", 0) or 0) == year
            and int(row.get("month", 0) or 0) == month
            and (self._timestamp(row).hour if self._timestamp(row) is not None else -1) == hour
        ]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"{detail.get('canonicalAlias', detail.get('vendor', 'Alias'))} | {datetime(year, month, 1).strftime('%b %Y')} | {hour:02d}:00"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_category_breakdown_popup(self, item: dict[str, Any]) -> None:
        """Open all global-category transactions for one clicked category."""

        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else {}
        if not isinstance(detail, dict) or not detail:
            return
        category = str(item.get("category", item.get("label", ""))).strip()
        if not category:
            return
        rows = [
            row
            for row in detail.get("categoryTransactions", detail.get("transactions", []))
            if str(row.get("category", "Uncategorized") or "Uncategorized").strip() == category
        ]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"Category | {category}"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_category_month_breakdown_popup(self, item: dict[str, Any]) -> None:
        """Open selected-month global-category transactions for one clicked category."""

        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else {}
        if not isinstance(detail, dict) or not detail:
            return
        category_group = detail.get("categoryGroup", {}) if isinstance(detail.get("categoryGroup", {}), dict) else {}
        selected_month = category_group.get("selectedMonth", {}) if isinstance(category_group.get("selectedMonth", {}), dict) else {}
        year = int(selected_month.get("year", 0) or 0)
        month = int(selected_month.get("month", 0) or 0)
        category = str(item.get("category", item.get("label", ""))).strip()
        if year <= 0 or month <= 0 or not category:
            return
        rows = [
            row
            for row in detail.get("categoryTransactions", detail.get("transactions", []))
            if int(row.get("year", 0) or 0) == year
            and int(row.get("month", 0) or 0) == month
            and str(row.get("category", "Uncategorized") or "Uncategorized").strip() == category
        ]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"Category | {category} | {datetime(year, month, 1).strftime('%b %Y')}"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_category_scope_monthly_popup(self, item: dict[str, Any]) -> None:
        """Open category-scoped rows for one clicked month."""

        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else {}
        if not isinstance(detail, dict) or not detail:
            return
        category = str(detail.get("categoryGroup", {}).get("categoryName", detail.get("category", "Uncategorized"))).strip() or "Uncategorized"
        year = int(item.get("year", 0) or 0)
        month = int(item.get("month", 0) or 0)
        if year <= 0 or month <= 0:
            return
        rows = [
            row
            for row in detail.get("categoryTransactions", detail.get("transactions", []))
            if str(row.get("category", "Uncategorized") or "Uncategorized").strip() == category
            and int(row.get("year", 0) or 0) == year
            and int(row.get("month", 0) or 0) == month
        ]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"Category | {category} | {datetime(year, month, 1).strftime('%b %Y')}"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_category_scope_yearly_popup(self, item: dict[str, Any]) -> None:
        """Open category-scoped rows for one clicked year."""

        detail = self.loaded_vendor_detail if self.vendor_panel_state == "loaded" else {}
        if not isinstance(detail, dict) or not detail:
            return
        category = str(detail.get("categoryGroup", {}).get("categoryName", detail.get("category", "Uncategorized"))).strip() or "Uncategorized"
        year = int(item.get("year", 0) or 0)
        if year <= 0:
            return
        rows = [
            row
            for row in detail.get("categoryTransactions", detail.get("transactions", []))
            if str(row.get("category", "Uncategorized") or "Uncategorized").strip() == category
            and int(row.get("year", 0) or 0) == year
        ]
        rows = self._sorted_rows_desc(rows)
        if not rows:
            return
        title = f"Category | {category} | {year}"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_vendor_transactions_popup(self, vendor: str, rows: list[dict[str, Any]]) -> None:
        """Open a popup that lists every transaction for the active vendor."""

        if not rows:
            return
        title = f"{vendor or 'Vendor'} | All Transactions"
        self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))

    def _show_alias_vendors_popup(self, alias_name: str, vendors: list[dict[str, Any]]) -> None:
        """Open a popup that lists the canonical vendors attached to one alias cluster."""

        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowTitle(f"{alias_name or 'Alias'} Vendors")
        dialog.setMinimumSize(640, 420)
        self._apply_dialog_chrome(dialog)

        root = QVBoxLayout(dialog)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        root.addWidget(self._dialog_title(f"{(alias_name or 'Alias').upper()} | ATTACHED_VENDORS"))
        root.addWidget(self._dialog_note(f"{len(vendors)} vendor row(s)", role="secondary", size=9))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)

        if not vendors:
            content_layout.addWidget(self._empty_card("No attached vendors", "This alias cluster has no vendor entries to inspect."))
        else:
            for vendor_item in vendors:
                vendor_name = str(vendor_item.get("canonicalVendor", "")).strip() or "Unknown"
                tx_count = int(vendor_item.get("transactionCount", 0) or 0)
                total_spend = self._format_inr(float(vendor_item.get("totalDebit", 0.0) or 0.0))
                row_card = CardFrame(bg=self.theme.hex("surface_soft"), border=self._ui_color("divider"), glow="#00000000", radius=0)
                row = QHBoxLayout(row_card)
                row.setContentsMargins(12, 8, 12, 8)
                row.setSpacing(10)
                row.addWidget(make_label(vendor_name, self._ui_color("primary"), 10, True, mono=True), 2)
                row.addWidget(make_label(f"{tx_count} TX", self.theme.hex("text_secondary"), 9, True, mono=True), 1)
                row.addWidget(make_label(total_spend, self.theme.hex("emerald"), 10, True, mono=True), 1)
                open_button = self._action_button("OPEN VENDOR", role="interactive")
                open_button.clicked.connect(
                    lambda _, vendor=vendor_name, vendor_key=str(vendor_item.get("aliasKey", "")): (dialog.accept(), self._set_active_vendor(vendor, vendor_key))
                )
                row.addWidget(open_button)
                content_layout.addWidget(row_card)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        close_button = self._action_button("Close")
        close_button.clicked.connect(dialog.accept)
        footer.addWidget(close_button)
        root.addLayout(footer)
        dialog.exec()

    def _show_transaction_popup(self, title: str, rows: list[dict[str, Any]], total_amount: float) -> None:
        sorted_rows = self._sorted_rows_desc(rows)
        self.transaction_popup.set_transactions(
            title,
            sorted_rows,
            self._format_inr(total_amount),
            self._open_vendor_from_popup,
            self._toggle_ignore_transaction,
            self._load_popup_provenance,
        )

    def _load_popup_provenance(self, transaction_key: str) -> None:
        worker = SourceProvenanceWorker(transaction_key=transaction_key, loader=self.screen_api.list_transaction_sources)
        worker.signals.finished.connect(self._show_popup_provenance)
        worker.signals.failed.connect(lambda _key, message: self.transaction_popup.meta_label.setText(f"Could not load source details: {message}"))
        self.ledger_rows_thread_pool.start(worker)

    def _show_popup_provenance(self, transaction_key: str, rows: list) -> None:
        detail = QDialog(self.transaction_popup)
        detail.setWindowTitle("Source Details")
        layout = QVBoxLayout(detail)
        layout.addWidget(make_label(f"{transaction_key}\n{len(rows)} supporting source record(s)", self._ui_color("primary"), 9, True, mono=True))
        for row in rows:
            layout.addWidget(make_label(f"{row.get('providerId', '')} · {row.get('recordType', '')} · {row.get('matchKind', '')}\n{row.get('sourceUri', '')}", self._ui_color("secondary"), 8, False, mono=True))
        close = self._action_button("Close")
        close.clicked.connect(detail.accept)
        layout.addWidget(close)
        detail.exec()
        self.transaction_popup.exec()

    def _open_vendor_from_popup(self, vendor: str, vendor_key: str) -> None:
        self.transaction_popup.accept()
        self._set_active_vendor(vendor, vendor_key)

    def _open_calendar_day_popup(self, group_key: str, day_number: int) -> None:
        self.selected_calendar_day = group_key.strip()
        rows = self._ledger_group_rows(group_key)
        if rows:
            title = datetime(self.selected_year, self.selected_month, day_number).strftime("Ledger | %d %b %Y")
            self._show_transaction_popup(title, rows, sum(self._amount(row) for row in rows))
            self._render_ledger_panel()
            return
        self._request_ledger_group_rows(group_key, kind="popup", day_number=day_number)
        self._render_ledger_panel()

    def _empty_card(self, title: str, note: str) -> QWidget:
        card = CardFrame(bg=self._ui_color("panel_alt"), border=self._ui_color("divider"), glow="#00000000", radius=0)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        layout.addWidget(make_label(title, self.theme.hex("text_primary"), 10, True))
        label = make_label(note, self.theme.hex("text_muted"), 8, False)
        label.setWordWrap(True)
        layout.addWidget(label)
        return card

    def _labeled_control(self, label: str, control: QWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(make_label(label.upper(), self.theme.hex("text_secondary"), 8, True))
        layout.addWidget(control)
        return box

    def _vendor_status_column(
        self,
        label: str,
        value: str,
        *,
        value_color: str | None = None,
        mono: bool = True,
    ) -> QWidget:
        """Render one command-style key/value column for vendor status blocks."""

        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(make_label(label.upper(), self.theme.hex("text_muted"), 9, True, mono=True))
        value_label = make_label(value, value_color or self._ui_color("primary"), 12, True, mono=mono)
        value_label.setWordWrap(True)
        layout.addWidget(value_label)
        return box

    def _status_divider(self) -> QWidget:
        """Render one vertical divider used in vendor command strips."""

        line = QFrame()
        line.setFixedWidth(1)
        line.setStyleSheet(f"QFrame {{ background-color: {self._ui_color('divider')}; border: none; }}")
        return line

    def _suggestion_match_token(self, suggestion: dict[str, Any]) -> str:
        """Extract the normalized fuzzy token from one suggestion row."""

        reason = str(suggestion.get("reason", "")).strip()
        if ":" in reason:
            return reason.split(":", 1)[1].strip() or "unknown"
        return reason or "unknown"

    def _vendor_record_id(self, vendor_record: dict[str, Any] | None) -> int:
        """Resolve a usable vendor id from known payload key variants."""

        if not isinstance(vendor_record, dict):
            return 0
        for key in ("vendorId", "id", "vendor_id"):
            try:
                value = int(vendor_record.get(key, 0) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        return 0

    def _visible_rows(self, *, direction: str | None = None) -> list[dict[str, Any]]:
        rows = list(self.widget_data.get("transactions", []))
        if not self.show_ignored:
            rows = [row for row in rows if not bool(row.get("ignored"))]
        if direction:
            rows = [row for row in rows if self._direction(row) == direction]
        return rows

    def _selected_rows(self) -> list[dict[str, Any]]:
        return [
            row
            for row in self._visible_rows()
            if self._year(row) == self.selected_year and (self.selected_month <= 0 or self._month(row) == self.selected_month)
        ]

    def _month_day_rows(self, group_key: str, *, direction: str | None = None) -> list[dict[str, Any]]:
        key = group_key.strip()
        rows = []
        for row in self._selected_rows():
            stamp = self._timestamp(row)
            if stamp is None:
                continue
            row_key = f"{stamp.year:04d}-{stamp.month:02d}-{stamp.day:02d}"
            if row_key == key:
                rows.append(row)
        if direction:
            rows = [row for row in rows if self._direction(row) == direction]
        if self.ledger_search_text:
            rows = self._apply_ledger_search_filter(rows)
        return self._sorted_rows_desc(rows)

    def _apply_ledger_search_filter(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        query = self.ledger_search_text.strip().lower()
        if not query:
            return rows
        return [row for row in rows if query in self._ledger_search_blob(row)]

    def _ledger_groups(self) -> list[dict[str, Any]]:
        cache_key = (self.selected_year, self.selected_month, self.selected_currency, self.show_ignored, self.ledger_search_text.lower())
        groups = self.ledger_groups_cache.get(cache_key)
        if groups is not None:
            return groups
        self._request_ledger_groups(cache_key)
        return []

    def _request_ledger_groups(self, cache_key: tuple[Any, ...] | None = None) -> None:
        cache_key = cache_key or (self.selected_year, self.selected_month, self.selected_currency, self.show_ignored, self.ledger_search_text.lower())
        if self._active_ledger_groups_request_key == cache_key and self.ledger_groups_busy:
            return
        self._ledger_groups_request_id += 1
        request_id = self._ledger_groups_request_id
        self._active_ledger_groups_request_id = request_id
        self._active_ledger_groups_request_key = cache_key
        self.ledger_groups_busy = True
        self.ledger_groups_error = ""
        if self.ledger_panel_container is not None:
            self._render_ledger_panel()
        worker = LedgerGroupsWorker(
            load_groups=self.screen_api.list_ledger_groups,
            request_id=request_id,
            cache_key=cache_key,
            year=self.selected_year,
            month=self.selected_month,
            currency=self.selected_currency,
            include_ignored=self.show_ignored,
            search_text=self.ledger_search_text,
        )
        worker.signals.finished.connect(self._handle_ledger_groups_finished)
        worker.signals.failed.connect(self._handle_ledger_groups_failed)
        self.ledger_groups_thread_pool.start(worker)

    def _handle_ledger_groups_finished(self, request_id: int, cache_key: tuple[Any, ...], groups: list[dict[str, Any]]) -> None:
        if request_id != self._active_ledger_groups_request_id:
            return
        self.ledger_groups_cache[cache_key] = list(groups or [])
        self.ledger_groups_busy = False
        self.ledger_groups_error = ""
        self._render_ledger_panel()

    def _handle_ledger_groups_failed(self, request_id: int, _cache_key: tuple[Any, ...], message: str) -> None:
        if request_id != self._active_ledger_groups_request_id:
            return
        self.ledger_groups_busy = False
        self.ledger_groups_error = message
        self._render_ledger_panel()

    def _toggle_group(self, group_key: str, body: QWidget, body_layout: QGridLayout, toggle: QWidget) -> None:
        if group_key in self.expanded_groups:
            self.expanded_groups.remove(group_key)
            body.hide()
            if hasattr(toggle, "setText"):
                toggle.setText("+")
            return
        self.expanded_groups.add(group_key)
        self._expand_group(group_key, body, body_layout, toggle)

    def _expand_group(self, group_key: str, body: QWidget, body_layout: QGridLayout, toggle: QWidget) -> None:
        rows = self._ledger_group_rows(group_key)
        body.show()
        if rows:
            self._bind_group_rows(group_key, body_layout, rows)
        else:
            self._bind_group_loading(body_layout)
            self._request_ledger_group_rows(group_key, kind="group", body=body, body_layout=body_layout, toggle=toggle)
        if hasattr(toggle, "setText"):
            toggle.setText("-")

    def _ledger_group_rows(self, group_key: str) -> list[dict[str, Any]]:
        cache_key = (self.selected_year, self.selected_month, self.selected_currency, self.show_ignored, self.ledger_search_text.lower(), group_key)
        return self.group_rows_cache.get(cache_key, [])

    def _bind_group_loading(self, layout: QGridLayout) -> None:
        self.clear_layout(layout)
        loading = make_label("Loading ledger rows...", self._ui_color("secondary"), 8, False)
        loading.setWordWrap(True)
        layout.addWidget(loading, 0, 0)

    def _request_ledger_group_rows(self, group_key: str, *, kind: str, body: QWidget | None = None, body_layout: QGridLayout | None = None, toggle: QWidget | None = None, day_number: int | None = None) -> None:
        cache_key = (self.selected_year, self.selected_month, self.selected_currency, self.show_ignored, self.ledger_search_text.lower(), group_key)
        if self._active_ledger_rows_request_key == cache_key and self.ledger_rows_busy:
            return
        self._ledger_rows_request_id += 1
        request_id = self._ledger_rows_request_id
        self._active_ledger_rows_request_id = request_id
        self._active_ledger_rows_request_key = cache_key
        self._pending_ledger_request = {
            "kind": kind,
            "group_key": group_key,
            "day_number": day_number,
        }
        self.ledger_rows_busy = True
        self.ledger_rows_job_label = "Loading ledger rows"
        self.ledger_rows_error = ""
        self._render_ledger_panel()
        worker = LedgerRowsWorker(
            load_rows=self.screen_api.list_ledger_group_rows,
            request_id=request_id,
            cache_key=cache_key,
            year=self.selected_year,
            month=self.selected_month,
            currency=self.selected_currency,
            group_key=group_key,
            include_ignored=self.show_ignored,
            search_text=self.ledger_search_text,
        )
        worker.signals.finished.connect(self._handle_ledger_rows_finished)
        worker.signals.failed.connect(self._handle_ledger_rows_failed)
        self.ledger_rows_thread_pool.start(worker)

    def _handle_ledger_rows_finished(self, request_id: int, cache_key: tuple[Any, ...], rows: list[dict[str, Any]]) -> None:
        if request_id != self._active_ledger_rows_request_id:
            return
        self.group_rows_cache[cache_key] = [self._decorate_repository_row(row) for row in rows]
        pending = dict(self._pending_ledger_request or {})
        self.ledger_rows_busy = False
        self.ledger_rows_job_label = ""
        self.ledger_rows_error = ""
        self._pending_ledger_request = None
        if pending.get("kind") == "popup":
            day_number = int(pending.get("day_number") or 0)
            if day_number > 0:
                title = datetime(self.selected_year, self.selected_month, day_number).strftime("Ledger | %d %b %Y")
                loaded_rows = self.group_rows_cache[cache_key]
                self._show_transaction_popup(title, loaded_rows, sum(self._amount(row) for row in loaded_rows))
        self._render_ledger_panel()

    def _handle_ledger_rows_failed(self, request_id: int, _cache_key: tuple[Any, ...], message: str) -> None:
        if request_id != self._active_ledger_rows_request_id:
            return
        self.ledger_rows_busy = False
        self.ledger_rows_job_label = ""
        self.ledger_rows_error = message
        self._pending_ledger_request = None
        self._render_ledger_panel()

    def _bind_group_rows(self, group_key: str, layout: QGridLayout, rows: list[dict[str, Any]]) -> None:
        pool    = self.group_widget_pools.setdefault(group_key, [])
        columns = 3 if self.width() >= 1800 else 2
        for index, row in enumerate(rows):
            if index >= len(pool):
                pool.append(LedgerRowCard(self.theme, self._set_active_vendor, self._toggle_ignore_transaction))
            widget = pool[index]
            vendor_name, vendor_key = self._vendor_identity(row)
            widget.bind_row(
                row,
                vendor=vendor_name,
                vendor_key=vendor_key,
                action_busy=self.ledger_action_busy,
                action_pending=str(row.get("transactionKey", "")).strip() == self.ledger_action_transaction_key,
            )
            layout.addWidget(widget, index // columns, index % columns)
            widget.show()
        for index in range(len(rows), len(pool)):
            pool[index].hide()

    def _vendor_autocomplete_items(self, query: str = "") -> list[dict[str, Any]]:
        """Return vendor and alias autocomplete items without collapsing them into one identity."""

        normalized_query = query.strip().lower()
        # The current snapshot already contains the complete selected-range vendor
        # directory. Keeping autocomplete local avoids a blocking SQL query on every
        # keystroke; vendor records outside this range remain available through New
        # Vendor / the catalog editor.
        cached_items = self.vendor_autocomplete_cache.get(normalized_query, [])
        if cached_items:
            return [dict(item) for item in cached_items]

        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        rows = list(self.widget_data.get("transactions", []))

        vendor_buckets: dict[tuple[str, str], dict[str, Any]] = {}
        alias_buckets: dict[str, dict[str, Any]] = {}

        for row in rows:
            canonical_vendor = str(row.get("canonicalVendor", row.get("vendor", ""))).strip()
            canonical_alias = str(row.get("canonicalAlias", canonical_vendor)).strip() or canonical_vendor
            alias_key = str(row.get("aliasKey", "")).strip() or self._normalize_alias_key(canonical_alias or canonical_vendor)
            category = str(row.get("category", "Uncategorized") or "Uncategorized").strip() or "Uncategorized"
            raw_counterparty = str(row.get("rawCounterparty", "")).strip()
            nickname = str(row.get("resolvedVendor", "")).strip()

            if canonical_vendor:
                bucket = vendor_buckets.setdefault(
                    (canonical_vendor.lower(), alias_key.lower()),
                    {
                        "entryType": "vendor",
                        "displayLabel": f"{canonical_vendor} [VENDOR]",
                        "vendor": canonical_vendor,
                        "vendorKey": alias_key,
                        "canonicalVendor": canonical_vendor,
                        "canonicalAlias": canonical_alias,
                        "aliasValue": "",
                        "category": category,
                        "matchText": " ".join(part for part in [canonical_vendor, canonical_alias, category, raw_counterparty, nickname] if part).lower(),
                    },
                )
                if bucket.get("category", "Uncategorized") == "Uncategorized" and category:
                    bucket["category"] = category

            if alias_key:
                bucket = alias_buckets.setdefault(
                    alias_key.lower(),
                    {
                        "entryType": "alias_cluster",
                        "displayLabel": f"{canonical_alias or canonical_vendor} [ALIAS CLUSTER]",
                        "vendor": canonical_vendor or canonical_alias or "Unknown",
                        "vendorKey": alias_key,
                        "canonicalVendor": canonical_vendor or canonical_alias or "Unknown",
                        "canonicalAlias": canonical_alias or canonical_vendor or "Unknown",
                        "aliasValue": canonical_alias or canonical_vendor,
                        "category": category,
                        "matchText": " ".join(part for part in [canonical_alias, canonical_vendor, category, raw_counterparty, nickname] if part).lower(),
                    },
                )
                if (
                    canonical_vendor
                    and self._normalize_vendor_token(canonical_vendor) == self._normalize_alias_key(canonical_alias or canonical_vendor)
                ):
                    bucket["vendor"] = canonical_vendor
                    bucket["canonicalVendor"] = canonical_vendor
                if bucket.get("category", "Uncategorized") == "Uncategorized" and category:
                    bucket["category"] = category

        for payload in vendor_buckets.values():
            marker = (str(payload.get("entryType", "")), str(payload.get("displayLabel", "")).lower(), str(payload.get("vendorKey", "")).lower())
            if marker in seen:
                continue
            seen.add(marker)
            items.append(payload)

        for payload in alias_buckets.values():
            marker = (str(payload.get("entryType", "")), str(payload.get("displayLabel", "")).lower(), str(payload.get("vendorKey", "")).lower())
            if marker in seen:
                continue
            seen.add(marker)
            items.append(payload)

        for item in self.widget_data.get("vendorAutocomplete", []) or self.widget_data.get("vendorDirectory", []):
            entry_type = str(item.get("entryType", "alias")).strip().lower()
            if entry_type not in {"alias", "alias_cluster", "vendor"}:
                entry_type = "alias"
            vendor_key = str(item.get("vendorKey", "")).strip()
            canonical_vendor = str(item.get("canonicalVendor", item.get("vendor", ""))).strip()
            canonical_alias = str(item.get("canonicalAlias", canonical_vendor)).strip() or canonical_vendor
            label = str(item.get("displayLabel", "")).strip() or (
                f"{canonical_vendor} [VENDOR]" if entry_type == "vendor" else f"{canonical_alias} [ALIAS CLUSTER]" if entry_type == "alias_cluster" else str(item.get("vendor", canonical_vendor))
            )
            payload = {
                "entryType": entry_type,
                "displayLabel": label,
                "vendor": str(item.get("vendor", canonical_vendor)).strip() or canonical_vendor,
                "vendorKey": vendor_key,
                "canonicalVendor": canonical_vendor,
                "canonicalAlias": canonical_alias,
                "aliasValue": str(item.get("aliasValue", "")).strip(),
                "category": str(item.get("category", "Uncategorized")).strip() or "Uncategorized",
                "matchText": str(item.get("matchText", item.get("searchText", ""))).strip().lower()
                or " ".join(
                    part
                    for part in [
                        label,
                        canonical_vendor,
                        canonical_alias,
                        str(item.get("aliasValue", "")).strip(),
                        str(item.get("category", "Uncategorized")).strip(),
                    ]
                    if part
                ).lower(),
            }
            marker = (payload["entryType"], payload["displayLabel"].lower(), payload["vendorKey"].lower())
            if marker in seen:
                continue
            seen.add(marker)
            items.append(payload)

        return items

    def _vendor_match_score(self, item: dict[str, Any], query: str) -> tuple[int, int] | None:
        """Rank one autocomplete item using prefix-first matching with contains fallback."""

        normalized = query.strip().lower()
        if not normalized:
            return (3, 0)
        fields = (
            (0, str(item.get("aliasValue", "")).strip()),
            (1, str(item.get("vendor", "")).strip()),
            (2, str(item.get("canonicalAlias", "")).strip()),
            (3, str(item.get("canonicalVendor", "")).strip()),
            (4, str(item.get("displayLabel", "")).strip()),
        )
        best: tuple[int, int] | None = None
        for field_rank, value in fields:
            lowered = value.lower()
            if not lowered:
                continue
            score: tuple[int, int] | None = None
            if lowered == normalized:
                score = (0, field_rank)
            elif lowered.startswith(normalized):
                score = (1, field_rank)
            elif normalized in lowered:
                score = (2, field_rank)
            if score is not None and (best is None or score < best):
                best = score
        return best

    def _vendor_autocomplete_labels(self, query: str = "") -> list[str]:
        """Return unique display labels for the vendor completer model."""

        items = self._vendor_autocomplete_items(query)
        normalized = query.strip().lower()
        labels: list[str] = []
        seen: set[tuple[str, str]] = set()
        if not normalized:
            for item in items:
                label = str(item.get("displayLabel", "")).strip() or str(item.get("vendor", "")).strip()
                vendor_key = str(item.get("vendorKey", "")).strip().lower()
                marker = (label.lower(), vendor_key)
                if not label or marker in seen:
                    continue
                labels.append(label)
                seen.add(marker)
            return labels

        ranked: list[tuple[int, int, int, str]] = []
        has_prefix_match = False
        for index, item in enumerate(items):
            label = str(item.get("displayLabel", "")).strip() or str(item.get("vendor", "")).strip()
            vendor_key = str(item.get("vendorKey", "")).strip().lower()
            marker = (label.lower(), vendor_key)
            if not label or marker in seen:
                continue
            score = self._vendor_match_score(item, normalized)
            if score is None:
                continue
            if score[0] <= 1:
                has_prefix_match = True
            ranked.append((score[0], score[1], index, label))
            seen.add(marker)
        if has_prefix_match:
            ranked = [item for item in ranked if item[0] <= 1]
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in ranked]

    def _canonical_vendor_candidates(self, excluded_vendor_id: int = 0) -> list[dict[str, Any]]:
        """Return root catalog vendors that can be used as merge targets."""

        candidates: list[dict[str, Any]] = []
        for item in self.screen_api.list_vendors():
            vendor_id = int(item.get("vendorId", 0) or 0)
            root_vendor_id = int(item.get("rootVendorId", vendor_id) or vendor_id)
            if vendor_id <= 0 or vendor_id != root_vendor_id or vendor_id == excluded_vendor_id:
                continue
            candidates.append(dict(item))
        candidates.sort(key=lambda item: str(item.get("canonicalVendor", "")).lower())
        return candidates

    def _vendor_candidate_label(self, item: dict[str, Any]) -> str:
        """Return one compact canonical-vendor label for merge target selection."""

        vendor = str(item.get("canonicalVendor", "")).strip() or "Unknown"
        alias = str(item.get("canonicalAlias", "")).strip()
        if alias and alias.lower() != vendor.lower():
            return f"{vendor} [{alias}]"
        return vendor

    def _vendor_candidate_score(self, item: dict[str, Any], query: str) -> tuple[int, int] | None:
        """Rank one canonical vendor for merge-target search."""

        normalized = query.strip().lower()
        if not normalized:
            return (3, 0)
        fields = (
            (0, str(item.get("canonicalVendor", "")).strip()),
            (1, str(item.get("canonicalAlias", "")).strip()),
            (2, str(item.get("nickname", "")).strip()),
            (3, self._vendor_candidate_label(item)),
        )
        best: tuple[int, int] | None = None
        for field_rank, value in fields:
            lowered = value.lower()
            if not lowered:
                continue
            score: tuple[int, int] | None = None
            if lowered == normalized:
                score = (0, field_rank)
            elif lowered.startswith(normalized):
                score = (1, field_rank)
            elif normalized in lowered:
                score = (2, field_rank)
            if score is not None and (best is None or score < best):
                best = score
        return best

    def _best_vendor_candidate(self, query: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return the best canonical vendor match for one free-text query."""

        ranked: list[tuple[int, int, int, dict[str, Any]]] = []
        for index, item in enumerate(candidates):
            score = self._vendor_candidate_score(item, query)
            if score is None:
                continue
            ranked.append((score[0], score[1], index, item))
        if not ranked:
            return None
        if any(item[0] <= 1 for item in ranked):
            ranked = [item for item in ranked if item[0] <= 1]
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        return ranked[0][3]

    def _prompt_merge_target(self, detail: dict[str, Any]) -> dict[str, Any] | None:
        """Open one popup that chooses the canonical vendor to keep during merge."""

        source_vendor_id = int(detail.get("vendorId", 0) or 0)
        candidates = self._canonical_vendor_candidates(source_vendor_id)
        if not candidates:
            self.vendor_feedback_text = "No other canonical vendors are available for merge."
            self._render_vendor_panel()
            return None

        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowTitle("Merge Vendor")
        dialog.setMinimumWidth(520)
        self._apply_dialog_chrome(dialog)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        source_name = str(detail.get("canonicalVendor", detail.get("vendor", "Vendor"))).strip() or "Vendor"
        layout.addWidget(self._dialog_title(f"MERGE {source_name.upper()} INTO"))

        search_input = QLineEdit()
        search_input.setPlaceholderText("Search canonical vendor name or alias")
        search_input.setStyleSheet(self._input_style())
        layout.addWidget(search_input)

        labels = [self._vendor_candidate_label(item) for item in candidates]
        completer = QCompleter(labels, dialog)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        search_input.setCompleter(completer)

        helper = self._dialog_note("Choose the vendor that will remain visible after the soft merge.")
        layout.addWidget(helper)

        selection: dict[str, Any] = {}

        def accept_selection() -> None:
            candidate = self._best_vendor_candidate(search_input.text(), candidates)
            if candidate is None:
                helper.setText("No canonical vendor matched the current search.")
                return
            selection["vendor"] = dict(candidate)
            dialog.accept()

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        choose_button = self._action_button("SELECT TARGET", role="interactive", filled=True)
        choose_button.clicked.connect(accept_selection)
        cancel_button = self._action_button("CANCEL")
        cancel_button.clicked.connect(dialog.reject)
        action_row.addWidget(choose_button)
        action_row.addWidget(cancel_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        search_input.returnPressed.connect(accept_selection)
        search_input.setFocus()
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return None
        return dict(selection.get("vendor", {})) if selection else None

    def _prompt_merge_alias_choice(self, source_vendor: dict[str, Any], target_vendor: dict[str, Any]) -> str | None:
        """Open one popup that chooses the alias cluster to keep after merge."""

        source_alias = str(source_vendor.get("canonicalAlias", "") or source_vendor.get("canonicalVendor", "")).strip()
        target_alias = str(target_vendor.get("canonicalAlias", "") or target_vendor.get("canonicalVendor", "")).strip()
        if not source_alias:
            return target_alias or None
        if not target_alias:
            return source_alias
        if self._normalize_alias_key(source_alias) == self._normalize_alias_key(target_alias):
            return target_alias

        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowTitle("Select Alias To Keep")
        dialog.setMinimumWidth(520)
        self._apply_dialog_chrome(dialog)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        layout.addWidget(self._dialog_title("CHOOSE ALIAS TO KEEP"))
        prompt = self._dialog_note(
            "These vendors use different aliases. Choose the alias that should become the shared vendor identity.",
        )
        layout.addWidget(prompt)

        selector = QComboBox()
        selector.setStyleSheet(self._input_style())
        selector.addItem(f"Keep target alias ({target_alias})", target_alias)
        selector.addItem(f"Keep source alias ({source_alias})", source_alias)
        selector.addItem("Enter custom alias", "__custom__")
        layout.addWidget(selector)

        custom_alias_input = QLineEdit()
        custom_alias_input.setPlaceholderText("Custom alias")
        custom_alias_input.setStyleSheet(self._input_style())
        custom_alias_input.setEnabled(False)
        layout.addWidget(custom_alias_input)

        def sync_custom_enabled() -> None:
            custom_alias_input.setEnabled(str(selector.currentData()) == "__custom__")
            if custom_alias_input.isEnabled():
                custom_alias_input.setFocus()

        selector.currentIndexChanged.connect(lambda _: sync_custom_enabled())

        chosen: dict[str, str] = {}

        def accept_choice() -> None:
            data = str(selector.currentData() or "").strip()
            alias = custom_alias_input.text().strip() if data == "__custom__" else data
            if not alias:
                prompt.setText("Alias is required to continue the merge.")
                return
            chosen["alias"] = alias
            dialog.accept()

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        keep_button = self._action_button("CONTINUE", role="interactive", filled=True)
        keep_button.clicked.connect(accept_choice)
        cancel_button = self._action_button("CANCEL")
        cancel_button.clicked.connect(dialog.reject)
        actions.addWidget(keep_button)
        actions.addWidget(cancel_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        sync_custom_enabled()
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return None
        return chosen.get("alias")

    def _confirm_vendor_delete(self, vendor_name: str) -> bool:
        dialog = QDialog(self)
        dialog.setModal(True)
        dialog.setWindowTitle("Delete Vendor")
        dialog.setMinimumWidth(520)
        self._apply_dialog_chrome(dialog)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self._dialog_title(f"DELETE {vendor_name.upper()}?"))
        layout.addWidget(
            self._dialog_note(
                "This removes the catalog vendor record. Vendor mappings are reconciled immediately afterward so the ledger and vendor views stay consistent."
            )
        )
        layout.addWidget(
            self._dialog_note(
                "Transactions are not deleted, but this vendor entry will no longer be available for direct editing or merge actions.",
                role="muted",
            )
        )

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        delete_button = self._action_button("DELETE VENDOR", role="danger", filled=True)
        cancel_button = self._action_button("CANCEL")
        delete_button.clicked.connect(dialog.accept)
        cancel_button.clicked.connect(dialog.reject)
        actions.addWidget(delete_button)
        actions.addWidget(cancel_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return dialog.exec() == int(QDialog.DialogCode.Accepted)

    def _begin_vendor_merge(self, detail: dict[str, Any]) -> None:
        """Collect merge inputs, then start one soft vendor merge in the background."""

        if self.vendor_action_busy:
            return
        source_vendor = self._catalog_vendor_for_detail(detail)
        if not source_vendor:
            self.vendor_feedback_text = "No canonical vendor is available for merge."
            self._render_vendor_panel()
            return
        target_vendor = self._prompt_merge_target(detail)
        if not target_vendor:
            return
        kept_alias = self._prompt_merge_alias_choice(source_vendor, target_vendor)
        if kept_alias is None:
            return
        self._run_vendor_action(
            action_id="merge_vendor",
            label=f"MERGING {str(target_vendor.get('canonicalVendor', 'VENDOR')).upper()}",
            handler=lambda source_id=int(source_vendor.get("vendorId", 0) or 0), target_id=int(target_vendor.get("vendorId", 0) or 0), alias=str(kept_alias): self.screen_api.merge_vendors(source_id, target_id, alias),
        )

    def _begin_vendor_unmerge(self, merged_vendor: dict[str, Any]) -> None:
        """Restore one merged vendor row back into its own canonical cluster."""

        if self.vendor_action_busy:
            return
        source_vendor_id = int(merged_vendor.get("vendorId", 0) or 0)
        if source_vendor_id <= 0:
            self.vendor_feedback_text = "Merged vendor id is missing."
            self._render_vendor_panel()
            return
        self._run_vendor_action(
            action_id="unmerge_vendor",
            label=f"UNMERGING {str(merged_vendor.get('canonicalVendor', 'VENDOR')).upper()}",
            handler=lambda source_id=source_vendor_id: self.screen_api.unmerge_vendor(source_id),
        )

    def _run_vendor_action(self, *, action_id: str, label: str, handler) -> None:
        """Execute one vendor mutation in the worker pool and keep the panel responsive."""

        if self.vendor_action_busy:
            return
        self.vendor_action_busy = True
        self.vendor_action_label = label
        self.vendor_feedback_text = f"{label.replace('_', ' ').title()} in progress."
        self._render_vendor_panel()

        worker = VendorMutationWorker(action_id=action_id, handler=handler)
        worker.signals.finished.connect(self._handle_vendor_action_finished)
        worker.signals.failed.connect(self._handle_vendor_action_failed)
        self.vendor_thread_pool.start(worker)

    def _handle_vendor_action_finished(self, action_id: str, report: dict[str, Any]) -> None:
        """Finalize one completed vendor mutation and reload the mounted cards."""

        try:
            self.vendor_action_busy = False
            self.vendor_action_label = ""
            root_vendor = str(report.get("canonicalVendor", "")).strip()
            root_alias = str(report.get("canonicalAlias", "")).strip()
            force_root_key = ""
            if action_id == "delete_vendor":
                self.active_vendor = ""
                self.active_vendor_key = ""
                self.active_vendor_alias_label = ""
                self.active_vendor_alias_value = ""
            elif action_id == "unmerge_vendor":
                restored_vendor = str(report.get("restoredVendor", "")).strip()
                restored_alias = str(report.get("restoredAlias", "")).strip()
                if restored_vendor:
                    root_vendor = restored_vendor
                    root_alias = restored_alias or restored_vendor
                    force_root_key = self._normalize_alias_key(root_alias or root_vendor)
            root_key = force_root_key or str(report.get("aliasKey", "")).strip() or self._normalize_alias_key(root_alias or root_vendor)
            if action_id != "delete_vendor" and root_vendor and root_key:
                self.active_vendor = root_vendor
                self.active_vendor_key = root_key
                self.active_vendor_alias_label = ""
                self.active_vendor_alias_value = ""
            reconcile = report.get("reconcile", {}) if isinstance(report.get("reconcile", {}), dict) else {}
            changed_rows = int(reconcile.get("changedRows", 0) or 0)
            if action_id == "save_vendor":
                saved_vendor = str(report.get("canonicalVendor", "")).strip() or "vendor"
                if str(report.get("action", "updated")).strip().lower() == "created":
                    self.vendor_feedback_text = f"Created vendor {saved_vendor}. Reconciled {changed_rows} transaction row(s)."
                else:
                    self.vendor_feedback_text = f"Saved vendor {saved_vendor}. Reconciled {changed_rows} transaction row(s)."
            elif action_id == "delete_vendor":
                deleted_vendor = str(report.get("deletedVendor", "")).strip() or "vendor"
                self.vendor_feedback_text = f"Deleted vendor {deleted_vendor}. Reconciled {changed_rows} transaction row(s)."
            elif action_id == "merge_vendor":
                self.vendor_feedback_text = f"Merged vendor cluster. Reconciled {changed_rows} transaction row(s)."
            elif action_id == "unmerge_vendor":
                self.vendor_feedback_text = f"Restored merged vendor. Reconciled {changed_rows} transaction row(s)."
            elif action_id == "merge_raw_alias":
                raw_name = str(report.get("rawName", "")).strip()
                self.vendor_feedback_text = f"Merged raw alias {raw_name or 'vendor alias'}. Reconciled {changed_rows} transaction row(s)."
            elif action_id == "unmerge_raw_alias":
                raw_name = str(report.get("rawName", "")).strip()
                self.vendor_feedback_text = f"Removed raw alias {raw_name or 'vendor alias'}. Reconciled {changed_rows} transaction row(s)."
            elif action_id == "add_alias_category":
                category = str(report.get("category", "")).strip() or "category"
                self.vendor_feedback_text = f"Added shared category {category}. Reconciled {changed_rows} transaction row(s)."
            elif action_id == "remove_alias_category":
                category = str(report.get("category", "")).strip() or "category"
                self.vendor_feedback_text = f"Removed shared category {category}. Reconciled {changed_rows} transaction row(s)."
            else:
                self.vendor_feedback_text = "Vendor update completed."
            self.vendor_editor_mode = ""
            self.vendor_form_data = {}
            self._invalidate_vendor_detail_cache()
            if self.window_api is not None:
                self._schedule_safe_reload(failure_scope="vendor")
            else:
                self._render_vendor_panel()
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Vendor action completion crashed action_id=%s error=%s", action_id, exc)
            self.vendor_action_busy = False
            self.vendor_action_label = ""
            self.vendor_feedback_text = f"Vendor action completed but UI refresh failed: {exc}"
            self._render_vendor_panel()

    def _handle_vendor_action_failed(self, action_id: str, message: str) -> None:
        """Restore the vendor panel state after one failed background mutation."""

        self.logger.warning("Vendor action failed action_id=%s message=%s", action_id, message)
        self.vendor_action_busy = False
        self.vendor_action_label = ""
        self.vendor_feedback_text = f"Vendor update failed: {message}"
        self._render_vendor_panel()

    def _handle_transaction_action_finished(self, action_id: str, report: dict[str, Any]) -> None:
        """Finalize one completed ledger mutation and reload the expense cards."""

        _ = action_id, report
        self.ledger_action_busy = False
        self.ledger_action_label = ""
        self.ledger_action_error = ""
        self.ledger_action_transaction_key = ""
        self._invalidate_vendor_detail_cache()
        if self.window_api is not None:
            self._schedule_safe_reload(failure_scope="ledger")
        else:
            self._render_ledger_panel()
            self._render_vendor_panel()

    def _handle_transaction_action_failed(self, action_id: str, message: str) -> None:
        """Restore the ledger panel state after one failed background mutation."""

        self.logger.warning("Ledger action failed action_id=%s message=%s", action_id, message)
        self.ledger_action_busy = False
        self.ledger_action_label = ""
        self.ledger_action_error = f"Ledger update failed: {message}"
        self.ledger_action_transaction_key = ""
        self._render_ledger_panel()

    def _schedule_safe_reload(self, *, failure_scope: str = "vendor") -> None:
        """Defer one targeted expense-data reload until the current Qt event finishes."""

        if self.window_api is None:
            if failure_scope == "ledger":
                self._render_ledger_panel()
            else:
                self._render_vendor_panel()
            return

        def run_reload() -> None:
            try:
                callback = getattr(self.window_api, "reload_cards", None)
                if callable(callback):
                    callback(("panel.expenses", "panel.expenses_tab"))
                else:
                    self.window_api.reload_all_cards()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Deferred reload failed after %s action error=%s", failure_scope, exc)
                if failure_scope == "ledger":
                    self.ledger_action_error = f"Card reload failed after ledger update: {exc}"
                    self._render_ledger_panel()
                else:
                    self.vendor_feedback_text = f"Vendor action applied but card reload failed: {exc}"
                    self._render_vendor_panel()

        QTimer.singleShot(0, run_reload)

    def _vendor_detail(self, vendor_identity: str) -> dict[str, Any]:
        vendor_identity = vendor_identity.strip()
        if not vendor_identity:
            return {}

        rows = [
            row
            for row in self.widget_data.get("transactions", [])
            if str(row.get("vendorKey", row.get("vendor", ""))).strip() == vendor_identity
        ]
        visible = [row for row in rows if self.show_ignored or not bool(row.get("ignored"))]
        if not visible:
            return {}

        debit_rows  = [row for row in visible if self._direction(row) == "debit"]
        credit_rows = [row for row in visible if self._direction(row) == "credit"]
        range_rows  = [row for row in visible if self._year(row) == self.selected_year and (self.selected_month <= 0 or self._month(row) == self.selected_month)]
        monthly_map = defaultdict(float)
        yearly_map  = defaultdict(int)

        for row in visible:
            stamp = self._timestamp(row)
            if stamp is None:
                continue
            monthly_map[(stamp.year, stamp.month)] += self._amount(row)
            yearly_map[stamp.year] += 1

        monthly_series = []
        for year, month in sorted(monthly_map.keys()):
            monthly_series.append({"label": datetime(year, month, 1).strftime("%b %y"), "amount": monthly_map[(year, month)]})
        yearly_series = [{"label": str(year), "amount": float(count)} for year, count in sorted(yearly_map.items())]
        active_months = max(1, len(monthly_map))
        lead          = visible[0]
        vendor_record = self._catalog_vendor_for_row(lead)
        categories    = list(vendor_record.get("categories", [])) if isinstance(vendor_record, dict) else []
        merge_members = list(vendor_record.get("mergeMembers", [])) if isinstance(vendor_record, dict) else []
        return {
            "vendor": str(lead.get("vendor", "Unknown")),
            "vendorKey": str(lead.get("vendorKey", vendor_identity)),
            "vendorId": int(vendor_record.get("vendorId", 0) or 0) if isinstance(vendor_record, dict) else 0,
            "canonicalVendor": str((vendor_record.get("canonicalVendor") if isinstance(vendor_record, dict) else "") or lead.get("canonicalVendor", lead.get("vendor", "Unknown")) or lead.get("vendor", "Unknown")),
            "canonicalAlias": str((vendor_record.get("canonicalAlias") if isinstance(vendor_record, dict) else "") or lead.get("vendor", "Unknown") or lead.get("canonicalVendor", "Unknown")),
            "aliasKey": str(lead.get("aliasKey", vendor_identity)).strip() or vendor_identity,
            "category": str((categories[0] if categories else lead.get("category", "Uncategorized")) or "Uncategorized"),
            "categories": categories,
            "subcategory": str(lead.get("subcategory", "") or ""),
            "nickname": str(vendor_record.get("nickname", "")) if isinstance(vendor_record, dict) else "",
            "notes": str(vendor_record.get("notes", "")) if isinstance(vendor_record, dict) else "",
            "mergeMembers": merge_members,
            "transactionCount": len(visible),
            "totalDebit": sum(self._amount(row) for row in debit_rows),
            "totalCredit": sum(self._amount(row) for row in credit_rows),
            "net": sum(self._amount(row) for row in credit_rows) - sum(self._amount(row) for row in debit_rows),
            "selectedRangeDebit": sum(self._amount(row) for row in range_rows if self._direction(row) == "debit"),
            "averagePerMonth": sum(self._amount(row) for row in visible) / active_months,
            "activeMonths": active_months,
            "monthlyTotals": monthly_series,
            "yearlyTransactionCounts": yearly_series,
            "transactions": sorted(visible, key=lambda item: str(item.get("timestamp", "")), reverse=True),
        }

    def _vendor_summary(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets = defaultdict(lambda: {"amount": 0.0, "count": 0, "category": "Uncategorized", "vendor": "Unknown"})
        for row in rows:
            vendor_key = str(row.get("vendorKey", row.get("vendor", ""))).strip() or "Unknown"
            vendor     = str(row.get("vendor", "")).strip() or "Unknown"
            buckets[vendor_key]["amount"] += self._amount(row)
            buckets[vendor_key]["count"] += 1
            buckets[vendor_key]["category"] = str(row.get("category", "Uncategorized") or "Uncategorized")
            if buckets[vendor_key]["vendor"] == "Unknown" or (vendor.count(" ") > buckets[vendor_key]["vendor"].count(" ")):
                buckets[vendor_key]["vendor"] = vendor
        result = [{"vendor": payload["vendor"], "vendorKey": vendor_key, "amount": payload["amount"], "count": payload["count"], "category": payload["category"]} for vendor_key, payload in buckets.items()]
        result.sort(key=lambda item: (item["amount"], item["count"], item["vendor"]), reverse=True)
        return result

    def _category_summary(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets = defaultdict(lambda: {"amount": 0.0, "count": 0})
        for row in rows:
            label = str(row.get("category", "Uncategorized") or "Uncategorized")
            buckets[label]["amount"] += self._amount(row)
            buckets[label]["count"] += 1
        result = [{"label": label, "amount": payload["amount"], "count": payload["count"]} for label, payload in buckets.items()]
        result.sort(key=lambda item: item["amount"], reverse=True)
        return result

    def _insight_summary(self, rows: list[dict[str, Any]]) -> list[str]:
        if not rows:
            return ["No month-scoped debit data for the selected month.", "The year chart above can still show debit from other months in the same year."]
        amounts  = [self._amount(row) for row in rows]
        biggest  = max(rows, key=lambda row: self._amount(row))
        daily    = self._daily_series(rows)
        weekly   = self._weekly_series(rows)
        top_day  = max(daily, key=lambda item: float(item.get("amount", 0.0) or 0.0), default={"label": "-", "amount": 0.0})
        top_week = max(weekly, key=lambda item: float(item.get("amount", 0.0) or 0.0), default={"label": "-", "amount": 0.0})
        lines = [
            f"Largest debit: {biggest.get('vendor', 'Unknown')} | {self._format_inr(self._amount(biggest))}",
            f"Median debit: {self._format_inr(median(amounts) if amounts else 0.0)}",
            f"Highest spend day: {top_day.get('label', '-')} | {self._format_inr(float(top_day.get('amount', 0.0) or 0.0))}",
            f"Highest spend week: {top_week.get('label', '-')} | {self._format_inr(float(top_week.get('amount', 0.0) or 0.0))}",
        ]
        recurring = self._active_month_recurrence(rows)
        if recurring:
            lines.append(
                f"Strongest recurring: {recurring['vendor']} | {recurring['cadence']} | {self._format_inr(float(recurring['averageAmount']))}"
            )
            lines.append(
                f"Recurring monthly burden: {self._format_inr(float(recurring['monthlyLikeAmount']))} across {int(recurring['activeCount'])} vendor(s)"
            )
        return lines

    def _month_scoped_metrics(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate month-scoped KPI values for the insights command panel."""

        if not rows:
            return {
                "largest_amount": 0.0,
                "largest_vendor": "NO_VENDOR",
                "median_amount": 0.0,
                "peak_day_label": "--",
                "peak_day_amount": 0.0,
                "peak_week_label": "--",
                "peak_week_amount": 0.0,
                "recurring_amount": 0.0,
                "recurring_label": "NO_STABLE_PATTERN",
                "monthly_burden": 0.0,
                "active_recurring_count": 0,
            }

        amounts = [self._amount(row) for row in rows]
        largest_row = max(rows, key=lambda row: self._amount(row))
        daily = self._daily_series(rows)
        weekly = self._weekly_series(rows)
        peak_day = max(daily, key=lambda item: float(item.get("amount", 0.0) or 0.0), default={"label": "--", "amount": 0.0})
        peak_week = max(weekly, key=lambda item: float(item.get("amount", 0.0) or 0.0), default={"label": "--", "amount": 0.0})
        recurring = self._active_month_recurrence(rows)

        recurring_amount = float(recurring.get("averageAmount", 0.0) or 0.0) if recurring else 0.0
        recurring_vendor = str(recurring.get("vendor", "")).strip() if recurring else ""
        recurring_cadence = str(recurring.get("cadence", "")).strip() if recurring else ""
        recurring_label = (
            f"{recurring_vendor.upper()} | {recurring_cadence.upper()}"
            if recurring_vendor and recurring_cadence
            else "NO_STABLE_PATTERN"
        )
        monthly_burden = float(recurring.get("monthlyLikeAmount", 0.0) or 0.0) if recurring else 0.0
        active_count = int(recurring.get("activeCount", 0) or 0) if recurring else 0

        return {
            "largest_amount": self._amount(largest_row),
            "largest_vendor": str(largest_row.get("vendor", "UNKNOWN")).upper(),
            "median_amount": median(amounts) if amounts else 0.0,
            "peak_day_label": str(peak_day.get("label", "--") or "--"),
            "peak_day_amount": float(peak_day.get("amount", 0.0) or 0.0),
            "peak_week_label": str(peak_week.get("label", "--") or "--"),
            "peak_week_amount": float(peak_week.get("amount", 0.0) or 0.0),
            "recurring_amount": recurring_amount,
            "recurring_label": recurring_label,
            "monthly_burden": monthly_burden,
            "active_recurring_count": active_count,
        }

    def _selected_scoped_metrics(self) -> dict[str, Any]:
        """Map complete service-side selected aggregates to the insight card contract."""

        insights = dict(self.widget_data.get("selectedInsights", {}))
        averages = dict(self.widget_data.get("selectedAverages", {}))
        largest = dict(insights.get("largestTransaction", {}))
        peak_day = dict(insights.get("highestSpendDay", {}))
        peak_week = dict(insights.get("highestSpendWeek", {}))
        recurring = dict(insights.get("strongestRecurring", {}))
        recurring_vendor = str(recurring.get("vendor", "")).strip()
        recurring_cadence = str(recurring.get("cadence", "")).strip()
        return {
            "largest_amount": float(largest.get("amount", 0.0) or 0.0),
            "largest_vendor": str(largest.get("vendor", "NO_VENDOR") or "NO_VENDOR").upper(),
            "median_amount": float(insights.get("medianTransactionAmount", 0.0) or 0.0),
            "peak_day_label": str(peak_day.get("label", "--") or "--"),
            "peak_day_amount": float(peak_day.get("amount", 0.0) or 0.0),
            "peak_week_label": str(peak_week.get("label", "--") or "--"),
            "peak_week_amount": float(peak_week.get("amount", 0.0) or 0.0),
            "recurring_amount": float(recurring.get("averageAmount", 0.0) or 0.0),
            "recurring_label": (
                f"{recurring_vendor.upper()} | {recurring_cadence.upper()}"
                if recurring_vendor and recurring_cadence
                else "NO_STABLE_PATTERN"
            ),
            "monthly_burden": float(insights.get("recurringMonthlyBurden", 0.0) or 0.0),
            "active_recurring_count": int(insights.get("recurringActiveCount", 0) or 0),
            "average_debit": float(averages.get("averageTransactionAmount", 0.0) or 0.0),
        }

    def _daily_series(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.selected_month <= 0:
            return []
        year          = self.selected_year
        month         = self.selected_month
        days_in_month = 31 if month not in {4, 6, 9, 11} else 30
        if month == 2:
            days_in_month = 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
        buckets = {day: {"label": f"{day:02d}", "amount": 0.0} for day in range(1, days_in_month + 1)}
        for row in rows:
            stamp = self._timestamp(row)
            if stamp is None or stamp.year != year or stamp.month != month:
                continue
            buckets[stamp.day]["amount"] += self._amount(row)
        return [{"day": day, **payload} for day, payload in buckets.items()]

    def _weekly_series(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.selected_month <= 0:
            return []
        buckets = {week: {"label": f"W{week}", "amount": 0.0} for week in range(1, 6)}
        for row in rows:
            stamp = self._timestamp(row)
            if stamp is None or stamp.year != self.selected_year or stamp.month != self.selected_month:
                continue
            week = min(5, ((stamp.day - 1) // 7) + 1)
            buckets[week]["amount"] += self._amount(row)
        return [{"week": week, **payload} for week, payload in buckets.items()]

    def _yearly_series(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets = {month: 0.0 for month in range(1, 13)}
        for row in rows:
            stamp = self._timestamp(row)
            if stamp is None or stamp.year != self.selected_year:
                continue
            buckets[stamp.month] += self._amount(row)
        return [{"label": datetime(self.selected_year, month, 1).strftime("%b"), "month": month, "amount": buckets[month]} for month in range(1, 13)]

    def _top_vendor(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        summary = self._vendor_summary(rows)
        if not summary:
            return {"vendor": "None", "vendorKey": "", "amount": 0.0}
        return {"vendor": summary[0]["vendor"], "vendorKey": summary[0].get("vendorKey", ""), "amount": summary[0]["amount"]}

    def _summary_vendor_click(self, vendor: dict[str, Any]):
        """Return a click handler that opens vendor details from one metric card."""

        vendor_name = str(vendor.get("vendor", "")).strip()
        vendor_key = str(vendor.get("vendorKey", vendor_name)).strip()
        if not vendor_name or vendor_name == "None":
            return None
        return lambda vendor_name=vendor_name, vendor_key=vendor_key: self._set_active_vendor(vendor_name, vendor_key)

    def _ensure_active_vendor(self) -> None:
        current_identity = (self.active_vendor_key or self.active_vendor).strip()
        if current_identity:
            return
        self.active_vendor     = ""
        self.active_vendor_key = ""
        self.active_vendor_alias_label = ""
        self.active_vendor_alias_value = ""
        top                    = dict(self.widget_data.get("selectedTopVendor", {})) or self._top_vendor([row for row in self._selected_rows() if self._direction(row) == "debit"])
        if str(top.get("vendor", "None")) != "None":
            self.active_vendor     = str(top.get("vendor", ""))
            self.active_vendor_key = str(top.get("vendorKey", "")) or self.active_vendor

    def _year_changed(self, value: Any) -> None:
        if value is None:
            return
        self.selected_year = int(value)
        self.selected_page = 1
        self._reset_ledger_state()
        self._invalidate_vendor_detail_cache()
        self._load_analysis_snapshot()

    def _currency_changed(self, value: Any) -> None:
        currency = str(value or "").strip().upper()
        if not currency or currency == self.selected_currency:
            return
        self.selected_currency = currency
        self.selected_page = 1
        self._reset_ledger_state()
        self._invalidate_vendor_detail_cache()
        self._load_analysis_snapshot()

    def _month_changed(self, value: Any) -> None:
        if value is None:
            return
        self._apply_month_selection(int(value), partial=False)

    def _ignored_changed(self, checked: bool) -> None:
        self.show_ignored  = checked
        self.selected_page = 1
        self._reset_ledger_state()
        self._invalidate_vendor_detail_cache()
        self._load_analysis_snapshot()

    def _apply_month_selection(self, month_value: int, *, partial: bool) -> None:
        self.selected_month = month_value
        self.selected_page  = 1
        self._reset_ledger_state()
        self._invalidate_vendor_detail_cache()
        self._load_analysis_snapshot()

    def _change_page(self, page: int) -> None:
        self.selected_page = max(1, page)
        self._render()

    def _reset_ledger_state(self) -> None:
        self.expanded_groups.clear()
        self.selected_calendar_day = ""
        self.ledger_groups_cache.clear()
        self.group_rows_cache.clear()
        self.group_widget_pools.clear()
        self.ledger_groups_busy = False
        self.ledger_groups_error = ""
        self._active_ledger_groups_request_key = None
        self.ledger_rows_busy = False
        self.ledger_rows_job_label = ""
        self.ledger_rows_error = ""
        self._active_ledger_rows_request_key = None
        self._pending_ledger_request = None

    def _expand_all_groups(self) -> None:
        groups = self._ledger_groups()
        if not groups:
            return
        self.expanded_groups = {
            str(group.get("groupKey", "")).strip()
            for group in groups
            if str(group.get("groupKey", "")).strip()
        }
        self._render()

    def _select_calendar_day(self, group_key: str) -> None:
        normalized = group_key.strip()
        if not normalized:
            return
        self.selected_calendar_day = "" if self.selected_calendar_day == normalized else normalized
        self._render_ledger_panel()

    def _ledger_search_changed(self, text: str) -> None:
        self.pending_ledger_text = text
        self.ledger_search_timer.start()

    def _apply_ledger_search(self) -> None:
        self.ledger_search_text = self.pending_ledger_text.strip()
        self.selected_page      = 1
        self._reset_ledger_state()
        self._load_analysis_snapshot()

    def _vendor_search_changed(self, text: str) -> None:
        self.pending_vendor_text = text
        if self.vendor_search_input is not None:
            completer = self.vendor_search_input.completer()
            if completer is not None:
                labels = self._vendor_autocomplete_labels(text)[:40]
                completer.setModel(QStringListModel(labels, completer))
        self.vendor_search_timer.start()

    def _apply_vendor_search(self) -> None:
        self.vendor_search_text = self.pending_vendor_text.strip()

    def _load_analysis_snapshot(self) -> None:
        """Refresh the bounded DB-backed analysis payload outside the GUI thread."""

        loader = getattr(self.screen_api, "get_analysis_snapshot", None)
        if not callable(loader):
            return
        self._analysis_snapshot_request_id += 1
        request_id = self._analysis_snapshot_request_id
        self.analysis_snapshot_busy = True
        self.analysis_snapshot_error = ""
        self._set_analysis_filter_enabled(False)
        worker = AnalysisSnapshotWorker(
            loader=loader,
            request_id=request_id,
            filters={
                "year": self.selected_year,
                "month": self.selected_month,
                "currency": self.selected_currency,
                "include_ignored": True,
                "search_text": self.ledger_search_text,
            },
        )
        worker.signals.finished.connect(self._analysis_snapshot_loaded)
        worker.signals.failed.connect(self._analysis_snapshot_failed)
        self.analysis_snapshot_thread_pool.start(worker)

    def _analysis_snapshot_loaded(self, request_id: int, payload: dict[str, Any]) -> None:
        if request_id != self._analysis_snapshot_request_id:
            return
        self.analysis_snapshot_busy = False
        self.analysis_snapshot_error = ""
        self.widget_data = dict(payload)
        self.vendor_autocomplete_cache.clear()
        self._sync_state_from_data(reset=False)
        self._reset_ledger_state()
        self._invalidate_vendor_detail_cache()
        self._ensure_active_vendor()
        self._set_analysis_filter_enabled(True)
        self._render_summary_panel()
        self._render_charts_panel()
        self._render_insights_panel()
        self._render_ledger_panel()
        self._render_vendor_panel()
        self._ensure_vendor_panel_loaded()

    def _analysis_snapshot_failed(self, request_id: int, message: str) -> None:
        if request_id != self._analysis_snapshot_request_id:
            return
        self.analysis_snapshot_busy = False
        self.analysis_snapshot_error = str(message or "Could not update analysis.")
        self._set_analysis_filter_enabled(True)
        if self.search_input is not None:
            self.search_input.setToolTip(self.analysis_snapshot_error)

    def _set_analysis_filter_enabled(self, enabled: bool) -> None:
        for control in (self.year_combo, self.month_combo, self.currency_combo):
            if control is not None:
                control.setEnabled(enabled)
        if self.search_input is not None:
            self.search_input.setAccessibleDescription("" if enabled else "Updating analysis")
            self.search_input.setToolTip("" if enabled else "Updating analysis in the background…")

    def _set_active_vendor(
        self,
        vendor: str,
        vendor_key: str = "",
        *,
        alias_label: str = "",
        alias_value: str = "",
    ) -> None:
        self.active_vendor      = vendor.strip()
        self.active_vendor_key  = vendor_key.strip() or self.active_vendor
        self.active_vendor_alias_label = alias_label.strip()
        self.active_vendor_alias_value = alias_value.strip()
        self.vendor_editor_mode = ""
        self.vendor_form_data   = {}
        self._ensure_vendor_panel_loaded(force=True)
        self._focus_vendor_panel()

    def _clear_vendor(self) -> None:
        self.active_vendor       = ""
        self.active_vendor_key   = ""
        self.active_vendor_alias_label = ""
        self.active_vendor_alias_value = ""
        self.vendor_search_text  = ""
        self.pending_vendor_text = ""
        self.vendor_editor_mode  = ""
        self.vendor_form_data    = {}
        self.vendor_panel_state  = "idle"
        self.loaded_vendor_detail = None
        self.vendor_detail_error  = ""
        self.vendor_detail_loading_key = None
        self._render_vendor_panel()

    def _select_vendor_from_input(self) -> None:
        if self.vendor_search_input is None:
            return
        self._select_vendor_from_label(self.vendor_search_input.text())

    def _select_vendor_from_completion(self, text: str) -> None:
        self._select_vendor_from_label(text)

    def _select_vendor_from_label(self, label: str) -> None:
        query = label.strip().lower()
        if not query:
            return
        ranked: list[tuple[int, int, int, str, str, dict[str, Any]]] = []
        for index, item in enumerate(self._vendor_autocomplete_items(label)):
            vendor = str(item.get("vendor", "")).strip()
            vendor_key = str(item.get("vendorKey", vendor)).strip() or vendor
            if not vendor_key:
                continue
            score = self._vendor_match_score(item, query)
            if score is None:
                continue
            target_vendor = vendor or str(item.get("canonicalVendor", "")).strip() or "Unknown"
            ranked.append((score[0], score[1], index, target_vendor, vendor_key, item))
        if not ranked:
            directory_search = getattr(self.screen_api, "search_vendor_directory", None)
            if callable(directory_search):
                # The snapshot intentionally bounds suggestions for large ledgers.
                # A deliberate Enter search can still resolve a less-frequent vendor.
                try:
                    remote_items = [dict(item) for item in directory_search(label, limit=40) if isinstance(item, dict)]
                except Exception:  # noqa: BLE001
                    remote_items = []
                for index, item in enumerate(remote_items):
                    vendor = str(item.get("vendor", "")).strip()
                    vendor_key = str(item.get("vendorKey", vendor)).strip() or vendor
                    score = self._vendor_match_score(item, query)
                    if vendor_key and score is not None:
                        ranked.append((score[0], score[1], index, vendor or "Unknown", vendor_key, item))
            if not ranked:
                return
        if any(item[0] <= 1 for item in ranked):
            ranked = [item for item in ranked if item[0] <= 1]
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        selected_item = ranked[0][5]
        selected_alias_value = ""
        selected_alias_label = ""
        if str(selected_item.get("entryType", "canonical")).strip().lower() == "alias":
            selected_alias_value = str(selected_item.get("aliasValue", "")).strip()
            selected_alias_label = str(selected_item.get("displayLabel", "")).strip()
        self._set_active_vendor(
            ranked[0][3],
            ranked[0][4],
            alias_label=selected_alias_label,
            alias_value=selected_alias_value,
        )

    def _vendor_cache_key(self, vendor_name: str, alias_key: str) -> tuple[Any, ...]:
        return (
            vendor_name.strip(),
            alias_key.strip() or vendor_name.strip(),
            self.selected_year,
            self.selected_month,
            self.selected_currency,
            bool(self.show_ignored),
        )

    def _ensure_vendor_panel_loaded(self, *, force: bool = False) -> None:
        vendor_name = self.active_vendor.strip()
        alias_key = (self.active_vendor_key or self.active_vendor).strip()
        if not vendor_name and not alias_key:
            self.vendor_panel_state = "idle"
            self.loaded_vendor_detail = None
            self.vendor_detail_error = ""
            self.vendor_detail_loading_key = None
            self._render_vendor_panel()
            return

        cache_key = self._vendor_cache_key(vendor_name, alias_key)
        if not force and cache_key in self.vendor_detail_cache:
            self.loaded_vendor_detail      = self.vendor_detail_cache[cache_key]
            self.vendor_panel_state        = "loaded"
            self.vendor_detail_error       = ""
            self.vendor_detail_loading_key = None
            self._render_vendor_panel()
            return

        if self.vendor_detail_loading_key == cache_key and self.vendor_panel_state == "loading":
            self._render_vendor_panel()
            return

        self.vendor_panel_state        = "loading"
        self.loaded_vendor_detail      = None
        self.vendor_detail_error       = ""
        self.vendor_detail_loading_key = cache_key
        self._render_vendor_panel()

        worker = VendorDetailWorker(
            lookup_vendor         = self.screen_api.find_vendor,
            vendor_name           = vendor_name,
            alias_key             = alias_key,
            cache_key             = cache_key,
            all_rows              = list(self.widget_data.get("transactions", [])),
            recurring_patterns    = list(self.widget_data.get("recurringPatterns", [])),
            selected_year         = self.selected_year,
            selected_month        = self.selected_month,
            selected_currency     = self.selected_currency,
            dismissed_suggestions = self.dismissed_merge_suggestions.get(alias_key or vendor_name, set()),
            load_rows             = getattr(self.screen_api, "list_vendor_transactions", None),
        )
        worker.signals.finished.connect(self._handle_vendor_detail_loaded)
        worker.signals.failed.connect(self._handle_vendor_detail_failed)
        self.vendor_thread_pool.start(worker)

    def _handle_vendor_detail_loaded(self, vendor_identity: str, cache_key: tuple[Any, ...], detail: dict[str, Any]) -> None:
        current_key = (self.active_vendor_key or self.active_vendor).strip()
        if current_key != vendor_identity or self.vendor_detail_loading_key != cache_key:
            return
        self.vendor_detail_cache[cache_key] = detail
        self.loaded_vendor_detail           = detail or None
        self.vendor_panel_state             = "loaded" if detail else "idle"
        self.vendor_detail_error            = ""
        self.vendor_detail_loading_key      = None
        self._render_vendor_panel()

    def _handle_vendor_detail_failed(self, vendor_identity: str, cache_key: tuple[Any, ...], message: str) -> None:
        current_key = (self.active_vendor_key or self.active_vendor).strip()
        if current_key != vendor_identity or self.vendor_detail_loading_key != cache_key:
            return
        self.vendor_panel_state        = "error"
        self.loaded_vendor_detail      = None
        self.vendor_detail_error       = message
        self.vendor_detail_loading_key = None
        self._render_vendor_panel()

    def _invalidate_vendor_detail_cache(self) -> None:
        self.vendor_detail_cache.clear()
        self.loaded_vendor_detail = None if self.vendor_panel_state != "idle" else self.loaded_vendor_detail
        self.vendor_detail_loading_key = None

    def _focus_vendor_panel(self) -> None:
        if self.vendor_panel_container is None:
            return
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        if isinstance(parent, QScrollArea):
            QTimer.singleShot(0, lambda: parent.ensureWidgetVisible(self.vendor_panel_container, 0, 96))

    def _toggle_ignore_transaction(self, transaction_key: str, next_ignored: bool) -> None:
        if self.ledger_action_busy or not str(transaction_key or "").strip():
            return
        self.ledger_action_busy = True
        self.ledger_action_label = "Ignoring ledger row" if next_ignored else "Restoring ledger row"
        self.ledger_action_error = ""
        self.ledger_action_transaction_key = str(transaction_key).strip()
        self._render_ledger_panel()

        worker = TransactionMutationWorker(
            action_id="toggle_ignored",
            handler=lambda current_key=str(transaction_key), current_ignored=bool(next_ignored): self.screen_api.set_transaction_ignored_state(
                current_key,
                current_ignored,
            ),
        )
        worker.signals.finished.connect(self._handle_transaction_action_finished)
        worker.signals.failed.connect(self._handle_transaction_action_failed)
        self.vendor_thread_pool.start(worker)

    def _catalog_vendor_for_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        for candidate in (
            str(row.get("canonicalVendor", "")).strip(),
            str(row.get("vendor", "")).strip(),
            str(row.get("rawCounterparty", "")).strip(),
        ):
            if not candidate:
                continue
            record = self.screen_api.find_vendor(candidate)
            if record:
                return record
        return None

    def _catalog_vendor_for_detail(self, detail: dict[str, Any]) -> dict[str, Any] | None:
        if not detail:
            return None
        cached_record = detail.get("vendorRecord")
        if isinstance(cached_record, dict):
            return cached_record
        for candidate in (
            str(detail.get("canonicalVendor", "")).strip(),
            str(detail.get("canonicalAlias", "")).strip(),
            str(detail.get("vendor", "")).strip(),
        ):
            if not candidate:
                continue
            record = self.screen_api.find_vendor(candidate)
            if record:
                return record
        return None

    def _begin_vendor_create(self) -> None:
        self.vendor_editor_mode   = "new"
        self.vendor_form_data     = {"canonicalVendor": self.active_vendor or "", "canonicalAlias": self.active_vendor or "", "nickname": "", "notes": "", "categories": []}
        self.vendor_feedback_text = "Create a new catalog vendor from here."
        self._render_vendor_panel()

    def _begin_vendor_edit(self, detail: dict[str, Any]) -> None:
        record                    = self._catalog_vendor_for_detail(detail) if detail else None
        self.vendor_editor_mode   = "edit"
        self.vendor_form_data     = self._current_vendor_form(detail, record)
        self.vendor_feedback_text = "Edit vendor fields, categories, and merges."
        self._render_vendor_panel()

    def _cancel_vendor_edit(self) -> None:
        self.vendor_editor_mode   = ""
        self.vendor_form_data     = {}
        self.vendor_feedback_text = ""
        self._render_vendor_panel()

    def _current_vendor_form(self, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> dict[str, Any]:
        if self.vendor_form_data:
            return self.vendor_form_data
        if vendor_record:
            return {
                "vendorId": int(vendor_record.get("vendorId", 0) or 0),
                "canonicalVendor": str(vendor_record.get("canonicalVendor", "")),
                "canonicalAlias": str(vendor_record.get("canonicalAlias", "")),
                "nickname": str(vendor_record.get("nickname", "")),
                "notes": str(vendor_record.get("notes", "")),
                "categories": list(vendor_record.get("categories", [])),
            }
        return {
            "vendorId": int(detail.get("vendorId", 0) or 0) if detail else 0,
            "canonicalVendor": str(detail.get("canonicalVendor", detail.get("vendor", ""))) if detail else "",
            "canonicalAlias": str(detail.get("canonicalAlias", detail.get("vendor", ""))) if detail else "",
            "nickname": str(detail.get("nickname", "")) if detail else "",
            "notes": str(detail.get("notes", "")) if detail else "",
            "categories": list(detail.get("categories", [])) if detail else [],
        }

    def _add_form_category(self, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> None:
        value = self.vendor_category_entry.text().strip() if self.vendor_category_entry is not None else ""
        if not value:
            return
        form       = self._current_vendor_form(detail, vendor_record)
        categories = list(form.get("categories", []))
        if value.lower() not in {item.lower() for item in categories}:
            categories.append(value)
        self.vendor_form_data = {**form, "categories": categories}
        self._render_vendor_panel()

    def _remove_form_category(self, category: str, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> None:
        form                  = self._current_vendor_form(detail, vendor_record)
        categories            = [item for item in form.get("categories", []) if item.lower() != category.lower()]
        self.vendor_form_data = {**form, "categories": categories}
        self._render_vendor_panel()

    def _add_alias_category(self, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> None:
        """Add one shared category tag to the current alias cluster."""

        if self.vendor_action_busy:
            return
        value = self.alias_category_entry.text().strip() if self.alias_category_entry is not None else ""
        if not value:
            self.vendor_feedback_text = "Category is required."
            self._render_vendor_panel()
            return
        record = vendor_record or self._catalog_vendor_for_detail(detail)
        if not record:
            self.vendor_feedback_text = "No catalog vendor exists for the selected alias."
            self._render_vendor_panel()
            return
        self._run_vendor_action(
            action_id="add_alias_category",
            label=f"ADDING CATEGORY {value.upper()}",
            handler=lambda current_vendor_id=int(record.get("vendorId", 0) or 0), current_category=str(value): self.screen_api.add_vendor_category_and_reconcile(
                current_vendor_id,
                current_category,
            ),
        )

    def _remove_alias_category(self, category: str, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> None:
        """Remove one shared category tag from the current alias cluster."""

        if self.vendor_action_busy:
            return
        record = vendor_record or self._catalog_vendor_for_detail(detail)
        if not record:
            self.vendor_feedback_text = "No catalog vendor exists for the selected alias."
            self._render_vendor_panel()
            return
        self._run_vendor_action(
            action_id="remove_alias_category",
            label=f"REMOVING CATEGORY {str(category).upper()}",
            handler=lambda current_vendor_id=int(record.get("vendorId", 0) or 0), current_category=str(category): self.screen_api.remove_vendor_category_and_reconcile(
                current_vendor_id,
                current_category,
            ),
        )

    def _save_vendor_crud(self, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> None:
        if self.vendor_action_busy:
            return
        canonical_name  = self.vendor_name_input.text().strip() if self.vendor_name_input is not None else ""
        canonical_alias = self.vendor_alias_editor_input.text().strip() if self.vendor_alias_editor_input is not None else ""
        nickname        = self.vendor_nickname_input.text().strip() if self.vendor_nickname_input is not None else ""
        notes           = self.vendor_notes_input.text().strip() if self.vendor_notes_input is not None else ""
        categories      = list(self.vendor_form_data.get("categories", []))
        if not canonical_name:
            self.vendor_feedback_text = "Vendor name is required."
            self._render()
            return
        seed_raw = str(detail.get("rawCounterparty", "") or detail.get("vendor", "")).strip() if detail else ""
        self._run_vendor_action(
            action_id="save_vendor",
            label=f"SAVING {canonical_name.upper()}",
            handler=lambda current_vendor_id=int(vendor_record.get("vendorId", 0) or 0) if vendor_record else None,
            current_name=str(canonical_name),
            current_alias=str(canonical_alias or canonical_name),
            current_nickname=str(nickname),
            current_notes=str(notes),
            current_categories=list(categories),
            current_seed=str(seed_raw): self.screen_api.save_vendor_rule_and_reconcile(
                vendor_id=current_vendor_id,
                canonical_name=current_name,
                canonical_alias=current_alias,
                nickname=current_nickname,
                notes=current_notes,
                categories=current_categories,
                seed_raw_name=current_seed,
            ),
        )

    def _delete_vendor_crud(self, detail: dict[str, Any]) -> None:
        if self.vendor_action_busy:
            return
        record = self._catalog_vendor_for_detail(detail)
        if not record:
            self.vendor_feedback_text = "No catalog vendor exists for the selected item."
            self._render()
            return
        vendor_name = str(record.get("canonicalVendor", "Vendor")).strip() or "Vendor"
        if not self._confirm_vendor_delete(vendor_name):
            return
        self._run_vendor_action(
            action_id="delete_vendor",
            label=f"DELETING {vendor_name.upper()}",
            handler=lambda current_vendor_id=int(record.get("vendorId", 0) or 0): self.screen_api.delete_vendor_and_reconcile(current_vendor_id),
        )

    def _merge_suggestions(self, detail: dict[str, Any], vendor_record: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not detail:
            return []
        base = self._normalize_vendor_token(str(detail.get("canonicalAlias") or detail.get("vendor") or ""))
        if not base:
            return []
        vendor_key                            = str(detail.get("vendorKey", detail.get("vendor", "")))
        dismissed                             = self.dismissed_merge_suggestions.get(vendor_key, set())
        existing                              = {self._normalize_vendor_token(str(item.get("rawName", ""))) for item in (vendor_record.get("mergeMembers", []) if vendor_record else [])}
        buckets   : dict[str, dict[str, Any]] = {}
        for row in self.widget_data.get("transactions", []):
            raw_name = str(row.get("rawCounterparty", "") or row.get("vendor", "")).strip()
            if not raw_name:
                continue
            normalized = self._normalize_vendor_token(raw_name)
            if not normalized or normalized == base or normalized in existing or raw_name in dismissed:
                continue
            if not (normalized.startswith(base) or base.startswith(normalized) or base in normalized or normalized in base):
                continue
            bucket = buckets.setdefault(raw_name, {"rawName": raw_name, "count": 0, "amount": 0.0, "reason": f"Normalized match: {normalized}"})
            bucket["count"] += 1
            bucket["amount"] += self._amount(row)
        result = list(buckets.values())
        result.sort(key=lambda item: (item["count"], item["amount"], item["rawName"]), reverse=True)
        return result

    def _accept_merge_suggestion(self, vendor_id: int, raw_name: str) -> None:
        if self.vendor_action_busy:
            return
        if int(vendor_id or 0) <= 0:
            self.vendor_feedback_text = "Cannot merge: vendor id is missing."
            self._render_vendor_panel()
            return
        self._run_vendor_action(
            action_id="merge_raw_alias",
            label=f"MERGING {raw_name.upper()}",
            handler=lambda current_vendor_id=int(vendor_id), current_raw_name=str(raw_name): self.screen_api.merge_raw_alias(
                current_vendor_id,
                current_raw_name,
                source="fuzzy_accepted",
            ),
        )

    def _accept_merge_suggestion_for_detail(self, detail: dict[str, Any], raw_name: str) -> None:
        """Create the vendor catalog row when needed, then merge one fuzzy suggestion."""

        if self.vendor_action_busy:
            return
        self._run_vendor_action(
            action_id="merge_raw_alias",
            label=f"MERGING {raw_name.upper()}",
            handler=lambda current_detail=dict(detail), current_raw_name=str(raw_name): self.screen_api.accept_merge_suggestion(
                current_detail,
                current_raw_name,
                source="fuzzy_accepted",
            ),
        )

    def _dismiss_merge_suggestion(self, vendor_key: str, raw_name: str) -> None:
        self.dismissed_merge_suggestions.setdefault(vendor_key, set()).add(raw_name)
        self._invalidate_vendor_detail_cache()
        self._ensure_vendor_panel_loaded(force=True)

    def _remove_merge_member(self, vendor_id: int, raw_name: str) -> None:
        if self.vendor_action_busy:
            return
        if int(vendor_id or 0) <= 0:
            self.vendor_feedback_text = "Cannot unmerge: vendor id is missing."
            self._render_vendor_panel()
            return
        self._run_vendor_action(
            action_id="unmerge_raw_alias",
            label=f"REMOVING {raw_name.upper()}",
            handler=lambda current_vendor_id=int(vendor_id), current_raw_name=str(raw_name): self.screen_api.unmerge_raw_alias(
                current_vendor_id,
                current_raw_name,
            ),
        )

    def _is_loading_state(self) -> bool:
        state = self._expense_background()
        total_rows = int(self.widget_data.get("meta", {}).get("visibleTransactionCount", len(self.widget_data.get("transactions", []))) or 0)
        return bool(state.get("running")) or (total_rows == 0 and int(state.get("percent", 0) or 0) < 100)

    def _expense_background(self) -> dict[str, Any]:
        background = self.widget_data.get("backgroundState", {})
        if isinstance(background, dict):
            expenses = background.get("expenses", {})
            if isinstance(expenses, dict):
                return expenses
        meta = self.widget_data.get("meta", {})
        if isinstance(meta, dict):
            progress = meta.get("rebuildProgress", {})
            if isinstance(progress, dict):
                return progress
        return {"running": False, "label": "Ready", "processed": 0, "total": 0, "percent": 100}

    def _ledger_search_blob(self, row: dict[str, Any]) -> str:
        return " ".join(
            [
                str(row.get("vendor", "")),
                str(row.get("canonicalVendor", "")),
                str(row.get("rawCounterparty", "")),
                str(row.get("accountLabel", "")),
                str(row.get("reference", "")),
                str(row.get("title", "")),
                str(row.get("dayLabel", "")),
                str(row.get("direction", "")),
            ]
        ).lower()

    def _vendor_blob(self, row: dict[str, Any]) -> str:
        return " ".join(
            [
                str(row.get("vendor", "")),
                str(row.get("canonicalVendor", "")),
                str(row.get("category", "")),
            ]
        ).lower()

    def _decorate_repository_row(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        stamp   = self._timestamp(payload)
        vendor, vendor_key = self._vendor_identity(payload)
        payload["vendor"] = vendor
        payload["vendorKey"] = vendor_key
        payload["accountKey"] = f"{(str(payload.get('bankName', '')).strip() or 'unknown').lower()}:{str(payload.get('accountSuffix', '')).strip() or 'unknown'}"
        payload["accountLabel"] = f"{str(payload.get('bankName', '')).strip() or 'Unknown bank'} A/C x{str(payload.get('accountSuffix', '')).strip() or 'Unknown'}"
        payload["year"] = stamp.year if stamp else int(payload.get("year", 0) or 0)
        payload["month"] = stamp.month if stamp else int(payload.get("month", 0) or 0)
        payload["monthLabel"] = stamp.strftime("%b %Y") if stamp else ""
        payload["dayLabel"] = stamp.strftime("%d %b %Y %H:%M") if stamp else str(payload.get("timestamp", ""))
        payload["dayOnlyLabel"] = stamp.strftime("%d %b") if stamp else ""
        payload["timeLabel"] = stamp.strftime("%H:%M") if stamp else ""
        payload["reference"] = payload.get("transactionId", "") or payload.get("title", "")
        return payload

    def _vendor_identity(self, row: dict[str, Any]) -> tuple[str, str]:
        canonical_vendor = str(row.get("canonicalVendor", "")).strip() or str(row.get("resolvedVendor", "")).strip() or str(row.get("vendor", "")).strip()
        alias_key = str(row.get("aliasKey", "")).strip()
        if not alias_key:
            alias_source = str(row.get("canonicalAlias", "")).strip() or canonical_vendor
            alias_key = self._normalize_alias_key(alias_source)
        if canonical_vendor and alias_key:
            return canonical_vendor, alias_key

        raw_value = (
            canonical_vendor
            or str(row.get("counterparty", "")).strip()
            or str(row.get("rawCounterparty", "")).strip()
            or str(row.get("bankName", "Unknown")).strip()
            or "Unknown"
        )
        tokens     = self._vendor_tokens(raw_value)
        normalized = "".join(tokens)
        if not normalized:
            return "Unknown", "unknown"
        alias_map = {
            "cred": "Cred",
            "credclub": "Cred",
            "credccbp": "Cred",
            "bigbasket": "BigBasket",
            "bbnow": "BigBasket",
            "blinkit": "Blinkit",
            "swiggy": "Swiggy",
            "zomato": "Zomato",
        }
        for prefix, display in alias_map.items():
            if normalized.startswith(prefix):
                return display, prefix
        display = " ".join(token.capitalize() for token in tokens) if tokens else (raw_value.strip() or "Unknown")
        return display, normalized

    def _normalize_alias_key(self, value: str) -> str:
        """Normalize one alias label into the stable aggregation key format."""

        return "".join(ch.lower() for ch in str(value or "").strip() if ch.isalnum() or ch in {"@", ".", "_", "-"})

    def _normalize_vendor_token(self, raw_value: str) -> str:
        return "".join(self._vendor_tokens(raw_value))

    def _vendor_tokens(self, raw_value: str) -> list[str]:
        value = raw_value.strip().lower()
        if not value:
            return []
        if "@" in value:
            value = value.split("@", 1)[0]
        value     = re.sub(r"[^a-z0-9]+", " ", value)
        removable = {
            "upi",
            "bank",
            "axis",
            "axisbank",
            "icici",
            "yesbank",
            "sbi",
            "ybl",
            "ibl",
            "okaxis",
            "oksbi",
            "okhdfcbank",
            "okicici",
            "okyesbank",
        }
        return [token for token in value.split() if token and token not in removable]

    def _amount(self, row: dict[str, Any]) -> float:
        try:
            return float(row.get("amount", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _average(self, rows: list[dict[str, Any]]) -> float:
        if not rows:
            return 0.0
        return sum(self._amount(row) for row in rows) / len(rows)

    def _direction(self, row: dict[str, Any]) -> str:
        return str(row.get("direction", "")).strip().lower()

    def _year(self, row: dict[str, Any]) -> int:
        try:
            return int(row.get("year", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _month(self, row: dict[str, Any]) -> int:
        try:
            return int(row.get("month", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _timestamp(self, row: dict[str, Any]) -> datetime | None:
        value = str(row.get("timestamp", "")).strip()
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _sorted_rows_desc(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            rows,
            key=lambda row: (
                self._timestamp(row) or datetime.min,
                str(row.get("reference", "") or ""),
            ),
            reverse=True,
        )

    def _month_title(self, prefix: str) -> str:
        month_label = datetime(self.selected_year, max(1, self.selected_month), 1).strftime("%b %Y") if self.selected_month > 0 else f"All Months {self.selected_year}"
        return f"{prefix} | {month_label}"

    def _popup_stamp_label(self, row: dict[str, Any]) -> str:
        stamp = self._timestamp(row)
        if stamp is None:
            day_only = str(row.get("dayOnlyLabel", "")).strip()
            time_only = str(row.get("timeLabel", "")).strip()
            return " ".join(part for part in [day_only, time_only] if part) or "-"
        return stamp.strftime("%d %b %H:%M")

    def _format_inr(self, amount: float) -> str:
        currency = str(self.selected_currency or self.widget_data.get("meta", {}).get("currency", "INR")).strip().upper() or "INR"
        return f"{currency} {amount:,.0f}" if abs(amount) >= 1 else f"{currency} {amount:,.2f}"

    def _format_signed_inr(self, amount: float) -> str:
        return f"+ {self._format_inr(amount)}" if amount >= 0 else f"- {self._format_inr(abs(amount))}"

    def _format_short_dt(self, value: str) -> str:
        stamp = self._timestamp({"timestamp": value})
        if stamp is None:
            return value or "-"
        return stamp.strftime("%d %b %Y")

    def _adaptive_columns(self, *, base_width: int, minimum: int, maximum: int) -> int:
        available = max(base_width * minimum, self.width() - 80)
        columns   = max(minimum, available // max(base_width, 1))
        return max(minimum, min(maximum, columns))

    def _active_month_recurrence(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        active_vendor_keys = {str(row.get("vendorKey", "")).strip() for row in rows if str(row.get("vendorKey", "")).strip()}
        patterns = [
            dict(item)
            for item in self.widget_data.get("recurringPatterns", [])
            if str(item.get("vendorKey", "")).strip() in active_vendor_keys
        ]
        if not patterns:
            return {}
        strongest = sorted(
            patterns,
            key=lambda item: (
                str(item.get("cadence", "")) != "Monthly recurring",
                -float(item.get("averageAmount", 0.0) or 0.0),
                -int(item.get("transactionCount", 0) or 0),
            ),
        )[0]
        monthly_like = sum(float(item.get("averageAmount", 0.0) or 0.0) for item in patterns if str(item.get("cadence", "")).strip() == "Monthly recurring")
        return {
            "vendor"           : str(strongest.get("vendor", "Unknown")),
            "cadence"          : str(strongest.get("cadence", "")),
            "averageAmount"    : float(strongest.get("averageAmount", 0.0) or 0.0),
            "monthlyLikeAmount": monthly_like,
            "activeCount"      : len({str(item.get("vendorKey", "")).strip() for item in patterns}),
        }

    def _combo_style(self) -> str:
        return f"""
        QComboBox {{
            color: {self.theme.hex("text_primary")};
            background-color: {self._ui_color("panel_alt")};
            border: 1px solid {self._ui_color("divider")};
            border-radius: 0px;
            padding: 6px 10px;
            min-height: 18px;
        }}
        QComboBox:hover {{
            background-color: {self.theme.hex("surface_hover", self._ui_color("panel_alt"))};
            border-color: {self._ui_color("focus")};
        }}
        QComboBox:focus {{
            background-color: {self.theme.hex("surface_hover", self._ui_color("panel_alt"))};
            border-color: {self._ui_color("focus")};
        }}
        QComboBox:disabled {{
            color: {self._ui_color("disabled")};
            background-color: {self._ui_color("disabled_surface")};
            border-color: {self._ui_color("divider")};
        }}
        """

    def _input_style(self) -> str:
        return f"""
        QLineEdit {{
            color: {self.theme.hex("text_primary")};
            background-color: {self._ui_color("panel_alt")};
            border: 1px solid {self._ui_color("divider")};
            border-radius: 0px;
            padding: 6px 10px;
            min-height: 18px;
        }}
        QLineEdit:hover {{
            background-color: {self.theme.hex("surface_hover", self._ui_color("panel_alt"))};
            border-color: {self._ui_color("focus")};
        }}
        QLineEdit:focus {{
            background-color: {self.theme.hex("surface_hover", self._ui_color("panel_alt"))};
            border-color: {self._ui_color("focus")};
        }}
        QLineEdit:disabled {{
            color: {self._ui_color("disabled")};
            background-color: {self._ui_color("disabled_surface")};
            border-color: {self._ui_color("divider")};
        }}
        """

