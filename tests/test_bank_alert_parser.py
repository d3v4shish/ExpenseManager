from __future__ import annotations

import html
import tempfile
import unittest
from pathlib import Path

from src.expenses.email.bank_alert_parser import BankAlertParser
from src.expenses.email.bank_rule_catalog import BankRuleCatalog, build_bank_rule_template
from src.expenses.email.extractors.base import BankExtractionHelpers
from src.expenses.email.mail_types import SourceRecord
from src.expenses.email.thunderbird_reader import ThunderbirdMailboxReader


ROOT_DIR = Path(__file__).resolve().parents[1]


class AxisBankParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.catalog = BankRuleCatalog(
            ROOT_DIR / "src" / "expenses" / "defaults" / "bank_email_rules.json",
            Path(self.temp_dir.name) / "bank_email_rules.overrides.json",
        )
        self.parser = BankAlertParser(self.catalog)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_axis_parser_handles_encoded_html_summary_email(self) -> None:
        html_body = """
        <html>
          <head>
            <style type="text/css">.hidden { display: none; }</style>
          </head>
          <body>
            <div>11-03-2026</div>
            <p>Dear Example User, Here's the summary of your transaction:</p>
            <p>Amount Debited: INR 286.00</p>
            <p>Account Number: XX1234</p>
            <p>Date &amp; Time: 11-03-26, 14:21:52 IST</p>
            <p>Transaction Info: UPI/P2M/234567891/BBNOW</p>
          </body>
        </html>
        """
        encoded_html = html.escape(html.escape(html_body))
        subject = "INR 286.00 was debited from your A/c no. XX1234."
        result = self.parser.diagnose(
            SourceRecord(
                provider_id="thunderbird_local",
                record_type="email",
                external_id="axis-encoded-html",
                title=subject,
                sender='"alerts@axis.bank.in" <alerts@axis.bank.in>',
                received_at="2026-03-11T14:21:53+05:30",
                payload={
                    "subject": subject,
                    "htmlBody": encoded_html,
                },
            )
        )

        self.assertTrue(result["candidateMatched"])
        self.assertEqual(result["attempts"][0]["parseStatus"], "parsed")
        facts = list(result["facts"])
        self.assertEqual(len(facts), 2)
        transaction = next(item for item in facts if item.fact_type == "bank_transaction")
        self.assertEqual(transaction.payload["direction"], "debit")
        self.assertEqual(transaction.payload["amount"], 286.0)
        self.assertEqual(transaction.payload["accountSuffix"], "1234")
        self.assertEqual(transaction.payload["transactionId"], "234567891")
        self.assertEqual(transaction.payload["counterparty"], "Bbnow")
        self.assertEqual(transaction.payload["timestamp"], "2026-03-11T14:21:52+05:30")

    def test_axis_parser_handles_legacy_has_been_debited_format(self) -> None:
        subject = "Debit notification from Axis Bank"
        result = self.parser.diagnose(
            SourceRecord(
                provider_id="thunderbird_local",
                record_type="email",
                external_id="axis-legacy-text",
                title=subject,
                sender="Axis Bank Alerts <alerts@axisbank.com>",
                received_at="2024-12-07T18:11:37+05:30",
                payload={
                    "subject": subject,
                    "textBody": (
                        "07-12-2024 Dear Example User, INR 1350.00 has been debited from A/c no. XX1234 "
                        "on 07-12-24 at 18:11:36 IST. Info- UPI/P2M/123456789/EXAMPLE MARKET."
                    ),
                },
            )
        )

        self.assertTrue(result["candidateMatched"])
        self.assertEqual(result["attempts"][0]["parseStatus"], "parsed")
        facts = list(result["facts"])
        transaction = next(item for item in facts if item.fact_type == "bank_transaction")
        self.assertEqual(transaction.payload["direction"], "debit")
        self.assertEqual(transaction.payload["amount"], 1350.0)
        self.assertEqual(transaction.payload["accountSuffix"], "1234")
        self.assertEqual(transaction.payload["transactionId"], "123456789")
        self.assertEqual(transaction.payload["counterparty"], "Example Market")
        self.assertEqual(transaction.payload["timestamp"], "2024-12-07T18:11:36+05:30")


class CitiAndSbiBankParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.catalog = BankRuleCatalog(
            ROOT_DIR / "src" / "expenses" / "defaults" / "bank_email_rules.json",
            Path(self.temp_dir.name) / "bank_email_rules.overrides.json",
        )
        self.parser = BankAlertParser(self.catalog)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_citi_credit_acknowledgement_parses_as_credit_transaction(self) -> None:
        self.catalog.save_overrides({"banks": [_citi_acknowledgement_rule()]})
        subject = "CitiAlert - UPI Fund Transfer Acknowledgement"
        html_body = """
        <html>
          <body>
            <p>UPI Fund Transfer - Credit to your Citibank Account.</p>
            <p>Your Citibank A/C has been credited with INR 160.00 on 02-May-2024 at 13:34 received from payer@example.</p>
            <p>UPI Ref No. 345678912</p>
          </body>
        </html>
        """
        result = self.parser.diagnose(
            SourceRecord(
                provider_id="thunderbird_local",
                record_type="email",
                external_id="citi-credit-html",
                title=subject,
                sender="Citibank India <CitiAlert.India@citicorp.com>",
                received_at="2024-05-02T13:34:34+05:30",
                payload={
                    "subject": subject,
                    "htmlBody": html_body,
                },
            )
        )

        self.assertTrue(result["candidateMatched"])
        self.assertEqual(result["attempts"][0]["parseStatus"], "parsed")
        facts = list(result["facts"])
        self.assertEqual(len(facts), 1)
        transaction = next(item for item in facts if item.fact_type == "bank_transaction")
        self.assertEqual(transaction.payload["direction"], "credit")
        self.assertEqual(transaction.payload["amount"], 160.0)
        self.assertEqual(transaction.payload["accountSuffix"], "1234")
        self.assertEqual(transaction.payload["transactionId"], "345678912")
        self.assertEqual(transaction.payload["counterparty"], "Payer@Example")
        self.assertEqual(transaction.payload["timestamp"], "2024-05-02T13:34:00+05:30")

    def test_sbi_debit_by_transfer_parses_as_debit_transaction(self) -> None:
        subject = "CBSSBI ALERT"
        result = self.parser.diagnose(
            SourceRecord(
                provider_id="thunderbird_local",
                record_type="email",
                external_id="sbi-debit-transfer",
                title=subject,
                sender="<cbsalerts.sbi@alerts.sbi.bank.in>",
                received_at="2026-02-17T06:57:20+05:30",
                payload={
                    "subject": subject,
                    "textBody": (
                        "Greetings from SBI ! Dear Customer, "
                        "Your A/C XXXXX451234 has a debit by transfer of Rs 236.00 on 17/02/26. "
                        "Avl Bal Rs 9,49,410.87.-SBI"
                    ),
                },
            )
        )

        self.assertTrue(result["candidateMatched"])
        self.assertEqual(result["attempts"][0]["parseStatus"], "parsed")
        facts = list(result["facts"])
        self.assertEqual(len(facts), 2)
        transaction = next(item for item in facts if item.fact_type == "bank_transaction")
        self.assertEqual(transaction.payload["direction"], "debit")
        self.assertEqual(transaction.payload["amount"], 236.0)
        self.assertEqual(transaction.payload["accountSuffix"], "1234")
        self.assertEqual(transaction.payload["timestamp"], "2026-02-17T06:57:20+05:30")

    def test_unknown_bank_like_email_is_reported_as_rule_miss(self) -> None:
        subject = "Debit alert for your account"
        result = self.parser.diagnose(
            SourceRecord(
                provider_id="thunderbird_local",
                record_type="email",
                external_id="unknown-bank-alert",
                title=subject,
                sender="Example Bank Alerts <alerts@examplebank.test>",
                received_at="2026-06-01T10:00:00+05:30",
                payload={
                    "subject": subject,
                    "textBody": "Your A/C XX1234 was debited by INR 450.00 using UPI transaction ref 123456789.",
                },
            )
        )

        self.assertTrue(result["candidateMatched"])
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["attempts"][0]["parseStatus"], "failed")
        self.assertEqual(result["attempts"][0]["reasonCode"], "no_matching_rule")
        self.assertIn("examplebank.test", result["attempts"][0]["preview"]["suggestedFromContains"])


class BankRuleTemplateTests(unittest.TestCase):
    def test_build_bank_rule_template_returns_unique_regex_scaffold(self) -> None:
        template = build_bank_rule_template(existing_ids={"new_bank", "new_bank_2"})

        self.assertEqual(template["id"], "new_bank_3")
        self.assertEqual(template["extractor"]["type"], "declarative")
        self.assertIn("alerts@example-bank.com", template["match"]["fromContains"])
        self.assertTrue(template["fields"]["amountPatterns"])
        self.assertTrue(template["fields"]["transactionIdPatterns"])
        self.assertTrue(template["fields"]["dateTimePatterns"])


def _citi_acknowledgement_rule() -> dict:
    return {
        "id": "example_citi_upi",
        "name": "Example Bank",
        "match": {
            "fromContains": ["citialert.india@citicorp.com"],
            "subjectContains": ["citialert - upi fund transfer acknowledgement"],
        },
        "extractor": {"type": "declarative"},
        "defaults": {
            "bankName": "Example Bank",
            "accountSuffix": "1234",
        },
        "fields": {
            "directionRules": [
                {"type": "debit", "patterns": ["upi fund transfer - debit", "has been debited with inr"]},
                {"type": "credit", "patterns": ["upi fund transfer - credit", "credit to your citibank account", "has been credited with inr"]},
            ],
            "amountPatterns": [
                "debited with inr\\s*([0-9,]+(?:\\.[0-9]{2})?)",
                "credited with inr\\s*([0-9,]+(?:\\.[0-9]{2})?)",
            ],
            "transactionIdPatterns": [
                "upi ref(?: no\\.?|erence no\\.?|erence)?\\s*([0-9]+)",
                "content-description:\\s*([0-9]+)@upidebt",
            ],
            "counterpartyPatterns": [
                "account\\s+([a-z0-9._@-]+)\\s+has been credited",
                "received from\\s+([a-z0-9._@-]+)",
            ],
            "accountSuffixPatterns": [],
            "dateTimePatterns": [
                "on\\s+(\\d{2}-[a-z]{3}-\\d{4})\\s+at\\s+(\\d{2}:\\d{2})",
            ],
        },
    }


class EmailCleanupTests(unittest.TestCase):
    def test_shared_normalizer_collapses_repeated_crlf_sequences(self) -> None:
        helpers = BankExtractionHelpers()

        result = helpers._normalize_text("Line 1\r\n\r\n\r\nLine 2\r\n\r\nLine 3")

        self.assertEqual(result, "Line 1\r\nLine 2\r\nLine 3")

    def test_thunderbird_reader_cleans_stored_body_text(self) -> None:
        reader = ThunderbirdMailboxReader({"id": "thunderbird_local"}, Path("."))

        result = reader._cleanup_body_text("Line 1\r\n\r\n\r\nLine 2\n\nLine 3\r\rLine 4")

        self.assertEqual(result, "Line 1\r\nLine 2\r\nLine 3\r\nLine 4")


if __name__ == "__main__":
    unittest.main()
