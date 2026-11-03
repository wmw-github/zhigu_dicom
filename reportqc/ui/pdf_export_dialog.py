# -*- coding: utf-8 -*-
"""报告 PDF 导出对话框（从质控小窗「PDF」按钮进入）。

功能
----
* 列出 ``C:\\temp`` 目录下的 PDF 文件（按修改时间倒序）；
* 列表**只展示「患者名 + 影像号」，不显示 PDF 文件名**（便于快速查找）；
* 每个 PDF 右侧一个「导出到桌面」按钮：复制到桌面，文件名**只用患者姓名**
  （如 ``张三.pdf``），不追加影像号；姓名/影像号取自 Info 元数据或正文
  ToUnicode 文本，见 :mod:`reportqc.ui.pdf_text`；
* 取不到姓名时回退原文件名，并在界面标明，绝不静默丢信息。

不弹模态确认框打断操作 —— 结果直接写在本窗底部状态条与每行的提示里。
"""

import os
import shutil

import PyQt5.QtCore as QtCore
import PyQt5.QtGui as QtGui
import PyQt5.QtWidgets as QtWidgets

from .pdf_text import extract_patient_meta, sanitize_filename, PDFTextError
import theme
from reportqc.ui.skin import lazy_color

__all__ = ["PdfExportDialog"]

# 扫描目录（固定为 C:\temp，按需求）
PDF_FOLDER = r"C:\temp"


C_BG = lazy_color("canvas", "#1e2022")
C_BG2 = lazy_color("panel", "#26282b")
C_BORDER = lazy_color("border", "#3d3f41")
C_TEXT = lazy_color("text", "#e8eaec")
C_TEXT_DIM = lazy_color("text_dim", "#9aa0a6")
C_ACCENT = lazy_color("accent", "#2a82da")


class _PdfRow(QtWidgets.QWidget):
    """列表里的一行：文件名 + 识别到的姓名 + 导出按钮 + 结果提示。"""

    exported = QtCore.pyqtSignal(str)      # 传目标完整路径，供父窗更新状态条

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self._path = path
        self._name = None
        self._img_no = None
        self._target = None
        self._done = False
        self.setObjectName("pdfCard")
        self.setStyleSheet("QWidget#pdfCard { background:%s; border-radius:3px; }" % C_BG2)

        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(10, 7, 10, 7)
        root.setSpacing(8)

        # 左：患者信息（不显示文件名 —— 列表只看「姓名 + 影像号」）
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(2)
        self._name_lbl = QtWidgets.QLabel("")
        self._name_lbl.setStyleSheet(
            "color:%s; background:transparent; border:none; font-size:12px;" % C_TEXT)
        # 原文件名放进悬停提示：不占版面，但需要时仍可核对
        self._name_lbl.setToolTip(path)
        left.addWidget(self._name_lbl)
        root.addLayout(left, 1)

        # 右：导出按钮（占满右侧，克制成一行操作）
        self._btn = QtWidgets.QPushButton("导出到桌面")
        self._btn.setFixedWidth(110)
        # 行内操作：一屏十几行的实色主色块会变成「按钮墙」，
        # 用 tonal（浅底同色系，悬停才填充）弱化重复重量
        theme.role(self._btn, "tonal")
        self._btn.clicked.connect(self._on_export)
        root.addWidget(self._btn, 0)

    # -- 患者名 + 影像号解析（构造后由父窗调用，便于分批） -----------------
    def resolve_name(self):
        try:
            self._name, self._img_no = extract_patient_meta(self._path)
        except PDFTextError as exc:
            self._name = None
            self._img_no = None
            self._name_lbl.setText("（解析失败：%s）" % exc)
            return
        self._update_label()

    def _update_label(self):
        """列表里展示「患者名 + 影像号」，方便快速定位。"""
        parts = []
        if self._name:
            parts.append("患者 %s" % self._name)
        if self._img_no:
            parts.append("影像号 %s" % self._img_no)
        if parts:
            # 全角空格分隔，中文下更清晰
            self._name_lbl.setText("　".join(parts))
        else:
            self._name_lbl.setText("（未识别姓名，导出时将用原文件名）")

    # -- 导出 -----------------------------------------------------------
    def _on_export(self):
        if self._done:
            return
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        try:
            if not os.path.isdir(desktop):
                raise OSError("桌面目录不存在：%s" % desktop)
        except Exception as exc:                       # noqa: BLE001
            self._name_lbl.setText("导出失败：%s" % exc)
            return

        # 导出文件名：**只用患者姓名**（需求：张三.pdf，不追加影像号）；
        # 识别不到姓名时回退原文件名，保证仍可导出。
        stem = sanitize_filename(self._name) if self._name else ""
        if not stem:
            stem = sanitize_filename(os.path.splitext(os.path.basename(self._path))[0])
        if not stem:
            stem = "报告"
        target = os.path.join(desktop, stem + ".pdf")
        # 同名碰撞：追加序号
        n = 1
        while os.path.exists(target):
            target = os.path.join(desktop, "%s_%d.pdf" % (stem, n))
            n += 1
        try:
            shutil.copy2(self._path, target)
        except Exception as exc:                       # noqa: BLE001
            self._name_lbl.setText("导出失败：%s" % exc)
            return

        self._target = target
        self._done = True
        self._btn.setEnabled(False)
        self._btn.setText("已导出")
        self._name_lbl.setText("已导出：%s" % os.path.basename(target))
        self._name_lbl.setStyleSheet(
            "color:#28a745; background:transparent; border:none; font-size:11px;")
        self.exported.emit(target)


class PdfExportDialog(QtWidgets.QDialog):
    """报告 PDF 批量导出窗。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("报告 PDF 导出")
        self.resize(460, 520)
        # 对话框底色/文字由全局皮肤给（QDialog 规则）
        self._build()
        self._reload()

    # ------------------------------------------------------------------

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- 顶部栏：标题 + 刷新 ----------------------------------------
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(44)
        bar.setObjectName("headerBar")
        bar.setStyleSheet("QWidget#headerBar { background:%s; border:none; }" % C_BG2)
        bl = QtWidgets.QHBoxLayout(bar)
        bl.setContentsMargins(12, 0, 10, 0)
        bl.setSpacing(10)

        title = QtWidgets.QLabel("报告 PDF 导出")
        title.setStyleSheet("color:%s; background:transparent; border:none;"
                            " font-size:13px; font-weight:bold;" % C_TEXT)
        bl.addWidget(title)

        self._folder_lbl = QtWidgets.QLabel("")
        self._folder_lbl.setStyleSheet("color:%s; background:transparent;"
                                        " border:none; font-size:11px;" % C_TEXT_DIM)
        bl.addWidget(self._folder_lbl, 1)

        refresh = QtWidgets.QPushButton("刷新")
        refresh.setFixedWidth(70)

        refresh.clicked.connect(self._reload)
        bl.addWidget(refresh)
        root.addWidget(bar)

        # -- 列表 --------------------------------------------------------
        self._list_host = QtWidgets.QWidget()
        self._list_layout = QtWidgets.QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(8, 8, 8, 8)
        self._list_layout.setSpacing(6)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(self._list_host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        root.addWidget(scroll, 1)

        # -- 底部状态条 --------------------------------------------------
        self._status = QtWidgets.QLabel("")
        self._status.setFixedHeight(28)
        self._status.setStyleSheet(
            "background:%s; color:%s; border:none; padding:0 12px; font-size:11px;"
            % (C_BG2, C_TEXT_DIM))
        self._status.setWordWrap(False)
        root.addWidget(self._status)

    # ------------------------------------------------------------------

    def _reload(self):
        # 清空旧行
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        self._folder_lbl.setText("来源：%s" % PDF_FOLDER)
        if not os.path.isdir(PDF_FOLDER):
            self._status.setText("目录不存在：%s" % PDF_FOLDER)
            self._list_layout.addStretch(1)
            return

        try:
            files = [os.path.join(PDF_FOLDER, f)
                     for f in os.listdir(PDF_FOLDER)
                     if f.lower().endswith(".pdf")]
        except Exception as exc:                       # noqa: BLE001
            self._status.setText("读取目录失败：%s" % exc)
            self._list_layout.addStretch(1)
            return

        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        if not files:
            self._status.setText("C:\\temp 下没有 PDF 文件")
            self._list_layout.addStretch(1)
            return

        for path in files:
            row = _PdfRow(path)
            row.resolve_name()
            row.exported.connect(self._on_exported)
            self._list_layout.addWidget(row)
        self._list_layout.addStretch(1)
        self._status.setText("共 %d 个 PDF，点击右侧「导出到桌面」" % len(files))

    def _on_exported(self, target):
        self._status.setText("已导出：%s" % target)
