import ctypes
import os
import unittest
import uuid
from collections import Counter
from unittest.mock import Mock, patch

from careeyes_runtime import (
    GammaController, SingleInstance, WindowsActivityMonitor, WorkClock,
    _LastInputInfo, _WindowsInstanceApi,
)


def make_ramp(offset=0):
    return tuple(
        tuple(min(65535, index * 250 + offset + channel) for index in range(256))
        for channel in range(3)
    )


class FakeGammaBackend:
    def __init__(self):
        self.ramps = {"primary": make_ramp(10), "secondary": make_ramp(20)}
        self.connected = list(self.ramps)
        self.reads = Counter()
        self.writes = []
        self.read_failures = set()
        self.write_failures = set()

    def devices(self):
        return self.connected

    def read_ramp(self, device):
        self.reads[device] += 1
        if device in self.read_failures:
            raise OSError("read failed")
        return self.ramps[device]

    def write_ramp(self, device, ramp):
        self.writes.append((device, ramp))
        if device in self.write_failures:
            raise OSError("write failed")
        self.ramps[device] = ramp


class GammaControllerTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeGammaBackend()
        self.originals = dict(self.backend.ramps)
        self.controller = GammaController(self.backend)

    def test_repeated_apply_restores_exact_per_screen_originals(self):
        self.assertTrue(self.controller.apply(make_ramp(30)))
        self.assertTrue(self.controller.apply(make_ramp(40)))
        self.assertEqual(self.backend.reads, {"primary": 1, "secondary": 1})
        self.assertTrue(self.controller.restore())
        self.assertEqual(self.backend.ramps, self.originals)
        self.assertEqual(self.controller.originals, {})

    def test_unreadable_screen_is_never_modified(self):
        self.backend.read_failures.add("primary")
        self.assertTrue(self.controller.apply(make_ramp(30)))
        self.assertEqual([device for device, ramp in self.backend.writes], ["secondary"])
        self.assertIn("primary", self.controller.errors)
        self.assertTrue(self.controller.restore())
        self.assertEqual(self.backend.ramps, self.originals)

    def test_failed_restore_retains_snapshot_for_retry(self):
        self.controller.apply(make_ramp(30))
        self.backend.write_failures.add("primary")
        self.assertFalse(self.controller.restore())
        self.assertEqual(self.controller.originals, {"primary": self.originals["primary"]})
        self.backend.write_failures.clear()
        self.assertTrue(self.controller.restore())
        self.assertEqual(self.backend.ramps, self.originals)

    def test_failed_apply_still_preserves_original(self):
        self.backend.write_failures.add("primary")
        self.controller.apply(make_ramp(30))
        self.assertEqual(self.controller.originals["primary"], self.originals["primary"])
        self.backend.write_failures.clear()
        self.assertTrue(self.controller.restore())

    def test_disconnected_screen_is_restored_after_reconnect(self):
        self.controller.apply(make_ramp(30))
        self.backend.connected = ["primary"]
        self.assertFalse(self.controller.restore())
        self.assertIn("secondary", self.controller.originals)
        self.backend.connected.append("secondary")
        self.assertTrue(self.controller.restore())
        self.assertEqual(self.backend.ramps, self.originals)

    def test_new_screen_is_captured_before_first_write(self):
        self.controller.apply(make_ramp(30))
        added_original = make_ramp(50)
        self.backend.connected.append("added")
        self.backend.ramps["added"] = added_original
        self.controller.apply(make_ramp(40))
        self.assertEqual(self.controller.originals["added"], added_original)
        self.assertTrue(self.controller.restore())
        self.assertEqual(self.backend.ramps["added"], added_original)

    def test_reenable_captures_external_calibration_after_restore(self):
        self.controller.apply(make_ramp(30))
        self.controller.restore()
        self.backend.ramps["primary"] = make_ramp(60)
        self.controller.apply(make_ramp(40))
        self.controller.restore()
        self.assertEqual(self.backend.ramps["primary"], make_ramp(60))

    def test_restore_without_apply_does_not_touch_displays(self):
        self.backend.devices = Mock(side_effect=AssertionError("unexpected enumeration"))
        self.assertTrue(self.controller.restore())
        self.assertEqual(self.backend.writes, [])

    def test_enumeration_failure_keeps_existing_snapshots(self):
        self.controller.apply(make_ramp(30))
        self.backend.devices = Mock(side_effect=OSError("enumeration failed"))
        self.assertFalse(self.controller.restore())
        self.assertEqual(self.controller.originals, self.originals)
        self.assertIn("displays", self.controller.errors)


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.register_message.return_value = 0xC123
        self.api.create_mutex.return_value = (0x100000001, False)
        self.api.activate.return_value = True
        self.guard = SingleInstance("CareEyesTest", "test-config.json", api=self.api)

    def test_primary_owns_one_handle_and_close_is_idempotent(self):
        self.assertTrue(self.guard.acquire())
        self.assertTrue(self.guard.acquire())
        self.api.create_mutex.assert_called_once_with(self.guard.name)
        self.guard.close()
        self.guard.close()
        self.api.close.assert_called_once_with(0x100000001)

    def test_secondary_activates_existing_instance_without_becoming_primary(self):
        self.api.create_mutex.return_value = (44, True)
        self.assertFalse(self.guard.acquire())
        self.assertTrue(self.guard.activate_existing())
        self.api.activate.assert_called_once_with(0xC123)
        self.guard.close()
        self.api.close.assert_called_once_with(44)

    def test_different_config_paths_use_different_mutexes(self):
        other = SingleInstance("CareEyesTest", "other-config.json", api=self.api)
        self.assertNotEqual(self.guard.name, other.name)

    def test_lock_failure_does_not_claim_primary(self):
        self.api.create_mutex.side_effect = OSError("access denied")
        with self.assertRaises(OSError):
            self.guard.acquire()
        self.guard.close()
        self.api.close.assert_not_called()

    def test_activation_filter_only_allows_registered_message(self):
        self.guard.allow_activation(0x100000002)
        self.api.allow_activation.assert_called_once_with(0x100000002, 0xC123)

    def test_secondary_can_open_higher_integrity_mutex_read_only(self):
        api = _WindowsInstanceApi.__new__(_WindowsInstanceApi)
        api._kernel32 = Mock()
        api._kernel32.CreateMutexW.return_value = None
        api._kernel32.OpenMutexW.return_value = 44
        with patch("ctypes.set_last_error", create=True), patch(
            "ctypes.get_last_error", return_value=5, create=True
        ):
            self.assertEqual(api.create_mutex("test-mutex"), (44, True))
        api._kernel32.OpenMutexW.assert_called_once_with(0x00100000, False, "test-mutex")


@unittest.skipUnless(os.name == "nt", "Windows named mutex test")
class NativeMutexTests(unittest.TestCase):
    def test_kernel_mutex_is_exclusive_and_released(self):
        name = f"CareEyesTest-{uuid.uuid4().hex}"
        primary = SingleInstance(name, "test-config.json")
        secondary = SingleInstance(name, "test-config.json")
        replacement = SingleInstance(name, "test-config.json")
        try:
            self.assertTrue(primary.acquire())
            self.assertFalse(secondary.acquire())
            secondary.close()
            primary.close()
            self.assertTrue(replacement.acquire())
        finally:
            secondary.close()
            primary.close()
            replacement.close()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class WorkClockTests(unittest.TestCase):
    def setUp(self):
        self.source = FakeClock()
        self.clock = WorkClock(300, clock=self.source)
        self.clock.sample(True)

    def test_delayed_callback_uses_elapsed_seconds(self):
        self.source.advance(25)
        self.assertEqual(self.clock.sample(True), 25)
        self.assertEqual(self.clock.remaining_seconds, 275)
        self.assertEqual(self.clock.sample(True), 0)

    def test_pause_and_resume_do_not_credit_inactive_time(self):
        self.source.advance(12)
        self.assertEqual(self.clock.sample(False), 12)
        self.source.advance(3600)
        self.assertEqual(self.clock.sample(False), 0)
        self.assertEqual(self.clock.sample(True), 0)
        self.source.advance(8)
        self.clock.sample(True)
        self.assertEqual(self.clock.remaining_seconds, 280)
        self.assertEqual(self.clock.session_seconds, 20)

    def test_crossing_idle_threshold_only_credits_active_part(self):
        self.source.advance(10)
        self.assertEqual(self.clock.sample(False, inactive_seconds=6), 4)
        self.assertEqual(self.clock.remaining_seconds, 296)

    def test_resume_can_discard_unobserved_suspend_gap(self):
        self.source.advance(3600)
        self.assertEqual(self.clock.sample(False, inactive_seconds=float("inf")), 0)
        self.assertEqual(self.clock.remaining_seconds, 300)

    def test_restart_uses_new_interval_and_optional_session_reset(self):
        self.source.advance(20)
        self.clock.sample(True)
        self.clock.restart(600)
        self.assertEqual(self.clock.remaining_seconds, 600)
        self.assertEqual(self.clock.session_seconds, 20)
        self.clock.restart(300, reset_session=True)
        self.assertEqual(self.clock.session_seconds, 0)

    def test_fractional_seconds_round_up_and_expiry_never_goes_negative(self):
        self.source.advance(0.2)
        self.clock.sample(True)
        self.assertEqual(self.clock.remaining_seconds, 300)
        self.source.advance(500)
        self.clock.sample(True)
        self.assertEqual(self.clock.remaining_seconds, 0)

    def test_snooze_keeps_countdown_and_usage_running(self):
        self.clock.snooze(900)
        self.source.advance(120)
        self.assertEqual(self.clock.sample(True), 120)
        self.assertEqual(self.clock.remaining_seconds, 180)
        self.assertEqual(self.clock.session_seconds, 120)
        self.assertEqual(self.clock.snooze_remaining_seconds, 780)

    def test_snooze_expires_while_usage_clock_is_inactive(self):
        self.clock.snooze(900)
        self.clock.sample(False)
        self.source.advance(1200)
        self.assertEqual(self.clock.snooze_remaining_seconds, 0)
        self.assertEqual(self.clock.sample(False), 0)
        self.assertEqual(self.clock.remaining_seconds, 300)

    def test_cancel_snooze_preserves_work_progress(self):
        self.clock.snooze(900)
        self.source.advance(30)
        self.clock.sample(True)
        self.clock.cancel_snooze()
        self.assertEqual(self.clock.snooze_remaining_seconds, 0)
        self.assertEqual(self.clock.remaining_seconds, 270)
        self.assertEqual(self.clock.session_seconds, 30)

    def test_replacing_snooze_restarts_deadline_and_rounds_up(self):
        self.clock.snooze(900)
        self.source.advance(100)
        self.clock.snooze(1800)
        self.source.advance(0.2)
        self.assertEqual(self.clock.snooze_remaining_seconds, 1800)
        self.clock.restart(600)
        self.assertEqual(self.clock.snooze_remaining_seconds, 1800)

    def test_invalid_snooze_durations_preserve_existing_deadline(self):
        self.clock.snooze(900)
        for duration in (0, -1, True, "900", None, float("inf"), float("nan")):
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    self.clock.snooze(duration)
                self.assertEqual(self.clock.snooze_remaining_seconds, 900)


class ActivityMonitorTests(unittest.TestCase):
    def test_unavailable_monitor_degrades_to_unknown_activity(self):
        monitor = WindowsActivityMonitor.__new__(WindowsActivityMonitor)
        monitor._user32 = None
        self.assertIsNone(monitor.idle_seconds())

    def test_last_input_tick_wrap_is_handled(self):
        monitor = WindowsActivityMonitor.__new__(WindowsActivityMonitor)
        monitor._user32 = Mock()
        monitor._kernel32 = Mock()
        monitor._kernel32.GetTickCount64.return_value = 2**32 + 9000

        def fill_last_input(pointer):
            info = ctypes.cast(pointer, ctypes.POINTER(_LastInputInfo)).contents
            info.dwTime = 2**32 - 1000
            return True

        monitor._user32.GetLastInputInfo.side_effect = fill_last_input
        self.assertEqual(monitor.idle_seconds(), 10)

    def test_failed_idle_query_does_not_look_like_active_input(self):
        monitor = WindowsActivityMonitor.__new__(WindowsActivityMonitor)
        monitor._user32 = Mock()
        monitor._user32.GetLastInputInfo.return_value = False
        self.assertIsNone(monitor.idle_seconds())


if __name__ == "__main__":
    unittest.main()
