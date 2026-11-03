"""
骨龄检测对话框

基于YOLOv8的骨龄检测系统，采用RUS-CHN法进行骨龄评估
"""
import sys
import os
import cv2
import numpy as np
from pathlib import Path

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QMessageBox,
    QGroupBox, QRadioButton, QProgressBar, QTextEdit,
    QSplitter, QScrollArea, QFrame, QCheckBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QFont

from core.model.boneage_inference import get_boneage_inference
from utils.logger import get_logger
from utils.image_io import imwrite_cn

logger = get_logger(__name__)


class BoneAgeDetectionThread(QThread):
    """骨龄检测线程"""
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(int)
    
    def __init__(self, img_path: str, sex: str):
        super().__init__()
        self.img_path = img_path
        self.sex = sex
        self.detector = get_boneage_inference()
    
    def run(self):
        try:
            # 模拟进度
            for i in range(0, 50, 10):
                self.progress.emit(i)
                self.msleep(100)
            
            # 执行检测
            result = self.detector.detect(self.img_path, self.sex)
            
            self.progress.emit(100)
            self.finished.emit(result)
            
        except Exception as e:
            logger.error(f"骨龄检测失败: {e}")
            self.error.emit(str(e))


class BoneAgeDialog(QDialog):
    """骨龄检测对话框"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("骨龄检测系统 - RUS-CHN法")
        self.setMinimumSize(1200, 800)
        
        self.current_image_path = None
        self.current_result = None
        self.detection_thread = None
        
        self._init_ui()
        self._load_model()
    
    def _init_ui(self):
        """初始化UI"""
        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        
        # 标题
        title = QLabel("骨龄检测系统")
        title.setStyleSheet("""
            font-size: 24px;
            font-weight: bold;
            color: #0078d4;
            padding: 12px;
        """)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        # 副标题
        subtitle = QLabel("基于YOLOv8深度学习模型 - RUS-CHN法骨龄评估")
        subtitle.setStyleSheet("font-size: 14px; color: #666666;")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)
        
        # 分割器
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter)
        
        # 左侧面板
        left_panel = self._create_left_panel()
        splitter.addWidget(left_panel)
        
        # 右侧面板
        right_panel = self._create_right_panel()
        splitter.addWidget(right_panel)
        
        splitter.setSizes([600, 600])
    
    def _create_left_panel(self):
        """创建左侧面板"""
        panel = QWidget()
        panel.setStyleSheet("background-color: #f8f9fa;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        
        # 原始图像显示
        image_group = QGroupBox("手部X光片")
        image_group.setStyleSheet(self._get_groupbox_style())
        image_layout = QVBoxLayout(image_group)
        
        self.original_label = QLabel("请选择手部X光片图像")
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
        
        # 按钮区域
        btn_layout = QHBoxLayout()
        
        self.open_btn = QPushButton("选择图像")
        self.open_btn.setStyleSheet(self._get_primary_button_style())
        self.open_btn.clicked.connect(self._on_open_image)
        btn_layout.addWidget(self.open_btn)
        
        self.detect_btn = QPushButton("开始检测")
        self.detect_btn.setStyleSheet(self._get_detect_button_style())
        self.detect_btn.clicked.connect(self._on_start_detection)
        self.detect_btn.setEnabled(False)
        btn_layout.addWidget(self.detect_btn)
        
        layout.addLayout(btn_layout)
        
        # 性别选择
        gender_group = QGroupBox("受检者性别")
        gender_group.setStyleSheet(self._get_groupbox_style())
        gender_layout = QHBoxLayout(gender_group)
        
        self.radio_boy = QRadioButton("男")
        self.radio_boy.setChecked(True)
        self.radio_boy.setStyleSheet("font-size: 14px; padding: 8px;")
        gender_layout.addWidget(self.radio_boy)
        
        self.radio_girl = QRadioButton("女")
        self.radio_girl.setStyleSheet("font-size: 14px; padding: 8px;")
        gender_layout.addWidget(self.radio_girl)
        
        gender_layout.addStretch()
        layout.addWidget(gender_group)
        
        # 进度条
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
        """创建右侧面板"""
        panel = QWidget()
        panel.setStyleSheet("background-color: #f8f9fa;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        
        # 结果图像显示
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
        
        # 检测结果摘要
        summary_group = QGroupBox("检测摘要")
        summary_group.setStyleSheet(self._get_groupbox_style())
        summary_layout = QVBoxLayout(summary_group)
        
        self.summary_label = QLabel("暂无检测结果")
        self.summary_label.setStyleSheet("""
            font-size: 16px;
            color: #333333;
            padding: 12px;
        """)
        self.summary_label.setAlignment(Qt.AlignCenter)
        summary_layout.addWidget(self.summary_label)
        
        layout.addWidget(summary_group)
        
        # 详细报告
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
        self.report_text.setText("""=== 骨龄检测系统使用说明 ===

1. 点击"选择图像"按钮上传左手X光片（掌心向下）
2. 选择受检者性别（男/女）
3. 点击"开始检测"进行骨龄分析

系统将自动检测13个关节，包括：
- 桡骨、尺骨
- 第一、三、五掌骨
- 第一、三、五近节指骨
- 第三、五中节指骨
- 第一、三、五远节指骨

使用RUS-CHN法计算骨龄，结果仅供参考。""")
        report_layout.addWidget(self.report_text)
        
        layout.addWidget(report_group)
        
        # 标注图层开关：13 个关节框与等级标签是画在位图上的，
        # 复核时需要能切回无标注底图逐节比对，所以给一个勾选框。
        self.anno_check = QCheckBox("显示关节标注（13 关节框与等级）")
        self.anno_check.setChecked(True)
        self.anno_check.setEnabled(False)
        self.anno_check.setStyleSheet("font-size: 13px; padding: 2px 4px;")
        self.anno_check.toggled.connect(self._on_toggle_annotation)
        layout.addWidget(self.anno_check)
        
        # 保存按钮
        self.save_btn = QPushButton("保存标注图片")
        self.save_btn.setObjectName("save_btn")
        self.save_btn.setStyleSheet(self._get_save_button_style())
        self.save_btn.clicked.connect(self._on_save_image)
        self.save_btn.setEnabled(False)
        self.save_btn.setMinimumHeight(45)
        layout.addWidget(self.save_btn)
        
        return panel
    
    def _display_result_image(self, bgr, annotate=True):
        """把 BGR 结果显示到结果区

        标注图与无标注底图尺寸一致，切换时按同一个缩放口径重绘，
        画面不会跳。
        """
        if bgr is None:
            return
        try:
            result_img_rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            h, w, ch = result_img_rgb.shape
            qt_image = QImage(result_img_rgb.data, w, h, ch * w,
                              QImage.Format_RGB888).copy()
            pixmap = QPixmap.fromImage(qt_image)
            scaled = pixmap.scaled(
                max(80, self.result_label.width() - 20),
                max(80, self.result_label.height() - 20),
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.result_label.setPixmap(scaled)
        except Exception as exc:          # noqa: BLE001
            logger.warning(f"结果显示失败: {exc}")
    
    def _on_toggle_annotation(self, on):
        """在带标注成品图与无标注底图之间切换"""
        if not self.current_result:
            return
        key = "result_image" if on else "raw_image"
        self._display_result_image(self.current_result.get(key), on)
        if self.current_result.get(key) is None:
            # 旧结果没有 raw_image（例如更早版本缓存），只能保持标注图
            self.anno_check.blockSignals(True)
            self.anno_check.setChecked(True)
            self.anno_check.blockSignals(False)
    

    def _get_groupbox_style(self):
        """获取分组框样式"""
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
        """获取主按钮样式"""
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
        """获取检测按钮样式"""
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
        """获取保存按钮样式"""
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
    
    def _load_model(self):
        """加载模型"""
        try:
            get_boneage_inference()
            logger.info("骨龄检测模型加载完成")
        except Exception as e:
            logger.error(f"加载骨龄模型失败: {e}")
            QMessageBox.critical(self, "错误", f"加载骨龄检测模型失败:\n{str(e)}")
    
    def _on_open_image(self):
        """打开图像"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择手部X光片",
            "",
            "图像文件 (*.png *.jpg *.jpeg *.bmp *.dcm *.dicom)"
        )
        
        if file_path:
            self.current_image_path = file_path
            
            # 显示图像
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
        """开始检测"""
        if not self.current_image_path:
            QMessageBox.warning(self, "警告", "请先选择图像")
            return
        
        # 获取性别
        sex = "boy" if self.radio_boy.isChecked() else "girl"
        
        # 禁用按钮，显示进度条
        self.open_btn.setEnabled(False)
        self.detect_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        
        # 启动检测线程
        self.detection_thread = BoneAgeDetectionThread(self.current_image_path, sex)
        self.detection_thread.finished.connect(self._on_detection_finished)
        self.detection_thread.error.connect(self._on_detection_error)
        self.detection_thread.progress.connect(self._on_detection_progress)
        self.detection_thread.start()
    
    def _on_detection_progress(self, value):
        """更新进度"""
        self.progress_bar.setValue(value)
    
    def _on_detection_finished(self, result):
        """检测完成"""
        logger.info(f"_on_detection_finished 被调用，success={result.get('success')}")
        
        # 启用按钮
        self.open_btn.setEnabled(True)
        self.detect_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        
        if not result.get("success", False):
            QMessageBox.critical(self, "检测失败", result.get("error", "未知错误"))
            return
        
        self.current_result = result
        
        # 显示结果图像（带 13 关节标注；勾选框可切回无标注底图）
        self.anno_check.blockSignals(True)
        self.anno_check.setChecked(True)
        self.anno_check.blockSignals(False)
        self.anno_check.setEnabled(result.get("raw_image") is not None)
        self._display_result_image(result.get("result_image"), True)
        
        # 更新摘要
        bone_age = result.get("bone_age", 0)
        total_score = result.get("total_score", 0)
        joint_count = result.get("joint_count", 0)
        sex_text = "男" if self.radio_boy.isChecked() else "女"
        
        summary_text = f"""<b>骨龄:</b> {bone_age} 岁<br>
<b>总得分:</b> {total_score} 分<br>
<b>检测关节数:</b> {joint_count}/13<br>
<b>性别:</b> {sex_text}"""
        self.summary_label.setText(summary_text)
        
        # 更新详细报告
        report = result.get("report", "")
        self.report_text.setText(report)
        
        # 启用保存按钮
        logger.info(f"启用保存按钮")
        self.save_btn.setEnabled(True)
        self.save_btn.setVisible(True)  # 确保按钮可见
        
        # 使用QTimer延迟弹出提示，确保按钮状态先更新
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(100, lambda: QMessageBox.information(self, "检测完成", f"骨龄检测完成！\n骨龄: {bone_age}岁"))
    
    def _on_detection_error(self, error_msg):
        """检测错误"""
        self.open_btn.setEnabled(True)
        self.detect_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        
        QMessageBox.critical(self, "检测错误", error_msg)
    
    def _on_save_image(self):
        """保存标注图片"""
        if not self.current_result:
            QMessageBox.warning(self, "警告", "没有可保存的检测结果")
            return
        
        from datetime import datetime
        
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存标注图片",
            f"骨龄标注_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            "PNG图片 (*.png);;JPEG图片 (*.jpg)"
        )
        
        if file_path:
            try:
                result_img = self.current_result.get("result_image")
                if result_img is None:
                    raise ValueError("没有结果图片数据")
                
                # 注意：imwrite_cn 与 cv2.imwrite 同口径，收的是 BGR。
                # 老代码在这里多做了一次 BGR→RGB，存出来整张偏色（红蓝互换）。
                success = imwrite_cn(file_path, result_img)
                
                if success:
                    QMessageBox.information(self, "成功", f"标注图片已保存到:\n{file_path}")
                else:
                    raise ValueError("图像保存失败")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"保存图片失败:\n{str(e)}")


if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication
    
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    dialog = BoneAgeDialog()
    dialog.show()
    
    sys.exit(app.exec_())
