import os
import gc
import json
import shutil
import sys
import re
import threading
import time
import math
import traceback

# ------------------------------------------------------------------
# 嵌入式环境 DLL 搜索路径引导（必须放在 PyQt5 / VTK / pydicom 之前）
#
# 桌面快捷方式直接跑 pyenv\pythonw.exe main.py，不经过 run_env.bat，
# 进程继承的是系统 PATH，导致 pyenv\Library\bin 下的 sqlite3.dll /
# libcrypto.dll 等找不到 —— 表现就是「点漏检反馈按钮弹错」（sqlite3
# 导入失败）与 pydicom 导入异常。runtime_env.bootstrap() 把发行包内
# 的关键目录补进 PATH + os.add_dll_directory，从根上修掉。
# ------------------------------------------------------------------
try:
    import runtime_env as _runtime_env
    _runtime_env.bootstrap()
except Exception:            # noqa: BLE001  引导失败也不能挡住主程序启动
    pass

from PyQt5 import QtCore, QtWidgets, QtGui
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
from vtkmodules.vtkCommonColor import vtkNamedColors
from vtkmodules.vtkCommonCore import vtkOutputWindow, vtkLookupTable, vtkPoints, VTK_FLOAT
from vtkmodules.vtkCommonDataModel import vtkImageData, vtkPolyData, vtkCellArray, vtkImplicitSelectionLoop
from vtkmodules.vtkCommonTransforms import vtkTransform
from vtkmodules.vtkFiltersCore import vtkFlyingEdges3D, vtkPolyDataNormals, vtkProbeFilter, vtkClipPolyData
from vtkmodules.vtkFiltersSources import vtkSphereSource, vtkLineSource
from vtkmodules.vtkFiltersCore import vtkAppendPolyData
from vtkmodules.vtkImagingCore import vtkImageReslice
from vtkmodules.vtkIOImage import vtkDICOMImageReader, vtkImageImport, vtkPNGWriter
from vtkmodules.vtkInteractionImage import vtkImageViewer2
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera, vtkInteractorStyleUser
from vtkmodules.vtkRenderingCore import vtkActor, vtkPointPicker, vtkPolyDataMapper, vtkPropPicker, vtkRenderer, vtkImageActor, vtkActor2D, vtkPolyDataMapper2D, vtkTextActor, vtkCoordinate, vtkWindowToImageFilter, vtkProperty
# 三维长度测量的标签：vtkBillboardTextActor3D 永远正对相机，
# 用世界坐标定位，随模型旋转一起转（VTK 9.5 在 vtkRenderingCore 里）。
from vtkmodules.vtkRenderingCore import vtkBillboardTextActor3D
# MIP/MinIP 投影图用：vtkImageSlice + vtkOpenGLImageSliceMapper 是 VTK 9.5
# 里唯一能进 3D renderer 的「2D 影像切片」组合（vtkImageMapper 是 2D 的，
# 配 vtkImageSlice 会抛 TypeError）。详见 _render_projection 里的说明。
from vtkmodules.vtkRenderingCore import vtkImageSlice
from vtkmodules.vtkRenderingOpenGL2 import vtkOpenGLImageSliceMapper
# 三维体渲染 MIP/MinIP（v1.10.1）：用户期望「像三维重建一样可旋转」。
# vtkSmartVolumeMapper 会自动在 GPU ray casting 与 CPU ray casting 之间选择，
# blend mode 直接支持 Maximum/Minimum Intensity —— 这才是「三维的 MIP」。
# 注意 vtkSmartVolumeMapper 在 vtkRenderingVolumeOpenGL2 里，不在 Core。
from vtkmodules.vtkRenderingCore import vtkVolume, vtkVolumeProperty, vtkColorTransferFunction
from vtkmodules.vtkCommonDataModel import vtkPiecewiseFunction
from vtkmodules.vtkRenderingVolumeOpenGL2 import vtkSmartVolumeMapper
from vtkmodules.vtkImagingCore import vtkImageFlip
from vtkmodules.vtkImagingCore import vtkImageShrink3D
from vtkmodules.vtkRenderingCore import vtkWindowLevelLookupTable

import numpy as np
import pydicom

# 注意：本模块原名 utils.py。X 线 AI 子系统（xray/app）同样以 `utils` 作为包名，
# 两者同名会互相顶掉（sys.modules 里只能存在一个 `utils`）。X 线侧有 21 处
# `from utils.xxx import ...` 引用，改名成本远高于只被本文件引用一次的 CT3D 侧，
# 因此这里把 CT3D 自己的工具模块改名为 ct3d_utils。
from ct3d_utils import (analyze_dicom_series, collect_ct_patients, is_dicom_dir,
                        get_best_series_for_reconstruction,
                        pick_consistent_files, dominant_dimension_uids,
                        is_xr_modality, is_mr_modality, analyze_slice_geometry,
                        is_stackable_series, is_scout_series, mr_series_kind,
                        mr_window_level, mr_percentile_range, _fix_gbk_text,
                        is_single_series_dir, count_dicom_files,
                        xray_part_key, xray_part_key_from_dicom,
                        xray_coarse_to_part, xray_ai_kinds_for_part,
                        xray_part_mismatch_text, XRAY_PART_LABELS,
                        XRAY_PART_UNKNOWN, XRAY_AUTO_PICK,
                        XRAY_AI_PART_SCOPE, is_image_file,
                        IMAGE_FILE_EXTS,
                        _analyze_series_from_dicom_fast,
                        _series_index_uids)
from coordinate_utils import CoordinateConverter, ViewDirectionManager, CursorSynchronizer
import export_utils
# 统一的科技感加载动画（替代各处系统 QProgressDialog / 纯文字提示）
import loading_ui
# 工具按钮图标（运行时用 QPainter 画出来，零外部资源依赖，Win7 友好）
import ui_icons
# 界面皮肤（主题）系统：一套语义色 token 驱动全局 QSS + QPalette，
# 支持一键切换。详见 theme.py 顶部说明。
import theme

# 足弓 5 个关键点的显示色（BGR，与 xray 侧 foot_arch_inference.KEYPOINT_COLORS
# 一致：P1 跟骨 / P2 距骨 / P3 第一跖骨 / P4 第五跖骨 / P5 跟骰）。放到这里
# 是为了让「模型画什么样、手柄就什么样」，两边改一处即可。
_FOOT_ARCH_KEYPOINT_COLORS = (
    (255, 68, 68), (68, 170, 255), (68, 255, 68),
    (255, 170, 0), (255, 68, 255),
)

# 骨 4 个手柄的显示色：上端椎终板线两端一个色、下端椎终板线两端一个色。
# 与 xray 侧 draw_results 里的画法呼应（上橙下紫），但 overlay 是矢量线，
# 比位图上的细线更容易看清端点在哪。
_COBB_LINE_COLORS = (
    (255, 170, 0), (255, 170, 0),   # 1/2 上端椎
    (170, 120, 255), (170, 120, 255),  # 3/4 下端椎
)


# ----------------------------------------------------------------------
# 错误日志助手
# ----------------------------------------------------------------------
# 全局 excepthook 只覆盖「未捕获异常」，但很多错误在槽函数里被
# try/except 吞掉后只弹了窗、没留痕。这里补一个统一写盘的口子：
# 任何被捕获并已提示用户的错误都追加到 error_log.txt，便于事后追溯
# （Win7 现场无控制台，只能靠这个文件定位）。

def _log_error(tag, exc, extra=""):
    """把一条已捕获的错误追加写入 error_log.txt（失败不影响主流程）。"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "error_log.txt")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s: %s\n%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), tag, exc, extra))
    except Exception:               # noqa: BLE001
        pass


# ----------------------------------------------------------------------
# 最大密度投影（MIP）/ 最小密度投影（MinIP）
# ----------------------------------------------------------------------
# 说明：VTK 9.5 里 vtkMaximumScalarProjection / vtkImageProjector 已不存在，
# vtkSmartVolumeMapper 的混合模式又会把 MIP 混进体渲染、还得开 GPU，
# 在这套「离屏 CPU 渲染 + 逐层 numpy」的架构里既不可控也不可测。
# 所以这里直接用 numpy 沿层轴取极值：结果确定、可单测、内存可控。
PROJECTION_SURFACE = "surface"
PROJECTION_MIP = "mip"
PROJECTION_MINIP = "minip"
PROJECTION_MODES = (PROJECTION_SURFACE, PROJECTION_MIP, PROJECTION_MINIP)
PROJECTION_LABELS = {
    PROJECTION_SURFACE: "三维表面重建",
    PROJECTION_MIP: "最大密度投影 (MIP)",
    PROJECTION_MINIP: "最小密度投影 (MinIP)",
}


def project_volume(arr, mode: str, z_stride: int = 1):
    """沿层轴（axis=0）取极值，得到单张投影图。

    参数
    ----
    arr : ndarray，形状 (nz, ny, nx)，第 0 维是层
    mode : "mip" 取最大密度；"minip" 取最小密度
    z_stride : 层方向抽稀步长（全分辨率 MIP 对大体积偏慢，可 >1 降载）

    返回 (proj, used_stride)
    - proj 形状 (ny, nx)，dtype 保持输入 dtype
    - used_stride 实际使用的步长（回传给调用方做 UI 提示）

    空体数据 / 非法 mode 一律返回 (None, 1)，由调用方决定怎么提示。
    """
    if arr is None:
        return None, 1
    arr = np.asarray(arr)
    if arr.ndim != 3 or arr.size == 0:
        return None, 1
    mode = (mode or "").lower()
    if mode not in (PROJECTION_MIP, PROJECTION_MINIP):
        return None, 1

    step = int(z_stride) if z_stride and int(z_stride) > 1 else 1
    if step > 1:
        arr = arr[::step]

    # 投影方向固定为「从体外往体内看」：
    # MIP 取最亮的那层（骨/血管/钙化），MinIP 取最暗的那层（气道/软组织）。
    if mode == PROJECTION_MIP:
        proj = arr.max(axis=0)
    else:
        proj = arr.min(axis=0)
    return proj, step


def projection_window_level(proj, is_mr: bool = False):
    """给投影图挑一组窗宽窗位。

    统一用 1%~99% 分位（自适应），而不是写死肺窗/骨窗：
    投影图的内容跨度随部位变化很大（看骨、看血管、看气道各不相同），
    写死窗值在这三种场景里总有两天全黑或全白。分位自适应对
    CT(HU) 与 MR(信号强度) 语义都成立，因此 is_mr 只影响下限保护，
    不改变计算口径 —— 保证同一体数据 CT/MR 表现一致、可对照。
    """
    if proj is None:
        return 1500.0, -600.0
    finite = proj[np.isfinite(proj)]
    if finite.size == 0:
        return 1500.0, -600.0
    lo = float(np.percentile(finite, 1))
    hi = float(np.percentile(finite, 99))
    if hi <= lo:
        # 分位退化（常量图 / 极端稀疏）时退回 min/max；
        # level 取 mid 而不是 mid+0.5，否则常量图会偏半个窗位。
        fmin = float(finite.min())
        fmax = float(finite.max())
        if fmax <= fmin:
            # 全常量：level 就是那个常量，window 给最小可分辨宽度
            return 1.0, fmin
        lo, hi = fmin, fmax
    return max(hi - lo, 1.0), (lo + hi) / 2.0


def _cobb_angle_from_lines(pts):
    """由两条终板线端点算 Cobb 角（度）；点不齐全或退化时返回 None。

    口径必须和 xray 侧 spine_inference.angle_between_lines 完全一致：先把
    无向线的朝向归一到 [-90, 90)，再取两条线之间的锐角。手动微调出来的
    读数要是跟模型自己的数差一点，用户就会以为算错了。
    """
    if not all(k in pts for k in (1, 2, 3, 4)):
        return None
    try:
        from xray.app.core.model.spine_inference import (
            angle_between_lines)
        upper = (tuple(pts[1]), tuple(pts[2]))
        lower = (tuple(pts[3]), tuple(pts[4]))
        return float(angle_between_lines(upper, lower))
    except Exception:        # noqa: BLE001
        pass
    # 模型不在（或被裁剪）时走同一套数学，保证读数一致
    def _norm(a):
        return (a + 90) % 180 - 90
    a1 = _norm(math.degrees(math.atan2(pts[2][1] - pts[1][1],
                                       pts[2][0] - pts[1][0])))
    a2 = _norm(math.degrees(math.atan2(pts[4][1] - pts[3][1],
                                       pts[4][0] - pts[3][0])))
    d = abs(a1 - a2)
    return float(180 - d if d > 90 else d)


def _cobb_extension(l1, l2, factor=2.5):
    """把两条终板线沿各自方向延长并求交点，用于读数挂在交点旁。

    纯几何（只画延长线，不改变读数），所以不引模型依赖；实在求不出交点
    （两线近乎平行）就返回 None，读数改挂在上端椎终板线中点。
    """
    def _ext(line):
        (x1, y1), (x2, y2) = line
        dx, dy = x2 - x1, y2 - y1
        return ((x1 - dx * factor, y1 - dy * factor),
                (x2 + dx * factor, y2 + dy * factor))
    e1, e2 = _ext(l1), _ext(l2)
    (x1, y1), (x2, y2) = e1
    (x3, y3), (x4, y4) = e2
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-10:
        return e1, e2, None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d
    return e1, e2, (x1 + t * (x2 - x1), y1 + t * (y2 - y1))

# 程序版本号（唯一来源）：与 技术说明.md / 用户说明.md 的最新版本号保持同步
# v1.7.0：并入 X 线辅助诊断（胸片/骨折/脊柱/骨龄/足弓），患者列表 CT 与 DR 共存
# v1.7.4：修复 DR 正位/侧位多序列时「列表预览只有一张、AI 检测的却是另一张」
# v1.7.5：正位/侧位被设备写进同一个 SeriesInstanceUID 时，按图拆成多个序列，
#         列表里每张片子各占一行、各有一张缩略图
# v1.7.6：新增「漏检反馈」——模型没识别出来时可一键登记当前片子，
#         图片存到软件目录 feedback_images/，记录写入数据库（只存路径）
# v1.7.7：修复「点漏检反馈就闪退」——FEEDBACK_KINDS 的值是字符串，界面却按
#         (名称, 提示) 二元组解包，构造对话框时 ValueError，PyQt5 在槽里
#         直接 abort 掉整个进程；新增缩略图拖放到阅读区打开序列
# v1.8.0：修复「勾选去除细小碎屑后三维图像质量明显下降、看起来都是纹路」——
#         filter_bone_components 剔除碎屑时用 out[~keep[labels]] = bg，把
#         labels==0 的背景体素也一并砸成 arr.min()，摧毁了阈值附近的 HU 梯度，
#         等值面插值点被逐体素推向内侧，抖动放大成横向条纹、表面碎裂成片。
#         改为只写被剔除的连通域体素，背景逐体素保持原值。
# v1.9.0：新增 MRI 支持。像素是信号强度而非 HU，故MR 走独立的窗值策略、
#         独立的三维血管重建（按分位数自适应阈值，不套 CT 的 HU 床体识别），
#         并在列表中滤除定位像与3D_Batch 重建批次。
# v1.9.1：X 线 AI 辅助诊断加「部位适用」约束。原先载入任意 X 线后五个检测
#         按钮全部放开，在胸片上点「足弓测量」模型照样跑出一个角度值 ——
#         数值毫无临床意义却看起来像正常结果。现在载入 X 线时先判检查部位，
#         不匹配的功能按钮置灰并说明原因，点击路径上再拦截一次；
#         「检测设置」里可勾选忽略该约束（设备乱写 BodyPart 时的出口）。
# v1.9.2：所有等待环节换用统一的科技感加载动画（loading_ui.py）——替代系统
#         QProgressDialog 的灰底白条；新增旋转指示器、HUD 浮层、贴附式遮罩。
#         注意：QWidget.setVisible 是虚函数，TechBusyDialog 里 show/hide/
#         setVisible 必须收口到 _show_impl/_hide_impl 且只调 super().setVisible()，
#         否则 show→setVisible→show 无限递归直接爆栈（实测 exit 127 段错误）。
# v1.9.3：胸片检测对儿童片必报「心脏肥大」——模型是成人数据训的，而婴幼儿心影
#         本就相对大。现在按患者年龄对 Cardiomegaly 做二次判读：用心影框估
#         心胸比，与该年龄的儿科上限比，未超限的降级为「心影偏大（儿童生理性）」
#         且不计入检出病变数；估不出的标「未能核实」。其余 13 类不受影响。
# v1.9.4：「打开影像文件」行为修正。①原先只认 .dcm，现在也吃常见图片
#         （PNG/JPG/BMP/TIFF/WebP，走 cv2 解码，绝不喂给遇到非 DICOM 会段
#         错误的 vtkDICOMImageReader）；②单文件载入补齐了与「载入序列」一致的
#         模态窗值策略——X 线原先套 CT 的 400/40 兜底，整幅发白，看起来像
#         「新文件没替换掉旧图」；③单文件载入现在也按模态切换工具区（X 线
#         显示 AI 辅助诊断分组），并把精确文件路径写进序列信息，AI 检测不再
#         从目录里随便挑一张片子。
# v1.9.5：足弓关键点可手动微调。模型对 P1~P5 的定位常有几像素偏差，重训要
#         大量标注数据，所以留了「微调」口子：跑完足弓检测直接拖图上的五个
#         手柄，拖动即时重算内侧/外侧足弓角并写回摘要与指标。
#         手柄画在既有 overlay 层（Layer 1）上、与底图共用相机，拖动时底图
#         仍是 AI 标注图；被拖动的点会套一个白环并用「原」标出模型原始位置，
#         「复位关键点」一键还原。坐标一律记在图像像素（x 右 y 下，与 cv2
#         一致），画到 overlay 前按 set_ai_overlay_image 那套 spacing 换算，
#         否则手柄会落在模型画的点旁边（上下镜像的坑见 v1.9.4 的DICOM 说明）。
# v1.9.6：脊柱侧弯（Cobb 角）可手动微调。与足弓共用一套「拖手柄 → 即时重算
#         → 复位」机制，区别只在形态：模型交给上层的是上下终板线各两个端点
#         （上端椎 P1/P2、下端椎 P3/P4），拖端点即时重算 Cobb 角。读数口径
#         直接复用 xray spine_inference 的 angle_between_lines（无向线归一到
#         [-90,90) 再取锐角），手动调出来的数与模型自己的数严丝合缝。
#         overlay 上画两条终板线 + 细延长线 + 交点环，读数挂在交点旁；两条线
#         平行求不出交点时读数改挂上端椎线，不崩。
# v1.9.7：修 PACS 导出目录「加载患者目录读不出来」。两条独立根因：
#         ① 从 PACS 导出的 DX 文件头里有 (0088,0200) PresentationState
#            ReferenceSequence，vtkDICOMImageReader 的 DICOMParser 会解析到
#            一半放弃、Rows/Columns 取不到 → 整卷退回默认 64x64（画面一小块
#            黑图），而 Z 维还在，上层「维度异常」拦不住。现在两处一起兜：
#            _load_series 先做一次 modality 探测（DX/DR 直接走 pydicom 装配），
#            加载线程里再拿 pydicom 文件头尺寸跟 VTK 读出的对一遍，不一致就
#            换 pydicom 装配重读。
#         ② PACS「导出为图片」的目录里是散的 JPG（实测 04 顶层 0 文件、图片
#            躺在 Untitled-0/-1/-2 里），既不是单序列目录也不是患者根目录。
#            新增 ImageSeriesReader（目录图片当序列，尺寸不齐补到公共网格）
#            与 _open_dir 的四分支决策：纯图片目录整目录当序列，图片在子目录
#            就问一句并加载张数最多的那个，可翻层。
# v1.9.8：图片目录「按文件夹分组」+ X 线多文件序列。用户从 PACS 导出的
#         07 目录（顶层空、子目录 Untitled-0 里 5 张尺寸各不相同的 DX）扫不
#         到、直接加载又「只显示第一张」。改了三处：①XraySeriesReader 以前只
#         拿第一张的帧数当层数，目录里其余 .dcm 整个丢掉，现在每个文件一层；
#         ②DicomLoadWorker 为防 VTK 段错误而做的「尺寸少数派剔除」只在
#         vtkDICOMImageReader 下生效（pydicom 装配会自己居中补边）；③目录树
#         扫不到序列时，按「一个文件夹 = 一条序列」把图片列进右侧患者列表。
# v1.9.9：设置目录增量 + 深层嵌套扫描 + 混放多 UID 拆分。用户反馈两问
#         题：①设置里添加目录会清空程序原有列表——原因是设置确认把
#         self._root_dir 传给 _load_patient_tree，而显式 root_dir 会独占
#         扫描根（新加的设置目录永远轮不到）；改为传 None + incremental。
#         ②F:\疑难病例 这类多级嵌套目录扫不出——图片分组兜底只扫
#         root_dir 单根，改为扫全部扫描根；③一个文件夹里混放多条序列
#         （如 hiv肺炎 756 张实为 155 条多 UID），按 SeriesInstanceUID
#         拆开各成一条带 file_uids；④pat_id 用绝对路径防同名 key 撞车；
#         设备占位描述 DummySeriesDesc! 当空回退「序列N」。
# v1.10.0：①新增最大密度投影 MIP / 最小密度投影 MinIP（三维右侧「渲染模式」
#         下拉，投影模式下隐藏无效的阈值/去床板/配色）；②患者树右键导出
#         DICOM（原文件拷贝）与 PNG/JPG 图片到桌面「患者名/序列名」目录，
#         患者行导全部序列、序列行导单个序列；③MR 的 MRA 血管重建序列
#         （ProtocolName=tof3d_tra_MRA、无 IPP）不再被丢弃，改为保留在
#         列表并标「仅二维（不支持三维）」；④患者列表占位提示统一收口，
#         刷新出患者后不再残留「未找到CT薄层序列」。
# v1.10.1：MIP/MinIP 升级为**三维体渲染**（vtkSmartVolumeMapper 的
#         Maximum/Minimum Intensity blend，可像三维重建一样自由旋转）；
#         体渲染不可用时自动回退二维投影图并弹窗说明。三维投影下
#         显示阈值/去床板滑块（传递函数与掩膜依赖它们）。
# v1.11.0：新增「肺结节筛查（CT）」（pulmo_bridge + Pulmo 两阶段模型）：
#         【已于 v1.15.0 整体移除，以下 v1.11.x / v1.12.0 相关条目仅作历史留档】
#         胸部 CT 一键筛查结节，结果列表 + 二维视图圆圈标记 + 点击跳层，
#         良恶性概率附 8 征象归因（毛刺/分叶/钙化等）。仅供研究参考。
# v1.11.1：肺结节筛查增加「使用 GPU 加速推理」勾选（torch 为 CUDA 版时
#         可用，QSettings 记忆选择；CPU 环境灰置并说明）。
# v1.11.2：肺结节筛查增加肺区域过滤：HU 阈值法估计肺野（剔除体外空气取
#         大连通域），Stage 1 肺外候选（胸壁/纵隔/骨假阳）一律剔除，
#         估计失败 fail-open；完成状态显示剔除数量。
# v1.12.0：肺结节筛查检出阶段升级为 MONAI RetinaNet（标准版，Apache-2.0，
#         LUNA16 官方 bundle：1 FP/scan 灵敏度 0.957 / CPM≈0.94，替代
#         Pulmo Stage1 热图的 CPM≈0.63），良恶性/征象/分割仍用 Pulmo
#         Stage2。LUNA16 官方双标注实测：命中误差 ≤1.5 体素（约 1~2mm），
#         置信度 0.999+（旧热图全卷 max≈0.29）。MONAI 不可用（Win7 版）
#         自动回退 Pulmo Stage1，行为同 v1.11.3。
# v1.11.3：修复病例目录混放多序列时肺结节筛查「缝合」体数据问题：
#         build_volume_from_dir 按 SeriesInstanceUID 分组（指定 uid 用该
#         组，否则取最大组），主程序把当前阅读序列的 UID 传给筛查，
#         保证「筛的和看的是同一条序列」。LUNA16 官方标注实证：模型与
#         预处理通路本身正确（官方结节命中误差 <6mm）。
# v1.13.0：本轮四项改动
#   ① 修复 Win7 上「点漏检反馈按钮弹错」：根因是桌面快捷方式直接跑
#      pythonw.exe main.py、绕过了 run_env.bat，进程 PATH 缺 pyenv\Library\bin，
#      sqlite3.dll 找不到 → import sqlite3 报 "DLL load failed"。新增
#      runtime_env.py 在启动最早期补 DLL 搜索路径（PATH + os.add_dll_directory），
#      并在反馈入口做 SQLite 可用性自检，失败时给出可读中文提示。
#   ② 患者列表刷新完成的提示从 QMessageBox 改为 Toast 轻提示
#      （loading_ui.Toast，自绘 + 自动消失 + 不阻塞事件循环）。
#   ③ 三维窗口新增「测长」：在模型上点两点量距离（mm），标签用
#      vtkBillboardTextActor3D 始终正对相机；二维长度测量原本已具备，
#      本轮统一改为图标 + 汉字悬停提示。
#   ④ 全部工具按钮（CT值/长度/角度/清除/切割/撤销/重置/截图/多视角/序列/
#      测长/清测量）改为运行时 QPainter 绘制的图标，鼠标悬停显示汉字提示。
# v1.14.0：新增「报告质控」功能模块（reportqc）
#   ① PACS 报告区域 OCR 质控：左栏「报告质控」按钮 → 全屏框选 PACS 报告
#      区域 → 截图 → PP-OCRv4 识别（自实现推理，零 pyclipper/shapely）→
#      字段提取（姓名/影像号/所见/结论）→ 规则引擎质控 → 悬浮小窗四级
#      配色告警（危急红 / 需修改橙 / 需复核黄 / 通过绿）。
#   ② 数据层：SQLite 7 张表（reportqc/config/report_qc.db），存检测记录、
#      问题明细、误判标记流水、忽略词、日统计；详情窗可标记误报/忽略词，
#      历史窗支持筛选与 CSV 导出（utf-8-sig，Excel 直开不乱码）。
#   ③ 左栏新增「质控历史」「质控设置」入口；设置含 OCR 放大倍数、
#      提示级问题显示开关、忽略词管理，持久化到 QSettings。
#   ④ QApplication 创建前调用 reportqc_bridge.ensure_dpi_aware()，
#      保证框选坐标与截图像素一致。
# v1.15.0：移除「肺结节筛查」功能，精简发行包体积并强化 Win7 兼容性
#   ① 删除 pulmo/（Pulmo 两阶段模型 .pth 约 40MB）、pulmo_monai/
#      （MONAI RetinaNet model.pt 约 80MB）、pulmo_bridge.py、pulmo_monai.py；
#      main.py 移除对应面板、Worker、二维视图圆圈标记及全部调用点（约 490 行）。
#      依赖的 PyTorch（约 5GB）随之不再需要，安装包大幅瘦身。
#   ② 同步移除其它未被使用的重型依赖（selenium / kivy / kubernetes /
#      clearml / nipype / tensorboard / pythonwin / sympy / py_mini_racer 等）。
#   ③ Win7 兼容：启动早期统一设置进程默认 TLS 1.2、禁用可能缺失的
#      现代 API 调用路径；DLL 搜索路径引导保持 PATH 优先（Win7 无
#      KB2533623 时 os.add_dll_directory 不可用，PATH 方案同样有效）。
#   ④ 删除发行包根目录下一份与 pyenv 重复的 Lib/ 副本（约 262MB）。
# v1.15.0 界面调整：
#   ⑤ 工具栏（2D 阅片左下角 + 三维窗口）统一改为「图标在上、汉字在下」，
#      新增「选择」「反色」两个按钮，栏尾加「隐藏」可整条收起
#      （收起后左下角留「工具」按钮恢复）；窗口最大化按钮从单独浮层
#      移入工具栏。两处工具栏都改为 QWidget 容器整体摆位，不再逐个 move()。
#   ⑥ 轻提示（Toast）由顶部改为居中显示；三维重建的进度文案统一为
#      「正在重建三维模型，请稍候…」。
# v1.16.0：报告质控改为「常驻悬浮小窗 + 区域只设一次」
#   ① 程序启动即自动弹出悬浮小窗（就绪态，不自动识别）；左栏「报告质控」
#      改为「显示/重新打开小窗」。
#   ② 识别区域持久化到 QSettings（reportqc/pacs/region_store.py，存
#      HKCU\Software\ZhiGu\CT3D）：首次点「设置区域」框选一次，之后每次
#      点小窗「刷新」或**双击小窗**即按保存区域截图识别，不再重复框选。
#   ③ 小窗新增患者信息卡（姓名大字 + 性别/年龄 + 影像号/检查号 + 设备部位），
#      一眼确认是不是当前患者的报告；「刷新」改为主色按钮。
#   ④ 就绪态/未设置态有明确引导文案；区域因换显示器失效时自动当作未设置。
#   ⑤ 修复：picker 的 event.globalPos().toPoint() 在 Qt5 下必抛
#      AttributeError（Qt5 返回 QPoint），导致框选卡住无法退出；改为
#      类型兼容取坐标 + mouseReleaseEvent 异常兜底（finished 必发射一次）。
#   ⑥ 修复：reportqc_bridge._run_pipeline 未先 _ensure_window()，
#      小窗未建时 AttributeError。
# v1.17.0：报告质控三个可用性问题（用户实测反馈）
#   ① 误判学习真正生效：原来「标记为误报」只写库不读回，下次照样报。
#      现在 set_verdict 会从问题里**抠出关键词**写进 learning_records 和
#      learned_words，controller 每次检测前读取并过滤 → 标记一次不再报；
#      设置窗可查看/撤销。
#   ② 新增「自定义错别字」入口（设置 → 自定义规则页签）：错词 + 正确写法
#      + 级别，存 custom_typos 表，每次检测注入 checker，加了立即生效。
#   ③ 修复医学英文缩写误报：原来「不在硬编码白名单就报无意义输入」，
#      实测把 ECT / PICC / TACE / VATS / DTPA / RADS 全报成错别字。
#      改为举证式判定（全大写 2~6 字母视为缩写，只有键盘连按、连续重复、
#      无元音长串才报），并降为提示级；另加「医学术语白名单」入口。
# v1.18.0：患者列表搜索 + 导出脱敏
#   ① 患者列表新增搜索框：汉字子串、全拼、拼音首字母、患者 ID 都能匹配；
#      多音姓氏（单 shan/dan、曾 zeng/ceng、解 xie/jie…）把每个读音都算作
#      关键词，否则「输入 shan 搜不到单田芳」。Ctrl+F 定位搜索框，Esc 清空。
#      只对顶层患者行 setHidden，不重建树 —— 重建会丢掉缩略图缓存、
#      展开状态和当前选中项。
#   ② 右键导出 DICOM / PNG / JPG 时先弹「导出设置」，可选是否脱敏：
#      · DICOM：姓名→ANON-001、ID→假名，生日/地址/电话/送检医师/医院名/
#        设备序列号等清空，私有标签删除，各类 UID 重新生成（同一次导出内
#        一一对应，同序列各层保持一致，导入 PACS 仍是一个序列），文件名
#        改流水名（原文件名常含姓名）；性别/年龄/检查日期保留。
#      · 图片：导出目录改用「匿名001」，可选把四角烧录文字涂黑（CR/DR）。
#      · 一律只改**导出副本**，源目录原始文件不动。
#   ③ 修复：VTKViewer.showEvent 仍调用改名前的 _position_fullscreen_button，
#      切三维直接 AttributeError 崩溃（工具栏重构遗留）。
#   ④ 新增 pypinyin（纯 Python，Win7 可用）用于拼音检索。
# v1.18.1：修复图片导出的窗值 / 极性（「看到脑窗导出成骨窗」「DR 导出成负片」）
#   ① 根因一：导出走的是裸 pydicom.pixel_array，**没做 Modality LUT**。
#      CT 的 RescaleIntercept 常是 -1024（实测头颅 CT 原始值 11~328 →
#      HU -1013~-696），而 DICOM 的 WindowCenter/WindowWidth 是「LUT 之后」
#      域的值；显示端走 vtkDICOMImageReader（读取时已应用 LUT），导出没跟，
#      两边不在同一个域。现在导出统一先做 Modality LUT。
#   ② 根因二：导出没传窗值，走 1%~99% 分位自适应。头颅 CT 的分位区间跨
#      空气到颅骨，等效 WW≈2000+，正好是骨窗量级 → 灰白质（相差 10HU）
#      被压成 1 个灰度级，糊成一片。现在优先用当前视图窗值（所见即所得），
#      其次 DICOM 自带 WC/WW（设备的诊断窗），脏值才退分位。
#   ③ 根因三：导出没处理 PhotometricInterpretation。MONOCHROME1 是负片
#      （值越大越暗），显示端有 series_needs_invert + _invert_polarity，
#      导出没有 → 显示正常、导出负片。现在逐帧判断并翻正。
#   ④ 视图窗值取用时按「整卷 min+max」把窗位镜像回原始数据域：视图里的
#      窗位是配给已反相数据的，直接套到原始数据会整体偏移。
#   ⑤ 反相与四角遮盖的顺序：先反相再涂黑，否则负片导出的四角变成白块。
#   ⑥ 顺带修掉 _PHI_KEYWORDS 里的无效关键字 PatientEthnicGroup（pydicom
#      每次脱敏导出都刷 UserWarning；EthnicGroup 已在列表内）。
# v1.18.2：报告质控小窗精简（状态条并入标题栏）
#   ① 删掉 44px 的独立状态条，结论改成标题栏里的胶囊
#      （就绪 / ··· / 危急 / 修改 / 复核 / 通过 / 未设置 / 错误），
#      右侧只留一句话小字（问题计数 / 操作提示），超宽省略、全文进 tooltip；
#      就绪态窗口高度 260 → 188px，压在 PACS 角落更不碍事。
#   ② 不再显示识别区域的尺寸坐标（"760 × 900 @ (40,60)"）：医生不关心，
#      那行只会让小窗变啰嗦。
#   ③ 患者卡空行自动隐藏；浅底胶囊（黄 / 绿）自动换深色字 ——
#      原来白字压在 #d8b90a 上几乎看不清。
# v1.19.0：全面测试后的性能与兼容性优化（不改任何交互）
#   ① 患者列表扫描：热缓存 3.31s → 0.68s，冷缓存 8.48s → 4.09s
#      （IPP/IOP 加指纹缓存 + specific_tags；mtime 复用目录索引不再逐
#        文件 stat，原实现一次全量扫描 stat 4.5 万次；大目录并发读头）
#   ② 图片导出：909 张 PNG 10.89s → 4.25s（4 线程并行，进度回调仍在
#      调用线程，结果保序，已验证与串行逐像素一致）
#   ③ 中文路径：主流程两处裸 cv2.imread 换成 imread_cn（脊柱拼接在中文
#      目录下原本整批读不出来）；cv2.imwrite 对非 ASCII 路径静默失败，
#      导出早已走 imencode，这次把读取也统一
#   ④ xray 的 VOI LUT 失败不再裸 except: pass（片子发灰却查不出原因）
#   ⑤ 自检 75 → 89 项；新增 _tools/ 下的静态扫描与性能实测脚本
# v1.19.1：报告质控三处体验修整
#   ① 小窗年龄不再重复「岁」：PACS 上年龄常写作「62岁」，原来无条件拼
#      「%s岁」→ 显示成「62岁岁」。改由 fields.format_demo() 统一去尾「岁」。
#   ② 影像号可点击复制：医生要拿它在 PACS 里搜患者，14 位数字手抄易错。
#      点患者卡第二行即进剪贴板，标题栏给一句「已复制影像号 …」，
#      1.6s 后自动还原摘要（不弹窗，不打断阅片）。
#   ③ 修掉「标点符号异常」满篇误报：checker_grammar 原来用「汉字 5 个以上
#      + 后面不是白名单字符」判异常标点，白名单漏了半角 , . ( ) : ; 和
#      省略号 …；而医生本来就习惯半角标点、OCR 也常把「，」认成「,」，
#      于是几乎每份报告都报两条。现在只报真有举证价值的字符：
#      控制字符/乱码（high）、报告里不可能出现的 @ # $ ^ | \ `（low）、
#      同类标点三连以上（low）；半角标点、省略号、数字单位一律放过。
# v1.20.0：报告质控同时支持两套写报告界面
#   ① 识别区域从「一条」升级为「一组配置」，每条绑定框选时的前台进程名
#      （reportqc/pacs/region_store.py → QSettings 存 JSON 数组）。
#      体检登记软件和标准 PACS 各框一次，点刷新时按当前前台窗口自动选，
#      来回切换不用改设置。旧格式首次读取时自动迁移，原 key 保留不删。
#   ② 前台窗口用 1s 轮询记录「最近一个不属于本进程的窗口」
#      （reportqc/pacs/windowinfo.py，纯 ctypes，Win7 可用）：用户点小窗
#      按钮触发刷新时前台是小窗自己，现取必然匹配不上。
#   ③ fields.extract_fields 按版式自适应：体检登记界面（项目表格 +
#      右侧检查结果/诊断建议，layout=form）与标准报告界面（影像所见 /
#      诊断结果，layout=titled）各走一套正文切分；三块正文全部质控。
#   ④ 顺手修掉：行内标签后跟非标签 UI 文字时取值一路吃到行尾
#      （体检编号变成「101061223  科室:DK室」）、OCR 美术符号行混进正文。
#   ⑤ 设置窗「常规」页加「识别区域」一行，可查看已绑定软件并清除。
# v1.21.0：四套界面皮肤，一键切换（布局与交互零改动）
#   ① 新增 theme.py：一套皮肤 = 一组语义色 token（canvas/panel/field/
#      raised/button/accent/text/border…），控件只认 token、不写死颜色，
#      所以「换皮肤」只是把另一组值灌进同一张全局样式表 + QPalette。
#      四套：石墨（炭灰）/ 墨蓝（海军蓝）/ 象牙（暖白纸感）/ 青瓷（冷白釉色）。
#   ② 入口在右侧「工具区」标题旁，点开是自绘浮层（色片 + 名字 + 一句话
#      说明），点哪套当场生效，选择存 QSettings 的 ui/skin，下次启动沿用。
#   ③ 阅片区恒定深色：VIEWER 与皮肤解耦（只有强调色跟随 accent_view）——
#      影像背景跟着变亮会直接影响诊断，这条不开放给皮肤。
#   ④ 勾选框/单选框/下拉箭头/数字箭头全部改由 QProxyStyle 自绘，零外部
#      图片资源（发行包仍不带任何图标文件，Win7 直接可用）。
#   ⑤ 报告质控四个对话框、质控小窗、脊柱拼接、授权窗口、加载动画与 Toast
#      全部接入；加载组件那种纯 QPainter 自绘的块改为每帧向 theme 取色。
# v1.22.0：在线升级 + 增量补丁（不用再整包拷 1.4 GB）
#   ① 新增 updater.py：从升级服务器拉 latest.json，用内置 RSA 公钥验签，
#      再按补丁链逐级下载（v1.21.0 → v1.22.0 → …），每个包校验 SHA256。
#   ② 新增 update_ui.py：右侧「工具区」标题旁新增「更新」按钮；启动 6 秒后
#      后台静默查一次（没网/服务器不通都不打扰），查到新版按钮变强调色，
#      点开是更新窗口（版本对比 + 更新说明 + 下载进度），装完自动重启软件。
#   ③ 为什么能省：发行包 1.4 GB 里 95% 是 pyenv 与 AI 模型，从不变化；
#      补丁只含变动过的 .py/.md，通常 1 MB 以内，压缩后几百 KB。
#   ④ 安全：索引由 RSA-2048 签名、补丁包带 SHA256 指纹，即使走明文 HTTP，
#      链路被劫持也装不进伪造代码。验签用 pow() + hashlib 手算，
#      不依赖 cryptography —— Win7 上那套 libcrypto 冲突的坑不碰。
# v1.22.1：修「换皮肤后按检查类型着色失效」
#   ① theme.py 的全局 QSS 通配给 ::item 写了 color，QStyleSheetStyle 会拿它
#      盖掉 QWidgetItem 的 ForegroundRole —— setForeground() 全部失效
#      （患者名按 CT/DR/MR 着色、质控历史按结论等级着色一起变灰）。
#      ::item 里只留 border-radius，文字基色交给 QTreeView/QTableView 的 color。
#   ② 模态配色分深浅两套：浅色皮肤列表底色是纯白，原亮色对比度仅 1.8~2.5。
#   ③ 「年龄缺失刷成灰」的分支去掉 —— 它会盖掉刚刚恢复的类型色。
#   ⑤ 安装前自动备份到 _upgrade_backup\，任何一步失败逆序回滚；
#      report_qc.db / detection_records.db、阈值设置、反馈图、日志一律不碰。
# v1.22.2：修「质控小窗患者信息被部位/时间行、按钮区压住」
#   ① 根因不是文案，是**高度算错**：窗口高度以前按「30 + 卡片 + 6 + 42 +
#      列表 + 12」手算，而 QScrollArea 的 sizeHint 与它的最小高度差 20px，
#      结果窗口比布局最小值矮一截 → Qt 让列表区直接压在患者卡上。
#      改为向布局要 root.sizeHint()/minimumSize()，并以最小值为下限。
#   ② 患者卡第三行（设备/部位/项目/时间）去掉 wordWrap 改单行：折行的第二行
#      会画到卡片外面压住列表。超长用**中间省略**（保住末尾的检查时间），
#      全文进 tooltip。
#   ③ 部位与项目重复（「胸部 · 胸部CT平扫」）合并成一条；性别年龄不再被
#      自适应宽度反复挤成「男 /…」；三行各留 4~6px 行高余量，字不再切边。
#   ④ 底栏按六个按钮的实际需要宽度兜底最小窗宽，窄窗时按钮不互相压字。
# v1.22.3：修「点升级后软件退出了、却没重启，手动打开还是老版本」
#   ① 根因一：补丁执行器遇到「目标文件与升级前指纹不一致」直接中止（退出码 4），
#      而在线升级时控制台是一闪而过的，用户完全看不到。改为**默认备份后覆盖**
#      （仍可用 --strict 恢复旧行为），原件都进 _upgrade_backup 可回滚。
#   ② 根因二：主程序刚收到退出指令时还没放掉 main.py 句柄，安装器 3 秒后就开始
#      覆盖 → PermissionError → 回滚 → 版本没变。改为**重试替换**，最多等 45 秒。
#   ③ 无论成功失败，只要是从软件里点的升级，装完都把软件拉回来
#      —— 失败时至少能继续用旧版，而不是面对「程序自己消失了」。
#   ④ 全过程写 <软件目录>\_update_log.txt；失败弹系统提示框告知原因与备份路径。
#      失败时 cmd 窗口停留 20 秒，不再一闪而过。
#   ⑤ 在线升级不再经「升级.bat」中转，直接调 pyenv\python.exe，
#      客户机路径带空格或中文时不会被 cmd 拆坏。
# v1.22.4：修「点升级报『补丁包不完整：缺 manifest.json』」
#   ① 根因：升级.bat 里 set "HERE=%~dp0" 末尾带反斜杠，写成
#      --source "%HERE%" 时展开成 --source "D:\...\_patch\"，Windows CRT 把
#      结尾的 \" 当成转义引号，参数值变成 D:\...\_patch"（末尾多一个引号）
#      → 拼出的 manifest 路径根本不存在 → 升级一个文件都没装就退出。
#   ② 升级.bat 不再传 --source：执行器默认就用自己所在目录（补丁解压根），
#      少一个参数少一个坑。
#   ③ 执行器加自愈：传进来的目录末尾若带引号 / 反斜杠 / 点，先规整；
#      该目录里没有 manifest.json 时退回脚本自身目录再找一次。
#      —— 客户机上残留的旧 bat 也不会再卡在这一步。
#   ④ 报错文案带上实际查找的目录；CT3D_NO_POPUP=1 时可跳过系统弹窗
#      （无人值守时弹窗会一直挂着等点击）。
APP_VERSION = "v1.24.2"

# ---------------------------------------------------------------------------
# 工具栏统一外观（v1.15.0）
#
# 工具栏按钮改为「图标在上 + 汉字在下」，所以按钮比原来高不少；
# 高度集中成一个常量，2D 与三维两处工具栏共用，避免两栏高度不一致。
# ---------------------------------------------------------------------------
TOOL_BTN_H = 44          # 图标 18 + 8pt 标签 + 内边距
TOOL_BTN_W = 46          # 三字标签（如「CT值」）也放得下


def tool_btn_qss():
    """工具栏按钮样式（阅片区浮层，恒定深色玻璃；强调色随皮肤走）。

    v1.21.0 起不再写成常量：按钮压在影像上，必须始终保持深色底，
    但「选中」用的是皮肤强调色的**亮色版**（accent_view），
    所以每次切皮肤都要重新生成，交给 ``theme.bind`` 注册。
    """
    return theme.qss_tool_buttons()


def tool_bar_qss():
    """工具栏容器：背景透明（不要挡住影像），按钮样式同上。"""
    return "QWidget { background: transparent; }" + theme.qss_tool_buttons()

# 调试日志开关：默认关闭。光标同步/校准等热路径 print 走控制台 I/O，
# 每次鼠标移动都会触发，低配机器上会明显拖慢交互。
# 需要排查问题时设环境变量 CT3D_DEBUG=1 恢复输出。
_DEBUG_LOG = os.environ.get("CT3D_DEBUG") == "1"

DEFAULT_DATA_DIR = r"D:\ClearTmp\DicomTmp"
DEFAULT_SCAN_DIRS = [r"D:\Cache", r"D:\ClearTmp\DicomTmp"]

def mra_vessel_threshold(arr: np.ndarray, percentile: float = None,
                         min_keep_fraction: float = 0.0008):
    """求 MRA 血管提取阈值，返回 (阈值, 统计信息)

    **实测结论：不能用高分位数，必须用绝对高阈值**
    初版按 p92 取阈值，离屏渲染出来是**整个头颅的软组织壳**（表面碎片
    11434 片），根本不是血管。原因是这批 TOF 序列的脑组织背景本身就很亮
    （全幅 p90 已达 206），血管并不是「最亮的那一撮」。

    薄层原图目视确认：血管在 Willis 环附近只是几个**小白点**，
    而脑实质亮度甚至高于血管。所以真正的 MRA 亮血管在设备的
    ``tof3d_tra_MRA_MIP`` 序列里（3 张不同角度的 MIP 投影图）。
    要从薄层重建血管，只能靠**绝对高阈值**把血管「烧」出来。

    实测阈值扫描（186 张 0.5mm 薄层，p50=58 / p90=206 / p99=281 /
    p99.9=417 / max=1323），用「表面连通片数 + 目视」双判据::

        thr   保留体素   最大连通域   表面片数   形态（离屏渲染目视）
        250   2.30%      21%         2465       脑组织块状壳 → 不可用
        300   0.61%      19%         767        血管 + 大量脑组织碎块
        350   0.20%      46%         56         干净血管树，末梢略少
        400   0.11%      63%         39         ★ 最佳（Willis 环清晰）
        450   0.08%      70%         34         ★ 最佳（末梢最完整）
        500   0.07%      36%         36         开始丢细末梢
        600   0.04%      35%         19         仅主干

    **分界很清楚：<350 是脑组织壳，350~500 是血管树，最佳区400~450。**

    策略：以 ``p99.9 的倍数`` 为基线（**不能用 max**），再加保留量兜底。
    ``percentile`` 传 None 时只用p99.9 基线（MR 血管理线默认如此）。
    """
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return None, {}
    # 16bit 数据量级大，抽样算分位数即可
    sample = finite[::max(1, finite.size // 2000000)]

    stats = {
        "p50": float(np.percentile(sample, 50)),
        "p90": float(np.percentile(sample, 90)),
        "p99": float(np.percentile(sample, 99)),
        "p999": float(np.percentile(sample, 99.9)),
        "max": float(finite.max()),
    }

    # 标定依据：取 **p99.9 的倍数**，而不是 max。
    # max 是单个体素的极值（实测 1323，而 p99.9 才 417、p99.99 才 790），
    # 按 max 定标会被单点噪声拉偏；p99.9 稳定代表血管结构的高信号水平。
    # 实测最佳区400~450，取p99.9 × 1.05≈438 正落在其中。
    base = float(stats["p999"]) * 1.05
    if percentile is not None:
        base = max(base, float(np.percentile(sample, percentile)))
    if base <= 0:
        base = 1.0

    # 兜底：保留体素不能太少，否则等值面会碎成渣。
    # 0.0008（0.08%）是**从实测最佳区反推**的：阈值 400~450 对应保留率
    # 0.113%~0.084%，取下限留一点余量。原先用 0.0015（0.15%）会把
    # 439 一路乘 0.85 降到 317 —— 落回「含脑组织碎块」区间，
    # 等值面重新变成一坨组织壳。
    thr = base
    for _ in range(8):
        kept = float((finite >= thr).mean())
        if kept >= min_keep_fraction:
            break
        thr *= 0.85

    stats.update({"threshold": thr, "kept_fraction": float((finite >= thr).mean())})
    return thr, stats


def filter_vessel_components(arr, iso, min_voxels=60,
                             keep_ratio=0.02, neighbor_gap=2):
    """MRA 血管等值面的连通域清理，返回过滤后数组副本或 None。

    与 CT 的 ``filter_bone_components`` **刻意不同**，不复用其床体识别：

    - CT 靠「p99 < 600HU」「≥900HU 体素占比」这类密度统计识别床板，
      那是 HU 量纲特有的判据，MR 上毫无意义。
    - MRA 的血管是**细长分叉树状结构**，孤立的小连通域绝大多数是
      血管末梢或噪点，但**不能按尺寸一刀切**：真正的血管分支可能只有
      几十个体素。所以沿用 CT 已验证的**邻接保护**思路——小连通域
      若与主干相距 ≤ neighbor_gap 体素则视为血管末梢予以保留。
    - 不做「半空间切除」：头颈 MRA 的血管充满视野，切边会削掉血管。

    只写被剔除的连通域体素，**背景逐体素保持原值**（同 v1.8.0 的教训：
    改背景会摧毁阈值附近的梯度，等值面被逐体素内缩 → 满屏条纹）。
    """
    from scipy import ndimage
    if arr is None:
        return None
    vessel = arr >= iso
    if not vessel.any():
        return None

    labels, n = ndimage.label(vessel)
    if n <= 0:
        return None
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0

    # 阈值：绝对下限与「最大块的相对比例」取大者，
    # 这样血管分支多的小体数据不会被绝对下限误杀
    thr = float(min_voxels)
    if keep_ratio > 0 and sizes.max() > 0:
        thr = max(thr, keep_ratio * float(sizes.max()))
    keep = sizes >= thr

    if not keep[1:].any():
        # 兜底：没有任何块达到阈值，至少保留最大块作主干。
        # **这里也必须做邻接保护**（初版漏了这一步）：MRA 的血管末梢
        # 常常只有几个体素，远低于 min_voxels，若只保留最大块而跳过邻接
        # 保护，紧贴主干的细分支会被整段删掉—— 树状形态直接断裂。
        keep[int(np.argmax(sizes))] = True
    # 邻接保护：距主干 ≤ gap 的小块是血管末梢，不是噪点。
    # 放在 if/else 之外统一执行，兜底分支同样受益。
    objs = ndimage.find_objects(labels)
    nz, ny, nx = arr.shape
    main_mask = keep[labels]
    g = int(neighbor_gap)
    for cid in np.where(~keep[1:])[0] + 1:
        sl = objs[cid - 1]
        exp = (slice(max(0, sl[0].start - g), min(nz, sl[0].stop + g)),
               slice(max(0, sl[1].start - g), min(ny, sl[1].stop + g)),
               slice(max(0, sl[2].start - g), min(nx, sl[2].stop + g)))
        if main_mask[exp].any():
            keep[cid] = True
    del main_mask

    removed = bool((~keep[1:]).any())
    if not removed:
        return None                             # 无剔除，走快速路径

    cut_val = np.int16(min(int(iso) - 1, 0))
    out = np.array(arr, copy=True)
    rm_mask = keep.copy()
    rm_mask[0] = False                          # 背景恒不改动
    rm_mask[1:] = ~keep[1:]
    out[rm_mask[labels]] = cut_val
    return out


def filter_bone_components(arr, iso, keep_large, remove_bed,
                           min_voxels=300, rel_fraction=0.0,
                           bed_min_voxels=3000, bed_hu_p99=600.0,
                           bed_hu_hot=900.0, bed_hot_fraction=0.002,
                           bed_hu_p99_max=1000.0,
                           debris_gap=6, cut_max_fraction=0.15):
    """对体数据 (nz, ny, nx) 做图像级 3D 连通域过滤，返回过滤后数组副本或 None。

    - keep_large：剔除「孤立」的细小碎屑。小连通域若与任何保留的大块结构
      相距 ≤ debris_gap 体素则视为骨表面噪声壳层予以保留（正是这层壳撑起
      等值面的质感——直接按尺寸剔除会明显破坏骨表面，见 v1.6.2 教训；
      头颅/四肢关节表面的细颗粒纹理也靠它，邻接保护距离必须足够大，
      实测 gap=2 仍会掏掉关节面 dist 2~5 的细节壳层，gap=6 足够保守且
      胸部漂浮碎屑（对比剂血管段等，距骨多在 6 体素以外）清理量基本不变）；
      只有远离骨块的漂浮噪点才被剔除。
    - remove_bed：床体（碳纤维板/塑料垫/U 型托架/检查床+FOV 环融合壳）
      密度远低于骨质，两条件任一命中即判定为床并整块删除：
      ①连通域 HU 的 99 分位 < bed_hu_p99（干净床材）；
      ②高密度体素占比极低：HU ≥ bed_hu_hot 的体素占比 < bed_hot_fraction
        且 p99 < bed_hu_p99_max（混合床体：胸部检查床与 FOV 边界伪影融合的
        大壳实测 p99≈750、≥900 HU 体素 <0.05%，而真骨普遍有 0.5%~70%，
        即使骨质疏松样椎体也有 ~0.5%）。
      位于患者主体 y 范围外侧的床块再向图像外缘**半空间切除**，清掉
      支柱/碎段等残留；单侧切除骨量超过总骨量 cut_max_fraction 时放弃
      该侧切除（防止把患者组织当床切掉）。
      保护：找不到骨性患者主体、或剔除后剩余骨不足总骨量 5% 时放弃。
    无需剔除任何体素时返回 None（调用方可走快速路径）。
    """
    from scipy import ndimage
    nz, ny, nx = arr.shape
    bone = arr >= iso
    if not bone.any():
        return None
    labels, n = ndimage.label(bone)
    # 每 y 行的骨体素数（用于切面保护统计）
    row_counts = bone.sum(axis=(0, 2))
    total_bone = int(row_counts.sum())
    del bone
    if n <= 0:
        return None
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0

    objs = None

    def _get_objs():
        nonlocal objs
        if objs is None:
            objs = ndimage.find_objects(labels)
        return objs

    def _comp_hu_stats(cid):
        """连通域 HU 的 (p99, 高密度体素占比)；逐层取值避免整 bbox 拷贝。"""
        sl = _get_objs()[cid - 1]
        parts = []
        for z in range(sl[0].start, sl[0].stop):
            m = labels[z] == cid
            if m.any():
                parts.append(arr[z][m])
        if not parts:
            return -1000.0, 1.0
        vals = np.concatenate(parts) if len(parts) > 1 else parts[0]
        hot = float((vals >= bed_hu_hot).mean())
        return float(np.percentile(vals, 99)), hot

    if keep_large:
        thr = float(min_voxels)
        if rel_fraction > 0:
            thr = max(thr, rel_fraction * int(sizes.max()))
        keep = sizes >= thr
        if not keep[1:].any():
            keep[int(np.argmax(sizes))] = True    # 兜底：至少保留最大块
        # 邻接保护：与大块结构相距 ≤ debris_gap 体素的小连通域是骨表面
        # 噪声壳层，必须保留；只剔除远离骨块的漂浮碎屑。
        # 与 MR 侧同理，放在 if/else 之外统一执行 —— 兜底分支同样要保护，
        # 否则「整块都不足阈值」时会连紧贴骨面的壳层一起删掉。
        large_mask = keep[labels]
        g = int(debris_gap)
        for cid in np.where(~keep[1:])[0] + 1:
            sl = _get_objs()[cid - 1]
            exp = (slice(max(0, sl[0].start - g), min(nz, sl[0].stop + g)),
                   slice(max(0, sl[1].start - g), min(ny, sl[1].stop + g)),
                   slice(max(0, sl[2].start - g), min(nx, sl[2].stop + g)))
            if large_mask[exp].any():
                keep[cid] = True
        del large_mask
    else:
        keep = np.ones(n + 1, bool)
        keep[0] = False

    def _is_bed_stat(p99, hot_frac):
        """床体统计特征：①p99 低于床材上限；②混合床体（高密度体素占比
        极低且 p99 < bed_hu_p99_max）。"""
        return (p99 < bed_hu_p99
                or (hot_frac < bed_hot_fraction and p99 < bed_hu_p99_max))

    bed_cids = []
    cut_lo = None    # 切除 y < cut_lo（床板位于 y 小的一侧）
    cut_hi = None    # 切除 y >= cut_hi（床板位于 y 大的一侧）
    if remove_bed:
        # 骨性患者主体 = 最大「非床体统计特征」连通域
        # （胸部检查床+FOV 环融合壳 p99≈750、≥900 体素 <0.05%，不会误当主体）
        main_cid = 0
        for cid in np.argsort(sizes)[::-1]:
            if sizes[cid] <= 0 or not keep[cid]:
                continue
            p99, hot_frac = _comp_hu_stats(int(cid))
            if not _is_bed_stat(p99, hot_frac):
                main_cid = int(cid)
                break
        if main_cid:
            msl = _get_objs()[main_cid - 1]
            my0, my1 = msl[1].start, msl[1].stop
            for cid in range(1, n + 1):
                if cid == main_cid or not keep[cid] or sizes[cid] < bed_min_voxels:
                    continue
                p99, hot_frac = _comp_hu_stats(cid)
                if _is_bed_stat(p99, hot_frac):
                    bed_cids.append(cid)
                    sl = objs[cid - 1]
                    y0, y1 = sl[1].start, sl[1].stop
                    if y1 <= my0:
                        cut_lo = y1 if cut_lo is None else max(cut_lo, y1)
                    elif y0 >= my1:
                        cut_hi = y0 if cut_hi is None else min(cut_hi, y0)
        # 半空间切除保护：单侧切除骨量超过 cut_max_fraction 视为误判
        # （环形壳底部/患者组织占比大的区域整列切除会削掉患者）
        if cut_hi is not None and total_bone > 0:
            if int(row_counts[cut_hi:].sum()) > cut_max_fraction * total_bone:
                cut_hi = None
        if cut_lo is not None and total_bone > 0:
            if int(row_counts[:cut_lo].sum()) > cut_max_fraction * total_bone:
                cut_lo = None
        # 总保护：所有去床操作合计后剩余骨不足总骨量 5% 视为误判，整体放弃
        if bed_cids or cut_lo is not None or cut_hi is not None:
            removed_est = sum(int(sizes[c]) for c in bed_cids)
            if cut_hi is not None:
                removed_est += int(row_counts[cut_hi:].sum())
            if cut_lo is not None:
                removed_est += int(row_counts[:cut_lo].sum())
            if total_bone > 0 and (total_bone - removed_est) < 0.05 * total_bone:
                bed_cids = []
                cut_lo = cut_hi = None

    removed_debris = bool((~keep[1:]).any())
    removed_bed = bool(bed_cids) or cut_lo is not None or cut_hi is not None
    if not removed_debris and not removed_bed:
        return None    # 无任何剔除，走快速路径

    # ---- 剔除体素的填充值：只改「被剔除的连通域体素」，绝不改背景 ----
    # 背景（labels==0）承载着阈值附近的 HU 梯度：等值面位置由
    # 「阈值以下体素 ↔ 阈值以上体素」的线性插值决定。若把整个背景砸成
    # 极低值（min(arr)），190 HU 这类「刚好在阈值下方」的体素会变成
    # -1024，插值点从 t=(200-190)/(400-190)=0.05 被推到 t≈0.96，
    # 等值面整体内缩近一个体素；且每个体素的内缩量取决于它原本离
    # 阈值多远，抖动被逐体素放大 —— 渲染出来就是满屏横向条纹（用户
    # 反馈的「看起来都是纹路」），表面连通片数也会被打散成碎片
    # （实测 4286 序列 pieces 332 → 632）。v1.8.0 修复。
    bg = min(int(arr.min()), int(iso) - 1)
    # 阈值下方的安全填充值：保证剔除后不产生新的等值面，又不破坏
    # 周围体素的 HU 梯度（相对阈值而言足够低即可，无需 -1024）
    cut_val = np.int16(min(int(iso) - 1, -1))

    out = np.array(arr, copy=True)
    if removed_debris:
        # debris_mask[cid]：cid=0（背景）恒为 False —— 背景不动
        debris_mask = keep.copy()
        debris_mask[0] = False
        debris_mask[1:] = ~keep[1:]
        rm = debris_mask[labels]
        out[rm] = cut_val
        del rm, debris_mask
    if bed_cids:
        # 床体是「整体删掉」，此处同样只动被判定为床的体素，不碰背景
        bed_mask = np.isin(labels, bed_cids)
        out[bed_mask] = cut_val
        del bed_mask
    del labels
    if cut_lo is not None:
        out[:, :cut_lo, :] = cut_val
    if cut_hi is not None:
        out[:, cut_hi:, :] = cut_val
    return out


class _PipelineWorker(QtCore.QThread):
    """后台线程执行三维管线重计算（等值面提取等），避免冻结 UI。

    vtkFlyingEdges3D 内部本身使用 VTK SMP 多核并行；把整条链的 Update
    移到工作线程后，主线程只保留渲染与交互，低配机器上切三维/调阈值
    时界面保持响应。
    """
    done = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal(str)

    def __init__(self, prepare_fn, parent=None):
        super().__init__(parent)
        self._prepare_fn = prepare_fn

    def run(self):
        try:
            self._prepare_fn()
            self.done.emit()
        except Exception as e:
            self.failed.emit(str(e))


class VTKViewer(QtWidgets.QFrame):
    # 全分辨率体素数超过该阈值时，等值面重计算转后台线程（约 24M 体素 ≈ 100MB）
    ASYNC_VOXEL_LIMIT = 24_000_000
    # 图像级连通域过滤参数（取代原先多边形级“仅保留最大连通域”与“裁剪底部去床板”）
    KEEP_REGION_MIN_VOXELS = 300      # 碎屑剔除尺寸下限（仅剔除远离骨块的孤立碎屑）
    KEEP_REGION_RELATIVE = 0.0        # 相对阈值置 0：按比例剔除会掏空骨小梁（质量回退教训）
    BED_MIN_VOXELS = 3000             # 床体判定：连通域 ≥ 3000 体素才参与
    BED_HU_P99 = 600.0                # 床体判定①：HU 99 分位 < 600（干净床材）
    BED_HU_HOT = 900.0                # 床体判定②：≥900 HU 的体素视为骨性证据
    BED_HOT_FRACTION = 0.002          # 床体判定②：骨性证据占比 <0.2%（真骨 ≥0.5%）
    BED_HU_P99_MAX = 1000.0           # 床体判定②：且 p99 < 1000（混合床体/FOV 环壳）
    CUT_MAX_FRACTION = 0.15           # 半空间切除保护：单侧切除骨量超 15% 放弃该侧
    MAX_FILTER_VOXELS = 300_000_000   # 超过此体素数跳过连通域过滤（防内存溢出）
    # MR 血管三维参数（信号强度语义，不复用 CT 的 HU 判据）
    VESSEL_MIN_VOXELS = 60            # 血管末梢尺寸下限（小块靠邻接保护保留）
    VESSEL_KEEP_RATIO = 0.05          # 同时要求 ≥ 主干体积的 5%，避免绝对阈值误杀
    VESSEL_NEIGHBOR_GAP = 2           # 距主干 ≤2 体素视为血管末梢
    # 骨折线增强（独立叠加层，不改骨面着色）
    FRACTURE_VOID_HU = 150.0            # 缝内（液体/空气）低于此值
    FRACTURE_SURROUND_HU = 800.0        # 缝两侧必须是致密骨，高于此值
    FRACTURE_SHEET_DIST = 3             # 沿轴采样距离（体素）
    FRACTURE_MIN_COMPONENT_FACES = 60   # 连通块小于此面数视为噪点丢弃
    FRACTURE_MAX_VOXELS = 150_000_000   # 超过此体素数跳过增强（内存保护）
    FRACTURE_OFFSET_VOXEL = 0.8         # 叠加层沿骨外微移（体素），防 z-fighting
    FRACTURE_LINE_COLOR = (0.10, 0.06, 0.05)   # 暗褐近黑，像"裂缝"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(600)
        self._colors = vtkNamedColors()
        self._iso_value = 300
        # 去碎屑默认开启：只剔除「远离骨块」的漂浮碎屑，骨表面壳层受邻接保护
        self._keep_largest_region = True
        self._reader = None
        self._pipeline_worker = None
        self._is_mr = False          # MR 语义：信号强度而非 HU（走血管三维）
        self._masked_image = None    # 连通域过滤后的体数据（去碎屑/去床板）
        self._mask_state = None      # 生成掩膜时的参数快照 (iso, keep, bed, reader)
        self._mapper_source = None   # mapper 的全分辨率输入源（normals 或 probe）
        self._iso = None
        self._connectivity = None
        self._normals = None
        self._mapper = None
        self._actor = None
        self._color_mode = "sample_bone"
        self._lut = None
        self._probe = None
        self._enhance_fractures = True   # 骨折增强：骨内低密度薄片 → 暗色叠加层
        self._fracture_actor = None      # 骨折线叠加层 actor（独立于骨面 actor）
        self._fracture_poly = None       # 骨折线叠加层网格（计算结果）
        
        # 光标相关变量
        self._cursor_source = None
        self._visual_cursor_actor = None
        self._interaction_cursor_actor = None
        self._is_dragging_cursor = False
        self._detection_actors = []
        self._segmentation_actors = []
        
        self._flip_filter = None
        self._preview_enabled = False
        self._remove_bed_enabled = True
        # MIP/MinIP：surface=原三维表面，mip/minip=投影
        self._projection_mode = PROJECTION_SURFACE
        # 投影的**实现方式**：volume=三维体渲染（可旋转，默认尝试）；
        # slice=二维投影图（体渲染不可用时的自动回退）。
        self._projection_impl = "volume"
        self._projection_actor = None   # 二维投影图 actor（vtkImageSlice）
        self._projection_image = None   # 投影图对应的 vtkImageData
        self._projection_stride = 1     # 实际使用的层抽稀步长（UI 提示用）
        self._volume_actor = None       # 三维体渲染 actor（vtkVolume）
        self._shrink_factor = 2
        self._shrink = None
        
        self._vtk_widget = QVTKRenderWindowInteractor(self)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.addWidget(self._vtk_widget)

        self._renderer = vtkRenderer()
        # 设置更专业的深色背景
        self._renderer.SetBackground(0.1, 0.12, 0.14)
        self._renderer.SetBackground2(0.02, 0.02, 0.04)
        self._renderer.SetGradientBackground(True)
        self._vtk_widget.GetRenderWindow().AddRenderer(self._renderer)
        
        # 配置双层渲染以实现光标置顶
        self._vtk_widget.GetRenderWindow().SetNumberOfLayers(2)
        self._renderer.SetLayer(0)
        
        # 创建 Overlay Renderer (Layer 1)
        self._overlay_renderer = vtkRenderer()
        self._overlay_renderer.SetLayer(1)
        self._overlay_renderer.SetInteractive(0)
        self._overlay_renderer.PreserveColorBufferOn()  # 关键：保留底层颜色
        self._overlay_renderer.SetActiveCamera(self._renderer.GetActiveCamera()) # 共享相机
        self._vtk_widget.GetRenderWindow().AddRenderer(self._overlay_renderer)
        
        self._interactor = self._vtk_widget.GetRenderWindow().GetInteractor()
        self._interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
        self._interactor_initialized = False
        
        self._cursor_synchronizer = None
        self._picker = vtkPointPicker()
        self._picker.PickFromListOn()
        self._last_click_pos = None
        
        self._interactor.AddObserver("LeftButtonPressEvent", self._on_left_down, 1.0)
        self._interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_up, 1.0)
        self._interactor.AddObserver("RightButtonPressEvent", self._on_right_down, 1.0)
        self._interactor.AddObserver("RightButtonReleaseEvent", self._on_right_up, 1.0)
        self._interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 1.0)
        self._fullscreen_expanded = False

        # 工具栏容器（v1.15.0）：底部一排工具按钮统一放进一个 QWidget，
        # 便于整体显隐与定位 —— 原先是逐个 move() 摆位，按钮一多就出错位。
        self._tool_bar = QtWidgets.QWidget(self)
        self._tool_bar_layout = QtWidgets.QHBoxLayout(self._tool_bar)
        self._tool_bar_layout.setContentsMargins(0, 0, 0, 0)
        self._tool_bar_layout.setSpacing(4)
        theme.bind(self._tool_bar, tool_bar_qss)

        # 鼠标模式与显示极性（v1.15.0 新增，放在工具栏最前）
        self._select_mode_3d = False      # 选择模式：左键拖动 = 平移而非旋转
        self._select_panning_3d = False
        self._select_last_3d = None
        self._invert_3d = False           # 反色：模型/背景明暗反相
        self._select_btn = self._add_tool_button(
            "select", "选择",
            "选择：开启后鼠标左键拖动为平移视图（不旋转、不圈画、不取点），"
            "便于挪动观察位置；再点一次退出",
            checkable=True)
        self._select_btn.toggled.connect(self._on_select3d_toggled)
        self._invert_btn = self._add_tool_button(
            "invert", "反色",
            "反色：把三维模型与背景的明暗反相显示（只影响显示，不改数据）",
            checkable=True)
        self._invert_btn.toggled.connect(self._on_invert3d_toggled)

        # 添加“左” “右” 汉字标签
        self._left_char_label = QtWidgets.QLabel("右", self)  # 屏幕左侧显示“右”
        self._right_char_label = QtWidgets.QLabel("左", self) # 屏幕右侧显示“左”
        
        for _lbl in (self._left_char_label, self._right_char_label):
            theme.bind(_lbl, lambda: theme.qss_char_label("24pt"))
        self._left_char_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._right_char_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._left_char_label.hide()
        self._right_char_label.hide()
        # 多视角批量导出按钮（三维模型一次性导出前/后/左/右/上/下六视角 PNG）
        self._save_mview_btn = self._add_tool_button(
            "multiview", "多视角",
            "保存多视角：把当前三维模型按 前位/后位/左侧位/右侧位/上(顶)位/下(底)位 "
            "六个视角一次性导出为 PNG 图片")
        self._save_mview_btn.clicked.connect(self._on_save_multiview_clicked)

        # 截图按钮（保存用户手动旋转后的当前视角单张图片，所见即所得）
        self._screenshot_btn = self._add_tool_button(
            "camera", "截图",
            "截图：保存当前三维视角（手动旋转后的角度）为单张 PNG 图片")
        self._screenshot_btn.clicked.connect(self._on_screenshot_clicked)

        # 切割（圈画去除）相关状态
        self._cut_mode = False            # 是否处于切割模式（左键画圈而非旋转）
        self._cut_drawing = False         # 当前是否正在画圈
        self._cut_screen_pts = []         # 画圈过程中累积的屏幕坐标点
        self._cut_loop_actor = None       # 画圈过程的实时预览折线（overlay 层）
        self._cut_loop_mapper = None
        # 画圈预览的渲染降频器：鼠标每移动一次都全场景重绘会让重网格卡成幻灯片，
        # 这里把重绘合并到定时器里（每 60ms 最多一次），大幅减少渲染次数。
        self._cut_preview_timer = QtCore.QTimer(self)
        self._cut_preview_timer.setSingleShot(True)
        self._cut_preview_timer.setInterval(60)
        self._cut_preview_timer.timeout.connect(self._update_cut_loop_preview)
        self._current_polydata = None     # 当前可编辑的网格（含历次切割结果）
        self._original_polydata = None    # 首次切割前保存的完整网格（用于重置）
        self._cut_history = []           # 撤销栈：每次切割前的网格快照

        # 三维长度测量（在三维模型上点两点，量两点间距离，单位 mm）
        self._measure_mode_3d = False     # 是否处于三维测量模式
        self._measure_pts_3d = []         # 当前正在放置的点（世界坐标，最多 2 个）
        self._measure_items_3d = []       # 已完成的测量 [{"p1","p2","mm"}]
        self._measure_actors_3d = []      # overlay 层承载的测量 actor

        # 切割按钮（切换模式）
        self._cut_btn = self._add_tool_button(
            "cut", "切割",
            "切割：进入后用鼠标左键在三维图上圈画要去掉的部分，松开即删除该区域；"
            "再点一次退出（恢复旋转）。可多次圈画，配合撤销/重置使用。",
            checkable=True, color=ui_icons.ICON_WARN)
        self._cut_btn.clicked.connect(self._on_cut_toggled)
        # 撤销按钮
        self._cut_undo_btn = self._add_tool_button(
            "undo", "撤销", "撤销：撤销上一次切割", enabled=False)
        self._cut_undo_btn.clicked.connect(self._on_cut_undo)
        # 重置按钮
        self._cut_reset_btn = self._add_tool_button(
            "reset", "重置", "重置：恢复切割前的完整三维模型", enabled=False)
        self._cut_reset_btn.clicked.connect(self._on_cut_reset)

        # 三维长度测量按钮（切换模式）
        self._measure3d_btn = self._add_tool_button(
            "measure3d", "测长",
            "三维测长：进入后依次点击模型上的两个点，"
            "两点间距离（mm）会标在连线中点；再点一次退出测量模式。",
            checkable=True)
        self._measure3d_btn.clicked.connect(self._on_measure3d_toggled)
        # 三维测量清除按钮
        self._measure3d_clear_btn = self._add_tool_button(
            "measure_clear", "清测量",
            "清测量：清除三维窗口中所有的长度测量", enabled=False)
        self._measure3d_clear_btn.clicked.connect(self._clear_measure3d)

        # 全屏（原先单独浮在左下角，v1.15.0 收进工具栏）
        self._fullscreen_btn = self._add_tool_button(
            "maximize", "全屏", "全屏：把本视图最大化，再点一次还原")
        self._fullscreen_btn.clicked.connect(self._on_fullscreen_clicked)

        # 收起工具栏
        self._toolbar_hide_btn = self._add_tool_button(
            "eye_off", "隐藏",
            "隐藏：收起本工具栏（左下角保留「工具」按钮可再次展开）")
        self._toolbar_hide_btn.clicked.connect(
            lambda: self._set_toolbar_visible(False))
        # 收起后用来重新展开的小按钮
        self._toolbar_show_btn = QtWidgets.QToolButton(self)
        self._toolbar_show_btn.setFixedHeight(TOOL_BTN_H)
        theme.bind(self._toolbar_show_btn, tool_btn_qss)
        ui_icons.apply_tool_button(self._toolbar_show_btn, "tools", "工具",
                                   "工具：重新展开工具栏", size=20, icon_size=18)
        self._toolbar_show_btn.setVisible(False)
        self._toolbar_show_btn.clicked.connect(
            lambda: self._set_toolbar_visible(True))

        self._orientation_visible = True   # 由 QuadViewer.set_orientation_visible 统一管控（2D 关闭）

    def set_cursor_synchronizer(self, synchronizer):
        self._cursor_synchronizer = synchronizer

    # ---------- 工具栏（v1.15.0） ----------
    def _add_tool_button(self, name, text, tip, checkable=False,
                         color=None, enabled=True):
        """往底部工具栏追加一个「图标 + 汉字」按钮。

        统一走这里可以保证：高度/样式一致、顺序即布局顺序、
        不会再出现漏 addWidget 导致按钮「创建了却看不见」。
        """
        btn = QtWidgets.QToolButton(self._tool_bar)
        btn.setCheckable(checkable)
        btn.setFixedHeight(TOOL_BTN_H)
        btn.setEnabled(bool(enabled))
        ui_icons.apply_tool_button(btn, name, text, tip, size=20, icon_size=18,
                                   color=(color or ui_icons.ICON_COLOR))
        self._tool_bar_layout.addWidget(btn)
        return btn

    def _on_left_down(self, obj, event):
        pos = obj.GetEventPosition()
        self._last_click_pos = pos
        # 选择模式：左键拖动 = 平移视图（优先于切割，两者互斥）
        if getattr(self, "_select_mode_3d", False):
            self._select_panning_3d = True
            self._select_last_3d = (pos[0], pos[1])
            try:
                self._vtk_widget.setCursor(QtCore.Qt.ClosedHandCursor)
            except Exception:        # noqa: BLE001
                pass
            return
        # 切割模式下：左键用于画圈，不旋转相机
        if self._cut_mode:
            self._cut_drawing = True
            self._cut_screen_pts = [(pos[0], pos[1])]
            self._clear_cut_loop_actor()
            return
        # 三维测量模式：左键仍然可以拖动旋转（走默认 Trackball 行为），
        # 只有「按下→抬起且几乎没移动」才视为取点，见 _on_left_up。
        # 不像切割那样切成 User 样式 —— 测量时用户需要反复转视角看不同部位。

    def _on_right_down(self, obj, event):
        pos = obj.GetEventPosition()
        
        # 切割模式下不处理光标拖动（避免误触破坏切割模式）
        if self._cut_mode:
            return
        # 三维测量模式下同样不处理光标拖动（避免误触破坏测量）
        if self._measure_mode_3d:
            return

        # 1. 检测是否点击了交互光标（仅在右键时触发）
        if self._interaction_cursor_actor:
            # 使用 CellPicker 提高拾取准确率
            picker = vtk.vtkCellPicker()
            picker.SetTolerance(0.005)
            picker.PickFromListOn()
            picker.AddPickList(self._interaction_cursor_actor)
            picker.Pick(pos[0], pos[1], 0, self._overlay_renderer)
            
            if picker.GetActor() == self._interaction_cursor_actor:
                self._is_dragging_cursor = True
                if _DEBUG_LOG:
                    print("DEBUG: 右键开始拖动3D光标")
                
                # 禁用右键默认的缩放功能，防止冲突
                self._interactor.GetInteractorStyle().OnRightButtonDown = lambda: None
                return

    def _on_mouse_move(self, obj, event):
        # 选择模式：左键拖动平移相机
        if getattr(self, "_select_panning_3d", False):
            pos = obj.GetEventPosition()
            last = getattr(self, "_select_last_3d", None)
            if last is not None:
                self._pan_camera_3d(pos[0] - last[0], pos[1] - last[1])
            self._select_last_3d = (pos[0], pos[1])
            return
        if self._cut_mode and self._cut_drawing:
            pos = obj.GetEventPosition()
            # 节流：与上一点距离过近则跳过，避免点过密
            if self._cut_screen_pts:
                lx, ly = self._cut_screen_pts[-1]
                if (pos[0] - lx) ** 2 + (pos[1] - ly) ** 2 < 9.0:
                    return
            self._cut_screen_pts.append((pos[0], pos[1]))
            # 只累积点，真正的重绘交给定时器降频执行
            if not self._cut_preview_timer.isActive():
                self._cut_preview_timer.start()
            return
        if self._is_dragging_cursor:
            pos = obj.GetEventPosition()
            # 在模型上拾取新位置（使用主渲染器）
            picker = vtkPointPicker()
            picker.PickFromListOn()
            if self._actor:
                picker.AddPickList(self._actor)
            
            if picker.Pick(pos[0], pos[1], 0, self._renderer):
                world_pos = picker.GetPickPosition()
                self.set_cursor_position(world_pos)
                if self._cursor_synchronizer:
                    self._cursor_synchronizer.set_world_position(world_pos, "3d")

    def _on_right_up(self, obj, event):
        # 恢复右键默认行为（切割模式下不恢复，否则会顶掉用于画圈的 User 样式）
        if self._is_dragging_cursor and not self._cut_mode:
             self._interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
             
        if self._is_dragging_cursor:
            self._is_dragging_cursor = False
            if _DEBUG_LOG:
                print("DEBUG: 右键停止拖动3D光标")
            return

    def _on_left_up(self, obj, event):
        pos = obj.GetEventPosition()
        # 选择模式：结束拖动，光标回到「可抓取」状态
        if getattr(self, "_select_panning_3d", False):
            self._select_panning_3d = False
            self._select_last_3d = None
            try:
                self._vtk_widget.setCursor(QtCore.Qt.OpenHandCursor)
            except Exception:        # noqa: BLE001
                pass
            return
        # 切割模式下：结束画圈并执行切割
        if self._cut_mode and self._cut_drawing:
            self._cut_drawing = False
            self._clear_cut_loop_actor()
            if len(self._cut_screen_pts) >= 3:
                try:
                    self._apply_cut()
                except Exception as exc:
                    QtWidgets.QMessageBox.critical(
                        self, "切割失败", "执行切割出错：\n%s" % exc)
            else:
                self._cut_screen_pts = []
            return
        if self._last_click_pos:
            # 计算移动距离平方
            dist = (pos[0]-self._last_click_pos[0])**2 + (pos[1]-self._last_click_pos[1])**2
            if dist < 4.0: # 移动很小，认为是点击
                if self._measure_mode_3d:
                    # 三维测量模式：这次点击用于在模型上取点
                    self._on_measure3d_click(pos)
                else:
                    self._pick_position(pos)
        self._last_click_pos = None

    # ------------------------------------------------------------------
    # 三维长度测量
    #
    # 交互：进入测量模式后，在模型上点击两次取两个点，两点间距离（mm）
    # 标在连线中点；可继续点下一组（再点两下）。按住左键拖动仍可旋转
    # 视角（按下→抬起位移很小才算「点击」）。
    #
    # 长度怎么算：三维世界的坐标单位来自 vtkDICOMImageReader 读出的
    # 体数据 spacing（DICOM 里就是毫米），所以两点世界坐标的欧氏距离
    # 直接就是毫米值。非 DICOM 序列（PNG/JPG 堆叠）spacing 是 1.0，
    # 这时读数没有物理意义 —— 用 _world_scale_mm() 把「世界单位」
    # 换算成毫米；换不出来就不显示毫米、只显示像素数。
    # ------------------------------------------------------------------
    def _world_scale_mm(self):
        """返回 1 个世界单位等于多少毫米；无法确定时返回 None。

        DICOM 读出来的体数据 spacing 本身就是毫米（PixelSpacing），
        世界坐标即毫米 → 比例 1.0。图片序列（ImageSeriesReader）没有
        物理尺寸，spacing 恒为 1.0 → 返回 None（只能报像素）。
        """
        reader = self._reader
        if reader is None:
            return None
        try:
            spacing = reader.GetOutput().GetSpacing()
        except Exception:            # noqa: BLE001
            return None
        try:
            # DICOM 的 PixelSpacing 已经是毫米；用 extent 推算不出更可靠的
            # 比例，所以这里只判断「是否有真实物理尺寸」：
            # ImageSeriesReader / ImageFileReader 的 spacing 恒为 1.0。
            if all(abs(float(s) - 1.0) < 1e-6 for s in spacing):
                # 可能是图片序列（无物理尺寸），也可能是 DICOM 恰好 1mm
                # 的等体素数据 —— 后者仍按毫米处理（1.0mm/单位 → 比例 1.0）
                if not self._reader_has_dicom_geometry():
                    return None
            return 1.0
        except Exception:            # noqa: BLE001
            return None

    def _reader_has_dicom_geometry(self):
        """判断当前体数据是否带有 DICOM 的物理尺寸信息。"""
        reader = self._reader
        if reader is None:
            return False
        # vtkDICOMImageReader 之外的都是本项目自研的读图器（PNG/JPG），
        # 它们没有 PixelSpacing。
        return type(reader).__name__ == "vtkDICOMImageReader"

    def _on_measure3d_toggled(self, checked):
        """进入/退出三维测量模式。"""
        if checked:
            if self._actor is None or not self._actor.GetVisibility():
                self._measure3d_btn.setChecked(False)
                QtWidgets.QMessageBox.information(
                    self, "提示", "当前没有三维模型，请先完成三维重建。")
                return
            # 测量与切割互斥：进测量前先退出切割
            if self._cut_mode:
                self._exit_cut_mode()
            self._measure_mode_3d = True
            self._measure_pts_3d = []
        else:
            self._measure_mode_3d = False
            self._measure_pts_3d = []
        self._rebuild_measure3d_actors()

    def _on_measure3d_click(self, pos):
        """在模型上取一个测量点（世界坐标）。"""
        if self._actor is None:
            return
        # 只在模型本体上取点：点空白处不产生测量点
        picker = vtkPointPicker()
        picker.SetTolerance(0.005)
        picker.PickFromListOn()
        picker.AddPickList(self._actor)
        if not picker.Pick(pos[0], pos[1], 0, self._renderer):
            return
        world = picker.GetPickPosition()
        self._measure_pts_3d.append((float(world[0]), float(world[1]),
                                     float(world[2])))
        if len(self._measure_pts_3d) >= 2:
            p1, p2 = self._measure_pts_3d[0], self._measure_pts_3d[1]
            self._measure_pts_3d = []
            scale = self._world_scale_mm()
            mm = math.sqrt(sum((p2[i] - p1[i]) ** 2 for i in range(3)))
            self._measure_items_3d.append({
                "p1": p1, "p2": p2,
                "mm": (mm * scale) if scale else None,
                "units": mm,
            })
            self._measure3d_clear_btn.setEnabled(True)
        self._rebuild_measure3d_actors()

    def _clear_measure3d(self):
        """清除三维窗口的全部长度测量。"""
        self._measure_pts_3d = []
        self._measure_items_3d = []
        self._measure3d_clear_btn.setEnabled(False)
        self._rebuild_measure3d_actors()

    def _clear_measure3d_actors(self):
        for a in self._measure_actors_3d:
            try:
                self._overlay_renderer.RemoveActor(a)
            except Exception:        # noqa: BLE001
                pass
        self._measure_actors_3d = []

    def _mk_measure3d_line_actor(self, p1, p2, color_rgb, width=2.0):
        """三维测量的连线（世界坐标，随模型一起转）。"""
        pd = vtkPolyData()
        pts = vtkPoints()
        pts.InsertNextPoint(p1[0], p1[1], p1[2])
        pts.InsertNextPoint(p2[0], p2[1], p2[2])
        pd.SetPoints(pts)
        ca = vtkCellArray()
        ca.InsertNextCell(2)
        ca.InsertCellPoint(0)
        ca.InsertCellPoint(1)
        pd.SetLines(ca)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(pd)
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(color_rgb[0], color_rgb[1], color_rgb[2])
        prop.SetLineWidth(width)
        prop.LightingOff()
        return actor

    def _mk_measure3d_text_actor(self, text, world_pos, color_rgb):
        """三维测量标签：始终正对相机，跟随模型旋转。"""
        actor = vtkBillboardTextActor3D()
        actor.SetInput(str(text))
        actor.SetPosition(float(world_pos[0]), float(world_pos[1]),
                          float(world_pos[2]))
        tp = actor.GetTextProperty()
        tp.SetColor(color_rgb[0], color_rgb[1], color_rgb[2])
        tp.SetFontSize(16)
        tp.SetBold(True)
        # 描边：让文字在任何底色上都看得清
        # （vtkTextProperty 没有 SetOutline/SetShadowColor，
        #  这里只用 SetShadow + 偏移做深色衬底的近似效果）
        tp.SetShadow(1)
        tp.SetShadowOffset(1, 1)
        return actor

    def _add_measure3d_actor(self, actor):
        """把测量 actor 加到 overlay 层（永远画在模型之上，不被骨面遮挡）。"""
        if actor is None:
            return
        try:
            self._overlay_renderer.AddActor(actor)
        except Exception:            # noqa: BLE001
            return
        self._measure_actors_3d.append(actor)

    def _rebuild_measure3d_actors(self):
        """重建三维测量的线与标签。"""
        self._clear_measure3d_actors()
        color = (0.0, 0.898, 1.0)      # #00E5FF，与二维测量同一色系
        for it in self._measure_items_3d:
            p1, p2 = it["p1"], it["p2"]
            self._add_measure3d_actor(
                self._mk_measure3d_line_actor(p1, p2, color))
            mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0,
                   (p1[2] + p2[2]) / 2.0)
            if it.get("mm") is not None:
                text = "%.1f mm" % it["mm"]
            else:
                # 无物理尺寸（图片序列）：只能给像素数，明确标注
                text = "%.1f px" % it["units"]
            self._add_measure3d_actor(
                self._mk_measure3d_text_actor(text, mid, color))
        # 进行中的第一个点：画个小十字提示已落点
        for w in self._measure_pts_3d:
            self._add_measure3d_actor(
                self._mk_measure3d_line_actor(
                    (w[0] - 6, w[1], w[2]), (w[0] + 6, w[1], w[2]), color,
                    width=2.0))
            self._add_measure3d_actor(
                self._mk_measure3d_line_actor(
                    (w[0], w[1] - 6, w[2]), (w[0], w[1] + 6, w[2]), color,
                    width=2.0))
        try:
            self._vtk_widget.GetRenderWindow().Render()
        except Exception:            # noqa: BLE001
            pass

    def _pick_position(self, pos):
        # 优先在 Overlay 层拾取交互光标（用于点击选中）
        self._picker.Pick(pos[0], pos[1], 0, self._overlay_renderer)
        if self._picker.GetActor() == self._interaction_cursor_actor:
            # 如果点到了光标，不做处理（或者可以做一些高亮反馈）
            return

        # 否则在底层拾取模型位置
        if not self._picker.Pick(pos[0], pos[1], 0, self._renderer):
            return
        
        world_pos = self._picker.GetPickPosition()
        self.set_cursor_position(world_pos)
        
        if self._cursor_synchronizer:
            self._cursor_synchronizer.set_world_position(world_pos, "3d")

    def showEvent(self, event):
        super().showEvent(event)
        if not self._interactor_initialized:
            self._interactor.Initialize()
            self._interactor_initialized = True
        self._position_toolbar()
        bar = getattr(self, "_tool_bar", None)
        if bar is not None:
            bar.raise_()

    def set_shrink_factor(self, factor: int):
        self._shrink_factor = int(factor)
        if self._shrink is not None:
            self._shrink.SetShrinkFactors(self._shrink_factor, self._shrink_factor, self._shrink_factor)

    def set_iso_value(self, value: int):
        self._iso_value = int(value)
        if self._iso is None or self._reader is None:
            return
        self._iso.SetValue(0, self._iso_value)
        # 连通域掩膜（去碎屑/去床板）依赖阈值：非预览下阈值变化需重算掩膜再渲染
        if ((self._keep_largest_region or self._remove_bed_enabled)
                and not self._preview_enabled):
            if self._is_heavy_volume():
                self._render_pipeline_async(full_setup=False)
                return
            self._prepare_and_update()   # 小体数据：同步重算（快）
            self._sync_fracture_actor()
        elif not self._preview_enabled and self._enhance_fractures:
            # 没开连通域过滤时不会走 _prepare_and_update，但等值面还是变了，
            # 骨折线叠加层要跟着重算，否则线会飘在旧表面上。
            try:
                self._fracture_poly = self._detect_fracture_poly()
                self._sync_fracture_actor()
            except Exception:            # noqa: BLE001
                pass
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_toolbar()
        self._position_labels()

    def _position_toolbar(self):
        """把底部工具栏整体摆到左下角。

        v1.15.0：按钮全部收进 ``_tool_bar``，这里只需摆一个控件；
        原先逐个 ``move()`` 的写法在按钮变多后非常容易错位。
        """
        bar = getattr(self, "_tool_bar", None)
        show_btn = getattr(self, "_toolbar_show_btn", None)
        m = 6
        if show_btn is not None:
            if show_btn.isVisible():
                show_btn.adjustSize()
                show_btn.move(m, self.height() - show_btn.height() - m)
                show_btn.raise_()
        if bar is None:
            return
        bar.adjustSize()
        y = max(0, self.height() - bar.height() - m)
        x = m
        if show_btn is not None and show_btn.isVisible():
            x = m + show_btn.width() + 6
        bar.move(x, y)
        bar.raise_()

    def _set_toolbar_visible(self, visible: bool):
        """显示/隐藏三维工具栏，并保留一个「工具」按钮用来恢复。"""
        bar = getattr(self, "_tool_bar", None)
        show_btn = getattr(self, "_toolbar_show_btn", None)
        if bar is None:
            return
        bar.setVisible(bool(visible))
        if show_btn is not None:
            show_btn.setVisible(not bool(visible))
        self._position_toolbar()

    def _on_fullscreen_clicked(self):
        parent = self.parent()
        while parent is not None and not isinstance(parent, QuadViewer):
            parent = parent.parent()
        if parent is not None:
            parent.toggle_maximize(self)

    # ---------- 选择模式（拖拽平移） ----------
    def _on_select3d_toggled(self, checked):
        """选择模式：左键拖动改为平移；与切割/测长互斥。

        与切割模式同一套做法：切成 ``vtkInteractorStyleUser``，
        否则 Trackball 仍会把左键拖动解释为旋转，平移根本走不到。
        """
        self._select_mode_3d = bool(checked)
        self._select_panning_3d = False
        self._select_last_3d = None
        if checked:
            for btn in (getattr(self, "_cut_btn", None),
                        getattr(self, "_measure3d_btn", None)):
                if btn is not None and btn.isChecked():
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.blockSignals(False)
            try:
                self._interactor.SetInteractorStyle(vtkInteractorStyleUser())
            except Exception:    # noqa: BLE001
                pass
            self._select_btn.setToolTip(
                "选择中：左键拖动可平移视图（不旋转）。"
                "点击本按钮退出选择模式。")
        else:
            try:
                self._interactor.SetInteractorStyle(
                    vtkInteractorStyleTrackballCamera())
            except Exception:    # noqa: BLE001
                pass
            self._select_btn.setToolTip(
                "选择：开启后鼠标左键拖动为平移视图（不旋转、不圈画、不取点），"
                "便于挪动观察位置；再点一次退出")
        try:
            self._vtk_widget.setCursor(
                QtCore.Qt.OpenHandCursor if checked else QtCore.Qt.ArrowCursor)
        except Exception:        # noqa: BLE001
            pass

    def _pan_camera_3d(self, dx, dy):
        """把相机平移 (dx, dy) 个像素（选择模式下左键拖动用）。

        Trackball 默认**中键**平移、左键旋转；选择模式下把左键改成平移。
        这里用相机的基向量直接算位移，而不是走
        ``renderer.WorldToDisplay / DisplayToWorld`` —— 后者依赖渲染器已有
        真实尺寸（没渲染过或尺寸为 0 时换算结果恒为 0，平移会完全失效）。

        方向遵循 VTK 的 Pan 约定：位移量取**光标的反方向**，
        于是模型跟着光标走（拖动 = 抓住画面）。
        """
        renderer = getattr(self, "_renderer", None)
        if renderer is None:
            return
        camera = renderer.GetActiveCamera()
        if camera is None:
            return

        # 视口高度（像素）：渲染器拿不到就退回 VTK 控件高度
        h = 0
        try:
            sz = renderer.GetSize()
            if sz and len(sz) > 1:
                h = int(sz[1])
        except Exception:        # noqa: BLE001
            h = 0
        if h <= 0:
            try:
                h = int(self._vtk_widget.height())
            except Exception:    # noqa: BLE001
                h = 0
        if h <= 0:
            return

        try:
            pos = camera.GetPosition()
            fp = camera.GetFocalPoint()
            dop = (fp[0] - pos[0], fp[1] - pos[1], fp[2] - pos[2])
            dist = math.sqrt(dop[0] ** 2 + dop[1] ** 2 + dop[2] ** 2)
            if dist < 1e-9:
                dist = 1.0
            dop = (dop[0] / dist, dop[1] / dist, dop[2] / dist)

            # 屏幕右方向 r = dop × up，再把 up 正交化
            up = camera.GetViewUp()
            r = (dop[1] * up[2] - dop[2] * up[1],
                 dop[2] * up[0] - dop[0] * up[2],
                 dop[0] * up[1] - dop[1] * up[0])
            rn = math.sqrt(r[0] ** 2 + r[1] ** 2 + r[2] ** 2)
            if rn < 1e-9:
                return
            r = (r[0] / rn, r[1] / rn, r[2] / rn)
            u = (r[1] * dop[2] - r[2] * dop[1],
                 r[2] * dop[0] - r[0] * dop[2],
                 r[0] * dop[1] - r[1] * dop[0])

            # 焦点平面处「每像素对应多少世界单位」
            half_h = dist * math.tan(math.radians(float(camera.GetViewAngle())) / 2.0)
            wpp = 2.0 * half_h / float(h)

            off = (-(dx * wpp) * r[0] - (dy * wpp) * u[0],
                   -(dx * wpp) * r[1] - (dy * wpp) * u[1],
                   -(dx * wpp) * r[2] - (dy * wpp) * u[2])
            camera.SetFocalPoint(fp[0] + off[0], fp[1] + off[1], fp[2] + off[2])
            camera.SetPosition(pos[0] + off[0], pos[1] + off[1], pos[2] + off[2])
            renderer.ResetCameraClippingRange()
            self._render()
        except Exception:        # noqa: BLE001
            pass

    # ---------- 反色 ----------
    def _on_invert3d_toggled(self, checked):
        """三维反色：背景与模型明暗对调（只影响显示）。"""
        self._invert_3d = bool(checked)
        self._apply_3d_invert()

    def _apply_3d_invert(self):
        renderer = getattr(self, "_renderer", None)
        if renderer is None:
            return
        inverted = bool(getattr(self, "_invert_3d", False))
        try:
            if inverted:
                renderer.SetBackground(0.92, 0.93, 0.94)   # 浅底
            else:
                renderer.SetBackground(0.10, 0.11, 0.12)   # 深底（原默认）
        except Exception:        # noqa: BLE001
            pass
        # 模型颜色跟着换，否则浅底上白骨会看不见
        actor = getattr(self, "_actor", None)
        if actor is not None:
            try:
                prop = actor.GetProperty()
                if inverted:
                    prop.SetColor(0.16, 0.15, 0.14)        # 深骨
                else:
                    prop.SetColor(0.90, 0.88, 0.84)        # 浅骨（原默认）
            except Exception:    # noqa: BLE001
                pass
        self._render()

    def _position_labels(self):
        m = 20
        # "右" 在屏幕左侧
        self._left_char_label.adjustSize()
        self._left_char_label.move(m, (self.height() - self._left_char_label.height()) // 2)

        # "左" 在屏幕右侧
        self._right_char_label.adjustSize()
        self._right_char_label.move(self.width() - self._right_char_label.width() - m, (self.height() - self._right_char_label.height()) // 2)

        # 仅在本视图真正可见、且三维方位字开关打开时才显示方位字
        # （VTKViewer 属于 QuadViewer，处于隐藏的 _quad_host 内时
        # isVisible() 为 False；切到二维时 QuadViewer.set_orientation_visible(False)
        # 会把 _orientation_visible 置 False，双保险杜绝漏到二维阅读模式）。
        if self.isVisible() and getattr(self, "_orientation_visible", True):
            self._left_char_label.show()
            self._right_char_label.show()
        self._left_char_label.raise_()
        self._right_char_label.raise_()

    def set_fullscreen_button(self, expanded: bool):
        """外部（QuadViewer 最大化切换）同步按钮的按下态。

        图标不再随状态切换（换图标容易让人以为是另一个按钮），
        统一用「按下高亮 + tooltip」表达当前是否已最大化。
        """
        self._fullscreen_expanded = bool(expanded)
        btn = getattr(self, "_fullscreen_btn", None)
        if btn is None:
            return
        btn.blockSignals(True)
        btn.setChecked(bool(expanded))
        btn.blockSignals(False)
        btn.setToolTip("退出最大化" if expanded
                       else "全屏：把本视图最大化，再点一次还原")

    def set_remove_bed(self, enabled: bool):
        self._remove_bed_enabled = bool(enabled)
        if self._reader is not None:
            self._rebuild_pipeline()

    def set_is_mr(self, is_mr: bool):
        """切换到 MR 语义：像素是信号强度而非 HU

        MR 下床板识别与「骨阈值」都无意义（床板在 MR 上信号特征与 CT 完全不同），
        因此强制关闭去床，并改用血管专用的连通域清理。
        """
        self._is_mr = bool(is_mr)
        if self._is_mr:
            self._remove_bed_enabled = False
        if self._reader is not None:
            self._rebuild_pipeline()

    def set_keep_largest_region(self, enabled: bool):
        self._keep_largest_region = bool(enabled)
        if self._reader is not None:
            self._rebuild_pipeline()

    def set_color_mode(self, mode: str):
        self._color_mode = mode or "sample_bone"
        if self._reader is not None:
            self._rebuild_pipeline()

    def set_enhance_fractures(self, enabled: bool):
        """骨折增强开关：识别骨内低密度薄片（未移位骨折缝），在骨面标出暗线。

        实现为**独立叠加层 actor**，骨面本身的着色/光照完全不变，
        因此开启或关闭都不会影响画质（不会出现「开了变脏」）。

        仅对 CT 表面重建生效；MR 血管与 MIP/MinIP 投影不受影响。
        """
        enabled = bool(enabled)
        if enabled == self._enhance_fractures:
            return
        self._enhance_fractures = enabled
        if self._reader is not None:
            self._rebuild_pipeline()

    def set_reader(self, reader):
        self._reader = reader
        # 换了体数据，旧的投影已失效；重建前先拆掉投影 actor，
        # 否则 RemoveAllViewProps 后会变成悬空引用。
        self._teardown_projection()
        self._projection_mode = PROJECTION_SURFACE
        self._projection_impl = "volume"
        self._rebuild_pipeline()

    def load_dicom(self, folder: str):
        reader = vtkDICOMImageReader()
        reader.SetDirectoryName(folder)
        reader.Update()
        self.set_reader(reader)

    def _rebuild_pipeline(self):
        if self._reader is None:
            return

        # 投影模式（MIP/MinIP）下不建等值面：投影只依赖体数据本身。
        # 注意去床板/阈值变化也会走到这里 —— 三维体渲染的传递函数
        # 与掩膜依赖这些参数，所以必须重刷新，不能当作白跑。
        if self._projection_mode != PROJECTION_SURFACE:
            self._refresh_projection()
            return

        # 重建网格（换阈值/换数据/开关去床等）时清除切割状态，
        # 否则旧的切割网格会残留、且撤销/重置会指向已失效的几何。
        try:
            self._reset_cut_state()
        except Exception:
            pass

        self._reader.Update()

        iso = vtkFlyingEdges3D()
        shrink = vtkImageShrink3D()
        shrink.SetShrinkFactors(self._shrink_factor, self._shrink_factor, self._shrink_factor)
        shrink.AveragingOn()
        # iso 的输入（原目录体数据 / 连通域过滤后的掩膜体数据 / 预览降采样）
        # 由 _rewire_iso_input() 统一动态接线，见 _prepare_and_update()。
        iso.SetValue(0, self._iso_value)

        normals = vtkPolyDataNormals()
        normals.SetInputConnection(iso.GetOutputPort())
        normals.SetFeatureAngle(60.0)
        normals.ConsistencyOn()
        normals.SplittingOff()
        # 说明：原先的“仅保留最大连通域”(vtkConnectivityFilter) 与
        # “去床板”(裁剪底部 25%) 都已改为图像级连通域过滤，
        # 见 _build_component_mask() —— 踝/腕等多骨关节不再丢骨，
        # 床板按几何特征整块识别移除，不再误切患者组织。

        mapper = vtkPolyDataMapper()
        mode = (self._color_mode or "sample_bone").lower()

        # 统一处理颜色和映射器设置
        if mode in ("mono", "ge_bone", "sample_bone"):
            # 骨面渲染保持「纯骨色 + 光照」，不掺任何标量着色 ——
            # 掺色会让整块骨变脏/变暗（用户实测：画质下降），
            # 骨折线改由独立的叠加层 actor 表达（见 _detect_fracture_poly）。
            mapper.SetInputConnection(normals.GetOutputPort())
            mapper.ScalarVisibilityOff()
            self._mapper_source = normals
        else:
            # 只有伪彩模式才使用探针和LUT
            probe = vtkProbeFilter()
            probe.SetInputConnection(normals.GetOutputPort())
            probe.SetSourceConnection(self._reader.GetOutputPort())
            mapper.SetInputConnection(probe.GetOutputPort())
            mapper.ScalarVisibilityOn()
            self._reader.Update()
            output = self._reader.GetOutput()
            srange = output.GetScalarRange() if output else (0.0, 1.0)
            lut = vtkLookupTable()
            lut.SetNumberOfTableValues(256)
            lut.Build()
            def lerp(a, b, t):
                return a + (b - a) * t
            def set_grad(stops):
                for i in range(256):
                    t = i / 255.0
                    n = len(stops) - 1
                    seg = int(t * n)
                    if seg >= n:
                        seg = n - 1
                        tt = 1.0
                    else:
                        tt = (t * n) - seg
                    c0 = stops[seg]
                    c1 = stops[seg + 1]
                    r = lerp(c0[0], c1[0], tt)
                    g = lerp(c0[1], c1[1], tt)
                    b = lerp(c0[2], c1[2], tt)
                    lut.SetTableValue(i, r, g, b, 1.0)
            
            if mode == "rainbow":
                set_grad([(0.0,0.0,1.0),(0.0,1.0,1.0),(0.0,1.0,0.0),(1.0,1.0,0.0),(1.0,0.0,0.0)])
            elif mode == "fire":
                set_grad([(0.0,0.0,0.0),(0.5,0.0,0.0),(1.0,0.5,0.0),(1.0,1.0,0.0),(1.0,1.0,1.0)])
            elif mode == "ocean":
                set_grad([(0.0,0.0,0.2),(0.0,0.2,0.6),(0.0,0.6,0.8),(0.4,0.8,1.0)])
            elif mode == "gray":
                set_grad([(0.0,0.0,0.0),(1.0,1.0,1.0)])
            else:
                set_grad([(0.9, 0.9, 0.9), (0.9, 0.9, 0.9)])
                
            mapper.SetLookupTable(lut)
            mapper.SetScalarRange(srange[0], srange[1])
            self._probe = probe
            self._lut = lut
            self._mapper_source = probe

        actor = vtkActor()
        actor.SetMapper(mapper)
        
        # 设置具体模式的颜色
        if mode == "ge_bone":
            actor.GetProperty().SetColor(0.97, 0.95, 0.90) # 骨白
        elif mode == "sample_bone":
            # 默认骨色采用明亮的米黄色
            actor.GetProperty().SetColor(1.0, 0.92, 0.84) # 象牙白/米色
        elif mode == "mono":
            actor.GetProperty().SetColor(1.0, 1.0, 1.0)
            
        # 统一光照属性：亮度恢复原值（Ambient0.3+Diffuse0.8，保持骨色温暖不发灰），
        # 仅把高光系数降低并收紧高光范围（0.2/Power20 → 0.08/Power45），
        # 消除颅骨等光滑骨面的刺眼反光亮斑，同时不明显改变整体观感。
        prop = actor.GetProperty()
        prop.SetInterpolationToPhong()
        prop.SetAmbient(0.3)
        prop.SetDiffuse(0.8)
        prop.SetSpecular(0.08)
        prop.SetSpecularPower(45.0)

        self._renderer.RemoveAllViewProps()
        self._clear_fracture_actor()   # 叠加层是独立 actor，重建时要一并撤掉
        self._renderer.AddActor(actor)
        # 数据就绪前先隐藏，避免窗口重绘在主线程触发管线执行、与后台线程竞争
        actor.SetVisibility(False)

        # 更新拾取列表
        self._picker.InitializePickList()
        self._picker.AddPickList(actor)
        
        # 光标现在由 Overlay Renderer 管理，不需要在这里添加
        # if self._cursor_actor is not None:
        #    self._renderer.AddActor(self._cursor_actor)
        
        # 保存管线引用（后台构建期间由本类持有）
        self._iso = iso
        self._shrink = shrink
        self._connectivity = None   # 已由图像级连通域过滤取代
        self._normals = normals
        self._mapper = mapper
        # 注意：_mapper_source 已在上面按颜色模式分别赋值（灰度=normals，伪彩=probe），
        # 不能在这里无条件覆盖，否则伪彩模式丢失标量映射会整片变灰
        self._actor = actor

        # 全分辨率且体素数超阈值时，等值面重计算放到后台线程；
        # 预览（降采样）或小体数据同步执行即可，开销可忽略。
        if self._is_heavy_volume():
            self._render_pipeline_async(full_setup=True)
        else:
            self._prepare_and_update()
            self._sync_fracture_actor()
            self._setup_camera_and_render(first_build=True)

    # ------------------------------------------------------------------
    # MIP / MinIP：把体数据沿层轴取极值，生成单张二维投影图
    # ------------------------------------------------------------------
    # 全分辨率 MIP 对大体积要扫全部体素（几十万行 CT 可到秒级），
    # 所以按体素数自动抽稀层方向，并在 UI 上如实告知用了多大步长，
    # 不能让用户以为看到的是全分辨率。
    PROJECTION_STRIDE_VOXEL_LIMIT = 40_000_000

    def set_projection_mode(self, mode: str):
        """切换渲染模式：surface / mip / minip。

        返回 (ok, message)。ok=False 时不改变当前状态，message 供 UI 弹窗。
        mip/minip 优先走**三维体渲染**（可旋转，类似三维重建）；
        体渲染不可用时自动回退为二维投影并在 message 里说明。
        """
        mode = (mode or PROJECTION_SURFACE).lower()
        if mode not in PROJECTION_MODES:
            return False, "未知的投影模式：%s" % mode
        if mode == self._projection_mode:
            return True, ""

        if mode == PROJECTION_SURFACE:
            self._projection_mode = mode
            self._teardown_projection()
            # 投影时藏掉的光标要恢复，否则切回三维就再也看不到定位点了
            try:
                self._visual_cursor_actor.SetVisibility(True)
                self._interaction_cursor_actor.SetVisibility(True)
            except Exception:
                pass
            self._rebuild_pipeline()
            return True, ""

        self._projection_mode = mode
        self._projection_impl = "volume"     # 每次切换都先尝试三维体渲染
        ok, message = self._refresh_projection()
        if not ok:
            # 渲染失败要退回 surface，否则用户卡在「切不回三维」的坏状态
            self._projection_mode = PROJECTION_SURFACE
            self._teardown_projection()
            self._rebuild_pipeline()
            return False, message
        return True, message

    def projection_mode(self) -> str:
        return self._projection_mode

    def _projection_source_array(self):
        """取投影用的体数据 (nz, ny, nx) —— 与三维掩膜同一份口径。

        优先用去床板/去碎屑后的掩膜（否则 MIP 里会有一条床板亮带），
        没有掩膜时退回原始体数据。
        """
        from vtkmodules.util.numpy_support import vtk_to_numpy

        image = self._masked_image
        if image is None and self._reader is not None:
            self._reader.Update()
            image = self._reader.GetOutput()
        if image is None or image.GetNumberOfScalarComponents() > 1:
            return None, None
        ext = image.GetExtent()
        nx = ext[1] - ext[0] + 1
        ny = ext[3] - ext[2] + 1
        nz = ext[5] - ext[4] + 1
        if nx <= 0 or ny <= 0 or nz <= 0:
            return None, None
        arr = vtk_to_numpy(image.GetPointData().GetScalars()).reshape(nz, ny, nx)
        return arr, image

    @classmethod
    def _projection_stride_for(cls, nz: int, ny: int, nx: int) -> int:
        """按体素量挑层抽稀步长（1 = 全分辨率）。

        用 classmethod：只依赖类常量 `PROJECTION_STRIDE_VOXEL_LIMIT`，
        不读实例状态 —— 这样能脱离 VTKViewer 实例单独测（offscreen 下
        构造 VTKViewer 会触发 OpenGL，段错误）。
        """
        voxels = int(nx) * int(ny) * int(nz)
        if voxels <= cls.PROJECTION_STRIDE_VOXEL_LIMIT:
            return 1
        import math
        return int(max(1, math.ceil(voxels / float(cls.PROJECTION_STRIDE_VOXEL_LIMIT))))

    def _refresh_projection(self):
        """按当前模式刷新投影显示（统一入口，_rebuild_pipeline 也走这里）。

        三维体渲染（volume）优先 —— 用户期望「像三维重建一样可旋转」；
        构造失败（异常 / 老显卡不支持）自动回退二维投影图（slice），
        并在 message 里如实告知用户当前看到的是哪一种。
        """
        if self._projection_impl == "slice":
            return self._render_projection()
        try:
            ok, message = self._render_volume_projection()
        except Exception as exc:       # noqa: BLE001
            ok, message = False, "三维体渲染初始化失败：%s" % exc
        if ok:
            return True, message
        # 三维失败 → 回退二维
        fallback_reason = message
        self._teardown_projection()
        self._projection_impl = "slice"
        ok2, msg2 = self._render_projection()
        if ok2:
            msg2 = (msg2 + "\n\n" if msg2 else "\n") + (
                "本机无法启用三维体渲染（%s），已自动改用二维最大/最小密度"
                "投影图 —— 它是三维体数据沿视线压平的单张图像，不能旋转。"
                % fallback_reason)
        return ok2, msg2

    def _render_volume_projection(self):
        """三维体渲染 MIP/MinIP：可旋转、类似三维重建。

        用 vtkSmartVolumeMapper 的 blend mode 沿每条视线取最大/最小值，
        GPU 可用时走 GPU ray casting，不可用自动落到 CPU ray casting。
        返回 (ok, message)。
        """
        if self._reader is None:
            return False, "请先载入影像数据。"
        try:
            arr_image, image = self._projection_source_array()
        except Exception as exc:       # noqa: BLE001
            return False, "读取体数据失败：%s" % exc
        if arr_image is None:
            return False, "当前序列没有可用于投影的体数据。"
        source = self._masked_image if self._masked_image is not None \
            else image

        mapper = vtkSmartVolumeMapper()
        mapper.SetInputData(source)
        if self._projection_mode == PROJECTION_MIP:
            mapper.SetBlendModeToMaximumIntensity()
        else:
            mapper.SetBlendModeToMinimumIntensity()

        otf, ctf = self._build_volume_tf(source)
        prop = vtkVolumeProperty()
        prop.SetScalarOpacity(otf)
        prop.SetColor(ctf)
        prop.SetInterpolationTypeToLinear()
        # MIP/MinIP 里 ambient/diffuse 光照没有意义（不是表面着色），
        # 关掉 Shade 避免老显卡上出现整块黑影
        try:
            prop.ShadeOff()
        except Exception:
            pass

        volume = vtkVolume()
        volume.SetMapper(mapper)
        volume.SetProperty(prop)

        self._renderer.RemoveAllViewProps()
        self._clear_fracture_actor()   # 体渲染模式没有骨面，撤掉骨折线叠加层
        self._renderer.AddVolume(volume)
        self._volume_actor = volume

        # 光标在体渲染下没有锚定语义，藏起来
        try:
            self._visual_cursor_actor.SetVisibility(False)
            self._interaction_cursor_actor.SetVisibility(False)
        except Exception:
            pass

        self._setup_volume_camera()
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()
        return True, ""

    def _build_volume_tf(self, source):
        """构造体渲染的传递函数（不透明度 + 灰度颜色）。

        以当前阈值 iso_value 为视觉锚点：阈值附近开始渐显，
        高密度（骨/血管）接近不透明发亮 —— 与表面重建的阈值语义一致，
        用户拖阈值滑块时体渲染的「显现程度」会同步变化。
        """
        vmin, vmax = source.GetScalarRange()
        vmin, vmax = float(vmin), float(vmax)
        iso = float(self._iso_value)
        lo = min(vmin, iso - 300.0)
        hi = max(vmax, iso + 300.0)
        if hi <= lo:
            hi = lo + 1.0

        otf = vtkPiecewiseFunction()
        otf.AddPoint(lo, 0.0)
        otf.AddPoint(iso - (iso - lo) * 0.25, 0.04)
        otf.AddPoint(iso, 0.18)
        otf.AddPoint((iso + hi) / 2.0, 0.55)
        otf.AddPoint(hi, 0.94)

        ctf = vtkColorTransferFunction()
        ctf.AddRGBPoint(lo, 0.02, 0.02, 0.03)
        ctf.AddRGBPoint(iso, 0.52, 0.50, 0.48)
        ctf.AddRGBPoint(hi, 1.0, 0.99, 0.95)
        return otf, ctf

    def _setup_volume_camera(self):
        """体渲染的初始相机：与表面重建相同的 3/4 视角，可自由旋转。

        不能复用 _setup_camera_and_render —— 它会把 surface actor
        重新 SetVisibility(True)，投影下会漏出旧模型。
        """
        camera = self._renderer.GetActiveCamera()
        camera.SetViewUp(0, -1, 0)
        if self._reader:
            output = self._reader.GetOutput()
            if output:
                extent = output.GetExtent()
                spacing = output.GetSpacing()
                origin = output.GetOrigin()
                center_x = origin[0] + (extent[0] + extent[1]) * spacing[0] / 2.0
                center_y = origin[1] + (extent[2] + extent[3]) * spacing[1] / 2.0
                center_z = origin[2] + (extent[4] + extent[5]) * spacing[2] / 2.0
                size = max((extent[1] - extent[0]) * spacing[0],
                           (extent[3] - extent[2]) * spacing[1],
                           (extent[5] - extent[4]) * spacing[2])
                distance = max(size, 1.0) * 2.5
                camera.SetFocalPoint(center_x, center_y, center_z)
                camera.SetPosition(center_x + distance * 0.7,
                                   center_y - distance * 0.7,
                                   center_z + distance * 0.5)
                camera.SetClippingRange(distance * 0.1, distance * 5)
        self._renderer.ResetCamera()
        self._renderer.GetActiveCamera().Zoom(1.35)

    def _render_projection(self):
        """生成 MIP/MinIP 投影图并显示。返回 (ok, message)。"""
        if self._reader is None:
            return False, "请先载入影像数据。"
        try:
            arr, image = self._projection_source_array()
        except Exception as exc:   # noqa: BLE001
            return False, "读取体数据失败：%s" % exc
        if arr is None:
            return False, "当前序列没有可用于投影的体数据。"

        ext = image.GetExtent()
        nz = ext[5] - ext[4] + 1
        ny = ext[3] - ext[2] + 1
        nx = ext[1] - ext[0] + 1
        stride = self._projection_stride_for(nz, ny, nx)

        proj, used_stride = project_volume(arr, self._projection_mode, stride)
        if proj is None:
            return False, "投影失败：体数据为空或模式非法。"

        window, level = projection_window_level(proj, self._is_mr)

        # numpy 的行序是「上→下」，VTK 显示坐标 y 向上为正，
        # 与二维读片一致地翻转一次（见项目铁律：显示坐标 y 向上）。
        proj = np.flipud(proj).astype(np.float32, copy=False)

        vtk_img = vtkImageData()
        vtk_img.SetDimensions(nx, ny, 1)
        try:
            spacing = image.GetSpacing()
            origin = image.GetOrigin()
            vtk_img.SetSpacing(float(spacing[0]), float(spacing[1]), 1.0)
            # y 翻转后第一行对应 vtk 的 j=0，故 origin y 用体数据的最大 y 边
            vtk_img.SetOrigin(float(origin[0]),
                              float(origin[1]) + float(spacing[1]) * (ny - 1),
                              float(origin[2]))
        except Exception:
            pass
        vtk_img.AllocateScalars(VTK_FLOAT, 1)
        from vtkmodules.util.numpy_support import vtk_to_numpy as _v2n
        dest = _v2n(vtk_img.GetPointData().GetScalars()).reshape(ny, nx)
        dest[:] = proj

        # VTK 9.5 的类型配对（踩过，写清楚别再改）：
        #   - `vtkImageSlice.SetMapper()` 要的是 **vtkImageMapper3D**（抽象类），
        #     传 vtkImageMapper 会报 `TypeError: SetMapper argument %Id: %V`；
        #   - vtkImageMapper 继承的是 vtkMapper2D，只能配 vtkActor2D，
        #     而 vtkActor2D 走 overlay 层、没有 3D 相机，进不了三维窗口；
        #   - 所以 2D 投影图在三维窗口里的正确组合是
        #     **vtkImageSlice + vtkOpenGLImageSliceMapper**（vtkImageMapper3D
        #     的具体实现）。
        mapper = vtkOpenGLImageSliceMapper()
        mapper.SetInputData(vtk_img)
        actor = vtkImageSlice()
        actor.SetMapper(mapper)
        # 窗宽窗位在 vtkImageProperty 上，名字是 SetColorWindow/SetColorLevel
        # （不是 vtkActor 那套 SetWindowLevel，vtkImageProperty 没有）
        prop = actor.GetProperty()
        prop.SetColorWindow(float(window))
        prop.SetColorLevel(float(level))
        prop.SetInterpolationTypeToNearest()

        self._renderer.GetActors().InitTraversal()
        for i in range(self._renderer.GetActors().GetNumberOfItems()):
            a = self._renderer.GetActors().GetNextActor()
            if a is not None:
                a.SetVisibility(False)

        self._renderer.RemoveAllViewProps()
        self._clear_fracture_actor()   # 投影模式没有骨面，撤掉骨折线叠加层
        self._renderer.AddActor(actor)
        self._projection_actor = actor
        self._projection_image = vtk_img
        self._projection_stride = used_stride

        # 方位字与光标在投影下没有意义，先藏起来避免误导
        try:
            self._visual_cursor_actor.SetVisibility(False)
            self._interaction_cursor_actor.SetVisibility(False)
        except Exception:
            pass

        self._setup_projection_camera()
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

        message = ""
        if used_stride > 1:
            message = ("体数据较大（%d 层），已按每 %d 层取一层抽样以保证速度；"
                       "如需全分辨率请改用较小体数据或关闭三维预览。"
                       % (nz, used_stride))
        return True, message

    def _setup_projection_camera(self):
        """投影图的正视相机。

        单独一套而不复用 _setup_camera_and_render，原因有两个：
        1) 那个会把 surface actor 重新 SetVisibility(True)，投影下会漏出旧模型；
        2) 那个按「斜上方 3/4 视角」摆相机，二维投影图必须正对（相机在 -Z 一侧），
           否则看到的不是投影图本身。
        投影图位于 XY 平面（Z 固定），所以相机放在 -Z 侧正对，viewup 取 -Y，
        与三维视图的 viewup 一致，切模式时方位不会跳。
        """
        image = self._projection_image
        if image is None:
            return
        ext = image.GetExtent()
        spacing = image.GetSpacing()
        origin = image.GetOrigin()
        cx = origin[0] + (ext[0] + ext[1]) * spacing[0] / 2.0
        cy = origin[1] + (ext[2] + ext[3]) * spacing[1] / 2.0
        cz = origin[2]
        width = (ext[1] - ext[0]) * spacing[0]
        height = (ext[3] - ext[2]) * spacing[1]
        distance = max(width, height, 1.0) * 3.0

        camera = self._renderer.GetActiveCamera()
        camera.SetViewUp(0, -1, 0)
        camera.SetFocalPoint(cx, cy, cz)
        camera.SetPosition(cx, cy, cz - distance)
        camera.SetViewAngle(30.0)
        camera.SetClippingRange(distance * 0.05, distance * 5)

        self._renderer.ResetCamera()
        self._renderer.GetActiveCamera().Zoom(1.0)

    def _teardown_projection(self):
        """拆掉全部投影 actor（二维图与三维体渲染），回到「无投影」状态。"""
        actor = self._projection_actor
        if actor is not None:
            try:
                self._renderer.RemoveActor(actor)
            except Exception:
                pass
        vol = self._volume_actor
        if vol is not None:
            try:
                self._renderer.RemoveVolume(vol)
            except Exception:
                pass
        self._projection_actor = None
        self._projection_image = None
        self._volume_actor = None
        self._projection_stride = 1

    def has_projection(self) -> bool:
        return (self._projection_actor is not None
                or self._volume_actor is not None)

    def _is_heavy_volume(self):
        """全分辨率且体素量大时返回 True（等值面提取应放到后台线程）"""
        if self._reader is None or self._preview_enabled:
            return False
        try:
            ext = self._reader.GetOutput().GetExtent()
            voxels = ((ext[1] - ext[0] + 1) * (ext[3] - ext[2] + 1)
                      * (ext[5] - ext[4] + 1))
            return voxels >= self.ASYNC_VOXEL_LIMIT
        except Exception:
            return False

    def _rewire_iso_input(self):
        """按预览/掩膜状态重接等值面及其下游输入。

        可在 worker 线程调用（须与主线程渲染互斥——构建期间 actor 已隐藏）。
        """
        if self._iso is None:
            return
        if self._preview_enabled:
            if self._shrink is None:
                self._shrink = vtkImageShrink3D()
                self._shrink.SetShrinkFactors(self._shrink_factor,
                                              self._shrink_factor,
                                              self._shrink_factor)
                self._shrink.AveragingOn()
            if self._masked_image is not None:
                self._shrink.SetInputData(self._masked_image)
            elif self._reader is not None:
                self._shrink.SetInputConnection(self._reader.GetOutputPort())
            self._iso.SetInputConnection(self._shrink.GetOutputPort())
        else:
            if self._masked_image is not None:
                self._iso.SetInputData(self._masked_image)
            elif self._reader is not None:
                self._iso.SetInputConnection(self._reader.GetOutputPort())
        # mapper 输入：预览时直接接 iso（跳过法线计算提速）；
        # 全分辨率时接 _mapper_source（灰度模式=normals，伪彩模式=probe）
        if self._mapper is not None:
            if self._preview_enabled:
                self._mapper.SetInputConnection(self._iso.GetOutputPort())
            elif self._mapper_source is not None:
                self._mapper.SetInputConnection(self._mapper_source.GetOutputPort())

    def _prepare_and_update(self):
        """准备数据（必要时重建连通域掩膜）并执行整条三维管线重计算。

        优先在 worker 线程调用（_PipelineWorker）；小体数据也可在主线程
        同步调用。期间主线程不得触碰管线对象（渲染侧已用隐藏 actor 互斥）。
        """
        if self._reader is None or self._mapper is None:
            return
        self._reader.Update()
        if self._keep_largest_region or self._remove_bed_enabled or self._is_mr:
            state = (int(self._iso_value), bool(self._keep_largest_region),
                     bool(self._remove_bed_enabled), id(self._reader),
                     bool(self._is_mr))
            if self._mask_state != state:
                self._build_component_mask()
                self._mask_state = state
        else:
            self._masked_image = None
            self._mask_state = None
        self._rewire_iso_input()
        self._mapper.Update()
        # 骨折线叠加层：纯计算，不碰 renderer，可在 worker 线程执行
        self._fracture_poly = self._detect_fracture_poly()

    # ------------------------------------------------------------------
    # 骨折线增强：独立叠加层（不动骨面着色，保证画质不下降）
    # ------------------------------------------------------------------
    #
    # 原理：未移位细微骨折在 CT 里表现为骨内一条**低密度薄片**（缝内是
    # 液体/空气），两侧是致密骨；它比骨面曲率可靠得多 —— 曲率在 CT 噪声
    # 造成的粗糙等值面上信噪比极差（实测召回 0%，只能给噪声上色，正是
    # 「骨折增强后画质下降」的根因）。
    #
    # 做法：体数据里找「自身 HU 低、且沿 x/y/z 任一轴两侧 dist 体素处
    # 都是致密骨」的薄片体素 → 投影到等值面顶点（±1 体素邻域）→ 连通性
    # 过滤 → 做成**独立的暗色叠加层 actor**。
    # 骨面本身的颜色/光照完全不变，所以最坏情况也只是「没画出来」，
    # 不可能让画质变差。

    @staticmethod
    def _fracture_sheet_mask(arr, void_hu, surround_hu, dist):
        """找体数据里的「低密度薄片」。

        arr: (nz, ny, nx) HU 数组。返回同形状 bool 掩膜。
        判据：体素 HU < void_hu（缝内），且沿 x/y/z 任一轴两侧 dist 体素处
        HU > surround_hu（缝两侧是致密骨）。按 z 分块处理，内存可控。
        """
        nz, ny, nx = arr.shape
        out = np.zeros((nz, ny, nx), bool)
        d = int(max(1, dist))
        step = max(4, int(6_000_000 / max(1, ny * nx)))
        for z0 in range(0, nz, step):
            z1 = min(nz, z0 + step)
            a0 = max(0, z0 - d)
            a1 = min(nz, z1 + d)
            sub = arr[a0:a1]
            void = sub < void_hu
            bone = sub > surround_hu
            hit = np.zeros_like(void)
            # x 轴
            bp = np.zeros_like(bone)
            bp[:, :, d:] = bone[:, :, :-d]
            bm = np.zeros_like(bone)
            bm[:, :, :-d] = bone[:, :, d:]
            hit |= void & bp & bm
            # y 轴
            bp = np.zeros_like(bone)
            bp[:, d:, :] = bone[:, :-d, :]
            bm = np.zeros_like(bone)
            bm[:, :-d, :] = bone[:, d:, :]
            hit |= void & bp & bm
            # z 轴（sub 已带 halo，结果只取中间段）
            bp = np.zeros_like(bone)
            bp[d:, :, :] = bone[:-d, :, :]
            bm = np.zeros_like(bone)
            bm[:-d, :, :] = bone[d:, :, :]
            hit |= void & bp & bm
            out[z0:z1] = hit[z0 - a0: z1 - a0]
        return out

    @staticmethod
    def _fracture_surface_mask(pts, shape, origin, spacing, sheet):
        """把薄片体素投影到等值面顶点：自身或 26 邻域命中薄片 → 该顶点是裂缝。

        只在薄片体素的包围盒内查表（骨折线通常只占很小一块），
        否则 27 次全量 gather 在百万级顶点上要好几秒。
        """
        nz, ny, nx = shape
        uvw = (pts - origin) / spacing
        i0 = np.floor(uvw).astype(np.int64)
        x0 = np.clip(i0[:, 0], 0, nx - 1)
        y0 = np.clip(i0[:, 1], 0, ny - 1)
        z0 = np.clip(i0[:, 2], 0, nz - 1)
        hit = np.zeros(pts.shape[0], bool)
        # 薄片体素在三个轴上的投影范围（不做 nonzero，避免大数组索引爆内存）
        any_z = sheet.any(axis=(1, 2))
        any_y = sheet.any(axis=(0, 2))
        any_x = sheet.any(axis=(0, 1))
        if not (any_z.any() and any_y.any() and any_x.any()):
            return hit
        bx0, bx1 = int(np.argmax(any_x)), int(nx - 1 - np.argmax(any_x[::-1]))
        by0, by1 = int(np.argmax(any_y)), int(ny - 1 - np.argmax(any_y[::-1]))
        bz0, bz1 = int(np.argmax(any_z)), int(nz - 1 - np.argmax(any_z[::-1]))
        sel = ((x0 >= bx0 - 1) & (x0 <= bx1 + 1)
               & (y0 >= by0 - 1) & (y0 <= by1 + 1)
               & (z0 >= bz0 - 1) & (z0 <= bz1 + 1))
        if not sel.any():
            return hit
        sx, sy, sz = x0[sel], y0[sel], z0[sel]
        sub = np.zeros(int(sel.sum()), bool)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    sub |= sheet[np.clip(sz + dz, 0, nz - 1),
                                 np.clip(sy + dy, 0, ny - 1),
                                 np.clip(sx + dx, 0, nx - 1)]
        hit[sel] = sub
        return hit

    @staticmethod
    def _fracture_keep_large_components(mask, faces, min_faces):
        """只保留足够大的连通块：骨折线是长条，噪点是小斑块。"""
        keep_face = mask[faces].sum(axis=1) >= 2
        if not keep_face.any():
            return np.zeros(mask.shape[0], bool)
        sub = faces[keep_face]
        if sub.shape[0] > 400000:          # 候选过多时不做连通块过滤
            return mask
        parent = {}

        def find(a):
            root = a
            while parent[root] != root:
                root = parent[root]
            while parent[a] != root:      # 路径压缩
                parent[a], a = root, parent[a]
            return root

        for a, b, c in sub:
            for x, y in ((a, b), (b, c), (a, c)):
                parent.setdefault(x, x)
                parent.setdefault(y, y)
                rx, ry = find(x), find(y)
                if rx != ry:
                    parent[rx] = ry
        sizes = {}
        for a, _b, _c in sub:
            r = find(a)
            sizes[r] = sizes.get(r, 0) + 1
        big = set(r for r, s in sizes.items() if s >= min_faces)
        out = np.zeros(mask.shape[0], bool)
        for a, b, c in sub:
            if find(a) in big:
                out[a] = out[b] = out[c] = True
        return out

    def _detect_fracture_poly(self):
        """检测骨面骨折线，返回叠加层网格（vtkPolyData 或 None）。

        纯计算（numpy + VTK），不触碰 renderer，可在后台线程调用。
        """
        if not self._enhance_fractures or self._is_mr or self._preview_enabled:
            return None
        if self._normals is None:
            return None
        try:
            # 表面可能刚换过阈值/退出预览，先把法线链刷新到最新
            self._normals.Update()
        except Exception:                # noqa: BLE001
            return None
        image = self._masked_image
        if image is None:
            if self._reader is None:
                return None
            self._reader.Update()
            image = self._reader.GetOutput()
        if image is None or image.GetNumberOfScalarComponents() > 1:
            return None
        ext = image.GetExtent()
        nx = ext[1] - ext[0] + 1
        ny = ext[3] - ext[2] + 1
        nz = ext[5] - ext[4] + 1
        if nx <= 1 or ny <= 1 or nz <= 1:
            return None
        if nx * ny * nz > self.FRACTURE_MAX_VOXELS:
            return None                  # 内存保护：超大体数据跳过增强
        try:
            from vtkmodules.util.numpy_support import vtk_to_numpy
            arr = vtk_to_numpy(image.GetPointData().GetScalars()).reshape(nz, ny, nx)
            sheet = self._fracture_sheet_mask(
                arr, self.FRACTURE_VOID_HU, self.FRACTURE_SURROUND_HU,
                self.FRACTURE_SHEET_DIST)
            if not sheet.any():
                return None
            poly = self._normals.GetOutput()
            pts = vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)
            faces = vtk_to_numpy(poly.GetPolys().GetConnectivityArray())
            faces = faces.reshape(-1, 3).astype(np.int64)
            origin = np.array(image.GetOrigin(), dtype=np.float64)
            spacing = np.array(image.GetSpacing(), dtype=np.float64)
            mask = self._fracture_surface_mask(pts, (nz, ny, nx), origin,
                                               spacing, sheet)
            if not mask.any():
                return None
            mask = self._fracture_keep_large_components(
                mask, faces, self.FRACTURE_MIN_COMPONENT_FACES)
            if not mask.any():
                return None
            return self._build_fracture_overlay(pts, faces, mask, arr,
                                                 origin, spacing)
        except Exception:                # noqa: BLE001
            return None

    def _build_fracture_overlay(self, pts, faces, mask, arr, origin, spacing):
        """生成叠加层网格：只保留裂缝处的面，顶点沿「骨外」方向微移防 z-fighting。"""
        keep = mask[faces].sum(axis=1) >= 2
        if not keep.any():
            return None
        sub = faces[keep]
        used = np.unique(sub.ravel())
        remap = np.full(pts.shape[0], -1, np.int64)
        remap[used] = np.arange(used.size)
        moved = pts[used] + self._fracture_outward_offset(
            pts[used], arr, origin, spacing) * (self.FRACTURE_OFFSET_VOXEL
                                                * float(spacing[0]))
        poly = vtkPolyData()
        vpts = vtkPoints()
        try:
            from vtkmodules.util.numpy_support import numpy_to_vtk
            vpts.SetData(numpy_to_vtk(moved, deep=True))
        except Exception:                # noqa: BLE001
            return None
        poly.SetPoints(vpts)
        cells = vtkCellArray()
        for a, b, c in remap[sub]:
            cells.InsertNextCell(3)
            cells.InsertCellPoint(int(a))
            cells.InsertCellPoint(int(b))
            cells.InsertCellPoint(int(c))
        poly.SetPolys(cells)
        return poly

    def _fracture_outward_offset(self, pts, arr, origin, spacing):
        """单位向量：指向骨外（体积梯度方向取负）。

        网格法线在 CT 粗糙等值面上不可靠（实测只有约一半朝外），
        体积梯度（指向密度增大 = 指向骨内）才是稳的。
        """
        nz, ny, nx = arr.shape

        def sample_at(p):
            uvw = np.floor((p - origin) / spacing).astype(np.int64)
            xi = np.clip(uvw[:, 0], 0, nx - 1)
            yi = np.clip(uvw[:, 1], 0, ny - 1)
            zi = np.clip(uvw[:, 2], 0, nz - 1)
            return arr[zi, yi, xi].astype(np.float64)

        h = 0.75
        grad = np.empty((pts.shape[0], 3))
        for ax in range(3):
            step = np.zeros_like(pts)
            step[:, ax] = h * spacing[ax]
            grad[:, ax] = sample_at(pts + step) - sample_at(pts - step)
        norm = np.linalg.norm(grad, axis=1)
        out = np.zeros_like(pts)
        good = norm > 1.0
        out[good] = -grad[good] / norm[good][:, None]
        return out

    def _sync_fracture_actor(self):
        """把算好的骨折线网格装成叠加层 actor（必须在主线程调用）。"""
        self._clear_fracture_actor()
        poly = getattr(self, "_fracture_poly", None)
        if poly is None or poly.GetNumberOfPoints() == 0:
            self._fracture_poly = None
            return
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(poly)
        mapper.ScalarVisibilityOff()
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*self.FRACTURE_LINE_COLOR)
        prop.SetAmbient(0.85)
        prop.SetDiffuse(0.25)
        prop.SetSpecular(0.0)
        try:
            self._renderer.AddActor(actor)
        except Exception:                # noqa: BLE001
            return
        self._fracture_actor = actor

    def _clear_fracture_actor(self):
        """移除叠加层 actor（renderer 可能已被 RemoveAllViewProps 清空）。"""
        actor = getattr(self, "_fracture_actor", None)
        if actor is not None:
            try:
                self._renderer.RemoveActor(actor)
            except Exception:            # noqa: BLE001
                pass
        self._fracture_actor = None
        self._fracture_poly = None

    def _build_component_mask(self):
        """按当前阈值对体数据做 3D 连通域过滤，生成 self._masked_image。

        去碎屑 / 去床板算法见模块级 filter_bone_components()。
        无需剔除任何体素时置 _masked_image=None 走快速路径。
        仅在 worker / 主线程互斥环境下调用。
        """
        from vtkmodules.util.numpy_support import vtk_to_numpy
        self._masked_image = None
        output = self._reader.GetOutput()
        if output is None or output.GetNumberOfScalarComponents() > 1:
            return
        ext = output.GetExtent()
        nx = ext[1] - ext[0] + 1
        ny = ext[3] - ext[2] + 1
        nz = ext[5] - ext[4] + 1
        # 内存保护：连通域标记需 ~4 字节/体素，超限序列跳过过滤（防低配机 OOM）
        if nx * ny * nz > self.MAX_FILTER_VOXELS:
            return
        arr = vtk_to_numpy(output.GetPointData().GetScalars()).reshape(nz, ny, nx)
        if self._is_mr:
            # MR：信号强度语义。血管/组织都是高信号结构，没有 HU 床板可认，
            # 只做「剔除非主干的漂浮噪点」，且必须用邻接保护保住细分支。
            filtered = filter_vessel_components(
                arr, float(self._iso_value),
                min_voxels=self.VESSEL_MIN_VOXELS,
                keep_ratio=self.VESSEL_KEEP_RATIO,
                neighbor_gap=self.VESSEL_NEIGHBOR_GAP,
            )
        else:
            filtered = filter_bone_components(
                arr, float(self._iso_value),
                keep_large=bool(self._keep_largest_region),
                remove_bed=bool(self._remove_bed_enabled),
                min_voxels=self.KEEP_REGION_MIN_VOXELS,
                rel_fraction=self.KEEP_REGION_RELATIVE,
                bed_min_voxels=self.BED_MIN_VOXELS,
                bed_hu_p99=self.BED_HU_P99,
                bed_hu_hot=self.BED_HU_HOT,
                bed_hot_fraction=self.BED_HOT_FRACTION,
                bed_hu_p99_max=self.BED_HU_P99_MAX,
                cut_max_fraction=self.CUT_MAX_FRACTION,
            )
        if filtered is None:
            return    # 无任何剔除，走快速路径
        image = vtkImageData()
        image.SetDimensions(nx, ny, nz)
        try:
            image.SetSpacing(*output.GetSpacing())
            image.SetOrigin(*output.GetOrigin())
        except Exception:
            pass
        image.AllocateScalars(output.GetScalarType(), 1)
        dest = vtk_to_numpy(image.GetPointData().GetScalars()).reshape(nz, ny, nx)
        dest[:] = filtered
        self._masked_image = image

    def _setup_camera_and_render(self, first_build: bool = False):
        """配置相机并渲染（轻量操作，始终在主线程执行）"""
        # 数据已就绪（同步路径或 worker 已完成），恢复 actor 显示
        if self._actor is not None:
            self._actor.SetVisibility(True)
        camera = self._renderer.GetActiveCamera()
        camera.SetViewUp(0, -1, 0)  # 与轴向视图保持一致

        # 获取数据范围并设置合适的相机位置
        if self._reader:
            output = self._reader.GetOutput()
            if output:
                extent = output.GetExtent()
                spacing = output.GetSpacing()
                origin = output.GetOrigin()

                # 计算数据中心的物理坐标
                center_x = origin[0] + (extent[0] + extent[1]) * spacing[0] / 2.0
                center_y = origin[1] + (extent[2] + extent[3]) * spacing[1] / 2.0
                center_z = origin[2] + (extent[4] + extent[5]) * spacing[2] / 2.0

                # 计算数据尺寸
                width = (extent[1] - extent[0]) * spacing[0]
                height = (extent[3] - extent[2]) * spacing[1]
                depth = (extent[5] - extent[4]) * spacing[2]

                # 设置相机焦点在数据中心
                camera.SetFocalPoint(center_x, center_y, center_z)

                # 设置相机位置（从斜上方观察）
                distance = max(width, height, depth) * 2.5
                camera.SetPosition(center_x + distance * 0.7, center_y - distance * 0.7, center_z + distance * 0.5)

                # 确保能看到整个数据集
                camera.SetClippingRange(distance * 0.1, distance * 5)

        self._renderer.ResetCamera()
        self._renderer.GetActiveCamera().Zoom(1.35)

        if first_build:
            # 添加坐标系校准 + 设置3D视图初始光标（仅首次构建）
            self.calibrate_3d_coordinates()
            self._setup_initial_cursor()

        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            self._renderer.ResetCameraClippingRange()
            render_win.Render()

    def _render_pipeline_async(self, full_setup: bool):
        """在后台线程执行三维管线重计算，完成后回主线程渲染。

        用 QEventLoop 模态等待（进度框期间界面保持响应不冻结）。
        full_setup=True 时完成后重置相机/校准/初始光标（首次构建、换数据）；
        False 时仅刷新裁剪面并渲染（调阈值/退出预览，保持视角）。
        """
        worker = self._pipeline_worker
        if worker is not None and worker.isRunning():
            worker.wait()   # 兜底：上一轮还没跑完就先等它结束

        # 构建期间隐藏 actor：防止窗口重绘在主线程触发管线执行、与 worker 竞争
        if self._actor is not None:
            self._actor.SetVisibility(False)

        # 只有在**没有外层遮罩**时才弹 TechBusyDialog。
        # 否则会出现两个提示：StudyViewPanel 上的 BusyOverlay 文案居中，
        # 而本对话框的 parent=self(VTKViewer) 被 showEvent 定位到 VTKViewer 中心
        # ——VTKViewer 在四宫格 1,1 位，就落在右下角。
        # 外层遮罩正在运行时再弹一个窗口，用户会认为卡死。
        outer_text = loading_ui.busy_ancestor_text(self)
        progress = None
        if outer_text is None:
            progress = loading_ui.TechBusyDialog(
                "正在重建三维模型，请稍候…", None, self)
            progress.setWindowTitle("正在重建三维模型")
            progress.setWindowModality(QtCore.Qt.WindowModal)
            progress.setMinimumDuration(0)
            progress.show()
            QtWidgets.QApplication.processEvents()

        loop = QtCore.QEventLoop()
        errors = []
        worker = _PipelineWorker(self._prepare_and_update)
        worker.done.connect(loop.quit)
        worker.failed.connect(lambda msg: (errors.append(msg), loop.quit()))
        self._pipeline_worker = worker
        worker.start()
        loop.exec_()          # 等待期间事件循环仍在跑，界面不冻结
        if progress is not None:
            progress.close()
        if self._pipeline_worker is worker:
            self._pipeline_worker = None
        worker.deleteLater()
        if errors:
            QtWidgets.QMessageBox.warning(self, "三维构建失败", errors[0])
            return   # 保持 actor 隐藏，避免主线程同步执行失败管线
        if self._actor is not None:
            self._actor.SetVisibility(True)
        self._sync_fracture_actor()
        if full_setup:
            self._setup_camera_and_render(first_build=True)
        else:
            render_win = self._vtk_widget.GetRenderWindow()
            if render_win:
                self._renderer.ResetCameraClippingRange()
                render_win.Render()

    def set_preview_enabled(self, enabled: bool):
        self._preview_enabled = bool(enabled)
        if self._iso is None or self._reader is None:
            return
        if enabled:
            if self._actor is not None:
                prop = self._actor.GetProperty()
                prop.SetInterpolationToFlat()
                prop.SetAmbient(0.3)
                prop.SetDiffuse(0.8)
                prop.SetSpecular(0.0)
        else:
            if self._actor is not None:
                prop = self._actor.GetProperty()
                prop.SetInterpolationToPhong()
                prop.SetAmbient(0.3)
                prop.SetDiffuse(0.8)
                prop.SetSpecular(0.08)
                prop.SetSpecularPower(45.0)
        self._rewire_iso_input()
        # 退出预览（全分辨率）是重计算，大数据走后台线程；预览(降采样)数据小可同步渲染
        if not enabled and self._is_heavy_volume():
            self._render_pipeline_async(full_setup=False)
            return
        if enabled:
            # 预览是降采样数据，曲率/密度都不可靠：撤掉叠加层，只留骨面
            self._fracture_poly = None
            self._sync_fracture_actor()
        else:
            # 回到全分辨率：重算骨折线并装上叠加层
            self._fracture_poly = self._detect_fracture_poly()
            self._sync_fracture_actor()
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def _setup_initial_cursor(self):
        """设置3D视图的初始光标位置"""
        if not self._reader:
            return
            
        self._reader.Update()
        output = self._reader.GetOutput()
        
        if output:
            extent = output.GetExtent()
            spacing = output.GetSpacing()
            origin = output.GetOrigin()
            
            # 计算图像中心的世界坐标
            center_i = (extent[1] - extent[0]) // 2
            center_j = (extent[3] - extent[2]) // 2
            center_k = (extent[5] - extent[4]) // 2
            
            center_x = origin[0] + center_i * spacing[0]
            center_y = origin[1] + center_j * spacing[1]
            center_z = origin[2] + center_k * spacing[2]
            initial_world = (center_x, center_y, center_z)

            if _DEBUG_LOG:
                print(f"3D_INIT_DEBUG: 3D视图初始光标位置 - 世界坐标:{initial_world}")
            
            # 设置初始光标
            self.set_cursor_position(initial_world)

    def calibrate_3d_coordinates(self):
        """校准3D视图坐标系"""
        if not self._reader:
            return
            
        self._reader.Update()
        output = self._reader.GetOutput()
        
        # 获取图像的基本信息
        origin = output.GetOrigin()
        spacing = output.GetSpacing()
        extent = output.GetExtent()
        
        if _DEBUG_LOG:
            print(f"CALIBRATION: 3D视图图像原点: {origin}")
            print(f"CALIBRATION: 3D视图图像间距: {spacing}")
            print(f"CALIBRATION: 3D视图图像范围: {extent}")
        
        # 在3D空间中标记关键点进行校准
        self._mark_calibration_points(origin, spacing, extent)

    def _mark_calibration_points(self, origin, spacing, extent):
        """标记校准点"""
        # 标记原点
        origin_marker = vtkSphereSource()
        origin_marker.SetCenter(*origin)
        origin_marker.SetRadius(2.0)
        
        # 标记中心点
        center_x = origin[0] + (extent[1] + extent[0]) * spacing[0] / 2.0
        center_y = origin[1] + (extent[3] + extent[2]) * spacing[1] / 2.0
        center_z = origin[2] + (extent[5] + extent[4]) * spacing[2] / 2.0
        
        if _DEBUG_LOG:
            print(f"CALIBRATION: 3D视图中心点: ({center_x}, {center_y}, {center_z})")

    def reset_view(self):
        self._renderer.ResetCamera()
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    # ------------------------------------------------------------------
    # 多视角批量导出（前/后/左/右/上/下 六视角一次性存为 PNG）
    # ------------------------------------------------------------------
    def _capture_window(self, scale=2):
        """把当前渲染窗口抓成 vtkImageData（含 overlay 层）。"""
        render_win = self._vtk_widget.GetRenderWindow()
        render_win.Render()
        w2i = vtkWindowToImageFilter()
        w2i.SetInput(render_win)
        w2i.SetInputBufferTypeToRGB()
        w2i.ShouldRerenderOff()   # 已手动 Render，避免重复渲染
        if scale and scale > 1:
            w2i.SetScale(int(scale))
        w2i.Modified()
        w2i.Update()
        return w2i.GetOutput()

    def export_multi_view(self, folder, scale=2):
        """将当前三维模型按 6 个正交视角导出为 PNG。

        视角沿重建体数据的世界坐标轴取，标签按习惯命名：
        前位 / 后位 / 左侧位 / 右侧位 / 上(顶)位 / 下(底)位。
        若你的数据前-后轴对应其它世界轴，可在本方法内的 VIEWS 里
        调整方向向量与 up 向量。
        """
        render_win = self._vtk_widget.GetRenderWindow()
        renderer = self._renderer
        camera = renderer.GetActiveCamera()

        # 1) 保存当前相机状态，导出后还原
        saved = {
            "pos": camera.GetPosition(),
            "fp": camera.GetFocalPoint(),
            "up": camera.GetViewUp(),
            "angle": camera.GetViewAngle(),
            "near": camera.GetClippingRange()[0],
            "far": camera.GetClippingRange()[1],
        }

        # 2) 计算模型包围盒中心与半径（优先用三维 actor 自身包围盒）
        if self._actor is not None and self._actor.GetVisibility():
            b = self._actor.GetBounds()
        else:
            b = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            renderer.ComputeVisiblePropBounds(b)
        cx = 0.5 * (b[0] + b[1])
        cy = 0.5 * (b[2] + b[3])
        cz = 0.5 * (b[4] + b[5])
        dx, dy, dz = b[1] - b[0], b[3] - b[2], b[5] - b[4]
        radius = 0.5 * math.sqrt(dx * dx + dy * dy + dz * dz)
        if radius <= 0:
            radius = 1.0
        half_fov = math.radians(camera.GetViewAngle() / 2.0)
        dist = radius / math.sin(half_fov) * 1.08  # 留少量边距

        # 3) 临时隐藏 3D 光标，避免干扰导出图
        cursor_actors = [a for a in (self._visual_cursor_actor,
                                     self._interaction_cursor_actor) if a is not None]
        for a in cursor_actors:
            a.SetVisibility(False)

        # 6 个正交视角（方向向量 + up 向量，均与世界轴对齐）
        VIEWS = [
            ("前位",     (0, 0, 1),  (0, -1, 0)),
            ("后位",     (0, 0, -1), (0, -1, 0)),
            ("左侧位",   (-1, 0, 0), (0, -1, 0)),
            ("右侧位",   (1, 0, 0),  (0, -1, 0)),
            ("上(顶)位", (0, -1, 0), (0, 0, -1)),
            ("下(底)位", (0, 1, 0),  (0, 0, 1)),
        ]

        written = []
        try:
            for name, (vx, vy, vz), (ux, uy, uz) in VIEWS:
                camera.SetFocalPoint(cx, cy, cz)
                camera.SetPosition(cx + vx * dist, cy + vy * dist, cz + vz * dist)
                camera.SetViewUp(ux, uy, uz)
                camera.OrthogonalizeViewUp()
                renderer.ResetCameraClippingRange()
                img = self._capture_window(scale)
                path = os.path.join(folder, "3D_%s.png" % name)
                writer = vtkPNGWriter()
                writer.SetInputData(img)
                writer.SetFileName(path)
                writer.Write()
                written.append(os.path.basename(path))
        finally:
            # 还原相机与光标
            camera.SetPosition(*saved["pos"])
            camera.SetFocalPoint(*saved["fp"])
            camera.SetViewUp(*saved["up"])
            camera.SetViewAngle(saved["angle"])
            camera.SetClippingRange(saved["near"], saved["far"])
            renderer.ResetCameraClippingRange()
            for a in cursor_actors:
                a.SetVisibility(True)
            render_win.Render()

        return written

    def _on_save_multiview_clicked(self):
        if self._actor is None or not self._actor.GetVisibility():
            QtWidgets.QMessageBox.information(
                self, "提示", "当前没有可导出的三维模型，请先完成三维重建。")
            return
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择多视角图片保存目录", os.path.expanduser("~"))
        if not folder:
            return
        try:
            written = self.export_multi_view(folder, scale=2)
        except Exception as exc:   # 任何失败都弹窗，不静默崩溃
            QtWidgets.QMessageBox.critical(
                self, "导出失败", "多视角导出出错：\n%s" % exc)
            return
        QtWidgets.QMessageBox.information(
            self, "导出完成",
            "已导出 %d 张多视角图片到：\n%s\n\n文件：%s"
            % (len(written), folder, "、".join(written)))

    # ------------------------------------------------------------------
    # 截图（保存用户手动旋转后的当前视角，所见即所得，单张 PNG）
    # ------------------------------------------------------------------
    def save_screenshot(self, folder, scale=2):
        """保存当前视角（用户手动旋转后的角度）为单张 PNG，所见即所得。

        - 不重置相机角度，按用户旋转后的实际朝向抓取；
        - 临时隐藏 3D 光标以保证图片干净，抓取后还原；
        - 文件名带时间戳，重复点击不会相互覆盖。
        """
        render_win = self._vtk_widget.GetRenderWindow()
        renderer = self._renderer

        # 临时隐藏 3D 光标
        cursor_actors = [a for a in (self._visual_cursor_actor,
                                     self._interaction_cursor_actor) if a is not None]
        for a in cursor_actors:
            a.SetVisibility(False)
        try:
            img = self._capture_window(scale)
        finally:
            for a in cursor_actors:
                a.SetVisibility(True)
            render_win.Render()

        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(folder, "3D_截图_%s.png" % ts)
        writer = vtkPNGWriter()
        writer.SetInputData(img)
        writer.SetFileName(path)
        writer.Write()
        return path

    def _on_screenshot_clicked(self):
        if self._actor is None or not self._actor.GetVisibility():
            QtWidgets.QMessageBox.information(
                self, "提示", "当前没有三维模型，请先完成三维重建。")
            return
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择截图保存目录", os.path.expanduser("~"))
        if not folder:
            return
        try:
            path = self.save_screenshot(folder, scale=2)
        except Exception as exc:   # 任何失败都弹窗，不静默崩溃
            QtWidgets.QMessageBox.critical(
                self, "截图失败", "保存截图出错：\n%s" % exc)
            return
        QtWidgets.QMessageBox.information(
            self, "截图完成", "已保存当前视角截图：\n%s" % path)

    # ------------------------------------------------------------------
    # 切割（圈画去除）：左键画圈 → vtkImplicitSelectionLoop + vtkClipPolyData
    # ------------------------------------------------------------------
    def _on_cut_toggled(self, checked):
        """进入/退出切割模式。"""
        if checked:
            if self._actor is None or not self._actor.GetVisibility():
                self._cut_btn.setChecked(False)
                QtWidgets.QMessageBox.information(
                    self, "提示", "当前没有可切割的三维模型，请先完成三维重建。")
                return
            self._cut_mode = True
            # 按钮已改成纯图标（文字改为 tooltip 提示），
            # 这里同步更新提示文案，让用户知道当前处于切割模式
            self._cut_btn.setToolTip(
                "切割中：左键在三维图上圈画要去掉的部分，松开即删除该区域。"
                "点击本按钮退出切割模式（恢复旋转）。")
            # 切成 User 样式：左键不再旋转相机，留给画圈使用
            self._interactor.SetInteractorStyle(vtkInteractorStyleUser())
        else:
            self._exit_cut_mode()

    def _exit_cut_mode(self):
        self._cut_mode = False
        self._cut_drawing = False
        self._cut_screen_pts = []
        self._clear_cut_loop_actor()
        self._cut_btn.blockSignals(True)
        self._cut_btn.setChecked(False)
        self._cut_btn.setToolTip(
            "切割：进入后用鼠标左键在三维图上圈画要去掉的部分，松开即删除该区域；"
            "再点一次退出（恢复旋转）。可多次圈画，配合撤销/重置使用。")
        self._cut_btn.blockSignals(False)
        # 恢复旋转
        self._interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())

    def _clear_cut_loop_actor(self):
        """移除画圈过程的实时预览折线。"""
        if self._cut_loop_actor is not None:
            self._overlay_renderer.RemoveActor(self._cut_loop_actor)
            self._cut_loop_actor = None
            try:
                self._vtk_widget.GetRenderWindow().Render()
            except Exception:
                pass

    def _screen_to_world_on_focal_plane(self, sx, sy):
        """把屏幕坐标换算到「过相机焦点、垂直于视线」的世界平面上的一点。

        所有圈画点都落在同一平面，保证 vtkImplicitSelectionLoop 的回路是平面的。
        """
        renderer = self._renderer
        camera = renderer.GetActiveCamera()
        coord = vtkCoordinate()
        coord.SetCoordinateSystemToDisplay()
        coord.SetValue(float(sx), float(sy), 0.0)
        p_near = list(coord.GetComputedWorldValue(renderer))
        cam_pos = list(camera.GetPosition())
        focal = list(camera.GetFocalPoint())
        # 平面法向 = 相机视线方向（由相机指向焦点）
        n = [focal[i] - cam_pos[i] for i in range(3)]
        ln = math.sqrt(sum(v * v for v in n)) or 1.0
        n = [v / ln for v in n]
        # 射线 cam_pos -> p_near 与平面 (X - focal)·n = 0 求交
        d = [p_near[i] - cam_pos[i] for i in range(3)]
        den = sum(d[i] * n[i] for i in range(3))
        num = sum((focal[i] - cam_pos[i]) * n[i] for i in range(3))
        s = num / den if abs(den) > 1e-9 else 0.0
        return [cam_pos[i] + s * d[i] for i in range(3)]

    def _build_loop_polydata(self, screen_pts):
        """由屏幕点列构造世界坐标的闭合折线 vtkPolyData。"""
        world_pts = [self._screen_to_world_on_focal_plane(x, y) for x, y in screen_pts]
        # 去掉连续重复点
        uniq = []
        for p in world_pts:
            if not uniq or sum((p[i] - uniq[-1][i]) ** 2 for i in range(3)) > 1e-12:
                uniq.append(p)
        pts = vtkPoints()
        for p in uniq:
            pts.InsertNextPoint(p[0], p[1], p[2])
        if len(uniq) < 3:
            return None
        lines = vtkCellArray()
        lines.InsertNextCell(len(uniq) + 1)
        for i in range(len(uniq)):
            lines.InsertCellPoint(i)
        lines.InsertCellPoint(0)   # 自动闭合
        pd = vtkPolyData()
        pd.SetPoints(pts)
        pd.SetLines(lines)
        return pd

    def _update_cut_loop_preview(self):
        """画圈过程中实时显示回路折线。"""
        if len(self._cut_screen_pts) < 2:
            return
        pd = self._build_loop_polydata(self._cut_screen_pts)
        if pd is None:
            return
        if self._cut_loop_actor is None:
            mapper = vtkPolyDataMapper()
            mapper.ScalarVisibilityOff()
            actor = vtkActor()
            actor.SetMapper(mapper)
            prop = actor.GetProperty()
            prop.SetColor(1.0, 0.8, 0.1)      # 醒目黄色
            prop.SetLineWidth(2.0)
            prop.SetAmbient(1.0)
            prop.SetDiffuse(0.0)
            prop.LightingOff()
            self._overlay_renderer.AddActor(actor)
            self._cut_loop_actor = actor
            self._cut_loop_mapper = mapper
        self._cut_loop_mapper.SetInputData(pd)
        try:
            self._vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass

    def _ensure_cut_base(self):
        """确保已拿到当前网格与原始网格快照（首次切割时惰性初始化）。"""
        if self._current_polydata is None or self._original_polydata is None:
            # 取当前 mapper 上游的实际几何输出
            if self._normals is not None:
                self._normals.Update()
                base = self._normals.GetOutput()
            elif self._actor is not None and self._actor.GetMapper() is not None:
                base = self._actor.GetMapper().GetInput()
            else:
                raise RuntimeError("无法获取当前三维网格")
            if base is None or base.GetNumberOfPoints() == 0:
                raise RuntimeError("当前三维网格为空，无法切割")
            self._current_polydata = base
            self._original_polydata = vtkPolyData()
            self._original_polydata.DeepCopy(base)
            self._cut_history = []

    def _apply_cut(self):
        """按圈画的回路执行一次切割，去除回路以内的部分。"""
        self._ensure_cut_base()
        pd = self._build_loop_polydata(self._cut_screen_pts)
        if pd is None:
            self._cut_screen_pts = []
            return

        # 撤销快照：vtkClipPolyData 不会修改输入几何，因此存引用即可，
        # 无需 DeepCopy（实测大网格省十几~二十几 ms、并省下大量内存）。
        # 仅首帧快照用已深拷贝的 _original_polydata，避免引用到会重算的 filter 输出。
        if not self._cut_history:
            self._cut_history.append(self._original_polydata)
        else:
            self._cut_history.append(self._current_polydata)
        if len(self._cut_history) > 30:
            self._cut_history.pop(0)

        # 回路隐式函数：回路内部 EvaluateFunction 为负，
        # vtkClipPolyData 默认保留 >= 0 的部分 → 即去掉圈内、保留圈外。
        loop = vtkImplicitSelectionLoop()
        loop.SetLoop(pd.GetPoints())
        loop.SetAutomaticNormalGeneration(True)

        # 裁剪是纯 CPU 运算（VTK 无 GPU 版 clip），百万面网格可达 0.5~1.7s，
        # 期间给个等待光标，避免看起来像卡死。
        clip = vtkClipPolyData()
        clip.SetInputData(self._current_polydata)
        clip.SetClipFunction(loop)
        clip.SetValue(0.0)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            QtWidgets.QApplication.processEvents()
            clip.Update()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        out = clip.GetOutput()
        if out is None or out.GetNumberOfCells() == 0:
            # 整块被切掉视为误操作，回滚本次快照
            self._cut_history.pop()
            self._cut_screen_pts = []
            QtWidgets.QMessageBox.information(
                self, "提示", "该次圈画会把整个模型都去掉，已自动忽略。")
            return

        self._current_polydata = out
        mapper = self._actor.GetMapper()
        mapper.SetInputData(out)
        # 切割后几何不再带标量，统一按实体色显示
        try:
            mapper.SetScalarVisibility(False)
        except Exception:
            pass
        prop = self._actor.GetProperty()
        # 切开处会露出背面，给背面同样颜色，避免看起来被掏空
        prop.SetBackfaceCulling(False)
        bf = self._actor.GetBackfaceProperty()
        if bf is None:
            self._actor.SetBackfaceProperty(vtkProperty())
            bf = self._actor.GetBackfaceProperty()
        bf.SetColor(prop.GetColor())
        bf.SetAmbient(0.3)
        bf.SetDiffuse(0.8)
        bf.SetSpecular(0.05)

        self._cut_screen_pts = []
        self._cut_undo_btn.setEnabled(True)
        self._cut_reset_btn.setEnabled(True)
        try:
            self._vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass

    def _on_cut_undo(self):
        if not self._cut_history:
            return
        prev = self._cut_history.pop()
        self._current_polydata = prev
        self._actor.GetMapper().SetInputData(prev)
        self._cut_undo_btn.setEnabled(bool(self._cut_history))
        try:
            self._vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass

    def _on_cut_reset(self):
        if self._original_polydata is None:
            return
        self._cut_history = []
        restored = vtkPolyData()
        restored.DeepCopy(self._original_polydata)
        self._current_polydata = restored
        self._actor.GetMapper().SetInputData(restored)
        self._cut_undo_btn.setEnabled(False)
        try:
            self._vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass

    def _reset_cut_state(self):
        """重建管线（换阈值/换数据）时清除切割状态，恢复旋转。"""
        if getattr(self, "_cut_mode", False):
            self._exit_cut_mode()
        self._cut_screen_pts = []
        self._cut_history = []
        self._current_polydata = None
        self._original_polydata = None
        self._clear_cut_loop_actor()
        try:
            self._cut_undo_btn.setEnabled(False)
            self._cut_reset_btn.setEnabled(False)
        except Exception:
            pass
        # 换数据/换阈值后旧的三维测量点已无意义（世界坐标会变），一并清掉
        self._reset_measure3d_state()

    def _reset_measure3d_state(self):
        """清除三维测量状态（换数据、换阈值、重置管线时调用）。"""
        self._measure_mode_3d = False
        self._measure_pts_3d = []
        self._measure_items_3d = []
        try:
            self._measure3d_btn.blockSignals(True)
            self._measure3d_btn.setChecked(False)
            self._measure3d_btn.blockSignals(False)
            self._measure3d_clear_btn.setEnabled(False)
        except Exception:            # noqa: BLE001
            pass
        self._clear_measure3d_actors()

    def set_cursor_position(self, world_pos):
        """增强版3D光标设置 - 包含坐标系验证"""
        if world_pos is None:
            return
        
        # 确保只在第一次调用时初始化光标
        if self._interaction_cursor_actor is None:
            self._setup_cursor_actor()
            
        # 验证坐标范围
        if self._reader:
            self._reader.Update()
            output = self._reader.GetOutput()
            if output:
                extent = output.GetExtent()
                spacing = output.GetSpacing()
                origin = output.GetOrigin()
                
                # 检查世界坐标是否在数据范围内
                x, y, z = world_pos
                x_min = origin[0] + extent[0] * spacing[0]
                x_max = origin[0] + extent[1] * spacing[0]
                y_min = origin[1] + extent[2] * spacing[1]
                y_max = origin[1] + extent[3] * spacing[1]
                z_min = origin[2] + extent[4] * spacing[2]
                z_max = origin[2] + extent[5] * spacing[2]
                
                # 如果超出范围，调整坐标到边界
                original_pos = (x, y, z)
                x = max(x_min, min(x_max, x))
                y = max(y_min, min(y_max, y))
                z = max(z_min, min(z_max, z))
                
                world_pos = (x, y, z)
        
        # 设置光标位置
        if self._visual_cursor_actor:
            self._visual_cursor_actor.SetPosition(*world_pos)
        if self._interaction_cursor_actor:
            self._interaction_cursor_actor.SetPosition(*world_pos)
            
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def _setup_cursor_actor(self):
        """初始化双层光标：视觉十字 + 交互隐形球"""
        # 1. 视觉光标：手动构建3D十字 (三条正交线段)
        # 回退到 vtkLineSource 方案以确保兼容性，但优化参数使其看起来更像2D十字
        append = vtkAppendPolyData()
        
        # 长度设为 20mm (±10mm)
        extent = 10.0
        
        # X轴线段
        line_x = vtkLineSource()
        line_x.SetPoint1(-extent, 0, 0)
        line_x.SetPoint2(extent, 0, 0)
        append.AddInputConnection(line_x.GetOutputPort())
        
        # Y轴线段
        line_y = vtkLineSource()
        line_y.SetPoint1(0, -extent, 0)
        line_y.SetPoint2(0, extent, 0)
        append.AddInputConnection(line_y.GetOutputPort())
        
        # Z轴线段
        line_z = vtkLineSource()
        line_z.SetPoint1(0, 0, -extent)
        line_z.SetPoint2(0, 0, extent)
        append.AddInputConnection(line_z.GetOutputPort())
        
        visual_mapper = vtkPolyDataMapper()
        visual_mapper.SetInputConnection(append.GetOutputPort())
        
        self._visual_cursor_actor = vtkActor()
        self._visual_cursor_actor.SetMapper(visual_mapper)
        self._visual_cursor_actor.GetProperty().SetColor(self._colors.GetColor3d("Red"))
        self._visual_cursor_actor.GetProperty().SetLineWidth(1.0) # 关键：细线
        self._visual_cursor_actor.GetProperty().SetLighting(False) # 关键：禁用光照，扁平化显示
        self._visual_cursor_actor.GetProperty().SetAmbient(1.0) # 增强自发光
        self._visual_cursor_actor.GetProperty().SetDiffuse(0.0)
        self._visual_cursor_actor.PickableOff() # 视觉光标不参与拾取
        
        # 2. 交互光标：隐形球体 (比十字中心稍大，便于选中)
        sphere = vtkSphereSource()
        sphere.SetRadius(12.0) # 半径12mm，容易点中
        sphere.SetThetaResolution(16)
        sphere.SetPhiResolution(16)
        
        interaction_mapper = vtkPolyDataMapper()
        interaction_mapper.SetInputConnection(sphere.GetOutputPort())
        
        self._interaction_cursor_actor = vtkActor()
        self._interaction_cursor_actor.SetMapper(interaction_mapper)
        self._interaction_cursor_actor.GetProperty().SetOpacity(0.0) # 完全透明
        self._interaction_cursor_actor.PickableOn() # 允许拾取
        
        # 添加到 Overlay Renderer
        self._overlay_renderer.AddActor(self._visual_cursor_actor)
        self._overlay_renderer.AddActor(self._interaction_cursor_actor)
        
        # 更新Picker的拾取列表，只包含交互光标
        self._picker.InitializePickList()
        self._picker.AddPickList(self._interaction_cursor_actor)

    def clear(self):
        try:
            self._renderer.RemoveAllViewProps()
            if self._overlay_renderer:
                self._overlay_renderer.RemoveAllViewProps()
        except Exception:
            pass
        self._reader = None
        self._actor = None
        self._mapper = None
        self._normals = None
        self._connectivity = None
        self._iso = None
        self._visual_cursor_actor = None
        self._interaction_cursor_actor = None
        self._detection_actors = []
        self._segmentation_actors = []
        self._picker.InitializePickList()
        # 清空三维测量（模型都没了，测量点没有意义）
        self._reset_measure3d_state()
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def clear_detection_markers(self):
        if self._overlay_renderer:
            for actor in self._detection_actors:
                self._overlay_renderer.RemoveActor(actor)
        self._detection_actors = []
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def set_detection_markers(self, markers):
        self.clear_detection_markers()
        if not markers:
            return
        for marker in markers:
            center = marker.get("center")
            radius = marker.get("radius", 2.0)
            if not center:
                continue
            sphere = vtkSphereSource()
            sphere.SetCenter(*center)
            sphere.SetRadius(float(radius))
            sphere.SetThetaResolution(20)
            sphere.SetPhiResolution(20)
            mapper = vtkPolyDataMapper()
            mapper.SetInputConnection(sphere.GetOutputPort())
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(0.2, 1.0, 0.4)
            actor.GetProperty().SetOpacity(0.85)
            actor.GetProperty().SetLighting(False)
            actor.PickableOff()
            if self._overlay_renderer:
                self._overlay_renderer.AddActor(actor)
            self._detection_actors.append(actor)
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def clear_segmentation_mask(self):
        if self._overlay_renderer:
            for a in self._segmentation_actors:
                self._overlay_renderer.RemoveActor(a)
        self._segmentation_actors = []
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def set_segmentation_mask(self, mask, origin, spacing):
        self.clear_segmentation_mask()
        # 根据用户需求，3D视图不再显示分割结果，仅在MPR视图显示
        return

        if mask is None:
            return
        import numpy as np
        arr = np.ascontiguousarray(mask)
        if arr.ndim != 3:
            raise RuntimeError("分割结果维度异常")
        labels = [int(x) for x in np.unique(arr) if int(x) > 0]
        if not labels:
            return
        palette = [
            (0.94, 0.33, 0.31),
            (0.30, 0.74, 0.93),
            (0.50, 0.80, 0.50),
            (0.98, 0.77, 0.19),
            (0.59, 0.40, 0.74),
            (1.00, 0.60, 0.40),
            (0.35, 0.78, 0.98),
            (0.87, 0.30, 0.86),
        ]
        depth, height, width = arr.shape
        for i, lbl in enumerate(labels):
            bin_arr = (arr == lbl).astype(np.uint8, copy=False)
            importer = vtkImageImport()
            importer.SetDataScalarTypeToUnsignedChar()
            data_string = bin_arr.tobytes()
            importer.CopyImportVoidPointer(data_string, len(data_string))
            importer.SetNumberOfScalarComponents(1)
            importer.SetDataExtent(0, width - 1, 0, height - 1, 0, depth - 1)
            importer.SetWholeExtent(0, width - 1, 0, height - 1, 0, depth - 1)
            if spacing:
                importer.SetDataSpacing(*spacing)
            if origin:
                importer.SetDataOrigin(*origin)
            importer.Update()
            surface = vtkFlyingEdges3D()
            surface.SetInputConnection(importer.GetOutputPort())
            surface.SetValue(0, 0.5)
            normals = vtkPolyDataNormals()
            normals.SetInputConnection(surface.GetOutputPort())
            normals.SetFeatureAngle(60.0)
            normals.ConsistencyOn()
            normals.SplittingOff()
            mapper = vtkPolyDataMapper()
            mapper.SetInputConnection(normals.GetOutputPort())
            actor = vtkActor()
            actor.SetMapper(mapper)
            color = palette[i % len(palette)]
            actor.GetProperty().SetColor(*color)
            actor.GetProperty().SetOpacity(0.40)
            actor.GetProperty().SetLighting(True)
            if self._overlay_renderer:
                self._overlay_renderer.AddActor(actor)
            self._segmentation_actors.append(actor)
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()


def _reader_needs_consistent_files(reader_cls) -> bool:
    """读取器是否**必须**拿到同尺寸的第一批 DICOM

    只有 ``vtkDICOMImageReader`` 会按首图尺寸开缓冲、尺寸一变就写越界段错误，
    所以 CT / MR 那条路必须先剔除尺寸少数派。X 线（``XraySeriesReader``，
    pydicom 装配）与图片序列（``ImageSeriesReader``）自己会把不同尺寸的图
    居中补边归一，尺寸不齐是常态 —— 07 的 5 张 DX 各不相同的尺寸若被当成
    「脏数据」剔除，用户看到的就是「只显示第一张图片」（v1.9.8）。
    """
    if isinstance(reader_cls, type) and \
            issubclass(reader_cls, vtkDICOMImageReader):
        return True
    return False


class DicomLoadWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, str)
    finished_success = QtCore.pyqtSignal(object, object, object, int, object)
    error = QtCore.pyqtSignal(str)
    canceled = QtCore.pyqtSignal()

    def __init__(self, folder: str, file_uids: list = None,
                 analyzer_fn=analyze_dicom_series,
                 reader_cls=vtkDICOMImageReader,
                 allow_single_slice=False,
                 require_staging=False,
                 parent=None):
        super().__init__(parent)
        self._folder = folder
        self._file_uids = file_uids or []
        self._analyzer_fn = analyzer_fn
        self._reader_cls = reader_cls
        # X 线（DR/DX）一幅片子就是一个序列，Z 维必然是 1；CT 体数据则要求
        # 至少两层，否则视为数据异常（原行为保留）。
        self._allow_single_slice = allow_single_slice
        # MRI：文件名是 SOPInstanceUID，字母序 != 层序，必须靠临时目录里
        # 的 0000_ 序号前缀把层序固化下来，因此禁用「直接读原目录」快路径。
        self._require_staging = require_staging
        self._stop = False
        self._temp_folder = None

    def stop(self):
        self._stop = True

    @staticmethod
    def _header_dims(folder: str, limit: int = 6):
        """从目录里前几张 .dcm 的**文件头**取 (rows, cols)，取不到返回 None

        只 peek 头（``stop_before_pixels=True``），不读像素，千张序列也很快。
        取多个文件是为了容错：个别 PACS 导出会往同一个目录里塞一张零尺寸的
        占位图，只看第一张会误判成「VTK 读对了」。
        """
        if not folder or not os.path.isdir(folder):
            return None
        try:
            names = sorted(f for f in os.listdir(folder)
                           if f.lower().endswith(".dcm")
                           and os.path.isfile(os.path.join(folder, f)))
        except Exception:                  # noqa: BLE001
            return None
        rows = cols = None
        for n in names[:limit]:
            try:
                ds = pydicom.dcmread(os.path.join(folder, n),
                                     stop_before_pixels=True, force=True)
                r = int(getattr(ds, "Rows", 0) or 0)
                c = int(getattr(ds, "Columns", 0) or 0)
            except Exception:              # noqa: BLE001
                continue
            if r <= 0 or c <= 0:
                continue
            rows, cols = r, c
            break
        if rows and cols:
            return (rows, cols)
        return None

    def _copy_selected_files(self, folder: str, file_uids: list,
                             keep_inconsistent: bool = False) -> tuple:
        """把选中的 .dcm 暂存到纯 DICOM 目录（``keep_inconsistent`` 为 True
        时不剔除尺寸少数派，供 pydicom 装配类读取器使用）"""
        import tempfile
        import shutil
        if not file_uids:
            # 未指定文件列表时，仍需过滤掉目录中的非 DICOM 文件（如 'ko'、'*.xml'）。
            # vtkDICOMImageReader.SetDirectoryName 会无差别扫描目录内所有文件，
            # 遇到损坏/非 DICOM 文件可能直接崩溃，因此这里统一暂存到临时目录。
            return self._stage_valid_dicom(folder,
                                           keep_inconsistent=keep_inconsistent)

        self.progress.emit(25, f"正在准备序列文件 ({len(file_uids)}张)...")
        temp_folder = tempfile.mkdtemp(prefix="dicom_series_")

        all_files = [f for f in os.listdir(folder) if f.lower().endswith('.dcm')]
        uid_to_file = {}
        for filename in all_files:
            base_name = filename[:-4] if filename.lower().endswith('.dcm') else filename
            for uid in file_uids:
                if base_name == uid:
                    uid_to_file[uid] = filename
                    break

        # 目录纯净（全部为目标 .dcm、无其他文件）且文件名排序与读取顺序一致时，
        # 直接使用原目录，省去整卷数百 MB 的临时拷贝（低配机磁盘 I/O 大头）。
        # 层序依据：vtkDICOMImageReader 按文件名排序读取（无过滤路径同样依赖该行为）。
        try:
            all_entries = [f for f in os.listdir(folder)
                           if os.path.isfile(os.path.join(folder, f))]
        except Exception:
            all_entries = []
        dcm_entries = [f for f in all_entries if f.lower().endswith('.dcm')]
        base_names = sorted((f[:-4] if f.lower().endswith('.dcm') else f)
                            for f in dcm_entries)
        if (file_uids
                and not self._require_staging
                and len(uid_to_file) == len(file_uids)
                and len(all_entries) == len(dcm_entries) == len(file_uids)
                and list(file_uids) == sorted(file_uids)   # 列表顺序须与文件名字母序一致（即层序）
                and base_names == sorted(file_uids)):
            self.progress.emit(40, f"直接读取序列文件 ({len(file_uids)}张)...")
            return folder, None, len(file_uids)

        copied_count = 0
        for idx, uid in enumerate(file_uids):
            if self._stop:
                return folder, None, 0
            if uid in uid_to_file:
                filename = uid_to_file[uid]
                src_path = os.path.join(folder, filename)
                new_filename = f"{idx:04d}_{filename}"
                dst_path = os.path.join(temp_folder, new_filename)
                try:
                    shutil.copy2(src_path, dst_path)
                    copied_count += 1
                except Exception as copy_error:
                    print(f"复制文件失败: {src_path} -> {dst_path}, 错误: {copy_error}")
                    continue

        if copied_count > 0:
            return temp_folder, temp_folder, copied_count
        else:
            try:
                shutil.rmtree(temp_folder)
            except Exception:
                pass
            return folder, None, 0

    def _stage_valid_dicom(self, folder: str,
                           keep_inconsistent: bool = False) -> tuple:
        """把目录中真正的 DICOM 文件暂存到临时目录，返回 (目录, 临时目录, 数量)。

        用于规避 vtkDICOMImageReader 在扫描目录时解析到非 DICOM 文件而崩溃。
        仅在确实存在需要剔除的文件时才创建临时目录，否则原样返回以免多余拷贝。
        ``keep_inconsistent`` 为 True 时**不**按尺寸剔少数派（pydicom 装配类
        读取器自己会补边，07 那种每张尺寸都不同的 X 线片组就是这么保住的）。
        """
        import tempfile
        import shutil

        try:
            entries = [f for f in os.listdir(folder)
                       if os.path.isfile(os.path.join(folder, f))]
        except Exception:
            return folder, None, 0

        def _is_dicom(name: str) -> bool:
            path = os.path.join(folder, name)
            try:
                with open(path, 'rb') as fh:
                    fh.seek(128)
                    if fh.read(4) != b'DICM':
                        return False
                # 头部合法，再校验 DICOM 结构是否可解析
                pydicom.dcmread(path, stop_before_pixels=True, force=True)
                return True
            except Exception:
                return False

        # 优先用扩展名白名单加速判断，再用内容校验
        candidates = [f for f in entries if f.lower().endswith('.dcm')]
        skipped = [f for f in entries
                   if not f.lower().endswith('.dcm')]
        valid = [f for f in candidates if _is_dicom(f)]

        # 尺寸不一致会让 vtkDICOMImageReader 写越界段错误，这里先剔除少数派。
        # 但 X 线 / 图片路径走的是 pydicom 装配或自研读取器（会居中补边），
        # 尺寸不齐是**常态**（实测 07 的 5 张正位/侧位/斜位各不同尺寸），
        # 剔除等于把片子丢掉，故只在 VTK 读取器下才做这道保护。
        if len(valid) > 1 and not keep_inconsistent:
            consistent = pick_consistent_files(folder, valid)
            if len(consistent) != len(valid):
                skipped = skipped + [f for f in valid if f not in set(consistent)]
                valid = consistent

        # 若没有需要剔除的文件，直接使用原目录
        if not skipped and len(valid) == len(candidates) and valid:
            return folder, None, len(valid)

        if not valid:
            return folder, None, 0

        if not skipped and len(valid) == len(candidates) and valid:
            return folder, None, len(valid)

        temp_folder = tempfile.mkdtemp(prefix="dicom_series_")
        copied = 0
        for idx, name in enumerate(valid):
            if self._stop:
                return folder, None, 0
            try:
                shutil.copy2(os.path.join(folder, name),
                             os.path.join(temp_folder, f"{idx:04d}_{name}"))
                copied += 1
            except Exception:
                continue

        if copied > 0:
            return temp_folder, temp_folder, copied
        try:
            shutil.rmtree(temp_folder)
        except Exception:
            pass
        return folder, None, 0

    def run(self):
        try:
            # 先暂存有效 DICOM 文件，避免分析器/读取器在原始目录中
            # 扫描到非 DICOM 文件（如 'ko'、'*.xml'）而崩溃
            folder = self._folder
            # 后重建序列（椎间盘/3D 批处理）常混入少量尺寸不同的图像，
            # 会让 vtkDICOMImageReader 写越界段错误，先剔除这些少数派。
            # 但 X 线 / 图片走 pydicom 装配（会居中补边），尺寸不齐是常态
            # （07 的 5 张正位/侧位/斜位各不同尺寸），剔除会丢片子，
            # 这类读取器整夹交给它自己归一。
            consistent_only = _reader_needs_consistent_files(self._reader_cls)
            if self._file_uids and consistent_only:
                self._file_uids = dominant_dimension_uids(self._folder,
                                                          self._file_uids)
            folder, temp_folder, copied_count = self._copy_selected_files(
                folder, self._file_uids,
                keep_inconsistent=not consistent_only)
            if temp_folder:
                self._temp_folder = temp_folder

            if self._stop:
                self.canceled.emit()
                return

            self.progress.emit(20, "正在分析DICOM元数据...")
            params = self._analyzer_fn(folder)
            window_center = params.get("window_center")
            window_width = params.get("window_width")
            image_count = params.get("image_count", 0)
            if copied_count > 0:
                image_count = copied_count

            if self._stop:
                self.canceled.emit()
                return

            self.progress.emit(50, "正在初始化VTK读取器...")
            reader = self._reader_cls()
            reader.SetDirectoryName(folder)

            self.progress.emit(70, f"正在读取DICOM数据 ({image_count}张图像)...")
            reader.Update()

            output = reader.GetOutput()
            if output is None:
                raise RuntimeError("无法读取DICOM数据，输出为空")
            extent = output.GetExtent()
            # 兜底一：VTK 读到 64x64 这种明显退化的尺寸，直接当失败拦下
            if extent[1] - extent[0] <= 0 or extent[3] - extent[2] <= 0:
                raise RuntimeError("DICOM数据维度异常")
            # Z 维只允许单层的情况见 allow_single_slice 的说明
            if extent[5] - extent[4] <= 0 and not self._allow_single_slice:
                raise RuntimeError("DICOM数据维度异常")
            # 兜底二：**VTK 与 pydicom 尺寸交叉校验**（v1.9.7）
            #   vtkDICOMImageReader 的 DICOMParser 遇到它不认识的元素会
            #   解析到一半就放弃，Rows/Columns 取不到 → 退回默认的 64x64，
            #   画面是一小块黑图，而 Z 维还在所以上层的「维度异常」拦不住。
            #   实测 PACS 导出的 DX 就栽在 (0088,0200) PresentationState
            #   Reference Sequence 上（8 张 2012x2012 全被读成 64x64）。
            #   这里拿 pydicom 的头做基准，不一致就换 pydicom 装配重读一遍。
            if isinstance(self._reader_cls, type) and \
                    issubclass(self._reader_cls, vtkDICOMImageReader):
                expected = self._header_dims(folder)
                # expected 是 (rows, cols)，VTK extent 的 X 是列、Y 是行 ——
                # 非方阵（如 96x128）最容易在这里写反，一写反就会把「读对了」
                # 当成「读错了」，白白触发一次无谓的 pydicom 重读（v1.9.7）
                _e_rows, _e_cols = expected
                if (extent[1] - extent[0] + 1 != _e_cols
                        or extent[3] - extent[2] + 1 != _e_rows):
                    print("VTK 读取维度异常（%dx%d），与文件头 %dx%d 不符，"
                          "改用 pydicom 装配重读"
                          % (extent[1] - extent[0] + 1, extent[3] - extent[2] + 1,
                             _e_rows, _e_cols))
                    try:
                        alt = XraySeriesReader()
                        alt.SetDirectoryName(folder)
                        alt.Update()
                        alt_out = alt.GetOutput()
                        if alt_out is not None:
                            aext = alt_out.GetExtent()
                            if (aext[1] - aext[0] + 1 == _e_cols
                                    and aext[3] - aext[2] + 1 == _e_rows):
                                reader = alt
                                output = alt_out
                                extent = aext
                    except Exception as alt_err:   # noqa: BLE001
                        print("pydicom 装配兜底失败: %s" % alt_err)

            if self._stop:
                self.canceled.emit()
                return

            self.progress.emit(90, "正在初始化视图...")
            self.finished_success.emit(reader, window_center, window_width, image_count, self._temp_folder)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.progress.emit(100, "完成")


class RefreshWorker(QtCore.QThread):
    finished = QtCore.pyqtSignal(list)
    error = QtCore.pyqtSignal(str)
    
    def __init__(self, scan_dirs):
        super().__init__()
        self._scan_dirs = scan_dirs
        self._stop = False
    
    def stop(self):
        self._stop = True
    
    def run(self):
        try:
            patients = collect_ct_patients(self._scan_dirs, min_images=0)
            if not self._stop:
                self.finished.emit(patients)
        except Exception as e:
            if not self._stop:
                self.error.emit(str(e))

# ---------------------------------------------------------------------------
# 患者列表 · 序列缩略图（v1.6.0）
#
# 设计要点：
#   1. 序列缩略图只读取「序列中段」的 1~3 个 DICOM 文件，按文件自带的
#      WindowCenter/WindowWidth 做灰度映射，不加载整个序列，避免卡顿；
#   2. 生成过程放在线程池（QRunnable）里，主线程只负责把 QImage 转成
#      QPixmap 并设置到树上（QPixmap 属于 GUI 资源，必须在主线程创建）；
#   3. 结果按 series_uid|folder 缓存，刷新列表时不会重复读盘。
# ---------------------------------------------------------------------------

def _dicom_first_number(value):
    """把 DICOM 多值元素（MultiValue / list）安全地转成单个浮点数"""
    if value is None:
        return None
    try:
        if not isinstance(value, (int, float)):
            if hasattr(value, "__len__"):
                value = value[0]
        return float(value)
    except Exception:
        return None


def _preview_candidate_paths(folder: str, file_uids: list, count: int = 3):
    """挑选「本序列」的若干 DICOM 文件作为预览候选

    file_uids 一般是文件名（不含 .dcm）；若无法与目录内文件对应
    （部分 XFD 序列写的是 SOPInstanceUID），退化为目录内 .dcm 按名排序。

    关键（v1.7.4）：早先要求 file_uids 至少匹配到 2 个文件才采用，
    于是「只有 1 张」的 X 线序列（正位/侧位各一张）会被判为匹配失败，
    退化成「目录内按名排序取中段」——一个患者目录里有 4 个序列时，
    每行的缩略图都抽到同一张文件（实测 4 行缩略图完全相同），
    看起来「预览只有一张」，而点开检测用的却是该序列自己的那张。
    现在只要匹配到 1 个就认定是本序列的文件，再从中段抽样。
    """
    if not folder or not os.path.isdir(folder):
        return []
    try:
        names = [f for f in os.listdir(folder) if f.lower().endswith(".dcm")]
    except Exception:
        return []
    if not names:
        # 纯图片目录（PACS 导出的 PNG/JPG）没有 .dcm：若这里直接返回空，
        # 列表里每个图片文件夹都只有一张占位灰图。补上常见图片当预览源。
        try:
            names = sorted(f for f in os.listdir(folder)
                           if f.lower().endswith(IMAGE_FILE_EXTS))
        except Exception:
            return []
        if not names:
            return []

    paths = []
    if file_uids:
        base_map = {}
        for name in names:
            base = name[:-4] if name.lower().endswith(".dcm") else name
            base_map.setdefault(base, name)
        matched = [base_map[u] for u in file_uids if u in base_map]
        if matched:
            paths = [os.path.join(folder, n) for n in matched]
    if not paths:
        names.sort()
        paths = [os.path.join(folder, n) for n in names]

    total = len(paths)
    if total <= count:
        return paths
    mid = total // 2
    step = max(1, total // 8)
    idxs = [mid]
    for delta in (step, -step, 2 * step, -2 * step):
        i = mid + delta
        if 0 <= i < total and i not in idxs:
            idxs.append(i)
        if len(idxs) >= count:
            break
    return [paths[i] for i in idxs[:count]]


def _read_preview_image(path: str, modality=None):
    """读取单个 DICOM 文件并做窗宽窗位归一化，返回 (QImage, 评分)

    评分用归一化后图像的「标准差」衡量信息量：CT 序列里靠近两端的层
    往往几乎全是空气，标准差很小，用它可以避免抽到一张全黑的预览图。

    v1.7.4：X 线（DR/CR）的 DICOM 窗值量纲与 CT 完全不同（曾见
    WW6880/WL12943），直接套用会让预览发灰/发黑，与主视图
    `_xr_window_level` 的 1%~99% 分位不一致。这里对 X 线统一走分位拉伸。
    """
    try:
        ds = pydicom.dcmread(path, force=True)
    except Exception:
        return None, -1.0
    try:
        arr = ds.pixel_array
    except Exception:
        return None, -1.0
    if arr is None or arr.size == 0:
        return None, -1.0

    spp = 1
    try:
        spp = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    except Exception:
        spp = 1
    if arr.ndim == 3 and spp == 1:
        arr = arr[0]  # 多帧图像只取首帧

    # 彩色图像：直接贴图，不做窗宽窗位
    if arr.ndim == 3 and arr.shape[2] in (3, 4):
        rgb = arr[:, :, :3]
        if rgb.dtype != np.uint8:
            rgb = np.clip(rgb, 0, 255).astype(np.uint8)
        h, w, _ = rgb.shape
        qimg = QtGui.QImage(rgb.tobytes(), w, h, 3 * w,
                            QtGui.QImage.Format_RGB888).copy()
        return qimg, float(rgb.std())

    # 灰度：先做 Modality LUT（RescaleSlope / RescaleIntercept）
    try:
        from pydicom.pixel_data_handlers.util import apply_modality_lut
        arr = apply_modality_lut(arr, ds)
    except Exception:
        try:
            slope = _dicom_first_number(getattr(ds, "RescaleSlope", None)) or 1.0
            intercept = _dicom_first_number(getattr(ds, "RescaleIntercept", None)) or 0.0
            arr = arr.astype(np.float32) * slope + intercept
        except Exception:
            pass

    a = np.asarray(arr).astype(np.float32)
    if a.ndim == 3:
        a = a[:, :, 0]
    valid = a[np.isfinite(a)]
    if valid.size == 0:
        return None, -1.0

    wc = _dicom_first_number(getattr(ds, "WindowCenter", None))
    ww = _dicom_first_number(getattr(ds, "WindowWidth", None))
    is_xr = is_xr_modality(str(modality or "").strip().upper())
    if is_xr:
        # X 线：忽略设备窗值，按分位拉伸（与主视图同一基准）
        ww = wc = None
    if ww is None or ww <= 0 or wc is None:
        # 设备未写入窗值：用 1%~99% 分位拉伸，保证预览有内容
        lo = float(np.percentile(valid, 1.0))
        hi = float(np.percentile(valid, 99.0))
        if hi - lo < 1e-3:
            lo = float(valid.min())
            hi = float(valid.max())
        if hi - lo < 1e-3:
            hi = lo + 1.0
    else:
        lo = wc - ww / 2.0
        hi = wc + ww / 2.0

    out = np.clip((a - lo) / max(1e-6, hi - lo), 0.0, 1.0)
    try:
        if str(getattr(ds, "PhotometricInterpretation", "")).strip().upper() == "MONOCHROME1":
            out = 1.0 - out
    except Exception:
        pass
    img8 = (out * 255.0 + 0.5).astype(np.uint8)
    h, w = img8.shape
    qimg = QtGui.QImage(img8.tobytes(), w, h, w,
                        QtGui.QImage.Format_Grayscale8).copy()
    return qimg, float(img8.std())


def build_series_thumbnail_image(folder: str, file_uids: list, size: int = 96,
                                 modality=None):
    """生成一个序列的预览图（QImage）；失败返回 None"""
    best_img, best_score = None, -1.0
    for path in _preview_candidate_paths(folder, file_uids, 3):
        qimg, score = _read_preview_image(path, modality)
        if qimg is None or qimg.isNull():
            continue
        if score > best_score:
            best_img, best_score = qimg, score
    if best_img is None:
        return None
    return best_img.scaled(size, size, QtCore.Qt.KeepAspectRatio,
                           QtCore.Qt.SmoothTransformation)


class SeriesThumbnailWorker(QtCore.QThread):
    """常驻的缩略图生成工作线程

    队列由主线程 append（GIL 保证原子），线程侧轮询消费；每次只生成一张
    缩略图（约 30ms），因此轮询间隔不会造成可感知的延迟。

    这里刻意不使用 QThreadPool + QRunnable：实测把信号挂在 QRunnable 上、
    或在主线程把 QRunnable 的信号连到槽函数后投入线程池，回调都可能无法
    送达（run() 返回后 QRunnable 被析构，PyQt 只在信号被 emit 时才建立
    连接，排队期间连接尚不存在）。用 QThread 子类自带信号则稳定可靠。
    """

    done = QtCore.pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = []
        self._stop = False

    def submit(self, key: str, folder: str, file_uids: list, size: int = 96,
               modality=None):
        self._queue.append((key, folder, list(file_uids or []), int(size),
                            modality))

    def stop(self):
        self._stop = True
        self._queue = []

    def run(self):
        while not self._stop:
            if not self._queue:
                self.msleep(20)
                continue
            key, folder, file_uids, size, modality = self._queue.pop(0)
            img = None
            try:
                img = build_series_thumbnail_image(folder, file_uids, size,
                                                   modality)
            except Exception as e:
                print(f"序列缩略图生成失败 [{key}]: {e}")
            if self._stop:
                return
            try:
                self.done.emit(key, img)
            except Exception as e:
                print(f"序列缩略图回调失败 [{key}]: {e}")


# 拖放 MIME：序列卡片拖到阅读区时携带的载荷类型
SERIES_MIME = "application/x-ct3d-series"


def _draw_count_badge(pixmap, count):
    """在 pixmap 右上角画一个「N」的计数角标（原地修改）。

    为什么单独抽出来：这段曾经把程序打崩 —— 用了
    ``painter.drawText(x, y, str)`` 这个**并不存在**的三参重载
    （PyQt5 只有 ``drawText(x, y, w, h, flags, str)`` 以及各种
    QRect/QPoint 版本），一多选拖动就抛
    ``TypeError: arguments did not match any overloaded call``。
    现在统一用 ``drawText(QRect, flags, str)``。

    数字用短横线代替笔画是刻意的降级：无显示环境（如 offscreen 验收）
    ``QFontDatabase().families()`` 为空，画不出字。画不出来时只留蓝底圆点，
    仍然能传达「多选了若干个」，不至于一个角标都没有。
    """
    try:
        w, h = pixmap.width(), pixmap.height()
        size = max(16, min(26, h // 3))
        badge = QtCore.QRect(w - size - 2, 2, size, size)
        painter = QtGui.QPainter(pixmap)
        try:
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setBrush(QtGui.QColor(42, 130, 218))
            painter.setPen(QtGui.QPen(QtCore.Qt.white, 2))
            painter.drawEllipse(badge)
            painter.setPen(QtGui.QPen(QtCore.Qt.white))

            has_font = bool(QtGui.QFontDatabase().families())
            if has_font:
                font = painter.font()
                # 用 pixelSize 而不是 pointSize：图标是固定像素尺寸的小图，
                # 点值还要按 DPI 换算，不同缩放下大小会飘。
                font.setPixelSize(max(11, int(size * 0.62)))
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(badge, QtCore.Qt.AlignCenter, str(int(count)))
            else:
                # 无字体环境：画一条白横线示意「有计数」，保证角标可见
                bar = QtCore.QRect(badge.center().x() - size // 5,
                                   badge.center().y() - 1,
                                   max(6, size // 3), 2)
                painter.fillRect(bar, QtCore.Qt.white)
        finally:
            painter.end()
    except Exception:                                 # noqa: BLE001
        # 角标只是装饰，画不出来绝不能影响拖拽本身
        pass


def _series_payload_from_item(item):
    """从患者树节点解析出「可直接交给 _load_series」的序列字典。

    序列节点本身只存序列信息，患者姓名/性别/检查号挂在父节点上，双击加载
    与拖放加载都需要这些字段（骨龄要性别、写检测记录要检查号），所以两条
    路径必须走同一个解析函数，否则拖进来的序列会缺患者上下文。
    """
    if item is None:
        return None
    item_data = item.data(0, QtCore.Qt.UserRole)
    if not item_data:
        return None
    if isinstance(item_data, dict) and item_data.get("type") == "series":
        series = item_data.get("data")
        parent_item = item.parent()
        if parent_item is not None:
            parent_data = parent_item.data(0, QtCore.Qt.UserRole)
            if isinstance(parent_data, dict) and parent_data.get("type") == "patient":
                pd = parent_data.get("data") or {}
                series["patient_name"] = pd.get("pat_name", "Unknown")
                series["patient_id"] = pd.get("pat_id", "-")
                series["patient_sex"] = pd.get("pat_sex", "")
                series["patient_modality"] = pd.get("modality", "")
                series["patient_access"] = pd.get("pat_access", "")
                series["patient_study_date"] = pd.get("study_date", "")
        return series
    if isinstance(item_data, dict) and item_data.get("type") == "patient":
        return None
    return item_data if isinstance(item_data, dict) else None


class SeriesDragTreeWidget(QtWidgets.QTreeWidget):
    """支持把「序列卡片」拖到阅读区的患者树。

    只序列行可拖（患者行拖过去没有意义），拖动载荷是序列数据字典本身，
    放进一个自增 token 的注册表，MIME 里只带 token 字符串 —— 序列数据
    可能含非 ASCII 文本与嵌套结构，序列化成 JSON 反而容易出问题。

    **支持 Ctrl/Shift 多选后一起拖**（v1.9.9）：脊柱拼接常常要正位+侧位一起
    拼，之前只取 `currentItem()` 导致只能拖一个序列、拼不了。选中多个序列行
    （Ctrl 点选 / Shift 连选）后拖动，MIME 里放一个「批量 token」，指向
    注册表里的**列表**；单选时仍然放单个字典，保持既有拖放路径不变。
    """

    # 批量拖动用的 MIME：值是「批量 token」，指向注册表里的 series 列表
    SERIES_BATCH_MIME = "application/x-ct3d-series-batch"

    # 右键导出信号：(item_at_pos, global_pos)。
    # **必须声明在类体里**：`pyqtSignal` 只有作为类属性时 PyQt 才会在元类里
    # 建出真正的 signal 对象。写进 __init__ 就只是普通实例属性，
    # 连 `.connect()` 都没有 —— 表现为 `AttributeError:
    # 'PyQt5.QtCore.pyqtSignal' object has no attribute 'connect'`，
    # 且只在构造 SeriesDragTreeWidget 时才炸，主程序直接起不来。
    export_requested = QtCore.pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_payloads = {}     # token -> series dict 或 list[series]
        self._drag_seq = 0
        self.setDragEnabled(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragOnly)
        self.setDefaultDropAction(QtCore.Qt.CopyAction)
        # 多选：Ctrl 点选加/减、Shift 连选一段。脊柱拼接与三维重建都要多选。
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        # 右键导出：患者行导出全部序列，序列行导出单个序列。
        # 菜单由 MainWindow 构建（要访问序列数据与导出逻辑），这里只负责发信号。
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def _on_context_menu(self, pos):
        item = self.itemAt(pos)
        self.export_requested.emit(item, self.viewport().mapToGlobal(pos))

    def register_payload(self, payload):
        """登记一份拖放载荷，返回 token"""
        self._drag_seq += 1
        token = "ct3d-series-%d" % self._drag_seq
        self._drag_payloads[token] = payload
        # 只保留最近 50 份，防止长时间运行后无上限增长
        if len(self._drag_payloads) > 50:
            for k in list(self._drag_payloads)[:-50]:
                self._drag_payloads.pop(k, None)
        return token

    def payload(self, token):
        return self._drag_payloads.get(token)

    def _dragged_series_items(self):
        """本次拖动涉及的序列行。

        优先用**选中项**（Ctrl/Shift 多选），没有选中才退回当前项。
        患者行与提示行一律跳过（它们没有 `folder`，拼不出东西）。
        """
        items = self.selectedItems()
        if not items:
            cur = self.currentItem()
            items = [cur] if cur is not None else []

        picked = []
        for it in items:
            if it is None:
                continue
            data = it.data(0, QtCore.Qt.UserRole)
            if not (isinstance(data, dict) and data.get("type") == "series"):
                continue          # 患者行 / 提示行不参与拖放
            series = _series_payload_from_item(it)
            if series and series.get("folder"):
                picked.append((it, series))
        return picked

    def startDrag(self, supported_actions):
        picked = self._dragged_series_items()
        if not picked:
            return

        items = [it for it, _s in picked]
        series_list = [s for _it, s in picked]

        mime = QtCore.QMimeData()
        if len(series_list) == 1:
            # 单选：沿用既有单个字典载荷，阅读区等既有通路零改动
            mime.setData(SERIES_MIME,
                         self.register_payload(series_list[0]).encode("utf-8"))
            mime.setText(str(series_list[0].get("folder")))
        else:
            # 多选：批量载荷。阅读区不认这个 MIME（会忽略），只有脊柱拼接
            # 面板处理它 —— 所以拖到阅读区等于什么都没拖，符合直觉。
            mime.setData(self.SERIES_BATCH_MIME,
                         self.register_payload(series_list).encode("utf-8"))
            mime.setText("%d 个序列" % len(series_list))

        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        # 拖动时用序列缩略图当指针，让用户知道拖的是哪张片子；多选时叠一张
        # 「计数」角标，避免用户以为只拖了一个。
        pm = items[0].data(0, QtCore.Qt.DecorationRole)
        if isinstance(pm, QtGui.QPixmap) and not pm.isNull():
            icon = pm.scaled(72, 72, QtCore.Qt.KeepAspectRatio,
                             QtCore.Qt.SmoothTransformation)
            if len(items) > 1:
                icon = QtGui.QPixmap(icon)
                _draw_count_badge(icon, len(items))
            drag.setPixmap(icon)
        drag.exec_(QtCore.Qt.CopyAction)


class SeriesThumbDelegate(QtWidgets.QStyledItemDelegate):
    """序列行的缩略图绘制委托

    默认委托只在左侧画一个小图标、右侧单行文本，这里改成
    「左侧缩略图 + 右侧两行说明」，让患者列表真正以缩略图方式浏览序列。
    患者行（以及提示行）仍走默认绘制，保持原有外观。
    """

    THUMB_SIZE = 96
    MARGIN = 5
    TEXT_GAP = 8
    LINE_GAP = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._placeholder = None     # 懒生成：切皮肤后要按新配色重建

    def invalidate_placeholder(self):
        """皮肤切换后调用：丢弃按旧配色画好的占位图。"""
        self._placeholder = None

    @staticmethod
    def _make_placeholder(size: int) -> QtGui.QPixmap:
        t = theme.tokens()
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtGui.QColor(t["field"]))
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setPen(QtGui.QPen(QtGui.QColor(t["border_strong"])))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, size - 1, size - 1), 6, 6)
        p.setPen(QtGui.QPen(QtGui.QColor(t["text_muted"])))
        font = p.font()
        font.setPointSize(max(7, size // 9))
        p.setFont(font)
        p.drawText(QtCore.QRect(0, 0, size, size), QtCore.Qt.AlignCenter, "预览")
        p.end()
        return pm

    def _series_data(self, index):
        """仅对序列行生效，其他行返回 None"""
        try:
            data = index.data(QtCore.Qt.UserRole)
        except Exception:
            return None
        if isinstance(data, dict) and data.get("type") == "series":
            return data.get("data") or {}
        return None

    def sizeHint(self, option, index):
        if self._series_data(index) is None:
            return super().sizeHint(option, index)
        height = self.THUMB_SIZE + self.MARGIN * 2
        return QtCore.QSize(self.THUMB_SIZE + 160, height)

    def paint(self, painter, option, index):
        if self._series_data(index) is None:
            super().paint(painter, option, index)
            return

        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QtWidgets.QApplication.style()

        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        # 选中/悬停背景交给样式绘制，保持与整体主题一致
        style.drawPrimitive(QtWidgets.QStyle.PE_PanelItemViewItem, opt, painter, widget)

        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        text_color = (option.palette.color(QtGui.QPalette.HighlightedText) if selected
                      else option.palette.color(QtGui.QPalette.Text))

        rect = option.rect.adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        thumb = QtCore.QRect(rect.left(),
                             rect.top() + max(0, (rect.height() - self.THUMB_SIZE) // 2),
                             self.THUMB_SIZE, self.THUMB_SIZE)

        pm = index.data(QtCore.Qt.DecorationRole)
        if isinstance(pm, QtGui.QIcon):
            pm = pm.pixmap(self.THUMB_SIZE, self.THUMB_SIZE)
        if not isinstance(pm, QtGui.QPixmap) or pm.isNull():
            if self._placeholder is None:
                self._placeholder = self._make_placeholder(self.THUMB_SIZE)
            pm = self._placeholder

        w = min(self.THUMB_SIZE, pm.width())
        h = min(self.THUMB_SIZE, pm.height())
        target = QtCore.QRect(thumb.center().x() - w // 2,
                              thumb.center().y() - h // 2, w, h)
        painter.drawPixmap(target, pm)
        painter.setPen(QtGui.QPen(theme.color("border_strong")))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRect(target.adjusted(0, 0, -1, -1))

        # 右侧两行文字
        x = thumb.right() + self.TEXT_GAP
        text_rect = QtCore.QRect(x, rect.top(),
                                 max(10, rect.right() - x), rect.height())
        lines = []
        title = index.data(QtCore.Qt.DisplayRole)
        if title:
            lines.append(str(title))
        detail = index.data(QtCore.Qt.UserRole + 1)
        if detail:
            lines.append(str(detail))
        if not lines:
            painter.restore()
            return

        heights = []
        total_h = 0
        for i, _ in enumerate(lines):
            f = QtGui.QFont(option.font)
            if i > 0:
                f.setPointSize(max(7, f.pointSize() - 1))
            fm = QtGui.QFontMetrics(f)
            heights.append(fm.height())
            total_h += fm.height()
        total_h += self.LINE_GAP * (len(lines) - 1)

        y = text_rect.top() + max(0, (text_rect.height() - total_h) // 2)
        for i, line in enumerate(lines):
            f = QtGui.QFont(option.font)
            if i > 0:
                f.setPointSize(max(7, f.pointSize() - 1))
                painter.setPen(text_color.darker(125) if not selected else text_color)
            else:
                painter.setPen(text_color)
            painter.setFont(f)
            fm = QtGui.QFontMetrics(f)
            elided = fm.elidedText(line, QtCore.Qt.ElideRight, text_rect.width())
            painter.drawText(QtCore.QRect(text_rect.left(), y,
                                          text_rect.width(), heights[i]),
                             QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, elided)
            y += heights[i] + self.LINE_GAP
        painter.restore()


class DetectionSettingsDialog(QtWidgets.QDialog):
    """X 线检测阈值设置（沿用 CT3D 深色主题，读写 xray 的 json 配置）"""

    FIELDS = [
        ("chest_conf",          "胸片检测置信度",        0.05, 0.95),
        ("fracture_conf",       "骨折检测置信度",        0.02, 0.95),
        ("boneage_detect_conf", "骨龄-关节定位置信度",   0.05, 0.95),
        ("boneage_cls_conf",    "骨龄-等级分类置信度",   0.05, 0.95),
    ]

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self.setWindowTitle("X 线检测设置")
        self.setMinimumWidth(380)

        layout = QtWidgets.QVBoxLayout(self)
        tip = QtWidgets.QLabel("阈值越低越敏感（检出更多，误报也可能增加）")
        theme.role(tip, "muted")
        layout.addWidget(tip)

        form = QtWidgets.QFormLayout()
        self._spins = {}
        values = bridge.load_detection_settings()
        for key, label, lo, hi in self.FIELDS:
            sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(0.01)
            sp.setDecimals(2)
            sp.setValue(float(values.get(key, 0.25)))
            form.addRow(label, sp)
            self._spins[key] = sp
        layout.addLayout(form)

        # 部位匹配是防止「胸片跑足弓」这类错配的护栏；但国产设备常把
        # BodyPartExamined 写空/写错，判定会失准，所以留一个显式出口。
        self._ignore_part_cb = QtWidgets.QCheckBox(
            "忽略部位匹配限制（不推荐）")
        self._ignore_part_cb.setChecked(bool(values.get("ignore_part_scope",
                                                        False)))
        self._ignore_part_cb.setToolTip(
            "勾选后，无论检查部位是什么都放开全部检测功能。\n"
            "仅在设备未写（或写错）检查部位、导致本该可用的功能被置灰时使用，\n"
            "此时请自行确认片子与所选检测项目是否匹配。")

        layout.addWidget(self._ignore_part_cb)

        # 儿科心影校正：儿童胸片的心影本就相对大，模型几乎必报心脏肥大。
        # 关掉它就恢复「成人判读」的原始行为（用于对照/回顾历史结果）。
        self._paed_cb = QtWidgets.QCheckBox(
            "儿童胸片心影校正（按儿科心胸比标准）")
        self._paed_cb.setChecked(bool(values.get("paed_cardio_correct", True)))
        self._paed_cb.setToolTip(
            "勾选后，对 15 岁以下患者的「心脏肥大」结论做二次判读：\n"
            "按检测框估计心胸比（CTR），与该年龄的生理上限比较，\n"
            "未超限时降级为「心影偏大（儿童生理性）」，不再作为病变报出。\n"
            "儿科 CTR 生理上限：<1 岁 0.60 / 1~4 岁 0.55 / ≥5 岁 0.50。")

        layout.addWidget(self._paed_cb)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        reset = QtWidgets.QPushButton("恢复默认")
        reset.clicked.connect(self._reset)
        ok = QtWidgets.QPushButton("保存")
        ok.setDefault(True)
        ok.clicked.connect(self._save)
        cancel = QtWidgets.QPushButton("取消")
        cancel.clicked.connect(self.reject)
        for b in (reset, ok, cancel):
            btns.addWidget(b)
        layout.addLayout(btns)

    def _reset(self):
        defaults = {"chest_conf": 0.25, "fracture_conf": 0.13,
                    "boneage_detect_conf": 0.30, "boneage_cls_conf": 0.30}
        for key, sp in self._spins.items():
            sp.setValue(defaults[key])
        self._ignore_part_cb.setChecked(False)
        self._paed_cb.setChecked(True)

    def _save(self):
        values = {k: float(sp.value()) for k, sp in self._spins.items()}
        values["ignore_part_scope"] = bool(self._ignore_part_cb.isChecked())
        values["paed_cardio_correct"] = bool(self._paed_cb.isChecked())
        if self._bridge.save_detection_settings(values):
            self.accept()
        else:
            QtWidgets.QMessageBox.warning(self, "保存失败", "无法写入检测设置文件。")


class DetectionRecordsDialog(QtWidgets.QDialog):
    """检测记录浏览 / 导出"""

    HEADERS = ["时间", "患者", "类型", "摘要"]
    PAGE_SIZE = 50

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._store = bridge.DetectionRecordStore()
        self._page = 1
        self._total = 0
        self._ids = []

        self.setWindowTitle("X 线检测记录")
        self.resize(860, 520)

        layout = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("按患者姓名 / 检查号 / 摘要搜索...")
        self._search.returnPressed.connect(self._reload)
        bar.addWidget(self._search, 1)
        self._kind_combo = QtWidgets.QComboBox()
        self._kind_combo.addItem("全部类型", None)
        for key, (cn, _v) in bridge.KIND_META.items():
            self._kind_combo.addItem(cn, key)
        self._kind_combo.currentIndexChanged.connect(self._reload)
        bar.addWidget(self._kind_combo)
        reload_btn = QtWidgets.QPushButton("查询")
        reload_btn.clicked.connect(self._reload)
        bar.addWidget(reload_btn)
        layout.addLayout(bar)

        self._table = QtWidgets.QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows)
        self._table.verticalHeader().setVisible(False)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self._table, 1)

        foot = QtWidgets.QHBoxLayout()
        self._stats = QtWidgets.QLabel("")
        theme.role(self._stats, "muted")
        foot.addWidget(self._stats)
        foot.addStretch(1)

        self._prev_btn = QtWidgets.QPushButton("上一页")
        self._prev_btn.clicked.connect(lambda: self._goto(self._page - 1))
        self._next_btn = QtWidgets.QPushButton("下一页")
        self._next_btn.clicked.connect(lambda: self._goto(self._page + 1))
        del_btn = QtWidgets.QPushButton("删除选中")
        del_btn.clicked.connect(self._delete_selected)
        clear_btn = QtWidgets.QPushButton("清空全部")
        clear_btn.setProperty("role", "danger")
        clear_btn.clicked.connect(self._clear_all)
        export_btn = QtWidgets.QPushButton("导出 CSV")
        export_btn.clicked.connect(self._export)
        compact_btn = QtWidgets.QPushButton("清理历史图像数据")
        compact_btn.setToolTip(
            "旧版记录把整张标注图也写进了数据库，可能占用数 GB 磁盘。\n"
            "此操作只丢弃图像、保留全部记录信息，并回收磁盘空间")
        compact_btn.clicked.connect(self._compact)
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        for b in (self._prev_btn, self._next_btn, del_btn, clear_btn,
                  export_btn, compact_btn, close_btn):
            foot.addWidget(b)
        layout.addLayout(foot)

        self._reload()

    # ---------- 数据 ----------

    def _reload(self):
        self._page = 1
        self._load()

    def _goto(self, page):
        if page < 1:
            return
        if self._total and (page - 1) * self.PAGE_SIZE >= self._total:
            return
        self._page = page
        self._load()

    def _load(self):
        search = self._search.text().strip() or None
        kind = self._kind_combo.currentData()
        rows, total = self._store.list(self._page, self.PAGE_SIZE, search, kind)
        self._total = total
        self._ids = []
        self._table.setRowCount(0)
        for r in rows:
            i = self._table.rowCount()
            self._table.insertRow(i)
            summary = (r.get("summary") or "").replace("\n", " ")
            cells = [r.get("detection_time", ""), r.get("patient_name", ""),
                     self._kind_cn(r.get("detection_type")), summary[:300]]
            item0 = QtWidgets.QTableWidgetItem(str(cells[0]))
            item0.setData(QtCore.Qt.UserRole, r.get("id"))
            for c, text in enumerate(cells):
                if c == 0:
                    self._table.setItem(i, c, item0)
                else:
                    self._table.setItem(i, c, QtWidgets.QTableWidgetItem(str(text)))
            self._ids.append(r.get("id"))
        n_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        st = self._store.stats()
        self._stats.setText(
            "共 %d 条 · 第 %d/%d 页 · 库文件 %.1f MB"
            % (total, self._page, n_pages, st["db_bytes"] / 1048576.0))
        self._prev_btn.setEnabled(self._page > 1)
        self._next_btn.setEnabled(self._page < n_pages)

    @staticmethod
    def _kind_cn(kind):
        info = None
        try:
            info = __import__("xray_bridge").KIND_META.get(kind)
        except Exception:
            pass
        return info[0] if info else (kind or "")

    # ---------- 操作 ----------

    def _selected_ids(self):
        ids = []
        for idx in self._table.selectionModel().selectedRows():
            item = self._table.item(idx.row(), 0)
            if item is not None:
                v = item.data(QtCore.Qt.UserRole)
                if v is not None:
                    ids.append(v)
        return ids

    def _delete_selected(self):
        ids = self._selected_ids()
        if not ids:
            QtWidgets.QMessageBox.information(self, "提示", "请先选中要删除的记录。")
            return
        if QtWidgets.QMessageBox.question(
                self, "确认删除", "确定删除选中的 %d 条记录？" % len(ids),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        ) != QtWidgets.QMessageBox.Yes:
            return
        self._store.delete(ids)
        self._load()

    def _clear_all(self):
        if QtWidgets.QMessageBox.warning(
                self, "确认清空", "将删除全部检测记录，且不可恢复。是否继续？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        ) != QtWidgets.QMessageBox.Yes:
            return
        self._store.clear()
        self._reload()

    def _export(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出检测记录", "检测记录.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        out = self._store.export_csv(
            path, self._search.text().strip() or None,
            self._kind_combo.currentData())
        if out:
            QtWidgets.QMessageBox.information(self, "导出完成", "已导出到：\n%s" % out)
        else:
            QtWidgets.QMessageBox.warning(self, "导出失败", "没有可导出的记录。")

    def _compact(self):
        st = self._store.stats()
        mb = st["db_bytes"] / 1048576.0
        if QtWidgets.QMessageBox.question(
                self, "清理历史图像数据",
                "数据库当前 %.1f MB。\n\n"
                "旧版记录把整张标注图也存进了数据库，这部分数据占绝大部分空间。\n"
                "清理后会先备份原库（.bak），仅丢弃图像、保留全部记录信息。\n\n"
                "是否继续？" % mb,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        ) != QtWidgets.QMessageBox.Yes:
            return
        progress = loading_ui.TechBusyDialog(
            "正在清理并回收磁盘空间，请稍候...", None, self)
        progress.setWindowTitle("请稍候")
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QtWidgets.QApplication.processEvents()
        try:
            ok, info = self._store.compact()
        finally:
            progress.close()
        if ok:
            QtWidgets.QMessageBox.information(
                self, "完成", "清理完成，原库已备份为：\n%s" % info)
            self._load()
        else:
            QtWidgets.QMessageBox.warning(self, "失败", "清理失败：%s" % info)


class FeedbackDialog(QtWidgets.QDialog):
    """漏检反馈登记：选类型 + 填备注，确认后图落盘 + 记数据库

    刻意做得轻量：反馈者的成本越低，收集到的素材越多。因此只要求
    「选一个类型」，备注可留空；当前片子信息与刚跑过的检测结果自动带上。
    """

    def __init__(self, bridge, store, context, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._store = store
        self._ctx = context or {}
        self.record_id = None

        self.setWindowTitle("漏检反馈")
        self.setMinimumWidth(460)
        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(self._summary_text())
        info.setWordWrap(True)
        theme.role(info, "card")
        layout.addWidget(info)

        form = QtWidgets.QFormLayout()
        self._kind = QtWidgets.QComboBox()
        for key, cn in bridge.FEEDBACK_KINDS.items():
            self._kind.addItem(cn, key)
        # 默认选中"骨折漏检"（本功能最初的需求就是骨折）
        idx = self._kind.findData("missed_fracture")
        if idx >= 0:
            self._kind.setCurrentIndex(idx)
        form.addRow("反馈类型", self._kind)

        self._part = QtWidgets.QLineEdit(self._ctx.get("body_part") or "")
        self._part.setPlaceholderText("如：腕关节 / 腰椎 / 胸片")
        form.addRow("检查部位", self._part)

        self._note = QtWidgets.QLineEdit()
        self._note.setPlaceholderText(
            "漏检描述（可选），如：桡骨远端骨折未标出")
        form.addRow("备注", self._note)
        layout.addLayout(form)

        self._note_detail = QtWidgets.QPlainTextEdit()
        self._note_detail.setPlaceholderText(
            "详细描述（可选）：骨折部位、影像表现、为什么判定为漏检……")
        self._note_detail.setMaximumHeight(90)
        layout.addWidget(self._note_detail)

        self._hint = QtWidgets.QLabel(
            "图片将以 PNG 存到软件目录 feedback_images/日期/，"
            "数据库只记录路径（不存图像，库体积不会膨胀）。")
        self._hint.setWordWrap(True)
        theme.role(self._hint, "caption")
        layout.addWidget(self._hint)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        cancel = QtWidgets.QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QtWidgets.QPushButton("提交反馈")
        ok.setDefault(True)
        ok.clicked.connect(self._submit)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        layout.addLayout(btns)

    def _summary_text(self):
        c = self._ctx
        lines = ["<b>当前片子</b>"]
        if c.get("series_title"):
            lines.append("· 序列：%s" % c["series_title"])
        if c.get("patient_name"):
            lines.append("· 患者：%s%s" % (
                c["patient_name"],
                ("（%s）" % c["patient_id"]) if c.get("patient_id") else ""))
        if c.get("access_no"):
            lines.append("· 检查号：%s" % c["access_no"])
        if c.get("study_date"):
            lines.append("· 检查日期：%s" % c["study_date"])
        if c.get("source_path"):
            lines.append("· 文件：%s" % os.path.basename(c["source_path"]))
        if c.get("detector"):
            lines.append("· 刚执行的检测：%s" % c["detector"])
        if c.get("detect_result"):
            lines.append("· 其结果：%s" % c["detect_result"])
        return "<br>".join(lines)

    def _submit(self):
        src = self._ctx.get("source_path")
        if not src or not os.path.isfile(src):
            QtWidgets.QMessageBox.warning(
                self, "无法登记", "未能定位当前影像文件，请重新载入序列后再试。")
            return
        note = self._note.text().strip()
        detail = self._note_detail.toPlainText().strip()
        if detail:
            note = (note + "\n" + detail) if note else detail
        try:
            self.record_id = self._store.add(
                feedback_type=self._kind.currentData(),
                source_path=src,
                patient_name=str(self._ctx.get("patient_name") or ""),
                patient_id=str(self._ctx.get("patient_id") or ""),
                access_no=str(self._ctx.get("access_no") or ""),
                study_date=str(self._ctx.get("study_date") or ""),
                body_part=self._part.text().strip(),
                note=note,
                detector=str(self._ctx.get("detector") or ""),
                detect_result=str(self._ctx.get("detect_result") or ""),
                model_version=str(self._ctx.get("model_version") or ""),
                tag=self._kind.currentData())
        except Exception as exc:      # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "提交失败", "写入反馈失败：\n%s" % exc)
            return
        if not self.record_id:
            QtWidgets.QMessageBox.critical(
                self, "提交失败", "写入反馈失败，请查看程序控制台输出。")
            return
        self.accept()


class FeedbackListDialog(QtWidgets.QDialog):
    """漏检反馈浏览 / 导出 / 打开图片所在目录"""

    HEADERS = ["时间", "患者", "部位", "类型", "备注", "图片"]
    PAGE_SIZE = 50

    def __init__(self, bridge, store, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._store = store
        self._page = 1
        self._total = 0

        self.setWindowTitle("漏检反馈列表")
        self.resize(900, 520)

        layout = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("按患者姓名 / 检查号 / 部位 / 备注搜索...")
        self._search.returnPressed.connect(self._reload)
        bar.addWidget(self._search, 1)
        self._kind_combo = QtWidgets.QComboBox()
        self._kind_combo.addItem("全部类型", None)
        for key, cn in bridge.FEEDBACK_KINDS.items():
            self._kind_combo.addItem(cn, key)
        self._kind_combo.currentIndexChanged.connect(self._reload)
        bar.addWidget(self._kind_combo)
        reload_btn = QtWidgets.QPushButton("查询")
        reload_btn.clicked.connect(self._reload)
        bar.addWidget(reload_btn)
        layout.addLayout(bar)

        self._table = QtWidgets.QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._table.verticalHeader().setVisible(False)
        hdr = self._table.horizontalHeader()
        for c in range(5):
            hdr.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self._table, 1)

        foot = QtWidgets.QHBoxLayout()
        self._stats = QtWidgets.QLabel("")
        theme.role(self._stats, "muted")
        foot.addWidget(self._stats)
        foot.addStretch(1)
        self._prev_btn = QtWidgets.QPushButton("上一页")
        self._prev_btn.clicked.connect(lambda: self._goto(self._page - 1))
        self._next_btn = QtWidgets.QPushButton("下一页")
        self._next_btn.clicked.connect(lambda: self._goto(self._page + 1))
        open_btn = QtWidgets.QPushButton("打开图片")
        open_btn.setToolTip("用系统默认看图程序打开选中记录的反馈图片")
        open_btn.clicked.connect(self._open_image)
        dir_btn = QtWidgets.QPushButton("打开素材目录")
        dir_btn.clicked.connect(self._open_root)
        del_btn = QtWidgets.QPushButton("删除选中")
        del_btn.clicked.connect(self._delete_selected)
        export_btn = QtWidgets.QPushButton("导出 CSV")
        export_btn.clicked.connect(self._export)
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        for b in (self._prev_btn, self._next_btn, open_btn, dir_btn,
                  del_btn, export_btn, close_btn):
            foot.addWidget(b)
        layout.addLayout(foot)

        self._reload()

    def _reload(self):
        self._page = 1
        self._load()

    def _goto(self, page):
        if page < 1 or (self._total and
                        (page - 1) * self.PAGE_SIZE >= self._total):
            return
        self._page = page
        self._load()

    def _load(self):
        search = self._search.text().strip() or None
        kind = self._kind_combo.currentData()
        rows, total = self._store.list(self._page, self.PAGE_SIZE, search, kind)
        self._total = total
        self._table.setRowCount(0)
        for r in rows:
            i = self._table.rowCount()
            self._table.insertRow(i)
            note = (r.get("note") or "").replace("\n", " ")
            cells = [r.get("feedback_time", ""), r.get("patient_name", ""),
                     r.get("body_part", ""), r.get("type_cn", ""), note[:200],
                     os.path.basename(r.get("image_file") or "")
                     + ("" if r.get("image_exists") else "（文件缺失）")]
            item0 = QtWidgets.QTableWidgetItem(str(cells[0]))
            item0.setData(QtCore.Qt.UserRole, r.get("id"))
            self._table.setItem(i, 0, item0)
            for c in range(1, len(cells)):
                self._table.setItem(i, c,
                                    QtWidgets.QTableWidgetItem(str(cells[c])))
        n_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        st = self._store.stats()
        self._stats.setText(
            "共 %d 条 · 第 %d/%d 页 · 图片 %d 张 / %.1f MB"
            % (total, self._page, n_pages, st.get("image_count", 0),
               st.get("image_bytes", 0) / 1048576.0))
        self._prev_btn.setEnabled(self._page > 1)
        self._next_btn.setEnabled(self._page < n_pages)

    def _selected(self):
        idxs = self._table.selectionModel().selectedRows()
        if not idxs:
            return None
        item = self._table.item(idxs[0].row(), 0)
        return item.data(QtCore.Qt.UserRole) if item is not None else None

    def _open_image(self):
        rid = self._selected()
        if rid is None:
            QtWidgets.QMessageBox.information(self, "提示", "请先选中一条记录。")
            return
        rows, _ = self._store.list(1, 500)
        for r in rows:
            if r.get("id") == rid:
                p = r.get("image_abs")
                if not p or not os.path.isfile(p):
                    QtWidgets.QMessageBox.warning(
                        self, "图片缺失", "该记录的反馈图片已不在磁盘上。")
                    return
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(p))
                return

    def _open_root(self):
        root = self._store.image_root
        os.makedirs(root, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(root))

    def _delete_selected(self):
        idxs = self._table.selectionModel().selectedRows()
        ids = []
        for idx in idxs:
            item = self._table.item(idx.row(), 0)
            if item is not None and item.data(QtCore.Qt.UserRole) is not None:
                ids.append(item.data(QtCore.Qt.UserRole))
        if not ids:
            QtWidgets.QMessageBox.information(self, "提示", "请先选中要删除的记录。")
            return
        if QtWidgets.QMessageBox.question(
                self, "确认删除",
                "确定删除选中的 %d 条反馈？\n磁盘上对应的图片也会一并删除。" % len(ids),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        ) != QtWidgets.QMessageBox.Yes:
            return
        self._store.delete(ids)
        self._load()

    def _export(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出反馈记录", "漏检反馈.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        out = self._store.export_csv(
            path, self._search.text().strip() or None,
            self._kind_combo.currentData())
        if out:
            QtWidgets.QMessageBox.information(self, "导出完成", "已导出到：\n%s" % out)
        else:
            QtWidgets.QMessageBox.warning(self, "导出失败", "没有可导出的记录。")


class SettingsDialog(QtWidgets.QDialog):
    """设置对话框：患者扫描目录（多选）+ 在线升级服务器 + 使用说明入口

    目录列表与升级服务器地址都保存到软件目录 app_settings.json，
    启动时按这些目录扫描患者、按该地址检查更新。
    """

    def __init__(self, parent=None, dirs=None, help_callback=None,
                 base_url=None, reportqc_callback=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)
        self._help_callback = help_callback
        self._reportqc_callback = reportqc_callback
        self._test_worker = None

        try:
            import updater as _updater
            self._default_base = _updater.DEFAULT_BASE_URL
        except Exception:              # noqa: BLE001
            self._default_base = ""

        layout = QtWidgets.QVBoxLayout(self)

        tip = QtWidgets.QLabel(
            "患者扫描目录：启动时与「刷新」会扫描这些目录里的患者数据。\n"
            "可添加多个目录；不添加则使用默认目录。")
        tip.setWordWrap(True)
        theme.role(tip, "muted")
        layout.addWidget(tip)

        self._dir_list = QtWidgets.QListWidget()
        # v1.23.1：下面又多了「报告质控」一组，列表从 140 收到 110。
        # 整个对话框别超过 768 屏的可用高度（约 700），否则底部按钮会被挤出去。
        self._dir_list.setMinimumHeight(110)
        layout.addWidget(self._dir_list)
        for d in (dirs or []):
            self._dir_list.addItem(d)

        row = QtWidgets.QHBoxLayout()
        add_btn = QtWidgets.QPushButton("添加目录…")
        add_btn.clicked.connect(self._on_add_dir)
        self._remove_btn = QtWidgets.QPushButton("移除选中")
        self._remove_btn.setEnabled(False)
        self._remove_btn.clicked.connect(self._on_remove_dir)
        row.addWidget(add_btn)
        row.addWidget(self._remove_btn)
        row.addStretch()
        layout.addLayout(row)

        self._dir_list.itemSelectionChanged.connect(
            lambda: self._remove_btn.setEnabled(
                self._dir_list.currentItem() is not None))
        self._dir_list.itemDoubleClicked.connect(
            lambda _it: self._on_add_dir())

        # ---- 在线升级服务器 ----
        # 换主机是运维常态（换机房、换公网 IP、本地搭个镜像）。
        # 地址写在软件目录的 app_settings.json 里，升级补丁不会覆盖它，
        # 所以改一次就一直有效，不用重装、也不用改代码发版。
        layout.addSpacing(12)
        srv_title = QtWidgets.QLabel("在线升级服务器")
        theme.role(srv_title, "section")
        layout.addWidget(srv_title)

        srv_tip = QtWidgets.QLabel(
            "「更新」按钮从这个地址检查并下载更新包。主机换了就改这里，"
            "点「测试连接」确认能通再保存。")
        srv_tip.setWordWrap(True)
        theme.role(srv_tip, "muted")
        layout.addWidget(srv_tip)

        srv_row = QtWidgets.QHBoxLayout()
        self._base_edit = QtWidgets.QLineEdit()
        self._base_edit.setPlaceholderText(self._default_base or "http://主机地址")
        self._base_edit.setText(base_url or "")
        self._base_edit.setToolTip(
            "例：http://10.0.0.8:8000\n"
            "不带 http:// 也可以，保存时会自动补上。")
        srv_row.addWidget(self._base_edit, 1)
        self._default_btn = QtWidgets.QPushButton("恢复默认")
        self._default_btn.setToolTip(
            "清空并回到内置地址 %s" % self._default_base)
        self._default_btn.clicked.connect(self._on_restore_default)
        srv_row.addWidget(self._default_btn)
        layout.addLayout(srv_row)

        test_row = QtWidgets.QHBoxLayout()
        self._test_btn = QtWidgets.QPushButton("测试连接")
        self._test_btn.clicked.connect(self._on_test_connection)
        test_row.addWidget(self._test_btn)
        test_row.addStretch(1)
        layout.addLayout(test_row)

        # 结果单独占一行且不设固定高度：加了固定高度再 wordWrap，文字会画到
        # 容器外压住下面的按钮（这个坑在别处踩过）。
        self._test_label = QtWidgets.QLabel("")
        self._test_label.setWordWrap(True)
        theme.role(self._test_label, "caption")
        layout.addWidget(self._test_label)

        # ---- 报告质控 ----
        # v1.23.1：设置类入口全收到这里。原来「质控设置」是左侧栏一个独立按钮，
        # 而它管的只是几个开关，跟「设置」放一起更顺；左栏也因此少一个按钮。
        layout.addSpacing(12)
        qc_title = QtWidgets.QLabel("报告质控")
        theme.role(qc_title, "section")
        layout.addWidget(qc_title)

        self._qc_show_mini_cb = None
        qc_ready = True
        try:
            from reportqc.ui.settings_dialog import get_show_mini as _get_show_mini
        except Exception:              # noqa: BLE001
            _get_show_mini = None
            qc_ready = False

        if qc_ready:
            self._qc_show_mini_cb = QtWidgets.QCheckBox(
                "启动时自动打开质控悬浮小窗")
            try:
                self._qc_show_mini_cb.setChecked(bool(_get_show_mini()))
            except Exception:          # noqa: BLE001
                self._qc_show_mini_cb.setChecked(False)
            layout.addWidget(self._qc_show_mini_cb)
            qc_tip_text = ("默认不自动打开。需要时点左下角「报告质控」按钮即可调出小窗。\n"
                           "勾上则每次启动自动弹出（就绪态，不会自动识别）。")
        else:
            qc_tip_text = ("报告质控模块不可用（OCR 模型可能缺失），相关设置已隐藏。\n"
                           "左侧「报告质控」按钮同样会提示模型缺失。")
        qc_tip = QtWidgets.QLabel(qc_tip_text)
        qc_tip.setWordWrap(True)
        theme.role(qc_tip, "muted")
        layout.addWidget(qc_tip)

        qc_row = QtWidgets.QHBoxLayout()
        self._qc_set_btn = QtWidgets.QPushButton("质控设置…")
        self._qc_set_btn.setToolTip(
            "OCR 放大倍数、提示级问题是否显示、忽略词管理、\n"
            "自定义错别字 / 医学术语白名单 / 误判学习词")
        self._qc_set_btn.setEnabled(bool(qc_ready) and reportqc_callback is not None)
        self._qc_set_btn.clicked.connect(self._on_qc_settings)
        qc_row.addWidget(self._qc_set_btn)
        qc_row.addStretch(1)
        layout.addLayout(qc_row)

        layout.addSpacing(8)
        btn_row = QtWidgets.QHBoxLayout()
        help_btn = QtWidgets.QPushButton("使用说明")
        help_btn.setToolTip("查看软件使用说明")
        help_btn.clicked.connect(self._on_help)
        save_btn = QtWidgets.QPushButton("保存")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)
        cancel_btn = QtWidgets.QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(help_btn)
        btn_row.addStretch()
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _on_add_dir(self):
        start = self._dir_list.currentItem().text() \
            if self._dir_list.currentItem() else ""
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择患者扫描目录", start or os.path.expanduser("~"))
        if not folder:
            return
        folder = os.path.normpath(folder)
        # 去重
        for i in range(self._dir_list.count()):
            if os.path.normpath(self._dir_list.item(i).text()) == folder:
                self._dir_list.setCurrentRow(i)
                return
        self._dir_list.addItem(folder)
        self._dir_list.setCurrentRow(self._dir_list.count() - 1)

    def _on_remove_dir(self):
        row = self._dir_list.currentRow()
        if row >= 0:
            self._dir_list.takeItem(row)

    def _on_help(self):
        if self._help_callback:
            self._help_callback()

    # ---- 在线升级服务器 ----
    def _on_restore_default(self):
        """恢复默认：清空输入框 = 跟随内置地址（占位符里会显示它）。"""
        self._base_edit.setText("")
        self._test_label.setText(
            "已恢复默认，保存后使用 %s。可以点「测试连接」先验证。"
            % (self._default_base or "内置地址"))

    def _on_test_connection(self):
        """在后台线程里拉一次索引，通不通当场看结果。

        注意不能用 check_update 那套「有没有新版本」来判断连通性：返回
        「已是最新」同样是通的，而这里要的正是"能不能连上"。
        """
        try:
            import updater
            import update_ui
        except Exception as exc:       # noqa: BLE001
            self._test_label.setText("升级模块加载失败：%s" % exc)
            return
        url = updater.normalize_base_url(self._base_edit.text()) \
            or (self._default_base or "")
        if not url:
            self._test_label.setText(
                "地址填得不对。请写成 http://主机 或 http://主机:端口。")
            return
        # 顺手把规范化结果填回去：用户粘贴「192.168.1.100:8080/」这类
        # 半成品很常见，保存前先让他看见最终会用哪个地址。
        self._base_edit.setText(
            "" if url == self._default_base else url)

        self._test_btn.setEnabled(False)
        self._test_label.setText("正在连接 %s …" % url)
        self._stop_test_worker()
        try:
            w = update_ui.CheckWorker(url, APP_VERSION, None)   # 不挂 parent：自己管生命周期
            self._test_worker = w
            w.found.connect(self._on_test_found)
            w.uptodate.connect(self._on_test_uptodate)
            w.failed.connect(self._on_test_failed)
            w.finished.connect(w.deleteLater)
            w.start()
        except Exception as exc:       # noqa: BLE001
            self._test_worker = None
            self._test_btn.setEnabled(True)
            self._test_label.setText("测试没能启动：%s" % exc)

    def _on_test_found(self, plan):
        self._test_btn.setEnabled(True)
        self._test_label.setText(
            "连接正常。线上最新 %s，本机 %s，有 %d 个更新包可下载。"
            % (plan.get('latest'), APP_VERSION,
               len(plan.get('steps') or [])))

    def _on_test_uptodate(self, cur):
        self._test_btn.setEnabled(True)
        self._test_label.setText(
            "连接正常，线上没有比 %s 更新的版本。" % cur)

    def _on_test_failed(self, msg):
        self._test_btn.setEnabled(True)
        self._test_label.setText(
            "连不上：%s\n检查主机地址、端口和网络；服务端没起也会这样。" % msg)

    def _stop_test_worker(self):
        """等在跑的测试线程结束，避免对话框销毁时线程还在跑（会硬崩）。"""
        w = self._test_worker
        self._test_worker = None
        if w is None:
            return
        try:
            if w.isRunning():
                w.wait(3000)
        except Exception:              # noqa: BLE001
            pass

    def done(self, result):
        self._stop_test_worker()
        super().done(result)

    def get_base_url(self):
        """返回生效的服务器地址；空串表示跟随内置默认。"""
        try:
            import updater
            return updater.normalize_base_url(self._base_edit.text())
        except Exception:              # noqa: BLE001
            return self._base_edit.text().strip()

    # ---- 报告质控 ----
    def _on_qc_settings(self):
        """打开质控详细设置窗（由主窗口回调，本类不直接依赖 reportqc_bridge）。"""
        if self._reportqc_callback is None:
            return
        try:
            # 必须把自己（模态的设置窗）当父窗口传下去，否则弹出来的质控设置
            # 会被压在本窗后面，用户以为按钮没反应。
            self._reportqc_callback(self)
        except Exception as exc:       # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "报告质控", "打不开质控设置：%s" % exc)

    def get_show_mini(self):
        """启动时是否自动打开质控小窗；None = 模块不可用，当前无法设置。"""
        cb = getattr(self, "_qc_show_mini_cb", None)
        return None if cb is None else bool(cb.isChecked())

    def get_dirs(self):
        """返回当前目录列表（保持输入顺序，去重）"""
        dirs = []
        for i in range(self._dir_list.count()):
            d = self._dir_list.item(i).text().strip()
            if d and d not in dirs:
                dirs.append(d)
        return dirs


def _strip_tone(text):
    """去掉拼音声调（dān → dan，lǚ → lu）。只用于姓名检索，不用于显示。"""
    try:
        import unicodedata
        return "".join(c for c in unicodedata.normalize("NFD", text)
                       if not unicodedata.combining(c))
    except Exception:              # noqa: BLE001
        return text


class ExportOptionsDialog(QtWidgets.QDialog):
    """导出选项（v1.15.1）：选是否脱敏。

    为什么每次导出都弹：脱敏与否直接决定导出的片子能不能在别的医院直接
    对回患者，属「一次选错就收不回来」的操作，不适合静默沿用上次设置。
    但勾选状态用 QSettings 记住，日常连导十几个序列时不用反复勾。
    """

    ORG, APP = "ZhiGu", "CT3D"
    KEY_DESENS = "export/desensitize"
    KEY_MASK = "export/mask_corners"

    def __init__(self, kind: str, series_count: int = 0, parent=None):
        super().__init__(parent)
        self._kind = (kind or "dicom").lower()
        self.is_image = self._kind in ("png", "jpg")

        title = {"dicom": "导出 DICOM 文件", "png": "导出为 PNG 图片",
                 "jpg": "导出为 JPG 图片"}.get(self._kind, "导出")
        self.setWindowTitle(title)
        self.setMinimumWidth(460)

        layout = QtWidgets.QVBoxLayout(self)

        head = QtWidgets.QLabel(
            "%s：共 %d 个序列" % (title, series_count) if series_count
            else title)
        theme.role(head, "title")
        layout.addWidget(head)

        # ---- 脱敏主开关 ----
        self._desens_cb = QtWidgets.QCheckBox(
            "脱敏导出（移除患者姓名 / ID / 生日等身份信息）")

        layout.addWidget(self._desens_cb)

        detail = QtWidgets.QLabel()
        detail.setWordWrap(True)
        theme.role(detail, "caption")
        # 说明文字缩进对齐到勾选框文字（原来是 QSS 的 padding-left）
        detail.setContentsMargins(22, 2, 0, 0)
        if self.is_image:
            detail.setText(
                "勾选后：导出目录不再以患者姓名命名，改为「匿名001」这样的代号。\n"
                "注意：图片本身若已被设备把姓名烧进像素（CR/DR 常见），\n"
                "仅改目录名无法去掉，需要再勾选下面的遮盖选项。")
        else:
            detail.setText(
                "勾选后：姓名→ANON-001、患者 ID→假名，生日/地址/电话/送检医师/\n"
                "医院名/设备序列号等一律清空，设备私有标签删除，\n"
                "各类 UID 重新生成（同一序列内保持一致，导入 PACS 仍是一个序列）。\n"
                "性别、年龄、检查日期保留（有临床价值，单独不构成身份信息）。")
        layout.addWidget(detail)

        # ---- 图片专用：遮盖烧录文字 ----
        self._mask_cb = None
        if self.is_image:
            self._mask_cb = QtWidgets.QCheckBox(
                "同时把图像四角涂黑（遮盖设备烧录的姓名/ID 文字）")

            self._mask_cb.setToolTip(
                "只覆盖图像四角的矩形区域，不触碰中央解剖结构。\n"
                "X 线/CR/DR 的烧录文字几乎都在四角；CT/MR 一般没有烧录文字，\n"
                "不需要勾选（勾了会挡住四角的一小块影像）。")
            layout.addWidget(self._mask_cb)

            # 窗值策略说明：用户最容易在这里产生误解（「我明明选了脑窗」）。
            win_note = QtWidgets.QLabel(
                "窗宽窗位：该序列正在视图中显示时，按你当前看到的窗值导出；\n"
                "没打开时按 DICOM 自带的诊断窗值导出（不是按像素极值拉伸）。")
            win_note.setWordWrap(True)
            theme.role(win_note, "caption")
            layout.addWidget(win_note)

        warn = QtWidgets.QLabel(
            "注意：脱敏改的是导出副本，源目录里的原始文件不会被修改。")
        warn.setWordWrap(True)
        theme.role(warn, "warn")
        layout.addWidget(warn)

        layout.addSpacing(4)
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        ok = QtWidgets.QPushButton("开始导出")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QtWidgets.QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        layout.addLayout(btns)

        self._load_state()
        # 遮盖只在脱敏时才讲得通：未勾脱敏时置灰，避免「涂黑了却还带名字」。
        self._desens_cb.toggled.connect(self._on_desens_toggled)
        self._on_desens_toggled(self._desens_cb.isChecked())

    # ---- 状态记忆 ----
    def _settings(self):
        return QtCore.QSettings(self.ORG, self.APP)

    def _load_state(self):
        try:
            st = self._settings()
            self._desens_cb.setChecked(
                st.value(self.KEY_DESENS, False, type=bool))
            if self._mask_cb is not None:
                self._mask_cb.setChecked(
                    st.value(self.KEY_MASK, False, type=bool))
        except Exception:          # noqa: BLE001
            pass

    def _on_desens_toggled(self, checked):
        if self._mask_cb is None:
            return
        self._mask_cb.setEnabled(bool(checked))
        if not checked:
            # 不取消勾选，只置灰：用户下次再勾脱敏时原来的意图还在
            pass

    def _save_state(self):
        try:
            st = self._settings()
            st.setValue(self.KEY_DESENS, bool(self._desens_cb.isChecked()))
            if self._mask_cb is not None:
                st.setValue(self.KEY_MASK, bool(self._mask_cb.isChecked()))
            st.sync()
        except Exception:          # noqa: BLE001
            pass

    @property
    def desensitize(self):
        return bool(self._desens_cb.isChecked())

    @property
    def mask_corners(self):
        return bool(self._mask_cb is not None and self._mask_cb.isChecked()
                    and self._desens_cb.isChecked())

    def accept(self):
        self._save_state()
        super().accept()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("知骨影像阅读器")
        self.resize(1400, 900)
        self._root_dir = DEFAULT_DATA_DIR
        # 患者扫描目录（设置里可配多个，持久化到 app_settings.json；
        # 为空时回退到 DEFAULT_SCAN_DIRS）
        self._scan_dirs = self._load_scan_dirs_config()
        self._current_reader = None
        self._current_series_folder = None
        self._current_series_data = None
        self._last_segmentation_mask = None
        # 多窗：每个窗口各自持有的暂存目录（键=窗口索引）。
        # 早先只有一个 _last_temp_folder 槽位，载第二窗会覆盖并删掉
        # 第一窗的暂存目录，导致那一窗图像失效。
        self._view_temp_folders = {}

        self._cached_patients = {}
        self._file_watcher = QtCore.QFileSystemWatcher(self)
        self._auto_refresh_enabled = False  # 禁用自动刷新功能
        # 不再创建和启动自动刷新定时器
        self._last_scan_time = 0
        self._refresh_worker = None
        
        icon_pixmap = self._create_app_icon_pixmap(64)
        icon = QtGui.QIcon(icon_pixmap)
        app = QtWidgets.QApplication.instance()
        if app:
            app.setWindowIcon(icon)
        self.setWindowIcon(icon)
        status = QtWidgets.QStatusBar(self)
        icon_label = QtWidgets.QLabel()
        icon_label.setPixmap(icon_pixmap.scaled(16, 16, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        status.addPermanentWidget(icon_label)
        self._license_label = QtWidgets.QLabel()
        self._license_label.setToolTip("双击查看/更换授权")
        self._license_label.setCursor(QtCore.Qt.PointingHandCursor)
        self._license_label.mousePressEvent = self._on_license_label_clicked
        status.addPermanentWidget(self._license_label)
        self._refresh_license_label()
        self.setStatusBar(status)

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        # **主窗口接受文件拖放**：从桌面/资源管理器把 .dcm 文件或
        # DICOM 目录拖进来即可打开（原先只有阅读区接受拖放，且只认
        # 序列卡片的内部 MIME，拖外部文件无效）。
        self.setAcceptDrops(True)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal, central)
        main_layout = QtWidgets.QHBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.addWidget(splitter)

        self._patient_tree = SeriesDragTreeWidget()
        self._patient_tree.setHeaderHidden(True)
        self._patient_tree.itemDoubleClicked.connect(self._on_series_double_clicked)
        # 单击即可展开/收起患者（展开后立刻呈现序列缩略图，不触发加载）。
        # 关掉「双击自动展开」，避免单击已切换过一次、双击再切换一次造成闪动。
        self._patient_tree.setExpandsOnDoubleClick(False)
        self._patient_tree.itemClicked.connect(self._on_tree_item_clicked)
        self._patient_tree.itemExpanded.connect(self._on_patient_expanded)
        # 右键导出：患者行 → 全部序列；序列行 → 单个序列
        self._patient_tree.export_requested.connect(self._on_patient_tree_menu)
        # 序列行改为「缩略图 + 两行说明」，行高不一致，需关闭统一行高
        self._patient_tree.setUniformRowHeights(False)
        # 子节点缩进收窄，给缩略图留出宽度
        self._patient_tree.setIndentation(6)
        # 去掉 QTreeView 默认 20px 的树形分支占位列，否则缩略图会整体右移
        self._patient_tree.setRootIsDecorated(False)
        self._series_delegate = SeriesThumbDelegate(self._patient_tree)
        self._patient_tree.setItemDelegate(self._series_delegate)
        # 序列缩略图的后台工作线程与缓存
        self._thumb_cache = {}     # series_key -> QPixmap / None
        self._thumb_items = {}     # series_key -> [QTreeWidgetItem, ...]
        self._thumb_pending = set()  # 正在生成缩略图的 series_key
        self._last_patient_click = (None, QtCore.QTime())  # 单击防抖（避开双击第二下）
        self._thumb_worker = SeriesThumbnailWorker(self)
        self._thumb_worker.done.connect(self._on_series_thumb_done)
        self._thumb_worker.start()
        left_panel = QtWidgets.QWidget()
        # 侧栏底色（比窗口底浅一档）由皮肤统一定义，见 theme.qss 的 #sidePanel
        left_panel.setObjectName("sidePanel")
        left_panel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        
        # 患者列表标题和排序控件
        left_header_layout = QtWidgets.QHBoxLayout()
        left_header_layout.addWidget(QtWidgets.QLabel("患者列表"))
        left_header_layout.addStretch()
        
        # 排序下拉框
        self._sort_combo = QtWidgets.QComboBox()
        self._sort_combo.addItem("倒序", "desc")
        self._sort_combo.addItem("正序", "asc")
        self._sort_combo.setToolTip("按文件修改时间排序")
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        left_header_layout.addWidget(self._sort_combo)
        
        # 删除所有患者按钮
        self._delete_all_btn = QtWidgets.QPushButton("删除全部")
        self._delete_all_btn.setToolTip("删除所有患者文件夹")
        theme.role(self._delete_all_btn, "danger")
        self._delete_all_btn.clicked.connect(self._on_delete_all_patients)
        left_header_layout.addWidget(self._delete_all_btn)

        # 清空列表按钮：只清空显示，不删除任何文件
        self._clear_list_btn = QtWidgets.QPushButton("清空")
        self._clear_list_btn.setToolTip(
            "仅清空患者列表显示，不删除任何文件；点「刷新」可重新载入")
        self._clear_list_btn.clicked.connect(self._on_clear_list_clicked)
        left_header_layout.addWidget(self._clear_list_btn)

        # 刷新按钮从右侧工具区移到患者列表区
        self._refresh_btn = QtWidgets.QPushButton("刷新")
        self._refresh_btn.setToolTip("刷新患者列表（扫描目录，检测新文件夹）")
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        left_header_layout.addWidget(self._refresh_btn)

        left_layout.addLayout(left_header_layout)

        # ---- 患者搜索（v1.15.1）：汉字 / 全拼 / 拼音首字母都能匹配 ----
        # 放在列表正上方而不是标题行里：标题行已经挤了「排序/删除全部/清空/
        # 刷新」四个控件，再塞输入框在窄窗口下会被压得只剩几十像素。
        search_row = QtWidgets.QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(4)
        self._search_edit = QtWidgets.QLineEdit()
        self._search_edit.setPlaceholderText("搜索姓名 / 拼音 / 首字母")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setToolTip(
            "输入汉字（张三）、全拼（zhangsan）或拼音首字母（zs）均可匹配；\n"
            "也会匹配患者 ID。Ctrl+F 定位到搜索框，Esc 清空。")
        self._search_edit.textChanged.connect(self._on_patient_search_changed)
        search_row.addWidget(self._search_edit, 1)

        self._search_count = QtWidgets.QLabel("")
        theme.role(self._search_count, "caption")
        self._search_count.setToolTip("当前匹配到的患者数 / 总数")
        search_row.addWidget(self._search_count)

        left_layout.addLayout(search_row)

        # Esc 清空搜索（只在输入框内生效，不影响阅读区的 Esc 行为）
        try:
            esc_sc = QtWidgets.QShortcut(
                QtGui.QKeySequence(QtCore.Qt.Key_Escape), self._search_edit)
            esc_sc.setContext(QtCore.Qt.WidgetShortcut)
            esc_sc.activated.connect(self._search_edit.clear)
        except Exception:          # noqa: BLE001
            pass

        left_layout.addWidget(self._patient_tree)

        drag_tip = QtWidgets.QLabel("提示：按住序列缩略图卡片拖到右侧阅读区即可打开")
        drag_tip.setWordWrap(True)
        theme.role(drag_tip, "caption")
        left_layout.addWidget(drag_tip)

        # 报告质控（常驻悬浮小窗：区域只设一次 → 之后点刷新/双击识别）
        self._reportqc_btn = QtWidgets.QPushButton("报告质控")
        self._reportqc_btn.setToolTip(
            "显示报告质控悬浮小窗。识别区域只需设置一次，"
            "之后每次点小窗上的「刷新」或双击小窗即可识别当前 PACS 报告")
        self._reportqc_btn.clicked.connect(self._on_reportqc_clicked)
        left_layout.addWidget(self._reportqc_btn)

        # 报告质控历史记录
        self._reportqc_hist_btn = QtWidgets.QPushButton("质控历史")
        self._reportqc_hist_btn.setToolTip(
            "查看报告质控的历史检测记录，可筛选、查看详情、导出 CSV")
        self._reportqc_hist_btn.clicked.connect(self._on_reportqc_history_clicked)
        left_layout.addWidget(self._reportqc_hist_btn)

        # v1.23.1：原来的「质控设置」按钮撤掉了 —— 设置类入口统一在「设置」里
        # （设置 → 报告质控：开关 + 质控设置…）。左栏按钮本来就多，能少一个是一个。

        # 设置按钮（原「帮助」按钮改版：帮助移入设置，设置里另含患者目录配置）
        settings_button = QtWidgets.QPushButton("设置")
        settings_button.setToolTip("患者扫描目录设置、使用说明")
        settings_button.clicked.connect(self._on_settings_clicked)
        left_layout.addWidget(settings_button)
        
        splitter.addWidget(left_panel)

        self._study_panel = StudyViewPanel()
        self._study_panel.mode_changed.connect(self._on_view_mode_changed)
        # 从患者树把序列卡片拖到阅读区 = 直接加载该序列
        self._study_panel.series_dropped.connect(self._on_series_dropped)
        # W 键切换二维/三维：改用窗口级 QShortcut。原先放在 MainWindow.keyPressEvent
        # 里，但焦点位于 VTK 渲染控件（OpenGL 原生窗口）时 Qt 按键事件到不了
        # MainWindow，表现为「按 W 没反应」；QShortcut(WindowShortcut) 在任意
        # 控件持有焦点时都能触发。
        self._shortcut_toggle_mode = QtWidgets.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_W), self)
        self._shortcut_toggle_mode.setContext(QtCore.Qt.WindowShortcut)
        self._shortcut_toggle_mode.activated.connect(self._on_shortcut_toggle_mode)
        # Ctrl+F 定位到患者搜索框（v1.15.1）。同样用窗口级快捷键：
        # 焦点在 VTK 控件时也生效。
        self._shortcut_find_patient = QtWidgets.QShortcut(
            QtGui.QKeySequence.Find, self)
        self._shortcut_find_patient.setContext(QtCore.Qt.WindowShortcut)
        self._shortcut_find_patient.activated.connect(self._focus_patient_search)
        # QuadViewer 首次创建后，连接 MPR 标签点击信号
        splitter.addWidget(self._study_panel)
        QtCore.QTimer.singleShot(0, self._connect_quad_signals)

        right_panel = QtWidgets.QWidget()
        right_panel.setObjectName("sidePanel")
        right_panel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        # 标题行：左边「工具区」，右边皮肤切换按钮（一键换肤的入口）
        header_row = QtWidgets.QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(4)
        header_title = QtWidgets.QLabel("工具区")
        theme.role(header_title, "section")
        header_row.addWidget(header_title)
        header_row.addStretch(1)
        # 在线升级入口：平时是个不起眼的小按钮，后台查到新版本才点亮
        self._update_btn = QtWidgets.QPushButton("更新")
        self._update_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._update_btn.setMinimumWidth(62)      # 文案变化时不跳动
        self._update_btn.setToolTip("检查在线更新\n当前版本 %s" % APP_VERSION)
        self._update_btn.clicked.connect(self._on_check_update)
        theme.role(self._update_btn, "tonal")
        header_row.addWidget(self._update_btn)
        self._update_plan = None
        self._update_dialog = None
        self._update_checker = None
        # 启动 6 秒后静默查一次：子线程跑，不挡界面；查不到就安静算了
        QtCore.QTimer.singleShot(6000, self._start_silent_update_check)

        self._skin_switcher = theme.SkinSwitcher(self)
        self._skin_switcher.setToolTip(
            "界面皮肤：点一下挑一套，立即生效，不用重启"
            "\n当前「%s」" % theme.display_name())
        theme.on_change(self._on_skin_changed)
        header_row.addWidget(self._skin_switcher)
        right_layout.addLayout(header_row)

        # 路径标签不再添加到UI，但保留用于内部状态跟踪
        self._path_label = QtWidgets.QLabel("未加载DICOM目录")
        self._path_label.setWordWrap(True)

        load_button = QtWidgets.QPushButton("加载患者目录")
        load_button.clicked.connect(self._on_load_clicked)
        right_layout.addWidget(load_button)

        open_file_button = QtWidgets.QPushButton("打开影像文件")
        open_file_button.setToolTip(
            "打开单个影像文件：DICOM（.dcm）或常见图片（%s）。\n"
            "也可以选某个序列目录直接打开；\n"
            "或把文件/文件夹从桌面、资源管理器拖到窗口任意位置。"
            % "、".join(IMAGE_FILE_EXTS))
        open_file_button.clicked.connect(self._on_open_dicom_clicked)
        right_layout.addWidget(open_file_button)

        # ===== 脊柱拼接（全脊柱拼接 + Cobb 角测量）=====
        # 独立工作台：从 spine 子项目（spine/spine_app.py）进程内集成而来，
        # 算法（SIFT 位移 / 亮度匹配 / 羽化 / Cobb 估线）全部复用原实现，
        # 详见 spine_bridge.py。点开后可直接把影像或文件夹拖进去拼接。
        self._spine_stitch_btn = QtWidgets.QPushButton("脊柱拼接")
        self._spine_stitch_btn.setToolTip(
            "全脊柱图像拼接与 Cobb 角测量。\n"
            "点开后把脊柱 X 线影像（或整个文件夹）拖进窗口即可拼接，\n"
            "可自动或手动测量 Cobb 角，并导出 PNG / DICOM。")
        self._spine_stitch_btn.clicked.connect(self._on_spine_stitch_clicked)
        right_layout.addWidget(self._spine_stitch_btn)

        # 「刷新患者列表」按钮已移到左侧患者列表区（left_header_layout）

        # ===== 显示模式切换 =====
        self._mode_label = QtWidgets.QLabel("显示模式: 二维阅读")
        theme.role(self._mode_label, "accent")
        right_layout.addWidget(self._mode_label)

        self._view_mode_combo = QtWidgets.QComboBox()
        self._view_mode_combo.addItem("二维阅读 (默认)", "2d")
        self._view_mode_combo.addItem("三维重建", "3d")
        self._view_mode_combo.setToolTip("快捷键 W 可快速切换；载入序列后默认进入二维阅读模式")
        self._view_mode_combo.currentIndexChanged.connect(self._on_view_mode_combo_changed)
        right_layout.addWidget(self._view_mode_combo)

        self._series_info_label = QtWidgets.QLabel("窗宽/窗位: -")
        self._series_info_label.setWordWrap(True)
        right_layout.addWidget(self._series_info_label)

        # ===== 窗宽窗位（两种模式通用） =====
        # 预设值是 CT 专用的（肺窗/纵膈窗/骨窗/脑窗），X 线下会隐藏
        self._window_preset_label = QtWidgets.QLabel("窗宽/窗位预设:")
        right_layout.addWidget(self._window_preset_label)
        self._window_preset_combo = QtWidgets.QComboBox()
        self._window_preset_combo.addItem("自定义", (None, None))
        self._window_preset_combo.addItem("肺窗 (WW:1500 WL:-600)", (1500, -600))
        self._window_preset_combo.addItem("纵膈窗 (WW:400 WL:40)", (400, 40))
        self._window_preset_combo.addItem("骨窗 (WW:1500 WL:300)", (1500, 300))
        self._window_preset_combo.addItem("脑窗 (WW:80 WL:40)", (80, 40))
        self._window_preset_combo.currentIndexChanged.connect(self._on_window_preset_changed)
        right_layout.addWidget(self._window_preset_combo)

        # 自定义窗宽窗位调节
        custom_ww_wl_layout = QtWidgets.QHBoxLayout()
        self._ww_spin = QtWidgets.QSpinBox()
        # 上限覆盖 16bit：X 线的窗宽可达 7000+，沿用 4000 会调不动
        self._ww_spin.setRange(1, 100000)
        self._ww_spin.setValue(400)
        self._ww_spin.setPrefix("WW:")
        self._ww_spin.valueChanged.connect(self._on_custom_ww_wl_changed)
        custom_ww_wl_layout.addWidget(self._ww_spin)

        self._wl_spin = QtWidgets.QSpinBox()
        self._wl_spin.setRange(-40000, 40000)
        self._wl_spin.setValue(160)
        self._wl_spin.setPrefix("WL:")
        self._wl_spin.valueChanged.connect(self._on_custom_ww_wl_changed)
        custom_ww_wl_layout.addWidget(self._wl_spin)
        right_layout.addLayout(custom_ww_wl_layout)

        # ===== 三维重建参数（仅三维模式显示） =====
        self._volume_params_label = QtWidgets.QLabel("—— 三维重建参数 ——")
        theme.role(self._volume_params_label, "muted")
        right_layout.addWidget(self._volume_params_label)

        self._largest_region_checkbox = QtWidgets.QCheckBox("去除细小碎屑")
        self._largest_region_checkbox.setChecked(True)
        self._largest_region_checkbox.setToolTip(
            "只剔除远离骨块的漂浮噪点碎屑（对比剂血管段、重建噪斑等）；\n"
            "骨表面 6 个体素内的噪声壳层受邻接保护，不会破坏骨质感")
        self._largest_region_checkbox.toggled.connect(self._on_largest_region_toggled)
        right_layout.addWidget(self._largest_region_checkbox)

        self._remove_bed_checkbox = QtWidgets.QCheckBox("自动去床板")
        self._remove_bed_checkbox.setChecked(True)
        self._remove_bed_checkbox.setToolTip(
            "按 CT 密度自动识别床体并整块移除：床材（碳纤维板/塑料/泡沫垫/\n"
            "U 型托架/护栏）及检查床与 FOV 边界伪影的融合壳，密度远低于骨质；\n"
            "位于身体外侧的床块还会向图像外缘整体切除（含支柱/碎段残留）")
        self._remove_bed_checkbox.toggled.connect(self._on_remove_bed_toggled)
        right_layout.addWidget(self._remove_bed_checkbox)

        # 建议 2：低功耗模式开关
        self._low_power_checkbox = QtWidgets.QCheckBox("低功耗模式 (滑动更流畅)")
        self._low_power_checkbox.setChecked(True)
        self._low_power_checkbox.setToolTip("开启后，滑动阈值时会降低图像质量以提升响应速度，释放时恢复质量")
        self._low_power_checkbox.toggled.connect(self._on_low_power_toggled)
        right_layout.addWidget(self._low_power_checkbox)

        self._color_label = QtWidgets.QLabel("三维颜色")
        right_layout.addWidget(self._color_label)
        self._color_combo = QtWidgets.QComboBox()
        self._color_combo.addItem("默认骨色", "sample_bone")
        self._color_combo.addItem("GE骨白", "ge_bone")
        self._color_combo.currentIndexChanged.connect(self._on_color_mode_changed)
        right_layout.addWidget(self._color_combo)

        self._enhance_fractures_checkbox = QtWidgets.QCheckBox("骨折增强")
        self._enhance_fractures_checkbox.setChecked(True)
        self._enhance_fractures_checkbox.setToolTip(
            "增强细微骨折显示：自动识别骨内低密度薄片（未移位骨折缝），"
            "在骨面上以暗色细线标出。\n"
            "骨面本身的颜色与光照完全不变，不会影响画质；"
            "只对 CT 表面重建生效，MR 与 MIP/MinIP 投影不受影响。")
        self._enhance_fractures_checkbox.toggled.connect(
            self._on_enhance_fractures_toggled)
        right_layout.addWidget(self._enhance_fractures_checkbox)

        self._threshold_label = QtWidgets.QLabel("骨组织阈值: -")
        right_layout.addWidget(self._threshold_label)
        self._threshold_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._threshold_slider.setRange(50, 500)
        self._threshold_slider.setValue(200)
        self._threshold_slider.valueChanged.connect(self._on_threshold_slider_changed)
        self._threshold_slider.sliderPressed.connect(self._on_threshold_pressed)
        self._threshold_slider.sliderReleased.connect(self._on_threshold_released)
        right_layout.addWidget(self._threshold_slider)

        self._threshold_preset_label = QtWidgets.QLabel("骨阈值预设")
        right_layout.addWidget(self._threshold_preset_label)
        self._threshold_preset_combo = QtWidgets.QComboBox()
        self._threshold_preset_combo.addItem("四肢骨 (200 HU)", 200)
        self._threshold_preset_combo.addItem("肋骨 (150 HU)", 150)
        self._threshold_preset_combo.addItem("脊柱 (180 HU)", 180)
        self._threshold_preset_combo.addItem("颅脑 (250 HU)", 250)
        self._threshold_preset_combo.addItem("骨盆 (200 HU)", 200)
        self._threshold_preset_combo.currentIndexChanged.connect(self._on_threshold_preset_changed)
        right_layout.addWidget(self._threshold_preset_combo)
        self._pending_iso_value = 200

        # ---- MR 血管三维专用控件（仅MRI 载入时显示，默认隐藏）----
        # MR 像素是信号强度而非 HU：CT 的「骨阈值 50~500 HU」刻度对 MR
        # 毫无意义（实测 MRA 血管阈值在 350~500、ADC 图可达数千），
        # 所以另建一套20~8000 的刻度与按实测标定的预设。
        self._mr_vessel_label = QtWidgets.QLabel("血管信号阈值: -")
        theme.role(self._mr_vessel_label, "accent")
        right_layout.addWidget(self._mr_vessel_label)
        self._mr_vessel_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._mr_vessel_slider.setRange(20, 8000)
        self._mr_vessel_slider.setValue(440)
        self._mr_vessel_slider.setToolTip(
            "MRA 血管提取阈值（信号强度，非 HU）。\n"
            "实测参考区间（本机TOF 186张 0.5mm 薄层）：\n"
            "  350~500  → 血管树（最佳区400~450）\n"
            "  <350     → 阈值过低，会把脑组织一起烧成壳\n"
            "  >600     → 阈值过高，只剩粗主干、丢失末梢\n"
            "载入 MRI 时会按该序列的强度分布自动推荐，默认即最佳区。")
        self._mr_vessel_slider.valueChanged.connect(self._on_mr_vessel_changed)
        self._mr_vessel_slider.sliderPressed.connect(self._on_threshold_pressed)
        self._mr_vessel_slider.sliderReleased.connect(self._on_threshold_released)
        right_layout.addWidget(self._mr_vessel_slider)

        self._mr_vessel_preset_combo = QtWidgets.QComboBox()
        # index 0 = 自动（载入时按该序列强度分布算出，落在实测最佳区400~450）
        # MR_AUTO 用哨兵值而非 None，这样「自定义」可以与「自动」区分开
        self._mr_vessel_preset_combo.addItem("自动（推荐）", "auto")
        self._mr_vessel_preset_combo.addItem("仅主干 (550)", 550)
        self._mr_vessel_preset_combo.addItem("末梢清晰 (380)", 380)
        self._mr_vessel_preset_combo.addItem("自定义", "custom")
        self._mr_vessel_preset_combo.currentIndexChanged.connect(
            self._on_mr_vessel_preset_changed)
        right_layout.addWidget(self._mr_vessel_preset_combo)
        self._pending_mr_vessel = 440
        self._mr_vessel_widgets = [
            self._mr_vessel_label,
            self._mr_vessel_slider,
            self._mr_vessel_preset_combo,
        ]
        for w in self._mr_vessel_widgets:
            w.setVisible(False)

        # ===== 切片控制（标签随模式变化） =====
        self._mpr_label = QtWidgets.QLabel("切片位置: 50%")
        right_layout.addWidget(self._mpr_label)

        self._mpr_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._mpr_slider.setRange(0, 1000)
        self._mpr_slider.setValue(500)
        self._mpr_slider.valueChanged.connect(self._on_mpr_changed)
        right_layout.addWidget(self._mpr_slider)

        # ===== 三维渲染模式：表面重建 / MIP / MinIP =====
        self._projection_label = QtWidgets.QLabel("渲染模式:")
        right_layout.addWidget(self._projection_label)
        self._projection_combo = QtWidgets.QComboBox()
        for mode in PROJECTION_MODES:
            self._projection_combo.addItem(PROJECTION_LABELS[mode], mode)
        self._projection_combo.currentIndexChanged.connect(
            self._on_projection_mode_changed)
        right_layout.addWidget(self._projection_combo)

        # 显隐分组（v1.10.1 起投影分两种实现，显隐逻辑见
        # _update_param_widgets_visibility）：
        # - _threshold_widgets：表面重建 + 三维体渲染 MIP/MinIP 都显示
        #   （三维投影的传递函数/掩膜依赖阈值与去床板，拖滑块有实际效果）；
        # - 伪彩配色只对表面重建有意义，体渲染与二维投影都隐藏；
        # - 二维投影图（回退路径）只吃体数据，全部隐藏。
        self._threshold_widgets = [
            self._volume_params_label,
            self._largest_region_checkbox,
            self._remove_bed_checkbox,
            self._low_power_checkbox,
            self._threshold_label,
            self._threshold_slider,
            self._threshold_preset_label,
            self._threshold_preset_combo,
        ]
        self._color_widgets = [
            self._color_label,
            self._color_combo,
            self._enhance_fractures_checkbox,
        ]
        self._surface_only_widgets = (self._threshold_widgets
                                      + self._color_widgets)

        # 三维模式专属项集合（用于按模式显隐）
        self._volume_widgets = [
            self._volume_params_label,
            self._largest_region_checkbox,
            self._remove_bed_checkbox,
            self._low_power_checkbox,
            self._color_label,
            self._color_combo,
            self._enhance_fractures_checkbox,
            self._threshold_label,
            self._threshold_slider,
            self._threshold_preset_label,
            self._threshold_preset_combo,
            self._projection_label,
            self._projection_combo,
        ]

        reset_button = QtWidgets.QPushButton("重置视角")
        reset_button.clicked.connect(self._on_reset_view)
        right_layout.addWidget(reset_button)
        close_button = QtWidgets.QPushButton("关闭当前图像")
        close_button.clicked.connect(self._on_close_current)
        right_layout.addWidget(close_button)

        # ===== X 线 AI 辅助诊断 =====
        # 只在载入 X 线（DR/DX/CR）序列时显示，CT 阅片流程完全不受影响。
        self._build_xray_ai_panel(right_layout)


        # 右下角不再放「帮助」按钮，左下角已有一个
        right_layout.addStretch(1)
        splitter.addWidget(right_panel)
        # 左侧面板要容纳 96px 的序列缩略图，宽度相应放宽
        left_panel.setMinimumWidth(230)
        splitter.setSizes([300, 880, 320])
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 1)

        # 延迟加载患者列表，避免启动时卡顿
        self._clear_patient_tree()
        self._patient_tree.addTopLevelItem(QtWidgets.QTreeWidgetItem(["正在加载患者列表..."]))
        self._apply_view_mode_ui(StudyViewPanel.MODE_2D)
        QtCore.QTimer.singleShot(100, lambda: self._load_patient_tree(self._root_dir))

    # ---------- 在线升级 ----------
    def _on_check_update(self):
        """打开在线升级窗口（检查 → 下载 → 落地）。"""
        try:
            import update_ui
        except Exception as e:                   # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "无法检查更新",
                "升级模块加载失败：%s\n\n请确认软件目录里 update_ui.py / updater.py 都在。"
                % e)
            return
        app_dir = os.path.dirname(os.path.abspath(__file__))
        # 地址走配置：设置里改过主机就按新地址查，没改过自动回退内置默认
        dlg = update_ui.UpdateDialog(app_dir, APP_VERSION, self,
                                     base_url=self._load_update_base_url())
        self._update_dialog = dlg
        dlg.exec_()
        if dlg.restart_requested:
            # 安装器已经在跑：立刻退出，把 main.py / pyc 的句柄让出去，
            # 否则安装器覆盖文件会失败
            QtWidgets.QApplication.quit()

    def _start_silent_update_check(self):
        """启动后台静默检查。没有网络、服务器不通都不打扰用户。"""
        try:
            import update_ui
        except Exception:                        # noqa: BLE001
            return
        base = (self._load_update_base_url()
                or update_ui.updater.DEFAULT_BASE_URL)
        if not base:
            # 未配置升级服务器（开源版默认如此），跳过检查
            return
        self._update_checker = update_ui.SilentChecker(
            base, APP_VERSION, self)
        self._update_checker.start(self._on_update_available)

    def _on_update_available(self, plan):
        """后台查到新版本：只点亮按钮，不弹窗。"""
        self._update_plan = plan
        try:
            self._update_btn.setText("● 更新")
            self._update_btn.setToolTip(
                "发现新版本 %s（当前 %s）\n点这里升级"
                % (plan.get('latest'), APP_VERSION))
            theme.role(self._update_btn, "primary")
        except Exception:                        # noqa: BLE001
            pass

    # ---------- 皮肤 ----------
    def _on_skin_changed(self, _key=None):
        """切皮肤后需要重算的自绘缓存与状态色。

        大部分控件由全局 QSS / theme.bind 自动跟随，这里只补两类：
        ① 缩略图占位图（QPixmap 缓存，必须重建）；
        ② 授权状态标签（颜色取决于状态，不是固定角色）。
        """
        try:
            if getattr(self, "_series_delegate", None) is not None:
                self._series_delegate.invalidate_placeholder()
            btn = getattr(self, "_skin_switcher", None)
            if btn is not None:
                btn.refresh()
            if getattr(self, "_license_label", None) is not None:
                self._refresh_license_label()
        except Exception:          # noqa: BLE001
            pass

    # ---------- 授权状态显示 ----------
    def _refresh_license_label(self):
        """刷新状态栏上的授权标签（显示类型与剩余时间）"""
        try:
            import license_bridge
            st = license_bridge.license_status()
        except Exception as exc:            # noqa: BLE001
            self._license_label.setText("授权状态未知")
            theme.role(self._license_label, "danger")
            self._license_label.setToolTip("读取授权状态失败：%s" % exc)
            return

        if not st["ok"]:
            self._license_label.setText("⚠ 未授权")
            theme.role(self._license_label, "danger")
            self._license_label.setToolTip(st["message"])
            return

        kind = st["kind"]
        if kind == "unlimited":
            text, role = "已授权（永久）", "success"
        elif kind == "permanent" and st["hours_left"] >= 0:
            text, role = "已授权（剩余 %d 小时）" % st["hours_left"], "warn"
        elif kind == "trial":
            text, role = "试用中（剩余 %d 小时）" % max(0, st["hours_left"]), "warn"
        else:
            text, role = "已授权", "success"
        self._license_label.setText(text)
        theme.role(self._license_label, role)
        self._license_label.setToolTip("%s\n双击查看/更换授权" % st["message"])

    def _on_license_label_clicked(self, event):
        """状态栏授权标签：双击打开激活窗口（换机/续期用）"""
        if event.button() == QtCore.Qt.LeftButton:
            try:
                import license_ui
                license_ui.open_activate_dialog(self)
                # 激活状态可能变了，回来后刷新标签
                self._refresh_license_label()
            except Exception as exc:        # noqa: BLE001
                QtWidgets.QMessageBox.warning(
                    self, "授权窗口打不开", "打开授权窗口失败：\n%s" % exc)

    # ---------- 显示模式（二维 / 三维） ----------
    def _connect_quad_signals(self):
        """QuadViewer 创建后连接 MPR 标签点击信号（仅连接一次）"""
        if getattr(self, "_quad_signals_connected", False):
            return
        quad = self._study_panel.quad_viewer()
        quad._axial_view.label_clicked.connect(self._cycle_window_presets)
        quad._sagittal_view.label_clicked.connect(self._cycle_window_presets)
        quad._coronal_view.label_clicked.connect(self._cycle_window_presets)
        self._quad_signals_connected = True

    def _on_view_mode_combo_changed(self, index):
        mode = self._view_mode_combo.itemData(index)
        self._study_panel.set_mode(mode)

    def _on_view_mode_changed(self, mode: str):
        """StudyViewPanel 模式变化后的 UI 同步"""
        self._apply_view_mode_ui(mode)
        if mode == StudyViewPanel.MODE_3D:
            QtCore.QTimer.singleShot(0, self._render_3d_after_toggle)

    def _on_projection_mode_changed(self, index: int):
        """渲染模式（表面/MIP/MinIP）切换。

        槽函数一律用具名方法：`currentIndexChanged` 会带 int 参数，
        与「无参信号用 lambda」是两类错误，别混。
        """
        try:
            mode = self._projection_combo.itemData(index)
        except Exception:
            return
        self._apply_projection_mode(mode)

    def _update_param_widgets_visibility(self):
        """按「显示模式 + 渲染模式 + 投影实现」统一刷新参数区显隐。

        - 二维阅读：全部隐藏；
        - 表面重建：阈值/去床板/配色全显示；
        - 三维体渲染 MIP/MinIP：阈值/去床板显示（传递函数与掩膜依赖它们，
          拖滑块有实际效果），伪彩配色隐藏；
        - 二维投影图（回退路径）：全部隐藏（它只吃体数据，任何参数都不参与）。
        """
        if not self._is_3d_mode():
            vis_threshold = vis_color = False
        else:
            viewer = self._current_volume_view()
            impl = getattr(viewer, "_projection_impl", "surface")
            mode = getattr(viewer, "_projection_mode", PROJECTION_SURFACE)
            if mode == PROJECTION_SURFACE:
                vis_threshold = vis_color = True
            elif impl == "volume":
                vis_threshold, vis_color = True, False
            else:
                vis_threshold = vis_color = False
        for w in self._threshold_widgets:
            w.setVisible(vis_threshold)
        for w in self._color_widgets:
            w.setVisible(vis_color)

    def _apply_projection_mode(self, mode: str):
        """把渲染模式推到三维视图，并同步参数区显隐。"""
        mode = mode or PROJECTION_SURFACE
        is_surface = (mode == PROJECTION_SURFACE)

        # 先按保守口径隐藏（可能还没有视图），切换成功后再精确刷新
        if not is_surface:
            for w in self._threshold_widgets + self._color_widgets:
                w.setVisible(False)

        # 只读**已经存在**的三维视图，不要因为改了下拉框就新建一个：
        # QuadViewer 里躺着 4 个 VTK OpenGL 渲染窗，构造它既重又会
        # 抢走 GPU 上下文；而且用户此时多半还没载入影像，建了也是空的。
        # 没有视图 = 不在三维模式或还没数据 → 复位下拉框并说明。
        viewer = self._current_volume_view()
        if viewer is None:
            self._sync_projection_combo(PROJECTION_SURFACE)
            self._update_param_widgets_visibility()
            if not is_surface:
                QtWidgets.QMessageBox.information(
                    self, "请先载入影像",
                    "最大/最小密度投影需要先载入影像数据。\n\n"
                    "请双击左侧某个序列载入，再切到「三维重建」使用。")
            return
        try:
            ok, message = viewer.set_projection_mode(mode)
        except Exception as exc:      # noqa: BLE001
            # 用户可触发的路径必须弹窗，绝不静默（否则用户以为切了其实没切）
            QtWidgets.QMessageBox.critical(
                self, "切换渲染模式失败", "切换到「%s」时出错：\n%s"
                % (PROJECTION_LABELS.get(mode, mode), exc))
            self._sync_projection_combo(PROJECTION_SURFACE)
            return
        if not ok:
            QtWidgets.QMessageBox.warning(
                self, "无法切换渲染模式", message)
            self._sync_projection_combo(PROJECTION_SURFACE)
            return
        # 切换成功后按实际实现（三维体渲染 or 二维回退）精确刷新参数区
        self._update_param_widgets_visibility()
        if message:
            # 抽样提示/回退说明是「知情权」而非错误，用信息框告知
            QtWidgets.QMessageBox.information(
                self, PROJECTION_LABELS.get(mode, mode), message)

    def _sync_projection_combo(self, mode: str):
        idx = self._projection_combo.findData(mode)
        if idx >= 0 and idx != self._projection_combo.currentIndex():
            self._projection_combo.blockSignals(True)
            self._projection_combo.setCurrentIndex(idx)
            self._projection_combo.blockSignals(False)

    def _is_3d_mode(self) -> bool:
        # 注意 mode 是 @property，直接读值不是调用方法
        try:
            return self._study_panel.mode == StudyViewPanel.MODE_3D
        except Exception:
            return False

    def _current_volume_view(self):
        """当前生效的三维视图。

        `StudyViewPanel._quad_viewer` 是**惰性创建**的（未进三维模式时为 None）。
        这里**只读不建**：拿不到就返回 None，由调用方按「还没进三维模式」
        处理。绝不能顺手去创建 —— 见下方注释。
        """
        try:
            return self._study_panel.current_volume_view()
        except Exception:
            return None

    # 注意：这里**不要**加一个 `_ensure_quad_viewer()` 之类的"帮我建一个"。
    # QuadViewer 里躺着 4 个 VTK OpenGL 渲染窗，构造它既重又会抢 GPU 上下文，
    # 在无数据时被 UI 逻辑意外触发会直接段错误（offscreen 下必崩）。
    # 渲染模式切换一律走 `_current_volume_view()` 只读已有视图。

    def _apply_view_mode_ui(self, mode: str):
        is_3d = (mode == StudyViewPanel.MODE_3D)

        # 下拉框同步（阻断信号防止递归）
        idx = self._view_mode_combo.findData(mode)
        if idx >= 0 and idx != self._view_mode_combo.currentIndex():
            self._view_mode_combo.blockSignals(True)
            self._view_mode_combo.setCurrentIndex(idx)
            self._view_mode_combo.blockSignals(False)

        self._mode_label.setText("显示模式: 三维重建" if is_3d else "显示模式: 二维阅读")
        self._mpr_label.setText("MPR切片同步: 50%" if is_3d else "切片位置: 50%")

        # 三维专属参数区显隐（含渲染模式下拉框）
        for w in self._volume_widgets:
            w.setVisible(is_3d)

        # 切到二维时把渲染模式复位成「表面」：三维窗口不可见时留在 MIP 上，
        # 下次切回来会让人以为三维一直是投影状态
        if not is_3d:
            self._sync_projection_combo(PROJECTION_SURFACE)
        self._update_param_widgets_visibility()

    def _apply_modality_ui(self, is_xray: bool, is_mr: bool = False):
        """按影像模态切换右侧工具区。

        X 线：显示 AI 辅助诊断分组、禁用三维模式入口、隐藏三维参数；
        CT  ：隐藏 AI 分组，一切与原流程一致；
        MR  ：隐藏 AI 分组，**切换到血管信号阈值控件**、隐藏 CT 骨阈值预设
              （HU 刻度对信号强度无意义），并关闭「自动去床板」
              （MR 无 CT 密度可依）。
        """
        if hasattr(self, "_xray_group"):
            self._xray_group.setVisible(bool(is_xray))

        if is_xray:
            self._xray_clear_result()
            # 先判检查部位再放开按钮：胸片不给足弓/骨龄，腕片不给胸片检测……
            # 顺序不能反，否则这一轮刷新出来的是未过滤的按钮状态。
            self._xray_refresh_part()
            self._set_xray_ui_enabled(
                True,
                "%s。%s" % (self._xray_part_hint(),
                            "请选择检测功能或点「智能识别并检测」"))
        else:
            self._xray_part_key = XRAY_PART_UNKNOWN
            self._set_xray_ui_enabled(False, "请先载入 X 线影像")


        # 窗值预设是 CT 专用（肺窗/纵膈窗/骨窗/脑窗），X 线下隐藏避免误导；
        # MR 有自己的窗值来源（DICOM 序列自带 / MR 预设 / 分位数），
        # CT 那套 HU 预设同样不适用，一并隐藏。
        for w in (getattr(self, "_window_preset_label", None),
                  getattr(self, "_window_preset_combo", None)):
            if w is not None:
                w.setVisible(not is_xray and not is_mr)

        # 骨阈值相关控件：CT 专用（HU 刻度 + 骨阈值预设 + 骨折增强）
        for w in (getattr(self, "_threshold_label", None),
                  getattr(self, "_threshold_preset_label", None),
                  getattr(self, "_threshold_preset_combo", None),
                  getattr(self, "_enhance_fractures_checkbox", None)):
            if w is not None:
                w.setVisible(not is_mr)
        if getattr(self, "_threshold_label", None) is not None:
            self._threshold_label.setText("骨组织阈值: -" if not is_mr
                                          else "血管信号阈值: -")

        # MR 血管阈值控件：仅 MR 可见
        for w in getattr(self, "_mr_vessel_widgets", []):
            w.setVisible(bool(is_mr))

        # 去床板是 CT 密度判据，MR 下无意义
        cb = getattr(self, "_remove_bed_checkbox", None)
        if cb is not None:
            cb.setEnabled(not is_mr)
            if is_mr:
                cb.setChecked(False)
                cb.setToolTip("MR 为信号强度成像，没有 CT 密度可依，床体自动识别不适用")

        # 三维模式下拉框：X 线时整个禁用（二维是唯一选项）
        if hasattr(self, "_view_mode_combo"):
            self._view_mode_combo.setEnabled(not is_xray)
            if is_xray:
                tip = "X 线为单帧投影影像，仅支持二维阅读"
            elif is_mr:
                tip = "MRA 血管可做三维重建；普通 MR 序列建议二维阅片"
            else:
                tip = "快捷键 W 可快速切换；载入序列后默认进入二维阅读模式"
            self._view_mode_combo.setToolTip(tip)

    def _render_3d_after_toggle(self):
        """切换到三维后，确保三维管线渲染完成"""
        if self._study_panel.mode != StudyViewPanel.MODE_3D:
            return
        if not self._study_panel.has_volume():
            return
        quad = self._study_panel.quad_viewer()
        self._apply_iso_value_once()
        self._quad_viewer_volume_render(quad)

    @staticmethod
    def _quad_viewer_volume_render(quad):
        try:
            rw = quad._volume_view._vtk_widget.GetRenderWindow()
            if rw is not None:
                rw.Render()
        except Exception:
            pass

    def _on_shortcut_toggle_mode(self):
        """W 键（窗口级快捷键）：切换二维/三维模式"""
        if self._study_panel._btn_2d.isEnabled():
            self._study_panel.toggle_mode()

    def keyPressEvent(self, event):
        # W 键已由 QShortcut（_shortcut_toggle_mode）接管，见 __init__ 注释
        super().keyPressEvent(event)

    def _show_about(self):
        text = (
            "知骨影像阅读器\n"
            "版本：" + APP_VERSION + "\n"
            "作者：wmw\n\n"
            "使用方法：\n"
            "1. 左侧患者列表：单击患者姓名展开序列缩略图（不加载），"
            "双击序列卡片才加载图像；\n"
            "   患者名前缀 [CT] 为 CT 检查、[DR] 为 X 线检查、[MR] 为核磁检查，"
            "三者同列表共存；\n"
            "2. 患者扫描目录：默认 D:\\Cache 与 D:\\ClearTmp\\DicomTmp，"
            "可在左下角「设置 → 患者扫描目录」里添加/移除多个目录；\n"
            "3. 右侧工具区可调窗宽/窗位、骨阈值、MPR同步；\n"
            "4. 显示区左下角按钮可切换全屏/退出全屏；\n"
            "5. 2D 模式下可用左下角工具栏做测量：CT值（圆形 ROI 区域平均 HU）、"
            "长度（两点）、角度（三点），点「清除」移除。\n"
            "   CT值：点击放置圆心，滚轮或 [ ] 调整半径，显示区域内平均 HU；\n"
            "   测量时左键用于下点，平移请改用中键拖动或 Ctrl+滚轮；按 Esc 取消正在进行的测量。\n"
            "\n"
            "【二维多窗对比】\n"
            "阅读区上方「窗口：1 / 2 / 4」可切换布局，用于并排对比多个序列\n"
            "（如头部 MR 的 T1/T2/FLAIR/DWI）。每个窗口完全独立：\n"
            "· 先点一下某个窗口（亮蓝框 = 当前活动窗口），再双击或拖入序列卡片，\n"
            "  序列就载入到该窗口；\n"
            "· 各窗口的窗宽窗位、当前层、缩放平移互不影响；\n"
            "· 测量与 AI 标注只作用于活动窗口；\n"
            "· 切换窗口数不会丢失各窗口已载入的序列。\n"
            "窗口栏右侧的「同步」勾选后，任意窗口翻层或调窗值，\n"
            "其余窗口会跟着联动（层数不同时按比例对齐）——\n"
            "适合「a 窗肺窗、b 窗纵隔窗」同层对比，或 MR 的 T1 与 FLAIR 对读。\n"
            "\n"
            "【打开影像文件】\n"
            "· 把影像文件或序列文件夹从桌面 / 资源管理器直接拖到窗口任意位置即可打开；\n"
            "· 或点右侧「打开影像文件」按钮来选择文件——DICOM（.dcm）与常见图片\n"
            "  （PNG / JPG / BMP / TIFF / WebP）都支持；图片按 X 线单帧影像处理，\n"
            "  可直接用于 AI 辅助诊断；\n"
            "· 「加载患者目录」选到患者数据根目录会刷新列表，选到具体序列目录则直接打开。\n"
            "\n"
            "【患者列表配色】\n"
            "患者名按检查类型着色：CT 蓝色、DR 绿色、MR 紫色、混合橙色。\n"
            "\n"
            "【MRI 核磁共振】\n"
            "载入 MR 影像后，右侧「骨组织阈值」会自动换成「血管信号阈值」：\n"
            "· MRA 序列可做血管三维重建，看到 Willis 环与颈内动脉的立体形态；\n"
            "· 阈值默认按该序列强度自动推荐，也可拖动或选预设（主干清晰/平衡/末梢分支）；\n"
            "· CT 专用的窗值预设与 CT 值测量对 MR 自动隐藏（MR 是信号强度，不是 HU）；\n"
            "· 定位像与设备重建批次会自动跳过，同名序列自动编号区分。\n"
            "说明：普通 MR 序列（T1/T2/FLAIR/DWI/ADC）建议以二维阅片与 MPR 为主。\n"
            "\n"
            "【X 线辅助诊断】\n"
            "载入 X 线（DR/DX/CR）影像后，右侧出现「X线 AI 辅助诊断」分组：\n"
            "· 智能识别并检测：按 DICOM 检查部位自动选择合适的功能；\n"
            "· 胸片检测（14 类病变）/ 骨折检测 / 脊柱侧弯（Cobb 角）/ "
            "骨龄评估（RUS-CHN）/ 足弓测量；\n"
            "· 检测结果直接标注在图像上，右侧同步列出明细与关键指标；\n"
            "· 「清除 AI 标注」恢复原始影像；「检测设置」调整阈值；"
            "「检测记录」查看与导出历史。\n"
            "说明：X 线为单帧投影影像，仅支持二维阅读，三维重建不可用。"
        )
        QtWidgets.QMessageBox.information(self, "关于", text)

    def closeEvent(self, event):
        """程序关闭时的清理"""
        # 停止刷新线程
        if self._refresh_worker and self._refresh_worker.isRunning():
            self._refresh_worker.stop()
            self._refresh_worker.wait(1000)  # 等待线程结束

        # 停止计时器（若存在）
        timer = getattr(self, "_refresh_timer", None)
        if timer is not None:
            timer.stop()

        # 停止缩略图工作线程
        worker = getattr(self, "_thumb_worker", None)
        if worker is not None:
            self._thumb_items.clear()
            worker.stop()
            worker.wait(1500)

        # 停止 X 线 AI 推理线程（模型推理可能正在跑，等它收尾）
        xw = self._xray_worker
        if xw is not None and xw.isRunning():
            xw.stop()
            xw.wait(3000)

        # 关闭报告质控的框选遮罩与小窗（否则主程序退出后它们还浮在桌面上）
        try:
            import reportqc_bridge as _rqc
            _ctrl = _rqc.get_controller()
            if getattr(_ctrl, "_picker", None) is not None:
                _ctrl._picker.close()
                _ctrl._picker = None
            _ctrl.close_window()
            for _d in list(getattr(_ctrl, "_dialogs", [])):
                try:
                    _d.close()
                except Exception:
                    pass
            # 停掉前台窗口轮询（1s 定时器），避免进程退出瞬间回调到
            # 已析构的 Qt 对象上
            _stop = getattr(_ctrl, "stop_watcher", None)
            if callable(_stop):
                _stop()
        except Exception:
            pass

        # 清理临时文件夹（含多窗：每个窗口可能各持有一个暂存目录）
        for temp in self._all_temp_folders():
            try:
                self._cleanup_temp_folder(temp)
            except Exception:
                pass
        self._view_temp_folders = {}
        temp = getattr(self, "_last_temp_folder", None)
        self._last_temp_folder = None
        if temp:
            try:
                self._cleanup_temp_folder(temp)
            except Exception:
                pass

        super().closeEvent(event)

    def _create_app_icon_pixmap(self, size: int) -> QtGui.QPixmap:
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        pen = QtGui.QPen(QtGui.QColor(220, 50, 47))
        pen.setWidth(max(2, size // 28))
        p.setPen(pen)
        p.setBrush(QtGui.QBrush(QtGui.QColor(245, 245, 245)))
        p.drawEllipse(1, 1, size - 2, size - 2)
        spine = QtGui.QPen(QtGui.QColor(220, 50, 47))
        spine.setWidth(max(2, size // 24))
        p.setPen(spine)
        p.drawLine(int(size * 0.55), int(size * 0.22), int(size * 0.55), int(size * 0.78))
        rib_pen = QtGui.QPen(QtGui.QColor(220, 50, 47))
        rib_pen.setWidth(max(2, size // 30))
        p.setPen(rib_pen)
        for i in range(5):
            y = size * (0.28 + i * 0.1)
            path = QtGui.QPainterPath()
            path.moveTo(size * 0.25, y)
            path.cubicTo(size * 0.38, y - size * 0.04, size * 0.48, y - size * 0.02, size * 0.55, y)
            p.drawPath(path)
            path2 = QtGui.QPainterPath()
            path2.moveTo(size * 0.55, y)
            path2.cubicTo(size * 0.62, y - size * 0.02, size * 0.72, y - size * 0.04, size * 0.80, y)
            p.drawPath(path2)
        p.end()
        return pm
    def _on_threshold_preset_changed(self, index: int):
        value = int(self._threshold_preset_combo.currentData())
        self._threshold_label.setText(f"骨组织阈值: {value}")
        self._pending_iso_value = value
        
        # 内嵌遮罩代替系统进度框：不抢焦点、不弹窗（loading_ui）
        with loading_ui.busy_scope(self._study_panel,
                                   "正在切换阈值预设并渲染...",
                                   sub="ISO THRESHOLD · %d" % int(value)):
            # 同步滑动条
            self._threshold_slider.blockSignals(True)
            self._threshold_slider.setValue(value)
            self._threshold_slider.blockSignals(False)
            self._study_panel.set_iso_value(value)
    
    def _on_threshold_slider_changed(self, value: int):
        self._threshold_label.setText(f"骨组织阈值: {int(value)}")
        self._pending_iso_value = int(value)
        self._study_panel.set_iso_value(int(value))

    def _on_mr_vessel_changed(self, value: int, mark_custom: bool = True):
        """MR 血管信号强度阈值变化"""
        self._mr_vessel_label.setText(f"血管信号阈值: {int(value)}")
        self._pending_mr_vessel = int(value)
        # 手动拖动滑块即脱离「自动」，把下拉框切到「自定义」——
        # 否则下次自动重算会把用户刚调好的值悄悄改回去。
        if mark_custom and self._mr_vessel_preset_combo.currentIndex() != 3:
            self._mr_vessel_preset_combo.blockSignals(True)
            self._mr_vessel_preset_combo.setCurrentIndex(3)
            self._mr_vessel_preset_combo.blockSignals(False)
        self._study_panel.set_iso_value(int(value))

    def _on_mr_vessel_preset_changed(self, index: int):
        value = self._mr_vessel_preset_combo.currentData()
        if value == "auto":
            # 重新按当前序列的强度分布自动推荐
            self._auto_set_mr_vessel_threshold()
            return
        if value == "custom":
            # 自定义：不改变当前值，等用户拖滑块
            return
        self._mr_vessel_slider.blockSignals(True)
        self._mr_vessel_slider.setValue(int(value))
        self._mr_vessel_slider.blockSignals(False)
        self._on_mr_vessel_changed(int(value), mark_custom=False)

    def _apply_mr_vessel_threshold_auto(self, reader):
        """按已载入 MR 体数据的强度分布设定血管阈值（不依赖 _current_reader）

        载入流程里 reader 是局部变量，此时 self._current_reader 还没更新，
        因此不能复用 _auto_set_mr_vessel_threshold()，单独按入参算。
        """
        self._set_mr_vessel_threshold_from(reader)

    def _auto_set_mr_vessel_threshold(self):
        """按已载入 MR 序列的像素分布自动推荐血管阈值

        复用 mra_vessel_threshold 的标定（基于 p99.9 的倍数），对 MRA
        之外的 MR 序列也同样适用：阈值跟着该序列自己的强度上界走。
        """
        reader = getattr(self, "_current_reader", None)
        if reader is None:
            return
        self._set_mr_vessel_threshold_from(reader)

    def _set_mr_vessel_threshold_from(self, reader):
        """按体数据强度分布设定血管阈值，并切回「自动」

        标定见 mra_vessel_threshold：实测最佳区是 p99.9 的 1.0~1.1 倍
        （本机 417→400~450）。切回 index 0 表示这是自动值，
        之后用户手动拖动会切到「自定义」，不会被自动重算覆盖。
        """
        try:
            reader.Update()
            out = reader.GetOutput()
            from vtkmodules.util.numpy_support import vtk_to_numpy
            arr = vtk_to_numpy(out.GetPointData().GetScalars())
            if arr is None or arr.size == 0:
                return
            ext = out.GetExtent()
            nx = ext[1] - ext[0] + 1
            ny = ext[3] - ext[2] + 1
            nz = ext[5] - ext[4] + 1
            thr, _stats = mra_vessel_threshold(arr.reshape(nz, ny, nx))
            if thr is None:
                return
            value = int(np.clip(thr, self._mr_vessel_slider.minimum(),
                                self._mr_vessel_slider.maximum()))
            self._mr_vessel_slider.blockSignals(True)
            self._mr_vessel_slider.setValue(value)
            self._mr_vessel_slider.blockSignals(False)
            # 预设下拉切回「自动」
            self._mr_vessel_preset_combo.blockSignals(True)
            self._mr_vessel_preset_combo.setCurrentIndex(0)
            self._mr_vessel_preset_combo.blockSignals(False)
            self._pending_mr_vessel = value
            self._mr_vessel_label.setText(f"血管信号阈值: {value}")
            self._study_panel.set_iso_value(value)
        except Exception as auto_err:
            print("MR 血管阈值自动推荐失败: %s" % auto_err)

    def _on_largest_region_toggled(self, checked: bool):
        self._study_panel.set_keep_largest_region(checked)

    def _on_remove_bed_toggled(self, checked: bool):
        self._study_panel.set_remove_bed(checked)

    def _on_enhance_fractures_toggled(self, checked: bool):
        self._study_panel.set_enhance_fractures(bool(checked))

    def _on_low_power_toggled(self, checked: bool):
        # 实时生效，但不触发重新渲染，仅在下次滑动时体现
        pass

    def _on_mpr_changed(self, value: int):
        ratio = value / 1000.0
        self._mpr_label.setText(f"MPR切片同步: {int(ratio * 100)}%")
        self._study_panel.set_slice_ratio(ratio)

    def _on_window_preset_changed(self, index: int):
        """窗宽窗位预设改变"""
        ww_wl = self._window_preset_combo.currentData()
        if ww_wl and ww_wl[0] is not None and ww_wl[1] is not None:
            window, level = ww_wl
            
            # 内嵌遮罩代替系统进度框（loading_ui）
            with loading_ui.busy_scope(self._study_panel, "正在调整窗宽窗位...",
                                       sub="WW/WL · %d / %d" % (int(window),
                                                                int(level))):
                # 更新spinbox但不触发信号
                self._ww_spin.blockSignals(True)
                self._wl_spin.blockSignals(True)
                self._ww_spin.setValue(int(window))
                self._wl_spin.setValue(int(level))
                self._ww_spin.blockSignals(False)
                self._wl_spin.blockSignals(False)
                # 应用到视图
                self._study_panel.set_window_level(window, level)
                self._series_info_label.setText(f"窗宽/窗位: {window:.0f}/{level:.0f}")
                # 阈值不再在这里回写：两个滑块各自的 valueChanged 已维护
                # _pending_iso_value / _pending_mr_vessel。此处强行回写 CT 的值
                # 会在 MR 载入时污染三维阈值（渲染成头颅壳而非血管）。

    def _cycle_window_presets(self):
        """循环切换窗宽窗位预设"""
        count = self._window_preset_combo.count()
        if count <= 1:
            return
        curr = self._window_preset_combo.currentIndex()
        # 跳过第一个“自定义”选项（如果是自定义的话，或者直接跳到下一个）
        next_idx = (curr + 1) % count
        if next_idx == 0: # 假设 0 是自定义，循环到 1
            next_idx = 1
        self._window_preset_combo.setCurrentIndex(next_idx)

    def _on_custom_ww_wl_changed(self):
        """自定义窗宽窗位改变"""
        window = self._ww_spin.value()
        level = self._wl_spin.value()
        # 切换到自定义选项
        self._window_preset_combo.blockSignals(True)
        self._window_preset_combo.setCurrentIndex(0)
        self._window_preset_combo.blockSignals(False)
        # 应用到视图
        self._study_panel.set_window_level(window, level)
        self._series_info_label.setText(f"窗宽/窗位: {window:.0f}/{level:.0f}")
        # 同_2d 的窗值预设：不在此回写三维阈值，避免污染 MR 的血管阈值

    def _current_iso_value(self) -> int:
        """按当前序列的模态取应使用的三维阈值。

        CT 与 MR 的阈值语义完全不同（HU vs 信号强度），且各有一套滑块。
        初版这里只读 CT 的 ``_pending_iso_value``（恒为 200），于是
        载入 MR 后切换三维、或拖动 MR 滑块再松手，都会把阈值强行拉回
        200 —— 渲染出来是**整个头颅的软组织壳**而不是血管，
        必须手动选一次下拉框预设才会出正确结果。
        """
        if self._is_current_series_mr():
            return int(self._pending_mr_vessel)
        return int(self._pending_iso_value)

    def _is_current_series_mr(self) -> bool:
        return is_mr_modality(
            (getattr(self, "_current_series_data", None) or {}).get("modality"))

    def _apply_iso_value_once(self):
        self._study_panel.set_iso_value(self._current_iso_value())

    def _on_threshold_pressed(self):
        # 如果开启低功耗模式，按下时设置更大幅度的降采样
        if self._low_power_checkbox.isChecked():
            self._study_panel.set_shrink_factor(4)
        self._study_panel.set_preview_enabled(True)

    def _on_threshold_released(self):
        # 顺序很关键：先在预览（低分辨率）下应用新阈值（即时、廉价），
        # 再退出预览模式 —— 全分辨率重计算会自动转入后台线程，界面不冻结。
        # 两个滑块共用这两个槽，所以取值必须按当前模态分流
        # （MR 的信号强度阈值存放在 _pending_mr_vessel）。
        self._study_panel.set_shrink_factor(2)
        val = self._current_iso_value()
        self._study_panel.set_iso_value(val)          # 预览分辨率下立即生效
        self._study_panel.set_preview_enabled(False)  # 大数据自动转后台构建

    # ------------------------------------------------------------------
    # 主窗口文件拖放（拖 .dcm 文件 / DICOM 目录进来打开）
    # ------------------------------------------------------------------

    @staticmethod
    def _dicom_like_path(path: str) -> bool:
        """判断拖进来的路径是否能打开（用于拖入时的高亮反馈）

        现在也接受常见图片（PNG/JPG…），否则拖进来的图片会被静默忽略 —— 与
        「打开」按钮不一致。
        """
        if not path or not os.path.exists(path):
            return False
        if os.path.isdir(path):
            return count_dicom_files(path) > 0 or any(
                e.lower().endswith(".dcm") for e in os.listdir(path)[:50])
        if is_image_file(path):
            return True
        ext = os.path.splitext(path)[1].lower()
        # DICOM 常常没有扩展名，故后缀缺失也按DICOM 尝试
        return ext in ("", ".dcm", ".dicom", ".ima")

    def dragEnterEvent(self, event):
        try:
            md = event.mimeData()
            if md.hasUrls() and any(
                    self._dicom_like_path(u.toLocalFile())
                    for u in md.urls()):
                event.acceptProposedAction()
                return
        except Exception:       # noqa: BLE001
            pass
        event.ignore()

    def dragMoveEvent(self, event):
        try:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return
        except Exception:       # noqa: BLE001
            pass
        event.ignore()

    def dragLeaveEvent(self, event):
        event.accept()

    def dropEvent(self, event):
        try:
            md = event.mimeData()
            if not md.hasUrls():
                event.ignore()
                return
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if not paths:
                event.ignore()
                return
            event.acceptProposedAction()
            # 多个路径时逐个打开（最后一个会成为当前显示）
            for p in paths:
                if self._dicom_like_path(p):
                    self._open_path(p)
        except Exception as e:       # noqa: BLE001
            traceback.print_exc()
            QtWidgets.QMessageBox.warning(self, "打开失败", str(e))

    # 患者列表模态配色：CT 蓝 / DR 绿 / MR 紫 / 混合 橙。
    # 按皮肤分两套：深色底用「亮而柔和」的一组（对 #101216 对比度 7~10），
    # 浅色底（ivory / celadon 的列表底色是纯白）必须换成同色相的深色版，
    # 否则原来那组在原色底上对比度只有 1.8~2.5，等于看不清（v1.22.1 实测）。
    # 两套都保证：彼此色距 >= 100、对各自底色对比度 >= 4.5（正文可读线）。
    _MODALITY_COLORS = {
        "CT":   QtGui.QColor(88, 166, 255),    # 蓝
        "DR":   QtGui.QColor(90, 200, 140),    # 绿
        "XR":   QtGui.QColor(90, 200, 140),    #绿（XR 与 DR 同类）
        "MR":   QtGui.QColor(186, 140, 255),   # 紫
        "MIXED": QtGui.QColor(255, 176, 80),   # 橙
    }
    # 浅色皮肤：同一色相压暗到白底可读（#2476ce / #188649 / #812dff / #a6661d）
    _MODALITY_COLORS_LIGHT = {
        "CT":   QtGui.QColor(36, 118, 206),
        "DR":   QtGui.QColor(24, 134, 73),
        "XR":   QtGui.QColor(24, 134, 73),
        "MR":   QtGui.QColor(129, 45, 255),
        "MIXED": QtGui.QColor(166, 102, 29),
    }
    _MODALITY_FALLBACK_COLORS = {
        True:  QtGui.QColor(200, 204, 210),    # 深色底
        False: QtGui.QColor(107, 114, 128),    # 浅色底
    }

    @classmethod
    def _modality_palette(cls):
        """当前皮肤该用哪一套模态配色（浅色皮肤用压暗版）"""
        try:
            import theme as _th
            return (cls._MODALITY_COLORS if _th.is_dark()
                    else cls._MODALITY_COLORS_LIGHT)
        except Exception:            # noqa: BLE001
            return cls._MODALITY_COLORS

    @classmethod
    def _modality_color(cls, patient: dict, mods=None):
        """返回该患者行的模态配色

        优先看患者级 ``modality``（MIXED 走橙色）；缺失时按该患者
        实际含有的序列模态推断，单模态给对应色，多模态给混合色。
        """
        pal = cls._modality_palette()
        mods = mods if mods is not None else {
            str(s.get("modality") or "").upper()
            for s in (patient.get("series") or [])}
        key = str(patient.get("modality") or "").upper()
        if key in pal:
            return pal[key]
        has_ct = "CT" in mods
        has_mr = any(is_mr_modality(m) for m in mods)
        has_xr = any(is_xr_modality(m) for m in mods)
        n = sum([has_ct, has_mr, has_xr])
        if n == 0:
            try:
                import theme as _th
                return cls._MODALITY_FALLBACK_COLORS[bool(_th.is_dark())]
            except Exception:        # noqa: BLE001
                return cls._MODALITY_FALLBACK_COLORS[True]
        if n > 1:
            return pal["MIXED"]
        if has_mr:
            return pal["MR"]
        if has_xr:
            return pal["DR"]
        return pal["CT"]

    # ---------- 脊柱拼接 ----------
    def _on_spine_stitch_clicked(self):
        """工具区「脊柱拼接」：打开全脊柱拼接 + Cobb 角测量工作台。

        窗口是**非模态**的（`show()` 而非 `exec_()`）：拼接要跑 SIFT，用户
        可能想一边看 CT 一边拼脊柱，模态会把主界面锁死。

        异常一律弹框 —— 槽函数里 print 等于静默失败（用户只看到「点了没反应」）。
        """
        try:
            # 复用已打开的窗口：反复点按钮不该开一堆同样的窗口
            dlg = getattr(self, "_spine_stitch_dlg", None)
            if dlg is not None:
                try:
                    dlg.show()
                    dlg.raise_()
                    dlg.activateWindow()
                    return
                except RuntimeError:
                    # C++ 侧已被销毁（用户关过），重新建一个
                    self._spine_stitch_dlg = None

            import spine_stitch_dialog                  # noqa: PLC0415
            dlg = spine_stitch_dialog.SpineStitchDialog(self)
            dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
            dlg.destroyed.connect(self._on_spine_stitch_destroyed)
            self._spine_stitch_dlg = dlg
            dlg.show()
        except Exception as exc:                       # noqa: BLE001
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(
                self, "脊柱拼接",
                "打开脊柱拼接模块失败：\n%s\n\n"
                "请确认 spine/spine_app.py 与 opencv-python 均存在。" % exc)

    def _on_spine_stitch_destroyed(self, *args):
        """拼接窗口被关闭时清掉缓存引用，避免下次点到已销毁的 C++ 对象。"""
        self._spine_stitch_dlg = None

    def _on_open_dicom_clicked(self):
        """工具栏「打开影像文件」：选单个 DICOM / 常见图片，或序列目录

        过滤规则里同时给出 DICOM 与图片两栏：科室经常拿到的是从 PACS 导出的
        PNG/JPG，只给 .dcm 一栏会让人以为软件打不开图片。DICOM 允许无扩展名，
        所以还留着「所有文件」兜底。
        """
        img_pat = " ".join("*" + e for e in IMAGE_FILE_EXTS)
        path, _sel = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择影像文件（DICOM 或常见图片）", self._root_dir or "",
            "影像文件 (*.dcm *.dicom *.ima %s);;"
            "DICOM 文件 (*.dcm *.dicom *.ima);;"
            "常见图片 (%s);;所有文件 (*.*)" % (img_pat, img_pat))
        if not path:
            return
        self._open_path(path)

    def _open_path(self, path: str) -> bool:
        """打开一个影像路径：文件（单张影像/图片）或目录（序列）

        供「打开文件按钮」与「拖入文件」共用，保证两条路径行为一致。
        - 常见图片 → ImageFileReader（cv2 解码）
        - 其余文件 → 按 DICOM 读；DICOM 允许无扩展名，后缀缺失也按 DICOM 试
        """
        if not path or not os.path.exists(path):
            QtWidgets.QMessageBox.warning(
                self, "打开失败", "路径不存在：\n%s" % path)
            return False
        try:
            if os.path.isfile(path):
                if is_image_file(path):
                    return self._load_single_image_file(path)
                # _load_single_dicom_file 内部已弹提示，这里不再重复
                return self._load_single_dicom_file(path)
            if os.path.isdir(path):
                return self._open_dir(path)
            QtWidgets.QMessageBox.warning(
                self, "打开失败",
                "不支持的路径类型（既不是文件也不是目录）：\n%s" % path)
        except Exception as e:       # noqa: BLE001
            traceback.print_exc()
            QtWidgets.QMessageBox.warning(
                self, "打开失败", "无法打开该路径：\n%s\n\n%s" % (path, e))
        return False

    @staticmethod
    def _probe_single_dicom(path):
        """只读头判断该文件是不是 DICOM，返回 (info_dict, wc, ww)；不是则 None

        必须先用 pydicom 过一遍再交给 vtkDICOMImageReader：后者遇到非 DICOM
        文件不是抛异常而是**直接段错误**（实测 exit 139），所以不能拿它来试。
        Rows/Columns 是每张图像必备的元素，用它做最低限度的合法性校验。
        """
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
        except Exception:                  # noqa: BLE001
            return None
        if ds is None or not hasattr(ds, "Rows") or not hasattr(ds, "Columns"):
            return None
        info = {}
        modality = str(getattr(ds, "Modality", "") or "").strip().upper()
        if modality:
            info["modality"] = modality
        info["series_desc"] = _fix_gbk_text(
            getattr(ds, "SeriesDescription", "") or "")
        info["protocol_name"] = _fix_gbk_text(
            getattr(ds, "ProtocolName", "") or "")
        info["patient_name"] = _fix_gbk_text(
            getattr(ds, "PatientName", "") or "")
        info["patient_id"] = _fix_gbk_text(
            getattr(ds, "PatientID", "") or "")
        info["body_part"] = _fix_gbk_text(
            getattr(ds, "BodyPartExamined", "") or "")
        info["series_description"] = info["series_desc"]
        info["study_description"] = _fix_gbk_text(
            getattr(ds, "StudyDescription", "") or "")
        info["patient_sex"] = str(getattr(ds, "PatientSex", "") or "")
        wc = _dicom_first_number(getattr(ds, "WindowCenter", None))
        ww = _dicom_first_number(getattr(ds, "WindowWidth", None))
        return info, wc, ww

    def _load_single_dicom_file(self, path: str) -> bool:
        """读取单个文件并显示（工具栏「打开影像文件」/ 拖入单张都走这条）

        与「载入序列」保持同一套语义，否则会出现两类肉眼可见的假故障：
          · 窗值：X 线的 DICOM 窗值与 CT 的 HU 量纲完全不同，套 CT 的 400/40
            兜底会让整幅发白 —— 看起来就像「新文件没替换掉旧图」；
          · 工具区：一律按 CT 处理就不会显示 X 线 AI 辅助诊断分组。
        另外这里把**精确文件路径**写进序列信息（file_path），AI 检测才不会再从
        目录里随便挑一张 —— 一个目录里常躺着同一位患者的正位/侧位/多部位多张。
        """
        probed = self._probe_single_dicom(path)
        if probed is None:
            # 不是 DICOM：可能只是后缀写错（.dcm 的 PNG）。按图片再试一次，
            # 真的读不出来才报错 —— 用户可触发的失败路径必须弹窗，不能只 print。
            if ImageFileReader._decode(path) is not None:
                return self._load_single_image_file(path)
            QtWidgets.QMessageBox.warning(
                self, "打开失败",
                "这个文件既不是 DICOM 影像，也不是能识别的图片：\n%s\n\n"
                "支持的图片格式：%s\n"
                "若这是设备私有格式，可尝试用「加载患者目录」选中它所在的目录。"
                % (os.path.basename(path), "、".join(IMAGE_FILE_EXTS)))
            return False

        info, wc, ww = probed
        from vtkmodules.vtkIOImage import vtkDICOMImageReader
        try:
            modality = info.get("modality")
            is_xr = is_xr_modality(modality)
            is_mr = is_mr_modality(modality)
            folder = os.path.dirname(path)

            # X 线走 pydicom 装配（部分国产 DR 不写 Transfer Syntax UID，
            # vtkDICOMImageReader 会解析错位读成 64x64 空图），其余仍用 VTK。
            if is_xr:
                reader = XraySeriesReader()
                reader.SetFiles([path])
            else:
                reader = vtkDICOMImageReader()
                reader.SetFileName(path)
            reader.Update()
            out = reader.GetOutput()
            if out is None:
                raise RuntimeError("读取器没有输出，文件可能已损坏")
            ext = out.GetExtent()
            if (ext[1] - ext[0] + 1) <= 1 and (ext[3] - ext[2] + 1) <= 1:
                raise RuntimeError("图像尺寸异常（%d×%d）" % (
                    ext[1] - ext[0] + 1, ext[3] - ext[2] + 1))
            if out.GetNumberOfPoints() < 2:
                raise RuntimeError("像素数据为空")

            # ---- 窗宽窗位：与 _load_series 完全同源，按模态分流 ----
            if is_xr:
                mono1 = series_needs_invert(reader, modality)
                wl = self._xr_window_level(reader, invert=mono1)
                if wl:
                    ww_, wl_ = wl
                elif mono1:
                    ww_, wl_ = self._xr_default_window_level()
                elif self._plausible_window(ww, wc):
                    ww_, wl_ = float(ww), float(wc)
                else:
                    ww_, wl_ = self._xr_default_window_level()
            elif is_mr:
                # MR 走独立窗值策略（信号强度量纲，CT 校验会误拒 ADC 窗值）
                _kind = mr_series_kind(
                    info.get("series_desc", ""), info.get("protocol_name", ""),
                    info.get("body_part", ""))
                p1, p99 = mr_percentile_range(
                    folder,
                    [os.path.splitext(os.path.basename(path))[0]])
                ww_, wl_ = mr_window_level(wc, ww, _kind, p1, p99)
                self._current_mr_kind = _kind
            elif self._plausible_window(ww, wc):
                ww_, wl_ = float(ww), float(wc)
            else:
                ww_, wl_ = self._fallback_window_level(False)

            # ---- 序列信息：带上精确文件路径，供 AI 定位与「当前片子」显示 ----
            stem = os.path.splitext(os.path.basename(path))[0]
            info["file_path"] = path
            info["folder"] = folder
            info["file_uids"] = [stem]
            if not info.get("series_desc"):
                info["series_desc"] = os.path.basename(path)

            self._current_reader = reader
            self._current_series_folder = folder
            self._current_series_data = info
            # 索引里若没有这一条路径，_current_xray_path() 会退回扫目录
            return self._install_single_frame(
                reader, info, ww_, wl_, is_xr, is_mr, path)
        except Exception as e:       # noqa: BLE001
            print("读取单张 DICOM 失败 %s: %s" % (path, e))
            QtWidgets.QMessageBox.warning(
                self, "打开失败",
                "无法读取该 DICOM 文件：\n%s\n\n错误：%s\n\n"
                "若这是无扩展名的 DICOM 或设备私有格式，"
                "可尝试用「加载患者目录」选中它所在的目录。"
                % (os.path.basename(path), e))
            return False

    def _load_single_image_file(self, path: str) -> bool:
        """打开一张常见图片（PNG/JPG/BMP/TIFF/WebP）并显示。

        按「X 线单帧影像」处理：图片没有 DICOM 头，没有 HU 可测，也堆不出体
        数据。这样 AI 辅助诊断分组会照常显示，可直接拿导出的片子跑检测
        （xray 侧的读图入口本来就支持 JPG/PNG）。
        """
        reader = ImageFileReader()
        reader.SetFileName(path)
        try:
            reader.Update()
            out = reader.GetOutput()
            if out is None or out.GetNumberOfPoints() < 2:
                QtWidgets.QMessageBox.warning(
                    self, "打开失败",
                    "无法解码这张图片：\n%s\n\n支持的格式：%s"
                    % (os.path.basename(path), "、".join(IMAGE_FILE_EXTS)))
                return False

            # 图片没有 DICOM 窗值可依，按像素分布推算显示区间
            ww_, wl_ = self._image_window_level(reader)

            info = {
                "modality": "",                     # 图片没有模态，按 X 线单帧处理
                "series_desc": os.path.basename(path),
                "series_description": os.path.basename(path),
                "protocol_name": "",
                "patient_name": "",
                "patient_id": "",
                "body_part": "",
                "file_path": path,
                "folder": os.path.dirname(path),
                "file_uids": [os.path.splitext(os.path.basename(path))[0]],
                "image_file": True,
            }
            self._current_reader = reader
            self._current_series_folder = os.path.dirname(path)
            self._current_series_data = info
            return self._install_single_frame(
                reader, info, ww_, wl_, True, False, path)
        except Exception as e:       # noqa: BLE001
            traceback.print_exc()
            print("读取图片失败 %s: %s" % (path, e))
            QtWidgets.QMessageBox.warning(
                self, "打开失败",
                "无法读取这张图片：\n%s\n\n错误：%s"
                % (os.path.basename(path), e))
            return False

    def _image_window_level(self, reader):
        """常见图片的窗宽窗位：无 DICOM 窗值可依，按像素分布推算。

        单张图片与图片序列（ImageSeriesReader）共用这一套，避免两处逻辑跑偏
        出现「同一张图从文件入口看正常、从目录入口一片黑」。
        """
        try:
            if reader is not None and reader.is_color():
                return 255.0, 127.5
        except Exception:                  # noqa: BLE001
            pass
        wl = self._xr_window_level(reader)
        if wl:
            try:
                return float(wl[0]), float(wl[1])
            except Exception:              # noqa: BLE001
                pass
        try:
            out = reader.GetOutput()
            arr = out.GetPointData().GetScalars()
            from vtkmodules.util.numpy_support import vtk_to_numpy
            a = vtk_to_numpy(arr)
            lo, hi = float(a.min()), float(a.max())
        except Exception:                  # noqa: BLE001
            return 255.0, 127.5
        if hi - lo < 1.0:
            return 255.0, 127.5
        return float(hi - lo), float((hi + lo) / 2.0)

    def _install_single_frame(self, reader, info, ww, wl, is_xr, is_mr,
                              path) -> bool:
        """把「单帧影像」装进阅读区：清窗 → 切工具区 → 显示。

        单文件与序列走的是同一套收尾逻辑，差别只在数据源。**必须先清目标窗**
        （clear_all(view_index)）：只调 set_reader 的话，上一张片子留下的 AI
        标注层会继续盖在画面上，看起来就是「换了文件但图没变」。
        """
        target = self._study_panel.active_index
        self._study_panel.clear_all(target)
        if target == 0:
            self._volume_built = False
            self._last_segmentation_mask = None
            if hasattr(self, "_ab_seg_button"):
                self._ab_seg_button.setEnabled(False)

        self._apply_modality_ui(bool(is_xr), bool(is_mr))
        if is_xr:
            self._refresh_feedback_count()
        # 单帧影像堆不出体数据（X 线/图片本就单帧，MR 单张 MIP 同理）
        self._study_panel.set_3d_available(not (is_xr or is_mr))
        self._study_panel.set_is_mr(bool(is_mr))
        if not is_mr:
            self._study_panel.set_iso_value(int(self._threshold_slider.value()))
            self._threshold_label.setText(
                "骨组织阈值: %d" % int(self._threshold_slider.value()))
        self._study_panel.set_mode(StudyViewPanel.MODE_2D)
        self._study_panel.set_data(
            reader, window_width=ww, window_center=wl,
            dicom_data=info, slice_positions=None, view_index=target)
        # X 线没有 HU，MR 是信号强度，两者做「CT 值」测量都会误导
        self._study_panel.set_ct_measure_enabled(not (is_xr or is_mr))
        self._series_info_label.setText("窗宽/窗位: %.0f/%.0f" % (ww, wl))
        self._path_label.setText("已加载文件: %s" % os.path.basename(path))

        if is_xr and hasattr(self, "_xray_hint_label"):
            # 与部位提示合并而不是覆盖：那句「当前部位：…」是用户判断按钮
            # 为什么置灰的唯一依据（v1.9.1）
            desc = str(info.get("series_desc") or "").strip()
            self._xray_hint_label.setText(
                "%s\n当前片子：%s" % (self._xray_part_hint(), desc))
        gc.collect()
        return True

    def _on_load_clicked(self):
        """「加载患者目录」：选到序列目录就直接打开，选到根目录就刷新列表

        分支判据必须是 `is_single_series_dir`（序列结构）而不是
        `is_dicom_dir`（只要有 dcm 就算）—— 患者数据根目录里也常有
        落单dcm（实测 D:\\ClearTmp\\DicomTmp 直属就有 1 张），
        用后者会把选到根目录的当单序列去读，表现为「按钮点了没反应」。
        """
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择患者目录（或直接选择某个序列目录）", self._root_dir)
        if not folder:
            return
        self._open_dir(folder)

    def _open_dir(self, folder: str) -> bool:
        """「加载患者目录」与「打开影像文件→目录」共用的目录分支决策

        原来是两份一模一样的 if/is_single_series_dir 拷贝（v1.9.4 加
        `_open_path` 时复制了一份），PACS 导出目录的四条分支因此各自为政，
        任一处漏传 series_data 就表现为「点了没反应」。这里收口成一份。

        分支顺序（前面能成立就不再往后走）：
          1. 单序列 DICOM 目录 → 直接打开（_load_series 会自行补 modality）；
          2. 纯图片目录 → 整目录当图片序列打开；
          3. 图片躺在子目录里 → 问一句再打开最大的那个（否则一片空白）；
          4. 否则 → 当作患者数据根目录，重建序列树。
        """
        try:
            if is_single_series_dir(folder):
                self._load_series(folder)
                return True
            if is_image_only_dir(folder):
                self._load_series(folder, kind="image")
                return True

            subs = image_subdirs(folder)
            if len(subs) == 1:
                # 只有一个图片子目录：直接打开，省一次双击
                self._load_series(subs[0][2], kind="image")
                return True
            if subs:
                # 多个图片子目录（v1.9.7 曾弹窗「加载张数最多的那个」）：
                # 用户要的是**全部可见、按文件夹分组**，弹窗替他选等于丢数据。
                # 这里直接落到患者列表，每个文件夹一条，点哪条加载哪条。
                pass

            self._root_dir = folder
            self._load_patient_tree(folder)
            return True
        except Exception as exc:           # noqa: BLE001
            traceback.print_exc()
            # except 里的 print 对用户等于静默失败，这里必须弹窗
            QtWidgets.QMessageBox.warning(
                self, "打开失败",
                "无法打开该目录：\n%s\n\n%s" % (folder, exc))
        return False

    def _on_reset_view(self):
        self._study_panel.reset_view()

    def _on_close_current(self):
        self._study_panel.clear_all()
        self._series_info_label.setText("窗宽/窗位: -")
        self._threshold_label.setText("骨组织阈值: -")
        if hasattr(self, "_threshold_preset_combo"):
            self._threshold_preset_combo.setCurrentIndex(0)
        if hasattr(self, "_mr_vessel_label"):
            self._mr_vessel_label.setText("血管信号阈值: -")
            self._mr_vessel_preset_combo.setCurrentIndex(0)
        self._path_label.setText("未加载DICOM目录")
        self._current_reader = None
        self._current_series_folder = None
        self._current_series_data = None
        self._last_segmentation_mask = None
        # 多窗：关闭时清掉**所有**窗口的暂存目录
        for _t in self._all_temp_folders():
            try:
                self._cleanup_temp_folder(_t)
            except Exception:
                pass
        self._view_temp_folders = {}
        self._last_temp_folder = None
        self._xray_clear_result()
        # 关闭图像后回到「未知模态」：收起 AI 分组、恢复三维可用
        self._study_panel.set_3d_available(True)
        self._study_panel.set_ct_measure_enabled(True)
        self._study_panel.set_is_mr(False)
        self._apply_modality_ui(False)
        # 立即释放已关闭序列占用的体数据内存
        gc.collect()

    # ------------------------------------------------------------------
    # X 线 AI 辅助诊断
    # ------------------------------------------------------------------

    def _build_xray_ai_panel(self, right_layout):
        """构建右侧「X 线 AI 辅助诊断」分组（默认隐藏，载入 X 线后显示）"""
        try:
            import xray_bridge
            self._xray_bridge = xray_bridge
            self._xray_ai = xray_bridge.XrayAI()
        except Exception as exc:      # noqa: BLE001
            print("X 线 AI 子系统加载失败: %s" % exc)
            self._xray_bridge = None
            self._xray_ai = None
        self._xray_worker = None
        self._xray_result = None
        # 当前结果是否带「可切换的无标注底图」（骨龄），控制关节标注开关是否放开
        self._xray_anno_ready = False
        # 手动微调：模型给出的点位 + 微调开关状态。
        # kind 记录这批点属于哪个测量（"foot" 足弓 / "spine" 脊柱侧弯），
        # 拖动后的读数口径跟读数对象都按它分派。
        self._xray_kp_points = None    # {idx: (x, y)} 模型原始点位
        self._xray_kp_kind = None      # 这批点位所属功能：foot_arch / spine
        self._xray_kp_angles = None    # 足弓 (内侧, 外侧) 度 / 脊柱 (Cobb, None)
        self._xray_kp_ai_image = None  # 模型原标注图（退出微调时还原用）
        self._xray_kp_moved = False
        self._xray_kp_editing = False
        if hasattr(self, "_study_panel"):
            try:
                self._study_panel.keypoints_changed.connect(
                    self._on_xray_keypoint_moved)
            except Exception:        # noqa: BLE001
                pass
        # 当前片子的检查部位（XRAY_PART_*）与「忽略部位约束」开关
        self._xray_part_key = XRAY_PART_UNKNOWN
        self._xray_part_ignore = False
        if self._xray_bridge is not None:
            try:
                self._xray_part_ignore = bool(
                    self._xray_bridge.load_detection_settings().get(
                        "ignore_part_scope", False))
            except Exception:        # noqa: BLE001
                self._xray_part_ignore = False
        self._xray_store = (xray_bridge.DetectionRecordStore()
                            if self._xray_bridge is not None else None)
        self._xray_feedback_store = (xray_bridge.FeedbackStore()
                                     if self._xray_bridge is not None else None)
        self._xray_feedback_result = None

        group = QtWidgets.QGroupBox("X线 AI 辅助诊断")

        g_layout = QtWidgets.QVBoxLayout(group)
        g_layout.setContentsMargins(6, 10, 6, 6)
        g_layout.setSpacing(4)

        self._xray_hint_label = QtWidgets.QLabel("请先载入 X 线影像")
        theme.role(self._xray_hint_label, "muted")
        self._xray_hint_label.setWordWrap(True)
        g_layout.addWidget(self._xray_hint_label)

        # 五个 AI 功能按钮，沿用 xray 原系统的语义色，便于老用户迁移
        self._xray_buttons = {}
        # 第 5 项是各功能适用的部位（与 ct3d_utils.XRAY_AI_PART_SCOPE 同源，
        # 这里只用来生成提示文案；真正的过滤在 _xray_allowed_kinds() 里）
        specs = [
            ("chest",    "胸片检测",   "#2a82da", "胸部 X 线：14 类病变自动识别与定位",
             "仅适用于胸部片"),
            ("fracture", "骨折检测",   "#d9534f", "四肢 / 关节 / 脊柱 / 足部 X 线：骨折自动检测与定位",
             "适用于四肢、关节、脊柱、足部片（不含胸部）"),
            ("spine",    "脊柱侧弯",   "#7b68ee", "全脊柱正位：椎体识别 + Cobb 角测量",
             "仅适用于脊柱片"),
            ("boneage",  "骨龄评估",   "#e08b28", "左手腕片：RUS-CHN（中华 05）骨龄",
             "仅适用于左手腕片"),
            ("foot_arch", "足弓测量",  "#2e9e5b", "足部 X 线：足弓关键点与角度测量",
             "仅适用于足部片"),
        ]
        self._xray_button_tips = {}
        # 功能 key → 中文名（提示文案与检测记录共用一套命名）
        meta = getattr(self._xray_bridge, "KIND_META", {}) or {}
        self._xray_kind_names = {
            k: (meta.get(k, (k, ""))[0] or k)
            for k in ("chest", "fracture", "spine", "boneage", "foot_arch")}
        for key, text, color, tip, scope_tip in specs:
            btn = QtWidgets.QPushButton(text)
            full_tip = "%s\n适用部位：%s" % (tip, scope_tip)
            btn.setToolTip(full_tip)
            self._xray_button_tips[key] = full_tip
            theme.bind(btn, lambda c=color: theme.qss_semantic_button(c))
            btn.setEnabled(False)
            btn.clicked.connect(
                lambda _c=False, k=key: self._on_xray_detect(k))
            g_layout.addWidget(btn)
            self._xray_buttons[key] = btn

        auto_btn = QtWidgets.QPushButton("智能识别并检测")
        auto_btn.setToolTip("按 DICOM 的检查部位自动选择最合适的检测功能")
        auto_btn.setEnabled(False)
        auto_btn.clicked.connect(self._on_xray_auto_detect)
        g_layout.addWidget(auto_btn)
        self._xray_auto_btn = auto_btn

        clear_ai_btn = QtWidgets.QPushButton("清除 AI 标注")
        clear_ai_btn.setEnabled(False)
        clear_ai_btn.clicked.connect(self._on_xray_clear_annotation)
        g_layout.addWidget(clear_ai_btn)
        self._xray_clear_btn = clear_ai_btn

        # 标注图层（骨龄专用，其他检测的标注图没有对应底图故置灰）
        # 骨龄的 13 关节框和中文标签是**烘在位图里**的，看着「名字配错骨头」
        # 时最需要的就是一键切回无标注底图逐节比对，所以给个常驻开关。
        # 两个按钮并成一行，避免再往侧栏里叠高度。
        anno_row = QtWidgets.QHBoxLayout()
        anno_row.setContentsMargins(0, 0, 0, 0)
        self._xray_anno_btn = QtWidgets.QPushButton("关节标注")
        self._xray_anno_btn.setToolTip(
            "显示 / 隐藏骨龄的 13 个关节标注\n"
            "取消勾选即切回无标注底图，方便核对每个框落在哪一节骨头")
        self._xray_anno_btn.setCheckable(True)
        self._xray_anno_btn.setChecked(True)
        self._xray_anno_btn.setEnabled(False)
        self._xray_anno_btn.clicked.connect(self._on_xray_toggle_annotation)
        anno_row.addWidget(self._xray_anno_btn)
        self._xray_save_btn = QtWidgets.QPushButton("保存标注图")
        self._xray_save_btn.setToolTip(
            "把带有关节标注的整张原分辨率图片另存为 PNG / JPG\n"
            "（直接存模型绘制的位图，比屏幕截图清晰）")
        self._xray_save_btn.setEnabled(False)
        self._xray_save_btn.clicked.connect(self._on_xray_save_annotation)
        anno_row.addWidget(self._xray_save_btn)
        g_layout.addLayout(anno_row)

        # 手动微调：模型给出的点位常有 3~5 像素偏差，重训模型要大量标注
        # 数据，所以留「拖手柄微调」的口子 —— 足弓拖 P1~P5 重算足弓角，
        # 脊柱侧弯拖上下终板线四个端点重算 Cobb 角。默认禁用，跑完检测
        # 拿到点位后才放开。
        kp_btn = QtWidgets.QPushButton("微调关键点")
        kp_btn.setToolTip(
            "拖动图上关键点微调位置（即时重算角度）\n"
            "左键按住关键点即可拖动；拖出的点会标「原」提示模型原始位置")
        kp_btn.setEnabled(False)
        kp_btn.setCheckable(True)
        kp_btn.clicked.connect(self._on_xray_toggle_kp_adjust)
        g_layout.addWidget(kp_btn)
        self._xray_kp_btn = kp_btn

        kp_reset_btn = QtWidgets.QPushButton("复位关键点")
        kp_reset_btn.setToolTip("把 P1~P5 恢复成模型原始提取的位置")
        kp_reset_btn.setEnabled(False)
        kp_reset_btn.clicked.connect(self._on_xray_reset_kp)
        g_layout.addWidget(kp_reset_btn)
        self._xray_kp_reset_btn = kp_reset_btn

        self._xray_kp_tip = QtWidgets.QLabel("")
        self._xray_kp_tip.setWordWrap(True)

        self._xray_kp_tip.setVisible(False)
        g_layout.addWidget(self._xray_kp_tip)

        # 检测设置 / 检测记录：与检测按钮同属 X 线工作流，放在同一分组里
        tools_row = QtWidgets.QHBoxLayout()
        tools_row.setContentsMargins(0, 0, 0, 0)
        self._xray_settings_btn = QtWidgets.QPushButton("检测设置")
        self._xray_settings_btn.setToolTip("调整各项检测的置信度阈值")
        self._xray_settings_btn.clicked.connect(self._on_xray_settings)
        self._xray_records_btn = QtWidgets.QPushButton("检测记录")
        self._xray_records_btn.setToolTip("查看 / 导出历史检测记录")
        self._xray_records_btn.clicked.connect(self._on_xray_records)
        tools_row.addWidget(self._xray_settings_btn)
        tools_row.addWidget(self._xray_records_btn)
        g_layout.addLayout(tools_row)

        # 漏检反馈：模型没识别出来时，把片子登记下来（图落盘 + 记数据库），
        # 作为后续优化/重训的素材。放在检测记录旁边，同属 X 线工作流。
        self._xray_feedback_btn = QtWidgets.QPushButton("漏检反馈")
        self._xray_feedback_btn.setToolTip(
            "模型漏检或结果不对？点此把当前片子登记为反馈素材\n"
            "（图片存到软件目录 feedback_images，记录写入数据库）")
        self._xray_feedback_btn.setEnabled(False)
        self._xray_feedback_btn.clicked.connect(self._on_xray_feedback)
        g_layout.addWidget(self._xray_feedback_btn)

        fb_row = QtWidgets.QHBoxLayout()
        fb_row.setContentsMargins(0, 0, 0, 0)
        self._xray_feedback_list_btn = QtWidgets.QPushButton("反馈列表")
        self._xray_feedback_list_btn.setToolTip("查看 / 导出已登记的漏检反馈")
        self._xray_feedback_list_btn.clicked.connect(self._on_xray_feedback_list)
        self._xray_feedback_list_btn.setEnabled(False)
        self._xray_feedback_count_label = QtWidgets.QLabel("")
        theme.role(self._xray_feedback_count_label, "caption")
        fb_row.addWidget(self._xray_feedback_list_btn)
        fb_row.addWidget(self._xray_feedback_count_label, 1)
        g_layout.addLayout(fb_row)

        self._xray_summary_label = QtWidgets.QLabel("")
        self._xray_summary_label.setWordWrap(True)
        theme.role(self._xray_summary_label, "card")
        self._xray_summary_label.setVisible(False)
        g_layout.addWidget(self._xray_summary_label)

        self._xray_list = QtWidgets.QListWidget()
        self._xray_list.setMaximumHeight(150)
        self._xray_list.setVisible(False)
        g_layout.addWidget(self._xray_list)

        # 复制结果：把当前辅助诊断结果（摘要 + 逐条病变）复制为纯文本
        self._xray_copy_btn = QtWidgets.QPushButton("复制结果")
        self._xray_copy_btn.setToolTip(
            "把当前辅助诊断结果复制到剪贴板（纯文本，可粘贴到报告）")
        self._xray_copy_btn.setEnabled(False)
        self._xray_copy_btn.clicked.connect(self._on_xray_copy_result)
        g_layout.addWidget(self._xray_copy_btn)

        # 忙碌指示：小号 spinner + 文字（原来是系统 QProgressBar 的跑马条）
        # 变量名沿用 _xray_progress，setVisible 的调用点不用改
        self._xray_progress = loading_ui.SpinnerRow("AI 推理中，请稍候...", size=20)
        self._xray_progress.setVisible(False)
        g_layout.addWidget(self._xray_progress)

        group.setVisible(False)
        right_layout.addWidget(group)
        self._xray_group = group
        # 计数标签先给个占位，真正载入 X 线时才会刷新为真实条数
        self._xray_feedback_count_label.setText("尚无反馈记录")

    def _xray_kp_set_buttons(self, can_edit, editing=False, moved=False,
                             kind=None):
        """同步微调按钮的可用/勾选态与提示文案（各控件可能尚未建好）"""
        if getattr(self, "_xray_kp_btn", None) is None:
            return
        self._xray_kp_btn.setEnabled(bool(can_edit))
        self._xray_kp_reset_btn.setEnabled(bool(editing))
        if getattr(self, "_xray_kp_btn", None) is not None:
            if self._xray_kp_btn.isChecked() != bool(editing):
                self._xray_kp_btn.blockSignals(True)
                self._xray_kp_btn.setChecked(bool(editing))
                self._xray_kp_btn.blockSignals(False)
        tip = getattr(self, "_xray_kp_tip", None)
        if tip is not None:
            if not can_edit:
                tip.setVisible(False)
            else:
                # 文案按当前测量类型分岔：足弓是 P1~P5 五个点、读内外侧角，
                # 脊柱是上下终板线四个端点、读 Cobb 角。
                is_spine = (kind or getattr(self, "_xray_kp_kind", None)) == "spine"
                subject = ("拖动 P1~P4 四个端点微调上下终板线，Cobb 角实时更新"
                           if is_spine else
                           "拖动 P1~P5 微调位置，角度实时更新")
                tail = ("（已手动微调，点「复位关键点」还原）" if moved
                        else "；微调时显示原始影像，「原」为模型原始位置")
                tip.setText("%s%s" % (subject, tail))
                tip.setVisible(True)

    def _on_xray_toggle_kp_adjust(self, checked=None):
        """开关「微调关键点」模式（足弓与脊柱侧弯共用）

        进模式时底图切回原始影像：AI 标注图里的圆点/线是**烘死在位图里**的，
        位置又跟拖动手柄完全重合 —— 不切的话用户看不出自己抓到的是哪个，
        拖完也看不出变化。只在 overlay 层（Layer 1）叠矢量手柄，可抓可拖，
        拖完即时重算角度。
        """

        on = True if checked is None else bool(checked)
        if on and not self._xray_kp_points:
            return
        self._xray_kp_editing = on
        # 多窗下必须落在**活动窗口**上：否则在 4 窗里微调的是看不见的那个窗
        reader = self._study_panel.active_reader
        if on:
            # 底图切回原始影像：AI 标注图里那些圆点是**烘死在位图里**的，
            # 位置又跟拖动的手柄完全重合 —— 不切的话用户根本看不出自己抓到
            # 的是哪个，拖完也看不出变化。原始影像 + overlay 上的矢量手柄，
            # 一进微调态就只有手柄在动，抓哪个、动到哪儿一目了然。
            # （手柄坐标按 _ai_actor 的 spacing 换算，标注图与底图通常等大，
            # 万一被缩放过，本函数退出时会把标注图还原回去，读数依旧可信。）
            reader.set_ai_overlay_image(None)
            reader.show_keypoints(True)
        else:
            reader.show_keypoints(False)
            # 退出微调：把 AI 标注图还回去
            res_img = (self._xray_result or {}).get("image")
            if res_img is None:
                res_img = self._xray_kp_ai_image
            try:
                reader.set_ai_overlay_image(res_img)
            except Exception:            # noqa: BLE001
                reader.set_ai_overlay_image(None)
        self._xray_kp_set_buttons(
            can_edit=bool(self._xray_kp_points),
            editing=on, moved=bool(self._xray_kp_moved),
            kind=getattr(self, "_xray_kp_kind", None))

    def _on_xray_reset_kp(self):
        """复位到模型原始关键点"""
        try:
            self._study_panel.reset_keypoints()
        except Exception as exc:      # noqa: BLE001
            print("复位关键点失败: %s" % exc)
            return
        # 面板那层代理是 emit=False（怕信号回环），所以复位不会走
        # keypoints_changed —— 主窗这边必须自己把「已手动微调」标记摘掉并
        # 重取一次读数，否则复位完了摘要上还挂着「已手动微调」。
        try:
            reader = self._study_panel.active_reader
            ang = reader.keypoint_angles()
            self._xray_kp_angles = tuple(ang or (None, None))
        except Exception:        # noqa: BLE001
            pass
        self._xray_kp_moved = False
        self._sync_xray_kp_labels()

    def _on_xray_keypoint_moved(self, payload):
        """关键点被拖动：按测量类型把新读数写回结果摘要与指标"""
        if not isinstance(payload, dict):
            return
        angles = payload.get("angles") or (None, None)
        self._xray_kp_angles = tuple(angles) if isinstance(angles, (tuple, list)) else None
        self._xray_kp_moved = bool(payload.get("edited"))
        res = self._xray_result
        if isinstance(res, dict):
            res["keypoints"] = dict(payload.get("points") or {})
            if getattr(self, "_xray_kp_kind", None) == "spine":
                # 脊柱：读数就是两条终板线的夹角，直接覆盖最初那个 Cobb 角
                cobb = (self._xray_kp_angles or (None, None))[0]
                res["metrics"] = {
                    "Cobb角": ("%.1f°" % cobb) if cobb is not None else "—",
                    "椎体数": (res.get("metrics") or {}).get("椎体数", 0),
                }
                try:
                    res["summary"] = "Cobb 角 %.1f°（手动调整）" % cobb
                except TypeError:
                    pass
            else:
                med, lat = (self._xray_kp_angles or (None, None))
                res["metrics"] = {
                    "内侧足弓角": ("%.1f°" % med) if med is not None else "—",
                    "外侧足弓角": ("%.1f°" % lat) if lat is not None else "—",
                }
        self._sync_xray_kp_labels()

    def _sync_xray_kp_labels(self):
        """把当前角度/微调状态刷进摘要行与结果列表。

        摘要行是富文本，构造时必须走 html.escape —— 角度是数字本来安全，
        但摘要里可能混着片子名等任意文本，直接拼会打断标签结构。
        """
        import html as _html
        res = self._xray_result or {}
        med, lat = (self._xray_kp_angles or (None, None))
        if med is None and lat is None and not res.get("metrics"):
            return
        parts = []
        summary = str(res.get("summary") or "")
        if summary:
            parts.append("<b>%s</b>" % _html.escape(summary))
        metrics = res.get("metrics") or {}
        if metrics:
            parts.append(" · ".join(
                "%s %s" % (_html.escape(str(k)), _html.escape(str(v)))
                for k, v in metrics.items()))
        if self._xray_kp_moved:
            parts.append('<span style="color:#5ac88c;">已手动微调</span>')
        elif self._xray_kp_editing:
            is_spine = getattr(self, "_xray_kp_kind", None) == "spine"
            parts.append('<span style="color:#8a9096;">拖动 %s 调整</span>'
                         % ("P1~P4 微调终板线" if is_spine else "P1~P5 调整"))
        text = "<br>".join(parts)
        lab = getattr(self, "_xray_summary_label", None)
        if lab is not None:
            lab.setText(text)
            lab.setVisible(bool(text))
        self._xray_kp_set_buttons(
            can_edit=bool(self._xray_kp_points),
            editing=bool(self._xray_kp_editing),
            moved=bool(self._xray_kp_moved),
            kind=getattr(self, "_xray_kp_kind", None))

    def _xray_clear_result(self):
        """清空 AI 结果区并恢复原图显示"""
        if hasattr(self, "_xray_list"):
            self._xray_list.clear()
            self._xray_list.setVisible(False)
            self._xray_summary_label.setText("")
            self._xray_summary_label.setVisible(False)
            self._xray_clear_btn.setEnabled(False)
            try:
                self._xray_last_result_text = ""
                self._xray_copy_btn.setEnabled(False)
            except Exception:
                pass
        self._xray_result = None
        self._xray_kp_points = None
        self._xray_kp_kind = None
        self._xray_kp_angles = None
        self._xray_kp_ai_image = None
        self._xray_kp_moved = False
        self._xray_kp_editing = False
        self._xray_anno_ready = False
        for attr in ("_xray_anno_btn", "_xray_save_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.setEnabled(False)
        if getattr(self, "_xray_anno_btn", None) is not None:
            self._xray_anno_btn.blockSignals(True)
            self._xray_anno_btn.setChecked(True)
            self._xray_anno_btn.blockSignals(False)
        self._xray_kp_set_buttons(can_edit=False)
        try:
            self._study_panel.reader_2d.set_ai_overlay_image(None)
            self._study_panel.clear_keypoints()
        except Exception:
            pass

    def _xray_allowed_kinds(self):
        """当前片子部位下可用的 AI 功能 key 集合"""
        return xray_ai_kinds_for_part(getattr(self, "_xray_part_key",
                                              XRAY_PART_UNKNOWN),
                                      getattr(self, "_xray_part_ignore", False))



    def _xray_refresh_part(self):
        """判定当前 X 线片子的检查部位，并按部位放开可用的 AI 功能。

        BodyPartExamined 在国产设备上经常是空的或写成乱码，所以判据有三层：
        DICOM 头字段 → 列表里已有的部位/序列名 → xray 侧的粗分类。
        判不出部位时全部放开（不该因为设备没写字段就锁死功能），
        但提示文案会提醒用户自行判断片子是否合适。
        """
        self._xray_part_key = XRAY_PART_UNKNOWN
        path = self._current_xray_path()
        if path:
            self._xray_part_key = xray_part_key_from_dicom(path)
        if self._xray_part_key == XRAY_PART_UNKNOWN:
            data = getattr(self, "_current_series_data", None) or {}
            self._xray_part_key = xray_part_key(
                data.get("body_part"), data.get("series_description"),
                data.get("study_description"))
        if self._xray_part_key == XRAY_PART_UNKNOWN and \
                self._xray_ai is not None and path:
            # 最后兜底：xray 自己的粗分类（BodyPartDetector）
            try:
                self._xray_part_key = xray_coarse_to_part(
                    self._xray_ai.detect_body_part(path))
            except Exception:        # noqa: BLE001
                self._xray_part_key = XRAY_PART_UNKNOWN

    def _xray_part_hint(self):
        """按当前部位生成提示文案（列出哪些功能可用）"""
        key = getattr(self, "_xray_part_key", XRAY_PART_UNKNOWN)
        label = XRAY_PART_LABELS.get(key, key)
        if getattr(self, "_xray_part_ignore", False):
            return "当前部位：%s（已设为忽略部位限制）" % label
        allowed = self._xray_allowed_kinds()
        if key == XRAY_PART_UNKNOWN:
            return ("未能识别检查部位（设备未写 BodyPartExamined），"
                    "已放开全部检测功能，请自行确认片子是否合适。")
        if not allowed:
            return ("当前部位：%s —— 暂无适用的 AI 检测功能，"
                    "可在「检测设置」里勾选忽略部位限制。" % label)
        kind_names = getattr(self, "_xray_kind_names", {}) or {}
        names = []
        for k in ("chest", "fracture", "spine", "boneage", "foot_arch"):
            if k in allowed:
                names.append(kind_names.get(k, k))
        return "当前部位：%s，可用：%s" % (label, "、".join(names))

    def _set_xray_ui_enabled(self, enabled, hint=None):
        """X 线按钮的可用性开关

        ``enabled`` 是总闸（是否载入 X 线 / 是否正在检测），在此之上再按
        「部位是否适用」逐按钮过滤：不适用的按钮保持置灰，tooltip 写明原因。
        """
        if not hasattr(self, "_xray_buttons"):
            return
        allowed = self._xray_allowed_kinds()
        part_key = getattr(self, "_xray_part_key", XRAY_PART_UNKNOWN)
        for key, btn in self._xray_buttons.items():
            ok = bool(enabled) and key in allowed
            btn.setEnabled(ok)
            base_tip = (getattr(self, "_xray_button_tips", {}) or {}).get(key)
            if not ok and base_tip and part_key != XRAY_PART_UNKNOWN:
                btn.setToolTip("%s\n\n⚠ %s" % (
                    base_tip, xray_part_mismatch_text(
                        (getattr(self, "_xray_kind_names", {}) or {}).get(
                            key, key), part_key)))
            elif base_tip:
                btn.setToolTip(base_tip)
        self._xray_auto_btn.setEnabled(bool(enabled))
        # 漏检反馈对「任何一次检测结果」都成立，不只在检测成功后
        self._xray_feedback_btn.setEnabled(bool(enabled))
        if hint is not None:
            self._xray_hint_label.setText(hint)

    def _refresh_feedback_count(self):
        """刷新反馈按钮旁的计数（登记一条后要 +1）"""
        if not hasattr(self, "_xray_feedback_count_label"):
            return
        store = getattr(self, "_xray_feedback_store", None)
        if store is None:
            return
        try:
            n = store.count()
        except Exception:       # noqa: BLE001
            n = 0
        self._xray_feedback_count_label.setText(
            "已登记 %d 条" % n if n else "尚无反馈记录")
        self._xray_feedback_list_btn.setEnabled(True)

    def _on_xray_clear_annotation(self):
        """只撤掉图上的 AI 标注，保留结果列表"""
        try:
            self._study_panel.reader_2d.set_ai_overlay_image(None)
        except Exception:
            pass
        self._on_xray_clear_annotation_done()

    def _on_xray_clear_annotation_done(self):
        if hasattr(self, "_xray_clear_btn"):
            self._xray_clear_btn.setEnabled(False)
        # 图上的标注已经撤掉，开关和保存都没有对象可操作
        if hasattr(self, "_xray_anno_btn"):
            self._xray_anno_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # 标注图层（骨龄 13 关节）
    # ------------------------------------------------------------------
    def _xray_has_annotation(self):
        """当前结果是否带可切换的关节标注（目前只有骨龄）"""
        res = self._xray_result or {}
        return (res.get("image") is not None
                and res.get("raw_image") is not None)

    def _on_xray_toggle_annotation(self, checked=None):
        """在「带标注」和「无标注底图」之间切换

        标注是模型直接画在位图上的，没有矢量层可单独隐藏，所以只能整幅换图。
        切到底图后仍可用「保存标注图」导出带标注版本，两者互不影响。
        """
        res = self._xray_result or {}
        # 注意：这里必须用 is None 判断 —— 图像是 numpy 数组，
        # 直接 `if not arr` 会抛「truth value of an array is ambiguous」。
        if res.get("image") is None or res.get("raw_image") is None:
            return
        on = (self._xray_anno_btn.isChecked()
              if checked is None else bool(checked))
        img = res.get("image") if on else res.get("raw_image")
        try:
            self._study_panel.reader_2d.set_ai_overlay_image(img)
        except Exception as exc:      # noqa: BLE001
            print("切换关节标注失败: %s" % exc)
            return
        if hasattr(self, "_xray_anno_btn"):
            if self._xray_anno_btn.isChecked() != on:
                self._xray_anno_btn.blockSignals(True)
                self._xray_anno_btn.setChecked(on)
                self._xray_anno_btn.blockSignals(False)

    def _on_xray_save_annotation(self):
        """保存带标注的整张原分辨率图（不是屏幕截图）"""
        import cv2
        import numpy as np
        res = self._xray_result or {}
        img = res.get("image")
        if img is None:
            QtWidgets.QMessageBox.information(
                self, "提示", "当前没有可保存的标注图，请先执行一次检测。")
            return
        data = getattr(self, "_current_series_data", None) or {}
        pname = str(data.get("patient_name") or "").strip() or "X线"
        pname = "".join(c for c in pname
                        if c not in '\\/:*?"<>|\r\n\t')[:40] or "X线"
        kind = str((self._xray_kind_names or {}).get(
            res.get("kind") or "", "AI")).strip() or "AI"
        default = os.path.join(
            os.path.expanduser("~"),
            "%s_%s标注_%s.png" % (pname, kind,
                                  time.strftime("%Y%m%d_%H%M%S")))
        path, _sel = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存标注图", default,
            "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg)")
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg"):
            path += ".png"
            ext = ".png"
        try:
            arr = np.asarray(img)
            if arr.ndim == 2:
                arr = np.stack([arr] * 3, axis=-1)
            bgr = cv2.cvtColor(np.ascontiguousarray(arr[:, :, :3]), 
                               cv2.COLOR_RGB2BGR)
            # 走 imencode + tofile：中文路径用 cv2.imwrite 会静默失败/写乱码名
            ok, buf = cv2.imencode(ext, bgr,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 95]
                                   if ext != ".png" else [])
            if not ok:
                raise ValueError("图像编码失败")
            buf.tofile(path)
        except Exception as exc:      # noqa: BLE001
            QtWidgets.QMessageBox.critical(
                self, "保存失败", "标注图保存出错：\n%s" % exc)
            return
        QtWidgets.QMessageBox.information(
            self, "已保存", "标注图已保存到：\n%s" % path)
        self._xray_hint_label.setText("标注图已保存至 %s" % os.path.basename(path))

    def _current_xray_path(self):
        """当前 X 线序列对应的影像文件绝对路径"""
        data = getattr(self, "_current_series_data", None) or {}
        # 精确路径优先：用「打开影像文件」载入的单张片子，目录里可能同时躺着
        # 同一位患者的正位/侧位/多部位多张片子（DX 目录很常见）。若还按
        # 「取中间那张」去猜，AI 检测的可能压根不是屏幕上这张（v1.9.4）。
        exact = data.get("file_path")
        if exact and os.path.isfile(str(exact)):
            return str(exact)
        folder = data.get("folder") or getattr(self, "_current_series_folder", None)
        if not folder:
            return None
        uids = data.get("file_uids") or []
        if not uids:
            try:
                uids = sorted(n[:-4] for n in os.listdir(folder)
                              if n.lower().endswith(".dcm"))
            except Exception:
                return None
        if not uids:
            return None
        mid = uids[len(uids) // 2]
        name = mid if mid.lower().endswith(".dcm") else mid + ".dcm"
        path = os.path.join(folder, name)
        return path if os.path.isfile(path) else None

    def _on_xray_auto_detect(self):
        """按部位自动选择检测功能"""
        if self._xray_ai is None:
            return
        path = self._current_xray_path()
        if not path:
            QtWidgets.QMessageBox.information(
                self, "提示", "未能定位当前影像文件，请重新载入序列。")
            return
        # 先按载入时判定的部位选；部位没判出来再退回 xray 的粗分类。
        # 判不出就**不自动检测** —— 宁可让用户手动选，也不能把胸片喂给
        # 足弓模型跑出一个看起来正常的假数值。
        kind = XRAY_AUTO_PICK.get(getattr(self, "_xray_part_key",
                                          XRAY_PART_UNKNOWN))
        if kind is None:
            coarse = None
            try:
                coarse = self._xray_ai.detect_body_part(path)
            except Exception:        # noqa: BLE001
                coarse = None
            key = xray_coarse_to_part(coarse)
            if key != XRAY_PART_UNKNOWN:
                self._xray_part_key = key
                self._set_xray_ui_enabled(True, self._xray_part_hint())
                kind = XRAY_AUTO_PICK.get(key)
        if kind is None:
            QtWidgets.QMessageBox.information(
                self, "提示",
                "无法从 DICOM 判断检查部位，请手动选择对应的检测功能。\n\n"
                "（未识别部位时不自动检测，避免用错模型给出无意义的结果）")
            return
        names = getattr(self, "_xray_kind_names", {}) or {}
        self._xray_hint_label.setText(
            "已识别部位，正在执行 %s..." % names.get(kind, kind))
        self._on_xray_detect(kind)

    def _on_xray_detect(self, kind):
        if self._xray_ai is None:
            QtWidgets.QMessageBox.warning(
                self, "不可用", "X 线 AI 子系统未加载，请检查 xray 目录是否完整。")
            return
        if self._xray_worker is not None and self._xray_worker.isRunning():
            return

        # 部位适用性拦截：按钮平时已按部位置灰，这里再拦一次，覆盖
        # 「按钮刚被放开、随后载入新序列」以及任何程序化调用的边角情况。
        if not getattr(self, "_xray_part_ignore", False) and \
                kind not in self._xray_allowed_kinds():
            kind_cn = (getattr(self, "_xray_kind_names", {}) or {}).get(
                kind, kind)
            QtWidgets.QMessageBox.information(
                self, "部位不匹配",
                "%s\n\n请改用适用本部位的功能，或到「检测设置」里勾选"
                "「忽略部位匹配限制」（需自行确认片子是否合适）。"
                % xray_part_mismatch_text(
                    kind_cn, getattr(self, "_xray_part_key",
                                     XRAY_PART_UNKNOWN)))
            return

        # 五个功能统一走「文件路径」接口：xray 内部自行读片，能保证它拿到
        # 与自己原本一致的行序与窗值。曾尝试给胸片/足弓直接喂二维视图里的
        # ndarray，但那份数组是 vtkDICOMImageReader 的行序（下→上），与
        # xray 的 pydicom/cv2 行序（上→下）相反，标注图会上下镜像。
        path = self._current_xray_path()
        if path is None:
            QtWidgets.QMessageBox.information(
                self, "提示", "未能定位当前影像文件，请重新载入序列。")
            return

        sex = None
        if kind == "boneage":
            sex = self._resolve_patient_sex()
            if sex is None:
                return

        self._set_xray_ui_enabled(False, "正在执行检测，请稍候...")

        # 同一位患者常常有正位/侧位/多个部位的多张片子（如「颈椎正位 +
        # 颈椎侧位 + 腰椎正位 + 腰椎侧位」），检测对象必须与列表里看到的
        # 预览/当前画面是同一张，这里把片子名显式写出来供核对（v1.7.4）。
        cur = getattr(self, "_current_series_data", None) or {}
        shot = str(cur.get("series_description") or cur.get("body_part")
                   or "").strip() or os.path.basename(path)
        self._xray_hint_label.setText("正在对《%s》执行检测…" % shot)

        self._xray_progress.setVisible(True)
        self._xray_list.clear()
        # 新一轮检测开始：旧结果已清，复制按钮随之置灰
        try:
            self._xray_last_result_text = ""
            self._xray_copy_btn.setEnabled(False)
        except Exception:
            pass
        # 阅读区叠一层半透明遮罩：AI 推理要跑几秒，画面上得能看出来
        loading_ui.attach_busy(
            self._study_panel, "AI 正在分析影像...",
            sub=str(self._xray_kind_names.get(kind, kind) or kind), dim=0.62,
            spinner_size=64)

        worker = self._xray_bridge.XrayAIWorker(
            self._xray_ai, kind, image_path=path, image_rgb=None,
            sex=sex, parent=self)
        worker.progressed.connect(self._xray_hint_label.setText)
        worker.progressed.connect(
            lambda t: loading_ui.attach_busy(
                self._study_panel, "AI 正在分析影像...", sub=str(t or ""),
                dim=0.62))
        worker.finished.connect(self._on_xray_finished)
        worker.failed.connect(self._on_xray_failed)
        self._xray_worker = worker
        worker.start()

    def _resolve_patient_sex(self):
        """骨龄评估需要性别，DICOM 取不到时询问用户"""
        data = getattr(self, "_current_series_data", None) or {}
        raw = str(data.get("patient_sex") or "").strip().upper()
        if raw in ("M", "MALE", "男"):
            return "boy"
        if raw in ("F", "FEMALE", "女"):
            return "girl"
        items = ["男", "女"]
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "骨龄评估", "未能从 DICOM 取得患者性别，请选择：",
            items, 0, False)
        if not ok:
            return None
        return "boy" if choice == "男" else "girl"

    @staticmethod
    def _xray_paediatric_notes(result):
        """把「儿科心影校正」的依据渲染成几行浅色说明。

        没有儿科信息时返回空列表。为什么不直接塞进 items：那会让校正说明
        跟真正的检出项混在一起，用户分不清哪条是病变。
        """
        paed = result.get("paediatric") or {}
        if not paed:
            return []
        esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;"))
        line = '<span style="color:#8a9096;">%s</span>'
        out = []
        if paed.get("applied"):
            age = paed.get("age")
            try:
                head = "儿科心影校正：患者 %s 岁（＜15 岁，" \
                       "按儿科心胸比标准二次判读）" % ("%.1f" % float(age))
            except (TypeError, ValueError):
                head = "儿科心影校正：已按儿科心胸比标准二次判读"
            out.append(line % head)
            for note in paed.get("notes") or []:
                out.append(line % "　· %s" % esc(note))
        elif paed.get("text"):
            out.append(line % esc(paed["text"]))
        return out

    def _on_xray_finished(self, result):
        self._xray_progress.setVisible(False)
        loading_ui.detach_busy(self._study_panel)
        self._set_xray_ui_enabled(True, "检测完成")
        self._xray_result = result

        if not result.get("success"):
            self._xray_hint_label.setText(
                "检测失败：%s" % (result.get("error") or "未知原因"))
            QtWidgets.QMessageBox.warning(
                self, "检测失败", str(result.get("error") or "未知原因"))
            return

        # 图上标注：直接用 AI 生成的标注图替换显示
        if result.get("image") is not None:
            try:
                self._study_panel.reader_2d.set_ai_overlay_image(result["image"])
                self._xray_clear_btn.setEnabled(True)
            except Exception as exc:      # noqa: BLE001
                print("显示 AI 标注失败: %s" % exc)

        # 标注图层开关 / 保存标注图
        # 保存：任何带标注图的检测都能用（胸片、骨折、脊柱、足弓、骨龄）
        # 切换：只有骨龄同时带回无标注底图，才做得了「显示/隐藏」
        can_anno = bool(result.get("image") is not None
                        and result.get("raw_image") is not None)
        if getattr(self, "_xray_save_btn", None) is not None:
            self._xray_save_btn.setEnabled(result.get("image") is not None)
        if getattr(self, "_xray_anno_btn", None) is not None:
            self._xray_anno_btn.setEnabled(can_anno)
            self._xray_anno_btn.blockSignals(True)
            self._xray_anno_btn.setChecked(True)
            self._xray_anno_btn.blockSignals(False)
        self._xray_anno_ready = can_anno

        # 手动微调：把模型交点/关键点交给「微调关键点」，按钮这才放开。
        # 脊柱侧弯给的是上下终板线四个端点（拖它们重算 Cobb 角），
        # 足弓给的是五个关键点（拖它们重算足弓角）。
        self._xray_kp_points = result.get("keypoints") or None
        self._xray_kp_kind = result.get("kind") or None
        self._xray_kp_ai_image = result.get("image")
        self._xray_kp_moved = False
        self._xray_kp_angles = None
        try:
            reader = self._study_panel.active_reader
            if self._xray_kp_points and result.get("image") is not None:
                size = (int(np.asarray(result["image"]).shape[1]),
                        int(np.asarray(result["image"]).shape[0]))
                names = result.get("keypoint_names") or []
                if self._xray_kp_kind == "spine":
                    colors = _COBB_LINE_COLORS
                else:
                    # 手柄配色直接取模型自己的调色板：模型画的点是什么色，
                    # 手柄就是什么色，微调前后画面一致，用户不用重学一套色。
                    colors = _FOOT_ARCH_KEYPOINT_COLORS
                    try:
                        from xray.app.core.model.foot_arch_inference import (
                            KEYPOINT_COLORS)
                        colors = tuple(tuple(c) for c in KEYPOINT_COLORS)
                    except Exception:    # noqa: BLE001
                        pass
                reader.set_keypoints(
                    self._xray_kp_points, names=names, colors=colors,
                    size=size, shape=("cobb" if self._xray_kp_kind == "spine"
                                      else "foot"))
                reader.show_keypoints(False)
            else:
                reader.clear_keypoints()
        except Exception as exc:        # noqa: BLE001
            print("装载微调关键点失败: %s" % exc)
        self._xray_kp_set_buttons(
            can_edit=bool(self._xray_kp_points),
            editing=False, moved=False, kind=self._xray_kp_kind)

        metrics = result.get("metrics") or {}
        summary = result.get("summary") or ""
        parts = ["<b>%s</b>" % summary] if summary else []
        if metrics:
            parts.append(" · ".join("%s %s" % (k, v) for k, v in metrics.items()))
        parts.extend(self._xray_paediatric_notes(result))
        self._xray_summary_label.setText("<br>".join(parts))
        self._xray_summary_label.setVisible(True)

        # 同步生成一份纯文本结果（供「复制结果」用），与界面显示同一数据源
        plain = []
        if summary:
            plain.append(summary)
        if metrics:
            plain.append(" · ".join("%s %s" % (k, v) for k, v in metrics.items()))
        plain.extend(self._xray_paediatric_notes(result))
        for item in result.get("items", []):
            text = str(item.get("title", ""))
            detail = item.get("detail")
            if detail:
                text += "  %s" % detail
            conf = item.get("confidence")
            if conf is not None:
                text += "   %.2f" % float(conf)
            plain.append("• " + text)
        self._xray_last_result_text = "\n".join(plain)
        # 某些检测类型（骨龄）自带排版好的纯文本报告（影像所见/影像提示
        # 两段式），复制时优先用它，保证粘贴出去就是临床模板格式。
        copy_override = result.get("copy_text") or ""
        if copy_override:
            self._xray_last_result_text = copy_override
        self._xray_copy_btn.setEnabled(bool(self._xray_last_result_text))

        self._xray_list.clear()
        for item in result.get("items", []):
            text = str(item.get("title", ""))
            detail = item.get("detail")
            if detail:
                text += "  %s" % detail
            conf = item.get("confidence")
            if conf is not None:
                text += "   %.2f" % float(conf)
            self._xray_list.addItem(text)
        self._xray_list.setVisible(self._xray_list.count() > 0)
        self._xray_hint_label.setText("检测完成")

        self._save_xray_record(result)

    def _on_xray_copy_result(self):
        """把当前辅助诊断结果复制到剪贴板（纯文本）"""
        text = getattr(self, "_xray_last_result_text", "") or ""
        if not text:
            QtWidgets.QMessageBox.information(
                self, "提示", "当前没有可复制的辅助诊断结果。")
            return
        QtWidgets.QApplication.clipboard().setText(text)
        try:
            self.statusBar().showMessage("辅助诊断结果已复制到剪贴板", 3000)
        except Exception:
            pass

    def _save_xray_record(self, result):
        """把本次检测写入检测记录（只存文本摘要与指标，不存图像）"""
        if self._xray_store is None:
            return
        try:
            data = getattr(self, "_current_series_data", None) or {}
            self._xray_store.add(
                kind=result.get("kind", ""),
                summary=result.get("summary", ""),
                metrics=result.get("metrics") or {},
                patient_name=str(data.get("patient_name") or ""),
                patient_id=str(data.get("patient_id") or ""),
                access_no=str(data.get("patient_access") or ""),
                study_date=str(data.get("patient_study_date") or ""),
                image_path=str(self._current_xray_path() or ""))
        except Exception as exc:          # noqa: BLE001
            print("写入检测记录失败: %s" % exc)

    def _on_xray_settings(self):
        if self._xray_bridge is None:
            return
        dlg = DetectionSettingsDialog(self._xray_bridge, self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            # 「忽略部位限制」会改变按钮可用性，保存后立即重算一次
            try:
                self._xray_part_ignore = bool(
                    self._xray_bridge.load_detection_settings().get(
                        "ignore_part_scope", False))
            except Exception:        # noqa: BLE001
                pass
            if getattr(self, "_xray_group", None) is not None and \
                    self._xray_group.isVisible():
                self._set_xray_ui_enabled(True, self._xray_part_hint())
            else:
                self._xray_hint_label.setText("检测设置已保存")

    def _on_xray_records(self):
        if self._xray_bridge is None:
            return
        DetectionRecordsDialog(self._xray_bridge, self).exec_()

    def _xray_feedback_context(self):
        """收集当前片子的元信息，作为反馈记录的上下文"""
        data = getattr(self, "_current_series_data", None) or {}
        ctx = {
            "source_path": self._current_xray_path(),
            "patient_name": str(data.get("patient_name") or ""),
            "patient_id": str(data.get("patient_id") or ""),
            "access_no": str(data.get("patient_access") or ""),
            "study_date": str(data.get("patient_study_date") or ""),
            "body_part": str(data.get("body_part") or data.get("series_description") or ""),
            "series_title": str(data.get("series_description") or ""),
        }
        # 带上刚跑过的那次检测，反馈时能明确"在哪个结果上发现的漏检"
        res = getattr(self, "_xray_result", None) or {}
        if res:
            kind = res.get("kind") or ""
            name = ""
            if self._xray_bridge is not None:
                name = self._xray_bridge.KIND_META.get(kind, ("", ""))[0]
            ctx["detector"] = name or kind
            ctx["detect_result"] = str(res.get("summary") or "")
            ctx["model_version"] = self._xray_bridge.KIND_META.get(
                kind, (kind, ""))[1] if self._xray_bridge is not None else ""
        return ctx

    def _on_xray_feedback(self):
        """登记一条漏检反馈（图落盘 + 写库）

        槽函数里任何未捕获异常在 PyQt5 下都会 abort 掉整个进程（表现为
        「点一下按钮程序就没了」，控制台只有一行 ValueError）。所以这里
        整体包 try：出错就弹窗说明，而不是让程序静默闪退。
        """
        try:
            self._do_xray_feedback()
        except Exception as exc:      # noqa: BLE001
            traceback.print_exc()
            _log_error("反馈失败", exc)
            QtWidgets.QMessageBox.critical(
                self, "反馈失败", "打开反馈窗口时出错：\n%s" % exc)

    def _do_xray_feedback(self):
        if self._xray_bridge is None or self._xray_feedback_store is None:
            QtWidgets.QMessageBox.information(
                self, "提示", "X 线 AI 子系统未就绪，无法登记反馈。")
            return
        # 反馈库需要 SQLite；嵌入式环境若 DLL 路径缺失这里会抛 RuntimeError，
        # 提前做一次可用性自检，给出可读提示而不是等后面崩在渲染环节。
        try:
            ok, msg = self._xray_bridge.probe_sqlite()
        except Exception as exc:      # noqa: BLE001
            ok, msg = False, str(exc)
        if not ok:
            _log_error("反馈库不可用", msg)
            QtWidgets.QMessageBox.critical(
                self, "反馈失败", "反馈数据库不可用：\n%s" % msg)
            return
        path = self._current_xray_path()
        if not path:
            QtWidgets.QMessageBox.information(
                self, "提示", "未能定位当前影像文件，请重新载入序列后再登记反馈。")
            return
        ctx = self._xray_feedback_context()
        # 图片渲染发生在「提交反馈」时（Dialog._submit 内部），打开窗口
        # 阶段没有耗时操作，所以这里不要挂 QProgressDialog —— 模态进度框
        # 套在模态对话框外面，Windows 上会让事件循环嵌套出过问题。
        dlg = FeedbackDialog(self._xray_bridge, self._xray_feedback_store,
                             ctx, self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        self._refresh_feedback_count()
        self._xray_hint_label.setText("已登记反馈 #%d" % dlg.record_id)
        # 登记成功用轻提示即可，不必再弹一个要用户点「确定」的框
        loading_ui.show_toast(
            self, "反馈已登记 #%d，可在「反馈列表」中查看与导出" % dlg.record_id,
            kind="success", duration=3000)

    def _on_xray_feedback_list(self):
        try:
            if self._xray_bridge is None or self._xray_feedback_store is None:
                return
            ok, msg = self._xray_bridge.probe_sqlite()
            if not ok:
                _log_error("反馈列表不可用", msg)
                QtWidgets.QMessageBox.critical(
                    self, "反馈列表", "反馈数据库不可用：\n%s" % msg)
                return
            FeedbackListDialog(self._xray_bridge, self._xray_feedback_store,
                               self).exec_()
            self._refresh_feedback_count()
        except Exception as exc:      # noqa: BLE001
            traceback.print_exc()
            _log_error("反馈列表出错", exc)
            QtWidgets.QMessageBox.critical(
                self, "反馈列表", "打开反馈列表时出错：\n%s" % exc)

    def _on_xray_failed(self, message):
        self._xray_progress.setVisible(False)
        loading_ui.detach_busy(self._study_panel)
        self._set_xray_ui_enabled(True, "检测失败")
        QtWidgets.QMessageBox.warning(self, "检测失败", str(message))

    def _on_color_mode_changed(self, idx):
        data = self._color_combo.itemData(idx)
        
        # 内嵌遮罩代替系统进度框（loading_ui）
        with loading_ui.busy_scope(self._study_panel, "正在切换渲染模式...",
                                   sub="COLOR MODE"):
            self._study_panel.set_color_mode(data)

    # ------------------------------------------------------------------
    # 设置：患者扫描目录（多目录，持久化到 app_settings.json）
    # ------------------------------------------------------------------
    def _settings_config_path(self):
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "app_settings.json")

    def _read_settings_config(self):
        """读 app_settings.json，永远返回 dict（读不到/损坏/不是对象都给空）。"""
        try:
            with open(self._settings_config_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_settings_config(self, patch, what="配置"):
        """读-改-写：只改 patch 里出现的键，其余键原样保留。

        🔴 必须是读-改-写的。早先 scan_dirs 是唯一配置项，直接
        json.dump({"scan_dirs": ...}) 看不出问题；现在同一个文件里还存着
        update_base_url，整体覆盖会把升级服务器地址冲掉，表现成「设置里
        明明改过，下次打开又变回默认」，还查不出原因。
        """
        try:
            data = self._read_settings_config()
            data.update(patch or {})
            path = self._settings_config_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)      # Win7 + Py3.8 可用，比 remove 再改名少一个窗口期
            return True
        except Exception as exc:       # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "保存设置失败",
                "%s未能写入：\n%s\n\n文件：%s"
                % (what, exc, self._settings_config_path()))
            return False

    def _load_scan_dirs_config(self):
        """从 app_settings.json 读患者扫描目录；读不到/非法时返回空表（用默认）"""
        dirs = self._read_settings_config().get("scan_dirs")
        if isinstance(dirs, list):
            return [str(d).strip() for d in dirs if str(d).strip()]
        return []

    def _save_scan_dirs_config(self, dirs):
        return self._write_settings_config(
            {"scan_dirs": list(dirs)}, "扫描目录设置")

    def _load_update_base_url(self):
        """在线升级服务器：配置里没有/非法 → 内置默认地址。"""
        try:
            import updater
            return updater.resolve_base_url(self._read_settings_config())
        except Exception:              # noqa: BLE001
            return ""

    def _qc_show_mini_now(self):
        """当前「启动时自动打开质控小窗」的实际值；读不到给 None。"""
        try:
            from reportqc.ui.settings_dialog import get_show_mini
            return bool(get_show_mini())
        except Exception:              # noqa: BLE001
            return None

    def _apply_reportqc_show_mini(self, on):
        """写入质控小窗自动弹出开关，并让它**立即生效**。

        为什么要立即生效：设置里勾上以后如果还要等下次启动才出现，
        用户会以为没保存成功（v1.22 之前就是这个反馈，后来才改成即时）。
        这里只对「本次已经打开过质控」的情况动作，不主动创建控制器 ——
        为了一个开关去加载 OCR 引擎不值得。
        """
        try:
            from reportqc.ui.settings_dialog import _settings as _qc_settings
            st = _qc_settings()
            st.setValue("reportqc/show_mini", "1" if on else "0")
            st.sync()                  # 立刻落盘，别等进程退出
        except Exception as exc:       # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "保存设置失败", "报告质控设置未能写入：\n%s" % exc)
            return
        try:
            import reportqc_bridge
            reportqc_bridge.apply_mini_visibility_if_running()
        except Exception:              # noqa: BLE001
            pass                       # 小窗没开过就没什么可应用的

    def _get_scan_dirs(self, root_dir=None):
        """本次扫描实际用的目录。

        优先级：显式指定的 root_dir（用户刚在「加载患者目录」里选的）
        > 设置里配置的多个目录（取存在的）> 出厂默认目录。
        """
        if root_dir and os.path.isdir(root_dir) and root_dir != DEFAULT_DATA_DIR:
            return [root_dir]
        dirs = [d for d in getattr(self, "_scan_dirs", [])
                if d and os.path.isdir(d)]
        if dirs:
            return dirs
        return [d for d in DEFAULT_SCAN_DIRS if os.path.isdir(d)]

    def _on_reportqc_clicked(self):
        """显示报告质控悬浮小窗（启动时会自动弹出，这里用于重新打开）。

        交互（v1.16.0 起）：小窗常驻；识别区域只需设置一次（存 QSettings），
        之后每次点小窗上的「刷新」或双击小窗即可识别当前报告。
        控制器在 reportqc_bridge 里（进程内单例），OCR 推理走后台线程，
        不阻塞 UI。模型缺失时桥接层会弹窗说明。
        """
        try:
            import reportqc_bridge
        except Exception as exc:      # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "报告质控", "报告质控模块加载失败：%s" % exc)
            return
        if not reportqc_bridge.reportqc_available():
            QtWidgets.QMessageBox.warning(
                self, "报告质控",
                "OCR 模型缺失，报告质控不可用。\n"
                "请确认 reportqc/models/ocr/ 下的三个 .onnx 模型文件完好。")
            return
        reportqc_bridge.get_controller().show_window()

    def _on_reportqc_history_clicked(self):
        """打开报告质控历史记录窗。"""
        try:
            import reportqc_bridge
        except Exception as exc:      # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "报告质控", "报告质控模块加载失败：%s" % exc)
            return
        reportqc_bridge.get_controller().open_history()

    def _on_reportqc_settings_clicked(self, parent=None):
        """打开报告质控设置窗（入口：主「设置」→ 报告质控 → 质控设置…）。"""
        try:
            import reportqc_bridge
        except Exception as exc:      # noqa: BLE001
            QtWidgets.QMessageBox.warning(
                self, "报告质控", "报告质控模块加载失败：%s" % exc)
            return
        reportqc_bridge.get_controller().open_settings(parent=parent)

    def _on_settings_clicked(self):
        """打开设置对话框：患者扫描目录 + 在线升级服务器 + 报告质控 + 使用说明"""
        cur_dirs = list(getattr(self, "_scan_dirs", []))
        cur_base = self._load_update_base_url()
        dlg = SettingsDialog(
            self, dirs=cur_dirs,
            help_callback=self._show_about,
            base_url=cur_base,
            reportqc_callback=self._on_reportqc_settings_clicked)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return

        # ---- 报告质控：启动是否自动弹小窗 ----
        want_mini = dlg.get_show_mini()
        if want_mini is not None and want_mini != self._qc_show_mini_now():
            self._apply_reportqc_show_mini(want_mini)

        # ---- 升级服务器 ----
        # 输入等于内置默认就存空串（= 跟随常量），这样以后代码里改默认地址，
        # 没动过这项的客户机自动跟着变；存的是"改过"的地址才保留。
        base = dlg.get_base_url()
        base_cfg = "" if base in ("", getattr(dlg, "_default_base", None)) else base
        old_cfg = str(self._read_settings_config().get("update_base_url") or "")
        if base_cfg != old_cfg:
            if self._write_settings_config({"update_base_url": base_cfg},
                                           "升级服务器设置"):
                # 换了源，之前那次静默检查的结论就作废了：按钮恢复常态，
                # 重新查一次（新地址没网也不会打扰用户）。
                try:
                    self._update_plan = None
                    self._update_btn.setText("更新")
                    self._update_btn.setToolTip(
                        "检查在线更新\n当前版本 %s\n服务器 %s"
                        % (APP_VERSION, base or self._load_update_base_url()))
                    theme.role(self._update_btn, "tonal")
                except Exception:      # noqa: BLE001
                    pass
                self._start_silent_update_check()

        # ---- 扫描目录 ----
        dirs = dlg.get_dirs()
        if dirs == cur_dirs:
            return   # 没改动就不重扫
        self._scan_dirs = dirs
        self._save_scan_dirs_config(dirs)
        # 新目录立即生效：重扫列表 + 让文件监视器盯新目录
        try:
            self._setup_file_watcher()
        except Exception:
            pass
        # root_dir 必须传 None：_get_scan_dirs 里显式 root_dir 会**独占**返回
        # （只扫那一个目录），设置里新加的目录就永远轮不到。同时用增量刷新，
        # 只追加新目录的患者 —— 此前 incremental=False 全量重建，用户手动
        # 加载过的目录会被直接清掉（v1.9.9）
        self._load_patient_tree(None, incremental=True, show_progress=True)

    def _on_clear_list_clicked(self):
        """仅清空患者列表显示，不删除任何文件"""
        reply = QtWidgets.QMessageBox.question(
            self, "清空列表",
            "确定清空患者列表吗？\n\n只清空显示，不会删除任何文件；"
            "点「刷新」可重新载入。",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if reply != QtWidgets.QMessageBox.Yes:
            return
        self._clear_patient_tree()
        self._cached_patients.clear()
        self._set_tree_placeholder(
            "列表已清空（文件未删除，点「刷新」重新载入）")

    def _on_refresh_clicked(self):
        """刷新患者列表，检测新文件夹（增量刷新）

        root_dir 传 None：_get_scan_dirs 里显式 root_dir 会独占返回，
        传 self._root_dir 时设置里新增的目录永远刷不进来（v1.9.9）。
        """
        self._load_patient_tree(None, incremental=True, show_progress=True)

    def _on_sort_changed(self, index):
        """排序方式改变时重新加载患者列表"""
        self._load_patient_tree(None, incremental=False, show_progress=False)

    def _on_delete_all_patients(self):
        """一键删除所有患者文件夹"""
        # 获取扫描目录
        scan_dirs = self._get_scan_dirs(self._root_dir)
        
        # 确认对话框
        reply = QtWidgets.QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除以下目录中的所有患者文件夹吗？\n\n" +
            "\n".join(scan_dirs) +
            "\n\n此操作不可恢复！",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No
        )
        
        if reply != QtWidgets.QMessageBox.Yes:
            return
        
        # 执行删除
        deleted_count = 0
        failed_items = []
        
        for scan_dir in scan_dirs:
            if not os.path.isdir(scan_dir):
                continue
            try:
                for item in os.listdir(scan_dir):
                    item_path = os.path.join(scan_dir, item)
                    if os.path.isdir(item_path):
                        try:
                            shutil.rmtree(item_path)
                            deleted_count += 1
                        except Exception as e:
                            failed_items.append(f"{item}: {str(e)}")
            except Exception as e:
                failed_items.append(f"{scan_dir}: {str(e)}")
        
        # 清空当前显示
        self._on_close_current()
        
        # 刷新患者列表
        self._clear_patient_tree()
        self._cached_patients.clear()
        self._set_tree_placeholder("未找到CT薄层序列")
        
        # 显示结果
        if failed_items:
            QtWidgets.QMessageBox.warning(
                self,
                "删除完成",
                f"成功删除 {deleted_count} 个文件夹\n\n以下项目删除失败:\n" + "\n".join(failed_items[:10])
            )
        else:
            QtWidgets.QMessageBox.information(
                self,
                "删除完成",
                f"成功删除 {deleted_count} 个患者文件夹"
            )

    def _load_patient_tree(self, root_dir: str, incremental: bool = False, show_progress: bool = False):
        refresh_item = None
        if not incremental:
            self._clear_patient_tree()
            refresh_item = QtWidgets.QTreeWidgetItem(["正在刷新患者列表..."])
            self._patient_tree.addTopLevelItem(refresh_item)
        else:
            if self._patient_tree.topLevelItemCount() > 0:
                first_item = self._patient_tree.topLevelItem(0)
                if first_item.text(0) == "正在刷新患者列表...":
                    return
            if show_progress:
                refresh_item = QtWidgets.QTreeWidgetItem(["正在刷新患者列表..."])
                self._patient_tree.addTopLevelItem(refresh_item)
        QtCore.QCoreApplication.processEvents()

        # 无论是否弹进度框，都在树控件上叠一层内嵌遮罩：启动后的首次加载
        # （show_progress=False）原本只有一行「正在加载患者列表...」文字，
        # 看不出程序在动，这里统一给个动画（loading_ui）。
        loading_ui.attach_busy(self._patient_tree, "正在扫描患者数据...",
                               sub="SCANNING DICOM NODES", spinner_size=46,
                               dim=0.80)

        progress = None
        if show_progress:
            progress = loading_ui.TechBusyDialog("正在扫描患者数据...", "取消", self)
            progress.setWindowTitle("请稍候")
            progress.setWindowModality(QtCore.Qt.WindowModal)
            progress.setMinimumDuration(500)
            progress.setRange(0, 100)
            progress.setValue(0)

        def progress_callback(current, total, patient_name):
            if total > 0:
                percent = int(current * 100 / total)
                if progress:
                    progress.setValue(percent)
                    progress.setLabelText(
                        f"正在分析: {patient_name}\n({current}/{total})")
                loading_ui.attach_busy(
                    self._patient_tree, "正在扫描患者数据...",
                    sub="%d / %d · %s" % (current, total, patient_name or ""))
            QtCore.QCoreApplication.processEvents()

        try:
            scan_dirs = self._get_scan_dirs(root_dir)
            patients = collect_ct_patients(
                scan_dirs,
                min_images=0,
                progress_callback=progress_callback
            )
        except Exception as e:
            if progress:
                progress.close()
            loading_ui.detach_busy(self._patient_tree)
            # 必须摘掉「正在刷新患者列表...」条目：残留会让后续所有增量刷新
            # 在「首项是刷新提示则 return」的判断处永久静默早退。
            if refresh_item is not None:
                idx = self._patient_tree.indexOfTopLevelItem(refresh_item)
                if idx != -1:
                    self._patient_tree.takeTopLevelItem(idx)
            if not incremental:
                QtWidgets.QMessageBox.warning(self, "错误", f"加载患者列表失败: {str(e)}")
            return

        if progress:
            progress.close()

        # 兜底（v1.9.8）：目录里没有 XML / 合规 DICOM 序列时（PACS 导出的图片
        # 目录、纯图片导出包、命名随意的科室导出），按「一个文件夹 = 一条序列」
        # 把图片列出来 —— 此前只会落一句「未找到CT薄层序列」，用户根本无从
        # 下手。v1.9.9 起对**每个扫描根**都补（此前只扫 root_dir，设置里新加的
        # 目录深层图片永远进不了列表 —— 实测 F:\疑难病例\典型病例\hiv肺炎
        # 这种多层嵌套就是扫不出来）。os.walk 不限深度，深层目录也能到。
        if not patients:
            merged = []
            for _sd in scan_dirs:
                if not _sd or not os.path.isdir(_sd):
                    continue
                try:
                    loading_ui.attach_busy(
                        self._patient_tree, "正在扫描患者数据...",
                        sub="图片目录 %s" % os.path.basename(_sd))
                    merged.extend(build_image_group_patient(
                        _sd, dir_image_groups(_sd)))
                except Exception as grp_err:      # noqa: BLE001
                    print("图片分组扫描失败 [%s]: %s" % (_sd, grp_err))
            patients = merged

        # Remove the refresh item
        if refresh_item:
            if not incremental:
                self._clear_patient_tree()
            else:
                index = self._patient_tree.indexOfTopLevelItem(refresh_item)
                if index != -1:
                    self._patient_tree.takeTopLevelItem(index)

        if not patients:
            loading_ui.detach_busy(self._patient_tree)
            if not incremental:
                self._set_tree_placeholder("未找到CT薄层序列")
            return

        existing_patients = {}
        if incremental:
            for i in range(self._patient_tree.topLevelItemCount()):
                item = self._patient_tree.topLevelItem(i)
                item_data = item.data(0, QtCore.Qt.UserRole)
                if item_data and item_data.get("type") == "patient":
                    patient_data = item_data.get("data", {})
                    pat_id = patient_data.get("pat_id", "")
                    pat_access = patient_data.get("pat_access", "")
                    pat_name = patient_data.get("pat_name", "")
                    key = f"{pat_id}|{pat_access}|{pat_name}"
                    existing_patients[key] = item

        new_patients = []
        for patient in patients:
            pat_id = patient.get("pat_id", "")
            pat_access = patient.get("pat_access", "")
            pat_name = patient.get("pat_name", "")
            key = f"{pat_id}|{pat_access}|{pat_name}"
            
            if key in existing_patients:
                continue
            
            new_patients.append(patient)
            self._cached_patients[key] = patient

        if not incremental:
            self._clear_patient_tree()
            self._cached_patients.clear()
            for patient in patients:
                pat_id = patient.get("pat_id", "")
                pat_access = patient.get("pat_access", "")
                pat_name = patient.get("pat_name", "")
                key = f"{pat_id}|{pat_access}|{pat_name}"
                self._cached_patients[key] = patient
            new_patients = patients
        
        # 根据排序选项对患者列表进行排序
        sort_order = self._sort_combo.currentData() if hasattr(self, '_sort_combo') else "desc"
        if sort_order == "asc":
            # 最早在前（正序）
            new_patients.sort(key=lambda x: x.get("mtime", 0.0))
        else:
            # 最新在前（倒序，默认）
            new_patients.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)

        # 移除可能存在的 empty_item（可能有多个，全部清掉）
        self._clear_tree_placeholder()

        for patient in new_patients:
            name = patient.get("pat_name") or "未知患者"
            sex = patient.get("pat_sex") or ""
            age = patient.get("pat_age") or ""
            study_date = patient.get("study_date", "")
            if study_date and len(study_date) == 8:
                study_date = f"{study_date[:4]}-{study_date[4:6]}-{study_date[6:]}"

            # 获取检查部位（从第一个序列获取）
            series_list = patient.get("series", [])
            body_part = ""
            if series_list:
                body_part = series_list[0].get("body_part", "")

            # 患者级模态前缀：CT / DR / MR 在同一个列表里，靠前缀一眼区分
            modality = str(patient.get("modality") or "").upper()
            mods = {str(s.get("modality") or "").upper() for s in (series_list or [])}
            has_mr = any(is_mr_modality(m) for m in mods)
            has_xr = any(is_xr_modality(m) for m in mods)
            has_ct = "CT" in mods
            if modality == "MIXED":
                # 多模态混合：按实际含有的模态拼前缀，最多两个
                parts = []
                if has_ct:
                    parts.append("CT")
                if has_xr:
                    parts.append("DR")
                if has_mr:
                    parts.append("MR")
                prefix = "[%s]" % "+".join(parts[:2]) if parts else ""
            elif modality == "CT":
                prefix = "[CT]"
            elif modality == "MR" or (modality != "XR" and has_mr and not has_ct):
                prefix = "[MR]"
            elif modality == "XR":
                prefix = "[DR]"
            else:
                prefix = ""

            # 构建标题：前缀 姓名 性别 年龄 检查部位 日期（年龄仅显示，不再用于选阈值）
            display_age = age if age else "年龄未知"
            display_name = ("%s %s" % (prefix, name)).strip() if prefix else name
            title_parts = [p for p in [display_name, sex, display_age,
                                       body_part, study_date] if p]
            title = " ".join(title_parts)
            patient_item = QtWidgets.QTreeWidgetItem([title])
            # 按模态给患者名着色，方便在一长串列表里快速定位
            # （CT 蓝 / DR 绿 / MR 紫 / 混合 橙）
            patient_item.setForeground(
                0, QtGui.QBrush(self._modality_color(patient, mods)))
            patient_item.setData(0, QtCore.Qt.UserRole, {
                "type": "patient",
                "data": patient,
            })

            # 年龄缺失的行曾在这里把前景色刷成淡灰当提示，但那会把上面
            # 的模态配色整个盖掉 —— 一长串列表里「缺年龄」比「什么检查」
            # 更不常看，而标题里本来就写着「年龄未知」。所以提示改由文字
            # 承担，颜色只表达模态。

            self._patient_tree.addTopLevelItem(patient_item)

            for idx, series in enumerate(series_list):
                series_uid = series.get("series_uid") or "未命名序列"
                img_count = series.get("img_count") or 0
                slice_thickness = series.get("slice_thickness", 0)
                series_desc = series.get("series_description", "")
                series_body_part = series.get("body_part", "")  # 该序列的检查部位

                # 构建显示文本 - 按XML中的SeriesItem顺序显示
                seq_num = idx + 1  # 序列序号
                is_xr_series = is_xr_modality(series.get("modality"))
                # X 线一幅片子就是一个序列，再写「1张」没有意义，改用体位名做主标题
                count_str = "1 幅" if is_xr_series else f"{img_count}张"

                # 设备只导出一张概览图的 MR 序列：明确标注，避免被误认为解析出错
                # （实测有MRA 只给 1 张、躺在扫描根目录下，序列名却叫 tof3d_tra_MRA）
                is_surveyor = bool(series.get("is_surveyor_single"))
                if is_surveyor:
                    count_str = "概览图 1张"

                # 层厚显示（过滤掉异常值）
                thickness_str = ""
                if 0.1 < slice_thickness < 50:
                    thickness_str = f"层厚:{slice_thickness:.2f}mm"

                # 显示检查部位
                part_str = f"| {series_body_part}" if series_body_part else ""

                series_title = f"序列{seq_num}: {count_str} {thickness_str} {part_str}".strip()
                if is_surveyor:
                    series_title += "\n（设备仅导出概览图，未包含完整序列数据）"

                if is_xr_series:
                    # 缩略图卡片：第一行=体位（如「颈椎正位」），第二行=部位/张数
                    main_desc = series_desc or series_body_part or count_str
                    title_line = f"{seq_num}. {main_desc}"
                    detail_line = " · ".join(
                        [p for p in [series_body_part, count_str] if p])
                else:
                    # 缩略图卡片：第一行=序号/张数/部位，第二行=层厚/序列描述
                    title_line = f"序列{seq_num} · {count_str}"
                    if series_body_part:
                        title_line += f" · {series_body_part}"
                    detail_line = " · ".join(
                        [p for p in [thickness_str, series_desc] if p])
                    if is_surveyor:
                        detail_line += " · 设备仅导出概览图"
                    # 血管重建系列能看二维、但堆不成体数据，
                    # 必须在列表上就告知用户，否则他点三维
                    # 拿到一壁墙才发现不可用。
                    if series.get("mr_stackable") is False:
                        detail_line += " · 仅二维（不支持三维）"

                series_item = QtWidgets.QTreeWidgetItem([title_line])
                series_item.setData(0, QtCore.Qt.UserRole, {"type": "series", "data": series})
                series_item.setData(0, QtCore.Qt.UserRole + 1, detail_line)
                series_item.setToolTip(
                    0, f"{series_title}\n目录: {series.get('folder', '')}")

                patient_item.addChild(series_item)

                # 异步生成序列缩略图（结果按 series 缓存，刷新列表不会重复读盘）
                series_folder = series.get("folder", "")
                thumb_key = f"{series_uid}|{series_folder}"
                # 模态要传给缩略图生成：X 线的 DICOM 窗值量纲与 CT 不同，
                # 预览与加载后的主画面必须走同一套灰度基准
                self._request_series_thumbnail(thumb_key, series_folder,
                                               series.get("file_uids", []),
                                               series_item,
                                               series.get("modality"))

                # 自动展开并选中第一个序列（仅对第一个患者生效，或根据需要调整）
                if idx == 0 and self._patient_tree.topLevelItemCount() == 1:
                    patient_item.setExpanded(True)
                    self._patient_tree.setCurrentItem(series_item)

        # 树已填充完毕，摘掉遮罩（放在最后：建树本身也可能是耗时的活）
        loading_ui.detach_busy(self._patient_tree)

        if incremental and show_progress:
            if len(new_patients) > 0:
                # 轻提示（Toast）：不需要用户点「确定」，自动消失
                loading_ui.show_toast(
                    self, "刷新完成：成功添加 %d 位新患者" % len(new_patients),
                    kind="success", duration=2600)

        if incremental:
            self._setup_file_watcher()

        # 增量刷新可能新增了患者条目：若当前正在搜索，新条目也要走一遍过滤，
        # 否则「搜索着刷新」会把不匹配的患者直接塞进结果里。
        try:
            self._apply_patient_filter()
        except Exception:          # noqa: BLE001
            pass

    # ---------------- 患者列表序列缩略图 ----------------

    def _clear_patient_tree(self):
        """清空患者列表（同时释放缩略图待刷新项，避免持有已销毁的条目）"""
        self._thumb_items.clear()
        self._patient_tree.clear()
        label = getattr(self, "_search_count", None)
        if label is not None:
            label.setText("")

    def _set_tree_placeholder(self, text: str):
        """在患者列表区显示唯一的占位提示项。

        占位项必须带 UserRole={"type": "empty"} 标记，否则增量刷新时
        认不出来，会与真实患者条目同时残留在列表里（曾经出现过
        「有患者列表却仍显示『未找到CT薄层序列』」）。
        """
        self._clear_tree_placeholder()
        empty_item = QtWidgets.QTreeWidgetItem([str(text)])
        empty_item.setData(0, QtCore.Qt.UserRole, {"type": "empty"})
        self._patient_tree.addTopLevelItem(empty_item)
        return empty_item

    def _clear_tree_placeholder(self):
        """移除所有占位提示项（真实患者条目不受影响）。"""
        tree = getattr(self, "_patient_tree", None)
        if tree is None:
            return 0
        removed = 0
        for i in range(tree.topLevelItemCount() - 1, -1, -1):
            item = tree.topLevelItem(i)
            item_data = item.data(0, QtCore.Qt.UserRole)
            if item_data and item_data.get("type") == "empty":
                tree.takeTopLevelItem(i)
                removed += 1
        return removed

    # ------------------------------------------------------------------
    # 患者搜索（v1.15.1）：汉字 / 全拼 / 拼音首字母
    #
    # 实现要点：
    #   * 不重建树，只对**顶层患者节点** setHidden(True/False)，序列子节点
    #     跟着父节点一起隐藏 —— 重建树会丢掉缩略图缓存、展开状态和选中项。
    #   * 拼音转换按「姓名」缓存，同一个患者反复过滤只算一次；
    #     pypinyin 第一次调用要加载 ~4MB 字典（约 200ms），所以这里**懒加载**，
    #     没用到搜索功能的人不付这个启动成本。
    #   * 姓氏多音字（单 shan / 曾 zeng / 解 xie …）要把所有读音都当关键词，
    #     否则输入 "shan" 搜不到「单田芳」。
    # ------------------------------------------------------------------
    def _focus_patient_search(self):
        """Ctrl+F：把焦点落到搜索框并全选，方便直接覆盖输入。"""
        edit = getattr(self, "_search_edit", None)
        if edit is None:
            return
        try:
            edit.setFocus(QtCore.Qt.ShortcutFocusReason)
            edit.selectAll()
        except Exception:          # noqa: BLE001
            pass

    def _on_patient_search_changed(self, text):
        self._apply_patient_filter(text)

    def _name_search_keys(self, name):
        """姓名 → 可被搜索命中的关键词集合（汉字/全拼/首字母/姓氏多音字）。"""
        name = (name or "").strip()
        if not name:
            return set()

        cache = getattr(self, "_pinyin_cache", None)
        if cache is None:
            cache = self._pinyin_cache = {}
        if name in cache:
            return cache[name]

        keys = {name.lower()}
        try:
            from pypinyin import lazy_pinyin, pinyin
            syllables = [s.lower() for s in lazy_pinyin(name)]
            full = "".join(syllables)
            initials = "".join(s[0] for s in syllables if s)
            keys.add(full)
            keys.add(initials)

            # 姓氏多音字：只用第一个字的所有读音去替换首音节，
            # 组合数不会爆炸（单字最多 4 个读音），却能覆盖 90% 的姓氏坑。
            first = name[0]
            if "\u4e00" <= first <= "\u9fff" and len(syllables) >= 1:
                rest_full = "".join(syllables[1:])
                rest_ini = "".join(s[0] for s in syllables[1:] if s)
                try:
                    # 注意两个坑：
                    # ① pinyin('单', heteronym=True) 返回的是
                    #    [['dān','chán','shàn']] —— 外层每个汉字一个元素，
                    #    内层才是该字的全部读音。写 pinyin(...)[i][0] 会只拿到
                    #    第一个读音（单 shan 就搜不到）。
                    # ② style=Style.NORMAL 与 heteronym 同时用时，pypinyin
                    #    只给首读音，所以这里取带声调的原样结果再自己去调。
                    readings = pinyin(first, heteronym=True)
                    raw = list(readings[0]) if readings else []
                    variants = [_strip_tone(str(r)).lower() for r in raw]
                except Exception:      # noqa: BLE001
                    variants = []
                for v in variants:
                    if not v:
                        continue
                    keys.add(v + rest_full)
                    keys.add(v[0] + rest_ini)
        except Exception:              # noqa: BLE001
            # 没有 pypinyin 也要能用：退化成纯汉字子串匹配
            pass

        keys.discard("")
        cache[name] = keys
        return keys

    def _patient_matches(self, patient, query):
        query = (query or "").strip().lower()
        if not query:
            return True
        name = str(patient.get("pat_name") or "")
        if query in name.lower():
            return True
        # 顺带支持按患者 ID 检索（PACS 号/住院号那种纯数字或字母串）
        if query in str(patient.get("pat_id") or "").lower():
            return True
        return any(query in k for k in self._name_search_keys(name))

    def _apply_patient_filter(self, text=None):
        """按搜索词隐藏不匹配的患者行，并更新右上角的「匹配/总数」。"""
        tree = getattr(self, "_patient_tree", None)
        if tree is None:
            return
        if text is None:
            edit = getattr(self, "_search_edit", None)
            text = edit.text() if edit is not None else ""
        query = (text or "").strip()

        total = 0
        shown = 0
        matched_items = []
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            try:
                data = item.data(0, QtCore.Qt.UserRole)
            except Exception:      # noqa: BLE001
                continue
            if not (isinstance(data, dict) and data.get("type") == "patient"):
                continue        # 占位项 / 加载提示项不参与过滤
            total += 1
            if not query:
                item.setHidden(False)
                shown += 1
                continue
            hit = self._patient_matches(data.get("data") or {}, query)
            item.setHidden(not hit)
            if hit:
                shown += 1
                matched_items.append(item)

        # 命中很少时自动展开，省掉用户再点一下的功夫；
        # 命中多就不展开，否则列表会被撑成几百行，反而看不到全貌。
        if query and len(matched_items) <= 5:
            for item in matched_items:
                item.setExpanded(True)

        label = getattr(self, "_search_count", None)
        if label is not None:
            if not query:
                label.setText("")
            elif total == 0:
                label.setText("")
            else:
                label.setText("%d/%d" % (shown, total))

    # ------------------------------------------------------------------
    # 右键导出：患者级（全部序列）/ 序列级（单个序列）
    # ------------------------------------------------------------------
    def _on_patient_tree_menu(self, item, global_pos):
        """患者树右键菜单。

        注意 item 可能是 None（点在空白处）—— 这时**不弹任何菜单**，
        不是错误路径，但也不能什么都不做让用户以为程序卡住。
        """
        if item is None:
            return
        try:
            data = item.data(0, QtCore.Qt.UserRole)
        except Exception:
            return
        if not (isinstance(data, dict) and data.get("type") in ("patient", "series")):
            return

        menu = QtWidgets.QMenu(self)
        # DICOM 项一律给：患者节点下若混了图片目录，导出会按序列报出
        # 「该序列不是 DICOM 文件」并列出失败原因，比提前禁用更好懂
        act_dicom = menu.addAction("导出 DICOM 文件...")
        act_png = menu.addAction("导出为 PNG 图片...")
        act_jpg = menu.addAction("导出为 JPG 图片...")
        menu.addSeparator()
        act_open = menu.addAction("打开导出目录")

        chosen = menu.exec_(global_pos)
        if chosen is None:
            return
        try:
            if chosen is act_dicom:
                self._export_series_items([item], "dicom")
            elif chosen is act_png:
                self._export_series_items([item], "png")
            elif chosen is act_jpg:
                self._export_series_items([item], "jpg")
            elif chosen is act_open:
                self._open_last_export_dir()
        except Exception as exc:   # noqa: BLE001
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(
                self, "导出失败", "导出过程中出错：\n%s" % exc)

    def _view_window_for_series(self, series):
        """该序列若正在二维视图中显示，返回 (窗宽, 窗位, 反相)；否则全 None。

        为什么要取视图的窗值：用户在界面上切到「脑窗」再导出，期望导出的
        就是他看到的那一帧。导出端若自行按 DICOM 窗值或像素分位重算，
        必然对不上（实测：脑窗 WW80/WL40 被重算成等价 1500 宽的骨窗）。

        反相取「MONOCHROME1 自动反相」与「用户手动反色」的**异或** ——
        与 Dicom2DReader._build_slice_image 的判定完全一致，两者都对同一
        区间做镜像，同时成立会互相抵消。

        **窗位必须换算回原始数据域**：视图里的窗位是配给「已反相数据」的
        （自动反相时 MainWindow 用 _xr_window_level(invert=True) 算了镜像
        窗值；手动反色时数据镜像、窗值不动，效果等价于镜像）。导出走的是
        「原始数据 + 灰度域翻转」，两者域不同，直接用会整体偏移
        (S-2L)/W —— 实测表现为画面偏亮或发黑。所以这里用整卷
        min+max（_invert_sum）把窗位镜像回去。
        """
        try:
            panel = self._study_panel
            if panel is None:
                return None, None, None
            reader = panel.active_reader
            if reader is None or not reader.has_image():
                return None, None, None

            # 序列身份：先比目录，同一目录里多序列时再比文件集合。
            # 不能只比目录 —— 一个目录里躺着头颅平扫 + 增强两个序列很常见。
            try:
                folder = reader.GetDirectoryName()
            except Exception:      # noqa: BLE001
                folder = None
            s_folder = series.get("folder") or ""
            if not folder or not s_folder:
                return None, None, None
            if os.path.normcase(os.path.abspath(folder)) != \
                    os.path.normcase(os.path.abspath(s_folder)):
                return None, None, None

            try:
                r_files = reader.GetFiles()
            except Exception:      # noqa: BLE001
                r_files = None
            s_uids = series.get("file_uids") or []
            if r_files and s_uids:
                rset = {os.path.splitext(os.path.normcase(os.path.basename(
                    str(f))))[0] for f in r_files}
                sset = {os.path.splitext(os.path.normcase(str(u)))[0]
                        for u in s_uids}
                if not (rset & sset):
                    return None, None, None

            window, level = panel.get_window_level()
            try:
                window = float(window)
                level = float(level)
            except (TypeError, ValueError):
                return None, None, None
            if window < 1.0:
                return None, None, None

            invert = bool(getattr(reader, "_display_invert", False)) != \
                bool(getattr(reader, "_user_invert", False))
            if invert:
                # 整卷 min+max：必须与 Dicom2DReader._invert_polarity 同一基准，
                # 否则镜像后的窗位与数据不同域。
                total = getattr(reader, "_invert_sum", None)
                if total is None:
                    # 还没算过（序列刚载入）→ 让导出自己按 DICOM 判断极性，
                    # 不能拿一个域不确定的窗值去套。
                    return None, None, None
                level = float(total) - level
            return window, level, invert
        except Exception:          # noqa: BLE001
            return None, None, None

    def _export_series_items(self, items, kind: str):
        """把选中节点涉及的序列导出到桌面。

        - 患者节点 → 其全部子序列
        - 序列节点 → 仅该序列
        - 多个选中（Ctrl/Shift）时按选中项合并导出，**允许部分成功**：
          失败项一次性列出，不因一个序列坏掉就整批报废。
        """
        # 收集「患者 + 序列」配对。
        # 关键：**不能用 id(series) 关联患者**。QTreeWidgetItem.data() 返回的是
        # QVariantMap 反序列化出来的**新 dict**，每次取值对象都不同，id() 必然
        # 失配（实测症状：导出目录叫 ANON-001、文件里的 PatientName 却是
        # ANON-002）。统一用「序列特征值」做键。
        def _series_key(s):
            return (s.get("series_uid", ""), s.get("folder", ""),
                    tuple(s.get("file_uids") or []))

        entries = []
        seen = set()
        for it in (items or []):
            if it is None:
                continue
            pat = self._patient_of_node(it)
            for series in self._collect_series_for_export(it):
                # 去重键必须带 series_uid：同一目录里两个序列若 file_uids
                # 恰好一致（个别设备导出重名文件），只按路径去重会把
                # 「两个不同序列」误并成一个，少导一整个序列。
                key = _series_key(series)
                if key in seen:
                    continue
                seen.add(key)
                entries.append((pat, series))
        series_list = [s for _, s in entries]
        pat_by_series = {_series_key(s): p for p, s in entries}
        if not series_list:
            QtWidgets.QMessageBox.information(
                self, "无可导出内容", "所选项目下没有可导出的序列文件。")
            return

        # ---- 导出选项（v1.15.1）：是否脱敏 ----
        # 每次导出都问：脱敏与否决定片子还能不能对回患者，选错收不回来，
        # 不适合静默沿用上次设置（勾选状态本身有记忆，不用反复设）。
        try:
            dlg = ExportOptionsDialog(kind, len(series_list), self)
        except Exception:          # noqa: BLE001
            dlg = None
        desensitize = False
        mask_corners = False
        if dlg is not None:
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return
            desensitize = dlg.desensitize
            mask_corners = dlg.mask_corners

        anon_map = {}

        def _patient_key(p):
            return "%s|%s|%s" % (p.get("pat_id", ""), p.get("pat_access", ""),
                                 p.get("pat_name", ""))

        def _anon_of(p):
            """患者 → (PatientName, PatientID) 假名，按出现顺序编号。"""
            key = _patient_key(p)
            if key not in anon_map:
                n = len(anon_map) + 1
                anon_map[key] = ("ANON-%03d" % n, "ANON%03d" % n)
            return anon_map[key]

        # 导出根目录名：脱敏时**绝不能**带患者姓名（目录名本身就是身份信息）
        if desensitize:
            pats = [p for p in pat_by_series.values() if p]
            keys = {_patient_key(p) for p in pats}
            if len(keys) == 1:
                root_name = _anon_of(pats[0])[0]
            else:
                root_name = "匿名导出_%s" % QtCore.QDateTime.currentDateTime() \
                    .toString("yyyyMMdd_HHmm")
        else:
            root_name = self._export_patient_name(items, series_list)
        root = export_utils.unique_dir(export_utils.desktop_dir(), root_name)

        ok_series, failed = [], []
        total_files = 0
        # 整个导出共用一份 UID 映射：同一序列各层必须映射到同一个新 Study/
        # Series UID，否则导入 PACS 后一层一个序列。
        uid_map = {}
        seq_total = len(series_list)

        for seq_idx, series in enumerate(series_list, 1):
            pat = pat_by_series.get(_series_key(series)) or {}
            # 脱敏时**目录名也不能带身份信息**：SeriesDescription 里塞患者姓名
            # 的设备并不少见（"头颅_张三"），只改 DICOM 标签等于白脱敏。
            if desensitize:
                label = self._desensitized_series_label(series, pat, seq_idx)
            else:
                label = self._export_series_label(series)
            sub = export_utils.unique_dir(root, label)
            if desensitize:
                loading_ui.attach_busy(
                    self._patient_tree, "正在脱敏导出...",
                    sub="%d / %d · %s" % (seq_idx, seq_total, label))
                QtCore.QCoreApplication.processEvents()

            def _on_prog(done, total, _idx=seq_idx, _name=self._export_series_label(series)):
                # 每 15 层刷新一次即可：逐层 processEvents 会让导出慢好几倍
                if done % 15 and done != total:
                    return
                try:
                    loading_ui.attach_busy(
                        self._patient_tree, "正在脱敏导出...",
                        sub="%d / %d · %s（%d/%d）"
                            % (_idx, seq_total, _name, done, total))
                    QtCore.QCoreApplication.processEvents()
                except Exception:      # noqa: BLE001
                    pass

            try:
                if kind == "dicom":
                    if not export_utils.is_dicom_series(series):
                        raise RuntimeError("该序列不是 DICOM 文件")
                    if desensitize:
                        anon_name, anon_id = _anon_of(pat)
                        n = export_utils.export_series_dicom_anonymized(
                            series, sub, anon_name=anon_name, anon_id=anon_id,
                            uid_map=uid_map, on_progress=_on_prog)
                    else:
                        n = export_utils.export_series_dicom(series, sub)
                    if n == 0:
                        raise RuntimeError("序列目录内没有可复制的文件")
                else:
                    # 窗宽窗位与灰度极性都必须复刻显示端，否则「看到脑窗、
                    # 导出骨窗」「看到正片、导出负片」（v1.18.1 修的真实故障）。
                    vw, vl, vinv = self._view_window_for_series(series)
                    n = export_utils.export_series_images(
                        series, sub, fmt=kind, mask_corners=mask_corners,
                        window=vw, level=vl, invert=vinv)
                    if n == 0:
                        raise RuntimeError("序列内没有可读的图片帧")
                ok_series.append(series)
                total_files += n
            except Exception as exc:   # noqa: BLE001
                failed.append("%s：%s" % (self._export_series_label(series), exc))

        loading_ui.detach_busy(self._patient_tree)

        self._last_export_dir = root
        tag = "（已脱敏）" if desensitize else ""
        extra = ""
        if desensitize and mask_corners:
            extra = "\n\n图像四角已涂黑，遮盖设备烧录的文字。"
        if not failed:
            QtWidgets.QMessageBox.information(
                self, "导出完成",
                "已导出 %d 个序列、共 %d 个文件%s到桌面：\n%s%s"
                % (len(ok_series), total_files, tag, root, extra))
            return
        # 部分成功也要说清楚：成功多少、失败哪些、为什么
        QtWidgets.QMessageBox.warning(
            self, "导出部分完成",
            "已导出 %d 个序列、%d 个文件%s到：\n%s\n\n"
            "以下 %d 个序列失败：\n%s"
            % (len(ok_series), total_files, tag, root, len(failed),
               "\n".join(failed[:10])))

    def _patient_of_node(self, item):
        """节点 → 所属患者字典（序列节点向上回溯父节点）。

        找不到时返回空 dict：脱敏假名会退化成 ANON-001，不会崩。
        """
        node = item
        for _ in range(4):
            if node is None:
                break
            try:
                data = node.data(0, QtCore.Qt.UserRole)
            except Exception:      # noqa: BLE001
                break
            if isinstance(data, dict) and data.get("type") == "patient":
                return data.get("data") or {}
            node = node.parent()
        return {}

    def _collect_series_for_export(self, item):
        """节点 → 序列字典列表（患者节点展开全部子序列）。"""
        try:
            data = item.data(0, QtCore.Qt.UserRole)
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        if data.get("type") == "series":
            series = data.get("data") or {}
            return [series] if series.get("folder") else []
        if data.get("type") == "patient":
            patient = data.get("data") or {}
            return [s for s in (patient.get("series") or [])
                    if s.get("folder")]
        return []

    def _export_patient_name(self, items, series_list):
        """导出目录名 = 患者姓名（用户明确要求）。

        必须**回溯父节点**：右键序列行时用户只点了序列，患者名挂在父节点上；
        序列字典里的 `patient_name` 只有走双击/拖放路径才回填，
        直接拿会退化成「未命名患者」目录。
        """
        for it in (items or []):
            node = it
            # 向上最多几层找患者节点（series → patient，兼容未来再嵌一层）
            for _ in range(4):
                if node is None:
                    break
                try:
                    data = node.data(0, QtCore.Qt.UserRole)
                except Exception:
                    break
                if isinstance(data, dict) and data.get("type") == "patient":
                    patient = data.get("data") or {}
                    name = patient.get("pat_name")
                    if name:
                        return export_utils.sanitize_filename(
                            name, default="未命名患者")
                node = node.parent()
        for series in series_list:
            name = series.get("patient_name")
            if name:
                return export_utils.sanitize_filename(name, default="未命名患者")
        return "未命名患者"

    def _export_series_label(self, series):
        """导出用的序列目录名：优先 SeriesDescription，其次部位/序号。"""
        desc = (series.get("series_description") or "").strip()
        part = (series.get("body_part") or "").strip()
        if desc and part and desc != part:
            return export_utils.sanitize_filename(
                "%s_%s" % (part, desc), default="序列")
        if desc or part:
            return export_utils.sanitize_filename(desc or part, default="序列")
        uid = str(series.get("series_uid") or "").strip()
        if uid:
            return export_utils.sanitize_filename(uid[-16:], default="序列")
        return "序列"

    def _desensitized_series_label(self, series, patient, idx):
        """脱敏导出用的序列目录名：抹掉患者姓名 / ID / 原 UID，抹空则用序号。

        为什么必须处理：实测不少设备的 SeriesDescription 里直接带患者姓名
        （"头颅_张三"），只改 DICOM 标签、不改目录名等于白脱敏。
        另外 `_export_series_label` 在没有描述时会退化成**原
        SeriesInstanceUID 的尾号**，那是个能回原 PACS 反查身份的标识，
        脱敏时不能留在目录名里。
        """
        label = self._export_series_label(series)
        for token in (patient.get("pat_name"), patient.get("pat_id"),
                      patient.get("pat_access")):
            token = str(token or "").strip()
            if token and token in label:
                label = label.replace(token, "")
        old_uid = str(series.get("series_uid") or "")
        if old_uid and (old_uid in label
                        or (len(old_uid) > 16 and old_uid[-16:] in label)):
            label = ""
        label = label.strip(" _-·|,，")
        if not label:
            label = "序列%02d" % idx
        return label

    def _open_last_export_dir(self):
        path = getattr(self, "_last_export_dir", "")
        if not path or not os.path.isdir(path):
            QtWidgets.QMessageBox.information(
                self, "暂无导出目录", "还没有导出过，请先执行一次导出。")
            return
        try:
            os.startfile(path)       # noqa: S606  (Windows 专用)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self, "打开目录失败", "无法打开目录：\n%s\n\n%s" % (path, exc))

    def _request_series_thumbnail(self, key: str, folder: str, file_uids: list,
                                  item, modality=None):
        """为序列项请求缩略图：命中缓存直接设置，否则交给后台线程生成"""
        items = self._thumb_items.setdefault(key, [])
        if item is not None and id(item) not in [id(x) for x in items]:
            items.append(item)

        if key in self._thumb_cache:
            pm = self._thumb_cache.get(key)
            if item is not None and isinstance(pm, QtGui.QPixmap) and not pm.isNull():
                item.setData(0, QtCore.Qt.DecorationRole, pm)
            return

        if key in self._thumb_pending:
            return
        self._thumb_pending.add(key)

        self._thumb_worker.submit(key, folder, file_uids,
                                  SeriesThumbDelegate.THUMB_SIZE, modality)

    # ---------------- 患者行点击：只展开序列（不加载） ----------------

    def _is_patient_item(self, item) -> bool:
        data = item.data(0, QtCore.Qt.UserRole) if item is not None else None
        return isinstance(data, dict) and data.get("type") == "patient"

    def _expand_patient_item(self, item, expand: bool = True):
        """展开/收起患者节点，并为其下的序列申请缩略图"""
        if item is None or not self._is_patient_item(item):
            return
        item.setExpanded(bool(expand))
        if expand:
            self._ensure_series_thumbnails(item)

    def _ensure_series_thumbnails(self, patient_item):
        """为某位患者的所有序列补发缩略图请求（命中缓存则直接回填）"""
        if patient_item is None:
            return
        for j in range(patient_item.childCount()):
            child = patient_item.child(j)
            data = child.data(0, QtCore.Qt.UserRole)
            if not isinstance(data, dict) or data.get("type") != "series":
                continue
            series = data.get("data") or {}
            key = f"{series.get('series_uid', '')}|{series.get('folder', '')}"
            self._request_series_thumbnail(key, series.get("folder", ""),
                                           series.get("file_uids", []), child,
                                           series.get("modality"))

    def _on_tree_item_clicked(self, item, _column):
        """单击患者行 → 切换展开；双击的第二下会被忽略，避免「展开→收起」闪动"""
        if not self._is_patient_item(item):
            return
        now = QtCore.QTime.currentTime()
        last_item, last_time = self._last_patient_click
        if (last_item is item and last_time.isValid()
                and last_time.msecsTo(now) < QtWidgets.QApplication.doubleClickInterval()):
            # 这是双击的第二次点击，展开/收起已由第一次点击完成
            self._last_patient_click = (None, QtCore.QTime())
            return
        self._last_patient_click = (item, now)
        self._expand_patient_item(item, not item.isExpanded())

    def _on_patient_expanded(self, item):
        """节点展开时补齐缩略图（覆盖首次加载时尚未生成的情况）"""
        if self._is_patient_item(item):
            self._ensure_series_thumbnails(item)

    def _on_series_thumb_done(self, key: str, image):
        """后台缩略图生成完毕：转 QPixmap 后回填到对应的序列项"""
        self._thumb_pending.discard(key)

        pm = None
        if isinstance(image, QtGui.QImage) and not image.isNull():
            pm = QtGui.QPixmap.fromImage(image)

        # 缓存容量上限，避免长期运行后无谓占用
        if len(self._thumb_cache) > 300:
            self._thumb_cache.clear()
        self._thumb_cache[key] = pm

        if pm is None:
            return

        alive = []
        for item in self._thumb_items.get(key, []):
            try:
                if item.treeWidget() is None:
                    continue
                item.setData(0, QtCore.Qt.DecorationRole, pm)
                alive.append(item)
            except RuntimeError:
                continue
        self._thumb_items[key] = alive

        try:
            self._patient_tree.viewport().update()
        except Exception:
            pass

    def _setup_file_watcher(self):
        scan_dirs = self._get_scan_dirs(self._root_dir)

        self._file_watcher.removePaths(self._file_watcher.directories())
        for scan_dir in scan_dirs:
            if os.path.isdir(scan_dir):
                self._file_watcher.addPath(scan_dir)

    def _on_auto_refresh_check(self):
        if not self._auto_refresh_enabled:
            return
        
        current_time = time.time()
        if current_time - self._last_scan_time < 10:  # 10秒间隔
            return
        
        # 如果已经有刷新线程在运行，跳过
        if self._refresh_worker and self._refresh_worker.isRunning():
            return
        
        scan_dirs = self._get_scan_dirs(self._root_dir)
        
        # 创建并启动后台刷新线程
        self._refresh_worker = RefreshWorker(scan_dirs)
        self._refresh_worker.finished.connect(self._on_refresh_finished)
        self._refresh_worker.error.connect(self._on_refresh_error)
        self._refresh_worker.finished.connect(self._refresh_worker.deleteLater)
        self._refresh_worker.start()

    def _on_refresh_finished(self, patients):
        """后台刷新完成"""
        try:
            new_patients_count = 0
            for patient in patients:
                pat_id = patient.get("pat_id", "")
                pat_access = patient.get("pat_access", "")
                pat_name = patient.get("pat_name", "")
                key = f"{pat_id}|{pat_access}|{pat_name}"
                
                if key not in self._cached_patients:
                    new_patients_count += 1
            
            if new_patients_count > 0:
                # 在主线程中更新UI
                # root_dir 传 None = 扫全部已配置目录（v1.9.9）：
                # 传 self._root_dir 会变成「独占扫描一个根」，
                # 设置里新增的目录永远刷不出来。
                QtCore.QMetaObject.invokeMethod(self, "_load_patient_tree",
                                              QtCore.Qt.QueuedConnection,
                                              QtCore.Q_ARG(str, None),
                                              QtCore.Q_ARG(bool, True),
                                              QtCore.Q_ARG(bool, False))
                
            self._last_scan_time = time.time()
        except Exception:
            pass

    def _on_refresh_error(self, error):
        """后台刷新错误"""
        pass  # 静默处理错误，避免打扰用户

    def _on_series_double_clicked(self, item, _column):
        item_data = item.data(0, QtCore.Qt.UserRole)
        if not item_data:
            return

        if isinstance(item_data, dict) and item_data.get("type") == "patient":
            # 双击患者姓名只展开序列列表（缩略图），不再自动加载序列。
            # 原先会自动加载第一个序列，几百层的数据要等好几秒才有反应，
            # 而用户此时往往只是想浏览有哪些序列。加载改为双击序列卡片触发。
            self._expand_patient_item(item, True)
            return

        if isinstance(item_data, dict) and item_data.get("type") == "series":
            # 患者字段的补全（姓名/性别/检查号/检查日期）与拖放加载共用
            # _series_payload_from_item，两条路径必须一致
            series = _series_payload_from_item(item)
        else:
            series = item_data

        if not series:
            return

        self._load_series_payload(series)

    def _load_series_payload(self, series):
        """按序列字典加载（双击与拖放共用同一条路径）"""
        if not series:
            return
        folder = series.get("folder")
        file_uids = series.get("file_uids", [])
        self._current_body_part = series.get("body_part", "")
        if not folder:
            return
        if is_dicom_dir(folder):
            self._load_series(folder, file_uids, series_data=series)
        elif is_image_only_dir(folder) or series.get("is_image_group"):
            # 纯图片文件夹（PACS 导出的 PNG/JPG）：整夹当图片序列打开，
            # 与 CT「一个文件夹 = 一个序列」同口径（v1.9.8）
            self._load_series(folder, kind="image", series_data=series)
        else:
            QtWidgets.QMessageBox.warning(self, "提示", "序列目录中未检测到DICOM文件")

    def _all_temp_folders(self) -> list:
        """收集所有窗口持有的暂存目录（去重，不含已删除的）

        多窗下每个窗口的 `DicomLoadWorker` 可能各自建了暂存目录
        （MR 强制暂存、目录不纯净时也会暂存）。早先只有一个
        `_last_temp_folder` 槽位，载第二窗时会覆盖并**删掉**第一窗的
        暂存目录 —— 那窗的reader 还指着它，图像会随之失效。
        故改为按窗口记录，关闭时统一清理。
        """
        out = []
        for p in list(getattr(self, "_view_temp_folders", {}).values()):
            if p and p not in out and os.path.isdir(p):
                out.append(p)
        last = getattr(self, "_last_temp_folder", None)
        if last and last not in out and os.path.isdir(last):
            out.append(last)
        return out

    def _on_series_dropped(self, token, view_index=None):
        """阅读区收到拖放：解析载荷并加载到指定窗口

        ``view_index`` 由 StudyViewPanel 按**鼠标落点**算出并已设为活动窗口，
        加载时统一从 ``active_index`` 取；为 None 时回落到活动窗口。
        """
        try:
            series = self._patient_tree.payload(token)
            if not series:
                return
            # 拖过来的序列在树上已有对应节点，顺带把它设为当前选中项，
            # 让「当前序列」的高亮与实际显示的画面保持一致
            self._select_series_item_by_payload(series)
            self._load_series_payload(series)
        except Exception as exc:      # noqa: BLE001
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(
                self, "打开失败", "拖放序列时出错：\n%s" % exc)

    def _select_series_item_by_payload(self, series):
        """按 folder + series_uid 在患者树里定位并选中对应序列节点"""
        folder = series.get("folder")
        if not folder:
            return
        for i in range(self._patient_tree.topLevelItemCount()):
            pat = self._patient_tree.topLevelItem(i)
            for j in range(pat.childCount()):
                child = pat.child(j)
                data = child.data(0, QtCore.Qt.UserRole)
                if not (isinstance(data, dict)
                        and data.get("type") == "series"):
                    continue
                s = data.get("data") or {}
                if s.get("folder") == folder and (
                        s.get("series_uid") == series.get("series_uid")):
                    self._patient_tree.setCurrentItem(child)
                    return

    def _cleanup_temp_folder(self, temp_folder: str):
        """清理临时文件夹，如果失败则延迟清理"""
        if not temp_folder or not os.path.exists(temp_folder):
            return
        
        try:
            # 尝试立即清理
            shutil.rmtree(temp_folder)
        except Exception:
            # 如果失败，使用延迟清理
            import threading
            import time
            
            def delayed_cleanup(folder, retries=5, delay=1.0):
                for _ in range(retries):
                    time.sleep(delay)
                    try:
                        if os.path.exists(folder):
                            shutil.rmtree(folder)
                        break
                    except Exception:
                        continue
            
            # 启动后台线程进行延迟清理
            cleanup_thread = threading.Thread(
                target=delayed_cleanup,
                args=(temp_folder,),
                daemon=True
            )
            cleanup_thread.start()

    def _build_dicom_info(self, series_data, reader, folder):
        """构建二维/三维叠加显示所需的 DICOM 信息"""
        info = {
            'patient_name': 'Unknown',
            'patient_id': '-',
            'series_desc': 'Series',
            'modality': 'CT',
            'protocol_name': '',
            'slice_thickness': None,
            'pixel_spacing_str': '-',
            # 注意：vtkDICOMImageReader 在读取时已应用 Modality LUT，
            # 所以 _volume_array 里已经是 HU；这两个系数仅供记录/诊断，
            # 测量时【不要再】拿它们再换算一次（否则骨密度会被算成负数）。
            'rescale_slope': 1.0,
            'rescale_intercept': 0.0,
        }
        if series_data:
            info['patient_name'] = series_data.get('patient_name') or 'Unknown'
            info['patient_id'] = series_data.get('patient_id') or '-'
            info['series_desc'] = (series_data.get('series_description')
                                   or series_data.get('series_desc') or 'Series')
            info['modality'] = series_data.get('modality') or 'CT'
            thickness = series_data.get('slice_thickness')
            if thickness:
                info['slice_thickness'] = thickness

        # 从 DICOM 文件头补充精确信息（像素间距、层厚、序列描述）
        try:
            files = [f for f in sorted(os.listdir(folder))
                     if f.lower().endswith('.dcm')] if folder and os.path.isdir(folder) else []
            if files:
                ds = pydicom.dcmread(os.path.join(folder, files[0]),
                                     stop_before_pixels=True, force=True)
                # 模态以 DICOM 标签为准（序列字典里的可能缺失或靠目录名猜的）
                m = str(getattr(ds, 'Modality', '') or '').strip().upper()
                if m:
                    info['modality'] = m
                # MR 协议名用于判定序列类型（T1/T2/FLAIR/DWI/ADC/MRA）→ 选窗值预设
                proto = _fix_gbk_text(getattr(ds, 'ProtocolName', '') or '')
                if proto:
                    info['protocol_name'] = proto
                ps = getattr(ds, 'PixelSpacing', None)
                if ps is not None and len(ps) >= 2 and self._plausible_edge(float(ps[0]), float(ps[1])):
                    info['pixel_spacing_str'] = f"{float(ps[0]):.2f}×{float(ps[1]):.2f}mm"
                elif getattr(reader, 'GetPixelSpacing', None) is not None:
                    sp = reader.GetPixelSpacing()
                    if sp and self._plausible_edge(float(sp[0]), float(sp[1])):
                        info['pixel_spacing_str'] = f"{float(sp[0]):.2f}×{float(sp[1]):.2f}mm"
                st = getattr(ds, 'SliceThickness', None)
                # 层厚合理性校验：CT/MR 层厚通常在 0.1~20mm 之间；
                # 部分设备（如 Scout 定位像）会写入数百 mm 的脏数据，需过滤。
                if st and not info['slice_thickness'] and self._plausible_thickness(float(st)):
                    info['slice_thickness'] = float(st)
                sd = getattr(ds, 'SeriesDescription', None)
                if sd and info['series_desc'] in (None, '', 'Series'):
                    info['series_desc'] = str(sd)

                # 记录 Rescale 系数（仅供参考；vtkDICOMImageReader 已在读取时
                # 应用过 Modality LUT，体数据本身已是 HU，测量不再二次换算）。
                slope = _dicom_first_number(getattr(ds, 'RescaleSlope', None))
                intercept = _dicom_first_number(getattr(ds, 'RescaleIntercept', None))
                if slope:
                    info['rescale_slope'] = float(slope)
                if intercept is not None:
                    info['rescale_intercept'] = float(intercept)
        except Exception:
            pass

        return info

    @staticmethod
    def _plausible_thickness(value: float) -> bool:
        """层厚合理性校验：单层厚度应在 0.1~20mm 之间"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        return 0.1 <= v <= 20.0

    @staticmethod
    def _plausible_edge(px: float, py: float) -> bool:
        """像素间距合理性校验：医用影像单像素边长通常在 0.05~5mm 之间"""
        try:
            a, b = float(px), float(py)
        except (TypeError, ValueError):
            return False
        return 0.05 <= a <= 5.0 and 0.05 <= b <= 5.0

    @staticmethod
    def _plausible_window(window, level) -> bool:
        """窗宽窗位合理性校验：设备写入的 DICOM 窗值可直接用于显示"""
        try:
            w = float(window)
            c = float(level)
        except (TypeError, ValueError):
            return False
        return 1.0 <= w <= 6000.0 and -1500.0 <= c <= 1500.0

    @staticmethod
    def _fallback_window_level(is_lung: bool) -> tuple:
        """设备未提供可用窗值时的兜底方案"""
        if is_lung:
            return (1500.0, -600.0)
        return (400.0, 40.0)

    @staticmethod
    def _xr_default_window_level() -> tuple:
        """X 线兜底窗值：覆盖 16bit 满量程，保证一定不是黑屏"""
        return (65535.0, 32767.0)

    @staticmethod
    def _xr_window_level(reader, invert=False) -> tuple:
        """按像素分布推算 X 线的窗宽窗位。

        X 线是投影灰度图，DICOM 里写入的 WindowCenter/WindowWidth 量纲往往
        与 CT 的 HU 不可比；直接套 CT 的合理性区间会被误判为异常。
        取 1%~99% 分位作为显示区间，既避开极值噪点又保留足够对比度。
        返回 ``(窗宽, 窗位)``；失败返回 None。

        ``invert=True``（MONOCHROME1）时把区间对 [min,max] 中点镜像，得到的
        窗值是反相后数据的窗值——必须与 Dicom2DReader._invert_polarity 用
        同一套换算，否则窗值与数据不同域，画面会偏亮或发黑。
        """
        try:
            from vtkmodules.util.numpy_support import vtk_to_numpy
            reader.Update()
            out = reader.GetOutput()
            arr = vtk_to_numpy(out.GetPointData().GetScalars())
            if arr is None or arr.size == 0:
                return None
            sample = arr[::max(1, arr.size // 200000)]     # 大图抽样，够用且快
            sample = sample.astype(np.float64)
            lo, hi = np.percentile(sample, [1.0, 99.0])
            if not np.isfinite(lo) or not np.isfinite(hi):
                return None
            if hi - lo < 1.0:
                lo, hi = float(sample.min()), float(sample.max())
            if hi - lo < 1.0:
                return None
            if invert:
                # 镜像基准必须与 Dicom2DReader._invert_polarity 完全一致：
                # 那边用的是**整幅**的 min+max，这里若用抽样 min/max，
                # 抽样漏掉极值时镜像区间会整体偏移 → 画面一端被裁掉。
                total = float(arr.min()) + float(arr.max())
                lo, hi = total - hi, total - lo
            return (float(hi - lo), float((hi + lo) / 2.0))
        except Exception as exc:          # noqa: BLE001
            print("推算 X 线窗值失败: %s" % exc)
            return None

    @staticmethod
    def _build_slice_positions(reader):
        """从 DICOM 的 ImagePositionPatient 推算每层的物理位置（mm）。

        由于 vtkDICOMImageReader 输出的层顺序与文件名顺序可能不一致，
        这里统一按文件名的数值前缀/名称排序重建层位置序列；
        若无法解析则返回 None，由界面按 origin + index * spacing 兜底。
        """
        try:
            spacing_z = float(reader.GetOutput().GetSpacing()[2])
        except Exception:
            spacing_z = 0.0
        try:
            origin_z = float(reader.GetOutput().GetOrigin()[2])
        except Exception:
            origin_z = 0.0
        try:
            directory = reader.GetDirectoryName()
        except Exception:
            directory = None
        if not directory or not os.path.isdir(directory):
            return None

        positions = []
        try:
            names = [f for f in os.listdir(directory) if f.lower().endswith('.dcm')]
            def _sort_key(name):
                m = re.match(r'^(\d+)', name)
                if m:
                    return (0, int(m.group(1)), name)
                return (1, 0, name)
            names.sort(key=_sort_key)
            for name in names:
                try:
                    ds = pydicom.dcmread(os.path.join(directory, name),
                                         stop_before_pixels=True, force=True)
                    ipp = getattr(ds, 'ImagePositionPatient', None)
                    if ipp is not None and len(ipp) >= 3:
                        positions.append(float(ipp[2]))
                except Exception:
                    continue
        except Exception:
            return None

        if len(positions) < 2:
            return None
        return positions

    def _load_series(self, folder: str, file_uids: list = None, series_data: dict = None,
                     kind: str = "dicom"):
        """加载序列（异步）

        kind="dicom"（默认）走 DICOM 读取器；kind="image" 走 ImageSeriesReader，
        用于「PACS 只导出了 JPG/PNG」的目录（见 ImageSeriesReader 的说明）。
        """
        # 暂停自动刷新，避免干扰三维重建
        self._auto_refresh_enabled = False

        # **先确定目标窗口**：多窗下往副窗载序列时，只清那一个窗，
        # 其余窗口已载入的序列必须原样保留（否则表现为「载到 B 窗后
        # A 窗的序列消失了」）。落点由 dropEvent / 点选决定。
        _target = self._study_panel.active_index

        if _target == 0:
            # 主窗口：全局清理（切序列要作废三维管线与分割掩膜）
            self._study_panel.clear_all()
            self._study_panel.set_segmentation_mask(None, None, None)
            self._last_segmentation_mask = None
            if hasattr(self, "_ab_seg_button"):
                self._ab_seg_button.setEnabled(False)
            # 清理主窗口上一次的临时文件夹（**不动其它窗的**）
            if getattr(self, '_last_temp_folder', None):
                self._cleanup_temp_folder(self._last_temp_folder)
                self._last_temp_folder = None
            self._view_temp_folders.pop(0, None)
        else:
            # 副窗：只清该窗，三维/分割/其他窗都不动。
            # 连带清掉**该窗自己的**旧暂存目录（不能碰别的窗的）。
            _old_tmp = getattr(self, "_view_temp_folders", {}).get(_target)
            self._study_panel.clear_all(_target)
            if _old_tmp:
                try:
                    self._cleanup_temp_folder(_old_tmp)
                except Exception:
                    pass
                self._view_temp_folders.pop(_target, None)

        # **先清掉上一序列的模态标记**：切换序列后，任何依赖「当前是不是
        # MR」的判断（_current_iso_value 等）都必须立刻反映新序列，
        # 否则会把上一个序列的阈值语义带到下一个。
        self._current_series_data = {}
        self._pending_mr_vessel = int(self._mr_vessel_slider.value())

        self._path_label.setText(f"正在加载: {folder}")
        QtCore.QCoreApplication.processEvents()

        progress = loading_ui.TechBusyDialog("正在加载DICOM数据...", "取消", self)
        progress.setWindowTitle("请稍候")
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(300)
        progress.setRange(0, 100)
        progress.setValue(10)
        # 阅读区同时叠一层内嵌遮罩：进度框是独立浮层，遮罩让「画面正在换」
        # 这件事在主界面上直接可见（loading_ui）
        loading_ui.attach_busy(self._study_panel, "正在加载影像数据...",
                               sub="READING DICOM SERIES", dim=0.70)

        # 「加载患者目录」/「打开影像文件→目录」这两条入口调 _load_series 时
        # 没有 series_data（签名的默认值就是 None），于是 modality 恒为空，
        # DX/CR/MR 全被当成 CT 去走 vtkDICOMImageReader。实测 PACS 导出的
        # DX（D:\ClearTmp\DicomTmp\02，8 张 2012x2012）就栽在这里：VTK 把
        # 头解析到一半放弃、退回默认 64x64，画面是一小块黑图（v1.9.7）。
        # 这里只在这两条入口（series_data 缺失）时补探一次，序列树路径
        # 本来就带着 modality，不必重复读盘。
        if not series_data or not (series_data or {}).get("modality"):
            try:
                _probe = _analyze_series_from_dicom_fast(folder) or {}
            except Exception as _probe_err:      # noqa: BLE001
                _probe = {}
                print("序列模态探测失败，按原路径加载: %s" % _probe_err)
            if _probe.get("modality") or _probe.get("series_description"):
                series_data = dict(series_data or {})
                for _k in ("modality", "series_description", "body_part",
                           "protocol_name", "image_type", "is_localizer"):
                    if _probe.get(_k) not in (None, ""):
                        series_data.setdefault(_k, _probe[_k])
            else:
                series_data = series_data or {}

        # X 线改走 pydicom 装配的读取器：vtkDICOMImageReader 遇到未写
        # Transfer Syntax UID 的设备文件会解析错位（胸片读成 64x64 空图）。
        is_xray_load = is_xr_modality((series_data or {}).get("modality"))
        is_mr_load = is_mr_modality((series_data or {}).get("modality"))

        # MRI 层序方向规范化：vtkDICOMImageReader 自会按 IPP 降序堆层（实测与文件名
        # 无关），这里统一重排为升序，与 CT 侧既有流程和「从上到下」的阅读
        # 习惯一致；同时矢状面 MR的 IPPz 恒定，只能沿层法向投影排序。
        if is_mr_load and file_uids and len(file_uids) > 1:
            try:
                _geo = analyze_slice_geometry(folder, file_uids)
                if _geo.get("reordered"):
                    file_uids = _geo["ordered_uids"]
            except Exception as geo_error:
                print("MRI 层序分析失败，按原顺序加载: %s" % geo_error)

        # 图片序列：没有任何 DICOM 语义（无 HU、无层厚），也堆不出体数据，
        # 只当多帧图片翻层看。
        is_image_series = (kind == "image")

        # Z 维只有 1 层时是否放行：
        #  - X 线：一幅片子就是一个序列，Z 维必然是 1，恒放行；
        #  - 图片序列：同 DICOM 单帧，恒放行；
        #  - MR  ：**单张序列是合法影像**（实测有 MRA 的单张 MIP 投影图直接
        #    躺在扫描根目录下），张数为 1 时放行；张数 >1 却只读出 1 层
        #    说明数据真的有问题，仍按异常拦截。
        #  - CT  ：单层是数据异常（原本就该拦），不放行。
        n_uids = len(file_uids) if file_uids else 0
        if is_image_series:
            allow_single = True
        elif is_xray_load:
            allow_single = True
        elif is_mr_load:
            allow_single = n_uids <= 1
        else:
            allow_single = False

        if is_image_series:
            reader_cls = ImageSeriesReader
        elif is_xray_load:
            reader_cls = XraySeriesReader
        else:
            reader_cls = vtkDICOMImageReader

        worker = DicomLoadWorker(
            folder, file_uids,
            allow_single_slice=allow_single,
            require_staging=is_mr_load,
            reader_cls=reader_cls,
            parent=self)

        def on_progress(val, text):
            progress.setValue(val)
            progress.setLabelText(text)
            loading_ui.attach_busy(self._study_panel, "正在加载影像数据...",
                                   sub=str(text or ""), dim=0.70)
            QtCore.QCoreApplication.processEvents()

        def on_finished(reader, window_center, window_width, image_count, temp_folder):
            # 记录暂存目录：按窗口分别保存（多窗下各自独立，见
            # _all_temp_folders 的说明），同时保留 _last_temp_folder
            # 供既有关闭逻辑使用。
            self._last_temp_folder = temp_folder
            if _target == 0:
                # 主窗口换了序列 → 清掉主窗口自己的旧暂存目录
                _old = getattr(self, "_view_temp_folders", {}).get(0)
                if _old and _old != temp_folder:
                    try:
                        self._cleanup_temp_folder(_old)
                    except Exception:
                        pass
            if temp_folder:
                self._view_temp_folders[_target] = temp_folder

            # 进度条结束后，提示正在渲染
            progress.setLabelText("正在准备三维渲染...")
            progress.setValue(95)
            loading_ui.attach_busy(self._study_panel, "正在重建三维模型，请稍候…",
                                   sub="准备体渲染数据", dim=0.70)
            QtCore.QCoreApplication.processEvents()

            # 更新UI
            is_xray = is_xr_modality((series_data or {}).get("modality"))
            is_mr = is_mr_modality((series_data or {}).get("modality"))
            # 立刻记下当前序列：后续三维阈值取值（_current_iso_value）
            # 全靠它判断该用 HU 还是信号强度，晚设会让切三维时用错语义。
            self._current_series_data = series_data or {}

            if is_image_series:
                # 图片没有 DICOM 窗值：与「打开单张图片」同法，按像素分布推算
                window_width, window_center = self._image_window_level(reader)
            elif is_xray:
                # X 线（DR/DX）的 DICOM 窗值量纲与 CT 完全不同（曾见 WW6880/WL12943），
                # 直接用 CT 的合理性校验会被判为异常而兜底成 400/40 → 整幅发黑。
                # 这里改为按实际像素分布计算显示区间。
                # 若片子是 MONOCHROME1（负片），二维视图会把灰度反相后再显示，
                # 窗值也必须同步镜像到同一域（见 Dicom2DReader._invert_polarity）。
                mono1 = series_needs_invert(
                    reader, (series_data or {}).get("modality"))
                wl = self._xr_window_level(reader, invert=mono1)
                if wl:
                    window_width, window_center = wl
                elif mono1:
                    # 负片的 DICOM 窗值同样需要镜像，但此时取不到可靠区间，
                    # 直接给满量程（等效于不裁剪），避免因域不一致而发黑
                    window_width, window_center = self._xr_default_window_level()
                elif self._plausible_window(window_width, window_center):
                    window_width = float(window_width)
                    window_center = float(window_center)
                else:
                    window_width, window_center = self._xr_default_window_level()
            elif is_mr:
                # MR 的像素是信号强度而非 HU：不能套 CT 的肺窗/骨窗，也不能用
                # CT 的窗值合理性区间（ADC 序列实测 WC1615/WW3578 会被误判为
                # 异常而兜底成 400/40 → 整幅发黑）。走 MR 专用策略：
                # DICOM 自带窗值 → 序列类型预设 → 像素分位数。
                _kind = mr_series_kind(
                    (series_data or {}).get("series_description") or "",
                    (series_data or {}).get("protocol_name") or "",
                    (series_data or {}).get("body_part") or "")
                _p1, _p99 = mr_percentile_range(folder, file_uids or [])
                window_width, window_center = mr_window_level(
                    window_center, window_width, _kind, _p1, _p99)
                self._current_mr_kind = _kind
            else:
                bp = (getattr(self, "_current_body_part", "") or "").upper()
                is_lung = ("LUNG" in bp) or ("CHEST" in bp) or ("THORAX" in bp)
                # 窗宽窗位优先采用序列自身的默认值（DICOM WindowCenter/WindowWidth），
                # 只有在设备未写入或数值不合理时才按部位兜底，避免加载后出现黑屏
                if self._plausible_window(window_width, window_center):
                    window_width = float(window_width)
                    window_center = float(window_center)
                else:
                    window_width, window_center = self._fallback_window_level(is_lung)

            self._series_info_label.setText(f"窗宽/窗位: {window_width:.0f}/{window_center:.0f}")
            self._study_panel.set_window_level(window_width, window_center)

            # 阈值语义随模态切换：CT 用 HU 骨阈值，MR 用信号强度血管阈值
            if is_mr:
                # MR 三维按血管信号强度重建。阈值自动推荐依赖已就绪的 reader，
                # 所以放在 set_data 之前手动算一次。
                self._apply_mr_vessel_threshold_auto(reader)
                iso_value = int(self._mr_vessel_slider.value())
                progress.setLabelText(f"血管阈值: {iso_value}，正在准备三维...")
                progress.setValue(98)
                QtCore.QCoreApplication.processEvents()
                self._study_panel.set_is_mr(True)
                self._study_panel.set_iso_value(iso_value)
            else:
                # 骨阈值：不再按年龄/儿童模式自动选择，统一采用滑动条当前值（默认 200）
                iso_value = int(self._threshold_slider.value())

                # 显示正在渲染提示
                progress.setLabelText(f"骨阈值: {iso_value}，正在渲染3D图像...")
                progress.setValue(98)
                QtCore.QCoreApplication.processEvents()

                self._threshold_label.setText(f"骨组织阈值: {iso_value}")
                self._study_panel.set_is_mr(False)
                self._study_panel.set_iso_value(iso_value)
                try:
                    # 同步预设下拉选择显示
                    preset_values = [self._threshold_preset_combo.itemData(i) for i in range(self._threshold_preset_combo.count())]
                    if iso_value in preset_values:
                        idx = preset_values.index(iso_value)
                        self._threshold_preset_combo.setCurrentIndex(idx)
                except Exception:
                    pass
            try:
                # 获取 DICOM 信息用于叠加显示
                dicom_info = self._build_dicom_info(series_data, reader, folder)
                slice_positions = self._build_slice_positions(reader)

                # 三维是否可用：X 线是单帧投影，没有体数据；
                # MR 单张序列（如 MRA 的 MIP 投影图）同样只有一幅图像，
                # 堆不出层，同样要禁掉三维入口；
                # 图片序列（PACS 导出的 JPG/PNG）同理，没有 HU 语义。
                if is_image_series or is_xray or (is_mr and image_count < 2):
                    self._study_panel.set_3d_available(False)
                else:
                    self._study_panel.set_3d_available(True)

                # 默认进入二维阅读模式；三维管线在用户切换时按需构建
                self._study_panel.set_mode(StudyViewPanel.MODE_2D)
                # 载到目标窗口：多窗下用拖放落点（或用户点选的窗口），
                # 单窗时恒为 0，与原有行为完全一致。
                self._study_panel.set_data(
                    reader,
                    window_width=window_width,
                    window_center=window_center,
                    dicom_data=dicom_info,
                    slice_positions=slice_positions,
                    view_index=self._study_panel.active_index,
                    series_data=series_data,
                )
                # X 线没有 HU，MR 是信号强度，两者做「CT 值」测量都会误导
                self._study_panel.set_ct_measure_enabled(not (is_xray or is_mr))
                # 上一卷体数据（数百 MB）随旧 reader 释放，显式触发一轮回收
                gc.collect()
            except Exception as view_error:
                QtWidgets.QMessageBox.critical(self, "加载失败", f"视图初始化失败:\n{str(view_error)}")
                self._path_label.setText("加载失败")
                if temp_folder:
                    self._cleanup_temp_folder(temp_folder)
                progress.close()
                # 这条 return 会跳过函数末尾的摘罩 —— 必须在这里补一次，
                # 否则遮罩留在阅读区，盖住后面加载的序列（v1.9.2）
                loading_ui.detach_busy(self._study_panel)
                return
            self._path_label.setText(f"已加载: {folder} ({image_count}张)")
            if temp_folder:
                self._last_temp_folder = temp_folder
            self._current_reader = reader
            self._current_series_folder = folder
            self._current_series_data = series_data or {}

            # 按模态切换右侧工具区：X 线显示 AI 诊断分组、隐藏三维参数；
            # CT 反过来；MR 换成血管信号阈值控件。
            self._apply_modality_ui(is_xray, is_mr)
            if is_xray:
                # 反馈按钮旁的「已登记 N 条」计数（只查 COUNT，很便宜）
                self._refresh_feedback_count()

            # 同一患者常有正位/侧位/多部位多张片子，载入后把当前片子名写进
            # AI 面板，用户点开哪张、AI 就检测哪张，一目了然（v1.7.4）
            if is_xray:
                cur_desc = str((series_data or {}).get("series_description")
                               or (series_data or {}).get("body_part")
                               or "").strip()
                if cur_desc and hasattr(self, "_xray_hint_label"):
                    # 必须与部位提示**合并**而不是覆盖：_apply_modality_ui 刚
                    # 写入的「当前部位：胸部，可用：胸片检测」是用户判断按钮
                    # 为什么置灰的依据，被片子名顶掉就白写了（v1.9.1）。
                    self._xray_hint_label.setText(
                        "%s\n当前片子：%s" % (self._xray_part_hint(), cur_desc))

            if hasattr(self, "_ab_seg_button"):
                self._ab_seg_button.setEnabled(True)
            progress.setValue(100)
            progress.close()
            loading_ui.detach_busy(self._study_panel)
            
            # 三维重建完成后，重新启用自动刷新
            self._auto_refresh_enabled = True

        def on_error(msg):
            QtWidgets.QMessageBox.critical(self, "加载失败", f"加载DICOM数据失败:\n{msg}")
            self._path_label.setText("加载失败")
            progress.close()
            loading_ui.detach_busy(self._study_panel)
            # 加载失败后，重新启用自动刷新
            self._auto_refresh_enabled = True

        def on_canceled():
            self._path_label.setText("已取消加载")
            progress.close()
            loading_ui.detach_busy(self._study_panel)
            # 加载取消后，重新启用自动刷新
            self._auto_refresh_enabled = True

        worker.progress.connect(on_progress)
        worker.finished_success.connect(on_finished)
        worker.error.connect(on_error)
        worker.canceled.connect(on_canceled)

        def do_cancel():
            worker.stop()
        progress.canceled.connect(do_cancel)

        worker.start()


class ThumbnailStrip(QtWidgets.QFrame):
    """底部序列缩略图条，点击可快速跳转到对应切片。"""

    slice_requested = QtCore.pyqtSignal(int)

    THUMB_W = 64
    THUMB_H = 64

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.THUMB_H + 18)
        self.setObjectName("thumbnailStrip")
        theme.bind(self, theme.qss_thumbnail_strip)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(0)

        self._scroll = QtWidgets.QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self._inner = QtWidgets.QWidget()
        self._inner_layout = QtWidgets.QHBoxLayout(self._inner)
        self._inner_layout.setContentsMargins(0, 0, 0, 0)
        self._inner_layout.setSpacing(3)
        self._inner_layout.addStretch(1)
        self._scroll.setWidget(self._inner)
        outer.addWidget(self._scroll)

        self._buttons = []
        self._current_index = -1

    def clear(self):
        while self._inner_layout.count() > 1:
            item = self._inner_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._buttons = []
        self._current_index = -1

    def set_thumbnails(self, pixmaps, labels=None):
        """pixmaps: QPixmap 列表；labels: 可选文本标签列表"""
        self.clear()
        if not pixmaps:
            return
        for idx, pm in enumerate(pixmaps):
            btn = QtWidgets.QToolButton()
            btn.setFixedSize(self.THUMB_W + 6, self.THUMB_H + 14)
            btn.setIcon(QtGui.QIcon(pm))
            btn.setIconSize(QtCore.QSize(self.THUMB_W, self.THUMB_H))
            btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextUnderIcon)
            btn.setText(str(labels[idx]) if labels and idx < len(labels) else str(idx + 1))
            theme.bind(btn, theme.qss_thumbnail_button)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, i=idx: self._on_clicked(i))
            self._inner_layout.insertWidget(self._inner_layout.count() - 1, btn)
            self._buttons.append(btn)

    def _on_clicked(self, index: int):
        self.slice_requested.emit(int(index))

    def set_current_index(self, index: int, scroll_to: bool = True):
        if index == self._current_index:
            return
        if 0 <= self._current_index < len(self._buttons):
            self._buttons[self._current_index].setChecked(False)
        self._current_index = int(index)
        if 0 <= self._current_index < len(self._buttons):
            btn = self._buttons[self._current_index]
            btn.setChecked(True)
            if scroll_to:
                self._scroll.ensureWidgetVisible(btn, 60, 0)


# ---------------------------------------------------------------------------
# X 线灰度极性（MONOCHROME1 反相）
# ---------------------------------------------------------------------------

def _reader_first_dicom_file(reader):
    """从读取器反查一个真实 DICOM 文件路径（可能为 None）

    优先用 ``GetFiles()``：XraySeriesReader 这类自装配读取器只有显式文件列表，
    没有 VTK 的 ``GetFileNames()``。少了这一步它就会退到「扫目录取第一个 .dcm」，
    而一个目录里常躺着同一位患者的正位/侧位/多个部位多张片子 —— 反查到的可能
    是**另一张片子**，MONOCHROME1 极性判断、部位判定都会跟着错。
    """
    try:
        files = reader.GetFiles()
        if files:
            path = str(files[0])
            if os.path.isfile(path):
                return path
    except Exception:       # noqa: BLE001
        pass
    try:
        names = reader.GetFileNames()
        if names is not None and names.GetNumberOfValues() > 0:
            path = str(names.GetValue(0))
            if os.path.isfile(path):
                return path
    except Exception:
        pass
    try:
        folder = reader.GetDirectoryName()
    except Exception:
        folder = None
    if folder and os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith((".dcm", ".dicom")):
                return os.path.join(folder, f)
    return None


def series_needs_invert(reader, modality=None) -> bool:
    """该序列显示前是否需要反相（X 线的 MONOCHROME1 片子）。

    DICOM 里 ``PhotometricInterpretation = MONOCHROME1`` 表示「像素值越大越暗」，
    即文件里存的本身就是负片，阅片软件必须先反相才是正常的「骨头亮」画面。
    vtkDICOMImageReader **不处理** PhotometricInterpretation，直接把原始灰度
    送进渲染管线，于是 CT3D 二维视图显示 DR 时整幅是负片。

    实测（渲染图 vs xray 侧正确渲染的相关系数）：反相前 -0.9998（完全负片），
    说明这不是对比度问题而是极性反了。CT 永远是 MONOCHROME2，不受影响。
    """
    if not is_xr_modality(modality):
        return False
    path = _reader_first_dicom_file(reader)
    if not path:
        return False
    try:
        import pydicom
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
        return str(getattr(ds, "PhotometricInterpretation", "")
                   ).strip().upper() == "MONOCHROME1"
    except Exception as exc:              # noqa: BLE001
        print("检测 X 线灰度极性失败: %s" % exc)
        return False


class XraySeriesReader(object):
    """X 线序列读取器：用 pydicom 取像素，自行装配 vtkImageData。

    为什么不能沿用 vtkDICOMImageReader：
      部分国产 DR（含部分胸片）导出的 DICOM **没有写 Transfer Syntax UID**
      元素，VTK 的 DICOMParser 拿不到编码信息后按默认方式解析，会读到错位的
      Rows/Columns —— 实测某院胸片（Rows=1112 / Columns=1252）被读成
      64x64 的 uint8 空图：画面缩成一小块、几乎全黑（v1.7.2 修复）。
      同一个文件 pydicom 能正确解析，因此对 X 线（单帧、无三维重建）
      改由 pydicom 取像素、这里装配 vtkImageData，既绕开该缺陷，也顺带
      屏蔽了目录里 'ko' / '*.xml' 等非 DICOM 文件对目录扫描的干扰。

    对外只暴露 VTK reader 那一小撮调用面（Update / GetOutput /
    GetSpacing / GetOrigin / GetPixelSpacing / GetDirectoryName），
    上层 DicomLoadWorker 与 Dicom2DReader 无需区分两者。
    """

    def __init__(self):
        self._folder = None
        self._image = None
        self._pixel_spacing = (1.0, 1.0)
        self._files = []
        self._explicit = None

    # ---- VTK reader 兼容接口 ----
    def SetDirectoryName(self, folder):
        self._folder = folder
        self._image = None

    def SetFiles(self, files):
        """显式指定要读的文件（「打开」单个 DICOM 文件时用）。

        工具栏打开单个 .dcm 时**不能**让读取器去扫整个目录：目录里常同时躺着
        正位/侧位/多部位的多张片子，扫描会读到别的一张。这里直接锁定文件。
        """
        self._explicit = [str(f) for f in (files or [])]
        self._folder = (os.path.dirname(self._explicit[0])
                        if self._explicit else None)
        self._image = None

    def GetFiles(self):
        """实际参与读取的文件绝对路径列表（_reader_first_dicom_file 用）"""
        return list(self._files)

    def GetDirectoryName(self):
        return self._folder

    def GetOutput(self):
        return self._image

    def GetSpacing(self):
        return (self._pixel_spacing[1], self._pixel_spacing[0], 1.0)

    def GetOrigin(self):
        return (0.0, 0.0, 0.0)

    def GetExtent(self):
        img = self._image
        if img is None:
            return (0, 0, 0, 0, 0, 0)
        return img.GetExtent()

    def GetPixelSpacing(self):
        return list(self._pixel_spacing)

    def GetScalarType(self):
        img = self._image
        if img is None:
            return None
        return img.GetScalarType()

    def Update(self):
        if self._image is not None:
            return
        if self._explicit is not None:
            # 显式文件列表（单文件打开）：路径原样使用，不扫目录
            self._files = [f for f in self._explicit if os.path.isfile(f)]
        else:
            folder = self._folder
            if not folder or not os.path.isdir(folder):
                return
            try:
                names = sorted(f for f in os.listdir(folder)
                               if f.lower().endswith(".dcm"))
            except Exception:      # noqa: BLE001
                return
            self._files = [os.path.join(folder, n) for n in names]
        if not self._files:
            return
        try:
            import pydicom
            # 目录里每个 .dcm 都是**一层**（v1.9.8）。此前只取第一张的帧数当
            # nf，目录里其余文件被整个丢掉 —— 实测 DICOMTmp\\07\\Untitled-0
            # 5 张 DX（2876x1836 / 2880x2040 / 2772x1864 / 2640x1112 / 2912x972）
            # 只出 1 层，表现为「只显示第一张图片」。各片尺寸本就不同
            # （正位/侧位/斜位混放），故先按 _pick_image_grid 定公共网格再逐层写。
            meta = []                       # [(帧列表, rows, cols), ...]
            for _f in self._files:
                ds = pydicom.dcmread(_f)
                arr = ds.pixel_array
                # 多帧影像摊平成若干层（X 线基本是单帧，留作兜底）
                _fr = [arr[i] for i in range(arr.shape[0])] \
                    if arr.ndim == 3 else [arr]
                meta.append((_fr, int(ds.Rows), int(ds.Columns)))
            grid = _pick_image_grid([(m[1], m[2]) for m in meta]) \
                or meta[0][1:3]
            rows, cols = int(grid[0]), int(grid[1])
            # 层数 = 所有文件的帧数之和（单文件时退化为「该文件的帧数」）
            frames = []
            for _fr, _r, _c in meta:
                for _a in _fr:
                    if _a.ndim > 2:          # 彩色只取第一通道，保底
                        _a = _a[..., 0]
                    frames.append(_a)
            nf = max(1, len(frames))
        except Exception as exc:      # noqa: BLE001
            print("X 线读取失败: %s" % exc)
            return

        # PixelSpacing 是 [行间距, 列间距]；VTK 的 X 对应列、Y 对应行
        try:
            # 只取 PixelSpacing 一个标签，别把整张像素解码出来
            # （X 线单张可达 2000×3000，解码一次几百毫秒纯属浪费）
            ps = pydicom.dcmread(self._files[0], stop_before_pixels=True,
                                 force=True).get("PixelSpacing", None)
        except Exception:      # noqa: BLE001
            ps = None
        try:
            row_sp, col_sp = float(ps[0]), float(ps[1])
        except Exception:      # noqa: BLE001
            row_sp, col_sp = 1.0, 1.0
        if not (0.001 < row_sp < 100.0) or not (0.001 < col_sp < 100.0):
            row_sp = col_sp = 1.0
        self._pixel_spacing = (row_sp, col_sp)

        try:
            from vtkmodules.vtkCommonDataModel import vtkImageData
            from vtkmodules.util.numpy_support import vtk_to_numpy
            from vtkmodules.vtkCommonCore import (
                VTK_CHAR, VTK_UNSIGNED_CHAR, VTK_SHORT, VTK_UNSIGNED_SHORT,
                VTK_INT, VTK_UNSIGNED_INT, VTK_LONG, VTK_UNSIGNED_LONG,
                VTK_FLOAT, VTK_DOUBLE)
            # AllocateScalars 只认 VTK 类型常量，直接传 numpy dtype 会报
            # "AllocateScalars argument %Id: %V" 并整幅加载失败
            np_to_vtk = {
                "int8": VTK_CHAR, "uint8": VTK_UNSIGNED_CHAR,
                "int16": VTK_SHORT, "uint16": VTK_UNSIGNED_SHORT,
                "int32": VTK_INT, "uint32": VTK_UNSIGNED_INT,
                "int64": VTK_LONG, "uint64": VTK_UNSIGNED_LONG,
                "float32": VTK_FLOAT, "float64": VTK_DOUBLE,
            }
            dtype_name = str(frames[0].dtype)
            data_type = np_to_vtk.get(dtype_name)
            if data_type is None:
                dtype_name = "uint16"
                data_type = VTK_UNSIGNED_SHORT
            img = vtkImageData()
            img.SetDimensions(cols, rows, nf)
            img.SetSpacing(col_sp, row_sp, 1.0)
            img.SetOrigin(0.0, 0.0, 0.0)
            img.AllocateScalars(data_type, 1)
            buf = vtk_to_numpy(img.GetPointData().GetScalars())
            buf = buf.reshape(nf, rows, cols)
            for k in range(nf):
                src = frames[k]
                if src.ndim > 2:          # 彩色只取第一通道，保底
                    src = src[..., 0]
                if str(src.dtype) != dtype_name:
                    src = src.astype(dtype_name)
                if src.shape != (rows, cols):
                    # 各片尺寸不同（如 07 的正位/侧位/斜位）：居中补 0 归一，
                    # 绝不放缩；网格由 _pick_image_grid 给定
                    src = _pad_image_to(src, (rows, cols))
                # 行序翻成「下→上」，与 vtkDICOMImageReader 路径保持一致。
                #
                # 约定（务必不要再踩）：两条读取路径产出的 _volume_array 行序
                # 必须**统一为「下→上」**（即 array[0][0] 是图像左下角），
                # 显示方向的翻转由 _build_slice_image / _extract_slice 的
                # np.flipud 全权负责。若这里不翻，X 线就会比 CT 多翻一次
                # → 画面上下颠倒（v1.7.3 修复）。
                #
                # 「下→上」是 VTK 路径的自然结果（vtkDICOMImageReader 输出的
                # 第 0 行即图像底部），实测 corr(vtk, pydicom)=+1.0000 但
                # corr(vtk, flipud(pydicom))=+0.4285，即与 pydicom 原序**反序**；
                # 而 XraySeriesReader 的 buf 若直接塞 pydicom 原序（上→下），
                # 相关系数为 +1.0000 —— 两者若不同序，上层的 flipud 就会
                # 只对一条路径生效（实测曾经导致 DR 上下翻转）。
                buf[k] = np.flipud(src)
            self._image = img
        except Exception as exc:      # noqa: BLE001
            print("X 线图像装配失败: %s" % exc)


class ImageFileReader(object):
    """常见图片（PNG/JPG/BMP/TIFF/WebP）读取器。

    为什么单独做一个类：``vtkDICOMImageReader`` 遇到非 DICOM 文件不是报错而是
    **段错误**（实测 exit 139），所以普通图片绝不能交给它去试；OpenCV 对常见
    格式的解码要稳得多，顺带还能读 16bit PNG/TIFF。

    对外只暴露 VTK reader 那一小撮调用面（Update / GetOutput / GetSpacing /
    GetOrigin / GetExtent / GetScalarType / GetPixelSpacing），上层
    Dicom2DReader 与 _xr_window_level 无需区分它和 vtkDICOMImageReader。

    行序约定与 XraySeriesReader **完全一致**：内部先 np.flipud 再写入，使
    ``_volume_array`` 的第 0 行是图像**底部**（array[0][0] = 左下角）；
    显示方向的翻转由 Dicom2DReader._build_slice_image 全权负责。这里若少翻
    一次，图片就会上下颠倒。
    """

    # VTK 只接受这几种标量类型，其余一律归一到 uint8
    _SUPPORTED_DTYPE = ("uint8", "uint16", "int16", "float32")

    def __init__(self):
        self._path = None
        self._image = None
        self._spacing = (1.0, 1.0, 1.0)
        self._components = 1
        self._is_color = False

    # ---- 构造 ----
    def SetFileName(self, path):
        self._path = str(path) if path else None
        self._image = None

    def GetFileName(self):
        return self._path

    def GetDirectoryName(self):
        """故意返回 None。

        ``series_needs_invert()`` → ``_reader_first_dicom_file()`` 会拿目录去翻
        .dcm；图片目录里若恰好躺着一张 MONOCHROME1 的片子，就会被误判成负片
        而整幅反相。图片本身没有 DICOM 极性概念，这里必须断掉这条回退链。
        """
        return None

    # ---- VTK reader 兼容接口 ----
    def GetOutput(self):
        return self._image

    def GetSpacing(self):
        return self._spacing

    def GetOrigin(self):
        return (0.0, 0.0, 0.0)

    def GetExtent(self):
        img = self._image
        if img is None:
            return (0, 0, 0, 0, 0, 0)
        return img.GetExtent()

    def GetScalarType(self):
        img = self._image
        return None if img is None else img.GetScalarType()

    def GetPixelSpacing(self):
        return [self._spacing[0], self._spacing[1]]

    def is_color(self) -> bool:
        """是否按彩色显示（3 通道且确实含色）"""
        return bool(self._is_color)

    def Update(self):
        if self._image is not None:
            return
        if not self._path or not os.path.isfile(self._path):
            return
        arr = self._decode(self._path)
        if arr is None:
            return
        self._assemble(arr)

    # ---- 解码 ----
    @staticmethod
    def _decode(path):
        """读成 numpy 数组；彩色保持 (H, W, 3)，灰度 (H, W)。失败返回 None"""
        arr = None
        try:
            import cv2
            # 中文路径下 cv2.imread 会静默返回 None（实测：非 ASCII 路径
            # 必失败）。走 imread_cn：ASCII 走原生，非 ASCII 走 imdecode。
            try:
                import export_utils as _eu
                arr = _eu.imread_cn(path, cv2.IMREAD_UNCHANGED)
            except Exception:                 # noqa: BLE001
                arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if arr is not None and arr.ndim == 3:
                ch = int(arr.shape[2])
                if ch == 4:                       # BGRA：用白底合成掉 alpha
                    alpha = arr[:, :, 3:4].astype(np.float32) / 255.0
                    bgr = arr[:, :, :3].astype(np.float32)
                    arr = (bgr * alpha + 255.0 * (1.0 - alpha)).astype(np.uint8)
                if arr.ndim == 3 and int(arr.shape[2]) == 3:
                    import cv2 as _cv2                 # BGR → RGB
                    arr = _cv2.cvtColor(arr, _cv2.COLOR_BGR2RGB)
        except Exception:                     # noqa: BLE001
            arr = None
        if arr is None:
            # cv2 读不了的（某些 WebP/TIFF 编码）退回 Pillow 再试一次
            try:
                from PIL import Image
                with Image.open(path) as im:
                    mode = im.mode
                    if mode in ("I;16", "I;16B", "I;16L", "I"):
                        arr = np.array(im, dtype=np.int32).astype(np.uint16)
                    elif mode == "L":
                        arr = np.array(im, dtype=np.uint8)
                    else:
                        arr = np.array(im.convert("RGB"), dtype=np.uint8)
            except Exception:                 # noqa: BLE001
                arr = None
        if arr is None:
            return None
        if arr.ndim == 3 and int(arr.shape[2]) not in (1, 3):
            arr = arr[:, :, 0]
        if arr.ndim == 3 and int(arr.shape[2]) == 1:
            arr = arr[:, :, 0]
        return arr

    def _assemble(self, arr):
        """numpy → vtkImageData（行序翻成「下→上」）"""
        try:
            from vtkmodules.vtkCommonDataModel import vtkImageData
            from vtkmodules.util.numpy_support import vtk_to_numpy
            from vtkmodules.vtkCommonCore import (
                VTK_UNSIGNED_CHAR, VTK_UNSIGNED_SHORT, VTK_SHORT, VTK_FLOAT)

            src = np.asarray(arr)
            if src.ndim not in (2, 3):
                return
            if src.ndim == 3 and int(src.shape[2]) == 3:
                # 三通道全相等 = 灰底彩图，按灰度走单通道管线（省一份显存，
                # 也避免 ImageProperty 的窗值去裁彩色分量）
                if bool(np.array_equal(src[:, :, 0], src[:, :, 1])) and \
                        bool(np.array_equal(src[:, :, 1], src[:, :, 2])):
                    src = src[:, :, 0]
                    nc, is_color = 1, False
                else:
                    nc, is_color = 3, True
            else:
                nc, is_color = 1, False

            if not is_color:
                dtype_name = str(src.dtype)
                if dtype_name not in self._SUPPORTED_DTYPE:
                    if src.dtype.kind == "f":
                        src = src.astype(np.float32)
                    elif src.dtype.kind in ("i", "u") and src.dtype.itemsize <= 2:
                        src = src.astype(np.uint16)
                    else:
                        src = src.astype(np.uint8)
            else:
                src = src.astype(np.uint8)

            vtk_type = {
                "uint8": VTK_UNSIGNED_CHAR, "uint16": VTK_UNSIGNED_SHORT,
                "int16": VTK_SHORT, "float32": VTK_FLOAT,
            }.get(str(src.dtype), VTK_UNSIGNED_CHAR)

            rows, cols = int(src.shape[0]), int(src.shape[1])
            if rows <= 0 or cols <= 0:
                return
            img = vtkImageData()
            img.SetDimensions(cols, rows, 1)
            img.SetSpacing(1.0, 1.0, 1.0)
            img.SetOrigin(0.0, 0.0, 0.0)
            img.AllocateScalars(vtk_type, nc)
            buf = vtk_to_numpy(img.GetPointData().GetScalars())
            buf = buf.reshape(rows, cols, nc) if nc > 1 else buf.reshape(rows, cols)
            # 与 XraySeriesReader / vtkDICOMImageReader 同序：下→上
            flipped = np.flipud(src)
            if nc > 1:
                buf[:, :, :] = flipped
            else:
                buf[:, :] = flipped
            self._image = img
            self._components = nc
            self._is_color = is_color
            self._spacing = (1.0, 1.0, 1.0)
        except Exception as exc:      # noqa: BLE001
            print("图片装配失败: %s" % exc)
            self._image = None


def _pick_image_grid(shapes):
    """决定一组图片的公共网格（rows, cols）

    必须是**严格多数**（`top * 2 > len`）：两张尺寸不一的图各占 1 票时
    `top * 2 >= len` 成立，取 max() 只会拿到**先出现的那张**（较小的），
    另一张比网格还大就补不进去（下标变成负数，广播报错）。
    实测 D:\\ClearTmp\\DicomTmp\\04\\Untitled-0（2792x2524 + 2984x2272）就踩过。
    少数派走「面积最大」，让画面信息尽量留在网格里。
    """
    if not shapes:
        return None
    counter = {}
    for s in shapes:
        counter[s] = counter.get(s, 0) + 1
    top = max(counter.values())
    if top * 2 > len(shapes):
        return max(counter.items(), key=lambda kv: kv[1])[0]
    return max(shapes, key=lambda s: (s[0] * s[1]))


def _pad_image_to(arr, shape):
    """把 arr 归到 shape（rows, cols）：居中补 0（arr 更大则居中裁中心）

    绝不放缩：诊断影像改比例会直接误判。网格是面积最大的那张，正常情况
    只需补边；但这道保险必须留着 —— 万一某张图比网格还大，居中切片的下标
    会变成负数（out[-96:2988] 只有 96 行），广播报错比裁一下难看得多。
    """
    rows, cols = int(shape[0]), int(shape[1])
    h, w = int(arr.shape[0]), int(arr.shape[1])
    if (h, w) == (rows, cols):
        return arr
    if arr.ndim == 3:
        out = np.zeros((rows, cols, int(arr.shape[2])), dtype=arr.dtype)
    else:
        out = np.zeros((rows, cols), dtype=arr.dtype)
    if h >= rows:
        y0 = (h - rows) // 2
        arr = arr[y0:y0 + rows, :]
    if w >= cols:
        x0 = (w - cols) // 2
        arr = arr[:, x0:x0 + cols]
    y0, x0 = (rows - int(arr.shape[0])) // 2, (cols - int(arr.shape[1])) // 2
    out[y0:y0 + arr.shape[0], x0:x0 + arr.shape[1]] = arr
    return out


class ImageSeriesReader(object):
    """常见图片**序列**读取器：把一个目录里的 N 张图片堆成多帧体数据。

    为什么要有这个类（v1.9.7）
    ------------------------
    科室从 PACS 导出的影像经常**根本不是 DICOM**：很多导选项只导出 JPG/PNG，
    一层目录一个序列。实测 ``D:\\ClearTmp\\DicomTmp\\04`` 就是 3 个子目录
    共 8 张尺寸各异的独立 JPG（2792x2524 / 2984x2272 / ...），以前走
    「加载患者目录」会被 ``is_single_series_dir`` 判成患者根目录 → 刷新出
    空列表（「未找到CT薄层序列」），用户看到的就是「PACS 导出的图读不出来」。

    这类目录的正确归宿是**整目录当多帧序列**：每张图一层，可翻层逐张看。
    图片没有 DICOM 头也没有 HU，因此：
      - 尺寸不齐时补黑边凑成公共网格（**不做缩放**，否则诊疗比例失真；
        也绝不能像 vtkDICOMImageReader 那样按首个文件分配缓冲区、
        遇到更大的图直接写越界段错误）；
      - 通道数以第一张为准；三通道全相等的首张图会退化成灰度，避免
        ImageProperty 的窗值去裁彩色分量（见 ImageFileReader._assemble）。

    对外只暴露与 XraySeriesReader / ImageFileReader 相同的一小撮调用面
    （SetDirectoryName / Update / GetOutput / GetSpacing / GetOrigin /
    GetExtent / GetScalarType / GetPixelSpacing），让 DicomLoadWorker 与
    Dicom2DReader 无需区分它和另外两个读取器。
    """

    # 目录里塞成百上千张缩略图时只取前若干张，避免一次解码把内存撑爆
    _MAX_FRAMES = 240
    _SUPPORTED_DTYPE = ("uint8", "uint16", "int16", "float32")

    def __init__(self):
        self._folder = None
        self._files = []
        self._image = None
        self._spacing = (1.0, 1.0, 1.0)
        self._components = 1
        self._is_color = False
        self._padded = 0          # 被补边的张数（供上层提示用）

    # ---- 构造 ----
    def SetDirectoryName(self, folder):
        self._folder = str(folder) if folder else None
        self._image = None

    def GetDirectoryName(self):
        return self._folder

    def GetFiles(self):
        return list(self._files)

    # ---- VTK reader 兼容接口 ----
    def GetOutput(self):
        return self._image

    def GetSpacing(self):
        return (self._spacing[0], self._spacing[1],
                1.0 * max(1, len(self._files)))

    def GetOrigin(self):
        return (0.0, 0.0, 0.0)

    def GetExtent(self):
        img = self._image
        if img is None:
            return (0, 0, 0, 0, 0, 0)
        return img.GetExtent()

    def GetScalarType(self):
        img = self._image
        return None if img is None else img.GetScalarType()

    def GetPixelSpacing(self):
        return [self._spacing[0], self._spacing[1]]

    def is_color(self) -> bool:
        return bool(self._is_color)

    @property
    def padded_count(self) -> int:
        """被补了黑边的张数（上层据此给用户一句提示）"""
        return int(self._padded)

    # ---- 解码 ----
    @staticmethod
    def _decode(path):
        """复用 ImageFileReader 的解码（含 BGRA 合成、BGR→RGB、Pillow 兜底）"""
        try:
            return ImageFileReader._decode(path)
        except Exception:                  # noqa: BLE001
            return None

    def _pick_grid(self, shapes):
        """决定公共网格：绝大多数同尺寸 → 用它们；否则取**面积最大**的那张

        必须是严格多数（`top * 2 > len`）：两张尺寸不一的图各占 1 票时
        `top * 2 >= len` 成立，取 max() 只会拿到**先出现的那张**（较小的），
        另一张比网格还大就补不进去（下标变成负数，广播报错）。
        实测 D:\\ClearTmp\\DicomTmp\\04\\Untitled-0（2792x2524 + 2984x2272）
        就踩过这个平局。
        """
        if not shapes:
            return None
        counter = {}
        for s in shapes:
            counter[s] = counter.get(s, 0) + 1
        top = max(counter.values())
        if top * 2 > len(shapes):
            return max(counter.items(), key=lambda kv: kv[1])[0]
        return max(shapes, key=lambda s: (s[0] * s[1]))

    def Update(self):
        if self._image is not None:
            return
        folder = self._folder
        if not folder or not os.path.isdir(folder):
            return
        try:
            names = sorted(f for f in os.listdir(folder)
                           if os.path.isfile(os.path.join(folder, f))
                           and f.lower().endswith(IMAGE_FILE_EXTS))
        except Exception:                  # noqa: BLE001
            return
        if not names:
            return

        paths = [os.path.join(folder, n) for n in names[:self._MAX_FRAMES]]
        arrays = []
        for p in paths:
            a = self._decode(p)
            if a is not None and a.ndim == 2 and a.size > 0:
                arrays.append(a)
        if not arrays:
            return

        # 通道归一：以第一张为准（三通道且确有彩色的按彩图，否则一律灰度）
        is_color = (arrays[0].ndim == 3 and int(arrays[0].shape[2]) == 3)
        if is_color:
            flat = [a if a.ndim == 3 else np.stack([a] * 3, axis=-1)
                    for a in arrays]
        else:
            flat = [a if a.ndim == 2 else a[:, :, 0] for a in arrays]

        grid = _pick_image_grid([(a.shape[0], a.shape[1]) for a in flat])
        if grid is None:
            return
        self._padded = sum(1 for a in flat if (a.shape[0], a.shape[1]) != grid)
        nc = 3 if is_color else 1

        # 尺寸归一 + 行序翻成「下→上」（与 ImageFileReader._assemble 同约定）
        slices = [np.flipud(_pad_image_to(a, grid)) for a in flat]

        src = slices[0]
        name = str(src.dtype)
        if name not in self._SUPPORTED_DTYPE:
            if src.dtype.kind == "f":
                src = src.astype(np.float32)
            elif src.dtype.kind in ("i", "u") and src.dtype.itemsize <= 2:
                src = src.astype(np.uint16)
            else:
                src = src.astype(np.uint8)
        slices = [s.astype(src.dtype, copy=False) for s in slices]

        try:
            from vtkmodules.vtkCommonDataModel import vtkImageData
            from vtkmodules.util.numpy_support import vtk_to_numpy
            from vtkmodules.vtkCommonCore import (
                VTK_UNSIGNED_CHAR, VTK_UNSIGNED_SHORT, VTK_SHORT, VTK_FLOAT)

            rows, cols = grid
            n = len(slices)
            if rows <= 0 or cols <= 0 or n <= 0:
                return
            vtk_type = {
                "uint8": VTK_UNSIGNED_CHAR, "uint16": VTK_UNSIGNED_SHORT,
                "int16": VTK_SHORT, "float32": VTK_FLOAT,
            }.get(str(src.dtype), VTK_UNSIGNED_CHAR)

            img = vtkImageData()
            img.SetDimensions(cols, rows, n)
            img.SetSpacing(1.0, 1.0, 1.0)
            img.SetOrigin(0.0, 0.0, 0.0)
            img.AllocateScalars(vtk_type, nc)
            buf = vtk_to_numpy(img.GetPointData().GetScalars())
            # VTK 点序：x 最快、y 次、z 最慢 → numpy 轴序 (z, y, x[, c])
            if nc > 1:
                buf = buf.reshape(rows, cols, n, nc)
                for z, s in enumerate(slices):
                    buf[:, :, z, :] = s
            else:
                buf = buf.reshape(rows, cols, n)
                for z, s in enumerate(slices):
                    buf[:, :, z] = s

            self._image = img
            self._components = nc
            self._is_color = is_color
            self._files = paths
        except Exception as exc:          # noqa: BLE001
            print("图片序列装配失败: %s" % exc)
            self._image = None


def dir_image_files(folder: str, limit: int = None) -> list:
    """列出目录直属的常见图片（按文件名排序）；目录里没有 .dcm 时才该用"""
    if not folder or not os.path.isdir(folder):
        return []
    try:
        names = sorted(f for f in os.listdir(folder)
                       if os.path.isfile(os.path.join(folder, f))
                       and f.lower().endswith(IMAGE_FILE_EXTS))
    except Exception:                  # noqa: BLE001
        return []
    return names[:limit] if limit else names


def image_subdirs(folder: str) -> list:
    """列出**一层子目录**里装满了常见图片的子目录，返回 [(名称, 图片数, 路径)]

    PACS 的「导出为图片」常按序列再建一层目录（实测 D:\\ClearTmp\\DicomTmp\\04
    顶层 0 文件，Untitled-0/-1/-2 各装 2/2/4 张 JPG），这时整个目录既不是
    单序列目录、也不是患者根目录，得单独认出来。
    """
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    try:
        names = sorted(os.listdir(folder))
    except Exception:                  # noqa: BLE001
        return out
    for n in names:
        p = os.path.join(folder, n)
        if not os.path.isdir(p):
            continue
        imgs = dir_image_files(p)
        if imgs:
            out.append((str(n), len(imgs), p))
    return out


def is_image_only_dir(folder: str) -> bool:
    """目录**没有任何 .dcm**，但躺着常见图片 —— 这就是「PACS 只导出了图片」"""
    if not folder or not os.path.isdir(folder):
        return False
    try:
        names = os.listdir(folder)
    except Exception:                  # noqa: BLE001
        return False
    for n in names:
        p = os.path.join(folder, n)
        if not os.path.isfile(p):
            continue
        low = n.lower()
        if low.endswith(".dcm"):
            return False
    return bool(dir_image_files(folder))


def dir_image_groups(root: str, max_depth: int = 3):
    """按**文件夹**分组扫描「图片 / 简易影像」目录（v1.9.8）

    与 CT 的口径对齐：CT 里一个文件夹就是一个序列，这里同样
    「一个文件夹 = 一条序列」，列表里每个条目后面挂着张数。

    只统计目录**直属**的文件（不递归进子目录），返回
    ``[(相对名, 绝对路径, n_dicom, n_image), ...]``：

    - ``D:\\...\\07``           顶层无文件 → 子目录 ``Untitled-0`` 5 张 dcm → 1 组
    - ``D:\\...\\04``           顶层无文件 → ``Untitled-0/-1/-2`` 各一组（3 组）
    - ``D:\\...\\02``           顶层 8 张 dcm → **root 自己**一组（不再递归子目录）

    最后一条很关键：扁平目录不该把每个子目录再列一遍，否则用户点进去
    看到的是同一批片子被拆成多行。
    """
    if not root or not os.path.isdir(root):
        return []

    def _count(d):
        n_d = n_i = 0
        try:
            names = os.listdir(d)
        except OSError:
            return 0, 0
        for name in names:
            if not os.path.isfile(os.path.join(d, name)):
                continue
            low = name.lower()
            if low.endswith(".dcm"):
                n_d += 1
            elif low.endswith(IMAGE_FILE_EXTS):
                n_i += 1
        return n_d, n_i

    n_d, n_i = _count(root)
    if n_d or n_i:
        return [(".", root, n_d, n_i)]

    out = []
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            n_d = sum(1 for f in filenames if f.lower().endswith(".dcm"))
            n_i = sum(1 for f in filenames
                      if not f.lower().endswith(".dcm")
                      and f.lower().endswith(IMAGE_FILE_EXTS))
            if n_d or n_i:
                out.append((os.path.relpath(dirpath, root), dirpath, n_d, n_i))
    except OSError:
        return out
    return out


def build_image_group_patient(root: str, groups: list):
    """把「文件夹分组」打包成患者树可直接消费的 ``patients`` 列表

    患者节点用根目录名（如 ``07``），每个分组就是一个序列节点，``folder``
    直接指向该文件夹 —— 双击时 `_load_series_payload` 认得它就能整夹加载。
    有 .dcm 的组顺带探一次模态（DX 走 XraySeriesReader 才有正确窗值），
    纯图片组标 ``is_image_group``，加载时走 `ImageSeriesReader`。
    """
    if not groups:
        return []
    root_name = os.path.basename(os.path.normpath(root)) or "图片目录"
    try:
        mtime = float(os.path.getmtime(root))
    except OSError:
        mtime = 0.0

    series = []
    for rel, path, n_d, n_i in groups:
        name = os.path.basename(os.path.normpath(path)) or rel or path
        item = {
            "series_uid": name,
            "folder": path,
            "file_uids": [],
            "img_count": int(n_d + n_i),
            "modality": "",
            "series_description": name,
            "body_part": "",
            "slice_thickness": 0,
            "is_image_group": n_d == 0,
        }
        if n_d:
            # 文件夹里往往**混放多个序列**（典型病例导出：hiv肺炎 756 张
            # 实际是 CT+MR 好几个 SeriesInstanceUID）。按 UID 拆开，
            # 与 CT 扫描「一个 SeriesInstanceUID = 一条序列」同口径；
            # 索引按目录缓存，拆不出（损坏/全同 UID）就退回整夹一条。
            try:
                uid_groups, _idx = _series_index_uids(path)
            except Exception:                  # noqa: BLE001
                uid_groups = {}
            if len(uid_groups) > 1:
                for seq_no, (uid, files) in enumerate(
                        sorted(uid_groups.items(),
                               key=lambda kv: -len(kv[1])), start=1):
                    try:
                        info = _analyze_series_from_dicom_fast(path, uid) or {}
                    except Exception:          # noqa: BLE001
                        info = {}
                    _desc = str(info.get("series_description") or "").strip()
                    if _desc.lower().startswith("dummy"):
                        # 部分设备写的是占位描述（实测 DummySeriesDesc!），
                        # 155 条全叫一个名没法分辨，当空处理回退「序列N」
                        _desc = ""
                    series.append({
                        "series_uid": uid,
                        "folder": path,
                        "file_uids": list(files),
                        "img_count": len(files),
                        "modality": info.get("modality", ""),
                        "series_description": _desc or ("序列%d" % seq_no),
                        "body_part": info.get("body_part", ""),
                        "slice_thickness": info.get("slice_thickness", 0),
                        "is_image_group": False,
                    })
                continue
            try:
                info = _analyze_series_from_dicom_fast(path) or {}
            except Exception:      # noqa: BLE001
                info = {}
            if info.get("modality"):
                item["modality"] = info["modality"]
            if info.get("series_description"):
                item["series_description"] = info["series_description"]
            if info.get("body_part"):
                item["body_part"] = info["body_part"]
        series.append(item)

    # 患者级模态：有任一分组是 X 线（DX/CR/DR…）就标 XR，列表里会挂
    # [DR] 前缀走绿色，跟 CT 目录一眼区分开
    mods = {str(s.get("modality") or "").upper() for s in series}
    modality = "XR" if any(is_xr_modality(m) for m in mods) else ""

    return [{
        # pat_id 用目录绝对路径：两个扫描根的 basename 可能同名（如两个
        # 「疑难病例」），pat_id 为空串时患者 key（id|access|name）会撞车，
        # 增量刷新时第二个目录的条目会被当成重复跳过
        "pat_id": os.path.normcase(os.path.abspath(root)),
        "pat_access": "",
        "pat_name": root_name,
        "pat_sex": "",
        "pat_age": "",
        "study_date": "",
        "modality": modality,
        "mtime": mtime,
        "series": series,
        "series_keys": {s.get("series_uid", "") for s in series},
    }]


class Dicom2DReader(QtWidgets.QFrame):
    """二维 DICOM 阅读器。

    - 单视图大画面显示，滚轮 / ↑↓ 方向键翻层
    - 右侧纵向滑条：拖动浏览整个序列
    - 右键上下拖动：调节窗宽窗位（R 键复位到序列默认窗值）
    - 中键拖动 / Ctrl+滚轮：缩放
    - 左键拖动：平移（Pan）
    - 底部缩略图条快速跳层

    渲染方式：每次换层都构造一张单层二维 vtkImageData 交给 ImageActor，
    而不是把整卷三维数据塞给 ImageActor 再用 SetDisplayExtent 切层。
    """

    slice_changed = QtCore.pyqtSignal(int, int)   # 当前层(1基), 总层数
    window_changed = QtCore.pyqtSignal(float, float)  # ww, wl
    # 足弓关键点被手动拖动后抛出，携带 {"medial": float|None,
    # "lateral": float|None, "edited": bool, "points": {...}}
    keypoints_changed = QtCore.pyqtSignal(dict)

    THUMB_SAMPLE_LIMIT = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dicom2dReader")
        # 允许通过键盘控制翻层
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 视图行：左侧 VTK 渲染区 + 右侧纵向翻层滑条
        self._view_row = QtWidgets.QWidget(self)
        row_layout = QtWidgets.QHBoxLayout(self._view_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(0)
        layout.addWidget(self._view_row, 1)

        self._vtk_widget = QVTKRenderWindowInteractor(self._view_row)
        self._vtk_widget.setFocusPolicy(QtCore.Qt.StrongFocus)
        # 键盘事件优先由本控件处理（方向键翻层）
        self._vtk_widget.installEventFilter(self)
        row_layout.addWidget(self._vtk_widget, 1)

        self._slice_slider = QtWidgets.QSlider(QtCore.Qt.Vertical, self._view_row)
        self._slice_slider.setFixedWidth(22)
        self._slice_slider.setFocusPolicy(QtCore.Qt.NoFocus)
        self._slice_slider.setCursor(QtCore.Qt.PointingHandCursor)
        self._slice_slider.setToolTip("拖动浏览序列图层（↑/↓ 键同样可翻层）")
        # 顶部=第 1 层，向下为后续层，符合从上到下的阅片顺序
        self._slice_slider.setInvertedAppearance(True)
        self._slice_slider.setInvertedControls(True)
        theme.bind(self._slice_slider, theme.qss_slice_slider)
        self._slice_slider.valueChanged.connect(self._on_slider_changed)
        self._slice_slider.hide()
        row_layout.addWidget(self._slice_slider)

        self._strip = ThumbnailStrip(self)
        self._strip.slice_requested.connect(self._on_thumb_requested)
        layout.addWidget(self._strip)
        self._strip.hide()

        # 完全手动接管渲染管线：
        # 不使用 vtkImageViewer2 —— 它默认把 z 轴朝向相机，且其内部的
        # vtkImageMapToWindowLevelColors 在无输入时会持续刷错误日志。
        # 这里自建 Renderer + ImageActor + 正交相机，确保轴向切片按阅片
        # 习惯正确显示；窗宽窗位直接用 ImageActor 自带的 ImageProperty
        # （SetColorWindow/SetColorLevel），无需额外的映射管线。
        #
        # 重要：ImageActor 的输入必须是「单层二维 vtkImageData」。
        # 若把整卷 3D 数据交给 ImageActor、再用 SetDisplayExtent 切层，
        # OpenGL 后端会把整卷当作 3D 纹理处理：层数较多时纹理无法完整
        # 上传或不随 DisplayExtent 更新，表现为「只有前几层有图像、
        # 之后全部黑屏」。因此每次换层都重新构造一张单层图像。
        self._renderer = vtkRenderer()
        self._renderer.SetBackground(0.0, 0.0, 0.0)
        self._vtk_widget.GetRenderWindow().AddRenderer(self._renderer)

        self._image_actor = vtkImageActor()
        self._image_actor.SetVisibility(False)
        self._image_actor.GetProperty().SetColorWindow(400.0)
        self._image_actor.GetProperty().SetColorLevel(40.0)
        self._renderer.AddActor(self._image_actor)

        # AI 标注结果图（RGB）专用 actor：与 _image_actor 共用同一相机与
        # 同一 spacing/origin，因此与原图逐像素对齐；靠可见性切换在原图与
        # 标注图之间来回，互不干扰各自的窗宽窗位设置。
        self._ai_actor = vtkImageActor()
        self._ai_actor.SetVisibility(False)
        self._ai_actor.GetProperty().SetColorWindow(255.0)
        self._ai_actor.GetProperty().SetColorLevel(127.5)
        self._renderer.AddActor(self._ai_actor)

        # 测量图形叠加层：沿用三维视图已验证可靠的做法——在渲染窗口上开第二层
        # overlay renderer（Layer 1），与底层共享同一相机。所有测量线/文字都以
        # VTK actor 形式画在 GL 上下文内，因此能正确叠加在图像之上（不会被
        # 父窗体背景透出导致黑屏）。注意：不能用 Qt 子控件盖在 VTK 原生窗口上
        # 来做这件事——那样透出的是父窗体背景而非 GL 图像。
        rw = self._vtk_widget.GetRenderWindow()
        rw.SetNumberOfLayers(2)
        self._renderer.SetLayer(0)
        self._overlay_renderer = vtkRenderer()
        self._overlay_renderer.SetLayer(1)
        self._overlay_renderer.SetInteractive(0)
        self._overlay_renderer.PreserveColorBufferOn()
        # 共享与底层相同的相机（_fit_camera 中会再次设置，确保引用一致）
        self._overlay_renderer.SetActiveCamera(self._renderer.GetActiveCamera())
        rw.AddRenderer(self._overlay_renderer)

        self._interactor_initialized = False
        self._source_reader = None
        self._origin = None
        self._spacing = None
        self._extent = None
        # AI 标注图（uint8 RGB）的尺寸；足弓关键点微调按它换算世界坐标
        self._ai_size = None

        # numpy 形式的体数据视图 (nz, ny, nx[, nc])，用于零拷贝取单层
        self._volume_array = None
        self._volume_shape = None      # (nz, ny, nx, nc)
        self._scalar_type = None
        self._slice_image = None       # 当前层的二维 vtkImageData

        # MONOCHROME1 的 X 线片子要反相显示（详见 series_needs_invert）。
        # _invert_sum = min + max，反相即 x' = _invert_sum - x（对区间中点镜像，
        # 保证映射后取值范围不变，窗宽窗位只需同步镜像一次）。
        self._display_invert = False
        self._invert_sum = None

        self._window_width = 400.0
        self._window_center = 40.0
        self._default_window_width = 400.0
        self._default_window_center = 40.0

        self._mouse_down = False
        self._right_mouse_down = False
        self._middle_mouse_down = False
        self._last_mouse_pos = None
        self._ctrl_down = False

        self._dicom_data = {}
        self._slice_positions = None   # 每层物理位置(mm)
        # 本窗口当前载入的 vtkImageData 输出（has_image() 据此判断有无图像）
        self._reader_output = None
        self._thumb_pixmaps = []
        self._thumb_labels = []
        self._thumbnail_ready = False

        # VTK 交互器触发的缩略图生成请求（由 Qt 定时器在主线程执行，防止崩溃）
        self._thumb_refresh_timer = QtCore.QTimer(self)
        self._thumb_refresh_timer.setSingleShot(True)
        self._thumb_refresh_timer.timeout.connect(self._build_thumbnails)

        self._build_overlay_labels()
        self._build_fullscreen_button()
        self._init_measure_state()
        self._build_measure_toolbar()

    # ---------- UI 构建 ----------
    def _build_overlay_labels(self):
        corner_names = ["_tl_label", "_tr_label", "_bl_label", "_br_label"]
        for name in corner_names:
            lbl = QtWidgets.QLabel(self)
            theme.bind(lbl, theme.qss_overlay_label)
            lbl.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            lbl.hide()
            setattr(self, name, lbl)

        # 屏幕方向标识（沿用现有视图习惯：屏幕左为“右”，屏幕右为“左”）
        self._left_char_label = QtWidgets.QLabel("右", self)
        self._right_char_label = QtWidgets.QLabel("左", self)
        for _lbl in (self._left_char_label, self._right_char_label):
            theme.bind(_lbl, lambda: theme.qss_char_label("22pt", 160))
        self._left_char_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._right_char_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._left_char_label.hide()
        self._right_char_label.hide()

        # 二维阅读不再显示「左/右」方位字（按用户要求直接关闭，
        # 避免与三维 MPR 方位字叠加成重字）。保留两个 QLabel 实例（隐藏态），
        # 仅从叠加层清单剔除，使其永不被 _update_overlays / 显隐循环 show 出来。
        self._all_labels = [
            self._tl_label, self._tr_label, self._bl_label, self._br_label,
        ]

    def _build_fullscreen_button(self):
        """缩略图条开关。

        全屏按钮原先也在这里单独创建、浮在左下角；v1.15.0 起已迁入底部
        工具栏（见 ``_build_measure_toolbar``），缩放时随栏整体移动。
        """
        self._fullscreen_expanded = False

        # 缩略图条开关（样式与工具栏保持一致：图标在上、文字在下）
        self._strip_btn = QtWidgets.QToolButton(self)
        self._strip_btn.setCheckable(True)
        self._strip_btn.setFixedHeight(TOOL_BTN_H)
        theme.bind(self._strip_btn, tool_btn_qss)
        self._strip_btn.toggled.connect(self._on_strip_toggled)
        ui_icons.apply_tool_button(
            self._strip_btn, "strip", "序列",
            "序列：显示/隐藏底部缩略图条", size=20, icon_size=18)

    def toggle_fullscreen(self):
        """全屏 / 还原主窗口。

        按钮本身常驻工具栏，图标不再随状态切换（换图标反而让人以为是两个
        按钮），改用「按下态 + tooltip」表达当前是否全屏。
        """
        win = self.window()
        if win is None:
            return
        if win.isFullScreen():
            win.showMaximized()
            self._fullscreen_expanded = False
        else:
            win.showFullScreen()
            self._fullscreen_expanded = True
        btn = getattr(self, "_fullscreen_btn", None)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(bool(self._fullscreen_expanded))
            btn.blockSignals(False)
            btn.setToolTip("退出全屏" if self._fullscreen_expanded
                           else "全屏：把主窗口全屏显示，再点一次还原")

    # ---------- 测量工具 ----------
    def _init_measure_state(self):
        self._measure_tool = "none"      # none | ct | length | angle
        self._select_mode = False        # 选择模式：左键拖动 = 平移视图
        self._user_invert = False        # 用户手动反色（与 MONOCHROME1 反相异或）
        self._select_panning = False     # 选择模式下是否正在拖动
        self._select_last_pos = None
        self._measure_items = []         # 已完成测量（按层存储）
        self._measure_draft = []         # 当前正在放置的点（世界坐标）
        self._hover_world = None         # 光标世界坐标
        self._hover_text = ""            # 光标旁浮标文本（CT 实时值）
        self._measure_actors = []        # overlay renderer 中当前承载的测量 VTK actor
        # 圆形 ROI 默认半径（mm）；滚轮或 [ ] 调整大小
        self._ct_radius_mm = 10.0
        self._init_keypoint_state()

    # ---------- 足弓关键点手动微调 ----------
    def _init_keypoint_state(self):
        """关键点微调状态。

        模型给出的 5 个足弓关键点常常有 3~5 像素的偏差，重训模型代价太大，
        所以这里支持在图上直接拖动手柄微调、拖动即时重算足弓角。所有坐标
        都记在**图像像素**里（行序与 xray 侧一致：行号从上往下增长），
        只有在画到 overlay 层时才换算成世界坐标。

        ``_kp_shape`` 区分两类测量：``"foot"``（足弓，P1~P5 折线 + 内外侧
        角）与 ``"cobb"``（脊柱侧弯，P1/P2 上端椎终板线、P3/P4 下端椎终板
        线，读数直接是 Cobb 角）。两者共用同一套抓握/拖动/复位机制。
        """
        self._kp_points = {}       # {idx: (x, y)} 当前（可能被改过的）点位
        self._kp_source = {}       # {idx: (x, y)} 模型原始点位，供复位
        self._kp_names = {i: "P%d" % i for i in range(1, 6)}
        self._kp_colors = {}       # {idx: (r, g, b)} 0~255
        self._kp_size = None       # (nx, ny) 点位所属图像的像素尺寸
        self._kp_actors = []
        self._kp_shown = False     # 手柄是否正显示在 overlay 上
        self._kp_drag = None       # 正在拖动的手柄 idx
        self._kp_hover = None      # 光标下的手柄 idx（用于改光标形状）
        self._kp_radius_px = 9     # 手柄半径（图像像素），抓握半径另算
        self._kp_grab_px = 16      # 抓握判定半径（屏幕像素）
        self._kp_moved = False     # 是否被手动改过（用于界面提示）
        self._kp_shape = "foot"    # "foot" 足弓折线 / "cobb" 终板线 + Cobb 读数

    def _build_measure_toolbar(self):
        """底部工具栏：选择 / 反色 / CT值 / 长度 / 角度 / 清除 / 全屏 / 隐藏。

        v1.15.0 三处改动：
          ① 全部按钮改为「图标在上 + 汉字在下」——纯图标在深色底上很难一眼
             分辨（实测用户把「剪刀=切割」误认为「裁剪图片」）。
          ② 新增「选择」（鼠标模式：拖拽=平移）与「反色」（灰度黑白反转）。
          ③ 全屏按钮从「单独浮在左下角」收进栏内，缩放时随栏整体移动；
             栏尾加「隐藏」，收起后左下角留一个「工具」按钮可再展开。
        """
        bar = QtWidgets.QWidget(self)
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        theme.bind(bar, tool_bar_qss)

        def _mk(name, text, tip, checkable=False, color=None):
            btn = QtWidgets.QToolButton(bar)
            btn.setCheckable(checkable)
            btn.setFixedHeight(TOOL_BTN_H)
            ui_icons.apply_tool_button(
                btn, name, text, tip, size=20, icon_size=18,
                color=(color or ui_icons.ICON_COLOR))
            layout.addWidget(btn)
            return btn

        # 鼠标模式：开启后左键拖动 = 平移视图，不再落测量点
        self._select_btn = _mk(
            "select", "选择",
            "选择：开启后鼠标拖动为平移视图，不再落测量点；再点一次退出",
            checkable=True)
        self._select_btn.toggled.connect(self._on_select_toggled)

        # 反色：灰度黑白反转（只影响显示，不改原始数据）
        self._invert_btn = _mk(
            "invert", "反色",
            "反色：把当前层灰度黑白反转显示（只影响显示，不改动原始数据）",
            checkable=True)
        self._invert_btn.toggled.connect(self._on_invert_toggled)

        self._ct_btn = _mk("ct", "CT值",
                           "CT值：点击放置圆形 ROI 圆心，滚轮或 [ ] 调整半径，"
                           "显示区域内平均 HU", checkable=True)
        self._len_btn = _mk("ruler", "长度",
                            "长度：依次点击两个点，量出两点间距离（mm）",
                            checkable=True)
        self._ang_btn = _mk("angle", "角度",
                            "角度：依次点击三个点（顶点、起始边、终止边），"
                            "量出夹角", checkable=True)
        for btn, tool in ((self._ct_btn, "ct"),
                          (self._len_btn, "length"),
                          (self._ang_btn, "angle")):
            btn.toggled.connect(
                lambda checked, t=tool: self._on_measure_toggled(t, checked))

        self._measure_clear_btn = _mk("eraser", "清除",
                                      "清除：清除当前层的全部测量标记")
        self._measure_clear_btn.clicked.connect(self._clear_measurements)

        # 全屏（原先单独浮在左下角）
        self._fullscreen_btn = _mk("maximize", "全屏",
                                   "全屏：把主窗口全屏显示，再点一次还原")
        self._fullscreen_btn.clicked.connect(self.toggle_fullscreen)

        # 收起工具栏
        self._toolbar_hide_btn = _mk("eye_off", "隐藏",
                                     "隐藏：收起本工具栏（左下角保留「工具」"
                                     "按钮可再次展开）")
        self._toolbar_hide_btn.clicked.connect(
            lambda: self._set_toolbar_visible(False))

        self._measure_bar = bar

        # 工具栏收起后，用来重新展开的小按钮（常驻左下角）
        self._toolbar_show_btn = QtWidgets.QToolButton(self)
        self._toolbar_show_btn.setFixedHeight(TOOL_BTN_H)
        theme.bind(self._toolbar_show_btn, tool_btn_qss)
        ui_icons.apply_tool_button(self._toolbar_show_btn, "tools", "工具",
                                   "工具：重新展开底部工具栏",
                                   size=20, icon_size=18)
        self._toolbar_show_btn.setVisible(False)
        self._toolbar_show_btn.clicked.connect(
            lambda: self._set_toolbar_visible(True))

    def _set_toolbar_visible(self, visible: bool):
        """显示/隐藏底部工具栏，并保持左下角始终有一个入口按钮。"""
        bar = getattr(self, "_measure_bar", None)
        show_btn = getattr(self, "_toolbar_show_btn", None)
        if bar is None:
            return
        bar.setVisible(bool(visible))
        if show_btn is not None:
            show_btn.setVisible(not bool(visible))
        self._position_widgets()

    # ---------- 选择 / 反色 ----------
    def _on_select_toggled(self, checked):
        """选择模式：拖拽平移视图；与测量工具互斥。"""
        self._select_mode = bool(checked)
        if checked:
            # 选中「选择」时退出测量，避免左键既下点又拖动的歧义
            for btn in (self._ct_btn, self._len_btn, self._ang_btn):
                if btn.isChecked():
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.blockSignals(False)
            self._measure_tool = "none"
        self._apply_measure_mode()

    def _on_invert_toggled(self, checked):
        """反色：与 MONOCHROME1 的自动反相做异或，二者同时生效会互相抵消。"""
        self._user_invert = bool(checked)
        try:
            self._apply_slice(self._current_index)
            self._render()
        except Exception:          # noqa: BLE001
            pass

    def _measure_active(self):
        return self._measure_tool != "none"

    def _on_measure_toggled(self, tool, checked):
        if checked:
            # 三个工具互斥：选中一个时取消另外两个
            for other, btn in (("ct", self._ct_btn),
                               ("length", self._len_btn),
                               ("angle", self._ang_btn)):
                if other != tool and btn.isChecked():
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.blockSignals(False)
            # 测量与「选择」互斥：下了测量点就不该再拖动平移
            sel = getattr(self, "_select_btn", None)
            if sel is not None and sel.isChecked():
                sel.blockSignals(True)
                sel.setChecked(False)
                sel.blockSignals(False)
            self._select_mode = False
            self._measure_tool = tool
        else:
            if self._measure_tool == tool:
                self._measure_tool = "none"
        self._apply_measure_mode()

    def _apply_measure_mode(self):
        # 切换工具时放弃进行中的测量，并刷新光标
        self._measure_draft = []
        self._hover_text = ""
        try:
            if self._measure_active():
                cur = QtCore.Qt.CrossCursor
            elif getattr(self, "_select_mode", False):
                cur = QtCore.Qt.OpenHandCursor
            else:
                cur = QtCore.Qt.ArrowCursor
            self._vtk_widget.setCursor(cur)
        except Exception:
            pass
        self._update_measure_overlay()

    def _update_measure_overlay(self):
        """重建测量 VTK actor（画在 overlay renderer 上）并刷新。
        线/点用世界坐标（随图像平移缩放），文字与 CT 十字用显示坐标
        （恒定屏幕大小，不受缩放影响）。
        """
        self._rebuild_measure_actors()
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win is not None:
            render_win.Render()

    # ---------- 测量 VTK actor 构建 ----------
    def _clear_measure_actors(self):
        ov = getattr(self, "_overlay_renderer", None)
        if ov is None:
            self._measure_actors = []
            return
        for a in self._measure_actors:
            try:
                ov.RemoveActor(a)
            except Exception:
                pass
        self._measure_actors = []

    def _add_measure_actor(self, actor):
        ov = getattr(self, "_overlay_renderer", None)
        if ov is None or actor is None:
            return
        ov.AddActor(actor)
        self._measure_actors.append(actor)

    def _mk_world_line_actor(self, pts_world, color_rgb, width=2.0):
        pd = vtkPolyData()
        pts = vtkPoints()
        for p in pts_world:
            pts.InsertNextPoint(float(p[0]), float(p[1]), 0.0)
        pd.SetPoints(pts)
        ca = vtkCellArray()
        if len(pts_world) <= 1:
            for i in range(len(pts_world)):
                ca.InsertNextCell(1)
                ca.InsertCellPoint(i)
            pd.SetVerts(ca)
        else:
            ca.InsertNextCell(len(pts_world))
            for i in range(len(pts_world)):
                ca.InsertCellPoint(i)
            pd.SetLines(ca)
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(pd)
        coord = vtkCoordinate()
        coord.SetCoordinateSystemToWorld()
        mapper.SetTransformCoordinate(coord)
        actor = vtkActor2D()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(color_rgb[0], color_rgb[1], color_rgb[2])
        actor.GetProperty().SetLineWidth(width)
        return actor

    def _mk_disp_line_actor(self, pts_display, color_rgb, width=2.0):
        pd = vtkPolyData()
        pts = vtkPoints()
        for p in pts_display:
            pts.InsertNextPoint(float(p[0]), float(p[1]), 0.0)
        pd.SetPoints(pts)
        ca = vtkCellArray()
        ca.InsertNextCell(len(pts_display))
        for i in range(len(pts_display)):
            ca.InsertCellPoint(i)
        pd.SetLines(ca)
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(pd)
        coord = vtkCoordinate()
        coord.SetCoordinateSystemToDisplay()
        mapper.SetTransformCoordinate(coord)
        actor = vtkActor2D()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(color_rgb[0], color_rgb[1], color_rgb[2])
        actor.GetProperty().SetLineWidth(width)
        return actor

    def _mk_text_actor(self, text, display_xy, color_rgb):
        ta = vtkTextActor()
        ta.SetInput(str(text))
        tp = ta.GetTextProperty()
        tp.SetColor(color_rgb[0], color_rgb[1], color_rgb[2])
        tp.SetFontSize(13)
        tp.SetBold(True)
        tp.SetBackgroundColor(0.0, 0.0, 0.0)
        tp.SetBackgroundOpacity(0.55)
        tp.SetJustificationToLeft()
        tp.SetVerticalJustificationToBottom()
        coord = vtkCoordinate()
        coord.SetCoordinateSystemToDisplay()
        ta.SetPositionCoordinate(coord)
        ta.SetPosition(float(display_xy[0]) + 6.0, float(display_xy[1]) + 6.0)
        return ta

    def _add_ct_marker(self, world_pt, color_rgb, r=7.0):
        """CT 探针位置画一个恒定屏幕大小的十字（显示坐标）"""
        d = self._world_to_display(world_pt)
        if d is None:
            return
        x, y = float(d[0]), float(d[1])
        self._add_measure_actor(
            self._mk_disp_line_actor([(x - r, y), (x + r, y)], color_rgb))
        self._add_measure_actor(
            self._mk_disp_line_actor([(x, y - r), (x, y + r)], color_rgb))

    def _add_ct_circle(self, world_pt, radius_mm, color_rgb, width=1.5):
        """以世界坐标画一个圆形 ROI（半径单位 mm，随平移/缩放自适应）"""
        cx, cy = float(world_pt[0]), float(world_pt[1])
        R = float(radius_mm)
        sp = self._spacing or (1.0, 1.0, 1.0)
        sx = float(sp[0]) or 1e-6
        sy = float(sp[1]) or 1e-6
        # 世界坐标下以 mm 为单位的真圆；按像素密度取足够分段
        seg = max(32, int(round(2.0 * math.pi * (R / max(sx, sy)) / 3.0)))
        seg = min(seg, 240)
        pts = []
        for i in range(seg + 1):
            a = 2.0 * math.pi * float(i) / float(seg)
            pts.append((cx + R * math.cos(a), cy + R * math.sin(a)))
        self._add_measure_actor(
            self._mk_world_line_actor(pts, color_rgb, width=width))

    def _rebuild_measure_actors(self):
        self._clear_measure_actors()
        k = self._current_slice()
        color_len = (0.0, 0.898, 1.0)   # #00E5FF
        color_ct = (1.0, 0.835, 0.31)   # #FFD54F

        def _line(world_pts, color):
            self._add_measure_actor(self._mk_world_line_actor(world_pts, color))

        def _text(world_pt, text, color):
            d = self._world_to_display(world_pt)
            if d is not None:
                self._add_measure_actor(self._mk_text_actor(text, d, color))

        items = [it for it in self._measure_items if it.get("slice") == k]
        has_ct = any(it.get("type") == "ct" for it in items)
        for it in items:
            if it["type"] == "length":
                _line([it["p1"], it["p2"]], color_len)
                mid = ((it["p1"][0] + it["p2"][0]) / 2.0,
                       (it["p1"][1] + it["p2"][1]) / 2.0)
                _text(mid, f"{it['mm']:.1f} mm", color_len)
            elif it["type"] == "angle":
                _line([it["p1"], it["pv"]], color_len)
                _line([it["pv"], it["p2"]], color_len)
                if it["deg"] is not None:
                    _text(it["pv"], f"{it['deg']:.1f}deg", color_len)
            elif it["type"] == "ct":
                self._add_ct_circle(it["world"], it.get("radius", 10.0), color_ct)
                self._add_ct_marker(it["world"], color_ct)
                hu = it["hu"]
                if hu is None:
                    txt = "-"
                elif self._dicom_data.get("modality") == "CT":
                    txt = f"Mean {hu:.0f} HU"
                else:
                    txt = f"Mean {hu:.0f}"
                if it.get("n"):
                    txt += f"  r={it['radius']:.0f}mm n={it['n']}"
                _text(it["world"], txt, color_ct)

        # 进行中的 draft（橡皮筋预览）
        if self._measure_draft:
            for w in self._measure_draft:
                _line([w], color_len)
            if self._hover_world is not None:
                if self._measure_tool == "length" and len(self._measure_draft) == 1:
                    _line([self._measure_draft[0], self._hover_world], color_len)
                elif self._measure_tool == "angle":
                    if len(self._measure_draft) == 1:
                        _line([self._measure_draft[0], self._hover_world], color_len)
                    elif len(self._measure_draft) == 2:
                        _line([self._measure_draft[0], self._measure_draft[1]],
                              color_len)
                        _line([self._measure_draft[1], self._hover_world],
                              color_len)

        # CT 实时浮标：仅在尚未放置圆形 ROI 时显示（放置后保留已提交的圆形与均值）
        if (self._measure_tool == "ct" and not has_ct
                and self._hover_world is not None):
            self._add_ct_circle(self._hover_world, self._ct_radius_mm, color_ct)
            self._add_ct_marker(self._hover_world, color_ct)
            if self._hover_text:
                d = self._world_to_display(self._hover_world)
                if d is not None:
                    self._add_measure_actor(
                        self._mk_text_actor(self._hover_text, d, color_ct))

    def _world_to_display(self, w):
        try:
            self._renderer.SetWorldPoint(float(w[0]), float(w[1]), 0.0, 1.0)
            self._renderer.WorldToDisplay()
            d = self._renderer.GetDisplayPoint()
            return (float(d[0]), float(d[1]))
        except Exception:
            return None

    def _display_to_world(self, x, y):
        try:
            self._renderer.SetDisplayPoint(float(x), float(y), 0.0)
            self._renderer.DisplayToWorld()
            p = self._renderer.GetWorldPoint()
            if p[3] not in (0.0, 1.0) and p[3] != 0.0:
                return (float(p[0]) / p[3], float(p[1]) / p[3])
            return (float(p[0]), float(p[1]))
        except Exception:
            return None

    def _world_to_widget(self, w):
        """世界坐标 → 叠加层（相对 VTK 渲染区左上角）的像素坐标"""
        r = self._view_rect()
        size = self._renderer.GetSize()
        if not size or size[0] <= 0 or size[1] <= 0:
            return None
        d = self._world_to_display(w)
        if d is None:
            return None
        rx = d[0] * r.width() / float(size[0])
        ry = r.height() - d[1] * r.height() / float(size[1])
        return QtCore.QPointF(rx, ry)

    def _ct_value_at(self, world):
        """取世界坐标处的 CT 值（HU）。

        关键点（修复「测骨头出负数」的根因）：
        本项目的二维体数据来自 vtkDICOMImageReader，而该读取器在**读取阶段
        已经应用了 Modality LUT**（RescaleSlope / RescaleIntercept），所以
        self._volume_array 里保存的**已经是 HU 值**。这里直接取用即可，
        绝不能再乘一遍 slope/intercept —— 否则会把骨密度（+1000HU 左右）
        算成 1000 + (-1024) = -24 之类的负数。

        图像层在构造时做了 Y 轴翻转（np.flipud），所以世界 Y 要先翻回原始行号。
        """
        if self._volume_array is None:
            return None
        nz, ny, nx, nc = self._volume_shape
        sp = self._spacing or (1.0, 1.0, 1.0)
        sx = float(sp[0]) or 1e-6
        sy = float(sp[1]) or 1e-6
        col = int(round(float(world[0]) / sx))
        row_flipped = int(round(float(world[1]) / sy))
        if col < 0 or col >= nx or row_flipped < 0 or row_flipped >= ny:
            return None
        row = ny - 1 - row_flipped      # 还原成未翻转的数组行号
        k = self._current_slice()
        try:
            raw = self._volume_array[k][row, col]
            if nc > 1:
                raw = raw[0]
            raw = float(raw)
        except Exception:
            return None
        # _volume_array 已经是 HU（vtkDICOMImageReader 已做 Modality LUT），直接返回
        return raw

    def _ct_mean_in_circle(self, world, radius_mm):
        """计算以 world 为圆心、radius_mm 为半径的圆形 ROI 内 CT 值（HU）统计。

        返回 {mean_hu, n, min_hu, max_hu}；ROI 内无像素时返回 None。
        与单点探针同样的 Y 翻转处理：显示行号 = ny-1-数组行号。
        为避免在大半径下逐像素遍历全图，按每行 X 方向的圆内区间剪枝。

        注意：self._volume_array 已经是 HU（vtkDICOMImageReader 读取时已
        应用 Modality LUT），这里直接累加即可，不要再乘 slope/intercept，
        否则会与显示不一致并出现骨密度被测成负数的问题。
        """
        if self._volume_array is None:
            return None
        nz, ny, nx, nc = self._volume_shape
        sp = self._spacing or (1.0, 1.0, 1.0)
        sx = float(sp[0]) or 1e-6
        sy = float(sp[1]) or 1e-6
        wx, wy = float(world[0]), float(world[1])
        R = float(radius_mm)
        if R <= 0:
            return None
        r2 = R * R
        col0 = max(0, int(math.floor((wx - R) / sx)))
        col1 = min(nx - 1, int(math.ceil((wx + R) / sx)))
        row0 = max(0, int(math.floor((wy - R) / sy)))
        row1 = min(ny - 1, int(math.ceil((wy + R) / sy)))
        if col0 > col1 or row0 > row1:
            return None
        k = self._current_slice()
        arr = self._volume_array[k]
        total = 0.0
        n = 0
        mn = None
        mx = None
        for rf in range(row0, row1 + 1):
            dy = rf * sy - wy
            if dy * dy > r2:
                continue
            rem = r2 - dy * dy
            if rem < 0:
                continue
            dxmax = math.sqrt(rem)
            c0 = max(col0, int(math.ceil((wx - dxmax) / sx)))
            c1 = min(col1, int(math.floor((wx + dxmax) / sx)))
            if c0 > c1:
                continue
            arr_row = arr[ny - 1 - rf]
            for c in range(c0, c1 + 1):
                raw = arr_row[c]
                if nc > 1:
                    raw = raw[0]
                raw = float(raw)
                total += raw
                n += 1
                if mn is None or raw < mn:
                    mn = raw
                if mx is None or raw > mx:
                    mx = raw
        if n == 0:
            return None
        mean_raw = total / float(n)
        # _volume_array 已经是 HU，直接返回统计值
        return {
            "mean_hu": mean_raw,
            "n": n,
            "min_hu": mn,
            "max_hu": mx,
        }

    @staticmethod
    def _seg_mm(p, q):
        return math.hypot(q[0] - p[0], q[1] - p[1])

    @staticmethod
    def _angle_deg(a, v, b):
        v1 = (a[0] - v[0], a[1] - v[1])
        v2 = (b[0] - v[0], b[1] - v[1])
        n1 = math.hypot(v1[0], v1[1])
        n2 = math.hypot(v2[0], v2[1])
        if n1 < 1e-9 or n2 < 1e-9:
            return None
        cosv = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
        cosv = max(-1.0, min(1.0, cosv))
        return math.degrees(math.acos(cosv))

    def _on_measure_click(self, x, y):
        w = self._display_to_world(x, y)
        if w is None:
            return
        k = self._current_slice()
        if self._measure_tool == "ct":
            r = self._ct_radius_mm
            res = self._ct_mean_in_circle(w, r)
            mean_hu = res["mean_hu"] if res else None
            n = res["n"] if res else 0
            existing = [it for it in self._measure_items if it.get("type") == "ct"]
            if existing:
                it = existing[0]
                it["world"] = w
                it["radius"] = r
                it["hu"] = mean_hu
                it["n"] = n
                it["slice"] = k
            else:
                self._measure_items.append({
                    "type": "ct", "slice": k, "world": w, "radius": r,
                    "hu": mean_hu, "n": n})
            self._update_measure_overlay()
            return
        if self._measure_tool == "length":
            self._measure_draft.append(w)
            if len(self._measure_draft) >= 2:
                p1, p2 = self._measure_draft[0], self._measure_draft[1]
                self._measure_items.append({
                    "type": "length", "slice": k, "p1": p1, "p2": p2,
                    "mm": self._seg_mm(p1, p2)})
                self._measure_draft = []
            self._update_measure_overlay()
            return
        if self._measure_tool == "angle":
            self._measure_draft.append(w)
            if len(self._measure_draft) >= 3:
                a, v, b = (self._measure_draft[0], self._measure_draft[1],
                           self._measure_draft[2])
                self._measure_items.append({
                    "type": "angle", "slice": k, "p1": a, "pv": v, "p2": b,
                    "deg": self._angle_deg(a, v, b)})
                self._measure_draft = []
            self._update_measure_overlay()
            return

    def _on_measure_hover(self, x, y):
        w = self._display_to_world(x, y)
        if w is None:
            return
        self._hover_world = w
        if self._measure_tool == "ct":
            hu = self._ct_value_at(w)
            if hu is None:
                self._hover_text = ""
            else:
                self._hover_text = (f"{hu:.0f} HU"
                                    if self._dicom_data.get("modality") == "CT"
                                    else f"{hu:.0f}")
        else:
            self._hover_text = ""
        self._update_measure_overlay()

    def _clear_measurements(self):
        self._measure_items = []
        self._measure_draft = []
        self._hover_text = ""
        self._update_measure_overlay()

    def _reset_measurements(self):
        """加载新序列 / 清空时重置所有测量状态"""
        self._measure_tool = "none"
        self._measure_items = []
        self._measure_draft = []
        self._hover_world = None
        self._hover_text = ""
        for btn in (getattr(self, "_ct_btn", None),
                    getattr(self, "_len_btn", None),
                    getattr(self, "_ang_btn", None)):
            if btn is not None:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)
        try:
            self._vtk_widget.setCursor(QtCore.Qt.ArrowCursor)
        except Exception:
            pass
        # 清空 overlay renderer 中的测量 actor（避免残留图形）
        self._update_measure_overlay()

    def _cancel_measure_draft(self):
        if self._measure_draft:
            self._measure_draft = []
            self._update_measure_overlay()

    # ------------------------------------------------------------------
    # 足弓关键点手动微调
    #
    # 模型给出的 5 个足弓关键点通常有 3~5 像素偏差，重训要大量标注数据；
    # 退而求其次的做法是让用户直接在图上拖动这些点、即时重算足弓角。
    #
    # 坐标约定：一律记在**图像像素**里（x 向右、y 向下，与 cv2/pydicom 一致），
    # 画到 overlay 层时才换算成世界坐标。这里换算的依据是
    # ``set_ai_overlay_image`` 里对标注图用的那套 spacing —— 标注图通常与
    # 底图等大，此时直接用底图 spacing；万一不等大（被缩放过），就用当时
    # 按尺寸比例算出来的 spacing，保证手柄落在模型画的点上而不是偏开。
    # ------------------------------------------------------------------
    def _kp_spacing(self):
        """关键点图像像素 → 世界坐标的换算比例 (sx, sy)

        权威依据是 AI 标注图层（``_ai_actor``）自己的 spacing：关键点就是
        要贴在那张图上，手柄和标注图必须用同一套比例，否则图一缩放手柄就偏。
        ``set_ai_overlay_image`` 里按 ``_extent/标注图尺寸`` 反算过一次 spacing，
        这里原样读回来，天然自洽。

        不要改成依赖 ``self._extent`` —— clear() 之后它是 None，换算会静默
        退化成 1:1，手柄落在模型点的旁边（实测真实足部片踩到）。
        """
        sp = None
        try:
            if self._ai_actor is not None:
                img = self._ai_actor.GetInput()
                if img is not None:
                    s = img.GetSpacing()
                    if s and s[0] > 0 and s[1] > 0:
                        sp = (float(s[0]), float(s[1]))
        except Exception:            # noqa: BLE001
            sp = None
        if sp is None:
            sp0 = self._spacing or (1.0, 1.0, 1.0)
            sp = (float(sp0[0]), float(sp0[1]))
        return sp

    def _kp_to_world(self, x, y):
        sx, sy = self._kp_spacing()
        return (float(x) * sx, float(y) * sy)

    def _kp_display_pixel(self, disp_x, disp_y):
        """屏幕显示坐标 → 图像像素（浮点，未取整），越界不裁剪由调用方决定"""
        w = self._display_to_world(disp_x, disp_y)
        if w is None:
            return None
        sx, sy = self._kp_spacing()
        return (w[0] / sx if sx else 0.0, w[1] / sy if sy else 0.0)

    def _kp_clamp(self, x, y):
        """把图像像素约束回图像范围（手柄不允许拖出画面）"""
        nx, ny = self._kp_size or (0, 0)
        if nx > 0:
            x = max(0.0, min(float(nx) - 1.0, x))
        if ny > 0:
            y = max(0.0, min(float(ny) - 1.0, y))
        return (x, y)

    def _kp_hit(self, disp_x, disp_y):
        """抓握测试：返回光标下的手柄 idx，没抓到返回 None"""
        best, best_d = None, 1e9
        for idx, pt in sorted(self._kp_points.items()):
            d = self._world_to_display(self._kp_to_world(*pt))
            if d is None:
                continue
            dist = ((d[0] - disp_x) ** 2 + (d[1] - disp_y) ** 2) ** 0.5
            if dist <= self._kp_grab_px and dist < best_d:
                best, best_d = idx, dist
        return best

    def _kp_clear_actors(self):
        ov = getattr(self, "_overlay_renderer", None)
        for a in getattr(self, "_kp_actors", []) or []:
            try:
                ov.RemoveActor(a)
            except Exception:        # noqa: BLE001
                pass
        self._kp_actors = []

    def _mk_kp_circle_actor(self, cx, cy, r, color_rgb, width=2.0):
        """世界坐标下的一个圆环（手柄），半径以世界单位给出"""
        pts = []
        seg = max(20, min(96, int(r / max(1e-6, min(self._kp_spacing())) * 4)))
        seg = min(seg, 96) or 24
        for i in range(seg + 1):
            a = 2.0 * math.pi * float(i) / seg
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return self._mk_world_line_actor(pts, color_rgb, width=width)

    def _kp_rebuild(self):
        """按当前点位重建 overlay 上的手柄 / 连线 / 角度文字"""
        self._kp_clear_actors()
        if not self._kp_shown:
            return
        ov = getattr(self, "_overlay_renderer", None)
        if ov is None:
            return
        pts = self._kp_points
        idxs = sorted(pts)

        def _add(actor):
            if actor is not None:
                ov.AddActor(actor)
                self._kp_actors.append(actor)

        # 每个手柄的显示色（0~1），连线/文字都要用，先把表准备好：
        # 顺序错一下就会在 cobb 分支里撞 UnboundLocalError
        colors01 = {}
        for idx in idxs:
            raw = self._kp_colors.get(idx, (255, 255, 255))
            colors01[idx] = (raw[0] / 255.0, raw[1] / 255.0, raw[2] / 255.0)

        # 足弓连线：内侧 P1-P2-P3，外侧 P1-P5-P4（与模型可视化一致）
        def _chain(a, b, c):
            if all(k in pts for k in (a, b, c)):
                _add(self._mk_world_line_actor(
                    [self._kp_to_world(*pts[a]) for _ in (a, b, c)],
                    (0.27, 1.0, 0.27), width=2.0))
        if self._kp_shape == "cobb":
            # 脊柱：上下终板线各画一条粗线 + 细延长线，交点标一个环，
            # 读数挂在交点旁（Cobb 角就是这两条线夹的角）
            if all(k in pts for k in (1, 2, 3, 4)):
                up = (tuple(pts[1]), tuple(pts[2]))
                low = (tuple(pts[3]), tuple(pts[4]))
                _add(self._mk_world_line_actor(
                    [self._kp_to_world(*p) for p in up], colors01[1], width=3.0))
                _add(self._mk_world_line_actor(
                    [self._kp_to_world(*p) for p in low], colors01[3], width=3.0))
                e1, e2, inter = _cobb_extension(up, low)
                _add(self._mk_world_line_actor(
                    [self._kp_to_world(*p) for p in e1],
                    (0.55, 0.55, 0.62), width=1.0))
                _add(self._mk_world_line_actor(
                    [self._kp_to_world(*p) for p in e2],
                    (0.55, 0.55, 0.62), width=1.0))
                if inter is not None:
                    wx, wy = self._kp_to_world(*inter)
                    _add(self._mk_kp_circle_actor(
                        wx, wy, float(self._kp_radius_px) * 0.8 * max(self._kp_spacing()),
                        (0.9, 0.9, 1.0)))
        else:
            _chain(1, 2, 3)
            _chain(1, 5, 4)

        # 手柄本体（注意：colors01 上面已按当前点位填好，这里只管画，
        # 不要重新赋空表 —— 会把键清掉，画到第一个手柄就 KeyError）
        for idx in idxs:
            wx, wy = self._kp_to_world(*pts[idx])
            sx, sy = self._kp_spacing()
            r = float(self._kp_radius_px) * max(sx, sy)
            col = colors01[idx]
            drift = self._kp_source.get(idx)
            if drift is not None and tuple(drift) != tuple(pts[idx]):
                # 被手动改过的点套一个白环，和模型原始位置区分开
                d = self._world_to_display(self._kp_to_world(*drift))
                if d is not None:
                    _add(self._mk_text_actor(
                        "原", (d[0] - 12.0, d[1] - 12.0), (1.0, 1.0, 1.0)))
            _add(self._mk_kp_circle_actor(wx, wy, r, col))
            # 手柄实心中心的十字（世界坐标，保证与点位严格对齐）
            _add(self._mk_world_line_actor(
                [(wx - r * 0.45, wy), (wx + r * 0.45, wy),
                 (wx, wy + r * 0.45), (wx, wy - r * 0.45)], col, width=3.0))

        # 名称标签用显示坐标，缩放时字号不变（与既有测量文字一致）
        for idx in idxs:
            d = self._world_to_display(self._kp_to_world(*pts[idx]))
            if d is None:
                continue
            _add(self._mk_text_actor(
                str(self._kp_names.get(idx, "P%d" % idx)), (d[0], d[1]),
                colors01[idx]))

        # 角度读数：脊柱挂两终板线延长线的交点旁（与模型标注图一致），
        # 足弓挂顶点旁（顶点取 P2 / P5，与模型算法一致）
        meds, lats = self.keypoint_angles()
        if self._kp_shape == "cobb":
            if meds is not None and all(k in pts for k in (1, 2, 3, 4)):
                txt = "Cobb %.1f°" % meds
                if self._kp_moved:
                    txt += "（手动调整）"
                _, _, inter = _cobb_extension(
                    (tuple(pts[1]), tuple(pts[2])),
                    (tuple(pts[3]), tuple(pts[4])))
                d = None
                if inter is not None:
                    d = self._world_to_display(self._kp_to_world(*inter))
                if d is None:
                    d = self._world_to_display(self._kp_to_world(*pts[1]))
                if d is not None:
                    _add(self._mk_text_actor(
                        txt, (d[0] + 16.0, d[1] + 14.0), (1.0, 1.0, 1.0)))
        elif meds is not None or lats is not None:
            txt = " ".join([
                "内侧 %.1f°" % meds if meds is not None else "",
                "外侧 %.1f°" % lats if lats is not None else "",
            ]).strip()
            for vertex in (2, 5):
                if vertex in pts:
                    d = self._world_to_display(self._kp_to_world(*pts[vertex]))
                    if d is not None:
                        _add(self._mk_text_actor(
                            txt, (d[0] - 70.0, d[1] + 14.0), (1.0, 1.0, 1.0)))
                        break
        self._render()

    def set_keypoints(self, points, names=None, colors=None, size=None,
                      shape="foot"):
        """装载一批可微调的关键点（图像像素）+ 刷新显示

        ``shape`` 决定 overlay 怎么画、读数怎么算：
        ``"foot"``（默认）画足弓折线、读内外侧角；``"cobb"`` 画上下终板线
        与延长线交点、读 Cobb 角。
        """
        self._kp_source = {int(k): (int(pt[0]), int(pt[1]))
                           for k, pt in dict(points or {}).items()}
        self._kp_points = dict(self._kp_source)
        self._kp_moved = False
        if size:
            self._kp_size = (int(size[0]), int(size[1]))
        if names:
            try:
                self._kp_names = {i + 1: str(names[i])
                                  for i in range(len(names))}
            except Exception:        # noqa: BLE001
                pass
        if colors:
            try:
                self._kp_colors = {i + 1: tuple(colors[i])
                                   for i in range(len(colors))}
            except Exception:        # noqa: BLE001
                self._kp_colors = {}
        self._kp_shape = "foot" if not shape else str(shape)

        def _norm(v):
            return (float(v[0]), float(v[1]))
        self._kp_points = {k: _norm(v) for k, v in self._kp_points.items()}
        self._kp_source = {k: _norm(v) for k, v in self._kp_source.items()}
        self._kp_clamp_all()
        self._kp_rebuild()

    def _kp_clamp_all(self):
        nx, ny = self._kp_size or (0, 0)
        if nx <= 0 or ny <= 0:
            return
        self._kp_points = {k: self._kp_clamp(*v) for k, v in self._kp_points.items()}

    def move_keypoint(self, idx, x, y, emit=True):
        """拖动一个关键点，即时重算并重绘"""
        x, y = self._kp_clamp(float(x), float(y))
        self._kp_points[idx] = (x, y)
        self._kp_moved = True
        self._kp_rebuild()
        if emit:
            self.keypoints_changed.emit({
                "points": dict(self._kp_points),
                "edited": self._kp_moved,
                "angles": self.keypoint_angles(),
            })

    def reset_keypoints(self, emit=True):
        """复位到模型原始点位"""
        self._kp_points = dict(self._kp_source)
        self._kp_moved = False
        self._kp_drag = None
        self._kp_rebuild()
        if emit:
            self.keypoints_changed.emit({
                "points": dict(self._kp_points),
                "edited": False,
                "angles": self.keypoint_angles(),
            })

    def keypoint_angles(self):
        """返回 (角度A, 角度B)，单位是度；算不出为 None。

        足弓（``shape="foot"``）是 (内侧角, 外侧角)：顶点取 P2（内侧）与
        P5（外侧），与 xray 的 foot_arch 推理保持一致，否则手动微调出来的
        角度会和模型原来的读数对不上。
        脊柱（``shape="cobb"``）是 (Cobb 角, None)：由上下终板线两端点算，
        口径同样与模型的 angle_between_lines 同源。
        """
        pts = self._kp_points
        if self._kp_shape == "cobb":
            return (_cobb_angle_from_lines(pts), None)

        def _deg(a, v, b):
            if not all(k in pts for k in (a, v, b)):
                return None
            try:
                v1 = (pts[a][0] - pts[v][0], pts[a][1] - pts[v][1])
                v2 = (pts[b][0] - pts[v][0], pts[b][1] - pts[v][1])
            except Exception:        # noqa: BLE001
                return None
            n1 = math.hypot(v1[0], v1[1])
            n2 = math.hypot(v2[0], v2[1])
            if n1 < 1e-9 or n2 < 1e-9:
                return None
            c = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
            return float(math.degrees(math.acos(c)))

        return (_deg(1, 2, 3), _deg(1, 5, 4))

    def show_keypoints(self, shown: bool):
        """显示 / 隐藏手柄（隐藏时一并清掉 actor）"""
        self._kp_shown = bool(shown)
        if not self._kp_shown:
            self._kp_drag = None
            self._kp_hover = None
        self._kp_rebuild()

    def clear_keypoints(self):
        self._kp_shown = False
        self._kp_drag = None
        self._kp_hover = None
        self._kp_moved = False
        self._kp_points = {}
        self._kp_source = {}
        self._kp_size = None
        self._kp_shape = "foot"
        self._kp_clear_actors()
        self._render()


    # ---------- 事件 ----------
    def showEvent(self, event):
        super().showEvent(event)
        if not self._interactor_initialized:
            interactor = self._vtk_widget.GetRenderWindow().GetInteractor()
            interactor.Initialize()
            interactor.SetInteractorStyle(vtkInteractorStyleUser())
            interactor.AddObserver("MouseMoveEvent", self._on_mouse_move)
            interactor.AddObserver("LeftButtonPressEvent", self._on_left_down)
            interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_up)
            interactor.AddObserver("RightButtonPressEvent", self._on_right_down)
            interactor.AddObserver("RightButtonReleaseEvent", self._on_right_up)
            interactor.AddObserver("MiddleButtonPressEvent", self._on_middle_down)
            interactor.AddObserver("MiddleButtonReleaseEvent", self._on_middle_up)
            interactor.AddObserver("MouseWheelForwardEvent", self._on_wheel_forward)
            interactor.AddObserver("MouseWheelBackwardEvent", self._on_wheel_backward)
            interactor.AddObserver("KeyPressEvent", self._on_key_press)
            interactor.AddObserver("KeyReleaseEvent", self._on_key_release)
            self._interactor_initialized = True
        self._position_widgets()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_widgets()

    def _view_rect(self):
        """VTK 渲染区在本控件坐标系中的矩形（已排除侧边滑条与缩略图条）"""
        try:
            return self._vtk_widget.geometry()
        except Exception:
            return self.rect()

    def _position_widgets(self):
        r = self._view_rect()

        # 底部一排控件：[工具(仅收起时)] [序列] [工具栏]，全部靠左下角排开。
        # 它们会占用左下角，因此**左下角的层数信息标签要抬高到它们上方**，
        # 否则「层数」文字会压住「序列」按钮（实测三者原本全部重叠）。
        # v1.15.0：全屏按钮已并入工具栏，不再单独占位。
        bar = getattr(self, "_measure_bar", None)
        show_btn = getattr(self, "_toolbar_show_btn", None)

        x = r.x() + 8
        if show_btn is not None and show_btn.isVisible():
            show_btn.adjustSize()
            show_btn.move(x, max(0, r.bottom() - show_btn.height() - 8))
            show_btn.raise_()
            x += show_btn.width() + 6

        self._strip_btn.adjustSize()
        self._strip_btn.move(x, max(0, r.bottom() - self._strip_btn.height() - 8))
        self._strip_btn.raise_()

        if bar is not None:
            bar.adjustSize()
            bar.move(x + self._strip_btn.width() + 6,
                     max(0, r.bottom() - bar.height() - 8))
            bar.raise_()

        # 以这些控件中**最高者**的顶部作为遮挡线（不能只用某一个的
        # sizeHint —— 各按钮高度可能不同）。隐藏的控件不参与计算。
        tops = [w for w in (show_btn, self._strip_btn, bar)
                if w is not None and w.isVisible()]
        occl_top = min((w.y() for w in tops), default=r.bottom())
        self._position_overlays(occluded_from_y=occl_top)

    def _position_overlays(self, occluded_from_y=None):
        """摆放四角信息与左右方位字

        ``occluded_from_y``：底部控件（含全屏/序列/测量工具栏）的顶部 y 坐标，
        该线以下的区域不再放角标，避免层数文字压住「序列」按钮。
        """
        m = 10
        # 所有叠加信息都以 VTK 渲染区为基准（缩略图条与侧边滑条各占独立区域）
        r = self._view_rect()

        for lbl in [self._tl_label, self._tr_label, self._bl_label, self._br_label]:
            lbl.adjustSize()

        self._tl_label.move(r.x() + m, r.y() + m)
        self._tr_label.move(
            max(m, r.right() - self._tr_label.width() - m), r.y() + m)

        # 左下角：抬到遮挡线之上；实在放不下（窗口太矮）就贴上边，绝不压按钮
        bl_h = self._bl_label.height()
        bl_y = r.y() + m
        if occluded_from_y is not None and occluded_from_y > r.y():
            bl_y = min(occluded_from_y - bl_h - 4, r.bottom() - bl_h)
        self._bl_label.move(r.x() + m, max(r.y() + m, bl_y))

        br_y = r.y() + m
        if occluded_from_y is not None and occluded_from_y > r.y():
            br_y = min(occluded_from_y - self._br_label.height() - 4,
                       r.bottom() - self._br_label.height())
        self._br_label.move(
            max(m, r.right() - self._br_label.width() - m),
            max(r.y() + m, br_y),
        )

        # 左右方位字（「右」在屏幕左、「左」在屏幕右）垂直居中于**可视区**，
        # 避开底部控件；每侧只有一个，不会重复。
        self._left_char_label.adjustSize()
        self._right_char_label.adjustSize()
        view_h = r.height()
        if occluded_from_y is not None and occluded_from_y > r.y():
            view_h = max(self._left_char_label.height(),
                         occluded_from_y - r.y() - 4)
        cy = r.y() + max(0, (view_h - self._left_char_label.height()) // 2)
        self._left_char_label.move(r.x() + m, cy)
        self._right_char_label.move(
            max(m, r.right() - self._right_char_label.width() - m), cy)

        for lbl in self._all_labels:
            lbl.raise_()

    # ---------- 数据加载 ----------

    def update_layer_text(self, current: int, total: int):
        """只刷新层号显示（副窗口翻层时用，避免重跑整套叠加层计算）

        多窗模式下每个副窗口的 `slice_changed` 不冒泡到 MainWindow，
        由 StudyViewPanel 调用本方法就地更新，避免串扰主窗口的状态栏。
        """
        try:
            if total and total > 0 and int(current) > 0:
                self._tr_label.setText("%d / %d" % (int(current), int(total)))
        except Exception:       # noqa: BLE001
            pass

    def set_window(self, window_width=None, window_center=None):
        """外部设置窗宽窗位（多窗下用于「把本窗窗值同步给其他窗」）"""
        if window_width and window_center is not None:
            self.set_window_level(float(window_width), float(window_center))

    def series_title(self) -> str:
        """当前窗口所载序列的描述（用于窗口标题/提示）"""
        d = self._dicom_data or {}
        for k in ("series_desc", "series_description", "name"):
            v = d.get(k)
            if v:
                return str(v)
        return ""

    def has_image(self) -> bool:
        """本窗口是否已载入图像"""
        return self._reader_output is not None
    def set_reader(self, reader, window_width=None, window_center=None,
                   dicom_data=None, slice_positions=None):
        self._source_reader = reader
        self._dicom_data = dicom_data or {}
        self._thumbnail_ready = False

        if reader is None:
            self.clear()
            return

        # X 线负片（MONOCHROME1）在显示阶段反相；CT 恒为 MONOCHROME2。
        # 必须在 _cache_volume / _apply_slice 之前确定，窗宽窗位由
        # MainWindow 按同一极性推算后传入。
        self._invert_sum = None
        self._display_invert = series_needs_invert(
            reader, (dicom_data or {}).get("modality"))

        reader.Update()
        output = reader.GetOutput()
        if output is None:
            return

        self._origin = output.GetOrigin()
        self._spacing = output.GetSpacing()
        self._extent = output.GetExtent()
        self._reader_output = output

        # 建立 numpy 视图，后续逐层取数据。
        # 不再用 vtkImageFlip 复制整卷（几百层时白白多占一份内存），
        # Y 轴翻转在取单层时用切片一次完成。
        if not self._cache_volume(output):
            return

        if window_width and window_center is not None:
            self.set_window_level(window_width, window_center)
        # 记录序列默认窗宽窗位，便于后续一键复位
        self._default_window_width = self._window_width
        self._default_window_center = self._window_center

        self._slice_positions = slice_positions

        smin, smax = self.slice_range()
        self._sync_slider_range()
        mid = int((smin + smax) // 2)
        self._apply_slice(mid)

        self._fit_camera()

        if self._strip_btn.isChecked():
            self._schedule_thumbnails()

        # 新序列：清空上一序列遗留的测量（含工具栏高亮复位）
        self._reset_measurements()
        self._update_overlays()
        # 相机已在 _fit_camera 配置好，overlay 共享之；重建测量层并渲染
        self._update_measure_overlay()

    def _cache_volume(self, output) -> bool:
        """把 VTK 图像缓存为 numpy 视图 (nz, ny, nx[, nc])"""
        from vtkmodules.util.numpy_support import vtk_to_numpy
        try:
            ext = output.GetExtent()
            nx = int(ext[1] - ext[0] + 1)
            ny = int(ext[3] - ext[2] + 1)
            nz = int(ext[5] - ext[4] + 1)
            nc = int(output.GetNumberOfScalarComponents())
            arr = vtk_to_numpy(output.GetPointData().GetScalars())
        except Exception as e:
            print(f"二维视图：读取图像数据失败 {e}")
            return False
        if nx <= 0 or ny <= 0 or nz <= 0 or arr is None:
            return False
        nc = nc if nc > 1 else 1
        try:
            self._volume_array = (arr.reshape(nz, ny, nx) if nc == 1
                                  else arr.reshape(nz, ny, nx, nc))
        except Exception as e:
            print(f"二维视图：体数据整形失败 {e}")
            return False
        self._volume_shape = (nz, ny, nx, nc)
        self._scalar_type = int(output.GetScalarType())
        return True

    def _invert_polarity(self, src):
        """按 MONOCHROME1 的语义把灰度翻转为正常显示极性。

        对区间中点镜像（x' = min + max - x）而不是简单的 max - x，这样映射
        前后取值范围完全一致，窗宽窗位只需镜像闸值、窗宽不变即可。
        """
        if self._invert_sum is None:
            arr = self._volume_array
            self._invert_sum = float(arr.min()) + float(arr.max())
        out = self._invert_sum - src
        return out.astype(self._volume_array.dtype, copy=False)

    def _build_slice_image(self, index: int):
        """取出第 index 层并构造一张单层二维 vtkImageData（含 Y 轴翻转）"""
        from vtkmodules.util.numpy_support import vtk_to_numpy
        if self._volume_array is None:
            return None
        nz, ny, nx, nc = self._volume_shape
        k = max(0, min(nz - 1, int(index)))
        src = self._volume_array[k]
        # 行序翻转：使图像上下方向符合阅片习惯
        src = np.flipud(src)
        # X 线负片反相（在此处做，窗值映射仍由 ImageActor 的 ColorWindow 完成）。
        # MONOCHROME1 的自动反相与用户手动「反色」是两件独立的事：两者都是
        # 对同一区间做镜像，同时成立会互相抵消，所以用**异或**决定最终极性。
        if (bool(self._display_invert)
                != bool(getattr(self, "_user_invert", False))):
            src = self._invert_polarity(src)
        flat = src.reshape(-1)

        image = vtkImageData()
        image.SetDimensions(nx, ny, 1)
        try:
            sp = self._spacing or (1.0, 1.0, 1.0)
            image.SetSpacing(float(sp[0]), float(sp[1]),
                             max(1e-6, float(sp[2])))
        except Exception:
            image.SetSpacing(1.0, 1.0, 1.0)
        image.SetOrigin(0.0, 0.0, 0.0)
        image.AllocateScalars(self._scalar_type, nc)
        try:
            dest = vtk_to_numpy(image.GetPointData().GetScalars())
            dest.reshape(-1)[:] = flat
        except Exception as e:
            print(f"二维视图：第 {k} 层拷贝失败 {e}")
            return None
        return image

    def set_ai_overlay_image(self, image_rgb):
        """显示 / 隐藏 X 线 AI 标注结果图。

        xray 各检测模块返回的 ``result_image`` 已经把中文标签、Cobb 角弧线、
        骨龄关节标注等都画好了，整幅替换是还原度最高也最省事的做法。
        传 ``None`` 即恢复原始影像。

        翻转约定必须与 ``_build_slice_image`` 完全一致（同样 np.flipud），
        否则标注图会相对原图上下镜像。
        """
        if self._ai_actor is None:
            return

        if image_rgb is None:
            self._ai_actor.SetVisibility(False)
            if self._image_actor is not None:
                self._image_actor.SetVisibility(True)
            self._render()
            return


        try:
            from vtkmodules.util.numpy_support import numpy_to_vtk
            arr = np.asarray(image_rgb)
            if arr.ndim == 2:
                arr = np.stack([arr] * 3, axis=-1)
            arr = np.ascontiguousarray(arr[:, :, :3].astype(np.uint8))
            # 注意：这里**不能**照抄 _build_slice_image 的 np.flipud。
            # _build_slice_image 的输入是 vtkDICOMImageReader 的输出，行序为
            # 「下→上」；而 xray 返回的 result_image 来自 pydicom/cv2，行序是
            # 「上→下」（与磁盘上的 DICOM 像素顺序一致）。两者行序相反，
            # 若再 flipud 一次就会上下镜像（实测已复现）。
            ny, nx = int(arr.shape[0]), int(arr.shape[1])
            if nx <= 0 or ny <= 0:
                return
            # 记下标注图尺寸：足弓关键点微调要按这套 spacing 把图像像素
            # 换算成世界坐标，标注图若与底图不等大（被缩放过）必须用它自己的
            # 比例，否则手柄会落在模型点的旁边而不是点上。
            self._ai_size = (nx, ny)

            image = vtkImageData()
            image.SetDimensions(nx, ny, 1)
            image.SetOrigin(0.0, 0.0, 0.0)

            # 标注图尺寸通常与原图一致；万一不一致（例如被缩放），按比例换算
            # spacing，保证两者覆盖同一物理范围，不会出现标注框错位。
            o_sp = self._spacing or (1.0, 1.0, 1.0)
            sx, sy = float(o_sp[0]), float(o_sp[1])
            try:
                if self._extent is not None:
                    onx = int(self._extent[1] - self._extent[0] + 1)
                    ony = int(self._extent[3] - self._extent[2] + 1)
                    if onx > 0 and ony > 0:
                        sx = o_sp[0] * onx / float(nx)
                        sy = o_sp[1] * ony / float(ny)
            except Exception:
                pass
            image.SetSpacing(sx, sy, max(1e-6, float(o_sp[2])))

            vtk_arr = numpy_to_vtk(arr.reshape(-1), deep=True)
            vtk_arr.SetNumberOfComponents(3)
            image.GetPointData().SetScalars(vtk_arr)

            self._ai_actor.SetInputData(image)
            # 标注图是 uint8 RGB，窗宽窗位固定满量程，避免沿用 CT 的 HU 窗值
            self._ai_actor.GetProperty().SetColorWindow(255.0)
            self._ai_actor.GetProperty().SetColorLevel(127.5)
            self._ai_actor.SetVisibility(True)
            if self._image_actor is not None:
                self._image_actor.SetVisibility(False)
            self._render()
        except Exception as exc:          # noqa: BLE001
            print("显示 AI 标注图失败: %s" % exc)

    def set_ct_measure_enabled(self, enabled: bool):
        """X 线影像没有 HU 物理意义，CT 值工具需要禁用"""
        btn = getattr(self, "_ct_btn", None)
        if btn is None:
            return
        btn.setEnabled(bool(enabled))
        if not enabled and btn.isChecked():
            btn.setChecked(False)

    def _fit_camera(self):
        """按当前图像尺寸重置相机（正交 + 自适应缩放）

        观察侧必须与 MPR 轴位视图（`vtkImageViewer2`）保持一致：**相机位于
        -Z 一侧**、ViewUp = (0,-1,0)。vtkImageViewer2 的 XY 方向相机位置为
        (0,0,-1)，即屏幕右方向指向世界 +X；若二维把相机放在 +Z 一侧，
        屏幕右方向会变成 -X，画面相对三维视图整体左右镜像。
        """
        renderer = self._renderer
        camera = renderer.GetActiveCamera()
        # overlay renderer 共享同一相机，保证测量图形与图像同步平移/缩放
        try:
            self._overlay_renderer.SetActiveCamera(camera)
        except Exception:
            pass
        camera.ParallelProjectionOn()
        # 翻转后，Y 轴向下为图像上方（保持与 DICOM 阅片一致）
        camera.SetViewUp(0, -1, 0)
        # ResetCamera 会沿用相机当前的视线方向，只重新计算距离。
        # 因此先强制一个确定的方向（相机在 +Z 一侧），保证下面的镜像是幂等的，
        # 否则第二次加载时相机已在 -Z 侧，再镜像一次就会翻回 +Z。
        camera.SetFocalPoint(0.0, 0.0, 0.0)
        camera.SetPosition(0.0, 0.0, 1.0)
        camera.SetViewUp(0, -1, 0)
        renderer.ResetCamera()
        renderer.ResetCameraClippingRange()
        # 此时相机必然位于 +Z 一侧，镜像到 -Z 一侧与三维视图一致
        pos = camera.GetPosition()
        fp = camera.GetFocalPoint()
        dz = pos[2] - fp[2]
        camera.SetPosition(pos[0], pos[1], fp[2] - dz)
        renderer.ResetCameraClippingRange()
        camera.Zoom(1.15)

    def clear(self):
        try:
            self._image_actor.SetVisibility(False)
        except Exception:
            pass
        # 断开输入连接并隐藏，避免残留数据影响下次渲染
        try:
            self._image_actor.SetInputData(None)
        except Exception:
            pass
        # AI 标注层一并清掉，否则换序列后旧标注会盖在新图上
        try:
            self._ai_actor.SetVisibility(False)
            self._ai_actor.SetInputData(None)
        except Exception:
            pass
        # 手柄同样属于「上一次检测的产物」，换序列必须跟着走
        self.clear_keypoints()
        self._ai_size = None
        self._reader_output = None
        self._volume_array = None
        self._volume_shape = None
        self._scalar_type = None
        self._slice_image = None
        self._source_reader = None
        self._origin = None
        self._spacing = None
        self._extent = None
        self._display_invert = False
        self._invert_sum = None
        self._dicom_data = {}
        self._slice_positions = None
        self._thumbnail_ready = False
        self._thumb_pixmaps = []
        self._thumb_labels = []
        self._strip.clear()
        self._slice_slider.blockSignals(True)
        self._slice_slider.setValue(0)
        self._slice_slider.blockSignals(False)
        self._slice_slider.hide()
        self._reset_measurements()
        for lbl in self._all_labels:
            lbl.hide()
        self._render()

    # ---------- 窗宽窗位 ----------
    def set_window_level(self, window: float, level: float):
        def _safe(v, lo, hi, default):
            try:
                vv = float(v)
            except Exception:
                return default
            if vv < lo or vv > hi:
                return default
            return vv

        # 上限放宽到 16bit 量纲：X 线（DR/DX）的窗值远大于 CT 的 HU
        # （实测 WW≈7000 / WL≈12880）。沿用 CT 的 6000/1500 会把合理窗值
        # 判为越界并兜底成 400/40，导致 X 线整幅发黑。
        # CT 侧的越界值已由 MainWindow._plausible_window 在上游拦掉，
        # 因此放宽这里不会让 CT 出现异常窗值。
        window = _safe(window, 1.0, 100000.0, 400.0)
        level = _safe(level, -40000.0, 40000.0, 40.0)
        self._window_width = window
        self._window_center = level
        # 窗宽窗位直接作用于 ImageActor 的 ImageProperty（映射在渲染时完成，
        # 因此改窗值无需重建数据，实时生效且与切片同步无关）
        self._image_actor.GetProperty().SetColorWindow(window)
        self._image_actor.GetProperty().SetColorLevel(level)
        self._update_overlays()
        self._render()

    def get_window_level(self):
        return self._window_width, self._window_center

    # ---------- 切片控制 ----------
    def slice_range(self):
        """返回 (最小层索引, 最大层索引)，基于缓存的体数据"""
        if self._volume_shape is None:
            return (0, 0)
        return (0, int(self._volume_shape[0]) - 1)

    def _apply_slice(self, index: int):
        """取出该层数据生成二维图像交给 ImageActor（不触发重绘）"""
        if self._volume_array is None:
            return
        image = self._build_slice_image(index)
        if image is None:
            return
        _nz, ny, nx, _nc = self._volume_shape
        self._slice_image = image
        # 每次换层都替换为一个全新的数据对象，确保 OpenGL 纹理被重新上传，
        # 避免出现「只有前几层有图像、后续层全黑」的问题
        self._image_actor.SetInputData(image)
        self._image_actor.SetDisplayExtent(0, nx - 1, 0, ny - 1, 0, 0)
        self._image_actor.SetVisibility(True)
        self._current_index = int(index)

    def set_slice(self, index: int):
        if self._volume_array is None:
            return
        smin, smax = self.slice_range()
        index = max(smin, min(smax, int(index)))
        self._apply_slice(index)
        self._sync_slider_value(index)
        self._update_overlays()
        self._strip.set_current_index(index - smin)
        try:
            self.slice_changed.emit(index - smin + 1, smax - smin + 1)
        except Exception:
            pass
        # 翻层后当前层测量需重建（仅本层可见）
        self._update_measure_overlay()

    def step_slice(self, delta: int):
        """相对当前层移动若干层（方向键/翻页键使用）"""
        if self._volume_array is None:
            return
        smin, smax = self.slice_range()
        self.set_slice(max(smin, min(smax, self._current_slice() + int(delta))))

    def goto_slice(self, index: int):
        """跳到绝对层索引（0 基）"""
        self.set_slice(int(index))

    def first_slice(self):
        smin, _ = self.slice_range()
        self.set_slice(smin)

    def last_slice(self):
        _, smax = self.slice_range()
        self.set_slice(smax)

    def reset_window_level(self):
        """恢复到序列默认的窗宽窗位（R 键）

        刻意**不参与多窗广播**：各窗的默认窗值本就不同（a 窗肺窗、
        b 窗纵隔窗），一个窗按R 复位不应把别的窗一起改掉。
        """
        self.set_window_level(self._default_window_width,
                              self._default_window_center)

    def _current_slice(self):
        if self._volume_array is None:
            return 0
        return int(getattr(self, "_current_index", 0))

    # ---------- 侧边滑条 ----------
    def _sync_slider_range(self):
        smin, smax = self.slice_range()
        total = smax - smin + 1
        if total <= 1:
            self._slice_slider.hide()
            return
        self._slice_slider.blockSignals(True)
        self._slice_slider.setMinimum(smin)
        self._slice_slider.setMaximum(smax)
        self._slice_slider.setSingleStep(1)
        self._slice_slider.setPageStep(max(1, total // 20))
        self._slice_slider.setValue(getattr(self, "_current_index", smin))
        self._slice_slider.blockSignals(False)
        self._slice_slider.show()
        self._slice_slider.raise_()

    def _sync_slider_value(self, index: int):
        if self._slice_slider.minimum() == self._slice_slider.maximum():
            return
        if self._slice_slider.value() == index:
            return
        self._slice_slider.blockSignals(True)
        self._slice_slider.setValue(int(index))
        self._slice_slider.blockSignals(False)

    def _on_slider_changed(self, value: int):
        if self._volume_array is None:
            return
        if int(value) == self._current_slice():
            return
        self.set_slice(int(value))

    # ---------- 键盘控制 ----------
    def eventFilter(self, obj, event):
        # VTK 控件上的按键先由本控件处理，用于方向键翻层
        if obj is self._vtk_widget and event.type() == QtCore.QEvent.KeyPress:
            if self._handle_slice_key(event.key()):
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if self._handle_slice_key(event.key()):
            return
        super().keyPressEvent(event)

    def _handle_slice_key(self, key) -> bool:
        if self._volume_array is None:
            return False
        mapping = {
            QtCore.Qt.Key_Up: -1,
            QtCore.Qt.Key_Down: 1,
            QtCore.Qt.Key_PageUp: -10,
            QtCore.Qt.Key_PageDown: 10,
        }
        if key in mapping:
            self.step_slice(mapping[key])
            return True
        if key == QtCore.Qt.Key_Home:
            self.first_slice()
            return True
        if key == QtCore.Qt.Key_End:
            self.last_slice()
            return True
        if key == QtCore.Qt.Key_R:
            self.reset_window_level()
            return True
        if key == QtCore.Qt.Key_Escape:
            self._cancel_measure_draft()
            return True
        return False

    def _on_thumb_requested(self, index: int):
        smin, _ = self.slice_range()
        self.set_slice(smin + int(index))

    # ---------- 渲染 ----------
    def _render(self):
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win is not None:
            render_win.Render()

    # ---------- 交互 ----------
    def _on_left_down(self, obj, event):
        x, y = obj.GetEventPosition()
        # 关键点微调优先：左键落在手柄上就是「抓住它」，不进入测量下点
        if self._kp_shown and not self._measure_active():
            idx = self._kp_hit(x, y)
            if idx is not None:
                self._kp_drag = idx
                try:
                    self._vtk_widget.setCursor(QtCore.Qt.SizeAllCursor)
                except Exception:        # noqa: BLE001
                    pass
                return
        # 选择模式：左键拖动改为平移视图（原来只有中键能平移）
        if getattr(self, "_select_mode", False):
            self._select_panning = True
            self._select_last_pos = (x, y)
            try:
                self._vtk_widget.setCursor(QtCore.Qt.ClosedHandCursor)
            except Exception:        # noqa: BLE001
                pass
            self._vtk_widget.setFocus()
            return
        # 左键仅用于「测量模式下下点」，不再平移（用户不需要左键平移）
        if self._measure_active():
            self._on_measure_click(x, y)
        self._vtk_widget.setFocus()

    def _on_left_up(self, obj, event):
        if self._kp_drag is not None:
            self._kp_drag = None
            try:
                self._vtk_widget.setCursor(
                    QtCore.Qt.SizeAllCursor if self._kp_hover
                    else QtCore.Qt.ArrowCursor)
            except Exception:        # noqa: BLE001
                pass
        # 选择模式：结束拖动，光标回到「可抓取」状态
        if getattr(self, "_select_panning", False):
            self._select_panning = False
            self._select_last_pos = None
            try:
                self._vtk_widget.setCursor(QtCore.Qt.OpenHandCursor)
            except Exception:        # noqa: BLE001
                pass
        self._mouse_down = False
        self._last_mouse_pos = None

    def _on_middle_down(self, obj, event):
        self._middle_mouse_down = True
        self._last_mouse_pos = obj.GetEventPosition()

    def _on_middle_up(self, obj, event):
        self._middle_mouse_down = False
        self._last_mouse_pos = None

    def _on_right_down(self, obj, event):
        self._right_mouse_down = True
        self._last_mouse_pos = obj.GetEventPosition()

    def _on_right_up(self, obj, event):
        self._right_mouse_down = False
        self._last_mouse_pos = None

    def _on_key_press(self, obj, event):
        key = obj.GetKeySym() if hasattr(obj, "GetKeySym") else ""
        if key in ("Control_L", "Control_R"):
            self._ctrl_down = True
        # CT 工具激活时，[ ] 调整圆形 ROI 半径
        if self._measure_tool == "ct" and key in (
                "bracketleft", "bracketright"):
            self._adjust_ct_radius(
                1.0 / 1.1 if key == "bracketleft" else 1.1)

    def _on_key_release(self, obj, event):
        key = obj.GetKeySym() if hasattr(obj, "GetKeySym") else ""
        if key in ("Control_L", "Control_R"):
            self._ctrl_down = False

    def _on_mouse_move(self, obj, event):
        x, y = obj.GetEventPosition()

        # 关键点微调：拖动中的手柄跟随光标，抓不到的点清掉悬停态
        if self._kp_drag is not None:
            px = self._kp_display_pixel(x, y)
            if px is not None:
                self.move_keypoint(self._kp_drag, px[0], px[1], emit=False)
                self._kp_rebuild()
                self.keypoints_changed.emit({
                    "points": dict(self._kp_points),
                    "edited": True,
                    "angles": self.keypoint_angles(),
                })
            return
        if self._kp_shown:
            hover = self._kp_hit(x, y)
            if hover != self._kp_hover:
                self._kp_hover = hover
                try:
                    self._vtk_widget.setCursor(
                        QtCore.Qt.SizeAllCursor if hover is not None
                        else QtCore.Qt.ArrowCursor)
                except Exception:        # noqa: BLE001
                    pass

        # 选择模式：左键拖动 = 平移视图（与中键平移同一套换算）
        if getattr(self, "_select_panning", False):
            last = getattr(self, "_select_last_pos", None)
            if last is not None:
                self._pan(x - last[0], y - last[1])
            self._select_last_pos = (x, y)
            self._render()
            return

        # 中键拖动：平移
        if self._middle_mouse_down and self._last_mouse_pos is not None:
            dx = x - self._last_mouse_pos[0]
            dy = y - self._last_mouse_pos[1]
            self._pan(dx, dy)
            self._last_mouse_pos = (x, y)
            return

        # 右键拖动：调窗
        if self._right_mouse_down:
            if self._last_mouse_pos is None:
                self._last_mouse_pos = (x, y)
                return
            dx = x - self._last_mouse_pos[0]
            dy = y - self._last_mouse_pos[1]
            new_width = max(1.0, self._window_width + dx * 2.0)
            new_center = self._window_center + dy * 2.0
            self._window_width = new_width
            self._window_center = new_center
            self._image_actor.GetProperty().SetColorWindow(new_width)
            self._image_actor.GetProperty().SetColorLevel(new_center)
            self._last_mouse_pos = (x, y)
            self._update_overlays()
            self._render()
            return

        # 测量工具：无按键拖动时实时跟随光标（CT 值实时读数 / 橡皮筋预览）
        if self._measure_active():
            self._on_measure_hover(x, y)
            return

    def _pan(self, dx: int, dy: int):
        if self._reader_output is None:
            return
        camera = self._renderer.GetActiveCamera()
        scale = camera.GetParallelScale()
        viewport = self._renderer.GetSize()
        height = viewport[1] if viewport and viewport[1] > 0 else self.height() or 1
        # 将像素位移换算为世界坐标位移
        factor = (2.0 * scale) / float(max(1, height))
        pos = camera.GetPosition()
        fp = camera.GetFocalPoint()
        offset = (-dx * factor, -dy * factor, 0.0)
        camera.SetPosition(pos[0] + offset[0], pos[1] + offset[1], pos[2])
        camera.SetFocalPoint(fp[0] + offset[0], fp[1] + offset[1], fp[2])
        self._renderer.ResetCameraClippingRange()
        # 相机移动后文字(显示坐标)需重新定位，重建测量 actor
        self._update_measure_overlay()

    def _zoom(self, step: float):
        if self._reader_output is None:
            return
        camera = self._renderer.GetActiveCamera()
        camera.Zoom(step)
        self._renderer.ResetCameraClippingRange()
        self._update_measure_overlay()

    def _on_wheel_forward(self, obj, event):
        if self._reader_output is None:
            return
        # CT 工具激活时，滚轮用于调整圆形 ROI 半径（Ctrl/中键仍缩放）
        if self._measure_tool == "ct" and not (
                self._ctrl_down or self._middle_mouse_down):
            self._adjust_ct_radius(1.1)
            return
        if self._ctrl_down or self._middle_mouse_down:
            self._zoom(1.12)
            return
        current = self._current_slice()
        _, smax = self.slice_range()
        if current < smax:
            self.set_slice(current + 1)

    def _on_wheel_backward(self, obj, event):
        if self._reader_output is None:
            return
        if self._measure_tool == "ct" and not (
                self._ctrl_down or self._middle_mouse_down):
            self._adjust_ct_radius(1.0 / 1.1)
            return
        if self._ctrl_down or self._middle_mouse_down:
            self._zoom(1.0 / 1.12)
            return
        current = self._current_slice()
        smin, _ = self.slice_range()
        if current > smin:
            self.set_slice(current - 1)

    def _adjust_ct_radius(self, factor: float):
        """调整圆形 ROI 半径（mm），并据新半径重算已放置 ROI 的平均值"""
        mn, mx = 0.5, 100.0
        self._ct_radius_mm = max(mn, min(mx, self._ct_radius_mm * factor))
        for it in self._measure_items:
            if it.get("type") == "ct":
                res = self._ct_mean_in_circle(it["world"], self._ct_radius_mm)
                it["radius"] = self._ct_radius_mm
                it["hu"] = res["mean_hu"] if res else None
                it["n"] = res["n"] if res else 0
        self._update_measure_overlay()

    # ---------- 信息叠加 ----------
    def _update_overlays(self):
        if not self._dicom_data:
            return

        name = self._dicom_data.get("patient_name", "Unknown")
        pid = self._dicom_data.get("patient_id", "-")
        self._tl_label.setText(f"{name}\nID: {pid}")

        series = self._dicom_data.get("series_desc", "Series")
        self._tr_label.setText(f"{series}\nW:{int(self._window_width)} L:{int(self._window_center)}")

        smin, smax = self.slice_range()
        total = smax - smin + 1 if smax >= smin else 0
        curr = self._current_slice() - smin + 1

        # 层位置（mm）
        pos_str = "-"
        try:
            if self._slice_positions is not None:
                idx = max(0, min(len(self._slice_positions) - 1, curr - 1))
                pos_str = f"{self._slice_positions[idx]:.1f} mm"
            elif self._origin is not None and self._spacing is not None:
                pos_str = f"{self._origin[2] + self._current_slice() * self._spacing[2]:.1f} mm"
        except Exception:
            pos_str = "-"

        modality = self._dicom_data.get("modality", "CT")
        self._bl_label.setText(f"层 {curr}/{total}\n{pos_str}")

        thickness = self._dicom_data.get("slice_thickness")
        pixel_spacing = self._dicom_data.get("pixel_spacing_str", "-")
        thickness_str = f"{thickness:.2f}mm" if isinstance(thickness, (int, float)) and thickness else "-"
        self._br_label.setText(f"{modality}  层厚:{thickness_str}\n像素:{pixel_spacing}")

        for lbl in self._all_labels:
            lbl.show()
        self._position_overlays()

    # ---------- 缩略图 ----------
    def _on_strip_toggled(self, checked: bool):
        if checked:
            self._strip.show()
            self._position_widgets()
            self._position_overlays()   # 底部信息需为缩略图条让位
            self._schedule_thumbnails()
        else:
            self._strip.hide()
            self._position_widgets()
            self._position_overlays()

    def _schedule_thumbnails(self):
        # 由 VTK 事件触发时延迟到主线程执行，避免在渲染回调中重入
        self._thumb_refresh_timer.start(0)

    def _build_thumbnails(self):
        if self._source_reader is None or self._volume_array is None:
            return
        if self._thumbnail_ready:
            return
        try:
            nz, _ny, _nx, _nc = self._volume_shape
            total = nz
            if total <= 0:
                return

            self._thumb_pixmaps = []
            self._thumb_labels = []

            if total <= self.THUMB_SAMPLE_LIMIT:
                indices = list(range(total))
            else:
                step = total / float(self.THUMB_SAMPLE_LIMIT)
                indices = [int(i * step) for i in range(self.THUMB_SAMPLE_LIMIT)]

            lo = self._window_center - self._window_width / 2.0
            inv = 1.0 / max(1.0, self._window_width)

            for k in indices:
                arr = self._extract_slice(
                    self._volume_array, self._volume_shape, k)
                if arr is None:
                    continue
                # 缩略图与主视图必须同极性（X 线负片同样要反相），
                # 否则缩略图会与主画面一正一负
                if self._display_invert:
                    arr = self._invert_polarity(arr)
                # 预降采样到约 2 倍缩略图边长，把逐像素运算量降低 ~95%，
                # 窗值映射为逐点线性操作，先降采样不影响结果
                h0, w0 = arr.shape
                sy = max(1, h0 // (ThumbnailStrip.THUMB_H * 2))
                sx = max(1, w0 // (ThumbnailStrip.THUMB_W * 2))
                if sy > 1 or sx > 1:
                    arr = arr[::sy, ::sx]
                arr = arr.astype(np.float32)
                arr = np.clip((arr - lo) * inv, 0.0, 1.0)
                img8 = (arr * 255.0).astype(np.uint8)

                h, w = img8.shape
                qimg = QtGui.QImage(img8.tobytes(), w, h, w, QtGui.QImage.Format_Grayscale8)
                pm = QtGui.QPixmap.fromImage(qimg.copy()).scaled(
                    ThumbnailStrip.THUMB_W, ThumbnailStrip.THUMB_H,
                    QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                self._thumb_pixmaps.append(pm)
                self._thumb_labels.append(str(k + 1))

            if self._thumb_pixmaps:
                self._strip.set_thumbnails(self._thumb_pixmaps, self._thumb_labels)
                smin, _ = self.slice_range()
                self._strip.set_current_index(self._current_slice() - smin)
                self._thumbnail_ready = True
        except Exception as e:
            print(f"缩略图生成失败: {e}")

    @staticmethod
    def _extract_slice(volume_array, volume_shape, k):
        """从 numpy 体数据中提取第 k 层（含 Y 轴翻转）的二维数组"""
        if volume_array is None or volume_shape is None:
            return None
        try:
            return np.flipud(volume_array[int(k)])
        except Exception:
            return None


class SliceView(QtWidgets.QFrame):
    label_clicked = QtCore.pyqtSignal()
    zoom_changed = QtCore.pyqtSignal(float)

    @staticmethod
    def _make_placeholder_image():
        """构造 1x1 的单分量占位图像，避免 viewer 空输入时刷错误日志"""
        img = vtkImageData()
        img.SetDimensions(1, 1, 1)
        img.AllocateScalars(3, 1)   # 3 = VTK_UNSIGNED_SHORT
        img.SetScalarComponentFromDouble(0, 0, 0, 0, 0.0)
        return img

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vtk_widget = QVTKRenderWindowInteractor(self)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._vtk_widget)

        self._viewer = vtkImageViewer2()
        self._viewer.SetRenderWindow(self._vtk_widget.GetRenderWindow())
        self._viewer.SetupInteractor(self._vtk_widget.GetRenderWindow().GetInteractor())
        self._viewer.GetRenderer().SetBackground(0.1, 0.1, 0.1)
        # vtkImageViewer2 内部的 vtkImageMapToWindowLevelColors 在无输入时
        # 每次渲染都会刷 "Input port 0 ... has 0 connections" 错误日志，
        # 这里先喂一个 1x1 占位图像，等 set_reader 时再替换为真实数据。
        self._viewer.SetInputData(self._make_placeholder_image())
        self._interactor_initialized = False
        self._picker = vtkPointPicker()
        self._picker.PickFromListOn()
        self._cursor = None
        self._cursor_actor = None
        self._cursor_callback = None
        self._origin = None
        self._spacing = None
        self._extent = None
        self._orientation = "axial"
        self._reslice = None
        self._reslice_filter = None
        self._last_ijk = None
        self._source_reader = None
        self._flip_filter = None
        self._flip_filter_x = None
        self._mouse_down = False
        self._right_mouse_down = False
        self._middle_mouse_down = False
        self._last_mouse_pos = None
        self._window_center = None
        self._window_width = None
        self._zoom_level = 1.0
        self._coordinate_converter = CoordinateConverter()
        self._cursor_synchronizer = None
        self._segmentation_mask = None
        self._segmentation_import = None
        self._segmentation_actor = None
        self._segmentation_buffer = None
        self._fullscreen_expanded = False
        # —— 三维四视图统一工具栏 ——
        # 四窗工具栏一致：前三个 MPR 窗默认折叠，只留左下角「工具」入口；
        # 3D 专属按钮（切割/测长/多视角/撤销/重置/清测量）委托给第 4 窗的
        # 三维模型执行，勾选/可用状态与第 4 窗实时同步（见 _sync_toolbar_state）。
        self._volume_peer = None
        self._tool_bar = QtWidgets.QWidget(self)
        self._tool_bar_layout = QtWidgets.QHBoxLayout(self._tool_bar)
        self._tool_bar_layout.setContentsMargins(0, 0, 0, 0)
        self._tool_bar_layout.setSpacing(4)
        theme.bind(self._tool_bar, tool_bar_qss)

        # 选择（委托到三维：左键拖动平移）
        self._select_btn = self._add_tool_button(
            "select", "选择",
            "选择：开启后鼠标左键拖动为平移视图（作用到三维模型）；再点一次退出",
            checkable=True)
        self._select_btn.clicked.connect(lambda: self._delegate_toggle("select"))
        # 反色（委托到三维：模型/背景明暗反相）
        self._invert_btn = self._add_tool_button(
            "invert", "反色",
            "反色：把三维模型与背景的明暗反相显示（只影响显示，不改数据）",
            checkable=True)
        self._invert_btn.clicked.connect(lambda: self._delegate_toggle("invert"))
        # 多视角（委托到三维：六视角导出）
        self._save_mview_btn = self._add_tool_button(
            "multiview", "多视角",
            "保存多视角：把三维模型按六个视角一次性导出为 PNG 图片")
        self._save_mview_btn.clicked.connect(self._delegate_action("multiview"))
        # 截图（本窗：保存当前 MPR 层面为单张 PNG）
        self._screenshot_btn = self._add_tool_button(
            "camera", "截图",
            "截图：保存当前层面（矢状位/冠状位/横断位）为单张 PNG 图片")
        self._screenshot_btn.clicked.connect(self._on_screenshot_clicked)
        # 切割（委托到三维）
        self._cut_btn = self._add_tool_button(
            "cut", "切割",
            "切割：在三维模型上圈画要去掉的部分，松开即删除；再点一次退出。",
            checkable=True, color=ui_icons.ICON_WARN)
        self._cut_btn.clicked.connect(lambda: self._delegate_toggle("cut"))
        # 撤销 / 重置（委托到三维）
        self._cut_undo_btn = self._add_tool_button(
            "undo", "撤销", "撤销：撤销上一次切割", enabled=False)
        self._cut_undo_btn.clicked.connect(self._delegate_action("cut_undo"))
        self._cut_reset_btn = self._add_tool_button(
            "reset", "重置", "重置：恢复切割前的完整三维模型", enabled=False)
        self._cut_reset_btn.clicked.connect(self._delegate_action("cut_reset"))
        # 测长（委托到三维）
        self._measure3d_btn = self._add_tool_button(
            "measure3d", "测长",
            "三维测长：在三维模型上依次点两点，量两点间距离(mm)；再点一次退出。",
            checkable=True)
        self._measure3d_btn.clicked.connect(lambda: self._delegate_toggle("measure3d"))
        self._measure3d_clear_btn = self._add_tool_button(
            "measure_clear", "清测量",
            "清测量：清除三维窗口中的所有长度测量", enabled=False)
        self._measure3d_clear_btn.clicked.connect(
            self._delegate_action("measure_clear"))
        # 全屏（本窗）
        self._fullscreen_btn = self._add_tool_button(
            "maximize", "全屏", "全屏：把本视图最大化，再点一次还原")
        self._fullscreen_btn.clicked.connect(self._on_fullscreen_clicked)
        # 收起工具栏（本窗）
        self._toolbar_hide_btn = self._add_tool_button(
            "eye_off", "隐藏",
            "隐藏：收起本工具栏（左下角保留「工具」按钮可再次展开）")
        self._toolbar_hide_btn.clicked.connect(
            lambda: self._set_toolbar_visible(False))
        # 收起后重新展开的小按钮
        self._toolbar_show_btn = QtWidgets.QToolButton(self)
        self._toolbar_show_btn.setFixedHeight(TOOL_BTN_H)
        theme.bind(self._toolbar_show_btn, tool_btn_qss)
        ui_icons.apply_tool_button(self._toolbar_show_btn, "tools", "工具",
                                   "工具：重新展开工具栏", size=20, icon_size=18)
        self._toolbar_show_btn.clicked.connect(
            lambda: self._set_toolbar_visible(True))

        # 前三个 MPR 窗默认折叠工具栏（第 4 窗由 VTKViewer 保持展开）
        self._tool_bar.setVisible(False)
        self._toolbar_show_btn.setVisible(True)

        # MPR信息标签（右下角）
        self._info_label = QtWidgets.QLabel(self)
        # 使用更深且不透明的背景，并增加边框，防止渲染残留
        theme.bind(self._info_label, theme.qss_mpr_info)
        self._info_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, False)
        self._info_label.setCursor(QtCore.Qt.PointingHandCursor)
        self._info_label.installEventFilter(self)
        self._info_label.hide()

        # DICOM叠加信息标签
        # 左上角：患者姓名、ID
        self._top_left_label = QtWidgets.QLabel(self)
        # 右上角：序列描述、窗宽窗位
        self._top_right_label = QtWidgets.QLabel(self)
        # 左下角：切片信息、位置
        self._bottom_left_label = QtWidgets.QLabel(self)
        
        # 统一设置叠加信息样式
        for lbl in [self._top_left_label, self._top_right_label, self._bottom_left_label]:
            theme.bind(lbl, lambda: theme.qss_overlay_label(
                font_family="'Segoe UI', 'Microsoft YaHei', sans-serif"))
            # 允许点击穿透，不影响 VTK 交互
            lbl.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            lbl.hide()
            
        self._dicom_data = {} # 存储当前显示的 DICOM 元数据

        # 添加“左” “右” 汉字标签
        self._left_char_label = QtWidgets.QLabel("右", self)  # 屏幕左侧显示“右”
        self._right_char_label = QtWidgets.QLabel("左", self) # 屏幕右侧显示“左”
        
        for _lbl in (self._left_char_label, self._right_char_label):
            theme.bind(_lbl, lambda: theme.qss_char_label("24pt"))
        self._left_char_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._right_char_label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._left_char_label.hide()
        self._right_char_label.hide()
        self._orientation_visible = True   # 由 QuadViewer.set_orientation_visible 统一管控（2D 关闭）

    # ---------- 四窗统一工具栏（委托 + 同步） ----------
    def _add_tool_button(self, name, text, tip, checkable=False,
                         color=None, enabled=True):
        """往底部工具栏追加一个「图标 + 汉字」按钮（与 VTKViewer 同一套）。"""
        btn = QtWidgets.QToolButton(self._tool_bar)
        btn.setCheckable(checkable)
        btn.setFixedHeight(TOOL_BTN_H)
        btn.setEnabled(bool(enabled))
        ui_icons.apply_tool_button(btn, name, text, tip, size=20, icon_size=18,
                                   color=(color or ui_icons.ICON_COLOR))
        self._tool_bar_layout.addWidget(btn)
        return btn

    # 三维窗（VTKViewer）上各按钮对应的处理器方法名
    _TOGGLE_HANDLERS = {
        "select": "_on_select3d_toggled",
        "invert": "_on_invert3d_toggled",
        "cut": "_on_cut_toggled",
        "measure3d": "_on_measure3d_toggled",
    }
    _ACTION_HANDLERS = {
        "multiview": "_on_save_multiview_clicked",
        "cut_undo": "_on_cut_undo",
        "cut_reset": "_on_cut_reset",
        "measure_clear": "_clear_measure3d",
    }

    def set_volume_peer(self, peer):
        """把三维窗口（VTKViewer）设为委托目标；3D 按钮都转发给它。"""
        self._volume_peer = peer
        self._sync_toolbar_state()

    def _delegate_toggle(self, key):
        """把某个可勾选按钮的点击转发给三维窗按钮（触发其完整处理器），
        再统一拉回勾选/可用状态，保证四窗一致。"""
        peer = self._volume_peer
        lb = getattr(self, "_%s_btn" % key, None)
        if peer is None:
            if lb is not None:
                lb.blockSignals(True)
                lb.setChecked(False)
                lb.blockSignals(False)
            return
        pb = getattr(peer, "_%s_btn" % key, None)
        if pb is not None:
            pb.click()   # 走三维窗按钮的完整点击流程（含 toggled/clicked 处理器）
        QtCore.QTimer.singleShot(0, self._sync_toolbar_state)

    def _delegate_action(self, key):
        """把某个动作按钮（撤销/重置/清测量/多视角）转发给三维窗处理器。"""
        def _fire():
            peer = self._volume_peer
            if peer is None:
                return
            handler = getattr(peer, self._ACTION_HANDLERS[key], None)
            if handler is not None:
                try:
                    handler()
                except Exception:        # noqa: BLE001
                    pass
            QtCore.QTimer.singleShot(0, self._sync_toolbar_state)
        return _fire

    def _sync_toolbar_state(self):
        """把本窗工具栏的勾选/可用状态对齐到三维窗（四窗一致）。"""
        peer = self._volume_peer
        if peer is None:
            return
        for local_name in ("_select_btn", "_invert_btn", "_cut_btn",
                           "_measure3d_btn"):
            lb = getattr(self, local_name, None)
            pb = getattr(peer, local_name, None)
            if lb is None or pb is None:
                continue
            lb.blockSignals(True)
            lb.setChecked(pb.isChecked())
            lb.setEnabled(pb.isEnabled())
            lb.blockSignals(False)
        for local_name in ("_cut_undo_btn", "_cut_reset_btn",
                           "_measure3d_clear_btn"):
            lb = getattr(self, local_name, None)
            pb = getattr(peer, local_name, None)
            if lb is None or pb is None:
                continue
            lb.setEnabled(pb.isEnabled())

    def _position_toolbar(self):
        """把底部工具栏整体摆到左下角（折叠时只摆「工具」入口）。"""
        bar = getattr(self, "_tool_bar", None)
        show_btn = getattr(self, "_toolbar_show_btn", None)
        m = 6
        if show_btn is not None:
            if show_btn.isVisible():
                show_btn.adjustSize()
                show_btn.move(m, self.height() - show_btn.height() - m)
                show_btn.raise_()
        if bar is None:
            return
        bar.adjustSize()
        y = max(0, self.height() - bar.height() - m)
        x = m
        if show_btn is not None and show_btn.isVisible():
            x = m + show_btn.width() + 6
        bar.move(x, y)
        bar.raise_()

    def _set_toolbar_visible(self, visible: bool):
        """显示/隐藏底部工具栏，并保留「工具」入口按钮。"""
        bar = getattr(self, "_tool_bar", None)
        show_btn = getattr(self, "_toolbar_show_btn", None)
        if bar is None:
            return
        bar.setVisible(bool(visible))
        if show_btn is not None:
            show_btn.setVisible(not bool(visible))
        self._position_toolbar()

    def _update_dicom_overlays(self):
        """更新 DICOM 叠加信息内容"""
        if not self._dicom_data:
            return
            
        # 左上角：患者姓名 (Name), 患者ID (ID)
        name = self._dicom_data.get('patient_name', 'Unknown')
        pid = self._dicom_data.get('patient_id', '-')
        self._top_left_label.setText(f"{name}\n{pid}")
        
        # 右上角：序列描述 (Series), 窗宽/位 (W/L)
        series = self._dicom_data.get('series_desc', 'Series')
        ww = int(self._window_width) if self._window_width else '-'
        wl = int(self._window_center) if self._window_center else '-'
        self._top_right_label.setText(f"{series}\nW:{ww} L:{wl}")
        
        # 左下角：切片位置 (Slice), 坐标 (Pos)
        s_idx = self._viewer.GetSlice()
        s_max = self._viewer.GetSliceMax()
        s_min = self._viewer.GetSliceMin()
        total = s_max - s_min + 1 if s_max >= s_min else 0
        curr = s_idx - s_min + 1
        
        # 计算当前物理坐标
        pos_str = "-"
        if self._coordinate_converter and self._extent:
            # 根据不同视图方向计算当前切片的物理位置
            origin = self._origin
            spacing = self._spacing
            if origin and spacing:
                if self._orientation == "axial":
                    pos = origin[2] + s_idx * spacing[2]
                    pos_str = f"Z: {pos:.1f} mm"
                elif self._orientation == "sagittal":
                    pos = origin[0] + s_idx * spacing[0]
                    pos_str = f"X: {pos:.1f} mm"
                elif self._orientation == "coronal":
                    pos = origin[1] + s_idx * spacing[1]
                    pos_str = f"Y: {pos:.1f} mm"
                    
        self._bottom_left_label.setText(f"Slice: {curr}/{total}\n{pos_str}")
        
        self._top_left_label.show()
        self._top_right_label.show()
        self._bottom_left_label.show()
        # 方位字仅在三维模式显示。关键：除 _orientation_visible 标志外，
        # 还必须满足 self.isVisible() —— 即整条父链（QuadViewer → _quad_host）
        # 当前真的可见。2D 阅读时 _quad_host 被 hide()，isVisible() 为 False，
        # 因此无论翻层/缩放/resize 哪条刷新路径触发本方法，三维 MPR 的「左/右」
        # 都不会漏到二维，也不会与 Dicom2DReader 的「左/右」叠加成重字。
        if self.isVisible() and getattr(self, "_orientation_visible", True):
            self._left_char_label.show()
            self._right_char_label.show()
        self._position_overlays()

    def _position_overlays(self):
        """调整叠加标签的位置"""
        m = 10 # 边距
        
        # 左上角
        self._top_left_label.adjustSize()
        self._top_left_label.move(m, m)
        
        # 右上角
        self._top_right_label.adjustSize()
        self._top_right_label.move(self.width() - self._top_right_label.width() - m, m)
        
        # 左下角 (在底部工具栏/「工具」入口上方)
        self._bottom_left_label.adjustSize()
        bar_h = 0
        bar = getattr(self, "_tool_bar", None)
        show_btn = getattr(self, "_toolbar_show_btn", None)
        if bar is not None and bar.isVisible():
            bar_h = bar.height()
        elif show_btn is not None and show_btn.isVisible():
            bar_h = show_btn.height()
        self._bottom_left_label.move(
            m, self.height() - self._bottom_left_label.height() - bar_h - m - 5)
        
        # "右" 在屏幕左侧
        self._left_char_label.adjustSize()
        self._left_char_label.move(m, (self.height() - self._left_char_label.height()) // 2)
        
        # "左" 在屏幕右侧
        self._right_char_label.adjustSize()
        self._right_char_label.move(self.width() - self._right_char_label.width() - m, (self.height() - self._right_char_label.height()) // 2)
        
        # 确保标签置顶
        self._top_left_label.raise_()
        self._top_right_label.raise_()
        self._bottom_left_label.raise_()
        self._left_char_label.raise_()
        self._right_char_label.raise_()

    def eventFilter(self, obj, event):
        if obj == self._info_label and event.type() == QtCore.QEvent.MouseButtonPress:
            if event.button() == QtCore.Qt.LeftButton:
                self.label_clicked.emit()
                return True
        return super().eventFilter(obj, event)

    def _transform_coordinate(self, world_pos):
        """处理坐标转换（考虑视图翻转）"""
        if not self._flip_filter and not self._flip_filter_x:
            return world_pos

        # 未初始化图像属性或世界坐标为非法值时，直接返回原值，避免 numpy 报错
        if (self._coordinate_converter.origin is None
                or self._coordinate_converter.spacing is None):
            return world_pos
        try:
            world_pos = tuple(float(v) for v in world_pos)
        except (TypeError, ValueError):
            return world_pos
        if any(v != v for v in world_pos):   # NaN 检测
            return world_pos
        if any(self._coordinate_converter.spacing <= 0):
            return world_pos

        # 转换到 IJK
        i, j, k = self._coordinate_converter.world_to_ijk(*world_pos)
        
        # 获取范围
        extent = self._extent
        if not extent:
            return world_pos
            
        # 应用翻转逻辑（正向和逆向变换公式相同）
        if self._orientation in ["axial", "sagittal"]:
            # Y轴翻转: j_new = j_max + j_min - j_old
            j = extent[2] + extent[3] - j
            
        if self._orientation == "coronal":
            # X轴翻转: i_new = i_max + i_min - i_old
            i = extent[0] + extent[1] - i
            
        # 转回 World
        return self._coordinate_converter.ijk_to_world(i, j, k)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._interactor_initialized:
            interactor = self._vtk_widget.GetRenderWindow().GetInteractor()
            interactor.Initialize()
            interactor.SetInteractorStyle(vtkInteractorStyleUser())
            interactor.AddObserver("MouseMoveEvent", self._on_mouse_move)
            interactor.AddObserver("LeftButtonPressEvent", self._on_left_down)
            interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_up)
            interactor.AddObserver("RightButtonPressEvent", self._on_right_down)
            interactor.AddObserver("RightButtonReleaseEvent", self._on_right_up)
            interactor.AddObserver("MiddleButtonPressEvent", self._on_middle_down)
            interactor.AddObserver("MiddleButtonReleaseEvent", self._on_middle_up)
            interactor.AddObserver("MouseWheelForwardEvent", self._on_wheel_forward)
            interactor.AddObserver("MouseWheelBackwardEvent", self._on_wheel_backward)
            self._interactor_initialized = True
        self._position_toolbar()
        self._position_info_label()
        self._position_overlays()
        self._tool_bar.raise_()
        self._toolbar_show_btn.raise_()
        self._info_label.raise_()
        self._top_left_label.raise_()
        self._top_right_label.raise_()
        self._bottom_left_label.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_toolbar()
        self._position_info_label()
        self._position_overlays()
        self._tool_bar.raise_()
        self._toolbar_show_btn.raise_()
        self._info_label.raise_()
        self._top_left_label.raise_()
        self._top_right_label.raise_()
        self._bottom_left_label.raise_()
        # QVTKRenderWindowInteractor 会自动处理绘制，无需手动 update 防止某些驱动下闪退

    def _on_fullscreen_clicked(self):
        parent = self.parent()
        while parent is not None and not isinstance(parent, QuadViewer):
            parent = parent.parent()
        if parent is not None:
            parent.toggle_maximize(self)

    def set_fullscreen_button(self, expanded: bool):
        """外部（QuadViewer 最大化切换）同步本窗「全屏」按钮按下态。"""
        self._fullscreen_expanded = expanded
        btn = getattr(self, "_fullscreen_btn", None)
        if btn is None:
            return
        btn.blockSignals(True)
        btn.setChecked(bool(expanded))
        btn.blockSignals(False)
        btn.setToolTip("退出最大化" if expanded
                       else "全屏：把本视图最大化，再点一次还原")

    # ------------------------------------------------------------------
    # 截图（把当前层面——矢状位/冠状位/横断位——保存为单张 PNG）
    # ------------------------------------------------------------------
    ORIENTATION_LABELS = {
        "axial": "横断位",
        "sagittal": "矢状位",
        "coronal": "冠状位",
    }

    def _capture_window(self, scale=2):
        """把当前层面渲染窗口抓成 vtkImageData。

        OpenGL 画面必须用 vtkWindowToImageFilter，Qt 的 grab 抓不到。
        """
        render_win = self._vtk_widget.GetRenderWindow()
        render_win.Render()
        w2i = vtkWindowToImageFilter()
        w2i.SetInput(render_win)
        w2i.SetInputBufferTypeToRGB()
        w2i.ShouldRerenderOff()   # 已手动 Render，避免重复渲染
        if scale and scale > 1:
            w2i.SetScale(int(scale))
        w2i.Modified()
        w2i.Update()
        return w2i.GetOutput()

    def save_screenshot(self, folder, scale=2):
        """保存当前层面为单张 PNG，文件名含方位与时间戳（重复截图不覆盖）。"""
        img = self._capture_window(scale)
        label = self.ORIENTATION_LABELS.get(self._orientation, "层面")
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(folder, "MPR_%s_%s.png" % (label, ts))
        writer = vtkPNGWriter()
        writer.SetInputData(img)
        writer.SetFileName(path)
        writer.Write()
        return path

    def _on_screenshot_clicked(self):
        inp = self._viewer.GetInput()
        # 未载入序列时 viewer 里是 1x1x1 的占位图像，不应导出
        if inp is None or max(inp.GetDimensions()) <= 1:
            QtWidgets.QMessageBox.information(
                self, "提示", "当前没有可截图的影像，请先载入序列。")
            return
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择截图保存目录", os.path.expanduser("~"))
        if not folder:
            return
        try:
            path = self.save_screenshot(folder, scale=2)
        except Exception as exc:   # 任何失败都弹窗，不静默崩溃
            QtWidgets.QMessageBox.critical(
                self, "截图失败", "保存截图出错：\n%s" % exc)
            return
        QtWidgets.QMessageBox.information(
            self, "截图完成", "已保存当前层面截图：\n%s" % path)

    def set_orientation(self, orientation: str):
        self._orientation = orientation
        if orientation == "axial":
            self._viewer.SetSliceOrientationToXY()
        elif orientation == "sagittal":
            self._viewer.SetSliceOrientationToYZ()
        elif orientation == "coronal":
            self._viewer.SetSliceOrientationToXZ()
        self._viewer.GetRenderer().ResetCamera()
        
        # 使用标准化方向管理确保一致性
        camera = self._viewer.GetRenderer().GetActiveCamera()
        settings = ViewDirectionManager.get_standard_camera_settings(orientation)
        camera.SetViewUp(*settings["view_up"])
        
        # 特殊处理矢状位，确保正确的竖向显示（上下不反转）
        if orientation == "sagittal":
            # 矢状位需要特殊的相机角度：Z轴向下以避免上下反转
            camera.SetViewUp(0, 0, -1)  # Z轴向下
            
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def set_input(self, reader):
        self._source_reader = reader
        reader.Update()
        output = reader.GetOutput()
        
        # 重置所有过滤器
        self._flip_filter = None
        self._flip_filter_x = None
        try:
            self._viewer.GetRenderer().RemoveAllViewProps()
        except Exception:
            pass
        self._viewer.SetInputConnection(None)
        
        # 根据不同方位设置不同的翻转
        if self._orientation == "axial":
            # 轴向需要Y轴翻转（上下翻转）
            self._flip_filter = vtkImageFlip()
            self._flip_filter.SetFilteredAxis(1)  # Y轴翻转
            self._flip_filter.SetInputConnection(reader.GetOutputPort())
            self._viewer.SetInputConnection(self._flip_filter.GetOutputPort())
        elif self._orientation == "sagittal":
            # 矢状位需要Y轴翻转以保持与横断位一致
            self._flip_filter = vtkImageFlip()
            self._flip_filter.SetFilteredAxis(1)  # Y轴翻转
            self._flip_filter.SetInputConnection(reader.GetOutputPort())
            self._viewer.SetInputConnection(self._flip_filter.GetOutputPort())
        elif self._orientation == "coronal":
            # 冠状位采用X轴翻转，统一左右方向
            self._flip_filter_x = vtkImageFlip()
            self._flip_filter_x.SetFilteredAxis(0)  # X轴翻转
            self._flip_filter_x.SetInputConnection(reader.GetOutputPort())
            self._viewer.SetInputConnection(self._flip_filter_x.GetOutputPort())
            self._viewer.SetSliceOrientationToXZ()
        actor = self._viewer.GetImageActor()
        if actor is not None:
            actor.SetVisibility(True)
            self._viewer.GetRenderer().AddActor(actor)
        
        self._viewer.GetRenderer().ResetCamera()
        
        camera = self._viewer.GetRenderer().GetActiveCamera()
        camera.ParallelProjectionOn()
        
        # 使用标准化方向管理
        settings = ViewDirectionManager.get_standard_camera_settings(self._orientation)
        camera.SetViewUp(*settings["view_up"])
        
        self._viewer.GetRenderer().ResetCamera()
        camera = self._viewer.GetRenderer().GetActiveCamera()
        
        extent = output.GetExtent()
        spacing = output.GetSpacing()
        origin = output.GetOrigin()
        
        # 初始化坐标转换器
        self._coordinate_converter.set_image_properties(origin, spacing, extent)
        
        # 设置初始光标位置 - 统一设置为左上角
        if self._orientation == "coronal":
            # 冠状位特殊处理：设置为左上角位置
            initial_i = extent[0]  # 最小X值
            initial_j = extent[3]  # 最大Y值（左上角）
            initial_k = (extent[5] - extent[4]) // 2  # 中心Z值
        else:
            # 其他视图保持中心位置
            initial_i = (extent[1] - extent[0]) // 2
            initial_j = (extent[3] - extent[2]) // 2  
            initial_k = (extent[5] - extent[4]) // 2
            
        initial_world = self._coordinate_converter.ijk_to_world(initial_i, initial_j, initial_k)
        
        print(f"INIT_DEBUG: {self._orientation}视图初始光标位置 - IJK:({initial_i}, {initial_j}, {initial_k}), 世界坐标:{initial_world}")
        
        # 设置初始光标
        self.set_cursor_world(initial_world)
        
        # 如果有同步器，也初始化同步器的位置
        if self._cursor_synchronizer:
            self._cursor_synchronizer.set_world_position(initial_world, self._orientation)
        
        width = (extent[1] - extent[0]) * spacing[0]
        height = (extent[3] - extent[2]) * spacing[1]
        depth = (extent[5] - extent[4]) * spacing[2]
        max_dim = max(width, height, depth)
        
        # 计算数据中心
        center = (
            origin[0] + width/2,
            origin[1] + height/2,
            origin[2] + depth/2
        )
        
        # 使用标准化相机设置，但为矢状位和冠状位做特殊处理
        if self._orientation == "sagittal":
            # 矢状位特殊处理：相机在X轴负方向，向上为-Z轴（避免上下反转）
            camera.SetViewUp(0, 0, -1)
            camera.SetPosition(center[0] - max_dim * 2, center[1], center[2])
            camera.SetFocalPoint(*center)
        elif self._orientation == "coronal":
            settings = ViewDirectionManager.get_standard_camera_settings(self._orientation)
            camera.SetViewUp(*settings["view_up"])
            camera.SetPosition(center[0], center[1] - max_dim * 2, center[2])
            camera.SetFocalPoint(*center)
        else:
            # 其他视图使用标准化设置
            ViewDirectionManager.adjust_camera_for_consistency(
                camera, self._orientation, center, max_dim
            )
        
        self._viewer.GetRenderer().ResetCameraClippingRange()
        self._viewer.GetRenderer().ResetCamera()
        smin = self._viewer.GetSliceMin()
        smax = self._viewer.GetSliceMax()
        if smin is not None and smax is not None and smin <= smax:
            mid = int((smin + smax) // 2)
            self._viewer.SetSlice(mid)
        camera.Zoom(1.6)
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()
        
        self._origin = origin
        self._spacing = spacing
        self._extent = extent
        self._picker.InitializePickList()
        self._picker.AddPickList(self._viewer.GetImageActor())
        self._update_segmentation_overlay()
        if self._cursor is None:
            self._cursor = vtkSphereSource()
            self._cursor.SetRadius(1.5)
            self._cursor.SetThetaResolution(12)
            self._cursor.SetPhiResolution(12)
            mapper = vtkPolyDataMapper()
            mapper.SetInputConnection(self._cursor.GetOutputPort())
            self._cursor_actor = vtkActor()
            self._cursor_actor.SetMapper(mapper)
            self._cursor_actor.GetProperty().SetColor(1.0, 0.0, 0.0)
            self._viewer.GetRenderer().AddActor(self._cursor_actor)

    def set_window_level(self, window: float, level: float):
        # 设置窗宽/窗位，钳制非法值避免白屏
        def _safe(v, lo, hi, default):
            try:
                vv = float(v)
            except Exception:
                return default
            if vv < lo or vv > hi:
                return default
            return vv
        window = _safe(window, 1.0, 6000.0, 400.0)
        level = _safe(level, -1500.0, 1500.0, 50.0)
        self._window_width = window
        self._window_center = level
        self._viewer.SetColorWindow(window)
        self._viewer.SetColorLevel(level)
        
        # 更新信息标签
        self._info_label.setText(f"W: {int(window)} L: {int(level)}")
        self._info_label.show()
        self._position_info_label()
        
        # 更新 DICOM 叠加信息
        self._update_dicom_overlays()
        
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def update_info_label(self, window: float, level: float):
        """仅更新标签内容，不触发现渲染（用于修复重叠显示）"""
        self._info_label.setText(f"W: {int(window)} L: {int(level)}")
        self._info_label.show()
        self._position_info_label()

    def _position_info_label(self):
        if not self._info_label.isVisible():
            return
        m = 8
        s = self._info_label.sizeHint()
        # 确保标签在右下角，且不会超出边界
        self._info_label.setFixedSize(s)
        self._info_label.move(self.width() - s.width() - m, self.height() - s.height() - m)
        self._info_label.raise_()

    def clear(self):
        try:
            self._viewer.GetRenderer().RemoveAllViewProps()
            self._viewer.SetInputConnection(None)
            try:
                actor = self._viewer.GetImageActor()
                if actor:
                    actor.SetVisibility(False)
            except Exception:
                pass
        except Exception:
            pass
        if self._segmentation_actor is not None:
            self._viewer.GetRenderer().RemoveActor(self._segmentation_actor)
        self._segmentation_actor = None
        self._segmentation_import = None
        self._segmentation_buffer = None
        self._segmentation_mask = None
        self._origin = None
        self._spacing = None
        self._extent = None
        if self._cursor_actor is not None:
            self._viewer.GetRenderer().RemoveActor(self._cursor_actor)
        self._cursor = None
        self._cursor_actor = None
        self._picker.InitializePickList()
        
        # 清理 DICOM 叠加信息
        self._dicom_data = {}
        self._top_left_label.hide()
        self._top_right_label.hide()
        self._bottom_left_label.hide()
        self._left_char_label.hide()
        self._right_char_label.hide()
        self._info_label.hide()
        
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def set_slice(self, index: int):
        if not self._viewer or self._viewer.GetInput() is None:
            return
        slice_min, slice_max = self.slice_range()
        if slice_min is None or slice_max is None:
            return
        index = max(slice_min, min(slice_max, index))
        self._viewer.SetSlice(index)
        self._update_segmentation_overlay()
        
        # 更新叠加信息中的切片位置
        self._update_dicom_overlays()
        
        # 确保 render window 有效
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def slice_range(self):
        if self._viewer is None or self._viewer.GetInput() is None:
            return (0, 0)
        slice_min = self._viewer.GetSliceMin()
        slice_max = self._viewer.GetSliceMax()
        if slice_min is None or slice_max is None or slice_min > slice_max:
            return (0, 0)
        return (slice_min, slice_max)

    def clear_segmentation_mask(self):
        self._segmentation_mask = None
        self._segmentation_buffer = None
        if self._segmentation_actor is not None:
            self._viewer.GetRenderer().RemoveActor(self._segmentation_actor)
        self._segmentation_actor = None
        self._segmentation_import = None
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def set_segmentation_mask(self, mask):
        self._segmentation_mask = mask
        self._update_segmentation_overlay()

    def _build_segmentation_rgba(self, slice_arr):
        import numpy as np
        labels = np.unique(slice_arr)
        labels = [int(x) for x in labels if int(x) > 0]
        if not labels:
            return None
        palette = [
            (0.94, 0.33, 0.31),
            (0.30, 0.74, 0.93),
            (0.50, 0.80, 0.50),
            (0.98, 0.77, 0.19),
            (0.59, 0.40, 0.74),
            (1.00, 0.60, 0.40),
            (0.35, 0.78, 0.98),
            (0.87, 0.30, 0.86),
        ]
        h, w = slice_arr.shape
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        alpha = 120
        for lbl in labels:
            color = palette[(lbl - 1) % len(palette)]
            r = int(color[0] * 255)
            g = int(color[1] * 255)
            b = int(color[2] * 255)
            mask = slice_arr == lbl
            rgba[mask, 0] = r
            rgba[mask, 1] = g
            rgba[mask, 2] = b
            rgba[mask, 3] = alpha
        return rgba

    def _update_segmentation_overlay(self):
        if self._segmentation_mask is None or self._extent is None:
            if self._segmentation_actor is not None:
                self._segmentation_actor.SetVisibility(False)
            return
        if self._viewer is None or self._viewer.GetInput() is None:
            return
        import numpy as np
        from vtkmodules.vtkIOImage import vtkImageImport
        mask = self._segmentation_mask
        if not hasattr(mask, "shape") or mask.ndim != 3:
            return
        ext = self._extent
        slice_idx = self._viewer.GetSlice()
        if self._orientation == "axial":
            if slice_idx < ext[4] or slice_idx > ext[5]:
                return
            z_index = slice_idx - ext[4]
            slice_arr = mask[z_index, :, :]
            slice_arr = slice_arr[::-1, :]
            rgba = self._build_segmentation_rgba(slice_arr)
            if rgba is None:
                if self._segmentation_actor is not None:
                    self._segmentation_actor.SetVisibility(False)
                return
            data = rgba[None, ...]
            data_extent = (ext[0], ext[1], ext[2], ext[3], slice_idx, slice_idx)
        elif self._orientation == "sagittal":
            if slice_idx < ext[0] or slice_idx > ext[1]:
                return
            x_index = slice_idx - ext[0]
            slice_arr = mask[:, :, x_index]
            slice_arr = slice_arr[:, ::-1]
            rgba = self._build_segmentation_rgba(slice_arr)
            if rgba is None:
                if self._segmentation_actor is not None:
                    self._segmentation_actor.SetVisibility(False)
                return
            data = rgba[:, :, None, :]
            data_extent = (slice_idx, slice_idx, ext[2], ext[3], ext[4], ext[5])
        else:
            if slice_idx < ext[2] or slice_idx > ext[3]:
                return
            y_index = slice_idx - ext[2]
            slice_arr = mask[:, y_index, :]
            slice_arr = slice_arr[:, ::-1]
            rgba = self._build_segmentation_rgba(slice_arr)
            if rgba is None:
                if self._segmentation_actor is not None:
                    self._segmentation_actor.SetVisibility(False)
                return
            data = rgba[:, None, :, :]
            data_extent = (ext[0], ext[1], slice_idx, slice_idx, ext[4], ext[5])
        self._segmentation_buffer = np.ascontiguousarray(data)
        importer = vtkImageImport()
        importer.SetDataScalarTypeToUnsignedChar()
        importer.SetNumberOfScalarComponents(4)
        data_string = self._segmentation_buffer.tobytes()
        importer.CopyImportVoidPointer(data_string, len(data_string))
        importer.SetDataExtent(*data_extent)
        importer.SetWholeExtent(*data_extent)
        if self._spacing:
            importer.SetDataSpacing(*self._spacing)
        if self._origin:
            importer.SetDataOrigin(*self._origin)
        importer.Update()
        if self._segmentation_actor is None:
            actor = vtkImageActor()
            actor.SetInputData(importer.GetOutput())
            self._viewer.GetRenderer().AddActor(actor)
            self._segmentation_actor = actor
        else:
            self._segmentation_actor.SetInputData(importer.GetOutput())
            self._segmentation_actor.SetVisibility(True)
        self._segmentation_import = importer
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()

    def set_cursor_callback(self, callback):
        self._cursor_callback = callback

    def set_cursor_world(self, world_pos):
        if self._cursor is None or world_pos is None:
            return
            
        # 接收到的 world_pos 是原始坐标，需要转换为显示坐标
        display_pos = self._transform_coordinate(world_pos)
        
        self._cursor.SetCenter(*display_pos)
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()
        
    def set_cursor_synchronizer(self, synchronizer: CursorSynchronizer):
        """设置光标同步器"""
        self._cursor_synchronizer = synchronizer

    def _on_mouse_move(self, obj, event):
        x, y = self._viewer.GetRenderWindow().GetInteractor().GetEventPosition()
        
        if self._mouse_down:
            if self._origin is None or self._spacing is None or self._extent is None:
                return
            if not self._picker.Pick(x, y, 0, self._viewer.GetRenderer()):
                return
            display_world = self._picker.GetPickPosition()
            
            # 将拾取到的显示坐标转换为原始坐标
            real_world = self._transform_coordinate(display_world)
            
            # 使用坐标转换器进行精确转换
            i, j, k = self._coordinate_converter.world_to_ijk(real_world[0], real_world[1], real_world[2])
            
            # 直接更新光标同步器 (传递原始坐标)
            if self._cursor_synchronizer:
                self._cursor_synchronizer.update_cursor_position(i, j, k, self._orientation)
            
            # 同时调用原始回调保持兼容性
            if self._cursor_callback:
                self._cursor_callback((i, j, k))
            
            # 实时更新当前视图的光标显示 (传入原始坐标，set_cursor_world 会自动转换回显示坐标)
            self.set_cursor_world(real_world)
            return
        
        if self._right_mouse_down:
            if self._last_mouse_pos is None:
                self._last_mouse_pos = (x, y)
                return
            dx = x - self._last_mouse_pos[0]
            dy = y - self._last_mouse_pos[1]
            if self._window_width is not None and self._window_center is not None:
                new_width = max(1, self._window_width + dx * 2)
                new_center = self._window_center + dy * 2
                self._window_width = new_width
                self._window_center = new_center
                self._viewer.SetColorWindow(new_width)
                self._viewer.SetColorLevel(new_center)
                # 仅更新当前视图的标签，防止重叠
                self.update_info_label(new_width, new_center)
                render_win = self._vtk_widget.GetRenderWindow()
                if render_win:
                    render_win.Render()
            self._last_mouse_pos = (x, y)
            return

    def _on_left_down(self, obj, event):
        self._mouse_down = True

    def _on_left_up(self, obj, event):
        self._mouse_down = False

    def _on_right_down(self, obj, event):
        self._right_mouse_down = True
        self._last_mouse_pos = self._viewer.GetRenderWindow().GetInteractor().GetEventPosition()

    def _on_right_up(self, obj, event):
        self._right_mouse_down = False
        self._last_mouse_pos = None

    def _on_middle_down(self, obj, event):
        self._middle_mouse_down = True

    def _on_middle_up(self, obj, event):
        self._middle_mouse_down = False

    def set_zoom(self, zoom_factor: float, emit_signal: bool = False):
        """设置缩放比例"""
        if self._viewer.GetInput() is None:
            return
            
        self._zoom_level = zoom_factor
        camera = self._viewer.GetRenderer().GetActiveCamera()
        
        # 获取基础平行尺度 (根据图像大小)
        extent = self._extent
        spacing = self._spacing
        if extent and spacing:
            # 基础高度
            base_height = (extent[3] - extent[2] + 1) * spacing[1]
            # ParallelScale 是视口高度的一半
            # zoom_factor 越大，ParallelScale 越小 (显示内容越少，即放大)
            camera.SetParallelScale((base_height / 2.0) / zoom_factor)
            
        # 确保 render window 有效
        render_win = self._vtk_widget.GetRenderWindow()
        if render_win:
            render_win.Render()
        
        if emit_signal:
            self.zoom_changed.emit(zoom_factor)

    def _on_wheel_forward(self, obj, event):
        if self._viewer.GetInput() is None:
            return
        if self._middle_mouse_down:
            new_zoom = min(10.0, self._zoom_level + 0.1)
            self.set_zoom(new_zoom, emit_signal=True)
        else:
            current = self._viewer.GetSlice()
            max_slice = self._viewer.GetSliceMax()
            if current < max_slice:
                self._viewer.SetSlice(current + 1)
                render_win = self._vtk_widget.GetRenderWindow()
                if render_win:
                    render_win.Render()

    def _on_wheel_backward(self, obj, event):
        if self._viewer.GetInput() is None:
            return
        if self._middle_mouse_down:
            new_zoom = max(0.1, self._zoom_level - 0.1)
            self.set_zoom(new_zoom, emit_signal=True)
        else:
            current = self._viewer.GetSlice()
            min_slice = self._viewer.GetSliceMin()
            if current > min_slice:
                self._viewer.SetSlice(current - 1)
                render_win = self._vtk_widget.GetRenderWindow()
                if render_win:
                    render_win.Render()


class QuadViewer(QtWidgets.QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._layout = layout

        self._axial_view = SliceView()
        self._sagittal_view = SliceView()
        self._coronal_view = SliceView()
        self._volume_view = VTKViewer()

        self._axial_view.set_orientation("axial")
        self._sagittal_view.set_orientation("sagittal")
        self._coronal_view.set_orientation("coronal")
        
        # 初始化光标同步器
        self._cursor_synchronizer = CursorSynchronizer()
        self._cursor_synchronizer.register_callback(self._on_cursor_sync_changed)
        
        # 为各视图设置光标同步器
        self._axial_view.set_cursor_synchronizer(self._cursor_synchronizer)
        self._sagittal_view.set_cursor_synchronizer(self._cursor_synchronizer)
        self._coronal_view.set_cursor_synchronizer(self._cursor_synchronizer)
        self._volume_view.set_cursor_synchronizer(self._cursor_synchronizer)

        # 连接缩放同步
        self._axial_view.zoom_changed.connect(self._sync_zoom)
        self._sagittal_view.zoom_changed.connect(self._sync_zoom)
        self._coronal_view.zoom_changed.connect(self._sync_zoom)

        # 四窗工具栏一致 + 同步：前三个 MPR 窗的 3D 按钮委托给三维窗口，
        # 勾选/可用状态与三维窗口实时同步；前三个工具栏默认折叠。
        for v in (self._axial_view, self._sagittal_view, self._coronal_view):
            v.set_volume_peer(self._volume_view)
        self._wire_toolbar_sync()

        layout.addWidget(self._axial_view, 0, 0)
        layout.addWidget(self._sagittal_view, 0, 1)
        layout.addWidget(self._coronal_view, 1, 0)
        layout.addWidget(self._volume_view, 1, 1)

        self._slice_ranges = None
        self._origin = None
        self._spacing = None
        self._maximized_view = None
        self._orientation_visible = True   # 三维模式下方位字可见；切到二维由 StudyViewPanel 置 False

    def set_iso_value(self, value: int):
        self._volume_view.set_iso_value(value)

    def set_keep_largest_region(self, enabled: bool):
        self._volume_view.set_keep_largest_region(enabled)

    def set_remove_bed(self, enabled: bool):
        self._volume_view.set_remove_bed(enabled)

    def set_is_mr(self, is_mr: bool):
        self._volume_view.set_is_mr(is_mr)

    def set_orientation_visible(self, visible: bool):
        """统一开关四个子视图（3×MPR + 体渲染）的「左/右」方位字。

        三维模式下置 True；切回二维阅读时由 StudyViewPanel 置 False。
        用持久开关而非仅 hide()，是为了挡住所有刷新路径
        （SliceView._update_dicom_overlays / VTKViewer._position_labels 在
        翻层、缩放、resize 时都会重新 show()），确保 2D 下绝不再漏出，
        避免与 Dicom2DReader 的「左/右」叠加成重复字符。
        """
        self._orientation_visible = bool(visible)
        for v in (self._axial_view, self._sagittal_view,
                  self._coronal_view, self._volume_view):
            try:
                v._orientation_visible = self._orientation_visible
                if self._orientation_visible and v.isVisible():
                    v._left_char_label.show()
                    v._right_char_label.show()
                else:
                    v._left_char_label.hide()
                    v._right_char_label.hide()
            except Exception:   # noqa: BLE001
                pass

    def hide_orientation_labels(self):
        """兼容旧调用：等价于 set_orientation_visible(False)。"""
        self.set_orientation_visible(False)

    def reset_view(self):
        self._volume_view.reset_view()
        for view in [self._axial_view, self._sagittal_view, self._coronal_view]:
            rng = view.slice_range()
            if rng and rng[0] < rng[1]:
                mid = (rng[0] + rng[1]) // 2
                view.set_slice(mid)

    def _wire_toolbar_sync(self):
        """把三维窗口工具栏的变化广播给三个 MPR 窗（四窗状态一致）。"""
        vol = self._volume_view
        for attr in ("_select_btn", "_invert_btn", "_cut_btn",
                     "_measure3d_btn"):
            btn = getattr(vol, attr, None)
            if btn is None:
                continue
            btn.clicked.connect(self._on_volume_toolbar_changed)
            btn.toggled.connect(self._on_volume_toolbar_changed)

    def _on_volume_toolbar_changed(self, *_):
        """三维窗口工具栏变化 → 三个 MPR 窗拉取最新勾选/可用状态。"""
        for v in (self._axial_view, self._sagittal_view, self._coronal_view):
            if hasattr(v, "_sync_toolbar_state"):
                v._sync_toolbar_state()

    def _sync_zoom(self, zoom_factor: float):
        """同步所有视图的缩放比例"""
        # 阻断信号防止循环触发
        self._axial_view.blockSignals(True)
        self._sagittal_view.blockSignals(True)
        self._coronal_view.blockSignals(True)
        
        try:
            self._axial_view.set_zoom(zoom_factor)
            self._sagittal_view.set_zoom(zoom_factor)
            self._coronal_view.set_zoom(zoom_factor)
        finally:
            self._axial_view.blockSignals(False)
            self._sagittal_view.blockSignals(False)
            self._coronal_view.blockSignals(False)
            
            # 刷新所有视图显示
            for view in [self._axial_view, self._sagittal_view, self._coronal_view]:
                if view._viewer.GetInput() is not None:
                    view._vtk_widget.GetRenderWindow().Render()

    def clear_all(self):
        if self._maximized_view is not None:
            self.restore_layout()
        self._volume_view.clear_detection_markers()
        self._volume_view.clear_segmentation_mask()
        self._volume_view.clear()
        self._axial_view.clear()
        self._sagittal_view.clear()
        self._coronal_view.clear()
        self._slice_ranges = None
        self._origin = None
        self._spacing = None
        self._cursor_synchronizer = CursorSynchronizer()
        self._cursor_synchronizer.register_callback(self._on_cursor_sync_changed)
        self._axial_view.set_cursor_synchronizer(self._cursor_synchronizer)
        self._sagittal_view.set_cursor_synchronizer(self._cursor_synchronizer)
        self._coronal_view.set_cursor_synchronizer(self._cursor_synchronizer)
        self._volume_view.set_cursor_synchronizer(self._cursor_synchronizer)

    def toggle_maximize(self, view):
        if self._maximized_view is view:
            self.restore_layout()
            return
        if self._maximized_view is not None:
            self.restore_layout()
        for w in [self._axial_view, self._sagittal_view, self._coronal_view, self._volume_view]:
            self._layout.removeWidget(w)
            if w is not view:
                w.hide()
            else:
                w.show()
        self._layout.addWidget(view, 0, 0, 2, 2)
        self._maximized_view = view
        for w in [self._axial_view, self._sagittal_view, self._coronal_view, self._volume_view]:
            if hasattr(w, "set_fullscreen_button"):
                w.set_fullscreen_button(w is view)

    def restore_layout(self):
        for w in [self._axial_view, self._sagittal_view, self._coronal_view, self._volume_view]:
            self._layout.removeWidget(w)
        self._layout.addWidget(self._axial_view, 0, 0)
        self._layout.addWidget(self._sagittal_view, 0, 1)
        self._layout.addWidget(self._coronal_view, 1, 0)
        self._layout.addWidget(self._volume_view, 1, 1)
        for w in [self._axial_view, self._sagittal_view, self._coronal_view, self._volume_view]:
            w.show()
            if hasattr(w, "set_fullscreen_button"):
                w.set_fullscreen_button(False)
        self._maximized_view = None

    def set_reader(self, reader, window_center=None, window_width=None, dicom_data: dict = None):
        self._volume_view.set_reader(reader)
        for view in [self._axial_view, self._sagittal_view, self._coronal_view]:
            view.set_input(reader)
            view.set_window_level(window_width, window_center)
            if dicom_data:
                view._dicom_data = dicom_data
                view._update_dicom_overlays()
        self._slice_ranges = [
            self._axial_view.slice_range(),
            self._sagittal_view.slice_range(),
            self._coronal_view.slice_range(),
        ]
        output = reader.GetOutput()
        self._origin = output.GetOrigin()
        self._spacing = output.GetSpacing()
        
        # 设置光标同步器的图像属性
        extent = output.GetExtent()
        self._cursor_synchronizer.set_image_properties(self._origin, self._spacing, extent)
        
        # 统一设置所有视图的初始光标到左上角
        left_top_i = extent[0]      # 最小X
        left_top_j = extent[3]      # 最大Y（左上角）
        left_top_k = (extent[5] - extent[4]) // 2  # 中心Z
        
        left_top_world = (
            self._origin[0] + left_top_i * self._spacing[0],
            self._origin[1] + left_top_j * self._spacing[1], 
            self._origin[2] + left_top_k * self._spacing[2]
        )
        
        if _DEBUG_LOG:
            print(f"UNIFIED_INIT: 统一初始光标世界坐标: {left_top_world}")
        
        # 为所有视图设置相同的初始世界坐标
        self._axial_view.set_cursor_world(left_top_world)
        self._sagittal_view.set_cursor_world(left_top_world)
        self._coronal_view.set_cursor_world(left_top_world)
        self._volume_view.set_cursor_position(left_top_world)
        
        # 初始化同步器
        self._cursor_synchronizer.set_world_position(left_top_world)
        
        self.set_slice_ratio(0.5)

    def set_slice_ratio(self, ratio: float):
        if not self._slice_ranges:
            return
        ratio = max(0.0, min(1.0, ratio))
        for view, (smin, smax) in zip(
            [self._axial_view, self._sagittal_view, self._coronal_view],
            self._slice_ranges,
        ):
            idx = int(round(smin + (smax - smin) * ratio))
            view.set_slice(idx)

    def set_window_level(self, window: float, level: float):
        """设置所有MPR视图的窗宽窗位"""
        self._axial_view.set_window_level(window, level)
        self._sagittal_view.set_window_level(window, level)
        self._coronal_view.set_window_level(window, level)
        # 确保标签位置正确且无重叠
        self._axial_view.update_info_label(window, level)
        self._sagittal_view.update_info_label(window, level)
        self._coronal_view.update_info_label(window, level)
    
    def set_preview_enabled(self, enabled: bool):
        self._volume_view.set_preview_enabled(enabled)
    
    def set_color_mode(self, mode: str):
        self._volume_view.set_color_mode(mode)

    def set_enhance_fractures(self, enabled: bool):
        self._volume_view.set_enhance_fractures(enabled)

    def set_detection_markers(self, markers):
        self._volume_view.set_detection_markers(markers)

    def set_segmentation_mask(self, mask, origin, spacing):
        self._volume_view.set_segmentation_mask(mask, origin, spacing)
        try:
            self._axial_view.set_segmentation_mask(mask)
            self._sagittal_view.set_segmentation_mask(mask)
            self._coronal_view.set_segmentation_mask(mask)
        except Exception:
            pass

    def _on_cursor_sync_changed(self, world_pos, source_view=None):
        """光标同步回调：跨视图更新切片与光标（鼠标移动热路径，勿加阻塞操作）"""
        if not self._origin or not self._spacing or world_pos is None:
            return

        # 获取各视图中的位置信息
        view_positions = self._cursor_synchronizer.get_view_positions(world_pos)

        # 更新各视图的切片和光标
        for view_name, view_obj in [("axial", self._axial_view),
                                   ("sagittal", self._sagittal_view),
                                   ("coronal", self._coronal_view)]:
            if view_name in view_positions:
                pos_info = view_positions[view_name]
                slice_idx = pos_info["slice_index"]
                cursor_pos = pos_info["cursor_pos"]

                # 确保切片索引在有效范围内
                slice_range = view_obj.slice_range()
                if slice_range[0] <= slice_idx <= slice_range[1]:
                    view_obj.set_slice(slice_idx)
                    if _DEBUG_LOG:
                        print(f"DEBUG: {view_name}视图切片更新成功")

                # 更新光标位置
                if source_view != view_name:  # 避免循环更新
                    view_obj.set_cursor_world(world_pos)

        # 更新3D视图中的光标
        if source_view != "3d":
            self._volume_view.set_cursor_position(world_pos)
            
    def _on_cursor_changed(self, ijk):
        """保持向后兼容性的旧回调方法"""
        if not self._origin or not self._spacing:
            return
        i, j, k = ijk
        ox, oy, oz = self._origin
        sx, sy, sz = self._spacing
        world = (ox + i * sx, oy + j * sy, oz + k * sz)
        
        # 使用新的同步机制
        self._cursor_synchronizer.set_world_position(world)


class StudyViewPanel(QtWidgets.QFrame):
    """影像浏览容器：在「二维阅读模式」和「三维重建模式」之间切换。

    - 默认进入二维阅读模式（Dicom2DReader 单视图 + 滚轮翻页）
    - 可随时手动切换到三维重建模式（QuadViewer 四视图）
    - 切换到三维时才构建三维渲染管线，避免不必要的性能开销
    """

    mode_changed = QtCore.pyqtSignal(str)   # "2d" / "3d"
    series_dropped = QtCore.pyqtSignal(object, object)   # 拖进来的序列token, 目标窗索引
    # 活动窗口里的足弓关键点被拖动（转发自 Dicom2DReader）
    keypoints_changed = QtCore.pyqtSignal(dict)

    MODE_2D = "2d"
    MODE_3D = "3d"

    # 顶部模式栏的样式不再写死在这里：交给我 theme.qss_mode_bar()，
    # 由 theme.bind 注册，切皮肤自动重建（拖放悬停时临时改成强调色边框）。

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = self.MODE_2D
        self._reader = None
        self._dicom_data = {}
        self._window_width = 400.0
        self._window_center = 40.0
        self._volume_built = False
        self._build_pending = False
        self._3d_available = True
        self._is_mr = False         # MR 语义（信号强度），影响三维阈值算法
        self._drop_active = False

        # 整个阅读区都是放置目标
        self.setAcceptDrops(True)
        self._drop_hint = QtWidgets.QLabel("松开即可打开该序列")
        self._drop_hint.setAlignment(QtCore.Qt.AlignCenter)
        theme.bind(self._drop_hint, theme.qss_drop_hint)
        self._drop_hint.setVisible(False)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._layout = layout

        # 顶部模式切换栏
        self._mode_bar = QtWidgets.QFrame()
        self._mode_bar.setObjectName("modeBar")
        theme.bind(self._mode_bar, lambda: theme.qss_mode_bar(self._drop_active))
        bar_layout = QtWidgets.QHBoxLayout(self._mode_bar)
        bar_layout.setContentsMargins(6, 3, 6, 3)
        bar_layout.setSpacing(6)

        bar_layout.addWidget(QtWidgets.QLabel("显示模式："))

        self._btn_2d = QtWidgets.QPushButton("二维阅读")
        self._btn_2d.setCheckable(True)
        self._btn_2d.setChecked(True)
        self._btn_2d.setToolTip("二维 DICOM 读片模式（滚轮翻层、右键调窗、中键平移）")
        self._btn_2d.clicked.connect(lambda: self.set_mode(self.MODE_2D))

        self._btn_3d = QtWidgets.QPushButton("三维重建")
        self._btn_3d.setCheckable(True)
        self._btn_3d.setToolTip("三维体渲染 + MPR 四视图模式")
        self._btn_3d.clicked.connect(lambda: self.set_mode(self.MODE_3D))

        bar_layout.addWidget(self._btn_2d)
        bar_layout.addWidget(self._btn_3d)

        # 窗口数切换（仅二维模式可用）：临床常要对比同一检查的多个序列，
        # 例如 T1/T2/FLAIR 并排看。1 窗时按钮组整体隐藏，不占空间。
        self._layout_group_box = QtWidgets.QFrame()
        _lg = QtWidgets.QHBoxLayout(self._layout_group_box)
        _lg.setContentsMargins(10, 0, 0, 0)
        _lg.setSpacing(0)
        _lg.addWidget(QtWidgets.QLabel("窗口："))
        self._layout_buttons = []
        for _n in (1, 2, 4):
            _b = QtWidgets.QPushButton(str(_n))
            _b.setCheckable(True)
            _b.setFixedWidth(30)
            _b.setToolTip("%d 窗显示：每个窗口可独立载入不同序列" % _n)
            _b.clicked.connect(lambda _checked, n=_n: self.set_view_count(n))
            _lg.addWidget(_b)
            self._layout_buttons.append(_b)
        self._layout_buttons[0].setChecked(True)

        # 窗口同步开关：开启后任一窗翻层/调窗值，其余窗跟随。
        # CT 典型用法是 a 窗肺窗、b 窗纵隔窗做同层对比；
        # MRI 则用于 T1 与 FLAIR 逐层对读。
        self._sync_checkbox = QtWidgets.QCheckBox("同步")
        self._sync_checkbox.setToolTip("关闭：各窗独立")
        self._sync_checkbox.toggled.connect(self._on_sync_toggled)
        _lg.addWidget(self._sync_checkbox)
        self._sync_buttons = [self._sync_checkbox]

        bar_layout.addWidget(self._layout_group_box)

        bar_layout.addStretch(1)

        self._hint_label = QtWidgets.QLabel(
            "滚轮/↑↓ 翻层 · 右侧滑条浏览 · 右键拖动调窗 · 中键拖动平移 · "
            "Ctrl+滚轮缩放 · W 切换模式 · R 复位默认窗值")
        theme.role(self._hint_label, "caption")
        bar_layout.addWidget(self._hint_label)
        layout.addWidget(self._mode_bar)

        # 二维阅读区：网格容器（1 窗时就是单格，视觉与原来完全一致）
        self._view_grid_host = QtWidgets.QWidget()
        self._view_grid = QtWidgets.QGridLayout(self._view_grid_host)
        self._view_grid.setContentsMargins(0, 0, 0, 0)
        self._view_grid.setSpacing(3)
        layout.addWidget(self._view_grid_host, 1)

        # 二维阅读器列表：索引 0 是主窗口（沿用既有全部行为，保证零回归），
        # 索引 1..N-1 是多窗模式下的副窗口，各自有独立的 reader/测量/窗值。
        self._readers_2d = []
        self._active_view = 0        # 活动窗口索引
        self._view_count = 1         # 当前窗口数（1 / 2 / 4）
        # 多窗同步：开启后任一窗翻层/调窗值，其余窗跟随
        self._sync_enabled = False
        self._sync_guard = False     # 广播期间的回环防护
        self._sync_buttons = []
        # 最近一次载入的序列参数，切到多窗时用它给新窗口复制一份
        self._last_loaded_payload = None
        self._reader_2d = self._make_reader_2d()   # 主窗口（既有引用名保持可用）
        self._readers_2d.append(self._reader_2d)
        self._view_grid.addWidget(self._reader_2d, 0, 0)

        # 三维四视图（按需创建）
        self._quad_viewer = None
        # 三维视图的独立宿主容器（懒创建于 quad_viewer()）——
        # 独立出来才能与二维网格严格互斥，避免 SliceView 的方位字
        # 与 Dicom2DReader 的方位字叠加显示
        self._quad_host = None

        # VTK 的渲染控件是**原生 OpenGL 子窗口**，拖拽事件不会被 Qt 自动
        # 冒泡到父控件，必须显式装事件过滤器转发，否则鼠标停在画面上时
        # 阅读区收不到 DragEnter/Drop（表现为「只有拖到边框外才有反应」）
        self._drop_filters_installed = False
        targets = [self._reader_2d]
        try:
            targets.append(self._reader_2d._vtk_widget)
        except Exception:       # noqa: BLE001
            pass
        for w in targets:
            try:
                w.installEventFilter(self)
                self._drop_filters_installed = True
            except Exception:       # noqa: BLE001
                pass
        self._drop_hint.setParent(self)
        self._drop_hint.resize(240, 56)
        self._raise_drop_hint()

        self._set_buttons_enabled(False)

    def _raise_drop_hint(self):
        """把拖放提示压在阅读区正中（要在 resize 后重跑）"""
        if not hasattr(self, "_drop_hint"):
            return
        r = self.rect()
        self._drop_hint.move(max(0, (r.width() - self._drop_hint.width()) // 2),
                             max(0, (r.height() - self._drop_hint.height()) // 2))
        self._drop_hint.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._raise_drop_hint()

    # ---------- 拖放 ----------
    @staticmethod
    def _drop_token(mime):
        """从 mimeData 里取序列载荷 token；不是本程序的拖放返回 None。

        判定与取载荷都收在这里，`_accept_drop()` 供事件处理与测试共用：
        伪造 QDragEnterEvent/QDropEvent 做单测在 PyQt5 下会段错误
        （QObject.eventFilter 无参调用、QDragEnterEvent 手工构造都是），
        所以测试走这条纯函数路径，不去碰 Qt 的事件对象。
        """
        if mime is None or not mime.hasFormat(SERIES_MIME):
            return None
        raw = bytes(mime.data(SERIES_MIME)).decode("utf-8", "ignore")
        return raw or None

    def _accept_drop(self, mime):
        return self._drop_token(mime) is not None

    def _set_drop_active(self, active):
        active = bool(active)
        if active == self._drop_active:
            return
        self._drop_active = active
        self._drop_hint.setVisible(active)
        self._raise_drop_hint()
        self._mode_bar.setStyleSheet(theme.qss_mode_bar(active))

    def dragEnterEvent(self, event):
        if self._accept_drop(event.mimeData()):
            event.acceptProposedAction()
            self._set_drop_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._accept_drop(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drop_active(False)
        event.accept()

    def _reader_index_of(self, w) -> int:
        """判断某个子控件属于哪个二维窗口（找不到返回 -1）

        落点拿到的多半是窗口**内部**的子控件（VTK 原生渲染控件、翻层滑条、
        覆盖层标签…），不是窗口本身，所以要沿 parent 链往上找。
        """
        cur = w
        for _ in range(12):          # 防 parent 链异常
            if cur is None:
                return -1
            for j, r in enumerate(self._readers_2d):
                if cur is r:
                    return j
            cur = cur.parentWidget()
        return -1

    def view_index_at(self, pos=None) -> int:
        """按鼠标落点反查落在哪个二维窗口（返回 _readers_2d 的索引）

        多窗下拖放必须用**落点**判定目标窗，而不是活动窗口 ——
        用户把序列拖到右窗，就是想看右窗。之前只发序列载荷、丢了落点信息，
        于是无论拖到哪个窗都载入活动窗口，表现为「后面的拖放把前面的覆盖了」。

        两个坑（都实测踩过）：
        1. 宿主是**普通 QWidget**，`indexAt()` 只有 QStackedWidget 才有；
        2. `childAt()` 拿到的多半是窗口**内部**的子控件（VTK 渲染控件等），
           不是窗口本身，必须沿 parent 链上溯 —— 否则永远匹配不到，
           `view_index_at` 恒返回 0，看起来「拖哪儿都一样」。

        ``pos`` 为 None 或落点不在任何窗口上时返回活动窗口。
        """
        if pos is None:
            return self._active_view
        try:
            w = self._view_grid_host.childAt(pos)
        except Exception:       # noqa: BLE001
            return self._active_view
        if w is None:
            return self._active_view
        idx = self._reader_index_of(w)
        if 0 <= idx < self.view_count():
            return idx
        return self._active_view

    def dropEvent(self, event):
        self._set_drop_active(False)
        token = self._drop_token(event.mimeData())
        if token is None:
            event.ignore()
            return
        event.acceptProposedAction()
        # 用鼠标落点定位目标窗（多窗下拖到哪窗就载入哪窗）
        try:
            pos = event.position().toPoint() if hasattr(event, "position") \
                else event.pos()
        except Exception:       # noqa: BLE001
            pos = None
        target = self.view_index_at(pos)
        if target != self._active_view:
            self.set_active_view(target)
        self.series_dropped.emit(token, target)

    def eventFilter(self, obj, event):
        """子控件事件转发：
        1) 拖放事件转给本面板处理（VTK 原生窗口不参与 Qt 冒泡，必须显式转发）
        2) 多窗模式下把发生交互的窗口记为活动窗口（决定双击序列卡载入到哪、
           测量作用于哪）
        """
        etype = event.type()
        if etype in (QtCore.QEvent.MouseButtonPress,
                     QtCore.QEvent.FocusIn,
                     QtCore.QEvent.Wheel):
            try:
                for i, r in enumerate(self._readers_2d):
                    if obj is r or obj is getattr(r, "_vtk_widget", None):
                        if i != self._active_view:
                            self.set_active_view(i)
                        break
            except Exception:   # noqa: BLE001
                pass
        if etype in (QtCore.QEvent.DragEnter, QtCore.QEvent.DragMove,
                     QtCore.QEvent.DragLeave, QtCore.QEvent.Drop):
            if etype == QtCore.QEvent.DragLeave or self._accept_drop(
                    event.mimeData()):
                if etype == QtCore.QEvent.DragEnter:
                    self.dragEnterEvent(event)
                elif etype == QtCore.QEvent.DragMove:
                    self.dragMoveEvent(event)
                elif etype == QtCore.QEvent.DragLeave:
                    self.dragLeaveEvent(event)
                else:
                    self.dropEvent(event)
                return True
        return super().eventFilter(obj, event)

    # ---------- 对外接口 ----------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def reader_2d(self) -> Dicom2DReader:
        return self._reader_2d

    def current_volume_view(self):
        """取当前三维视图（VTKViewer）；尚未建立时返回 None。

        对外只暴露这一个入口：`_quad_viewer` 是惰性创建的，且三维视图
        藏在 QuadViewer 内部，直接让外部摸 `_quad_viewer._volume_view`
        会绕过创建时机，产生「拿到 None 但不报错」的静默失败。
        """
        quad = self._quad_viewer
        if quad is None:
            return None
        return getattr(quad, "_volume_view", None)

    # ------------------------------------------------------------------
    # 多窗显示（1 / 2 / 4 窗）
    # ------------------------------------------------------------------

    def _make_reader_2d(self) -> Dicom2DReader:
        """新建一个二维阅读器并接入信号

        每个窗口是独立的 `Dicom2DReader` 实例（自带 VTK 控件、测量层、
        窗值），互不干扰。所有窗的翻层都汇到 ``on_view_slice_changed``，
        由它决定要不要广播给其他窗（同步开关）—— 主窗与副窗走同一条路径，
        行为一致。
        """
        r = Dicom2DReader()
        idx = len(self._readers_2d)
        r.slice_changed.connect(
            lambda cur, tot, i=idx: self.on_view_slice_changed(i, cur, tot))
        r.keypoints_changed.connect(self.keypoints_changed)
        # 点击窗口即把它设为活动窗口（决定双击序列卡载入到哪、测量作用于哪）
        try:
            r.installEventFilter(self)
        except Exception:       # noqa: BLE001
            pass
        try:
            r._vtk_widget.installEventFilter(self)
        except Exception:       # noqa: BLE001
            pass
        return r

    def set_active_view(self, index: int):
        """设定活动窗口（多窗模式下决定后续操作作用于哪个窗口）

        只允许激活到**当前显示中**的窗口：切回 1 窗后实例仍在（数据保留），
        但隐藏的窗口不能成为活动窗口，否则载入/测量会作用到看不见的地方。
        """
        if not (0 <= index < min(len(self._readers_2d), self.view_count())):
            return
        if index == self._active_view:
            return
        self._active_view = index
        self._update_view_border()

    @property
    def active_index(self) -> int:
        """当前活动窗口索引（0 = 主窗口）"""
        return self._active_view

    @property
    def active_reader(self) -> Dicom2DReader:
        """当前活动窗口的阅读器（单窗时就是主窗口）"""
        if 0 <= self._active_view < len(self._readers_2d):
            return self._readers_2d[self._active_view]
        return self._reader_2d

    # ------------------------------------------------------------------
    # 足弓关键点手动微调：一律作用于活动窗口
    # ------------------------------------------------------------------

    def show_keypoints(self, shown):
        """显示 / 隐藏足弓可拖动手柄（活动窗口）"""
        try:
            self.active_reader.show_keypoints(shown)
        except Exception:       # noqa: BLE001
            pass

    def move_keypoint(self, idx, x, y):
        """程序化挪动一个关键点（活动窗口），不触发信号"""
        try:
            self.active_reader.move_keypoint(idx, x, y, emit=False)
        except Exception:       # noqa: BLE001
            pass

    def reset_keypoints(self):
        try:
            self.active_reader.reset_keypoints(emit=False)
        except Exception:       # noqa: BLE001
            pass

    def keypoints_visible(self):
        try:
            return bool(self.active_reader._kp_shown)
        except Exception:       # noqa: BLE001
            return False

    def clear_keypoints(self):
        """清掉**所有**窗口的关键点（换序列/清屏时用，避免残留）"""
        for r in self._readers_2d:
            try:
                r.clear_keypoints()
            except Exception:    # noqa: BLE001
                pass

    def _update_view_border(self):
        """给活动窗口加高亮边框，标明测量/载入会作用于此"""
        for i, r in enumerate(self._readers_2d):
            try:
                r.setFrameStyle(
                    QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Box
                    if i == self._active_view
                    else QtWidgets.QFrame.NoFrame)
                if i == self._active_view and len(self._readers_2d) > 1:
                    r.setStyleSheet(theme.qss_active_border())
                else:
                    r.setStyleSheet("")
            except Exception:   # noqa: BLE001
                pass

    def view_count(self) -> int:
        """当前**显示中**的窗口数（1 / 2 / 4）

        注意不能用 ``len(self._readers_2d)``：那是已创建的实例数，
        切回 1 窗后实例仍在（数据要保留），但用户只看到 1 个窗。
        """
        return int(getattr(self, "_view_count", 1))

    # ------------------------------------------------------------------
    # 窗口同步（多窗对比的核心便利功能）
    # ------------------------------------------------------------------

    def set_sync_enabled(self, enabled: bool):
        """开启/关闭多窗同步

        开启后：任一窗翻层或调窗值，其余**已载入图像**的窗跟随。
        典型用法：a 窗肺窗、b 窗纵隔窗，同步翻层便于逐层对比。
        MRI 同理（T1 与 FLAIR 逐层对读）。
        """
        self._sync_enabled = bool(enabled)
        if self._sync_enabled:
            self._update_sync_tooltip()
        return self._sync_enabled

    def sync_enabled(self) -> bool:
        return bool(getattr(self, "_sync_enabled", False))

    def _update_sync_tooltip(self):
        tip = "关闭：各窗独立"
        if self._sync_enabled:
            n = sum(1 for r in self._readers_2d if r.has_image())
            tip = ("已开启：任一窗翻层/调窗值，其余 %d 个窗跟随\n"
                   "层数不同时按**比例**对齐" % n) if n > 1 else \
                  "已开启：但当前只有 1 个窗载了图像"
        for b in getattr(self, "_sync_buttons", []):
            b.setToolTip(tip)

    def _broadcast_slice(self, source_index: int, current: int, total: int):
        """把源窗的层号按比例广播到其余窗（带回环防护）

        ``current``/``total`` 都是**1 基**的层号（来自 slice_changed 信号）。
        比例换算必须首尾都严格对齐：第1 层→目标第 1 层、
        末层 → 目标末层，故用 ``(current-1)/(total-1)`` 映射到
        ``(rmin..rmax)`` 的同构区间，两窗层数不同时也能对上解剖位置。
        """
        if not self.sync_enabled() or total <= 0:
            return
        src = self._readers_2d[source_index]
        _smin, smax = src.slice_range()
        # 源窗 1 基层号 → 0 基比例
        if total > 1:
            src_ratio = (current - 1) / float(total - 1)
        else:
            src_ratio = 0.0
        src_ratio = max(0.0, min(1.0, src_ratio))
        # 防回环：置位期间不响应其它窗的广播
        self._sync_guard = True
        try:
            for i, r in enumerate(self._readers_2d):
                if i == source_index or r.isHidden() or not r.has_image():
                    continue
                rmin, rmax = r.slice_range()
                if rmax <= rmin:
                    continue
                idx = int(round(rmin + (rmax - rmin) * src_ratio))
                r.set_slice(max(rmin, min(rmax, idx)))
        finally:
            self._sync_guard = False
        self._update_sync_tooltip()

    def _broadcast_window_level(self, window: float, level: float):
        """把窗宽窗位广播到其余窗（带回环防护）"""
        if not self.sync_enabled():
            return
        self._sync_guard = True
        try:
            for i, r in enumerate(self._readers_2d):
                if i == self._active_view or r.isHidden() or not r.has_image():
                    continue
                r.set_window_level(window, level)
        finally:
            self._sync_guard = False

    def on_view_slice_changed(self, index: int, current: int, total: int):
        """任一窗翻层后的统一入口：更新标签 + 广播同步

        副窗口的 ``slice_changed`` 也走这里（原先只就地更新本窗标签，
        不参与同步）；主窗口的信号已连到 ``_on_2d_slice_changed``，
        由它转发到本方法。
        """
        if 0 <= index < len(self._readers_2d):
            self._readers_2d[index].update_layer_text(current, total)
        if self.sync_enabled() and not getattr(self, "_sync_guard", False):
            self._broadcast_slice(index, current, total)

    def apply_window_level_to_views(self, window: float, level: float):
        """工具栏改窗值后的统一入口：作用于活动窗+ 广播其余窗"""
        if self.sync_enabled() and not getattr(self, "_sync_guard", False):
            self._broadcast_window_level(window, level)
        self._update_sync_tooltip()

    def _on_sync_toggled(self, checked: bool):
        """「同步」勾选框：多窗翻层与窗值联动"""
        self.set_sync_enabled(checked)

    def set_view_count(self, n: int):
        """切换二维阅读区的窗口数为 1 / 2 / 4

        - 从少到多：新建窗口，**第一个窗口沿用当前已加载的序列**（不用重双击），
          其余窗口留空；
        - 从多到少：只隐藏多余窗口，**不丢弃它们的数据** —— 切回时序列还在，
          便于来回对比；
        - 三维模式下调用会先切回二维。
        """
        n = int(n)
        if n not in (1, 2, 4):
            return
        if self._mode == self.MODE_3D:
            self.set_mode(self.MODE_2D)

        cur = len(self._readers_2d)
        # 实例数已经够用（从 4 切回 1 再切 4）时也要走重排，
        # 否则只勾按钮、布局不动 —— 这里不提前return。
        need_more = n > cur

        if need_more:
            # 保留「当前显示的序列」参数，供新窗口复用
            seed = self._last_loaded_payload or {}
            for _ in range(n - cur):
                r = self._make_reader_2d()
                self._readers_2d.append(r)
                if seed.get("reader") is not None:
                    try:
                        r.set_reader(seed["reader"],
                                     seed.get("window_width"),
                                     seed.get("window_center"),
                                     dicom_data=seed.get("dicom_data") or {},
                                     slice_positions=seed.get("slice_positions"))
                    except Exception as e:       # noqa: BLE001
                        print("新窗口复制当前序列失败: %s" % e)

        # 先记目标窗口数，_layout_readers() 依赖它决定显隐
        self._view_count = n
        self._layout_readers()
        for i, b in enumerate(self._layout_buttons):
            b.setChecked((i + 1) == n)
        if self._active_view >= n:
            self._active_view = 0
        self._update_view_border()

    def _layout_readers(self):
        """按窗口数重新排布网格（1=单格，2=左右，4=四宫格）

        注意：这里**不能**用 ``isHidden()`` 反推窗口数 —— ``set_view_count``
        在建新窗时已经 ``hide()`` 了它们，据此算会永远得到 1。
        窗口数由调用方通过 ``_view_count`` 给出。
        """
        n = int(getattr(self, "_view_count", 1))
        # 先全部移出网格，再按目标布局重新挂回去
        for r in self._readers_2d:
            self._view_grid.removeWidget(r)
        if n >= 4:
            for i, (rr, cc) in enumerate([(0, 0), (0, 1), (1, 0), (1, 1)]):
                self._view_grid.addWidget(self._readers_2d[i], rr, cc)
        elif n == 2:
            self._view_grid.addWidget(self._readers_2d[0], 0, 0)
            self._view_grid.addWidget(self._readers_2d[1], 0, 1)
        else:
            self._view_grid.addWidget(self._readers_2d[0], 0, 0)

        # 显隐：只有网格内的窗显示，多余的窗保留数据但隐藏
        for i, r in enumerate(self._readers_2d):
            if i < n:
                r.show()
            else:
                r.hide()

        for i in range(self._view_grid.count()):
            # 注意：itemAt() 返回的是 QWidgetItem，**没有** setSizePolicy；
            # 尺寸策略要设在 widget 上（否则均分空间失效，窗口会挤在一起）
            w = self._view_grid.itemAt(i).widget()
            if w is not None:
                w.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                QtWidgets.QSizePolicy.Expanding)

    def quad_viewer(self):
        """获取 QuadViewer；若尚未创建则创建（会构建三维管线前的空视图）

        **是方法不是属性**：MainWindow 侧有 4 处按 ``quad_viewer()`` 调用
        （连MPR 信号、渲染刷新、_apply_visibility 等）。
        曾误加 ``@property`` 导致 `TypeError: 'QuadViewer' object is not callable`。
        """
        if self._quad_viewer is None:
            self._quad_viewer = QuadViewer()
            self._quad_viewer.hide()
            # **必须装进独立的 host 容器**：直接 `self._layout.addWidget(quad, 1)`
            # 会与二维网格 `_view_grid_host`（也在 index=1）挤进同一格 ——
            # 两者都可见时，QuadViewer 里 3 个SliceView 的「右/左」方位字
            # 会与 Dicom2DReader 的「右/左」**同时显示**，看起来就是
            # 「左右字符重复叠加」。各自装进独立 host 后由
            # _apply_visibility 严格互斥，杜绝叠加。
            self._quad_host = QtWidgets.QWidget()
            _ql = QtWidgets.QVBoxLayout(self._quad_host)
            _ql.setContentsMargins(0, 0, 0, 0)
            _ql.setSpacing(0)
            _ql.addWidget(self._quad_viewer)
            self._quad_host.hide()
            self._layout.addWidget(self._quad_host, 1)
            # 三维视图是 4 个 VTK 原生 OpenGL 窗口，拖放同样收不到，
            # 逐个装上过滤器才能在整个阅读区范围内拖放
            try:
                for w in self._quad_viewer.findChildren(QtWidgets.QWidget):
                    w.installEventFilter(self)
            except Exception:       # noqa: BLE001
                pass
            # viewer 建好后补上此前记录的 MR 语义标记
            if getattr(self, "_is_mr", False):
                try:
                    self._quad_viewer.set_is_mr(True)
                except Exception:   # noqa: BLE001
                    pass
        return self._quad_viewer

    def has_volume(self) -> bool:
        return self._volume_built

    def set_data(self, reader, window_width=None, window_center=None,
                 dicom_data=None, slice_positions=None, view_index=None,
                 series_data=None):
        """加载新序列后调用：更新数据并按当前模式显示

        ``view_index`` 指定载入到哪个窗口（None = 活动窗口）。
        多窗模式下双击序列卡会传入用户当前点击/聚焦的窗口。

        ``series_data`` 是序列字典，只为携带 ``mr_stackable`` /
        ``mr_unstackable_reason`` / ``series_description`` 三个字段——
        血管重建系列不能堆体数据，三维入口靠它拦截。
        不传等于不拦截，保持旧行为。
        """
        if view_index is None:
            view_index = self._active_view
        if not (0 <= view_index < len(self._readers_2d)):
            view_index = 0
        target = self._readers_2d[view_index]

        if view_index == 0:
            # 主窗口沿用既有路径（保持全部原有行为）
            self._reader = reader
            self._dicom_data = dicom_data or {}
            if window_width and window_center is not None:
                self._window_width = float(window_width)
                self._window_center = float(window_center)
            target.set_reader(
                reader, self._window_width, self._window_center,
                dicom_data=self._dicom_data, slice_positions=slice_positions)
        else:
            # 副窗口：独立窗值，不影响主窗口
            ww = float(window_width) if window_width else 400.0
            wl = float(window_center) if window_center else 40.0
            target.set_reader(reader, ww, wl,
                              dicom_data=dicom_data or {},
                              slice_positions=slice_positions)

        # 记下最近一次载入参数，切到多窗时据此复制
        # mr_stackable / mr_unstackable_reason 必须带上：血管重建系列
        # 不能堆体数据，三维入口靠它拦截（否则给出一壁墙）。
        self._last_loaded_payload = {
            "reader": reader,
            "window_width": float(window_width) if window_width else None,
            "window_center": float(window_center) if window_center else None,
            "dicom_data": dicom_data or {},
            "slice_positions": slice_positions,
            "mr_stackable": (series_data or {}).get("mr_stackable"),
            "mr_unstackable_reason": (series_data or {}).get(
                "mr_unstackable_reason", ""),
            "series_description": (series_data or {}).get(
                "series_description", ""),
        }

        if view_index == 0:
            # 数据换了，旧的三维管线作废（只由主窗口的数据决定三维）
            self._volume_built = False
            if self._quad_viewer is not None and reader is not None:
                self._quad_viewer.clear_all()

        self._set_buttons_enabled(True)
        self.set_active_view(view_index)
        # 保持当前模式：重建三维管线。
        # 注意这里不能直接调 _ensure_volume：血管重建系列
        # 堆不成体数据，必须走 set_mode 的拦截（否则一壁墙）。
        if self._mode == self.MODE_3D:
            if self._volume_unstackable_reason():
                self._mode = self.MODE_2D
                self._btn_3d.setChecked(False)
                self._apply_visibility()
            else:
                self._ensure_volume()
        else:
            self._apply_visibility()

    def clear_all(self, view_index=None):
        """清空窗口

        ``view_index=None`` 时清空**所有**窗口（关闭整个检查时用）；
        给定时只清那一个窗口 —— 多窗下往B 窗载序列时**不能**把A 窗一起清掉，
        否则表现为「载到B 窗后A 窗的序列消失了」。
        """
        if view_index is None:
            for r in self._readers_2d:
                r.clear()
            if self._quad_viewer is not None:
                self._quad_viewer.clear_all()
            self._reader = None
            self._dicom_data = {}
            self._last_loaded_payload = None
            self._volume_built = False
            self._set_buttons_enabled(False)
        else:
            if 0 <= view_index < len(self._readers_2d):
                self._readers_2d[view_index].clear()
            if view_index == 0:
                # 主窗口的数据决定三维与工具栏状态
                self._reader = None
                self._dicom_data = {}
                self._last_loaded_payload = None
                self._volume_built = False
                if self._quad_viewer is not None:
                    self._quad_viewer.clear_all()
                # 还有其它窗载着数据时，按钮要保持可用
                if not any(r.has_image() for r in self._readers_2d[1:]):
                    self._set_buttons_enabled(False)
    def set_3d_available(self, available: bool):
        """X 线是单帧投影影像，不支持三维重建：禁用三维按钮并强制回到二维。"""
        available = bool(available)
        self._3d_available = available
        self._btn_3d.setEnabled(available)
        if available:
            self._btn_3d.setText("三维重建")
            self._btn_3d.setToolTip("三维体渲染 + MPR 四视图模式")
        else:
            self._btn_3d.setText("三维重建（X线不可用）")
            self._btn_3d.setToolTip("X 线为单帧投影影像，无法进行三维重建")
            if self._mode == self.MODE_3D:
                self.set_mode(self.MODE_2D)
        # 三维参数控件在 X 线下无意义，交由 MainWindow 显隐

    def set_ai_overlay_image(self, image_rgb):
        """把 X 线 AI 标注结果图显示到二维视图；传 None 恢复原图

        多窗下作用于**活动窗口** —— AI 只针对刚检测的那张片子。
        """
        self.active_reader.set_ai_overlay_image(image_rgb)

    def set_ct_measure_enabled(self, enabled: bool):
        """X 线没有 HU 物理意义，禁用 CT 值工具

        多窗下对所有已创建的窗口统一设置（工具可用性是全局的）。
        """
        for r in self._readers_2d:
            r.set_ct_measure_enabled(enabled)

    def set_mode(self, mode: str):
        if mode not in (self.MODE_2D, self.MODE_3D):
            return
        if mode == self.MODE_3D and not getattr(self, "_3d_available", True):
            return
        # 血管重建系列（MRA/TOF 批次）没有 ImagePositionPatient，
        # 堆不成体数据——三维只会给出一壁墙。
        # 在这里拦下来并告知用户，不让他看到一壁墙
        # 才发现不可用。注意：该信息由序列字段
        # mr_stackable 带传，因此必须从 payload 里取。
        if mode == self.MODE_3D and self._volume_unstackable_reason():
            QtWidgets.QMessageBox.information(
                self, "该序列不支持三维",
                "%s\n\n原因：%s\n\n"
                "该序列可以正常翻看二维图像，"
                "但无法拼叠成体数据。"
                % (self._current_series_title(),
                   self._volume_unstackable_reason()))
            return
        if mode == self._mode:
            return
        self._mode = mode
        self._btn_2d.setChecked(mode == self.MODE_2D)
        self._btn_3d.setChecked(mode == self.MODE_3D)
        # 窗口数切换只在二维模式有意义
        self._layout_group_box.setVisible(mode == self.MODE_2D)

        if mode == self.MODE_3D and self._reader is not None:
            self._ensure_volume()

        self._apply_visibility()
        self.mode_changed.emit(mode)

    def _volume_unstackable_reason(self) -> str:
        """当前序列是否不能堆叠成体数据；是则返回原因，否则返回空串。

        数据来源是序列字典里的 ``mr_stackable`` / ``mr_unstackable_reason``
        （由 ct3d_utils.mr_series_readable 在收集阶段写入）。
        没有这两个字段的历史 payload 一律当作可堆叠，保持旧行为。
        """
        payload = getattr(self, "_last_loaded_payload", None) or {}
        if payload.get("mr_stackable") is False:
            return str(payload.get("mr_unstackable_reason")
                       or "该序列无法按层堆叠为体数据")
        return ""

    def _current_series_title(self) -> str:
        payload = getattr(self, "_last_loaded_payload", None) or {}
        for key in ("series_description", "series_title", "title"):
            val = str(payload.get(key) or "").strip()
            if val:
                return val
        return "当前序列"

    def toggle_mode(self):
        if (self._mode == self.MODE_2D
                and not getattr(self, "_3d_available", True)):
            return
        self.set_mode(self.MODE_3D if self._mode == self.MODE_2D else self.MODE_2D)

    def set_window_level(self, window: float, level: float):
        """外部（工具栏）调整窗宽窗位

        默认**只作用于活动窗口** —— 各窗可能显示不同序列，窗值本就不同，
        强行同步会把用户精心调好的对比度抹掉。
        勾选「同步」后（本属性内部广播）其余已载入图像的窗一并跟随，
        用于「a 窗肺窗、b 窗纵隔窗同层对比」这种场景。
        """
        self._window_width = float(window)
        self._window_center = float(level)
        self.active_reader.set_window_level(window, level)
        # 同步开启时广播到其余窗（带回环防护）
        self.apply_window_level_to_views(window, level)
        # 主窗口则要同步给三维
        if self._active_view == 0 and self._quad_viewer is not None \
                and self._volume_built:
            self._quad_viewer.set_window_level(window, level)

    def get_window_level(self):
        """返回活动窗口的窗值（工具栏显示应与用户正在看的窗口一致）"""
        if self._active_view == 0:
            return self._window_width, self._window_center
        try:
            return self.active_reader.get_window_level()
        except Exception:       # noqa: BLE001
            return self._window_width, self._window_center

    def set_iso_value(self, value: int):
        if self._quad_viewer is not None:
            self._quad_viewer.set_iso_value(value)

    def set_keep_largest_region(self, enabled: bool):
        if self._quad_viewer is not None:
            self._quad_viewer.set_keep_largest_region(enabled)

    def set_remove_bed(self, enabled: bool):
        if self._quad_viewer is not None:
            self._quad_viewer.set_remove_bed(enabled)

    def set_is_mr(self, is_mr: bool):
        """记录 MR 语义并透传给三维视图

        三维管线是懒加载的（``quad_viewer`` 首次切到三维才创建），
        所以这里必须先把状态存下来，等viewer 建好时补应用，
        否则先设 MR 后切三维会丢失标记、错用CT 的 HU 算法。
        """
        self._is_mr = bool(is_mr)
        if self._quad_viewer is not None:
            self._quad_viewer.set_is_mr(is_mr)

    def set_color_mode(self, mode: str):
        if self._quad_viewer is not None:
            self._quad_viewer.set_color_mode(mode)

    def set_enhance_fractures(self, enabled: bool):
        if self._quad_viewer is not None:
            self._quad_viewer.set_enhance_fractures(enabled)

    def set_preview_enabled(self, enabled: bool):
        if self._quad_viewer is not None:
            self._quad_viewer.set_preview_enabled(enabled)

    def set_shrink_factor(self, factor: int):
        if self._quad_viewer is not None:
            self._quad_viewer._volume_view.set_shrink_factor(factor)

    def set_segmentation_mask(self, mask, origin, spacing):
        if self._quad_viewer is not None:
            self._quad_viewer.set_segmentation_mask(mask, origin, spacing)

    def set_detection_markers(self, markers):
        if self._quad_viewer is not None:
            self._quad_viewer.set_detection_markers(markers)

    def clear_detection_markers(self):
        if self._quad_viewer is not None:
            self._quad_viewer.clear_detection_markers()

    def set_slice_ratio(self, ratio: float):
        """二维模式：按比例跳层（作用于活动窗口）；三维模式：MPR 同步"""
        if self._mode == self.MODE_2D:
            # 多窗下作用于活动窗口 —— 各窗序列不同，跳同一比例没意义
            r = self.active_reader
            smin, smax = r.slice_range()
            if smax > smin:
                idx = int(round(smin + (smax - smin) * max(0.0, min(1.0, ratio))))
                r.set_slice(idx)
        elif self._quad_viewer is not None:
            self._quad_viewer.set_slice_ratio(ratio)

    def reset_view(self):
        if self._mode == self.MODE_2D:
            # 复位所有已载入图像的窗口（每窗独立记录自己的相机状态）
            for r in self._readers_2d:
                if r.isHidden():
                    continue
                try:
                    if r._reader_output is None:
                        continue
                    r._fit_camera()
                    r._render()
                except Exception:   # noqa: BLE001
                    pass
        elif self._quad_viewer is not None:
            self._quad_viewer.reset_view()

    # ---------- 内部 ----------
    def _set_buttons_enabled(self, enabled: bool):
        self._btn_2d.setEnabled(enabled)
        # 三维按钮还要看模态是否支持：X 线载入后 set_data 会走这里，
        # 若无条件置 True 会把 set_3d_available(False) 的禁用覆盖掉。
        self._btn_3d.setEnabled(bool(enabled) and getattr(self, "_3d_available", True))

    def _ensure_volume(self):
        if self._reader is None or self._volume_built:
            return
        # 三维管线构建（体渲染 + MPR 三视图）是同步重操作，大序列可能卡顿数秒；
        # 用内嵌遮罩盖住视图（loading_ui），避免用户以为程序卡死。
        # 这里是同步阻塞，动画会停在最后一帧 —— 但面板本身比系统进度框直观。
        self._build_pending = True
        with loading_ui.busy_scope(self, "正在重建三维模型，请稍候…",
                                   sub="构建体渲染与 MPR 三视图"):
            try:
                quad = self.quad_viewer()
                quad.set_reader(
                    self._reader,
                    window_center=self._window_center,
                    window_width=self._window_width,
                    dicom_data=self._dicom_data,
                )
                quad.set_window_level(self._window_width, self._window_center)
                self._volume_built = True
            finally:
                self._build_pending = False

    def _apply_visibility(self):
        if self._mode == self.MODE_2D:
            if self._quad_host is not None:
                self._quad_host.hide()
            # 兜底：即便 _quad_host 被某种时序漏藏，也强制把三维子视图的
            # 「左/右」方位字藏掉，杜绝与二维阅读器的方位字叠加重复。
            if self._quad_viewer is not None:
                self._quad_viewer.set_orientation_visible(False)
            # 多窗：显示网格容器（容器内各窗口的显隐由 _layout_readers 管）
            self._view_grid_host.show()
            self._view_grid_host.raise_()
            for r in self._readers_2d:
                if not r.isHidden():
                    r.show()
                    r._position_widgets()
        else:
            # 三维：隐藏整个二维网格（而不是只藏主窗口，副窗口会漏出来）
            self._view_grid_host.hide()
            quad = self.quad_viewer()          # 确保 host 已创建
            if self._quad_host is not None:
                self._quad_host.show()
                self._quad_host.raise_()
            quad.show()
            quad.raise_()
            quad.set_orientation_visible(True)


def apply_global_theme(app, key=None):
    """应用界面皮肤。

    Args:
        app: QApplication 实例。
        key: 皮肤键（graphite / midnight / ivory / celadon）。
            省略时沿用用户上次的选择，首次启动为 graphite。

    Returns:
        实际生效的皮肤键。

    皮肤系统本身的异常**不能挡住软件启动**：出错就退回 Fusion 默认外观，
    并把 traceback 写进 error_log.txt（界面难看但能用，比打不开强）。
    """
    try:
        return theme.apply(app, key)
    except Exception:              # noqa: BLE001
        try:
            with open(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "error_log.txt"), "a", encoding="utf-8") as fh:
                fh.write("主题应用失败：\n" + traceback.format_exc() + "\n")
        except Exception:          # noqa: BLE001
            pass
        return theme.DEFAULT_SKIN


def main():
    import traceback
    try:
        vtkOutputWindow.SetGlobalWarningDisplay(0)
        # 报告质控的屏幕框选/截图要求「逻辑坐标 = 物理像素」，
        # 必须在 QApplication 创建之前声明 DPI aware（晚了设置无效）。
        try:
            import reportqc_bridge as _rqc
            _rqc.ensure_dpi_aware()
        except Exception:       # noqa: BLE001
            pass
        app = QtWidgets.QApplication(sys.argv)

        # ---- 网页一键导入：单实例闸门 ----
        # 浏览器扩展用 zhigu:// 协议唤起本程序，每次点击都会起一个新进程。
        # 这里必须在授权弹窗和 MainWindow 之前判断：已有实例在跑就把唤醒
        # 参数转发过去并立刻退出，否则用户每点一次按钮就多弹一次授权窗。
        # 全 try/except 包裹：桥接出任何问题都只是"没有这个功能"，不能影响启动。
        _web_bridge = False
        try:
            import web_bridge
            if web_bridge.bootstrap(sys.argv):
                sys.exit(0)
            _web_bridge = True
        except Exception:
            _web_bridge = False

        # PyQt5 默认行为：槽函数里抛出未捕获异常 -> qFatal -> 进程直接消失，
        # 用户只看到「点一下就闪退」，控制台才有 traceback。装一个全局兜底，
        # 把异常写成 error_log.txt 并弹窗提示，让问题可追查而不是靠猜。
        def _excepthook(exc_type, exc_value, exc_tb):
            text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            try:
                with open(os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "error_log.txt"), "a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except Exception:       # noqa: BLE001
                pass
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            try:
                QtWidgets.QMessageBox.critical(
                    None, "程序出错",
                    "发生了未预期的错误，程序不会退出。\n"
                    "详细信息已写入软件目录 error_log.txt。\n\n%s"
                    % ("".join(traceback.format_exception_only(exc_type, exc_value))[:500]))
            except Exception:       # noqa: BLE001
                pass

        sys.excepthook = _excepthook
        
        # 界面皮肤：Fusion 打底（各平台外观一致、Win7 也能吃 QSS），
        # 配色/描边/圆角全部由 theme.py 的 token 生成。
        # 用户上次选的皮肤存在注册表，首次启动用「石墨」。
        app.setStyle("Fusion")
        apply_global_theme(app)

        # ---- 授权门禁（必须在 MainWindow 创建之前）----
        # 放在样式表之后：激活对话框才会继承这套深色主题，
        # 不会弹出白底黑字、与主程序割裂的窗口。
        # 授权不通过就 return，绝不进入 app.exec_()。
        try:
            import license_ui
        except Exception:
            error_log = "error_log.txt"
            with open(error_log, "w", encoding="utf-8") as f:
                f.write("Error: 授权界面模块导入失败\n")
                f.write("Traceback:\n")
                f.write(traceback.format_exc())
            QtWidgets.QMessageBox.critical(
                None, "软件安装不完整",
                "授权模块加载失败，软件无法启动。\n\n"
                "请确认软件目录完整，或联系供应商重新安装。")
            sys.exit(1)

        if not license_ui.require_license(None):
            # 用户未激活或主动取消：正常退出，不算错误
            sys.exit(0)

        window = MainWindow()
        window.showMaximized()
        # 报告质控悬浮小窗：v1.23.1 起**默认不自动弹出**（使用频率不高，
        # 启动就占一块屏幕反而碍事）。用户要它自动出来就在
        # 「设置 → 报告质控」里勾上；这里照常调用，由设置决定弹不弹。
        # 内部延迟 1.2s：等主窗口画完再定位，否则小窗可能落到屏幕外。
        try:
            import reportqc_bridge as _rqc_auto
            _rqc_auto.startup_auto_show()
        except Exception:       # noqa: BLE001
            pass
        if _web_bridge:
            try:
                web_bridge.bind_window(window)
            except Exception:
                pass
        sys.exit(app.exec_())
    except Exception as e:
        # 记录详细错误信息到文件
        error_log = "error_log.txt"
        with open(error_log, "w", encoding="utf-8") as f:
            f.write(f"Error: {str(e)}\n")
            f.write("Traceback:\n")
            f.write(traceback.format_exc())
        print(f"Error occurred. Check {error_log} for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
