#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不开 QQ，直接看她会怎么回。改 persona.md 的时候用这个反复试。

    python tune.py                             # 用 config.toml 里的人设
    python tune.py "在吗" "我今天好累"          # 连着试几句
    python tune.py --persona personas/冷.md "今天被骂了"   # 临时换一份人设

用的是 data/tune.db，跟真记忆（data/brain.db）分开，随便清。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import bot as B

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


async def main() -> None:
    ap = argparse.ArgumentParser(description="试她怎么说话")
    ap.add_argument("lines", nargs="*", help="他说的话，可以给多句")
    ap.add_argument("--persona", help="临时换一份人格文件（相对本目录）")
    ap.add_argument("--as", dest="rel", default="owner",
                    choices=["owner", "mistress", "friend", "stranger"],
                    help="把说话的人当成谁：主人 / 女主人 / 熟人 / 陌生人")
    args = ap.parse_args()

    cfg = B.load_config(Path("config.toml"))
    if args.persona:
        if not (Path.cwd() / args.persona).exists():
            raise SystemExit(f"没有这个人格文件：{args.persona}")
        cfg.persona = args.persona
    print(f"人设：{cfg.persona}　温度：{cfg.temperature}")

    brain = B.Brain(Path("data/tune.db"))
    brain.clear(0)
    llm = B.DeepSeek(cfg)
    bot = B.Bot(cfg, brain, llm)

    lines = args.lines or ["在吗，今天加班到十点，累死了"]
    for line in lines:
        print(f"\n{'=' * 54}\n他：{line}")
        brain.add_msg(0, "user", line)
        try:
            raw = await llm.chat(bot._messages(0, args.rel))
        except Exception as e:
            print(f"\n[调用失败] {type(e).__name__}: {e}")
            print("依次检查：api_key 有没有错、账上有没有余额、网络能不能出去")
            brain.close()
            raise SystemExit(1)
        brain.add_msg(0, "assistant", raw)
        segs = B.plan_segments(raw, cfg.split_len, cfg.strip_period, cfg.max_segments)
        print(f"\n模型原文（{len(raw)} 字）：\n{raw}")
        print(f"\n她实际会发 {len(segs)} 条 QQ 消息：")
        for s in segs:
            print(f"  [{len(s):>2}字] {s}")

    brain.close()
    print(f"\n{'=' * 54}\n这个库是 data/tune.db，跟真记忆分开，随便试")


if __name__ == "__main__":
    asyncio.run(main())
