"""
GUI程序入口

胸部X光片智能诊断系统

Windows 7兼容性说明：
- 使用ONNX Runtime 1.12.1作为推理后端（最后支持Win7 SP1的版本）
- 所有模型预先导出为ONNX格式（在Win10上运行export_models_to_onnx.py）
- 不要在Win7上安装torch/ultralytics
"""
import sys
import platform
from pathlib import Path
from importlib.abc import MetaPathFinder, Loader
from importlib.machinery import ModuleSpec


def check_license_file_exists():
    """检查授权文件是否存在（使用安全多文件模式）"""
    try:
        from license_secure import has_valid_license_secure
        return has_valid_license_secure()
    except Exception as e:
        return False


def check_license_valid():
    """检查授权是否有效（使用安全多文件模式）"""
    try:
        from license_secure import check_license_secure
        is_valid, message = check_license_secure()
        return is_valid, message
    except Exception as e:
        return False, str(e)

# 修补 cv2 版本兼容性问题
# opencv-python 4.5.5(.pyd)与新版本typing stubs(4.6+)共存时,
# cv2.typing引用cv2.dnn.DictValue但旧版pyd无此属性, 导致import cv2崩溃
# 解决方案: 用meta path finder拦截cv2.typing的导入, 返回空模块


class _Cv2TypingCompatLoader(Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        pass


class _Cv2CompatFinder(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith('cv2.typing'):
            return ModuleSpec('cv2.typing', _Cv2TypingCompatLoader())
        return None


sys.meta_path.insert(0, _Cv2CompatFinder())

# 抑制 ONNX Runtime 在 Win7 上的版本警告
# ONNX Runtime 1.12.1 虽然提示不支持Win7，但实际功能正常
import warnings
warnings.filterwarnings('ignore', message='Unsupported Windows version')

# 抑制 OpenCV 内部的 WARN（中文路径每次都会触发，无实际影响）
# 各模块通过 utils.image_io.imread_cn/imwrite_cn 处理中文路径
import os
import cv2
os.environ["OPENCV_LOG_LEVEL"] = "ERROR"
cv2.setLogLevel(0)  # 0 = silent

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTextCodec
from ui.windows.main_window import MainWindow
from utils.logger import setup_logger

# Win7 中文编码修复：确保 Qt 窗口标题中文字符正常显示
QTextCodec.setCodecForLocale(QTextCodec.codecForName('UTF-8'))


def is_windows7():
    """检查是否为Windows 7系统"""
    try:
        if platform.system() != 'Windows':
            return False
        release = platform.release()
        version = platform.version()
        return release == '7' or version.startswith('6.1')
    except:
        return False


class ModelWarmupThread(QThread):
    """模型预热线程 - Win7兼容版本"""
    finished = pyqtSignal()
    progress = pyqtSignal(str)
    
    def run(self):
        try:
            self.progress.emit("正在预热胸片检测模型...")
            QApplication.processEvents()
            
            # 导入模型模块
            from core.model import (
                get_inference_engine, 
                check_onnx_available, 
                is_windows7
            )
            
            # 检测系统类型
            if is_windows7():
                self.progress.emit("[Win7模式] 使用ONNX Runtime...")
            else:
                onnx_ok = check_onnx_available()
                if onnx_ok:
                    self.progress.emit("检测到 ONNX Runtime，将优先使用...")
                else:
                    self.progress.emit("警告：未检测到推理后端！")
            
            QApplication.processEvents()
            
            # 胸片模型会在MainWindow初始化时自动加载
            
            # 预热骨龄模型（如果可用）
            self.progress.emit("正在预热骨龄检测模型...")
            QApplication.processEvents()
            
            try:
                from core.model.boneage_inference import get_boneage_inference
                boneage_detector = get_boneage_inference()
                if boneage_detector:
                    boneage_detector.warmup()
                    self.progress.emit("✅ 骨龄模型预热完成")
                else:
                    self.progress.emit("⚠️ 骨龄模型未加载")
            except Exception as e:
                self.progress.emit(f"⚠️ 骨龄模型预热失败: {str(e)[:50]}")
            
            QApplication.processEvents()
            
            # 预热骨折检测模型
            self.progress.emit("正在预热骨折检测模型...")
            QApplication.processEvents()
            
            try:
                from core.model.fracture_inference import get_fracture_inference
                fracture_detector = get_fracture_inference()
                if fracture_detector:
                    fracture_detector.warmup()
                    self.progress.emit("✅ 骨折模型预热完成")
                else:
                    self.progress.emit("⚠️ 骨折模型未加载")
            except Exception as e:
                self.progress.emit(f"⚠️ 骨折模型预热失败: {str(e)[:50]}")
            
            # 预热脊柱检测模型
            self.progress.emit("正在预热脊柱检测模型...")
            QApplication.processEvents()
            
            try:
                from core.model.spine_inference import get_spine_inference
                spine_detector = get_spine_inference()
                if spine_detector:
                    spine_detector.warmup()
                    self.progress.emit("✅ 脊柱模型预热完成")
                else:
                    self.progress.emit("⚠️ 脊柱模型未加载")
            except Exception as e:
                self.progress.emit(f"⚠️ 脊柱模型预热失败: {str(e)[:50]}")
            
            QApplication.processEvents()
            self.progress.emit("✅ 所有模型预热完成！")
                
        except Exception as e:
            self.progress.emit(f"⚠️ 模型预热失败: {str(e)[:50]}")
            import traceback
            traceback.print_exc()
        
        self.finished.emit()


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
    
    # 创建应用（授权对话框需要）
    app = QApplication(sys.argv)
    app.setApplicationName("胸部X光片智能诊断系统")
    app.setApplicationVersion("1.0.0")
    
    # 设置应用样式
    app.setStyle("Fusion")
    
    # ========== 授权验证 ==========
    has_license_file = check_license_file_exists()
    
    if not has_license_file:
        # 没有授权文件，显示授权对话框，允许试用
        logger.info("未检测到授权文件，显示试用选项")
        from ui.windows.license_dialog import LicenseDialog
        
        # is_trial_expired=False 表示允许继续试用
        license_dialog = LicenseDialog(is_trial_expired=False)
        result = license_dialog.exec_()
        
        # 如果用户点击退出或关闭窗口，则退出程序
        if result == LicenseDialog.Rejected and not license_dialog.is_activated():
            logger.info("用户选择退出程序")
            sys.exit(0)
        
        # 如果激活成功，继续运行
        if license_dialog.is_activated():
            logger.info("授权激活成功")
    else:
        # 有授权文件，检查是否有效
        is_valid, message = check_license_valid()
        
        if not is_valid:
            # 授权无效或过期，必须输入授权码
            logger.info(f"授权无效: {message}")
            from ui.windows.license_dialog import LicenseDialog
            from PyQt5.QtWidgets import QMessageBox
            
            # is_trial_expired=True 表示试用期已过，必须激活
            license_dialog = LicenseDialog(is_trial_expired=True)
            result = license_dialog.exec_()
            
            if result != LicenseDialog.Accepted and not license_dialog.is_activated():
                logger.warning("用户未激活授权，退出程序")
                QMessageBox.critical(
                    None, 
                    "授权失败", 
                    f"软件授权已过期或未激活。\n\n{message}\n\n请联系管理员获取授权码。"
                )
                sys.exit(1)
    
    # 重新检查授权状态
    is_valid, _ = check_license_valid()
    if not is_valid and has_license_file:
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.warning(
            None,
            "授权警告",
            "授权验证失败，程序将以受限模式运行。"
        )
    # ========== 授权验证结束 ==========
    
    # 创建主窗口
    window = MainWindow()
    window.showMaximized()
    
    logger.info("主窗口显示（最大化模式）")
    
    # 后台预热模型（不阻塞UI，但显示进度）
    warmup_thread = ModelWarmupThread()
    warmup_thread.progress.connect(
        lambda msg: window.statusbar.showMessage(f"🔄 {msg}", 3000)
    )
    warmup_thread.finished.connect(
        lambda: logger.info("模型预热完成") or 
                 window.statusbar.showMessage("✅ 所有模型已就绪，可以开始检测", 5000)
    )
    warmup_thread.start()
    
    # 运行应用
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
