#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真相层：所有动作的结果都是一个带证据的结构，而不是一句"成功了"。

她能不能"从经验里学"，取决于她的眼睛会不会撒谎。
所以这里只有一件事：**任何动作都必须能说清"我看到了什么"**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Result:
    """一次看/点/打/验的结果。ok 只代表"动作发生了且看到了预期"，不代表事情办成了。"""

    ok: bool
    evidence: str = ""
    error: str = ""
    ms: int = 0
    image: str = ""                      # data URL（看屏幕时）
    data: dict = field(default_factory=dict)

    def text(self) -> str:
        head = "成功" if self.ok else "失败"
        body = self.evidence if self.ok else (self.error or self.evidence or "没有证据")
        return f"（{head}）{body}"

    def as_dict(self) -> dict:
        d = {"ok": self.ok, "evidence": self.evidence, "error": self.error, "ms": self.ms}
        if self.data:
            d["data"] = self.data
        return d


def ok(evidence: str, ms: int = 0, image: str = "", **data) -> Result:
    return Result(True, evidence=evidence, ms=ms, image=image, data=data)


def fail(error: str, evidence: str = "", ms: int = 0) -> Result:
    return Result(False, evidence=evidence, error=error, ms=ms)
