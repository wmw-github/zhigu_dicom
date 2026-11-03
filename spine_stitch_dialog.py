# -*- coding: utf-8 -*-
"""脊柱拼接 / Cobb 角测量面板（CT3D 内嵌）。

由主程序工具区「脊柱拼接」按钮打开。定位是「**拖图进来 → 拼接 → 测量 →
导出**」的轻量工作台，不是 spine 子项目那个全屏独立程序：

- **拖拽导入**：支持拖 DICOM（``.dcm`` / 无扩展名）与常见图片进来，也支持
  拖文件夹（递归收影像）。这正是 spine_app 缺的那一步 —— 那边只能
  「打开目录」被动扫全盘。
- **顺序可见可改**：拼接是**顺序敏感**的（SIFT 逐对算相对位移）。实测设备
  导出的 ``InstanceNumber`` 常在每个子组里从 1 重新计数（同一批片子出现
  1,2,1,2,1,2,3...），按文件名或 InstanceNumber 直接排都会错序。所以这里
  把顺序摆到列表里，并给出上移/下移。
- **算法不重写**：SIFT 位移、亮度匹配、羽化、Cobb 估线全部走
  ``spine_bridge`` 转出的 spine_app 原类，口径与原程序一致。
"""

import os

from PyQt5 import QtCore, QtWidgets, QtGui

import numpy as np

import loading_ui
import spine_bridge
import theme

# 主程序患者树拖放用的 MIME。与 main.py 的 SERIES_MIME 保持一致；
# 这里独立定义而非 import main，避免模块级循环导入
# （main 会 import 本模块的对话框，脊柱面板再 import main 就成环了）。
SERIES_MIME = "application/x-ct3d-series"
# 多选拖动（Ctrl/Shift 选多个序列一起拖）：值指向注册表里的 series **列表**
SERIES_BATCH_MIME = "application/x-ct3d-series-batch"


def _find_patient_tree():
    """找到主窗口的患者树（用于解析拖放 token → 序列字典）。

    不能 `import main`：main 在模块级就可能拉起对话框面板。这里改从
    `QApplication.allWidgets()` 里按类型找——Qt 的 allWidgets 会递归列出
    所有存活控件，注册在 QApplication 上，不需要 import 主程序模块。
    """
    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    for w in app.allWidgets():
        # 按类名判断，避免 import main 造成循环依赖
        if type(w).__name__ == "SeriesDragTreeWidget":
            return w
    return None


class _StitchWorker(QtCore.QThread):
    """后台跑「读图 → 亮度匹配 → 算位移」。SIFT 在大图上很慢，不能卡 UI。"""

    progress = QtCore.pyqtSignal(int, str)
    finished_ok = QtCore.pyqtSignal(object, object)   # images, positions
    failed = QtCore.pyqtSignal(str)

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self._paths = list(paths)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            stitcher = spine_bridge.get_stitcher()
            if stitcher is None:
                self.failed.emit("脊柱拼接算法模块不可用（spine/spine_app.py 缺失或导入失败）")
                return

            images = []
            total = max(1, len(self._paths))
            for i, path in enumerate(self._paths):
                if self._cancel:
                    return
                self.progress.emit(int(i * 40 / total), "正在读取影像 %d/%d" % (i + 1, total))
                arr = spine_bridge.load_image_gray(path)
                if arr is None:
                    continue
                images.append(arr)
            if self._cancel:
                return

            if len(images) < 2:
                self.failed.emit("至少需要 2 张可读影像才能拼接（当前 %d 张）" % len(images))
                return

            self.progress.emit(45, "正在统一亮度对比度...")
            images = stitcher.match_brightness_contrast(images)
            if self._cancel:
                return

            self.progress.emit(55, "正在计算拼接位置...")
            positions = stitcher.calculate_offsets(images)
            if self._cancel:
                return

            self.progress.emit(100, "完成")
            self.finished_ok.emit(images, positions)
        except Exception as exc:                            # noqa: BLE001
            self.failed.emit("拼接失败：%s" % exc)


def _series_label(series):
    """序列的简短显示名（用于提示文案）。

    优先 SeriesDescription，其次患者名，最后退回目录名末段。
    目录名常常是「01」这种流水号，对用户毫无意义 —— 所以先试着从序列里
    的文件读一次 SeriesDescription（只 stop_before_pixels，很便宜），
    读不到再退。目录名一律截断（可能带很长的随机串）。
    """
    if not isinstance(series, dict):
        return "（未知序列）"

    for key in ("desc", "series_desc", "description", "series_title"):
        val = series.get(key)
        if val:
            return str(val)[:40]

    # 目录名只是流水号（「01」），对用户没意义 → 试着读一次 DICOM 里的描述
    folder = str(series.get("folder") or "")
    if folder and os.path.isdir(folder):
        desc = _read_series_desc(folder)
        if desc:
            return desc

    name = series.get("patient_name") or ""
    tail = os.path.basename(folder.rstrip("\\/"))[:24] if folder else ""
    if name and tail:
        return "%s · %s" % (name, tail)
    return tail or "（未命名序列）"


def _read_series_desc(folder):
    """读一个序列目录的描述文字（SeriesDescription → StudyDescription）。

    两个都要试：国产 DR 设备**经常只填 StudyDescription**、
    SeriesDescription 留空或写成同一段乱码；只读前者会拿到空串。
    每段都要过 `_fix_gbk_text` —— 设备用 GBK 写中文却不写
    SpecificCharacterSet，pydicom 会按 Latin-1 解出乱码。
    """
    try:
        import glob                                  # noqa: PLC0415
        import pydicom                              # noqa: PLC0415
        try:
            from ct3d_utils import _fix_gbk_text     # noqa: PLC0415
        except Exception:                            # noqa: BLE001
            def _fix_gbk_text(s):
                return str(s or "")

        for fn in sorted(glob.glob(os.path.join(folder, "*.dcm")))[:1]:
            ds = pydicom.dcmread(fn, stop_before_pixels=True)
            for key in ("SeriesDescription", "StudyDescription",
                        "BodyPartExamined"):
                raw = str(getattr(ds, key, "") or "").strip()
                if not raw:
                    continue
                fixed = _fix_gbk_text(raw).strip()
                # 含替换字符说明两边都没解对，跳过看下一个候选
                if not fixed or "�" in fixed:
                    continue
                return fixed[:40]
            break
    except Exception:                                # noqa: BLE001
        pass
    return ""


class SpineStitchDialog(QtWidgets.QDialog):
    """脊柱拼接工作台。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("脊柱拼接 / Cobb 角测量")
        # 允许最小化 / 最大化：原窗口固定且偏小，拼接长图时看不清。
        # QDialog 默认只有「关闭」和「帮助?」按钮，必须显式加这两个 hint
        # 标题栏才会出现最小 / 最大化按钮。
        self.setWindowFlags(
            self.windowFlags()
            | QtCore.Qt.WindowMinimizeButtonHint
            | QtCore.Qt.WindowMaximizeButtonHint)
        # 默认给更大的初始尺寸，最大化前也能看清大部分拼接结果。
        self.resize(1366, 860)
        self.setAcceptDrops(True)

        self._paths = []            # 有序路径列表（拼接顺序）
        self._images = []           # 最近一次拼接的灰度图
        self._positions = []
        # 显示端逐片图元（无缝模式下每张片独立可拖动）+ 归一化 BGR 缓存
        self._slice_items = []
        self._slice_imgs = []
        self._reblend_timer = None   # 拖动防抖重算遮罩
        self._applying_blend = False  # 程序化 setPos 期间屏蔽 on_moved 回调，防递归
        self._worker = None
        self._busy = None
        self._worker_failed = False
        self._thumbs = {}                # {路径: QPixmap} 缩略图缓存
        self._thumbs_enabled = True
        self._records_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "stitch_records.json")

        self._build_ui()
        self._sync_buttons()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ---- 左：影像列表 + 顺序调整 ----
        left = QtWidgets.QWidget()
        # 要放得下缩略图（150px 网格 = 图标 56 + 文字），300 只能挤两列且
        # 文件名被截断看不清，给到 340。
        left.setFixedWidth(340)
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)

        lv.addWidget(QtWidgets.QLabel("待拼接影像（自上而下，可 Ctrl/Shift 多选）"))

        self._list = QtWidgets.QListWidget()
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        # 注意：不要写成 `lambda _: ...`。`itemSelectionChanged` 是**无参信号**，
        # PyQt 会以 0 个实参调用可调用对象 → `TypeError: <lambda>() missing 1
        # required positional argument: '_'`（用户点「打开文件」就崩在这里）。
        # 同理 `stateChanged`/`valueChanged` 在某些 Qt 版本上也会退化成 0 参调用。
        # 一律用具名方法（签名带默认值，两种情况都接得住）。
        self._list.currentRowChanged.connect(self._on_list_selection_changed)
        self._list.itemSelectionChanged.connect(self._on_list_selection_changed)
        # 滚动到新行时补缩略图（懒加载的关键接线）
        self._list.verticalScrollBar().valueChanged.connect(
            self._on_scroll_for_thumbs)
        # 有缩略图就得给它腾地方：默认 QListWidget 行高按字算，图标会被压扁。
        # 用 IconMode 之外的 ListMode + 自定义 itemSize，行高由缩略图决定。
        self._list.setIconSize(QtCore.QSize(56, 56))
        self._list.setGridSize(QtCore.QSize(150, 68))
        lv.addWidget(self._list, 1)

        order_row = QtWidgets.QHBoxLayout()
        self._up_btn = QtWidgets.QPushButton("上移")
        self._down_btn = QtWidgets.QPushButton("下移")
        self._up_btn.clicked.connect(lambda: self._move_selected(-1))
        self._down_btn.clicked.connect(lambda: self._move_selected(1))
        order_row.addWidget(self._up_btn)
        order_row.addWidget(self._down_btn)
        lv.addLayout(order_row)

        add_row = QtWidgets.QHBoxLayout()
        self._add_files_btn = QtWidgets.QPushButton("添加文件")
        self._add_dir_btn = QtWidgets.QPushButton("添加文件夹")
        self._add_files_btn.clicked.connect(self._on_add_files)
        self._add_dir_btn.clicked.connect(self._on_add_dir)
        add_row.addWidget(self._add_files_btn)
        add_row.addWidget(self._add_dir_btn)
        lv.addLayout(add_row)

        act_row = QtWidgets.QHBoxLayout()
        self._sort_btn = QtWidgets.QPushButton("按文件名排序")
        self._remove_btn = QtWidgets.QPushButton("移除选中")
        self._clear_btn = QtWidgets.QPushButton("清空")
        self._sort_btn.clicked.connect(self._on_sort_by_name)
        self._remove_btn.clicked.connect(self._on_remove_selected)
        self._clear_btn.clicked.connect(self._on_clear)
        act_row.addWidget(self._sort_btn)
        act_row.addWidget(self._remove_btn)
        act_row.addWidget(self._clear_btn)
        lv.addLayout(act_row)

        self._stitch_btn = QtWidgets.QPushButton("开始拼接")
        theme.role(self._stitch_btn, "primary")
        self._stitch_btn.setToolTip(
            "拼接选中项；若无选中则拼接全部。\n"
            "Ctrl/Shift 多选可只拼一部分（例如先拼正位、再拼侧位）。")
        self._stitch_btn.clicked.connect(self._on_stitch)
        lv.addWidget(self._stitch_btn)

        self._status = QtWidgets.QLabel("把影像文件或文件夹拖到这里，或点上方按钮添加")
        self._status.setWordWrap(True)
        theme.role(self._status, "caption")
        lv.addWidget(self._status)

        root.addWidget(left, 0)

        # ---- 右：画布 + 工具条 ----
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(6)

        bar = QtWidgets.QHBoxLayout()
        self._feather_cb = QtWidgets.QCheckBox("单层融合（无缝）")
        self._feather_cb.setChecked(True)
        self._feather_cb.setToolTip(
            "勾选：重叠区单层交叉淡化，拼接处无明显接缝（推荐，用于查看/导出）。\n"
            "取消：恢复半透明叠层，方便拖动各片重新对齐。")
        self._feather_cb.stateChanged.connect(self._on_feather_changed)
        bar.addWidget(self._feather_cb)
        bar.addWidget(QtWidgets.QLabel("淡化宽度"))
        self._feather_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._feather_slider.setRange(20, 400)
        self._feather_slider.setValue(150)
        self._feather_slider.setMaximumWidth(150)
        self._feather_slider.valueChanged.connect(self._on_feather_changed)
        bar.addWidget(self._feather_slider)

        bar.addWidget(QtWidgets.QLabel("不透明度"))
        self._opacity_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._opacity_slider.setRange(20, 100)
        self._opacity_slider.setValue(85)
        self._opacity_slider.setMaximumWidth(110)
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        bar.addWidget(self._opacity_slider)
        bar.addStretch(1)

        self._cobb_auto_btn = QtWidgets.QPushButton("自动测 Cobb 角")
        self._cobb_manual_btn = QtWidgets.QPushButton("手动测量")
        self._cobb_auto_btn.clicked.connect(self._on_auto_cobb)
        self._cobb_manual_btn.clicked.connect(self._on_manual_cobb)
        bar.addWidget(self._cobb_auto_btn)
        bar.addWidget(self._cobb_manual_btn)
        right.addLayout(bar)

        self._angle_label = QtWidgets.QLabel("Cobb 角: -")
        theme.role(self._angle_label, "accent")
        right.addWidget(self._angle_label)

        self._scene = QtWidgets.QGraphicsScene(self)
        view_cls = spine_bridge.get_graphics_view_class()
        if view_cls is not None:
            self._view = view_cls(self._scene, self)
        else:                                    # 理论上不可达，兜底不崩
            self._view = QtWidgets.QGraphicsView(self._scene, self)
        self._view.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self._view.setRenderHint(QtGui.QPainter.Antialiasing)
        self._view.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        # 画布恒为深色（图像衬底，与皮肤无关）
        self._view.setStyleSheet("background:%s;" % theme.VIEWER["bg"])
        right.addWidget(self._view, 1)

        save_row = QtWidgets.QHBoxLayout()
        self._save_png_btn = QtWidgets.QPushButton("保存拼接图像")
        self._save_dcm_btn = QtWidgets.QPushButton("保存为 DICOM")
        self._save_rec_btn = QtWidgets.QPushButton("保存测量记录")
        self._fit_btn = QtWidgets.QPushButton("适应窗口")
        self._reset_view_btn = QtWidgets.QPushButton("清空画布")
        self._save_png_btn.clicked.connect(self._on_save_png)
        self._save_dcm_btn.clicked.connect(self._on_save_dicom)
        self._save_rec_btn.clicked.connect(self._on_save_record)
        self._fit_btn.clicked.connect(self._on_fit)
        self._reset_view_btn.clicked.connect(self._on_clear_scene)
        for b in (self._save_png_btn, self._save_dcm_btn, self._save_rec_btn,
                  self._fit_btn, self._reset_view_btn):
            save_row.addWidget(b)
        save_row.addStretch(1)
        right.addLayout(save_row)

        root.addLayout(right, 1)

        self._cobb_tool = None
        cobb_cls = spine_bridge.get_cobb_tool_class()
        if cobb_cls is not None:
            self._cobb_tool = cobb_cls(self._scene)
        self._view.viewport().installEventFilter(self)

    # ---- 信号槽（一律具名方法，不用 lambda _: 见 _build_ui 里的说明）----
    def _on_list_selection_changed(self, *_args):
        """列表选中/当前行变化（两个信号的参数个数不同，用 *args 兼容）"""
        self._sync_buttons()

    def _on_scroll_for_thumbs(self, value=0):
        """列表滚动 → 从当前滚动位置往后补缩略图"""
        self._ensure_visible_thumbs(offset=int(value or 0))

    def _on_feather_changed(self, *_args):
        """羽化开关/高度变化 → 整场重建图元"""
        self._refresh_scene()

    def _on_opacity_changed(self, *_args):
        """不透明度变化 → 改已建图元"""
        self._apply_opacity()

    def _sync_buttons(self):
        has = bool(self._paths)
        self._stitch_btn.setEnabled(len(self._paths) >= 2)
        has_sel = bool(self._list.selectedItems())
        self._remove_btn.setEnabled(has_sel)
        self._up_btn.setEnabled(has_sel)
        self._down_btn.setEnabled(has_sel)
        self._sort_btn.setEnabled(has)
        self._clear_btn.setEnabled(has)
        stitched = bool(self._images)
        for b in (self._cobb_auto_btn, self._cobb_manual_btn,
                  self._save_png_btn, self._save_dcm_btn, self._save_rec_btn):
            b.setEnabled(stitched)

    # ------------------------------------------------------------------
    # 拖放
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_images(paths):
        """从拖进来的路径里收集影像文件（文件夹递归）。"""
        import glob                              # noqa: PLC0415

        found = []
        for p in paths:
            if os.path.isdir(p):
                for dirpath, _dirs, files in os.walk(p):
                    for fn in files:
                        full = os.path.join(dirpath, fn)
                        ext = os.path.splitext(fn)[1].lower()
                        if ext == ".dcm" or ext in spine_bridge.IMAGE_EXTS:
                            found.append(full)
                        elif ext == "":            # 无扩展名，按 DICOM 试读
                            if spine_bridge.load_image_gray(full) is not None:
                                found.append(full)
            else:
                found.append(p)
        return found

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or self._series_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls() or self._series_from_mime(event.mimeData()):
            event.acceptProposedAction()

    @staticmethod
    def _series_from_mime(mime):
        """从拖放载荷里取主程序患者列表的序列信息，**统一返回列表**。

        主程序患者树拖序列时用 token 注册表（载荷走注册表而不是把整个字典
        塞进 MIME，见 main.py `SeriesDragTreeWidget.startDrag`）。两种载荷：

        - 单选 → `SERIES_MIME`，注册表里是**一个** series 字典
        - 多选（Ctrl/Shift）→ `SERIES_BATCH_MIME`，注册表里是**列表**

        无论哪种都返回 list（元素为 series dict），调用方不用区分。
        取不到返回空列表，交给普通的文件/文件夹通路。
        """
        if mime is None:
            return []
        try:
            is_batch = mime.hasFormat(SERIES_BATCH_MIME)
            if not is_batch and not mime.hasFormat(SERIES_MIME):
                return []
            fmt = SERIES_BATCH_MIME if is_batch else SERIES_MIME
            raw = bytes(mime.data(fmt)).decode("utf-8", "ignore").strip()
            if not raw:
                return []
            tree = _find_patient_tree()
            if tree is None:
                return []
            payload = tree.payload(raw)
            if payload is None:
                return []
            if isinstance(payload, list):
                return [s for s in payload if isinstance(s, dict)]
            if isinstance(payload, dict):
                # 批量 MIME 里理论上也是 list；兜底防止注册表被塞了单值
                return [payload]
            return []
        except Exception:                             # noqa: BLE001
            return []

    def dropEvent(self, event):
        try:
            # ① 主程序患者列表拖过来的序列（单选或多选）
            series_list = self._series_from_mime(event.mimeData())
            if series_list:
                if self._add_series_list(series_list):
                    event.acceptProposedAction()
                    return

            # ② 资源管理器拖过来的文件 / 文件夹
            urls = event.mimeData().urls()
            paths = [u.toLocalFile() for u in urls if u.toLocalFile()]
            if not paths:
                return
            imgs = self._collect_images(paths)
            if not imgs:
                loading_ui.show_toast(self, "拖入的内容里没找到可读的影像文件", kind="warning")
                return
            added = self._append_paths(imgs)
            self._status.setText(
                "已添加 %d 张（本次 %d 张），当前共 %d 张" % (len(imgs), added, len(self._paths)))
            event.acceptProposedAction()
        except Exception as exc:                # noqa: BLE001
            loading_ui.show_toast(self, "导入失败：%s" % exc, kind="error")

    def _add_series_list(self, series_list):
        """把若干个序列一起加进拼接列表（Ctrl 多选后拖过来走这条）。

        返回是否成功接收。

        **「至少 2 张」是对合计而言，不是对每个序列**：DR 设备常把正位、
        侧位、斜位各存成**独立序列，每列只有 1 张**（本项目铁律第 30 条
        同理）。用户拖两个单张序列过来正是最典型的全脊柱拼接场景，
        逐序列要求 2 张会把它们全拒掉 —— 这是实测踩到的坑。
        """
        if not series_list:
            return False
        if len(series_list) == 1:
            return self._add_series(series_list[0])

        all_paths, rejected = [], []
        for ser in series_list:
            paths, note = spine_bridge.series_to_paths(ser)
            if not paths:
                rejected.append("%s —— %s" % (_series_label(ser),
                                              note or "没有可用影像"))
                continue
            # 单张序列照收：数量门槛留到合并之后统一判
            all_paths.extend(paths)

        if not all_paths:
            loading_ui.show_toast(
                self,
                "选中的 %d 个序列都不能拼接：\n%s\n"
                "脊柱拼接只接受 X 线正/侧位片（CR/DX/DR 等）。"
                % (len(series_list), "\n".join(rejected)),
                kind="warning")
            return False

        if len(all_paths) < 2:
            loading_ui.show_toast(
                self,
                "选中的序列里只找到 1 张影像（%s），拼接至少需要 2 张。\n"
                "请再选一张片子（正位 + 侧位一起拼最常见）。"
                % _series_label(series_list[0]),
                kind="warning")
            return False

        added = self._append_paths(all_paths)
        head = "已从患者列表加入 %d 个序列共 %d 张（新增 %d 张），当前共 %d 张" % (
            len(series_list) - len(rejected), len(all_paths), added, len(self._paths))
        if rejected:
            detail = "%s\n已跳过 %d 个序列：\n%s" % (
                head, len(rejected), "\n".join(rejected))
            loading_ui.show_toast(self, detail, kind="warning")
            self._status.setText(head)
        else:
            self._status.setText(head)
        return True

    def _add_series(self, series):
        """把患者列表的一个序列加进拼接列表。返回是否成功接收。"""
        paths, note = spine_bridge.series_to_paths(series)
        if not paths:
            loading_ui.show_toast(
                self,
                "无法用于拼接：%s\n请改用 X 线正/侧位片所在的序列。"
                % (note or "该序列没有可用影像"),
                kind="warning")
            return False
        if len(paths) < 2:
            # 单张序列（DR 正位/侧位常各存一个序列）是有意义的，只是还不能拼。
            # 明确告诉用户「这张已收下，再选一张就能拼」，而不是笼统报错。
            added = self._append_paths(paths)
            self._status.setText(
                "已加入「%s」1 张，再拖入至少 1 张即可拼接"
                % _series_label(series))
            loading_ui.show_toast(
                self,
                "「%s」只有 1 张影像，已为你加入列表。\n"
                "再选一张片子拖进来即可拼接（正位 + 侧位是最常见组合）。"
                % _series_label(series),
                kind="info")
            return bool(added)

        added = self._append_paths(paths)
        self._status.setText(
            "已从患者列表加入「%s」%d 张（新增 %d 张），当前共 %d 张"
            % (_series_label(series), len(paths), added, len(self._paths)))
        return True

    def _append_paths(self, paths):
        """追加路径并去重，返回实际新增数量。"""
        existing = {os.path.normcase(os.path.abspath(p)) for p in self._paths}
        added = 0
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if key in existing:
                continue
            existing.add(key)
            self._paths.append(p)
            added += 1
        # 新拖进来的排在后面，并各自按文件名自然排序
        tail = self._paths[-added:] if added else []
        self._paths = self._paths[:-added] + spine_bridge.sort_paths(tail) if added else self._paths
        self._refresh_list()
        return added

    def _refresh_list(self):
        """重建列表（带缩略图）。

        缩略图必须**懒加载**：DICOM 单张解码要几十 ms，一次导入几十张时
        同步全解会卡住界面。所以只给「当前可见行」挂图标，滚动时补，
        并按路径缓存 pixmap（同一文件重复进出列表不重复解码）。
        """
        self._list.blockSignals(True)
        self._list.clear()
        for i, p in enumerate(self._paths):
            item = QtWidgets.QListWidgetItem("%d. %s" % (i + 1, os.path.basename(p)))
            item.setData(QtCore.Qt.UserRole, p)
            cached = self._thumbs.get(p)
            if cached is not None:
                item.setIcon(QtGui.QIcon(cached))
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._sync_buttons()
        self._ensure_visible_thumbs()

    def _ensure_visible_thumbs(self, offset=0, max_rows=24):
        """从 ``offset`` 行起补缩略图（不在事件循环里做重活）。

        这里刻意**不用** `firstVisibleRow/lastVisibleRow`：窗口还没 show()
        时（例如作为子窗口刚构造、或 offscreen 验收）它们一律返回 -1，
        缩略图就永远挂不上。所以退化成「按行号区间」——列表行高固定，
        滚动量/行高≈可见行数，从该处往后补 N 行即可覆盖视口。
        """
        if not self._thumbs_enabled:
            return
        start = max(0, int(offset))
        end = min(self._list.count(), start + max_rows)
        for row in range(start, end):
            item = self._list.item(row)
            if item is None or not item.icon().isNull():
                continue
            path = item.data(QtCore.Qt.UserRole)
            if not path or path in self._thumbs:
                continue
            pm = self._make_thumb(path)
            if pm is not None and not pm.isNull():
                self._thumbs[path] = pm
                item.setIcon(QtGui.QIcon(pm))

    @staticmethod
    def _make_thumb(path, size=64):
        """生成单张缩略图；失败返回 None（不抛）"""
        try:
            arr = spine_bridge.load_image_gray(path)
            if arr is None:
                return None
            h, w = arr.shape[:2]
            img = QtGui.QImage(arr.data, w, h, w, QtGui.QImage.Format_Grayscale8)
            # QImage 不复制 numpy 缓冲 → 必须 copy 一次，否则 arr 被回收后
            # 画出来是花屏（这类悬垂引用不会报错，只会显示错）。
            return QtGui.QPixmap.fromImage(img.copy()).scaled(
                size, size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        except Exception:                             # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 列表操作
    # ------------------------------------------------------------------
    def _selected_rows(self):
        """选中项的行号列表（升序）。

        注意：``QListWidgetItem`` **没有** ``row()`` 方法（那是 QTreeWidgetItem
        才有的），行号只能从 selectionModel 的索引取。
        """
        rows = []
        for idx in self._list.selectionModel().selectedRows():
            r = idx.row()
            if r >= 0:
                rows.append(r)
        return sorted(set(rows))

    def _move_selected(self, delta):
        rows = self._selected_rows()
        if not rows:
            return
        if delta < 0 and rows[0] == 0:
            return
        if delta > 0 and rows[-1] == len(self._paths) - 1:
            return
        new_paths = list(self._paths)
        for r in rows:
            j = r + delta
            new_paths[r], new_paths[j] = new_paths[j], new_paths[r]
        self._paths = new_paths
        self._refresh_list()
        # 恢复选中：必须延后到下一轮事件循环。_refresh_list 刚 clear() 过，
        # 同帧再 setCurrentRow 会与尚未收尾的布局/选择状态打架 —— offscreen
        # 下直接段错误（exit 127，无 traceback）。
        for r in rows:
            QtCore.QTimer.singleShot(0, lambda j=r + delta: self._select_row(j))
        self._status.setText("顺序已调整（拼接自上而下进行）")

    def _select_row(self, row):
        """选中指定行（越界安全）"""
        if 0 <= row < self._list.count():
            try:
                self._list.setCurrentRow(row)
            except RuntimeError:            # 列表已销毁（窗口关闭中）
                pass

    def _on_remove_selected(self):
        rows = set(self._selected_rows())
        if not rows:
            return
        self._paths = [p for i, p in enumerate(self._paths) if i not in rows]
        self._refresh_list()
        self._status.setText("已移除 %d 张" % len(rows))

    def _on_clear(self):
        self._paths = []
        self._on_clear_scene()
        self._refresh_list()
        self._status.setText("已清空列表")

    def _on_sort_by_name(self):
        self._paths = spine_bridge.sort_paths(self._paths)
        self._refresh_list()
        self._status.setText("已按文件名自然排序")

    def _on_add_files(self):
        exts = " ".join("*" + e for e in (".dcm",) + spine_bridge.IMAGE_EXTS)
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "选择影像文件", "", "影像文件 (%s);;所有文件 (*)" % exts)
        if not paths:
            return
        added = self._append_paths(list(paths))
        self._status.setText("已添加 %d 张，当前共 %d 张" % (added, len(self._paths)))

    def _on_add_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择影像文件夹")
        if not d:
            return
        imgs = self._collect_images([d])
        if not imgs:
            QtWidgets.QMessageBox.warning(self, "提示", "该文件夹里没找到影像文件")
            return
        added = self._append_paths(imgs)
        self._status.setText("已添加 %d 张，当前共 %d 张" % (added, len(self._paths)))

    # ------------------------------------------------------------------
    # 拼接
    # ------------------------------------------------------------------
    def _stitch_targets(self):
        """本次要拼接的路径列表。

        **有选中就只拼选中的，没选中才拼全部**（Ctrl/Shift 多选）。
        列表已是 ExtendedSelection，用户可以只挑正位那几张先拼一张图，
        拼完再拼侧位 —— 一次全拼往往不是想要的。
        """
        rows = self._selected_rows()
        if not rows:
            return list(self._paths), False
        picked = [self._paths[r] for r in rows if 0 <= r < len(self._paths)]
        return picked, True

    def _on_stitch(self):
        targets, partial = self._stitch_targets()
        if len(targets) < 2:
            if partial and targets:
                QtWidgets.QMessageBox.warning(
                    self, "提示",
                    "只选中了 1 张影像，拼接至少需要 2 张。\n"
                    "请 Ctrl/Shift 多选，或先取消选择以拼接全部。")
            else:
                QtWidgets.QMessageBox.warning(self, "提示", "至少需要 2 张影像")
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._worker_failed = False

        busy = loading_ui.TechBusyDialog("正在拼接，请稍候...", None, self)
        busy.setWindowTitle("脊柱拼接")
        busy.setWindowModality(QtCore.Qt.WindowModal)
        busy.setRange(0, 100)
        busy.setValue(0)
        busy.show()
        # 持引用给 _on_progress 用：findChild 在 dialog.deleteLater 后可能落空
        self._busy = busy

        self._worker = _StitchWorker(targets, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_stitch_done)
        self._worker.failed.connect(self._on_stitch_failed)
        self._worker.finished.connect(self._on_worker_finished)
        # 所有出口都必须摘遮罩（loading_ui 铁律），用 finally 兜底
        try:
            self._worker.start()
            while self._worker.isRunning():
                QtWidgets.QApplication.processEvents()
                self._worker.wait(30)
            # 线程刚结束的那一刻，finished_ok/failed 还排在事件队列里没派发
            # —— 上面循环随 isRunning() 变 False 立刻退出，最后一次
            # processEvents 赶不上，结果就是「算完了但界面没反应」。
            # 这里再泵几轮，把收尾信号真正送到槽里。
            for _ in range(5):
                QtWidgets.QApplication.processEvents()
                QtCore.QThread.msleep(10)
        finally:
            self._busy = None
            busy.close()
            busy.deleteLater()

    def _on_progress(self, value, text):
        dlg = self._busy
        if dlg is None:
            return
        try:
            dlg.setValue(value)
            # TechBusyDialog 的文案接口是 setLabelText（QProgressDialog 兼容面），
            # 没有 setText。槽函数里抛异常 = 静默闪退，故整体兜住。
            if text and hasattr(dlg, "setLabelText"):
                dlg.setLabelText(text)
        except RuntimeError:                 # 对话框已销毁
            pass

    def _on_worker_finished(self):
        """线程收尾：结果既没成功也没失败（例如被取消）时给个交代。

        绝不能让用户对着一个不动的进度条猜 —— 槽函数静默失败等于闪退。
        """
        if not self._images and not self._worker_failed:
            self._status.setText("拼接已取消")

    def _on_stitch_done(self, images, positions):
        self._images = list(images)
        self._positions = list(positions)
        # 新拼接：旧的 Cobb 测量线已失效，先清空（_refresh_scene 只换影像图元）
        if self._cobb_tool is not None:
            self._cobb_tool.lines = []
            self._cobb_tool.text_item = None
        self._refresh_scene()
        self._on_fit()
        self._sync_buttons()
        # 明说这次用的是哪些：全部拼 vs 只拼选中的，用户要能对上数
        scope = ("全部 %d 张" % len(self._paths)
                 if len(self._images) == len(self._paths)
                 else "选中的 %d 张（共 %d 张）" % (len(self._images), len(self._paths)))
        self._status.setText(
            "拼接完成：%s。鼠标右键拖动可调亮度；Ctrl/Shift 可选部分重拼。"
            % scope)

    def _on_stitch_failed(self, msg):
        self._worker_failed = True
        QtWidgets.QMessageBox.warning(self, "拼接失败", msg)
        self._status.setText("拼接失败")

    def _refresh_scene(self, force_seamless=None):
        if not self._images:
            return
        stitcher = spine_bridge.get_stitcher()
        item_cls = spine_bridge.get_image_item_class()
        helper = spine_bridge.get_dicom_helper()
        if stitcher is None or item_cls is None or helper is None:
            return

        # 只移除「影像图元」，保留 Cobb 测量线/文字（避免刷新时丢掉测量）
        for it in list(self._scene.items()):
            if getattr(it, "_is_stitch_image", False):
                self._scene.removeItem(it)
        self._slice_items = []
        self._slice_imgs = []

        # 归一化 BGR uint8 缓存（遮罩与重混都用）；灰度片也能正确加 alpha
        import cv2                                 # noqa: PLC0415
        for im in self._images:
            bgr = im
            if bgr.ndim == 2:
                bgr = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
            elif bgr.shape[2] == 4:
                bgr = cv2.cvtColor(bgr, cv2.COLOR_BGRA2BGR)
            if bgr.dtype != np.uint8:
                bgr = np.clip(bgr, 0, 255).astype(np.uint8)
            self._slice_imgs.append(bgr)

        seamless = self._feather_cb.isChecked() if force_seamless is None else force_seamless
        feather_h = self._feather_slider.value()
        opacity = self._opacity_slider.value() / 100.0

        self._applying_blend = True
        try:
            if seamless:
                # ① 重叠区单层交叉淡化：但**仍保留每张片为独立可拖动图元**，
                #    用逐片余弦权重遮罩呈现无缝外观。拖动任一张片后按新位置
                #    重算遮罩即可，导出/显示始终单层、无重影。
                res = stitcher.blend_single_layer(
                    self._slice_imgs, self._positions, feather_h)
                if res is not None:
                    _out, (cx0, cy0), masks, offsets = res
                    for i, bgr in enumerate(self._slice_imgs):
                        if i >= len(masks):
                            break
                        bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
                        bgra[..., 3] = masks[i]
                        item = item_cls(helper.numpy_to_qpixmap(bgra))
                        item._is_stitch_image = True
                        item._slice_index = i
                        item.setOpacity(1.0)
                        item.on_moved = self._on_slice_moved
                        if i < len(offsets):
                            item.setPos(cx0 + offsets[i][0], cy0 + offsets[i][1])
                        elif i < len(self._positions):
                            item.setPos(*self._positions[i])
                        self._scene.addItem(item)
                        self._slice_items.append(item)
            else:
                # ② 半透明叠层（用于拖动对齐）；羽化已改余弦过渡
                for i, img in enumerate(self._images):
                    disp = stitcher.apply_feathering(
                        img, top=(i > 0), bottom=(i < len(self._images) - 1),
                        feather_height=feather_h)
                    item = item_cls(helper.numpy_to_qpixmap(disp))
                    item._is_stitch_image = True
                    item._slice_index = i
                    item.setOpacity(opacity)
                    item.on_moved = self._on_slice_moved
                    if i < len(self._positions):
                        item.setPos(*self._positions[i])
                    self._scene.addItem(item)
                    self._slice_items.append(item)
        finally:
            self._applying_blend = False

        view = self._view
        if hasattr(view, "update_brightness_overlay"):
            view.update_brightness_overlay()

    def _apply_opacity(self):
        # 单层融合模式下整张是合成图，不透明度调节无意义，跳过
        if self._feather_cb.isChecked():
            return
        opacity = self._opacity_slider.value() / 100.0
        item_cls = spine_bridge.get_image_item_class()
        if item_cls is None:
            return
        for it in self._scene.items():
            if isinstance(it, item_cls):
                it.setOpacity(opacity)

    # ---- 无缝模式下逐片拖动 → 实时重算遮罩 ----
    def _on_slice_moved(self, item, new_pos):
        """某张片被拖动后：同步 positions，并防抖重算逐片遮罩（接缝跟随）。"""
        if self._applying_blend:
            return
        idx = getattr(item, "_slice_index", None)
        if idx is None or idx >= len(self._positions):
            return
        self._positions[idx] = (float(new_pos.x()), float(new_pos.y()))
        self._schedule_reblend()

    def _schedule_reblend(self):
        if self._reblend_timer is None:
            self._reblend_timer = QtCore.QTimer(self)
            self._reblend_timer.setSingleShot(True)
            self._reblend_timer.timeout.connect(self._reblend)
        self._reblend_timer.start(40)

    def _reblend(self):
        """无缝模式下按最新 positions 重算逐片余弦遮罩并更新图元（导出仍无缝）。"""
        if not self._images or not self._slice_items or not self._slice_imgs:
            return
        if not self._feather_cb.isChecked():
            return
        stitcher = spine_bridge.get_stitcher()
        helper = spine_bridge.get_dicom_helper()
        item_cls = spine_bridge.get_image_item_class()
        if stitcher is None or helper is None or item_cls is None:
            return
        import cv2                                 # noqa: PLC0415
        res = stitcher.blend_single_layer(
            self._slice_imgs, self._positions, self._feather_slider.value())
        if res is None:
            return
        _out, (cx0, cy0), masks, offsets = res
        self._applying_blend = True
        try:
            for i, item in enumerate(self._slice_items):
                if i >= len(masks):
                    break
                bgra = cv2.cvtColor(self._slice_imgs[i], cv2.COLOR_BGR2BGRA)
                bgra[..., 3] = masks[i]
                item.setPixmap(helper.numpy_to_qpixmap(bgra))
                if i < len(offsets):
                    item.setPos(cx0 + offsets[i][0], cy0 + offsets[i][1])
                elif i < len(self._positions):
                    item.setPos(*self._positions[i])
        finally:
            self._applying_blend = False
        view = self._view
        if hasattr(view, "update_brightness_overlay"):
            view.update_brightness_overlay()

    def _on_fit(self):
        rect = self._scene.itemsBoundingRect()
        if rect.isEmpty():
            return
        self._scene.setSceneRect(rect)
        self._view.fitInView(rect, QtCore.Qt.KeepAspectRatio)

    def _on_clear_scene(self):
        self._scene.clear()
        self._images = []
        self._positions = []
        self._angle_label.setText("Cobb 角: -")
        if self._cobb_tool is not None:
            self._cobb_tool.lines = []
            self._cobb_tool.text_item = None
        if hasattr(self._view, "reset_brightness"):
            self._view.reset_brightness()
        self._sync_buttons()

    # ------------------------------------------------------------------
    # Cobb 角
    # ------------------------------------------------------------------
    def _build_composite_array(self):
        """直接合成无缝拼接图（BGR ndarray），用于导出，不依赖当前显示模式。"""
        if not self._images or not self._positions:
            return None
        stitcher = spine_bridge.get_stitcher()
        if stitcher is None:
            return None
        res = stitcher.blend_single_layer(
            self._images, self._positions, self._feather_slider.value())
        if res is None:
            return None
        return res[0]  # (out_bgr, (cx0,cy0), masks, offsets)

    def _render_scene_to_array(self):
        """把当前画布渲染成 BGR ndarray（Cobb 算法输入）。"""
        import cv2                                     # noqa: PLC0415

        self._scene.clearSelection()
        rect = self._scene.itemsBoundingRect()
        if rect.isEmpty():
            return None
        image = QtGui.QImage(rect.size().toSize(), QtGui.QImage.Format_RGB32)
        image.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(image)
        self._scene.render(painter, target=QtCore.QRectF(image.rect()), source=rect)
        painter.end()
        w, h = image.width(), image.height()
        ptr = image.bits()
        ptr.setsize(image.byteCount())
        arr = np.array(ptr).reshape(h, w, 4)
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)

    def _on_auto_cobb(self):
        if not self._images:
            return
        algo = spine_bridge.get_auto_cobb()
        smart_cls = spine_bridge.get_smart_cobb_line()
        if algo is None or smart_cls is None or self._cobb_tool is None:
            QtWidgets.QMessageBox.warning(self, "提示", "Cobb 测量模块不可用")
            return

        with loading_ui.busy_scope(self._view, "正在自动测量 Cobb 角..."):
            img_bgr = self._render_scene_to_array()
            if img_bgr is None:
                QtWidgets.QMessageBox.warning(self, "提示", "请先拼接图像")
                return
            centerline = algo.calculate_spine_centerline(img_bgr)
            if not centerline:
                QtWidgets.QMessageBox.warning(
                    self, "提示", "未能自动检测到脊柱区域，请改用手动测量")
                return
            lines = algo.estimate_cobb_angle_points(centerline, img_bgr.shape)

        if not lines:
            QtWidgets.QMessageBox.warning(self, "提示", "未能算出有效测量线，请改用手动测量")
            return

        rect = self._scene.itemsBoundingRect()
        self._cobb_tool.clear()
        for p1, p2 in lines:
            smart = smart_cls(
                (p1[0] + rect.x(), p1[1] + rect.y()),
                (p2[0] + rect.x(), p2[1] + rect.y()),
                self._scene, self._cobb_tool.calculate_cobb_angle)
            self._cobb_tool.lines.append(smart)
        self._update_angle()

    def _on_manual_cobb(self):
        if self._cobb_tool is None:
            QtWidgets.QMessageBox.warning(self, "提示", "Cobb 测量模块不可用")
            return
        if not self._images:
            QtWidgets.QMessageBox.warning(self, "提示", "请先拼接图像")
            return
        self._cobb_tool.active = True
        self._view.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self._view.setCursor(QtCore.Qt.CrossCursor)
        self._status.setText("手动测量：在图上点两下画两条线（分别垂直于上/下端椎），自动算夹角")

    def _update_angle(self):
        if self._cobb_tool is None:
            return
        try:
            angle = self._cobb_tool.calculate_cobb_angle()
        except Exception:                             # noqa: BLE001
            angle = None
        if angle is None:
            return
        if angle < 10:
            grade = "脊柱无侧弯"
        elif angle < 20:
            grade = "轻度侧弯"
        elif angle < 40:
            grade = "中度侧弯"
        else:
            grade = "重度侧弯"
        self._angle_label.setText("Cobb 角: %.1f°（%s）" % (angle, grade))

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def _default_name(self):
        if self._paths:
            name = spine_bridge.patient_label(self._paths[0])
            if name:
                return name
            return os.path.splitext(os.path.basename(self._paths[0]))[0]
        return "Stitched_Spine"

    def _on_save_png(self):
        if not self._images:
            return
        self._scene.clearSelection()
        # 导出始终用无缝融合结果（临时切到单层融合模式渲染，含 Cobb 线）
        prev = self._feather_cb.isChecked()
        self._refresh_scene(force_seamless=True)
        try:
            rect = self._scene.itemsBoundingRect()
            if rect.isEmpty():
                QtWidgets.QMessageBox.warning(self, "提示", "画布为空")
                return
            image = QtGui.QImage(rect.size().toSize(), QtGui.QImage.Format_RGB32)
            image.fill(QtCore.Qt.transparent)
            painter = QtGui.QPainter(image)
            self._scene.render(painter, target=QtCore.QRectF(image.rect()), source=rect)
            painter.end()
        finally:
            self._refresh_scene(force_seamless=prev)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存拼接图像",
            os.path.join(os.path.expanduser("~"), "Desktop", self._default_name() + ".png"),
            "PNG (*.png);;JPEG (*.jpg)")
        if not path:
            return
        if image.save(path):
            self._status.setText("图像已保存：%s" % path)
        else:
            QtWidgets.QMessageBox.critical(self, "保存失败", "无法写入：%s" % path)

    def _on_save_dicom(self):
        import cv2                                     # noqa: PLC0415
        import pydicom                                 # noqa: PLC0415

        if not self._images:
            return
        # 导出始终用无缝融合结果（直接合成，不依赖当前显示模式）
        img_bgr = self._build_composite_array()
        if img_bgr is None:
            QtWidgets.QMessageBox.warning(self, "提示", "画布为空")
            return
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存为 DICOM",
            os.path.join(os.path.expanduser("~"), "Desktop", self._default_name() + ".dcm"),
            "DICOM (*.dcm)")
        if not path:
            return
        try:
            meta = pydicom.dataset.FileMetaDataset()
            meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
            meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
            meta.ImplementationClassUID = pydicom.uid.generate_uid()
            meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
            ds = pydicom.dataset.FileDataset(path, {}, file_meta=meta,
                                             preamble=b"\0" * 128)
            ds.PatientName = self._default_name()
            ds.PatientID = "STITCH"
            ds.Modality = "OT"
            ds.SeriesInstanceUID = pydicom.uid.generate_uid()
            ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
            ds.SOPClassUID = meta.MediaStorageSOPClassUID
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = "MONOCHROME2"
            ds.PixelRepresentation = 0
            ds.HighBit = 7
            ds.BitsStored = 8
            ds.BitsAllocated = 8
            ds.Rows, ds.Columns = gray.shape
            ds.PixelData = gray.tobytes()
            ds.save_as(path)
            self._status.setText("DICOM 已保存：%s" % path)
        except Exception as exc:                     # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "保存失败", "写 DICOM 失败：%s" % exc)

    def _on_save_record(self):
        import json                                    # noqa: PLC0415
        from datetime import datetime                 # noqa: PLC0415

        if self._cobb_tool is None or len(self._cobb_tool.lines) < 2:
            QtWidgets.QMessageBox.warning(self, "提示", "还没有可保存的测量结果")
            return
        angle = self._cobb_tool.calculate_cobb_angle()
        if angle is None:
            QtWidgets.QMessageBox.warning(self, "提示", "无法计算 Cobb 角")
            return
        record = {
            "name": self._default_name(),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cobb_angle": round(float(angle), 2),
            "image_count": len(self._paths),
            "image_id": spine_bridge.image_id_label(self._paths[0]) if self._paths else "",
        }
        try:
            records = []
            if os.path.exists(self._records_path):
                with open(self._records_path, "r", encoding="utf-8") as f:
                    records = json.load(f)
            records.append(record)
            with open(self._records_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=4)
            self._status.setText("测量记录已保存（%s）" % self._records_path)
        except Exception as exc:                     # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "保存失败", str(exc))

    # ------------------------------------------------------------------
    # 手动测量的事件转发（复用 spine_app 的交互口径）
    # ------------------------------------------------------------------
    def eventFilter(self, source, event):
        if self._cobb_tool is not None and getattr(self._cobb_tool, "active", False):
            if source == self._view.viewport():
                if event.type() == QtCore.QEvent.MouseButtonPress and event.button() == QtCore.Qt.LeftButton:
                    pos = self._view.mapToScene(event.pos())
                    self._cobb_tool.start_drawing(pos)
                    return True
                if event.type() == QtCore.QEvent.MouseMove:
                    pos = self._view.mapToScene(event.pos())
                    self._cobb_tool.update_drawing(pos)
                    return True
                if event.type() == QtCore.QEvent.MouseButtonRelease and event.button() == QtCore.Qt.LeftButton:
                    self._cobb_tool.finish_drawing()
                    self._update_angle()
                    if not self._cobb_tool.active:
                        self._view.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
                        self._view.setCursor(QtCore.Qt.ArrowCursor)
                    return True
        return super().eventFilter(source, event)

    def resizeEvent(self, event):
        """窗口缩放 / 最大化时把拼接图重新适配到画布，确保看得清。

        仅当场景里已有拼接内容才适配（空场景 itemsBoundingRect 为空，
        fitInView 会无声失败）。延后一帧到 singleShot(0)：此刻布局还没把
        GraphicsView 的视口尺寸更新完，直接 fit 会用旧视口大小算缩放，
        最大化后反而留白。
        """
        super().resizeEvent(event)
        if self._images:
            QtCore.QTimer.singleShot(0, self._on_fit)

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(2000)
        super().closeEvent(event)
