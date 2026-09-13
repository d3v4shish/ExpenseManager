from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QSizePolicy, QVBoxLayout, QWidget

from src.app.ui_kit import BaseWidget, CardFrame, Dot, PillLabel, ThemedMessageBox, make_button


class ExpensesOverviewWidget(BaseWidget):
    """Render the compressed terminal-style overview surface."""

    GRID_CELL_HEIGHT = 60

    def __init__(self, widget_config, widget_data, theme, screen_api, window_api, parent=None):
        """Store state and mount the overview layout."""

        super().__init__(widget_config, widget_data, theme, screen_api, window_api, parent)
        self.rebuild_running = False
        self.rebuild_job_label = ""
        self._compact_layout = False
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(0)
        self._render()

    def _render(self) -> None:
        """Render the overview card content."""

        self.clear_layout(self.root)
        self.root.addWidget(self._build_bento_surface())

    def _build_bento_surface(self) -> QWidget:
        """Build the rigid 6-column overview matrix."""

        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        summary = self.widget_data.get("summary", {})
        meta = self.widget_data.get("meta", {})
        top_vendor = self.widget_data.get("topVendor", {})
        recent = self.widget_data.get("recent", [])
        compact = self.width() > 0 and self.width() < 960

        if compact:
            grid.addWidget(self._build_metric_cell("Month total", self._format_overview_total("month"), accent=self._tone("blue"), right=True, bottom=True), 0, 0)
            grid.addWidget(self._build_metric_cell("Year to date", self._format_overview_total("year"), accent=self._tone("amber"), right=False, bottom=True), 0, 1)
            grid.addWidget(self._build_metric_cell("Ignored / New insights", f"{int(meta.get('ignoredCount', 0) or 0):03d} / {self._unread_insight_count():03d}", accent=self._tone("rose"), right=True, bottom=True), 1, 0)
            grid.addWidget(self._build_status_cell(right=False, bottom=True), 1, 1)
            grid.addWidget(self._build_concentration_cell(top_vendor), 2, 0, 1, 2)
            grid.addWidget(self._build_trend_cell(recent), 3, 0, 1, 2)
            for row in range(4):
                grid.setRowMinimumHeight(row, self.GRID_CELL_HEIGHT)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            return host

        grid.addWidget(
            self._build_metric_cell(
                "Month total",
                self._format_overview_total("month"),
                accent=self._tone("blue"),
                right=True,
                bottom=True,
            ),
            0,
            0,
            2,
            2,
        )
        grid.addWidget(
            self._build_metric_cell(
                "Year to date",
                self._format_overview_total("year"),
                accent=self._tone("amber"),
                right=True,
                bottom=True,
            ),
            0,
            2,
            2,
            2,
        )
        grid.addWidget(
            self._build_metric_cell(
                "Ignored / New insights",
                f"{int(meta.get('ignoredCount', 0) or 0):03d} / {self._unread_insight_count():03d}",
                accent=self._tone("rose"),
                right=True,
                bottom=True,
            ),
            0,
            4,
            2,
            1,
        )
        grid.addWidget(self._build_status_cell(right=False, bottom=True), 0, 5, 2, 1)
        grid.addWidget(self._build_concentration_cell(top_vendor), 2, 0, 1, 4)
        grid.addWidget(self._build_trend_cell(recent), 2, 4, 1, 2)

        for row in range(3):
            grid.setRowMinimumHeight(row, self.GRID_CELL_HEIGHT)
        for column in range(6):
            grid.setColumnStretch(column, 1)
        return host

    def resizeEvent(self, event) -> None:  # noqa: N802
        compact = self.width() < 960
        if compact != self._compact_layout:
            self._compact_layout = compact
            QTimer.singleShot(0, self._render)
        super().resizeEvent(event)

    def _build_metric_cell(self, label: str, value: str, *, accent: str, right: bool, bottom: bool) -> QWidget:
        """Render one tall metric tile."""

        cell, content = self._build_cell_shell(
            height=self.GRID_CELL_HEIGHT * 2,
            right=right,
            bottom=bottom,
            accent=accent,
        )
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)
        layout.addWidget(self._make_meta_label(label), 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addStretch(1)
        layout.addWidget(self._make_value_label(value, 24), 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        return cell

    def _build_status_cell(self, *, right: bool, bottom: bool) -> QWidget:
        """Render the status and actions tile."""

        cell, content = self._build_cell_shell(
            height=self.GRID_CELL_HEIGHT * 2,
            right=right,
            bottom=bottom,
        )
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        layout.addWidget(self._make_meta_label("Status"), 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        status_row.addWidget(Dot(self._system_status_color(), 10), 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self._make_status_subtext(self._system_status_text()), 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch(1)
        layout.addLayout(status_row)
        note_text = self._status_note_text()
        if note_text:
            note_label = self._make_note_label(note_text)
            note_label.setWordWrap(True)
            layout.addWidget(note_label, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)

        reload_button = self._make_status_control_button("Reload")
        reload_button.clicked.connect(self._trigger_incremental_reload)
        actions.addWidget(reload_button, 1)

        config_button = self._make_config_toggle_button()
        config_button.clicked.connect(self._toggle_mail_config_pane)
        actions.addWidget(config_button, 1)

        insights_button = self._make_status_control_button("Insights")
        insights_button.clicked.connect(lambda: self.navigate_to("insights"))
        actions.addWidget(insights_button, 1)

        rebuild_button = self._make_rebuild_button()
        rebuild_button.clicked.connect(self._trigger_rebuild)
        actions.addWidget(rebuild_button, 1)

        layout.addLayout(actions)
        return cell

    def _build_concentration_cell(self, top_vendor: dict) -> QWidget:
        """Render the top-vendor concentration strip."""

        cell, content = self._build_cell_shell(
            height=self.GRID_CELL_HEIGHT,
            right=True,
            bottom=False,
        )
        row = QHBoxLayout(content)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(14)

        vendor_name = self._truncate(str(top_vendor.get("vendor", "No top vendor")), 28)
        vendor_amount = self._format_inr(float(top_vendor.get("amount", 0.0) or 0.0), str(top_vendor.get("currency", "INR")))

        heading = QVBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(2)
        heading.addWidget(self._make_meta_label("Top vendor"))
        row.addLayout(heading, 2)
        row.addStretch(1)
        values = QVBoxLayout()
        values.setContentsMargins(0, 0, 0, 0)
        values.setSpacing(2)
        values.addWidget(self._make_value_label(vendor_name, 15), 0, Qt.AlignmentFlag.AlignRight)
        values.addWidget(self._make_value_label(vendor_amount, 12, color=self.theme.hex("blue")), 0, Qt.AlignmentFlag.AlignRight)
        row.addLayout(values, 2)
        return cell

    def _build_trend_cell(self, recent: list[dict]) -> QWidget:
        """Render the recent-vector strip."""

        cell, content = self._build_cell_shell(
            height=self.GRID_CELL_HEIGHT,
            right=False,
            bottom=False,
        )
        row = QHBoxLayout(content)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(12)
        labels = QVBoxLayout()
        labels.setContentsMargins(0, 0, 0, 0)
        labels.setSpacing(2)
        labels.addWidget(self._make_meta_label("Trend"))
        row.addLayout(labels, 2)
        row.addStretch(1)

        track = QFrame()
        track.setFixedSize(170, 10)
        track.setStyleSheet(
            f"QFrame {{ background-color: {self.theme.hex('surface_panel_alt')}; border: 1px solid {self.theme.hex('divider', self.theme.hex('border'))}; border-radius: 0px; }}"
        )
        track_layout = QHBoxLayout(track)
        track_layout.setContentsMargins(0, 0, 0, 0)
        track_layout.setSpacing(0)

        fill = QFrame()
        fill_percent = self._trend_fill_percent(recent)
        fill.setStyleSheet(f"QFrame {{ background-color: {self.theme.hex('blue')}; border: none; border-radius: 0px; }}")
        track_layout.addWidget(fill, fill_percent)
        track_layout.addStretch(max(1, 100 - fill_percent))
        row.addWidget(track, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._make_value_label(f"{fill_percent:02d}%" if fill_percent else "—", 11, color=self.theme.hex("text_secondary")), 0, Qt.AlignmentFlag.AlignVCenter)
        return cell

    def _build_cell_shell(
        self,
        *,
        height: int,
        right: bool,
        bottom: bool,
        accent: str | None = None,
    ) -> tuple[QFrame, QWidget]:
        """Build one shared-border cell and return its content host."""

        _ = (right, bottom)
        cell = CardFrame(
            bg=self.theme.hex("surface_panel"),
            border=self.theme.hex("divider", self.theme.hex("border")),
            glow=accent,
            radius=0,
        )
        cell.setFixedHeight(height)

        shell = QHBoxLayout(cell)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        content = QWidget()
        content.setObjectName("expenseOverviewContent")
        content.setStyleSheet("QWidget#expenseOverviewContent { background: transparent; border: none; }")
        shell.addWidget(content, 1)
        return cell, content

    def _make_meta_label(self, text: str) -> QLabel:
        """Create the rigid uppercase label style."""

        label = QLabel(text.upper())
        label.setFont(self._mono_font(8, bold=False, letter_spacing=1.1))
        label.setStyleSheet(f"QLabel {{ color: {self.theme.hex('text_secondary')}; background: transparent; border: none; padding: 0px; }}")
        return label

    def _make_value_label(self, text: str, size: int, *, color: str | None = None) -> QLabel:
        """Create the primary value label."""

        label = QLabel(text)
        label.setFont(self._mono_font(size, bold=True))
        label.setStyleSheet(
            f"QLabel {{ color: {color or self.theme.hex('text_primary')}; background: transparent; border: none; padding: 0px; }}"
        )
        return label

    def _make_note_label(self, text: str) -> QLabel:
        """Create a compact muted note label."""

        label = QLabel(text)
        label.setWordWrap(True)
        label.setFont(self._mono_font(8, bold=False))
        label.setStyleSheet(f"QLabel {{ color: {self.theme.hex('text_muted')}; background: transparent; border: none; padding: 0px; }}")
        return label

    def _make_status_subtext(self, text: str) -> QLabel:
        """Create the small colored status line shown under the section label."""

        label = QLabel(text)
        label.setFont(self._mono_font(9, bold=True))
        label.setStyleSheet(
            f"QLabel {{ color: {self._system_status_color()}; background: transparent; border: none; padding: 0px; }}"
        )
        return label

    def _make_action_button(self, text: str, *, primary: bool):
        """Create one rigid action button."""

        if primary:
            button = make_button(
                text.upper(),
                self.theme.hex("accent"),
                self.theme.hex("text_primary"),
                self.theme.hex("accent"),
                emphasis="primary",
            )
        else:
            button = make_button(
                text.upper(),
                self.theme.hex("divider", self.theme.hex("border")),
                self.theme.hex("text_primary"),
                self.theme.hex("surface_panel_alt"),
            )
        button.setMinimumHeight(36)
        return button

    def _make_status_control_button(self, text: str):
        """Create one compact status-row control button."""

        button = make_button(
            text.upper(),
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setMinimumHeight(30)
        button.setMaximumHeight(30)
        button.setMinimumWidth(0)
        font = button.font()
        font.setPointSize(9)
        button.setFont(font)
        button.setStyleSheet(
            button.styleSheet()
            + f"""
            QPushButton {{
                border: 1px solid {self.theme.hex("divider", self.theme.hex("border"))};
                min-height: 30px;
                padding: 4px 8px;
            }}
            """
        )
        return button

    def _make_rebuild_button(self):
        """Create the destructive rebuild action for the status tile."""

        divider = self.theme.hex("divider", self.theme.hex("border"))
        accent = QColor(self.theme.hex("rose")).darker(135).name()
        button = make_button(
            "REBUILD",
            divider,
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setMinimumHeight(30)
        button.setMaximumHeight(30)
        button.setMinimumWidth(0)
        font = button.font()
        font.setPointSize(9)
        button.setFont(font)
        button.setStyleSheet(
            button.styleSheet()
            + f"""
            QPushButton {{
                border-top: 1px solid {divider};
                border-right: 1px solid {divider};
                border-bottom: 1px solid {divider};
                border-left: 2px solid {accent};
                min-height: 30px;
                padding: 4px 8px;
            }}
            QPushButton:hover {{
                border-left: 2px solid {accent};
            }}
            QPushButton:pressed {{
                border-left: 2px solid {accent};
            }}
            """
        )
        return button

    def _make_config_toggle_button(self):
        """Create the compact trigger for the mail-configuration popup."""

        button = make_button(
            "MAIL CFG",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setMinimumHeight(30)
        button.setMaximumHeight(30)
        button.setMinimumWidth(0)
        font = button.font()
        font.setPointSize(9)
        button.setFont(font)
        border_color = self.theme.hex("divider", self.theme.hex("border"))
        button.setStyleSheet(
            button.styleSheet()
            + f"""
            QPushButton {{
                border: 1px solid {border_color};
                min-height: 30px;
                padding: 4px 8px;
            }}
            """
        )
        button.setToolTip("Open Thunderbird mail configuration without moving the dashboard.")
        return button

    def _mono_font(self, size: int, *, bold: bool, letter_spacing: float = 0.0) -> QFont:
        """Build the shared JetBrains Mono font variant."""

        font = QFont(self.theme.mono_font_family(), pointSize=size)
        font.setBold(bold)
        if letter_spacing:
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
        return font

    def _system_status_text(self) -> str:
        """Return the current status token."""

        if self.rebuild_running:
            return "Syncing"
        if (self.rebuild_job_label or "").strip().lower() == "refresh failed":
            return "Refresh failed"
        if not bool(self.widget_data.get("meta", {}).get("sourceReady", True)):
            return "Setup needed"
        return "Ready"

    def _system_status_color(self) -> str:
        """Return the status dot color."""

        if self.rebuild_running:
            return self.theme.hex("warning")
        if (self.rebuild_job_label or "").strip().lower() == "refresh failed":
            return self.theme.hex("rose")
        if not bool(self.widget_data.get("meta", {}).get("sourceReady", True)):
            return self.theme.hex("amber")
        return self.theme.hex("emerald")

    def _status_note_text(self) -> str:
        """Return one short operator-facing note below the status pill."""

        if self.rebuild_running:
            return self.rebuild_job_label or "Mailbox and ledger are updating."
        if (self.rebuild_job_label or "").strip().lower() == "refresh failed":
            return "Use Rebuild to refresh the ledger."
        if not bool(self.widget_data.get("meta", {}).get("sourceReady", True)):
            return str(self.widget_data.get("meta", {}).get("sourceMessage", "Configure a Thunderbird account to start syncing."))
        return ""

    def _trend_fill_percent(self, recent: list[dict]) -> int:
        """Return the compact trend fill percentage."""

        if not recent:
            return 0
        amounts = [float(item.get("amount", 0.0) or 0.0) for item in recent[:5]]
        peak = max(amounts) or 1.0
        latest = amounts[0] if amounts else 0.0
        return max(12, min(100, int((latest / peak) * 100)))

    def _truncate(self, value: str, limit: int) -> str:
        """Trim long identifiers to keep the strip stable."""

        text = (value or "").strip() or "-"
        return text if len(text) <= limit else f"{text[: limit - 3]}..."

    def _format_metric_number(self, value: float) -> str:
        """Format a top-line metric without the currency prefix."""

        return f"{int(round(value)):,}"

    def _trigger_rebuild(self) -> None:
        """Ask the screen API to rebuild the expenses projection."""

        confirm = ThemedMessageBox(self.theme, self)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setWindowTitle("Confirm Rebuild")
        confirm.setText("Rebuild expenses from all configured emails?")
        confirm.setInformativeText("This rescans mailboxes and rebuilds the active expense database.")
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirm.set_destructive_buttons(QMessageBox.StandardButton.Yes)
        if confirm.exec() != int(QMessageBox.StandardButton.Yes):
            return
        self.screen_api.run_rebuild()

    def _trigger_incremental_reload(self) -> None:
        """Ask the screen API to run the incremental refresh job."""

        self.screen_api.run_refresh()

    def _toggle_mail_config_pane(self) -> None:
        """Open the Thunderbird account popup without changing dashboard layout."""

        callback = getattr(self.window_api, "open_mail_config_dialog", None)
        if callable(callback):
            callback()
        else:
            self.toggle_card("panel.expenses_config", scroll_into_view=True)

    def _unread_insight_count(self) -> int:
        """Return the lightweight persisted unread count for the overview strip."""

        callback = getattr(self.screen_api, "unread_insight_count", None)
        if not callable(callback):
            return 0
        try:
            return max(0, int(callback() or 0))
        except Exception:  # noqa: BLE001
            self.logger.exception("Could not load unread insight count")
            return 0

    def set_job_state(self, job_id: str, running: bool, payload: dict | None = None) -> None:
        """Update the status label while refresh jobs run."""

        if job_id not in {"expenses.refresh", "expenses.rebuild"}:
            return
        state = ((payload or {}).get("phase", "") or "").strip().lower()
        self.rebuild_running = running
        if running and job_id == "expenses.rebuild":
            self.rebuild_job_label = "Syncing mailbox"
        elif running:
            self.rebuild_job_label = "Refreshing ledger"
        elif state == "failed":
            self.rebuild_job_label = "Refresh failed"
        else:
            self.rebuild_job_label = ""
        self._render()

    def _tone(self, tone: str) -> str:
        """Return the accent color for one semantic tone."""

        styles = {
            "rose": self.theme.hex("rose"),
            "emerald": self.theme.hex("emerald"),
            "blue": self.theme.hex("blue"),
            "amber": self.theme.hex("amber"),
        }
        return styles.get(tone, self.theme.hex("accent"))

    def _format_overview_total(self, period: str) -> str:
        """Show per-currency totals without combining incomparable money values."""

        summaries = self.widget_data.get("summaryByCurrency", {})
        if not isinstance(summaries, dict) or not summaries:
            summary = self.widget_data.get("summary", {})
            return self._format_inr(float(summary.get(period, 0.0) or 0.0), "INR")
        values = [
            self._format_inr(float(metrics.get(period, 0.0) or 0.0), str(currency))
            for currency, metrics in summaries.items()
            if isinstance(metrics, dict)
        ]
        return " · ".join(values) if values else "INR 0"

    def _format_inr(self, value: float, currency: str = "INR") -> str:
        """Format one numeric amount with its actual currency code."""

        code = str(currency or "INR").strip().upper() or "INR"
        return f"{code} {value:,.0f}" if value else f"{code} 0"
