from __future__ import annotations

import configparser
import json
import re
from pathlib import Path
from typing import Any

MAILBOX_CANDIDATES = (
    "[Gmail].sbd/All Mail",
    "[Google Mail].sbd/All Mail",
    "Inbox",
    "INBOX",
)

USER_PREF_RE = re.compile(r'^user_pref\("(?P<key>[^"]+)",\s*(?P<value>.+)\);\s*$')


def discover_thunderbird_profile_accounts(profile_path: str) -> dict[str, Any]:
    """Parse one Thunderbird profile and return usable IMAP accounts."""

    root = Path(str(profile_path or "").strip()).expanduser()
    if not root.is_dir():
        raise ValueError("Select a valid Thunderbird profile directory.")

    prefs_path = root / "prefs.js"
    if not prefs_path.exists():
        raise FileNotFoundError(f"Thunderbird prefs.js not found under: {root}")

    prefs = _parse_prefs_js(prefs_path)
    warnings: list[str] = []
    accounts: list[dict[str, Any]] = []

    identity_emails = _pref_namespace_map(prefs, "mail.identity.", ".useremail")
    identity_names = _pref_namespace_map(prefs, "mail.identity.", ".fullName")
    account_identities = _pref_namespace_map(prefs, "mail.account.", ".identities")
    account_servers = _pref_namespace_map(prefs, "mail.account.", ".server")
    server_types = _pref_namespace_map(prefs, "mail.server.", ".type")
    server_names = _pref_namespace_map(prefs, "mail.server.", ".name")
    server_rel_dirs = _pref_namespace_map(prefs, "mail.server.", ".directory-rel")
    server_abs_dirs = _pref_namespace_map(prefs, "mail.server.", ".directory")

    for account_id, identities_value in account_identities.items():
        server_id = str(account_servers.get(account_id, "")).strip()
        server_type = str(server_types.get(server_id, "")).strip().lower()
        if not server_id or server_type != "imap":
            continue

        mail_root_rel, mail_root_abs = _resolve_server_mail_root(
            root,
            str(server_rel_dirs.get(server_id, "")).strip(),
            str(server_abs_dirs.get(server_id, "")).strip(),
        )
        if not mail_root_abs or not mail_root_abs.exists():
            warnings.append(f"{account_id}: Thunderbird server directory is missing for {server_id}.")
            continue

        identity_ids = [segment.strip() for segment in str(identities_value or "").split(",") if segment.strip()]
        for identity_id in identity_ids:
            email_value = str(identity_emails.get(identity_id, "")).strip().lower()
            if not email_value:
                continue

            mailbox_rel, mailbox_abs = _resolve_default_mailbox(mail_root_rel, mail_root_abs)
            if mailbox_abs is None:
                warnings.append(f"{email_value}: no usable mailbox was found under {mail_root_abs}.")
                continue

            full_name = str(identity_names.get(identity_id, "")).strip()
            server_name = str(server_names.get(server_id, "")).strip()
            display_name = full_name or email_value
            accounts.append(
                {
                    "email": email_value,
                    "displayName": display_name,
                    "serverId": server_id,
                    "serverName": server_name,
                    "mailRootRel": mail_root_rel,
                    "mailRootAbs": str(mail_root_abs),
                    "defaultMailboxRel": mailbox_rel,
                    "defaultMailboxAbs": str(mailbox_abs),
                }
            )

    accounts.sort(key=lambda item: (str(item.get("displayName", "")).lower(), str(item.get("email", "")).lower()))
    return {
        "profilePath": str(root),
        "accounts": accounts,
        "warnings": warnings,
    }


def discover_default_thunderbird_profiles(
    *,
    home: Path | None = None,
    homes_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return profiles from known Thunderbird locations in readable home directories.

    Passing ``home`` scopes discovery to that one directory for deterministic
    callers and tests.  Normal desktop discovery checks the current user's home
    plus every readable direct child of ``/home``.  It only reads Thunderbird
    profile metadata and checks mailbox paths; it never scans mailbox content.
    """

    home_dirs = [Path(home).expanduser()] if home is not None else _candidate_home_directories(homes_root)
    seen: set[Path] = set()
    results: list[dict[str, Any]] = []
    for home_dir in home_dirs:
        roots = [
            home_dir / ".thunderbird",
            home_dir / ".mozilla-thunderbird",
            home_dir / "snap" / "thunderbird" / "common" / ".thunderbird",
        ]
        for root in roots:
            if not root.exists():
                continue
            for profile_path in _profile_paths_from_root(root):
                resolved = profile_path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                result: dict[str, Any] = {"profilePath": str(profile_path)}
                try:
                    discovery = discover_thunderbird_profile_accounts(str(profile_path))
                except Exception as exc:  # noqa: BLE001
                    result.update({"accounts": [], "warnings": [], "error": str(exc)})
                else:
                    result.update(
                        {
                            "profilePath": str(discovery.get("profilePath", profile_path)),
                            "accounts": list(discovery.get("accounts", [])),
                            "warnings": list(discovery.get("warnings", [])),
                            "error": "",
                        }
                    )
                results.append(result)
    results.sort(key=lambda item: (0 if item.get("accounts") else 1, str(item.get("profilePath", "")).lower()))
    return results


def _candidate_home_directories(homes_root: Path | None) -> list[Path]:
    """Return readable direct homes without recursively traversing user data."""

    candidates = [Path.home()]
    root = Path(homes_root) if homes_root is not None else Path("/home")
    try:
        children = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name.lower())
    except OSError:
        children = []
    candidates.extend(children)
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(candidate)
    return result


def _profile_paths_from_root(root: Path) -> list[Path]:
    profiles_ini = root / "profiles.ini"
    paths: list[Path] = []
    if profiles_ini.exists():
        parser = configparser.ConfigParser()
        parser.read(profiles_ini, encoding="utf-8")
        for section in parser.sections():
            if not section.lower().startswith("profile"):
                continue
            raw_path = parser.get(section, "Path", fallback="").strip()
            if not raw_path:
                continue
            is_relative = parser.get(section, "IsRelative", fallback="1").strip() != "0"
            path = root / raw_path if is_relative else Path(raw_path)
            if path.is_dir():
                paths.append(path)
    for path in root.glob("*.default*"):
        if path.is_dir():
            paths.append(path)
    return paths


def _parse_prefs_js(path: Path) -> dict[str, Any]:
    """Return the Thunderbird prefs.js key-value map."""

    prefs: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        match = USER_PREF_RE.match(line)
        if not match:
            continue
        key = match.group("key")
        value = _parse_pref_value(match.group("value"))
        prefs[key] = value
    return prefs


def _parse_pref_value(raw_value: str) -> Any:
    """Parse one prefs.js literal."""

    value = raw_value.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip('"')
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def _pref_namespace_map(prefs: dict[str, Any], prefix: str, suffix: str) -> dict[str, Any]:
    """Return a map of namespace id to value for one Thunderbird pref family."""

    result: dict[str, Any] = {}
    for key, value in prefs.items():
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        middle = key[len(prefix) : len(key) - len(suffix)]
        if middle:
            result[middle] = value
    return result


def _resolve_server_mail_root(profile_root: Path, rel_value: str, abs_value: str) -> tuple[str, Path | None]:
    """Resolve one Thunderbird server directory into relative and absolute forms."""

    rel_path = ""
    if rel_value.startswith("[ProfD]"):
        rel_path = rel_value.removeprefix("[ProfD]").replace("\\", "/").strip("/")
        return rel_path.replace("\\", "/"), profile_root / Path(rel_path)
    if rel_value:
        rel_path = rel_value.replace("\\", "/").strip("/")
        return rel_path, profile_root / Path(rel_path)
    if abs_value:
        absolute = Path(abs_value)
        try:
            rel = absolute.relative_to(profile_root).as_posix()
        except ValueError:
            rel = absolute.name
        return rel, absolute
    return "", None


def _resolve_default_mailbox(mail_root_rel: str, mail_root_abs: Path) -> tuple[str, Path | None]:
    """Resolve the default mailbox path in the configured fallback order."""

    normalized_root = str(mail_root_rel or "").strip().strip("/")
    for candidate in MAILBOX_CANDIDATES:
        candidate_abs = mail_root_abs / Path(candidate)
        if not candidate_abs.exists() or not candidate_abs.is_file():
            continue
        candidate_rel = "/".join(part for part in (normalized_root, candidate.replace("\\", "/")) if part)
        return candidate_rel, candidate_abs
    return "", None
