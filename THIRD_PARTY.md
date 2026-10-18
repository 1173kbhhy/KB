# 第三方资源声明（THIRD_PARTY）

本项目**不随仓库分发**以下第三方资源。请使用者自行获取，并遵守其各自的许可证。

---

## 1. 开机动画：dsh-startup-screen

| 项目 | 说明 |
|---|---|
| 来源仓库 | `6shenhonghong9/dsh-startup-screen`（GitHub） |
| 用途 | NOVA AI 的启动动画（Canvas 光场 + 本地语音播报） |
| 涉及文件 | `assets/splash.js`（运行时）、`assets/splash.css`（样式）、`assets/dsh_voice/line-1..6.mp3`（语音素材） |
| 获取方式 | 运行 `python scripts/fetch_splash_assets.py` |

**许可证**：请以上游仓库中的 `LICENSE` 文件为准。使用前请自行确认并遵守。

**关于商标**：该动画的视觉元素涉及 DeepSeek 商标。本项目仅将其作为**可选的启动界面**用于学习与个人用途，不代表与 DeepSeek / 深度求索有任何关联或背书关系。若需商用或正式分发，请先移除该动画资源。

### 获取脚本做了什么

`scripts/fetch_splash_assets.py` 会从 GitHub 拉取上述文件到 `assets/` 目录。脚本本身不修改任何文件内容，也不重新分发受版权保护的素材。

### 如果你不想要这个动画

删除 `assets/splash.js` 与 `assets/splash.css` 即可。程序会检测到资源缺失并**跳过启动动画**，直接进入聊天界面（详见 `app.py` 中 `do_GET` 对 `/` 的处理）。

---

## 2. 依赖的第三方库

以下为通过 `pip` 安装的依赖，各自许可证见其官方仓库：

| 库 | 用途 | 许可证 |
|---|---|---|
| `pywebview` | 原生窗口（Edge WebView2） | BSD-3-Clause |
| `mcp`（Model Context Protocol SDK） | MCP 客户端/服务端 | MIT |
| `qdrant-client` | 本地向量数据库 | Apache-2.0 |
| `fastmcp` | MCP 服务端框架 | Apache-2.0 |
| `python-dotenv` | 读取 `.env` | BSD-3-Clause |
| `firecrawl-py` | 联网检索（可选） | MIT |
| `pymupdf4llm` | PDF 转 Markdown | AGPL-3.0 ⚠️ |

> ⚠️ **关于 `pymupdf4llm`（AGPL-3.0）**
>
> PyMuPDF 采用 AGPL-3.0 许可证，属于**强 copyleft**。本项目仅在**读取 PDF** 时按需导入它（`rag_mcp_server.py` 中为延迟导入），但：
>
> - 若你要把本项目用于**闭源商业用途**，请注意 AGPL 的传染性义务，建议改用 `pypdf`（BSD 许可）替代。
> - 若你仅个人学习使用，AGPL 无实际限制。
>
> 这是选用该库时必须知晓的合规点，此处如实标注。

---

## 3. 模型

| 模型 | 用途 | 说明 |
|---|---|---|
| `qwen3:8b` | 主对话模型 | 由 Ollama 提供，遵循 Qwen 许可证 |
| `nomic-embed-text` | 嵌入模型 | 遵循 Nomic AI 许可证 |

本仓库**不包含任何模型权重**，使用者需自行从 Ollama 官方库拉取。

---

## 4. 桌面图标

`assets/` 下的应用图标为 AI 生成的原创素材，未使用任何第三方商标或素材。

---

*最后更新：2026-10-18*
