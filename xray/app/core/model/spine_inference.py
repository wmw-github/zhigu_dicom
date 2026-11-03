"""
脊柱侧弯检测与Cobb角测量推理模块 - ONNX版本（Win7兼容）

基于YOLOv8的脊柱X线片检测系统
模型由Win10训练并导出为ONNX格式
使用纯onnxruntime推理，不依赖ultralytics/torch
"""
import os
import cv2
import math
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from scipy.interpolate import CubicSpline

from utils.image_io import imread_cn
from utils.logger import get_logger

logger = get_logger(__name__)

SPINE_MODEL_DIR = Path(__file__).parent.parent.parent / "models" / "spine"
SPINE_MODEL_PATH = SPINE_MODEL_DIR / "best.onnx"

LABEL_MAP = {
    'scoliosis_spine': '脊柱侧弯',
    'normal_spine': '正常脊柱'
}


class SpineInference:
    """脊柱侧弯检测推理类（纯ONNX，兼容Win7）"""

    INPUT_SIZE = 640

    def __init__(self):
        self.session = None
        self.input_name = None
        self.output_name = None
        self.model_names = {}
        self._load_model()

    def _load_model(self):
        try:
            import onnxruntime as ort

            if not SPINE_MODEL_PATH.exists():
                raise FileNotFoundError(
                    f"未找到脊柱检测ONNX模型: {SPINE_MODEL_PATH}\n"
                    f"请先在Win10上训练并导出ONNX模型"
                )

            self.session = ort.InferenceSession(
                str(SPINE_MODEL_PATH),
                providers=['CPUExecutionProvider']
            )
            self.input_name = self.session.get_inputs()[0].name
            self.output_name = self.session.get_outputs()[0].name

            meta = self.session.get_modelmeta()
            if hasattr(meta, 'custom_metadata_map') and 'names' in meta.custom_metadata_map:
                import ast
                try:
                    self.model_names = ast.literal_eval(meta.custom_metadata_map['names'])
                except Exception:
                    pass

            logger.info(f"脊柱检测模型(ONNX)加载完成: {SPINE_MODEL_PATH}")
        except Exception as e:
            logger.error(f"加载脊柱检测模型失败: {e}")
            raise

    def warmup(self):
        if self.session is None:
            logger.warning("脊柱检测模型未加载，跳过预热")
            return
        try:
            dummy = np.zeros((1, 3, self.INPUT_SIZE, self.INPUT_SIZE), dtype=np.float32)
            self.session.run([self.output_name], {self.input_name: dummy})
            logger.info("脊柱检测模型预热完成")
        except Exception as e:
            logger.error(f"脊柱检测模型预热失败: {e}")

    def _preprocess(self, img_bgr: np.ndarray) -> Tuple[np.ndarray, float, Tuple[int, int]]:
        h, w = img_bgr.shape[:2]
        scale = min(self.INPUT_SIZE / w, self.INPUT_SIZE / h)
        new_w = int(w * scale)
        new_h = int(h * scale)

        resized = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.INPUT_SIZE, self.INPUT_SIZE, 3), 114, dtype=np.uint8)
        canvas[:new_h, :new_w] = resized

        img_rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        tensor = img_rgb.transpose(2, 0, 1).astype(np.float32) / 255.0
        tensor = np.expand_dims(tensor, 0)
        return tensor, scale, (new_w, new_h)

    def _postprocess(self, raw_output: np.ndarray, scale: float,
                     pad: Tuple[int, int], orig_w: int, orig_h: int,
                     conf_thresh: float, iou_thresh: float) -> Dict:
        new_w, new_h = pad
        pred = np.squeeze(raw_output).T

        if pred.ndim == 1:
            return {'boxes': [], 'vertebra_data': [], 'spine_class': None}

        num_outputs = pred.shape[1]
        scores = np.max(pred[:, 4:], axis=1)
        mask = scores > conf_thresh
        pred = pred[mask]
        scores = scores[mask]

        if len(pred) == 0:
            return {'boxes': [], 'vertebra_data': [], 'spine_class': None}

        class_ids = np.argmax(pred[:, 4:], axis=1)

        cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        x1 = cx - bw / 2
        y1 = cy - bh / 2
        x2 = cx + bw / 2
        y2 = cy + bh / 2

        x1 = np.clip(x1, 0, new_w)
        y1 = np.clip(y1, 0, new_h)
        x2 = np.clip(x2, 0, new_w)
        y2 = np.clip(y2, 0, new_h)

        x1o = (x1 / scale).astype(int)
        y1o = (y1 / scale).astype(int)
        x2o = (x2 / scale).astype(int)
        y2o = (y2 / scale).astype(int)
        boxes_orig = np.stack([x1o, y1o, x2o, y2o], axis=1)

        boxes_xywh = [[int(b[0]), int(b[1]), int(b[2]-b[0]), int(b[3]-b[1])] for b in boxes_orig]
        indices = cv2.dnn.NMSBoxes(boxes_xywh, scores.tolist(), conf_thresh, iou_thresh)

        boxes = []
        if len(indices) > 0:
            for i in indices.flatten():
                bx1, by1, bx2, by2 = boxes_orig[i]
                if bx2 <= bx1 or by2 <= by1:
                    continue
                cls_id = int(class_ids[i])
                cls_name = self.model_names.get(cls_id, str(cls_id))
                boxes.append({
                    'cls_id': cls_id,
                    'cls_name': cls_name,
                    'confidence': float(scores[i]),
                    'xyxy': np.array([bx1, by1, bx2, by2]),
                    'cx': (bx1 + bx2) // 2,
                    'cy': (by1 + by2) // 2,
                    'width': bx2 - bx1,
                    'height': by2 - by1,
                })

        vertebra_data = []
        spine_class = None
        for box in boxes:
            cls_name = box['cls_name']
            if 'Vertebra' in cls_name or box['cls_id'] == 0:
                vertebra_data.append(box)
            elif 'spine' in cls_name:
                if spine_class is None or box['confidence'] > spine_class['confidence']:
                    spine_class = box

        vertebra_data.sort(key=lambda v: v['cy'])
        return {
            'boxes': boxes,
            'vertebra_data': vertebra_data,
            'spine_class': spine_class,
        }

    def detect(self, img_path: str, conf_threshold: float = 0.25,
               iou_threshold: float = 0.45) -> Dict:
        result = {
            'success': False,
            'spine_class': None,
            'cobb_angle': None,
            'vertebra_count': 0,
            'upper_end_idx': None,
            'lower_end_idx': None,
            'apex_idx': None,
            'result_image': None,
            'report': '',
            'error': None,
        }

        try:
            original_img = self._load_image(img_path)
            if original_img is None:
                raise ValueError("无法加载图像")
            H, W = original_img.shape[:2]

            tensor, scale, pad = self._preprocess(original_img)
            raw_output = self.session.run([self.output_name], {self.input_name: tensor})[0]
            detections = self._postprocess(raw_output, scale, pad, W, H, conf_threshold, iou_threshold)

            vd = detections['vertebra_data']
            spine_class = detections['spine_class']

            cobb_angle, upper_line, lower_line, uidx, lidx, aidx = calculate_cobb_angle(vd)

            annotated_img = draw_results(
                original_img.copy(), detections['boxes'], vd,
                cobb_angle, upper_line, lower_line,
                uidx, lidx, aidx, self.model_names
            )

            report = _generate_report(spine_class, cobb_angle, len(vd), uidx, lidx, aidx)

            # 两条终板线各两个端点（1/2 上端椎，3/4 下端椎）。这个不是给
            # 画图用的（标注图里已经画好了），是给上层「手动微调 Cobb 线」
            # 用的：把四个端点交出去，用户拖完端点重算的 Cobb 角就能与模型
            # 口径对齐。坐标一律是原图像素（行序向上/下无关，cv2 默认）。
            cobb_points = {}
            for i, line in enumerate((upper_line, lower_line)):
                for j, pt in enumerate(line or ()):
                    cobb_points[(i * 2 + j) + 1] = (
                        float(pt[0]), float(pt[1]))

            result.update({
                'success': True,
                'spine_class': spine_class,
                'cobb_angle': cobb_angle,
                'vertebra_count': len(vd),
                'upper_end_idx': uidx,
                'lower_end_idx': lidx,
                'apex_idx': aidx,
                'upper_line': upper_line,
                'lower_line': lower_line,
                'cobb_points': cobb_points,
                'result_image': annotated_img,
                'report': report,
            })

            logger.info(f"脊柱检测完成: Cobb角={cobb_angle}°, 椎体数={len(vd)}")

        except Exception as e:
            logger.error(f"脊柱检测失败: {e}")
            result['error'] = str(e)

        return result

    def _load_image(self, img_path: str) -> Optional[np.ndarray]:
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
                logger.warning(f"DICOM读取失败，尝试普通方式: {e}")

        img = imread_cn(img_path)
        if img is None:
            raise ValueError(f"无法读取图像文件: {img_path}")
        return img


def fit_spine_curve(vertebra_data):
    if len(vertebra_data) < 3:
        return None, None, None, None
    centers_x = np.array([v['cx'] for v in vertebra_data])
    centers_y = np.array([v['cy'] for v in vertebra_data])
    try:
        cs = CubicSpline(centers_y, centers_x, bc_type='natural')
        return cs, centers_y, centers_x, 'spline'
    except Exception:
        z = np.polyfit(centers_y, centers_x, 2)
        return np.poly1d(z), centers_y, centers_x, 'poly2'


def get_tangent_angle(spine_curve, cy):
    if isinstance(spine_curve, np.poly1d):
        derivative = np.polyder(spine_curve)
        dx_dy = derivative(cy)
    else:
        try:
            dx_dy = float(spine_curve.derivative()(cy))
        except Exception:
            dx_dy = 0
    return math.degrees(math.atan2(dx_dy, 1.0))


def normalize_line_angle(angle):
    """Normalize an unoriented line angle to [-90, 90)."""
    return (angle + 90) % 180 - 90


def angle_between_orientations(angle1, angle2):
    """Return the acute angle between two unoriented lines."""
    diff = abs(normalize_line_angle(angle1) - normalize_line_angle(angle2))
    return 180 - diff if diff > 90 else diff


def angle_between_lines(line1, line2):
    (a1, a2), (b1, b2) = line1, line2
    angle1 = math.degrees(math.atan2(a2[1] - a1[1], a2[0] - a1[0]))
    angle2 = math.degrees(math.atan2(b2[1] - b1[1], b2[0] - b1[0]))
    return angle_between_orientations(angle1, angle2)


def estimate_endplate(vertebra_data, idx, spine_curve):
    v = vertebra_data[idx]
    cx, cy = v['cx'], v['cy']
    tangent_angle = get_tangent_angle(spine_curve, cy)
    endplate_angle = -tangent_angle
    half_w = v['width'] / 2
    rad = math.radians(endplate_angle)
    dx = half_w * math.cos(rad)
    dy = half_w * math.sin(rad)
    p1 = (cx - dx, cy - dy)
    p2 = (cx + dx, cy + dy)
    return endplate_angle, p1, p2


def find_apex_vertebra(vertebra_data, spine_curve, curve_type):
    n = len(vertebra_data)
    if n < 3:
        return n // 2
    centers_x = np.array([v['cx'] for v in vertebra_data])
    centers_y = np.array([v['cy'] for v in vertebra_data])

    if curve_type == 'poly2' and isinstance(spine_curve, np.poly1d):
        derivative = np.polyder(spine_curve)
        roots = np.roots(derivative)
        apex_y = None
        for r in roots:
            if np.isreal(r) and centers_y[0] <= r <= centers_y[-1]:
                apex_y = np.real(r)
                break
        if apex_y is not None:
            distances = [abs(v['cy'] - apex_y) for v in vertebra_data]
            return int(np.argmin(distances))

    first = (vertebra_data[0]['cx'], vertebra_data[0]['cy'])
    last = (vertebra_data[-1]['cx'], vertebra_data[-1]['cy'])
    dx = last[0] - first[0]
    dy = last[1] - first[1]
    line_len = math.hypot(dx, dy)
    if line_len < 1:
        return n // 2

    max_dist = 0
    apex_idx = n // 2
    for i in range(1, n - 1):
        cx, cy = vertebra_data[i]['cx'], vertebra_data[i]['cy']
        cross = abs(dx * (first[1] - cy) - (first[0] - cx) * dy)
        dist = cross / line_len
        if dist > max_dist:
            max_dist = dist
            apex_idx = i
    return apex_idx


def find_end_vertebrae(vertebra_data, apex_idx, spine_curve):
    n = len(vertebra_data)
    if n < 3:
        return 0, n - 1

    endplate_tilts = []
    for i in range(n):
        angle, _, _ = estimate_endplate(vertebra_data, i, spine_curve)
        endplate_tilts.append(normalize_line_angle(angle))

    upper_range = list(range(0, apex_idx))
    lower_range = list(range(apex_idx + 1, n))
    if not upper_range or not lower_range:
        return 0, n - 1

    best_pair = (upper_range[0], lower_range[-1])
    best_angle = -1
    for upper_idx in upper_range:
        for lower_idx in lower_range:
            angle = angle_between_orientations(
                endplate_tilts[upper_idx],
                endplate_tilts[lower_idx]
            )
            if angle > best_angle:
                best_angle = angle
                best_pair = (upper_idx, lower_idx)

    return best_pair


def calculate_cobb_angle(vertebra_data):
    if len(vertebra_data) < 4:
        return None, None, None, None, None, None

    try:
        spine_curve, _, _, curve_type = fit_spine_curve(vertebra_data)
        if spine_curve is None:
            return None, None, None, None, None, None

        apex_idx = find_apex_vertebra(vertebra_data, spine_curve, curve_type)
        upper_end_idx, lower_end_idx = find_end_vertebrae(vertebra_data, apex_idx, spine_curve)

        _, upper_p1, upper_p2 = estimate_endplate(vertebra_data, upper_end_idx, spine_curve)
        _, lower_p1, lower_p2 = estimate_endplate(vertebra_data, lower_end_idx, spine_curve)

        upper_line = (upper_p1, upper_p2)
        lower_line = (lower_p1, lower_p2)

        cobb_angle = angle_between_lines(upper_line, lower_line)

        if cobb_angle < 5:
            uc = (vertebra_data[upper_end_idx]['cx'], vertebra_data[upper_end_idx]['cy'])
            ac = (vertebra_data[apex_idx]['cx'], vertebra_data[apex_idx]['cy'])
            lc = (vertebra_data[lower_end_idx]['cx'], vertebra_data[lower_end_idx]['cy'])
            a1 = math.degrees(math.atan2(ac[0] - uc[0], max(ac[1] - uc[1], 1)))
            a2 = math.degrees(math.atan2(lc[0] - ac[0], max(lc[1] - ac[1], 1)))
            c = abs(a1 - a2)
            if c > 90:
                c = 180 - c
            cobb_angle = max(cobb_angle, c)

        return cobb_angle, upper_line, lower_line, upper_end_idx, lower_end_idx, apex_idx

    except Exception as e:
        logger.error(f"Cobb角计算错误: {e}")
        return None, None, None, None, None, None


def extend_line(line, factor=2.5):
    (x1, y1), (x2, y2) = line
    dx, dy = x2 - x1, y2 - y1
    return ((x1 - dx * factor, y1 - dy * factor),
            (x2 + dx * factor, y2 + dy * factor))


def line_intersection(line1, line2):
    (x1, y1), (x2, y2) = line1
    (x3, y3), (x4, y4) = line2
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-10:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def draw_angle_arc(img, vertex, p1, p2, radius=60, color=(0, 255, 0), thickness=2):
    vx, vy = vertex
    a1 = math.atan2(p1[1] - vy, p1[0] - vx)
    a2 = math.atan2(p2[1] - vy, p2[0] - vx)
    da = a2 - a1
    if da > math.pi:
        da -= 2 * math.pi
    elif da < -math.pi:
        da += 2 * math.pi
    steps = max(20, int(abs(da) * 15))
    for i in range(steps):
        t = a1 + da * i / (steps - 1)
        x = int(vx + radius * math.cos(t))
        y = int(vy + radius * math.sin(t))
        if i > 0:
            cv2.line(img, prev, (x, y), color, thickness)
        prev = (x, y)


def get_cobb_color(angle):
    if angle > 40:
        return (0, 0, 255)
    elif angle > 25:
        return (0, 140, 255)
    elif angle > 10:
        return (0, 255, 255)
    return (0, 255, 0)


def _get_chinese_font(size):
    from PIL import ImageFont
    for fp in ["C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/msyh.ttc"]:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _draw_chinese_text(img, text, x, y, bg_color, font_size):
    from PIL import Image, ImageDraw
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    font = _get_chinese_font(font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    ly = y - th - 5 if y - th - 5 > 10 else y + 5
    bg_pil = (bg_color[2], bg_color[1], bg_color[0])
    draw.rectangle([x, ly, x + tw + 4, ly + th + 4], fill=bg_pil)
    draw.text((x + 2, ly + 2), text, fill=(0, 0, 0), font=font)
    result = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    img[...] = result


def _draw_cobb_label(img, angle, W, color):
    from PIL import Image, ImageDraw
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    font = _get_chinese_font(36)
    text = f"Cobb角: {angle:.1f}°"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 15
    overlay = Image.new('RGBA', img_pil.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([W//2 - tw//2 - pad, 20, W//2 + tw//2 + pad, 20 + th + pad * 2],
                 fill=(0, 0, 0, 180))
    img_pil = Image.alpha_composite(img_pil.convert('RGBA'), overlay).convert('RGB')
    draw = ImageDraw.Draw(img_pil)
    rgb_color = (color[2], color[1], color[0])
    draw.text((W//2 - tw//2, 20 + pad), text, fill=rgb_color, font=font)
    result = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    img[...] = result


def _draw_legend(img, W, H):
    from PIL import Image, ImageDraw
    items = [
        ("灰色框: 椎体", (128, 128, 128)),
        ("橙色框: 上端椎", (0, 165, 255)),
        ("紫色框: 下端椎", (255, 0, 255)),
        ("黄色框: 顶椎", (0, 255, 255)),
        ("红线: Cobb角测量线", (0, 0, 255)),
    ]
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    font = _get_chinese_font(14)
    draw = ImageDraw.Draw(img_pil)
    lx, ly = W - 260, H - 115
    overlay = Image.new('RGBA', img_pil.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([lx - 10, ly - 8, lx + 250, ly + len(items) * 20 + 5],
                 fill=(0, 0, 0, 140))
    img_pil = Image.alpha_composite(img_pil.convert('RGBA'), overlay).convert('RGB')
    draw = ImageDraw.Draw(img_pil)
    for j, (text, bgr) in enumerate(items):
        yp = ly + j * 20
        rgb = (bgr[2], bgr[1], bgr[0])
        draw.rectangle([lx, yp + 2, lx + 15, yp + 15], fill=rgb)
        draw.text((lx + 22, yp), text, fill=(255, 255, 255), font=font)
    result = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    img[...] = result


def draw_results(image, boxes, vertebra_data, cobb_angle, upper_line, lower_line,
                 upper_idx, lower_idx, apex_idx, model_names):
    img = image.copy()
    H, W = img.shape[:2]

    if len(vertebra_data) >= 2:
        for i in range(len(vertebra_data) - 1):
            p1 = (int(vertebra_data[i]['cx']), int(vertebra_data[i]['cy']))
            p2 = (int(vertebra_data[i+1]['cx']), int(vertebra_data[i+1]['cy']))
            cv2.line(img, p1, p2, (200, 200, 200), 1, cv2.LINE_AA)

    if cobb_angle is not None and upper_line and lower_line:
        upper_ext = extend_line(upper_line, 2.5)
        lower_ext = extend_line(lower_line, 2.5)
        intersection = line_intersection(upper_ext, lower_ext)
        cobb_color = get_cobb_color(cobb_angle)

        u1, u2 = upper_ext
        cv2.line(img, (int(u1[0]), int(u1[1])), (int(u2[0]), int(u2[1])),
                 (0, 0, 200), 1, cv2.LINE_AA)
        l1, l2 = lower_ext
        cv2.line(img, (int(l1[0]), int(l1[1])), (int(l2[0]), int(l2[1])),
                 (0, 0, 200), 1, cv2.LINE_AA)

        ul1, ul2 = upper_line
        cv2.line(img, (int(ul1[0]), int(ul1[1])), (int(ul2[0]), int(ul2[1])),
                 (0, 0, 255), 3, cv2.LINE_AA)
        ll1, ll2 = lower_line
        cv2.line(img, (int(ll1[0]), int(ll1[1])), (int(ll2[0]), int(ll2[1])),
                 (255, 0, 255), 3, cv2.LINE_AA)

        if intersection:
            ix, iy = intersection
            if 0 <= ix < W * 1.5 and 0 <= iy < H * 1.5:
                cv2.circle(img, (int(ix), int(iy)), 5, cobb_color, -1, cv2.LINE_AA)
                cv2.circle(img, (int(ix), int(iy)), 8, cobb_color, 2, cv2.LINE_AA)

                d1 = math.hypot(upper_line[0][0] - ix, upper_line[0][1] - iy)
                d2 = math.hypot(upper_line[1][0] - ix, upper_line[1][1] - iy)
                ref_upper = upper_line[0] if d1 < d2 else upper_line[1]
                d3 = math.hypot(lower_line[0][0] - ix, lower_line[0][1] - iy)
                d4 = math.hypot(lower_line[1][0] - ix, lower_line[1][1] - iy)
                ref_lower = lower_line[0] if d3 < d4 else lower_line[1]
                draw_angle_arc(img, (int(ix), int(iy)),
                               (int(ref_upper[0]), int(ref_upper[1])),
                               (int(ref_lower[0]), int(ref_lower[1])),
                               70, cobb_color, 2)

    for i, v in enumerate(vertebra_data):
        xyxy = v['xyxy'].astype(int)
        x1, y1, x2, y2 = xyxy
        if i == apex_idx:
            color, thick = (0, 255, 255), 3
        elif i == upper_idx:
            color, thick = (0, 165, 255), 3
        elif i == lower_idx:
            color, thick = (255, 0, 255), 3
        else:
            color, thick = (128, 128, 128), 1
        cv2.rectangle(img, (x1, y1), (x2, y2), color, max(1, thick - 1))
        cx, cy = v['cx'], v['cy']
        cv2.circle(img, (int(cx), int(cy)), 2, color, -1)

    for box in boxes:
        cls_name = box['cls_name']
        if 'spine' not in cls_name:
            continue
        xyxy = box['xyxy'].astype(int)
        x1, y1, x2, y2 = xyxy
        if 'scoliosis' in cls_name:
            color, label = (0, 255, 255), f"脊柱侧弯 {box['confidence']:.2f}"
        else:
            color, label = (0, 255, 0), f"正常脊柱 {box['confidence']:.2f}"
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        _draw_chinese_text(img, label, x1, y1 - 5, color, 20)

    if cobb_angle is not None:
        cobb_color = get_cobb_color(cobb_angle)
        _draw_cobb_label(img, cobb_angle, W, cobb_color)

    _draw_legend(img, W, H)
    return img


def _generate_report(spine_class, cobb_angle, vertebra_count,
                     upper_idx, lower_idx, apex_idx):
    lines = ["<b>脊柱检测结果</b><br>"]

    if spine_class:
        cls_name = spine_class['cls_name']
        cn_name = LABEL_MAP.get(cls_name, cls_name)
        conf = spine_class['confidence']
        if 'scoliosis' in cls_name:
            lines.append(f"<br><b style='color: red;'>分类: {cn_name} (置信度: {conf:.1%})</b>")
        else:
            lines.append(f"<br><b style='color: green;'>分类: {cn_name} (置信度: {conf:.1%})</b>")
    else:
        lines.append("<br>未检测到脊柱分类结果")

    lines.append(f"<br>检测到椎体数: {vertebra_count} 个")

    if cobb_angle is not None:
        if cobb_angle > 40:
            severity = "重度侧弯"
            color = "red"
        elif cobb_angle > 25:
            severity = "中度侧弯"
            color = "orange"
        elif cobb_angle > 10:
            severity = "轻度侧弯"
            color = "yellow"
        else:
            severity = "正常范围"
            color = "green"

        lines.append(f"<br><b style='color: {color}; font-size: 16px;'>Cobb角: {cobb_angle:.1f}° ({severity})</b>")
        if upper_idx is not None and lower_idx is not None:
            lines.append(f"<br>上端椎: #{upper_idx + 1} | 下端椎: #{lower_idx + 1}")
            if apex_idx is not None:
                lines.append(f" | 顶椎: #{apex_idx + 1}")
    else:
        if vertebra_count < 4:
            lines.append("<br><b style='color: gray;'>Cobb角: 无法计算（椎体数量不足，需要至少4个）</b>")
        else:
            lines.append("<br><b style='color: gray;'>Cobb角: 无法计算</b>")

    lines.append("<br><br><b>临床意义:</b>")
    lines.append("<br>• Cobb角 < 10°: 正常生理弯曲")
    lines.append("<br>• Cobb角 10°-25°: 轻度侧弯，建议定期随访")
    lines.append("<br>• Cobb角 25°-40°: 中度侧弯，建议支具治疗")
    lines.append("<br>• Cobb角 > 40°: 重度侧弯，可能需要手术评估")
    lines.append("<br><br><b>注意:</b> 本结果仅供参考，请结合临床综合判断")

    return "".join(lines)


_spine_instance = None


def get_spine_inference() -> Optional[SpineInference]:
    global _spine_instance
    if _spine_instance is None:
        try:
            _spine_instance = SpineInference()
        except Exception as e:
            logger.error(f"初始化脊柱检测推理器失败: {e}")
            return None
    return _spine_instance
