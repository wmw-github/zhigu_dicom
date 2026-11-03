# -*- coding: utf-8 -*-
"""界面皮肤（主题）系统 — v1.21.0

设计原则
========
1. **不动布局**：只改颜色 / 圆角 / 描边 / 字重，控件尺寸与排列保持原样。
2. **一键切换、即时生效**：切换后立刻重绘，不必重启，也不必重开窗口。
3. **影像区恒定深色**：无论选浅色还是深色皮肤，阅片区（含它的浮动工具条、
   四角叠加文字、层厚滑杆、方位字）永远是深色。读片要暗背景，这也是
   专业阅片软件的通行做法 —— 顺带避免了浅色皮肤把影像衬得发灰。

实现要点
========
* 一套皮肤 = 一组**语义色 token**（``SKINS``），不写死到具体控件上。
* ``qss()`` 把 token 渲染成全局 QSS；``palette()`` 同时给出 QPalette ——
  前者管外观细节（边框/圆角/hover），后者管 QStyle 自己画的部分
  （勾选框对勾、箭头、图标着色）。
* **控件级样式不要写死颜色**：
  - 纯文字色/字号差异 → ``role(widget, "muted")``，交给全局 QSS 的
    ``[role="..."]`` 选择器，切皮肤自动跟随；
  - 结构性样式（整块自绘容器、滑杆、按钮组）→ ``bind(widget, factory)``，
    工厂函数每次被调用时按**当前** token 重新生成 QSS，切皮肤自动重建。
* ``qss_viewer_*`` 系列是阅片区专用（恒定深色），与皮肤无关。

新增皮肤
========
往 ``SKINS`` 里加一条即可（键名不要太长，会写进注册表）；
``_ORDER`` 决定切换面板里的顺序。
"""

from __future__ import annotations

import weakref

from PyQt5 import QtCore, QtGui, QtWidgets

ORG = "ZhiGu"
APP = "CT3D"
SETTINGS_KEY = "ui/skin"
DEFAULT_SKIN = "graphite"

# ---------------------------------------------------------------------------
# 皮肤定义
#   label / desc   : 切换面板里显示的名字与一句话说明
#   canvas         : 窗口底色（最底层）
#   panel          : 侧栏/分组等「面板」面
#   field          : 列表、输入框、文本域等「可编辑内容」面
#   raised         : 菜单、气泡、卡片等「浮起」面
#   button         : 按钮常态面
#   border         : 分隔线 / 弱描边
#   border_strong  : 输入框、按钮等可交互元素的描边
#   accent         : 唯一强调色（选中态、当前项、主操作）
#   accent_view    : 阅片区用的强调色（要压在纯黑影像上，所以比 accent 亮）
#   accent_alt     : 与 accent 相邻的第二色，只给加载动画的双色环用
#                    （同色系双色调比「青+紫」这种固定搭配更耐看，
#                     也不会在浅色皮肤上突然跳出一块饱和紫）
#   sel_bg/sel_fg  : 列表选中行（实色，QPalette 不支持半透明混色）
# ---------------------------------------------------------------------------
SKINS = {
    "graphite": {
        "label": "石墨",
        "desc": "中性炭灰 · 长时间阅片首选",
        "canvas": "#14161A",
        "panel": "#1A1D22",
        "field": "#101216",
        "raised": "#21252B",
        "button": "#262B32",
        "button_hover": "#2F343C",
        "button_press": "#1F242A",
        "hover": "#1E2228",
        "border": "#282D34",
        "border_strong": "#39414A",
        "text": "#E8EAED",
        "text_dim": "#A9AFB8",
        "text_muted": "#79808A",
        "accent": "#4A8FE7",
        "accent_hover": "#63A2EE",
        "accent_press": "#3B7BCC",
        "accent_view": "#4A9BFF",
        "accent_alt": "#8E86E8",
        "on_accent": "#FFFFFF",
        "danger": "#E0705F",
        "warning": "#D9A441",
        "success": "#5BAE86",
    },
    "midnight": {
        "label": "墨蓝",
        "desc": "深海军蓝 · 冷调、专注",
        "canvas": "#0E1520",
        "panel": "#131C28",
        "field": "#0A1017",
        "raised": "#1A2532",
        "button": "#1F2B3A",
        "button_hover": "#27364A",
        "button_press": "#1A2431",
        "hover": "#182231",
        "border": "#22303D",
        "border_strong": "#354A5E",
        "text": "#E4ECF4",
        "text_dim": "#9EB1C3",
        "text_muted": "#6E8092",
        "accent": "#3FB6A8",
        "accent_hover": "#55C9BA",
        "accent_press": "#349C90",
        "accent_view": "#46C2B4",
        "accent_alt": "#6E86E8",
        "on_accent": "#06201C",
        "danger": "#E0705F",
        "warning": "#D7A657",
        "success": "#4FB58C",
    },
    "ivory": {
        "label": "象牙",
        "desc": "暖白纸感 · 明亮、柔和",
        "canvas": "#F2EFEA",
        "panel": "#FAF8F5",
        "field": "#FFFFFF",
        "raised": "#FFFFFF",
        "button": "#FFFFFF",
        "button_hover": "#F3F0EA",
        "button_press": "#E8E3D9",
        "hover": "#EFEBE4",
        "border": "#E1DCD2",
        "border_strong": "#CCC5B8",
        "text": "#221F1B",
        "text_dim": "#5A544B",
        "text_muted": "#8A8376",
        "accent": "#2E6E63",
        "accent_hover": "#387F72",
        "accent_press": "#245A51",
        "accent_view": "#4FBFA8",
        "accent_alt": "#23786C",
        "on_accent": "#FFFFFF",
        "danger": "#B4462F",
        "warning": "#96681A",
        "success": "#2E7A55",
    },
    "celadon": {
        "label": "青瓷",
        "desc": "冷白釉色 · 克制、通透",
        "canvas": "#EBF0F1",
        "panel": "#F7FAFA",
        "field": "#FFFFFF",
        "raised": "#FFFFFF",
        "button": "#FFFFFF",
        "button_hover": "#EDF2F3",
        "button_press": "#E1E9EA",
        "hover": "#E9EFF0",
        "border": "#D6DEDF",
        "border_strong": "#BCC7C9",
        "text": "#1A2124",
        "text_dim": "#4F5C5E",
        "text_muted": "#7B8A8B",
        "accent": "#1F7A8C",
        "accent_hover": "#268FA4",
        "accent_press": "#196476",
        "accent_view": "#35B0C6",
        "accent_alt": "#175F70",
        "on_accent": "#FFFFFF",
        "danger": "#B04236",
        "warning": "#8F6A1C",
        "success": "#2A7A63",
    },
}

_ORDER = ["graphite", "midnight", "ivory", "celadon"]

# 阅片区（恒定深色，与皮肤无关）
VIEWER = {
    "bg": "#08090B",
    "hud_bg": "rgba(12, 14, 17, 200)",
    "hud_bg_hover": "rgba(32, 38, 46, 235)",
    "hud_border": "rgba(255, 255, 255, 26)",
    "hud_border_hover": "rgba(255, 255, 255, 64)",
    "hud_text": "#D6DAE0",
    "hud_text_hover": "#FFFFFF",
    "overlay_bg": "rgba(0, 0, 0, 150)",
    "overlay_border": "rgba(255, 255, 255, 26)",
    "overlay_text": "#FFFFFF",
}

_state = {"key": None, "app": None}
_bindings = []          # [(weakref.ref(widget), factory)]
_listeners = []         # [callable]


# ---------------------------------------------------------------------------
# 颜色小工具
# ---------------------------------------------------------------------------

def qcolor(hex_or_name, alpha=None) -> QtGui.QColor:
    c = QtGui.QColor(hex_or_name)
    if alpha is not None:
        c.setAlpha(int(alpha))
    return c


def _mix(a: str, b: str, k: float) -> str:
    """把颜色 a 按比例 k 混进 b（k=0 全 b，k=1 全 a）。"""
    ca, cb = QtGui.QColor(a), QtGui.QColor(b)
    return QtGui.QColor(
        int(ca.red() * k + cb.red() * (1 - k)),
        int(ca.green() * k + cb.green() * (1 - k)),
        int(ca.blue() * k + cb.blue() * (1 - k)),
    ).name()


def _rgba(hex_color: str, alpha: float) -> str:
    """'#RRGGBB' + 0~1 透明度 → QSS 的 rgba(...)（alpha 用 0~255 整数）。"""
    c = QtGui.QColor(hex_color)
    return "rgba(%d, %d, %d, %d)" % (c.red(), c.green(), c.blue(),
                                     max(0, min(255, int(round(alpha * 255)))))


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------

def keys():
    return list(_ORDER)


def current() -> str:
    key = _state.get("key")
    return key if key in SKINS else DEFAULT_SKIN


def current_skin() -> dict:
    return SKINS[current()]


def is_dark() -> bool:
    return current() in ("graphite", "midnight")


def display_name(key: str = None) -> str:
    return SKINS.get(key or current(), {}).get("label", "")


def tokens(key: str = None) -> dict:
    """取某套皮肤的完整 token（含派生色）。"""
    key = key if key in SKINS else current()
    raw = SKINS[key]
    t = dict(raw)
    t["key"] = key
    t["accent_soft"] = _rgba(raw["accent"], 0.18)
    t["accent_line"] = _rgba(raw["accent"], 0.62)
    t["danger_soft"] = _rgba(raw["danger"], 0.14)
    t["sel_bg"] = _mix(raw["accent"], raw["field"],
                       0.26 if key in ("graphite", "midnight") else 0.20)
    t["sel_fg"] = raw["text"]
    t["scroll"] = _rgba(raw["text_muted"], 0.30 if key in ("graphite", "midnight") else 0.42)
    t["scroll_hover"] = _rgba(raw["text_muted"], 0.62)
    t["track"] = _rgba(raw["text_muted"], 0.22)
    t["shadow"] = "rgba(0, 0, 0, 90)" if key in ("graphite", "midnight") else "rgba(0, 0, 0, 26)"
    # 未选中的勾选框/单选框描边：比 border_strong 再亮一档，
    # 否则深色皮肤下「方框」跟底色糊在一起，浅色皮肤下又太淡。
    t["control_line"] = _mix(raw["text_muted"], raw["border_strong"], 0.55)
    # 进度条/滑杆的「槽」：深色皮肤用 field 就够，浅色皮肤得比白再暗一档，
    # 否则白槽+白底完全看不见。
    t["well"] = (raw["field"] if key in ("graphite", "midnight")
                 else _mix(raw["canvas"], raw["border_strong"], 0.40))
    t.setdefault("accent_alt", raw["accent"])
    return t


def token(name: str, default: str = "#000000") -> str:
    return tokens().get(name, default)


def color(name: str) -> QtGui.QColor:
    return QtGui.QColor(tokens().get(name, "#000000"))


def hud() -> dict:
    """自绘 HUD（加载动画 / 忙碌遮罩 / Toast）用的取色口。

    这几块是纯 QPainter 绘制的，吃不到应用级样式表，所以颜色必须由调用方
    显式来取。集中在这里而不是散落到 loading_ui.py，是为了「切皮肤后
    下一次重绘就是新颜色」这件事只有一处实现。

    返回的 primary/secondary 是双色调，浅色皮肤下换成同色系的深一档副色，
    不会像固定「青+紫」那样在暖白/冷白底上跳出一块饱和紫。
    """
    t = tokens()
    return {
        "panel": t["panel"],
        "overlay": t["canvas"],
        "text": t["text"],
        "dim": t["text_dim"],
        "primary": t["accent_view"],
        "secondary": t["accent_alt"],
        "dark": is_dark(),
    }


# ---------------------------------------------------------------------------
# QPalette：让 QStyle 自己画的东西（对勾、箭头、禁用色）也跟随皮肤
# ---------------------------------------------------------------------------

def palette(key: str = None) -> QtGui.QPalette:
    t = tokens(key)
    p = QtGui.QPalette()
    p.setColor(QtGui.QPalette.Window, qcolor(t["canvas"]))
    p.setColor(QtGui.QPalette.WindowText, qcolor(t["text"]))
    p.setColor(QtGui.QPalette.Base, qcolor(t["field"]))
    p.setColor(QtGui.QPalette.AlternateBase, qcolor(t["panel"]))
    p.setColor(QtGui.QPalette.ToolTipBase, qcolor(t["raised"]))
    p.setColor(QtGui.QPalette.ToolTipText, qcolor(t["text"]))
    p.setColor(QtGui.QPalette.Text, qcolor(t["text"]))
    p.setColor(QtGui.QPalette.Button, qcolor(t["button"]))
    p.setColor(QtGui.QPalette.ButtonText, qcolor(t["text"]))
    p.setColor(QtGui.QPalette.BrightText, qcolor(t["danger"]))
    p.setColor(QtGui.QPalette.Link, qcolor(t["accent"]))
    p.setColor(QtGui.QPalette.LinkVisited, qcolor(t["accent"]))
    p.setColor(QtGui.QPalette.Highlight, qcolor(t["sel_bg"]))
    p.setColor(QtGui.QPalette.HighlightedText, qcolor(t["sel_fg"]))
    p.setColor(QtGui.QPalette.Light, qcolor(t["border_strong"]))
    p.setColor(QtGui.QPalette.Midlight, qcolor(t["border"]))
    p.setColor(QtGui.QPalette.Mid, qcolor(t["border"]))
    p.setColor(QtGui.QPalette.Dark, qcolor(_mix(t["canvas"], "#000000", 0.5)))
    p.setColor(QtGui.QPalette.Shadow, qcolor("#000000"))
    try:                                    # Qt 5.12+ 才有 PlaceholderText
        p.setColor(QtGui.QPalette.PlaceholderText, qcolor(t["text_muted"]))
    except Exception:                       # noqa: BLE001
        pass
    for group in (QtGui.QPalette.Disabled,):
        p.setColor(group, QtGui.QPalette.WindowText, qcolor(t["text_muted"]))
        p.setColor(group, QtGui.QPalette.Text, qcolor(t["text_muted"]))
        p.setColor(group, QtGui.QPalette.ButtonText, qcolor(t["text_muted"]))
    return p


# ---------------------------------------------------------------------------
# 全局 QSS
# ---------------------------------------------------------------------------
_QSS = """
/* ===== 基底 ===== */
QMainWindow, QDialog, QMessageBox, QFileDialog, QInputDialog, QErrorMessage {
    background-color: %(canvas)s;
    color: %(text)s;
}
QDialog QWidget { color: %(text)s; }

QLabel { color: %(text)s; background: transparent; }
QLabel:disabled { color: %(text_muted)s; }

/* ===== 文字角色（语义色，切皮肤自动跟随）===== */
QLabel[role="muted"]    { color: %(text_muted)s; }
QLabel[role="dim"]      { color: %(text_dim)s; }
QLabel[role="caption"]  { color: %(text_muted)s; font-size: 9pt; }
QLabel[role="accent"]   { color: %(accent)s; font-weight: 600; }
QLabel[role="danger"]   { color: %(danger)s; }
QLabel[role="warn"]     { color: %(warning)s; }
QLabel[role="success"]  { color: %(success)s; }
QLabel[role="section"]  { color: %(text)s; font-weight: 600; }
QLabel[role="title"]    { color: %(text)s; font-weight: 600; font-size: 10pt; }
QLabel[role="card"] {
    color: %(text_dim)s; background-color: %(raised)s;
    border: 1px solid %(border)s; border-radius: 5px; padding: 7px 9px;
}
QLabel[role="note"] {
    color: %(text_muted)s; background-color: %(panel)s;
    border-left: 2px solid %(border_strong)s; padding: 4px 8px;
}
QLabel[role="chip"] {
    color: %(accent)s; background-color: %(accent_soft)s;
    border-radius: 9px; padding: 1px 9px; font-size: 9pt;
}

/* ===== 按钮 ===== */
QPushButton {
    background-color: %(button)s;
    color: %(text)s;
    border: 1px solid %(border_strong)s;
    border-radius: 4px;
    padding: 4px 11px;
}
QPushButton:hover   { background-color: %(button_hover)s; border-color: %(accent_line)s; }
QPushButton:pressed { background-color: %(button_press)s; }
QPushButton:default { border-color: %(accent_line)s; }
QPushButton:focus   { outline: none; }
/* 禁用态保留按钮面，只把文字和描边压暗。
   直接把底色刷成 panel 会让按钮在深色皮肤上「消失成一个洞」——
   用户看不出那里本来有个按钮，只会以为界面缺了一块。 */
QPushButton:disabled {
    color: %(text_muted)s; background-color: %(button)s;
    border-color: %(border)s;
}
QPushButton[role="primary"] {
    background-color: %(accent)s; color: %(on_accent)s;
    border: 1px solid %(accent)s; font-weight: 600;
}
QPushButton[role="primary"]:hover   { background-color: %(accent_hover)s; border-color: %(accent_hover)s; }
QPushButton[role="primary"]:pressed { background-color: %(accent_press)s; border-color: %(accent_press)s; }
QPushButton[role="primary"]:disabled {
    background-color: %(button)s; color: %(text_muted)s;
    border-color: %(border)s; font-weight: 400;
}
/* tonal：一列重复出现的行内操作（如 PDF 列表每行的「导出」）用它。
   整屏十几个实色主色块会变成「按钮墙」，浅底同色系更干净，
   悬停时才填充 —— 既弱化重量又不丢可点感。 */
QPushButton[role="tonal"] {
    background-color: %(accent_soft)s; color: %(accent)s;
    border: 1px solid %(accent_line)s;
}
QPushButton[role="tonal"]:hover {
    background-color: %(accent)s; color: %(on_accent)s; border-color: %(accent)s;
}
QPushButton[role="tonal"]:pressed {
    background-color: %(accent_press)s; color: %(on_accent)s;
    border-color: %(accent_press)s;
}
QPushButton[role="ghost"] {
    background-color: transparent; border-color: transparent; color: %(text_dim)s;
}
QPushButton[role="ghost"]:hover {
    background-color: %(button_hover)s; border-color: %(border)s; color: %(text)s;
}
QPushButton[role="danger"] { color: %(danger)s; }
QPushButton[role="danger"]:hover {
    background-color: %(danger_soft)s; border-color: %(danger)s;
}

QToolButton {
    background: transparent; color: %(text)s;
    border: 1px solid transparent; border-radius: 4px; padding: 3px 6px;
}
QToolButton:hover   { background-color: %(button_hover)s; border-color: %(border)s; }
QToolButton:pressed { background-color: %(button_press)s; }
QToolButton:checked { background-color: %(accent_soft)s; border-color: %(accent_line)s; }
QToolButton:disabled { color: %(text_muted)s; }
QToolButton::menu-indicator { image: none; width: 0px; }

/* ===== 输入类 ===== */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QAbstractSpinBox {
    background-color: %(field)s;
    color: %(text)s;
    border: 1px solid %(border_strong)s;
    border-radius: 4px;
    padding: 4px 8px;
    selection-background-color: %(accent)s;
    selection-color: %(on_accent)s;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: %(accent_line)s;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: %(accent)s;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: %(text_muted)s; background-color: %(panel)s; border-color: %(border)s;
}
/* 下拉/微调箭头不写子控件规则：Qt 一旦接管 ::drop-down / ::up-button
   的绘制，就不再调用样式画箭头（实测直接变成一块空白）。改为把右边
   留出 padding，让 Fusion 的原生箭头照常画出来。 */
QComboBox { padding-right: 22px; }
QSpinBox, QDoubleSpinBox { padding-right: 20px; }
QComboBox QAbstractItemView {
    background-color: %(raised)s; color: %(text)s;
    border: 1px solid %(border)s; border-radius: 4px;
    selection-background-color: %(sel_bg)s; selection-color: %(sel_fg)s;
    outline: none; padding: 3px;
}

/* ===== 文本域 ===== */
QTextEdit, QPlainTextEdit {
    background-color: %(field)s; color: %(text)s;
    border: 1px solid %(border)s; border-radius: 5px;
    padding: 6px;
    selection-background-color: %(accent)s; selection-color: %(on_accent)s;
}
QTextEdit:focus, QPlainTextEdit:focus { border-color: %(accent_line)s; }

/* ===== 列表 / 表格 / 树 ===== */
QListWidget, QListView, QTreeWidget, QTreeView, QTableWidget, QTableView {
    background-color: %(field)s;
    alternate-background-color: %(panel)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 6px;
    outline: 0;
    selection-background-color: %(sel_bg)s;
    selection-color: %(sel_fg)s;
}
QListWidget::item, QListView::item, QTreeWidget::item, QTreeView::item,
QTableWidget::item, QTableView::item {
    border-radius: 4px;
    /* 这里**不能**写 color。QSS 一旦给 ::item 指定颜色，
       QStyleSheetStyle 就会盖掉 item 自己的 ForegroundRole，
       所有 setForeground(...) 全部失效（患者名按检查类型着色、
       质控历史按结论等级着色都靠它）。文字基色由上面的
       QTreeView/QTableView { color } 提供，效果一样。 */
}
QListWidget::item:selected, QListView::item:selected,
QTreeWidget::item:selected, QTreeView::item:selected,
QTableWidget::item:selected, QTableView::item:selected {
    background-color: %(sel_bg)s;
    color: %(sel_fg)s;
}
QListWidget::item:hover, QListView::item:hover,
QTreeWidget::item:hover, QTreeView::item:hover {
    background-color: %(hover)s;
}
QHeaderView { background-color: %(panel)s; border: none; }
QHeaderView::section {
    background-color: %(panel)s; color: %(text_dim)s;
    border: none; border-bottom: 1px solid %(border)s;
    padding: 5px 6px; font-weight: 600;
}
QHeaderView::section:vertical { border-right: 1px solid %(border)s; }
QTableCornerButton::section { background-color: %(panel)s; border: none; }

/* ===== 勾选 / 单选 ===== */
/* 指示器（方框 / 圆圈 / 对勾）不写 QSS，由 _SkinStyle.drawPrimitive 自绘：
   Fusion 在深色 palette 下把方框描边画得跟底色几乎同色（实测「只有对勾、
   没有框」），而一旦给 ::indicator 写规则，Qt 又只肯画实心块、不画对勾。
   自绘能同时拿到「看得见的框」和「精致的对勾」，还不依赖任何图片资源。 */
QCheckBox, QRadioButton { color: %(text)s; spacing: 8px; background: transparent; }
QCheckBox:disabled, QRadioButton:disabled { color: %(text_muted)s; }

/* ===== 滚动条 ===== */
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 2px 1px 2px 0;
}
QScrollBar:horizontal {
    background: transparent; height: 10px; margin: 0 2px 1px 2px;
}
QScrollBar::handle:vertical {
    background: %(scroll)s; border-radius: 3px; min-height: 26px; width: 5px;
}
QScrollBar::handle:horizontal {
    background: %(scroll)s; border-radius: 3px; min-width: 26px; height: 5px;
}
QScrollBar::handle:hover { background: %(scroll_hover)s; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed {
    background: %(accent)s;
}

/* ===== 滑杆 ===== */
QSlider::groove:horizontal {
    height: 4px; background: %(track)s; border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: %(accent)s; border-radius: 2px;
}
QSlider::add-page:horizontal { background: %(track)s; border-radius: 2px; }
/* 滑块用「浮起面 + 强调色描边」，不用 text 色填充：
   深色皮肤下 text 近白、浅色皮肤下近黑，用 text 当填充会让浅色皮肤
   出现一颗黑疙瘩，压在浅色槽上特别重。 */
QSlider::handle:horizontal {
    background: %(raised)s; border: 2px solid %(accent)s;
    width: 12px; height: 12px; margin: -6px 0; border-radius: 8px;
}
QSlider::handle:horizontal:hover { background: %(accent_hover)s; }
QSlider::handle:horizontal:disabled { border-color: %(border_strong)s; background: %(button)s; }

/* ===== 进度条 ===== */
QProgressBar {
    background-color: %(well)s; color: %(text_dim)s;
    border: 1px solid %(border)s; border-radius: 4px;
    text-align: center; padding: 1px;
}
QProgressBar::chunk { background-color: %(accent)s; border-radius: 3px; }

/* ===== 分组框 ===== */
QGroupBox {
    border: 1px solid %(border_strong)s; border-radius: 6px;
    margin-top: 15px; padding-top: 10px;
    color: %(text_dim)s; font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 10px; padding: 0 5px; color: %(accent)s;
}

/* ===== 菜单 ===== */
QMenuBar { background-color: %(canvas)s; color: %(text)s; border: none; }
QMenuBar::item { background: transparent; padding: 5px 11px; border-radius: 4px; }
QMenuBar::item:selected { background: %(button_hover)s; }
QMenu {
    background-color: %(raised)s; color: %(text)s;
    border: 1px solid %(border_strong)s; padding: 5px;
}
QMenu::item { padding: 6px 26px 6px 14px; border-radius: 4px; }
QMenu::item:selected { background-color: %(accent_soft)s; color: %(text)s; }
QMenu::item:disabled { color: %(text_muted)s; }
QMenu::separator { height: 1px; background: %(border)s; margin: 4px 8px; }
QMenu::icon { margin-left: 6px; }

/* ===== 标签页 ===== */
QTabWidget::pane {
    border: 1px solid %(border)s; border-radius: 6px; top: -1px;
    background-color: %(panel)s;
}
QTabBar::tab {
    background: transparent; color: %(text_muted)s;
    padding: 7px 15px; border: none;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:hover { color: %(text)s; }
QTabBar::tab:selected {
    color: %(text)s; font-weight: 600;
    border-bottom: 2px solid %(accent)s;
}

/* ===== 分隔 / 状态栏 / 提示 ===== */
/* 侧栏面板：比窗口底浅一档，撑出「窗口 → 面板 → 内容」三层 */
QWidget#sidePanel { background-color: %(panel)s; }
QWidget#sidePanel QLabel { background: transparent; }
QSplitter::handle { background-color: transparent; }
QSplitter::handle:hover { background-color: %(accent_soft)s; }
QStatusBar { background-color: %(panel)s; color: %(text_muted)s; border-top: 1px solid %(border)s; }
QStatusBar::item { border: none; }
QToolTip {
    background-color: %(raised)s; color: %(text)s;
    border: 1px solid %(border_strong)s; border-radius: 4px;
    padding: 5px 8px;
}
QScrollArea { border: none; background: transparent; }
QAbstractScrollArea::corner { background: transparent; border: none; }
"""


def qss(key: str = None) -> str:
    """当前（或指定）皮肤的全局样式表。"""
    return _QSS % tokens(key)


# ---------------------------------------------------------------------------
# 阅片区专用样式（恒定深色，与皮肤无关，只有强调色随皮肤变）
# ---------------------------------------------------------------------------

def qss_viewer_accent(key: str = None) -> str:
    return tokens(key)["accent_view"]


def qss_tool_buttons(key: str = None) -> str:
    """阅片区浮动工具条上的「图标 + 汉字」按钮（压在影像上，恒为深色玻璃）。"""
    return """
    QToolButton {
        background: %(hud_bg)s; color: %(hud_text)s;
        border: 1px solid %(hud_border)s; border-radius: 4px;
        padding: 2px 4px; font-size: 8pt;
    }
    QToolButton:hover { background: %(hud_bg_hover)s; border-color: %(hud_border_hover)s; color: %(hud_text_hover)s; }
    QToolButton:checked {
        background: %(accent)s; color: #FFFFFF; border-color: %(accent)s;
    }
    QToolButton:disabled { color: rgba(255,255,255,90); border-color: rgba(255,255,255,18); }
    """ % {"hud_bg": VIEWER["hud_bg"], "hud_bg_hover": VIEWER["hud_bg_hover"],
           "hud_border": VIEWER["hud_border"],
           "hud_border_hover": VIEWER["hud_border_hover"],
           "hud_text": VIEWER["hud_text"], "hud_text_hover": VIEWER["hud_text_hover"],
           "accent": qss_viewer_accent(key)}


def qss_overlay_label(font_family="'Consolas', 'Microsoft YaHei', monospace",
                      font_size="10pt") -> str:
    """影像四角叠加信息（患者信息 / 窗宽窗位 / 层位置）。"""
    return """
    QLabel {
        color: %(text)s;
        background-color: %(bg)s;
        border: 1px solid %(border)s;
        border-radius: 4px;
        padding: 3px 7px;
        font-family: %(family)s;
        font-size: %(size)s;
    }
    """ % {"text": VIEWER["overlay_text"], "bg": VIEWER["overlay_bg"],
           "border": VIEWER["overlay_border"], "family": font_family,
           "size": font_size}


def qss_char_label(font_size="24pt", alpha=180) -> str:
    """屏幕方位字（「左」「右」）。"""
    return """
    QLabel {
        color: rgba(255, 255, 255, %(alpha)d);
        font-family: 'SimHei', 'Microsoft YaHei', sans-serif;
        font-size: %(size)s;
        font-weight: bold;
        background: transparent;
    }
    """ % {"alpha": int(alpha), "size": font_size}


def qss_floating_button(name_prop=True) -> str:
    """阅片区里的小号纯文字/图标浮起按钮（截图、全屏等）。"""
    return """
    QToolButton {
        background: %(hud_bg)s; color: %(hud_text)s;
        border: 1px solid %(hud_border)s; border-radius: 4px;
        padding: 0px 6px; font-size: 9pt;
    }
    QToolButton:hover {
        background: %(hud_bg_hover)s; border-color: %(hud_border_hover)s;
        color: %(hud_text_hover)s;
    }
    QToolButton:checked { background: %(accent)s; color: #FFFFFF; border-color: %(accent)s; }
    """ % {"hud_bg": VIEWER["hud_bg"], "hud_bg_hover": VIEWER["hud_bg_hover"],
           "hud_border": VIEWER["hud_border"],
           "hud_border_hover": VIEWER["hud_border_hover"],
           "hud_text": VIEWER["hud_text"], "hud_text_hover": VIEWER["hud_text_hover"],
           "accent": qss_viewer_accent()}


def qss_mpr_info() -> str:
    """三维四视图右下角的 MPR 层位信息（等宽、绿字，压在黑底影像上）。"""
    return """
    QLabel {
        color: #5BE38B;
        background-color: %(bg)s;
        border: 1px solid %(border)s;
        border-radius: 4px;
        padding: 4px 7px;
        font-weight: bold;
        font-family: 'Consolas', monospace;
    }
    """ % {"bg": VIEWER["hud_bg"], "border": VIEWER["hud_border"]}


def qss_bare_toolbutton() -> str:
    """只要图标、不要底色的按钮（全屏/图层切换这类纯图标位置）。"""
    return ("QToolButton { background: transparent; border: none; padding: 0; }"
            "QToolButton:hover { background: transparent; }")


def qss_slice_slider() -> str:
    """二维阅片区的竖排层厚滑杆。"""
    return """
    QSlider { background: transparent; padding: 2px 0px; }
    QSlider::groove:vertical {
        background: %(hud_bg)s;
        border: 1px solid %(hud_border)s;
        border-radius: 5px;
        width: 8px;
        margin: 4px 6px;
    }
    QSlider::sub-page:vertical {
        background: %(accent_soft)s;
        border-radius: 5px;
    }
    QSlider::add-page:vertical { background: transparent; }
    QSlider::handle:vertical {
        background: %(accent)s;
        border: 1px solid %(accent)s;
        height: 12px; width: 12px;
        margin: 0 -3px;
        border-radius: 7px;
    }
    QSlider::handle:vertical:hover { background: %(accent_hover)s; }
    """ % {"hud_bg": VIEWER["hud_bg"], "hud_border": VIEWER["hud_border"],
           "accent": qss_viewer_accent(), "accent_hover": qss_viewer_accent(),
           "accent_soft": _rgba(qss_viewer_accent(), 0.45)}


def qss_thumbnail_strip() -> str:
    """底部序列缩略图条（属于窗口 chrome，跟随皮肤）。"""
    t = tokens()
    return """
    QFrame#thumbnailStrip {
        background-color: %(field)s;
        border-top: 1px solid %(border)s;
    }
    QScrollArea { background: transparent; border: none; }
    QScrollBar:horizontal {
        background: transparent; height: 8px; margin: 0px;
    }
    QScrollBar::handle:horizontal {
        background: %(scroll)s; border-radius: 4px; min-width: 30px;
    }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    """ % t


def qss_thumbnail_button() -> str:
    t = tokens()
    return """
    QToolButton {
        background: transparent;
        border: 2px solid transparent;
        border-radius: 5px;
        color: %(text_muted)s;
        font-size: 8pt;
        padding: 0px;
    }
    QToolButton:hover { border: 2px solid %(border_strong)s; color: %(text)s; }
    QToolButton:checked {
        border: 2px solid %(accent)s; color: %(text)s;
        background: %(accent_soft)s;
    }
    """ % t


def qss_mode_bar(drop=False) -> str:
    """二维/三维模式切换条（顶部阅读区 chrome）。"""
    t = tokens()
    t = dict(t)
    t["bar_border"] = t["accent"] if drop else t["border"]
    t["bar_width"] = "2px" if drop else "1px"
    return """
    QFrame#modeBar {
        background-color: %(panel)s;
        border: %(bar_width)s solid %(bar_border)s;
        border-radius: 5px;
    }
    QFrame#modeBar QLabel { color: %(text_dim)s; }
    QFrame#modeBar QPushButton {
        background-color: transparent; color: %(text_dim)s;
        border: 1px solid %(border)s; border-radius: 4px;
        padding: 3px 8px; font-size: 10pt;
    }
    QFrame#modeBar QPushButton:hover {
        background-color: %(button_hover)s; color: %(text)s;
        border-color: %(border_strong)s;
    }
    QFrame#modeBar QPushButton:checked {
        background-color: %(accent)s; color: %(on_accent)s;
        border-color: %(accent)s; font-weight: 600;
    }
    QFrame#modeBar QPushButton:disabled {
        color: %(text_muted)s; border-color: %(border)s; background: transparent;
    }
    """ % t


def qss_drop_hint() -> str:
    """拖放悬停时盖在阅读区中央的提示块。"""
    acc = qss_viewer_accent()
    return """
    QLabel {
        color: #FFFFFF;
        background-color: %(bg)s;
        font-size: 14pt; font-weight: bold;
        border: 1px solid %(line)s;
        border-radius: 8px;
        padding: 12px 22px;
    }
    """ % {"bg": _rgba(acc, 0.86), "line": _rgba("#FFFFFF", 0.35)}


def qss_active_border() -> str:
    """多窗时给「当前活动窗口」描一圈强调色。"""
    return "border: 1px solid %s;" % qss_viewer_accent()


def qss_semantic_button(bg_hex: str) -> str:
    """彩色按钮（X 线 AI 的各检测功能，底色保留各自的语义色）。

    v1.21.0 改为**同色调（tonal）**画法：浅色底 + 同色描边 + 同色文字。
    原来是满饱和实心色块，五六个堆在侧栏里像调色板，和「安静、克制」的
    整体气质冲突；tonal 保留了「一眼分辨功能」的色相，同时把视觉重量
    降下来，叠在深底上也不再抢注意力。
    """
    t = tokens()
    base = QtGui.QColor(bg_hex)
    dark = is_dark()
    text_col = base.lighter(142) if dark else base.darker(118)
    fill = _rgba(base.name(), 0.16 if dark else 0.14)
    fill_hover = _rgba(base.name(), 0.26 if dark else 0.22)
    fill_press = _rgba(base.name(), 0.36 if dark else 0.30)
    line = _rgba(base.name(), 0.52 if dark else 0.48)
    return """
    QPushButton {
        background-color: %(fill)s; color: %(text)s;
        border: 1px solid %(line)s; border-radius: 4px;
        padding: 5px 8px; font-weight: 600;
    }
    QPushButton:hover {
        background-color: %(hover)s; border-color: %(text)s;
    }
    QPushButton:pressed { background-color: %(press)s; }
    QPushButton:disabled {
        background-color: %(panel)s; color: %(muted)s;
        border-color: %(border)s; font-weight: normal;
    }
    """ % {"fill": fill, "hover": fill_hover, "press": fill_press,
           "line": line, "text": text_col.name(), "panel": t["panel"],
           "muted": t["text_muted"], "border": t["border"]}


def qss_panel_surface() -> str:
    """侧栏面板底色（比窗口底色略浅/略深一档，做出层次）。"""
    t = tokens()
    return "QWidget#sidePanel { background-color: %s; }" % t["panel"]


# ---------------------------------------------------------------------------
# 绑定 / 刷新
# ---------------------------------------------------------------------------

def bind(widget, factory):
    """登记一个「按当前皮肤重建 QSS」的控件。

    factory 是一个无参函数，返回 QSS 字符串（内部读 ``tokens()``）。
    切皮肤时会被重新调用，因此不要在 factory 里捕获旧的 token。
    """
    if widget is None or factory is None:
        return
    try:
        _bindings.append((weakref.ref(widget), factory))
        widget.setStyleSheet(factory())
    except Exception:              # noqa: BLE001
        pass


def role(widget, name: str):
    """给控件打语义角色（muted / caption / accent / danger / section ...）。

    文字类：muted / dim / caption / accent / danger / warn / success /
            section / title / card / note / chip
    按钮类：primary（唯一主操作）/ tonal（一行里重复出现的行内操作）/
            ghost（次要、几乎无底）/ danger
    """
    if widget is None:
        return widget
    try:
        widget.setProperty("role", name)
        repolish(widget)
    except Exception:              # noqa: BLE001
        pass
    return widget


def repolish(widget):
    try:
        st = widget.style()
        st.unpolish(widget)
        st.polish(widget)
        widget.update()
    except Exception:              # noqa: BLE001
        pass


def _refresh_bindings():
    alive = []
    for ref, factory in _bindings:
        w = ref()
        if w is None:
            continue
        try:
            w.setStyleSheet(factory())
        except RuntimeError:       # 底层 C++ 对象已删除
            continue
        except Exception:          # noqa: BLE001
            pass
        alive.append((ref, factory))
    _bindings[:] = alive


def _refresh_icons():
    try:
        import ui_icons
        ui_icons.refresh_theme_icons()
    except Exception:              # noqa: BLE001
        pass


def on_change(callback):
    """注册皮肤切换回调（例如自绘控件需要重建缓存的图）。

    存弱引用：回调多半是窗口的方法，强引用会让窗口关掉也不释放。
    """
    if not callable(callback):
        return
    try:
        if hasattr(callback, "__self__") and callback.__self__ is not None:
            obj = callback.__self__
            name = callback.__name__

            def _weak(_obj_ref=weakref.ref(obj), _name=name, _key=_state["key"]):
                inst = _obj_ref()
                if inst is None:
                    return
                fn = getattr(inst, _name, None)
                if fn is not None:
                    fn()

            _listeners.append(_weak)
            return
    except Exception:              # noqa: BLE001
        pass
    _listeners.append(callback)


class _Signaller(QtCore.QObject):
    changed = QtCore.pyqtSignal(str)


_signaller = None


def _emit(key):
    global _signaller
    try:
        if _signaller is None:
            _signaller = _Signaller()
        _signaller.changed.emit(key)
    except Exception:              # noqa: BLE001
        pass
    for cb in list(_listeners):
        try:
            cb(key)
        except TypeError:          # 弱引用回调不收参数
            try:
                cb()
            except Exception:      # noqa: BLE001
                pass
        except Exception:          # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def _settings():
    return QtCore.QSettings(ORG, APP)


def saved_key() -> str:
    try:
        v = _settings().value(SETTINGS_KEY, DEFAULT_SKIN, type=str)
        return v if v in SKINS else DEFAULT_SKIN
    except Exception:              # noqa: BLE001
        return DEFAULT_SKIN


def _save(key):
    try:
        _settings().setValue(SETTINGS_KEY, key)
    except Exception:              # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

# 下拉框/菜单用 PE_IndicatorArrow*，数字框用的是另一套 PE_IndicatorSpin*
# （Fusion 的 CC_SpinBox 只认后者）—— 两套都要接管，否则数字框右边
# 只剩两道几乎看不见的细线。
_ARROW_ELEMENTS = (
    QtWidgets.QStyle.PE_IndicatorArrowDown,
    QtWidgets.QStyle.PE_IndicatorArrowUp,
    QtWidgets.QStyle.PE_IndicatorArrowLeft,
    QtWidgets.QStyle.PE_IndicatorArrowRight,
    QtWidgets.QStyle.PE_IndicatorSpinUp,
    QtWidgets.QStyle.PE_IndicatorSpinDown,
)


class _SkinStyle(QtWidgets.QProxyStyle):
    """在 Fusion 之上接管「勾选框 / 单选按钮」两种指示器的绘制。

    为什么非自绘不可（都是实测踩出来的）
    ------------------------------------
    1. Fusion 在深色 palette 下画勾选框：方框的描边取 Button/Dark 色，
       跟我们的深底几乎同色，屏幕上是「只有一个孤零零的对勾」；
    2. 一旦在 QSS 里给 ``::indicator`` 写任何规则，Qt 会切成「QSS 绘制」
       路径，只肯画实心色块，对勾反而没了；
    3. 想要「描边方框 + 精致的对勾」这套观感，又不能用图片资源
       （绿色发行包不许依赖资源文件），只剩 QPainter 自绘一条路。

    只覆盖这两个 primitive，其余全部委托给 Fusion —— 风险面最小。
    """

    def drawPrimitive(self, element, option, painter, widget=None):
        try:
            if element == QtWidgets.QStyle.PE_IndicatorCheckBox:
                self._paint_check(option, painter)
                return
            if element == QtWidgets.QStyle.PE_IndicatorRadioButton:
                self._paint_radio(option, painter)
                return
            if element in _ARROW_ELEMENTS:
                self._paint_arrow(element, option, painter)
                return
        except Exception:          # noqa: BLE001  自绘失败就退回原生，别影响可用性
            pass
        super().drawPrimitive(element, option, painter, widget)

    # ---- 内部 ----
    @staticmethod
    def _state(option):
        on = bool(option.state & QtWidgets.QStyle.State_On)
        partial = bool(option.state & QtWidgets.QStyle.State_NoChange)
        enabled = bool(option.state & QtWidgets.QStyle.State_Enabled)
        return on, partial, enabled

    def _paint_check(self, option, painter):
        t = tokens()
        on, partial, enabled = self._state(option)
        r = QtCore.QRectF(option.rect)
        side = min(r.width(), r.height())
        box = QtCore.QRectF(r.center().x() - side / 2.0,
                            r.center().y() - side / 2.0, side, side)
        box.adjust(1.0, 1.0, -1.0, -1.0)

        accent = qcolor(t["accent"])
        if not enabled:
            accent.setAlpha(110)
        filled = on or partial
        fill = accent if filled else qcolor(t["field"])
        edge = accent if filled else qcolor(t["control_line"])
        if not enabled:
            fill.setAlpha(160 if not filled else 110)
            edge = qcolor(t["border"])

        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtGui.QPen(edge, 1.2))
        painter.setBrush(fill)
        painter.drawRoundedRect(box, 3.5, 3.5)

        if filled:
            pen = QtGui.QPen(qcolor("#FFFFFF"), max(1.5, side * 0.12),
                             QtCore.Qt.SolidLine, QtCore.Qt.RoundCap,
                             QtCore.Qt.RoundJoin)
            if not enabled:
                c = pen.color()
                c.setAlpha(170)
                pen.setColor(c)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            if partial:                      # 半选：一根横杠
                painter.drawLine(QtCore.QPointF(box.left() + side * 0.26,
                                                box.center().y()),
                                 QtCore.QPointF(box.right() - side * 0.26,
                                                box.center().y()))
            else:                            # 全选：对勾
                painter.drawPolyline(QtGui.QPolygonF([
                    QtCore.QPointF(box.left() + side * 0.26, box.top() + side * 0.52),
                    QtCore.QPointF(box.left() + side * 0.44, box.top() + side * 0.70),
                    QtCore.QPointF(box.left() + side * 0.76, box.top() + side * 0.30),
                ]))
        painter.restore()

    def _paint_arrow(self, element, option, painter):
        """自绘小三角箭头（下拉框 / 数字框 / 子菜单）。

        Fusion 的箭头用的是 palette Button 色，深色皮肤下跟输入框底几乎
        同色 —— 实测「数字框右边只有两道几乎看不见的细线」。这里统一
        用 text_dim 画等边小三角，四套皮肤下都清晰。
        """
        t = tokens()
        r = QtCore.QRectF(option.rect)
        if r.width() < 3 or r.height() < 3:
            return
        enabled = bool(option.state & QtWidgets.QStyle.State_Enabled)
        col = qcolor(t["text_dim"] if enabled else t["border_strong"])
        side = min(r.width(), r.height())
        w = max(5.0, min(9.0, side * 0.72))
        h = w * 0.60
        cx, cy = r.center().x(), r.center().y()
        if element in (QtWidgets.QStyle.PE_IndicatorArrowDown,
                       QtWidgets.QStyle.PE_IndicatorSpinDown):
            pts = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                   (cx, cy + h / 2)]
        elif element in (QtWidgets.QStyle.PE_IndicatorArrowUp,
                         QtWidgets.QStyle.PE_IndicatorSpinUp):
            pts = [(cx - w / 2, cy + h / 2), (cx + w / 2, cy + h / 2),
                   (cx, cy - h / 2)]
        elif element == QtWidgets.QStyle.PE_IndicatorArrowLeft:
            pts = [(cx + h / 2, cy - w / 2), (cx + h / 2, cy + w / 2),
                   (cx - h / 2, cy)]
        else:                                    # PE_IndicatorArrowRight
            pts = [(cx - h / 2, cy - w / 2), (cx - h / 2, cy + w / 2),
                   (cx + h / 2, cy)]
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(col)
        painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in pts]))
        painter.restore()

    def _paint_radio(self, option, painter):
        t = tokens()
        on, _partial, enabled = self._state(option)
        r = QtCore.QRectF(option.rect)
        side = min(r.width(), r.height())
        circle = QtCore.QRectF(r.center().x() - side / 2.0,
                               r.center().y() - side / 2.0, side, side)
        circle.adjust(1.4, 1.4, -1.4, -1.4)

        accent = qcolor(t["accent"])
        edge = accent if on else qcolor(t["control_line"])
        if not enabled:
            accent.setAlpha(110)
            edge = accent if on else qcolor(t["border"])

        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtGui.QPen(edge, 1.6 if on else 1.2))
        painter.setBrush(qcolor(t["field"]) if not on else QtCore.Qt.NoBrush)
        painter.drawEllipse(circle)
        if on:
            dot = circle.adjusted(side * 0.26, side * 0.26,
                                  -side * 0.26, -side * 0.26)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(dot)
        painter.restore()


_style_installed = False


def install_style(app=None):
    """把 _SkinStyle 装成应用样式（Fusion 之上的一层，幂等）。"""
    global _style_installed
    app = app or QtWidgets.QApplication.instance()
    if app is None or _style_installed:
        return
    try:
        app.setStyle(_SkinStyle("Fusion"))
        _style_installed = True
    except Exception:              # noqa: BLE001
        _style_installed = False


def apply(app=None, key: str = None) -> str:
    """应用皮肤（幂等）。返回实际生效的皮肤键。

    Args:
        app: QApplication；省略时取当前实例。
        key: 皮肤键；省略时沿用当前（首次为上次保存的选择）。
    """
    app = app or QtWidgets.QApplication.instance()
    if app is None:
        return current()
    if key is None:
        key = _state.get("key") or saved_key()
    if key not in SKINS:
        key = DEFAULT_SKIN
    _state["app"] = app
    _state["key"] = key
    install_style(app)
    app.setPalette(palette(key))
    app.setStyleSheet(qss(key))
    _refresh_bindings()
    _refresh_icons()
    _save(key)
    _emit(key)
    return key


def switch(key: str) -> str:
    """切换皮肤并立刻生效。"""
    key = key if key in SKINS else DEFAULT_SKIN
    if key == current() and _state.get("app") is not None:
        # 同一套也重建一次：防止外部改动过样式表
        pass
    return apply(_state.get("app"), key)


# ---------------------------------------------------------------------------
# 皮肤切换器（一键切换的入口）
# ---------------------------------------------------------------------------

def _ui_font(size_pt, weight=QtGui.QFont.Normal):
    f = QtGui.QFont("Microsoft YaHei", int(size_pt))
    f.setWeight(weight)
    return f


def _chip_pixmap(key: str, w=22, h=13) -> QtGui.QPixmap:
    """把一套皮肤画成「三色色片」，用作按钮与列表里的缩略标识。"""
    skin = SKINS.get(key, SKINS[DEFAULT_SKIN])
    pm = QtGui.QPixmap(int(w) * 2, int(h) * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    path = QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(0.5, 0.5, w - 1, h - 1), 3.5, 3.5)
    p.setClipPath(path)
    p.fillRect(QtCore.QRectF(0, 0, w, h), qcolor(skin["canvas"]))
    p.fillRect(QtCore.QRectF(w * 0.34, 0, w * 0.34, h), qcolor(skin["panel"]))
    p.fillRect(QtCore.QRectF(w * 0.68, 0, w * 0.32, h), qcolor(skin["accent"]))
    p.setClipping(False)
    p.setPen(QtGui.QPen(qcolor(skin["border_strong"]), 1))
    p.setBrush(QtCore.Qt.NoBrush)
    p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, w - 1, h - 1), 3.5, 3.5)
    p.end()
    return pm


class _SkinPopup(QtWidgets.QWidget):
    """皮肤选择浮层：一行一套皮肤（色片 + 名字 + 一句话说明 + 选中勾）。

    自绘而不是堆控件：四行内容固定、宽度固定，自绘能把字距、色片圆角、
    选中勾的位置都抠准，也不会被全局 QSS 的按钮内边距带偏。点一行立即
    生效并保持浮层打开，方便来回比对 —— 这才是「一键切换」该有的手感。
    """

    W = 286
    ROW_H = 56
    HEIGHT = 12 + 22 + 6 + ROW_H * len(_ORDER) + 4

    switched = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint
                         | QtCore.Qt.NoDropShadowWindowHint)
        self.setMouseTracking(True)
        self.setFixedSize(self.W, self.HEIGHT)
        self._hover = -1

    # ---- 几何 ----
    def _row_rect(self, i):
        return QtCore.QRect(8, 40 + i * self.ROW_H, self.W - 16, self.ROW_H - 4)

    def _index_at(self, pos):
        for i in range(len(_ORDER)):
            if self._row_rect(i).contains(pos):
                return i
        return -1

    # ---- 交互 ----
    def mouseMoveEvent(self, ev):
        i = self._index_at(ev.pos())
        if i != self._hover:
            self._hover = i
            self.setCursor(QtCore.Qt.PointingHandCursor if i >= 0
                           else QtCore.Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, ev):
        self._hover = -1
        self.update()

    def mouseReleaseEvent(self, ev):
        i = self._index_at(ev.pos())
        if i >= 0 and ev.button() == QtCore.Qt.LeftButton:
            key = _ORDER[i]
            switch(key)
            self.switched.emit(key)
            self.update()
        else:
            self.close()

    def keyPressEvent(self, ev):
        if ev.key() in (QtCore.Qt.Key_Escape, QtCore.Qt.Key_Return):
            self.close()

    def show_at(self, global_pos):
        """在按钮下方展开，并保证不越出屏幕。"""
        scr = QtWidgets.QApplication.screenAt(global_pos) if hasattr(
            QtWidgets.QApplication, "screenAt") else None
        if scr is None:
            scr = QtWidgets.QApplication.primaryScreen()
        avail = scr.availableGeometry()
        x = min(max(avail.left() + 6, global_pos.x() - self.W + 24),
                avail.right() - self.W - 6)
        y = global_pos.y() + 6
        if y + self.height() > avail.bottom():
            y = global_pos.y() - self.height() - 6
        self.move(x, max(avail.top() + 6, y))
        self.show()
        self.raise_()
        self.setFocus()

    # ---- 绘制 ----
    def paintEvent(self, ev):
        t = tokens()
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        # 底板：不透明圆角矩形（不依赖窗口透明合成，Win7 关掉 Aero 也不发虚）
        p.setPen(QtGui.QPen(qcolor(t["border_strong"]), 1))
        p.setBrush(qcolor(t["raised"]))
        p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, self.W - 1, self.HEIGHT - 1), 8, 8)

        # 标题行
        p.setPen(qcolor(t["text"]))
        p.setFont(_ui_font(9.5, QtGui.QFont.DemiBold))
        p.drawText(QtCore.QRect(16, 11, 120, 20),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, "界面皮肤")
        p.setPen(qcolor(t["text_muted"]))
        p.setFont(_ui_font(8))
        p.drawText(QtCore.QRect(self.W - 130, 11, 114, 20),
                   QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, "点击即生效")

        cur = current()
        for i, key in enumerate(_ORDER):
            skin = SKINS[key]
            r = self._row_rect(i)
            selected = (key == cur)

            if selected:
                p.setPen(QtGui.QPen(qcolor(t["accent_line"]), 1))
                p.setBrush(_color_with_alpha(t["accent"], 0.10))
                p.drawRoundedRect(QtCore.QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
            elif i == self._hover:
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(qcolor(t["button_hover"]))
                p.drawRoundedRect(QtCore.QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

            # 色片
            chip = QtCore.QRect(r.left() + 10, r.top() + (r.height() - 34) // 2, 58, 34)
            p.setPen(QtGui.QPen(qcolor(t["border"]), 1))
            p.setBrush(qcolor(skin["canvas"]))
            p.drawRoundedRect(QtCore.QRectF(chip), 5, 5)
            inner = QtCore.QRect(chip.x() + 6, chip.y() + 7, chip.width() - 12,
                                 chip.height() - 14)
            p.setPen(QtGui.QPen(qcolor(skin["border"]), 1))
            p.setBrush(qcolor(skin["panel"]))
            p.drawRoundedRect(QtCore.QRectF(inner), 3, 3)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(qcolor(skin["accent"]))
            p.drawRoundedRect(QtCore.QRectF(inner.x() + 4, inner.y() + 4,
                                            inner.width() - 26, 4), 2, 2)
            p.setBrush(qcolor(skin["button"]))
            p.drawRoundedRect(QtCore.QRectF(inner.x() + 4, inner.y() + 11,
                                            inner.width() - 14, 3), 1.5, 1.5)

            # 名字 + 说明
            tx = chip.right() + 12
            p.setPen(qcolor(t["text"]))
            p.setFont(_ui_font(9.5, QtGui.QFont.DemiBold))
            p.drawText(QtCore.QRect(tx, r.top() + 9, r.width() - 120, 18),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, skin["label"])
            p.setPen(qcolor(t["text_muted"]))
            p.setFont(_ui_font(8))
            p.drawText(QtCore.QRect(tx, r.top() + 27, r.width() - 80, 16),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, skin["desc"])

            # 选中勾
            if selected:
                cx, cy = r.right() - 20, r.center().y()
                p.setPen(QtGui.QPen(qcolor(t["accent"]), 1.8,
                                    QtCore.Qt.SolidLine, QtCore.Qt.RoundCap,
                                    QtCore.Qt.RoundJoin))
                p.drawPolyline(QtGui.QPolygonF([
                    QtCore.QPointF(cx - 5, cy + 0.5),
                    QtCore.QPointF(cx - 1.5, cy + 4),
                    QtCore.QPointF(cx + 5, cy - 4.5),
                ]))
        p.end()


def _color_with_alpha(hex_color, alpha):
    c = QtGui.QColor(hex_color)
    c.setAlpha(max(0, min(255, int(round(alpha * 255)))))
    return c


class SkinSwitcher(QtWidgets.QToolButton):
    """「皮肤」按钮 + 一键切换浮层。放进任意工具栏/侧栏即可用。"""

    def __init__(self, parent=None, text="皮肤"):
        super().__init__(parent)
        self.setText(text)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.setIconSize(QtCore.QSize(22, 13))
        self.setPopupMode(QtWidgets.QToolButton.DelayedPopup)
        self.setToolTip("界面皮肤：点一下挑一套，立即生效，不用重启")
        self.setFont(_ui_font(9))
        self.clicked.connect(self._show_popup)
        self._popup = None
        self.refresh()

    def refresh(self):
        """按当前皮肤刷新按钮上的色片与文字。"""
        self.setIcon(QtGui.QIcon(_chip_pixmap(current(), 22, 13)))
        self.setToolTip("界面皮肤：当前「%s」" % display_name()
                        + "\n点一下切换，立即生效，不用重启")
        repolish(self)

    def _show_popup(self):
        if self._popup is not None:
            try:
                self._popup.close()
                self._popup.deleteLater()
            except Exception:      # noqa: BLE001
                pass
        self._popup = _SkinPopup(self)
        self._popup.switched.connect(self._on_switched)
        gp = self.mapToGlobal(QtCore.QPoint(self.width(), self.height()))
        self._popup.show_at(gp)

    def _on_switched(self, _key):
        self.refresh()
