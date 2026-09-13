"""Lazy UI exports keep domain-only tooling independent from Qt imports."""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "ExpenseInsightsWidget": ("src.expenses.ui.insights_widget", "ExpenseInsightsWidget"),
    "ExpenseSettingsWidget": ("src.expenses.ui.settings_widget", "ExpenseSettingsWidget"),
    "ExpensesAnalysisWidget": ("src.expenses.ui.analysis_widget", "ExpensesAnalysisWidget"),
    "ExpensesConfigWidget": ("src.expenses.ui.config_widget", "ExpensesConfigWidget"),
    "ExpensesMailDebugWidget": ("src.expenses.ui.debug_widget", "ExpensesMailDebugWidget"),
    "ExpensesOverviewWidget": ("src.expenses.ui.overview_widget", "ExpensesOverviewWidget"),
    "ExpensesSourcesWidget": ("src.expenses.ui.sources_widget", "ExpensesSourcesWidget"),
    "ExpensesScreenApi": ("src.expenses.ui.screen_api", "ExpensesScreenApi"),
}


def __getattr__(name: str):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


__all__ = sorted(_EXPORTS)
