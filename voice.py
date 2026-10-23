#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调音色用：列声音、批量试听，不用开着 QQ。

    python voice.py --list                  # 所有中文声音
    python voice.py                         # 几个候选各合成一句
    python voice.py --voice zh-CN-XiaoxiaoNeural --pitch +30Hz --rate +5%

生成的文件在 data/voice/preview/，双击就能听。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import edge_tts

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = Path(__file__).resolve().parent / "data" / "voice" / "preview"
SAMPLE = "诶，你回来啦喵~ 今天怎么这么晚，我等你等好久了"

# (声音, 音高, 语速, 标签)
CANDIDATES = [
    ("zh-CN-XiaoyiNeural", "+16Hz", "+4%", "小艺-活泼"),
    ("zh-CN-XiaoxiaoNeural", "+20Hz", "+2%", "小晓-温柔"),
    ("zh-CN-XiaoyiNeural", "+40Hz", "+8%", "小艺-更幼"),
    ("zh-CN-liaoning-XiaobeiNeural", "+10Hz", "+2%", "小北-东北话"),
    ("zh-TW-HsiaoChenNeural", "+14Hz", "+2%", "晓臻-台湾腔"),
]


async def main() -> None:
    ap = argparse.ArgumentParser(description="试听音色")
    ap.add_argument("--list", action="store_true", help="列出所有中文声音")
    ap.add_argument("--voice", help="只试这一个声音")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--text", default=SAMPLE)
    a = ap.parse_args()

    if a.list:
        voices = await edge_tts.list_voices()
        rows = [v for v in voices if v.get("Locale", "").startswith("zh")]
        for v in sorted(rows, key=lambda x: x["ShortName"]):
            tags = v.get("VoiceTag", {}).get("VoicePersonalities", [])
            print(f'{v["ShortName"]:<34} {v.get("Gender",""):<7} {v["Locale"]:<8} {"/".join(tags)}')
        print(f"\n共 {len(rows)} 个中文声音")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [(a.voice, a.pitch, a.rate, "custom")] if a.voice else CANDIDATES
    for i, (voice, pitch, rate, tag) in enumerate(jobs, 1):
        path = OUT / f"{i:02d}_{tag}.mp3"
        await edge_tts.Communicate(a.text, voice, rate=rate, pitch=pitch).save(str(path))
        print(f"{path.name:<26} {voice:<34} pitch={pitch:<6} rate={rate}")

    print(f"\n都在这儿：{OUT}")
    print("听完把选中的写进 config.toml：voice_name / voice_pitch / voice_rate")


if __name__ == "__main__":
    asyncio.run(main())
