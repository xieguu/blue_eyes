import ctypes
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import mainpro
from careeyes_runtime import WorkClock
from test_runtime import FakeClock


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.source = FakeClock()
        self.app = mainpro.CareEyesApp.__new__(mainpro.CareEyesApp)
        self.app._work_clock = WorkClock(300, clock=self.source)
        self.app._activity = Mock()
        self.app._activity.idle_seconds.return_value = 0
        self.app._quitting = False
        self.app._session_locked = False
        self.app._suspended = False
        self.app._rest_deferred = False
        self.app._pause_reason = ""
        self.app._activation_message = 0xC001
        self.app._accent = "#0ea5e9"
        self.app.is_enabled = True
        self.app.overlay = None
        self.app.pet = None
        self.app._pet_progress = mainpro.PetProgress()
        self.app._stat_date = mainpro.date.today().isoformat()
        self.app._today_seconds = 0.0
        self.app.today_minutes = 0
        self.app.break_count = 0
        self.app.week_data = {}
        self.app._stat_save_ticks = 0
        self.app._warned_1min = False
        self.app._next_rest_secs = 300
        self.app.rest_interval_min = 5
        self.app.rest_duration_sec = 20
        self.app.temp = 5000
        self.app.bright = 1.0
        self.app.force_rest = False
        self.app.auto_mode = False
        self.app.super_dim = False
        self.app.super_dim_alpha = 80
        self.app.sound_enabled = False
        self.app.pet_enabled = False
        self.app.pet_pos = None
        self.app._hk_listener = None
        for name in (
            "next_rest_label", "today_stat", "fullscreen_warn", "tray",
            "stat_today", "stat_session", "stat_breaks", "day_ring", "bar_chart",
            "toggle", "toggle_label", "guard_timer", "auto_status_lbl",
            "_transition", "_dim_mgr", "_metrics",
            "stat_timer", "countdown_timer", "auto_timer", "metrics_timer", "_save_timer",
        ):
            setattr(self.app, name, Mock())
        self.app._schedule_save = Mock()
        self.app._open_main = Mock()
        self.app._is_fullscreen = Mock(return_value=False)
        self.app._read_autostart = Mock(return_value=False)
        self.app._sync_work_clock()

    def test_delayed_countdown_and_statistics_share_one_clock(self):
        self.source.advance(125)
        self.app._refresh_countdown()
        self.assertEqual(self.app._next_rest_secs, 175)
        self.assertEqual(self.app.today_minutes, 2)
        self.assertEqual(self.app._today_seconds, 125)
        self.app._update_stat()
        self.app._update_stat()
        self.assertEqual(self.app.today_minutes, 2)
        self.assertEqual(self.app._work_clock.session_seconds, 125)

    def test_snooze_suppresses_reminders_without_losing_usage(self):
        self.app.show_rest_overlay = Mock()
        self.assertTrue(self.app._snooze_reminders(15))
        self.source.advance(250)
        self.app._refresh_countdown()
        self.app.tray.showMessage.assert_not_called()
        self.source.advance(100)
        self.app._refresh_countdown()
        self.app._on_rest_trigger()
        self.app.show_rest_overlay.assert_not_called()
        self.assertTrue(self.app._work_clock.active)
        self.assertEqual(self.app._today_seconds, 350)
        self.assertEqual(self.app._next_rest_secs, 0)
        self.assertIn("免打扰", self.app.next_rest_label.setText.call_args.args[0])

    def test_snooze_expiry_delivers_overdue_reminder(self):
        self.app.show_rest_overlay = Mock()
        self.app._snooze_reminders(15)
        self.source.advance(900)
        self.app._refresh_countdown()
        self.app.show_rest_overlay.assert_called_once_with()
        self.assertEqual(self.app._today_seconds, 900)
        self.assertEqual(self.app._work_clock.snooze_remaining_seconds, 0)

    def test_manual_resume_keeps_remaining_work_time(self):
        self.app.show_rest_overlay = Mock()
        self.app._snooze_reminders(15)
        self.source.advance(40)
        self.app._resume_reminders()
        self.assertEqual(self.app._next_rest_secs, 260)
        self.assertEqual(self.app._today_seconds, 40)
        self.app.show_rest_overlay.assert_not_called()

    def test_manual_resume_delivers_already_due_reminder(self):
        self.app.show_rest_overlay = Mock()
        self.app._snooze_reminders(15)
        self.source.advance(350)
        self.app._resume_reminders()
        self.app.show_rest_overlay.assert_called_once_with()
        self.assertEqual(self.app._today_seconds, 350)

    def test_idle_pauses_usage_but_not_snooze_expiry(self):
        self.app.show_rest_overlay = Mock()
        self.app._snooze_reminders(15)
        self.source.advance(310)
        self.app._activity.idle_seconds.return_value = 310
        self.app._refresh_countdown()
        self.source.advance(590)
        self.app._activity.idle_seconds.return_value = 900
        self.app._refresh_countdown()
        self.app.show_rest_overlay.assert_not_called()
        self.assertEqual(self.app._today_seconds, 300)
        self.assertEqual(self.app._work_clock.snooze_remaining_seconds, 0)
        self.app._activity.idle_seconds.return_value = 0
        self.app._refresh_countdown()
        self.app.show_rest_overlay.assert_called_once_with()
        self.assertEqual(self.app._today_seconds, 300)

    def test_snooze_cannot_interrupt_an_existing_rest(self):
        self.app.overlay = Mock()
        self.assertFalse(self.app._snooze_reminders(15))
        self.assertEqual(self.app._work_clock.snooze_remaining_seconds, 0)

    def test_snooze_is_rejected_when_disabled_or_quitting(self):
        self.app.is_enabled = False
        self.assertFalse(self.app._snooze_reminders(15))
        self.app.is_enabled = True
        self.app._quitting = True
        self.assertFalse(self.app._snooze_reminders(15))

    def test_idle_crossing_pauses_and_input_resumes_remaining_time(self):
        self.source.advance(15)
        self.app._activity.idle_seconds.return_value = 310
        self.app._refresh_countdown()
        self.assertEqual(self.app._today_seconds, 5)
        self.assertEqual(self.app._next_rest_secs, 295)
        self.assertFalse(self.app._work_clock.active)
        self.assertIn("空闲暂停", self.app.next_rest_label.setText.call_args.args[0])
        self.source.advance(600)
        self.app._activity.idle_seconds.return_value = 910
        self.app._refresh_countdown()
        self.app._activity.idle_seconds.return_value = 0
        self.app._refresh_countdown()
        self.source.advance(30)
        self.app._refresh_countdown()
        self.assertEqual(self.app._today_seconds, 35)
        self.assertEqual(self.app._next_rest_secs, 265)

    def test_lock_and_unlock_exclude_locked_duration(self):
        self.source.advance(12)
        self.app._set_session_pause(locked=True)
        self.source.advance(3600)
        self.app._refresh_countdown()
        self.assertEqual(self.app._today_seconds, 12)
        self.assertEqual(self.app._next_rest_secs, 288)
        self.app._set_session_pause(locked=False)
        self.source.advance(8)
        self.app._refresh_countdown()
        self.assertEqual(self.app._today_seconds, 20)
        self.assertEqual(self.app._next_rest_secs, 280)

    def test_resume_discards_gap_even_if_suspend_notification_was_missed(self):
        self.source.advance(3600)
        self.app._set_session_pause(suspended=False)
        self.assertEqual(self.app._today_seconds, 0)
        self.assertEqual(self.app._next_rest_secs, 300)

    def test_idle_expiry_does_not_open_rest_window(self):
        self.app.show_rest_overlay = Mock()
        self.source.advance(600)
        self.app._activity.idle_seconds.return_value = 600
        self.app._refresh_countdown()
        self.assertEqual(self.app._next_rest_secs, 0)
        self.app.show_rest_overlay.assert_not_called()
        self.app._activity.idle_seconds.return_value = 0
        self.app._refresh_countdown()
        self.app.show_rest_overlay.assert_called_once()

    def test_fullscreen_defer_replaces_interval_without_duplicate_trigger(self):
        self.app._is_fullscreen.return_value = True
        self.source.advance(300)
        self.app._refresh_countdown()
        self.assertTrue(self.app._rest_deferred)
        self.assertEqual(self.app._next_rest_secs, 300)
        self.assertEqual(self.app.tray.showMessage.call_count, 1)
        self.app._refresh_countdown()
        self.assertEqual(self.app.tray.showMessage.call_count, 1)
        self.source.advance(300)
        self.app._refresh_countdown()
        self.assertEqual(self.app.tray.showMessage.call_count, 2)

    def test_warning_is_not_missed_when_callback_skips_exactly_sixty(self):
        self.source.advance(250)
        self.app._refresh_countdown()
        self.assertTrue(self.app._warned_1min)
        self.app.tray.showMessage.assert_called_once()
        self.app._refresh_countdown()
        self.app.tray.showMessage.assert_called_once()

    def test_rest_window_is_unique_and_rest_time_is_not_counted(self):
        overlay = Mock()
        overlay.completed = False
        overlay.isVisible.return_value = True
        with patch("mainpro.EyeExerciseOverlay", return_value=overlay) as constructor:
            self.source.advance(12)
            self.app.show_rest_overlay()
            self.app.show_rest_overlay()
            constructor.assert_called_once()
            self.assertEqual(self.app.break_count, 1)
            self.source.advance(60)
            self.app._refresh_countdown()
            self.assertEqual(self.app._today_seconds, 12)
            self.app._on_overlay_closed()
            self.assertEqual(self.app._work_clock.session_seconds, 0)
            self.source.advance(5)
            self.app._refresh_countdown()
            self.assertEqual(self.app._next_rest_secs, 295)
            self.assertEqual(self.app._today_seconds, 17)

    def test_master_toggle_pauses_statistics_and_restores_gamma(self):
        self.app._save_settings = Mock()
        with patch.object(mainpro.DisplayManager, "apply"), patch.object(
            mainpro.DisplayManager, "reset"
        ) as restore:
            self.source.advance(42)
            self.app.toggle.isChecked.return_value = False
            self.app.toggle_master()
            restore.assert_called_once()
            self.source.advance(600)
            self.app._refresh_countdown()
            self.assertEqual(self.app._today_seconds, 42)
            self.app.toggle.isChecked.return_value = True
            self.app.toggle_master()
            self.source.advance(30)
            self.app._refresh_countdown()
            self.assertEqual(self.app._today_seconds, 72)
            self.assertEqual(self.app._next_rest_secs, 270)

    def test_midnight_splits_elapsed_time_before_archiving(self):
        today = mainpro.date.today()
        previous = today - timedelta(days=1)
        self.app._stat_date = previous.isoformat()
        self.app._today_seconds = 3598.0
        self.app.today_minutes = 59
        moment = datetime.combine(today, datetime.min.time()) + timedelta(seconds=2)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment

        self.source.advance(4)
        with patch("mainpro.datetime", FixedDateTime):
            self.app._sync_work_clock()
        self.assertEqual(self.app.week_data[previous.isoformat()], 60)
        self.assertEqual(self.app.today_minutes, 0)
        self.assertEqual(self.app._today_seconds, 2)

    def test_fractional_seconds_survive_save_and_reload(self):
        self.source.advance(61.25)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            with patch("mainpro.CONFIG_FILE", path):
                self.app._save_settings()
                with open(path, encoding="utf-8") as handle:
                    saved = json.load(handle)
                self.assertEqual(saved["today_seconds"], 61.25)
                self.app.load_settings()
        self.assertEqual(self.app._today_seconds, 61.25)
        self.assertEqual(self.app.today_minutes, 1)

    def test_activation_message_only_opens_existing_window(self):
        message = ctypes.wintypes.MSG()
        message.message = self.app._activation_message
        result = self.app.nativeEvent(b"windows_generic_MSG", ctypes.addressof(message))
        self.assertEqual(result, (True, 0))
        self.app._open_main.assert_called_once()

    def test_session_notification_routes_to_pause(self):
        message = ctypes.wintypes.MSG()
        message.message = 0x02B1
        message.wParam = 7
        self.app.nativeEvent(b"windows_generic_MSG", ctypes.addressof(message))
        self.assertTrue(self.app._session_locked)
        self.assertFalse(self.app._work_clock.active)

    def test_cleanup_stops_transition_before_restoring_and_is_idempotent(self):
        self.app._save_settings = Mock()
        events = []
        self.app._transition.stop.side_effect = lambda: events.append("stop")
        with patch.object(mainpro.DisplayManager, "reset", side_effect=lambda: events.append("restore")):
            self.app._cleanup()
            self.app._cleanup()
        self.assertEqual(events, ["stop", "restore"])
        self.app._activity.close.assert_called_once()


class RestOverlayClockTests(unittest.TestCase):
    def test_delayed_overlay_tick_uses_deadline_for_duration_and_force_lock(self):
        overlay = mainpro.EyeExerciseOverlay.__new__(mainpro.EyeExerciseOverlay)
        overlay._deadline = 120.0
        overlay._unlock_deadline = 110.0
        with patch("mainpro.time.monotonic", return_value=114.25):
            overlay._sync_remaining()
        self.assertEqual(overlay.remaining, 6)
        self.assertEqual(overlay._lock_secs, 0)
        with patch("mainpro.time.monotonic", return_value=130):
            overlay._sync_remaining()
        self.assertEqual(overlay.remaining, 0)


if __name__ == "__main__":
    unittest.main()
