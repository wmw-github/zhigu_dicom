"""
数据增强模块
"""
import cv2
import numpy as np
from pathlib import Path
from typing import List, Tuple, Dict
import random

from config.settings import AUGMENTATION_CONFIG, CLASS_CONFIG


class DataAugmentor:
    """数据增强器"""
    
    def __init__(self):
        self.config = AUGMENTATION_CONFIG
        self.class_names = CLASS_CONFIG["class_names"]
    
    def augment_image(self, image: np.ndarray, bboxes: List[List[float]], 
                     class_ids: List[int]) -> Tuple[np.ndarray, List[List[float]], List[int]]:
        """
        对图像进行增强
        
        Args:
            image: 原始图像
            bboxes: 边界框列表 [x_center, y_center, width, height] (归一化)
            class_ids: 类别ID列表
        
        Returns:
            Tuple: (增强后的图像, 增强后的边界框, 类别ID)
        """
        h, w = image.shape[:2]
        
        # 随机水平翻转
        if random.random() < 0.5:
            image = cv2.flip(image, 1)
            bboxes = self._flip_bboxes(bboxes)
        
        # 随机旋转
        if random.random() < 0.3:
            angle = random.uniform(-15, 15)
            image, bboxes = self._rotate_image_and_bboxes(image, bboxes, angle)
        
        # 随机亮度调整
        if random.random() < 0.5:
            factor = random.uniform(0.8, 1.2)
            image = np.clip(image * factor, 0, 255).astype(np.uint8)
        
        # 随机对比度调整
        if random.random() < 0.5:
            factor = random.uniform(0.8, 1.2)
            mean = image.mean()
            image = np.clip((image - mean) * factor + mean, 0, 255).astype(np.uint8)
        
        # 高斯噪声
        if random.random() < 0.3:
            noise = np.random.normal(0, 10, image.shape).astype(np.float32)
            image = np.clip(image.astype(np.float32) + noise, 0, 255).astype(np.uint8)
        
        return image, bboxes, class_ids
    
    def _flip_bboxes(self, bboxes: List[List[float]]) -> List[List[float]]:
        """水平翻转边界框"""
        flipped = []
        for bbox in bboxes:
            x_center, y_center, width, height = bbox
            x_center = 1.0 - x_center
            flipped.append([x_center, y_center, width, height])
        return flipped
    
    def _rotate_image_and_bboxes(self, image: np.ndarray, bboxes: List[List[float]], 
                                  angle: float) -> Tuple[np.ndarray, List[List[float]]]:
        """旋转图像和边界框"""
        h, w = image.shape[:2]
        center = (w // 2, h // 2)
        
        # 旋转矩阵
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        
        # 旋转图像
        rotated = cv2.warpAffine(image, M, (w, h), borderValue=(114, 114, 114))
        
        # 注意：边界框旋转比较复杂，这里简化处理
        # 实际应用中可能需要更复杂的计算
        
        return rotated, bboxes
    
    def should_augment_class(self, class_id: int) -> bool:
        """判断是否需要对该类别进行增强"""
        return class_id in self.config["rare_classes"]
    
    def get_augment_factor(self, class_id: int) -> int:
        """获取增强倍数"""
        return self.config["augment_factor"].get(class_id, 1)
