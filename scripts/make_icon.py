"""Draw the app icon and write it as PNG (256 px) and multi-size ICO.

    uv run --with pillow python scripts/make_icon.py

Outputs `src/collections2mo2/gui/assets/icon.png` and `icon.ico`. The drawing is three
stacked, offset cards -- collections layered on one instance -- on a dark rounded tile.
Qt does the drawing so the result matches what the GUI renders; Pillow only packs the
ICO sizes. Re-run after changing anything here and commit the outputs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

ASSETS = Path(__file__).resolve().parents[1] / "src" / "collections2mo2" / "gui" / "assets"
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 256.0

    # Tile
    tile = QRectF(8 * s, 8 * s, 240 * s, 240 * s)
    bg = QLinearGradient(tile.topLeft(), tile.bottomRight())
    bg.setColorAt(0.0, QColor("#2b3a4f"))
    bg.setColorAt(1.0, QColor("#111827"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(tile, 52 * s, 52 * s)

    # Three cards, back to front, each offset up-left of the one behind it.
    cards = [
        (QRectF(76 * s, 108 * s, 132 * s, 88 * s), "#0f766e", "#115e59"),
        (QRectF(62 * s, 84 * s, 132 * s, 88 * s), "#14b8a6", "#0d9488"),
        (QRectF(48 * s, 60 * s, 132 * s, 88 * s), "#ccfbf1", "#99f6e4"),
    ]
    for i, (rect, top, bottom) in enumerate(cards):
        shadow = QPainterPath()
        shadow.addRoundedRect(rect.translated(0, 6 * s), 16 * s, 16 * s)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawPath(shadow)
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, QColor(top))
        grad.setColorAt(1.0, QColor(bottom))
        p.setBrush(grad)
        p.drawRoundedRect(rect, 16 * s, 16 * s)

    # Two "list rows" on the front card: a mod list, at a glance.
    front = cards[-1][0]
    pen = QPen(QColor("#0f766e"))
    pen.setWidthF(9 * s)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    x0 = front.left() + 20 * s
    for k, width in enumerate((72, 48)):
        y = front.top() + (30 + k * 26) * s
        p.drawLine(QPointF(x0, y), QPointF(x0 + width * s, y))
    p.end()
    return img


def main() -> int:
    QGuiApplication(sys.argv)
    ASSETS.mkdir(parents=True, exist_ok=True)
    png = ASSETS / "icon.png"
    if not draw(256).save(str(png)):
        print(f"could not write {png}", file=sys.stderr)
        return 1
    print(f"wrote {png}")
    try:
        from PIL import Image
    except ImportError:
        print("Pillow not installed: run with `uv run --with pillow`; icon.ico not written")
        return 1
    frames = []
    for n in ICO_SIZES:
        tmp = ASSETS / f"_icon_{n}.png"
        draw(n).save(str(tmp))
        frames.append(Image.open(tmp).convert("RGBA"))
    ico = ASSETS / "icon.ico"
    frames[-1].save(ico, format="ICO", sizes=[(n, n) for n in ICO_SIZES], append_images=frames[:-1])
    for n in ICO_SIZES:
        (ASSETS / f"_icon_{n}.png").unlink(missing_ok=True)
    print(f"wrote {ico} ({', '.join(str(n) for n in ICO_SIZES)} px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
