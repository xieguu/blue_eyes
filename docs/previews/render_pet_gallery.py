import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPen
from PyQt5.QtWidgets import QApplication

from mainpro import DesktopPet, PetPreview


def draw_text(painter, rectangle, content, size, color, bold=False):
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(size)
    font.setBold(bold)
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(rectangle, Qt.AlignCenter, content)


def render_gallery(output, states=False, kinds=None, outfit=None):
    kinds = list(DesktopPet.PET_STYLES) if kinds is None else list(kinds)
    if not kinds or any(kind not in DesktopPet.PET_STYLES for kind in kinds):
        raise ValueError("Select at least one registered pet skin")
    application = QApplication.instance() or QApplication([])
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
    state_labels = {"idle": "陪伴", "tired": "疲劳", "resting": "休息", "off": "关闭"}
    items = [(kind, state) for state in (state_labels if states else ("idle",)) for kind in kinds]
    columns = len(kinds) if states else min(3, len(kinds))
    rows = (len(items) + columns - 1) // columns
    card_width, card_height = (156, 204) if states else (360, 290)
    margin, gap, header = 24, 16, 104
    width = margin * 2 + columns * card_width + (columns - 1) * gap
    height = header + rows * card_height + (rows - 1) * gap + margin
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#0d1723"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    draw_text(painter, QRectF(24, 20, width - 48, 36), "给休息，找个小搭子。", 27, "#e8f1f7", True)
    draw_text(painter, QRectF(24, 60, width - 48, 24),
              f"CareEyes Pro  /  {len(kinds)} 款桌宠 · 原生 QPainter 实时绘制", 13, "#8fa5b8")
    for index, (kind, state) in enumerate(items):
        left = margin + (index % columns) * (card_width + gap)
        top = header + (index // columns) * (card_height + gap)
        painter.setPen(QPen(QColor("#2a3d4d"), 1))
        painter.setBrush(QColor("#172838"))
        painter.drawRoundedRect(QRectF(left, top, card_width, card_height), 16, 16)
        preview = PetPreview(kind, animated=False, halo=False)
        if outfit is not None:
            preview.set_outfit(outfit)
        preview._state = state
        preview._phase = .35
        scale = 1.0 if states else 1.32
        painter.save()
        painter.translate(left + (card_width - DesktopPet.W * scale) / 2, top + 4)
        painter.scale(scale, scale)
        preview._paint_scene(painter, show_bar=False, show_bubble=False)
        painter.restore()
        label = DesktopPet.PET_STYLES[kind]["label"]
        if states:
            label += " · " + state_labels[state]
        draw_text(painter, QRectF(left + 4, top + card_height - (36 if states else 63),
                                 card_width - 8, 27), label, 12 if states else 18, "#e5eef5", True)
        if not states:
            draw_text(painter, QRectF(left + 8, top + card_height - 34, card_width - 16, 22),
                      DesktopPet.PET_STYLES[kind]["tagline"], 12, "#93a9ba")
        preview.deleteLater()
    painter.end()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(output)):
        raise OSError(f"Cannot save gallery: {output}")
    application.processEvents()
    print(output.resolve())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render the actual desktop pet artwork without changing display settings.")
    parser.add_argument("--output", type=Path, default=Path("tmp/pet-gallery.png"))
    parser.add_argument("--states", action="store_true")
    parser.add_argument("--kinds", nargs="+", choices=tuple(DesktopPet.PET_STYLES))
    parser.add_argument("--outfit", nargs="*", choices=tuple(DesktopPet.DECORATIONS))
    arguments = parser.parse_args()
    render_gallery(arguments.output, arguments.states, arguments.kinds, arguments.outfit)
