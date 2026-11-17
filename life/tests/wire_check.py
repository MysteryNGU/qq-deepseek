"""在 /tmp 源码副本里验证接线：不碰生产树、不碰生产库、不联网、不发 QQ。

用法（云端）：python /tmp/stage-a/wire_check.py <tree>
在 <tree> 里 import life.companion，用假 core 调 _messages 的接线点，
并按远端执行器的包装顺序做一次 end-to-end。
"""
import ast
import json
import os
import sys
import tempfile
import types

tree = sys.argv[1]
sys.path.insert(0, tree)
os.environ.setdefault('HOME', tempfile.mkdtemp(prefix='wire-home-'))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

report = {}


def check(name, fn):
    try:
        report[name] = fn()
    except Exception as error:  # noqa: BLE001
        import traceback
        report[name] = 'ERROR: %s: %s' % (type(error).__name__, error)
        traceback.print_exc()


def t_import():
    from life import companion  # noqa: F401
    from life import companion_awareness as aw
    from life import companion_executor  # noqa: F401
    from life import companion_remote  # noqa: F401
    from life import companion_context
    guide = companion_context.GUIDES['memory']
    return dict(guide_has_ops='op=awareness' in guide,
                guide_has_attention_set='attention_set' in guide,
                public_view_exported=hasattr(aw, 'public_view'),
                read_ops=list(aw.READ_OPS))


def fake_core():
    core = types.SimpleNamespace()
    core.home = __import__('pathlib').Path(tempfile.mkdtemp(prefix='wire-core-'))
    core.cfg = types.SimpleNamespace(owner=10001, bot_qq=0, model='deepseek-flash',
                                     dsh_root='', dsh_node='', dsh_home='', dsh_provider='deepseek-official')
    core.life = types.SimpleNamespace(agent_id='acct-wire')
    core.scope = types.SimpleNamespace(scope_id='acct-wire|qq:private:10001',
                                       conversation_id='qq:private:10001', participant_id='10001')
    core.process_started_at = 1700000000.0
    core.started_at = None
    core.active = True
    core.spending = types.SimpleNamespace(policy=types.SimpleNamespace(enabled=False))
    core.executor = types.SimpleNamespace(_dsh_paths=lambda: None, available_kinds=lambda: ['command'])
    core._drawer = {}
    core.repo = types.SimpleNamespace(tx=lambda: types.SimpleNamespace(
        __enter__=lambda s: s, __exit__=lambda *a: False))
    core.repo._all = lambda sql, args=(): []
    core.repo._one = lambda sql, args=(): None
    core.repo._exec = lambda sql, args=(): None

    def _state(key, default=None):
        return core._drawer.get(key, default)

    def _save(key, value):
        core._drawer[key] = value

    core._recent = lambda **kw: [dict(id='evt-1', role='user', content='一句',
                                     created=1700000000.0)]
    core._state = _state
    core._save = _save
    return core


def t_projection():
    from life.companion_awareness import build, public_view
    core = fake_core()
    projection = build(core, state=dict(available_actions=['command', 'skill', 'memory']),
                       wake_kind='user')
    shown = public_view(projection)
    body = json.dumps(shown, ensure_ascii=False)
    account = shown['self']['account']
    return dict(bytes=len(body),
                sections=sorted(shown),
                account_state=account['state'],
                account_value=account['value'],
                account_source=account['source_ref'],
                cap_not_ready=shown['capabilities'].get('not_ready'),
                cap_total=shown['capabilities'].get('total'),
                observed_at=shown['observed_at'],
                has_private_keys=any(k.startswith('_') for k in shown))


def t_remote_chain():
    """服务模式的包装顺序：RemoteExecutor(server, ActionExecutor) → memory 读 op 能找到 core。"""
    from life import companion_awareness as aw
    from life.companion_executor import ActionExecutor
    from life.companion_remote import RemoteExecutor
    core = fake_core()
    home = core.home
    original = ActionExecutor(core.cfg, home)
    original.attach_core(core)
    server = types.SimpleNamespace(im=None, im_factory=None, peer=None,
                                   mobile=types.SimpleNamespace(phone=types.SimpleNamespace(
                                       context=lambda: dict(connected=False)), devices={},
                                       subscribers={}, device_source=lambda d: {}))
    remote = RemoteExecutor(server, original)
    import asyncio
    result = asyncio.run(remote.run(dict(kind='memory', op='capabilities',
                                         arguments=json.dumps(dict(op='capabilities', limit=3)))))
    return dict(status=result.get('status'), returned=result.get('returned'),
                has_envelope=all(k in result for k in ('items', 'next_cursor', 'source_refs',
                                                       'freshness', 'truncation')),
                first=((result.get('items') or [{}])[0].get('capability_ref')))


def t_search_still_lookup():
    """op=search 仍然走原来的回查实现（不能把旧行为吃掉）。"""
    from life.companion_executor import ActionExecutor
    import asyncio
    core = fake_core()
    original = ActionExecutor(core.cfg, core.home)
    original.attach_repo(types.SimpleNamespace(_all=lambda *a, **k: [], _one=lambda *a, **k: None),
                         'acct-wire')
    result = asyncio.run(original.run(dict(kind='memory', op='search',
                                           arguments=json.dumps(dict(query='码头')))))
    return dict(status=result.get('status'), output=(result.get('output') or '')[:40])


check('import', t_import)
check('projection', t_projection)
check('remote_chain', t_remote_chain)
check('search_lookup', t_search_still_lookup)
print(json.dumps(report, ensure_ascii=False, indent=2))
