from __future__ import annotations

from pathlib import Path

from src.app.files import JsonConfigStore, JsonStateStore, RuntimeFiles
from src.app.runtime import AppRuntime, JobSpec
from src.expenses.account_config import (
    load_email_accounts_config,
    migrate_legacy_expenses_db_if_needed,
    resolve_active_expenses_db_path,
)
from src.expenses.email import BankAlertParser, BankRuleCatalog, MailParserChain, ScriptedEmailParser
from src.expenses.repositories import ExpensesRepository, VendorCatalogRepository
from src.expenses.services import ExpensesMailIngestionService, ExpensesService, VendorCatalogService


def build_runtime(root_dir: Path) -> AppRuntime:
    """Build the runtime-owned expense domain bundle."""

    files = RuntimeFiles(root_dir)
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
    mail_ingestion = ExpensesMailIngestionService(
        repository=expenses_repository,
        parser_chain=parser_chain,
        config_store=config_store,
        thunderbird_local_path=files.user_local_path("thunderbird.json"),
    )
    expenses_service = ExpensesService(
        expenses_repository=expenses_repository,
        mail_ingestion_service=mail_ingestion,
        state_store=state_store,
        vendor_catalog_service=vendor_catalog,
    )
    return AppRuntime(
        services={
            "expenses": expenses_service,
            "mail_ingestion": mail_ingestion,
            "vendor_catalog": vendor_catalog,
            "bank_rule_catalog": bank_rule_catalog,
            "scripted_email_parser": scripted_email_parser,
        },
        repositories={
            "expenses": expenses_repository,
            "vendor_catalog": vendor_catalog_repository,
        },
        jobs=(
            JobSpec(job_id="expenses.refresh", handler=expenses_service.refresh_expenses, async_job=True),
            JobSpec(job_id="expenses.rebuild", handler=expenses_service.rebuild_expenses, async_job=True),
        ),
        startup_tasks=(expenses_service.refresh_views_from_repository,),
        files=files,
    )
