"""
文件操作工具模块
"""
import shutil
from pathlib import Path
from typing import List, Union


def ensure_dir(path: Union[str, Path]) -> Path:
    """
    确保目录存在，不存在则创建
    
    Args:
        path: 目录路径
    
    Returns:
        Path: 目录路径
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def copy_file(src: Union[str, Path], dst: Union[str, Path]) -> Path:
    """
    复制文件
    
    Args:
        src: 源文件路径
        dst: 目标文件路径
    
    Returns:
        Path: 目标文件路径
    """
    src = Path(src)
    dst = Path(dst)
    ensure_dir(dst.parent)
    shutil.copy2(src, dst)
    return dst


def get_file_list(
    directory: Union[str, Path],
    extensions: List[str] = None,
    recursive: bool = False
) -> List[Path]:
    """
    获取文件列表
    
    Args:
        directory: 目录路径
        extensions: 文件扩展名列表 (如 ['.jpg', '.png'])
        recursive: 是否递归搜索
    
    Returns:
        List[Path]: 文件路径列表
    """
    directory = Path(directory)
    
    if not directory.exists():
        return []
    
    if extensions:
        extensions = [ext.lower() if ext.startswith('.') else f'.{ext.lower()}' 
                     for ext in extensions]
    
    files = []
    
    if recursive:
        pattern = "**/*"
    else:
        pattern = "*"
    
    for file_path in directory.glob(pattern):
        if file_path.is_file():
            if extensions is None or file_path.suffix.lower() in extensions:
                files.append(file_path)
    
    return sorted(files)


def get_image_list(directory: Union[str, Path], recursive: bool = False) -> List[Path]:
    """
    获取图像文件列表
    
    Args:
        directory: 目录路径
        recursive: 是否递归搜索
    
    Returns:
        List[Path]: 图像文件路径列表
    """
    extensions = ['.jpg', '.jpeg', '.png', '.bmp', '.dcm', '.dicom', '.tiff', '.webp']
    return get_file_list(directory, extensions, recursive)
