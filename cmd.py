#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地命令口客户端：连 bot 的 127.0.0.1:8765，直接和她说话。

输出是一问一答：

    你> 你现在在做什么
    她> 我在动态页找右下角那个加号喵

为什么改成"问完等回、再问下一句"：
以前是 `input()` 和异步收信同时跑，她的回复会把她还没打完的字冲掉，
屏幕上就变成 "你> （收到……）" 这种错位。现在一次只走一轮，不会串行错乱。
"""

from __future__ import annotations

import asyncio
import sys


async def collect_reply(reader: asyncio.StreamReader, first: float = 120.0,
                        more: float = 0.5) -> list[str] | None:
    """等一条回复：等到返回行列表；超时返回 None；连接断了返回 []。"""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=first)
    except asyncio.TimeoutError:
        return None
    if not line:
        return []
    out = [line.decode("utf-8", "replace").rstrip()]
    # 一次回复可能是好几行（比如 /记忆），把已经到的都收干净
    while True:
        try:
            nxt = await asyncio.wait_for(reader.readline(), timeout=more)
        except asyncio.TimeoutError:
            break
        if not nxt:
            break
        out.append(nxt.decode("utf-8", "replace").rstrip())
    return out


async def main(port: int) -> None:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        print(f"连不上 127.0.0.1:{port}；先确认 bot 在跑（命令口会打印在 bot.log）。")
        return
    try:
        hello = await reader.readline()
        if hello:
            print(hello.decode("utf-8", "replace").rstrip())
    except Exception:
        pass
    print("（一问一答；Ctrl+C 退出）")

    try:
        while True:
            try:
                text = await asyncio.to_thread(input, "你> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            text = (text or "").strip()
            if not text:
                continue
            try:
                writer.write((text + "\n").encode("utf-8"))
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                print("（连接断了）")
                break
            try:
                lines = await collect_reply(reader)
            except KeyboardInterrupt:
                print()
                break
            if lines is None:
                print("她> （等太久了，她可能还卡在手机上；再发一句试试）")
                continue
            if not lines:
                print("（连接断了）")
                break
            for ln in lines:
                print("她> " + ln)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8765))
    except KeyboardInterrupt:
        pass
