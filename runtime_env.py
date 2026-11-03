"""嵌入式 Python 运行环境的 DLL 搜索路径引导（Windows / Win7 兼容）。

背景
----
本项目是「绿色嵌入式」发行版：自带 pyenv（CPython 3.8.3 + conda 风格布局），
不依赖系统安装的 Python。正常启动走 ``run_env.bat``，它会设置

    PATH=%~dp0\\pyenv;%~dp0\\pyenv\\Library\\bin;...\\PyQt5\\Qt5\\bin;...
    QT_PLUGIN_PATH=...\\PyQt5\\Qt5\\plugins

但桌面快捷方式（install.bat 调 CreateShortcut.vbs 生成）**直接执行**
``pyenv\\pythonw.exe main.py``，**绕过了 run_env.bat**，进程继承的是
Windows 系统 PATH。结果嵌入式环境里的第三方扩展模块会找不到配套 DLL：

  * ``sqlite3``  -> ``_sqlite3.pyd`` 依赖 ``sqlite3.dll``
                    （实际在 ``pyenv\\Library\\bin\\``）
                    => ``ImportError: DLL load failed while importing _sqlite3``
                    => 点击「漏检反馈 / 反馈列表」按钮直接报错弹窗
                       （FeedbackStore / DetectionRecordStore 都用 sqlite3）
  * ``pydicom``  -> cryptography 加载不到配套的 libcrypto
                    => ``AttributeError: module 'lib' has no attribute ...``
  * ``onnxruntime`` / ``cv2`` 等同理（取决于各自的 DLL 依赖）

修复
----
在进程最早期（**任何会 import 扩展模块的语句之前**）把发行包内的关键目录补进：

  1) ``os.environ['PATH']``       —— 供依赖 DLL 的延迟加载使用
  2) ``os.add_dll_directory()``   —— 供 LoadLibraryEx 显式搜索使用

``os.add_dll_directory`` 是 Python 3.8+ / Windows 引入的，正好匹配本项目的
Python 3.8.3 嵌入式环境。Win7 上它依赖 KB2533623 更新，未打补丁时调用会抛
``OSError``，此时静默忽略、仅靠 PATH 兜底即可（PATH 方案在 Win7 上同样有效）。

**必须保持返回句柄的引用**：`os.add_dll_directory()` 返回的对象一旦被 GC，
对应的搜索目录会立刻失效，所以这里用模块级列表 ``_DLL_HANDLES`` 持有。

本模块只依赖标准库，导入无副作用，可安全地在任意 import 之前调用。
"""

import os
import sys

__all__ = ["bootstrap", "app_dir", "added_dirs", "dll_handles"]

# add_dll_directory 的句柄必须持有引用，否则目录会被 GC 掉导致失效
_DLL_HANDLES = []
_ADDED_DIRS = []
_BOOTSTRAPPED = False


def app_dir():
    """本模块所在目录 == 发行包根目录。"""
    return os.path.dirname(os.path.abspath(__file__))


def _candidate_dirs(root):
    """发行包内需要加入 DLL 搜索路径的目录（按优先级从高到低）。"""
    sp = os.path.join("pyenv", "Lib", "site-packages")
    names = (
        os.path.join("pyenv", "Library", "bin"),   # sqlite3.dll / libcrypto / libssl ...
        os.path.join("pyenv", "DLLs"),             # _sqlite3.pyd / _ssl.pyd 等
        os.path.join("pyenv", "Scripts"),
        os.path.join(sp, "PyQt5", "Qt5", "bin"),   # Qt5Core.dll / Qt5Gui.dll ...
        # ^^ 关键：桌面快捷方式直接跑 pythonw.exe，PATH 里没有这个目录。
        #    PyQt5 自身有兜底逻辑，但在 Win7 上并不总可靠，这里显式补上。
        os.path.join(sp, "cv2"),                   # opencv 配套 DLL（部分构建放在这里）
        sp,
        os.path.join("pyenv", "lib", "site-packages"),
        "pyenv",
    )
    out = []
    for name in names:
        path = os.path.join(root, name)
        if os.path.isdir(path) and path not in out:
            out.append(path)
    return out


def _prepend_path(dirs):
    """把目录按优先级移到 PATH 最前面（返回是否发生了变化）。

    **不能只处理「PATH 里没有的目录」**。若目录已经在 PATH 里，却排在
    别家软件后面 —— 典型是用户装过 Git / Anaconda / PostgreSQL，系统
    PATH 里带着另一份 ``libcrypto-3-x64.dll`` —— Windows 的 DLL 搜索会
    先命中那一份，而 cryptography 需要的是 pyenv 里的 1.1 版，于是崩在
    ``module 'lib' has no attribute 'X509_V_FLAG_NOTIFY_POLICY'``
    （实测：Git Bash 的 /mingw64/bin 就能稳定复现，调不调 bootstrap 是
    成败分界）。所以这里要把我们的目录**提到最前**，而不是「缺失才补」。
    """
    path_env = os.environ.get("PATH", "")
    sep = os.pathsep
    current = [p for p in path_env.split(sep) if p]

    # dirs 按优先级从高到低，去重后整体前置
    wanted, seen = [], set()
    for d in dirs:
        key = os.path.normcase(d)
        if key not in seen:
            seen.add(key)
            wanted.append(d)

    lowered = {os.path.normcase(d) for d in wanted}
    rest = [p for p in current if os.path.normcase(p) not in lowered]
    new = wanted + rest
    if new == current:
        return False
    os.environ["PATH"] = sep.join(new)
    return True


def _add_dll_directory(dirs):
    """Python 3.8+ / Windows：注册显式 DLL 搜索目录。"""
    adder = getattr(os, "add_dll_directory", None)
    if adder is None:
        return
    for d in dirs:
        try:
            handle = adder(d)
        except Exception:            # noqa: BLE001  (Win7 未打 KB2533623 等)
            continue
        if handle is not None:
            _DLL_HANDLES.append(handle)


def _ensure_qt_plugin_path(root):
    """兜底设置 QT_PLUGIN_PATH（快捷方式启动时 run_env.bat 没设过）。

    注意：已设置但**指向不存在的目录**时必须覆盖。历史版本 run_env.bat
    写的是 ``pyenv\\Library\\plugins``（根本不存在），而这个环境变量一旦
    被设上，Qt 就不会再去默认位置找平台插件 —— 表现是「程序起来了但
    窗口一片空白 / 无法创建 QWidget」。Win7 上更隐蔽，所以这里必须校验。
    """
    cur = os.environ.get("QT_PLUGIN_PATH")
    if cur and os.path.isdir(cur):
        return
    for rel in (os.path.join("pyenv", "Lib", "site-packages", "PyQt5", "Qt5", "plugins"),
                os.path.join("pyenv", "lib", "site-packages", "PyQt5", "Qt5", "plugins")):
        path = os.path.join(root, rel)
        if os.path.isdir(path):
            os.environ["QT_PLUGIN_PATH"] = path
            return


def bootstrap():
    """修正嵌入式环境的 DLL 搜索路径。幂等，可重复调用。

    必须在 import pydicom / PyQt5 等扩展模块之前调用。
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return _ADDED_DIRS
    _BOOTSTRAPPED = True

    root = app_dir()
    dirs = _candidate_dirs(root)

    # 1) PATH（Win7 / Python 3.8 都可靠）
    _prepend_path(dirs)

    # 2) 显式 DLL 搜索目录（Python 3.8+）
    _add_dll_directory(dirs)

    # 3) Qt 插件目录
    _ensure_qt_plugin_path(root)

    # 4) 把发行包根目录放进 sys.path（保证 `import runtime_env` 之后
    #    同目录的 xray_bridge / ct3d_utils / loading_ui 都能被找到）
    if root not in sys.path:
        try:
            sys.path.insert(0, root)
        except Exception:            # noqa: BLE001
            pass

    _ADDED_DIRS.extend(dirs)
    return dirs


def dll_handles():
    """返回持有的 DLL 目录句柄列表（诊断用，勿释放）。"""
    return list(_DLL_HANDLES)


def added_dirs():
    """返回已加入搜索路径的目录列表（诊断用）。"""
    return list(_ADDED_DIRS)
