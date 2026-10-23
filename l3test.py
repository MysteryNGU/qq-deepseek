"""L3 联调：假 OneBot + 真 Bot + 真 DeepSeek + 真 DSH 工具 turn。

不碰真实 QQ。假服务器只实现收到消息和回执所需的最小 OneBot v11 协议。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent
PYTHON = Path(r"C:\Users\GEUEK\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe")
OWNER = 763282103


def _text_of(message) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "".join(str(p.get("data", {}).get("text", "")) for p in message
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


async def main() -> int:
    raw = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))
    api_key = os.environ.get("DEEPSEEK_API_KEY") or str(raw.get("api_key") or "")
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY")
        return 2

    replies: list[tuple[float, str]] = []
    connected = asyncio.Event()
    stop = asyncio.Event()

    async def handler(ws):
        connected.set()
        try:
            async for frame in ws:
                try:
                    msg = json.loads(frame)
                except Exception:
                    continue
                echo = msg.get("echo")
                action = msg.get("action")
                if echo is not None:
                    await ws.send(json.dumps({"status": "ok", "retcode": 0,
                                              "data": {"message_id": int(time.time() * 1000)},
                                              "echo": echo}, ensure_ascii=False))
                if action in ("send_private_msg", "send_msg", "send_group_msg"):
                    text = _text_of((msg.get("params") or {}).get("message"))
                    if text:
                        replies.append((time.time(), text))
                        print("BOT_SENT", text[:300].replace("\n", " "), flush=True)
        except Exception:
            pass

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    print("FAKE_ONEBOT", port, flush=True)

    cfg_path = ROOT / "data" / "l3-config.toml"
    cfg_path.parent.mkdir(exist_ok=True)
    cfg = {
        "name": "L3",
        "persona": str(raw.get("persona") or "persona.md"),
        "base_url": str(raw.get("base_url") or "https://api.deepseek.com"),
        "model": str(raw.get("model") or "deepseek-flash"),
        "owner": OWNER,
        "allow_all_private": True,
        "reply_private": True,
        "stranger_chat": True,
        "life": False,
        "life_shadow": True,
        "tools": True,
        "dsh_bridge": True,
        "dsh_root": r"D:/projects/dsh-bridge-work",
        "dsh_home": str(ROOT / "data" / "dsh-home-l3"),
        "dsh_tmp": r"D:/projects/tools/tmp",
        "db": str(ROOT / "data" / "l3-brain.db"),
        "life_db": str(ROOT / "data" / "l3-life.db"),
        "channel": "ws",
        "console": False,
        "diary": False,
        "night_nudge": False,
        "autonomy": False,
        "evolution": False,
        "autonomous": False,
        "phone": False,
        "screen": False,
        "web": False,
    }
    lines = []
    for k, v in cfg.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, int):
            lines.append(f"{k} = {v}")
        else:
            lines.append(f'{k} = {json.dumps(str(v), ensure_ascii=False)}')
    cfg_path.write_text("\n".join(lines) + "\n", "utf-8")

    env = os.environ.copy()
    env.update({
        "DEEPSEEK_API_KEY": api_key,
        "QQ_WS_URL": f"ws://127.0.0.1:{port}",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "DSH_CLIENT_COMMIT_HASH": "0000000",
        "TMP": r"D:/projects/tools/tmp",
        "TEMP": r"D:/projects/tools/tmp",
    })
    log_path = ROOT / "data" / "l3-bot.log"
    log_fh = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(PYTHON), str(ROOT / "l3run.py"), str(cfg_path)],
        cwd=str(ROOT), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        await asyncio.wait_for(connected.wait(), 30)
        await asyncio.sleep(3)
        sent_at = time.time()
        event = {
            "post_type": "message", "message_type": "private", "sub_type": "friend",
            "self_id": 10000, "user_id": OWNER, "message_id": int(time.time() * 1000),
            "time": int(time.time()),
            "raw_message": "帮我看看 DSH 有没有后台任务，用 dsh_bridge 的 job_list。",
            "message": [{"type": "text", "data": {"text": "帮我看看 DSH 有没有后台任务，用 dsh_bridge 的 job_list。"}}],
            "sender": {"user_id": OWNER, "nickname": "主人"},
        }
        # 从已连接的假服务器广播给 bot
        for ws in list(server.connections):
            await ws.send(json.dumps(event, ensure_ascii=False))
        print("EVENT_SENT", flush=True)

        deadline = time.time() + 240
        while time.time() < deadline:
            got = [t for ts, t in replies if ts >= sent_at]
            if got and any(("no background jobs" in t.lower() or "没有后台任务" in t
                            or "目前没有" in t or "没有正在运行" in t) for t in got):
                print("L3_OK", flush=True)
                return 0
            if proc.poll() is not None:
                print("BOT_EXIT", proc.returncode, flush=True)
                break
            await asyncio.sleep(1)
        got = [t for ts, t in replies if ts >= sent_at]
        print("L3_REPLIES", got, flush=True)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
        server.close()
        await server.wait_closed()
        print("BOT_LOG", log_path, flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
