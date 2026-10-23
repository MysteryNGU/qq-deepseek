"""Single production entry: OneBot + LifeRuntime + a complete DSH Agent."""
import argparse
import asyncio
import msvcrt
from pathlib import Path
import sys

from .role_config import Config,load_config
from .role_host import RoleHost,setup_logging

ROOT = Path(__file__).resolve().parents[1]


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='DSH 主角色 QQ 机器人')
    parser.add_argument('--config',default='config.toml')
    args = parser.parse_args()
    path = Path(args.config)
    cfg = load_config(path if path.is_absolute() else ROOT/path)
    (ROOT/'data').mkdir(exist_ok=True)
    lock = (ROOT/'data/bot.lock').open('a+b')
    try:
        lock.seek(0)
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        lock.close()
        print('已有 Bot 运行，本次不重复启动。')
        return 1
    setup_logging(ROOT)
    try:
        return asyncio.run(RoleHost(cfg,ROOT).run())
    except KeyboardInterrupt:
        return 0
    finally:
        lock.close()
