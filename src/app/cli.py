"""Deterministic local CLI for reports, sources, and reconciliation management."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.expenses.bootstrap import build_runtime

SCHEMA_VERSION = "expense-manager-cli-v1"
EXIT_PROVIDER_ERROR = 1
EXIT_USAGE = 2
EXIT_GUI_SETUP_REQUIRED = 3


def run_cli(argv: list[str], root_dir: Path, *, program_name: str = "ExpenseManager") -> int:
    """Run one headless CLI command without importing the GUI."""

    try:
        output_format, normalized_argv = _extract_format(argv)
    except ValueError as exc:
        _emit_error(str(exc), "text")
        return EXIT_USAGE
    parser = _build_parser(program_name)
    args = parser.parse_args(normalized_argv)
    runtime = build_runtime(root_dir)
    try:
        if args.command == "sources":
            payload, exit_code = _sources(args, runtime, runtime.files)
        elif args.command == "duplicates":
            payload, exit_code = _duplicates(args, runtime)
        elif args.command == "reconciliation":
            payload, exit_code = _reconciliation(args, runtime)
        elif args.command == "templates":
            payload, exit_code = _templates(args, runtime)
        else:
            payload, exit_code = _reports(args, runtime)
    except (ValueError, KeyError, FileNotFoundError, json.JSONDecodeError) as exc:
        _emit_error(str(exc), output_format)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - command boundary must not expose tracebacks by default.
        _emit_error(str(exc) or exc.__class__.__name__, output_format)
        return EXIT_PROVIDER_ERROR
    _emit(payload, output_format)
    return exit_code


def _build_parser(program_name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program_name,
        description="ExpenseManager local reports and source management.",
        epilog="Thunderbird profile/account selection is GUI-only: run 'ExpenseManager gui', open Mail Configuration, then return to the CLI.",
    )
    parser.add_argument("--version", action="version", version="ExpenseManager 0.2.0")
    commands = parser.add_subparsers(dest="command", required=True)

    sources = commands.add_parser("sources", help="Configure and import local sources.")
    source_actions = sources.add_subparsers(dest="action", required=True)
    source_actions.add_parser("list")
    source_actions.add_parser("validate")
    source_actions.add_parser("setup-status", help="Show CLI-ready and GUI-required source setup.")
    source_actions.add_parser("add-thunderbird", help="Explain GUI-only Thunderbird setup; does not change data.")
    for name, provider_type in (("add-eml", "eml_folder"), ("add-csv", "csv"), ("add-sms-backup", "sms_backup"), ("add-axios-archive", "axios_archive")):
        command = source_actions.add_parser(name)
        command.set_defaults(provider_type=provider_type)
        command.add_argument("--id", required=True)
        command.add_argument("--path", required=True)
        command.add_argument("--mapping-json", default="{}")
    refresh = source_actions.add_parser("refresh")
    refresh.add_argument("--full", action="store_true")
    rebuild = source_actions.add_parser("rebuild")
    rebuild.add_argument("--full", action="store_true")
    remove = source_actions.add_parser("remove")
    remove.add_argument("--id", required=True)
    remove.add_argument("--confirm", action="store_true")
    inspect = source_actions.add_parser("inspect")
    inspect.add_argument("--id", required=True)
    inspect.add_argument("--limit", type=int, default=500)
    delete_data = source_actions.add_parser("delete-data")
    delete_data.add_argument("--id", required=True)
    delete_data.add_argument("--confirm", action="store_true")
    set_enabled = source_actions.add_parser("set-enabled")
    set_enabled.add_argument("--id", required=True)
    enabled_group = set_enabled.add_mutually_exclusive_group(required=True)
    enabled_group.add_argument("--enabled", action="store_true")
    enabled_group.add_argument("--disabled", action="store_true")

    duplicates = commands.add_parser("duplicates", help="Review source deduplication candidates.")
    duplicate_actions = duplicates.add_subparsers(dest="action", required=True)
    duplicate_list = duplicate_actions.add_parser("list")
    duplicate_list.add_argument("--state", default="needs_review")
    resolve = duplicate_actions.add_parser("resolve")
    resolve.add_argument("--key", required=True)
    resolve.add_argument("--decision", choices=("kept_separate", "merged"), required=True)

    reconciliation = commands.add_parser("reconciliation", help="Review exact transaction-identity conflicts across local sources.")
    reconciliation_actions = reconciliation.add_subparsers(dest="action", required=True)
    reconciliation_list = reconciliation_actions.add_parser("list")
    reconciliation_list.add_argument("--status", default="")
    reconciliation_resolve = reconciliation_actions.add_parser("resolve")
    reconciliation_resolve.add_argument("--key", required=True)
    reconciliation_resolve.add_argument("--signature", required=True)
    reconciliation_policy = reconciliation_actions.add_parser("set-policy")
    reconciliation_policy.add_argument("--mode", choices=("manual", "provider_priority"), required=True)
    reconciliation_policy.add_argument("--prefer-provider", default="")

    templates = commands.add_parser("templates", help="Mine masked local bank-alert templates and create review-only rule drafts.")
    template_actions = templates.add_subparsers(dest="action", required=True)
    template_mine = template_actions.add_parser("mine")
    _template_filters(template_mine)
    template_draft = template_actions.add_parser("draft")
    template_draft.add_argument("--key", required=True)
    _template_filters(template_draft)

    reports = commands.add_parser("report", help="Read safe GUI-visible data as reports.")
    report_actions = reports.add_subparsers(dest="action", required=True)
    dashboard = report_actions.add_parser("dashboard")
    _analysis_filters(dashboard, dashboard=True)
    report_actions.add_parser("overview")
    analytics = report_actions.add_parser("analytics")
    _analysis_filters(analytics, dashboard=True)
    transactions = report_actions.add_parser("transactions")
    transaction_actions = transactions.add_subparsers(dest="report_action", required=True)
    transaction_list = transaction_actions.add_parser("list")
    _transaction_filters(transaction_list)
    transaction_get = transaction_actions.add_parser("get")
    transaction_get.add_argument("--key", required=True)
    transaction_groups = transaction_actions.add_parser("groups")
    _ledger_filters(transaction_groups)
    transaction_group = transaction_actions.add_parser("group")
    _ledger_filters(transaction_group)
    transaction_group.add_argument("--group-key", required=True)
    transaction_sources = transaction_actions.add_parser("sources")
    transaction_sources.add_argument("--key", required=True)
    vendors = report_actions.add_parser("vendors")
    vendor_actions = vendors.add_subparsers(dest="report_action", required=True)
    vendor_list = vendor_actions.add_parser("list")
    vendor_list.add_argument("--query", default="")
    vendor_list.add_argument("--limit", type=int, default=100)
    vendor_search = vendor_actions.add_parser("search")
    vendor_search.add_argument("--query", required=True)
    vendor_search.add_argument("--limit", type=int, default=40)
    vendor_get = vendor_actions.add_parser("get")
    vendor_get.add_argument("--identity", required=True)
    vendor_get.add_argument("--currency", default="")
    vendor_get.add_argument("--include-ignored", action="store_true")
    vendor_get.add_argument("--limit", type=int, default=500)
    insights = report_actions.add_parser("insights")
    insight_actions = insights.add_subparsers(dest="report_action", required=True)
    insight_list = insight_actions.add_parser("list")
    insight_list.add_argument("--status", action="append", default=[])
    insight_list.add_argument("--severity", default="")
    insight_list.add_argument("--type", default="")
    insight_list.add_argument("--search", default="")
    insight_list.add_argument("--limit", type=int, default=200)
    insight_list.add_argument("--offset", type=int, default=0)
    insight_get = insight_actions.add_parser("get")
    insight_get.add_argument("--key", required=True)
    diagnostics = report_actions.add_parser("diagnostics")
    diagnostics.add_argument("--provider-id", default="thunderbird_local")
    diagnostics.add_argument("--limit", type=int, default=200)
    for name in ("sources", "account", "settings", "storage", "capabilities"):
        report_actions.add_parser(name)
    return parser


def _analysis_filters(parser: argparse.ArgumentParser, *, dashboard: bool = False) -> None:
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--currency", default="")
    parser.add_argument("--include-ignored", action="store_true")
    parser.add_argument("--search", default="")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--top", type=int, default=10)


def _transaction_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--currency", default="")
    parser.add_argument("--include-ignored", action="store_true")
    parser.add_argument("--search", default="")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--offset", type=int, default=0)


def _ledger_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--month", required=True, type=int)
    parser.add_argument("--currency", default="")
    parser.add_argument("--include-ignored", action="store_true")
    parser.add_argument("--search", default="")


def _template_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider-id", default="")
    parser.add_argument("--limit", type=int, default=2_000)
    parser.add_argument("--min-support", type=int, default=2)


def _sources(args: argparse.Namespace, runtime, files) -> tuple[dict[str, Any], int]:
    try:
        config = files.read_user("email_accounts.json")
    except FileNotFoundError:
        config = {"providers": [], "sync": {}}
    providers = [dict(item) for item in config.get("providers", []) if isinstance(item, dict)]
    service = runtime.services["mail_ingestion"]
    reports = runtime.services["reports"]
    if args.action == "setup-status":
        return reports.setup_status(), 0
    if args.action == "add-thunderbird":
        return reports.setup_status(), EXIT_GUI_SETUP_REQUIRED
    if args.action == "list":
        return _action({"providers": providers, "supportedTypes": list(service.provider_registry.supported_types())}), 0
    if args.action == "validate":
        validation_reports = []
        for provider_config in providers:
            if not bool(provider_config.get("enabled", True)):
                validation_reports.append({"id": provider_config.get("id", ""), "type": provider_config.get("type", ""), "enabled": False, "valid": True, "message": "Source is disabled; validation skipped."})
                continue
            try:
                provider = service.provider_registry.create(provider_config, service.thunderbird_local_path)
                validation_reports.append({"id": provider_config.get("id", ""), "type": provider_config.get("type", ""), "enabled": True, **provider.validate()})
            except ValueError as exc:
                validation_reports.append({"id": provider_config.get("id", ""), "type": provider_config.get("type", ""), "valid": False, "message": str(exc)})
        return _action({"providers": validation_reports}), 0 if all(bool(item.get("valid")) for item in validation_reports) else EXIT_PROVIDER_ERROR
    if args.action.startswith("add-"):
        if any(str(item.get("id", "")) == args.id for item in providers):
            raise ValueError(f"A source with id '{args.id}' already exists.")
        mapping = json.loads(args.mapping_json)
        if not isinstance(mapping, dict):
            raise ValueError("--mapping-json must contain a JSON object.")
        provider = {"id": args.id, "type": args.provider_type, "enabled": True, "path": str(Path(args.path).expanduser())}
        if args.provider_type == "csv":
            provider["mapping"] = mapping
        validation = service.provider_registry.create(provider, service.thunderbird_local_path).validate()
        if not bool(validation.get("valid")):
            raise ValueError(str(validation.get("message", "Source is not valid.")))
        providers.append(provider)
        files.write_user("email_accounts.json", {**config, "providers": providers})
        return _action({"added": provider, "validation": validation}), 0
    if args.action == "remove":
        if not args.confirm:
            raise ValueError("Removing a source only removes its configuration; rerun with --confirm.")
        remaining = [item for item in providers if str(item.get("id", "")) != args.id]
        if len(remaining) == len(providers):
            raise ValueError(f"Source '{args.id}' was not configured.")
        files.write_user("email_accounts.json", {**config, "providers": remaining})
        return _action({"removed": args.id, "transactionsDeleted": False}), 0
    if args.action == "inspect":
        return _action({"providerId": args.id, "records": runtime.repositories["expenses"].list_source_debug_rows(provider_id=args.id, limit=args.limit), "journals": runtime.repositories["expenses"].list_import_journals(provider_id=args.id, limit=args.limit)}), 0
    if args.action == "delete-data":
        if not args.confirm:
            raise ValueError("Deleting retained source data changes the ledger; rerun with --confirm.")
        return _action(runtime.services["expenses"].delete_source_data(args.id)), 0
    if args.action == "set-enabled":
        target = next((item for item in providers if str(item.get("id", "")) == args.id), None)
        if target is None:
            raise ValueError(f"Source '{args.id}' was not configured.")
        next_enabled = bool(args.enabled)
        if next_enabled:
            validation = service.provider_registry.create(target, service.thunderbird_local_path).validate()
            if not bool(validation.get("valid")):
                raise ValueError(str(validation.get("message", "Source is not valid.")))
        target["enabled"] = next_enabled
        files.write_user("email_accounts.json", {**config, "providers": providers})
        return _action({"id": args.id, "enabled": next_enabled}), 0
    if args.action in {"refresh", "rebuild"}:
        report = runtime.services["expenses"].rebuild_expenses() if args.action == "rebuild" or args.full else runtime.services["expenses"].refresh_expenses()
        return _action(report), EXIT_PROVIDER_ERROR if report.get("providerErrors") else 0
    return _action({}), EXIT_USAGE


def _duplicates(args: argparse.Namespace, runtime) -> tuple[dict[str, Any], int]:
    repository = runtime.repositories["expenses"]
    if args.action == "list":
        return _action({"candidates": repository.list_duplicate_candidates(state=args.state)}), 0
    return _action(runtime.services["expenses"].resolve_duplicate_candidate(args.key, args.decision)), 0


def _reconciliation(args: argparse.Namespace, runtime) -> tuple[dict[str, Any], int]:
    service = runtime.services["expenses"]
    if args.action == "list":
        return _action({"policy": service.get_reconciliation_policy(), "conflicts": service.list_reconciliation_conflicts(status=args.status)}), 0
    if args.action == "resolve":
        return _action(service.resolve_reconciliation_conflict(args.key, args.signature)), 0
    return _action(service.set_reconciliation_policy(args.mode, preferred_provider_id=args.prefer_provider)), 0


def _templates(args: argparse.Namespace, runtime) -> tuple[dict[str, Any], int]:
    service = runtime.services["template_mining"]
    if args.action == "mine":
        return _action(service.mine(provider_id=args.provider_id, limit=args.limit, min_support=args.min_support)), 0
    return _action(
        service.build_draft(
            args.key,
            provider_id=args.provider_id,
            limit=args.limit,
            min_support=args.min_support,
        )
    ), 0


def _reports(args: argparse.Namespace, runtime) -> tuple[dict[str, Any], int]:
    reports = runtime.services["reports"]
    if args.action == "dashboard":
        return reports.dashboard(year=args.year, month=args.month, currency=args.currency, months=args.months, top=args.top), 0
    if args.action == "overview":
        return reports.overview(), 0
    if args.action == "analytics":
        return reports.analytics(year=args.year, month=args.month, currency=args.currency, include_ignored=args.include_ignored, search_text=args.search, months=args.months, top=args.top), 0
    if args.action == "transactions":
        if args.report_action == "list":
            return reports.transactions(year=args.year, month=args.month, currency=args.currency, include_ignored=args.include_ignored, search_text=args.search, limit=args.limit, offset=args.offset), 0
        if args.report_action == "get":
            return reports.transaction(args.key), 0
        if args.report_action == "sources":
            return _action({"transactionKey": args.key, "sources": runtime.repositories["expenses"].list_transaction_sources(args.key)}), 0
        if args.report_action == "groups":
            return reports.ledger_groups(year=args.year, month=args.month, currency=args.currency, include_ignored=args.include_ignored, search_text=args.search), 0
        return reports.ledger_group(year=args.year, month=args.month, group_key=args.group_key, currency=args.currency, include_ignored=args.include_ignored, search_text=args.search), 0
    if args.action == "vendors":
        if args.report_action == "get":
            return reports.vendor(args.identity, currency=args.currency, include_ignored=args.include_ignored, limit=args.limit), 0
        if args.report_action == "search":
            return _action({"query": args.query, "matches": runtime.repositories["expenses"].search_vendor_directory(args.query, limit=args.limit)}), 0
        return reports.vendors(query=args.query, limit=args.limit), 0
    if args.action == "insights":
        if args.report_action == "get":
            return reports.insight(args.key), 0
        return reports.insights(statuses=args.status, severity=args.severity, insight_type=args.type, search_text=args.search, limit=args.limit, offset=args.offset), 0
    if args.action == "sources":
        return reports.sources(), 0
    if args.action == "account":
        return reports.account(), 0
    if args.action == "diagnostics":
        return reports.diagnostics(provider_id=args.provider_id, limit=args.limit), 0
    if args.action == "settings":
        return reports.settings(), 0
    if args.action == "storage":
        return reports.storage(), 0
    return reports.capabilities(), 0


def _action(data: dict[str, Any]) -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "result": data}


def _extract_format(argv: list[str]) -> tuple[str, list[str]]:
    output_format = "text"
    result: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--format":
            if index + 1 >= len(argv):
                raise ValueError("--format requires 'text' or 'json'.")
            output_format = argv[index + 1].lower()
            index += 2
            continue
        if item.startswith("--format="):
            output_format = item.split("=", 1)[1].lower()
            index += 1
            continue
        result.append(item)
        index += 1
    if output_format not in {"text", "json"}:
        raise ValueError("--format must be 'text' or 'json'.")
    return output_format, result


def _emit(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str))
        return
    print(_format_text(payload))


def _emit_error(message: str, output_format: str) -> None:
    payload = {"schemaVersion": SCHEMA_VERSION, "error": {"message": message}}
    if output_format == "json":
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False), file=sys.stderr)
        return
    print(f"Error: {message}", file=sys.stderr)


def _format_text(payload: Any) -> str:
    """Render the same result as a compact terminal-oriented tree."""

    lines: list[str] = []

    def visit(value: Any, *, label: str = "", depth: int = 0) -> None:
        prefix = "  " * depth
        if isinstance(value, dict):
            if label:
                lines.append(f"{prefix}{label}:")
                depth += 1
                prefix = "  " * depth
            for key, item in value.items():
                visit(item, label=str(key), depth=depth)
            return
        if isinstance(value, list):
            if label:
                lines.append(f"{prefix}{label}:")
            if not value:
                lines.append(f"{'  ' * (depth + 1)}(none)")
                return
            for item in value:
                if isinstance(item, (dict, list)):
                    lines.append(f"{'  ' * (depth + 1)}-")
                    visit(item, depth=depth + 2)
                else:
                    lines.append(f"{'  ' * (depth + 1)}- {item}")
            return
        lines.append(f"{prefix}{label}: {value}" if label else f"{prefix}{value}")

    visit(payload)
    return "\n".join(lines)
