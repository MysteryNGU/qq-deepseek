#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""她能用的两只手：看屏幕、上网页。

规矩：不是自己乱跑，是"被模型点名才执行"。
每次执行都写进日志，你能看到她什么时候偷看了屏幕。
"""

from __future__ import annotations

import base64
import html
import io
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


# ---------------------------------------------------------------- 看屏幕


def screenshot_data_url(max_side: int = 1280, quality: int = 80) -> str:
    """截一张你现在的屏幕，压成 JPEG，返回 data URL。

    模型那边会自动缩到约 1300×1300，一张图最多算 1024 token，
    所以这里先压到 1280 长边，省流量也省不到哪去——主要是上传快。
    """
    from PIL import ImageGrab  # 只有需要时才依赖 Pillow

    try:
        img = ImageGrab.grab(all_screens=True)   # 多显示器一起截
    except TypeError:                            # 老版本 Pillow
        img = ImageGrab.grab()
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- 上网页


def _looks_like_css(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    return s.count("{") + s.count("}") + s.count(";") >= 2


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|canvas)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<!--.*?-->", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0\u3000]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    # 有些页面 style 标签没闭合，剩下的 CSS 会变成正文，按"像 CSS"整行丢掉
    lines = [ln for ln in text.split("\n") if not _looks_like_css(ln)]
    return "\n".join(lines).strip()


def fetch_text(url: str, limit: int = 4000) -> str:
    """抓一个网页，返回能读的正文。"""
    if not re.match(r"^https?://", url or "", re.I):
        return f"这不是个网址：{url}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })

    # 这网络 TLS 握手时不时超时，重试两次比让她说"打不开"强
    body = b""
    charset = "utf-8"
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                body = r.read(3_000_000)
            last = None
            break
        except Exception as e:
            last = e
            time.sleep(0.7 * (attempt + 1))
    if last is not None:
        return f"打不开这个网址（{type(last).__name__}: {last}）"

    text = _html_to_text(body.decode(charset, "replace"))
    if len(text) > limit:
        text = text[:limit] + f"\n……（后面还有 {len(text) - limit} 字，太长了）"
    return text or "（这页没什么字，可能内容要靠 JS 才加载出来）"

# ---------------------------------------------------------------- 收图


def image_data_url(url: str, max_side: int = 1024) -> str:
    """把别人发来的图下载下来，压成 data URL 给视觉模型看。"""
    from PIL import Image

    if not url:
        return ""
    u = html.unescape(url)
    req = urllib.request.Request(u, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read(8_000_000)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- 说话（TTS）

_VOICE_DIR = Path(__file__).resolve().parent / "data" / "voice"


async def tts(text: str, voice: str = "zh-CN-XiaoyiNeural", *,
              rate: str = "+0%", pitch: str = "+0Hz", volume: str = "+0%") -> Path:
    """把一句话合成成 mp3。NapCat 收到 record 段会自己转码发出去。

    rate/pitch/volume 是 edge-tts 的调节字符串，形如 "+10%"、"-20Hz"。
    """
    import edge_tts

    _VOICE_DIR.mkdir(parents=True, exist_ok=True)
    out = _VOICE_DIR / f"{int(time.time() * 1000)}.mp3"
    await edge_tts.Communicate(
        text, voice, rate=rate, pitch=pitch, volume=volume
    ).save(str(out))

    return out

# ---------------------------------------------------------------- 表情包

def file_data_url(path, max_side: int = 1024) -> str:
    """本地图片文件 → data URL。"""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def random_sticker(folder) -> str:
    """从 stickers/ 里随便挑一张。没有就返回空串。"""
    import random

    try:
        files = [p for p in folder.iterdir()
                 if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}]
    except OSError:
        return ""
    if not files:
        return ""
    return file_data_url(random.choice(files))

# ---------------------------------------------------------------- QQ 空间

def _qzone_get(url: str, cookies: str, referer: str = "https://user.qzone.qq.com/") -> str:
    req = urllib.request.Request(url, headers={
        "Cookie": cookies, "User-Agent": UA, "Referer": referer,
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(500_000).decode("utf-8", "replace")


def _callback_json(body: str):
    i, j = body.find("("), body.rfind(")")
    if i != -1 and j > i:
        try:
            return json.loads(body[i + 1:j])
        except Exception:
            pass
    try:
        return json.loads(body)
    except Exception:
        return None


def qzone_own_posts(cookies: str, gtk: str, uin: int, num: int = 5) -> list[str]:
    """她自己发过的说说。"""
    qs = urllib.parse.urlencode({
        "uin": uin, "ftype": 0, "sort": 0, "pos": 0, "num": num,
        "replynum": 0, "g_tk": gtk, "code": 0, "format": "json",
    })
    url = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/"
           "emotion_cgi_msglist_v6?" + qs)
    try:
        data = _callback_json(_qzone_get(url, cookies, f"https://user.qzone.qq.com/{uin}")) or {}
    except Exception:
        return []
    out = []
    for m in (data.get("msglist") or [])[:num]:
        txt = re.sub(r"<[^>]+>", "", m.get("content") or "")
        txt = html.unescape(txt).strip()
        if txt:
            out.append(txt[:120])
    return out


def qzone_feeds(cookies: str, gtk: str, uin: int, num: int = 8) -> list[str]:
    """好友动态。best-effort：接口一变就可能空，空了就当没看见。"""
    qs = urllib.parse.urlencode({
        "uin": uin, "scope": 0, "view": 1, "filter": "all", "flag": 1,
        "applist": "all", "fupdate": 1, "face": 0, "version": 8,
        "g_tk": gtk, "page": 1, "count": num,
    })
    url = ("https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/"
           "feeds/feeds3_html_more?" + qs)
    try:
        body = _qzone_get(url, cookies, f"https://user.qzone.qq.com/{uin}")
    except Exception:
        return []
    out: list[str] = []
    for m in re.finditer(r"content\s*:\s*[\"'](.{4,200}?)[\"']", body):
        txt = re.sub(r"<[^>]+>", "", m.group(1))
        txt = html.unescape(txt).strip()
        if txt and txt not in out:
            out.append(txt[:120])
        if len(out) >= num:
            break
    return out
