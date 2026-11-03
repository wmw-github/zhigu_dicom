"""
主窗口模块

集成License验证和患者列表功能
患者列表直接显示在主窗口左侧
"""
import sys
import os
import cv2
import numpy as np
from pathlib import Path

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QSplitter,
    QStatusBar, QMenuBar, QAction, QProgressBar,
    QTableWidget, QTableWidgetItem, QGroupBox,
    QMessageBox, QScrollArea, QFrame, QApplication,
    QHeaderView, QAbstractItemView, QLineEdit, QSizePolicy,
    QTreeWidget, QTreeWidgetItem, QInputDialog
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QIcon, QFont, QColor

from core.data.preprocessor import ImagePreprocessor
from core.model import get_inference_engine  # 使用自动检测的推理引擎
from core.model.boneage_inference import get_boneage_inference
from core.data.patient_scanner import PatientScanner, PatientInfo
from core.data.body_part_detector import BodyPartDetector
from config.settings import SUPPORTED_FORMATS, GUI_CONFIG, CLASS_CONFIG
from utils.logger import get_logger
from utils.image_io import imwrite_cn
from ui.styles import get_main_style, get_patient_list_style, get_image_panel_style, get_result_panel_style
from ui.components.scanner_label import ScannerLabel

# License相关导入（使用安全多文件模式）
from license_secure import check_license_secure, get_license_status_secure

logger = get_logger(__name__)


class InferenceWorker(QThread):
    """推理工作线程 - 避免阻塞UI"""
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, img_path, preprocessor, inference_engine, settings):
        super().__init__()
        self.img_path = img_path
        self.preprocessor = preprocessor
        self.inference_engine = inference_engine
        self.settings = settings

    def run(self):
        try:
            # 加载图像
            image, metadata = self.preprocessor.load_image(self.img_path)
            # 预处理
            input_tensor, scale, pad_info = self.preprocessor.preprocess(image)
            # 推理
            self.inference_engine.conf_thresh = self.settings.get("chest_conf", 0.25)
            results = self.inference_engine.infer(input_tensor, scale, pad_info)
            self.finished.emit({"success": True, "results": results, "image": image})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit(str(e))


class FractureWorker(QThread):
    """骨折检测工作线程"""
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    
    def __init__(self, img_path, settings=None):
        super().__init__()
        self.img_path = img_path
        self.settings = settings or {}
    
    def run(self):
        try:
            from core.model.fracture_inference import get_fracture_inference
            detector = get_fracture_inference()
            if detector is None:
                raise RuntimeError("骨折检测模型未初始化")
            fracture_conf = self.settings.get("fracture_conf", 0.05)
            result = detector.detect(self.img_path, conf_threshold=fracture_conf)
            self.finished.emit(result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit(str(e))


class FootArchWorker(QThread):
    """足弓测量工作线程"""
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, img_array):
        super().__init__()
        self.img_array = img_array  # numpy array (BGR or RGB)

    def run(self):
        try:
            from core.model.foot_arch_inference import get_foot_arch_inference
            detector = get_foot_arch_inference()
            if detector is None:
                raise RuntimeError("足弓检测模型未初始化")
            result = detector.predict_from_array(self.img_array)
            self.finished.emit(result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.error.emit(str(e))


class SpineWorker(QThread):
    """脊柱检测工作线程"""
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, img_path, conf_threshold=0.25):
        super().__init__()
        self.img_path = img_path
        self.conf_threshold = conf_threshold

    def run(self):
        try:
            from core.model.spine_inference import get_spine_inference
            detector = get_spine_inference()
            if detector is None:
                raise RuntimeError("脊柱检测模型未初始化")
            result = detector.detect(self.img_path, conf_threshold=self.conf_threshold)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class MainWindow(QMainWindow):
    """
    主窗口 - 胸部X光片智能诊断系统
    
    功能：
    1. 加载医学影像 (DICOM/JPG/PNG/BMP)
    2. 执行AI诊断
    3. 显示检测结果 (分类 + 定位)
    4. 导出诊断报告
    5. 患者列表管理（集成在左侧）
    6. License授权验证
    """
    
    def __init__(self):
        super().__init__()
        
        # 初始化核心组件
        self.preprocessor = ImagePreprocessor(target_size=640)
        
        # 尝试加载模型（自动选择ONNX或PyTorch后端）
        try:
            self.inference_engine = get_inference_engine()  # 自动检测最佳后端
            self.model_loaded = True
            logger.info(f"推理引擎加载成功: {type(self.inference_engine).__name__}")
        except Exception as e:
            logger.error(f"加载模型失败: {e}")
            self.model_loaded = False
        
        # 初始化患者扫描器
        self.patient_scanner = PatientScanner()
        self.patients = []
        
        # 状态变量
        self.current_image = None
        self.current_image_path = None
        self.current_results = None
        self.current_result = None  # 用于骨龄检测结果
        self.current_metadata = None
        self.selected_detection_index = -1
        self.current_patient = None
        
        # 初始化UI
        self._init_ui()
        self._init_menu()
        # 工具栏已移除，功能合并到菜单
        self._init_statusbar()
        
        # 显示模型状态
        if not self.model_loaded:
            self.statusbar.showMessage("警告: 模型加载失败，请检查模型文件")
            QMessageBox.warning(self, "警告", "模型加载失败，请检查模型文件是否存在")
        
        # 显示授权状态
        self._update_license_status()
        
        # 加载患者列表
        self._load_patient_list()
    
    def _set_window_icon(self):
        """设置窗口图标"""
        try:
            app_root = Path(__file__).parent.parent.parent.parent
            icon_paths = [
                app_root / "assets" / "icon_32.png",
                app_root / "assets" / "icon_64.png",
                app_root / "assets" / "icon.png",
            ]
            
            for icon_path in icon_paths:
                if icon_path.exists():
                    try:
                        pixmap = QPixmap(str(icon_path))
                        if not pixmap.isNull():
                            icon = QIcon(pixmap)
                            self.setWindowIcon(icon)
                            logger.info(f"主窗口图标已设置: {icon_path}")
                            return
                    except Exception as e2:
                        logger.warning(f"加载图标异常: {icon_path}, 错误: {e2}")
            
            logger.warning("未能设置主窗口图标")
                        
        except Exception as e:
            logger.warning(f"设置窗口图标失败: {e}")
    
    def _init_ui(self):
        """初始化UI"""
        self.setWindowTitle(GUI_CONFIG["window_title"])
        self.setGeometry(50, 50, GUI_CONFIG["window_size"][0], GUI_CONFIG["window_size"][1])
        
        # 应用主窗口样式
        self.setStyleSheet(get_main_style())
        
        # 中央部件
        central = QWidget()
        self.setCentralWidget(central)
        
        # 主布局
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)
        
        # 主分割器 - 左中右三栏
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setHandleWidth(2)
        main_splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #e0e0e0;
            }
            QSplitter::handle:hover {
                background-color: #0078d4;
            }
        """)
        main_layout.addWidget(main_splitter)
        
        # 左侧面板 - 患者列表
        left_panel = self._create_patient_list_panel()
        main_splitter.addWidget(left_panel)
        
        # 中间面板 - 图像显示
        center_panel = self._create_image_panel()
        main_splitter.addWidget(center_panel)
        
        # 右侧面板 - 结果展示
        right_panel = self._create_result_panel()
        main_splitter.addWidget(right_panel)
        
        # 设置分割比例 (患者列表:图像:结果 = 280:900:420)
        main_splitter.setSizes([280, 900, 420])
    
    def _create_patient_list_panel(self):
        """创建患者列表面板（左侧）- 使用树形结构显示患者和图像"""
        panel = QWidget()
        panel.setMinimumWidth(280)
        panel.setMaximumWidth(350)
        panel.setStyleSheet("background-color: #f8f9fa;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        
        # 患者树形列表（替代原来的表格）
        self.patient_tree = QTreeWidget()
        self.patient_tree.setColumnCount(1)
        self.patient_tree.setHeaderLabel("患者列表")
        self.patient_tree.setAlternatingRowColors(True)
        self.patient_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.patient_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.patient_tree.setExpandsOnDoubleClick(False)  # 双击不展开，单击展开
        self.patient_tree.setStyleSheet("""
            QTreeWidget {
                background-color: white;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                font-size: 12px;
                outline: none;
            }
            QTreeWidget::item {
                padding: 6px 8px;
                border-bottom: 1px solid #f5f5f5;
                min-height: 28px;
                color: #333333;
                border-left: 3px solid transparent;
            }
            QTreeWidget::item:selected {
                background-color: #f0f7ff;
                color: #0078d4 !important;
                border-left: 3px solid #0078d4;
            }
            QTreeWidget::item:selected:active {
                background-color: #e6f2ff;
                color: #005a9e !important;
                border-left: 3px solid #005a9e;
            }
            QTreeWidget::item:hover {
                background-color: #fafcff;
                color: #0078d4;
            }
            QTreeWidget::item:has-children {
                font-weight: bold;
            }
            QTreeWidget::item:children {
                font-weight: normal;
                margin-left: 15px;
                background-color: #fafafa;
                color: #666666;
                border-left: 3px solid transparent;
            }
            QTreeWidget::item:children:hover {
                background-color: #f5f9ff;
                color: #0078d4;
            }
            QTreeWidget::item:children:selected {
                background-color: #f0f7ff;
                color: #0078d4 !important;
                border-left: 3px solid #0078d4;
            }
            QHeaderView::section {
                background-color: #f8f9fa;
                color: #333333;
                padding: 10px 8px;
                border: none;
                border-bottom: 2px solid #e0e0e0;
                font-weight: 600;
                font-size: 11px;
            }
        """)
        self.patient_tree.itemClicked.connect(self._on_patient_tree_clicked)
        self.patient_tree.itemExpanded.connect(self._on_patient_tree_expanded)
        self.patient_tree.itemCollapsed.connect(self._on_patient_tree_collapsed)
        
        layout.addWidget(self.patient_tree)
        
        # 操作按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        
        self.select_folder_btn = QPushButton("选择文件夹")
        self.select_folder_btn.setToolTip("选择患者数据所在的文件夹")
        self.select_folder_btn.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: 600;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #218838;
            }
        """)
        self.select_folder_btn.clicked.connect(self._on_select_patient_folder)
        btn_layout.addWidget(self.select_folder_btn)
        
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setToolTip("重新扫描患者目录")
        self.refresh_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: 600;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
        """)
        self.refresh_btn.clicked.connect(self._load_patient_list)
        btn_layout.addWidget(self.refresh_btn)
        
        self.clear_patients_btn = QPushButton("清空")
        self.clear_patients_btn.setToolTip("清空患者列表（不删除原文件）")
        self.clear_patients_btn.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-weight: 600;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
        """)
        self.clear_patients_btn.clicked.connect(self._clear_patient_list)
        btn_layout.addWidget(self.clear_patients_btn)
        
        layout.addLayout(btn_layout)
        
        # 统计信息
        self.patient_stats_label = QLabel("共 0 个患者")
        self.patient_stats_label.setStyleSheet("color: #999999; font-size: 11px;")
        self.patient_stats_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.patient_stats_label)
        
        return panel
    
    def _create_image_panel(self):
        """创建图像显示面板（中间）"""
        panel = QWidget()
        panel.setStyleSheet("background-color: #f5f6f7;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        
        # 患者信息显示区域（优化布局）
        self.patient_info_group = QGroupBox("患者信息")
        self.patient_info_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.patient_info_group.setMinimumHeight(110)
        self.patient_info_group.setStyleSheet("""
            QGroupBox {
                background-color: white;
                border: 1px solid #d0d0d0;
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 20px;
                font-weight: bold;
                font-size: 12px;
                color: #0078d4;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                font-size: 11px;
            }
        """)
        
        patient_info_layout = QVBoxLayout()
        patient_info_layout.setSpacing(4)
        patient_info_layout.setContentsMargins(14, 18, 14, 10)
        
        self.patient_info_label = QLabel("未选择患者")
        self.patient_info_label.setStyleSheet("""
            padding: 2px; 
            color: #333333; 
            font-size: 12px;
            line-height: 1.5;
        """)
        self.patient_info_label.setWordWrap(True)
        self.patient_info_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        patient_info_layout.addWidget(self.patient_info_label)
        
        self.patient_info_group.setLayout(patient_info_layout)
        self.patient_info_group.setVisible(False)
        layout.addWidget(self.patient_info_group)
        
        # 图像显示区域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setStyleSheet("background-color: #2b2b2b; border-radius: 8px;")
        
        # 使用带扫描特效的图片显示组件
        self.image_label = ScannerLabel()
        self.image_label.setText("请从左侧患者列表选择患者\n或点击\"打开图像\"按钮")
        self.image_label.setMinimumSize(GUI_CONFIG["image_min_size"][0],
                                        GUI_CONFIG["image_min_size"][1])
        
        scroll_area.setWidget(self.image_label)
        layout.addWidget(scroll_area)
        
        # 按钮组
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        
        self.open_btn = QPushButton("打开图像")
        self.open_btn.setToolTip("打开医学影像文件 (Ctrl+O)")
        self.open_btn.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                color: #333333;
                border: 1px solid #d0d0d0;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: 500;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #e5e5e5;
                border-color: #0078d4;
            }
        """)
        self.open_btn.clicked.connect(self._on_open_image)
        btn_layout.addWidget(self.open_btn)
        
        self.diagnose_btn = QPushButton("胸片检测")
        self.diagnose_btn.setToolTip("执行胸部X光AI诊断")
        self.diagnose_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: 600;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """)
        self.diagnose_btn.clicked.connect(self._on_diagnose)
        self.diagnose_btn.setEnabled(False)
        btn_layout.addWidget(self.diagnose_btn)

        self.foot_arch_btn = QPushButton("足弓测量")
        self.foot_arch_btn.setToolTip("自动测量内侧和外侧足弓角度")
        self.foot_arch_btn.setStyleSheet("""
            QPushButton {
                background-color: #2e7d32;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: 600;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #1b5e20;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """)
        self.foot_arch_btn.clicked.connect(self._on_foot_arch_measure)
        self.foot_arch_btn.setEnabled(False)
        btn_layout.addWidget(self.foot_arch_btn)

        self.boneage_btn = QPushButton("骨龄检测")
        self.boneage_btn.setToolTip("打开骨龄检测系统")
        self.boneage_btn.setStyleSheet("""
            QPushButton {
                background-color: #ff9800;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: 600;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #e68900;
            }
        """)
        self.boneage_btn.clicked.connect(self._on_boneage)
        btn_layout.addWidget(self.boneage_btn)

        self.fracture_btn = QPushButton("骨折检测")
        self.fracture_btn.setToolTip("执行骨折自动检测")
        self.fracture_btn.setStyleSheet("""
            QPushButton {
                background-color: #d32f2f;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: 600;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #b71c1c;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """)
        self.fracture_btn.clicked.connect(self._on_fracture_detect)
        self.fracture_btn.setEnabled(False)
        btn_layout.addWidget(self.fracture_btn)

        self.spine_btn = QPushButton("脊柱检测")
        self.spine_btn.setToolTip("执行脊柱侧弯检测与Cobb角测量")
        self.spine_btn.setStyleSheet("""
            QPushButton {
                background-color: #7b68ee;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: 600;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #6a5acd;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """)
        self.spine_btn.clicked.connect(self._on_spine_detect)
        self.spine_btn.setEnabled(False)
        btn_layout.addWidget(self.spine_btn)

        self.show_all_btn = QPushButton("显示全部标注")
        self.show_all_btn.setToolTip("显示所有检测到的病变标注")
        self.show_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                color: #333333;
                border: 1px solid #d0d0d0;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: 500;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #e5e5e5;
                border-color: #0078d4;
            }
            QPushButton:disabled {
                background-color: #f5f5f5;
                color: #999999;
            }
        """)
        self.show_all_btn.clicked.connect(self._on_show_all_detections)
        self.show_all_btn.setEnabled(False)
        btn_layout.addWidget(self.show_all_btn)
        
        self.export_btn = QPushButton("导出图片")
        self.export_btn.setToolTip("导出检测图片 (Ctrl+E)")
        self.export_btn.setStyleSheet("""
            QPushButton {
                background-color: #107c10;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: 500;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #0e6e0e;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """)
        self.export_btn.clicked.connect(self._on_export)
        self.export_btn.setEnabled(False)
        btn_layout.addWidget(self.export_btn)
        
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        return panel
    
    def _create_result_panel(self):
        """创建结果展示面板（右侧）"""
        panel = QWidget()
        panel.setStyleSheet("background-color: #f8f9fa;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        
        # 检测结果表格
        result_group = QGroupBox("检测结果")
        result_group.setStyleSheet("""
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
        """)
        result_layout = QVBoxLayout(result_group)
        
        self.result_table = QTableWidget()
        self.result_table.setColumnCount(5)
        self.result_table.setHorizontalHeaderLabels(["序号", "疾病", "中文", "置信度", "位置"])
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setStyleSheet("""
            QTableWidget {
                background-color: white;
                border: none;
                border-radius: 4px;
                font-size: 12px;
                gridline-color: #f0f0f0;
            }
            QTableWidget::item {
                padding: 8px;
                border-bottom: 1px solid #f0f0f0;
            }
            QTableWidget::item:selected {
                background-color: #0078d4;
                color: white;
            }
            QTableWidget::item:hover {
                background-color: #f0f7ff;
            }
            QHeaderView::section {
                background-color: #f8f9fa;
                color: #333333;
                padding: 8px;
                border: none;
                border-bottom: 2px solid #e0e0e0;
                font-weight: 600;
                font-size: 11px;
            }
        """)
        self.result_table.itemClicked.connect(self._on_result_clicked)
        self.result_table.itemSelectionChanged.connect(self._on_selection_changed)
        result_layout.addWidget(self.result_table)
        
        layout.addWidget(result_group)
        
        # 影像诊断
        summary_group = QGroupBox("影像诊断")
        summary_group.setStyleSheet("""
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
        """)
        summary_layout = QVBoxLayout(summary_group)
        
        summary_top_layout = QHBoxLayout()
        
        self.summary_label = QLabel("暂无诊断结果")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("""
            padding: 12px;
            font-size: 13px;
            line-height: 1.6;
            color: #333333;
        """)
        summary_top_layout.addWidget(self.summary_label, stretch=1)
        
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setObjectName("copy_btn")
        self.copy_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 16px;
                font-weight: 600;
                font-size: 12px;
                max-width: 80px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """)
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._on_copy_summary)
        summary_top_layout.addWidget(self.copy_btn)
        
        summary_layout.addLayout(summary_top_layout)
        
        layout.addWidget(summary_group)
        layout.addStretch()
        
        return panel
    
    def _init_menu(self):
        """初始化菜单栏"""
        menubar = self.menuBar()
        
        # 文件菜单
        file_menu = menubar.addMenu("文件")
        
        open_action = QAction("打开图像", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_image)
        file_menu.addAction(open_action)
        
        file_menu.addSeparator()
        
        export_action = QAction("导出图片", self)
        export_action.setShortcut("Ctrl+E")
        export_action.triggered.connect(self._on_export)
        file_menu.addAction(export_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # 视图菜单
        view_menu = menubar.addMenu("视图")
        
        refresh_patients_action = QAction("刷新患者列表", self)
        refresh_patients_action.setShortcut("F5")
        refresh_patients_action.triggered.connect(self._load_patient_list)
        view_menu.addAction(refresh_patients_action)
        
        view_menu.addSeparator()
        
        show_all_action = QAction("显示全部标注", self)
        show_all_action.triggered.connect(self._on_show_all_detections)
        view_menu.addAction(show_all_action)
        
        # 工具菜单
        tools_menu = menubar.addMenu("工具")

        annotation_action = QAction("足弓关键点标注工具", self)
        annotation_action.triggered.connect(self._on_open_annotation_tool)
        tools_menu.addAction(annotation_action)

        tools_menu.addSeparator()

        settings_action = QAction("检测设置", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._on_settings)
        tools_menu.addAction(settings_action)

        tools_menu.addSeparator()

        license_action = QAction("软件授权", self)
        license_action.triggered.connect(self._on_license)
        tools_menu.addAction(license_action)
        
        # 记录菜单
        records_menu = menubar.addMenu("记录")
        
        view_records_action = QAction("查看检测记录", self)
        view_records_action.setShortcut("Ctrl+R")
        view_records_action.triggered.connect(self._on_view_detection_records)
        records_menu.addAction(view_records_action)
        
        # 帮助菜单
        help_menu = menubar.addMenu("帮助")
        
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)
    
    def _init_statusbar(self):
        """初始化状态栏"""
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("就绪")
        
        # 授权状态标签
        self.license_status_label = QLabel()
        self.license_status_label.setStyleSheet("padding: 0 10px;")
        self.statusbar.addPermanentWidget(self.license_status_label)
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setVisible(False)
        self.statusbar.addPermanentWidget(self.progress_bar)
    
    def _update_license_status(self):
        """更新授权状态显示"""
        try:
            status = get_license_status_secure()

            if status["is_valid"]:
                license_type = status.get("type", "")

                if license_type == "unlimited" or status["remaining_hours"] >= 999999:
                    self.license_status_label.setText("授权: 永久")
                    self.license_status_label.setStyleSheet("color: green; padding: 0 10px;")
                else:
                    remaining = int(status["remaining_hours"])
                    if remaining > 24:
                        days = remaining // 24
                        self.license_status_label.setText(f"授权: {days}天")
                    else:
                        self.license_status_label.setText(f"授权: {remaining}小时")
                    self.license_status_label.setStyleSheet("color: orange; padding: 0 10px;")
            else:
                self.license_status_label.setText("授权: 已过期")
                self.license_status_label.setStyleSheet("color: red; padding: 0 10px;")

        except Exception as e:
            logger.error(f"更新授权状态失败: {e}")
            self.license_status_label.setText("授权: 未知")
    
    def _on_select_patient_folder(self):
        """选择患者数据文件夹"""
        folder_path = QFileDialog.getExistingDirectory(
            self, 
            "选择患者数据目录",
            "",
            QFileDialog.ShowDirsOnly
        )
        
        if folder_path:
            try:
                # 更新PatientScanner的扫描路径
                self.patient_scanner.set_scan_directory(folder_path)
                
                # 显示当前选择的路径
                self.select_folder_btn.setToolTip(f"当前目录: {folder_path}")
                
                # 自动加载该目录的患者列表
                self._load_patient_list()
                
                self.statusbar.showMessage(f"已选择患者目录: {folder_path}")
                
            except Exception as e:
                logger.error(f"选择文件夹失败: {e}")
                QMessageBox.critical(self, "错误", f"选择文件夹失败:\n{str(e)}")
    
    def _load_patient_list(self):
        """加载患者列表"""
        try:
            self.statusbar.showMessage("正在扫描患者列表...")
            self.patients = self.patient_scanner.scan()
            self._update_patient_table(self.patients)
            self.patient_stats_label.setText(f"共 {len(self.patients)} 个患者")
            self.statusbar.showMessage(f"患者列表加载完成，共 {len(self.patients)} 个患者")
        except Exception as e:
            logger.error(f"加载患者列表失败: {e}")
            self.statusbar.showMessage("加载患者列表失败")
    
    def _update_patient_table(self, patients):
        """更新患者树形列表，按来源目录分组"""
        self.patient_tree.clear()
        
        # 按来源目录分组
        groups = {}
        for patient in patients:
            source = patient.source_dir or "未知来源"
            if source not in groups:
                groups[source] = []
            groups[source].append(patient)
        
        # 定义目录友好名称
        dir_names = {
            r"D:\ClearTmp\DicomTmp": "本地缓存 (D:\\ClearTmp\\DicomTmp)",
            r"D:\Cache": "设备缓存 (D:\\Cache)",
            r"E:\ClearTmp\DicomTmp": "本地缓存 (E:\\ClearTmp\\DicomTmp)",
            r"E:\Cache": "设备缓存 (E:\\Cache)",
        }
        
        # 遍历每个分组
        for source_dir, group_patients in groups.items():
            # 创建目录分组项
            dir_name = dir_names.get(source_dir, source_dir)
            group_item = QTreeWidgetItem(self.patient_tree)
            group_item.setText(0, f"📁 {dir_name} ({len(group_patients)}个患者)")
            
            # 设置分组项样式
            font = group_item.font(0)
            font.setBold(True)
            font.setPointSize(10)
            group_item.setFont(0, font)
            group_item.setForeground(0, QColor("#0078d7"))
            group_item.setBackground(0, QColor("#e8f4fd"))
            
            # 展开分组项
            group_item.setExpanded(True)
            
            # 添加该分组下的患者
            for patient in group_patients:
                # 创建患者项
                patient_item = QTreeWidgetItem(group_item)
                
                # 格式化显示文本：姓名 | 检查号 | 日期 | 图像数
                date_str = patient.study_date
                if len(date_str) == 8:
                    date_str = f"{date_str[4:6]}-{date_str[6:]}"
                
                display_text = f"{patient.name}  |  {patient.access_no}  |  {date_str}  |  📷{len(patient.dicom_files)}张"
                patient_item.setText(0, display_text)
                patient_item.setData(0, Qt.UserRole, patient)  # 保存患者对象
                
                # 设置字体加粗
                font = patient_item.font(0)
                font.setBold(True)
                patient_item.setFont(0, font)
                
                # 添加图像子项（如果有多个图像）
                if len(patient.dicom_files) > 1:
                    for i, dicom_file in enumerate(patient.dicom_files):
                        file_name = Path(dicom_file).name
                        image_item = QTreeWidgetItem(patient_item)
                        
                        # 显示文件名和大小
                        try:
                            file_size = os.path.getsize(dicom_file)
                            if file_size < 1024 * 1024:
                                size_str = f"{file_size/1024:.1f}KB"
                            else:
                                size_str = f"{file_size/(1024*1024):.1f}MB"
                            item_text = f"  📄 {i+1}. {file_name} ({size_str})"
                        except:
                            item_text = f"  📄 {i+1}. {file_name}"
                        
                        image_item.setText(0, item_text)
                        image_item.setData(0, Qt.UserRole, dicom_file)  # 保存文件路径
                        
                        # 设置图标（使用不同颜色区分）
                        image_item.setForeground(0, QColor("#666666"))
                    
                    # 默认展开有多个图像的患者
                    patient_item.setExpanded(False)
        
        # 调整列宽
        self.patient_tree.resizeColumnToContents(0)
    
    def _on_patient_search(self, text):
        """搜索患者"""
        if not text:
            self._update_patient_table(self.patients)
            return
        
        text = text.lower()
        filtered = []
        
        for patient in self.patients:
            if (text in patient.name.lower() or
                text in patient.patient_id.lower() or
                text in patient.access_no.lower()):
                filtered.append(patient)
        
        self._update_patient_table(filtered)
        self.patient_stats_label.setText(f"显示 {len(filtered)} / {len(self.patients)} 个患者")
    
    def _on_patient_tree_clicked(self, item, column):
        """患者树形列表点击事件"""
        # 获取点击项的数据
        data = item.data(0, Qt.UserRole)
        
        # 判断是分组项、患者项还是图像子项
        if data is None:
            # 点击的是分组标题项（无数据）
            return
        elif isinstance(data, PatientInfo):
            # 点击的是患者项
            self._select_patient(data)
        elif isinstance(data, str) and os.path.exists(data):
            # 点击的是图像子项
            self._load_image(data)
            file_name = Path(data).name
            self.statusbar.showMessage(f"已加载: {file_name}")
    
    def _on_patient_tree_expanded(self, item):
        """患者树形展开事件"""
        pass  # 可用于日志记录或统计
    
    def _on_patient_tree_collapsed(self, item):
        """患者树形折叠事件"""
        pass  # 可用于日志记录或统计
    
    def _select_patient(self, patient: PatientInfo):
        """选择患者"""
        self.current_patient = patient
        
        # 更新患者信息显示
        self._update_patient_info_display()
        
        # 加载第一个DICOM文件
        if patient.dicom_files:
            self._load_image(patient.dicom_files[0])
            self.statusbar.showMessage(f"已加载患者: {patient.name} ({patient.access_no})")
        else:
            QMessageBox.warning(self, "警告", "该患者没有可用的DICOM图像文件")
    
    def _clear_patient_list(self):
        """清空患者列表（不删除原文件）"""
        reply = QMessageBox.question(
            self, "确认清空",
            "确定要清空当前患者列表吗？\n\n注意：这只会清空内存中的列表，不会删除任何原始文件。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.patients = []
            self._update_patient_table([])
            self.patient_stats_label.setText("共 0 个患者")
            self.current_patient = None
            self.current_image_path = None
            self.current_results = None
            self.current_result = None
            
            # 清空显示区域
            self.patient_info_group.setVisible(False)
            
            # 重置图像显示
            self.image_label.setText("请从左侧患者列表选择患者\n或点击\"打开图像\"按钮")
            self.result_table.setRowCount(0)
            self.summary_label.setText("暂无诊断结果")
            self.copy_btn.setEnabled(False)
            
            self.statusbar.showMessage("患者列表已清空")
    
    def _update_patient_info_display(self):
        """更新患者信息显示（双栏布局）"""
        if not self.current_patient:
            self.patient_info_group.setVisible(False)
            return
        
        patient = self.current_patient
        
        # 格式化检查时间
        study_time = patient.study_datetime or '-'
        if len(str(study_time)) > 16:
            study_time = str(study_time)[:16]
        
        # 使用HTML表格实现双栏布局
        info_text = f"""<div style='font-size: 12px; line-height: 1.8;'>
<table style='width: 100%; border-collapse: collapse;'>
<tr>
<td style='width: 50%; padding: 2px 8px; vertical-align: top;'>
<b style='color: #0078d4;'>姓名:</b> <span style='color: #333; font-weight: 600;'>{patient.name}</span><br>
<b style='color: #666;'>检查号:</b> <span style='color: #333;'>{patient.access_no}</span><br>
<b style='color: #666;'>性别:</b> <span style='color: #333;'>{patient.sex}</span>
</td>
<td style='width: 50%; padding: 2px 8px; vertical-align: top;'>
<b style='color: #666;'>年龄:</b> <span style='color: #333;'>{patient.age}</span><br>
<b style='color: #666;'>检查时间:</b> <span style='color: #333;'>{study_time}</span><br>
<b style='color: #666;'>图像数:</b> <span style='color: #333;'>{len(patient.dicom_files)} 张</span>
</td>
</tr>
</table>
</div>"""
        
        self.patient_info_label.setText(info_text)
        self.patient_info_group.setVisible(True)
        self.patient_info_group.setMinimumHeight(90)
    
    def _on_open_image(self):
        """打开图像"""
        filters = "医学影像 (*.jpg *.jpeg *.png *.bmp *.dcm *.dicom);;所有文件 (*.*)"
        file_path, _ = QFileDialog.getOpenFileName(
            self, "打开图像", "", filters
        )

        if file_path:
            self.current_patient = None
            self.patient_info_group.setVisible(False)
            self.patient_tree.clearSelection()
            self._load_image(file_path)
    
    def _load_image(self, file_path: str):
        """加载图像"""
        try:
            logger.info(f"加载图像: {file_path}")
            
            # 加载图像
            image, metadata = self.preprocessor.load_image(file_path)
            self.current_image = image
            self.current_metadata = metadata
            self.current_image_path = file_path
            
            # 清空之前的结果
            self.current_results = None
            self.selected_detection_index = -1
            self.result_table.setRowCount(0)
            self.summary_label.setText("暂无诊断结果")
            self.copy_btn.setEnabled(False)
            
            # 显示图像
            self._display_image(image, detections=None, highlight_index=-1)
            
            # 启用诊断按钮
            self.diagnose_btn.setEnabled(self.model_loaded)
            self.fracture_btn.setEnabled(True)
            self.spine_btn.setEnabled(True)
            self.foot_arch_btn.setEnabled(True)
            self.export_btn.setEnabled(False)
            self.show_all_btn.setEnabled(False)
            
            # 更新状态栏
            meta_info = ""
            if self.current_patient:
                meta_info = f" | 患者: {self.current_patient.name}"
            elif metadata:
                meta_info = f" | 患者: {metadata.get('patient_id', 'Unknown')}"
            self.statusbar.showMessage(f"已加载: {Path(file_path).name}{meta_info}")
            
            # 自动判断部位并触发检测
            self._auto_detect_and_run(file_path)
            
        except Exception as e:
            logger.error(f"加载图像失败: {e}")
            QMessageBox.critical(self, "错误", f"加载图像失败:\n{str(e)}")
    
    def _auto_detect_and_run(self, file_path: str):
        """自动判断部位并触发相应的检测"""
        # 只处理DICOM文件
        if not file_path.lower().endswith(('.dcm', '.dicom')):
            logger.info("非DICOM文件，跳过自动检测")
            return
        
        try:
            # 自动判断检测类型
            detection_type = BodyPartDetector.detect_from_dicom(file_path)
            
            if detection_type is None:
                logger.info("无法判断部位，不自动检测")
                self.statusbar.showMessage("无法判断部位，请手动选择检测类型", 3000)
                return
            
            detection_name = BodyPartDetector.get_detection_name(detection_type)
            logger.info(f"识别部位: {detection_name}")
            
            # 根据检测类型触发相应的检测
            if detection_type == BodyPartDetector.DETECTION_CHEST:
                # 胸片检测 - 自动触发
                if self.model_loaded:
                    self.statusbar.showMessage(f"自动识别为{detection_name}，正在执行检测...", 3000)
                    # 延迟一点执行，确保UI已更新
                    from PyQt5.QtCore import QTimer
                    QTimer.singleShot(500, self._on_diagnose)
                else:
                    self.statusbar.showMessage("胸片检测模型未加载", 3000)
            
            elif detection_type == BodyPartDetector.DETECTION_FRACTURE:
                # 骨折检测 - 自动触发
                self.statusbar.showMessage(f"自动识别为{detection_name}，正在执行检测...", 3000)
                # 延迟一点执行，确保UI已更新
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(500, self._on_fracture_detect)
            
            elif detection_type == BodyPartDetector.DETECTION_SPINE:
                # 脊柱检测 - 不自动检测，提示用户手动点击
                self.statusbar.showMessage(f"识别为脊柱部位，如需检测请点击【脊柱检测】按钮", 5000)
            
            elif detection_type == BodyPartDetector.DETECTION_BONEAGE:
                # 骨龄检测 - 不自动检测，提示用户手动点击
                self.statusbar.showMessage(f"识别为手腕部位，如需骨龄检测请点击【骨龄检测】按钮", 5000)
            
        except Exception as e:
            logger.error(f"自动检测失败: {e}")
            # 不显示错误对话框，只记录日志，避免影响用户体验
    
    def _display_image(self, image: np.ndarray, detections: list = None, highlight_index: int = -1):
        """显示图像"""
        display_img = image.copy()
        
        # 绘制检测框
        if detections:
            if highlight_index >= 0 and highlight_index < len(detections):
                det = detections[highlight_index]
                self._draw_detection(display_img, det, thickness=3, draw_label=True)
            else:
                for det in detections:
                    self._draw_detection(display_img, det, thickness=2, draw_label=True)
        
        # 转换为QPixmap
        h, w, ch = display_img.shape
        bytes_per_line = ch * w
        qt_image = QImage(display_img.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qt_image)
        
        # 缩放显示
        label_size = self.image_label.size()
        # 如果 label_size 无效，使用默认尺寸
        if label_size.width() <= 0 or label_size.height() <= 0:
            label_size = self.image_label.minimumSize()
        if label_size.width() <= 0 or label_size.height() <= 0:
            label_size = self.image_label.parent().size() if self.image_label.parent() else self.size()
        if label_size.width() > 0 and label_size.height() > 0:
            scaled = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)
        else:
            # 如果仍然无法获取有效尺寸，直接显示原图
            self.image_label.setPixmap(pixmap)
    
    def _draw_detection(self, image: np.ndarray, det: dict, thickness: int = 2, draw_label: bool = True):
        """绘制检测框"""
        try:
            x1, y1, x2, y2 = det["bbox"]
            class_id = det["class_id"]
            
            # 获取颜色
            color = self.inference_engine.colors[class_id]
            if hasattr(color, 'tolist'):
                color = color.tolist()
            # 确保颜色是整数元组
            color = tuple(int(c) for c in color)
            
            # 绘制边界框
            cv2.rectangle(image, (x1, y1), (x2, y2), color, thickness)
        
            if draw_label:
                label = f"{det['class_name']}: {det['confidence']:.2f}"
                (text_width, text_height), _ = cv2.getTextSize(
                    label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, thickness
                )
                
                label_y = max(y1 - 10, text_height + 5)
                cv2.rectangle(
                    image,
                    (x1, label_y - text_height - 5),
                    (x1 + text_width, label_y + 5),
                    color,
                    -1
                )
                
                cv2.putText(
                    image,
                    label,
                    (x1, label_y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (255, 255, 255),
                    thickness
                )
        except Exception as e:
            logger.error(f"绘制检测框失败: {e}")
    
    def _on_diagnose(self):
        """开始诊断 - 使用后台线程避免阻塞UI"""
        if self.current_image_path is None or not self.model_loaded:
            QMessageBox.warning(self, "提示", "请先打开图像")
            return

        self.statusbar.showMessage("正在诊断...")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.diagnose_btn.setEnabled(False)

        # 启动扫描特效
        if hasattr(self.image_label, 'start_scan'):
            self.image_label.start_scan(duration_ms=2000)

        # 使用后台线程执行推理
        settings = self._get_detection_settings()
        self.inference_worker = InferenceWorker(
            self.current_image_path,
            self.preprocessor,
            self.inference_engine,
            settings
        )
        self.inference_worker.finished.connect(self._on_diagnose_finished)
        self.inference_worker.error.connect(self._on_diagnose_error)
        self.inference_worker.start()

    def _on_diagnose_finished(self, result):
        """胸片诊断完成"""
        self.progress_bar.setVisible(False)
        self.diagnose_btn.setEnabled(True)

        if not result.get('success', False):
            if hasattr(self.image_label, 'stop_scan_smooth'):
                self.image_label.stop_scan_smooth()
            QMessageBox.critical(self, "错误", result.get('error', '未知错误'))
            self.statusbar.showMessage("诊断失败")
            return

        results = result.get('results', [])
        image = result.get('image')

        results = self._filter_pediatric_cardiomegaly(results)

        self.current_results = results
        self.selected_detection_index = -1

        if image is not None:
            self.current_image = image

        self._display_image(self.current_image, results, -1)
        self._update_result_table(results)

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()

        self.export_btn.setEnabled(True)
        self.show_all_btn.setEnabled(True)
        self.statusbar.showMessage(f"诊断完成，发现 {len(results)} 处异常")

        self._save_detection_record('chest_xray', results, self.summary_label.text())

    def _filter_pediatric_cardiomegaly(self, results: list) -> list:
        """
        过滤儿童胸片的心脏肥大误检

        由于训练数据是成人的，儿童心脏相对比例本来就比成人大，
        容易导致误检。如果是儿童(<14岁)，则过滤掉心脏肥大检测结果。

        Args:
            results: 原始检测结果列表

        Returns:
            过滤后的结果列表
        """
        # 检查是否有心脏肥大检测结果
        has_cardiomegaly = any(
            det.get('class_id') == 3 or det.get('class_name') == 'Cardiomegaly' or det.get('class_name_cn') == '心脏肥大'
            for det in results
        )

        # 如果没有心脏肥大结果，无需处理
        if not has_cardiomegaly:
            return results

        # 获取患者年龄
        age_str = ""
        if self.current_patient:
            age_str = getattr(self.current_patient, 'age', '')

        # 如果没有年龄信息，询问用户
        if not age_str:
            age_str = self._ask_patient_age_for_chest_xray()
            if age_str and self.current_patient:
                self.current_patient.age = age_str

        if not age_str:
            # 用户未提供年龄，不过滤
            return results

        # 解析年龄（支持格式："10", "10Y", "10岁"等）
        try:
            # 提取数字部分
            age_num = ''
            for char in str(age_str):
                if char.isdigit():
                    age_num += char
                else:
                    break

            if not age_num:
                return results

            age = int(age_num)

            # 如果是儿童(<14岁)，过滤掉心脏肥大(Cardio类ID为3)
            if age < 14:
                filtered_results = []
                removed_count = 0
                for det in results:
                    if det.get('class_id') == 3 or det.get('class_name') == 'Cardiomegaly' or det.get('class_name_cn') == '心脏肥大':
                        removed_count += 1
                        logger.info(f"儿童胸片过滤心脏肥大检测结果: {det}")
                        continue
                    filtered_results.append(det)

                if removed_count > 0:
                    logger.info(f"儿童患者(年龄{age}岁)，已过滤{removed_count}个心脏肥大误检")
                    self.statusbar.showMessage(f"儿童患者({age}岁)，已过滤心脏肥大误检", 5000)

                return filtered_results

        except (ValueError, TypeError) as e:
            logger.warning(f"解析患者年龄失败: {age_str}, 错误: {e}")

        return results

    def _ask_patient_age_for_chest_xray(self) -> str:
        """
        为胸片检测询问患者年龄

        当检测到心脏肥大但没有患者年龄信息时调用，
        用于判断是否为儿童，避免心脏肥大误检。

        Returns:
            年龄字符串，如果用户取消则返回空字符串
        """
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("患者年龄确认")
        msg_box.setText("检测到心脏肥大")
        msg_box.setInformativeText(
            "系统检测到心脏肥大，但由于训练数据基于成人胸片，\n"
            "儿童胸片可能导致误判。\n\n"
            "请确认患者年龄类型以优化检测结果。"
        )

        # 添加自定义按钮
        child_btn = msg_box.addButton("儿童 (<14岁)", QMessageBox.ActionRole)
        adult_btn = msg_box.addButton("成人 (≥14岁)", QMessageBox.ActionRole)
        unknown_btn = msg_box.addButton("不确定", QMessageBox.ActionRole)

        msg_box.setDefaultButton(adult_btn)
        msg_box.setIcon(QMessageBox.Question)

        msg_box.exec_()

        clicked_btn = msg_box.clickedButton()

        if clicked_btn == child_btn:
            logger.info("用户确认患者为儿童(<14岁)")
            return "10"  # 使用典型儿童年龄
        elif clicked_btn == adult_btn:
            logger.info("用户确认患者为成人(≥14岁)")
            return "30"  # 使用典型成人年龄
        else:
            # 不确定/跳过
            logger.info("用户未确认患者年龄")
            return ""

    def _on_diagnose_error(self, error_msg):
        """胸片诊断错误"""
        self.progress_bar.setVisible(False)
        self.diagnose_btn.setEnabled(True)
        self.statusbar.showMessage("诊断失败")

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()

        logger.error(f"诊断失败: {error_msg}")
        QMessageBox.critical(self, "错误", f"诊断失败:\n{error_msg}")
    
    def _update_result_table(self, results: list):
        """更新结果表格"""
        self.result_table.setRowCount(len(results))
        
        for i, det in enumerate(results):
            # 序号
            num_item = QTableWidgetItem(str(i + 1))
            num_item.setTextAlignment(Qt.AlignCenter)
            self.result_table.setItem(i, 0, num_item)
            
            # 疾病名称 (英文)
            self.result_table.setItem(i, 1, QTableWidgetItem(det["class_name"]))
            
            # 疾病名称 (中文)
            self.result_table.setItem(i, 2, QTableWidgetItem(det["class_name_cn"]))
            
            # 置信度
            conf_item = QTableWidgetItem(f"{det['confidence']:.3f}")
            conf_item.setTextAlignment(Qt.AlignCenter)
            self.result_table.setItem(i, 3, conf_item)
            
            # 位置
            bbox = det["bbox"]
            pos_str = f"({bbox[0]}, {bbox[1]}) - ({bbox[2]}, {bbox[3]})"
            self.result_table.setItem(i, 4, QTableWidgetItem(pos_str))
        
        # 调整列宽
        self.result_table.resizeColumnsToContents()
        
        # 更新摘要
        diseases = {}
        for det in results:
            name = det["class_name_cn"]
            conf = det["confidence"]
            if name not in diseases or diseases[name] < conf:
                diseases[name] = conf
        
        if diseases:
            summary = f"<b>检测到 {len(results)} 处异常</b><br><br>"
            summary += "涉及疾病:<br>"
            for name, conf in sorted(diseases.items(), key=lambda x: x[1], reverse=True):
                summary += f"• {name}: {conf:.1%}<br>"
        else:
            summary = "<b>未发现明显异常</b>"
        
        self.summary_label.setText(summary)
        self.copy_btn.setEnabled(True)
    
    def _on_result_clicked(self, item):
        """点击结果项"""
        row = item.row()
        if self.current_results and row < len(self.current_results):
            self.selected_detection_index = row
            self._display_image(self.current_image, self.current_results, row)
            self.statusbar.showMessage(f"显示第 {row + 1} 个病变: {self.current_results[row]['class_name_cn']}")
    
    def _on_selection_changed(self):
        """选择改变时的处理"""
        selected_items = self.result_table.selectedItems()
        if not selected_items:
            self.selected_detection_index = -1
            if self.current_results:
                self._display_image(self.current_image, self.current_results, -1)
                self.statusbar.showMessage("显示全部标注")
    
    def _on_show_all_detections(self):
        """显示全部标注"""
        if self.current_results:
            self.selected_detection_index = -1
            self.result_table.clearSelection()
            self._display_image(self.current_image, self.current_results, -1)
            self.statusbar.showMessage("显示全部标注")
    
    def _on_export(self):
        """导出报告/图片"""
        # 检查是否有骨龄检测结果
        if self.current_result and 'bone_age' in self.current_result:
            # 导出骨龄标注图片
            self._export_boneage_image()
        elif self.current_result and 'fracture_count' in self.current_result:
            # 导出骨折检测图片
            self._export_fracture_image()
        elif self.current_result and 'cobb_angle' in self.current_result:
            # 导出脊柱检测图片
            self._export_spine_image()
        elif self.current_result and 'keypoints' in self.current_result:
            # 导出足弓测量图片
            self._export_foot_arch_image()
        elif self.current_results:
            # 导出胸片检测报告
            self._export_diagnosis_report()
        else:
            QMessageBox.information(self, "提示", "请先进行诊断或检测")
            return
    
    def _export_fracture_image(self):
        """导出骨折检测图片"""
        result = self.current_result
        result_img = result.get("result_image")
        if result_img is None:
            QMessageBox.warning(self, "警告", "没有可导出的检测图片")
            return
        
        default_name = "fracture_detection.png"
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出骨折检测图片", default_name,
            "PNG图片 (*.png);;JPEG图片 (*.jpg);;所有文件 (*.*)"
        )
        
        if not file_path:
            return
        
        try:
            success = imwrite_cn(file_path, result_img)
            
            if success:
                self.statusbar.showMessage(f"骨折检测图片已保存: {file_path}")
                QMessageBox.information(self, "成功", "骨折检测图片已导出")
            else:
                raise ValueError("图像保存失败")
                
        except Exception as e:
            logger.error(f"导出失败: {e}")
            QMessageBox.critical(self, "错误", f"导出失败:\n{str(e)}")

    def _export_spine_image(self):
        """导出脊柱检测图片"""
        result = self.current_result
        result_img = result.get("result_image")
        if result_img is None:
            QMessageBox.warning(self, "警告", "没有可导出的检测图片")
            return

        default_name = "spine_detection.png"

        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出脊柱检测图片", default_name,
            "PNG图片 (*.png);;JPEG图片 (*.jpg);;所有文件 (*.*)"
        )

        if not file_path:
            return

        try:
            success = imwrite_cn(file_path, result_img)

            if success:
                self.statusbar.showMessage(f"脊柱检测图片已保存: {file_path}")
                QMessageBox.information(self, "成功", "脊柱检测图片已导出")
            else:
                raise ValueError("图像保存失败")

        except Exception as e:
            logger.error(f"导出失败: {e}")
            QMessageBox.critical(self, "错误", f"导出失败:\n{str(e)}")

    def _export_foot_arch_image(self):
        """导出足弓测量图片"""
        result = self.current_result
        result_img = result.get("result_image")
        if result_img is None:
            QMessageBox.warning(self, "警告", "没有可导出的检测图片")
            return

        default_name = "foot_arch_measurement.png"
        if self.current_patient:
            default_name = f"足弓_{self.current_patient.access_no}_{self.current_patient.name}.png"

        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出足弓测量图片", default_name,
            "PNG图片 (*.png);;JPEG图片 (*.jpg);;所有文件 (*.*)"
        )

        if not file_path:
            return

        try:
            success = imwrite_cn(file_path, result_img)

            if success:
                self.statusbar.showMessage(f"足弓测量图片已保存: {file_path}")
                QMessageBox.information(self, "成功", "足弓测量图片已导出")
            else:
                raise ValueError("图像保存失败")

        except Exception as e:
            logger.error(f"导出失败: {e}")
            QMessageBox.critical(self, "错误", f"导出失败:\n{str(e)}")
    
    def _on_copy_summary(self):
        """复制影像诊断内容到剪贴板"""
        text = self.summary_label.text()
        
        # 移除HTML标签，提取纯文本
        import re
        clean_text = re.sub(r'<[^>]+>', '', text)
        clean_text = clean_text.replace('&nbsp;', ' ').replace('<br>', '\n').replace('<br/>', '\n')
        clean_text = clean_text.strip()
        
        if not clean_text or clean_text == "暂无诊断结果":
            return
        
        clipboard = QApplication.clipboard()
        clipboard.setText(clean_text)
        
        # 临时显示"已复制"反馈
        original_text = self.copy_btn.text()
        self.copy_btn.setText("已复制")
        self.copy_btn.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 16px;
                font-weight: 600;
                font-size: 12px;
                max-width: 80px;
            }
        """)
        
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(1500, lambda: self._restore_copy_button(original_text))
    
    def _restore_copy_button(self, original_text):
        """恢复复制按钮状态"""
        self.copy_btn.setText(original_text)
        self.copy_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 16px;
                font-weight: 600;
                font-size: 12px;
                max-width: 80px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """)
    
    def _export_boneage_image(self):
        """导出骨龄标注图片"""
        result = self.current_result
        result_img = result.get("result_image")
        if result_img is None:
            QMessageBox.warning(self, "警告", "没有可导出的标注图片")
            return
        
        default_name = "boneage_result.png"
        if self.current_patient:
            default_name = f"骨龄_{self.current_patient.access_no}_{self.current_patient.name}.png"
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出骨龄标注图片", default_name,
            "PNG图片 (*.png);;JPEG图片 (*.jpg);;所有文件 (*.*)"
        )
        
        if file_path:
            try:
                # OpenCV图像转RGB
                if len(result_img.shape) == 3 and result_img.shape[2] == 3:
                    result_img_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
                else:
                    result_img_rgb = result_img
                
                # 保存图像
                success = imwrite_cn(file_path, result_img_rgb)
                
                if success:
                    self.statusbar.showMessage(f"标注图片已保存: {file_path}")
                    QMessageBox.information(self, "成功", "骨龄标注图片已导出")
                else:
                    raise ValueError("图像保存失败")
            except Exception as e:
                logger.error(f"导出失败: {e}")
                QMessageBox.critical(self, "错误", f"导出失败:\n{str(e)}")
    
    def _export_diagnosis_report(self):
        """导出胸部诊断检测报告"""
        default_name = "diagnosis_report.png"
        if self.current_patient:
            default_name = f"{self.current_patient.access_no}_{self.current_patient.name}.png"
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出报告", default_name,
            "PNG图片 (*.png);;JPEG图片 (*.jpg);;所有文件 (*.*)"
        )
        
        if file_path:
            try:
                display_img = self.current_image.copy()
                for det in self.current_results:
                    x1, y1, x2, y2 = det["bbox"]
                    class_id = det["class_id"]
                    color = self.inference_engine.colors[class_id].tolist()
                    cv2.rectangle(display_img, (x1, y1), (x2, y2), color, 2)
                
                save_img = cv2.cvtColor(display_img, cv2.COLOR_RGB2BGR)
                imwrite_cn(file_path, save_img)
                
                self.statusbar.showMessage(f"报告已保存: {file_path}")
                QMessageBox.information(self, "成功", "诊断报告已导出")
                
            except Exception as e:
                logger.error(f"导出失败: {e}")
                QMessageBox.critical(self, "错误", f"导出失败:\n{str(e)}")
    
    def _on_fracture_detect(self):
        """执行骨折检测 - 后台线程"""
        img_path = self.current_image_path
        if not img_path:
            QMessageBox.warning(self, "提示", "请先打开图像")
            return

        self.fracture_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.statusbar.showMessage("正在执行骨折检测...")

        if hasattr(self.image_label, 'start_scan'):
            self.image_label.start_scan(duration_ms=2000)

        settings = self._get_detection_settings()
        self.fracture_worker = FractureWorker(img_path, settings)
        self.fracture_worker.finished.connect(self._on_fracture_finished)
        self.fracture_worker.error.connect(self._on_fracture_error)
        self.fracture_worker.start()
    
    def _on_fracture_finished(self, result):
        """骨折检测完成"""
        self.progress_bar.setVisible(False)
        self.statusbar.showMessage("骨折检测完成")
        self.fracture_btn.setEnabled(True)

        if not result.get('success', False):
            if hasattr(self.image_label, 'stop_scan_smooth'):
                self.image_label.stop_scan_smooth()
            QMessageBox.critical(self, "检测失败", result.get('error', '未知错误'))
            return

        self.current_result = result

        result_img = result.get('result_image')
        if result_img is not None:
            result_img_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
            h, w, ch = result_img_rgb.shape
            bytes_per_line = ch * w
            qt_image = QImage(result_img_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)

            label_size = self.image_label.size()
            scaled = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()

        fracture_count = result.get('fracture_count', 0)
        has_fracture = result.get('has_fracture', False)
        report = result.get('report', '')

        self.result_table.setRowCount(0)

        self.summary_label.setText(report)
        self.copy_btn.setEnabled(True)

        self.export_btn.setEnabled(True)

        self._save_detection_record('fracture', result, report)
    
    def _on_fracture_error(self, error_msg):
        """骨折检测错误"""
        self.progress_bar.setVisible(False)
        self.statusbar.showMessage("骨折检测失败")
        self.fracture_btn.setEnabled(True)

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()
        QMessageBox.critical(self, "错误", f"骨折检测失败:\n{error_msg}")

    def _on_spine_detect(self):
        """执行脊柱侧弯检测"""
        img_path = self.current_image_path
        if not img_path:
            QMessageBox.warning(self, "提示", "请先打开图像")
            return

        self.spine_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.statusbar.showMessage("正在执行脊柱侧弯检测...")

        if hasattr(self.image_label, 'start_scan'):
            self.image_label.start_scan(duration_ms=2000)

        QApplication.processEvents()

        self.spine_worker = SpineWorker(img_path)
        self.spine_worker.finished.connect(self._on_spine_finished)
        self.spine_worker.error.connect(self._on_spine_error)
        self.spine_worker.start()

    def _on_spine_finished(self, result):
        """脊柱检测完成"""
        self.progress_bar.setVisible(False)
        self.statusbar.showMessage("脊柱检测完成")
        self.spine_btn.setEnabled(True)

        if not result.get('success', False):
            if hasattr(self.image_label, 'stop_scan_smooth'):
                self.image_label.stop_scan_smooth()
            QMessageBox.critical(self, "检测失败", result.get('error', '未知错误'))
            return

        self.current_result = result

        result_img = result.get('result_image')
        if result_img is not None:
            result_img_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
            h, w, ch = result_img_rgb.shape
            bytes_per_line = ch * w
            qt_image = QImage(result_img_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)

            label_size = self.image_label.size()
            scaled = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()

        self.result_table.setRowCount(0)

        report = result.get('report', '')
        self.summary_label.setText(report)
        self.copy_btn.setEnabled(True)

        self.export_btn.setEnabled(True)

        self._save_detection_record('spine', result, report)

        cobb_angle = result.get('cobb_angle')
        vertebra_count = result.get('vertebra_count', 0)
        if cobb_angle is not None:
            QMessageBox.information(
                self, "检测完成",
                f"脊柱检测完成！\n\nCobb角: {cobb_angle:.1f}°\n椎体数: {vertebra_count} 个\n请查看右侧详细报告"
            )
        else:
            QMessageBox.information(
                self, "检测完成",
                f"脊柱检测完成！\n\n检测到 {vertebra_count} 个椎体\n请查看右侧详细报告"
            )

    def _on_spine_error(self, error_msg):
        """脊柱检测错误"""
        self.progress_bar.setVisible(False)
        self.statusbar.showMessage("脊柱检测失败")
        self.spine_btn.setEnabled(True)

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()
        QMessageBox.critical(self, "错误", f"脊柱检测失败:\n{error_msg}")

    def _on_foot_arch_measure(self):
        """执行足弓测量"""
        if self.current_image is None:
            QMessageBox.warning(self, "提示", "请先打开图像")
            return

        self.foot_arch_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.statusbar.showMessage("正在执行足弓测量...")

        if hasattr(self.image_label, 'start_scan'):
            self.image_label.start_scan(duration_ms=2000)

        QApplication.processEvents()

        self.foot_arch_worker = FootArchWorker(self.current_image)
        self.foot_arch_worker.finished.connect(self._on_foot_arch_finished)
        self.foot_arch_worker.error.connect(self._on_foot_arch_error)
        self.foot_arch_worker.start()

    def _on_foot_arch_finished(self, result):
        """足弓测量完成"""
        self.progress_bar.setVisible(False)
        self.statusbar.showMessage("足弓测量完成")
        self.foot_arch_btn.setEnabled(True)

        if not result.get('success', False):
            if hasattr(self.image_label, 'stop_scan_smooth'):
                self.image_label.stop_scan_smooth()
            QMessageBox.critical(self, "检测失败", result.get('error', '未知错误'))
            return

        self.current_result = result

        result_img = result.get('result_image')
        if result_img is not None:
            result_img_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)
            h, w, ch = result_img_rgb.shape
            bytes_per_line = ch * w
            qt_image = QImage(result_img_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)

            label_size = self.image_label.size()
            scaled = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()

        self.result_table.setRowCount(0)

        medial_angle = result.get('medial_angle')
        lateral_angle = result.get('lateral_angle')

        # 格式化摘要（只保留内侧和外侧足弓两行）
        summary_lines = []
        if medial_angle is not None:
            summary_lines.append(f"内侧足弓角约: {int(round(medial_angle))}°")
        if lateral_angle is not None:
            summary_lines.append(f"外侧足弓角约: {int(round(lateral_angle))}°")

        summary_text = "<br>".join(summary_lines) if summary_lines else "未能检测到足弓关键点"
        self.summary_label.setText(summary_text)
        self.copy_btn.setEnabled(True)

        self.export_btn.setEnabled(True)

        self._save_detection_record('foot_arch', result, summary_text)

    def _on_foot_arch_error(self, error_msg):
        """足弓测量错误"""
        self.progress_bar.setVisible(False)
        self.statusbar.showMessage("足弓测量失败")
        self.foot_arch_btn.setEnabled(True)

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()
        QMessageBox.critical(self, "错误", f"足弓测量失败:\n{error_msg}")

    def _on_boneage(self):
        """执行骨龄检测 - 使用当前患者列表选中的图像"""
        # 检查是否有当前图像
        if self.current_image_path is None:
            QMessageBox.warning(self, "警告", "请先从左侧患者列表选择一个患者")
            return
        
        # 从DICOM元数据获取性别
        patient_sex = "Unknown"
        if self.current_metadata and "patient_sex" in self.current_metadata:
            patient_sex = self.current_metadata["patient_sex"]
        
        # 转换性别代码
        if patient_sex == "M":
            sex_code = "boy"
            sex_text = "男"
        elif patient_sex == "F":
            sex_code = "girl"
            sex_text = "女"
        else:
            # 如果DICOM中没有性别信息，弹出选择对话框
            from PyQt5.QtWidgets import QInputDialog
            sex_items = ["男", "女"]
            sex, ok = QInputDialog.getItem(self, "选择性别", "DICOM中未找到患者性别，请选择:", sex_items, 0, False)
            if not ok:
                return
            sex_code = "boy" if sex == "男" else "girl"
            sex_text = sex
        
        logger.info(f"骨龄检测 - 患者: {self.current_patient.name if self.current_patient else 'Unknown'}, 性别: {sex_text}")
        
        # 显示进度
        self.statusbar.showMessage(f"正在进行骨龄检测 (患者性别: {sex_text})...")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        
        # 启动扫描特效
        if hasattr(self.image_label, 'start_scan'):
            self.image_label.start_scan(duration_ms=2000)
        
        # 在后台线程执行检测
        from PyQt5.QtCore import QThread
        
        class BoneAgeWorker(QThread):
            finished = pyqtSignal(dict)
            error = pyqtSignal(str)

            def __init__(self, img_path, sex, settings):
                super().__init__()
                self.img_path = img_path
                self.sex = sex
                self.settings = settings

            def run(self):
                try:
                    from core.model.boneage_inference import get_boneage_inference
                    detector = get_boneage_inference()
                    detect_conf = self.settings.get("boneage_detect_conf", 0.30)
                    cls_conf = self.settings.get("boneage_cls_conf", 0.30)
                    result = detector.detect(self.img_path, self.sex,
                                                  detect_conf=detect_conf,
                                                  cls_conf=cls_conf)
                    self.finished.emit(result)
                except Exception as e:
                    self.error.emit(str(e))

        settings = self._get_detection_settings()
        self.boneage_worker = BoneAgeWorker(self.current_image_path, sex_code, settings)
        self.boneage_worker.finished.connect(self._on_boneage_finished)
        self.boneage_worker.error.connect(self._on_boneage_error)
        self.boneage_worker.start()
    
    def _on_boneage_finished(self, result):
        """骨龄检测完成"""
        self.progress_bar.setVisible(False)
        self.statusbar.showMessage("骨龄检测完成")

        if not result.get("success", False):
            if hasattr(self.image_label, 'stop_scan_smooth'):
                self.image_label.stop_scan_smooth()
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

            label_size = self.image_label.size()
            scaled = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()

        bone_age = result.get("bone_age", 0)
        total_score = result.get("total_score", 0)
        joint_count = result.get("joint_count", 0)
        report = result.get("report", "")

        self.result_table.setRowCount(0)

        summary_text = f"""<b>骨龄检测结果</b><br><br>
<b>骨龄:</b> {bone_age} 岁<br>
<b>总得分:</b> {total_score} 分<br>
<b>检测关节数:</b> {joint_count}/13<br><br>
<b>详细报告:</b><br>{report.replace(chr(10), '<br>')}"""

        self.summary_label.setText(summary_text)
        self.copy_btn.setEnabled(True)

        self.export_btn.setEnabled(True)
        
        # 自动保存检测记录
        self._save_detection_record('bone_age', result, summary_text)
        
        QMessageBox.information(self, "检测完成", f"骨龄检测完成！\n骨龄: {bone_age}岁")
    
    def _on_boneage_error(self, error_msg):
        """骨龄检测错误"""
        self.progress_bar.setVisible(False)
        self.statusbar.showMessage("骨龄检测失败")

        if hasattr(self.image_label, 'stop_scan_smooth'):
            self.image_label.stop_scan_smooth()
        QMessageBox.critical(self, "检测错误", error_msg)

    def _on_settings(self):
        """显示检测设置对话框"""
        try:
            from ui.windows.settings_dialog import SettingsDialog, get_detection_settings

            dialog = SettingsDialog(self)
            if dialog.exec_() == dialog.Accepted:
                settings = dialog.get_settings()
                logger.info(f"检测设置已更新: {settings}")
                QMessageBox.information(
                    self, "设置已更新",
                    "检测阈值已保存！\n\n"
                    f"• 胸片阈值: {settings['chest_conf']:.2f}\n"
                    f"• 骨龄-关节检测: {settings['boneage_detect_conf']:.2f}\n"
                    f"• 骨龄-等级分类: {settings['boneage_cls_conf']:.2f}\n"
                    f"• 骨折检测: {settings['fracture_conf']:.2f}\n\n"
                    "下次检测时将使用新设置的阈值。"
                )
        except Exception as e:
            logger.error(f"打开设置对话框失败: {e}")
            QMessageBox.critical(self, "错误", f"打开设置失败:\n{str(e)}")

    def _get_detection_settings(self):
        """获取当前检测阈值设置（筛查模式：低阈值，高灵敏度）"""
        try:
            from ui.windows.settings_dialog import get_detection_settings
            return get_detection_settings()
        except:
            return {
                "chest_conf": 0.15,
                "boneage_detect_conf": 0.20,
                "boneage_cls_conf": 0.20,
                "fracture_conf": 0.03
            }

    def _on_license(self):
        """显示授权对话框"""
        try:
            from ui.windows.license_dialog import show_license_dialog
            
            # 获取授权结果和是否为用户主动激活
            result = show_license_dialog(self, force_show=True)
            
            if isinstance(result, tuple):
                is_authorized, user_activated = result
            else:
                # 兼容旧版本（如果返回单个bool）
                is_authorized = result
                user_activated = False
            
            self._update_license_status()
            
            # 只有用户明确完成激活/试用操作才显示成功提示
            if user_activated:
                QMessageBox.information(self, "成功", "授权验证通过")
            elif not is_authorized:
                QMessageBox.warning(
                    self, "需要授权",
                    "本软件需要有效授权才能使用全部功能！\n\n"
                    "请输入授权码激活，或联系管理员获取试用授权。"
                )
                
        except Exception as e:
            logger.error(f"打开授权对话框失败: {e}")
            QMessageBox.critical(self, "错误", f"打开授权对话框失败:\n{str(e)}")

    def _on_open_annotation_tool(self):
        """打开足弓关键点标注工具"""
        try:
            from ui.windows.foot_arch_annotation_tool import FootArchAnnotationTool
            
            # 默认数据集路径
            app_root = Path(__file__).parent.parent.parent.parent
            default_dataset = app_root / "Pesplanus Dataset"
            dataset_root = str(default_dataset) if default_dataset.exists() else None
            
            self._annotation_tool = FootArchAnnotationTool(dataset_root)
            self._annotation_tool.show()
            self.statusbar.showMessage("足弓关键点标注工具已打开")
        except Exception as e:
            logger.error(f"打开标注工具失败: {e}")
            QMessageBox.critical(self, "错误", f"打开标注工具失败:\n{str(e)}")
    
    def _on_view_detection_records(self):
        """查看检测记录"""
        try:
            from ui.windows.detection_records_dialog import DetectionRecordsDialog
            
            dialog = DetectionRecordsDialog(self)
            dialog.exec_()
            
        except Exception as e:
            logger.error(f"打开检测记录对话框失败: {e}")
            QMessageBox.critical(self, "错误", f"打开检测记录失败:\n{str(e)}")
    
    def _save_detection_record(self, detection_type: str, results: dict = None, summary: str = ""):
        """保存检测记录到数据库"""
        try:
            from core.data.detection_records import get_db
            import json
            
            db = get_db()
            
            # 获取患者信息
            patient_name = ""
            access_no = ""
            study_date = ""
            image_path = self.current_image_path or ""
            
            if self.current_patient:
                patient_name = getattr(self.current_patient, 'name', '')
                access_no = getattr(self.current_patient, 'access_no', '')
                study_date = getattr(self.current_patient, 'study_date', '')
                logger.info(f"[记录保存] 患者信息: name={patient_name}, access_no={access_no}")
            else:
                logger.warning(f"[记录保存] 当前无选中患者，使用空患者信息")
            
            # 处理summary：如果是HTML格式，提取纯文本
            summary_text = summary
            if isinstance(summary, str) and '<' in summary:
                import re
                summary_text = re.sub('<[^>]+>', '', summary).strip()
            
            record_data = {
                'patient_name': patient_name,
                'patient_id': getattr(self.current_patient, 'patient_id', '') if self.current_patient else '',
                'access_no': access_no,
                'study_date': study_date,
                'image_path': image_path,
                'detection_type': detection_type,
                'results': results or {},
                'summary': summary_text,
                'model_version': ''
            }
            
            # 根据类型设置版本信息
            if detection_type == 'chest_xray':
                record_data['model_version'] = 'YOLOv8-CXR'
            elif detection_type == 'bone_age':
                record_data['model_version'] = 'RUS-CHN v1.0'
            elif detection_type == 'fracture':
                record_data['model_version'] = 'Fracture-YOLOv8s'
            elif detection_type == 'spine':
                record_data['model_version'] = 'Spine-YOLOv8'
            elif detection_type == 'foot_arch':
                record_data['model_version'] = 'FootArch-ResNet50'
            
            logger.info(f"[记录保存] 准备保存: type={detection_type}, patient={patient_name}")
            record_id = db.add_record(record_data)
            
            logger.info(f"✅ 已保存检测记录 ID={record_id}, 类型={detection_type}")
            self.statusbar.showMessage(f"已保存{detection_type}检测记录 (ID:{record_id})", 3000)
            
        except Exception as e:
            error_msg = f"保存检测记录失败({detection_type}): {e}"
            logger.error(error_msg, exc_info=True)
            import traceback
            traceback.print_exc()
            self.statusbar.showMessage(f"⚠️ 记录保存失败: {str(e)[:50]}", 5000)
    
    def _on_about(self):
        """关于对话框"""
        about_text = f"""
        <h2>X线辅助诊断系统</h2>
        <p><b>版本:</b> v2.2</p>
        <p>基于YOLOv8深度学习模型的智能诊断系统</p>

        <p style='color: #ff6b6b; font-weight: bold; font-size: 13px; margin: 15px 0; padding: 10px; background-color: #fff3f3; border-left: 4px solid #ff6b6b;'>
            ⚠️ 免责声明：本软件仅供学习交流使用，不做诊断证明。任何医疗诊断决策请以专业医疗机构和医生的意见为准。
        </p>

        <p><b>功能模块:</b></p>
        <ul>
            <li><b>胸片检测:</b> 支持14种胸部疾病自动识别与定位</li>
            <li><b>骨龄评估:</b> 基于RUS-CHN法（中华05标准）的骨龄自动评估</li>
            <li><b>骨折检测:</b> 基于深度学习的X光片骨折自动检测与定位</li>
            <li><b>脊柱侧弯检测:</b> 基于深度学习的脊柱侧弯检测与Cobb角自动测量</li>
            <li><b>足弓测量:</b> 基于深度学习的足弓关键点检测与内外侧足弓角自动测量</li>
            <li><b>格式支持:</b> DICOM、JPG、PNG、BMP</li>
            <li><b>患者管理:</b> 自动扫描DICOM目录，患者列表管理</li>
            <li><b>检测记录:</b> SQLite数据库存储，支持导出和删除</li>
        </ul>

        <p><b>胸片支持疾病:</b></p>
        <p>{', '.join(CLASS_CONFIG['class_names_cn'])}</p>

        <p><b>骨龄评估模块:</b></p>
        <ul>
            <li>算法: RUS-CHN分级计分法（中华05标准）</li>
            <li>评估关节: 13个关键骨骼关节（RUS法）</li>
            <li>适用范围: 0-18岁儿童青少年左手腕正位X光片</li>
            <li>输出: 骨龄（岁）、CHN总分、各关节成熟度等级</li>
        </ul>

        <p><b>骨折检测模块:</b></p>
        <ul>
            <li>算法: YOLOv8目标检测网络</li>
            <li>检测目标: 骨折区域自动识别与定位</li>
            <li>适用范围: 各部位X光片骨折检测</li>
            <li>输出: 骨折位置、置信度、检测结果图像</li>
            <li>特点: 实时检测、高精度定位、可视化标注</li>
        </ul>

        <p><b>脊柱侧弯检测模块:</b></p>
        <ul>
            <li>算法: YOLOv8目标检测 + Cobb角自动测量</li>
            <li>检测目标: 椎体识别与定位、脊柱侧弯分类</li>
            <li>测量指标: Cobb角（脊柱侧弯黄金标准）</li>
            <li>适用: 全脊柱正位X光片</li>
            <li>特点: 自动端椎/顶椎定位、曲线拟合、侧弯严重度分级</li>
        </ul>

        <p><b>足弓测量模块:</b></p>
        <ul>
            <li>算法: ResNet50 + 热图回归关键点检测</li>
            <li>检测目标: 5个足弓关键点（跟骨、距骨、第一跖骨头、第五跖骨头、跟骰关节）</li>
            <li>测量指标: 内侧足弓角、外侧足弓角</li>
            <li>适用: 足部X线正位/侧位片</li>
            <li>特点: 自动关键点定位、角度测量、扁平足/高足弓风险提示</li>
        </ul>

        <hr style='border: none; border-top: 1px solid #ddd; margin: 10px 0;'>
        <p style='color: #666; font-size: 11px;'>© 2026 X线辅助诊断系统<br>基于PyQt5 + YOLOv8 + RUS-CHN标准开发</p>
        """
        QMessageBox.about(self, "关于 - X线辅助诊断系统", about_text)
    
    def closeEvent(self, event):
        """关闭事件"""
        logger.info("应用程序关闭")
        event.accept()
