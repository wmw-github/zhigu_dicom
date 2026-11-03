"""
足弓关键点检测推理模块 - ONNX 版本（Win7 兼容）

基于 ResNet50 热图回归的足弓关键点检测，自动测量内侧和外侧足弓角。
使用纯 ONNX Runtime 推理，不依赖 torch/torchvision，兼容 Win7 部署环境。
"""
import os
import sys
import cv2
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# 添加 app 目录到 sys.path 以支持 utils 等模块
APP_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(APP_DIR))

from utils.logger import get_logger

logger = get_logger(__name__)

# 关键点定义
NUM_KEYPOINTS = 5
KEYPOINT_NAMES = [
    "P1_跟骨最低点",
    "P2_距骨最低点",
    "P3_第一跖骨头最低点",
    "P4_第五跖骨头最低点",
    "P5_跟骰关节最低点",
]
KEYPOINT_COLORS = [
    (255, 68, 68),    # P1 红
    (68, 170, 255),   # P2 蓝
    (68, 255, 68),    # P3 绿
    (255, 170, 0),    # P4 橙
    (255, 68, 255),   # P5 紫
]

INPUT_SIZE = 800
HEATMAP_SIZE = 200

IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]

MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "foot_arch"
ONNX_MODEL_PATH = MODEL_DIR / "foot_arch_keypoint.onnx"

_instance = None


def _letterbox_params(orig_w, orig_h, input_size=INPUT_SIZE):
    """计算 letterbox 缩放参数"""
    scale = min(input_size / orig_w, input_size / orig_h)
    new_w, new_h = int(orig_w * scale), int(orig_h * scale)
    pad_x = (input_size - new_w) // 2
    pad_y = (input_size - new_h) // 2
    return scale, pad_x, pad_y


def _input_to_orig(input_x, input_y, scale, pad_x, pad_y):
    """从输入图像坐标还原到原图坐标"""
    return (input_x - pad_x) / scale, (input_y - pad_y) / scale


def _get_subpixel_heatmap_peak(heatmap):
    """亚像素级热图峰值定位"""
    h, w = heatmap.shape
    max_idx = np.unravel_index(np.argmax(heatmap), heatmap.shape)
    hm_row, hm_col = max_idx
    peak_val = heatmap[hm_row, hm_col]
    if peak_val < 1e-6:
        return float(hm_col), float(hm_row)
    window = 2
    r_start, r_end = max(0, hm_row - window), min(h, hm_row + window + 1)
    c_start, c_end = max(0, hm_col - window), min(w, hm_col + window + 1)
    patch = np.maximum(heatmap[r_start:r_end, c_start:c_end], 0)
    total = patch.sum()
    if total < 1e-6:
        return float(hm_col), float(hm_row)
    rows = np.arange(r_start, r_end).reshape(-1, 1)
    cols = np.arange(c_start, c_end).reshape(1, -1)
    sub_row = (patch * rows).sum() / total
    sub_col = (patch * cols).sum() / total
    return float(sub_col), float(sub_row)


def _compute_arch_angle(p_vertex, p1, p2):
    """计算三点夹角（度数），p_vertex 为顶点"""
    v1 = np.array(p1) - np.array(p_vertex)
    v2 = np.array(p2) - np.array(p_vertex)
    cos_a = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-8)
    cos_a = np.clip(cos_a, -1.0, 1.0)
    return float(np.degrees(np.arccos(cos_a)))


def _normalize_image(img_np):
    """ImageNet 标准化"""
    for c in range(3):
        img_np[:, :, c] = (img_np[:, :, c] - IMAGENET_MEAN[c]) / IMAGENET_STD[c]
    return img_np


class FootArchInference:
    """足弓关键点检测推理引擎 (ONNX)"""

    def __init__(self, model_path=None):
        import onnxruntime as ort

        mp = model_path or ONNX_MODEL_PATH
        if not os.path.exists(mp):
            raise FileNotFoundError(f"足弓ONNX模型文件不存在: {mp}")

        providers = ['CPUExecutionProvider']
        if 'CUDAExecutionProvider' in ort.get_available_providers():
            providers.insert(0, 'CUDAExecutionProvider')

        self.session = ort.InferenceSession(str(mp), providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        logger.info(f"足弓关键点ONNX模型加载成功: {mp}, providers={providers}")

    def _preprocess(self, img_bgr):
        """预处理图像：letterbox + resize + normalize -> NCHW float32"""
        orig_h, orig_w = img_bgr.shape[:2]
        scale, pad_x, pad_y = _letterbox_params(orig_w, orig_h)
        new_w, new_h = int(orig_w * scale), int(orig_h * scale)
        img_resized = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        img_padded = np.full((INPUT_SIZE, INPUT_SIZE, 3), 128, dtype=np.uint8)
        img_padded[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = img_resized

        img_norm = img_padded.astype(np.float32) / 255.0
        img_norm = _normalize_image(img_norm)
        img_chw = np.transpose(img_norm, (2, 0, 1)).astype(np.float32)
        img_batch = np.expand_dims(img_chw, axis=0)

        return img_batch, scale, pad_x, pad_y, orig_w, orig_h

    def predict(self, img_path):
        """
        对单张图像进行足弓关键点检测

        Args:
            img_path: 图像路径

        Returns:
            dict: 检测结果
        """
        img = cv2.imread(img_path)
        if img is None:
            return {'success': False, 'error': f'无法读取图像: {img_path}'}
        return self._run_inference(img)

    def predict_from_array(self, img_rgb):
        """
        从 numpy 数组进行足弓关键点检测（支持 DICOM 等已解码图像）

        Args:
            img_rgb: numpy array, RGB 格式

        Returns:
            dict: 检测结果
        """
        if img_rgb is None:
            return {'success': False, 'error': '图像数据为空'}
        # RGB 转 BGR
        img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
        return self._run_inference(img_bgr)

    def _run_inference(self, img):
        """
        执行推理核心逻辑

        Args:
            img: numpy array (BGR)

        Returns:
            dict: 检测结果
        """

        try:
            img_batch, scale, pad_x, pad_y, orig_w, orig_h = self._preprocess(img)

            outputs = self.session.run(None, {self.input_name: img_batch})
            heatmap_pred = outputs[0][0]  # (5, 200, 200)

            # 从热图恢复关键点坐标
            hm_to_input = INPUT_SIZE / HEATMAP_SIZE
            keypoints = {}
            for kp_idx in range(NUM_KEYPOINTS):
                hm = heatmap_pred[kp_idx]
                col, row = _get_subpixel_heatmap_peak(hm)
                inp_x = col * hm_to_input
                inp_y = row * hm_to_input
                ox, oy = _input_to_orig(inp_x, inp_y, scale, pad_x, pad_y)
                keypoints[kp_idx + 1] = (int(round(ox)), int(round(oy)))

            # 计算足弓角度
            medial_angle = None
            lateral_angle = None
            if all(k in keypoints for k in (1, 2, 3)):
                medial_angle = _compute_arch_angle(keypoints[2], keypoints[1], keypoints[3])
            if all(k in keypoints for k in (1, 4, 5)):
                lateral_angle = _compute_arch_angle(keypoints[5], keypoints[1], keypoints[4])

            # 生成可视化图像
            vis = img.copy()
            for kp_idx in range(NUM_KEYPOINTS):
                kp_id = kp_idx + 1
                if kp_id not in keypoints:
                    continue
                px, py = keypoints[kp_id]
                color = KEYPOINT_COLORS[kp_idx]
                cv2.circle(vis, (px, py), 4, color, -1)
                cv2.circle(vis, (px, py), 6, (255, 255, 255), 1)
                cv2.putText(vis, f"P{kp_id}", (px + 8, py + 3),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)

            # 绘制足弓连线
            if all(k in keypoints for k in (1, 2, 3)):
                pts = [keypoints[k] for k in (1, 2, 3)]
                for i in range(len(pts) - 1):
                    cv2.line(vis, pts[i], pts[i + 1], (68, 255, 68), 2)
            if all(k in keypoints for k in (1, 4, 5)):
                pts = [keypoints[k] for k in (1, 5, 4)]
                for i in range(len(pts) - 1):
                    cv2.line(vis, pts[i], pts[i + 1], (255, 170, 0), 2)

            # 生成报告（仅内侧和外侧足弓两行，整数）
            report_lines = []
            if medial_angle is not None:
                report_lines.append(f"内侧足弓角约: {int(round(medial_angle))}°")
            if lateral_angle is not None:
                report_lines.append(f"- 外侧足弓角约: {int(round(lateral_angle))}°")

            report = "\n".join(report_lines)

            return {
                'success': True,
                'keypoints': keypoints,
                'keypoint_names': KEYPOINT_NAMES,
                'medial_angle': medial_angle,
                'lateral_angle': lateral_angle,
                'result_image': vis,
                'report': report,
                'error': None,
            }

        except Exception as e:
            import traceback
            traceback.print_exc()
            return {'success': False, 'error': str(e)}


def get_foot_arch_inference(model_path=None):
    """获取足弓检测推理引擎单例"""
    global _instance
    if _instance is None:
        try:
            _instance = FootArchInference(model_path=model_path)
        except Exception as e:
            logger.error(f"足弓模型加载失败: {e}")
            return None
    return _instance
