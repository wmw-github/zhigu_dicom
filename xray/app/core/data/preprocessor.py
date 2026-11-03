"""
图像预处理模块
"""
import cv2
import numpy as np
from pathlib import Path
from typing import Tuple, Optional, Union

from config.settings import SUPPORTED_FORMATS
from core.data.dicom_loader import DicomLoader
from utils.image_io import imread_cn


class ImagePreprocessor:
    """图像预处理器 - 支持多种医学影像格式"""
    
    def __init__(self, target_size: int = 640):
        """
        初始化预处理器
        
        Args:
            target_size: 目标图像尺寸
        """
        self.target_size = target_size
        self.dicom_loader = DicomLoader()
    
    def load_image(self, image_path: Union[str, Path]) -> Tuple[np.ndarray, Optional[dict]]:
        """
        加载图像 (支持DICOM和标准格式)
        
        Args:
            image_path: 图像路径
        
        Returns:
            Tuple: (RGB格式图像数组, 元数据)
        """
        path = Path(image_path)
        ext = path.suffix.lower()
        
        if ext in SUPPORTED_FORMATS["dicom"]:
            return self.dicom_loader.load(path)
        else:
            return self._load_standard(path)
    
    def _load_standard(self, path: Path) -> Tuple[np.ndarray, None]:
        """加载标准图像格式"""
        image = imread_cn(str(path))
        if image is None:
            raise ValueError(f"无法加载图像: {path}")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        return image, None
    
    def preprocess(self, image: np.ndarray) -> Tuple[np.ndarray, float, Tuple[int, int]]:
        """
        预处理图像
        
        Args:
            image: 原始图像
        
        Returns:
            Tuple: (预处理后的图像张量, 缩放比例, (原始resize后的宽高))
        """
        # 医学影像归一化
        image = self._medical_normalize(image)
        
        # 保持长宽比resize
        h, w = image.shape[:2]
        scale = min(self.target_size / h, self.target_size / w)
        new_h, new_w = int(h * scale), int(w * scale)
        
        resized = cv2.resize(image, (new_w, new_h))
        
        # 填充到目标尺寸
        padded = np.full((self.target_size, self.target_size, 3), 114, dtype=np.uint8)
        padded[:new_h, :new_w] = resized
        
        # 归一化到0-1
        tensor = padded.astype(np.float32) / 255.0
        
        # HWC to CHW
        tensor = tensor.transpose(2, 0, 1)
        
        # Add batch dimension
        tensor = np.expand_dims(tensor, axis=0)
        
        return tensor, scale, (new_w, new_h)
    
    def _medical_normalize(self, image: np.ndarray) -> np.ndarray:
        """医学影像专用归一化 (2-98百分位数)"""
        if image.dtype != np.uint8:
            p2, p98 = np.percentile(image, (2, 98))
            image = np.clip(image, p2, p98)
            image = ((image - p2) / (p98 - p2) * 255).astype(np.uint8)
        return image
    
    def preprocess_for_training(self, image: np.ndarray) -> np.ndarray:
        """
        训练时的预处理 (不归一化到0-1，保持0-255)
        
        Args:
            image: 原始图像
        
        Returns:
            np.ndarray: 预处理后的图像
        """
        # 医学影像归一化
        image = self._medical_normalize(image)
        
        # 保持长宽比resize
        h, w = image.shape[:2]
        scale = min(self.target_size / h, self.target_size / w)
        new_h, new_w = int(h * scale), int(w * scale)
        
        resized = cv2.resize(image, (new_w, new_h))
        
        # 填充到目标尺寸
        padded = np.full((self.target_size, self.target_size, 3), 114, dtype=np.uint8)
        padded[:new_h, :new_w] = resized
        
        return padded
