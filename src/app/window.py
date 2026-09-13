from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, QSettings, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget

from src.app.runtime import AppRuntime
from src.app.theme import AppTheme
from src.app.ui_kit import BusyOverlay, ThemedMessageBox, configure_ui_kit_theme


class JobSignals(QObject):
    """Bridge worker completion from background threads back to the UI thread."""

    finished = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)


class OverlaySignals(QObject):
    """Forward background progress payloads into the UI thread."""

    expenses_progress = pyqtSignal(object)
    reload_cards_requested = pyqtSignal(object)


class JobWorker(QRunnable):
    """Run one backend job in the shared Qt thread pool."""

    def __init__(self, job_id: str, handler) -> None:
        """Store the target job id and callable."""

        super().__init__()
        self.job_id = job_id
        self.handler = handler
        self.signals = JobSignals()

    def run(self) -> None:
        """Execute the job and publish success or failure."""

        try:
            result = self.handler()
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.job_id, str(exc))
            return
        self.signals.finished.emit(self.job_id, result)


class ExpenseManagerWindow(QMainWindow):
    """Host the standalone expense cards in a simple scrollable window."""

    BLOCKING_JOB_IDS = {"expenses.refresh", "expenses.rebuild"}
    DATA_JOB_IDS = {"expenses.refresh", "expenses.rebuild", "analytics.recompute"}

    def __init__(
        self,
        *,
        title: str,
        runtime: AppRuntime,
        screen_api,
        widget_mounts: dict[str, type[Any]],
        card_ids: Sequence[str],
        tab_targets: dict[str, str] | None = None,
    ) -> None:
        """Build the window, create the cards, and install job timers."""

        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.runtime = runtime
        self.files = runtime.files
        self.screen_api = screen_api
        self.widget_mounts = dict(widget_mounts)
        self.card_ids = tuple(card_ids)
        self.tab_targets = dict(tab_targets or {})
        self.job_specs = {spec.job_id: spec for spec in runtime.jobs}
        self.active_jobs: set[str] = set()
        self.thread_pool = QThreadPool.globalInstance()
        self.card_configs: dict[str, dict[str, Any]] = {}
        self.card_widgets: dict[str, QWidget] = {}
        self.card_visibility: dict[str, bool] = {}
        self.refresh_timers: list[QTimer] = []
        self.overlay_signals = OverlaySignals()
        self.overlay_signals.expenses_progress.connect(self._handle_expenses_progress)
        self.overlay_signals.reload_cards_requested.connect(self.reload_cards)
        self.blocking_overlay: BusyOverlay | None = None
        self.active_blocking_job_id = ""
        self.notification_tray: QSystemTrayIcon | None = None
        self._startup_tasks_started = False
        self._thunderbird_warning_shown = False
        self.window_settings = QSettings("ExpenseManager", "ExpenseManager")

        self.setWindowTitle(title)
        self._apply_window_icon()
        saved_geometry = self.window_settings.value("windowGeometry")
        if saved_geometry is None or not self.restoreGeometry(saved_geometry):
            self.resize(1440, 980)

        self.theme = AppTheme(self.files.theme_path())
        configure_ui_kit_theme(self.theme)
        self._apply_window_style()
        self.screen_api.attach_window(self)

        self._build_layout()
        self._install_blocking_overlay()
        self._bind_expenses_progress()
        self._install_refresh_timers()
        QTimer.singleShot(0, self._check_source_configuration_on_launch)
        QTimer.singleShot(0, self._run_startup_tasks)

    def reload_all_cards(self) -> None:
        """Reload every mounted card in place when possible."""

        self.reload_cards(self.card_ids)

    def request_card_reload(self, card_ids: Sequence[str]) -> None:
        """Queue a card reload onto the GUI thread from any worker thread."""

        self.overlay_signals.reload_cards_requested.emit(tuple(card_ids))

    def reload_cards(self, card_ids: Sequence[str]) -> None:
        """Reload the provided mounted cards in order, skipping unknown ids."""

        requested = [str(card_id).strip() for card_id in card_ids if str(card_id).strip()]
        seen: set[str] = set()
        for card_id in requested:
            if card_id in seen or card_id not in self.card_widgets:
                continue
            seen.add(card_id)
            try:
                self.reload_card(card_id)
            except Exception as exc:  # noqa: BLE001
                self.runtime.services.get("expenses").logger.exception("Card reload failed card_id=%s error=%s", card_id, exc)
                self._show_warning_dialog("Card Reload Failed", f"{card_id}\n\n{exc}")

    def reload_card(self, card_id: str) -> None:
        """Reload one card and replace the widget if it cannot update in place."""

        try:
            if card_id not in self.card_widgets:
                return
            card_config = self.files.load_card(card_id)
            widget_data = self.files.load_card_data(card_config)
            widget = self.card_widgets[card_id]
            if hasattr(widget, "apply_reload") and widget.apply_reload(card_config, widget_data):
                self.card_configs[card_id] = card_config
                return

            index = self.cards_layout.indexOf(widget)
            if index < 0:
                return
            widget.deleteLater()
            replacement = self._create_card_widget(card_id, card_config, widget_data)
            self.card_widgets[card_id] = replacement
            self.card_configs[card_id] = card_config
            visible = self.card_visibility.get(card_id, self._default_card_visibility(card_config))
            self.card_visibility[card_id] = visible
            replacement.setVisible(visible)
            callback = getattr(replacement, "on_card_visibility_changed", None)
            if callable(callback):
                callback(visible)
            self.cards_layout.insertWidget(index, replacement)
            return
        except Exception:
            self.runtime.services.get("expenses").logger.exception("reload_card crashed card_id=%s", card_id)
            raise

    def navigate_to_tab(self, tab_id: str) -> None:
        """Scroll the window to the card mapped to one logical tab id."""

        target = self.tab_targets.get(tab_id)
        if not target:
            return
        self.navigate_to_card(target)

    def navigate_to_card(self, card_id: str) -> None:
        """Make one card visible and scroll it into view from persistent navigation."""

        target = str(card_id).strip()
        if not target:
            return
        if not self.is_card_visible(target):
            self.set_card_visibility(target, True)
        widget = self.card_widgets.get(target)
        if widget is None:
            return
        self.scroll_area.ensureWidgetVisible(widget, 0, 24)

    def is_card_visible(self, card_id: str) -> bool:
        """Return whether one mounted card is currently visible."""

        return bool(self.card_visibility.get(card_id, True))

    def toggle_card_visibility(self, card_id: str, *, scroll_into_view: bool = False) -> bool:
        """Toggle one mounted card and return the new visibility state."""

        next_visible = not self.is_card_visible(card_id)
        self.set_card_visibility(card_id, next_visible, scroll_into_view=scroll_into_view)
        return next_visible

    def set_card_visibility(self, card_id: str, visible: bool, *, scroll_into_view: bool = False) -> None:
        """Show or hide one mounted card in place."""

        widget = self.card_widgets.get(card_id)
        if widget is None:
            return
        self.card_visibility[card_id] = bool(visible)
        widget.setVisible(bool(visible))
        callback = getattr(widget, "on_card_visibility_changed", None)
        if callable(callback):
            callback(bool(visible))
        if visible and scroll_into_view:
            self.scroll_area.ensureWidgetVisible(widget, 0, 24)

    def run_job(self, job_id: str, *, async_override: bool | None = None) -> None:
        """Run one registered backend job, optionally in the background."""

        spec = self.job_specs.get(job_id)
        if spec is None or job_id in self.active_jobs:
            return
        if job_id in self.DATA_JOB_IDS and self.active_jobs.intersection(self.DATA_JOB_IDS):
            return
        if job_id in {"expenses.refresh", "expenses.rebuild"} and not self._expenses_source_is_valid_for_job():
            return
        self.active_jobs.add(job_id)
        if self._should_block_on_job_start(job_id):
            self.active_blocking_job_id = job_id
            self._show_blocking_overlay(job_id, self._blocking_start_payload(job_id))
        self._notify_job_state(job_id, True, {"phase": "started"})
        should_async = spec.async_job if async_override is None else async_override
        if should_async:
            worker = JobWorker(job_id, spec.handler)
            worker.signals.finished.connect(self._handle_job_finished)
            worker.signals.failed.connect(self._handle_job_failed)
            self.thread_pool.start(worker)
            return
        try:
            spec.handler()
        except Exception as exc:  # noqa: BLE001
            self._handle_job_failed(job_id, str(exc))
            return
        self._handle_job_finished(job_id, None)

    def _run_startup_tasks(self) -> None:
        """Run startup refresh tasks after the first paint path is scheduled."""

        if self._startup_tasks_started:
            return
        self._startup_tasks_started = True
        for index, task in enumerate(self.runtime.startup_tasks):
            worker = JobWorker(f"startup.{index}", task)
            worker.signals.finished.connect(self._handle_startup_task_finished)
            worker.signals.failed.connect(self._handle_startup_task_failed)
            self.thread_pool.start(worker)

    def _handle_startup_task_finished(self, _job_id: str, _result: object) -> None:
        """Reload mounted cards after one deferred startup task finishes."""

        self.reload_all_cards()

    def _handle_startup_task_failed(self, job_id: str, message: str) -> None:
        """Log startup-task failures without preventing the window from opening."""

        self.logger.warning("Startup task failed job_id=%s error=%s", job_id, message)

    def _check_source_configuration_on_launch(self) -> None:
        """Show a launch warning only when no enabled local source is usable."""

        if self._thunderbird_warning_shown:
            return
        result = self._source_validation_result()
        if not result or bool(result.get("valid")):
            return
        self._thunderbird_warning_shown = True
        self._show_source_warning(result)

    def _expenses_source_is_valid_for_job(self) -> bool:
        result = self._source_validation_result()
        if not result or bool(result.get("valid")):
            return True
        self._show_source_warning(result)
        return False

    def _source_validation_result(self) -> dict[str, Any]:
        validator = getattr(self.screen_api, "validate_configured_sources", None)
        if callable(validator):
            result = validator()
            return dict(result) if isinstance(result, dict) else {}
        return self._thunderbird_validation_result()

    def _thunderbird_validation_result(self) -> dict[str, Any]:
        validator = getattr(self.screen_api, "validate_thunderbird_directory", None)
        if not callable(validator):
            return {}
        result = validator()
        return dict(result) if isinstance(result, dict) else {}

    def _show_source_warning(self, result: dict[str, Any]) -> None:
        message = str(result.get("message", "")).strip() or "Expense Manager could not find a valid local source to import."
        suggestions = [item for item in result.get("suggestedProfiles", []) if isinstance(item, dict)]
        if suggestions:
            first = suggestions[0]
            accounts = [item for item in first.get("accounts", []) if isinstance(item, dict)]
            account = str(accounts[0].get("email", "")).strip() if accounts else ""
            suffix = f"\n\nSuggested profile: {first.get('profilePath', '')}"
            if account:
                suffix += f"\nSuggested account: {account}"
            message += suffix
        self._show_warning_dialog(
            "No Usable Source",
            f"{message}\n\nOpen Sources and add or enable a valid local source before importing.",
        )

    def _build_layout(self) -> None:
        """Create persistent navigation and popup entry points above the dashboard."""

        container = QWidget()
        self.cards_layout = QVBoxLayout(container)
        self.cards_layout.setContentsMargins(10, 10, 10, 10)
        self.cards_layout.setSpacing(12)
        for card_id in self.card_ids:
            card_config = self.files.load_card(card_id)
            widget_data = self.files.load_card_data(card_config)
            widget = self._create_card_widget(card_id, card_config, widget_data)
            self.card_configs[card_id] = card_config
            self.card_widgets[card_id] = widget
            visible = self._default_card_visibility(card_config)
            self.card_visibility[card_id] = visible
            widget.setVisible(visible)
            callback = getattr(widget, "on_card_visibility_changed", None)
            if callable(callback):
                callback(visible)
            self.cards_layout.addWidget(widget)
        self.cards_layout.addStretch(1)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidget(container)

        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(10, 8, 10, 10)
        shell_layout.setSpacing(8)
        nav = QFrame()
        nav.setStyleSheet(
            f"QFrame {{ background: {self.theme.hex('surface_panel')}; border: 1px solid {self.theme.hex('divider')}; }}"
        )
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(8, 6, 8, 6)
        nav_layout.setSpacing(6)
        for label, card_id in (
            ("Overview", "panel.expenses"),
            ("Ledger", "panel.expenses_tab"),
            ("Insights", "panel.expense_insights"),
        ):
            button = self._make_top_window_button(label, f"Open {label}")
            button.clicked.connect(lambda _checked=False, target=card_id: self.navigate_to_card(target))
            nav_layout.addWidget(button)
        self.settings_button = self._make_top_window_button("SETTINGS", "Open settings and storage")
        self.settings_button.clicked.connect(self.open_settings_dialog)
        nav_layout.addWidget(self.settings_button)
        nav_layout.addStretch(1)
        self.sources_button = self._make_top_window_button("SOURCES", "Open source selection and import setup")
        self.sources_button.clicked.connect(self.open_sources_dialog)
        self.mail_config_button = self._make_top_window_button("MAIL CFG", "Open Thunderbird mail configuration")
        self.mail_config_button.clicked.connect(self.open_mail_config_dialog)
        nav_layout.addWidget(self.sources_button)
        nav_layout.addWidget(self.mail_config_button)
        shell_layout.addWidget(nav)
        shell_layout.addWidget(self.scroll_area, 1)
        self.setCentralWidget(shell)

    def _make_top_window_button(self, label: str, accessible_name: str) -> QPushButton:
        """Build one fixed top-window control with the shared sharp geometry."""

        button = QPushButton(label)
        button.setAccessibleName(accessible_name)
        button.setMinimumHeight(30)
        button.setToolTip(accessible_name)
        button.setStyleSheet(
            f"QPushButton {{ color: {self.theme.hex('text_primary')}; background: {self.theme.hex('surface_panel_alt')}; border: 1px solid {self.theme.hex('divider')}; border-radius: 0px; padding: 3px 9px; }}"
            f"QPushButton:hover, QPushButton:focus {{ border-color: {self.theme.hex('focus_ring', self.theme.hex('accent'))}; background: {self.theme.hex('surface_active')}; }}"
        )
        return button

    def open_sources_dialog(self) -> None:
        """Open source selection without moving the dashboard scroll position."""

        self._open_popup_card("panel.expenses_sources", "Sources", minimum_size=(1120, 720))

    def open_mail_config_dialog(self) -> None:
        """Open Thunderbird configuration without inserting it into the dashboard."""

        self._open_popup_card("panel.expenses_config", "Mail Configuration", minimum_size=(880, 560))

    def open_settings_dialog(self) -> None:
        """Open settings and storage without inserting it into the dashboard."""

        self._open_popup_card("panel.expenses_settings", "Settings & Storage", minimum_size=(1120, 760))

    def _open_popup_card(self, card_id: str, title: str, *, minimum_size: tuple[int, int]) -> None:
        """Mount a configured card inside a closable modal dialog.

        Popup cards are intentionally constructed on demand instead of reparenting
        dashboard widgets.  This keeps the dashboard geometry and scroll position
        stable while setup is open.
        """

        card_config = self.files.load_card(card_id)
        widget_data = self.files.load_card_data(card_config)
        widget = self._create_card_widget(card_id, card_config, widget_data)
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setWindowIcon(self.windowIcon())
        dialog.setModal(True)
        dialog.setMinimumSize(*minimum_size)
        dialog.resize(*minimum_size)
        dialog.setStyleSheet(
            f"QDialog {{ background: {self.theme.hex('window_bg')}; }}"
            f"QPushButton {{ border-radius: 0px; }}"
        )
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addWidget(widget, 1)
        close_button = self._make_top_window_button("CLOSE", f"Close {title}")
        close_button.clicked.connect(dialog.reject)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)
        dialog.exec()
        self.reload_cards(("panel.expenses", "panel.expense_insights", "panel.expenses_tab"))

    def _install_blocking_overlay(self) -> None:
        """Create the window-level blocking overlay used during expense sync jobs."""

        self.blocking_overlay = BusyOverlay(self.theme, self)
        self.blocking_overlay.setGeometry(self.rect())
        self.blocking_overlay.hide_overlay()

    def _bind_expenses_progress(self) -> None:
        """Subscribe to live expense progress updates emitted from background work."""

        expenses_service = self.runtime.services.get("expenses")
        callback = getattr(expenses_service, "add_progress_listener", None)
        if callable(callback):
            callback(self._forward_expenses_progress)

    def _create_card_widget(self, card_id: str, card_config: dict[str, Any], widget_data: dict[str, Any]) -> QWidget:
        """Create one widget instance from the local card definition."""

        widget_name = str(card_config.get("widget", "")).strip()
        widget_cls = self.widget_mounts[widget_name]
        widget = widget_cls(card_config, widget_data, self.theme, self.screen_api, self)
        if hasattr(widget, "bind_card_ref"):
            widget.bind_card_ref(card_id)
        return widget

    def _default_card_visibility(self, card_config: dict[str, Any]) -> bool:
        """Return whether one card should be visible on first mount."""

        visibility = card_config.get("visibility", {})
        if isinstance(visibility, dict):
            return bool(visibility.get("defaultVisible", True))
        return True

    def _install_refresh_timers(self) -> None:
        """Create interval timers for cards that declare background refresh."""

        job_seconds: dict[str, int] = {}
        for card_id in self.card_ids:
            refresh = self.card_configs.get(card_id, {}).get("refresh", {})
            if not isinstance(refresh, dict) or str(refresh.get("mode", "")).strip().lower() != "interval":
                continue
            job_id = str(refresh.get("job", "")).strip()
            seconds = self._refresh_interval_seconds(job_id, refresh)
            if not job_id or seconds <= 0:
                continue
            current = job_seconds.get(job_id)
            job_seconds[job_id] = seconds if current is None else min(current, seconds)

        for job_id, seconds in job_seconds.items():
            timer = QTimer(self)
            timer.setInterval(seconds * 1000)
            timer.timeout.connect(lambda job_id=job_id: self.run_job(job_id))
            timer.start()
            self.refresh_timers.append(timer)

    def _refresh_interval_seconds(self, job_id: str, refresh: dict[str, Any]) -> int:
        """Resolve one interval timer, allowing runtime sync config to override card JSON."""

        if job_id == "expenses.refresh":
            snapshot_fn = getattr(self.screen_api, "get_expense_mail_config_snapshot", None)
            if callable(snapshot_fn):
                snapshot = snapshot_fn()
                sync = snapshot.get("sync", {}) if isinstance(snapshot, dict) else {}
                source_validation = snapshot.get("sourceValidation", {}) if isinstance(snapshot, dict) and isinstance(snapshot.get("sourceValidation", {}), dict) else {}
                if source_validation and not bool(source_validation.get("valid")):
                    return 0
                refresh_minutes = int((sync or {}).get("refreshMinutes", 0) or 0)
                if refresh_minutes > 0:
                    return refresh_minutes * 60
        return int(refresh.get("seconds", 0) or 0)

    def _notify_job_state(self, job_id: str, running: bool, payload: dict[str, Any]) -> None:
        """Forward job state into widgets that expose `set_job_state`."""

        for widget in self.card_widgets.values():
            callback = getattr(widget, "set_job_state", None)
            if callable(callback):
                callback(job_id, running, payload)

    def _handle_job_finished(self, job_id: str, payload: object) -> None:
        """Reload the UI after a successful background job."""

        self.active_jobs.discard(job_id)
        self._sync_blocking_overlay_after_job(job_id)
        self._notify_job_state(job_id, False, {"phase": "completed", "payload": payload})
        self.reload_cards(self._reload_scope_for_job(job_id))
        self._show_insight_notification(job_id, payload)

    def _show_insight_notification(self, job_id: str, payload: object) -> None:
        """Show an opt-in local desktop notification for newly generated insights."""

        if job_id not in {"analytics.recompute", "expenses.refresh", "expenses.rebuild"} or not isinstance(payload, dict):
            return
        inserted = int(payload.get("inserted", 0) or 0)
        if inserted <= 0:
            return
        settings_fn = getattr(self.screen_api, "get_settings_snapshot", None)
        settings = settings_fn() if callable(settings_fn) else {}
        if not bool(dict(settings.get("notifications", {})).get("desktop", False)):
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.logger.info("Desktop insight notification skipped because no system tray is available")
            return
        if self.notification_tray is None:
            self.notification_tray = QSystemTrayIcon(self.windowIcon(), self)
            self.notification_tray.show()
        self.notification_tray.showMessage(
            "ExpenseManager Insights",
            f"{inserted} new local insight{'s' if inserted != 1 else ''} available.",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def _handle_job_failed(self, job_id: str, message: str) -> None:
        """Surface background job failures and keep the UI mounted."""

        self.active_jobs.discard(job_id)
        self._sync_blocking_overlay_after_job(job_id)
        self._notify_job_state(job_id, False, {"phase": "failed", "message": message})
        self.reload_all_cards()
        self._show_warning_dialog("Background Job Failed", f"{job_id}\n\n{message}")

    def _forward_expenses_progress(self, payload: dict[str, Any]) -> None:
        """Bridge one expense progress payload from a worker thread into the UI thread."""

        self.overlay_signals.expenses_progress.emit(dict(payload))

    def _handle_expenses_progress(self, payload: object) -> None:
        """Refresh the blocking overlay from live expense progress."""

        if not isinstance(payload, dict):
            return
        job_id = self._current_blocking_job_id()
        if not job_id:
            return
        if not self._should_show_overlay_for_progress(job_id, payload):
            if job_id == "expenses.refresh":
                self._hide_blocking_overlay()
            return
        self.active_blocking_job_id = job_id
        self._show_blocking_overlay(job_id, payload)

    def _current_blocking_job_id(self) -> str:
        """Return the blocking job currently responsible for the overlay."""

        if self.active_blocking_job_id in self.active_jobs:
            return self.active_blocking_job_id
        for job_id in self.BLOCKING_JOB_IDS:
            if job_id in self.active_jobs:
                return job_id
        return ""

    def _blocking_start_payload(self, job_id: str) -> dict[str, Any]:
        """Return the initial overlay payload shown before live progress arrives."""

        return {
            "running": True,
            "label": "Refreshing expenses" if job_id == "expenses.refresh" else "Rebuilding expenses",
            "processed": 0,
            "total": 0,
            "percent": 0,
            "providerId": "",
        }

    def _should_block_on_job_start(self, job_id: str) -> bool:
        """Return whether one job should block immediately on launch."""

        return job_id == "expenses.rebuild"

    def _should_show_overlay_for_progress(self, job_id: str, payload: dict[str, Any]) -> bool:
        """Return whether one progress payload should surface the blocking overlay."""

        if job_id == "expenses.rebuild":
            return True
        if job_id != "expenses.refresh":
            return False
        return bool(payload.get("blockUi", False))

    def _show_blocking_overlay(self, job_id: str, payload: dict[str, Any]) -> None:
        """Show or refresh the full-window blocking overlay."""

        if self.blocking_overlay is None:
            return
        self.blocking_overlay.setGeometry(self.rect())
        self.blocking_overlay.set_progress(job_id, payload)
        if not self.blocking_overlay.isVisible():
            self.blocking_overlay.show_overlay()
        else:
            self.blocking_overlay.raise_()

    def _hide_blocking_overlay(self) -> None:
        """Hide the full-window blocking overlay."""

        if self.blocking_overlay is not None and self.blocking_overlay.isVisible():
            self.blocking_overlay.hide_overlay()
        self.active_blocking_job_id = ""

    def _sync_blocking_overlay_after_job(self, finished_job_id: str) -> None:
        """Refresh overlay ownership after one job completes or fails."""

        if finished_job_id not in self.BLOCKING_JOB_IDS:
            return
        next_job_id = self._current_blocking_job_id()
        if next_job_id:
            self.active_blocking_job_id = next_job_id
            self._show_blocking_overlay(next_job_id, self._blocking_start_payload(next_job_id))
            return
        self._hide_blocking_overlay()

    def _show_warning_dialog(self, title: str, text: str) -> None:
        """Show one themed warning popup."""

        dialog = ThemedMessageBox(self.theme, self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(title)
        dialog.setText(text)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.setDefaultButton(QMessageBox.StandardButton.Ok)
        dialog.exec()

    def _apply_window_style(self) -> None:
        """Apply the shared app background and typography."""

        font = QFont(self.theme.font_family(), 10)
        QApplication.instance().setFont(font)
        self.setStyleSheet(
            f"""
            QMainWindow {{
                background-color: {self.theme.hex("window_bg")};
            }}
            QScrollArea {{
                background-color: {self.theme.hex("window_bg")};
                border: none;
            }}
            QScrollBar:vertical {{
                background: {self.theme.hex("surface_panel_alt")};
                width: 10px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {self.theme.hex("divider")};
                min-height: 28px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QCheckBox {{ color: {self.theme.hex("text_primary")}; spacing: 6px; }}
            QCheckBox::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {self.theme.hex("divider")};
                background: {self.theme.hex("surface_panel_alt")};
            }}
            QCheckBox::indicator:checked {{
                background: {self.theme.hex("accent")};
                border-color: {self.theme.hex("accent")};
            }}
            QWidget {{
                background-color: transparent;
            }}
            QMessageBox {{
                background-color: {self.theme.hex("surface_strong")};
            }}
            """
        )

    def _apply_window_icon(self) -> None:
        """Apply the shipped application icon to the Qt app and window."""

        icon_path = self.files.app_icon_ico_path() if os.name == "nt" else self.files.app_icon_path()
        if not icon_path.exists() and icon_path.suffix.lower() == ".ico":
            icon_path = self.files.app_icon_path()
        if not icon_path.exists():
            self.logger.warning("Application icon not found path=%s", icon_path)
            return
        app_icon = QIcon(str(icon_path))
        if app_icon.isNull():
            self.logger.warning("Application icon could not be loaded path=%s", icon_path)
            return
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(app_icon)
        self.setWindowIcon(app_icon)

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Keep the blocking overlay sized to the current window."""

        super().resizeEvent(event)
        if self.blocking_overlay is not None:
            self.blocking_overlay.setGeometry(self.rect())
            if self.blocking_overlay.isVisible():
                self.blocking_overlay.raise_()

    def closeEvent(self, event) -> None:  # noqa: N802
        """Persist the user's window geometry for the next launch."""

        self.window_settings.setValue("windowGeometry", self.saveGeometry())
        super().closeEvent(event)

    @staticmethod
    def _reload_scope_for_job(job_id: str) -> tuple[str, ...]:
        """Keep unrelated cards and unsaved settings untouched after a job finishes."""

        if job_id in {"expenses.refresh", "expenses.rebuild"}:
            return ("panel.expenses", "panel.expense_insights", "panel.expenses_debug", "panel.expenses_tab")
        if job_id == "analytics.recompute":
            return ("panel.expenses", "panel.expense_insights")
        return ()


def run_app_window(
    *,
    title: str,
    runtime: AppRuntime,
    screen_api,
    widget_mounts: dict[str, type[Any]],
    card_ids: Sequence[str],
    tab_targets: dict[str, str] | None = None,
) -> int:
    """Create the Qt application, mount the standalone window, and run it."""

    _apply_windows_app_user_model_id()
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(title)
    app.setOrganizationName("ExpenseManager")
    app.setDesktopFileName("expense-manager")
    window = ExpenseManagerWindow(
        title=title,
        runtime=runtime,
        screen_api=screen_api,
        widget_mounts=widget_mounts,
        card_ids=card_ids,
        tab_targets=tab_targets,
    )
    window.show()
    autoclose_ms = int(os.environ.get("NOC_AUTOCLOSE_MS", "0") or 0)
    if autoclose_ms > 0:
        QTimer.singleShot(autoclose_ms, app.quit)
    return app.exec()


def _apply_windows_app_user_model_id() -> None:
    """Give Windows taskbar grouping an app id distinct from python.exe."""

    if os.name != "nt":
        return
    app_id = "expense-manager-pyqt"
    try:
        import ctypes

        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long
        result = setter(app_id)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("Windows AppUserModelID setup failed app_id=%s error=%s", app_id, exc)
        return
    if result != 0:
        logging.getLogger(__name__).warning("Windows AppUserModelID setup failed app_id=%s hresult=%s", app_id, result)
