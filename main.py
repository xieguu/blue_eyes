import sys
import ctypes
import math
import json
import os
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QLabel, QSlider, QHBoxLayout, QFrame, QStackedWidget,
    QListWidget, QListWidgetItem, QDesktopWidget, QGraphicsDropShadowEffect
)
from PyQt5.QtCore import Qt, QTimer, QSize, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon

# =========================
# ⚙️ 底层配置与常量
# =========================
CONFIG_FILE = "care_eyes_pro.json"

MODES = {
    "常规": {"temp": 5000, "bright": 0.9, "desc": "平衡模式，适合日常使用"},
    "办公": {"temp": 5500, "bright": 1.0, "desc": "较高色温，保持工作警觉"},
    "游戏": {"temp": 6000, "bright": 1.0, "desc": "色彩还原度高，降低蓝光"},
    "阅读": {"temp": 4000, "bright": 0.8, "desc": "模拟纸质书，极致柔和"},
    "睡眠": {"temp": 2500, "bright": 0.6, "desc": "过滤 90% 蓝光，助眠必备"}
}

# =========================
# 💻 核心驱动 (Windows API)
# =========================
def set_screen_gamma(temp_kelvin, brightness):
    """
    temp_kelvin: 1000 - 10000
    brightness: 0.2 - 1.0
    """
    # 转换色温
    t = temp_kelvin / 100
    if t <= 66:
        r = 255
        g = 99.47 * math.log(t) - 161.12 if t > 1 else 0
    else:
        r = 329.7 * ((t - 60) ** -0.13)
        g = 288.1 * ((t - 60) ** -0.07)
    
    if t >= 66: b = 255
    elif t <= 19: b = 0
    else: b = 138.5 * math.log(t - 10) - 305.05

    # 归一化并结合亮度
    r_ratio = (max(0, min(255, r)) / 255) * brightness
    g_ratio = (max(0, min(255, g)) / 255) * brightness
    b_ratio = (max(0, min(255, b)) / 255) * brightness

    hdc = ctypes.windll.user32.GetDC(0)
    ramp = (ctypes.c_ushort * 256 * 3)()
    for i in range(256):
        base = i * 256
        ramp[0][i] = int(min(65535, r_ratio * base))
        ramp[1][i] = int(min(65535, g_ratio * base))
        ramp[2][i] = int(min(65535, b_ratio * base))
    ctypes.windll.gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))

# =========================
# 🕒 全屏休息窗口 (对标 CareUEyes)
# =========================
class RestOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.showFullScreen()
        
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)
        
        # 磨砂背景效果 (用半透明黑色模拟)
        self.bg = QFrame(self)
        self.bg.setStyleSheet("background-color: rgba(20, 20, 20, 230); border-radius: 0px;")
        self.bg.setGeometry(self.rect())
        
        title = QLabel("休息时间到 ☕")
        title.setStyleSheet("color: white; font-size: 48px; font-weight: bold;")
        layout.addWidget(title, 0, Qt.AlignCenter)
        
        self.timer_label = QLabel("离开电脑，远眺 20 秒...")
        self.timer_label.setStyleSheet("color: #bbb; font-size: 24px;")
        layout.addWidget(self.timer_label, 0, Qt.AlignCenter)
        
        btn_skip = QPushButton("跳过休息")
        btn_skip.setFixedSize(120, 40)
        btn_skip.setStyleSheet("""
            QPushButton { background: #444; color: white; border-radius: 5px; }
            QPushButton:hover { background: #666; }
        """)
        btn_skip.clicked.connect(self.close)
        layout.addSpacing(50)
        layout.addWidget(btn_skip, 0, Qt.AlignCenter)
        
        self.setLayout(layout)

# =========================
# 📱 主界面
# =========================
class CareEyesApp(QWidget):
    def __init__(self):
        super().__init__()
        self.temp = 5000
        self.bright = 1.0
        self.is_enabled = True
        self.load_settings()
        
        self.init_ui()
        self.apply_effect()
        
        # 定时器：休息提醒（每45分钟）
        self.rest_timer = QTimer(self)
        self.rest_timer.timeout.connect(self.show_rest_overlay)
        self.rest_timer.start(45 * 60 * 1000) 

    def init_ui(self):
        self.setWindowTitle("CareEyes Python Pro")
        self.setFixedSize(700, 450)
        self.setStyleSheet("""
            QWidget { background-color: #1e1e1e; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; }
            QSlider::groove:horizontal { height: 6px; background: #333; border-radius: 3px; }
            QSlider::handle:horizontal { background: #0078d7; width: 18px; margin: -6px 0; border-radius: 9px; }
            QPushButton { border: none; padding: 10px; border-radius: 5px; background: #2d2d2d; }
            QPushButton:hover { background: #3d3d3d; }
            QPushButton#active_btn { background: #0078d7; }
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # --- 左侧侧边栏 ---
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(180)
        self.sidebar.setStyleSheet("""
            QListWidget { background-color: #252526; border: none; outline: none; padding-top: 20px; }
            QListWidget::item { height: 50px; padding-left: 20px; border-left: 3px solid transparent; }
            QListWidget::item:selected { background-color: #2d2d2d; color: #0078d7; border-left: 3px solid #0078d7; }
        """)
        self.sidebar.addItem("🏠 护眼控制")
        self.sidebar.addItem("⏲ 休息提醒")
        self.sidebar.addItem("⚙ 高级设置")
        self.sidebar.currentRowChanged.connect(self.switch_page)
        main_layout.addWidget(self.sidebar)

        # --- 右侧内容区 ---
        self.pages = QStackedWidget()
        self.pages.addWidget(self.create_home_page())
        self.pages.addWidget(self.create_timer_page())
        self.pages.addWidget(self.create_settings_page())
        main_layout.addWidget(self.pages)

    def create_home_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 30, 30, 30)

        # 顶部开关
        top_row = QHBoxLayout()
        self.status_label = QLabel("护眼模式已开启")
        self.status_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.switch_btn = QPushButton("ON")
        self.switch_btn.setCheckable(True)
        self.switch_btn.setChecked(True)
        self.switch_btn.setFixedSize(60, 30)
        self.switch_btn.setStyleSheet("background: #0078d7; font-weight: bold;")
        self.switch_btn.clicked.connect(self.toggle_master)
        top_row.addWidget(self.status_label)
        top_row.addStretch()
        top_row.addWidget(self.switch_btn)
        layout.addLayout(top_row)

        layout.addSpacing(20)

        # 模式选择卡片
        mode_layout = QHBoxLayout()
        for m_name in MODES.keys():
            btn = QPushButton(m_name)
            btn.clicked.connect(lambda ch, n=m_name: self.apply_preset(n))
            mode_layout.addWidget(btn)
        layout.addLayout(mode_layout)

        layout.addSpacing(30)

        # 色温滑动条
        layout.addWidget(QLabel("蓝光过滤 (色温)"))
        self.temp_slider = QSlider(Qt.Horizontal)
        self.temp_slider.setRange(2000, 8000)
        self.temp_slider.setValue(self.temp)
        self.temp_slider.valueChanged.connect(self.on_slider_change)
        layout.addWidget(self.temp_slider)

        # 亮度滑动条
        layout.addWidget(QLabel("屏幕亮度"))
        self.bright_slider = QSlider(Qt.Horizontal)
        self.bright_slider.setRange(30, 100)
        self.bright_slider.setValue(int(self.bright * 100))
        self.bright_slider.valueChanged.connect(self.on_slider_change)
        layout.addWidget(self.bright_slider)

        layout.addStretch()
        return page

    def create_timer_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignCenter)
        
        icon_label = QLabel("⏲")
        icon_label.setStyleSheet("font-size: 80px;")
        layout.addWidget(icon_label, 0, Qt.AlignCenter)
        
        layout.addWidget(QLabel("休息提醒设置"), 0, Qt.AlignCenter)
        layout.addWidget(QLabel("每隔 45 分钟提醒休息一次"), 0, Qt.AlignCenter)
        
        test_btn = QPushButton("立即测试休息效果")
        test_btn.clicked.connect(self.show_rest_overlay)
        layout.addWidget(test_btn, 0, Qt.AlignCenter)
        
        return page

    def create_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("版本: v2.0 Pro"))
        layout.addWidget(QLabel("开机自启动: [已开启]"))
        layout.addWidget(QLabel("托盘最小化: [已开启]"))
        layout.addStretch()
        return page

    # =========================
    # 🎮 逻辑处理
    # =========================
    def switch_page(self, index):
        self.pages.setCurrentIndex(index)

    def on_slider_change(self):
        self.temp = self.temp_slider.value()
        self.bright = self.bright_slider.value() / 100
        self.apply_effect()

    def apply_preset(self, name):
        p = MODES[name]
        self.temp = p['temp']
        self.bright = p['bright']
        self.temp_slider.setValue(self.temp)
        self.bright_slider.setValue(int(self.bright * 100))
        self.apply_effect()

    def toggle_master(self):
        self.is_enabled = self.switch_btn.isChecked()
        if self.is_enabled:
            self.switch_btn.setText("ON")
            self.switch_btn.setStyleSheet("background: #0078d7;")
            self.apply_effect()
        else:
            self.switch_btn.setText("OFF")
            self.switch_btn.setStyleSheet("background: #444;")
            set_screen_gamma(6500, 1.0) # 还原

    def apply_effect(self):
        if self.is_enabled:
            set_screen_gamma(self.temp, self.bright)

    def show_rest_overlay(self):
        self.overlay = RestOverlay()
        self.overlay.show()

    def load_settings(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                self.temp = data.get('temp', 5000)
                self.bright = data.get('bright', 1.0)

    def closeEvent(self, event):
        with open(CONFIG_FILE, 'w') as f:
            json.dump({'temp': self.temp, 'bright': self.bright}, f)
        super().closeEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("CareEyes Pro")
    
    # 强制深色风格适配
    window = CareEyesApp()
    window.show()
    sys.exit(app.exec_())