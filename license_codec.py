# -*- coding: utf-8 -*-
"""授权码编解码 —— **零依赖的独立实现**（唯一真相源）。

为什么单独成文件
----------------
1. **发码工具要能独立打包成 exe**。若让 gen_license_gui 去 import
   license_bridge，PyInstaller 会顺着 license_bridge 顶层的
   `sys.path.append(xray/app)` 递归分析整个 xray 子项目树
   （含 3.7 GB 的独立 Python 与全部推理依赖），把不相干的东西全打进
   产物；实测打出来的 exe **启动即段错误 0xC0000005**，且产物 86 MB。
   本文件只依赖 hashlib / winreg / json，打包产物只有 40 MB 且稳定。
2. **主程序与发码工具必须算出同一个码**。若两边各写一份，天长日久
   必然分叉，导致「签发的码激活不了」。所以这里是唯一定义处，
   license_bridge 与 gen_license_gui 都从这里 import。

代码由 build 脚本从 license_bridge 精确抽取，保证两边逻辑永远一致。
"""

import hashlib


def _read_registry_str(hive, path, name):
    """读注册表字符串值，失败返回 None（不抛）"""
    try:
        import winreg
        with winreg.OpenKey(hive, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value) if value is not None else None
    except Exception:       # noqa: BLE001 — 任何失败都只当"取不到"
        return None


def _stable_hardware_ids():
    """按稳定性从高到低收集硬件标识。

    只取**不会因为日常使用而改变**的项：
    - MachineGuid：Windows 安装时生成的主板 UUID，最稳定
    - CPUID：CPU 序列号，虚拟机/换主板才会变
    - 主板序列号（部分设备写在 BIOS 键里）

    **刻意不取 hostname 与 MAC 地址** —— 它们会因改名字、换网卡、
    虚拟机克隆而变化，会导致用户被迫反复重新激活。
    """
    import winreg

    ids = []

    guid = _read_registry_str(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography", "MachineGuid")
    if guid:
        ids.append(("MachineGuid", guid))

    cpu = _read_registry_str(
        winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        "ProcessorNameString")
    if cpu:
        # CPU 型号不是唯一标识，仅作为「多机同 MachineGuid 时的区分补充」，
        # 放在最后，且只取型号不取序列号（型号在同型号机器间会重复）。
        ids.append(("CPU", cpu))

    if not ids:
        # 最后兜底：给一个固定的、可重复的默认值（同一台机每次都一样）
        ids.append(("FALLBACK", "CT3D-NO-HWID"))

    return ids


def get_machine_code():
    """稳定机器码（完整 SHA256 十六进制）"""
    import hashlib

    parts = ["%s=%s" % (k, v) for k, v in _stable_hardware_ids()]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def get_machine_code_short():
    """短机器码（16 位大写），用于界面显示与授权码绑定"""
    return get_machine_code()[:16].upper()


def machine_code_display():
    """给用户看的机器码：分组显示，便于电话/微信报读

    形如 `6DF0-707D-7960-30B4`
    """
    s = get_machine_code_short()
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


# ---------------------------------------------------------------------------
# license 模块加载
# ---------------------------------------------------------------------------



_PREFIX = "CT3D"
_KIND_PERM = "PERM"
_KIND_TERM = "TERM"
_MC_LEN = 16
_HOURS_LEN = 8

# 字符替换表：数字与字母互换，便于口头报读/手工输入时不易看错
# （0<->O 会混，故数字只与数字互转，字母只与字母互转）。
_SCRAMBLE_MAP = {
    "0": "7", "1": "4", "2": "9", "3": "6", "4": "1", "5": "8",
    "6": "3", "7": "0", "8": "5", "9": "2",
    "A": "Z", "B": "W", "C": "X", "D": "Q", "E": "R", "F": "K",
    "G": "J", "H": "S", "I": "T", "J": "G", "K": "F", "L": "M",
    "M": "L", "N": "Y", "O": "V", "P": "U", "Q": "D", "R": "E",
    "S": "H", "T": "I", "U": "P", "V": "O", "W": "B", "X": "C",
    "Y": "N", "Z": "A",
}
_UNSCRAMBLE_MAP = {v: k for k, v in _SCRAMBLE_MAP.items()}


def _scramble(text):
    """字符替换混淆：按固定映射表逐字符替换。定长、可逆、不增删字符。

    为什么用替换表而不是移位：移位类混淆在固定长度下容易出现
    **多轮互相抵消**（实测 3 轮移位后 `CT3DTERM3AB2B53B…` 原样露出，
    等于没混淆）。逐字符替换不会自抵消，且机器码、期限、类型标记
    都不会以可读形式出现。
    """
    return "".join(_SCRAMBLE_MAP.get(ch, ch) for ch in text)


def _unscramble(text):
    return "".join(_UNSCRAMBLE_MAP.get(ch, ch) for ch in text)


def _format_key(raw):
    """把定长明文排版成授权码（每 16 位加一个连字符，便于口头报读/手工输入）"""
    groups = [raw[i:i + 16] for i in range(0, len(raw), 16)]
    return "-".join(groups)


def generate_license_key(hours=0, machine_code=None):
    """生成授权码（给发码工具用）。

    hours=0 → 永久授权；hours>0 → 限期 hours 小时。
    machine_code 为 None 时用**本机**机器码；给定字符串时为**该机器**签发
    （发码工具必须显式传客户机器码，否则会把自己电脑的码发给所有客户）。
    """
    mc = (machine_code or get_machine_code_short())
    mc = "".join(ch for ch in str(mc).upper() if ch.isalnum())[:_MC_LEN]
    if len(mc) != _MC_LEN:
        raise ValueError("机器码必须是 %d 位十六进制，收到：%r" % (_MC_LEN, machine_code))
    if hours:
        raw = "%s%s%s%s" % (_PREFIX, _KIND_TERM, mc,
                            format(int(hours), "0%dX" % _HOURS_LEN))
    else:
        raw = "%s%s%s" % (_PREFIX, _KIND_PERM, mc)
    return _format_key(_scramble(raw))


def _parse_license_key(license_key):
    """解析授权码 → (kind, machine_code, hours)

    kind 为 "unlimited" / "term"；无法解析返回 None。
    """
    s = (license_key or "").strip().upper().replace("-", "").replace(" ", "")
    if len(s) % 2:
        s = s[:-1]
    raw = _unscramble(s)
    if not raw.startswith(_PREFIX):
        return None
    body = raw[len(_PREFIX):]
    if body.startswith(_KIND_PERM):
        mc = body[len(_KIND_PERM):]
        if len(mc) >= _MC_LEN:
            return "unlimited", mc[:_MC_LEN], 0
        return None
    if body.startswith(_KIND_TERM):
        rest = body[len(_KIND_TERM):]
        if len(rest) >= _MC_LEN + _HOURS_LEN:
            mc = rest[:_MC_LEN]
            try:
                hours = int(rest[_MC_LEN:_MC_LEN + _HOURS_LEN], 16)
            except ValueError:
                return None
            return "term", mc, hours
    return None


def validate_license_key(license_key, machine_code):
    """校验授权码是否适用于本机。

    返回 ``(ok, kind, hours)``，kind 为 "unlimited" / "term" / None。
    与 license.py 原函数同名同签名，便于替换。
    """
    parsed = _parse_license_key(license_key)
    if parsed is None:
        return False, None, 0
    kind, key_mc, hours = parsed
    if key_mc.upper() != str(machine_code).upper()[:_MC_LEN]:
        return False, None, 0
    return True, kind, hours


