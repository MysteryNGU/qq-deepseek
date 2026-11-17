"""在 /tmp 副本里跑真实 Companion._messages()：只读装配，不写库、不联网、不去 QQ。

证明三件事：
  A1  state['awareness'] 真的进了「当前生活材料」那段系统消息；
  A7  QQ 通道没开时 self.account.state 是 disabled，不是 connected；
  A6  某一段抛错只进 projection_errors，整轮装配仍然完成。
"""
import asyncio
import json
import os
import sys
import tempfile

tree = sys.argv[1]
sys.path.insert(0, tree)
home = tempfile.mkdtemp(prefix='msg-home-')
os.environ['HOME'] = home
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

from life import companion as C  # noqa: E402
from life import companion_awareness as aw  # noqa: E402

REPORT = {}


def fake_core():
    core = object.__new__(C.Companion)
    core.home = __import__('pathlib').Path(home)
    core.cfg = type('Cfg', (), dict(owner=10001, bot_qq=0, model='deepseek-flash', dsh_root='',
                                    dsh_node='', dsh_home='', dsh_provider='deepseek-official',
                                    companion_autonomy=True, quiet_hours='',
                                    spending_policy=None))()
    core.life = type('Life', (), dict(agent_id='acct-msg', mood=lambda *a, **k: None,
                                      affect_summary=lambda *a, **k: {},
                                      life_recall=type('Recall', (), dict(
                                          context=lambda *a, **k: '')),
                                      advance_time=lambda *a, **k: None))()
    core.scope = type('Scope', (), dict(scope_id='acct-msg|qq:private:10001',
                                        conversation_id='qq:private:10001', participant_id='10001'))()
    core.process_started_at = 1700000000.0
    core.started_at = None
    core.active = True
    core._body_context = dict(connected=False)
    core._body = None
    core._body_pending = False
    core._body_revision = 0
    core._model_requests = {}
    core._batch = None
    core._last_material = {}
    core._last_material_extra = {}
    core._recall_prefetch = []
    core._drawer = {}
    core.spending = type('Spending', (), dict(policy=type('P', (), dict(enabled=False))()))()
    class Ex:
        _dsh_paths = lambda self: None
        available_kinds = lambda self: ['command', 'skill', 'memory']
        phone_context = lambda self: dict(connected=False, devices=[])
        known_mobile_destinations = lambda self: set()
    core.executor = Ex()
    core.repo = type('Repo', (), dict(
        tx=lambda *a: type('Ctx', (), dict(__enter__=lambda s: s, __exit__=lambda *a: False))(),
        _all=lambda *a, **k: [],
        _one=lambda *a, **k: None,
        _exec=lambda *a, **k: None))()
    core._state = lambda key, default=None: core._drawer.get(key, default)
    core._save = lambda key, value: core._drawer.__setitem__(key, value)
    core._persona = lambda: '（人格占位）'
    core._recent = lambda limit=8, include_images=False: []
    core._notes = lambda: []
    core._projects = lambda: []
    core._today_material = lambda: []
    core._ambient_material = lambda: []
    core._growth_material = lambda clues=(): []
    core._recollection_material = lambda: []
    core._available_destinations = lambda: []
    core._clip_text = C.Companion._clip_text.__get__(core)
    core._message_route = C.Companion._message_route.__get__(core)
    core._group_input = C.Companion._group_input.__get__(core)
    return core


def assembly(core, *, current=None, wake_kind='user'):
    from life.companion_letter import Letter
    from life.companion_motivation import CompanionMotivation
    return C.Companion._messages(core, current, '看看' if current is None else current.text,
                                 route=dict(input_source=dict(kind='internal'),
                                            reply_delivery=dict(targets=['all'])),
                                 source_ref='src-1', wake_kind=wake_kind, attention=[],
                                 purpose='activity' if current is None else 'dialogue')


class Stub:
    def context(self, **kwargs):
        return {}


def run():
    core = fake_core()
    core.letter = Stub()
    core.motivation = type('M', (), dict(context=lambda *a, **k: dict(
        drives={}, recent_choices=[], attention=[], intimacy=None)))()
    core.continuity = type('Ct', (), dict(context=lambda *a, **k: dict(
        review_projects=[], own_projects=[], interests=[])))()
    core.people = type('People', (), dict(
        project=lambda *a, **k: ({str(core.cfg.owner): []}, {})))()
    core.growth = type('Growth', (), dict(for_actor=lambda *a, **k: []))()

    class Town:
        config = type('TC', (), dict(enabled=False, code=''))()
        thinking = 0

        def context(self):
            return []

        def snapshot(self):
            return dict(enabled=False, has_code=False, state='disabled', error='', scope=None,
                        turns=0, turn_kinds={}, events_after=None)

        def pending_project_refs(self):
            return []
    core.town = Town()
    core.bilibili_social = Stub()
    core.recollection = type('Rec', (), dict(pick=lambda *a, **k: None))()
    core._recollection_cues = lambda: []
    core.executor.attach_kinds = None

    messages = assembly(core)
    state_text = messages[1]['content']
    payload = json.loads(state_text.split('当前生活材料：', 1)[1])
    REPORT['A1_messages'] = dict(
        total_messages=len(messages),
        awareness_in_request='awareness' in payload,
        observed_at=payload.get('awareness', {}).get('observed_at'),
        account_state=payload.get('awareness', {}).get('self', {}).get('account', {}).get('state'),
        account_value=payload.get('awareness', {}).get('self', {}).get('account', {}).get('value'),
        account_source=payload.get('awareness', {}).get('self', {}).get('account', {}).get('source_ref'),
        here=payload.get('awareness', {}).get('here', {}),
        cap_not_ready=payload.get('awareness', {}).get('capabilities', {}).get('not_ready'),
        awareness_bytes=len(json.dumps(payload.get('awareness'), ensure_ascii=False)),
        request_has_private_keys=any(k.startswith('_') for k in payload.get('awareness', {})),
        sticky_set=isinstance(getattr(core, '_last_awareness', None), dict),
    )

    # A6：让 projection 里的一小段真的抛错，整轮装配仍完成
    def angry(core_, current, now):
        raise RuntimeError('segmented boom')
    aw._recent_experience = angry
    messages2 = assembly(core)
    payload2 = json.loads(messages2[1]['content'].split('当前生活材料：', 1)[1])
    REPORT['A6_isolation'] = dict(
        still_assembled=bool(payload2.get('awareness')),
        errors=payload2['awareness'].get('projection_errors'),
        recent_experience=payload2['awareness'].get('recent_experience'),
        account_state=payload2['awareness']['self']['account']['state'])

    # 按需读取：直接走运行时投影，确认信封与 not_implemented
    core._recent = lambda limit=8, include_images=False: []
    reads = {}
    for op in ('awareness', 'capabilities', 'attention', 'agent_tasks', 'people'):
        result = asyncio.run(aw.run(core, dict(kind='memory', op=op,
                                               arguments=json.dumps(dict(op=op)))))
        reads[op] = dict(status=result.get('status'), returned=result.get('returned'),
                         has_envelope=all(k in result for k in ('items', 'next_cursor', 'source_refs',
                                                                'freshness', 'truncation')),
                         reason=(result.get('reason') or '')[:24],
                         observed_at=(result.get('freshness') or {}).get('observed_at'))
    REPORT['A4_reads'] = reads
    print(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str))


run()
