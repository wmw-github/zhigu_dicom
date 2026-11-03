# -*- coding: utf-8 -*-
"""科技感加载动画组件（CT3D v1.9.2）

程序里所有「等一下」的地方原本都是系统 QProgressDialog：灰底白条、样式
跟深色界面格格不入，也看不出程序是在干活还是卡死了。这里换成一套自绘的
HUD 风格组件：

  1. ``TechSpinner``    —— 自绘旋转指示器（拖尾弧 + 反向内环 + 呼吸核
                           + 轨道光点），支持「不确定/确定进度」两种模式
  2. ``TechBusyDialog`` —— 替代 QProgressDialog 的模态浮层，接口与之兼容
                           （setLabelText / setValue / setRange / canceled …）
  3. ``BusyOverlay``    —— 贴在任意控件上的内嵌遮罩（网格 + 扫描线 + 角标）
  4. ``SpinnerRow``     —— 一行「小 spinner + 文字」，塞进已有面板布局

**动画为什么有时候会停**：所有动画由 QTimer 在*主线程*驱动。真正耗时的活
（DICOM 读取、患者扫描、三维管线、AI 推理）本来就跑在 QThread 上，主线程
是空的，动画满帧跑；少数同步重操作（切阈值、构建三维视图）会占住主线程，
此时动画停在最后一帧 —— 这是 Qt 单线程绘制的硬限制，面板本身仍然比系统
进度框好看，但别指望它在阻塞期间继续流转。
"""

import math

from PyQt5 import QtCore, QtGui, QtWidgets

# ---- 配色 ----
# 这里的常量只是「theme 取不到色」时的兜底（例如单独 import 本模块做离线
# 截图/单测）。正常运行时一律走 _hud() 向 theme 现取，这样切皮肤后下一次
# 重绘就是新配色，浅色皮肤下也不会出现一块深色孤岛。
PRIMARY = "#38bdf8"        # 主色（兜底）
SECONDARY = "#7b68ee"      # 副色（兜底）
BG_PANEL = "#0d1117"       # 面板底色（兜底）
BG_OVERLAY = "#080b0f"     # 遮罩底色（兜底）
TEXT_MAIN = "#e6edf3"
TEXT_DIM = "#8b949e"

GRID_STEP = 16             # 背景网格间距（像素）
FPS = 30


def _hud():
    """向 theme 取当前皮肤的 HUD 配色（theme 不可用时退回上面的常量）"""
    try:
        import theme
        return theme.hud()
    except Exception:          # noqa: BLE001
        return {"panel": BG_PANEL, "overlay": BG_OVERLAY, "text": TEXT_MAIN,
                "dim": TEXT_DIM, "primary": PRIMARY,
                "secondary": SECONDARY, "dark": True}



def _role(widget, name):
    """给控件打皮肤语义角色（theme 不可用时无声跳过，保持旧观感）"""
    try:
        import theme
        theme.role(widget, name)
    except Exception:          # noqa: BLE001
        pass


def _c(color, alpha=1.0):
    """QColor + alpha 的简写"""
    col = QtGui.QColor(color)
    col.setAlphaF(max(0.0, min(1.0, float(alpha))))
    return col


def _mono_font(size=10, bold=False):
    f = QtGui.QFont("Consolas, Microsoft YaHei UI, SimHei")
    f.setPointSize(size)
    f.setBold(bold)
    return f


def _ui_font(size=10, bold=False):
    f = QtGui.QFont("Microsoft YaHei UI, SimHei")
    f.setPointSize(size)
    f.setBold(bold)
    return f


def _paint_hud(painter, rect, radius=14.0, scan=0.0, fill_alpha=0.94,
               base=None):
    """画一块 HUD 风格底板：圆角玻璃 + 网格 + 扫描线 + 四角角标 + 顶边高亮

    ``scan`` 为 0~1 的相位（由调用方按时间推进），控制扫描线的纵向位置。
    ``base`` 为空时取当前皮肤的面板色（浅色皮肤下就是一块「霜白玻璃」）。
    """
    pal = _hud()
    pri = pal["primary"]
    if base is None:
        base = pal["panel"]
    path = QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(rect), radius, radius)

    painter.save()
    painter.setClipPath(path)

    # 底
    painter.fillPath(path, _c(base, fill_alpha))

    # 网格
    painter.setPen(QtGui.QPen(_c(pri, 0.065), 1.0))
    x = rect.left()
    while x <= rect.right():
        painter.drawLine(QtCore.QPointF(x, rect.top()),
                         QtCore.QPointF(x, rect.bottom()))
        x += GRID_STEP
    y = rect.top()
    while y <= rect.bottom():
        painter.drawLine(QtCore.QPointF(rect.left(), y),
                         QtCore.QPointF(rect.right(), y))
        y += GRID_STEP

    # 扫描线（一条上下循环的亮带）
    if scan:
        sy = rect.top() + (scan % 1.0) * rect.height()
        grad = QtGui.QLinearGradient(0.0, sy - 34.0, 0.0, sy + 34.0)
        grad.setColorAt(0.0, _c(pri, 0.0))
        grad.setColorAt(0.5, _c(pri, 0.14))
        grad.setColorAt(1.0, _c(pri, 0.0))
        painter.fillRect(QtCore.QRectF(rect.left(), sy - 34.0,
                                       rect.width(), 68.0), grad)

    # 顶部高亮边
    painter.fillRect(QtCore.QRectF(rect.left() + radius, rect.top(),
                                   rect.width() - 2.0 * radius, 2.0),
                     _c(pri, 0.7))

    # 四角角标（科技面板的经典装饰）
    m = 6.0
    L = 14.0
    painter.setPen(QtGui.QPen(_c(pri, 0.55), 1.6))
    corners = (
        (rect.left() + m, rect.top() + m, 1, 1),
        (rect.right() - m, rect.top() + m, -1, 1),
        (rect.left() + m, rect.bottom() - m, 1, -1),
        (rect.right() - m, rect.bottom() - m, -1, -1),
    )
    for cx, cy, dx, dy in corners:
        painter.drawLine(QtCore.QPointF(cx, cy),
                         QtCore.QPointF(cx + L * dx, cy))
        painter.drawLine(QtCore.QPointF(cx, cy),
                         QtCore.QPointF(cx, cy + L * dy))

    painter.restore()

    # 边框（不放进 clip，保证 1px 不被裁掉）
    painter.setPen(QtGui.QPen(_c(pri, 0.38), 1.2))
    painter.drawRoundedRect(QtCore.QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
                            radius, radius)


# ---------------------------------------------------------------------------
# 1. 旋转指示器
# ---------------------------------------------------------------------------
class TechSpinner(QtWidgets.QWidget):
    """自绘科技感旋转指示器。

    ``set_value(-1)`` → 不确定模式（拖尾弧 + 呼吸核）；
    ``set_value(0~100)`` → 确定模式（外圈进度弧 + 中心百分比数字）。
    """

    BUSY = -1

    def __init__(self, parent=None, size=64, primary=None,
                 secondary=None, fps=FPS, show_percent=False):
        super().__init__(parent)
        # None = 跟随皮肤（每帧现取），传入具体色则固定用该色
        self._primary = None if primary is None else QtGui.QColor(primary)
        self._secondary = None if secondary is None else QtGui.QColor(secondary)
        self._show_percent = bool(show_percent)
        self._value = self.BUSY
        self._t = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.set_fps(fps)
        self.set_size(size)
        # 不吃鼠标事件：遮罩上的点击仍然落到被覆盖的控件上
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

    # ---- 外部接口 ----
    def set_size(self, size):
        self._size = int(size)
        self.setFixedSize(self._size, self._size)
        self.update()

    def spinner_size(self):
        """注意：不能叫 size()——那会顶掉 QWidget.size()，布局会拿错尺寸"""
        return self._size

    def set_fps(self, fps):
        self._fps = max(8, int(fps))
        self._timer.setInterval(int(1000.0 / self._fps))

    def set_value(self, v):
        """-1=不确定；0~100=确定进度"""
        self._value = int(v)
        self.update()

    def value(self):
        return self._value

    def set_busy(self):
        self.set_value(self.BUSY)

    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()

    # ---- Qt 事件 ----
    def showEvent(self, ev):
        self.start()
        super().showEvent(ev)

    def hideEvent(self, ev):
        self.stop()
        super().hideEvent(ev)

    def _tick(self):
        self._t += 1.0 / self._fps
        if self._t > 1.0e6:
            self._t = 0.0
        self.update()

    def _colors(self):
        """(当前皮肤 HUD 配色, 主色, 副色)——每帧现取，切皮肤立即跟随"""
        pal = _hud()
        prim = self._primary or QtGui.QColor(pal["primary"])
        sec = self._secondary or QtGui.QColor(pal["secondary"])
        return pal, prim, sec

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        r = min(w, h) / 2.0 - 4.0
        if r <= 2:
            p.end()
            return

        box = lambda rr: QtCore.QRectF(cx - rr, cy - rr, 2.0 * rr, 2.0 * rr)

        pal, prim, sec = self._colors()
        dark = pal.get("dark", True)
        # 深色皮肤上用白色做高光；浅色皮肤上白色高光等于消失，改用主色
        core = "#ffffff" if dark else pal["primary"]

        # 轨道底环（浅色皮肤下白色环看不见，改用文字色的极低透明度）
        p.setPen(QtGui.QPen(_c("#ffffff", 0.07) if dark
                            else _c(pal["text"], 0.10),
                            max(1.5, r * 0.075)))
        p.drawEllipse(box(r))

        if self._value == self.BUSY:
            self._paint_busy(p, cx, cy, r, box, prim, sec, core, pal)
        else:
            self._paint_progress(p, cx, cy, r, box, prim, sec, core, pal)

        p.end()

    def _paint_busy(self, p, cx, cy, r, box, prim, sec, core, pal=None):
        t = self._t
        # 外层拖尾弧：顺时针扫，尾部逐段变淡变细
        head = -(t * 320.0) % 360.0
        seg, step = 22, 8.0
        for i in range(seg):
            k = 1.0 - i / float(seg)
            a = pow(k, 1.7)
            start = head - (i + 1) * step
            p.setPen(QtGui.QPen(_c(prim, 0.10 + 0.90 * a),
                                max(0.6, r * 0.11 * (0.35 + 0.65 * a))))
            p.drawArc(box(r), start * 16.0, step * 16.0 + 1.0)

        # 内层反向弧（副色，转速更快，形成层次）
        r2 = r * 0.66
        head2 = (t * 470.0) % 360.0
        seg2, step2 = 14, 7.0
        for i in range(seg2):
            k = 1.0 - i / float(seg2)
            a = pow(k, 2.0)
            start = head2 + i * step2
            p.setPen(QtGui.QPen(_c(sec, 0.08 + 0.75 * a),
                                max(0.5, r * 0.075 * (0.3 + 0.7 * a))))
            p.drawArc(box(r2), start * 16.0, step2 * 16.0 + 1.0)

        # 外圈呼吸脉冲
        pulse = (math.sin(t * 2.0 * math.pi * 0.55) + 1.0) / 2.0
        p.setPen(QtGui.QPen(_c(prim, 0.05 + 0.22 * pulse), 1.2))
        p.drawEllipse(box(r + 2.5 + 3.0 * pulse))

        # 呼吸核心（多层同心圆模拟发光）
        cr = r * 0.17 * (1.0 + 0.28 * math.sin(t * 2.0 * math.pi * 0.9))
        for i in range(4):
            rr = cr * (1.0 + i * 0.85)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(_c(prim, 0.30 / (i + 1.0)))
            p.drawEllipse(box(rr))
        p.setBrush(_c(core, 0.92))
        p.drawEllipse(box(cr * 0.55))

        # 轨道光点（三颗，速度/半径各不相同）
        for j, (rr_, sp, off, col) in enumerate((
                (r * 0.90, 150.0, 0.0, prim),
                (r * 0.90, 150.0, 120.0, prim),
                (r * 0.90, 150.0, 240.0, sec),
        )):
            ang = math.radians(-(t * sp + off) % 360.0)
            px = cx + rr_ * math.cos(ang)
            py = cy + rr_ * math.sin(ang)
            p.setBrush(_c(col, 0.85))
            p.setPen(QtCore.Qt.NoPen)
            p.drawEllipse(QtCore.QRectF(px - 1.8, py - 1.8, 3.6, 3.6))

    def _paint_progress(self, p, cx, cy, r, box, prim, sec, core, pal=None):
        pct = max(0.0, min(100.0, float(self._value)))
        # 进度弧：从 12 点起顺时针
        span = -pct * 3.6
        p.setPen(QtGui.QPen(_c(prim, 0.95), max(2.0, r * 0.12)))
        p.drawArc(box(r), 90.0 * 16.0, span * 16.0)
        # 弧头亮点
        ang = math.radians(90.0 - pct * 3.6)
        hx = cx + r * math.cos(ang)
        hy = cy - r * math.sin(ang)
        p.setBrush(_c(core, 0.9))
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(QtCore.QRectF(hx - 2.4, hy - 2.4, 4.8, 4.8))

        if self._show_percent and r >= 16:
            p.setPen(_c((pal or _hud())["text"], 0.95))
            p.setFont(_mono_font(max(7, int(r * 0.34)), bold=True))
            p.drawText(box(r), QtCore.Qt.AlignCenter, "%d" % int(round(pct)))


# ---------------------------------------------------------------------------
# 2. 底部流光条
# ---------------------------------------------------------------------------
class FlowBar(QtWidgets.QWidget):
    """细长进度条：确定模式填充，不确定模式一段高亮左右流动"""

    def __init__(self, parent=None, height=4, busy=True, value=0):
        super().__init__(parent)
        self._busy = bool(busy)
        self._value = float(value)
        self._t = 0.0
        self.setFixedHeight(height)
        self.setMinimumWidth(60)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(1000 // FPS)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.update()

    def set_value(self, v):
        self._value = max(0.0, min(100.0, float(v)))
        self.update()

    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()

    def showEvent(self, ev):
        self.start()
        super().showEvent(ev)

    def hideEvent(self, ev):
        self.stop()
        super().hideEvent(ev)

    def _tick(self):
        self._t += 1.0 / FPS
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        pal = _hud()
        pri, sec = pal["primary"], pal["secondary"]
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(_c(pri, 0.13))
        p.drawRoundedRect(QtCore.QRectF(0, 0, w, h), h / 2.0, h / 2.0)

        if self._busy:
            # 不确定：一段 40% 宽的高亮来回流动
            span = w * 0.40
            pos = (math.sin(self._t * 2.0 * math.pi * 0.42) + 1.0) / 2.0
            x = pos * (w - span)
            g = QtGui.QLinearGradient(x, 0.0, x + span, 0.0)
            g.setColorAt(0.0, _c(sec, 0.25))
            g.setColorAt(0.45, _c(pri, 0.95))
            g.setColorAt(1.0, _c(pri, 0.0))
            p.setBrush(g)
            p.drawRoundedRect(QtCore.QRectF(x, 0, span, h), h / 2.0, h / 2.0)
        else:
            fw = max(0.0, w * self._value / 100.0)
            if fw > 0.5:
                # 实色而非「副色→主色」渐变：跨色相渐变在两个颜色之间会
                # 混出一段发灰的中间色（浅色皮肤下尤其明显）
                p.setBrush(_c(pri, 1.0))
                p.drawRoundedRect(QtCore.QRectF(0, 0, fw, h), h / 2.0, h / 2.0)
        p.end()


# ---------------------------------------------------------------------------
# 3. 模态忙碌浮层（替代 QProgressDialog）
# ---------------------------------------------------------------------------
class TechBusyDialog(QtWidgets.QDialog):
    """HUD 风格忙碌浮层，接口与 QProgressDialog 保持一致。

    典型用法（与原来完全同构，可原地替换）::

        dlg = TechBusyDialog("正在加载DICOM数据...", "取消", self)
        dlg.setWindowTitle("请稍候")
        dlg.setWindowModality(QtCore.Qt.WindowModal)
        dlg.setRange(0, 100)
        dlg.setValue(10)
        dlg.show()
        ...
        dlg.canceled.connect(do_cancel)
        ...
        dlg.close()
    """

    canceled = QtCore.pyqtSignal()

    def __init__(self, text="", cancel_text=None, parent=None, title="",
                 spinner_size=68):
        super().__init__(parent)
        self._title = str(title or "")
        self._min_duration = 0
        self._value = 0
        self._min = 0
        self._max = 0            # QProgressDialog 默认 (0,0) = 忙碌
        self._canceled = False
        self._t = 0.0
        self._dots = 0

        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.Dialog
            | QtCore.Qt.NoDropShadowWindowHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setModal(True)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(12)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(16)
        self._spinner = TechSpinner(self, size=spinner_size,
                                    show_percent=False)
        row.addWidget(self._spinner, 0, QtCore.Qt.AlignVCenter)

        col = QtWidgets.QVBoxLayout()
        col.setSpacing(4)
        self._title_label = QtWidgets.QLabel(self._title)
        self._title_label.setFont(_ui_font(9, bold=True))
        _role(self._title_label, "accent")
        self._title_label.setVisible(bool(self._title))
        col.addWidget(self._title_label)

        self._text_label = QtWidgets.QLabel(str(text or ""))
        self._text_label.setFont(_ui_font(11))
        self._text_label.setWordWrap(True)
        self._text_label.setMinimumWidth(230)
        self._text_label.setMaximumWidth(340)
        col.addWidget(self._text_label)

        self._pct_label = QtWidgets.QLabel("")
        self._pct_label.setFont(_mono_font(9))
        _role(self._pct_label, "muted")
        self._pct_label.setVisible(False)
        col.addWidget(self._pct_label)

        row.addLayout(col, 1)
        outer.addLayout(row)

        self._bar = FlowBar(self, height=4, busy=True)
        outer.addWidget(self._bar)

        self._cancel_btn = None
        if cancel_text:
            btn_row = QtWidgets.QHBoxLayout()
            btn_row.addStretch(1)
            self._cancel_btn = QtWidgets.QPushButton(str(cancel_text))
            self._cancel_btn.setCursor(QtCore.Qt.PointingHandCursor)
            self._cancel_btn.setFixedHeight(26)
            self._cancel_btn.setStyleSheet("padding: 2px 14px;")
            _role(self._cancel_btn, "ghost")
            self._cancel_btn.clicked.connect(self._on_cancel_clicked)
            btn_row.addWidget(self._cancel_btn)
            outer.addLayout(btn_row)

        # 计时器：驱动背景扫描线 + 文字跳点
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(1000 // FPS)

        # setMinimumDuration 的延迟显示支持
        self._delay = QtCore.QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.timeout.connect(self._delayed_show)

    # ---- 兼容 QProgressDialog 的接口 ----
    def setWindowTitle(self, title):
        self._title = str(title or "")
        self._title_label.setText(self._title)
        self._title_label.setVisible(bool(self._title))
        super().setWindowTitle(self._title)

    def setLabelText(self, text):
        self._text_label.setText(str(text or ""))

    def labelText(self):
        return self._text_label.text()

    def setMinimumDuration(self, ms):
        self._min_duration = int(ms or 0)

    def setRange(self, lo, hi):
        self._min, self._max = int(lo), int(hi)
        busy = (self._max <= self._min)
        if busy:
            self._spinner.set_busy()
        else:
            self._spinner.set_value(self._value)
        self._bar.set_busy(busy)
        self._pct_label.setVisible(not busy)
        self._sync_pct()

    def setMinimum(self, lo):
        self.setRange(int(lo), self._max)

    def setMaximum(self, hi):
        self.setRange(self._min, int(hi))

    def minimum(self):
        return self._min

    def maximum(self):
        return self._max

    def setValue(self, v):
        self._value = int(v)
        busy = (self._max <= self._min)
        if busy:
            self._spinner.set_busy()
        else:
            span = float(self._max - self._min) or 1.0
            self._spinner.set_value(
                (self._value - self._min) * 100.0 / span)
            self._bar.set_value((self._value - self._min) * 100.0 / span)
        self._sync_pct()

    def value(self):
        return self._value

    def wasCanceled(self):
        return self._canceled

    def setCancelButtonText(self, text):
        if self._cancel_btn is not None:
            self._cancel_btn.setText(str(text))

    # ---- 显示控制 ----
    #
    # 坑（实测直接段错误，exit 127）：QWidget.setVisible 是**虚函数**，Qt
    # 内部的 show()/hide() 就是靠调它实现的。若在 setVisible 里再调
    # self.show()/self.hide()，就会 show → setVisible → show → … 无限递归
    # 爆栈。所以 show/hide/setVisible 三个入口必须全部收口到 _show_impl /
    # _hide_impl，而这两个只准调 super().setVisible()（C++ 实现，不会再
    # 回到 Python）。
    def show(self):
        self._show_impl()

    def setVisible(self, visible):
        if visible:
            self._show_impl()
        else:
            self._hide_impl()

    def hide(self):
        self._hide_impl()

    def close(self):
        self._hide_impl()
        super().close()

    def _show_impl(self):
        if self._min_duration > 0 and not self.isVisible():
            self._delay.start(self._min_duration)
            return
        super().setVisible(True)

    def _delayed_show(self):
        super().setVisible(True)

    def _hide_impl(self):
        self._delay.stop()
        self._timer.stop()
        super().setVisible(False)

    def reject(self):
        """Esc 等同于点取消（与 QProgressDialog 保持一致）"""
        self._canceled = True
        self.canceled.emit()
        super().reject()

    def _on_cancel_clicked(self):
        self._canceled = True
        self.canceled.emit()
        self.close()

    def _sync_pct(self):
        busy = (self._max <= self._min)
        if busy:
            self._pct_label.setText("")
            return
        span = float(self._max - self._min) or 1.0
        pct = (self._value - self._min) * 100.0 / span
        self._pct_label.setText("%3d%%" % int(round(pct)))

    def _tick(self):
        self._t += 1.0 / FPS
        # 忙碌时文字尾部跳动的省略号 —— 让「程序还活着」这件事可见
        if self._max <= self._min and int(self._t * FPS) % 15 == 0:
            self._dots = (self._dots + 1) % 4
            base = self._text_label.text().rstrip(".· ")
            self._text_label.setText(base + "." * self._dots)
        self.update()

    def showEvent(self, ev):
        self._timer.start()
        par = self.parent()
        if par is not None:
            self.adjustSize()
            try:
                gc = par.mapToGlobal(par.rect().center())
                self.move(gc.x() - self.width() // 2,
                          gc.y() - self.height() // 2)
            except Exception:      # noqa: BLE001
                pass
        super().showEvent(ev)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        _paint_hud(p, rect, radius=14.0, scan=self._t * 0.45, fill_alpha=0.95)
        p.end()


# ---------------------------------------------------------------------------
# 4. 贴附式遮罩（非模态，覆盖在任意控件上）
# ---------------------------------------------------------------------------
class BusyOverlay(QtWidgets.QWidget):
    """覆盖在目标控件上的科技感遮罩。

    与 ``TechBusyDialog`` 的区别：它不抢焦点、不弹窗，只是把目标控件盖住，
    适合「同步阻塞的短操作」和「后台线程跑、界面照常可用」的场景。
    """

    def __init__(self, target, text="加载中...", spinner_size=54,
                 dim=0.72):
        super().__init__(target)
        self._target = target
        self._dim = float(dim)
        self._t = 0.0

        lay = QtWidgets.QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)

        self._spinner = TechSpinner(self, size=spinner_size)
        lay.addWidget(self._spinner, 0, QtCore.Qt.AlignHCenter)

        self._label = QtWidgets.QLabel(str(text or ""))
        self._label.setFont(_ui_font(11, bold=True))
        _role(self._label, "title")
        self._label.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
        self._label.setWordWrap(True)
        lay.addWidget(self._label, 0, QtCore.Qt.AlignHCenter)

        self._sub = QtWidgets.QLabel("")
        self._sub.setFont(_mono_font(9))
        _role(self._sub, "muted")
        self._sub.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
        self._sub.setVisible(False)
        lay.addWidget(self._sub, 0, QtCore.Qt.AlignHCenter)

        lay.addStretch(1)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(1000 // FPS)

        target.installEventFilter(self)
        self._follow()
        self.hide()

    # ---- 接口 ----
    def set_text(self, text):
        self._label.setText(str(text or ""))

    def set_sub_text(self, text):
        text = str(text or "")
        self._sub.setText(text)
        self._sub.setVisible(bool(text))

    def text(self):
        """当前文案（busy_ancestor_text 用它回传文案）"""
        try:
            return str(self._label.text() or "")
        except Exception:          # noqa: BLE001
            return ""

    def target(self):
        return self._target

    # ---- Qt 事件 ----
    def eventFilter(self, obj, ev):
        if obj is self._target and ev.type() in (
                QtCore.QEvent.Resize, QtCore.QEvent.Show,
                QtCore.QEvent.WindowStateChange):
            self._follow()
        return False

    def _follow(self):
        try:
            self.setGeometry(0, 0, self._target.width(), self._target.height())
            self.raise_()
        except Exception:          # noqa: BLE001
            pass

    def _tick(self):
        self._t += 1.0 / FPS
        self.update()

    def showEvent(self, ev):
        self._follow()
        self._timer.start()
        super().showEvent(ev)

    def hideEvent(self, ev):
        self._timer.stop()
        super().hideEvent(ev)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        _paint_hud(p, rect, radius=10.0, scan=self._t * 0.5,
                   fill_alpha=self._dim, base=_hud()["overlay"])
        p.end()


# ---------------------------------------------------------------------------
# 5. 一行式「小 spinner + 文字」（塞进已有面板布局）
# ---------------------------------------------------------------------------
class SpinnerRow(QtWidgets.QWidget):
    """小尺寸 spinner + 说明文字，用于面板内部的忙碌指示"""

    def __init__(self, text="处理中...", parent=None, size=20, fps=24):
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        self._spinner = TechSpinner(self, size=size, fps=fps)
        lay.addWidget(self._spinner, 0, QtCore.Qt.AlignVCenter)
        self._label = QtWidgets.QLabel(str(text or ""))
        self._label.setFont(_ui_font(10))
        _role(self._label, "accent")
        lay.addWidget(self._label, 0, QtCore.Qt.AlignVCenter)
        lay.addStretch(1)
        self.hide()

    def set_text(self, text):
        self._label.setText(str(text or ""))

    def text(self):
        return self._label.text()


# ---------------------------------------------------------------------------
# 6. 轻提示（Toast）
# ---------------------------------------------------------------------------
# 用途：把「操作完成」这类**不需要用户确认**的消息从 QMessageBox 里换出来。
# 原生 QMessageBox 会模态阻塞、必须点「确定」，在「刷新患者列表」这种
# 高频操作上很烦人。Toast 具备：
#   * 不抢焦点、不阻塞事件循环（用 QTimer 自动消失）
#   * 贴在父控件顶部居中，随父控件移动/缩放
#   * 支持 info / success / warning / error 四种配色
#   * 父控件上只保留一个 Toast，新的会把旧的顶掉（不堆积）
#
# Win7 注意：只用 QTimer + QWidget，不依赖 QPropertyAnimation 之类的新 API。

class Toast(QtWidgets.QWidget):
    """一条自动消失的轻提示。

    全部内容**自绘**（QPainter），不用 QLabel 子控件 —— 因为淡入淡出是
    靠 ``painter.setOpacity()`` 实现的，而子控件不受父级 painter 透明度
    影响，用 QLabel 会出现「背景在淡入、字却一直全亮」的割裂感。
    """

    # 兜底色（theme 不可用时用）。正常走 _toast_colors() 取皮肤语义色：
    # 浅色皮肤下若还画深色玻璃块，会跟整个界面割裂。
    _STYLES = {
        "info":    (PRIMARY, PRIMARY, TEXT_MAIN),
        "success": ("#3fb950", "#3fb950", TEXT_MAIN),
        "warning": ("#d29922", "#d29922", TEXT_MAIN),
        "error":   ("#f85149", "#f85149", "#ffd7d5"),
    }

    PAD_X = 16
    PAD_Y = 10
    BAR_W = 3
    BAR_GAP = 11
    LINE_H = 20

    def __init__(self, parent, text="", kind="info", duration=2600,
                 max_width=520):
        super().__init__(parent)
        self._kind = kind if kind in self._STYLES else "info"
        self._duration = int(duration)
        self._opacity = 0.0
        self._t = 0.0
        self._closing = False
        self._text = str(text or "")
        self._max_text_w = int(max_width)

        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(QtCore.Qt.NoFocus)

        self._font = _ui_font(10, bold=True)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(33)

        self._closer = QtCore.QTimer(self)
        self._closer.setSingleShot(True)
        self._closer.timeout.connect(self._fade_out)

        self._resize_to_text()

    def _toast_colors(self, kind=None):
        """(主色, 竖条色, 文字色)——每种语义色都来自当前皮肤"""
        kind = kind or self._kind
        try:
            import theme
            t = theme.tokens()
        except Exception:          # noqa: BLE001
            return self._STYLES[kind]
        bar = {"success": t["success"], "warning": t["warning"],
               "error": t["danger"]}.get(kind, t["accent"])
        return (bar, bar, t["text"])

    # ---- 文本与尺寸 ----
    def setText(self, text):
        self._text = str(text or "")
        self._resize_to_text()
        self.update()

    def _text_width(self, fm, s):
        """文本宽度（兼容 Qt 5.11 之前只有 width() 的老版本）。"""
        adv = getattr(fm, "horizontalAdvance", None)
        try:
            if adv is not None:
                return adv(s)
        except Exception:          # noqa: BLE001
            pass
        return fm.width(s)

    def _text_lines(self, width):
        """按可用宽度把文本折成若干行（按字符宽度估算，兼容无字体环境）。"""
        fm = QtGui.QFontMetrics(self._font)
        lines = []
        for para in self._text.split("\n"):
            if not para:
                lines.append("")
                continue
            cur = ""
            for ch in para:
                if self._text_width(fm, cur + ch) > width and cur:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            lines.append(cur)
        return lines

    def _resize_to_text(self):
        """按文本内容计算并设置控件尺寸。"""
        text_w = max(120, self._max_text_w)
        lines = self._text_lines(text_w)
        # 实际最宽的一行
        fm = QtGui.QFontMetrics(self._font)
        widest = 0
        for ln in lines:
            widest = max(widest, self._text_width(fm, ln))
        used_w = min(text_w, max(120, widest))
        lines = self._text_lines(used_w)
        w = self.PAD_X + self.BAR_W + self.BAR_GAP + used_w + self.PAD_X
        h = self.PAD_Y * 2 + max(1, len(lines)) * self.LINE_H
        self.setFixedSize(int(w), int(h))

    # ---- 动画 ----
    def _tick(self):
        self._t += 0.033
        self.update()

    def show_toast(self):
        """显示并开始计时（由 show_toast 便捷函数调用）。"""
        self._timer.start()
        self._closer.start(self._duration)
        self.show()
        self.raise_()
        self._reposition()
        return self

    def _fade_out(self):
        self._closing = True
        self._timer.start()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        # 淡入 220ms，之后保持；_closing 后 320ms 淡出
        if self._closing:
            self._opacity = max(0.0, 1.0 - self._t / 0.32)
            if self._opacity <= 0.0:
                self._timer.stop()
                self._finish()
                return
        else:
            self._opacity = min(1.0, self._t / 0.22)

        p.setOpacity(self._opacity)
        _paint_hud(p, rect, radius=8.0, scan=0.0,
                   fill_alpha=0.97, base=_hud()["panel"])

        # 左侧状态竖条（画在底板之上）
        _main_col, bar_col_hex, text_col_hex = self._toast_colors()
        bar_col = QtGui.QColor(bar_col_hex)
        p.fillRect(QtCore.QRectF(self.PAD_X - 4, self.PAD_Y - 3,
                                 self.BAR_W, self.height() - 2 * self.PAD_Y + 6),
                   bar_col)

        # 文字
        text_col = text_col_hex
        p.setPen(QtGui.QPen(QtGui.QColor(text_col)))
        p.setFont(self._font)
        fm = QtGui.QFontMetrics(self._font)
        text_x = self.PAD_X + self.BAR_W + self.BAR_GAP
        text_w = max(40, self.width() - text_x - self.PAD_X)
        y = self.PAD_Y + fm.ascent()
        for ln in self._text_lines(text_w):
            p.drawText(text_x, y, ln)
            y += self.LINE_H
        p.end()

    def _finish(self):
        try:
            self.hide()
            self.deleteLater()
        except Exception:          # noqa: BLE001
            pass

    # ---- 定位 ----
    def _reposition(self):
        """把提示摆到父控件的**中心区**。

        原先固定在顶部（y=18），但主窗口的标题栏/工具栏那一带本来就有
        信息密度很高的控件，提示压在那里既挡按钮又不像「全局通知」；
        而用户视线焦点在影像中心区，居中显示最容易被看到。
        """
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            pw, ph = parent.width(), parent.height()
            w = self.width()
            h = self.height()
            x = max(8, (pw - w) // 2)
            # 中心略微上移（约 12%），避免正好压住影像正中；
            # 窗口过矮时退化为贴顶，保证完整可见。
            y = (ph - h) // 2 - int(ph * 0.06)
            y = max(8, min(y, max(8, ph - h - 8)))
            self.move(x, y)
        except Exception:          # noqa: BLE001
            pass

    def eventFilter(self, obj, ev):
        if ev.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
            self._reposition()
        return False

    def showEvent(self, ev):
        parent = self.parentWidget()
        if parent is not None:
            parent.installEventFilter(self)
        self._reposition()
        super().showEvent(ev)


def show_toast(parent, text="", kind="info", duration=2600, max_width=520):
    """在 ``parent`` 上显示一条轻提示（自动消失，无需确认）。

    同一个 ``parent`` 上只保留一条 Toast：新的会把旧的直接替换掉，
    避免连续操作时提示堆成一摞。

    Returns:
        Toast 或 None（parent 为空时返回 None）。
    """
    if parent is None:
        return None
    try:
        old = getattr(parent, "_toast_widget", None)
        if old is not None:
            try:
                old._timer.stop()
                old._closer.stop()
                old.hide()
                old.deleteLater()
            except Exception:      # noqa: BLE001
                pass
        toast = Toast(parent, text=text, kind=kind, duration=duration,
                      max_width=max_width)
        parent._toast_widget = toast
        toast.show_toast()
        return toast
    except Exception:              # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 7. 便捷入口
# ---------------------------------------------------------------------------
def attach_busy(target, text="加载中...", sub="", spinner_size=54, dim=0.72):
    """在 ``target`` 上显示（或复用）一个遮罩，返回遮罩对象。

    遮罩缓存在 target 上，重复调用不会堆积控件。
    """
    if target is None:
        return None
    ov = getattr(target, "_busy_overlay", None)
    if ov is None:
        try:
            ov = BusyOverlay(target, text=text, spinner_size=spinner_size,
                             dim=dim)
        except Exception:          # noqa: BLE001
            return None
        target._busy_overlay = ov
    ov.set_text(text)
    ov.set_sub_text(sub)
    ov.show()
    ov.raise_()
    QtWidgets.QApplication.processEvents()
    return ov


def busy_ancestor_text(widget):
    """找到 ``widget`` 自身或祖先中**正在显示**的 BusyOverlay 文案。

    用于避免「两个加载提示叠加」：StudyViewPanel 上的遮罩文案居中，
    VTKViewer 另外弹一个 TechBusyDialog（它定位到 parent = VTKViewer 的中心，
    而 VTKViewer 在四宫格的右下角）——同时出现两个提示，
    用户会认为程序卡死。

    只认 ``_busy_overlay`` 属性与 ``_busy_overlay`` 这一自己维护的缓存键，
    不通用地猜「有遮罩就不弹窗口」——
    那会让没有外层遮罩的场景（如阈值滑块、着色切换）
    也拒绝显示主提示，反而没有任何反馈。

    Returns:
        str: 文案（可能为空字符串）；没有遮罩则返回 None。
    """
    if widget is None:
        return None
    node = widget
    # 只向上找父链，深度有限，避免异常深的控件树
    for _ in range(64):
        if node is None:
            return None
        ov = getattr(node, "_busy_overlay", None)
        if ov is not None:
            try:
                if ov.isVisible():
                    return str(ov.text() or "")
            except Exception:      # noqa: BLE001
                return ""
        try:
            node = node.parentWidget()
        except Exception:          # noqa: BLE001
            return None
    return None


def detach_busy(target):
    """隐藏并销毁 ``target`` 上的遮罩"""
    if target is None:
        return
    ov = getattr(target, "_busy_overlay", None)
    if ov is None:
        return
    try:
        ov.hide()
        ov.deleteLater()
    except Exception:              # noqa: BLE001
        pass
    try:
        delattr(target, "_busy_overlay")
    except Exception:              # noqa: BLE001
        pass


class busy_scope(object):
    """上下文管理器：进入显示遮罩，退出隐藏。

    ::

        with busy_scope(self._study_panel, "正在切换阈值预设..."):
            do_something_slow()

    注意：同步阻塞的活会让动画停在当前帧（见文件头说明），退出时务必保证
    遮罩被摘掉 —— 这里用 try/finally 保证。
    """

    def __init__(self, target, text="加载中...", sub="", spinner_size=54,
                 dim=0.72):
        self._target = target
        self._text = text
        self._sub = sub
        self._size = spinner_size
        self._dim = dim
        self._ov = None

    def __enter__(self):
        self._ov = attach_busy(self._target, self._text, self._sub,
                               self._size, self._dim)
        # 让首帧真的画出来（同步阻塞前唯一能刷屏的机会）
        for _ in range(3):
            QtWidgets.QApplication.processEvents()
        return self._ov

    def __exit__(self, exc_type, exc, tb):
        detach_busy(self._target)
        return False
