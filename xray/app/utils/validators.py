"""
数据验证模块
"""
from pathlib import Path
from typing import Union, Tuple
from config.settings import SUPPORTED_FORMATS


def validate_image_path(image_path: Union[str, Path]) -> Tuple[bool, str]:
    """
    验证图像路径
    
    Args:
        image_path: 图像路径
    
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    path = Path(image_path)
    
    # 检查文件是否存在
    if not path.exists():
        return False, f"文件不存在: {path}"
    
    # 检查是否为文件
    if not path.is_file():
        return False, f"路径不是文件: {path}"
    
    # 检查扩展名
    ext = path.suffix.lower()
    valid_extensions = (SUPPORTED_FORMATS["dicom"] + 
                       SUPPORTED_FORMATS["standard"])
    
    if ext not in valid_extensions:
        return False, f"不支持的文件格式: {ext}"
    
    return True, ""


def validate_model_path(model_path: Union[str, Path]) -> Tuple[bool, str]:
    """
    验证模型路径
    
    Args:
        model_path: 模型路径
    
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    path = Path(model_path)
    
    # 检查文件是否存在
    if not path.exists():
        return False, f"模型文件不存在: {path}"
    
    # 检查扩展名
    ext = path.suffix.lower()
    valid_extensions = ['.pt', '.pth', '.onnx', '.engine', '.tflite']
    
    if ext not in valid_extensions:
        return False, f"不支持的模型格式: {ext}"
    
    return True, ""


def validate_dataset_structure(data_dir: Union[str, Path]) -> Tuple[bool, str]:
    """
    验证数据集结构
    
    Args:
        data_dir: 数据目录
    
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    data_dir = Path(data_dir)
    
    # 检查必要目录
    required_dirs = [
        data_dir / "images" / "train",
        data_dir / "images" / "val",
        data_dir / "labels" / "train",
        data_dir / "labels" / "val",
    ]
    
    for dir_path in required_dirs:
        if not dir_path.exists():
            return False, f"缺少目录: {dir_path}"
    
    return True, ""
