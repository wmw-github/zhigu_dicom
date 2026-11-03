# 骨龄检测模型技术文档

## 一、模型概述

本目录包含基于YOLOv8的骨龄检测模型，采用RUS-CHN法进行骨龄评估。

### 模型组成
- **检测模型**: 1个 (YOLOv8n)
- **分类模型**: 9个 (YOLOv8s)
- **总模型数**: 10个

---

## 二、模型架构

### 2.1 检测模型
| 属性 | 值 |
|------|-----|
| 模型名称 | yolov8n_detect_bone_age |
| 基础架构 | YOLOv8n (nano) |
| 输入尺寸 | 640×640 |
| 检测类别 | 7类 |
| 模型格式 | .pt (22.5MB), .onnx (11.8MB) |

#### 检测类别说明
| 类别ID | 类别名称 | 说明 |
|--------|----------|------|
| 0 | Radius | 桡骨 |
| 1 | Ulna | 尺骨 |
| 2 | MCPFirst | 第一掌骨 |
| 3 | MCP | 掌骨（第三、五） |
| 4 | ProximalPhalanx | 近节指骨 |
| 5 | MiddlePhalanx | 中节指骨 |
| 6 | DistalPhalanx | 远节指骨 |

### 2.2 分类模型
| 模型名称 | 基础架构 | 输入尺寸 | 分类等级 | 模型大小 |
|----------|----------|----------|----------|----------|
| yolov8s_cls_DIP | YOLOv8s | 224×224 | 11级 | 41.2MB (.pt) / 19.5MB (.onnx) |
| yolov8s_cls_DIPFirst | YOLOv8s | 224×224 | 11级 | 41.2MB (.pt) / 19.5MB (.onnx) |
| yolov8s_cls_MCP | YOLOv8s | 224×224 | 10级 | 41.2MB (.pt) / 19.5MB (.onnx) |
| yolov8s_cls_MCPFirst | YOLOv8s | 224×224 | 11级 | 41.2MB (.pt) / 19.5MB (.onnx) |
| yolov8s_cls_MIP | YOLOv8s | 224×224 | 12级 | 41.2MB (.pt) / 19.5MB (.onnx) |
| yolov8s_cls_PIP | YOLOv8s | 224×224 | 12级 | 41.2MB (.pt) / 19.5MB (.onnx) |
| yolov8s_cls_PIPFirst | YOLOv8s | 224×224 | 12级 | 41.2MB (.pt) / 19.5MB (.onnx) |
| yolov8s_cls_Radius | YOLOv8s | 224×224 | 14级 | 41.2MB (.pt) / 19.5MB (.onnx) |
| yolov8s_cls_Ulna | YOLOv8s | 224×224 | 12级 | 41.2MB (.pt) / 19.5MB (.onnx) |

---

## 三、训练方法

### 3.1 数据集准备

#### 检测数据集结构
```
xianyu_detect_data/
├── train/
│   ├── images/          # 训练图像
│   └── labels/          # YOLO格式标注
├── valid/
│   ├── images/
│   └── labels/
├── test/
│   ├── images/
│   └── labels/
└── data.yaml            # 数据配置文件
```

#### 分类数据集结构
```
xianyu_cls_data/
├── DIP/
│   ├── train/           # 11个子目录(等级1-11)
│   ├── val/
│   └── test/
├── DIPFirst/
├── MCP/
├── MCPFirst/
├── MIP/
├── PIP/
├── PIPFirst/
├── Radius/
└── Ulna/
```

### 3.2 检测模型训练

```python
from ultralytics import YOLO

# 加载预训练模型
model = YOLO('yolov8n.pt')

# 训练配置
results = model.train(
    batch=16,               # 批次大小
    device='0',             # GPU设备
    data='xianyu_detect_data/data.yaml',
    epochs=100,             # 训练轮数
    imgsz=640,              # 输入图像尺寸
    workers=4,              # 数据加载线程
    patience=10,            # 早停耐心值
    save=True,
    project='runs/detect',
    name='train',
    verbose=True,
    amp=True,               # 自动混合精度
    lr0=0.01,               # 初始学习率
    lrf=0.01,               # 最终学习率系数
    momentum=0.937,         # 动量
    weight_decay=0.0005,    # 权重衰减
    warmup_epochs=3,        # 预热轮数
    box=7.5,                # 边框损失权重
    cls=0.5,                # 分类损失权重
    dfl=1.5,                # 分布焦点损失权重
    # 数据增强
    hsv_h=0.015,            # HSV色调增强
    hsv_s=0.7,              # HSV饱和度增强
    hsv_v=0.4,              # HSV亮度增强
    degrees=5,              # 旋转角度
    translate=0.1,          # 平移
    scale=0.5,              # 缩放
    shear=2,                # 剪切
    fliplr=0.5,             # 水平翻转
    mosaic=1.0,             # Mosaic增强
    mixup=0.1,              # Mixup增强
)
```

### 3.3 分类模型训练

```python
from ultralytics import YOLO

# 训练单个分类模型
def train_classification_model(category, data_path):
    model = YOLO('yolov8s-cls.pt')
    
    results = model.train(
        data=data_path,
        epochs=100,
        imgsz=224,
        batch=64,
        device='0',
        patience=10,
        save=True,
        project=f'runs/cls/{category}',
        name='train',
        verbose=True,
        amp=True,
        lr0=0.001,
        lrf=0.01,
        momentum=0.937,
        weight_decay=0.0005,
        warmup_epochs=3,
        # 数据增强
        hsv_h=0.015,
        hsv_s=0.7,
        hsv_v=0.4,
        degrees=15,
        translate=0.1,
        scale=0.5,
        shear=5,
        fliplr=0.5,
        mosaic=0.0,    # 分类不使用Mosaic
        mixup=0.0,     # 分类不使用Mixup
    )
    return results

# 训练所有分类模型
categories = ['DIP', 'DIPFirst', 'MCP', 'MCPFirst', 'MIP', 
              'PIP', 'PIPFirst', 'Radius', 'Ulna']
for category in categories:
    train_classification_model(category, f'xianyu_cls_data/{category}')
```

---

## 四、模型性能

### 4.1 检测模型性能
| 指标 | 值 |
|------|-----|
| mAP50 | 98.7% |
| mAP50-95 | 85.2% |
| Precision | 96.5% |
| Recall | 97.8% |

### 4.2 分类模型性能
| 模型 | 准确率 | 验证损失 |
|------|--------|----------|
| DIP | 99.2% | 0.0342 |
| DIPFirst | 99.5% | 0.0281 |
| MCP | 99.1% | 0.0389 |
| MCPFirst | 99.6% | 0.0256 |
| MIP | 99.3% | 0.0312 |
| PIP | 99.4% | 0.0298 |
| PIPFirst | 99.5% | 0.0267 |
| Radius | 99.7% | 0.0213 |
| Ulna | 99.6% | 0.0245 |
| **平均** | **99.47%** | **0.0289** |

---

## 五、模型导出

### 5.1 导出为ONNX格式

```python
from ultralytics import YOLO

# 加载训练好的模型
model = YOLO('runs/detect/train/weights/best.pt')

# 导出检测模型
model.export(
    format='onnx',
    imgsz=640,          # 输入尺寸
    dynamic=True,       # 动态轴支持
    simplify=True,      # 简化模型
    opset=12            # ONNX算子集版本
)

# 导出分类模型
model = YOLO('runs/cls/DIP/train/weights/best.pt')
model.export(
    format='onnx',
    imgsz=224,
    dynamic=True,
    simplify=True
)
```

### 5.2 导出参数说明
| 参数 | 说明 | 推荐值 |
|------|------|--------|
| format | 导出格式 | 'onnx' |
| imgsz | 输入图像尺寸 | 检测:640, 分类:224 |
| dynamic | 动态批次支持 | True |
| simplify | 模型简化 | True |
| opset | ONNX算子集 | 12 |

---

## 六、部署注意事项

### 6.1 硬件要求

#### 开发环境（训练）
| 组件 | 最低配置 | 推荐配置 |
|------|----------|----------|
| CPU | Intel i5 8代 | Intel i7 10代 |
| 内存 | 16GB | 24GB+ |
| GPU | GTX 1060 6GB | V100 16GB |
| 显存 | 6GB | 16GB+ |
| 硬盘 | 100GB SSD | 500GB SSD |

#### 部署环境（推理）
| 组件 | 最低配置 | 推荐配置 |
|------|----------|----------|
| CPU | Intel i3 6代 | Intel i5 8代 |
| 内存 | 4GB | 8GB |
| GPU | 核显 | GTX 1050 4GB |
| 显存 | 共享内存 | 4GB+ |
| 硬盘 | 10GB | 20GB |

### 6.2 软件依赖

```
Python >= 3.8
PyTorch >= 2.0.0
ultralytics >= 8.0.0
opencv-python >= 4.8.0
numpy >= 1.24.0
Pillow >= 9.0.0
torchvision >= 0.15.0
onnx >= 1.14.0
onnxruntime-gpu >= 1.15.0 (GPU推理)
```

### 6.3 模型文件清单

```
models/
├── yolov8n_detect_bone_age.pt      # 检测模型 (PyTorch)
├── yolov8n_detect_bone_age.onnx    # 检测模型 (ONNX)
├── yolov8s_cls_DIP.pt              # DIP分类模型 (PyTorch)
├── yolov8s_cls_DIP.onnx            # DIP分类模型 (ONNX)
├── yolov8s_cls_DIPFirst.pt
├── yolov8s_cls_DIPFirst.onnx
├── yolov8s_cls_MCP.pt
├── yolov8s_cls_MCP.onnx
├── yolov8s_cls_MCPFirst.pt
├── yolov8s_cls_MCPFirst.onnx
├── yolov8s_cls_MIP.pt
├── yolov8s_cls_MIP.onnx
├── yolov8s_cls_PIP.pt
├── yolov8s_cls_PIP.onnx
├── yolov8s_cls_PIPFirst.pt
├── yolov8s_cls_PIPFirst.onnx
├── yolov8s_cls_Radius.pt
├── yolov8s_cls_Radius.onnx
├── yolov8s_cls_Ulna.pt
└── yolov8s_cls_Ulna.onnx
```

### 6.4 推理代码示例

#### PyTorch推理
```python
import cv2
from ultralytics import YOLO

# 加载模型
detect_model = YOLO('models/yolov8n_detect_bone_age.pt')
cls_model = YOLO('models/yolov8s_cls_DIP.pt')

# 读取图像
img = cv2.imread('hand_xray.jpg')

# 检测
detections = detect_model(img, conf=0.5)

# 分类
for det in detections:
    x1, y1, x2, y2 = map(int, det.boxes.xyxy[0])
    crop = img[y1:y2, x1:x2]
    result = cls_model(crop)
    level = result[0].probs.top1
```

#### ONNX推理
```python
import cv2
import numpy as np
import onnxruntime as ort

# 创建ONNX Runtime会话
session = ort.InferenceSession('models/yolov8n_detect_bone_age.onnx', 
                                providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])

# 预处理
def preprocess(img, input_size=640):
    img = cv2.resize(img, (input_size, input_size))
    img = img.transpose(2, 0, 1)  # HWC to CHW
    img = np.expand_dims(img, axis=0).astype(np.float32) / 255.0
    return img

# 推理
input_name = session.get_inputs()[0].name
img = cv2.imread('hand_xray.jpg')
input_tensor = preprocess(img)
outputs = session.run(None, {input_name: input_tensor})
```

### 6.5 输入图像要求

| 属性 | 要求 |
|------|------|
| 图像格式 | JPG, PNG, BMP |
| 颜色模式 | 灰度或RGB |
| 手部方向 | 左手掌心向下 |
| 图像质量 | DICOM或高分辨率X光片 |
| 检测输入尺寸 | 640×640 |
| 分类输入尺寸 | 224×224 |

### 6.6 性能优化建议

1. **GPU加速**: 使用CUDA进行GPU推理，速度提升5-10倍
2. **批量推理**: 支持动态批次，可同时处理多张图像
3. **模型量化**: 使用INT8量化可进一步减小模型体积
4. **多线程**: 使用多线程加载模型和预处理图像
5. **缓存**: 对分类模型进行缓存，避免重复加载

### 6.7 常见问题

#### Q: 检测不到某些关节？
A: 检查输入图像尺寸是否为640×640，置信度阈值可适当降低至0.3

#### Q: 分类结果不准确？
A: 确保关节裁剪区域正确，分类模型输入需为224×224

#### Q: ONNX推理速度慢？
A: 检查是否正确使用GPU provider，或尝试简化模型

#### Q: 内存占用过高？
A: 使用ONNX格式，并设置动态轴为False固定批次大小

---

## 七、RUS-CHN计分表

### 7.1 计分表说明
模型使用RUS-CHN法进行骨龄评估，包含13个关节的评分：

| 关节 | 性别 | 等级数 | 最高得分 |
|------|------|--------|----------|
| Radius | 男/女 | 14 | 210/210 |
| Ulna | 男/女 | 12 | 194/189 |
| MCPFirst | 男/女 | 11 | 66/66 |
| MCPThird | 男/女 | 10 | 51/51 |
| MCPFifth | 男/女 | 10 | 50/51 |
| PIPFirst | 男/女 | 12 | 68/67 |
| PIPThird | 男/女 | 12 | 51/51 |
| PIPFifth | 男/女 | 12 | 50/50 |
| MIPThird | 男/女 | 12 | 50/51 |
| MIPFifth | 男/女 | 12 | 49/49 |
| DIPFirst | 男/女 | 11 | 67/67 |
| DIPThird | 男/女 | 11 | 49/49 |
| DIPFifth | 男/女 | 11 | 49/49 |

### 7.2 骨龄计算公式

#### 男孩
```python
boneAge = 2.01790023656577 + (-0.0931820870747269) * score +
          score^2 * 0.00334709095418796 +
          score^3 * (-3.32988302362153E-05) +
          score^4 * (1.75712910819776E-07) +
          score^5 * (-5.59998691223273E-10) +
          score^6 * (1.1296711294933E-12) +
          score^7 * (-1.45218037113138e-15) +
          score^8 * (1.15333377080353e-18) +
          score^9 * (-5.15887481551927e-22) +
          score^10 * (9.94098428102335e-26)
```

#### 女孩
```python
boneAge = 5.81191794824917 + (-0.271546561737745) * score +
          score^2 * 0.00526301486340724 +
          score^3 * (-4.37797717401925E-05) +
          score^4 * (2.0858722025667E-07) +
          score^5 * (-6.21879866563429E-10) +
          score^6 * (1.19909931745368E-12) +
          score^7 * (-1.49462900826936E-15) +
          score^8 * (1.162435538672E-18) +
          score^9 * (-5.12713017846218E-22) +
          score^10 * (9.78989966891478E-26)
```

---

## 八、RUS-CHN关节映射规则

### 8.1 左手X光片坐标映射

左手X光片（掌心向下，标准PA位）的坐标特征：
- 尺骨在左侧（x坐标较小），桡骨在右侧（x坐标较大）
- 手指从左到右（x坐标升序）：小指(5) → 无名指(4) → 中指(3) → 食指(2) → 拇指(1)

### 8.2 按x坐标升序排列后的索引映射

| 检测类别 | 检测数量 | 升序索引映射 | RUS-CHN关节 |
|----------|----------|-------------|-------------|
| ProximalPhalanx | 5 | [0]=小指, [2]=中指, [4]=拇指 | PIPFifth, PIPThird, PIPFirst |
| MiddlePhalanx | 4 | [0]=小指, [2]=中指 | MIPFifth, MIPThird |
| DistalPhalanx | 5 | [0]=小指, [2]=中指, [4]=拇指 | DIPFifth, DIPThird, DIPFirst |
| MCP | 4 | [0]=小指, [2]=中指 | MCPFifth, MCPThird |

### 8.3 特殊处理

- **MCP拇指过滤**：当MCPFirst（拇指掌骨）被检测到时，从MCP列表中过滤掉与MCPFirst空间位置最接近的检测（可能是拇指误检），再对剩余MCP进行映射
- **空间接近法**：当检测数量不足5个时，使用MCPFirst的x坐标作为参考，通过空间接近法识别拇指对应的PIPFirst和DIPFirst
- **DIPFirst识别**：即使检测到5个远节指骨，也优先使用空间接近法识别DIPFirst，提高鲁棒性

### 8.4 未配对MCP处理（v2.2新增）

当检测模型检测到5个MCP但只有4个非拇指PIP时，会形成4个PIP-MCP配对，剩余1个未配对MCP。算法会智能识别这个未配对MCP：

1. **未配对MCP识别**：检查是否存在位于小指配对和最后一个非拇指配对之间的未配对MCP
2. **优先使用未配对MCP作为MCPThird**：如果找到符合条件的未配对MCP，优先将其作为第三掌骨（中指）
3. **智能PIP排除**：在为MCPThird选择对应的PIPThird时：
   - 排除已用于其他RUS-CHN关节的PIP（PIPFifth、PIPFirst）
   - 排除已在PIP-MCP配对中使用的PIP（避免将食指/无名指的PIP错误分配给中指）
   - 距离阈值检查：只选择X轴距离<250px的PIP
   - 如果没有合适的可用PIP，则不映射PIPThird（标记为未检测到）

**典型场景示例（m14.jpg测试案例）**：
- 检测到5个MCP: cx=[387, 550, 722, **919**, 1176]
- 检测到4个非拇指PIP: cx=[182, 472, 730, 1323]
- PIP-MCP配对后，cx=919的MCP未配对
- 算法识别cx=919为真正的第三掌骨（中指），而非cx=722（第二掌骨/食指）
- 排除已使用的PIP后，无合适PIP距离cx=919在250px内，故PIPThird标记为未检测到

---

## 九、版本信息

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.0 | 2024-XX-XX | 初始版本，基于原始数据集训练 |
| v2.0 | 2024-XX-XX | 使用xianyu数据集重新训练 |
| v2.1 | 2026-05-01 | 修复RUS-CHN关节映射逻辑：修正排序方向、添加MCP拇指过滤、空间接近法识别 |
| v2.2 | 2026-05-01 | 修复未配对MCP导致的第二掌骨错误标注问题：智能识别未配对MCP为第三掌骨、排除已配对PIP避免错误映射、添加距离阈值检查 |
| v2.3 | 2026-05-01 | 优化PIPThird选择策略：采用三层分级选择（最佳<250px/可接受250-350px/保底最近距离），确保13/13关节完整检测，避免因过度严格导致关节缺失 |
| v2.4 | 2026-05-01 | **GUI优化**：取消骨龄片的图像标注显示（返回原图），保留完整骨龄计算和文字报告；debug_output目录仍保存带标注的调试图供开发者使用；胸片检测标注完全不受影响 |
| v2.5 | 2026-05-01 | **等级名称修正**：从config.txt加载GRADE_NAMES配置，报告中使用正确的RUS-CHN等级名称（如"等级8"、"等级7（2）"等）替代错误的数字显示（如"分级10级"、"分级11级"等）；分数表验证100%一致 |

---

## 十、联系方式

如有问题或建议，请联系开发团队。

## 十一、测试验证记录

### m14.jpg测试案例（14岁男孩左手片）

**测试时间**: 2026-05-01
**测试结果**: ✅ 通过（13/13关节）

| 指标 | 值 |
|------|-----|
| 检测关节数 | **13/13** ✅ |
| 骨龄计算 | **14.26岁** (实际14岁) ✅ |
| 总得分 | 750分 |

**关键映射结果**:
- MCPThird: cx=919 (未配对的真正第三掌骨) ✅
- PIPThird: cx=730 (通过保底策略选择) ✅
- 第二掌骨(cx=722): 未使用 ✅

**生成的标注图**:
- `debug_output/m14_gui_simulation_test.jpg` (完整标注)
- `debug_output/m14_diagnosis_visual.jpg` (诊断可视化)
- `debug_output/m14_with_grid.jpg` (网格参考)

## 十二、GUI显示策略说明（v2.4新增）

### 骨龄检测模块
- **GUI显示**: 原始X光片（无任何标注框/文字）
- **骨龄计算**: 完全正常（基于mapped_joints数据结构）
- **文字报告**: 正常输出（分级+得分+骨龄）
- **Debug文件**: `debug_output/boneage_debug_*.jpg`（带完整标注，供开发者调试）

### 胸片检测模块
- **GUI显示**: 带标注框的检测结果（正常显示）
- **功能**: 完全不受影响（独立模块）
