from __future__ import annotations

import tempfile
import unittest
import hashlib
import json
import zipfile
from pathlib import Path
from unittest import mock

from src.expenses.bootstrap import build_runtime
from src.expenses.services.mail_ingestion import SourceIngestionCancelled


class MultiSourceIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runtime_home = self.root / "runtime"
        self.env = mock.patch.dict("os.environ", {"EXPENSE_MANAGER_HOME": str(self.runtime_home)})
        self.env.start()
        self.csv_path = self.root / "transactions.csv"
        self.csv_path.write_text(
            "Date,Amount,Direction,Description,Reference,Account,Bank,Currency\n"
            "2026-09-09T10:00:00+05:30,120.00,debit,Cafe,REF12345,1234,Axis,INR\n",
            encoding="utf-8",
        )
        self.runtime = build_runtime(Path(__file__).resolve().parents[1])
        self.runtime.files.write_user(
            "email_accounts.json",
            {
                "sync": {"lookbackDays": 3650, "overlapHours": 24},
                "providers": [
                    {
                        "id": "bank-csv",
                        "type": "csv",
                        "enabled": True,
                        "path": str(self.csv_path),
                        "mapping": {
                            "date": "Date", "amount": "Amount", "direction": "Direction",
                            "description": "Description", "reference": "Reference", "account": "Account",
                            "bank": "Bank", "currency": "Currency",
                        },
                    }
                ],
            },
        )

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_first_incremental_refresh_imports_new_csv_and_repeat_is_idempotent(self) -> None:
        service = self.runtime.services["expenses"]
        first = service.refresh_expenses()
        self.assertTrue(first["materialized"])
        self.assertEqual(first["changedRecords"], 1)
        repository = self.runtime.repositories["expenses"]
        self.assertEqual(repository.count_source_records(), 1)
        transactions = repository.list_transactions()
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["transactionId"], "REF12345")
        self.assertEqual(len(repository.list_transaction_sources(transactions[0]["transactionKey"])), 1)
        journal = repository.list_import_journals(provider_id="bank-csv")[0]
        self.assertEqual(journal["status"], "completed")
        self.assertEqual(journal["recordsChanged"], 1)

        second = service.refresh_expenses()
        self.assertFalse(second["materialized"])
        self.assertEqual(second["changedRecords"], 0)
        self.assertEqual(repository.count_source_records(), 1)
        self.assertEqual(repository.list_import_journals(provider_id="bank-csv")[0]["recordsChanged"], 0)

    def test_snapshot_source_removes_deleted_rows_and_repeat_is_a_no_op(self) -> None:
        service = self.runtime.services["expenses"]
        repository = self.runtime.repositories["expenses"]
        service.refresh_expenses()
        self.csv_path.write_text(
            "Date,Amount,Direction,Description,Reference,Account,Bank,Currency\n",
            encoding="utf-8",
        )

        refreshed = service.refresh_expenses()

        self.assertTrue(refreshed["materialized"])
        self.assertEqual(refreshed["removedRecords"], 1)
        self.assertEqual(repository.count_source_records(), 0)
        self.assertEqual(repository.list_transactions(), [])
        repeated = service.refresh_expenses()
        self.assertFalse(repeated["materialized"])
        self.assertEqual(repeated["removedRecords"], 0)

    def test_snapshot_source_replaces_a_changed_historical_row(self) -> None:
        service = self.runtime.services["expenses"]
        repository = self.runtime.repositories["expenses"]
        service.refresh_expenses()
        self.csv_path.write_text(
            "Date,Amount,Direction,Description,Reference,Account,Bank,Currency\n"
            "2026-09-09T10:00:00+05:30,175.00,debit,Updated Cafe,REF12345,1234,Axis,INR\n",
            encoding="utf-8",
        )

        refreshed = service.refresh_expenses()

        self.assertTrue(refreshed["materialized"])
        self.assertEqual(refreshed["changedRecords"], 1)
        transactions = repository.list_transactions()
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["amount"], 175.0)
        self.assertEqual(transactions[0]["rawCounterparty"], "Updated Cafe")

    def test_cancelled_snapshot_scan_never_prunes_prior_records(self) -> None:
        service = self.runtime.services["expenses"]
        repository = self.runtime.repositories["expenses"]
        service.refresh_expenses()
        self.csv_path.write_text(
            "Date,Amount,Direction,Description,Reference,Account,Bank,Currency\n"
            "2026-09-09T10:01:00+05:30,80.00,debit,Tea,REF12346,1234,Axis,INR\n"
            "2026-09-09T10:02:00+05:30,90.00,debit,Snacks,REF12347,1234,Axis,INR\n",
            encoding="utf-8",
        )
        calls = {"count": 0}

        def cancelled() -> bool:
            calls["count"] += 1
            return calls["count"] >= 3

        report = service.refresh_expenses(is_cancelled=cancelled)

        self.assertTrue(report["cancelled"])
        self.assertEqual(repository.count_source_records(), 2)
        self.assertEqual(len(repository.list_transactions()), 1)

    def test_exact_csv_and_sms_matches_share_one_transaction_with_two_provenance_links(self) -> None:
        sms_path = self.root / "backup.xml"
        sms_path.write_text(
            '<smses><sms _id="phone-1" address="AXISBK" '
            'body="A/c XX1234 debited INR 120.00 to Cafe UPI Ref: REF12345" '
            'date="1788928200000" /></smses>',
            encoding="utf-8",
        )
        config = self.runtime.files.read_user("email_accounts.json")
        config["providers"].append({"id": "phone-sms", "type": "sms_backup", "enabled": True, "path": str(sms_path)})
        self.runtime.files.write_user("email_accounts.json", config)

        report = self.runtime.services["expenses"].rebuild_expenses()
        self.assertTrue(report["materialized"])
        repository = self.runtime.repositories["expenses"]
        transactions = repository.list_transactions()
        self.assertEqual(len(transactions), 1)
        sources = repository.list_transaction_sources(transactions[0]["transactionKey"])
        self.assertEqual({source["providerId"] for source in sources}, {"bank-csv", "phone-sms"})
        self.assertEqual({source["matchKind"] for source in sources}, {"strong"})

    def test_user_fuzzy_merge_is_applied_without_losing_source_provenance(self) -> None:
        first_csv = self.root / "first.csv"
        second_csv = self.root / "second.csv"
        header = "Date,Amount,Direction,Description,Account,Bank,Currency\n"
        first_csv.write_text(header + "2026-09-09T10:00:00+05:30,75,debit,Coffee Shop,1234,Axis,INR\n", encoding="utf-8")
        second_csv.write_text(header + "2026-09-09T10:05:00+05:30,75,debit,Coffee Shop,1234,Axis,INR\n", encoding="utf-8")
        mapping = {"date": "Date", "amount": "Amount", "direction": "Direction", "description": "Description", "account": "Account", "bank": "Bank", "currency": "Currency"}
        self.runtime.files.write_user(
            "email_accounts.json",
            {"sync": {"lookbackDays": 3650, "overlapHours": 24}, "providers": [
                {"id": "first", "type": "csv", "enabled": True, "path": str(first_csv), "mapping": mapping},
                {"id": "second", "type": "csv", "enabled": True, "path": str(second_csv), "mapping": mapping},
            ]},
        )
        service = self.runtime.services["expenses"]
        repository = self.runtime.repositories["expenses"]
        service.rebuild_expenses()
        candidate = repository.list_duplicate_candidates()[0]
        result = service.resolve_duplicate_candidate(candidate["candidateKey"], "merged")
        self.assertEqual(result["transactionCount"], 1)
        transactions = repository.list_transactions()
        self.assertEqual(len(transactions), 1)
        self.assertEqual(len(repository.list_transaction_sources(transactions[0]["transactionKey"])), 2)

    def test_deleting_retained_source_data_keeps_configuration_but_rebuilds_ledger(self) -> None:
        service = self.runtime.services["expenses"]
        repository = self.runtime.repositories["expenses"]
        service.refresh_expenses()
        report = service.delete_source_data("bank-csv")
        self.assertEqual(report["deletedSourceRecords"], 1)
        self.assertEqual(report["transactionCount"], 0)
        self.assertEqual(repository.count_source_records(), 0)
        self.assertEqual(repository.list_transactions(), [])
        self.assertEqual(self.runtime.files.read_user("email_accounts.json")["providers"][0]["id"], "bank-csv")

    def test_disabled_provider_is_journaled_without_importing_data(self) -> None:
        config = self.runtime.files.read_user("email_accounts.json")
        config["sourceFeatures"] = {"csv": False}
        self.runtime.files.write_user("email_accounts.json", config)
        report = self.runtime.services["expenses"].refresh_expenses()
        repository = self.runtime.repositories["expenses"]
        self.assertEqual(report["changedRecords"], 0)
        self.assertEqual(repository.count_source_records(), 0)
        journal = repository.list_import_journals(provider_id="bank-csv")[0]
        self.assertEqual(journal["status"], "disabled")

    def test_invalid_provider_does_not_prevent_a_valid_source_from_importing(self) -> None:
        config = self.runtime.files.read_user("email_accounts.json")
        config["providers"].append({"id": "broken", "type": "csv", "enabled": True, "path": str(self.root / "missing.csv"), "mapping": {"date": "Date", "amount": "Amount"}})
        self.runtime.files.write_user("email_accounts.json", config)
        report = self.runtime.services["expenses"].refresh_expenses()
        self.assertTrue(report["materialized"])
        self.assertEqual(report["providerErrors"][0]["providerId"], "broken")
        journal = self.runtime.repositories["expenses"].list_import_journals(provider_id="broken")[0]
        self.assertEqual(journal["status"], "failed")

    def test_incremental_cancel_retains_written_source_records_but_not_a_partial_ledger(self) -> None:
        self.csv_path.write_text(
            "Date,Amount,Direction,Description,Reference,Account,Bank,Currency\n"
            "2026-09-09T10:00:00+05:30,120.00,debit,Cafe,REF12345,1234,Axis,INR\n"
            "2026-09-09T10:01:00+05:30,80.00,debit,Tea,REF12346,1234,Axis,INR\n",
            encoding="utf-8",
        )
        calls = {"count": 0}

        def cancelled() -> bool:
            calls["count"] += 1
            return calls["count"] >= 3

        report = self.runtime.services["expenses"].refresh_expenses(is_cancelled=cancelled)
        repository = self.runtime.repositories["expenses"]
        self.assertTrue(report["cancelled"])
        self.assertFalse(report["materialized"])
        self.assertEqual(repository.count_source_records(), 1)
        self.assertEqual(repository.list_transactions(), [])
        self.assertEqual(repository.list_import_journals(provider_id="bank-csv")[0]["status"], "cancelled")
        recovered = self.runtime.services["expenses"].refresh_expenses()
        self.assertTrue(recovered["materialized"])
        self.assertEqual(len(repository.list_transactions()), 2)

    def test_cancelled_rebuild_restores_the_previous_repository_snapshot(self) -> None:
        service = self.runtime.services["expenses"]
        repository = self.runtime.repositories["expenses"]
        service.refresh_expenses()
        before = repository.list_transactions()
        with self.assertRaises(SourceIngestionCancelled):
            service.rebuild_expenses(is_cancelled=lambda: True)
        self.assertEqual(repository.list_transactions(), before)
        self.assertEqual(repository.count_source_records(), 1)

    def test_axios_archive_and_sms_backup_share_strong_provenance(self) -> None:
        archive = self.root / "phone.expense"
        transactions = [{"transactionKey": "phone-txn", "sourceMessageId": "phone-1", "bankName": "Axis", "accountSuffix": "1234", "direction": "debit", "amount": 120, "currency": "INR", "transactionId": "REF12345", "counterparty": "Cafe", "timestamp": "2026-09-09T10:00:00+05:30"}]
        identities = [{"platformMessageId": "phone-1"}]
        encoded_transactions = json.dumps(transactions).encode("utf-8")
        encoded_identities = json.dumps(identities).encode("utf-8")
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("manifest.json", json.dumps({"formatVersion": 1, "parserVersion": "axios-android-v1", "transactionsSha256": hashlib.sha256(encoded_transactions).hexdigest(), "sourceSmsIdentitiesSha256": hashlib.sha256(encoded_identities).hexdigest()}))
            output.writestr("transactions.json", encoded_transactions)
            output.writestr("source_sms_identities.json", encoded_identities)
        sms = self.root / "backup.xml"
        sms.write_text('<smses><sms _id="phone-1" address="AXISBK" body="A/c XX1234 debited INR 120.00 to Cafe UPI Ref: REF12345" date="1788928200000" /></smses>', encoding="utf-8")
        self.runtime.files.write_user("email_accounts.json", {"sync": {"lookbackDays": 3650}, "providers": [{"id": "axios", "type": "axios_archive", "enabled": True, "path": str(archive)}, {"id": "sms", "type": "sms_backup", "enabled": True, "path": str(sms)}]})
        self.runtime.services["expenses"].rebuild_expenses()
        repository = self.runtime.repositories["expenses"]
        transactions = repository.list_transactions()
        self.assertEqual(len(transactions), 1)
        self.assertEqual({row["providerId"] for row in repository.list_transaction_sources(transactions[0]["transactionKey"])}, {"axios", "sms"})

    def test_full_rebuild_accepts_isolated_eml_csv_sms_and_axios_sources(self) -> None:
        """Exercise each local source type from fresh, representative files."""

        mail_folder = self.root / "bank-alerts"
        mail_folder.mkdir()
        (mail_folder / "axis-debit.eml").write_text(
            "From: Axis Bank Alerts <alerts@axisbank.com>\n"
            "Subject: Debit notification from Axis Bank\n"
            "Date: Sun, 13 Sep 2026 09:00:01 +0530\n"
            "Content-Type: text/plain; charset=utf-8\n\n"
            "13-09-2026 Dear Example User, INR 1350.00 has been debited from A/c no. XX1234 "
            "on 13-09-26 at 09:00:00 IST. Info- UPI/P2M/123456789/EXAMPLE MARKET.",
            encoding="utf-8",
        )
        csv_path = self.root / "bank-export.csv"
        csv_path.write_text(
            "Date,Amount,Direction,Description,Reference,Account,Bank,Currency\n"
            "2026-09-13T10:00:00+05:30,350.00,debit,CSV Cafe,CSVREF001,1234,Axis,INR\n",
            encoding="utf-8",
        )
        sms_path = self.root / "phone-sms.xml"
        sms_path.write_text(
            '<smses><sms _id="sms-001" address="AXISBK" '
            'body="A/c XX1234 debited INR 450.00 to SMS Market UPI Ref: SMSREF001" '
            'date="1789288200000" /></smses>',
            encoding="utf-8",
        )
        archive_path = self.root / "axios-alternative.expense"
        archive_transactions = [
            {
                "transactionKey": "axios-001",
                "sourceMessageId": "axios-sms-001",
                "bankName": "Axis",
                "accountSuffix": "1234",
                "direction": "debit",
                "amount": 550.0,
                "currency": "INR",
                "transactionId": "AXIOSREF001",
                "counterparty": "Axios Market",
                "timestamp": "2026-09-13T11:00:00+05:30",
            }
        ]
        archive_identities = [{"platformMessageId": "axios-sms-001"}]
        encoded_transactions = json.dumps(archive_transactions).encode("utf-8")
        encoded_identities = json.dumps(archive_identities).encode("utf-8")
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "formatVersion": 1,
                        "parserVersion": "axios-android-v1",
                        "transactionsSha256": hashlib.sha256(encoded_transactions).hexdigest(),
                        "sourceSmsIdentitiesSha256": hashlib.sha256(encoded_identities).hexdigest(),
                    }
                ),
            )
            archive.writestr("transactions.json", encoded_transactions)
            archive.writestr("source_sms_identities.json", encoded_identities)

        mapping = {
            "date": "Date",
            "amount": "Amount",
            "direction": "Direction",
            "description": "Description",
            "reference": "Reference",
            "account": "Account",
            "bank": "Bank",
            "currency": "Currency",
        }
        self.runtime.files.write_user(
            "email_accounts.json",
            {
                "sync": {"lookbackDays": 3650, "overlapHours": 24},
                "providers": [
                    {"id": "fixture-eml", "type": "eml_folder", "enabled": True, "path": str(mail_folder)},
                    {"id": "fixture-csv", "type": "csv", "enabled": True, "path": str(csv_path), "mapping": mapping},
                    {"id": "fixture-sms", "type": "sms_backup", "enabled": True, "path": str(sms_path)},
                    {"id": "fixture-axios", "type": "axios_archive", "enabled": True, "path": str(archive_path)},
                ],
            },
        )

        report = self.runtime.services["expenses"].rebuild_expenses()
        repository = self.runtime.repositories["expenses"]
        transactions = repository.list_transactions()

        self.assertTrue(report["materialized"])
        self.assertEqual(report["providerErrors"], [])
        self.assertEqual(repository.count_source_records(), 4)
        self.assertEqual(len(transactions), 4)
        self.assertEqual(
            {row["transactionId"] for row in transactions},
            {"123456789", "CSVREF001", "SMSREF001", "AXIOSREF001"},
        )
        journals = repository.list_import_journals()
        self.assertEqual({row["providerId"] for row in journals}, {"fixture-eml", "fixture-csv", "fixture-sms", "fixture-axios"})
        self.assertTrue(all(row["status"] == "completed" for row in journals))

    def test_same_reference_disagreement_requires_review_then_allows_manual_or_priority_resolution(self) -> None:
        first_csv = self.root / "first.csv"
        second_csv = self.root / "second.csv"
        header = "Date,Amount,Direction,Description,Reference,Account,Bank,Currency\n"
        first_csv.write_text(header + "2026-09-09T10:00:00+05:30,120,debit,Cafe,REF-CONFLICT,1234,Axis,INR\n", encoding="utf-8")
        second_csv.write_text(header + "2026-09-09T10:00:00+05:30,220,debit,Cafe Updated,REF-CONFLICT,1234,Axis,INR\n", encoding="utf-8")
        mapping = {"date": "Date", "amount": "Amount", "direction": "Direction", "description": "Description", "reference": "Reference", "account": "Account", "bank": "Bank", "currency": "Currency"}
        self.runtime.files.write_user(
            "email_accounts.json",
            {"providers": [
                {"id": "first", "type": "csv", "enabled": True, "path": str(first_csv), "mapping": mapping},
                {"id": "second", "type": "csv", "enabled": True, "path": str(second_csv), "mapping": mapping},
            ]},
        )
        service = self.runtime.services["expenses"]
        repository = self.runtime.repositories["expenses"]
        service.rebuild_expenses()

        transactions = repository.list_transactions()
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["reconciliationStatus"], "needs_review")
        self.assertEqual(transactions[0]["amount"], 120.0)
        conflict = repository.list_reconciliation_conflicts()[0]
        self.assertEqual(conflict["candidateCount"], 2)
        self.assertEqual(len(repository.list_transaction_sources(transactions[0]["transactionKey"])), 2)

        service.set_reconciliation_policy("provider_priority", preferred_provider_id="second")
        automatically_resolved = repository.list_transactions()[0]
        self.assertEqual(automatically_resolved["amount"], 220.0)
        self.assertEqual(automatically_resolved["reconciliationStatus"], "auto_resolved")

        service.set_reconciliation_policy("manual")
        self.assertEqual(repository.list_transactions()[0]["reconciliationStatus"], "needs_review")
        conflict = repository.list_reconciliation_conflicts()[0]
        preferred_signature = next(item["signature"] for item in conflict["candidates"] if item["amount"] == 220.0)
        service.resolve_reconciliation_conflict(conflict["reconciliationKey"], preferred_signature)
        manually_resolved = repository.list_transactions()[0]
        self.assertEqual(manually_resolved["amount"], 220.0)
        self.assertEqual(manually_resolved["reconciliationStatus"], "user_resolved")

        second_csv.write_text(header, encoding="utf-8")
        service.refresh_expenses()
        self.assertEqual(repository.list_reconciliation_conflicts(), [])
        remaining = repository.list_transactions()[0]
        self.assertEqual(remaining["amount"], 120.0)
        self.assertEqual(remaining["reconciliationStatus"], "exact")


if __name__ == "__main__":
    unittest.main()
