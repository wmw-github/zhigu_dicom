import os
import json
import hashlib
import time
from datetime import datetime, timedelta


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
    machine_code = get_machine_code()
    return machine_code[:16].upper()

def shuffle_license(data):
    """混淆授权码（字符交换）"""
    if len(data) < 4:
        return data
    chars = list(data)
    n = len(chars)
    for i in range(0, n - 1, 2):
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return ''.join(chars)

def unshuffle_license(shuffled):
    """解混淆授权码"""
    if not shuffled or len(shuffled) < 4:
        return shuffled
    chars = list(shuffled)
    n = len(chars)
    for i in range(0, n - 1, 2):
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return ''.join(chars)

def generate_license_key(machine_code, hours=0):
    """生成授权码"""
    machine_code = machine_code.upper()
    if hours == 0:
        raw = "UNLIMITED-" + machine_code[:16]
    else:
        hours_hex = format(hours, '08X')
        raw = f"{hours_hex}H{machine_code[:12]}"
    return shuffle_license(raw)

def validate_license_key(license_key, machine_code):
    """验证授权码是否有效"""
    try:
        unshuffled = unshuffle_license(license_key.strip().upper())
        
        if unshuffled.startswith("UNLIMITED-"):
            key_machine = unshuffled[10:26].upper()
            return key_machine == machine_code.upper()[:16], "unlimited", None
        
        elif "H" in unshuffled:
            parts = unshuffled.split("H")
            if len(parts) == 2:
                hours_hex = parts[0]
                key_machine = parts[1][:12].upper()
                
                try:
                    hours = int(hours_hex, 16)
                    return key_machine == machine_code.upper()[:12], "trial", hours * 3600
                except ValueError:
                    return False, None, None
        
        return False, None, None
        
    except Exception as e:
        print(f"[ERROR] 验证授权码失败: {e}")
        return False, None, None

def get_license_file_path():
    """获取授权文件路径"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    possible_paths = [
        os.path.join(base_dir, "license.dat"),
        os.path.join(os.path.dirname(base_dir), "data", "license.dat"),
        os.path.join(base_dir, "sys", "license.dat"),
        os.path.join(base_dir, "config", "license.dat"),
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            return path
    
    # 如果都不存在，创建默认位置
    default_path = os.path.join(base_dir, "license.dat")
    os.makedirs(os.path.dirname(default_path), exist_ok=True)
    return default_path

def save_trial_info(expiry_time=None, trial_start=None):
    """保存试用信息"""
    license_file = get_license_file_path()
    
    if expiry_time is None:
        expiry_time = datetime.now() + timedelta(hours=72)
    
    if trial_start is None:
        trial_start = datetime.now()
    
    data = {
        "type": "trial",
        "start_time": trial_start.isoformat(),
        "expiry_time": expiry_time.isoformat(),
        "machine_code": get_machine_code_short(),
        "created_at": datetime.now().isoformat()
    }
    
    with open(license_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_license(license_key, license_type="permanent"):
    """保存正式授权"""
    license_file = get_license_file_path()
    
    is_valid, ltype, remaining = validate_license_key(license_key, get_machine_code_short())
    
    if not is_valid:
        return False, "无效的授权码"
    
    data = {
        "type": license_type,
        "license_key": license_key,
        "machine_code": get_machine_code_short(),
        "activated_at": datetime.now().isoformat(),
        "expiry_time": None if ltype == "unlimited" else (datetime.now() + timedelta(seconds=remaining)).isoformat()
    }
    
    with open(license_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    return True, "授权成功"

def load_license():
    """加载授权信息"""
    license_file = get_license_file_path()
    
    if not os.path.exists(license_file):
        return None
    
    try:
        with open(license_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] 加载授权文件失败: {e}")
        return None

def check_trial():
    """检查试用状态"""
    license_data = load_license()
    
    if license_data is None:
        return False, "未找到授权文件"
    
    license_type = license_data.get("type", "")
    
    if license_type == "unlimited":
        return True, "永久授权"
    
    elif license_type == "permanent":
        expiry_str = license_data.get("expiry_time")
        if expiry_str:
            try:
                expiry_time = datetime.fromisoformat(expiry_str)
                if datetime.now() < expiry_time:
                    remaining = (expiry_time - datetime.now()).total_seconds()
                    return True, f"正式授权，剩余{int(remaining/3600)}小时"
                else:
                    return False, "授权已过期"
            except:
                pass
        return True, "正式授权"
    
    elif license_type == "trial":
        expiry_str = license_data.get("expiry_time")
        if expiry_str:
            try:
                expiry_time = datetime.fromisoformat(expiry_str)
                now = datetime.now()
                
                if now >= expiry_time:
                    return False, "试用期已结束"
                
                remaining = (expiry_time - now).total_seconds()
                
                if remaining <= 0:
                    return False, "试用期已结束"
                
                hours = int(remaining / 3600)
                
                if hours >= 999999:
                    return True, "永久试用"
                
                return True, f"试用期剩余约{hours}小时"
            except Exception as e:
                print(f"[ERROR] 解析试用时间失败: {e}")
                return False, "试用时间解析错误"
        
        return False, "无效的试用授权"
    
    return False, "未知授权类型"

def get_trial_status():
    """获取详细试用状态"""
    license_data = load_license()
    
    if license_data is None:
        return {
            "is_valid": False,
            "type": None,
            "remaining_hours": 0,
            "message": "无授权信息",
            "machine_code": get_machine_code_short()
        }
    
    is_valid, message = check_trial()
    
    status = {
        "is_valid": is_valid,
        "type": license_data.get("type"),
        "remaining_hours": 0,
        "message": message,
        "machine_code": license_data.get("machine_code", ""),
        "activated_at": license_data.get("activated_at"),
        "created_at": license_data.get("created_at")
    }
    
    if license_data.get("expiry_time"):
        try:
            expiry_time = datetime.fromisoformat(license_data["expiry_time"])
            remaining = (expiry_time - datetime.now()).total_seconds()
            status["remaining_hours"] = max(0, int(remaining / 3600))
        except:
            pass
    
    return status

def create_trial_license(hours=72):
    """创建试用授权"""
    try:
        start_time = datetime.now()
        expiry_time = start_time + timedelta(hours=hours)
        
        save_trial_info(
            expiry_time=expiry_time,
            trial_start=start_time
        )
        
        return True, f"已创建{hours}小时试用授权"
    
    except Exception as e:
        return False, f"创建试用授权失败: {e}"

def activate_license(license_key):
    """激活授权"""
    machine_code = get_machine_code_short()
    is_valid, ltype, remaining = validate_license_key(license_key, machine_code)
    
    if not is_valid:
        return False, "授权码无效或不匹配本机"
    
    if ltype == "unlimited":
        success, msg = save_license(license_key, "unlimited")
    else:
        success, msg = save_license(license_key, "permanent")
    
    if success:
        return True, "激活成功"
    else:
        return False, msg

def has_valid_license():
    """检查是否有有效授权（简化版）"""
    is_valid, _ = check_trial()
    return is_valid

if __name__ == "__main__":
    print("="*50)
    print("License Module Test")
    print("="*50)
    
    print(f"\nMachine Code: {get_machine_code_short()}")
    print(f"\nLicense Check: {check_trial()}")
