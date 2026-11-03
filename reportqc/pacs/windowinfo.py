# -*- coding: utf-8 -*-
"""前台窗口识别：判断「用户此刻在看哪个软件」。

为什么需要
----------
医生同时用两套写报告界面（体检登记界面、标准 PACS 报告界面），两块
识别区域完全不同。要求是「各框一次，之后点刷新自动用对的那块」，
所以每次刷新都必须知道前台是哪个程序。

为什么不能「刷新时现取前台窗口」
--------------------------------
用户是**点悬浮小窗的按钮**触发刷新的 —— 那一刻前台窗口就是小窗自己。
现取必然拿到 ``pythonw.exe``，两个区域都匹配不上。

因此本模块提供 :class:`ForegroundWatcher`：一个 1 秒轮询的低成本监听器，
只记录「**最近一个不属于本进程**的前台窗口」。点小窗时它自然保留着
上一步用户所在的 PACS / 体检软件。

Win7 兼容
---------
只用 ``user32.GetForegroundWindow`` / ``GetWindowTextW`` /
``GetWindowThreadProcessId`` 与 ``psapi.GetModuleBaseNameW``，全部
XP 起可用，不依赖 shcore / DWM。取不到 exe（权限不足或被保护的进程）
时退化为空串，匹配逻辑会退到窗口标题。
"""

import os
import sys

__all__ = ["foreground", "own_exe", "ForegroundWatcher", "available"]

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010


def available():
    """当前平台能否做前台窗口探测（仅 Windows）。"""
    return sys.platform == "win32"


def own_exe():
    """本进程的 exe 文件名（小写，用于排除自己）。"""
    try:
        return os.path.basename(sys.executable).lower()
    except Exception:                                  # noqa: BLE001
        return ""


def _exe_of_pid(pid):
    """pid → exe 文件名（小写）。失败返回空串。"""
    try:
        import ctypes
        from ctypes import wintypes
        psapi = ctypes.WinDLL("psapi.dll")
        kernel32 = ctypes.WinDLL("kernel32.dll")

        h = kernel32.OpenProcess(
            _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = psapi.GetModuleBaseNameW(h, None, buf, 1024)
            if not n:
                return ""
            return buf.value.strip().lower()
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h))
    except Exception:                                  # noqa: BLE001
        return ""


def foreground():
    """返回当前前台窗口 ``(exe, title)``；拿不到时对应项为空串。

    绝不在异常时抛错 —— 这是每秒都可能被调度的探测，任何异常都不该
    影响主程序。
    """
    if not available():
        return "", ""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32.dll")

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", ""

        title = ""
        try:
            tbuf = ctypes.create_unicode_buffer(512)
            if user32.GetWindowTextW(wintypes.HWND(hwnd), tbuf, 512):
                title = tbuf.value.strip()
        except Exception:                              # noqa: BLE001
            title = ""

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd),
                                        ctypes.byref(pid))
        exe = _exe_of_pid(pid.value) if pid.value else ""
        return exe, title
    except Exception:                                  # noqa: BLE001
        return "", ""


class ForegroundWatcher(object):
    """记住「最近一个不是本进程的前台窗口」。

    用法：``w = ForegroundWatcher(); w.start(); ... w.last()``。
    不继承 QObject 以免把 Qt 依赖带进纯逻辑层；定时器由调用方
    （``reportqc_bridge``）持有并在回调里调 :meth:`poll`。

    为什么不用 QObject + QTimer 自己管
    ----------------------------------
    本模块刻意**不 import PyQt5**：``_release_selfcheck.py`` 在没有
    QApplication 的环境里也会 import 它，一旦这里带 Qt 控件依赖，
    自检就会 Qt abort（详见技术说明 v1.18.2 的踩坑表）。
    """

    def __init__(self):
        self._exe = ""
        self._title = ""
        self._self = own_exe()

    def poll(self):
        """采样一次前台窗口。命中外部窗口才更新缓存。"""
        exe, title = foreground()
        if not exe and not title:
            return
        if self._self and exe == self._self:
            return                       # 就是本程序（用户点了小窗/主窗）
        if not title and not exe:
            return
        self._exe, self._title = exe, title

    def last(self):
        """最近一个外部前台窗口 ``(exe, title)``；从没采到过则为空。"""
        return self._exe, self._title

    def reset(self):
        self._exe = ""
        self._title = ""

    def has_value(self):
        return bool(self._exe or self._title)
