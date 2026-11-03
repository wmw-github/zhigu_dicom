"""
DICOM文件加载模块
"""
import numpy as np
from pathlib import Path
from typing import Tuple, Optional


class DicomLoader:
    """DICOM文件加载器"""
    
    def load(self, dicom_path: Path) -> Tuple[np.ndarray, dict]:
        """
        加载DICOM文件并转换为标准图像格式
        
        Args:
            dicom_path: DICOM文件路径
        
        Returns:
            Tuple: (RGB格式图像数组, 元数据)
        """
        try:
            import pydicom
            from pydicom.pixel_data_handlers.util import apply_voi_lut
        except ImportError:
            raise ImportError("请安装pydicom: pip install pydicom")
        
        ds = pydicom.dcmread(str(dicom_path))
        pixel_array = ds.pixel_array
        
        # 应用VOI LUT (窗宽窗位)
        if 'WindowCenter' in ds and 'WindowWidth' in ds:
            try:
                pixel_array = apply_voi_lut(pixel_array, ds)
            except Exception as _e:
                # 原来这里裸 except: pass —— 窗值没生效却毫无痕迹，
                # 后续归一化会照常出图，表现为「片子发灰/发黑但查不出原因」。
                # 保留「失败也继续」的行为（有兜底），但必须留下可追查的记录。
                print("[WARN] VOI LUT 应用失败，改用原始像素：%s: %s"
                      % (type(_e).__name__, _e))
        
        # 处理PhotometricInterpretation
        # MONOCHROME1: 像素值越大越暗（负片），需要反转
        # MONOCHROME2: 像素值越大越亮（正常）
        photometric = getattr(ds, 'PhotometricInterpretation', 'MONOCHROME2')
        if photometric == 'MONOCHROME1':
            pixel_array = pixel_array.max() - pixel_array
        
        # 归一化到0-255
        pixel_array = self._normalize_to_uint8(pixel_array)
        
        # 转RGB
        if len(pixel_array.shape) == 2:
            image = np.stack([pixel_array] * 3, axis=-1)
        elif len(pixel_array.shape) == 3 and pixel_array.shape[2] == 1:
            image = np.repeat(pixel_array, 3, axis=-1)
        else:
            image = pixel_array
        
        # 辅助函数：解码中文字符串
        def decode_str(value):
            if value is None:
                return "Unknown"
            try:
                s = str(value)
                # 尝试GBK解码
                try:
                    return s.encode('latin1').decode('gbk')
                except:
                    try:
                        return s.encode('latin1').decode('utf-8')
                    except:
                        return s
            except:
                return str(value) if value else "Unknown"
        
        # 提取元数据
        metadata = {
            "patient_id": decode_str(getattr(ds, "PatientID", "Unknown")),
            "patient_name": decode_str(getattr(ds, "PatientName", "Unknown")),
            "patient_sex": decode_str(getattr(ds, "PatientSex", "Unknown")),  # M=男, F=女
            "study_date": decode_str(getattr(ds, "StudyDate", "Unknown")),
            "modality": decode_str(getattr(ds, "Modality", "Unknown")),
            "body_part": decode_str(getattr(ds, "BodyPartExamined", "Unknown")),
            "window_center": getattr(ds, "WindowCenter", None),
            "window_width": getattr(ds, "WindowWidth", None),
        }
        
        return image, metadata
    
    def _normalize_to_uint8(self, array: np.ndarray) -> np.ndarray:
        """归一化到uint8"""
        array = array.astype(np.float32)
        array_min = array.min()
        array_max = array.max()
        
        if array_max > array_min:
            array = (array - array_min) / (array_max - array_min)
        else:
            array = np.zeros_like(array)
        
        return (array * 255).astype(np.uint8)
