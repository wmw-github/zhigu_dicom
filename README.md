# Zhigu DICOM · 知骨影像

一套面向 Windows 的医学影像桌面软件，集成 **CT 三维重建阅片**、**脊柱 X 线拼接**、
**X 线 AI 辅助分析**（骨折检测 / 骨龄评估 / 脊柱侧弯 / 足弓分析）与
**影像报告智能质控**，并提供浏览器「网页取图」桥接能力。

主界面基于 PyQt5 + VTK，AI 推理基于 ONNX Runtime，DICOM 解析基于 pydicom（含 JPEG-LS 支持）。

> **免责声明**：本软件（包括其 AI 模块）输出的所有结果仅供医学研究与临床辅助参考，
> **不构成诊断依据**，不能替代执业医师的专业判断。任何临床使用及其后果由使用者自行承担。

---

## 功能特性

| 模块 | 说明 |
|---|---|
| **CT 三维阅片** | DICOM 序列加载、二维联动阅片、MPR 多平面重建、体渲染、骨骼三维增强（低密度骨折缝叠加层）、测量与图片导出 |
| **脊柱拼接** | 全脊柱 X 线分片自动拼接（`spine/`） |
| **Xray AI** | 骨折检测（YOLO 系）、骨龄评估（RUS-CHN 13 关节分级）、脊柱侧弯（Cobb 角）、足弓关键点分析（`xray/`） |
| **报告质控** | 影像报告 OCR、错别字/矛盾/危急值/左右侧别等多规则质控（`reportqc/`） |
| **网页取图** | Chrome 扩展 + 本地桥接（`web_bridge/`），从网页把影像拉进软件 |
| **在线升级** | 可自建升级服务器，带 RSA 索引验签与 SHA256 补丁链（`updater.py`） |

## 目录结构

```
zhigu_dicom_opensource/
├── main.py                # 主程序入口（界面 + CT 阅片 + 各模块装配）
├── theme.py               # 皮肤/主题系统（语义色 token → QSS）
├── ct3d_utils.py          # CT 体数据处理、重建、三维增强
├── export_utils.py        # 图片/序列导出
├── coordinate_utils.py    # 坐标换算
├── loading_ui.py / ui_icons.py
├── runtime_env.py         # 运行环境引导（Qt 插件路径、嵌入式 pyenv 兼容）
├── updater.py / update_ui.py   # 在线升级客户端（纯标准库可独立测试）
├── license_*.py           # 授权激活系统（可选，见下文「授权系统」）
├── *_bridge.py            # 各模块与网页取图桥的接线
├── run_env.bat            # 启动脚本（优先本地 pyenv，回退系统 Python）
├── install.bat            # 创建桌面快捷方式
├── spine/                 # 脊柱拼接算法
├── xray/                  # Xray AI 子项目
│   └── app/
│       ├── core/          # 数据加载、模型推理、预处理
│       ├── ui/            # Xray 界面
│       ├── models/        # ⚠ 模型权重不入库，见 models/README.md
│       └── config.txt     # 骨龄 13 关节 ↔ 分类模型映射、评分标准
├── reportqc/              # 报告质控子项目（QC 规则、OCR、PACS 截取）
│   └── models/            # ⚠ OCR 权重不入库，见 models/README.md
├── web_bridge/            # Chrome 扩展 + 原生消息桥接
└── docs/                  # 部署、技术、用户、质控说明文档
```

## 快速开始

### 环境要求

- Windows 7 及以上（开发与测试以 Win7 / Win10 为主）
- Python **3.8+**（推荐 3.8，与原产品内嵌运行时一致）
- 依赖安装：

```bash
pip install -r requirements.txt
```

### 放置 AI 模型权重

模型权重不随仓库分发（约 440 MB），按说明放到对应目录：

- Xray AI 模型 → 见 [`xray/app/models/README.md`](xray/app/models/README.md)
- 报告质控 OCR 模型 → 见 [`reportqc/models/README.md`](reportqc/models/README.md)

只用 CT 阅片/三维重建不需要任何模型。

### 运行

```bash
# 方式一：双击 run_env.bat（会优先使用 ./pyenv 内嵌运行时，否则用系统 Python）
run_env.bat

# 方式二：直接运行
python main.py
```

`install.bat` 可在桌面创建快捷方式。

> **关于根目录的 `python3.dll`**：它是 CPython 稳定 ABI 转发库（PSF 许可，
> 约 60 KB）。若你把内嵌运行时放在 `./pyenv` 下运行，需要它才能正常加载
> PyQt5 / cv2（缺失会段错误）；使用 pip 安装的系统 Python 时可忽略。

### 网页取图（可选）

1. 在 Chrome 扩展管理页加载 `web_bridge/chrome-extension/`（开发者模式 → 加载已解压的扩展程序）；
2. 修改 `com.zhigu.bridge.json` 中的 `path` 为本机 `web_bridge_native.bat` 的绝对路径，
   `allowed_origins` 改成你的扩展 ID；
3. 以管理员权限运行 `web_bridge/register_protocol.bat` 注册协议/原生消息宿主。

详见 [`web_bridge/README.md`](web_bridge/README.md)。

## 配置说明

| 文件 | 作用 |
|---|---|
| `app_settings.json` | 扫描目录、在线升级服务器（`update_base_url`）等运行时配置 |
| `com.zhigu.bridge.json` | 网页取图原生消息宿主注册信息 |
| `xray/app/config.txt` | 骨龄关节-模型映射与评分标准 |

**在线升级**：开源版默认不指定升级服务器。如需自建，在 设置 → 在线升级服务器
填写你的地址（`updater.py` 内置 RSA 公钥验签，配套服务端签名工具可自行实现，
索引格式见 `updater.py` 顶部注释）。

## 授权系统

仓库保留了完整的授权激活模块（`license_codec.py`、`license_bridge.py`、
`xray/app/license*.py`），但**不含任何激活数据/密钥**（`license.dat` 已排除）。
你可以：

- 原样保留：自行签发授权码（编解码逻辑见 `license_codec.py` 文档字符串）；
- 或移除调用处（入口在 `main.py` 与 `xray/app/main.py` 的启动检查），
  让软件开箱即用。

## 开发与自检

```bash
# 语法自检（对全部 .py 做编译检查）
python -m compileall -q .

# 发行自检（约 144 项，覆盖布局、主题、升级、导出等回归点）
python _release_selfcheck.py
```

`_release_selfcheck.py` 依赖 `inspect.getsource` 做静态断言，
不构建 QWidget，可在无显示环境下运行。

## 文档

| 文档 | 内容 |
|---|---|
| [`技术说明.md`](技术说明.md) | 架构与实现细节 |
| [`用户说明.md`](用户说明.md) | 使用手册 |
| [`docs/部署说明.md`](docs/部署说明.md) | 部署与安装 |
| [`docs/报告质控功能说明.md`](docs/报告质控功能说明.md) | 质控模块 |
| [`docs/发行版文件清单.md`](docs/发行版文件清单.md) | 发行包文件清单（历史参考） |

## 贡献

欢迎 Issue 与 Pull Request。提交前请保证：

1. `python -m compileall -q .` 通过；
2. `_release_selfcheck.py` 通过；
3. 不提交模型权重、患者数据、日志与 `pyenv/`（见 `.gitignore`）。

## 许可证

本项目源代码采用 **Apache License 2.0** 发布，详见 [`LICENSE`](LICENSE) 与
[`NOTICE`](NOTICE)。

⚠ **重要**：PyQt5 为 GPL-3.0 / 商业双许可。分发链接 PyQt5 的二进制（打包成品）
需要满足 GPL-3.0 对组合作品的要求，或持有 Riverbank/Qt 商业许可；
将本项目（Apache-2.0）纳入 GPL-3.0 作品分发是兼容的。详见 `NOTICE` 中的说明。
