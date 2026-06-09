from __future__ import annotations

from datetime import datetime
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.app.ui_kit import BaseWidget, CardFrame, SectionCard, make_button, make_label


class _TaskSignals(QObject):
    """Bridge one widget-scoped background task back to the UI thread."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class _TaskWorker(QRunnable):
    """Run one small screen-api call away from the main UI thread."""

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


class ExpensesConfigWidget(BaseWidget):
    """Render the Thunderbird account-selection card for the expenses feature."""

    def __init__(self, widget_config, widget_data, theme, screen_api, window_api, parent=None):
        super().__init__(widget_config, widget_data, theme, screen_api, window_api, parent)
        self.snapshot = self.screen_api.get_expense_mail_config_snapshot()
        self.discovery_running = False
        self.save_running = False
        self.expenses_job_running = False
        self.expenses_job_label = ""
        self.discovery_error = ""
        self.discovery_warnings: list[str] = []
        self.inline_message = ""
        self.inline_tone = "muted"
        self.accounts: list[dict[str, Any]] = []
        self._build_ui()
        self._load_snapshot_fields(preserve_dirty=False)
        self._refresh_view_state()

    def bind_card_ref(self, card_ref: str) -> None:
        """Bind the card id and start lazy discovery only when visible."""

        super().bind_card_ref(card_ref)
        if self.is_card_visible():
            QTimer.singleShot(0, self._auto_discover_from_snapshot)

    def apply_reload(self, widget_config: dict[str, Any], widget_data: dict[str, Any]) -> bool:
        """Refresh the card in place while preserving local discovery state."""

        self.widget_config = widget_config
        self.widget_data = widget_data
        self.datasource_file = self._describe_datasource(widget_config)
        self.snapshot = self.screen_api.get_expense_mail_config_snapshot()
        self._load_snapshot_fields(preserve_dirty=True)
        self._refresh_view_state()
        if self.profile_input.text().strip() and not self.discovery_running and self.is_card_visible():
            QTimer.singleShot(0, self._auto_discover_from_snapshot)
        return True

    def on_card_visibility_changed(self, visible: bool) -> None:
        """Start lazy account discovery when the hidden pane is opened."""

        if visible:
            QTimer.singleShot(0, self._auto_discover_from_snapshot)

    def set_job_state(self, job_id: str, running: bool, payload: dict | None = None) -> None:
        """Disable config mutation while expense sync jobs run."""

        if job_id not in {"expenses.refresh", "expenses.rebuild"}:
            return
        self.expenses_job_running = running
        phase = str((payload or {}).get("phase", "")).strip().lower()
        if running:
            self.expenses_job_label = "Updating account DB..." if job_id == "expenses.refresh" else "Rebuilding account DB..."
        elif phase == "failed":
            self.expenses_job_label = "Expenses sync failed."
        else:
            self.expenses_job_label = ""
        self._refresh_view_state()

    def _build_ui(self) -> None:
        """Build the fixed config-card layout once."""

        self.setStyleSheet(
            f"""
            QWidget {{
                color: {self.theme.hex("text_primary")};
            }}
            QLineEdit, QComboBox {{
                color: {self.theme.hex("text_primary")};
                background-color: {self.theme.hex("surface_panel_alt")};
                border: 1px solid {self.theme.hex("divider", self.theme.hex("border"))};
                padding: 4px 6px;
                min-height: 28px;
                selection-background-color: {self.theme.hex("surface_active", self.theme.hex("accent"))};
            }}
            QLineEdit:focus, QComboBox:focus {{
                border-color: {self.theme.hex("focus_ring", self.theme.hex("accent"))};
            }}
            QLineEdit:disabled, QComboBox:disabled {{
                color: {self.theme.hex("text_disabled")};
                background-color: {self.theme.hex("surface_disabled", self.theme.hex("surface_panel_alt"))};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 18px;
            }}
            QComboBox QAbstractItemView {{
                color: {self.theme.hex("text_primary")};
                background-color: {self.theme.hex("surface_panel")};
                border: 1px solid {self.theme.hex("divider", self.theme.hex("border"))};
                selection-background-color: {self.theme.hex("surface_active", self.theme.hex("accent"))};
            }}
            """
        )

        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(0)

        self.card = SectionCard(
            "Expense Mail Config",
            "Pick the active Thunderbird account.",
            bg=self.theme.hex("surface_panel"),
            border=self.theme.hex("divider", self.theme.hex("border")),
        )
        self.header_status_label = QLabel()
        self.header_status_label.setStyleSheet(
            f"QLabel {{ color: {self.theme.hex('text_secondary')}; background: transparent; border: none; }}"
        )
        self.card.set_header_aux_widget(self.header_status_label, breakpoint=980)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(2, 2, 2, 2)
        body_layout.setSpacing(6)

        form = QWidget()
        form_layout = QGridLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setHorizontalSpacing(6)
        form_layout.setVerticalSpacing(4)

        profile_label = self._make_field_label("Thunderbird Profile")
        self.profile_input = QLineEdit()
        self.profile_input.setPlaceholderText(r"C:\Users\<you>\AppData\Roaming\Thunderbird\Profiles\...")
        self.profile_input.textChanged.connect(self._handle_profile_text_changed)

        self.browse_button = make_button(
            "Browse...",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.browse_button.clicked.connect(self._choose_profile_path)
        self._compact_button(self.browse_button)

        self.reload_button = make_button(
            "Reload Accounts",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.reload_button.setToolTip("Inspect the selected Thunderbird profile and list usable IMAP accounts.")
        self.reload_button.clicked.connect(self._start_discovery)
        self._compact_button(self.reload_button)

        account_label = self._make_field_label("Expense Account")
        self.account_combo = QComboBox()
        self.account_combo.currentIndexChanged.connect(self._handle_account_changed)

        mailbox_label = self._make_field_label("Resolved Mailbox")
        self.mailbox_value = QLineEdit()
        self.mailbox_value.setReadOnly(True)
        self.mailbox_value.setPlaceholderText("No mailbox selected.")

        form_layout.addWidget(profile_label, 0, 0)
        form_layout.addWidget(self.profile_input, 0, 1)
        form_layout.addWidget(self.browse_button, 0, 2)
        form_layout.addWidget(self.reload_button, 0, 3)
        form_layout.addWidget(account_label, 1, 0)
        form_layout.addWidget(self.account_combo, 1, 1)
        form_layout.addWidget(mailbox_label, 1, 2)
        form_layout.addWidget(self.mailbox_value, 1, 3)
        form_layout.setColumnStretch(1, 1)
        form_layout.setColumnStretch(3, 1)
        form_layout.setColumnMinimumWidth(0, 132)
        form_layout.setColumnMinimumWidth(2, 124)
        body_layout.addWidget(form)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(
            f"""
            QLabel {{
                background-color: {self.theme.hex("surface_panel_alt")};
                color: {self.theme.hex("text_secondary")};
                border: 1px solid {self.theme.hex("divider", self.theme.hex("border"))};
                padding: 5px 8px;
            }}
            """
        )
        body_layout.addWidget(self.banner)

        self.db_frame = CardFrame(
            bg=self.theme.hex("surface_panel_alt"),
            border=self.theme.hex("divider", self.theme.hex("border")),
            radius=0,
        )
        db_layout = QGridLayout(self.db_frame)
        db_layout.setContentsMargins(10, 8, 10, 8)
        db_layout.setHorizontalSpacing(10)
        db_layout.setVerticalSpacing(3)

        self.db_state_value = self._make_value_label("No account selected")
        self.db_transactions_value = self._make_value_label("0")
        self.db_sources_value = self._make_value_label("0")
        self.db_checkpoint_value = self._make_value_label("Never")
        self.db_path_value = QLabel("")
        self.db_path_value.setWordWrap(True)
        self.db_path_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.db_path_value.setStyleSheet(
            f"QLabel {{ color: {self.theme.hex('text_muted')}; background: transparent; border: none; }}"
        )

        db_layout.addWidget(self._make_field_label("DB State"), 0, 0)
        db_layout.addWidget(self._make_field_label("Transactions"), 0, 1)
        db_layout.addWidget(self._make_field_label("Source Records"), 0, 2)
        db_layout.addWidget(self._make_field_label("Last Checkpoint"), 0, 3)
        db_layout.addWidget(self.db_state_value, 1, 0)
        db_layout.addWidget(self.db_transactions_value, 1, 1)
        db_layout.addWidget(self.db_sources_value, 1, 2)
        db_layout.addWidget(self.db_checkpoint_value, 1, 3)
        db_layout.addWidget(self._make_field_label("DB Path"), 2, 0, 1, 4)
        db_layout.addWidget(self.db_path_value, 3, 0, 1, 4)
        for column in range(4):
            db_layout.setColumnStretch(column, 1)
        self.db_frame.hide()
        body_layout.addWidget(self.db_frame)

        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(6)

        self.save_button = make_button(
            "Save",
            self.theme.hex("accent"),
            self.theme.hex("text_primary"),
            self.theme.hex("accent"),
            emphasis="primary",
        )
        self.save_button.setToolTip("Save the selected Thunderbird account and switch the visible ledger to its dedicated DB.")
        self.save_button.clicked.connect(self._save_selected_account)
        self._compact_button(self.save_button)

        self.primary_action_button = make_button(
            "Update Now",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.primary_action_button.clicked.connect(self._run_primary_action)
        self._compact_button(self.primary_action_button)

        self.advanced_button = make_button(
            "Advanced",
            self.theme.hex("divider", self.theme.hex("border")),
            self.theme.hex("text_primary"),
            self.theme.hex("surface_panel_alt"),
        )
        self.advanced_button.setToolTip("Show the DB summary row and open the mail-debug pane.")
        self.advanced_button.clicked.connect(self._toggle_advanced_pane)
        self._compact_button(self.advanced_button)

        actions_layout.addWidget(self.save_button)
        actions_layout.addWidget(self.primary_action_button)
        actions_layout.addWidget(self.advanced_button)
        actions_layout.addStretch(1)
        body_layout.addWidget(actions)

        self.card.add_content_widget(body)
        self.root.addWidget(self.card)

    def _make_field_label(self, text: str, row_span: int = 1, column_span: int = 1):
        """Create one compact uppercase field label."""

        label = make_label(text.upper(), self.theme.hex("text_secondary"), 8, True, mono=True)
        label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return label

    def _make_value_label(self, text: str) -> QLabel:
        """Create one value label inside the DB summary frame."""

        label = QLabel(text)
        label.setStyleSheet(
            f"QLabel {{ color: {self.theme.hex('text_primary')}; background: transparent; border: none; font-weight: 600; }}"
        )
        return label

    def _compact_button(self, button) -> None:
        """Apply one denser local button treatment for the config pane."""

        button.setMinimumHeight(28)
        button.setMaximumHeight(28)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        button.setStyleSheet(
            button.styleSheet()
            + """
            QPushButton {
                min-height: 28px;
                padding: 2px 8px;
            }
            """
        )

    def _auto_discover_from_snapshot(self) -> None:
        """Discover accounts on first paint when a profile path already exists."""

        if not self.is_card_visible():
            return
        if self.discovery_running or self.accounts or not self.profile_input.text().strip():
            return
        self._start_discovery()

    def _handle_profile_text_changed(self) -> None:
        """Refresh UI affordances when the profile path changes."""

        self.discovery_error = ""
        self.accounts = []
        self._populate_accounts([], preferred_email="")
        if not self.profile_input.text().strip():
            self.inline_message = ""
        self._refresh_view_state()

    def _handle_account_changed(self) -> None:
        """Refresh the mailbox preview and DB summary when selection changes."""

        self._refresh_view_state()

    def _choose_profile_path(self) -> None:
        """Open a folder picker for the Thunderbird profile root."""

        current = self.profile_input.text().strip()
        selected = QFileDialog.getExistingDirectory(self, "Select Thunderbird Profile", current or "")
        if not selected:
            return
        self.profile_input.setText(selected)
        self._start_discovery()

    def _start_discovery(self) -> None:
        """Run Thunderbird account discovery in the background."""

        if self.discovery_running or self.save_running:
            return
        profile_path = self.profile_input.text().strip()
        if not profile_path:
            self.discovery_error = "Select a Thunderbird profile directory first."
            self.discovery_warnings = []
            self.accounts = []
            self._populate_accounts([], preferred_email="")
            self._refresh_view_state()
            return
        self.discovery_running = True
        self.discovery_error = ""
        self.inline_message = ""
        self.discovery_warnings = []
        self._refresh_view_state()
        worker = _TaskWorker(self.screen_api.discover_thunderbird_accounts, profile_path)
        worker.signals.finished.connect(self._handle_discovery_success)
        worker.signals.failed.connect(self._handle_discovery_failed)
        self.window_api.thread_pool.start(worker)

    def _handle_discovery_success(self, result: object) -> None:
        """Apply one completed discovery result."""

        self.discovery_running = False
        payload = dict(result) if isinstance(result, dict) else {}
        self.discovery_error = ""
        self.discovery_warnings = [str(item) for item in payload.get("warnings", []) if str(item).strip()]
        self.accounts = [dict(item) for item in payload.get("accounts", []) if isinstance(item, dict)]
        preferred_email = self._preferred_selected_email(payload)
        self._populate_accounts(self.accounts, preferred_email=preferred_email)
        if not self.accounts:
            self.inline_message = "No usable Thunderbird IMAP accounts were found in that profile."
            self.inline_tone = "warning"
        elif self.discovery_warnings:
            self.inline_message = self.discovery_warnings[0]
            self.inline_tone = "warning"
        else:
            self.inline_message = ""
        self._refresh_view_state()

    def _handle_discovery_failed(self, message: str) -> None:
        """Apply one failed discovery result."""

        self.discovery_running = False
        self.discovery_error = str(message or "Thunderbird account discovery failed.")
        self.discovery_warnings = []
        self.accounts = []
        self._populate_accounts([], preferred_email="")
        self._refresh_view_state()

    def _populate_accounts(self, accounts: list[dict[str, Any]], *, preferred_email: str) -> None:
        """Rebuild the account combo box from the latest discovery payload."""

        current_email = self._selected_account_email()
        target_email = preferred_email or current_email or str(self.snapshot.get("activeAccountEmail", "")).strip()
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in accounts:
            account_email = str(account.get("email", "")).strip()
            self.account_combo.addItem(self._account_label(account), account)
            if account_email and account_email == target_email:
                self.account_combo.setCurrentIndex(self.account_combo.count() - 1)
        if self.account_combo.count() > 0 and self.account_combo.currentIndex() < 0:
            self.account_combo.setCurrentIndex(0)
        self.account_combo.blockSignals(False)

    def _account_label(self, account: dict[str, Any]) -> str:
        """Return the combo-box display text for one discovered account."""

        display_name = str(account.get("displayName", "")).strip()
        email_value = str(account.get("email", "")).strip()
        server_name = str(account.get("serverName", "")).strip()
        if not display_name or display_name.lower() == email_value.lower():
            label = email_value
        else:
            label = f"{display_name} <{email_value}>"
        if server_name:
            return f"{label} | {server_name}"
        return label

    def _save_selected_account(self) -> None:
        """Persist the selected Thunderbird account and switch the active DB."""

        if self.save_running or self.discovery_running or self.expenses_job_running:
            return
        profile_path = self.profile_input.text().strip()
        account_email = self._selected_account_email()
        self.save_running = True
        self.discovery_error = ""
        self.inline_message = ""
        self._refresh_view_state()
        worker = _TaskWorker(self.screen_api.save_expense_mail_config, profile_path, account_email)
        worker.signals.finished.connect(self._handle_save_success)
        worker.signals.failed.connect(self._handle_save_failed)
        self.window_api.thread_pool.start(worker)

    def _handle_save_success(self, result: object) -> None:
        """Refresh the app after a successful account switch."""

        self.save_running = False
        payload = dict(result) if isinstance(result, dict) else {}
        self.snapshot = self.screen_api.get_expense_mail_config_snapshot()
        self._load_snapshot_fields(preserve_dirty=False)
        self.inline_message = (
            "Saved account and queued a delta refresh."
            if bool(payload.get("hadExistingDb"))
            else "Saved account. This DB has no cached expense data yet; use Rebuild Now."
        )
        self.inline_tone = "success" if bool(payload.get("hadExistingDb")) else "warning"
        self.reload_cards("panel.expenses", "panel.expenses_debug", "panel.expenses_tab")
        if bool(payload.get("hadExistingDb")):
            self.screen_api.run_refresh()
        else:
            self._refresh_view_state()

    def _handle_save_failed(self, message: str) -> None:
        """Show one failed save/switch result."""

        self.save_running = False
        self.inline_message = str(message or "Could not save the selected Thunderbird account.")
        self.inline_tone = "error"
        self._refresh_view_state()

    def _run_primary_action(self) -> None:
        """Run the delta refresh or full rebuild for the active saved account."""

        if self._has_unsaved_changes() or self.expenses_job_running:
            return
        status = self._display_db_status()
        if bool(status.get("hasCachedData")):
            self.screen_api.run_refresh()
            return
        self.screen_api.run_rebuild()

    def _toggle_advanced_pane(self) -> None:
        """Show or hide the advanced debug surfaces for this pane."""

        visible = self.toggle_card("panel.expenses_debug", scroll_into_view=True)
        self.db_frame.setVisible(visible)
        self.advanced_button.setText("Hide Advanced" if visible else "Advanced")

    def _load_snapshot_fields(self, *, preserve_dirty: bool) -> None:
        """Apply the saved config into the editable fields when appropriate."""

        if preserve_dirty and self._has_unsaved_changes():
            return
        profile_path = str(self.snapshot.get("profilePath", "")).strip()
        self.profile_input.blockSignals(True)
        self.profile_input.setText(profile_path)
        self.profile_input.blockSignals(False)
        self.header_status_label.setText(self._saved_account_badge_text())

    def _active_account_text(self) -> str:
        """Return the saved-active-account hint shown under the form."""

        active_account = str(self.snapshot.get("activeAccountEmail", "")).strip()
        if not active_account:
            return "No Thunderbird expense account is saved yet."
        return f"Saved account: {active_account}"

    def _saved_account_badge_text(self) -> str:
        """Return the compact header-side saved-account label."""

        active_account = str(self.snapshot.get("activeAccountEmail", "")).strip()
        if not active_account:
            return "No saved account"
        if len(active_account) > 38:
            active_account = f"{active_account[:35]}..."
        return f"Saved: {active_account}"

    def _preferred_selected_email(self, discovery_payload: dict[str, Any]) -> str:
        """Return the email that should stay selected after discovery reloads."""

        if self._has_unsaved_changes():
            current = self._selected_account_email()
            if current:
                return current
        current_snapshot = str(self.snapshot.get("activeAccountEmail", "")).strip()
        if current_snapshot:
            return current_snapshot
        return str(discovery_payload.get("selectedAccountEmail", "")).strip()

    def _selected_account_email(self) -> str:
        """Return the currently selected discovered-account email."""

        account = self.account_combo.currentData()
        if isinstance(account, dict):
            return str(account.get("email", "")).strip()
        return ""

    def _selected_account(self) -> dict[str, Any] | None:
        """Return the currently selected discovered-account record."""

        account = self.account_combo.currentData()
        return dict(account) if isinstance(account, dict) else None

    def _has_unsaved_changes(self) -> bool:
        """Return whether the current form differs from the saved active selection."""

        saved_profile = str(self.snapshot.get("profilePath", "")).strip()
        saved_account = str(self.snapshot.get("activeAccountEmail", "")).strip()
        current_profile = self.profile_input.text().strip()
        current_account = self._selected_account_email()
        if current_profile != saved_profile:
            return True
        if current_account and current_account != saved_account:
            return True
        return False

    def _display_db_status(self) -> dict[str, Any]:
        """Return the DB status currently relevant to the UI."""

        selected_account = self._selected_account()
        if isinstance(selected_account, dict) and isinstance(selected_account.get("dbStatus"), dict):
            return dict(selected_account.get("dbStatus", {}))
        snapshot_status = self.snapshot.get("accountDb", {})
        return dict(snapshot_status) if isinstance(snapshot_status, dict) else {}

    def _refresh_view_state(self) -> None:
        """Refresh button state, banner text, mailbox preview, and DB summary."""

        selected_account = self._selected_account()
        if selected_account is not None:
            self.mailbox_value.setText(str(selected_account.get("defaultMailboxRel", "")).strip())
            self.mailbox_value.setToolTip(str(selected_account.get("defaultMailboxAbs", "")).strip())
        else:
            self.mailbox_value.setText(str(self.snapshot.get("selectedMailboxPath", "")).strip())
            self.mailbox_value.setToolTip("")

        db_status = self._display_db_status()
        self.db_state_value.setText(self._db_state_label(db_status))
        self.db_transactions_value.setText(f"{int(db_status.get('transactionCount', 0) or 0):,}")
        self.db_sources_value.setText(f"{int(db_status.get('sourceRecordCount', 0) or 0):,}")
        self.db_checkpoint_value.setText(self._format_timestamp(str(db_status.get("lastReceivedAt", "")).strip()))
        self.db_path_value.setText(str(db_status.get("dbPath", "")).strip() or "No account DB selected.")

        save_enabled = (
            not self.discovery_running
            and not self.save_running
            and not self.expenses_job_running
            and bool(self.profile_input.text().strip())
            and bool(self._selected_account_email())
            and self._has_unsaved_changes()
        )
        primary_enabled = not self.discovery_running and not self.save_running and not self.expenses_job_running and not self._has_unsaved_changes()
        self.save_button.setEnabled(save_enabled)
        self.primary_action_button.setEnabled(primary_enabled)
        self.advanced_button.setEnabled(not self.discovery_running and not self.save_running)
        self.profile_input.setEnabled(not self.save_running and not self.expenses_job_running)
        self.account_combo.setEnabled(not self.save_running and not self.expenses_job_running and (self.account_combo.count() > 0))
        self.reload_button.setEnabled(not self.save_running and bool(self.profile_input.text().strip()))
        self.browse_button.setEnabled(not self.save_running and not self.expenses_job_running)
        advanced_visible = self.is_card_visible("panel.expenses_debug")
        self.db_frame.setVisible(advanced_visible)
        self.advanced_button.setText("Hide Advanced" if advanced_visible else "Advanced")
        self.header_status_label.setText(self._saved_account_badge_text())

        has_cached_data = bool(db_status.get("hasCachedData"))
        self.primary_action_button.setText("Update Now" if has_cached_data else "Rebuild Now")
        self.primary_action_button.setToolTip(
            "Run an incremental mailbox refresh against the active account DB."
            if has_cached_data
            else "Run a first-time full mailbox rebuild for the active account DB."
        )

        tone_text, tone_name = self._banner_text()
        self.banner.setText(tone_text)
        self._apply_banner_tone(tone_name)

    def _banner_text(self) -> tuple[str, str]:
        """Return the highest-priority inline status message."""

        if self.save_running:
            return "Saving account and switching the ledger...", "info"
        if self.discovery_running:
            return "Inspecting the Thunderbird profile...", "info"
        if self.discovery_error:
            return self.discovery_error, "error"
        if self.expenses_job_running:
            return self.expenses_job_label or "Account DB update in progress.", "info"
        if self.inline_message:
            return self.inline_message, self.inline_tone
        if self.discovery_warnings:
            return self.discovery_warnings[0], "warning"
        if not self.profile_input.text().strip():
            return "Select a Thunderbird profile.", "muted"
        if self.account_combo.count() == 0:
            return "Reload accounts.", "muted"
        if self._has_unsaved_changes():
            return "Save to switch the active ledger.", "warning"
        source_validation = self.snapshot.get("sourceValidation", {}) if isinstance(self.snapshot.get("sourceValidation", {}), dict) else {}
        if source_validation and not bool(source_validation.get("valid")):
            return str(source_validation.get("message", "Thunderbird source is not parseable.")).strip(), "warning"
        script_extractors = self.snapshot.get("scriptExtractors", {}) if isinstance(self.snapshot.get("scriptExtractors", {}), dict) else {}
        script_errors = list(script_extractors.get("loadErrors", [])) if isinstance(script_extractors.get("loadErrors", []), list) else []
        if script_errors:
            return f"Python script extractors have {len(script_errors):,} load issue(s). Open Advanced for details.", "warning"
        if bool(self._display_db_status().get("hasCachedData")):
            return "Cached DB is ready for update.", "muted"
        return "Active DB is empty. Run Rebuild Now.", "muted"

    def _apply_banner_tone(self, tone: str) -> None:
        """Apply one semantic tone to the inline message banner."""

        palette = {
            "error": (self.theme.hex("rose"), self.theme.hex("surface_panel_alt")),
            "warning": (self.theme.hex("amber"), self.theme.hex("surface_panel_alt")),
            "success": (self.theme.hex("emerald"), self.theme.hex("surface_panel_alt")),
            "info": (self.theme.hex("blue"), self.theme.hex("surface_panel_alt")),
            "muted": (self.theme.hex("divider", self.theme.hex("border")), self.theme.hex("surface_panel_alt")),
        }
        border, background = palette.get(tone, palette["muted"])
        text_color = self.theme.hex("text_primary") if tone in {"error", "warning", "success", "info"} else self.theme.hex("text_secondary")
        self.banner.setStyleSheet(
            f"""
            QLabel {{
                background-color: {background};
                color: {text_color};
                border: 1px solid {border};
                padding: 8px 10px;
            }}
            """
        )

    def _db_state_label(self, status: dict[str, Any]) -> str:
        """Return the display label for the selected account DB state."""

        if str(status.get("error", "")).strip():
            return "Unreadable"
        if bool(status.get("hasCachedData")):
            return "Cached"
        if bool(status.get("exists")):
            return "No cached data"
        return "Missing"

    def _format_timestamp(self, value: str) -> str:
        """Format one stored ISO timestamp for the compact DB summary."""

        if not value:
            return "Never"
        try:
            return datetime.fromisoformat(value).strftime("%d %b %Y %H:%M")
        except ValueError:
            return value
