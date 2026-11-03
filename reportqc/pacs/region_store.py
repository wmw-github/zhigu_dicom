# -*- coding: utf-8 -*-
"""报告识别区域的持久化（QSettings → Windows 注册表 HKCU）。

为什么单独一个模块
------------------
用户的要求是「**设置一次，之后每次点刷新就识别**」。区域必须跨进程保存，
且必须与业务数据库解耦：

* 业务库 `report_qc.db` 在软件目录，只读环境下要回退 `%APPDATA%`，
  位置会飘；区域是纯本机 UI 偏好，不该混进去；
* `QSettings` 在 Win7/10/11 都写 ``HKCU\\Software\\ZhiGu\\CT3D``，
  绿色包免安装、免管理员权限也能写；
* 与悬浮小窗的位置持久化（`mini_window.py`）共用同一命名空间。

多套配置（v1.20.0）
------------------
医生机器上同时装了两套写报告软件，报告区位置、版式都不一样，一套区域
覆盖不了。所以存储从「一条 x,y,w,h」升级为「**一组配置**」，每条记住：

============  ==========================================================
``exe``       框选时前台进程名（如 ``report.exe``）—— 主匹配键
``title``     框选时的完整窗口标题 —— exe 取不到时的兜底
``rect``      屏幕全局坐标 [x, y, w, h]
``layout``    版式（``form`` 表格型 / ``titled`` 标题型 / ``auto``）
``name``      给人看的名字（从窗口标题里截的，设置窗/就绪态展示）
============  ==========================================================

**为什么用 exe 而不是窗口标题做主键**：PACS 窗口标题里带患者姓名和
检查号（「报告编辑 - 张三」），换个病人标题就变了，拿它当键第二份报告
就匹配不上。exe 才是稳定的。

旧格式（``reportqc/region`` 单条）在首次读取时自动迁移成一条配置，
迁移后**保留原 key 不删** —— 万一用户退回旧版本，区域设置还在。

坐标系
------
存的是**屏幕全局坐标**下的 QRect —— 与 ``ScreenPicker.finished`` 发出的
矩形、``QScreen.grabWindow`` 接受的矩形完全一致（DPI aware 进程下
逻辑像素 == 物理像素），所以保存后可以直接拿去截图，无需换算。
"""

import json
import uuid

import PyQt5.QtCore as QtCore

__all__ = ["load_profiles", "save_profile", "match_profile",
           "remove_profile", "get_profile", "clear_profiles",
           "load_region", "save_region", "clear_region", "region_text"]

ORG = "ZhiGu"
APP = "CT3D"
KEY_PROFILES = "reportqc/profiles"     # JSON 数组
KEY_LEGACY = "reportqc/region"         # 旧格式 "x,y,w,h"（只读迁移用）

# 小于此边长的区域视为脏数据 / 误触残留，拒绝加载
_MIN_SIZE = 20


def _settings():
    return QtCore.QSettings(ORG, APP)


def _on_any_screen(rect):
    """区域是否还落在某个屏幕范围内（换分辨率/拔掉副屏后可能失效）。"""
    try:
        widgets_app = None
        try:
            import PyQt5.QtWidgets as QtWidgets
            widgets_app = QtWidgets.QApplication.instance()
        except Exception:                              # noqa: BLE001
            widgets_app = None
        target = widgets_app or QtCore.QCoreApplication.instance()
        if target is None or not hasattr(target, "screens"):
            return True                                # 没有 GUI 环境，不阻断
        screens = target.screens()
        if not screens:
            return True
        for s in screens:
            if s.geometry().intersects(rect):
                return True
        return False
    except Exception:                                  # noqa: BLE001
        return True


def _rect_ok(rect):
    return (rect is not None
            and rect.width() >= _MIN_SIZE and rect.height() >= _MIN_SIZE
            and _on_any_screen(rect))


def _to_rect(raw):
    """``[x, y, w, h]`` / ``"x,y,w,h"`` → QRect；非法返回 None。"""
    try:
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.split(",")]
        elif isinstance(raw, (list, tuple)) and len(raw) == 4:
            parts = list(raw)
        else:
            return None
        if len(parts) != 4:
            return None
        x, y, w, h = [int(float(p)) for p in parts]
        rect = QtCore.QRect(x, y, w, h)
        return rect if _rect_ok(rect) else None
    except Exception:                                  # noqa: BLE001
        return None


def _rect_to_list(rect):
    return [int(rect.x()), int(rect.y()),
            int(rect.width()), int(rect.height())]


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def _read_raw():
    """读原始 JSON 列表（不做 rect 校验，保留脏数据以便后续清理）。"""
    try:
        raw = _settings().value(KEY_PROFILES)
        if raw is None:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        if isinstance(raw, str):
            data = json.loads(raw)
        elif isinstance(raw, (list, dict)):
            data = raw                            # 某些后端会直接回原生类型
        else:
            return []
        if isinstance(data, dict):
            data = [data]
        return [p for p in (data or []) if isinstance(p, dict)]
    except Exception:                                  # noqa: BLE001
        return []


def _write_raw(items):
    try:
        s = _settings()
        s.setValue(KEY_PROFILES, json.dumps(items, ensure_ascii=False))
        s.sync()
        return True
    except Exception:                                  # noqa: BLE001
        return False


def _normalize(item):
    """原始条目 → 统一结构（rect 转 QRect，缺字段补默认）。"""
    rect = _to_rect(item.get("rect"))
    if rect is None:
        return None
    return {
        "id": str(item.get("id") or uuid.uuid4().hex[:12]),
        "name": str(item.get("name") or ""),
        "exe": str(item.get("exe") or "").lower(),
        "title": str(item.get("title") or ""),
        "layout": str(item.get("layout") or "auto"),
        "rect": rect,
    }


def _migrate_legacy():
    """旧单条区域 → 一条配置（``exe`` 空，靠「只有一个就兜底」生效）。"""
    try:
        raw = _settings().value(KEY_LEGACY)
        if raw is None:
            return []
        rect = _to_rect(str(raw).strip())
        if rect is None:
            return []
        item = {"id": uuid.uuid4().hex[:12], "name": "默认区域",
                "exe": "", "title": "", "layout": "auto",
                "rect": _rect_to_list(rect)}
        _write_raw([item])
        return [item]
    except Exception:                                  # noqa: BLE001
        return []


def load_profiles():
    """全部可用配置（list of dict，``rect`` 已是 QRect）。"""
    raw = _read_raw()
    if not raw:
        raw = _migrate_legacy()
    out = []
    for item in raw:
        n = _normalize(item)
        if n is not None:
            out.append(n)
    return out


def get_profile(pid):
    for p in load_profiles():
        if p["id"] == pid:
            return p
    return None


def save_profile(rect, exe="", title="", name="", layout="auto"):
    """新增或更新一条配置。

    同一 ``exe`` 再次框选视为「改区域」而不是「多一条」—— 否则医生每点
    一次设置区域就会多出一堆重复配置。``exe`` 为空时按 ``title`` 去重；
    两者都为空（探测失败）则总是新增。
    """
    if not _rect_ok(rect):
        return None
    exe = (exe or "").lower().strip()
    title = (title or "").strip()
    raw = _read_raw() or _migrate_legacy()

    same_key = False
    target = None
    for item in raw:
        iexe = str(item.get("exe") or "").lower().strip()
        ititle = str(item.get("title") or "").strip()
        if not exe and not title:
            # 纯区域存档（旧接口 save_region / 设置窗「重新设置」）：幂等更新
            same_key = (not iexe and not ititle)
        elif exe:
            same_key = (iexe == exe)
        else:
            same_key = (not iexe and ititle == title)
        if same_key:
            target = item
            break

    if target is None:
        target = {"id": uuid.uuid4().hex[:12]}
        raw.append(target)

    target.update({
        "exe": exe,
        "title": title,
        "layout": (layout or "auto"),
        "rect": _rect_to_list(rect),
        "name": (name or target.get("name") or exe or "识别区域"),
    })
    _write_raw(raw)
    return _normalize(target)


def match_profile(exe, title):
    """按当前前台窗口挑配置；挑不到返回 ``None``。

    匹配顺序：

    1. ``exe`` 完全相同 —— 唯一可靠的判据；
    2. ``exe`` 都为空时比标题（框选和刷新走同一软件，标题常常一字不差）；
    3. **只有一个配置时直接用它** —— 单软件用户（绝大多数）不该因为
       探测不到 exe 就退化成「未设置区域」。
    """
    profiles = load_profiles()
    if not profiles:
        return None

    exe = (exe or "").lower().strip()
    title = (title or "").strip()

    if exe:
        for p in profiles:
            if p["exe"] == exe:
                return p
    if title:
        for p in profiles:
            if p["title"] and p["title"] == title:
                return p
    if len(profiles) == 1:
        return profiles[0]
    return None


def remove_profile(pid):
    raw = _read_raw() or _migrate_legacy()
    keep = [p for p in raw if str(p.get("id")) != str(pid)]
    if len(keep) == len(raw):
        return False
    return _write_raw(keep)


def clear_profiles():
    """清空全部配置（设置窗「重新设置区域」用）。"""
    try:
        s = _settings()
        s.remove(KEY_PROFILES)
        s.remove(KEY_LEGACY)
        s.sync()
    except Exception:                                  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 旧接口（单区域）—— 保留给既有测试脚本与旧调用点，等价于「第一条配置」
# ---------------------------------------------------------------------------

def load_region():
    """第一条配置的区域；没有则 None。"""
    profiles = load_profiles()
    return profiles[0]["rect"] if profiles else None


def save_region(rect):
    """保存为「默认区域」（不绑定进程）。成功返回 True。"""
    return save_profile(rect, name="默认区域") is not None


def clear_region():
    """兼容旧名：等同 :func:`clear_profiles`。"""
    clear_profiles()


def region_text(rect):
    """人类可读的区域描述（日志/调试用；小窗已不展示尺寸）。"""
    if rect is None:
        return "未设置识别区域"
    return "区域 %d, %d · %d × %d" % (
        rect.x(), rect.y(), rect.width(), rect.height())
