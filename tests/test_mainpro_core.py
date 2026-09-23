import unittest
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mainpro


class FormattingTests(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(mainpro._format_bytes(None), "\u2014")
        self.assertEqual(mainpro._format_bytes(0), "0 B")
        self.assertEqual(mainpro._format_bytes(1536), "1.5 KB")
        self.assertEqual(mainpro._format_bytes(-1), "\u2014")

    def test_format_uptime(self):
        self.assertEqual(mainpro._format_uptime(None), "\u2014")
        self.assertEqual(mainpro._format_uptime(0), "0\u5206")
        self.assertEqual(mainpro._format_uptime(3660), "1\u65f6 01\u5206")
        self.assertEqual(mainpro._format_uptime(90061), "1\u5929 01\u65f6")

    def test_percent(self):
        self.assertEqual(mainpro._percent(25), 25.0)
        self.assertEqual(mainpro._percent(5, 10), 50.0)
        self.assertEqual(mainpro._percent(10, 0), None)
        self.assertEqual(mainpro._percent(None, 10), None)
        self.assertEqual(mainpro._percent(120), 100.0)

    def test_bounded_values_and_position(self):
        self.assertEqual(mainpro._bounded_int(float("nan"), 7, 0, 10), 7)
        self.assertEqual(mainpro._bounded_int(99, 7, 0, 10), 10)
        self.assertEqual(mainpro._bounded_float(float("inf"), 0.5, 0, 1), 0.5)
        self.assertEqual(mainpro._parse_position([12.9, -4.2]), [12, -4])
        self.assertIsNone(mainpro._parse_position([float("nan"), 2]))


class DisplayManagerTests(unittest.TestCase):
    def setUp(self):
        mainpro.DisplayManager._build_ramp.cache_clear()
        self.addCleanup(mainpro.DisplayManager._build_ramp.cache_clear)

    def test_cached_ramp_is_immutable_and_preserves_samples(self):
        channels = (0.5, 1.0, 0.2)
        ramp = mainpro.DisplayManager._build_ramp(*channels)
        expected = tuple(
            tuple(int(channel * (sample_index * 256))
                  for sample_index in range(256))
            for channel in channels
        )
        self.assertEqual(ramp, expected)
        self.assertIs(ramp, mainpro.DisplayManager._build_ramp(*channels))
        with self.assertRaises(TypeError):
            ramp[0][0] = 1

    def test_repeated_settings_reuse_ramp_but_still_write_displays(self):
        controller = Mock(spec=mainpro.GammaController)
        controller.apply.return_value = True
        with patch.object(mainpro.DisplayManager, "_controller", controller):
            self.assertTrue(mainpro.DisplayManager.apply(5000, 0.9))
            self.assertTrue(mainpro.DisplayManager.apply(5000, 0.9))
        self.assertEqual(controller.apply.call_count, 2)
        self.assertIs(
            controller.apply.call_args_list[0].args[0],
            controller.apply.call_args_list[1].args[0],
        )
        cache = mainpro.DisplayManager._build_ramp.cache_info()
        self.assertEqual((cache.hits, cache.misses), (1, 1))

    def test_changed_settings_build_distinct_ramps(self):
        controller = Mock(spec=mainpro.GammaController)
        with patch.object(mainpro.DisplayManager, "_controller", controller):
            mainpro.DisplayManager.apply(5000, 0.6)
            mainpro.DisplayManager.apply(5000, 0.9)
            mainpro.DisplayManager.apply(6000, 0.9)
        ramps = [call.args[0] for call in controller.apply.call_args_list]
        self.assertNotEqual(ramps[0], ramps[1])
        self.assertNotEqual(ramps[1], ramps[2])
        self.assertEqual(mainpro.DisplayManager._build_ramp.cache_info().misses, 3)

    def test_failed_display_write_is_not_cached_as_success(self):
        controller = Mock(spec=mainpro.GammaController)
        controller.apply.side_effect = [False, True]
        with patch.object(mainpro.DisplayManager, "_controller", controller):
            self.assertFalse(mainpro.DisplayManager.apply(5000, 0.9))
            self.assertTrue(mainpro.DisplayManager.apply(5000, 0.9))
        self.assertEqual(controller.apply.call_count, 2)

    def test_guard_uses_controller_verification_with_cached_ramp(self):
        controller = Mock(spec=mainpro.GammaController)
        controller.ensure.return_value = True
        with patch.object(mainpro.DisplayManager, "_controller", controller):
            self.assertTrue(mainpro.DisplayManager.ensure(5000, 0.9))
            self.assertTrue(mainpro.DisplayManager.ensure(5000, 0.9))
        self.assertEqual(controller.ensure.call_count, 2)
        self.assertIs(
            controller.ensure.call_args_list[0].args[0],
            controller.ensure.call_args_list[1].args[0],
        )

    def test_ramp_cache_is_bounded(self):
        for sample_index in range(40):
            mainpro.DisplayManager._build_ramp(sample_index / 40, 1.0, 1.0)
        cache = mainpro.DisplayManager._build_ramp.cache_info()
        self.assertEqual(cache.maxsize, 32)
        self.assertEqual(cache.currsize, 32)


class UiStateDeduplicationTests(unittest.TestCase):
    def test_unchanged_preset_state_does_not_reparse_styles(self):
        buttons = {name: Mock() for name in mainpro.MODES}
        subject = SimpleNamespace(
            temp=5000,
            bright=0.9,
            mode_btns=buttons,
            _mode_qss=Mock(side_effect=lambda active: str(active)),
        )
        mainpro.CareEyesApp._sync_mode_selection(subject)
        self.assertEqual(subject._mode_qss.call_count, len(mainpro.MODES))

        subject._mode_qss.reset_mock()
        for button in buttons.values():
            button.setStyleSheet.reset_mock()
        mainpro.CareEyesApp._sync_mode_selection(subject)
        subject._mode_qss.assert_not_called()
        for button in buttons.values():
            button.setStyleSheet.assert_not_called()

        subject.temp = 5100
        mainpro.CareEyesApp._sync_mode_selection(subject)
        self.assertEqual(subject._mode_qss.call_count, len(mainpro.MODES))


class MetricsTests(unittest.TestCase):
    def test_cpu_delta(self):
        calc = mainpro.SystemMetricsCollector._calculate_cpu_percent
        self.assertEqual(calc((10, 20, 30), (20, 40, 50)), 75.0)
        self.assertEqual(calc((10, 20, 30), (20, 30, 30)), 0.0)
        self.assertIsNone(calc((1, 2), (3, 4)))
        self.assertIsNone(calc((3, 2, 1), (1, 2, 3)))

    def test_unavailable_collector_degrades(self):
        collector = mainpro.SystemMetricsCollector.__new__(
            mainpro.SystemMetricsCollector
        )
        collector._kernel32 = None
        collector._previous_cpu = None
        snapshot = collector.sample_fast()
        self.assertEqual(snapshot.cpu_state, "unavailable")
        self.assertIsNone(snapshot.cpu_percent)
        self.assertIsNone(snapshot.memory_total)
        self.assertIsNone(snapshot.uptime_seconds)

    def test_disk_snapshot_has_a_root(self):
        snapshot = mainpro.SystemMetricsCollector().sample_disk()
        self.assertTrue(snapshot.root)


class StatisticsTests(unittest.TestCase):
    def test_rollover_archives_previous_day(self):
        app = mainpro.CareEyesApp.__new__(mainpro.CareEyesApp)
        previous = mainpro.date.today() - mainpro.timedelta(days=1)
        today = mainpro.date.today().isoformat()
        app._stat_date = previous.isoformat()
        app.today_minutes = 42
        app.break_count = 3
        app.week_data = {}
        app.session_start = mainpro.datetime.now()

        changed = app._rollover_stats_if_needed(today)

        self.assertTrue(changed)
        self.assertEqual(app.week_data[previous.isoformat()], 42)
        self.assertEqual(app.today_minutes, 0)
        self.assertEqual(app.break_count, 0)
        self.assertEqual(app._stat_date, today)

    def test_invalid_or_future_stat_days_are_rejected(self):
        cutoff = mainpro.date.today() - mainpro.timedelta(days=31)
        self.assertFalse(mainpro.CareEyesApp._valid_stat_day("not-a-date", cutoff))
        self.assertFalse(
            mainpro.CareEyesApp._valid_stat_day(
                (mainpro.date.today() + mainpro.timedelta(days=1)).isoformat(),
                cutoff,
            )
        )

    def test_load_settings_recovers_previous_day_snapshot(self):
        previous = mainpro.date.today() - mainpro.timedelta(days=1)
        config = dict(mainpro.CareEyesApp._DEFAULTS)
        config.update({
            "stat_date": previous.isoformat(),
            "today_minutes": 37,
            "break_count": 2,
        })
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        old_config = mainpro.CONFIG_FILE
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(config, handle)
            mainpro.CONFIG_FILE = path
            app = mainpro.CareEyesApp.__new__(mainpro.CareEyesApp)
            app.load_settings()
            self.assertEqual(app.week_data[previous.isoformat()], 37)
            self.assertEqual(app.today_minutes, 0)
            self.assertEqual(app.break_count, 0)
        finally:
            mainpro.CONFIG_FILE = old_config
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    unittest.main()
