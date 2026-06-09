from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from PyQt6.QtGui import QColor


class AppTheme:
    """Load and expose the app theme tokens."""

    def __init__(self, theme_path: Path) -> None:
        """Store the theme path and load the current tokens."""

        self.logger = logging.getLogger(self.__class__.__name__)
        self.theme_path = theme_path
        self.theme: dict[str, Any] = {}
        self.load(theme_path)

    def load(self, theme_path: Path) -> None:
        """Load the theme JSON from disk."""

        self.theme_path = theme_path
        self.logger.info("Loading theme from %s", theme_path)
        with theme_path.open("r", encoding="utf-8") as handle:
            self.theme = json.load(handle)

    def get(self, key: str, default: Any = None) -> Any:
        """Return one raw theme token."""

        return self.theme.get(key, default)

    def color(self, key: str, default: str | None = None) -> QColor:
        """Return one theme color token as QColor."""

        value = self.theme.get(key, default or "#ffffff")
        return QColor(value)

    def hex(self, key: str, default: str | None = None) -> str:
        """Return one theme color token as a hex string."""

        return str(self.theme.get(key, default or "#ffffff"))

    def font_family(self) -> str:
        """Return the preferred proportional UI font family."""

        return str(self.theme.get("font_family", "Inter"))

    def mono_font_family(self) -> str:
        """Return the preferred monospace font family."""

        return str(self.theme.get("mono_font_family", "JetBrains Mono"))
