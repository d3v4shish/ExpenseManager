from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.expenses.email.bank_alert_parser import BankAlertParser
from src.expenses.email.bank_rule_catalog import BankRuleCatalog
from src.expenses.email.mail_parser_chain import MailParserChain
from src.expenses.email.mail_types import ParsedFact, SourceRecord
from src.expenses.email.scripted_email_parser import ScriptedEmailParser


ROOT_DIR = Path(__file__).resolve().parents[1]


class ScriptedEmailParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.script_dir = self.root / "scripts"
        self.script_dir.mkdir()
        self.config_path = self.root / "script_extractors.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_missing_config_disables_parser(self) -> None:
        parser = ScriptedEmailParser(self.root / "missing.json")

        status = parser.status()

        self.assertFalse(status["enabled"])
        self.assertEqual(status["loadedCount"], 0)

    def test_matching_script_returns_transaction_and_expense_facts(self) -> None:
        self._write_config()
        self._write_script(
            "axis_card.py",
            """
EXTRACTOR_ID = "axis_card"
MATCH = {
    "from_contains": ["alerts@axis.bank.in"],
    "subject_contains": ["spent on credit card"]
}

def parse_email(email):
    return [{
        "bank_name": "Axis Bank",
        "direction": "debit",
        "amount": "4,431",
        "account_suffix": "XX1234",
        "counterparty": "EXAMPLESTORE",
        "timestamp": "2026-06-08T10:42:39+05:30",
        "transaction_id": ""
    }]
""",
        )
        parser = ScriptedEmailParser(self.config_path)

        result = parser.diagnose(self._record())

        self.assertTrue(result["candidateMatched"])
        self.assertEqual(result["attempts"][0]["parseStatus"], "parsed")
        self.assertEqual(len(result["facts"]), 2)
        transaction = next(item for item in result["facts"] if item.fact_type == "bank_transaction")
        self.assertEqual(transaction.parser_id, "scripted_email:axis_card")
        self.assertEqual(transaction.payload["direction"], "debit")
        self.assertEqual(transaction.payload["amount"], 4431.0)
        self.assertEqual(transaction.payload["accountSuffix"], "1234")
        self.assertEqual(transaction.payload["counterparty"], "EXAMPLESTORE")

    def test_repo_axis_credit_card_script_parses_spend_email(self) -> None:
        script_path = ROOT_DIR / "scripts" / "email_extractors" / "axis_credit_card_spend.py"
        self.config_path.write_text(
            json.dumps({"enabled": True, "paths": [str(script_path)]}),
            encoding="utf-8",
        )
        parser = ScriptedEmailParser(self.config_path)

        result = parser.diagnose(
            self._record(
                text_body=(
                    "Here's the summary of your Axis Bank Credit Card Transaction: "
                    "Transaction Amount: INR 4431 "
                    "Merchant Name: EXAMPLESTORE "
                    "Axis Bank Credit Card No. XX1234 "
                    "Date & Time: 08-06-2026, 10:42:39 IST "
                    "Available Limit*: INR 206551"
                )
            )
        )

        self.assertTrue(result["candidateMatched"])
        self.assertEqual(result["attempts"][0]["parseStatus"], "parsed")
        self.assertEqual(len(result["facts"]), 2)
        transaction = next(item for item in result["facts"] if item.fact_type == "bank_transaction")
        self.assertEqual(transaction.parser_id, "scripted_email:axis_credit_card_spend")
        self.assertEqual(transaction.payload["bankName"], "Axis Bank")
        self.assertEqual(transaction.payload["direction"], "debit")
        self.assertEqual(transaction.payload["amount"], 4431.0)
        self.assertEqual(transaction.payload["currency"], "INR")
        self.assertEqual(transaction.payload["accountSuffix"], "1234")
        self.assertEqual(transaction.payload["counterparty"], "EXAMPLESTORE")
        self.assertEqual(transaction.payload["timestamp"], "2026-06-08T10:42:39+05:30")

    def test_runtime_chain_uses_script_for_axis_credit_card_email(self) -> None:
        script_path = ROOT_DIR / "scripts" / "email_extractors" / "axis_credit_card_spend.py"
        self.config_path.write_text(
            json.dumps({"enabled": True, "paths": [str(script_path)]}),
            encoding="utf-8",
        )
        catalog = BankRuleCatalog(
            ROOT_DIR / "src" / "expenses" / "defaults" / "bank_email_rules.json",
            self.root / "bank_email_rules.overrides.json",
        )
        chain = MailParserChain()
        chain.add(BankAlertParser(catalog))
        chain.add(ScriptedEmailParser(self.config_path))

        result = chain.inspect_record(
            self._record(
                text_body=(
                    "Transaction Amount: INR 4431 "
                    "Merchant Name: EXAMPLESTORE "
                    "Axis Bank Credit Card No. XX1234 "
                    "Date & Time: 08-06-2026, 10:42:39 IST"
                )
            )
        )

        self.assertTrue(result["candidateMatched"])
        transactions = [item for item in result["facts"] if item.fact_type == "bank_transaction"]
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].parser_id, "scripted_email:axis_credit_card_spend")
        self.assertEqual(transactions[0].payload["amount"], 4431.0)

    def test_script_is_skipped_when_chain_already_has_facts_by_default(self) -> None:
        self._write_config()
        self._write_script(
            "fallback.py",
            """
EXTRACTOR_ID = "fallback"
MATCH = {"subject_contains": ["spent on credit card"]}

def parse_email(email):
    return [{
        "direction": "debit",
        "amount": 999,
        "account_suffix": "9999"
    }]
""",
        )
        chain = MailParserChain()
        chain.add(_AlwaysParsedParser())
        chain.add(ScriptedEmailParser(self.config_path))

        result = chain.inspect_record(self._record())

        self.assertTrue(result["candidateMatched"])
        self.assertEqual(len(result["facts"]), 1)
        self.assertEqual(result["facts"][0].parser_id, "dummy")

    def test_run_on_parsed_allows_script_augmentation(self) -> None:
        self._write_config()
        self._write_script(
            "augment.py",
            """
EXTRACTOR_ID = "augment"
RUN_ON_PARSED = True
MATCH = {"subject_contains": ["spent on credit card"]}

def parse_email(email):
    return [{
        "direction": "credit",
        "amount": 1,
        "account_suffix": "1234"
    }]
""",
        )
        chain = MailParserChain()
        chain.add(_AlwaysParsedParser())
        chain.add(ScriptedEmailParser(self.config_path))

        result = chain.inspect_record(self._record())

        self.assertEqual(len(result["facts"]), 2)
        self.assertEqual([fact.parser_id for fact in result["facts"]], ["dummy", "scripted_email:augment"])

    def test_later_default_scripts_skip_after_first_script_success(self) -> None:
        self._write_config()
        self._write_script(
            "first.py",
            """
EXTRACTOR_ID = "first"
ORDER = 10
MATCH = {"subject_contains": ["spent on credit card"]}

def parse_email(email):
    return [{"direction": "debit", "amount": 10, "account_suffix": "1111"}]
""",
        )
        self._write_script(
            "second.py",
            """
EXTRACTOR_ID = "second"
ORDER = 20
MATCH = {"subject_contains": ["spent on credit card"]}

def parse_email(email):
    return [{"direction": "debit", "amount": 20, "account_suffix": "2222"}]
""",
        )
        parser = ScriptedEmailParser(self.config_path)

        result = parser.diagnose(self._record())

        transactions = [item for item in result["facts"] if item.fact_type == "bank_transaction"]
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].parser_id, "scripted_email:first")

    def test_script_load_errors_are_reported_in_status(self) -> None:
        self._write_config()
        self._write_script("broken.py", "raise RuntimeError('boom')\n")

        parser = ScriptedEmailParser(self.config_path)

        status = parser.status()
        self.assertEqual(status["loadedCount"], 0)
        self.assertEqual(len(status["loadErrors"]), 1)
        self.assertIn("Import failed", status["loadErrors"][0]["error"])

    def test_invalid_output_becomes_failed_attempt(self) -> None:
        self._write_config()
        self._write_script(
            "invalid.py",
            """
EXTRACTOR_ID = "invalid"
MATCH = {"subject_contains": ["spent on credit card"]}

def parse_email(email):
    return [{"direction": "debit", "amount": "not-a-number", "account_suffix": "1234"}]
""",
        )
        parser = ScriptedEmailParser(self.config_path)

        result = parser.diagnose(self._record())

        self.assertTrue(result["candidateMatched"])
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["attempts"][0]["parseStatus"], "failed")
        self.assertEqual(result["attempts"][0]["reasonCode"], "script_error")
        self.assertIn("amount must be numeric", result["attempts"][0]["reasonText"])

    def _write_config(self) -> None:
        self.config_path.write_text(
            json.dumps({"enabled": True, "paths": [str(self.script_dir)]}),
            encoding="utf-8",
        )

    def _write_script(self, name: str, content: str) -> None:
        (self.script_dir / name).write_text(content.strip() + "\n", encoding="utf-8")

    def _record(self, *, text_body: str = "Transaction Amount: INR 4431 Merchant Name: EXAMPLESTORE") -> SourceRecord:
        subject = "INR 4431 spent on credit card no. XX1234"
        return SourceRecord(
            provider_id="thunderbird_local",
            record_type="email",
            external_id="script-email",
            title=subject,
            sender="Axis Bank Alerts <alerts@axis.bank.in>",
            received_at="2026-06-08T10:42:41+05:30",
            payload={
                "subject": subject,
                "textBody": text_body,
            },
        )


class _AlwaysParsedParser:
    parser_id = "dummy"

    def diagnose(self, record: SourceRecord, *, rules_payload=None) -> dict:
        _ = rules_payload
        return {
            "candidateMatched": True,
            "facts": [
                ParsedFact(
                    fact_type="bank_transaction",
                    parser_id=self.parser_id,
                    source_provider_id=record.provider_id,
                    source_record_type=record.record_type,
                    source_external_id=record.external_id,
                    payload={
                        "direction": "debit",
                        "amount": 10.0,
                        "accountSuffix": "1111",
                    },
                )
            ],
            "attempts": [],
        }


if __name__ == "__main__":
    unittest.main()
