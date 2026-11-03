"""
配置模块 - 全局配置中心
"""
from .settings import (
    TRAIN_CONFIG,
    INFERENCE_CONFIG,
    CLASS_CONFIG,
    SUPPORTED_FORMATS,
    AUGMENTATION_CONFIG,
)
from .paths import PathManager

__all__ = [
    'TRAIN_CONFIG',
    'INFERENCE_CONFIG',
    'CLASS_CONFIG',
    'SUPPORTED_FORMATS',
    'AUGMENTATION_CONFIG',
    'PathManager',
]
