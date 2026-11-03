# -*- coding: utf-8 -*-
"""CT3D 与「授权模块」之间的集成桥接层。

机器码计算与授权码编解码已抽到 license_codec.py（零依赖、可独立打包），
本层只做「能安全地 import xray 的 license 模块」+ 落盘/状态判定。
发码工具 gen_license_gui 直接用 license_codec，不经过本层 ——
理由见 license_codec.py 的模块文档。
"""

import os
import sys
import traceback

# 授权码编解码（唯一真相源，与发码工具共用）
from license_codec import (  # noqa: F401
    get_machine_code,
    get_machine_code_short,
    machine_code_display,
    generate_license_key,
    validate_license_key,
    _parse_license_key,
)

# ---------------------------------------------------------------------------
# xray/app 路径引导（与 xray_bridge.py 同一套规矩：append，绝不 insert(0)）
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
XRAY_APP = os.path.join(_HERE, 'xray', 'app')

if os.path.isdir(XRAY_APP) and XRAY_APP not in sys.path:
    sys.path.append(XRAY_APP)

# -*- coding: utf-8 -*-
"""CT3D 与「授权模块」之间的集成桥接层。

设计要点
--------
1. **复用 xray/app/license.py，不重写授权逻辑**
   那个模块本身是完整的（机器码、授权码生成/校验、试用、激活、落盘），
   本层只负责「能安全地 import 它」+「补上它缺的东西」。

2. **机器码改成稳定来源（重要）**
   原 `license.get_machine_code()` 把三样东西拼起来哈希：
   MachineGuid + **hostname** + **MAC 地址**。
   后两者极不稳定 —— 改计算机名、换网卡、虚拟机克隆、拔网线重连
   都会让机器码变化，用户会被反复要求「重新激活」，是纯 bug 源。
   本层改为：**只认 MachineGuid（主板 UUID）**，取不到才降级到
   CPU ID / 主板序列号，且**绝不含 hostname 与 MAC**。

3. **授权校验集中在本层，UI 不直接碰 license 模块**
   便于将来整体替换为 Ed25519 签名方案时，只改这一个文件。

4. **失败一律抛出可读异常，不静默 return False**
   主程序只在「确实没授权」时才拦截；模块坏了要报错而不是假装没授权，
   否则会出现「正版用户被挡在门外且不知道为什么」。
"""

import os
import sys
import traceback

# ---------------------------------------------------------------------------
# xray/app 路径引导（与 xray_bridge.py 同一套规矩：append，绝不 insert(0)）
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
XRAY_APP = os.path.join(_HERE, "xray", "app")

if os.path.isdir(XRAY_APP) and XRAY_APP not in sys.path:
    # xray/app 下也有 main.py，插到最前会让 `import main` 指到它的主程序。
    sys.path.append(XRAY_APP)


# ---------------------------------------------------------------------------
# 机器码：稳定来源
# ---------------------------------------------------------------------------
def license_module():
    """拿到 xray/app/license 模块。

    加载时**立即**把机器码函数替换成本层的稳定版本（见 ``_patch_machine_code``）。
    必须在这里做、而不是等到 activate() 再做：读取路径
    （load_license / check_trial）也会间接触发机器码计算，
    漏掉会导致「刚激活就变成不匹配」。

    加载失败抛 RuntimeError（附原始原因），**不返回 None** ——
    授权模块缺失属于安装损坏，必须让用户知道，而不是当成"没授权"。
    """
    if "license" in sys.modules:
        return sys.modules["license"]
    if not os.path.isdir(XRAY_APP):
        raise RuntimeError(
            "软件安装不完整：找不到授权模块目录\n%s" % XRAY_APP)
    try:
        import license as _lic
    except Exception as exc:               # noqa: BLE001
        raise RuntimeError(
            "授权模块加载失败：%s\n请确认软件目录完整，或联系供应商重新安装。"
            % exc) from exc
    _patch_machine_code(_lic)
    _silence_license_parser_chatter(_lic)
    return _lic


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 授权码生成 / 校验
# ---------------------------------------------------------------------------
#
# 为什么不用 license.py 自带的 generate_license_key：
#
# **它的限期授权码有致命缺陷 —— 实测发现。**
#   raw = f"{hours_hex:08X}H{machine_code[:12]}"   # 例 "00002283" + "H" + "3AB2B53B4C70"
#   然后 shuffle_license() 做「相邻两字符交换」——这是一个**定长、可逆但会
#   跨分隔符打乱位置**的变换。原 validate_license_key 用 split("H") 还原，
#   但 "H" 已被 shuffle 移走，split 出来的两段根本不是「小时 + 机器码」。
#   实测后果：**两个不同机器码签出的限期授权码完全相同**
#   （000022833HBAB2354B7C0），且解不出原始机器码 → 限期授权功能实质失效。
#
# 永久码（"UNLIMITED-" + 16 位）不受影响，因为它是前缀定长、无分隔符。
#
# 因此这里自带一套**定长分段**的编解码，格式如下（全部定长，可无歧义切分）：
#
#   永久：  "CT3D" + "PERM" + <16 位机器码>
#   限期：  "CT3D" + "TERM" + <16 位机器码> + <8 位十六进制小时数>
#
# 之后按固定步长做字符打乱（仅打乱、不改变字符集合），纯混淆、不承担安全性。
# 真正的安全性由「机器码绑定」提供 —— 授权码只能在指定机器上用。
def activate(license_key):
    """用授权码激活。

    返回 ``(ok, message)``，message 为面向用户的中文。
    """
    key = (license_key or "").strip()
    if not key:
        return False, "请输入授权码"

    try:
        lic = license_module()
    except RuntimeError as exc:
        return False, str(exc)

    # 本层用自带的定长编解码，不走 license.activate_license ——
    # 后者的限期码解析有上面记录的缺陷。持久化仍复用 license 模块的落盘函数。
    ok, kind, hours = validate_license_key(key, get_machine_code_short())
    if not ok:
        # 区分两种失败原因，客户才明白该做什么：
        #   · 码本身格式不对  → 可能抄错了
        #   · 码有效但机器不符 → 换机/抄错机器码
        parsed = _parse_license_key(key)
        if parsed is None:
            return False, ("授权码格式不正确，请核对是否复制完整"
                           "（应形如 XXXX…-XXXX…，共 20 或 36 位）")
        return False, ("授权码有效，但与本机机器码不匹配。\n\n"
                       "该授权码是给另一台电脑签发的。"
                       "若您换了电脑，请把新机器码发给我重新签发。")

    try:
        from datetime import datetime, timedelta

        ltype = "unlimited" if kind == "unlimited" else "permanent"
        data = {
            "type": ltype,
            "license_key": key,
            "machine_code": get_machine_code_short(),
            "activated_at": datetime.now().isoformat(),
            "expiry_time": (None if kind == "unlimited"
                            else (datetime.now() +
                                  timedelta(hours=hours)).isoformat()),
            "format": "ct3d-v1",
        }
        import json
        path = lic.get_license_file_path()
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:               # noqa: BLE001
        traceback.print_exc()
        return False, "授权信息写入失败：%s" % exc

    return True, ("永久授权已生效" if kind == "unlimited"
                  else "限期授权已生效，有效期 %d 天"
                       % max(1, hours // 24))


def license_status():
    """检查本机授权状态。

    返回 dict::

        {
            "ok":          bool,   # 是否放行
            "kind":        str,    # trial / permanent / unlimited / none / invalid / broken
            "message":     str,    # 面向用户的中文说明
            "machine_code":str,    # 短机器码
            "hours_left":  int,    # 剩余小时（-1 表示永久/未知）
        }

    ``kind == "broken"`` 表示授权模块本身坏了（安装不完整），
    与"没授权"是两回事 —— 前者应报错，后者才拦截。
    """
    out = {
        "ok": False,
        "kind": "none",
        "message": "未授权",
        "machine_code": get_machine_code_short(),
        "hours_left": 0,
    }

    try:
        lic = license_module()
    except RuntimeError as exc:
        out["kind"] = "broken"
        out["message"] = str(exc)
        return out

    try:
        data = lic.load_license()
    except Exception:                      # noqa: BLE001
        data = None

    if not data:
        # 授权文件读不出来时，先分清三种情况（顺序很重要）：
        #  a) 磁盘上有旧版混淆格式文件 → 提示重新激活
        #  b) 本次运行指向的路径确实没有文件 → 就是「未授权」
        #  c) 目标文件存在但内容不是 JSON → 安装损坏
        #
        # 注意 a) 的判定只在「**目标授权文件本身**不可读」时才生效。
        # 早期版本把「扫描到任意旧格式残留」都判成 legacy_format，
        # 导致全新用户（机器上恰好有 xray 子项目遗留的 dat）在
        # 还没试过激活之前就被拦下，且拦下后无法通过激活自救。
        target = None
        try:
            target = _lic_license_path()
        except Exception:                  # noqa: BLE001
            target = None

        if target and os.path.exists(target) and _looks_like_secure_dat(target):
            out["kind"] = "legacy_format"
            out["message"] = (
                "检测到旧版本授权文件（格式不兼容当前版本），需要重新激活一次。\n"
                "请联系供应商重新签发授权码。")
            return out

        legacy = _find_legacy_secure_files()
        if legacy:
            # 只是别处有历史残留，不影响本次判定，附在消息里备查即可
            out["hint_legacy_files"] = legacy
        return out

    # 机器码必须与本机一致：授权文件被拷到别的机器上要能识别出来
    saved = str(data.get("machine_code", "")).upper()
    if saved and saved != out["machine_code"]:
        out["kind"] = "invalid"
        out["message"] = "授权文件与本机不匹配（可能是拷贝到了其他电脑）"
        return out

    try:
        ok, message = lic.check_trial()
    except Exception:                      # noqa: BLE001
        out["kind"] = "broken"
        out["message"] = "授权校验出错，请重新激活或联系供应商"
        return out

    out["ok"] = bool(ok)
    out["kind"] = str(data.get("type", "none"))
    out["message"] = str(message)

    try:
        if data.get("expiry_time"):
            from datetime import datetime
            exp = datetime.fromisoformat(data["expiry_time"])
            out["hours_left"] = int((exp - datetime.now()).total_seconds() / 3600)
        elif ok:
            out["hours_left"] = -1        # 永久
    except Exception:                      # noqa: BLE001
        pass

    return out


def _patch_machine_code(lic):
    """让 license 模块改用本层的稳定机器码。

    license.py 的 get_machine_code / get_machine_code_short 是模块级函数，
    内部 validate_license_key 会间接用到。这里做**定向替换**：只改这两个
    函数，不动模块其他行为。
    """
    lic.get_machine_code = get_machine_code
    lic.get_machine_code_short = get_machine_code_short


# ---------------------------------------------------------------------------
# 旧授权文件（license_secure 的 XOR 混淆格式）兼容
# ---------------------------------------------------------------------------
#
# 实测：xray/app/license.dat 与 xray/app/config/license.dat 是 **332 字节
# 二进制密文**，是 xray 子项目 license_secure.py 的 XOR 混淆格式；而本层
# 接线的 license.py 用 **明文 JSON**。两者**互不兼容** ——
# 若不处理，用户激活后重启时 load_license() 会 JSON 解析失败、返回 None，
# 表现为「明明激活了却又变成未授权」，且界面上看不出原因。
#
# 因此在 license_status() 里显式区分这两种「读不到」，给出可操作的提示。

def _looks_like_secure_dat(path):
    """判断某文件是否为 license_secure 的混淆格式（而不是 JSON）"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return False
    if not head:
        return False
    stripped = head.lstrip()[:1]
    # JSON 一定以 { 或 [ 开头；混淆密文不会
    return stripped not in (b"{", b"[")


def _find_legacy_secure_files():
    """找出磁盘上遗留的 license_secure 混淆授权文件（仅供参考，不作为拦截依据）"""
    found = []
    base = XRAY_APP
    for rel in (os.path.join("config", "license.dat"), "license.dat"):
        p = os.path.join(base, rel)
        if os.path.exists(p) and _looks_like_secure_dat(p):
            found.append(p)
    return found


def _lic_license_path():
    """取 license 模块认定的授权文件路径（失败返回 None）"""
    lic = license_module()
    return lic.get_license_file_path()


def _silence_license_parser_chatter(lic):
    """屏蔽 license.load_license() 内部对 JSON 解析失败的 print。

    原实现在 except 里 print(f"[ERROR] 加载授权文件失败: {e}")。这有两个问题：
      1. 用户在 GUI 里看不到它，等于**静默失败**（本项目铁律 46）；
      2. 首次运行/旧格式文件时必然触发一次，属于预期路径，不该刷错误日志。

    真正的判定逻辑全部在 license_status() 里（那里有明确的 kind 与 message），
    这里只负责不往 stdout 吐噪音，**不影响任何行为**。
    """
    lic = license_module()
    if getattr(lic, "_ct3d_chatter_patched", False):
        return
    try:
        lic.load_license = lambda: _load_license_quiet(lic)
        lic._ct3d_chatter_patched = True
    except Exception:                       # noqa: BLE001 — 打桩失败不应影响授权
        pass


def _load_license_quiet(lic):
    """等价于 lic.load_license()，但不打印。"""
    path = lic.get_license_file_path()
    if not path or not os.path.exists(path):
        return None
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                       # noqa: BLE001 — 由调用方转成可读提示
        return None


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def start_trial(hours=72):
    """首次运行时开试用（尚未有任何授权文件时才调）。

    返回 ``(ok, message)``。

    注意：**必须先打机器码补丁**。license.create_trial_license →
    save_trial_info 内部会用模块自己的 get_machine_code_short() 把机器码
    写进授权文件；不补丁的话它写入的是「含 hostname+MAC」的那一套短码，
    而 license_status() 比对的是本层的稳定短码 → 两者永远不等 →
    刚开好的试用会被判成「授权与本机不匹配」。
    """
    try:
        lic = license_module()
    except RuntimeError as exc:
        return False, str(exc)
    try:
        _patch_machine_code(lic)
        return lic.create_trial_license(hours)
    except Exception as exc:               # noqa: BLE001
        return False, "试用初始化失败：%s" % exc


def has_license_file():
    """是否已存在授权/试用文件（用于判断该不该首次开试用）"""
    try:
        lic = license_module()
    except RuntimeError:
        return False
    try:
        return lic.load_license() is not None
    except Exception:                      # noqa: BLE001
        return False
