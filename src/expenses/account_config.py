from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path
from typing import Any

MANAGED_PROVIDER_ID = "thunderbird_local"
MANAGED_PROVIDER_TYPE = "thunderbird"
MANAGED_PROVIDER_FIELDS = {
    "id",
    "type",
    "enabled",
    "accountEmail",
    "mailboxPaths",
    "mailboxGlobs",
    "profilePath",
}
DEFAULT_SYNC = {
    "lookbackDays": 14,
    "refreshMinutes": 10,
    "overlapHours": 2,
}


def load_email_accounts_config(files) -> dict[str, Any]:
    """Return the current user email-account config with a safe default shape."""

    try:
        payload = files.read_user("email_accounts.json")
    except FileNotFoundError:
        payload = {}
    providers = payload.get("providers", [])
    sync = payload.get("sync", {})
    return {
        "providers": copy.deepcopy(providers) if isinstance(providers, list) else [],
        "sync": _normalized_sync(sync),
    }


def load_local_thunderbird_config(files) -> dict[str, Any]:
    """Return the local Thunderbird config with a safe default shape."""

    try:
        payload = files.read_user_local("thunderbird.json")
    except FileNotFoundError:
        payload = {}
    if isinstance(payload, dict):
        return copy.deepcopy(payload)
    return {}


def get_managed_thunderbird_provider(email_config: dict[str, Any]) -> dict[str, Any] | None:
    """Return the managed Thunderbird provider when it exists."""

    providers = email_config.get("providers", [])
    if not isinstance(providers, list):
        return None
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if str(provider.get("id", "")).strip() == MANAGED_PROVIDER_ID:
            return copy.deepcopy(provider)
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if str(provider.get("type", "")).strip().lower() == MANAGED_PROVIDER_TYPE:
            return copy.deepcopy(provider)
    return None


def normalize_account_email(account_email: str) -> str:
    """Normalize one account email for config and path routing."""

    return str(account_email or "").strip().lower()


def account_db_slug(account_email: str) -> str:
    """Return the filesystem-safe slug used for one account DB."""

    normalized = normalize_account_email(account_email)
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return slug or "unconfigured_account"


def account_db_path(files, account_email: str) -> Path:
    """Return the dedicated DB path for one Thunderbird account."""

    return files.db_path(f"accounts/{account_db_slug(account_email)}.db")


def resolve_active_expenses_db_path(files, email_config: dict[str, Any] | None = None) -> Path:
    """Return the DB path that should back the active expenses runtime."""

    config = email_config if isinstance(email_config, dict) else load_email_accounts_config(files)
    managed = get_managed_thunderbird_provider(config) or {}
    account_email = normalize_account_email(str(managed.get("accountEmail", "")).strip())
    if not account_email:
        return files.db_path("expenses.db")
    return account_db_path(files, account_email)


def migrate_legacy_expenses_db_if_needed(files, active_db_path: Path) -> bool:
    """Copy the legacy shared DB trio into the active account DB when needed."""

    legacy_db_path = files.db_path("expenses.db")
    if legacy_db_path == active_db_path:
        return False
    if not legacy_db_path.exists() or active_db_path.exists():
        return False
    active_db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{legacy_db_path}{suffix}")
        destination = Path(f"{active_db_path}{suffix}")
        if not source.exists():
            continue
        shutil.copy2(source, destination)
    return True


def rewrite_managed_thunderbird_provider(
    email_config: dict[str, Any],
    *,
    account_email: str,
    mailbox_path: str,
) -> dict[str, Any]:
    """Rewrite the Thunderbird providers down to the one managed provider."""

    normalized_email = normalize_account_email(account_email)
    normalized_mailbox = str(mailbox_path or "").strip()
    existing_provider = get_managed_thunderbird_provider(email_config) or {}
    managed_provider = {
        **{
            key: copy.deepcopy(value)
            for key, value in existing_provider.items()
            if key not in MANAGED_PROVIDER_FIELDS
        },
        "id": MANAGED_PROVIDER_ID,
        "type": MANAGED_PROVIDER_TYPE,
        "enabled": True,
        "accountEmail": normalized_email,
        "mailboxPaths": [normalized_mailbox] if normalized_mailbox else [],
        "mailboxGlobs": [],
        "profilePath": "",
    }

    original_providers = email_config.get("providers", [])
    providers: list[dict[str, Any]] = []
    inserted = False
    if isinstance(original_providers, list):
        for provider in original_providers:
            if not isinstance(provider, dict):
                continue
            provider_type = str(provider.get("type", "")).strip().lower()
            if provider_type == MANAGED_PROVIDER_TYPE:
                if not inserted:
                    providers.append(managed_provider)
                    inserted = True
                continue
            providers.append(copy.deepcopy(provider))
    if not inserted:
        providers.insert(0, managed_provider)

    return {
        "providers": providers,
        "sync": _normalized_sync(email_config.get("sync", {})),
    }


def rewrite_local_thunderbird_config(local_config: dict[str, Any], *, profile_path: str) -> dict[str, Any]:
    """Rewrite the local Thunderbird config for one profile-root selection."""

    normalized_profile = str(profile_path or "").strip()
    payload = {
        key: copy.deepcopy(value)
        for key, value in local_config.items()
        if key not in {"profilePath", "mailboxPaths", "mailboxGlobs"}
    }
    payload["profilePath"] = normalized_profile
    payload["mailboxPaths"] = []
    payload["mailboxGlobs"] = []
    return payload


def build_expense_mail_config_snapshot(files, repository) -> dict[str, Any]:
    """Return the current active Thunderbird selection and DB status."""

    email_config = load_email_accounts_config(files)
    local_config = load_local_thunderbird_config(files)
    managed = get_managed_thunderbird_provider(email_config) or {}
    active_account_email = normalize_account_email(str(managed.get("accountEmail", "")).strip())
    selected_mailboxes = [
        str(item).strip()
        for item in managed.get("mailboxPaths", [])
        if str(item).strip()
    ]
    active_db_path = resolve_active_expenses_db_path(files, email_config)
    if repository is not None and getattr(repository, "db_path", None) == active_db_path:
        account_db = repository.describe_current_db(provider_id=MANAGED_PROVIDER_ID)
    else:
        account_db = repository.describe_db_path(active_db_path, provider_id=MANAGED_PROVIDER_ID)
    return {
        "profilePath": str(local_config.get("profilePath", "")).strip(),
        "activeAccountEmail": active_account_email,
        "providerEnabled": bool(managed.get("enabled", False)),
        "selectedMailboxPath": selected_mailboxes[0] if selected_mailboxes else "",
        "selectedMailboxPaths": selected_mailboxes,
        "sync": _normalized_sync(email_config.get("sync", {})),
        "accountDb": account_db,
    }


def _normalized_sync(value: Any) -> dict[str, Any]:
    """Return the normalized sync block with existing values preserved."""

    payload = dict(DEFAULT_SYNC)
    if isinstance(value, dict):
        payload.update(value)
    return payload
