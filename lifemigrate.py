#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""把旧 brain.db 增量导入 life.db。

默认 dry-run；加 --apply 才写。可以重复执行：
(旧库, 表, 主键) 有唯一映射，已导入的行不会重复。

    .\.venv\Scripts\python.exe lifemigrate.py --apply
"""

from __future__ import annotations

import argparse
from pathlib import Path

from life import LifeRuntime
from life.migrate import migrate_brain

ROOT = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", default=str(ROOT / "data" / "brain.db"))
    ap.add_argument("--life", default=str(ROOT / "data" / "life.db"))
    ap.add_argument("--agent", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true", help="真正写入；默认只看数量")
    ap.add_argument("--adopt-peer", type=int, default=0,
                    help="把该 peer 的 legacy_unknown 数据归到 --conversation")
    ap.add_argument("--conversation", default="",
                    help="目标会话键，例如 qq:private:123456789")
    ap.add_argument("--confirm-legacy", action="store_true",
                    help="把该会话里 legacy_unverified 候选记忆升为 active")
    args = ap.parse_args()

    rt = LifeRuntime(args.life, agent_id=args.agent or "default")
    try:
        report = migrate_brain(
            args.brain, rt, agent_id=args.agent or None,
            dry_run=not args.apply, limit=args.limit or None,
        )
        if args.apply and args.adopt_peer and args.conversation:
            scope = rt.scope(args.conversation, str(args.adopt_peer), args.conversation)
            adopted = rt.adopt_legacy(args.adopt_peer, scope)
            print(f"归入会话 {args.conversation}：{adopted}")
            if args.confirm_legacy:
                print(f"确认旧记忆：{rt.confirm_legacy(scope)} 条升为 active")
    finally:
        rt.close()
    mode = "写入" if args.apply else "dry-run"
    print(f"[{mode}] 消息 {report.messages}，事实 {report.facts}，日记 {report.diaries}，"
          f"已导入跳过 {report.skipped}")
    for err in report.errors:
        print("错误：", err)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
