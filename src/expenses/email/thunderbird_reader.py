from __future__ import annotations

import email
import hashlib
import json
import logging
import mailbox
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from src.expenses.email.mail_types import SourceRecord


class ThunderbirdMailboxReader:
    """Read local Thunderbird mailboxes into normalized source records."""

    provider_id = "thunderbird_local"
    record_type = "email"

    def __init__(self, provider_config: dict[str, Any], local_config_path: Path) -> None:
        """Store the mailbox config and optional local Thunderbird config path."""

        self.provider_config = provider_config
        self.local_config_path = local_config_path
        self.logger = logging.getLogger(self.__class__.__name__)

    def fetch_records(self, since: datetime | None = None) -> list[SourceRecord]:
        """Return Thunderbird mail as normalized source records."""

        mailbox_paths = self.resolve_mailbox_paths()
        account_email = str(self.provider_config.get("accountEmail", "")).strip() or "<unspecified>"
        self.logger.info(
            "Fetching Thunderbird email records account_email=%s mailbox_count=%s since=%s",
            account_email,
            len(mailbox_paths),
            since.isoformat() if since else "<none>",
        )
        for mailbox_path in mailbox_paths:
            self.logger.info(
                "Thunderbird mailbox resolved account_email=%s mailbox_path=%s mailbox_label=%s",
                account_email,
                mailbox_path,
                self._mailbox_label(mailbox_path),
            )
        records: list[SourceRecord] = []
        max_workers = max(1, int(self.provider_config.get("readWorkers", 4) or 4))
        chunk_size = max(25, int(self.provider_config.get("normalizeChunkSize", 200) or 200))
        for mailbox_path in mailbox_paths:
            if not self._mailbox_matches_msf_prefilter(mailbox_path):
                self.logger.info("Skipping mailbox due to msf prefilter mailbox_path=%s", mailbox_path)
                continue
            try:
                mbox = mailbox.mbox(mailbox_path, create=False)
            except (FileNotFoundError, OSError) as exc:
                self.logger.warning("Failed to open Thunderbird mailbox path=%s error=%s", mailbox_path, exc)
                continue
            mailbox_total = 0
            mailbox_first_dt: datetime | None = None
            mailbox_last_dt: datetime | None = None
            mailbox_fetched = 0
            normalize_batch: list[tuple[Any, datetime | None]] = []
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tb-mail") as executor:
                for message in mbox:
                    mailbox_total += 1
                    received_dt = self._message_datetime(message)
                    if received_dt is not None:
                        if mailbox_first_dt is None or received_dt < mailbox_first_dt:
                            mailbox_first_dt = received_dt
                        if mailbox_last_dt is None or received_dt > mailbox_last_dt:
                            mailbox_last_dt = received_dt
                    if since is not None and received_dt is not None and received_dt < since:
                        continue
                    normalize_batch.append((message, received_dt))
                    if len(normalize_batch) >= chunk_size:
                        fetched = self._normalize_batch(executor, normalize_batch)
                        records.extend(fetched)
                        mailbox_fetched += len(fetched)
                        normalize_batch = []
                if normalize_batch:
                    fetched = self._normalize_batch(executor, normalize_batch)
                    records.extend(fetched)
                    mailbox_fetched += len(fetched)
            self.logger.info(
                "Thunderbird mailbox summary account_email=%s mailbox_path=%s mailbox_label=%s total_messages=%s fetched_messages=%s first_message_at=%s last_message_at=%s since=%s",
                account_email,
                mailbox_path,
                self._mailbox_label(mailbox_path),
                mailbox_total,
                mailbox_fetched,
                mailbox_first_dt.isoformat() if mailbox_first_dt else "<unknown>",
                mailbox_last_dt.isoformat() if mailbox_last_dt else "<unknown>",
                since.isoformat() if since else "<full-scan>",
            )
        self.logger.info("Fetched Thunderbird email records count=%s", len(records))
        records.sort(key=lambda item: item.received_at, reverse=True)
        return records

    def resolve_mailbox_paths(self) -> list[Path]:
        """Return configured Thunderbird mailbox files that can be parsed."""

        return self._resolve_mailbox_paths()

    def describe_source(self) -> dict[str, Any]:
        """Return a diagnostic summary of the configured Thunderbird source."""

        local_config = self._load_local_config()
        profile_path = self._resolve_profile_path(local_config)
        profile = Path(profile_path).expanduser() if profile_path else None
        explicit = self._resolve_explicit_paths(self.provider_config.get("mailboxPaths", []), profile_path)
        local_explicit = self._resolve_explicit_paths(local_config.get("mailboxPaths", []), profile_path) if isinstance(local_config, dict) else []
        mailbox_paths = self.resolve_mailbox_paths()
        return {
            "providerId": str(self.provider_config.get("id", self.provider_id)).strip(),
            "accountEmail": self._account_email(),
            "profilePath": str(profile or ""),
            "profileExists": bool(profile and profile.is_dir()),
            "prefsExists": bool(profile and (profile / "prefs.js").exists()),
            "configuredMailboxPaths": [str(path) for path in explicit + local_explicit],
            "missingMailboxPaths": [str(path) for path in explicit + local_explicit if not path.exists()],
            "mailboxPaths": [str(path) for path in mailbox_paths],
            "mailboxCount": len(mailbox_paths),
        }

    def _resolve_mailbox_paths(self) -> list[Path]:
        """Resolve mailbox files using provider and local Thunderbird config."""

        local_config = self._load_local_config()

        profile_path = self._resolve_profile_path(local_config)
        explicit = self._resolve_explicit_paths(self.provider_config.get("mailboxPaths", []), profile_path)
        local_explicit = self._resolve_explicit_paths(local_config.get("mailboxPaths", []), profile_path) if isinstance(local_config, dict) else []
        explicit_paths = [path for path in explicit + local_explicit if path.exists()]
        if explicit_paths:
            self.logger.info("Using explicit Thunderbird mailbox paths account_email=%s count=%s", self._account_email(), len(explicit_paths))
            return explicit_paths

        provider_globs = [str(item).strip() for item in self.provider_config.get("mailboxGlobs", []) if str(item).strip()]
        local_globs = [str(item).strip() for item in local_config.get("mailboxGlobs", []) if str(item).strip()] if isinstance(local_config, dict) else []
        if profile_path and (provider_globs or local_globs):
            globbed = self._expand_mailbox_globs(Path(profile_path), provider_globs + local_globs)
            if globbed:
                self.logger.info(
                    "Using Thunderbird mailbox globs account_email=%s count=%s patterns=%s",
                    self._account_email(),
                    len(globbed),
                    provider_globs + local_globs,
                )
                return globbed

        if not profile_path:
            self.logger.warning("No Thunderbird mailboxPaths/mailboxGlobs/profilePath configured")
            return []

        root = Path(profile_path)
        discover_patterns = [str(item).strip() for item in self.provider_config.get("discoverGlobs", []) if str(item).strip()]
        if not discover_patterns and isinstance(local_config, dict):
            discover_patterns = [str(item).strip() for item in local_config.get("discoverGlobs", []) if str(item).strip()]
        if not discover_patterns:
            discover_patterns = [
                "Mail/*/Inbox",
                "Mail/*/INBOX",
                "ImapMail/*/Inbox",
                "ImapMail/*/INBOX",
                "ImapMail/*/[Gmail].sbd/All Mail",
                "ImapMail/*/[Google Mail].sbd/All Mail",
                "ImapMail/*/[Gmail].sbd/AllMail",
                "ImapMail/*/[Google Mail].sbd/AllMail",
            ]
        discovered = self._expand_mailbox_globs(root, discover_patterns)
        self.logger.info("Discovered Thunderbird mailbox paths account_email=%s count=%s", self._account_email(), len(discovered))
        return discovered

    def _load_local_config(self) -> dict[str, Any]:
        local_config: dict[str, Any] = {}
        if self.local_config_path.exists():
            try:
                payload = json.loads(self.local_config_path.read_text(encoding="utf-8"))
                local_config = payload if isinstance(payload, dict) else {}
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Failed to read Thunderbird local config path=%s error=%s", self.local_config_path, exc)
                local_config = {}
        return local_config

    def _resolve_profile_path(self, local_config: dict[str, Any]) -> str:
        """Return the Thunderbird profile path from local config or provider config."""

        profile_path = ""
        if isinstance(local_config, dict):
            profile_path = str(local_config.get("profilePath", "")).strip()
        if not profile_path:
            profile_path = str(self.provider_config.get("profilePath", "")).strip()
        return profile_path

    def _resolve_explicit_paths(self, values, profile_path: str) -> list[Path]:
        """Return the explicit mailbox paths resolved against the Thunderbird profile."""

        paths: list[Path] = []
        root = Path(profile_path) if profile_path else None
        for value in values or []:
            raw = str(value).strip()
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute() and root is not None:
                path = root / path
            paths.append(path)
        return paths

    def _expand_mailbox_globs(self, root: Path, patterns: list[str]) -> list[Path]:
        """Expand the configured mailbox globs under the Thunderbird profile."""

        candidates: list[Path] = []
        for pattern in patterns:
            candidates.extend(root.glob(pattern))
        seen: set[Path] = set()
        results: list[Path] = []
        for path in candidates:
            if path.is_file() and path not in seen and path.suffix.lower() != ".msf":
                seen.add(path)
                results.append(path)
        return results

    def _account_email(self) -> str:
        """Return the configured account email label."""

        return str(self.provider_config.get("accountEmail", "")).strip() or "<unspecified>"

    def _normalize_batch(self, executor: ThreadPoolExecutor, batch: list[tuple[Any, datetime | None]]) -> list[SourceRecord]:
        """Normalize one mailbox batch in parallel."""

        return [record for record in executor.map(lambda item: self._normalize_message(item[0], item[1]), batch) if record is not None]

    def _mailbox_matches_msf_prefilter(self, mailbox_path: Path) -> bool:
        """Return whether the mailbox passes the optional MSF prefilter."""

        config = self.provider_config.get("msfPrefilter", {})
        if not isinstance(config, dict) or not bool(config.get("enabled")):
            return True
        tokens = [str(item).strip().lower() for item in config.get("tokens", []) if str(item).strip()]
        if not tokens:
            return True
        msf_path = mailbox_path.with_suffix(mailbox_path.suffix + ".msf") if mailbox_path.suffix else Path(f"{mailbox_path}.msf")
        if not msf_path.exists():
            return True
        try:
            content = msf_path.read_bytes().lower()
        except OSError as exc:
            self.logger.warning("Failed to read msf prefilter path=%s error=%s", msf_path, exc)
            return True
        return any(token.encode("utf-8", errors="ignore") in content for token in tokens)

    def _mailbox_label(self, path: Path) -> str:
        """Return a friendlier mailbox label for logs."""

        path_text = str(path).replace("\\", "/")
        if "[Gmail].sbd/All Mail" in path_text:
            return "[Gmail]/All Mail"
        if "[Google Mail].sbd/All Mail" in path_text:
            return "[Google Mail]/All Mail"
        return path.name

    def _normalize_message(self, message, received_dt: datetime | None) -> SourceRecord | None:
        """Normalize one Thunderbird message into a source record."""

        subject = self._decode_header_value(message.get("subject", ""))
        sender = self._decode_header_value(message.get("from", ""))
        message_id = self._decode_header_value(message.get("message-id", "")).strip() or self._hash_identity(subject, sender, message.as_string())
        text_body, html_body = self._extract_bodies(message)
        received_at = (received_dt or datetime.now().astimezone()).astimezone().isoformat()
        content_hash = self._hash_identity(subject, sender, f"{received_at}|{text_body}|{html_body}")
        return SourceRecord(
            provider_id=str(self.provider_config.get("id", self.provider_id)),
            record_type=self.record_type,
            external_id=message_id,
            title=subject or "Untitled email",
            sender=sender,
            received_at=received_at,
            payload={
                "subject": subject,
                "from": sender,
                "textBody": text_body,
                "htmlBody": html_body,
            },
            content_hash=content_hash,
        )

    def _message_datetime(self, message) -> datetime | None:
        """Parse one message date header into a timezone-aware datetime."""

        raw_date = self._decode_header_value(message.get("date", ""))
        if not raw_date:
            return None
        try:
            dt = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            return None
        return dt.astimezone() if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

    def _extract_bodies(self, message) -> tuple[str, str]:
        """Return the plain-text and HTML bodies for one message."""

        text_parts: list[str] = []
        html_parts: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", "")).lower()
                if "attachment" in disposition:
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="replace")
                except LookupError:
                    decoded = payload.decode("utf-8", errors="replace")
                if content_type == "text/plain":
                    text_parts.append(decoded)
                elif content_type == "text/html":
                    html_parts.append(decoded)
        else:
            payload = message.get_payload(decode=True)
            if payload is not None:
                charset = message.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="replace")
                except LookupError:
                    decoded = payload.decode("utf-8", errors="replace")
                if message.get_content_type() == "text/html":
                    html_parts.append(decoded)
                else:
                    text_parts.append(decoded)
        return self._cleanup_body_text("\n".join(text_parts)), self._cleanup_body_text("\n".join(html_parts))

    def _cleanup_body_text(self, value: str) -> str:
        """Normalize stored email body line endings and collapse repeated blank lines."""

        text = str(value or "").strip()
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{2,}", "\n", text)
        return text.replace("\n", "\r\n").strip()

    def _decode_header_value(self, value: str) -> str:
        """Decode one MIME header value."""

        try:
            return str(make_header(decode_header(value)))
        except Exception:  # noqa: BLE001
            return str(value)

    def _hash_identity(self, *parts: str) -> str:
        """Return one stable content hash for the normalized message fields."""

        digest = hashlib.sha1()
        for part in parts:
            digest.update(part.encode("utf-8", errors="ignore"))
            digest.update(b"\0")
        return digest.hexdigest()
