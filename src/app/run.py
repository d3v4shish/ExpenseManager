from __future__ import annotations

from pathlib import Path

from src.app.logging_utils import configure_app_logging
from src.app.window import run_app_window
from src.expenses.bootstrap import build_runtime
from src.expenses.ui import (
    ExpensesAnalysisWidget,
    ExpensesConfigWidget,
    ExpensesMailDebugWidget,
    ExpensesOverviewWidget,
    ExpensesScreenApi,
)


def run() -> int:
    """Run the expense manager as a standalone PyQt app."""

    base_dir = Path(__file__).resolve().parents[2]
    configure_app_logging(base_dir)
    runtime = build_runtime(base_dir)
    screen_api = ExpensesScreenApi(
        expenses_service=runtime.services["expenses"],
        expenses_repository=runtime.repositories["expenses"],
        vendor_catalog_service=runtime.services["vendor_catalog"],
        bank_rule_catalog=runtime.services["bank_rule_catalog"],
        mail_ingestion_service=runtime.services["mail_ingestion"],
        files=runtime.files,
    )
    return run_app_window(
        title="Expense Manager",
        runtime=runtime,
        screen_api=screen_api,
        widget_mounts={
            "ExpensesAnalysisWidget": ExpensesAnalysisWidget,
            "ExpensesConfigWidget": ExpensesConfigWidget,
            "ExpensesMailDebugWidget": ExpensesMailDebugWidget,
            "ExpensesOverviewWidget": ExpensesOverviewWidget,
        },
        card_ids=("panel.expenses", "panel.expenses_config", "panel.expenses_debug", "panel.expenses_tab"),
        tab_targets={"expenses_tab": "panel.expenses_tab"},
    )
