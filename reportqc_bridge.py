# -*- coding: utf-8 -*-
"""CT3D 主程序 ↔ 报告质控模块（``reportqc``）的集成桥接层。

与 ``spine_bridge.py`` / ``xray_bridge.py`` 同构的进程内集成，但本模块
额外承担**控制器**职责：把「区域截图 → OCR → 质控 → 小窗显示」串成
一条流水线，并保证 OCR 推理不阻塞主程序 UI。

交互模型（v1.16.0 起：常驻小窗 + 区域只设一次）
---------------------------------------------
* 程序启动 → :func:`startup_auto_show` 自动弹出悬浮小窗（就绪态，不识别）；
* 首次用「设置区域」框选一次 → :meth:`ReportQCController.start_pick_and_check`
  把区域存进 QSettings（:mod:`reportqc.pacs.region_store`）并立即识别；
* 之后每次打开新报告 → 点「刷新」/双击小窗 →
  :meth:`ReportQCController.refresh_check` 按保存区域截图识别，**不再框选**。

主程序侧的调用面
----------------
1. ``reportqc_available()`` —— 模型是否就位（纯路径判定，不触发 import）；
2. ``startup_auto_show()`` —— 启动时自动显示小窗（main() 调用）；
3. ``get_controller()`` —— 拿到 :class:`ReportQCController` 单例。

线程模型
--------
* 框选遮罩、截图、小窗显示：**主线程**（Qt GUI 对象只能在主线程建）；
* OCR + 质控（1~3s）：**后台 QThread**，完成后经信号回到主线程渲染。
* 引擎 / 规则检查器在后台线程里**懒加载并缓存**，第一次检测稍慢
  （模型加载约 0.3s），之后每次只有纯推理耗时。

Win7 兼容：Python 3.8 语法；不新增第三方依赖；ORT session 线程安全。
"""

import os
import sys
import threading

import PyQt5.QtCore as QtCore
import PyQt5.QtWidgets as QtWidgets

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 可用性探测
# ---------------------------------------------------------------------------

_MODEL_FILES = (
    "ch_PP-OCRv4_det_infer.onnx",
    "ch_PP-OCRv4_rec_infer.onnx",
)


def reportqc_available():
    """OCR 模型是否就位（不 import 重模块，供菜单置灰判断）。"""
    models_dir = os.path.join(_HERE, "reportqc", "models", "ocr")
    for fn in _MODEL_FILES:
        if not os.path.isfile(os.path.join(models_dir, fn)):
            return False
    return True


# ---------------------------------------------------------------------------
# 控制器
# ---------------------------------------------------------------------------

def _profile_name(title, exe):
    """从窗口标题/进程名截一个短名，给区域配置当标签。

    PACS 窗口标题常是「××医学影像工作站 - 报告编辑 - 张三」，带患者信息，
    存下来换个病人就对不上了，所以只取分隔符前的一段当**显示名**；
    真正的匹配键是 exe（见 :mod:`reportqc.pacs.region_store`）。
    """
    t = (title or "").strip()
    if t:
        for sep in (" - ", " — ", " – ", "－", "-"):
            if sep in t:
                head = t.split(sep)[0].strip()
                if head:
                    t = head
                break
        return (t or "识别区域")[:16]
    e = (exe or "").strip()
    if e.lower().endswith(".exe"):
        e = e[:-4]
    return (e or "识别区域")[:16]


class _Worker(QtCore.QThread):
    """把任意可调用对象丢到后台线程跑，完成/失败都发信号回主线程。"""

    done = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._args = ()
        self._kwargs = {}

    def configure(self, *args, **kwargs):
        self._args, self._kwargs = args, kwargs
        return self

    def run(self):                                   # noqa: D102
        try:
            self.done.emit(self._fn(*self._args, **self._kwargs))
        except Exception as exc:                     # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.failed.emit(str(exc))


class ReportQCController(QtCore.QObject):
    """报告质控流水线控制器（进程内单例，见 :func:`get_controller`）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._win = None                # MiniReportWindow
        self._engine = None             # PPOCREngine（懒加载）
        self._checker = None            # SmartTextChecker（懒加载）
        self._last_rect = None          # 本次实际使用的区域（落库留痕）
        self._profile = None            # 本次命中/新建的区域配置 dict
        self._watcher = None            # ForegroundWatcher（懒加载）
        self._watch_timer = None        # 前台窗口轮询（1s）
        self._busy = False              # 防止重复点击叠加多个 worker
        self._worker = None
        self._lock = threading.Lock()
        # 数据层（懒加载）+ 最近一次结果，供详情窗/历史窗使用
        self._db = None
        self._last_result = None
        self._last_record_id = None
        self._dialogs = []              # 持有详情/历史窗引用，防被 GC
        self._picker = None             # 持有框选遮罩引用，防被 GC

    # -- 懒加载 ----------------------------------------------------------

    def _get_engine(self):
        if self._engine is None:
            from reportqc.ocr.ppocr_engine import PPOCREngine
            from reportqc.ui.settings_dialog import get_ocr_scale
            self._engine = PPOCREngine(scale=get_ocr_scale())
        return self._engine

    def _get_checker(self):
        if self._checker is None:
            from reportqc.qc.checker import SmartTextChecker
            # enable_ai=False：AI 检测需要 184MB 模型 + torch，绿色包不带。
            # 纯规则部分零依赖，是 CT3D 环境下的完整可用集。
            self._checker = SmartTextChecker(enable_ai=False)
        return self._checker

    def _get_db(self):
        """数据层懒加载。库文件不可写时会回退到 APPDATA，失败返回 None。"""
        if self._db is not None:
            return self._db
        try:
            from reportqc.qc.database import get_database
            self._db = get_database()
        except Exception as exc:                     # noqa: BLE001
            print("[reportqc] 数据库初始化失败: %s" % exc)
            self._db = None
        return self._db

    # -- 识别区域（多套配置，按前台窗口自动选） --------------------------

    def _ensure_watcher(self):
        """前台窗口监听器（1s 轮询，懒启动）。

        为什么需要轮询而不是「刷新时现取前台窗口」：用户是点**小窗按钮**
        触发刷新的，那一刻前台窗口就是小窗自己，现取必然拿到 pythonw.exe，
        两套区域都匹配不上。轮询只记录「最近一个不属于本进程的前台窗口」，
        点小窗时它自然还留着用户上一步所在的写报告软件。
        """
        if self._watcher is not None:
            return self._watcher
        try:
            from reportqc.pacs.windowinfo import ForegroundWatcher
        except Exception as exc:                     # noqa: BLE001
            print("[reportqc] 前台窗口探测不可用: %s" % exc)
            return None
        try:
            self._watcher = ForegroundWatcher()
            self._watcher.poll()
            timer = QtCore.QTimer(self)
            timer.setInterval(1000)
            timer.timeout.connect(self._poll_foreground)
            timer.start()
            self._watch_timer = timer
        except Exception as exc:                     # noqa: BLE001
            print("[reportqc] 前台窗口轮询启动失败: %s" % exc)
        return self._watcher

    def _poll_foreground(self):
        try:
            if self._watcher is not None:
                self._watcher.poll()
        except Exception:                            # noqa: BLE001
            pass

    def _current_profile(self):
        """按当前前台窗口挑一套区域配置；挑不到返回 ``None``。

        侧写 ``self._profile`` / ``self._last_rect``，落库时带上实际用的区域。
        """
        exe, title = ("", "")
        w = self._ensure_watcher()
        if w is not None:
            exe, title = w.last()
        try:
            from reportqc.pacs.region_store import match_profile
            p = match_profile(exe, title)
        except Exception as exc:                     # noqa: BLE001
            print("[reportqc] 读取识别区域失败: %s" % exc)
            p = None
        self._profile = p
        self._last_rect = p["rect"] if p else None
        return p

    def _load_region(self):
        """当前应使用的识别区域 QRect（没有则 None）。"""
        p = self._current_profile()
        return p["rect"] if p else None

    def _store_region(self, rect):
        """保存区域，并绑定到「框选时的前台窗口」。返回配置 dict 或 None。

        同一软件再次框选会覆盖原配置而不是新增 —— 医生每点一次
        「设置区域」就多一条重复配置是很烦人的事。
        """
        self._last_rect = rect
        exe, title = ("", "")
        if self._watcher is not None:
            exe, title = self._watcher.last()
        try:
            from reportqc.pacs.region_store import save_profile
            p = save_profile(rect, exe=exe, title=title,
                             name=_profile_name(title, exe))
            if p is None:
                print("[reportqc] 识别区域保存失败（不影响本次识别）")
            else:
                self._profile = p
            return p
        except Exception as exc:                     # noqa: BLE001
            print("[reportqc] 识别区域保存异常: %s" % exc)
            return None

    # -- 对外动作 --------------------------------------------------------

    def show_window(self):
        """显示悬浮小窗（就绪态）。

        主程序启动（:func:`startup_auto_show`）和左栏「报告质控」按钮
        都走这里。**不自动识别**：程序刚起来时主窗口可能盖住 PACS 报告区，
        自动截图会截到本程序自己的界面 → 误报错误患者信息。
        """
        if not self._ensure_window():
            return
        self._ensure_watcher()
        try:
            from reportqc.pacs.region_store import load_profiles
            n = len(load_profiles())
        except Exception:                            # noqa: BLE001
            n = 0
        # 只传「有几套已保存的区域」，不再传尺寸坐标（小窗已不展示区域大小）
        try:
            self._win.show_ready(n > 0, n)
        except TypeError:
            try:
                self._win.show_ready(n > 0)          # 旧签名（测试桩）
            except Exception as exc:                 # noqa: BLE001
                print("[reportqc] 就绪态渲染异常: %s" % exc)
        except Exception as exc:                     # noqa: BLE001
            print("[reportqc] 就绪态渲染异常: %s" % exc)
        self._win.show()
        self._win.raise_()
        self._win.activateWindow()

    def refresh_check(self):
        """刷新：按**当前前台窗口对应的区域**截图识别，不弹框选。

        悬浮小窗的「刷新」按钮与双击窗口都走这里。
        还没有任何区域配置时，退化为直接弹框选（首次使用引导）。
        """
        if self._busy:
            return
        if not self._ensure_window():
            return
        profile = self._current_profile()
        if profile is None:
            try:
                from reportqc.pacs.region_store import load_profiles
                has_any = bool(load_profiles())
            except Exception:                        # noqa: BLE001
                has_any = False
            if not has_any:
                self.start_pick_and_check()          # 首次：先设置区域
            else:
                # 配过多套（多个写报告软件），当前窗口一套都对不上
                self._show_error(
                    "当前窗口没有对应的识别区域。\n"
                    "请切到写报告软件后点「设置区域」框一次。")
            return

        rect = profile["rect"]
        from reportqc.pacs.capture import grab_rect, pixmap_to_bgr
        pix = grab_rect(rect)
        if pix is None:
            self._show_error("截图失败，请确认报告窗口未被遮挡后重试")
            return
        img = pixmap_to_bgr(pix)
        if img is None:
            self._show_error("截图数据转换失败")
            return
        self._run_pipeline(img)

    def start_pick_and_check(self):
        """「设置区域」：框选 → 保存区域 → 截图 → 后台质控 → 小窗显示。

        **回调式，不阻塞主事件循环**：框选结果通过 ``ScreenPicker.finished``
        信号回来，而不是用嵌套 ``QEventLoop`` 等。

        为什么不用阻塞式（2026-10-24 实测教训）
        -------------------------------------
        原来调 ``pick_region()``（内部 ``loop.exec_()``）。一旦 ``finished``
        没发射 —— 窗口标志带 ``WindowDoesNotAcceptFocus`` 导致 ESC 失效，
        或拖拽释放事件被系统吞掉 —— 嵌套循环就永久挂起，用户表现为
        「框选卡住、无法退出」，只能任务管理器杀进程。
        回调式从结构上消除这个失败模式：即使信号没来，主程序照常响应。
        """
        if self._busy:
            return
        if not self._ensure_window():
            return
        self._ensure_watcher()

        from reportqc.pacs.picker import ScreenPicker
        picker = ScreenPicker()
        picker.finished.connect(self._on_region_picked)
        # 持有引用：picker 是顶层无父窗口，不存起来会被 GC 掉
        self._picker = picker
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def _on_region_picked(self, rect):
        """框选结束回调（``ScreenPicker.finished`` 驱动，主线程执行）。"""
        picker = self.sender()
        if picker is not None:
            try:
                picker.finished.disconnect(self._on_region_picked)
            except Exception:                          # noqa: BLE001
                pass
            picker.deleteLater()
        if self._picker is picker:
            self._picker = None

        if rect is None:
            return                                     # 用户取消（ESC/右键/误触）
        # 区域只设一次：存下来并**绑定到框选时的前台窗口**，
        # 之后点「刷新」按当前窗口自动选区域，两套写报告软件各框一次即可
        prof = self._store_region(rect)
        if prof is not None:
            print("[reportqc] 识别区域已保存：%s（%s）"
                  % (prof.get("name") or "", prof.get("exe") or "未识别进程"))

        from reportqc.pacs.capture import grab_rect, pixmap_to_bgr
        pix = grab_rect(rect)
        if pix is None:
            self._show_error("截图失败，请重试")
            return
        img = pixmap_to_bgr(pix)
        if img is None:
            self._show_error("截图数据转换失败")
            return
        self._run_pipeline(img)

    def recheck_last(self):
        """[兼容旧入口] 等同 :meth:`refresh_check`。"""
        self.refresh_check()

    def close_window(self):
        if self._win is not None:
            self._win.close()

    def stop_watcher(self):
        """停掉前台窗口轮询（主程序退出时调）。

        不停的话，进程退出瞬间定时器可能回调到已析构的 Qt 对象上，
        在 Win7 上表现为退出时弹一次 APPCRASH。
        """
        t = getattr(self, "_watch_timer", None)
        if t is not None:
            try:
                t.stop()
            except Exception:                        # noqa: BLE001
                pass
            self._watch_timer = None
        self._watcher = None

    def open_history(self):
        """打开历史记录窗。"""
        db = self._get_db()
        if db is None:
            QtWidgets.QMessageBox.warning(
                None, "报告质控", "数据库不可用，无法查看历史记录。")
            return
        try:
            from reportqc.ui.history_dialog import ReportHistoryDialog
        except Exception as exc:                     # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                None, "报告质控", "历史界面加载失败：%s" % exc)
            return
        dlg = ReportHistoryDialog(db)
        dlg.show()
        dlg.raise_()
        self._dialogs.append(dlg)

    def open_pdf_export(self):
        """打开报告 PDF 导出窗（列举 C:\\temp 的 PDF 并导出到桌面）。"""
        try:
            from reportqc.ui.pdf_export_dialog import PdfExportDialog
        except Exception as exc:                     # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                None, "报告质控", "PDF 导出界面加载失败：%s" % exc)
            return
        dlg = PdfExportDialog()
        dlg.show()
        dlg.raise_()
        self._dialogs.append(dlg)

    def open_settings(self, parent=None):
        """打开质控设置窗（OCR 放大倍数 / 显示开关 / 忽略词 / 自定义规则）。

        v1.23.1 起入口在**主程序「设置 → 报告质控 → 质控设置…」**，
        左侧栏的「质控设置」按钮已移除（设置类入口集中到一处）。
        「启动时显示悬浮小窗」不再在这个窗里，见主设置。

        ``parent`` 传主「设置」对话框：它是模态的，这里再弹一个无父窗的
        对话框会被压在后面看着像"点了没反应"。
        """
        try:
            from reportqc.ui.settings_dialog import ReportQCSettingsDialog
        except Exception as exc:                     # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                None, "报告质控", "设置界面加载失败：%s" % exc)
            return
        dlg = ReportQCSettingsDialog(db=self._get_db(), parent=parent)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            # 放大倍数改了要让引擎重建，否则下次检测还用旧 scale
            self._engine = None

    def _apply_mini_visibility(self):
        """按当前「启动时显示悬浮小窗」设置，立即显示/关闭悬浮小窗。

        只读 QSettings，不依赖重启。手动「报告质控」按钮仍随时可打开
        （:meth:`show_window` 不受此开关限制）。
        """
        try:
            from reportqc.ui.settings_dialog import get_show_mini
        except Exception:                            # noqa: BLE001
            return
        if get_show_mini():
            self.show_window()
        else:
            self.close_window()

    def open_detail(self):
        """打开最近一次检测的详情窗。"""
        if self._last_result is None:
            QtWidgets.QMessageBox.information(
                None, "报告质控", "还没有检测记录，请先做一次报告质控。")
            return
        try:
            from reportqc.ui.detail_dialog import ReportDetailDialog
        except Exception as exc:                     # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                None, "报告质控", "详情界面加载失败：%s" % exc)
            return
        dlg = ReportDetailDialog(self._last_result,
                                 record_id=self._last_record_id,
                                 db=self._get_db())
        dlg.show()
        dlg.raise_()
        self._dialogs.append(dlg)

    # -- 内部 ------------------------------------------------------------

    def _run_pipeline(self, img_bgr):
        self._busy = True
        if not self._ensure_window():
            # 小窗不可用（模型缺失等）时不能崩，弹错误提示并中止
            self._busy = False
            self._show_error("质控界面不可用，无法开始检测。")
            return
        self._win.show_running()

        worker = _Worker(self._pipeline_job, self)
        worker.configure(img_bgr)
        worker.done.connect(self._on_pipeline_done)
        worker.failed.connect(self._on_pipeline_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _pipeline_job(self, img_bgr):
        """后台线程执行体：引擎懒加载 + 全流程质控。

        每次运行都从数据库重读一遍用户规则，因此「设置里加词」和
        「详情窗标记误报」都是**即时生效**的，不需要重启程序：

        * ``ignore_words``  用户主动忽略的词
        * ``learned_words`` 标记误报后自动学到的词（下次不再报）
        * ``custom_typos``  用户自定义错别字（错词 → 正确写法）
        * ``extra_terms``   用户补充的医学术语白名单（英文缩写等）

        读失败一律降级为空，不能因为数据库不可用就整条流水线失败。
        """
        from reportqc.pacs.pipeline import run_pipeline
        from reportqc.ui.settings_dialog import get_show_low

        ignore_words, learned_words, extra_terms = [], [], []
        custom_typos = {}
        try:
            db = self._get_db()
            if db is not None:
                ignore_words = db.get_ignore_words()
                learned_words = db.get_learned_words()
                extra_terms = db.get_medical_terms()
                for row in (db.list_custom_typos() or []):
                    typo = (row.get("typo") or "").strip()
                    corr = (row.get("correction") or "").strip()
                    if typo and corr:
                        custom_typos[typo] = {
                            "correction": corr,
                            "severity": row.get("severity") or "medium",
                        }
        except Exception:                              # noqa: BLE001
            pass

        return run_pipeline(
            img_bgr,
            engine=self._get_engine(),
            checker=self._get_checker(),
            ignore_words=ignore_words,
            learned_words=learned_words,
            custom_typos=custom_typos,
            extra_terms=extra_terms,
            drop_low=not get_show_low(),
        )

    def _on_pipeline_done(self, result):
        self._last_result = result
        # 落库（主线程执行，数据库内部按线程管理连接，安全）
        record_id = None
        try:
            db = self._get_db()
            if db is not None:
                record_id = db.save_result(result, rect=self._last_rect)
        except Exception as exc:                      # noqa: BLE001
            print("[reportqc] 落库异常: %s" % exc)
        self._last_record_id = record_id

        if self._win is None:
            return
        self._win.show_result(result, record_id=record_id)
        self._win.show()
        self._win.raise_()

    def _on_pipeline_failed(self, message):
        self._show_error("质控失败：%s" % message)

    def _on_worker_finished(self):
        self._busy = False

    def _show_error(self, message):
        if self._win is not None:
            try:
                self._win.show_error(message)
                self._win.show()
                self._win.raise_()
                return
            except Exception:                       # noqa: BLE001
                pass
        QtWidgets.QMessageBox.warning(None, "报告质控", message)

    def _ensure_window(self):
        """确保小窗已创建。返回 False 表示不可用（调用方应中止）。"""
        if self._win is not None:
            return True
        if not reportqc_available():
            QtWidgets.QMessageBox.warning(
                None, "报告质控",
                "OCR 模型缺失，报告质控不可用。\n"
                "请确认 reportqc/models/ocr/ 下有三个 .onnx 模型文件。")
            return False
        try:
            from reportqc.ui.mini_window import MiniReportWindow
        except Exception as exc:                     # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                None, "报告质控", "质控界面加载失败：%s" % exc)
            return False
        self._win = MiniReportWindow()
        self._win.request_recheck.connect(self.refresh_check)    # 刷新按钮 / 双击窗口
        self._win.request_repick.connect(self.start_pick_and_check)  # 设置区域
        self._win.request_detail.connect(self.open_detail)
        self._win.request_history.connect(self.open_history)
        self._win.request_pdf.connect(self.open_pdf_export)        # PDF 导出
        return True


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_CONTROLLER = None
_CONTROLLER_LOCK = threading.Lock()


def get_controller():
    """控制器进程内单例。"""
    global _CONTROLLER
    with _CONTROLLER_LOCK:
        if _CONTROLLER is None:
            _CONTROLLER = ReportQCController()
        return _CONTROLLER


def apply_mini_visibility_if_running():
    """设置里改了「启动时自动打开悬浮小窗」后调用，让改动立即生效。

    只处理**已经建过控制器**的情况：本次启动还没打开过质控时不去创建它 ——
    为了一个开关去加载 OCR 引擎不值当，下次启动按新设置走就行。

    返回 True 表示真的调用了（控制器存在）；False 表示当前没有小窗要处理。
    """
    with _CONTROLLER_LOCK:
        ctrl = _CONTROLLER
    if ctrl is None:
        return False
    try:
        ctrl._apply_mini_visibility()
        return True
    except Exception as exc:                         # noqa: BLE001
        print("[reportqc] 应用小窗可见性失败: %s" % exc)
        return False


def startup_auto_show(delay_ms=1200):
    """程序启动后自动弹出报告质控悬浮小窗（主程序 ``main()`` 调用）。

    * **v1.23.1 起默认不弹**（``reportqc/show_mini`` 默认 0）：这项用得不多，
      启动就占一块屏幕反而碍事。要它自动出来就在「设置 → 报告质控」里勾上。
    * 模型不在位时静默跳过（不做 OCR 的发行包不该弹空窗）；
    * 延迟 ``delay_ms`` 再弹：主窗口是 ``showMaximized()`` 的，要等它
      真正画完，``mini_window._restore_pos`` 的屏幕可用区域校验才准确，
      否则小窗可能被定位到屏幕外；
    * **只显示就绪态，不自动识别** —— 此时主窗口很可能盖着 PACS 报告区，
      自动截图会截到本程序自己的界面，识别出错误的患者信息。
    * 全程 try/except：桥接出任何问题都只是「没有这个功能」，不能影响启动。

    返回 True 表示已排队弹出。
    """
    if not reportqc_available():
        return False
    # 能否自动弹只看这一项设置（开关在「设置 → 报告质控」）。
    try:
        from reportqc.ui.settings_dialog import get_show_mini
        if not get_show_mini():
            return False
    except Exception:                                # noqa: BLE001
        # 读不到设置就别弹。默认已经是不弹，这里也按 fail-closed 处理，
        # 否则一个异常就把"不打扰用户"的默认行为翻过来了。
        return False
    try:
        ctrl = get_controller()

        def _show():
            try:
                ctrl.show_window()
            except Exception:                        # noqa: BLE001
                import traceback
                traceback.print_exc()

        QtCore.QTimer.singleShot(int(delay_ms), _show)
        return True
    except Exception:                                # noqa: BLE001
        return False
