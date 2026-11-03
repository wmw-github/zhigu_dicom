# -*- coding: utf-8 -*-
"""报告质控悬浮小窗口（**常驻形态**）。

交互模型（v1.16.0 起）
---------------------
程序启动即自动出现（不用点任何按钮）；首次用「设置区域」框选一次，
区域存进 QSettings，之后每次打开新报告只需：
**点「刷新」或双击小窗口** → 按保存的区域截图 → OCR → 质控 → 显示。
患者姓名/性别年龄/影像号常驻展示在窗口上方的信息卡里。

设计目标：**医生在 PACS 里写报告时，这个小窗浮在 PACS 上面**，
一眼看出「是不是这个患者」+「报告有没有问题」。因此：

* 无边框（``FramelessWindowHint``）+ 置顶（``WindowStaysOnTopHint``）
  + ``Qt.Tool``（不出现在任务栏，不抢主程序焦点）；
* 配色跟随主程序皮肤（v1.21.0 起）：深色皮肤下是深色玻璃小窗，浅色皮肤下
  是浅底小窗；切换皮肤即时跟随（见 ``_apply_skin``）。为了让它压在任何
  PACS 界面上都「一眼看出是浮层」，窗口始终保留 1px 描边，标题栏与底栏
  始终比内容区深一档；
* 标题栏可拖动整窗；双击窗口空白处 = 刷新；点问题条目展开/收起详情；
* 患者信息卡 + 状态色块 + 摘要一行 + 问题列表 + 动作按钮。

颜色口径（与 :mod:`reportqc.pacs.pipeline` 的 LEVEL_COLORS 一致）
---------------------------------------------------------------
* ``#dc3545`` 红   —— 危急值
* ``#e8850c`` 橙   —— 高危错别字（需修改）
* ``#d8b90a`` 黄   —— 中低危问题 / 字段缺失（需复核）
* ``#28a745`` 绿   —— 未发现问题

Win7 兼容
---------
半透明用 ``setWindowOpacity``（不用 ``WA_TranslucentBackground`` +
``QPainter`` 圆角方案，后者在 Win7 关闭 Aero 时会露出黑角）；
不用任何 Qt6 API；全部控件为 Qt5 基础类。
"""

import PyQt5.QtCore as QtCore
import PyQt5.QtGui as QtGui
import PyQt5.QtWidgets as QtWidgets

import theme
from ..pacs.pipeline import LEVEL_COLORS, LEVEL_CRITICAL, LEVEL_HIGH, LEVEL_MEDIUM, LEVEL_LOW
from ..pacs.fields import format_demo
from .skin import lazy_color

__all__ = ["MiniReportWindow"]

# ---------------------------------------------------------------------------
# 配色
#
# 全部走 lazy_color：模块级常量在 **用的时候** 才向 theme 取当前皮肤色，
# 所以小窗在浅色皮肤下不再是「一块深色孤岛」，切皮肤后重新打开/重刷也跟得上。
# 只保留语义状态色（LEVEL_COLORS）不变 —— 红/橙/黄/绿表达的是结论等级，
# 跨皮肤必须一致，跟着皮肤变反而让人误判。
# ---------------------------------------------------------------------------

C_BG = lazy_color("canvas", "#1e2022")
C_BG2 = lazy_color("panel", "#26282b")
C_BORDER = lazy_color("border", "#3d3f41")
C_TEXT = lazy_color("text", "#e8eaec")
C_TEXT_DIM = lazy_color("text_dim", "#9aa0a6")
C_TEXT_MUTED = lazy_color("text_muted", "#6b7178")
C_ACCENT = lazy_color("accent", "#2a82da")
C_ON_ACCENT = lazy_color("on_accent", "#ffffff")
C_BTN = lazy_color("button", "#3d3f41")
C_BTN_HOVER = lazy_color("button_hover", "#4d4f51")
C_BTN_LINE = lazy_color("border_strong", "#5d5f61")
C_PILL = lazy_color("border_strong", "#3d3f41")
C_SCROLL = lazy_color("scroll", "#4a4d50")
C_LINK = lazy_color("accent", "#8ab4f8")
C_TAG_BG = lazy_color("accent_soft", "#2a3a55")
C_WARN = lazy_color("warning", "#d8b90a")
C_DANGER = lazy_color("danger", "#dc3545")
C_SUCCESS = lazy_color("success", "#28a745")

# 标题栏胶囊文案（v1.18.2 起状态条并入标题栏，一句话说完，不再占 44px）
_LEVEL_BADGE = {
    "red": "危急",
    "orange": "修改",
    "yellow": "复核",
    "green": "通过",
}


class _IssueItem(QtWidgets.QWidget):
    """问题列表中的一条。点击在「摘要」和「详情」之间切换。"""

    def __init__(self, issue, parent=None):
        super().__init__(parent)
        self._issue = issue
        self._expanded = False
        self.setObjectName("issueItem")
        self.setStyleSheet(
            "QWidget#issueItem { background:%s; border-radius:3px; }" % C_BG2)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(2)

        # -- 第一行：色点 + 消息 -----------------------------------------
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(6)

        dot = QtWidgets.QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(
            "background:%s; border-radius:5px; border:none;" % (
                LEVEL_COLORS.get(_color_key(issue.level), "#888888")))
        top.addWidget(dot, 0, QtCore.Qt.AlignTop)

        self._msg = QtWidgets.QLabel(issue.message)
        self._msg.setWordWrap(True)
        self._msg.setStyleSheet(
            "color:%s; background:transparent; border:none; font-size:12px;" % C_TEXT)
        top.addWidget(self._msg, 1)

        if issue.ocr_suspect:
            tag = QtWidgets.QLabel("识别存疑")
            tag.setStyleSheet(
                "color:%s; background:%s; border:none;"
                " border-radius:2px; padding:1px 4px; font-size:10px;"
                % (C_LINK, C_TAG_BG))
            top.addWidget(tag, 0, QtCore.Qt.AlignTop)

        root.addLayout(top)

        # -- 详情（默认隐藏） --------------------------------------------
        self._detail = QtWidgets.QWidget()
        dv = QtWidgets.QVBoxLayout(self._detail)
        dv.setContentsMargins(16, 2, 0, 0)
        dv.setSpacing(2)
        if issue.suggestion:
            lb = QtWidgets.QLabel("建议：%s" % issue.suggestion)
            lb.setWordWrap(True)
            lb.setStyleSheet("color:%s; background:transparent; border:none; font-size:11px;"
                             % C_TEXT_DIM)
            dv.addWidget(lb)
        if issue.source:
            lb2 = QtWidgets.QLabel("原文：%s" % _shorten(issue.source, 90))
            lb2.setWordWrap(True)
            lb2.setStyleSheet("color:%s; background:transparent; border:none;"
                              " font-size:11px;" % C_TEXT_MUTED)
            dv.addWidget(lb2)
        conf_bits = []
        if issue.confidence:
            conf_bits.append("规则置信度 %.2f" % issue.confidence)
        if issue.ocr_conf is not None:
            conf_bits.append("OCR 置信度 %.2f" % issue.ocr_conf)
        if conf_bits:
            lb3 = QtWidgets.QLabel(" · ".join(conf_bits))
            lb3.setStyleSheet("color:%s; background:transparent; border:none;"
                              " font-size:10px;" % C_TEXT_MUTED)
            dv.addWidget(lb3)
        self._detail.setVisible(False)
        root.addWidget(self._detail)

    def mousePressEvent(self, event):
        self._expanded = not self._expanded
        self._detail.setVisible(self._expanded)
        self.adjustSize()
        self.updateGeometry()
        w = self.window()
        if w is not None and hasattr(w, "_sync_size"):
            w._sync_size()
        super().mousePressEvent(event)


class _CopyLabel(QtWidgets.QLabel):
    """单击即复制的小标签（影像号行用）。

    v1.19.1：医生要拿影像号去 PACS 里搜患者，手抄 14 位数字容易错，
    点一下就进剪贴板。悬停由 stylesheet 的 ``QLabel:hover`` 变亮 + 手型光标。
    """

    clicked = QtCore.pyqtSignal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(QtCore.Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _color_key(level):
    """issue.level（critical/high/...）→ 颜色键（red/orange/...）。"""
    return {
        LEVEL_CRITICAL: "red",
        LEVEL_HIGH: "orange",
        LEVEL_MEDIUM: "yellow",
        LEVEL_LOW: "yellow",
    }.get(level, "yellow")


def _shorten(text, n):
    t = (text or "").replace("\n", " ").strip()
    return t if len(t) <= n else t[:n] + "…"


def _join_unique(bits):
    """按顺序拼接片段，丢掉被更长片段包含的那些。

    「CT」+「胸部」+「胸部CT平扫（薄层）」→「胸部CT平扫（薄层）」：
    体检登记软件里检查部位与检查项目本来就有大段重叠，照原样拼会在患者卡
    同一行里出现「胸部 · 胸部CT平扫」，既啰嗦又更容易超宽折行。
    """
    vals = [b for b in bits if b]
    out = []
    for b in vals:
        if any(b != o and b in o for o in vals):
            continue                     # 已被更长的片段覆盖
        if b not in out:
            out.append(b)
    return " · ".join(out)


def _contrast_fg(bg):
    """按背景色亮度选前景色（浅底用深字，深底用白字）。"""
    try:
        r = int(bg[1:3], 16)
        g = int(bg[3:5], 16)
        b = int(bg[5:7], 16)
    except (ValueError, IndexError, TypeError):
        return "white"
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return C_BG if lum > 0.62 else "white"


class MiniReportWindow(QtWidgets.QWidget):
    """悬浮小窗口主体（常驻形态）。

    使用方式（用户视角）
    -------------------
    程序启动即自动出现 → 首次点「设置区域」框选一次 PACS 报告区 →
    之后每次打开新报告，点「刷新」或**双击本窗口**即自动识别并显示
    患者信息与质控结论，**不再需要重复框选**。

    Signals
    -------
    request_recheck()
        刷新识别（用已保存的区域截图）。按钮「刷新」和双击窗口都发这个。
    request_repick()
        重新设置识别区域（框选一次并保存）。
    request_detail()
        打开详情窗（原文定位 / 误判标记）。
    request_history()
        打开历史记录窗。
    request_pdf()
        打开报告 PDF 导出窗（列举 C:\\temp 的 PDF 并导出到桌面）。
    """

    request_recheck = QtCore.pyqtSignal()
    request_repick = QtCore.pyqtSignal()
    request_detail = QtCore.pyqtSignal()
    request_history = QtCore.pyqtSignal()
    request_pdf = QtCore.pyqtSignal()

    W = 430
    H = 470

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
        )
        self.setWindowTitle("报告质控")
        # 样式统一在 _apply_skin() 里刷（切皮肤要重新刷一遍）。
        # 必须用 #objectName 限定：无选择器的样式表是万能选择器，
        # 会把子控件一起染色且优先级高于应用级样式表（踩过）。
        self.setObjectName("miniRoot")
        self.setMinimumSize(320, 188)
        self.resize(self.W, self.H)

        self._drag_pos = None
        self._record_id = None
        self._copy_no = ""            # 当前可复制的影像号/检查号
        self._copy_kind = "影像号"
        self._flash_saved = None      # 标题栏临时提示前保存的原摘要
        self._status_full = ""
        self._status_color = C_TEXT_DIM
        self._badge_text = "就绪"
        self._badge_bg = None
        self._badge_fg = None
        # 患者卡三行「未截断原文」，实际显示的是它的省略版（见 _apply_patient_text）
        self._pt_name_full = "—"
        self._pt_demo_full = ""
        self._pt_meta_full = ""
        self._pt_exam_full = ""
        self._build()
        self._apply_skin()
        # 切皮肤后即时跟随：不重建窗口（会丢当前结论），只重刷配色
        try:
            theme.on_change(self._apply_skin)
        except Exception:                              # noqa: BLE001
            pass

        # 窗口位置持久化到 QSettings（注册表 HKCU，Win7 可用且总是可写）。
        # 用 QSettings 而不是 app_settings.json / SQLite：位置是纯 UI 状态，
        # 不该跟业务数据混在一起；且库文件在软件目录，只读环境下会写失败。
        self._restore_pos()

    _last_pos = None

    # ------------------------------------------------------------------
    # 位置持久化
    # ------------------------------------------------------------------

    _SETTINGS_ORG = "ZhiGu"
    _SETTINGS_APP = "CT3D"

    def _restore_pos(self):
        try:
            s = QtCore.QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)
            x = s.value("reportqc/mini_x")
            y = s.value("reportqc/mini_y")
            if x is not None and y is not None:
                pos = QtCore.QPoint(int(x), int(y))
                screen = QtWidgets.QApplication.primaryScreen()
                if screen is not None and screen.availableGeometry().contains(pos):
                    self.move(pos)
        except Exception:                              # noqa: BLE001
            pass

    def _save_pos(self):
        try:
            s = QtCore.QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)
            s.setValue("reportqc/mini_x", int(self.x()))
            s.setValue("reportqc/mini_y", int(self.y()))
        except Exception:                              # noqa: BLE001
            pass

    # ------------------------------------------------------------------

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- 标题栏（状态胶囊并入这里，省掉原来 44px 的独立状态条） ---------
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(30)
        bar.setObjectName("miniBar")
        bl = QtWidgets.QHBoxLayout(bar)
        bl.setContentsMargins(10, 0, 4, 0)
        bl.setSpacing(6)

        title = QtWidgets.QLabel("报告质控")
        self._title = title
        bl.addWidget(title)

        # 状态胶囊：「就绪 / ··· / 危急 / 修改 / 复核 / 通过 / 未设置 / 错误」
        self._badge = QtWidgets.QLabel("就绪")
        self._badge.setFixedHeight(17)
        self._badge.setAlignment(QtCore.Qt.AlignCenter)
        self._badge.setSizePolicy(QtWidgets.QSizePolicy.Fixed,
                                  QtWidgets.QSizePolicy.Fixed)
        bl.addWidget(self._badge)

        # 状态说明：一句话小字（问题计数 / 操作提示），超宽省略，全文进 tooltip
        self._status_text = QtWidgets.QLabel("")
        bl.addWidget(self._status_text, 1)

        close_btn = QtWidgets.QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setToolTip("关闭小窗（可从主程序左栏「报告质控」重新打开）")
        self._close_x = close_btn
        close_btn.clicked.connect(self.close)
        bl.addWidget(close_btn, 0)

        root.addWidget(bar)
        self._bar = bar
        self._set_badge("就绪")

        # -- 患者信息卡（核心展示区） -------------------------------------
        # 医生最关心「这是不是当前患者的报告」，所以姓名给最大字号，
        # 性别/年龄右对齐同排，影像号/检查号放第二三行小字。
        card_host = QtWidgets.QWidget()
        chl = QtWidgets.QVBoxLayout(card_host)
        chl.setContentsMargins(8, 6, 8, 0)
        chl.setSpacing(0)

        self._patient_card = QtWidgets.QFrame()
        self._patient_card.setObjectName("patientCard")
        pv = QtWidgets.QVBoxLayout(self._patient_card)
        pv.setContentsMargins(10, 7, 10, 7)
        pv.setSpacing(2)

        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(8)
        self._pt_name = QtWidgets.QLabel("—")
        row1.addWidget(self._pt_name, 1)
        self._pt_demo = QtWidgets.QLabel("")
        row1.addWidget(self._pt_demo, 0)
        pv.addLayout(row1)

        # 影像号行：点一下就复制（v1.19.1）—— 医生要拿它在 PACS 里搜人，
        # 手抄 14 位数字容易错。悬停变手型 + 高亮，点击复制到剪贴板。
        self._pt_meta = _CopyLabel("")
        self._pt_meta.clicked.connect(self._copy_accession)
        pv.addWidget(self._pt_meta)

        # 第三行：设备 / 部位 / 项目 / 时间。**必须单行**（不 wordWrap）：
        # 卡片高度是按 sizeHint 固定的，一旦这行折成两行，第二行就会画到
        # 卡片外面压住下方列表（v1.20.1 用户实机反馈的「文字重复、被遮挡」）。
        # 超长用省略号，全文进 tooltip。
        self._pt_exam = QtWidgets.QLabel("")
        self._pt_exam.setWordWrap(False)
        pv.addWidget(self._pt_exam)

        chl.addWidget(self._patient_card)
        root.addWidget(card_host)
        self._card_host = card_host

        # -- 问题列表 -----------------------------------------------------
        self._list_host = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(self._list_host)
        lv.setContentsMargins(8, 6, 8, 6)
        lv.setSpacing(4)

        self._empty_label = QtWidgets.QLabel("")
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(QtCore.Qt.AlignCenter)
        lv.addWidget(self._empty_label)

        self._items_layout = QtWidgets.QVBoxLayout()
        self._items_layout.setSpacing(4)
        lv.addLayout(self._items_layout)
        lv.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(self._list_host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setObjectName("miniScroll")
        root.addWidget(scroll, 1)
        self._scroll = scroll

        # -- 底部按钮 -----------------------------------------------------
        btn_bar = QtWidgets.QWidget()
        btn_bar.setFixedHeight(42)
        btn_bar.setObjectName("miniBtnBar")
        self._btn_bar = btn_bar
        bvl = QtWidgets.QHBoxLayout(btn_bar)
        bvl.setContentsMargins(6, 5, 6, 5)
        bvl.setSpacing(4)

        self._recheck_btn = QtWidgets.QPushButton("刷新")
        self._recheck_btn.setToolTip("按已设置的区域重新识别（也可双击本窗口）")
        self._repick_btn = QtWidgets.QPushButton("设置区域")
        self._repick_btn.setToolTip("重新框选 PACS 报告区域，只需设置一次")
        self._detail_btn = QtWidgets.QPushButton("详情")
        self._history_btn = QtWidgets.QPushButton("历史")
        self._pdf_btn = QtWidgets.QPushButton("PDF")
        self._pdf_btn.setToolTip("列举 C:\\temp 的 PDF 并导出到桌面（按患者姓名重命名）")
        self._close_btn = QtWidgets.QPushButton("关闭")
        self._small_btns = (self._repick_btn, self._detail_btn,
                            self._history_btn, self._pdf_btn, self._close_btn)
        self._recheck_btn.clicked.connect(self.request_recheck.emit)
        self._repick_btn.clicked.connect(self.request_repick.emit)
        self._detail_btn.clicked.connect(self.request_detail.emit)
        self._history_btn.clicked.connect(self.request_history.emit)
        self._pdf_btn.clicked.connect(self.request_pdf.emit)
        self._close_btn.clicked.connect(self.close)
        # 没有记录编号（数据库不可用）时禁用详情
        self._detail_btn.setEnabled(False)
        bvl.addWidget(self._recheck_btn)
        bvl.addWidget(self._repick_btn)
        bvl.addWidget(self._detail_btn)
        bvl.addWidget(self._history_btn)
        bvl.addWidget(self._pdf_btn)
        bvl.addStretch(1)
        bvl.addWidget(self._close_btn)
        root.addWidget(btn_bar)

    # ------------------------------------------------------------------
    # 配色刷新
    # ------------------------------------------------------------------

    def _apply_skin(self, *_args):
        """把当前皮肤配色刷到小窗上。

        构建完成后调一次；切换皮肤时（``theme.on_change``）再调一次。
        刻意只重刷样式、不重建窗口 —— 重建会丢掉当前患者的质控结论，
        医生换皮肤时正看着报告，不能让他再刷一次。
        """
        try:
            self.setStyleSheet(
                "QWidget#miniRoot { background:%s; border:1px solid %s; }"
                % (C_BG, C_BORDER))
            self._bar.setStyleSheet(
                "QWidget#miniBar { background:%s; border:none; }" % C_BG2)
            self._title.setStyleSheet(
                "color:%s; background:transparent; border:none;"
                " font-size:11px; font-weight:bold;" % C_TEXT)
            # padding 必须显式归零：全局皮肤的 QPushButton 带 4px 11px 内边距，
            # 20×20 的定宽按钮被内边距吃光后「×」会被裁掉只剩一个点。
            self._close_x.setStyleSheet(
                "QPushButton { background:transparent; color:%s; border:none;"
                " padding:0px; font-size:15px; }"
                "QPushButton:hover { color:%s; }" % (C_TEXT_DIM, C_DANGER))
            self._patient_card.setStyleSheet(
                "QFrame#patientCard { background:%s; border:none;"
                " border-radius:3px; }" % C_BG2)
            self._pt_name.setStyleSheet(
                "color:%s; background:transparent; border:none;"
                " font-size:16px; font-weight:bold;" % C_TEXT)
            self._pt_demo.setStyleSheet(
                "color:%s; background:transparent; border:none;"
                " font-size:12px;" % C_TEXT)
            self._pt_meta.setStyleSheet(
                "QLabel { color:%s; background:transparent; border:none;"
                " font-size:11px; }"
                "QLabel:hover { color:%s; }" % (C_TEXT_DIM, C_LINK))
            # 10px 的宋体在 Windows 上笔画会糊成一团（底部竖钩常常看不见，
            # 看起来像「字被切了一半」），最小用 11px + 亮一档的灰。
            self._pt_exam.setStyleSheet(
                "color:%s; background:transparent; border:none;"
                " font-size:11px;" % C_TEXT_DIM)
            # 中文在这个字体下 ascent+descent 恰好等于字号，行高取等值会把
            # 字的上下各切掉一点（看起来像「姓名上一半被盖住」）。每行按字体
            # 实测高度留 4~6px 余量，行间距就正常了。
            for _lb, _pad in ((self._pt_name, 6), (self._pt_demo, 5),
                              (self._pt_meta, 5), (self._pt_exam, 5)):
                _lb.setMinimumHeight(_lb.fontMetrics().height() + _pad)
            self._empty_label.setStyleSheet(
                "color:%s; background:transparent; border:none;"
                " font-size:12px;" % C_TEXT_DIM)
            self._scroll.setStyleSheet(
                "QScrollArea { background:transparent; border:none; }"
                "QScrollBar:vertical { background:%s; width:8px; }"
                "QScrollBar::handle:vertical { background:%s;"
                " border-radius:4px; }" % (C_BG, C_SCROLL))
            self._btn_bar.setStyleSheet(
                "QWidget#miniBtnBar { background:%s; border:none; }" % C_BG2)

            # 小按钮：紧凑尺寸由 padding 决定，保留原值免得底栏变高
            small = (
                "QPushButton { background:%s; color:%s; border:1px solid %s;"
                " border-radius:2px; padding:4px 8px; font-size:11px; }"
                "QPushButton:hover { background:%s; }"
                "QPushButton:disabled { color:%s; border-color:%s; }"
                % (C_BTN, C_TEXT, C_BTN_LINE, C_BTN_HOVER,
                   C_TEXT_MUTED, C_BORDER))
            for b in self._small_btns:
                b.setStyleSheet(small)
            # 「刷新」是主操作，用强调色，一眼能认出来
            self._recheck_btn.setStyleSheet(
                "QPushButton { background:%s; color:%s; border:1px solid %s;"
                " border-radius:2px; padding:4px 14px; font-size:11px;"
                " font-weight:bold; }"
                "QPushButton:hover { background:%s; }"
                "QPushButton:disabled { background:%s; color:%s;"
                " border-color:%s; }"
                % (C_ACCENT, C_ON_ACCENT, C_ACCENT, C_ACCENT,
                   C_BTN, C_TEXT_MUTED, C_BORDER))

            # 状态文字与胶囊按「当前状态」重刷（不能重置成就绪）
            self._set_status(self._status_full, self._status_color)
            self._set_badge(self._badge_text, self._badge_bg, self._badge_fg)

            # 底栏按钮在窄窗口下会被挤到互相压字（文字被切一半看起来像错别字）。
            # 把窗口最小宽度提到「六个按钮 + 间距 + 边距」的实际需要宽度，
            # 让 Qt 宁可让窗口宽一点，也不去压按钮文字。
            blay = self._btn_bar.layout()
            if blay is not None:
                blay.activate()
                need = self._btn_bar.sizeHint().width() + 4
                if need > self.minimumWidth():
                    self.setMinimumWidth(need)
        except Exception as exc:                       # noqa: BLE001
            print("[reportqc] 小窗配色刷新失败: %s" % exc)

    # ------------------------------------------------------------------
    # 标题栏状态（v1.18.2：原 44px 独立状态条并入标题栏，整窗矮 44px）
    # ------------------------------------------------------------------

    def _set_badge(self, text, bg=None, fg=None):
        """标题栏状态胶囊：文字自适应宽度，17px 高。

        ``bg`` 不传时用皮肤的中性胶囊色（原来写死 #3d3f41，浅色皮肤下
        就成了白底上的一块深灰）；``fg`` 不传时按背景亮度自动取黑/白 ——
        黄色 #d8b90a、绿色 #28a745 配白字看不清，必须先判亮度。
        """
        if bg is None:
            bg = C_PILL
        self._badge_text = text or ""
        self._badge_bg = bg
        self._badge_fg = fg
        self._badge.setText(text or "")
        self._badge.setStyleSheet(
            "background:%s; color:%s; border:none; border-radius:8px;"
            " padding:1px 7px; font-size:10px; font-weight:bold;"
            % (bg, fg or _contrast_fg(bg)))
        # QLabel 的 sizeHint 不含 stylesheet 的 padding，手动补上再锁定宽度，
        # 否则胶囊会被挤成一条只显示半个字。
        fm = self._badge.fontMetrics()
        w = fm.boundingRect(text or "").width() + 16
        self._badge.setFixedWidth(max(30, w))
        self._badge.setToolTip(text or "")

    def _set_status(self, text, color=None):
        """标题栏一句话说明：超宽省略号，全文进 tooltip（鼠标悬停可看）。"""
        self._status_full = text or ""
        self._status_color = color or C_TEXT_DIM
        self._status_text.setStyleSheet(
            "color:%s; background:transparent; border:none; font-size:10px;"
            % self._status_color)
        tip = self._status_full
        if tip:
            tip += "\n双击窗口可刷新识别 · 按住空白处可拖动"
        self._status_text.setToolTip(tip)
        self._apply_status_elide()

    def _apply_status_elide(self):
        """按当前实际宽度重算省略号（窗口尺寸变了要重算）。"""
        if not hasattr(self, "_status_text") or not hasattr(self, "_status_full"):
            return
        fm = self._status_text.fontMetrics()
        avail = self._status_text.width() or 240
        self._status_text.setText(
            fm.elidedText(self._status_full, QtCore.Qt.ElideRight,
                          max(40, avail - 4)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_status_elide()
        self._apply_patient_text()

    def showEvent(self, event):
        """首次显示时字体才真正生效，这里再截一次，避免按默认字号算出的省略号。"""
        super().showEvent(event)
        self._apply_patient_text()

    # ------------------------------------------------------------------
    # 结果渲染
    # ------------------------------------------------------------------

    def show_result(self, result, record_id=None):
        """渲染一个 :class:`~reportqc.pacs.pipeline.QCResult`。

        ``record_id`` 是落库后的记录编号；给了才能开详情窗做误判标记。
        """
        self._record_id = record_id
        self._detail_btn.setEnabled(record_id is not None)
        color = LEVEL_COLORS.get(result.level, "#888888")

        self._cancel_flash()
        # 标题栏：胶囊 = 结论，小字 = 问题计数（耗时只在 tooltip 里给）
        self._set_badge(_LEVEL_BADGE.get(result.level, ""), color)
        meta = result.summary_text()
        if result.note:
            meta += " · %s" % result.note
        if result.elapsed:
            meta += " · %.1fs" % result.elapsed
        self._set_status(meta, color)

        # 患者信息卡
        self._fill_patient(result.fields)

        # 列表
        self._clear_items()
        if result.issues:
            self._empty_label.setVisible(False)
            for issue in result.issues:
                w = _IssueItem(issue)
                self._items_layout.addWidget(w)
        else:
            self._empty_label.setText(
                "未识别到问题。\n报告正文为空时不会检出任何问题。")
            self._empty_label.setVisible(True)

        self._sync_size()

    def _fill_patient(self, fields):
        """把提取到的字段填进患者信息卡。字段缺失时给「未识别到」而不是空白。"""
        if fields is None:
            self._set_patient("—", "", "未提取到患者信息", "")
            return

        name = (getattr(fields, "patient_name", "") or "").strip()
        gender = (getattr(fields, "gender", "") or "").strip()
        age = (getattr(fields, "age", "") or "").strip()

        # 性别/年龄：「62岁」不再拼成「62岁岁」（见 fields.format_demo）
        demo = format_demo(gender, age) if (gender or age) else ""

        # 第二行：影像号优先（PACS 主键），没有再退检查号。
        # 检查号那一栏在体检登记软件里叫「体检编号」，照原样显示 ——
        # 医生要拿这个号回软件里检索，叫错了就找不着地方填。
        acc = (getattr(fields, "accession_no", "") or "").strip()
        exam_no = (getattr(fields, "exam_no", "") or "").strip()
        exam_label = (getattr(fields, "exam_no_label", "") or "").strip() or "检查号"
        meta_bits = []
        copy_no = ""
        copy_kind = "影像号"
        if acc:
            meta_bits.append("影像号 %s" % acc)
            copy_no = acc
        elif exam_no:
            meta_bits.append("%s %s" % (exam_label, exam_no))
            copy_no = exam_no
            copy_kind = exam_label
        if not meta_bits:
            meta_bits.append("未识别到影像号")

        # 第三行：设备/部位/项目 + 检查时间。
        # 去重：体检软件里「检查部位=胸部」和「检查项目=胸部CT平扫」常常同一段
        # 文字，直接拼接就成了「胸部 · 胸部CT平扫」，患者卡上一行放不下还折行。
        exam_bits = []
        for key in ("modality", "body_part", "exam_item"):
            v = (getattr(fields, key, "") or "").strip()
            if v and v not in exam_bits:
                exam_bits.append(v)
        t = (getattr(fields, "exam_time", "") or "").strip()
        if t:
            exam_bits.append(t)

        self._set_patient(name or "（未识别到姓名）", demo,
                          " · ".join(meta_bits), _join_unique(exam_bits),
                          copy_no=copy_no, copy_kind=copy_kind)

    def _set_patient(self, name, demo, meta, exam, copy_no="", copy_kind="影像号"):
        """填患者卡；空行直接隐藏，不留空白（精简形态）。

        ``copy_no`` 是点影像号行时复制进剪贴板的值（为空则不可复制）。

        三行文字都**按实际宽度单行截断**（省略号 + 全文 tooltip）。原因见
        ``_apply_patient_text()``：卡片高度靠 sizeHint 固定，任何一行折行
        都会溢出到卡片外面。
        """
        self._pt_name_full = name or ""
        self._pt_demo_full = demo or ""
        self._pt_meta_full = meta or ""
        self._pt_exam_full = exam or ""

        self._pt_demo.setVisible(bool(demo))
        self._pt_meta.setVisible(bool(meta))
        self._pt_exam.setVisible(bool(exam))

        self._copy_no = (copy_no or "").strip()
        self._copy_kind = copy_kind or "影像号"
        if self._copy_no:
            self._pt_meta.setCursor(QtCore.Qt.PointingHandCursor)
        else:
            self._pt_meta.setCursor(QtCore.Qt.ArrowCursor)
        self._apply_patient_text()

    def _elide_to(self, label, full, tip_prefix="", mode=None, avail=0):
        """把 ``full`` 按可用宽度单行截断后写入，全文放 tooltip。

        ``avail`` 优先用调用方算好的宽度 —— **不要依赖 label.width()**：
        窗口还没完成布局时控件宽度是上一次的旧值（实测比最终宽一半），
        按它截断会得到一条「长得能溢出标签」的字符串，末尾被硬生生切断。
        ``mode`` 用 :class:`QtCore.Qt.ElideMiddle` 可保住首尾。
        """
        fm = label.fontMetrics()
        if avail < 40:
            avail = label.width()
        if avail < 40:                        # 尚未布局（首次渲染）用窗宽兜底
            avail = max(160, self.width() - 60)
        shown = fm.elidedText(full or "", mode or QtCore.Qt.ElideRight,
                              max(40, avail - 2))
        label.setText(shown)
        tips = []
        if full:
            tips.append(full)
        if tip_prefix:
            tips.append(tip_prefix.strip())
        label.setToolTip("\n".join(tips))

    def _apply_patient_text(self):
        """按当前宽度重算患者卡三行的省略号（窗口宽度变化/换内容都要重算）。

        为什么不让 Qt 自己 wordWrap：卡片高度在 ``_sync_size()`` 里按
        ``sizeHint()`` 固定死了，而带 wordWrap 的 QLabel 的 sizeHint 不知道
        实际宽度、常常按「两行」报高度；布局再把两行压成一行的高度，
        多出来的那行就画到卡片外面 —— 表现就是文字叠在下面的列表/按钮上，
        看起来像「重复显示 + 错别字被切掉一半」。
        """
        # 构造期 resizeEvent 可能早于 _build()（__init__ 里先 resize 再建控件），
        # 事件处理器里抛异常在 PyQt 下是直接吞掉/硬崩，必须显式挡一下。
        if not hasattr(self, "_pt_name"):
            return

        # 可用宽度按**窗口宽度**推算，不看控件当前宽度：未完成布局时
        # 控件宽度还是旧值（实测能宽出一半），照它截断会溢出标签、末尾被硬切。
        # 430 窗宽 → 430 - 2 边框 - 16 外层留白 - 20 卡片内边距 ≈ 392。
        inner = max(120, self.width() - 40)
        card_w = self._patient_card.width()
        if card_w >= 120:                   # 布局已就绪时以卡片实测为准
            inner = min(inner, card_w - 22)

        # 性别/年龄先落地：它是固定列宽，若也按「自己当前宽度」截断会形成
        # 反馈环 —— 截短 → sizeHint 变小 → 列更窄 → 再截短，最后只剩「男 /…」。
        demo = getattr(self, "_pt_demo_full", "")
        fm_d = self._pt_demo.fontMetrics()
        max_demo = max(70, int(inner * 0.42))
        if demo and fm_d.width(demo) > max_demo:
            self._pt_demo.setText(fm_d.elidedText(demo, QtCore.Qt.ElideRight,
                                                  max_demo))
            self._pt_demo.setToolTip(demo)
        else:
            self._pt_demo.setText(demo)
            self._pt_demo.setToolTip("")

        # 姓名拿「总宽 - 性别年龄宽度 - 间距」；用 sizeHint 而不是 width()，
        # 它不随布局抖动。
        demo_w = self._pt_demo.sizeHint().width() if demo else 0
        name_avail = max(60, inner - demo_w - 10)

        self._elide_to(self._pt_name, getattr(self, "_pt_name_full", ""),
                       avail=name_avail)
        self._elide_to(self._pt_meta, getattr(self, "_pt_meta_full", ""),
                       avail=inner,
                       tip_prefix=("点击复制%s %s" % (self._copy_kind,
                                                 self._copy_no))
                       if self._copy_no else "")
        self._elide_to(self._pt_exam, getattr(self, "_pt_exam_full", ""),
                       avail=inner,
                       mode=QtCore.Qt.ElideMiddle)   # 保住末尾的检查时间

    def _copy_accession(self):
        """点影像号行 → 复制号码，标题栏给一句「已复制」反馈。"""
        val = getattr(self, "_copy_no", "")
        if not val:
            return
        try:
            QtWidgets.QApplication.clipboard().setText(val)
        except Exception as exc:                       # noqa: BLE001
            print("[reportqc] 复制失败: %s" % exc)
            return
        self._flash("已复制%s %s" % (self._copy_kind, val), C_SUCCESS)

    def _flash(self, message, color=None, ms=1600):
        """标题栏临时提示，到时还原原来的摘要（不弹框打断阅片）。"""
        if not getattr(self, "_flash_saved", None):
            self._flash_saved = (getattr(self, "_status_full", ""),
                                 getattr(self, "_status_color", C_TEXT_DIM))
        self._set_status(message, color)
        t = getattr(self, "_flash_timer", None)
        if t is None:
            t = QtCore.QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._unflash)
            self._flash_timer = t
        t.start(ms)

    def _cancel_flash(self):
        """新状态来了就取消未到点的还原 —— 否则 1.6s 后会把新摘要盖回旧的。"""
        self._flash_saved = None
        t = getattr(self, "_flash_timer", None)
        if t is not None:
            t.stop()

    def _unflash(self):
        saved = getattr(self, "_flash_saved", None)
        self._flash_saved = None
        if saved:
            self._set_status(saved[0], saved[1])

    def show_ready(self, has_region, profile_count=0):
        """就绪态：**不自动识别**，只告诉用户当前区域已就绪、怎么触发。

        ``profile_count`` 是已保存的区域配置套数（对应几套写报告软件）。
        只在 ≥2 时提一句「已绑定 N 个软件」—— 配了多套说明用户确实在
        两个软件之间来回切，这句能让他确信不用手动改设置；只有一套时
        说这话纯属噪音，不显示。

        为什么启动时不自动跑一次识别
        ----------------------------
        程序刚起来时主窗口是最大化的，很可能正好盖住 PACS 报告区域；
        这时自动截图会截到本程序自己的界面，OCR 出的「患者信息」是错的，
        比让用户多点一次刷新危险得多。所以启动只显示就绪态。
        """
        # 兼容旧调用（第二参数曾经是区域描述字符串）
        n = profile_count if isinstance(profile_count, int) else 0

        self._record_id = None
        self._detail_btn.setEnabled(False)
        self._cancel_flash()
        self._clear_items()

        if has_region:
            self._set_badge("就绪", None, C_TEXT_DIM)
            if n > 1:
                self._set_status("已绑定 %d 个软件 · 点「刷新」或双击识别" % n)
            else:
                self._set_status("点「刷新」或双击窗口识别")
            self._set_patient("—", "", "尚未识别", "")
            self._empty_label.setText("")
            self._empty_label.setVisible(False)
        else:
            self._set_badge("未设置", C_WARN, None)
            self._set_status("点「设置区域」框住报告区，每个软件各设一次",
                            C_WARN)
            self._set_patient("—", "", "尚未识别", "")
            self._empty_label.setText("")
            self._empty_label.setVisible(False)

        self._recheck_btn.setEnabled(True)
        self._sync_size()

    def show_running(self):
        """切换到「检测中」状态（OCR + 质控约 1~3s）。"""
        self._cancel_flash()
        self._set_badge("···", None, C_TEXT_DIM)
        self._set_status("正在识别与检测…")
        self._clear_items()
        self._empty_label.setText("")
        self._empty_label.setVisible(False)
        self._detail_btn.setEnabled(False)
        self.show()
        self.raise_()

    def show_error(self, message):
        """显示一条错误信息（保持窗口打开，便于重试）。"""
        self._cancel_flash()
        self._set_badge("错误", C_DANGER)
        self._set_status(message, C_DANGER)
        self._clear_items()
        self._empty_label.setText(message)
        self._empty_label.setVisible(True)
        self._detail_btn.setEnabled(False)
        # 患者卡回到「未识别」而不是留着上一次的旧患者信息，
        # 避免医生把上一份报告的患者当成当前患者
        self._set_patient("—", "", "本次未识别成功", "")

    def _clear_items(self):
        while self._items_layout.count():
            item = self._items_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _sync_size(self):
        """按内容算高度，但不超出屏幕的 70%。

        踩坑（2026-10-27）：患者信息卡曾被压扁 —— ``QScrollArea`` 带
        stretch=1 会跟卡片抢高度，默认 sizePolicy 允许压到 sizeHint 以下，
        结果卡片第三行被下边框切掉。所以这里**把卡片高度固定成它自己要的
        高度**，再按「各段固定高度 + 列表所需高度」算窗口高度。

        踩坑（v1.20.1）：只那样做还不够 —— 第三行带 wordWrap 时，折行后
        的第二行会画到卡片外面压住列表/按钮（用户反馈「文字重复、被遮挡」）。
        现在三行都单行截断，且**先截断再取 sizeHint**：顺序反了会按「两行」
        的高度算，卡片又被撑大一圈。
        """
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        max_h = int(avail.height() * 0.7)

        self._apply_patient_text()                     # 先按当前宽度截断
        lay = self._patient_card.layout()
        if lay is not None:
            lay.activate()                             # 让 sizeHint 反映截断后的文字
        card_h = self._patient_card.sizeHint().height()
        self._patient_card.setFixedHeight(card_h)      # 不许被压缩
        # 外层容器也要一起锁死：只锁卡片的话，窗口高度到上限（70% 屏高）时
        # 被挤的是容器，卡片仍然画到容器外面 —— 表现就是压在下面列表上。
        self._card_host.setFixedHeight(card_h + 6)

        # 高度**不再手算**。以前写的是 30 + card + 6 + 42 + list + 12，
        # 但 QScrollArea 的 sizeHint 和它的最小高度不是一回事（实测差 20px），
        # 手算结果比布局真正需要的少 → Qt 只能让列表区压到患者卡上，
        # 患者卡的部位/时间行就被列表盖住一半（v1.20.1 用户反馈的现象）。
        root = self.layout()
        want = 0
        min_h = 0
        if root is not None:
            root.activate()
            want = root.sizeHint().height()
            min_h = root.minimumSize().height()
        if not want:
            want = 30 + card_h + 6 + 42 + self._scroll.sizeHint().height() + 12
        self.resize(self.W, max(min_h, max(188, min(max_h, want))))
        self._apply_patient_text()                     # 宽度落定后再截一次

    # ------------------------------------------------------------------
    # 拖动 / 关闭
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & QtCore.Qt.LeftButton:
            self.move(event.globalPos() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击小窗（空白处/标题栏/患者卡）→ 刷新识别。

        列表里的条目自己会处理鼠标事件（展开/收起），按钮也会 accept，
        所以双击它们不会误触发刷新 —— 事件到不了这里。
        """
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_pos = None          # 双击前那次 press 设过，清掉防误移
            self.request_recheck.emit()
        super().mouseDoubleClickEvent(event)

    def closeEvent(self, event):
        self._save_pos()
        super().closeEvent(event)
