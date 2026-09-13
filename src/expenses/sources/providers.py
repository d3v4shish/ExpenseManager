from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import zipfile
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree

from src.expenses.email.mail_types import SourceRecord
from src.expenses.email.thunderbird_reader import ThunderbirdMailboxReader


def _file_fingerprint(paths: list[Path]) -> list[dict[str, int | str]]:
    result: list[dict[str, int | str]] = []
    for path in sorted({item.resolve() for item in paths if item.exists()}):
        stat = path.stat()
        result.append({"path": str(path), "size": int(stat.st_size), "modifiedNs": int(stat.st_mtime_ns)})
    return result


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _iso_timestamp(value: Any, fallback: datetime | None = None) -> str:
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        numeric = int(value)
        if numeric > 10_000_000_000:
            numeric //= 1000
        return datetime.fromtimestamp(numeric).astimezone().isoformat()
    raw = str(value or "").strip()
    if raw:
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return (stamp.astimezone() if stamp.tzinfo else stamp.replace(tzinfo=datetime.now().astimezone().tzinfo)).isoformat()
        except ValueError:
            pass
    return (fallback or datetime.now().astimezone()).isoformat()


class ThunderbirdProvider:
    """Adapt the existing Thunderbird reader to the generic source contract."""

    def __init__(self, provider_config: dict[str, Any], local_config_path: Path) -> None:
        self._reader = ThunderbirdMailboxReader(provider_config, local_config_path)
        self.provider_id = str(provider_config.get("id", "thunderbird_local")).strip() or "thunderbird_local"
        self.record_type = "email"

    def validate(self) -> dict[str, Any]:
        report = self._reader.describe_source()
        paths = list(report.get("mailboxPaths", []))
        return {"valid": bool(paths), "message": "" if paths else "No readable Thunderbird mailbox was found.", **report}

    def source_fingerprint(self) -> list[dict[str, int | str]]:
        return self._reader.source_fingerprint()

    def fetch_records(self, since: datetime | None = None, *, progress_callback: Callable[[dict], None] | None = None) -> list[SourceRecord]:
        return self._reader.fetch_records(since=since, progress_callback=progress_callback)


class EmlFolderProvider:
    """Scan a folder of standalone RFC 822 messages without mutating it."""

    record_type = "email"

    def __init__(self, provider_config: dict[str, Any]) -> None:
        self.config = provider_config
        self.provider_id = str(provider_config.get("id", "eml_folder")).strip() or "eml_folder"

    def _root(self) -> Path:
        return Path(str(self.config.get("path", "")).strip()).expanduser()

    def _paths(self) -> list[Path]:
        root = self._root()
        if not root.is_dir():
            return []
        recursive = bool(self.config.get("recursive", True))
        iterator = root.rglob("*.eml") if recursive else root.glob("*.eml")
        include = _patterns(self.config.get("includePatterns"))
        exclude = _patterns(self.config.get("excludePatterns"))
        return sorted(
            path
            for path in iterator
            if path.is_file() and _path_matches(path, root, include=include, exclude=exclude)
        )

    def validate(self) -> dict[str, Any]:
        root = self._root()
        return {"valid": root.is_dir(), "message": "" if root.is_dir() else f"EML directory does not exist: {root}", "path": str(root)}

    def source_fingerprint(self) -> list[dict[str, int | str]]:
        return _file_fingerprint(self._paths())

    def fetch_records(self, since: datetime | None = None, *, progress_callback: Callable[[dict], None] | None = None) -> list[SourceRecord]:
        return list(self.iter_records(since=since, progress_callback=progress_callback))

    def iter_records(self, since: datetime | None = None, *, progress_callback: Callable[[dict], None] | None = None):
        """Yield EML records one at a time for bounded-memory batch ingestion."""

        root = self._root()
        for index, path in enumerate(self._paths(), start=1):
            try:
                raw = path.read_bytes()
                if b"\x00" in raw:
                    raise ValueError("EML file contains an invalid NUL byte.")
                message = BytesParser(policy=policy.default).parsebytes(raw)
                received = _email_timestamp(message, path)
                if since is not None and datetime.fromisoformat(received) < since:
                    continue
                sender = parseaddr(str(message.get("from", "")))[1] or str(message.get("from", ""))
                text_body, html_body = _email_bodies(message)
                relative = path.relative_to(root).as_posix()
                yield SourceRecord(
                    provider_id=self.provider_id,
                    record_type=self.record_type,
                    external_id=relative,
                    title=str(message.get("subject", "")).strip() or path.name,
                    sender=sender,
                    received_at=received,
                    payload={"subject": str(message.get("subject", "")), "from": sender, "textBody": text_body, "htmlBody": html_body, "sourceUri": str(path.resolve())},
                    content_hash=_sha256(raw),
                    source_uri=str(path.resolve()),
                )
            except Exception as exc:  # noqa: BLE001
                yield _error_record(self.provider_id, "eml_error", path, exc)
            if progress_callback is not None:
                progress_callback({"processed": index, "sourcePath": str(path)})


class CsvProvider:
    """Import explicitly mapped transaction CSV rows as normalized source records."""

    record_type = "csv_transaction"

    def __init__(self, provider_config: dict[str, Any]) -> None:
        self.config = provider_config
        self.provider_id = str(provider_config.get("id", "csv")).strip() or "csv"

    def _path(self) -> Path:
        return Path(str(self.config.get("path", "")).strip()).expanduser()

    def validate(self) -> dict[str, Any]:
        path = self._path()
        mapping = self.config.get("mapping", {})
        valid = path.is_file() and isinstance(mapping, dict) and bool(mapping.get("date")) and bool(mapping.get("amount") or mapping.get("debit"))
        return {"valid": valid, "message": "" if valid else "CSV requires an existing file and date plus amount/debit mapping.", "path": str(path)}

    def source_fingerprint(self) -> list[dict[str, int | str]]:
        return _file_fingerprint([self._path()])

    def preview(self, *, limit: int = 25) -> dict[str, Any]:
        """Return a bounded mapping preview without writing source records."""

        path = self._path()
        if not path.is_file():
            raise ValueError(f"CSV file does not exist: {path}")
        raw = path.read_bytes()
        text = _decode_text(raw)
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(text.splitlines(), dialect=dialect)
        rows: list[dict[str, Any]] = []
        for row_number, row in enumerate(reader, start=2):
            normalized = _csv_transaction(row, dict(self.config.get("mapping", {})))
            timestamp = _csv_timestamp(normalized.get("timestamp", ""), dict(self.config.get("mapping", {})))
            if timestamp is None:
                normalized = {**normalized, "valid": False, "error": "Date is invalid or ambiguous; set mapping.dateFormat."}
            else:
                normalized["timestamp"] = timestamp
            rows.append({"rowNumber": row_number, "raw": row, "normalized": normalized})
            if len(rows) >= max(1, min(int(limit), 100)):
                break
        return {"path": str(path), "columns": list(reader.fieldnames or []), "rows": rows}

    def fetch_records(self, since: datetime | None = None, *, progress_callback: Callable[[dict], None] | None = None) -> list[SourceRecord]:
        path = self._path()
        mapping = dict(self.config.get("mapping", {}))
        if not path.is_file():
            return []
        raw = path.read_bytes()
        text = _decode_text(raw)
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
        records: list[SourceRecord] = []
        for index, row in enumerate(rows, start=2):
            normalized = _csv_transaction(row, mapping)
            encoded = json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")
            external_id = str(_column(row, mapping.get("reference"))).strip() or f"{path.name}:{index}:{_sha256(encoded)[:16]}"
            timestamp = _csv_timestamp(normalized.get("timestamp", ""), mapping)
            if timestamp is None:
                normalized = {**normalized, "valid": False, "error": "Date is invalid or ambiguous; set mapping.dateFormat.", "timestamp": str(_column(row, mapping.get("date"))).strip()}
                timestamp = _iso_timestamp("")
            if since is not None and datetime.fromisoformat(timestamp) < since:
                continue
            title = str(normalized.get("counterparty", "")).strip() or f"CSV row {index}"
            records.append(
                SourceRecord(
                    provider_id=self.provider_id,
                    record_type=self.record_type if normalized.get("valid") else "csv_error",
                    external_id=external_id,
                    title=title,
                    sender="CSV",
                    received_at=timestamp,
                    payload={"transaction": normalized, "row": row, "sourceUri": str(path.resolve())},
                    content_hash=_sha256(encoded),
                    source_uri=str(path.resolve()),
                )
            )
            if progress_callback is not None:
                progress_callback({"processed": index - 1, "fetched": len(records), "sourcePath": str(path)})
        return records


class SmsBackupProvider:
    """Read offline Android SMS Backup & Restore XML or JSON exports."""

    record_type = "sms"

    def __init__(self, provider_config: dict[str, Any]) -> None:
        self.config = provider_config
        self.provider_id = str(provider_config.get("id", "sms_backup")).strip() or "sms_backup"

    def _path(self) -> Path:
        return Path(str(self.config.get("path", "")).strip()).expanduser()

    def validate(self) -> dict[str, Any]:
        path = self._path()
        valid = path.is_file() and path.suffix.lower() in {".xml", ".json"}
        return {"valid": valid, "message": "" if valid else "SMS backup must be an XML or JSON file.", "path": str(path)}

    def source_fingerprint(self) -> list[dict[str, int | str]]:
        return _file_fingerprint([self._path()])

    def fetch_records(self, since: datetime | None = None, *, progress_callback: Callable[[dict], None] | None = None) -> list[SourceRecord]:
        path = self._path()
        if not path.is_file():
            return []
        raw = path.read_bytes()
        entries = _sms_entries(raw, path.suffix.lower())
        records: list[SourceRecord] = []
        for index, entry in enumerate(entries, start=1):
            body = _sms_body(entry)
            sender = str(entry.get("address", entry.get("sender", ""))).strip()
            received_at = _iso_timestamp(entry.get("date", entry.get("timestamp", "")))
            if since is not None and datetime.fromisoformat(received_at) < since:
                continue
            message_id = str(entry.get("_id", entry.get("id", ""))).strip() or f"{index}:{_sha256((sender + body + received_at).encode())[:16]}"
            records.append(
                SourceRecord(
                    provider_id=self.provider_id,
                    record_type=self.record_type,
                    external_id=message_id,
                    title=body[:120] or "SMS",
                    sender=sender,
                    received_at=received_at,
                    payload={"body": body, "address": sender, "sourceUri": str(path.resolve()), "platformMessageId": message_id},
                    content_hash=_sha256(json.dumps(entry, sort_keys=True).encode("utf-8")),
                    source_uri=str(path.resolve()),
                )
            )
            if progress_callback is not None:
                progress_callback({"processed": index, "fetched": len(records), "sourcePath": str(path)})
        return sorted(records, key=lambda item: item.received_at, reverse=True)


class AxiosAlternativeArchiveProvider:
    """Read a checksummed, versioned offline AxiosAlternative archive."""

    record_type = "axios_transaction"
    manifest_version = 1

    def __init__(self, provider_config: dict[str, Any]) -> None:
        self.config = provider_config
        self.provider_id = str(provider_config.get("id", "axios_archive")).strip() or "axios_archive"

    def _path(self) -> Path:
        return Path(str(self.config.get("path", "")).strip()).expanduser()

    def validate(self) -> dict[str, Any]:
        try:
            self._read_archive()
            return {"valid": True, "message": "", "path": str(self._path())}
        except ValueError as exc:
            return {"valid": False, "message": str(exc), "path": str(self._path())}

    def source_fingerprint(self) -> list[dict[str, int | str]]:
        return _file_fingerprint([self._path()])

    def fetch_records(self, since: datetime | None = None, *, progress_callback: Callable[[dict], None] | None = None) -> list[SourceRecord]:
        manifest, transactions, source_sms_identities, archive_bytes = self._read_archive()
        records: list[SourceRecord] = []
        for index, transaction in enumerate(transactions, start=1):
            if not isinstance(transaction, dict):
                continue
            timestamp = _iso_timestamp(transaction.get("timestamp", transaction.get("receivedAt", "")))
            if since is not None and datetime.fromisoformat(timestamp) < since:
                continue
            external_id = str(transaction.get("sourceMessageId", transaction.get("transactionKey", ""))).strip() or f"archive:{index}"
            records.append(
                SourceRecord(
                    provider_id=self.provider_id,
                    record_type=self.record_type,
                    external_id=external_id,
                    title=str(transaction.get("counterparty", transaction.get("canonicalVendor", "Axios transaction"))),
                    sender=str(transaction.get("sender", "AxiosAlternative")),
                    received_at=timestamp,
                    payload={"transaction": transaction, "manifest": manifest, "sourceSmsIdentities": source_sms_identities, "sourceUri": str(self._path().resolve())},
                    content_hash=_sha256(json.dumps(transaction, sort_keys=True).encode("utf-8")),
                    source_uri=str(self._path().resolve()),
                )
            )
            if progress_callback is not None:
                progress_callback({"processed": index, "fetched": len(records), "sourcePath": str(self._path())})
        _ = archive_bytes
        return records

    def _read_archive(self) -> tuple[dict[str, Any], list[Any], list[Any], bytes]:
        path = self._path()
        if not path.is_file():
            raise ValueError(f"AxiosAlternative archive does not exist: {path}")
        raw = path.read_bytes()
        try:
            with zipfile.ZipFile(path) as archive:
                manifest_bytes = archive.read("manifest.json")
                transactions_bytes = archive.read("transactions.json")
                source_sms_identities_bytes = archive.read("source_sms_identities.json")
        except (zipfile.BadZipFile, KeyError) as exc:
            raise ValueError("AxiosAlternative archive must contain manifest.json, transactions.json, and source_sms_identities.json.") from exc
        try:
            manifest = json.loads(manifest_bytes)
            transactions = json.loads(transactions_bytes)
            source_sms_identities = json.loads(source_sms_identities_bytes)
        except json.JSONDecodeError as exc:
            raise ValueError("AxiosAlternative archive JSON is invalid.") from exc
        if not isinstance(manifest, dict) or int(manifest.get("formatVersion", 0) or 0) != self.manifest_version:
            raise ValueError(f"Unsupported AxiosAlternative archive format; expected version {self.manifest_version}.")
        expected = str(manifest.get("transactionsSha256", "")).strip().lower()
        if expected != _sha256(transactions_bytes):
            raise ValueError("AxiosAlternative archive checksum did not match transactions.json.")
        if not str(manifest.get("parserVersion", "")).strip():
            raise ValueError("AxiosAlternative archive manifest is missing parserVersion.")
        identities_expected = str(manifest.get("sourceSmsIdentitiesSha256", "")).strip().lower()
        if identities_expected != _sha256(source_sms_identities_bytes):
            raise ValueError("AxiosAlternative archive checksum did not match source_sms_identities.json.")
        if not isinstance(transactions, list):
            raise ValueError("AxiosAlternative transactions.json must be an array.")
        if not isinstance(source_sms_identities, list):
            raise ValueError("AxiosAlternative source_sms_identities.json must be an array.")
        return manifest, transactions, source_sms_identities, raw


def _email_timestamp(message, path: Path) -> str:
    raw = str(message.get("date", "")).strip()
    if raw:
        try:
            stamp = parsedate_to_datetime(raw)
            return (stamp.astimezone() if stamp.tzinfo else stamp.replace(tzinfo=datetime.now().astimezone().tzinfo)).isoformat()
        except (TypeError, ValueError):
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()


def _email_bodies(message) -> tuple[str, str]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001
            content = _decode_text(part.get_payload(decode=True) or b"")
        if part.get_content_type() == "text/html":
            html_parts.append(str(content))
        else:
            text_parts.append(str(content))
    return "\n".join(text_parts), "\n".join(html_parts)


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _column(row: dict[str, Any], name: Any) -> Any:
    return row.get(str(name or ""), "")


def _patterns(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _path_matches(path: Path, root: Path, *, include: list[str], exclude: list[str]) -> bool:
    relative = path.relative_to(root).as_posix()
    if include and not any(fnmatch.fnmatch(relative, pattern) for pattern in include):
        return False
    return not any(fnmatch.fnmatch(relative, pattern) for pattern in exclude)


def _csv_timestamp(value: Any, mapping: dict[str, Any]) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    date_format = str(mapping.get("dateFormat", "")).strip()
    formats = [date_format] if date_format else []
    # ISO inputs are self-describing. Non-ISO values require an explicit format
    # so a 03/04 bank export is never silently guessed as March or April.
    if not formats:
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return (stamp.astimezone() if stamp.tzinfo else stamp.replace(tzinfo=datetime.now().astimezone().tzinfo)).isoformat()
        except ValueError:
            return None
    for pattern in formats:
        try:
            return datetime.strptime(raw, pattern).astimezone().isoformat()
        except ValueError:
            continue
    return None


def _csv_transaction(row: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    amount_raw = _decimal_text(_column(row, mapping.get("amount")), mapping)
    debit_raw = _decimal_text(_column(row, mapping.get("debit")), mapping)
    credit_raw = _decimal_text(_column(row, mapping.get("credit")), mapping)
    direction = str(_column(row, mapping.get("direction"))).strip().lower()
    amount = amount_raw or debit_raw or credit_raw
    if not direction:
        direction = "debit" if debit_raw else "credit" if credit_raw else ""
    try:
        numeric_amount = abs(float(amount))
    except ValueError:
        return {"valid": False, "error": "Amount is not numeric.", "timestamp": _column(row, mapping.get("date"))}
    if direction not in {"debit", "credit"}:
        return {"valid": False, "error": "Direction must be debit or credit.", "timestamp": _column(row, mapping.get("date"))}
    return {
        "valid": True,
        "bankName": str(_column(row, mapping.get("bank"))).strip(),
        "accountSuffix": str(_column(row, mapping.get("account"))).strip(),
        "direction": direction,
        "amount": numeric_amount,
        "currency": str(_column(row, mapping.get("currency"))).strip() or "INR",
        "transactionId": str(_column(row, mapping.get("reference"))).strip(),
        "counterparty": str(_column(row, mapping.get("description"))).strip(),
        "timestamp": str(_column(row, mapping.get("date"))).strip(),
    }


def _decimal_text(value: Any, mapping: dict[str, Any]) -> str:
    raw = str(value or "").strip().replace(" ", "")
    decimal_separator = str(mapping.get("decimalSeparator", ".")).strip() or "."
    grouping_separator = str(mapping.get("groupingSeparator", ",")).strip()
    if grouping_separator:
        raw = raw.replace(grouping_separator, "")
    if decimal_separator != ".":
        raw = raw.replace(decimal_separator, ".")
    return raw


def _sms_entries(raw: bytes, suffix: str) -> list[dict[str, Any]]:
    if suffix == ".xml":
        root = ElementTree.fromstring(raw)
        return [dict(item.attrib) for item in root.findall(".//sms")]
    payload = json.loads(_decode_text(raw))
    if isinstance(payload, dict):
        payload = payload.get("sms", payload.get("messages", []))
    return [dict(item) for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _sms_body(entry: dict[str, Any]) -> str:
    """Normalize either one SMS body or an explicitly grouped multipart export."""

    parts = entry.get("parts")
    if isinstance(parts, list):
        values = [str(part.get("body", part)) if isinstance(part, dict) else str(part) for part in parts]
        return "".join(value for value in values if value).strip()
    return str(entry.get("body", "")).strip()


def _error_record(provider_id: str, record_type: str, path: Path, error: Exception) -> SourceRecord:
    raw = f"{path.resolve()}:{error}".encode("utf-8", errors="replace")
    return SourceRecord(
        provider_id=provider_id,
        record_type=record_type,
        external_id=str(path.resolve()),
        title=path.name,
        sender="",
        received_at=datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(),
        payload={"sourceUri": str(path.resolve()), "error": str(error)},
        content_hash=_sha256(raw),
        source_uri=str(path.resolve()),
    )
