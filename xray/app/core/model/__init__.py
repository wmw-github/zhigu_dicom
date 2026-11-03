"""
模型模块 - 支持ONNX和PyTorch双后端

自动检测可用的推理后端：
1. 优先使用 ONNX Runtime (Win7/Win10/Win11 通用)
2. 如果不可用，回退到 PyTorch

Windows 7兼容性说明：
- Win7 必须使用 ONNX Runtime 1.12.1（最后支持 Win7 SP1 的版本）
- ONNX 模型由 Win10 上的 export_models_to_onnx.py 预先导出
- 不要在 Win7 上安装 torch/ultralytics（兼容性极差）
- 骨龄/骨折模型已使用纯 ONNX，胸片模型也走 ONNX

使用方法:
    from core.model import get_inference_engine
    
    # 自动选择最佳后端
    engine = get_inference_engine()
    
    # 或指定后端
    engine = get_inference_engine(backend='onnx')
"""
import sys
import platform
from typing import Optional

# onnxruntime 加载失败时的真实原因。**绝不能吞掉**：
# 空 except 会让「版本不支持 Win7」「DLL 缺失」「依赖缺失」三类完全不同的
# 问题统统伪装成「未安装」，排查时毫无线索（2026-10 实测踩过）。
# 上层（xray_bridge）会把这里的字符串原样显示给用户/写进日志。
_ORT_IMPORT_ERROR = ""


def _describe_ort_failure(exc: BaseException) -> str:
    """把 onnxruntime 的加载异常翻译成一句能直接照做的中文提示。"""
    text = "%s: %s" % (type(exc).__name__, exc)
    low = text.lower()
    if "dll load failed" in low or "winerror 126" in low or "winerror 127" in low:
        return ("onnxruntime 的 DLL 加载失败（%s）。\n"
                "  · Windows 7 必须用 onnxruntime==1.12.1，"
                "1.15 及以上已放弃 Win7 支持；\n"
                "  · 若版本正确仍报错，检查 pyenv\\Lib\\site-packages\\onnxruntime\\capi\\"
                "三个二进制文件是否齐全。" % text)
    if is_windows7():
        return ("onnxruntime 加载失败（%s）。\n"
                "  当前系统是 Windows 7，仅 onnxruntime<=1.12.1 可用。" % text)
    return "onnxruntime 加载失败：%s" % text


def is_windows7() -> bool:
    """
    检查是否为Windows 7系统
    
    Returns:
        bool: 是否为Win7
    """
    try:
        if platform.system() != 'Windows':
            return False
        release = platform.release()
        version = platform.version()
        # Win7: release='7' 或 version.startswith('6.1')
        return release == '7' or version.startswith('6.1')
    except:
        return False


def check_onnx_available() -> bool:
    """
    检查ONNX Runtime是否可用

    失败时把真实原因记进模块级 ``_ORT_IMPORT_ERROR``，供调用方给出
    可操作的提示（而不是笼统的「未安装」）。

    Returns:
        bool: 是否可用
    """
    global _ORT_IMPORT_ERROR
    _ORT_IMPORT_ERROR = ""
    try:
        import onnxruntime as ort
        # 某些精简版 Win7 上新版 ORT 能 import 却一建会话就崩，
        # 所以这里额外做一次版本护栏：Win7 只认 1.12.1。
        if is_windows7():
            ver = getattr(ort, "__version__", "")
            try:
                major = int(str(ver).split(".")[0])
                minor = int(str(ver).split(".")[1])
            except (ValueError, IndexError):
                major, minor = 99, 99
            if (major, minor) > (1, 12):
                _ORT_IMPORT_ERROR = (
                    "Win7 上检测到 onnxruntime %s，该版本不支持 Win7。\n"
                    "  onnxruntime 从 1.15 起已放弃 Win7 支持，"
                    "请降级到 1.12.1。" % (ver or "未知版本"))
                return False
        return True
    except BaseException as e:
        # 用 BaseException：Win7 上 DLL 加载失败可能抛 OSError，
        # 而 pybind11 状态异常时偶尔是别的异常类型，一条都不能漏。
        _ORT_IMPORT_ERROR = _describe_ort_failure(e)
        return False


def check_torch_available() -> bool:
    """
    检查PyTorch是否可用
    
    Returns:
        bool: 是否可用
    """
    # Win7 上直接返回 False，避免尝试加载 torch DLL
    if is_windows7():
        return False
    try:
        import torch
        return True
    except (ImportError, OSError):
        return False


def get_inference_engine(
    backend: Optional[str] = None,
    model_path: str = None,
    **kwargs
):
    """
    获取推理引擎（自动检测最佳后端）
    
    Args:
        backend: 指定后端 ('onnx' 或 'torch')，None表示自动选择
        model_path: 模型路径
        **kwargs: 其他参数传递给引擎构造函数
    
    Returns:
        ONNXInferenceEngine 或 TorchInferenceEngine 实例
    
    使用示例:
        >>> # 自动选择（所有平台优先ONNX）
        >>> engine = get_inference_engine()
        
        >>> # 强制使用指定后端
        >>> engine = get_inference_engine(backend='torch')
        
        >>> # 带参数
        >>> engine = get_inference_engine(conf_thresh=0.3)
    """
    if backend is None:
        # 自动检测：优先ONNX（Win7/Win10/Win11通用），回退到PyTorch
        if check_onnx_available():
            backend = 'onnx'
        elif check_torch_available():
            backend = 'torch'
        else:
            detail = _ORT_IMPORT_ERROR or ""
            if not detail and not check_torch_available():
                detail = "onnxruntime 与 torch 均不可用"
            raise ImportError(
                "未找到可用的推理后端！\n\n"
                "%s\n\n"
                "  修复方式（Windows 7）：\n"
                "  pyenv\\python.exe -m pip install "
                "onnxruntime==1.12.1 --no-deps --force-reinstall" % detail
            )
    
    # 根据选择的backend创建对应引擎
    if backend == 'onnx':
        if not check_onnx_available():
            detail = _ORT_IMPORT_ERROR or "未知原因"
            raise ImportError(
                "ONNX Runtime 未安装或无法加载！\n\n"
                "%s\n\n"
                "  修复方式（Windows 7）：\n"
                "  pyenv\\python.exe -m pip install "
                "onnxruntime==1.12.1 --no-deps --force-reinstall\n"
                "  仓库内已有离线包：xray\\wheels_win7\\"
                "onnxruntime-1.12.1-cp38-cp38-win_amd64.whl" % detail
            )
        
        from .onnx_inference import ONNXInferenceEngine
        return ONNXInferenceEngine(model_path=model_path, **kwargs)
    
    elif backend == 'torch':
        if not check_torch_available():
            raise ImportError(
                "PyTorch未安装！\n"
                "请运行: pip install torch torchvision\n"
                "或使用ONNX后端: pip install onnxruntime==1.12.1\n"
            )
        
        from .torch_inference import TorchInferenceEngine
        
        # PT模型的默认路径
        if model_path is None:
            import os
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            models_dir = os.path.join(base_dir, '..', 'models', 'trained')
            
            pt_models = [f for f in os.listdir(models_dir) if f.endswith('.pt')]
            
            if pt_models:
                model_path = os.path.join(models_dir, pt_models[0])
            else:
                raise FileNotFoundError(
                    "找不到PT模型文件！\n"
                    f"请在 {models_dir} 目录中放置 .pt 模型文件。"
                )
        
        return TorchInferenceEngine(model_path=model_path, **kwargs)
    
    else:
        raise ValueError(f"不支持的后端类型: {backend}，可选值: 'onnx', 'torch', 或 None (自动)")


# 导出所有引擎类
try:
    from .onnx_inference import ONNXInferenceEngine
except ImportError:
    ONNXInferenceEngine = None

# Win7 及 torch 不可用时，跳过 TorchInferenceEngine 导入
# torch 虽然可能安装了但 DLL 加载会报 OSError，需要同时捕获
TorchInferenceEngine = None
if not is_windows7():
    try:
        from .torch_inference import TorchInferenceEngine
    except (ImportError, OSError):
        TorchInferenceEngine = None

__all__ = [
    'get_inference_engine',
    'check_onnx_available',
    'check_torch_available',
    'is_windows7',
]

if ONNXInferenceEngine:
    __all__.append('ONNXInferenceEngine')

if TorchInferenceEngine:
    __all__.append('TorchInferenceEngine')


# 兼容性别名（保持向后兼容）
def InferenceEngine(*args, **kwargs):
    """向后兼容的工厂函数，自动选择后端"""
    return get_inference_engine(*args, **kwargs)

__all__.append('InferenceEngine')
