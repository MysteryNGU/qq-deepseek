# -*- coding: utf-8 -*-
"""Machine-checkable acceptance for the companion capability chain (task t5).

Run on the cloud host, as the companion user:

    cd /opt/qq-deepseek
    .venv-companion/bin/python -B -X utf8 ops/verify-capability-chain.py \\
        --out .acceptance/<ts>-cap --db-copy /var/lib/companion/xiaoyu/data/life.db

Isolation is enforced by the script itself:

* a throw-away HOME under ``--out/tmp`` with HOME / USERPROFILE / XDG_* / TMPDIR
  all redirected before ``life.*`` is imported;
* the production database is only read (SQLite backup API over ``mode=ro``);
  the source file's size + mtime + sha256 are compared before and after;
* no QQ, no model call (FakeModel), no systemctl, no network: subprocess.run,
  asyncio.create_subprocess_* and socket.connect raise ExternalCallBlocked while
  the checks run, and every attempt is recorded in ``guard_events``;
* the production ``config.toml`` is never parsed; ``Config`` comes from
  ``CONFIG_TRANSCRIPT`` below, transcribed field by field from the deployed file
  (credentials blanked: this script never talks to a model).

Groups (``verdict`` at the end of the JSON):

  C1 manifest completeness    code-without-doc / doc-without-code
  C2 state truthfulness       fake connectors: ready / offline / needs_setup / unknown
  C3 projection format        usable / unusable / how + budget
  C4 on-demand reads          the reading ops the prompt tells her to use
  C5 experience memory        reproduce t4 in isolation
  C6 request projection       what the model actually sees this turn
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Transcription of the deployed /opt/qq-deepseek/config.toml (only what the
# acceptance needs). api_key is blank on purpose.
CONFIG_TRANSCRIPT = {
    'name': '\u5c0f\u9c7c', 'owner': 763282103, 'life_db': 'data/life.db',
    'life_agent': 'default', 'api_key': '', 'base_url': 'https://api.deepseek.com',
    'model': 'deepseek-flash', 'persona': 'persona.md', 'unified_persona': False,
    'public_persona': 'personas/public.md', 'resource_policy': {}, 'spending_policy': {},
    'memory_index': {}, 'embedding': {}, 'life_dynamics': {}, 'experience': {},
    'companion_autonomy': False, 'dsh_root': '', 'dsh_node': '', 'dsh_home': '',
    'dsh_provider': 'deepseek-official', 'media_library': {}, 'local_media': {},
    'profile_backfill': {}, 'memory_organize': {}, 'activity': {}, 'known_people': {},
    'friends': [], 'allow_groups': [], 'bot_qq': 763282103, 'quiet_hours': '',
    'handoff_seconds': 10, 'typing_chars_per_second': 3, 'paste_threshold': 1800,
    'media_max_mb': 64, 'media_local_roots': [], 'action_per_hour': 500,
    'action_per_day': 0, 'acknowledge_before_work': True, 'bili_plugin': '',
    'local_embedding': {}, 'experience_private': {},
}


def cfg_for(home):
    cfg = SimpleNamespace(**dict(CONFIG_TRANSCRIPT))
    cfg.service = SimpleNamespace(raw=None)
    return cfg


class FakeModel:
    """Empty decision, no model call: this acceptance never looks at model output."""

    MAX_OUTPUT_TOKENS = 8000

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        self.calls += 1
        payload = dict(reply='', followups=[], remember=[], projects=[], actions=[],
                       wake_after=0)
        return dict(message=dict(content=json.dumps(payload, ensure_ascii=False)),
                    finish_reason='completed', usage=None, model='fake')

    async def close(self):
        return None


# ------------------------------------------------------------------ guards

GUARD_EVENTS = []


class ExternalCallBlocked(RuntimeError):
    pass


class ExternalGuard:
    """Block child processes and outbound sockets; every attempt is recorded."""

    def __enter__(self):
        import subprocess
        original_popen = subprocess.Popen.__init__

        def blocked_popen(self_, *args, **kwargs):
            GUARD_EVENTS.append(dict(what='subprocess.Popen', args=str(args[:1])[:200]))
            raise ExternalCallBlocked('subprocess blocked: %r' % (args[:1],))

        self._popen = (subprocess.Popen, original_popen)
        subprocess.Popen.__init__ = blocked_popen

        async def blocked_exec(*args, **kwargs):
            GUARD_EVENTS.append(dict(what='create_subprocess_exec', args=str(args[:1])[:200]))
            raise ExternalCallBlocked('subprocess blocked: %r' % (args[:1],))

        async def blocked_shell(*args, **kwargs):
            GUARD_EVENTS.append(dict(what='create_subprocess_shell', args=str(args[:1])[:200]))
            raise ExternalCallBlocked('subprocess blocked: %r' % (args[:1],))

        self._exec = asyncio.create_subprocess_exec
        self._shell = asyncio.create_subprocess_shell
        asyncio.create_subprocess_exec = blocked_exec
        asyncio.create_subprocess_shell = blocked_shell

        original_connect = socket.socket.connect

        def blocked_connect(self_, address):
            host = address[0] if isinstance(address, tuple) and address else address
            if host in ('127.0.0.1', '::1', 'localhost'):
                return original_connect(self_, address)
            GUARD_EVENTS.append(dict(what='socket.connect', args=str(address)[:200]))
            raise ExternalCallBlocked('socket blocked: %r' % (address,))

        self._connect = (socket.socket, original_connect)
        socket.socket.connect = blocked_connect
        return self

    def __exit__(self, *info):
        cls, original = self._popen
        cls.__init__ = original
        asyncio.create_subprocess_exec = self._exec
        asyncio.create_subprocess_shell = self._shell
        cls, original = self._connect
        cls.connect = original
        return False


# ------------------------------------------------------------------ fakes

class FakePhone:
    """Stand-in for phone_context()."""

    def __init__(self, devices=None, connected=None):
        self.devices = list(devices or [])
        self.connected = connected

    def context(self):
        connected = self.connected
        if connected is None:
            connected = bool(self.devices)
        return dict(connected=bool(connected), devices=list(self.devices))


class FakeServer:
    """What RemoteExecutor looks at: peer / mobile / im."""

    def __init__(self, *, peer=None, phone=None, im=None):
        self.peer = peer
        self.mobile = SimpleNamespace(phone=FakePhone(**(phone or {})),
                                      devices={}, subscribers={})
        self.im = im
        self.qq = None


class FakeIm:
    """Channel manager whose snapshot() answers exactly what the scenario sets."""

    def __init__(self, qq=None, wechat=None):
        self.qq = None
        self.wechat = None
        self._snapshot = dict(channels={
            'qq': qq if qq is not None else dict(state='disabled', enabled=False),
            'wechat': wechat if wechat is not None else dict(state='disabled', enabled=False),
        })

    def snapshot(self):
        return self._snapshot


class FakeBase:
    """Base stand-in for ActionExecutor: only what RemoteExecutor forwards to."""

    def __init__(self, kinds, phone=None, dsh=False):
        self.kinds = list(kinds)
        self._phone = phone
        self._dsh = dsh

    def available_kinds(self):
        return list(self.kinds)

    def phone_context(self):
        return dict(self._phone or dict(connected=False, devices=[]))

    def _dsh_paths(self):
        return ('/dsh/stub', '/dsh/stub/node', 'lib/bin.js') if self._dsh else None

    def attach_repo(self, repo, agent_id):
        from life.companion_memory import Lookup
        self._memory = Lookup(repo, agent_id)
        self.repo, self.agent_id = repo, agent_id

    def attach_core(self, core):
        self.core = core

    def attach_agents(self, manager):
        self.agents = manager

    def attach_growth(self, store):
        self._growth = store

    async def close(self):
        return None


class FakeRemote(FakeBase):
    """Same shape as RemoteExecutor: .server reachable, .original reachable."""

    def __init__(self, kinds, *, peer=None, phone=None, im=None, dsh=False):
        super().__init__(kinds, phone=phone, dsh=dsh)
        self.server = FakeServer(peer=peer, phone=phone, im=im)
        self.original = FakeBase(kinds, phone=phone, dsh=dsh)
        self._real = None

    def available_kinds(self):
        kinds = [kind for kind in self.original.available_kinds() if kind != 'computer']
        try:
            phone_connected = bool(self.phone_context()['connected'])
            self.phone_error = None
        except Exception as error:  # noqa: BLE001 - a dead bridge just means "not connected"
            phone_connected, self.phone_error = False, '%s: %s' % (type(error).__name__, error)
        return (kinds + (['computer'] if self.server.peer is not None else [])
                + (['phone'] if phone_connected else []))

    def phone_context(self):
        return self.server.mobile.phone.context()

    async def run(self, action):
        """Same routing as RemoteExecutor.run: only computer/phone leave the host;
        everything else is dispatched by the real ActionExecutor (which owns the
        memory extension ops, growth, work, web...)."""
        from life.companion_executor import ActionExecutor
        if action.get('kind') in ('computer', 'phone'):
            return dict(status='failed', output='not exercised in this acceptance',
                        code='fake_connector')
        return await ActionExecutor.run(self._real, action)

    def attach_core(self, core):
        self.core = core
        if self._real is not None:
            self._real.attach_core(core)   # same wiring CompanionServer.start() does
            # the host wiring (repo/growth/agents) lives on the inner executor too
            if getattr(self, 'repo', None) is not None:
                self._real.attach_repo(self.repo, self.agent_id)

    def attach_repo(self, repo, agent_id):
        """Server-level wiring passes (None, None); remember it and keep going."""
        self.repo, self.agent_id = repo, agent_id

    def attach_real(self, real):
        """Bind the real executor used for dispatch; share the repo/growth wiring."""
        self._real = real


# ------------------------------------------------------------------ helpers

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def snapshot_sqlite(source, destination):
    """Read-only copy of the production DB; the source must not change."""
    before = (source.stat().st_size, source.stat().st_mtime_ns, sha256_file(source))
    origin = sqlite3.connect('file:%s?mode=ro' % source, uri=True, timeout=30)
    try:
        if destination.exists():
            os.chmod(destination, 0o600)
            destination.unlink()
        copy = sqlite3.connect(str(destination))
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()
    after = (source.stat().st_size, source.stat().st_mtime_ns, sha256_file(source))
    return dict(source=str(source), copy=str(destination), before=before, after=after,
                unchanged=before == after, copy_sha256=sha256_file(destination))


def make_home(root, *, seeds=None):
    """A brand-new temporary HOME."""
    home = Path(root)
    (home / 'data/companion').mkdir(parents=True, exist_ok=True)
    (home / 'skills').mkdir(parents=True, exist_ok=True)
    for name in ('persona.md', 'self.md'):
        (home / name).write_text('# test persona\n', encoding='utf-8')
    for relative, value in (seeds or {}).items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str)
                        else json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return home


def expose_home(db_path):
    """Realistic home layout: the DB sits at <home>/data/life.db.

    ``life_db`` in config is ``data/life.db`` relative to the home, so the copy
    has to live inside the sandbox home for any check to read the real ledger.
    """
    home = db_path.parent.parent
    (home / 'data/companion').mkdir(parents=True, exist_ok=True)
    (home / 'skills').mkdir(parents=True, exist_ok=True)
    for name in ('persona.md', 'self.md'):
        (home / name).write_text('# test persona\n', encoding='utf-8')
    return home


def attach_prod_evidence(core, db_path):
    """What the deployed ledger says about the two kinds that decide `ready`.

    ``agent.subagent`` becomes ready only when a completed agent task exists and
    ``action.dsh`` only when a DSH endpoint exists; a fresh sandbox HOME has
    neither, so without the production copy those two would be misreported as
    needs_setup. Only ``--prod-evidence`` reads them from the copy.
    """
    from life import companion_awareness as awareness_module

    conn = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
    try:
        row = conn.execute("SELECT id, verification FROM companion_agent_tasks "
                           "WHERE status='completed' ORDER BY updated DESC LIMIT 1").fetchone()
        total = conn.execute("SELECT COUNT(*) FROM companion_agent_tasks "
                             "WHERE status='completed'").fetchone()[0]
    finally:
        conn.close()

    class ProdAgentRepo:
        """Answers exactly the one query ``agent_capability`` makes; writes nothing."""

        def __init__(self, answer):
            self._answer = answer

        def _one(self, sql, args=()):
            return self._answer

        def _all(self, sql, args=()):
            return []

    saved = dict(repo=core.repo, original_dsh_endpoint=awareness_module._dsh_endpoint)
    awareness_module._dsh_endpoint = lambda executor: True
    core.repo = ProdAgentRepo(dict(id=row[0], verification=row[1]) if row else None)
    return saved, dict(dsh_endpoint=True, agent_completed_tasks=int(total or 0),
                       agent_last_verification=row[1] if row else None)


def restore_prod_evidence(core, saved):
    from life import companion_awareness as awareness_module
    core.repo = saved['repo']
    awareness_module._dsh_endpoint = saved['original_dsh_endpoint']


def make_builder():
    """A real Companion subclass whose ActionExecutor is a stand-in shell.

    Companion.__init__ constructs the executor itself when none is passed, so
    the shell is built from the real class (``__new__``) minus its private
    collaborators; ``attach_repo`` then wires the real ``companion_memory.Lookup``.
    """
    from life.companion import Companion
    from life.companion_agents import OPS as AGENT_OPS
    from life.companion_context import GUIDES as CONTEXT_GUIDES
    from life.companion_executor import ActionExecutor as RealExecutor
    from life import companion as companion_module
    from life import companion_awareness as awareness_module

    class Builder:
        DECISION = companion_module.DECISION
        OPS = tuple(AGENT_OPS)
        GUIDES = dict(CONTEXT_GUIDES)
        READ_OPS = tuple(awareness_module.READ_OPS)
        TRACKED_KINDS = tuple(awareness_module.TRACKED_KINDS)
        NAME = Companion.__name__

        def __init__(self):
            self._cores = []

        def __call__(self, cfg, home, model=None, executor=None, on_event=None):
            shell = RealExecutor.__new__(RealExecutor)
            shell.__dict__.update(dict(_work=None, _memory=None, _growth=None,
                                       _agents=None, _closed=False, _running=set(),
                                       home=Path(home), cfg=cfg))
            if hasattr(executor, 'attach_real'):
                executor.attach_real(shell)
            if executor is not None:
                # CompanionServer.start() re-wires repo/growth/agents onto the
                # executor it wraps; without a server the acceptance does the same.
                attach_repo = getattr(executor, 'attach_repo', None)
                if callable(attach_repo):
                    attach_repo(None, None)
                attach_growth = getattr(executor, 'attach_growth', None)
                if callable(attach_growth):
                    attach_growth(None)
            core = Companion(cfg, home, model=model or FakeModel(),
                             executor=executor or shell, on_event=on_event)
            # In service mode CompanionServer.start() calls attach_core on the
            # executor; without a server the acceptance must do the same, or every
            # memory extension read answers "executor not wired" - a harness gap,
            # not a product defect.
            attach_core = getattr(core.executor, 'attach_core', None)
            if callable(attach_core):
                attach_core(core)
            self._cores.append(core)
            return core

        def close_all(self):
            for core in self._cores:
                try:
                    core.life.close()
                except Exception:  # noqa: BLE001
                    pass
            self._cores = []

    return Builder()


def table_names(repo):
    return {row['name'] for row in repo._all(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def table_counts(repo):
    out = {}
    for name in sorted(table_names(repo)):
        try:
            row = repo._one('SELECT COUNT(*) AS n FROM "%s"' % name) or {}
            out[name] = int(row.get('n') or 0)
        except Exception:  # noqa: BLE001
            out[name] = None
    return out

# ------------------------------------------------------------------ C1

def c1_manifest(core):
    """code-without-doc / doc-without-code.

    Summary lines are matched by NAME against the catalogue, not by splitting on a
    separator: the summary format is ``name（short state）`` since t3-v3, and a
    separator-based parser silently turns a format change into 16 fake "doc only".
    Any line whose name cannot be matched is reported as ``unparsed_lines`` so a
    future format drift shows up instead of being swallowed.
    """
    import life.companion as companion_module
    from life import companion_awareness as awareness_module
    from life import companion_context as context_module
    from life.companion_awareness import capabilities, capability_summary

    guides = set(context_module.GUIDES)
    available = sorted(set(core.executor.available_kinds()))
    enum = sorted(companion_module.DECISION['properties']['actions']['items']
                  ['properties']['kind']['enum'])
    tracked = list(awareness_module.TRACKED_KINDS)
    code_kinds = sorted(set(available) | set(enum) | set(tracked))
    catalogue = capabilities(core, dict(available_actions=available))
    summary = capability_summary(catalogue)

    # name -> ref, for both the short action names and the dotted refs.
    names = {}
    for item in catalogue:
        ref = str(item.get('capability_ref') or '')
        names[ref] = ref
        names[ref.replace('action.', '')] = ref
    names.setdefault('agent', 'agent.subagent')

    def leading_name(line):
        text = line.strip()
        for name in sorted(names, key=len, reverse=True):
            if text.startswith(name):
                rest = text[len(name):]
                if not rest or rest[0] in '（(：:' or rest[0].isspace():
                    return name
        return None

    documented, unparsed, summary_map = set(), [], {}
    for layer in ('usable', 'unusable'):
        for line in summary.get(layer) or []:
            name = leading_name(line)
            if name is None:
                unparsed.append(dict(layer=layer, line=line[:80]))
                continue
            documented.add(name)
            summary_map[name] = dict(layer=layer, line=line[:120])
    documented |= guides
    if 'kind=agent' in companion_module.AGENT_INSTRUCTIONS:
        documented.add('agent')

    code_without_doc = [kind for kind in code_kinds if kind not in documented]
    # Only *action kinds* can be "documented but not implemented": channels and
    # guard.watchdog are deliberately not action kinds (they are surfaced in the
    # summary so she knows they exist, but she never dispatches to them).
    doc_without_code = [kind for kind in sorted(documented)
                        if kind not in code_kinds and kind in enum]
    # every name mentioned in the summary must be a real entry in the catalogue
    summary_names_unknown = [name for name in summary_map if name not in names]
    missing_from_summary = [str(item.get('capability_ref')) for item in catalogue
                            if str(item.get('capability_ref')).replace('action.', '')
                            not in summary_map
                            and str(item.get('capability_ref')) != 'agent.subagent'
                            and str(item.get('capability_ref')) not in summary_map]
    dispatched = dict(action_schema_enum=enum,
                      executor_available=available,
                      guides_with_instructions=sorted(guides),
                      guides_injected_for_available=sorted(
                          kind for kind in guides if kind in available))
    return dict(available=available, decision_enum=enum, tracked=tracked,
                code_kinds=code_kinds, documented_kinds=sorted(documented),
                code_without_doc=code_without_doc, doc_without_code=doc_without_code,
                unparsed_summary_lines=unparsed,
                summary_names_not_in_catalogue=summary_names_unknown,
                catalogue_entries_missing_from_summary=missing_from_summary,
                summary_map=summary_map,
                agent_explained_by=dict(in_guides='agent' in guides,
                                        in_agent_instructions=True,
                                        in_capability_entry='agent.subagent' in names),
                dispatch_evidence=dispatched,
                guides=sorted(guides)), summary, catalogue


# ------------------------------------------------------------------ C2

def build_c2(factory, tmp):
    """One real Companion per scenario, each with a fake connector."""
    scenarios = {}

    def start(name, remote):
        home = make_home(tmp / ('c2-' + name))
        core = factory(cfg_for(home), home, model=FakeModel(), executor=remote)
        if hasattr(remote, 'attach_core'):
            remote.attach_core(core)
        return core

    kinds = ['web', 'command', 'skill', 'work', 'memory', 'growth']
    im_ok = FakeIm(qq=dict(state='connected', enabled=True, account='763282103', error=''),
                   wechat=dict(state='unconfigured', enabled=False))
    scenarios['ready'] = dict(
        remote=FakeRemote(kinds, peer=('ws', object()),
                          phone=dict(devices=[dict(device_id='d1', name='pair-1',
                                                   has_stream=True, last_seen=time.time())]),
                          im=im_ok, dsh=True))
    im_off = FakeIm(qq=dict(state='disconnected', enabled=True, account=None, error=''),
                    wechat=dict(state='login_required', enabled=True))
    scenarios['offline'] = dict(
        remote=FakeRemote(kinds, peer=None, phone=dict(devices=[]), im=im_off, dsh=False))
    im_dis = FakeIm(qq=dict(state='disabled', enabled=False, account=None, error=''),
                    wechat=dict(state='unconfigured', enabled=False))
    scenarios['needs_setup'] = dict(
        remote=FakeRemote(kinds, peer=None,
                          phone=dict(devices=[dict(device_id='d1', name='pair-1',
                                                   has_stream=False, last_seen=time.time())]),
                          im=im_dis, dsh=False))
    # phone registered inside the 90 s window but without a live stream: the
    # executor still reports it available while dispatch would fail (t1 F9).
    scenarios['phone_registered_no_stream'] = dict(
        remote=FakeRemote(kinds, peer=None,
                          phone=dict(connected=True, devices=[dict(
                              device_id='d1', name='pair-1', has_stream=False,
                              last_seen=time.time())]),
                          im=im_dis, dsh=False))
    # phone_context itself raises: the reason must not invent a cause.
    scenarios['phone_unavailable'] = dict(
        remote=FakeRemote(kinds, peer=None, phone=dict(devices=[]), im=im_dis, dsh=False))

    class BrokenPhone:
        def context(self):
            raise RuntimeError('phone bridge down')

    scenarios['phone_unavailable']['remote'].server.mobile.phone = BrokenPhone()

    # IM object exists but its snapshot() raises: "asked and failed" must read as
    # unknown, never fall back to the config file and claim "turned off".
    # (The config file here says enabled=false on purpose: that is exactly the
    #  trap - falling back would report "主动关的" for a channel we know nothing about.)
    class BrokenIm(FakeIm):
        def snapshot(self):
            raise RuntimeError('im snapshot unavailable')

    scenarios['channel_snapshot_broken'] = dict(
        remote=FakeRemote(kinds, peer=None, phone=dict(devices=[]),
                          im=BrokenIm(qq=dict(state='disabled', enabled=False),
                                      wechat=dict(state='disabled', enabled=False)),
                          dsh=False))

    scenarios['unknown'] = dict(
        remote=FakeRemote(kinds, peer=None, phone=dict(devices=[]), im=BrokenIm(), dsh=False))
    for name, row in scenarios.items():
        row['core'] = start(name, row['remote'])
    return scenarios


EXPECT = {
    'ready': {
        'channel.qq': 'ready', 'channel.wechat': 'needs_setup',
        'action.phone': 'ready', 'action.computer': 'ready',
        'action.web': 'ready', 'action.command': 'ready',
        'action.town': 'needs_setup', 'action.bilibili': 'needs_setup',
    },
    'offline': {
        'channel.qq': 'offline', 'channel.wechat': 'needs_setup',
        'action.phone': 'offline', 'action.computer': 'offline',
        'action.body': 'offline',
    },
    'needs_setup': {
        'channel.qq': 'offline', 'channel.wechat': 'needs_setup',
        'action.phone': 'ready', 'action.computer': 'offline',
    },
    'unknown': {
        'channel.qq': 'needs_setup', 'channel.wechat': 'needs_setup',
        'action.phone': 'offline',
    },
    # Same situation as phone_registered_no_stream (the two used to disagree;
    # t5 rev-1 wrongly expected a status downgrade here - see REPORT §10).
    'phone_registered_no_stream': {
        'action.phone': 'ready',
    },
    'phone_unavailable': {
        'action.phone': 'unknown',
    },
    'channel_snapshot_broken': {
        'channel.qq': 'needs_setup', 'channel.wechat': 'needs_setup',
    },
}

# Extra assertions per scenario that a plain status map cannot express.
EXTRA = {
    'needs_setup': dict(
        dispatchable={'action.phone': False},
        # an entry may carry a reason while ready - only if it explains a caveat
        ready_reason_must_not_assert=('action.phone', ('本机没有', '没有连上', '被关掉')),
    ),
    'phone_registered_no_stream': dict(dispatchable={'action.phone': False}),
    'phone_unavailable': dict(
        dispatchable={'action.phone': False},
        reason_must_not_assert=('action.phone', ('本机没有', '没有连上', '被关掉', '没连着')),
    ),
    'channel_snapshot_broken': dict(
        source_scope={'channel.qq': 'runtime_snapshot_failed',
                      'channel.wechat': 'runtime_snapshot_failed'},
        reason_must_not_assert=('channel.qq', ('关掉', '关的', '关的', '未登录', '没有配置')),
    ),
    'ready': dict(dispatchable={'action.phone': True}),
}

# Contract checks that hold in EVERY scenario (t5 rev-3):
#   * every catalogue entry carries the key ``dispatchable`` (True/False/None) -
#     a missing key reads as None downstream and is indistinguishable from "unset";
#   * every channel entry carries ``state_source`` so "unknown vs disabled" survives;
#   * a ready + dispatchable=False entry must say WHY in ``reason``;
#   * the capability summary must stay within the 4000-char budget.
CONTRACT = dict(
    dispatchable_key_on_every_entry=True,
    dispatchable_boolean_or_none=True,
    dispatchable_false_has_reason=True,
    state_source_on_channels=True,
    summary_budget_chars=4000,
)

ASSERTION_WORDS = ('本机没有', '没有连上', '被关掉', '关的', '没连着', '没连上', '未登录')


def c2_states(scenarios):
    from life.companion_awareness import capabilities, capability_summary
    out = {}
    for name, row in scenarios.items():
        core = row['core']
        kinds = core.executor.available_kinds()
        catalogue = capabilities(core, dict(available_actions=kinds))
        by = {item['capability_ref']: item for item in catalogue}
        summary = capability_summary(catalogue)
        checks = []
        for ref, wanted in EXPECT[name].items():
            item = by.get(ref)
            got = item.get('status') if item else None
            checks.append(dict(ref=ref, wanted=wanted, got=got, ok=got == wanted,
                               reason=(item.get('reason') if item else 'MISSING') or '',
                               usable=item.get('usable') if item else None,
                               dispatchable=item.get('dispatchable') if item else None,
                               state_scope=item.get('state_scope') if item else None,
                               checked_at=item.get('checked_at') if item else None,
                               source_scope=item.get('source_scope') if item else None))
        extra_checks = []
        rules = EXTRA.get(name) or {}
        for ref, wanted in (rules.get('dispatchable') or {}).items():
            got = (by.get(ref) or {}).get('dispatchable', 'ABSENT')
            extra_checks.append(dict(check='dispatchable', ref=ref, wanted=wanted, got=got,
                                     ok=got == wanted))
        for ref, wanted in (rules.get('state_scope') or {}).items():
            got = (by.get(ref) or {}).get('state_scope')
            extra_checks.append(dict(check='state_scope', ref=ref, wanted=wanted, got=got,
                                     ok=got == wanted))
        for ref, wanted in (rules.get('source_scope') or {}).items():
            got = (by.get(ref) or {}).get('source_scope')
            extra_checks.append(dict(check='source_scope', ref=ref, wanted=wanted, got=got,
                                     ok=got == wanted))
        for key, rule in (('reason_must_not_assert', rules.get('reason_must_not_assert')),
                          ('ready_reason_must_not_assert',
                           rules.get('ready_reason_must_not_assert'))):
            if not rule:
                continue
            ref, words = rule
            reason = str((by.get(ref) or {}).get('reason') or '')
            hits = [word for word in words if word in reason]
            extra_checks.append(dict(check=key, ref=ref, wanted='no assertion words',
                                     got=(reason[:120] or '(no reason)'), ok=not hits,
                                     hits=hits))
        # ---- cross-scenario contract (every catalogue entry, not just the ones named above)
        missing_key = [item['capability_ref'] for item in catalogue if 'dispatchable' not in item]
        bad_value = [item['capability_ref'] for item in catalogue
                     if 'dispatchable' in item
                     and item['dispatchable'] not in (True, False, None)]
        silent_false = [item['capability_ref'] for item in catalogue
                        if item.get('dispatchable') is False
                        and item.get('status') == 'ready'
                        and not str(item.get('reason') or '').strip()]
        channels_without_source = [item['capability_ref'] for item in catalogue
                                   if str(item.get('capability_ref')).startswith('channel.')
                                   and not item.get('state_source')]
        section = json.dumps(summary, ensure_ascii=False, separators=(',', ':'))
        contract_checks = [
            dict(check='dispatchable_key_on_every_entry', wanted='[]', got=missing_key,
                 ok=not missing_key),
            dict(check='dispatchable_boolean_or_none', wanted='[]', got=bad_value,
                 ok=not bad_value),
            dict(check='dispatchable_false_has_reason', wanted='[]', got=silent_false,
                 ok=not silent_false),
            dict(check='state_source_on_channels', wanted='[]', got=channels_without_source,
                 ok=not channels_without_source),
            dict(check='summary_budget_chars', wanted='<=%d' % CONTRACT['summary_budget_chars'],
                 got=len(section), ok=len(section) <= CONTRACT['summary_budget_chars']),
        ]
        extra_checks.extend(contract_checks)
        out[name] = dict(
            kinds=list(kinds), checks=checks, extra_checks=extra_checks,
            all_ok=all(item['ok'] for item in checks) and
                   all(item['ok'] for item in extra_checks),
            unusable_missing_reason=[item['capability_ref'] for item in catalogue
                                     if item.get('status') in ('needs_setup', 'offline', 'unknown')
                                     and not item.get('reason')],
            summary=dict(usable=list(summary['usable']), unusable=list(summary['unusable']),
                         total=summary['total'], counts=summary['counts'],
                         ready=summary['ready'], not_ready=summary['not_ready']))
    return out


# ------------------------------------------------------------------ C3

def c3_projection(core, *, purpose='dialogue'):
    """Run the real ``_messages``; measure the capability section it produced."""
    import life.companion as companion_module
    messages = core._messages(None, 'tick', None, wake_kind='timer', purpose=purpose)
    instructions = messages[0]['content']
    state_text = messages[1]['content']
    prefix = '\u5f53\u524d\u751f\u6d3b\u6750\u6599\uff1a'
    material = json.loads(state_text[len(prefix):] if state_text.startswith(prefix) else state_text)
    caps = ((material.get('awareness') or {}).get('capabilities') or {})
    estimate = companion_module.estimate_tokens
    section = json.dumps(caps, ensure_ascii=False, separators=(',', ':'))
    total = sum(estimate(item['content']) for item in messages)
    offline_lines = [line for line in instructions.splitlines()
                     if line.startswith(('phone\uff1a', 'computer\uff1a', 'body\uff1a'))]
    return dict(
        purpose=purpose, request_tokens=total,
        capability_section_chars=len(section), capability_section_tokens=estimate(section),
        summary_keys=sorted(caps), usable=caps.get('usable'), unusable=caps.get('unusable'),
        how=caps.get('how'), note=caps.get('note'),
        three_layers=all(key in caps for key in ('usable', 'unusable', 'how')),
        budget_4000=len(section) <= 4000,
        budget_note='UNUSABLE_IN_PROMPT / 4000-char cap used by test_projection_stays_small',
        instructions_has_offline_block=('\u73b0\u5728\u7528\u4e0d\u4e86\u7684' in instructions),
        instructions_offline_lines=offline_lines,
        request_ceiling=companion_module.REQUEST_TOKEN_CEILING,
        request_within_ceiling=total <= companion_module.REQUEST_TOKEN_CEILING,
        materials_present=bool(material),
        tool_instruction_chars=len(instructions.split('\n\n')[-1]) if instructions else 0,
        has_guides_for_available=[kind for kind in ('web', 'command', 'skill', 'work',
                                                    'memory', 'growth')
                                  if kind in instructions],
    )


# ------------------------------------------------------------------ C4

def c4_ondemand(core):
    """Every reading op the prompt names must really return something usable."""
    result = {}
    plans = [('capabilities', {}), ('capabilities', dict(status='offline')),
             ('awareness', {}), ('used', dict(kind=['command', 'web'])),
             ('agent_tasks', {}), ('attention', {}),
             ('experience', {}), ('people', {}), ('conversations', {}),
             ('capabilities', dict(limit=3)),
             ('capabilities', dict(cursor='awareness:3'))]
    for op, fields in plans:
        key = op + ('[' + ','.join(sorted(fields)) + ']' if fields else '')
        try:
            reply = asyncio.run(core.executor.run(dict(
                kind='memory', op=op, project='t5',
                arguments=json.dumps(dict(op=op, **fields), ensure_ascii=False))))
        except Exception as error:  # noqa: BLE001 - a failure here is a finding
            reply = dict(status='exception',
                         output='%s: %s' % (type(error).__name__, error))
        items = [item for item in (reply.get('items') or []) if isinstance(item, dict)]
        gaps = [item for item in items if item.get('status') != 'ready']
        result[key] = dict(
            status=reply.get('status'), output=(reply.get('output') or '')[:400],
            returned=reply.get('returned'), available=reply.get('available'),
            next_cursor=reply.get('next_cursor'),
            reason=(reply.get('reason') or '')[:200],
            first_item_keys=sorted(items[0]) if items else [],
            gaps_with_reason=sum(1 for item in gaps if item.get('reason')),
            gaps_total=len(gaps),
            sample_ref=(items[0].get('capability_ref') if items else None))
    reply = asyncio.run(core.executor.run(dict(
        kind='memory', op='capabilities', project='t5',
        arguments=json.dumps(dict(op='capabilities', status='ready'), ensure_ascii=False))))
    items = [item for item in (reply.get('items') or []) if isinstance(item, dict)]
    full = asyncio.run(core.executor.run(dict(
        kind='memory', op='capabilities', project='t5',
        arguments=json.dumps(dict(op='capabilities', limit=20), ensure_ascii=False))))
    everything = [item for item in (full.get('items') or []) if isinstance(item, dict)]
    guard = next((item for item in everything
                  if item.get('capability_ref') == 'guard.watchdog'), None)
    result['ready_entries_detail'] = dict(
        total=len(items),
        with_what=sum(1 for item in items if item.get('what')),
        with_how_ref=sum(1 for item in items if item.get('how_ref')),
        agent_item=next((dict(what=item.get('what'), how_ref=item.get('how_ref'),
                              ops=item.get('ops')) for item in items
                         if item.get('capability_ref') == 'agent.subagent'), None),
        all_refs=[item.get('capability_ref') for item in everything],
        guard_item=(dict(status=guard.get('status'), reason=guard.get('reason'),
                         what_it_does_not_do=guard.get('what_it_does_not_do'),
                         evidence=guard.get('evidence'), state_scope=guard.get('state_scope'))
                    if guard else None))
    return result


# ------------------------------------------------------------------ C5

def c5_experience(core):
    """Reproduce t4 in isolation: real dispatch + real ``_settle`` + real sqlite."""
    from life.text import stable_id
    project_key = 't5-experience'
    core.tasks.ensure(core.scope, project_key, None)
    before = table_counts(core.repo)
    source, _ = core.life.ingest(core.scope, 'message', 'user_report', 'check disk',
                                 payload=dict(input_source='qq:private:100000'))
    plan = [('command', dict(status='completed', output='Filesystem /dev/vda3 39G 18%')),
            ('web', dict(status='completed', output='3 results')),
            ('command', dict(status='failed', output='sh: df: command not found'))]
    receipts = []
    for index, (kind, result) in enumerate(plan):
        row_id = stable_id('companion-action', 't5-exp-%d-%s' % (index, time.time()))
        if kind == 'command':
            payload = dict(kind=kind, project=project_key, command='df -h /')
        else:
            payload = dict(kind=kind, project=project_key, op='search',
                           arguments=json.dumps(dict(query='wharf'), ensure_ascii=False))
        with core.repo.tx():
            core.repo._exec(
                'INSERT INTO companion_actions (id,agent,source,project_ref,action,status,'
                'created,updated) VALUES (?,?,?,?,?,?,?,?)',
                (row_id, core.life.agent_id, source.id, project_key,
                 json.dumps(payload, ensure_ascii=False), 'running', time.time(), time.time()))
        row = core.repo._one('SELECT * FROM companion_actions WHERE id=?', (row_id,))
        core._settle(row, result)
        saved = core.repo._one('SELECT id,status,result FROM companion_actions WHERE id=?', (row_id,))
        receipts.append(dict(action_id=row_id, kind=kind, status=saved['status'],
                             output=(saved['result'] or '')[:120]))
    after = table_counts(core.repo)
    used = asyncio.run(core.executor.run(dict(
        kind='memory', op='used', project=project_key,
        arguments=json.dumps(dict(op='used', kind=['command', 'web', 'dsh']),
                             ensure_ascii=False))))
    by_kind = {item['kind']: item for item in used.get('items') or []}
    read_back = asyncio.run(core.executor.run(dict(
        kind='memory', op='read', project=project_key,
        arguments=json.dumps(dict(op='read', id=receipts[0]['action_id']), ensure_ascii=False))))
    empty = asyncio.run(core.executor.run(dict(
        kind='memory', op='used', project=project_key,
        arguments=json.dumps(dict(op='used', kind=['town']), ensure_ascii=False))))
    town = {item['kind']: item for item in empty.get('items') or []}.get('town') or {}
    saved_project = core.tasks.get(project_key, core.scope.scope_id) or {}
    ledger = {row['action_id'] for row in receipts}
    matched = []
    for item in used.get('items') or []:
        last = item.get('last') or {}
        matched.append(dict(kind=item['kind'], basis=item.get('basis'),
                            counts=item.get('counts'),
                            standing=(item.get('standing') or {}).get('state'),
                            last_action=last.get('action_id'), last_outcome=last.get('outcome'),
                            in_ledger=last.get('action_id') in ledger,
                            receipt_ref_present=bool(last.get('receipt_ref'))))
    return dict(
        receipts=receipts,
        tables_created=[name for name in after if name not in before],
        used_status=used.get('status'), used_output=(used.get('output') or '')[:600],
        by_kind={kind: dict(counts=row.get('counts'),
                            standing=(row.get('standing') or {}).get('state'),
                            basis=row.get('basis'))
                 for kind, row in by_kind.items()},
        reported=used.get('reported'), basis_kinds=used.get('basis_kinds'),
        read_back_status=read_back.get('status'),
        read_back_output_has_source='Filesystem' in json.dumps(read_back, ensure_ascii=False),
        never_used=dict(state=(town.get('standing') or {}).get('state'),
                        counts=town.get('counts'),
                        text=(town.get('standing') or {}).get('note')),
        last_receipt_present=bool(saved_project.get('last_receipt')),
        last_receipt_action=(saved_project.get('last_receipt') or {}).get('action_id'),
        matched=matched,
        checks=dict(
            counts_expected_2_1_0=[(by_kind.get(kind) or {}).get('counts')
                                   for kind in ('command', 'web', 'dsh')],
            command_standing=(by_kind.get('command') or {}).get('standing', {}).get('state'),
            command_counts=(by_kind.get('command') or {}).get('counts'),
            web_counts=(by_kind.get('web') or {}).get('counts'),
            read_back_same_source=read_back.get('status') == 'completed',
            town_never_used=(town.get('standing') or {}).get('state') == 'never_used',
            all_last_ids_from_ledger=all(row['in_ledger'] for row in matched
                                         if row['standing'] != 'never_used'),
            basis_split=all(row['basis'] in ('first_hand', 'reported') for row in matched),
            last_receipt_written=bool(saved_project.get('last_receipt')),
            no_new_tables=not [name for name in after if name not in before],
        ))


# ------------------------------------------------------------------ main

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--db-copy', default='/var/lib/companion/xiaoyu/data/life.db')
    parser.add_argument('--label', default='isolated-t5')
    parser.add_argument('--prod-evidence', action='store_true',
                        help='read the two ready-deciders (DSH endpoint, completed agent '
                             'task) from the production copy instead of the empty sandbox')
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    (out / 'tmp').mkdir(parents=True, exist_ok=True)
    report = dict(started=time.strftime('%Y-%m-%dT%H:%M:%S%z'), label=args.label,
                  out=str(out), python=sys.version, cwd=str(ROOT))

    sandbox_home = out / 'tmp/home'
    for name in ('HOME', 'USERPROFILE'):
        os.environ[name] = str(sandbox_home)
    for name in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
        os.environ[name] = str(sandbox_home / ('.' + name.lower()))
    for name in ('TMPDIR', 'TEMP', 'TMP'):
        os.environ[name] = str(out / 'tmp')
    tempfile.tempdir = str(out / 'tmp')
    for name in ('DEEPSEEK_API_KEY', 'COMPANION_API_KEY', 'QQ_WS_URL', 'QQ_WORKER_TOKEN',
                 'QQ_WORKER_HEALTH', 'ADB_PATH', 'ANDROID_SERIAL'):
        os.environ.pop(name, None)
    os.environ['QQ_OFFLINE'] = '1'
    report['env'] = dict(HOME=os.environ['HOME'], TMPDIR=os.environ['TMPDIR'],
                         QQ_OFFLINE='1',
                         home_is_sandbox=str(Path(os.path.expanduser('~'))) == str(sandbox_home))

    source = Path(args.db_copy)
    copy_path = out / 'tmp/life.db'
    if source.exists() and source.is_file():
        report['db_copy'] = snapshot_sqlite(source, copy_path)
        os.chmod(copy_path, 0o444)
        try:
            probe = sqlite3.connect(str(copy_path))
            try:
                probe.execute('PRAGMA user_version')
                report['db_copy']['copy_openable'] = True
            finally:
                probe.close()
        except Exception as error:  # noqa: BLE001
            report['db_copy']['copy_openable'] = '%s: %s' % (type(error).__name__, error)
        copy_path.chmod(0o644)
        prod_home = expose_home(copy_path)
        prod_cfg = cfg_for(prod_home)
    else:
        sqlite3.connect(str(copy_path)).close()
        report['db_copy'] = dict(source=str(source), exists=False,
                                 note='no production DB found; fresh DB used')
        prod_home, prod_cfg = None, None
    report['prod_evidence'] = bool(args.prod_evidence and prod_home is not None)

    with ExternalGuard():
        from life.companion import Companion  # noqa: F401 - import under the guard
        report['import_ok'] = True
        factory = make_builder()

        scenarios = build_c2(factory, out / 'tmp')
        report['c2'] = c2_states(scenarios)

        # C1/C3/C4 run on the sealed copy of the production DB when available: the
        # deployed ledger is what decides agent.subagent and the home layout is the
        # production one. Both are read-only; nothing is written back.
        if prod_cfg is not None:
            main_core = factory(prod_cfg, prod_home, model=FakeModel(),
                                executor=FakeRemote(['web', 'command', 'skill', 'work',
                                                     'memory', 'growth'], peer=('ws', object()),
                                                    phone=dict(devices=[dict(
                                                        device_id='d1', name='pair-1',
                                                        has_stream=True,
                                                        last_seen=time.time())]),
                                                    im=FakeIm(
                                                        qq=dict(state='connected', enabled=True,
                                                                account='763282103', error=''),
                                                        wechat=dict(state='unconfigured',
                                                                    enabled=False)),
                                                    dsh=True))
            report['c1_basis'] = 'production DB copy + prod-shaped home'
        else:
            main_core = scenarios['ready']['core']
            report['c1_basis'] = 'fresh sandbox home (no production copy)'

        saved = None
        if prod_cfg is not None and args.prod_evidence:
            saved, evidence = attach_prod_evidence(main_core, copy_path)
            report['prod_evidence_detail'] = evidence
        try:
            manifest, summary, catalogue = c1_manifest(main_core)
        finally:
            if saved is not None:
                restore_prod_evidence(main_core, saved)
        report['c1'] = manifest
        report['c1_catalogue_refs'] = sorted(item['capability_ref'] for item in catalogue)

        report['c3'] = [c3_projection(main_core, purpose=purpose)
                        for purpose in ('dialogue', 'work')]
        report['c4'] = c4_ondemand(main_core)

        exp_home = make_home(out / 'tmp/home-exp')
        exp_core = factory(cfg_for(exp_home), exp_home, model=FakeModel(),
                           executor=FakeRemote(['web', 'command', 'skill', 'work',
                                                'memory', 'growth'], peer=None,
                                               phone=dict(devices=[]), im=FakeIm(),
                                               dsh=False))
        report['c5'] = c5_experience(exp_core)
        first = report['c5']['receipts'][0]['action_id']
        read_back = asyncio.run(exp_core.executor.run(dict(
            kind='memory', op='read', project='t5-experience',
            arguments=json.dumps(dict(op='read', id=first), ensure_ascii=False))))
        report['c5']['read_back'] = dict(
            status=read_back.get('status'),
            excerpt=json.dumps(read_back, ensure_ascii=False)[:900])
        report['c6'] = c3_projection(exp_core, purpose='dialogue')
        factory.close_all()

    report['guard_events'] = list(GUARD_EVENTS)
    report['external_calls_blocked'] = len(GUARD_EVENTS)
    report['db_source_after'] = dict(
        unchanged=report['db_copy'].get('unchanged'),
        size=source.stat().st_size if source.exists() else None,
        mtime_ns=source.stat().st_mtime_ns if source.exists() else None)
    report['finished'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    (out / 'verification.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({
        'out': str(out),
        'import_ok': report.get('import_ok'),
        'external_calls_blocked': report['external_calls_blocked'],
        'db_unchanged': report['db_copy'].get('unchanged'),
        'c1_code_without_doc': report['c1']['code_without_doc'],
        'c1_doc_without_code': report['c1']['doc_without_code'],
        'c2_all_ok': {name: row['all_ok'] for name, row in report['c2'].items()},
        'c2_missing_reason': {name: row['unusable_missing_reason']
                              for name, row in report['c2'].items()},
        'c3': [{key: row[key] for key in ('purpose', 'capability_section_chars',
                                          'three_layers', 'budget_4000',
                                          'request_tokens', 'request_within_ceiling',
                                          'instructions_has_offline_block')}
               for row in report['c3']],
        'c4_status': {key: row.get('status') for key, row in report['c4'].items()
                      if isinstance(row, dict)},
        'c5_checks': report['c5']['checks'],
    }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - a traceback says exactly which step blew up
        traceback.print_exc()
        raise SystemExit(2)
