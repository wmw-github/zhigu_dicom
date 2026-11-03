#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线升级界面

分三层，互不纠缠：
  * updater.py   纯逻辑（拉索引、验签、下载、校验、解压），不 import PyQt5
  * 本文件       线程 + 对话框 + 启动安装器
  * main.py      只挂一个按钮和一次后台检查

线程规则（踩过的坑）：
  * QThread 实例必须挂在 self 上，否则被 GC 掉，表现为「点了没反应」或硬崩；
  * 网络与磁盘操作全在子线程，主线程只更新界面；
  * 下载可取消，用 threading.Event 传信号，不用 terminate()。
"""
import os
import subprocess
import threading

from PyQt5 import QtCore, QtWidgets

import theme
import updater

CACHE_DIR_NAME = '_update_cache'


def cache_dir(app_dir):
    d = os.path.join(app_dir, CACHE_DIR_NAME)
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def human_size(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%d B' % n) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0


def install_and_restart(app_dir, zip_path):
    """把补丁落地并启动安装器，装完自动重新打开软件。

    返回补丁目录。调用方负责随后退出本进程 —— 一定要退，
    否则 Python 还占着 pyc / 日志句柄，安装器覆盖文件会失败。
    """
    patch_dir = updater.stage_patch(zip_path, app_dir)
    script = os.path.join(patch_dir, 'apply_patch.py')
    if not os.path.isfile(script):
        raise RuntimeError('补丁包不完整：缺少 apply_patch.py')

    env = dict(os.environ)
    env['CT3D_QUIET'] = '1'          # 抑制交互，失败也不等按键
    flags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
    py = os.path.join(app_dir, 'pyenv', 'python.exe')
    if os.path.isfile(py):
        # 直接调解释器，不经「升级.bat」：少一层 cmd 解析，
        # 客户机路径带空格 / 中文目录时不会被拆坏（曾因此整次升级没启动）。
        cmd = [py, script, '--target', app_dir, '--source', patch_dir,
               '--relaunch']
    else:
        launcher = os.path.join(patch_dir, u'升级.bat')
        if not os.path.isfile(launcher):
            raise RuntimeError('补丁包不完整：既没有 pyenv\\python.exe，也没有「升级.bat」')
        cmd = ['cmd', '/c', launcher, '--relaunch']
    subprocess.Popen(cmd, cwd=patch_dir, env=env, creationflags=flags,
                     close_fds=True)
    return patch_dir


# ── 检查更新 ────────────────────────────────────────────────────────────────

class CheckWorker(QtCore.QThread):
    found = QtCore.pyqtSignal(dict)
    uptodate = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, base_url, current_version, parent=None):
        super(CheckWorker, self).__init__(parent)
        self._base = base_url
        self._cur = current_version

    def run(self):
        try:
            plan = updater.plan_update(self._base, self._cur)
        except Exception as e:                       # noqa: BLE001
            self.failed.emit(str(e))
            return
        if plan:
            self.found.emit(plan)
        else:
            self.uptodate.emit(self._cur)


# ── 下载 ────────────────────────────────────────────────────────────────────

class DownloadWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, str)       # 百分比, 文案
    done = QtCore.pyqtSignal(str)                # 最后一个 zip 的本地路径
    failed = QtCore.pyqtSignal(str)

    def __init__(self, plan, dest_dir, parent=None):
        super(DownloadWorker, self).__init__(parent)
        self._plan = plan
        self._dest = dest_dir
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        steps = self._plan['steps']
        total_bytes = max(1, sum(s.get('size') or 0 for s in steps))
        base_done = 0
        last_path = None
        try:
            for i, step in enumerate(steps):
                self.progress.emit(
                    int(100.0 * base_done / total_bytes),
                    '正在下载 %s → %s（%d/%d）'
                    % (step['from'], step['to'], i + 1, len(steps)))

                def _cb(done, _total, _base=base_done):
                    pct = int(100.0 * (_base + done) / total_bytes)
                    self.progress.emit(pct, '正在下载 %s → %s'
                                       % (step['from'], step['to']))

                last_path = updater.download_patch(
                    self._plan['base_url'], step, self._dest,
                    progress_cb=_cb, cancel=self._cancel)
                base_done += (step.get('size') or 0)
            self.progress.emit(100, '下载完成，正在校验')
            self.done.emit(last_path or '')
        except Exception as e:                       # noqa: BLE001
            self.failed.emit(str(e))


# ── 对话框 ──────────────────────────────────────────────────────────────────

class UpdateDialog(QtWidgets.QDialog):
    """检查 → 展示 → 下载 → 落地。关闭时自动取消下载。"""

    def __init__(self, app_dir, current_version, parent=None, auto_install=False,
                 base_url=None):
        super(UpdateDialog, self).__init__(parent)
        self._app_dir = app_dir
        self._cur = current_version
        # 服务器地址：显式传入 > app_settings.json 里的 update_base_url > 内置默认。
        # 现场换过主机后，这里必须跟着换，否则「检查更新」永远连老地址。
        self._base_url = base_url or updater.load_base_url(app_dir)
        self._plan = None
        self._zip = None
        self._checker = None
        self._downloader = None
        self._auto_install = auto_install
        self.restart_requested = False

        self.setWindowTitle('软件更新')
        self.setMinimumWidth(480)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        self.title = QtWidgets.QLabel('正在检查更新…')
        f = self.title.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        self.title.setFont(f)
        lay.addWidget(self.title)

        self.detail = QtWidgets.QLabel('当前版本 %s' % current_version)
        theme.role(self.detail, 'muted')
        self.detail.setWordWrap(True)
        lay.addWidget(self.detail)

        self.notes = QtWidgets.QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMinimumHeight(96)
        self.notes.setMaximumHeight(168)
        self.notes.setVisible(False)
        lay.addWidget(self.notes)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        self.status = QtWidgets.QLabel('')
        theme.role(self.status, 'muted')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        lay.addStretch(1)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.btn_later = QtWidgets.QPushButton('关闭')
        self.btn_later.clicked.connect(self._on_later_clicked)
        theme.role(self.btn_later, 'ghost')
        row.addWidget(self.btn_later)

        self.btn_go = QtWidgets.QPushButton('立即升级')
        self.btn_go.setDefault(True)
        self.btn_go.setVisible(False)
        self.btn_go.clicked.connect(self._start_download)
        theme.role(self.btn_go, 'primary')
        row.addWidget(self.btn_go)
        lay.addLayout(row)

        QtCore.QTimer.singleShot(0, self._start_check)

    # ── 检查 ────────────────────────────────────────────────────────────
    def _start_check(self):
        self._checker = CheckWorker(self._base_url, self._cur, self)
        self._checker.found.connect(self._on_found)
        self._checker.uptodate.connect(self._on_uptodate)
        self._checker.failed.connect(self._on_failed)
        self._checker.finished.connect(self._checker.deleteLater)
        self._checker.start()

    def _on_found(self, plan):
        self._plan = plan
        self.title.setText('发现新版本 %s' % plan['latest'])
        self.detail.setText(
            '当前版本 %s  →  最新版本 %s\n共 %d 个更新包，合计 %s'
            % (self._cur, plan['latest'], len(plan['steps']),
               human_size(plan['total_size'])))
        if plan.get('notes'):
            self.notes.setPlainText(plan['notes'])
            self.notes.setVisible(True)
        self.btn_go.setVisible(True)
        self.btn_later.setText('稍后')
        self.status.setText('点击「立即升级」开始下载。')
        if self._auto_install:
            self._start_download()

    def _on_uptodate(self, cur):
        self.title.setText('已是最新版本')
        self.detail.setText('当前版本 %s，没有可用的更新。' % cur)
        self.status.setText('')
        self.btn_go.setVisible(False)

    def _on_failed(self, msg):
        self.title.setText('检查更新失败')
        self.detail.setText('')
        # 把实际用的服务器地址写出来：换过主机后连错地方，看这一行就能发现，
        # 不用再去翻 app_settings.json。
        self.status.setText('%s\n服务器：%s' % (msg, self._base_url))
        self.status.setWordWrap(True)
        self.btn_go.setVisible(False)

    # ── 下载 ────────────────────────────────────────────────────────────
    def _on_later_clicked(self):
        """下载中这个按钮是「取消」，其余时候是「关闭」。"""
        if self._downloader is not None and self._downloader.isRunning():
            self._downloader.cancel()
            self.status.setText('正在取消…')
            self.btn_later.setEnabled(False)
            return
        self.reject()

    def _start_download(self):
        if not self._plan:
            return
        self.btn_go.setEnabled(False)
        self.btn_go.setText('升级中…')
        self.btn_later.setText('取消')
        self.btn_later.setEnabled(True)
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.status.setText('正在连接升级服务器…')

        self._downloader = DownloadWorker(self._plan, cache_dir(self._app_dir), self)
        self._downloader.progress.connect(self._on_progress)
        self._downloader.done.connect(self._on_downloaded)
        self._downloader.failed.connect(self._on_download_failed)
        self._downloader.start()

    def _on_progress(self, pct, text):
        self.bar.setValue(max(0, min(100, pct)))
        if text:
            self.status.setText(text)

    def _on_download_failed(self, msg):
        self.bar.setVisible(False)
        self.btn_go.setEnabled(True)
        self.btn_go.setText('重试')
        self.btn_later.setText('关闭')
        self.btn_later.setEnabled(True)
        if '取消' in msg:
            self.status.setText('已取消下载。')
        else:
            self.status.setText('下载失败：%s' % msg)

    def _on_downloaded(self, zip_path):
        if not zip_path:
            self._on_download_failed('没拿到补丁文件')
            return
        self._zip = zip_path
        self.bar.setValue(100)
        self.status.setText('下载完成，准备安装。')
        self.btn_later.setText('关闭')
        self.btn_later.setEnabled(True)
        self._apply()

    def _apply(self):
        """确认后落地。默认直接问一句，避免误点导致软件重启。"""
        r = QtWidgets.QMessageBox.question(
            self, '开始升级',
            '升级过程中软件会自动关闭，装完自动重新打开。\n\n'
            '请先保存手头的工作。现在升级吗？',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes)
        if r != QtWidgets.QMessageBox.Yes:
            self.status.setText('已取消，更新包已下载好，下次可以直接装。')
            self.btn_go.setEnabled(True)
            self.btn_go.setText('立即升级')
            self.btn_later.setEnabled(True)
            return
        try:
            install_and_restart(self._app_dir, self._zip)
        except Exception as e:                       # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, '升级启动失败', str(e))
            self.status.setText('升级启动失败：%s' % e)
            self.btn_go.setEnabled(True)
            return
        self.restart_requested = True
        self.accept()

    def closeEvent(self, ev):
        if self._downloader and self._downloader.isRunning():
            self._downloader.cancel()
            self._downloader.wait(3000)
        super(UpdateDialog, self).closeEvent(ev)


# ── 后台静默检查（启动时用，不弹窗）─────────────────────────────────────────

class SilentChecker(QtCore.QObject):
    """启动后静默查一次；有新版本才回调，失败静默。"""

    def __init__(self, base_url, current_version, parent=None):
        super(SilentChecker, self).__init__(parent)
        self._worker = None
        self._base = base_url
        self._cur = current_version

    def start(self, on_found):
        self._worker = CheckWorker(self._base, self._cur, self)
        self._worker.found.connect(on_found)
        self._worker.uptodate.connect(lambda *_: None)
        self._worker.failed.connect(lambda *_: None)   # 静默：没网不是错误
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()
