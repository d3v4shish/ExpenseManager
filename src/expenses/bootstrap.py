from __future__ import annotations

from pathlib import Path

from src.app.data_management import DataManagementService
from src.app.files import JsonConfigStore, JsonStateStore, RuntimeFiles
from src.app.reports import ReportService
from src.app.runtime import AppRuntime, JobSpec
from src.app.settings import SettingsService
from src.expenses.account_config import (
    load_email_accounts_config,
    migrate_legacy_expenses_db_if_needed,
    resolve_active_expenses_db_path,
)
from src.expenses.email import BankAlertParser, BankRuleCatalog, MailParserChain, ScriptedEmailParser
from src.expenses.repositories import ExpensesRepository, VendorCatalogRepository
from src.expenses.services import AnalyticsService, ExpensesService, SourceIngestionService, TemplateMiningService, VendorCatalogService
from src.expenses.sources import build_default_source_provider_registry
from src.expenses.sources.parsers import ImportedTransactionParser, SmsBankAlertParser


def build_runtime(root_dir: Path) -> AppRuntime:
    """Build the runtime-owned expense domain bundle."""

    files = RuntimeFiles(root_dir)
    settings_service = SettingsService(files)
    data_management = DataManagementService(files)
    state_store = JsonStateStore(files)
    config_store = JsonConfigStore(files)
    email_config = load_email_accounts_config(files)
    active_db_path = resolve_active_expenses_db_path(files, email_config)
    migrate_legacy_expenses_db_if_needed(files, active_db_path)
    expenses_repository = ExpensesRepository(active_db_path)
    vendor_catalog_repository = VendorCatalogRepository(files.db_path("vendor_catalog.db"))
    vendor_catalog = VendorCatalogService(vendor_catalog_repository)
    bank_rule_catalog = BankRuleCatalog(
        files.default_path("bank_email_rules.json"),
        files.user_path("bank_email_rules.overrides.json"),
    )
    scripted_email_parser = ScriptedEmailParser(files.user_path("script_extractors.json"))
    parser_chain = MailParserChain()
    parser_chain.add(BankAlertParser(bank_rule_catalog))
    parser_chain.add(scripted_email_parser)
    parser_chain.add(ImportedTransactionParser())
    parser_chain.add(SmsBankAlertParser())
    mail_ingestion = SourceIngestionService(
        repository=expenses_repository,
        parser_chain=parser_chain,
        config_store=config_store,
        thunderbird_local_path=files.user_local_path("thunderbird.json"),
        provider_registry=build_default_source_provider_registry(),
    )
    expenses_service = ExpensesService(
        expenses_repository=expenses_repository,
        mail_ingestion_service=mail_ingestion,
        state_store=state_store,
        vendor_catalog_service=vendor_catalog,
    )
    template_mining_service = TemplateMiningService(
        expenses_repository=expenses_repository,
        bank_rule_catalog=bank_rule_catalog,
    )
    analytics_service = AnalyticsService(expenses_repository, settings_service)
    report_service = ReportService(
        expenses_service=expenses_service,
        expenses_repository=expenses_repository,
        analytics_service=analytics_service,
        vendor_catalog_service=vendor_catalog,
        settings_service=settings_service,
        data_management_service=data_management,
        mail_ingestion_service=mail_ingestion,
        files=files,
    )

    def refresh_with_insights():
        refresh_report = expenses_service.refresh_expenses()
        if not bool(refresh_report.get("materialized")):
            return {**refresh_report, "analyticsSkipped": True}
        return {**refresh_report, **analytics_service.recompute()}

    def rebuild_with_insights():
        rebuild_report = expenses_service.rebuild_expenses()
        return {**rebuild_report, **analytics_service.recompute()}

    def startup_views_and_insights():
        expenses_service.refresh_views_from_repository()
        return analytics_service.recompute()

    return AppRuntime(
        services={
            "expenses": expenses_service,
            "analytics": analytics_service,
            "settings": settings_service,
            "data_management": data_management,
            "mail_ingestion": mail_ingestion,
            "vendor_catalog": vendor_catalog,
            "bank_rule_catalog": bank_rule_catalog,
            "template_mining": template_mining_service,
            "scripted_email_parser": scripted_email_parser,
            "reports": report_service,
        },
        repositories={
            "expenses": expenses_repository,
            "vendor_catalog": vendor_catalog_repository,
        },
        jobs=(
            JobSpec(job_id="expenses.refresh", handler=refresh_with_insights, async_job=True),
            JobSpec(job_id="expenses.rebuild", handler=rebuild_with_insights, async_job=True),
            JobSpec(job_id="analytics.recompute", handler=analytics_service.recompute, async_job=True),
        ),
        startup_tasks=(startup_views_and_insights,),
        files=files,
    )
