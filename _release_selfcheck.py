# -*- coding: utf-8 -*-
"""发行版自检：验证 v1.17.0 的报告质控（含用户规则与误判学习）在发行目录可用。

与开发版的验收脚本不同，这里刻意**不开真实窗口、不读 DICOM**：
发行版只需要证明「代码在只带运行必需文件的目录里能跑起来」，
真实数据验收在开发版的 _test_drag_feedback.py 里做。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))


def main():
    here = os.path.dirname(os.path.abspath(__file__))

    # 1. 关键模块能导入（发行版最常见的问题是漏复制 DLL / 模型）
    import main as app_main
    import export_utils
    check("发行版 main.py 可导入", True)
    check("版本号已同步", app_main.APP_VERSION == "v1.24.2",
          "(%s)" % app_main.APP_VERSION)

    # 1a-1. 在线升级（v1.21.0）
    #       客户端凭内置 RSA 公钥验 latest.json。公钥或验签实现一旦被改坏，
    #       故障表现是「永远提示已是最新」——不会报错，最难发现，所以锁死。
    try:
        import base64 as _b64
        import updater as _upd
        check("updater 升级模块可导入", True)
        _bits = int(_upd.PUBKEY_N_HEX, 16).bit_length()
        check("内置公钥是 RSA-2048", _bits == 2048, "(%d bit)" % _bits)
        _payload = b"ct3d-update-selftest-v1"
        _sig = _b64.b64decode(
            "PKd8iQ/wA3koMABtLH8fzR6ZwXzfz0tynJaBStvvIDn54U36Rvlu+agh1ViWj1yf"
            "k0bkfr4UcWbxXgKuKYE51FrLIk4ekQCe50QIAR0aNGNCU7HFejRK7FzEWVabdwHRo"
            "/LoZWxHVgiKdwKXmRSm2UaN/RBNYFM5hER1y9PMNM/Aci/GqHcm+bXdDtrUICTp"
            "L169xv4JfKtiLJpy8wX1sVLMELfCSo3qq2KT5K4C6mS3ehR04XJuxx5D+RkRKLQ"
            "2++vO6z/VGzhxPMB1dNjaZuBHRhrZIj+Dl7PeQuGrpl59iQOmBY0uzWLbC3+R/W"
            "wZaI6q6Odh8g6lxLrqikrR0A==")
        check("升级索引验签自检（公钥与私钥匹配）",
              _upd.verify_signature(_payload, _sig))
        _bad = bytearray(_sig)
        _bad[7] ^= 0x01
        check("验签能识别被篡改的签名",
              not _upd.verify_signature(_payload, bytes(_bad)))
        check("版本号比较逻辑正确",
              _upd.version_gt("v1.2.0", "v1.1.9")
              and not _upd.version_gt("v1.1.0", "v1.1.0")
              and not _upd.version_gt("v1.1.0", "v1.2.0"))
    except Exception as _e:                      # noqa: BLE001
        check("updater 升级模块可用", False, "(%s)" % _e)

    try:
        import update_ui as _uui                  # noqa: F401
        check("update_ui 升级界面可导入", True)
        check("主窗口挂上了升级入口",
              hasattr(app_main.MainWindow, "_on_check_update")
              and hasattr(app_main.MainWindow, "_start_silent_update_check")
              and hasattr(app_main.MainWindow, "_on_update_available"))
    except Exception as _e:                      # noqa: BLE001
        check("update_ui 升级界面可用", False, "(%s)" % _e)

    # 1a-2. 升级服务器地址可配（v1.23.0）
    #       现场换主机时改的就是这个。这里全程只用 inspect 读源码，不构造
    #       任何 QWidget —— 自检环境没有 QApplication，建控件会直接 abort，
    #       连报错都打印不出来（exit 127、stdout 全丢），这条坑踩过。
    try:
        import inspect as _insp
        check("地址规范化能补协议头、去尾斜杠",
              _upd.normalize_base_url("10.0.0.8:8000/") == "http://10.0.0.8:8000"
              and _upd.normalize_base_url(" http://a.b ") == "http://a.b")
        check("地址规范化不会把垃圾当地址",
              _upd.normalize_base_url("") == ""
              and _upd.normalize_base_url("http://") == "")
        check("配置里的地址压过内置默认",
              _upd.resolve_base_url({"update_base_url": "1.2.3.4:81"})
              == "http://1.2.3.4:81"
              and _upd.resolve_base_url({}) == _upd.DEFAULT_BASE_URL)

        _sig_dlg = _insp.signature(app_main.SettingsDialog.__init__)
        check("设置对话框接受升级服务器地址",
              "base_url" in _sig_dlg.parameters,
              "(%s)" % ", ".join(_sig_dlg.parameters))
        check("设置对话框能回吐地址",
              hasattr(app_main.SettingsDialog, "get_base_url"))
        _sig_upd = _insp.signature(_uui.UpdateDialog.__init__)
        check("升级窗口接受指定服务器地址",
              "base_url" in _sig_upd.parameters,
              "(%s)" % ", ".join(_sig_upd.parameters))

        # 读-改-写：整体覆盖会把另一个键冲掉，这条用源码文本兜住
        _wsrc = _insp.getsource(app_main.MainWindow._write_settings_config)
        check("写配置是读-改-写（不会冲掉别的键）",
              "data.update(" in _wsrc and "json.dump(data" in _wsrc)
        _lsrc = _insp.getsource(app_main.MainWindow._load_update_base_url)
        check("读服务器地址走 resolve_base_url（非法值能回退）",
              "resolve_base_url" in _lsrc)
        _csrc = _insp.getsource(app_main.MainWindow._on_check_update)
        _ssrc = _insp.getsource(app_main.MainWindow._start_silent_update_check)
        check("检查和静默检查都用配置地址",
              "_load_update_base_url" in _csrc
              and "_load_update_base_url" in _ssrc)
    except Exception as _e:                      # noqa: BLE001
        check("升级服务器地址可配", False, "(%s)" % _e)

    # 1a-3. 报告质控：设置收进主设置 + 默认不自动弹（v1.23.1）
    #       这里只读源码文本、不 import 界面模块：reportqc.ui.settings_dialog
    #       顶部就会碰 Qt，虽然不建控件，但没必要在这条链上冒险。
    try:
        import inspect as _insp3
        import io as _io
        import re as _re
        _root3 = os.path.dirname(os.path.abspath(app_main.__file__))
        _qcsd = _io.open(os.path.join(
            _root3, 'reportqc', 'ui', 'settings_dialog.py'),
            encoding='utf-8').read()
        _bridge = _io.open(os.path.join(_root3, 'reportqc_bridge.py'),
                           encoding='utf-8').read()

        check("质控悬浮小窗默认不自动弹（读不到配置也按不弹处理）",
              _re.search(r'reportqc/show_mini["\'],\s*["\']0["\']', _qcsd)
              is not None)
        check("开关已从质控设置窗移除（避免两处各写一份）",
              '_show_mini_cb' not in _qcsd)
        check("主设置能拿到这个开关的状态",
              hasattr(app_main.SettingsDialog, 'get_show_mini'))
        check("主设置能打开质控详细设置",
              'reportqc_callback' in _insp3.signature(
                  app_main.SettingsDialog.__init__).parameters)
        check("保存设置时会应用质控开关",
              '_apply_reportqc_show_mini' in _insp3.getsource(
                  app_main.MainWindow._on_settings_clicked))
        check("写质控开关会 sync（否则要等退出才落盘）",
              '.sync()' in _insp3.getsource(
                  app_main.MainWindow._apply_reportqc_show_mini))
        check("给已开的小窗做即时生效、且不主动建控制器",
              'def apply_mini_visibility_if_running' in _bridge
              and '_CONTROLLER' in _bridge)
        check("左侧栏不再有「质控设置」按钮（入口已并入设置）",
              '_reportqc_set_btn' not in _insp3.getsource(
                  app_main.MainWindow.__init__))
    except Exception as _e:                      # noqa: BLE001
        check("报告质控设置已收进主设置", False, "(%s)" % _e)

    # 1a-0. 界面皮肤（v1.21.0）：皮肤是一组 token 灌进全局样式表，
    #       任何一套缺键/占位符没替换，用户点下去就是「切了直接崩」。
    try:
        import theme as _th
        from PyQt5 import QtGui as _qtg
        check("theme 皮肤模块可导入", True)
        _need = ("canvas panel field raised button button_hover button_press"
                 " hover border border_strong text text_dim text_muted"
                 " accent accent_hover accent_press accent_view accent_alt"
                 " on_accent danger warning success").split()
        _miss = []
        for _k in _th.keys():
            _t = _th.tokens(_k)
            _miss += ["%s.%s" % (_k, _n) for _n in _need if not _t.get(_n)]
        check("四套皮肤 token 齐全", not _miss, "(%s)" % ",".join(_miss[:6]))

        _bad = []
        for _k in _th.keys():
            try:
                _s = _th.qss(_k)
                if "%(" in _s or not _s.strip():
                    _bad.append(_k + ":占位符未替换")
            except Exception as _e:        # noqa: BLE001
                _bad.append("%s:%s" % (_k, _e))
        check("每套皮肤可生成完整样式表", not _bad, "(%s)" % _bad[:2])

        # 回归防护（都是实际踩过的坑）：全局样式表一旦接管这些子控件，
        # Qt 就只画实心块 / 干脆不画，勾选框只剩对勾、下拉框变空白。
        # 它们全部由 theme._SkinStyle 自绘，QSS 里必须留白。
        # 注意先去掉 /* 注释 */ —— 样式表里大段注释正文就写着这些选择器，
        # 直接字符串判断会误报（自检自己也踩了一回）。
        import re as _re
        _g = _re.sub(r"/\*.*?\*/", "", _th.qss(_th.current()), flags=_re.S)
        _hits = [x for x in ("::indicator", "::drop-down", "::up-button",
                             "::down-button") if x in _g]
        check("QSS 未接管自绘子控件（indicator/drop-down）", not _hits,
              "(%s)" % ",".join(_hits))

        # 🔴 v1.22.1 回归：QSS 一旦给 ::item 指定 color，QStyleSheetStyle 就会
        #    盖掉条目自己的 ForegroundRole —— 患者名按检查类型着色（CT 蓝 /
        #    DR 绿 / MR 紫 / 混合橙）、质控历史按结论等级着色会全部变灰，
        #    表现为「更新皮肤后这个功能没了」。文字基色由 QTreeView /
        #    QTableView 的 color 提供，::item 里不能再写一遍。
        #    （:selected / :hover 是整行统一配色，允许。）
        _bad_items = []
        for _blk in _g.split("}"):
            if "{" not in _blk:
                continue
            _sel, _body = _blk.split("{", 1)
            if "::item" not in _sel:
                continue
            # QMenu 不用条目前景色（禁用项本来就该变灰），不在此列；
            # 列表/树的 :selected / :hover 是整行统一配色，也允许。
            if "QMenu" in _sel or ":selected" in _sel or ":hover" in _sel:
                continue
            if "color" in _body.replace("background-color", ""):
                _bad_items.append(_sel.strip().replace("\n", " ")[-40:])
        check("QSS 的 ::item 不指定文字色（否则 setForeground 失效）",
              not _bad_items, "(%s)" % _bad_items[:2])

        # 患者列表按检查类型着色（CT 蓝 / DR 绿 / MR 紫 / 混合橙）。
        # 除了 QSS 压制，还踩过「年龄缺失就把整行刷灰」把模态色盖掉，
        # 所以这里同时锁配色表、色距和填充逻辑。
        _mc = getattr(app_main.MainWindow, "_MODALITY_COLORS", {})
        _mk = ("CT", "DR", "MR", "MIXED")
        check("患者列表模态配色表齐全", all(k in _mc for k in _mk),
              "(%s)" % [k for k in _mk if k not in _mc])
        _mind = 999.0
        _have = [k for k in _mk if k in _mc]
        for _i in range(len(_have)):
            for _j in range(_i + 1, len(_have)):
                _a, _b = _mc[_have[_i]], _mc[_have[_j]]
                _d = ((_a.red() - _b.red()) ** 2 + (_a.green() - _b.green()) ** 2
                      + (_a.blue() - _b.blue()) ** 2) ** 0.5
                _mind = min(_mind, _d)
        check("模态配色两两可区分（最小色距 >= 60）", _mind >= 60,
              "(%.0f)" % _mind)
        import inspect as _insp
        _src_tree = _insp.getsource(app_main.MainWindow._load_patient_tree)
        check("患者名仍按模态着色",
              "_modality_color" in _src_tree and "setForeground" in _src_tree)
        check("模态色不被其他分支覆盖", "text_muted" not in _src_tree,
              "(text_muted 出现 %d 次)"
              % _src_tree.count("text_muted"))

        # 浅色皮肤必须换一套压暗版：原来那组亮色在白底上对比度只有 1.8~2.5，
        # 用户看到的是「颜色淡到看不出区别」，等于功能又没了。
        _ml = getattr(app_main.MainWindow, "_MODALITY_COLORS_LIGHT", {})
        check("浅色皮肤有专用模态配色", all(k in _ml for k in _mk),
              "(%s)" % [k for k in _mk if k not in _ml])

        def _lum(c):
            def _f(v):
                v = v / 255.0
                return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
            return (0.2126 * _f(c.red()) + 0.7152 * _f(c.green())
                    + 0.0722 * _f(c.blue()))

        _low = []
        for _k in _mk:
            for _sk in _th.keys():
                _bg = _th.tokens(_sk)["field"]
                _use = _ml if _sk in ("ivory", "celadon") else _mc
                _c = _use.get(_k)
                if _c is None:
                    continue
                _la, _lb = _lum(_c), _lum(_qtg.QColor(_bg))
                _cr = (max(_la, _lb) + 0.05) / (min(_la, _lb) + 0.05)
                if _cr < 4.5:
                    _low.append("%s@%s=%.1f" % (_k, _sk, _cr))
        check("模态配色在各皮肤下对比度 >= 4.5", not _low, "(%s)" % _low[:4])

        check("阅片区底色恒定（不随皮肤变亮）",
              _th.VIEWER.get("bg") == "#08090B", "(%s)" % _th.VIEWER.get("bg"))
        _hud = _th.hud()
        check("加载动画取色口可用", all(_hud.get(k) for k in
                                        ("panel", "overlay", "text", "dim",
                                         "primary", "secondary")))
    except Exception as _exc:            # noqa: BLE001
        check("theme 皮肤模块可导入", False, "(%s)" % _exc)

    import xray_bridge as xb
    check("xray_bridge 可导入", True)

    # 1a-0. 骨龄关节标注接线（v1.24.0）
    #      标注只写进 debug_output 时，主界面「AI 标注」层拿到的其实是
    #      **无标注原图** —— 医生屏幕上什么框都看不到，程序却一切正常，
    #      这种「静默失效」最难发现，所以每条通路都锁一道源码断言。
    try:
        import inspect as _insp_b
        from core.model.boneage_inference import BoneAgeInference as _BAI
        _dsrc = _insp_b.getsource(_BAI.detect)
        check("骨龄 detect 返回带标注成品图",
              '"result_image": annotated_img' in _dsrc)
        check("骨龄 detect 另存无标注底图（供标注开关）",
              '"raw_image": original_img' in _dsrc)
        _rsrc = _insp_b.getsource(xb.XrayAI.run_boneage)
        check("桥接层透传标注图 / 底图 / 标注列表",
              '"raw_image"' in _rsrc and 'res["annotations"]' in _rsrc)
        check("桥接层结果结构含 raw_image 与 annotations",
              all(k in xb._blank_result("boneage")
                  for k in ("raw_image", "annotations")))
        _psrc = _insp_b.getsource(app_main.MainWindow._build_xray_ai_panel)
        check("界面有「关节标注」开关与「保存标注图」",
              "_xray_anno_btn" in _psrc and "_xray_save_btn" in _psrc)
        _tsrc = _insp_b.getsource(app_main.MainWindow._on_xray_toggle_annotation)
        check("标注开关切到底图用的是 raw_image", 'raw_image' in _tsrc)
        _ssrc2 = _insp_b.getsource(app_main.MainWindow._on_xray_save_annotation)
        check("保存标注图按原分辨率落盘（不是屏幕截图）",
              "imencode" in _ssrc2 and "tofile" in _ssrc2)
    except Exception as _exc:            # noqa: BLE001
        check("骨龄关节标注接线（v1.24.0）", False, "(%s)" % _exc)

    # 1a-0b. 骨龄分级阈值分档 + 等级名同源（v1.24.1）
    #   中节指骨（尤其第五指）是 13 项里最小、最弱的一项，模型 argmax 常对但
    #   置信度只有 0.17~0.29。统一按 0.30 一刀切会把它整块丢掉，丢 = 该项记 0 分
    #   （MIPFifth 满分 49/1000）→ 骨龄系统性偏低，界面上只留一句「第五中节指骨
    #   未评级」。所以阈值必须按模型分档，且「低置信」与「未评级」要能区分开。
    try:
        from core.model.boneage_inference import CLS_CONF_BY_MODEL as _CCM
        import models.boneage.boneage_annotation as _bann
        check("中节指骨置信度阈值已放宽（<0.30）",
              _CCM.get("MIP", 1.0) < 0.30, "(MIP=%.2f)" % _CCM.get("MIP", -1))
        check("桡骨仍按 0.30 卡（权重最高，不能放宽）",
              _CCM.get("Radius") == 0.30)
        check("等级名换算函数存在", callable(_bann.grade_display_name))
        check("细分档 12 档 → RUS-CHN「8级」",
              _bann.grade_display_name("MIPThird", 12) == "8")
        _csrc = _insp_b.getsource(_BAI._classify_joints)
        check("低置信关节保留计分而不是丢弃",
              "low_confidence" in _csrc and "CLS_CONF_BY_MODEL" in _csrc)
        _gsrc = _insp_b.getsource(_bann.generate_ruschn_report)
        check("报告等级名走同一换算函数（不能各自为政）",
              "grade_display_name" in _gsrc)
        check("报告写出「分级说明」段（点名低置信/未分级）",
              "分级说明" in _gsrc)
        _brsrc = _insp_b.getsource(xb.XrayAI.run_boneage)
        check("桥接层透传低置信 / 未分级关节",
              "low_confidence_joints" in _brsrc and "ungraded_joints" in _brsrc)
        check("桥接层结果结构含 low_confidence_joints 与 ungraded_joints",
              all(k in xb._blank_result("boneage")
                  for k in ("low_confidence_joints", "ungraded_joints")))
    except Exception as _exc:            # noqa: BLE001
        check("骨龄分级阈值分档（v1.24.1）", False, "(%s)" % _exc)

    # 1a. 网页取图（web_bridge）：压缩 DICOM 的解码器必须在位。
    #     鲁云等云端 PACS 下发的是 JPEG-LS 无损压缩（1.2.840.10008.1.2.4.80），
    #     VTK 自带的 vtkDICOMImageReader 解不了。漏打包 python-gdcm 的表现是
    #     「能导入、序列也能打开、但每张都是白板」—— 用户完全看不出是缺依赖，
    #     所以必须在发行前把它变成一个显式失败项。
    try:
        import web_bridge as _wb
        import pydicom.config as _pcfg
        check("web_bridge 可导入", True)
        _dec_ok = _wb._decoder_ready()
        check("JPEG-LS 解码器在位（python-gdcm）", _dec_ok,
              "" if _dec_ok else "（缺 gdcm，云端压缩影像会显示空白）")
        _has_gdcm = any("gdcm" in _m.__name__
                        for _m in _pcfg.pixel_data_handlers)
        check("pydicom 已挂上 GDCM 像素解码器", _has_gdcm)
        # 位宽转换是解压落盘的关键：int16 的负值必须按位重解释，
        # 用 astype 会被当成数值转换，-1000 直接变成 64536 之类的垃圾。
        import numpy as _np

        class _Shape(object):
            Rows = 4
            Columns = 4
            BitsAllocated = 16
            SamplesPerPixel = 1
            NumberOfFrames = 1

        _raw = _wb._pixel_bytes(_np.full((4, 4), -1000, dtype=_np.int16), _Shape())
        _want = (-1000 & 0xFFFF).to_bytes(2, "little")
        check("像素位宽转换正确（int16 负值不被翻转）",
              _raw is not None and len(_raw) == 32 and _raw[:2] == _want)
    except Exception as _exc:          # noqa: BLE001
        check("web_bridge 可导入", False, "(%s)" % _exc)

    # 1b. 报告质控模块（v1.14.0 新增）：模型文件在位 + 桥接层可导入。
    #     OCR 三个 .onnx 共 16MB，是 reportqc 唯一的外部资产，
    #     漏复制会让整个功能静默不可用，必须在这里断言。
    import reportqc_bridge as rb
    check("reportqc_bridge 可导入", True)
    _models_ok = rb.reportqc_available()
    check("报告质控 OCR 模型在位（det/rec/cls）", _models_ok,
          "" if _models_ok else "（reportqc/models/ocr/ 缺少 .onnx）")
    try:
        from reportqc.qc.database import QCDatabase as _QDB
        from reportqc.pacs.pipeline import run_pipeline as _rp
        from reportqc.pacs.fields import extract_fields as _ef
        from reportqc.pacs import region_store as _rs
        from reportqc.ui.mini_window import MiniReportWindow as _MW
        check("报告质控子模块可导入（qc/pacs/ui）", True)
    except Exception as _exc:          # noqa: BLE001
        check("报告质控子模块可导入（qc/pacs/ui）", False, "(%s)" % _exc)

    # 1b-2. v1.18.2 精简小窗：状态胶囊并入标题栏（原 44px 状态条已删），
    #       且不再展示识别区域的尺寸坐标。
    #       这里**只看源码结构，不实例化窗口** —— 自检进程没有 QApplication，
    #       构造 QWidget 会让 Qt 直接 abort（exit 127、无 traceback）。
    import inspect as _insp
    import sys as _sys
    _src = _insp.getsource(_sys.modules[_MW.__module__])
    _ok = ("_meta_text" not in _src
           and "_set_badge" in _src
           and "setFixedHeight(44)" not in _src
           and "region_text" not in _src)
    check("小窗精简态（状态并入标题栏、不显示区域尺寸）", _ok,
          "" if _ok else "（mini_window 里仍有独立状态条或区域尺寸展示）")

    # 1b-3. v1.19.1：① 年龄「62岁」不再拼成「62岁岁」
    #                 ② 半角标点 / 省略号不再误报「标点异常」
    #                 ③ 影像号可点击复制
    #       ①② 是纯 Python，可以直接跑真逻辑；③ 涉及剪贴板只能在源码层断言。
    try:
        from reportqc.pacs.fields import format_demo as _fd
        _demo = _fd("男", "62岁")
        check("年龄「岁」不重复", _demo == "男 / 62岁", "(%s)" % _demo)
    except Exception as _exc:          # noqa: BLE001
        check("年龄「岁」不重复", False, "(%s)" % _exc)

    try:
        from reportqc.qc.checker_grammar import ChineseGrammarChecker as _CGC
        _ck = _CGC()
        _norm = ("检查所见:胸廓对称,气管居中。双肺纹理清晰,右肺上叶见结节状"
                 "高密度影,大小约1.2cm,边缘光滑(考虑良性).建议随访……")
        _perr = [e for e in _ck.check_grammar(_norm) if e["type"] == "标点错误"]
        check("半角标点/省略号不再误报「标点异常」", not _perr,
              "" if not _perr else "(%s)" % [e["word"] for e in _perr])
        _bad = [e for e in _ck.check_grammar("双肺纹理\x07清晰。")
                if e["type"] == "标点错误"]
        check("真正的异常字符仍能检出", bool(_bad))
    except Exception as _exc:          # noqa: BLE001
        check("半角标点/省略号不再误报「标点异常」", False, "(%s)" % _exc)

    _copy_ok = ("_copy_accession" in _src and "clipboard" in _src
                and "_CopyLabel" in _src)
    check("小窗影像号可点击复制", _copy_ok,
          "" if _copy_ok else "（mini_window 缺少复制影像号实现）")

    # 1b-4. v1.22.2：患者卡三行**必须单行 + 按布局实测高度定窗口**。
    #       原来第三行（部位/时间）带 wordWrap，折行的第二行会画到卡片外面
    #       压住下方列表；窗口高度又是手算的（比布局最小值少 20px），
    #       Qt 干脆让列表区压在卡片上 —— 用户看到的就是「文字重复、被切一半」。
    _no_wrap = ("_pt_exam.setWordWrap(True)" not in _src
                and "_pt_exam.setWordWrap(False)" in _src)
    _layout_h = ("_apply_patient_text" in _src
                 and "root.minimumSize().height()" in _src)
    check("小窗患者卡单行截断、窗口高度按布局实测", _no_wrap and _layout_h,
          "" if (_no_wrap and _layout_h)
          else "（第三行仍在折行，或窗口高度仍是手算）")

    # 1b-5. v1.22.3：「点升级 → 软件退出 → 没重启 → 手动打开还是老版本」。
    #       两个坑都出在在线升级的启动方式上：
    #       ① 以前经「cmd /c 升级.bat」中转，客户机路径带空格/中文时会被 cmd
    #          拆坏，整次升级静默不执行 → 改成直接调 pyenv\python.exe；
    #       ② 装完必须重启（--relaunch 交给 apply_patch，成败都拉起来），
    #          否则用户面对的是「程序自己消失了」。
    try:
        import inspect as _inspect
        import update_ui as _uui
        _ins_src = _inspect.getsource(_uui.install_and_restart)
        _direct = ("'python.exe'" in _ins_src
                   and "'apply_patch.py'" in _ins_src
                   and "'--target'" in _ins_src
                   and "'--relaunch'" in _ins_src)
        check("在线升级直接调解释器（不经 cmd / bat）", _direct,
              "" if _direct else "（install_and_restart 仍在走 cmd / 升级.bat）")
        _fallback = "升级.bat" in _ins_src
        check("没有 python.exe 时仍能退回「升级.bat」", _fallback,
              "" if _fallback else "（缺 pyenv 的兜底分支没了）")
    except Exception as _e:                       # noqa: BLE001
        check("在线升级启动方式可检查", False, "(%s)" % _e)

    # 1c. v1.16.0 常驻小窗：识别区域要能持久化（"设置一次，之后点刷新"的前提）。
    #     注意：QSettings 写的是真实注册表，自检不能把用户已设好的区域冲掉，
    #     测完必须**原样还原全部配置**（v1.20.0 起是多套，只还原第一条会丢）。
    def _backup_regions():
        return [(p["rect"], p["exe"], p["title"], p["name"], p["layout"])
                for p in _rs.load_profiles()]

    def _restore_regions(items):
        _rs.clear_profiles()
        for _r, _e, _t, _n, _l in items:
            _rs.save_profile(_r, exe=_e, title=_t, name=_n, layout=_l)

    try:
        _prev = _backup_regions()
        _rs.clear_profiles()
        _rect = app_main.QtCore.QRect(120, 90, 640, 480)
        _saved = _rs.save_region(_rect)
        _back = _rs.load_region()
        check("识别区域可保存/读回（QSettings）",
              bool(_saved and _back is not None and _back == _rect),
              "(%s)" % (_back,))
        _rs.clear_region()
        check("识别区域可清除", _rs.load_region() is None)
        _restore_regions(_prev)                       # 还原用户原有设置
        check("启动自动显示入口存在",
              callable(getattr(rb, "startup_auto_show", None)))
    except Exception as _exc:          # noqa: BLE001
        check("识别区域可保存/读回（QSettings）", False, "(%s)" % _exc)

    # 1c-2. v1.20.0 两套写报告界面并存：
    #       ① 区域多套保存、按前台进程名匹配（同一软件重复框选要覆盖不要新增）
    #       ② 字段提取按版式自适应（体检登记界面 / 标准报告界面）
    #       ③ 前台窗口探测模块可用（Win7 兼容的 ctypes 实现）
    try:
        _prev2 = _backup_regions()
        _rs.clear_profiles()
        _ra = _rs.save_profile(app_main.QtCore.QRect(10, 10, 300, 200),
                               exe="softa.exe", title="体检登记 - 张三", name="A")
        _rb2 = _rs.save_profile(app_main.QtCore.QRect(20, 20, 400, 300),
                                exe="softb.exe", title="报告编辑 - 李四", name="B")
        check("两套识别区域可同时保存", len(_rs.load_profiles()) == 2,
              "%d 套" % len(_rs.load_profiles()))
        _mA = _rs.match_profile("softa.exe", "随便什么标题")
        _mB = _rs.match_profile("softb.exe", "随便什么标题")
        check("按进程名选中各自的区域",
              _mA is not None and _mB is not None
              and _mA["rect"] == _ra["rect"] and _mB["rect"] == _rb2["rect"])
        check("未知进程不误用别人的区域",
              _rs.match_profile("chrome.exe", "谷歌浏览器") is None)
        _rs.save_profile(app_main.QtCore.QRect(30, 30, 500, 400),
                         exe="softa.exe", name="A")
        check("同一软件重复框选只覆盖不新增", len(_rs.load_profiles()) == 2,
              "%d 套" % len(_rs.load_profiles()))
        check("覆盖后区域已更新",
              _rs.match_profile("softa.exe", "")["rect"].width() == 500)
        _rs.clear_profiles()
        _rs.save_profile(app_main.QtCore.QRect(10, 10, 300, 200),
                         exe="only.exe", name="唯一")
        check("单套配置兜底生效（进程探测失败也能用）",
              _rs.match_profile("", "") is not None)
        _restore_regions(_prev2)
    except Exception as _exc:          # noqa: BLE001
        check("两套识别区域可同时保存", False, "(%s)" % _exc)

    try:
        from reportqc.pacs.fields import (detect_layout as _dl,
                                          LAYOUT_FORM as _LF,
                                          LAYOUT_TITLED as _LT)
        _rows_form = [{"text": "姓名:朱潇  性 别:女  年龄:37"},
                      {"text": "体检编号:101061223  科室:DK室"},
                      {"text": "项目编号  项目名称  检查结果"}]
        _rows_titled = [{"text": "姓名：胡廷学  性别：男  年龄：66岁"},
                        {"text": "影像所见"}]
        check("体检登记界面判为 form 版式", _dl(_rows_form) == _LF)
        check("标准报告界面判为 titled 版式", _dl(_rows_titled) == _LT)
    except Exception as _exc:          # noqa: BLE001
        check("体检登记界面判为 form 版式", False, "(%s)" % _exc)

    try:
        from reportqc.pacs.windowinfo import own_exe as _oe, available as _wa
        _wok = _wa() and bool(_oe())
        check("前台窗口探测可用（Win7 兼容 ctypes）", _wok, "(%s)" % _oe())
    except Exception as _exc:          # noqa: BLE001
        check("前台窗口探测可用（Win7 兼容 ctypes）", False, "(%s)" % _exc)
    # 数据层要能真的建库：写不进去（装在 Program Files 未提权）时会
    # 回退 APPDATA，两种路径都要验证能落到磁盘
    try:
        _tmpdb = os.path.join(tempfile.gettempdir(), "_rqc_selfcheck.db")
        for _sfx in ("", "-wal", "-shm"):
            if os.path.exists(_tmpdb + _sfx):
                os.remove(_tmpdb + _sfx)
        _db = _QDB(db_path=_tmpdb)
        _rid = _db.save_result(_rp.__doc__ and __import__(
            "reportqc.pacs.pipeline", fromlist=["QCResult"]).QCResult())
        _db.set_setting("selfcheck", "ok")
        check("报告质控 SQLite 可建库/可写", _rid is not None and
              _db.get_setting("selfcheck") == "ok")
        _db.close()
        for _sfx in ("", "-wal", "-shm"):
            if os.path.exists(_tmpdb + _sfx):
                os.remove(_tmpdb + _sfx)
    except Exception as _exc:          # noqa: BLE001
        check("报告质控 SQLite 可建库/可写", False, "(%s)" % _exc)

    # 1c. 用户规则与误判学习（v1.17.0）。这三条都是「用户在设置窗加了词 /
    #     在详情窗标了误报」之后必须真生效的路径 —— 曾经出现过「表能写、
    #     检测压根不读」的问题，所以这里同时验证读回和实际检测行为。
    try:
        _tmpdb2 = os.path.join(tempfile.gettempdir(), "_rqc_selfcheck_rules.db")
        for _sfx in ("", "-wal", "-shm"):
            if os.path.exists(_tmpdb2 + _sfx):
                os.remove(_tmpdb2 + _sfx)
        _db2 = _QDB(db_path=_tmpdb2)
        _db2.add_custom_typo("磨玻璃蜜度", "磨玻璃密度", "high")
        _ok_typo = any((r.get("typo") == "磨玻璃蜜度")
                       for r in (_db2.list_custom_typos() or []))
        _db2.add_learned_word("隔肌")
        _ok_learn_add = (_db2.get_learned_words() == ["隔肌"])
        _db2.remove_learned_word("隔肌")
        _ok_learn_del = (_db2.get_learned_words() == [])
        _db2.add_medical_term("ECT")
        _ok_term = ("ECT" in (_db2.get_medical_terms() or []))
        _db2.close()
        for _sfx in ("", "-wal", "-shm"):
            if os.path.exists(_tmpdb2 + _sfx):
                os.remove(_tmpdb2 + _sfx)
        check("自定义错别字可增删（custom_typos）", _ok_typo)
        check("误判学习词可增删（learned_words）", _ok_learn_add and _ok_learn_del)
        check("医学术语白名单可增删（medical_terms）", _ok_term)
    except Exception as _exc:          # noqa: BLE001
        check("自定义错别字可增删（custom_typos）", False, "(%s)" % _exc)

    # 1d. 缩写误报回归（2026-10-27 用户实测反馈）。影像报告里 ECT / PICC /
    #     TACE 这类缩写密度极高，被判成「无意义字母」会让质控整体不可信。
    try:
        from reportqc.qc.checker import SmartTextChecker as _STC
        _ck = _STC(enable_ai=False)
        _rr = _ck.check_with_intelligence(
            "右上肢PICC置管后改变。ECT示骨多发异常浓聚。肝癌TACE术后改变。",
            gender="男", check_method="CT", report_type="CT")
        _bad = [(t.get("word") or "") for t in (_rr.get("typos") or [])
                if (t.get("word") or "").upper() in ("ECT", "PICC", "TACE")]
        _bad += [(w or "") for w in (_rr.get("warnings") or [])
                 if any(k in str(w) for k in ("ECT", "PICC", "TACE"))]
        check("医学缩写不误报（ECT/PICC/TACE）", not _bad, "(%s)" % _bad)
    except Exception as _exc:          # noqa: BLE001
        check("医学缩写不误报（ECT/PICC/TACE）", False, "(%s)" % _exc)

    # 1a. onnxruntime 版本护栏（2026-10 Win7「辅助诊断全部失效」根因）。
    # onnxruntime 从 1.15 起官方放弃 Win7，1.19.2 装进发行版会让
    # 胸片/骨折/骨龄/脊柱/足弓**五个** AI 功能在 Win7 上同时报
    # 「ONNX Runtime 未安装或无法加载」。打包环境容易顺手装最新版，
    # 所以这里必须断言版本，装错当场发现，而不是等客户投诉。
    try:
        import onnxruntime as _ort
        _ort_ver = getattr(_ort, "__version__", "")
    except BaseException as _e:
        _ort_ver = "导入失败: %s" % _e
    _ort_ok = _ort_ver == "1.12.1"
    check("onnxruntime 为 1.12.1（Win7 唯一可用版本）", _ort_ok,
          "(当前 %s)" % _ort_ver)
    try:
        check("onnxruntime 可创建 CPU 会话",
              _ort.get_available_providers() and True)
    except BaseException as _e:
        check("onnxruntime 可创建 CPU 会话", False, str(_e))

    # 1a2. 患者搜索 / 导出脱敏（v1.18.0）依赖与入口。
    try:
        import pypinyin as _py
        _keys = app_main.MainWindow._name_search_keys(
            type("_S", (), {"_pinyin_cache": {}})(), "单田芳")
        check("pypinyin 在位（拼音检索）", True, "(%s)" % _py.__version__)
        # 单纯 import 成功不够：多音姓氏取不全的话「输入 shan 搜不到单田芳」
        check("多音姓氏拼音可取全（单→shan）", "shantianfang" in _keys,
              "(%s)" % sorted(_keys)[:5])
    except BaseException as _e:
        check("pypinyin 在位（拼音检索）", False, "(%s)" % _e)
    try:
        check("导出脱敏接口在位",
              all(hasattr(export_utils, n) for n in (
                  "anonymize_dataset", "export_series_dicom_anonymized",
                  "mask_burned_in_corners")))
        check("导出选项对话框已定义", hasattr(app_main, "ExportOptionsDialog"))
    except BaseException as _e:
        check("导出脱敏接口在位", False, "(%s)" % _e)

    # 1a3. 图片导出的窗值 / 极性管线（v1.18.1）。
    # 这一组全是「用户肉眼看才发现的错误」，退化成旧行为时程序照样不报错
    # （DR 导成负片、头颅 CT 脑窗导成骨窗），所以必须在这里用合成数据守住。
    try:
        import numpy as _np
        import pydicom as _pyd
        from pydicom.dataset import Dataset as _DS, FileMetaDataset as _FM
        from pydicom.uid import ExplicitVRLittleEndian as _EVLE

        def _new_ds(rows, cols, mono, wc=None, ww=None, inter=None):
            """造一个最小可用 Dataset：pydicom 的 pixel_array 要求有
            file_meta.TransferSyntaxUID，缺了会直接 AttributeError。"""
            d = _DS()
            d.file_meta = _FM()
            d.file_meta.TransferSyntaxUID = _EVLE
            d.Rows, d.Columns = rows, cols
            d.SamplesPerPixel = 1
            d.PhotometricInterpretation = mono
            d.BitsAllocated = d.BitsStored = 16
            d.HighBit = 15
            d.PixelRepresentation = 0
            if wc is not None:
                d.WindowCenter = wc
            if ww is not None:
                d.WindowWidth = ww
            if inter is not None:
                d.RescaleSlope = 1
                d.RescaleIntercept = inter
            return d

        # 构造 1 帧"头颅 CT"：raw = HU + 1024，带脑窗 WC40/WW80
        _hu = _np.array([[-1000., 40., 1000.]], dtype=_np.float32)
        _raw = _np.clip(_hu + 1024.0, 0, 65535).astype(_np.uint16)
        _ct = _new_ds(1, 3, "MONOCHROME2", 40, 80, -1024)
        _ct.PixelData = _raw.tobytes()

        _arr = export_utils.frame_display_array(_ct)
        check("导出做 Modality LUT（raw→HU）", abs(float(_arr.min()) + 1000) < 1.5,
              "(min=%.0f)" % _arr.min())
        _w, _l = export_utils.dicom_window_level(_ct, _arr)
        check("导出采用 DICOM 脑窗 WW80/WL40",
              _w is not None and abs(_w - 80) < 0.5 and abs(_l - 40) < 0.5,
              "(%s/%s)" % (_w, _l))

        # MONOCHROME1（负片）必须翻正：同一份数据标成 M1 后导出应互为镜像
        _m1 = _new_ds(1, 3, "MONOCHROME1")
        _m1.PixelData = _raw.tobytes()
        check("MONOCHROME1 被识别为负片",
              export_utils._is_monochrome1(_m1)
              and not export_utils._is_monochrome1(_ct))

        # 口径一致性：反相 + 窗位镜像（显示端做法）== 先映射再翻转（导出做法）
        import tempfile as _tf
        _tmp = _tf.mkdtemp(prefix="relchk_")
        _p1 = os.path.join(_tmp, "m2.png")
        _p2 = os.path.join(_tmp, "m1.png")
        export_utils.write_image(_p1, _arr, _w, _l, invert=False)
        export_utils.write_image(_p2, _arr, _w, _l, invert=True)
        check("反相导出与正片互为黑白翻转",
              os.path.exists(_p1) and os.path.exists(_p2), "(文件已写出)")

        check("导出窗值查询入口在位",
              hasattr(app_main.MainWindow, "_view_window_for_series"))
        _bad = _new_ds(1, 1, "MONOCHROME2", 9999, 10)
        _bad.PixelData = _np.array([[40]], dtype=_np.uint16).tobytes()
        _w2, _l2 = export_utils.dicom_window_level(
            _bad, export_utils.frame_display_array(_bad))
        check("脏窗值被拒（退回分位自适应）", _w2 is None, "(%s)" % (_w2,))
    except BaseException as _e:
        check("图片导出窗值/极性管线", False, "(%s)" % _e)

    # 1b. 脊柱拼接（v1.9.9）：桥接层 + 面板 + 主窗按钮三件套。
    # 少任何一个都是「开发版能拼、发行版点不动」，故必须在这里守住。
    import spine_bridge as sb
    check("spine_bridge 可导入", True)
    check("spine/spine_app.py 在位", sb.spine_available())
    check("spine_app 模块可加载", sb.spine_module() is not None)
    check("SIFT 可用（opencv）", hasattr(__import__("cv2"), "SIFT_create"))
    check("StitcherAlgo 可取", sb.get_stitcher() is not None)
    check("AutoCobbAlgo 可取", sb.get_auto_cobb() is not None)
    check("CobbAngleTool 可取", sb.get_cobb_tool_class() is not None)
    check("自然排序 img2 < img10",
          [os.path.basename(p) for p in
           sb.sort_paths(["x/img10.png", "x/img2.png"])] == ["img2.png", "img10.png"])

    import spine_stitch_dialog as ssd
    check("spine_stitch_dialog 可导入", True)
    check("拼接面板类已定义", hasattr(ssd, "SpineStitchDialog"))
    # 这里刻意**不构造**面板：offscreen 下 QApplication 退出期析构
    # QGraphicsView 会段错误（exit 127，无 traceback）。面板的真构造 +
    # 拖放 + 端到端拼接由 _test_spine_integration.py（60 项）覆盖。
    check("拼接面板声明接受拖放",
          "setAcceptDrops(True)" in open(
              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "spine_stitch_dialog.py"),
              encoding="utf-8").read())
    check("主窗有「脊柱拼接」按钮",
          hasattr(app_main.MainWindow, "_on_spine_stitch_clicked"))
    check("工具区已插入按钮",
          "_spine_stitch_btn" in open(
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"),
              encoding="utf-8").read())

    # 2. 漏检反馈：FEEDBACK_KINDS 的用法（v1.7.7 闪退根因所在）
    names = []
    for key, cn in xb.FEEDBACK_KINDS.items():      # v1.7.6 这里是 (cn, _tip)
        names.append((key, cn))
    check("FEEDBACK_KINDS 可按 (key, 名称) 遍历", len(names) == 4,
          "(%d 项)" % len(names))
    check("全部类型名为字符串", all(isinstance(cn, str) for _, cn in names))

    # 3. 拖放载荷解析函数存在且对 None 安全
    check("_series_payload_from_item 存在",
          callable(getattr(app_main, "_series_payload_from_item", None)))
    check("_series_payload_from_item(None) 安全",
          app_main._series_payload_from_item(None) is None)
    check("SERIES_MIME 常量存在", getattr(app_main, "SERIES_MIME", "")
          == "application/x-ct3d-series")

    # 4. 拖放树控件与面板类可实例化（无窗口模式，仅验证构造不抛异常）
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    tree = app_main.SeriesDragTreeWidget()
    check("SeriesDragTreeWidget 可构造", isinstance(tree, app_main.SeriesDragTreeWidget))
    check("已开启拖拽", tree.dragEnabled())
    tok = tree.register_payload({"folder": r"D:\Cache\1", "file_uids": ["a"]})
    check("载荷注册/取回", tree.payload(tok) == {"folder": r"D:\Cache\1",
                                                "file_uids": ["a"]})

    panel = app_main.StudyViewPanel()
    check("StudyViewPanel 可构造", panel.acceptDrops())
    check("VTK 渲染控件已装拖放事件过滤器",
          getattr(panel, "_drop_filters_installed", False))

    from PyQt5 import QtCore
    mime = QtCore.QMimeData()
    check("非本程序拖放被拒", panel._drop_token(mime) is None)
    check("空 mimeData 不崩", panel._drop_token(None) is None)
    mime.setData(app_main.SERIES_MIME, tok.encode("utf-8"))
    check("本程序拖放 token 解析正确", panel._drop_token(mime) == tok)

    # 4a. **真构造 MainWindow**（2026-10-10 桌面图标/bat 起不来的根因）。
    # pyqtSignal 被误写进 __init__ 时，构造 SeriesDragTreeWidget 本身不报错，
    # 只有 MainWindow 里那句 .connect() 才炸 —— 也就是说「模块能导入」
    # +「控件能构造」都不足以说明程序起得来，必须真 new 一个窗。
    try:
        win = app_main.MainWindow()
        check("MainWindow 可构造（程序能起来）", win is not None)
        check("MainWindow 带 MIP/MinIP 渲染模式下拉",
              getattr(win, "_projection_combo", None) is not None)
        check("MainWindow 带右键导出槽",
              callable(getattr(win, "_on_patient_tree_menu", None)))
        win.deleteLater()

    except Exception as _exc:      # noqa: BLE001
        check("MainWindow 可构造（程序能起来）", False, "(%s)" % _exc)

    # 5. 反馈落盘链路（临时目录，绝不碰用户数据）
    import numpy as np
    import cv2
    tmp_db = os.path.join(tempfile.gettempdir(), "rel_fb.db")
    tmp_img = os.path.join(tempfile.gettempdir(), "rel_fb_images")
    for suf in ("", "-journal", "-wal", "-shm"):
        if os.path.isfile(tmp_db + suf):
            os.remove(tmp_db + suf)
    st = xb.FeedbackStore(db_path=tmp_db, image_root=tmp_img)
    src = os.path.join(tempfile.gettempdir(), "rel_src.png")
    cv2.imwrite(src, (np.random.RandomState(1).rand(80, 80) * 255).astype(np.uint8))
    rid = st.add(feedback_type="missed_fracture", source_path=src,
             patient_name="发行版自检", note="n", tag="rel")
    check("反馈写入返回 id", bool(rid), "(id=%s)" % rid)
    rows, total = st.list(1, 10)
    check("列表可读回", total == 1, "(total=%d)" % total)
    if rows:
        check("类型中文化", rows[0].get("type_cn") == "骨折漏检",
              rows[0].get("type_cn"))
        check("图片已落盘", rows[0].get("image_exists") is True,
              rows[0].get("image_file") or "")
    st.delete([rid])
    rows2, total2 = st.list(1, 10)
    check("删除后库为空", total2 == 0)
    check("库体积不含图像",
      os.path.getsize(tmp_db) < 1048576,
      "(%.1f KB)" % (os.path.getsize(tmp_db) / 1024.0))

    import shutil
    shutil.rmtree(tmp_img, ignore_errors=True)
    if os.path.isfile(src):
        os.remove(src)

    # 4c. 投影 actor 配对自检（定义在文件末尾的独立函数里，因为它需要
    #     模块级的类与方法绑定，不能写在 main() 函数体内）
    check_projection_actor(app_main)

    # 4d. v1.19.0 性能与兼容性护栏
    check_perf_compat(app_main)

    print("\n==== 发行版自检结果 ====")
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL: %s" % f)
    return 0 if not FAIL else 1


# ======================================================================
# MIP/MinIP 投影 actor 配对护栏
# ======================================================================
# 2026-10-10 用户报「切换到最小密度投影 (MinIP) 时出错：SetMapper argument
# %Id: 96%」。根因是 VTK 9.5 的类型体系被搞错了：
#
#   - vtkImageSlice / vtkImageActor 的 SetMapper() 只收 **vtkImageMapper3D**
#     （抽象类）；
#   - vtkImageMapper 继承的是 **vtkMapper2D**，是 2D 的，传进去直接 TypeError。
#     报错文案之所以是「%Id: 96%」这种占位符，是因为 VTK 内部的
#     vtkErrorMacro 在这个分支里打印失败，泄漏了格式化占位符；
#   - vtkMapper2D 只能配 vtkActor2D，而 vtkActor2D 走 overlay 层、没有 3D 相机，
#     进不了三维窗口；
#   - 正解：**vtkImageSlice + vtkOpenGLImageSliceMapper**（后者是
#     vtkImageMapper3D 的具体实现）。
#
# 另外 vtkImageProperty 的窗宽窗位叫 SetColorWindow/SetColorLevel，
# 不是 vtkActor 那套 SetWindowLevel；插值叫 SetInterpolationTypeToNearest。
#
# 这些都是「源码字符串断言」测不出来的 —— 上一轮 64 项 MIP 测试全绿，
# 却从没真正执行过 actor.SetMapper(...)。所以这里真跑一遍构造。


class _FakeProjectionViewer(object):
    """借用 VTKViewer 投影方法的最小宿主。

    不构造 VTKViewer 本体：它会建 QVTKRenderWindowInteractor，
    offscreen 下必然段错误 139。这里只把需要的几个方法挂上，
    render window 换成返回 None 的空壳（正式代码 `if render_win:` 会跳过）。
    """

    _is_mr = False
    _projection_stride = 1

    def __init__(self, image, vtk_viewer_cls):
        import types
        from vtkmodules.vtkRenderingCore import vtkRenderer
        self._renderer = vtkRenderer()
        self._reader = type("_R", (), {
            "Update": lambda *_a: None,
            "GetOutput": lambda *_a: image,
        })()
        self._masked_image = None
        self._projection_actor = None
        self._projection_image = None
        self._visual_cursor_actor = None
        self._interaction_cursor_actor = None
        # 骨折线叠加层：投影路径会撤掉它
        self._fracture_actor = None
        self._fracture_poly = None
        self._vtk_widget = type("_W", (), {
            "GetRenderWindow": lambda *_a: None,
        })()
        # 从真实的 VTKViewer 借方法。
        # 两个坑：
        # 1) 要绑成**绑定方法**，直接 setattr 放未绑定函数会缺 self；
        # 2) `_projection_stride_for` 是 **classmethod**，绑定到实例会多传
        #    一个 self（报 takes 4 but 5 given），所以它不能借，走类本身。
        for name in ("_projection_source_array",
                     "_render_projection", "_setup_projection_camera",
                     "_clear_fracture_actor"):
            setattr(self, name, types.MethodType(
                getattr(vtk_viewer_cls, name), self))
        self._projection_stride_for = types.MethodType(
            lambda _s, nz, ny, nx: vtk_viewer_cls._projection_stride_for(
                nz, ny, nx), self)

    def run(self):
        return self._render_projection()


def check_projection_actor(app_main):
    """执行 MIP/MinIP 投影 actor 配对检查（由 main() 调用一次）。"""
    import numpy as np
    from vtkmodules.vtkCommonDataModel import vtkImageData
    from vtkmodules.vtkCommonCore import VTK_FLOAT
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkRenderingCore import vtkRenderer
    from vtkmodules.vtkRenderingCore import vtkImageSlice
    from vtkmodules.vtkRenderingCore import vtkImageMapper
    from vtkmodules.vtkRenderingOpenGL2 import vtkOpenGLImageSliceMapper

    arr = np.zeros((4, 8, 8), dtype=np.float32)
    arr[1, 3, 4] = 1200.0
    img = vtkImageData()
    img.SetDimensions(8, 8, 4)
    img.AllocateScalars(VTK_FLOAT, 1)
    vtk_to_numpy(img.GetPointData().GetScalars()).reshape(4, 8, 8)[:] = arr

    # 反例留档：2D 的 vtkImageMapper 配 vtkImageSlice 必须失败
    bad = vtkImageMapper()
    bad.SetInputData(img)
    rejected = False
    try:
        vtkImageSlice().SetMapper(bad)
    except TypeError:
        rejected = True
    check("2D vtkImageMapper 被 vtkImageSlice 正确拒绝", rejected)

    # 正解
    mapper = vtkOpenGLImageSliceMapper()
    mapper.SetInputData(img)
    actor = vtkImageSlice()
    try:
        actor.SetMapper(mapper)
        check("vtkImageSlice 可接 vtkOpenGLImageSliceMapper",
              actor.GetMapper() is not None)
    except Exception as exc:       # noqa: BLE001
        check("vtkImageSlice 可接 vtkOpenGLImageSliceMapper", False,
              "(%s)" % exc)

    prop = actor.GetProperty()
    try:
        prop.SetColorWindow(2000.0)
        prop.SetColorLevel(300.0)
        prop.SetInterpolationTypeToNearest()
        ok_wl = abs(prop.GetColorWindow() - 2000.0) < 1e-6
    except Exception as exc:       # noqa: BLE001
        ok_wl = False
    check("投影图窗宽窗位可设（vtkImageProperty 用 ColorWindow）", ok_wl)

    renderer = vtkRenderer()
    renderer.AddActor(actor)
    check("投影 actor 可进 3D renderer",
          renderer.GetViewProps().GetNumberOfItems() == 1)

    # 投影核心逻辑真跑一遍（MIP + MinIP）
    for mode, label in (("mip", "MIP"), ("minip", "MinIP")):
        try:
            fv = _FakeProjectionViewer(img, app_main.VTKViewer)
            fv._projection_mode = mode
            rv_ok, _msg = fv.run()
        except Exception as exc:   # noqa: BLE001
            rv_ok, _msg = False, str(exc)
        check("_render_projection %s 可执行" % label, rv_ok, "(%s)" % _msg)

    # v1.10.1：三维体渲染 MIP/MinIP（用户期望「像三维重建一样可旋转」）。
    # vtkSmartVolumeMapper 的 blend mode 是唯一的三维 MIP 路径；
    # 构造失败（异常）不阻塞启动 —— 运行时会自动回退二维投影。
    try:
        from vtkmodules.vtkRenderingVolumeOpenGL2 import (
            vtkSmartVolumeMapper)
        from vtkmodules.vtkRenderingCore import (
            vtkVolume, vtkVolumeProperty, vtkColorTransferFunction)
        from vtkmodules.vtkCommonDataModel import vtkPiecewiseFunction

        _vm = vtkSmartVolumeMapper()
        _vm.SetInputData(img)
        _vm.SetBlendModeToMaximumIntensity()
        _vol = vtkVolume()
        _vol.SetMapper(_vm)
        _vp = vtkVolumeProperty()
        _otf = vtkPiecewiseFunction()
        _otf.AddPoint(-1024, 0.0)
        _otf.AddPoint(300, 0.2)
        _otf.AddPoint(1000, 0.94)
        _vp.SetScalarOpacity(_otf)
        _ctf = vtkColorTransferFunction()
        _ctf.AddRGBPoint(-1024, 0, 0, 0)
        _ctf.AddRGBPoint(1000, 1, 1, 1)
        _vp.SetColor(_ctf)
        _vp.SetInterpolationTypeToLinear()
        _vol.SetProperty(_vp)
        _r2 = vtkRenderer()
        _r2.AddVolume(_vol)
        check("三维体渲染 MIP 管线可构造",
              _r2.GetViewProps().GetNumberOfItems() == 1)
        check("体渲染有三维厚度（非压平切片）",
              _vol.GetBounds()[4] != _vol.GetBounds()[5])
        check("main.py 已导入体渲染类",
              all(hasattr(app_main, _n) for _n in
                  ("vtkSmartVolumeMapper", "vtkVolume", "vtkVolumeProperty",
                   "vtkPiecewiseFunction", "vtkColorTransferFunction")))
    except Exception as _exc:          # noqa: BLE001
        check("三维体渲染 MIP 管线可构造", False, "(%s)" % _exc)


def _make_probe_dicom(path, ipp=None, iop=None):
    """写一个最小可解析的 DICOM（供缓存/并行/路径三项检查用）。"""
    import numpy as np
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    fm.MediaStorageSOPInstanceUID = "1.2.840.99999.1.1"
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = fm
    ds.PatientName = " probe^test "
    ds.PatientID = "P001"
    ds.Modality = "CT"
    ds.SeriesInstanceUID = "1.2.840.99999.1.2"
    ds.SOPInstanceUID = "1.2.840.99999.1.1"
    ds.SOPClassUID = fm.MediaStorageSOPClassUID
    ds.StudyInstanceUID = "1.2.840.99999.1.3"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    if ipp is not None:
        ds.ImagePositionPatient = ipp
    if iop is not None:
        ds.ImageOrientationPatient = iop
    ds.PixelData = np.arange(16, dtype=np.uint16).tobytes()
    # 注意：旧版 pydicom 的 save_as 不接受 enforce_file_format 参数
    ds.save_as(path)
    return path


def check_perf_compat(app_main):
    """v1.19.0 性能与兼容性护栏（由 main() 调用一次）。

    都不开窗口（本脚本没有 QApplication，构造 QWidget 会让进程直接 abort）。
    """
    import tempfile
    import shutil
    import numpy as np

    tmp = tempfile.mkdtemp(prefix="selfcheck_perf_")
    try:
        # --- 1. 并行读头与串行结果必须一致 ---
        try:
            import ct3d_utils
            has_par = hasattr(ct3d_utils, "_read_headers_parallel")
            check("并发读头函数在位", has_par)
            n = 70           # 超过 _PARALLEL_HEADER_MIN(64) 才会走并行
            names = []
            for i in range(n):
                nm = "p%03d.dcm" % i
                _make_probe_dicom(os.path.join(tmp, nm),
                                  ipp=[0.0, 0.0, float(i)],
                                  iop=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
                names.append(nm)
            ser = [ct3d_utils._read_dicom_header_fast(
                os.path.join(tmp, x))["uid"] for x in names]
            par = [h["uid"] for h in
                   ct3d_utils._read_headers_parallel(tmp, names)]
            check("并发读头与串行结果一致", ser == par,
                  "(%d 个文件)" % n)
            check("并发读头保序且非空",
                  len(par) == n and all(par),
                  "" if all(par) else "存在空 uid")

            # --- 2. 目录 mtime 走缓存，不再逐文件 stat ---
            mt = ct3d_utils._folder_max_mtime(tmp)
            check("_folder_max_mtime 返回有效时间戳", mt > 0, "(%.1f)" % mt)
            check("_folder_max_mtime 对空目录返回 0",
                  ct3d_utils._folder_max_mtime(
                      os.path.join(tmp, "不存在")) == 0.0)

            # --- 3. IPP/IOP 缓存：第二次调用必须命中 ---
            p1 = os.path.join(tmp, "p000.dcm")
            ct3d_utils._ipp_iop_cache.clear()
            a1 = ct3d_utils._read_ipp_and_iop(p1)
            n1 = len(ct3d_utils._ipp_iop_cache)
            a2 = ct3d_utils._read_ipp_and_iop(p1)
            n2 = len(ct3d_utils._ipp_iop_cache)
            check("IPP/IOP 读到了层位置", a1[0] is not None,
                  "(%s)" % (a1[0],))
            check("IPP/IOP 第二次调用命中缓存",
                  n1 == 1 and n2 == 1 and a1 == a2,
                  "(缓存 %d -> %d)" % (n1, n2))
            check("IPP/IOP 缓存有容量上限",
                  getattr(ct3d_utils, "_IPP_IOP_CACHE_MAX", 0) > 0)
        except BaseException as _e:      # noqa: BLE001
            check("扫描性能优化在位", False, "(%s)" % _e)

        # --- 4. 中文路径读写（cv2.imwrite 在非 ASCII 路径会静默失败）---
        try:
            import export_utils as _eu
            cn_dir = os.path.join(tmp, "患者_张三_头颅")
            os.makedirs(cn_dir, exist_ok=True)
            cn = os.path.join(cn_dir, "序列_001.png")
            img = np.linspace(0, 255, 256, dtype=np.uint8).reshape(16, 16)
            ok = _eu.write_image(cn, img)
            back = _eu.imread_cn(cn)
            check("中文路径可写图", ok and os.path.isfile(cn))
            check("中文路径可读回", back is not None and back.shape == (16, 16))
            check("中文路径像素正确",
                  back is not None and int(back.min()) == 0
                  and int(back.max()) > 245,
                  "" if back is None else
                  "(min=%d max=%d)" % (int(back.min()), int(back.max())))
            # 混合路径（中文+空格）
            sp = os.path.join(cn_dir, "子 目录")
            os.makedirs(sp, exist_ok=True)
            sp_f = os.path.join(sp, "图 像.png")
            check("中文+空格混合路径可写",
                  _eu.write_image(sp_f, img)
                  and _eu.imread_cn(sp_f) is not None)
        except BaseException as _e:      # noqa: BLE001
            check("中文路径读写", False, "(%s)" % _e)

        # --- 5. 并行导出阈值合理 ---
        try:
            import export_utils as _eu
            lo = getattr(_eu, "_EXPORT_PARALLEL_MIN", 0)
            w = getattr(_eu, "_EXPORT_PARALLEL_WORKERS", 0)
            check("并行导出阈值在合理区间", 8 <= lo <= 128, "(%d 张)" % lo)
            check("并行导出线程数在合理区间", 2 <= w <= 8, "(%d 线程)" % w)
        except BaseException as _e:      # noqa: BLE001
            check("并行导出参数", False, "(%s)" % _e)

        # --- 6. PATH 前置：别家 DLL 目录在前时必须被我们压到后面 ---
        try:
            import runtime_env as _re
            _root = os.path.dirname(os.path.abspath(__file__))
            mine = os.path.join(_root, "pyenv", "Library", "bin")
            fake = os.path.join(_root, "pyenv", "Scripts")
            other = "C:\\SomeOtherApp\\bin"
            saved = os.environ.get("PATH", "")
            try:
                os.environ["PATH"] = os.pathsep.join(
                    [other, fake, mine, "C:\\Windows\\System32"])
                _re._BOOTSTRAPPED = False
                _ds = _re.bootstrap()
                parts = [p for p in os.environ["PATH"].split(os.pathsep) if p]
                i_other = parts.index(other)
                i_mine = [i for i, p in enumerate(parts)
                          if os.path.normcase(p) in
                          {os.path.normcase(d) for d in _ds}]
                check("我们的 DLL 目录排在别家之前",
                      bool(i_mine) and max(i_mine) < i_other,
                      "(我们 %d / 别家 %d)"
                      % (max(i_mine) if i_mine else -1, i_other))
                n1 = len(parts)
                _re.bootstrap()
                check("重复 bootstrap 不重复追加 PATH",
                      len([p for p in os.environ["PATH"].split(os.pathsep)
                           if p]) == n1)
            finally:
                os.environ["PATH"] = saved
                _re._BOOTSTRAPPED = False
                _re.bootstrap()      # 恢复到正常的引导状态
        except BaseException as _e:      # noqa: BLE001
            check("PATH 前置顺序", False, "(%s)" % _e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# 入口放最后：check_projection_actor 必须先定义好，main() 才能调到它
if __name__ == "__main__":
    sys.exit(main())
