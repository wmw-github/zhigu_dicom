"""
License授权对话框

用于显示机器码、输入授权码进行激活
"""
import sys
import os

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox, QGroupBox, QTextEdit, QApplication
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

from license_secure import (
    get_machine_code_short,
    activate_license,
    check_license_secure,
    get_license_status_secure,
    create_trial_license
)
from utils.logger import get_logger
from ui.styles import get_license_dialog_style

logger = get_logger(__name__)


class LicenseDialog(QDialog):
    """License授权对话框"""
    
    def __init__(self, parent=None, is_trial_expired=False):
        super().__init__(parent)
        self.is_trial_expired = is_trial_expired
        self.activation_success = False
        
        self.setWindowTitle("X线辅助诊断系统 - 软件授权")
        self.setMinimumSize(550, 450)
        
        self._init_ui()
        self._check_current_status()
    
    def _init_ui(self):
        """初始化UI"""
        self.setStyleSheet(get_license_dialog_style())
        
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)
        
        # 标题
        title = QLabel("X线辅助诊断系统")
        title.setStyleSheet("""
            font-size: 22px;
            font-weight: bold;
            color: #0078d4;
            padding: 12px;
        """)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        # 副标题
        subtitle = QLabel("软件授权管理")
        subtitle.setStyleSheet("""
            font-size: 14px;
            color: #666666;
            padding-bottom: 8px;
        """)
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)
        
        # 状态显示
        self.status_group = QGroupBox("当前授权状态")
        self.status_group.setStyleSheet("""
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
        status_layout = QVBoxLayout()
        
        self.status_label = QLabel("正在检查授权状态...")
        self.status_label.setStyleSheet("font-size: 14px; padding: 12px; color: #333333;")
        self.status_label.setAlignment(Qt.AlignCenter)
        status_layout.addWidget(self.status_label)
        
        self.status_group.setLayout(status_layout)
        layout.addWidget(self.status_group)
        
        # 机器码显示
        machine_group = QGroupBox("机器码 (请将此码提供给管理员)")
        machine_group.setStyleSheet("""
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
        machine_layout = QVBoxLayout()
        
        machine_code = get_machine_code_short()
        self.machine_label = QLabel(machine_code)
        self.machine_label.setStyleSheet("""
            font-size: 18px;
            font-weight: bold;
            color: #0078d4;
            padding: 16px;
            background-color: #f0f7ff;
            border: 2px solid #0078d4;
            border-radius: 8px;
            font-family: "Consolas", "Courier New", monospace;
            letter-spacing: 2px;
        """)
        self.machine_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.machine_label.setAlignment(Qt.AlignCenter)
        machine_layout.addWidget(self.machine_label)
        
        # 复制按钮
        copy_btn = QPushButton("复制机器码")
        copy_btn.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                color: #333333;
                border: 1px solid #d0d0d0;
                border-radius: 4px;
                padding: 8px 16px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #e5e5e5;
                border-color: #0078d4;
                color: #0078d4;
            }
        """)
        copy_btn.clicked.connect(self._copy_machine_code)
        machine_layout.addWidget(copy_btn)
        
        machine_group.setLayout(machine_layout)
        layout.addWidget(machine_group)
        
        # 授权码输入
        license_group = QGroupBox("输入授权码")
        license_group.setStyleSheet("""
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
        license_layout = QVBoxLayout()
        
        self.license_edit = QLineEdit()
        self.license_edit.setPlaceholderText("请输入授权码 (如: UNLIMITED-XXXXXXXXXXXXXXXX)")
        self.license_edit.setStyleSheet("""
            QLineEdit {
                background-color: white;
                border: 1px solid #d0d0d0;
                border-radius: 4px;
                padding: 10px;
                font-size: 14px;
            }
            QLineEdit:focus {
                border: 2px solid #0078d4;
                padding: 9px;
            }
        """)
        license_layout.addWidget(self.license_edit)
        
        # 激活按钮
        self.activate_btn = QPushButton("激活")
        self.activate_btn.setStyleSheet("""
            QPushButton {
                background-color: #107c10;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 12px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #0e6e0e;
            }
        """)
        self.activate_btn.clicked.connect(self._on_activate)
        license_layout.addWidget(self.activate_btn)
        
        license_group.setLayout(license_layout)
        layout.addWidget(license_group)
        
        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        if not self.is_trial_expired:
            self.skip_btn = QPushButton("开始试用(72小时)")
            self.skip_btn.setStyleSheet("""
                QPushButton {
                    background-color: #0078d7;
                    color: white;
                    padding: 8px 15px;
                }
                QPushButton:hover {
                    background-color: #005a9e;
                }
            """)
            self.skip_btn.clicked.connect(self._on_start_trial)
            btn_layout.addWidget(self.skip_btn)
        
        self.exit_btn = QPushButton("退出程序")
        self.exit_btn.clicked.connect(self._on_exit)
        btn_layout.addWidget(self.exit_btn)
        
        layout.addLayout(btn_layout)
    
    def _check_current_status(self):
        """检查当前授权状态"""
        try:
            status = get_license_status_secure()

            if status["is_valid"]:
                license_type = status.get("type", "")

                if license_type == "unlimited":
                    self.status_label.setText("✓ 永久授权")
                    self.status_label.setStyleSheet("font-size: 14px; padding: 10px; color: green;")
                    if hasattr(self, 'skip_btn'):
                        self.skip_btn.setVisible(False)
                elif status["remaining_hours"] >= 999999:
                    self.status_label.setText("✓ 永久授权")
                    self.status_label.setStyleSheet("font-size: 14px; padding: 10px; color: green;")
                    if hasattr(self, 'skip_btn'):
                        self.skip_btn.setVisible(False)
                else:
                    remaining = int(status["remaining_hours"])
                    self.status_label.setText(f"✓ 试用期剩余: 约{remaining}小时")
                    self.status_label.setStyleSheet("font-size: 14px; padding: 10px; color: orange;")
            else:
                self.status_label.setText("✗ 授权已过期或无效")
                self.status_label.setStyleSheet("font-size: 14px; padding: 10px; color: red;")

                if self.is_trial_expired:
                    self.status_label.setText("✗ 试用期已结束，请购买授权码")

        except Exception as e:
            logger.error(f"检查授权状态失败: {e}")
            self.status_label.setText("? 无法检查授权状态")
    
    def _copy_machine_code(self):
        """复制机器码到剪贴板"""
        machine_code = get_machine_code_short()
        clipboard = QApplication.clipboard()
        clipboard.setText(machine_code)
        QMessageBox.information(self, "成功", "机器码已复制到剪贴板！")
    
    def _on_activate(self):
        """激活授权"""
        license_key = self.license_edit.text().strip()
        
        if not license_key:
            QMessageBox.warning(self, "错误", "请输入授权码")
            return
        
        try:
            success, message = activate_license(license_key)
            
            if success:
                QMessageBox.information(self, "成功", message)
                self.activation_success = True
                self.accept()
            else:
                QMessageBox.critical(self, "失败", message)
                
        except Exception as e:
            logger.error(f"激活失败: {e}")
            QMessageBox.critical(self, "错误", f"激活失败: {str(e)}")
    
    def _on_start_trial(self):
        """开始试用"""
        try:
            # 先检查是否已有有效的试用授权
            is_valid, status_msg = check_license_secure()
            if is_valid:
                remaining = get_license_status_secure().get("remaining_hours", 0)
                if remaining >= 999999:
                    QMessageBox.warning(self, "提示", "您已经是永久授权状态，无需试用！")
                else:
                    QMessageBox.warning(
                        self, "提示",
                        f"您当前还有有效的试用期！\n\n"
                        f"剩余时间: 约{int(remaining)}小时\n\n"
                        f"如需延长试用时间，请联系管理员获取正式授权码。"
                    )
                return

            # 如果试用期已结束，不允许再次试用
            if self.is_trial_expired:
                QMessageBox.warning(
                    self, "试用已结束",
                    "您的试用期已经结束！\n\n"
                    "如需继续使用本软件，请购买正式授权码。\n"
                    "或联系管理员申请延长试用期。"
                )
                return

            # 创建新的试用授权
            success, message = create_trial_license()
            if success:
                QMessageBox.information(self, "成功", f"{message}\n\n您获得72小时试用时间。")
                self.activation_success = True
                self.accept()
            else:
                QMessageBox.critical(self, "失败", message)
        except Exception as e:
            logger.error(f"创建试用授权失败: {e}")
            QMessageBox.critical(self, "错误", f"创建试用授权失败: {str(e)}")
    
    def _on_exit(self):
        """退出程序"""
        self.reject()
        # 通知主程序退出
        if self.parent():
            self.parent().close()
    
    def is_activated(self) -> bool:
        """是否激活成功"""
        return self.activation_success


def show_license_dialog(parent=None, force_show=False):
    """
    显示授权对话框

    Args:
        parent: 父窗口
        force_show: 是否强制显示（忽略试用状态）

    Returns:
        tuple: (is_authorized, user_activated)
            - is_authorized: 是否有有效授权（可以进入程序）
            - user_activated: 是否用户刚完成了激活/试用操作
    """
    # 检查当前授权状态
    is_valid, message = check_license_secure()

    # 如果授权有效且不是强制显示，直接返回True
    if is_valid and not force_show:
        return (True, False)

    # 试用期是否已过期
    is_expired = not is_valid

    # 显示授权对话框
    dialog = LicenseDialog(parent, is_trial_expired=is_expired)
    result = dialog.exec_()

    # 只有用户明确激活或开始试用了才标记为user_activated
    user_activated = dialog.is_activated()

    if user_activated:
        # 用户完成了有效的激活/试用操作
        logger.info("用户完成授权操作")
        return (True, True)

    # 如果用户关闭对话框或点击退出（没有激活）
    if result == QDialog.Rejected or not user_activated:
        # 重新检查当前状态（可能之前就有有效授权）
        is_valid_now, _ = check_license_secure()

        if is_valid_now:
            # 之前有有效授权，允许继续使用
            logger.info("用户取消操作，但检测到已有有效授权")
            return (True, False)  # 有授权但不是本次激活的
        else:
            # 没有有效授权，且用户没有完成激活
            logger.warning("用户取消操作，无有效授权")

            # 如果是试用期已过期的情况
            if is_expired:
                QMessageBox.warning(
                    parent, "需要授权",
                    "本软件需要有效授权才能使用！\n\n"
                    "请输入授权码激活，或联系管理员获取试用授权。"
                )
            return (False, False)

    # 默认返回False（安全起见）
    return (False, False)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    dialog = LicenseDialog()
    dialog.show()
    sys.exit(app.exec_())
