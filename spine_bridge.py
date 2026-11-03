# -*- coding: utf-8 -*-
"""CT3D 与「全脊柱拼接 / Cobb 角测量」（spine 子项目）之间的集成桥接层。

与 ``xray_bridge.py`` 同构的进程内集成，三个要点：

1. **进程内模块级集成，不拉起子进程**
   ``spine/spine_app.py`` 顶层只有 import / class / def，且入口被
   ``if __name__ == '__main__':`` 守着（无 ``QApplication`` 构造、无扫盘），
   所以可以直接 ``import spine_app`` 拿到算法类。CT3D 自带 pyenv 已装
   opencv 4.12（SIFT 可用）/ pydicom / numpy，无需额外环境。

2. **绝不复用 spine_app.MainWindow**
   它 ``__init__`` 里会做两件在 CT3D 里有害的事：
   ① ``apply_dark_theme()`` → ``self.setStyleSheet(dark_qss)``，把整块深色
      QSS 挂在这个窗上（虽不污染全局，但与 CT3D 观感割裂）；
   ② ``load_data_files()`` → **构造即自动扫 ``D:\\ClearTmp\\DicomTmp``**
      并起 ``DicomLoaderThread``，用户只想拖几张图却被强制全盘扫描。
   因此这里只取**算法与图形基类**，UI 由 ``spine_stitch_dialog.py`` 重建。

3. **算法口径原样复用，不重写**
   ``StitcherAlgo``（SIFT 位移 + 亮度匹配 + 羽化）、``AutoCobbAlgo``（中线拟合
   估 Cobb 端点）、``CobbAngleTool``（角度计算与分级）是这个项目的核心资产，
   自己重算数学必然与模型行为漂移（同 xray 侧 Cobb 的教训）。
"""

import os
import sys

import numpy as np

# ---------------------------------------------------------------------------
# spine 子项目路径引导
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
SPINE_ROOT = os.path.join(_HERE, "spine")
SPINE_APP_PY = os.path.join(SPINE_ROOT, "spine_app.py")

# 用 append 而不是 insert(0)：spine 目录下有 build/ dist/ data/ 等子目录，
# 插到最前有概率让同名 stdlib / 第三方模块被遮蔽。
if SPINE_ROOT not in sys.path:
    sys.path.append(SPINE_ROOT)


def spine_available() -> bool:
    """spine 子项目是否就位（不触发 import，纯路径判定）"""
    return os.path.isfile(SPINE_APP_PY)


def spine_module():
    """懒加载并返回 ``spine_app`` 模块；不可用时返回 ``None``。

    缓存到模块级单例：``import`` 本身很轻，但 ``spine_app`` 顶层会
    ``import cv2``（约 0.3s），重复导入无意义。
    """
    global _SPINE_MODULE
    if _SPINE_MODULE is not None:
        return _SPINE_MODULE
    if not spine_available():
        return None
    try:
        import spine_app  # noqa: PLC0415  (延迟导入是本层的既定语义)
    except Exception:                                          # noqa: BLE001
        _SPINE_MODULE = None
        return None
    _SPINE_MODULE = spine_app
    return _SPINE_MODULE


_SPINE_MODULE = None


# ---------------------------------------------------------------------------
# 算法 / 图形类出口
# ---------------------------------------------------------------------------

def _attr(name):
    """从 spine_app 取一个属性，取不到就返回 ``None``（不抛）"""
    mod = spine_module()
    if mod is None:
        return None
    return getattr(mod, name, None)


def get_dicom_helper():
    return _attr("DicomHelper")


def get_stitcher():
    return _attr("StitcherAlgo")


def get_auto_cobb():
    return _attr("AutoCobbAlgo")


def get_cobb_tool_class():
    return _attr("CobbAngleTool")


def get_smart_cobb_line():
    return _attr("SmartCobbLine")


def get_image_item_class():
    return _attr("ImageGraphicsItem")


def get_graphics_view_class():
    return _attr("StitchingGraphicsView")


# ---------------------------------------------------------------------------
# 图片装载：spine_app 只认 .dcm，这里补上常见图片 + 排序 + 灰度归一
# ---------------------------------------------------------------------------

# 常见图片扩展名（与 ct3d_utils.IMAGE_FILE_EXTS 保持一致）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".jpe", ".bmp", ".tif", ".tiff", ".webp")


def _norm_gray(arr: np.ndarray) -> np.ndarray:
    """任意位深/极性的灰度图 → uint8 0~255。

    ``spine_app.DicomHelper.load_dicom_as_image`` 是「除以全局 max」，对 DR
    那种大片过曝（背景接近饱和）会把骨性细节压到几个灰阶。这里改用
    **1%~99% 分位拉伸**，对 DR/CT/MR 都更稳，也和 CT3D 侧落盘反馈图的
    口径一致（同一条经验）。
    """
    if arr is None:
        return None
    a = np.asarray(arr)
    if a.ndim == 3:                       # 彩色 → 亮度
        a = a[..., :3].mean(axis=2)
    if a.dtype == np.uint8:
        return a
    a = a.astype(np.float32)
    lo, hi = np.percentile(a, (1.0, 99.0))
    if hi - lo < 1e-6:                    # 纯色图，退化用 min/max
        lo, hi = float(a.min()), float(a.max())
    if hi - lo < 1e-6:
        return np.zeros(a.shape, dtype=np.uint8)
    out = (a - lo) * (255.0 / (hi - lo))
    return np.clip(out, 0, 255).astype(np.uint8)


def _invert_if_needed(path: str, arr: np.ndarray) -> np.ndarray:
    """MONOCHROME1（负片约定）需反相，否则 X 线片显示成白底黑骨。

    与 CT3D 侧 ``series_needs_invert()`` 同一口径。判不出来就保持原样，
    绝不「猜着反相」。
    """
    try:
        import pydicom                                    # noqa: PLC0415
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        photo = str(getattr(ds, "PhotometricInterpretation", "") or "").upper()
        if photo == "MONOCHROME1":
            return (255 - arr).astype(np.uint8)
    except Exception:                                      # noqa: BLE001
        pass
    return arr


def load_image_gray(path: str):
    """读一张影像 → ``uint8`` 灰度 ndarray，失败返回 ``None``。

    DICOM 走 pydicom（含像素），常见图片走 cv2。**不做尺寸归一**：
    拼接算法自己按特征算位移，强行拉伸会破坏真实几何。
    """
    if not path:
        return None
    ext = os.path.splitext(str(path))[1].lower()

    # DICOM：优先按「无扩展名 + pydicom 可读」判定，避免设备 .dcm 大小写/
    # 无扩展名时漏判
    is_dicom = ext == ".dcm"
    if not is_dicom and ext not in IMAGE_EXTS:
        try:
            import pydicom                                # noqa: PLC0415
            pydicom.dcmread(path, stop_before_pixels=True)
            is_dicom = True
        except Exception:                                  # noqa: BLE001
            is_dicom = False

    if is_dicom:
        try:
            import pydicom                                # noqa: PLC0415
            ds = pydicom.dcmread(path)
            arr = ds.pixel_array
            if arr is None or arr.size == 0:
                return None
            gray = _norm_gray(arr)
            return _invert_if_needed(path, gray)
        except Exception:                                  # noqa: BLE001
            return None

    if ext in IMAGE_EXTS:
        try:
            import cv2                                     # noqa: PLC0415
            # 中文目录下 cv2.imread 会静默返回 None，必须走 imread_cn；
            # 万一 export_utils 导入不到，退回原生 imread，不能因此读不了图
            raw = None
            try:
                import export_utils                        # noqa: PLC0415
                raw = export_utils.imread_cn(
                    str(path), cv2.IMREAD_UNCHANGED)
            except Exception:                              # noqa: BLE001
                raw = None
            if raw is None:
                raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if raw is None:
                return None
            if raw.ndim == 3 and raw.shape[2] == 4:       # BGRA → 灰度
                raw = cv2.cvtColor(raw, cv2.COLOR_BGRA2GRAY)
            return _norm_gray(raw)
        except Exception:                                  # noqa: BLE001
            return None
    return None


def patient_label(path: str) -> str:
    """取患者名 / 片号，供拼接记录与默认文件名使用；取不到返回空串。"""
    helper = get_dicom_helper()
    if helper is not None:
        try:
            name = helper.get_patient_name(path)
            if name and name != "Unknown_Patient":
                return str(name)
        except Exception:                                  # noqa: BLE001
            pass
    return ""


def image_id_label(path: str) -> str:
    """取 AccessionNumber / PatientID；取不到返回空串。"""
    try:
        import pydicom                                    # noqa: PLC0415
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        for key in ("AccessionNumber", "PatientID"):
            val = getattr(ds, key, None)
            if val:
                return str(val)
    except Exception:                                      # noqa: BLE001
        pass
    return ""


def natural_sort_key(path: str):
    """自然排序键：``img2.png`` 排在 ``img10.png`` 前面。

    拼接是**顺序敏感**的（SIFT 逐对算相对位移），而设备导出的文件名常是
    ``1.dcm / 2.dcm / ... / 10.dcm``，纯字符串排序会得到 1,10,11,...,2 的
    错序，拼出来的人体是折的。这里把数字段按整数比较。
    """
    import re                                            # noqa: PLC0415
    name = os.path.basename(str(path))
    return tuple(
        int(tok) if tok.isdigit() else tok.lower()
        for tok in re.split(r"(\d+)", name)
    )


def sort_paths(paths):
    """按自然排序返回新列表（不改动入参）"""
    return sorted(list(paths), key=natural_sort_key)


# ---------------------------------------------------------------------------
# 患者列表序列 → 可拼接文件
# ---------------------------------------------------------------------------

def series_to_paths(series):
    """把主程序患者列表里的「序列」解析成拼接用的文件路径列表。

    返回 ``(paths, note)``：``paths`` 为自然排序后的文件列表，``note`` 是给
    用户看的原因说明（成功时为空串）。

    为什么不直接用 ``file_uids`` 全量转路径：患者列表里 CT/MR 序列动辄
    上千层（实测本机有 1560 层的序列），那些是**横断断层**，拿去拼接会得到
    一堆无意义的碎片。脊柱拼接只吃**X 线摄影片**（正位/侧位，一层一张），
    所以这里按模态分流：

    - X 线（CR/DX/DR/RF/XA/MG/IO/PX）→ 整个序列的层都可拼；
    - CT / MR / 模态缺失 → 返回空并说明原因，由 UI 提示用户换序列。

    ``file_uids`` 是**文件名去 .dcm**（不是 SeriesInstanceUID，见 main.py
    ``_load_series`` 的约定），拼回路径时要补回扩展名；设备也有直接以
    SeriesInstanceUID 命名的文件，故两种都试。
    """
    if not isinstance(series, dict):
        return [], "序列信息为空"

    folder = series.get("folder") or ""
    modality = str(series.get("modality") or "").strip().upper()

    # 先判模态再判目录：用户拖进来一个 CT 序列时，最该看到的反馈是
    # 「这是 CT、不是 X 线片」，而不是「目录不存在」这种答非所问的话。
    try:
        from ct3d_utils import XR_MODALITIES       # noqa: PLC0415
    except Exception:                              # noqa: BLE001
        XR_MODALITIES = {"CR", "DX", "DR", "RF", "XA", "MG", "IO", "PX"}

    if modality and modality not in XR_MODALITIES:
        return [], ("该序列是 %s，不是 X 线摄影片。\n"
                    "脊柱拼接只接受 X 线正/侧位片（CR/DX/DR 等）。" % modality)

    if not folder or not os.path.isdir(folder):
        return [], "序列目录不存在：%s" % (folder or "(空)")

    uids = list(series.get("file_uids") or [])
    if uids:
        paths = []
        for uid in uids:
            for cand in (os.path.join(folder, "%s.dcm" % uid),
                         os.path.join(folder, str(uid))):
                if os.path.isfile(cand):
                    paths.append(cand)
                    break
        if paths:
            return sort_paths(paths), ""

    # file_uids 为空/全不匹配时，退回「扫目录取可读影像」
    try:
        from PyQt5 import QtWidgets                # noqa: PLC0415,F401
    except Exception:                              # noqa: BLE001
        pass
    found = []
    for fn in os.listdir(folder):
        full = os.path.join(folder, fn)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext == ".dcm" or ext in IMAGE_EXTS or ext == "":
            found.append(full)
    if not found:
        return [], "该序列目录里没有可读的影像文件"
    return sort_paths(found), ""

