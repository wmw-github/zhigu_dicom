# -*- coding: utf-8 -*-
"""web_bridge —— 网页一键导入：外部浏览器（Chrome 扩展）→ 知骨影像

设计约束（改这个文件前请先读完）：

1. Win7 兼容：只用 Python 3.8 语法 + 标准库 + PyQt5。
   发行包 Lib/site-packages 里**没有** requests/urllib3，所以下载一律走
   urllib.request；不要图省事 import requests，否则用户实机 ImportError。
2. 零侵入 main.py：只加两处（bootstrap + bind_window），且全部 try/except 包裹。
   本模块任何异常都必须降级成「没有这个功能」，不能拖垮主程序启动。
3. 控制面 / 数据面分离：
   - 控制面 zhigu:// 自定义协议 —— 只用来**唤醒**软件（命令行 2047 字符上限）
   - 数据面 127.0.0.1 回环 HTTP —— 扩展只推「URL 清单 + 凭据」，不推像素字节
   为什么不让扩展把 dcm 字节 POST 过来？一个 CT 300 张 ≈ 150MB，base64 再膨胀
   33%，浏览器内存和回环 IO 都吃不消，而且软件端还要管临时文件清理。
   现在的做法是扩展只交"取数凭据"，下载由软件自己完成 —— 传输量降到几十 KB。
4. 为什么不能由软件自己去请求云 PACS 的 API：
   鲁云影像（ftimage 架构）的接口响应体是 SM2 密文（"04..." 开头），请求参数
   前端也做了加密，还带 BIZ-REQUEST-ID。外部进程重放请求会拿到 HTTP 605。
   所以清单只能从浏览器页面里取（页面已经解好密了），这是本方案的根本前提。
5. 两类 PACS 共用同一条数据通路，形态不同：
   - 云影像（ftimage）：清单里是**相对路径**，要按 CDN 基址拼；
   - 内网 PACS（WebDicomView + Wado）：**没有清单接口**，扩展从页面的
     viewer 对象里读出序列/图像 id，自己拼
     ``{wado}/GetBlock/{id}?studyuid=..&hospitalid=..&seriesId=..&blockIndex=0&imageType=1``。
     这种部署的取图接口靠**会话 Cookie**鉴权，所以 payload 里有
     ``cookieMap``（主机 → Cookie 串，扩展用 chrome.cookies 才读得到 HttpOnly）
     和 ``referer``（必须是 viewer 那一帧的地址，不是顶层页面）。

对外 API：
    bootstrap(argv) -> bool     # True = 已有实例在处理，调用方应 sys.exit(0)
    bind_window(win)            # 绑定主窗口，之后收到的影像自动打开
    current_port() -> int       # 本地服务端口，调试用
"""

from __future__ import print_function

import json
import os
import ssl
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 常量

APP_ID = "zhigu-ct3d"
PROTOCOL_SCHEME = "zhigu"

# 单实例名与端口允许环境变量覆盖 —— 只为自动化测试/多实例调试留口子，
# 正式使用不设置这些变量，行为与原来完全一致（18923 起 8 个端口）。
# 没有这个隔离，测试进程会把清单 POST 给正在运行的真实软件，
# 结果在用户窗口里弹出临时目录，且断言永远看到空列表。
LOCAL_SERVER_NAME = os.environ.get("ZHIGU_BRIDGE_LOCAL") or "zhigu-ct3d-webimport"

# 扩展会从 18923 开始探测 8 个端口，第一个回 /ping 且 app 匹配的就是我们。
PORT_BASE = int(os.environ.get("ZHIGU_BRIDGE_PORT_BASE") or 18923)
PORT_COUNT = int(os.environ.get("ZHIGU_BRIDGE_PORT_COUNT") or 8)

MAX_BODY = 4 * 1024 * 1024        # 清单 JSON 上限 4MB，正常只有几十 KB
DOWNLOAD_TIMEOUT = 40             # 单张 dcm 超时（秒）
DOWNLOAD_WORKERS = 6              # 并发下载数。医院出口带宽普遍不高，6 路够用
LOG_MAX_BYTES = 512 * 1024

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_HERE, "web_bridge.log")

# 全局状态。Qt 对象在主线程创建，下载线程只碰 _TASKS（带锁）。
_STATE = {
    "hub": None,          # _Hub
    "win": None,          # MainWindow
    "server": None,       # ThreadingHTTPServer
    "local_server": None,  # QLocalServer（必须持有引用，否则被 GC 掉）
    "port": 0,
    "pending": [],        # 主窗口还没绑定时先攒着的结果目录
}
_TASKS = {}
_TASKS_LOCK = threading.Lock()
_TASK_SEQ = [0]


# ---------------------------------------------------------------- 日志

def _log(msg):
    """写 web_bridge.log。日志写失败绝不能影响主流程。"""
    try:
        line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > LOG_MAX_BYTES:
            try:
                os.remove(_LOG_PATH)
            except OSError:
                pass
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:          # noqa: BLE001
        pass
    try:
        print("[web_bridge] " + str(msg))
    except Exception:          # noqa: BLE001
        pass


# ---------------------------------------------------------------- 工具

def _safe_name(text, fallback="series", limit=40):
    """把序列描述转成合法目录名。Windows 非法字符 + 保留名都要挡掉。"""
    text = (text or "").strip()
    for ch in '\\/:*?"<>|\r\n\t':
        text = text.replace(ch, "_")
    text = "".join(c for c in text if ord(c) >= 32)
    text = text.strip(" .")[:limit].strip()
    if not text:
        text = fallback
    upper = text.upper()
    if upper in ("CON", "PRN", "AUX", "NUL") or upper.startswith(("COM1", "LPT1")):
        text = "_" + text
    return text


def _abs_url(url, base):
    """把相对路径补成绝对 URL。ftimage 的 dicom 字段可能是 //host/path 形式。"""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    try:
        return urllib.parse.urljoin(base or "https://localhost/", url)
    except Exception:          # noqa: BLE001
        return ""


def _unverified_ctx():
    """医院内网自签证书很常见，且我们访问的是用户浏览器刚刚访问过的同一资源。
    这里不做证书校验，换取"能装上就能用"。"""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    except Exception:          # noqa: BLE001
        return None


_SSL_CTX = _unverified_ctx()


class _HeaderSet(object):
    """下载用的请求头，可按主机给不同的 Cookie。

    为什么要分主机：内网临床系统把影像放在 8989（Wado）、页面在 11519，
    是两个不同的来源，会话 Cookie 各管各的。只带一份 Cookie 的话，另一端
    永远是「未登录」—— 取图接口会回一页登录 HTML，还常常是 200。

    扩展端会把「主机 → Cookie 串」作为 cookieMap 传下来（它才能读到
    HttpOnly 的那些）；老版本扩展只给一个 cookies 字符串，作为兜底。
    """

    def __init__(self, base, cookie_map=None, fallback_cookie=""):
        self.base = dict(base or {})
        self.map = {}
        for k, v in (cookie_map or {}).items():
            if isinstance(k, str) and isinstance(v, str) and v:
                self.map[k.rstrip("/").lower()] = v
        self.fallback = fallback_cookie or ""

    def _cookie_for(self, url):
        try:
            parts = urllib.parse.urlsplit(url)
        except Exception:                       # noqa: BLE001
            return self.fallback
        origin = "%s://%s" % (parts.scheme, parts.netloc)
        keys = [origin.lower(), parts.netloc.lower(),
                parts.hostname.lower() if parts.hostname else ""]
        for key in keys:
            if key and key in self.map:
                return self.map[key]
        return self.fallback

    def for_url(self, url):
        h = dict(self.base)
        ck = self._cookie_for(url)
        if ck:
            h["Cookie"] = ck
        return h


def _http_get(url, headers, timeout=DOWNLOAD_TIMEOUT):
    # headers 既可以是普通 dict，也可以是 _HeaderSet（按主机换 Cookie）
    hdrs = headers.for_url(url) if isinstance(headers, _HeaderSet) else headers
    req = urllib.request.Request(url, headers=hdrs)
    resp = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
    try:
        data = resp.read()
        code = getattr(resp, "status", None) or resp.getcode()
    finally:
        try:
            resp.close()
        except Exception:      # noqa: BLE001
            pass
    return code, data


# ---------------------------------------------------------------- 下载

def _new_task(total):
    with _TASKS_LOCK:
        _TASK_SEQ[0] += 1
        tid = "%d-%d" % (int(time.time()), _TASK_SEQ[0])
        task = {"id": tid, "state": "running", "total": total, "done": 0,
                "failed": 0, "dir": "", "message": "正在下载影像…"}
        _TASKS[tid] = task
    return task


def _task_update(task, **kw):
    with _TASKS_LOCK:
        task.update(kw)


def _sniff(code, data):
    """按内容判断响应到底是什么。返回 ``"dicom"`` / ``"jpeg"`` / ``""``（不是影像）。

    为什么必须看内容而不是看扩展名/字段名：ftimage 系的 CDN 路径
    （``/v3/sdly/<院区>/.../<rawKey>``）**没有扩展名**，同一个地址今天可能给
    原始 DICOM、明天给 JPEG 预览；而页面上 ``imageResult`` 里也常常只有 ``jpeg``
    一个字段。只有魔数能说实话。

    常见陷阱是「假 200」：链接过期后被重定向到 HTML 登录页，状态码还是 200。
    """
    if code and int(code) >= 400:
        return ""
    if not data or len(data) < 64:
        return ""
    head = data[:256].lstrip()
    if head[:1] == b"<" or b"<html" in head[:64].lower():
        return ""
    if len(data) >= 132 and data[128:132] == b"DICM":
        return "dicom"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    # 裸数据集（无 128 字节导言）：首组号是小端 0002/0008/0010 之类
    if len(data) >= 256 and data[0:2] in (b"\x02\x00", b"\x08\x00", b"\x10\x00"):
        return "dicom"
    return ""


def _looks_like_dicom(code, data):
    """兼容旧调用点：只关心「是不是能用的影像」。"""
    return _sniff(code, data) != ""


# ------------------------------------------------------- 压缩 DICOM 解压

def _decoder_ready():
    """当前环境有没有能解 JPEG-LS / JPEG2000 的解码器（GDCM）。

    云端 PACS 下发的往往不是裸像素。鲁云（ftimage 架构）给 CT 的就是
    JPEG-LS 无损（``1.2.840.10008.1.2.4.80``），单张 200KB 看着像 DICOM，
    魔数、行列数、窗值全都对，但像素是压缩流。VTK 自带的
    vtkDICOMImageReader 只认未压缩 + JPEG baseline，遇到 JPEG-LS 直接
    ``Unable to get PixelData``，**几何尺寸对、像素全空** —— 界面上就是
    「序列能打开，但每一张都是白板」。pydicom 也要靠 GDCM 才能解这类像素。
    """
    try:
        import gdcm          # noqa: F401
        return True
    except Exception:        # noqa: BLE001
        return False


def _pixel_bytes(arr, ds):
    """把解码出的数组转成 PixelData 需要的裸字节；尺寸对不上返回 None。"""
    try:
        import numpy as np
        bits = int(getattr(ds, "BitsAllocated", 16) or 16)
        width = max(1, bits // 8)
        dtype = {1: np.uint8, 2: np.uint16, 4: np.uint32}.get(width)
        if dtype is None:
            return None
        a = np.asarray(arr)
        if a.dtype.itemsize == width:
            if a.dtype != dtype:
                a = a.view(dtype)       # 纯位重解释：int16 的负值不能做数值转换
        else:
            a = a.astype(dtype)
        raw = a.tobytes()
        expected = (int(getattr(ds, "Rows", 0) or 0)
                    * int(getattr(ds, "Columns", 0) or 0)
                    * int(getattr(ds, "SamplesPerPixel", 1) or 1)
                    * int(getattr(ds, "NumberOfFrames", 1) or 1)
                    * width)
        if expected and len(raw) != expected:
            return None
        if len(raw) % 2:
            raw += b"\x00"              # DICOM 元素长度必须是偶数
        return raw
    except Exception:                   # noqa: BLE001
        return None


def _decompress_one(path):
    """把压缩传输语法的 DICOM **就地**改写为未压缩小端，返回 (ok, why)。

    为什么改写文件而不是只在上层解压：软件里 CT 走 vtkDICOMImageReader，
    三维重建、测量、AI 检测都吃 VTK 的体数据；只要磁盘上是未压缩的，
    整条下游链路一行都不用改。代价是体积涨 3~4 倍（512x512 CT 约 0.5MB/张，
    一个 419 张的检查 ≈ 210MB），换来的是「能看、能测、能重建」。

    未压缩的文件原样返回 (True, "")，绝不重写 —— 云端有时给的就是裸像素，
    多写一遍纯属把用户的磁盘当耗材。
    """
    tmp = None
    try:
        import pydicom
        from pydicom.uid import ExplicitVRLittleEndian
    except Exception:                   # noqa: BLE001
        return True, ""                 # 没有 pydicom 就别动文件，交给下游
    try:
        # 先只读头判断传输语法：未压缩的文件占绝大多数，别为它们白读一遍像素
        head = pydicom.dcmread(path, stop_before_pixels=True)
        ts = getattr(getattr(head, "file_meta", None), "TransferSyntaxUID", None)
        if ts is None or not bool(getattr(ts, "is_compressed", False)):
            return True, ""
        ds = pydicom.dcmread(path)
        try:
            arr = ds.pixel_array
        except Exception as exc:        # noqa: BLE001
            return False, "解不开压缩像素（%s）" % (str(exc).split("\n")[0][:120],)
        raw = _pixel_bytes(arr, ds)
        if raw is None:
            return False, "像素长度与 %sx%s/%sbit 不匹配" % (
                getattr(ds, "Rows", "?"), getattr(ds, "Columns", "?"),
                getattr(ds, "BitsAllocated", "?"))
        ds.PixelData = raw
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        try:
            ds["PixelData"].is_undefined_length = False
        except Exception:               # noqa: BLE001
            pass
        # 压缩标记要清掉：留着会让下游（含 AI 预处理）以为还要再解一次
        for kw in ("LossyImageCompression", "LossyImageCompressionRatio",
                   "LossyImageCompressionMethod"):
            if kw in ds:
                try:
                    del ds[kw]
                except Exception:       # noqa: BLE001
                    pass
        tmp = path + ".part"
        ds.save_as(tmp, write_like_original=False)
        os.replace(tmp, path)
        tmp = None
        return True, ""
    except Exception as exc:            # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:120])
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _probe_prefixes(jobs, headers):
    """每个序列目录用第一张探一次，把能通的候选换到首位。

    云 PACS 的 dicom 字段多是相对路径，前缀有 3 种拼法（<origin>/、
    <origin>/dimage/、相对当前目录）。逐张回退的话，300 张会白跑 600 次请求，
    还夹着 0.6s 的重试等待；先探一张就能定下整批用哪种前缀。
    """
    first_of = {}
    for i, job in enumerate(jobs):
        d = os.path.dirname(job[1])
        if d not in first_of:
            first_of[d] = i

    for folder, idx in first_of.items():
        cands = jobs[idx][0]
        if len(cands) < 2:
            continue
        good = -1
        for k, u in enumerate(cands):
            try:
                code, data = _http_get(u, headers)
            except Exception:             # noqa: BLE001
                continue
            if _looks_like_dicom(code, data):
                good = k
                break
        if good <= 0:
            continue                      # 首位本来就对，或全都不通（留给逐张回退）
        for job in jobs:
            if os.path.dirname(job[1]) != folder:
                continue
            if len(job[0]) > good:
                job[0].insert(0, job[0].pop(good))
        _log("序列 %s 探测到第 %d 个候选可用：%s"
             % (os.path.basename(folder), good + 1, cands[0][:120]))


def _download_one(job, headers):
    """下载单张影像。job = (候选 URL 列表, 目标路径)，按顺序回退。

    返回 ``(ok, 说明, 实际路径)``。实际路径可能和传入的不一样 —— 扩展按
    ``.dcm`` 命名，但拿回来可能是 JPEG，这时要改成 ``.jpg``，否则 VTK/pydicom
    都会读失败。

    为什么要多候选：云 PACS 给的常是相对路径，到底是 ``<origin>/<path>`` 还是
    ``<CDN 根>/<path>`` 取决于部署方式，扩展端几种都给了，这里按顺序试。
    """
    urls, dst = job
    last = ""
    fatal = False
    for url in urls:
        for attempt in (1, 2):
            try:
                code, data = _http_get(url, headers)
                kind = _sniff(code, data)
                if not kind:
                    raw = data or b""
                    head = raw[:256].lstrip()
                    if code and int(code) >= 400:
                        last = "HTTP %s" % code
                    elif head[:1] == b"<" or b"<html" in head[:64].lower():
                        last = "返回了 HTML（可能是登录页或链接过期）"
                    elif len(raw) < 128:
                        last = "响应过短：%s" % raw[:40].decode("utf-8", "replace")
                    else:
                        last = "不是影像（首字节 %s）" % raw[:8].hex()
                    # 内容不对 = 这个地址给的不是影像，重试没有意义。
                    # 标成确定性失败是为了让外层能熔断：ftimage 的 CDN 对失效
                    # key 会返回 200 + "no resource found"，站点域名则会返回
                    # 200 + 一整页 HTML —— 两种都是「假 200」，419 张全撞上时
                    # 不熔断会让用户盯着 0/419 等十几分钟。
                    fatal = True
                    break
                base, ext = os.path.splitext(dst)
                want = ".dcm" if kind == "dicom" else ".jpg"
                final = dst if ext.lower() == want else base + want
                tmp = final + ".part"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                if os.path.exists(final):
                    os.remove(final)
                os.replace(tmp, final)
                return True, "", final, False
            except urllib.error.HTTPError as exc:
                last = "HTTP %s" % exc.code
                # 4xx 是确定性失败（地址不对 / 链接过期），重试纯属浪费时间。
                # 419 张 × 4 个候选 × 2 次重试 + 0.6s 等待 = 用户得干等十几分钟，
                # 界面上就是「0/419 一直不动」。这里必须立刻换下一个候选。
                if 400 <= int(getattr(exc, "code", 0) or 0) < 500:
                    fatal = True
                    break
                if attempt == 1:
                    time.sleep(0.5)
            except Exception as exc:      # noqa: BLE001
                last = "%s: %s" % (type(exc).__name__, exc)
                if attempt == 1:
                    time.sleep(0.5)
    return False, last, None, fatal


def _prune_old_dirs(keep=3, prefix="zhigu_web_"):
    """清掉本项目以前下载的临时目录，只留最近 keep 个。

    为什么需要：云端 CT 多为 JPEG-LS 压缩，解压后体积涨 4 倍（512x512 每张
    0.5MB），一个 419 张的检查就是 210MB。而每次点按钮都会 mkdtemp 建新目录，
    不清理的话点几次就是几个 GB，用户磁盘很快就见底。

    安全性：只动 ``%TEMP%`` 下**自己创建**、前缀严格匹配的目录；保留最近
    3 个，避免把软件当前正打开着的那一个删掉（删了表现为「图突然消失」）。
    任何异常一律吞掉 —— 清理失败远没有导入失败严重。
    """
    try:
        import shutil
        base = tempfile.gettempdir()
        cands = []
        for name in os.listdir(base):
            if not name.startswith(prefix):
                continue
            path = os.path.join(base, name)
            try:
                if os.path.isdir(path):
                    cands.append((os.path.getmtime(path), path))
            except OSError:
                continue
        if len(cands) <= keep:
            return
        cands.sort(reverse=True)
        for _, path in cands[keep:]:
            shutil.rmtree(path, ignore_errors=True)
    except Exception:                   # noqa: BLE001
        pass


def _collect_jobs(payload):
    """把 payload 摊平成 [(url, 目标路径)]，顺便统计总数。"""
    page_url = payload.get("pageUrl") or ""
    base = payload.get("baseUrl") or page_url
    _prune_old_dirs()                   # 先清旧的，再建新的，别把自己清掉
    root = tempfile.mkdtemp(prefix="zhigu_web_")
    jobs = []
    series_list = payload.get("series") or []
    for si, series in enumerate(series_list, 1):
        images = series.get("images") or []
        if not images:
            continue
        desc = series.get("desc") or series.get("seriesDesc") or ""
        folder = "%02d_%s" % (si, _safe_name(desc or series.get("modality") or "series"))
        out_dir = os.path.join(root, folder)
        try:
            os.makedirs(out_dir)
        except OSError:
            pass
        for ii, image in enumerate(images):
            cands = []
            name = ""
            if isinstance(image, dict):
                for raw in [image.get("url")] + list(image.get("alt") or []):
                    u = _abs_url(raw, base)
                    if u and u not in cands:
                        cands.append(u)
                name = image.get("name") or ""
            else:
                u = _abs_url(image, base)
                if u:
                    cands.append(u)
            if not cands:
                continue
            name = _safe_name(os.path.basename(str(name)), "", 60)
            if not name.lower().endswith(".dcm"):
                name = ("%04d" % (ii + 1)) + ("_" + name if name else "") + ".dcm"
            jobs.append((cands, os.path.join(out_dir, name)))
    return root, jobs


def _download_study(payload, task):
    base_headers = {
        "User-Agent": payload.get("userAgent") or DEFAULT_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    # Referer 优先用「采集到清单的那一帧」的地址：内网临床系统里按钮在顶层，
    # 影像在 8989 的 iframe 里，拿顶层地址当 Referer 会让 GetBlock 的防盗链 403。
    referer = payload.get("referer") or payload.get("pageUrl") or ""
    if referer:
        base_headers["Referer"] = referer
    headers = _HeaderSet(base_headers, payload.get("cookieMap"),
                         payload.get("cookies") or "")
    _log("下载开始：series=%d 分主机Cookie=%d 兜底Cookie=%s referer=%s"
         % (len(payload.get("series") or []), len(headers.map),
            "有" if headers.fallback else "无", referer[:110]))

    root, jobs = _collect_jobs(payload)
    total = len(jobs)
    _task_update(task, total=total)
    if not jobs:
        _task_update(task, state="error", message="没有解析到任何影像地址")
        _log("payload 里没有可用影像地址，series=%d" % len(payload.get("series") or []))
        return

    # 先定前缀，再并发。否则每张图都要走一遍候选回退，300 张白跑 600 次请求。
    try:
        _probe_prefixes(jobs, headers)
    except Exception:                     # noqa: BLE001
        _log("前缀探测失败（不影响后续按候选回退）：\n" + traceback.format_exc())

    lock = threading.Lock()
    results = []
    kinds = {"dicom": 0, "jpeg": 0, "undec": 0}
    # 连续「确定性失败」（404/403）到一定数量就说明整批 URL 前缀错了，
    # 继续跑只是让用户在「0/419」上白等。这里主动熔断并给出可操作的提示。
    abort = {"on": False, "consec": 0}
    ABORT_AFTER = 12

    def worker(chunk):
        for job in chunk:
            if abort["on"]:
                with lock:
                    task["done"] += 1
                    task["failed"] += 1
                continue
            ok, why, path, fatal = _download_one(job, headers)
            is_dcm = bool(path) and path.lower().endswith(".dcm")

            # ① 先结算「下载成败」和「已经落下过图」。这一步必须抢在解压之前：
            #    解压要 20ms/张，6 路并发时这点延迟足够让后面十几张 404 抢先
            #    把熔断计数器打满，于是「前 5 张成功、其余失效」这种正常场景
            #    会被误判成「基址全错」整批中止（回归测试实测复现过）。
            with lock:
                results.append((ok, why))
                if not ok:
                    task["failed"] += 1
                    if fatal:
                        abort["consec"] += 1
                        # 只在「一张都没成功」时熔断。已经落下过图说明基址
                        # 是对的，这时个别序列 403/404（权限不同、个别 key
                        # 过期）不该把剩下的几百张一起判死刑。
                        if (abort["consec"] >= ABORT_AFTER and not abort["on"]
                                and (kinds["dicom"] + kinds["jpeg"]) == 0):
                            abort["on"] = True
                    else:
                        abort["consec"] = 0    # 网络抖动不算，别误熔断
                else:
                    abort["consec"] = 0
                    if is_dcm:
                        kinds["dicom"] += 1
                    else:
                        kinds["jpeg"] += 1

            # ② 落盘后立刻解压。放在 worker 里而不是全部下完再统一处理：
            #    进度条读的就是 task["done"]，同一张「下载+解压」都完成才算
            #    真到位，否则会出现「419/419 已满但图还是白板」的中间态。
            #    一张解不开只影响它自己，不会让整批在收尾阶段一起失败。
            if ok and is_dcm:
                dok, dec_why = _decompress_one(path)
                if not dok:
                    with lock:
                        kinds["undec"] += 1
                    _log("解压失败 %s -> %s" % (os.path.basename(path), dec_why))

            with lock:
                task["done"] += 1

            if not ok:
                _log("下载失败 %s -> %s" % ((job[0] or [""])[0][:160], why))

    chunks = [[] for _ in range(min(DOWNLOAD_WORKERS, total))]
    for i, job in enumerate(jobs):
        chunks[i % len(chunks)].append(job)
    threads = [threading.Thread(target=worker, args=(c,)) for c in chunks if c]
    for t in threads:
        t.daemon = True
        t.start()
    for t in threads:
        t.join()

    ok_count = sum(1 for ok, _ in results if ok)
    if ok_count == 0:
        why = next((w for ok, w in results if not ok), "未知原因")
        if abort["on"]:
            msg = ("影像地址全部无法访问（%s）。\n"
                   "两种情况最常见：① 云影像：地址前缀和实际 CDN 不一致——"
                   "把页面打开到看得到图像的序列、翻一遍图再点按钮；"
                   "② 内网 PACS：取图接口需要登录态——确认浏览器里影像能正常显示，"
                   "并检查扩展是否有 cookies 权限（浏览器提示重新授权时要同意）。"
                   % why)
        else:
            msg = "全部下载失败：%s" % why
        _task_update(task, state="error", message=msg)
        _log("全部下载失败，首个原因：%s（熔断=%s）" % (why, abort["on"]))
        return

    # 全是 JPEG 的部署（ftimage 移动版常见）要明确说出来：不是导入失败，
    # 是这个入口压根没下发原始 DICOM。含混地报"成功"会让人以为能测量。
    if kinds["dicom"]:
        msg = "已导入 %d/%d 张" % (ok_count, total)
        if kinds["undec"]:
            # 这些文件不报错也不消失，只是在阅读器里是白板 —— 必须点名，
            # 否则用户只会说「打开后都是空白图」，谁也想不到是解码器的事。
            msg += "（%d 张压缩影像无法解压，会显示空白）" % kinds["undec"]
    else:
        msg = "已导入 %d 张（仅 JPEG 预览，该入口未提供原始 DICOM）" % ok_count
    _task_update(task, state="done", dir=root, message=msg)
    _log("下载完成 %d/%d（dicom=%d jpeg=%d 未解压=%d 解码器=%s）-> %s"
         % (ok_count, total, kinds["dicom"], kinds["jpeg"], kinds["undec"],
            "GDCM" if _decoder_ready() else "无", root))
    _notify_window(root)


# ---------------------------------------------------------------- Qt 桥

def _ensure_qt():
    global QtCore, QtNetwork, QtWidgets
    from PyQt5 import QtCore, QtNetwork, QtWidgets   # noqa: F401


def _make_hub_class():
    _ensure_qt()

    class HubImpl(QtCore.QObject):
        # payload, source, task_id —— task_id 必须复用 HTTP 端建的那个，
        # 否则扩展按 taskId 轮询永远查不到进度（会一直停在 0/total）。
        payload_received = QtCore.pyqtSignal(dict, str, str)
        result_ready = QtCore.pyqtSignal(str)             # 目录

    return HubImpl


_HubImpl = None


def _hub():
    global _HubImpl
    if _HubImpl is None:
        _HubImpl = _make_hub_class()
    if _STATE["hub"] is None:
        hub = _HubImpl()
        hub.payload_received.connect(_on_payload, QtCore.Qt.QueuedConnection)
        hub.result_ready.connect(_on_result, QtCore.Qt.QueuedConnection)
        _STATE["hub"] = hub
    return _STATE["hub"]


def _emit_payload(payload, source, task_id):
    try:
        _hub().payload_received.emit(payload, source, task_id)
    except Exception:          # noqa: BLE001
        _log("emit payload 失败：\n" + traceback.format_exc())


def _notify_window(path):
    try:
        _hub().result_ready.emit(path)
    except Exception:          # noqa: BLE001
        _log("emit result 失败：\n" + traceback.format_exc())


def _on_payload(payload, source, task_id):
    """主线程：拿到扩展推来的清单，开线程下载。"""
    try:
        viewport = payload.get("pageTitle") or payload.get("pageUrl") or ""
        _log("收到清单 source=%s 页面=%s series=%d"
             % (source, viewport[:120], len(payload.get("series") or [])))
        with _TASKS_LOCK:
            task = _TASKS.get(task_id)
        if task is None:                 # 协议唤醒等没有 taskId 的入口
            task = _new_task(0)
        t = threading.Thread(target=_download_study_safe, args=(payload, task))
        t.daemon = True
        t.start()
    except Exception:          # noqa: BLE001
        _log("处理清单失败：\n" + traceback.format_exc())


def _download_study_safe(payload, task):
    try:
        _download_study(payload, task)
    except Exception:          # noqa: BLE001
        _log("下载线程异常：\n" + traceback.format_exc())
        _task_update(task, state="error", message="下载线程异常，详见 web_bridge.log")


def _on_result(path):
    """主线程：把下载好的目录交给主窗口打开。"""
    win = _STATE["win"]
    if win is None:
        _STATE["pending"].append(path)
        _log("主窗口未就绪，暂存待打开目录：%s" % path)
        return
    _open_in_window(win, path)


def _open_in_window(win, path):
    try:
        opener = getattr(win, "_open_dir", None)
        ok = False
        if callable(opener):
            ok = bool(opener(path))
        if not ok:
            opener2 = getattr(win, "_open_path", None)
            if callable(opener2):
                ok = bool(opener2(path))
        try:
            win.raise_()
            win.activateWindow()
        except Exception:      # noqa: BLE001
            pass
        _log("打开目录%s：%s" % ("成功" if ok else "失败", path))
    except Exception:          # noqa: BLE001
        _log("打开目录异常：\n" + traceback.format_exc())


# ---------------------------------------------------------------- 本地 HTTP

def _cors_headers(handler, allow_origin):
    handler.send_header("Access-Control-Allow-Origin", allow_origin)
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers",
                        "Content-Type, X-Zhigu-Bridge")
    # Chrome 的 Private Network Access：https 页面访问 127.0.0.1 需要这个。
    handler.send_header("Access-Control-Allow-Private-Network", "true")
    handler.send_header("Access-Control-Max-Age", "600")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZhiguBridge/1.0"

    def _trace(self, tag=""):
        """记录每个外部请求（/task 轮询除外，否则每秒刷屏）。

        排查「扩展唤起了软件但没送数据」时，这行日志是唯一能回答
        「请求到底有没有到」的证据 —— 没有它就只能靠猜。
        """
        try:
            if urllib.parse.urlparse(self.path).path == "/task":
                return
            origin = self.headers.get("Origin") or "(无)"
            ua = (self.headers.get("User-Agent") or "")[:36] or "(无)"
            _log("HTTP %s %s%s origin=%s ua=%s"
                 % (self.command, self.path[:90], (" " + tag) if tag else "", origin, ua))
        except Exception:          # noqa: BLE001
            pass

    # 静音默认的 stderr 访问日志
    def log_message(self, fmt, *args):
        pass

    # ---- 安全：只认扩展来源
    def _origin_ok(self, allow_bare=False):
        """allow_bare：允许既无 Origin 又无自定义头的请求（仅用于 /ping 探活）。

        /import 必须走 chrome-extension:// 来源或带 X-Zhigu-Bridge 头：
        普通网页想跨域加自定义头会先发预检，而预检里 Origin 不是
        chrome-extension:// 会被 403，所以恶意页面无法「静默拉图」。
        """
        origin = self.headers.get("Origin") or ""
        if origin.startswith("chrome-extension://"):
            return True, origin
        if not origin and (allow_bare or self.headers.get("X-Zhigu-Bridge")):
            return True, "*"
        return False, origin or "*"

    def _reply(self, code, obj, origin="*"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        _cors_headers(self, origin)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:      # noqa: BLE001
            pass

    def do_OPTIONS(self):
        self._trace("<预检>")
        ok, origin = self._origin_ok()
        self.send_response(204 if ok else 403)
        self.send_header("Content-Length", "0")
        _cors_headers(self, origin)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        self._trace()
        ok, origin = self._origin_ok(allow_bare=(parsed.path == "/ping"))
        if not ok:
            self._reply(403, {"ok": False, "error": "origin not allowed"}, origin)
            return
        if parsed.path == "/ping":
            self._reply(200, {"ok": True, "app": APP_ID, "port": _STATE["port"],
                              "bound": _STATE["win"] is not None}, origin)
            return
        if parsed.path == "/task":
            qs = urllib.parse.parse_qs(parsed.query)
            tid = (qs.get("id") or [""])[0]
            with _TASKS_LOCK:
                task = dict(_TASKS.get(tid) or {})
            if not task:
                self._reply(404, {"ok": False, "error": "no such task"}, origin)
                return
            self._reply(200, {"ok": True, "task": task}, origin)
            return
        self._reply(404, {"ok": False, "error": "not found"}, origin)

    def do_POST(self):
        self._trace("<-清单")
        ok, origin = self._origin_ok()
        if not ok:
            self._reply(403, {"ok": False, "error": "origin not allowed"}, origin)
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/import":
            self._reply(404, {"ok": False, "error": "not found"}, origin)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._reply(400, {"ok": False, "error": "empty body"}, origin)
            return
        if length > MAX_BODY:
            self._reply(413, {"ok": False, "error": "body too large"}, origin)
            return
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload 不是对象")
        except Exception as exc:      # noqa: BLE001
            self._reply(400, {"ok": False, "error": "bad json: %s" % exc}, origin)
            return

        series = payload.get("series") or []
        total_images = sum(len(s.get("images") or []) for s in series
                           if isinstance(s, dict))
        if total_images == 0:
            self._reply(400, {"ok": False, "error": "清单里没有影像"}, origin)
            return

        task = _new_task(total_images)
        _emit_payload(payload, origin, task["id"])
        self._reply(200, {"ok": True, "taskId": task["id"],
                          "series": len(series), "images": total_images}, origin)


def _start_http():
    last_err = None
    for offset in range(PORT_COUNT):
        port = PORT_BASE + offset
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        except OSError as exc:
            last_err = exc
            continue
        server.daemon_threads = True
        _STATE["server"] = server
        _STATE["port"] = port
        t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5})
        t.daemon = True
        t.start()
        _log("本地服务已启动 http://127.0.0.1:%d" % port)
        return port
    _log("本地服务启动失败（端口 %d-%d 全部占用）：%s"
         % (PORT_BASE, PORT_BASE + PORT_COUNT - 1, last_err))
    return 0


# ---------------------------------------------------------------- 协议 / 单实例

def parse_protocol_arg(arg):
    """zhigu://import?u=<url> -> {'pageUrl': <url>}；不是我们的协议返回 None。"""
    if not arg:
        return None
    low = arg.strip().strip('"')
    if not low.lower().startswith(PROTOCOL_SCHEME + "://"):
        return None
    try:
        parsed = urllib.parse.urlparse(low)
        qs = urllib.parse.parse_qs(parsed.query)
        page = (qs.get("u") or qs.get("url") or [""])[0]
        return {"pageUrl": page, "action": parsed.netloc or "open"}
    except Exception:          # noqa: BLE001
        return {"pageUrl": "", "action": "open"}


def _find_protocol_payload(argv):
    for arg in argv[1:]:
        payload = parse_protocol_arg(arg)
        if payload:
            return payload
    return None


def _enabled():
    """app_settings.json 里 web_import_enabled=false 可整体关掉本功能
    （医院信息安全检查要求关闭本地监听时用）。缺字段视为开启。"""
    try:
        path = os.path.join(_HERE, "app_settings.json")
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        return bool(cfg.get("web_import_enabled", True))
    except Exception:          # noqa: BLE001
        return True


def bootstrap(argv):
    """在 QApplication 创建之后、MainWindow 之前调用。

    返回 True 表示"已有实例接管了本次唤醒"，调用方应立刻 sys.exit(0)。
    返回 False 表示本进程是主实例（或桥接不可用），继续正常启动。
    """
    try:
        if not _enabled():
            _log("app_settings.json 里 web_import_enabled=false，跳过网页导入功能")
            return False
        _ensure_qt()
        _hub()          # hub 必须在主线程建好：下载线程只 emit，不建 QObject
        payload = _find_protocol_payload(argv) or {}

        # 1) 试连已有实例：连上就把 payload 转过去，自己退出。
        sock = QtNetwork.QLocalSocket()
        sock.connectToServer(LOCAL_SERVER_NAME)
        if sock.waitForConnected(300):
            msg = json.dumps({"payload": payload}, ensure_ascii=False).encode("utf-8")
            sock.write(msg)
            sock.flush()
            sock.waitForBytesWritten(500)
            sock.disconnectFromServer()
            _log("已有实例在运行，已转发唤醒参数并退出")
            return True

        # 2) 成为主实例
        server = QtNetwork.QLocalServer()
        server.setSocketOptions(QtNetwork.QLocalServer.UserAccessOption)
        QtNetwork.QLocalServer.removeServer(LOCAL_SERVER_NAME)
        if not server.listen(LOCAL_SERVER_NAME):
            # 监听失败不影响 HTTP 通道，只是丢掉了"二次唤醒"能力
            _log("QLocalServer.listen 失败：%s" % server.errorString())
        else:
            server.newConnection.connect(_on_local_connection)
        _STATE["local_server"] = server

        _start_http()
        if payload.get("pageUrl"):
            _log("协议唤醒，来源页面：%s" % payload["pageUrl"])
            # 这里不直接下载：扩展随后会 POST 清单过来。
        return False
    except Exception:          # noqa: BLE001
        _log("bootstrap 失败（本功能降级为不可用）：\n" + traceback.format_exc())
        return False


def _on_local_connection():
    server = _STATE.get("local_server")
    if server is None:
        return
    while server.hasPendingConnections():
        conn = server.nextPendingConnection()
        conn.waitForReadyRead(500)
        try:
            raw = bytes(conn.readAll()).decode("utf-8", "replace")
            msg = json.loads(raw) if raw.strip() else {}
        except Exception:      # noqa: BLE001
            msg = {}
        try:
            conn.disconnectFromServer()
        except Exception:      # noqa: BLE001
            pass
        payload = (msg or {}).get("payload") or {}
        if payload.get("pageUrl"):
            _log("收到二次唤醒：%s" % payload["pageUrl"][:120])
        _try_bring_to_front()


def _try_bring_to_front():
    win = _STATE.get("win")
    if win is None:
        return
    try:
        if win.isMinimized():
            win.showNormal()
        win.raise_()
        win.activateWindow()
    except Exception:          # noqa: BLE001
        pass


def bind_window(win):
    """MainWindow 构造完成后调用。"""
    try:
        _hub()                       # 确保 hub 在主线程建好
        _STATE["win"] = win
        pending = _STATE.get("pending") or []
        _STATE["pending"] = []
        for path in pending:
            QtCore.QTimer.singleShot(0, lambda p=path: _open_in_window(win, p))
        return True
    except Exception:          # noqa: BLE001
        _log("bind_window 失败：\n" + traceback.format_exc())
        return False


def current_port():
    return _STATE.get("port") or 0
