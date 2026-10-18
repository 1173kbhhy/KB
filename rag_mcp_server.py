# -*- coding: utf-8 -*-
"""本地 RAG 知识库 MCP 服务器（轻量版，不依赖 torch / sentence-transformers）

设计目标：
- 复用本机 Ollama 的 nomic-embed-text 做向量嵌入（无需下载 550MB 模型，无需 torch）
- 用 Qdrant 本地文件存储做持久化向量库（重启不丢）
- 检索采用两阶段：先用向量粗召回 RERANK_CANDIDATES 条，再重排精排，最后截断 top_k
- 暴露 3 个 MCP 工具，与原 mcp-rag-server 接口保持一致：
    knowledge_base_search(query, top_k)  -> 本地知识库语义检索
    web_search(query)                    -> 联网检索（需 FIRECRAWL_API_KEY）
    ingest_document(filepath, title)     -> 把文档加入知识库（更新数据库）
- 通过 stdio 与桌面客户端通信（FastMCP 默认传输）
"""
import logging
import os
import re
import time
import urllib.request
import urllib.error
import json
import hashlib
import uuid
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

load_dotenv()

logger = logging.getLogger("rag_mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ── 配置 ──────────────────────────────────────────────────────────────
OLLAMA = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")  # 768 维
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("RAG_DATA_DIR", str(ROOT / "kb_data"))).resolve()
QDRANT_PATH = Path(os.getenv("RAG_QDRANT_PATH", str(ROOT / "qdrant_data"))).resolve()
COLLECTION = "kb"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 80
TOP_K = 4
SCORE_THRESHOLD = 0.55
VECTOR_DIM = 768  # nomic-embed-text

# ── 重排配置（两阶段检索：向量粗召回 → 重排精排）──────────────────────
# RERANK_MODE: off(纯向量) | lexical(混合打分，零依赖) | ollama(神经重排) | hybrid(神经+词法)
RERANK_MODE = (os.getenv("RERANK_MODE", "lexical") or "lexical").strip().lower()
RERANK_MODEL = os.getenv("RERANK_MODEL", "dengcao/Qwen3-Reranker-0.6B")
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "20"))  # 粗召回数（此处不设阈值）
RERANK_KEEP_ALIVE = os.getenv("RERANK_KEEP_ALIVE", "30s")  # 打分后即卸载，避免与主力模型抢显存
RERANK_KEEP = int(os.getenv("RERANK_KEEP", str(TOP_K)))  # 重排后最多保留条数
RERANK_W_VEC = float(os.getenv("RERANK_W_VEC", "0.6"))  # 混合打分中向量分权重
RERANK_W_LEX = float(os.getenv("RERANK_W_LEX", "0.4"))  # 混合打分中词法分权重
RERANK_MERGE_N = int(os.getenv("RERANK_MERGE_N", "20"))  # 送入重排的候选上限（防止打分太慢）
RERANK_TIMEOUT = int(os.getenv("RERANK_TIMEOUT", "20"))  # 单条重排请求超时（秒）

# 词法打分用的标点/停用字符（ASCII 切词时按这些断句）
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

DATA_DIR.mkdir(parents=True, exist_ok=True)
QDRANT_PATH.mkdir(parents=True, exist_ok=True)

mcp = FastMCP(
    "local-rag",
    instructions=(
        "你拥有一个本地私有知识库（knowledge_base_search）和联网检索（web_search）。"
        "优先用 knowledge_base_search 查找用户文档中的权威内容；"
        "若知识库无结果或用户明确要求联网，再使用 web_search；否则用自己的知识作答。"
        "引用资料时注明来源文件名/标题。"
    ),
)

client = QdrantClient(path=str(QDRANT_PATH))
if not client.collection_exists(COLLECTION):
    client.create_collection(
        COLLECTION,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )


# ── Ollama 嵌入 ────────────────────────────────────────────────────────
def embed(texts):
    """调用 Ollama /api/embed 获取向量。texts: list[str] -> list[list[float]]"""
    if isinstance(texts, str):
        texts = [texts]
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA + "/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("embeddings", [])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(
                f"Ollama 缺少嵌入模型 {EMBED_MODEL}，请先执行：ollama pull {EMBED_MODEL}"
            )
        raise RuntimeError(f"Ollama 嵌入失败：{e.code} {e.read().decode('utf-8','replace')}")
    except Exception as e:
        raise RuntimeError(f"Ollama 嵌入调用异常：{e}")


# ── 重排：词法打分 + 神经重排 ─────────────────────────────────────────
def _lex_terms(text):
    """拆出词法匹配用的 term：ASCII 词/数字（已小写）、中文字符二元组、单字兜底。"""
    low = (text or "").lower()
    ascii_terms = set(_ASCII_TOKEN_RE.findall(low))
    chars = _CJK_RE.findall(low)
    bigrams = {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}
    return ascii_terms, bigrams, set(chars)


def lexical_score(query, text):
    """查询与片段的词法重叠度（0~1），零依赖。

    ASCII 词/数字按精确 token 命中计权重 1.0；中文优先用字符二元组（权重 0.5，
    比单字更能体现"子网划分"这种词的存在），query 只有一个汉字时退回单字。
    """
    q_ascii, q_big, q_uni = _lex_terms(query)
    if not (q_ascii or q_big or q_uni):
        return 0.0
    t_ascii, t_big, t_uni = _lex_terms(text)
    total = hit = 0.0
    for t in q_ascii:
        total += 1.0
        if t in t_ascii:
            hit += 1.0
    cjk_terms, t_pool, w = (q_big, t_big, 0.5) if q_big else (q_uni, t_uni, 1.0)
    for t in cjk_terms:
        total += w
        if t in t_pool:
            hit += w
    score = hit / total if total else 0.0
    # 整串包含再补一点分，照顾精确关键词
    q_strip = (query or "").strip().lower()
    if q_strip and q_strip in (text or "").lower():
        score = min(1.0, score + 0.1)
    return round(score, 4)


_RERANK_PROMPT = (
    "判断下面的资料片段能否回答检索问题。"
    "只输出 yes 或 no，不要输出任何其他内容。\n\n"
    "检索问题：{query}\n\n资料片段：\n{passage}\n\n"
    "该片段是否与检索问题相关？只回答 yes 或 no："
)


def _parse_rerank_score(raw):
    """从重排模型输出里解析 0~1 相关度；兼容 yes/no 与 0-10 打分，解析不了返回 None。"""
    low = (raw or "").strip().lower()
    if not low:
        return None
    # 先看 "8/10" 这类分数形式，其次看裸数字：<=1 视为已是 0~1，否则按 0~10 折算
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", low)
    if m:
        try:
            num, den = float(m.group(1)), float(m.group(2))
        except ValueError:
            return None
        if den > 0:
            return max(0.0, min(1.0, num / den))
    m = re.search(r"\b(10|[0-9](?:\.\d+)?)\b", low)
    if m:
        try:
            v = float(m.group(1))
        except ValueError:
            return None
        return max(0.0, min(1.0, v if v <= 1.0 else v / 10.0))
    # 注意顺序："不相关"里也含"相关"，先排否定
    if re.search(r"\bno\b|不相关|无关|不符合", low):
        return 0.0
    if re.search(r"\byes\b|相关|符合", low):
        return 1.0
    return None


def _ollama_rerank_score(query, passage, timeout):
    """调用本机 Ollama 重排模型，返回 0~1 相关度；网络/模型异常向上抛，由调用方降级。"""
    passage = (passage or "")[:1200]  # 过长片段对判定无益，还拖慢单条打分
    payload = json.dumps({
        "model": RERANK_MODEL,
        "messages": [{"role": "user", "content": _RERANK_PROMPT.format(query=query, passage=passage)}],
        "stream": False,
        "think": False,
        "keep_alive": RERANK_KEEP_ALIVE,
        "options": {"temperature": 0, "num_predict": 8},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"缺少重排模型 {RERANK_MODEL}，请先执行：ollama pull {RERANK_MODEL}")
        raise RuntimeError(f"Ollama 重排失败：{e.code}")
    score = _parse_rerank_score((data.get("message") or {}).get("content", ""))
    if score is None:
        logger.warning("重排输出无法解析：%r", ((data.get("message") or {}).get("content") or "")[:80])
    return score


def _apply_lexical(items):
    """按 RERANK_W_VEC / RERANK_W_LEX 做混合打分（lexical 模式与各降级路径共用）。"""
    for it in items:
        it["final"] = RERANK_W_VEC * it["vector"] + RERANK_W_LEX * it.get("lex", 0.0)
        it["label"] = "重排分"
    return items


def _rerank_items(query, items):
    """对粗召回候选重排。就地写入 final/label 并按 final 降序，返回 (items, 生效模式说明)。"""
    mode = RERANK_MODE
    if mode not in ("off", "lexical", "ollama", "hybrid"):
        logger.warning("未知 RERANK_MODE=%r，回退为 lexical", mode)
        mode = "lexical"

    # off：保持旧的纯向量行为，作为兜底基线
    if mode == "off":
        for it in items:
            it["final"], it["label"] = it["vector"], "向量分"
        items.sort(key=lambda x: x["final"], reverse=True)
        return items, "off（未启用重排）"

    for it in items:
        it["lex"] = lexical_score(query, it["text"])
    if mode == "lexical":
        _apply_lexical(items)
        items.sort(key=lambda x: x["final"], reverse=True)
        return items, "lexical（混合打分）"

    # ollama / hybrid：逐条神经打分，任何失败都退到词法分，绝不让检索崩掉
    neural = [None] * len(items)
    n = min(len(items), RERANK_MERGE_N)
    for i in range(n):
        try:
            neural[i] = _ollama_rerank_score(query, items[i]["text"], RERANK_TIMEOUT)
        except Exception as e:
            logger.warning("第 %d 条重排失败，改用词法分：%s", i + 1, e)
    ok = sum(1 for v in neural if v is not None)
    if ok == 0:
        logger.warning("神经重排全部失败，整体降级为 lexical")
        _apply_lexical(items)
        items.sort(key=lambda x: x["final"], reverse=True)
        return items, f"{mode}→lexical（降级）"

    for i, it in enumerate(items):
        ns = neural[i]
        if ns is None:
            # 超 RERANK_MERGE_N 或单条失败：用混合分兜底，避免纯词法分把强向量候选压下去
            it["final"] = RERANK_W_VEC * it["vector"] + RERANK_W_LEX * it["lex"]
            it["label"] = "重排分（词法兜底）"
        elif mode == "hybrid":
            it["final"] = 0.85 * ns + 0.15 * it["lex"]
            it["label"] = "重排分"
        else:
            it["final"], it["label"] = ns, "重排分"
    items.sort(key=lambda x: x["final"], reverse=True)
    return items, f"{mode}（神经分 {ok}/{len(items)} 条）"


# ── 文档分块 ────────────────────────────────────────────────────────────
def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if size <= overlap:
        overlap = 0
    chunks, start = [], 0
    while start < len(text):
        end = start + size
        seg = text[start:end].strip()
        if seg:
            chunks.append(seg)
        start = end - overlap
        if start <= 0:
            start = end
    return chunks


def _read_file(path: Path):
    ext = path.suffix.lower()
    if ext in (".md", ".txt"):
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        try:
            import pymupdf4llm
        except ImportError:
            raise ValueError("读取 PDF 需要安装 pymupdf4llm（pip install pymupdf4llm）")
        pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
        parts = []
        for i, p in enumerate(pages, 1):
            t = p.get("text", "").strip()
            if t:
                parts.append(f"[Page {i}]\n{t}")
        return "\n\n".join(parts)
    raise ValueError(f"不支持的文件类型：{ext}（仅支持 .md/.txt/.pdf）")


# ── MCP 工具 ────────────────────────────────────────────────────────────
@mcp.tool()
def knowledge_base_search(query: str, top_k: int | None = None) -> str:
    """在本地私有知识库中检索与问题相关的文档片段。

    对**任何**可能由用户文档回答的问题都应先调用本工具。
    返回带来源标题/文件名与相关性分数的片段；若无结果返回提示语。
    """
    query = (query or "").strip()
    if not query:
        return "Error: 查询内容不能为空。"
    t0 = time.time()
    try:
        qv = embed([query])
        if not qv:
            return "Error: 嵌入失败，无法检索。"
        # 粗召回：这一步刻意不传 score_threshold，阈值留到重排后按最终分过滤，
        # 否则低向量分但关键词高度命中的片段会被提前丢掉。
        hits = client.query_points(COLLECTION, query=qv[0], limit=RERANK_CANDIDATES).points
    except Exception as e:
        return f"Error: 知识库检索失败 —— {e}"

    if not hits:
        return "知识库中未找到相关内容。"

    items = []
    for h in hits:
        p = h.payload or {}
        items.append({
            "src": p.get("title") or p.get("source") or "未知来源",
            "text": p.get("text", ""),
            "vector": getattr(h, "score", 0) or 0,
        })

    try:
        items, mode_desc = _rerank_items(query, items)
    except Exception as e:  # 重排整体异常也只降级，不影响检索可用性
        logger.exception("重排整体失败，降级为纯向量")
        items.sort(key=lambda x: x["vector"], reverse=True)
        mode_desc = f"降级为纯向量（{e}）"

    # 先按最终分过阈值，再截断；全被滤掉时回退到最接近的几条
    kept = [it for it in items if it["final"] >= SCORE_THRESHOLD]
    low_conf = False
    if not kept:
        kept = items[:RERANK_KEEP]
        low_conf = True
    kept = kept[:max(1, top_k or RERANK_KEEP)]

    lines = []
    for it in kept:
        src = it["src"]
        if it["label"] == "向量分":
            head = f"[来源: {src} | 向量分: {it['final']:.2f}]"
        else:
            head = f"[来源: {src} | {it['label']}: {it['final']:.2f} | 向量分: {it['vector']:.2f}]"
        lines.append(f"{head}\n{it['text']}")

    body = "\n\n---\n\n".join(lines)
    if low_conf:
        body += "\n\n（相关度偏低，以上为重排后最接近问题的片段，建议结合上下文判断。）"
    body += f"\n\n[重排: {mode_desc} | 粗召回 {len(items)} 条 | 耗时 {int((time.time() - t0) * 1000)} ms]"
    return body


@mcp.tool()
def web_search(query: str) -> str:
    """联网检索互联网信息（仅在知识库无结果或用户明确要求联网时使用）。

    需要环境变量 FIRECRAWL_API_KEY。返回标题、链接与摘要。
    """
    query = (query or "").strip()
    if not query:
        return "Error: 查询内容不能为空。"
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        return "Error: 未配置 FIRECRAWL_API_KEY，无法联网检索。请在 .env 中配置后重启。"
    try:
        from firecrawl import Firecrawl
        fc = Firecrawl(api_key=api_key)
        res = fc.search(query, limit=5)
        items = getattr(res, "web", None) or []
        if not items:
            return "未检索到结果。"
        out = []
        for it in items:
            title = getattr(it, "title", "") or "无标题"
            url = getattr(it, "url", "") or ""
            desc = getattr(it, "description", "") or ""
            out.append(f"**{title}**\n{url}\n{desc}")
        return "\n\n---\n\n".join(out)
    except Exception as e:
        logger.exception("web_search failed")
        return f"Error: 联网检索失败 —— {e}"


@mcp.tool()
def ingest_document(filepath: str, title: str = "") -> str:
    """把文档（.md/.txt/.pdf）加入本地知识库，立即可被检索（更新数据库）。

    重复加入同一文档不会产生重复分块（按内容去重）。
    返回本次索引的分块数量。
    """
    path = Path(filepath)
    if not path.is_file():
        return f"Error: 文件不存在：{filepath}"
    try:
        text = _read_file(path)
    except Exception as e:
        return f"Error: 读取失败 —— {e}"
    chunks = chunk_text(text)
    if not chunks:
        return "Error: 文档内容为空。"
    title = title or path.stem
    try:
        vectors = embed(chunks)
    except Exception as e:
        return f"Error: 嵌入失败 —— {e}"
    points = []
    for ch, vec in zip(chunks, vectors):
        pid = uuid.uuid5(uuid.NAMESPACE_URL, f"{title}:{ch}")
        points.append(PointStruct(id=int(pid) & 0x7FFFFFFF, vector=vec,
                                  payload={"text": ch, "source": path.name, "title": title}))
    client.upsert(COLLECTION, points=points)
    return f"已入库「{title}」：{len(points)} 个分块，立即可检索。"


if __name__ == "__main__":
    logger.info("启动本地 RAG MCP 服务器（嵌入模型=%s，重排模式=%s，知识库=%s）",
                EMBED_MODEL, RERANK_MODE, DATA_DIR)
    # 启动时把知识库目录里已有的文档索引进来（仅新增，按内容去重）
    try:
        existing = [p for p in DATA_DIR.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".txt", ".pdf")]
        for p in existing:
            try:
                logger.info("预索引：%s", p.name)
                ingest_document(str(p), title=p.stem)
            except Exception as e:
                logger.warning("预索引失败 %s: %s", p.name, e)
    except Exception as e:
        logger.warning("启动预索引异常：%s", e)
    mcp.run()
