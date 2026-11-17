# -*- coding: utf-8 -*-
"""记忆使用的证据，而不是对模型心理过程的猜测。

五个层次分开：库里有 / 网络有关联 / 召回命中 / 本轮装入 / 实际使用。
前四个是程序事实；"实际使用"只在有可解析证据时记为已确认，其余一律记
"已装入，使用未确认"——不能从一句自然回复推断她真的用到了某条材料。
"""
from __future__ import annotations

import json
import time
import uuid

from .text import stable_id

EXPLICIT_REF = 'explicit_ref'   # 结构化回复或工具参数里带了可解析的 source_ref
READ = 'read'                   # 沿 ref 读取并成功返回内容
ACTION_LINK = 'action_link'     # 行动关联 source_ref 且取得对应结果回执

KINDS = (EXPLICIT_REF, READ, ACTION_LINK)

SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_context_loads (
  decision_id   TEXT PRIMARY KEY,
  agent_id      TEXT NOT NULL,
  slots         TEXT NOT NULL DEFAULT '{}',
  total_used    INTEGER NOT NULL DEFAULT 0,
  request_tokens INTEGER NOT NULL DEFAULT 0,
  refs          TEXT NOT NULL DEFAULT '[]',
  at            REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS companion_context_evidence (
  id            TEXT PRIMARY KEY,
  agent_id      TEXT NOT NULL,
  decision_id   TEXT NOT NULL,
  kind          TEXT NOT NULL,
  source_ref    TEXT NOT NULL,
  evidence_ref  TEXT NOT NULL DEFAULT '',
  at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_companion_evidence_decision
  ON companion_context_evidence(agent_id, decision_id, kind);
"""


def ensure(repo):
    with repo.tx():
        for statement in SCHEMA.strip().split(';'):
            if statement.strip():
                repo._exec(statement)


def record_load(repo, *, agent_id, decision_id, report=None, refs=(), request_tokens=0, now=None):
    """记录这一轮实际装入了什么。report 来自 companion_life_material.assemble。"""
    at = time.time() if now is None else float(now)
    ensure(repo)
    report = report or {}
    slots = {slot: dict(row) for slot, row in report.items()
             if isinstance(row, dict) and 'used' in row}
    with repo.tx():
        repo._exec('INSERT OR REPLACE INTO companion_context_loads'
                   '(decision_id,agent_id,slots,total_used,request_tokens,refs,at) VALUES(?,?,?,?,?,?,?)',
                   (str(decision_id), agent_id, json.dumps(slots, ensure_ascii=False),
                    int(report.get('total_used') or 0), int(request_tokens or 0),
                    json.dumps([str(r) for r in refs][:200], ensure_ascii=False), at))
    return dict(decision_id=decision_id, slots=slots, total_used=int(report.get('total_used') or 0))


def record(repo, *, agent_id, decision_id, kind, source_ref, evidence_ref='', now=None):
    """登记一条使用证据。同一 决定/类型/来源 只记一次。"""
    if kind not in KINDS or not source_ref:
        return None
    at = time.time() if now is None else float(now)
    ensure(repo)
    ident = stable_id('companion-evidence', str(decision_id), kind, str(source_ref))
    with repo.tx():
        repo._exec('INSERT OR IGNORE INTO companion_context_evidence'
                   '(id,agent_id,decision_id,kind,source_ref,evidence_ref,at) VALUES(?,?,?,?,?,?,?)',
                   (ident, agent_id, str(decision_id), kind, str(source_ref), str(evidence_ref or ''), at))
    return dict(id=ident, kind=kind, source_ref=source_ref)


def resolve(repo, ref, agent_id=None):
    """引用必须能解析到真实来源，否则不能当作证据。"""
    if not ref:
        return None
    ref = str(ref)
    row = repo._one('SELECT id FROM events WHERE id=? AND redacted_at IS NULL', (ref,))
    if row:
        return dict(kind='event', ref=row['id'])
    row = repo._one('SELECT v FROM meta WHERE k=?', ('action_task:' + ref,))
    if row:
        return dict(kind='project', ref=ref)
    if ref.startswith('note:') and agent_id:
        row = repo._one('SELECT key FROM companion_notes WHERE agent=? AND key=?', (agent_id, ref[5:]))
        if row:
            return dict(kind='note', ref=ref)
    return None


def summary(repo, *, agent_id, decision_id):
    """这一轮的四个层次：装入 / 显式引用 / 读取 / 行动关联。"""
    ensure(repo)
    load = repo._one('SELECT * FROM companion_context_loads WHERE decision_id=?', (str(decision_id),))
    evidence = repo._all('SELECT kind,source_ref,evidence_ref FROM companion_context_evidence '
                         'WHERE agent_id=? AND decision_id=?', (agent_id, str(decision_id)))
    by_kind = {}
    for row in evidence:
        by_kind.setdefault(row['kind'], []).append(row['source_ref'])
    loaded = json.loads(load['refs']) if load else []
    used = set()
    for refs in by_kind.values():
        used.update(refs)
    return dict(
        # 哪些证据类型真的接线了，哪些还没有。不接线的类型不会被算作"未使用"，
        # 但也不能假装有数据。
        kinds_instrumented={EXPLICIT_REF: True, READ: True, ACTION_LINK: True},
        scope='explicit_ref=回复或工具参数含可解析ref；read=沿ref读取并返回内容；'
              'action_link=行动关联ref且有结果回执。三者都只证明关联，不证明结论正确。',
        loaded_items=len(loaded),
        loaded_refs=loaded,
        total_used_tokens=int(load['total_used']) if load else None,
        request_tokens=int(load['request_tokens']) if load else None,
        explicit_refs=sorted(set(by_kind.get(EXPLICIT_REF, []))),
        reads=sorted(set(by_kind.get(READ, []))),
        action_links=sorted(set(by_kind.get(ACTION_LINK, []))),
        used_confirmed=sorted(used),
        loaded_unconfirmed=len([r for r in loaded if r not in used]),
        note='已装入、使用未确认' if loaded and not used else '',
    )


def library_counts(life, scope):
    """她"库里有"多少东西——和这一轮装入了多少不是一回事。"""
    counts = {}
    for key, sql in (
        ('memories', 'SELECT COUNT(*) AS n FROM memories WHERE agent_id=?'),
        ('notes', 'SELECT COUNT(*) AS n FROM companion_notes WHERE agent=?'),
        ('activities', 'SELECT COUNT(*) AS n FROM companion_activity WHERE agent=?'),
    ):
        try:
            row = life.repo._one(sql, (life.agent_id,))
            counts[key] = int(row['n']) if row else 0
        except Exception:
            counts[key] = None
    return counts
