"""
脊柱X线片检测与Cobb角测量脚本 V5 - 最终版
关键改进：
1. 更强的顶椎检测（使用曲线顶点而非简单距离）
2. 端椎选择更加鲁棒
3. 在图像上绘制清晰的Cobb角测量线和角度弧
"""

import os
import random
from pathlib import Path
from ultralytics import YOLO
import torch
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import math
from scipy.interpolate import CubicSpline

LABEL_MAP = {
    'scoliosis_spine': '脊柱侧弯',
    'normal_spine': '正常脊柱'
}

def get_vertebra_data(boxes, model_names):
    """获取所有椎体的中心点坐标和边界框"""
    vertebra_data = []
    for i, box in enumerate(boxes):
        cls_id = int(box.cls[0])
        cls_name = model_names[cls_id]
        if 'Vertebra' in cls_name or cls_id == 0:
            xyxy = box.xyxy[0].cpu().numpy()
            center_x = (xyxy[0] + xyxy[2]) / 2
            center_y = (xyxy[1] + xyxy[3]) / 2
            vertebra_data.append({
                'center': (center_x, center_y),
                'xyxy': xyxy,
                'width': xyxy[2] - xyxy[0],
                'height': xyxy[3] - xyxy[1]
            })
    vertebra_data.sort(key=lambda v: v['center'][1])
    return vertebra_data

def fit_spine_curve(vertebra_data):
    """
    使用三次样条拟合脊柱曲线 x = f(y)
    返回: (curve_function, centers_y, centers_x, curve_type)
    """
    if len(vertebra_data) < 3:
        return None, None, None, None
    
    centers_x = np.array([v['center'][0] for v in vertebra_data])
    centers_y = np.array([v['center'][1] for v in vertebra_data])
    
    try:
        cs = CubicSpline(centers_y, centers_x, bc_type='natural')
        return cs, centers_y, centers_x, 'spline'
    except Exception:
        z = np.polyfit(centers_y, centers_x, 2)
        return np.poly1d(z), centers_y, centers_x, 'poly2'

def get_tangent_angle(spine_curve, cy):
    """获取脊柱曲线在某点的切线与垂直方向的夹角"""
    if isinstance(spine_curve, np.poly1d):
        derivative = np.polyder(spine_curve)
        dx_dy = derivative(cy)
    else:
        try:
            dx_dy = float(spine_curve.derivative()(cy))
        except:
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
    """
    估算椎体终板线
    终板与脊柱该点的切线垂直
    
    返回: (endplate_angle, line_start_pt, line_end_pt)
    """
    v = vertebra_data[idx]
    cx, cy = v['center']
    
    tangent_angle = get_tangent_angle(spine_curve, cy)
    
    # 终板方向 = 切线方向 + 90度
    endplate_angle = -tangent_angle
    
    half_w = v['width'] / 2
    rad = math.radians(endplate_angle)
    dx = half_w * math.cos(rad)
    dy = half_w * math.sin(rad)
    
    p1 = (cx - dx, cy - dy)
    p2 = (cx + dx, cy + dy)
    return endplate_angle, p1, p2

def find_apex_vertebra(vertebra_data, spine_curve, curve_type):
    """
    找到顶椎(apex) - 脊柱弯曲偏离中线最远的点
    
    策略:
    - 对于二次拟合: 找到抛物线的顶点
    - 对于样条拟合: 找到偏离首尾连线最远的点
    """
    n = len(vertebra_data)
    if n < 3:
        return n // 2
    
    centers_x = np.array([v['center'][0] for v in vertebra_data])
    centers_y = np.array([v['center'][1] for v in vertebra_data])
    
    if curve_type == 'poly2' and isinstance(spine_curve, np.poly1d):
        # 二次曲线 y = ax^2 + bx + c 的顶点
        # 我们拟合的是 x = f(y)，找到导数=0的点
        derivative = np.polyder(spine_curve)
        roots = np.roots(derivative)
        apex_y = None
        for r in roots:
            if np.isreal(r) and centers_y[0] <= r <= centers_y[-1]:
                apex_y = np.real(r)
                break
        
        if apex_y is not None:
            # 找到最接近顶点y坐标的椎体
            distances = [abs(v['center'][1] - apex_y) for v in vertebra_data]
            return int(np.argmin(distances))
    
    # 通用方法: 找到偏离首尾连线最远的点
    first = vertebra_data[0]['center']
    last = vertebra_data[-1]['center']
    
    # 首尾连线的向量
    dx = last[0] - first[0]
    dy = last[1] - first[1]
    line_len = math.hypot(dx, dy)
    
    if line_len < 1:
        return n // 2
    
    max_dist = 0
    apex_idx = n // 2
    
    for i in range(1, n - 1):
        cx, cy = vertebra_data[i]['center']
        # 点到直线的距离
        cross = abs(dx * (first[1] - cy) - (first[0] - cx) * dy)
        dist = cross / line_len
        
        if dist > max_dist:
            max_dist = dist
            apex_idx = i
    
    return apex_idx

def find_end_vertebrae(vertebra_data, apex_idx, spine_curve):
    """
    找到上端椎和下端椎
    
    端椎是侧弯上下两端倾斜最大的椎体。
    在顶椎上方寻找倾斜最大的作为上端椎，
    在顶椎下方寻找倾斜最大的作为下端椎。
    """
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

def calculate_cobb_angle_v5(vertebra_data):
    """
    V5 Cobb角计算
    
    使用脊柱曲线切线估算终板方向，计算两条终板线夹角
    """
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
        
        # 计算夹角
        cobb_angle = angle_between_lines(upper_line, lower_line)
        
        if cobb_angle < 5:
            # 备选：脊柱弯曲角
            uc = vertebra_data[upper_end_idx]['center']
            ac = vertebra_data[apex_idx]['center']
            lc = vertebra_data[lower_end_idx]['center']
            
            a1 = math.degrees(math.atan2(ac[0] - uc[0], max(ac[1] - uc[1], 1)))
            a2 = math.degrees(math.atan2(lc[0] - ac[0], max(lc[1] - ac[1], 1)))
            c = abs(a1 - a2)
            if c > 90:
                c = 180 - c
            cobb_angle = max(cobb_angle, c)
        
        return cobb_angle, upper_line, lower_line, upper_end_idx, lower_end_idx, apex_idx
        
    except Exception as e:
        print(f"  Cobb角计算错误: {e}")
        return None, None, None, None, None, None

def extend_line(line, factor=2.5):
    """延长线段"""
    (x1, y1), (x2, y2) = line
    dx, dy = x2 - x1, y2 - y1
    return ((x1 - dx * factor, y1 - dy * factor), 
            (x2 + dx * factor, y2 + dy * factor))

def line_intersection(line1, line2):
    """计算两条直线的交点"""
    (x1, y1), (x2, y2) = line1
    (x3, y3), (x4, y4) = line2
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-10:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))

def draw_angle_arc(img, vertex, p1, p2, radius=60, color=(0, 255, 0), thickness=2):
    """绘制角度弧线"""
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
    """根据Cobb角大小返回颜色"""
    if angle > 40:
        return (0, 0, 255)    # 红色-重度
    elif angle > 25:
        return (0, 140, 255)  # 橙色-中度
    elif angle > 10:
        return (0, 255, 255)  # 黄色-轻度
    return (0, 255, 0)        # 绿色-正常

def draw_results(image, boxes, vertebra_data, cobb_angle, upper_line, lower_line,
                 upper_idx, lower_idx, apex_idx, model_names):
    """绘制检测结果"""
    img = image.copy()
    H, W = img.shape[:2]
    
    # 1. 先画脊柱中轴线（灰色虚线）
    if len(vertebra_data) >= 2:
        centers = [v['center'] for v in vertebra_data]
        for i in range(len(centers) - 1):
            p1 = (int(centers[i][0]), int(centers[i][1]))
            p2 = (int(centers[i+1][0]), int(centers[i+1][1]))
            cv2.line(img, p1, p2, (200, 200, 200), 1, cv2.LINE_AA)
    
    # 2. 绘制Cobb角测量线（先画线再画框，线在框下面）
    if cobb_angle is not None and upper_line and lower_line:
        upper_ext = extend_line(upper_line, 2.5)
        lower_ext = extend_line(lower_line, 2.5)
        intersection = line_intersection(upper_ext, lower_ext)
        
        cobb_color = get_cobb_color(cobb_angle)
        
        # 上终板延长线
        u1, u2 = upper_ext
        cv2.line(img, (int(u1[0]), int(u1[1])), (int(u2[0]), int(u2[1])), 
                (0, 0, 200), 1, cv2.LINE_AA)
        # 下终板延长线
        l1, l2 = lower_ext
        cv2.line(img, (int(l1[0]), int(l1[1])), (int(l2[0]), int(l2[1])), 
                (0, 0, 200), 1, cv2.LINE_AA)
        
        # 原始终板线加粗
        ul1, ul2 = upper_line
        cv2.line(img, (int(ul1[0]), int(ul1[1])), (int(ul2[0]), int(ul2[1])), 
                (0, 0, 255), 3, cv2.LINE_AA)
        ll1, ll2 = lower_line
        cv2.line(img, (int(ll1[0]), int(ll1[1])), (int(ll2[0]), int(ll2[1])), 
                (255, 0, 255), 3, cv2.LINE_AA)
        
        # 交点和角度弧线
        if intersection:
            ix, iy = intersection
            if 0 <= ix < W * 1.5 and 0 <= iy < H * 1.5:
                cv2.circle(img, (int(ix), int(iy)), 5, cobb_color, -1, cv2.LINE_AA)
                cv2.circle(img, (int(ix), int(iy)), 8, cobb_color, 2, cv2.LINE_AA)
                
                # 找距离交点近的端板端点
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
    
    # 3. 绘制椎体边界框
    for i, v in enumerate(vertebra_data):
        xyxy = v['xyxy'].astype(int)
        x1, y1, x2, y2 = xyxy
        
        if i == apex_idx:
            color, thick = (0, 255, 255), 3  # 黄色-顶椎
        elif i == upper_idx:
            color, thick = (0, 165, 255), 3  # 橙色-上端椎
        elif i == lower_idx:
            color, thick = (255, 0, 255), 3  # 紫色-下端椎
        else:
            color, thick = (128, 128, 128), 1  # 灰色-普通椎体
        
        cv2.rectangle(img, (x1, y1), (x2, y2), color, max(1, thick - 1))
        cx, cy = v['center']
        cv2.circle(img, (int(cx), int(cy)), 2, color, -1)
    
    # 4. 脊柱分类框
    for box in boxes:
        cls_id = int(box.cls[0])
        cls_name = model_names[cls_id]
        if 'spine' not in cls_name:
            continue
        xyxy = box.xyxy[0].cpu().numpy().astype(int)
        x1, y1, x2, y2 = xyxy
        
        if 'scoliosis' in cls_name:
            color, label = (0, 255, 255), f"脊柱侧弯 {float(box.conf[0]):.2f}"
        else:
            color, label = (0, 255, 0), f"正常脊柱 {float(box.conf[0]):.2f}"
        
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        _draw_chinese_text(img, label, x1, y1 - 5, color, 20)
    
    # 5. Cobb角数值显示
    if cobb_angle is not None:
        cobb_color = get_cobb_color(cobb_angle)
        _draw_cobb_label(img, cobb_angle, W, cobb_color)
    
    # 6. 图例
    _draw_legend(img, W, H)
    
    return img

def _draw_chinese_text(img, text, x, y, bg_color, font_size):
    """使用PIL绘制中文文字"""
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
    """绘制Cobb角标签"""
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    font = _get_chinese_font(36)
    
    text = f"Cobb角: {angle:.1f}°"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 15
    
    # 半透明背景
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
    """绘制图例"""
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

def _get_chinese_font(size):
    for fp in ["C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/msyh.ttc"]:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except:
                pass
    return ImageFont.load_default()

def run_inference():
    print("=" * 80)
    print("YOLOv8 脊柱检测模型 - Cobb角测量 V5 (最终版)")
    print("=" * 80)

    base = Path(__file__).parent
    model = YOLO(str(base / "runs" / "train" / "weights" / "best.pt"))
    out = base / "test_results_cobb_v5"
    out.mkdir(exist_ok=True)
    
    images = list((base / "datasets" / "images" / "test").glob("*.jpg"))
    print(f"\n加载模型: {base / 'runs' / 'train' / 'weights' / 'best.pt'}")
    print(f"测试集: {len(images)} 张")
    selected = random.sample(images, min(5, len(images)))

    for i, img_path in enumerate(selected, 1):
        print(f"\n[{i}/5] {img_path.name}")
        orig = cv2.imread(str(img_path))
        H, W = orig.shape[:2]

        results = model(str(img_path), 
                        device=0 if torch.cuda.is_available() else 'cpu',
                        conf=0.25, iou=0.45, verbose=False)
        for r in results:
            vd = get_vertebra_data(r.boxes, model.names)
            ca, ul, ll, uidx, lidx, aidx = calculate_cobb_angle_v5(vd)
            
            if vd:
                print(f"  椎体: {len(vd)}个  |  "
                      f"上端椎#{uidx} 顶椎#{aidx} 下端椎#{lidx}")
                for b in r.boxes:
                    cn = model.names[int(b.cls[0])]
                    if 'spine' in cn:
                        print(f"  分类: {LABEL_MAP.get(cn, cn)} (置信度: {float(b.conf[0]):.3f})")
                if ca is not None:
                    print(f"  Cobb角: {ca:.1f}°")
                else:
                    print(f"  Cobb角: 无法计算")
            
            ann = draw_results(orig, r.boxes, vd, ca, ul, ll, uidx, lidx, aidx, model.names)
            of = out / f"result_{i:02d}_{img_path.name}"
            cv2.imwrite(str(of), ann)
            print(f"  已保存: {of.name}")

    print(f"\n{'='*80}")
    print(f"推理完成! 结果: {out}")
    print(f"{'='*80}")

if __name__ == "__main__":
    run_inference()
