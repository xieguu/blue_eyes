import csv
import json
import math
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt5.QtCore import QCoreApplication, QEvent, QRect, Qt
from PyQt5.QtGui import QColor, QImage, QPainter
from PyQt5.QtWidgets import QApplication

import mainpro


class OffscreenUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.real_init_tray = mainpro.CareEyesApp.init_tray
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config_path = os.path.join(self.directory.name, "settings.json")
        config = dict(mainpro.CareEyesApp._DEFAULTS)
        config.update({
            "pet_enabled": False,
            "sound_enabled": False,
            "pet_kind": "mint_bunny",
        })
        self.config_path = config_path
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        def fake_tray(window):
            window.tray = Mock()

        patches = [
            patch("mainpro.CONFIG_FILE", config_path),
            patch("mainpro.DisplayManager.apply", return_value=True),
            patch("mainpro.DisplayManager.reset", return_value=True),
            patch("mainpro.WindowsActivityMonitor"),
            patch("mainpro.CareEyesApp.init_tray", fake_tray),
            patch("mainpro.CareEyesApp.init_hotkeys"),
            patch("mainpro.CareEyesApp._read_autostart", return_value=False),
            patch("mainpro.CareEyesApp._set_autostart"),
            patch("mainpro.CareEyesApp._refresh_system_metrics"),
            patch("mainpro._is_admin", return_value=True),
        ]
        for patcher in patches:
            started = patcher.start()
            self.addCleanup(patcher.stop)
            if patcher.attribute == "WindowsActivityMonitor":
                started.return_value.idle_seconds.return_value = 0
        self.window = mainpro.CareEyesApp()
        self.addCleanup(self.close_window)
        self.application.processEvents()

    def close_window(self):
        self.window._cleanup()
        if self.window.pet is not None:
            self.window.pet._anim_timer.stop()
            self.window.pet._chat_timer.stop()
            self.window.pet._msg_timer.stop()
            self.window.pet.deleteLater()
        self.window.deleteLater()
        self.application.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_reminder_controls_and_tray_actions_share_snooze_state(self):
        with patch("mainpro.QSystemTrayIcon"):
            self.real_init_tray(self.window)
        self.window.snooze_button.menu().actions()[0].trigger()
        self.assertGreater(self.window._work_clock.snooze_remaining_seconds, 895)
        self.assertEqual(self.window.next_rest_label.text(), "免打扰")
        self.assertTrue(self.window.resume_reminders_button.isEnabled())
        self.assertTrue(self.window.resume_reminders_action.isEnabled())
        self.window.snooze_tray_menu.actions()[1].trigger()
        self.assertGreater(self.window._work_clock.snooze_remaining_seconds, 1795)
        self.window.resume_reminders_action.trigger()
        self.assertEqual(self.window._work_clock.snooze_remaining_seconds, 0)
        self.assertFalse(self.window.resume_reminders_button.isEnabled())
        self.assertFalse(self.window.resume_reminders_action.isEnabled())

    def test_manual_rest_cancels_snooze_and_disables_controls(self):
        self.window._snooze_reminders(15)
        self.window.show_rest_overlay()
        self.assertEqual(self.window._work_clock.snooze_remaining_seconds, 0)
        self.assertFalse(self.window.snooze_button.isEnabled())
        self.assertFalse(self.window.resume_reminders_button.isEnabled())
        self.window.overlay.close()
        self.assertTrue(self.window.snooze_button.isEnabled())

    def test_reset_clears_snooze_without_persisting_its_deadline(self):
        self.window._snooze_reminders(15)
        self.window._reset_settings()
        self.assertEqual(self.window._work_clock.snooze_remaining_seconds, 0)
        self.assertFalse(self.window.resume_reminders_button.isEnabled())
        with open(self.config_path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
        self.assertFalse(any("snooze" in field for field in settings))

    def test_csv_export_includes_current_day_and_sanitizes_history(self):
        today = mainpro.date.today()
        previous = (today - mainpro.timedelta(days=1)).isoformat()
        oldest = (today - mainpro.timedelta(days=31)).isoformat()
        self.window.week_data = {
            previous: 37,
            oldest: 99999,
            (today - mainpro.timedelta(days=32)).isoformat(): 10,
            (today + mainpro.timedelta(days=1)).isoformat(): 10,
            (today - mainpro.timedelta(days=2)).isoformat(): True,
            "=INVALID()": 42,
            "not-a-date": 3,
        }
        self.window._today_seconds = 125.0
        self.window.today_minutes = 2
        path = os.path.join(self.directory.name, "usage.csv")
        with patch("mainpro.QFileDialog.getSaveFileName", return_value=(path, "")):
            self.assertTrue(self.window._export_statistics())
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(3), b"\xef\xbb\xbf")
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows, [
            ["date", "usage_minutes"],
            [oldest, "1440"], [previous, "37"], [today.isoformat(), "2"],
        ])
        self.assertIn("已导出 3 天记录", self.window.export_status_label.text())

    def test_cancelled_csv_export_does_not_write(self):
        with patch("mainpro.QFileDialog.getSaveFileName", return_value=("", "")):
            with patch("mainpro._write_atomic") as write:
                self.assertFalse(self.window._export_statistics())
        write.assert_not_called()

    def test_failed_csv_export_preserves_existing_file_and_shows_error(self):
        path = os.path.join(self.directory.name, "usage.csv")
        with open(path, "wb") as handle:
            handle.write(b"original export")
        with patch("mainpro.QFileDialog.getSaveFileName", return_value=(path, "")):
            with patch("mainpro._write_atomic", side_effect=OSError("disk full")):
                self.assertFalse(self.window._export_statistics())
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"original export")
        self.assertIn("disk full", self.window.export_status_label.text())

    def test_failed_settings_save_is_visible_and_success_clears_error(self):
        with open(self.config_path, "rb") as handle:
            original = handle.read()
        with patch("mainpro._write_atomic", side_effect=OSError("disk full")):
            self.assertFalse(self.window._save_settings())
        with open(self.config_path, "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(self.window._settings_error, "disk full")
        self.assertIn("保存失败", self.window.config_status_label.text())
        self.assertTrue(self.window._save_settings())
        self.assertEqual(self.window._settings_error, "")
        self.assertEqual(self.window.config_status_label.text(), "设置已保存")

    def test_nonfinite_settings_never_replace_valid_configuration(self):
        with open(self.config_path, "rb") as handle:
            original = handle.read()
        self.window.bright = float("nan")
        with patch("mainpro._write_atomic") as write:
            self.assertFalse(self.window._save_settings())
        write.assert_not_called()
        with open(self.config_path, "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.window.bright = 1.0

    def test_gamma_guard_does_not_overwrite_active_transition(self):
        self.window.apply_preset("睡眠")
        with patch("mainpro.DisplayManager.apply") as apply:
            self.window._guard_apply()
            apply.assert_not_called()
            self.window._transition.stop()
            self.window._guard_apply()
            apply.assert_called_once_with(2500, 0.55)

    def test_hidden_window_keeps_required_timers_only(self):
        self.assertFalse(self.window.pet._anim_timer.isActive())
        self.assertFalse(self.window.pet._chat_timer.isActive())
        self.assertFalse(self.window.pet_preview._preview_timer.isActive())
        self.assertFalse(self.window.metrics_timer.isActive())
        self.window._nav(2)
        self.assertFalse(self.window.metrics_timer.isActive())
        self.window._nav(3)
        self.assertFalse(self.window.pet_preview._preview_timer.isActive())
        self.window._refresh_countdown()
        self.assertTrue(self.window.guard_timer.isActive())
        self.assertTrue(self.window.countdown_timer.isActive())
        self.assertTrue(self.window._work_clock.active)

    def test_pet_timers_follow_visibility_and_clear_messages(self):
        pet = self.window.pet
        self.window._on_pet_toggle(True)
        self.assertTrue(pet._anim_timer.isActive())
        self.assertTrue(pet._chat_timer.isActive())
        pet.say("take a break")
        self.assertTrue(pet._msg_timer.isActive())
        self.window._on_pet_toggle(False)
        self.assertFalse(pet._anim_timer.isActive())
        self.assertFalse(pet._chat_timer.isActive())
        self.assertFalse(pet._msg_timer.isActive())
        self.assertEqual(pet._msg, "")
        self.window._on_pet_toggle(True)
        self.assertTrue(pet._anim_timer.isActive())
        self.assertTrue(pet._chat_timer.isActive())

    def test_hidden_pet_messages_wait_until_shown(self):
        pet = self.window.pet
        pet.say("take a break", 500)
        self.assertFalse(pet._msg_timer.isActive())
        self.window._on_pet_toggle(True)
        self.assertTrue(pet._msg_timer.isActive())
        self.assertEqual(pet._msg_timer.interval(), 500)

    def test_preview_timer_follows_navigation_and_window_visibility(self):
        timer = self.window.pet_preview._preview_timer
        self.window.show()
        self.assertFalse(timer.isActive())
        self.window._nav(3)
        self.assertTrue(timer.isActive())
        for preview in self.window.findChildren(mainpro.PetPreview):
            if preview is not self.window.pet_preview:
                self.assertFalse(preview._preview_timer.isActive())
        self.window._nav(0)
        self.assertFalse(timer.isActive())
        self.window._nav(3)
        self.assertTrue(timer.isActive())
        self.window.hide()
        self.assertFalse(timer.isActive())
        self.window.show()
        self.assertTrue(timer.isActive())

    def test_minimized_window_suspends_visual_timers(self):
        for page_index, timer in (
            (2, self.window.metrics_timer),
            (3, self.window.pet_preview._preview_timer),
        ):
            with self.subTest(page=page_index):
                self.window.showNormal()
                self.window._nav(page_index)
                self.application.processEvents()
                self.assertTrue(timer.isActive())
                self.window.showMinimized()
                self.application.processEvents()
                self.assertFalse(timer.isActive())
                self.assertTrue(self.window.countdown_timer.isActive())
                self.window.showNormal()
                self.application.processEvents()
                self.assertTrue(timer.isActive())

    def test_metrics_sampling_is_lazy_and_resets_on_resume(self):
        refresh = self.window._refresh_system_metrics
        refresh.assert_not_called()
        with patch.object(self.window._metrics, "reset_cpu_baseline") as reset:
            self.window.show()
            refresh.assert_not_called()
            self.window._nav(2)
            refresh.assert_called_once()
            self.assertTrue(self.window.metrics_timer.isActive())
            self.window._sync_metrics_timer()
            refresh.assert_called_once()
            self.window.hide()
            self.assertFalse(self.window.metrics_timer.isActive())
            self.window.show()
            self.assertEqual(refresh.call_count, 2)
            self.window._nav(0)
            self.assertFalse(self.window.metrics_timer.isActive())
            self.window._nav(2)
            self.assertEqual(refresh.call_count, 3)
            self.assertEqual(reset.call_count, 3)

    def test_countdown_refreshes_pet_page_only_when_visible_and_once(self):
        with patch.object(self.window, "_sync_pet_page") as refresh:
            self.window._refresh_countdown()
            refresh.assert_not_called()
            self.window.show()
            self.window._nav(3)
            refresh.reset_mock()
            self.window._refresh_countdown()
            refresh.assert_called_once()
            self.window.hide()
            refresh.reset_mock()
            self.window._refresh_countdown()
            refresh.assert_not_called()

    def test_cleanup_stops_previews_and_pet_timers(self):
        self.window.show()
        self.window._nav(3)
        self.window._on_pet_toggle(True)
        self.window.pet.say("take a break")
        self.window._cleanup()
        for timer in (
            self.window.pet_preview._preview_timer,
            self.window.pet._anim_timer,
            self.window.pet._chat_timer,
            self.window.pet._msg_timer,
            self.window.guard_timer,
            self.window.countdown_timer,
            self.window.metrics_timer,
        ):
            self.assertFalse(timer.isActive())

    def test_unchanged_preview_state_does_not_repaint(self):
        preview = self.window.pet_preview
        with patch.object(preview, "update") as update:
            preview.set_state("idle")
            update.assert_not_called()
            preview.set_state("tired")
            update.assert_called_once()
            preview.set_state("invalid")
            update.assert_called_once()

    def test_starter_outfit_and_locked_rewards_are_visible(self):
        self.assertEqual(self.window.pet_level_label.text(), "Lv. 1")
        self.assertEqual(self.window.pet_reward_count.text(), "0 / 6")
        self.assertEqual(self.window.pet_experience_progress.maximum(), 20)
        for decoration in ("scarf", "sprout"):
            self.assertTrue(self.window.pet_outfit_buttons[decoration].isEnabled())
            self.assertTrue(self.window.pet_outfit_buttons[decoration].isChecked())
        for decoration in ("star_pin", "night_cap"):
            self.assertFalse(self.window.pet_outfit_buttons[decoration].isEnabled())
            self.window._toggle_pet_decoration(decoration)
        self.assertEqual(self.window.pet._outfit, ("scarf", "sprout"))
        self.assertEqual(self.window.pet_preview._outfit, self.window.pet._outfit)

    def test_outfit_toggles_update_all_previews_and_save_empty_selection(self):
        self.window.pet_outfit_buttons["scarf"].click()
        self.assertEqual(self.window.pet._outfit, ("sprout",))
        self.assertEqual(self.window.pet_preview._outfit, ("sprout",))
        for button in self.window.pet_skin_buttons.values():
            self.assertEqual(button.preview._outfit, ("sprout",))
        self.window.pet_outfit_buttons["sprout"].click()
        self.assertEqual(self.window._pet_progress.outfit, ())
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["pet_outfit"], [])
        restored = mainpro.CareEyesApp.__new__(mainpro.CareEyesApp)
        restored.load_settings()
        self.assertEqual(restored._pet_progress.outfit, ())

    def test_level_three_matches_reference_and_star_unlock_updates_cards(self):
        self.window._pet_progress = mainpro.PetProgress(4)
        self.window._sync_pet_progress()
        self.assertEqual(self.window.pet_level_label.text(), "Lv. 3")
        self.assertEqual(self.window.pet_reward_count.text(), "4 / 6")
        self.assertIn("2", self.window.pet_reward_remaining.text())
        self.window._pet_progress = mainpro.PetProgress(6)
        self.window._sync_pet_progress()
        self.window.pet_outfit_buttons["star_pin"].click()
        self.assertEqual(self.window.pet._outfit, ("scarf", "sprout", "star_pin"))
        self.assertEqual(self.window.pet_reward_count.text(), "6 / 12")
        self.assertFalse(self.window.pet_outfit_buttons["night_cap"].isEnabled())

    def test_headwear_replaces_sprout_and_remains_equipped_when_skin_changes(self):
        self.window._pet_progress = mainpro.PetProgress(12, ["scarf", "sprout", "star_pin"])
        self.window._sync_pet_progress()
        self.window.pet_outfit_buttons["night_cap"].click()
        outfit = ("scarf", "star_pin", "night_cap")
        self.assertEqual(self.window.pet._outfit, outfit)
        self.assertFalse(self.window.pet_outfit_buttons["sprout"].isChecked())
        self.window.pet_skin_buttons["pixel_robot"].click()
        self.assertEqual(self.window.pet_preview._outfit, outfit)
        self.assertEqual(self.window._pet_progress.level, 7)
        self.assertEqual(self.window.pet_reward_title.text(), "衣柜已集齐")
        with open(self.config_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["pet_outfit"], list(outfit))
        self.assertEqual(saved["pet_completed_rests"], 12)

    def test_completed_rest_awards_once_unlocks_and_persists(self):
        self.window._pet_progress = mainpro.PetProgress(5)
        self.window._sync_pet_progress()
        self.window.show_rest_overlay()
        overlay = self.window.overlay
        overlay._deadline = mainpro.time.monotonic() - 1
        overlay._tick()
        self.assertTrue(overlay.completed)
        self.assertIsNone(self.window.overlay)
        self.assertEqual(self.window._pet_progress.completed_rests, 6)
        self.assertEqual(self.window.pet_level_label.text(), "Lv. 4")
        self.assertTrue(self.window.pet_outfit_buttons["star_pin"].isEnabled())
        self.assertFalse(self.window.pet_outfit_buttons["star_pin"].isChecked())
        self.window._on_overlay_closed(overlay)
        self.assertEqual(self.window._pet_progress.completed_rests, 6)
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["pet_completed_rests"], 6)

    def test_skipping_rest_does_not_award_growth(self):
        self.window.show_rest_overlay()
        overlay = self.window.overlay
        overlay._close()
        self.assertFalse(overlay.completed)
        self.assertEqual(self.window._pet_progress.completed_rests, 0)
        self.assertEqual(self.window.pet_experience_progress.value(), 0)

    def test_forced_rest_can_be_skipped_after_lock_without_awarding_growth(self):
        self.window.force_rest = True
        self.window.show_rest_overlay()
        overlay = self.window.overlay
        overlay._close()
        self.assertIs(self.window.overlay, overlay)
        overlay._unlock_deadline = mainpro.time.monotonic() - 1
        overlay._close()
        self.assertIsNone(self.window.overlay)
        self.assertFalse(overlay.completed)
        self.assertEqual(self.window._pet_progress.completed_rests, 0)

    def test_old_overlay_callback_cannot_close_or_reward_new_rest(self):
        self.window.show_rest_overlay()
        previous_overlay = self.window.overlay
        previous_overlay._close()
        self.window.show_rest_overlay()
        current_overlay = self.window.overlay
        self.window._on_overlay_closed(previous_overlay)
        self.assertIs(self.window.overlay, current_overlay)
        self.assertEqual(self.window._pet_progress.completed_rests, 0)
        current_overlay._deadline = mainpro.time.monotonic() - 1
        current_overlay._tick()
        self.assertEqual(self.window._pet_progress.completed_rests, 1)

    def test_reset_cancels_pending_completion_and_clears_growth(self):
        self.window._pet_progress = mainpro.PetProgress(12, ["night_cap"])
        self.window.show_rest_overlay()
        overlay = self.window.overlay
        overlay._deadline = mainpro.time.monotonic() - 1
        self.window._reset_settings()
        self.assertFalse(overlay.completed)
        self.assertEqual(self.window._pet_progress.completed_rests, 0)
        self.assertEqual(self.window.pet._outfit, ("scarf", "sprout"))
        self.assertFalse(self.window.pet_outfit_buttons["star_pin"].isEnabled())

    def test_shutdown_cancels_pending_completion_without_awarding_growth(self):
        self.window._pet_progress = mainpro.PetProgress(5)
        self.window.show_rest_overlay()
        overlay = self.window.overlay
        overlay._deadline = mainpro.time.monotonic() - 1
        self.window._cleanup()
        self.assertFalse(overlay.completed)
        self.assertEqual(self.window._pet_progress.completed_rests, 5)
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["pet_completed_rests"], 5)

    def test_old_settings_do_not_turn_started_breaks_into_growth(self):
        with open(self.config_path, encoding="utf-8") as handle:
            settings = json.load(handle)
        settings.pop("pet_completed_rests")
        settings.pop("pet_outfit")
        settings["break_count"] = 100
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        restored = mainpro.CareEyesApp.__new__(mainpro.CareEyesApp)
        restored.load_settings()
        self.assertEqual(restored._pet_progress.completed_rests, 0)
        self.assertEqual(restored._pet_progress.outfit, ("scarf", "sprout"))

    def test_progress_settings_are_sanitized_before_rendering(self):
        for count, outfit, expected_count, expected_outfit in (
            (-4, ["scarf", "night_cap", {}], 0, ("scarf",)),
            (True, ["sprout"], 0, ("sprout",)),
            (6, ["night_cap", "star_pin", "star_pin"], 6, ("star_pin",)),
            ("12", "night_cap", 0, ("scarf", "sprout")),
        ):
            with self.subTest(count=count, outfit=outfit):
                with open(self.config_path, "w", encoding="utf-8") as handle:
                    json.dump({"pet_completed_rests": count, "pet_outfit": outfit}, handle)
                restored = mainpro.CareEyesApp.__new__(mainpro.CareEyesApp)
                restored.load_settings()
                self.assertEqual(restored._pet_progress.completed_rests, expected_count)
                self.assertEqual(restored._pet_progress.outfit, expected_outfit)

    def test_daily_rollover_preserves_lifetime_growth_and_outfit(self):
        self.window._pet_progress = mainpro.PetProgress(6, ["scarf", "star_pin"])
        self.window._stat_date = (mainpro.date.today() - mainpro.timedelta(days=1)).isoformat()
        self.window._rollover_stats_if_needed()
        self.assertEqual(self.window._pet_progress.completed_rests, 6)
        self.assertEqual(self.window._pet_progress.outfit, ("scarf", "star_pin"))

    def test_compact_studio_keeps_cards_inside_viewport_and_expands_controls(self):
        self.window.resize(760, 560)
        self.window.show()
        self.window._nav(3)
        self.application.processEvents()
        self.assertEqual(self.window.pet_page_body.width(), self.window.pet_scroll.viewport().width())
        self.assertFalse(self.window.pet_more_skins.isVisible())
        self.window.pet_more_button.click()
        self.assertTrue(self.window.pet_more_skins.isVisible())
        self.window.pet_play_toggle.click()
        self.assertTrue(self.window.pet_play_panel.isVisible())
        self.application.processEvents()
        self.assertEqual(self.window.pet_page_body.width(), self.window.pet_scroll.viewport().width())

    def test_construct_pause_toggle_and_resume(self):
        self.assertTrue(self.window._work_clock.active)
        self.window._set_session_pause(locked=True)
        self.assertIn("锁屏暂停", self.window.next_rest_label.text())
        self.assertFalse(self.window._work_clock.active)
        self.window._hk_toggle()
        self.assertFalse(self.window.is_enabled)
        self.assertEqual(self.window.next_rest_label.text(), "已暂停")
        self.window._set_session_pause(locked=False)
        self.window._hk_toggle()
        self.assertTrue(self.window.is_enabled)
        self.assertTrue(self.window._work_clock.active)
        self.window.interval_spin.setValue(6)
        self.window.apply_timer_settings()
        self.assertEqual(self.window._next_rest_secs, 360)

    def test_rest_overlay_close_and_settings_reset(self):
        self.window.show_rest_overlay()
        overlay = self.window.overlay
        self.assertIsNotNone(overlay)
        self.assertFalse(self.window._work_clock.active)
        self.window.show_rest_overlay()
        self.assertIs(self.window.overlay, overlay)
        self.application.processEvents()
        overlay._close()
        self.assertIsNone(self.window.overlay)
        self.assertTrue(self.window._work_clock.active)
        self.window._reset_settings()
        self.assertEqual(self.window.today_minutes, 0)
        self.assertEqual(self.window._next_rest_secs, 45 * 60)
        self.assertTrue(self.window.is_enabled)

    def test_pet_styles_change_and_persist(self):
        expected_new_styles = {
            "seagull": "小海鸥",
            "cream_cat": "奶油猫",
            "pixel_robot": "像素机器人",
        }
        self.assertEqual(self.window.pages.count(), 5)
        self.assertEqual(
            [button.text() for button in self.window.nav_btns],
            ["护眼", "休息", "统计", "桌宠", "设置"],
        )
        self.assertFalse(hasattr(self.window, "pet_cb"))
        self.assertFalse(hasattr(self.window, "pet_style_combo"))
        self.assertGreaterEqual(len(mainpro.DesktopPet.PET_STYLES), 9)
        self.assertEqual(
            set(self.window.pet_skin_buttons),
            set(mainpro.DesktopPet.PET_STYLES),
        )
        for pet_kind, label in expected_new_styles.items():
            self.assertEqual(
                mainpro.DesktopPet.PET_STYLES[pet_kind]["label"], label
            )

        self.assertEqual(self.window.pet_kind, "mint_bunny")
        self.assertEqual(self.window.pet._pet_kind, "mint_bunny")
        self.assertTrue(self.window.pet_skin_buttons["mint_bunny"].isChecked())
        self.assertEqual(self.window.pet_preview._pet_kind, "mint_bunny")

        self.window.pet_skin_buttons["pixel_robot"].click()
        self.assertEqual(self.window.pet._pet_kind, "pixel_robot")
        self.assertEqual(self.window.pet_preview._pet_kind, "pixel_robot")
        self.assertTrue(self.window.pet_skin_buttons["pixel_robot"].isChecked())
        self.assertEqual(self.window.pet_name_label.text(), "像素机器人")

        self.window.pet_enable_toggle.click()
        self.assertTrue(self.window.pet_enabled)
        self.assertTrue(self.window.pet.isVisible())
        self.assertEqual(self.window.pet_status_label.text(), "桌宠已开启")
        self.window._save_settings()

        with open(self.config_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["pet_kind"], "pixel_robot")
        self.assertTrue(saved["pet_enabled"])

        for pet_kind in mainpro.DesktopPet.PET_STYLES:
            pet = mainpro.DesktopPet(pet_kind=pet_kind)
            for state in ("idle", "tired", "resting", "off"):
                pet._state = state
                pet.show()
                self.application.processEvents()
                self.assertFalse(pet.grab().isNull(), f"{pet_kind}:{state}")
            pet._anim_timer.stop()
            pet._chat_timer.stop()
            pet._msg_timer.stop()
            pet.deleteLater()
        self.application.processEvents()

    def test_new_skins_switch_save_and_restore_without_resetting_progress(self):
        outfit = ("scarf", "star_pin", "night_cap")
        self.window._pet_progress = mainpro.PetProgress(12, outfit)
        self.window._sync_pet_progress()
        self.window._set_pet_interaction_mode("stretch")
        self.window._work_clock.restart(937)
        with patch("mainpro.DisplayManager.apply") as apply_display:
            for pet_kind in ("capybara", "red_panda", "penguin", "mint_bunny"):
                with self.subTest(pet_kind=pet_kind):
                    self.window.pet_skin_buttons[pet_kind].click()
                    self.assertEqual(self.window.pet_kind, pet_kind)
                    self.assertEqual(self.window.pet._pet_kind, pet_kind)
                    self.assertEqual(self.window.pet_preview._pet_kind, pet_kind)
                    self.assertEqual(self.window.pet._outfit, outfit)
                    self.assertEqual(self.window.pet_preview._outfit, outfit)
                    self.assertEqual(self.window._pet_progress.completed_rests, 12)
                    self.assertEqual(self.window._work_clock.remaining_seconds, 937)
                    self.assertFalse(self.window.pet_enabled)
                    self.assertTrue(self.window.pet_skin_buttons[pet_kind].isChecked())
                    self.assertEqual(sum(button.isChecked() for button in
                                         self.window.pet_skin_buttons.values()), 1)
                    self.assertTrue(self.window._save_timer.isActive())
                    self.window._save_timer.stop()
                    self.window._save_timer.timeout.emit()
                    with open(self.config_path, encoding="utf-8") as handle:
                        saved = json.load(handle)
                    self.assertEqual(saved["pet_kind"], pet_kind)
                    self.assertEqual(saved["pet_outfit"], list(outfit))
                    self.assertEqual(saved["pet_completed_rests"], 12)
                    restored = mainpro.CareEyesApp.__new__(mainpro.CareEyesApp)
                    restored.load_settings()
                    self.assertEqual(restored.pet_kind, pet_kind)
                    self.assertEqual(restored._pet_progress.outfit, outfit)
                    self.assertEqual(restored._pet_progress.completed_rests, 12)
                    self.assertEqual(restored.pet_interaction_mode, "stretch")
            apply_display.assert_not_called()

    def test_expanded_skin_gallery_is_reachable_at_compact_sizes(self):
        self.assertEqual(mainpro.DesktopPet.FEATURED_PETS,
                         ("capybara", "red_panda", "penguin"))
        older_skins = set(mainpro.DesktopPet.PET_STYLES) - set(mainpro.DesktopPet.FEATURED_PETS)
        self.assertEqual(self.window.pet_more_button.text(),
                         f"更多外观 · {len(older_skins)} 款")
        more_layout = self.window.pet_more_skins.layout()
        self.assertEqual({more_layout.itemAt(index).widget().pet_kind
                          for index in range(more_layout.count())}, older_skins)
        self.window._nav(3)
        self.window.show()
        self.window.pet_more_button.setChecked(True)
        for width, height in ((860, 640), (760, 560)):
            self.window.resize(width, height)
            self.application.processEvents()
            self.assertTrue(self.window.pet_more_skins.isVisible())
            for pet_kind, button in self.window.pet_skin_buttons.items():
                with self.subTest(size=(width, height), pet_kind=pet_kind):
                    self.window.pet_scroll.ensureWidgetVisible(button, 0, 0)
                    self.application.processEvents()
                    viewport = self.window.pet_scroll.viewport()
                    center = button.mapTo(viewport, button.rect().center())
                    self.assertTrue(viewport.rect().contains(center))
        self.window.pet_more_button.setChecked(False)
        self.assertTrue(self.window.pet_more_skins.isHidden())

    def test_pet_interaction_modes_render_and_persist(self):
        pet = self.window.pet
        self.assertEqual(pet.interaction_mode, "move")
        self.assertEqual(self.window.pet_interaction_mode, "move")
        self.assertTrue(self.window.pet_interaction_buttons["move"].isChecked())

        self.window._set_pet_interaction_mode("tickle")
        self.assertEqual(pet.interaction_mode, "tickle")
        self.assertEqual(self.window.pet_preview.interaction_mode, "tickle")
        self.assertTrue(self.window.pet_interaction_buttons["tickle"].isChecked())
        self.assertIn("挠痒痒", self.window.pet_interaction_hint_label.text())

        # 拖动时应产生 wobble 与粒子，松手后回弹而不是停在形变状态。
        pet._begin_interaction(75, 85, 200, 200)
        pet._update_drag_interaction(118, 84, 245, 199)
        self.assertTrue(pet._interaction_active)
        self.assertGreater(pet._interaction_target["wobble"], 0)
        pet._finish_interaction(was_dragged=True)
        self.assertFalse(pet._interaction_active)
        self.assertGreater(len(pet._particles), 0)

        # "抛一下"和双击彩蛋都需要绘制通过，覆盖局部变换和粒子 renderer。
        self.window._set_pet_interaction_mode("toss")
        pet._begin_interaction(75, 82, 100, 100)
        pet._update_drag_interaction(109, 53, 170, 25)
        pet._finish_interaction(was_dragged=True)
        self.assertTrue(pet._toss_active)
        for _ in range(8):
            pet._tick_interaction(.06)
        pet.trigger_surprise("happy")
        self.assertEqual(pet._surprise_kind, "happy")
        pet.show()
        self.application.processEvents()
        self.assertFalse(pet.grab().isNull())

        self.window._save_settings()
        with open(self.config_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["pet_interaction_mode"], "toss")


class PetArtworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.preview = mainpro.PetPreview("blue_cat", animated=False, halo=False)

    def tearDown(self):
        self.preview.deleteLater()
        self.application.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    @staticmethod
    def _image_bytes(image):
        converted = image.convertToFormat(QImage.Format_RGBA8888)
        return converted.constBits().asstring(converted.byteCount())

    def _render(self, pet_kind, state="idle", decoration="scarf", scale=1.0,
                blink=0, look=(0.0, 0.0), surprise=None):
        self.preview._pet_kind = pet_kind
        self.preview._state = state
        self.preview.set_outfit((decoration,) if isinstance(decoration, str) else decoration)
        self.preview._phase = .35
        self.preview._blink = blink
        self.preview._look = look
        self.preview._surprise_kind = surprise
        image = QImage(round(self.preview.W * scale), round(self.preview.H * scale),
                       QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.scale(scale, scale)
        transform = painter.worldTransform()
        try:
            self.preview._paint_scene(painter, show_bar=False, show_bubble=False)
            self.assertEqual(painter.worldTransform(), transform)
        finally:
            painter.end()
        return image

    def test_new_skins_have_registered_renderers_and_complete_palettes(self):
        expected = {"capybara": "焦糖水豚", "red_panda": "枫叶小熊猫", "penguin": "雪团企鹅"}
        for pet_kind, label in expected.items():
            with self.subTest(pet_kind=pet_kind):
                style = mainpro.DesktopPet.PET_STYLES[pet_kind]
                self.assertEqual(style["label"], label)
                self.assertEqual(style["renderer"], pet_kind)
                self.assertIn(pet_kind, mainpro.DesktopPet.ART_TOP)
                self.assertEqual(set(style["palette"]), {"idle", "tired", "resting", "off"})
                for palette in style["palette"].values():
                    self.assertEqual(len(palette), 3)
                    self.assertTrue(all(QColor(color).isValid() for color in palette))
                renderer = getattr(self.preview, f"_paint_{pet_kind}")
                with patch.object(self.preview, f"_paint_{pet_kind}", wraps=renderer) as draw:
                    self._render(pet_kind)
                draw.assert_called_once()

    def test_new_skins_are_distinct_silhouettes_not_palette_swaps(self):
        new_skins = ("capybara", "red_panda", "penguin")
        silhouettes = {}
        for pet_kind in mainpro.DesktopPet.PET_STYLES:
            image = self._render(pet_kind, decoration=())
            silhouettes[pet_kind] = bytes(value > 127 for value in self._image_bytes(image)[3::4])
        for pet_kind in new_skins:
            for other_kind, silhouette in silhouettes.items():
                if other_kind != pet_kind:
                    with self.subTest(pet_kind=pet_kind, other_kind=other_kind):
                        self.assertNotEqual(silhouettes[pet_kind], silhouette)

    def test_new_skin_outfits_leave_eyes_unobstructed_in_every_state(self):
        top = mainpro.DesktopPet.BODY_TOP + math.sin(.35) * 3
        eyes = QRect(43, round(top + 29), 65, 27)
        for pet_kind in ("capybara", "red_panda", "penguin"):
            for state in ("idle", "tired", "resting", "off"):
                bare = self._render(pet_kind, state, decoration=())
                for outfit in (("scarf", "sprout", "star_pin"),
                               ("scarf", "star_pin", "night_cap")):
                    with self.subTest(pet_kind=pet_kind, state=state, outfit=outfit):
                        dressed = self._render(pet_kind, state, decoration=outfit)
                        self.assertEqual(self._image_bytes(bare.copy(eyes)),
                                         self._image_bytes(dressed.copy(eyes)))
                        self.assertNotEqual(self._image_bytes(bare), self._image_bytes(dressed))

    def test_penguin_star_pin_attaches_to_the_head(self):
        anchor_y = round(mainpro.DesktopPet.BODY_TOP + math.sin(.35) * 3 + 18)
        bare = self._render("penguin", decoration=())
        pinned = self._render("penguin", decoration="star_pin")
        self.assertGreaterEqual(bare.pixelColor(103, anchor_y).alpha(), 240)
        self.assertEqual(pinned.pixelColor(103, anchor_y), QColor("#e5ca8c"))

    def test_all_skins_states_and_decorations_fit_the_canvas(self):
        for pet_kind, style in mainpro.DesktopPet.PET_STYLES.items():
            for state in style["palette"]:
                for decoration in mainpro.DesktopPet.DECORATIONS:
                    with self.subTest(pet_kind=pet_kind, state=state, decoration=decoration):
                        image = self._render(pet_kind, state, decoration)
                        alpha = self._image_bytes(image)[3::4]
                        width = image.width()
                        border = alpha[:width] + alpha[-width:] + alpha[::width] + alpha[width - 1::width]
                        self.assertFalse(any(border), "Artwork is clipped at the window edge")
                        self.assertGreater(sum(value > 0 for value in alpha), 3500)

    def test_every_character_blinks_without_changing_its_silhouette(self):
        face = QRect(44, mainpro.DesktopPet.BODY_TOP + 20, 62, 40)
        for pet_kind in mainpro.DesktopPet.PET_STYLES:
            with self.subTest(pet_kind=pet_kind):
                awake = self._render(pet_kind)
                blinking = self._render(pet_kind, blink=3)
                self.assertNotEqual(self._image_bytes(awake.copy(face)),
                                    self._image_bytes(blinking.copy(face)))
                self.assertEqual(self._image_bytes(awake)[3::4],
                                 self._image_bytes(blinking)[3::4])

    def test_interaction_poses_do_not_crop_ears_or_tails(self):
        poses = {
            "squish": {"scale_x": 1.25, "scale_y": .64, "y": 8},
            "stretch": {"scale_x": .9, "scale_y": 1.35, "y": -12, "rotation": .2},
            "toss": {"x": 16, "y": -27, "rotation": .3, "scale_y": 1.14},
        }
        for pet_kind in mainpro.DesktopPet.PET_STYLES:
            for decoration in ("scarf", "sprout"):
                for pose_name, pose in poses.items():
                    with self.subTest(pet_kind=pet_kind, decoration=decoration, pose=pose_name):
                        self.preview._interaction_current = self.preview._neutral_interaction_values()
                        self.preview._interaction_current.update(pose)
                        image = self._render(pet_kind, decoration=decoration)
                        alpha = self._image_bytes(image)[3::4]
                        width = image.width()
                        border = alpha[:width] + alpha[-width:] + alpha[::width] + alpha[width - 1::width]
                        self.assertFalse(any(border), "Interaction crops the character")

    def test_every_character_tracks_the_pointer(self):
        face = QRect(44, mainpro.DesktopPet.BODY_TOP + 20, 62, 40)
        for pet_kind in mainpro.DesktopPet.PET_STYLES:
            with self.subTest(pet_kind=pet_kind):
                left = self._render(pet_kind, look=(-3.0, -2.5))
                right = self._render(pet_kind, look=(3.0, 2.5))
                self.assertNotEqual(self._image_bytes(left.copy(face)),
                                    self._image_bytes(right.copy(face)))

    def test_surprises_and_tickle_change_expression_not_care_state(self):
        face = QRect(44, mainpro.DesktopPet.BODY_TOP + 20, 62, 40)
        for pet_kind in mainpro.DesktopPet.PET_STYLES:
            with self.subTest(pet_kind=pet_kind):
                normal = self._render(pet_kind)
                happy = self._render(pet_kind, surprise="happy")
                self.assertEqual(self.preview._pet_expression(), "happy")
                self.assertEqual(self.preview._state, "idle")
                self.assertNotEqual(self._image_bytes(normal.copy(face)),
                                    self._image_bytes(happy.copy(face)))
                self._render(pet_kind, surprise="sleep")
                self.assertEqual(self.preview._pet_expression(), "sleep")
                self.assertEqual(self.preview._state, "idle")
        self.preview._surprise_kind = None
        self.preview._interaction_active = True
        self.preview._interaction_mode = "tickle"
        self.assertEqual(self.preview._pet_expression(), "happy")
        self.preview._state = "resting"
        self.assertEqual(self.preview._pet_expression(), "sleep")

    def test_robot_accessories_do_not_cover_the_pixel_heart(self):
        baseline = round(mainpro.DesktopPet.BODY_TOP + math.sin(.35) * 3 - 5)
        for decoration in ("scarf", "sprout", "star_pin", "night_cap",
                           ("scarf", "sprout", "star_pin"),
                           ("scarf", "star_pin", "night_cap")):
            with self.subTest(decoration=decoration):
                image = self._render("pixel_robot", decoration=decoration)
                heart_pixels = sum(
                    image.pixelColor(pixel_x, pixel_y) == QColor("#f0b6b6")
                    for pixel_x in range(68, 83)
                    for pixel_y in range(baseline + 79, baseline + 94)
                )
                self.assertEqual(heart_pixels, 144)

    def test_layered_outfits_fit_every_skin_and_care_state(self):
        for pet_kind, style in mainpro.DesktopPet.PET_STYLES.items():
            for state in style["palette"]:
                for outfit in ((), ("scarf", "sprout", "star_pin"),
                               ("scarf", "star_pin", "night_cap")):
                    with self.subTest(pet_kind=pet_kind, state=state, outfit=outfit):
                        image = self._render(pet_kind, state, decoration=outfit)
                        alpha = self._image_bytes(image)[3::4]
                        width = image.width()
                        border = alpha[:width] + alpha[-width:] + alpha[::width] + alpha[width - 1::width]
                        self.assertFalse(any(border))
                        self.assertGreater(sum(value > 0 for value in alpha), 3500)

    def test_thumbnail_and_high_dpi_rendering(self):
        for pet_kind in mainpro.DesktopPet.PET_STYLES:
            for scale in (.35, 1.25, 2.0):
                with self.subTest(pet_kind=pet_kind, scale=scale):
                    image = self._render(pet_kind, scale=scale)
                    self.assertEqual(image.width(), round(mainpro.DesktopPet.W * scale))
                    self.assertEqual(image.height(), round(mainpro.DesktopPet.H * scale))
                    alpha = self._image_bytes(image)[3::4]
                    self.assertGreater(sum(value > 0 for value in alpha), 3000 * scale * scale)

    def test_sleep_marks_do_not_require_font_glyphs(self):
        with patch.object(QPainter, "drawText") as draw_text:
            for pet_kind in mainpro.DesktopPet.PET_STYLES:
                self._render(pet_kind, state="resting")
        draw_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
