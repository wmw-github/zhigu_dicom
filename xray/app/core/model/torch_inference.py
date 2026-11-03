"""
PyTorch推理模块 - 用于替代ONNX Runtime
兼容性更好，适合Windows 7等旧系统

使用方法：
1. 需要安装: torch, torchvision
2. 模型格式: .pt (PyTorch)
3. 自动检测并使用可用的后端
"""
import numpy as np
import cv2
from typing import List, Dict, Tuple, Optional
from pathlib import Path

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

from config.settings import INFERENCE_CONFIG, CLASS_CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)


class TorchInferenceEngine:
    """
    PyTorch推理引擎 - 使用CPU进行推理
    
    功能：
    1. 疾病分类 (14类疾病识别)
    2. 病灶定位 (边界框标注)
    
    优势：
    - 不依赖onnxruntime，兼容性更好
    - PyTorch在Win7上更稳定
    - 可以直接加载训练好的.pt模型
    
    输出格式：
    {
        "class_id": int,
        "class_name": str,
        "confidence": float,
        "bbox": [x1, y1, x2, y2]
    }
    """
    
    def __init__(
        self,
        model_path: str = None,
        conf_thresh: float = None,
        nms_thresh: float = None,
        device: str = 'cpu'
    ):
        """
        初始化PyTorch推理引擎
        
        Args:
            model_path: PyTorch模型路径 (.pt文件)
            conf_thresh: 置信度阈值
            nms_thresh: NMS阈值
            device: 设备 ('cpu' 或 'cuda')
        """
        if not TORCH_AVAILABLE:
            raise ImportError("PyTorch未安装！请运行: pip install torch torchvision")
        
        # 模型路径（自动查找.pt或.onnx）
        self.model_path = model_path or self._find_model_path()
        
        if not Path(self.model_path).exists():
            raise FileNotFoundError(f"模型不存在: {self.model_path}")
            
        self.conf_thresh = conf_thresh or INFERENCE_CONFIG.get("conf_thresh", 0.25)
        self.nms_thresh = nms_thresh or INFERENCE_CONFIG.get("nms_thresh", 0.45)
        self.input_size = INFERENCE_CONFIG["input_size"]
        self.device = torch.device(device if torch.cuda.is_available() else 'cpu')
        
        # 类别信息
        self.class_names = CLASS_CONFIG["class_names"]
        self.class_names_cn = CLASS_CONFIG["class_names_cn"]
        self.num_classes = CLASS_CONFIG["num_classes"]
        
        # 加载模型
        self._load_model()
        
        # 生成类别颜色
        np.random.seed(42)
        self.colors = np.random.randint(0, 255, size=(self.num_classes, 3), dtype=np.uint8)
        
        logger.info(f"PyTorch推理引擎初始化完成")
        logger.info(f"  模型: {self.model_path}")
        logger.info(f"  设备: {self.device}")
        logger.info(f"  置信度阈值: {self.conf_thresh}")
    
    def _find_model_path(self) -> str:
        """自动查找模型文件"""
        base_path = Path(__file__).parent.parent.parent / "models" / "trained"
        
        # 优先查找.pt文件
        pt_models = list(base_path.glob("*.pt"))
        if pt_models:
            return str(pt_models[0])
        
        # 备选：查找.onnx文件（需要转换）
        onnx_models = list(base_path.glob("*.onnx"))
        if onnx_models:
            logger.warning("只找到ONNX模型，建议转换为PT格式以获得更好兼容性")
            return str(onnx_models[0])
        
        # 默认路径
        return INFERENCE_CONFIG["model_path"]
    
    def _load_model(self):
        """加载PyTorch模型"""
        try:
            # 尝试加载为YOLOv8格式（ultralytics）
            from ultralytics import YOLO
            
            self.model = YOLO(self.model_path)
            self.model_type = 'yolov8'
            logger.info(f"  模型类型: YOLOv8 (ultralytics)")
            
        except ImportError:
            # 如果没有ultralytics，尝试通用PyTorch加载
            logger.info("ultralytics未安装，尝试通用PyTorch加载...")
            self._load_generic_pytorch()
    
    def _load_generic_pytorch(self):
        """通用PyTorch模型加载"""
        try:
            checkpoint = torch.load(
                self.model_path,
                map_location=self.device,
                weights_only=False
            )
            
            if isinstance(checkpoint, dict) and 'model' in checkpoint:
                self.model = checkpoint['model']
            else:
                self.model = checkpoint
            
            self.model.eval()
            self.model.to(self.device)
            self.model_type = 'generic'
            
            logger.info(f"  模型类型: 通用PyTorch")
            
        except Exception as e:
            raise RuntimeError(f"无法加载PyTorch模型: {e}\n请确保模型文件有效")
    
    def infer(self, input_tensor: np.ndarray, scale: float, pad_info: Tuple[int, int]) -> List[Dict]:
        """
        执行推理
        
        Args:
            input_tensor: 预处理后的图像张量 (1, 3, 640, 640)
            scale: 缩放比例
            pad_info: (resized_w, resized_h)
        
        Returns:
            List[Dict]: 检测结果列表
        """
        with torch.no_grad():
            if self.model_type == 'yolov8':
                results = self._infer_yolov8(input_tensor, scale, pad_info)
            else:
                results = self._infer_generic(input_tensor, scale, pad_info)
        
        return results
    
    def _infer_yolov8(self, input_tensor: np.ndarray, scale: float, 
                       pad_info: Tuple[int, int]) -> List[Dict]:
        """YOLOv8模型推理"""
        resized_w, resized_h = pad_info
        
        # 转换为PIL Image（YOLOv8需要）
        img_np = ((input_tensor[0].transpose(1, 2, 0) + 1) * 127.5).astype(np.uint8)
        
        # 推理
        results = self.model(
            img_np,
            conf=self.conf_thresh,
            iou=self.nms_thresh,
            verbose=False
        )
        
        # 解析结果
        detections = []
        for result in results:
            boxes = result.boxes
            
            if boxes is None or len(boxes) == 0:
                continue
            
            for box in boxes:
                xyxy = box.xyxy[0].cpu().numpy()  # [x1, y1, x2, y2]
                conf = float(box.conf[0])
                cls_id = int(box.cls[0])
                
                # 坐标还原
                x1 = max(0, min(xyxy[0], resized_w))
                y1 = max(0, min(xyxy[1], resized_h))
                x2 = max(0, min(xyxy[2], resized_w))
                y2 = max(0, min(xyxy[3], resized_h))
                
                # 还原到原始尺寸
                x1, y1, x2, y2 = [int(c / scale) for c in [x1, y1, x2, y2]]
                
                if x2 <= x1 or y2 <= y1:
                    continue
                
                cls_name = self.class_names[cls_id] if cls_id < len(self.class_names) else f"class_{cls_id}"
                cls_name_cn = self.class_names_cn[cls_id] if cls_id < len(self.class_names_cn) else f"类别{cls_id}"
                
                detections.append({
                    "class_id": cls_id,
                    "class_name": cls_name,
                    "class_name_cn": cls_name_cn,
                    "confidence": round(conf, 4),
                    "bbox": [int(x1), int(y1), int(x2), int(y2)]
                })
        
        # 按置信度排序
        detections.sort(key=lambda x: x["confidence"], reverse=True)
        
        return detections
    
    def _infer_generic(self, input_tensor: np.ndarray, scale: float,
                        pad_info: Tuple[int, int]) -> List[Dict]:
        """通用PyTorch模型推理"""
        # 转换为tensor
        tensor = torch.from_numpy(input_tensor).float().to(self.device)
        
        # 前向传播
        outputs = self.model(tensor)
        
        if isinstance(outputs, torch.Tensor):
            output = outputs.cpu().numpy()
        elif isinstance(outputs, (list, tuple)):
            output = outputs[0].cpu().numpy() if len(outputs) > 0 else None
        elif isinstance(outputs, dict):
            output = list(outputs.values())[0].cpu().numpy() if outputs else None
        else:
            output = outputs
        
        if output is None:
            return []
        
        # 后处理（与ONNX版本相同）
        return self._postprocess(output, scale, pad_info)
    
    def _postprocess(self, output: np.ndarray, scale: float, 
                      pad_info: Tuple[int, int]) -> List[Dict]:
        """
        后处理 - NMS和坐标还原
        
        与ONNX版本保持一致的后处理逻辑
        """
        resized_w, resized_h = pad_info
        
        # 解析输出
        predictions = np.squeeze(output).T
        
        if predictions.ndim == 1:
            predictions = predictions.reshape(-1, len(predictions))
        
        if predictions.shape[0] == 0 or predictions.shape[1] < 5:
            return []
        
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
        
        # 转换框格式
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
                
                # 坐标裁剪和还原
                box[0] = np.clip(box[0], 0, resized_w)
                box[1] = np.clip(box[1], 0, resized_h)
                box[2] = np.clip(box[2], 0, resized_w)
                box[3] = np.clip(box[3], 0, resized_h)
                
                box = box / scale
                box = box.astype(int)
                
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                
                cls_id = int(class_ids[i])
                cls_name = self.class_names[cls_id] if cls_id < len(self.class_names) else f"class_{cls_id}"
                cls_name_cn = self.class_names_cn[cls_id] if cls_id < len(self.class_names_cn) else f"类别{cls_id}"
                
                detections.append({
                    "class_id": cls_id,
                    "class_name": cls_name,
                    "class_name_cn": cls_name_cn,
                    "confidence": float(confidences[i]),
                    "bbox": box.tolist()
                })
        
        # 排序
        detections.sort(key=lambda x: x["confidence"], reverse=True)
        
        return detections
    
    def _xywh2xyxy(self, x: np.ndarray) -> np.ndarray:
        """转换框格式 (xywh -> xyxy)"""
        y = np.copy(x)
        y[:, 0] = x[:, 0] - x[:, 2] / 2  # x1
        y[:, 1] = x[:, 1] - x[:, 3] / 2  # y1
        y[:, 2] = x[:, 0] + x[:, 2] / 2  # x2
        y[:, 3] = x[:, 1] + x[:, 3] / 2  # y2
        return y
    
    def get_class_color(self, class_id: int) -> Tuple[int, int, int]:
        """获取类别颜色"""
        return tuple(int(c) for c in self.colors[class_id % len(self.colors)])
