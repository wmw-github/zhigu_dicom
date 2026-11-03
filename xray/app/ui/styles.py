"""
UI样式模块

提供专业、简洁、美观的界面样式
"""

# 主窗口样式
MAIN_WINDOW_STYLE = """
QMainWindow {
    background-color: #f5f6f7;
}

QWidget {
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}

/* 分组框样式 */
QGroupBox {
    background-color: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 16px;
    font-weight: bold;
    color: #333333;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 8px;
    color: #0078d4;
    font-size: 13px;
}

/* 按钮样式 */
QPushButton {
    background-color: #0078d4;
    color: white;
    border: none;
    border-radius: 4px;
    padding: 8px 16px;
    font-weight: 500;
    min-height: 32px;
}

QPushButton:hover {
    background-color: #106ebe;
}

QPushButton:pressed {
    background-color: #005a9e;
}

QPushButton:disabled {
    background-color: #cccccc;
    color: #666666;
}

QPushButton#secondary {
    background-color: #f0f0f0;
    color: #333333;
    border: 1px solid #d0d0d0;
}

QPushButton#secondary:hover {
    background-color: #e5e5e5;
}

/* 输入框样式 */
QLineEdit {
    background-color: white;
    border: 1px solid #d0d0d0;
    border-radius: 4px;
    padding: 6px 10px;
    min-height: 20px;
}

QLineEdit:focus {
    border: 2px solid #0078d4;
    padding: 5px 9px;
}

QLineEdit::placeholder {
    color: #999999;
}

/* 表格样式 */
QTableWidget {
    background-color: white;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    gridline-color: #f0f0f0;
    selection-background-color: #0078d4;
    selection-color: white;
    alternate-background-color: #f8f9fa;
}

QTableWidget::item {
    padding: 8px 12px;
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
    padding: 10px 12px;
    border: none;
    border-bottom: 2px solid #e0e0e0;
    font-weight: 600;
    font-size: 12px;
}

QHeaderView::section:hover {
    background-color: #f0f0f0;
}

/* 标签样式 */
QLabel {
    color: #333333;
}

QLabel#title {
    font-size: 18px;
    font-weight: bold;
    color: #0078d4;
}

QLabel#subtitle {
    font-size: 12px;
    color: #666666;
}

QLabel#info {
    font-size: 11px;
    color: #999999;
}

/* 状态栏样式 */
QStatusBar {
    background-color: #f8f9fa;
    border-top: 1px solid #e0e0e0;
    padding: 4px 12px;
}

QStatusBar QLabel {
    font-size: 12px;
    color: #666666;
}

/* 进度条样式 */
QProgressBar {
    border: none;
    border-radius: 2px;
    background-color: #f0f0f0;
    text-align: center;
    height: 4px;
}

QProgressBar::chunk {
    background-color: #0078d4;
    border-radius: 2px;
}

/* 菜单栏样式 */
QMenuBar {
    background-color: #f8f9fa;
    border-bottom: 1px solid #e0e0e0;
    padding: 4px 8px;
}

QMenuBar::item {
    padding: 8px 16px;
    border-radius: 4px;
}

QMenuBar::item:selected {
    background-color: #e5f1fb;
    color: #0078d4;
}

QMenu {
    background-color: white;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 4px;
}

QMenu::item {
    padding: 8px 24px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #e5f1fb;
    color: #0078d4;
}

/* 工具栏样式 */
QToolBar {
    background-color: #f8f9fa;
    border-bottom: 1px solid #e0e0e0;
    padding: 8px;
    spacing: 8px;
}

QToolBar QPushButton {
    background-color: transparent;
    color: #333333;
    border: 1px solid #d0d0d0;
    padding: 6px 12px;
    font-size: 12px;
}

QToolBar QPushButton:hover {
    background-color: #f0f0f0;
    border-color: #0078d4;
    color: #0078d4;
}

/* 滚动区域样式 */
QScrollArea {
    border: none;
    background-color: transparent;
}

QScrollBar:vertical {
    background-color: #f0f0f0;
    width: 8px;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    background-color: #c0c0c0;
    border-radius: 4px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background-color: #a0a0a0;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollBar:horizontal {
    background-color: #f0f0f0;
    height: 8px;
    border-radius: 4px;
}

QScrollBar::handle:horizontal {
    background-color: #c0c0c0;
    border-radius: 4px;
    min-width: 30px;
}

QScrollBar::handle:horizontal:hover {
    background-color: #a0a0a0;
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0px;
}

/* 分割器样式 */
QSplitter::handle {
    background-color: #e0e0e0;
}

QSplitter::handle:horizontal {
    width: 2px;
}

QSplitter::handle:vertical {
    height: 2px;
}

QSplitter::handle:hover {
    background-color: #0078d4;
}
"""

# 患者列表面板样式
PATIENT_LIST_STYLE = """
QWidget {
    background-color: #f8f9fa;
}

QLabel#patient_list_title {
    font-size: 16px;
    font-weight: bold;
    color: #0078d4;
    padding: 12px;
}

QLabel#patient_list_info {
    font-size: 11px;
    color: #666666;
    padding: 4px 12px;
}

QLineEdit#patient_search {
    background-color: white;
    border: 1px solid #d0d0d0;
    border-radius: 20px;
    padding: 8px 16px;
    margin: 8px 12px;
}

QLineEdit#patient_search:focus {
    border: 2px solid #0078d4;
    padding: 7px 15px;
}

QPushButton#refresh_btn {
    background-color: #f0f0f0;
    color: #333333;
    border: 1px solid #d0d0d0;
    border-radius: 4px;
    margin: 0 12px;
}

QPushButton#refresh_btn:hover {
    background-color: #e5e5e5;
    border-color: #0078d4;
}

QTableWidget#patient_table {
    background-color: white;
    border: none;
    border-radius: 8px;
    margin: 8px 12px;
}

QLabel#patient_stats {
    font-size: 11px;
    color: #999999;
    padding: 8px 12px;
}
"""

# 图像显示面板样式
IMAGE_PANEL_STYLE = """
QWidget {
    background-color: #f5f6f7;
}

QGroupBox#patient_info {
    background-color: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    margin: 8px;
}

QLabel#image_placeholder {
    background-color: #2b2b2b;
    color: #888888;
    font-size: 14px;
    border-radius: 8px;
}

QPushButton#action_btn {
    background-color: #0078d4;
    color: white;
    border: none;
    border-radius: 4px;
    padding: 10px 20px;
    font-weight: 500;
    min-width: 100px;
}

QPushButton#action_btn:hover {
    background-color: #106ebe;
}

QPushButton#action_btn:disabled {
    background-color: #cccccc;
    color: #666666;
}

QPushButton#secondary_btn {
    background-color: #f0f0f0;
    color: #333333;
    border: 1px solid #d0d0d0;
    border-radius: 4px;
    padding: 10px 20px;
    font-weight: 500;
    min-width: 100px;
}

QPushButton#secondary_btn:hover {
    background-color: #e5e5e5;
    border-color: #0078d4;
}
"""

# 结果面板样式
RESULT_PANEL_STYLE = """
QWidget {
    background-color: #f8f9fa;
}

QGroupBox {
    background-color: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    margin: 8px;
}

QLabel#summary {
    font-size: 13px;
    line-height: 1.6;
    color: #333333;
}

QLabel#summary_title {
    font-size: 14px;
    font-weight: bold;
    color: #0078d4;
}

QTableWidget#result_table {
    background-color: white;
    border: none;
    border-radius: 8px;
}
"""

# 授权对话框样式
LICENSE_DIALOG_STYLE = """
QDialog {
    background-color: #f5f6f7;
}

QLabel#license_title {
    font-size: 20px;
    font-weight: bold;
    color: #0078d4;
    padding: 16px;
}

QLabel#machine_code {
    font-size: 18px;
    font-weight: bold;
    color: #0078d4;
    background-color: #f0f7ff;
    border: 2px solid #0078d4;
    border-radius: 8px;
    padding: 16px;
}

QLabel#status_valid {
    color: #107c10;
    font-weight: bold;
    font-size: 14px;
}

QLabel#status_invalid {
    color: #d83b01;
    font-weight: bold;
    font-size: 14px;
}

QPushButton#activate_btn {
    background-color: #107c10;
    color: white;
    border: none;
    border-radius: 4px;
    padding: 12px 24px;
    font-weight: bold;
    font-size: 14px;
}

QPushButton#activate_btn:hover {
    background-color: #0e6e0e;
}

QPushButton#trial_btn {
    background-color: #0078d4;
    color: white;
    border: none;
    border-radius: 4px;
    padding: 12px 24px;
    font-weight: bold;
    font-size: 14px;
}

QPushButton#trial_btn:hover {
    background-color: #106ebe;
}

QPushButton#exit_btn {
    background-color: #f0f0f0;
    color: #333333;
    border: 1px solid #d0d0d0;
    border-radius: 4px;
    padding: 12px 24px;
    font-weight: 500;
}

QPushButton#exit_btn:hover {
    background-color: #e5e5e5;
}
"""


def get_main_style():
    """获取主窗口样式"""
    return MAIN_WINDOW_STYLE


def get_patient_list_style():
    """获取患者列表样式"""
    return PATIENT_LIST_STYLE


def get_image_panel_style():
    """获取图像面板样式"""
    return IMAGE_PANEL_STYLE


def get_result_panel_style():
    """获取结果面板样式"""
    return RESULT_PANEL_STYLE


def get_license_dialog_style():
    """获取授权对话框样式"""
    return LICENSE_DIALOG_STYLE
