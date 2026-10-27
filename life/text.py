#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文友好的轻量文本处理：二元字索引、token 估算、稳定哈希。

首版按设计第 6.4 节先用词法基线，不引入向量库。
"""

from __future__ import annotations

import hashlib
import re

_LATIN = re.compile(r"[a-z0-9_]{2,}")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_SPACE = re.compile(r"\s+")


def terms(text: str) -> list[str]:
    """返回检索词：拉丁词 + 汉字二元组（单字也保留一个）。

    中文没有空格，FTS5 默认分词对中文不友好；二元字索引在小数据量下
    足够稳定，也方便以后换成真正的分词器而不改表结构。
    """
    s = (text or "").lower()
    out: list[str] = []
    seen: set[str] = set()
    for w in _LATIN.findall(s):
        if w not in seen:
            seen.add(w)
            out.append(w)
    # Punctuation/whitespace are real word boundaries. Joining all Han
    # characters manufactured matches such as “甲，乙” -> “甲乙”.
    for run in re.findall(r'[\u4e00-\u9fff]+', s):
        pieces = [run] if len(run) == 1 else [a+b for a, b in zip(run, run[1:])]
        for piece in pieces:
            if piece not in seen:
                seen.add(piece)
                out.append(piece)
    return out


def estimate_tokens(text: str) -> int:
    """保守估算：汉字约 1 token/字，拉丁约 4 字符/token。"""
    s = text or ""
    if not s:
        return 0
    han = len(_HAN.findall(s))
    other = len(s) - han
    return max(1, han + other // 4)


def stable_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{h}"


def one_line(text: str, limit: int = 160) -> str:
    s = _SPACE.sub(" ", (text or "").strip())
    return s[:limit]


def normalize_value(text: str) -> str:
    return one_line(text, 200).lower()
