<div align="center">

# NOVA AI

**基于 Ollama 的本地桌面 AI 助手 · 完全离线 · 数据不出本机**

</div>

---

一个用 Python 写的桌面聊天客户端，把本地大模型（Ollama）装进原生窗口里，
再通过 MCP 协议接上本地知识库和长期记忆。

**没有云 API，没有账号，没有数据上传。** 拔掉网线照样能用。

---

## 这是什么

普通聊天界面 + 三个让它真正好用的能力：

| 能力 | 解决什么问题 |
|---|---|
| **本地知识库** | 把你的 PDF / Markdown / TXT 丢进文件夹，自动建立语义索引，回答时检索相关片段并标注来源 |
| **长期记忆** | 一个 `memory.md` 文件，每次对话自动注入模型的系统提示，模型知道你是谁、在做什么 |
| **联网检索** | 可选开关。开启后模型可检索互联网（需要自备 API Key） |

外加一个**评估基线**脚本：改了提示词或检索参数后，用同一套题跑一遍，用数字告诉你改动是变好还是变差。

---

## 效果

启动界面可选接入一个第三方动画组件（见 [第三方资源](#第三方资源)）；不安装则直接进入聊天界面。

<p align="center">
  <em>界面：左侧为模型列表、显存占用、性能设置与释放模式，右侧为对话区</em>
</p>

---

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows（已在 Windows 11 验证） |
| Python | 3.10+ |
| Ollama | 0.40.6 或更高 |
| 内存 | 建议 16 GB |
| 显存 | 6 GB 可运行 8B Q4 量化模型 |

不需要显卡也能跑，只是会慢很多（CPU 推理）。

---

## 安装

### 1. 准备 Ollama 与模型

```bash
# 安装 Ollama 后，拉取主模型与嵌入模型
ollama pull qwen3:8b
ollama pull nomic-embed-text
```

### 2. 克隆本项目

```bash
git clone <your-repo-url>
cd <repo-dir>
```

### 3. 安装 Python 依赖

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux / macOS

pip install pywebview mcp fastmcp qdrant-client python-dotenv firecrawl-py pymupdf4llm
```

### 4. 配置

```bash
copy .env.example .env          # Windows
# cp .env.example .env          # Linux / macOS
```

打开 `.env`，**至少确认这两项**：

```ini
FIRECRAWL_API_KEY=              # 留空即可，只用本地功能时不需要
RERANK_MODE=lexical             # 检索重排模式，见下文
```

> `.env` 不会被提交到仓库（已在 `.gitignore` 中）。

### 5.（可选）获取启动动画

```bash
python scripts/fetch_splash_assets.py            # 直连
python scripts/fetch_splash_assets.py --mirror   # 网络受限时走镜像
```

跳过这一步也能正常运行，只是没有启动动画。

### 6. 启动

```bash
python app.py
```

---

## 使用

### 组织知识库

把 `.md` / `.txt` / `.pdf` 放进 `kb_data/`，**支持子目录**。下次启动自动索引，无需手动操作。

```
kb_data/
├── 01-分类A/
│   ├── 某主题笔记.md
│   └── 子目录/更多资料.md      ← 子目录里的也会被索引
├── 02-分类B/
└── README.md
```

### 编辑长期记忆

界面左侧「长期记忆文档」→ 点「编辑记忆」→ 直接改 → 保存即生效。

`memory.md` 永远保留为你可编辑的原文；系统另存一份压缩摘要用于长对话，两者分离。

### 性能调优

界面左侧「性能设置」可实时调整：

- **上下文长度**（`num_ctx`）—— 显存紧张时调小
- **GPU 层数**（`num_gpu_layers`）—— 调小可把显存让给上下文；`0` 表示纯 CPU
- **KV Cache 量化** —— `q8_0` 推荐，省显存且质量损失小

设置自动持久化，重启保留。改动**下一条消息即生效**。

---

## 检索重排序

知识库检索采用标准两阶段策略：**向量粗召回 → 重排精排**。

```
RERANK_MODE=off        纯向量，最快
RERANK_MODE=lexical    混合打分（默认，零额外依赖，中文友好）
RERANK_MODE=ollama     神经重排，质量最好但更慢
RERANK_MODE=hybrid     神经 + 词法
```

**一个实测发现**：知识库文档较少时（比如少于 10 篇），`off` 的表现往往不输 `lexical`——
因为候选池太小，重排没有优化空间，反而可能因词法权重把正确结果压下去。
建议文档变多后再切 `ollama`，并用下面的评估脚本对比。

### 神经重排模型（可选）

```bash
ollama pull dengcao/Qwen3-Reranker-0.6B
```

打分会短暂占用显存。设置 `RERANK_KEEP_ALIVE=5s` 可让打分后立即释放，
避免与主模型抢占资源。

---

## 评估基线

改完提示词或检索参数后，跑一遍评估看真实影响：

```bash
# 完整评估（约 3 分钟）
venv\Scripts\python.exe eval/eval.py --tag my-change

# 只测检索（秒级，用于快速对比重排开关）
venv\Scripts\python.exe eval/eval.py --only-retrieval --rerank off
venv\Scripts\python.exe eval/eval.py --only-retrieval --rerank lexical

# 裸模型基线（不给知识库）
venv\Scripts\python.exe eval/eval.py --no-kb
```

三项指标：

- **检索命中率** —— 该召回的片段是否召回了
- **忠实度** —— 回答是否真的基于检索内容；**负样本专门测「知识库没有时会编造吗」**
- **格式合规率** —— 输出是否非空、是否跑偏

结果写入 `eval/report.md`（含历史对比表）与 `eval/report.csv`。

评估使用 `temperature=0` + 固定 seed，结果可复现。

---

## 安全设计

本项目只监听 `127.0.0.1`，不对局域网暴露。即便如此，仍做了几层防护：

- **会话令牌** —— 每次启动随机生成；所有接口需携带令牌，防止本机其它程序越权调用
- **Host 校验** —— 阻断 DNS rebinding 攻击
- **关闭 CORS 通配** —— 防止恶意网页跨域读取你的记忆与聊天记录
- **路径穿越防护** —— 静态资源路径归一校验，禁止越出资源目录
- **只绑定回环地址** —— 从设计上就不提供对外访问

⚠️ 本项目未经过第三方安全审计。若需暴露到网络，请自行加装 TLS。

---

## 已知限制

诚实列出，避免踩坑：

- **界面为中文** —— 前端文案未做国际化
- **仅验证过 Windows** —— 用了 `ctypes` 调用 Win32 API（单实例、窗口激活），其他平台需改写这部分
- **启动动画为第三方资源** —— 不随仓库分发，且涉及他人商标，仅供学习参考
- **`pymupdf4llm` 为 AGPL-3.0** —— 闭源商用请改用 `pypdf`，详见 [第三方资源](#第三方资源)
- **模型能力取决于所选模型** —— 8B 级别模型在长链路推理与严格格式输出上会有明显短板
- **联网检索依赖第三方 API** —— 不配置 Key 时该功能关闭，其余功能不受影响

---

## 项目结构

```
.
├── app.py                      # HTTP 服务 · 原生窗口 · MCP 桥接 · 记忆压缩
├── index.html                  # 聊天主界面
├── splash.html                 # 启动界面（可选）
├── rag_mcp_server.py           # 知识库 MCP 服务（FastMCP / stdio）
├── eval/
│   ├── eval.py                 # 评估脚本
│   └── eval_set.jsonl          # 评测集
├── scripts/
│   └── fetch_splash_assets.py  # 动画资源获取
├── kb_data/                    # 知识库目录（放你的资料）
├── assets/                     # 前端资源（动画需自行下载）
└── .env.example                # 配置示例
```

---

## 第三方资源

本仓库**不分发**以下内容，请自行获取并遵守其许可证：

- **启动动画** —— 来自 `6shenhonghong9/dsh-startup-screen`，运行 `scripts/fetch_splash_assets.py` 获取
- **`pymupdf4llm`** —— AGPL-3.0 强 copyleft，闭源商用请替换

完整声明见 [`THIRD_PARTY.md`](THIRD_PARTY.md)。

---

## 许可

MIT —— 见 [`LICENSE`](LICENSE)。
