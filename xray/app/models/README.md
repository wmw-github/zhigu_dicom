# 模型权重放置说明（Xray AI 模型）

本目录下的 AI 模型权重（`.onnx`）**不随开源仓库分发**（总体积约 440 MB，
单文件最大约 125 MB）。运行骨折检测、骨龄评估、脊柱侧弯、足弓分析等功能前，
请把权重文件按下列清单放到对应路径。

## 需要的模型文件

| 功能 | 相对路径（自本目录起） | 大小（约） |
|---|---|---|
| 骨折检测 | `best_fracture_yolov8dle.onnx` | 43 MB |
| 骨折检测（备用） | `trained/best.onnx` | 43 MB |
| 脊柱侧弯 | `spine/best.onnx` | 43 MB |
| 足弓关键点 | `foot_arch/foot_arch_keypoint.onnx` | 125 MB |
| 骨龄 · 检测 | `boneage/yolov8n_detect_bone_age.onnx` | 12 MB |
| 骨龄 · 分级分类（9 个模型覆盖 13 关节，见 `xray/app/config.txt` 映射） | `boneage/yolov8s_cls_MCPFirst.onnx` | 20 MB |
| | `boneage/yolov8s_cls_MCP.onnx`（MCPThird/MCPFifth 共用） | 20 MB |
| | `boneage/yolov8s_cls_DIPFirst.onnx` | 20 MB |
| | `boneage/yolov8s_cls_DIP.onnx`（DIPThird/DIPFifth 共用） | 20 MB |
| | `boneage/yolov8s_cls_PIPFirst.onnx` | 20 MB |
| | `boneage/yolov8s_cls_PIP.onnx`（PIPThird/PIPFifth 共用） | 20 MB |
| | `boneage/yolov8s_cls_MIP.onnx`（MIPThird/MIPFifth 共用） | 20 MB |
| | `boneage/yolov8s_cls_Radius.onnx` | 20 MB |
| | `boneage/yolov8s_cls_Ulna.onnx` | 20 MB |

> 骨龄 13 关节与分类模型的对应关系定义在 `xray/app/config.txt` 的 `Arthrosis` 表里，
> 同一指骨的双侧关节（如 MCPFifth）与同指中节共用同一模型文件。

## 获取方式

模型权重为训练产物，请通过以下任一方式获取：

1. 项目的 **Releases / 网盘发布页**（若维护者提供）下载完整模型包；
2. 自行训练：骨龄标注/训练辅助脚本见 `boneage/` 目录，
   脊柱训练脚本见 `spine/train_quick.py`。

## 校验放置是否正确

启动软件后打开「Xray 分析」，在设置里查看模型加载状态；
缺文件时程序会在日志（`xray/app/` 下 `*.log`）中给出缺失路径。

训练辅助脚本（`boneage/*.py`、`spine/*.py`）已随仓库提供，无需下载。
