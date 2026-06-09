from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.expenses.email.mail_types import ParsedFact, SourceRecord
from src.expenses.repositories.expenses_repository import ExpensesRepository
from src.expenses.services.expense_service import ExpensesService
from src.expenses.ui.analysis_workers import VendorDetailWorker
from src.expenses.ui.screen_api import ExpensesScreenApi


class _StateStore:
    def load(self, name: str) -> dict:
        return {}

    def save(self, name: str, payload: dict) -> None:
        _ = name, payload


class AnalysisQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = ExpensesRepository(Path(self.temp_dir.name) / "expenses.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_repository_returns_bounded_transaction_pages_and_vendor_summary(self) -> None:
        self.repository.upsert_transactions(
            [
                self._transaction("txn-a", "2026-06-03T10:00:00+05:30", "Grocer", "grocer", 120.0),
                self._transaction("txn-b", "2026-06-02T10:00:00+05:30", "Cafe", "cafe", 80.0),
                self._transaction("txn-c", "2026-05-02T10:00:00+05:30", "Grocer", "grocer", 40.0),
            ]
        )

        page = self.repository.list_transactions_page(year=2026, month=6, limit=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["rows"]), 1)
        self.assertTrue(page["hasMore"])

        summary = self.repository.list_vendor_summary(year=2026, month=6)
        self.assertEqual(summary[0]["vendorKey"], "grocer")
        self.assertEqual(summary[0]["amount"], 120.0)

        matches = self.repository.search_vendor_directory("caf")
        self.assertEqual(matches[0]["vendorKey"], "cafe")

    def test_transaction_search_uses_repository_index_with_like_fallback(self) -> None:
        self.repository.upsert_transactions(
            [
                self._transaction("txn-a", "2026-06-03T10:00:00+05:30", "Grocer", "grocer", 120.0),
                self._transaction("txn-b", "2026-06-02T10:00:00+05:30", "Cafe", "cafe", 80.0),
            ]
        )

        fts_page = self.repository.list_transactions_page(year=2026, month=6, search_text="groc")
        self.assertEqual(fts_page["total"], 1)
        self.assertEqual(fts_page["rows"][0]["transactionKey"], "txn-a")

        groups = self.repository.list_ledger_groups(year=2026, month=6, search_text="caf")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["groupKey"], "2026-06-02")

        with self.repository._connect() as conn:
            conn.execute("DROP TABLE IF EXISTS expense_transaction_search")
        fallback_page = self.repository.list_transactions_page(year=2026, month=6, search_text="cafe")
        self.assertEqual(fallback_page["total"], 1)
        self.assertEqual(fallback_page["rows"][0]["transactionKey"], "txn-b")

    def test_repository_batches_source_record_parse_results(self) -> None:
        first = self._source_record("email-a", "hash-a", "2026-06-03T10:00:00+05:30")
        second = self._source_record("email-b", "hash-b", "2026-06-04T10:00:00+05:30")
        attempt = {
            "parserId": "bank_alert_email:test",
            "parseStatus": "parsed",
            "factsCount": 1,
            "preview": {"merchant": "Grocer"},
        }

        results = self.repository.upsert_source_record_parse_results(
            [
                (first, [attempt], [self._fact("email-a", 120.0)]),
                (second, [attempt], [self._fact("email-b", 80.0)]),
            ]
        )

        self.assertEqual([result["changed"] for result in results], [True, True])
        self.assertEqual(self.repository.count_source_records(), 2)
        self.assertEqual(len(self.repository.list_facts("bank_transaction")), 2)

        unchanged = self.repository.upsert_source_record_parse_results(
            [(first, [attempt], [self._fact("email-a", 999.0)])]
        )
        self.assertFalse(unchanged[0]["changed"])
        self.assertFalse(unchanged[0]["written"])

        reparsed = self.repository.upsert_source_record_parse_results(
            [(first, [attempt], [self._fact("email-a", 999.0)])],
            reparse_all=True,
        )
        self.assertFalse(reparsed[0]["changed"])
        self.assertTrue(reparsed[0]["written"])
        updated = [
            row
            for row in self.repository.list_facts("bank_transaction")
            if row["externalId"] == "email-a"
        ]
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]["payload"]["amount"], 999.0)

    def test_vendor_transactions_are_decorated_for_detail_worker(self) -> None:
        self.repository.upsert_transactions(
            [
                self._transaction("txn-a", "2026-06-03T10:00:00+05:30", "Grocer", "grocer", 120.0),
                self._transaction("txn-b", "2026-06-02T10:00:00+05:30", "Cafe", "cafe", 80.0),
            ]
        )
        screen_api = ExpensesScreenApi(
            expenses_service=None,
            expenses_repository=self.repository,
            vendor_catalog_service=None,
        )

        rows = screen_api.list_vendor_transactions(alias_key="grocer")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["vendorKey"], "grocer")
        self.assertEqual(rows[0]["vendor"], "Grocer")

        worker = VendorDetailWorker(
            lookup_vendor=lambda _value: None,
            vendor_name="Grocer",
            alias_key="grocer",
            cache_key=("Grocer", "grocer", 2026, 6, False),
            all_rows=rows,
            recurring_patterns=[],
            selected_year=2026,
            selected_month=6,
            dismissed_suggestions=set(),
        )
        detail = worker._build_detail()
        self.assertEqual(detail["vendorKey"], "grocer")
        self.assertEqual(detail["transactionCount"], 1)

    def test_analysis_snapshot_keeps_transactions_bounded(self) -> None:
        rows = [
            self._transaction(
                f"txn-{index:03d}",
                f"2026-06-{(index % 28) + 1:02d}T10:00:00+05:30",
                f"Vendor {index % 5}",
                f"vendor-{index % 5}",
                float(index + 1),
            )
            for index in range(240)
        ]
        self.repository.upsert_transactions(rows)
        service = ExpensesService(
            expenses_repository=self.repository,
            mail_ingestion_service=None,
            state_store=_StateStore(),
            vendor_catalog_service=None,
        )

        payload = service.build_analysis_snapshot(year=2026, month=6)

        self.assertEqual(payload["meta"]["analysisDataMode"], "db-backed-bootstrap")
        self.assertEqual(payload["meta"]["visibleTransactionCount"], 240)
        self.assertEqual(payload["meta"]["loadedTransactionCount"], 200)
        self.assertEqual(len(payload["transactions"]), 200)
        self.assertEqual(payload["selectedVendorDetail"], {})

    def _transaction(self, key: str, timestamp: str, vendor: str, alias_key: str, amount: float) -> dict:
        return {
            "transactionKey": key,
            "providerId": "test",
            "externalId": key,
            "parserId": "bank_alert_email:test",
            "bankName": "Test Bank",
            "accountSuffix": "1234",
            "direction": "debit",
            "amount": amount,
            "currency": "INR",
            "transactionId": key,
            "counterparty": vendor,
            "rawCounterparty": vendor,
            "resolvedVendor": vendor,
            "canonicalVendor": vendor,
            "canonicalAlias": vendor,
            "aliasKey": alias_key,
            "category": "Uncategorized",
            "subcategory": "",
            "vendorMatchSource": "fallback",
            "timestamp": timestamp,
            "year": 2026,
            "month": int(timestamp[5:7]),
            "title": key,
            "sender": "alerts@example.com",
        }

    def _source_record(self, external_id: str, content_hash: str, received_at: str) -> SourceRecord:
        return SourceRecord(
            provider_id="test-provider",
            record_type="email",
            external_id=external_id,
            title=f"Alert {external_id}",
            sender="alerts@example.com",
            received_at=received_at,
            payload={"externalId": external_id},
            content_hash=content_hash,
        )

    def _fact(self, external_id: str, amount: float) -> ParsedFact:
        return ParsedFact(
            fact_type="bank_transaction",
            parser_id="bank_alert_email:test",
            source_provider_id="test-provider",
            source_record_type="email",
            source_external_id=external_id,
            payload={"amount": amount},
            confidence=1.0,
        )


if __name__ == "__main__":
    unittest.main()
