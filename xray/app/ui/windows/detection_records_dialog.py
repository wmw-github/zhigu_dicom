"""
检测记录对话框
用于查看、导出、删除检测记录
"""
import sys
import os
from datetime import datetime

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QLineEdit, QComboBox, QMessageBox, QProgressBar,
    QGroupBox, QFileDialog, QWidget, QHeaderView,
    QApplication, QAbstractItemView, QCheckBox
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont

from core.data.detection_records import get_db


class DetectionRecordsDialog(QDialog):
    """检测记录管理对话框"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("检测记录")
        self.setMinimumSize(1100, 700)
        self.db = get_db()
        
        # 分页参数
        self.current_page = 1
        self.per_page = 10
        self.total_pages = 1
        self.total_records = 0
        
        # 初始化UI
        self._init_ui()
        
        # 加载数据
        self._load_data()
    
    def _init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)
        
        # ====== 顶部搜索和筛选区域 ======
        search_group = QGroupBox("搜索与筛选")
        search_layout = QHBoxLayout(search_group)
        search_layout.setSpacing(10)
        
        # 搜索框
        search_layout.addWidget(QLabel("关键词:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入患者姓名或检查号...")
        self.search_input.setMaximumWidth(250)
        self.search_input.returnPressed.connect(self._on_search)
        search_layout.addWidget(self.search_input)
        
        # 类型筛选
        search_layout.addWidget(QLabel("类型:"))
        self.type_filter = QComboBox()
        self.type_filter.addItems(['全部', '胸片检测', '骨龄检测', '骨折检测'])
        self.type_filter.currentTextChanged.connect(self._on_search)
        search_layout.addWidget(self.type_filter)
        
        # 搜索按钮
        self.search_btn = QPushButton("🔍 搜索")
        self.search_btn.clicked.connect(self._on_search)
        search_layout.addWidget(self.search_btn)
        
        # 重置按钮
        self.reset_btn = QPushButton("🔄 重置")
        self.reset_btn.clicked.connect(self._on_reset)
        search_layout.addWidget(self.reset_btn)
        
        # 弹性空间
        search_layout.addStretch()
        
        # 统计信息
        self.stats_label = QLabel()
        self.stats_label.setStyleSheet("color: #666; font-size: 11px;")
        search_layout.addWidget(self.stats_label)
        
        layout.addWidget(search_group)
        
        # ====== 记录表格 ======
        table_group = QGroupBox("检测记录列表")
        table_layout = QVBoxLayout(table_group)
        
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            "序号", "检测时间", "患者姓名", "检查号", "检查日期", 
            "检测类型", "诊断摘要", "模型版本"
        ])
        
        # 设置表格属性
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)  # 多选
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        
        # 设置列宽
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)   # 序号
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)   # 时间
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)   # 姓名
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)   # 检查号
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)   # 日期
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)   # 类型
        header.setSectionResizeMode(6, QHeaderView.Stretch)            # 摘要（自适应）
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)   # 版本
        
        # 样式
        self.table.setStyleSheet("""
            QTableWidget {
                gridline-color: #e0e0e0;
                font-size: 11px;
            }
            QTableWidget::item {
                padding: 6px;
            }
            QTableWidget::item:selected {
                background-color: #0078d4;
                color: white;
            }
            QHeaderView::section {
                background-color: #f5f5f5;
                padding: 8px;
                border: none;
                border-bottom: 2px solid #ddd;
                font-weight: bold;
                font-size: 11px;
            }
        """)
        
        table_layout.addWidget(self.table)
        layout.addWidget(table_group)
        
        # ====== 底部操作区域 ======
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(10)
        
        # 左侧：分页控制
        page_group = QGroupBox("分页")
        page_layout = QHBoxLayout(page_group)
        page_layout.setSpacing(5)
        
        self.prev_btn = QPushButton("◀ 上一页")
        self.prev_btn.setEnabled(False)
        self.prev_btn.clicked.connect(self._prev_page)
        page_layout.addWidget(self.prev_btn)
        
        self.page_info_label = QLabel("第 1 / 1 页")
        self.page_info_label.setMinimumWidth(100)
        self.page_info_label.setAlignment(Qt.AlignCenter)
        page_layout.addWidget(self.page_info_label)
        
        self.next_btn = QPushButton("下一页 ▶")
        self.next_btn.setEnabled(False)
        self.next_btn.clicked.connect(self._next_page)
        page_layout.addWidget(self.next_btn)
        
        # 跳转到指定页
        page_layout.addWidget(QLabel("跳转:"))
        self.page_jump_input = QLineEdit()
        self.page_jump_input.setMaximumWidth(50)
        self.page_jump_input.returnPressed.connect(self._jump_to_page)
        page_layout.addWidget(self.page_jump_input)
        
        self.jump_btn = QPushButton("Go")
        self.jump_btn.setMaximumWidth(40)
        self.jump_btn.clicked.connect(self._jump_to_page)
        page_layout.addWidget(self.jump_btn)
        
        bottom_layout.addWidget(page_group)
        
        bottom_layout.addStretch()
        
        # 右侧：批量操作
        action_group = QGroupBox("批量操作")
        action_layout = QHBoxLayout(action_group)
        action_layout.setSpacing(8)
        
        self.select_all_cb = QCheckBox("全选")
        self.select_all_cb.stateChanged.connect(self._on_select_all)
        action_layout.addWidget(self.select_all_cb)
        
        self.delete_selected_btn = QPushButton("🗑️ 删除选中")
        self.delete_selected_btn.clicked.connect(self._delete_selected)
        action_layout.addWidget(self.delete_selected_btn)
        
        self.export_csv_btn = QPushButton("📄 导出CSV")
        self.export_csv_btn.clicked.connect(lambda: self._export('csv'))
        action_layout.addWidget(self.export_csv_btn)
        
        self.export_excel_btn = QPushButton("📊 导出Excel")
        self.export_excel_btn.clicked.connect(lambda: self._export('excel'))
        action_layout.addWidget(self.export_excel_btn)
        
        self.clear_all_btn = QPushButton("🗑️ 清空全部")
        self.clear_all_btn.setStyleSheet("color: red;")
        self.clear_all_btn.clicked.connect(self._clear_all)
        action_layout.addWidget(self.clear_all_btn)
        
        bottom_layout.addWidget(action_group)
        
        layout.addLayout(bottom_layout)
    
    def _load_data(self):
        """加载数据"""
        try:
            # 获取搜索条件
            search_text = self.search_input.text().strip()
            
            # 转换类型筛选
            type_map = {
                '全部': None,
                '胸片检测': 'chest_xray',
                '骨龄检测': 'bone_age',
                '骨折检测': 'fracture'
            }
            detection_type = type_map.get(self.type_filter.currentText(), None)
            
            # 查询数据
            result = self.db.get_records(
                page=self.current_page,
                per_page=self.per_page,
                search_text=search_text if search_text else None,
                detection_type=detection_type
            )
            
            records = result['records']
            self.total_records = result['total']
            self.total_pages = result['total_pages']
            
            # 更新表格
            self._update_table(records)
            
            # 更新分页信息
            self._update_pagination(result)
            
            # 更新统计信息
            stats = self.db.get_statistics()
            self.stats_label.setText(
                f"共 {stats['total']} 条记录 | "
                f"今日 {stats['today_count']} 条"
            )
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"加载检测记录失败:\n{str(e)}")
    
    def _update_table(self, records):
        """更新表格内容"""
        self.table.setRowCount(len(records))
        
        for row, record in enumerate(records):
            # 序号
            item = QTableWidgetItem(str((self.current_page - 1) * self.per_page + row + 1))
            item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 0, item)
            
            # 检测时间
            time_str = record.get('detection_time', '')
            if len(time_str) > 19:
                time_str = time_str[:19]
            self.table.setItem(row, 1, QTableWidgetItem(time_str))
            
            # 患者姓名
            self.table.setItem(row, 2, QTableWidgetItem(record.get('patient_name', '')))
            
            # 检查号
            self.table.setItem(row, 3, QTableWidgetItem(record.get('access_no', '')))
            
            # 检查日期
            self.table.setItem(row, 4, QTableWidgetItem(record.get('study_date', '')))
            
            # 检测类型（中文显示）
            type_display = {
                'chest_xray': '胸片检测',
                'bone_age': '骨龄检测',
                'fracture': '骨折检测'
            }.get(record.get('detection_type', ''), record.get('detection_type', ''))
            type_item = QTableWidgetItem(type_display)
            
            # 不同类型用不同颜色标识
            if record.get('detection_type') == 'chest_xray':
                type_item.setForeground(QColor("#0078d4"))
            elif record.get('detection_type') == 'bone_age':
                type_item.setForeground(QColor("#107c10"))
            elif record.get('detection_type') == 'fracture':
                type_item.setForeground(QColor("#d83b01"))
            
            self.table.setItem(row, 5, type_item)
            
            # 诊断摘要（截断显示，完整内容在tooltip中）
            summary = record.get('summary', '')
            summary_item = QTableWidgetItem(summary[:80] + ('...' if len(summary) > 80 else ''))
            summary_item.setToolTip(summary)
            self.table.setItem(row, 6, summary_item)
            
            # 模型版本
            self.table.setItem(row, 7, QTableWidgetItem(record.get('model_version', '')))
    
    def _update_pagination(self, result):
        """更新分页控件状态"""
        self.current_page = result['page']
        self.total_pages = result['total_pages']
        
        # 更新页码信息
        self.page_info_label.setText(f"第 {self.current_page} / {self.total_pages} 页")
        
        # 更新按钮状态
        self.prev_btn.setEnabled(result['has_prev'])
        self.next_btn.setEnabled(result['has_next'])
        
        # 清除全选状态
        self.select_all_cb.setChecked(False)
    
    def _on_search(self):
        """执行搜索"""
        self.current_page = 1  # 回到第一页
        self._load_data()
    
    def _on_reset(self):
        """重置搜索条件"""
        self.search_input.clear()
        self.type_filter.setCurrentIndex(0)  # 全部
        self.current_page = 1
        self._load_data()
    
    def _prev_page(self):
        """上一页"""
        if self.current_page > 1:
            self.current_page -= 1
            self._load_data()
    
    def _next_page(self):
        """下一页"""
        if self.current_page < self.total_pages:
            self.current_page += 1
            self._load_data()
    
    def _jump_to_page(self):
        """跳转到指定页"""
        try:
            page = int(self.page_jump_input.text())
            if 1 <= page <= self.total_pages:
                self.current_page = page
                self._load_data()
                self.page_jump_input.clear()
            else:
                QMessageBox.warning(self, "提示", f"请输入 1-{self.total_pages} 之间的页码")
        except ValueError:
            QMessageBox.warning(self, "提示", "请输入有效的页码数字")
    
    def _on_select_all(self, state):
        """全选/取消全选"""
        if state == Qt.Checked:
            self.table.selectAll()
        else:
            self.table.clearSelection()
    
    def _get_selected_ids(self) -> list:
        """获取选中的记录ID列表"""
        selected_rows = set(item.row() for item in self.table.selectedItems())
        ids = []
        for row in selected_rows:
            # 从表格获取记录ID（需要从数据库查询或存储在隐藏列）
            # 这里我们通过行号和当前页计算实际ID
            record_index = (self.current_page - 1) * self.per_page + row
            # 简化处理：重新查询该页数据获取ID
            pass
        
        # 更可靠的方法：直接查询当前页数据
        search_text = self.search_input.text().strip()
        type_map = {'全部': None, '胸片检测': 'chest_xray', 
                   '骨龄检测': 'bone_age', '骨折检测': 'fracture'}
        detection_type = type_map.get(self.type_filter.currentText(), None)
        
        result = self.db.get_records(
            page=self.current_page,
            per_page=self.per_page,
            search_text=search_text if search_text else None,
            detection_type=detection_type
        )
        
        selected_ids = [result['records'][row]['id'] for row in sorted(selected_rows)]
        return selected_ids
    
    def _delete_selected(self):
        """删除选中的记录"""
        selected_rows = list(set(item.row() for item in self.table.selectedItems()))
        
        if not selected_rows:
            QMessageBox.information(self, "提示", "请先选择要删除的记录")
            return
        
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除选中的 {len(selected_rows)} 条记录吗？\n\n此操作不可撤销！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            try:
                ids = self._get_selected_ids()
                deleted = self.db.delete_records(ids)
                
                QMessageBox.information(
                    self, "成功",
                    f"成功删除 {deleted} 条记录"
                )
                
                # 重新加载数据
                self._load_data()
                
            except Exception as e:
                QMessageBox.critical(self, "错误", f"删除失败:\n{str(e)}")
    
    def _clear_all(self):
        """清空所有记录"""
        reply = QMessageBox.question(
            self, "确认清空",
            "确定要清空所有检测记录吗？\n\n此操作不可撤销！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            try:
                deleted = self.db.clear_all_records()
                
                QMessageBox.information(
                    self, "成功",
                    f"已清空 {deleted} 条记录"
                )
                
                self._load_data()
                
            except Exception as e:
                QMessageBox.critical(self, "错误", f"清空失败:\n{str(e)}")
    
    def _export(self, format_type: str):
        """导出记录"""
        try:
            search_text = self.search_input.text().strip()
            type_map = {'全部': None, '胸片检测': 'chest_xray',
                       '骨龄检测': 'bone_age', '骨折检测': 'fracture'}
            detection_type = type_map.get(self.type_filter.currentText(), None)
            
            if format_type == 'csv':
                output_path = self.db.export_to_csv(
                    search_text=search_text if search_text else None,
                    detection_type=detection_type
                )
            else:  # excel
                output_path = self.db.export_to_excel(
                    search_text=search_text if search_text else None,
                    detection_type=detection_type
                )
            
            QMessageBox.information(
                self, "导出成功",
                f"记录已成功导出到:\n\n{output_path}"
            )
            
            # 可选：打开文件所在目录
            if os.path.exists(output_path):
                os.startfile(os.path.dirname(output_path))
                
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"导出过程中出现错误:\n{str(e)}")
