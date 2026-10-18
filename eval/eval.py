# -*- coding: utf-8 -*-
"""本地离线评测脚本（需求 4）——一条命令跑完，输出 Markdown + CSV 报告。

用法：
    venv/Scripts/python.exe eval/eval.py                    # 跑全量评测
    venv/Scripts/python.exe eval/eval.py --tag before        # 给本次结果打标签
    venv/Scripts/python.exe eval/eval.py --only-retrieval    # 只测检索，跳过生成（快）
    venv/Scripts/python.exe eval/eval.py --no-kb             # 基线：不给知识库，测裸模型

为什么这样设计：
- 直接 import rag_mcp_server 里的重排逻辑，保证「评测的就是线上跑的那份代码」，
  而不是复制一份评测专用实现（那样改了线上代码评测就失真了）。
- 为避免与正在运行的软件抢 qdrant 文件锁，默认**复制**一份 qdrant_data 到临时目录再评测。
- 全程 temperature=0 + 固定 seed，实测同 seed 输出完全一致，可复现。

三项指标（需求 4.3）：
    检索命中率 hit_rate     —— 该命中的题，知识库里能否召回含答案要点的片段
    忠实度   faithfulness   —— 回答的要点是否真的来自检索结果（无检索时是否正确地说"不知道"）
    格式合规率 format_rate  —— 输出是否非空、没跑偏、没复读问题
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "eval"
EVAL_SET = EVAL_DIR / "eval_set.jsonl"
REPORT_MD = EVAL_DIR / "report.md"
REPORT_CSV = EVAL_DIR / "report.csv"

sys.path.insert(0, str(ROOT))

# 评测用的采样参数（需求 6.2：评测必须 temperature=0 + 固定 seed）
EVAL_OPTIONS = {
    "temperature": 0,
    "seed": 42,
    "top_p": 1.0,
    "num_predict": 512,
}
EVAL_MODEL = os.getenv("EVAL_MODEL", "qwen3:8b")
OLLAMA = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

ABSTAIN_HINTS = ["未找到", "没有找到", "知识库", "无法回答", "不确定", "抱歉", "没有相关",
                 "不包含", "未提及", "资料不足", "查不到"]


def load_cases():
    cases = []
    with open(EVAL_SET, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                cases.append(json.loads(line))
    return cases


def ollama_chat(prompt, system=None, options=None, model=None):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {
        "model": model or EVAL_MODEL,
        "messages": msgs,
        "stream": False,
        "think": False,
        "options": dict(EVAL_OPTIONS, **(options or {})),
    }
    req = urllib.request.Request(
        OLLAMA + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("message") or {}).get("content", ""), data


def norm(s):
    return re.sub(r"[\s,，。；;：:、()（）\[\]【】`'\"]+", "", (s or "").lower())


def contains_all(text, keys):
    """所有要点词是否都出现在文本里（归一化后子串匹配，容忍空格与标点差异）。"""
    t = norm(text)
    return [k for k in keys if norm(k) not in t]


def build_retriever(tmp_qdrant):
    """复制 qdrant 到临时目录后，import 线上那份 RAG 模块（避免文件锁冲突）。"""
    env_backup = dict(os.environ)
    os.environ["RAG_QDRANT_PATH"] = str(tmp_qdrant)
    # 让 rag_mcp_server 用我们的路径
    import rag_mcp_server as R
    return R, env_backup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run", help="本次结果标签，用于对比")
    ap.add_argument("--only-retrieval", action="store_true", help="只测检索，不跑生成（快）")
    ap.add_argument("--no-kb", action="store_true", help="不给知识库，测裸模型基线")
    ap.add_argument("--rerank", default=None, help="覆盖 RERANK_MODE，如 lexical/ollama/off")
    args = ap.parse_args()

    cases = load_cases()
    print(f"评测集：{len(cases)} 题 | 模型：{EVAL_MODEL} | 标签：{args.tag}")
    print(f"采样：temperature=0 seed=42（可复现）")

    tmp_dir = None
    R = None
    if not args.no_kb:
        # 复制 qdrant，避免和运行中的软件抢锁
        tmp_dir = tempfile.mkdtemp(prefix="nova_eval_")
        tmp_qdrant = Path(tmp_dir) / "qdrant_data"
        src = ROOT / "qdrant_data"
        if src.exists():
            shutil.copytree(src, tmp_qdrant)
            for junk in (".lock",):
                p = tmp_qdrant / junk
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            print(f"已复制向量库到临时目录（避免文件锁）：{tmp_qdrant}")
        else:
            print("⚠️ 未找到 qdrant_data，检索环节将无结果")
        if args.rerank:
            os.environ["RERANK_MODE"] = args.rerank
        R, _ = build_retriever(tmp_qdrant)
        print(f"重排模式：{getattr(R, 'RERANK_MODE', '?')} | 粗召回：{getattr(R, 'RERANK_CANDIDATES', '?')}")

    rows = []
    t_start = time.time()
    for i, c in enumerate(cases, 1):
        query = c["query"]
        expect_abstain = c.get("expect_abstain", False)
        keys = c.get("must_include", [])

        # ── 检索环节 ──
        ctx, retrieved, best = "", False, ""
        if R is not None:
            try:
                ctx = R.knowledge_base_search(query, top_k=5)
                if not ctx.startswith("Error") and "未找到相关内容" not in ctx:
                    # 该题是否"命中"：检索结果里含全部要点
                    retrieved = not contains_all(ctx, keys) if keys else False
                    # 负样本不应命中
                    if expect_abstain:
                        retrieved = False
                else:
                    ctx = ""
            except Exception as e:
                print(f"  ! 检索异常 {c['id']}: {e}")
                ctx = ""

        hit_rate = None
        if R is not None and not expect_abstain:
            hit_rate = retrieved

        # ── 生成环节 ──
        if args.only_retrieval:
            answer, fmt_ok = "", True
        else:
            if R is None or not ctx:
                sysmsg = (
                    "你是一个网络技术方向的技术助手。用户会问问题。"
                    "如果提供的【资料】为空或与问题无关，你必须明确回答"
                    "「知识库中没有找到相关资料」，然后可以用你自己的通用知识补充，"
                    "但绝不能编造知识库里没有的细节。"
                )
                user = f"【资料】\n（无）\n\n问题：{query}"
            else:
                sysmsg = (
                    "你是一个网络技术方向的技术助手。**只依据【资料】回答**，"
                    "资料里没有的内容不要编造。回答要简洁准确。"
                )
                user = f"【资料】\n{ctx}\n\n问题：{query}"
            try:
                answer, _ = ollama_chat(user, system=sysmsg)
            except Exception as e:
                print(f"  ! 生成异常 {c['id']}: {e}")
                answer = ""
            # 格式合规：非空、不复读问题、长度合理
            fmt_ok = bool(answer.strip()) and len(answer) < 6000 and norm(query) not in norm(answer[:60])

        # ── 忠实度 ──
        # 注意：--only-retrieval 模式下没有生成答案，忠实度不可评，应留空而不是记 0 分
        if args.only_retrieval:
            faith = ""
            abstain_flag = ""
            faith_note = ""
        elif expect_abstain:
            said_unknown = any(h in answer for h in ABSTAIN_HINTS) if answer else True
            faith = 1.0 if said_unknown else 0.0
            faith_note = "OK" if said_unknown else "编造"
            abstain_flag = "Y" if expect_abstain else ""
        else:
            if not answer:
                faith = 0.0
                faith_note = "空回答"
            else:
                missing = contains_all(answer, keys) if keys else []
                faith = 1.0 if not missing else round(1 - len(missing) / len(keys), 3) if keys else 0.0
                faith_note = ""
            abstain_flag = ""

        rows.append({
            "id": c["id"], "cat": c.get("cat", ""), "query": query,
            "retrieved": "Y" if retrieved else ("N/A" if R is None else "N"),
            "hit_rate": "" if hit_rate is None else ("1" if hit_rate else "0"),
            "faithfulness": faith, "format_ok": "1" if fmt_ok else "0",
            "abstain": abstain_flag, "faith_note": faith_note,
            "missing": ",".join(contains_all(answer, keys)) if keys and answer else "",
            "answer_head": (answer or "").replace("\n", " ")[:110],
        })
        fmark = "" if faith == "" else f"faith={faith:.2f}"
        mark = "✓" if (faith == "" or (faith >= 0.999 and fmt_ok)) else "·"
        print(f"  [{i:2d}/{len(cases)}] {mark} {c['id']:6} {c['cat']:12} "
              f"hit={'Y' if retrieved else 'N'} {fmark} {query[:26]}")

    dt = time.time() - t_start

    # ── 汇总 ──
    def avg(key, filt=None):
        vals = []
        for r in rows:
            v = r[key]
            if v == "" or v is None:
                continue
            if filt is not None and not filt(r):
                continue
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        return round(sum(vals) / len(vals), 3) if vals else None

    hit = avg("hit_rate")
    faith_all = avg("faithfulness")
    fmt = avg("format_ok")
    faith_pos = avg("faithfulness", lambda r: not r["abstain"])
    faith_neg = avg("faithfulness", lambda r: bool(r["abstain"]))

    summary = {
        "tag": args.tag, "model": EVAL_MODEL,
        "rerank": getattr(R, "RERANK_MODE", "n/a") if R else "n/a(no-kb)",
        "mode": "retrieval-only" if args.only_retrieval else ("no-kb" if args.no_kb else "full"),
        "total": len(rows), "seconds": round(dt, 1),
        "hit_rate": hit, "faithfulness": faith_all,
        "faithfulness_positive": faith_pos, "faithfulness_negative": faith_neg,
        "format_rate": fmt,
    }
    print("\n" + "=" * 56)
    print(f"[{args.tag}] 题数={summary['total']} 用时={summary['seconds']}s 模式={summary['mode']}")
    print(f"  检索命中率 hit_rate     : {hit}")
    if faith_all is None:
        print(f"  忠实度   faithfulness   : 未评估（--only-retrieval 模式不生成回答）")
    else:
        print(f"  忠实度   faithfulness   : {faith_all}  (有资料题={faith_pos} / 负样本={faith_neg})")
    print(f"  格式合规 format_rate    : {fmt}")
    print("=" * 56)

    # ── 报告 ──
    with open(REPORT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    with open(EVAL_DIR / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    history = []
    hp = EVAL_DIR / "history.jsonl"
    if hp.exists():
        history = [json.loads(l) for l in hp.read_text(encoding="utf-8").splitlines() if l.strip()]

    lines = ["# 本地知识库评测报告", "", f"最近一次：**{args.tag}**", "",
             "## 本次结果", "", "| 指标 | 值 |", "|---|---|",
             f"| 题数 | {summary['total']} |",
             f"| 用时 | {summary['seconds']}s |",
             f"| 重排模式 | {summary['rerank']} |",
             f"| 检索命中率 hit_rate | {hit} |",
             f"| 忠实度 faithfulness | {faith_all} |",
             f"| 忠实度（有资料题） | {faith_pos} |",
             f"| 忠实度（负样本/该说不知道） | {faith_neg} |",
             f"| 格式合规率 format_rate | {fmt} |", ""]
    if len(history) > 1:
        lines += ["## 历史对比（改动前后）", "",
                  "| 标签 | 重排模式 | 检索命中率 | 忠实度 | 负样本忠实度 | 格式合规率 | 题数 |",
                  "|---|---|---|---|---|---|---|"]
        for h in history:
            lines.append(f"| {h['tag']} | {h['rerank']} | {h['hit_rate']} | {h['faithfulness']} "
                         f"| {h['faithfulness_negative']} | {h['format_rate']} | {h['total']} |")
        lines.append("")
    lines += ["## 逐题明细", "", "| id | 类别 | 检索 | 忠实度 | 格式 | 缺要点 | 问题 |",
              "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['cat']} | {r['retrieved']} | {r['faithfulness']} "
                     f"| {'✓' if r['format_ok']=='1' else '✗'} | {r['missing'] or '-'} | {r['query'][:30]} |")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入：\n  {REPORT_MD}\n  {REPORT_CSV}")

    if tmp_dir:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
