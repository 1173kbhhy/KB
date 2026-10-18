# -*- coding: utf-8 -*-
"""获取开机动画资源（dsh-startup-screen）。

这些资源不随本仓库分发（见 THIRD_PARTY.md），本脚本负责从 GitHub 拉取到 assets/。

用法：
    python scripts/fetch_splash_assets.py
    python scripts/fetch_splash_assets.py --mirror      # 走镜像（raw 直连被墙时用）

说明：
- 脚本只下载，不修改内容，也不重新分发受版权保护的素材；
- 重复运行会跳过已存在的文件，加 --force 可强制重新下载；
- 动画资源是**可选的**，缺失时程序会跳过启动动画直接进入聊天界面。
"""
import argparse
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

REPO = "6shenhonghong9/dsh-startup-screen"
BRANCH = "main"
RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
MIRROR = "https://ghproxy.net/https://raw.githubusercontent.com/{repo}/{branch}/{path}"

# (本地相对路径, 仓库内路径, 是否必需)
ASSETS = [
    ("assets/splash.js", "lib/splash.js", True),
    ("assets/splash.css", "lib/splash.css", True),
    ("assets/dsh_voice/line-1.mp3", "lib/assets/line-1.mp3", False),
    ("assets/dsh_voice/line-2.mp3", "lib/assets/line-2.mp3", False),
    ("assets/dsh_voice/line-3.mp3", "lib/assets/line-3.mp3", False),
    ("assets/dsh_voice/line-4.mp3", "lib/assets/line-4.mp3", False),
    ("assets/dsh_voice/line-5.mp3", "lib/assets/line-5.mp3", False),
    ("assets/dsh_voice/line-6.mp3", "lib/assets/line-6.mp3", False),
    ("assets/dsh_voice/sfx-tick.mp3", "lib/assets/sfx-tick.mp3", False),
    ("assets/dsh_voice/sfx-ticks-run.mp3", "lib/assets/sfx-ticks-run.mp3", False),
]

ROOT = Path(__file__).resolve().parent.parent


def download(url, dest, timeout=60):
    req = urllib.request.Request(
        url, headers={"User-Agent": "nova-ai-setup", "Accept": "*/*"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if not data or len(data) < 100:
        raise RuntimeError(f"响应过小（{len(data)} 字节），可能是错误页")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)


def main():
    ap = argparse.ArgumentParser(description="下载 NOVA AI 开机动画资源")
    ap.add_argument("--mirror", action="store_true", help="使用镜像加速（raw 直连不可用时）")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = ap.parse_args()

    tpl = MIRROR if args.mirror else RAW
    print(f"仓库：{REPO}@{BRANCH}")
    print(f"通道：{'镜像' if args.mirror else '直连'}")
    print()

    ok, skip, fail = 0, 0, 0
    for rel, repo_path, required in ASSETS:
        dest = ROOT / rel
        if dest.exists() and not args.force:
            print(f"  跳过（已存在）{rel}")
            skip += 1
            continue
        url = tpl.format(repo=REPO, branch=BRANCH, path=repo_path)
        try:
            n = download(url, dest)
            print(f"  下载成功 {rel}  ({n:,} 字节)")
            ok += 1
        except Exception as e:
            mark = "必需" if required else "可选"
            print(f"  失败 {rel}  [{mark}] {e}")
            fail += 1

    print()
    print(f"完成：成功 {ok} · 跳过 {skip} · 失败 {fail}")

    js_ok = (ROOT / "assets" / "splash.js").exists()
    css_ok = (ROOT / "assets" / "splash.css").exists()
    if js_ok and css_ok:
        print("开机动画已就绪。若素材下载失败，动画会跳过语音，但画面仍正常。")
    else:
        print("⚠️ 动画核心文件缺失，程序将跳过启动动画，直接进入聊天界面（不影响使用）。")
        print("   网络受限时可尝试：python scripts/fetch_splash_assets.py --mirror")
    return 0 if (ok or skip) else 1


if __name__ == "__main__":
    sys.exit(main())
