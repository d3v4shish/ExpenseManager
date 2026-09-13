from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.expenses.email.thunderbird_profile import discover_default_thunderbird_profiles
from src.expenses.services.mail_ingestion import ExpensesMailIngestionService


class _ConfigStore:
    def __init__(self, payload: dict | None = None, *, missing: bool = False) -> None:
        self.payload = payload or {}
        self.missing = missing

    def load_user(self, name: str) -> dict:
        if self.missing:
            raise FileNotFoundError(name)
        return self.payload


class ThunderbirdValidationTests(unittest.TestCase):
    def test_validation_accepts_configured_mailbox_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mailbox_path = Path(temp_dir) / "Inbox"
            mailbox_path.write_text("", encoding="utf-8")
            service = self._service(
                {
                    "providers": [
                        {
                            "id": "thunderbird_local",
                            "type": "thunderbird",
                            "enabled": True,
                            "mailboxPaths": [str(mailbox_path)],
                        }
                    ]
                }
            )

            result = service.validate_thunderbird_directory()

            self.assertTrue(result["valid"])
            self.assertEqual(result["mailboxPaths"], [str(mailbox_path)])

    def test_validation_reports_missing_thunderbird_source(self) -> None:
        service = self._service(
            {
                "providers": [
                    {
                        "id": "thunderbird_local",
                        "type": "thunderbird",
                        "enabled": True,
                        "mailboxPaths": ["/missing/thunderbird/Inbox"],
                    }
                ]
            }
        )

        result = service.validate_thunderbird_directory()

        self.assertFalse(result["valid"])
        self.assertEqual(result["mailboxPaths"], [])
        self.assertEqual(result["providers"][0]["missingMailboxPaths"], ["/missing/thunderbird/Inbox"])

    def test_default_profile_discovery_reads_profiles_ini(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            profile = home / ".thunderbird" / "abcd.default-release"
            mailbox_root = profile / "ImapMail" / "imap.example.test"
            mailbox_root.mkdir(parents=True)
            (mailbox_root / "Inbox").write_text("", encoding="utf-8")
            (profile / "prefs.js").write_text(
                "\n".join(
                    [
                        'user_pref("mail.account.account1.server", "server1");',
                        'user_pref("mail.account.account1.identities", "id1");',
                        'user_pref("mail.identity.id1.useremail", "bank@example.test");',
                        'user_pref("mail.server.server1.type", "imap");',
                        'user_pref("mail.server.server1.name", "Example Mail");',
                        'user_pref("mail.server.server1.directory-rel", "[ProfD]ImapMail/imap.example.test");',
                    ]
                ),
                encoding="utf-8",
            )
            (home / ".thunderbird" / "profiles.ini").write_text(
                "\n".join(
                    [
                        "[Profile0]",
                        "Name=default-release",
                        "IsRelative=1",
                        "Path=abcd.default-release",
                    ]
                ),
                encoding="utf-8",
            )

            profiles = discover_default_thunderbird_profiles(home=home)

            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0]["accounts"][0]["email"], "bank@example.test")
            self.assertEqual(profiles[0]["accounts"][0]["defaultMailboxRel"], "ImapMail/imap.example.test/Inbox")

    def test_default_profile_discovery_checks_each_home_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            homes_root = Path(temp_dir) / "home"
            self._write_profile(homes_root / "alice", "alice@example.test")
            self._write_profile(homes_root / "bob", "bob@example.test")

            profiles = discover_default_thunderbird_profiles(homes_root=homes_root)

            self.assertEqual(
                {profile["accounts"][0]["email"] for profile in profiles if profile.get("accounts")},
                {"alice@example.test", "bob@example.test"},
            )

    def test_validation_reports_missing_config(self) -> None:
        service = self._service({}, missing=True)

        result = service.validate_thunderbird_directory()

        self.assertFalse(result["valid"])
        self.assertIn("Thunderbird account", result["message"])

    def _service(self, payload: dict, *, missing: bool = False) -> ExpensesMailIngestionService:
        return ExpensesMailIngestionService(
            repository=None,
            parser_chain=None,
            config_store=_ConfigStore(payload, missing=missing),
            thunderbird_local_path=Path("/missing/thunderbird.json"),
        )

    @staticmethod
    def _write_profile(home: Path, email: str) -> None:
        profile = home / ".thunderbird" / "fixture.default-release"
        mailbox_root = profile / "ImapMail" / "imap.example.test"
        mailbox_root.mkdir(parents=True)
        (mailbox_root / "Inbox").write_text("", encoding="utf-8")
        (profile / "prefs.js").write_text(
            "\n".join(
                [
                    'user_pref("mail.account.account1.server", "server1");',
                    'user_pref("mail.account.account1.identities", "id1");',
                    f'user_pref("mail.identity.id1.useremail", "{email}");',
                    'user_pref("mail.server.server1.type", "imap");',
                    'user_pref("mail.server.server1.name", "Example Mail");',
                    'user_pref("mail.server.server1.directory-rel", "[ProfD]ImapMail/imap.example.test");',
                ]
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
