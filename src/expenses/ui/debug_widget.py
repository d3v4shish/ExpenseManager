from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.app.ui_kit import BaseWidget, CardFrame, SectionCard, make_button, make_label
from src.expenses.email.bank_rule_catalog import build_bank_rule_template
from src.expenses.ui.debug_table import CandidateMailTableModel


class _TaskSignals(QObject):
    """Bridge one widget-local background task back to the UI thread."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class _TaskWorker(QRunnable):
    """Run one small screen-api task away from the main UI thread."""

    def __init__(self, fn, *args, **kwargs) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(str(exc))
            return
        self.signals.finished.emit(result)


class ExpensesMailDebugWidget(BaseWidget):
    """Render the candidate-mail debug pane and raw bank-rule editor."""

    def __init__(self, widget_config, widget_data, theme, screen_api, window_api, parent=None):
        super().__init__(widget_config, widget_data, theme, screen_api, window_api, parent)
        self.loading = False
        self.detail_loading = False
        self._detail_request_id = 0
        self.validating = False
        self.saving = False
        self.reparsing = False
        self.mining = False
        self.expenses_job_running = False
        self.summary: dict[str, Any] = {}
        self.rows: list[dict[str, Any]] = []
        self.selected_source_record_id = 0
        self.detail: dict[str, Any] | None = None
        self.defaults_text = ""
        self.overrides_text = ""
        self.effective_text = ""
        self.script_extractors: dict[str, Any] = {}
        self.template_suggestions: list[dict[str, Any]] = []
        self.status_message = ""
        self.status_tone = "muted"
        self._build_ui()
        self._refresh_view_state()

    def bind_card_ref(self, card_ref: str) -> None:
        """Bind the card id and lazy-load the debug data when visible."""

        super().bind_card_ref(card_ref)
        if self.is_card_visible():
            QTimer.singleShot(0, self._load_snapshot)

    def apply_reload(self, widget_config: dict[str, Any], widget_data: dict[str, Any]) -> bool:
        """Refresh the debug pane in place and reload data when visible."""

        self.widget_config = widget_config
        self.widget_data = widget_data
        self.datasource_file = self._describe_datasource(widget_config)
        if self.is_card_visible() and not self.loading:
            QTimer.singleShot(0, self._load_snapshot)
        return True

    def on_card_visibility_changed(self, visible: bool) -> None:
        """Start the expensive debug load only when the pane becomes visible."""

        if visible:
            QTimer.singleShot(0, self._load_snapshot)

    def set_job_state(self, job_id: str, running: bool, payload: dict | None = None) -> None:
        """Disable debug mutations while expense sync jobs run."""

        if job_id not in {"expenses.refresh", "expenses.rebuild"}:
            return
        self.expenses_job_running = running
        phase = str((payload or {}).get("phase", "")).strip().lower()
        if running:
            self.status_message = "Expenses sync is running; debug edits are temporarily disabled."
            self.status_tone = "info"
        elif phase == "failed":
            self.status_message = "Expenses sync failed."
            self.status_tone = "error"
        else:
            self.status_message = ""
            self.status_tone = "muted"
        self._refresh_view_state()

    def _build_ui(self) -> None:
        """Build the fixed debug-pane layout once."""

        self.setStyleSheet(
            f"""
            QWidget {{
                color: {self.theme.hex("text_primary")};
            }}
            QComboBox, QPlainTextEdit, QTableView {{
                color: {self.theme.hex("text_primary")};
                background-color: {self.theme.hex("surface_panel_alt")};
                border: 1px solid {self.theme.hex("divider", self.theme.hex("border"))};
                selection-background-color: {self.theme.hex("surface_active", self.theme.hex("accent"))};
            }}
            QComboBox {{
                min-height: 30px;
                padding: 4px 8px;
            }}
            QPlainTextEdit {{
                padding: 8px;
                font-family: {self.theme.mono_font_family()};
                font-size: 11px;
            }}
            QHeaderView::section {{
                background-color: {self.theme.hex("surface_panel")};
                color: {self.theme.hex("text_secondary")};
                border: none;
                border-bottom: 1px solid {self.theme.hex("divider", self.theme.hex("border"))};
                padding: 6px 8px;
                font-weight: 600;
            }}
            """
        )

        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(0)

        card = SectionCard(
            str(self.widget_config.get("title", "Mail Debug")).strip() or "Mail Debug",
            "Inspect matched candidate emails, parse failures, and raw bank-rule config.",
            bg=self.theme.hex("surface_panel"),
            border=self.theme.hex("divider", self.theme.hex("border")),
        )

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(4, 4, 4, 4)
        body_layout.setSpacing(10)

        summary_row = QGridLayout()
        summary_row.setContentsMargins(0, 0, 0, 0)
        summary_row.setHorizontalSpacing(8)
        summary_row.setVerticalSpacing(8)
        self.total_value = self._summary_card(summary_row, 0, "Candidates", "0")
        self.parsed_value = self._summary_card(summary_row, 1, "Parsed", "0")
        self.failed_value = self._summary_card(summary_row, 2, "Failed", "0")
        self.rule_miss_value = self._summary_card(summary_row, 3, "Needs Rule", "0")
        self.checkpoint_value = self._summary_card(summary_row, 4, "Last Candidate", "Never")
        body_layout.addLayout(summary_row)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        body_layout.addWidget(self.banner)

        top_actions = QHBoxLayout()
        top_actions.setContentsMargins(0, 0, 0, 0)
        top_actions.setSpacing(8)

        self.filter_combo = QComboBox()
        self.filter_combo.addItem("All Candidates", "all")
        self.filter_combo.addItem("Failed Parses", "failed")
        self.filter_combo.addItem("Needs Rule", "rule_miss")
        self.filter_combo.addItem("Parsed", "parsed")
        self.filter_combo.currentIndexChanged.connect(self._apply_row_filter)
        top_actions.addWidget(self.filter_combo, 0)

        top_actions.addStretch(1)

        self.reload_button = make_button(
            "Reload",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.reload_button.clicked.connect(self._load_snapshot)
        top_actions.addWidget(self.reload_button)

        self.reparse_button = make_button(
            "Reparse Candidates",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.reparse_button.setToolTip("Re-run the active rules against every stored candidate email.")
        self.reparse_button.clicked.connect(self._reparse_candidates)
        top_actions.addWidget(self.reparse_button)

        self.mine_templates_button = make_button(
            "Mine Templates",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.mine_templates_button.setToolTip("Cluster stored email candidates into masked, review-only message templates.")
        self.mine_templates_button.clicked.connect(self._mine_templates)
        top_actions.addWidget(self.mine_templates_button)
        body_layout.addLayout(top_actions)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(make_label("Candidate Mail", self.theme.hex("text_secondary"), 9, True))

        self.table_model = CandidateMailTableModel(
            format_timestamp=self._format_timestamp,
            status_colors={
                "parsed": QColor(self.theme.hex("emerald")),
                "failed": QColor(self.theme.hex("amber")),
                "matched": QColor(self.theme.hex("text_secondary")),
            },
            parent=self,
        )
        self.table = QTableView()
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.selectionModel().selectionChanged.connect(self._handle_table_selection_changed)
        left_layout.addWidget(self.table, 1)
        splitter.addWidget(left_panel)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setChildrenCollapsible(False)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(make_label("Selected Candidate", self.theme.hex("text_secondary"), 9, True))

        self.detail_meta = QLabel("Select one candidate email.")
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setStyleSheet(
            f"QLabel {{ color: {self.theme.hex('text_primary')}; background: transparent; border: none; }}"
        )
        detail_layout.addWidget(self.detail_meta)

        self.detail_body = QPlainTextEdit()
        self.detail_body.setReadOnly(True)
        self.detail_body.setPlaceholderText("Raw text body will appear here.")
        detail_layout.addWidget(self.detail_body, 2)

        self.detail_result = QPlainTextEdit()
        self.detail_result.setReadOnly(True)
        self.detail_result.setPlaceholderText("Parse attempts and extracted facts will appear here.")
        detail_layout.addWidget(self.detail_result, 2)
        right_splitter.addWidget(detail_panel)

        rule_panel = QWidget()
        rule_layout = QVBoxLayout(rule_panel)
        rule_layout.setContentsMargins(0, 0, 0, 0)
        rule_layout.setSpacing(8)
        rule_layout.addWidget(make_label("Raw Rule Config", self.theme.hex("text_secondary"), 9, True))

        rule_actions = QHBoxLayout()
        rule_actions.setContentsMargins(0, 0, 0, 0)
        rule_actions.setSpacing(8)
        self.validate_button = make_button(
            "Validate On Selected Mail",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.validate_button.setToolTip("Run the edited override JSON against the selected candidate email without saving.")
        self.validate_button.clicked.connect(self._validate_selected_rule_text)
        rule_actions.addWidget(self.validate_button)

        self.add_bank_button = make_button(
            "Add Bank Template",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.add_bank_button.setToolTip("Append a new regex-based bank rule template to the local override JSON.")
        self.add_bank_button.clicked.connect(self._insert_bank_template)
        rule_actions.addWidget(self.add_bank_button)

        self.mined_template_combo = QComboBox()
        self.mined_template_combo.setMinimumWidth(260)
        self.mined_template_combo.setToolTip("Select a mined template before inserting its editable rule draft.")
        self.mined_template_combo.addItem("Mine templates to create a draft", "")
        rule_actions.addWidget(self.mined_template_combo, 1)

        self.add_mined_button = make_button(
            "Insert Mined Draft",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.add_mined_button.setToolTip("Insert the selected mined template as an editable, unsaved rule draft.")
        self.add_mined_button.clicked.connect(self._insert_mined_template)
        rule_actions.addWidget(self.add_mined_button)

        self.save_button = make_button(
            "Save Overrides",
            self.theme.hex("accent"),
            self.theme.hex("text_primary"),
            self.theme.hex("accent"),
            emphasis="primary",
        )
        self.save_button.setToolTip("Persist the override JSON, then reparse the stored candidate emails for the active account.")
        self.save_button.clicked.connect(self._save_override_text)
        rule_actions.addWidget(self.save_button)
        rule_actions.addStretch(1)
        rule_layout.addLayout(rule_actions)

        rule_splitter = QSplitter(Qt.Orientation.Vertical)
        rule_splitter.setChildrenCollapsible(False)
        self.defaults_editor = self._make_rule_editor(read_only=True, placeholder="Shipped default rules.")
        self.overrides_editor = self._make_rule_editor(read_only=False, placeholder="Editable local override JSON.")
        self.effective_editor = self._make_rule_editor(read_only=True, placeholder="Effective merged rules.")
        rule_splitter.addWidget(self._editor_panel("Shipped defaults", self.defaults_editor))
        rule_splitter.addWidget(self._editor_panel("Local overrides", self.overrides_editor))
        rule_splitter.addWidget(self._editor_panel("Effective result", self.effective_editor))
        rule_layout.addWidget(rule_splitter, 1)
        right_splitter.addWidget(rule_panel)

        splitter.addWidget(right_splitter)
        splitter.setSizes([560, 760])
        right_splitter.setSizes([360, 420])
        body_layout.addWidget(splitter, 1)

        card.add_content_widget(body)
        self.root.addWidget(card)

    def _summary_card(self, layout: QGridLayout, column: int, label: str, initial_value: str) -> QLabel:
        """Create one compact summary card and return its value label."""

        frame = CardFrame(
            bg=self.theme.hex("surface_panel_alt"),
            border=self.theme.hex("divider", self.theme.hex("border")),
            radius=0,
        )
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(12, 10, 12, 10)
        inner.setSpacing(4)
        inner.addWidget(make_label(label.upper(), self.theme.hex("text_secondary"), 8, False, mono=True))
        value = QLabel(initial_value)
        value.setStyleSheet(
            f"QLabel {{ color: {self.theme.hex('text_primary')}; background: transparent; border: none; }}"
        )
        inner.addWidget(value)
        layout.addWidget(frame, 0, column)
        layout.setColumnStretch(column, 1)
        return value

    def _make_rule_editor(self, *, read_only: bool, placeholder: str) -> QPlainTextEdit:
        """Create one shared raw JSON editor."""

        editor = QPlainTextEdit()
        editor.setReadOnly(read_only)
        editor.setPlaceholderText(placeholder)
        return editor

    def _editor_panel(self, title: str, editor: QPlainTextEdit) -> QWidget:
        """Wrap one editor in a labeled panel."""

        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(make_label(title, self.theme.hex("text_secondary"), 8, True))
        layout.addWidget(editor, 1)
        return host

    def _load_snapshot(self) -> None:
        """Load the latest candidate-mail rows and rule config in the background."""

        if self.loading or not self.is_card_visible():
            return
        self.loading = True
        self.status_message = "Loading candidate mail debug data..."
        self.status_tone = "info"
        self._refresh_view_state()
        worker = _TaskWorker(self.screen_api.get_mail_debug_snapshot)
        worker.signals.finished.connect(self._handle_snapshot_success)
        worker.signals.failed.connect(self._handle_snapshot_failed)
        self.window_api.thread_pool.start(worker)

    def _handle_snapshot_success(self, result: object) -> None:
        """Apply one completed debug snapshot."""

        self.loading = False
        payload = dict(result) if isinstance(result, dict) else {}
        self.summary = dict(payload.get("summary", {})) if isinstance(payload.get("summary", {}), dict) else {}
        self.rows = [dict(item) for item in payload.get("rows", []) if isinstance(item, dict)]
        self.defaults_text = str(payload.get("defaultsText", "")).rstrip() + ("\n" if str(payload.get("defaultsText", "")).strip() else "")
        self.overrides_text = str(payload.get("overridesText", "")).rstrip() + ("\n" if str(payload.get("overridesText", "")).strip() else "")
        self.effective_text = str(payload.get("effectiveText", "")).rstrip() + ("\n" if str(payload.get("effectiveText", "")).strip() else "")
        self.script_extractors = dict(payload.get("scriptExtractors", {})) if isinstance(payload.get("scriptExtractors", {}), dict) else {}
        self.defaults_editor.setPlainText(self.defaults_text)
        self.overrides_editor.setPlainText(self.overrides_text)
        self.effective_editor.setPlainText(self.effective_text)
        if not self.status_message or self.status_tone == "info":
            rule_misses = int(self.summary.get("ruleMissCount", 0) or 0)
            script_errors = len(list(self.script_extractors.get("loadErrors", []))) if isinstance(self.script_extractors.get("loadErrors", []), list) else 0
            script_loaded = int(self.script_extractors.get("loadedCount", 0) or 0)
            script_enabled = bool(self.script_extractors.get("enabled"))
            if script_errors > 0:
                self.status_message = f"Python script extractors have {script_errors:,} load issue(s). Check script_extractors.json and script files."
                self.status_tone = "warning"
            elif rule_misses > 0:
                self.status_message = f"{rule_misses:,} email(s) look like bank alerts but do not match any rule. Use the Needs Rule filter."
                self.status_tone = "warning"
            elif script_enabled:
                self.status_message = f"Python script extractors enabled: {script_loaded:,} script(s) loaded."
                self.status_tone = "muted"
            else:
                self.status_message = ""
                self.status_tone = "muted"
        self._refresh_summary()
        self._apply_row_filter()
        self._refresh_view_state()

    def _handle_snapshot_failed(self, message: str) -> None:
        """Show one failed snapshot load."""

        self.loading = False
        self.status_message = str(message or "Could not load candidate mail debug data.")
        self.status_tone = "error"
        self._refresh_view_state()

    def _apply_row_filter(self) -> None:
        """Render the candidate table from the latest cached row set."""

        filter_value = str(self.filter_combo.currentData() or "all")
        previous_id = self.selected_source_record_id
        self.table_model.set_rows(self.rows)
        self.table_model.set_filter_mode(filter_value)

        target_row = self.table_model.row_for_source_record_id(previous_id)
        if target_row < 0 and self.table_model.rowCount() > 0:
            target_row = 0
        if target_row >= 0:
            self.table.selectRow(target_row)
            self.table.scrollTo(self.table_model.index(target_row, 0))
        else:
            self.selected_source_record_id = 0
            self.detail = None
            self._render_detail()

    def _handle_table_selection_changed(self) -> None:
        """Load the selected candidate detail lazily."""

        selection_model = self.table.selectionModel()
        if selection_model is None or not selection_model.hasSelection():
            self.selected_source_record_id = 0
            self.detail = None
            self._render_detail()
            self._refresh_view_state()
            return
        current_index = self.table.currentIndex()
        source_record_id = self.table_model.source_record_id_at(current_index.row())
        if source_record_id <= 0 or source_record_id == self.selected_source_record_id:
            return
        self.selected_source_record_id = source_record_id
        self._load_selected_detail(source_record_id)

    def _load_selected_detail(self, source_record_id: int) -> None:
        """Load one candidate detail in the background."""

        self.detail_loading = True
        self._detail_request_id += 1
        request_id = self._detail_request_id
        self.detail_meta.setText("Loading selected candidate...")
        self.detail_body.clear()
        self.detail_result.clear()
        self._refresh_view_state()
        worker = _TaskWorker(self.screen_api.get_candidate_mail_detail, source_record_id)
        worker.signals.finished.connect(lambda result, token=request_id, expected=source_record_id: self._handle_detail_success(token, expected, result))
        worker.signals.failed.connect(lambda message, token=request_id: self._handle_detail_failed(token, message))
        self.window_api.thread_pool.start(worker)

    def _handle_detail_success(self, request_id: int, expected_source_record_id: int, result: object) -> None:
        """Apply one completed candidate-detail load."""

        if request_id != self._detail_request_id or expected_source_record_id != self.selected_source_record_id:
            return
        self.detail_loading = False
        self.detail = dict(result) if isinstance(result, dict) else None
        self._render_detail()
        self._refresh_view_state()

    def _handle_detail_failed(self, request_id: int, message: str) -> None:
        """Show one failed candidate-detail load."""

        if request_id != self._detail_request_id:
            return
        self.detail_loading = False
        self.detail = None
        self.detail_meta.setText(str(message or "Could not load candidate detail."))
        self.detail_body.clear()
        self.detail_result.clear()
        self._refresh_view_state()

    def _render_detail(self) -> None:
        """Render the selected candidate email into the detail pane."""

        if not isinstance(self.detail, dict):
            self.detail_meta.setText("Select one candidate email.")
            self.detail_body.clear()
            self.detail_result.clear()
            return

        payload = self.detail.get("payload", {}) if isinstance(self.detail.get("payload", {}), dict) else {}
        attempts = list(self.detail.get("attempts", [])) if isinstance(self.detail.get("attempts", []), list) else []
        facts = list(self.detail.get("facts", [])) if isinstance(self.detail.get("facts", []), list) else []
        first_attempt = attempts[0] if attempts else {}
        guidance = self._detail_guidance(first_attempt)
        self.detail_meta.setText(
            "\n".join(
                [
                    f"{str(self.detail.get('title', '')).strip() or 'Untitled email'}",
                    f"Status: {self._status_label(first_attempt or self.detail)}",
                    f"Rule: {str(first_attempt.get('matchedRuleId', '')).strip() or 'None'}",
                    f"Sender: {str(self.detail.get('sender', '')).strip() or 'Unknown'}",
                    f"Received: {self._format_timestamp(str(self.detail.get('receivedAt', '')).strip())}",
                ]
            )
        )
        body_text = str(payload.get("textBody", "")).strip()
        if not body_text:
            body_text = str(payload.get("htmlBody", "")).strip()
        self.detail_body.setPlainText(body_text)
        self.detail_result.setPlainText(
            json.dumps(
                {
                    "attempts": attempts,
                    "facts": facts,
                    "nextStep": guidance,
                },
                indent=2,
            )
        )
        if guidance:
            self.status_message = guidance
            self.status_tone = "warning"

    def _validate_selected_rule_text(self) -> None:
        """Run the edited override JSON against the selected candidate email."""

        if self.validating or self.saving or self.loading or self.expenses_job_running:
            return
        if self.selected_source_record_id <= 0:
            self.status_message = "Select one candidate email first."
            self.status_tone = "warning"
            self._refresh_view_state()
            return
        self.validating = True
        self.status_message = "Validating override JSON against the selected candidate..."
        self.status_tone = "info"
        self._refresh_view_state()
        worker = _TaskWorker(
            self.screen_api.validate_bank_rule_overrides,
            self.selected_source_record_id,
            self.overrides_editor.toPlainText(),
        )
        worker.signals.finished.connect(self._handle_validate_success)
        worker.signals.failed.connect(self._handle_validate_failed)
        self.window_api.thread_pool.start(worker)

    def _handle_validate_success(self, result: object) -> None:
        """Apply one completed rule validation."""

        self.validating = False
        payload = dict(result) if isinstance(result, dict) else {}
        self.effective_editor.setPlainText(str(payload.get("effectiveText", "")))
        attempts = list(payload.get("attempts", [])) if isinstance(payload.get("attempts", []), list) else []
        facts = list(payload.get("facts", [])) if isinstance(payload.get("facts", []), list) else []
        if bool(payload.get("candidateMatched")) and facts:
            self.status_message = f"Validation parsed {len(facts)} fact(s) for the selected candidate."
            self.status_tone = "success"
        elif bool(payload.get("candidateMatched")) and attempts:
            self.status_message = str(attempts[0].get("reasonText", "Validation still failed for the selected candidate.")).strip()
            self.status_tone = "warning"
        else:
            self.status_message = "The selected candidate no longer matches the effective rules."
            self.status_tone = "warning"
        self._refresh_view_state()

    def _handle_validate_failed(self, message: str) -> None:
        """Show one failed validation result."""

        self.validating = False
        self.status_message = str(message or "Could not validate the override JSON.")
        self.status_tone = "error"
        self._refresh_view_state()

    def _save_override_text(self) -> None:
        """Persist the edited override JSON and reparse stored candidates."""

        if self.saving or self.validating or self.loading or self.expenses_job_running:
            return
        self.saving = True
        self.status_message = "Saving overrides and reparsing stored candidates..."
        self.status_tone = "info"
        self._refresh_view_state()
        worker = _TaskWorker(self.screen_api.save_bank_rule_overrides, self.overrides_editor.toPlainText())
        worker.signals.finished.connect(self._handle_save_success)
        worker.signals.failed.connect(self._handle_save_failed)
        self.window_api.thread_pool.start(worker)

    def _handle_save_success(self, result: object) -> None:
        """Apply one completed save/reparse result."""

        self.saving = False
        payload = dict(result) if isinstance(result, dict) else {}
        self.overrides_editor.setPlainText(str(payload.get("overridesText", "")))
        self.effective_editor.setPlainText(str(payload.get("effectiveText", "")))
        reparse = payload.get("reparse", {}) if isinstance(payload.get("reparse", {}), dict) else {}
        reparsed = int(reparse.get("reparsedRecords", 0) or 0)
        self.status_message = f"Saved overrides and reparsed {reparsed} stored candidates. Rebuild to backfill older history if match scope changed."
        self.status_tone = "success"
        self._refresh_view_state()
        self._load_snapshot()

    def _handle_save_failed(self, message: str) -> None:
        """Show one failed override save."""

        self.saving = False
        self.status_message = str(message or "Could not save the override JSON.")
        self.status_tone = "error"
        self._refresh_view_state()

    def _reparse_candidates(self) -> None:
        """Re-run parsing for every stored candidate email."""

        if self.reparsing or self.saving or self.validating or self.loading or self.expenses_job_running:
            return
        self.reparsing = True
        self.status_message = "Reparsing stored candidate emails..."
        self.status_tone = "info"
        self._refresh_view_state()
        worker = _TaskWorker(self.screen_api.reparse_candidate_mails)
        worker.signals.finished.connect(self._handle_reparse_success)
        worker.signals.failed.connect(self._handle_reparse_failed)
        self.window_api.thread_pool.start(worker)

    def _mine_templates(self) -> None:
        """Mine local message templates on a worker; suggestions never change rules."""

        if self.mining or self.saving or self.validating or self.loading or self.expenses_job_running:
            return
        self.mining = True
        self.status_message = "Mining masked message templates from stored candidate emails..."
        self.status_tone = "info"
        self._refresh_view_state()
        worker = _TaskWorker(self.screen_api.mine_bank_alert_templates)
        worker.signals.finished.connect(self._handle_template_mining_success)
        worker.signals.failed.connect(self._handle_template_mining_failed)
        self.window_api.thread_pool.start(worker)

    def _handle_template_mining_success(self, result: object) -> None:
        """Render template choices without exposing any message body."""

        self.mining = False
        payload = dict(result) if isinstance(result, dict) else {}
        self.template_suggestions = [dict(item) for item in payload.get("templates", []) if isinstance(item, dict)]
        self.mined_template_combo.clear()
        if not self.template_suggestions:
            self.mined_template_combo.addItem("No repeated templates found", "")
            self.status_message = "No repeated templates found yet. Import at least two similar candidate emails, then try again."
            self.status_tone = "muted"
        else:
            for item in self.template_suggestions:
                label = f"{item.get('senderHint', 'unknown')} · {int(item.get('support', 0) or 0):,} messages"
                self.mined_template_combo.addItem(label, str(item.get("templateKey", "")))
            self.status_message = (
                f"Found {len(self.template_suggestions):,} masked template(s). Select one and insert an editable draft; "
                "it will not become active until you review and save overrides."
            )
            self.status_tone = "success"
        self._refresh_view_state()

    def _handle_template_mining_failed(self, message: str) -> None:
        """Show one template-mining failure without changing local rules."""

        self.mining = False
        self.status_message = str(message or "Could not mine message templates.")
        self.status_tone = "error"
        self._refresh_view_state()

    def _insert_mined_template(self) -> None:
        """Build the selected mined cluster into an unsaved editable rule draft."""

        template_key = str(self.mined_template_combo.currentData() or "").strip()
        if not template_key or self.mining or self.saving or self.validating or self.loading or self.expenses_job_running:
            return
        self.mining = True
        self.status_message = "Building an editable rule draft from the selected template..."
        self.status_tone = "info"
        self._refresh_view_state()
        worker = _TaskWorker(self.screen_api.build_mined_bank_rule_draft, template_key)
        worker.signals.finished.connect(self._handle_mined_draft_success)
        worker.signals.failed.connect(self._handle_template_mining_failed)
        self.window_api.thread_pool.start(worker)

    def _handle_mined_draft_success(self, result: object) -> None:
        """Append one generated draft to the editor without persisting it."""

        self.mining = False
        payload = dict(result) if isinstance(result, dict) else {}
        draft = payload.get("draft", {}) if isinstance(payload.get("draft", {}), dict) else {}
        if not draft:
            self.status_message = "The selected template did not produce a rule draft."
            self.status_tone = "error"
            self._refresh_view_state()
            return
        try:
            overrides = self._read_override_payload()
        except ValueError as exc:
            self.status_message = str(exc)
            self.status_tone = "error"
            self._refresh_view_state()
            return
        overrides.setdefault("banks", []).append(draft)
        self.overrides_editor.setPlainText(json.dumps(overrides, indent=2) + "\n")
        self.status_message = (
            f"Inserted mined draft '{draft.get('id', '')}'. Review its sender/subject match and extraction regexes, "
            "then Validate On Selected Mail and Save Overrides to activate it."
        )
        self.status_tone = "info"
        self._refresh_view_state()

    def _handle_reparse_success(self, result: object) -> None:
        """Apply one completed reparse result."""

        self.reparsing = False
        payload = dict(result) if isinstance(result, dict) else {}
        reparsed = int(payload.get("reparsedRecords", 0) or 0)
        self.status_message = f"Reparsed {reparsed} stored candidates."
        self.status_tone = "success"
        self._refresh_view_state()
        self._load_snapshot()

    def _handle_reparse_failed(self, message: str) -> None:
        """Show one failed candidate reparse result."""

        self.reparsing = False
        self.status_message = str(message or "Could not reparse stored candidate emails.")
        self.status_tone = "error"
        self._refresh_view_state()

    def _refresh_summary(self) -> None:
        """Render the summary cards from the latest snapshot."""

        self.total_value.setText(f"{int(self.summary.get('totalCandidates', 0) or 0):,}")
        self.parsed_value.setText(f"{int(self.summary.get('parsedCount', 0) or 0):,}")
        self.failed_value.setText(f"{int(self.summary.get('failedCount', 0) or 0):,}")
        self.rule_miss_value.setText(f"{int(self.summary.get('ruleMissCount', 0) or 0):,}")
        self.checkpoint_value.setText(self._format_timestamp(str(self.summary.get("lastCandidateAt", "")).strip()))

    def _refresh_view_state(self) -> None:
        """Refresh button state and the inline status banner."""

        busy = self.loading or self.detail_loading or self.validating or self.saving or self.reparsing or self.mining or self.expenses_job_running
        self.reload_button.setEnabled(not busy)
        self.reparse_button.setEnabled(not busy and bool(self.rows))
        self.mine_templates_button.setEnabled(not busy)
        self.validate_button.setEnabled(not busy and self.selected_source_record_id > 0)
        self.add_bank_button.setEnabled(not busy)
        self.mined_template_combo.setEnabled(not busy and bool(self.template_suggestions))
        self.add_mined_button.setEnabled(not busy and bool(self.mined_template_combo.currentData()))
        self.save_button.setEnabled(not busy)
        self.filter_combo.setEnabled(not busy and bool(self.rows))
        self._apply_banner_tone(
            self.status_tone,
            self.status_message or "Inspect candidate emails, insert a bank template, and edit the raw override JSON.",
        )

    def _apply_banner_tone(self, tone: str, text: str) -> None:
        """Apply one semantic tone to the inline banner."""

        palette = {
            "error": (self.theme.hex("rose"), self.theme.hex("surface_panel_alt"), self.theme.hex("text_primary")),
            "warning": (self.theme.hex("amber"), self.theme.hex("surface_panel_alt"), self.theme.hex("text_primary")),
            "success": (self.theme.hex("emerald"), self.theme.hex("surface_panel_alt"), self.theme.hex("text_primary")),
            "info": (self.theme.hex("blue"), self.theme.hex("surface_panel_alt"), self.theme.hex("text_primary")),
            "muted": (
                self.theme.hex("divider", self.theme.hex("border")),
                self.theme.hex("surface_panel_alt"),
                self.theme.hex("text_secondary"),
            ),
        }
        border, background, color = palette.get(tone, palette["muted"])
        self.banner.setText(text)
        self.banner.setStyleSheet(
            f"""
            QLabel {{
                background-color: {background};
                color: {color};
                border: 1px solid {border};
                padding: 8px 10px;
            }}
            """
        )

    def _format_timestamp(self, value: str) -> str:
        """Format one stored ISO timestamp for compact desktop display."""

        if not value:
            return "Never"
        try:
            return datetime.fromisoformat(value).strftime("%d %b %Y %H:%M")
        except ValueError:
            return value

    def _insert_bank_template(self) -> None:
        """Append one starter bank rule into the editable override JSON."""

        if self.saving or self.validating or self.loading or self.expenses_job_running:
            return
        try:
            payload = self._read_override_payload()
        except ValueError as exc:
            self.status_message = str(exc)
            self.status_tone = "error"
            self._refresh_view_state()
            return

        banks = payload.setdefault("banks", [])
        existing_ids = {
            str(item.get("id", "")).strip()
            for item in banks
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
        template = build_bank_rule_template(existing_ids=existing_ids)
        self._seed_template_from_selected_detail(template, existing_ids)
        banks.append(template)
        self.overrides_editor.setPlainText(json.dumps(payload, indent=2) + "\n")
        self.status_message = (
            f"Inserted bank template '{template['id']}'. Fill regex fields, Validate On Selected Mail, then Save Overrides. "
            "Run Rebuild Now to backfill newly matched history."
        )
        self.status_tone = "info"
        self._refresh_view_state()

    def _read_override_payload(self) -> dict[str, Any]:
        """Return the current override editor JSON as one mutable object."""

        raw_text = self.overrides_editor.toPlainText().strip()
        if not raw_text:
            return {"banks": []}
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Fix the override JSON before inserting a bank template: {exc.msg}.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Override JSON must be one object with a 'banks' list.")
        banks = payload.get("banks", [])
        if banks in (None, ""):
            banks = []
        if not isinstance(banks, list):
            raise ValueError("Override JSON must contain a 'banks' list.")
        payload["banks"] = list(banks)
        return payload

    def _detail_guidance(self, attempt: dict[str, Any]) -> str:
        reason_code = str(attempt.get("reasonCode", "")).strip()
        if reason_code == "no_matching_rule":
            return (
                "No active rule matched this email. Select Add Bank Template; it will prefill sender/subject from this email. "
                "Then set amount, date/time, account, transaction id, and counterparty regexes and Validate On Selected Mail."
            )
        if reason_code in {"missing_required_fields", "no_transaction_evidence", "no_payload"}:
            return "The rule matched but extraction failed. Adjust the regex fields in Local overrides and Validate On Selected Mail."
        return ""

    def _seed_template_from_selected_detail(self, template: dict[str, Any], existing_ids: set[str]) -> None:
        if not isinstance(self.detail, dict):
            return
        attempts = list(self.detail.get("attempts", [])) if isinstance(self.detail.get("attempts", []), list) else []
        first_attempt = attempts[0] if attempts else {}
        if str(first_attempt.get("reasonCode", "")).strip() != "no_matching_rule":
            return
        preview = first_attempt.get("preview", {}) if isinstance(first_attempt.get("preview", {}), dict) else {}
        payload = self.detail.get("payload", {}) if isinstance(self.detail.get("payload", {}), dict) else {}
        sender = str(preview.get("sender", "") or self.detail.get("sender", "") or payload.get("from", "")).strip()
        subject = str(preview.get("subject", "") or payload.get("subject", "") or self.detail.get("title", "")).strip()
        from_contains = [str(item).strip() for item in preview.get("suggestedFromContains", []) if str(item).strip()]
        subject_contains = [str(item).strip() for item in preview.get("suggestedSubjectContains", []) if str(item).strip()]
        if not from_contains:
            from_contains = self._sender_suggestions(sender)
        if not subject_contains:
            subject_contains = self._subject_suggestions(subject)
        if from_contains:
            template.setdefault("match", {})["fromContains"] = from_contains[:3]
        if subject_contains:
            template.setdefault("match", {})["subjectContains"] = subject_contains[:3]
        bank_name = self._bank_name_from_sender(sender)
        if bank_name:
            template["name"] = bank_name
            template.setdefault("defaults", {})["bankName"] = bank_name
            candidate_id = re.sub(r"[^a-z0-9]+", "_", bank_name.lower()).strip("_")
            if candidate_id and candidate_id not in existing_ids:
                template["id"] = candidate_id

    def _sender_suggestions(self, sender: str) -> list[str]:
        sender_text = str(sender or "").strip().lower()
        match = re.search(r"@([a-z0-9.-]+)", sender_text)
        if match:
            return [match.group(1)]
        clean = re.sub(r"[^a-z0-9@._-]+", " ", sender_text).strip()
        return [clean] if clean else []

    def _subject_suggestions(self, subject: str) -> list[str]:
        text = re.sub(r"[^a-z0-9]+", " ", str(subject or "").strip().lower())
        tokens = [token for token in text.split() if len(token) >= 4 and not token.isdigit()]
        return [" ".join(tokens[:4])] if tokens else []

    def _bank_name_from_sender(self, sender: str) -> str:
        suggestions = self._sender_suggestions(sender)
        if not suggestions:
            return ""
        domain = suggestions[0].split("@")[-1]
        stem = domain.split(".", 1)[0]
        tokens = [token for token in re.split(r"[^a-z0-9]+", stem) if token and token not in {"mail", "alerts", "alert", "noreply", "no"}]
        return " ".join(token.capitalize() for token in tokens[:3])
