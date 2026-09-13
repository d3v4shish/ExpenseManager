from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.app.ui_kit import BaseWidget, SectionCard, ThemedMessageBox, make_button, make_label


class _Signals(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class _Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs) -> None:
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()

    def run(self) -> None:
        try:
            self.signals.finished.emit(self.fn(*self.args, **self.kwargs))
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(str(exc))


class StorageTableModel(QAbstractTableModel):
    HEADERS = ("Area", "File", "Kind", "Size", "Path")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else ""
        return super().headerData(section, orientation, role)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole or not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        values = (
            str(row.get("category", "")).title(),
            str(row.get("relativePath", "")),
            str(row.get("kind", "")).title(),
            self.format_size(int(row.get("sizeBytes", 0) or 0)),
            str(row.get("path", "")),
        )
        return values[index.column()]

    def set_report(self, report: dict[str, Any]) -> None:
        rows: list[dict[str, Any]] = []
        for category in report.get("categories", []):
            for entry in category.get("entries", []):
                rows.append({"category": category.get("category", ""), **dict(entry)})
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    @staticmethod
    def format_size(size: int) -> str:
        value = float(max(0, size))
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024.0 or unit == "GiB":
                return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value):,} B"
            value /= 1024.0
        return f"{value:,.1f} GiB"


class ExpenseSettingsWidget(BaseWidget):
    """Expose all settings, runtime sizes, backup, restore, and retention controls."""

    def __init__(self, widget_config, widget_data, theme, screen_api, window_api, parent=None):
        super().__init__(widget_config, widget_data, theme, screen_api, window_api, parent)
        self.thread_pool = QThreadPool.globalInstance()
        self.settings: dict[str, Any] = {}
        self.storage: dict[str, Any] = {}
        self.busy = False
        self.dirty = False
        self._applying_settings = False
        self._compact_settings_grid = False
        self._build_ui()

    def bind_card_ref(self, card_ref: str) -> None:
        super().bind_card_ref(card_ref)
        if self.is_card_visible():
            QTimer.singleShot(0, self._load_all)

    def apply_reload(self, widget_config: dict[str, Any], widget_data: dict[str, Any]) -> bool:
        self.widget_config = widget_config
        self.widget_data = widget_data
        if self.is_card_visible() and not self.busy:
            QTimer.singleShot(0, self._load_storage if self.dirty else self._load_all)
        return True

    def on_card_visibility_changed(self, visible: bool) -> None:
        if visible and not self.busy:
            QTimer.singleShot(0, self._load_storage if self.dirty else self._load_all)

    def _build_ui(self) -> None:
        self.setStyleSheet(
            f"""
            QSpinBox, QDoubleSpinBox, QComboBox, QTableView {{
                color: {self.theme.hex('text_primary')};
                background: {self.theme.hex('surface_panel_alt')};
                border: 1px solid {self.theme.hex('divider')};
                selection-background-color: {self.theme.hex('surface_active')};
            }}
            QSpinBox, QDoubleSpinBox, QComboBox {{ min-height: 28px; padding: 2px 6px; }}
            QHeaderView::section {{
                color: {self.theme.hex('text_secondary')};
                background: {self.theme.hex('surface_panel')};
                border: none;
                border-bottom: 1px solid {self.theme.hex('divider')};
                padding: 5px 7px;
                font-weight: 600;
            }}
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = SectionCard(
            str(self.widget_config.get("title", "Settings & Storage")),
            "Runtime behavior, local analytics, backups, and generated data.",
            bg=self.theme.hex("surface_panel"),
            border=self.theme.hex("divider"),
        )
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        self.banner = QLabel("Loading settings and storage…")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(f"color: {self.theme.hex('text_secondary')};")
        layout.addWidget(self.banner)

        self.settings_grid = QGridLayout()
        self.settings_grid.setHorizontalSpacing(10)
        self.settings_grid.setVerticalSpacing(6)
        self.refresh_minutes = self._spin(1, 1440)
        self.lookback_days = self._spin(1, 3650)
        self.overlap_hours = self._spin(0, 168)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.log_size_mb = self._double_spin(0.1, 100.0, 0.5)
        self.log_backups = self._spin(1, 50)
        self.analytics_enabled = QCheckBox("Enable local insight generation")
        self.desktop_notifications = QCheckBox("Enable desktop notifications")
        self.sensitivity = QComboBox()
        self.sensitivity.addItems(["low", "normal", "high"])
        self.minimum_history = self._spin(3, 100)
        self.unusual_multiplier = self._double_spin(1.1, 20.0, 0.1)
        self.robust_deviation = self._double_spin(1.0, 20.0, 0.1)
        self.comparison_months = self._spin(3, 36)
        self.minimum_comparison_months = self._spin(2, 36)
        self.period_spike = self._double_spin(1.0, 1000.0, 1.0)
        self.recurring_change = self._double_spin(1.0, 1000.0, 1.0)
        self.recurring_confidence = self._double_spin(0.0, 1.0, 0.05)
        self.duplicate_window = self._spin(1, 1440)
        self.inr_transaction_floor = self._double_spin(0.0, 100_000_000.0, 100.0)
        self.inr_period_floor = self._double_spin(0.0, 100_000_000.0, 100.0)
        for control in (
            self.refresh_minutes, self.lookback_days, self.overlap_hours, self.log_level,
            self.log_size_mb, self.log_backups, self.analytics_enabled, self.desktop_notifications,
            self.sensitivity, self.minimum_history, self.unusual_multiplier, self.robust_deviation,
            self.comparison_months, self.minimum_comparison_months, self.period_spike,
            self.recurring_change, self.recurring_confidence, self.duplicate_window,
            self.inr_transaction_floor, self.inr_period_floor,
        ):
            signal = getattr(control, "valueChanged", None) or getattr(control, "currentTextChanged", None) or getattr(control, "toggled", None)
            if signal is not None:
                signal.connect(self._mark_dirty)

        fields = (
            ("Refresh minutes", self.refresh_minutes, "Lookback days", self.lookback_days),
            ("Overlap hours", self.overlap_hours, "Log level", self.log_level),
            ("Log file MiB", self.log_size_mb, "Log backups", self.log_backups),
            ("Sensitivity", self.sensitivity, "Minimum history", self.minimum_history),
            ("Amount multiplier", self.unusual_multiplier, "Robust deviation", self.robust_deviation),
            ("Comparison months", self.comparison_months, "Minimum months", self.minimum_comparison_months),
            ("Period spike %", self.period_spike, "Recurring change %", self.recurring_change),
            ("Recurring confidence", self.recurring_confidence, "Duplicate window min", self.duplicate_window),
            ("INR transaction floor", self.inr_transaction_floor, "INR period floor", self.inr_period_floor),
        )
        self.setting_fields = [
            (make_label(left_label, self.theme.hex("text_secondary"), 8), left,
             make_label(right_label, self.theme.hex("text_secondary"), 8), right)
            for left_label, left, right_label, right in fields
        ]
        self._reflow_settings_grid()
        layout.addLayout(self.settings_grid)

        settings_actions = QHBoxLayout()
        self.save_button = make_button("Save Settings", self.theme.hex("accent"), self.theme.hex("text_primary"), self.theme.hex("accent"))
        self.save_button.clicked.connect(self._save_settings)
        settings_actions.addWidget(self.save_button)
        settings_actions.addStretch(1)
        layout.addLayout(settings_actions)

        storage_header = QHBoxLayout()
        self.storage_total = make_label("Storage: calculating…", self.theme.hex("text_primary"), 9, True)
        storage_header.addWidget(self.storage_total)
        storage_header.addStretch(1)
        self.scan_button = make_button("Refresh Sizes", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.scan_button.clicked.connect(self._load_storage)
        storage_header.addWidget(self.scan_button)
        layout.addLayout(storage_header)

        self.storage_model = StorageTableModel()
        self.storage_table = QTableView()
        self.storage_table.setModel(self.storage_model)
        self.storage_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.storage_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.storage_table.verticalHeader().setVisible(False)
        self.storage_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.storage_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.storage_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.storage_table.setMinimumHeight(220)
        self.storage_table.selectionModel().selectionChanged.connect(self._storage_selection_changed)
        layout.addWidget(self.storage_table)

        data_actions = QHBoxLayout()
        self.backup_button = make_button("Create Backup", self.theme.hex("accent"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.restore_button = make_button("Restore Backup", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.clear_button = make_button("Clear Generated", self.theme.hex("amber"), self.theme.hex("amber"), self.theme.hex("surface_panel_alt"))
        self.uninstall_button = make_button("Prepare Uninstall", self.theme.hex("rose"), self.theme.hex("rose"), self.theme.hex("surface_panel_alt"))
        self.delete_database_button = make_button("Delete Selected DB", self.theme.hex("rose"), self.theme.hex("rose"), self.theme.hex("surface_panel_alt"))
        self.backup_button.clicked.connect(self._choose_backup_destination)
        self.restore_button.clicked.connect(self._choose_restore_source)
        self.clear_button.clicked.connect(self._confirm_clear_generated)
        self.uninstall_button.clicked.connect(self._prepare_uninstall)
        self.delete_database_button.clicked.connect(self._confirm_delete_selected_database)
        self.delete_database_button.setEnabled(False)
        for button in (self.backup_button, self.restore_button, self.clear_button, self.delete_database_button, self.uninstall_button):
            data_actions.addWidget(button)
        data_actions.addStretch(1)
        layout.addLayout(data_actions)

        card.add_content_widget(body)
        root.addWidget(card)

    def _reflow_settings_grid(self) -> None:
        """Switch advanced settings to one field per row before controls become cramped."""

        compact = self.width() > 0 and self.width() < 900
        if compact == self._compact_settings_grid and self.settings_grid.count():
            return
        self._compact_settings_grid = compact
        grid = self.settings_grid
        while grid.count():
            grid.takeAt(0)
        for column in range(4):
            grid.setColumnStretch(column, 0)
            grid.setColumnMinimumWidth(column, 0)
        if compact:
            row_index = 0
            for left_label, left, right_label, right in self.setting_fields:
                grid.addWidget(left_label, row_index, 0)
                grid.addWidget(left, row_index, 1)
                row_index += 1
                grid.addWidget(right_label, row_index, 0)
                grid.addWidget(right, row_index, 1)
                row_index += 1
            grid.addWidget(self.analytics_enabled, row_index, 0, 1, 2)
            grid.addWidget(self.desktop_notifications, row_index + 1, 0, 1, 2)
            grid.setColumnStretch(1, 1)
        else:
            for row_index, (left_label, left, right_label, right) in enumerate(self.setting_fields):
                grid.addWidget(left_label, row_index, 0)
                grid.addWidget(left, row_index, 1)
                grid.addWidget(right_label, row_index, 2)
                grid.addWidget(right, row_index, 3)
            checkbox_row = len(self.setting_fields)
            grid.addWidget(self.analytics_enabled, checkbox_row, 0, 1, 2)
            grid.addWidget(self.desktop_notifications, checkbox_row, 2, 1, 2)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(3, 1)
        grid.invalidate()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Reflow the dense settings form as the window crosses its breakpoint."""

        self._reflow_settings_grid()
        super().resizeEvent(event)

    def _load_all(self) -> None:
        self.settings = self.screen_api.get_settings_snapshot()
        self._apply_settings()
        self._load_storage()

    def _load_storage(self) -> None:
        if self.busy:
            return
        self._start_task(self.screen_api.get_storage_report, self._storage_loaded, "Scanning runtime files…")

    def _storage_loaded(self, payload: object) -> None:
        self._finish_task()
        self.storage = dict(payload) if isinstance(payload, dict) else {}
        self.storage_model.set_report(self.storage)
        total = StorageTableModel.format_size(int(self.storage.get("totalSizeBytes", 0) or 0))
        important = StorageTableModel.format_size(int(self.storage.get("importantSizeBytes", 0) or 0))
        generated = StorageTableModel.format_size(int(self.storage.get("generatedSizeBytes", 0) or 0))
        self.storage_total.setText(f"Storage {total} · Important {important} · Generated {generated}")
        self.banner.setText("All data remains on this device.")
        self._storage_selection_changed()

    def _storage_selection_changed(self, *_args) -> None:
        selected = self.storage_table.selectionModel().selectedRows() if self.storage_table.selectionModel() is not None else []
        row = self.storage_model.rows[selected[0].row()] if selected and selected[0].row() < len(self.storage_model.rows) else {}
        path = str(row.get("path", ""))
        self.delete_database_button.setEnabled(not self.busy and path.endswith(".db"))

    def _confirm_delete_selected_database(self) -> None:
        selected = self.storage_table.selectionModel().selectedRows()
        if not selected:
            return
        row_index = selected[0].row()
        if row_index < 0 or row_index >= len(self.storage_model.rows):
            return
        path = str(self.storage_model.rows[row_index].get("path", "")).strip()
        if not path.endswith(".db"):
            return
        confirm = ThemedMessageBox(self.theme, self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Delete Selected Database")
        confirm.setText(f"Permanently delete this database?\n{path}")
        confirm.setInformativeText("Its WAL and SHM sidecar files will also be removed. This cannot be undone.")
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if confirm.exec() != int(QMessageBox.StandardButton.Yes):
            return
        self._start_task(self.screen_api.delete_database, self._database_deleted, "Deleting selected database…", path)

    def _database_deleted(self, payload: object) -> None:
        self._finish_task()
        report = dict(payload) if isinstance(payload, dict) else {}
        self.banner.setText(f"Deleted database files; freed {StorageTableModel.format_size(int(report.get('freedBytes', 0) or 0))}.")
        self._load_storage()

    def _apply_settings(self) -> None:
        self._applying_settings = True
        sync = dict(self.settings.get("sync", {}))
        logging = dict(self.settings.get("logging", {}))
        analytics = dict(self.settings.get("analytics", {}))
        notifications = dict(self.settings.get("notifications", {}))
        self.refresh_minutes.setValue(int(sync.get("refreshMinutes", 10)))
        self.lookback_days.setValue(int(sync.get("lookbackDays", 14)))
        self.overlap_hours.setValue(int(sync.get("overlapHours", 2)))
        self.log_level.setCurrentText(str(logging.get("level", "INFO")))
        self.log_size_mb.setValue(float(logging.get("maxBytes", 2_000_000)) / 1_000_000.0)
        self.log_backups.setValue(int(logging.get("backupCount", 5)))
        self.analytics_enabled.setChecked(bool(analytics.get("enabled", True)))
        self.desktop_notifications.setChecked(bool(notifications.get("desktop", False)))
        self.sensitivity.setCurrentText(str(analytics.get("sensitivity", "normal")))
        self.minimum_history.setValue(int(analytics.get("minimumHistory", 5)))
        self.unusual_multiplier.setValue(float(analytics.get("unusualAmountMultiplier", 2.5)))
        self.robust_deviation.setValue(float(analytics.get("robustDeviation", 3.5)))
        self.comparison_months.setValue(int(analytics.get("comparisonMonths", 6)))
        self.minimum_comparison_months.setValue(int(analytics.get("minimumComparisonMonths", 3)))
        self.period_spike.setValue(float(analytics.get("periodSpikePercent", 30.0)))
        self.recurring_change.setValue(float(analytics.get("recurringChangePercent", 15.0)))
        self.recurring_confidence.setValue(float(analytics.get("recurringMinimumConfidence", 0.7)))
        self.duplicate_window.setValue(int(analytics.get("duplicateWindowMinutes", 10)))
        self.inr_transaction_floor.setValue(float(analytics.get("inrTransactionFloor", 500.0)))
        self.inr_period_floor.setValue(float(analytics.get("inrPeriodFloor", 1000.0)))
        self._applying_settings = False
        self.dirty = False

    def _settings_payload(self) -> dict[str, Any]:
        return {
            "sync": {
                "refreshMinutes": self.refresh_minutes.value(),
                "lookbackDays": self.lookback_days.value(),
                "overlapHours": self.overlap_hours.value(),
            },
            "logging": {
                "level": self.log_level.currentText(),
                "maxBytes": int(self.log_size_mb.value() * 1_000_000),
                "backupCount": self.log_backups.value(),
            },
            "analytics": {
                "enabled": self.analytics_enabled.isChecked(),
                "sensitivity": self.sensitivity.currentText(),
                "minimumHistory": self.minimum_history.value(),
                "unusualAmountMultiplier": self.unusual_multiplier.value(),
                "robustDeviation": self.robust_deviation.value(),
                "comparisonMonths": self.comparison_months.value(),
                "minimumComparisonMonths": self.minimum_comparison_months.value(),
                "periodSpikePercent": self.period_spike.value(),
                "recurringChangePercent": self.recurring_change.value(),
                "recurringMinimumConfidence": self.recurring_confidence.value(),
                "duplicateWindowMinutes": self.duplicate_window.value(),
                "inrTransactionFloor": self.inr_transaction_floor.value(),
                "inrPeriodFloor": self.inr_period_floor.value(),
            },
            "notifications": {"desktop": self.desktop_notifications.isChecked()},
        }

    def _save_settings(self) -> None:
        self._start_task(self.screen_api.save_settings, self._settings_saved, "Saving settings…", self._settings_payload())

    def _settings_saved(self, payload: object) -> None:
        self._finish_task()
        self.settings = dict(payload) if isinstance(payload, dict) else {}
        self._apply_settings()
        self.banner.setText("Settings saved; refresh timing changes apply after restart.")
        self.reload_cards("panel.expenses", "panel.expense_insights")

    def _mark_dirty(self, *_args) -> None:
        if self._applying_settings:
            return
        self.dirty = True
        if not self.busy:
            self.banner.setText("Unsaved settings changes.")

    def _choose_backup_destination(self) -> None:
        default = str(Path.home() / "ExpenseManager-Backup.expensemanager-backup")
        target, _ = QFileDialog.getSaveFileName(self, "Create ExpenseManager Backup", default, "ExpenseManager Backup (*.expensemanager-backup)")
        if not target:
            return
        warning = ThemedMessageBox(self.theme, self)
        warning.setIcon(QMessageBox.Icon.Warning)
        warning.setWindowTitle("Unencrypted Backup")
        warning.setText("This backup will not be encrypted.")
        warning.setInformativeText("Anyone with the file can read its financial data. Continue?")
        warning.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel)
        if warning.exec() != int(QMessageBox.StandardButton.Save):
            return
        self._start_task(self.screen_api.create_backup, self._backup_finished, "Creating consistent database backup…", target)

    def _backup_finished(self, payload: object) -> None:
        self._finish_task()
        report = dict(payload) if isinstance(payload, dict) else {}
        self.banner.setText(f"Backup created: {report.get('path', '')} ({StorageTableModel.format_size(int(report.get('sizeBytes', 0) or 0))})")
        self._load_storage()

    def _choose_restore_source(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "Restore ExpenseManager Backup", str(Path.home()), "ExpenseManager Backup (*.expensemanager-backup)")
        if not source:
            return
        confirm = ThemedMessageBox(self.theme, self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Restore Backup")
        confirm.setText("Replace current databases and configuration with this backup?")
        confirm.setInformativeText("The current files are snapshotted and automatically restored if the operation fails.")
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if confirm.exec() != int(QMessageBox.StandardButton.Yes):
            return
        self._start_task(self.screen_api.restore_backup, self._restore_finished, "Validating and restoring backup…", source)

    def _restore_finished(self, payload: object) -> None:
        self._finish_task()
        report = dict(payload) if isinstance(payload, dict) else {}
        self.banner.setText(f"Restored {int(report.get('restoredFiles', 0) or 0)} file(s).")
        self.reload_all()
        self._load_all()

    def _confirm_clear_generated(self) -> None:
        confirm = ThemedMessageBox(self.theme, self)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setWindowTitle("Clear Generated Data")
        confirm.setText("Delete derived state, caches, and rotating logs?")
        confirm.setInformativeText("Databases, configuration, and backups are kept. Derived state will be rebuilt.")
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if confirm.exec() != int(QMessageBox.StandardButton.Yes):
            return
        self._start_task(self.screen_api.clear_generated_data, self._clear_finished, "Clearing generated files…", ["state", "cache", "logs"])

    def _clear_finished(self, payload: object) -> None:
        self._finish_task()
        report = dict(payload) if isinstance(payload, dict) else {}
        self.banner.setText(f"Freed {StorageTableModel.format_size(int(report.get('freedBytes', 0) or 0))}.")
        self._load_storage()

    def _prepare_uninstall(self) -> None:
        dialog = ThemedMessageBox(self.theme, self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Prepare Uninstall")
        dialog.setText("Choose what ExpenseManager should retain.")
        dialog.setInformativeText("Keeping data is recommended. Generated cache, state, and logs are always removed.")
        keep_button = dialog.addButton("Keep Databases and Config", QMessageBox.ButtonRole.AcceptRole)
        delete_button = dialog.addButton("Delete All Data", QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked not in {keep_button, delete_button}:
            return
        keep = clicked is keep_button
        self._start_task(
            self.screen_api.prepare_uninstall,
            self._uninstall_prepared,
            "Preparing clean uninstall…",
            keep_important=keep,
        )

    def _uninstall_prepared(self, payload: object) -> None:
        self._finish_task()
        report = dict(payload) if isinstance(payload, dict) else {}
        self.banner.setText(
            "Uninstall preparation complete. Databases and configuration were retained."
            if report.get("keepImportant")
            else "Uninstall preparation complete. All application data was deleted by user choice."
        )
        self._load_storage()

    def _start_task(self, fn, finished, message: str, *args, **kwargs) -> None:
        if self.busy:
            return
        self.busy = True
        self._set_enabled(False)
        self.banner.setText(message)
        worker = _Worker(fn, *args, **kwargs)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(self._task_failed)
        self.thread_pool.start(worker)

    def _finish_task(self) -> None:
        self.busy = False
        self._set_enabled(True)

    def _task_failed(self, message: str) -> None:
        self._finish_task()
        self.banner.setText(f"Operation failed: {message}")

    def _set_enabled(self, enabled: bool) -> None:
        for button in (self.save_button, self.scan_button, self.backup_button, self.restore_button, self.clear_button, self.delete_database_button, self.uninstall_button):
            button.setEnabled(enabled)

    @staticmethod
    def _spin(minimum: int, maximum: int) -> QSpinBox:
        control = QSpinBox()
        control.setRange(minimum, maximum)
        return control

    @staticmethod
    def _double_spin(minimum: float, maximum: float, step: float) -> QDoubleSpinBox:
        control = QDoubleSpinBox()
        control.setRange(minimum, maximum)
        control.setSingleStep(step)
        control.setDecimals(2)
        return control


__all__ = ["ExpenseSettingsWidget", "StorageTableModel"]
