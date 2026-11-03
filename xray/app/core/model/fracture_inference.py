"""
骨折检测推理模块 - ONNX 版本（Win7 兼容）

基于 YOLOv8 的 X 光片骨折自动检测
模型由 Win10 上的 export_models_to_onnx.py 预先导出为 ONNX 格式
支持单类检测：fracture（骨折）
"""

import os
import cv2
import numpy as np
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple

from utils.image_io import imread_cn

logger = logging.getLogger(__name__)

# 模型路径配置
FRACTURE_MODEL_DIR = Path(__file__).parent.parent.parent / "models"
FRACTURE_MODEL_PATH_ONNX = FRACTURE_MODEL_DIR / "best_fracture_yolov8dle.onnx"
# 保留 PT 路径作为备用提示（Win10 可运行，Win7 不行）
FRACTURE_MODEL_PATH_PT = FRACTURE_MODEL_DIR / "best_fracture_yolov8dle.pt"


class FractureDetection:
    """单个骨折检测结果"""
    def __init__(self, box: Tuple[int, int, int, int], confidence: float,
                 class_name: str = "fracture", class_id: int = 0):
        self.box = box  # (x1, y1, x2, y2)
        self.confidence = confidence
        self.class_name = class_name
        self.class_id = class_id

    @property
    def x1(self): return self.box[0]
    @property
    def y1(self): return self.box[1]
    @property
    def x2(self): return self.box[2]
    @property
    def y2(self): return self.box[3]
    @property
    def cx(self): return (self.box[0] + self.box[2]) // 2
    @property
    def cy(self): return (self.box[1] + self.box[3]) // 2
    @property
    def width(self): return self.box[2] - self.box[0]
    @property
    def height(self): return self.box[3] - self.box[1]

    def to_dict(self) -> Dict:
        return {
            'box': self.box,
            'confidence': self.confidence,
            'class_name': self.class_name,
            'class_id': self.class_id,
            'cx': self.cx,
            'cy': self.cy,
            'width': self.width,
            'height': self.height
        }


class FractureInference:
    """骨折检测推理类（纯 ONNX，兼容 Win7）"""

    CLASS_NAMES_CN = {0: "骨折", 'fracture': "骨折"}
    DETECTION_COLOR = (0, 165, 255)
    BOX_THICKNESS = 2
    FONT_SCALE = 0.7
    FONT_THICKNESS = 2

    # YOLOv8 检测模型输入尺寸
    INPUT_SIZE = 640

    def __init__(self):
        self.session = None
        self.input_name = None
        self._load_model()

    def _load_model(self):
        """加载 ONNX 骨折检测模型"""
        try:
            import onnxruntime as ort

            if not FRACTURE_MODEL_PATH_ONNX.exists():
                if FRACTURE_MODEL_PATH_PT.exists():
                    raise FileNotFoundError(
                        f"未找到 ONNX 模型: {FRACTURE_MODEL_PATH_ONNX}\n"
                        f"请先在 Win10 上运行 export_models_to_onnx.py 导出 ONNX 文件"
                    )
                raise FileNotFoundError(f"未找到骨折检测模型（.onnx 或 .pt）: {FRACTURE_MODEL_DIR}")

            self.session = ort.InferenceSession(
                str(FRACTURE_MODEL_PATH_ONNX),
                providers=['CPUExecutionProvider']
            )
            self.input_name = self.session.get_inputs()[0].name
            self.output_name = self.session.get_outputs()[0].name
            logger.info(f"骨折检测模型(ONNX)加载完成: {FRACTURE_MODEL_PATH_ONNX}")

        except Exception as e:
            logger.error(f"加载骨折检测模型失败: {e}")
            raise

    def warmup(self):
        """模型预热"""
        if self.session is None:
            logger.warning("骨折检测模型未加载，跳过预热")
            return
        try:
            dummy = np.zeros((1, 3, self.INPUT_SIZE, self.INPUT_SIZE), dtype=np.float32)
            self.session.run([self.output_name], {self.input_name: dummy})
            logger.info("骨折检测模型预热完成")
        except Exception as e:
            logger.error(f"骨折检测模型预热失败: {e}")

    def _preprocess(self, img_bgr: np.ndarray) -> Tuple[np.ndarray, float, Tuple[int, int]]:
        """
        预处理图像：letterbox 缩放到 640×640

        Returns:
            tensor: (1, 3, 640, 640) float32
            scale:  缩放比例
            pad:    (有效宽, 有效高)
        """
        h, w = img_bgr.shape[:2]
        scale = min(self.INPUT_SIZE / w, self.INPUT_SIZE / h)
        new_w = int(w * scale)
        new_h = int(h * scale)

        resized = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # 创建正方形画布（灰色填充）
        canvas = np.full((self.INPUT_SIZE, self.INPUT_SIZE, 3), 114, dtype=np.uint8)
        canvas[:new_h, :new_w] = resized

        # BGR -> RGB, HWC -> CHW, /255
        img_rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        tensor = img_rgb.transpose(2, 0, 1).astype(np.float32) / 255.0
        tensor = np.expand_dims(tensor, 0)  # (1, 3, 640, 640)

        return tensor, scale, (new_w, new_h)

    def detect(self, img_path: str, conf_threshold: float = 0.25,
               iou_threshold: float = 0.45, max_det: int = 1000) -> Dict:
        """
        执行骨折检测

        Args:
            img_path: 图像文件路径
            conf_threshold: 置信度阈值
            iou_threshold: NMS IoU 阈值
            max_det: 最大检测数量

        Returns:
            dict: success / detections / result_image / fracture_count / has_fracture / report / error
        """
        result = {
            'success': False,
            'detections': [],
            'result_image': None,
            'fracture_count': 0,
            'has_fracture': False,
            'report': '',
            'error': None
        }

        try:
            original_img = self._load_image(img_path)
            if original_img is None:
                raise ValueError("无法加载图像")

            h, w = original_img.shape[:2]

            # 预处理
            tensor, scale, pad = self._preprocess(original_img)

            # ONNX 推理
            outputs = self.session.run([self.output_name], {self.input_name: tensor})
            raw_output = outputs[0]  # (1, 84, 8400) 或 (1, 5, 8400) 视模型而定

            # 后处理
            detections = self._postprocess(raw_output, scale, pad, conf_threshold, iou_threshold, w, h)

            # 按置信度排序
            detections.sort(key=lambda d: d.confidence, reverse=True)

            # 后处理优化（保持原版逻辑）
            detections = self._refine_detections(detections, w, h)

            annotated_img = self._draw_detections(original_img.copy(), detections)
            report = self._generate_report(detections, w, h)

            result.update({
                'success': True,
                'detections': detections,
                'result_image': annotated_img,
                'fracture_count': len(detections),
                'has_fracture': len(detections) > 0,
                'report': report
            })

            logger.info(f"骨折检测完成: 发现 {len(detections)} 处骨折")

        except Exception as e:
            logger.error(f"骨折检测失败: {e}")
            result['error'] = str(e)

        return result

    def _postprocess(self, raw_output: np.ndarray, scale: float,
                     pad: Tuple[int, int], conf_thresh: float, iou_thresh: float,
                     orig_w: int, orig_h: int) -> List[FractureDetection]:
        """
        YOLOv8 ONNX 后处理（纯 numpy + cv2.dnn.NMSBoxes）

        YOLOv8 输出格式: (1, num_outputs, 8400)
        num_outputs = 4 (xywh) + num_classes
        """
        new_w, new_h = pad

        # squeeze: (num_outputs, 8400) -> (8400, num_outputs)
        pred = np.squeeze(raw_output).T  # (8400, num_outputs)

        if pred.ndim == 1:
            return []

        num_outputs = pred.shape[1]
        if num_outputs < 5:
            return []

        # 对于骨折模型，只有 1 个类，num_outputs = 5 (4 box + 1 conf)
        # 多类时 num_outputs = 4 + num_classes
        scores = np.max(pred[:, 4:], axis=1)
        mask = scores > conf_thresh
        pred = pred[mask]
        scores = scores[mask]

        if len(pred) == 0:
            return []

        class_ids = np.argmax(pred[:, 4:], axis=1)

        # xywh(相对640) -> xyxy(相对640)
        cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        x1 = cx - bw / 2
        y1 = cy - bh / 2
        x2 = cx + bw / 2
        y2 = cy + bh / 2

        # 裁剪到有效区域（去除 letterbox padding）
        x1 = np.clip(x1, 0, new_w)
        y1 = np.clip(y1, 0, new_h)
        x2 = np.clip(x2, 0, new_w)
        y2 = np.clip(y2, 0, new_h)

        # 还原到原始图像尺寸
        x1 = (x1 / scale).astype(int)
        y1 = (y1 / scale).astype(int)
        x2 = (x2 / scale).astype(int)
        y2 = (y2 / scale).astype(int)

        boxes = np.stack([x1, y1, x2, y2], axis=1)

        # NMS（cv2.dnn.NMSBoxes 接受 [x, y, w, h] 格式）
        boxes_xywh = [[int(b[0]), int(b[1]), int(b[2]-b[0]), int(b[3]-b[1])] for b in boxes]
        indices = cv2.dnn.NMSBoxes(boxes_xywh, scores.tolist(), conf_thresh, iou_thresh)

        detections = []
        if len(indices) > 0:
            for i in indices.flatten():
                x1i, y1i, x2i, y2i = int(boxes[i][0]), int(boxes[i][1]), int(boxes[i][2]), int(boxes[i][3])
                if x2i <= x1i or y2i <= y1i:
                    continue
                detections.append(FractureDetection(
                    box=(x1i, y1i, x2i, y2i),
                    confidence=float(scores[i]),
                    class_name="fracture",
                    class_id=int(class_ids[i])
                ))

        return detections

    def _load_image(self, img_path: str) -> Optional[np.ndarray]:
        """加载图像，支持 DICOM 和普通格式"""
        if not os.path.exists(img_path):
            raise FileNotFoundError(f"图像文件不存在: {img_path}")

        img_lower = img_path.lower()

        if img_lower.endswith('.dcm') or img_lower.endswith('.dicom'):
            try:
                import pydicom
                ds = pydicom.dcmread(img_path)
                pixel_array = ds.pixel_array

                if 'WindowCenter' in ds and 'WindowWidth' in ds:
                    wc = ds.WindowCenter
                    ww = ds.WindowWidth
                    if hasattr(wc, '__iter__'): wc = wc[0]
                    if hasattr(ww, '__iter__'): ww = ww[0]
                    min_val = wc - ww / 2
                    max_val = wc + ww / 2
                    pixel_array = np.clip(pixel_array, min_val, max_val)

                pixel_array = ((pixel_array - pixel_array.min()) /
                               (pixel_array.max() - pixel_array.min() + 1e-10) * 255).astype(np.uint8)

                if len(pixel_array.shape) == 2:
                    pixel_array = cv2.cvtColor(pixel_array, cv2.COLOR_GRAY2BGR)
                return pixel_array

            except Exception as e:
                logger.warning(f"DICOM 读取失败，尝试普通方式: {e}")

        img = imread_cn(img_path)
        if img is None:
            raise ValueError(f"无法读取图像文件: {img_path}")
        return img

    def _refine_detections(self, detections: List[FractureDetection],
                           img_width: int, img_height: int) -> List[FractureDetection]:
        """后处理：优化检测框位置和大小（保持原版逻辑不变）"""
        if not detections:
            return detections

        refined = []
        img_area = img_width * img_height

        for det in detections:
            x1, y1, x2, y2 = det.box
            conf = det.confidence

            box_w = x2 - x1
            box_h = y2 - y1
            box_area = box_w * box_h
            area_ratio = box_area / img_area * 100

            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            aspect_ratio = box_w / max(box_h, 1)

            if area_ratio > 3.0:
                target_area_ratio = 2.0
                scale_factor = (target_area_ratio / area_ratio) ** 0.5
                new_w = int(box_w * scale_factor)
                new_h = int(box_h * scale_factor)
                x1 = cx - new_w // 2
                y1 = cy - new_h // 2
                x2 = cx + new_w // 2
                y2 = cy + new_h // 2
            elif aspect_ratio > 3.0 or aspect_ratio < 0.33:
                if aspect_ratio > 3.0:
                    new_w = int(box_h * 2.0)
                    x1 = cx - new_w // 2
                    x2 = cx + new_w // 2
                else:
                    new_h = int(box_w / 2.0)
                    y1 = cy - new_h // 2
                    y2 = cy + new_h // 2

            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(img_width, x2)
            y2 = min(img_height, y2)

            if (x2 - x1) < 20 or (y2 - y1) < 20:
                x1, y1, x2, y2 = det.box

            refined.append(FractureDetection(
                box=(x1, y1, x2, y2),
                confidence=conf,
                class_name=det.class_name,
                class_id=det.class_id
            ))

        return refined

    def _draw_detections(self, img: np.ndarray,
                         detections: List[FractureDetection]) -> np.ndarray:
        """在图像上绘制骨折检测结果（支持中文）"""
        if not detections:
            return img

        try:
            from PIL import Image, ImageDraw, ImageFont

            img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            draw = ImageDraw.Draw(img_pil)

            font_paths = [
                r"C:\Windows\Fonts\msyh.ttc",
                r"C:\Windows\Fonts\simsun.ttc",
                r"C:\Windows\Fonts\simhei.ttf",
            ]
            font = None
            for fp in font_paths:
                if os.path.exists(fp):
                    try:
                        font = ImageFont.truetype(fp, 20)
                        break
                    except Exception:
                        continue
            if font is None:
                font = ImageFont.load_default()

            for det in detections:
                x1, y1, x2, y2 = det.box
                conf = det.confidence
                cn_name = self.CLASS_NAMES_CN.get(det.class_id, "骨折")

                draw.rectangle([x1, y1, x2, y2], outline=self.DETECTION_COLOR, width=self.BOX_THICKNESS)

                label = f"{cn_name} {conf:.1%}"
                bbox = draw.textbbox((0, 0), label, font=font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
                label_y1 = max(y1 - text_h - 8, 0)
                draw.rectangle([x1, label_y1, x1 + text_w + 6, y1], fill=self.DETECTION_COLOR)
                draw.text((x1 + 3, label_y1), label, fill=(255, 255, 255), font=font)

                cx, cy = det.cx, det.cy
                draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(0, 0, 255))

            return cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

        except Exception as e:
            logger.warning(f"PIL 绘制失败，回退到 OpenCV: {e}")
            for det in detections:
                x1, y1, x2, y2 = det.box
                conf = det.confidence
                cv2.rectangle(img, (x1, y1), (x2, y2), self.DETECTION_COLOR, self.BOX_THICKNESS)
                label = f"Fracture {conf:.1%}"
                (lw, lh), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, self.FONT_SCALE, self.FONT_THICKNESS)
                label_y1 = max(y1 - lh - 10, 0)
                cv2.rectangle(img, (x1, label_y1), (x1 + lw + 4, y1), self.DETECTION_COLOR, -1)
                cv2.putText(img, label, (x1 + 2, y1 - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, self.FONT_SCALE, (255, 255, 255), self.FONT_THICKNESS)
                cv2.circle(img, (det.cx, det.cy), 5, (0, 0, 255), -1)
            return img

    def _generate_report(self, detections: List[FractureDetection],
                         img_width: int, img_height: int) -> str:
        lines = ["<b>骨折检测结果</b><br>"]

        if not detections:
            lines.append("<br><b style='color: green;'>✓ 未检测到明显骨折</b>")
            lines.append("<br>建议：结合临床体征进一步评估")
        else:
            lines.append(f"<br><b style='color: red;'>⚠ 检测到 {len(detections)} 处疑似骨折</b><br>")
            for i, det in enumerate(detections, 1):
                conf_pct = det.confidence * 100
                rel_x = det.cx / img_width * 100
                rel_y = det.cy / img_height * 100
                lines.append(f"<b>区域 {i}:</b> 置信度 {conf_pct:.1f}%")
                lines.append(f"   位置: ({rel_x:.1f}%, {rel_y:.1f}%)")
                lines.append(f"   大小: {det.width}×{det.height}px<br>")
            lines.append("<br><b>建议:</b>")
            lines.append("• 请结合临床症状和体征综合判断")
            lines.append("• 建议进行进一步影像学检查确认")
            lines.append("• 如有疑问请咨询专业放射科医师")

        return "".join(lines)


# 全局单例缓存
_fracture_instance = None


def get_fracture_inference() -> Optional[FractureInference]:
    """获取骨折检测推理器实例（单例模式）"""
    global _fracture_instance
    if _fracture_instance is None:
        try:
            _fracture_instance = FractureInference()
        except Exception as e:
            logger.error(f"初始化骨折检测推理器失败: {e}")
            return None
    return _fracture_instance
