import argparse
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtWidgets import QApplication

import mainpro


def render_studio(output, completed_rests=4, outfit=None, width=1120, height=760,
                  pet_kind="seagull", show_more=False):
    if pet_kind not in mainpro.DesktopPet.PET_STYLES:
        raise ValueError(f"Unknown pet skin: {pet_kind}")
    progress = mainpro.PetProgress(completed_rests, outfit)
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
    application.setFont(QFont("Microsoft YaHei UI", 10))

    def initialize_tray(window):
        window.tray = Mock()

    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        config_path = str(Path(directory) / "preview-settings.json")
        settings = dict(mainpro.CareEyesApp._DEFAULTS)
        settings.update({
            "pet_enabled": True, "pet_kind": pet_kind, "sound_enabled": False,
            "pet_completed_rests": progress.completed_rests,
            "pet_outfit": list(progress.outfit),
        })
        Path(config_path).write_text(json.dumps(settings), encoding="utf-8")
        for patcher in (
            patch("mainpro.CONFIG_FILE", config_path),
            patch("mainpro.DisplayManager.apply", return_value=True),
            patch("mainpro.DisplayManager.reset", return_value=True),
            patch("mainpro.CareEyesApp.init_tray", initialize_tray),
            patch("mainpro.CareEyesApp.init_hotkeys"),
            patch("mainpro.CareEyesApp._read_autostart", return_value=False),
            patch("mainpro.CareEyesApp._set_autostart"),
            patch("mainpro._is_admin", return_value=True),
        ):
            stack.enter_context(patcher)
        activity = stack.enter_context(patch("mainpro.WindowsActivityMonitor"))
        activity.return_value.idle_seconds.return_value = 0
        window = mainpro.CareEyesApp()
        try:
            window.resize(width, height)
            window._nav(3)
            window.pet_more_button.setChecked(show_more)
            window._work_clock.restart(18 * 60 + 42)
            window.show()
            application.processEvents()
            window._refresh_countdown()
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(output)):
                raise OSError(f"Cannot save preview: {output}")
            return output.resolve()
        finally:
            window._cleanup()
            window.pet.deleteLater()
            window.deleteLater()
            application.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def main():
    parser = argparse.ArgumentParser(description="Render the actual pet studio with isolated demo data.")
    parser.add_argument("--output", type=Path, default=Path("docs/images/ui-pet-studio.png"))
    parser.add_argument("--rests", type=int, default=4)
    parser.add_argument("--outfit", nargs="*", choices=tuple(mainpro.DesktopPet.DECORATIONS))
    parser.add_argument("--width", type=int, default=1120)
    parser.add_argument("--height", type=int, default=760)
    parser.add_argument("--pet-kind", choices=tuple(mainpro.DesktopPet.PET_STYLES), default="seagull")
    parser.add_argument("--more-skins", action="store_true")
    arguments = parser.parse_args()
    print(render_studio(arguments.output, arguments.rests, arguments.outfit,
                        arguments.width, arguments.height, arguments.pet_kind, arguments.more_skins))


if __name__ == "__main__":
    main()
