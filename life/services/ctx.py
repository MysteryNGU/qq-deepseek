#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单次行动上下文：来源、会话、审批范围。

用 contextvars 而不是全局变量，避免自主任务和聊天任务互相污染。
"""

from __future__ import annotations

from contextvars import ContextVar

# commanded = 主人吩咐；initiative = 她自己想做；exploration = 已授权的只读探索
current_origin: ContextVar[str] = ContextVar("role_origin", default="commanded")
current_session: ContextVar[str] = ContextVar("role_session", default="")
current_trace: ContextVar[str] = ContextVar("role_trace", default="")
current_goal: ContextVar[str] = ContextVar("role_goal", default="")
current_task: ContextVar[dict | None] = ContextVar("action_task_binding", default=None)


def set_origin(origin: str) -> None:
    current_origin.set(origin or "commanded")


def get_origin() -> str:
    return current_origin.get() or "commanded"


def set_goal(goal: str) -> None:
    """这一轮/这件事的概括目标，用来做计划级授权，而不是每一步都问。"""
    current_goal.set(str(goal or "")[:500])


def get_goal() -> str:
    return current_goal.get() or ""
