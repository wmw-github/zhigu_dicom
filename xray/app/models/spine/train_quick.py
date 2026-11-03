"""
YOLOv8 快速训练脚本 - 使用默认参数
直接运行此脚本即可开始训练
"""

import os
import sys
import subprocess
from pathlib import Path

def install_dependencies():
    """安装依赖"""
    print("正在安装依赖库...")
    packages = [
        'ultralytics',
    ]

    for package in packages:
        print(f"安装 {package}...")
        try:
            subprocess.check_call([
                sys.executable, '-m', 'pip', 'install',
                package,
                '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple',
                '--trusted-host', 'pypi.tuna.tsinghua.edu.cn'
            ], timeout=600)
        except:
            try:
                subprocess.check_call([
                    sys.executable, '-m', 'pip', 'install',
                    package
                ], timeout=600)
            except Exception as e:
                print(f"安装 {package} 失败: {e}")
                return False
    return True

def main():
    print("=" * 80)
    print("YOLOv8 脊柱检测模型 - 快速训练")
    print("=" * 80)

    base_path = Path(__file__).parent
    data_yaml = base_path / "data.yaml"

    if not data_yaml.exists():
        print("错误: data.yaml 文件不存在")
        return

    if not install_dependencies():
        print("依赖安装失败")
        return

    try:
        from ultralytics import YOLO
        import torch

        print(f"\nGPU状态: {'可用' if torch.cuda.is_available() else '不可用'}")
        if torch.cuda.is_available():
            print(f"GPU: {torch.cuda.get_device_name(0)}")

        print("\n加载预训练模型 yolov8s.pt...")
        model = YOLO('yolov8s.pt')

        print("\n开始训练...")
        print("配置:")
        print("  - 模型: yolov8s")
        print("  - 轮数: 100")
        print("  - 批次大小: 16")
        print("  - 图像大小: 640")
        print("  - 设备: " + ("cuda" if torch.cuda.is_available() else "cpu"))
        print()

        results = model.train(
            data=str(data_yaml),
            epochs=100,
            batch=16,
            imgsz=640,
            project=str(base_path / "runs"),
            name="train",
            exist_ok=True,
            device=0 if torch.cuda.is_available() else 'cpu',
            workers=8,
            patience=50,
            save=True,
            save_period=10,
            cache=True,
            amp=True,
            pretrained=True,
            optimizer='AdamW',
            lr0=0.001,
            verbose=True,
        )

        print("\n" + "=" * 80)
        print("训练完成!")
        print("=" * 80)
        print(f"最佳模型: {results.save_dir}/weights/best.pt")
        print(f"最终模型: {results.save_dir}/weights/last.pt")

        print("\n运行验证...")
        metrics = model.val(
            data=str(data_yaml),
            split='val',
            batch=16,
            imgsz=640,
            device=0 if torch.cuda.is_available() else 'cpu',
            workers=8,
            plots=True,
            save=True,
        )

        print("\n验证结果:")
        print(f"  mAP50: {metrics.box.map50:.4f}")
        print(f"  mAP50-95: {metrics.box.map:.4f}")
        print(f"  Precision: {metrics.box.mp:.4f}")
        print(f"  Recall: {metrics.box.mr:.4f}")

        print("\n" + "=" * 80)
        print("所有任务完成!")
        print("=" * 80)

    except Exception as e:
        print(f"发生错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
