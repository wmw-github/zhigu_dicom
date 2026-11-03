# -*- coding: utf-8 -*-
"""授权激活对话框。

给主程序与「关于」入口共用。设计上刻意保持简单：
一个只读的机器码（供用户报给供应商）+ 一个授权码输入框 + 激活按钮。

**为什么单独成文件**：激活入口以后可能出现在多个地方（启动拦截、
菜单、设置页），集中在一处免得各写一遍。
"""

import os
import sys

from PyQt5 import QtCore, QtGui, QtWidgets


import theme
import license_bridge


class ActivateDialog(QtWidgets.QDialog):
    """授权激活窗口"""

    def __init__(self, parent=None, message=""):
        super(ActivateDialog, self).__init__(parent)
        self.setWindowTitle("软件授权")
        self.setMinimumWidth(460)
        # 模态：必须先处理授权才能继续用主程序
        self.setWindowModality(QtCore.Qt.ApplicationModal)
        self.setWindowFlags(
            self.windowFlags() & ~QtCore.Qt.WindowContextHelpButtonHint)

        self._build(message)
        self._fill_status()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build(self, message):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        if message:
            tip = QtWidgets.QLabel(message)
            tip.setWordWrap(True)
            theme.role(tip, "warn")
            layout.addWidget(tip)

        # --- 机器码（只读，供用户报给供应商）---
        machine_box = QtWidgets.QGroupBox("本机机器码")
        machine_layout = QtWidgets.QVBoxLayout(machine_box)

        self.machine_label = QtWidgets.QLabel("读取中…")
        self.machine_label.setFont(QtGui.QFont("Consolas", 12))
        self.machine_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        machine_layout.addWidget(self.machine_label)

        btn_row = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("复制机器码")
        copy_btn.clicked.connect(self._on_copy)
        btn_row.addWidget(copy_btn)
        copy_btn.setToolTip("把这串码发给供应商，用于生成授权码")
        btn_row.addStretch(1)
        machine_layout.addLayout(btn_row)

        layout.addWidget(machine_box)

        # --- 授权码输入 ---
        key_box = QtWidgets.QGroupBox("授权码")
        key_layout = QtWidgets.QVBoxLayout(key_box)

        self.key_edit = QtWidgets.QLineEdit()
        self.key_edit.setPlaceholderText("在此粘贴供应商提供的授权码")
        self.key_edit.setFont(QtGui.QFont("Consolas", 10))
        # 回车即激活，省得再点按钮
        self.key_edit.returnPressed.connect(self._on_activate)
        key_layout.addWidget(self.key_edit)

        layout.addWidget(key_box)

        # --- 状态提示 ---
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(34)
        layout.addWidget(self.status_label)

        # --- 按钮 ---
        buttons = QtWidgets.QDialogButtonBox()
        self.activate_btn = QtWidgets.QPushButton("激活")
        # 主操作走强调色，跟对话框里的其他按钮区分开
        theme.role(self.activate_btn, "primary")
        self.activate_btn.clicked.connect(self._on_activate)
        buttons.addButton(self.activate_btn, QtWidgets.QDialogButtonBox.AcceptRole)
        quit_btn = buttons.addButton("退出", QtWidgets.QDialogButtonBox.RejectRole)
        quit_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _fill_status(self):
        """填机器码 + 当前授权状态"""
        try:
            self.machine_label.setText(license_bridge.machine_code_display())
        except Exception as exc:                    # noqa: BLE001
            self.machine_label.setText("（读取失败）")
            self._set_status("机器码读取失败：%s" % exc, error=True)

        try:
            st = license_bridge.license_status()
            if st["ok"]:
                extra = "" if st["hours_left"] < 0 else \
                    "（剩余约 %d 小时）" % st["hours_left"]
                self._set_status("当前状态：%s%s" % (st["message"], extra))
        except Exception as exc:                    # noqa: BLE001
            self._set_status("读取授权状态失败：%s" % exc, error=True)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _set_status(self, text, error=False):
        # 颜色走皮肤语义色：浅色皮肤下原来的浅绿/浅红会和白底糊在一起
        theme.role(self.status_label, "danger" if error else "success")
        self.status_label.setText(text)

    def _on_copy(self):
        try:
            code = license_bridge.machine_code_display()
            QtWidgets.QApplication.clipboard().setText(code)
            self._set_status("机器码已复制到剪贴板。")
        except Exception as exc:                    # noqa: BLE001
            self._set_status("复制失败：%s" % exc, error=True)

    def _on_activate(self):
        key = self.key_edit.text().strip()
        if not key:
            self._set_status("请先输入授权码。", error=True)
            return

        # 激活涉及文件写入，稍作等待反馈；不做线程以免引入新的退出问题
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            ok, msg = license_bridge.activate(key)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        if ok:
            self._set_status("激活成功，正在进入主程序…")
            QtWidgets.QMessageBox.information(
                self, "激活成功", "授权激活成功。\n\n%s" % msg)
            self.accept()
        else:
            # 失败必须让用户看到确切原因（铁律 46：不能只 print）
            self._set_status(msg, error=True)
            QtWidgets.QMessageBox.warning(self, "激活失败", msg)


def require_license(parent=None):
    """启动时的授权门禁。

    返回 True 表示可以继续启动主程序。

    流程：
      1. 已有有效授权 → 直接放行
      2. 从未激活 → 自动开 72 小时试用，并提示用户
      3. 试用到期/授权失效 → 弹激活框让用户输入授权码
      4. 模块本身损坏 → 明确报错（不静默放行，也不静默拦截）
    """
    TRIAL_HOURS = 72

    try:
        status = license_bridge.license_status()
    except Exception as exc:                        # noqa: BLE001
        QtWidgets.QMessageBox.critical(
            parent, "授权检查失败",
            "授权检查时发生错误，软件无法确认使用许可：\n%s" % exc)
        return False

    if status["ok"]:
        return True

    # 安装损坏 → 明确报错，不放行
    if status["kind"] == "broken":
        QtWidgets.QMessageBox.critical(
            parent, "授权检查失败",
            "%s\n\n软件无法确认使用许可，无法启动。" % status["message"])
        return False

    # 从未激活 → 给试用
    if not license_bridge.has_license_file():
        ok, msg = license_bridge.start_trial(TRIAL_HOURS)
        if ok:
            QtWidgets.QMessageBox.information(
                parent, "欢迎试用",
                "本软件尚未授权，已为这台电脑开启 %d 小时试用期。\n\n"
                "试用期结束后需要输入正式授权码才能继续使用。\n\n"
                "如需正式授权，请联系供应商。" % TRIAL_HOURS)
            return True
        # 试用开不出来也要让用户知道原因
        QtWidgets.QMessageBox.warning(
            parent, "试用初始化失败", msg)
        return False

    # 授权无效（过期 / 换机 / 旧格式残留）→ 弹激活框让用户输入授权码。
    # 注意：旧格式（legacy_format）**也走这条路**，而不是直接拦死 ——
    # 用户必须有机会通过输入新授权码自救。
    hint = "授权无效：%s" % status["message"]
    if status["kind"] == "legacy_format":
        hint = ("检测到本机的旧版本授权文件无法继续使用。\n"
                "请输入新的授权码完成激活；如需帮助请联系供应商。")
    dlg = ActivateDialog(
        parent,
        message="%s\n\n如需续期或重新授权，请联系供应商。" % hint)
    return dlg.exec_() == QtWidgets.QDialog.Accepted


def open_activate_dialog(parent=None):
    """供「关于/授权」入口手动打开激活窗口（已激活时也能换机器重激活）"""
    dlg = ActivateDialog(parent, message="")
    return dlg.exec_() == QtWidgets.QDialog.Accepted
