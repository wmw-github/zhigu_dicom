# -*- coding: utf-8 -*-
"""PACS 报告 OCR 文本 → 结构化字段提取。

针对真实 PACS 报告编辑界面的布局设计（见 2026-10-24 实测截图）：

.. code-block:: text

    ┌─ 基本信息 ──────────────────────────────┐┌─ 右侧面板 ─┐
    │ 姓名：王晨   性别：男   年龄：19          ││ 历史 个人  │
    │ 检查号：BY101432589   影像号：101432589  ││ 典型 描述  │
    │ 检查项目：…  设备类型：DR  检查部位：…    ││            │
    ├─────────────────────────────────────────┤│ 公共模板   │
    │ 影像所见                                 ││ 个人模板   │
    │ ┌───────────────────────────────────┐   ││            │
    │ │ （正文，可能为空）                 │   ││  诊断      │
    │ └───────────────────────────────────┘   ││  审核      │
    │ 诊断结果                                 ││  预览      │
    │ ┌───────────────────────────────────┐   ││  打印      │
    │ │ （正文）                           │   ││  临时保存  │
    │ └───────────────────────────────────┘   ││  退出      │
    └─────────────────────────────────────────┘└────────────┘

四个必须处理的现实问题（全部在真实截图上复现过）
----------------------------------------------
1. **同一水平线有多个「标签：值」对**（姓名/性别/年龄同行）→ OCR 给出多个
   box，先按 y 聚类成逻辑行，再把逻辑行按标签位置**切成多对**逐一提值。
2. **标签和值可能被 OCR 拆成两行**（裁掉半个字符时常见，实测复现：
   ``检查号：`` / ``BY101432589``）→ 标签行未取到值时，向下找第一个
   「不像标签」的行补值。
3. **右侧面板与底部按钮的文字 y 与正文重叠** → 用标题行 x 定义正文栏的
   右边界，并用停用词表过滤 UI 文字。
4. **同名标签出现多次**（基本信息区的「设备类型」与右侧面板下拉的
   「设备类型」）→ 第一个**非空**值生效，空值不覆盖已有值。

所有匹配都**容忍全角/半角冒号混用与冒号丢失**（OCR 常见噪声）。
"""

import re

__all__ = ["ReportFields", "extract_fields", "merge_lines", "y_of",
           "detect_layout", "format_demo", "LAYOUT_FORM", "LAYOUT_TITLED"]

# ---------------------------------------------------------------------------
# 标签表
# ---------------------------------------------------------------------------

def _spaced(word):
    """中文标签 → 「字间允许空白」的正则。

    真实界面里有 ``性　　别：女``（标签被拉宽对齐，中间是全角空格），
    OCR 会原样取回 ``性 别``。写死 ``性别`` 就永远匹配不上（v1.20.0 实测）。
    只对纯中文标签用，英文标签（``Name`` / ``Gender``）保持原样。
    """
    return r"\s*".join(word)


# (字段名, 正则)。``^`` 只用于「整行就是一个标签」的判定；
# 行内多对切分用去掉 ``^`` 的 :data:`_LABELS_ANY`。
_LABELS = [
    # 「体检编号」是体检登记软件（版式二）的检查主键，位置等价于「检查号」
    ("exam_no",      re.compile(r"^(%s|%s|%s|%s|检查ID|Check\s*No)"
                                % (_spaced("体检编号"), _spaced("体检号"),
                                   _spaced("检查号"), _spaced("登记号")))),
    ("accession_no", re.compile(r"^(%s|%s|%s|Accession\s*No)"
                                % (_spaced("影像号"), _spaced("影像ID"),
                                   _spaced("影像序号")))),
    ("patient_name", re.compile(r"^(%s|%s|%s|Name)"
                                % (_spaced("患者姓名"), _spaced("病人姓名"),
                                   _spaced("姓名")))),
    ("gender",       re.compile(r"^(%s|Gender)" % _spaced("性别"))),
    ("age",          re.compile(r"^(%s|Age)" % _spaced("年龄"))),
    ("exam_time",    re.compile(r"^(%s|%s|%s|Exam\s*Time)"
                                % (_spaced("检查时间"), _spaced("检查日期"),
                                   _spaced("报告时间")))),
    ("modality",     re.compile(r"^(%s|%s|Modality)"
                                % (_spaced("设备类型"), _spaced("设备")))),
    ("body_part",    re.compile(r"^(%s|%s|Body\s*Part)"
                                % (_spaced("检查部位"), _spaced("部位")))),
    ("exam_item",    re.compile(r"^(%s|%s|%s)"
                                % (_spaced("检查项目"), _spaced("项目"),
                                   _spaced("检查名称")))),
    ("exam_doctor",  re.compile(r"^(%s|%s|%s)"
                                % (_spaced("报告医生"), _spaced("报告医师"),
                                   _spaced("检查医生")))),
    ("audit_doctor", re.compile(r"^(%s|%s)"
                                % (_spaced("审核医生"), _spaced("审核医师")))),
    ("apply_no",     re.compile(r"^(%s|%s|%s)"
                                % (_spaced("申请单号"), _spaced("申请号"),
                                   _spaced("申请单")))),
]

_LABELS_ANY = [(n, re.compile(p.pattern.lstrip("^"))) for n, p in _LABELS]

# 只用来**截断取值**、本身不落字段的「伪标签」。
#
# 一行多对切分的规则是「值的右边界 = 下一个标签的起点」。真实界面里
# 标签右边常跟着不属于任何字段的 UI 文字：``体检编号:101061223  科室:DK室
# …  检索(I)``、``检查医生 厉彦平  检索(I)  所有科室(F)``。不把这些词
# 也算作边界，取值就会一路吃到行尾（v1.20.0 实测：检查号变成
# 「101061223  科室:DK室」）。命中即截断，但不赋值。
_BOUNDARY_WORDS = (
    "刷卡", "被替换人姓名", "读玻片号", "存玻片号", "档案信息",
    "科室", "检索", "所有科室", "追加项目",
    "小结医生", "小结日期", "患者来源", "住院/门诊号", "床号",
)

_BOUNDARY_ANY = re.compile("|".join(_spaced(w) for w in _BOUNDARY_WORDS))

# 冒号可选（OCR 常丢冒号或把全角认成半角）

# 正文区标题（可带「影像所见：」这类尾巴）。
# 版式二（体检登记界面）用「检查结果：」当所见标题，一并收进来 ——
# 版式一没有这个标题，多认一个不会误伤。
_SEC_FINDINGS = re.compile(r"^(影像(所见|表现|描述)|检查结果|检查所见|检查描述)")
_SEC_CONCLUSION = re.compile(r"^(诊断|结论|报告结论|意见)")

# 右侧面板 / 工具栏 / 底部按钮的 UI 文字。命中即丢弃，不作为正文。
_UI_STOPWORDS = {
    "历史", "个人", "典型", "描述", "公共模板", "个人模板", "要塞",
    "诊断", "审核", "预览", "打印", "临时保存", "退出", "疾病名称",
    "报告模板", "阴性", "阳性", "追查", "学习", "会诊", "基本信息",
    "详情", "申请单", "单组影像", "多组影像", "电子病历", "特殊字符",
    "其他操作", "检查方法", "临床信息", "检查条目", "患者来源",
    "住院/门诊号", "床号", "申请科室", "检查部位", "检查时间",
    "设备类型", "检查项目", "报告医生", "审核医生", "覆盖", "痕迹",
    # -- 版式二（体检登记界面）特有 --------------------------------------
    "体检登记列表", "项目描述及小结", "已小结病人", "未小结病人",
    "未体检列表", "项目编号", "项目名称", "小结医生", "小结日期",
    "读卡", "历史体检", "自动小结", "删除小结", "全部未检", "全部默认",
    "替代选取", "追加选取", "关键词", "恢复默认", "诊断维护", "图片报告",
    "刷卡", "被替换人姓名", "读玻片号", "存玻片号", "档案信息",
    "检查医生", "检索", "所有科室", "追加项目", "未体检", "体检编号",
}

# OCR 常把复选框方框认成这些字符，去掉了才能与停用词比对
_BOX_PREFIX = "口■□☑☒✓✔·〇◯○● "

# 纯噪声行（无中文且无数字字母）
_NOISE = re.compile(r"^[\W_]+$")


def y_of(box):
    """文本框 (4,2) 的竖直中心。box 形如 ``[[x,y],...]``。"""
    try:
        ys = [float(p[1]) for p in box]
        return sum(ys) / len(ys)
    except Exception:                              # noqa: BLE001
        return 0.0


def x_of(box):
    try:
        xs = [float(p[0]) for p in box]
        return min(xs)
    except Exception:                              # noqa: BLE001
        return 0.0


def h_of(box):
    try:
        ys = [float(p[1]) for p in box]
        return max(ys) - min(ys)
    except Exception:                              # noqa: BLE001
        return 0.0


def merge_lines(lines, y_tol_ratio=0.4):
    """把 OCR 行按竖直位置聚类成「逻辑行」，同一逻辑行按 x 排序拼接。

    真实界面里 ``姓名：王晨`` 与 ``性别：男`` 常在同一水平线，会被 OCR
    分成两个 box；不合并的话后面的标签匹配会漏掉一半字段。

    ``y_tol_ratio`` 默认 0.4：同一水平线的 box 的 y 中心差通常 < 3px，
    而相邻两行文字的中心差 ≈ 行高 × 1.4（实测 22px 行高差 9px），
    0.4 × 20 = 8px 能稳定分开两者（0.6 会把两行并成一行，实测复现）。

    Parameters
    ----------
    lines : list of objects
        有 ``.text`` / ``.box`` 属性的对象（:class:`TextLine` 或同名 dict）。
    """
    items = []
    for ln in lines:
        text = (getattr(ln, "text", None) or (ln.get("text") if isinstance(ln, dict) else "") or "")
        box = getattr(ln, "box", None)
        if box is None and isinstance(ln, dict):
            box = ln.get("box")
        if not text or box is None:
            continue
        items.append((y_of(box), x_of(box), h_of(box), text))
    if not items:
        return []

    items.sort(key=lambda t: (t[0], t[1]))
    # 行高取中位数，避免个别大标题把容差带偏
    heights = sorted(t[2] for t in items)
    med_h = heights[len(heights) // 2] if heights else 12.0
    tol = max(4.0, med_h * y_tol_ratio)

    rows, cur = [], [items[0]]
    for it in items[1:]:
        if it[0] - cur[-1][0] <= tol:
            cur.append(it)
        else:
            rows.append(cur)
            cur = [it]
    rows.append(cur)

    out = []
    for row in rows:
        row.sort(key=lambda t: t[1])
        out.append({
            "y": sum(t[0] for t in row) / len(row),
            "x": min(t[1] for t in row),
            "h": max(t[2] for t in row),
            "text": "  ".join(t[3] for t in row),
            # 分段坐标：``[(x, text), ...]`` 按 x 升序。
            # 版式二（体检登记界面）里左侧项目表格与右侧「检查结果/诊断建议」
            # 框处在同一水平线，合并成一行后**必须靠各段自己的 x 分栏**，
            # 只留 row["text"] 会把两栏内容混成一锅（v1.20.0 加）。
            "segs": [(t[1], t[3]) for t in row],
        })
    return out


def _split_row_pairs(text):
    """把一个逻辑行按标签位置切成多个 ``(字段名, 值)``。

    ``姓名：王晨  性别：男  年龄:19`` → ``[("patient_name","王晨"),
    ("gender","男"), ("age","19")]``。

    这是真实 PACS 界面的核心适配点：基本信息是一行多对布局，
    只取第一个标签会漏掉后面所有字段（实测复现）。

    ``字段名`` 为 ``None`` 的项是 :data:`_BOUNDARY_ANY` 命中的「伪标签」：
    它只起到截断上一个值的作用，调用方应跳过。
    """
    hits = []
    for name, pat in _LABELS_ANY:
        for m in pat.finditer(text):
            hits.append((m.start(), m.end(), name))
    for m in _BOUNDARY_ANY.finditer(text):
        hits.append((m.start(), m.end(), None))
    if not hits:
        return []
    hits.sort(key=lambda h: h[0])

    pairs = []
    for k, (s, e, name) in enumerate(hits):
        nxt = hits[k + 1][0] if k + 1 < len(hits) else len(text)
        val = text[e:nxt]
        val = re.sub(r"^[:：\s]+", "", val).strip()
        pairs.append((name, val))
    return pairs


def _is_ui_stopword(text):
    t = text.strip().strip(_BOX_PREFIX).strip(":：").strip()
    if not t:
        return True
    if t in _UI_STOPWORDS:
        return True
    # 「影像所见」「诊断结果」这类正文标题不算停用词
    if _SEC_FINDINGS.match(t) or _SEC_CONCLUSION.match(t):
        return False
    # 以停用词开头且很短（如「打印 」「阳性 」）也视为 UI 文字
    for w in _UI_STOPWORDS:
        if t.startswith(w) and len(t) <= len(w) + 2:
            return True
    return False


def format_demo(gender, age):
    """「男 / 62岁」。``age`` 自带「岁」时不重复拼（PACS 上常直接写「62岁」）。

    v1.19.1 修：原来无条件拼 ``"%s岁"``，OCR 取到的年龄已带「岁」时
    小窗上就显示成「62岁岁」。
    """
    g = (gender or "").strip() or "?"
    a = re.sub(r"[岁\s]+$", "", (age or "").strip())
    if not a:
        return "%s / ?" % g
    return "%s / %s岁" % (g, a)


class ReportFields(object):
    """一次提取的结构化结果。取不到的字段为空串（不猜、不填默认值）。"""

    __slots__ = (
        "patient_name", "gender", "age", "exam_no", "accession_no",
        "exam_time", "modality", "body_part", "exam_item",
        "exam_doctor", "audit_doctor", "apply_no",
        "findings", "conclusion", "report_text", "rows",
        # layout: 识别到的版式（form / titled），见 :func:`detect_layout`
        # exam_no_label: 检查号那一栏在原界面上的叫法（「体检编号」/「检查号」），
        #                仅用于小窗文案，不落库
        "layout", "exam_no_label",
    )

    def __init__(self):
        for k in self.__slots__:
            setattr(self, k, [] if k == "rows" else "")

    def as_dict(self):
        return dict((k, getattr(self, k)) for k in self.__slots__)

    def summary(self):
        """一行摘要，供悬浮窗标题栏显示。"""
        bits = []
        if self.patient_name:
            bits.append(self.patient_name)
        if self.gender or self.age:
            bits.append(format_demo(self.gender, self.age))
        if self.accession_no:
            bits.append("影像号 %s" % self.accession_no)
        elif self.exam_no:
            bits.append("%s %s" % (self.exam_no_label or "检查号", self.exam_no))
        return "  ".join(bits) if bits else "（未识别到患者信息）"


def _set_field(f, name, value):
    """第一个非空值生效；空值不覆盖已有值（见模块 docstring 第 4 条）。"""
    if not value:
        return False
    if getattr(f, name):
        return False
    setattr(f, name, value)
    return True


def _value_ok(name, value):
    """值的合理性检查（空值放行，交给 pending 补值逻辑）。

    体检登记界面顶部有「追加项目(A)」按钮、「项目描述及小结」标签页，
    裸标签正则 ``项目`` 会把 ``(A)`` ``描述及小结`` 当成项目名吃进去 ——
    项目名不可能只有 3 个字，也不会以「描述/编号/名称/列表」开头。
    """
    v = (value or "").strip()
    if not v:
        return True
    if name == "exam_item":
        if len(v) <= 3:
            return False
        if re.match(r"^(描述|编号|名称|列表|小结|模板)", v):
            return False
        return True
    if len(v) <= 3 and not re.search(r"[\u4e00-\u9fff0-9]", v):
        return False
    return True


def extract_fields(lines, image_width=None):
    """从 OCR 行列表提取报告字段。

    Parameters
    ----------
    lines : list
        :class:`reportqc.ocr.ppocr_engine.TextLine` 列表。
    image_width : int, optional
        截图宽度。用于推断正文栏右边界；不给时用各行 x 的最大值近似。

    Returns
    -------
    ReportFields
    """
    f = ReportFields()
    rows = merge_lines(lines)
    f.rows = rows
    if not rows:
        return f

    if image_width is None:
        image_width = max(int(r["x"]) for r in rows) + 1

    f.layout = detect_layout(rows)

    # 检查号那一栏在原界面叫什么 —— 体检登记软件写「体检编号」，
    # 标准 PACS 写「检查号」，小窗照原样显示，医生才对得上。
    for row in rows:
        m = re.search(r"(体检编号|体检号|检查号|登记号|检查序号)",
                      row["text"])
        if m:
            f.exam_no_label = m.group(1)
            break

    # --- 1. 标签：值（一行多对） ---------------------------------------
    #    标签后没值的记下来，稍后向下补值（实测：OCR 会把「检查号：」
    #    和它的值拆成两行）
    pending = []          # [(row_index, field_name)]
    for i, row in enumerate(rows):
        pairs = _split_row_pairs(row["text"])
        if not pairs:
            continue
        for name, value in pairs:
            if name is None:
                continue                 # 伪标签：只用于截断上一个值
            if not _value_ok(name, value):
                continue
            if _set_field(f, name, value):
                continue
            if not value:
                pending.append((i, name))

    # 标签值在下一行：向下找第一个「不是标签、不是停用词」的行
    for i, name in pending:
        if getattr(f, name):
            continue
        for j in range(i + 1, len(rows)):
            if _split_row_pairs(rows[j]["text"]):
                break                    # 遇到下一个标签行，放弃补值
            if _is_ui_stopword(rows[j]["text"]):
                continue
            if _NOISE.match(rows[j]["text"].strip()):
                continue
            _set_field(f, name, rows[j]["text"].strip())
            break

    # --- 2. 正文区切分（按版式分流） ------------------------------------
    if f.layout == LAYOUT_FORM:
        f.findings, f.conclusion = _split_body_form(rows, image_width)
    else:
        f.findings, f.conclusion = _split_body(rows, image_width)
    f.report_text = "\n".join(
        t for t in (f.findings, f.conclusion) if t
    ).strip()
    return f


def _filter_ui_segments(text):
    """把逻辑行按双空格拆成段，丢掉 UI 段（复选框/按钮文字），返回剩余内容。

    真实界面底部是一行 UI 控件（``疾病名称:  报告模板:  阴性  口阳性
    追查  口学习  会诊  临时保存  退出``），整行做停用词匹配匹配不上
    （字符串太长），必须逐段判。实测复现过这行漏进「诊断结果」正文。
    """
    parts = [p.strip() for p in text.split("  ")]
    keep = [p for p in parts if p and not _is_ui_stopword(p)]
    return "  ".join(keep)


def _split_body(rows, image_width):
    """把正文行切成 (影像所见, 诊断结果)。

    策略：先定位两个标题行；标题行的 x 作为正文栏左缘，正文栏右缘取
    「标题 x + 45% 图宽」，把右侧面板文字挡在外面。标题下方、下一标题
    之上的正文行归属前一标题；**标题行自身不进正文**（实测曾把
    「诊断结果」四个字当成所见内容，故显式跳过）。
    """
    idx_find = idx_conc = None
    x_find = 0.0
    for i, row in enumerate(rows):
        t = row["text"].strip()
        if idx_find is None and _SEC_FINDINGS.match(t):
            idx_find, x_find = i, row["x"]
        elif idx_conc is None and _SEC_CONCLUSION.match(t):
            idx_conc = i
            if idx_find is None:
                x_find = row["x"]

    if idx_find is None and idx_conc is None:
        # 用户只框选了正文区（无标题）：把所有非停用词行都当所见
        body = _body_lines(rows, 0.0, image_width, strict=False)
        return "\n".join(body), ""

    x_right = x_find + image_width * 0.45

    findings, conclusion = [], []
    start = (idx_find + 1) if idx_find is not None else (idx_conc + 1)
    for j in range(start, len(rows)):
        if j == idx_find or j == idx_conc:
            continue                       # 标题行自身
        t = _filter_ui_segments(rows[j]["text"])
        if not t or _NOISE.match(t) or _ARTIFACT.search(t):
            continue
        if rows[j]["x"] > x_right:
            continue                       # 右侧面板文字
        if idx_conc is not None and j > idx_conc:
            conclusion.append(t)
        else:
            findings.append(t)
    return "\n".join(findings), "\n".join(conclusion)


def _body_lines(rows, x_left, x_right, strict=True):
    out = []
    for row in rows:
        t = _filter_ui_segments(row["text"])
        if not t or _NOISE.match(t) or _ARTIFACT.search(t):
            continue
        if strict and not (x_left <= row["x"] <= x_right):
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# 版式判定与版式二的正文切分
# ---------------------------------------------------------------------------

LAYOUT_TITLED = "titled"     # 标准 PACS 报告界面：影像所见 / 诊断结果
LAYOUT_FORM = "form"         # 体检登记界面：项目表格 + 检查结果 / 诊断建议

# 只在截图前 20 行里找 —— 体检登记界面的顶部信息区一定有这几个词之一
_FORM_MARKERS = ("项目编号", "体检编号", "体检登记", "项目描述及小结")

# 版式二的列标题（用于反推各列竖线位置）
_FORM_COL_TITLES = ("项目编号", "项目名称", "检查结果", "小结医生", "小结日期")

# 纯编号 / 行号（表格里混进「检查结果」列的脏段）
_INDEX_ONLY = re.compile(r"^[\d\sA-Za-z.#、()（）]{1,12}$")

# 正文里不该出现的美术符号（OCR 把图标/复选框认成的字符）。
# 真实截图底部按钮行被认成「违△收庄必西时廿一止本」这种事，含这类
# 符号的行一律不当正文。
_ARTIFACT = re.compile(r"[△▲※☆★◇◆◎○●□■⊙⊕]")


def _looks_like_table_body(text):
    """判断一段落在「项目名称」列 x 位置的文字其实是「检查结果」正文。

    真实 OCR 行为（2026-10-28 实测截图）：项目名称列与检查结果列之间
    只有约 10px 空白、没有框线，PP-OCR 会把它检测成**一个文本框**，
    x 取的是项目名称列的起点。按列 x 归栏会把这些整段正文丢掉。

    项目名称本身很短（「颈椎正侧位检查」7 字、「X光颈椎正侧位检查」9 字），
    而检查结果至少一整句，所以用长度 + 标点判：14 字以上，或含句读标点。
    """
    t = (text or "").strip()
    if not t:
        return False
    if len(t) >= 14:
        return True
    return bool(re.search(r"[，。；、！？]", t))


def detect_layout(rows):
    """判断截图属于哪种写报告界面。

    Returns
    -------
    str
        ``form``（体检登记界面）或 ``titled``（标准 PACS 报告界面）。

    Notes
    -----
    判定只看**顶部信息区的关键词**，不看正文标题：版式一的正文也可能有
    「检查结果」四个字，反过来版式二从不会有「体检编号」「项目编号」。
    判不出来时默认 ``titled`` —— 那是原本就支持的版式，退回旧行为最安全。
    """
    for row in (rows or [])[:20]:
        t = (row.get("text") if isinstance(row, dict) else "") or ""
        if any(m in t for m in _FORM_MARKERS):
            return LAYOUT_FORM
    return LAYOUT_TITLED


def _split_body_form(rows, image_width):
    """版式二（体检登记界面）的正文切分，返回 ``(所见, 结论)``。

    版面长这样::

        项目编号 │ 项目名称        │ 检查结果            │ 小结医生:  小结日期:
        0806     │ 颈椎正侧位检查                          │ 检查结果:
        08008    │ X光颈椎正侧位检 │ 颈椎生理曲度变直…   │ 1. 颈椎生理曲度变直。
        0813     │ 腰椎正侧位检查                          │ 2. 腰椎平片未见明显异常。
                 │                                         │ 诊断建议:

    **难点是左右两栏处在同一水平线**：``08008 X光… 颈椎生理曲度变直…`` 与
    右侧 ``1. 颈椎生理曲度变直。`` 的 y 只差几像素，:func:`merge_lines`
    必然把它们并成同一行。按整行归栏会一锅端，所以：

    1. 从列标题行反推三条竖线（项目编号 x / 检查结果 x / 右栏 x）；
    2. 按每个**分段自己的 x** 归栏（``row["segs"]``）—— 这是 v1.20.0
       给 :func:`merge_lines` 加 ``segs`` 的唯一原因；
    3. 右栏再按「检查结果 / 诊断建议」两个标题切成所见和结论。

    三块全部质控：表格「检查结果」列 + 右栏「检查结果」+ 右栏「诊断建议」。
    前两块内容常有重复表述，交给 :func:`pipeline._dedupe_issues` 归并成
    「（出现 N 次）」。
    """
    cols = {}
    header_idx = None
    for i, row in enumerate(rows):
        for x, t in row.get("segs") or [(row["x"], row["text"])]:
            tt = t.strip().strip(":：").replace(" ", "")
            for key in _FORM_COL_TITLES:
                if key in cols:
                    continue
                if tt.startswith(key):
                    cols[key] = x
        if "项目编号" in cols and "检查结果" in cols:
            header_idx = i
            break

    if header_idx is None:
        # 用户框得太窄，没框到列标题 → 退回标题型逻辑，尽量多收正文
        return _split_body(rows, image_width)

    tol = 24.0
    result_x = cols.get("检查结果", image_width * 0.26)
    right_x = (cols.get("小结医生") or cols.get("小结日期")
               or (image_width * 0.52))
    if right_x <= result_x:
        right_x = image_width * 0.52          # 列标题认歪了，按图宽估

    table_lines, right_lines = [], []
    for row in rows[header_idx + 1:]:
        for x, t in row.get("segs") or [(row["x"], row["text"])]:
            tt = t.strip()
            if not tt:
                continue
            if x >= right_x - tol:
                right_lines.append(tt)
            elif x >= result_x - tol:
                table_lines.append(tt)
            elif _looks_like_table_body(tt):
                # 项目名称 + 检查结果被 OCR 粘成一个 box，x 落在项目名称列。
                # 项目名只有几个字，长成一句话的只可能是检查结果正文。
                table_lines.append(tt)

    # -- 右栏：以「诊断建议」标题为分界，之前全归所见 ---------------------
    #
    # 不用「检查结果」标题当起点：OCR 常把它和第一行内容判成同一 y，
    # 排序后内容可能排在标题**前面**（标题 x 相同、y 差 2px），按顺序
    # 切会把第一条内容吃掉（v1.20.0 实测少一条）。「诊断建议」是硬分隔，
    # 且它前面不可能有结论内容，所以只认这一个标题更稳。
    findings_r, conclusion_r = [], []
    in_conclusion = False
    for t in right_lines:
        bare = t.strip().strip(":：").replace(" ", "")
        if _SEC_CONCLUSION.match(bare):
            in_conclusion = True
            continue                          # 标题自身不进正文
        if _SEC_FINDINGS.match(bare):
            continue                          # 「检查结果：」标题
        if _is_ui_stopword(t) or _NOISE.match(t) or _ARTIFACT.search(t):
            continue                          # 小结医生 / 小结日期 / 未体检…
        if in_conclusion:
            conclusion_r.append(t)
        else:
            findings_r.append(t)

    # -- 表格「检查结果」列 ---------------------------------------------
    table_clean = []
    for t in table_lines:
        if _NOISE.match(t) or _is_ui_stopword(t) or _ARTIFACT.search(t):
            continue
        if _INDEX_ONLY.match(t):
            continue                          # 项目编号 / 行号误落
        table_clean.append(t)

    findings = table_clean + findings_r
    if not findings and not conclusion_r:
        return _split_body(rows, image_width)  # 三块都空，交给兜底逻辑
    return "\n".join(findings), "\n".join(conclusion_r)
