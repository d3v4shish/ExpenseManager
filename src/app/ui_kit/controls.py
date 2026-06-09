from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QWidget

_UI_KIT_THEME = {
    "font_family": "Inter",
    "mono_font_family": "JetBrains Mono",
    "text_primary": "#ffffff",
    "text_secondary": "#8b949e",
    "text_muted": "#6B7280",
    "text_disabled": "#5f6b78",
    "accent": "#6366f1",
    "focus_ring": "#78a6ff",
    "border": "#30363D",
    "divider": "#25303a",
    "surface_active": "#1a2432",
    "surface_disabled": "#0b1016",
    "surface_hover": "#1b2230",
    "surface_panel": "#131a22",
    "surface_panel_alt": "#0c1118",
    "window_bg": "#0d1117",
    "radius": 0,
}


def configure_ui_kit_theme(theme) -> None:
    """Load shared UI kit tokens from the active app theme."""

    _UI_KIT_THEME.update(
        {
            "font_family": theme.font_family(),
            "mono_font_family": theme.mono_font_family(),
            "text_primary": theme.hex("text_primary", "#ffffff"),
            "text_secondary": theme.hex("text_secondary", "#8b949e"),
            "text_muted": theme.hex("text_muted", "#6B7280"),
            "text_disabled": theme.hex("text_disabled", "#5f6b78"),
            "accent": theme.hex("accent", "#6366f1"),
            "focus_ring": theme.hex("focus_ring", theme.hex("accent", "#6366f1")),
            "border": theme.hex("border", "#30363D"),
            "divider": theme.hex("divider", theme.hex("border_soft", "#25303a")),
            "surface_active": theme.hex("surface_active", theme.hex("surface_hover", "#1a2432")),
            "surface_disabled": theme.hex("surface_disabled", theme.hex("card_alt_bg", "#0b1016")),
            "surface_hover": theme.hex("surface_hover", theme.hex("card_alt_bg", "#1b2230")),
            "surface_panel": theme.hex("surface_panel", theme.hex("card_bg", "#131a22")),
            "surface_panel_alt": theme.hex("surface_panel_alt", theme.hex("card_alt_bg", "#0c1118")),
            "window_bg": theme.hex("window_bg", "#0d1117"),
            "radius": int(theme.get("radius", 0) or 0),
        }
    )


def ui_kit_hex(key: str, default: str | None = None) -> str:
    """Return one configured UI kit token."""

    return str(_UI_KIT_THEME.get(key, default or "#ffffff"))


def _ui_kit_font_family(*, mono: bool) -> str:
    """Return the configured font family for one text role."""

    return str(_UI_KIT_THEME["mono_font_family" if mono else "font_family"])


def _ui_kit_radius(default: int = 0) -> int:
    """Return the configured shared radius."""

    try:
        return max(0, int(_UI_KIT_THEME.get("radius", default) or default))
    except (TypeError, ValueError):
        return default


def _lighter_hex(value: str, factor: int) -> str:
    """Return a lighter variant of one color token for hover feedback."""

    color = QColor(value)
    if not color.isValid():
        return value
    return color.lighter(factor).name()


def make_label(
    text: str,
    color: str,
    size: int = 11,
    bold: bool = False,
    *,
    mono: bool = False,
    family: str | None = None,
) -> QLabel:
    """Create the shared standalone-app text label."""

    label = QLabel(text)
    font = QFont(family or _ui_kit_font_family(mono=mono), pointSize=size)
    font.setBold(bold)
    label.setFont(font)
    label.setStyleSheet(
        f"QLabel {{ color: {color}; background: transparent; border: none; padding: 0px; }}"
    )
    return label


def make_button(text: str, border: str, fg: str, bg: str, *, emphasis: str = "neutral") -> QPushButton:
    """Create the shared standalone-app push button."""

    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    font = QFont(_ui_kit_font_family(mono=False), pointSize=9)
    font.setBold(True)
    button.setFont(font)
    if emphasis == "primary":
        hover_bg = _lighter_hex(bg, 106)
        hover_border = ui_kit_hex("focus_ring", ui_kit_hex("accent"))
        hover_fg = ui_kit_hex("text_primary")
    else:
        hover_bg = ui_kit_hex("surface_active")
        hover_border = ui_kit_hex("focus_ring", ui_kit_hex("accent"))
        hover_fg = fg
    button.setStyleSheet(
        f"""
        QPushButton {{
            color: {fg};
            background-color: {bg};
            border: 1px solid {border};
            border-radius: {_ui_kit_radius()}px;
            min-height: 30px;
            padding: 4px 10px;
        }}
        QPushButton:hover {{
            color: {hover_fg};
            background-color: {hover_bg};
            border-color: {hover_border};
        }}
        QPushButton:pressed {{
            color: {hover_fg};
            background-color: {hover_bg};
            border-color: {hover_border};
        }}
        QPushButton:checked {{
            color: {ui_kit_hex("text_primary")};
            background-color: {ui_kit_hex("surface_active")};
            border-color: {hover_border};
        }}
        QPushButton:focus {{
            border-color: {ui_kit_hex("focus_ring", ui_kit_hex("accent"))};
        }}
        QPushButton:disabled {{
            color: {ui_kit_hex("text_disabled")};
            background-color: {ui_kit_hex("surface_disabled")};
            border-color: {ui_kit_hex("divider", ui_kit_hex("border"))};
        }}
        QToolTip {{
            color: {ui_kit_hex("text_primary")};
            background-color: {ui_kit_hex("surface_panel_alt")};
            border: 1px solid {ui_kit_hex("divider", ui_kit_hex("border"))};
            padding: 4px 6px;
        }}
        """
    )
    return button


class PillLabel(QLabel):
    """Render a small pill-shaped status label."""

    def __init__(self, text: str, fg: str, border: str, bg: str) -> None:
        """Store pill colors and render the compact label style."""

        super().__init__(text)
        font = QFont(_ui_kit_font_family(mono=False), pointSize=8)
        font.setBold(True)
        self.setFont(font)
        self.setStyleSheet(
            f"QLabel {{ color: {fg}; background-color: {bg}; border: 1px solid {border}; border-radius: {_ui_kit_radius()}px; padding: 2px 7px; }}"
        )


class Dot(QWidget):
    """Render a glowing status dot."""

    def __init__(self, color: str, diameter: int = 8) -> None:
        """Store the dot appearance and reserve its fixed size."""

        super().__init__()
        self.color = QColor(color)
        self.diameter = diameter
        self.setFixedSize(diameter, diameter)

    def paintEvent(self, event) -> None:  # noqa: N802
        """Paint the status dot with a crisp terminal-style ring."""

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(ui_kit_hex("window_bg")), 1))
        painter.setBrush(self.color)
        painter.drawEllipse(QRectF(1, 1, self.diameter - 2, self.diameter - 2))


class CardFrame(QFrame):
    """Draw the shared flat card surface used across the app."""

    def __init__(self, bg: str, border: str, radius: int | None = None, glow: str | None = None) -> None:
        """Store card colors and enable translucent custom painting."""

        super().__init__()
        self.bg_color = QColor(bg)
        self.border_color = QColor(border)
        self.accent_color = QColor(glow or "#00000000")
        self.radius = _ui_kit_radius() if radius is None else max(0, int(radius))
        self.hovered = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

    def paintEvent(self, event) -> None:  # noqa: N802
        """Paint the card background, border, and optional thin accent rail."""

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        rect = self.rect().adjusted(1, 1, -1, -1)
        frame_rect = QRectF(rect)

        fill = QColor(self.bg_color)
        if self.hovered:
            fill = QColor(fill).lighter(103)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(frame_rect, self.radius, self.radius)

        border = QColor(self.border_color)
        border.setAlpha(255)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(frame_rect, self.radius, self.radius)

        if self.accent_color.alpha() > 0:
            accent = QColor(self.accent_color)
            accent.setAlpha(190 if self.hovered else 164)
            painter.setPen(QPen(accent, 1.0))
            painter.drawLine(
                int(frame_rect.left() + 2),
                int(frame_rect.top() + 3),
                int(frame_rect.left() + 2),
                int(frame_rect.bottom() - 3),
            )

    def enterEvent(self, event) -> None:  # noqa: N802
        """Raise the card surface slightly on hover."""

        self.hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        """Restore the default card surface when the pointer exits."""

        self.hovered = False
        self.update()
        super().leaveEvent(event)


__all__ = ["CardFrame", "Dot", "PillLabel", "configure_ui_kit_theme", "make_button", "make_label", "ui_kit_hex"]
