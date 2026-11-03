"""
路径管理模块
"""
from pathlib import Path
from typing import Union


class PathManager:
    """路径管理器 - 统一管理项目路径"""
    
    def __init__(self, project_root: Union[str, Path] = None):
        if project_root is None:
            self.project_root = Path(__file__).parent.parent
        else:
            self.project_root = Path(project_root)
    
    @property
    def data_dir(self) -> Path:
        """数据目录"""
        return self.project_root / "data"
    
    @property
    def images_train_dir(self) -> Path:
        """训练图像目录"""
        return self.data_dir / "images" / "train"
    
    @property
    def images_val_dir(self) -> Path:
        """验证图像目录"""
        return self.data_dir / "images" / "val"
    
    @property
    def labels_train_dir(self) -> Path:
        """训练标签目录"""
        return self.data_dir / "labels" / "train"
    
    @property
    def labels_val_dir(self) -> Path:
        """验证标签目录"""
        return self.data_dir / "labels" / "val"
    
    @property
    def models_dir(self) -> Path:
        """模型目录"""
        return self.project_root / "models"
    
    @property
    def pretrained_dir(self) -> Path:
        """预训练模型目录"""
        return self.models_dir / "pretrained"
    
    @property
    def trained_dir(self) -> Path:
        """训练好的模型目录"""
        return self.models_dir / "trained"
    
    @property
    def training_results_dir(self) -> Path:
        """训练结果目录"""
        return self.models_dir / "training_results"
    
    @property
    def logs_dir(self) -> Path:
        """日志目录"""
        return self.project_root / "logs"
    
    @property
    def docs_dir(self) -> Path:
        """文档目录"""
        return self.project_root / "docs"
    
    @property
    def dataset_yaml(self) -> Path:
        """数据集配置文件"""
        return self.data_dir / "dataset.yaml"
    
    def ensure_dirs(self):
        """确保所有目录存在"""
        dirs = [
            self.data_dir,
            self.images_train_dir,
            self.images_val_dir,
            self.labels_train_dir,
            self.labels_val_dir,
            self.models_dir,
            self.pretrained_dir,
            self.trained_dir,
            self.training_results_dir,
            self.logs_dir,
            self.docs_dir,
        ]
        for dir_path in dirs:
            dir_path.mkdir(parents=True, exist_ok=True)
        return self


# 全局路径管理器实例
path_manager = PathManager()
