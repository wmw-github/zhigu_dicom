# -*- coding: utf-8 -*-
"""报告质控流水线：截图 → OCR → 字段提取 → 规则质控 → 分级结果。

本模块是**纯逻辑层**：不 import 任何 Qt UI 类，只吃 ``numpy`` BGR 图、
吐 :class:`QCResult`。因此可以在没有 QApplication 的环境下单测
（见 ``test_pipeline.py``），UI 层（:mod:`reportqc.ui.mini_window`）只负责
把 QCResult 渲染出来。

分级口径（与悬浮窗颜色一一对应）
--------------------------------
.. code-block:: text

    危急值                    → red      红色    最高优先，建议弹窗/声音
    高危错别字(high)          → orange   橙色    需人工修改
    中危错别字 / 逻辑 / 语法  → yellow   黄色    建议人工复核
    低危 / 字段缺失 / 提示    → yellow   黄色（弱）
    无问题                    → green    绿色

OCR 置信度门控（规划 §8 的落地）
--------------------------------
「隔肌/膈肌」「肺大泡/肺大疱」这类**既是 OCR 高频混淆对、又是错别字规则
库字对**的词，检测结果无法自证是「OCR 认错了」还是「报告真写错了」。
处理：命中错别字时，回查该词所在 OCR 行的置信度，低于
``OCR_SUSPECT_CONF`` (0.85) 时降为 ``low`` 并打上
``ocr_suspect`` 标记，UI 标注为「可能是识别误差，请人工确认」。
危急值检测**不做此降级** —— 危急值关键词笔画清晰、上下文冗长，
实测几乎不受形近字混淆影响（2026-10-24 实测：危急值关键词全部正确识别）。
"""

from .fields import extract_fields

__all__ = ["QCResult", "QCIssue", "run_pipeline", "rebuild_result", "LEVELS",
           "OCR_SUSPECT_CONF", "LEVEL_COLORS"]

# 低于该 OCR 置信度的错别字 → 标记为「疑似识别误差」
OCR_SUSPECT_CONF = 0.85

LEVEL_CRITICAL = "critical"
LEVEL_HIGH = "high"
LEVEL_MEDIUM = "medium"
LEVEL_LOW = "low"

LEVELS = (LEVEL_CRITICAL, LEVEL_HIGH, LEVEL_MEDIUM, LEVEL_LOW)

# 整体结论 → 颜色（与 mini_window 的颜色标识一致）
LEVEL_COLORS = {
    "red": "#dc3545",
    "orange": "#e8850c",
    "yellow": "#d8b90a",
    "green": "#28a745",
}


class QCIssue(object):
    """一条质控问题。``kind`` 用于 UI 分组与图标，``level`` 决定颜色。"""

    __slots__ = ("level", "kind", "message", "suggestion",
                 "source", "confidence", "ocr_conf", "ocr_suspect")

    def __init__(self, level, kind, message, suggestion="",
                 source="", confidence=0.0, ocr_conf=None, ocr_suspect=False):
        self.level = level
        self.kind = kind
        self.message = message
        self.suggestion = suggestion
        self.source = source              # 触发问题的原文片段
        self.confidence = confidence      # 规则自身置信度
        self.ocr_conf = ocr_conf          # 该片段对应 OCR 行的置信度（可为 None）
        self.ocr_suspect = ocr_suspect    # 是否为「疑似 OCR 误报」

    def as_dict(self):
        return dict((k, getattr(self, k)) for k in self.__slots__)


class QCResult(object):
    """一次质控的完整结果。"""

    __slots__ = ("level", "issues", "fields", "text", "ocr_lines",
                 "elapsed", "note")

    def __init__(self):
        self.level = "green"
        self.issues = []
        self.fields = None
        self.text = ""
        self.ocr_lines = []
        self.elapsed = 0.0
        self.note = ""                    # 附加说明（如「未识别到正文」）

    # -- 便捷查询 --------------------------------------------------------

    def by_level(self, level):
        return [i for i in self.issues if i.level == level]

    @property
    def has_critical(self):
        return self.level == "red"

    def summary_text(self):
        """悬浮窗摘要行，例如「2 项危急值 · 3 项待修改」。"""
        if not self.issues:
            return "未发现问题"
        parts = []
        for lv, name in ((LEVEL_CRITICAL, "危急值"), (LEVEL_HIGH, "待修改"),
                         (LEVEL_MEDIUM, "待复核"), (LEVEL_LOW, "提示")):
            n = len(self.by_level(lv))
            if n:
                parts.append("%d 项%s" % (n, name))
        return " · ".join(parts)


def _ocr_conf_of(word, ocr_lines):
    """回查 ``word`` 出现在哪个 OCR 行里，返回该行最高置信度；找不到返回 None。"""
    if not word:
        return None
    best = None
    for ln in ocr_lines:
        text = getattr(ln, "text", "") or ""
        if word and word in text:
            conf = float(getattr(ln, "confidence", 0.0) or 0.0)
            if best is None or conf > best:
                best = conf
    return best


def _apply_ignore(issues, words):
    """丢弃命中忽略词的问题。

    匹配范围是 message 和 source（原文片段）两者，因为错别字类问题的
    关键词在 message 里（「肺大泡」应为「肺大疱」），而逻辑类问题的
    关键词往往在 source 里。
    """
    words = [str(w).strip() for w in (words or []) if str(w).strip()]
    if not words:
        return issues
    out = []
    for it in issues:
        hay = "%s %s" % (it.message or "", it.source or "")
        if any(w in hay for w in words):
            continue
        out.append(it)
    return out


def rebuild_result(record, issue_rows):
    """从数据库行重建 :class:`QCResult`（历史窗 / 详情窗复用同一套 UI）。

    Parameters
    ----------
    record : dict
        ``qc_records`` 的一行（``QCDatabase.query_records`` 的返回元素）。
    issue_rows : list of dict
        同一次检测的 ``qc_issues`` 行。

    Notes
    -----
    库里不存 :class:`QCIssue` 的 Python 对象，这里按落库字段逐项还原。
    ``ocr_conf`` 为 ``None`` 表示该问题不是错别字类（没有对应 OCR 行）。
    """
    from .fields import ReportFields

    res = QCResult()
    res.level = record.get("overall_level") or "green"
    res.text = record.get("full_text") or ""
    res.elapsed = float(record.get("elapsed_ms") or 0) / 1000.0
    res.note = record.get("note") or ""

    f = ReportFields()
    f.patient_name = record.get("patient_name") or ""
    f.gender = record.get("gender") or ""
    f.age = record.get("age") or ""
    f.exam_no = record.get("exam_no") or ""
    f.accession_no = record.get("accession_no") or ""
    f.modality = record.get("modality") or ""
    f.body_part = record.get("body_part") or ""
    f.exam_time = record.get("exam_time") or ""
    res.fields = f

    for row in issue_rows or []:
        ocr_conf = row.get("ocr_conf")
        res.issues.append(QCIssue(
            level=row.get("level") or LEVEL_LOW,
            kind=row.get("kind") or "",
            message=row.get("message") or "",
            suggestion=row.get("suggestion") or "",
            source=row.get("source") or "",
            confidence=float(row.get("confidence") or 0.0),
            ocr_conf=None if ocr_conf is None else float(ocr_conf),
            ocr_suspect=bool(row.get("ocr_suspect")),
        ))
    return res


def _dedupe_issues(issues):
    """按 (kind, message) 归并重复问题，标注出现次数。

    同一个错别字常在「影像所见」和「诊断结果」里各出现一次，规则引擎会
    分别报；小窗口里显示两条一模一样的条目没有信息量。归并后只在
    message 尾部标注「（出现 N 次）」。
    """
    order = []
    seen = {}
    for it in issues:
        key = (it.kind, it.message)
        if key in seen:
            seen[key].append(it)
        else:
            seen[key] = [it]
            order.append(key)
    out = []
    for key in order:
        group = seen[key]
        first = group[0]
        if len(group) > 1:
            first.message = "%s（出现 %d 次）" % (first.message, len(group))
        out.append(first)
    return out


def _decide_level(issues):
    if not issues:
        return "green"
    if any(i.level == LEVEL_CRITICAL for i in issues):
        return "red"
    if any(i.level == LEVEL_HIGH for i in issues):
        return "orange"
    if any(i.level == LEVEL_MEDIUM for i in issues):
        return "yellow"
    return "yellow"


def run_pipeline(image_bgr, image_width=None, ocr_scale=2.0,
                 gender=None, report_type=None, dicom_info=None,
                 engine=None, checker=None, ignore_words=None,
                 learned_words=None, custom_typos=None, extra_terms=None,
                 drop_low=False):
    """执行一次完整质控。

    Parameters
    ----------
    image_bgr : numpy.ndarray
        截图的 BGR 图（:func:`reportqc.pacs.capture.pixmap_to_bgr` 的输出）。
    image_width : int, optional
        图宽（像素）。不给则从 ``image_bgr.shape`` 取。
    ocr_scale : float
        OCR 前放大倍数，默认 2.0（PACS 小字号宋体需要）。
    gender : str, optional
        患者性别。不给则用 OCR 识别到的；都没有则传「未知」。
    report_type : str, optional
        报告类型/模态。不给则用 OCR 识别到的设备类型。
    dicom_info : dict, optional
        DICOM 侧患者信息，键：``patient_name`` / ``accession_number`` /
        ``gender`` / ``age``。**提供即启用交叉核对**（CT3D 独有能力），
        与报告不一致会产生 ``level=high`` 的「张冠李戴」告警。
    engine : PPOCREngine, optional
        复用已建好的引擎（避免重复加载模型）。不给则内部新建。
    checker : SmartTextChecker, optional
        复用已建好的规则引擎。
    ignore_words : list of str, optional
        忽略词表。问题文本（message 或原文片段）包含其中任意一词即被丢弃。
        用户在详情窗点「忽略该词」后由 controller 从数据库读取并传入。
    learned_words : list of str, optional
        **误判学习**学到的词。用户在详情窗点「标记为误报」后，数据库会把该条
        问题的关键词学下来；这里传进来，命中即不再报，实现「标记一次，
        以后不再提示」。
    custom_typos : dict, optional
        用户自定义错别字 ``{错词: {"correction": 正确写法, "severity": 级别}}``。
        每次运行注入 checker，用户在设置窗新增后立即生效。
    extra_terms : list of str, optional
        用户补充的医学术语白名单（英文缩写等），避免被当成无意义字母。
    drop_low : bool, optional
        丢弃全部低危（``low``）问题。用户在设置里关掉「显示提示级问题」后
        由 controller 传入；危急值/高危/中危不受影响。

    Returns
    -------
    QCResult
    """
    import time
    t0 = time.time()

    res = QCResult()

    if image_bgr is None or getattr(image_bgr, "size", 0) == 0:
        res.level = "green"
        res.note = "截图为空"
        res.elapsed = time.time() - t0
        return res

    if image_width is None:
        image_width = int(image_bgr.shape[1])

    # --- 1. OCR ---------------------------------------------------------
    if engine is None:
        from ..ocr.ppocr_engine import PPOCREngine
        engine = PPOCREngine(scale=ocr_scale)
    lines = engine(image_bgr) or []
    res.ocr_lines = lines

    # --- 2. 字段提取 ----------------------------------------------------
    fields = extract_fields(lines, image_width=image_width)
    res.fields = fields

    if gender is None:
        gender = fields.gender or "未知"
    if report_type is None:
        report_type = fields.modality or "未知"

    # --- 3. 决定送入质控的正文 -----------------------------------------
    text = fields.report_text
    if not text.strip():
        # 用户可能只框选了正文区的一部分，或标题不在框内：
        # 退化为「全部 OCR 行拼起来」
        text = "\n".join((getattr(l, "text", "") or "") for l in lines)
        text = text.strip()
    if not text:
        res.text = ""
        res.level = "green"
        res.note = "未识别到报告正文"
        _check_fields_completeness(res, fields)
        res.elapsed = time.time() - t0
        return res

    res.text = text

    # --- 4. 规则质控 ----------------------------------------------------
    if checker is None:
        from ..qc.checker import SmartTextChecker
        checker = SmartTextChecker(enable_ai=False)
    # 用户自定义错别字 / 术语白名单：每次运行都注入一遍（热更新）。
    # checker 是缓存的复用实例，不注入的话「设置里加了词但不生效」。
    if custom_typos or extra_terms:
        try:
            checker.apply_user_rules(custom_typos, extra_terms)
        except Exception as exc:                        # noqa: BLE001
            print("[reportqc] 注入用户规则失败: %s" % exc)
    raw = checker.check_with_intelligence(text, gender=str(gender or "未知"),
                                          report_type=str(report_type or "未知"))

    issues = []

    # 4.1 危急值 —— 最高优先，不做 OCR 降级
    for cv in raw.get("critical_values") or []:
        kw = cv.get("keyword") or cv.get("word") or ""
        issues.append(QCIssue(
            level=LEVEL_CRITICAL,
            kind="critical_value",
            message="危急值：%s（%s）" % (kw, cv.get("category") or cv.get("description") or ""),
            suggestion="按危急值制度立即通知临床医师并记录",
            source=(cv.get("context") or "").strip()[:120],
            confidence=1.0,
        ))

    # 4.2 错别字 —— 带 OCR 置信度门控
    for tp in raw.get("typos") or []:
        word = tp.get("word") or ""
        corr = tp.get("correction") or ""
        sev = (tp.get("severity") or "medium").lower()
        level = LEVEL_HIGH if sev in ("high", "critical") else (
            LEVEL_MEDIUM if sev == "medium" else LEVEL_LOW)
        ocr_conf = _ocr_conf_of(word, lines)
        suspect = False
        if ocr_conf is not None and ocr_conf < OCR_SUSPECT_CONF:
            # 识别本身就没把握 → 不能一口咬定是报告写错
            level = LEVEL_LOW
            suspect = True
        issues.append(QCIssue(
            level=level,
            kind="typo",
            message="「%s」应为「%s」" % (word, corr) if corr else "「%s」疑似错别字" % word,
            suggestion=tp.get("suggestion") or ("改为「%s」" % corr if corr else ""),
            source=(tp.get("context") or "").strip()[:120],
            confidence=float(tp.get("confidence") or 0.0),
            ocr_conf=ocr_conf,
            ocr_suspect=suspect,
        ))

    # 4.3 逻辑 / 语法 / 语义
    for lg in raw.get("logic_issues") or []:
        sev = (lg.get("severity") or "medium").lower()
        level = LEVEL_MEDIUM if sev in ("medium", "high") else LEVEL_LOW
        issues.append(QCIssue(
            level=level,
            kind="logic",
            message=lg.get("description") or lg.get("type") or "逻辑问题",
            suggestion=lg.get("suggestion") or "",
            source=(lg.get("text") or lg.get("keywords") or ""),
            confidence=float(lg.get("confidence") or 0.0),
        ))
    for sm in raw.get("semantic_issues") or []:
        issues.append(QCIssue(
            level=LEVEL_MEDIUM,
            kind="semantic",
            message=sm.get("description") or sm.get("type") or "语义问题",
            suggestion=sm.get("suggestion") or "",
            source=(sm.get("text") or ""),
            confidence=float(sm.get("confidence") or 0.0),
        ))
    for ctx in raw.get("contextual_issues") or []:
        issues.append(QCIssue(
            level=LEVEL_LOW,
            kind="context",
            message=ctx.get("description") or ctx.get("type") or "上下文问题",
            suggestion=ctx.get("suggestion") or "",
            source=(ctx.get("text") or ""),
            confidence=float(ctx.get("confidence") or 0.0),
        ))
    for w in raw.get("warnings") or []:
        if isinstance(w, dict):
            msg = w.get("description") or w.get("message") or str(w)
            src = w.get("text") or ""
        else:
            msg, src = str(w), ""
        # 「AI检测功能不可用」这类环境提示不该算报告问题
        if "AI" in msg and "不可用" in msg:
            continue
        issues.append(QCIssue(level=LEVEL_LOW, kind="warning",
                              message=msg, source=src))

    # --- 5. DICOM 交叉核对（可选） --------------------------------------
    if dicom_info:
        issues.extend(_cross_check_dicom(fields, dicom_info))

    # 忽略词 + 误判学习词：都是「命中就不报」，合并一次过滤。
    # 分开存是为了让设置窗能区分「主动忽略」和「误判学到」，过滤时等价。
    suppress = list(ignore_words or []) + list(learned_words or [])
    if suppress:
        issues = _apply_ignore(issues, suppress)
    if drop_low:
        # 危急值/字段缺失提示保留：前者是安全底线，后者是「这次没框对」
        # 的操作性提示，都不该被「少点噪音」的开关吞掉。
        issues = [i for i in issues
                  if i.level != LEVEL_LOW
                  or i.kind in ("critical_value", "field_missing")]

    res.issues = _dedupe_issues(issues)
    res.level = _decide_level(res.issues)

    # --- 6. 字段完整性 --------------------------------------------------
    _check_fields_completeness(res, fields)

    res.elapsed = time.time() - t0
    return res


def _cross_check_dicom(fields, dicom_info):
    """报告字段 vs DICOM 患者信息。不一致 = 「张冠李戴」级错误。"""
    out = []
    dname = (dicom_info.get("patient_name") or "").strip()
    if dname and fields.patient_name:
        if dname != fields.patient_name and dname not in fields.patient_name \
                and fields.patient_name not in dname:
            out.append(QCIssue(
                level=LEVEL_HIGH,
                kind="dicom_mismatch",
                message="姓名不一致：报告「%s」 vs DICOM「%s」" % (
                    fields.patient_name, dname),
                suggestion="确认是否为同一患者，严防张冠李戴",
                source=fields.patient_name,
                confidence=1.0,
            ))
    dacc = (dicom_info.get("accession_number") or "").strip()
    if dacc and fields.accession_no:
        if dacc != fields.accession_no and dacc not in fields.accession_no \
                and fields.accession_no not in dacc:
            out.append(QCIssue(
                level=LEVEL_HIGH,
                kind="dicom_mismatch",
                message="影像号不一致：报告「%s」 vs DICOM「%s」" % (
                    fields.accession_no, dacc),
                suggestion="核对申请单与影像数据是否匹配",
                source=fields.accession_no,
                confidence=1.0,
            ))
    return out


def _check_fields_completeness(res, fields):
    """关键字段没识别到时给一条 low 提示（不参与红色/橙色判定）。"""
    missing = []
    if not fields.patient_name:
        missing.append("患者姓名")
    if not (fields.accession_no or fields.exam_no):
        missing.append("影像号")
    if not (fields.findings or fields.conclusion):
        missing.append("报告正文")
    if missing:
        res.issues.append(QCIssue(
            level=LEVEL_LOW,
            kind="field_missing",
            message="未识别到：%s" % "、".join(missing),
            suggestion="调整框选范围后重试，或确认报告是否已填写",
        ))
        res.level = _decide_level(res.issues)
