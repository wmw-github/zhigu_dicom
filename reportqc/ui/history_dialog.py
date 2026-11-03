# -*- coding: utf-8 -*-
"""报告质控历史查询窗口。

功能：按患者 / 结论等级 / 时间范围筛选历史检测记录，查看详情，导出 CSV。

CSV 编码用 ``utf-8-sig``（带 BOM）—— 这是让 Excel 直接双击打开不乱码的
唯一可靠方式；用 gbk 会在 Mac / WPS 上出问题，用无 BOM 的 utf-8 则
Excel 默认按 ANSI 解码、中文全变乱码。
"""

import csv
import os

import PyQt5.QtCore as QtCore
import PyQt5.QtGui as QtGui
import PyQt5.QtWidgets as QtWidgets

from ..pacs.pipeline import LEVEL_COLORS, rebuild_result
from .detail_dialog import ReportDetailDialog
from reportqc.ui.skin import lazy_color

__all__ = ["ReportHistoryDialog"]

C_BG = lazy_color("canvas", "#1e2022")
C_BG2 = lazy_color("panel", "#26282b")
C_BORDER = lazy_color("border", "#3d3f41")
C_TEXT = lazy_color("text", "#e8eaec")
C_TEXT_DIM = lazy_color("text_dim", "#9aa0a6")

_LEVEL_FILTER = [
    ("", "全部结论"),
    ("red", "危急"),
    ("orange", "需修改"),
    ("yellow", "需复核"),
    ("green", "通过"),
]

_DAYS_FILTER = [
    (0, "全部时间"),
    (1, "今天"),
    (7, "近 7 天"),
    (30, "近 30 天"),
    (365, "近一年"),
]

_LEVEL_NAME = {"red": "危急", "orange": "需修改", "yellow": "需复核", "green": "通过"}

_COLUMNS = [
    ("id", "编号", 50),
    ("created_at", "检测时间", 145),
    ("patient_name", "患者", 90),
    ("gender", "性别", 45),
    ("age", "年龄", 45),
    ("accession_no", "影像号", 120),
    ("modality", "设备", 50),
    ("overall_level", "结论", 70),
    ("issue_count", "问题数", 60),
    ("critical_count", "危急", 45),
    ("high_count", "高危", 45),
    ("elapsed_ms", "耗时ms", 65),
]


class ReportHistoryDialog(QtWidgets.QDialog):
    """历史记录查询窗。"""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.setWindowTitle("报告质控 — 历史记录")
        self.resize(1080, 620)
        # 对话框底色/文字由全局皮肤给（QDialog 规则），这里不要再设万能
        # background —— 它会盖掉按钮/输入框自己的底色。
        self._db = db
        self._records = []

        self._build()
        self._reload()

    # ------------------------------------------------------------------

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- 筛选栏 -------------------------------------------------------
        filt = QtWidgets.QWidget()
        filt.setFixedHeight(46)
        filt.setObjectName("filterBar")
        filt.setStyleSheet("QWidget#filterBar { background:%s; border:none; }" % C_BG2)
        fl = QtWidgets.QHBoxLayout(filt)
        fl.setContentsMargins(10, 7, 10, 7)
        fl.setSpacing(8)

        self._patient_edit = QtWidgets.QLineEdit()
        self._patient_edit.setPlaceholderText("患者姓名 / 影像号")
        self._patient_edit.setFixedWidth(180)
        self._level_combo = QtWidgets.QComboBox()
        self._days_combo = QtWidgets.QComboBox()
        for text, label in _LEVEL_FILTER:
            self._level_combo.addItem(label, text)
        for days, label in _DAYS_FILTER:
            self._days_combo.addItem(label, days)
        for w in (self._patient_edit, self._level_combo, self._days_combo):
            # 输入框外观交给全局皮肤，只留字号（筛选栏偏紧凑）
            w.setStyleSheet("QLineEdit, QComboBox { font-size:11px; }")

        btn_query = QtWidgets.QPushButton("查询")
        btn_query.clicked.connect(self._reload)
        btn_query.setStyleSheet(self._btn_css())
        self._patient_edit.returnPressed.connect(self._reload)

        self._btn_export = QtWidgets.QPushButton("导出 CSV")
        self._btn_export.clicked.connect(self._export_csv)
        self._btn_export.setStyleSheet(self._btn_css())

        fl.addWidget(QtWidgets.QLabel("筛选："))
        fl.addWidget(self._patient_edit)
        fl.addWidget(self._level_combo)
        fl.addWidget(self._days_combo)
        fl.addWidget(btn_query)
        fl.addStretch(1)
        fl.addWidget(self._btn_export)
        for i in range(fl.count()):
            w = fl.itemAt(i).widget()
            if isinstance(w, QtWidgets.QLabel):
                w.setStyleSheet("color:%s; background:transparent; border:none;"
                                " font-size:11px;" % C_TEXT_DIM)
        root.addWidget(filt)

        # -- 表格 ---------------------------------------------------------
        self._table = QtWidgets.QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels([c[1] for c in _COLUMNS])
        for i, (_key, _label, width) in enumerate(_COLUMNS):
            self._table.setColumnWidth(i, width)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(False)
        self._table.verticalHeader().setVisible(False)
        # 表格外观交给全局皮肤，这里只保留行内边距（QLabel 风格更紧凑）
        self._table.setStyleSheet("QTableWidget::item { padding:3px 4px; }")
        self._table.doubleClicked.connect(self._open_detail)
        root.addWidget(self._table, 1)

        # -- 底部 ---------------------------------------------------------
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(44)
        bar.setObjectName("bottomBar")
        bar.setStyleSheet("QWidget#bottomBar { background:%s; border:none; }" % C_BG2)
        bl = QtWidgets.QHBoxLayout(bar)
        bl.setContentsMargins(12, 7, 12, 7)

        self._status = QtWidgets.QLabel("")
        self._status.setStyleSheet(
            "color:%s; background:transparent; border:none; font-size:11px;" % C_TEXT_DIM)
        bl.addWidget(self._status)
        bl.addStretch(1)

        btn_detail = QtWidgets.QPushButton("查看详情")
        btn_detail.setStyleSheet(self._btn_css())
        btn_detail.clicked.connect(self._open_detail)
        btn_close = QtWidgets.QPushButton("关闭")
        btn_close.setStyleSheet(self._btn_css())
        btn_close.clicked.connect(self.accept)
        bl.addWidget(btn_detail)
        bl.addWidget(btn_close)
        root.addWidget(bar)

    def _btn_css(self):
        """按钮外观交给全局皮肤（v1.21.0 起不再手写深色 QSS）。"""
        return ""

    # ------------------------------------------------------------------

    def _reload(self):
        patient = self._patient_edit.text().strip()
        level = self._level_combo.currentData()
        days = self._days_combo.currentData()
        try:
            self._records = self._db.query_records(
                patient=patient or None,
                level=level or None,
                days=int(days) if days else None,
                limit=1000)
        except Exception as exc:                        # noqa: BLE001
            self._records = []
            self._status.setText("查询失败：%s" % exc)
            return

        self._table.setRowCount(len(self._records))
        for r, rec in enumerate(self._records):
            for c, (key, _label, _w) in enumerate(_COLUMNS):
                val = rec.get(key)
                if key == "overall_level":
                    text = _LEVEL_NAME.get(val, val or "")
                elif val is None:
                    text = ""
                else:
                    text = str(val)
                item = QtWidgets.QTableWidgetItem(text)
                if key == "overall_level" and val in LEVEL_COLORS:
                    item.setForeground(QtGui.QColor(LEVEL_COLORS[val]))
                self._table.setItem(r, c, item)

        total = len(self._records)
        n_crit = sum(1 for x in self._records if x.get("overall_level") == "red")
        self._status.setText(
            "共 %d 条记录" % total + ("，其中危急 %d 条" % n_crit if n_crit else ""))

    # ------------------------------------------------------------------

    def _current_record(self):
        row = self._table.currentRow()
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def _open_detail(self):
        rec = self._current_record()
        if rec is None:
            return
        try:
            issues = self._db.query_issues(int(rec["id"])) or []
        except Exception:                              # noqa: BLE001
            issues = []
        result = rebuild_result(rec, issues)
        dlg = ReportDetailDialog(result, record_id=int(rec["id"]), db=self._db,
                                 parent=self)
        dlg.show()
        dlg.raise_()

    # ------------------------------------------------------------------

    def _export_csv(self):
        if not self._records:
            QtWidgets.QMessageBox.information(self, "导出", "当前没有可导出的记录。")
            return
        default_name = "报告质控记录_%s.csv" % QtCore.QDate.currentDate().toString("yyyyMMdd")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出 CSV", default_name, "CSV 文件 (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        # 两级明细：一行一个问题，记录字段重复，便于在 Excel 里筛选
        header = ["记录编号", "检测时间", "患者", "性别", "年龄", "影像号",
                  "检查号", "设备", "检查部位", "整体结论",
                  "问题等级", "问题类型", "问题描述", "修改建议",
                  "规则置信度", "OCR置信度", "识别存疑", "用户标记"]
        rows = [header]
        for rec in self._records:
            try:
                issues = self._db.query_issues(int(rec["id"])) or []
            except Exception:                          # noqa: BLE001
                issues = []
            base = [
                rec.get("id"), rec.get("created_at"), rec.get("patient_name"),
                rec.get("gender"), rec.get("age"), rec.get("accession_no"),
                rec.get("exam_no"), rec.get("modality"), rec.get("body_part"),
                _LEVEL_NAME.get(rec.get("overall_level"), rec.get("overall_level")),
            ]
            if not issues:
                rows.append(base + ["", "", "（无问题）", "", "", "", "", ""])
                continue
            for it in issues:
                rows.append(base + [
                    _LEVEL_NAME.get(it.get("level"), it.get("level")),
                    it.get("kind"), it.get("message"), it.get("suggestion"),
                    it.get("confidence"), it.get("ocr_conf"),
                    "是" if it.get("ocr_suspect") else "",
                    {"false_positive": "误报", "confirmed": "确认有问题"}.get(
                        it.get("user_verdict"), ""),
                ])

        try:
            # utf-8-sig：带 BOM，Excel 双击打开不乱码
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerows(rows)
        except Exception as exc:                        # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "导出失败", str(exc))
            return

        QtWidgets.QMessageBox.information(
            self, "导出完成",
            "已导出 %d 行（含表头）到：\n%s" % (len(rows), os.path.basename(path)))
