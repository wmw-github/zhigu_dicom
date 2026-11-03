"""
部位自动检测模块

根据DICOM文件的BodyPartExamined字段自动判断检测类型
支持英文和中文（含GBK编码乱码修复）的部位名称
"""
import pydicom
from pathlib import Path
from typing import Optional
from utils.logger import get_logger

logger = get_logger(__name__)


class BodyPartDetector:
    """部位检测器 - 自动判断应该执行的检测类型"""
    
    # 检测类型常量
    DETECTION_CHEST = "chest_xray"
    DETECTION_FRACTURE = "fracture"
    DETECTION_SPINE = "spine"
    DETECTION_BONEAGE = "bone_age"
    DETECTION_NONE = None
    
    # BodyPart映射表 (全部使用大写英文或中文)
    # 胸片检测
    CHEST_PARTS = {
        "CHEST", "CHESTPA", "CHESTAP", "CHESTLATERAL",
        "胸部",
    }
    
    # 骨折检测（四肢、骨盆等小关节）
    FRACTURE_PARTS = {
        "HAND", "FINGER", "ARM", "ELBOW", "SHOULDER",
        "LEG", "KNEE", "ANKLE", "FOOT", "TOE", "HIP",
        "PELVIS", "FEMUR", "TIBIA", "FIBULA", "HUMERUS", "RADIUS", "ULNA",
        "手", "手指", "前臂", "肘", "肩", "小腿", "膝", "踝", "足", "趾", "髋", 
        "股骨", "胫骨", "腓骨", "肱骨", "桡骨", "尺骨", "骨盆"
    }
    
    # 脊柱检测
    SPINE_PARTS = {
        "TSPINE", "LSPINE", "CSPINE", "SPINE", "WHOLESPINE",
        "THORACIC", "LUMBAR", "CERVICAL",
        "颈椎", "胸椎", "腰椎", "脊柱", "胸腰段"
    }
    
    # 骨龄检测
    BONEAGE_PARTS = {
        "WRIST", "手腕"
    }
    
    @classmethod
    def detect_from_dicom(cls, dicom_path: str) -> Optional[str]:
        """
        从DICOM文件自动检测应该执行的检测类型
        
        Args:
            dicom_path: DICOM文件路径
            
        Returns:
            检测类型字符串，无法判断返回None
        """
        try:
            ds = pydicom.dcmread(dicom_path, stop_before_pixels=True)
            
            # 获取BodyPartExamined字段
            body_part = getattr(ds, 'BodyPartExamined', None)
            if body_part:
                # 解码BodyPart，处理可能的GBK乱码
                body_part_str = cls._decode_body_part(body_part)
                logger.info(f"检测到BodyPartExamined: {body_part_str}")
                
                # 判断检测类型
                detection_type = cls._classify_body_part(body_part_str)
                
                if detection_type:
                    logger.info(f"自动判断检测类型: {detection_type}")
                else:
                    logger.info(f"无法判断检测类型，BodyPart: {body_part_str}")
                
                return detection_type
            
            # 如果没有BodyPartExamined字段，尝试从其他信息推断
            logger.warning("DICOM文件中未找到BodyPartExamined字段")
            return cls._infer_from_other_info(ds)
            
        except Exception as e:
            logger.error(f"读取DICOM文件失败: {e}")
            return None
    
    @classmethod
    def _decode_body_part(cls, body_part) -> str:
        """
        解码BodyPartExamined字段，处理GBK编码乱码
        
        某些设备写入的中文部位会被pydicom错误解码为Latin-1，
        例如 'Ñü×µ' 实际上是 '腰椎' 的GBK字节被当作Latin-1解码。
        """
        body_part_str = str(body_part).strip()
        
        # 尝试修复乱码的中文 (Latin-1 -> GBK)
        try:
            cn_str = body_part_str.encode('latin1').decode('gbk')
            # 如果解码成功且包含中文字符，返回中文
            if any('\u4e00' <= c <= '\u9fff' for c in cn_str):
                return cn_str
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        
        # 尝试修复乱码的中文 (Latin-1 -> UTF-8)
        try:
            cn_str = body_part_str.encode('latin1').decode('utf-8')
            if any('\u4e00' <= c <= '\u9fff' for c in cn_str):
                return cn_str
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        
        # 返回原始字符串的大写形式
        return body_part_str.upper()
    
    @classmethod
    def _classify_body_part(cls, body_part: str) -> Optional[str]:
        """
        根据BodyPart分类检测类型
        
        优先级：胸片 > 骨折 > 脊柱 > 骨龄
        注意：骨龄和脊柱能识别，但暂不自动触发检测
        """
        # 统一转大写用于英文匹配，保留原文用于中文匹配
        body_part_upper = body_part.upper()
        
        # 1. 胸片检测（最高优先级）
        if body_part_upper in cls.CHEST_PARTS or body_part in cls.CHEST_PARTS:
            return cls.DETECTION_CHEST
        
        # 2. 骨折检测（四肢小关节）
        # 注意：WRIST(手腕) 优先归为骨龄，如果不自动骨龄则归为骨折
        if body_part_upper in cls.FRACTURE_PARTS or body_part in cls.FRACTURE_PARTS:
            return cls.DETECTION_FRACTURE
        
        # 3. 脊柱检测（能识别，暂不自动触发）
        if body_part_upper in cls.SPINE_PARTS or body_part in cls.SPINE_PARTS:
            return cls.DETECTION_SPINE
        
        # 4. 骨龄检测（能识别，暂不自动触发）
        if body_part_upper in cls.BONEAGE_PARTS or body_part in cls.BONEAGE_PARTS:
            return cls.DETECTION_BONEAGE
        
        # 无法判断
        return cls.DETECTION_NONE
    
    @classmethod
    def _infer_from_other_info(cls, ds) -> Optional[str]:
        """
        从其他DICOM信息推断检测类型（备用方案）
        
        目前暂不实现，返回None
        """
        # 可以尝试从以下信息推断：
        # - Modality (CR, DR, CT, MR)
        # - StudyDescription
        # - SeriesDescription
        # - ProtocolName
        
        # 目前暂不实现自动推断
        return cls.DETECTION_NONE
    
    @classmethod
    def get_detection_name(cls, detection_type: Optional[str]) -> str:
        """获取检测类型的中文名称"""
        names = {
            cls.DETECTION_CHEST: "胸片检测",
            cls.DETECTION_FRACTURE: "骨折检测",
            cls.DETECTION_SPINE: "脊柱检测",
            cls.DETECTION_BONEAGE: "骨龄检测",
        }
        return names.get(detection_type, "无法判断")
