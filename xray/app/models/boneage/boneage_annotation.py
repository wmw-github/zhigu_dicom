"""
RUS-CHN骨龄标注模块

按照中华05骨龄评估标准(RUS-CHN法)进行关节标注
评估13个关键关节的骨化程度

手指编号说明：
- 第1指：拇指 (Thumb)
- 第2指：食指 (Index)
- 第3指：中指 (Middle)
- 第4指：无名指 (Ring)
- 第5指：小指 (Little)

左手X光片特征（手掌向下，标准PA位）：
- 桡骨在尺骨的桡侧（拇指侧），即x坐标较大
- 尺骨在桡骨的尺侧（小指侧），即x坐标较小
- 手指从左到右（x坐标升序）：小指(5) -> 无名指(4) -> 中指(3) -> 食指(2) -> 拇指(1)

映射算法说明（空间关联法）：
1. 先用MCPFirst锚定拇指，识别PIPFirst和DIPFirst
2. 对非拇指手指，通过PIP-MCP垂直对齐关系确定每个手指的身份
3. 按x坐标排序后，从左到右依次为：小指(5)、无名指(4)、中指(3)、食指(2)
"""

import cv2
import numpy as np
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from pathlib import Path

def _load_config():
    """从config.txt加载GRADE_NAMES配置"""
    config_path = Path(__file__).parent.parent.parent / 'config.txt'
    config_dict = {}
    
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                exec(f.read(), config_dict)
            
            GRADE_NAMES = config_dict.get('GRADE_NAMES', {})
            if GRADE_NAMES:
                return GRADE_NAMES
        except Exception:
            pass
    
    return {}

GRADE_NAMES = _load_config()


@dataclass
class JointDetection:
    class_name: str
    confidence: float
    box: Tuple[int, int, int, int]
    cx: int
    cy: int
    img_crop: np.ndarray


RUSCHN_JOINTS = {
    'Radius': {'name': '桡骨', 'finger': 0, 'type': 'bone'},
    'Ulna': {'name': '尺骨', 'finger': 0, 'type': 'bone'},
    'MCPFirst': {'name': '第一掌骨', 'finger': 1, 'type': 'MCP'},
    'MCPThird': {'name': '第三掌骨', 'finger': 3, 'type': 'MCP'},
    'MCPFifth': {'name': '第五掌骨', 'finger': 5, 'type': 'MCP'},
    'PIPFirst': {'name': '第一近节指骨', 'finger': 1, 'type': 'PIP'},
    'PIPThird': {'name': '第三近节指骨', 'finger': 3, 'type': 'PIP'},
    'PIPFifth': {'name': '第五近节指骨', 'finger': 5, 'type': 'PIP'},
    'MIPThird': {'name': '第三中节指骨', 'finger': 3, 'type': 'MIP'},
    'MIPFifth': {'name': '第五中节指骨', 'finger': 5, 'type': 'MIP'},
    'DIPFirst': {'name': '第一远节指骨', 'finger': 1, 'type': 'DIP'},
    'DIPThird': {'name': '第三远节指骨', 'finger': 3, 'type': 'DIP'},
    'DIPFifth': {'name': '第五远节指骨', 'finger': 5, 'type': 'DIP'},
}

FINGER_NAMES = {
    1: '拇指',
    2: '食指',
    3: '中指',
    4: '无名指',
    5: '小指'
}

SCORE_TABLE = {
    'girl': {
        'Radius': [10, 15, 22, 25, 40, 59, 91, 125, 138, 178, 192, 199, 203, 210],
        'Ulna': [27, 31, 36, 50, 73, 95, 120, 157, 168, 176, 182, 189],
        'MCPFirst': [5, 7, 10, 16, 23, 28, 34, 41, 47, 53, 66],
        'MCPThird': [3, 5, 6, 9, 14, 21, 32, 40, 47, 51],
        'MCPFifth': [4, 5, 7, 10, 15, 22, 33, 43, 47, 51],
        'PIPFirst': [6, 7, 8, 11, 17, 26, 32, 38, 45, 53, 60, 67],
        'PIPThird': [3, 5, 7, 9, 15, 20, 25, 29, 35, 41, 46, 51],
        'PIPFifth': [4, 5, 7, 11, 18, 21, 25, 29, 34, 40, 45, 50],
        'MIPThird': [4, 5, 7, 10, 16, 21, 25, 29, 35, 43, 46, 51],
        'MIPFifth': [3, 5, 7, 12, 19, 23, 27, 32, 35, 39, 43, 49],
        'DIPFirst': [5, 6, 8, 10, 20, 31, 38, 44, 45, 52, 67],
        'DIPThird': [3, 5, 7, 10, 16, 24, 30, 33, 36, 39, 49],
        'DIPFifth': [5, 6, 7, 11, 18, 25, 29, 33, 35, 39, 49]
    },
    'boy': {
        'Radius': [8, 11, 15, 18, 31, 46, 76, 118, 135, 171, 188, 197, 201, 209],
        'Ulna': [25, 30, 35, 43, 61, 80, 116, 157, 168, 180, 187, 194],
        'MCPFirst': [4, 5, 8, 16, 22, 26, 34, 39, 45, 52, 66],
        'MCPThird': [3, 4, 5, 8, 13, 19, 30, 38, 44, 51],
        'MCPFifth': [3, 4, 6, 9, 14, 19, 31, 41, 46, 50],
        'PIPFirst': [4, 5, 7, 11, 17, 23, 29, 36, 44, 52, 59, 66],
        'PIPThird': [3, 4, 5, 8, 14, 19, 23, 28, 34, 40, 45, 50],
        'PIPFifth': [3, 4, 6, 10, 16, 19, 24, 28, 33, 40, 44, 50],
        'MIPThird': [3, 4, 5, 9, 14, 18, 23, 28, 35, 42, 45, 50],
        'MIPFifth': [3, 4, 6, 11, 17, 21, 26, 31, 36, 40, 43, 49],
        'DIPFirst': [4, 5, 6, 9, 19, 28, 36, 43, 46, 51, 67],
        'DIPThird': [3, 4, 5, 9, 15, 23, 29, 33, 37, 40, 49],
        'DIPFifth': [3, 4, 6, 11, 17, 23, 29, 32, 36, 40, 49]
    }
}


# 检测器输出的类别名 -> 真实解剖类别（2026-11 校正，务必不要按字面理解）
#
# 依据一：数量自证。检测器稳定输出 PROX=5 / MID=4 / MCP=5 / DIST=4 / MC1=1。
#   解剖上 远节指骨=5（五指都有）、中节=4（拇指没有）、近节=5、掌骨第2~5=4。
#   若按字面理解则得到「远节只有4个、掌骨有5个」，解剖上不可能，说明整体错开一格。
# 依据二：逐框目视。把每个框裁出来放大看，PROX 的 5 个框都框在「带甲粗隆的指尖」，
#   DIST 的 4 个框都框在掌骨头上；拇指放大后 PROX 覆盖远端指骨、MCP 覆盖近端指骨。
# 依据三：分类模型反向验证。cls_DIP 喂真远节时等级随年龄单调上升，喂掌骨恒为满级。
DETECTOR_CLASS_ANATOMY = {
    'ProximalPhalanx': 'distal',      # 实际是「远节指骨」
    'MiddlePhalanx': 'middle',        # 实际是「中节指骨」（唯一与字面相符的）
    'MCP': 'proximal',                # 实际是「近节指骨」
    'DistalPhalanx': 'metacarpal',    # 实际是「掌骨第2~5」
    'MCPFirst': 'mc1',                # 实际是「第一掌骨」
    'Radius': 'radius',
    'Ulna': 'ulna',
}


def _rank_pair(a_list: List['JointDetection'], b_list: List['JointDetection']):
    """按 cx 升序做「名次配对」。

    手指在片子上是扇形张开的，同一根手指从指尖到手心 x 会漂移 100~200px，
    所以不能按「最近邻」配对（会把隔壁手指抢走），只能按左右名次一一对应。
    """
    a = sorted(a_list, key=lambda d: d.cx)
    b = sorted(b_list, key=lambda d: d.cx)
    return list(zip(a, b))


def _pick_thumb_distal(distal: List['JointDetection'], middle: List['JointDetection']):
    """挑出拇指的远节指骨：拇指没有中节指骨，所以「配不上中节」的那一列就是拇指。

    不用 MCPFirst 的 cx 当锚点 —— 第一掌骨骨骺在掌骨基底部（靠近腕部），
    cx 靠近手的中线，按距离找会抓到食指（这是老代码的第二个 bug）。
    """
    if not distal:
        return None
    if len(distal) < 2 or not middle:
        return max(distal, key=lambda d: d.cx)
    best, best_cost = None, None
    for cand in distal:
        rest = [d for d in distal if d is not cand]
        if len(rest) != len(middle):
            continue
        cost = sum(abs(a.cx - b.cx) for a, b in _rank_pair(rest, middle))
        if best_cost is None or cost < best_cost:
            best, best_cost = cand, cost
    return best if best is not None else max(distal, key=lambda d: d.cx)


class RUSCHNAnnotator:
    """RUS-CHN骨龄标注器"""

    def __init__(self):
        self.detections: Dict[str, List[JointDetection]] = defaultdict(list)
        self.hand_side: Optional[str] = None

    def parse_yolo_results(self, results, original_img: np.ndarray) -> Dict[str, List[JointDetection]]:
        self.detections = defaultdict(list)
        for result in results:
            for box in result.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf)
                cls = int(box.cls)
                cls_name = result.names[cls]
                if x2 > x1 and y2 > y1:
                    img_crop = original_img[y1:y2, x1:x2].copy()
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                    detection = JointDetection(
                        class_name=cls_name,
                        confidence=conf,
                        box=(x1, y1, x2, y2),
                        cx=cx,
                        cy=cy,
                        img_crop=img_crop
                    )
                    self.detections[cls_name].append(detection)
        for cls_name in self.detections:
            self.detections[cls_name].sort(key=lambda d: d.box[0])
        return self.detections

    def determine_hand_side(self) -> str:
        radius_dets = self.detections.get('Radius', [])
        ulna_dets = self.detections.get('Ulna', [])
        if not radius_dets or not ulna_dets:
            raise ValueError("未检测到桡骨和尺骨，无法判断左右手")
        radius_x = min(d.box[0] for d in radius_dets)
        ulna_x = min(d.box[0] for d in ulna_dets)
        if ulna_x < radius_x:
            self.hand_side = 'L'
        else:
            self.hand_side = 'R'
        return self.hand_side

    def _find_closest_to_ref(self, dets: List[JointDetection], ref_cx: int) -> Optional[JointDetection]:
        if not dets:
            return None
        return min(dets, key=lambda d: abs(d.cx - ref_cx))

    def _find_closest_below(self, dets: List[JointDetection], ref_cx: int, ref_cy: int,
                             max_x_diff: int = 200) -> Optional[JointDetection]:
        """
        在检测结果列表中找到位于参考点下方且x坐标接近的检测
        用于PIP-MCP垂直对齐匹配
        
        Args:
            dets: 候选检测列表
            ref_cy: 参考点的y坐标（PIP的y）
            ref_cx: 参考点的x坐标
            max_x_diff: x方向最大允许偏差
        """
        candidates = [d for d in dets if d.cy > ref_cy and abs(d.cx - ref_cx) < max_x_diff]
        if not candidates:
            return None
        return min(candidates, key=lambda d: abs(d.cx - ref_cx))

    def _pair_pip_mcp(self, pips: List[JointDetection], mcps: List[JointDetection]) -> List[Tuple]:
        """
        将近节指骨(PIP)与掌骨(MCP)通过垂直对齐关系配对
        
        每个PIP应该在其正下方有一个对应的MCP（同一根手指）
        
        Returns:
            配对列表 [(pip_det, mcp_det), ...]，按pip的cx升序排列
        """
        pairs = []
        used_mcps = set()
        
        for pip in sorted(pips, key=lambda p: p.cx):
            best_mcp = None
            best_score = float('inf')
            
            for mcp in mcps:
                if id(mcp) in used_mcps:
                    continue
                if mcp.cy <= pip.cy:
                    continue
                
                x_diff = abs(mcp.cx - pip.cx)
                y_diff = mcp.cy - pip.cy
                score = x_diff * 2 + y_diff * 0.5
                
                if score < best_score and x_diff < 250:
                    best_score = score
                    best_mcp = mcp
            
            if best_mcp is not None:
                pairs.append((pip, best_mcp))
                used_mcps.add(id(best_mcp))
            else:
                pairs.append((pip, None))
        
        return pairs

    def map_to_ruschn(self) -> Dict[str, JointDetection]:
        """
        使用空间关联法将检测结果映射到RUS-CHN需要的13个关节
        
        算法步骤：
        1. 识别拇指：以MCPFirst为锚点，找到最接近的PIP和DIP作为PIPFirst/DIPFirst
        2. PIP-MCP配对：对非拇指的手指，通过垂直对齐关系将PIP与MCP配对
        3. 手指身份确定：按配对的x坐标排序，从左到右为 小指(5)->无名指(4)->中指(3)->食指(2)
        4. MIP/DIP映射：根据已确定的PIP位置，通过x接近度映射对应的中节/远节指骨
        
        Returns:
            映射后的关节字典 {关节名称: 检测结果}
        """
        if self.hand_side is None:
            self.determine_hand_side()

        if self.hand_side != 'L':
            raise ValueError("RUS-CHN法仅支持左手X光片")

        mapped_joints = {}

        for det in self.detections.get('Radius', []):
            if 'Radius' not in mapped_joints or det.confidence > mapped_joints['Radius'].confidence:
                mapped_joints['Radius'] = det

        for det in self.detections.get('Ulna', []):
            if 'Ulna' not in mapped_joints or det.confidence > mapped_joints['Ulna'].confidence:
                mapped_joints['Ulna'] = det

        mcpfirst_det = None
        for det in self.detections.get('MCPFirst', []):
            if mcpfirst_det is None or det.confidence > mcpfirst_det.confidence:
                mcpfirst_det = det
        if mcpfirst_det:
            mapped_joints['MCPFirst'] = mcpfirst_det

        all_pip = list(self.detections.get('ProximalPhalanx', []))
        all_mcp = list(self.detections.get('MCP', []))
        all_mip = list(self.detections.get('MiddlePhalanx', []))
        all_dip = list(self.detections.get('DistalPhalanx', []))

        thumb_pip = None
        thumb_dip = None
        mcpfirst_cx = mcpfirst_det.cx if mcpfirst_det else None

        if mcpfirst_cx is not None:
            thumb_candidates = [p for p in all_pip if abs(p.cx - mcpfirst_cx) < 300]
            if thumb_candidates:
                thumb_pip = min(thumb_candidates, key=lambda p: abs(p.cx - mcpfirst_cx))

            dip_candidates = [d for d in all_dip if abs(d.cx - mcpfirst_cx) < 350]
            if dip_candidates:
                thumb_dip = min(dip_candidates, key=lambda d: abs(d.cx - mcpfirst_cx))

        if thumb_pip:
            mapped_joints['PIPFirst'] = thumb_pip
        if thumb_dip:
            mapped_joints['DIPFirst'] = thumb_dip

        non_thumb_pip = [p for p in all_pip if p is not thumb_pip]
        non_thumb_mcp = [m for m in all_mcp]

        if non_thumb_pip and non_thumb_mcp:
            pairs = self._pair_pip_mcp(non_thumb_pip, non_thumb_mcp)
            sorted_pairs = sorted(pairs, key=lambda pair: pair[0].cx)

            n_pairs = len(sorted_pairs)

            paired_mcps = {id(p[1]) for p in sorted_pairs if p[1] is not None}
            unpaired_mcps = [m for m in non_thumb_mcp if id(m) not in paired_mcps]
            unpaired_mcps.sort(key=lambda m: m.cx)

            if n_pairs >= 1:
                mapped_joints['PIPFifth'] = sorted_pairs[0][0]
                if sorted_pairs[0][1]:
                    mapped_joints['MCPFifth'] = sorted_pairs[0][1]

            if n_pairs >= 3:
                has_unpaired_middle_mcp = False
                if unpaired_mcps:
                    pipfifth_cx = sorted_pairs[0][0].cx
                    last_normal_pair_cx = sorted_pairs[-1][0].cx
                    if abs(last_normal_pair_cx - (mcpfirst_cx or 9999)) < 300:
                        last_normal_pair_cx = sorted_pairs[-2][0].cx if n_pairs >= 2 else pipfifth_cx

                    candidate_middle_mcps = [m for m in unpaired_mcps
                                           if pipfifth_cx < m.cx < last_normal_pair_cx + 100]

                    if candidate_middle_mcps:
                        best_middle_mcp = candidate_middle_mcps[len(candidate_middle_mcps) // 2]
                        mapped_joints['MCPThird'] = best_middle_mcp
                        has_unpaired_middle_mcp = True

                        paired_pips_in_use = {id(p[0]) for p in sorted_pairs
                                             if p[1] is not None and p[0].cx != sorted_pairs[0][0].cx}
                        used_pips = paired_pips_in_use | {
                            id(mapped_joints.get('PIPFifth')),
                            id(mapped_joints.get('PIPFirst'))
                        }
                        available_pips = [p for p in non_thumb_pip if id(p) not in used_pips]

                        pipthird_found = False

                        if available_pips:
                            closest_pip = min(available_pips,
                                            key=lambda p: abs(p.cx - best_middle_mcp.cx))
                            dist = abs(closest_pip.cx - best_middle_mcp.cx)

                            if dist < 250:
                                mapped_joints['PIPThird'] = closest_pip
                                pipthird_found = True
                            elif dist < 350:
                                mapped_joints['PIPThird'] = closest_pip
                                pipthird_found = True

                        if not pipthird_found and non_thumb_pip:
                            all_candidates = [p for p in non_thumb_pip
                                           if id(p) not in {
                                               id(mapped_joints.get('PIPFifth')),
                                               id(mapped_joints.get('PIPFirst'))
                                           }]
                            if all_candidates:
                                fallback_pip = min(all_candidates,
                                                key=lambda p: abs(p.cx - best_middle_mcp.cx))
                                mapped_joints['PIPThird'] = fallback_pip

                if not has_unpaired_middle_mcp:
                    valid_non_thumb_pairs = [p for p in sorted_pairs
                                             if abs(p[0].cx - (mcpfirst_cx or 0)) > 200]

                    if len(valid_non_thumb_pairs) >= 3:
                        sorted_valid = sorted(valid_non_thumb_pairs, key=lambda p: p[0].cx)
                        middle_idx = len(sorted_valid) // 2
                        mapped_joints['PIPThird'] = sorted_valid[middle_idx][0]
                        if sorted_valid[middle_idx][1]:
                            mapped_joints['MCPThird'] = sorted_valid[middle_idx][1]
                    elif len(valid_non_thumb_pairs) == 2 and unpaired_mcps:
                        pipfifth_cx = sorted_pairs[0][0].cx
                        last_valid_cx = valid_non_thumb_pairs[-1][0].cx
                        between_mcps = [m for m in unpaired_mcps
                                       if pipfifth_cx < m.cx < last_valid_cx + 150]
                        if between_mcps:
                            mapped_joints['MCPThird'] = between_mcps[len(between_mcps) // 2]
                            paired_pips_in_use = {id(p[0]) for p in sorted_pairs
                                                 if p[1] is not None and p[0].cx != sorted_pairs[0][0].cx}
                            used_pips = paired_pips_in_use | {
                                id(mapped_joints.get('PIPFifth')),
                                id(mapped_joints.get('PIPFirst'))
                            }
                            available_pips = [p for p in non_thumb_pip if id(p) not in used_pips]
                            pipthird_found_2 = False
                            if available_pips:
                                closest_pip = min(available_pips,
                                                key=lambda p: abs(p.cx - mapped_joints['MCPThird'].cx))
                                dist2 = abs(closest_pip.cx - mapped_joints['MCPThird'].cx)
                                if dist2 < 250:
                                    mapped_joints['PIPThird'] = closest_pip
                                    pipthird_found_2 = True
                                elif dist2 < 350:
                                    mapped_joints['PIPThird'] = closest_pip
                                    pipthird_found_2 = True

                            if not pipthird_found_2 and non_thumb_pip:
                                all_cands_2 = [p for p in non_thumb_pip
                                             if id(p) not in {
                                                 id(mapped_joints.get('PIPFifth')),
                                                 id(mapped_joints.get('PIPFirst'))
                                             }]
                                if all_cands_2:
                                    fallback_2 = min(all_cands_2,
                                                  key=lambda p: abs(p.cx - mapped_joints['MCPThird'].cx))
                                    mapped_joints['PIPThird'] = fallback_2
                        else:
                            middle_idx = 1
                            mapped_joints['PIPThird'] = sorted_pairs[middle_idx][0]
                            if sorted_pairs[middle_idx][1]:
                                mapped_joints['MCPThird'] = sorted_pairs[middle_idx][1]
                    else:
                        middle_idx = n_pairs // 2
                        mapped_joints['PIPThird'] = sorted_pairs[middle_idx][0]
                        if sorted_pairs[middle_idx][1]:
                            mapped_joints['MCPThird'] = sorted_pairs[middle_idx][1]

            elif n_pairs == 2:
                if unpaired_mcps:
                    pipfifth_cx = sorted_pairs[0][0].cx
                    piplast_cx = sorted_pairs[-1][0].cx
                    between_mcps = [m for m in unpaired_mcps
                                   if pipfifth_cx < m.cx < piplast_cx + 200]
                    if between_mcps:
                        mapped_joints['MCPThird'] = between_mcps[len(between_mcps) // 2]
                        paired_pips_in_use = {id(p[0]) for p in sorted_pairs
                                             if p[1] is not None and p[0].cx != sorted_pairs[0][0].cx}
                        used_pips = paired_pips_in_use | {
                            id(mapped_joints.get('PIPFifth')),
                            id(mapped_joints.get('PIPFirst'))
                        }
                        available_pips = [p for p in non_thumb_pip if id(p) not in used_pips]
                        pipthird_found_3 = False
                        if available_pips:
                            closest_pip = min(available_pips,
                                            key=lambda p: abs(p.cx - mapped_joints['MCPThird'].cx))
                            dist3 = abs(closest_pip.cx - mapped_joints['MCPThird'].cx)
                            if dist3 < 250:
                                mapped_joints['PIPThird'] = closest_pip
                                pipthird_found_3 = True
                            elif dist3 < 350:
                                mapped_joints['PIPThird'] = closest_pip
                                pipthird_found_3 = True

                        if not pipthird_found_3 and non_thumb_pip:
                            all_cands_3 = [p for p in non_thumb_pip
                                         if id(p) not in {
                                             id(mapped_joints.get('PIPFifth')),
                                             id(mapped_joints.get('PIPFirst'))
                                         }]
                            if all_cands_3:
                                fallback_3 = min(all_cands_3,
                                              key=lambda p: abs(p.cx - mapped_joints['MCPThird'].cx))
                                mapped_joints['PIPThird'] = fallback_3
                    else:
                        mapped_joints['PIPThird'] = sorted_pairs[1][0]
                        if sorted_pairs[1][1]:
                            mapped_joints['MCPThird'] = sorted_pairs[1][1]
                else:
                    mapped_joints['PIPThird'] = sorted_pairs[1][0]
                    if sorted_pairs[1][1]:
                        mapped_joints['MCPThird'] = sorted_pairs[1][1]

        elif non_thumb_pip:
            sorted_pips = sorted(non_thumb_pip, key=lambda p: p.cx)
            n = len(sorted_pips)

            if n >= 1:
                mapped_joints['PIPFifth'] = sorted_pips[0]

            if n >= 3:
                mapped_joints['PIPThird'] = sorted_pips[n // 2]
            elif n >= 2:
                mapped_joints['PIPThird'] = sorted_pips[1]

            if 'MCPFifth' not in mapped_joints and non_thumb_mcp:
                sorted_mcps = sorted(non_thumb_mcp, key=lambda m: m.cx)
                if len(sorted_mcps) >= 1:
                    mapped_joints['MCPFifth'] = sorted_mcps[0]
                if len(sorted_mcps) >= 2:
                    mapped_joints['MCPThird'] = sorted_mcps[min(2, len(sorted_mcps) - 1)]

        if all_mip:
            sorted_mip = sorted(all_mip, key=lambda m: m.cx)
            n_mip = len(sorted_mip)

            if n_mip >= 1:
                mapped_joints['MIPFifth'] = sorted_mip[0]

            if 'PIPThird' in mapped_joints:
                pipthird_cx = mapped_joints['PIPThird'].cx
                mipthird_candidate = self._find_closest_to_ref(sorted_mip, pipthird_cx)
                if mipthird_candidate:
                    mapped_joints['MIPThird'] = mipthird_candidate
            elif n_mip >= 2:
                mapped_joints['MIPThird'] = sorted_mip[n_mip // 2]

        non_thumb_dip = [d for d in all_dip if d is not thumb_dip]

        if non_thumb_dip:
            sorted_dip = sorted(non_thumb_dip, key=lambda d: d.cx)
            n_dip = len(sorted_dip)

            if n_dip >= 1:
                mapped_joints['DIPFifth'] = sorted_dip[0]

            if 'PIPThird' in mapped_joints:
                pipthird_cx = mapped_joints['PIPThird'].cx
                dipthird_candidate = self._find_closest_to_ref(sorted_dip, pipthird_cx)
                if dipthird_candidate and dipthird_candidate is not mapped_joints.get('DIPFifth'):
                    mapped_joints['DIPThird'] = dipthird_candidate
                else:
                    remaining = [d for d in sorted_dip if d is not mapped_joints.get('DIPFifth')]
                    if remaining:
                        mapped_joints['DIPThird'] = remaining[len(remaining) // 2]
            elif n_dip >= 2:
                mapped_joints['DIPThird'] = sorted_dip[n_dip // 2]

        if 'MCPFifth' not in mapped_joints and non_thumb_mcp:
            sorted_mcps = sorted(non_thumb_mcp, key=lambda m: m.cx)
            if sorted_mcps:
                mapped_joints['MCPFifth'] = sorted_mcps[0]
            if len(sorted_mcps) >= 2:
                if 'PIPThird' in mapped_joints:
                    pipthird_cx = mapped_joints['PIPThird'].cx
                    mcpthird = self._find_closest_to_ref(sorted_mcps, pipthird_cx)
                    if mcpthird:
                        mapped_joints['MCPThird'] = mcpthird
                else:
                    mapped_joints['MCPThird'] = sorted_mcps[-1]

        return mapped_joints

    def get_annotation_summary(self, mapped_joints: Dict[str, JointDetection]) -> str:
        lines = []
        lines.append("=" * 50)
        lines.append("RUS-CHN骨龄标注结果")
        lines.append("=" * 50)
        lines.append(f"手别: {'左手' if self.hand_side == 'L' else '右手'}")
        lines.append(f"检测关节数: {len(mapped_joints)}/13")
        lines.append("")

        lines.append("【骨骼关节标注】")
        for joint in ['Radius', 'Ulna']:
            if joint in mapped_joints:
                det = mapped_joints[joint]
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}: 置信度 {det.confidence:.3f}, 位置 ({det.cx}, {det.cy})")
            else:
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}: 未检测到")

        lines.append("")
        lines.append("【掌骨关节标注】")
        for joint in ['MCPFirst', 'MCPThird', 'MCPFifth']:
            if joint in mapped_joints:
                det = mapped_joints[joint]
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}({FINGER_NAMES[info['finger']]}): 置信度 {det.confidence:.3f}")
            else:
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}({FINGER_NAMES[info['finger']]}): 未检测到")

        lines.append("")
        lines.append("【近节指骨关节标注】")
        for joint in ['PIPFirst', 'PIPThird', 'PIPFifth']:
            if joint in mapped_joints:
                det = mapped_joints[joint]
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}({FINGER_NAMES[info['finger']]}): 置信度 {det.confidence:.3f}")
            else:
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}({FINGER_NAMES[info['finger']]}): 未检测到")

        lines.append("")
        lines.append("【中节指骨关节标注】")
        for joint in ['MIPThird', 'MIPFifth']:
            if joint in mapped_joints:
                det = mapped_joints[joint]
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}({FINGER_NAMES[info['finger']]}): 置信度 {det.confidence:.3f}")
            else:
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}({FINGER_NAMES[info['finger']]}): 未检测到")

        lines.append("")
        lines.append("【远节指骨关节标注】")
        for joint in ['DIPFirst', 'DIPThird', 'DIPFifth']:
            if joint in mapped_joints:
                det = mapped_joints[joint]
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}({FINGER_NAMES[info['finger']]}): 置信度 {det.confidence:.3f}")
            else:
                info = RUSCHN_JOINTS[joint]
                lines.append(f"  {info['name']}({FINGER_NAMES[info['finger']]}): 未检测到")

        lines.append("")
        missing = set(RUSCHN_JOINTS.keys()) - set(mapped_joints.keys())
        if missing:
            lines.append(f"缺失关节: {', '.join(missing)}")
        else:
            lines.append("所有关节检测完整!")

        return "\n".join(lines)

    def map_to_ruschn_calibrated(self) -> Dict[str, JointDetection]:
        """校正版映射：先按解剖归位类别名，再用「列」结构装配 13 个关节。

        与旧 map_to_ruschn 的区别：
          1. 旧版把 ProximalPhalanx 当近节、DistalPhalanx 当远节，实际正好相反，
             PIP/MCP/DIP 三类共 9 个关节都喂了隔壁一节骨头；
          2. 旧版用 MCPFirst.cx ± 300px 找拇指指骨，而第一掌骨骨骺在基底部、
             cx 靠近腕部中线，实际抓到的是食指，导致「第一」三个关节全错位；
          3. 旧版配对用「最近邻 + 一堆 250/300/350 绝对像素阈值」，与分辨率绑定；
             新版改用名次配对，且拇指由「缺中节」判定，与尺度无关。
        """
        if self.hand_side is None:
            self.determine_hand_side()

        by: Dict[str, List[JointDetection]] = {}
        for cls_name, dets in self.detections.items():
            key = DETECTOR_CLASS_ANATOMY.get(cls_name)
            if key and dets:
                by.setdefault(key, []).extend(dets)

        distal = by.get('distal', [])
        middle = by.get('middle', [])
        proximal = by.get('proximal', [])
        metacarpal = by.get('metacarpal', [])

        if not distal:
            # 没检到任何远节指骨时退回旧算法，保证不会整个失败
            return self.map_to_ruschn()

        mapped: Dict[str, JointDetection] = {}

        for key, joint in (('radius', 'Radius'), ('ulna', 'Ulna')):
            if by.get(key):
                mapped[joint] = max(by[key], key=lambda d: d.confidence)

        thumb_distal = _pick_thumb_distal(distal, middle)
        rest_distal = [d for d in distal if d is not thumb_distal]

        col: Dict[int, Dict[str, JointDetection]] = {}
        for dd, pp in _rank_pair(distal, proximal):
            col.setdefault(id(dd), {'distal': dd})['proximal'] = pp
        for dd, mm in _rank_pair(rest_distal, middle):
            col.setdefault(id(dd), {'distal': dd})['middle'] = mm
        for dd, mc in _rank_pair(rest_distal, metacarpal):
            col.setdefault(id(dd), {'distal': dd})['metacarpal'] = mc

        cols = [col[id(d)] for d in distal if id(d) in col]
        cols.sort(key=lambda c: c['distal'].cx)
        thumb_col = col.get(id(thumb_distal))

        non_thumb = [c for c in cols if c is not thumb_col]
        # 左手 PA 位：从左到右是小指→无名指→中指→食指，拇指单独在最右
        if self.hand_side != 'L':
            non_thumb = list(reversed(non_thumb))

        order = {5: None, 4: None, 3: None, 2: None}
        if len(non_thumb) >= 4:
            order[5], order[4], order[3], order[2] = non_thumb[:4]
        elif len(non_thumb) == 3:
            order[5], order[3], order[2] = non_thumb

        if thumb_col:
            mapped['DIPFirst'] = thumb_col['distal']
            mapped['PIPFirst'] = thumb_col['proximal']
        if by.get('mc1'):
            mapped['MCPFirst'] = max(by['mc1'], key=lambda d: d.confidence)

        for finger, code in ((5, 'Fifth'), (3, 'Third')):
            c = order.get(finger)
            if not c:
                continue
            for key, joint in (('distal', 'DIP'), ('middle', 'MIP'),
                               ('proximal', 'PIP'), ('metacarpal', 'MCP')):
                det = c.get(key)
                if det is not None:
                    mapped[joint + code] = det

        return {k: v for k, v in mapped.items() if v is not None}


JOINT_COLORS_BGR = [
    (0, 255, 255), (0, 0, 255), (0, 255, 0), (255, 0, 255),
    (0, 165, 255), (255, 0, 0), (211, 0, 148), (144, 128, 255),
    (144, 238, 144), (255, 165, 0), (255, 255, 0), (128, 0, 128), (0, 255, 128)
]

JOINT_ORDER = [
    'Radius', 'Ulna',
    'MCPFirst', 'MCPThird', 'MCPFifth',
    'PIPFirst', 'PIPThird', 'PIPFifth',
    'MIPThird', 'MIPFifth',
    'DIPFirst', 'DIPThird', 'DIPFifth'
]


def grade_display_name(joint: str, level: int) -> str:
    """把内部细分档序号（1..14）翻成 RUS-CHN 等级名（1~8 级，含 (0)/(2) 子档）。

    🔴 两个显示口（标注图、文字报告）**必须**都走这个函数。历史上标注图用
    ``GRADE_NAMES``、报告直接用档位序号，于是同一个关节在图上是「8级」、
    在报告里是「12级」—— 医生逐项核对时必然当成两回事，还会质疑 RUS-CHN
    哪来的 12 级（该法只有 1~8 级，5/7/8 级再分 (0)/(2)/(3)/(4) 子档）。
    """
    names = GRADE_NAMES.get(joint) or []
    if not names:
        return '%d' % level
    idx = min(max(1, int(level)), len(names)) - 1
    return names[idx].replace('等级', '')


def draw_ruschn_annotations(img: np.ndarray,
                             mapped_joints: Dict[str, JointDetection],
                             results_score: Dict[str, Tuple[int, int]] = None,
                             detail: Dict[str, Dict] = None) -> np.ndarray:
    """把 13 个 RUS-CHN 关节画到片子上。

    2026-11 重写：旧版用 14px 固定字号、标签一律画在框正上方、只做 30x16 的粗避让，
    在 1400~2000px 的片子上字小到看不清，标签还经常压进隔壁的框里 —— 看的人会误以为
    「名字配错了骨头」。现在按片子尺寸定字号，标签做加权避让 + 半透明底 + 引线。

    ``detail`` 来自分类明细：低置信但已采用的关节在等级后加「?」，完全没评上的
    写「未评级」并注明原因（没检到框 / 置信度不足），避免医生把「低置信」误当
    「没做」或反过来。
    """
    import copy
    from PIL import Image, ImageDraw, ImageFont

    img = copy.deepcopy(img)
    h, w = img.shape[:2]

    CHINESE_NAMES = {joint: info['name'] for joint, info in RUSCHN_JOINTS.items()}
    colors_rgb = [(b[2], b[1], b[0]) for b in JOINT_COLORS_BGR]

    base = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).convert('RGBA')
    draw = ImageDraw.Draw(base)

    # 字号跟片子尺寸走：1772x1432 上约 34px，小片子最低 18px
    label_size = max(18, int(min(w, h) / 42))

    def _load(sz):
        for fp in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
                   "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc"):
            try:
                return ImageFont.truetype(fp, sz)
            except Exception:
                continue
        return ImageFont.load_default()

    font = _load(label_size)

    def overlap(a, b):
        dx = min(a[2], b[2]) - max(a[0], b[0])
        dy = min(a[3], b[3]) - max(a[1], b[1])
        return dx * dy if dx > 0 and dy > 0 else 0

    def dist(a, b):
        dx = max(0, max(a[0] - b[2], b[0] - a[2]))
        dy = max(0, max(a[1] - b[3], b[1] - a[3]))
        return (dx * dx + dy * dy) ** 0.5

    # 1) 先画框 + 中心点
    geo = []
    for idx, joint_name in enumerate(JOINT_ORDER):
        if joint_name not in mapped_joints:
            continue
        det = mapped_joints[joint_name]
        color = colors_rgb[idx % len(colors_rgb)]
        x1, y1, x2, y2 = (max(0, min(det.box[0], w - 1)), max(0, min(det.box[1], h - 1)),
                          max(0, min(det.box[2], w - 1)), max(0, min(det.box[3], h - 1)))
        if x2 <= x1 or y2 <= y1:
            continue
        draw.rectangle([x1, y1, x2, y2], outline=color + (255,), width=3)
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=(255, 0, 0, 255))
        geo.append((joint_name, color, (x1, y1, x2, y2)))

    all_boxes = [g[2] for g in geo]

    # 2) 排标签位置：候选 6 个锚点 x 若干微调，取与已有标签/其它框冲突最小的
    placed = []
    labels = []
    has_low_conf = False
    for joint_name, color, box in sorted(geo, key=lambda g: g[2][1]):
        chinese_name = CHINESE_NAMES.get(joint_name, joint_name)
        d = (detail or {}).get(joint_name) or {}
        if results_score and joint_name in results_score:
            level, score = results_score[joint_name]
            grade_display = grade_display_name(joint_name, level)
            # 低置信但已采用：等级后加 ?，并在图角说明，避免与「没做」混淆
            if d.get('low_confidence'):
                has_low_conf = True
                label = f"{chinese_name} {grade_display}级? S{score}"
            else:
                label = f"{chinese_name} {grade_display}级 S{score}"
        else:
            status = d.get('status')
            if status == 'no_box':
                label = f"{chinese_name} 未检到"
            elif status == 'dropped':
                label = f"{chinese_name} 未评级(置信度低)"
            else:
                label = f"{chinese_name} 未评级"

        tb = draw.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        bw, bh = tw + 14, th + 10
        x1, y1, x2, y2 = box
        mid_y = (y1 + y2) // 2 - bh // 2
        anchors = [(x1, y1 - bh - 4), (x2 - bw, y1 - bh - 4),
                   (x1, y2 + 4), (x2 - bw, y2 + 4),
                   (x1 - bw - 8, mid_y), (x2 + 8, mid_y)]
        best, best_score = None, None
        for ax, ay in anchors:
            for dy in (0, -18, 18, -36, 36, -54, 54):
                for dx in (0, -28, 28):
                    r = (ax + dx, ay + dy, ax + dx + bw, ay + dy + bh)
                    if r[0] < 2 or r[1] < 2 or r[2] > w - 2 or r[3] > h - 2:
                        continue
                    s = 0.0
                    for p in placed:
                        s += 60.0 * overlap(r, p)
                    for bx in all_boxes:
                        if bx is box:
                            continue
                        s += 10.0 * overlap(r, bx)
                    s += 3.0 * overlap(r, box) + 0.9 * dist(r, box)
                    if best_score is None or s < best_score:
                        best, best_score = r, s
        if best is None:
            best = (x1, max(2, y1 - bh - 4), x1 + bw, max(2, y1 - bh - 4) + bh)
        placed.append(best)
        labels.append((joint_name, color, box, label, best))

    # 3) 半透明底 + 文字 + 引线
    overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for joint_name, color, box, label, rect in labels:
        od.rectangle(list(rect), fill=(0, 0, 0, 190), outline=color + (210,), width=1)
    base = Image.alpha_composite(base, overlay)

    draw = ImageDraw.Draw(base)
    for joint_name, color, box, label, rect in labels:
        if rect[3] < box[1] or rect[1] > box[3]:
            lx = min(max(rect[0] + 8, box[0]), box[2])
            ly1 = rect[3] if rect[3] < box[1] else rect[1]
            ly2 = box[1] if rect[3] < box[1] else box[3]
            draw.line([(lx, ly1), (lx, ly2)], fill=color + (120,), width=1)
        draw.text((rect[0] + 7, rect[1] + 5), label, font=font, fill=color + (255,))

    # 图角说明：等级后的 ? 是「低置信但仍计分」，不等于「没做」，需自解释
    if has_low_conf:
        note = "? = 该项置信度偏低，已按最佳判定计分，建议复核"
        f_note = _load(max(16, int(label_size * 0.72)))
        nb = draw.textbbox((0, 0), note, font=f_note)
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        nx = w - nw - 24
        ny = h - nh - 20
        if nx > 4 and ny > 4:
            ov2 = Image.new('RGBA', base.size, (0, 0, 0, 0))
            ImageDraw.Draw(ov2).rectangle(
                [nx - 8, ny - 6, nx + nw + 8, ny + nh + 8], fill=(0, 0, 0, 175))
            base = Image.alpha_composite(base, ov2)
            draw = ImageDraw.Draw(base)
            draw.text((nx, ny), note, font=f_note, fill=(255, 200, 90))

    return cv2.cvtColor(np.array(base.convert('RGB')), cv2.COLOR_RGB2BGR)


# 13 个 RUS-CHN 关节的中文名称与报告顺序（2026-10 按临床报告口径全中文化，
# 排列：掌骨 → 近节 → 中节 → 远节 → 尺骨 → 桡骨；名称严格按报告模板，
# 如「第一掌骨骺」「第三掌骨骨骺」——以模板原文为准，勿自行"规范化"）。
RUSCHN_REPORT_ORDER = (
    'MCPFirst', 'MCPThird', 'MCPFifth',
    'PIPFirst', 'PIPThird', 'PIPFifth',
    'MIPThird', 'MIPFifth',
    'DIPFirst', 'DIPThird', 'DIPFifth',
    'Ulna', 'Radius',
)

RUSCHN_JOINT_CN = {
    'MCPFirst': '第一掌骨骺',
    'MCPThird': '第三掌骨骨骺',
    'MCPFifth': '第五掌骨骨骺',
    'PIPFirst': '第一近节骨骺',
    'PIPThird': '第三近节骨骺',
    'PIPFifth': '第五近节骨骺',
    'MIPThird': '第三中节指骨骨骺',
    'MIPFifth': '第五中节指骨骨骺',
    'DIPFirst': '第一远节指骨骨骺',
    'DIPThird': '第三远节指骨骨骺',
    'DIPFifth': '第五远节指骨骨骺',
    'Ulna': '尺骨骨骺',
    'Radius': '桡骨骨骺',
}


def _fmt_bone_age(value) -> str:
    """骨龄保留最多两位小数并去掉无意义的尾零：10.27→'10.27'，10.00→'10'。"""
    text = ('%.2f' % float(value)).rstrip('0').rstrip('.')
    return text if text else '0'


def generate_ruschn_report(results_score: Dict[str, Tuple[int, int]],
                           total_score: int,
                           bone_age: float,
                           detail: Dict[str, Dict] = None) -> str:
    """生成骨龄报告（2026-10 改版：影像所见 / 影像提示 两段式，全中文无缩写）：

    影像所见：第一掌骨骺6级，得28分；……；桡骨骨骺7级，得91分。
    影像提示：RUS-CHN 分级计分法，受检儿 CHN 总得分：485分，骨龄约为10.27岁。

    分类失败的关节（results_score 中缺失）不进「影像所见」，与总得分的
    实际构成保持一致（旧版会把它写成 0 级 0 分，反而误导）。

    2026-11：``detail`` 传入后，末段追加一条「分级说明」，把
    「低置信但仍计分」和「确实没评上」的关节点名列出。此前这两种情况在报告里
    完全看不见（前者被静默丢弃、后者被静默跳过），医生只能靠数「影像所见」
    里有几项才发现缺了，容易把少算的分数当成真实骨龄偏低。
    """
    parts = []
    for joint in RUSCHN_REPORT_ORDER:
        val = results_score.get(joint)
        if not val:
            continue
        level, score = val
        parts.append('%s%s级，得%d分'
                     % (RUSCHN_JOINT_CN.get(joint, joint),
                        grade_display_name(joint, level), score))
    if parts:
        findings = '影像所见：%s。' % '；'.join(parts)
    else:
        findings = '影像所见：各关节未能有效分级。'
    tip = ('影像提示：RUS-CHN 分级计分法，受检儿 CHN 总得分：%d分，'
           '骨龄约为%s岁。' % (total_score, _fmt_bone_age(bone_age)))

    lines = [findings, tip]
    if detail:
        low = [RUSCHN_JOINT_CN.get(j, j) for j in RUSCHN_REPORT_ORDER
               if (detail.get(j) or {}).get('low_confidence')]
        ungraded = [RUSCHN_JOINT_CN.get(j, j) for j in RUSCHN_REPORT_ORDER
                    if j not in results_score]
        notes = []
        if low:
            notes.append('%s形态显示欠清晰，已按最佳判定分级计分，建议结合临床复核'
                         % '、'.join(low))
        if ungraded:
            notes.append('%s未能分级，未计入总分（该 %d 项缺损会使骨龄偏低，'
                         '必要时请手工补评）' % ('、'.join(ungraded), len(ungraded)))
        if notes:
            lines.append('分级说明：%s。' % '；'.join(notes))
    return '\n'.join(lines)
