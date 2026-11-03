# -*- coding: utf-8 -*-
"""CT3D 与「X 线辅助诊断系统」之间的集成桥接层。

设计要点
--------
1. **进程内集成，不再另起 Python 环境**
   实测 CT3D 自带的 pyenv（Python 3.8.3）可以直接 import 并运行 xray 的全部
   ONNX 推理模块（onnxruntime 1.19.2 / cv2 4.12 / numpy 1.24 均齐备），
   所以不需要再拉起 xray 的嵌入式 Python 或独立 exe。

2. **包名冲突已由 ct3d_utils 改名解决**
   xray 的 ``app`` 目录下有 ``utils`` 包，CT3D 根目录原本也有 ``utils.py``。
   由于 sys.modules 里同一个名字只能存在一个，这里把 CT3D 侧改名为
   ``ct3d_utils``（只有 main.py 一处引用），xray 侧保持原样。

3. **模型按需懒加载**
   五个模型的 ONNX 合计约 300MB，全部预加载会明显拖慢启动，
   因此只在用户第一次点某个功能时才加载对应模型，之后缓存在单例里。

4. **结果统一形状**
   五个模块各自的返回结构差别很大，这里统一归一成 ``dict``，
   UI 层只认这一种格式。
"""

import contextlib
import os
import sys
import traceback

# 嵌入式环境 DLL 搜索路径引导：sqlite3 依赖 pyenv\Library\bin\sqlite3.dll，
# 桌面快捷方式不经 run_env.bat 时该目录不在 PATH，反馈功能会因
# 「DLL load failed while importing _sqlite3」直接报错。这里先兜底修正。
try:
    import runtime_env as _runtime_env
    _runtime_env.bootstrap()
except Exception:            # noqa: BLE001
    pass

import numpy as np

# ---------------------------------------------------------------------------
# xray 子系统路径引导
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
XRAY_ROOT = os.path.join(_HERE, "xray")
XRAY_APP = os.path.join(XRAY_ROOT, "app")

# 用 append 而不是 insert(0)：xray/app 下也有 main.py，插到最前会让
# `import main` 指到 xray 的主程序上去。
if XRAY_APP not in sys.path:
    sys.path.append(XRAY_APP)


def xray_available() -> bool:
    """xray 子系统是否可导入"""
    return os.path.isdir(XRAY_APP) and os.path.isdir(
        os.path.join(XRAY_APP, "core"))


# ---------------------------------------------------------------------------
# SQLite 打开辅助
#
# sqlite3 是标准库，但本项目是嵌入式 Python：_sqlite3.pyd 依赖
# pyenv\Library\bin\sqlite3.dll。从桌面快捷方式启动（不经 run_env.bat）
# 时该目录不在 PATH，import sqlite3 会抛
#   ImportError: DLL load failed while importing _sqlite3
# 反馈按钮正是因此弹错。这里做两件事：
#   1) 导入失败时自动补一次 DLL 搜索路径再重试（自愈）
#   2) 仍失败则抛一个带中文说明的 RuntimeError，让 UI 弹窗能看懂
# ---------------------------------------------------------------------------

_SQLITE_HINT = (
    "无法加载 SQLite 运行库（sqlite3.dll）。\n"
    "这通常是因为程序不是通过 run_env.bat 启动的，导致嵌入式环境目录未加入 PATH。\n"
    "请改用「启动程序.bat」或桌面快捷方式重新打开软件。"
)


def _import_sqlite():
    """导入 sqlite3，失败时先自愈一次 DLL 搜索路径。"""
    try:
        import sqlite3
        return sqlite3
    except ImportError as exc:
        # 自愈：补上嵌入式环境的 DLL 目录后再试一次
        try:
            import runtime_env as _rt
            _rt.bootstrap()
        except Exception:            # noqa: BLE001
            pass
        try:
            import sqlite3
            return sqlite3
        except ImportError:
            raise RuntimeError("%s\n\n原始错误：%s" % (_SQLITE_HINT, exc))


def _open_sqlite(db_path):
    """打开（必要时创建）SQLite 连接并确保父目录存在。"""
    sqlite3 = _import_sqlite()
    parent = os.path.dirname(db_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception:            # noqa: BLE001
            pass
    return sqlite3.connect(db_path)


def probe_sqlite():
    """探测 SQLite 是否可用。返回 (ok: bool, message: str)。

    供 UI 在打开反馈窗口前做自检，把「DLL 加载失败」这种天书错误
    提前换成一句人能看懂、能对症处理的话。
    """
    try:
        sqlite3 = _import_sqlite()
    except Exception as exc:        # noqa: BLE001
        return False, str(exc)
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE IF NOT EXISTS _probe (id INTEGER)")
        conn.close()
    except Exception as exc:        # noqa: BLE001
        return False, "SQLite 数据库不可写：%s" % exc
    return True, ""


# ---------------------------------------------------------------------------
# 统一结果结构
# ---------------------------------------------------------------------------

def _blank_result(kind):
    return {
        "kind": kind,
        "success": False,
        "image": None,       # 已画好标注的 RGB ndarray（uint8）
        "items": [],         # [{"title","detail","confidence"}]
        "summary": "",       # 纯文本摘要
        "report": "",        # HTML 报告
        "copy_text": "",     # 「复制结果」用的纯文本（不设则由摘要+条目拼装）
        "metrics": {},       # 关键指标（Cobb 角、骨龄、足弓角...）
        "keypoints": None,   # {idx: (x, y)} 关键点的原始图像像素坐标，供手动微调
        "keypoint_names": [],
        # 标注图层（目前只有骨龄用）：raw_image 是无标注底图，
        # annotations 是 [{key,name,level,score,box}]，供界面「显示关节标注」
        # 开关与图例使用。其他检测类型两者为空，界面侧按需隐藏那组控件。
        "raw_image": None,
        "annotations": [],
        # 分级质量（目前只有骨龄用）：
        #   low_confidence_joints —— 置信度偏低但已按最佳判定计分的关节（图上等级后带 ?）
        #   ungraded_joints       —— 确实没评上等级、未计入总分的关节
        # 两者分开，是因为前者「有分但不稳」、后者「没分并拉低骨龄」，处理方式不同。
        "low_confidence_joints": [],
        "ungraded_joints": [],
    }


def _strip_html(text):
    import re
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", str(text)).strip()


# ---------------------------------------------------------------------------
# 中文标注绘制（cv2.putText 不支持中文，这里统一走 PIL）
# ---------------------------------------------------------------------------

_FONT_CACHE = {}


def _pil_font(size):
    key = int(size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    from PIL import ImageFont
    font = None
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                font = ImageFont.truetype(path, key)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def draw_detections_rgb(image_rgb, detections, color_of=None):
    """在 RGB 图上画检测框 + 中文标签，返回新的 RGB 图。

    detections: [{"bbox":(x1,y1,x2,y2), "label":str, "confidence":float}]
    """
    from PIL import Image, ImageDraw
    img = Image.fromarray(np.ascontiguousarray(image_rgb))
    draw = ImageDraw.Draw(img)
    w, h = img.size
    font = _pil_font(max(14, int(min(w, h) / 55)))

    for det in detections:
        x1, y1, x2, y2 = [int(v) for v in det.get("bbox", (0, 0, 0, 0))]
        if color_of is not None:
            color = color_of(det)
        else:
            color = tuple(int(c) for c in det.get("color", (0, 200, 255)))
        color = tuple(int(c) for c in color)
        for t in range(3):          # 加粗一点，大图上细框看不清
            draw.rectangle([x1 - t, y1 - t, x2 + t, y2 + t], outline=color)

        label = det.get("label", "")
        conf = det.get("confidence", None)
        if conf is not None:
            label = f"{label} {float(conf):.2f}"
        if label:
            # Pillow 10 移除了 textsize/font.getsize，统一用 textbbox
            try:
                _box = draw.textbbox((0, 0), label, font=font)
                tw, th = _box[2] - _box[0], _box[3] - _box[1]
            except Exception:
                tw, th = len(label) * font.size, font.size
            ty = y1 - th - 6 if y1 - th - 6 > 0 else y2 + 4
            draw.rectangle([x1, ty, x1 + tw + 8, ty + th + 6], fill=color)
            draw.text((x1 + 4, ty + 3), label, fill=(0, 0, 0), font=font)
    return np.asarray(img)


# ---------------------------------------------------------------------------
# X 线负片（MONOCHROME1）输入纠正
# ---------------------------------------------------------------------------
#
# 问题：xray 的骨折 / 脊柱 / 骨龄三个引擎走 .dcm 分支时**只做窗值裁剪、
# 不做 MONOCHROME1 反相**（详见 core/model/fracture_inference._load_image、
# spine_inference._load_image、boneage_inference._load_image）。而国产 DR
# 设备（实测：颈椎/腰椎正位）就是按 MONOCHROME1 存档的，于是：
#   1) 模型吃进去的是负片，与它训练时看到的正常极性相反；
#   2) 画出来的 result_image 整幅是负片，和 CT3D 二维视图的正确极性不一致。
# 实测：两张 DR 片子的检测结果图与正确渲染的相关系数都是 -1.0000（严格取反）。
#
# 处理：既然是「输入」不对，就在入口处把输入纠正过来，而不是事后反相结果图
# ——事后反相会把检测框/中文标签的颜色一起翻转（红变青）。这里把片子按正确
# 极性渲染成一张 8bit 灰度图暂存，交给引擎，退出时删掉。
# 该引擎的 .dcm 分支本来就是「裁窗 → 归一化到 uint8」，所以换成 8bit 图不会
# 损失可用精度；非负片（MONOCHROME2）的片子仍原样走原路径，不做任何改动。

def _is_mono1_dicom(path) -> bool:
    if not path or not str(path).lower().endswith((".dcm", ".dicom")):
        return False
    try:
        import pydicom
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        return str(getattr(ds, "PhotometricInterpretation", "")
                   ).strip().upper() == "MONOCHROME1"
    except Exception:
        return False


@contextlib.contextmanager
def corrected_source(ai, path):
    """给引擎一个「极性正确」的输入路径；无需纠正时原样返回。"""
    if not _is_mono1_dicom(path):
        yield path
        return
    tmp = None
    try:
        import cv2
        import tempfile
        rgb, _meta = ai.load_image_rgb(path)     # 该加载器已做 MONOCHROME1 反相
        arr = np.asarray(rgb)
        if arr.ndim == 3:
            arr = arr[:, :, :3]
        fd, tmp = tempfile.mkstemp(prefix="xr_polarity_", suffix=".png")
        os.close(fd)
        if arr.ndim == 3:
            cv2.imwrite(tmp, cv2.cvtColor(arr, cv2.COLOR_RGB2BGR))
        else:
            cv2.imwrite(tmp, arr)
        print("[XRAY] MONOCHROME1 片子已按正确极性渲染为临时图: %s" % tmp)
        yield tmp
    except Exception as exc:                     # noqa: BLE001
        print("[XRAY] 负片纠正失败，退回原始输入: %s" % exc)
        yield path
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 单例引擎（懒加载）
# ---------------------------------------------------------------------------

class XrayAI(object):
    """xray AI 能力的统一入口。所有模型按需加载并缓存。"""

    def __init__(self):
        self._pre = None
        self._chest = None
        self._fracture = None
        self._spine = None
        self._boneage = None
        self._foot = None
        self._loaded = set()
        self._errors = {}

    # ---------- 公共：图像读取 ----------

    def preprocessor(self):
        if self._pre is None:
            from core.data.preprocessor import ImagePreprocessor
            self._pre = ImagePreprocessor(target_size=640)
        return self._pre

    def load_image_rgb(self, path):
        """读 DICOM / JPG / PNG，返回 (RGB ndarray, metadata dict)"""
        return self.preprocessor().load_image(path)

    @staticmethod
    def detect_body_part(path):
        """按 DICOM 的 BodyPartExamined 判断适合哪种检测"""
        if not path or not str(path).lower().endswith((".dcm", ".dicom")):
            return None
        try:
            from core.data.body_part_detector import BodyPartDetector
            return BodyPartDetector.detect_from_dicom(path)
        except Exception:
            return None

    # ---------- 模型懒加载 ----------

    def _mark(self, name, ok, err=None):
        if ok:
            self._loaded.add(name)
        else:
            self._errors[name] = str(err) if err else "模型加载失败"

    def chest_engine(self):
        if self._chest is None:
            try:
                from core.model import get_inference_engine
                self._chest = get_inference_engine(backend="onnx")
                self._mark("chest", self._chest is not None)
            except Exception as e:
                self._mark("chest", False, e)
                traceback.print_exc()
        return self._chest

    def fracture_engine(self):
        if self._fracture is None:
            try:
                from core.model.fracture_inference import get_fracture_inference
                self._fracture = get_fracture_inference()
                self._mark("fracture", self._fracture is not None)
            except Exception as e:
                self._mark("fracture", False, e)
                traceback.print_exc()
        return self._fracture

    def spine_engine(self):
        if self._spine is None:
            try:
                from core.model.spine_inference import get_spine_inference
                self._spine = get_spine_inference()
                self._mark("spine", self._spine is not None)
            except Exception as e:
                self._mark("spine", False, e)
                traceback.print_exc()
        return self._spine

    def boneage_engine(self):
        if self._boneage is None:
            try:
                from core.model.boneage_inference import get_boneage_inference
                self._boneage = get_boneage_inference()
                self._mark("boneage", self._boneage is not None)
            except Exception as e:
                self._mark("boneage", False, e)
                traceback.print_exc()
        return self._boneage

    def foot_engine(self):
        if self._foot is None:
            try:
                from core.model.foot_arch_inference import get_foot_arch_inference
                self._foot = get_foot_arch_inference()
                self._mark("foot_arch", self._foot is not None)
            except Exception as e:
                self._mark("foot_arch", False, e)
                traceback.print_exc()
        return self._foot

    # ---------- 各项检测 ----------

    def run_chest(self, image_rgb, conf=None, image_path=None,
                  age_years=None):
        res = _blank_result("chest")
        eng = self.chest_engine()
        if eng is None:
            res["error"] = self._errors.get("chest", "胸片模型未加载")
            return res
        settings = load_detection_settings()
        eng.conf_thresh = float(conf if conf is not None
                                else settings.get("chest_conf", 0.25))
        tensor, scale, pad = self.preprocessor().preprocess(image_rgb)
        dets = eng.infer(tensor, scale, pad) or []

        # ---- 儿科心影校正（v1.9.3）----
        # 儿童（<15 岁）在胸片上的心影本来就相对大，模型在儿童片上几乎必报
        # Cardiomegaly。这里按估计的心胸比与该年龄的生理上限做二次判读：
        # 只有超出上限才作为病变报出，否则降级为「生理性」提示。
        # 注意：这一层只处理 Cardiomegaly，其它 13 类的判定完全不受影响。
        if settings.get("paed_cardio_correct", True) and \
                (age_years is not None or image_path):
            dets, res["paediatric"] = self._review_paed_cardiomegaly(
                image_rgb, dets, image_path, age_years)

        items = []
        for d in dets:
            paed = d.get("_paed") or {}
            title = d.get("class_name_cn") or d.get("class_name")
            if paed.get("demoted"):
                title = d.get("_paed_label") or title
            items.append({
                "title": title,
                "detail": d.get("_paed_detail") or str(d.get("class_name") or ""),
                "confidence": float(d.get("confidence", 0.0)),
            })
        boxes = [{
            "bbox": tuple(d["bbox"]),
            "label": d.get("_paed_label") or d.get("class_name_cn")
                     or d.get("class_name", ""),
            "confidence": d.get("confidence"),
            "color": tuple(int(c) for c in d.get("_paed_color")
                           or eng.colors[d["class_id"]]),
        } for d in dets]
        res["image"] = draw_detections_rgb(image_rgb, boxes)
        res["items"] = items
        res["success"] = True
        n_pos = sum(1 for d in dets
                    if not (d.get("_paed") or {}).get("demoted"))
        res["summary"] = (f"共检出 {n_pos} 处病变" if n_pos
                          else "未检出明显病变")
        return res

    @staticmethod
    def _review_paed_cardiomegaly(image_rgb, dets, image_path, age_years):
        """给 dets 里的 Cardiomegaly 打儿科二次判读标记。

        返回 ``(dets, info)``：``info`` 为 dict 或 None，用于 UI 展示依据。
        任何异常都不影响检测本身 —— 校正失败就原样返回。
        """
        from ct3d_utils import (PAEDIATRIC_MAX_AGE, estimate_ctr,
                                patient_age_years, review_cardiomegaly)

        if not dets:
            return dets, None
        try:
            if age_years is None and image_path:
                age_years = patient_age_years(path=image_path)
            if age_years is None:
                return dets, {"applied": False, "age": None,
                              "text": "未能读取患者年龄，未做儿科心影校正"}
            if float(age_years) >= PAEDIATRIC_MAX_AGE:
                return dets, None

            arr = np.asarray(image_rgb)
            gray = arr[:, :, :3].mean(axis=2) if arr.ndim == 3 else arr
            if gray.size == 0:
                return dets, None

            info = {"applied": True, "age": float(age_years), "notes": []}
            for d in dets:
                name = str(d.get("class_name") or d.get("class_name_cn")
                           or "").strip().lower()
                if name != "cardiomegaly":
                    continue
                ctr = estimate_ctr(gray, d.get("bbox"))
                verdict = review_cardiomegaly(age_years, ctr,
                                              d.get("confidence", 0.0))
                d["_paed"] = verdict
                if verdict["keep"]:
                    d["_paed_detail"] = verdict.get("text") or str(
                        d.get("class_name") or "")
                else:
                    d["_paed"] = dict(verdict, demoted=True)
                    d["_paed_label"] = verdict.get(
                        "label") or "心影偏大（儿童生理性）"
                    d["_paed_detail"] = verdict.get("text", "")
                    # 灰框：儿童生理性偏大，不是病变
                    d["_paed_color"] = ((150, 150, 150)
                                        if verdict.get("level") == "physiologic"
                                        else (198, 160, 70))
                if verdict.get("text"):
                    info["notes"].append(verdict["text"])
            return dets, info
        except Exception as exc:                 # noqa: BLE001
            traceback.print_exc()
            return dets, {"applied": False, "age": age_years,
                          "text": "儿科心影校正未执行：%s" % exc}

    def run_fracture(self, path, conf=None):
        res = _blank_result("fracture")
        eng = self.fracture_engine()
        if eng is None:
            res["error"] = self._errors.get("fracture", "骨折模型未加载")
            return res
        settings = load_detection_settings()
        with corrected_source(self, path) as src:
            out = eng.detect(src, conf_threshold=float(
                conf if conf is not None
                else settings.get("fracture_conf", 0.13)))
        if not out or not out.get("success"):
            res["error"] = (out or {}).get("error", "骨折检测失败")
            return res
        for d in out.get("detections", []):
            box = getattr(d, "box", None) or (0, 0, 0, 0)
            items_row = {
                "title": "骨折",
                "detail": f"位置 ({int(box[0])}, {int(box[1])})",
                "confidence": float(getattr(d, "confidence", 0.0)),
            }
            res["items"].append(items_row)
        img = out.get("result_image")
        if img is not None:
            import cv2
            res["image"] = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        res["success"] = True
        res["report"] = out.get("report", "")
        res["metrics"] = {"骨折处数": int(out.get("fracture_count", 0))}
        res["summary"] = ("检出 %d 处骨折" % out.get("fracture_count", 0)
                          if out.get("has_fracture") else "未检出骨折")
        return res

    def run_spine(self, path, conf=None):
        res = _blank_result("spine")
        eng = self.spine_engine()
        if eng is None:
            res["error"] = self._errors.get("spine", "脊柱模型未加载")
            return res
        with corrected_source(self, path) as src:
            out = eng.detect(src, conf_threshold=float(conf or 0.25))
        if not out or not out.get("success"):
            res["error"] = (out or {}).get("error", "脊柱检测失败")
            return res
        cls = out.get("spine_class") or {}
        angle = out.get("cobb_angle")
        if cls:
            res["items"].append({
                "title": cls.get("cls_name", ""),
                "detail": f"椎体 {out.get('vertebra_count', 0)} 个",
                "confidence": float(cls.get("confidence", 0.0)),
            })
        img = out.get("result_image")
        if img is not None:
            import cv2
            res["image"] = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        res["success"] = True
        res["report"] = out.get("report", "")
        # 两条终板线的四个端点交给上层，供「手动微调 Cobb 线」拖端点重算角度。
        # 顺序与 cobb_points 一致：上端椎 1/2、下端椎 3/4。
        kps = out.get("cobb_points") or {}
        if kps:
            res["keypoints"] = {
                int(k): (float(p[0]), float(p[1]))
                for k, p in sorted(kps.items(), key=lambda kv: kv[0])}
            res["keypoint_names"] = ["上端椎1", "上端椎2",
                                     "下端椎1", "下端椎2"]
        res["metrics"] = {
            "Cobb角": ("%.1f°" % angle) if angle is not None else "—",
            "椎体数": out.get("vertebra_count", 0),
        }
        res["summary"] = ("Cobb 角 %.1f°" % angle
                          if angle is not None else "未能测量 Cobb 角")
        # 「复制结果」只要 Cobb 角——分型/椎体数等框内条目是界面参考信息，
        # 不进剪贴板（2026-10 按临床报告口径精简）。
        res["copy_text"] = (("Cobb 角 %.1f°" % angle) if angle is not None
                            else "未能测量 Cobb 角")
        return res

    def run_boneage(self, path, sex):
        """sex: 'boy' / 'girl'"""
        res = _blank_result("boneage")
        if sex not in ("boy", "girl"):
            res["error"] = "骨龄评估需要患者性别（男/女）"
            return res
        eng = self.boneage_engine()
        if eng is None:
            res["error"] = self._errors.get("boneage", "骨龄模型未加载")
            return res
        settings = load_detection_settings()
        with corrected_source(self, path) as src:
            out = eng.detect(
                src, sex,
                detect_conf=float(settings.get("boneage_detect_conf", 0.30)),
                cls_conf=float(settings.get("boneage_cls_conf", 0.30)))
        if not out or not out.get("success"):
            res["error"] = (out or {}).get("error", "骨龄评估失败")
            return res
        # 条目按 RUS-CHN 报告顺序排列，标题用中文（不再用 MCPFirst 等
        # 英文缩写），详情格式「N级，得M分」与报告「影像所见」一致。
        #
        # 2026-11：等级名统一取 annotations[i]["grade"]（RUS-CHN 1~8 级），
        # 不再直接用 level —— level 是内部细分档序号，会写出「12级」这种
        # RUS-CHN 里不存在的说法，而且与标注图上的「8级」对不上。
        # 未分级的关节也列出来（此前静默跳过，13 项里少一行医生很难察觉）。
        _anns = {a.get("key"): a for a in (out.get("annotations") or [])}
        for name in BONEAGE_REPORT_ORDER:
            a = _anns.get(name)
            cn = BONEAGE_JOINT_CN.get(name, name)
            if not a or a.get("level") is None:
                res["items"].append({
                    "title": cn,
                    "detail": "未分级（未计入总分）",
                    "confidence": None,
                })
                continue
            grade = a.get("grade") or str(a.get("level"))
            detail = "%s级，得%d分" % (grade, int(a.get("score") or 0))
            if a.get("low_confidence"):
                detail += "（低置信，建议复核）"
            res["items"].append({
                "title": cn,
                "detail": detail,
                "confidence": None,
            })
        img = out.get("result_image")
        if img is not None:
            import cv2
            res["image"] = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        # 骨龄的「标注」是 13 个关节框 + 中文标签，已烘在 result_image 里；
        # 同时带回无标注底图，界面才能做「显示关节标注」的开关。
        raw = out.get("raw_image")
        if raw is not None:
            import cv2
            res["raw_image"] = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
        res["annotations"] = out.get("annotations") or []
        low_conf = list(out.get("low_confidence_joints") or [])
        ungraded = list(out.get("ungraded_joints") or [])
        res["low_confidence_joints"] = low_conf
        res["ungraded_joints"] = ungraded
        res["success"] = True
        res["report"] = out.get("report", "")
        # 摘要即报告的「影像提示」行；指标不再单列（总分/骨龄已在摘要里，
        # 重复显示反而挤占版面）。部分关节分级失败时补一句实际关节数。
        joint_count = int(out.get("joint_count", 0) or 0)
        summary = ("RUS-CHN 分级计分法，受检儿 CHN 总得分：%d分，"
                   "骨龄约为%s岁"
                   % (int(out.get("total_score", 0) or 0),
                      _fmt_bone_age(out.get("bone_age", 0.0))))
        if 0 < joint_count < 13:
            summary += "（实际分级 %d/13 个关节）" % joint_count
        if low_conf:
            summary += "（其中 %d 项低置信，建议复核）" % len(low_conf)
        summary += "。"
        res["summary"] = summary
        res["metrics"] = {}
        # 「复制结果」直接用报告全文（影像所见/影像提示 两段式模板），
        # 界面列表仍显示逐关节条目，两边同源不同排布。
        res["copy_text"] = res["report"]
        return res

    def run_foot_arch(self, image_rgb):
        res = _blank_result("foot_arch")
        eng = self.foot_engine()
        if eng is None:
            res["error"] = self._errors.get("foot_arch", "足弓模型未加载")
            return res
        out = eng.predict_from_array(image_rgb)
        if not out or not out.get("success"):
            res["error"] = (out or {}).get("error", "足弓测量失败")
            return res
        for idx, name in enumerate(out.get("keypoint_names") or [], start=1):
            pt = (out.get("keypoints") or {}).get(idx)
            if pt:
                res["items"].append({
                    "title": str(name),
                    "detail": "(%d, %d)" % (int(pt[0]), int(pt[1])),
                    "confidence": None,
                })
        img = out.get("result_image")
        if img is not None:
            import cv2
            res["image"] = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        # 关键点交给上层做手动微调：模型给的坐标是「原始图像像素」（行序与
        # result_image 一致，都是 cv2/pydicom 的「上→下」），因此拖动手柄时
        # 不用再关心 letterbox 缩放，也不用再翻行序。
        res["keypoints"] = {
            int(k): (int(round(float(pt[0]))), int(round(float(pt[1]))))
            for k, pt in (out.get("keypoints") or {}).items()
        }
        res["keypoint_names"] = list(out.get("keypoint_names") or [])
        res["success"] = True
        res["report"] = out.get("report", "")
        medial = out.get("medial_angle")
        lateral = out.get("lateral_angle")
        res["metrics"] = {
            "内侧足弓角": ("%.1f°" % medial) if medial is not None else "—",
            "外侧足弓角": ("%.1f°" % lateral) if lateral is not None else "—",
        }
        res["summary"] = _strip_html(out.get("report", ""))[:120]
        # 「复制结果」只要两个足弓角度——关键点坐标是界面/微调参考信息，
        # 不进剪贴板（2026-10 按临床报告口径精简）。
        copy_lines = ["%s %s" % (k, v)
                      for k, v in res["metrics"].items() if v != "—"]
        res["copy_text"] = "\n".join(copy_lines) if copy_lines \
            else "未能测量足弓角"
        return res


# ---------------------------------------------------------------------------
# 检测阈值配置（直接读 JSON，不再实例化 QDialog）
# ---------------------------------------------------------------------------

def load_detection_settings():
    """读取 xray 的检测阈值配置。

    原实现 ``get_detection_settings()`` 每次都 new 一个 SettingsDialog 再读文件，
    在没有 QApplication 的上下文里会崩，也白白构造 Qt 对象，这里直接读文件。
    """
    defaults = {
        "chest_conf": 0.25,
        "boneage_detect_conf": 0.30,
        "boneage_cls_conf": 0.30,
        "fracture_conf": 0.13,
        # 部位匹配护栏的开关：默认开启护栏（False = 不忽略部位限制）
        "ignore_part_scope": False,
        # 儿科心影校正：默认开启（False = 关闭校正，恢复成人判定逻辑）
        "paed_cardio_correct": True,
    }
    path = os.path.join(XRAY_APP, "data", "detection_settings.json")
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in defaults:
            if k in data:
                defaults[k] = data[k]
    except Exception:
        pass
    return defaults


def save_detection_settings(values: dict) -> bool:
    """把检测阈值写回 xray 的 detection_settings.json"""
    path = os.path.join(XRAY_APP, "data", "detection_settings.json")
    try:
        import json
        data = {}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        data.update(values)
        data["settings_version"] = 2
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        traceback.print_exc()
        return False


# 检测类型 -> 展示名 / 模型版本号
KIND_META = {
    "chest":     ("胸片检测",  "YOLOv8-CXR"),
    "fracture":  ("骨折检测",  "Fracture-YOLOv8s"),
    "spine":     ("脊柱侧弯",  "Spine-YOLOv8"),
    "boneage":   ("骨龄评估",  "RUS-CHN v1.0"),
    "foot_arch": ("足弓测量",  "FootArch-ResNet50"),
}

# 骨龄 13 关节的中文名与报告顺序（与 boneage_annotation.py / tools.py 保持
# 一致；2026-10 全中文化——结果列表不再显示 MCPFirst 等英文缩写）。
# 本地保留一份映射而非 import：bridge 导入期不能依赖 xray 子包的 sys.path。
BONEAGE_REPORT_ORDER = (
    'MCPFirst', 'MCPThird', 'MCPFifth',
    'PIPFirst', 'PIPThird', 'PIPFifth',
    'MIPThird', 'MIPFifth',
    'DIPFirst', 'DIPThird', 'DIPFifth',
    'Ulna', 'Radius',
)
BONEAGE_JOINT_CN = {
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
    try:
        text = ('%.2f' % float(value)).rstrip('0').rstrip('.')
    except Exception:
        text = ''
    return text if text else '0'


class DetectionRecordStore(object):
    """检测记录存取。

    与 xray 原系统共用同一个 SQLite 库（保证历史记录延续），但做了两处关键
    修正：

    1. **不再把 result_image 写进库**。原实现把整张标注图（numpy 序列化后
       数 MB ~ 数十 MB）塞进 results_json，99 条记录就让库涨到 2.96 GB
       （实测 avg 20.6 MB/条，max 62 MB）。这里只存文本摘要与关键指标。
    2. **列表查询做列裁剪**。SQLite 对未选中的大字段不会读取，因此即使库里
       还留着历史大字段，列表查询依然只需 0.17s。
    """

    COLUMNS = ("id, detection_time, patient_name, patient_id, access_no, "
               "study_date, image_path, detection_type, summary, model_version")

    def __init__(self, db_path=None):
        self.db_path = db_path or os.path.join(
            XRAY_APP, "data", "detection_records.db")

    # ---------- 连接与建表 ----------

    def _connect(self):
        conn = _open_sqlite(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detection_records (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                detection_time TEXT NOT NULL,
                patient_name   TEXT,
                patient_id     TEXT,
                access_no      TEXT,
                study_date     TEXT,
                image_path     TEXT,
                detection_type TEXT,
                results_json   TEXT,
                summary        TEXT,
                model_version  TEXT,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        return conn

    def available(self) -> bool:
        return os.path.isfile(self.db_path)

    # ---------- 写入 ----------

    def add(self, kind, summary, metrics=None, patient_name="", patient_id="",
            access_no="", study_date="", image_path=""):
        """新增一条检测记录（只存文本，不存图像）"""
        try:
            import json
            import datetime
            name, version = KIND_META.get(kind, (kind, ""))
            payload = {"kind": kind, "kind_cn": name, "metrics": metrics or {}}
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO detection_records (detection_time, patient_name,"
                    " patient_id, access_no, study_date, image_path,"
                    " detection_type, results_json, summary, model_version)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     patient_name, patient_id, access_no, study_date, image_path,
                     kind, json.dumps(payload, ensure_ascii=False),
                     _strip_html(summary), version))
                conn.commit()
            finally:
                conn.close()
            return True
        except Exception:
            traceback.print_exc()
            return False

    # ---------- 查询 ----------

    def list(self, page=1, per_page=30, search=None, kind=None):
        """返回 (rows, total)。row 为 dict。"""
        try:
            conn = self._connect()
            try:
                where, args = [], []
                if search:
                    where.append("(patient_name LIKE ? OR summary LIKE ?"
                                 " OR access_no LIKE ?)")
                    args += ["%%%s%%" % search] * 3
                if kind:
                    where.append("detection_type = ?")
                    args.append(kind)
                sql_where = (" WHERE " + " AND ".join(where)) if where else ""
                total = conn.execute(
                    "SELECT COUNT(*) FROM detection_records" + sql_where,
                    args).fetchone()[0]
                cur = conn.execute(
                    "SELECT %s FROM detection_records%s"
                    " ORDER BY id DESC LIMIT ? OFFSET ?"
                    % (self.COLUMNS, sql_where),
                    args + [int(per_page), (int(page) - 1) * int(per_page)])
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                return rows, total
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
            return [], 0

    def max_id(self) -> int:
        """当前最大主键。测试/清理要以它为基线，不能用「记录条数」代替——

        id 是自增主键且历史上删过行，条数与 id 并不相等（实测：98 条记录
        的 id 范围是 156~253），拿条数当基线比较会误删真实历史记录。
        """
        try:
            conn = self._connect()
            try:
                v = conn.execute(
                    "SELECT MAX(id) FROM detection_records").fetchone()[0]
                return int(v or 0)
            finally:
                conn.close()
        except Exception:
            return 0

    def delete_above(self, baseline_id):
        """删除 id > baseline_id 的记录（用于清理本次会话新写入的测试数据）。

        返回删除条数；baseline_id 为 0 时不做任何删除（防止误清空整库）。
        """
        if not baseline_id:
            return 0
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM detection_records WHERE id > ?",
                    (int(baseline_id),))
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
            return 0

    def delete(self, ids):
        if not ids:
            return 0
        try:
            conn = self._connect()
            try:
                q = "DELETE FROM detection_records WHERE id IN (%s)" % \
                    ",".join("?" * len(ids))
                cur = conn.execute(q, list(ids))
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        except Exception:
            return 0

    def clear(self):
        try:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM detection_records")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        except Exception:
            return 0

    # ---------- 导出 ----------

    def export_csv(self, out_path, search=None, kind=None):
        try:
            import csv
            rows, _total = self.list(1, 100000, search, kind)
            if not rows:
                return None
            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            return out_path
        except Exception:
            traceback.print_exc()
            return None

    # ---------- 维护 ----------

    def stats(self):
        try:
            import os as _os
            conn = self._connect()
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM detection_records").fetchone()[0]
                today = conn.execute(
                    "SELECT COUNT(*) FROM detection_records"
                    " WHERE date(detection_time) = date('now','localtime')"
                ).fetchone()[0]
                big = conn.execute(
                    "SELECT SUM(LENGTH(results_json)) FROM detection_records"
                ).fetchone()[0] or 0
            finally:
                conn.close()
            return {"total": total, "today": today,
                    "db_bytes": _os.path.getsize(self.db_path) if
                    _os.path.isfile(self.db_path) else 0,
                    "blob_bytes": int(big)}
        except Exception:
            return {"total": 0, "today": 0, "db_bytes": 0, "blob_bytes": 0}

    def compact(self):
        """清掉历史记录里冗余的图像数据并回收磁盘空间。

        只丢弃 results_json 中的图像，检测时间/患者/摘要/模型版本等
        全部保留（这些才是记录的价值；图像随时可以重新检测得到）。
        """
        try:
            import shutil
            backup = self.db_path + ".bak"
            if not os.path.isfile(backup):
                shutil.copy2(self.db_path, backup)
            conn = self._connect()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS detection_records_slim AS
                    SELECT id, detection_time, patient_name, patient_id,
                           access_no, study_date, image_path, detection_type,
                           '' AS results_json, summary, model_version, created_at
                    FROM detection_records""")
                conn.execute("DELETE FROM detection_records_slim")
                conn.execute("""
                    INSERT INTO detection_records_slim
                    SELECT id, detection_time, patient_name, patient_id,
                           access_no, study_date, image_path, detection_type,
                           '' AS results_json, summary, model_version, created_at
                    FROM detection_records""")
                conn.execute("DROP TABLE detection_records")
                conn.execute("ALTER TABLE detection_records_slim"
                             " RENAME TO detection_records")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_detection_time"
                             " ON detection_records(detection_time)")
                conn.execute("COMMIT")
                conn.execute("VACUUM")
            finally:
                conn.close()
            return True, backup
        except Exception as exc:          # noqa: BLE001
            traceback.print_exc()
            return False, str(exc)


# ---------------------------------------------------------------------------
# 漏检反馈（用户报告「模型没识别出来」的片子）
# ---------------------------------------------------------------------------

# 反馈类型 -> (中文名, 默认备注提示)
FEEDBACK_KINDS = {
    "missed_fracture": "骨折漏检",
    "missed_other":    "其他漏检",
    "false_positive":  "误报（实际没有）",
    "wrong_result":    "结果不正确",
}


class FeedbackStore(object):
    """漏检反馈的落盘与检索。

    设计要点（都是被 2.76GB 的旧库教出来的）：

    1. **图像绝不进数据库**。图片以 PNG 存到软件目录的
       ``feedback_images/YYYYMMDD/`` 下，库里只存**相对路径**。
       一张 3000x3000 的标注图若 json 序列化进 SQLite，单条就是几十 MB
       （旧库实测 avg 16.3MB/条、max 59.6MB，118 条涨到 2.76GB）。
    2. **独立表** ``ai_feedback``，不与 xray 原系统的 ``detection_records``
       混用 —— 反馈是"训练素材"，检测记录是"诊断流水"，语义不同。
       同库不同表，方便日后把反馈集整体导出成训练集。
    3. 存的是**原始 DICOM 的拷贝**（不是标注图）。漏检的片子本身没有
       标注，原图才是训练需要的东西；同时保留原始 DICOM 才能复查。
    """

    COLUMNS = ("id, feedback_time, feedback_type, patient_name, patient_id,"
               " access_no, study_date, body_part, source_path, image_file,"
               " note, detector, detect_result, model_version")

    def __init__(self, db_path=None, image_root=None):
        self.db_path = db_path or os.path.join(
            XRAY_APP, "data", "detection_records.db")
        # 图片放软件目录里（而不是用户数据目录），方便直接打包/拷贝
        # 用 _HERE（= 本文件所在目录 = 软件根），不要 dirname(dirname(__file__))：
        # xray_bridge.py 就在软件根下，多退一层会落到**上一级目录**。
        self.image_root = image_root or os.path.join(_HERE, "feedback_images")

    # ---------- 连接与建表 ----------

    def _connect(self):
        conn = _open_sqlite(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_time TEXT NOT NULL,
                feedback_type TEXT NOT NULL,
                patient_name  TEXT,
                patient_id    TEXT,
                access_no     TEXT,
                study_date    TEXT,
                body_part     TEXT,
                source_path   TEXT,
                image_file    TEXT,
                note          TEXT,
                detector      TEXT,
                detect_result TEXT,
                model_version TEXT,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_time"
            " ON ai_feedback(feedback_time)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_type"
            " ON ai_feedback(feedback_type)")
        return conn

    def available(self) -> bool:
        return os.path.isfile(self.db_path)

    # ---------- 图像落盘 ----------

    def _export_image(self, src_path, tag="", date_str=None):
        """把源影像渲染成 PNG 存到 feedback_images/<日期>/，返回相对路径。

        优先用 pydicom 读原始像素并按 1%~99% 分位拉伸 —— 国产 DR 的设备窗值
        量纲与显示端不一致（实测 WW6880/WL12943），直接用会存下一张发灰的图。
        非 DICOM（png/jpg）直接原样拷贝。
        """
        import datetime
        import shutil
        if not src_path or not os.path.isfile(src_path):
            return None
        day = date_str or datetime.datetime.now().strftime("%Y%m%d")
        day_dir = os.path.join(self.image_root, day)
        os.makedirs(day_dir, exist_ok=True)

        base = os.path.splitext(os.path.basename(src_path))[0][:40]
        safe = "".join(c for c in base if c.isalnum() or c in "-_") or "img"
        stamp = datetime.datetime.now().strftime("%H%M%S")
        name = "%s_%s_%s.png" % (stamp, safe, tag) if tag else "%s_%s.png" % (stamp, safe)
        out = os.path.join(day_dir, name)

        ext = os.path.splitext(src_path)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"):
            shutil.copy2(src_path, out)
            return os.path.join(day, name)

        try:
            import numpy as np
            import cv2
            import pydicom
            ds = pydicom.dcmread(src_path)
            arr = ds.pixel_array
            if getattr(ds, "PhotometricInterpretation", "") == "MONOCHROME1":
                # 负片：按整幅 min/max 镜像，训练集要的是"骨头亮"的正片
                arr = arr.max() + arr.min() - arr
            arr = np.asarray(arr, dtype=np.float32)
            lo, hi = np.percentile(arr, 1), np.percentile(arr, 99)
            if hi <= lo:
                lo, hi = float(arr.min()), float(arr.max())
            if hi <= lo:
                hi = lo + 1.0
            out8 = np.clip((arr - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
            cv2.imwrite(out, out8)
            return os.path.join(day, name)
        except Exception as exc:                  # noqa: BLE001
            traceback.print_exc()
            # 渲染失败也要留证据：原样拷贝一份（可能是压缩传输语法等）
            try:
                shutil.copy2(src_path, out)
                return os.path.join(day, name)
            except Exception:                     # noqa: BLE001
                print("[FEEDBACK] 图像落盘失败: %s" % exc)
                return None

    def image_abs_path(self, rel_path):
        """相对路径 -> 绝对路径（rel 为空返回 None）"""
        if not rel_path:
            return None
        return os.path.join(self.image_root, str(rel_path).replace("/", os.sep))

    # ---------- 写入 ----------

    def add(self, feedback_type, source_path, patient_name="", patient_id="",
            access_no="", study_date="", body_part="", note="",
            detector="", detect_result="", model_version="", tag="",
            image_rel=None, date_str=None):
        """登记一条反馈。

        ``image_rel`` 为空时才现场渲染落盘；调用方若已渲染好可直接传入，
        避免重复解码 DICOM。
        """
        try:
            import datetime
            if feedback_type not in FEEDBACK_KINDS:
                feedback_type = "missed_other"
            now = datetime.datetime.now()
            if not image_rel:
                image_rel = self._export_image(source_path, tag=tag,
                                               date_str=date_str)
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO ai_feedback (feedback_time, feedback_type,"
                    " patient_name, patient_id, access_no, study_date,"
                    " body_part, source_path, image_file, note, detector,"
                    " detect_result, model_version)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), feedback_type,
                     patient_name, patient_id, access_no, study_date,
                     body_part, source_path, image_rel or "", note,
                     detector, detect_result, model_version))
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
        except Exception as exc:                  # noqa: BLE001
            traceback.print_exc()
            print("[FEEDBACK] 写入反馈失败: %s" % exc)
            return None

    # ---------- 查询 ----------

    def list(self, page=1, per_page=30, search=None, kind=None):
        """返回 (rows, total)；row 为 dict（含 image_abs 可直接打开）"""
        try:
            conn = self._connect()
            try:
                where, args = [], []
                if search:
                    where.append("(patient_name LIKE ? OR note LIKE ?"
                                 " OR access_no LIKE ? OR body_part LIKE ?)")
                    args += ["%%%s%%" % search] * 4
                if kind:
                    where.append("feedback_type = ?")
                    args.append(kind)
                sql_where = (" WHERE " + " AND ".join(where)) if where else ""
                total = conn.execute(
                    "SELECT COUNT(*) FROM ai_feedback" + sql_where,
                    args).fetchone()[0]
                cur = conn.execute(
                    "SELECT %s FROM ai_feedback%s"
                    " ORDER BY id DESC LIMIT ? OFFSET ?"
                    % (self.COLUMNS, sql_where),
                    args + [int(per_page), (int(page) - 1) * int(per_page)])
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                for r in rows:
                    r["type_cn"] = FEEDBACK_KINDS.get(
                        r.get("feedback_type"), r.get("feedback_type"))
                    r["image_abs"] = self.image_abs_path(r.get("image_file"))
                    r["image_exists"] = bool(
                        r["image_abs"] and os.path.isfile(r["image_abs"]))
                return rows, total
            finally:
                conn.close()
        except Exception as exc:                  # noqa: BLE001
            traceback.print_exc()
            print("[FEEDBACK] 读取反馈失败: %s" % exc)
            return [], 0

    def count(self, kind=None):
        try:
            conn = self._connect()
            try:
                if kind:
                    return conn.execute(
                        "SELECT COUNT(*) FROM ai_feedback WHERE feedback_type=?",
                        (kind,)).fetchone()[0]
                return conn.execute(
                    "SELECT COUNT(*) FROM ai_feedback").fetchone()[0]
            finally:
                conn.close()
        except Exception:
            return 0

    def max_id(self) -> int:
        """与 DetectionRecordStore 同理：测试清理必须用 MAX(id) 做基线，
        不能拿「条数」当 id 基线（id 自增且历史删过行，两者不相等）。"""
        try:
            conn = self._connect()
            try:
                v = conn.execute("SELECT MAX(id) FROM ai_feedback").fetchone()[0]
                return int(v or 0)
            finally:
                conn.close()
        except Exception:
            return 0

    # ---------- 删除 / 导出 ----------

    def delete(self, ids, remove_images=True):
        """删除反馈记录。remove_images=True 时一并删掉磁盘上的图片。"""
        if not ids:
            return 0
        try:
            conn = self._connect()
            try:
                rels = []
                if remove_images:
                    q = "SELECT image_file FROM ai_feedback WHERE id IN (%s)" % \
                        ",".join("?" * len(ids))
                    rels = [r[0] for r in conn.execute(q, list(ids)).fetchall()
                            if r and r[0]]
                q = "DELETE FROM ai_feedback WHERE id IN (%s)" % \
                    ",".join("?" * len(ids))
                cur = conn.execute(q, list(ids))
                conn.commit()
                n = cur.rowcount or 0
            finally:
                conn.close()
            for rel in rels:
                try:
                    p = self.image_abs_path(rel)
                    if p and os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
            self._prune_empty_dirs()
            return n
        except Exception as exc:                  # noqa: BLE001
            traceback.print_exc()
            return 0

    def _prune_empty_dirs(self):
        """删掉空掉的日期目录，避免 feedback_images 下堆一堆空壳"""
        try:
            if not os.path.isdir(self.image_root):
                return
            for day in os.listdir(self.image_root):
                d = os.path.join(self.image_root, day)
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
        except OSError:
            pass

    def export_csv(self, out_path, search=None, kind=None):
        try:
            import csv
            rows, _total = self.list(1, 100000, search, kind)
            if not rows:
                return None
            cols = ["id", "feedback_time", "type_cn", "patient_name",
                    "patient_id", "access_no", "study_date", "body_part",
                    "detector", "detect_result", "note", "source_path",
                    "image_file"]
            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            return out_path
        except Exception as exc:                  # noqa: BLE001
            traceback.print_exc()
            return None

    def stats(self):
        try:
            conn = self._connect()
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM ai_feedback").fetchone()[0]
                today = conn.execute(
                    "SELECT COUNT(*) FROM ai_feedback"
                    " WHERE date(feedback_time) = date('now','localtime')"
                ).fetchone()[0]
                by_kind = dict(conn.execute(
                    "SELECT feedback_type, COUNT(*) FROM ai_feedback"
                    " GROUP BY feedback_type").fetchall())
            finally:
                conn.close()
            img_bytes, img_n = 0, 0
            if os.path.isdir(self.image_root):
                for dirpath, _dn, fns in os.walk(self.image_root):
                    for fn in fns:
                        p = os.path.join(dirpath, fn)
                        try:
                            img_bytes += os.path.getsize(p)
                            img_n += 1
                        except OSError:
                            pass
            return {"total": total, "today": today, "by_kind": by_kind,
                    "image_count": img_n, "image_bytes": img_bytes,
                    "image_root": self.image_root}
        except Exception as exc:                  # noqa: BLE001
            traceback.print_exc()
            return {"total": 0, "today": 0, "by_kind": {},
                    "image_count": 0, "image_bytes": 0,
                    "image_root": self.image_root}


# ---------------------------------------------------------------------------
# 后台工作线程（沿用 CT3D 的 QThread 约定，不用 QRunnable）
# ---------------------------------------------------------------------------

def _build_worker_class():
    from PyQt5 import QtCore

    class XrayAIWorker(QtCore.QThread):
        """在后台跑一次 X 线 AI 检测。

        沿用 CT3D 既有约定：QThread 子类自带信号。实测 QRunnable 的信号在
        排队期间连接尚未建立，回调会丢失。
        """
        finished = QtCore.pyqtSignal(object)   # 归一化结果 dict
        failed = QtCore.pyqtSignal(str)
        progressed = QtCore.pyqtSignal(str)

        def __init__(self, ai, kind, image_path=None, image_rgb=None,
                     sex=None, parent=None):
            super().__init__(parent)
            self.ai = ai
            self.kind = kind
            self.image_path = image_path
            self.image_rgb = image_rgb
            self.sex = sex
            self._stop = False

        def stop(self):
            self._stop = True

        def run(self):
            try:
                if self.kind == "chest":
                    self.progressed.emit("正在加载胸片模型...")
                    img = self.image_rgb
                    if img is None and self.image_path:
                        img, _meta = self.ai.load_image_rgb(self.image_path)
                    self.progressed.emit("正在检测胸片...")
                    # path 一并传进去：儿科心影校正要读 DICOM 里的患者年龄
                    out = self.ai.run_chest(img, image_path=self.image_path)
                elif self.kind == "fracture":
                    self.progressed.emit("正在加载骨折模型...")
                    out = self.ai.run_fracture(self.image_path)
                elif self.kind == "spine":
                    self.progressed.emit("正在加载脊柱模型...")
                    out = self.ai.run_spine(self.image_path)
                elif self.kind == "boneage":
                    self.progressed.emit("正在加载骨龄模型...")
                    out = self.ai.run_boneage(self.image_path, self.sex)
                elif self.kind == "foot_arch":
                    self.progressed.emit("正在加载足弓模型...")
                    img = self.image_rgb
                    if img is None and self.image_path:
                        img, _meta = self.ai.load_image_rgb(self.image_path)
                    out = self.ai.run_foot_arch(img)
                else:
                    self.failed.emit("未知的 AI 检测类型: %s" % self.kind)
                    return
                if self._stop:
                    return
                self.finished.emit(out)
            except Exception as e:
                traceback.print_exc()
                self.failed.emit(str(e))

    return XrayAIWorker


XrayAIWorker = _build_worker_class()
