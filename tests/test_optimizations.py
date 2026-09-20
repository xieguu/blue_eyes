import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt5.QtWidgets import QApplication

import mainpro


class AtomicWriteTests(unittest.TestCase):
    def test_atomically_replaces_existing_unicode_path(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = "\u62a4\u773c\u8bbe\u7f6e.json"
            path = os.path.join(directory, filename)
            with open(path, "wb") as handle:
                handle.write(b"previous")
            payload = '{"value": "\u5df2\u4fdd\u5b58"}'
            mainpro._write_atomic(path, payload.encode("utf-8"))
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), payload)
            self.assertEqual(os.listdir(directory), [filename])

    def test_open_failure_is_reported_without_direct_write(self):
        save_file = Mock()
        save_file.open.return_value = False
        save_file.errorString.return_value = "permission denied"
        with patch("mainpro.QSaveFile", return_value=save_file):
            with self.assertRaisesRegex(OSError, "permission denied"):
                mainpro._write_atomic("settings.json", b"new settings")
        save_file.setDirectWriteFallback.assert_called_once_with(False)
        save_file.write.assert_not_called()
        save_file.commit.assert_not_called()

    def test_partial_write_is_cancelled_without_committing(self):
        save_file = Mock()
        save_file.write.return_value = 1
        save_file.errorString.return_value = "disk full"
        with patch("mainpro.QSaveFile", return_value=save_file):
            with self.assertRaisesRegex(OSError, "disk full"):
                mainpro._write_atomic("settings.json", b"new settings")
        save_file.cancelWriting.assert_called_once_with()
        save_file.commit.assert_not_called()

    def test_commit_failure_is_reported(self):
        save_file = Mock()
        save_file.write.return_value = 3
        save_file.commit.return_value = False
        save_file.errorString.return_value = "replace failed"
        with patch("mainpro.QSaveFile", return_value=save_file):
            with self.assertRaisesRegex(OSError, "replace failed"):
                mainpro._write_atomic("settings.json", b"new")


class SmoothTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.transition = mainpro.SmoothTransition()
        self.addCleanup(self.transition.stop)
        patcher = patch("mainpro.DisplayManager.apply", return_value=True)
        self.apply = patcher.start()
        self.addCleanup(patcher.stop)

    def test_timeline_uses_elapsed_time_and_cubic_easing(self):
        self.transition.start(5000, 1.0, 3000, 0.6, duration_ms=1000)
        self.transition._timeline.setCurrentTime(500)
        temperature, brightness = self.apply.call_args.args
        self.assertEqual(temperature, 3250)
        self.assertAlmostEqual(brightness, 0.65)
        self.assertEqual(self.transition._timeline.updateInterval(), 50)

    def test_delayed_event_finishes_without_replaying_missed_frames(self):
        finished = Mock()
        self.transition.finished.connect(finished)
        self.transition.start(5000, 1.0, 2500, 0.55, duration_ms=100, step_ms=20)
        self.application.processEvents()
        time.sleep(0.2)
        self.application.processEvents()
        self.application.processEvents()
        self.assertFalse(self.transition.is_active())
        self.apply.assert_called_with(2500.0, 0.55)
        finished.assert_called_once_with()
        self.assertLessEqual(self.apply.call_count, 2)

    def test_retarget_starts_at_last_displayed_value(self):
        self.transition.start(5000, 1.0, 3000, 0.6, duration_ms=1000)
        self.transition._timeline.setCurrentTime(500)
        self.transition.start(3000, 0.6, 6500, 1.0, duration_ms=1000)
        self.transition._timeline.setCurrentTime(500)
        temperature, brightness = self.apply.call_args.args
        self.assertEqual(temperature, 3250 + (6500 - 3250) * 0.875)
        self.assertAlmostEqual(brightness, 0.65 + (1.0 - 0.65) * 0.875)

    def test_stopping_rejects_queued_frames(self):
        self.transition.start(5000, 1.0, 3000, 0.6)
        self.transition.stop()
        self.apply.reset_mock()
        self.transition._step(0.5)
        self.assertFalse(self.transition.is_active())
        self.apply.assert_not_called()

    def test_failed_frame_is_not_used_as_retarget_origin(self):
        self.apply.return_value = False
        self.transition.start(5000, 1.0, 3000, 0.6, duration_ms=1000)
        self.transition._timeline.setCurrentTime(500)
        self.apply.return_value = True
        self.transition.start(3000, 0.6, 6500, 1.0, duration_ms=1000)
        self.transition._timeline.setCurrentTime(500)
        self.apply.assert_called_with(5000 + (6500 - 5000) * 0.875, 1.0)


if __name__ == "__main__":
    unittest.main()
