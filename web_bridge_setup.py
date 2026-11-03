# -*- coding: utf-8 -*-
"""web_bridge_setup —— 注册 / 卸载 zhigu:// 自定义协议

为什么不用 .bat / .ps1 写注册表：
安装目录名常带中文（"D:\\医学影像\\知骨..."），.bat 必须存成 GBK、
.ps1 必须存成 UTF-8 with BOM 才不乱码，一旦编辑器存错就是"注册成功但
打不开"的诡异故障。用发行包自带的 Python 执行就没有编码问题。

用法（双击只会上报成功/失败，不弹控制台）：
    python web_bridge_setup.py            注册（默认）
    python web_bridge_setup.py --uninstall  卸载
    python web_bridge_setup.py --check      只检查，不写注册表
"""

import os
import sys

try:
    import winreg
except ImportError:                 # 非 Windows
    winreg = None

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_ROOT = r"Software\Classes\zhigu"
PROTO_DESC = "URL:Zhigu Imaging Protocol"


def _msgbox(text, title="知骨影像 · 网页打开", flags=0x40):
    """0x40=信息  0x10=错误  0x30=警告。没有 GUI 时退回控制台。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
        return
    except Exception:               # noqa: BLE001
        pass
    try:
        print(text)
    except Exception:               # noqa: BLE001
        pass


def resolve_app_dir():
    """本脚本放在 <安装包>\\web_bridge\\，发行包在 <安装包>\\CT3D260414_release\\。
    也兼容把本脚本直接丢进发行包根目录的情况。"""
    if os.path.exists(os.path.join(HERE, "main.py")) and \
            os.path.isdir(os.path.join(HERE, "pyenv")):
        return HERE
    parent = os.path.dirname(HERE)
    for name in ("CT3D260414_release",):
        cand = os.path.join(parent, name)
        if os.path.exists(os.path.join(cand, "main.py")):
            return cand
    # 再兜一层：扫描同级目录里第一个带 main.py + pyenv 的
    try:
        for entry in os.listdir(parent):
            cand = os.path.join(parent, entry)
            if os.path.exists(os.path.join(cand, "main.py")) and \
                    os.path.isdir(os.path.join(cand, "pyenv")):
                return cand
    except OSError:
        pass
    return ""


def build_command(app_dir):
    """注册表里的命令行。%1 由 Windows 替换成 zhigu://... 整串 URL。"""
    pythonw = os.path.join(app_dir, "pyenv", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = os.path.join(app_dir, "pyenv", "python.exe")
    main_py = os.path.join(app_dir, "main.py")
    return '"%s" "%s" "%%1"' % (pythonw, main_py)


def install(app_dir):
    exe_cmd = build_command(app_dir)
    icon = os.path.join(app_dir, "logo.ico")
    if not os.path.exists(icon):
        icon = os.path.join(app_dir, "pyenv", "pythonw.exe")

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY_ROOT) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, PROTO_DESC)
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY_ROOT + r"\DefaultIcon") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, icon + ",0")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          KEY_ROOT + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, exe_cmd)
    return exe_cmd


def uninstall():
    def delete_tree(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_ALL_ACCESS) as key:
                while True:
                    try:
                        sub = winreg.EnumKey(key, 0)
                    except OSError:
                        break
                    delete_tree(path + "\\" + sub)
        except OSError:
            return
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    delete_tree(KEY_ROOT)


def current_command():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            KEY_ROOT + r"\shell\open\command") as key:
            return winreg.QueryValueEx(key, "")[0]
    except OSError:
        return ""


def main(argv):
    if winreg is None:
        _msgbox("当前系统不支持注册表操作。", flags=0x10)
        return 1

    if "--uninstall" in argv:
        uninstall()
        _msgbox("已撤销「知骨影像」的网页打开协议。\n\n"
                "浏览器插件再次点击时会提示软件未启动。")
        return 0

    app_dir = resolve_app_dir()
    if not app_dir:
        _msgbox("没找到软件目录。\n\n"
                "请把 web_bridge 文件夹和 CT3D260414_release 放在同一个目录下，"
                "再运行本脚本。", flags=0x10)
        return 1

    missing = []
    for rel in ("main.py", os.path.join("pyenv", "pythonw.exe")):
        if not os.path.exists(os.path.join(app_dir, rel)):
            missing.append(rel)
    if missing:
        _msgbox("软件目录不完整，缺少：\n  " + "\n  ".join(missing) +
                "\n\n目录：" + app_dir, flags=0x10)
        return 1

    if "--check" in argv:
        cmd = current_command()
        _msgbox("软件目录：\n%s\n\n当前注册命令：\n%s" % (app_dir, cmd or "（未注册）"))
        return 0

    cmd = install(app_dir)
    _msgbox("注册成功。\n\n"
            "现在浏览器里点「用知骨打开」就能唤起本软件了。\n\n"
            "注册的命令行：\n%s\n\n"
            "如需撤销，运行：\n"
            "  web_bridge_setup.py --uninstall" % cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
