
import sys
import csv
import ctypes
import ctypes.wintypes
import io
import math
import json
import os
import random
import shutil
import time
try:
    import winreg
except ImportError:  # 允许在非 Windows 环境运行纯逻辑检查
    winreg = None
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from functools import lru_cache
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton, QLabel,
    QSlider, QHBoxLayout, QFrame, QStackedWidget, QSpinBox,
    QSystemTrayIcon, QMenu, QAction, QCheckBox, QGraphicsDropShadowEffect,
    QSizePolicy, QGridLayout, QProgressBar, QMessageBox, QScrollArea, QFileDialog
)
from PyQt5.QtCore import (
    Qt, QTimer, QPointF, QRect, QRectF, pyqtSignal, QObject,
    QEasingCurve, QIODevice, QSaveFile, QTimeLine,
)
from PyQt5.QtGui import (
    QColor, QCursor, QFont, QFontMetrics, QGradient, QIcon, QLinearGradient,
    QPainter, QPainterPath, QPen, QPixmap, QRadialGradient, QTransform
)
from careeyes_runtime import (
    GammaController, PetProgress, SingleInstance, WindowsActivityMonitor,
    WindowsGammaBackend, WorkClock,
)

# ─────────────────────────────────────────────
# ⚙️  全局常量
# ─────────────────────────────────────────────
APP_NAME    = "CareEyesPro"
APP_TITLE   = "CareEyes Pro"
APP_VER     = "v5.5"
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".care_eyes_pro.json")
IDLE_PAUSE_SECONDS = 300

# 全屏检测：这些进程名即便占全屏也不触发推迟（黑名单=不推迟）
FULLSCREEN_WHITELIST = {
    "explorer.exe", "shellexperiencehost.exe", "searchhost.exe",
    "wallpaperengine.exe", "desktopwindowmanager.exe", "dwm.exe",
    "lively.exe", "rainmeter.exe", "everything.exe",
}
# 全屏检测：这些进程名强制推迟（白名单=一定推迟，优先级高于尺寸判断）
FULLSCREEN_FORCE_DEFER = {
    "javaw.exe",   # Minecraft
}

MODES = {
    "常规": {"temp": 5000, "bright": 0.90, "icon": "○"},
    "办公": {"temp": 5500, "bright": 1.00, "icon": "□"},
    "游戏": {"temp": 6000, "bright": 1.00, "icon": "◈"},
    "阅读": {"temp": 4000, "bright": 0.80, "icon": "≡"},
    "睡眠": {"temp": 2500, "bright": 0.55, "icon": "◐"},
    "户外": {"temp": 6500, "bright": 1.00, "icon": "◉"},
}

# 24小时自动色温曲线
AUTO_CURVE = {
    0: 2700, 1: 2700, 2: 2700, 3: 2700, 4: 2700, 5: 3200,
    6: 4000, 7: 5000, 8: 5800, 9: 6200, 10: 6500, 11: 6500,
    12: 6500, 13: 6500, 14: 6500, 15: 6200, 16: 5800, 17: 5200,
    18: 4500, 19: 4000, 20: 3500, 21: 3200, 22: 3000, 23: 2700,
}


def _write_atomic(path, content):
    save_file = QSaveFile(os.fspath(path))
    save_file.setDirectWriteFallback(False)
    if not save_file.open(QIODevice.WriteOnly):
        raise OSError(save_file.errorString())
    if save_file.write(content) != len(content):
        error = save_file.errorString()
        save_file.cancelWriting()
        raise OSError(error)
    if not save_file.commit():
        raise OSError(save_file.errorString())


# ─────────────────────────────────────────────
# 🎨  系统主题色读取 (#10)
# ─────────────────────────────────────────────
def _read_system_accent() -> str:
    """读取 Windows 系统强调色，返回 #RRGGBB，失败时返回默认蓝。"""
    if winreg is None:
        return "#0ea5e9"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent"
        ) as key:
        # AccentColorMenu 是 ABGR 格式的 DWORD
            val, _ = winreg.QueryValueEx(key, "AccentColorMenu")
        b = (val >> 16) & 0xFF
        g = (val >>  8) & 0xFF
        r =  val        & 0xFF
        # 太暗的颜色（亮度 < 40）回退默认
        if (r * 299 + g * 587 + b * 114) // 1000 < 40:
            return "#0ea5e9"
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return "#0ea5e9"


# ─────────────────────────────────────────────
# 🔑  权限检测 (#2)
# ─────────────────────────────────────────────
def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _format_bytes(value) -> str:
    """将字节数格式化成紧凑的用户可读文本。"""
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(value) or value < 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def _format_uptime(seconds) -> str:
    """将系统运行秒数格式化为天/时/分，避免状态卡片被撑宽。"""
    if seconds is None:
        return "—"
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "—"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}天 {hours:02d}时"
    if hours:
        return f"{hours}时 {minutes:02d}分"
    return f"{minutes}分"


def _percent(value, total=None):
    """返回 0..100 的百分比；输入不完整时返回 None。"""
    if total is not None:
        try:
            if total <= 0:
                return None
            value = float(value) / float(total) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    try:
        value = float(value)
        if not math.isfinite(value):
            return None
        return max(0.0, min(100.0, value))
    except (TypeError, ValueError):
        return None


def _bounded_int(value, default, low=None, high=None):
    """将外部配置安全转换为整数，拒绝 bool/NaN/无穷值。"""
    try:
        if isinstance(value, bool):
            raise ValueError
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError
        result = int(numeric)
    except (TypeError, ValueError, OverflowError):
        result = int(default)
    if low is not None:
        result = max(int(low), result)
    if high is not None:
        result = min(int(high), result)
    return result


def _bounded_float(value, default, low=None, high=None):
    """将外部配置安全转换为有限浮点数并限制范围。"""
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        result = float(default)
    if low is not None:
        result = max(float(low), result)
    if high is not None:
        result = min(float(high), result)
    return result


def _parse_position(value):
    """解析桌宠位置；损坏或非有限坐标直接回退默认位置。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        coords = [float(v) for v in value]
        if not all(math.isfinite(v) for v in coords):
            return None
        return [int(coords[0]), int(coords[1])]
    except (TypeError, ValueError, OverflowError):
        return None


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.wintypes.DWORD),
        ("dwHighDateTime", ctypes.wintypes.DWORD),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.wintypes.DWORD),
        ("dwMemoryLoad", ctypes.wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


@dataclass
class SystemSnapshot:
    cpu_state: str = "unavailable"
    cpu_percent: float = None
    memory_used: int = None
    memory_total: int = None
    uptime_seconds: int = None


@dataclass
class DiskSnapshot:
    root: str
    used: int = None
    total: int = None
    free: int = None


class SystemMetricsCollector:
    """使用 WinAPI 和标准库读取轻量系统状态。"""

    def __init__(self):
        self._kernel32 = None
        self._previous_cpu = None
        try:
            self._kernel32 = ctypes.windll.kernel32
            self._kernel32.GetSystemTimes.argtypes = [
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME),
            ]
            self._kernel32.GetSystemTimes.restype = ctypes.wintypes.BOOL
            self._kernel32.GlobalMemoryStatusEx.argtypes = [
                ctypes.POINTER(_MEMORYSTATUSEX)
            ]
            self._kernel32.GlobalMemoryStatusEx.restype = ctypes.wintypes.BOOL
            self._kernel32.GetTickCount64.argtypes = []
            self._kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        except Exception:
            # 统计页仍可显示磁盘信息，CPU/内存/运行时间降级为不可用。
            self._kernel32 = None

    @property
    def available(self):
        return self._kernel32 is not None

    @staticmethod
    def _filetime_value(value):
        return (value.dwHighDateTime << 32) | value.dwLowDateTime

    def reset_cpu_baseline(self):
        self._previous_cpu = None

    def _read_system_times(self):
        if self._kernel32 is None:
            raise OSError("Windows performance API unavailable")
        idle = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not self._kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError()
        return tuple(self._filetime_value(v) for v in (idle, kernel, user))

    @staticmethod
    def _calculate_cpu_percent(previous, current):
        if not previous or not current or len(previous) != 3 or len(current) != 3:
            return None
        try:
            deltas = tuple(now - old for old, now in zip(previous, current))
        except TypeError:
            return None
        idle_delta, kernel_delta, user_delta = deltas
        total_delta = kernel_delta + user_delta
        if any(delta < 0 for delta in deltas) or total_delta <= 0:
            return None
        busy_delta = total_delta - idle_delta
        return _percent(busy_delta / total_delta * 100.0)

    def _sample_cpu(self):
        try:
            current = self._read_system_times()
        except Exception:
            self.reset_cpu_baseline()
            return "unavailable", None
        previous = self._previous_cpu
        self._previous_cpu = current
        if previous is None:
            return "warming", None
        percent = self._calculate_cpu_percent(previous, current)
        if percent is None:
            return "warming", None
        return "ready", percent

    def _sample_memory(self):
        try:
            if self._kernel32 is None:
                raise OSError("Windows performance API unavailable")
            status = _MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(status)
            if not self._kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise ctypes.WinError()
            total = int(status.ullTotalPhys)
            available = int(status.ullAvailPhys)
            if total <= 0:
                raise ValueError("invalid physical memory total")
            return max(0, total - available), total
        except Exception:
            return None, None

    def _sample_uptime(self):
        try:
            if self._kernel32 is None:
                raise OSError("Windows performance API unavailable")
            return int(self._kernel32.GetTickCount64() // 1000)
        except Exception:
            return None

    def sample_fast(self):
        cpu_state, cpu_percent = self._sample_cpu()
        memory_used, memory_total = self._sample_memory()
        return SystemSnapshot(
            cpu_state=cpu_state,
            cpu_percent=cpu_percent,
            memory_used=memory_used,
            memory_total=memory_total,
            uptime_seconds=self._sample_uptime(),
        )

    @staticmethod
    def system_drive_root():
        root = os.environ.get("SystemDrive")
        if not root:
            root = os.path.splitdrive(os.environ.get("SystemRoot", ""))[0]
        if os.name != "nt":
            return os.path.abspath(os.sep)
        root = (root or "C:").rstrip("\\/")
        return os.path.abspath(root + os.sep)

    def sample_disk(self):
        root = self.system_drive_root()
        try:
            usage = shutil.disk_usage(root)
            return DiskSnapshot(root, usage.used, usage.total, usage.free)
        except Exception:
            return DiskSnapshot(root)


class DisplayManager:
    _controller = None

    @staticmethod
    def _kelvin_to_rgb(temp_kelvin):
        try:
            temp_kelvin = max(1000.0, min(10000.0, float(temp_kelvin)))
        except (TypeError, ValueError):
            temp_kelvin = 6500.0
        t = temp_kelvin / 100
        if t <= 66:
            r = 255
            g = max(0.0, 99.47 * math.log(max(t, 1)) - 161.12)
        else:
            r = max(0.0, min(255.0, 329.7 * ((t - 60) ** -0.13)))
            g = max(0.0, min(255.0, 288.1 * ((t - 60) ** -0.07)))
        b = (255.0 if t >= 66 else
             0.0   if t <= 19 else
             max(0.0, min(255.0, 138.5 * math.log(t - 10) - 305.05)))
        return r / 255, g / 255, b / 255

    @staticmethod
    @lru_cache(maxsize=32)
    def _build_ramp(red, green, blue):
        try:
            channels = [max(0.0, min(1.0, float(channel)))
                        for channel in (red, green, blue)]
        except (TypeError, ValueError):
            channels = [1.0, 1.0, 1.0]
        return tuple(
            tuple(int(min(65535, channel * sample_index * 256))
                  for sample_index in range(256))
            for channel in channels
        )

    @classmethod
    def _target_ramp(cls, temp_kelvin, brightness):
        r, g, b = cls._kelvin_to_rgb(temp_kelvin)
        try:
            brightness = max(0.0, min(1.0, float(brightness)))
        except (TypeError, ValueError):
            brightness = 1.0
        r *= brightness; g *= brightness; b *= brightness
        return cls._build_ramp(r, g, b)

    @classmethod
    def _get_controller(cls):
        if cls._controller is None:
            try:
                cls._controller = GammaController(WindowsGammaBackend())
            except (AttributeError, OSError):
                return None
        return cls._controller

    @classmethod
    def apply(cls, temp_kelvin, brightness):
        controller = cls._get_controller()
        if controller is None:
            return False
        return controller.apply(cls._target_ramp(temp_kelvin, brightness))

    @classmethod
    def ensure(cls, temp_kelvin, brightness):
        controller = cls._get_controller()
        if controller is None:
            return False
        return controller.ensure(cls._target_ramp(temp_kelvin, brightness))

    @classmethod
    def reset(cls):
        return cls._controller.restore() if cls._controller is not None else True


# ─────────────────────────────────────────────
# 🔁  SmoothTransition —— 平滑渐变
# ─────────────────────────────────────────────
class SmoothTransition(QObject):
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timeline = QTimeLine(1500, self)
        self._timeline.setEasingCurve(QEasingCurve.OutCubic)
        self._timeline.valueChanged.connect(self._step)
        self._timeline.finished.connect(self.finished)
        self._cur_temp = 5000.0; self._cur_bright = 1.0
        self._tgt_temp = 5000.0; self._tgt_bright = 1.0
        self._last_temp = 5000.0; self._last_bright = 1.0

    def start(self, cur_temp, cur_bright, tgt_temp, tgt_bright,
              duration_ms=1500, step_ms=50):
        if self.is_active():
            cur_temp, cur_bright = self._last_temp, self._last_bright
        self.stop()
        self._cur_temp = float(cur_temp); self._cur_bright = float(cur_bright)
        self._tgt_temp = float(tgt_temp); self._tgt_bright = float(tgt_bright)
        self._last_temp, self._last_bright = self._cur_temp, self._cur_bright
        self._timeline.setDuration(max(1, int(duration_ms)))
        self._timeline.setUpdateInterval(max(1, int(step_ms)))
        self._timeline.start()

    def _step(self, progress):
        if not self.is_active():
            return
        temperature = self._cur_temp + (self._tgt_temp - self._cur_temp) * progress
        brightness = self._cur_bright + (self._tgt_bright - self._cur_bright) * progress
        if DisplayManager.apply(temperature, brightness):
            self._last_temp, self._last_bright = temperature, brightness

    def is_active(self):
        return self._timeline.state() == QTimeLine.Running

    def stop(self):
        self._timeline.stop()


# ─────────────────────────────────────────────
# 🖥  SuperDimOverlay —— 软件超暗遮罩（多屏版）(#5)
# ─────────────────────────────────────────────
class SuperDimOverlay(QWidget):
    """单屏超暗遮罩，鼠标穿透。由 DimManager 统一管理多实例。"""
    def __init__(self, screen_geometry):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint |
            Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._alpha = 80
        self.setGeometry(screen_geometry)

    def set_alpha(self, alpha: int):
        self._alpha = max(0, min(200, alpha))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, self._alpha))


class DimManager:
    """管理所有屏幕的超暗遮罩，支持热插拔。"""
    def __init__(self):
        self._overlays: list[SuperDimOverlay] = []
        self._alpha = 80
        self._active = False

    def show(self, alpha: int):
        self._active = True
        self._alpha = max(0, min(200, int(alpha)))
        self._rebuild()

    def hide(self):
        self._active = False
        for ov in self._overlays:
            ov.hide()

    def set_alpha(self, alpha: int):
        self._alpha = max(0, min(200, int(alpha)))
        for ov in self._overlays:
            ov.set_alpha(self._alpha)

    def rebuild(self):
        """屏幕数量变化时重建（热插拔）。"""
        if self._active:
            self._rebuild()

    def _rebuild(self):
        # 清除旧实例
        for ov in self._overlays:
            ov.close()
        self._overlays.clear()
        # 为每块屏幕创建独立遮罩
        for screen in QApplication.screens():
            ov = SuperDimOverlay(screen.geometry())
            ov.set_alpha(self._alpha)
            ov.show()
            self._overlays.append(ov)


# ─────────────────────────────────────────────
# 👁  EyeExerciseOverlay —— 视力训练窗口
#     #7: 关闭时停球定时器  #9: 前10秒锁定跳过
# ─────────────────────────────────────────────
class EyeExerciseOverlay(QWidget):
    closed = pyqtSignal()

    def __init__(self, duration_secs=20, force_mode=False):
        super().__init__()
        self.total = duration_secs
        self.remaining = duration_secs
        self.force_mode = force_mode          # #9 强制模式
        self._lock_secs = 10 if force_mode else 0
        started = time.monotonic()
        self._deadline = started + duration_secs
        self._unlock_deadline = started + min(self._lock_secs, duration_secs)
        self._can_close = False
        self._closed_emitted = False
        self._cancelled = False
        self.completed = False
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.showFullScreen()
        self._ball_angle = 0.0
        self._build_ui()
        self._cd = QTimer(self)
        self._cd.timeout.connect(self._tick)
        self._cd.start(1000)
        self._bt = QTimer(self)               # #7 球动画仅在窗口存在时运行
        self._bt.timeout.connect(self._move_ball)
        self._bt.start(16)

    @property
    def _cx(self): return self.width() // 2
    @property
    def _cy(self): return self.height() // 2
    @property
    def _rx(self): return min(320, self.width() // 3)
    @property
    def _ry(self): return min(160, self.height() // 5)

    @property
    def _ball_xy(self):
        x = self._cx + self._rx * math.cos(self._ball_angle)
        y = self._cy + self._ry * math.sin(self._ball_angle)
        return int(x), int(y)

    def _move_ball(self):
        self._ball_angle += 0.025
        bx, by = self._ball_xy
        self._ball.move(bx - 18, by - 18)

    def _build_ui(self):
        bg = QFrame(self)
        bg.setStyleSheet("background: rgba(2,8,20,215);")
        bg.setGeometry(self.rect())

        def lbl(text, style, parent=self):
            l = QLabel(text, parent)
            l.setStyleSheet(style)
            l.adjustSize()
            return l

        title = lbl("眼睛休息时间",
                    "color:#e2e8f0;font-size:44px;font-weight:800;letter-spacing:3px;")
        title.move(self._cx - title.width()//2, self._cy - 230)

        sub = lbl("请跟随小球缓慢转动眼球，放松睫状肌",
                  "color:#64748b;font-size:17px;")
        sub.move(self._cx - sub.width()//2, self._cy - 168)

        self._timer_lbl = lbl(str(self.remaining),
                              "color:#0ea5e9;font-size:72px;font-weight:900;")
        self._timer_lbl.move(self._cx - self._timer_lbl.width()//2, self._cy + 110)

        self._sec_lbl = lbl("秒后自动结束", "color:#475569;font-size:15px;")
        self._sec_lbl.move(self._cx - self._sec_lbl.width()//2, self._cy + 195)

        self._ball = QFrame(self)
        self._ball.setFixedSize(36, 36)
        self._ball.setStyleSheet("background:#0ea5e9;border-radius:18px;")
        eff = QGraphicsDropShadowEffect()
        eff.setBlurRadius(28); eff.setColor(QColor("#0ea5e9")); eff.setOffset(0, 0)
        self._ball.setGraphicsEffect(eff)

        # #9 跳过按钮 — 强制模式下前10秒禁用
        self._skip = QPushButton("跳过", self)
        self._skip.setFixedSize(100, 38)
        self._skip.setStyleSheet("""
            QPushButton {
                background:rgba(255,255,255,0.07); color:#64748b;
                border:1px solid rgba(255,255,255,0.1); border-radius:19px; font-size:14px;
            }
            QPushButton:hover { background:rgba(255,255,255,0.14); color:#e2e8f0; }
            QPushButton:disabled { color:#2d3748; border-color:rgba(255,255,255,0.04); }
        """)
        self._skip.move(self._cx - 50, self.height() - 80)
        self._skip.clicked.connect(self._close)
        if self.force_mode:
            self._skip.setEnabled(False)
            self._lock_lbl = lbl(f"强制休息中，{self._lock_secs}秒后可跳过",
                                 "color:#f97316;font-size:13px;")
            self._lock_lbl.move(self._cx - self._lock_lbl.width()//2, self.height() - 120)
        else:
            self._lock_lbl = None

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(14, 165, 233, 28), 2, Qt.DashLine)
        p.setPen(pen)
        p.drawEllipse(self._cx - self._rx, self._cy - self._ry,
                      self._rx * 2, self._ry * 2)

    def _sync_remaining(self):
        now = time.monotonic()
        self.remaining = max(0, math.ceil(self._deadline - now))
        self._lock_secs = max(0, math.ceil(self._unlock_deadline - now))

    def _tick(self):
        self._sync_remaining()
        # #9 解锁逻辑
        if self.force_mode:
            if self._lock_secs <= 0:
                self._skip.setEnabled(True)
                if self._lock_lbl:
                    self._lock_lbl.setText("现在可以跳过")
            elif self._lock_lbl:
                self._lock_lbl.setText(f"强制休息中，{self._lock_secs}秒后可跳过")

        self._timer_lbl.setText(str(self.remaining))
        self._timer_lbl.adjustSize()
        self._timer_lbl.move(self._cx - self._timer_lbl.width()//2, self._cy + 110)
        if self.remaining <= 0:
            self._close()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape, Qt.Key_Q):
            self._close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # 强制休息模式下，前 10 秒拦截 Alt+F4/窗口管理器关闭。
        self._sync_remaining()
        app = QApplication.instance()
        shutting_down = bool(app and app.closingDown())
        if (self.force_mode and self._lock_secs > 0 and self.remaining > 0
                and not self._can_close and not shutting_down):
            event.ignore()
            return
        self._cd.stop()
        self._bt.stop()
        event.accept()
        if not self._closed_emitted:
            self._closed_emitted = True
            self.completed = self.remaining == 0 and not self._cancelled and not shutting_down
            self.closed.emit()

    def cancel(self):
        self._cancelled = True
        self._can_close = True
        self.close()

    def _close(self):
        self._sync_remaining()
        if self.force_mode and self._lock_secs > 0 and self.remaining > 0:
            return
        self._can_close = True
        self._cd.stop()
        self._bt.stop()   # #7 确保球动画定时器停止
        self.close()


# ─────────────────────────────────────────────
# 🎨  自定义控件
# ─────────────────────────────────────────────
class AnimatedToggle(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(52, 26)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0ea5e9") if self.isChecked() else QColor("#374151"))
        p.drawRoundedRect(0, 0, self.width(), self.height(), 13, 13)
        x = self.width() - 22 if self.isChecked() else 4
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(x, 3, 19, 19)


class RingProgress(QWidget):
    def __init__(self, size=110, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._v = 0; self._sz = size

    def setValue(self, v):
        self._v = max(0, min(100, v)); self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = 7
        rect = QRect(m, m, self._sz - 2*m, self._sz - 2*m)
        p.setPen(Qt.NoPen); p.setBrush(QColor("#161b22"))
        p.drawEllipse(rect)
        pen = QPen(QColor("#0ea5e9"), 6, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawArc(rect, 90*16, int(-self._v * 360 / 100 * 16))
        p.setPen(QColor("#e6edf3"))
        p.setFont(QFont("Segoe UI", max(8, int(self._sz * 0.12)), QFont.Bold))
        p.drawText(rect, Qt.AlignCenter, f"{self._v}%")


class BarChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self._data = [0]*7; self._labels = []

    def set_data(self, data, labels):
        self._data = data[-7:]; self._labels = labels[-7:]; self.update()

    def paintEvent(self, event):
        if not self._data: return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        n = len(self._data)
        bar_w  = max(10, (w - 20) // n - 6)
        spacing = (w - bar_w * n) // (n + 1)
        max_h  = h - 32
        max_val = max(480, max(self._data) if self._data else 1)
        for i, val in enumerate(self._data):
            x = spacing + i * (bar_w + spacing)
            bh = max(2, int(val / max_val * max_h))
            y  = max_h - bh + 4
            color = QColor("#0ea5e9") if i == n-1 else QColor("#1e4a6e")
            p.setPen(Qt.NoPen); p.setBrush(color)
            p.drawRoundedRect(x, y, bar_w, bh, 3, 3)
            p.setPen(QColor("#484f58"))
            p.setFont(QFont("Segoe UI", 9))
            lbl = self._labels[i] if i < len(self._labels) else ""
            p.drawText(x, h - 14, bar_w, 14, Qt.AlignCenter, lbl)


# ─────────────────────────────────────────────
# 📱  主应用
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# 🐾  DesktopPet —— 护眼桌宠
# ─────────────────────────────────────────────
class DesktopPet(QWidget):
    """纯 QPainter 绘制的桌面宠物，无额外依赖。

    左键戳一下有反应，拖动移动并自动贴边；右键出菜单；
    外观跟随主程序状态：正常 / 快到休息 / 休息中 / 护眼关闭。
    """
    W, H = 150, 168
    BODY_TOP = 36
    ART_TOP = {
        "blue_cat": -9, "orange_fox": -12, "mint_bunny": -31,
        "purple_owl": -4, "pink_poodle": 2, "charcoal_cat": -9,
        "seagull": -7, "cream_cat": -13, "pixel_robot": -22,
        "capybara": -4, "red_panda": -8, "penguin": -5,
    }

    DEFAULT_PET_KIND = "blue_cat"
    FEATURED_PETS = ("capybara", "red_panda", "penguin")
    DEFAULT_DECORATION = "scarf"

    # 参考 momo-soft-play 的四种软体玩法；额外保留轻戳模式，方便
    # 想维持旧版“点一下就说话”行为的用户。模式只影响桌宠本身，
    # 不改变窗口拖动、护眼计时或托盘逻辑。
    # 默认不强加互动手势，保持传统桌宠的左键拖动体验。
    DEFAULT_INTERACTION_MODE = "move"
    INTERACTION_MODES = {
        "move": {
            "label": "自由移动", "hint": "左键按住拖动，调整桌宠位置",
            "color": "#7dd3fc",
        },
        "squish": {
            "label": "捏一捏",
            "hint": "按住桌宠，它会软软回弹",
            "color": "#60d8ce",
        },
        "stretch": {
            "label": "拉长长",
            "hint": "拖动桌宠，感受弹性拉伸",
            "color": "#8ec5ff",
        },
        "tickle": {
            "label": "挠痒痒",
            "hint": "左右轻拖，看看它笑出小星星",
            "color": "#f6c86f",
        },
        "toss": {
            "label": "抛一下",
            "hint": "拖动后松手，让它弹一下",
            "color": "#c4a7ff",
        },
        "poke": {
            "label": "轻戳",
            "hint": "点击一下，触发一个小反应",
            "color": "#f39ab4",
        },
    }

    INTERACTION_PHRASES = {
        "squish": (
            "嘿，被你捏成一小团了",
            "慢慢捏，我会自己弹回来",
            "压力交给我，快乐还给你",
        ),
        "stretch": (
            "再长一点点也没关系",
            "拉——长——一小团快乐",
            "我的弹性还在线呢",
        ),
        "tickle": (
            "哈哈哈，那里真的很痒",
            "停一下，我要笑出星星了",
            "你挠得我尾巴都在抖",
        ),
        "toss": (
            "接住我，我要起飞啦",
            "轻轻一抛，快乐就起飞",
            "落地也要软乎乎的",
        ),
        "poke": (
            "被你戳到啦",
            "我在这儿，收到你的信号",
            "轻一点，我会害羞的",
        ),
    }

    DECORATIONS = {
        "scarf": {
            "label": "薄荷围巾", "symbol": "⌁", "color": "#60d8ce",
        },
        "sprout": {
            "label": "头顶小芽", "symbol": "❧", "color": "#9bd7a6",
        },
        "round_glasses": {
            "label": "圆框眼镜", "symbol": "◎", "color": "#8fc6d8",
        },
        "star_pin": {
            "label": "星星别针", "symbol": "★", "color": "#e6c878",
        },
        "heart_badge": {
            "label": "爱心徽章", "symbol": "♥", "color": "#ef8fa3",
        },
        "night_cap": {
            "label": "晚安帽", "symbol": "☾", "color": "#a8b2d8",
        },
        "moon_charm": {
            "label": "月光吊坠", "symbol": "☾", "color": "#d8c98f",
        },
        "tiny_crown": {
            "label": "星光小冠", "symbol": "♛", "color": "#f0cf78",
        },
    }

    @staticmethod
    def _make_palette(primary, highlight, belly):
        base = QColor(primary)
        return {
            "idle": (primary, highlight, belly),
            "tired": (base.darker(105).name(), highlight, belly),
            "resting": (base.darker(112).name(), highlight, belly),
            "off": ("#7b8994", "#a2aeb6", "#dfe6ea"),
        }

    PET_STYLES = {
        "blue_cat": {
            "label": "蓝猫",
            "tagline": "元气在线，提醒你别盯太久。",
            "palette": _make_palette("#8cbdd9", "#bcdcec", "#eff7fb"),
            "ears": "cat",
            "tail": "cat",
            "renderer": "blue_cat",
        },
        "orange_fox": {
            "label": "橘狐",
            "tagline": "机灵守时，到点就催你歇会儿。",
            "palette": _make_palette("#e6a16b", "#f2c79e", "#fff2df"),
            "ears": "pointed",
            "tail": "cat",
            "renderer": "orange_fox",
        },
        "mint_bunny": {
            "label": "薄荷兔",
            "tagline": "清清爽爽，陪你把节奏慢下来。",
            "palette": _make_palette("#9bcfbd", "#c5e8d8", "#f1faf3"),
            "ears": "tall",
            "tail": "puff",
            "renderer": "mint_bunny",
        },
        "purple_owl": {
            "label": "紫鸮",
            "tagline": "夜里也盯着你，别拿熬夜当本事。",
            "palette": _make_palette("#b1a0d2", "#d8cbed", "#f7f1fb"),
            "ears": "round",
            "tail": "wing",
            "renderer": "purple_owl",
        },
        "pink_poodle": {
            "label": "粉贵宾",
            "tagline": "软乎归软乎，休息时间一点不让。",
            "palette": _make_palette("#dba9bd", "#edcfdd", "#fff3f5"),
            "ears": "round",
            "tail": "puff",
            "renderer": "pink_poodle",
        },
        "charcoal_cat": {
            "label": "夜行猫",
            "tagline": "安静待命，护眼关闭也会提醒你。",
            "palette": _make_palette("#526477", "#8396ab", "#dce6ef"),
            "ears": "cat",
            "tail": "cat",
            "renderer": "charcoal_cat",
        },
        "seagull": {
            "label": "小海鸥",
            "tagline": "不催你努力，只提醒你歇会儿。",
            "renderer": "seagull",
            "palette": {
                "idle": ("#f1f6f9", "#d4e2ec", "#ffffff"),
                "tired": ("#e5edf2", "#c5d5e1", "#f8fafc"),
                "resting": ("#d9e4eb", "#c5d5df", "#f1f5f9"),
                "off": ("#85939e", "#a5b3bd", "#dfe6ea"),
            },
        },
        "cream_cat": {
            "label": "奶油猫",
            "tagline": "暖乎乎陪着你，忙完记得眨眨眼。",
            "renderer": "cream_cat",
            "palette": _make_palette("#edc9a0", "#ca996d", "#fff3e1"),
        },
        "pixel_robot": {
            "label": "像素机器人",
            "tagline": "精准计时，休息指令从不掉线。",
            "renderer": "pixel_robot",
            "palette": {
                "idle": ("#add9ec", "#527d99", "#213b52"),
                "tired": ("#98c6da", "#496f89", "#1e374d"),
                "resting": ("#789db2", "#a3acc8", "#26334d"),
                "off": ("#64748b", "#475569", "#1e293b"),
            },
        },
        "capybara": {
            "label": "焦糖水豚",
            "tagline": "慢慢来，发会儿呆也很不错。",
            "renderer": "capybara",
            "palette": _make_palette("#c5a078", "#a47c58", "#efdbc0"),
        },
        "red_panda": {
            "label": "枫叶小熊猫",
            "tagline": "把忙碌放下，和我伸个懒腰。",
            "renderer": "red_panda",
            "palette": _make_palette("#d58c62", "#865347", "#fff0da"),
        },
        "penguin": {
            "label": "雪团企鹅",
            "tagline": "摇摇摆摆，陪你走一小段。",
            "renderer": "penguin",
            "palette": _make_palette("#7899b6", "#526f8e", "#f0f6fc"),
        },
    }

    TIPS = {
        "idle":    ["今天也要好好护眼", "记得多眨眨眼", "坐直一点，别驼背",
                    "喝口水吧", "我在这儿陪着你", "看看远处，放松一下"],
        "tired":   ["快到休息时间啦", "眼睛有点酸了呢"],
        "resting": ["闭上眼睛，放松一会儿", "看看窗外的远处"],
        "off":     ["护眼已关闭，别熬太久"],
    }

    def __init__(self, open_app=None, hide_pet=None, quit_app=None,
                 rest_now=None, toggle_care=None, on_moved=None,
                 pet_kind=DEFAULT_PET_KIND, decoration=DEFAULT_DECORATION,
                 interaction_mode=DEFAULT_INTERACTION_MODE,
                 on_interaction_mode_changed=None, outfit=None):
        super().__init__(None)
        self._open_app    = open_app
        self._hide_pet    = hide_pet
        self._quit_app    = quit_app
        self._rest_now    = rest_now
        self._toggle_care = toggle_care
        self._on_moved    = on_moved
        self._on_interaction_mode_changed = on_interaction_mode_changed
        self._pet_kind = pet_kind if pet_kind in self.PET_STYLES else self.DEFAULT_PET_KIND
        self._outfit = ()
        self.set_outfit((decoration,) if outfit is None else outfit)
        self._interaction_mode = self.normalize_interaction_mode(interaction_mode)

        self._state = "idle"
        self._phase = 0.0
        self._blink = 0             # 剩余眨眼帧
        self._next_blink = 40
        self._squash = 0.0          # 被戳时的挤压动画
        self._drag_from = None
        self._dragged = False
        self._msg = ""
        self._tip_idx = 0
        self._left_secs = 0
        self._total_secs = 0
        self._look = (0.0, 0.0)     # 眼球偏移
        self._init_interaction_state()

        self.setFixedSize(self.W, self.H)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint |
                            Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setCursor(Qt.PointingHandCursor)
        self._update_tooltip()

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.setInterval(60)
        self._msg_timer = QTimer(self)
        self._msg_timer.setSingleShot(True)
        self._msg_timer.timeout.connect(self._clear_msg)
        self._chat_timer = QTimer(self)          # 偶尔自己冒个泡
        self._chat_timer.timeout.connect(self._auto_chat)
        self._chat_timer.setInterval(120_000)

    def showEvent(self, event):
        super().showEvent(event)
        self._anim_timer.start()
        self._chat_timer.start()
        if self._msg:
            self._msg_timer.start()

    def hideEvent(self, event):
        self._anim_timer.stop()
        self._chat_timer.stop()
        self._msg_timer.stop()
        self._clear_msg()
        super().hideEvent(event)

    @classmethod
    def normalize_interaction_mode(cls, mode):
        """返回可用互动模式，旧配置或脏数据统一回落到默认值。"""
        return mode if mode in cls.INTERACTION_MODES else cls.DEFAULT_INTERACTION_MODE

    @staticmethod
    def _neutral_interaction_values():
        return {
            "x": 0.0,
            "y": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "rotation": 0.0,
            "bend": 0.0,
            "press": 0.0,
            "wobble": 0.0,
        }

    def _init_interaction_state(self):
        """初始化与窗口位置无关的软体动画状态，预览组件也可复用。"""
        self._interaction_current = self._neutral_interaction_values()
        self._interaction_target = self._neutral_interaction_values()
        self._interaction_velocity = {
            key: 0.0 for key in self._interaction_current
        }
        self._interaction_active = False
        self._interaction_announced = False
        self._interaction_start = (self.W / 2, self.BODY_TOP + 48)
        self._interaction_global_start = self._interaction_start
        self._interaction_last_global = self._interaction_start
        self._interaction_pointer = self._interaction_start
        self._interaction_last_time = time.monotonic()
        self._pointer_velocity = (0.0, 0.0)
        self._last_particle_at = 0.0
        self._toss_active = False
        self._toss_velocity = [0.0, 0.0]
        self._toss_elapsed = 0.0
        self._last_bounce_at = 0.0
        self._particles = []
        self._ripples = []
        self._hovering = False
        self._interaction_count = 0
        self._surprise_kind = None
        self._surprise_elapsed = 0.0
        self._surprise_duration = 0.0
        self._surprise_index = 0
        self._surprise_burst_done = False
        self._window_dragging = False

    @property
    def interaction_mode(self):
        return self._interaction_mode

    @interaction_mode.setter
    def interaction_mode(self, mode):
        self.set_interaction_mode(mode)

    def _update_tooltip(self):
        info = self.INTERACTION_MODES[self._interaction_mode]
        countdown = ""
        if getattr(self, "_total_secs", 0):
            minutes, seconds = divmod(max(0, self._left_secs), 60)
            countdown = f"距离下次休息 {minutes:02d}:{seconds:02d}\n"
        self.setToolTip(
            f"{countdown}玩法：{info['label']} · "
            f"{'左键拖动移动' if self._interaction_mode == 'move' else '左键玩耍 · Shift+拖动移动'} · "
            "双击惊喜 · Ctrl+双击主界面 · 右键菜单"
        )

    def set_interaction_mode(self, mode):
        """切换玩法并让当前形变平滑回到中性状态。"""
        mode = self.normalize_interaction_mode(mode)
        if mode == self._interaction_mode:
            self._update_tooltip()
            return False
        self.cancel_interaction()
        self._interaction_mode = mode
        self._update_tooltip()
        self._spawn_particles("spark", 5)
        if hasattr(self, "_msg_timer"):
            self.say(f"切到{self.INTERACTION_MODES[mode]['label']}模式啦", 2200)
        callback = getattr(self, "_on_interaction_mode_changed", None)
        if callback is not None:
            callback(mode)
        self.update()
        return True

    def _set_interaction_target(self, **values):
        for key, value in values.items():
            if key in self._interaction_target:
                self._interaction_target[key] = float(value)

    def _neutral_interaction(self):
        self._interaction_target.update(self._neutral_interaction_values())

    def cancel_interaction(self, immediate=False):
        """终止抓取、抛掷或惊喜动画；用于模式切换和失焦恢复。"""
        self._interaction_active = False
        self._interaction_announced = False
        self._toss_active = False
        self._toss_velocity[:] = (0.0, 0.0)
        self._surprise_kind = None
        self._neutral_interaction()
        if immediate:
            self._interaction_current.update(self._neutral_interaction_values())
            for key in self._interaction_velocity:
                self._interaction_velocity[key] = 0.0
        self.update()

    def _interaction_phrase(self):
        phrases = self.INTERACTION_PHRASES[self._interaction_mode]
        index = self._interaction_count % len(phrases)
        self._interaction_count += 1
        return phrases[index]

    def _announce_interaction(self):
        if self._interaction_announced:
            return
        self._interaction_announced = True
        if hasattr(self, "_msg_timer"):
            self.say(self._interaction_phrase(), 2800)

    def _begin_interaction(self, local_x, local_y, global_x=None, global_y=None):
        now = time.monotonic()
        gx = float(local_x if global_x is None else global_x)
        gy = float(local_y if global_y is None else global_y)
        self._interaction_active = True
        self._interaction_announced = False
        self._interaction_start = (float(local_x), float(local_y))
        self._interaction_global_start = (gx, gy)
        self._interaction_last_global = (gx, gy)
        self._interaction_pointer = (float(local_x), float(local_y))
        self._interaction_last_time = now
        self._pointer_velocity = (0.0, 0.0)
        self._toss_active = False
        self._surprise_kind = None
        self._spawn_ripple(local_x, local_y)

        mode = self._interaction_mode
        if mode == "squish":
            self._set_interaction_target(scale_x=1.16, scale_y=.76,
                                         y=5, press=1.0, wobble=.08)
        elif mode == "stretch":
            self._set_interaction_target(scale_x=.98, scale_y=1.08,
                                         press=.35)
        elif mode == "tickle":
            self._set_interaction_target(scale_x=1.03, scale_y=.96,
                                         press=.22, wobble=.6)
        elif mode == "toss":
            self._set_interaction_target(scale_x=.96, scale_y=1.05,
                                         press=.18)
        else:
            self._set_interaction_target(scale_x=1.08, scale_y=.90,
                                         y=4, press=.55)
        self._blink = max(self._blink, 2)

    def _update_drag_interaction(self, local_x, local_y, global_x, global_y):
        if not self._interaction_active:
            return
        now = time.monotonic()
        dt = max(.008, min(.12, now - self._interaction_last_time))
        gx, gy = float(global_x), float(global_y)
        last_x, last_y = self._interaction_last_global
        velocity_x = (gx - last_x) / dt
        velocity_y = (gy - last_y) / dt
        # 低通一下，避免低频鼠标事件造成释放速度暴跳。
        old_vx, old_vy = self._pointer_velocity
        self._pointer_velocity = (
            old_vx * .45 + velocity_x * .55,
            old_vy * .45 + velocity_y * .55,
        )
        self._interaction_last_global = (gx, gy)
        self._interaction_last_time = now
        self._interaction_pointer = (float(local_x), float(local_y))
        start_x, start_y = self._interaction_global_start
        dx, dy = gx - start_x, gy - start_y
        distance = math.hypot(dx, dy)
        if distance > 7:
            self._announce_interaction()

        mode = self._interaction_mode
        if mode == "stretch":
            horizontal = min(.45, abs(dx) / 115.0)
            vertical = min(.42, abs(dy) / 105.0)
            self._set_interaction_target(
                x=max(-18, min(18, dx * .16)),
                y=max(-15, min(16, dy * .14)),
                scale_x=max(.82, 1 + horizontal - vertical * .20),
                scale_y=max(.72, 1 + vertical - horizontal * .20),
                rotation=max(-.22, min(.22, dx * .0022)),
                bend=max(-18, min(18, dx * .08)),
                press=.32,
            )
        elif mode == "tickle":
            speed = math.hypot(*self._pointer_velocity)
            wobble = max(.35, min(2.4, speed / 180.0))
            self._set_interaction_target(
                x=max(-10, min(10, dx * .10)),
                y=max(-7, min(7, dy * .08)),
                rotation=math.sin(self._phase * 8.0) * .055,
                scale_x=1.04,
                scale_y=.95,
                bend=max(-9, min(9, dx * .04)),
                press=.22,
                wobble=wobble,
            )
            if speed > 75 and now - self._last_particle_at > .11:
                self._spawn_particles("star", 2, (local_x, local_y))
                self._last_particle_at = now
        elif mode == "toss":
            speed_y = min(.14, abs(self._pointer_velocity[1]) / 2600.0)
            self._set_interaction_target(
                x=max(-34, min(34, dx * .48)),
                y=max(-27, min(28, dy * .42)),
                rotation=max(-.32, min(.32, dx * .0035)),
                scale_x=max(.84, 1.0 - speed_y),
                scale_y=1.0 + speed_y,
                bend=max(-12, min(12, dx * .05)),
                press=.12,
            )
        elif mode == "squish":
            pressure = min(.42, .24 + distance / 260.0)
            self._set_interaction_target(
                x=max(-10, min(10, dx * .08)),
                y=max(2, min(11, 5 + dy * .07)),
                scale_x=1.0 + pressure * .62,
                scale_y=max(.58, 1.0 - pressure),
                rotation=max(-.16, min(.16, dx * .0018)),
                bend=max(-11, min(11, dx * .055)),
                press=1.0,
                wobble=.10,
            )
        else:
            self._set_interaction_target(
                x=max(-7, min(7, dx * .06)),
                y=max(-5, min(7, dy * .06)),
                scale_x=1.08,
                scale_y=.90,
                rotation=max(-.10, min(.10, dx * .0015)),
                press=.55,
            )

    def _start_toss(self, velocity_x, velocity_y, force=False):
        if not force and math.hypot(velocity_x, velocity_y) < 85:
            velocity_x, velocity_y = 0.0, -290.0
        self._toss_active = True
        self._toss_elapsed = 0.0
        self._toss_velocity[:] = (
            max(-520.0, min(520.0, velocity_x * .48)),
            max(-640.0, min(430.0, velocity_y * .48)),
        )
        if abs(self._toss_velocity[0]) + abs(self._toss_velocity[1]) < 70:
            self._toss_velocity[1] = -290.0
        self._set_interaction_target(press=0, scale_x=1, scale_y=1)
        self._spawn_particles("bubble", 5)

    def _finish_interaction(self, was_dragged=False, cancelled=False):
        if not self._interaction_active and not self._toss_active:
            return
        self._interaction_active = False
        if cancelled:
            self._neutral_interaction()
            return
        velocity_x, velocity_y = self._pointer_velocity
        if not was_dragged:
            self._poke()
            return
        self._announce_interaction()
        if self._interaction_mode == "toss":
            self._start_toss(velocity_x, velocity_y)
        else:
            self._neutral_interaction()
            # 给弹簧一个反向冲量，让松手不是机械地线性复位。
            self._interaction_velocity["scale_x"] -= .65
            self._interaction_velocity["scale_y"] += .75
            self._interaction_velocity["rotation"] -= velocity_x * .00035
            effect = "heart" if self._interaction_mode == "squish" else "star"
            count = 7 if self._interaction_mode == "tickle" else 4
            self._spawn_particles(effect, count)

    def trigger_surprise(self, kind=None):
        """触发双击彩蛋；kind 主要用于自动化测试和菜单的确定性调用。"""
        kinds = ("happy", "sneeze", "sleep")
        if kind not in kinds:
            kind = kinds[self._surprise_index % len(kinds)]
            self._surprise_index += 1
        self.cancel_interaction()
        self._surprise_kind = kind
        self._surprise_elapsed = 0.0
        self._surprise_duration = {
            "happy": .95, "sneeze": .90, "sleep": 2.35,
        }[kind]
        self._surprise_burst_done = False
        messages = {
            "happy": "噔噔，送你一颗小太阳",
            "sneeze": "阿——嚏！可爱也会打喷嚏",
            "sleep": "我先融化一会儿，你也歇一下",
        }
        if hasattr(self, "_msg_timer"):
            self.say(messages[kind], 3200 if kind != "sleep" else 4200)
        if kind == "happy":
            self._spawn_particles("heart", 13, (self.W / 2, self.BODY_TOP + 42))
        elif kind == "sleep":
            self._spawn_particles("bubble", 5, (self.W * .72, self.BODY_TOP + 18))
        self.update()
        return kind

    def _tick_surprise(self, dt):
        if not self._surprise_kind:
            return
        self._surprise_elapsed += dt
        elapsed = self._surprise_elapsed
        if self._surprise_kind == "happy":
            jump = -abs(math.sin(min(1.0, elapsed / .78) * math.pi)) * 16
            self._set_interaction_target(
                y=jump, scale_x=.94, scale_y=1.09,
                rotation=math.sin(elapsed * 15) * .045, wobble=.45,
            )
        elif self._surprise_kind == "sneeze":
            if elapsed < .28:
                self._set_interaction_target(
                    scale_x=.86, scale_y=1.14, rotation=-.05, y=-2,
                    press=.25,
                )
            elif elapsed < .52:
                self._set_interaction_target(
                    scale_x=1.23, scale_y=.76, rotation=.08, y=7,
                    wobble=1.5, press=.65,
                )
                if not self._surprise_burst_done:
                    self._spawn_particles("star", 11,
                                          (self.W * .70, self.BODY_TOP + 42))
                    self._surprise_burst_done = True
            else:
                self._neutral_interaction()
        else:  # sleep
            self._set_interaction_target(
                y=8, scale_x=1.18, scale_y=.72, rotation=-.025,
                press=.22, wobble=0,
            )
            self._blink = max(self._blink, 2)

        if elapsed >= self._surprise_duration:
            self._surprise_kind = None
            self._neutral_interaction()

    def _tick_interaction(self, dt=.06):
        self._tick_surprise(dt)

        if self._toss_active:
            self._toss_elapsed += dt
            velocity_x, velocity_y = self._toss_velocity
            velocity_y += 720.0 * dt
            x = self._interaction_target["x"] + velocity_x * dt
            y = self._interaction_target["y"] + velocity_y * dt
            bounced = False
            if x < -35 or x > 35:
                x = max(-35, min(35, x))
                velocity_x *= -.62
                bounced = True
            if y < -31:
                y = -31
                velocity_y = abs(velocity_y) * .54
                bounced = True
            elif y > 28:
                y = 28
                velocity_y *= -.56
                velocity_x *= .72
                bounced = True
                self._interaction_velocity["scale_x"] += .85
                self._interaction_velocity["scale_y"] -= .95
            self._toss_velocity[:] = (velocity_x, velocity_y)
            self._set_interaction_target(
                x=x, y=y,
                rotation=max(-.34, min(.34, velocity_x * .00055)),
                scale_x=max(.88, min(1.14, 1 - abs(velocity_y) * .00015)),
                scale_y=max(.88, min(1.16, 1 + abs(velocity_y) * .00017)),
            )
            if bounced and self._toss_elapsed - self._last_bounce_at > .18:
                self._spawn_ripple(self.W / 2 + x,
                                   self.BODY_TOP + 92 + min(18, y))
                self._last_bounce_at = self._toss_elapsed
            if (self._toss_elapsed > 2.1 or
                    (self._toss_elapsed > .75 and
                     abs(velocity_x) + abs(velocity_y) < 95)):
                self._toss_active = False
                self._neutral_interaction()

        # 阻尼弹簧：每个形变维度独立回弹，避免突然跳回原形。
        stiffness = 34.0 if self._interaction_mode == "squish" else 42.0
        damping = 9.5
        for key, current in tuple(self._interaction_current.items()):
            target = self._interaction_target[key]
            velocity = self._interaction_velocity[key]
            velocity += ((target - current) * stiffness - velocity * damping) * dt
            current += velocity * dt
            if abs(target - current) < .0005 and abs(velocity) < .003:
                current, velocity = target, 0.0
            self._interaction_current[key] = current
            self._interaction_velocity[key] = velocity

        self._interaction_current["scale_x"] = max(
            .55, min(1.55, self._interaction_current["scale_x"])
        )
        self._interaction_current["scale_y"] = max(
            .55, min(1.55, self._interaction_current["scale_y"])
        )
        self._interaction_current["rotation"] = max(
            -.45, min(.45, self._interaction_current["rotation"])
        )

        for particle in self._particles:
            particle["life"] -= dt
            particle["x"] += particle["vx"] * dt
            particle["y"] += particle["vy"] * dt
            particle["vy"] += 34.0 * dt
            particle["rotation"] += particle["spin"] * dt
        self._particles = [p for p in self._particles if p["life"] > 0]

        for ripple in self._ripples:
            ripple["life"] -= dt
            ripple["radius"] += 24.0 * dt
        self._ripples = [r for r in self._ripples if r["life"] > 0]

    def _spawn_particles(self, kind="spark", count=5, origin=None):
        if not hasattr(self, "_particles"):
            return
        origin = origin or (self.W / 2, self.BODY_TOP + 45)
        color = self.INTERACTION_MODES[self._interaction_mode]["color"]
        for index in range(max(0, int(count))):
            angle = random.uniform(-math.pi * .88, -math.pi * .12)
            speed = random.uniform(24.0, 66.0)
            self._particles.append({
                "x": float(origin[0]) + random.uniform(-4, 4),
                "y": float(origin[1]) + random.uniform(-3, 3),
                "vx": math.cos(angle) * speed,
                "vy": math.sin(angle) * speed,
                "life": random.uniform(.62, 1.05),
                "max_life": 1.05,
                "size": random.uniform(2.5, 5.0),
                "kind": kind if kind in ("spark", "star", "heart", "bubble")
                        else ("star" if index % 2 else "bubble"),
                "color": color,
                "rotation": random.uniform(-.5, .5),
                "spin": random.uniform(-2.2, 2.2),
            })
        if len(self._particles) > 54:
            self._particles = self._particles[-54:]

    def _spawn_ripple(self, x, y):
        if not hasattr(self, "_ripples"):
            return
        self._ripples.append({
            "x": float(x), "y": float(y), "radius": 4.0,
            "life": .55, "max_life": .55,
        })
        self._ripples = self._ripples[-6:]

    # ── 位置 ──────────────────────────────────
    def place(self, pos=None):
        """恢复上次的位置；没有记录或已越界则贴到右下角。"""
        if pos and len(pos) == 2 and self._on_any_screen(pos[0], pos[1]):
            self.move(int(pos[0]), int(pos[1]))
            return
        scr = QApplication.primaryScreen()
        if scr:
            a = scr.availableGeometry()
            self.move(a.right() - self.width() - 24, a.bottom() - self.height() - 8)

    @staticmethod
    def _on_any_screen(x, y) -> bool:
        probe = QRect(int(x), int(y), 48, 48)
        return any(s.availableGeometry().intersects(probe)
                   for s in QApplication.screens())

    def keep_on_screen(self):
        """显示器热插拔后把桌宠拉回可见区域。"""
        if not self._on_any_screen(self.x(), self.y()):
            self.place(None)

    def _snap_edge(self):
        """拖动结束后限制在屏幕内，靠近左右边缘时自动贴边。"""
        scr = QApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        if not scr:
            return
        a = scr.availableGeometry()
        x = min(max(self.x(), a.left()), a.right() - self.width())
        y = min(max(self.y(), a.top()), a.bottom() - self.height())
        if x - a.left() < 32:
            x = a.left()
        elif a.right() - (x + self.width()) < 32:
            x = a.right() - self.width()
        self.move(x, y)

    # ── 与主程序联动 ──────────────────────────
    def set_state(self, state: str):
        palette = self.PET_STYLES[self._pet_kind]["palette"]
        if state not in palette or state == self._state:
            return
        self._state = state
        if state != "idle":
            self.say(self.TIPS[state][0], 5000)
        self.update()

    def set_pet_kind(self, pet_kind: str):
        if pet_kind not in self.PET_STYLES or pet_kind == self._pet_kind:
            return
        self._pet_kind = pet_kind
        self._tip_idx = 0
        self.say(f"我是{self.PET_STYLES[pet_kind]['label']}，继续陪你护眼")
        self.update()

    def set_decoration(self, decoration: str):
        """显示单件装饰，穿戴权限由成长模型控制。"""
        if decoration not in self.DECORATIONS:
            return
        self.set_outfit((decoration,))

    def set_outfit(self, decorations):
        if not isinstance(decorations, (tuple, list)):
            raise ValueError("outfit must be a list or tuple")
        if any(not isinstance(item, str) or item not in self.DECORATIONS
               for item in decorations):
            raise ValueError("unknown decoration")
        selected = tuple(item for item in self.DECORATIONS if item in decorations)
        if selected != self._outfit:
            self._outfit = selected
            self.update()

    def set_countdown(self, left_secs: int, total_secs: int):
        self._left_secs = max(0, int(left_secs))
        self._total_secs = max(0, int(total_secs))
        self._update_tooltip()
        if self._total_secs:
            self.update()

    def say(self, text: str, ms: int = 3200):
        self._msg = text
        if hasattr(self, "_msg_timer"):
            self._msg_timer.setInterval(ms)
            if self.isVisible():
                self._msg_timer.start()
        self.update()

    def _clear_msg(self):
        self._msg = ""
        self.update()

    def _auto_chat(self):
        tips = self.TIPS.get(self._state, self.TIPS["idle"])
        self._tip_idx = (self._tip_idx + 1) % len(tips)
        self.say(tips[self._tip_idx])

    # ── 动画 ──────────────────────────────────
    def _tick(self):
        self._phase += 0.09
        if self._squash > 0:
            self._squash = max(0.0, self._squash - 0.1)
        self._tick_interaction(.06)
        if self._blink > 0:
            self._blink -= 1
        else:
            self._next_blink -= 1
            if self._next_blink <= 0:
                self._blink = 4
                self._next_blink = 45 + int(abs(math.sin(self._phase * 3)) * 45)
        # 眼球追随鼠标
        d = QCursor.pos() - self.geometry().center()
        self._look = (max(-3.0, min(3.0, d.x() / 55)),
                      max(-2.5, min(2.5, d.y() / 75)))
        self.update()

    # ── 绘制 ──────────────────────────────────
    def paintEvent(self, event):
        p = QPainter(self)
        self._paint_scene(p)

    def _paint_scene(self, p, show_bar=True, show_bubble=True):
        style = self.PET_STYLES[self._pet_kind]
        body, light, belly = style["palette"][self._state]
        p.setRenderHint(QPainter.Antialiasing)
        motion = getattr(self, "_interaction_current", None)
        if motion is None:
            motion = self._neutral_interaction_values()
        bob = math.sin(self._phase) * 3.0 + self._squash * 5
        top = self.BODY_TOP + bob
        p.setPen(Qt.NoPen)

        # 影子与软体位移分开绘制：主体被拉伸或抛起时，脚下仍有落点感。
        shadow_y = 142 + max(0, int(motion["y"] * .22))
        sw = max(44, 92 - int(bob * 2) - int(abs(motion["y"]) * .35))
        shadow = QRadialGradient(.5, .5, .5)
        shadow.setCoordinateMode(QGradient.ObjectBoundingMode)
        shadow.setColorAt(0, QColor(5, 16, 25, 92))
        shadow.setColorAt(.55, QColor(5, 16, 25, 45))
        shadow.setColorAt(1, QColor(5, 16, 25, 0))
        p.setBrush(shadow)
        p.drawEllipse(QRectF(self.W / 2 + motion["x"] * .22 - sw / 2,
                            shadow_y, sw, max(6, 12 - abs(motion["y"]) * .06)))

        # 角色本体使用同一个局部坐标系，所以所有皮肤都能获得一致的
        # squish / stretch / wobble 效果，不需要复制每个 renderer 的路径。
        pivot_x = self.W / 2
        pivot_y = top + 58
        wobble_x = math.sin(self._phase * 10.5) * motion["wobble"] * 1.3
        wobble_y = math.sin(self._phase * 16.0) * motion["wobble"] * .45
        pose = QTransform()
        pose.translate(pivot_x + motion["x"] + wobble_x,
                       pivot_y + motion["y"] + wobble_y + motion["press"] * 1.2)
        pose.rotate(math.degrees(motion["rotation"] + motion["bend"] * .0015))
        pose.scale(motion["scale_x"], motion["scale_y"])
        pose.translate(-pivot_x, -pivot_y)
        art_top = self.ART_TOP.get(self._pet_kind, -13)
        if any(item in self._outfit for item in ("sprout", "night_cap", "tiny_crown")):
            art_top = min(art_top, -30)
        bounds = pose.mapRect(QRectF(20, top + art_top, 123, 113 - art_top))
        fit = min(1.0, (self.W - 8) / max(1, bounds.width()),
                  (self.H - 18) / max(1, bounds.height()))
        half_width, half_height = bounds.width() * fit / 2, bounds.height() * fit / 2
        center_x = max(4 + half_width, min(self.W - 4 - half_width, bounds.center().x()))
        center_y = max(4 + half_height, min(self.H - 14 - half_height, bounds.center().y()))
        p.save()
        p.translate(center_x - bounds.center().x() * fit,
                    center_y - bounds.center().y() * fit)
        p.scale(fit, fit)
        p.setWorldTransform(pose, True)
        renderer = style.get("renderer", "classic")
        if renderer == "seagull":
            self._paint_seagull(p, top, body, light, belly)
        elif renderer == "cream_cat":
            self._paint_cream_cat(p, top, body, light, belly)
        elif renderer == "pixel_robot":
            self._paint_pixel_robot(p, top, body, light, belly)
        elif renderer == "blue_cat":
            self._paint_blue_cat(p, top, body, light, belly)
        elif renderer == "orange_fox":
            self._paint_orange_fox(p, top, body, light, belly)
        elif renderer == "mint_bunny":
            self._paint_mint_bunny(p, top, body, light, belly)
        elif renderer == "purple_owl":
            self._paint_purple_owl(p, top, body, light, belly)
        elif renderer == "pink_poodle":
            self._paint_pink_poodle(p, top, body, light, belly)
        elif renderer == "charcoal_cat":
            self._paint_charcoal_cat(p, top, body, light, belly)
        elif renderer == "capybara":
            self._paint_capybara(p, top, body, light, belly)
        elif renderer == "red_panda":
            self._paint_red_panda(p, top, body, light, belly)
        elif renderer == "penguin":
            self._paint_penguin(p, top, body, light, belly)
        else:
            wag = math.sin(self._phase * 1.7) * 9
            if style["tail"] == "puff":
                p.setBrush(QColor(light))
                p.drawEllipse(112, int(top + 72), 24, 24)
            elif style["tail"] == "wing":
                p.setBrush(QColor(light))
                p.drawEllipse(104, int(top + 65 + wag * 0.3), 34, 17)
            else:
                p.setBrush(QColor(body))
                p.drawEllipse(int(106 + wag * 0.5), int(top + 60), 28, 13)

            self._paint_ears(p, top, light, belly, style["ears"])

            p.setPen(Qt.NoPen)
            p.setBrush(QColor(body))
            p.drawRoundedRect(QRect(28, int(top + 8), 94, 90), 34, 34)
            p.setBrush(QColor(belly))
            p.drawEllipse(39, int(top + 22), 72, 62)
            p.setBrush(QColor(body).darker(112))
            p.drawEllipse(38, int(top + 84), 28, 15)
            p.drawEllipse(84, int(top + 84), 28, 15)

            self._paint_face(p, top)
        for decoration in self._outfit:
            self._paint_decoration(p, top, decoration)
        p.restore()
        self._paint_interaction_fx(p)
        if show_bar:
            self._paint_bar(p)
        if show_bubble:
            self._paint_bubble(p)

    @staticmethod
    def _fill_path(painter, path, fill, stroke=None, width=1.0):
        painter.setBrush(QColor(fill) if isinstance(fill, str) else fill)
        if stroke:
            pen = QPen(QColor(stroke), width)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
        else:
            painter.setPen(Qt.NoPen)
        painter.drawPath(path)

    @staticmethod
    def _pet_ellipse(painter, left, top, width, height, fill, stroke=None):
        painter.setPen(QPen(QColor(stroke), .85) if stroke else Qt.NoPen)
        painter.setBrush(QColor(fill) if isinstance(fill, str) else fill)
        painter.drawEllipse(QRectF(left, top, width, height))

    @staticmethod
    def _pet_gradient(color, top, height=100):
        base = QColor(color)
        gradient = QLinearGradient(42, top, 102, top + height)
        gradient.setColorAt(0, base.lighter(114))
        gradient.setColorAt(.48, base)
        gradient.setColorAt(1, base.darker(112))
        return gradient

    def _paint_soft_body(self, painter, path, top, color):
        self._fill_path(painter, path, self._pet_gradient(color, top),
                        QColor(color).darker(112), .85)
        painter.save()
        painter.setClipPath(path)
        shine = QRadialGradient(53, top + 20, 64)
        shine.setColorAt(0, QColor(255, 255, 255, 76))
        shine.setColorAt(1, QColor(255, 255, 255, 0))
        self._pet_ellipse(painter, -11, top - 44, 128, 128, shine)
        painter.restore()

    def _paint_paws(self, painter, top, color, centers=(53, 97), width=25):
        for center_x in centers:
            self._pet_ellipse(painter, center_x - width / 2, top + 93,
                              width, 13, self._pet_gradient(color, top + 91, 17),
                              QColor(color).darker(110))
            painter.setPen(QPen(QColor(color).darker(119), .8, Qt.SolidLine, Qt.RoundCap))
            for offset in (-2, 2):
                painter.drawLine(QPointF(center_x + offset, top + 101),
                                 QPointF(center_x + offset, top + 104))

    def _paint_cheeks(self, painter, cheek_y, centers=(46, 104)):
        blush = QRadialGradient(.5, .5, .5)
        blush.setCoordinateMode(QGradient.ObjectBoundingMode)
        blush.setColorAt(0, QColor(237, 151, 165, 150 if self._state != "off" else 60))
        blush.setColorAt(1, QColor(237, 151, 165, 0))
        for center_x in centers:
            self._pet_ellipse(painter, center_x - 8, cheek_y, 16, 9, blush)

    def _pet_expression(self):
        surprise = getattr(self, "_surprise_kind", None)
        if self._state == "resting" or surprise == "sleep":
            return "sleep"
        if self._state == "off":
            return "off"
        if surprise == "happy" or (self._interaction_active and self._interaction_mode == "tickle"):
            return "happy"
        if self._blink > 0 or surprise == "sneeze":
            return "blink"
        return self._state

    def _paint_rounded_eyes(self, painter, centers, eye_y, ink,
                            eye_width=11, eye_height=16):
        expression = self._pet_expression()
        if expression in ("sleep", "blink", "happy"):
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(ink), 2.0, Qt.SolidLine, Qt.RoundCap))
            bend = -5 if expression == "happy" else 3.5 if expression == "sleep" else 1.0
            for center_x in centers:
                eyelid = QPainterPath(QPointF(center_x - eye_width / 2, eye_y + eye_height * .55))
                eyelid.quadTo(center_x, eye_y + eye_height * .55 + bend,
                              center_x + eye_width / 2, eye_y + eye_height * .55)
                painter.drawPath(eyelid)
            return
        look_x, look_y = self._look
        current_height = eye_height * .55 if expression == "tired" else eye_height
        current_y = eye_y + (eye_height - current_height) / 2
        for center_x in centers:
            left = center_x - eye_width / 2 + look_x
            self._pet_ellipse(painter, left, current_y + look_y, eye_width, current_height,
                              self._pet_gradient(ink, current_y, current_height))
            self._pet_ellipse(painter, left + 2, current_y + 2 + look_y,
                              3.2, 3.8, QColor(255, 255, 255, 235 if expression != "off" else 145))
            if expression != "tired":
                self._pet_ellipse(painter, left + eye_width - 4, current_y + current_height - 5 + look_y,
                                  1.7, 2.1, QColor(225, 242, 253, 155))
        if expression == "tired":
            painter.setPen(QPen(QColor(ink), 1.6, Qt.SolidLine, Qt.RoundCap))
            for center_x in centers:
                painter.drawLine(QPointF(center_x - eye_width / 2, current_y + look_y),
                                 QPointF(center_x + eye_width / 2, current_y + look_y - .5))

    def _paint_muzzle(self, painter, nose_y, nose_color, ink="#5c4b49"):
        nose = QPainterPath(QPointF(70.5, nose_y))
        nose.quadTo(75, nose_y - 3, 79.5, nose_y)
        nose.quadTo(78, nose_y + 3, 75, nose_y + 4)
        nose.quadTo(72, nose_y + 3, 70.5, nose_y)
        self._fill_path(painter, nose, nose_color)
        painter.setPen(QPen(QColor(ink), 1.35, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        for direction in (-1, 1):
            mouth = QPainterPath(QPointF(75, nose_y + 4))
            mouth.cubicTo(75, nose_y + 10, 75 + direction * 7,
                          nose_y + 11, 75 + direction * 9, nose_y + 7)
            painter.drawPath(mouth)

    def _paint_sleep_marks(self, painter, left, top, color="#c7d2fe"):
        if self._pet_expression() != "sleep":
            return
        painter.setPen(QPen(QColor(color), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        for offset_x, offset_y, size in ((0, 0, 5), (9, -11, 7)):
            baseline = top + offset_y + math.sin(self._phase + offset_x * .1) * 2
            mark = QPainterPath(QPointF(left + offset_x, baseline))
            mark.lineTo(left + offset_x + size, baseline)
            mark.lineTo(left + offset_x, baseline + size)
            mark.lineTo(left + offset_x + size, baseline + size)
            painter.drawPath(mark)

    def _paint_mint_scarf(self, painter, center_x, scarf_y, width=64):
        half = width / 2
        wrap_color = "#83cdbd" if self._state != "off" else "#a0b4b0"
        tail_color = "#58aaa2" if self._state != "off" else "#809b96"
        if self._pet_kind == "pixel_robot":
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setPen(Qt.NoPen)
            for left, offset, span, height, color in (
                (center_x - half, -5, width, 6, wrap_color),
                (center_x - half + 5, 1, width - 10, 5, tail_color),
                (center_x - 22, 5, 9, 16, tail_color),
                (center_x - half + 4, -4, width - 8, 2, "#b8e1d4"),
                (center_x - 21, 15, 7, 2, wrap_color),
            ):
                painter.setBrush(QColor(color))
                painter.drawRect(int(left), int(round(scarf_y + offset)), int(span), height)
            painter.restore()
            return

        sway = math.sin(self._phase * 1.4) * 1.4
        tail = QPainterPath(QPointF(center_x - 13, scarf_y + 3))
        tail.cubicTo(center_x - 14, scarf_y + 12, center_x - 9 + sway,
                     scarf_y + 21, center_x - 5 + sway, scarf_y + 26)
        tail.quadTo(center_x + 2 + sway, scarf_y + 27,
                     center_x + 7 + sway, scarf_y + 22)
        tail.lineTo(center_x + 1, scarf_y + 4)
        tail.closeSubpath()
        self._fill_path(painter, tail, self._pet_gradient(tail_color, scarf_y, 28))
        wrap = QPainterPath(QPointF(center_x - half, scarf_y - 6))
        wrap.cubicTo(center_x - 13, scarf_y + 1, center_x + 14, scarf_y + 2,
                     center_x + half, scarf_y - 6)
        wrap.lineTo(center_x + half - 3, scarf_y + 4)
        wrap.cubicTo(center_x + 12, scarf_y + 12, center_x - 14, scarf_y + 11,
                     center_x - half + 3, scarf_y + 4)
        wrap.closeSubpath()
        painter.save()
        painter.translate(0, 1.5)
        self._fill_path(painter, wrap, QColor(28, 65, 70, 35))
        painter.restore()
        self._fill_path(painter, wrap, self._pet_gradient(wrap_color, scarf_y - 5, 18))
        seam = QPainterPath(QPointF(center_x - half + 7, scarf_y - 1))
        seam.cubicTo(center_x - 12, scarf_y + 5, center_x + 14, scarf_y + 6,
                     center_x + half - 7, scarf_y - 1)
        self._fill_path(painter, seam, Qt.NoBrush, "#c6e9dc", 1.1)
        painter.setPen(QPen(QColor("#a5d9ca"), 1.0, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(center_x - 8 + sway, scarf_y + 18),
                         QPointF(center_x + 1 + sway, scarf_y + 15))

    def _paint_seagull(self, painter, top, body, light, belly):
        flap = math.sin(self._phase * 1.7) * 3
        feet_color = "#e9b667" if self._state != "off" else "#a4aaa9"
        self._paint_paws(painter, top, feet_color, (54, 96), 24)
        right_wing = QPainterPath(QPointF(108, top + 46))
        right_wing.cubicTo(124, top + 52 - flap, 133, top + 39 - flap,
                           130, top + 28 - flap)
        right_wing.cubicTo(142, top + 45 - flap, 134, top + 68, 111, top + 80)
        right_wing.quadTo(103, top + 62, 108, top + 46)
        self._fill_path(painter, right_wing, self._pet_gradient(light, top + 30, 55),
                        QColor(light).darker(108), .85)

        bird = QPainterPath(QPointF(72, top + 5))
        bird.cubicTo(43, top + 1, 32, top + 28, 32, top + 58)
        bird.cubicTo(29, top + 87, 45, top + 101, 75, top + 102)
        bird.cubicTo(108, top + 103, 122, top + 83, 117, top + 55)
        bird.cubicTo(115, top + 27, 103, top + 8, 87, top + 6)
        bird.cubicTo(89, top - 3, 81, top - 5, 72, top + 5)
        self._paint_soft_body(painter, bird, top, body)
        belly_glow = QColor(belly)
        belly_glow.setAlpha(100)
        self._pet_ellipse(painter, 44, top + 35, 64, 60, belly_glow)

        left_wing = QPainterPath(QPointF(37, top + 46))
        left_wing.cubicTo(23, top + 55 + flap, 25, top + 77, 42, top + 86)
        left_wing.cubicTo(49, top + 72, 47, top + 56, 37, top + 46)
        self._fill_path(painter, left_wing, self._pet_gradient(light, top + 42, 48))
        feather = QPainterPath(QPointF(33, top + 61))
        feather.quadTo(34, top + 72, 40, top + 77)
        self._fill_path(painter, feather, Qt.NoBrush, QColor(belly), 1.1)

        self._paint_rounded_eyes(painter, (59, 91), top + 35, "#263d4d", 10, 15)
        self._paint_cheeks(painter, top + 52)
        beak = QPainterPath(QPointF(67, top + 54))
        beak.quadTo(75, top + 49, 83, top + 54)
        beak.quadTo(80, top + 63, 75, top + 64)
        beak.quadTo(70, top + 63, 67, top + 54)
        self._fill_path(painter, beak, self._pet_gradient(feet_color, top + 50, 14))
        beak_fold = QPainterPath(QPointF(69, top + 55))
        beak_fold.quadTo(75, top + 58, 81, top + 55)
        self._fill_path(painter, beak_fold, Qt.NoBrush, QColor(feet_color).darker(119), .9)
        self._paint_sleep_marks(painter, 116, top + 23)

    def _paint_cream_cat(self, painter, top, body, light, belly):
        self._paint_cat_character(painter, top, body, light, belly)

    def _paint_cat_character(self, painter, top, body, light, belly):
        cream = self._pet_kind == "cream_cat"
        dark = self._pet_kind == "charcoal_cat"
        wag = math.sin(self._phase * 1.7) * 5
        tail_color = light if cream else body
        tail = QPainterPath(QPointF(108, top + 84))
        tail.cubicTo(137, top + 90, 141, top + 58 + wag, 123, top + 54 + wag)
        self._fill_path(painter, tail, Qt.NoBrush, QColor(tail_color).darker(106), 11)
        self._fill_path(painter, tail, Qt.NoBrush, QColor(tail_color).lighter(111), 3)

        ear_height = 9 if cream else 5
        cat = QPainterPath(QPointF(34, top + 32))
        cat.cubicTo(32, top + 17, 31, top - ear_height, 38, top - ear_height)
        cat.quadTo(44, top - ear_height - 2, 58, top + 10)
        cat.quadTo(75, top + 3, 94, top + 10)
        cat.quadTo(108, top - ear_height - 2, 113, top - ear_height + 2)
        cat.quadTo(118, top, 115, top + 32)
        cat.cubicTo(126, top + 49, 121, top + 72, 111, top + 83)
        cat.cubicTo(113, top + 100, 97, top + 103, 75, top + 103)
        cat.cubicTo(51, top + 103, 37, top + 100, 38, top + 82)
        cat.cubicTo(26, top + 72, 24, top + 50, 34, top + 32)
        self._paint_soft_body(painter, cat, top, body)
        for direction in (-1, 1):
            ear = QPainterPath(QPointF(75 + direction * 36, top + 3 - ear_height * .25))
            ear.quadTo(75 + direction * 35, top + 12, 75 + direction * 35, top + 22)
            ear.lineTo(75 + direction * 23, top + 17)
            ear.closeSubpath()
            self._fill_path(painter, ear, "#d6adba" if dark else "#eac2c0")

        if dark:
            chest = QPainterPath(QPointF(56, top + 66))
            chest.quadTo(75, top + 73, 94, top + 66)
            chest.quadTo(92, top + 92, 75, top + 96)
            chest.quadTo(58, top + 92, 56, top + 66)
            self._fill_path(painter, chest, belly)
            self._pet_ellipse(painter, 61, top + 56, 16, 14, belly)
            self._pet_ellipse(painter, 73, top + 56, 16, 14, belly)
            moon = QPainterPath()
            moon.addEllipse(QRectF(67, top + 16, 13, 13))
            cutout = QPainterPath()
            cutout.addEllipse(QRectF(72, top + 13, 12, 13))
            self._fill_path(painter, moon.subtracted(cutout), "#e8d4a2")
        else:
            mask = QPainterPath(QPointF(75, top + 37))
            mask.cubicTo(57, top + 24, 37, top + 34, 39, top + 57)
            mask.cubicTo(39, top + 80, 57, top + 92, 75, top + 94)
            mask.cubicTo(95, top + 91, 113, top + 78, 111, top + 55)
            mask.cubicTo(112, top + 35, 93, top + 26, 75, top + 37)
            self._fill_path(painter, mask, self._pet_gradient(belly, top + 30, 70))
            if cream:
                for stripe_x, height in ((62, 13), (75, 18), (88, 13)):
                    stripe = QPainterPath(QPointF(stripe_x - 3, top + 14))
                    stripe.quadTo(stripe_x, top + 11, stripe_x + 3, top + 14)
                    stripe.quadTo(stripe_x + 3, top + 22, stripe_x, top + 14 + height)
                    stripe.quadTo(stripe_x - 3, top + 22, stripe_x - 3, top + 14)
                    self._fill_path(painter, stripe, light)
            else:
                tuft = QPainterPath(QPointF(65, top + 17))
                tuft.quadTo(72, top + 21, 73, top + 27)
                tuft.quadTo(77, top + 20, 85, top + 19)
                self._fill_path(painter, tuft, Qt.NoBrush, light, 2.2)

        self._paint_paws(painter, top, light if cream else body)
        self._paint_rounded_eyes(painter, (58, 92), top + 39,
                                 "#e4c891" if dark else "#304352", 10.5, 15)
        self._paint_cheeks(painter, top + 57)
        self._paint_muzzle(painter, top + 58, "#bc8991" if not dark else "#d5a9ad")
        whisker_color = QColor("#d9e5ee" if dark else "#8e9daa")
        whisker_color.setAlpha(135)
        painter.setPen(QPen(whisker_color, 1.05, Qt.SolidLine, Qt.RoundCap))
        for direction in (-1, 1):
            for offset in (0, 5):
                painter.drawLine(QPointF(75 + direction * 30, top + 60 + offset),
                                 QPointF(75 + direction * 41, top + 58 + offset * 1.4))
        self._paint_sleep_marks(painter, 118, top + 23)

    def _paint_pixel_robot(self, painter, top, body, light, belly):
        baseline = int(round(top - 5))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(Qt.NoPen)

        def block(left, offset, width, height, color):
            painter.setBrush(QColor(color))
            painter.drawRect(left, baseline + offset, width, height)

        edge = QColor(light).darker(118)
        shine = QColor(body).lighter(121)
        screen_ink = "#98e5d8" if self._state != "off" else "#93a4ad"
        if "sprout" not in self._outfit and "night_cap" not in self._outfit:
            block(73, -10, 4, 15, light)
            block(69, -14, 12, 8, "#c5b6e8" if self._state != "off" else light)
            block(71, -13, 4, 2, "#e9e2f8")
        block(27, 19, 8, 23, edge)
        block(115, 19, 8, 23, edge)
        block(29, 20, 3, 18, light)
        block(118, 20, 3, 18, light)
        block(39, 4, 72, 55, edge)
        block(35, 8, 80, 47, body)
        block(39, 4, 72, 49, body)
        block(39, 5, 69, 3, shine)
        block(36, 10, 3, 40, shine)
        block(111, 10, 4, 44, light)
        block(39, 53, 72, 4, light)
        block(41, 13, 68, 36, edge)
        block(44, 16, 62, 30, belly)
        block(45, 17, 37, 2, QColor(217, 239, 242, 35))

        expression = self._pet_expression()
        for eye_x in (51, 89):
            if expression in ("sleep", "blink"):
                block(eye_x, 29, 10, 2, screen_ink)
            elif expression == "happy":
                block(eye_x, 26, 3, 6, screen_ink)
                block(eye_x + 3, 23, 4, 3, screen_ink)
                block(eye_x + 7, 26, 3, 6, screen_ink)
            else:
                eye_height = 5 if expression == "tired" else 10
                eye_y = 26 if expression == "tired" else 22
                look_x = int(round(self._look[0] / 2))
                look_y = int(round(self._look[1] / 2))
                block(eye_x + look_x, eye_y + look_y, 10, eye_height, screen_ink)
                block(eye_x + look_x + 1, eye_y + look_y + 1, 2, 2, shine)
        block(70, 38, 3, 3, screen_ink)
        block(73, 41, 6, 2, screen_ink)
        block(79, 38, 3, 3, screen_ink)
        if self._state != "off":
            block(47, 35, 6, 2, "#c597aa")
            block(98, 35, 6, 2, "#c597aa")

        block(69, 59, 12, 6, edge)
        block(50, 65, 50, 36, body)
        block(52, 66, 46, 3, shine)
        block(50, 97, 50, 4, light)
        block(59, 75, 33, 22, light)
        block(62, 77, 27, 18, belly)
        block(37, 69, 9, 24, body)
        block(104, 69, 9, 24, body)
        block(38, 69, 3, 18, shine)
        block(105, 69, 3, 18, shine)
        block(37, 91, 9, 5, light)
        block(104, 91, 9, 5, light)
        heart = "#f0b6b6" if self._state != "off" else "#93a4ad"
        for row_index, row in enumerate(("01010", "11111", "11111", "01110", "00100")):
            for column_index, pixel in enumerate(row):
                if pixel == "1":
                    block(68 + column_index * 3, 79 + row_index * 3, 3, 3, heart)
        for screw_x in (54, 95):
            block(screw_x, 71, 2, 2, edge)
        block(56, 101, 11, 5, edge)
        block(83, 101, 11, 5, edge)
        block(50, 106, 20, 8, light)
        block(80, 106, 20, 8, light)
        block(51, 106, 18, 2, body)
        block(81, 106, 18, 2, body)
        painter.restore()
        self._paint_sleep_marks(painter, 119, top + 22, "#c5b6e8")

    # 旧版的六款角色都走同一套圆身 fallback，看起来像换了颜色的同一只宠物。
    # 这里保留轻量 QPainter 方案，但给每个角色独立轮廓、脸型和标志性细节。
    def _paint_blue_cat(self, painter, top, body, light, belly):
        self._paint_cat_character(painter, top, body, light, belly)

    def _paint_orange_fox(self, painter, top, body, light, belly):
        wag = math.sin(self._phase * 1.5) * 3
        tail = QPainterPath(QPointF(106, top + 91))
        tail.cubicTo(139, top + 95, 145, top + 66 + wag, 132, top + 47 + wag)
        tail.quadTo(121, top + 31 + wag, 125, top + 21 + wag)
        tail.cubicTo(108, top + 29, 104, top + 48, 113, top + 64)
        tail.quadTo(96, top + 75, 106, top + 91)
        self._paint_soft_body(painter, tail, top + 20, body)
        painter.save()
        painter.setClipPath(tail)
        tail_tip = QPainterPath(QPointF(108, top + 13 + wag))
        tail_tip.lineTo(140, top + 13 + wag)
        tail_tip.lineTo(137, top + 49 + wag)
        tail_tip.quadTo(125, top + 43 + wag, 113, top + 49 + wag)
        tail_tip.closeSubpath()
        self._fill_path(painter, tail_tip, belly)
        painter.restore()

        fox = QPainterPath(QPointF(36, top + 32))
        fox.cubicTo(36, top + 19, 35, top - 7, 41, top - 9)
        fox.quadTo(45, top - 10, 59, top + 16)
        fox.quadTo(75, top + 9, 91, top + 16)
        fox.quadTo(105, top - 10, 110, top - 8)
        fox.quadTo(117, top + 2, 115, top + 32)
        fox.lineTo(123, top + 47)
        fox.lineTo(118, top + 56)
        fox.lineTo(126, top + 61)
        fox.quadTo(117, top + 73, 108, top + 80)
        fox.cubicTo(111, top + 96, 94, top + 102, 75, top + 102)
        fox.cubicTo(53, top + 102, 37, top + 94, 42, top + 80)
        fox.quadTo(29, top + 72, 24, top + 62)
        fox.lineTo(32, top + 56)
        fox.lineTo(28, top + 48)
        fox.closeSubpath()
        self._paint_soft_body(painter, fox, top, body)
        for direction in (-1, 1):
            ear = QPainterPath(QPointF(75 + direction * 33, top + 1))
            ear.lineTo(75 + direction * 32, top + 26)
            ear.lineTo(75 + direction * 22, top + 20)
            ear.closeSubpath()
            self._fill_path(painter, ear, "#98756b")

        mask = QPainterPath(QPointF(75, top + 48))
        mask.cubicTo(62, top + 44, 60, top + 31, 45, top + 34)
        mask.cubicTo(34, top + 40, 36, top + 63, 48, top + 70)
        mask.cubicTo(54, top + 93, 93, top + 101, 101, top + 72)
        mask.cubicTo(118, top + 61, 116, top + 40, 106, top + 34)
        mask.cubicTo(90, top + 31, 88, top + 44, 75, top + 48)
        self._fill_path(painter, mask, self._pet_gradient(belly, top + 32, 65))
        self._paint_paws(painter, top, QColor(body).darker(113), width=23)
        self._paint_rounded_eyes(painter, (58, 92), top + 40, "#594338", 10, 13)
        self._paint_cheeks(painter, top + 57)
        self._paint_muzzle(painter, top + 57, "#6a4b43")
        self._paint_sleep_marks(painter, 116, top + 24, "#e8cbaa")

    def _paint_mint_bunny(self, painter, top, body, light, belly):
        sway = math.sin(self._phase * 1.1) * 2
        left_ear = QPainterPath(QPointF(41, top + 31))
        left_ear.cubicTo(30, top + 5, 28, top - 27, 39, top - 28)
        left_ear.cubicTo(51, top - 28, 56, top + 2, 55, top + 31)
        left_ear.closeSubpath()
        self._paint_soft_body(painter, left_ear, top - 28, body)
        right_ear = QPainterPath(QPointF(92, top + 29))
        right_ear.cubicTo(88, top + 3, 98 + sway, top - 27, 109 + sway, top - 19)
        right_ear.cubicTo(118 + sway, top - 9, 107, top + 15, 107, top + 32)
        right_ear.closeSubpath()
        self._paint_soft_body(painter, right_ear, top - 22, body)
        inner_ear = QPainterPath(QPointF(41, top + 19))
        inner_ear.cubicTo(36, top + 1, 34, top - 19, 39, top - 19)
        inner_ear.cubicTo(45, top - 20, 49, top + 3, 48, top + 21)
        self._fill_path(painter, inner_ear, "#e6c9ce")
        inner_ear = QPainterPath(QPointF(98, top + 20))
        inner_ear.cubicTo(96, top + 3, 102 + sway, top - 15, 107 + sway, top - 13)
        inner_ear.cubicTo(112, top - 7, 103, top + 9, 103, top + 22)
        self._fill_path(painter, inner_ear, "#e6c9ce")
        self._pet_ellipse(painter, 113, top + 76, 21, 21,
                          self._pet_gradient(belly, top + 74, 24))
        bunny = QPainterPath(QPointF(75, top + 19))
        bunny.cubicTo(47, top + 16, 28, top + 34, 30, top + 59)
        bunny.cubicTo(25, top + 85, 45, top + 102, 75, top + 103)
        bunny.cubicTo(106, top + 103, 123, top + 84, 120, top + 58)
        bunny.cubicTo(122, top + 34, 102, top + 16, 75, top + 19)
        self._paint_soft_body(painter, bunny, top + 16, body)
        self._pet_ellipse(painter, 41, top + 36, 68, 58,
                          self._pet_gradient(belly, top + 36, 60))
        self._paint_paws(painter, top, light, width=27)
        self._paint_rounded_eyes(painter, (59, 91), top + 42, "#38594e", 10, 14)
        self._paint_cheeks(painter, top + 58)
        self._paint_muzzle(painter, top + 59, "#c58e9f", "#62877a")
        painter.setPen(QPen(QColor("#c1d6cc"), .65))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(QRectF(72, top + 65, 6, 6), 1.5, 1.5)
        painter.drawLine(QPointF(75, top + 66), QPointF(75, top + 70))
        self._paint_sleep_marks(painter, 116, top + 26, "#b3dac8")

    def _paint_purple_owl(self, painter, top, body, light, belly):
        owl = QPainterPath(QPointF(75, top + 11))
        owl.quadTo(55, top + 6, 40, top - 1)
        owl.quadTo(34, top + 9, 38, top + 21)
        owl.cubicTo(28, top + 39, 27, top + 68, 40, top + 86)
        owl.cubicTo(53, top + 108, 98, top + 108, 111, top + 86)
        owl.cubicTo(124, top + 66, 121, top + 38, 112, top + 21)
        owl.quadTo(115, top + 8, 110, top - 1)
        owl.quadTo(96, top + 6, 75, top + 11)
        self._paint_soft_body(painter, owl, top, body)
        self._pet_ellipse(painter, 48, top + 55, 54, 43,
                          self._pet_gradient(light, top + 50, 50))
        mask = QPainterPath(QPointF(75, top + 35))
        mask.cubicTo(57, top + 15, 34, top + 31, 40, top + 50)
        mask.cubicTo(44, top + 64, 63, top + 73, 75, top + 78)
        mask.cubicTo(89, top + 72, 107, top + 62, 111, top + 49)
        mask.cubicTo(116, top + 28, 92, top + 16, 75, top + 35)
        self._fill_path(painter, mask, self._pet_gradient(belly, top + 24, 55))
        for direction in (-1, 1):
            wing = QPainterPath(QPointF(75 + direction * 37, top + 46))
            wing.cubicTo(75 + direction * 54, top + 47,
                         75 + direction * 52, top + 76,
                         75 + direction * 34, top + 87)
            wing.quadTo(75 + direction * 29, top + 69,
                        75 + direction * 37, top + 46)
            self._fill_path(painter, wing, self._pet_gradient(body, top + 40, 45),
                            QColor(body).darker(115), .85)
            for offset in (0, 6):
                feather = QPainterPath(QPointF(75 + direction * 44, top + 58 + offset))
                feather.quadTo(75 + direction * 43, top + 68 + offset,
                               75 + direction * 37, top + 73 + offset)
                self._fill_path(painter, feather, Qt.NoBrush, light, 1)
        self._paint_paws(painter, top, "#d5b888", (57, 93), 18)
        self._paint_rounded_eyes(painter, (58, 92), top + 36, "#49425f", 13, 18)
        self._paint_cheeks(painter, top + 57, (45, 105))
        beak = QPainterPath(QPointF(69, top + 58))
        beak.quadTo(75, top + 53, 81, top + 58)
        beak.quadTo(78, top + 65, 75, top + 68)
        beak.quadTo(72, top + 65, 69, top + 58)
        self._fill_path(painter, beak, self._pet_gradient("#e8bf7d", top + 54, 14))
        for center_x in (63, 75, 87):
            feather = QPainterPath(QPointF(center_x - 2, top + 89))
            feather.quadTo(center_x, top + 93, center_x + 2, top + 89)
            self._fill_path(painter, feather, Qt.NoBrush, QColor(body).darker(111), 1.1)
        self._paint_sleep_marks(painter, 118, top + 23, "#ded4ef")

    def _paint_pink_poodle(self, painter, top, body, light, belly):
        tail = QPainterPath(QPointF(109, top + 83))
        tail.cubicTo(132, top + 88, 135, top + 72, 128, top + 64)
        self._fill_path(painter, tail, Qt.NoBrush, QColor(body).darker(108), 5)
        self._pet_ellipse(painter, 117, top + 55, 21, 20,
                          self._pet_gradient(light, top + 53, 24))
        dog = QPainterPath(QPointF(75, top + 24))
        dog.cubicTo(46, top + 19, 31, top + 40, 35, top + 65)
        dog.cubicTo(29, top + 90, 47, top + 103, 75, top + 103)
        dog.cubicTo(106, top + 103, 122, top + 87, 115, top + 65)
        dog.cubicTo(119, top + 38, 103, top + 19, 75, top + 24)
        self._paint_soft_body(painter, dog, top + 20, body)
        for mirrored in (False, True):
            painter.save()
            if mirrored:
                painter.translate(150, 0)
                painter.scale(-1, 1)
            ear = QPainterPath(QPointF(44, top + 30))
            ear.cubicTo(28, top + 23, 22, top + 38, 27, top + 49)
            ear.cubicTo(20, top + 59, 23, top + 77, 36, top + 80)
            ear.cubicTo(48, top + 82, 54, top + 68, 48, top + 57)
            ear.quadTo(53, top + 41, 44, top + 30)
            self._paint_soft_body(painter, ear, top + 29, light)
            for curl_y in (43, 58):
                curl = QPainterPath(QPointF(30, top + curl_y))
                curl.cubicTo(37, top + curl_y - 5, 43, top + curl_y + 1,
                             36, top + curl_y + 5)
                self._fill_path(painter, curl, Qt.NoBrush, QColor(body).darker(105), .9)
            painter.restore()

        curls = QPainterPath()
        curls.setFillRule(Qt.WindingFill)
        for curl_x, curl_y, diameter in ((42, 11, 29), (61, 5, 31), (82, 12, 27)):
            curls.addEllipse(QRectF(curl_x, top + curl_y, diameter, diameter))
        self._paint_soft_body(painter, curls.simplified(), top + 4, light)
        curl = QPainterPath(QPointF(69, top + 17))
        curl.cubicTo(72, top + 12, 81, top + 16, 76, top + 22)
        self._fill_path(painter, curl, Qt.NoBrush, QColor(body).darker(105), 1.2)
        self._pet_ellipse(painter, 45, top + 48, 60, 40,
                          self._pet_gradient(belly, top + 45, 45))
        self._paint_paws(painter, top, light, width=27)
        self._paint_rounded_eyes(painter, (59, 91), top + 42, "#594657", 10, 14)
        self._paint_cheeks(painter, top + 59)
        self._paint_muzzle(painter, top + 59, "#705164", "#876779")
        if self._pet_expression() == "happy":
            self._pet_ellipse(painter, 72, top + 65, 6, 7, "#dc9cad")
        self._paint_sleep_marks(painter, 118, top + 24, "#ecd0e0")

    def _paint_charcoal_cat(self, painter, top, body, light, belly):
        self._paint_cat_character(painter, top, body, light, belly)

    def _paint_capybara(self, painter, top, body, light, belly):
        for ear_x in (34, 96):
            self._pet_ellipse(painter, ear_x, top - 2, 20, 24,
                              self._pet_gradient(body, top - 2, 24), light)
            self._pet_ellipse(painter, ear_x + 5, top + 3, 10, 15, light)

        silhouette = QPainterPath(QPointF(39, top + 20))
        silhouette.cubicTo(46, top + 6, 101, top + 5, 112, top + 23)
        silhouette.cubicTo(124, top + 39, 125, top + 66, 118, top + 84)
        silhouette.cubicTo(114, top + 100, 96, top + 105, 75, top + 104)
        silhouette.cubicTo(47, top + 106, 30, top + 95, 29, top + 75)
        silhouette.cubicTo(25, top + 50, 28, top + 32, 39, top + 20)
        self._paint_soft_body(painter, silhouette, top, body)
        self._pet_ellipse(painter, 47, top + 70, 57, 31,
                          self._pet_gradient(belly, top + 70, 31))
        for paw_x in (33, 105):
            self._pet_ellipse(painter, paw_x, top + 74, 12, 26,
                              self._pet_gradient(body, top + 74, 26))
        self._paint_paws(painter, top, light, (49, 101), 27)

        for tuft_x in (66, 75, 84):
            tuft = QPainterPath(QPointF(tuft_x - 2, top + 16))
            tuft.quadTo(tuft_x - 3, top + 20, tuft_x, top + 22)
            self._fill_path(painter, tuft, Qt.NoBrush, light, 1.2)
        self._paint_rounded_eyes(painter, (49, 101), top + 32, "#503e33", 8.5, 11)
        self._paint_cheeks(painter, top + 49, (36, 114))
        self._pet_ellipse(painter, 41, top + 43, 68, 28,
                          self._pet_gradient(belly, top + 42, 30),
                          QColor(body).darker(106))
        for nostril_x in (68, 79):
            self._pet_ellipse(painter, nostril_x, top + 54, 2.7, 3.3, "#70513d")
        mouth = QPainterPath(QPointF(64, top + 63))
        mouth.quadTo(75, top + 69, 86, top + 63)
        self._fill_path(painter, mouth, Qt.NoBrush, "#70513d", 1.35)
        self._paint_sleep_marks(painter, 119, top + 23, "#d4bd94")

    def _paint_red_panda(self, painter, top, body, light, belly):
        wag = math.sin(self._phase * 1.7) * 3
        painter.save()
        painter.translate(wag * .18, wag)
        tail = QPainterPath(QPointF(107, top + 94))
        tail.cubicTo(136, top + 99, 143, top + 76, 136, top + 53)
        tail.cubicTo(131, top + 35, 122, top + 23, 116, top + 22)
        tail.cubicTo(107, top + 22, 109, top + 36, 116, top + 46)
        tail.cubicTo(128, top + 68, 123, top + 78, 105, top + 76)
        tail.closeSubpath()
        self._fill_path(painter, tail, self._pet_gradient(body, top + 22, 76), light, .8)
        painter.setClipPath(tail)
        for stripe_x, stripe_y in ((116, 34), (128, 53), (132, 73), (118, 90)):
            stripe = QPainterPath(QPointF(stripe_x - 11, top + stripe_y - 2))
            stripe.quadTo(stripe_x, top + stripe_y + 4,
                          stripe_x + 12, top + stripe_y - 3)
            self._fill_path(painter, stripe, Qt.NoBrush, light, 8)
        painter.restore()

        for ear_x in (31, 92):
            self._pet_ellipse(painter, ear_x, top - 5, 28, 32, belly,
                              QColor(body).darker(112))
            self._pet_ellipse(painter, ear_x + 6, top + 1, 16, 23, light)
        silhouette = QPainterPath(QPointF(34, top + 26))
        silhouette.cubicTo(41, top + 7, 105, top + 6, 117, top + 27)
        silhouette.quadTo(125, top + 39, 121, top + 49)
        silhouette.lineTo(128, top + 57)
        silhouette.lineTo(120, top + 61)
        silhouette.lineTo(123, top + 66)
        silhouette.quadTo(117, top + 77, 108, top + 80)
        silhouette.cubicTo(111, top + 98, 91, top + 104, 74, top + 104)
        silhouette.cubicTo(51, top + 104, 37, top + 95, 40, top + 81)
        silhouette.quadTo(29, top + 74, 26, top + 65)
        silhouette.lineTo(32, top + 60)
        silhouette.lineTo(25, top + 54)
        silhouette.quadTo(25, top + 38, 34, top + 26)
        self._paint_soft_body(painter, silhouette, top, body)

        chest = QPainterPath(QPointF(51, top + 68))
        chest.quadTo(75, top + 80, 99, top + 68)
        chest.quadTo(103, top + 99, 75, top + 102)
        chest.quadTo(47, top + 99, 51, top + 68)
        self._fill_path(painter, chest, self._pet_gradient(light, top + 68, 34))
        self._paint_paws(painter, top, light, (49, 101), 24)
        for direction in (-1, 1):
            mask = QPainterPath(QPointF(75 + direction * 6, top + 48))
            mask.cubicTo(75 + direction * 16, top + 25,
                         75 + direction * 36, top + 27,
                         75 + direction * 40, top + 43)
            mask.quadTo(75 + direction * 49, top + 57,
                        75 + direction * 29, top + 65)
            mask.quadTo(75 + direction * 8, top + 69,
                        75 + direction * 6, top + 48)
            self._fill_path(painter, mask, self._pet_gradient(belly, top + 30, 38))
            tear = QPainterPath(QPointF(75 + direction * 22, top + 46))
            tear.quadTo(75 + direction * 29, top + 49,
                        75 + direction * 28, top + 59)
            tear.quadTo(75 + direction * 21, top + 57,
                        75 + direction * 19, top + 49)
            tear.closeSubpath()
            self._fill_path(painter, tear, light)
            self._pet_ellipse(painter, 69 + direction * 21, top + 25, 12, 5, belly)
        self._pet_ellipse(painter, 56, top + 51, 38, 20, belly)
        self._paint_rounded_eyes(painter, (54, 96), top + 37, "#392f30", 9, 13)
        self._paint_cheeks(painter, top + 57, (39, 111))
        self._paint_muzzle(painter, top + 56, "#483735", "#634438")
        self._paint_sleep_marks(painter, 120, top + 19, "#e5bc93")

    def _paint_penguin(self, painter, top, body, light, belly):
        feet = "#e7b26b" if self._state != "off" else "#a4aaa9"
        self._paint_paws(painter, top, feet, (52, 98), 26)
        silhouette = QPainterPath(QPointF(74, top + 5))
        silhouette.cubicTo(49, top + 3, 35, top + 26, 38, top + 52)
        silhouette.cubicTo(27, top + 71, 30, top + 96, 52, top + 103)
        silhouette.quadTo(75, top + 109, 98, top + 103)
        silhouette.cubicTo(121, top + 96, 123, top + 70, 112, top + 52)
        silhouette.cubicTo(115, top + 29, 103, top + 7, 83, top + 5)
        silhouette.quadTo(85, top - 3, 79, top - 2)
        silhouette.quadTo(75, top, 74, top + 5)
        self._paint_soft_body(painter, silhouette, top, body)

        bib = QPainterPath(QPointF(75, top + 33))
        bib.cubicTo(60, top + 14, 42, top + 27, 46, top + 49)
        bib.cubicTo(34, top + 78, 48, top + 99, 75, top + 100)
        bib.cubicTo(102, top + 99, 116, top + 78, 104, top + 49)
        bib.cubicTo(108, top + 27, 90, top + 14, 75, top + 33)
        self._fill_path(painter, bib, self._pet_gradient(belly, top + 25, 78))

        flap = math.sin(self._phase * 1.7) * 3
        for direction in (-1, 1):
            flipper = QPainterPath(QPointF(75 + direction * 35, top + 47))
            flipper.cubicTo(75 + direction * 50, top + 52 + direction * flap,
                            75 + direction * 52, top + 74 + direction * flap,
                            75 + direction * 45, top + 85 + direction * flap)
            flipper.cubicTo(75 + direction * 33, top + 82,
                            75 + direction * 27, top + 60,
                            75 + direction * 35, top + 47)
            self._fill_path(painter, flipper,
                            self._pet_gradient(light, top + 46, 42),
                            QColor(light).darker(110), .8)
            feather = QPainterPath(QPointF(75 + direction * 41, top + 61))
            feather.quadTo(75 + direction * 43, top + 71,
                           75 + direction * 42, top + 76)
            self._fill_path(painter, feather, Qt.NoBrush, body, 1.1)

        self._paint_rounded_eyes(painter, (58, 92), top + 36, "#2e4055", 9.5, 14)
        self._paint_cheeks(painter, top + 53, (46, 104))
        beak = QPainterPath(QPointF(68, top + 55))
        beak.quadTo(75, top + 51, 82, top + 55)
        beak.quadTo(82, top + 60, 75, top + 65)
        beak.quadTo(68, top + 60, 68, top + 55)
        self._fill_path(painter, beak, self._pet_gradient(feet, top + 53, 12))
        fold = QPainterPath(QPointF(69, top + 56))
        fold.quadTo(75, top + 59, 81, top + 56)
        self._fill_path(painter, fold, Qt.NoBrush, QColor(feet).darker(119), .9)
        self._paint_sleep_marks(painter, 119, top + 23, "#b3d8e9")

    def _paint_decoration(self, painter, top, decoration):
        """绘制独立于角色本体的可切换装饰。"""
        if decoration == "scarf":
            scarf_y = top + (63 if self._pet_kind == "pixel_robot" else 70)
            width = 54 if self._pet_kind == "pixel_robot" else 64
            self._paint_mint_scarf(painter, 75, scarf_y, width)
        elif decoration == "sprout":
            if self._pet_kind == "pixel_robot":
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing, False)
                painter.setPen(Qt.NoPen)
                for left, offset, width, height, color in (
                    (74, -17, 3, 17, "#71a487"), (65, -19, 9, 5, "#8dcaa4"),
                    (62, -23, 8, 6, "#9ad4ac"), (77, -24, 10, 6, "#b2dfb6"),
                    (81, -28, 8, 5, "#b2dfb6"),
                ):
                    painter.setBrush(QColor(color))
                    painter.drawRect(left, int(round(top + offset)), width, height)
                painter.restore()
                return
            anchor = top + (13 if self._pet_kind == "mint_bunny" else 4)
            stem = QPainterPath(QPointF(75, anchor))
            stem.quadTo(78, anchor - 10, 76, anchor - 18)
            self._fill_path(painter, stem, Qt.NoBrush, "#6b9f80", 1.8)
            leaf = QPainterPath(QPointF(76, anchor - 10))
            leaf.cubicTo(61, anchor - 9, 59, anchor - 16, 59, anchor - 21)
            leaf.cubicTo(69, anchor - 23, 77, anchor - 20, 76, anchor - 10)
            self._fill_path(painter, leaf, self._pet_gradient("#91cba3", anchor - 22, 15))
            leaf = QPainterPath(QPointF(77, anchor - 14))
            leaf.cubicTo(77, anchor - 26, 88, anchor - 29, 94, anchor - 27)
            leaf.cubicTo(95, anchor - 18, 85, anchor - 12, 77, anchor - 14)
            self._fill_path(painter, leaf, self._pet_gradient("#b0dcb3", anchor - 28, 16))
        elif decoration == "star_pin":
            pin_x, pin_y = (103, top + 18) if self._pet_kind == "penguin" else (111, top + 8)
            star = QPainterPath()
            for point_index in range(10):
                angle = point_index * math.pi / 5 - math.pi / 2
                radius = 8 if point_index % 2 == 0 else 3.8
                point = QPointF(pin_x + math.cos(angle) * radius, pin_y + math.sin(angle) * radius)
                if point_index == 0:
                    star.moveTo(point)
                else:
                    star.lineTo(point)
            star.closeSubpath()
            self._fill_path(painter, star, "#e5ca8c")
        elif decoration == "night_cap":
            cap = QPainterPath(QPointF(46, top + 12))
            cap.quadTo(75, top - 31, 105, top + 12)
            cap.closeSubpath()
            self._fill_path(painter, cap, self._pet_gradient("#9b9fc4", top - 20, 34))
            painter.setPen(QPen(QColor("#e4dff2"), 3, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(49, top + 12), QPointF(101, top + 12))
            self._pet_ellipse(painter, 96, top - 16, 9, 9, "#e5ca8c")
        elif decoration == "round_glasses":
            painter.setBrush(QColor(20, 31, 42, 38))
            painter.setPen(QPen(QColor("#8fc6d8"), 2.2))
            painter.drawEllipse(QRectF(47, top + 27, 23, 20))
            painter.drawEllipse(QRectF(80, top + 27, 23, 20))
            painter.drawLine(QPointF(70, top + 36), QPointF(80, top + 36))
        elif decoration == "heart_badge":
            heart = QPainterPath(QPointF(109, top + 29))
            heart.cubicTo(98, top + 20, 96, top + 34, 109, top + 42)
            heart.cubicTo(122, top + 34, 120, top + 20, 109, top + 29)
            self._fill_path(painter, heart, self._pet_gradient("#ef8fa3", top + 22, 20))
        elif decoration == "moon_charm":
            painter.setPen(QPen(QColor("#d8c98f"), 1.7))
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(QRectF(51, top + 55, 48, 27), 205 * 16, 130 * 16)
            self._pet_ellipse(painter, 68, top + 73, 14, 14, "#e8d79a")
            self._pet_ellipse(painter, 73, top + 69, 12, 12, "#293b48")
        elif decoration == "tiny_crown":
            crown = QPainterPath(QPointF(58, top + 6))
            crown.lineTo(61, top - 12)
            crown.lineTo(71, top - 3)
            crown.lineTo(76, top - 16)
            crown.lineTo(83, top - 3)
            crown.lineTo(94, top - 12)
            crown.lineTo(92, top + 6)
            crown.closeSubpath()
            self._fill_path(painter, crown, self._pet_gradient("#f0cf78", top - 16, 22), "#b9923f", 1)

    def _paint_ears(self, p, top, light, belly, ear_style):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(light))
        if ear_style == "pointed":
            for x, angle in ((42, -18), (108, 18)):
                p.save()
                p.translate(x, top)
                p.rotate(angle)
                p.drawEllipse(-15, -20, 30, 38)
                p.restore()
        elif ear_style == "tall":
            p.drawEllipse(34, int(top - 18), 24, 52)
            p.drawEllipse(92, int(top - 18), 24, 52)
        elif ear_style == "round":
            p.drawEllipse(27, int(top - 5), 32, 30)
            p.drawEllipse(91, int(top - 5), 32, 30)
        else:
            p.drawEllipse(25, int(top - 4), 30, 34)
            p.drawEllipse(95, int(top - 4), 30, 34)
        p.setBrush(QColor(belly))
        if ear_style == "pointed":
            for x, angle in ((42, -18), (108, 18)):
                p.save()
                p.translate(x, top)
                p.rotate(angle)
                p.drawEllipse(-6, -7, 12, 18)
                p.restore()
        elif ear_style == "tall":
            p.drawEllipse(41, int(top + 1), 10, 25)
            p.drawEllipse(99, int(top + 1), 10, 25)
        elif ear_style == "round":
            p.drawEllipse(36, int(top + 3), 14, 14)
            p.drawEllipse(100, int(top + 3), 14, 14)
        else:
            p.drawEllipse(33, int(top + 5), 13, 17)
            p.drawEllipse(103, int(top + 5), 13, 17)

    def _paint_accessory(self, p, top, accessory):
        p.setPen(Qt.NoPen)
        if accessory == "bell":
            p.setBrush(QColor("#facc15"))
            p.drawEllipse(69, int(top + 74), 13, 13)
            p.setBrush(QColor("#78350f"))
            p.drawEllipse(74, int(top + 82), 3, 3)
        elif accessory == "bow":
            p.setBrush(QColor("#f43f5e"))
            p.drawEllipse(31, int(top + 88), 15, 10)
            p.drawEllipse(48, int(top + 88), 15, 10)
            p.drawEllipse(45, int(top + 90), 7, 7)
        elif accessory == "glasses":
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#1f2937"), 3))
            p.drawEllipse(46, int(top + 32), 21, 19)
            p.drawEllipse(84, int(top + 32), 21, 19)
            p.drawLine(67, int(top + 41), 84, int(top + 41))
        elif accessory == "leaf":
            p.setBrush(QColor("#22c55e"))
            p.drawEllipse(70, int(top - 10), 12, 7)
            p.setPen(QPen(QColor("#15803d"), 2))
            p.drawLine(76, int(top - 10), 76, int(top - 3))
        elif accessory == "star":
            font = QFont("Segoe UI Symbol")
            font.setPixelSize(15)
            font.setBold(True)
            p.setFont(font)
            p.setPen(QColor("#fde047"))
            p.drawText(108, int(top + 1), "★")

    def _paint_face(self, p, top):
        ink = QColor("#0f172a")
        ex, ey = self._look
        eye_y = top + 36
        if self._blink > 0 or self._state == "resting":
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(ink, 3, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(50, int(eye_y + 4), 16, 12, 0, -180 * 16)
            p.drawArc(85, int(eye_y + 4), 16, 12, 0, -180 * 16)
        else:
            eh = 11 if self._state == "tired" else 17
            for cx in (50, 88):
                p.setPen(Qt.NoPen); p.setBrush(ink)
                p.drawEllipse(int(cx + ex), int(eye_y + ey), 13, eh)
                p.setBrush(QColor(255, 255, 255, 230))
                p.drawEllipse(int(cx + 3 + ex), int(eye_y + 3 + ey), 4, 5)
            if self._state == "tired":              # 半睁的上眼皮
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(ink, 3, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(50, int(eye_y - 1), 63, int(eye_y - 2))
                p.drawLine(88, int(eye_y - 2), 101, int(eye_y - 1))
        # 腮红
        p.setPen(Qt.NoPen); p.setBrush(QColor(251, 113, 133, 140))
        p.drawEllipse(41, int(eye_y + 20), 13, 8)
        p.drawEllipse(96, int(eye_y + 20), 13, 8)
        # 嘴，休息时再冒两个 z
        p.setBrush(Qt.NoBrush); p.setPen(QPen(ink, 2, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(68, int(eye_y + 18), 16, 12, 200 * 16, 140 * 16)
        if self._state == "resting":
            zf = QFont("Segoe UI"); zf.setPixelSize(13); zf.setBold(True)
            p.setFont(zf)
            p.setPen(QColor("#c7d2fe"))
            p.drawText(112, int(eye_y - 4 + math.sin(self._phase) * 2), "z")
            p.drawText(122, int(eye_y - 16 + math.sin(self._phase + 1.2) * 2), "z")

    def _paint_bar(self, p):
        """脚下的细进度条：距离下次休息的进度。"""
        if self._state == "off" or self._total_secs <= 0:
            return
        done = 1 - self._left_secs / self._total_secs
        bar = QRect(33, 157, 84, 5)
        p.setPen(Qt.NoPen); p.setBrush(QColor(255, 255, 255, 45))
        p.drawRoundedRect(bar, 3, 3)
        p.setBrush(QColor("#f59e0b") if self._left_secs <= 60 else QColor("#38bdf8"))
        p.drawRoundedRect(QRect(bar.x(), bar.y(),
                                max(4, int(bar.width() * max(0.0, min(1.0, done)))),
                                bar.height()), 3, 3)

    def _paint_bubble(self, p):
        if not self._msg:
            return
        box = QRect(2, 0, self.W - 4, 30)
        p.setBrush(QColor(1, 4, 9, 235)); p.setPen(QPen(QColor("#38bdf8"), 1))
        p.drawRoundedRect(box.adjusted(0, 1, 0, -2), 8, 8)
        # 用像素字号，避免不同 DPI 下文字撑破气泡
        f = QFont("Microsoft YaHei"); f.setPixelSize(12)
        p.setFont(f); p.setPen(QColor("#dbeafe"))
        inner = box.adjusted(6, 0, -6, -1)
        p.drawText(inner, Qt.AlignCenter,
                   QFontMetrics(f).elidedText(self._msg, Qt.ElideRight, inner.width()))

    def _paint_interaction_fx(self, p):
        """绘制不会污染角色 renderer 的临时粒子和触摸波纹。"""
        for ripple in getattr(self, "_ripples", ()):
            ratio = max(0.0, min(1.0, ripple["life"] / ripple["max_life"]))
            color = QColor(self.INTERACTION_MODES[self._interaction_mode]["color"])
            color.setAlpha(int(120 * ratio))
            radius = ripple["radius"]
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, max(.7, 1.4 * ratio)))
            p.drawEllipse(int(ripple["x"] - radius), int(ripple["y"] - radius),
                          int(radius * 2), int(radius * 2))

        for particle in getattr(self, "_particles", ()):
            ratio = max(0.0, min(1.0,
                                 particle["life"] / particle["max_life"]))
            color = QColor(particle["color"])
            color.setAlpha(int(225 * ratio))
            x, y, size = particle["x"], particle["y"], particle["size"]
            kind = particle["kind"]
            p.save()
            p.translate(x, y)
            p.rotate(math.degrees(particle["rotation"]))
            if kind == "bubble":
                fill = QColor(color)
                fill.setAlpha(int(45 * ratio))
                p.setBrush(fill)
                p.setPen(QPen(color, 1.0))
                p.drawEllipse(int(-size), int(-size), int(size * 2), int(size * 2))
                shine = QColor(255, 255, 255, int(155 * ratio))
                p.setPen(Qt.NoPen)
                p.setBrush(shine)
                p.drawEllipse(int(-size * .38), int(-size * .44),
                              max(1, int(size * .42)), max(1, int(size * .35)))
            elif kind == "heart":
                heart = QPainterPath()
                heart.moveTo(0, size * .85)
                heart.cubicTo(-size * 2.0, -size * .25, -size * .95,
                              -size * 1.75, 0, -size * .55)
                heart.cubicTo(size * .95, -size * 1.75, size * 2.0,
                              -size * .25, 0, size * .85)
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.drawPath(heart)
            else:
                # star / spark：不依赖 emoji 字体，打包后的 Windows 也稳定。
                p.setPen(QPen(color, max(1.0, size * .36),
                              Qt.SolidLine, Qt.RoundCap))
                arm = size * (1.35 if kind == "star" else .9)
                p.drawLine(int(-arm), 0, int(arm), 0)
                p.drawLine(0, int(-arm), 0, int(arm))
                if kind == "star":
                    diagonal = arm * .62
                    p.drawLine(int(-diagonal), int(-diagonal),
                               int(diagonal), int(diagonal))
                    p.drawLine(int(-diagonal), int(diagonal),
                               int(diagonal), int(-diagonal))
            p.restore()

    # ── 交互 ──────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # 默认“自由移动”保留传统桌宠的左键拖动；互动模式用 Shift+拖动移动，
            # 避免与“拉长长”/“抛一下”的拖动手势冲突。
            self._window_dragging = (
                self._interaction_mode == "move" or
                bool(event.modifiers() & Qt.ShiftModifier)
            )
            self._dragged = False
            if self._window_dragging:
                self.cancel_interaction()
                self._drag_from = event.globalPos() - self.frameGeometry().topLeft()
                self.setCursor(Qt.ClosedHandCursor)
            else:
                self._drag_from = None
                self._begin_interaction(event.x(), event.y(),
                                        event.globalX(), event.globalY())
            event.accept()
        elif event.button() == Qt.RightButton:
            self.cancel_interaction()
            self._show_menu(event.globalPos())
            event.accept()

    def mouseMoveEvent(self, event):
        if not event.buttons() & Qt.LeftButton:
            return
        if self._window_dragging and self._drag_from is not None:
            self.move(event.globalPos() - self._drag_from)
            self._dragged = True
            event.accept()
        elif self._interaction_active:
            self._update_drag_interaction(event.x(), event.y(),
                                          event.globalX(), event.globalY())
            start_x, start_y = self._interaction_global_start
            self._dragged = (math.hypot(event.globalX() - start_x,
                                        event.globalY() - start_y) > 5.0)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._window_dragging and self._drag_from is not None:
                self._drag_from = None
                self._snap_edge()
                if self._on_moved:
                    self._on_moved(self.x(), self.y())
            elif self._interaction_active:
                self._finish_interaction(self._dragged)
            self._window_dragging = False
            self.setCursor(Qt.PointingHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            if event.modifiers() & Qt.ControlModifier and self._open_app:
                self._open_app()
            else:
                self.trigger_surprise()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def enterEvent(self, event):
        if self._total_secs and self._state != "off":
            m, s = divmod(self._left_secs, 60)
            self.say(f"距离休息还有 {m:02d}:{s:02d}", 2500)
        super().enterEvent(event)

    def _poke(self):
        self._squash = 1.0
        self._blink = 4
        self._neutral_interaction()
        self._interaction_velocity["scale_x"] += .30
        self._interaction_velocity["scale_y"] -= .42
        self._spawn_particles("spark", 3)
        self._announce_interaction()

    def _show_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("QMenu{background:#161b22;color:#c9d1d9;"
                           "border:1px solid #30363d;padding:4px;}"
                           "QMenu::item{padding:5px 24px 5px 14px;}"
                           "QMenu::item:selected{background:#21262d;}")
        acts = {}
        pet_kind_actions = {}
        interaction_actions = {}
        style_menu = menu.addMenu("切换宠物")
        interaction_menu = menu.addMenu("互动玩法")
        for label, cb in [("打开主界面", self._open_app),
                          ("立即休息一下", self._rest_now),
                          ("切换护眼", self._toggle_care),
                          (None, None),
                          ("藏起来", self._hide_pet),
                          ("退出程序", self._quit_app)]:
            if label is None:
                menu.addSeparator(); continue
            act = menu.addAction(label)
            act.setEnabled(cb is not None)
            acts[act] = cb
        for pet_kind, info in self.PET_STYLES.items():
            act = style_menu.addAction(info["label"])
            act.setCheckable(True)
            act.setChecked(pet_kind == self._pet_kind)
            pet_kind_actions[act] = pet_kind
        for mode, info in self.INTERACTION_MODES.items():
            act = interaction_menu.addAction(info["label"])
            act.setCheckable(True)
            act.setChecked(mode == self._interaction_mode)
            act.setToolTip(info["hint"])
            interaction_actions[act] = mode
        surprise = menu.addAction("来个小惊喜")
        acts[surprise] = self.trigger_surprise
        chosen = menu.exec_(pos)
        if chosen in pet_kind_actions:
            self.set_pet_kind(pet_kind_actions[chosen])
        elif chosen in interaction_actions:
            self.set_interaction_mode(interaction_actions[chosen])
        else:
            cb = acts.get(chosen)
            if cb:
                cb()


class PetPreview(DesktopPet):
    """复用桌宠 renderer 的嵌入式预览，不创建桌面窗口。"""

    def __init__(self, pet_kind, parent=None, animated=True, halo=True,
                 decoration=DesktopPet.DEFAULT_DECORATION,
                 interaction_mode=DesktopPet.DEFAULT_INTERACTION_MODE, outfit=None):
        QWidget.__init__(self, parent)
        self._pet_kind = (pet_kind if pet_kind in self.PET_STYLES
                          else self.DEFAULT_PET_KIND)
        self._state = "idle"
        self._phase = 0.35
        self._blink = 0
        self._next_blink = 55
        self._squash = 0.0
        self._look = (0.0, 0.0)
        self._left_secs = 0
        self._total_secs = 0
        self._msg = ""
        self._halo = halo
        self._animated = animated
        self._interaction_mode = self.normalize_interaction_mode(interaction_mode)
        self._on_interaction_mode_changed = None
        self._init_interaction_state()
        self._outfit = ()
        self.set_outfit((decoration,) if outfit is None else outfit)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet("background:transparent;border:none;")

        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._tick_preview)
        self._preview_timer.setInterval(80)

    def showEvent(self, event):
        QWidget.showEvent(self, event)
        if self._animated:
            self._preview_timer.start()

    def hideEvent(self, event):
        self._preview_timer.stop()
        QWidget.hideEvent(self, event)

    def _tick_preview(self):
        self._phase += 0.08
        self._tick_interaction(.08)
        if self._blink > 0:
            self._blink -= 1
        else:
            self._next_blink -= 1
            if self._next_blink <= 0:
                self._blink = 3
                self._next_blink = 55 + int(abs(math.sin(self._phase)) * 40)
        self.update()

    def set_pet_kind(self, pet_kind):
        if pet_kind in self.PET_STYLES and pet_kind != self._pet_kind:
            self._pet_kind = pet_kind
            self.update()

    def set_state(self, state):
        if (state != self._state
                and state in self.PET_STYLES[self._pet_kind]["palette"]):
            self._state = state
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._halo:
            side = min(self.width(), self.height()) - 12
            halo = QRect((self.width() - side) // 2,
                         (self.height() - side) // 2, side, side)
            p.setPen(QPen(QColor(76, 172, 184, 45), 1))
            p.setBrush(QColor(56, 126, 145, 18))
            p.drawEllipse(halo)
            inner = halo.adjusted(18, 18, -18, -18)
            p.setPen(QPen(QColor(76, 172, 184, 28), 1))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(inner)
            # 参考图里的轻量星光点缀，让预览区不再只有一圈空 halo。
            p.setPen(Qt.NoPen)
            for x, y, size, color in (
                (0.17, 0.48, 9, QColor("#8de5dc")),
                (0.82, 0.28, 7, QColor("#e6c878")),
                (0.86, 0.67, 8, QColor("#9ec8e5")),
                (0.20, 0.76, 5, QColor("#86a9c2")),
            ):
                cx, cy = self.width() * x, self.height() * y
                star = QPainterPath()
                star.moveTo(cx, cy - size)
                star.lineTo(cx + size * .28, cy - size * .28)
                star.lineTo(cx + size, cy)
                star.lineTo(cx + size * .28, cy + size * .28)
                star.lineTo(cx, cy + size)
                star.lineTo(cx - size * .28, cy + size * .28)
                star.lineTo(cx - size, cy)
                star.lineTo(cx - size * .28, cy - size * .28)
                star.closeSubpath()
                p.setBrush(color); p.drawPath(star)

        available_width = max(1, self.width() - (22 if self._halo else 4))
        available_height = max(1, self.height() - (12 if self._halo else 2))
        scale = min(available_width / self.W, available_height / self.H)
        draw_width = self.W * scale
        draw_height = self.H * scale
        p.translate((self.width() - draw_width) / 2,
                    (self.height() - draw_height) / 2)
        p.scale(scale, scale)
        self._paint_scene(p, show_bar=False, show_bubble=False)


class PetSkinCard(QPushButton):
    def __init__(self, pet_kind, info, parent=None):
        super().__init__(parent)
        self.pet_kind = pet_kind
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(92)
        self.setMinimumHeight(82)
        self.setMaximumHeight(112)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("""
            QPushButton { background:#172232; border:1px solid #273649;
                border-radius:10px; }
            QPushButton:hover { background:#1b2a3c; border-color:#3b5368; }
            QPushButton:checked { background:#18343c; border:1px solid #60d8ce; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 3, 5, 5)
        layout.setSpacing(0)
        self.preview = PetPreview(pet_kind, self, animated=False, halo=False)
        self.preview.setMinimumHeight(58)
        layout.addWidget(self.preview, 1)
        label = QLabel(info["label"])
        label.setAlignment(Qt.AlignCenter)
        label.setAttribute(Qt.WA_TransparentForMouseEvents)
        label.setStyleSheet("background:transparent;border:none;color:#b8c5d5;font-size:11px;")
        layout.addWidget(label)


class PetDecorationPreview(PetPreview):
    ICON_BOUNDS = {
        "scarf": QRectF(40, 60, 72, 40),
        "sprout": QRectF(56, -28, 42, 36),
        "star_pin": QRectF(100, -3, 23, 23),
        "night_cap": QRectF(43, -20, 65, 37),
        "round_glasses": QRectF(45, 25, 60, 25),
        "heart_badge": QRectF(95, 18, 29, 28),
        "moon_charm": QRectF(48, 52, 55, 39),
        "tiny_crown": QRectF(55, -19, 42, 29),
    }

    def __init__(self, decoration, parent=None):
        super().__init__("seagull", parent, animated=False, halo=False,
                         decoration=decoration)

    def paintEvent(self, event):
        decoration = self._outfit[0]
        bounds = self.ICON_BOUNDS[decoration]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.38)
        scale = min(max(1, self.width() - 6) / bounds.width(),
                    max(1, self.height() - 4) / bounds.height())
        painter.translate(self.width() / 2, self.height() / 2)
        painter.scale(scale, scale)
        painter.translate(-bounds.center())
        self._paint_decoration(painter, 0, decoration)


class PetOutfitCard(QPushButton):
    def __init__(self, decoration, parent=None):
        super().__init__(parent)
        self.decoration = decoration
        self._status = None
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(86)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName(DesktopPet.DECORATIONS[decoration]["label"])
        self.setStyleSheet(
            "QPushButton{background:#192737;border:1px solid #2c3d4e;border-radius:9px;}"
            "QPushButton:hover{background:#213749;border-color:#567a88;}"
            "QPushButton:checked{background:#1b393e;border-color:#60d8ce;}"
            "QPushButton:disabled{background:#172231;border-color:#263443;}"
            "QLabel{background:transparent;border:none;color:#a1b9c9;font-size:10px;}"
            "QLabel:disabled{color:#65788e;}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 4, 5, 5)
        layout.setSpacing(2)
        icon_row = QHBoxLayout()
        icon_row.setSpacing(0)
        self.icon = PetDecorationPreview(decoration, self)
        self.icon.setFixedHeight(38)
        self.marker = QLabel()
        self.marker.setFixedWidth(12)
        self.marker.setAlignment(Qt.AlignTop | Qt.AlignRight)
        icon_row.addWidget(self.icon, 1)
        icon_row.addWidget(self.marker)
        layout.addLayout(icon_row)
        label = QLabel(DesktopPet.DECORATIONS[decoration]["label"])
        label.setAlignment(Qt.AlignCenter)
        self.detail = QLabel()
        self.detail.setAlignment(Qt.AlignCenter)
        self.detail.setStyleSheet("font-size:9px;color:#7f9ba9;")
        layout.addWidget(label)
        layout.addWidget(self.detail)
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_status(self, unlocked, equipped, remaining):
        status = (unlocked, equipped, remaining)
        if status == self._status:
            return
        self._status = status
        self.setEnabled(unlocked)
        self.setChecked(equipped)
        self.marker.setText("✓" if equipped else "")
        self.marker.setStyleSheet("color:#60d8ce;font-size:12px;font-weight:700;")
        self.detail.setText(
            "已穿戴" if equipped else ("点击穿戴" if unlocked else f"还需 {remaining} 次")
        )
        if unlocked:
            hint = "点击脱下" if equipped else "点击穿戴"
            if PetProgress.OUTFIT_RULES[self.decoration]["slot"] == "head":
                hint += "；小芽与晚安帽共用头饰位置"
        else:
            hint = f"再完整完成 {remaining} 次休息解锁，跳过休息不计入成长"
        self.setToolTip(hint)
        self.setAccessibleDescription(hint)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._status is not None and not self._status[0]:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor("#708096"), 1.4))
            painter.setBrush(Qt.NoBrush)
            painter.drawArc(QRectF(self.width() - 15, 7, 6, 8), 0, 180 * 16)
            painter.setBrush(QColor("#708096"))
            painter.drawRoundedRect(QRectF(self.width() - 16, 11, 8, 7), 1.5, 1.5)


class CareEyesApp(QWidget):
    hotkey_brightness = pyqtSignal(int)
    hotkey_temperature = pyqtSignal(int)
    hotkey_toggle = pyqtSignal()

    def __init__(self, activation_message=0):
        super().__init__()
        self._activation_message = activation_message
        self._quitting = False
        self._session_locked = False
        self._suspended = False
        self._rest_deferred = False
        self._pause_reason = ""
        self._settings_error = ""
        self.overlay = None
        self.pet = None
        # ── 读取系统主题色 (#10) ──
        self._accent = _read_system_accent()

        # 默认状态
        self.temp = 5000; self.bright = 1.0
        self.is_enabled = True; self.auto_mode = False
        self.super_dim = False; self.super_dim_alpha = 80
        self.rest_interval_min = 45; self.rest_duration_sec = 20
        self.force_rest = False          # #9 强制休息
        self.autostart = False; self.sound_enabled = True
        self.pet_enabled = True
        self.pet_kind = DesktopPet.DEFAULT_PET_KIND
        self.pet_interaction_mode = DesktopPet.DEFAULT_INTERACTION_MODE
        self.pet_pos = None
        self.today_minutes = 0; self.break_count = 0
        self.week_data = {}
        self._next_rest_secs = self.rest_interval_min * 60
        self._warned_1min = False
        self._transition = SmoothTransition(self)
        self._dim_mgr = DimManager()     # #5 多屏超暗管理器
        self._metrics = SystemMetricsCollector()

        self.hotkey_brightness.connect(self._hk_bright)
        self.hotkey_temperature.connect(self._hk_temp)
        self.hotkey_toggle.connect(self._hk_toggle)

        self.load_settings()
        self._work_clock = WorkClock(self.rest_interval_min * 60)
        self._activity = WindowsActivityMonitor()
        self.init_ui()
        self._activity.register(int(self.winId()))
        self.init_tray()
        self.init_pet()
        self.init_timers()
        self._sync_work_clock()
        self._refresh_today_summary()
        self._refresh_countdown_label()
        self._refresh_stats()
        self.init_hotkeys()
        if self.super_dim and self.is_enabled:
            self._dim_mgr.show(self.super_dim_alpha)
        if self.auto_mode and self.is_enabled:
            self._auto_mode_tick()
        self.apply_effect()

        # #2 权限检测：低权限时托盘提示
        if not _is_admin():
            QTimer.singleShot(2000, lambda: self.tray.showMessage(
                APP_TITLE,
                "当前以普通权限运行。若护眼对管理员进程无效，请右键以管理员身份运行。",
                QSystemTrayIcon.Warning, 6000
            ))

    # ══════════════════════════════════════════
    def init_timers(self):
        # #1 守护频率提升至 800ms
        self.guard_timer = QTimer(self)
        self.guard_timer.timeout.connect(self._guard_apply)
        if self.is_enabled:
            self.guard_timer.start(800)

        self.stat_timer = QTimer(self)
        self.stat_timer.timeout.connect(self._update_stat)
        self.stat_timer.start(60_000)

        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self._refresh_countdown)
        self.countdown_timer.start(1000)

        # 合并高频滑条事件，显示设备写入频率最多 25Hz。
        self._effect_timer = QTimer(self)
        self._effect_timer.setSingleShot(True)
        self._effect_timer.setInterval(40)
        self._effect_timer.timeout.connect(self.apply_effect)

        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._auto_mode_tick)
        self.auto_timer.start(60_000)

        # 系统状态只用于展示，单独采样，避免影响护眼/休息定时器。
        self.metrics_timer = QTimer(self)
        self.metrics_timer.timeout.connect(self._refresh_system_metrics)
        self.metrics_timer.setInterval(2_000)
        self.pages.currentChanged.connect(self._sync_metrics_timer)

        # #3 多显示器热插拔监听
        QApplication.instance().primaryScreenChanged.connect(self._on_screen_change)
        try:
            QApplication.instance().screenAdded.connect(self._on_screen_change)
            QApplication.instance().screenRemoved.connect(self._on_screen_change)
        except Exception:
            pass

    # ══════════════════════════════════════════
    #  桌宠
    # ══════════════════════════════════════════
    def init_pet(self):
        self.pet = DesktopPet(
            open_app=self._open_main,
            hide_pet=lambda: self._on_pet_toggle(False),
            quit_app=self._quit_app,
            rest_now=self.show_rest_overlay,
            toggle_care=self._hk_toggle,
            on_moved=self._on_pet_moved,
            pet_kind=self.pet_kind,
            outfit=self._pet_progress.outfit,
            interaction_mode=self.pet_interaction_mode,
            on_interaction_mode_changed=self._on_pet_interaction_mode_changed,
        )
        self.pet.setVisible(False)
        self.pet.set_countdown(self._next_rest_secs, self.rest_interval_min * 60)
        if not self.is_enabled:
            self.pet.set_state("off")
        if self.pet_enabled:
            self._show_pet()

    def _show_pet(self):
        if self.pet is None:
            return
        if not self.pet.isVisible():
            self.pet.place(self.pet_pos)
            self.pet.show()
        self.pet.raise_()

    def _hide_pet(self):
        if self.pet is not None:
            self.pet.hide()

    def _set_pet_kind(self, pet_kind):
        if pet_kind not in DesktopPet.PET_STYLES or pet_kind == self.pet_kind:
            return
        self.pet_kind = pet_kind
        if self.pet is not None:
            self.pet.set_pet_kind(pet_kind)
        self._sync_pet_page()
        self._schedule_save()

    def _set_pet_interaction_mode(self, mode):
        mode = DesktopPet.normalize_interaction_mode(mode)
        changed = mode != self.pet_interaction_mode
        self.pet_interaction_mode = mode
        if self.pet is not None and self.pet.interaction_mode != mode:
            self.pet.set_interaction_mode(mode)
        self._sync_pet_page()
        if changed:
            self._schedule_save()

    def _on_pet_interaction_mode_changed(self, mode):
        """桌面右键菜单改玩法时，把选择同步回主界面和配置。"""
        mode = DesktopPet.normalize_interaction_mode(mode)
        if mode == self.pet_interaction_mode:
            return
        self.pet_interaction_mode = mode
        self._sync_pet_page()
        self._schedule_save()

    def _trigger_pet_surprise(self):
        if self.pet is not None:
            self.pet.trigger_surprise()

    def _toggle_pet_decoration(self, decoration):
        if self._pet_progress.toggle_decoration(decoration):
            self._sync_pet_progress()
            self._save_settings()

    def _sync_pet_progress(self):
        progress = self._pet_progress
        if self.pet is not None:
            self.pet.set_outfit(progress.outfit)
        controls = vars(self)
        preview = controls.get("pet_preview")
        if preview is not None:
            preview.set_outfit(progress.outfit)
        if "pet_level_label" not in controls:
            return
        self.pet_level_label.setText(f"Lv. {progress.level}")
        self.pet_experience_label.setText(
            f"{progress.level_experience} / {progress.experience_per_level} 成长值"
        )
        self.pet_experience_progress.setRange(0, progress.experience_per_level)
        self.pet_experience_progress.setValue(progress.level_experience)
        self.pet_growth_label.setText(
            f"累计完成 {progress.completed_rests} 次休息 · {progress.experience} 成长值"
        )
        for decoration, button in self.pet_outfit_buttons.items():
            required = PetProgress.OUTFIT_RULES[decoration]["rests"]
            button.set_status(
                progress.is_unlocked(decoration), decoration in progress.outfit,
                max(0, required - progress.completed_rests),
            )
        for button in self.pet_skin_buttons.values():
            button.preview.set_outfit(progress.outfit)
        reward = progress.next_unlock
        if reward is None:
            self.pet_reward_icon.set_decoration(next(reversed(DesktopPet.DECORATIONS)))
            self.pet_reward_title.setText("衣柜已集齐")
            self.pet_reward_remaining.setText("继续休息，陪伴等级继续成长")
            self.pet_reward_progress.setRange(0, 1)
            self.pet_reward_progress.setValue(1)
            total = len(PetProgress.OUTFIT_RULES)
            self.pet_reward_count.setText(f"{total} / {total}")
        else:
            decoration, required = reward
            self.pet_reward_icon.set_decoration(decoration)
            self.pet_reward_title.setText(
                f"下一个礼物 · {DesktopPet.DECORATIONS[decoration]['label']}"
            )
            self.pet_reward_remaining.setText(
                f"再完成 {required - progress.completed_rests} 次休息解锁"
            )
            self.pet_reward_progress.setRange(0, required)
            self.pet_reward_progress.setValue(progress.completed_rests)
            self.pet_reward_count.setText(f"{progress.completed_rests} / {required}")

    def _complete_pet_rest(self):
        previous_level = self._pet_progress.level
        unlocked = self._pet_progress.complete_rest()
        self._sync_pet_progress()
        self._save_settings()
        message = f"休息完成，成长值 +{PetProgress.EXPERIENCE_PER_REST}"
        if self._pet_progress.level > previous_level:
            message += f" · 升至 Lv. {self._pet_progress.level}"
        if unlocked:
            names = "、".join(DesktopPet.DECORATIONS[item]["label"] for item in unlocked)
            message += f" · 解锁{names}"
        self.tray.showMessage(APP_TITLE, message, QSystemTrayIcon.Information, 5000)
        if self.pet is not None:
            self.pet.say(message, 5000)

    def _open_main(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_pet_moved(self, x, y):
        self.pet_pos = [int(x), int(y)]
        self._schedule_save()

    def _pet_state(self, refresh_page=True):
        """根据当前护眼/休息状态推导桌宠表情。"""
        state = self._current_pet_state()
        if self.pet is not None:
            self.pet.set_state(state)
        if refresh_page:
            self._sync_pet_page()

    def _on_pet_toggle(self, state):
        self.pet_enabled = bool(state)
        if self.pet_enabled:
            self._show_pet()
        else:
            self._hide_pet()
        if hasattr(self, "pet_action"):
            self.pet_action.blockSignals(True)
            self.pet_action.setChecked(self.pet_enabled)
            self.pet_action.blockSignals(False)
        self._sync_pet_page()
        self._schedule_save()

    def _current_pet_state(self):
        if not self.is_enabled:
            return "off"
        if self.overlay is not None and self.overlay.isVisible():
            return "resting"
        if self._next_rest_secs <= 60:
            return "tired"
        return "idle"

    def _sync_pet_page(self):
        controls = vars(self)
        pet_enable_toggle = controls.get("pet_enable_toggle")
        if pet_enable_toggle is not None:
            pet_enable_toggle.blockSignals(True)
            pet_enable_toggle.setChecked(self.pet_enabled)
            pet_enable_toggle.blockSignals(False)
        pet_status_label = controls.get("pet_status_label")
        if pet_status_label is not None:
            pet_status_label.setText(
                "桌宠已开启" if self.pet_enabled else "桌宠已关闭"
            )
            color = "#a3e1d7" if self.pet_enabled else "#6e7681"
            pet_status_label.setStyleSheet(
                f"color:{color};font-size:12px;background:transparent;"
            )
        pet_visibility_button = controls.get("pet_visibility_button")
        if pet_visibility_button is not None:
            pet_visibility_button.setText(
                "正在使用" if self.pet_enabled else "显示桌宠"
            )
        pet_preview = controls.get("pet_preview")
        if pet_preview is not None:
            pet_preview.set_pet_kind(self.pet_kind)
            pet_preview.set_interaction_mode(self.pet_interaction_mode)
        pet_name_label = controls.get("pet_name_label")
        if pet_name_label is not None:
            info = DesktopPet.PET_STYLES[self.pet_kind]
            pet_name_label.setText(info["label"])
            controls["pet_tagline_label"].setText(info["tagline"])
        pet_skin_buttons = controls.get("pet_skin_buttons")
        if pet_skin_buttons is not None:
            for pet_kind, button in pet_skin_buttons.items():
                button.blockSignals(True)
                button.setChecked(pet_kind == self.pet_kind)
                button.blockSignals(False)
        pet_interaction_hint_label = controls.get("pet_interaction_hint_label")
        if pet_interaction_hint_label is not None:
            interaction = DesktopPet.INTERACTION_MODES[self.pet_interaction_mode]
            move_hint = ("左键拖动移动" if self.pet_interaction_mode == "move"
                         else "Shift+拖动移动")
            pet_interaction_hint_label.setText(
                f"{interaction['label']}：{interaction['hint']} · {move_hint}"
            )
        pet_interaction_buttons = controls.get("pet_interaction_buttons")
        if pet_interaction_buttons is not None:
            for mode, button in pet_interaction_buttons.items():
                button.blockSignals(True)
                button.setChecked(mode == self.pet_interaction_mode)
                button.blockSignals(False)
        self._refresh_pet_status()

    def _refresh_pet_status(self):
        """只刷新每秒会变化的桌宠状态，避免重写整页静态控件。"""
        controls = vars(self)
        state = self._current_pet_state()
        pet_preview = controls.get("pet_preview")
        if pet_preview is not None:
            pet_preview.set_state(state)
        pet_mood_label = controls.get("pet_mood_label")
        if pet_mood_label is not None:
            mood_text = {
                "idle": "精神在线",
                "tired": "该歇会儿了",
                "resting": "一起休息中",
                "off": "护眼已暂停",
            }[state]
            speech = {
                "idle": "再忙，也要给眼睛放个小假。",
                "tired": "快到休息时间啦，收个尾。",
                "resting": "闭上眼睛，放松一会儿。",
                "off": "护眼已关闭，别熬太久。",
            }[state]
            pet_mood_label.setText(mood_text)
            controls["pet_speech_label"].setText(speech)
            if not self.is_enabled:
                controls["pet_next_rest_label"].setText("休息计时当前已暂停")
                progress = 0
            elif self.overlay is not None and self.overlay.isVisible():
                controls["pet_next_rest_label"].setText("休息结束后重新开始计时")
                progress = 100
            else:
                minutes, seconds = divmod(max(0, self._next_rest_secs), 60)
                controls["pet_next_rest_label"].setText(
                    f"距离下次休息 {minutes:02d}:{seconds:02d}"
                )
                total = (5 * 60 if self._rest_deferred
                         else self.rest_interval_min * 60)
                progress = int(100 * (1 - self._next_rest_secs / max(1, total)))
            controls["pet_page_progress"].setValue(max(0, min(100, progress)))

    # ══════════════════════════════════════════
    def init_hotkeys(self):
        try:
            from pynput import keyboard as kb
            self._hk_pressed = set()

            def _on_press(key):
                try:
                    self._hk_pressed.add(key)
                    ctrl = {kb.Key.ctrl, kb.Key.ctrl_l, kb.Key.ctrl_r}
                    alt  = {kb.Key.alt,  kb.Key.alt_l,  kb.Key.alt_r}
                    if self._hk_pressed & ctrl and self._hk_pressed & alt:
                        if key == kb.Key.up:    self.hotkey_brightness.emit(+5)
                        elif key == kb.Key.down: self.hotkey_brightness.emit(-5)
                        elif key == kb.Key.right: self.hotkey_temperature.emit(+200)
                        elif key == kb.Key.left:  self.hotkey_temperature.emit(-200)
                        elif key == kb.Key.end:   self.hotkey_toggle.emit()
                except Exception: pass

            def _on_release(key):
                self._hk_pressed.discard(key)

            self._hk_listener = kb.Listener(
                on_press=_on_press, on_release=_on_release, daemon=True
            )
            self._hk_listener.start()
        except Exception:
            # pynput 在无桌面会话/权限受限时可能抛 OSError 或 RuntimeError；
            # 快捷键是可选能力，不应阻断主程序启动。
            self._hk_listener = None

    def _hk_bright(self, d):
        self.bright_slider.setValue(max(30, min(100, int(self.bright*100)+d)))
    def _hk_temp(self, d):
        self.temp_slider.setValue(max(2000, min(8000, self.temp+d)))
    def _hk_toggle(self):
        self.toggle.setChecked(not self.toggle.isChecked())
        self.toggle_master()

    # ══════════════════════════════════════════
    #  UI
    # ══════════════════════════════════════════
    def init_ui(self):
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(760, 560)
        self.resize(860, 640)
        self.setStyleSheet(self._qss())

        root = QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        root.addWidget(self._build_header())
        self.pages = QStackedWidget()
        for fn in [self._page_home, self._page_timer, self._page_stats,
                   self._page_pet, self._page_settings]:
            self.pages.addWidget(fn())
        root.addWidget(self.pages, 1)

    def _qss(self):
        ac = self._accent  # 系统强调色
        return f"""
        QWidget {{ background:#0d1117; color:#c9d1d9;
            font-family:'Segoe UI','Microsoft YaHei UI',sans-serif; font-size:13px; }}
        QSlider::groove:horizontal {{ height:5px; background:#21262d; border-radius:3px; }}
        QSlider::sub-page:horizontal {{
            background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {ac},stop:1 #8b5cf6);
            border-radius:3px; }}
        QSlider::handle:horizontal {{
            background:#fff; width:16px; height:16px;
            margin:-6px 0; border-radius:8px; border:2px solid {ac}; }}
        QScrollBar:vertical {{ width:0; }}
        QSpinBox {{ background:#161b22; color:#c9d1d9; border:1px solid #30363d;
            border-radius:5px; padding:4px 8px; min-width:70px; }}
        QProgressBar {{ background:#21262d; border:none; border-radius:3px;
            min-height:6px; max-height:6px; text-align:center; }}
        QProgressBar::chunk {{ background:{ac}; border-radius:3px; }}
        QCheckBox {{ spacing:8px; }}
        QCheckBox::indicator {{ width:15px; height:15px; border-radius:4px;
            border:1px solid #30363d; background:#161b22; }}
        QCheckBox::indicator:checked {{ background:{ac}; border-color:{ac}; }}
        """

    def _card(self):
        f = QFrame()
        f.setStyleSheet("background:#161b22;border-radius:8px;border:1px solid #21262d;")
        return f

    def _h2(self, t):
        l = QLabel(t); l.setStyleSheet("font-size:18px;font-weight:700;color:#e6edf3;")
        return l

    def _caption(self, t):
        l = QLabel(t); l.setStyleSheet("color:#8b949e;font-size:11px;letter-spacing:1px;")
        return l

    def _div(self):
        f = QFrame(); f.setFrameShape(QFrame.HLine)
        f.setStyleSheet("background:#21262d;max-height:1px;border:none;")
        return f

    # ── 顶部紧凑导航 ──
    def _build_header(self):
        header = QFrame()
        header.setFixedHeight(58)
        header.setStyleSheet("background:#010409;border-bottom:1px solid #21262d;")
        lay = QHBoxLayout(header)
        lay.setContentsMargins(18,0,14,0); lay.setSpacing(4)

        brand = QLabel("◉  CareEyes")
        brand.setStyleSheet("color:#0ea5e9;font-size:14px;font-weight:700;")
        brand.setMinimumWidth(102)
        lay.addWidget(brand)

        self.nav_btns = []
        for label in ["护眼", "休息", "统计", "桌宠", "设置"]:
            btn = QPushButton(label)
            btn.setCheckable(True); btn.setFixedSize(66, 32)
            ac = self._accent
            btn.setStyleSheet(f"""
                QPushButton {{ border:none;border-radius:6px;color:#6e7681;
                    font-size:12px;background:transparent; }}
                QPushButton:hover {{ color:#8b949e;background:rgba(255,255,255,0.03); }}
                QPushButton:checked {{ color:{ac};background:rgba(14,165,233,0.07);
                    border:1px solid {ac}; }}
            """)
            btn.clicked.connect(lambda _,i=len(self.nav_btns): self._nav(i))
            self.nav_btns.append(btn); lay.addWidget(btn)

        self.nav_btns[0].setChecked(True)
        lay.addStretch()
        self.today_stat = QLabel("今日 — 分钟")
        self.today_stat.setStyleSheet("color:#484f58;font-size:11px;")
        lay.addWidget(self.today_stat)
        return header

    # ══════════════════════════════════════════
    #  Page 0
    # ══════════════════════════════════════════
    def _page_home(self):
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(24,20,24,20); lay.setSpacing(0)

        top = QHBoxLayout()
        top.addWidget(self._h2("护眼控制")); top.addStretch()
        self.toggle_label = QLabel("已开启" if self.is_enabled else "已关闭")
        self.toggle_label.setStyleSheet(
            f"color:{self._accent};margin-right:8px;" if self.is_enabled
            else "color:#484f58;margin-right:8px;")
        self.toggle = AnimatedToggle()
        self.toggle.setChecked(self.is_enabled); self.toggle.clicked.connect(self.toggle_master)
        top.addWidget(self.toggle_label); top.addWidget(self.toggle)
        lay.addLayout(top); lay.addSpacing(14)

        # 自动模式
        auto_row = QHBoxLayout()
        auto_row.addWidget(QLabel("◐  昼夜自动模式")); auto_row.addStretch()
        self.auto_status_lbl = QLabel("")
        self.auto_status_lbl.setStyleSheet("color:#8b5cf6;font-size:12px;margin-right:8px;")
        self.auto_toggle = AnimatedToggle()
        self.auto_toggle.setChecked(self.auto_mode)
        self.auto_toggle.clicked.connect(self._on_auto_toggle)
        auto_row.addWidget(self.auto_status_lbl); auto_row.addWidget(self.auto_toggle)
        lay.addLayout(auto_row); lay.addSpacing(16)

        # 模式按钮
        lay.addWidget(self._caption("预设模式")); lay.addSpacing(7)
        mode_row = QHBoxLayout(); mode_row.setSpacing(6)
        self.mode_btns = {}
        for name, info in MODES.items():
            btn = QPushButton(f"{info['icon']}  {name}")
            btn.setFixedHeight(40)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setStyleSheet(self._mode_qss(False))
            btn.clicked.connect(lambda _,n=name: self.apply_preset(n))
            self.mode_btns[name] = btn; mode_row.addWidget(btn)
        lay.addLayout(mode_row); lay.addSpacing(14)

        # 滑条卡片
        card = self._card(); cl = QVBoxLayout(card)
        cl.setContentsMargins(20,15,20,15); cl.setSpacing(12)

        tr = QHBoxLayout()
        tr.addWidget(QLabel("◐  蓝光/色温"))
        self.temp_val = QLabel(f"{self.temp} K")
        self.temp_val.setStyleSheet("color:#0ea5e9;min-width:60px;")
        self.temp_slider = QSlider(Qt.Horizontal)
        self.temp_slider.setRange(2000,8000); self.temp_slider.setValue(self.temp)
        self.temp_slider.valueChanged.connect(self.on_slider_change)
        tr.addWidget(self.temp_slider); tr.addWidget(self.temp_val)
        cl.addLayout(tr); cl.addWidget(self._div())

        br = QHBoxLayout()
        br.addWidget(QLabel("○  屏幕亮度"))
        self.bright_val = QLabel(f"{int(self.bright*100)}%")
        self.bright_val.setStyleSheet("color:#0ea5e9;min-width:60px;")
        self.bright_slider = QSlider(Qt.Horizontal)
        self.bright_slider.setRange(30,100); self.bright_slider.setValue(int(self.bright*100))
        self.bright_slider.valueChanged.connect(self.on_slider_change)
        br.addWidget(self.bright_slider); br.addWidget(self.bright_val)
        cl.addLayout(br); cl.addWidget(self._div())

        # 超暗模式
        dim_row = QHBoxLayout()
        dim_row.addWidget(QLabel("□  超暗模式"))
        self.dim_toggle = AnimatedToggle()
        self.dim_toggle.setChecked(self.super_dim)
        self.dim_toggle.clicked.connect(self._on_dim_toggle)
        dim_row.addWidget(self.dim_toggle)
        dim_row.addSpacing(12)
        dim_row.addWidget(QLabel("强度"))
        dim_row.addSpacing(6)
        self.dim_slider = QSlider(Qt.Horizontal)
        self.dim_slider.setRange(20, 200)
        self.dim_slider.setValue(self.super_dim_alpha)
        self.dim_slider.setEnabled(self.super_dim)
        self.dim_slider.setMinimumWidth(120)
        self.dim_slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.dim_slider.valueChanged.connect(self._on_dim_alpha)
        dim_row.addWidget(self.dim_slider, 1)
        cl.addLayout(dim_row)

        lay.addWidget(card); lay.addStretch()
        self._sync_mode_selection()
        return page

    def _mode_qss(self, active):
        if active:
            return ("QPushButton{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
                    "stop:0 #0ea5e9,stop:1 #8b5cf6);color:#fff;border-radius:8px;"
                    "border:none;font-size:13px;font-weight:600;}")
        return ("QPushButton{background:#161b22;color:#8b949e;border-radius:8px;"
                "border:1px solid #30363d;font-size:13px;}"
                "QPushButton:hover{background:#1c2128;color:#e6edf3;}")

    def _sync_mode_selection(self):
        """让加载/重置后的滑条值与预设按钮状态保持一致。"""
        if not hasattr(self, "mode_btns"):
            return
        active_name = next(
            (name for name, preset in MODES.items()
             if self.temp == preset["temp"]
             and abs(self.bright - preset["bright"]) < 0.001),
            None,
        )
        if (hasattr(self, "_active_mode_name")
                and self._active_mode_name == active_name):
            return
        self._active_mode_name = active_name
        for name, preset in MODES.items():
            self.mode_btns[name].setStyleSheet(self._mode_qss(name == active_name))

    # ══════════════════════════════════════════
    #  Page 1
    # ══════════════════════════════════════════
    def _page_timer(self):
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(24,20,24,20); lay.setSpacing(12)
        lay.addWidget(self._h2("休息提醒"))

        card = self._card(); cl = QVBoxLayout(card)
        cl.setContentsMargins(22,16,22,16)
        cl.addWidget(self._caption("距下次休息"))
        self.next_rest_label = QLabel("45:00")
        self.next_rest_label.setStyleSheet(
            "color:#0ea5e9;font-size:40px;font-weight:900;")
        cl.addWidget(self.next_rest_label)
        self.fullscreen_warn = QLabel("")
        self.fullscreen_warn.setStyleSheet("color:#f97316;font-size:12px;")
        cl.addWidget(self.fullscreen_warn)
        self.snooze_status_label = QLabel("暂缓只影响提醒，护眼与用眼统计继续运行")
        self.snooze_status_label.setStyleSheet("color:#8b949e;font-size:11px;")
        self.snooze_status_label.setWordWrap(True)
        cl.addWidget(self.snooze_status_label)
        snooze_row = QHBoxLayout()
        self.snooze_button = QPushButton("免打扰")
        self.snooze_button.setMenu(self._create_snooze_menu())
        self.resume_reminders_button = QPushButton("恢复提醒")
        self.resume_reminders_button.clicked.connect(self._resume_reminders)
        self.resume_reminders_button.setEnabled(False)
        snooze_row.addWidget(self.snooze_button)
        snooze_row.addWidget(self.resume_reminders_button)
        snooze_row.addStretch()
        cl.addLayout(snooze_row)
        lay.addWidget(card)

        scard = self._card(); sl = QVBoxLayout(scard)
        sl.setContentsMargins(22,15,22,15); sl.setSpacing(10)
        lbl = QLabel("间隔设置"); lbl.setStyleSheet("color:#e6edf3;font-weight:600;")
        sl.addWidget(lbl)
        for row_lbl, attr, lo, hi, val in [
            ("工作时长（分钟）","interval_spin",5,120,self.rest_interval_min),
            ("休息时长（秒）",  "duration_spin",10,300,self.rest_duration_sec),
        ]:
            row = QHBoxLayout(); row.addWidget(QLabel(row_lbl))
            spin = QSpinBox(); spin.setRange(lo,hi); spin.setValue(val)
            setattr(self,attr,spin); row.addStretch(); row.addWidget(spin)
            sl.addLayout(row); sl.addWidget(self._div())

        ab = QPushButton("应用"); ab.setFixedHeight(33)
        ab.setStyleSheet("QPushButton{background:#0ea5e9;color:#fff;border-radius:7px;"
                         "font-weight:600;border:none;}"
                         "QPushButton:hover{background:#38bdf8;}")
        ab.clicked.connect(self.apply_timer_settings); sl.addWidget(ab)
        lay.addWidget(scard)

        tb = QPushButton("◉  立即测试"); tb.setFixedHeight(36)
        tb.setStyleSheet("QPushButton{background:#161b22;color:#8b949e;"
                         "border:1px solid #30363d;border-radius:8px;}"
                         "QPushButton:hover{color:#e6edf3;border-color:#0ea5e9;}")
        tb.clicked.connect(self.show_rest_overlay); lay.addWidget(tb)
        lay.addStretch()
        return page

    # ══════════════════════════════════════════
    #  Page 2
    # ══════════════════════════════════════════
    def _page_stats(self):
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(24,20,24,20); lay.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(self._h2("用眼统计"))
        header.addStretch()
        self.export_stats_button = QPushButton("导出 CSV")
        self.export_stats_button.setToolTip("导出最近 32 个自然日内已有的每日用眼分钟数，含今天")
        self.export_stats_button.clicked.connect(self._export_statistics)
        header.addWidget(self.export_stats_button)
        lay.addLayout(header)
        self.export_status_label = QLabel("")
        self.export_status_label.setStyleSheet("color:#8b949e;font-size:11px;")
        self.export_status_label.setWordWrap(True)
        lay.addWidget(self.export_status_label)

        cr = QHBoxLayout(); cr.setSpacing(10)
        for lbl_t, attr, unit, color in [
            ("今日用眼","stat_today","分钟","#0ea5e9"),
            ("本次连续","stat_session","分钟","#8b5cf6"),
            ("已休息",  "stat_breaks","次","#10b981"),
        ]:
            f = QFrame()
            f.setStyleSheet(f"background:#161b22;border-radius:10px;"
                            f"border-left:3px solid {color};border:1px solid #21262d;")
            fl = QVBoxLayout(f); fl.setContentsMargins(14,11,14,11)
            fl.addWidget(self._caption(lbl_t))
            val = QLabel("0"); val.setStyleSheet(f"color:{color};font-size:26px;font-weight:800;")
            setattr(self,attr,val); fl.addWidget(val)
            u = QLabel(unit); u.setStyleSheet("color:#484f58;font-size:11px;")
            fl.addWidget(u); cr.addWidget(f)
        lay.addLayout(cr)

        bot = QHBoxLayout(); bot.setSpacing(12)
        rc = self._card(); rl = QHBoxLayout(rc)
        rl.setContentsMargins(16,13,16,13)
        self.day_ring = RingProgress(100); rl.addWidget(self.day_ring)
        ri = QVBoxLayout()
        ri_lbl = QLabel("今日用眼目标"); ri_lbl.setStyleSheet("color:#484f58;font-size:11px;")
        ri.addWidget(ri_lbl)
        self.ring_sub = QLabel("≤ 480 分钟"); self.ring_sub.setStyleSheet("color:#484f58;font-size:11px;")
        ri.addWidget(self.ring_sub); ri.addStretch(); rl.addLayout(ri)
        bot.addWidget(rc, 1)

        cc = self._card(); cht = QVBoxLayout(cc)
        cht.setContentsMargins(14,11,14,11)
        cht.addWidget(self._caption("近7天用眼 (分钟)"))
        self.bar_chart = BarChart(); cht.addWidget(self.bar_chart)
        bot.addWidget(cc, 2)
        lay.addLayout(bot)

        # 轻量系统状态：帮助用户判断卡顿/高负载是否来自系统本身。
        system_card = self._card()
        system_grid = QGridLayout(system_card)
        system_grid.setContentsMargins(14, 9, 14, 9)
        system_grid.setHorizontalSpacing(16)
        system_grid.setVerticalSpacing(2)
        system_grid.addWidget(self._caption("系统状态"), 0, 0, 1, 4)
        metrics = [
            ("CPU", "system_cpu_value", "system_cpu_bar"),
            ("内存", "system_memory_value", "system_memory_bar"),
            ("磁盘", "system_disk_value", "system_disk_bar"),
            ("运行时间", "system_uptime_value", None),
        ]
        for col, (label_text, value_attr, bar_attr) in enumerate(metrics):
            label = QLabel(label_text)
            label.setStyleSheet("color:#6e7681;font-size:11px;")
            value = QLabel("—")
            value.setMinimumWidth(78)
            value.setStyleSheet("color:#e6edf3;font-size:13px;font-weight:600;")
            setattr(self, value_attr, value)
            system_grid.addWidget(label, 1, col)
            system_grid.addWidget(value, 2, col)
            if bar_attr:
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(0)
                bar.setTextVisible(False)
                setattr(self, bar_attr, bar)
                system_grid.addWidget(bar, 3, col)
            else:
                spacer = QFrame()
                spacer.setFixedHeight(6)
                system_grid.addWidget(spacer, 3, col)
            system_grid.setColumnStretch(col, 1)
        lay.addWidget(system_card)
        lay.addStretch()
        return page

    # ══════════════════════════════════════════
    #  Page 3
    # ══════════════════════════════════════════
    def _page_pet(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        self.pet_scroll = QScrollArea()
        self.pet_scroll.setWidgetResizable(True)
        self.pet_scroll.setFrameShape(QFrame.NoFrame)
        self.pet_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pet_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{width:6px;background:#101c29;margin:2px;}"
            "QScrollBar::handle:vertical{background:#30485a;min-height:24px;border-radius:3px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        self.pet_page_body = QWidget()
        self.pet_scroll.setWidget(self.pet_page_body)
        outer.addWidget(self.pet_scroll)
        lay = QVBoxLayout(self.pet_page_body)
        lay.setContentsMargins(22, 16, 22, 16)
        lay.setSpacing(12)

        heading = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title_box.addWidget(self._h2("给休息，找个小搭子。"))
        subtitle = QLabel("认真工作，也好好休息。让每一次放松，都变成一点小小的成长。")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color:#8295ac;font-size:12px;")
        title_box.addWidget(subtitle)
        heading.addLayout(title_box, 1)
        heading.addSpacing(16)

        status_pill = QFrame()
        status_pill.setObjectName("petStatusPill")
        status_pill.setStyleSheet(
            "QFrame#petStatusPill{background:#132a30;border:1px solid #254147;border-radius:18px;}"
        )
        status_layout = QHBoxLayout(status_pill)
        status_layout.setContentsMargins(14, 4, 9, 4)
        status_layout.setSpacing(9)
        status_dot = QLabel("●")
        status_dot.setStyleSheet(
            f"color:{self._accent};font-size:10px;background:transparent;border:none;"
        )
        self.pet_status_label = QLabel()
        self.pet_enable_toggle = AnimatedToggle()
        self.pet_enable_toggle.setChecked(self.pet_enabled)
        self.pet_enable_toggle.clicked.connect(self._on_pet_toggle)
        status_layout.addWidget(status_dot)
        status_layout.addWidget(self.pet_status_label)
        status_layout.addWidget(self.pet_enable_toggle)
        heading.addWidget(status_pill)
        lay.addLayout(heading)

        content = QHBoxLayout()
        content.setSpacing(12)

        hero = QFrame()
        hero.setObjectName("petHero")
        hero.setStyleSheet(
            "QFrame#petHero{background:#152638;border:1px solid #2b4252;"
            "border-radius:14px;}"
        )
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(16, 12, 16, 13)
        hero_layout.setSpacing(5)

        hero_top = QHBoxLayout()
        hero_caption = QLabel("正在陪伴你")
        hero_caption.setStyleSheet("color:#b4c6d5;font-size:12px;background:transparent;")
        hero_badge = QLabel("桌面预览")
        hero_badge.setAlignment(Qt.AlignCenter)
        hero_badge.setStyleSheet(
            "color:#a0b5c5;background:#213748;border-radius:10px;"
            "padding:3px 10px;font-size:10px;"
        )
        hero_top.addWidget(hero_caption)
        hero_top.addStretch()
        hero_top.addWidget(hero_badge)
        hero_layout.addLayout(hero_top)

        self.pet_speech_label = QLabel("再忙，也要给眼睛放个小假。")
        self.pet_speech_label.setAlignment(Qt.AlignCenter)
        self.pet_speech_label.setWordWrap(True)
        self.pet_speech_label.setStyleSheet(
            "color:#d8eee8;background:#28434c;border:1px solid #3c5c62;"
            "border-radius:11px;padding:7px;font-size:12px;"
        )
        hero_layout.addWidget(self.pet_speech_label)

        self.pet_preview = PetPreview(
            self.pet_kind, hero, animated=True, halo=True,
            interaction_mode=self.pet_interaction_mode,
            outfit=self._pet_progress.outfit,
        )
        self.pet_preview.setMinimumSize(250, 200)
        self.pet_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        hero_layout.addWidget(self.pet_preview, 1)

        pet_info = QHBoxLayout()
        pet_text = QVBoxLayout()
        pet_text.setSpacing(2)
        self.pet_name_label = QLabel()
        self.pet_name_label.setStyleSheet(
            "color:#edf4ff;font-size:20px;font-weight:700;background:transparent;"
        )
        self.pet_tagline_label = QLabel()
        self.pet_tagline_label.setWordWrap(True)
        self.pet_tagline_label.setStyleSheet(
            "color:#92a9ba;font-size:11px;background:transparent;"
        )
        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        name_row.addWidget(self.pet_name_label)
        self.pet_level_label = QLabel()
        self.pet_level_label.setAlignment(Qt.AlignCenter)
        self.pet_level_label.setStyleSheet(
            "color:#a6e7d6;background:#244448;border:none;border-radius:8px;"
            "padding:3px 8px;font-size:10px;font-weight:700;"
        )
        name_row.addWidget(self.pet_level_label, 0, Qt.AlignVCenter)
        name_row.addStretch()
        pet_text.addLayout(name_row)
        pet_text.addWidget(self.pet_tagline_label)
        pet_info.addLayout(pet_text, 1)
        self.pet_visibility_button = QPushButton()
        self.pet_visibility_button.setFixedSize(112, 38)
        self.pet_visibility_button.setCursor(Qt.PointingHandCursor)
        self.pet_visibility_button.setStyleSheet(
            "QPushButton{background:#60d8ce;color:#0c343b;border:none;"
            "border-radius:9px;font-weight:700;}"
            "QPushButton:hover{background:#7ce6dc;}"
        )
        self.pet_visibility_button.clicked.connect(
            lambda: self._on_pet_toggle(not self.pet_enabled)
        )
        pet_info.addWidget(self.pet_visibility_button, 0, Qt.AlignBottom)
        hero_layout.addLayout(pet_info)
        hero_layout.addSpacing(6)
        growth_row = QHBoxLayout()
        growth_title = QLabel("陪伴成长")
        growth_title.setStyleSheet("color:#a7bfca;font-size:10px;background:transparent;")
        self.pet_experience_label = QLabel()
        self.pet_experience_label.setStyleSheet(
            "color:#84b8bd;font-size:10px;background:transparent;"
        )
        growth_row.addWidget(growth_title)
        growth_row.addStretch()
        growth_row.addWidget(self.pet_experience_label)
        hero_layout.addLayout(growth_row)
        self.pet_experience_progress = QProgressBar()
        self.pet_experience_progress.setTextVisible(False)
        self.pet_experience_progress.setStyleSheet(
            "QProgressBar{background:#243d4c;border:none;border-radius:3px;}"
            "QProgressBar::chunk{background:#60d8ce;border-radius:3px;}"
        )
        hero_layout.addWidget(self.pet_experience_progress)
        self.pet_growth_label = QLabel()
        self.pet_growth_label.setStyleSheet(
            "color:#7e9dab;font-size:10px;background:transparent;"
        )
        hero_layout.addWidget(self.pet_growth_label)
        content.addWidget(hero, 5)

        side = QVBoxLayout()
        side.setSpacing(12)
        skin_card = self._card()
        skin_layout = QVBoxLayout(skin_card)
        skin_layout.setContentsMargins(13, 10, 13, 12)
        skin_layout.setSpacing(7)
        skin_head = QHBoxLayout()
        skin_head.setContentsMargins(0, 0, 0, 0)
        skin_title = QLabel("选择你的搭子")
        skin_title.setFixedHeight(22)
        skin_title.setStyleSheet(
            "color:#edf4ff;font-size:14px;font-weight:700;"
            "background:transparent;border:none;"
        )
        skin_count = QLabel(f"{len(DesktopPet.PET_STYLES)} 款外观 · 共享成长")
        skin_count.setFixedHeight(22)
        skin_count.setStyleSheet(
            "color:#8295ac;font-size:10px;background:transparent;border:none;"
        )
        skin_head.addWidget(skin_title)
        skin_head.addStretch()
        skin_head.addWidget(skin_count)
        skin_layout.addLayout(skin_head)

        self.pet_skin_buttons = {}
        self.pet_skin_scroller = QScrollArea()
        self.pet_skin_scroller.setWidgetResizable(True)
        self.pet_skin_scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.pet_skin_scroller.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pet_skin_scroller.setFrameShape(QFrame.NoFrame)
        self.pet_skin_scroller.setFixedHeight(105)
        self.pet_skin_scroller.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:horizontal{height:4px;background:#17222d;border:none;}"
            "QScrollBar::handle:horizontal{background:#4f7b86;border-radius:2px;min-width:32px;}"
            "QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0;}"
        )
        self.pet_skin_strip = QWidget()
        skin_row = QHBoxLayout(self.pet_skin_strip)
        skin_row.setContentsMargins(0, 0, 0, 5)
        skin_row.setSpacing(7)
        pet_order = list(DesktopPet.FEATURED_PETS) + [
            kind for kind in DesktopPet.PET_STYLES if kind not in DesktopPet.FEATURED_PETS
        ]
        for pet_kind in pet_order:
            info = DesktopPet.PET_STYLES[pet_kind]
            button = PetSkinCard(pet_kind, info, skin_card)
            button.setFixedWidth(112)
            button.clicked.connect(
                lambda _, kind=pet_kind: self._select_pet_skin(kind)
            )
            self.pet_skin_buttons[pet_kind] = button
            skin_row.addWidget(button)
        self.pet_skin_scroller.setWidget(self.pet_skin_strip)
        skin_layout.addWidget(self.pet_skin_scroller)
        side.addWidget(skin_card, 1)

        wardrobe = self._card()
        wardrobe_layout = QVBoxLayout(wardrobe)
        wardrobe_layout.setContentsMargins(13, 10, 13, 11)
        wardrobe_layout.setSpacing(6)
        wardrobe_title = QLabel("今天，穿什么？")
        wardrobe_title.setStyleSheet(
            "color:#edf4ff;font-size:14px;font-weight:700;background:transparent;border:none;"
        )
        wardrobe_layout.addWidget(wardrobe_title)
        wardrobe_hint = QLabel("完整休息获得奖励，点击穿戴或脱下。")
        wardrobe_hint.setWordWrap(True)
        wardrobe_hint.setStyleSheet(
            "color:#8295ac;font-size:10px;background:transparent;border:none;"
        )
        wardrobe_hint.setToolTip("围巾与别针可以叠穿；小芽和晚安帽共用一个头饰位置。")
        wardrobe_layout.addWidget(wardrobe_hint)
        self.pet_outfit_scroller = QScrollArea()
        self.pet_outfit_scroller.setWidgetResizable(True)
        self.pet_outfit_scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.pet_outfit_scroller.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pet_outfit_scroller.setFrameShape(QFrame.NoFrame)
        self.pet_outfit_scroller.setFixedHeight(96)
        self.pet_outfit_scroller.setStyleSheet(self.pet_skin_scroller.styleSheet())
        self.pet_outfit_strip = QWidget()
        outfit_row = QHBoxLayout(self.pet_outfit_strip)
        outfit_row.setContentsMargins(0, 0, 0, 5)
        outfit_row.setSpacing(6)
        self.pet_outfit_buttons = {}
        for decoration in DesktopPet.DECORATIONS:
            button = PetOutfitCard(decoration, wardrobe)
            button.setFixedWidth(112)
            button.clicked.connect(
                lambda _, selected=decoration: self._toggle_pet_decoration(selected)
            )
            self.pet_outfit_buttons[decoration] = button
            outfit_row.addWidget(button)
        self.pet_outfit_scroller.setWidget(self.pet_outfit_strip)
        wardrobe_layout.addWidget(self.pet_outfit_scroller)
        side.addWidget(wardrobe)

        reward = self._card()
        reward.setMaximumHeight(100)
        reward.setStyleSheet(
            "QFrame{background:#192833;border:1px solid #33434d;border-radius:10px;}"
            "QLabel{background:transparent;border:none;}"
        )
        reward_layout = QHBoxLayout(reward)
        reward_layout.setContentsMargins(13, 10, 13, 10)
        reward_layout.setSpacing(10)
        self.pet_reward_icon = PetDecorationPreview("star_pin", reward)
        self.pet_reward_icon.setFixedSize(42, 46)
        reward_layout.addWidget(self.pet_reward_icon)
        reward_text = QVBoxLayout()
        reward_text.setSpacing(5)
        self.pet_reward_title = QLabel()
        self.pet_reward_title.setWordWrap(True)
        self.pet_reward_title.setStyleSheet("color:#dacc9d;font-size:11px;font-weight:700;")
        reward_text.addWidget(self.pet_reward_title)
        reward_bar = QHBoxLayout()
        self.pet_reward_progress = QProgressBar()
        self.pet_reward_progress.setTextVisible(False)
        self.pet_reward_progress.setStyleSheet(
            "QProgressBar{background:#34424c;border:none;border-radius:3px;}"
            "QProgressBar::chunk{background:#d7c080;border-radius:3px;}"
        )
        self.pet_reward_count = QLabel()
        self.pet_reward_count.setStyleSheet("color:#c0b693;font-size:10px;")
        reward_bar.addWidget(self.pet_reward_progress, 1)
        reward_bar.addWidget(self.pet_reward_count)
        reward_text.addLayout(reward_bar)
        self.pet_reward_remaining = QLabel()
        self.pet_reward_remaining.setWordWrap(True)
        self.pet_reward_remaining.setStyleSheet("color:#95a0a6;font-size:10px;")
        reward_text.addWidget(self.pet_reward_remaining)
        reward_layout.addLayout(reward_text, 1)
        side.addWidget(reward)
        content.addLayout(side, 4)
        lay.addLayout(content, 1)

        companion = self._card()
        companion.setObjectName("petCompanion")
        companion.setStyleSheet(
            "QFrame#petCompanion{background:#15212e;border:1px solid #293e4d;border-radius:10px;}"
            "QLabel{background:transparent;border:none;}"
        )
        companion_layout = QVBoxLayout(companion)
        companion_layout.setContentsMargins(14, 10, 14, 11)
        companion_layout.setSpacing(5)
        companion_head = QHBoxLayout()
        companion_title = QLabel("下一次休息")
        companion_title.setStyleSheet("color:#edf4ff;font-weight:700;")
        self.pet_mood_label = QLabel()
        self.pet_mood_label.setStyleSheet("color:#60d8ce;font-size:11px;")
        companion_head.addWidget(companion_title)
        self.pet_next_rest_label = QLabel()
        self.pet_next_rest_label.setStyleSheet("color:#bacddc;font-size:12px;")
        companion_head.addWidget(self.pet_next_rest_label, 1)
        companion_head.addWidget(self.pet_mood_label)
        self.pet_play_toggle = QPushButton("互动玩法")
        self.pet_play_toggle.setCheckable(True)
        self.pet_play_toggle.setCursor(Qt.PointingHandCursor)
        self.pet_play_toggle.setFixedSize(76, 25)
        self.pet_play_toggle.setStyleSheet(
            "QPushButton{background:#21374a;color:#a6c9d8;border:none;border-radius:7px;"
            "font-size:10px;}QPushButton:checked{background:#1d474b;color:#b9eee1;}"
        )
        companion_head.addWidget(self.pet_play_toggle)
        companion_layout.addLayout(companion_head)
        self.pet_page_progress = QProgressBar()
        self.pet_page_progress.setRange(0, 100)
        self.pet_page_progress.setTextVisible(False)
        companion_layout.addWidget(self.pet_page_progress)

        self.pet_play_panel = QWidget()
        play_layout = QVBoxLayout(self.pet_play_panel)
        play_layout.setContentsMargins(0, 6, 0, 0)
        play_layout.setSpacing(6)
        play_head = QHBoxLayout()
        play_title = QLabel("互动玩法")
        play_title.setStyleSheet("color:#dce7f2;font-size:12px;font-weight:700;")
        self.pet_surprise_button = QPushButton("彩蛋")
        self.pet_surprise_button.setCursor(Qt.PointingHandCursor)
        self.pet_surprise_button.setFixedSize(48, 24)
        self.pet_surprise_button.setStyleSheet(
            "QPushButton{background:#263b51;color:#b9ddf6;border:1px solid #3c5871;"
            "border-radius:7px;font-size:10px;}"
            "QPushButton:hover{background:#304d68;color:#e2f4ff;}"
        )
        self.pet_surprise_button.clicked.connect(self._trigger_pet_surprise)
        play_head.addWidget(play_title)
        play_head.addStretch()
        play_head.addWidget(self.pet_surprise_button)
        play_layout.addLayout(play_head)
        self.pet_interaction_hint_label = QLabel()
        self.pet_interaction_hint_label.setWordWrap(True)
        self.pet_interaction_hint_label.setStyleSheet(
            "color:#8295ac;font-size:10px;background:transparent;"
        )
        play_layout.addWidget(self.pet_interaction_hint_label)
        interaction_row = QHBoxLayout()
        interaction_row.setSpacing(4)
        self.pet_interaction_buttons = {}
        for mode, info in DesktopPet.INTERACTION_MODES.items():
            button = QPushButton(info["label"])
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedHeight(28)
            button.setToolTip(info["hint"])
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setStyleSheet(
                "QPushButton{background:#1d2c3b;color:#91a7ba;border:1px solid #304558;"
                "border-radius:7px;font-size:10px;padding:0 3px;}"
                "QPushButton:hover{background:#243b4f;color:#d3e3ef;border-color:#52748d;}"
                "QPushButton:checked{background:#164550;color:#c9fffa;border-color:#60d8ce;}"
            )
            button.clicked.connect(
                lambda _, selected=mode: self._set_pet_interaction_mode(selected)
            )
            self.pet_interaction_buttons[mode] = button
            interaction_row.addWidget(button)
        play_layout.addLayout(interaction_row)
        companion_layout.addWidget(self.pet_play_panel)
        self.pet_play_toggle.toggled.connect(self.pet_play_panel.setVisible)
        self.pet_play_panel.hide()
        lay.addWidget(companion)

        self._sync_pet_page()
        self._sync_pet_progress()
        return page

    def _select_pet_skin(self, pet_kind):
        self._set_pet_kind(pet_kind)

    # ══════════════════════════════════════════
    #  Page 4
    # ══════════════════════════════════════════
    def _page_settings(self):
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(24,20,24,20); lay.setSpacing(12)
        lay.addWidget(self._h2("设置"))

        card = self._card(); cl = QVBoxLayout(card)
        cl.setContentsMargins(22,15,22,15); cl.setSpacing(10)

        for row_lbl, attr, default in [
            ("开机自动启动","autostart_cb",self.autostart),
            ("声音提示",   "sound_cb",    self.sound_enabled),
            ("强制休息（前10秒锁定跳过）","force_rest_cb", self.force_rest),
        ]:
            row = QHBoxLayout(); row.addWidget(QLabel(row_lbl))
            cb = QCheckBox(); cb.setChecked(default)
            setattr(self,attr,cb); row.addStretch(); row.addWidget(cb)
            cl.addLayout(row); cl.addWidget(self._div())

        self.autostart_cb.stateChanged.connect(self._on_autostart_change)
        self.sound_cb.stateChanged.connect(self._on_sound_toggle)
        self.force_rest_cb.stateChanged.connect(self._on_force_rest_toggle)

        hk = QLabel("全局快捷键\n"
                    "Ctrl+Alt+↑/↓  亮度 ±5%\n"
                    "Ctrl+Alt+←/→  色温 ±200K\n"
                    "Ctrl+Alt+End   开关护眼")
        hk.setStyleSheet("color:#484f58;font-size:11px;line-height:1.8;")
        cl.addWidget(hk)
        lay.addWidget(card)

        ic = self._card(); il = QVBoxLayout(ic)
        il.setContentsMargins(22,13,22,13)
        for k,v in [("版本",APP_VER),("配置文件",CONFIG_FILE)]:
            row = QHBoxLayout()
            kl=QLabel(k); kl.setStyleSheet("color:#484f58;")
            vl=QLabel(v); vl.setStyleSheet("color:#8b949e;")
            row.addWidget(kl); row.addStretch(); row.addWidget(vl)
            il.addLayout(row)
        self.config_status_label = QLabel("设置自动保存")
        self.config_status_label.setStyleSheet("color:#8b949e;font-size:11px;")
        self.config_status_label.setWordWrap(True)
        il.addWidget(self.config_status_label)
        lay.addWidget(ic)

        rb = QPushButton("◯  重置所有设置"); rb.setFixedHeight(35)
        rb.setToolTip("恢复默认设置，并清空用眼统计、陪伴成长和服装选择")
        rb.setStyleSheet("QPushButton{background:transparent;color:#f85149;"
                         "border:1px solid #f85149;border-radius:8px;}"
                         "QPushButton:hover{background:rgba(248,81,73,0.1);}")
        rb.clicked.connect(self._reset_settings); lay.addWidget(rb)
        lay.addStretch()
        return page

    # ══════════════════════════════════════════
    #  托盘
    # ══════════════════════════════════════════
    def init_tray(self):
        px = QPixmap(32,32); px.fill(Qt.transparent)
        p = QPainter(px); p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen); p.setBrush(QColor("#0ea5e9"))
        p.drawEllipse(2,2,28,28); p.setBrush(QColor("#010409"))
        p.drawEllipse(9,9,14,14); p.end()
        self.tray = QSystemTrayIcon(QIcon(px),self)
        menu = QMenu()
        menu.setStyleSheet("QMenu{background:#161b22;color:#c9d1d9;border:1px solid #30363d;padding:4px;}"
                           "QMenu::item:selected{background:#21262d;}")
        for label,slot in [("显示主界面",self._open_main),("切换护眼",self._hk_toggle),
                            (None,None),("退出",self._quit_app)]:
            if label is None: menu.addSeparator()
            else:
                a=QAction(label,self); a.triggered.connect(slot); menu.addAction(a)
        # 桌宠开关（与桌宠功能页同步）
        self.pet_action = QAction("显示桌宠", self, checkable=True)
        self.pet_action.setChecked(self.pet_enabled)
        self.pet_action.toggled.connect(self._on_pet_toggle)
        menu.insertAction(menu.actions()[2], self.pet_action)
        exit_action = menu.actions()[-1]
        self.snooze_tray_menu = self._create_snooze_menu()
        menu.insertMenu(exit_action, self.snooze_tray_menu)
        self.resume_reminders_action = QAction("恢复休息提醒", self)
        self.resume_reminders_action.triggered.connect(self._resume_reminders)
        self.resume_reminders_action.setEnabled(False)
        menu.insertAction(exit_action, self.resume_reminders_action)
        menu.insertSeparator(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(f"{APP_TITLE} — 运行中")
        self.tray.activated.connect(lambda r: self._open_main() if r==QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def _quit_app(self):
        self._cleanup()
        QApplication.quit()

    def _cleanup(self):
        if self._quitting:
            return
        self._sync_work_clock()
        self._quitting = True
        self._transition.stop()
        self._save_settings()
        for timer_name in ("guard_timer", "stat_timer", "countdown_timer",
                           "auto_timer", "metrics_timer", "_effect_timer",
                           "_save_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        preview = vars(self).get("pet_preview")
        if preview is not None:
            preview._preview_timer.stop()
        if self.overlay is not None:
            self.overlay.cancel()
        self._dim_mgr.hide()
        if self.pet is not None:
            self.pet.close()
        listener = getattr(self, "_hk_listener", None)
        if listener is not None:
            listener.stop()
        self._activity.close()
        self.tray.hide()
        DisplayManager.reset()

    # ══════════════════════════════════════════
    #  电源事件（休眠唤醒）
    # ══════════════════════════════════════════
    def nativeEvent(self, event_type, message):
        WM_SETTINGCHANGE      = 0x001A
        WM_DISPLAYCHANGE      = 0x007E
        WM_POWERBROADCAST     = 0x0218
        WM_WTSSESSION_CHANGE  = 0x02B1
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(ctypes.wintypes.MSG)).contents
            activation_message = getattr(self, "_activation_message", 0)
            if activation_message and msg.message == activation_message:
                self._open_main()
                return True, 0
            if msg.message in (WM_SETTINGCHANGE, WM_DISPLAYCHANGE):
                # #1 系统设置/分辨率变化 → 立即重应用
                QTimer.singleShot(300, self.apply_effect)
                # #3 屏幕布局变化 → 重建超暗遮罩
                QTimer.singleShot(400, self._on_screen_change)
            elif msg.message == WM_WTSSESSION_CHANGE and msg.wParam in (7, 8):
                self._set_session_pause(locked=msg.wParam == 7)
            elif msg.message == WM_POWERBROADCAST:
                if msg.wParam == 0x0004:
                    self._set_session_pause(suspended=True)
                elif msg.wParam in (0x0006, 0x0007, 0x0012):
                    self._set_session_pause(suspended=False)
                    self._metrics.reset_cpu_baseline()
                    QTimer.singleShot(2500, self.apply_effect)
        except Exception:
            pass
        return False, 0

    def _set_session_pause(self, locked=None, suspended=None):
        if locked is False or suspended is False:
            self._work_clock.sample(False, float("inf"))
        else:
            self._sync_work_clock()
        if locked is not None:
            self._session_locked = locked
        if suspended is not None:
            self._suspended = suspended
        self._sync_work_clock()
        self._refresh_countdown_label()

    # ══════════════════════════════════════════
    #  逻辑
    # ══════════════════════════════════════════
    def showEvent(self, event):
        super().showEvent(event)
        self._sync_metrics_timer()

    def hideEvent(self, event):
        timer = vars(self).get("metrics_timer")
        if timer is not None:
            timer.stop()
        super().hideEvent(event)

    def _sync_metrics_timer(self):
        timer = vars(self).get("metrics_timer")
        if timer is None:
            return
        if (self._quitting or not self.isVisible() or self.isMinimized()
                or self.pages.currentIndex() != 2):
            timer.stop()
        elif not timer.isActive():
            self._metrics.reset_cpu_baseline()
            self._refresh_system_metrics()
            timer.start()

    def _nav(self, idx):
        for i,btn in enumerate(self.nav_btns): btn.setChecked(i==idx)
        self.pages.setCurrentIndex(idx)
        if idx == 2: self._refresh_stats()
        elif idx == 3: self._sync_pet_page()

    def on_slider_change(self):
        self.temp = self.temp_slider.value()
        self.bright = self.bright_slider.value() / 100
        self.temp_val.setText(f"{self.temp} K")
        self.bright_val.setText(f"{int(self.bright*100)}%")
        self._transition.stop()
        if self.is_enabled:
            if not self._effect_timer.isActive():
                self._effect_timer.start()
        else:
            self._effect_timer.stop()
        self._sync_mode_selection()
        self._schedule_save()

    def apply_preset(self, name):
        p = MODES[name]
        self._effect_timer.stop()
        if self.is_enabled:
            self._transition.start(self.temp, self.bright, p['temp'], p['bright'], 1500)
        else:
            self._transition.stop()
        self.temp = p['temp']; self.bright = p['bright']
        for sl,val in [(self.temp_slider,self.temp),(self.bright_slider,int(self.bright*100))]:
            sl.blockSignals(True); sl.setValue(val); sl.blockSignals(False)
        self.temp_val.setText(f"{self.temp} K")
        self.bright_val.setText(f"{int(self.bright*100)}%")
        self._sync_mode_selection()
        self._save_settings()

    def toggle_master(self):
        self._sync_work_clock()
        self._effect_timer.stop()
        self.is_enabled = self.toggle.isChecked()
        if self.is_enabled:
            self.toggle_label.setText("已开启")
            self.toggle_label.setStyleSheet(f"color:{self._accent};margin-right:8px;")
            self.apply_effect()
            if not self.guard_timer.isActive():   # #7 重新启动守护
                self.guard_timer.start(800)
            if self.super_dim:
                self._dim_mgr.show(self.super_dim_alpha)
            if self.auto_mode:
                self._auto_mode_tick()
            if self.overlay is None or not self.overlay.isVisible():
                self._restart_rest_schedule(reset_session=True)
        else:
            self.toggle_label.setText("已关闭")
            self.toggle_label.setStyleSheet("color:#484f58;margin-right:8px;")
            self._transition.stop()
            self.guard_timer.stop()               # #7 停止守护节省 CPU
            self._restart_rest_schedule(reset_session=True)
            self._dim_mgr.hide()
            self.auto_status_lbl.setText("")
            DisplayManager.reset()
        self._refresh_countdown_label()
        self._pet_state()
        self._save_settings()

    def apply_effect(self):
        if self.is_enabled and not self._quitting:
            DisplayManager.apply(self.temp, self.bright)

    def _guard_apply(self):
        pending = vars(self).get("_effect_timer")
        if (self.is_enabled and not self._quitting
                and not self._transition.is_active()
                and not (pending is not None and pending.isActive())):
            DisplayManager.ensure(self.temp, self.bright)

    def apply_timer_settings(self):
        self.rest_interval_min = self.interval_spin.value()
        self.rest_duration_sec = self.duration_spin.value()
        self._restart_rest_schedule()
        self._refresh_countdown_label()
        self._save_settings()

    def _restart_rest_schedule(self, reset_session=False):
        self._sync_work_clock()
        self._rest_deferred = False
        self._warned_1min = False
        self._work_clock.restart(self.rest_interval_min * 60, reset_session)
        self._next_rest_secs = self._work_clock.remaining_seconds

    def _create_snooze_menu(self):
        menu = QMenu("暂缓休息提醒", self)
        menu.setStyleSheet(
            "QMenu{background:#161b22;color:#c9d1d9;border:1px solid #30363d;padding:4px;}"
            "QMenu::item:selected{background:#21262d;}"
            "QMenu::item:disabled{color:#484f58;}"
        )
        for minutes in (15, 30, 60):
            action = menu.addAction(f"{minutes} 分钟")
            action.triggered.connect(
                lambda checked=False, minutes=minutes: self._snooze_reminders(minutes)
            )
        return menu

    def _snooze_reminders(self, minutes):
        if self._quitting or not self.is_enabled or self.overlay is not None:
            return False
        self._sync_work_clock()
        self._work_clock.snooze(minutes * 60)
        self._refresh_countdown_label()
        return True

    def _resume_reminders(self):
        self._work_clock.cancel_snooze()
        self._refresh_countdown()

    def _sync_work_clock(self):
        clock = getattr(self, "_work_clock", None)
        if clock is None:
            return
        blocked = self._quitting or not self.is_enabled or self.overlay is not None
        self._pause_reason = ""
        if self._session_locked:
            blocked = True
            self._pause_reason = "锁屏"
        elif self._suspended:
            blocked = True
            self._pause_reason = "休眠"
        inactive_seconds = float("inf") if blocked else 0.0
        idle_seconds = None if blocked else self._activity.idle_seconds()
        if idle_seconds is not None and idle_seconds >= IDLE_PAUSE_SECONDS:
            inactive_seconds = idle_seconds - IDLE_PAUSE_SECONDS
            blocked = True
            self._pause_reason = "空闲"
        elapsed = clock.sample(not blocked, inactive_seconds)
        now = datetime.now()
        today = now.date().isoformat()
        if self._stat_date != today:
            midnight = datetime.combine(now.date(), datetime.min.time())
            end_offset = inactive_seconds if math.isfinite(inactive_seconds) else 0.0
            today_elapsed = max(0.0, (now - midnight).total_seconds() - end_offset)
            previous_elapsed = max(0.0, elapsed - today_elapsed)
            self._today_seconds += previous_elapsed
            self.today_minutes = min(1440, int(self._today_seconds / 60))
            self._rollover_stats_if_needed(today)
            elapsed -= previous_elapsed
        self._today_seconds = min(86400.0, self._today_seconds + elapsed)
        self.today_minutes = int(self._today_seconds / 60)
        self.week_data[today] = self.today_minutes
        self._next_rest_secs = clock.remaining_seconds

    def _refresh_countdown_label(self):
        snooze_seconds = self._work_clock.snooze_remaining_seconds
        can_snooze = self.is_enabled and self.overlay is None and not self._quitting
        for name in ("snooze_button", "snooze_tray_menu"):
            control = vars(self).get(name)
            if control is not None:
                control.setEnabled(can_snooze)
        for name in ("resume_reminders_button", "resume_reminders_action"):
            control = vars(self).get(name)
            if control is not None:
                control.setEnabled(snooze_seconds > 0 and not self._quitting)
        status = vars(self).get("snooze_status_label")
        if status is not None:
            if snooze_seconds:
                minutes, seconds = divmod(snooze_seconds, 60)
                status.setText(f"{minutes:02d}:{seconds:02d} 后恢复提醒 · 护眼与统计不受影响")
            else:
                status.setText("暂缓只影响提醒，护眼与用眼统计继续运行")
        if not self.is_enabled:
            self.next_rest_label.setText("已暂停")
            return
        if self.overlay is not None:
            self.next_rest_label.setText("休息中")
            return
        if snooze_seconds:
            self.next_rest_label.setText("免打扰")
            return
        minutes, seconds = divmod(self._next_rest_secs, 60)
        label = f"{minutes:02d}:{seconds:02d}"
        if self._pause_reason:
            label += f" · {self._pause_reason}暂停"
        self.next_rest_label.setText(label)

    def _refresh_today_summary(self):
        self.today_stat.setText(f"今日 {self.today_minutes} 分钟")
        self.today_stat.setStyleSheet("color:#6e7681;font-size:11px;")

    def _on_rest_trigger(self):
        if not self.is_enabled or self._quitting:
            return
        self._sync_work_clock()
        if self.overlay is not None or not self._work_clock.active:
            return
        if self._work_clock.snooze_remaining_seconds:
            return
        if self._work_clock.remaining_seconds > 0:
            return
        if self._is_fullscreen():
            self.fullscreen_warn.setText("⚠ 检测到全屏应用，休息提醒已推迟")
            self.tray.showMessage(APP_TITLE,"检测到全屏，休息已推迟5分钟",
                                  QSystemTrayIcon.Information,3000)
            self._rest_deferred = True
            self._work_clock.restart(5 * 60)
            self._next_rest_secs = self._work_clock.remaining_seconds
            self._warned_1min = False
            self._refresh_countdown_label()
            if self.pet is not None:
                self.pet.set_countdown(self._next_rest_secs, 5 * 60)
                self._pet_state()
            return
        self.fullscreen_warn.setText("")
        self.show_rest_overlay()

    def show_rest_overlay(self):
        if self._quitting:
            return
        if self.overlay is not None and self.overlay.isVisible():
            self.overlay.raise_()
            return
        self._sync_work_clock()
        self._work_clock.cancel_snooze()
        self.overlay = EyeExerciseOverlay(self.rest_duration_sec,
                                          force_mode=self.force_rest)
        self._restart_rest_schedule()
        current_overlay = self.overlay
        current_overlay.closed.connect(
            lambda: self._on_overlay_closed(current_overlay)
        )
        self.overlay.show()
        if self.sound_enabled:
            QApplication.beep()
        self.break_count += 1
        self._refresh_countdown_label()
        if self.pet is not None:
            self.pet.set_countdown(self._next_rest_secs, self.rest_interval_min * 60)
        self._pet_state()

    def _on_overlay_closed(self, overlay=None):
        overlay = self.overlay if overlay is None else overlay
        if overlay is None or overlay is not self.overlay:
            return
        self._sync_work_clock()
        self.overlay = None
        self._restart_rest_schedule(reset_session=True)
        if self.pet is not None:
            self.pet.set_countdown(self._next_rest_secs, self.rest_interval_min * 60)
        self._pet_state()
        self._refresh_countdown_label()
        if overlay.completed and not self._quitting:
            self._complete_pet_rest()
        overlay.deleteLater()

    def _refresh_countdown(self):
        previous_minutes = self.today_minutes
        self._sync_work_clock()
        if (self._work_clock.active and self._next_rest_secs == 0
                and not self._work_clock.snooze_remaining_seconds):
            self._on_rest_trigger()
        self._refresh_countdown_label()
        if self.today_minutes != previous_minutes:
            self._refresh_today_summary()
        if self.pet is not None:
            total = 5 * 60 if self._rest_deferred else self.rest_interval_min * 60
            self.pet.set_countdown(self._next_rest_secs, total)
        self._pet_state(refresh_page=False)
        preview = vars(self).get("pet_preview")
        if (preview is not None and preview.isVisible()
                and not preview.window().isMinimized()):
            self._refresh_pet_status()
        if (self._work_clock.active and 0 < self._next_rest_secs <= 60
                and not self._warned_1min and not self._rest_deferred
                and not self._work_clock.snooze_remaining_seconds):
            self._warned_1min = True
            self.tray.showMessage(APP_TITLE,"还有 1 分钟就该休息了 ☕",
                                  QSystemTrayIcon.Information,5000)
            if self.pet is not None:
                self.pet.say("还有 1 分钟就休息啦", 6000)

    def _update_stat(self):
        self._sync_work_clock()
        self._refresh_today_summary()
        self._refresh_stats()
        # 统计数据不必每秒写盘，但不要等到退出才落盘。
        self._stat_save_ticks = getattr(self, "_stat_save_ticks", 0) + 1
        if self._stat_save_ticks >= 5:
            self._stat_save_ticks = 0
            self._schedule_save()

    def _refresh_stats(self):
        self._sync_work_clock()
        sess = int(self._work_clock.session_seconds / 60)
        self.stat_today.setText(str(self.today_minutes))
        self.stat_session.setText(str(sess))
        self.stat_breaks.setText(str(self.break_count))
        self.day_ring.setValue(min(100,int(self.today_minutes/480*100)))
        today = date.today()
        days,vals = [],[]
        for i in range(6,-1,-1):
            d = today - timedelta(days=i)
            days.append(d.strftime("%m/%d")); vals.append(self.week_data.get(d.isoformat(),0))
        self.bar_chart.set_data(vals,days)

    def _usage_history(self):
        today = date.today()
        cutoff = today - timedelta(days=31)
        history = dict(self.week_data)
        history[today.isoformat()] = self.today_minutes
        return {
            day: _bounded_int(minutes, 0, 0, 24 * 60)
            for day, minutes in history.items()
            if isinstance(day, str) and isinstance(minutes, (int, float))
            and not isinstance(minutes, bool) and self._valid_stat_day(day, cutoff)
        }

    def _export_statistics(self):
        filename = f"CareEyesPro-usage-{date.today().isoformat()}.csv"
        path = QFileDialog.getSaveFileName(
            self, "导出用眼统计", filename, "CSV 文件 (*.csv)"
        )[0]
        if not path:
            return False
        self._sync_work_clock()
        history = self._usage_history()
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(("date", "usage_minutes"))
        writer.writerows(sorted(history.items()))
        try:
            _write_atomic(path, buffer.getvalue().encode("utf-8-sig"))
        except OSError as error:
            self.export_status_label.setText(f"导出失败：{error}")
            self.export_status_label.setStyleSheet("color:#f85149;font-size:11px;")
            self.export_status_label.setToolTip(path)
            return False
        self.export_status_label.setText(f"已导出 {len(history)} 天记录：{os.path.basename(path)}")
        self.export_status_label.setStyleSheet("color:#10b981;font-size:11px;")
        self.export_status_label.setToolTip(path)
        return True

    def _refresh_system_metrics(self):
        """刷新统计页的系统状态；任何单项失败都只影响该项。"""
        try:
            snapshot = self._metrics.sample_fast()
        except Exception:
            snapshot = SystemSnapshot()
        try:
            disk = self._metrics.sample_disk()
        except Exception:
            # 第三方驱动或受限环境不应影响主循环。
            disk = DiskSnapshot(SystemMetricsCollector.system_drive_root())

        cpu_pct = snapshot.cpu_percent if snapshot.cpu_state == "ready" else None
        memory_pct = _percent(snapshot.memory_used, snapshot.memory_total)
        disk_pct = _percent(disk.used, disk.total)

        def set_metric(value_widget, bar_widget, pct, unavailable_text="不可用"):
            if pct is None:
                value_widget.setText(unavailable_text)
                bar_widget.setValue(0)
                return
            value_widget.setText(f"{pct:.0f}%")
            bar_widget.setValue(int(round(pct)))

        cpu_state_text = "读取中" if snapshot.cpu_state == "warming" else "不可用"
        set_metric(self.system_cpu_value, self.system_cpu_bar, cpu_pct, cpu_state_text)
        set_metric(self.system_memory_value, self.system_memory_bar, memory_pct)
        set_metric(self.system_disk_value, self.system_disk_bar, disk_pct)
        self.system_uptime_value.setText(_format_uptime(snapshot.uptime_seconds))

        self.system_memory_value.setToolTip(
            f"已用 {_format_bytes(snapshot.memory_used)} / 总计 {_format_bytes(snapshot.memory_total)}"
        )
        self.system_disk_value.setToolTip(
            f"已用 {_format_bytes(disk.used)} / 总计 {_format_bytes(disk.total)}\n{disk.root}"
        )

    def _on_auto_toggle(self):
        self.auto_mode = self.auto_toggle.isChecked()
        if self.auto_mode: self._auto_mode_tick()
        else: self.auto_status_lbl.setText("")
        self._schedule_save()

    def _auto_mode_tick(self):
        if not self.auto_mode or not self.is_enabled or self._quitting: return
        h = datetime.now().hour; m = datetime.now().minute
        t0 = AUTO_CURVE[h]; t1 = AUTO_CURVE[(h+1)%24]
        target = int(t0 + (t1-t0)*m/60)
        self.auto_status_lbl.setText(f"自动 {target}K")
        if abs(target-self.temp) > 50:
            self._effect_timer.stop()
            self._transition.start(self.temp,self.bright,target,self.bright,3000)
            self.temp = target
            self.temp_slider.blockSignals(True); self.temp_slider.setValue(target)
            self.temp_slider.blockSignals(False); self.temp_val.setText(f"{target} K")

    def _on_dim_toggle(self):
        self.super_dim = self.dim_toggle.isChecked()
        self.dim_slider.setEnabled(self.super_dim)
        if self.super_dim and self.is_enabled:
            self._dim_mgr.show(self.super_dim_alpha)
        else:
            self._dim_mgr.hide()
        self._schedule_save()

    def _on_dim_alpha(self, val):
        self.super_dim_alpha = val
        if self.super_dim and self.is_enabled:
            self._dim_mgr.set_alpha(val)
        self._schedule_save()

    def _is_fullscreen(self) -> bool:
        """全屏检测：带进程名黑白名单，过滤伪全屏进程。(#4)"""
        try:
            import ctypes.wintypes as wt
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return False

            # 获取进程名
            pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            hproc = ctypes.windll.kernel32.OpenProcess(0x0410, False, pid.value)
            proc_name = ""
            if hproc:
                try:
                    buf = ctypes.create_unicode_buffer(260)
                    ctypes.windll.psapi.GetModuleBaseNameW(hproc, None, buf, 260)
                    proc_name = buf.value.lower()
                finally:
                    ctypes.windll.kernel32.CloseHandle(hproc)

            # 白名单：强制推迟
            if proc_name in FULLSCREEN_FORCE_DEFER:
                return True
            # 黑名单：忽略（不推迟）
            if proc_name in FULLSCREEN_WHITELIST:
                return False

            # 尺寸判断：对每个显示器分别判断，避免副屏使用主屏尺寸误判。
            rect = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return any(
                rect.left <= geo.left() and rect.top <= geo.top()
                and rect.right >= geo.right() and rect.bottom >= geo.bottom()
                for screen in QApplication.screens()
                for geo in (screen.geometry(),)
            )
        except Exception:
            return False

    def _on_screen_change(self, *args):
        """显示器热插拔：重应用 Gamma + 重建超暗遮罩。(#3)"""
        if self._quitting:
            return
        if self.is_enabled:
            self.apply_effect()
        else:
            DisplayManager.reset()
        if self.super_dim and self.is_enabled:
            self._dim_mgr.rebuild()
        if self.pet is not None and self.pet.isVisible():
            self.pet.keep_on_screen()

    def _on_autostart_change(self, state):
        self.autostart = bool(state); self._set_autostart(self.autostart); self._save_settings()

    def _on_sound_toggle(self, state):
        self.sound_enabled = bool(state)
        self._schedule_save()

    def _on_force_rest_toggle(self, state):
        self.force_rest = bool(state)
        self._schedule_save()

    def _set_autostart(self, enabled):
        path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        if winreg is None:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_SET_VALUE) as key:
                if enabled:
                    app_path = os.path.realpath(sys.argv[0])
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                                      f'"{app_path}"')
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
        except Exception: pass

    def _read_autostart(self):
        path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except Exception: return False

    def _reset_settings(self):
        self._sync_work_clock()
        self._effect_timer.stop()
        # 复位控件时暂时屏蔽信号，避免连续触发多次 Gamma/注册表写入。
        widgets = [self.temp_slider, self.bright_slider, self.interval_spin,
                   self.duration_spin, self.auto_toggle, self.dim_toggle,
                   self.dim_slider, self.force_rest_cb, self.sound_cb,
                   self.pet_enable_toggle, self.autostart_cb]
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.temp = 5000
            self.bright = 1.0
            self.rest_interval_min = 45
            self.rest_duration_sec = 20
            self.auto_mode = False
            self.super_dim = False
            self.super_dim_alpha = 80
            self.force_rest = False
            self.sound_enabled = True
            self.pet_enabled = True
            self.pet_kind = DesktopPet.DEFAULT_PET_KIND
            self.pet_interaction_mode = DesktopPet.DEFAULT_INTERACTION_MODE
            self.autostart = False
            self.temp_slider.setValue(5000)
            self.bright_slider.setValue(100)
            self.interval_spin.setValue(45)
            self.duration_spin.setValue(20)
            self.auto_toggle.setChecked(False)
            self.dim_toggle.setChecked(False)
            self.dim_slider.setValue(80)
            self.dim_slider.setEnabled(False)
            self.force_rest_cb.setChecked(False)
            self.sound_cb.setChecked(True)
            self.pet_enable_toggle.setChecked(True)
            self.autostart_cb.setChecked(False)
        finally:
            for widget in widgets:
                widget.blockSignals(False)

        self._set_autostart(False)
        if self.overlay is not None:
            self.overlay.cancel()
        self._pet_progress = PetProgress()
        self._sync_pet_progress()
        self._dim_mgr.hide()
        self._next_rest_secs = self.rest_interval_min * 60
        self._warned_1min = False
        self._stat_date = date.today().isoformat()
        self.today_minutes = 0
        self._today_seconds = 0.0
        self.break_count = 0
        self._stat_save_ticks = 0
        self.week_data = {}
        self._work_clock.cancel_snooze()
        self._work_clock.restart(self.rest_interval_min * 60, reset_session=True)
        self.pet_pos = None                 # 桌宠回到右下角默认位置
        if self.pet is not None:
            self.pet.set_pet_kind(self.pet_kind)
            self.pet.set_interaction_mode(self.pet_interaction_mode)
        self._hide_pet()
        self._show_pet()
        if self.pet is not None:
            self.pet.set_countdown(self._next_rest_secs, self.rest_interval_min * 60)
        if hasattr(self, "pet_action"):
            self.pet_action.blockSignals(True)
            self.pet_action.setChecked(True)
            self.pet_action.blockSignals(False)

        self.is_enabled = True
        self.toggle.blockSignals(True)
        self.toggle.setChecked(True)
        self.toggle.blockSignals(False)
        self.toggle_label.setText("已开启")
        self.toggle_label.setStyleSheet(f"color:{self._accent};margin-right:8px;")
        self._transition.stop()
        self.apply_effect()
        if not self.guard_timer.isActive():
            self.guard_timer.start(800)
        self._restart_rest_schedule()
        self.auto_status_lbl.setText("")
        self.fullscreen_warn.setText("")
        self._sync_mode_selection()
        self._refresh_today_summary()
        self._refresh_countdown_label()
        self._refresh_stats()
        self._pet_state()
        self._save_settings()

    # ══════════════════════════════════════════
    #  持久化
    # ══════════════════════════════════════════
    _DEFAULTS = {
        "temp":5000,"bright":1.0,"is_enabled":True,"rest_interval":45,"rest_duration":20,
        "force_rest":False,
        "auto_mode":False,"autostart":False,"super_dim":False,"super_dim_alpha":80,
        "sound_enabled":True,"stat_date":"","today_minutes":0,"break_count":0,
        "today_seconds":-1.0,
        "week_data":{},
        "pet_enabled":True,"pet_kind":"blue_cat",
        "pet_interaction_mode":"move","pet_pos":[],
        "pet_completed_rests":0,"pet_outfit":list(PetProgress.DEFAULT_OUTFIT),
    }

    def load_settings(self):
        cfg = dict(self._DEFAULTS)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE,"r",encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("configuration root must be an object")
                for k,dv in self._DEFAULTS.items():
                    v = raw.get(k,dv)
                    if not isinstance(v,type(dv)): v=dv
                    cfg[k] = v
            except Exception:
                pass  # 文件损坏 → 静默使用默认值
        self.temp=_bounded_int(cfg["temp"], 5000, 2000, 8000)
        self.bright=_bounded_float(cfg["bright"], 1.0, 0.30, 1.0)
        self.is_enabled=cfg["is_enabled"]
        self.rest_interval_min=_bounded_int(cfg["rest_interval"], 45, 5, 120)
        self.rest_duration_sec=_bounded_int(cfg["rest_duration"], 20, 10, 300)
        self.force_rest=cfg["force_rest"]
        self.auto_mode=cfg["auto_mode"]; self.super_dim=cfg["super_dim"]
        self.super_dim_alpha=_bounded_int(cfg["super_dim_alpha"], 80, 20, 200)
        self.sound_enabled=cfg["sound_enabled"]
        self.pet_enabled=cfg["pet_enabled"]
        self.pet_kind = (cfg["pet_kind"] if cfg["pet_kind"] in DesktopPet.PET_STYLES
                         else DesktopPet.DEFAULT_PET_KIND)
        self.pet_interaction_mode = DesktopPet.normalize_interaction_mode(
            cfg["pet_interaction_mode"]
        )
        self.pet_pos=_parse_position(cfg["pet_pos"])
        self._pet_progress = PetProgress(
            _bounded_int(cfg["pet_completed_rests"], 0, 0, 1_000_000),
            cfg["pet_outfit"],
        )
        cutoff = date.today() - timedelta(days=31)
        week_data = cfg["week_data"] if isinstance(cfg["week_data"], dict) else {}
        self.week_data={str(k): _bounded_int(v, 0, 0, 24 * 60)
                        for k,v in week_data.items()
                        if isinstance(k, str) and isinstance(v, (int, float))
                        and not isinstance(v, bool)
                        and self._valid_stat_day(k, cutoff)}
        self._next_rest_secs=self.rest_interval_min*60
        today=date.today().isoformat()
        stored_day = cfg["stat_date"] if isinstance(cfg["stat_date"], str) else ""
        try:
            stored_date = date.fromisoformat(stored_day)
        except (TypeError, ValueError):
            stored_date = None
        stored_minutes = _bounded_int(cfg["today_minutes"], 0, 0, 24 * 60)
        stored_seconds = _bounded_float(cfg["today_seconds"], -1.0, -1.0, 86400.0)
        if stored_seconds < 0:
            stored_seconds = float(stored_minutes * 60)
        else:
            stored_minutes = int(stored_seconds / 60)
        stored_breaks = _bounded_int(cfg["break_count"], 0, 0, 24 * 60)
        # 如果程序在午夜前崩溃，旧配置里的当天数据仍应归档到旧日期。
        if (stored_date is not None and stored_date.isoformat() != today
                and self._valid_stat_day(stored_day, cutoff)):
            self.week_data[stored_day] = stored_minutes
        self._stat_date=today
        self.today_minutes=stored_minutes if stored_day == today else 0
        self._today_seconds=stored_seconds if stored_day == today else 0.0
        self.break_count=stored_breaks if stored_day == today else 0
        self._stat_save_ticks = 0
        self.autostart=self._read_autostart()

    def _save_settings(self):
        """通过 QSaveFile 提交完整配置，失败时保留旧文件并报告状态。"""
        save_timer = vars(self).get("_save_timer")
        if save_timer is not None:
            save_timer.stop()
        self._sync_work_clock()
        today=date.today().isoformat()
        self._rollover_stats_if_needed(today)
        self.week_data = self._usage_history()
        data={
            "temp":self.temp,"bright":self.bright,"is_enabled":self.is_enabled,
            "rest_interval":self.rest_interval_min,"rest_duration":self.rest_duration_sec,
            "force_rest":self.force_rest,
            "auto_mode":self.auto_mode,"super_dim":self.super_dim,
            "super_dim_alpha":self.super_dim_alpha,"sound_enabled":self.sound_enabled,
            "stat_date":today,"today_minutes":self.today_minutes,
            "today_seconds":self._today_seconds,
            "break_count":self.break_count,"week_data":self.week_data,
            "pet_enabled":self.pet_enabled,
            "pet_kind":vars(self).get("pet_kind", DesktopPet.DEFAULT_PET_KIND),
            "pet_interaction_mode":vars(self).get(
                "pet_interaction_mode", DesktopPet.DEFAULT_INTERACTION_MODE
            ),
            "pet_pos":self.pet_pos or [],
            "pet_completed_rests":self._pet_progress.completed_rests,
            "pet_outfit":list(self._pet_progress.outfit),
        }
        try:
            _write_atomic(CONFIG_FILE, json.dumps(
                data, ensure_ascii=False, indent=2, allow_nan=False
            ).encode("utf-8"))
        except (OSError, TypeError, ValueError) as error:
            self._settings_error = str(error)
            self._refresh_settings_status()
            return False
        self._settings_error = ""
        self._refresh_settings_status()
        return True

    def _refresh_settings_status(self):
        label = vars(self).get("config_status_label")
        if label is not None:
            message = f"保存失败：{self._settings_error}" if self._settings_error else "设置已保存"
            color = "#f85149" if self._settings_error else "#8b949e"
            label.setText(message)
            label.setStyleSheet(f"color:{color};font-size:11px;")

    @staticmethod
    def _valid_stat_day(value, cutoff):
        try:
            parsed = date.fromisoformat(value)
            return cutoff <= parsed <= date.today()
        except (TypeError, ValueError):
            return False

    def _rollover_stats_if_needed(self, today=None):
        """在首次访问新的一天时归档旧数据，避免午夜附近丢失统计。"""
        today = today or date.today().isoformat()
        current = getattr(self, "_stat_date", today)
        if current == today:
            return False

        try:
            cutoff = date.fromisoformat(today) - timedelta(days=31)
        except (TypeError, ValueError):
            cutoff = date.today() - timedelta(days=31)
        week_data = getattr(self, "week_data", {})
        if (isinstance(week_data, dict) and isinstance(current, str)
                and self._valid_stat_day(current, cutoff)):
            week_data[current] = _bounded_int(
                getattr(self, "today_minutes", 0), 0, 0, 24 * 60
            )
        self.week_data = week_data if isinstance(week_data, dict) else {}
        self.today_minutes = 0
        self._today_seconds = 0.0
        self.break_count = 0
        self._stat_date = today
        self._stat_save_ticks = 0
        return True

    def _schedule_save(self):
        if not hasattr(self, "_save_timer"):
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.timeout.connect(self._save_settings)
        self._save_timer.start(500)

    def closeEvent(self, event):
        if not self._quitting:
            self._save_settings()
            self.hide()
            event.ignore()
            return
        timer = getattr(self, "metrics_timer", None)
        if timer is not None:
            timer.stop()
        self._save_settings()
        self._dim_mgr.hide()
        if self.pet is not None:
            self.pet.close()
        DisplayManager.reset()
        super().closeEvent(event)


# ─────────────────────────────────────────────
# 🚀  入口
# ─────────────────────────────────────────────
def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,    True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    instance = None
    window = None
    primary = False
    try:
        try:
            instance = SingleInstance(APP_NAME, CONFIG_FILE)
            primary = instance.acquire()
        except OSError as error:
            QMessageBox.critical(None, APP_TITLE, f"无法建立单实例保护，程序未启动。\n{error}")
            return 1
        if not primary:
            if not instance.activate_existing():
                QMessageBox.information(None, APP_TITLE, "程序已在运行，请从系统托盘打开。")
            return 0
        window = CareEyesApp(instance.activation_message)
        instance.allow_activation(int(window.winId()))
        app.aboutToQuit.connect(window._cleanup)
        window.show()
        return app.exec_()
    finally:
        try:
            if window is not None:
                window._cleanup()
        finally:
            try:
                if primary:
                    DisplayManager.reset()
            finally:
                if instance is not None:
                    instance.close()


if __name__ == "__main__":
    sys.exit(main())
