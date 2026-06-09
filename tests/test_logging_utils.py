from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.app.logging_utils import configure_app_logging


class LoggingUtilsTests(unittest.TestCase):
    def test_configure_app_logging_creates_rotating_log_path_once(self) -> None:
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        original_level = root_logger.level
        original_hook = sys.excepthook
        sentinel_name = "_expense_manager_logging_configured"
        original_sentinel = getattr(root_logger, sentinel_name, None)
        try:
            if hasattr(root_logger, sentinel_name):
                delattr(root_logger, sentinel_name)

            with tempfile.TemporaryDirectory() as temp_dir:
                root_path = Path(temp_dir)
                app_home = root_path / "runtime"
                with patch.dict("os.environ", {"EXPENSE_MANAGER_HOME": str(app_home)}):
                    first = configure_app_logging(root_path)
                    second = configure_app_logging(root_path)

                self.assertEqual(first, second)
                self.assertTrue(first.parent.exists())
                self.assertEqual(first.parent, app_home / "logs")
                self.assertEqual(first.name, "expense_manager.log")
                self.assertGreaterEqual(len(root_logger.handlers), 2)
                self.assertIsNot(sys.excepthook, original_hook)
                for handler in list(root_logger.handlers):
                    handler.close()
        finally:
            for handler in list(root_logger.handlers):
                handler.close()
            root_logger.handlers[:] = original_handlers
            root_logger.setLevel(original_level)
            sys.excepthook = original_hook
            if original_sentinel is None:
                if hasattr(root_logger, sentinel_name):
                    delattr(root_logger, sentinel_name)
            else:
                setattr(root_logger, sentinel_name, original_sentinel)


if __name__ == "__main__":
    unittest.main()
