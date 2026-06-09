from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtWidgets import QWidget


class BaseWidget(QWidget):
    """Provide shared behavior for app-owned widgets."""

    def __init__(
        self,
        widget_config: dict[str, Any],
        widget_data: dict[str, Any],
        theme,
        screen_api,
        window_api,
        parent: QWidget | None = None,
    ) -> None:
        """Store widget state and the app/domain callbacks."""

        super().__init__(parent)
        self.widget_config = widget_config
        self.widget_data = widget_data
        self.theme = theme
        self.screen_api = screen_api
        self.window_api = window_api
        self.logger = logging.getLogger(self.__class__.__name__)
        self.datasource_file = self._describe_datasource(widget_config)
        self.card_ref = ""

    def bind_card_ref(self, card_ref: str) -> None:
        """Bind the card reference used for targeted reloads."""

        self.card_ref = card_ref
        self.logger.debug("Bound widget to card_ref=%s datasource=%s", card_ref, self.datasource_file or "<none>")

    def reload_self(self) -> None:
        """Ask the app window to reload this widget's card."""

        if not self.card_ref:
            self.logger.warning("Cannot reload widget without card_ref datasource=%s", self.datasource_file or "<none>")
            return
        self.window_api.reload_card(self.card_ref)

    def reload_all(self) -> None:
        """Ask the app window to reload all mounted cards."""

        self.window_api.reload_all_cards()

    def reload_cards(self, *card_ids: str) -> None:
        """Ask the app window to reload a specific set of mounted cards."""

        callback = getattr(self.window_api, "reload_cards", None)
        if callable(callback):
            callback(card_ids)
            return
        self.reload_all()

    def navigate_to(self, tab_id: str) -> None:
        """Ask the app window to navigate to one tab target."""

        self.window_api.navigate_to_tab(tab_id)

    def is_card_visible(self, card_id: str | None = None) -> bool:
        """Return whether one mounted card is currently visible."""

        target = card_id or self.card_ref
        if not target:
            return True
        callback = getattr(self.window_api, "is_card_visible", None)
        if callable(callback):
            return bool(callback(target))
        return True

    def toggle_card(self, card_id: str, *, scroll_into_view: bool = False) -> bool:
        """Toggle one mounted card and return the new visibility state."""

        callback = getattr(self.window_api, "toggle_card_visibility", None)
        if callable(callback):
            return bool(callback(card_id, scroll_into_view=scroll_into_view))
        return False

    def apply_reload(self, widget_config: dict[str, Any], widget_data: dict[str, Any]) -> bool:
        """Apply new widget data in place when the widget supports it."""

        if self.widget_config == widget_config and self.widget_data == widget_data:
            self.logger.debug(
                "Skipping in-place widget reload because config/data are unchanged card_ref=%s datasource=%s",
                self.card_ref or "<unknown>",
                self.datasource_file or "<none>",
            )
            return True
        self.widget_config = widget_config
        self.widget_data = widget_data
        self.datasource_file = self._describe_datasource(widget_config)
        render = getattr(self, "_render", None)
        if callable(render):
            self.logger.info("Applying in-place widget reload card_ref=%s datasource=%s", self.card_ref or "<unknown>", self.datasource_file or "<none>")
            render()
            self.update()
            self.repaint()
            return True
        self.logger.debug("Widget does not implement in-place reload card_ref=%s", self.card_ref or "<unknown>")
        return False

    def clear_layout(self, layout) -> None:
        """Delete every widget and nested layout owned by one layout."""

        while layout.count():
            item = layout.takeAt(0)
            child_widget = item.widget()
            child_layout = item.layout()
            if child_layout is not None:
                self.clear_layout(child_layout)
            if child_widget is not None:
                child_widget.deleteLater()

    def _describe_datasource(self, widget_config: dict[str, Any]) -> str:
        """Return a compact label for the widget's configured datasource."""

        datasource = widget_config.get("datasource", {})
        filename = str(datasource.get("file", "")).strip()
        if filename:
            return filename
        files = datasource.get("files", [])
        if isinstance(files, list):
            return ",".join(str(item) for item in files if item)
        return ""


__all__ = ["BaseWidget"]
