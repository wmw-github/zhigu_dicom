"""
脊柱侧弯检测对话框

基于YOLOv8 ONNX模型的脊柱X线片检测与Cobb角自动测量
"""
import sys
import os
import cv2
import numpy as np
from pathlib import Path

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QMessageBox,
    QGroupBox, QProgressBar, QTextEdit,
    QSplitter
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage

from core.model.spine_inference import get_spine_inference
from utils.logger import get_logger
from utils.image_io import imwrite_cn

logger = get_logger(__name__)


class SpineDetectionThread(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(int)

    def __init__(self, img_path, conf_threshold=0.25):
        super().__init__()
        self.img_path = img_path
        self.conf_threshold = conf_threshold
        self.detector = get_spine_inference()

    def run(self):
        try:
            for i in range(0, 80, 20):
                self.progress.emit(i)
                self.msleep(50)

            result = self.detector.detect(self.img_path, conf_threshold=self.conf_threshold)
            self.progress.emit(100)
            self.finished.emit(result)
        except Exception as e:
            logger.error(f"脊柱检测失败: {e}")
            self.error.emit(str(e))


class SpineDialog(QDialog):
    def __init__(self, parent=None, image_path=None):
        super().__init__(parent)
        self.setWindowTitle("脊柱侧弯检测系统 - Cobb角测量")
        self.setMinimumSize(1200, 800)

        self.current_image_path = image_path
        self.current_result = None
        self.detection_thread = None

        self._init_ui()

        if self.current_image_path:
            self._load_image(self.current_image_path)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("脊柱侧弯检测系统")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #0078d4; padding: 12px;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("基于YOLOv8深度学习模型 - Cobb角自动测量")
        subtitle.setStyleSheet("font-size: 14px; color: #666666;")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)

        left_panel = self._create_left_panel()
        splitter.addWidget(left_panel)

        right_panel = self._create_right_panel()
        splitter.addWidget(right_panel)

        splitter.setSizes([600, 600])

    def _create_left_panel(self):
        panel = QWidget()
        panel.setStyleSheet("background-color: #f8f9fa;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        image_group = QGroupBox("脊柱X线片")
        image_group.setStyleSheet(self._get_groupbox_style())
        image_layout = QVBoxLayout(image_group)

        self.original_label = QLabel("请选择脊柱X线片图像")
        self.original_label.setAlignment(Qt.AlignCenter)
        self.original_label.setStyleSheet("""
            background-color: #2b2b2b;
            color: #888888;
            font-size: 14px;
            border-radius: 8px;
            min-height: 400px;
        """)
        image_layout.addWidget(self.original_label)

        layout.addWidget(image_group)

        btn_layout = QHBoxLayout()

        self.open_btn = QPushButton("选择图像")
        self.open_btn.setStyleSheet(self._get_primary_button_style())
        self.open_btn.clicked.connect(self._on_open_image)
        btn_layout.addWidget(self.open_btn)

        self.detect_btn = QPushButton("开始检测")
        self.detect_btn.setStyleSheet(self._get_detect_button_style())
        self.detect_btn.clicked.connect(self._on_start_detection)
        self.detect_btn.setEnabled(self.current_image_path is not None)
        btn_layout.addWidget(self.detect_btn)

        layout.addLayout(btn_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                border-radius: 4px;
                background-color: #f0f0f0;
                height: 8px;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
                border-radius: 4px;
            }
        """)
        layout.addWidget(self.progress_bar)

        return panel

    def _create_right_panel(self):
        panel = QWidget()
        panel.setStyleSheet("background-color: #f8f9fa;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        result_group = QGroupBox("检测结果")
        result_group.setStyleSheet(self._get_groupbox_style())
        result_layout = QVBoxLayout(result_group)

        self.result_label = QLabel("检测结果将显示在这里")
        self.result_label.setAlignment(Qt.AlignCenter)
        self.result_label.setStyleSheet("""
            background-color: #2b2b2b;
            color: #888888;
            font-size: 14px;
            border-radius: 8px;
            min-height: 400px;
        """)
        result_layout.addWidget(self.result_label)

        layout.addWidget(result_group)

        summary_group = QGroupBox("检测摘要")
        summary_group.setStyleSheet(self._get_groupbox_style())
        summary_layout = QVBoxLayout(summary_group)

        self.summary_label = QLabel("暂无检测结果")
        self.summary_label.setStyleSheet("font-size: 16px; color: #333333; padding: 12px;")
        self.summary_label.setAlignment(Qt.AlignCenter)
        summary_layout.addWidget(self.summary_label)

        layout.addWidget(summary_group)

        report_group = QGroupBox("详细报告")
        report_group.setStyleSheet(self._get_groupbox_style())
        report_layout = QVBoxLayout(report_group)

        self.report_text = QTextEdit()
        self.report_text.setReadOnly(True)
        self.report_text.setStyleSheet("""
            background-color: white;
            border: 1px solid #e0e0e0;
            border-radius: 4px;
            padding: 8px;
            font-size: 12px;
            line-height: 1.6;
        """)
        self.report_text.setText("""=== 脊柱侧弯检测系统使用说明 ===

1. 点击"选择图像"按钮上传脊柱X线片
2. 点击"开始检测"进行脊柱分析

系统将自动：
- 检测脊柱是否侧弯
- 识别并定位所有椎体
- 计算Cobb角度
- 标记端椎和顶椎位置

Cobb角临床意义：
• < 10°: 正常生理弯曲
• 10°-25°: 轻度侧弯，建议随访
• 25°-40°: 中度侧弯，建议支具治疗
• > 40°: 重度侧弯，需手术评估

注意：本结果仅供参考，请结合临床判断。""")
        report_layout.addWidget(self.report_text)

        layout.addWidget(report_group)

        self.save_btn = QPushButton("保存标注图片")
        self.save_btn.setObjectName("save_btn")
        self.save_btn.setStyleSheet(self._get_save_button_style())
        self.save_btn.clicked.connect(self._on_save_image)
        self.save_btn.setEnabled(False)
        self.save_btn.setMinimumHeight(45)
        layout.addWidget(self.save_btn)

        return panel

    def _get_groupbox_style(self):
        return """
            QGroupBox {
                background-color: white;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 24px;
                font-weight: bold;
                color: #0078d4;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 8px;
            }
        """

    def _get_primary_button_style(self):
        return """
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 12px 24px;
                font-weight: 600;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
        """

    def _get_detect_button_style(self):
        return """
            QPushButton {
                background-color: #107c10;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 12px 24px;
                font-weight: 600;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #0e6e0e;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """

    def _get_save_button_style(self):
        return """
            QPushButton {
                background-color: #ff9800;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 12px 24px;
                font-weight: 600;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #e68900;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """

    def _on_open_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择脊柱X线片",
            "",
            "图像文件 (*.png *.jpg *.jpeg *.bmp *.dcm *.dicom)"
        )
        if file_path:
            self._load_image(file_path)

    def _load_image(self, file_path):
        self.current_image_path = file_path
        pixmap = QPixmap(file_path)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                self.original_label.width() - 20,
                self.original_label.height() - 20,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
            self.original_label.setPixmap(scaled)
            self.detect_btn.setEnabled(True)
        else:
            QMessageBox.warning(self, "警告", "无法加载图像文件")

    def _on_start_detection(self):
        if not self.current_image_path:
            QMessageBox.warning(self, "警告", "请先选择图像")
            return

        self.open_btn.setEnabled(False)
        self.detect_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        self.detection_thread = SpineDetectionThread(self.current_image_path)
        self.detection_thread.finished.connect(self._on_detection_finished)
        self.detection_thread.error.connect(self._on_detection_error)
        self.detection_thread.progress.connect(self._on_detection_progress)
        self.detection_thread.start()

    def _on_detection_progress(self, value):
        self.progress_bar.setValue(value)

    def _on_detection_finished(self, result):
        self.open_btn.setEnabled(True)
        self.detect_btn.setEnabled(True)
        self.progress_bar.setVisible(False)

        if not result.get("success", False):
            QMessageBox.critical(self, "检测失败", result.get("error", "未知错误"))
            return

        self.current_result = result

        result_img = result.get("result_image")
        if result_img is not None:
            result_img_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
            h, w, ch = result_img_rgb.shape
            bytes_per_line = ch * w
            qt_image = QImage(result_img_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)
            scaled = pixmap.scaled(
                self.result_label.width() - 20,
                self.result_label.height() - 20,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
            self.result_label.setPixmap(scaled)

        cobb_angle = result.get("cobb_angle")
        vertebra_count = result.get("vertebra_count", 0)
        spine_class = result.get("spine_class")

        summary_parts = [f"<b>椎体数:</b> {vertebra_count} 个"]

        if spine_class:
            from core.model.spine_inference import LABEL_MAP
            cls_name = spine_class['cls_name']
            cn_name = LABEL_MAP.get(cls_name, cls_name)
            summary_parts.append(f"<b>分类:</b> {cn_name} ({spine_class['confidence']:.1%})")

        if cobb_angle is not None:
            if cobb_angle > 40:
                severity = "重度侧弯"
            elif cobb_angle > 25:
                severity = "中度侧弯"
            elif cobb_angle > 10:
                severity = "轻度侧弯"
            else:
                severity = "正常范围"
            summary_parts.append(f"<b>Cobb角:</b> {cobb_angle:.1f}° ({severity})")
        else:
            summary_parts.append("<b>Cobb角:</b> 无法计算")

        self.summary_label.setText("<br>".join(summary_parts))

        report = result.get("report", "")
        self.report_text.setText(report)

        self.save_btn.setEnabled(True)
        self.save_btn.setVisible(True)

        from PyQt5.QtCore import QTimer
        if cobb_angle is not None:
            QTimer.singleShot(100, lambda: QMessageBox.information(
                self, "检测完成", f"脊柱检测完成！\nCobb角: {cobb_angle:.1f}°"))
        else:
            QTimer.singleShot(100, lambda: QMessageBox.information(
                self, "检测完成", f"脊柱检测完成！\n检测到 {vertebra_count} 个椎体"))

    def _on_detection_error(self, error_msg):
        self.open_btn.setEnabled(True)
        self.detect_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        QMessageBox.critical(self, "检测错误", error_msg)

    def _on_save_image(self):
        if not self.current_result:
            QMessageBox.warning(self, "警告", "没有可保存的检测结果")
            return

        from datetime import datetime

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存标注图片",
            f"脊柱检测_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            "PNG图片 (*.png);;JPEG图片 (*.jpg)"
        )

        if file_path:
            try:
                result_img = self.current_result.get("result_image")
                if result_img is None:
                    raise ValueError("没有结果图片数据")

                if len(result_img.shape) == 3 and result_img.shape[2] == 3:
                    result_img_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
                else:
                    result_img_rgb = result_img

                success = imwrite_cn(file_path, result_img_rgb)
                if success:
                    QMessageBox.information(self, "成功", f"标注图片已保存到:\n{file_path}")
                else:
                    raise ValueError("图像保存失败")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"保存图片失败:\n{str(e)}")


def show_spine_dialog(parent=None, image_path=None):
    dialog = SpineDialog(parent, image_path)
    return dialog.exec_()


if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    dialog = SpineDialog()
    dialog.show()

    sys.exit(app.exec_())