# -*- coding: utf-8 -*-
"""报告质控设置对话框。

分两页（用页签，避免窗口过高超出 768 屏）：

**常规**
1. **OCR 放大倍数** —— PACS 界面字号小，放大能提升识别率，代价是耗时。
   实测 2.0 是平衡点（1.4~2.4s，准确率 97%+）。
2. **是否显示提示级问题** —— 低危问题噪音较大（字段缺失、缩写提示等），
   关掉后小窗只留危急/需修改/需复核三档。危急值与「没框对区域」的提示
   永远保留，不受此开关影响。
3. **忽略词管理** —— 在详情窗点「忽略该词」积累的词，这里可以查看和删除。

**自定义规则**（v1.17.0 新增，解决「找不到地方加错别字」）
4. **自定义错别字** —— 本院常见误写（错词 → 正确写法 + 严重级别），
   下次检测立即生效，可以和内置 57 条规则一起工作。
5. **医学术语白名单** —— 英文缩写/术语（ECT、PICC、TACE、VATS …），
   加进来的词不会再被当成「无意义字母」误报。
6. **误判学习词** —— 在详情窗点「标记为误报」后自动学到这里的词，
   命中即不再报；判错了可以在这里删掉撤销。

配置分三处存，各有理由：

* QSettings（注册表 HKCU）—— UI 偏好（放大倍数、开关、窗口位置），
  主线程读、总是可写；
* SQLite ``custom_typos`` 表 —— 自定义错别字，独立表便于增删；
* SQLite ``qc_settings`` —— 忽略词 / 学习词 / 术语白名单，因为 pipeline
  在**后台线程**跑，要读它们；且它们和误判标记是同一套学习数据。
"""

import PyQt5.QtCore as QtCore
import PyQt5.QtWidgets as QtWidgets
import theme
from reportqc.ui.skin import lazy_color

__all__ = ["ReportQCSettingsDialog", "get_ocr_scale", "get_show_low", "get_show_mini"]

C_BG = lazy_color("canvas", "#1e2022")
C_BG2 = lazy_color("panel", "#26282b")
C_BORDER = lazy_color("border", "#3d3f41")
C_TEXT = lazy_color("text", "#e8eaec")
C_TEXT_DIM = lazy_color("text_dim", "#9aa0a6")

_SCALES = [("1.5（最快）", 1.5), ("2.0（推荐）", 2.0),
           ("2.5", 2.5), ("3.0（最准，最慢）", 3.0)]

_SEVERITIES = [("高（需修改）", "high"), ("中（建议核对）", "medium"),
               ("低（仅提示）", "low")]

_ORG = "ZhiGu"
_APP = "CT3D"


def _settings():
    return QtCore.QSettings(_ORG, _APP)


def get_ocr_scale():
    """读取 OCR 放大倍数（设置里可改，默认 2.0）。"""
    try:
        val = _settings().value("reportqc/ocr_scale", 2.0)
        return float(val)
    except Exception:                                  # noqa: BLE001
        return 2.0


def get_show_low():
    """是否显示提示级问题（默认显示）。"""
    try:
        val = _settings().value("reportqc/show_low", "1")
        return str(val) not in ("0", "false", "False")
    except Exception:                                  # noqa: BLE001
        return True


def get_show_mini():
    """是否自动弹出质控悬浮小窗（**默认不弹**）。

    v1.23.1 起默认改为 False：这项用得不多，启动时白白占一块屏幕。
    开关挪到了主程序的「设置 → 报告质控」里（和扫描目录放一起，少一个入口）。

    既控制启动自动弹出，也在设置保存后**立即生效**（勾选马上弹、取消马上关，
    见 :func:`reportqc_bridge.apply_mini_visibility_if_running`）。
    关闭后启动不再自动弹小窗；仍可在主程序左侧「报告质控」按钮手动打开
    （:func:`reportqc_bridge.ReportQCController.show_window`）。
    """
    try:
        val = _settings().value("reportqc/show_mini", "0")
        return str(val) not in ("0", "false", "False")
    except Exception:                                  # noqa: BLE001
        return False


class ReportQCSettingsDialog(QtWidgets.QDialog):
    """质控设置窗。"""

    def __init__(self, db=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("报告质控设置")
        self.setMinimumWidth(560)
        # 对话框底色/文字由全局皮肤给（QDialog 规则）
        self._db = db

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self._tabs = QtWidgets.QTabWidget()
        # 页签必须显式给底色：Fusion 风格下 QTabBar 会退回浅色 palette，
        # 白字压浅灰底基本看不清（2026-10-27 实测）。选中态用主色蓝。
        # 页签外观交给全局皮肤（QTabBar 在 Fusion 下不吃 palette，
        # theme.qss 里已显式给了底色/选中态），这里只保留最小宽度。
        self._tabs.setStyleSheet("QTabBar::tab { min-width:80px; }")
        self._tabs.addTab(self._build_general_page(), "常规")
        self._tabs.addTab(self._build_rules_page(), "自定义规则")
        root.addWidget(self._tabs)

        # -- 底部 ---------------------------------------------------------
        bl = QtWidgets.QHBoxLayout()
        bl.addStretch(1)
        btn_ok = QtWidgets.QPushButton("保存")
        theme.role(btn_ok, "primary")
        btn_ok.clicked.connect(self.accept)
        btn_cancel = QtWidgets.QPushButton("取消")
        btn_cancel.setStyleSheet(self._btn_css())
        btn_cancel.clicked.connect(self.reject)
        bl.addWidget(btn_ok)
        bl.addWidget(btn_cancel)
        root.addLayout(bl)

        self._reload_all()

    # ------------------------------------------------------------------
    # 页 1：常规
    # ------------------------------------------------------------------

    def _build_general_page(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        # -- OCR 放大倍数 -------------------------------------------------
        g1 = QtWidgets.QGroupBox("识别设置")
        g1.setStyleSheet(self._group_css())
        f1 = QtWidgets.QFormLayout(g1)
        f1.setContentsMargins(12, 10, 12, 10)
        f1.setSpacing(8)
        self._scale_combo = QtWidgets.QComboBox()
        for label, val in _SCALES:
            self._scale_combo.addItem(label, val)
        cur = get_ocr_scale()
        idx = 0
        for i, (_l, val) in enumerate(_SCALES):
            if abs(val - cur) < 1e-6:
                idx = i
                break
        self._scale_combo.setCurrentIndex(idx)
        f1.addRow("OCR 放大倍数：", self._scale_combo)

        # 识别区域：多套配置的查看与清理入口。
        # 医生机器上可能同时装了体检登记软件和标准 PACS，各配了一套区域；
        # 一旦某次框到了错误的窗口（比如误在浏览器上点「设置区域」），
        # 需要能在这里看到并清掉，否则那条脏配置会一直参与匹配。
        self._region_label = QtWidgets.QLabel("")
        self._region_label.setStyleSheet(self._tip_css())
        self._region_label.setWordWrap(True)
        rrow = QtWidgets.QHBoxLayout()
        rrow.setSpacing(6)
        rrow.addWidget(self._region_label, 1)
        self._btn_clear_region = QtWidgets.QPushButton("清除")
        self._btn_clear_region.setStyleSheet(self._btn_css())
        self._btn_clear_region.setToolTip(
            "清除全部识别区域，之后在小窗点「设置区域」重新框选")
        self._btn_clear_region.clicked.connect(self._clear_regions)
        rrow.addWidget(self._btn_clear_region)
        f1.addRow("识别区域：", rrow)

        tip1 = QtWidgets.QLabel(
            "PACS 界面字号偏小，放大后识别更准，但耗时随之增加。\n"
            "实测 2.0 时单次 1.4~2.4 秒、准确率 97% 以上。")
        tip1.setStyleSheet(self._tip_css())
        tip1.setWordWrap(True)
        f1.addRow("", tip1)
        v.addWidget(g1)

        # -- 显示开关 -----------------------------------------------------
        g2 = QtWidgets.QGroupBox("结果显示")
        g2.setStyleSheet(self._group_css())
        f2 = QtWidgets.QVBoxLayout(g2)
        f2.setContentsMargins(12, 10, 12, 10)
        f2.setSpacing(6)
        self._show_low_cb = QtWidgets.QCheckBox("显示提示级问题（低危）")
        self._show_low_cb.setChecked(get_show_low())
        self._show_low_cb.setStyleSheet(
            "QCheckBox { color:%s; background:transparent; font-size:12px; }" % C_TEXT)
        f2.addWidget(self._show_low_cb)
        tip2 = QtWidgets.QLabel(
            "关闭后只提示危急 / 需修改 / 需复核三档。\n"
            "危急值与「未识别到报告正文」这类操作提示永远显示。")
        tip2.setStyleSheet(self._tip_css())
        tip2.setWordWrap(True)
        f2.addWidget(tip2)
        v.addWidget(g2)

        # -- 忽略词管理 ---------------------------------------------------
        g3 = QtWidgets.QGroupBox("忽略词（命中后不再提示）")
        g3.setStyleSheet(self._group_css())
        f3 = QtWidgets.QVBoxLayout(g3)
        f3.setContentsMargins(12, 10, 12, 10)
        f3.setSpacing(6)
        tip3 = QtWidgets.QLabel(
            "在详情窗点「忽略该词」会加到里；也可以在这里手动加。")
        tip3.setStyleSheet(self._tip_css())
        tip3.setWordWrap(True)
        f3.addWidget(tip3)
        self._ignore_list = QtWidgets.QListWidget()
        self._ignore_list.setFixedHeight(78)
        self._ignore_list.setStyleSheet(self._list_css())
        f3.addWidget(self._ignore_list)
        row3 = QtWidgets.QHBoxLayout()
        self._ignore_input = QtWidgets.QLineEdit()
        self._ignore_input.setPlaceholderText("输入要忽略的词")
        self._ignore_input.setStyleSheet(self._input_css())
        row3.addWidget(self._ignore_input, 1)
        btn_add_ignore = QtWidgets.QPushButton("添加")
        btn_add_ignore.setStyleSheet(self._btn_css())
        btn_add_ignore.clicked.connect(self._add_ignore)
        self._btn_remove = QtWidgets.QPushButton("删除所选")
        self._btn_remove.setStyleSheet(self._btn_css())
        self._btn_remove.clicked.connect(self._remove_ignore)
        row3.addWidget(btn_add_ignore)
        row3.addWidget(self._btn_remove)
        f3.addLayout(row3)
        v.addWidget(g3)
        v.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # 页 2：自定义规则
    # ------------------------------------------------------------------

    def _build_rules_page(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        # -- 自定义错别字 -------------------------------------------------
        g1 = QtWidgets.QGroupBox("自定义错别字（与本院的写法对齐）")
        g1.setStyleSheet(self._group_css())
        f1 = QtWidgets.QVBoxLayout(g1)
        f1.setContentsMargins(12, 10, 12, 10)
        f1.setSpacing(6)
        tip1 = QtWidgets.QLabel(
            "把本院出现的误写加进来，下次检测立即生效（与内置 57 条规则一起工作）。\n"
            "例：错词「纵膈」→ 正确「纵隔」，级别选高。")
        tip1.setStyleSheet(self._tip_css())
        tip1.setWordWrap(True)
        f1.addWidget(tip1)

        self._typo_table = QtWidgets.QTableWidget(0, 3)
        self._typo_table.setHorizontalHeaderLabels(["错词", "正确写法", "级别"])
        self._typo_table.setFixedHeight(104)
        self._typo_table.verticalHeader().setVisible(False)
        self._typo_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows)
        self._typo_table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers)
        self._typo_table.horizontalHeader().setStretchLastSection(False)
        self._typo_table.setColumnWidth(0, 180)
        self._typo_table.setColumnWidth(1, 180)
        self._typo_table.setColumnWidth(2, 110)
        self._typo_table.setStyleSheet(self._table_css())
        f1.addWidget(self._typo_table)

        row1 = QtWidgets.QHBoxLayout()
        self._typo_wrong = QtWidgets.QLineEdit()
        self._typo_wrong.setPlaceholderText("错词")
        self._typo_wrong.setStyleSheet(self._input_css())
        self._typo_right = QtWidgets.QLineEdit()
        self._typo_right.setPlaceholderText("正确写法")
        self._typo_right.setStyleSheet(self._input_css())
        self._typo_sev = QtWidgets.QComboBox()
        for label, val in _SEVERITIES:
            self._typo_sev.addItem(label, val)
        self._typo_sev.setStyleSheet(self._input_css())
        btn_add_typo = QtWidgets.QPushButton("添加")
        btn_add_typo.setStyleSheet(self._btn_css())
        btn_add_typo.clicked.connect(self._add_custom_typo)
        self._btn_del_typo = QtWidgets.QPushButton("删除所选")
        self._btn_del_typo.setStyleSheet(self._btn_css())
        self._btn_del_typo.clicked.connect(self._remove_custom_typo)
        row1.addWidget(self._typo_wrong, 2)
        row1.addWidget(self._typo_right, 2)
        row1.addWidget(self._typo_sev, 3)
        row1.addWidget(btn_add_typo)
        row1.addWidget(self._btn_del_typo)
        f1.addLayout(row1)
        v.addWidget(g1)

        # -- 医学术语白名单 -----------------------------------------------
        g2 = QtWidgets.QGroupBox("医学术语白名单（英文缩写不再误报）")
        g2.setStyleSheet(self._group_css())
        f2 = QtWidgets.QVBoxLayout(g2)
        f2.setContentsMargins(12, 10, 12, 10)
        f2.setSpacing(6)
        tip2 = QtWidgets.QLabel(
            "内置已收录 CT / MRI / ECT / PICC / PET / DSA 等 100+ 项。\n"
            "本院特有的缩写或外文术语加在这里，就不会被当成无意义字母提示。")
        tip2.setStyleSheet(self._tip_css())
        tip2.setWordWrap(True)
        f2.addWidget(tip2)
        self._term_list = QtWidgets.QListWidget()
        self._term_list.setFixedHeight(70)
        self._term_list.setStyleSheet(self._list_css())
        f2.addWidget(self._term_list)
        row2 = QtWidgets.QHBoxLayout()
        self._term_input = QtWidgets.QLineEdit()
        self._term_input.setPlaceholderText("如 ECT、PICC、TACE")
        self._term_input.setStyleSheet(self._input_css())
        row2.addWidget(self._term_input, 1)
        btn_add_term = QtWidgets.QPushButton("添加")
        btn_add_term.setStyleSheet(self._btn_css())
        btn_add_term.clicked.connect(self._add_term)
        self._btn_del_term = QtWidgets.QPushButton("删除所选")
        self._btn_del_term.setStyleSheet(self._btn_css())
        self._btn_del_term.clicked.connect(self._remove_term)
        row2.addWidget(btn_add_term)
        row2.addWidget(self._btn_del_term)
        f2.addLayout(row2)
        v.addWidget(g2)

        # -- 误判学习词 ---------------------------------------------------
        g3 = QtWidgets.QGroupBox("误判学习词（标记误报后自动积累）")
        g3.setStyleSheet(self._group_css())
        f3 = QtWidgets.QVBoxLayout(g3)
        f3.setContentsMargins(12, 10, 12, 10)
        f3.setSpacing(6)
        tip3 = QtWidgets.QLabel(
            "在详情窗点「标记为误报」，系统会把该条问题的关键词记到这里，"
            "以后命中就不再提示。\n判错了可以删掉撤销。")
        tip3.setStyleSheet(self._tip_css())
        tip3.setWordWrap(True)
        f3.addWidget(tip3)
        self._learned_list = QtWidgets.QListWidget()
        self._learned_list.setFixedHeight(70)
        self._learned_list.setStyleSheet(self._list_css())
        f3.addWidget(self._learned_list)
        row3 = QtWidgets.QHBoxLayout()
        row3.addStretch(1)
        self._btn_del_learned = QtWidgets.QPushButton("撤销所选")
        self._btn_del_learned.setStyleSheet(self._btn_css())
        self._btn_del_learned.clicked.connect(self._remove_learned)
        row3.addWidget(self._btn_del_learned)
        f3.addLayout(row3)
        v.addWidget(g3)
        v.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------

    # 下面几个样式函数在 v1.21.0 起统一返回空串：分组框/按钮/列表/输入框/
    # 表格的外观全部由 theme.qss 提供。原来每个都手写一套深色 QSS，加了皮肤
    # 之后浅色皮肤一打开就成了「深色孤岛」，还跟主程序两套风格打架。
    def _group_css(self):
        return ""

    def _btn_css(self, primary=False):
        return ""

    def _list_css(self):
        return ""

    def _input_css(self):
        return ""

    def _table_css(self):
        return ""

    def _tip_css(self):
        return "color:%s; font-size:10px; background:transparent; border:none;" % C_TEXT_DIM

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _reload_all(self):
        self._reload_ignores()
        self._reload_custom_typos()
        self._reload_terms()
        self._reload_learned()
        self._reload_regions()

    # ------------------------------------------------------------------
    # 识别区域（多套配置）
    # ------------------------------------------------------------------

    def _reload_regions(self):
        """把已保存的区域配置列在「识别区域」一行里。"""
        try:
            from ..pacs.region_store import load_profiles
            ps = load_profiles()
        except Exception:                            # noqa: BLE001
            ps = []
        btn = getattr(self, "_btn_clear_region", None)
        label = getattr(self, "_region_label", None)
        if label is None:
            return
        if not ps:
            label.setText("尚未设置（在小窗点「设置区域」框一次）")
            if btn is not None:
                btn.setEnabled(False)
            return
        names = []
        for p in ps[:3]:
            exe = (p.get("exe") or "").strip()
            nm = (p.get("name") or exe or "").strip()
            if not exe and nm in ("", "识别区域", "默认区域"):
                nm = "默认区域（未绑定软件）"
            if exe and exe not in nm:
                nm = "%s(%s)" % (nm[:10], exe)
            names.append(nm[:16])
        text = "、".join(names)
        if len(ps) > 3:
            text += " 等 %d 个" % len(ps)
        label.setText("%d 个软件：%s" % (len(ps), text))
        if btn is not None:
            btn.setEnabled(True)

    def _clear_regions(self):
        n = 0
        try:
            from ..pacs.region_store import load_profiles, clear_profiles
            n = len(load_profiles())
            if n and QtWidgets.QMessageBox.question(
                    self, "清除识别区域",
                    "将清除已保存的 %d 套识别区域。\n"
                    "之后需要在小窗重新点「设置区域」框一次。\n\n确定清除吗？" % n,
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                return
            clear_profiles()
        except Exception as exc:                     # noqa: BLE001
            print("[reportqc] 清除识别区域失败: %s" % exc)
        self._reload_regions()

    def _db_ok(self):
        return self._db is not None

    def _placeholder(self, widget, text, is_table=False):
        """列表/表格为空时给一句说明，而不是空白一片。"""
        if is_table:
            widget.setRowCount(1)
            item = QtWidgets.QTableWidgetItem(text)
            item.setForeground(QtCore.Qt.gray)
            widget.setItem(0, 0, item)
            widget.setSpan(0, 0, 1, 3)
        else:
            widget.addItem(text)

    def _reload_ignores(self):
        self._ignore_list.clear()
        if not self._db_ok():
            self._ignore_list.addItem("（数据库不可用）")
            self._btn_remove.setEnabled(False)
            return
        words = self._db.get_ignore_words() or []
        if not words:
            self._ignore_list.addItem("（暂无忽略词）")
            return
        for w in words:
            self._ignore_list.addItem(w)

    def _reload_custom_typos(self):
        self._typo_table.clearSpans()
        self._typo_table.setRowCount(0)
        if not self._db_ok():
            self._placeholder(self._typo_table, "（数据库不可用）", True)
            return
        rows = self._db.list_custom_typos() or []
        if not rows:
            self._placeholder(
                self._typo_table, "（暂无自定义错别字，可在下方填写后点「添加」）", True)
            return
        sev_name = {"high": "高", "medium": "中", "low": "低"}
        self._typo_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            cells = [row.get("typo") or "", row.get("correction") or "",
                     sev_name.get(str(row.get("severity") or "medium"), "中")]
            for c, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                # 第 1 列存原始错词，删除时用它
                if c == 0:
                    item.setData(QtCore.Qt.UserRole, row.get("typo") or "")
                self._typo_table.setItem(i, c, item)

    def _reload_terms(self):
        self._term_list.clear()
        if not self._db_ok():
            self._term_list.addItem("（数据库不可用）")
            self._btn_del_term.setEnabled(False)
            return
        terms = self._db.get_medical_terms() or []
        if not terms:
            self._term_list.addItem("（暂无补充术语，内置 100+ 项仍然生效）")
            return
        for t in terms:
            self._term_list.addItem(t)

    def _reload_learned(self):
        self._learned_list.clear()
        if not self._db_ok():
            self._learned_list.addItem("（数据库不可用）")
            self._btn_del_learned.setEnabled(False)
            return
        words = self._db.get_learned_words() or []
        if not words:
            self._learned_list.addItem("（还没有学过词）")
            return
        for w in words:
            self._learned_list.addItem(w)

    # ------------------------------------------------------------------
    # 增删
    # ------------------------------------------------------------------

    def _selected_text(self, widget):
        item = widget.currentItem()
        if item is None:
            return ""
        text = (item.text() or "").strip()
        if not text or text.startswith("（"):
            return ""
        return text

    def _add_ignore(self):
        if not self._db_ok():
            return
        word = self._ignore_input.text().strip()
        if not word:
            return
        self._db.add_ignore_word(word)
        self._ignore_input.clear()
        self._reload_ignores()

    def _remove_ignore(self):
        if not self._db_ok():
            return
        word = self._selected_text(self._ignore_list)
        if not word:
            return
        self._db.remove_ignore_word(word)
        self._reload_ignores()

    def _add_custom_typo(self):
        if not self._db_ok():
            return
        wrong = self._typo_wrong.text().strip()
        right = self._typo_right.text().strip()
        if not wrong or not right:
            QtWidgets.QMessageBox.information(
                self, "自定义错别字", "请同时填写「错词」和「正确写法」。")
            return
        if wrong == right:
            QtWidgets.QMessageBox.information(
                self, "自定义错别字", "错词和正确写法不能相同。")
            return
        self._db.add_custom_typo(wrong, right, self._typo_sev.currentData())
        self._typo_wrong.clear()
        self._typo_right.clear()
        self._reload_custom_typos()

    def _remove_custom_typo(self):
        if not self._db_ok():
            return
        row = self._typo_table.currentRow()
        if row < 0:
            return
        item = self._typo_table.item(row, 0)
        if item is None:
            return
        typo = (item.data(QtCore.Qt.UserRole) or item.text() or "").strip()
        if not typo or typo.startswith("（"):
            return
        self._db.remove_custom_typo(typo)
        self._reload_custom_typos()

    def _add_term(self):
        if not self._db_ok():
            return
        term = self._term_input.text().strip()
        if not term:
            return
        self._db.add_medical_term(term)
        self._term_input.clear()
        self._reload_terms()

    def _remove_term(self):
        if not self._db_ok():
            return
        term = self._selected_text(self._term_list)
        if not term:
            return
        self._db.remove_medical_term(term)
        self._reload_terms()

    def _remove_learned(self):
        if not self._db_ok():
            return
        word = self._selected_text(self._learned_list)
        if not word:
            return
        self._db.remove_learned_word(word)
        self._reload_learned()

    # ------------------------------------------------------------------

    def accept(self):
        try:
            s = _settings()
            s.setValue("reportqc/ocr_scale", float(self._scale_combo.currentData()))
            s.setValue("reportqc/show_low", "1" if self._show_low_cb.isChecked() else "0")
            # reportqc/show_mini 不在这里写：那个开关已挪到主程序「设置」里，
            # 由主窗口统一读写（同一个 key，避免两边各写一份互相覆盖）。
        except Exception:                              # noqa: BLE001
            pass
        # 注意：自定义错别字 / 术语白名单 / 学习词都是**立即写库**的，
        # pipeline 每次运行前会重新读一遍（见 controller._pipeline_job），
        # 所以这里不需要清缓存。只有 OCR 放大倍数要重建引擎，
        # 那件事由 controller.open_settings 在对话框 Accepted 后处理 ——
        # 放在这个对话框里做会引入 reportqc_bridge 的循环导入。
        super().accept()
