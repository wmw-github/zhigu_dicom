"""
图像安全读写工具 - 支持中文路径
OpenCV 的 cv2.imread()/imwrite() 在 Windows 上不支持非 ASCII 路径
"""
import os
import cv2
import numpy as np


def _is_ascii_path(filename):
    """检查路径是否全为 ASCII 字符"""
    try:
        str(filename).encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def imread_cn(filename, flags=cv2.IMREAD_COLOR):
    """
    支持中文路径的 cv2.imread 替代
    纯 ASCII 路径直接用 cv2.imread；非 ASCII 路径跳过 cv2.imread 避免 WARN
    """
    s = str(filename)
    if _is_ascii_path(s):
        return cv2.imread(s, flags)
    try:
        with open(s, 'rb') as f:
            data = f.read()
        return cv2.imdecode(np.frombuffer(data, np.uint8), flags)
    except Exception:
        return None


def imwrite_cn(filename, img, params=None):
    """
    支持中文路径的 cv2.imwrite 替代
    纯 ASCII 路径直接用 cv2.imwrite；非 ASCII 路径跳过避免 WARN
    """
    s = str(filename)
    if _is_ascii_path(s):
        return cv2.imwrite(s, img, params)
    try:
        ext = os.path.splitext(s)[1]
        if not ext:
            ext = '.png'
        success, buf = cv2.imencode(ext, img, params or [])
        if success:
            with open(s, 'wb') as f:
                f.write(buf.tobytes())
            return True
    except Exception:
        pass
    return False