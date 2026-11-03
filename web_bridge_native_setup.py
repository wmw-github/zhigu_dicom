# -*- coding: utf-8 -*-
"""web_bridge_native_setup.py —— 注册 Chrome / Edge 原生消息宿主。

用法（发行包自带的 Python）：

    pyenv\\python.exe web_bridge_native_setup.py          注册（或重新注册）
    pyenv\\python.exe web_bridge_native_setup.py --check   只检查当前状态
    pyenv\\python.exe web_bridge_native_setup.py --remove  卸载

它做三件事：
  1. 在发行包根目录生成 `com.zhigu.bridge.json`（Chrome 要求 manifest 单独成文件）
  2. 把 manifest 的绝对路径写进
     `HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.zhigu.bridge`
     （Edge 同样写一份，用 Edge 的用户也能用）
  3. 校验扩展 ID 与 allowed_origins 对得上

为什么用 Python 而不是 .reg / .bat：注册表里要写含中文或空格的绝对路径，
用 bat 传参会撞上 GBK/UTF-8 编码问题（本项目的既有教训）。
"""

from __future__ import print_function

import json
import os
import sys

HOST_NAME = "com.zhigu.bridge"
# manifest.json 里的 "key" 固定了这个扩展 ID。改了 key 就必须同步改这里。
EXT_ID = "enemdgbbndekeeihllpbikeifjnlefmj"

_HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(_HERE, HOST_NAME + ".json")
LAUNCHER = os.path.join(_HERE, "web_bridge_native.bat")
CORE = os.path.join(_HERE, "web_bridge_native.py")

REG_BASES = [
    r"Software\Google\Chrome\NativeMessagingHosts",
    r"Software\Microsoft\Edge\NativeMessagingHosts",
]


def _msgbox(text, title="知骨影像", flags=0x40):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:          # noqa: BLE001
        print(text)


def build_manifest():
    return {
        "name": HOST_NAME,
        "description": "知骨影像 · 网页取图桥接（stdin/stdout，不经网络栈）",
        "path": LAUNCHER,
        "type": "stdio",
        "allowed_origins": ["chrome-extension://%s/" % EXT_ID],
    }


def write_manifest():
    data = build_manifest()
    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return data


def register():
    import winreg
    written = []
    for base in REG_BASES:
        key_path = base + "\\" + HOST_NAME
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                    winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, MANIFEST_PATH)
            written.append(key_path)
        except OSError as exc:
            print("  写入失败 %s -> %s" % (key_path, exc))
    return written


def current():
    """返回 [(base, 当前值或 None)]"""
    import winreg
    out = []
    for base in REG_BASES:
        key_path = base + "\\" + HOST_NAME
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                val, _ = winreg.QueryValueEx(key, "")
        except OSError:
            val = None
        out.append((key_path, val))
    return out


def remove():
    import winreg
    removed = []
    for base in REG_BASES:
        key_path = base + "\\" + HOST_NAME
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            removed.append(key_path)
        except OSError:
            pass
    try:
        if os.path.exists(MANIFEST_PATH):
            os.remove(MANIFEST_PATH)
    except OSError:
        pass
    return removed


def check():
    print("扩展 ID        : %s" % EXT_ID)
    print("启动器         : %s  %s" % (LAUNCHER, "存在" if os.path.exists(LAUNCHER) else "缺失"))
    print("宿主脚本       : %s  %s" % (CORE, "存在" if os.path.exists(CORE) else "缺失"))
    print("manifest       : %s  %s" % (MANIFEST_PATH, "存在" if os.path.exists(MANIFEST_PATH) else "缺失"))
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            print("  path         : %s" % data.get("path"))
            print("  origins      : %s" % ", ".join(data.get("allowed_origins") or []))
        except Exception as exc:       # noqa: BLE001
            print("  manifest 读取失败：%s" % exc)
    for key_path, val in current():
        print("注册表 %s\n  -> %s" % (key_path, val or "(未注册)"))
    ok = (os.path.exists(LAUNCHER) and os.path.exists(CORE)
          and os.path.exists(MANIFEST_PATH)
          and all(val for _, val in current()))
    return ok


def main(argv):
    quiet = "--quiet" in argv
    action = "--check" if "--check" in argv else ("--remove" if "--remove" in argv else "")

    if action == "--check":
        ok = check()
        print("\n结论：%s" % ("已就绪" if ok else "未就绪，请运行本脚本注册"))
        return 0 if ok else 1

    if action == "--remove":
        for p in remove():
            print("已删除 %s" % p)
        if not quiet:
            _msgbox("已卸载原生消息宿主。\n扩展将只能走 HTTP 回环通道。", "知骨影像")
        return 0

    if not os.path.exists(CORE) or not os.path.exists(LAUNCHER):
        print("缺少 web_bridge_native.py 或 web_bridge_native.bat")
        if not quiet:
            _msgbox("缺少 web_bridge_native.py 或 web_bridge_native.bat，\n"
                    "请确认本脚本放在发行包根目录。", "知骨影像", 0x30)
        return 2

    write_manifest()
    written = register()
    print("已生成 manifest：%s" % MANIFEST_PATH)
    for p in written:
        print("已注册：%s" % p)
    check()
    if not quiet:
        _msgbox(
            "原生消息宿主已注册。\n\n"
            "接下来请在浏览器里：\n"
            "1) chrome://extensions 重新加载扩展\n"
            "2) 确认扩展 ID 为\n   %s\n"
            "3) 刷新影像页面后重新点按钮\n\n"
            "（扩展 ID 对不上时原生通道会被浏览器拒绝）" % EXT_ID,
            "知骨影像")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
