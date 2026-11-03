# -*- coding: utf-8 -*-
"""纯 Python 极简 PDF 文本 / 患者姓名提取（无第三方依赖，兼容 Win7）。

为什么不用第三方库
------------------
发行包是绿色嵌入 Python，且必须兼容 Win7；当前环境没装 fitz / PyPDF2 /
pdfminer。为了不给发行包新增依赖，这里用标准库手写一个够用的提取器。

覆盖两类最常见的报告 PDF
----------------------
1. **元数据**：Info 字典的 ``Title`` / ``Author`` / ``Subject`` 里直接带患者姓名
   （很多 PACS / 报告系统导出时就会写）。这些字符串是 PDFDocEncoding 或带
   BOM 的 UTF-16BE，可直接解出中文。
2. **正文**：CID 字体 + ``ToUnicode`` CMap 的数字报告。中文在 PDF 里是字体
   专属 CID，必须靠 ToUnicode 才能映射回 Unicode —— 这里解析 ``bfchar`` /
   ``bfrange`` 还原中文。扫描件（位图无文本层）解不出，会回退到原文件名。

设计取舍
------
* 不解析完整 xref：用正则扫描间接对象，足够覆盖未压缩 / FlateDecode 压缩的
  普通报告 PDF。极端结构（对象流 / 交叉引用流）跳过，属可接受降级。
* 只取「姓名」，不做通用全文理解。
"""

import os
import re
import zlib

__all__ = ["extract_text", "extract_patient_name", "extract_image_number",
           "extract_patient_meta", "PDFTextError"]

_UTF16_BOM = b"\xfe\xff"

_HAN = "\u4e00-\u9fa5"


class PDFTextError(Exception):
    """PDF 解析失败。"""


# ---------------------------------------------------------------------------
# 基础：PDF 字符串解码（元数据 / 字面量 / 十六进制）
# ---------------------------------------------------------------------------

def _unescape_literal(raw):
    """解开 PDF 字面量字符串的转义：\\( \\) \\\\ \\n \\r \\t \\b \\f \\ddd。"""
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0x5c:  # '\\'
            if i + 1 >= n:
                out.append(0x5c)
                break
            nxt = raw[i + 1]
            if nxt == 0x6e:       # \n
                out.append(0x0a)
            elif nxt == 0x72:     # \r
                out.append(0x0d)
            elif nxt == 0x74:     # \t
                out.append(0x09)
            elif nxt == 0x62:     # \b
                out.append(0x08)
            elif nxt == 0x66:     # \f
                out.append(0x0c)
            elif nxt == 0x28 or nxt == 0x29 or nxt == 0x5c:
                out.append(nxt)
            elif 0x30 <= nxt <= 0x37:   # 八进制 \ddd
                val = nxt - 0x30
                j = i + 2
                while j < min(i + 4, n) and 0x30 <= raw[j] <= 0x37:
                    val = val * 8 + (raw[j] - 0x30)
                    j += 1
                out.append(val & 0xff)
                i = j - 1
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def _decode_bytes_to_str(data):
    """把 PDF 字符串字节译成文本：UTF-16BE(BOM) > UTF-8 > GB18030 > Latin-1。"""
    if not data:
        return ""
    if data.startswith(_UTF16_BOM):
        try:
            return data[2:].decode("utf-16-be", "ignore")
        except Exception:                       # noqa: BLE001
            pass
    # 纯 ASCII 直接返回（也走下面 utf-8）
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:                       # noqa: BLE001
            continue
    return data.decode("latin-1", "ignore")


def _decode_pdf_string(raw):
    """``raw`` 是去掉了外层括号/尖括号的字节（字面量或十六进制）。"""
    return _decode_bytes_to_str(_unescape_literal(raw))


# ---------------------------------------------------------------------------
# ToUnicode CMap 解析：CID → Unicode
# ---------------------------------------------------------------------------

def _parse_tounicode(stream):
    """解析 ToUnicode 流，返回 ``{cid(int): char(str)}``。

    支持 ``beginbfchar`` / ``endbfchar``（1:1）与 ``beginbfrange`` /
    ``endbfrange``（含 ``<src> <dst> <count>`` 连续段与 ``<src> <dst> [..]`` 列表段）。
    """
    text = stream.decode("latin-1", "ignore")
    mapping = {}
    # bfchar：每行 <cid> <unicode>
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for cid_hex, uni_hex in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                cid = int(cid_hex, 16)
                uni = _hex_to_str(uni_hex)
                if uni:
                    mapping[cid] = uni
            except Exception:                   # noqa: BLE001
                continue
    # bfrange
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for m in re.finditer(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]+)>|\[([^\]]*)\])",
                block):
            lo = int(m.group(1), 16)
            hi = int(m.group(2), 16)
            if m.group(3):                       # 连续段 <lo> <hi> <count>
                dst0 = int(m.group(3), 16)
                for k in range(hi - lo + 1):
                    mapping[lo + k] = _codepoint_to_char(dst0 + k)
            else:                               # 列表段 <lo> <hi> [<u0> <u1> ...]
                targets = re.findall(r"<([0-9A-Fa-f]+)>", m.group(4) or "")
                for k, t in enumerate(targets):
                    if lo + k > hi:
                        break
                    uni = _hex_to_str(t)
                    if uni:
                        mapping[lo + k] = uni
    return mapping


def _hex_to_str(hex_str):
    try:
        data = bytes.fromhex(hex_str)
    except Exception:                           # noqa: BLE001
        return ""
    # 可能是 BOM 开头的 UTF-16BE
    if data.startswith(_UTF16_BOM):
        return data[2:].decode("utf-16-be", "ignore")
    # 4 位 = 1 个 BMP 码点（最常见）
    if len(data) == 2:
        return chr(int(hex_str, 16))
    # 多段（不应出现在 bfchar，但兜底）
    out = []
    for i in range(0, len(data) - 1, 2):
        out.append(chr(int(hex_str[i:i + 4], 16)))
    return "".join(out)


def _codepoint_to_char(cp):
    try:
        return chr(cp)
    except Exception:                           # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 对象扫描
# ---------------------------------------------------------------------------

_OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj(.*?)endobj", re.S)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
# 流起始（结束符不可靠：压缩数据末字节可能就是 0x0D，会被 \r?\nendstream 吃掉）
_STREAM_HEAD_RE = re.compile(rb"stream\r?\n")
# 直接长度；排除间接引用 ``/Length 12 0 R`` 与 ``/Length1``
_LENGTH_RE = re.compile(rb"/Length\s+(\d+)(?!\s+\d+\s+R)")


def _iter_objects(pdf):
    """产出 ``(num, gen, body_bytes, stream_bytes_or_None)``。

    取流**按 ``/Length`` 精确截取**，不用 ``endstream`` 正则 —— 真实 PDF 里
    压缩数据的最后一个字节可能就是 ``0x0D``，紧跟 ``\\nendstream``，用
    ``\\r?\\nendstream`` 会把数据尾字节当换行吃掉，zlib 报 incomplete stream
    （实测 tx_pdf 报告踩过）。
    """
    for m in _OBJ_RE.finditer(pdf):
        num = int(m.group(1))
        gen = int(m.group(2))
        body = m.group(3)
        stream = None
        sm = _STREAM_HEAD_RE.search(body)
        if sm:
            data_start = sm.end()
            lm = _LENGTH_RE.search(body[:sm.start()])   # Length 只在字典段找
            if lm:
                raw = body[data_start:data_start + int(lm.group(1))]
            else:
                raw = body[data_start:]
                em = raw.rfind(b"endstream")
                if em >= 0:
                    raw = raw[:em]
                raw = raw.rstrip(b"\r\n")
            # FlateDecode 解压
            if b"/FlateDecode" in body or b"/Fl" in body:
                try:
                    stream = zlib.decompress(raw)
                except Exception:               # noqa: BLE001
                    stream = None
            else:
                stream = raw
        yield num, gen, body, stream


def _get_stream_by_num(objs, num):
    for n, _g, _body, stream in objs:
        if n == num and stream is not None:
            return stream
    return None


def _resolve_refs(token):
    """从 ``[N G R N G R]`` 或 ``N G R`` 取出对象号列表。"""
    nums = re.findall(rb"(\d+)\s+(\d+)\s+R", token)
    return [int(a) for a, _b in nums]


def _balanced_dict(data, start):
    """``start`` 指向某个 ``<<`` 之后，返回该字典（不含外层 ``<<`` ``>>``）的字节。

    用层数配平处理 Resources 里嵌套 Font 字典的情况（不用 ``>>`` 当结束符，
    否则会提前在 Font 字典的 ``>>`` 处截断）。
    """
    if start < 0 or data[start - 2:start] != b"<<":
        return b""
    depth = 1
    j = start
    n = len(data)
    while j < n and depth > 0:
        if data[j:j + 2] == b"<<":
            depth += 1
            j += 2
        elif data[j:j + 2] == b">>":
            depth -= 1
            j += 2
        else:
            j += 1
    return data[start:j - 2]


def _dict_at(body, idx):
    """``idx`` 指向 ``<<`` 的位置，返回该字典（不含外层 ``<<`` ``>>``）。"""
    if idx < 0 or body[idx:idx + 2] != b"<<":
        return b""
    return _balanced_dict(body, idx + 2)


def _parse_page_fonts(body, bodies_by_num=None):
    """从页面对象体里解析 ``/Resources /Font << /F1 N G R ... >>``。

    真实 PACS 报告（如 tx_pdf）常把 ``/Resources`` 与 ``/Font`` 都写成**间接
    引用**（``/Resources 15 0 R``），字典実体在另一个对象里 —— 因此必须支持
    顺着 ``N G R`` 再解一层，否则拿不到字体映射，正文全空。
    """
    bodies_by_num = bodies_by_num or {}
    dict_bytes = b""

    m = re.search(rb"/Resources\s*<<", body)          # 内联字典
    if m:
        dict_bytes = _dict_at(body, m.end() - 2)
    else:                                             # 间接引用 /Resources N G R
        m = re.search(rb"/Resources\s+(\d+)\s+(\d+)\s+R", body)
        if m:
            res_body = bodies_by_num.get(int(m.group(1)), b"")
            dict_bytes = _dict_at(res_body, res_body.find(b"<<"))
    if not dict_bytes:
        return {}

    fdict = b""
    fm = re.search(rb"/Font\s*<<", dict_bytes)         # 内联 Font 字典
    if fm:
        fdict = _dict_at(dict_bytes, fm.end() - 2)
    else:                                              # 间接引用 /Font N G R
        fm = re.search(rb"/Font\s+(\d+)\s+(\d+)\s+R", dict_bytes)
        if fm:
            fb = bodies_by_num.get(int(fm.group(1)), b"")
            fdict = _dict_at(fb, fb.find(b"<<"))

    fonts = {}
    for fm in re.finditer(rb"/([A-Za-z0-9]+)\s+(\d+)\s+(\d+)\s+R", fdict):
        fonts[fm.group(1).decode("latin-1")] = int(fm.group(2))
    return fonts


# ---------------------------------------------------------------------------
# 正文文本提取（带 ToUnicode）
# ---------------------------------------------------------------------------

def _decode_glyphs(data, touni, width=None):
    """把内容流里的字形字节按 ToUnicode 映射成文本（无解映射则跳过）。

    ``width`` 为字体编码宽度：Type0(CID) 字体 = 2 字节，TrueType/Type1 简单
    字体 = 1 字节。**必须按字体固定宽度**——否则单个 2 字节 CID 会被错误拆成
    两个 1 字节 CID，而拆开后的低位往往也有映射（甚至命中更多），导致正文里
    混进一堆 ``\\x00`` 与错字（真实 tx_pdf 报告即如此）。

    未指定 ``width`` 时才退回「命中数更多」的启发式（兼容外部老调用）。
    """
    if not touni or not data:
        return ""
    if width in (1, 2):
        chars = []
        for i in range(0, len(data), width):
            chunk = data[i:i + width]
            if len(chunk) < width:
                continue
            cid = int.from_bytes(chunk, "big") if width == 2 else chunk[0]
            c = touni.get(cid)
            if c:
                chars.append(c)
        return "".join(chars)

    best = ""
    best_hits = -1
    for w in (2, 1):
        if w == 2 and len(data) % 2 != 0:
            continue
        chars = []
        hits = 0
        for i in range(0, len(data), w):
            chunk = data[i:i + w]
            if len(chunk) < w:
                continue
            cid = int.from_bytes(chunk, "big") if w == 2 else chunk[0]
            c = touni.get(cid)
            if c:
                chars.append(c)
                hits += 1
        if hits > best_hits:
            best = "".join(chars)
            best_hits = hits
    return best


_TJ_STR_RE = re.compile(rb"\((?:\\.|[^\\()])*\)|\[")  # 字面量或数组起点


def _decode_content(content, font_maps):
    """解码一段内容流文本。

    ``font_maps``：字体名 → ``(ToUnicode dict, 字节宽度)``；也兼容直接给
    ``dict``（宽度未知，退回启发式）。
    """
    if not font_maps:
        return ""
    cur = None
    out = []

    def emit(raw):
        if not cur or not raw:
            return
        if isinstance(cur, tuple):
            out.append(_decode_glyphs(raw, cur[0], cur[1]))
        else:
            out.append(_decode_glyphs(raw, cur))

    token_re = re.compile(
        rb"/([A-Za-z0-9]+)\s+(?:[-+]?[\d.]+\s+)?Tf"   # 1:字体名（常规是 /F3 10 Tf）
        rb"|\(((?:\\.|[^\\()])*)\)\s*Tj"              # 2: Tj 字面量
        rb"|\[([^\]]*)\]\s*TJ"                        # 3: TJ 数组
    )
    for m in token_re.finditer(content):
        if m.group(1):                          # Tf
            cur = font_maps.get(m.group(1).decode("latin-1"))
        elif m.group(2) is not None:            # Tj
            emit(_unescape_literal(m.group(2)))
        elif m.group(3) is not None:            # TJ
            seg = m.group(3)
            # 数组中：字面量串参与解码，数字（字距）忽略
            for sm in re.finditer(rb"\(((?:\\.|[^\\()])*)\)", seg):
                emit(_unescape_literal(sm.group(1)))
            for hm in re.finditer(rb"<([0-9A-Fa-f]+)>", seg):
                data = bytes.fromhex(hm.group(1).decode("ascii"))
                emit(data)
    return "".join(out)


def _collect_font_resources(pdf):
    """返回 ``{font_objnum: touni_dict}`` 与 ``{page_objnum: {fontname: font_objnum}}``。"""
    objs = list(_iter_objects(pdf))
    touni_by_font = {}
    font_res_by_page = {}
    streams_by_num = {n: s for n, _g, _b, s in objs if s is not None}

    # 1) 收集 ToUnicode 流 → 字体对象
    for num, _g, body, stream in objs:
        if stream is not None and (b"/ToUnicode" in body):
            # 该对象的 /ToUnicode 指向的流已在自身 stream 里（极少见）
            pass
    # 解析字体对象：/ToUnicode N G R，并记录编码宽度
    font_objs = {}      # font_objnum -> touni dict
    font_width = {}     # font_objnum -> 1 或 2 字节
    for num, _g, body, _s in objs:
        if b"/Subtype" in body and (b"/Type0" in body or b"/TrueType" in body
                                    or b"/Type1" in body):
            tu = re.search(rb"/ToUnicode\s+(\d+)\s+(\d+)\s+R", body)
            if tu:
                tu_num = int(tu.group(1))
                tu_stream = streams_by_num.get(tu_num)
                if tu_stream is not None:
                    try:
                        font_objs[num] = _parse_tounicode(tu_stream)
                        # Type0(CID) 是 2 字节编码，简单字体是 1 字节
                        font_width[num] = 2 if b"/Type0" in body else 1
                    except Exception:           # noqa: BLE001
                        pass

    # 2) 解析页面 /Resources /Font
    bodies_by_num = {n: b for n, _g, b, _s in objs}
    for num, _g, body, _s in objs:
        if re.search(rb"/Type\s*/Page\b", body):
            fonts = _parse_page_fonts(body, bodies_by_num)
            font_res_by_page[num] = fonts

    return font_objs, font_width, font_res_by_page, objs, streams_by_num


def _page_contents(body, streams_by_num):
    """从页面对象里取出内容流字节列表。"""
    contents = []
    c = re.search(rb"/Contents\s+(.*?)(?:/Group|/LastModified|>>|/Resources)",
                  body, re.S)
    if not c:
        return contents
    token = c.group(1).strip()
    # 数组
    arr = re.search(rb"\[(.*)\]", token, re.S)
    if arr:
        for n in _resolve_refs(arr.group(1)):
            s = streams_by_num.get(n)
            if s is not None:
                contents.append(s)
    else:
        nums = _resolve_refs(token)
        if nums:
            for n in nums:
                s = streams_by_num.get(n)
                if s is not None:
                    contents.append(s)
        else:
            # 直接内嵌流（少见）
            sm = _STREAM_RE.search(body)
            if sm:
                contents.append(sm.group(1))
    return contents


def _extract_metadata(pdf):
    """从 Info 字典抓 Title/Author/Subject/Keywords（解码后）。"""
    meta = {}
    for key in ("Title", "Author", "Subject", "Keywords"):
        for m in re.finditer(
                rb"/%s\s*(?:\(((?:\\.|[^\\()])*)\)|<([0-9A-Fa-f]+)>)"
                % key.encode(), pdf):
            if m.group(1) is not None:
                val = _decode_pdf_string(m.group(1))
            else:
                try:
                    val = _decode_bytes_to_str(bytes.fromhex(m.group(2).decode("ascii")))
                except Exception:               # noqa: BLE001
                    val = ""
            if val:
                meta[key.lower()] = val
    return meta


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def extract_text(pdf_path):
    """提取 PDF 全部可读文本（元数据 + 正文）。解不出返回空串。"""
    try:
        with open(pdf_path, "rb") as f:
            pdf = f.read()
    except Exception as exc:                    # noqa: BLE001
        raise PDFTextError("无法读取文件：%s" % exc)
    if not pdf.strip().startswith(b"%PDF"):
        raise PDFTextError("不是 PDF 文件（缺少 %PDF 头）")
    return _extract_text_inner(pdf)


def _extract_text_inner(pdf):
    """``pdf`` 已是字节内容，提取全部可读文本。"""
    font_objs, font_width, font_res_by_page, objs, streams_by_num = \
        _collect_font_resources(pdf)
    parts = []
    for num, _g, body, _s in objs:
        if re.search(rb"/Type\s*/Page\b", body):
            fonts = font_res_by_page.get(num, {})
            font_maps = {}
            for fname, fnum in fonts.items():
                tu = font_objs.get(fnum)
                if tu:
                    font_maps[fname] = (tu, font_width.get(fnum))
            for content in _page_contents(body, streams_by_num):
                txt = _decode_content(content, font_maps)
                if txt:
                    parts.append(txt)
    return "\n".join(parts)


# 明显不是患者姓名的词：系统用户名 / 软件名（PDF 元数据 Author 常是这些）
_NOT_NAME_WORDS = {
    "administrator", "admin", "user", "owner", "guest", "windows", "microsoft",
    "office", "word", "docx", "doc", "pdf", "untitled", "unknown", "pc",
}
# 机构/报告类词：出现在「姓名形态」里就不是患者名
_NOT_NAME_SUBSTR = (
    "医院", "报告", "检查", "科室", "门诊", "住院", "影像", "诊断", "医师",
    "医生", "病人", "患者", "日期", "时间", "设备", "部位", "方法", "意见",
    "表现", "所见", "提示", "申请", "审核", "签名", "打印", "系统", "软件",
)


def _looks_like_name(s):
    """是否为「姓名形态」：2~4 个汉字（可含中间点），或 1~2 个英文词。

    同时排除系统用户名（``Administrator``）与机构词（``医院报告``），否则
    元数据 ``/Author``（Windows 账户名）会被当成患者名。
    """
    if not s:
        return False
    s = s.strip()
    if s.lower() in _NOT_NAME_WORDS:
        return False
    if re.fullmatch(r"[%s]{2,4}(?:·[%s]{1,2})?" % (_HAN, _HAN), s):
        return not any(bad in s for bad in _NOT_NAME_SUBSTR)
    if re.fullmatch(r"[A-Za-z]+(?:\s[A-Za-z]+)?", s) and len(s) <= 30:
        return True
    return False


def _clean_name(s):
    s = (s or "").replace("：", ":").strip()
    # 去掉常见的「姓名:」「患者:」等前缀
    s = re.sub(r"^(姓名|患者姓名|病人姓名|姓名[:：]|患者[:：]|病人[:：]|Name|NAME|name)[:：]?\s*",
               "", s, flags=re.I)
    s = s.strip(" :：\t")
    return s


def extract_patient_name(pdf_path):
    """从 PDF 里取患者姓名；取不到返回 ``None``。

    委托给 :func:`extract_patient_meta`（只取姓名部分）。
    """
    return extract_patient_meta(pdf_path)[0]


def extract_image_number(pdf_path):
    """从 PDF 里取影像号（检查号 / 登记号 / Accession 等）；取不到返回 ``None``。

    委托给 :func:`extract_patient_meta`（只取影像号部分）。
    """
    return extract_patient_meta(pdf_path)[1]


def extract_patient_meta(pdf_path):
    """一次性取出 ``(患者姓名, 影像号)``，只读取一次 PDF。

    两者都取不到时对应元素为 ``None``，调用方应回退到原文件名。
    """
    try:
        with open(pdf_path, "rb") as f:
            pdf = f.read()
    except Exception as exc:                    # noqa: BLE001
        raise PDFTextError("无法读取文件：%s" % exc)
    if not pdf.strip().startswith(b"%PDF"):
        raise PDFTextError("不是 PDF 文件（缺少 %PDF 头）")

    meta = _extract_metadata(pdf)
    text = _extract_text_inner(pdf)

    # 1) 姓名：正文锚定（最可靠）> 元数据像姓名 > 正文首个疑似姓名
    name = _name_from_text(text)
    if not name:
        for key in ("author", "title", "subject", "keywords"):
            v = _clean_name(meta.get(key, ""))
            if _looks_like_name(v):
                name = v
                break
    if not name:
        name = _name_from_filename(pdf_path)

    # 2) 影像号：正文锚定 > 元数据兜底
    img_no = _image_number_from_text(text)
    if not img_no:
        img_no = _image_number_from_meta(meta)

    return name, img_no


# 影像号锚点（中文 + 英文）。顺序即优先级。
_IMG_NO_CN = (
    "影像号", "影像编号", "检查号", "检查编号", "检查UID", "检查ID",
    "登记号", "申请单号", "病历号", "门诊号", "住院号", "病案号",
    "就诊卡号", "影像ID",
)
_IMG_NO_EN = (
    "Accession", "AccessionNumber", "MRN", "PatientID", "StudyID", "ExamNo",
)
# 影像号取值：字母/数字 + 可选 . _ -，长度 3~31，且必须含数字（避免误抓普通词）
_IMG_NO_VAL = r"([A-Za-z0-9][A-Za-z0-9._\-]{2,30})"


def _flex(anchor):
    """把中文锚点译成「允许字间任意空白」的正则（真实报告是字距排开的）。"""
    return r"\s*".join(re.escape(ch) for ch in anchor)


# ---------------------------------------------------------------------------
# 「标签：值」网格切分
# ---------------------------------------------------------------------------
# 真实 PACS 报告（tx_pdf 等）是网格布局：标签与值之间靠字距排开，文本层里
# 表现为「姓    名：邓文奎性    别：男」。直接匹配「姓名」二字会因中间空白
# 失败，且姓名会连上后面的标签首字（邓文奎性）。这里统一按**标签位置**切分，
# 取「本标签之后 ~ 下一标签之前」作为值，再清洗 —— 姓名与影像号用同一套。
_LABELS = (
    ("姓名", r"姓\s*名|患\s*者\s*姓\s*名|病\s*人\s*姓\s*名"),
    ("性别", r"性\s*别"),
    ("年龄", r"年\s*龄"),
    ("影像号", r"影\s*像\s*号(?:码)?|影\s*像\s*编\s*号|影\s*像\s*ID"),
    ("检查号", r"检\s*查\s*(?:号(?:码)?|编\s*号|ID)"),
    ("登记号", r"登\s*记\s*号"),
    ("门诊号", r"门\s*诊\s*号"),
    ("住院号", r"住\s*院\s*号"),
    ("床号", r"床\s*号"),
    ("病案号", r"病\s*案\s*号"),
    ("病历号", r"病\s*历\s*号"),
    ("申请单号", r"申\s*请\s*单\s*号"),
    ("申请科室", r"申\s*请\s*科\s*室"),
    ("申请医生", r"申\s*请\s*医\s*生"),
    ("检查设备", r"检\s*查\s*设\s*备"),
    ("检查项目", r"检\s*查\s*项\s*目"),
    ("检查部位", r"检\s*查\s*部\s*位"),
    ("检查方法", r"检\s*查\s*方\s*法"),
    ("检查时间", r"检\s*查\s*时\s*间"),
    ("报告时间", r"报\s*告\s*时\s*间"),
    ("报告医生", r"报\s*告\s*医\s*生"),
    ("审核医生", r"审\s*核\s*医\s*生"),
    ("科室", r"科\s*室"),
)
_LABEL_RE = tuple((key, re.compile(pat)) for key, pat in _LABELS)


def _label_value_spans(text):
    """返回 ``[(label_key, value)]``：值 = 本标签后 ~ 下一标签前的文本。"""
    if not text:
        return []
    spans = []
    for key, pat in _LABEL_RE:
        for m in pat.finditer(text):
            if m.end() > m.start():
                spans.append((m.start(), m.end(), key))
    # 长标签优先（同起点时）：避免「患者姓名」被「姓名」抢先
    spans.sort(key=lambda it: (it[0], -(it[1] - it[0])))
    kept = []
    last_end = -1
    for s, e, key in spans:
        if s < last_end:            # 与前一个标签重叠，跳过
            continue
        kept.append((s, e, key))
        last_end = e
    out = []
    for i, (s, e, key) in enumerate(kept):
        nxt = kept[i + 1][0] if i + 1 < len(kept) else len(text)
        out.append((key, text[e:nxt].strip(" \t\r\n:：")))
    return out


def _label_map(text):
    """``{label_key: value}``，重复标签保留第一个。"""
    vals = {}
    for key, v in _label_value_spans(text):
        vals.setdefault(key, v)
    return vals


def _image_number_from_text(text):
    if not text:
        return None
    # 1) 标签网格：按优先级取第一个「像影像号」的值
    vals = _label_map(text)
    for key in ("影像号", "检查号", "登记号", "门诊号", "住院号", "床号",
                "病案号", "病历号", "申请单号"):
        v = vals.get(key, "")
        m = re.search(_IMG_NO_VAL, v)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1)
    # 2) 老锚点兜底（允许字间空白）
    for anchor in _IMG_NO_CN:
        m = re.search(_flex(anchor) + r"\s*[:：]?\s*" + _IMG_NO_VAL, text)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1)
    for anchor in _IMG_NO_EN:
        m = re.search(anchor + r"\s*[:：]?\s*" + _IMG_NO_VAL, text, re.I)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1)
    return None


def _image_number_from_meta(meta):
    """元数据（Title/Subject 等）里偶尔直接带影像号，做一次兜底尝试。"""
    for key in ("subject", "title", "keywords", "author"):
        v = meta.get(key, "")
        if not v:
            continue
        m = re.search(r"影像号?\s*[:：]?\s*" + _IMG_NO_VAL, v)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1)
        m = re.search(r"(?:Accession|PatientID|MRN)\s*[:：]?\s*" + _IMG_NO_VAL,
                      v, re.I)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1)
    return None


def _name_from_text(text):
    if not text:
        return None
    # 1) 标签网格（真实报告主路径）：姓名值取到下一个标签前
    v = _label_map(text).get("姓名", "")
    if v:
        m = re.search(r"[%s]{2,4}(?:·[%s]{1,2})?" % (_HAN, _HAN), v)
        if m:
            return m.group(0)
        # 拼音姓名（部分报告用拼音，如 YUE XIAOGANG）
        m = re.match(r"([A-Za-z][A-Za-z .,'\-]{0,29})$", v.strip())
        if m and _looks_like_name(m.group(1).strip()):
            return m.group(1).strip()
    # 2) 锚点兜底（允许字间空白）
    for anchor in ("姓名", "患者姓名", "病人姓名", "患者", "病人"):
        m = re.search(
            _flex(anchor) + r"\s*[:：]?\s*([%s]{2,4}(?:·[%s]{1,2})?)"
            % (_HAN, _HAN), text)
        if m:
            return m.group(1)
    # 3) 英文锚点
    m = re.search(r"(?:Name|Patient)\s*[:：]?\s*([A-Za-z]+(?:\s[A-Za-z]+)?)", text,
                  re.I)
    if m:
        return m.group(1)
    return None


def _name_from_filename(pdf_path):
    """文件名就是患者姓名时直接采用（如「何士进.pdf」「王静.pdf」）。

    扫描件类 PDF（Chrome 打印的整页位图）没有文本层，文件名是唯一可靠的
    姓名来源。仅当文件名是纯 2~4 汉字、且不含机构/报告词时才采用。
    """
    try:
        stem = os.path.splitext(os.path.basename(pdf_path))[0].strip()
    except Exception:                           # noqa: BLE001
        return None
    if re.fullmatch(r"[%s]{2,4}(?:·[%s]{1,2})?" % (_HAN, _HAN), stem):
        if not any(bad in stem for bad in _NOT_NAME_SUBSTR):
            return stem
    return None


def sanitize_filename(name):
    """清洗成合法文件名（去掉 Windows 非法字符，限长）。"""
    if not name:
        return ""
    bad = r'<>:"/\\|?*'
    out = "".join(ch for ch in name if ch not in bad)
    out = out.strip().rstrip(".")
    out = out[:60]
    return out
