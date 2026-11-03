#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""web_bridge_native.py —— Chrome 原生消息（Native Messaging）转发器。

## 为什么需要它

扩展 → 本机软件的常规通道是 `http://127.0.0.1:18923`。这条路在装了代理
软件 / VPN（Clash、v2ray 的 TUN 模式）、或企业装了代理管理扩展的机器上会
**静默失败**：`fetch` 直接 reject，连 TCP 都建不起来，而且浏览器不给任何
可读的错误。现象是「软件被协议唤醒，但一张图都没送过去」。

原生消息走的是 Chrome 与本地进程之间的 **stdin/stdout 管道**，完全不经过
网络栈，代理、VPN、防火墙、混合内容策略一概影响不到。

## 它做什么

一个极薄的转发器：把扩展发来的 JSON 用 urllib **显式禁用代理** 转发给
现有的 HTTP 服务（127.0.0.1:18923）。业务逻辑一行都不重复 —— 服务端还是
`web_bridge.py`，所以下载、落盘、打开目录的行为完全一致。

## 协议

Chrome 的规则：消息 = 4 字节小端长度 + UTF-8 JSON。

    入：{"cmd":"ping"}
    出：{"ok":true,"app":"zhigu-ct3d","port":18923,"bound":true}

    入：{"cmd":"import","payload":{...}}
    出：{"ok":true,"taskId":"...","series":2,"images":5}

    入：{"cmd":"task","id":"..."}
    出：{"task":{...}}

**stdout 只能有协议数据**，任何 print 都会让 Chrome 判定"消息格式错误"并
断开连接。所以本文件的日志一律写文件，不碰 stdout。
"""

from __future__ import print_function

import json
import os
import struct
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

APP_ID = "zhigu-ct3d"
# 与 web_bridge.py 保持一致：允许环境变量覆盖端口段，便于自动化测试隔离。
# 正式使用不设这些变量，就是 18923 起 8 个端口。
PORT_BASE = int(os.environ.get("ZHIGU_BRIDGE_PORT_BASE") or 18923)
PORT_COUNT = int(os.environ.get("ZHIGU_BRIDGE_PORT_COUNT") or 8)
HTTP_TIMEOUT = 12

_HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(_HERE, "web_bridge_native.log")


def _log(msg):
    """写文件而不是 stderr —— stderr 会被 Chrome 丢弃或混进日志，看不出来。"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(msg.rstrip() + "\n")
    except Exception:          # noqa: BLE001
        pass


# 关键：显式禁用代理。urllib 默认会读系统代理（getproxies），
# 而很多代理软件会把 127.0.0.1 也一起劫持 —— 那正是 HTTP 通道失效的原因。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_HEADERS = {
    "X-Zhigu-Bridge": "1",
    "Content-Type": "application/json",
    "User-Agent": "ZhiguNativeBridge/1.0",
}


def _http(url, body=None):
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_HEADERS)
    with _OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


_CACHED_PORT = [0]


def _find_port():
    """找到在监听的端口。找不到返回 0 和每个端口的失败原因。

    缓存上次成功的端口：进度轮询每 ~1 秒一次，每次都扫 8 个端口会把日志刷满
    （每轮 8 条 /ping），也会让 task 查询变慢。只用一次 ping 验证缓存端口即可。
    """
    tried = []
    cached = _CACHED_PORT[0]
    if cached:
        try:
            info = _http("http://127.0.0.1:%d/ping" % cached)
            if info and info.get("app") == APP_ID:
                return cached, info, tried
        except Exception:              # noqa: BLE001
            pass
        _CACHED_PORT[0] = 0            # 缓存失效，走全量扫描

    for i in range(PORT_COUNT):
        port = PORT_BASE + i
        try:
            info = _http("http://127.0.0.1:%d/ping" % port)
        except Exception as exc:       # noqa: BLE001
            tried.append("%d:%s" % (port, type(exc).__name__))
            continue
        if info and info.get("app") == APP_ID:
            _CACHED_PORT[0] = port
            return port, info, tried
        tried.append("%d:app 不匹配" % port)
    return 0, None, tried


# ------------------------------------------------------------------ 消息编解码

def read_message(stream):
    head = stream.read(4)
    if not head or len(head) < 4:
        return None
    (length,) = struct.unpack("<I", head)
    if length == 0 or length > 64 * 1024 * 1024:
        _log("消息长度异常：%d" % length)
        return None
    payload = stream.read(length)
    if payload is None or len(payload) < length:
        return None
    return json.loads(payload.decode("utf-8"))


def write_message(stream, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(data)))
    stream.write(data)
    stream.flush()


# ------------------------------------------------------------------ 业务

def handle(msg):
    cmd = (msg or {}).get("cmd")
    if cmd == "ping":
        port, info, tried = _find_port()
        if not port:
            return {"ok": False, "app": APP_ID, "port": 0,
                    "error": "软件未运行", "tried": tried}
        out = {"ok": True, "app": APP_ID, "port": port}
        out.update(info or {})
        return out

    if cmd == "import":
        port, info, tried = _find_port()
        if not port:
            return {"ok": False, "error": "软件未运行（%s）" % "; ".join(tried),
                    "tried": tried}
        try:
            r = _http("http://127.0.0.1:%d/import" % port, msg.get("payload") or {})
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:          # noqa: BLE001
                pass
            _log("import HTTP %s %s" % (exc.code, detail))
            return {"ok": False, "error": "HTTP %s %s" % (exc.code, detail), "port": port}
        except Exception as exc:       # noqa: BLE001
            _log("import 失败：\n" + traceback.format_exc())
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "port": port}
        r = dict(r or {})
        r.setdefault("port", port)
        return r

    if cmd == "task":
        port, info, tried = _find_port()
        if not port:
            return {"task": None, "error": "软件未运行"}
        try:
            return _http("http://127.0.0.1:%d/task?id=%s"
                         % (port, urllib.parse.quote(str(msg.get("id") or ""))))
        except Exception as exc:       # noqa: BLE001
            return {"task": None, "error": "%s: %s" % (type(exc).__name__, exc)}

    return {"ok": False, "error": "unknown cmd: %s" % cmd}


def main():
    # Windows 上必须把 stdio 切成二进制模式，否则 \r\n 转换会把长度字段弄坏
    try:
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    except Exception:              # noqa: BLE001
        pass

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    _log("native host 启动 pid=%d argv=%s" % (os.getpid(), sys.argv[1:]))

    while True:
        try:
            msg = read_message(stdin)
        except Exception:          # noqa: BLE001
            _log("读取消息失败：\n" + traceback.format_exc())
            break
        if msg is None:
            break                  # 管道关闭 = 扩展断开
        try:
            reply = handle(msg)
        except Exception:          # noqa: BLE001
            _log("处理消息异常：\n" + traceback.format_exc())
            reply = {"ok": False, "error": "native host 内部错误，详见 web_bridge_native.log"}
        # 回显请求 id：扩展用长连接并发发多条消息，靠它把响应配对回去
        if isinstance(msg, dict) and "__id" in msg and isinstance(reply, dict):
            reply["__id"] = msg["__id"]
        try:
            write_message(stdout, reply)
        except Exception:          # noqa: BLE001
            _log("回写失败：\n" + traceback.format_exc())
            break
    _log("native host 退出 pid=%d" % os.getpid())


if __name__ == "__main__":
    main()
