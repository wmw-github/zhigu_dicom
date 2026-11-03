"""
骨龄检测推理模块 - ONNX 版本（Win7 兼容）

基于 YOLOv8 的骨龄检测系统，采用 RUS-CHN 法进行骨龄评估
模型由 Win10 上的 export_models_to_onnx.py 预先导出为 ONNX 格式
使用纯 onnxruntime 推理，不依赖 ultralytics / torch / torchvision
"""
import os
import cv2
import math
import numpy as np
from pathlib import Path
from PIL import Image
from typing import Dict, List, Tuple, Optional

from utils.image_io import imread_cn, imwrite_cn

from utils.logger import get_logger

import sys
APP_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(APP_DIR))

from models.boneage.boneage_annotation import (
    RUSCHNAnnotator,
    RUSCHN_JOINTS,
    RUSCHN_REPORT_ORDER,
    SCORE_TABLE,
    draw_ruschn_annotations,
    generate_ruschn_report,
    grade_display_name,
)

logger = get_logger(__name__)

BONEAGE_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "boneage"

DETECT_MODEL_PATH = BONEAGE_MODEL_DIR / "yolov8n_detect_bone_age.onnx"

ARTHROSIS = {
    'MCPFirst': ['MCPFirst', 11],
    'MCPThird': ['MCP', 10],
    'MCPFifth': ['MCP', 10],
    'DIPFirst': ['DIPFirst', 11],
    'DIPThird': ['DIP', 11],
    'DIPFifth': ['DIP', 11],
    'PIPFirst': ['PIPFirst', 12],
    'PIPThird': ['PIP', 12],
    'PIPFifth': ['PIP', 12],
    'MIPThird': ['MIP', 12],
    'MIPFifth': ['MIP', 12],
    'Radius': ['Radius', 14],
    'Ulna': ['Ulna', 12],
}

MODEL_PATHS = {}
for model_key in set([v[0] for v in ARTHROSIS.values()]):
    MODEL_PATHS[model_key] = BONEAGE_MODEL_DIR / f"yolov8s_cls_{model_key}.onnx"

# 各分类模型的最低可信置信度（2026-11 引入，解决「第五中节指骨未评级」）
#
# 为什么不能用一个全局阈值：中节指骨（MIP）是 13 项里最小、骨骺特征最弱的一项，
# 尤其是第五指（小指）的中节，片子分辨率稍低就发虚。模型 argmax 经常是**对的**，
# 但置信度只有 0.17~0.29 —— 实测 9 张里 4 张的 MIPFifth 落在这个区间，被 0.30
# 一刀切掉。而「切掉」在当前计分逻辑里等于这一项记 0 分（MIPFifth 满分 49/1000），
# 于是骨龄被系统性低估：m17 的 MIPFifth 判 12 级（满级，完全正确）却因 conf=0.24
# 被丢，白少 49 分。
#
# 反过来，桡骨（Radius）权重最大（满分 210），错误判级会放大成 1~2 岁误差，
# 所以它必须继续卡在 0.30。实测对比（9 张，MAE）：
#   全局 0.30 → 0.60 ｜ MIP 放宽 → 0.39 ｜ 全部放开(0.0) → 0.67 ｜ 全部 0.20 → 0.77
# 只有「按模型分档」这一种同时改善了精度和关节完整度。
CLS_CONF_BY_MODEL = {
    'MIP': 0.10,          # 中节指骨：放宽，argmax 照用，标记为低置信供医生复核
    'MCP': 0.30,
    'PIP': 0.30,
    'DIP': 0.30,
    'MCPFirst': 0.30,
    'PIPFirst': 0.30,
    'DIPFirst': 0.30,
    'Radius': 0.30,       # 权重最高，误判代价最大，不放宽
    'Ulna': 0.30,
}

# 分类模型输入尺寸（YOLOv8s-cls 默认 224）
CLS_INPUT_SIZE = 224
# 检测模型输入尺寸
DET_INPUT_SIZE = 320  # ONNX 模型输入尺寸 (与 export_models_to_onnx.py 使用 imgsz=320 一致)


def _preprocess_grayscale_96(img_bgr: np.ndarray) -> np.ndarray:
    """
    替代 torchvision.transforms.Compose([Grayscale, Resize(96,96)])
    转为灰度 + resize 到 96×96，返回 BGR 格式供分类模型使用
    """
    # BGR -> 灰度
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    # resize 到 96×96
    resized = cv2.resize(gray, (96, 96), interpolation=cv2.INTER_LINEAR)
    # 灰度 -> BGR（3 通道，以符合 YOLOv8 分类模型输入）
    return cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)


def _letterbox(img_bgr: np.ndarray, target: int = 640) -> Tuple[np.ndarray, float, Tuple[int, int]]:
    """
    Letterbox 缩放：保持宽高比 resize 到 target×target，灰色填充

    Returns:
        canvas:  (target, target, 3) uint8
        scale:   缩放比例
        pad:     (有效宽, 有效高)
    """
    h, w = img_bgr.shape[:2]
    scale = min(target / w, target / h)
    new_w = int(w * scale)
    new_h = int(h * scale)

    resized = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((target, target, 3), 114, dtype=np.uint8)
    canvas[:new_h, :new_w] = resized
    return canvas, scale, (new_w, new_h)


def _bgr_to_tensor(img_bgr: np.ndarray) -> np.ndarray:
    """BGR (H,W,3) uint8 -> float32 tensor (1,3,H,W) in [0,1]"""
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    tensor = np.ascontiguousarray(img_rgb.transpose(2, 0, 1)).astype(np.float32) / 255.0
    return np.ascontiguousarray(np.expand_dims(tensor, 0))


class OnnxDetector:
    """YOLOv8 检测模型 ONNX 包装（用于骨关节定位）"""

    def __init__(self, model_path: Path, input_size: int = 640):
        import onnxruntime as ort
        if not model_path.exists():
            raise FileNotFoundError(
                f"未找到检测模型: {model_path}\n"
                f"请先在 Win10 上运行 export_models_to_onnx.py"
            )
        self.session = ort.InferenceSession(str(model_path), providers=['CPUExecutionProvider'])
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.input_size = input_size
        # 读取类别名（从模型元数据）
        meta = self.session.get_modelmeta()
        self.names = {}
        if hasattr(meta, 'custom_metadata_map') and 'names' in meta.custom_metadata_map:
            import ast
            try:
                self.names = ast.literal_eval(meta.custom_metadata_map['names'])
            except Exception:
                pass

    def predict(self, img_bgr: np.ndarray, conf: float = 0.5) -> List[Dict]:
        """
        检测骨关节位置

        Returns:
            list of {class_id, class_name, confidence, box, cx, cy, img_crop}
        """
        canvas, scale, (new_w, new_h) = _letterbox(img_bgr, self.input_size)
        tensor = _bgr_to_tensor(canvas)

        raw = self.session.run([self.output_name], {self.input_name: tensor})[0]
        pred = np.squeeze(raw).T  # (8400, 4+num_cls)

        if pred.ndim == 1:
            return []

        scores = np.max(pred[:, 4:], axis=1)
        mask = scores > conf
        pred = pred[mask]
        scores = scores[mask]

        if len(pred) == 0:
            return []

        class_ids = np.argmax(pred[:, 4:], axis=1)

        cx_raw, cy_raw, bw_raw, bh_raw = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        x1 = np.clip((cx_raw - bw_raw / 2), 0, new_w) / scale
        y1 = np.clip((cy_raw - bh_raw / 2), 0, new_h) / scale
        x2 = np.clip((cx_raw + bw_raw / 2), 0, new_w) / scale
        y2 = np.clip((cy_raw + bh_raw / 2), 0, new_h) / scale

        boxes = np.stack([x1, y1, x2, y2], axis=1)

        # NMS
        boxes_xywh = [[int(b[0]), int(b[1]), int(b[2]-b[0]), int(b[3]-b[1])] for b in boxes]
        indices = cv2.dnn.NMSBoxes(boxes_xywh, scores.tolist(), conf, 0.45)

        results = []
        if len(indices) > 0:
            orig_h, orig_w = img_bgr.shape[:2]
            for i in indices.flatten():
                x1i, y1i, x2i, y2i = (
                    max(0, int(boxes[i][0])), max(0, int(boxes[i][1])),
                    min(orig_w, int(boxes[i][2])), min(orig_h, int(boxes[i][3]))
                )
                if x2i <= x1i or y2i <= y1i:
                    continue
                cls_id = int(class_ids[i])
                cls_name = self.names.get(cls_id, str(cls_id))
                crop = img_bgr[y1i:y2i, x1i:x2i].copy()
                results.append({
                    'class_id': cls_id,
                    'class_name': cls_name,
                    'confidence': float(scores[i]),
                    'box': (x1i, y1i, x2i, y2i),
                    'cx': (x1i + x2i) // 2,
                    'cy': (y1i + y2i) // 2,
                    'img_crop': crop,
                })

        return results


class OnnxClassifier:
    """YOLOv8 分类模型 ONNX 包装（用于关节等级分类）"""

    def __init__(self, model_path: Path):
        import onnxruntime as ort
        if not model_path.exists():
            raise FileNotFoundError(f"未找到分类模型: {model_path}")
        self.session = ort.InferenceSession(str(model_path), providers=['CPUExecutionProvider'])
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        # 推断输入尺寸
        shape = self.session.get_inputs()[0].shape  # (1, 3, H, W)
        self.input_h = shape[2] if isinstance(shape[2], int) else CLS_INPUT_SIZE
        self.input_w = shape[3] if isinstance(shape[3], int) else CLS_INPUT_SIZE
        # 读取类别名
        meta = self.session.get_modelmeta()
        self.names = {}
        if hasattr(meta, 'custom_metadata_map') and 'names' in meta.custom_metadata_map:
            import ast
            try:
                self.names = ast.literal_eval(meta.custom_metadata_map['names'])
            except Exception:
                pass

    def predict(self, img_bgr: np.ndarray) -> Tuple[int, float]:
        """
        分类关节等级

        Returns:
            (top1_class_id, top1_confidence)
        """
        # resize + normalize
        resized = cv2.resize(img_bgr, (self.input_w, self.input_h), interpolation=cv2.INTER_LINEAR)
        tensor = _bgr_to_tensor(resized)

        raw = self.session.run([self.output_name], {self.input_name: tensor})[0]
        # YOLOv8 cls 输出: (1, num_classes) softmax 后的概率
        probs = np.squeeze(raw)
        top1 = int(np.argmax(probs))
        conf = float(probs[top1])
        return top1, conf


class BoneAgeInference:
    """骨龄检测推理类（纯 ONNX，兼容 Win7）"""

    def __init__(self):
        self.detect_model: Optional[OnnxDetector] = None
        self.cls_models: Dict[str, OnnxClassifier] = {}
        self.annotator = RUSCHNAnnotator()
        self._load_models()

    def _load_models(self):
        """加载检测模型和分类模型"""
        try:
            # 检测模型
            self.detect_model = OnnxDetector(DETECT_MODEL_PATH, DET_INPUT_SIZE)
            logger.info(f"骨龄检测模型(ONNX)加载完成: {DETECT_MODEL_PATH}")

            # 分类模型
            missing = []
            for model_key, model_path in MODEL_PATHS.items():
                if model_path.exists():
                    try:
                        self.cls_models[model_key] = OnnxClassifier(model_path)
                        logger.info(f"分类模型(ONNX)加载完成: {model_key}")
                    except Exception as e:
                        logger.error(f"加载分类模型失败 {model_key}: {e}")
                        missing.append(model_key)
                else:
                    logger.warning(f"模型文件不存在: {model_path}")
                    missing.append(model_key)

            required = set(v[0] for v in ARTHROSIS.values())
            loaded = set(self.cls_models.keys())
            still_missing = required - loaded
            if still_missing:
                logger.warning(f"缺少以下分类模型: {still_missing}")

        except Exception as e:
            logger.error(f"加载骨龄模型失败: {e}")
            raise

    def warmup(self):
        """模型预热"""
        if self.detect_model is None:
            logger.warning("检测模型未加载，跳过预热")
            return
        try:
            dummy = np.random.randint(0, 255, (640, 640, 3), dtype=np.uint8)
            self.detect_model.predict(dummy, conf=0.5)
            logger.info("骨龄检测模型预热完成")

            for model_key, model in self.cls_models.items():
                try:
                    dummy_crop = np.random.randint(0, 255, (96, 96, 3), dtype=np.uint8)
                    model.predict(dummy_crop)
                    logger.info(f"分类模型 {model_key} 预热完成")
                except Exception as e:
                    logger.warning(f"分类模型 {model_key} 预热失败: {e}")

            logger.info("所有骨龄模型预热完成")
        except Exception as e:
            logger.error(f"模型预热失败: {e}")

    def _load_image(self, img_path: str) -> np.ndarray:
        """加载图像，支持 DICOM 和普通图像格式"""
        img_path_lower = img_path.lower()

        if img_path_lower.endswith('.dcm') or img_path_lower.endswith('.dicom'):
            try:
                import pydicom
                from pydicom.pixel_data_handlers.util import apply_voi_lut

                ds = pydicom.dcmread(img_path)
                pixel_array = ds.pixel_array
                logger.info(f"DICOM图像尺寸: {pixel_array.shape}")

                if 'WindowCenter' in ds and 'WindowWidth' in ds:
                    try:
                        pixel_array = apply_voi_lut(pixel_array, ds)
                    except Exception:
                        pass

                pixel_array = pixel_array.astype(np.float32)
                array_min, array_max = pixel_array.min(), pixel_array.max()
                if array_max > array_min:
                    pixel_array = (pixel_array - array_min) / (array_max - array_min)
                else:
                    pixel_array = np.zeros_like(pixel_array)
                pixel_array = (pixel_array * 255).astype(np.uint8)

                if len(pixel_array.shape) == 2:
                    return cv2.cvtColor(pixel_array, cv2.COLOR_GRAY2BGR)
                elif len(pixel_array.shape) == 3 and pixel_array.shape[2] == 1:
                    return cv2.cvtColor(pixel_array[:, :, 0], cv2.COLOR_GRAY2BGR)
                else:
                    return cv2.cvtColor(pixel_array, cv2.COLOR_RGB2BGR)

            except ImportError:
                raise ImportError("请安装pydicom以支持DICOM文件: pip install pydicom")
            except Exception as e:
                raise FileNotFoundError(f"无法读取DICOM文件: {img_path}, 错误: {e}")
        else:
            img_bgr = imread_cn(img_path)
            if img_bgr is None:
                raise FileNotFoundError(f"无法读取图片: {img_path}")
            return img_bgr

    def _preprocess_for_classification(self, img_bgr: np.ndarray) -> np.ndarray:
        """
        图像预处理：转正方形 + 灰度 + resize 96×96 + 转 BGR 3 通道
        替代原版 torchvision.transforms（Grayscale + Resize(96,96)）
        """
        h, w, c = img_bgr.shape

        # 正方形填充
        if h != w:
            long_side = max(h, w)
            pad = (long_side - min(h, w)) // 2
            background = np.zeros((long_side, long_side, c), dtype=np.uint8)
            if h > w:
                background[:, pad:pad + w, :] = img_bgr
            else:
                background[pad:pad + h, :, :] = img_bgr
            img_bgr = background

        return _preprocess_grayscale_96(img_bgr)

    def _classify_joints(self, mapped_joints: Dict, sex: str, cls_conf: float = 0.5,
                         detail_out: Dict = None) -> Tuple[Dict, int]:
        """关节等级分类，计算分数。

        Args:
            cls_conf: 「基准」置信度。低于它但高于该模型分档阈值（CLS_CONF_BY_MODEL）
                的关节**保留**，并在 ``detail_out`` 里标 ``low_confidence=True``；
                低于分档阈值才真正丢弃。
            detail_out: 传入 dict 时，按关节写入
                ``{'level','confidence','score','model','low_confidence','status'}``，
                ``status`` 取值 ``ok / low_conf / dropped / no_box / error``。
                用于标注图上区分「低置信」与「未评级」，以及报告里的缺损说明。
        """
        results_score = {}
        total_score = 0

        for joint_name, det in mapped_joints.items():
            if joint_name not in ARTHROSIS:
                continue

            model_key = ARTHROSIS[joint_name][0]

            def _mark(status, **kw):
                if detail_out is None:
                    return
                row = {'level': 0, 'confidence': 0.0, 'score': 0,
                       'model': model_key, 'low_confidence': False, 'status': status}
                row.update(kw)
                detail_out[joint_name] = row

            if model_key not in self.cls_models:
                logger.warning(f"跳过{joint_name}：无对应分类模型{model_key}")
                _mark('no_model')
                continue

            crop_img = det.img_crop

            if crop_img is None or crop_img.size == 0:
                logger.warning(f"跳过{joint_name}：裁剪区域为空")
                _mark('no_box')
                continue

            try:
                cls_model = self.cls_models[model_key]
                crop_img_prep = self._preprocess_for_classification(crop_img)

                top1, conf = cls_model.predict(crop_img_prep)

                # class name -> int level
                # YOLOv8 cls 模型的类别名通常是 '1', '2', ... 字符串
                cls_name = cls_model.names.get(top1, str(top1))
                try:
                    level = int(cls_name) - 1
                except ValueError:
                    level = top1

                # 分档阈值：中节指骨等弱特征关节放宽，其余沿用基准值。
                # 按**比例**缩放而不是写死数值：界面设置里 boneage_cls_conf 可调，
                # 用户把门槛抬高时各档要跟着抬高，否则「设置不生效」比没设置更糟。
                factor = CLS_CONF_BY_MODEL.get(model_key, 0.30) / 0.30
                min_conf = cls_conf * factor
                if conf < min_conf:
                    logger.warning(f"{joint_name}：置信度 {conf:.3f} 低于阈值 {min_conf}")
                    _mark('dropped', confidence=conf,
                          level=(level + 1) if isinstance(level, int) else 0)
                    continue

                if joint_name not in SCORE_TABLE[sex]:
                    logger.warning(f"跳过{joint_name}：无{sex}性别的分数表")
                    _mark('no_score_table', confidence=conf)
                    continue

                score_list = SCORE_TABLE[sex][joint_name]
                if level < 0 or level >= len(score_list):
                    logger.warning(f"{joint_name}等级{level + 1}超出范围")
                    score = 0
                else:
                    score = score_list[level]

                low = conf < cls_conf
                results_score[joint_name] = (level + 1, score)
                total_score += score
                _mark('low_conf' if low else 'ok', level=level + 1, confidence=conf,
                      score=score, low_confidence=low)
                logger.info("%s：等级%d，得分%d (置信度%.3f%s)" % (
                    joint_name, level + 1, score, conf,
                    "，低置信但已采用" if low else ""))

            except Exception as e:
                logger.warning(f"{joint_name}分类失败：{e}")
                _mark('error')
                continue

        return results_score, total_score

    def _calculate_bone_age(self, score: int, sex: str) -> float:
        """骨龄计算公式（男/女不同，保持原版不变）"""
        if sex == 'boy':
            bone_age = (2.01790023656577
                        + (-0.0931820870747269) * score
                        + math.pow(score, 2) * 0.00334709095418796
                        + math.pow(score, 3) * (-3.32988302362153E-05)
                        + math.pow(score, 4) * (1.75712910819776E-07)
                        + math.pow(score, 5) * (-5.59998691223273E-10)
                        + math.pow(score, 6) * (1.1296711294933E-12)
                        + math.pow(score, 7) * (-1.45218037113138e-15)
                        + math.pow(score, 8) * (1.15333377080353e-18)
                        + math.pow(score, 9) * (-5.15887481551927e-22)
                        + math.pow(score, 10) * (9.94098428102335e-26))
            return round(bone_age, 2)
        elif sex == 'girl':
            bone_age = (5.81191794824917
                        + (-0.271546561737745) * score
                        + math.pow(score, 2) * 0.00526301486340724
                        + math.pow(score, 3) * (-4.37797717401925E-05)
                        + math.pow(score, 4) * (2.0858722025667E-07)
                        + math.pow(score, 5) * (-6.21879866563429E-10)
                        + math.pow(score, 6) * (1.19909931745368E-12)
                        + math.pow(score, 7) * (-1.49462900826936E-15)
                        + math.pow(score, 8) * (1.162435538672E-18)
                        + math.pow(score, 9) * (-5.12713017846218E-22)
                        + math.pow(score, 10) * (9.78989966891478E-26))
            return round(bone_age, 2)
        else:
            raise ValueError("性别必须为'boy'或'girl'")

    def detect(self, img_path: str, sex: str,
               detect_conf: float = 0.3, cls_conf: float = 0.3,
               draw_annotation: bool = True) -> Dict:
        """
        完整骨龄检测流程（接口与原版完全相同）

        Args:
            img_path: 手部 X 光片图像路径
            sex: 性别 ('boy' 或 'girl')
            detect_conf: 关节检测置信度阈值
            cls_conf: 等级分类置信度阈值
            draw_annotation: 是否把 13 关节标注画到返回图上

        Returns:
            Dict: 包含检测结果的字典

        ``result_image`` 自 v1.24.0 起是**已画好 13 关节标注**的图（此前返回的是
        无标注原图，导致主界面「AI 标注」层什么也看不到，标注功能只存在于
        debug_output 目录里）。需要底图时用 ``raw_image``。
        """
        try:
            logger.info(f"开始骨龄检测: {img_path}, 性别: {sex}")

            img_bgr = self._load_image(img_path)
            original_img = img_bgr.copy()
            logger.info(f"图像尺寸: {img_bgr.shape[1]}x{img_bgr.shape[0]}")

            # 关节检测（返回与原版 ultralytics 结果兼容的格式）
            raw_detections = self.detect_model.predict(img_bgr, conf=detect_conf)

            # 将 ONNX 检测结果包装成 RUSCHNAnnotator 期望的格式
            # parse_yolo_results 期望 ultralytics Results 对象，需要适配
            self.annotator = RUSCHNAnnotator()
            detections = self._adapt_detections(raw_detections, original_img)

            logger.info(f"检测到 {sum(len(v) for v in detections.values())} 个目标")

            hand_side = self.annotator.determine_hand_side()
            logger.info(f"检测到{'左手' if hand_side == 'L' else '右手'}")

            if hand_side != 'L':
                raise ValueError("RUS-CHN法仅支持左手X光片，请使用左手X光片进行检测")

            # 2026-11 校正：改用按解剖重建的映射（旧 map_to_ruschn 的类别名假设是错的，
            # 且拇指锚点会落到食指上，详见 boneage_annotation.DETECTOR_CLASS_ANATOMY）
            mapped_joints = self.annotator.map_to_ruschn_calibrated()

            annotation_summary = self.annotator.get_annotation_summary(mapped_joints)
            logger.info(f"\n{annotation_summary}")

            # 逐关节的分类明细（等级/置信度/是否低置信/未分级原因），
            # 供标注图区分「低置信」与「未评级」，也供报告说明缺项
            classify_detail: Dict[str, Dict] = {}

            results_score, total_score = self._classify_joints(
                mapped_joints, sex, cls_conf, detail_out=classify_detail)

            if len(results_score) == 0:
                raise ValueError("没有成功分类任何关节")

            bone_age = self._calculate_bone_age(total_score, sex)

            report = generate_ruschn_report(results_score, total_score, bone_age,
                                            detail=classify_detail)

            debug_dir = Path("debug_output")
            debug_dir.mkdir(exist_ok=True)

            # 标注图既是界面显示图，也是「保存标注图」的图源，一图两用。
            # 原图另存一份 raw_image：主界面「显示关节标注」取消勾选时要用它
            # 还原，不能靠再读一次片子（会与当前显示的行序/窗值对不上）。
            if draw_annotation:
                annotated_img = draw_ruschn_annotations(
                    original_img, mapped_joints, results_score, detail=classify_detail)
            else:
                annotated_img = original_img.copy()
            debug_path = debug_dir / f"boneage_debug_{Path(img_path).stem}.jpg"
            try:
                imwrite_cn(str(debug_path), annotated_img)
                logger.info(f"调试图像已保存: {debug_path}")
            except Exception as exc:      # noqa: BLE001
                # 调试图写不出来不能拖垮检测本身（目录只读、磁盘满都见过）
                logger.warning(f"调试图像保存失败（不影响结果）: {exc}")

            mapped_dict_for_return = {}
            for joint_name, det in mapped_joints.items():
                mapped_dict_for_return[joint_name] = [{
                    "class": det.class_name,
                    "confidence": det.confidence,
                    "box": det.box,
                    "cx": det.cx,
                    "cy": det.cy,
                    "img_crop": det.img_crop
                }]

            logger.info(f"骨龄检测完成: 总得分 {total_score}, 骨龄 {bone_age}岁")

            return {
                "success": True,
                "total_score": total_score,
                "bone_age": bone_age,
                "joint_scores": results_score,
                "report": report,
                # 带 13 关节标注的成品图（界面直接显示 / 可另存）
                "result_image": annotated_img,
                # 未标注原图：界面「显示关节标注」取消勾选时还原用
                "raw_image": original_img,
                "annotation_drawn": bool(draw_annotation),
                # 关节名 / 等级 / 分数 / 框，供界面做图例或高亮
                "annotations": self._collect_annotations(mapped_joints,
                                                         results_score,
                                                         classify_detail),
                "joint_count": len(results_score),
                # 低置信但仍计分的关节（界面提示「建议复核」）
                "low_confidence_joints": [k for k, v in classify_detail.items()
                                          if v.get("low_confidence")],
                # 真正没评上等级的关节（含没检到框的、置信度不足被丢的）
                "ungraded_joints": [k for k, v in classify_detail.items()
                                    if v.get("status") != "ok"
                                    and not v.get("low_confidence")],
                "classify_detail": classify_detail,
                "debug_image_path": str(debug_path),
                "annotation_summary": annotation_summary,
                "mapped_joints": mapped_dict_for_return
            }

        except Exception as e:
            logger.error(f"骨龄检测失败: {e}")
            return {"success": False, "error": str(e)}

    def _collect_annotations(self, mapped_joints: Dict, results_score: Dict,
                             detail: Dict = None) -> List[Dict]:
        """把 13 关节的「名称 / 等级 / 分数 / 框」整理成界面友好的列表

        界面侧要拿这份数据做两件事：检测结果列表按报告顺序排（已经由
        xray_bridge 做），以及「标注图例」里标出每个关节落在片子哪块。
        ``box`` 用原图像素坐标，与 ``result_image`` 同一坐标系。

        ``confidence`` / ``low_confidence`` / ``status`` 来自分类明细，用于
        区分「等级可信」「低置信但仍计分」「完全没评上」三种状态。
        """
        out = []
        try:
            order = list(RUSCHN_REPORT_ORDER)
        except Exception:            # noqa: BLE001
            order = list(mapped_joints.keys())
        for key in order:
            det = mapped_joints.get(key)
            if det is None:
                continue
            cn = (RUSCHN_JOINTS.get(key) or {}).get("name", key)
            level = score = None
            val = results_score.get(key)
            if val:
                try:
                    level, score = int(val[0]), int(val[1])
                except Exception:    # noqa: BLE001
                    level = score = None
            d = (detail or {}).get(key) or {}
            try:
                box = [int(v) for v in det.box]
            except Exception:        # noqa: BLE001
                box = None
            out.append({"key": key, "name": cn, "level": level,
                        "score": score, "box": box,
                        # grade 是 RUS-CHN 等级名（1~8 级，含子档），level 是内部
                        # 细分档序号（1~14）。界面一律显示 grade —— 报告也用它，
                        # 两处不同源就会变成「图上 8 级、报告 12 级」。
                        "grade": (grade_display_name(key, level)
                                  if level is not None else None),
                        "confidence": round(float(d.get("confidence") or 0.0), 3),
                        "low_confidence": bool(d.get("low_confidence")),
                        "status": d.get("status", "ok" if val else "dropped")})
        return out

    def _adapt_detections(self, raw_detections: List[Dict], original_img: np.ndarray) -> Dict:
        """
        将 ONNX 检测结果填入 RUSCHNAnnotator 的内部数据结构

        原版调用路径：
            results = self.detect_model(img_bgr, ...)   # ultralytics Results
            detections = self.annotator.parse_yolo_results(results, original_img)

        新版直接操作 annotator 内部的 detections dict，绕过 parse_yolo_results
        """
        from collections import defaultdict
        from models.boneage.boneage_annotation import JointDetection

        # 用 defaultdict(list) 与原版 parse_yolo_results 保持一致
        self.annotator.detections = defaultdict(list)

        for det in raw_detections:
            cls_name = det['class_name']
            x1, y1, x2, y2 = det['box']
            crop = original_img[y1:y2, x1:x2].copy()

            detection_obj = JointDetection(
                class_name=cls_name,
                confidence=det['confidence'],
                box=det['box'],
                cx=det['cx'],
                cy=det['cy'],
                img_crop=crop
            )
            self.annotator.detections[cls_name].append(detection_obj)

        # 按 x 坐标排序（与 parse_yolo_results 保持一致）
        for cls_name in self.annotator.detections:
            self.annotator.detections[cls_name].sort(key=lambda d: d.box[0])

        return self.annotator.detections


# 全局单例
boneage_inference = None


def get_boneage_inference():
    """获取骨龄检测推理实例（单例模式）"""
    global boneage_inference
    if boneage_inference is None:
        boneage_inference = BoneAgeInference()
    return boneage_inference
