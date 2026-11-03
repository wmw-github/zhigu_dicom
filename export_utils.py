# -*- coding: utf-8 -*-
"""患者/序列的导出工具：DICOM 原文件导出 与 图像（PNG/JPG）导出。

设计要点（都是踩过的坑，勿改）：

1. **DICOM 导出是「原文件拷贝」，不是重新合成**。
   重新合成会丢 Private Tag、丢掉设备私有信息、可能改错 Transfer Syntax，
   导出的片子回到 PACS 里可能读不出来。原样 copy2 最稳，也最快。

2. **`cv2.imwrite` 写不了 Windows 非 ASCII 路径**。
   患者姓名是中文，导出目录必然含中文 → 必须走
   `cv2.imencode(ext, arr)[1].tofile(path)`。

3. **导出目录要三级兜底**：注册表 Shell Folders → QStandardPaths
   → `~/Desktop` / `~/桌面`。Win7 上 QStandardPaths 偶尔给不出路径。

4. **文件名必须清洗**：患者名里可能有 `/ \ : * ? " < > |`，
   直接拼路径会创建失败或写到别处（`C:` 会被当盘符）。

5. **同名冲突要能自愈**：同一患者多次导出、同名患者不同目录，
   统一加 `_1`、`_2` 序号后缀，绝不静默覆盖。

6. **脱敏导出是「另存一份改写过的副本」，绝不原地改源文件**（v1.15.1）。
   路径 1：DICOM → pydicom 读进来改写身份标签 + 重映射实例 UID（同一序列
   内保持一致），再另存；路径 2：图片 → 像素里可能烧着姓名（CR/DR 常见），
   可选把四角文字区涂黑，且导出目录不带患者姓名。
"""

import os
import re
import shutil

# Windows 保留设备名（CON/PRN/AUX/NUL/COM1..9/LPT1..9）
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *["COM%d" % i for i in range(1, 10)],
    *["LPT%d" % i for i in range(1, 10)],
}

_ILLEGAL_CHARS = re.compile(r'[\\/:\*\?"<>\|\r\n\t]')


def sanitize_filename(name, default="未命名", max_len=60):
    """把任意文本清洗成安全的单级文件名（不含路径分隔符）。

    返回值可直接用于 `os.path.join`，保证结果一定落在这个目录里。
    """
    if name is None:
        name = ""
    name = str(name).strip()
    name = _ILLEGAL_CHARS.sub("_", name)
    # 去掉首尾的点与空格（Windows 不允许以 . 结尾）
    name = name.strip(". ")
    if not name:
        return default
    # 保留扩展名的前提下截断
    if len(name) > max_len:
        base, ext = os.path.splitext(name)
        if len(ext) > 10:      # 不是扩展名（点在中后部）
            base, ext = name, ""
        base = base[:max(1, max_len - len(ext))]
        name = base + ext
    # 去掉尾部的点（Windows 资源管理器打不开）
    while name.endswith("."):
        name = name[:-1]
    if not name:
        return default
    if name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    return name


def unique_dir(parent, name):
    """在 parent 下取一个不存在的目录名（同名自动加 `_1`/`_2`）。"""
    base = sanitize_filename(name)
    path = os.path.join(parent, base)
    if not os.path.exists(path):
        return path
    for i in range(1, 1000):
        path = os.path.join(parent, "%s_%d" % (base, i))
        if not os.path.exists(path):
            return path
    raise RuntimeError("同名目录过多，无法生成新目录：%s" % name)


def unique_file(parent, filename):
    """在 parent 下取一个不存在的文件全路径（同名自动加 `_1`/`_2`）。"""
    base = sanitize_filename(filename, default="image", max_len=80)
    stem, ext = os.path.splitext(base)
    path = os.path.join(parent, base)
    if not os.path.exists(path):
        return path
    for i in range(1, 10000):
        path = os.path.join(parent, "%s_%d%s" % (stem, i, ext))
        if not os.path.exists(path):
            return path
    raise RuntimeError("同名文件过多，无法生成新文件名：%s" % filename)


def desktop_dir():
    """取桌面目录（带兜底）。

    顺序：注册表 Shell Folders → QStandardPaths → `~/Desktop` / `~/桌面`。
    每一级都要求**目录真实存在**，否则下一级。
    """
    # 1) 注册表（Win 最准，能正确处理 OneDrive 重定向的桌面）
    if os.name == "nt":
        try:
            import winreg
            key = (r"Software\Microsoft\Windows\CurrentVersion"
                   r"\Explorer\User Shell Folders")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                val, _ = winreg.QueryValueEx(k, "Desktop")
            if val:
                path = os.path.expandvars(val)
                if os.path.isdir(path):
                    return path
        except Exception:
            pass

    # 2) Qt 自己的解析
    try:
        from PyQt5 import QtCore
        path = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.DesktopLocation)
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass

    # 3) 约定俗成的目录名（中文系统是「桌面」）
    home = os.path.expanduser("~")
    for sub in ("Desktop", "桌面", "桌面中文"):
        path = os.path.join(home, sub)
        if os.path.isdir(path):
            return path
    # 桌面目录不存在就退到用户主目录，总比报错强
    return home


def series_files(series):
    """列出序列内的源文件绝对路径（按 `file_uids` 原始顺序，去掉不存在的）。

    `file_uids` 是**文件名去 .dcm**，不是 SeriesInstanceUID —— 拼路径时必须
    用 series["folder"] 拼，不能用 pat_id（图片目录路径那条轨道）。
    """
    folder = (series or {}).get("folder") or ""
    if not folder or not os.path.isdir(folder):
        return []
    uids = series.get("file_uids") or []
    out = []
    for uid in uids:
        name = str(uid)
        path = os.path.join(folder, name)
        if not os.path.exists(path) and not name.lower().endswith(".dcm"):
            path = path + ".dcm"
        if os.path.isfile(path):
            out.append(path)
    if out:
        return out
    # file_uids 缺失/全失效：退回目录内直接列文件（保底，别交白卷）
    try:
        names = [n for n in os.listdir(folder)
                 if os.path.isfile(os.path.join(folder, n))]
    except Exception:
        return []
    return [os.path.join(folder, n) for n in sorted(names)]


def looks_like_dicom(path) -> bool:
    """DICOM 3.0 魔数检测：文件偏移 128 字节处为 'DICM'。

    这与文件命名习惯无关 —— CT/MR 序列的 file_uids 约定是**文件名去
    .dcm**（_load_series 的接口约定），所以「uid 带不带 .dcm 后缀」
    完全不能用来判断是不是 DICOM（v1.10.1 修的真实故障：真实 CT 序列
    被误判「该序列不是 DICOM 文件」，一个都导不出来）。

    少数超老设备导出的 DICOM 没有 128 字节前言（无 DICM 魔数）——
    此时退回 pydicom 强读，含序列级关键字段才算 DICOM。
    """
    try:
        with open(path, "rb") as f:
            f.seek(128)
            if f.read(4) == b"DICM":
                return True
    except OSError:
        return False
    # 无前言的野生 DICOM：pydicom force 读，有关键 Equipment/Series 字段才算
    try:
        import pydicom
        ds = pydicom.dcmread(path, force=True, stop_before_pixels=True)
    except Exception:
        return False
    return any(ds.get(tag) is not None
               for tag in ("Modality", "SOPClassUID", "SeriesInstanceUID"))


def is_dicom_series(series) -> bool:
    """该序列是否为 DICOM（决定菜单里给不给「导出 DICOM」）。

    用第一个文件的 DICM 魔数判断（looks_like_dicom），而不是文件名
    后缀 —— 理由见 looks_like_dicom 的 docstring。
    """
    files = series_files(series)
    if not files:
        return False
    return looks_like_dicom(files[0])


def export_series_dicom(series, dest_dir):
    """把一个序列的 DICOM 原文件拷到 dest_dir。

    返回写出的文件数。**原文件拷贝**，不改内容、不重写 Tag。
    """
    files = series_files(series)
    if not files:
        return 0
    os.makedirs(dest_dir, exist_ok=True)
    written = 0
    for src in files:
        name = os.path.basename(src)
        dst = unique_file(dest_dir, name)
        shutil.copy2(src, dst)
        written += 1
    return written


# ---------------------------------------------------------------------------
# 脱敏导出（v1.15.1）
#
# 用户诉求：导出 DICOM / 图片时能选「是否脱敏」，给外院或科研用时把身份
# 信息去掉。两条路径完全不同：
#   * DICOM —— 身份信息在**标签**里，改写标签即可（像素不动）。
#   * 图片  —— 身份信息可能**烧在像素上**（CR/DR 最常见），只能涂黑；
#              文件名/目录名也不能带姓名。
# 无论哪条路径都**不碰源文件**：读进来另存，源目录保持不变。
# ---------------------------------------------------------------------------

# 需要清空的身份相关标签（按 PS3.15 基本应用级保密性配置的常用子集）。
# 只清「直接指向人」的字段；性别/年龄/检查日期保留 —— 它们有临床价值，
# 且单独不构成可识别信息（本软件的质控/骨龄都依赖这些）。
_PHI_KEYWORDS = (
    "PatientName", "PatientID", "IssuerOfPatientID",
    "OtherPatientIDs", "OtherPatientIDsSequence", "OtherPatientNames",
    "PatientBirthName", "PatientMotherBirthName", "PatientBirthDate",
    "PatientBirthTime", "PatientAddress", "PatientTelephoneNumbers",
    "PatientComments", "PatientInsurancePlanCodeSequence",
    "PatientReligiousPreference", "EthnicGroup",
    "ReferencedPatientSequence", "ClinicalTrialSubjectID",
    "ClinicalTrialSubjectReadingID", "ClinicalTrialTimePointID",
    "AccessionNumber", "StudyID", "AdmittingDiagnosesDescription",
    "InstitutionName", "InstitutionAddress", "InstitutionalDepartmentName",
    "StationName", "ReferringPhysicianName", "ReferringPhysicianAddress",
    "ReferringPhysicianTelephoneNumbers", "PerformingPhysicianName",
    "OperatorsName", "PhysiciansOfRecord", "NameOfPhysiciansReadingStudy",
    "RequestingPhysician", "ScheduledPerformingPhysicianName",
    "RequestedProcedureID", "RequestedProcedureComments",
    "DeviceSerialNumber", "PlateID", "FillerOrderNumberImagingServiceRequest",
    "PlacerOrderNumberImagingServiceRequest", "ImagingServiceRequestComments",
)

# 这些 UID 是**标准定义的类标识**，重新生成会让文件彻底不可读，必须原样保留
_UID_KEYWORDS_KEEP = ("SOPClassUID", "TransferSyntaxUID", "CodingSchemeUID",
                      "ContextGroupExtensionCreatorUID")


def _is_uid_text(value):
    """粗判一个字符串是否像 UID（`1.2.840.10008.x` 这种点分数字）。"""
    value = str(value or "").strip()
    if not value or len(value) > 128:
        return False
    parts = value.split(".")
    if len(parts) < 2:
        return False
    for part in parts:
        if not part.isdigit():
            return False
    return True


def anonymize_dataset(ds, uid_map, anon_name="ANON", anon_id="ANON"):
    """就地把一个 pydicom Dataset 改写为脱敏版本。

    - uid_map: dict，**同一患者/同一次导出必须共用同一个 dict**，
      这样同一序列所有层的新 Study/Series UID 才一致，导入 PACS 后仍能
      被识别成一个序列（各层 UID 不同 = 变成几百个孤立实例）。
    - 返回被改动的标签个数（0 表示这个文件本来就没有身份信息）。
    """
    changed = 0

    # 1) 身份标签清空
    for kw in _PHI_KEYWORDS:
        if kw in ds:
            try:
                elem = ds[kw]
                if elem.VR == "SQ":
                    # 序列元素：清成空序列（逐个删元素会留下空壳）
                    ds[kw].value = []
                elif elem.VR in ("PN", "LO", "SH", "ST", "LT", "UT",
                                 "DA", "TM", "DT", "AS", "CS", "IS"):
                    ds[kw].value = ""
                else:
                    del ds[kw]
                    continue
                changed += 1
            except Exception:      # noqa: BLE001
                continue

    # 2) 患者姓名 / ID 换成假名（不能留空：个别 PACS 遇到空 PatientID 会拒收）
    try:
        ds.PatientName = anon_name
        ds.PatientID = anon_id
        changed += 2
    except Exception:              # noqa: BLE001
        pass

    # 3) 私有标签一律删除：设备私有块里经常塞姓名、技师、医院名，
    #    而且我们并不知道各家厂商的私有定义。
    try:
        ds.remove_private_tags()
        changed += 1
    except Exception:              # noqa: BLE001
        pass

    # 4) UID 重映射。原 UID 里虽然不含姓名，但可以拿回原 PACS 反查身份，
    #    所以真正脱敏必须换掉；同一个旧 UID 在同一次导出里映射成同一个新 UID。
    try:
        import pydicom.uid as _puid
        for elem in list(ds.iterall()):
            if elem.VR != "UI":
                continue
            kw = elem.keyword or ""
            if kw in _UID_KEYWORDS_KEEP or "ClassUID" in kw:
                continue
            val = elem.value
            if isinstance(val, (list, tuple)):
                new_vals = []
                for one in val:
                    if not _is_uid_text(one):
                        new_vals.append(one)
                        continue
                    key = str(one)
                    if key not in uid_map:
                        uid_map[key] = _puid.generate_uid()
                    new_vals.append(uid_map[key])
                    changed += 1
                elem.value = new_vals
            else:
                if not _is_uid_text(val):
                    continue
                key = str(val)
                if key not in uid_map:
                    uid_map[key] = _puid.generate_uid()
                elem.value = uid_map[key]
                changed += 1
    except Exception:              # noqa: BLE001
        pass

    # 5) 文件元信息里的 MediaStorageSOPInstanceUID 必须跟 dataset 里的一致，
    #    否则 pydicom / 多数 PACS 会判定「文件损坏」。
    try:
        if getattr(ds, "file_meta", None) is not None:
            sop = str(getattr(ds, "SOPInstanceUID", "") or "")
            if sop and "MediaStorageSOPInstanceUID" in ds.file_meta:
                ds.file_meta.MediaStorageSOPInstanceUID = sop
    except Exception:              # noqa: BLE001
        pass

    return changed


def export_series_dicom_anonymized(series, dest_dir, anon_name="ANON",
                                   anon_id="ANON", uid_map=None,
                                   on_progress=None, rename=True,
                                   name_prefix="IM"):
    """把序列导出为**脱敏后**的 DICOM 副本，返回写出的文件数。

    与 `export_series_dicom` 的区别：这里逐个读进来改写标签再另存，
    所以慢得多（每层都要解析 + 重写），但完全不改源文件。

    rename=True 时把文件统一改名成 ``IM_0001.dcm`` 这种流水名 ——
    实测不少 PACS/设备导出时用「姓名_序号.dcm」命名，只改标签不改文件名
    等于白脱敏。
    """
    try:
        import pydicom
    except Exception as exc:       # noqa: BLE001
        raise RuntimeError("缺少 pydicom，无法脱敏导出：%s" % exc)

    files = series_files(series)
    if not files:
        return 0
    os.makedirs(dest_dir, exist_ok=True)
    if uid_map is None:
        uid_map = {}

    total = len(files)
    written = 0
    fail = 0
    for idx, src in enumerate(files, 1):
        if rename:
            base = "%s_%04d.dcm" % (name_prefix, idx)
        else:
            base = os.path.basename(src)
        dst = unique_file(dest_dir, base)
        try:
            ds = pydicom.dcmread(src, force=True)
        except Exception:          # noqa: BLE001
            fail += 1
            continue
        try:
            anonymize_dataset(ds, uid_map, anon_name, anon_id)
        except Exception:          # noqa: BLE001
            fail += 1
            continue
        try:
            # write_like_original 保持原编码（含封装压缩的像素数据），
            # 只把我们改过的标签写回去。
            ds.save_as(dst, write_like_original=True)
            written += 1
        except Exception:          # noqa: BLE001
            fail += 1
            try:
                if os.path.exists(dst):
                    os.remove(dst)
            except Exception:      # noqa: BLE001
                pass
            continue
        if on_progress is not None:
            try:
                on_progress(idx, total)
            except Exception:      # noqa: BLE001
                pass

    # 一层都没写成功时，把失败原因讲明白（而不是留一个空目录）
    if written == 0 and fail:
        raise RuntimeError("脱敏导出失败：%d 个文件均无法解析或写入" % fail)
    return written


def mask_burned_in_corners(img, w_ratio=0.34, h_ratio=0.10):
    """把图像四角涂黑，遮盖设备烧录进像素的姓名/ID/医院名。

    只处理**四角**（上下各两个矩形），不动中间区域，避免遮到解剖结构。
    CR/DR 的烧录文字几乎都在四角；CT/MR 一般没有烧录文字，
    所以这个开关默认关闭，由用户显式勾选。

    **返回新数组，不修改入参**：早期版本就地改，结果调用方（或同一帧被
    另一个导出路径复用）会莫名其妙丢四角，排查起来极难。
    """
    import numpy as np

    if img is None:
        return img
    arr = np.array(img, copy=True)
    if arr.ndim < 2:
        return img
    h, w = arr.shape[0], arr.shape[1]
    if h < 12 or w < 12:
        return img
    bh = max(1, int(h * float(h_ratio)))
    bw = max(1, int(w * float(w_ratio)))
    arr[0:bh, 0:bw] = 0           # 左上
    arr[0:bh, w - bw:w] = 0       # 右上
    arr[h - bh:h, 0:bw] = 0       # 左下
    arr[h - bh:h, w - bw:w] = 0   # 右下
    return arr


# ---------------------------------------------------------------------------
# DICOM 帧 → 显示域灰度（v1.18.1）
#
# 导出必须复刻显示端的影像学处理，否则「看到的是脑窗、导出的是骨窗」，
# 「看到的是正片、导出的是负片」。这里集中处理三件事，缺一个就会错：
#   1. Modality LUT（RescaleSlope / RescaleIntercept）—— pydicom 不做；
#   2. 窗宽窗位取值（DICOM WC/WW，需与 1 之后的域一致）；
#   3. PhotometricInterpretation 极性（MONOCHROME1 是负片）。
# ---------------------------------------------------------------------------

# 这些模态的 WC/WW 单位与 CT 的 HU 不可比（DR 实测 WC=4279/WW=1108），
# 校验逻辑必须按「值域"来判断而不是按绝对数值区间来判断。
_XR_MODALITIES = ("DX", "CR", "DR", "RF", "XA", "MG", "IO", "PX")


def _first_number(value):
    """DICOM 多值字段取第一个数；解析失败返回 None。

    WC/WW 允许写成多值（每台设备一套窗），pydicom 给的是 DSfloat 或列表，
    直接 float() 列表会抛 TypeError。
    """
    if value is None:
        return None
    try:
        if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
            if len(value) == 0:
                return None
            value = value[0]
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _is_monochrome1(ds) -> bool:
    """该帧是否为 MONOCHROME1（负片：像素值越大越暗）。"""
    return (str(getattr(ds, "PhotometricInterpretation", "") or "")
            .strip().upper() == "MONOCHROME1")


def modality_lut_array(ds, img=None):
    """应用 Modality LUT，返回 float32 数组（非就地修改）。

    **为什么必须做**：``Dataset.pixel_array`` 返回的是磁盘上的**原始存储
    值**，而 DICOM 的 WindowCenter/WindowWidth 属于「Modality LUT 之后」
    的域（PS3.3 C.11.2）。实测头颅 CT：``RescaleSlope=1``、
    ``RescaleIntercept=-1024``、原始值 11~328，对应 HU -1013~-696；把
    HU 窗值(WW80/WL40) 套到原始值上，窗区间整体偏移 1024，画面必然错。

    显示端走 ``vtkDICOMImageReader``，它**在读取阶段已应用 Modality LUT**
    （见 Dicom2DReader._ct_value_at 的说明，那里靠这个避免「测骨头出负数」），
    所以二维/三维里都是 HU。导出不带这一步，就和显示端不在同一个域。
    """
    import numpy as np

    arr = ds.pixel_array if img is None else img
    if arr is None:
        return None
    # 彩色帧不做 Modality LUT（US 的 RGB、病理/内镜截图）
    if arr.ndim == 3 and arr.shape[2] > 1:
        return np.asarray(arr, dtype=np.float32)

    slope = _first_number(getattr(ds, "RescaleSlope", None))
    intercept = _first_number(getattr(ds, "RescaleIntercept", None))
    if slope is None and intercept is None:
        return np.asarray(arr, dtype=np.float32)
    if slope is None or slope == 0.0:      # 斜率为 0 会把整幅压成常数
        slope = 1.0
    if intercept is None:
        intercept = 0.0
    # 必须先转 float32：uint16 上做 -1024 会回绕成 64512
    return (np.asarray(arr, dtype=np.float32) * float(slope)
            + float(intercept))


def dicom_window_level(ds, arr):
    """取该帧 DICOM 自带的 (窗宽, 窗位)，不可用时返回 (None, None)。

    传入的 ``arr`` 必须是 **Modality LUT 之后**的数组：WC/WW 与它同域，
    所以这里按 arr 的实际值域做合理性校验，而不是套 CT 的固定 HU 区间。

    校验是必要的：设备把「LUT 之前」的值写进 WC/WW、或写入与本次扫描
    无关的预置窗值都不罕见，直接套上去会整幅发黑/发白 —— 这种脏值放弃，
    由调用方退回分位自适应。
    """
    import numpy as np

    wc = _first_number(getattr(ds, "WindowCenter", None))
    ww = _first_number(getattr(ds, "WindowWidth", None))
    if wc is None or ww is None or ww < 1.0:
        return None, None
    if arr is None or np.asarray(arr).size == 0:
        return None, None

    finite = np.asarray(arr, dtype=np.float32)
    lo = float(finite.min())
    hi = float(finite.max())
    span = max(hi - lo, 1.0)
    # 窗位要落在数据实际范围附近（各留 50% 余量）
    if not (lo - 0.5 * span <= wc <= hi + 0.5 * span):
        return None, None
    # 窗宽荒谬地宽（如 16bit 满量程写到 HU 数据上）同样视为脏值
    if ww > 50.0 * span:
        return None, None
    return float(ww), float(wc)


def frame_display_array(ds):
    """DICOM 数据集 → 可直接做窗映射的 float32 灰度数组。

    返回的数组已应用 Modality LUT；**极性不在这里翻**（见 write_image 的
    invert 参数），因为 DICOM 规定 WC/WW 作用在原始极性上、反相发生在
    灰度映射之后。
    """
    return modality_lut_array(ds)


def _to_uint8(arr, window=None, level=None):
    """把一帧数值数组按窗宽窗位映射成 uint8 灰度图。

    window/level 为 None 时用 1%~99% 分位自适应（与软件里投影/缩略图
    的口径一致，避免暗图全黑、亮图全白）。
    """
    import numpy as np

    arr = np.asarray(arr)
    if arr.ndim == 3:
        arr = arr[..., 0] if arr.shape[2] <= 4 else arr[0]
    arr = arr.astype(np.float32, copy=False)

    if window is None or level is None:
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.zeros(arr.shape, dtype=np.uint8)
        lo = float(np.percentile(finite, 1))
        hi = float(np.percentile(finite, 99))
        if hi <= lo:
            lo = float(finite.min())
            hi = float(finite.max())
        if hi <= lo:
            hi = lo + 1.0
        level = (lo + hi) / 2.0
        window = max(hi - lo, 1.0)

    half = max(float(window), 1e-6) / 2.0
    out = (arr.astype(np.float32) - (float(level) - half)) / (2.0 * half)
    out = np.clip(out * 255.0, 0, 255)
    return out.astype(np.uint8)


def imread_cn(path, flags=None):
    """读一张图，支持中文 / 非 ASCII 路径（失败返回 None）。

    `cv2.imread` 在 Windows 上遇到非 ASCII 路径会**静默返回 None**（连警告
    都不打），调用方往往只看到「图片读不出来」而想不到是路径编码问题。
    xray 子模块早就有 `utils.image_io.imread_cn` 处理这个，但主流程的
    两处 `cv2.imread` 一直是裸调（脊柱拼接在中文目录下会整批失败）。
    这里统一管理：ASCII 路径走原生 imread（最快），非 ASCII 先
    `np.fromfile` 读字节再 `imdecode`，完全绕开 cv2 的路径处理。
    """
    try:
        import cv2
    except Exception:                 # noqa: BLE001
        return None
    f = cv2.IMREAD_UNCHANGED if flags is None else flags
    try:
        path.encode("ascii")
        return cv2.imread(path, f)    # 纯 ASCII：原生路径，最快
    except Exception:                 # noqa: BLE001
        pass
    try:
        import numpy as np
        buf = np.fromfile(path, dtype=np.uint8)
        if buf.size:
            return cv2.imdecode(buf, f)
    except Exception:                 # noqa: BLE001
        pass
    return None


def write_image(path, arr, window=None, level=None, mask_corners=False,
                invert=False):
    """写一张灰度图到 path（自动支持中文路径）。

    cv2.imwrite 在 Windows 上遇到非 ASCII 路径会抛异常/静默失败，
    必须走 imencode().tofile()。

    invert=True 时把灰度翻转（MONOCHROME1 负片）。顺序是「先按窗宽窗位
    映射成 0~255，再翻转」—— 这正是 DICOM 的语义：WC/WW 作用在原始
    极性上，光度解释（越大越暗）发生在灰度映射之后。

    mask_corners=True 时先把四角涂黑（脱敏：遮盖烧录文字）。必须在
    翻转之后涂 —— 否则负片导出的四角会变成白块，反而更显眼。
    """
    import numpy as np
    try:
        import cv2
    except Exception:
        cv2 = None

    img = _to_uint8(arr, window, level)
    if invert:
        img = np.subtract(255, img, dtype=np.uint8)
    if mask_corners:
        img = mask_burned_in_corners(img)
    ext = os.path.splitext(path)[1] or ".png"

    if cv2 is not None:
        ok, buf = cv2.imencode(ext, img)
        if ok:
            buf.tofile(path)      # 关键：支持中文路径
            return True
        return False

    # 无 cv2 时退到 PIL（发行环境一定有 cv2，这里只是别让函数直接死掉）
    try:
        from PIL import Image
    except Exception:
        raise RuntimeError("既没有 cv2 也没有 PIL，无法导出图片")
    mode = "L" if img.ndim == 2 else "RGB"
    Image.fromarray(img, mode=mode).save(path)
    return True


# 张数达到这个量级才值得开线程池（线程本身有固定开销）
_EXPORT_PARALLEL_MIN = 32
# 并发上限：导出期间往往还有三维重建 / AI 推理在跑，别把 CPU 抢光
_EXPORT_PARALLEL_WORKERS = 4


def export_series_images(series, dest_dir, fmt="png", window=None,
                         level=None, on_progress=None, mask_corners=False,
                         invert=None, use_dicom_window=True):
    """把一个序列逐帧导出为图片到 dest_dir，返回写出的文件数。

    - fmt: "png" 或 "jpg"
    - on_progress(done, total)：可选进度回调（导出上千张时让 UI 有反应）
    - mask_corners: 脱敏用，把四角烧录文字涂黑
    - window/level: 调用方（界面）指定的窗值，**必须与 Modality LUT 之后的
      域一致**（CT 即 HU）。两者都给时逐帧照用，不再看 DICOM 窗值。
    - invert: None=按每帧的 PhotometricInterpretation 自动判断（DR 的
      MONOCHROME1 负片会翻正）；True/False=强制。
    - use_dicom_window: 未指定 window/level 时优先用 DICOM 自带 WC/WW
      （设备的诊断窗，比按像素分位猜更可靠），脏值自动退回分位。

    早期版本（v1.18.0 及以前）这里是「裸 pixel_array + 1%~99% 分位拉伸」，
    导致两个真实故障：DR 导出成负片（没处理 MONOCHROME1）、头颅 CT 选脑窗
    却导出成骨窗（分位区间跨空气到颅骨，等价宽窗）。
    """
    files = series_files(series)
    if not files:
        return 0
    os.makedirs(dest_dir, exist_ok=True)
    ext = "." + (fmt or "png").lower().lstrip(".")

    try:
        import pydicom
        import numpy as np
    except Exception as exc:
        raise RuntimeError("缺少 pydicom/numpy，无法导出图片：%s" % exc)

    total = len(files)
    fixed_window = (window is not None and level is not None)

    def _one(job):
        """导出单帧，返回 True/False。在工作线程里跑，绝不触碰 UI。"""
        idx, path = job
        try:
            ds = pydicom.dcmread(path, force=True)
            arr = frame_display_array(ds)
            if arr is None:
                return False
            # 极性逐帧判断：同一序列里混入负片（重拍 / 不同设备拼接）
            # 并不罕见，按序列级判断会漏掉个别帧。
            do_invert = _is_monochrome1(ds) if invert is None else bool(invert)
            w, l = window, level
            if not fixed_window and use_dicom_window:
                w, l = dicom_window_level(ds, arr)   # 脏值返回 (None, None)
            name = "img_%04d%s" % (idx, ext)
            return bool(write_image(unique_file(dest_dir, name),
                                    np.asarray(arr), w, l,
                                    mask_corners=mask_corners,
                                    invert=do_invert))
        except Exception:
            return False        # 单张失败跳过，不让整批导出失败

    # --- 大序列并行 ---
    # 每帧的开销是「解码像素 + 灰度映射 + 编码 PNG」，其中编码/解码都是
    # 释放 GIL 的 C 调用，多核下能真正并行。实测 909 张 CT：串行 10.9s
    # （12 ms/张），4 线程显著下降。
    # 进度回调**必须留在调用线程**：UI 的进度对话框不能从工作线程更新。
    # ThreadPoolExecutor.map 按输入顺序产出结果，于是顺序与进度都保持正确。
    jobs = list(enumerate(files, 1))
    if total >= _EXPORT_PARALLEL_MIN:
        try:
            from concurrent.futures import ThreadPoolExecutor
        except Exception:                      # noqa: BLE001
            ThreadPoolExecutor = None
        if ThreadPoolExecutor is not None:
            workers = min(_EXPORT_PARALLEL_WORKERS, max(1, total))
            written = 0
            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for idx, ok in enumerate(pool.map(_one, jobs), 1):
                        if ok:
                            written += 1
                        if on_progress is not None:
                            try:
                                on_progress(idx, total)
                            except Exception:  # noqa: BLE001
                                pass
                return written
            except Exception:                  # noqa: BLE001
                # 并行路径出任何岔子都退回串行，保证「能导出」优先于「快」
                written = 0

    # --- 串行路径（小序列，或并行不可用/失败时的兜底）---
    written = 0
    for idx, path in jobs:
        if _one((idx, path)):
            written += 1
        if on_progress is not None:
            try:
                on_progress(idx, total)
            except Exception:                  # noqa: BLE001
                pass
    return written