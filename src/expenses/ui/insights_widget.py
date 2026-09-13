from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.app.ui_kit import BaseWidget, SectionCard, make_button, make_label
from src.expenses.ui.insights_table import EvidenceTableModel, InsightTableModel


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


class ExpenseInsightsWidget(BaseWidget):
    """Render the local, explainable Insight Inbox as a dense model/view pane."""

    PAGE_SIZE = 200

    def __init__(self, widget_config, widget_data, theme, screen_api, window_api, parent=None):
        super().__init__(widget_config, widget_data, theme, screen_api, window_api, parent)
        self.thread_pool = QThreadPool.globalInstance()
        self.loading = False
        self.detail_loading = False
        self.selected_key = ""
        self.detail: dict[str, Any] = {}
        self._page_request_id = 0
        self._detail_request_id = 0
        self._build_ui()
        QTimer.singleShot(0, self._load_first_page)

    def apply_reload(self, widget_config: dict[str, Any], widget_data: dict[str, Any]) -> bool:
        self.widget_config = widget_config
        self.widget_data = widget_data
        QTimer.singleShot(0, self._load_first_page)
        return True

    def set_job_state(self, job_id: str, running: bool, payload: dict | None = None) -> None:
        if job_id not in {"expenses.refresh", "expenses.rebuild", "analytics.recompute"}:
            return
        self.refresh_button.setEnabled(not running)
        if running:
            self.status.setText("Computing local insights…")
        elif str((payload or {}).get("phase", "")) == "failed":
            self.status.setText("Insight computation failed; the last successful results are retained.")

    def _build_ui(self) -> None:
        self.setStyleSheet(
            f"""
            QLineEdit, QComboBox, QTableView {{
                color: {self.theme.hex('text_primary')};
                background: {self.theme.hex('surface_panel_alt')};
                border: 1px solid {self.theme.hex('divider', self.theme.hex('border'))};
                selection-background-color: {self.theme.hex('surface_active')};
            }}
            QLineEdit, QComboBox {{ min-height: 28px; padding: 3px 6px; }}
            QHeaderView::section {{
                background: {self.theme.hex('surface_panel')};
                color: {self.theme.hex('text_secondary')};
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
            str(self.widget_config.get("title", "Insight Inbox")),
            "Explainable local signals; nothing leaves this device.",
            bg=self.theme.hex("surface_panel"),
            border=self.theme.hex("divider"),
        )
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem("Active", ["new", "read"])
        self.status_filter.addItem("New", ["new"])
        self.status_filter.addItem("Dismissed", ["dismissed"])
        self.status_filter.addItem("Resolved", ["resolved"])
        self.status_filter.addItem("All", [])
        self.status_filter.currentIndexChanged.connect(self._load_first_page)
        toolbar.addWidget(self.status_filter)

        self.severity_filter = QComboBox()
        self.severity_filter.addItems(["All severities", "High", "Medium", "Low"])
        self.severity_filter.currentIndexChanged.connect(self._load_first_page)
        toolbar.addWidget(self.severity_filter)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search insights")
        self.search.returnPressed.connect(self._load_first_page)
        toolbar.addWidget(self.search, 1)

        self.refresh_button = make_button("Recompute", self.theme.hex("accent"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.refresh_button.clicked.connect(self.screen_api.run_analytics)
        toolbar.addWidget(self.refresh_button)
        self.settings_button = make_button("Settings", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.settings_button.clicked.connect(self._open_settings)
        toolbar.addWidget(self.settings_button)
        layout.addLayout(toolbar)

        self.status = QLabel("Loading insights…")
        self.status.setStyleSheet(f"color: {self.theme.hex('text_secondary')}; padding: 2px;")
        layout.addWidget(self.status)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.model = InsightTableModel(
            colors={
                "high": QColor(self.theme.hex("rose")),
                "medium": QColor(self.theme.hex("amber")),
                "low": QColor(self.theme.hex("blue")),
            }
        )
        self.model.moreRequested.connect(self._load_more)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        splitter.addWidget(self.table)

        detail_host = QWidget()
        detail_layout = QVBoxLayout(detail_host)
        detail_layout.setContentsMargins(0, 6, 0, 0)
        detail_layout.setSpacing(5)
        self.detail_title = make_label("Select an insight", self.theme.hex("text_primary"), 10, True)
        self.detail_summary = make_label("The explanation and supporting transactions appear here.", self.theme.hex("text_secondary"), 9)
        self.detail_summary.setWordWrap(True)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.detail_summary)
        actions = QHBoxLayout()
        self.read_button = make_button("Mark Read", self.theme.hex("divider"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.dismiss_button = make_button("Dismiss", self.theme.hex("rose"), self.theme.hex("rose"), self.theme.hex("surface_panel_alt"))
        self.reopen_button = make_button("Reopen", self.theme.hex("accent"), self.theme.hex("text_primary"), self.theme.hex("surface_panel_alt"))
        self.read_button.clicked.connect(lambda: self._set_status("read"))
        self.dismiss_button.clicked.connect(lambda: self._set_status("dismissed"))
        self.reopen_button.clicked.connect(lambda: self._set_status("new"))
        actions.addWidget(self.read_button)
        actions.addWidget(self.dismiss_button)
        actions.addWidget(self.reopen_button)
        actions.addStretch(1)
        detail_layout.addLayout(actions)
        self.evidence_model = EvidenceTableModel()
        self.evidence_table = QTableView()
        self.evidence_table.setModel(self.evidence_model)
        self.evidence_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.evidence_table.verticalHeader().setVisible(False)
        self.evidence_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.evidence_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        detail_layout.addWidget(self.evidence_table)
        splitter.addWidget(detail_host)
        splitter.setSizes([330, 210])
        layout.addWidget(splitter)
        card.add_content_widget(body)
        root.addWidget(card)
        self._refresh_actions()

    def _filters(self) -> dict[str, Any]:
        severity = self.severity_filter.currentText().lower()
        return {
            "statuses": list(self.status_filter.currentData() or []),
            "severity": "" if severity.startswith("all") else severity,
            "search_text": self.search.text().strip(),
            "limit": self.PAGE_SIZE,
        }

    def _open_settings(self) -> None:
        callback = getattr(self.window_api, "open_settings_dialog", None)
        if callable(callback):
            callback()
            return
        self.toggle_card("panel.expenses_settings", scroll_into_view=True)

    def _load_first_page(self, *_args) -> None:
        self._page_request_id += 1
        request_id = self._page_request_id
        self.loading = True
        self.status.setText("Loading insights…")
        worker = _Worker(self.screen_api.list_insights, **self._filters(), offset=0)
        worker.signals.finished.connect(lambda page, token=request_id: self._first_page_loaded(token, page))
        worker.signals.failed.connect(lambda message, token=request_id: self._load_failed(token, message))
        self.thread_pool.start(worker)

    def _load_more(self, offset: int) -> None:
        request_id = self._page_request_id
        worker = _Worker(self.screen_api.list_insights, **self._filters(), offset=offset)
        worker.signals.finished.connect(lambda page, token=request_id: self._more_loaded(token, page))
        worker.signals.failed.connect(lambda message, token=request_id: self._more_failed(token, message))
        self.thread_pool.start(worker)

    def _first_page_loaded(self, request_id: int, page: object) -> None:
        if request_id != self._page_request_id:
            return
        self.loading = False
        payload = dict(page) if isinstance(page, dict) else {}
        self.model.reset_page(payload)
        total = int(payload.get("total", 0) or 0)
        self.status.setText(
            f"{total:,} insight(s)"
            if total
            else "No insights yet. Run a refresh after importing transactions, then recompute when needed."
        )
        self.selected_key = ""
        self.detail = {}
        self.evidence_model.set_rows([])
        self._refresh_detail()

    def _more_loaded(self, request_id: int, page: object) -> None:
        if request_id != self._page_request_id:
            return
        self.model.append_page(dict(page) if isinstance(page, dict) else {})

    def _load_failed(self, request_id: int, message: str) -> None:
        if request_id != self._page_request_id:
            return
        self.loading = False
        self.model.fail_page()
        self.status.setText(f"Could not load insights: {message}")

    def _more_failed(self, request_id: int, message: str) -> None:
        if request_id != self._page_request_id:
            return
        self.model.fail_page()
        self.status.setText(f"Could not load more insights: {message}")

    def _selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        key = self.model.insight_key_at(rows[0].row()) if rows else ""
        if not key or key == self.selected_key:
            return
        self.selected_key = key
        self.detail_loading = True
        self._detail_request_id += 1
        request_id = self._detail_request_id
        self.detail_title.setText("Loading explanation…")
        worker = _Worker(self.screen_api.get_insight_detail, key)
        worker.signals.finished.connect(lambda payload, token=request_id, expected=key: self._detail_loaded(token, expected, payload))
        worker.signals.failed.connect(lambda message, token=request_id: self._detail_failed(token, message))
        self.thread_pool.start(worker)

    def _detail_loaded(self, request_id: int, expected_key: str, payload: object) -> None:
        if request_id != self._detail_request_id or expected_key != self.selected_key:
            return
        self.detail_loading = False
        self.detail = dict(payload) if isinstance(payload, dict) else {}
        self._refresh_detail()

    def _detail_failed(self, request_id: int, message: str) -> None:
        if request_id != self._detail_request_id:
            return
        self.detail_loading = False
        self.detail = {}
        self.detail_title.setText("Could not load insight")
        self.detail_summary.setText(message)
        self.evidence_model.set_rows([])
        self._refresh_actions()

    def _refresh_detail(self) -> None:
        if not self.detail:
            self.detail_title.setText("Select an insight")
            self.detail_summary.setText(
                "The explanation and supporting transactions appear here. "
                "When the inbox is empty, there are no active local signals for the selected filters."
            )
            self.evidence_model.set_rows([])
        else:
            self.detail_title.setText(str(self.detail.get("title", "Insight")))
            confidence = float(self.detail.get("confidence", 0.0) or 0.0) * 100.0
            self.detail_summary.setText(f"{self.detail.get('summary', '')}\nConfidence: {confidence:.0f}% · Algorithm: {self.detail.get('algorithmVersion', '')}")
            self.evidence_model.set_rows(list(self.detail.get("evidenceTransactions", [])))
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        status = str(self.detail.get("status", ""))
        enabled = bool(self.selected_key and self.detail)
        self.read_button.setEnabled(enabled and status == "new")
        self.dismiss_button.setEnabled(enabled and status not in {"dismissed", "resolved"})
        self.reopen_button.setEnabled(enabled and status in {"read", "dismissed", "resolved"})

    def _set_status(self, status: str) -> None:
        if not self.selected_key:
            return
        worker = _Worker(self.screen_api.set_insight_status, self.selected_key, status)
        worker.signals.finished.connect(self._status_changed)
        worker.signals.failed.connect(self._detail_failed)
        self.thread_pool.start(worker)

    def _status_changed(self, _payload: object) -> None:
        self.reload_cards("panel.expenses", "panel.expense_insights")


__all__ = ["ExpenseInsightsWidget"]
