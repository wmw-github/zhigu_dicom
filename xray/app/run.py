"""
GUI程序入口

胸部X光片智能诊断系统
集成License验证
"""
import sys
import os
from pathlib import Path

# 获取应用程序根目录
if getattr(sys, 'frozen', False):
    APP_ROOT = Path(sys.executable).parent
else:
    APP_ROOT = Path(os.path.dirname(os.path.abspath(__file__)))

# 添加项目根目录到路径
sys.path.insert(0, str(APP_ROOT))

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon, QPixmap
from utils.logger import setup_logger


def debug_print(msg):
    """调试输出 - 仅在非pythonw环境下输出"""
    try:
        print(msg)
        sys.stdout.flush()
    except:
        pass


def set_application_icon(app):
    """设置应用程序图标"""
    try:
        debug_print(f"[DEBUG] APP_ROOT: {APP_ROOT}")
        debug_print(f"[DEBUG] 当前工作目录: {os.getcwd()}")
        
        icon_paths = [
            APP_ROOT / "assets" / "icon_32.png",
            APP_ROOT / "assets" / "icon_64.png",
            APP_ROOT / "assets" / "icon.png",
        ]
        
        for icon_path in icon_paths:
            exists = icon_path.exists()
            debug_print(f"[DEBUG] 检查图标: {icon_path}, 存在: {exists}")
            if exists:
                try:
                    pixmap = QPixmap(str(icon_path))
                    is_null = pixmap.isNull()
                    width = pixmap.size().width()
                    height = pixmap.size().height()
                    debug_print(f"[DEBUG] QPixmap加载: isNull={is_null}, size={width}x{height}")
                    
                    if not is_null:
                        icon = QIcon(pixmap)
                        app.setWindowIcon(icon)
                        debug_print(f"[INFO] 已设置应用程序图标: {icon_path}")
                        return True
                except Exception as e:
                    debug_print(f"[WARNING] 加载图标失败: {icon_path}, 错误: {e}")
        
        debug_print("[WARNING] 未能设置应用程序图标")
        return False
    except Exception as e:
        debug_print(f"[ERROR] 设置应用程序图标失败: {e}")
        import traceback
        debug_print(traceback.format_exc())
        return False


def check_license_file_exists():
    """检查授权文件是否存在（使用安全多文件模式）"""
    try:
        from license_secure import has_valid_license_secure
        return has_valid_license_secure()
    except Exception as e:
        debug_print(f"[ERROR] 检查授权文件失败: {e}")
        return False


def check_license_valid():
    """检查授权是否有效（使用安全多文件模式）"""
    try:
        from license_secure import check_license_secure
        is_valid, message = check_license_secure()
        return is_valid, message
    except Exception as e:
        debug_print(f"[ERROR] License检查异常: {e}")
        return False, str(e)


def main():
    """主函数"""
    # 设置日志
    logger = setup_logger(
        name="chest_xray_app",
        log_file="logs/app.log",
        level="INFO"
    )
    
    logger.info("=" * 60)
    logger.info("启动胸部X光片智能诊断系统")
    logger.info("=" * 60)
    
    # 创建应用
    app = QApplication(sys.argv)
    app.setApplicationName("胸部X光片智能诊断系统")
    app.setApplicationVersion("1.0.0")
    
    # 设置应用样式
    app.setStyle("Fusion")
    
    # 设置应用程序图标
    set_application_icon(app)
    
    # 检查授权文件是否存在
    has_license_file = check_license_file_exists()
    
    if not has_license_file:
        # 没有授权文件，显示授权对话框，允许试用
        debug_print("[INFO] 未检测到授权文件，显示试用选项")
        from ui.windows.license_dialog import LicenseDialog
        
        # is_trial_expired=False 表示允许继续试用
        license_dialog = LicenseDialog(is_trial_expired=False)
        result = license_dialog.exec_()
        
        # 如果用户点击退出或关闭窗口，则退出程序
        if result == LicenseDialog.Rejected and not license_dialog.is_activated():
            debug_print("[INFO] 用户选择退出程序")
            sys.exit(0)
        
        # 如果激活成功，继续运行
        if license_dialog.is_activated():
            debug_print("[INFO] 授权激活成功")
    else:
        # 有授权文件，检查是否有效
        is_valid, message = check_license_valid()
        
        if not is_valid:
            # 授权无效或过期，必须输入授权码
            debug_print(f"[INFO] 授权无效: {message}")
            from ui.windows.license_dialog import LicenseDialog
            
            # is_trial_expired=True 表示试用期已过，必须激活
            license_dialog = LicenseDialog(is_trial_expired=True)
            result = license_dialog.exec_()
            
            if result != LicenseDialog.Accepted and not license_dialog.is_activated():
                debug_print("[WARNING] 用户未激活授权，退出程序")
                QMessageBox.critical(
                    None, 
                    "授权失败", 
                    f"软件授权已过期或未激活。\n\n{message}\n\n请联系管理员获取授权码。"
                )
                sys.exit(1)
    
    # 重新检查授权状态
    is_valid, _ = check_license_valid()
    if not is_valid and has_license_file:
        QMessageBox.warning(
            None,
            "授权警告",
            "授权验证失败，程序将以受限模式运行。"
        )
    
    # 导入并创建主窗口
    from ui.windows.main_window import MainWindow
    
    window = MainWindow()
    window.showMaximized()  # 最大化显示
    
    logger.info("主窗口显示（最大化模式）")
    
    # 运行应用
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
