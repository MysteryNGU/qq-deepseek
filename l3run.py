"""L3 子进程入口：隔离日志、独立数据库，不经过 main() 的单实例锁。"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from bot import Bot, Brain, DeepSeek, ROOT, load_config

logging.basicConfig(
    level=logging.INFO,
    format="L3 %(asctime)s %(levelname)-7s %(name)s %(message)s",
    stream=sys.stdout,
    force=True,
)
for noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config.toml"
cfg = load_config(cfg_path)
db = Path(cfg.db)
if not db.is_absolute():
    db = ROOT / db
brain = Brain(db)
llm = DeepSeek(cfg)
bot = Bot(cfg, brain, llm)
try:
    asyncio.run(bot.run())
except KeyboardInterrupt:
    pass
