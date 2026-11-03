import os
import cv2
import math
import numpy as np
from PIL import Image
from ultralytics import YOLO
from collections import defaultdict
from torchvision import transforms
from pathlib import Path

# 模型路径定义
BONEAGE_MODEL_DIR = Path(__file__).parent
DETECT_MODEL_PATH = BONEAGE_MODEL_DIR / "yolov8n_detect_bone_age.pt"
MODEL_PATHS = {
    'MCP': BONEAGE_MODEL_DIR / "yolov8s_cls_MCP.pt",
    'MCPFirst': BONEAGE_MODEL_DIR / "yolov8s_cls_MCPFirst.pt",
    'MIP': BONEAGE_MODEL_DIR / "yolov8s_cls_MIP.pt",
    'PIP': BONEAGE_MODEL_DIR / "yolov8s_cls_PIP.pt",
    'PIPFirst': BONEAGE_MODEL_DIR / "yolov8s_cls_PIPFirst.pt",
    'DIP': BONEAGE_MODEL_DIR / "yolov8s_cls_DIP.pt",
    'DIPFirst': BONEAGE_MODEL_DIR / "yolov8s_cls_DIPFirst.pt",
    'Radius': BONEAGE_MODEL_DIR / "yolov8s_cls_Radius.pt",
    'Ulna': BONEAGE_MODEL_DIR / "yolov8s_cls_Ulna.pt",
}

# 关节-分类模型映射关系
arthrosis = {
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

# RUS-CHN计分表（男/女）
SCORE = {
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

# 分类模型输入预处理
size_transform = transforms.Compose([
    transforms.Grayscale(num_output_channels=1),
    transforms.Resize((96, 96), antialias=True)
])


def preprocess_image_cv(img_bgr):
    """图像预处理（转正方形+标准化）"""
    h, w, c = img_bgr.shape
    # 补黑边转正方形
    if h != w:
        long_side = max(h, w)
        short_side = min(h, w)
        pad = (long_side - short_side) // 2
        background = np.zeros((long_side, long_side, c), dtype=np.uint8)
        if h > w:
            background[:, pad:pad + w, :] = img_bgr
        else:
            background[pad:pad + h, :, :] = img_bgr
        img_bgr = background
    # 标准化
    img_pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    img_pil = size_transform(img_pil)
    img_gray = np.array(img_pil)
    img_bgr_final = cv2.cvtColor(img_gray, cv2.COLOR_GRAY2BGR)
    return img_bgr_final


def calcBoneAge(score, sex):
    """骨龄计算公式（男/女不同）"""
    if sex == 'boy':
        boneAge = 2.01790023656577 + (-0.0931820870747269) * score + \
                  math.pow(score, 2) * 0.00334709095418796 + \
                  math.pow(score, 3) * (-3.32988302362153E-05) + \
                  math.pow(score, 4) * (1.75712910819776E-07) + \
                  math.pow(score, 5) * (-5.59998691223273E-10) + \
                  math.pow(score, 6) * (1.1296711294933E-12) + \
                  math.pow(score, 7) * (-1.45218037113138e-15) + \
                  math.pow(score, 8) * (1.15333377080353e-18) + \
                  math.pow(score, 9) * (-5.15887481551927e-22) + \
                  math.pow(score, 10) * (9.94098428102335e-26)
        return round(boneAge, 2)
    elif sex == 'girl':
        boneAge = 5.81191794824917 + (-0.271546561737745) * score + \
                  math.pow(score, 2) * 0.00526301486340724 + \
                  math.pow(score, 3) * (-4.37797717401925E-05) + \
                  math.pow(score, 4) * (2.0858722025667E-07) + \
                  math.pow(score, 5) * (-6.21879866563429E-10) + \
                  math.pow(score, 6) * (1.19909931745368E-12) + \
                  math.pow(score, 7) * (-1.49462900826936E-15) + \
                  math.pow(score, 8) * (1.162435538672E-18) + \
                  math.pow(score, 9) * (-5.12713017846218E-22) + \
                  math.pow(score, 10) * (9.78989966891478E-26)
        return round(boneAge, 2)
    else:
        raise ValueError("❌ 性别必须为'boy'或'girl'")


# 13 个 RUS-CHN 关节的中文名称与报告顺序（与 boneage_annotation.py 保持一致，
# 两条推理路径——旧 torch 版与本文件 / 新 ONNX 版——必须产出同一格式报告）。
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


def _fmt_bone_age(value):
    """骨龄保留最多两位小数并去掉尾零：10.27→'10.27'，10.00→'10'。"""
    text = ('%.2f' % float(value)).rstrip('0').rstrip('.')
    return text if text else '0'


def export(results, score, boneAge):
    """生成骨龄检测报告（2026-10 改版：影像所见 / 影像提示 两段式，全中文）"""
    parts = []
    for joint in RUSCHN_REPORT_ORDER:
        val = (results or {}).get(joint)
        if not val:
            continue
        level, s = val
        parts.append('%s%d级，得%d分' % (RUSCHN_JOINT_CN.get(joint, joint), level, s))
    if parts:
        findings = '影像所见：%s。' % '；'.join(parts)
    else:
        findings = '影像所见：各关节未能有效分级。'
    tip = ('影像提示：RUS-CHN 分级计分法，受检儿 CHN 总得分：%d分，'
           '骨龄约为%s岁。' % (score, _fmt_bone_age(boneAge)))
    return findings + '\n' + tip


class BoneAgeDetector:
    """骨龄检测主类"""

    def __init__(self):
        self.detect_model = None
        self.cls_models = {}
        self._load_models()

    def _load_models(self):
        """加载检测模型和分类模型"""
        # 加载检测模型
        if os.path.exists(DETECT_MODEL_PATH):
            self.detect_model = YOLO(DETECT_MODEL_PATH)
            print(f"✅ 检测模型加载完成：{DETECT_MODEL_PATH}")
        else:
            raise FileNotFoundError(f"❌ 未找到检测模型：{DETECT_MODEL_PATH}")

        # 加载分类模型
        for joint_name, (model_key, _) in arthrosis.items():
            if model_key in MODEL_PATHS:
                model_path = MODEL_PATHS[model_key]
                if os.path.exists(model_path):
                    try:
                        self.cls_models[model_key] = YOLO(model_path)
                        print(f"✅ 分类模型加载完成：{model_path} -> {model_key}")
                    except Exception as e:
                        print(f"❌ 加载分类模型失败 {model_path}: {e}")
                else:
                    print(f"❌ 模型文件不存在：{model_path}")
            else:
                print(f"⚠️ 模型键 {model_key} 不在 MODEL_PATHS 中")

        # 检查是否成功加载了所有必要的模型
        required_models = set([arthrosis[joint][0] for joint in arthrosis])
        loaded_models = set(self.cls_models.keys())
        missing_models = required_models - loaded_models
        if missing_models:
            print(f"⚠️ 缺少以下分类模型：{missing_models}")

    def _detect_joints(self, img_path):
        """检测关节，判断左右手，筛选关键关节"""
        # 读取并预处理图像
        img_bgr = cv2.imread(img_path)
        if img_bgr is None:
            raise FileNotFoundError(f"❌ 无法读取图片：{img_path}")
        
        # 直接使用原图进行检测
        img_opt_bgr = img_bgr.copy()

        # 检测关节
        results = self.detect_model(img_opt_bgr, conf=0.5)
        detection_results = []
        radius_x, ulna_x = None, None

        for result in results:
            for box in result.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf)
                cls = int(box.cls)
                cls_name = result.names[cls]

                # 记录桡骨/尺骨x坐标（判断左右手）
                if cls_name == "Radius":
                    radius_x = x1
                elif cls_name == "Ulna":
                    ulna_x = x1

                detection_results.append({
                    "class": cls_name,
                    "confidence": conf,
                    "box": (x1, y1, x2, y2),
                    "img_crop": img_opt_bgr[y1:y2, x1:x2]
                })

        # 判断左右手
        if radius_x is None or ulna_x is None:
            raise ValueError("❌ 未检测到桡骨和尺骨，无法判断左右手")
        hand_side = "R" if ulna_x > radius_x else "L"
        if hand_side == "R":
            raise ValueError("⚠️ 只检测左手，右手不处理")
        print(f"✅ 检测到左手，关节数量：{len(detection_results)}")

        # 筛选并映射关键关节
        class_dict = defaultdict(list)
        
        # 首先按类别分组
        for det in detection_results:
            cls_name = det["class"]
            class_dict[cls_name].append(det)
        
        # 对每个类别的检测结果按x坐标排序
        for cls_name in class_dict:
            class_dict[cls_name].sort(key=lambda x: x["box"][0])
        
        # 映射到具体的关节名称
        mapped_dict = defaultdict(list)
        
        # 处理ProximalPhalanx（近节指骨）
        # 左手X光片按x坐标排序后: index[0]=第5指, index[1]=第4指, index[2]=第3指, index[3]=第2指, index[4]=第1指
        # RUS-CHN需要: PIPFirst(拇指-index4), PIPThird(中指-index2), PIPFifth(小指-index0)
        if "ProximalPhalanx" in class_dict and len(class_dict["ProximalPhalanx"]) >= 3:
            phalanxes = class_dict["ProximalPhalanx"]
            n = len(phalanxes)
            
            # 如果MCPFirst存在，使用空间关系找到对应的PIPFirst
            mcpfirst_cx = None
            if "MCPFirst" in class_dict:
                mcpfirst_det = max(class_dict["MCPFirst"], key=lambda x: x["confidence"])
                mcpfirst_cx = mcpfirst_det["box"][0]
            
            if n >= 5:
                # 直接使用固定索引：index 0=第5指, index 2=第3指, index 4=第1指
                mapped_dict["PIPFifth"] = [phalanxes[0]]   # 第5指（小指）
                mapped_dict["PIPThird"] = [phalanxes[2]]   # 第3指（中指）
                mapped_dict["PIPFirst"] = [phalanxes[4]]    # 第1指（拇指）
            elif n >= 3:
                # 只有3个时，取index 0(小指)、index 1(中间)、index 4(拇指)
                mapped_dict["PIPFifth"] = [phalanxes[0]]   # 小指
                mapped_dict["PIPThird"] = [phalanxes[1]]   # 估计是中指
                if mcpfirst_cx is not None:
                    pipfirst = min(phalanxes, key=lambda p: abs(p["box"][0] - mcpfirst_cx))
                    mapped_dict["PIPFirst"] = [pipfirst]
                else:
                    mapped_dict["PIPFirst"] = [phalanxes[-1]]
        
        # 处理DistalPhalanx（远节指骨）
        # 同样需要正确映射
        if "DistalPhalanx" in class_dict and len(class_dict["DistalPhalanx"]) >= 3:
            phalanxes = class_dict["DistalPhalanx"]
            n = len(phalanxes)
            
            mcpfirst_cx = None
            if "MCPFirst" in class_dict:
                mcpfirst_det = max(class_dict["MCPFirst"], key=lambda x: x["confidence"])
                mcpfirst_cx = mcpfirst_det["box"][0]
            
            if n >= 5:
                # 按x坐标: index[0]=第5指, index[2]=第3指, index[4]=第1指
                mapped_dict["DIPFifth"] = [phalanxes[0]]   # 第5指（小指）
                mapped_dict["DIPThird"] = [phalanxes[2]]   # 第3指（中指）
                if mcpfirst_cx is not None:
                    dipfirst = min(phalanxes, key=lambda p: abs(p["box"][0] - mcpfirst_cx))
                    mapped_dict["DIPFirst"] = [dipfirst]
                else:
                    mapped_dict["DIPFirst"] = [phalanxes[4]]   # 第1指（拇指）
            elif n >= 3:
                mapped_dict["DIPFifth"] = [phalanxes[0]]
                mapped_dict["DIPThird"] = [phalanxes[1]]
                if mcpfirst_cx is not None:
                    dipfirst = min(phalanxes, key=lambda p: abs(p["box"][0] - mcpfirst_cx))
                    mapped_dict["DIPFirst"] = [dipfirst]
                else:
                    mapped_dict["DIPFirst"] = [phalanxes[-1]]
        
        # 处理MiddlePhalanx（中节指骨）
        # 拇指没有中节指骨，只有食指、中指、无名指、小指有
        # 按x坐标: index[0]=第5指, index[1]=第4指, index[2]=第3指, index[3]=第2指
        # RUS-CHN需要: MIPThird(中指-index2), MIPFifth(小指-index0)
        if "MiddlePhalanx" in class_dict and len(class_dict["MiddlePhalanx"]) >= 2:
            phalanxes = class_dict["MiddlePhalanx"]
            n = len(phalanxes)
            if n >= 4:
                # index[0]=第5指, index[2]=第3指
                mapped_dict["MIPFifth"] = [phalanxes[0]]   # 第5指（小指）
                mapped_dict["MIPThird"] = [phalanxes[2]]   # 第3指（中指）
            elif n >= 3:
                mapped_dict["MIPFifth"] = [phalanxes[0]]
                mapped_dict["MIPThird"] = [phalanxes[1]]
            elif n == 2:
                mapped_dict["MIPFifth"] = [phalanxes[0]]
                mapped_dict["MIPThird"] = [phalanxes[1]]
        
        # 处理MCP（掌骨）
        # MCPFirst单独检测(拇指)，MCP类别包含第1、2、3、4、5掌骨（可能包含拇指误检或第1掌骨）
        # 按x坐标排序后: index[0]=第5掌骨, index[1]=第4掌骨, index[2]=第3掌骨, index[3]=第2掌骨, index[4]=第1掌骨/拇指
        # RUS-CHN需要: MCPThird(中指-index2), MCPFifth(小指-index0)
        if "MCP" in class_dict:
            mcps = class_dict["MCP"]
            n = len(mcps)
            
            # 获取MCPFirst的x坐标（如果存在）
            mcpfirst_x1 = None
            if "MCPFirst" in class_dict:
                mcpfirst_det = max(class_dict["MCPFirst"], key=lambda x: x["confidence"])
                mcpfirst_x1 = mcpfirst_det["box"][0]
            
            if "MCPFirst" in class_dict and mcpfirst_x1 is not None:
                # 找到MCP中与MCPFirst最接近的（可能是拇指或第2掌骨）
                closest_mcp = min(mcps, key=lambda m: abs(m["box"][0] - mcpfirst_x1))
                
                # 排除最接近MCPFirst的那个（可能是拇指误检）
                other_mcps = [m for m in mcps if m != closest_mcp]
                
                if len(other_mcps) >= 2:
                    # 其他MCP按x坐标排序，取第1个作为第5掌骨，第3个作为第3掌骨
                    other_mcps.sort(key=lambda m: m["box"][0])
                    mapped_dict["MCPFifth"] = [other_mcps[0]]   # 第5掌骨（小指）
                    if len(other_mcps) >= 3:
                        mapped_dict["MCPThird"] = [other_mcps[2]]   # 第3掌骨（中指）
                    else:
                        mapped_dict["MCPThird"] = [other_mcps[1]]
            else:
                # 没有MCPFirst的情况
                if n >= 3:
                    mapped_dict["MCPFifth"] = [mcps[0]]
                    mapped_dict["MCPThird"] = [mcps[2]]
                elif n >= 2:
                    mapped_dict["MCPFifth"] = [mcps[0]]
        
        # 直接映射的关节
        for cls_name in ["MCPFirst", "Radius", "Ulna"]:
            if cls_name in class_dict:
                mapped_dict[cls_name] = class_dict[cls_name]

        # 过滤有效关节
        valid_joints = [k for k in mapped_dict.keys() if k in arthrosis]
        print(f"✅ 有效关节：{valid_joints}")
        
        if len(valid_joints) < 13:
            print(f"⚠️ 关键关节缺失，仅检测到{len(valid_joints)}/13个")
        
        return mapped_dict, img_opt_bgr

    def _classify_joints(self, class_dict, sex):
        """关节等级分类，计算分数"""
        results_score = {}
        total_score = 0

        for joint_name, dets in class_dict.items():
            if joint_name not in arthrosis:
                continue
            
            model_key = arthrosis[joint_name][0]
            
            if model_key not in self.cls_models:
                print(f"⚠️ 跳过{joint_name}：无对应分类模型{model_key}")
                continue
            
            cls_model = self.cls_models[model_key]

            for det in dets:
                crop_img = det["img_crop"]
                if crop_img.size == 0:
                    print(f"⚠️ 跳过{joint_name}：裁剪区域为空")
                    continue
                
                try:
                    crop_img_prep = preprocess_image_cv(crop_img)
                except Exception as e:
                    print(f"⚠️ 跳过{joint_name}：图像预处理失败 - {e}")
                    continue

                try:
                    cls_result = cls_model.predict(crop_img_prep, conf=0.5)
                    if len(cls_result) == 0 or len(cls_result[0].probs) == 0:
                        print(f"⚠️ {joint_name}分类结果为空")
                        continue
                    
                    level = int(cls_result[0].names[cls_result[0].probs.top1]) - 1
                    
                    if joint_name not in SCORE[sex]:
                        print(f"⚠️ 跳过{joint_name}：无{sex}性别的分数表")
                        continue
                    
                    score_list = SCORE[sex][joint_name]
                    if level < 0 or level >= len(score_list):
                        print(f"⚠️ {joint_name}等级{level + 1}超出范围")
                        score = 0
                    else:
                        score = score_list[level]
                    
                    results_score[joint_name] = (level + 1, score)
                    total_score += score
                    print(f"✅ {joint_name}：等级{level + 1}，得分{score}")
                    
                except Exception as e:
                    print(f"⚠️ {joint_name}分类失败：{e}")
                    continue

        return results_score, total_score

    def detect_bone_age(self, img_path, sex):
        """完整骨龄检测流程"""
        try:
            mapped_dict, img_opt_bgr = self._detect_joints(img_path)
            results_score, total_score = self._classify_joints(mapped_dict, sex)
            
            if len(results_score) == 0:
                raise ValueError("❌ 没有成功分类任何关节")
            
            bone_age = calcBoneAge(total_score, sex)
            report = export(results_score, total_score, bone_age)
            img_result = self._draw_results(img_opt_bgr, mapped_dict, results_score)
            cv2.imwrite("bone_age_result.png", img_result)
            print(f"✅ 结果图保存：bone_age_result.png")

            return {
                "total_score": total_score,
                "bone_age": bone_age,
                "joint_scores": results_score,
                "report": report,
                "result_img_path": "bone_age_result.png"
            }
        except Exception as e:
            return {"error": str(e)}

    def _draw_results(self, img, mapped_dict, results_score):
        """绘制检测框、等级、分数"""
        CHINESE_NAMES = {
            'MCPFirst': '第一掌骨',
            'MCPThird': '第三掌骨',
            'MCPFifth': '第五掌骨',
            'DIPFirst': '第一远节指骨',
            'DIPThird': '第三远节指骨',
            'DIPFifth': '第五远节指骨',
            'PIPFirst': '第一近节指骨',
            'PIPThird': '第三近节指骨',
            'PIPFifth': '第五近节指骨',
            'MIPThird': '第三中节指骨',
            'MIPFifth': '第五中节指骨',
            'Radius': '桡骨',
            'Ulna': '尺骨',
        }
        
        # 只绘制RUS-CHN需要的13个关节
        valid_joints = ['MCPFirst', 'MCPThird', 'MCPFifth', 'DIPFirst', 'DIPThird', 'DIPFifth',
                        'PIPFirst', 'PIPThird', 'PIPFifth', 'MIPThird', 'MIPFifth', 'Radius', 'Ulna']
        
        colors = [(0, 255, 255), (0, 0, 255), (0, 255, 0), (255, 0, 255),
                  (0, 165, 255), (255, 0, 0), (211, 0, 148), (144, 128, 255),
                  (144, 238, 144), (255, 165, 0), (255, 255, 0), (128, 0, 128), (0, 255, 128)]
        color_idx = 0

        for joint_name, dets in mapped_dict.items():
            # 只绘制有效的RUS-CHN关节，跳过其他类别（如MCP、ProximalPhalanx等原始类别）
            if joint_name not in valid_joints:
                continue
                
            color = colors[color_idx % len(colors)]
            color_idx += 1
            chinese_name = CHINESE_NAMES.get(joint_name, joint_name)
            for det in dets:
                x1, y1, x2, y2 = det["box"]
                conf = det["confidence"]
                cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                cv2.circle(img, (cx, cy), 3, (0, 0, 255), -1)
                if joint_name in results_score:
                    level, score = results_score[joint_name]
                    label = f"{chinese_name} L{level} S{score}"
                else:
                    label = f"{chinese_name}"
                cv2.putText(img, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        return img


if __name__ == "__main__":
    detector = BoneAgeDetector()
    print("模型加载测试完成！")
