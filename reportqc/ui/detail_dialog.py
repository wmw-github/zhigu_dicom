# -*- coding: utf-8 -*-
"""报告质控详情大窗口。

小窗（:mod:`reportqc.ui.mini_window`）只做「一眼看出有没有问题」，
详情窗承担三件小窗做不了的事：

1. **看全文**：小窗只有一条摘要，这里能看到整段被检测的报告原文，
   并**自动定位高亮**当前问题触发的文字片段；
2. **误判反馈**：把某条问题标记为「误报」或「确认有问题」，
   写入 ``learning_records`` 形成误判学习流水；
3. **忽略词**：对反复误报的词（典型就是 OCR 把「膈肌」认成「隔肌」），
   一键加入忽略表，之后不再报。

原文定位的现实困难
------------------
OCR 出来的文本与 PACS 里真实文本**不总是一致的**（OCR 会增删字符），
所以 ``source`` 片段不一定能在 ``text`` 里原样找到。找不到时不做高亮，
只在详情区显示片段本身，绝不「猜一个位置」把用户引到错误的段落。
"""

import re

import PyQt5.QtCore as QtCore
import PyQt5.QtGui as QtGui
import PyQt5.QtWidgets as QtWidgets

from ..pacs.pipeline import LEVEL_COLORS, LEVEL_CRITICAL, LEVEL_HIGH, LEVEL_MEDIUM, LEVEL_LOW
import theme
from reportqc.ui.skin import lazy_color

__all__ = ["ReportDetailDialog"]

C_BG = lazy_color("canvas", "#1e2022")
C_BG2 = lazy_color("panel", "#26282b")
C_BORDER = lazy_color("border", "#3d3f41")
C_TEXT = lazy_color("text", "#e8eaec")
C_TEXT_DIM = lazy_color("text_dim", "#9aa0a6")
C_ACCENT = lazy_color("accent", "#2a82da")

_LEVEL_LABEL = {
    LEVEL_CRITICAL: "危急",
    LEVEL_HIGH: "需修改",
    LEVEL_MEDIUM: "需复核",
    LEVEL_LOW: "提示",
}

_VERDICT_LABEL = {
    "false_positive": "已标记：误报",
    "confirmed": "已标记：确认有问题",
}


def _extract_keyword(message):
    """从问题描述里取出可复用的关键词（忽略词 / 误判学习的候选）。

    与 ``qc/database.py::extract_issue_word`` 保持同一口径，否则会出现
    「详情窗说已学习、数据层却学了个空串」这种不一致：

    * ``「纵膈」应为「纵隔」`` → ``纵膈``
    * ``检测到可能的无意义输入: 'asdf'`` → ``asdf``
    """
    msg = message or ""
    m = re.search(r"「(.+?)」", msg)
    if m:
        return m.group(1)
    m = re.search(r"'([^']{2,})'", msg)
    if m:
        return m.group(1)
    return ""


class ReportDetailDialog(QtWidgets.QDialog):
    """详情窗。``record_id`` 与 ``db`` 给了才能做误判标记/忽略词。"""

    def __init__(self, result, record_id=None, db=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("报告质控 — 详情")
        self.resize(880, 600)
        # 对话框底色/文字由全局皮肤给（QDialog 规则）

        self._result = result
        self._record_id = record_id
        self._db = db
        # qc_issues 的 id 与 result.issues 按下标一一对应（落库顺序一致）
        self._db_issues = []
        if db is not None and record_id:
            try:
                self._db_issues = db.query_issues(record_id) or []
            except Exception:                          # noqa: BLE001
                self._db_issues = []

        self._build()
        self._fill_issues()
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    # ------------------------------------------------------------------

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- 顶部信息条 ---------------------------------------------------
        header = QtWidgets.QWidget()
        header.setFixedHeight(52)
        header.setObjectName("headerBar")
        header.setStyleSheet("QWidget#headerBar { background:%s; border:none; }" % C_BG2)
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(12, 6, 12, 6)
        hl.setSpacing(10)

        badge = QtWidgets.QLabel({
            "red": "危急", "orange": "修改", "yellow": "复核", "green": "通过",
        }.get(self._result.level, ""))
        badge.setFixedSize(52, 30)
        badge.setAlignment(QtCore.Qt.AlignCenter)
        badge.setStyleSheet(
            "background:%s; border-radius:3px; border:none; color:white;"
            "font-size:12px; font-weight:bold;" % LEVEL_COLORS.get(
                self._result.level, "#888888"))
        hl.addWidget(badge)

        info = QtWidgets.QVBoxLayout()
        info.setSpacing(0)
        f = self._result.fields
        title = f.summary() if f is not None else "（未识别到患者信息）"
        t1 = QtWidgets.QLabel(title)
        t1.setStyleSheet("color:%s; background:transparent; border:none;"
                         " font-size:13px; font-weight:bold;" % C_TEXT)
        t2 = QtWidgets.QLabel(self._result.summary_text() + (
            "  ·  耗时 %.1fs" % self._result.elapsed if self._result.elapsed else ""))
        t2.setStyleSheet("color:%s; background:transparent; border:none;"
                         " font-size:11px;" % C_TEXT_DIM)
        info.addWidget(t1)
        info.addWidget(t2)
        hl.addLayout(info, 1)
        root.addWidget(header)

        # -- 主体：左列表 + 右详情 ---------------------------------------
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background:%s; }" % C_BORDER)

        # 左：问题列表
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 4, 8)
        lv.setSpacing(4)
        cap = QtWidgets.QLabel("问题列表（%d）" % len(self._result.issues))
        cap.setStyleSheet("color:%s; background:transparent; border:none;"
                          " font-size:11px;" % C_TEXT_DIM)
        lv.addWidget(cap)
        self._list = QtWidgets.QListWidget()
        # 列表底色/选中色交给全局皮肤，只保留本窗特有的行高与分隔线
        self._list.setStyleSheet(
            "QListWidget::item { padding:6px 4px; border-bottom:1px solid %s; }"
            % C_BORDER)
        self._list.currentRowChanged.connect(self._on_row_changed)
        lv.addWidget(self._list)
        splitter.addWidget(left)

        # 右：详情 + 原文
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(8, 8, 8, 8)
        rv.setSpacing(6)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        inner = QtWidgets.QWidget()
        iv = QtWidgets.QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(8)

        self._msg_label = QtWidgets.QLabel()
        self._msg_label.setWordWrap(True)
        self._msg_label.setStyleSheet(
            "color:%s; background:transparent; border:none; font-size:14px;"
            " font-weight:bold;" % C_TEXT)
        iv.addWidget(self._msg_label)

        self._meta_label = QtWidgets.QLabel()
        self._meta_label.setWordWrap(True)
        self._meta_label.setStyleSheet(
            "color:%s; background:transparent; border:none; font-size:11px;" % C_TEXT_DIM)
        iv.addWidget(self._meta_label)

        self._verdict_hint = QtWidgets.QLabel()
        self._verdict_hint.setWordWrap(True)
        self._verdict_hint.setStyleSheet(
            "color:#8ab4f8; background:#22303f; border:1px solid #2f4a63;"
            " border-radius:3px; padding:5px 8px; font-size:11px;")
        self._verdict_hint.setVisible(False)
        iv.addWidget(self._verdict_hint)

        iv.addWidget(self._hsep())
        src_cap = QtWidgets.QLabel("报告原文（问题片段已高亮）")
        src_cap.setStyleSheet(
            "color:%s; background:transparent; border:none; font-size:11px;" % C_TEXT_DIM)
        iv.addWidget(src_cap)

        self._text_edit = QtWidgets.QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setStyleSheet("QTextEdit { font-size:13px; }")
        self._text_edit.setPlainText(self._result.text or "（未识别到报告正文）")
        iv.addWidget(self._text_edit, 1)

        scroll.setWidget(inner)
        rv.addWidget(scroll, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 35)
        splitter.setStretchFactor(1, 65)
        root.addWidget(splitter, 1)

        # -- 底部按钮 -----------------------------------------------------
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(46)
        bar.setObjectName("bottomBar")
        bar.setStyleSheet("QWidget#bottomBar { background:%s; border:none; }" % C_BG2)
        bl = QtWidgets.QHBoxLayout(bar)
        bl.setContentsMargins(10, 7, 10, 7)
        bl.setSpacing(8)

        self._btn_fp = QtWidgets.QPushButton("标记为误报")
        self._btn_ok = QtWidgets.QPushButton("确认有问题")
        self._btn_ignore = QtWidgets.QPushButton("忽略该词")
        self._btn_close = QtWidgets.QPushButton("关闭")
        # 按钮外观交给全局皮肤（v1.21.0）；「确认无误」是主操作，加主色
        theme.role(self._btn_ok, "primary")
        self._btn_fp.clicked.connect(lambda: self._mark("false_positive"))
        self._btn_ok.clicked.connect(lambda: self._mark("confirmed"))
        self._btn_ignore.clicked.connect(self._ignore_word)
        self._btn_close.clicked.connect(self.accept)
        bl.addWidget(self._btn_fp)
        bl.addWidget(self._btn_ok)
        bl.addWidget(self._btn_ignore)
        bl.addStretch(1)
        bl.addWidget(self._btn_close)
        root.addWidget(bar)

        self._can_mark = bool(self._db is not None and self._record_id)
        self._btn_fp.setEnabled(self._can_mark)
        self._btn_ok.setEnabled(self._can_mark)
        self._btn_ignore.setEnabled(self._can_mark)

    def _hsep(self):
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet("color:%s; background:%s;" % (C_BORDER, C_BORDER))
        line.setFixedHeight(1)
        return line

    # ------------------------------------------------------------------

    def _fill_issues(self):
        self._list.clear()
        for i, it in enumerate(self._result.issues):
            color = LEVEL_COLORS.get(_color_key(it.level), "#888888")
            label = _LEVEL_LABEL.get(it.level, "")
            text = "[%s] %s" % (label, it.message)
            db_issue = self._db_issues[i] if i < len(self._db_issues) else None
            if db_issue is not None and db_issue.get("user_verdict"):
                text += "   ✓ %s" % _VERDICT_LABEL.get(
                    db_issue["user_verdict"], db_issue["user_verdict"])
            item = QtWidgets.QListWidgetItem(text)
            # 用色点图标代替文字染色（QSS 对 item 文本着色不可靠）
            pix = QtGui.QPixmap(12, 12)
            pix.fill(QtGui.QColor(color))
            item.setIcon(QtGui.QIcon(pix))
            self._list.addItem(item)

    def _on_row_changed(self, row):
        if row < 0 or row >= len(self._result.issues):
            return
        it = self._result.issues[row]
        color = LEVEL_COLORS.get(_color_key(it.level), "#888888")

        self._msg_label.setText(it.message)
        self._msg_label.setStyleSheet(
            "color:%s; background:transparent; border:none; font-size:14px;"
            " font-weight:bold;" % color)

        bits = []
        if it.suggestion:
            bits.append("建议：%s" % it.suggestion)
        if it.source:
            bits.append("原文片段：%s" % it.source)
        if it.confidence:
            bits.append("规则置信度 %.2f" % it.confidence)
        if it.ocr_conf is not None:
            bits.append("OCR 置信度 %.2f%s" % (
                it.ocr_conf, "（低于 0.85，可能是识别误差）" if it.ocr_suspect else ""))
        self._meta_label.setText("\n".join(bits))

        # 历史判定参考 + 误判学习的实际效果说明
        db_issue = self._db_issues[row] if row < len(self._db_issues) else None
        hint = ""
        if db_issue is not None and db_issue.get("user_verdict"):
            hint = "你已把这条标记为「%s」。" % _VERDICT_LABEL.get(
                db_issue["user_verdict"], db_issue["user_verdict"])
            if db_issue["user_verdict"] == "false_positive":
                # 说清「学习」到底发生了什么，否则用户不知道有没有生效
                learned = _extract_keyword(it.message)
                if learned:
                    hint += ("已学习关键词「%s」：之后的检测不再提示该词。"
                             "（可在「质控设置 → 自定义规则 → 误判学习词」里撤销）"
                             % learned)
                else:
                    hint += ("这类问题没有可复用的关键词，不会被自动学习；"
                             "若不想再看到，请用「忽略该词」。")
        elif self._db is not None and it.kind:
            try:
                past = self._db.query_verdicts(kind=it.kind, limit=500) or []
                fp = sum(1 for v in past if v.get("verdict") == "false_positive")
                ok = sum(1 for v in past if v.get("verdict") == "confirmed")
                if fp or ok:
                    hint = ("历史上同类问题（%s）：误报 %d 次 / 确认 %d 次。"
                            % (it.kind, fp, ok))
            except Exception:                          # noqa: BLE001
                pass
        self._verdict_hint.setText(hint)
        self._verdict_hint.setVisible(bool(hint))

        # 原文定位高亮
        self._highlight_source(it.source)

    def _highlight_source(self, source):
        """在原文框里高亮 ``source`` 片段；找不到就只清除旧高亮。"""
        selections = []
        doc_text = self._text_edit.toPlainText()
        if source and source.strip():
            needle = source.strip()
            start = doc_text.find(needle)
            if start < 0 and len(needle) > 12:
                # OCR 可能有字符级差异，退一步用前 8 个字试
                start = doc_text.find(needle[:8])
                if start >= 0:
                    needle = needle[:8]
            if start >= 0:
                cur = self._text_edit.textCursor()
                cur.setPosition(start)
                cur.setPosition(start + len(needle),
                                QtGui.QTextCursor.KeepAnchor)
                fmt = QtGui.QTextCharFormat()
                fmt.setBackground(QtGui.QColor("#5a4a12"))
                fmt.setForeground(QtGui.QColor("#ffffff"))
                sel = QtWidgets.QTextEdit.ExtraSelection()
                # 注意：PyQt5 的 ExtraSelection 是数据容器，
                # 没有 setCursor/setFormat 方法，必须直接赋属性
                sel.cursor = cur
                sel.format = fmt
                selections.append(sel)
                self._text_edit.setTextCursor(cur)
        self._text_edit.setExtraSelections(selections)

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def _current_row(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._result.issues):
            return row
        return -1

    def _mark(self, verdict):
        row = self._current_row()
        if row < 0 or not self._can_mark:
            return
        it = self._result.issues[row]
        db_issue = self._db_issues[row] if row < len(self._db_issues) else None
        if db_issue is None:
            return
        ok = self._db.set_verdict(int(db_issue["id"]), verdict)
        if not ok:
            QtWidgets.QMessageBox.warning(self, "标记失败", "写入数据库失败，请重试。")
            return
        db_issue["user_verdict"] = verdict
        self._fill_issues()
        self._list.setCurrentRow(row)
        # 顺手提示：如果这是误报且反复出现，可以加忽略词
        if verdict == "false_positive" and _extract_keyword(it.message):
            self._btn_ignore.setToolTip(
                "「%s」已标记为误报。点「忽略该词」后不再提示。" % _extract_keyword(it.message))

    def _ignore_word(self):
        row = self._current_row()
        if row < 0 or not self._can_mark:
            return
        it = self._result.issues[row]
        word = _extract_keyword(it.message)
        if not word:
            QtWidgets.QMessageBox.information(
                self, "无法忽略", "这类问题没有可提取的关键词，请改用「标记为误报」。")
            return
        if not self._db.add_ignore_word(word):
            QtWidgets.QMessageBox.warning(self, "忽略失败", "写入数据库失败，请重试。")
            return
        QtWidgets.QMessageBox.information(
            self, "已忽略",
            "「%s」已加入忽略表，之后的检测不再报这个词。\n"
            "（对本次已显示的结果无效，重新检测后生效）" % word)


def _color_key(level):
    return {
        LEVEL_CRITICAL: "red",
        LEVEL_HIGH: "orange",
        LEVEL_MEDIUM: "yellow",
        LEVEL_LOW: "yellow",
    }.get(level, "yellow")
