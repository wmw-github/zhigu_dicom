"""
患者列表对话框

显示扫描到的患者列表，支持选择患者进行诊断
"""
import os
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QLineEdit, QHeaderView, QMessageBox,
    QGroupBox, QSplitter, QAbstractItemView
)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont, QColor

from core.data.patient_scanner import PatientScanner, PatientInfo
from utils.logger import get_logger

logger = get_logger(__name__)


class PatientListDialog(QDialog):
    """患者列表对话框"""
    
    # 信号：选择患者
    patient_selected = pyqtSignal(PatientInfo)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("患者列表")
        self.setMinimumSize(900, 600)
        
        self.scanner = PatientScanner()
        self.patients = []
        self.selected_patient = None
        self.row_to_patient = {}  # 行号到患者的映射
        
        self._init_ui()
        self._load_patients()
    
    def _init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # 标题
        title_label = QLabel("DICOM患者列表")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 5px;")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)
        
        # 说明标签
        info_label = QLabel("扫描目录: D:\\ClearTmp\\DicomTmp, D:\\Cache | 仅显示X线检查(DX)")
        info_label.setStyleSheet("color: gray; font-size: 12px;")
        layout.addWidget(info_label)
        
        # 搜索框
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("搜索:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入患者姓名、ID或检查号进行搜索...")
        self.search_edit.textChanged.connect(self._on_search)
        search_layout.addWidget(self.search_edit)
        
        self.refresh_btn = QPushButton("刷新列表")
        self.refresh_btn.setToolTip("重新扫描DICOM目录")
        self.refresh_btn.clicked.connect(self._on_refresh)
        search_layout.addWidget(self.refresh_btn)
        
        layout.addLayout(search_layout)
        
        # 患者表格
        self.patient_table = QTableWidget()
        self.patient_table.setColumnCount(7)
        self.patient_table.setHorizontalHeaderLabels([
            "检查号", "患者姓名", "患者ID", "性别", "年龄", "检查时间", "图像数量"
        ])
        
        # 设置表格属性
        self.patient_table.horizontalHeader().setStretchLastSection(True)
        self.patient_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.patient_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.patient_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.patient_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.patient_table.setAlternatingRowColors(False)  # 禁用交替行颜色，避免影响分组标题
        self.patient_table.setStyleSheet("""
            QTableWidget {
                background-color: white;
            }
            QTableWidget::item:selected {
                background-color: #0078d7;
                color: white;
            }
        """)
        
        # 双击选择
        self.patient_table.doubleClicked.connect(self._on_double_click)
        self.patient_table.itemSelectionChanged.connect(self._on_selection_changed)
        
        layout.addWidget(self.patient_table)
        
        # 统计信息
        self.stats_label = QLabel("共 0 个患者")
        self.stats_label.setStyleSheet("color: gray;")
        layout.addWidget(self.stats_label)
        
        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)
        
        self.select_btn = QPushButton("选择患者")
        self.select_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078d7;
                color: white;
                padding: 8px 20px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #005a9e;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        self.select_btn.setEnabled(False)
        self.select_btn.clicked.connect(self._on_select)
        btn_layout.addWidget(self.select_btn)
        
        layout.addLayout(btn_layout)
    
    def _load_patients(self):
        """加载患者列表"""
        try:
            self.patients = self.scanner.scan()
            self._update_table(self.patients)
            self.stats_label.setText(f"共 {len(self.patients)} 个患者 (仅X线检查)")
            
            if not self.patients:
                QMessageBox.information(
                    self, 
                    "提示", 
                    "未找到X线检查患者。\n\n请确认:\n"
                    "1. D:\\ClearTmp\\DicomTmp 或 D:\\Cache 目录存在\n"
                    "2. 目录中包含有效的DICOM XML文件\n"
                    "3. 检查类型为DX(X线)而非CT或MRI"
                )
            
        except Exception as e:
            logger.error(f"加载患者列表失败: {e}")
            QMessageBox.critical(self, "错误", f"加载患者列表失败:\n{str(e)}")
    
    def _update_table(self, patients):
        """更新表格显示，按来源目录分组"""
        self.patient_table.setRowCount(0)
        self.row_to_patient.clear()
        
        # 按来源目录分组
        groups = {}
        for patient in patients:
            source = patient.source_dir or "未知来源"
            if source not in groups:
                groups[source] = []
            groups[source].append(patient)
        
        # 定义目录友好名称
        dir_names = {
            r"D:\ClearTmp\DicomTmp": "D:\\ClearTmp\\DicomTmp (本地缓存)",
            r"D:\Cache": "D:\\Cache (设备缓存)",
            r"E:\ClearTmp\DicomTmp": "E:\\ClearTmp\\DicomTmp (本地缓存)",
            r"E:\Cache": "E:\\Cache (设备缓存)",
        }
        
        row = 0
        for source_dir, group_patients in groups.items():
            # 添加分组标题行
            self.patient_table.insertRow(row)
            dir_name = dir_names.get(source_dir, source_dir)
            header_item = QTableWidgetItem(f"📁 {dir_name} ({len(group_patients)}个患者)")
            header_item.setFlags(Qt.ItemIsEnabled)  # 不可选中
            header_item.setBackground(QColor("#e8f4fd"))
            header_item.setForeground(QColor("#0078d7"))
            font = header_item.font()
            font.setBold(True)
            font.setPointSize(10)
            header_item.setFont(font)
            self.patient_table.setItem(row, 0, header_item)
            
            # 合并标题行的所有列
            for col in range(1, self.patient_table.columnCount()):
                self.patient_table.setItem(row, col, QTableWidgetItem(""))
                self.patient_table.item(row, col).setFlags(Qt.ItemIsEnabled)
                self.patient_table.item(row, col).setBackground(QColor("#e8f4fd"))
            
            self.patient_table.setSpan(row, 0, 1, self.patient_table.columnCount())
            row += 1
            
            # 添加该组的患者
            for patient in group_patients:
                self.patient_table.insertRow(row)
                
                # 检查号
                self.patient_table.setItem(row, 0, QTableWidgetItem(patient.access_no))
                
                # 患者姓名
                self.patient_table.setItem(row, 1, QTableWidgetItem(patient.name))
                
                # 患者ID
                self.patient_table.setItem(row, 2, QTableWidgetItem(patient.patient_id))
                
                # 性别
                self.patient_table.setItem(row, 3, QTableWidgetItem(patient.sex))
                
                # 年龄
                self.patient_table.setItem(row, 4, QTableWidgetItem(patient.age))
                
                # 检查时间
                self.patient_table.setItem(row, 5, QTableWidgetItem(patient.study_datetime))
                
                # 图像数量
                img_count_item = QTableWidgetItem(str(len(patient.dicom_files)))
                img_count_item.setTextAlignment(Qt.AlignCenter)
                self.patient_table.setItem(row, 6, img_count_item)
                
                # 记录行号到患者的映射
                self.row_to_patient[row] = patient
                
                row += 1
        
        # 调整列宽
        self.patient_table.resizeColumnsToContents()
        self.patient_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
    
    def _on_search(self, text):
        """搜索过滤"""
        if not text:
            self._update_table(self.patients)
            return
        
        text = text.lower()
        filtered = []
        
        for patient in self.patients:
            if (text in patient.name.lower() or
                text in patient.patient_id.lower() or
                text in patient.access_no.lower()):
                filtered.append(patient)
        
        self._update_table(filtered)
        self.stats_label.setText(f"显示 {len(filtered)} / {len(self.patients)} 个患者")
    
    def _on_refresh(self):
        """刷新列表"""
        self.search_edit.clear()
        self._load_patients()
    
    def _on_selection_changed(self):
        """选择改变"""
        selected_items = self.patient_table.selectedItems()
        self.select_btn.setEnabled(len(selected_items) > 0)
        
        if selected_items:
            row = selected_items[0].row()
            # 使用行号映射获取患者
            if row in self.row_to_patient:
                self.selected_patient = self.row_to_patient[row]
    
    def _on_double_click(self, index):
        """双击选择"""
        row = index.row()
        # 使用行号映射获取患者
        if row in self.row_to_patient:
            self.selected_patient = self.row_to_patient[row]
            self._on_select()
    
    def _on_select(self):
        """选择患者"""
        if self.selected_patient:
            if not self.selected_patient.dicom_files:
                QMessageBox.warning(self, "警告", "该患者没有可用的DICOM图像文件")
                return
            
            self.patient_selected.emit(self.selected_patient)
            self.accept()
    
    def get_selected_patient(self) -> PatientInfo:
        """获取选中的患者"""
        return self.selected_patient
