# 模型权重放置说明（报告质控 OCR 模型）

本目录下的 OCR 模型权重（`.onnx`）**不随开源仓库分发**。报告质控功能
（识别报告文字、定位质控区域）依赖 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)
导出的 PP-OCRv4 模型，请把下列文件放到 `ocr/` 子目录：

| 文件（自本目录起） | 用途 | 大小（约） |
|---|---|---|
| `ocr/ch_PP-OCRv4_det_infer.onnx` | 文本检测 | 4.6 MB |
| `ocr/ch_PP-OCRv4_rec_infer.onnx` | 文本识别 | 11 MB |
| `ocr/ch_ppocr_mobile_v2.0_cls_infer.onnx` | 文本方向分类 | 572 KB |

## 获取方式

PP-OCR 系列模型以 **Apache-2.0** 协议发布，可自行从 PaddleOCR 官方获取
并用 `paddle2onnx` 导出为 ONNX，或使用维护者提供的模型包（见项目 Releases）。

放置完成后启动软件，打开「报告质控」即可自动加载。
