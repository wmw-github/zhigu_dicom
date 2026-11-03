import os
import xml.etree.ElementTree as ET

import numpy as np
import pydicom
import re
import json
from collections import Counter
from typing import List, Union

# ---------------------------------------------------------------------------
# 模态分类：CT、X 线（DR/DX/CR）、MR 在同一套患者列表里共存，靠这些集合区分
# ---------------------------------------------------------------------------

# DICOM Modality 标签里属于「X 线摄影」的取值
XR_MODALITIES = {"CR", "DX", "DR", "RF", "XA", "MG", "IO", "PX"}

# DICOM Modality 标签里属于「磁共振」的取值
MR_MODALITIES = {"MR", "MRI"}

# X 线影像的 DICOM Storage Class UID（XFD 里写作 classUid）。
# CT3D 原本只认 CT 的两个 UID，因此 X 线序列会被整组丢掉。
CT_CLASS_UIDS = {
    "1.2.840.10008.5.1.4.1.1.2",      # CT Image Storage
    "1.2.840.10008.5.1.4.1.1.2.1",    # Enhanced CT Image Storage
}
XR_CLASS_UIDS = {
    "1.2.840.10008.5.1.4.1.1.1",      # CR Image Storage
    "1.2.840.10008.5.1.4.1.1.1.1",    # DX Image Storage (For Presentation)
    "1.2.840.10008.5.1.4.1.1.1.1.1",  # DX Image Storage (For Processing)
    "1.2.840.10008.5.1.4.1.1.1.2",    # DX Mammography (For Presentation)
    "1.2.840.10008.5.1.4.1.1.1.2.1",  # DX Mammography (For Processing)
    "1.2.840.10008.5.1.4.1.1.1.3",    # DX Intra-oral (For Presentation)
    "1.2.840.10008.5.1.4.1.1.12.1",   # XA Image Storage
    "1.2.840.10008.5.1.4.1.1.12.2",   # RF Image Storage
}
MR_CLASS_UIDS = {
    "1.2.840.10008.5.1.4.1.1.4",      # MR Image Storage
    "1.2.840.10008.5.1.4.1.1.4.1",    # Enhanced MR Image Storage
}


def is_xr_modality(modality) -> bool:
    """判断 Modality 是否属于 X 线摄影（CR/DX/DR...）"""
    return str(modality or "").strip().upper() in XR_MODALITIES


def is_mr_modality(modality) -> bool:
    """判断 Modality 是否属于磁共振（MR）"""
    return str(modality or "").strip().upper() in MR_MODALITIES


def is_supported_modality(modality) -> bool:
    """CT / X 线 / MR —— 本程序支持的三大模态"""
    m = str(modality or "").strip().upper()
    return m == "CT" or is_xr_modality(m) or is_mr_modality(m)


def _fix_gbk_text(value) -> str:
    """修复国产设备用 GBK 写中文、被 pydicom 按 Latin-1 解出的乱码。

    典型现象：'Ñü×µÕýÎ»'（腰椎正位）本是 GBK 字节被当 Latin-1 读的结果。
    仅在确实能解出中文时才替换，避免误伤正常的 ASCII 文本。
    """
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    try:
        fixed = s.encode("latin-1").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            fixed = s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
    # 只有解出实际中文字符才认为修复成功
    if any("\u4e00" <= c <= "\u9fff" for c in fixed):
        return fixed
    return s


def is_dicom_dir(path: str) -> bool:
    """目录直属是否有 .dcm 文件（不做递归）"""
    if not path or not os.path.isdir(path):
        return False
    for name in os.listdir(path):
        full_path = os.path.join(path, name)
        if os.path.isfile(full_path) and name.lower().endswith(".dcm"):
            return True
    return False


def count_dicom_files(path: str) -> int:
    """统计目录**直属**的 .dcm 文件数"""
    if not path or not os.path.isdir(path):
        return 0
    n = 0
    try:
        for name in os.listdir(path):
            if name.lower().endswith(".dcm") and \
                    os.path.isfile(os.path.join(path, name)):
                n += 1
    except OSError:
        return 0
    return n


def is_single_series_dir(path: str) -> bool:
    """判断目录能否作为「**一个**序列」直接打开

    用于「加载患者目录」按钮的分支决策。初版只看「直属 dcm 数量 ≥2」，
    结果踩了个坑：设备有些目录把**多个序列混放**在一起，例如

    - ``01/``：13 张，实测含 4 个序列（Cervical-spine 2 + Thoracic-spine 2
      + Lumbar-spine 3 + Thoracic-spine 6）
    - ``03/``：8 张，含 4 个序列（前后位 1+3、侧位 2+2）

    这种目录若被当成单序列去 `_load_series()`，``vtkDICOMImageReader``
    只会挑其中一个序列读（实测 ``01/`` 读出 Z 维仅 2，其余序列被丢弃），
    于是抛出「DICOM数据维度异常」。

    正确判据（按优先级）：
    1. 目录内**实际只有一个 SeriesInstanceUID** → 是单序列目录；
    2. 无直属 dcm → 不是（是患者根目录或空目录）；
    3. 无法索引（文件损坏等）时回退到「直属 ≥2 张且无子目录」。

    注意 uid 索引会**递归**取子目录，所以像 ``01/`` 这种没有子目录的
    目录索引到的就是它自己这一层的序列数。
    """
    n = count_dicom_files(path)
    if n == 0:
        return False

    # 判据只看**本目录直属**的实际序列数（索引非递归），与有无子目录无关。
    # 实测同一台设备上：
    #   02/                8 张  1 个序列(Thoracic-spine)   → 直接打开 ✓
    #   01/               13 张  4 个序列(颈/胸/腰…混放)   → 刷新列表 ✓
    #   ...6202511807/   755 张 24 个序列(T1/T2/MRA/…)    → 刷新列表 ✓
    # 早先加了「有子目录就排除」的规则，结果把 755 张的 MR 目录也排掉了
    # （它内部恰好有 1 个子目录），真序列反而打不开。**子目录不是判据。**
    try:
        groups, _index = _series_index_uids(path)
        n_series = len(groups)
    except Exception:
        groups, n_series = {}, 0

    # 直属只有 1 张 → 不作为「序列目录」：这类多是设备散落在根下的落单
    # 片子（实测 D:\\ClearTmp\\DicomTmp 直属就1 张 MRA），而根目录本身
    # 是几十个患者目录的容器，应当刷出列表让用户挑序列。
    # 单张片子请用「打开 DICOM 文件」或直接拖入。
    if n <= 1:
        return False
    if n_series == 1:
        return True
    if n_series > 1:
        return False          # 多序列混放 → 当作患者根目录，刷新列表
    # 索引失败时的保守回退
    try:
        has_subdir = any(
            os.path.isdir(os.path.join(path, d)) and not d.startswith('.')
            for d in os.listdir(path))
    except OSError:
        has_subdir = False
    return not has_subdir


def _safe_text(node, tag: str) -> str:
    if node is None:
        return ""
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _to_int(value: str) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _to_float(value: str) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


# 缓存DICOM分析结果
_dicom_analysis_cache = {}

# 缓存目录级序列索引（精确计数，避免重复读盘）
_series_index_cache = {}

# 缓存 (ImagePositionPatient, ImageOrientationPatient)。
# 每次刷新患者列表都会对 MR 序列重算层序几何（analyze_slice_geometry），
# 一个序列有多少层就重读多少次文件头 —— 实测 878 次 dcmread = 2.4s，
# 占热缓存扫描总耗时的一半以上。几何量只随文件内容变化，
# 故以 (mtime, size) 为指纹缓存即可安全复用。
_ipp_iop_cache = {}
_IPP_IOP_TAGS = ['ImagePositionPatient', 'ImageOrientationPatient']
# 容量上限：key 含 (mtime, size)，PACS 目录持续下载新检查时旧条目会一直堆积。
# 超过阈值整表清空（简单可靠，比 LRU 更适合这种「重建代价低」的缓存）。
_IPP_IOP_CACHE_MAX = 20000


def _scan_tag_value(data: bytes, tag: bytes, vr: bytes):
    """在原始字节里按「标签 + VR + 2字节长度」提取值（仅适用于显式 VR 小端）"""
    if len(data) < 8:
        return None
    i = data.find(tag)
    while i >= 0 and i + 8 <= len(data):
        if data[i + 4:i + 6] == vr:
            length = int.from_bytes(data[i + 6:i + 8], 'little')
            return data[i + 8:i + 8 + length]
        i = data.find(tag, i + 1)
    return None


def _read_dicom_header_fast(path: str, chunk: int = 16384) -> dict:
    """快速读取 SeriesInstanceUID / Rows / Columns：先原始字节定位，失败再回退 pydicom

    绝大多数 CT 是 Explicit VR Little Endian，这几个标签都在文件头 16KB 内，
    无需构造 Dataset，比 `dcmread` 快一个数量级（全量统计上千文件时差异显著）。
    """
    info = {'uid': None, 'rows': None, 'cols': None}
    raw = None
    try:
        with open(path, 'rb') as fh:
            raw = fh.read(chunk)
    except Exception:
        raw = None

    if raw:
        v = _scan_tag_value(raw, b'\x20\x00\x0e\x00', b'UI')      # (0020,000E)
        if v:
            try:
                info['uid'] = v.decode('ascii').strip('\x00').strip() or None
            except Exception:
                info['uid'] = None
        r = _scan_tag_value(raw, b'\x28\x00\x10\x00', b'US')      # (0028,0010) Rows
        c = _scan_tag_value(raw, b'\x28\x00\x11\x00', b'US')      # (0028,0011) Columns
        if r and c and len(r) >= 2 and len(c) >= 2:
            try:
                info['rows'] = int.from_bytes(r[:2], 'little')
                info['cols'] = int.from_bytes(c[:2], 'little')
            except Exception:
                info['rows'] = info['cols'] = None

    if info['uid'] and info['rows'] and info['cols']:
        return info

    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=True,
                             specific_tags=['SeriesInstanceUID', 'Rows', 'Columns'])
        if not info['uid']:
            uid = str(getattr(ds, 'SeriesInstanceUID', '')).strip()
            info['uid'] = uid or None
        if not info['rows'] or not info['cols']:
            try:
                info['rows'] = int(getattr(ds, 'Rows', 0)) or None
                info['cols'] = int(getattr(ds, 'Columns', 0)) or None
            except Exception:
                info['rows'] = info['cols'] = None
    except Exception:
        pass
    return info


def _read_series_uid_fast(path: str, chunk: int = 16384):
    """只读取 SeriesInstanceUID（兼容旧调用）"""
    return _read_dicom_header_fast(path, chunk).get('uid')


# 目录内 dcm 数达到这个量级才值得开线程池（线程本身有固定开销）
_PARALLEL_HEADER_MIN = 64
# 并发上限：读文件头是 IO 密集而非 CPU 密集，GIL 会在 read 期间释放，
# 但线程太多会把机械盘/网络盘的寻道打散反而更慢，8 是实测的甜点。
_PARALLEL_HEADER_WORKERS = 8


def _read_headers_parallel(folder: str, names: list) -> list:
    """并发读取一批文件的 DICOM 头，返回与 names 等长且同序的结果列表

    首次扫描 5572 个文件时 `io.open` 独占 2.4s（冷缓存总耗时 4.7s 的一半），
    全是等待磁盘。并发后墙钟时间显著下降，机械盘与网络盘收益更大。
    任一个文件失败都不影响其余（退化为该文件的空 header）。
    """
    out = [None] * len(names)
    try:
        from concurrent.futures import ThreadPoolExecutor
    except Exception:          # noqa: BLE001
        return [_read_dicom_header_fast(os.path.join(folder, n))
                for n in names]

    workers = min(_PARALLEL_HEADER_WORKERS, max(1, len(names)))

    def _one(name):
        try:
            return _read_dicom_header_fast(os.path.join(folder, name))
        except Exception:      # noqa: BLE001
            return {'uid': None, 'rows': None, 'cols': None}

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, hdr in enumerate(pool.map(_one, names)):
                out[i] = hdr
    except Exception:          # noqa: BLE001
        # 线程池本身起不来（极端环境）→ 退回串行，保证功能不受影响
        out = [_one(n) for n in names]
    return out


def _scan_folder_series_index(folder: str) -> dict:
    """扫描目录内所有 .dcm，按 SeriesInstanceUID **精确**分组计数

    早期版本用「最多抽 30 个文件再按比例放大」估算每个序列的张数。
    同一目录下多序列交错存放时（如肺窗 / 纵隔窗薄层交替写入），
    采样比例必然失真，同一对序列会被算成完全不同的张数（实测 463 vs 362）。
    这里改为一次性全量统计，结果按目录缓存。

    返回: {"total": 文件总数,
           "groups": {uid: {"count": int, "files": [文件名],
                            "dims": {(rows, cols): [文件名]}}}}
    """
    if not folder or not os.path.isdir(folder):
        return {"total": 0, "groups": {}}

    try:
        entries = [(e.name, e.stat().st_mtime) for e in os.scandir(folder)
                   if e.is_file() and e.name.lower().endswith('.dcm')]
    except Exception:
        return {"total": 0, "groups": {}}

    # 目录指纹：文件数 + 最新修改时间，用于判断缓存是否仍然有效
    stamp = (len(entries), max((m for _, m in entries), default=0.0))
    cached = _series_index_cache.get(folder)
    if cached is not None and cached.get('_stamp') == stamp:
        return cached

    names = [n for n, _ in entries]
    if len(names) >= _PARALLEL_HEADER_MIN:
        headers = _read_headers_parallel(folder, names)
    else:
        headers = [_read_dicom_header_fast(os.path.join(folder, n))
                   for n in names]

    groups = {}
    for name, hdr in zip(names, headers):
        uid = hdr.get('uid') or 'unknown'
        g = groups.get(uid)
        if g is None:
            g = {"count": 0, "files": [], "dims": {}}
            groups[uid] = g
        g["count"] += 1
        g["files"].append(name)
        rows, cols = hdr.get('rows'), hdr.get('cols')
        if rows and cols:
            g["dims"].setdefault((rows, cols), []).append(name)

    result = {"_stamp": stamp, "total": len(entries), "groups": groups}
    _series_index_cache[folder] = result
    return result


def _folder_max_mtime(folder: str) -> float:
    """目录内 .dcm 的最新修改时间（无文件/目录不存在时返回 0.0）

    复用 `_scan_folder_series_index` 的缓存 —— 它 scandir 时已经拿到了全部
    mtime。此前每个序列都自己 `os.listdir` + 逐文件 `getmtime` 算一遍，
    一个 1599 张的目录若有 6 个序列就要 stat 近万次；实测全量扫描里
    `nt.stat` 被调用 4.5 万次、耗时 1.1s，纯属重复劳动。
    """
    if not folder or not os.path.isdir(folder):
        return 0.0
    stamp = _scan_folder_series_index(folder).get("_stamp")
    if not stamp:
        return 0.0
    try:
        return float(stamp[1])
    except (TypeError, ValueError):
        return 0.0


def pick_consistent_files(folder: str, names: list) -> list:
    """剔除尺寸与多数图像不一致的文件（names 为带 .dcm 的文件名）

    `vtkDICOMImageReader` 假定目录内所有图像尺寸一致，按首个文件分配缓冲区；
    一旦混入几张更大的图像就会**写越界直接段错误（0xC0000005）**，
    Python 层完全无法捕获。后重建序列（椎间盘 DiskBatch、3D_Batch 等）
    常在一个 SeriesInstanceUID 下混入少量尺寸不同的图（多为拼接截图/定位图），
    因此加载前必须先剔除这些少数派。
    """
    if not names:
        return list(names)

    index = _scan_folder_series_index(folder)
    dims_map = {}
    for g in index.get("groups", {}).values():
        for dims, files in g.get("dims", {}).items():
            for n in files:
                dims_map[n] = dims

    counter = Counter()
    for n in names:
        d = dims_map.get(n)
        if d is not None:
            counter[d] += 1
    if len(counter) <= 1:
        return list(names)

    best = counter.most_common(1)[0][0]
    kept = [n for n in names if dims_map.get(n) == best]
    dropped = len(names) - len(kept)
    if dropped > 0:
        print(f"序列尺寸不一致：{folder} 保留 {len(kept)} 张 {best[0]}x{best[1]}，"
              f"剔除 {dropped} 张尺寸不同的图像（vtkDICOMImageReader 会因此崩溃）")
    return kept


def dominant_dimension_uids(folder: str, file_uids: list) -> list:
    """按尺寸一致性过滤 file_uids（文件名去 .dcm 的形式）"""
    if not file_uids:
        return []
    names = [u + '.dcm' for u in file_uids]
    kept = set(pick_consistent_files(folder, names))
    out = [u for u in file_uids if (u + '.dcm') in kept]
    return out if out else list(file_uids)


def _series_index_uids(folder: str) -> list:
    """返回目录内各 SeriesInstanceUID 的文件名列表（不含 .dcm 后缀）"""
    index = _scan_folder_series_index(folder)
    groups = index.get("groups", {})
    out = {}
    for uid, g in groups.items():
        out[uid] = [n[:-4] if n.lower().endswith('.dcm') else n for n in g["files"]]
    return out, index


def _analyze_series_from_dicom_fast(folder: str, series_uid: str = None) -> dict:
    """
    快速分析DICOM序列信息（带缓存）
    只获取层厚等基本信息，不判断窗类型

    张数取目录级索引的**精确值**，不再用采样估算。
    元数据仍只读该序列的一个代表文件头。
    """
    cache_key = f"{folder}|{series_uid}"
    if cache_key in _dicom_analysis_cache:
        return _dicom_analysis_cache[cache_key].copy()

    series_info = {
        'slice_thickness': 0.0,
        'series_description': '',
        'body_part': '',  # 检查部位
        # 协议名。**判「血管重建序列」必须读它**：实测东软设备把 MRA
        # 血管重建批次写成 SeriesDescription="3D_Batch1" 而
        # ProtocolName="tof3d_tra_MRA"，只看描述会把血管重建当成
        # 无关中间产物扔掉（用户反馈「MRA 序列不显示」的直接原因）。
        'protocol_name': '',
        'image_type': [],
        'is_localizer': False,
        'image_count': 0,
        'modality': '',   # DICOM Modality（CT / DX / CR ...）
    }

    if not folder or not os.path.isdir(folder):
        return series_info

    index = _scan_folder_series_index(folder)
    groups = index.get("groups", {})
    if not groups:
        return series_info

    # 未指定序列时取文件数最多的那个
    target_uid = series_uid
    if not target_uid or target_uid not in groups:
        target_uid = max(groups.keys(), key=lambda k: groups[k]["count"])

    group = groups.get(target_uid)
    if not group or not group["files"]:
        return series_info

    ds = None
    for name in group["files"][:3]:
        try:
            ds = pydicom.dcmread(os.path.join(folder, name),
                                 stop_before_pixels=True, force=True)
            break
        except Exception:
            continue
    if ds is None:
        return series_info

    slice_thickness = getattr(ds, 'SliceThickness', None)
    series_desc = getattr(ds, 'SeriesDescription', '')
    image_type = getattr(ds, 'ImageType', [])
    body_part = getattr(ds, 'BodyPartExamined', '')
    protocol_name = getattr(ds, 'ProtocolName', '')
    modality = getattr(ds, 'Modality', '')

    series_info['slice_thickness'] = float(slice_thickness) if slice_thickness else 0.0
    # 国产设备常把中文按 GBK 写入，pydicom 默认按 Latin-1 解出乱码，这里修回来
    series_info['series_description'] = _fix_gbk_text(series_desc)
    series_info['body_part'] = _fix_gbk_text(body_part)
    series_info['protocol_name'] = _fix_gbk_text(protocol_name)
    series_info['modality'] = str(modality).strip().upper() if modality else ''
    series_info['image_type'] = list(image_type) if image_type else []
    series_info['image_count'] = int(group["count"])

    # 只判断是否为定位图
    is_localizer = (
        'LOCALIZER' in image_type or
        'SCOUT' in str(series_desc).upper() or
        (slice_thickness and float(slice_thickness) > 100)
    )
    series_info['is_localizer'] = is_localizer

    # 缓存结果
    _dicom_analysis_cache[cache_key] = series_info.copy()
    return series_info


_patient_header_cache = {}


def _read_patient_info_from_dicom(folder: str) -> dict:
    """直接读一个 DICOM 文件头取患者信息（XFD/XML 缺信息时的兜底）

    现场设备生成的 XFD 是 JSON 行格式，``parse_xfd_file`` 的 key=value 正则
    匹配不到任何字段；患者姓名若只靠目录名就会显示成 ``DX53650_DX``。
    这里读目录里第一个 DICOM 的头，并做 GBK 中文修复。
    """
    if not folder or not os.path.isdir(folder):
        return {}
    if folder in _patient_header_cache:
        return _patient_header_cache[folder].copy()

    out = {"pat_id": "", "pat_name": "", "pat_sex": "", "pat_age": "",
           "pat_access": "", "study_date": "", "study_time": ""}
    try:
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith(".dcm"))
    except Exception:
        names = []
    if not names:
        return out
    ds = None
    for name in names[:3]:
        try:
            ds = pydicom.dcmread(os.path.join(folder, name),
                                 stop_before_pixels=True, force=True)
            break
        except Exception:
            continue
    if ds is None:
        return out

    out["pat_id"] = str(getattr(ds, "PatientID", "") or "")
    out["pat_name"] = _fix_gbk_text(getattr(ds, "PatientName", ""))
    out["pat_sex"] = str(getattr(ds, "PatientSex", "") or "")
    out["pat_age"] = str(getattr(ds, "PatientAge", "") or "")
    out["pat_access"] = str(getattr(ds, "AccessionNumber", "") or "")
    out["study_date"] = str(getattr(ds, "StudyDate", "") or "")
    out["study_time"] = str(getattr(ds, "StudyTime", "") or "")

    _patient_header_cache[folder] = out.copy()
    return out


def parse_xfd_file(xfd_path: str) -> dict:
    """
    解析XFD文件，提取患者与序列的关键信息
    支持 key=value 或 key: value 格式，忽略无法识别的行
    """
    if not xfd_path or not os.path.isfile(xfd_path):
        return {}
    info = {
        "pat_id": "",
        "pat_access": "",
        "pat_name": "",
        "pat_sex": "",
        "pat_age": "",
        "study_uid": "",
        "study_date": "",
        "study_time": "",
        "series_uid": "",
        "series_description": "",
        "modality": "",
        "img_count": 0,
    }
    kv_re = re.compile(r'^\s*([A-Za-z0-9_]+)\s*[:=]\s*(.*?)\s*$')
    try:
        with open(xfd_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                m = kv_re.match(line)
                if not m:
                    continue
                k = m.group(1).strip().lower()
                v = m.group(2).strip()
                if k in ("patientid", "patid", "pid"):
                    info["pat_id"] = v
                elif k in ("patientname", "patname", "pname"):
                    info["pat_name"] = v
                elif k in ("patientsex", "patsex", "sex"):
                    info["pat_sex"] = v
                elif k in ("patientage", "patage", "age"):
                    info["pat_age"] = v
                elif k in ("accessionnumber", "pataccessno", "accessno"):
                    info["pat_access"] = v
                elif k in ("studyuid", "studyinstanceuid", "study_uid"):
                    info["study_uid"] = v
                elif k in ("studydate", "date"):
                    info["study_date"] = v
                elif k in ("studytime", "time"):
                    info["study_time"] = v
                elif k in ("seriesuid", "seriesinstanceuid", "series_uid"):
                    info["series_uid"] = v
                elif k in ("seriesdescription", "series_desc", "desc"):
                    info["series_description"] = v
                elif k in ("modality",):
                    info["modality"] = v.upper()
                elif k in ("imgcount", "imagecount", "count"):
                    try:
                        info["img_count"] = int(v)
                    except Exception:
                        pass
    except Exception:
        return info
    return info


def parse_xml_patient_info(xml_path: str) -> dict:
    """解析XML文件获取患者和序列信息"""
    try:
        tree = ET.parse(xml_path)
        doc = tree.getroot()
    except Exception:
        return None

    pat_info = doc.find("PatInfo")
    study_info = doc.find("StudyInfo")
    series_info = doc.find("SeriesInfo")

    if series_info is None:
        return None

    patient_data = {
        "pat_id": _safe_text(pat_info, "PatId"),
        "pat_access": _safe_text(pat_info, "PatAccessNo"),
        "pat_name": _safe_text(pat_info, "PatName"),
        "pat_sex": _safe_text(pat_info, "PatSex"),
        "pat_age": _safe_text(pat_info, "PatAge"),
        "study_uid": _safe_text(study_info, "StudyUid"),
        "study_date": _safe_text(study_info, "StudyDate"),
        "study_time": _safe_text(study_info, "StudyTime"),
        "series": [],
        "xml_path": xml_path,
        "folder": os.path.dirname(xml_path),
    }

    series_items = series_info.findall("SeriesItem")
    for series in series_items:
        modality = _safe_text(series, "Modality").upper()
        img_count = _to_int(_safe_text(series, "ImgCount"))
        series_uid = _safe_text(series, "SeriesUid")

        # 收集该序列包含的所有文件UID
        file_uids = []
        for img_node in series.findall("SeriesImg"):
            if img_node.text:
                file_uids.append(img_node.text.strip())

        patient_data["series"].append({
            "series_uid": series_uid,
            "img_count": img_count,
            "modality": modality,
            "file_uids": file_uids,
        })

    return patient_data


# ---------------------------------------------------------------------------
# MR 几何分析：层序修正与「是否为断层序列」判据（v1.9.0）
# ---------------------------------------------------------------------------

def _read_ipp_and_iop(path: str):
    """读取 (ImagePositionPatient, ImageOrientationPatient)，失败返回 (None, None)

    带 (mtime, size) 指纹缓存：这两个量只随文件内容变化，而患者列表每刷新
    一次就要对每个 MR 序列的每一层重算一遍。缓存后重复刷新的开销趋近于 0。
    `specific_tags` 让 pydicom 只解析这两个标签，首次读取也比原来快得多
    （原来会把整个文件头上千个标签都解析一遍）。
    """
    try:
        st = os.stat(path)
        key = (path, st.st_mtime, st.st_size)
    except Exception:
        key = None
    if key is not None and key in _ipp_iop_cache:
        return _ipp_iop_cache[key]

    ipp = iop = None
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=True,
                             specific_tags=_IPP_IOP_TAGS)
    except Exception:
        # 老版本 pydicom 不支持 specific_tags，退回完整头解析
        try:
            ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
        except Exception:
            ds = None
    if ds is None:
        # 读失败也缓存，避免坏文件在每次刷新时被反复重试
        if key is not None:
            if len(_ipp_iop_cache) >= _IPP_IOP_CACHE_MAX:
                _ipp_iop_cache.clear()
            _ipp_iop_cache[key] = (None, None)
        return None, None
    ipp = getattr(ds, "ImagePositionPatient", None)
    iop = getattr(ds, "ImageOrientationPatient", None)
    try:
        p = [float(v) for v in ipp] if ipp is not None and len(ipp) >= 3 else None
    except (TypeError, ValueError):
        p = None
    try:
        o = [float(v) for v in iop] if iop is not None and len(iop) >= 6 else None
    except (TypeError, ValueError):
        o = None
    if key is not None:
        if len(_ipp_iop_cache) >= _IPP_IOP_CACHE_MAX:
            _ipp_iop_cache.clear()
        _ipp_iop_cache[key] = (p, o)
    return p, o


def analyze_slice_geometry(folder: str, file_uids: list) -> dict:
    """分析序列的层序几何，返回层序重排后的文件名清单与层间距。

    **实测结论（重要，别按直觉改）**
    ``vtkDICOMImageReader`` **自己就按 ImagePositionPatient 排序**堆层，
    与文件名顺序无关：用同一批切片分别以「字母序」和「逆序」命名临时文件，
    读出的逐层序列完全一致（21/21 层吻合，且均为沿法向**降序**）。
    所以 MRI 读出来**不会层序错乱**。

    那为什么还要排？两点实际收益：

    1. **方向规范化**：vtk 固定给的是降序，而 CT 侧既有流程（以及
       ``_build_slice_positions`` 的显示、以及医学阅读习惯「从头到脚」）
       都是升序。不统一会导致同一部位在 CT 与 MR 上呈现上下颠倒。
    2. **矢状面**：矢状面 MRI 的 ImagePositionPatient 的 z 几乎恒定，
       沿法向投影才有意义（实测 ``t2_fse_sag`` 法向 ≈ X 轴，
       沿法向间距 6.0mm，而 z 恒为 91~97）。只按 z 排会判定「同一层」。

    排序键 = 切片位置沿层法向的投影。法向取 ImageOrientationPatient 的
    两个行向量叉积（等价于标准 ``cross(row_dir, col_dir)``，
    行序约定与 DICOM PS3.3 一致），无 IOP 时退回 z 轴。

    返回::

        {
          'ordered_uids': [按层序升序排列的文件名（不含 .dcm）],
          'normal': [nx, ny, nz],
          'slice_spacing': 层间距 mm（无法确定时为 None）,
          'monotonic': 原始顺序是否已单调,
          'reordered': 是否与文件名字母序不同,
        }
    """
    uids = [str(u).strip() for u in (file_uids or []) if str(u).strip()]
    n = len(uids)
    result = {
        "ordered_uids": list(uids),
        "normal": None,
        "slice_spacing": None,
        "monotonic": True,
        "reordered": False,
    }
    if n < 2:
        return result

    points = [None] * n
    iop_ref = None
    for i, u in enumerate(uids):
        p, o = _read_ipp_and_iop(os.path.join(folder, u + ".dcm"))
        points[i] = p
        if o is not None and iop_ref is None:
            iop_ref = o

    # 缺 IPP 的文件无法定位层序，退回文件名顺序
    if any(p is None for p in points):
        return result

    if iop_ref is not None:
        row = np.array(iop_ref[0:3], dtype=np.float64)
        col = np.array(iop_ref[3:6], dtype=np.float64)
        normal = np.cross(row, col)
        norm = float(np.linalg.norm(normal))
        if norm > 1e-6:
            normal = normal / norm
        else:
            normal = None
    else:
        normal = None

    pts = np.array(points, dtype=np.float64)
    if normal is None:
        # 没有 IOP 时退回 z 轴（CT 设备普遍如此，等价于原行为）
        normal = np.array([0.0, 0.0, 1.0])

    proj = pts.dot(normal)

    # 法向符号规范化：使首末差值为正，保证排序方向稳定可复现
    span = float(proj[-1] - proj[0])
    if abs(span) > 1e-6:
        if span < 0:
            normal = -normal
            proj = -proj

    order = np.argsort(proj, kind="stable")
    result["normal"] = [float(v) for v in normal]
    result["ordered_uids"] = [uids[i] for i in order]
    result["reordered"] = bool(list(order) != list(range(n)))
    result["monotonic"] = bool(np.all(np.diff(proj) > 0))

    # 层间距取相邻差值的中位数，对个别层重复/缺失更稳健
    d = np.diff(np.sort(proj))
    d = d[d > 1e-6]
    if d.size:
        result["slice_spacing"] = float(np.median(d))
    return result


def is_stackable_series(geometry: dict, image_count: int,
                        max_spacing: float = 20.0) -> tuple:
    """判断序列能否作为「断层体数据」堆层，返回 (可堆层, 原因)

    设备除了正常的断层序列，还会写出两类**不能**堆层的东西，
    实测某头部 MR 检查（755 文件 / 24 序列）里两类都出现了：

    1. **重建批次**（``3D_Batch1/2``，1520×1520，25 张）：所有切片的
       ImagePositionPatient **完全相同**，沿法向投影间距为 0。它们是
       同一张体数据被切成多批渲染的**中间产物**，不是断层。
       硬堆起来会得到 25 层「同一张图」，三维重建出来是一堵墙。
    2. **定位像**（``gre_scout``，3 张正交定位图）：层间距为 0 或远大于
       层厚，属于定位用途而非断层。

    判据（任一命中即不可堆层）：

    - 张数 < 2：单张图不是体数据；
    - 投影间距中位数为 0（近似 0.05mm 以内）：切片位置重合，是批次/重复；
    - 层间距 > max_spacing：远大于层厚的「层」是重建产物或定位像
      （正常断层层间距不会超过 20mm）。
    """
    if image_count < 2:
        return False, "单张图像，无法堆层"
    sp = geometry.get("slice_spacing")
    if sp is None:
        return False, "无法确定层间距（缺少 ImagePositionPatient）"
    if sp < 0.05:
        return False, "切片位置重合（重建批次/重复层，实测间距 %.3fmm）" % sp
    if sp > max_spacing:
        return False, "层间距 %.1fmm 超出断层范围（>%.0fmm，疑为定位像或重建批次）" % (
            sp, max_spacing)
    return True, ""


def is_scout_series(series_desc: str, body_part: str = "",
                    image_type=None) -> bool:
    """定位像/示波图识别（MR 与 CT 共用判定口径）

    MR 侧的典型序列名是 ``gre_scout``；CT 侧是 Scout 定位像。
    """
    desc = "%s %s" % (_fix_gbk_text(series_desc or ""),
                      _fix_gbk_text(body_part or ""))
    low = desc.lower()
    for kw in ("scout", "localizer", "localiser", "survey", "topogram",
               "定位", "概览"):
        if kw in low:
            return True
    it = " ".join(str(x) for x in (image_type or []))
    for kw in ("LOCALIZER", "SCOUT", "SURVEY", "TOPOGRAM"):
        if kw in it.upper():
            return True
    return False


# MR 序列的「重建批次」命名：3D_Batch1 / 3D_Batch2 / Batch_1 ...
# 这些**不能**只凭名字当成中间产物丢掉 —— 实测东软设备正是用它们
# 承载 MRA 血管重建结果（ProtocolName=tof3d_tra_MRA，各 25 张），
# 正是用户要看的东西。判「是不是血管」必须看 ProtocolName。
_MR_BATCH_DESC_RE = re.compile(r"(^|[^a-z])batch\s*\d*", re.I)


def is_mr_batch_desc(series_desc: str) -> bool:
    """序列描述是否形如 ``3D_Batch1`` / ``Batch_2``（设备的重建批次命名）"""
    return bool(_MR_BATCH_DESC_RE.search(_fix_gbk_text(series_desc or "")))


def mr_series_kind(series_desc: str, protocol_name: str = "",
                   body_part: str = "") -> str:
    """粗分 MR 序列类型，供窗值预设与三维策略选择。

    返回小写标识：``mra`` / ``dwi`` / ``adc`` / ``t1`` / ``t2`` /
    ``flair`` / ``pd`` / ``unknown``。

    **协议名的优先级高于序列描述**：实测 MRA 血管重建批次的描述是
    ``3D_Batch1``（看不出是什么），协议名才是 ``tof3d_tra_MRA``。
    只看描述会把它归成 unknown，进而在几何校验里当「重建批次」剔掉，
    MRA 序列就从患者列表里消失了。
    """
    desc = _fix_gbk_text(series_desc or "")
    proto = _fix_gbk_text(protocol_name or "")
    # 先按协议名判血管（mra/tof/angio），命中即定；协议名没有再用描述
    for text in (proto, desc):
        t = text.lower()
        if "mra" in t or "tof" in t or "angio" in t:
            return "mra"
    text = ("%s %s" % (desc, proto)).lower()
    if not text.strip():
        return "unknown"
    # 血管成像：tof / mra / angiograph
    if "mra" in text or "tof" in text or "angio" in text:
        return "mra"
    # 弥散：ADC 表征图必须**先于 DWI 判定**。实测序列名形如
    # ``epi_dwi_tra_ADC``，名字里同时含 "dwi" 和 "adc"，若先查 dwi
    # 就会把 ADC 图误分类，而两者显示窗差了一个数量级
    # （实测 DWI 的 WW 数百、ADC 的 WW 达 3886），分类错了窗值就跟着错。
    if "adc" in text or "apparent" in text:
        return "adc"
    if "dwi" in text or "b1000" in text or "b0" in text:
        return "dwi"
    if "flair" in text:
        # T2-FLAIR 与 T1-FLAIR 都含 flair，看 t1/t2 前缀细分
        return "t1" if ("t1" in text and "t2" not in text) else "flair"
    if "t2" in text:
        return "t2"
    if "t1" in text:
        return "t1"
    if "pd" in text or "proton" in text:
        return "pd"
    if "mip" in text:
        return "mra"
    return "unknown"


def mr_series_readable(series: dict) -> tuple:
    """判断 MR 序列是否值得放进患者列表，返回 (是否保留, 原因)

    MR 检查里混着三类东西，只有第一类该给用户看：

    - **断层序列**（T1/T2/FLAIR/DWI/ADC…）：正常显示。
    - **血管重建序列**：**必须显示**，只是不能堆层（见下）。
    - **定位像/示波图**（``gre_scout``）：阅片价值低，且层间距 130mm
      根本不是断层，加载后三维必然崩坏。
    - **真·重建批次**：切片位置全部重合、且序列名/协议名都判不出
      任何影像类型的那种中间产物，才丢弃。

    注意判「定位像」要用**序列名**而不是层厚：MR 的 scout 层厚写在
    ``SliceThickness`` 里是 8mm（看起来像正常断层），但
    ``SpacingBetweenSlices`` / 实际层间距是 130mm。只看层厚会漏判。

    **血管重建序列的处理（v1.10.0 修）**：实测东软设备把 MRA 血管
    重建导出成 ``3D_Batch1`` / ``3D_Batch2``（各 25 张，1520×1520），
    描述看不出是什么，``ProtocolName`` 才是 ``tof3d_tra_MRA``；它们
    **不写 ImagePositionPatient**，几何校验必然判「无法确定层间距」。
    早期版本据此直接丢弃，导致两个血管重建序列从患者列表整体消失。
    现在改为：保留在列表供二维逐张翻阅，并置 ``mr_stackable=False``，
    由三维重建环节据此拦截并给出明确说明。
    """
    modality = str(series.get("modality") or "").strip().upper()
    if not is_mr_modality(modality):
        return True, ""

    desc = series.get("series_description") or ""
    body_part = series.get("body_part") or ""
    if is_scout_series(desc, body_part, series.get("image_type")):
        return False, "MR 定位像（%s）" % (desc or "未命名")

    n = int(series.get("img_count") or 0)
    folder = series.get("folder") or ""
    uids = series.get("file_uids") or []
    kind = mr_series_kind(desc, series.get("protocol_name") or "", body_part)

    if n >= 2 and folder and uids:
        geo = analyze_slice_geometry(folder, uids)
        ok, why = is_stackable_series(geo, n)
        if not ok:
            # **血管重建序列（MRA/TOF/血管 MIP）永不丢弃**。
            # 设备导出血管重建时经常不写 ImagePositionPatient，或者把
            # 重建批次写成 "3D_Batch1"（ProtocolName 才是 tof3d_tra_MRA），
            # 几何校验必然判不通过 —— 但它恰恰是用户要看的血管图像。
            # 早期版本直接 return False，结果两个 25 张的血管重建序列
            # 从患者列表里整体消失（用户反馈「MRA 序列不显示」）。
            # 正确做法：留在列表里供二维逐张翻阅，只把「不能堆层成体数据」
            # 记成标记，三维重建时再据此拦截。
            if kind == "mra":
                series["mr_stackable"] = False
                series["mr_unstackable_reason"] = (
                    "血管重建序列，无法按层堆叠为体数据（仅支持二维浏览）")
                return True, ""
            # 通用重建批次：名字不含血管信息且协议名也判不出类型时才丢。
            # 若只是协议名缺失而序列名确实像血管（tof/mra），同样保留。
            series["mr_stackable"] = False
            series["mr_unstackable_reason"] = why
            return False, why
    series["mr_stackable"] = True
    return True, ""


def _series_center_point(series: dict):
    """取序列的图像中心世界坐标（读首末切片的IPP 求中点），失败返回 None"""
    folder = series.get("folder") or ""
    uids = series.get("file_uids") or []
    if not folder or not uids:
        return None
    picks = [uids[0], uids[-1]] if len(uids) > 1 else uids[:1]
    pts = []
    for u in picks:
        p, _ = _read_ipp_and_iop(os.path.join(folder, str(u) + ".dcm"))
        if p is not None:
            pts.append(p)
    if not pts:
        return None
    return [float(sum(c[i] for c in pts) / len(pts)) for i in range(3)]


def _dedup_series_titles(series_list: list):
    """同名序列补上可区分的标注（原地修改 series_list）。

    只有当描述完全相同、且不止一个序列时才动手，避免给唯一序列乱加后缀。
    优先用身体侧别/方位（头部序列常有 "L"/"R" 前缀），否则退到图像中心位置。
    """
    if len(series_list) < 2:
        return
    groups = {}
    for s in series_list:
        key = str(s.get("series_description") or "").strip()
        groups.setdefault(key, []).append(s)

    for key, items in groups.items():
        if len(items) < 2:
            continue

        # 1) 序列名里自带侧别（…_L / …_R / 左 / 右）时无需处理
        low = key.lower()
        if any(w in low for w in ("_l", "_r", "left", "right", "左", "右")):
            continue

        # 2) 否则用图像中心位置排序，标成 (1)/ (2) …，并附上相对位置
        located = []
        for idx, s in enumerate(items):
            c = _series_center_point(s)
            located.append((idx, c))
        with_pos = [(i, c) for i, c in located if c is not None]
        if len(with_pos) < 2:
            continue

        # 以最长的分辨轴排序，得到稳定的前后次序
        axis = max(range(3), key=lambda i: max(c[i] for _i, c in with_pos)
                   - min(c[i] for _i, c in with_pos))
        with_pos.sort(key=lambda t: t[1][axis])

        total = len(with_pos)
        for rank, (idx, c) in enumerate(with_pos, start=1):
            name = key or "序列"
            extra = "%s（%d/%d）" % (name, rank, total)
            items[idx]["series_description"] = extra
            items[idx]["series_dedup_axis"] = axis
            items[idx]["series_dedup_pos"] = float(c[axis])


# ---------------------------------------------------------------------------
# MR 窗值策略（v1.9.0）
# ---------------------------------------------------------------------------

# 各 MR 序列的常用显示窗（WW, WL）。单位是设备写入的信号强度，
# 与 CT 的 HU 毫无关系，因此**不能套用 CT 的肺窗/骨窗预设**。
# 取自设备实测值附近（见 _test_mri_support.py 的实测样本），
# 仅在 DICOM 未写入可用窗值时作为兜底。
MR_WINDOW_PRESETS = {
    "t1":     (1850.0, 944.0),     # T1：白质亮、脂肪亮
    "t2":     (1998.0, 1000.0),    # T2：脑脊液亮
    "flair":  (800.0, 400.0),      # FLAIR：抑制脑脊液高信号
    "dwi":    (1100.0, 550.0),     # DWI：b1000 弥散加权
    "adc":    (3600.0, 1800.0),    # ADC：表观弥散系数
    "mra":    (540.0, 290.0),      # MRA：血管高信号
    "pd":     (1200.0, 600.0),
}

# MR 的窗值合理性区间。**刻意比 CT 宽得多**：ADC 图的
# WC1615/WW3578 是完全正常的 MR 窗值，但会被 CT 的
# (-1500~1500 / 1~6000) 校验判成异常而错误兜底成 400/40 → 整幅发黑。
MR_WINDOW_LIMITS = {
    "wc_min": -4096.0, "wc_max": 65535.0,
    "ww_min": 1.0,     "ww_max": 131070.0,
}


def mr_plausible_window(window, level) -> bool:
    """MR 窗值合理性校验（与 CT 的 _plausible_window 刻意不同）"""
    try:
        w = float(window)
        c = float(level)
    except (TypeError, ValueError):
        return False
    if not (np.isfinite(w) and np.isfinite(c)):
        return False
    lim = MR_WINDOW_LIMITS
    return lim["ww_min"] <= w <= lim["ww_max"] and lim["wc_min"] <= c <= lim["wc_max"]


def mr_window_level(dicom_wc, dicom_ww, kind: str = "unknown",
                    p1: float = None, p99: float = None) -> tuple:
    """决定 MR 的显示窗值，返回 ``(窗宽, 窗位)``

    优先级：

    1. **DICOM 自带窗值**且通过 MR 合理性校验 → 直接用（设备最了解自己的
       序列，MR 的窗值本就是设备按序列调好的）；
    2. 设备没写或写坏 → 按序列类型的预设（``MR_WINDOW_PRESETS``）；
    3. 都没有 → 按实测像素分位数（p1~p99）线性拉伸。

    绝不能复用 CT 的肺窗/纵膈窗/骨窗/脑窗：那些是 HU 量纲，
    套到 MR 信号强度上会把画面压成全黑或全白。
    """
    if mr_plausible_window(dicom_ww, dicom_wc):
        return (float(dicom_ww), float(dicom_wc))

    preset = MR_WINDOW_PRESETS.get(kind)
    if preset is not None:
        return preset

    # 分位数兜底：取 p1~p99 作为显示区间，避开极值噪点
    try:
        if p1 is not None and p99 is not None:
            lo = float(p1)
            hi = float(p99)
            if np.isfinite(lo) and np.isfinite(hi) and hi - lo >= 1.0:
                return (hi - lo, (hi + lo) / 2.0)
    except (TypeError, ValueError):
        pass

    # 最后兜底：覆盖常见 16bit 范围，保证不是黑屏
    return (4000.0, 2000.0)


def mr_percentile_range(folder: str, file_uids: list, sample: int = 8):
    """采样若干切片，返回 (p1, p99) 像素强度分位数。用于窗值兜底与血管阈值自适应。"""
    uids = [str(u) for u in (file_uids or []) if str(u)]
    if not uids:
        return None, None
    step = max(1, len(uids) // max(1, sample))
    picks = uids[::step][:sample]
    chunks = []
    for u in picks:
        try:
            ds = pydicom.dcmread(os.path.join(folder, u + ".dcm"), force=True)
            arr = ds.pixel_array.astype(np.float32)
            chunks.append(arr.reshape(-1))
        except Exception:
            continue
    if not chunks:
        return None, None
    allv = np.concatenate(chunks)
    lo, hi = np.percentile(allv, [1.0, 99.0])
    return float(lo), float(hi)


def mr_is_surveyor_single(series: dict) -> bool:
    """判断这个 MR 序列是否只是设备的「单张概览图」。

    实测场景：某些设备把 MRA 导出成**孤立的一张** ``tof3d_tra_MRA``
    （512×442），直接躺在扫描根目录下 —— 同一台设备的另一个患者
    同一序列名却有完整 186 张 0.5mm 各向同性薄层。列表里只显示
    「1张」时用户会以为解析出错，其实是设备只给了概览。

    **不能用 ImageType 的 ``DC2D`` 判断**：实测这台设备所有 MR 序列
    （含186 张的正常断层）都带 ``DC2D``（DICOM 里``DC`` = 未经处理的
    采集数据，是 MR 原始数据的标准标记），用它当判据会把全部序列
    都标成概览。

    判据只能是**「单张」 + 「序列名声称是血管/断层序列」**这个组合，
    纯属展示层提示，不影响数据是否收录。
    """
    modality = str(series.get("modality") or "").strip().upper()
    if not is_mr_modality(modality):
        return False
    if int(series.get("img_count") or 0) > 1:
        return False
    desc = _fix_gbk_text(series.get("series_description") or "").lower()
    if not desc:
        return False
    # 序列名在说「这是一个断层/血管序列」，却只有 1 张 → 概览图
    for kw in ("mra", "tra", "angio", "t1", "t2", "flair", "dwi", "adc"):
        if kw in desc:
            return True
    return False


def _iter_ct_patient_dirs(root_dir: str) -> List[str]:
    """兼容旧签名：只返回 CT 患者目录（内部转调 _iter_patient_dirs）"""
    return [p for p, _hint in _iter_patient_dirs(root_dir)]


def _looks_like_dicom_uid_dir(base: str) -> bool:
    """判断目录名是否形如 DICOM UID（``1.2.840.31314...6202511807``）

    判据是「点号分段且每段都是数字」。注意 UID 目录名**必然含点号**，
    所以不能用 ``"." not in name`` 来排除普通目录 —— 那样会把真正的
    UID 目录全部漏掉（MR 检查正是这种命名，导致 755 张片子整体消失）。
    """
    if not base or "." not in base:
        return False
    parts = base.split(".")
    if len(parts) < 3:          # UID 至少三段（组织根.产品.流水）
        return False
    return all(p.isdigit() for p in parts)


def _iter_patient_dirs(root_dir: str) -> List[tuple]:
    """递归查找患者目录，返回 ``[(目录, 模态提示), ...]``

    同时覆盖 CT、X 线与 MR 目录：目录名后缀形如 ``CT71285_CT`` /
    ``DX53650_DX``（X 线还可能是 ``*_CR`` / ``*_DR`` / ``*_RF`` / ``*_XA``），
    MR 可能是 ``*_MR``；且目录内存在 ``.dcm`` 文件。

    **MRI 的目录名往往是纯 UID**（东软等设备把检查号直接当目录名，形如
    ``1.2.840.31314.14143234.20251213000000.6202511807``），既没有模态后缀，
    同目录也未必有 XML 清单。若只按后缀匹配，MR 患者会**完全不出现在列表里**
    （实测 755 张 / 24 序列的 MR 检查就是这样丢失的）。
    因此追加一条兜底规则：名字里不含 ``.``（不是文件）且直接含 ``.dcm`` 文件的
    目录，一律作为候选，模态提示留空——真实模态随后一律以 DICOM 的
    Modality 标签为准，同目录混放多模态也不会错判。
    """
    xr_suffixes = ("_dx", "_cr", "_dr", "_rf", "_xa", "_mg")
    mr_suffixes = ("_mr", "_mri")
    candidates = []
    root_norm = os.path.normcase(os.path.abspath(root_dir))
    for r, dirs, files in os.walk(root_dir):
        base = os.path.basename(r).lower()
        if base.endswith("ct"):
            candidates.append((r, "CT"))
        elif base.endswith(xr_suffixes):
            candidates.append((r, "XR"))
        elif base.endswith(mr_suffixes):
            candidates.append((r, "MR"))
        elif _looks_like_dicom_uid_dir(base) and any(
                        f.lower().endswith(".dcm") for f in files):
            # 兜底：DICOM UID 命名的设备目录（MR 常见），模态以 DICOM 标签为准
            candidates.append((r, ""))
        elif os.path.normcase(os.path.abspath(r)) == root_norm and any(
                        f.lower().endswith(".dcm") for f in files):
            # 兜底：扫描根目录本身就直接躺着 dcm（设备把单张片子直接写在
            # 输出根下，实测有 MRA 单张这么落盘）。根目录不是患者目录，
            # 但不能因此让这批片子整体丢失。
            candidates.append((r, ""))
    result = []
    seen = set()
    for patient_dir, hint in candidates:
        rp = os.path.normcase(os.path.abspath(patient_dir))
        if rp in seen:
            continue
        seen.add(rp)
        has_dcm = False
        for rr, _, ff in os.walk(patient_dir):
            if any(f.lower().endswith(".dcm") for f in ff):
                has_dcm = True
                break
        if has_dcm:
            result.append((patient_dir, hint))
    return result


def _file_uids_resolve(folder: str, file_uids: list) -> int:
    """返回 file_uids 中能在目录内匹配到 .dcm 文件的数量

    `file_uids` 的约定是「文件名去掉 .dcm」；但 XFD 里写的是 SOPInstanceUID，
    永远匹配不上。用它判断既有清单是否可信。
    """
    if not folder or not os.path.isdir(folder) or not file_uids:
        return 0
    try:
        names = {n[:-4] if n.lower().endswith('.dcm') else n
                 for n in os.listdir(folder) if n.lower().endswith('.dcm')}
    except Exception:
        return 0
    return sum(1 for u in file_uids if u in names)


def _parse_xfd_sequences(xfd_paths: List[str], root_base: str) -> dict:
    sequences = {}
    ct_uids = CT_CLASS_UIDS
    xr_uids = XR_CLASS_UIDS
    mr_uids = MR_CLASS_UIDS
    for xfd in xfd_paths:
        try:
            with open(xfd, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or (line[0] != "{" and ":" not in line):
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    series_uid = obj.get("seriesinstanceUid") or obj.get("seriesInstanceUid")
                    class_uid = obj.get("classUid")
                    if not series_uid:
                        continue
                    seq = sequences.setdefault(
                        series_uid,
                        {"count": 0, "paths": [], "sopuids": [], "modality": ""})
                    if class_uid in ct_uids:
                        seq["modality"] = "CT"
                    elif class_uid in xr_uids:
                        seq["modality"] = "XR"
                    elif class_uid in mr_uids:
                        seq["modality"] = "MR"
                    else:
                        continue
                    seq["count"] += 1
                    sc = obj.get("storageControl") or ""
                    if sc:
                        rel = sc.replace("/", os.sep)
                        abspath = os.path.join(root_base, rel)
                        seq["paths"].append(abspath)
                    suid = obj.get("sopinstanceUid")
                    if suid:
                        seq["sopuids"].append(suid)
        except Exception:
            continue
    return sequences


# ---------------------------------------------------------------------------
# X 线「一张片子 = 一个序列」拆分（v1.7.5）
# ---------------------------------------------------------------------------

# 同一 SeriesInstanceUID 内超过这个张数就不拆：RF 介入透视 / 多帧序列
# 一个序列可能有几十帧，拆开会让患者列表徒增几十行，反而没法用。
XR_VIEW_SPLIT_MAX = 8

_XR_VIEW_CN = ("正位", "侧位", "斜位", "轴位", "切线位", "水平位", "开口位")

_XR_VIEW_EN = {
    "AP": "正位", "PA": "正位", "ANTERIOR": "正位", "POSTERIOR": "正位",
    "LAT": "侧位", "LATERAL": "侧位",
    "RAO": "右前斜位", "LAO": "左前斜位", "RPO": "右后斜位", "LPO": "左后斜位",
    "AXIAL": "轴位",
}


def _xr_view_label(ds) -> str:
    """从单张 DICOM 头推断体位（正位/侧位/…），判不出来返回空串"""
    view = str(getattr(ds, "ViewPosition", "") or "").strip().upper()
    if view in _XR_VIEW_EN:
        return _XR_VIEW_EN[view]
    desc = str(getattr(ds, "SeriesDescription", "") or "")
    for kw in _XR_VIEW_CN:
        if kw in desc:
            return kw
    for kw, cn in _XR_VIEW_EN.items():
        if len(kw) >= 2 and view.startswith(kw):
            return cn
    return ""


def split_xr_series_by_image(series: dict) -> list:
    """把「一个 SeriesInstanceUID 里装了多张片子」的 X 线序列拆成逐张单图

    现场设备（尤其国产 DR/CR）常把一次检查的**正位 + 侧位**写进**同一个**
    SeriesInstanceUID，只靠 InstanceNumber 1 / 2 区分（腕关节、胸片、腰椎等
    多体位检查都这么干）。此前序列一律按 UID 分组，于是两张片子被合成列表里的
    一行：缩略图只画抽到的那一张、检测取 ``file_uids`` 的另一张，用户看到的就是
    「预览一张、检测另一张」，而另一张片子**在列表里根本找不到**。
    X 线一张片子本来就是单张图像，按图拆开最符合阅片习惯，也顺带杜绝了
    预览与检测选到不同片子的可能。

    只拆 X 线（CT 一个 UID 下几百层是正常的，绝不能拆），且张数落在
    ``1 < n <= XR_VIEW_SPLIT_MAX``；RF 透视这种多帧序列保持原样。
    """
    modality = str(series.get("modality") or "").strip().upper()
    if not is_xr_modality(modality):
        return [series]

    uids = [str(u).strip() for u in (series.get("file_uids") or []) if str(u).strip()]
    if not uids:
        return [series]
    files = [u[:-4] if u.lower().endswith(".dcm") else u for u in uids]
    n = len(files)
    if n <= 1 or n > XR_VIEW_SPLIT_MAX:
        return [series]

    folder = series.get("folder") or ""
    base_desc = _fix_gbk_text(series.get("series_description") or "")
    base_part = _fix_gbk_text(series.get("body_part") or "")
    orig_uid = str(series.get("series_uid") or "")

    rows = []
    for i, name in enumerate(files):
        path = os.path.join(folder, name + ".dcm")
        desc, part, view = base_desc, base_part, ""
        inum = i + 1
        ds = None
        try:
            ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
        except Exception:
            ds = None
        if ds is not None:
            desc = _fix_gbk_text(getattr(ds, "SeriesDescription", "") or "") or base_desc
            part = _fix_gbk_text(getattr(ds, "BodyPartExamined", "") or "") or base_part
            view = _xr_view_label(ds)
            try:
                inum = int(getattr(ds, "InstanceNumber", i + 1) or (i + 1))
            except Exception:
                inum = i + 1
        try:
            mt = float(os.path.getmtime(path))
        except Exception:
            mt = float(series.get("mtime", 0.0) or 0.0)
        rows.append((i, inum, desc, part, view, mt))

    # 按 InstanceNumber 排序：正位通常在前，同序列内顺序稳定
    rows.sort(key=lambda t: (t[1], t[0]))

    titles = set()
    out = []
    for rank, (i, _inum, desc, part, view, mt) in enumerate(rows):
        title = desc or part or "图像"
        if view and view not in title:
            title = "%s %s" % (title, view)
        titles.add(title)
        item = dict(series)
        item["series_uid"] = ("%s#%d" % (orig_uid, i)) if orig_uid else ("%s#%d" % (folder, i))
        item["img_count"] = 1
        item["file_uids"] = [files[i]]
        item["series_description"] = title
        item["body_part"] = part
        item["mtime"] = mt
        item["split_index"] = rank
        out.append(item)

    # 多张片子描述完全一样时（不少设备只写「腕关节」），补 (i/n) 让两行可区分
    if len(titles) <= 1:
        for k, item in enumerate(out):
            suffix = "（%d/%d）" % (k + 1, n)
            item["series_description"] = (item["series_description"] + suffix).strip()
    return out


def collect_ct_patients(root_dir: Union[str, List[str]], min_images: int = 0,
                       progress_callback=None) -> list:
    """
    收集CT患者信息

    Args:
        root_dir: 根目录路径或目录列表
        min_images: 最小图像数量阈值（默认0，显示所有序列）
        progress_callback: 进度回调函数，接收(当前数量, 总数, 当前患者名)
    """
    patients = {}
    series_total = 0
    root_dirs: List[str] = []
    if isinstance(root_dir, (list, tuple)):
        root_dirs = [d for d in root_dir if isinstance(d, str) and os.path.isdir(d)]
    elif isinstance(root_dir, str) and os.path.isdir(root_dir):
        root_dirs = [root_dir]
    else:
        return []

    # 收集所有XML文件与患者目录（CT 与 X 线目录一并收集）
    xml_files = []
    ct_patient_dirs = []          # [(目录, 模态提示), ...]
    for rd in root_dirs:
        for root, _, files in os.walk(rd):
            for name in files:
                if name.lower().endswith(".xml"):
                    xml_files.append(os.path.join(root, name))
        # 特殊处理：名称以 CT / DX / CR / DR 结尾的目录作为患者目录
        ct_patient_dirs.extend(_iter_patient_dirs(rd))

    series_total = len(xml_files)

    # 处理XML来源
    for idx, xml_path in enumerate(xml_files):
        patient_data = parse_xml_patient_info(xml_path)

        if patient_data is None:
            continue

        # 报告进度
        if progress_callback:
            progress_callback(idx + 1, series_total, patient_data.get('pat_name', '未知'))

        patient_key = f"{patient_data['pat_id']}|{patient_data['pat_access']}|{patient_data['pat_name']}"

        if patient_key not in patients:
            patients[patient_key] = {
                "pat_id": patient_data["pat_id"],
                "pat_access": patient_data["pat_access"],
                "pat_name": patient_data["pat_name"],
                "pat_sex": patient_data["pat_sex"],
                "pat_age": patient_data["pat_age"],
                "study_date": patient_data["study_date"],
                "study_time": patient_data["study_time"],
                "series": [],
                "series_keys": set(),
            }

        series_folder = patient_data["folder"]

        for series in patient_data["series"]:
            modality = str(series["modality"] or "").strip().upper()
            is_xr = is_xr_modality(modality)
            is_mr = is_mr_modality(modality)
            if not (modality == "CT" or is_xr or is_mr):
                continue

            img_count = series["img_count"]
            series_uid = series["series_uid"]
            file_uids = series.get("file_uids", [])

            # 从DICOM文件获取层厚等基本信息
            dicom_info = _analyze_series_from_dicom_fast(series_folder, series_uid)

            # 目录级精确索引：张数以全量扫描为准，不再用采样估算
            index_uids, _ = _series_index_uids(series_folder)
            exact_files = index_uids.get(series_uid) or []
            if exact_files:
                img_count = len(exact_files)
                # XML 清单不完整（或写的是 SOPInstanceUID）时，改用扫描到的精确清单，
                # 否则加载时会漏层或退化成「整个目录一起读」
                if _file_uids_resolve(series_folder, file_uids) < len(exact_files):
                    file_uids = sorted(exact_files)
            elif dicom_info.get('image_count', 0) > img_count:
                img_count = dicom_info['image_count']

            # 跳过定位图
            if dicom_info.get('is_localizer', False):
                continue

            # 跳过单张图像的序列（通常是报告），但对于薄层CT可以放宽此限制
            # 如果DICOM分析显示有更多图像，应该使用DICOM的计数
            if img_count <= 1:
                dicom_count = dicom_info.get('image_count', 0)
                if dicom_count > 1:
                    img_count = dicom_count
                elif not (is_xr or is_mr):
                    # X 线一张片子就是一个序列（如「颈椎正位」），MR 的单张
                    # 序列（MRA 的 MIP 重建图）同理，1 张是常态而非报告
                    continue

            if img_count < min_images:
                continue

            series_key = f"{series_uid}|{series_folder}"
            if series_key in patients[patient_key]["series_keys"]:
                continue

            patients[patient_key]["series_keys"].add(series_key)
            patients[patient_key]["series"].append({
                "series_uid": series_uid,
                "img_count": img_count,
                "modality": modality,
                "folder": series_folder,
                "slice_thickness": dicom_info.get('slice_thickness', 0),
                "series_description": dicom_info.get('series_description', ''),
                "body_part": dicom_info.get('body_part', ''),  # 检查部位
                "protocol_name": dicom_info.get('protocol_name', ''),
                "image_type": dicom_info.get('image_type', []),
                "file_uids": file_uids,
                "mtime": _folder_max_mtime(series_folder),
            })

    # 处理患者目录：以XFD分组为序列（CT 与 X 线共用这条路径）
    for patient_dir, dir_hint in ct_patient_dirs:
        all_xfd = []
        for rr, _, ff in os.walk(patient_dir):
            for f in ff:
                if f.lower().endswith(".xfd"):
                    all_xfd.append(os.path.join(rr, f))
        base_info = parse_xfd_file(all_xfd[0]) if all_xfd else {}
        # XFD 常常拿不到患者信息（JSON 格式匹配不到 key=value），
        # 用 DICOM 头补齐，避免姓名退化成目录名
        dcm_pat = _read_patient_info_from_dicom(patient_dir)
        for _k in ("pat_id", "pat_name", "pat_sex", "pat_age",
                   "pat_access", "study_date", "study_time"):
            if not base_info.get(_k) and dcm_pat.get(_k):
                base_info[_k] = dcm_pat[_k]
        pat_id = base_info.get("pat_id", "")
        pat_access = base_info.get("pat_access", "")
        pat_sex = base_info.get("pat_sex", "")
        pat_age = base_info.get("pat_age", "")
        study_date = base_info.get("study_date", "")
        study_time = base_info.get("study_time", "")
        # 姓名优先用 DICOM/XFD 里的真实姓名，取不到才退回目录名
        pat_name = (base_info.get("pat_name") or "").strip() or os.path.basename(patient_dir)

        patient_key = f"{pat_id}|{pat_access}|{pat_name}"
        if patient_key not in patients:
            patients[patient_key] = {
                "pat_id": pat_id,
                "pat_access": pat_access,
                "pat_name": pat_name,
                "pat_sex": pat_sex,
                "pat_age": pat_age,
                "study_date": study_date,
                "study_time": study_time,
                "series": [],
                "series_keys": set(),
            }

        sequences = _parse_xfd_sequences(all_xfd, os.path.dirname(os.path.dirname(patient_dir)))
        # 没有 XFD（部分设备不生成）时退化成「按 SeriesInstanceUID 直接扫目录」，
        # 否则这类目录会一个序列都出不来。X 线目录尤其常见。
        if not sequences:
            _idx, _ = _series_index_uids(patient_dir)
            for uid, files in _idx.items():
                sequences[uid] = {
                    "count": len(files),
                    "paths": [os.path.join(patient_dir, f) for f in files],
                    "sopuids": [],
                    # 目录名没有模态后缀时（UID 命名的 MR 目录）留空，
                    # 真实模态由下面的 DICOM 标签决定
                    "modality": dir_hint if dir_hint in ("CT", "MR") else "XR",
                }
        series_total += len(sequences)
        for series_uid, seq in sequences.items():
            dicom_info = _analyze_series_from_dicom_fast(patient_dir, series_uid)

            # XFD 的 sopuids 是 SOPInstanceUID，不是文件名，永远匹配不上，
            # 会让加载退化成「整个目录一起读」（多个序列被混成一个体数据）。
            # 这里改用按 SeriesInstanceUID 精确扫描出的文件名清单。
            index_uids, _ = _series_index_uids(patient_dir)
            exact_files = index_uids.get(series_uid) or []
            file_uids = sorted(exact_files) if exact_files else seq.get("sopuids", [])

            img_count = max(seq.get("count", 0), dicom_info.get("image_count", 0))
            if exact_files:
                img_count = len(exact_files)

            # 真实模态优先取 DICOM 标签，其次 XFD 的 classUid，最后才是目录名提示
            dicom_modality = dicom_info.get("modality", "")
            if dicom_modality:
                modality = dicom_modality
            elif seq.get("modality") == "CT":
                modality = "CT"
            elif seq.get("modality") == "MR":
                modality = "MR"
            elif dir_hint == "MR":
                modality = "MR"
            elif dir_hint == "CT":
                modality = "CT"
            else:
                modality = "DX"
            is_xr = is_xr_modality(modality)
            is_mr = is_mr_modality(modality)

            # X 线一张片子就是一个序列，「1 张」是常态而不是报告页；
            # MR 的单张序列（MRA 的 MIP 重建图等）同样是有意义的影像
            if img_count <= 1 and not (is_xr or is_mr):
                continue
            if img_count < min_images:
                continue
            if progress_callback:
                progress_callback(series_total, series_total, pat_name)
            series_key = f"{series_uid}|{patient_dir}"
            if series_key in patients[patient_key]["series_keys"]:
                continue
            patients[patient_key]["series_keys"].add(series_key)
            mtime = 0.0
            for pth in seq.get("paths", []):
                try:
                    mtime = max(mtime, os.path.getmtime(pth))
                except Exception:
                    continue
            if mtime == 0.0:
                mtime = _folder_max_mtime(patient_dir)
            patients[patient_key]["series"].append({
                "series_uid": series_uid,
                "img_count": img_count,
                "modality": modality,
                "folder": patient_dir,
                "slice_thickness": dicom_info.get('slice_thickness', 0),
                "series_description": dicom_info.get('series_description', ''),
                "body_part": dicom_info.get('body_part', ''),
                "protocol_name": dicom_info.get('protocol_name', ''),
                "image_type": dicom_info.get('image_type', []),
                "file_uids": file_uids,
                "mtime": mtime,
            })

    result = []
    for patient in patients.values():
        if not patient["series"]:
            continue

        # X 线多体位检查（正位+侧位写进同一个 SeriesInstanceUID）拆成逐张，
        # 否则两张片子会被合成一行，列表里只剩一张缩略图、另一张找不到。
        expanded = []
        for _s in patient["series"]:
            expanded.extend(split_xr_series_by_image(_s))

        # MR 筛除定位像与重建批次（3D_Batch/scout）。放在拆序列之后，
        # 因为 X 线拆分会把 img_count 改成 1，而 MR 判定依赖真实张数与几何。
        kept = []
        for _s in expanded:
            _ok, _why = mr_series_readable(_s)
            if _ok:
                kept.append(_s)
            elif _why:
                print("已跳过 MR 序列: %s（%s）" % (_s.get("series_description") or "未命名", _why))
        patient["series"] = kept

        # 给「设备只导出一张概览图」的 MR 序列打标记，供列表层显示提示。
        # 数据照常保留（可能是设备只导出了概览，不该让用户打不开），
        # 只是明确告诉用户「这张不是完整序列」，避免误以为解析出错。
        for _s in patient["series"]:
            _s["is_surveyor_single"] = mr_is_surveyor_single(_s)

        # 按图像数量降序排序（X 线都是 1 张，排序稳定，保持原有先后）
        patient["series"].sort(key=lambda x: x["img_count"], reverse=True)

        # 同名序列消歧：MR 一次检查里同名序列很常见（实测某头部 MR 有两个
        # 都叫 tof3d_tra_MRA 的 186 张序列，分别是动脉期/静脉期或左右侧），
        # 列表里两行文字完全一样就分不出是哪一段。序列描述取不到差别时，
        # 用「图像中心位置」标注方位——这是 DICOM 唯一可靠的物理区别。
        _dedup_series_titles(patient["series"])

        # 患者级模态：同一患者混了多种模态时归入 MIXED，供列表前缀使用
        mods = {str(s.get("modality") or "").upper() for s in patient["series"]}
        has_ct = "CT" in mods
        has_xr = any(is_xr_modality(m) for m in mods)
        has_mr = any(is_mr_modality(m) for m in mods)
        if has_ct and has_xr and has_mr:
            patient["modality"] = "MIXED"
        elif has_ct and has_xr:
            patient["modality"] = "MIXED"
        elif has_ct and has_mr:
            patient["modality"] = "MIXED"
        elif has_xr and has_mr:
            patient["modality"] = "MIXED"
        elif has_ct:
            patient["modality"] = "CT"
        elif has_mr:
            patient["modality"] = "MR"
        elif has_xr:
            patient["modality"] = "XR"
        else:
            patient["modality"] = sorted(mods)[0] if mods else ""

        # 计算患者的最新时间戳（用于排序）
        try:
            patient["mtime"] = max((s.get("mtime", 0.0) for s in patient["series"]), default=0.0)
        except Exception:
            patient["mtime"] = 0.0

        result.append(patient)

    # 按最新文件时间降序排序（最新在前）
    result.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)

    return result


def get_best_series_for_reconstruction(patient: dict) -> dict:
    """
    获取最适合三维重建的序列（图像数量最多的）
    """
    if not patient or "series" not in patient:
        return None

    ct_series = [s for s in patient["series"] if s["modality"] == "CT"]
    if not ct_series:
        return None

    # 选择图像数量最多的
    ct_series.sort(key=lambda x: x["img_count"], reverse=True)
    return ct_series[0] if ct_series else None


def _first_value(value):
    """取 DICOM 元素的第一个值（VM>1 时）。

    pydicom 对多值元素返回的是 ``MultiValue``，它是
    ``collections.abc.MutableSequence`` 的子类，**不是** list/tuple 的实例，
    因此 ``isinstance(value, (list, tuple))`` 判不出，直接 ``float()`` 会抛::

        TypeError: float() argument must be a string or a number, not 'MultiValue'

    国产 DR 常把多组窗值预设写进 WindowCenter/WindowWidth（VM=2），
    CT3D 加载这类 X 线序列时正是在这里整批失败。这里改用鸭子类型判定。
    """
    for _ in range(8):                      # 防御性上限，杜绝意外死循环
        if value is None:
            return None
        if isinstance(value, (str, bytes)):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, (list, tuple)):
            value = value[0] if len(value) else None
            continue
        # MultiValue 等序列类型：能按下标取就取第 0 个
        if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
            try:
                value = value[0] if len(value) else None
                continue
            except Exception:      # noqa: BLE001
                break
        if hasattr(value, "value"):         # DataElement 之类
            try:
                value = value[0]
                continue
            except Exception:      # noqa: BLE001
                break
        break
    return value


def _safe_float(value, default=None):
    """把 DICOM 元素安全地转成 float（多值取第一个，失败返回 default）"""
    v = _first_value(value)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def analyze_dicom_series(folder: str, sample_limit: int = 20) -> dict:
    """分析DICOM序列，用于加载后的详细分析"""
    if not folder or not os.path.isdir(folder):
        return {}

    files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".dcm")]
    files.sort()
    if not files:
        return {}

    try:
        ds0 = pydicom.dcmread(files[0], stop_before_pixels=True, force=True)
        window_center = _first_value(getattr(ds0, "WindowCenter", None))
        window_width = _first_value(getattr(ds0, "WindowWidth", None))
        slice_thickness = getattr(ds0, "SliceThickness", None)
        pixel_spacing = getattr(ds0, "PixelSpacing", None)
    except Exception as e:
        print(f"读取DICOM头信息失败: {e}")
        return {}

    # 采样分析HU值
    if len(files) > sample_limit:
        idx = np.linspace(0, len(files) - 1, sample_limit).astype(int)
        sample_files = [files[i] for i in idx]
    else:
        sample_files = files

    values = []
    for fp in sample_files:
        try:
            ds = pydicom.dcmread(fp, force=True)
            px = ds.pixel_array.astype(np.int16)
            slope = _safe_float(getattr(ds, "RescaleSlope", 1), 1.0)
            intercept = _safe_float(getattr(ds, "RescaleIntercept", 0), 0.0)
            hu = px * slope + intercept
            values.append(hu.reshape(-1))
        except Exception as e:
            print(f"采样分析失败 {fp}: {e}")
            continue

    if not values:
        return {}

    allv = np.concatenate(values)
    p99 = float(np.percentile(allv, 99))
    iso = float(np.clip(p99, 200, 1200))

    result = {
        "window_center": _safe_float(window_center),
        "window_width": _safe_float(window_width),
        "recommended_iso": iso,
        "slice_thickness": _safe_float(slice_thickness),
        "image_count": len(files),
    }

    if pixel_spacing is not None:
        try:
            if hasattr(pixel_spacing, "__len__") and len(pixel_spacing) >= 2:
                result["pixel_spacing_x"] = _safe_float(pixel_spacing[0])
                result["pixel_spacing_y"] = _safe_float(pixel_spacing[1])
        except Exception:      # noqa: BLE001
            pass

    return result


# ---------------------------------------------------------------------------
# X 线 AI 功能 ↔ 检查部位 适配（v1.9.1）
# ---------------------------------------------------------------------------
#
# 起因：五个 AI 功能各自只在特定部位上有意义（足弓只在足部片、骨龄只在左手
# 腕片、脊柱侧弯只在全脊柱正位、胸片检测只在胸部片），但界面上五个按钮一直
# 是「载入 X 线就全部放开」，在胸片上点「足弓测量」模型照样跑出一个角度值，
# 结果毫无临床意义却看起来像正常结果 —— 这比报错更危险。
#
# 因此这里把「部位 → 可用功能」的关系显式定义出来：载入 X 线时先判部位，
# 不匹配的功能按钮直接置灰并说明原因，点击路径上再做一次拦截。

XRAY_PART_CHEST = "chest"
XRAY_PART_SPINE = "spine"
XRAY_PART_WRIST = "wrist"
XRAY_PART_FOOT = "foot"
XRAY_PART_LIMB = "limb"
XRAY_PART_UNKNOWN = "unknown"

XRAY_PART_LABELS = {
    XRAY_PART_CHEST: "胸部",
    XRAY_PART_SPINE: "脊柱",
    XRAY_PART_WRIST: "手腕",
    XRAY_PART_FOOT: "足部",
    XRAY_PART_LIMB: "四肢/关节",
    XRAY_PART_UNKNOWN: "未识别部位",
}

# 顺序即优先级。
#   1) 足/腕放最前：它们是最特异的（一旦命中就基本锁定功能）；
#   2) 脊柱必须排在胸部之前：「胸椎/胸腰段」里都含「胸」字，先判胸部会把
#      腰椎正位误判成胸片；
#   3) 四肢放最后兜底：它是一大类泛称，不该抢走更特异的部位。
XRAY_PART_KEYWORDS = (
    (XRAY_PART_FOOT, (
        "FOOT", "FEET", "ANKLE", "TOE", "CALCANEUS", "METATARSAL", "TARSAL",
        "足", "踝", "跟骨", "跖骨", "趾骨", "足弓", "足部",
    )),
    (XRAY_PART_WRIST, (
        "WRIST", "HAND", "FINGER", "CARPAL", "METACARPAL", "PHALANX",
        "腕", "手", "掌", "指", "骨龄",
    )),
    (XRAY_PART_SPINE, (
        "SPINE", "SPINAL", "CERVICAL", "THORACIC", "LUMBAR", "SACRUM",
        "TSPINE", "LSPINE", "CSPINE", "WHOLESPINE",
        "脊柱", "脊椎", "颈椎", "胸椎", "腰椎", "骶椎", "骶尾",
        "胸腰段", "腰骶", "全脊柱",
    )),
    (XRAY_PART_CHEST, (
        "CHEST", "CHESTPA", "CHESTAP", "CHESTLAT", "THORAX", "LUNG", "RIB",
        "胸部", "胸片", "胸腔", "肺", "肋骨", "心肺",
    )),
    (XRAY_PART_LIMB, (
        "KNEE", "HIP", "PELVIS", "SHOULDER", "ELBOW", "ARM", "LEG", "THIGH",
        "FEMUR", "TIBIA", "FIBULA", "HUMERUS", "RADIUS", "ULNA", "CLAVICLE",
        "PATELLA", "JOINT", "LIMB",
        "膝", "髋", "骨盆", "肩", "肘", "臂", "腿", "股骨", "胫骨", "腓骨",
        "肱骨", "桡骨", "尺骨", "锁骨", "髌骨", "关节", "四肢",
    )),
)

# 各 AI 功能适用的部位集合。
#   · 胸片检测只有胸部片：把胸片拿去做病变检测以外的项目没有意义；
#   · 骨折检测覆盖所有含骨部位（四肢/手腕/脊柱/足部），但排除胸部
#     —— 胸部有专门的胸片检测，且肋骨骨折不在该模型的训练范围内；
#   · 骨龄只在左手腕片（RUS-CHN 中华 05 的拍摄规范就是左手腕正位）；
#   · 足弓只在足部片。
XRAY_AI_PART_SCOPE = {
    "chest":     (XRAY_PART_CHEST,),
    "fracture":  (XRAY_PART_LIMB, XRAY_PART_WRIST, XRAY_PART_SPINE,
                  XRAY_PART_FOOT),
    "spine":     (XRAY_PART_SPINE,),
    "boneage":   (XRAY_PART_WRIST,),
    "foot_arch": (XRAY_PART_FOOT,),
}

# 「智能识别并检测」按部位默认选哪个功能
XRAY_AUTO_PICK = {
    XRAY_PART_CHEST: "chest",
    XRAY_PART_SPINE: "spine",
    XRAY_PART_WRIST: "boneage",
    XRAY_PART_FOOT: "foot_arch",
    XRAY_PART_LIMB: "fracture",
}

# xray 侧 BodyPartDetector 的粗分类 → 细分部位（自己判不出时的兜底）
_XRAY_COARSE_TO_PART = {
    "chest_xray": XRAY_PART_CHEST,
    "fracture": XRAY_PART_LIMB,
    "spine": XRAY_PART_SPINE,
    "bone_age": XRAY_PART_WRIST,
}


def xray_part_key(*texts) -> str:
    """从任意文本（部位名 / 序列名 / 检查描述）判定 X 线检查部位。

    返回 ``XRAY_PART_*`` 之一；判断不出返回 ``XRAY_PART_UNKNOWN``。
    纯函数，便于单测。
    """
    blob = " ".join(str(t or "") for t in texts)
    if not blob.strip():
        return XRAY_PART_UNKNOWN
    low = blob.lower()
    # 中文不做大小写，统一在一个串里按优先级顺序找
    for part, words in XRAY_PART_KEYWORDS:
        for w in words:
            if w.lower() in low:
                return part
    return XRAY_PART_UNKNOWN


def xray_part_key_from_dicom(path) -> str:
    """直接读一个 DICOM 的头判定部位（BodyPartExamined 优先，序列名兜底）。

    只读头不读像素，单张耗时可忽略。任何异常都退回 unknown，由调用方
    用列表里的部位字段再兜一次。

    普通图片（PNG/JPG…）先直接判 unknown：pydicom 带 force=True 也能把
    图片读出个数据集来，那纯属瞎解析，可能凭空吐出个部位名让 AI 用错模型。
    """
    if not path or not os.path.isfile(str(path)):
        return XRAY_PART_UNKNOWN
    if is_image_file(path):
        return XRAY_PART_UNKNOWN
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    except Exception:      # noqa: BLE001
        return XRAY_PART_UNKNOWN

    def _txt(tag):
        return _fix_gbk_text(getattr(ds, tag, "") or "")

    parts = [_txt("BodyPartExamined"), _txt("SeriesDescription"),
             _txt("StudyDescription"), _txt("ProtocolName"),
             _txt("RequestedProcedureDescription")]
    return xray_part_key(*parts)


def xray_coarse_to_part(coarse) -> str:
    """xray 侧 BodyPartDetector 的粗分类映射到细分部位 key"""
    return _XRAY_COARSE_TO_PART.get(str(coarse or "").strip(),
                                    XRAY_PART_UNKNOWN)


def xray_ai_kinds_for_part(part_key: str, ignore: bool = False) -> tuple:
    """某部位下可用的 AI 功能 key 列表。

    ``ignore=True`` 或部位未识别时放开全部 —— 设备乱写 BodyPart 是常态，
    不能因为判不出部位就把功能锁死，此时改用提示文案提醒用户自行判断。
    """
    if ignore or not part_key or part_key == XRAY_PART_UNKNOWN:
        return tuple(XRAY_AI_PART_SCOPE.keys())
    return tuple(k for k, parts in XRAY_AI_PART_SCOPE.items()
                 if part_key in parts)


def xray_part_mismatch_text(kind_cn: str, part_key: str) -> str:
    """部位不匹配时给用户的说明文案"""
    allowed = [k for k, p in XRAY_AI_PART_SCOPE.items() if part_key in p]
    if allowed:
        tip = "当前片子的检查部位是「%s」，%s 不适用于该部位。" % (
            XRAY_PART_LABELS.get(part_key, part_key), kind_cn)
    else:
        tip = ("当前片子的检查部位是「%s」，%s 不适用于该部位，"
               "且该部位没有其它可用的 AI 功能。" % (
                   XRAY_PART_LABELS.get(part_key, part_key), kind_cn))
    return tip



# ---------------------------------------------------------------------------
# 儿科心影校正（v1.9.3）
# ---------------------------------------------------------------------------
#
# 起因：胸片检测模型（VinDr-CXR 14 类）在儿童胸片上几乎必报「心脏肥大」。
# 这不是模型坏了 —— 婴幼儿心影在胸片上本来就相对大，心胸比（CTR，
# 心脏最大横径 / 胸廓内缘最大横径）的正常值随年龄变化：
#
#     年龄        生理上限       成人分界
#     < 1 岁      0.60          成人 0.50
#     1 ~ 4 岁    0.55
#     >= 5 岁     0.50（同成人）
#
# 而在实际使用中，儿童胸片和成人胸片进的是同一个模型、同一个阈值，于是所
# 有儿童都被判成 Cardiomegaly。这属于「模型跑通了、结论却是假的」，和足弓
# 模型跑胸片是同一类错误，必须拦。
#
# 修法不是简单地把儿童的置信度门槛拉高 —— 那样会把真正的病理性心脏增大一
# 起压掉。这里做的是二次判读：用模型给出的心脏框估算 CTR，再和该年龄的儿
# 科生理上限比较，只有超出上限才认定为异常。

# 儿科判定年龄上限（岁）。>= 该年龄按成人标准处理。
PAEDIATRIC_MAX_AGE = 15.0

# CTR 判定容差。估算值本身有 ±0.03 量级的误差，恰好卡在阈值上（例如 8 岁
# 估出 0.502）时不该判为异常 —— 取 CTR > 上限 + 容差 才算超出。
CTR_TOL = 0.01

# 各年龄段 CTR 生理上限：(年龄下界，上限)，按表格顺序取最后一个满足的
_PAEDE_CTR_TABLE = (
    (0.0, 0.60),      # < 1 岁
    (1.0, 0.55),      # 1 ~ 4 岁
    (5.0, 0.50),      # >= 5 岁，与成人一致
)


def parse_dicom_age(text):
    """把 DICOM AS 类型的年龄串转成岁（"005Y"/"018M"/"030D"/"002W"）。

    也容忍设备直接写数字的情况（"12" 按岁算）。解析不出来返回 None。
    纯函数，便于单测。
    """
    if text is None:
        return None
    s = str(text).strip().upper().replace(" ", "")
    if not s:
        return None
    unit_factor = {"Y": 1.0, "M": 1.0 / 12.0, "W": 1.0 / 52.0,
                   "D": 1.0 / 365.0}
    unit = s[-1]
    if unit in unit_factor:
        num = s[:-1]
    else:
        unit, num = "Y", s
    try:
        val = float(num)
    except (TypeError, ValueError):
        return None
    try:
        years = val * unit_factor[unit]
    except KeyError:
        return None
    if years != years or years < 0 or years > 130:      # NaN / 越界
        return None
    return years


def _ymd_to_date(text):
    """DICOM DA 日期串 -> (y, m, d)；失败返回 None"""
    s = str(text or "").strip()
    if len(s) < 8 or not s[:8].isdigit():
        return None
    y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
    if not (1900 <= y <= 2200 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return y, m, d


def age_from_birth_date(birth, study):
    """由 PatientBirthDate + StudyDate 算岁数（按月折算，保留一位小数粒度）"""
    b, s = _ymd_to_date(birth), _ymd_to_date(study)
    if b is None or s is None:
        return None
    by, bm, bd = b
    sy, sm, sd = s
    months = (sy - by) * 12 + (sm - bm)
    if sd < bd:                       # 当月还没到生日
        months -= 1
    years = months / 12.0
    if years < 0 or years > 130:
        return None
    return years


def patient_age_years(path=None, ds=None):
    """读出患者年龄（单位：岁，float）；读不到返回 None。

    优先 PatientAge（设备一般会写，但老旧设备常缺），
    退回 PatientBirthDate + StudyDate / SeriesDate / AcquisitionDate 自己算。
    """
    if ds is None:
        if not path or not os.path.isfile(str(path)):
            return None
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True,
                                 force=True)
        except Exception:             # noqa: BLE001
            return None
    age = parse_dicom_age(getattr(ds, "PatientAge", ""))
    if age is not None:
        return age
    birth = getattr(ds, "PatientBirthDate", "")
    if not birth:
        return None
    for tag in ("StudyDate", "SeriesDate", "AcquisitionDate",
                "ContentDate"):
        got = age_from_birth_date(birth, getattr(ds, tag, ""))
        if got is not None:
            return got
    return None


def paediatric_ctr_limit(age_years) -> float:
    """某年龄的心胸比生理上限；成人 / 年龄未知返回 0.50"""
    try:
        age = float(age_years)
    except (TypeError, ValueError):
        return _PAEDE_CTR_TABLE[-1][1]
    limit = _PAEDE_CTR_TABLE[0][1]
    for low, val in _PAEDE_CTR_TABLE:
        if age >= low:
            limit = val
    return limit


# 判定「有效曝光区」的相对阈值。列均值剖面里，未被照射的区域（准直黑边）
# 落在一个低位平台上，曝光区内是一个高位平台；取两者之间的这个比例作为分界。
THORAX_LEVEL_FRAC = 0.35

# 两个平台各自的最小相对高差（灰度级）。低于它就认为这侧没有黑边。
THORAX_MIN_CONTRAST = 8.0


def estimate_thorax_width(gray):
    """从已显示的灰度图估算胸廓横径（像素），失败返回 None。

    思路：X 线片被准直器挡住的区域几乎不感光（灰度贴地），照射区内是另一
    个强度平台。取图像中段的列均值剖面，在每一侧比较「该侧边缘平台」与
    「中心平台」，两者的相对阈值即为曝光边界。这个宽度在正负位片上约等于
    胸廓横径。

    两个平台的取法有讲究（都是踩过坑改的）：

    1. 中心平台取**中间 40% 列的均值**。胸腔中心必然是曝光区，这个值不会
       被边缘结构带偏。
    2. 边缘平台取**最外侧 3% 列的中位数**，而不是全剖面分位数。早期版本
       用 ``percentile(col, 55)`` 当高平台，遇到宽心影时就出事：心影占了大
       半列宽，把 55% 分位拉到心影亮度上，阈值跟着上移，肺野被当成背景剔
       掉，胸廓被低估一半、CTR 直接虚高到 1.0 —— 正是最不该错的方向。
    3. 某一侧若没有黑边（片子已裁到皮肤），两侧平台高差不足，就直接从该侧
       边缘起算，而不是硬套阈值。

    另外早期还写过「跳过黑边 -> 找胸壁亮带 -> 找进入肺野的下降沿」的三段
    结构，在合成图上很准，但**真实片子不成立**：阳性列剖面从边界进到胸腔
    后一路维持高位，中间从不回落到阈值以下 —— 胸腔里不止肺野，脊柱、纵隔、
    肋骨、膈下腹部的密度叠在一起。实测 5 张真实儿童胸片有 4 张直接算错。

    必须说清楚精度：这样得到的是胸廓外缘（含软组织）宽度，比 CTR 定义里的
    「肋骨内缘」横径大约宽 5%~15%，因此算出的 CTR **系统性偏低**。这是有意
    的 —— 宁可漏报一例真性增大，也不要继续每张儿童片都误报。

    纯 numpy 纯函数，可用合成图单测。
    """
    arr = np.asarray(gray)
    if arr.ndim == 3:
        arr = arr[:, :, :3].mean(axis=2)
    if arr.size == 0:
        return None
    arr = arr.astype(np.float64)
    if arr.ndim != 2:
        return None
    h, w = arr.shape
    if h < 40 or w < 80:
        return None

    roi = arr[int(h * 0.15):int(h * 0.85)]
    if roi.size == 0:
        return None
    col = roi.mean(axis=0)
    k = int(max(3, w * 0.015)) | 1
    kernel = np.hanning(k)
    kernel = kernel / kernel.sum()
    col = np.convolve(col, kernel, mode="same")

    # 中心平台：胸腔中部必然是曝光区
    center = col[int(w * 0.30):int(w * 0.70)]
    if center.size == 0:
        return None
    hi = float(np.median(center))
    if hi <= 0:
        return None

    # 连续命中所需的最小长度，用来滤掉孤立亮点/噪点
    run = max(3, int(w * 0.01))

    def _edge(edge_vals, indices, lo_first):
        """从一侧向中间扫，返回第一个「连续命中」的位置；无黑边则该侧从边缘起算"""
        lo = float(np.median(edge_vals))
        if hi - lo < THORAX_MIN_CONTRAST:
            return indices[0] if lo_first else indices[-1]
        thr = lo + THORAX_LEVEL_FRAC * (hi - lo)
        n = len(indices)
        for a in range(0, n - run):
            if all(col[indices[a + b]] > thr for b in range(run)):
                return indices[a]
        return None

    left = _edge(col[:max(4, int(w * 0.03))], list(range(w)), True)
    right = _edge(col[-max(4, int(w * 0.03)):], list(range(w - 1, -1, -1)),
                  False)
    if left is None or right is None:
        return None

    width = right - left
    # 合理性校验：胸廓横径不会小于片宽的 1/3，也不会超出整幅
    if not (0.30 * w <= width <= 1.00 * w):
        return None
    return int(width)


def estimate_ctr(gray, heart_box):
    """估算心胸比 = 心脏检测框宽度 / 胸廓内缘横径。

    ``heart_box`` 为 (x1, y1, x2, y2)。任一步失败返回 None。
    """
    if gray is None or heart_box is None:
        return None
    try:
        x1, y1, x2, y2 = [float(v) for v in heart_box]
    except (TypeError, ValueError):
        return None
    heart_w = x2 - x1
    if heart_w <= 0:
        return None
    thorax = estimate_thorax_width(gray)
    if not thorax:
        return None
    ctr = heart_w / float(thorax)
    # 上限取 1.0：心脏不可能比胸廓还宽，超过说明框或胸廓估算已失效，
    # 此时宁可判为「未能核实」，也不要吐一个不可能的数字出来。
    if not (0.15 <= ctr <= 1.00):
        return None
    return float(ctr)


def review_cardiomegaly(age_years, ctr, confidence):
    """对一次「心脏肥大」诊断做儿科二次判读。

    返回 dict：
      keep   True = 作为阳性检出保留
      level  "positive" 异常 / "physiologic" 生理性 / "unverified" 未能核实
      label  降级后显示用的标签（keep=True 时没有该键）
      text   给用户看的一句话依据

    规则：
      · 年龄未知 -> 不校正，保留（宁可多报，不能锁死功能）
      · 成人     -> 不校正，保留
      · 儿童 + CTR 可估：超出该年龄生理上限才保留，否则判为生理性
      · 儿童 + CTR 不可估：判为「未能核实」，不计入病变数。

    最后这条是踩过坑才改的：最初写的是「拉高儿童置信度门槛兜底」，实测
    站不住 —— 真实儿童胸片上这个模型给出的心脏肥大置信度普遍 0.77~0.86，
    比成人真阳性还高，门槛提到 0.55 甚至 0.8 都拦不住任何一张。与其装样
    子，不如诚实标为未能核实，把原始结论和置信度一并交给用户。
    """
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.0

    if age_years is None:
        return {"keep": True, "level": "unverified",
                "text": "未能读取患者年龄，未做儿科心影校正"}
    try:
        age = float(age_years)
    except (TypeError, ValueError):
        return {"keep": True, "level": "unverified",
                "text": "年龄格式异常，未做儿科心影校正"}
    if age >= PAEDIATRIC_MAX_AGE:
        return {"keep": True, "level": "positive", "text": ""}

    limit = paediatric_ctr_limit(age)
    if ctr is not None:
        ctr = float(ctr)
        if ctr > limit + CTR_TOL:
            return {"keep": True, "level": "positive",
                    "text": "估计心胸比 %.2f，超过 %.0f 岁儿童生理上限 "
                            "%.2f，请结合临床" % (ctr, age, limit)}
        return {"keep": False, "level": "physiologic",
                "label": "心影偏大（儿童生理性）",
                "text": "估计心胸比 %.2f，未超过 %.0f 岁儿童生理上限 %.2f，"
                        "儿童心影偏大多为生理性，此条不计入检出病变" % (
                            ctr, age, limit)}

    return {"keep": False, "level": "unverified",
            "label": "心影偏大（未能核实）",
            "text": "此片无法估算心胸比（模型置信度 %.2f）。儿童心影生理性偏大"
                    "很常见，此项未计入检出病变，请结合临床判断" % conf}


# ---------------------------------------------------------------------------
# 常见图片文件（v1.9.4）
# ---------------------------------------------------------------------------
#
# 「打开文件」按钮原本只吃 .dcm。实际使用中经常拿到的是导出的 PNG/JPG
# （科室从 PACS 导出的片子、截图、手机拍的胶片等），一律被拒很不方便。
# 这里把「哪些扩展名按普通图片处理」显式定义出来，供打开按钮、拖放识别、
# 以及部位判定（图片没有 DICOM 头，不能拿去解析 BodyPartExamined）共用。
#
# 注意：不包含 .gif —— OpenCV 默认不带 GIF 解码器，列进来只会得到一个
# 「选得中、打不开」的假支持。
IMAGE_FILE_EXTS = (
    ".png", ".jpg", ".jpeg", ".jpe", ".bmp", ".tif", ".tiff", ".webp",
)


def is_image_file(path) -> bool:
    """路径是否为常见图片文件（按扩展名判定，纯函数）"""
    if not path:
        return False
    try:
        ext = os.path.splitext(str(path))[1].lower()
    except Exception:            # noqa: BLE001
        return False
    return ext in IMAGE_FILE_EXTS
