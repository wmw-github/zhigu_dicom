"""
增强版授权系统 - 多文件防破解保护

核心特性:
1. 多文件分散存储 - 在多个位置生成license文件
2. 文件交叉验证 - 各文件间数据相互校验
3. 防篡改检测 - 检测文件是否被修改或删除
4. 首次运行生成多文件 - 增加破解难度
"""

import os
import json
import hashlib
import time
import random
import string
from datetime import datetime, timedelta
from pathlib import Path


# 多个存储位置配置（分散存储增加破解难度）
LICENSE_PATHS = [
    ("app", "license.dat"),
    ("data", ".sys_config"),
    ("config", "settings.bin"),
    ("logs", ".cache_idx"),
    (".", ".license_key"),
    ("app", "config", "license.dat"),
    ("app", "data", ".registry"),
]

# 混淆密钥（用于数据混淆）
OBFUSCATION_KEYS = [
    b"XRayDiag2024SecureKey#1",
    b"MedicalAI@LicenseProtect",
    b"ChestXRay#SecureAuth2024",
    b"VinDrCXR@ProtectedLicense",
]


def get_machine_code():
    """获取机器码（基于硬件信息）"""
    code_parts = []
    
    try:
        import winreg
        registry_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"),
            (winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS"),
        ]
        for hkey, path in registry_paths:
            try:
                key = winreg.OpenKey(hkey, path)
                try:
                    value, _ = winreg.QueryValueEx(key, "MachineGuid")
                    code_parts.append(value)
                except:
                    pass
                winreg.CloseKey(key)
            except:
                pass
    except:
        pass
    
    try:
        import socket
        hostname = socket.gethostname()
        code_parts.append(hostname)
    except:
        pass
    
    try:
        import uuid
        mac = uuid.UUID(int=uuid.getnode()).hex[-12:]
        code_parts.append(":".join([mac[i:i+2] for i in range(0, 12, 2)]))
    except:
        pass
    
    if not code_parts:
        code_parts.append("default")
    
    combined = "|".join(code_parts)
    machine_code = hashlib.sha256(combined.encode('utf-8')).hexdigest()
    return machine_code


def get_machine_code_short():
    """获取短机器码（前16位）"""
    return get_machine_code()[:16].upper()


def xor_encrypt(data: bytes, key: bytes) -> bytes:
    """XOR加密/解密"""
    return bytes([data[i] ^ key[i % len(key)] for i in range(len(data))])


def obfuscate_data(data: dict, key_index: int = None) -> bytes:
    """混淆数据"""
    if key_index is None:
        key_index = random.randint(0, len(OBFUSCATION_KEYS) - 1)
    
    json_str = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    json_bytes = json_str.encode('utf-8')
    
    # 添加随机前缀干扰
    prefix = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    prefixed = prefix.encode('utf-8') + b"|" + json_bytes
    
    # XOR加密
    encrypted = xor_encrypt(prefixed, OBFUSCATION_KEYS[key_index])
    
    # 添加密钥标识
    result = bytes([key_index]) + encrypted
    return result


def deobfuscate_data(data: bytes) -> dict:
    """解混淆数据"""
    if not data or len(data) < 2:
        return None
    
    key_index = data[0]
    if key_index >= len(OBFUSCATION_KEYS):
        return None
    
    encrypted = data[1:]
    decrypted = xor_encrypt(encrypted, OBFUSCATION_KEYS[key_index])
    
    try:
        parts = decrypted.split(b"|", 1)
        if len(parts) != 2:
            return None
        json_str = parts[1].decode('utf-8')
        return json.loads(json_str)
    except:
        return None


def generate_file_signature(data: dict, secret: str = None) -> str:
    """生成数据签名用于防篡改检测"""
    if secret is None:
        secret = get_machine_code()
    
    # 创建签名基础数据
    sig_data = {
        "machine": data.get("machine_code", ""),
        "type": data.get("type", ""),
        "expiry": str(data.get("expiry_time", "")),
        "created": str(data.get("created_at", "")),
        "secret": secret,
    }
    
    sig_str = json.dumps(sig_data, sort_keys=True, ensure_ascii=False)
    signature = hashlib.sha256(sig_str.encode('utf-8')).hexdigest()[:32]
    return signature


def verify_file_signature(data: dict) -> bool:
    """验证文件签名"""
    stored_sig = data.get("signature", "")
    expected_sig = generate_file_signature(data)
    return stored_sig == expected_sig


def get_all_license_paths():
    """获取所有可能的授权文件路径"""
    base_dir = Path(os.path.dirname(os.path.abspath(__file__))).parent
    
    paths = []
    for path_parts in LICENSE_PATHS:
        full_path = base_dir.joinpath(*path_parts)
        paths.append(full_path)
    
    return paths


def generate_unique_file_id():
    """生成唯一文件ID用于交叉验证"""
    return hashlib.sha256(
        (get_machine_code() + str(time.time())).encode()
    ).hexdigest()[:16]


def create_license_file(file_path: Path, license_data: dict, file_id: str = None):
    """创建单个授权文件"""
    if file_id is None:
        file_id = generate_unique_file_id()
    
    # 为每个文件添加唯一标识
    data = {
        **license_data,
        "file_id": file_id,
        "file_path_hash": hashlib.sha256(str(file_path).encode()).hexdigest()[:16],
    }
    
    # 生成签名
    data["signature"] = generate_file_signature(data)
    
    # 混淆并保存
    obfuscated = obfuscate_data(data)
    
    # 确保目录存在
    file_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 写入文件（使用二进制模式）
    with open(file_path, 'wb') as f:
        f.write(obfuscated)
    
    return file_id


def read_license_file(file_path: Path) -> dict:
    """读取授权文件"""
    try:
        if not file_path.exists():
            return None
        
        with open(file_path, 'rb') as f:
            data = f.read()
        
        return deobfuscate_data(data)
    except:
        return None


def create_multiple_license_files(license_data: dict):
    """创建多个授权文件（首次运行或创建试用时调用）"""
    paths = get_all_license_paths()
    file_id = generate_unique_file_id()
    created_count = 0
    
    for path in paths:
        try:
            create_license_file(path, license_data, file_id)
            created_count += 1
        except Exception as e:
            print(f"[警告] 无法创建授权文件 {path}: {e}")
    
    return created_count, file_id


def verify_all_license_files() -> tuple:
    """
    验证所有授权文件
    
    返回: (is_valid, license_data, tampered_count)
    - is_valid: 是否有有效授权
    - license_data: 有效的授权数据
    - tampered_count: 被篡改的文件数量
    """
    paths = get_all_license_paths()
    valid_files = []
    tampered_count = 0
    file_ids = set()
    
    for path in paths:
        data = read_license_file(path)
        
        if data is None:
            continue
        
        # 验证签名
        if not verify_file_signature(data):
            tampered_count += 1
            continue
        
        # 验证机器码
        file_machine = data.get("machine_code", "")
        current_machine = get_machine_code_short()
        
        if file_machine != current_machine:
            tampered_count += 1
            continue
        
        valid_files.append(data)
        file_ids.add(data.get("file_id", ""))
    
    if not valid_files:
        return False, None, tampered_count
    
    # 检查文件ID一致性（所有文件应该有相同的ID）
    if len(file_ids) > 1:
        # 文件ID不一致，可能是被篡改
        tampered_count += 1
    
    # 使用最新的有效数据
    latest_data = max(valid_files, key=lambda x: x.get("created_at", ""))
    
    # 检查是否过期
    expiry_str = latest_data.get("expiry_time")
    if expiry_str:
        try:
            expiry_time = datetime.fromisoformat(expiry_str)
            if datetime.now() >= expiry_time:
                return False, latest_data, tampered_count
        except:
            pass
    
    return True, latest_data, tampered_count


def save_license_secure(license_key: str, license_type: str = "permanent"):
    """安全保存授权"""
    from license import validate_license_key
    
    machine_code = get_machine_code_short()
    is_valid, ltype, remaining = validate_license_key(license_key, machine_code)
    
    if not is_valid:
        return False, "无效的授权码"
    
    expiry_time = None
    if ltype != "unlimited" and remaining:
        expiry_time = datetime.now() + timedelta(seconds=remaining)
    
    license_data = {
        "type": license_type if ltype == "unlimited" else "trial" if ltype == "trial" else "permanent",
        "license_key": license_key,
        "machine_code": machine_code,
        "activated_at": datetime.now().isoformat(),
        "expiry_time": expiry_time.isoformat() if expiry_time else None,
        "created_at": datetime.now().isoformat(),
    }
    
    created_count, file_id = create_multiple_license_files(license_data)
    
    if created_count == 0:
        return False, "无法创建授权文件"
    
    return True, f"授权成功（创建了{created_count}个保护文件）"


def create_trial_secure(hours: int = 72):
    """安全创建试用授权"""
    start_time = datetime.now()
    expiry_time = start_time + timedelta(hours=hours)
    
    license_data = {
        "type": "trial",
        "start_time": start_time.isoformat(),
        "expiry_time": expiry_time.isoformat(),
        "machine_code": get_machine_code_short(),
        "created_at": datetime.now().isoformat(),
        "trial_hours": hours,
    }
    
    created_count, file_id = create_multiple_license_files(license_data)
    
    if created_count == 0:
        return False, "无法创建试用授权"
    
    return True, f"已创建{hours}小时试用授权（{created_count}个保护文件）"


def check_license_secure():
    """安全检查授权状态"""
    is_valid, license_data, tampered_count = verify_all_license_files()
    
    if tampered_count > 0:
        # 检测到篡改行为
        print(f"[警告] 检测到{tampered_count}个授权文件被篡改")
    
    if not is_valid:
        if license_data is None:
            return False, "未找到授权文件"
        else:
            return False, "授权已过期"
    
    license_type = license_data.get("type", "")
    
    if license_type == "unlimited":
        return True, "永久授权"
    elif license_type == "trial":
        expiry_str = license_data.get("expiry_time")
        if expiry_str:
            try:
                expiry_time = datetime.fromisoformat(expiry_str)
                remaining = (expiry_time - datetime.now()).total_seconds()
                hours = int(remaining / 3600)
                return True, f"试用期剩余约{hours}小时"
            except:
                pass
        return True, "试用授权"
    else:
        return True, "正式授权"


def get_license_status_secure():
    """获取详细授权状态"""
    is_valid, license_data, tampered_count = verify_all_license_files()
    
    if license_data is None:
        return {
            "is_valid": False,
            "type": None,
            "remaining_hours": 0,
            "message": "无授权信息",
            "machine_code": get_machine_code_short(),
            "tampered_count": tampered_count,
            "file_count": 0,
        }
    
    is_valid_check, message = check_license_secure()
    
    status = {
        "is_valid": is_valid_check,
        "type": license_data.get("type"),
        "remaining_hours": 0,
        "message": message,
        "machine_code": license_data.get("machine_code", ""),
        "activated_at": license_data.get("activated_at"),
        "created_at": license_data.get("created_at"),
        "tampered_count": tampered_count,
        "file_count": len([p for p in get_all_license_paths() if p.exists()]),
    }
    
    if license_data.get("expiry_time"):
        try:
            expiry_time = datetime.fromisoformat(license_data["expiry_time"])
            remaining = (expiry_time - datetime.now()).total_seconds()
            status["remaining_hours"] = max(0, int(remaining / 3600))
        except:
            pass
    
    return status


def has_valid_license_secure():
    """检查是否有有效授权（简化版）"""
    is_valid, _, _ = verify_all_license_files()
    return is_valid


# 兼容旧版接口的包装函数
def get_license_file_path():
    """兼容旧接口 - 返回主授权文件路径"""
    base_dir = Path(os.path.dirname(os.path.abspath(__file__))).parent
    return base_dir / "app" / "license.dat"


def save_trial_info(expiry_time=None, trial_start=None):
    """兼容旧接口"""
    if expiry_time is None:
        expiry_time = datetime.now() + timedelta(hours=72)
    if trial_start is None:
        trial_start = datetime.now()
    
    license_data = {
        "type": "trial",
        "start_time": trial_start.isoformat(),
        "expiry_time": expiry_time.isoformat(),
        "machine_code": get_machine_code_short(),
        "created_at": datetime.now().isoformat(),
    }
    
    create_multiple_license_files(license_data)


def load_license():
    """兼容旧接口"""
    is_valid, license_data, _ = verify_all_license_files()
    return license_data


def check_trial():
    """兼容旧接口"""
    return check_license_secure()


def get_trial_status():
    """兼容旧接口"""
    return get_license_status_secure()


def create_trial_license(hours=72):
    """兼容旧接口"""
    return create_trial_secure(hours)


def activate_license(license_key):
    """兼容旧接口"""
    from license import validate_license_key
    
    machine_code = get_machine_code_short()
    is_valid, ltype, remaining = validate_license_key(license_key, machine_code)
    
    if not is_valid:
        return False, "授权码无效或不匹配本机"
    
    license_type = "unlimited" if ltype == "unlimited" else "permanent"
    return save_license_secure(license_key, license_type)


def has_valid_license():
    """兼容旧接口"""
    return has_valid_license_secure()


if __name__ == "__main__":
    print("=" * 60)
    print("增强版授权系统测试")
    print("=" * 60)
    
    print(f"\n机器码: {get_machine_code_short()}")
    
    # 测试创建试用授权
    print("\n[测试] 创建试用授权...")
    success, msg = create_trial_secure(72)
    print(f"结果: {msg}")
    
    # 测试验证
    print("\n[测试] 验证授权文件...")
    is_valid, data, tampered = verify_all_license_files()
    print(f"有效: {is_valid}, 篡改检测: {tampered}")
    
    if data:
        print(f"授权类型: {data.get('type')}")
        print(f"机器码: {data.get('machine_code')}")
    
    # 检查状态
    print("\n[测试] 授权状态:")
    status = get_license_status_secure()
    for key, value in status.items():
        print(f"  {key}: {value}")
