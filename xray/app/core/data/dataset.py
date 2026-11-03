"""
数据集类
"""
from pathlib import Path
from typing import Dict, List

from config.paths import path_manager


class ChestXrayDataset:
    """胸部X光数据集管理"""
    
    def __init__(self, data_yaml: str = None):
        """
        初始化数据集
        
        Args:
            data_yaml: 数据集配置文件路径
        """
        self.data_yaml = data_yaml or str(path_manager.dataset_yaml)
        self.config = self._load_config()
    
    def _load_config(self) -> Dict:
        """加载数据集配置"""
        # 默认配置
        return {
            'path': str(path_manager.data_dir),
            'train': 'images/train',
            'val': 'images/val',
            'nc': 14,
            'names': [
                'Aortic enlargement', 'Atelectasis', 'Calcification', 'Cardiomegaly',
                'Consolidation', 'ILD', 'Infiltration', 'Lung Opacity',
                'Nodule/Mass', 'Other lesion', 'Pleural effusion', 'Pleural thickening',
                'Pneumothorax', 'Pulmonary fibrosis'
            ]
        }
    
    def get_class_names(self) -> List[str]:
        """获取类别名称列表"""
        return self.config.get('names', [])
    
    def get_num_classes(self) -> int:
        """获取类别数量"""
        return self.config.get('nc', 14)
