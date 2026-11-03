"""
科技感扫描特效图片显示组件
带有上下扫描线的动态效果
"""

import numpy as np
from PyQt5.QtWidgets import QLabel, QApplication
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QPixmap, QImage, QPainter, QColor, QPen, QLinearGradient, QBrush
import cv2


class ScannerLabel(QLabel):
    """
    带扫描特效的图片显示组件

    特性:
    - 进度式扫描动画：从上到下扫描，已扫描区域显示处理效果
    - 多道扫描线：主扫描线 + 残影线 + 进度覆盖层
    - 平滑淡出过渡
    - 支持开始/停止扫描动画
    """

    scan_completed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("""
            QLabel {
                background-color: #1a1a2e;
                border: 2px solid #16213e;
                border-radius: 8px;
            }
        """)

        self.scan_position = 0.0
        self.scan_direction = 1
        self.scan_speed = 0.012

        self.is_scanning = False
        self._is_fading_out = False
        self._fade_alpha = 255
        self._fade_timer = QTimer(self)
        self._fade_timer.timeout.connect(self._update_fade)

        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self._update_scan)

        self.current_pixmap = None
        self.original_pixmap = None
        self._cached_pixmap = None

        self.scan_line_color = QColor(0, 255, 255)
        self.scan_line_width = 2
        self.scan_glow_width = 12

        self._buffer_pixmap = None

        self._trail_positions = []
        self._trail_max = 4

        self._scan_pass_count = 0

    def setPixmap(self, pixmap: QPixmap):
        self.original_pixmap = pixmap
        self._cached_pixmap = pixmap.copy() if pixmap else None
        self._buffer_pixmap = None
        self.current_pixmap = pixmap.copy() if pixmap else None
        super().setPixmap(self.current_pixmap)

    def start_scan(self, duration_ms: int = 2000):
        if self.original_pixmap is None:
            return

        self.is_scanning = True
        self._is_fading_out = False
        self._fade_alpha = 255
        self.scan_position = 0.0
        self.scan_direction = 1
        self._trail_positions = []
        self._scan_pass_count = 0

        self.scan_timer.start(16)

    def stop_scan(self):
        self.is_scanning = False
        self._is_fading_out = False
        self.scan_timer.stop()
        self._fade_timer.stop()
        if self.original_pixmap:
            super().setPixmap(self.original_pixmap)

    def stop_scan_smooth(self):
        if not self.is_scanning:
            self.stop_scan()
            return

        self.is_scanning = False
        self.scan_timer.stop()

        self._is_fading_out = True
        self._fade_alpha = 255
        self._fade_timer.start(20)

    def _update_fade(self):
        self._fade_alpha = max(0, self._fade_alpha - 18)
        if self._fade_alpha <= 0:
            self._fade_timer.stop()
            self._is_fading_out = False
            if self.original_pixmap:
                super().setPixmap(self.original_pixmap)
            return
        self._draw_scan_line()

    def _update_scan(self):
        if not self.is_scanning or self.original_pixmap is None:
            return

        self.scan_position += self.scan_speed * self.scan_direction

        if self.scan_position >= 1.0:
            self.scan_position = 1.0
            self.scan_direction = -1
            self._scan_pass_count += 1
        elif self.scan_position <= 0.0:
            self.scan_position = 0.0
            self.scan_direction = 1
            self._scan_pass_count += 1
            self.scan_completed.emit()

        self.scan_position = max(0.0, min(1.0, self.scan_position))

        self._trail_positions.append(self.scan_position)
        if len(self._trail_positions) > self._trail_max:
            self._trail_positions = self._trail_positions[-self._trail_max:]

        self._draw_scan_line()

    def _draw_scan_line(self):
        if self._cached_pixmap is None:
            return

        if self._buffer_pixmap is None or self._buffer_pixmap.size() != self._cached_pixmap.size():
            self._buffer_pixmap = self._cached_pixmap.copy()

        pixmap = self._buffer_pixmap
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, False)

        painter.drawPixmap(0, 0, self._cached_pixmap)

        height = pixmap.height()
        width = pixmap.width()

        fade_factor = self._fade_alpha / 255.0 if self._is_fading_out else 1.0

        progress_y = int(height * self.scan_position)
        if height > 0 and progress_y > 0:
            progress_overlay = QColor(0, 255, 255, int(10 * fade_factor))
            painter.fillRect(0, 0, width, progress_y, progress_overlay)

        scan_y = max(self.scan_glow_width, min(height - self.scan_glow_width, progress_y))

        for i, trail_pos in enumerate(self._trail_positions[:-1]):
            trail_y = int(height * trail_pos)
            trail_y = max(self.scan_glow_width, min(height - self.scan_glow_width, trail_y))
            trail_alpha = int((i + 1) * 15 * fade_factor)
            if trail_alpha > 0:
                trail_pen = QPen(QColor(0, 255, 255, trail_alpha), 1)
                painter.setPen(trail_pen)
                painter.drawLine(0, trail_y, width, trail_y)

        if fade_factor > 0:
            glow_alpha = int(255 * fade_factor)
            gradient = QLinearGradient(0, scan_y - self.scan_glow_width, 0, scan_y + self.scan_glow_width)
            gradient.setColorAt(0, QColor(0, 255, 255, 0))
            gradient.setColorAt(0.25, QColor(0, 255, 255, int(40 * fade_factor)))
            gradient.setColorAt(0.5, QColor(0, 255, 255, glow_alpha))
            gradient.setColorAt(0.75, QColor(0, 255, 255, int(40 * fade_factor)))
            gradient.setColorAt(1, QColor(0, 255, 255, 0))

            pen = QPen(QBrush(gradient), self.scan_glow_width)
            painter.setPen(pen)
            painter.drawLine(0, scan_y, width, scan_y)

            main_color = QColor(0, 255, 255, glow_alpha)
            pen = QPen(main_color)
            pen.setWidth(self.scan_line_width)
            painter.setPen(pen)
            painter.drawLine(0, scan_y, width, scan_y)

            point_size = 6
            painter.setBrush(QBrush(main_color))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(5, scan_y - point_size // 2, point_size, point_size)
            painter.drawEllipse(width - 5 - point_size, scan_y - point_size // 2, point_size, point_size)

        if self._is_fading_out and fade_factor < 0.5:
            scan_pct = 1.0
            if height > 0:
                scan_pct = max(0.0, min(1.0, (scan_y + self.scan_glow_width) / height))
            border_color = QColor(0, 255, 255, int(80 * fade_factor))
            border_pen = QPen(border_color, 2)
            painter.setPen(border_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(2, 2, width - 4, int((height - 4) * scan_pct))

        painter.end()

        super().setPixmap(pixmap)

    def set_scan_color(self, color: QColor):
        self.scan_line_color = color

    def set_scan_speed(self, speed: float):
        self.scan_speed = max(0.01, min(0.1, speed))


class ScanningOverlay(QLabel):
    """
    扫描覆盖层 - 可叠加在任意控件上
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.scan_position = 0.0
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self._update_scan)
        self.is_scanning = False

        self.scan_color = QColor(0, 255, 255, 180)
        self.scan_glow = QColor(0, 200, 255, 80)

    def start_scan(self):
        self.is_scanning = True
        self.scan_position = 0.0
        self.scan_timer.start(30)
        self.show()

    def stop_scan(self):
        self.is_scanning = False
        self.scan_timer.stop()
        self.hide()

    def _update_scan(self):
        self.scan_position += 0.015
        if self.scan_position > 1.0:
            self.scan_position = 0.0
        self.update()

    def paintEvent(self, event):
        if not self.is_scanning:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        height = self.height()
        width = self.width()
        scan_y = int(height * self.scan_position)

        gradient = QLinearGradient(0, scan_y - 30, 0, scan_y + 30)
        gradient.setColorAt(0, QColor(0, 255, 255, 0))
        gradient.setColorAt(0.5, self.scan_color)
        gradient.setColorAt(1, QColor(0, 255, 255, 0))

        pen = QPen(QBrush(gradient), 3)
        painter.setPen(pen)
        painter.drawLine(0, scan_y, width, scan_y)

        glow_gradient = QLinearGradient(0, scan_y - 60, 0, scan_y + 60)
        glow_gradient.setColorAt(0, QColor(0, 200, 255, 0))
        glow_gradient.setColorAt(0.5, self.scan_glow)
        glow_gradient.setColorAt(1, QColor(0, 200, 255, 0))

        pen = QPen(QBrush(glow_gradient), 20)
        painter.setPen(pen)
        painter.drawLine(0, scan_y, width, scan_y)

        painter.setPen(QPen(self.scan_color, 2))
        painter.drawRect(2, 2, width - 4, height - 4)


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)

    from PyQt5.QtWidgets import QVBoxLayout, QWidget, QPushButton

    window = QWidget()
    window.setWindowTitle("扫描特效测试")
    window.resize(800, 600)

    layout = QVBoxLayout()

    scanner = ScannerLabel()
    scanner.setMinimumSize(600, 400)

    test_img = np.zeros((400, 600, 3), dtype=np.uint8)
    test_img[:] = (30, 30, 50)
    cv2.putText(test_img, "X-Ray Image", (150, 200), cv2.FONT_HERSHEY_SIMPLEX, 2, (200, 200, 200), 3)

    h, w, ch = test_img.shape
    bytes_per_line = ch * w
    qt_img = QImage(test_img.data, w, h, bytes_per_line, QImage.Format_RGB888)
    scanner.setPixmap(QPixmap.fromImage(qt_img))

    layout.addWidget(scanner)

    btn_start = QPushButton("开始扫描")
    btn_start.clicked.connect(lambda: scanner.start_scan(3000))
    layout.addWidget(btn_start)

    btn_stop = QPushButton("平滑停止")
    btn_stop.clicked.connect(scanner.stop_scan_smooth)
    layout.addWidget(btn_stop)

    btn_stop_hard = QPushButton("立即停止")
    btn_stop_hard.clicked.connect(scanner.stop_scan)
    layout.addWidget(btn_stop_hard)

    window.setLayout(layout)
    window.show()

    sys.exit(app.exec_())