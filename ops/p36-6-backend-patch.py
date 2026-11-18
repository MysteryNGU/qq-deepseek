#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P36-6 后端 patch：在 /mobile/state 里给手机端一个精简的「能力入口」。

**为什么需要后端改**：`mobile_view()` 现在的返回里**完全没有能力目录**
（keys = active/history/life/name/next_wake/notes/phase/projects/town），
而 `companion_awareness.capabilities()` 的条目只进模型上下文、没进手机响应。
前端只靠 `state.phone_context` 做不出「可用 + 不可用里最重要的 3 条」。

**设计约束（captain 定的）**：
- 最多 6 条：可用 3 + 不可用 3
- **不带 raw / source_ref / evidence**（手机响应要小）
- 状态词**原样输出**，不翻译（`needs_setup` 和 `offline` 必须能被前端区分）
- 读不到就返回 None/[]，**不编**

**接法**：`mobile_view(snapshot, ...)` 只拿到快照、拿不到 core，
所以不要在 mobile_view 里去调 capabilities()（那需要 core）。
正确做法是在 **state handler** 里算好、塞进 snapshot 的一个私有键，
再由 mobile_view 原样带出。见下方两处 diff。

---
改动 1/2：life/companion_mobile.py，在 `def mobile_view(...)` 之前新增
---
"""
from __future__ import annotations

PATCH_FUNC = '''
CAPABILITY_BRIEF_LIMIT = 3      # 可用最多 3 条
CAPABILITY_OFFLINE_LIMIT = 3    # 不可用最多 3 条


def _capability_brief(snapshot):
    """手机端能力入口：可用 ≤3 + 不可用 ≤3，只带 name/status/reason。

    数据来自 companion_awareness.capabilities()，由 state handler 预先放进
    snapshot['_capability_brief']。**读不到就返回空列表，不编。**
    """
    rows = snapshot.get('_capability_brief')
    if not isinstance(rows, list):
        return []
    usable, rest = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        brief = dict(name=row.get('capability_ref'),
                     status=row.get('status'),         # 原样输出，不翻译
                     reason=row.get('reason'))
        if row.get('status') == 'ready':
            usable.append(brief)
        else:
            rest.append(brief)
    return (usable[:CAPABILITY_BRIEF_LIMIT] + rest[:CAPABILITY_OFFLINE_LIMIT])
'''

PATCH_VIEW = '''
    # 能力入口（P36-6）：可用 ≤3 + 不可用 ≤3，状态词原样带给手机。
    capabilities = _capability_brief(snapshot)
'''

PATCH_STATE = '''
    async def state(self,request):
        ...
        snapshot['_capability_brief'] = _collect_capability_brief(core)
'''

PATCH_COLLECT = '''
def _collect_capability_brief(core):
    """在 state handler 里调用：把能力目录整理成精简行放进 snapshot。

    与 companion_awareness.capabilities() 用同一份数据，
    所以手机看到的 status 和模型读到的完全一致。读不到就返回 None。
    """
    try:
        from . import companion_awareness as awareness
        items = awareness.capabilities(core, state=core.snapshot(include_image_data=False))
    except Exception:
        return None
    if isinstance(items, dict):
        items = items.get('items') or []
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        rows.append(dict(capability_ref=item.get('capability_ref'),
                         status=item.get('status'),
                         reason=item.get('reason')))
    return rows or None
'''

if __name__ == '__main__':
    print('=' * 70)
    print('P36-6 后端 patch（三处，均为新增，不改既有行为）')
    print('=' * 70)
    print('\n[1] life/companion_mobile.py — 在 `def mobile_view(...)` 之前插入：')
    print(PATCH_FUNC)
    print('\n[2] life/companion_mobile.py — mobile_view() 的 view = dict(...) 里追加一个键：')
    print(PATCH_VIEW)
    print('\n[3] life/companion_mobile.py — state handler 里，取到 snapshot 之后：')
    print("    snapshot['_capability_brief'] = _collect_capability_brief(core)")
    print('\n且在该文件模块级新增：')
    print(PATCH_COLLECT)
    print('\n注意：`core` 在 state handler 里可用（现有代码已用 `core=getattr(self.server,"core",None)`）。')
    print('若 core 为 None，_collect_capability_brief 会抛异常并被吞成 None → 前端显示"读不到"。')
