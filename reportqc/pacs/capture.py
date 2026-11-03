# -*- coding: utf-8 -*-
"""PACS 报告区域的屏幕截图。

坐标口径与 :mod:`reportqc.pacs.picker` 严格对齐：**输入输出都是屏幕全局
逻辑像素 QRect**，中间不引入任何 DPI 换算（详见 picker 模块 docstring 第 1 条）。

为什么用 ``QScreen.grabWindow`` 而不是 win32 BitBlt
--------------------------------------------------
* grabWindow 是 Qt 官方跨平台 API，Win7/Win10/Win11 行为一致；
* BitBlt 要先拿目标窗口 HDC、处理 8/16/32 位色深与分层窗口，坑更多；
* 唯一已知限制：Win7 关闭 Aero 时，抓取**开启毛玻璃/半透明**的窗口会得到
  变形结果。PACS 报告编辑窗是实心窗口，不受影响。

高 DPI 说明
----------
进程若未声明 Per-Monitor DPI Aware，Windows 会对 Qt 做**系统级 DPI 虚拟化**，
此时 grabWindow 抓到的也是虚拟化后的像素，与 picker 的逻辑坐标依然一致
（两边都被虚拟化），所以**不需要**在模块内做任何换算。
"""

import PyQt5.QtCore as QtCore
import PyQt5.QtGui as QtGui
import PyQt5.QtWidgets as QtWidgets

__all__ = ["grab_rect", "pixmap_to_bgr", "ensure_dpi_aware"]


def ensure_dpi_aware():
    """把进程声明为 Per-Monitor DPI Aware（幂等，仅 Windows 有效）。

    必须在 **QApplication 构造之前**调用才生效；本函数对此做了保护 ——
    若 QApplication 已存在则直接返回（不报错），因为此时再设置已无意义，
    而 Qt5.15 默认开启 AA_EnableHighDpiScaling，行为仍然可预期。

    Win7 支持：``SetProcessDPIAware`` 自 Win7 SP1 / Vista 起可用，
    无需 shcore.dll（那是 Per-Monitor V2，Win8.1+ 才有）。
    """
    import sys as _sys
    if _sys.platform != "win32":
        return False
    if QtWidgets.QApplication.instance() is not None:
        return False                     # 已创建 QApplication，设置无效
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE（Win8.1+ 才是 Per-Monitor V2）
        # 这里用 2 在 Win7 上会失败，回退用 shcore.GetProcessDpiAwareness
        # 太复杂；直接试 SetProcessDPIAware（Vista/ Win7 可用，等于 system DPI aware）
        if user32.SetProcessDPIAware():
            return True
    except Exception:                              # noqa: BLE001
        pass
    return False


def _screen_for_rect(rect):
    """找到与 rect 重叠面积最大的屏幕；找不到返回主屏。"""
    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    best, best_area = None, -1
    for s in app.screens():
        inter = rect.intersected(s.geometry())
        area = max(0, inter.width()) * max(0, inter.height())
        if area > best_area:
            best, best_area = s, area
    return best or app.primaryScreen()


def grab_rect(rect):
    """截取屏幕全局坐标 ``rect``（QRect）→ QPixmap。

    失败/区域非法返回 ``None``。跨显示器区域按主重叠屏截取（PACS 窗口
    通常整体位于一个屏幕内，跨屏抓取本就无意义）。
    """
    if rect is None:
        return None
    if not isinstance(rect, QtCore.QRect):
        rect = QtCore.QRect(rect)
    rect = rect.normalized()
    if rect.width() <= 0 or rect.height() <= 0:
        return None

    app = QtWidgets.QApplication.instance()
    if app is None:
        return None

    screen = _screen_for_rect(rect)
    if screen is None:
        return None

    # 把全局坐标换算到该 screen 的窗口坐标（grabWindow 的 x/y 是
    # 相对「窗口 0 = 整个 screen」的坐标，即 screen 局部坐标）
    local = rect.translated(-screen.geometry().topLeft())
    try:
        pix = screen.grabWindow(
            0,                       # 0 = 桌面根窗口
            local.left(), local.top(),
            local.width(), local.height(),
        )
    except Exception:                          # noqa: BLE001
        return None
    return pix


def pixmap_to_bgr(pixmap):
    """QPixmap → ``numpy`` BGR ``uint8`` 数组（OCR 引擎的输入口径）。

    PP-OCR 训练时用的是 **BGR**（cv2 口径），传 RGB 会得到形近错字，
    这里显式做 RGB→BGR 交换。空输入返回 ``None``。
    """
    if pixmap is None or pixmap.isNull():
        return None
    try:
        import numpy as np                      # noqa: PLC0415
    except Exception:                           # noqa: BLE001
        return None

    img = pixmap.toImage()
    img = img.convertToFormat(QtGui.QImage.Format_RGB888)
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return None

    ptr = img.constBits()
    try:
        ptr.setsize(img.byteCount())
    except Exception:                           # noqa: BLE001
        return None
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape(h, img.bytesPerLine())
    # bytesPerLine 可能有行尾 padding，裁到 w*3
    arr = arr[:, : w * 3].reshape(h, w, 3)
    # RGB → BGR
    return arr[:, :, ::-1].copy()
