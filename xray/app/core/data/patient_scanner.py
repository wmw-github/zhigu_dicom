"""
患者扫描器模块

扫描DICOM目录，解析XML文件，提取患者信息和DICOM文件路径
只保留X线检查(DX)，忽略CT和MRI
"""
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

try:
    import pydicom
except ImportError:
    pydicom = None

from utils.logger import get_logger

logger = get_logger(__name__)


class PatientInfo:
    """患者信息类"""
    
    def __init__(self):
        self.patient_id = ""           # 患者ID
        self.access_no = ""            # 检查号
        self.name = ""                 # 患者姓名
        self.sex = ""                  # 性别
        self.age = ""                  # 年龄
        self.study_uid = ""            # 检查UID
        self.study_date = ""           # 检查日期
        self.study_time = ""           # 检查时间
        self.modality = ""             # 检查类型 (DX=X线, CT=CT, MR=MRI)
        self.dicom_files = []          # DICOM文件路径列表
        self.xml_path = ""             # XML文件路径
        self.source_dir = ""           # 来源目录
    
    @property
    def is_xray(self) -> bool:
        """是否为X线检查"""
        return self.modality.upper() == "DX"
    
    @property
    def study_datetime(self) -> str:
        """格式化检查日期时间"""
        try:
            if len(self.study_date) == 8:
                date_str = f"{self.study_date[:4]}-{self.study_date[4:6]}-{self.study_date[6:]}"
            else:
                date_str = self.study_date
            
            if len(self.study_time) >= 6:
                time_str = f"{self.study_time[:2]}:{self.study_time[2:4]}:{self.study_time[4:6]}"
            else:
                time_str = self.study_time
            
            return f"{date_str} {time_str}"
        except:
            return f"{self.study_date} {self.study_time}"


class PatientScanner:
    """患者扫描器"""
    
    # 默认扫描目录
    DEFAULT_SCAN_DIRS = [
        r"D:\ClearTmp\DicomTmp",
        r"D:\Cache",
        r"E:\ClearTmp\DicomTmp",
        r"E:\Cache",
    ]
    
    def __init__(self, scan_dirs: Optional[List[str]] = None):
        """
        初始化扫描器
        
        Args:
            scan_dirs: 要扫描的目录列表，默认为DEFAULT_SCAN_DIRS
        """
        self.scan_dirs = scan_dirs or self.DEFAULT_SCAN_DIRS
        self.patients = []  # 患者列表
        
    def scan(self) -> List[PatientInfo]:
        """
        扫描所有目录，返回患者列表
        
        Returns:
            List[PatientInfo]: 患者信息列表（只包含X线检查）
        """
        self.patients = []
        
        for scan_dir in self.scan_dirs:
            if not os.path.exists(scan_dir):
                logger.warning(f"扫描目录不存在: {scan_dir}")
                continue
            
            try:
                self._scan_directory(scan_dir)
            except Exception as e:
                logger.error(f"扫描目录失败 {scan_dir}: {e}")
        
        # 按检查日期时间排序（最新的在前）
        self.patients.sort(key=lambda p: p.study_date + p.study_time, reverse=True)
        
        logger.info(f"扫描完成，共找到 {len(self.patients)} 个X线检查患者")
        return self.patients
    
    def set_scan_directory(self, directory: str):
        """
        设置单个扫描目录（替代默认目录列表）
        
        Args:
            directory: 要扫描的目录路径
        """
        if os.path.exists(directory):
            self.scan_dirs = [directory]
            logger.info(f"已设置扫描目录: {directory}")
        else:
            raise ValueError(f"目录不存在: {directory}")
    
    def reset_to_defaults(self):
        """重置为默认扫描目录"""
        self.scan_dirs = self.DEFAULT_SCAN_DIRS
        logger.info("已重置为默认扫描目录")
    
    def _scan_directory(self, directory: str):
        """扫描单个目录"""
        has_xml = False
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.lower().endswith('.xml'):
                    has_xml = True
                    xml_path = os.path.join(root, file)
                    try:
                        patient = self._parse_xml(xml_path)
                        if patient and patient.is_xray:
                            patient.source_dir = directory
                            self.patients.append(patient)
                            logger.debug(f"发现X线患者: {patient.name} ({patient.access_no})")
                        elif patient:
                            logger.debug(f"跳过非X线检查: {patient.name} ({patient.modality})")
                    except Exception as e:
                        logger.warning(f"解析XML失败 {xml_path}: {e}")
        
        # 如果没有XML文件，尝试按Cache目录结构扫描
        if not has_xml:
            self._scan_cache_directory(directory)
    
    def _parse_xml(self, xml_path: str) -> Optional[PatientInfo]:
        """
        解析XML文件
        
        Args:
            xml_path: XML文件路径
            
        Returns:
            PatientInfo: 患者信息对象，解析失败返回None
        """
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            
            patient = PatientInfo()
            patient.xml_path = xml_path
            
            # 解析患者信息
            pat_info = root.find('PatInfo')
            if pat_info is not None:
                patient.patient_id = self._get_text(pat_info, 'PatId')
                patient.access_no = self._get_text(pat_info, 'PatAccessNo')
                patient.name = self._get_text(pat_info, 'PatName')
                patient.sex = self._get_text(pat_info, 'PatSex')
                patient.age = self._get_text(pat_info, 'PatAge')
            
            # 解析检查信息
            study_info = root.find('StudyInfo')
            if study_info is not None:
                patient.study_uid = self._get_text(study_info, 'StudyUid')
                patient.study_date = self._get_text(study_info, 'StudyDate')
                patient.study_time = self._get_text(study_info, 'StudyTime')
            
            # 解析序列信息（获取检查类型和DICOM文件）
            series_info = root.find('SeriesInfo')
            if series_info is not None:
                series_item = series_info.find('SeriesItem')
                if series_item is not None:
                    patient.modality = self._get_text(series_item, 'Modality')
                    
                    # 获取DICOM文件列表
                    xml_dir = os.path.dirname(xml_path)
                    for series_img in series_item.findall('SeriesImg'):
                        dicom_uid = series_img.text
                        if dicom_uid:
                            # 在XML所在目录查找DICOM文件
                            dicom_file = self._find_dicom_file(xml_dir, dicom_uid)
                            if dicom_file:
                                patient.dicom_files.append(dicom_file)
            
            return patient
            
        except ET.ParseError as e:
            logger.warning(f"XML解析错误 {xml_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"解析XML异常 {xml_path}: {e}")
            return None
    
    def _get_text(self, element, tag: str, default: str = "") -> str:
        """安全获取XML元素文本"""
        try:
            child = element.find(tag)
            return child.text.strip() if child is not None and child.text else default
        except:
            return default
    
    def _find_dicom_file(self, directory: str, uid: str) -> Optional[str]:
        """
        在目录中查找DICOM文件
        
        Args:
            directory: 搜索目录
            uid: DICOM文件UID
            
        Returns:
            str: DICOM文件完整路径，未找到返回None
        """
        # 常见的DICOM文件扩展名
        dicom_extensions = ['.dcm', '.dicom', '']
        
        # 首先尝试直接匹配文件名
        for ext in dicom_extensions:
            file_path = os.path.join(directory, uid + ext)
            if os.path.exists(file_path):
                return file_path
        
        # 遍历目录查找包含UID的文件
        try:
            for filename in os.listdir(directory):
                if uid in filename:
                    file_path = os.path.join(directory, filename)
                    if os.path.isfile(file_path):
                        return file_path
        except:
            pass
        
        return None
    
    def _scan_cache_directory(self, directory: str):
        """
        扫描Cache目录结构
        
        Cache目录结构: {cache_dir}\{编号}\{日期}\{检查号}_{模态}\*.dcm
        例如: D:\Cache\1\20260204\DX53650_DX\
        """
        logger.info(f"尝试按Cache目录结构扫描: {directory}")
        
        # 遍历第一层：编号目录
        try:
            for item1 in os.listdir(directory):
                path1 = os.path.join(directory, item1)
                if not os.path.isdir(path1):
                    continue
                
                # 遍历第二层：日期目录
                for item2 in os.listdir(path1):
                    path2 = os.path.join(path1, item2)
                    if not os.path.isdir(path2):
                        continue
                    
                    # 检查是否为日期格式（8位数字）
                    if not (len(item2) == 8 and item2.isdigit()):
                        continue
                    
                    study_date = item2  # 例如: 20260204
                    
                    # 遍历第三层：检查目录
                    for item3 in os.listdir(path2):
                        path3 = os.path.join(path2, item3)
                        if not os.path.isdir(path3):
                            continue
                        
                        # 解析检查号和模态，格式: {检查号}_{模态}
                        # 例如: DX53650_DX
                        if '_' not in item3:
                            continue
                        
                        parts = item3.rsplit('_', 1)
                        if len(parts) != 2:
                            continue
                        
                        access_no = parts[0]  # 例如: DX53650
                        modality = parts[1]   # 例如: DX
                        
                        # 只处理X线检查
                        if modality.upper() != "DX":
                            logger.debug(f"跳过非X线检查: {item3}")
                            continue
                        
                        # 收集DICOM文件
                        dicom_files = []
                        for file in os.listdir(path3):
                            if file.lower().endswith('.dcm'):
                                dicom_files.append(os.path.join(path3, file))
                        
                        if not dicom_files:
                            continue
                        
                        # 从第一个DICOM文件读取患者信息
                        patient = self._read_dicom_info(dicom_files[0])
                        if patient is None:
                            patient = PatientInfo()
                        
                        # 设置从目录结构获取的信息
                        patient.patient_id = item1  # 使用编号作为患者ID
                        patient.access_no = access_no
                        patient.study_date = study_date
                        patient.modality = modality
                        patient.dicom_files = sorted(dicom_files)
                        patient.source_dir = directory  # 记录来源目录
                        
                        # 如果DICOM中没有姓名，使用检查号作为姓名
                        if not patient.name:
                            patient.name = access_no
                        
                        self.patients.append(patient)
                        logger.debug(f"发现X线患者: {patient.name} ({patient.access_no})")
                        
        except Exception as e:
            logger.error(f"扫描Cache目录失败 {directory}: {e}")
    
    def _read_dicom_info(self, dicom_path: str) -> Optional[PatientInfo]:
        """
        从DICOM文件头读取患者信息
        
        Args:
            dicom_path: DICOM文件路径
            
        Returns:
            PatientInfo: 患者信息对象，读取失败返回None
        """
        if pydicom is None:
            logger.warning("pydicom未安装，无法读取DICOM文件头")
            return None
        
        try:
            # 只读取文件头，不读取像素数据
            # 强制使用GBK编码处理中文字符
            ds = pydicom.dcmread(dicom_path, stop_before_pixels=True, force=True)
            
            # 尝试设置正确的字符集
            if hasattr(ds, 'SpecificCharacterSet'):
                if 'GBK' not in str(ds.SpecificCharacterSet) and 'GB2312' not in str(ds.SpecificCharacterSet):
                    ds.SpecificCharacterSet = 'GBK'
            else:
                ds.SpecificCharacterSet = 'GBK'
            
            patient = PatientInfo()
            
            # 辅助函数：解码字符串
            def decode_str(value):
                if value is None:
                    return ""
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
                    return str(value) if value else ""
            
            # 读取患者信息
            # (0010,0010) Patient's Name
            if hasattr(ds, 'PatientName'):
                patient.name = decode_str(ds.PatientName)
            
            # (0010,0020) Patient ID
            if hasattr(ds, 'PatientID'):
                patient.patient_id = decode_str(ds.PatientID)
            
            # (0010,0040) Patient's Sex
            if hasattr(ds, 'PatientSex'):
                patient.sex = decode_str(ds.PatientSex)
            
            # (0010,1010) Patient's Age
            if hasattr(ds, 'PatientAge'):
                age_str = decode_str(ds.PatientAge)
                # DICOM年龄格式: nnnD(天), nnnW(周), nnnM(月), nnnY(年)
                if age_str.endswith('Y') and age_str[:-1].isdigit():
                    patient.age = age_str[:-1]
                elif age_str.isdigit():
                    patient.age = age_str
                else:
                    patient.age = age_str
            
            # (0008,0020) Study Date
            if hasattr(ds, 'StudyDate'):
                patient.study_date = decode_str(ds.StudyDate)
            
            # (0008,0030) Study Time
            if hasattr(ds, 'StudyTime'):
                patient.study_time = decode_str(ds.StudyTime)
            
            # (0020,000D) Study Instance UID
            if hasattr(ds, 'StudyInstanceUID'):
                patient.study_uid = decode_str(ds.StudyInstanceUID)
            
            # (0008,0060) Modality
            if hasattr(ds, 'Modality'):
                patient.modality = decode_str(ds.Modality)
            
            # (0008,0050) Accession Number
            if hasattr(ds, 'AccessionNumber'):
                patient.access_no = decode_str(ds.AccessionNumber)
            
            return patient
            
        except Exception as e:
            logger.warning(f"读取DICOM文件头失败 {dicom_path}: {e}")
            return None
    
    def get_patient_by_access_no(self, access_no: str) -> Optional[PatientInfo]:
        """根据检查号获取患者信息"""
        for patient in self.patients:
            if patient.access_no == access_no:
                return patient
        return None
    
    def get_patient_by_study_uid(self, study_uid: str) -> Optional[PatientInfo]:
        """根据检查UID获取患者信息"""
        for patient in self.patients:
            if patient.study_uid == study_uid:
                return patient
        return None
    
    def refresh(self) -> List[PatientInfo]:
        """刷新患者列表"""
        return self.scan()


# 全局扫描器实例
patient_scanner = PatientScanner()
