"""Small palette-aware colour helpers.

Never hard-code a fixed hex for muted/secondary text -- `palette(mid)` (`QPalette.Mid`,
meant for bevel/groove shading, not text) and literal names like `"gray"` both land
close to the *background* colour on a dark palette (contrast ratio ~1.9:1, well under
WCAG's 4.5:1 floor for text), which is what made the progress page's elapsed-time/
counter/current-item lines nearly invisible in dark mode. `MUTED_STYLE` uses Qt's own
muted-text role instead (`QPalette.PlaceholderText`), which Qt keeps legible against
both a light and a dark `Window`/`Base` colour (~4.7:1 measured against a typical dark
palette here).

Qt has no equivalent built-in role for a "warning" colour, so `warning_style` below
picks between a light-theme and a dark-theme amber based on the current palette's
lightness -- computed once at construction, same as the rest of this GUI's one-shot
`setStyleSheet(...)` calls (nothing here re-themes live if the OS theme changes while
the window is open).
"""

from __future__ import annotations

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QWidget

# `palette(placeholder-text)` is resolved by the style engine at paint time (unlike a
# baked hex string), so it also tracks a runtime palette change automatically.
MUTED_STYLE = "color: palette(placeholder-text);"

_WARN_DARK = "#e8a33d"
_WARN_LIGHT = "#8a5a00"


def is_dark_palette(widget: QWidget) -> bool:
    """True if `widget`'s current `Window` colour is dark (HSL lightness < 128/255)."""
    return widget.palette().color(QPalette.ColorRole.Window).lightness() < 128


def warning_style(widget: QWidget) -> str:
    """A `color:` stylesheet string for a warning callout, legible on `widget`'s
    current theme (unlike a single hard-coded hex, which reads fine on one theme and
    poorly on the other -- e.g. the amber log tint used elsewhere in this GUI has a
    healthy ~6:1 contrast on dark but only ~2:1 on light)."""
    return f"color: {_WARN_DARK if is_dark_palette(widget) else _WARN_LIGHT};"
