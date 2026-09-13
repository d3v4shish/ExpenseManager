from __future__ import annotations

import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from src.app.cli import run_cli
from src.expenses.bootstrap import build_runtime
from src.expenses.email.bank_rule_catalog import BankRuleCatalog
from src.expenses.email.mail_types import SourceRecord
from src.expenses.email.template_miner import BankAlertTemplateMiner
from src.expenses.repositories.expenses_repository import ExpensesRepository
from src.expenses.services.template_mining import TemplateMiningService


class TemplateMinerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            {
                "sourceRecordId": 2,
                "externalId": "mail-2",
                "sender": "alerts@axisbank.com",
                "payload": {
                    "subject": "Transaction alert",
                    "textBody": "Your A/c XX1234 has been debited for INR 220.00 at Cafe Two. UPI Ref: ZYXWV98765",
                },
            },
            {
                "sourceRecordId": 1,
                "externalId": "mail-1",
                "sender": "alerts@axisbank.com",
                "payload": {
                    "subject": "Transaction alert",
                    "textBody": "Your A/c XX1234 has been debited for INR 120.00 at Cafe One. UPI Ref: ABCDE12345",
                },
            },
            {
                "sourceRecordId": 3,
                "externalId": "mail-3",
                "sender": "notice@otherbank.com",
                "payload": {"subject": "One alert", "textBody": "INR 50.00 debited"},
            },
        ]
        self.miner = BankAlertTemplateMiner()

    def test_clusters_are_deterministic_masked_and_bounded(self) -> None:
        first = self.miner.mine(self.records, min_support=2)
        second = self.miner.mine(list(reversed(self.records)), min_support=2)

        self.assertEqual(first, second)
        self.assertEqual(first["scannedRecords"], 3)
        self.assertEqual(len(first["templates"]), 1)
        template = first["templates"][0]
        self.assertEqual(template["support"], 2)
        self.assertEqual(template["senderHint"], "axisbank.com")
        self.assertEqual(template["sourceRecordIds"], [1, 2])
        self.assertIn("<AMOUNT>", template["bodyTemplate"])
        self.assertIn("<ID>", template["bodyTemplate"])
        self.assertNotIn("Cafe", template["bodyTemplate"])
        self.assertNotIn("ABCDE12345", template["bodyTemplate"])

    def test_rule_draft_is_unique_and_never_an_activation(self) -> None:
        template = self.miner.mine(self.records, min_support=2)["templates"][0]
        draft = self.miner.build_rule_draft(template, existing_ids={"mined_axisbank_com"})

        self.assertEqual(draft["id"], "mined_axisbank_com_2")
        self.assertEqual(draft["match"]["fromContains"], ["axisbank.com"])
        self.assertEqual(draft["match"]["subjectContains"], ["transaction alert"])
        self.assertEqual(draft["templateMining"]["templateKey"], template["templateKey"])
        self.assertNotIn("enabled", draft)

    def test_service_mines_stored_messages_and_draft_does_not_write_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = ExpensesRepository(root / "expenses.db")
            for record in self.records[:2]:
                source = SourceRecord(
                    provider_id="thunderbird_local",
                    record_type="email",
                    external_id=str(record["externalId"]),
                    title=str(record["payload"]["subject"]),
                    sender=str(record["sender"]),
                    received_at="2026-09-09T10:00:00+00:00",
                    payload=dict(record["payload"]),
                    content_hash=str(record["externalId"]),
                )
                repository.upsert_source_record_parse_results([(source, [], [])])
            override_path = root / "bank_email_rules.overrides.json"
            catalog = BankRuleCatalog(
                Path(__file__).resolve().parents[1] / "src" / "expenses" / "defaults" / "bank_email_rules.json",
                override_path,
            )
            service = TemplateMiningService(expenses_repository=repository, bank_rule_catalog=catalog)

            mined = service.mine(min_support=2)
            draft = service.build_draft(mined["templates"][0]["templateKey"], min_support=2)

            self.assertEqual(mined["templates"][0]["support"], 2)
            self.assertFalse(draft["saved"])
            self.assertEqual(draft["activation"], "review_and_save_required")
            self.assertFalse(override_path.exists())

    def test_cli_mine_and_draft_are_json_and_exclude_message_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(__file__).resolve().parents[1]
            with mock.patch.dict("os.environ", {"EXPENSE_MANAGER_HOME": str(Path(temp_dir) / "runtime")}):
                runtime = build_runtime(project_root)
                for record in self.records[:2]:
                    source = SourceRecord(
                        provider_id="thunderbird_local",
                        record_type="email",
                        external_id=str(record["externalId"]),
                        title=str(record["payload"]["subject"]),
                        sender=str(record["sender"]),
                        received_at="2026-09-09T10:00:00+00:00",
                        payload=dict(record["payload"]),
                        content_hash=str(record["externalId"]),
                    )
                    runtime.repositories["expenses"].upsert_source_record_parse_results([(source, [], [])])
                output = io.StringIO()
                with redirect_stdout(output):
                    mine_code = run_cli(["--format", "json", "templates", "mine"], project_root)
                mined = json.loads(output.getvalue())["result"]
                self.assertEqual(mine_code, 0)
                self.assertNotIn("Cafe", json.dumps(mined))
                template_key = mined["templates"][0]["templateKey"]
                output = io.StringIO()
                with redirect_stdout(output):
                    draft_code = run_cli(["--format", "json", "templates", "draft", "--key", template_key], project_root)
                draft = json.loads(output.getvalue())["result"]
                self.assertEqual(draft_code, 0)
                self.assertFalse(draft["saved"])
                self.assertEqual(draft["activation"], "review_and_save_required")


if __name__ == "__main__":
    unittest.main()
