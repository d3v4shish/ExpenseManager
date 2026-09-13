from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.app.ui_kit import BaseWidget, SectionCard, make_button, make_label
from src.expenses.sources.providers import CsvProvider


class _TaskSignals(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class _TaskWorker(QRunnable):
    """Run source discovery, validation, and writes outside the UI thread."""

    def __init__(self, fn, *args) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            self.signals.finished.emit(self.fn(*self.args))
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(str(exc))


class SourceSetupDialog(QDialog):
    """Collect one explicit local source definition before it is validated and saved."""

    TYPES = (
        ("EML folder", "eml_folder"),
        ("CSV file", "csv"),
        ("SMS backup", "sms_backup"),
        ("AxiosAlternative archive", "axios_archive"),
    )
    ID_PREFIXES = {
        "eml_folder": "eml",
        "csv": "csv",
        "sms_backup": "sms",
        "axios_archive": "axios",
    }
    CONFLICTING_ID_TOKENS = {
        "eml_folder": {"sms", "csv", "axios"},
        "csv": {"sms", "eml", "axios"},
        "sms_backup": {"csv", "eml", "axios"},
        "axios_archive": {"csv", "eml", "sms"},
    }

    def __init__(self, theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.setWindowTitle("Add Local Source")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.type_combo = QComboBox()
        for label, value in self.TYPES:
            self.type_combo.addItem(label, value)
        self.id_input = QLineEdit()
        self.id_input.textEdited.connect(self._mark_id_as_edited)
        self._id_was_edited = False
        self._suggested_source_id = ""
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Choose a local file or folder")
        self.path_input.textChanged.connect(self._suggest_source_id)
        browse = make_button("Browse…", theme.hex("divider"), theme.hex("text_primary"), theme.hex("surface_panel_alt"))
        browse.clicked.connect(self._choose_path)
        path_layout.addWidget(self.path_input, 1)
        path_layout.addWidget(browse)
        self.mapping_input = QPlainTextEdit()
        self.mapping_input.setPlaceholderText('{"date":"Date","amount":"Amount","direction":"Direction","description":"Description","reference":"Reference"}')
        self.mapping_input.setPlainText('{"date":"Date","amount":"Amount","direction":"Direction","description":"Description"}')
        self.mapping_input.setFixedHeight(96)
        self.preview_button = make_button("Preview mapping", theme.hex("divider"), theme.hex("text_primary"), theme.hex("surface_panel_alt"))
        self.preview_output = QPlainTextEdit()
        self.preview_output.setReadOnly(True)
        self.preview_output.setFixedHeight(120)
        self.recursive = QCheckBox("Scan subfolders")
        self.recursive.setChecked(True)
        self.form = form
        form.addRow("Source type", self.type_combo)
        form.addRow("Source ID", self.id_input)
        form.addRow("Path", path_row)
        form.addRow("CSV mapping", self.mapping_input)
        form.addRow("CSV preview", self.preview_button)
        form.addRow("EML options", self.recursive)
        layout.addLayout(form)
        layout.addWidget(self.preview_output)
        self.hint = make_label("CSV imports require a saved mapping. SMS accepts Android SMS Backup & Restore XML or the documented JSON format.", theme.hex("text_muted"), 8)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.type_combo.currentIndexChanged.connect(self._refresh_fields)
        self.preview_button.clicked.connect(self._start_preview)
        self._refresh_fields()

    def _refresh_fields(self) -> None:
        is_csv = self.provider_type() == "csv"
        is_eml = self.provider_type() == "eml_folder"
        self.mapping_input.setVisible(is_csv)
        self.preview_button.setVisible(is_csv)
        self.preview_output.setVisible(is_csv)
        self.recursive.setVisible(is_eml)
        self._set_form_row_visible(self.mapping_input, is_csv)
        self._set_form_row_visible(self.preview_button, is_csv)
        self._set_form_row_visible(self.recursive, is_eml)
        self._set_source_id_placeholder()
        self._suggest_source_id()

    def _set_form_row_visible(self, field: QWidget, visible: bool) -> None:
        """Hide a form label with its conditional field instead of leaving blank rows."""

        label = self.form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def _set_source_id_placeholder(self) -> None:
        """Show an example that matches the selected source type."""

        prefix = self.ID_PREFIXES.get(self.provider_type(), "source")
        self.id_input.setPlaceholderText(f"e.g. {prefix}-bank-alerts")

    def _mark_id_as_edited(self, _text: str) -> None:
        """Keep a deliberate user-supplied ID when the path changes."""

        self._id_was_edited = bool(self.id_input.text().strip())

    def _suggest_source_id(self, _path: str = "") -> None:
        """Generate a safe, source-type-specific ID until the user edits it."""

        if self._id_was_edited:
            return
        current = self.id_input.text().strip()
        if current and current != self._suggested_source_id:
            self._id_was_edited = True
            return
        prefix = self.ID_PREFIXES.get(self.provider_type(), "source")
        name = Path(self.path_input.text().strip()).name or "source"
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "source"
        self._suggested_source_id = f"{prefix}-{slug}"
        self.id_input.setText(self._suggested_source_id)

    def _choose_path(self) -> None:
        if self.provider_type() == "eml_folder":
            path = QFileDialog.getExistingDirectory(self, "Choose EML Folder", self.path_input.text() or str(Path.home()))
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose Local Source", self.path_input.text() or str(Path.home()))
        if path:
            self.path_input.setText(path)

    def provider_type(self) -> str:
        return str(self.type_combo.currentData() or "")

    def source_definition(self) -> dict[str, Any]:
        provider_id = self.id_input.text().strip()
        path = self.path_input.text().strip()
        if not provider_id or not path:
            raise ValueError("Source ID and path are required.")
        provider_type = self.provider_type()
        self._validate_source_id(provider_id, provider_type)
        source = {"id": provider_id, "type": provider_type, "path": path, "enabled": True}
        if source["type"] == "eml_folder":
            source["recursive"] = self.recursive.isChecked()
        if source["type"] == "csv":
            try:
                mapping = json.loads(self.mapping_input.toPlainText())
            except json.JSONDecodeError as exc:
                raise ValueError(f"CSV mapping must be valid JSON: {exc.msg}") from exc
            if not isinstance(mapping, dict):
                raise ValueError("CSV mapping must be a JSON object.")
            source["mapping"] = mapping
        return source

    def _validate_source_id(self, provider_id: str, provider_type: str) -> None:
        """Reject IDs that visibly claim a different configured source type."""

        tokens = {token for token in re.split(r"[^a-z0-9]+", provider_id.lower()) if token}
        conflicting = sorted(tokens.intersection(self.CONFLICTING_ID_TOKENS.get(provider_type, set())))
        if conflicting:
            expected = self.ID_PREFIXES.get(provider_type, "source").upper()
            claimed = conflicting[0].upper()
            raise ValueError(f"Source ID '{provider_id}' looks like {claimed}; choose an ID for this {expected} source.")

    def accept(self) -> None:
        try:
            self.source_definition()
        except ValueError as exc:
            self.hint.setText(str(exc))
            return
        super().accept()

    def _start_preview(self) -> None:
        try:
            source = self.source_definition()
        except ValueError as exc:
            self.hint.setText(str(exc))
            return
        self.preview_button.setEnabled(False)
        self.preview_output.setPlainText("Loading CSV preview…")
        self._preview_worker = _TaskWorker(lambda: CsvProvider(source).preview())
        self._preview_worker.signals.finished.connect(self._preview_loaded)
        self._preview_worker.signals.failed.connect(self._preview_failed)
        QThreadPool.globalInstance().start(self._preview_worker)

    def _preview_loaded(self, payload: object) -> None:
        self.preview_button.setEnabled(True)
        preview = dict(payload) if isinstance(payload, dict) else {}
        rows = list(preview.get("rows", []))
        self.preview_output.setPlainText(json.dumps({"columns": preview.get("columns", []), "rows": rows}, ensure_ascii=False, indent=2))

    def _preview_failed(self, message: str) -> None:
        self.preview_button.setEnabled(True)
        self.preview_output.setPlainText(f"Preview failed: {message}")


class SourceRecordsDialog(QDialog):
    """Show bounded source-record diagnostics already loaded by a worker."""

    def __init__(self, provider_id: str, rows: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Source Records — {provider_id}")
        self.resize(1040, 520)
        layout = QVBoxLayout(self)
        table = QTableWidget(len(rows), 7)
        table.setHorizontalHeaderLabels(("When", "Type", "Title", "Sender", "Status", "Reason", "Location"))
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.horizontalHeader().setStretchLastSection(True)
        for row_index, row in enumerate(rows):
            values = (
                row.get("receivedAt", ""), row.get("recordType", ""), row.get("title", ""), row.get("sender", ""),
                row.get("parseStatus", ""), row.get("reasonText", row.get("reasonCode", "")), row.get("sourceUri", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                table.setItem(row_index, column, item)
        layout.addWidget(table)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)


class ReconciliationConflictDialog(QDialog):
    """Let the user choose the retained value for one exact-identity conflict."""

    def __init__(self, conflict: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.conflict = dict(conflict)
        self.setWindowTitle("Resolve Source Conflict")
        self.resize(800, 360)
        layout = QVBoxLayout(self)
        identity = (
            f"{self.conflict.get('bankName', 'Unknown bank')} · "
            f"{self.conflict.get('transactionId', 'No reference')} · "
            f"{self.conflict.get('timestamp', '')}"
        )
        explanation = QLabel(
            "Sources share this transaction identity but disagree on its value. "
            "Choose the value to show in the ledger; all source records stay attached as evidence."
        )
        explanation.setWordWrap(True)
        layout.addWidget(make_label(identity, "#222222", 9, True))
        layout.addWidget(explanation)
        candidates = [dict(item) for item in self.conflict.get("candidates", []) if isinstance(item, dict)]
        self.table = QTableWidget(len(candidates), 4)
        self.table.setHorizontalHeaderLabels(("Provider(s)", "Amount", "Currency", "Merchant"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        for row, candidate in enumerate(candidates):
            values = (
                ", ".join(str(value) for value in candidate.get("providerIds", []) if str(value)),
                str(candidate.get("amount", "")),
                str(candidate.get("currency", "")),
                str(candidate.get("merchant", "")),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, str(candidate.get("signature", "")))
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        if candidates:
            self.table.selectRow(0)
        layout.addWidget(self.table)
        self.use_provider_priority = QCheckBox("Also use this provider first for future exact-identity conflicts")
        self.use_provider_priority.setToolTip("Enables the explicit provider-priority policy; existing manual choices remain unchanged.")
        layout.addWidget(self.use_provider_priority)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Apply)
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText("Use Selected Value")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_signature(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    def selected_provider_id(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        if item is None:
            return ""
        candidates = [dict(value) for value in self.conflict.get("candidates", []) if isinstance(value, dict)]
        if not 0 <= row < len(candidates):
            return ""
        return next((str(value).strip() for value in candidates[row].get("providerIds", []) if str(value).strip()), "")

    def accept(self) -> None:
        if not self.selected_signature():
            return
        super().accept()


class ExpensesSourcesWidget(BaseWidget):
    """Dense local-source grid with explicit setup, validation, and duplicate review."""

    def __init__(self, widget_config, widget_data, theme, screen_api, window_api, parent=None):
        super().__init__(widget_config, widget_data, theme, screen_api, window_api, parent)
        self.sources: list[dict[str, Any]] = []
        self.reconciliation_conflicts: list[dict[str, Any]] = []
        self.busy = False
        self._build_ui()
        self._reload_sources()

    def apply_reload(self, widget_config: dict[str, Any], widget_data: dict[str, Any]) -> bool:
        self.widget_config = widget_config
        self.widget_data = widget_data
        self._reload_sources()
        return True

    def set_job_state(self, job_id: str, running: bool, payload: dict | None = None) -> None:
        if job_id not in {"expenses.refresh", "expenses.rebuild"}:
            return
        self.busy = running
        if running:
            provider = str((payload or {}).get("providerId", "")).strip()
            self.status.setText(f"Importing {provider or 'sources'}…")
        else:
            self._reload_sources()
        self._refresh_actions()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = SectionCard("Sources", "Local, offline imports. Removing a source keeps its retained transactions.", bg=self.theme.hex("surface_panel"), border=self.theme.hex("divider"))
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.add_button = make_button("Add Source", self.theme.hex("accent"), self.theme.hex("text_primary"), self.theme.hex("accent"), emphasis="primary")
        self.validate_button = make_button("Validate", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.enable_button = make_button("Enable / Disable", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.refresh_button = make_button("Refresh", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.rebuild_button = make_button("Rebuild", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.remove_button = make_button("Remove", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.inspect_button = make_button("Inspect Records", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.delete_data_button = make_button("Delete Imported Data", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        for button in (self.add_button, self.validate_button, self.enable_button, self.refresh_button, self.rebuild_button, self.remove_button, self.inspect_button, self.delete_data_button):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        body_layout.addLayout(toolbar)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(("ID", "Type", "Location", "Enabled", "Records", "Stored", "Status", "Last import"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(190)
        body_layout.addWidget(self.table)
        self.status = make_label("Loading sources…", self.theme.hex("text_muted"), 8)
        self.status.setWordWrap(True)
        body_layout.addWidget(self.status)
        self.duplicates = QTableWidget(0, 4)
        self.duplicates.setHorizontalHeaderLabels(("Transaction A", "Transaction B", "Confidence", "Decision"))
        self.duplicates.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.duplicates.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.duplicates.horizontalHeader().setStretchLastSection(True)
        self.duplicates.setMaximumHeight(150)
        body_layout.addWidget(make_label("Duplicate review queue", self.theme.hex("text_secondary"), 9, True))
        body_layout.addWidget(self.duplicates)
        duplicate_actions = QHBoxLayout()
        self.keep_button = make_button("Keep Separate", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.merge_button = make_button("Mark Merged", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        duplicate_actions.addWidget(self.keep_button)
        duplicate_actions.addWidget(self.merge_button)
        duplicate_actions.addStretch(1)
        body_layout.addLayout(duplicate_actions)
        self.reconciliation = QTableWidget(0, 6)
        self.reconciliation.setHorizontalHeaderLabels(("Bank / Reference", "Time", "Values", "Status", "Mode", "Selected"))
        self.reconciliation.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.reconciliation.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.reconciliation.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.reconciliation.horizontalHeader().setStretchLastSection(True)
        self.reconciliation.setMaximumHeight(150)
        body_layout.addWidget(make_label("Exact transaction conflicts", self.theme.hex("text_secondary"), 9, True))
        body_layout.addWidget(self.reconciliation)
        reconciliation_actions = QHBoxLayout()
        self.resolve_conflict_button = make_button("Resolve Selected", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.resolve_conflict_button.setToolTip("Choose the ledger value for the selected exact transaction conflict.")
        reconciliation_actions.addWidget(self.resolve_conflict_button)
        reconciliation_actions.addStretch(1)
        body_layout.addLayout(reconciliation_actions)
        card.add_content_widget(body)
        root.addWidget(card)
        self.add_button.clicked.connect(self._add_source)
        self.validate_button.clicked.connect(self._validate_selected)
        self.enable_button.clicked.connect(self._toggle_selected)
        self.refresh_button.clicked.connect(lambda: self.screen_api.run_refresh())
        self.rebuild_button.clicked.connect(lambda: self.screen_api.run_rebuild())
        self.remove_button.clicked.connect(self._remove_selected)
        self.inspect_button.clicked.connect(self._inspect_selected)
        self.delete_data_button.clicked.connect(self._delete_selected_data)
        self.keep_button.clicked.connect(lambda: self._resolve_duplicate("kept_separate"))
        self.merge_button.clicked.connect(lambda: self._resolve_duplicate("merged"))
        self.resolve_conflict_button.clicked.connect(self._resolve_reconciliation_conflict)
        self.table.itemSelectionChanged.connect(self._refresh_actions)
        self.duplicates.itemSelectionChanged.connect(self._refresh_actions)
        self.reconciliation.itemSelectionChanged.connect(self._refresh_actions)
        self._refresh_actions()

    def _reload_sources(self) -> None:
        self.busy = True
        self._refresh_actions()
        self._start_task(self._load_data, self._loaded, "Could not load sources.")

    def _load_data(self) -> dict[str, Any]:
        return {
            "sources": self.screen_api.list_sources(),
            "duplicates": self.screen_api.list_duplicate_candidates(),
            "reconciliation": self.screen_api.list_reconciliation_conflicts(),
        }

    def _loaded(self, payload: object) -> None:
        data = dict(payload) if isinstance(payload, dict) else {}
        self.sources = [dict(item) for item in data.get("sources", []) if isinstance(item, dict)]
        self.table.setRowCount(len(self.sources))
        for row, source in enumerate(self.sources):
            validation = dict(source.get("validation", {}))
            checkpoint = getattr(self.screen_api.expenses_repository, "get_provider_checkpoint", lambda _id: None)(str(source.get("id", ""))) or {}
            values = (
                str(source.get("id", "")), str(source.get("type", "")), str(source.get("path", source.get("profilePath", ""))),
                "Yes" if bool(source.get("enabled", True)) else "No",
                f"{int(source.get('recordCount', 0) or 0):,}",
                _byte_label(int(source.get("payloadBytes", 0) or 0)),
                "Ready" if bool(validation.get("valid")) else str(validation.get("message", "Invalid")),
                str(checkpoint.get("updatedAt", "Never")) or "Never",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, source.get("id", ""))
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        candidates = [dict(item) for item in data.get("duplicates", []) if isinstance(item, dict)]
        self.duplicates.setRowCount(len(candidates))
        for row, candidate in enumerate(candidates):
            for column, value in enumerate((candidate.get("transactionKeyA", ""), candidate.get("transactionKeyB", ""), f"{float(candidate.get('confidence', 0)):.0%}", candidate.get("state", ""))):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, candidate.get("candidateKey", ""))
                self.duplicates.setItem(row, column, item)
        self.reconciliation_conflicts = [dict(item) for item in data.get("reconciliation", []) if isinstance(item, dict)]
        self.reconciliation.setRowCount(len(self.reconciliation_conflicts))
        for row, conflict in enumerate(self.reconciliation_conflicts):
            selected = next(
                (item for item in conflict.get("candidates", []) if isinstance(item, dict) and item.get("signature") == conflict.get("selectedSignature")),
                {},
            )
            values = (
                f"{conflict.get('bankName', '')} / {conflict.get('transactionId', '')}",
                conflict.get("timestamp", ""),
                f"{conflict.get('candidateCount', 0)} candidate values",
                conflict.get("status", ""),
                conflict.get("resolutionMode", ""),
                f"{selected.get('amount', '')} {selected.get('currency', '')}",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, conflict.get("reconciliationKey", ""))
                item.setToolTip(str(value))
                self.reconciliation.setItem(row, column, item)
        self.status.setText(
            f"{len(self.sources)} configured source(s); {len(candidates)} duplicate candidate(s); "
            f"{len(self.reconciliation_conflicts)} exact conflict(s) need review."
        )
        self.busy = False
        self._refresh_actions()

    def _add_source(self) -> None:
        dialog = SourceSetupDialog(self.theme, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.busy = True
        self.status.setText("Validating and saving source…")
        self._refresh_actions()
        self._start_task(self.screen_api.save_source, self._source_saved, "Source was not saved.", dialog.source_definition())

    def _source_saved(self, _payload: object) -> None:
        self.status.setText("Source saved. Refresh to import it.")
        self._reload_sources()

    def _selected_source(self) -> dict[str, Any] | None:
        row = self.table.currentRow()
        return self.sources[row] if 0 <= row < len(self.sources) else None

    def _validate_selected(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        self.busy = True
        self.status.setText(f"Validating {source.get('id', '')}…")
        self._refresh_actions()
        self._start_task(self.screen_api.validate_source, self._source_validated, "Source validation failed.", source)

    def _source_validated(self, payload: object) -> None:
        validation = dict(payload).get("validation", {}) if isinstance(payload, dict) else {}
        self.busy = False
        self.status.setText("Source is ready." if bool(validation.get("valid")) else str(validation.get("message", "Source is not valid.")))
        self._refresh_actions()

    def _toggle_selected(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        next_source = self.screen_api._source_definition(source)
        next_source["enabled"] = not bool(source.get("enabled", True))
        self.busy = True
        self._start_task(self.screen_api.save_source, self._source_saved, "Source setting was not saved.", next_source)

    def _remove_selected(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        self.busy = True
        self.status.setText("Removing source configuration; retained transactions will remain.")
        self._refresh_actions()
        self._start_task(self.screen_api.remove_source, self._source_saved, "Source was not removed.", str(source.get("id", "")))

    def _delete_selected_data(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        provider_id = str(source.get("id", ""))
        count = int(source.get("recordCount", 0) or 0)
        choice = QMessageBox.warning(
            self,
            "Delete Imported Data",
            f"Delete {count:,} retained source record(s) for '{provider_id}'?\n\nThis recalculates the ledger from remaining sources. The source configuration and original files are not deleted.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        self.busy = True
        self.status.setText("Deleting retained source data and rematerializing the ledger…")
        self._refresh_actions()
        self._start_task(self.screen_api.delete_source_data, self._source_saved, "Source data was not deleted.", provider_id)

    def _inspect_selected(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        provider_id = str(source.get("id", ""))
        self.busy = True
        self.status.setText(f"Loading retained records for {provider_id}…")
        self._refresh_actions()
        self._start_task(lambda: self.screen_api.list_source_debug_rows(provider_id), lambda rows: self._show_source_records(provider_id, rows), "Could not load source records.")

    def _show_source_records(self, provider_id: str, rows: object) -> None:
        self.busy = False
        self._refresh_actions()
        SourceRecordsDialog(provider_id, [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else [], self).exec()

    def _resolve_duplicate(self, decision: str) -> None:
        row = self.duplicates.currentRow()
        item = self.duplicates.item(row, 0) if row >= 0 else None
        if item is None:
            return
        self.busy = True
        self._start_task(lambda: self._resolve_then_load(str(item.data(Qt.ItemDataRole.UserRole)), decision), self._loaded, "Duplicate decision was not saved.")

    def _resolve_then_load(self, candidate_key: str, decision: str) -> dict[str, Any]:
        self.screen_api.resolve_duplicate_candidate(candidate_key, decision)
        return self._load_data()

    def _selected_reconciliation_conflict(self) -> dict[str, Any] | None:
        row = self.reconciliation.currentRow()
        return self.reconciliation_conflicts[row] if 0 <= row < len(self.reconciliation_conflicts) else None

    def _resolve_reconciliation_conflict(self) -> None:
        conflict = self._selected_reconciliation_conflict()
        if conflict is None:
            return
        dialog = ReconciliationConflictDialog(conflict, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        signature = dialog.selected_signature()
        if not signature:
            return
        self.busy = True
        self.status.setText("Applying source conflict decision…")
        self._refresh_actions()
        self._start_task(
            lambda: self._resolve_reconciliation_then_load(
                str(conflict.get("reconciliationKey", "")),
                signature,
                dialog.selected_provider_id() if dialog.use_provider_priority.isChecked() else "",
            ),
            self._loaded,
            "Source conflict was not resolved.",
        )

    def _resolve_reconciliation_then_load(self, reconciliation_key: str, signature: str, preferred_provider_id: str) -> dict[str, Any]:
        self.screen_api.resolve_reconciliation_conflict(reconciliation_key, signature)
        if preferred_provider_id:
            self.screen_api.set_reconciliation_policy("provider_priority", preferred_provider_id=preferred_provider_id)
        return self._load_data()

    def _start_task(self, fn, on_success, failure_prefix: str, *args) -> None:
        worker = _TaskWorker(fn, *args)
        worker.signals.finished.connect(on_success)
        worker.signals.failed.connect(lambda message: self._failed(failure_prefix, message))
        QThreadPool.globalInstance().start(worker)

    def _failed(self, prefix: str, message: str) -> None:
        self.busy = False
        self.status.setText(f"{prefix} {message}")
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        selected = self._selected_source() is not None
        duplicate_selected = self.duplicates.currentRow() >= 0
        reconciliation_selected = self.reconciliation.currentRow() >= 0
        for button in (self.add_button, self.validate_button, self.enable_button, self.remove_button, self.inspect_button, self.delete_data_button, self.refresh_button, self.rebuild_button):
            button.setEnabled(not self.busy and (button is self.add_button or button in (self.refresh_button, self.rebuild_button) or selected))
        self.keep_button.setEnabled(not self.busy and duplicate_selected)
        self.merge_button.setEnabled(not self.busy and duplicate_selected)
        self.resolve_conflict_button.setEnabled(not self.busy and reconciliation_selected)


__all__ = ["ExpensesSourcesWidget", "SourceSetupDialog", "SourceRecordsDialog", "ReconciliationConflictDialog"]


def _byte_label(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"
