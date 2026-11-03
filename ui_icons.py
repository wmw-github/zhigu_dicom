# -*- coding: utf-8 -*-
"""工具按钮图标工厂（CT3D v1.12.1）

为什么用代码画图标而不是放 .png/.svg 资源文件
-------------------------------------------
本项目是**绿色嵌入式发行包**（自带 pyenv 嵌入式 Python + PyQt5 + VTK），
资源文件一旦漏打包（复制目录时漏了 resources/、或被杀毒软件隔离），
按钮就变成空白方块，而且现场（医院内网机器）没有开发环境可补。

所以这里用 QPainter **在运行时把图标画出来**：
  * 零外部依赖，不存在「资源丢失」这种故障模式
  * Win7 上同样稳定（只用 QPainter 基本图形，PyQt5 5.15 全支持）
  * 还能按主题色自动适配深色界面

所有图标都是单色线条风格（深色底上用亮色描边），在 16~24px 下清晰可辨。
每个按钮都配一个**汉字 tooltip**，鼠标悬停即显示中文说明。
"""

from PyQt5 import QtCore, QtGui

# 主题色（与主程序深色医疗风格一致）
ICON_COLOR = "#d0d0d0"       # 普通图标
ICON_ACTIVE = "#38bdf8"      # 选中/高亮
ICON_WARN = "#ffd54f"        # 切割等「会改变数据」的操作
ICON_DANGER = "#f85149"      # 清除类


def _pen(color, width=1.6):
    p = QtGui.QPen(QtGui.QColor(color))
    p.setWidthF(width)
    p.setCapStyle(QtCore.Qt.RoundCap)
    p.setJoinStyle(QtCore.Qt.RoundJoin)
    return p


def _new_painter(size):
    """新建一个 size×size 的透明画布与 painter。"""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    return pm, p


def _finish(pm, p):
    p.end()
    return pm


# ---------------------------------------------------------------------------
# 单个图标的绘制函数：每个接收 (painter, size, color)
# ---------------------------------------------------------------------------

def _draw_ruler(p, s, color):
    """长度测量：带两端刻度的尺寸线（工程制图里的标注线）。"""
    p.setPen(_pen(color, 1.5))
    # 水平尺寸线
    p.drawLine(int(s * 0.18), int(s * 0.50), int(s * 0.82), int(s * 0.50))
    # 两端竖直界尺
    p.drawLine(int(s * 0.18), int(s * 0.32), int(s * 0.18), int(s * 0.68))
    p.drawLine(int(s * 0.82), int(s * 0.32), int(s * 0.82), int(s * 0.68))
    # 两端箭头（向内的实心三角）
    p.setBrush(QtGui.QBrush(QtGui.QColor(color)))
    p.setPen(QtCore.Qt.NoPen)
    p.drawPolygon(QtGui.QPolygonF([
        QtCore.QPointF(s * 0.18, s * 0.50),
        QtCore.QPointF(s * 0.30, s * 0.42),
        QtCore.QPointF(s * 0.30, s * 0.58),
    ]))
    p.drawPolygon(QtGui.QPolygonF([
        QtCore.QPointF(s * 0.82, s * 0.50),
        QtCore.QPointF(s * 0.70, s * 0.42),
        QtCore.QPointF(s * 0.70, s * 0.58),
    ]))


def _draw_angle(p, s, color):
    """角度测量：两条射线 + 圆弧（顶点带实心点）。"""
    p.setPen(_pen(color, 1.6))
    p.setBrush(QtCore.Qt.NoBrush)
    # 顶点在左下
    p.drawLine(int(s * 0.20), int(s * 0.82), int(s * 0.20), int(s * 0.24))
    p.drawLine(int(s * 0.20), int(s * 0.82), int(s * 0.84), int(s * 0.44))
    # 圆弧：从竖直方向扫到斜线方向
    p.setPen(_pen(color, 1.2))
    p.drawArc(QtCore.QRectF(s * 0.20, s * 0.44, s * 0.34, s * 0.38),
              -90 * 16, 42 * 16)
    # 顶点实心点
    p.setBrush(QtGui.QBrush(QtGui.QColor(color)))
    p.setPen(QtCore.Qt.NoPen)
    p.drawEllipse(QtCore.QRectF(s * 0.14, s * 0.76, s * 0.12, s * 0.12))


def _draw_ct(p, s, color):
    """CT 值：圆形 ROI + 中心十字 + 一个小方块（体素）。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtCore.Qt.NoBrush)
    p.drawEllipse(QtCore.QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68))
    p.setPen(_pen(color, 1.1))
    cx, cy = s * 0.5, s * 0.5
    p.drawLine(int(cx - s * 0.13), int(cy), int(cx + s * 0.13), int(cy))
    p.drawLine(int(cx), int(cy - s * 0.13), int(cx), int(cy + s * 0.13))


def _draw_eraser(p, s, color):
    """清除：橡皮擦 / 垃圾桶。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtCore.Qt.NoBrush)
    # 桶身
    p.drawLine(int(s * 0.30), int(s * 0.36), int(s * 0.36), int(s * 0.82))
    p.drawLine(int(s * 0.36), int(s * 0.82), int(s * 0.64), int(s * 0.82))
    p.drawLine(int(s * 0.64), int(s * 0.82), int(s * 0.70), int(s * 0.36))
    # 桶口
    p.drawLine(int(s * 0.24), int(s * 0.36), int(s * 0.76), int(s * 0.36))
    # 提手
    p.drawLine(int(s * 0.40), int(s * 0.36), int(s * 0.40), int(s * 0.24))
    p.drawLine(int(s * 0.60), int(s * 0.36), int(s * 0.60), int(s * 0.24))
    p.drawLine(int(s * 0.40), int(s * 0.24), int(s * 0.60), int(s * 0.24))


def _draw_scissors_cut(p, s, color):
    """切割：剪刀。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtCore.Qt.NoBrush)
    # 两个刀环
    p.drawEllipse(QtCore.QRectF(s * 0.16, s * 0.56, s * 0.26, s * 0.26))
    p.drawEllipse(QtCore.QRectF(s * 0.58, s * 0.56, s * 0.26, s * 0.26))
    # 交叉的刀刃
    p.drawLine(int(s * 0.36), int(s * 0.62), int(s * 0.74), int(s * 0.18))
    p.drawLine(int(s * 0.64), int(s * 0.62), int(s * 0.26), int(s * 0.18))


def _draw_curved_arrow(p, s, color, clockwise=True):
    """画一个带实心箭头的圆弧箭头（撤销 / 重置共用）。

    箭头方向由圆弧的**切线**算出，保证箭头永远指向圆弧前进的方向，
    不会出现「弧画了但箭头方向不对 / 箭头压在弧上」的问题。

    Args:
        clockwise: True = 顺时针扫过（撤销：回退），False = 逆时针（重置：重来）。
    """
    import math

    cx, cy = s * 0.50, s * 0.50
    r = s * 0.28
    start_deg = 55.0            # 起始角（屏幕坐标：0°=右，90°=下）
    span_deg = 285.0            # 扫过的角度
    end_deg = start_deg + span_deg

    p.setPen(_pen(color, 1.6))
    p.setBrush(QtCore.Qt.NoBrush)
    # drawArc 的正角度在屏幕上表现为顺时针
    p.drawArc(QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r),
              int(start_deg * 16), int(span_deg * 16))

    # 弧终点
    a = math.radians(end_deg)
    ex = cx + r * math.cos(a)
    ey = cy + r * math.sin(a)
    # 切线方向：正向扫动时 d/dθ(cos, sin) = (-sin, cos)
    if clockwise:
        tx, ty = -math.sin(a), math.cos(a)
    else:
        tx, ty = math.sin(a), -math.cos(a)
    # 法线
    nx, ny = -ty, tx

    head_len = s * 0.16
    head_w = s * 0.15
    tip = (ex + tx * head_len, ey + ty * head_len)
    base1 = (ex + nx * head_w / 2.0, ey + ny * head_w / 2.0)
    base2 = (ex - nx * head_w / 2.0, ey - ny * head_w / 2.0)

    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QBrush(QtGui.QColor(color)))
    p.drawPolygon(QtGui.QPolygonF([
        QtCore.QPointF(tip[0], tip[1]),
        QtCore.QPointF(base1[0], base1[1]),
        QtCore.QPointF(base2[0], base2[1]),
    ]))


def _draw_undo(p, s, color):
    """撤销：逆时针回转箭头。"""
    _draw_curved_arrow(p, s, color, clockwise=False)


def _draw_reset(p, s, color):
    """重置：顺时针回转箭头（与撤销镜像）。"""
    _draw_curved_arrow(p, s, color, clockwise=True)


def _draw_camera(p, s, color):
    """截图：相机。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtCore.Qt.NoBrush)
    p.drawRoundedRect(QtCore.QRectF(s * 0.12, s * 0.32, s * 0.76, s * 0.50),
                      s * 0.08, s * 0.08)
    # 取景器凸起
    p.drawLine(int(s * 0.36), int(s * 0.32), int(s * 0.42), int(s * 0.22))
    p.drawLine(int(s * 0.42), int(s * 0.22), int(s * 0.58), int(s * 0.22))
    p.drawLine(int(s * 0.58), int(s * 0.22), int(s * 0.64), int(s * 0.32))
    # 镜头
    p.drawEllipse(QtCore.QRectF(s * 0.38, s * 0.44, s * 0.24, s * 0.24))


def _draw_multiview(p, s, color):
    """保存多视角：四个小方块（多宫格）。"""
    p.setPen(_pen(color, 1.3))
    p.setBrush(QtCore.Qt.NoBrush)
    m = s * 0.14
    gap = s * 0.06
    sz = (s - 2 * m - gap) / 2.0
    for r in range(2):
        for c in range(2):
            p.drawRect(QtCore.QRectF(m + c * (sz + gap), m + r * (sz + gap),
                                     sz, sz))


def _draw_cube(p, s, color):
    """三维模型：立方体（线框）。"""
    p.setPen(_pen(color, 1.3))
    p.setBrush(QtCore.Qt.NoBrush)
    # 前面
    p.drawRect(QtCore.QRectF(s * 0.20, s * 0.34, s * 0.42, s * 0.42))
    # 后面（偏移）
    p.drawRect(QtCore.QRectF(s * 0.38, s * 0.22, s * 0.42, s * 0.42))
    # 连接线
    p.drawLine(int(s * 0.20), int(s * 0.34), int(s * 0.38), int(s * 0.22))
    p.drawLine(int(s * 0.62), int(s * 0.34), int(s * 0.80), int(s * 0.22))
    p.drawLine(int(s * 0.62), int(s * 0.76), int(s * 0.80), int(s * 0.64))


def _draw_strip(p, s, color):
    """序列：竖排缩略图。"""
    p.setPen(_pen(color, 1.3))
    p.setBrush(QtCore.Qt.NoBrush)
    p.drawRect(QtCore.QRectF(s * 0.20, s * 0.18, s * 0.60, s * 0.64))
    p.drawLine(int(s * 0.20), int(s * 0.39), int(s * 0.80), int(s * 0.39))
    p.drawLine(int(s * 0.20), int(s * 0.60), int(s * 0.80), int(s * 0.60))


def _draw_measure3d(p, s, color):
    """三维测长：立方体 + 斜穿的三维测量线（两端实心端点）。"""
    p.setPen(_pen(color, 1.2))
    p.setBrush(QtCore.Qt.NoBrush)
    # 立方体线框
    p.drawRect(QtCore.QRectF(s * 0.14, s * 0.42, s * 0.44, s * 0.44))
    p.drawRect(QtCore.QRectF(s * 0.30, s * 0.26, s * 0.44, s * 0.44))
    p.drawLine(int(s * 0.14), int(s * 0.42), int(s * 0.30), int(s * 0.26))
    p.drawLine(int(s * 0.58), int(s * 0.42), int(s * 0.74), int(s * 0.26))
    p.drawLine(int(s * 0.58), int(s * 0.86), int(s * 0.74), int(s * 0.70))
    # 穿过的测量线（用主色加粗，突出「这是测量」）
    p.setPen(_pen(ICON_ACTIVE, 2.0))
    p.drawLine(int(s * 0.18), int(s * 0.80), int(s * 0.82), int(s * 0.24))
    # 两端端点
    p.setBrush(QtGui.QBrush(QtGui.QColor(ICON_ACTIVE)))
    p.setPen(QtCore.Qt.NoPen)
    p.drawEllipse(QtCore.QRectF(s * 0.10, s * 0.72, s * 0.16, s * 0.16))
    p.drawEllipse(QtCore.QRectF(s * 0.74, s * 0.16, s * 0.16, s * 0.16))


def _draw_measure_clear(p, s, color):
    """清测量：尺寸线 + 叉号（表示「把测量清掉」）。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtCore.Qt.NoBrush)
    # 尺寸线（与 ruler 图标同款，让人一眼认出是「测量」）
    p.drawLine(int(s * 0.16), int(s * 0.50), int(s * 0.84), int(s * 0.50))
    p.drawLine(int(s * 0.16), int(s * 0.34), int(s * 0.16), int(s * 0.66))
    p.drawLine(int(s * 0.84), int(s * 0.34), int(s * 0.84), int(s * 0.66))
    # 右上角红色叉号
    p.setPen(_pen(ICON_DANGER, 1.9))
    p.drawLine(int(s * 0.56), int(s * 0.14), int(s * 0.86), int(s * 0.40))
    p.drawLine(int(s * 0.86), int(s * 0.14), int(s * 0.56), int(s * 0.40))


def _draw_invert(p, s, color):
    """反色：一个圆，左半填实、右半留空（黑白反相的直觉表达）。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtCore.Qt.NoBrush)
    r = QtCore.QRectF(s * 0.14, s * 0.14, s * 0.72, s * 0.72)
    p.drawEllipse(r)
    # 左半填实：半圆用 QPainterPath 更好控，这里用扇形（180°）代替
    p.setBrush(QtGui.QColor(color))
    p.drawChord(r, int(90 * 16), int(180 * 16))
    # 右半里再点一个小实心点，强化「黑白两态」的对比
    p.setBrush(QtCore.Qt.NoBrush)


def _draw_select(p, s, color):
    """选择：鼠标指针箭头（选中/浏览模式）。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtGui.QColor(color))
    poly = QtGui.QPolygonF([
        QtCore.QPointF(s * 0.26, s * 0.16),
        QtCore.QPointF(s * 0.26, s * 0.76),
        QtCore.QPointF(s * 0.42, s * 0.62),
        QtCore.QPointF(s * 0.54, s * 0.84),
        QtCore.QPointF(s * 0.66, s * 0.78),
        QtCore.QPointF(s * 0.54, s * 0.57),
        QtCore.QPointF(s * 0.76, s * 0.55),
    ])
    p.drawPolygon(poly)


def _draw_maximize(p, s, color):
    """最大化：矩形框 + 加粗的上边（Windows 最大化按钮的通用画法）。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtCore.Qt.NoBrush)
    x0, y0, x1, y1 = s * 0.16, s * 0.16, s * 0.84, s * 0.84
    p.drawRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
    p.setPen(_pen(color, 2.6))
    p.drawLine(int(x0), int(y0), int(x1), int(y0))


def _draw_eye_off(p, s, color):
    """隐藏：眼睛加一道斜杠。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtCore.Qt.NoBrush)
    cx, cy = s * 0.5, s * 0.5
    # 眼睛外形（两段弧拼成杏仁形）
    path = QtGui.QPainterPath()
    path.moveTo(s * 0.12, cy)
    path.quadTo(cx, s * 0.16, s * 0.88, cy)
    path.quadTo(cx, s * 0.84, s * 0.12, cy)
    p.drawPath(path)
    # 瞳孔
    p.setBrush(QtGui.QColor(color))
    p.drawEllipse(QtCore.QRectF(cx - s * 0.11, cy - s * 0.11,
                                s * 0.22, s * 0.22))
    p.setBrush(QtCore.Qt.NoBrush)
    # 斜杠
    p.drawLine(int(s * 0.18), int(s * 0.80), int(s * 0.82), int(s * 0.22))


def _draw_tools(p, s, color):
    """工具栏：三条横线 + 右侧滑块（表示「一排工具」）。"""
    p.setPen(_pen(color, 1.4))
    p.setBrush(QtCore.Qt.NoBrush)
    for i, frac in enumerate((0.30, 0.50, 0.70)):
        p.drawLine(int(s * 0.16), int(s * frac), int(s * 0.84), int(s * frac))
        p.setBrush(QtGui.QColor(color))
        p.drawEllipse(QtCore.QRectF(s * 0.62, s * frac - s * 0.075,
                                    s * 0.15, s * 0.15))
        p.setBrush(QtCore.Qt.NoBrush)


# ---------------------------------------------------------------------------
# 图标注册表：名字 → 绘制函数
# ---------------------------------------------------------------------------
_DRAWERS = {
    "ruler": _draw_ruler,
    "angle": _draw_angle,
    "ct": _draw_ct,
    "eraser": _draw_eraser,
    "cut": _draw_scissors_cut,
    "undo": _draw_undo,
    "reset": _draw_reset,
    "camera": _draw_camera,
    "multiview": _draw_multiview,
    "cube": _draw_cube,
    "strip": _draw_strip,
    "measure3d": _draw_measure3d,
    "measure_clear": _draw_measure_clear,
    "invert": _draw_invert,
    "select": _draw_select,
    "maximize": _draw_maximize,
    "eye_off": _draw_eye_off,
    "tools": _draw_tools,
}

# 缓存：同名同尺寸同颜色的图标只画一次（图标不会变，缓存可省重复绘制）
_CACHE = {}


def make_icon(name, size=18, color=ICON_COLOR):
    """按名字生成一个 QIcon。

    Args:
        name: 图标名（见 ``_DRAWERS`` 的键）。
        size: 边长（像素），默认 18。
        color: 线条颜色，默认浅灰（深色底上清晰）。

    Returns:
        QtGui.QIcon；名字未知时返回空图标（按钮会显示为纯图标按钮无图）。
    """
    drawer = _DRAWERS.get(name)
    if drawer is None:
        return QtGui.QIcon()
    key = (name, int(size), color)
    pm = _CACHE.get(key)
    if pm is None:
        pm, p = _new_painter(int(size))
        try:
            drawer(p, float(size), color)
        except Exception:            # noqa: BLE001  画图标失败不应影响主程序
            pass
        pm = _finish(pm, p)
        _CACHE[key] = pm
    return QtGui.QIcon(pm)


def apply_icon_button(btn, name, tooltip, size=18, color=ICON_COLOR,
                      icon_size=16):
    """把一个 QToolButton 改造成「纯图标 + 汉字提示」按钮。

    Args:
        btn: 目标 QToolButton。
        name: 图标名。
        tooltip: 鼠标悬停显示的**中文**说明。
        size: 绘制图标时的画布边长。
        color: 图标颜色。
        icon_size: 按钮上实际显示的图标边长。
    """
    try:
        btn.setIcon(make_icon(name, size, color))
        btn.setIconSize(QtCore.QSize(icon_size, icon_size))
        btn.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        btn.setToolTip(str(tooltip or ""))
    except Exception:                # noqa: BLE001
        pass
    return btn


def apply_tool_button(btn, name, text, tooltip, size=20, icon_size=18,
                      color=ICON_COLOR, min_width=46):
    """把 QToolButton 改造成「图标在上 + 汉字在下」的工具栏按钮。

    工具栏按钮长期只靠图标，用户（尤其是医生）很难一眼认出「这把剪刀是
    切割还是裁剪」，所以在图标下方补一行 2 字/3 字的短标签；tooltip 仍
    保留完整说明，悬停可看。

    Args:
        btn: 目标 QToolButton。
        name: 图标名。
        text: 图标下方显示的短文字（建议 2~3 个汉字）。
        tooltip: 悬停提示（完整中文说明）。
        min_width: 按钮最小宽度，保证 3 字标签不被压成省略号。
    """
    try:
        btn.setIcon(make_icon(name, size, color))
        btn.setIconSize(QtCore.QSize(icon_size, icon_size))
        btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextUnderIcon)
        btn.setText(str(text or ""))
        btn.setToolTip(str(tooltip or ""))
        f = btn.font()
        f.setPointSize(9)
        # 显式指定中文字体：默认字体在只装了西文字体的机器上会把汉字
        # 渲染成方框（Win7 上 YaHei/SimHei 一定在，最后退回 sans-serif）。
        f.setFamily("Microsoft YaHei")
        btn.setFont(f)
        btn.setMinimumWidth(int(min_width))
    except Exception:                # noqa: BLE001
        pass
    return btn
