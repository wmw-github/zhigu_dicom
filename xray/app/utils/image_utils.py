"""
图像处理工具模块
"""
import cv2
import numpy as np
from typing import List, Dict, Tuple


def draw_detections(
    image: np.ndarray,
    detections: List[Dict],
    colors: np.ndarray = None,
    thickness: int = 2,
    font_scale: float = 0.5
) -> np.ndarray:
    """
    在图像上绘制检测结果
    
    Args:
        image: 原始图像 (RGB格式)
        detections: 检测结果列表
        colors: 类别颜色数组
        thickness: 线条粗细
        font_scale: 字体大小
    
    Returns:
        np.ndarray: 绘制后的图像
    """
    result = image.copy()
    
    if colors is None:
        np.random.seed(42)
        colors = np.random.randint(0, 255, size=(100, 3), dtype=np.uint8)
    
    for det in detections:
        x1, y1, x2, y2 = det["bbox"]
        class_id = det["class_id"]
        class_name = det["class_name"]
        confidence = det["confidence"]
        
        # 获取颜色
        color = tuple(int(c) for c in colors[class_id % len(colors)])
        
        # 绘制边界框
        cv2.rectangle(result, (x1, y1), (x2, y2), color, thickness)
        
        # 绘制标签
        label = f"{class_name}: {confidence:.2f}"
        (text_width, text_height), _ = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
        )
        
        # 标签背景
        cv2.rectangle(
            result,
            (x1, y1 - text_height - 10),
            (x1 + text_width, y1),
            color,
            -1
        )
        
        # 标签文字
        cv2.putText(
            result,
            label,
            (x1, y1 - 5),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (255, 255, 255),
            thickness
        )
    
    return result


def resize_image(
    image: np.ndarray,
    target_size: int,
    keep_aspect: bool = True
) -> Tuple[np.ndarray, float]:
    """
    调整图像大小
    
    Args:
        image: 原始图像
        target_size: 目标尺寸
        keep_aspect: 是否保持长宽比
    
    Returns:
        Tuple[np.ndarray, float]: (调整后的图像, 缩放比例)
    """
    h, w = image.shape[:2]
    
    if keep_aspect:
        scale = min(target_size / h, target_size / w)
        new_h, new_w = int(h * scale), int(w * scale)
        resized = cv2.resize(image, (new_w, new_h))
        
        # 填充到目标尺寸
        if len(image.shape) == 3:
            result = np.full((target_size, target_size, 3), 114, dtype=np.uint8)
        else:
            result = np.full((target_size, target_size), 114, dtype=np.uint8)
        
        result[:new_h, :new_w] = resized
        return result, scale
    else:
        resized = cv2.resize(image, (target_size, target_size))
        scale = target_size / max(h, w)
        return resized, scale


def letterbox(
    image: np.ndarray,
    new_shape: Tuple[int, int] = (640, 640),
    color: Tuple[int, int, int] = (114, 114, 114),
    auto: bool = False,
    scale_fill: bool = False,
    scale_up: bool = True
) -> Tuple[np.ndarray, float, Tuple[float, float]]:
    """
    保持长宽比的图像缩放 (YOLO标准实现)
    
    Args:
        image: 原始图像
        new_shape: 目标尺寸
        color: 填充颜色
        auto: 自动调整填充
        scale_fill: 是否拉伸填充
        scale_up: 是否允许放大
    
    Returns:
        Tuple: (处理后的图像, 缩放比例, (填充宽, 填充高))
    """
    shape = image.shape[:2]  # 当前尺寸 [height, width]
    
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    
    # 计算缩放比例
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    if not scale_up:  # 只缩小，不放大
        r = min(r, 1.0)
    
    # 计算填充
    ratio = (r, r)  # 宽高比例相同
    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]
    
    if auto:  # 最小矩形
        dw, dh = np.mod(dw, 64), np.mod(dh, 64)
    elif scale_fill:  # 拉伸
        dw, dh = 0, 0
        new_unpad = (new_shape[1], new_shape[0])
        ratio = (new_shape[1] / shape[1], new_shape[0] / shape[0])
    
    # 均分填充
    dw /= 2
    dh /= 2
    
    # 缩放图像
    if shape[::-1] != new_unpad:
        image = cv2.resize(image, new_unpad, interpolation=cv2.INTER_LINEAR)
    
    # 添加填充
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    image = cv2.copyMakeBorder(
        image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color
    )
    
    return image, r, (dw, dh)
