"""
足弓关键点标注工具

基于PyQt5的医学影像关键点标注工具，用于标注足部X线片的5个足弓关键点。
输出Labelme兼容的JSON格式标注文件。

关键点定义：
  P1 - 跟骨最低点 (Calcaneus inferior)
  P2 - 距骨最低点 (Talus inferior)
  P3 - 第一跖骨头最低点 (1st Metatarsal head)
  P4 - 第五跖骨头最低点 (5th Metatarsal head)
  P5 - 跟骰关节最低点 (Calcaneocuboid joint)

快捷键：
  W/A/S/D    - 平移图像
  滚轮        - 缩放图像
  1-5         - 选择当前标注的关键点
  Tab         - 切换到下一个关键点
  Z           - 撤销当前关键点
  Delete      - 清除当前图片所有标注
  Space/→     - 下一张图片
  ←           - 上一张图片
  Ctrl+S      - 保存标注
  R           - 重置视图
"""

import os
import sys
import json
import base64
from pathlib import Path
from datetime import datetime

import cv2
import numpy as np

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPushButton, QLabel, QListWidget, QListWidgetItem, QGroupBox,
    QStatusBar, QMenuBar, QAction, QFileDialog, QMessageBox,
    QScrollArea, QFrame, QApplication, QAbstractItemView,
    QCheckBox, QProgressBar,
)
from PyQt5.QtCore import Qt, QPoint, QRect, QTimer, pyqtSignal, QEvent
from PyQt5.QtGui import (
    QPixmap, QImage, QPainter, QPen, QColor, QFont,
    QFontMetrics, QMouseEvent, QWheelEvent, QKeyEvent,
    QIcon,
)

# ---------------------------------------------------------------------------
# 关键点定义
# ---------------------------------------------------------------------------
KEYPOINT_DEFS = [
    {"id": 1,  "name": "P1_跟骨最低点",       "color": "#FF4444", "group": "内侧足弓"},
    {"id": 2,  "name": "P2_距骨最低点",       "color": "#44AAFF", "group": "内侧足弓"},
    {"id": 3,  "name": "P3_第一跖骨头最低点", "color": "#44FF44", "group": "内侧足弓"},
    {"id": 4,  "name": "P4_第五跖骨头最低点", "color": "#FFAA00", "group": "外侧足弓"},
    {"id": 5,  "name": "P5_跟骰关节最低点",   "color": "#FF44FF", "group": "外侧足弓"},
]


class ImageCanvas(QLabel):
    """可缩放、平移、标注关键点的图像画布"""

    point_added = pyqtSignal(int, int, int)          # keypoint_id, x, y (image coords)
    point_removed = pyqtSignal(int)                   # keypoint_id
    status_message = pyqtSignal(str)
    measure_completed = pyqtSignal(float, float, float, float, float)  # x1, y1, x2, y2, distance

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(600, 500)
        self.setStyleSheet("background-color: #2b2b2b; border-radius: 8px;")
        self.setAlignment(Qt.AlignCenter)

        # 原始图像（BGR）
        self._src_image = None
        # 显示用的QPixmap
        self._display_pixmap = None

        # 视图变换
        self._scale = 1.0
        self._offset_x = 0
        self._offset_y = 0
        self._panning = False
        self._pan_start = QPoint()

        # 关键点 {(image_x, image_y)} 映射到图像坐标
        self._keypoints = {}          # {kp_id: (x, y)} 图像坐标
        self._active_kp_id = 1        # 当前选中要标注的关键点ID

        # 鼠标悬停
        self._mouse_img_x = -1
        self._mouse_img_y = -1

        # 测距模式
        self._measure_mode = False
        self._measure_points = []     # [(x, y), (x, y)] 图像坐标
        self._measure_distance = 0.0

    def event(self, ev: QEvent):
        """拦截 Tab 键，防止被 Qt 焦点系统消耗"""
        if ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Tab:
            self.keyPressEvent(ev)
            return True
        return super().event(ev)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------
    def load_image(self, image_path: str):
        """加载图像"""
        img = cv2.imread(image_path)
        if img is None:
            return False
        self._src_image = img
        self._keypoints.clear()
        self._reset_view()
        self._render()
        return True

    def set_keypoints(self, kps: dict):
        """设置关键点 {kp_id: (x, y)}"""
        self._keypoints = dict(kps)
        self._render()

    def get_keypoints(self) -> dict:
        """获取当前关键点"""
        return dict(self._keypoints)

    def set_active_keypoint(self, kp_id: int):
        """设置当前活跃关键点"""
        if 1 <= kp_id <= len(KEYPOINT_DEFS):
            self._active_kp_id = kp_id
            self._render()

    def get_active_keypoint(self) -> int:
        return self._active_kp_id

    def clear_all_keypoints(self):
        self._keypoints.clear()
        self._render()

    def undo_last_keypoint(self):
        if self._active_kp_id in self._keypoints:
            del self._keypoints[self._active_kp_id]
            self._render()
            self.point_removed.emit(self._active_kp_id)

    def reset_view(self):
        self._reset_view()
        self._render()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _reset_view(self):
        """重置视图到适合窗口"""
        if self._src_image is None:
            return
        h, w = self._src_image.shape[:2]
        lw = self.width()
        lh = self.height()
        if lw < 10 or lh < 10:
            self._scale = 1.0
        else:
            self._scale = min(lw / w, lh / h) * 0.9
        self._offset_x = (lw - w * self._scale) / 2
        self._offset_y = (lh - h * self._scale) / 2

    def _render(self):
        """渲染图像和标注"""
        if self._src_image is None:
            self.setText("请加载图像")
            return

        h, w = self._src_image.shape[:2]
        # 创建显示图像
        display = self._src_image.copy()
        # 绘制已标注的关键点
        for kp_id, (px, py) in self._keypoints.items():
            if 0 <= px < w and 0 <= py < h:
                color_hex = KEYPOINT_DEFS[kp_id - 1]["color"]
                color_bgr = self._hex_to_bgr(color_hex)
                cv2.circle(display, (px, py), 4, color_bgr, -1)
                cv2.circle(display, (px, py), 6, (255, 255, 255), 1)
                label = f"P{kp_id}"
                cv2.putText(display, label, (px + 10, py + 3),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color_bgr, 1)

        # 如果鼠标在图像区域内，绘制十字准星
        mx, my = self._mouse_img_x, self._mouse_img_y
        if 0 <= mx < w and 0 <= my < h:
            active_color = self._hex_to_bgr(KEYPOINT_DEFS[self._active_kp_id - 1]["color"])
            cv2.line(display, (mx - 15, my), (mx + 15, my), active_color, 1)
            cv2.line(display, (mx, my - 15), (mx, my + 15), active_color, 1)

        # 转换为QPixmap
        display_rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
        h_d, w_d, ch = display_rgb.shape
        bytes_per_line = ch * w_d
        qt_img = QImage(display_rgb.data, w_d, h_d, bytes_per_line, QImage.Format_RGB888)
        self._display_pixmap = QPixmap.fromImage(qt_img)

        self._update_display()

    def _update_display(self):
        """更新控件的缩放显示"""
        if self._display_pixmap is None:
            return
        lw = self.width()
        lh = self.height()
        if lw < 5 or lh < 5:
            return

        scaled = self._display_pixmap.scaled(
            int(self._display_pixmap.width() * self._scale),
            int(self._display_pixmap.height() * self._scale),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        # 在固定大小的画布上绘制
        canvas = QPixmap(lw, lh)
        canvas.fill(QColor("#2b2b2b"))
        painter = QPainter(canvas)
        painter.drawPixmap(int(self._offset_x), int(self._offset_y), scaled)
        painter.end()
        self.setPixmap(canvas)
        self.setText("")

    # ------------------------------------------------------------------
    # 坐标转换
    # ------------------------------------------------------------------
    def _widget_to_image(self, wx: int, wy: int) -> tuple:
        """将控件坐标转换为图像坐标"""
        if self._src_image is None:
            return (-1, -1)
        h, w = self._src_image.shape[:2]
        img_x = (wx - self._offset_x) / self._scale
        img_y = (wy - self._offset_y) / self._scale
        return (int(img_x), int(img_y))

    def _image_to_widget(self, ix: int, iy: int) -> tuple:
        """将图像坐标转换为控件坐标"""
        wx = ix * self._scale + self._offset_x
        wy = iy * self._scale + self._offset_y
        return (int(wx), int(wy))

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def mousePressEvent(self, ev: QMouseEvent):
        if ev.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_start = ev.pos()
            self.setCursor(Qt.ClosedHandCursor)
        elif ev.button() == Qt.LeftButton:
            self._add_keypoint_at(ev.pos())
        elif ev.button() == Qt.RightButton:
            self._panning = True
            self._pan_start = ev.pos()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, ev: QMouseEvent):
        if self._panning:
            delta = ev.pos() - self._pan_start
            self._offset_x += delta.x()
            self._offset_y += delta.y()
            self._pan_start = ev.pos()
            self._update_display()
        else:
            # 更新鼠标位置用于十字准星
            mx, my = self._widget_to_image(ev.pos().x(), ev.pos().y())
            if self._src_image is not None:
                h, w = self._src_image.shape[:2]
                if 0 <= mx < w and 0 <= my < h:
                    self._mouse_img_x = mx
                    self._mouse_img_y = my
                    self.status_message.emit(f"坐标: ({mx}, {my}) | 当前关键点: P{self._active_kp_id}")
                    self._render()
                    return
            self._mouse_img_x = -1
            self._mouse_img_y = -1

    def mouseReleaseEvent(self, ev: QMouseEvent):
        if ev.button() in (Qt.MiddleButton, Qt.RightButton):
            self._panning = False
            self.setCursor(Qt.CrossCursor)

    def wheelEvent(self, ev: QWheelEvent):
        if self._src_image is None:
            return
        old_scale = self._scale
        delta = ev.angleDelta().y()
        if delta > 0:
            self._scale *= 1.1
        else:
            self._scale /= 1.1
        self._scale = max(0.05, min(self._scale, 10.0))

        # 以鼠标位置为中心缩放
        ratio = self._scale / old_scale
        mx = ev.pos().x()
        my = ev.pos().y()
        self._offset_x = mx - ratio * (mx - self._offset_x)
        self._offset_y = my - ratio * (my - self._offset_y)
        self._update_display()

    def keyPressEvent(self, ev: QKeyEvent):
        key = ev.key()
        step = 20
        if key == Qt.Key_W:
            self._offset_y += step
        elif key == Qt.Key_S:
            self._offset_y -= step
        elif key == Qt.Key_A:
            self._offset_x += step
        elif key == Qt.Key_D:
            self._offset_x -= step
        elif key == Qt.Key_R:
            self._reset_view()
        elif key in (Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4, Qt.Key_5):
            kp_id = key - Qt.Key_0
            self._active_kp_id = kp_id
            self.status_message.emit(f"切换到关键点 P{kp_id}: {KEYPOINT_DEFS[kp_id - 1]['name']}")
        elif key == Qt.Key_Tab:
            self._active_kp_id = self._active_kp_id % len(KEYPOINT_DEFS) + 1
            self.status_message.emit(f"切换到关键点 P{self._active_kp_id}: {KEYPOINT_DEFS[self._active_kp_id - 1]['name']}")
        elif key == Qt.Key_Z:
            self.undo_last_keypoint()
        elif key == Qt.Key_Delete:
            self.clear_all_keypoints()
            self.status_message.emit("已清除所有关键点")
        elif key == Qt.Key_Escape:
            self._active_kp_id = 1
            self.status_message.emit("已重置到 P1")
        self._render()

    def _add_keypoint_at(self, pos: QPoint):
        """在指定位置添加关键点"""
        if self._src_image is None:
            return
        img_x, img_y = self._widget_to_image(pos.x(), pos.y())
        h, w = self._src_image.shape[:2]
        if 0 <= img_x < w and 0 <= img_y < h:
            self._keypoints[self._active_kp_id] = (img_x, img_y)
            kp_name = KEYPOINT_DEFS[self._active_kp_id - 1]["name"]
            self.status_message.emit(f"已标注 P{self._active_kp_id} ({kp_name}) 于 ({img_x}, {img_y})")
            self.point_added.emit(self._active_kp_id, img_x, img_y)
            self._render()

    def leaveEvent(self, ev):
        self._mouse_img_x = -1
        self._mouse_img_y = -1

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._src_image is not None:
            self._update_display()

    @staticmethod
    def _hex_to_bgr(hex_color: str) -> tuple:
        hex_color = hex_color.lstrip('#')
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        return (b, g, r)  # OpenCV BGR


class FootArchAnnotationTool(QMainWindow):
    """足弓关键点标注工具主窗口"""

    def __init__(self, dataset_root: str = None):
        super().__init__()
        self.setWindowTitle("足弓关键点标注工具 - Labelme JSON 格式")
        self.setGeometry(100, 50, 1500, 900)

        # 数据集根目录
        self._dataset_root = None
        # 图像列表
        self._image_paths = []
        self._current_index = -1
        # 标注数据
        self._annotations = {}   # {image_path: {kp_id: (x,y)}}

        self._init_ui()
        self._init_menu()

        if dataset_root and os.path.isdir(dataset_root):
            self._open_dataset(dataset_root)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # 状态栏（需要先创建，因为面板初始化时会引用）
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage("就绪 - 请打开数据集文件夹")

        # 进度标签
        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("padding: 0 10px; color: #666;")
        self._statusbar.addPermanentWidget(self._progress_label)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet("QSplitter::handle { background-color: #e0e0e0; }")
        main_layout.addWidget(splitter)

        # 左侧面板 - 图像列表 + 关键点面板
        left_panel = self._create_left_panel()
        splitter.addWidget(left_panel)

        # 中间 - 图像画布
        center_panel = self._create_center_panel()
        splitter.addWidget(center_panel)

        # 右侧面板 - 关键点状态 + 说明
        right_panel = self._create_right_panel()
        splitter.addWidget(right_panel)

        splitter.setSizes([280, 900, 260])

    def _create_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(260)
        panel.setMaximumWidth(320)
        panel.setStyleSheet("background-color: #f8f9fa;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # 标题
        title = QLabel("图像列表")
        title.setStyleSheet("font-size: 14px; font-weight: bold; color: #0078d4; padding: 4px;")
        layout.addWidget(title)

        # 搜索过滤
        self._search_edit = QLabel("")
        self._search_edit.hide()  # 简单实现，后续可加搜索

        # 图像列表
        self._image_list = QListWidget()
        self._image_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._image_list.setStyleSheet("""
            QListWidget {
                background-color: white;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                font-size: 11px;
                outline: none;
            }
            QListWidget::item {
                padding: 6px 10px;
                border-bottom: 1px solid #f5f5f5;
                min-height: 24px;
                color: #333;
            }
            QListWidget::item:selected {
                background-color: #e6f2ff;
                color: #0078d4;
                border-left: 3px solid #0078d4;
            }
            QListWidget::item:hover {
                background-color: #fafcff;
            }
        """)
        self._image_list.itemClicked.connect(self._on_image_selected)
        layout.addWidget(self._image_list)

        # 按钮组
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(6)

        self._open_btn = QPushButton("打开数据集")
        self._open_btn.setStyleSheet(self._btn_style("#28a745"))
        self._open_btn.clicked.connect(self._on_open_dataset)
        btn_layout.addWidget(self._open_btn)

        # 分类筛选
        filter_layout = QHBoxLayout()
        self._filter_all_cb = QCheckBox("全部")
        self._filter_all_cb.setChecked(True)
        self._filter_all_cb.setStyleSheet("font-size: 11px;")
        self._filter_pesplanus_cb = QCheckBox("扁平足")
        self._filter_pesplanus_cb.setStyleSheet("font-size: 11px; color: #d32f2f;")
        self._filter_normal_cb = QCheckBox("正常")
        self._filter_normal_cb.setStyleSheet("font-size: 11px; color: #107c10;")
        self._filter_all_cb.toggled.connect(self._on_filter_changed)
        self._filter_pesplanus_cb.toggled.connect(self._on_filter_changed)
        self._filter_normal_cb.toggled.connect(self._on_filter_changed)
        filter_layout.addWidget(self._filter_all_cb)
        filter_layout.addWidget(self._filter_pesplanus_cb)
        filter_layout.addWidget(self._filter_normal_cb)
        btn_layout.addLayout(filter_layout)

        # 导航按钮
        nav_layout = QHBoxLayout()
        self._prev_btn = QPushButton("← 上一张")
        self._prev_btn.setStyleSheet(self._btn_style("#6c757d"))
        self._prev_btn.clicked.connect(self._on_prev_image)
        self._next_btn = QPushButton("下一张 →")
        self._next_btn.setStyleSheet(self._btn_style("#0078d4"))
        self._next_btn.clicked.connect(self._on_next_image)
        nav_layout.addWidget(self._prev_btn)
        nav_layout.addWidget(self._next_btn)
        btn_layout.addLayout(nav_layout)

        self._save_btn = QPushButton("保存标注 (Ctrl+S)")
        self._save_btn.setStyleSheet(self._btn_style("#107c10"))
        self._save_btn.clicked.connect(self._on_save_annotations)
        btn_layout.addWidget(self._save_btn)

        layout.addLayout(btn_layout)
        return panel

    def _create_center_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background-color: #f5f6f7;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        self._image_label = QLabel("足弓关键点标注工具\n\n请打开数据集文件夹开始标注")
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setStyleSheet("color: #888; font-size: 14px; background-color: #2b2b2b; border-radius: 8px;")

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        self._canvas = ImageCanvas()
        self._canvas.point_added.connect(self._on_point_added)
        self._canvas.point_removed.connect(self._on_point_removed)
        self._canvas.status_message.connect(self._statusbar.showMessage)

        scroll.setWidget(self._canvas)
        layout.addWidget(scroll)

        # 提示条
        hint = QLabel("左键: 放置关键点 | 右键/中键: 拖拽平移 | 滚轮: 缩放 | 1-5: 切换关键点 | Tab: 下一个关键点")
        hint.setStyleSheet("color: #888; font-size: 10px; padding: 4px;")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

        return panel

    def _create_right_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(240)
        panel.setMaximumWidth(300)
        panel.setStyleSheet("background-color: #f8f9fa;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # 关键点状态
        kp_group = QGroupBox("关键点状态")
        kp_group.setStyleSheet("""
            QGroupBox {
                background-color: white;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 20px;
                font-weight: bold;
                font-size: 11px;
                color: #0078d4;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
            }
        """)
        kp_layout = QVBoxLayout(kp_group)
        kp_layout.setSpacing(4)

        self._kp_labels = []
        for kp in KEYPOINT_DEFS:
            row = QHBoxLayout()
            row.setSpacing(6)
            indicator = QLabel("●")
            indicator.setStyleSheet(f"color: {kp['color']}; font-size: 14px; font-weight: bold;")
            indicator.setFixedWidth(18)
            row.addWidget(indicator)

            name_label = QLabel(kp["name"])
            name_label.setStyleSheet("font-size: 11px; color: #333;")
            row.addWidget(name_label)

            status_label = QLabel("未标注")
            status_label.setStyleSheet("font-size: 10px; color: #999;")
            status_label.setAlignment(Qt.AlignRight)
            row.addWidget(status_label)
            row.addStretch()

            kp_layout.addLayout(row)
            self._kp_labels.append((indicator, name_label, status_label))

        layout.addWidget(kp_group)

        # 当前图像信息
        info_group = QGroupBox("当前图像")
        info_group.setStyleSheet("""
            QGroupBox {
                background-color: white;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 20px;
                font-weight: bold;
                font-size: 11px;
                color: #0078d4;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
            }
        """)
        info_layout = QVBoxLayout(info_group)
        self._info_label = QLabel("未加载图像")
        self._info_label.setStyleSheet("font-size: 11px; color: #666; padding: 4px;")
        self._info_label.setWordWrap(True)
        info_layout.addWidget(self._info_label)
        layout.addWidget(info_group)

        # 足弓测量预览
        measure_group = QGroupBox("足弓测量预览")
        measure_group.setStyleSheet("""
            QGroupBox {
                background-color: white;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 20px;
                font-weight: bold;
                font-size: 11px;
                color: #0078d4;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
            }
        """)
        measure_layout = QVBoxLayout(measure_group)
        self._measure_label = QLabel("标注完关键点后\n自动计算足弓角度")
        self._measure_label.setStyleSheet("font-size: 11px; color: #999; padding: 4px;")
        self._measure_label.setWordWrap(True)
        measure_layout.addWidget(self._measure_label)
        layout.addWidget(measure_group)

        layout.addStretch()
        return panel

    def _init_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件")

        open_action = QAction("打开数据集", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_dataset)
        file_menu.addAction(open_action)

        save_action = QAction("保存标注", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self._on_save_annotations)
        file_menu.addAction(save_action)

        file_menu.addSeparator()

        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = menubar.addMenu("视图")
        reset_action = QAction("重置视图", self)
        reset_action.setShortcut("R")
        reset_action.triggered.connect(lambda: self._canvas.reset_view())
        view_menu.addAction(reset_action)

        help_menu = menubar.addMenu("帮助")
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    # ------------------------------------------------------------------
    # 数据集加载
    # ------------------------------------------------------------------
    def _on_open_dataset(self):
        folder = QFileDialog.getExistingDirectory(self, "选择数据集根目录", "")
        if folder:
            self._open_dataset(folder)

    def _open_dataset(self, folder: str):
        self._dataset_root = folder

        # 扫描图像
        self._image_paths = []
        for root, dirs, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')):
                    self._image_paths.append(os.path.normpath(os.path.join(root, f)))

        self._image_paths.sort()
        self._annotations = {}

        # 加载已有标注
        self._load_existing_annotations()

        # 刷新列表
        self._refresh_image_list()

        self._statusbar.showMessage(f"已加载 {len(self._image_paths)} 张图像，{len(self._annotations)} 张已标注")

    def _load_existing_annotations(self):
        """加载已有的标注文件（JSON与图片同名同目录）"""
        for img_path in self._image_paths:
            # 查找与图片同名的JSON文件
            base = os.path.splitext(img_path)[0]
            json_path = base + ".json"
            if not os.path.exists(json_path):
                continue
            try:
                with open(json_path, 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
                kps = {}
                for shape in data.get("shapes", []):
                    if shape.get("shape_type") == "point":
                        label = shape.get("label", "")
                        kp_id = self._parse_kp_id(label)
                        if kp_id and shape.get("points"):
                            pt = shape["points"][0]
                            kps[kp_id] = (int(pt[0]), int(pt[1]))
                if kps:
                    self._annotations[img_path] = kps
            except Exception as e:
                print(f"加载标注文件失败: {json_path}, {e}")

    def _parse_kp_id(self, label: str) -> int:
        """从标注标签中解析关键点ID"""
        for kp in KEYPOINT_DEFS:
            if kp["name"] == label:
                return kp["id"]
        # 兼容 P1, P2 等格式
        if label.startswith("P") and len(label) >= 2:
            try:
                return int(label[1:])
            except ValueError:
                pass
        return 0

    def _refresh_image_list(self, filter_text: str = None):
        """刷新图像列表"""
        self._image_list.clear()
        filtered = list(self._image_paths)

        # 分类筛选
        if not self._filter_all_cb.isChecked():
            filtered = []
            if self._filter_pesplanus_cb.isChecked():
                filtered.extend([p for p in self._image_paths if 'pesplanus' in Path(p).parent.name.lower() and 'not' not in Path(p).parent.name.lower()])
            if self._filter_normal_cb.isChecked():
                filtered.extend([p for p in self._image_paths if 'notpesplanus' in Path(p).parent.name.lower()])

        for img_path in filtered:
            rel_path = os.path.relpath(img_path, self._dataset_root) if self._dataset_root else os.path.basename(img_path)
            has_anno = img_path in self._annotations
            prefix = "✓ " if has_anno else "  "
            item = QListWidgetItem(f"{prefix}{rel_path}")
            item.setData(Qt.UserRole, img_path)
            if has_anno:
                item.setForeground(QColor("#107c10"))
            self._image_list.addItem(item)

        # 更新进度
        total = len(self._image_paths)
        annotated = len(self._annotations)
        self._progress_label.setText(f"进度: {annotated}/{total} ({annotated*100//total if total else 0}%)")

    # ------------------------------------------------------------------
    # 图像选择与导航
    # ------------------------------------------------------------------
    def _on_image_selected(self, item: QListWidgetItem):
        img_path = item.data(Qt.UserRole)
        if img_path:
            self._load_image(img_path)

    def _load_image(self, img_path: str):
        if not os.path.exists(img_path):
            return
        success = self._canvas.load_image(img_path)
        if not success:
            QMessageBox.warning(self, "错误", f"无法加载图像: {img_path}")
            return

        self._current_index = self._image_paths.index(img_path) if img_path in self._image_paths else -1

        # 恢复已有标注
        if img_path in self._annotations:
            self._canvas.set_keypoints(self._annotations[img_path])

        # 更新UI
        self._update_info_panel(img_path)
        self._update_kp_status()
        self._update_measure_preview()

        # 选中列表项
        for i in range(self._image_list.count()):
            item = self._image_list.item(i)
            if item.data(Qt.UserRole) == img_path:
                self._image_list.setCurrentItem(item)
                self._image_list.scrollToItem(item)
                break

        self._statusbar.showMessage(f"已加载: {os.path.basename(img_path)}")

    def _on_next_image(self):
        if not self._image_paths:
            return
        self._save_current_annotation()
        next_idx = (self._current_index + 1) % len(self._image_paths)
        self._load_image(self._image_paths[next_idx])

    def _on_prev_image(self):
        if not self._image_paths:
            return
        self._save_current_annotation()
        prev_idx = (self._current_index - 1) % len(self._image_paths)
        self._load_image(self._image_paths[prev_idx])

    def _on_filter_changed(self):
        """分类筛选变化"""
        sender = self.sender()
        if sender == self._filter_all_cb and self._filter_all_cb.isChecked():
            self._filter_pesplanus_cb.setChecked(False)
            self._filter_normal_cb.setChecked(False)
        elif sender in (self._filter_pesplanus_cb, self._filter_normal_cb):
            if self._filter_pesplanus_cb.isChecked() or self._filter_normal_cb.isChecked():
                self._filter_all_cb.setChecked(False)
            if not self._filter_pesplanus_cb.isChecked() and not self._filter_normal_cb.isChecked():
                self._filter_all_cb.setChecked(True)
        self._refresh_image_list()

    # ------------------------------------------------------------------
    # 关键点状态更新
    # ------------------------------------------------------------------
    def _on_point_added(self, kp_id: int, x: int, y: int):
        self._update_kp_status()
        self._update_measure_preview()

    def _on_point_removed(self, kp_id: int):
        self._update_kp_status()
        self._update_measure_preview()

    def _update_kp_status(self):
        """更新右侧关键点状态面板"""
        kps = self._canvas.get_keypoints()
        for i, kp in enumerate(KEYPOINT_DEFS):
            _, _, status_label = self._kp_labels[i]
            if kp["id"] in kps:
                x, y = kps[kp["id"]]
                status_label.setText(f"({x}, {y})")
                status_label.setStyleSheet("font-size: 10px; color: #107c10;")
            else:
                status_label.setText("未标注")
                status_label.setStyleSheet("font-size: 10px; color: #999;")

        # 高亮活跃关键点
        active_id = self._canvas.get_active_keypoint()
        for i, kp in enumerate(KEYPOINT_DEFS):
            indicator, name_label, _ = self._kp_labels[i]
            if kp["id"] == active_id:
                name_label.setStyleSheet("font-size: 11px; color: #333; font-weight: bold;")
            else:
                name_label.setStyleSheet("font-size: 11px; color: #333;")

    def _update_info_panel(self, img_path: str):
        """更新当前图像信息面板"""
        rel_path = os.path.relpath(img_path, self._dataset_root) if self._dataset_root else img_path
        parent = Path(img_path).parent.name
        category = "扁平足" if "pesplanus" in parent.lower() and "not" not in parent.lower() else "正常"
        fname = os.path.basename(img_path)
        info = f"文件名: {fname}\n类别: {category}\n路径: {rel_path}"
        self._info_label.setText(info)

    def _update_measure_preview(self):
        """根据已标注关键点计算足弓角度预览"""
        kps = self._canvas.get_keypoints()
        lines = []

        # 内侧足弓角：以P2(距骨)为顶点，∠P1-P2-P3
        if 1 in kps and 2 in kps and 3 in kps:
            p1 = np.array(kps[1])
            p2 = np.array(kps[2])
            p3 = np.array(kps[3])
            v1 = p1 - p2   # P2 -> P1
            v2 = p3 - p2   # P2 -> P3
            cos_a = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-8)
            cos_a = np.clip(cos_a, -1, 1)
            medial_angle = np.degrees(np.arccos(cos_a))
            lines.append(f"内侧足弓角: {medial_angle:.1f}°")

        # 外侧足弓角：以P5(跟骰关节)为顶点，∠P1-P5-P4
        if 1 in kps and 4 in kps and 5 in kps:
            p1 = np.array(kps[1])
            p4 = np.array(kps[4])
            p5 = np.array(kps[5])
            v1 = p1 - p5   # P5 -> P1
            v2 = p4 - p5   # P5 -> P4
            cos_a = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-8)
            cos_a = np.clip(cos_a, -1, 1)
            lateral_angle = np.degrees(np.arccos(cos_a))
            lines.append(f"外侧足弓角: {lateral_angle:.1f}°")

        if not lines:
            self._measure_label.setText("标注完关键点后\n自动计算足弓角度")
        else:
            self._measure_label.setText("\n".join(lines))

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------
    def _save_current_annotation(self):
        """保存当前图像的标注到内存，并自动写入JSON文件"""
        if self._current_index < 0 or self._current_index >= len(self._image_paths):
            return
        img_path = self._image_paths[self._current_index]
        kps = self._canvas.get_keypoints()
        if kps:
            self._annotations[img_path] = kps
            self._mark_item_annotated(img_path)
            # 自动保存单个JSON文件
            self._save_single_json(img_path, kps)
        elif img_path in self._annotations:
            del self._annotations[img_path]

    def _mark_item_annotated(self, img_path: str):
        """更新列表中对应项的颜色和前缀"""
        for i in range(self._image_list.count()):
            item = self._image_list.item(i)
            if item.data(Qt.UserRole) == img_path:
                rel_path = os.path.relpath(img_path, self._dataset_root) if self._dataset_root else os.path.basename(img_path)
                item.setText(f"✓ {rel_path}")
                item.setForeground(QColor("#107c10"))
                # 更新进度
                self._update_progress_label()
                break

    def _update_progress_label(self):
        total = len(self._image_paths)
        annotated = len(self._annotations)
        self._progress_label.setText(f"进度: {annotated}/{total} ({annotated*100//total if total else 0}%)")

    def _save_single_json(self, img_path: str, kps: dict):
        """保存单张图像的标注为Labelme JSON文件（与图片同名同目录）"""
        shapes = []
        for kp_id, (x, y) in sorted(kps.items()):
            if 1 <= kp_id <= len(KEYPOINT_DEFS):
                shapes.append({
                    "label": KEYPOINT_DEFS[kp_id - 1]["name"],
                    "points": [[x, y]],
                    "group_id": None,
                    "shape_type": "point",
                    "flags": {}
                })
        if not shapes:
            return

        img = cv2.imread(img_path)
        if img is None:
            return
        h, w = img.shape[:2]

        annotation = {
            "version": "5.0.1",
            "flags": {},
            "shapes": shapes,
            "imageData": None,
            "imageHeight": h,
            "imageWidth": w,
        }
        base = os.path.splitext(img_path)[0]
        json_path = base + ".json"
        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(annotation, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"自动保存失败: {json_path}, {e}")

    def _on_save_annotations(self):
        """保存所有标注到JSON文件（与图片同名同目录）"""
        self._save_current_annotation()
        if not self._dataset_root:
            QMessageBox.warning(self, "提示", "请先打开数据集")
            return

        saved_count = 0
        for img_path, kps in self._annotations.items():
            self._save_single_json(img_path, kps)
            saved_count += 1

        # 刷新列表
        self._refresh_image_list()
        self._statusbar.showMessage(f"已保存 {saved_count} 个标注文件")
        QMessageBox.information(self, "保存成功", f"已保存 {saved_count} 个标注文件\nJSON文件与图片存放在同一目录")

    # ------------------------------------------------------------------
    # 键盘事件转发
    # ------------------------------------------------------------------
    def keyPressEvent(self, ev: QKeyEvent):
        key = ev.key()
        if key == Qt.Key_Space or key == Qt.Key_Right:
            self._on_next_image()
        elif key == Qt.Key_Left:
            self._on_prev_image()
        elif key == Qt.Key_Delete:
            self._canvas.clear_all_keypoints()
            self._update_kp_status()
            self._update_measure_preview()
            self._statusbar.showMessage("已清除所有关键点")
        elif key == Qt.Key_Tab:
            # Tab 被 Qt 焦点系统拦截，需要在主窗口层显式处理
            self._canvas.keyPressEvent(ev)
            ev.accept()
        else:
            self._canvas.keyPressEvent(ev)

    # ------------------------------------------------------------------
    # 关闭事件
    # ------------------------------------------------------------------
    def closeEvent(self, ev):
        self._save_current_annotation()
        unsaved = len(self._annotations)
        if unsaved > 0:
            reply = QMessageBox.question(
                self, "确认退出",
                f"还有 {unsaved} 张图像的标注未保存到文件。\n是否保存后退出？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
            )
            if reply == QMessageBox.Save:
                self._on_save_annotations()
                ev.accept()
            elif reply == QMessageBox.Discard:
                ev.accept()
            else:
                ev.ignore()
        else:
            ev.accept()

    def _on_about(self):
        QMessageBox.about(self, "关于 - 足弓关键点标注工具",
                          """<h3>足弓关键点标注工具 v1.0</h3>
<p>用于标注足部负重侧位X线片的5个足弓关键点</p>
<p>输出Labelme兼容的JSON格式</p>
<hr>
<p><b>关键点:</b></p>
<ul>
<li>P1 - 跟骨最低点</li>
<li>P2 - 距骨最低点</li>
<li>P3 - 第一跖骨头最低点</li>
<li>P4 - 第五跖骨头最低点</li>
<li>P5 - 跟骰关节最低点</li>
</ul>
<p><b>快捷键:</b> 1-5选关键点, 左键放置, 滚轮缩放, 右键拖拽</p>""")

    @staticmethod
    def _btn_style(color: str) -> str:
        return f"""
            QPushButton {{
                background-color: {color};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 14px;
                font-weight: 600;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: {color}dd;
            }}
            QPushButton:disabled {{
                background-color: #cccccc;
                color: #666666;
            }}
        """


def launch_annotation_tool(dataset_root: str = None):
    """启动标注工具"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    # 默认数据集路径
    if dataset_root is None:
        default_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "Pesplanus Dataset")
        if os.path.isdir(default_root):
            dataset_root = default_root

    window = FootArchAnnotationTool(dataset_root)
    window.show()
    return window


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = launch_annotation_tool()
    sys.exit(app.exec_())