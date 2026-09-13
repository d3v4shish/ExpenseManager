from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.expenses.email.mail_types import SourceRecord
from src.expenses.repositories.expenses_repository import ExpensesRepository
from src.expenses.sources.providers import AxiosAlternativeArchiveProvider, CsvProvider, EmlFolderProvider, SmsBackupProvider
from src.expenses.sources.registry import build_default_source_provider_registry


class SourceProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_registry_rejects_unknown_provider_type(self) -> None:
        registry = build_default_source_provider_registry()
        with self.assertRaisesRegex(ValueError, "Unsupported source type"):
            registry.create({"type": "unknown", "id": "unknown"}, self.root / "local.json")
        self.assertEqual(registry.supported_types(), ("axios_archive", "csv", "eml_folder", "sms_backup", "thunderbird"))

    def test_eml_folder_provider_is_stable_and_preserves_file_provenance(self) -> None:
        folder = self.root / "mail"
        folder.mkdir()
        message = folder / "alert.eml"
        message.write_text(
            "From: alerts@example.test\nSubject: Debit alert\nDate: Tue, 09 Sep 2026 10:00:00 +0530\n\nA/c XX1234 debited INR 123.00.",
            encoding="utf-8",
        )
        provider = EmlFolderProvider({"id": "mail", "type": "eml_folder", "path": str(folder)})
        self.assertTrue(provider.validate()["valid"])
        first = provider.fetch_records()
        second = provider.fetch_records()
        streamed = list(provider.iter_records())
        self.assertEqual(len(first), 1)
        self.assertEqual([item.external_id for item in streamed], [item.external_id for item in first])
        self.assertEqual(first[0].external_id, "alert.eml")
        self.assertEqual(first[0].content_hash, second[0].content_hash)
        self.assertEqual(first[0].source_uri, str(message.resolve()))

    def test_eml_folder_provider_honors_include_and_exclude_patterns(self) -> None:
        folder = self.root / "mail"
        (folder / "alerts").mkdir(parents=True)
        (folder / "archive").mkdir()
        for path in (folder / "alerts" / "keep.eml", folder / "archive" / "skip.eml"):
            path.write_text("From: alerts@example.test\nSubject: Test\n\nBody", encoding="utf-8")
        records = EmlFolderProvider({"id": "mail", "type": "eml_folder", "path": str(folder), "includePatterns": ["**/*.eml"], "excludePatterns": ["archive/*"]}).fetch_records()
        self.assertEqual([record.external_id for record in records], ["alerts/keep.eml"])

    def test_eml_folder_provider_keeps_malformed_file_as_an_error_record(self) -> None:
        folder = self.root / "mail"
        folder.mkdir()
        malformed = folder / "bad.eml"
        malformed.write_bytes(b"\x00\xff\xfe")
        records = EmlFolderProvider({"id": "mail", "type": "eml_folder", "path": str(folder)}).fetch_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].record_type, "eml_error")
        self.assertEqual(records[0].source_uri, str(malformed.resolve()))

    def test_csv_provider_emits_mapped_transaction_and_invalid_row(self) -> None:
        path = self.root / "transactions.csv"
        path.write_text("Date,Amount,Direction,Description,Reference\n2026-09-09T10:00:00+05:30,120.00,debit,Cafe,REF1\n2026-09-09T11:00:00+05:30,nope,debit,Bad,REF2\n", encoding="utf-8")
        provider = CsvProvider(
            {
                "id": "csv", "type": "csv", "path": str(path),
                "mapping": {"date": "Date", "amount": "Amount", "direction": "Direction", "description": "Description", "reference": "Reference"},
            }
        )
        records = provider.fetch_records()
        self.assertEqual([record.record_type for record in records], ["csv_transaction", "csv_error"])
        self.assertEqual(records[0].payload["transaction"]["transactionId"], "REF1")
        self.assertIn("Amount is not numeric", records[1].payload["transaction"]["error"])

    def test_csv_provider_requires_explicit_format_for_ambiguous_dates(self) -> None:
        path = self.root / "ambiguous.csv"
        path.write_text("Date,Amount,Direction\n03/04/2026,12,debit\n", encoding="utf-8")
        base = {"id": "csv", "type": "csv", "path": str(path), "mapping": {"date": "Date", "amount": "Amount", "direction": "Direction"}}
        invalid = CsvProvider(base).fetch_records()[0]
        self.assertEqual(invalid.record_type, "csv_error")
        self.assertIn("ambiguous", invalid.payload["transaction"]["error"])
        formatted = CsvProvider({**base, "mapping": {**base["mapping"], "dateFormat": "%d/%m/%Y"}}).fetch_records()[0]
        self.assertEqual(formatted.record_type, "csv_transaction")

    def test_csv_preview_returns_columns_and_bounded_normalized_rows(self) -> None:
        path = self.root / "preview.csv"
        path.write_text("Date;Amount;Direction\n2026-09-09T10:00:00+05:30;12;debit\n", encoding="utf-8")
        preview = CsvProvider({"id": "csv", "type": "csv", "path": str(path), "mapping": {"date": "Date", "amount": "Amount", "direction": "Direction"}}).preview()
        self.assertEqual(preview["columns"], ["Date", "Amount", "Direction"])
        self.assertTrue(preview["rows"][0]["normalized"]["valid"])

    def test_sms_backup_provider_reads_xml_and_json_with_stable_message_identity(self) -> None:
        xml_path = self.root / "backup.xml"
        xml_path.write_text('<smses><sms _id="42" address="AXISBK" body="A/c XX1234 debited INR 12.50 at Cafe" date="1788928200000" /></smses>', encoding="utf-8")
        xml_records = SmsBackupProvider({"id": "sms", "type": "sms_backup", "path": str(xml_path)}).fetch_records()
        self.assertEqual(xml_records[0].external_id, "42")
        self.assertEqual(xml_records[0].sender, "AXISBK")
        json_path = self.root / "backup.json"
        json_path.write_text(json.dumps({"messages": [{"id": "json-1", "sender": "BANK", "body": "credited INR 20.00", "timestamp": "2026-09-09T10:00:00+05:30"}]}), encoding="utf-8")
        json_records = SmsBackupProvider({"id": "sms-json", "type": "sms_backup", "path": str(json_path)}).fetch_records()
        self.assertEqual(json_records[0].external_id, "json-1")

    def test_sms_backup_provider_normalizes_explicit_multipart_json_message(self) -> None:
        path = self.root / "multipart.json"
        path.write_text(json.dumps({"messages": [{"id": "multi-1", "sender": "BANK", "timestamp": "2026-09-09T10:00:00+05:30", "parts": ["A/c XX1234 debited ", {"body": "INR 45.00"}]}]}), encoding="utf-8")
        record = SmsBackupProvider({"id": "sms", "type": "sms_backup", "path": str(path)}).fetch_records()[0]
        self.assertEqual(record.external_id, "multi-1")
        self.assertEqual(record.payload["body"], "A/c XX1234 debited INR 45.00")

    def test_axios_archive_requires_a_valid_version_and_checksum(self) -> None:
        archive_path = self.root / "axios.expense"
        transactions = [{"transactionKey": "a-1", "amount": 50, "direction": "debit", "timestamp": "2026-09-09T10:00:00+05:30", "currency": "INR"}]
        encoded = json.dumps(transactions).encode("utf-8")
        identities = [{"platformMessageId": "sms-1"}]
        identities_encoded = json.dumps(identities).encode("utf-8")
        manifest = {"formatVersion": 1, "parserVersion": "axios-android-v1", "transactionsSha256": hashlib.sha256(encoded).hexdigest(), "sourceSmsIdentitiesSha256": hashlib.sha256(identities_encoded).hexdigest()}
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("transactions.json", encoded)
            archive.writestr("source_sms_identities.json", identities_encoded)
        provider = AxiosAlternativeArchiveProvider({"id": "axios", "type": "axios_archive", "path": str(archive_path)})
        self.assertTrue(provider.validate()["valid"])
        self.assertEqual(provider.fetch_records()[0].external_id, "a-1")
        manifest["transactionsSha256"] = "bad"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("transactions.json", encoded)
            archive.writestr("source_sms_identities.json", identities_encoded)
        self.assertFalse(provider.validate()["valid"])

    def test_source_content_revisions_and_duplicate_decisions_are_persisted(self) -> None:
        repository = ExpensesRepository(self.root / "expenses.db")
        record = SourceRecord("csv", "csv_transaction", "row-1", "Cafe", "CSV", "2026-09-09T10:00:00+05:30", {"sourceUri": "/tmp/a.csv"}, "hash-a", "/tmp/a.csv")
        source_id, changed = repository.upsert_source_record(record)
        self.assertTrue(changed)
        self.assertEqual(repository.get_source_record(source_id)["schemaVersion"], 1)
        _, unchanged = repository.upsert_source_record(record)
        self.assertFalse(unchanged)
        changed_record = SourceRecord("csv", "csv_transaction", "row-1", "Cafe", "CSV", "2026-09-09T10:00:00+05:30", {"sourceUri": "/tmp/a.csv", "row": 2}, "hash-b", "/tmp/a.csv")
        _, changed = repository.upsert_source_record(changed_record)
        self.assertTrue(changed)
        repository.upsert_transactions([
            _transaction("first", "2026-09-09T10:00:00+05:30"),
            _transaction("second", "2026-09-09T10:05:00+05:30"),
        ])
        repository.replace_transaction_sources([("first", source_id, "source")])
        self.assertEqual(repository.refresh_duplicate_candidates(), 1)
        candidate = repository.list_duplicate_candidates()[0]
        repository.resolve_duplicate_candidate(candidate["candidateKey"], "kept_separate")
        self.assertEqual(repository.list_duplicate_candidates()[0]["state"], "kept_separate")


def _transaction(key: str, timestamp: str) -> dict:
    return {
        "transactionKey": key, "providerId": "csv", "externalId": key, "parserId": "test",
        "bankName": "Axis", "accountSuffix": "1234", "direction": "debit", "amount": 120.0,
        "currency": "INR", "transactionId": "", "counterparty": "Cafe", "rawCounterparty": "Cafe",
        "resolvedVendor": "Cafe", "canonicalVendor": "Cafe", "canonicalAlias": "Cafe", "aliasKey": "cafe",
        "category": "Uncategorized", "subcategory": "", "vendorMatchSource": "test", "timestamp": timestamp,
        "year": 2026, "month": 9, "title": "", "sender": "",
    }


if __name__ == "__main__":
    unittest.main()
