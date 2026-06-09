from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from src.app.ui_kit.controls import CardFrame, make_label, ui_kit_hex


class SectionCard(CardFrame):
    """Provide a shared titled container used by expense-manager widgets."""

    def __init__(self, title: str, subtitle: str, *, bg: str, border: str, parent: QWidget | None = None) -> None:
        """Render the section header and outer content layout."""

        super().__init__(bg=bg, border=border, radius=0)
        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(12, 12, 12, 12)
        self.outer_layout.setSpacing(10)

        self.header = QWidget()
        self.header.setObjectName("sectionCardHeader")
        self.header.setStyleSheet(
            f"""
            QWidget#sectionCardHeader {{
                background-color: {ui_kit_hex('surface_panel_alt')};
                border: 1px solid {ui_kit_hex('divider', ui_kit_hex('border'))};
            }}
            """
        )
        self.header_layout = QVBoxLayout(self.header)
        self.header_layout.setContentsMargins(14, 12, 14, 12)
        self.header_layout.setSpacing(8)

        self.title_block = QWidget()
        title_block_layout = QVBoxLayout(self.title_block)
        title_block_layout.setContentsMargins(0, 0, 0, 0)
        title_block_layout.setSpacing(2)
        title_label = QLabel(title)
        title_font = QFont(ui_kit_hex("font_family"), 15)
        title_font.setBold(True)
        title_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 98.0)
        title_label.setFont(title_font)
        title_label.setStyleSheet(
            f"QLabel {{ color: {ui_kit_hex('text_secondary')}; background: transparent; border: none; padding: 0px; }}"
        )
        title_block_layout.addWidget(title_label)
        if subtitle:
            subtitle_label = make_label(subtitle, ui_kit_hex("text_muted", ui_kit_hex("text_secondary")), 8, False)
            subtitle_label.setContentsMargins(0, 0, 0, 0)
            title_block_layout.addWidget(subtitle_label)

        self.header_aux_widget: QWidget | None = None
        self.header_aux_breakpoint = 0
        self._reflow_header()
        self.outer_layout.addWidget(self.header)

    def set_header_aux_widget(self, widget: QWidget | None, *, breakpoint: int = 0) -> None:
        """Attach one optional widget to the section heading, with responsive stacking."""

        previous = self.header_aux_widget
        self.header_aux_widget = widget
        self.header_aux_breakpoint = max(0, int(breakpoint))
        if previous is not None and previous is not widget:
            previous.setParent(None)
            previous.deleteLater()
        self._reflow_header()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow_header()

    def add_content_widget(self, widget: QWidget) -> None:
        """Append one widget to the shared section body."""

        self.outer_layout.addWidget(widget)

    def _clear_layout(self, layout: QVBoxLayout | QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            if child_layout is not None:
                self._clear_layout(child_layout)
                continue
            child = item.widget()
            if child is None:
                continue
            if child in {self.title_block, self.header_aux_widget}:
                child.setParent(self.header)
            else:
                child.deleteLater()

    def _reflow_header(self) -> None:
        self._clear_layout(self.header_layout)

        aux = self.header_aux_widget
        compact = aux is not None and self.header_aux_breakpoint > 0 and self.width() < self.header_aux_breakpoint
        if aux is not None and hasattr(aux, "set_header_compact_mode"):
            aux.set_header_compact_mode(compact)

        if aux is None:
            self.header_layout.addWidget(self.title_block)
            return

        if compact:
            self.header_layout.addWidget(self.title_block)
            self.header_layout.addWidget(aux)
            return

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        row.addWidget(self.title_block, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        row.addWidget(aux, 0, Qt.AlignmentFlag.AlignVCenter)
        self.header_layout.addLayout(row)


__all__ = ["SectionCard"]
