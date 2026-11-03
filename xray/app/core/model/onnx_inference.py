"""
ONNX推理模块
"""
import onnxruntime as ort
import numpy as np
import cv2
from typing import List, Dict, Tuple, Optional
from pathlib import Path

from config.settings import INFERENCE_CONFIG, CLASS_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


class ONNXInferenceEngine:
    """
    ONNX推理引擎 - 使用CPU进行推理
    
    功能：
    1. 疾病分类 (14类疾病识别)
    2. 病灶定位 (边界框标注)
    
    输出格式：
    {
        "class_id": int,        # 类别ID
        "class_name": str,      # 类别名称
        "confidence": float,    # 置信度
        "bbox": [x1, y1, x2, y2]  # 边界框坐标
    }
    """
    
    def __init__(
        self,
        model_path: str = None,
        conf_thresh: float = None,
        nms_thresh: float = None,
        providers: List[str] = None
    ):
        """
        初始化推理引擎
        
        Args:
            model_path: ONNX模型路径
            conf_thresh: 置信度阈值
            nms_thresh: NMS阈值
            providers: 执行提供者列表，默认CPU
        """
        self.model_path = model_path or INFERENCE_CONFIG["model_path"]
        self.conf_thresh = conf_thresh or 0.25
        self.nms_thresh = nms_thresh or 0.45
        self.input_size = INFERENCE_CONFIG["input_size"]
        
        # 类别信息
        self.class_names = CLASS_CONFIG["class_names"]
        self.class_names_cn = CLASS_CONFIG["class_names_cn"]
        self.num_classes = CLASS_CONFIG["num_classes"]
        
        # 执行提供者 (默认CPU)
        if providers is None:
            self.providers = ['CPUExecutionProvider']
        else:
            self.providers = providers
        
        # 初始化ONNX Runtime
        self._init_session()
        
        # 生成类别颜色 (用于可视化)
        np.random.seed(42)
        self.colors = np.random.randint(0, 255, size=(self.num_classes, 3), dtype=np.uint8)
        
        logger.info(f"ONNX推理引擎初始化完成，模型: {self.model_path}")
        logger.info(f"置信度阈值: {self.conf_thresh}")
    
    def _init_session(self):
        """初始化ONNX Runtime会话"""
        if not Path(self.model_path).exists():
            raise FileNotFoundError(f"ONNX模型不存在: {self.model_path}")
        
        # 创建会话
        self.session = ort.InferenceSession(
            self.model_path,
            providers=self.providers
        )
        
        # 获取输入输出信息
        self.input_name = self.session.get_inputs()[0].name
        self.input_shape = self.session.get_inputs()[0].shape
        self.output_names = [o.name for o in self.session.get_outputs()]
        
        logger.debug(f"输入名称: {self.input_name}, 形状: {self.input_shape}")
        logger.debug(f"输出名称: {self.output_names}")
    
    def infer(self, input_tensor: np.ndarray, scale: float, pad_info: Tuple[int, int]) -> List[Dict]:
        """
        执行推理
        
        Args:
            input_tensor: 预处理后的图像张量 (1, 3, 640, 640)
            scale: 预处理时的缩放比例
            pad_info: (resized_w, resized_h) 预处理时的resize尺寸
        
        Returns:
            List[Dict]: 检测结果列表
        """
        # 推理
        outputs = self.session.run(self.output_names, {self.input_name: input_tensor})
        
        # 后处理
        detections = self._postprocess(outputs[0], scale, pad_info)
        
        return detections
    
    def _postprocess(self, output: np.ndarray, scale: float, pad_info: Tuple[int, int]) -> List[Dict]:
        """
        后处理 - NMS和坐标还原
        
        正确的坐标还原步骤：
        1. 模型输出是相对于640x640的坐标
        2. 先裁剪到resized区域（去除padding）
        3. 再按scale还原到原始图像尺寸
        
        Args:
            output: 模型原始输出
            scale: 缩放比例
            pad_info: (resized_w, resized_h) 预处理时的resize尺寸
        
        Returns:
            List[Dict]: 处理后的检测结果
        """
        resized_w, resized_h = pad_info
        
        # 解析输出 (YOLOv8格式)
        predictions = np.squeeze(output).T
        
        # 过滤低置信度
        scores = np.max(predictions[:, 4:], axis=1)
        mask = scores > self.conf_thresh
        predictions = predictions[mask]
        scores = scores[mask]
        
        if len(predictions) == 0:
            return []
        
        # 获取类别和置信度
        class_ids = np.argmax(predictions[:, 4:], axis=1)
        confidences = np.max(predictions[:, 4:], axis=1)
        
        # 转换框格式 (xywh -> xyxy)
        boxes = self._xywh2xyxy(predictions[:, :4])
        
        # NMS
        indices = cv2.dnn.NMSBoxes(
            boxes.tolist(),
            confidences.tolist(),
            self.conf_thresh,
            self.nms_thresh
        )
        
        # 构建结果
        detections = []
        if len(indices) > 0:
            for i in indices.flatten():
                box = boxes[i].copy()
                
                # 正确的坐标还原步骤：
                # 1. 先裁剪到resized区域（去除padding）
                # 模型输出是相对于640x640的，但有效区域只有resized_w x resized_h
                box[0] = np.clip(box[0], 0, resized_w)  # x1
                box[1] = np.clip(box[1], 0, resized_h)  # y1
                box[2] = np.clip(box[2], 0, resized_w)  # x2
                box[3] = np.clip(box[3], 0, resized_h)  # y2
                
                # 2. 再还原到原始图像尺寸
                box = box / scale
                box = box.astype(int)
                
                # 确保坐标有效
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                
                detections.append({
                    "class_id": int(class_ids[i]),
                    "class_name": self.class_names[class_ids[i]],
                    "class_name_cn": self.class_names_cn[class_ids[i]],
                    "confidence": float(confidences[i]),
                    "bbox": box.tolist()
                })
        
        # 按置信度排序
        detections.sort(key=lambda x: x["confidence"], reverse=True)
        
        return detections
    
    def _xywh2xyxy(self, x: np.ndarray) -> np.ndarray:
        """
        转换框格式 (xywh -> xyxy)
        
        Args:
            x: 输入框 (N, 4) [x_center, y_center, width, height]
        
        Returns:
            np.ndarray: 输出框 (N, 4) [x1, y1, x2, y2]
        """
        y = np.copy(x)
        y[:, 0] = x[:, 0] - x[:, 2] / 2  # x1
        y[:, 1] = x[:, 1] - x[:, 3] / 2  # y1
        y[:, 2] = x[:, 0] + x[:, 2] / 2  # x2
        y[:, 3] = x[:, 1] + x[:, 3] / 2  # y2
        return y
    
    def get_class_color(self, class_id: int) -> Tuple[int, int, int]:
        """
        获取类别颜色
        
        Args:
            class_id: 类别ID
        
        Returns:
            Tuple[int, int, int]: RGB颜色
        """
        return tuple(int(c) for c in self.colors[class_id % len(self.colors)])
