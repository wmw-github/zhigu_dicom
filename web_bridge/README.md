# 网页一键调用知骨影像

在云影像 / 内网 PACS 网页上放一个浮动按钮，点击后把当前检查的 DICOM
送到本机「知骨影像」打开。医生的操作从「下载 zip → 解压 → 找目录 → 拖进软件」
变成「点一下」。

目前适配两类站点，同一套按钮、同一套数据通路：

| 类型 | 样本 | 取数方式 |
|---|---|---|
| 云影像（ftimage 架构） | 鲁云影像 `lyyx.lyswsjkw.com/dimage/` | 钩响应拿影像清单（CDN 相对路径） |
| 内网 PACS（WADO） | 临床系统 + WebDicomView，`172.123.3.5:11519` / `:8989` | 读阅片器内存模型，自己拼 `GetBlock` 地址（见第九节） |

---

## 一、为什么不能让软件自己去请求云 PACS 的接口

对 `lyyx.lyswsjkw.com/dimage/`（ftimage 架构，鲁云/飞图系同一套）实测：

| 探测 | 结果 |
|---|---|
| `GET /api/pat/info`、`/api/series/image/{id}` | **HTTP 605**（自定义拒绝码，缺签名） |
| `app.js` 中的响应处理 | 响应体以 `04` 开头 → `Sz.decrypt()` 做 **SM2 解密** → 再 `JSON.parse` |
| 请求头 | `encrpt: true` + `BIZ-REQUEST-ID`（参数同样被加密） |

**结论：会话态和国密加解密全在浏览器里，外部进程重放请求拿不到任何数据。**
所以唯一可行的取数位置是「页面已经把响应解好密的那一刻」——这也是本方案把
钩子放在浏览器里的根本原因，不是技术偏好。

顺带说清一个容易白干的坑：`imageGrpcHost` 非空时，页面**不走 HTTP 取 DICOM**，
而是用 gRPC 传像素（`noGrpc=false` 的分支）。这种部署下页面里根本不存在可下载的
`.dcm` 地址，扩展会拿不到清单，只能回退到平台自带的「下载影像」。

### 实测到的真实数据结构（V3 移动版）

`GET /v3/api/pat/image/{seriesUuid}` 返回 SM2 密文，页面解密后是：

```json
{ "result": [
    { "imageTime": "2026-10-26 10:36:38",
      "rawKey": "8e8271f8e6eef7e4a22ae6a21ca1ac43",
      "customFilePath": false,
      "imageResult": { "jpeg": "/sdly/lysyxy02/05391013/<study>/<series>/<rawKey>" } } ],
  "code": "N0000", "message": "成功", "version": "V3.0" }
```

三个和「标准 PACS」不一样的地方，每一条都足以让朴素实现全军覆没：

| 反直觉的点 | 后果 |
|---|---|
| **没有 `imageList`**，影像就是 `result` 数组本身 | 只认 `imageList` 的递归一条都抓不到 |
| **元素没有 `imageUuid`**，身份只有 `rawKey` | 按 uuid 去重会全部丢弃 |
| **路径里没有 `seriesUuid` 字段**——序列号藏在路径里 | 无法归组；实际上就靠 `<study>/<series>/<rawKey>` 三段 |

**序列归属的正确解法**：从影像路径反推，倒数第二段就是 `seriesUuid`
（`/sdly/<院区>/<科室>/<study>/<series>/<rawKey>`）。

**真实资源地址**在 `https://images2.ftimage.cn/v3` + 上面那个相对路径。这个 CDN
**不校验来源**（直接 curl 能取到），路径必须严格按 `imageResult` 给的拼——
少一位就返回 `no resource found`（HTTP 200 + 17 字节文本，特别容易误判成成功）。

正因为 CDN 路径没有扩展名，究竟是 DICOM 还是 JPEG **只能看响应魔数**。
扩展会记录浏览器真正请求过的 URL，并把 `imageBase`、`imageResult` 的字段名
一起送出来；软件端下完按 `DICM` / `FFD8FF` 决定存 `.dcm` 还是 `.jpg`。

---

## 二、架构

```
┌─────────────────────── Chrome ───────────────────────┐
│ 云影像页面 (Vue)                                      │
│   │ 响应密文 → 页面自己解密 → JSON.parse              │
│   ▼                                                   │
│ boot.js (隔离世界)  → <script> 把 inject.js 送进主世界  │
│ inject.js (MAIN world, document_start)                │
│   hook JSON.parse / Response.json|text /              │
│        XHR.response & loadend 主动扫描                │
│   → 抓到影像清单（imageList / result 数组 / 单张懒加载）  │
│   → 归一化成 [{url, alt, kind}, ...]                   │
│   │ postMessage                                       │
│   ▼                                                   │
│ content.js (隔离世界)  浮动按钮「用知骨打开 · 312 张」  │
│   │ chrome.runtime.sendMessage                        │
│   ▼                                                   │
│ background.js  RPC + 跨 iframe 清单缓存               │
└──────┬──────────────────────────────┬─────────────────┘
       │ 首选：原生消息（stdio 管道）   │ 回退：HTTP 回环
       │ connectNative                │ fetch 127.0.0.1:18923
       ▼                              ▼
┌──────────────────┐        ┌─────────────────────────────┐
│ web_bridge_      │        │  {pageUrl, cookies,         │
│ native.py        │        │   series:[{images:[{url}]}]}│
│  ProxyHandler({})│        │  GET /task?id=... 轮询进度   │
│  显式禁用代理     │        └──────────────┬──────────────┘
└────────┬─────────┘                       │
         └──────────────┬──────────────────┘
                        ▼
┌────────────────────── 知骨影像 ──────────────────────┐
│ web_bridge.py                                        │
│  · QLocalServer 单实例：第二次唤醒转给已运行进程       │
│  · ThreadingHTTPServer 只监听 127.0.0.1:18923-18930  │
│  · 6 线程下载（带 Referer / Cookie / UA）             │
│  · 按响应魔数决定存 .dcm 还是 .jpg                     │
│  · 落到 %TEMP%\zhigu_web_xxx\<序号>_<序列名>\         │
│  · 回调 MainWindow._open_dir() 直接打开               │
└──────────────────────────────────────────────────────┘

另外：zhigu://import?u=<页面URL> 只在软件没启动时用来唤起（不经网络）。
```

**关键取舍：扩展只传"取数凭据"，不传像素。**
一个 CT 300 张 ≈ 150MB，base64 再膨胀 33%，浏览器内存和回环 IO 都吃不消。
只传 URL 清单，报文从 150MB 降到几十 KB，下载交给软件端慢慢跑。

---

## 三、四条路线的取舍

| 路线 | 稳定度 | 侵入性 | 适用 |
|---|---|---|---|
| **A. 官方下载 → 软件打开 zip** | 最高 | 零 | 有「下载影像」按钮就有；但要多点 3 步 |
| **B. 本方案：扩展 + 回环 HTTP** | 高 | 中 | 本机医生工作站，跨任意云 PACS |
| C. Native Messaging | 最高（双向、无长度限制） | 高（要写注册表 host manifest） | 走企业统一部署、批量推 |
| D. 软件自己去调 PACS 接口 | 不可行 | — | 鲁云这类国密加密场景直接排除 |

路线 C 和 B 的数据结构完全一样，只是把 HTTP 换成 `chrome.runtime.connectNative`。
如果以后要"下载进度回显到网页"或"网页发起质控任务"，再升级到 C 即可，
现在没必要先把复杂度付掉。

---

## 四、部署（四步）

```
<安装包>\
├─ CT3D260414_release\
│   ├─ main.py                    ← 已加 2 处钩子（web_bridge.bootstrap / bind_window）
│   ├─ web_bridge.py              ← HTTP 回环桥
│   ├─ web_bridge_setup.py        ← 注册 zhigu:// 协议
│   ├─ web_bridge_native.py       ← 原生消息宿主（stdin/stdout 转发）
│   ├─ web_bridge_native.bat      ← 原生消息启动器
│   └─ web_bridge_native_setup.py ← 注册原生消息宿主
└─ web_bridge\
    ├─ register_protocol.bat
    └─ chrome-extension\          ← 加载这个目录
```

1. **注册协议**：双击 `web_bridge\register_protocol.bat`
   写 `HKCU\Software\Classes\zhigu`（当前用户，**不需要管理员**）。
   校验：`python web_bridge_setup.py --check`
   撤销：`python web_bridge_setup.py --uninstall`

2. **注册原生消息宿主**（强烈建议，见第五节为什么）：
   ```
   pyenv\python.exe web_bridge_native_setup.py
   ```
   写 `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.zhigu.bridge`
   （Edge 同样写一份），并在发行包根生成 `com.zhigu.bridge.json`。
   校验：`pyenv\python.exe web_bridge_native_setup.py --check`

3. **装扩展**：Chrome → `chrome://extensions` → 打开「开发者模式」→
   「加载已解压的扩展程序」→ 选 `chrome-extension` 目录。
   **务必确认扩展 ID 是 `enemdgbbndekeeihllpbikeifjnlefmj`** —— 这个 ID 由
   manifest 里的 `key` 固定，原生消息的 `allowed_origins` 只认它。
   换过目录加载导致 ID 变了，原生通道会被浏览器拒绝（此时自动回退 HTTP）。
   Chrome 111+ 最好；低于 111 也能用（`boot.js` 会用 `<script>` 标签把钩子
   送进页面主世界，代价是如果站点 CSP 很严就送不进去）。
   **1.5.0 起多申请了 `cookies` 权限**（读 HttpOnly 会话，内网 PACS 必需）：
   升级后 Chrome 会停用扩展并提示重新授权，点「重新启用 / 同意」即可，
   不授权的话内网 PACS 会因未登录而全部下载失败。

4. **用**：打开鲁云影像的检查 → **点开一个具体序列、能看到图像** →
   按钮显示「用知骨打开 · 312 张」→ 点一下。
   按钮上没数字就先别点，那是还没拿到清单（见第六节）。

关闭整个功能：`app_settings.json` 加 `"web_import_enabled": false`。

---

## 五、为什么除了 HTTP 还要一条原生消息通道

扩展最初只走 `http://127.0.0.1:18923`。这条路在**装了代理软件 / VPN** 的机器上
会**静默失败**：

| 现象 | 真相 |
|---|---|
| 点按钮后软件被拉起来了，但一张图都没送过去 | `zhigu://` 协议唤起成功（浏览器本地处理，不经网络） |
| `web_bridge.log` 里只有「协议唤醒」，**没有「收到清单」** | 说明 `POST /import` 从未到达 |
| 再点一次，日志变成「已有实例在运行」 | 说明 `pickPort()` 探不到端口，扩展又去拉起软件 |

根因是 `fetch('http://127.0.0.1:18923/ping')` 在 TCP 层就建不起来 ——
Clash / v2ray 的 TUN 模式、企业代理管理扩展都可能劫持回环流量，而浏览器
不会把真实原因告诉你，`fetch` 只有一个 `TypeError: Failed to fetch`。

**原生消息（Native Messaging）走的是 Chrome 与本地进程之间的 stdin/stdout
管道，完全不经网络栈**，代理、VPN、防火墙、混合内容策略一概碰不到它。

设计上做了两件事保证不倒退：

1. **优先 native，失败回退 HTTP**。原生宿主没注册时 `connectNative` 会立刻
   抛错，`nativeState` 标记为 `missing` 后不再重试，老部署完全不受影响。
2. **native 只是转发器**，业务逻辑一行都不重复：它用 `urllib` 显式
   `ProxyHandler({})` 直连 127.0.0.1，把消息转给现有的 `web_bridge.py`。
   下载、落盘、打开目录的行为与纯 HTTP 方式完全一致。

诊断面板里的「本机软件」一行会写明走的是哪条通道。

---

---

## 六、抓不到图怎么排查（先看诊断，别猜）

按钮报「没找到影像」时，**双击按钮**会弹出诊断面板，里面直接写着卡在哪一环。
面板底部的「复制诊断信息」可以整段发出来。

面板里的几个数字对应的结论：

| 现象 | 含义 | 处理 |
|---|---|---|
| **钩子世界**「隔离世界 · 钩子无效」 | **先看这一行。** 下面所有数字都是假的，照着排查必然走偏 | 浏览器升到 111+；见下方「隔离世界」一节 |
| **版本**显示「未响应」 | 主世界钩子根本没跑起来 | 刷新页面（F5）。仍不行说明浏览器 < 111 且站点 CSP 拦了 script 注入 |
| **本机软件**「未连接」 | 扩展连不上软件 | 看后面的「直连探测」。若直连「可达」而本机软件「未连接」→ 装原生消息宿主（第四节第 2 步） |
| **直连探测**「不可达」 | 页面自己都连不上 127.0.0.1 | 软件没启动，或有代理/VPN 劫持回环。**这种情况只有原生通道能救** |
| 解析响应 **0 条** | 一条 JSON 都没拦到 | 页面还没取数，或者取数在 Worker 里。先点开序列再刷新 |
| 解析响应 > 0，序列 **0 个** | 拦到响应了，但里面没有影像清单 | **点开一个具体序列、能看到图像**，再点按钮。这一步最常见 |
| 序列 > 0，**无地址张数 > 0** | 识别到图像，但页面没下发地址 | 该部署只给 JPEG 预览。属部署限制 |
| 序列 > 0，张数很少（如 1~5） | 地址按张懒加载 | 在页面里把图像翻一遍，再点按钮 |
| **影像字段** 只有 `·rawKey,jpeg` | 该入口**没有原始 DICOM**，只有 JPEG 预览 | 能打开、能看，但不能测量/重建 |
| **影像字段** 含 `dicom` | 有原始 DICOM | 正常，优先下 DICOM |
| **CDN 基址** 为空 | 页面还没发过任何影像请求 | 翻一下图，或点开序列等图显示出来 |

「本机软件」后面括号里会写走的是**原生通道**还是 **HTTP 回环** —— 这决定了
出问题时该查哪一侧。

「影像字段」这一行直接回答了「这个部署到底给不给原始 DICOM」，不用再靠猜
结构反复试探。

**为什么「点开检查」不等于「有清单」**：鲁云的影像清单是按序列拉的
（`/v3/api/pat/image/{seriesUuid}`），只停在检查/序列列表页时前端内存里没有
这份数据，钩子自然什么也拿不到。这不是钩子失效。

### 「隔离世界」——换台电脑就失效的头号原因

诊断里最该先看的是**钩子世界**这一行。

浏览器世界里 JS 是分家的：页面的 `fetch` / `XHR` / `JSON.parse` 只存在于
**主世界**，扩展的 content script 跑在**隔离世界**，两者的 `window` 不是同一个
对象。钩子只有装在主世界才拦得到页面请求。

会有**两份** inject.js 同时存在（manifest 的 `world:"MAIN"` 一份、boot.js 的
`<script>` 注入一份）。Chrome < 111 时 manifest 那条会被**静默忽略**，只剩
隔离世界那份 —— 钩子照装、诊断照出、一条都抓不到，**全程没有任何报错**。

典型症状：换台电脑（或换个浏览器）后，诊断里出现这几个自相矛盾的数字：

```
解析响应  ：0 条（JSON.parse 命中 0）
真实影像址：0 条  ← 页面还没加载过任何一张图
已加载图片：https://images2.ftimage.cn/v3/...   ← 明明加载了 6 张
页面请求记录（共 1 条）· GET http://127.0.0.1:18923/ping   ← 只有我们自己的探测
```

**判据是最后那一行**：请求记录里只有扩展自己发的 `/ping`，页面自己的请求一条
都没有 → 钩子和 content.js 在同一个（隔离）世界，压根没覆盖到页面。

还有一个坑：旧版诊断**取先回应的那一份**。隔离世界那份常常先回（数据恒空），
把主世界那份的真数据盖掉 —— 于是「明明能取图，诊断却显示 0 张」。1.4.3 起
collect / diagnose 都只认 `world: "main"` 的那份。

处理：
1. **浏览器升到 Chrome 111+**（或改用 Edge 新版）——绝大多数情况是这条。
   360 / QQ / 医院受管 Chrome 的内核版本常常不够。
2. 升级后诊断仍显示隔离世界，才是 CSP 拦了 script 注入。
3. 权宜之计：点「重新收集」。1.4.3 起会从 `<img>` 和 `performance` 里兜底，
   把页面**已经渲染出来的**那几张直接带出去（拿不到完整序列，但至少能用）。

---

## 七、现场最容易踩的四个点

1. **先点开序列、看到图像，再点按钮。**
   按钮上没显示「· N 张」时不要点 —— 那说明还没拿到清单。刷新后立刻点也常常
   来不及，因为页面自己还在拉数据。

2. **影像不在站点域名下。**
   相对路径要拼到 CDN 根（`https://images2.ftimage.cn/v3`）才对，拼到
   `<origin>/` 一定是 404。扩展从浏览器真实请求里反推 CDN 根，并把这个根作为
   首选候选；`web_bridge._probe_prefixes` 再用该序列第一张探一次、把能通的那个
   提到首位，然后整批并发（否则 300 张会白跑 600 次请求）。
   **整批全失败时先看 `web_bridge.log`**：写「返回了 HTML」是被重定向到登录页；
   写「响应过短：no resource found」说明 CDN 路径拼错（少了位或多了段）。

   ⚠️ **v1.4.0 修了一个致命时序 bug**：CDN 根只有等浏览器真的请求过一张图
   才知道，而旧代码在「解析到清单」的那一刻就把 URL 拼死了，那时 CDN 根还是
   空的 → 只能拼站点域名 → 419 张全部失败。现在只存原始相对路径，
   **点按钮时才拼**（`snapshot → resolveUrls`）。改动者务必守住这个时序：
   任何在 `addImage` 里调 `candidates()` 的写法都会让功能整体失效。

3. **HttpOnly Cookie 拿不到。**
   `document.cookie` 读不到 HttpOnly。多数云影像的图片直链是免鉴权的 CDN 路径，
   不依赖它；真遇到 403 才需要给扩展加 `cookies` 权限
   （代价是多一条「读取所有网站数据」的安装警告）。

4. **医院 Chrome 可能是受管版本。**
   企业策略常禁止加载未打包扩展。这时要么下发 `.crx` + 策略白名单，
   要么直接走 C（Native Messaging + 企业统一安装）。

补充两条**只会在现场出现**的：

5. **进度停在 `0/N` 不动**，通常不是一个原因，而是两个叠加：
   - 扩展的 `getTask()` 原来只走 HTTP。原生模式下 `import` 响应会带回真实
     `port`，于是前端拿着它去 `fetch`，又被代理/VPN 拦掉 → `/task` 一次都到
     不了软件端（`web_bridge.log` 里 `GET /task` 计数为 0 就是它）。
     现在 `getTask` **先走原生**，失败才退回 HTTP。
   - 下载端对 4xx 也做重试：419 张 × 4 个候选 × 2 次 + 0.6s 等待 ≈ 十几分钟，
     界面上就是完全不动。现在 4xx/假 200 一律**不重试**，并且连续 12 张
     确定性失败就**熔断**，秒级给出可操作的错误。

6. **`series` 数等于图片数**（例如 419 张显示 419 个序列）。
   V3 的路径有时多一层 instance（`…/<study>/<series>/<inst>/<rawKey>`，4 段 hex），
   按「倒数第二段」取到的是 instance，每张都不同。现在只在**恰好 3 段 hex**
   时才认序列号，其余归一组 —— 知骨打开时会按 DICOM 的 SeriesInstanceUID
   自行分组，目录只是物理落点。

---

## 八、已知限制

- **像素流模式取不到原始 DICOM**（`imageGrpcHost` 非空的部署）。
- **只给 JPEG 预览的部署只能看、不能量**。V3 移动版常见 `imageResult` 里只有
  `jpeg` 字段，下回来是 8bit 压缩图，没有窗宽窗位、没有像素间距，
  三维重建和测量都用不了。软件会明确提示「仅 JPEG 预览」而不是假装成功。
- **代理 / VPN 会掐断 HTTP 回环**。这是原生消息通道存在的理由；
  没注册原生宿主时只能走 HTTP，这类机器上会失败。
- 只监听回环，**不对外网开放**；`/import` 只接受 `chrome-extension://` 来源
  或带 `X-Zhigu-Bridge` 头（跨域加这个头会先发预检，恶意页面过不了）。
- 下载目录在 `%TEMP%\zhigu_web_*`，目前**不自动清理**。长期使用建议加一条
  启动时清理 7 天前目录的逻辑。
- 上传到 PACS 的逆向流程（把本机处理结果推回云影像）不在本方案范围内。

---

## 九、内网 PACS（WADO / WebDicomView）适配

现场样本：**「临床系统」前端 + WebDicomView 阅片器**（实测 172.123.3.5:11519
出报告界面、:8989 出影像）。它和鲁云 ftimage 是两套完全不同的东西，硬套会一条都抓不到。

### 9.1 它没有"影像清单接口"

| 探测 | 结果 |
|---|---|
| 页面自带的取图接口 | 只有 4 个：`GetStudyInfo`、`GetSeriesMeta`、`GetImagesPath`、`GetImageThumb`、`GetBlock` |
| 影像清单 JSON | **不存在**。图像 id 只在 `GetStudyInfo` 的一次响应里下发，之后按 id 单张取 |
| 单张取图 | `GET {wado}/GetBlock/{imageId}?studyuid=..&hospitalid=..&seriesId=..&blockIndex=0&imageType=1` |
| 鉴权 | 靠**会话 Cookie**（HttpOnly）；`document.cookie` 读不到 |

所以适配的核心不是"抓 URL"，而是**读懂阅片器的内存模型，把 URL 拼出来**。

### 9.2 数据模型（字段名一个都不能少）

`GetStudyInfo` 的响应 → 阅片器 `studyArr[i]`：

```js
{
  studyuid, studyuuid, name, sex, age, modality, imgcount, seriescount,
  hasseries, totaltype,            // totaltype=JPG 表示该检查只有 JPEG 预览
  serieses: [ { uuid, uid, num, desp, sign, flatSeries,
                imgs: [ { id, num, url?, acqtm? } ] } ],
  imgs: [...]                      // hasseries 为假时用它（此时只有缩略图）
}
```
阅片器再包一层：`studyArr[i].arrSeries[j].arrImage[k] = { id, num }`。

**两个必踩的坑**：

1. **`seriesId` 必须去掉 query**。`flatSeries` 模式下阅片器会把 uuid 改写成
   `<uuid>?index=N` 再存进 `arrSeries`；原样拼进 GetBlock 会把查询串截断，
   服务端只回一张或者直接 500。
2. **`imageId` 不要 URL 编码**。厂商是原样拼接的，编码后反而匹配不上
   （真出现特殊字符时，走 9.3 的第 ③ 条真地址）。

### 9.3 三条取数路径（按可信度，自动降级）

| 顺序 | 来源 | 特点 |
|---|---|---|
| ① | `window.webDicomView.studyArr[].arrSeries[].arrImage[].id` | 最完整，含用户没翻到的那些张 |
| ② | `GetStudyInfo` 的响应 JSON（钩在 `JSON.parse` / XHR 上） | ① 拿不到时兜底；也覆盖"钩子装晚了" |
| ③ | 浏览器真实请求过的 `/GetBlock/` 地址（`performance` + XHR 记录） | 永远正确，但只有翻过的那几张；同时给 ① ② 当验货标准 |

①② 拼出的 URL 与页面自身的地址形状完全一致（`blockIndex=0&imageType=1`），
③ 命中时优先用原始地址（它带着 `v=<采集时间>`，重拼会丢）。

### 9.4 插件端的四处配套改动（1.5.0）

| 改动 | 为什么 |
|---|---|
| `inject.js` 加 WADO 适配层 | 见 9.3；独立命名空间，**不动** ftimage 那套逻辑 |
| `content.js` 认「影像在 iframe 里」的页面 | 临床系统的顶层页标题是「临床系统」、路径 `/content?reportId=`，按 URL/标题判断不出是 PACS，按钮根本不会出现。现在认 `#dicomview` / `.cloudFilm` / iframe 的 src |
| 子帧 → 顶层帧的 `frameReady` 广播 | 数据在 8989 的 iframe、按钮在 11519 的顶层；不主动通报的话按钮永远不带张数 |
| `manifest` 加 `cookies` 权限 + 放开 `host_permissions` | 会话 Cookie 是 HttpOnly，只能 `chrome.cookies` 读；同时把 `referer` 换成**采集帧**的地址（顶层页地址会被防盗链 403） |

`cookies` 权限会让安装时多一条「读取所有网站数据」的提示。这是内网部署里
唯一能拿到 HttpOnly 会话的手段；没授权也不会崩，只是回退到 `document.cookie`
（那种情况下 GetBlock 多半返回登录页，软件端会明确报"全部下载失败"）。

### 9.5 现场排查

双击按钮看诊断面板，内网 PACS 只需盯这几行：

| 面板显示 | 含义 | 处理 |
|---|---|---|
| `内网 Wado ：未识别到 viewer 对象` + `有 viewer 但没读到图像 id` | 阅片器还没初始化完 | 等图出来（能翻张）再点 |
| `内网 Wado ：未识别到 viewer 对象`（且 URL 里没有 `/Wado`） | 这套不是 WebDicomView | 按云影像那套走 |
| 有张数但软件端全部失败、日志写「返回了 HTML」 | 缺会话 Cookie | 重新授权扩展的 cookies 权限；确认浏览器里影像能正常显示 |
| `仅 JPEG 预览` | 该检查没下发原始 DICOM | 只能看，不能测量/重建 |
| `真实地址 0 条` 且张数对 | 用户一张都没翻过，URL 全是拼的 | 翻一两张再点（不是必须，但能提高成功率） |

### 9.6 离线验证

不接内网也能验（两个脚本都不进发行包）：

```bash
node _tools/test_web_bridge_wado.js
CT3D260414_release/pyenv/python.exe -X utf8 _tools/test_web_bridge_wado_download.py
```
前者用假 `window`/`document` 跑 inject.js，覆盖「viewer 对象 / 只有 JSON /
只有真实请求」三种页面状态，外加一条「鲁云那套不能被带坏」的回归；
后者起一个假 PACS（`/Wado/GetBlock` 返回真 DICOM、缺 Cookie 就回登录页），
把下载、落盘、`cookieMap`、`referer` 全跑一遍。

---

## 十、文件职责

| 文件 | 职责 | 改它的时候注意 |
|---|---|---|
| `chrome-extension/inject.js` | 主世界钩子，抓影像清单（ftimage）+ 读阅片器模型拼 WADO 地址 | 钩子里抛异常会直接毁掉页面，全部 try/catch；**`JSON.parse` 的预筛正则必须含 `imageResult`/`rawKey`**（鲁云）和 `serieses`/`hasseries`（内网 PACS），缺一个就整条静默跳过 |
| `chrome-extension/boot.js` | 把 inject.js 送进主世界 | 兜 Chrome < 111 时 `world:"MAIN"` 静默失效 |
| `chrome-extension/content.js` | 按钮 UI + 状态机 + 诊断面板 | Shadow DOM 隔离，别用全局 class |
| `chrome-extension/background.js` | 跨域 fetch + 跨 iframe 清单缓存 | service worker 会被休眠，不要存关键状态 |
| `web_bridge.py` | 协议/单实例/HTTP/下载 | 只用标准库（包里没有 requests）；**扩展名按响应魔数定，不能按字段名猜** |
| `web_bridge_setup.py` | 注册 zhigu:// 协议 | 用 Python 写注册表，避 bat/ps1 编码坑 |
| `web_bridge_native.py` | 原生消息宿主（stdio 转发） | **stdout 只能有协议数据**；必须 `ProxyHandler({})` 禁用代理 |
| `web_bridge_native.bat` | 原生消息启动器 | 纯 ASCII，用 `%~dp0`；**不能 echo** |
| `web_bridge_native_setup.py` | 注册原生消息宿主 | `EXT_ID` 必须与 manifest 里 `key` 推出的 ID 一致 |
