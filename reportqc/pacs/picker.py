# -*- coding: utf-8 -*-
"""PACS 报告区域的屏幕框选工具。

用户在主程序里点「报告质控」后，本模块弹出**全屏半透明遮罩**，
鼠标拖拽画出一个矩形，松开即得到该区域在**屏幕全局坐标系**下的 QRect。
上层拿这个 QRect 去 :mod:`reportqc.pacs.capture` 截图。

实现要点
--------
1. **坐标一致性**：框选用的是 *逻辑像素*（``screen.geometry()``），
   截图用的 ``QScreen.grabWindow`` 也是逻辑像素（DPIAware 进程下二者相等），
   因此 QRect 可以直接传给 grabWindow，无需换算。若进程未声明
   DPI aware，Windows 会对 Qt 做 DPI 虚拟化，两边仍一致（都被虚拟化）。
   → **不引入 devicePixelRatio 换算**，避免 Qt5.15 在不同 DPI 下的坑。

2. **多显示器**：遮罩覆盖「鼠标当前所在屏幕」而不是所有屏幕拼接，
   坐标系用该 screen 的 geometry() 为基准，转全局坐标时加上其原点。

3. **Win7 兼容**：只用到 Qt5 基础 API（FramelessWindowHint /
   WindowStaysOnTopHint / WA_TranslucentBackground 在 Win7 上均可用）；
   不使用 Qt6 专有 API；开启 Aero 时半透明正常，关闭 Aero 时
   ``setWindowOpacity`` 退化为不透明遮罩（仍然可用，只是观感不同）。

4. **可用性细节**：ESC/右键取消、拖拽时显示选区尺寸、选区外加暗化、
   十字光标、选区过小（<8px）视为误触忽略。
"""

import PyQt5.QtCore as QtCore
import PyQt5.QtGui as QtGui
import PyQt5.QtWidgets as QtWidgets

__all__ = ["ScreenPicker", "pick_region"]

# 选区最小边长（像素），小于此视为误触
_MIN_SIZE = 8


class ScreenPicker(QtWidgets.QWidget):
    """全屏拖拽框选遮罩。用法：``picker = ScreenPicker(); picker.finished.connect(...); picker.show()``

    Signals
    -------
    finished(QRect or None):
        用户选定区域时发射屏幕全局坐标 QRect；取消/过小发射 ``None``。
    """

    finished = QtCore.pyqtSignal(object)

    def __init__(self, screen=None, parent=None):
        super().__init__(parent)
        self._screen = screen or self._current_screen()
        geo = self._screen.geometry()
        self._origin = QtCore.QPoint(geo.left(), geo.top())

        # 遮罩铺满当前屏幕
        self.setGeometry(geo)
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
        )
        # ⚠️ 不要加 Qt.WindowDoesNotAcceptFocus：它会让窗口拿不到键盘焦点，
        # 结果是 ESC 取消完全失效（实测：用户按 ESC 无任何反应，只能杀进程）。
        # 鼠标事件本就不依赖焦点，加这个 flag 只有坏处没有好处。
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, False)
        self.setWindowOpacity(0.32)          # 整体压暗，Win7 无 Aero 时退化不透明
        self.setCursor(QtGui.QCursor(QtCore.Qt.CrossCursor))
        self.setMouseTracking(True)
        # 模态抢占：框选期间不接受其他窗口的输入，避免误点到底下的 PACS
        self.setWindowModality(QtCore.Qt.WindowModal)

        self._dragging = False
        self._start = None                   # 全局坐标
        self._end = None                     # 全局坐标
        self._finished = False               # finished 是否已发射（防重入）

    # -- 内部工具 ---------------------------------------------------------

    @staticmethod
    def _current_screen():
        """鼠标当前所在的屏幕；拿不到就退回主屏。"""
        try:
            app = QtWidgets.QApplication.instance()
            if app is not None:
                pos = QtGui.QCursor.pos()
                for s in app.screens():
                    if s.geometry().contains(pos):
                        return s
        except Exception:                              # noqa: BLE001
            pass
        return QtWidgets.QApplication.primaryScreen()

    @staticmethod
    def _global_pt(event):
        """鼠标全局坐标（QPoint）。

        ⚠️ **Qt5 的 ``QMouseEvent.globalPos()`` 返回 QPoint**（QPointF 是
        Qt6 ``globalPosition()`` 的口径），所以**不能**写 ``.toPoint()``。
        2026-10-24 用户实测：那行 ``AttributeError`` 让 ``mouseReleaseEvent``
        的 ``_finish()`` 永不执行、``finished`` 信号不发射，表现为
        「框选卡住、无法退出」（错误日志见发行包根目录 error_log.txt）。
        这里做类型兼容：QPoint 直接用，QPointF 取整转换。
        """
        gp = event.globalPos()
        if isinstance(gp, QtCore.QPoint):
            return QtCore.QPoint(gp)
        return QtCore.QPoint(int(round(gp.x())), int(round(gp.y())))

    def _to_local(self, global_pt):
        return global_pt - self._origin

    # -- 事件 -------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._dragging = True
            self._start = self._global_pt(event)
            self._end = self._start
            self.update()
        elif event.button() == QtCore.Qt.RightButton:
            self._cancel()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._end = self._global_pt(event)
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        # 防御：任何异常都必须走 _finish()，保证 finished 必然发射一次，
        # 用户永远能退出遮罩（"卡住无法退出"结构性不可能再发生）。
        try:
            if event.button() == QtCore.Qt.LeftButton and self._dragging:
                self._dragging = False
                self._end = self._global_pt(event)
                rect = self._picked_rect()
                if rect is None or rect.width() < _MIN_SIZE or rect.height() < _MIN_SIZE:
                    # 区域过小 = 误触，当作取消处理（不弹任何提示）
                    self._finish(None)
                else:
                    self._finish(rect)
        except Exception:                              # noqa: BLE001
            self._finish(None)
        finally:
            try:
                super().mouseReleaseEvent(event)
            except Exception:                          # noqa: BLE001
                pass

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Escape,):
            self._finish(None)
        elif event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            rect = self._picked_rect()
            if rect is not None and rect.width() >= _MIN_SIZE \
                    and rect.height() >= _MIN_SIZE:
                self._finish(rect)
        else:
            super().keyPressEvent(event)

    def _finish(self, rect):
        """**唯一出口**：保证 ``finished`` 恰好发射一次，然后关闭窗口。

        为什么需要它
        ------------
        2026-10-24 实测：原来 ``mouseReleaseEvent`` 里先 ``close()`` 再
        ``emit()``，且 ESC / 右键 / 拖拽 / 回车**各自** emit 一次。窗口标志
        里又带了 ``WindowDoesNotAcceptFocus``，ESC 根本收不到 —— 用户表现为
        「框选卡住、按 ESC 没反应、无法退出」，只能任务管理器杀进程。

        统一走这里之后：任何退出路径都必然发射一次 ``finished``，
        调用方（无论阻塞式还是回调式）都能收到确定的结束信号。
        """
        if self._finished:
            return
        self._finished = True
        self._dragging = False
        try:
            self.close()
        except Exception:                              # noqa: BLE001
            pass
        self.finished.emit(rect)

    def _cancel(self):
        self._finish(None)

    def _picked_rect(self):
        if self._start is None or self._end is None:
            return None
        return QtCore.QRect(self._start, self._end).normalized()

    # -- 绘制 -------------------------------------------------------------

    def paintEvent(self, event):
        """选区外整体压暗（靠 windowOpacity），选区内恢复清晰 + 边框 + 尺寸提示。

        做法：先用遮罩色铺满，再用「与选区做 SrcOver 反向」的简单手段 ——
        直接 fillRect(遮罩色) 之后把选区区域用 **源像素回填**是 Qt 做不到的
        （拿不到背后的窗口内容）。所以这里换一个思路：

        窗口本身 opacity=0.32 只作为「未拖拽时的整体压暗」；一旦开始拖拽，
        就用一个**带洞的绘制**：在 paintEvent 里只画「选区之外」的四块矩形
        （颜色用比 opacity 更深的实色），选区内不画 → 透过遮罩看到的就是
        接近原样的 PACS 内容。这在 Win7 上同样有效。
        """
        painter = QtGui.QPainter(self)
        try:
            painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 110))

            if not self._dragging or self._start is None:
                #  idle 提示
                painter.setPen(QtGui.QColor(230, 230, 230))
                painter.drawText(
                    self.rect(), QtCore.Qt.AlignCenter,
                    "拖拽框选 PACS 报告区域\n（ESC / 右键取消）",
                )
                return

            rect = self._picked_rect()
            local = QtCore.QRect(
                self._to_local(rect.topLeft()), self._to_local(rect.bottomRight())
            )
            r = local.intersected(self.rect())
            if r.isNull():
                return

            # 选区内不画 → 透出下方 PACS 原貌
            outer = [
                QtCore.QRect(0, 0, self.width(), r.top()),                    # 上
                QtCore.QRect(0, r.bottom() + 1, self.width(),
                             self.height() - r.bottom() - 1),                 # 下
                QtCore.QRect(0, r.top(), r.left(), r.height()),               # 左
                QtCore.QRect(r.right() + 1, r.top(),
                             self.width() - r.right() - 1, r.height()),       # 右
            ]
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(0, 0, 0, 110))
            for q in outer:
                if not q.isEmpty():
                    painter.drawRect(q)

            # 选区边框（亮蓝，与主程序高亮色 #2a82da 一致）
            pen = QtGui.QPen(QtGui.QColor(42, 130, 218), 2)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRect(r)

            # 尺寸提示气泡
            tip = "%d × %d" % (r.width(), r.height())
            painter.setPen(QtGui.QColor(255, 255, 255))
            fm = QtGui.QFontMetrics(painter.font())
            tw = fm.width(tip) + 10
            th = fm.height() + 6
            tx = min(r.left() + 2, self.width() - tw - 2)
            ty = max(r.top() - th - 2, 2)
            painter.setBrush(QtGui.QColor(0, 0, 0, 190))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRoundedRect(tx, ty, tw, th, 3, 3)
            painter.setPen(QtGui.QColor(255, 255, 255))
            painter.drawText(tx + 5, ty + 3 + fm.ascent(), tip)
        finally:
            painter.end()


def pick_region(parent=None, timeout_ms=120000):
    """阻塞式便捷入口：弹出框选，返回 QRect 或 ``None``。

    内部起一个局部事件循环，等 ``finished`` 信号。带三重退出保障，
    任何一路通都不会卡死：

    1. ``finished`` 正常发射（用户框选完成 / ESC / 右键取消）；
    2. ``destroyed`` —— 窗口被外部销毁（例如主程序退出）；
    3. ``timeout_ms`` 超时兜底（默认 120 秒）。

    ⚠️ 注意：嵌套事件循环是「最后手段」。**主路径请用回调式** ——
    直接 ``ScreenPicker()`` + ``picker.finished.connect(...)``，
    见 :meth:`reportqc_bridge.ReportQCController.start_pick_and_check`。
    嵌套循环里若调用方再弹模态对话框，会出现「点了没反应」的假死。
    """
    picker = ScreenPicker(parent=parent)
    box = {"rect": None, "done": False}

    def _on_done(rect):
        box["rect"] = rect
        box["done"] = True

    picker.finished.connect(_on_done)
    picker.show()
    picker.raise_()
    picker.activateWindow()

    loop = QtCore.QEventLoop()
    picker.finished.connect(loop.quit)
    picker.destroyed.connect(loop.quit)
    # 超时兜底：即使 finished 因任何原因没发射，也不会永久卡死主程序
    timer = QtCore.QTimer(picker)
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(int(timeout_ms))
    loop.exec_()
    return box["rect"] if box["done"] else None
