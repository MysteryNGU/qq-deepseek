#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single-instance entry to the native bot release supervisor."""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOGTAIL = ROOT / "data" / "supervise.log"
LOCK = ROOT / "data" / "supervise.pid"


_LOCK_FH = None


def validate_data_compatibility(source, config=None, *, database=None):
    """Refuse code unable to read the live relationship format before it can write.

    This protected bootstrap also checks release candidates and rollback images;
    the database stays forward-only even when application code is replaced.
    """
    import ast
    import sqlite3
    import tomllib
    source = Path(source).resolve()
    if not (source/'bot.py').is_file():
        return
    owned = database is None or isinstance(database,(str,Path))
    if owned:
        if database is None:
            settings = tomllib.loads(Path(config).read_text('utf-8')) if config else {}
            database = Path(settings.get('life_db','data/life.db'))
            if not database.is_absolute():
                database = source/database
        path = Path(database).resolve()
        if not path.is_file():
            return
        database = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        if not database.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            return
        memory_versions=[int(row[0]) for row in database.execute("SELECT v FROM meta WHERE k LIKE 'memory_schema:%'")]
        memory_required=max(memory_versions,default=0)
        if memory_required:
            module=source/'life/runtime.py'
            supported_memory=0
            if module.is_file():
                for node in ast.parse(module.read_text('utf-8-sig')).body:
                    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SUPPORTED_MEMORY_SCHEMA' for t in node.targets):
                        if isinstance(node.value,ast.Constant) and type(node.value.value) is int:
                            supported_memory=node.value.value
            if supported_memory<memory_required:
                raise RuntimeError('memory_schema_incompatible: use the new engine; retain the live database')
        experience_required=max((int(row[0]) for row in database.execute("SELECT v FROM meta WHERE k LIKE 'experience_schema:%'")),default=0)
        if experience_required:
            supported_experience=0
            module=source/'life/runtime.py'
            if module.is_file():
                for node in ast.parse(module.read_text('utf-8-sig')).body:
                    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SUPPORTED_EXPERIENCE_SCHEMA' for t in node.targets):
                        if isinstance(node.value,ast.Constant) and type(node.value.value) is int:
                            supported_experience=node.value.value
            if supported_experience<experience_required:
                raise RuntimeError('experience_schema_incompatible: use compatible code; retain the live database')
        versions = [int(row[0]) for row in database.execute("SELECT v FROM meta WHERE k LIKE 'relationship_schema:%'")]
        required = max(versions,default=0)
        if not required:
            return
        module = source/'life/runtime.py'
        supported = 0
        if module.is_file():
            for node in ast.parse(module.read_text('utf-8-sig')).body:
                if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SUPPORTED_RELATIONSHIP_SCHEMA' for t in node.targets):
                    if isinstance(node.value,ast.Constant) and type(node.value.value) is int:
                        supported = node.value.value
        if supported<required:
            raise RuntimeError(f'relationship_schema_incompatible: database={required}, code={supported}; use compatible code, retain the live database')
    finally:
        if owned:
            database.close()


def say(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [pid={os.getpid()}] {msg}"
    print(line, flush=True)
    try:
        LOGTAIL.parent.mkdir(exist_ok=True)
        with LOGTAIL.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def already_running() -> bool:
    """已经有一套在看孩子就返回 True。

    两套同时在跑 = 两个 bot 抢同一个聊天窗口 = 每句话被回两遍。
    用 Windows 文件锁判断：进程一死锁就自动释放，不存在"锁里记的 PID 对不上"。
    """
    global _LOCK_FH
    import msvcrt
    try:
        _LOCK_FH = open(LOCK, "a+b")
        _LOCK_FH.seek(0)
        msvcrt.locking(_LOCK_FH.fileno(), msvcrt.LK_NBLCK, 1)
        return False
    except OSError:
        return True


def drop_lock() -> None:
    global _LOCK_FH
    import msvcrt
    try:
        if _LOCK_FH:
            _LOCK_FH.seek(0)
            msvcrt.locking(_LOCK_FH.fileno(), msvcrt.LK_UNLCK, 1)
            _LOCK_FH.close()
            _LOCK_FH = None
    except Exception:
        pass


def main():
    if not PY.exists():
        say(f"找不到 python：{PY}")
        return 1
    LOCK.parent.mkdir(parents=True,exist_ok=True)
    if already_running():
        say("已经有一套在跑了，这次不重复启动。要重启请先「停止.bat」再「一键启动.bat」。")
        return 0
    say(f"看守启动，PID {os.getpid()}")
    try:
        (ROOT / "data" / "run_mode.txt").write_text("supervise", "utf-8")
    except Exception:
        pass
    # The small singleton bootstrap stays alive while the controller itself can upgrade.
    from life.supervisor import ProcessTree
    failures = 0
    while True:
        validate_data_compatibility(ROOT,ROOT/'config.toml')
        started = time.monotonic()
        tree = ProcessTree([str(PY),'-B',str(ROOT/'lifesupervisor.py')],cwd=ROOT,env=dict(os.environ))
        say(f'发布控制器已启动，PID {tree.process.pid}')
        try:
            code = tree.process.wait()
        finally:
            tree.stop()
        say(f'发布控制器已退出，PID {tree.process.pid}，exit_code={code}，运行 {time.monotonic()-started:.1f} 秒')
        if (ROOT/'data/life-control/stop.requested').exists() or code==0:
            return 0
        if code==42:
            failures = 0
            say('发布控制器代码已更新，重新加载。')
            continue
        failures = 1 if time.monotonic()-started>=60 else failures+1
        delay = min(30,2**min(failures,5))
        say(f'发布控制器异常退出，{delay} 秒后恢复；详细异常见 supervisor-console.log。')
        time.sleep(delay)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say('收到主动中止，停止看守。')
    except Exception:
        say('看守异常退出：\n'+traceback.format_exc())
        raise SystemExit(1)
    finally:
        drop_lock()
