import ctypes
import ctypes.wintypes as wintypes
import hashlib
import math
import os
import time


GammaRamp = ctypes.c_ushort * 256 * 3


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


class WindowsGammaBackend:
    def __init__(self):
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
        )
        self._user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC, ctypes.POINTER(wintypes.RECT),
            self._callback_type, wintypes.LPARAM,
        ]
        self._user32.EnumDisplayMonitors.restype = wintypes.BOOL
        self._user32.GetMonitorInfoW.argtypes = [
            wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo),
        ]
        self._user32.GetMonitorInfoW.restype = wintypes.BOOL
        self._gdi32.CreateDCW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
        ]
        self._gdi32.CreateDCW.restype = wintypes.HDC
        for name in ("GetDeviceGammaRamp", "SetDeviceGammaRamp"):
            function = getattr(self._gdi32, name)
            function.argtypes = [wintypes.HDC, ctypes.c_void_p]
            function.restype = wintypes.BOOL
        self._gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self._gdi32.DeleteDC.restype = wintypes.BOOL

    def devices(self):
        monitors = []

        def collect_monitor(monitor, device_context, rectangle, user_data):
            monitors.append(monitor)
            return True

        callback = self._callback_type(collect_monitor)
        if not self._user32.EnumDisplayMonitors(None, None, callback, 0):
            raise OSError("EnumDisplayMonitors failed")
        devices = []
        for monitor in monitors:
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                devices.append(info.szDevice)
        return list(dict.fromkeys(devices))

    def _open(self, device):
        device_context = self._gdi32.CreateDCW("DISPLAY", device, None, None)
        if not device_context:
            raise OSError(f"CreateDCW failed: {device}")
        return device_context

    def read_ramp(self, device):
        device_context = self._open(device)
        try:
            ramp = GammaRamp()
            if not self._gdi32.GetDeviceGammaRamp(device_context, ctypes.byref(ramp)):
                raise OSError(f"GetDeviceGammaRamp failed: {device}")
            return tuple(tuple(channel) for channel in ramp)
        finally:
            self._gdi32.DeleteDC(device_context)

    def write_ramp(self, device, values):
        ramp = GammaRamp()
        for channel_index, channel in enumerate(values):
            ramp[channel_index][:] = channel
        device_context = self._open(device)
        try:
            if not self._gdi32.SetDeviceGammaRamp(device_context, ctypes.byref(ramp)):
                raise OSError(f"SetDeviceGammaRamp failed: {device}")
        finally:
            self._gdi32.DeleteDC(device_context)


class GammaController:
    def __init__(self, backend):
        self.backend = backend
        self.originals = {}
        self.errors = {}

    def apply(self, ramp):
        self.errors = {}
        try:
            devices = self.backend.devices()
        except OSError as error:
            self.errors["displays"] = str(error)
            return False
        applied = False
        values = tuple(tuple(channel) for channel in ramp)
        for device in devices:
            try:
                if device not in self.originals:
                    original = self.backend.read_ramp(device)
                    self.originals[device] = tuple(tuple(channel) for channel in original)
                self.backend.write_ramp(device, values)
                applied = True
            except OSError as error:
                self.errors[device] = str(error)
        return applied

    def restore(self):
        self.errors = {}
        if not self.originals:
            return True
        try:
            connected = set(self.backend.devices())
        except OSError as error:
            self.errors["displays"] = str(error)
            return False
        for device, original in list(self.originals.items()):
            if device not in connected:
                continue
            try:
                self.backend.write_ramp(device, original)
                del self.originals[device]
            except OSError as error:
                self.errors[device] = str(error)
        return not self.originals


class _WindowsInstanceApi:
    def __init__(self):
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.OpenMutexW.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        self._user32.RegisterWindowMessageW.restype = wintypes.UINT
        self._user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self._user32.PostMessageW.restype = wintypes.BOOL
        self._user32.ChangeWindowMessageFilterEx.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p,
        ]
        self._user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL

    def register_message(self, name):
        message = self._user32.RegisterWindowMessageW(name)
        if not message:
            raise ctypes.WinError(ctypes.get_last_error())
        return message

    def create_mutex(self, name):
        ctypes.set_last_error(0)
        handle = self._kernel32.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle and error == 5:
            handle = self._kernel32.OpenMutexW(0x00100000, False, name)
            if handle:
                return handle, True
        if not handle:
            raise ctypes.WinError(error)
        return handle, error == 183

    def activate(self, message):
        return bool(self._user32.PostMessageW(0xFFFF, message, 0, 0))

    def allow_activation(self, window_handle, message):
        return bool(self._user32.ChangeWindowMessageFilterEx(window_handle, message, 1, None))

    def close(self, handle):
        self._kernel32.CloseHandle(handle)


class SingleInstance:
    def __init__(self, app_name, config_path, api=None):
        self._api = api if api is not None else _WindowsInstanceApi()
        identity = os.path.normcase(os.path.abspath(config_path)).encode("utf-8")
        suffix = hashlib.sha256(identity).hexdigest()[:24]
        self.name = f"Local\\{app_name}-{suffix}"
        self.activation_message = self._api.register_message(f"{app_name}.Activate.{suffix}")
        self._handle = None
        self._primary = False

    def acquire(self):
        if self._handle is None:
            self._handle, already_exists = self._api.create_mutex(self.name)
            self._primary = not already_exists
        return self._primary

    def activate_existing(self):
        return self._api.activate(self.activation_message)

    def allow_activation(self, window_handle):
        return self._api.allow_activation(window_handle, self.activation_message)

    def close(self):
        if self._handle is not None:
            self._api.close(self._handle)
            self._handle = None
            self._primary = False


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class WindowsActivityMonitor:
    def __init__(self):
        self._user32 = None
        self._window_handle = None
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
            self._user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
            self._user32.GetLastInputInfo.restype = wintypes.BOOL
            self._kernel32.GetTickCount64.argtypes = []
            self._kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            self._wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
            self._wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
            self._wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
            self._wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
        except (AttributeError, OSError):
            self._user32 = None

    def idle_seconds(self):
        if self._user32 is None:
            return None
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        elapsed = (self._kernel32.GetTickCount64() - info.dwTime) & 0xFFFFFFFF
        return elapsed / 1000.0

    def register(self, window_handle):
        if self._user32 is None:
            return False
        if not self._wtsapi32.WTSRegisterSessionNotification(window_handle, 0):
            return False
        self._window_handle = window_handle
        return True

    def close(self):
        if self._window_handle is not None:
            self._wtsapi32.WTSUnRegisterSessionNotification(self._window_handle)
            self._window_handle = None


class PetProgress:
    EXPERIENCE_PER_REST = 10
    RESTS_PER_LEVEL = 2
    DEFAULT_OUTFIT = ("scarf", "sprout")
    OUTFIT_RULES = {
        "scarf": {"slot": "neck", "rests": 0},
        "sprout": {"slot": "head", "rests": 0},
        "star_pin": {"slot": "pin", "rests": 6},
        "night_cap": {"slot": "head", "rests": 12},
    }

    def __init__(self, completed_rests=0, outfit=None):
        if type(completed_rests) is not int or completed_rests < 0:
            raise ValueError("completed_rests must be a nonnegative integer")
        self.completed_rests = completed_rests
        selected = self.DEFAULT_OUTFIT if outfit is None else outfit
        if not isinstance(selected, (list, tuple)):
            raise ValueError("outfit must be a list or tuple")
        slots = {}
        for decoration in selected:
            if self.is_unlocked(decoration):
                slots[self.OUTFIT_RULES[decoration]["slot"]] = decoration
        self._outfit = tuple(
            decoration for decoration, rule in self.OUTFIT_RULES.items()
            if slots.get(rule["slot"]) == decoration
        )

    @property
    def outfit(self):
        return self._outfit

    @property
    def level(self):
        return 1 + self.completed_rests // self.RESTS_PER_LEVEL

    @property
    def experience(self):
        return self.completed_rests * self.EXPERIENCE_PER_REST

    @property
    def level_experience(self):
        return self.completed_rests % self.RESTS_PER_LEVEL * self.EXPERIENCE_PER_REST

    @property
    def experience_per_level(self):
        return self.RESTS_PER_LEVEL * self.EXPERIENCE_PER_REST

    @property
    def next_unlock(self):
        return next(
            ((decoration, rule["rests"])
             for decoration, rule in self.OUTFIT_RULES.items()
             if self.completed_rests < rule["rests"]),
            None,
        )

    def is_unlocked(self, decoration):
        return (isinstance(decoration, str) and decoration in self.OUTFIT_RULES
                and self.completed_rests >= self.OUTFIT_RULES[decoration]["rests"])

    def toggle_decoration(self, decoration):
        if not self.is_unlocked(decoration):
            return False
        if decoration in self._outfit:
            self._outfit = tuple(item for item in self._outfit if item != decoration)
        else:
            slot = self.OUTFIT_RULES[decoration]["slot"]
            selected = {item for item in self._outfit
                        if self.OUTFIT_RULES[item]["slot"] != slot}
            selected.add(decoration)
            self._outfit = tuple(item for item in self.OUTFIT_RULES if item in selected)
        return True

    def complete_rest(self):
        self.completed_rests += 1
        return tuple(
            decoration for decoration, rule in self.OUTFIT_RULES.items()
            if rule["rests"] == self.completed_rests
        )


class WorkClock:
    def __init__(self, duration_seconds, clock=None):
        self._clock = clock if clock is not None else time.monotonic
        self._last_sample = self._clock()
        self._remaining = max(0.0, float(duration_seconds))
        self._snooze_deadline = None
        self.active = False
        self.session_seconds = 0.0

    @property
    def remaining_seconds(self):
        return math.ceil(self._remaining)

    @property
    def snooze_remaining_seconds(self):
        if self._snooze_deadline is None:
            return 0
        return max(0, math.ceil(self._snooze_deadline - self._clock()))

    def snooze(self, duration_seconds):
        if (isinstance(duration_seconds, bool)
                or not isinstance(duration_seconds, (int, float))
                or not math.isfinite(duration_seconds) or duration_seconds <= 0):
            raise ValueError("snooze duration must be a positive finite number")
        self._snooze_deadline = self._clock() + duration_seconds

    def cancel_snooze(self):
        self._snooze_deadline = None

    def sample(self, active, inactive_seconds=0.0):
        now = self._clock()
        elapsed = max(0.0, now - self._last_sample)
        self._last_sample = now
        credited = max(0.0, elapsed - inactive_seconds) if self.active else 0.0
        self._remaining = max(0.0, self._remaining - credited)
        self.session_seconds += credited
        self.active = bool(active)
        return credited

    def restart(self, duration_seconds, reset_session=False):
        self._remaining = max(0.0, float(duration_seconds))
        self._last_sample = self._clock()
        if reset_session:
            self.session_seconds = 0.0
