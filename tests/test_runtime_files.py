from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.app.files import RuntimeFiles


class RuntimeFilesTests(unittest.TestCase):
    def test_app_icon_path_points_to_shipped_png(self) -> None:
        root_dir = Path(__file__).resolve().parents[1]
        files = RuntimeFiles(root_dir)

        self.assertEqual(files.app_icon_path(), root_dir / "assets" / "icons" / "expense_manager_matte.png")
        self.assertTrue(files.app_icon_path().exists())
        self.assertEqual(files.app_icon_ico_path(), root_dir / "assets" / "icons" / "expense_manager_matte.ico")
        self.assertTrue(files.app_icon_ico_path().exists())
        self.assertEqual(files.app_icon_light_path(), root_dir / "assets" / "icons" / "expense_manager_matte_light.png")
        self.assertTrue(files.app_icon_light_path().exists())
        self.assertEqual(files.app_icon_light_ico_path(), root_dir / "assets" / "icons" / "expense_manager_matte_light.ico")
        self.assertTrue(files.app_icon_light_ico_path().exists())

    def test_expense_manager_home_routes_writable_paths(self) -> None:
        root_dir = Path(__file__).resolve().parents[1]
        with patch.dict("os.environ", {"EXPENSE_MANAGER_HOME": "/tmp/expense-manager-test-home"}):
            files = RuntimeFiles(root_dir)

        home = Path("/tmp/expense-manager-test-home")
        self.assertEqual(files.user_path("email_accounts.json"), home / "config" / "email_accounts.json")
        self.assertEqual(files.user_local_path("thunderbird.json"), home / "config" / "local" / "thunderbird.json")
        self.assertEqual(files.state_path("expenses.json"), home / "state" / "expenses" / "expenses.json")
        self.assertEqual(files.db_path("accounts/example.db"), home / "data" / "databases" / "accounts" / "example.db")
        self.assertEqual(files.cache_path("thumbnails/index.json"), home / "cache" / "thumbnails" / "index.json")
        self.assertEqual(files.log_path(), home / "logs" / "expense_manager.log")


if __name__ == "__main__":
    unittest.main()
