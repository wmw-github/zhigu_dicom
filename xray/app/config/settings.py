"""
全局配置参数
"""
import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 训练配置 (V100优化) - 重新训练，使用YOLOv8s，优化参数防止过拟合
TRAIN_CONFIG = {
    "model_type": "yolov8s",                    # 改为YOLOv8s，模型更小，防止过拟合
    "pretrained_path": str(PROJECT_ROOT / "models" / "pretrained" / "yolov8s.pt"),
    "epochs": 200,                              # 增加训练轮数
    "batch_size": 32,                           # V100 16GB优化
    "image_size": 640,
    "device": "0",                              # V100显卡
    "workers": 8,
    "amp": True,                                # 混合精度
    "cache": False,                             # 禁用缓存，避免内存溢出
    "optimizer": "AdamW",
    "lr0": 0.001,                               # 初始学习率
    "lrf": 0.01,                                # 最终学习率因子
    "momentum": 0.937,
    "weight_decay": 0.001,                      # 增加权重衰减，防止过拟合
    "warmup_epochs": 5,                         # 增加warmup
    "patience": 30,                             # 增加早停耐心值
    "save_period": 10,
    # 数据增强参数 - 增强泛化能力
    "hsv_h": 0.015,                             # HSV色调增强
    "hsv_s": 0.7,                               # HSV饱和度增强
    "hsv_v": 0.4,                               # HSV亮度增强
    "degrees": 10,                              # 旋转角度限制（减小）
    "translate": 0.1,                           # 平移
    "scale": 0.3,                               # 缩放（减小）
    "shear": 2,                                 # 剪切
    "flipud": 0.0,                              # 上下翻转
    "fliplr": 0.5,                              # 左右翻转
    "mosaic": 0.5,                              # 马赛克增强（降低概率）
    "mixup": 0.1,                               # MixUp增强
    "copy_paste": 0.0,                          # 复制粘贴（禁用，避免框变大）
    "erasing": 0.1,                             # 随机擦除
    "auto_augment": "randaugment",              # 自动增强
}

# 推理配置 (CPU)
INFERENCE_CONFIG = {
    "model_path": str(PROJECT_ROOT / "models" / "trained" / "best.onnx"),
    "device": "cpu",                            # 默认CPU
    "input_size": 640,
    "conf_thresh": 0.25,
    "nms_thresh": 0.45,
}

# 类别配置 - 官方VinDr-CXR 14类定义
CLASS_CONFIG = {
    "num_classes": 14,
    "class_names": [
        "Aortic enlargement",   # 0: 主动脉增大
        "Atelectasis",          # 1: 肺不张
        "Calcification",        # 2: 钙化
        "Cardiomegaly",         # 3: 心脏肥大
        "Consolidation",        # 4: 实变
        "ILD",                  # 5: 间质性肺疾病
        "Infiltration",         # 6: 浸润
        "Lung Opacity",         # 7: 肺不透明
        "Nodule/Mass",          # 8: 结节/肿块
        "Other lesion",         # 9: 其他病变
        "Pleural effusion",     # 10: 胸腔积液
        "Pleural thickening",   # 11: 胸膜增厚
        "Pneumothorax",         # 12: 气胸
        "Pulmonary fibrosis",   # 13: 肺纤维化
    ],
    # 类别中文名称
    "class_names_cn": [
        "主动脉增大", "肺不张", "钙化", "心脏肥大",
        "实变", "间质性肺疾病", "浸润", "肺不透明",
        "结节/肿块", "其他病变", "胸腔积液", "胸膜增厚",
        "气胸", "肺纤维化"
    ],
}

# 支持的图像格式
SUPPORTED_FORMATS = {
    "dicom": [".dcm", ".dicom"],
    "standard": [".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"],
}

# 数据增强配置
AUGMENTATION_CONFIG = {
    "rare_classes": [1, 4, 5, 12],              # 少数类ID
    "augment_factor": {
        1: 5,                                    # Cardiomegaly增强5倍
        4: 4,                                    # Emphysema增强4倍
        5: 3,                                    # Pleural Effusion增强3倍
        12: 8                                    # Pleural Thickening增强8倍
    },
    "transforms": {
        "random_rotate": {"limit": 15, "p": 0.5},
        "random_scale": {"scale_limit": 0.2, "p": 0.5},
        "random_shift": {"shift_limit": 0.1, "p": 0.5},
        "horizontal_flip": {"p": 0.5},
        "random_brightness": {"limit": 0.2, "p": 0.5},
        "random_contrast": {"limit": 0.2, "p": 0.5},
        "gauss_noise": {"var_limit": (10, 50), "p": 0.3},
        "clahe": {"clip_limit": 4.0, "p": 0.3},
        "random_gamma": {"gamma_limit": (80, 120), "p": 0.3},
    }
}

# GUI配置
GUI_CONFIG = {
    "window_title": "X线辅助诊断系统 - 仅供学习交流使用，不做诊断证明",
    "window_size": (1600, 1000),                # 增大窗口尺寸
    "image_min_size": (900, 700),               # 增大图像显示区域
    "theme": "dark",
}

# 日志配置
LOG_CONFIG = {
    "level": "INFO",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": str(PROJECT_ROOT / "logs" / "app.log"),
}
