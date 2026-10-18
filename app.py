# -*- coding: utf-8 -*-
"""Ollama 本地桌面聊天客户端（原生窗口，非浏览器）
- 启动前自动检查/拉起 Ollama
- 内嵌 HTTP 服务代理 Ollama API
- 用 pywebview 渲染聊天界面（Edge WebView2 原生窗口）
- 通过 MCP(stdio) 桥接本地 RAG 知识库 + 联网检索，实现「知识库 / 联网」增强
"""
import asyncio
import ctypes
import hashlib
import hmac
import html as _html
import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import subprocess
import sys

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

import webview
from dotenv import load_dotenv

load_dotenv()

OLLAMA = "http://localhost:11434"
HISTORY_DIR = r"D:\ollama"
HISTORY_FILE = os.path.join(HISTORY_DIR, "chat_history.json")
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8080

# ── 长期记忆文档（每次启动自动读取，注入系统提示） ─────────────────────
MEMORY_FILE = os.path.join(SERVER_DIR, "memory.md")
APP_STATE = {"memory": ""}

# ── 安全：会话令牌 / 来源校验 ───────────────────────────────────────────
# 每次启动随机生成一个令牌，前端页面里注入，所有 /api/* 必须带上。
# 这样别的网站（哪怕在本机浏览器里开着）无法读取你的记忆/历史，
# 也无法调用「彻底退出 Ollama」这类破坏性接口。
SESSION_TOKEN = secrets.token_urlsafe(32)

# 只允许这些 Host 头（127.0.0.1/localhost + 端口），阻断 DNS rebinding
ALLOWED_HOST_PREFIXES = ("127.0.0.1", "localhost", "[::1]", "::1")


def _is_safe_model_name(name):
    """模型名白名单校验：只允许 Ollama 命名规范，杜绝把参数当可执行文件。"""
    if not isinstance(name, str) or not name or len(name) > 128:
        return False
    if ".." in name or name.startswith(("/", ".", "-")) or "//" in name:
        return False
    # 允许 namespace/model:tag 形态，但字符集必须干净
    return all(ch.isalnum() or ch in ":._-/" for ch in name)


def _safe_asset_path(rel: str):
    rel = urllib.parse.unquote(rel or "").lstrip("/\\")
    if not rel:
        return None
    base = os.path.realpath(os.path.join(SERVER_DIR, "assets"))
    target = os.path.realpath(os.path.join(base, rel))
    # 必须仍在 assets 目录内（含目录本身），否则拒绝
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


# ── 单实例：只允许一个窗口，重复双击时激活已有窗口 ───────────────────────
WINDOW_TITLE = "NOVA AI"
MUTEX_NAME = "NOVAAI_Chat_SingleInstance_Mutex"
APP_MARKER = "NOVAAI-CHAT-APP"
_MUTEX_HANDLE = None

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_user32.FindWindowW.restype = ctypes.c_void_p
_user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
_user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
_user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.AttachThreadInput.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_bool]


def acquire_single_instance():
    """用 Windows 命名互斥体做单实例锁。

    返回 True  → 本进程是唯一实例，可以正常启动。
    返回 False → 已有实例在运行（本进程应退出，并把那个窗口顶到前面）。
    进程被杀/崩溃时系统会自动释放互斥体，不会留下死锁。
    """
    global _MUTEX_HANDLE
    try:
        ctypes.set_last_error(0)
        _MUTEX_HANDLE = _kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if not _MUTEX_HANDLE:
            return True  # API 异常就放行，别把用户挡在门外
        return ctypes.get_last_error() != 183  # 183 = ERROR_ALREADY_EXISTS
    except Exception:
        return True


def focus_existing_window():
    """找到已存在的 NOVA AI 窗口并把它恢复到前台。"""
    try:
        hwnd = _user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        _user32.ShowWindow(hwnd, 9)  # SW_RESTORE：先取消最小化
        # 解除 Windows 的「前台锁定」，否则后台进程无权抢焦点
        cur_tid = _kernel32.GetCurrentThreadId()
        tgt_tid = _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None)
        attached = False
        if tgt_tid and tgt_tid != cur_tid:
            attached = bool(
                _user32.AttachThreadInput(cur_tid, tgt_tid, True)
            )
        _user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        _user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        if attached:
            _user32.AttachThreadInput(cur_tid, tgt_tid, False)
        return True
    except Exception as e:
        print("[单实例] 激活已有窗口失败：", e)
        return False


def load_memory():
    """启动时读取记忆文档（与开机动画并行，不阻塞界面）。"""
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                APP_STATE["memory"] = f.read().strip()
            print(f"[记忆] 已载入 memory.md（{len(APP_STATE['memory'])} 字）")
        # 上次生成的摘要也读回来
        if os.path.exists(MEMORY_COMPACT_FILE):
            with open(MEMORY_COMPACT_FILE, "r", encoding="utf-8") as f:
                APP_STATE["memory_compact"] = f.read().strip()
    except Exception as e:
        print("[记忆] 读取失败：", e)


def warmup_model(model="qwen3:8b"):
    """后台预热模型：在开机动画播放期间把权重加载进显存，
    这样用户进入聊天后首条消息几乎无需等待。keep_alive 让模型多留一会儿。"""
    try:
        print("[预热] 正在加载模型（与开机动画并行）:", model)
        wp = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "keep_alive": "10m",
        }
        code, _ = ollama_request("/api/chat", "POST", wp, timeout=180)
        print("[预热] 完成，状态码", code)
    except Exception as e:
        print("[预热] 跳过：", e)


# ── 记忆摘要压缩（任务③）────────────────────────────────────────────────
# 记忆原文永远保留在 memory.md（你的可编辑真源），
# 这里额外维护一份「压缩摘要」，超长时只把摘要注入对话，省 token 也更稳。
MEMORY_COMPACT_FILE = os.path.join(SERVER_DIR, "memory.compact.md")
MEMORY_FULL_LIMIT = 2200      # 记忆原文超过这个字数才值得压缩
MEMORY_COMPACT_TARGET = 700   # 压缩后目标字数
HISTORY_KEEP_TURNS = 8        # 会话滑动窗口：只保留最近 N 轮原文
HISTORY_SUMMARY_LIMIT = 1200  # 旧对话滚动摘要的字数上限
APP_STATE["memory_compact"] = ""
APP_STATE["memory_hash"] = ""      # 原文指纹，用于判断摘要是否过期
APP_STATE["conv_summary"] = ""     # 旧对话的滚动摘要
APP_STATE["conv_hash"] = ""


def _llm_summarize(text, instruction, max_tokens=900):
    """用本地模型做一次非流式摘要。失败返回 None。"""
    if not text.strip():
        return None
    payload = {
        "model": "qwen3:8b",
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "think": False,
        "options": {"num_predict": max_tokens, "temperature": 0.2},
    }
    try:
        code, body = ollama_request("/api/chat", "POST", payload, timeout=240)
        if code != 200:
            return None
        data = json.loads(body)
        out = (data.get("message") or {}).get("content", "").strip()
        return out or None
    except Exception as e:
        print("[压缩] 摘要调用失败：", e)
        return None


def compact_memory(force=False):
    """把 memory.md 压缩成 memory.compact.md。

    返回 (说明文字, 错误)。原文不动，只是额外产出一份短摘要。
    """
    raw = APP_STATE.get("memory", "")
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    if not force and APP_STATE.get("memory_hash") == h and APP_STATE.get("memory_compact"):
        return ("摘要已是最新（原文未变化）。", None)
    if not force and len(raw) <= MEMORY_FULL_LIMIT:
        return (f"记忆仅 {len(raw)} 字，未超过 {MEMORY_FULL_LIMIT} 字，无需压缩。", None)
    if not raw.strip():
        return ("记忆文档为空。", None)

    print(f"[压缩] 正在压缩记忆（{len(raw)} 字 → 目标 {MEMORY_COMPACT_TARGET} 字）…")
    instr = (
        "你在压缩用户的长期记忆文档。请提炼其中**对未来对话仍然有用**的信息，"
        "用紧凑的中文条目式列出，必须保留：身份背景、技术栈与模型、正在备考/在学的内容、"
        "工具与路径、明确表达的偏好与禁忌、以及已确定的事实结论。"
        "删掉寒暄、重复和临时性内容，不要新增原文没有的信息，不要输出解释性前后缀。"
        f"总长度控制在 {MEMORY_COMPACT_TARGET} 字以内。"
    )
    out = _llm_summarize(raw, instr, max_tokens=1100)
    if not out:
        # 降级：至少做个确定性的头尾裁剪，别让功能完全不可用
        head = raw[: int(MEMORY_COMPACT_TARGET * 0.7)].rstrip()
        tail = raw[-int(MEMORY_COMPACT_TARGET * 0.3):].lstrip()
        out = head + "\n…（中间已省略，可点「编辑记忆」查看全文）…\n" + tail
        note = "（本地模型未就绪，本次为规则裁剪，建议稍后重试）"
    else:
        note = ""
    try:
        with open(MEMORY_COMPACT_FILE, "w", encoding="utf-8") as f:
            f.write(out)
    except Exception as e:
        return ("", f"写入摘要失败：{e}")
    APP_STATE["memory_compact"] = out.strip()
    APP_STATE["memory_hash"] = h
    msg = f"已压缩：{len(raw)} 字 → {len(out)} 字{note}"
    print("[压缩]", msg)
    return (msg, None)


def memory_for_prompt():
    """决定注入对话的记忆文本：短的用原文，长的用摘要。"""
    raw = APP_STATE.get("memory", "")
    if not raw:
        return "", False
    if len(raw) <= MEMORY_FULL_LIMIT:
        return raw, False
    comp = APP_STATE.get("memory_compact", "")
    if comp and APP_STATE.get("memory_hash") == hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:16]:
        return comp, True
    return "", False


def summarize_old_turns(messages):
    """会话滑动窗口：把靠前的旧对话压成滚动摘要，只保留最近几轮原文。"""
    keep = HISTORY_KEEP_TURNS
    if len(messages) <= keep:
        return messages, ""
    older, recent = messages[:-keep], messages[-keep:]
    blob = "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}：{m.get('content','')}"
        for m in older
    )
    h = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    prev = APP_STATE.get("conv_summary", "")
    if APP_STATE.get("conv_hash") == h and prev:
        return recent, prev
    merged = ((prev + "\n") if prev else "") + blob
    instr = (
        "你在压缩一段较早的对话记录。请合并下面内容（含此前的摘要）成一份简洁的中文"
        "「对话要点」，保留：已确定的结论、用户表达过的要求或偏好、做过的关键决定。"
        "丢弃过程性内容、寒暄与重复。不要新增内容，不要输出解释性前后缀。"
        f"总长度不超过 {HISTORY_SUMMARY_LIMIT} 字。"
    )
    out = _llm_summarize(merged[-12000:], instr, max_tokens=800)
    if not out:
        return recent, ""  # 压缩失败就只用最近几轮，宁可少信息也别塞垃圾
    out = out[: HISTORY_SUMMARY_LIMIT * 2]
    APP_STATE["conv_summary"] = out
    APP_STATE["conv_hash"] = h
    return recent, out


# ── MCP 桥接管理器 ──────────────────────────────────────────────────────
class MCPBridge:
    """在后台线程运行 asyncio 事件循环，常驻连接本地 RAG MCP 服务器(stdio)。"""
    def __init__(self):
        self.loop = None
        self.thread = None
        self.session = None
        self.ready = False
        self.status = "未启动"
        self._shutdown = None

    def start(self):
        self._shutdown = asyncio.Event()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        # 等待连接就绪（最多 60 秒，首次可能要建集合）
        for _ in range(120):
            if self.ready:
                break
            time.sleep(0.5)
        return self.ready

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except Exception as e:
            self.status = f"桥接异常：{e}"
            print("[MCP] bridge error:", e)

    async def _serve(self):
        venv_py = os.path.join(SERVER_DIR, "venv", "Scripts", "python.exe")
        server_script = os.path.join(SERVER_DIR, "rag_mcp_server.py")
        env = dict(os.environ)
        # 把 FIRECRAWL_API_KEY 透传给子进程
        fk = os.getenv("FIRECRAWL_API_KEY", "")
        if fk:
            env["FIRECRAWL_API_KEY"] = fk
        env["OLLAMA_BASE_URL"] = OLLAMA
        env["RAG_DATA_DIR"] = os.path.join(SERVER_DIR, "kb_data")
        env["RAG_QDRANT_PATH"] = os.path.join(SERVER_DIR, "qdrant_data")

        params = StdioServerParameters(
            command=venv_py, args=[server_script], env=env, cwd=SERVER_DIR
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    self.ready = True
                    self.status = "已连接（知识库可用）"
                    print("[MCP] RAG server connected")
                    await self._shutdown.wait()
        except Exception as e:
            self.status = f"RAG 服务器启动失败：{e}"
            self.ready = False
            print("[MCP] failed to start:", e)

    def _call(self, coro_factory, timeout=120):
        """coro_factory: 返回协程的可调用对象，仅在就绪时才会被调用。"""
        if not self.ready or self.session is None or self.loop is None:
            return None, self.status
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), self.loop)
        try:
            result = fut.result(timeout=timeout)
            return result, None
        except Exception as e:
            return None, str(e)

    def search_kb(self, query, top_k=4):
        async def _c():
            r = await self.session.call_tool(
                "knowledge_base_search",
                arguments={"query": query, "top_k": top_k},
            )
            return _tool_text(r)
        return self._call(_c)

    def web_search(self, query):
        async def _c():
            r = await self.session.call_tool("web_search", arguments={"query": query})
            return _tool_text(r)
        return self._call(_c)

    def ingest(self, filepath, title=""):
        async def _c():
            r = await self.session.call_tool(
                "ingest_document", arguments={"filepath": filepath, "title": title}
            )
            return _tool_text(r)
        return self._call(_c)


def _tool_text(result):
    """从 MCP CallToolResult 中提取文本。"""
    if result is None:
        return ""
    parts = []
    try:
        for c in result.content:
            if getattr(c, "type", "") == "text":
                parts.append(c.text)
    except Exception:
        pass
    return "\n".join(parts)


BRIDGE = MCPBridge()


# ── Ollama 辅助 ─────────────────────────────────────────────────────────
def ensure_history_dir():
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
    except Exception:
        pass


def ensure_ollama():
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2)
        return True
    except Exception:
        pass
    ollama_exe = os.path.expanduser(r"~\\AppData\\Local\\Programs\\Ollama\\ollama.exe")
    if not os.path.exists(ollama_exe):
        return False
    try:
        subprocess.Popen([ollama_exe], creationflags=0x00000008)
    except Exception:
        return False
    for _ in range(40):
        time.sleep(1)
        try:
            urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2)
            return True
        except Exception:
            pass
    return False


def ollama_request(path, method="GET", data=None, timeout=300):
    url = OLLAMA + path
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 502, json.dumps({"error": str(e)})


# ── 原生窗口 API（文件选择等） ─────────────────────────────────────────
class Api:
    def pick_file(self):
        try:
            win = webview.windows[0]
            result = win.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("文档 (*.md;*.txt;*.pdf)|*.md;*.txt;*.pdf"),
            )
            if isinstance(result, (list, tuple)) and result:
                return result[0]
            return result
        except Exception as e:
            return {"error": str(e)}


# ── HTTP 代理 ───────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "NOVA/1.0"

    def _headers(self, code, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # 不再发送 Access-Control-Allow-Origin: * —— 那是本应用最大的漏洞：
        # 任意网站都能跨域读取你的记忆、历史，以及调用 /api/quit。
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def _json(self, obj):
        self._headers(200)
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _forbid(self, why):
        self._headers(403, "text/plain; charset=utf-8")
        self.wfile.write(("403 " + why).encode("utf-8"))

    def _not_found(self):
        self._headers(404, "application/json; charset=utf-8")
        self.wfile.write(b'{"error": "not found"}')

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        return any(host.startswith(p) for p in ALLOWED_HOST_PREFIXES)

    def _token_ok(self):
        """令牌可通过 X-NOVA-Token 头或 ?_t= 查询参数传入。"""
        got = self.headers.get("X-NOVA-Token") or ""
        if not got:
            q = urllib.parse.urlparse(self.path)
            got = urllib.parse.parse_qs(q.query).get("_t", [""])[0]
        return bool(got) and hmac.compare_digest(got, SESSION_TOKEN)

    def _guard(self, need_token=True):
        """所有 /api/* 请求的统一入口校验。返回 True 表示放行。"""
        if not self._host_ok():
            self._forbid("非法 Host（仅允许本机访问）")
            return False
        if need_token and not self._token_ok():
            self._forbid("缺少或错误的会话令牌")
            return False
        return True

    def _send_html(self, filename, inject_token=True):
        """读取 HTML 并注入令牌，供前端 fetch 带上。

        同时把静态资源引用改写为带 ?_t= 的形式 —— <script src> 无法附加自定义头，
        只能靠查询参数传令牌，否则 splash.js/css 会 403 导致白屏/黑屏。
        """
        try:
            with open(os.path.join(SERVER_DIR, filename), "r", encoding="utf-8") as f:
                html = f.read()
            if inject_token:
                tag = (
                    '<script>window.NOVA_TOKEN='
                    + json.dumps(SESSION_TOKEN)
                    + ";</script>"
                )
                # 静态资源 URL 追加令牌：<script src> / <link href> 无法附加自定义头，
                # 只能靠 ?_t= 查询参数，否则 splash.js/css 会被 403 拦掉导致黑屏。
                # 不用带反向引用的正则（交替分支下编号极易错），改为逐个属性值直接拼字符串。
                def _tok_for_url(url):
                    if not url.startswith("/assets/") or "_t=" in url:
                        return url
                    sep = "&" if "?" in url else "?"
                    return url + sep + "_t=" + SESSION_TOKEN

                html = re.sub(
                    r'((?:href|src)\s*=\s*)(["\'])(/assets/[^"\']*)\2',
                    lambda m: m.group(1) + m.group(2) + _tok_for_url(m.group(3)) + m.group(2),
                    html,
                )
                # 插到 <head> 之后最稳（两个页面都有 <head>）
                if "<head>" in html:
                    html = html.replace("<head>", "<head>" + tag, 1)
                else:
                    html = tag + html
            self._headers(200, "text/html; charset=utf-8")
            self.wfile.write(html.encode("utf-8"))
        except Exception as e:
            self._json({"error": str(e)})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/splash"):
            # 开机动画是可选增强：资源缺失（首次 clone 未跑 fetch 脚本）时直接跳聊天页，
            # 否则 splash.js 加载失败会留下一个黑屏页面。
            _js = os.path.join(SERVER_DIR, "assets", "splash.js")
            _css = os.path.join(SERVER_DIR, "assets", "splash.css")
            if os.path.isfile(_js) and os.path.isfile(_css):
                self._send_html("splash.html")
            else:
                self.send_response(302)
                self.send_header("Location", "/chat")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
        elif path in ("/chat", "/index.html"):
            self._send_html("index.html")
        elif path.startswith("/assets/"):
            # 静态资源由 <script src> / <link href> 加载，浏览器无法附加自定义头，
            # 因此令牌只能走 ?_t= 查询参数；由 _send_html 在注入时自动改写 URL。
            if not self._guard():
                return
            fpath = _safe_asset_path(path[len("/assets/"):])
            if fpath and os.path.isfile(fpath):
                ctype = {
                    ".js": "application/javascript",
                    ".css": "text/css",
                    ".html": "text/html; charset=utf-8",
                    ".htm": "text/html; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".json": "application/json",
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".ico": "image/x-icon",
                    ".mp3": "audio/mpeg",
                    ".woff2": "font/woff2",
                }.get(os.path.splitext(fpath)[1].lower(), "application/octet-stream")
                try:
                    with open(fpath, "rb") as f:
                        self._headers(200, ctype)
                        self.wfile.write(f.read())
                except Exception as e:
                    self._json({"error": str(e)})
            else:
                self._not_found()
        elif path.startswith("/dsh-startup/asset/"):
            # 开机动画的真声素材（line-1..6 旁白 + sfx 音效），本地离线提供
            if not self._guard():
                return
            name = os.path.basename(path[len("/dsh-startup/asset/"):])
            if not name.endswith(".mp3"):
                self._not_found()
            else:
                fpath = os.path.join(SERVER_DIR, "assets", "dsh_voice", name)
                if os.path.isfile(fpath):
                    try:
                        with open(fpath, "rb") as f:
                            self._headers(200, "audio/mpeg")
                            self.wfile.write(f.read())
                    except Exception as e:
                        self._json({"error": str(e)})
                else:
                    self._not_found()
        elif path == "/api/health":
            # 供单实例探测用：标识「这个端口上的服务就是本应用」
            self._json({"app": APP_MARKER})
        elif not self._guard():
            return
        elif path == "/api/models":
            code, body = ollama_request("/api/tags")
            self._headers(code)
            self.wfile.write(body.encode("utf-8"))
        elif path == "/api/ps":
            code, body = ollama_request("/api/ps")
            self._headers(code)
            self.wfile.write(body.encode("utf-8"))
        elif path == "/api/history":
            ensure_history_dir()
            if os.path.exists(HISTORY_FILE):
                try:
                    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                        self._headers(200)
                        self.wfile.write(f.read().encode("utf-8"))
                except Exception as e:
                    self._json({"error": str(e)})
            else:
                self._json([])
        elif path == "/api/mcp_status":
            self._json({"ready": BRIDGE.ready, "status": BRIDGE.status})
        elif path == "/api/memory":
            raw_len = len(APP_STATE.get("memory", ""))
            comp = APP_STATE.get("memory_compact", "")
            h = hashlib.sha256(APP_STATE.get("memory", "").encode("utf-8")).hexdigest()[:16]
            self._json({
                "loaded": bool(raw_len),
                "length": raw_len,
                "compact_length": len(comp),
                "compressed": bool(comp) and APP_STATE.get("memory_hash") == h and raw_len > MEMORY_FULL_LIMIT,
            })
        elif path == "/api/memory_raw":
            # 返回记忆文档原始内容，供前端编辑框填充
            try:
                if os.path.exists(MEMORY_FILE):
                    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                        self._headers(200, "text/plain; charset=utf-8")
                        self.wfile.write(f.read().encode("utf-8"))
                else:
                    self._headers(200, "text/plain; charset=utf-8")
                    self.wfile.write(b"")
            except Exception as e:
                self._json({"error": str(e)})
        else:
            self._not_found()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        if not self._guard():
            return

        if path == "/api/chat":
            self._chat(payload)
            return
        elif path == "/api/release":
            model = payload.get("model", "qwen3:8b")
            # 只允许「已安装的模型名」形态，避免参数被当成别的可执行文件
            if not _is_safe_model_name(model):
                self._json({"ok": False, "error": "非法的模型名"})
                return
            try:
                out = subprocess.run(["ollama", "stop", model], capture_output=True, text=True, timeout=30)
                self._json({"ok": True, "output": (out.stdout + out.stderr).strip()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/quit":
            try:
                out = subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"], capture_output=True, text=True, timeout=30)
                self._json({"ok": True, "output": (out.stdout + out.stderr).strip()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/ingest":
            doc_path = payload.get("path", "")
            title = payload.get("title", "")
            res, err = BRIDGE.ingest(doc_path, title)
            if err:
                self._json({"ok": False, "error": err})
            else:
                self._json({"ok": True, "result": res or "已处理。"})
        elif path == "/api/warmup":
            # 供开机动画并行预热模型（也可由前端在 splash 加载时调用）
            model = payload.get("model", "qwen3:8b")
            if not _is_safe_model_name(model):
                self._json({"ok": False, "error": "非法的模型名"})
                return
            threading.Thread(target=warmup_model, args=(model,), daemon=True).start()
            self._json({"ok": True, "started": True})
        elif path == "/api/memory_update":
            # 保存记忆文档：前端编辑框写入，立即更新内存中的记忆
            content = payload.get("content", "")
            if not isinstance(content, str):
                self._json({"ok": False, "error": "内容必须是文本"})
                return
            try:
                with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                    f.write(content)
                APP_STATE["memory"] = content.strip()
                # 摘要已过期 —— 置空让它下次重新生成，绝不用旧摘要配新原文
                APP_STATE["memory_hash"] = ""
                threading.Thread(target=lambda: compact_memory(force=False), daemon=True).start()
                self._json({"ok": True, "length": len(APP_STATE["memory"])})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/history":
            ensure_history_dir()
            try:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    f.write(raw)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/memory_compact":
            # 手动触发记忆压缩（任务③）
            res, err = compact_memory(force=True)
            self._json({"ok": err is None, "result": res, "error": err})
        else:
            self._not_found()

    def _chat(self, payload):
        """增强版对话：可选先用知识库/联网检索，再把结果注入系统提示，再流式返回 Ollama。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        model = payload.get("model", "qwen3:8b")
        if not _is_safe_model_name(model):
            self.wfile.write(json.dumps({"error": "非法的模型名"}).encode("utf-8"))
            return
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        use_kb = bool(payload.get("kb", False))
        use_web = bool(payload.get("web", False))
        use_memory = payload.get("memory", True)
        think = payload.get("think", True)

        # 取最后一条用户消息作为检索词
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break

        context_parts = []
        kb_used = web_used = False
        if use_kb and last_user:
            res, err = BRIDGE.search_kb(last_user)
            if res and not (err or res.startswith("Error")):
                context_parts.append("【本地知识库检索结果】\n" + res)
                kb_used = True
        if use_web and last_user:
            res, err = BRIDGE.web_search(last_user)
            if res and not (err or res.startswith("Error")):
                context_parts.append("【联网检索结果（请综合并附来源链接）】\n" + res)
                web_used = True

        # 构造发给 Ollama 的消息：注入系统提示（记忆 → 知识库/联网资料）
        mem_text, mem_compressed = (
            memory_for_prompt() if use_memory else ("", False)
        )
        if not mem_text and use_memory and APP_STATE.get("memory"):
            # 原文过长且摘要还没生成好 —— 这次先不注入，绝不硬塞超长原文
            mem_text, mem_compressed = "", False
        sys_parts = []
        if mem_text:
            sys_parts.append(
                "【用户长期记忆 · 每次对话自动载入，请据此理解用户背景与偏好，不要在回答里原样复述这段记忆】\n"
                + mem_text
            )
        if context_parts:
            sys_parts.append(
                "以下是与用户问题相关的补充资料，请优先依据资料作答并标注来源；"
                "资料不足时再结合你的知识。\n\n" + "\n\n".join(context_parts)
            )
        sys_text = "\n\n".join(sys_parts) if sys_parts else None
        ollama_messages = []
        if sys_text:
            ollama_messages.append({"role": "system", "content": sys_text})

        # 会话滑动窗口：过长的历史压成滚动摘要，只把最近几轮原文给模型
        conv_summary = ""
        try:
            kept, conv_summary = summarize_old_turns(messages)
        except Exception:
            kept = messages
        ollama_messages.extend(kept)
        if conv_summary:
            ollama_messages.insert(
                1 if sys_text else 0,
                {
                    "role": "system",
                    "content": "【此前对话要点（较早内容已压缩，仅供参考）】\n" + conv_summary,
                },
            )

        ollama_payload = {
            "model": model,
            "messages": ollama_messages,
            "stream": True,
            "think": think,
        }

        # 性能选项（上下文长度 / GPU 层数 / KV Cache 量化），由侧栏「性能设置」传入
        options = {}
        if payload.get("num_ctx"):
            try:
                options["num_ctx"] = int(payload["num_ctx"])
            except Exception:
                pass
        if payload.get("num_gpu_layers") not in (None, ""):
            try:
                options["num_gpu_layers"] = int(payload["num_gpu_layers"])
            except Exception:
                pass
        if payload.get("kv_cache_type"):
            options["kv_cache_type"] = str(payload["kv_cache_type"])
        if options:
            ollama_payload["options"] = options

        # 在首块里告知前端本次是否动用了记忆/知识库/联网
        meta = {
            "memory_used": bool(mem_text),
            "memory_compressed": mem_compressed,
            "kb_used": kb_used,
            "web_used": web_used,
            "history_summarized": bool(conv_summary),
        }
        try:
            req = urllib.request.Request(
                OLLAMA + "/api/chat",
                data=json.dumps(ollama_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                first = True
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    if first:
                        # 在首个 chunk 前塞入元信息行（前端解析 __meta__）
                        self.wfile.write(
                            (json.dumps({"__meta__": meta}) + "\n").encode("utf-8")
                        )
                        first = False
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except Exception as e:
            try:
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            except Exception:
                pass

    def log_message(self, *args):
        pass


def _boot_ollama_and_warmup(model="qwen3:8b"):
    """后台任务：确保 Ollama 在运行 → 预热模型权重进显存。"""
    try:
        ensure_ollama()
        warmup_model(model)
    except Exception as e:
        print("[启动] Ollama/预热异常：", e)


def main():
    # ⓿ 单实例锁：已在运行时只激活已有窗口，不再开第二个
    if not acquire_single_instance():
        print("[单实例] 检测到 NOVA AI 已在运行，正在把已有窗口切到前台。")
        for _ in range(10):  # 最多等 3 秒，等对方窗口建好
            if focus_existing_window():
                break
            time.sleep(0.3)
        return

    # ① 最快路径：先起本地 HTTP 服务，然后立刻弹窗（开机动画马上出现）
    ensure_history_dir()
    load_memory()  # 读取长期记忆文档（毫秒级）
    port = socketserver_serve()

    # ② 所有耗时工作全部丢后台并行，互不阻塞窗口出现
    threading.Thread(target=BRIDGE.start, daemon=True).start()
    threading.Thread(
        target=_boot_ollama_and_warmup, args=("qwen3:8b",), daemon=True
    ).start()
    # 记忆过长时后台自动生成摘要（不阻塞窗口）
    threading.Thread(target=lambda: compact_memory(force=False), daemon=True).start()

    webview.create_window(
        WINDOW_TITLE,
        url=f"http://127.0.0.1:{port}",
        width=1100,
        height=720,
        min_size=(800, 560),
        js_api=Api(),
    )
    webview.start()
    httpd_shutdown()
    print("已退出")


# 模块级持有 httpd 以便关闭
_HTTPD = None


def socketserver_serve():
    """启动本地 HTTP 服务；端口被占用时自动顺延，返回实际使用的端口。

    注意：allow_reuse_address 必须为 False —— Windows 上 SO_REUSEADDR 允许
    多个进程绑定同一端口，会导致请求被别的（旧的）实例截走。
    """
    global _HTTPD
    import socketserver

    socketserver.TCPServer.allow_reuse_address = False
    last_err = None
    for port in range(PORT, PORT + 20):
        try:
            _HTTPD = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
            threading.Thread(target=_HTTPD.serve_forever, daemon=True).start()
            print(f"[HTTP] 本地服务已启动：http://127.0.0.1:{port}")
            return port
        except OSError as e:
            last_err = e
            continue
    raise RuntimeError(f"8080-8099 端口都被占用，无法启动本地服务：{last_err}")


def httpd_shutdown():
    if _HTTPD:
        _HTTPD.shutdown()


if __name__ == "__main__":
    main()
