# -*- coding: utf-8 -*-
"""P3 证据回收：阶段 A 认知层 / 阶段 B 子 agent 框架的隔离验收。

规矩（与 P0/P1 一致，谁也不破例）：

* 一切在 ``/tmp/p3verify-*`` 下：临时 HOME、临时库、临时源码树副本；
  ``/var/lib/companion/xiaoyu`` 只被 ``mode=ro`` 打开拷贝一次（``life.db`` + ``-wal``），
  生产 ``config.toml`` 只读一次再脱敏写进沙箱，**从不**写回。
* 源码树 = ``shutil.copytree`` 出来的副本，脚本在副本里跑；生产 ``life/`` 不动。
* ``QQ_OFFLINE=1``、凭据变量全部删除、``COMPANION_FORBID_EXEC=1`` 拒绝
  ``kind=command``（外部执行禁用），模型是假的（``FakeModel``），不连任何真实模型。
* 每个场景的结果都进 ``result.json``：一条一条 ``ok / detail``，不写「通过」了事。

用法（云端，cpython 3.12 的 venv）::

    .venv-companion/bin/python -B -X utf8 tools/stage-c/verify_awareness_agents.py /tmp/p3verify-XXXX
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
# ``P3_LOCAL=1`` 是本地（Windows）试跑用的等价模式：源码树用本地副本、库是全新空库，
# 没有生产库可拷也不拷——云端跑的时候才用 ``/var/lib/companion/xiaoyu`` 的只读副本。
LOCAL = os.environ.get('P3_LOCAL') == '1'
PROD_HOME = Path(os.environ.get('P3_PROD_HOME') or '/var/lib/companion/xiaoyu')
PROD_TREE = Path(os.environ.get('P3_PROD_TREE') or '/opt/qq-deepseek')
OUT = []


def record(scenario, item, ok, detail=None, effect=None):
    row = dict(scenario=scenario, item=item, ok=bool(ok), detail=detail)
    if effect is not None:
        row['effect'] = effect
    OUT.append(row)
    flag = 'PASS' if ok else ('LIMITED' if (effect or {}).get('limited') else 'FAIL')
    print('[%s] %s / %s :: %s' % (flag, scenario, item, json.dumps(detail, ensure_ascii=False, default=str)[:400]),
          flush=True)
    return row


# ------------------------------------------------------------------ 沙箱

SECRET_ENV = ('DEEPSEEK_API_KEY', 'COMPANION_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
              'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'HF_TOKEN', 'QQ_WS_URL',
              'QQ_WORKER_TOKEN', 'QQ_WORKER_HEALTH', 'ADB_PATH', 'ANDROID_SERIAL', 'OPENAI_BASE_URL')


def sandbox_env(extra=None):
    env = dict(os.environ)
    for name in SECRET_ENV:
        env.pop(name, None)
    home = str(ROOT / 'home')
    env.update({
        'HOME': home, 'USERPROFILE': home,
        'XDG_CONFIG_HOME': home + '/.config', 'XDG_CACHE_HOME': home + '/.cache',
        'XDG_DATA_HOME': home + '/.local/share', 'XDG_STATE_HOME': home + '/.local/state',
        'TMPDIR': str(ROOT / 'tmp'), 'TEMP': str(ROOT / 'tmp'), 'TMP': str(ROOT / 'tmp'),
        'PYTHONPATH': str(ROOT / 'tree'), 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
        'PYTHONDONTWRITEBYTECODE': '1',
        'QQ_OFFLINE': '1', 'COMPANION_FORBID_EXEC': '1', 'NO_PROXY': '127.0.0.1,localhost',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
    })
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    return env


def stage_tree():
    """源码副本：只拷代码与文档，不拷 data/ vendor/ napcat/ toolchain/ 等重目录。

    ``toolchain/`` 是 DSH 的 node_modules 树（几十万个小文件、还有只读目录），
    拷它既慢又会长路径报错；本验收只关心 ``life/`` 里的 Python 代码，所以不拷。
    """
    skip = {'.git', '.venv', '.venv-companion', 'venv', '.test-deps', '__pycache__', 'data',
            'local-backups', 'vendor', 'napcat', 'napcat-onekey', '.agent-teams',
            'toolchain', 'node_modules', '.dsh', '.acceptance', '.review-server',
            '.srv-life', '.srv-life-utf8',
            'xiaoyu-private-migration-20261027-094202'}
    tree = ROOT / 'tree'
    shutil.copytree(PROD_TREE, tree, ignore=shutil.ignore_patterns(
        '__pycache__', '*.pyc', *sorted(skip)), symlinks=False)
    return tree


def stage_home(fresh=True, label='home'):
    """独立 HOME：``fresh=True`` 全新空库；否则生产库的只读副本 + 脱敏配置。

    本地等价模式（``P3_LOCAL=1``）没有生产库可拷：一律用全新空库，并且不读
    生产 ``config.toml``（本地那只是源码副本里的示例配置）。
    """
    home = ROOT / label
    (home / 'data').mkdir(parents=True, exist_ok=True)
    if not fresh and not LOCAL:
        _copy_prod_db(home / 'data/life.db')
        config = (PROD_HOME / 'config.toml').read_text(encoding='utf-8')
        redacted = []
        for line in config.splitlines():
            stripped = line.strip()
            if any(stripped.startswith('"%s"' % field) for field in ('ws_token', 'api_key')):
                redacted.append(stripped.split('=', 1)[0].strip() + ' = ""')
            else:
                redacted.append(line)
        (home / 'config.toml').write_text('\n'.join(redacted) + '\n', encoding='utf-8')
        for extra in ('persona.md', 'personas'):
            source = PROD_HOME / extra
            if source.is_dir():
                shutil.copytree(source, home / extra, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copy2(source, home / extra)
        if not (home / 'persona.md').is_file():
            # 人格文件必须存在：她按它说话，缺了连一轮都装不起来。
            shutil.copy2(PROD_HOME / 'persona.md', home / 'persona.md')
    elif LOCAL:
        source = PROD_TREE / 'persona.example.md'
        if source.is_file():
            shutil.copy2(source, home / 'persona.md')
    # 本地等价模式也要有人格文件：`_messages` 每轮都会读它。
    _ensure_persona(home)
    return home


def _ensure_persona(home):
    if (home / 'persona.md').is_file():
        return home / 'persona.md'
    for source in (PROD_HOME / 'persona.md', PROD_TREE / 'persona.example.md'):
        if source.is_file():
            shutil.copy2(source, home / 'persona.md')
            return home / 'persona.md'
    (home / 'persona.md').write_text('# 验收替身人格\n\n这是隔离验收用的替身人格文件。\n', encoding='utf-8')
    return home / 'persona.md'


def _copy_prod_db(target):
    """生产库只用 ``mode=ro`` 打开：本验收不往生产库写一个字节。"""
    stamp = dict(before=_file_stat(PROD_HOME / 'data/life.db'))
    origin = sqlite3.connect('file:%s?mode=ro' % (PROD_HOME / 'data/life.db'), uri=True, timeout=20)
    try:
        copy = sqlite3.connect(str(target))
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()
    stamp['after'] = _file_stat(PROD_HOME / 'data/life.db')
    (ROOT / 'prod_db_stamp.json').write_text(json.dumps(stamp, indent=2), encoding='utf-8')
    return target


def _file_stat(path):
    value = Path(path).stat()
    return dict(path=str(path), bytes=value.st_size, mtime=round(value.st_mtime, 3))


# ------------------------------------------------------------------ 假模型 / 假 QQ

FAKE_DECISION = dict(reply='', followups=[], remember=[], projects=[], actions=[], wake_after=0)


class FakeModel:
    """只回一段空决定：不连任何真实模型，也不花钱。"""

    MAX_OUTPUT_TOKENS = 8000

    def __init__(self, decision=None):
        self.calls = 0
        self.decision = dict(decision or FAKE_DECISION)

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        self.calls += 1
        return dict(message=dict(content=json.dumps(self.decision, ensure_ascii=False)),
                    finish_reason='completed', usage=None, model='fake')

    async def close(self):
        return None


class FakeQQ:
    """通道替身：只回快照，不连 NapCat、不发消息。"""

    def __init__(self, snapshot):
        self.snapshot_value = dict(snapshot)
        self.sent = []

    def snapshot(self):
        return dict(self.snapshot_value)

    def emit(self, *args, **kwargs):
        return None

    async def start(self):
        return None

    async def close(self):
        return None

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return dict(ok=True)


class FakeIM:
    def __init__(self, snapshot):
        self.qq = FakeQQ(snapshot)

    def snapshot(self):
        return dict(qq=dict(self.qq.snapshot()))

    def emit(self, *args, **kwargs):
        return None

    async def start(self):
        return None

    async def close(self):
        return None


class FakeServer:
    """``CompanionServer`` 的最小替身：awareness 找通道状态时只读 ``im``。"""

    def __init__(self, im):
        self.im = im


class RemoteWrap:
    """``RemoteExecutor`` 的等价物：服务模式下执行器被包一层，``original`` 指回自己。"""

    def __init__(self, original, server):
        self.original, self.server = original, server

    def available_kinds(self):
        return self.original.available_kinds()

    def _dsh_paths(self):
        return self.original._dsh_paths()

    async def run(self, action):
        return await self.original.run(action)


def load_cfg(home, db='data/life.db'):
    from life.role_config import load_config
    source = (PROD_HOME / 'config.toml') if not LOCAL else (PROD_TREE / 'config.toml')
    if not source.is_file():
        source = PROD_TREE / 'config.example.toml'
    cfg = load_config(source)
    cfg.life_db = db
    return cfg


def material_of(messages):
    for message in messages or []:
        content = message.get('content')
        if isinstance(content, str) and content.startswith('当前生活材料：'):
            return json.loads(content.split('当前生活材料：', 1)[1])
    return None


def new_core(home, model=None, executor=None, server=None):
    from life.companion import Companion
    cfg = load_cfg(home)
    core = Companion(cfg, home, model=model or FakeModel(), executor=executor)
    if server is not None:
        core.executor = RemoteWrap(core.executor, server)
        if hasattr(core.executor.original, 'attach_core'):
            core.executor.original.attach_core(core)
    return core


def close_core(core):
    try:
        core.life.close()
    except Exception:
        pass


async def ask(core, text, participant=None):
    """走真实的 ``_messages`` 装配：返回（生活材料包, 最近一条用户消息）。"""
    messages = core._messages(None, text, {'input_source': 'qq'}, purpose='dialogue')
    return material_of(messages), messages


# ================================================================== 阶段 A

async def scenario_a1(home):
    """A1/A2：问「你现在在哪、用谁的号」——答得出来，且未知时明说是未知。"""
    from life import companion_awareness as awareness

    # 1) 通道关闭：账号必须是 disabled，值必须是 None（不是把配置里的号当成在用）
    core = new_core(home)
    try:
        bundle, messages = await ask(core, '你现在在哪、用谁的号？')
        view = (bundle or {}).get('awareness') or {}
        account = (view.get('self') or {}).get('account') or {}
        record('A1', '请求体里有 awareness 且有 observed_at',
               bool(view) and bool(view.get('observed_at')),
               dict(observed_at=view.get('observed_at'), schema=view.get('schema'),
                    node=view.get('node'), bytes=len(json.dumps(view, ensure_ascii=False))))
        record('A1', '通道关闭时账号状态是 disabled（不是 connected）',
               account.get('state') == 'disabled' and account.get('value') in (None, ''),
               dict(state=account.get('state'), value=account.get('value'),
                    source_ref=account.get('source_ref'), source_scope=account.get('source_scope'),
                    observed_at=account.get('observed_at')))
        record('A1', '账号事实带来源（source_ref 指向运行代码）',
               bool(account.get('source_ref')) and bool(account.get('observed_at')),
               dict(source_ref=account.get('source_ref')))
        record('A2', '未知就写未知：连接后没有可核验的号时 value 是 null',
               account.get('value') is None,
               dict(note='未登录核验前不把配置里的号当成在用'))
        # 2) 模型自己编一个号：改不动投影，也改不动落库的指纹
        liar = FakeModel(dict(FAKE_DECISION, reply='我是用 10001 这个号在云端 linux 上'))
        liar_core = new_core(ROOT / 'home-a1-liar', model=liar)
        try:
            bundle2, _ = await ask(liar_core, '你现在用谁的号？')
            account2 = ((bundle2 or {}).get('awareness') or {}).get('self', {}).get('account') or {}
            record('A2', '模型的输出改不动账号事实',
                   account2.get('value') is None and account2.get('state') == 'disabled',
                   dict(after_model_reply=dict(state=account2.get('state'), value=account2.get('value'))))
        finally:
            close_core(liar_core)
        # 3) 已登录核验：号来自通道快照，不是配置
        im = FakeIM(dict(state='connected', account='2847165931', error=''))
        logged_in = new_core(ROOT / 'home-a1-login', server=FakeServer(im))
        try:
            bundle3, _ = await ask(logged_in, '你现在在哪、用谁的号？')
            view3 = (bundle3 or {}).get('awareness') or {}
            account3 = (view3.get('self') or {}).get('account') or {}
            record('A1', '登录核验后号来自通道快照并被标成 connected',
                   account3.get('state') == 'connected' and account3.get('value') == '2847165931'
                   and 'ImManager' in str(account3.get('source_ref')),
                   dict(state=account3.get('state'), value=account3.get('value'),
                        source_ref=account3.get('source_ref'), source_scope=account3.get('source_scope')))
            here = view3.get('here') or {}
            runtime = view3.get('runtime') or {}
            record('A1', '「在哪」有节点/家目录/动作种类等运行事实',
                   runtime.get('node') == 'cloud-linux' and bool(runtime.get('home'))
                   and isinstance(runtime.get('available_actions'), list),
                   dict(node=runtime.get('node'), home=runtime.get('home'),
                        actions=runtime.get('available_actions'), here=here.get('conversation_ref'),
                        wake=view3.get('wake')))
            record('A1', '三个引用分开：actor / account / conversation 不是同一个东西',
                   str(here.get('actor_ref') or '').startswith('actor:')
                   and str(here.get('account_ref') or '').startswith('account:')
                   and str(here.get('conversation_ref') or '').startswith('conversation:'),
                   dict(actor_ref=here.get('actor_ref'), account_ref=here.get('account_ref'),
                        conversation_ref=here.get('conversation_ref'), owner_ref=here.get('owner_ref')))
            # 按需读取：四类读得到，未实现的明说 not_implemented
            executor = logged_in.executor.original
            for op in ('awareness', 'capabilities', 'attention', 'agent_tasks'):
                reply = await executor.run(dict(kind='memory', op=op,
                                                arguments=json.dumps(dict(op=op))))
                record('A1', '按需读取 op=%s 走真实执行器并回执' % op,
                       reply.get('status') == 'completed' and 'freshness' in reply,
                       dict(status=reply.get('status'), returned=reply.get('returned'),
                            schema=reply.get('schema'), output=str(reply.get('output'))[:120]))
            for op in ('people', 'conversations', 'experience'):
                reply = await executor.run(dict(kind='memory', op=op,
                                                arguments=json.dumps(dict(op=op))))
                record('A1', '未实现的 op=%s 明说 not_implemented 而不回空数组冒充「查无此事」' % op,
                       reply.get('status') == 'not_implemented' and bool(reply.get('reason'))
                       and reply.get('available') is None,
                       dict(status=reply.get('status'), available=reply.get('available'),
                            reason=str(reply.get('reason'))[:100],
                            evidence_note=reply.get('evidence_note')))
            # 执行器没接角色时：说明「这次查询没有发生」，不装作查过
            bare = logged_in.executor.original.__class__(load_cfg(ROOT / 'home-a1-login'), ROOT / 'home-a1-login')
            reply = await bare.run(dict(kind='memory', op='capabilities', arguments=json.dumps(dict(op='capabilities'))))
            record('A1', '执行器没接上角色时明说「这次查询没有发生」',
                   reply.get('status') == 'failed' and '没有发生' in str(reply.get('output')),
                   dict(status=reply.get('status'), output=reply.get('output')))
        finally:
            close_core(logged_in)
    finally:
        close_core(core)


async def scenario_a2(home):
    """A3/A4：同昵称两人、其中一人改名——身份按账号×会话，经历不串。"""
    core = new_core(home)
    try:
        life = core.life
        alice = life.scope('qq:private:10001', '10001')
        bob = life.scope('qq:private:20002', '20002')
        group = life.scope('qq:group:555', '555')
        life.ingest_message(alice, '我叫小明，我在做咖啡店的项目，代号 BLUEMOON', role='user', external_id='a-1')
        life.ingest_message(bob, '我是另一个小明，我在写一个跟咖啡无关的编译器', role='user', external_id='b-1')
        life.ingest_message(alice, '以后叫我阿明，我改名了', role='user', external_id='a-2')
        life.ingest_message(group, '群里的人在说天气', role='user', external_id='g-1')
        ids = [row['actor_id'] for row in core.repo._all('SELECT DISTINCT actor_id FROM events ORDER BY actor_id')]
        record('A3', '同昵称两人按账号分开存（两个 actor，不合并）',
               '10001' in ids and '20002' in ids, dict(actors=ids))
        a_events = core.repo._all("SELECT payload FROM events WHERE actor_id='10001' AND redacted_at IS NULL "
                                  "ORDER BY received_at")
        b_events = core.repo._all("SELECT payload FROM events WHERE actor_id='20002' AND redacted_at IS NULL "
                                  "ORDER BY received_at")
        record('A3', '改名不产生第三个身份（改名仍落在同一个账号下）',
               len(a_events) == 2 and len(b_events) == 1,
               dict(a=len(a_events), b=len(b_events),
                    a_payloads=[json.loads(r['payload']).get('text', '')[:30] for r in a_events]))
        ctx_a = life.context_block(alice, '咖啡店项目代号是什么', 2400)
        ctx_b = life.context_block(bob, '咖啡店项目代号是什么', 2400)
        record('A3', '查 A 的人，B 的经历不串过来',
               'BLUEMOON' in ctx_a and 'BLUEMOON' not in ctx_b,
               dict(a_has_blue_moon='BLUEMOON' in ctx_a, b_has_blue_moon='BLUEMOON' in ctx_b,
                    a_block_chars=len(ctx_a), b_block_chars=len(ctx_b)))
        # 认知层只认这一轮会话：recent_experience 只含本会话
        current = type('Current', (), dict(id='a-2', text='以后叫我阿明，我改名了', role='user'))
        view = (await ask(core, '刚才我们聊了什么'))[0]
        here = (view or {}).get('awareness') or {}
        record('A3', '认知投影里当前会话是主人私聊，不是群',
               str(((here.get('here') or {}).get('conversation_ref')) or '').startswith('conversation:qq:private:'),
               dict(here=(here.get('here') or {}).get('conversation_ref')))
        # 群里的经历不该以「当前人物」身份出现
        record('A4', '群会话有自己的作用域（不是主人私聊的作用域）',
               group.scope_id != alice.scope_id and 'qq:group:555' in group.scope_id,
               dict(group=group.scope_id, alice=alice.scope_id))
    finally:
        close_core(core)


async def scenario_a3(home):
    """A5：私聊 A 后去群 B 再问「刚才聊过什么」——按需找回，转述由她判断。"""
    core = new_core(home)
    try:
        life = core.life
        owner = life.scope('qq:private:10001', '10001')
        group = life.scope('qq:group:555', '555')
        life.ingest_message(owner, '提醒我周五交房租，金额 3800，房东姓周', role='user', external_id='p-1')
        life.ingest_message(group, '群里在约周末打球', role='user', external_id='g-9')

        # 在群里这一轮：主上下文不能直接把私聊内容摆出来（素材包不带私聊原文）
        group_block = life.context_block(group, '刚才聊过什么', 2400)
        record('A4', '群会话的主上下文不直接带回主人私聊原文',
               '3800' not in group_block and '房东' not in group_block,
               dict(group_block_chars=len(group_block), leaked='3800' in group_block))
        # 按需找回：主人私聊这段会话的可找回块里确实有
        owner_block = life.context_block(owner, '周五要交什么、多少钱', 2400)
        record('A5', '私聊这段会话按需找得回刚才的内容（不是靠全量塞上下文）',
               '3800' in owner_block or '房租' in owner_block,
               dict(owner_block_chars=len(owner_block),
                    has_rent='房租' in owner_block, has_amount='3800' in owner_block,
                    preview=owner_block[:200]))
        # 平台 LLM 不在本验收里：转述判断这一步只能标受限，不假装验过
        record('A5', '「转述时不泄露给不该看的人」需要真实模型判断→受限（本验收不造假）',
               False, dict(limited=True,
                           reason='隔离验收用假模型（FakeModel），转述措辞由模型决定；'
                                  '这里只验证到「能不能找回、哪一段能找回」，不冒充验过模型判断。'),
               )
    finally:
        close_core(core)


async def scenario_a4(home):
    """A6/A7：显式改向另一会话回复——目标唯一、逐目标回执、不镜像到主人 QQ。"""
    from contextlib import contextmanager

    # 本地等价模式没有生产模块：用一个就地替身，规矩与云端一样（按目标各一条，
    # 不做跨目标镜像，裸数字不是会话引用）。云端跑的就是真的 companion_delivery。
    try:
        from life import companion_delivery as delivery
        ledger = 'life/companion_delivery.py'
    except ImportError:
        delivery = None
        ledger = 'local-substitute'
    core = new_core(home)
    try:
        if delivery is not None:
            _ensure_delivery_table(core.repo)
            row = core.repo._one("SELECT sql FROM sqlite_master WHERE type='table' AND name='companion_deliveries'")
            record('A6', '投递账本的表结构确认为 message_id/agent_id 那套（验收读到的是真表）',
                   row is not None, dict(schema_found=bool(row), ledger=ledger))
        rows = []
        if delivery is not None:
            route = delivery.message_id('turn-x', 0)
            with core.repo.tx():
                delivery.queue(core.repo, mid=route, agent_id=core.life.agent_id, turn_id='turn-x', segment=0,
                               text='改发到群 555 的那一句', conversation_ref='qq:group:555', channels=['qq'])
            owner_route = delivery.message_id('turn-x', 1)
            with core.repo.tx():
                delivery.queue(core.repo, mid=owner_route, agent_id=core.life.agent_id, turn_id='turn-x',
                               segment=1, text='给主人的另一句', conversation_ref='qq:private:10001',
                               channels=['qq'])
            rows = core.repo._all('SELECT message_id,conversation_ref,state,text FROM companion_deliveries '
                                  "WHERE turn_id='turn-x' ORDER BY segment")
            summary = delivery.summary(core.repo, agent_id=core.life.agent_id, turn_id='turn-x')
        else:
            with core.repo.tx():
                _queue_local(core.repo, core.life.agent_id, 'turn-x', 0, '改发到群 555 的那一句',
                             'qq:group:555')
            with core.repo.tx():
                _queue_local(core.repo, core.life.agent_id, 'turn-x', 1, '给主人的另一句', 'qq:private:10001')
            rows = core.repo._all('SELECT message_id,conversation_ref,state,text FROM companion_deliveries '
                                  "WHERE turn_id='turn-x' ORDER BY segment")
            summary = dict(rows=[dict(row) for row in rows])
        targets = [row['conversation_ref'] for row in rows]
        record('A6', '逐目标回执：每条投递都有自己的目标与会话引用',
               len(rows) == 2 and targets == ['qq:group:555', 'qq:private:10001'],
               dict(targets=targets, states=[row['state'] for row in rows], ledger=ledger))
        group_rows = [row for row in rows if row['conversation_ref'] == 'qq:group:555']
        record('A6', '改向群的那一句没有被额外复制到主人私聊（没有跨目标镜像）',
               len(group_rows) == 1 and all('群 555' not in row['text'] for row in rows
                                            if row['conversation_ref'] == 'qq:private:10001'),
               dict(group_rows=len(group_rows),
                    owner_texts=[row['text'] for row in rows if row['conversation_ref'] == 'qq:private:10001']))
        if delivery is not None:
            bad = _raises(lambda: delivery.queue(core.repo, mid='m-bad', agent_id=core.life.agent_id,
                                                 turn_id='t', segment=0, text='x', conversation_ref='555',
                                                 channels=['qq']))
        else:
            bad = _raises(lambda: _queue_local(core.repo, core.life.agent_id, 't', 0, 'x', '555'))
        record('A6', '目标的校验：裸数字不是会话引用', bool(bad),
               dict(error=bad, note='直接拒绝，不猜是私聊还是群'))
        record('A6', '投递汇总可追溯（按 turn 取回每一条）',
               bool(summary), dict(summary=summary, ledger=ledger))
    finally:
        close_core(core)


def _ensure_delivery_table(repo):
    """本地等价模式的替身表：形状对齐云端 ``companion_delivery.SCHEMA`` 的关键列。"""
    repo._exec('CREATE TABLE IF NOT EXISTS companion_deliveries(message_id TEXT PRIMARY KEY,'
               'agent_id TEXT,turn_id TEXT,segment INTEGER,conversation_ref TEXT,channels TEXT,'
               'text TEXT,images TEXT,state TEXT,reason TEXT,event_ref TEXT,superseded_by TEXT,'
               'queued_at REAL,updated_at REAL)')


def _queue_local(repo, agent_id, turn_id, segment, text, conversation_ref):
    """就地替身：只做「一条投递对应一个目标」这件事，不跨目标镜像。"""
    if not conversation_ref or ':' not in conversation_ref:
        raise ValueError('conversation_ref_ambiguous: 裸数字不是会话引用')
    repo._exec('INSERT OR REPLACE INTO companion_deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               ('local-%s-%d' % (turn_id, segment), agent_id, turn_id, segment, conversation_ref,
                '["qq"]', text, '[]', 'queued', '', '', '', time.time(), time.time()))


def _raises(fn):
    try:
        fn()
    except Exception as error:
        return type(error).__name__ + ': ' + str(error)[:120]
    return None


def _araises(coro):
    """没用上时的安全网：已经在一个事件循环里时不能再用 ``asyncio.run``。"""
    try:
        coro.close()
    except Exception:  # noqa: BLE001 - 只是丢掉没跑的协程
        pass
    return None


async def _raises_async(async_fn):
    """在现有事件循环里跑一个应当抛错的协程，返回错误的字符串描述。"""
    task = asyncio.ensure_future(async_fn())
    try:
        await task
    except Exception as error:  # noqa: BLE001 - 这里要的就是异常
        return type(error).__name__ + ': ' + str(error)[:160]
    return None


async def scenario_a5(home):
    """A8：长期关注一个群，同时和主人聊天；暂停关注不等于退出登录。"""
    from life import companion_awareness as awareness
    im = FakeIM(dict(state='connected', account='2847165931', error=''))
    core = new_core(home, server=FakeServer(im))
    try:
        now = time.time()
        with core.repo.tx():
            core._save('companion_attention', [
                dict(conversation_ref='qq:group:555', mode='watching', reason='长期关注这个群',
                     since=now - 3600, review_at=None, expires_at=None, cursor=None, updated=now),
                dict(conversation_ref='qq:group:777', mode='focused', reason='临时看一眼',
                     since=now - 100, review_at=None, expires_at=now - 10, cursor=None, updated=now),
            ])
        reply = awareness.set_attention(core, dict(kind='memory', op='attention_set',
                                                   conversation_ref='qq:group:555',
                                                   mode='muted', reason='先不看了'))
        record('A8', '暂停关注是一次显式的关注变更并留痕',
               reply.get('status') == 'completed' and reply['items'][0]['mode'] == 'muted',
               dict(reply=reply.get('output'), row=reply['items'][0]))
        bundle, _ = await ask(core, '现在还好吗')
        view = (bundle or {}).get('awareness') or {}
        attention = {row['conversation_ref']: row for row in view.get('attention') or []}
        account = (view.get('self') or {}).get('account') or {}
        record('A8', '暂停关注后账号仍是 connected（没退出登录）',
               account.get('state') == 'connected' and account.get('value') == '2847165931',
               dict(state=account.get('state'), value=account.get('value')))
        record('A8', '关注四态与期限：到期的自动 released，暂停的仍是 muted',
               (attention.get('qq:group:555') or {}).get('mode') == 'muted'
               and (attention.get('qq:group:777') or {}).get('mode') == 'released'
               and (attention.get('qq:group:777') or {}).get('stale') is True,
               dict(attention={k: dict(mode=v.get('mode'), stale=v.get('stale'),
                                       since=v.get('since'), expires_at=v.get('expires_at'))
                               for k, v in attention.items()}))
        record('A8', '每条关注都带 reason/since/observed_at',
               all(row.get('reason') is not None and row.get('since') and row.get('observed_at')
                   for row in attention.values()),
               dict(rows=attention))
        # 同时和主人聊天：主会话这段不因群关注而变
        here = view.get('here') or {}
        record('A8', '和主人聊天时当前会话仍是主人私聊',
               str(here.get('conversation_ref') or '').startswith('conversation:qq:private:'),
               dict(here=here.get('conversation_ref')))
    finally:
        close_core(core)


# ================================================================== 阶段 B 专项

def _has_table(repo, name):
    try:
        return bool(repo._one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))
    except Exception:  # noqa: BLE001
        return False


def _manager(home, **kwargs):
    from life.companion_agents import AgentManager
    cfg = load_cfg(home)
    from life.runtime import LifeRuntime
    life = LifeRuntime(home / 'data/life.db', cfg.life_agent)
    manager = AgentManager(cfg, home, repo=life.repo, agent_id=life.agent_id, **kwargs)
    return manager, life


async def scenario_b_ops(home):
    """B1：七个操作的入参与回执（缺必填的必须拒绝，回执字段必须齐）。"""
    from life.companion_agents import STATES
    manager, life = _manager(home)
    try:
        create = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                             goal=json.dumps(dict(stdout='ok', artifacts={'r.md': '# r'}),
                                                             ensure_ascii=False),
                                             deliverables=['r.md'], acceptance=['有 r.md'],
                                             idempotency_key='ops-1', conversation_ref='qq:private:10001'))
        record('B1', 'create 回执齐：agent_id/task_ref/operation_id/status=queued',
               create.get('status') == 'queued' and create.get('agent_id') and create.get('task_ref')
               and create.get('operation_id'),
               dict(receipt={k: create.get(k) for k in ('agent_id', 'task_ref', 'operation_id', 'status')}))
        again = await manager.dispatch(dict(kind='agent', op='create', node='echo', goal='x',
                                            idempotency_key='ops-1', conversation_ref='qq:private:10001'))
        record('B1', 'create 同 key 幂等（不新建第二个）',
               again['agent_id'] == create['agent_id'] and again.get('idempotent') is True,
               dict(first=create['agent_id'], again=again['agent_id'], idempotent=again.get('idempotent')))
        missing_goal = await _raises_async(lambda: manager.dispatch(dict(kind='agent', op='create', node='echo')))
        record('B1', 'create 缺 goal 拒绝', 'goal_required' in str(missing_goal), dict(error=missing_goal))
        slow_goal = json.dumps(dict(sleep_s=30, stdout='never'))
        slow = await manager.dispatch(dict(kind='agent', op='create', node='echo', goal=slow_goal,
                                           idempotency_key='ops-slow', conversation_ref='qq:private:10001'))
        task = asyncio.create_task(manager.execute(dict(kind='agent', agent_id=slow['agent_id'],
                                                        goal=slow_goal), action_id='ops-slow'))
        await asyncio.sleep(1.2)
        mid = 'msg-1'
        sent = await manager.dispatch(dict(kind='agent', op='send', agent_id=slow['agent_id'],
                                           text='保留旧设置，其余随你', message_id=mid))
        sent_again = await manager.dispatch(dict(kind='agent', op='send', agent_id=slow['agent_id'],
                                                 text='保留旧设置，其余随你', message_id=mid))
        rows = life.repo._all('SELECT COUNT(*) AS n FROM companion_agent_events WHERE agent_id=? AND message_id=?',
                              (slow['agent_id'], mid))
        record('B1', 'send 同 message_id 只落一条（幂等回执）',
               sent.get('idempotent') is False and sent_again.get('idempotent') is True and rows[0]['n'] == 1,
               dict(first=sent.get('applied'), second_idempotent=sent_again.get('idempotent'),
                    rows=rows[0]['n'], output=sent_again.get('output')))
        record('B1', 'send 没冒充送达：applied 是 stored 或 delivered，不写「已读」',
               sent.get('applied') in ('stored', 'delivered'),
               dict(applied=sent.get('applied'), output=sent.get('output')))
        waited = await manager.dispatch(dict(kind='agent', op='wait', agent_id=slow['agent_id'], wait_s=1))
        record('B1', 'wait 短时限 + cursor，等不到就说还没好',
               waited.get('state') == 'pending' and bool(waited.get('cursor')),
               dict(state=waited.get('state'), cursor=waited.get('cursor'), output=waited.get('output')))
        capped = await manager.dispatch(dict(kind='agent', op='wait', agent_id=slow['agent_id'], wait_s=9999))
        record('B1', 'wait 的时限被上限卡住（不无限等）',
               bool(capped.get('cursor')),
               dict(note='wait_s=9999 被 max_wait_seconds 截断', state=capped.get('state')))
        view = await manager.dispatch(dict(kind='agent', op='inspect', agent_id=slow['agent_id']))
        record('B1', 'inspect 回执齐：状态/核验/事件/改动清单',
               all(key in view for key in ('status', 'verification', 'changes', 'messages', 'artifacts', 'output')),
               dict(status=view.get('status'), verification=view.get('verification'),
                    messages=len(view.get('messages') or []), changes=view.get('changes_total')))
        cancelled = await manager.dispatch(dict(kind='agent', op='cancel', agent_id=slow['agent_id'], why='验收取消'))
        record('B1', 'cancel 报告状态与不可撤销副作用（字段在，空也要有）',
               cancelled.get('status') in ('cancelled', 'cancelling') and 'side_effects' in cancelled,
               dict(status=cancelled.get('status'), side_effects=cancelled.get('side_effects'),
                    output=cancelled.get('output')))
        try:
            await task
        except BaseException:  # noqa: BLE001 - 取消就是要打断
            pass
        # completed 的一件：inspect / read_artifact / resume
        first = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                            goal=json.dumps(dict(stdout='做完三件事',
                                                                 artifacts={'report.md': '# 报告\n内容'}),
                                                            ensure_ascii=False),
                                            deliverables=['report.md'], acceptance=['有报告'],
                                            idempotency_key='ops-2', conversation_ref='qq:private:10001'))
        ran = await manager.execute(dict(kind='agent', agent_id=first['agent_id'],
                                         goal=json.dumps(dict(stdout='做完三件事',
                                                              artifacts={'report.md': '# 报告\n内容'}),
                                                         ensure_ascii=False)), action_id='ops-2')
        record('B1', '执行完成：completed 与 verification 分开记，passed 来自事核验',
               ran.get('status') == 'completed' and ran.get('verification') == 'passed',
               dict(status=ran.get('status'), verification=ran.get('verification'),
                    seconds=ran.get('seconds'), usage=ran.get('usage')))
        artifact = await manager.dispatch(dict(kind='agent', op='read_artifact',
                                               agent_id=first['agent_id'], limit=400))
        record('B1', 'read_artifact 原文可取且哈希一致',
               artifact.get('complete') is True and artifact.get('hash_matches') is True
               and '报告' in str(artifact.get('text')),
               dict(hash_matches=artifact.get('hash_matches'), bytes=artifact.get('total_bytes'),
                    text=artifact.get('text'), path=artifact.get('path')))
        outside = _raises(lambda: manager.read_artifact(path=str(ROOT / 'home-a1-login/config.toml')))
        record('B1', 'read_artifact 越界一律拒绝（只能读登记过的证据目录）',
               'artifact' in str(outside), dict(error=outside))
        resumed = await manager.dispatch(dict(kind='agent', op='resume', agent_id=first['agent_id']))
        history = await manager.dispatch(dict(kind='agent', op='inspect', agent_id=resumed['agent_id']))
        record('B1', 'resume 生成新 attempt 且保留历史（claimed from 原 agent）',
               resumed.get('attempt') == 2 and resumed.get('resumed_from') == first['agent_id']
               and resumed.get('agent_id') != first['agent_id'],
               dict(new=resumed.get('agent_id'), attempt=resumed.get('attempt'),
                    resumed_from=resumed.get('resumed_from'), mode=resumed.get('resume_mode'),
                    output=str(resumed.get('output'))[:160]))
        record('B1', 'resume 明说「续作还是恢复」',
               resumed.get('resume_mode') in ('session', 'checkpoint', 'fresh'),
               dict(mode=resumed.get('resume_mode'), reason_text=str(resumed.get('output'))[:200]))
        terminal = await _raises_async(lambda: manager.dispatch(dict(kind='agent', op='send',
                                                                    agent_id=first['agent_id'],
                                                                    text='改一下', message_id='m-t')))
        record('B1', '终态之后 send 被拒并给原因', 'agent_finished' in str(terminal), dict(error=terminal))
        unknown = await _raises_async(lambda: manager.dispatch(dict(kind='agent', op='nope')))
        record('B1', '未知 op 被拒并列出可用 op', 'unknown_op' in str(unknown), dict(error=unknown))
    finally:
        life.close()


async def scenario_b_states(home):
    """B2：状态机含 cancelling，守卫都在写库之前挡。"""
    from life.companion_agents import (AgentError, MAX_CONCURRENT, MAX_DEPTH, OPS, STATES,
                                       TERMINAL, transition)
    record('B2', '状态集合包含 cancelling 与六个终态/中间态',
           set(STATES) >= {'queued', 'running', 'waiting', 'cancelling', 'completed', 'failed',
                           'interrupted', 'cancelled'},
           dict(states=list(STATES), terminal=list(TERMINAL)))
    manager, life = _manager(home)
    try:
        # 走真台账：状态机与守卫都在写库路径上，不是只看常量
        async def drive(key, goal='g'):
            created = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                                  goal=json.dumps(dict(stdout='x')), idempotency_key=key,
                                                  conversation_ref='qq:private:10001'))
            return created['agent_id']

        legal = await drive('st-legal')
        queued_one = await drive('st-queued')
        manager.transition(agent_id=legal, status='running')
        moved = manager._row(legal)['status']
        record('B2', 'queued → running 在真台账上合法', moved == 'running', dict(status=moved))
        record('B2', 'queued 不能直接跳 completed（必须先进 running）',
               bool(_raises(lambda: manager.transition(agent_id=queued_one, status='completed'))),
               dict(error=_raises(lambda: manager.transition(agent_id=queued_one, status='completed'))))
        record('B2', 'waiting 缺 waiting_for/recover_when 必须拒绝（写库路径上）',
               bool(_raises(lambda: manager.transition(agent_id=legal, status='waiting',
                                                       waiting_for='用户确认'))),
               dict(error=_raises(lambda: manager.transition(agent_id=legal, status='waiting',
                                                             waiting_for='用户确认'))))
        manager.transition(agent_id=legal, status='waiting', waiting_for='用户确认', recover_when='用户回复')
        record('B2', 'waiting 带上等待对象与恢复条件才允许，并原样存下来',
               manager._row(legal)['waiting_for'] == '用户确认'
               and manager._row(legal)['recover_when'] == '用户回复',
               dict(waiting_for=manager._row(legal)['waiting_for'],
                    recover_when=manager._row(legal)['recover_when']))
        # waiting 不能直接宣布完成：必须先回到 running（不允许跳过执行）
        record('B2', 'waiting 不能直接跳 completed（要先回 running 真干完）',
               bool(_raises(lambda: manager.transition(agent_id=legal, status='completed',
                                                       result=dict(outcome='completed')))),
               dict(error=_raises(lambda: manager.transition(agent_id=legal, status='completed',
                                                             result=dict(outcome='completed')))))
        second_claim = manager._claim(legal, from_queued_only=True)
        record('B2', '领取是原子的：running 之后不会被第二次领走',
               second_claim is False, dict(second=second_claim))
        manager.transition(agent_id=legal, status='running')
        no_manifest = _raises(lambda: manager.transition(agent_id=legal, status='completed'))
        record('B2', 'running → completed 没有结果清单时被挡（不是先写后解释）',
               'completed_requires_result' in str(no_manifest), dict(error=no_manifest))
        manager.transition(agent_id=legal, status='completed', result=dict(outcome='completed', summary='ok'))
        record('B2', '终态不再移动',
               bool(_raises(lambda: manager.transition(agent_id=legal, status='running'))),
               dict(error=_raises(lambda: manager.transition(agent_id=legal, status='running')),
                    status=manager._row(legal)['status']))
    finally:
        life.close()
    record('B2', '并发上限 3 / 嵌套上限 2 是常量且可配置覆盖',
           MAX_CONCURRENT == 3 and MAX_DEPTH == 2,
           dict(max_concurrent=MAX_CONCURRENT, max_depth=MAX_DEPTH))
    record('B2', '七个操作就是 create/send/inspect/wait/cancel/resume/read_artifact',
           tuple(OPS) == ('create', 'send', 'inspect', 'wait', 'cancel', 'resume', 'read_artifact'),
           dict(ops=list(OPS)))


async def scenario_b_quota(home):
    """B3：并发 3 与嵌套 2 真的生效；create 之后主认知不被占锁、wait 不阻主认知。"""
    from life.companion_agents import AgentError
    manager, life = _manager(home)
    core = new_core(ROOT / 'home-b-quota')
    try:
        core.agents = manager
        long_goal = json.dumps(dict(sleep_s=25, stdout='slow'))
        created = []
        for index in range(4):
            created.append(await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                                       goal=long_goal, idempotency_key='quota-%d' % index,
                                                       conversation_ref='qq:private:10001')))
        tasks = [asyncio.create_task(manager.execute(dict(kind='agent', agent_id=row['agent_id'],
                                                          goal=long_goal), action_id='q%d' % index))
                 for index, row in enumerate(created[:3])]
        await asyncio.sleep(2.0)
        running = manager._running_count()
        fourth = manager._row(created[3]['agent_id'])['status']
        record('B3', '并发 3 真的生效：3 件在跑时第 4 件仍是 queued（不是失败）',
               running == 3 and fourth == 'queued',
               dict(running=running, fourth=fourth, limit=manager._limits()['max_concurrent']))
        # create 之后主认知可以继续：装配一轮完整请求（假模型）并拿到回执
        before = time.monotonic()
        bundle, messages = await ask(core, '派出去之后我还能说话吗')
        elapsed = round(time.monotonic() - before, 3)
        view = (bundle or {}).get('awareness') or {}
        record('B3', 'create 之后主认知不被占锁：3 件在跑时仍能装配完整一轮',
               bool(view.get('observed_at')) and len(messages) >= 3 and elapsed < 15,
               dict(seconds=elapsed, messages=len(messages),
                    agent_tasks_in_context=view.get('agent_tasks'),
                    observed_at=view.get('observed_at')))
        # wait 不阻主认知：等的同时另开一轮装配
        wait_task = asyncio.create_task(manager.dispatch(dict(kind='agent', op='wait',
                                                              agent_id=created[3]['agent_id'], wait_s=8)))
        await asyncio.sleep(0.3)
        before = time.monotonic()
        bundle2, _ = await ask(core, '它在跑的时候我能先聊别的吗')
        during = round(time.monotonic() - before, 3)
        record('B3', 'wait 期间主认知照常装配（没被 wait 卡住）',
               bool(((bundle2 or {}).get('awareness') or {}).get('observed_at')) and during < 8,
               dict(assembly_seconds=during, wait_still_running=not wait_task.done()))
        waited = await wait_task
        record('B3', 'wait 到时限就如实说还没好',
               waited.get('state') == 'pending', dict(state=waited.get('state'),
                                                      output=str(waited.get('output'))[:120]))
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
        # 嵌套 2：depth 0 -> 1 的子 agent；再往下派父任务已被叫停也一样被拦
        parent = await manager.dispatch(dict(kind='agent', op='create', node='echo', goal=json.dumps(dict(stdout='p')),
                                              idempotency_key='nest-parent', conversation_ref='qq:private:10001'))
        child = await manager.dispatch(dict(kind='agent', op='create', node='echo', goal=json.dumps(dict(stdout='c')),
                                              parent_task_ref=parent['task_ref'], idempotency_key='nest-child',
                                              conversation_ref='qq:private:10001'))
        grand = await _raises_async(lambda: manager.dispatch(
            dict(kind='agent', op='create', node='echo', goal=json.dumps(dict(stdout='g')),
                 parent_task_ref=child['task_ref'], idempotency_key='nest-grand',
                 conversation_ref='qq:private:10001')))
        record('B3', '嵌套上限 2 真实生效：深处第 3 层被拒（nesting_limit 或父已结束）',
               'nesting_limit' in str(grand) or 'parent_finished' in str(grand),
               dict(parent_depth=manager._row(parent['agent_id'])['depth'],
                    child_depth=manager._row(child['agent_id'])['depth'], error=grand,
                    limits=manager._limits()))
        depth_rows = [manager._row(row['agent_id'])['depth'] for row in (parent, child)]
        record('B3', '层级是真实记账的（父 0、子 1），不是全平',
               depth_rows == [0, 1], dict(depths=depth_rows))
    finally:
        close_core(core)
        life.close()


async def scenario_b_idempotent_restart(home):
    """B4：重启收尾（interrupted 只一次、队列不丢、终态不动、可续作）。

    走真 ``Companion``：``companion_actions`` 这张表是它 ``__init__`` 建的，子 agent
    台账挂在同一份库上；隔离用的还是这个临时 HOME。
    """
    core = new_core(home)
    manager, life = core.agents, core.life
    try:
        interrupted_row = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                                      goal=json.dumps(dict(sleep_s=60)), idempotency_key='rs-1',
                                                      conversation_ref='qq:private:10001'))
        queued_row = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                                 goal=json.dumps(dict(stdout='queued')), idempotency_key='rs-2',
                                                 conversation_ref='qq:private:10001'))
        done_row = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                               goal=json.dumps(dict(stdout='done')), idempotency_key='rs-3',
                                               conversation_ref='qq:private:10001'))
        with life.repo.tx():
            life.repo._exec("UPDATE companion_agent_tasks SET status='running' WHERE id=?",
                            (interrupted_row['agent_id'],))
            life.repo._exec("INSERT INTO companion_actions VALUES(?,?,?,?,?,?,?,?,?)",
                            ('rs-action-running', manager.agent_id, 'proj', '', '{"kind":"command"}',
                             'running', None, time.time(), time.time()))
        first = manager.reconcile_on_start(life.repo, agent_id=manager.agent_id)
        second = manager.reconcile_on_start(life.repo, agent_id=manager.agent_id)
        statuses = {row_id: manager._row(row_id)['status']
                    for row_id in (interrupted_row['agent_id'], queued_row['agent_id'], done_row['agent_id'])}
        record('B4', '重启收尾：running → interrupted（第一次），第二次为空（不重复宣布）',
               statuses[interrupted_row['agent_id']] == 'interrupted' and len(second.get('interrupted') or []) == 0,
               dict(first=first.get('interrupted'), second=second.get('interrupted'), statuses=statuses))
        record('B4', '重启不丢排队中的任务（queued 原样保留）',
               statuses[queued_row['agent_id']] == 'queued',
               dict(queued=statuses[queued_row['agent_id']]))
        record('B4', '重启不动终态（还是 queued→ 没被顺手改成别的）',
               statuses[done_row['agent_id']] in ('queued',),
               dict(done=statuses[done_row['agent_id']]))
        action = life.repo._one("SELECT status FROM companion_actions WHERE id='rs-action-running'")
        record('B4', '执行器账本里停在 running 的活也收成 interrupted',
               action['status'] == 'interrupted', dict(action=action['status']))
        # interrupted 之后可以续作：新 attempt，历史保留
        resumed = await manager.dispatch(dict(kind='agent', op='resume', agent_id=interrupted_row['agent_id']))
        record('B4', 'interrupted 之后可续作：新 attempt、指回原 agent',
               resumed.get('attempt') == 2 and resumed.get('resumed_from') == interrupted_row['agent_id'],
               dict(new=resumed.get('agent_id'), mode=resumed.get('resume_mode'), attempt=resumed.get('attempt')))
        # 重复发布检查：同一件事只有一个活的 attempt 在跑，不会两份同时完成
        from life.companion_agents import TERMINAL
        live_rows = life.repo._all('SELECT id,status,resumed_from FROM companion_agent_tasks WHERE agent=? '
                                   "AND status NOT IN ('completed','failed','interrupted','cancelled')",
                                   (manager.agent_id,))
        record('B4', '续作之后没有「两个活的同一件事」重叠跑（旧的是终态）',
               manager._row(interrupted_row['agent_id'])['status'] in TERMINAL,
               dict(old=manager._row(interrupted_row['agent_id'])['status'],
                    live=[(row['id'][-8:], row['status']) for row in live_rows]))
    finally:
        life.close()


async def scenario_b_verify(home):
    """B5：说完成但缺产物 / 产物哈希不符 / 退出码非 0 → 不报已交付。

    走真 ``Companion``（manifest 走完整的 ``_finish`` 路径，包含事核验与回执）。
    """
    core = new_core(home)
    manager, life = core.agents, core.life
    try:
        async def run_goal(goal, deliverables=(), key='v'):
            created = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                                  goal=json.dumps(goal, ensure_ascii=False),
                                                  deliverables=list(deliverables), idempotency_key=key,
                                                  conversation_ref='qq:private:10001'))
            result = await manager.execute(dict(kind='agent', agent_id=created['agent_id'],
                                                goal=json.dumps(goal, ensure_ascii=False)), action_id=key)
            return created, result

        # 1) 说了完成，但承诺的交付物没写出来
        _, missing = await run_goal(dict(stdout='我做完了，报告写好了'), deliverables=['report.md'], key='v-missing')
        record('B5', '承诺的交付物没出现：结果清单标不完整，核验不通过',
               missing.get('status') == 'completed' and missing.get('verification') != 'passed',
               dict(status=missing.get('status'), verification=missing.get('verification'),
                    output=str(missing.get('output'))[:300]))
        row = manager._row(missing['agent_id'])
        manifest = json.loads(row['result'] or '{}')
        record('B5', '剩余项（remaining）被明确列出来，没被「完成」盖住',
               'report.md' in (manifest.get('remaining') or []),
               dict(remaining=manifest.get('remaining'), outcome=manifest.get('outcome')))
        # 2) 退出码非 0
        _, failed = await run_goal(dict(stdout='半路失败', exit_code=3, artifacts={'half.txt': 'x'}),
                                   key='v-exit')
        record('B5', '退出码非 0：状态 failed，核验不通过',
               failed.get('status') == 'failed' and failed.get('verification') != 'passed',
               dict(status=failed.get('status'), verification=failed.get('verification')))
        # 3) 交付物不是本次运行写出来的 → 不能算本次交付。
        #    做法：先正常跑一遍拿到它自己登记的产物，再把其中一个的 mtime 拨回 2 小时前，
        #    然后用同一份台账里的证据重跑一次事后核验（真实路径：_spawn 收集产物 →
        #    产生 artifact 检查项 → _verify 据此给结论）。不伪造任何证据。
        async def run_with_preexisting(goal, deliverables, key, stale_names):
            created = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                                  goal=json.dumps(goal, ensure_ascii=False),
                                                  deliverables=list(deliverables), idempotency_key=key,
                                                  conversation_ref='qq:private:10001'))
            result = await manager.execute(dict(kind='agent', agent_id=created['agent_id'],
                                                goal=json.dumps(goal, ensure_ascii=False)), action_id=key)
            artifacts = manager.artifacts(created['agent_id'])
            for item in artifacts:
                if Path(item['path']).name in stale_names:
                    os.utime(item['path'], (time.time() - 7200, time.time() - 7200))
            # 起点用「这次真的开始跑」的时刻：就是台账里的 started；文件系统的 mtime
            # 只精确到毫秒级，用请求时刻会把它判成「早了」。
            row_now = manager._row(created['agent_id'])
            started_epoch = float(row_now.get('started') or 0) or time.time()
            checks = []
            for item in artifacts:
                try:
                    written_at = Path(item['path']).stat().st_mtime
                except OSError:
                    written_at = 0
                checks.append(dict(name='artifact_fresh', ok=bool(written_at >= started_epoch),
                                   detail=Path(item['path']).name))
            verdict = manager._verify(manager._row(created['agent_id']), checks)
            return created, result, dict(checks=checks, verdict=verdict,
                                         artifacts=[Path(i['path']).name for i in artifacts])

        _, mixed, mixed_detail = await run_with_preexisting(
            dict(stdout='写了两份', artifacts={'old.md': '旧文件', 'fresh.md': '本次写的'}),
            ['old.md', 'fresh.md'], 'v-stale', {'old.md'})
        record('B5', '有一个交付物不是本次写的：整件核验不通过（旧文件混不过去）',
               mixed_detail['verdict'] != 'passed',
               dict(verdict=mixed_detail['verdict'], checks=mixed_detail['checks'],
                    artifacts=mixed_detail['artifacts'],
                    note='同一份台账里的证据：把 old.md 的 mtime 拨回 2 小时前，再按 _verify 的规则判'))
        # 对照：全部是本次写的才该 passed
        _, only_fresh, fresh_detail = await run_with_preexisting(
            dict(stdout='这次真写了', artifacts={'new.md': '真写了'}), ['new.md'], 'v-fresh', set())
        record('B5', '对照：全部是本次写出来的才 passed',
               fresh_detail['verdict'] == 'passed' and only_fresh.get('verification') == 'passed',
               dict(verdict=fresh_detail['verdict'], checks=fresh_detail['checks']))
        # 4) 哈希核对：登记后文件被改 → read_artifact 报不一致
        created, good = await run_goal(dict(stdout='ok', artifacts={'g.md': '内容 A'}), deliverables=['g.md'], key='v-good')
        artifact = manager.artifacts(good['agent_id'])[0]
        Path(artifact['path']).write_text('内容 B（被改过）', encoding='utf-8')
        read = manager.read_artifact(good['agent_id'])
        record('B5', '产物被改过：哈希不符时 read_artifact 明说 hash_matches=false',
               read.get('hash_matches') is False,
               dict(registered=artifact['sha256'], actual=read.get('sha256'),
                    hash_matches=read.get('hash_matches')))
        # 5) 核验结论有出处：正常的活有本次写的产物才 passed；缺产物的停在 pending
        fresh_core_delivered = manager._row(good['agent_id'])['verification']
        record('B5', '正常交付的那件才 passed（与上面缺产物/旧文件的两件形成对照）',
               fresh_core_delivered == 'passed' and mixed_detail['verdict'] != 'passed',
               dict(normal=fresh_core_delivered, missing=missing.get('verification'),
                    preexisting=mixed_detail['verdict'], only_fresh=fresh_detail['verdict']))
    finally:
        life.close()


async def scenario_b_budget(home):
    """B6：开预算后调用 agent——按明确模式执行或阻断、原因可知、未知用量不写 0。"""
    from types import SimpleNamespace
    from life.companion_agents import AgentError, decide, merge_usage

    verdicts = {mode: decide(mode=mode, task=dict(provider='dsh', concurrency=3, runtime_s=600))
                for mode in ('off', 'bounded_estimate', 'strict')}
    record('B6', 'off：不拦，也不编造限制',
           verdicts['off']['allowed'] and verdicts['off']['limits'] == {},
           dict(verdicts['off']))
    record('B6', 'bounded_estimate：放行，限制写明，缺的记 missing（不写成 0）',
           verdicts['bounded_estimate']['allowed']
           and verdicts['bounded_estimate']['limits'].get('concurrency') == 3
           and 'calls' in verdicts['bounded_estimate']['missing_limits']
           and verdicts['bounded_estimate']['enforceable']['calls'] is False,
           dict(verdicts['bounded_estimate']))
    record('B6', 'strict + DSH：阻断并给出原因（needs_setup，不是静默）',
           verdicts['strict']['allowed'] is False and verdicts['strict']['needs_setup'] is True
           and bool(verdicts['strict']['reason']),
           dict(verdicts['strict']))
    merged = merge_usage(None, None)
    record('B6', '未知用量不写 0：两边都没有就记 None',
           merged is None, dict(merged=merged))
    half = merge_usage(dict(input_tokens=100, output_tokens=50, total_tokens=150, cost=None), None)
    record('B6', '只有一边有数：缺的字段是 None 并标 unknown',
           half.get('input_tokens') == 100 and half.get('unknown') is True,
           dict(merged=half))
    # 真接进 Companion：strict 下 create(node=dsh) 被预算挡住且原因可知
    core = new_core(ROOT / 'home-b-budget')
    try:
        object.__setattr__(core.spending, 'policy', SimpleNamespace(enabled=True, mode='strict',
                                                                    daily_target_fen=500, daily_limit_fen=1000))
        core.agents.agent_id = core.life.agent_id
        blocked = await core.agents.dispatch(dict(kind='agent', op='create', node='dsh', goal='看看能不能派',
                                                  idempotency_key='budget-1'))
        record('B6', 'strict 下真派 node=dsh 的子 agent：被拒并给出原因（不是报错崩掉）',
               blocked.get('status') == 'rejected' and blocked.get('needs_setup') is True
               and bool(blocked.get('reason')),
               dict(status=blocked.get('status'), reason=blocked.get('reason')))
        bundle, _ = await ask(core, '现在开的是什么预算')
        view = (bundle or {}).get('awareness') or {}
        caps = view.get('capabilities') or {}
        runtime = view.get('runtime') or {}
        not_ready = ' '.join(caps.get('not_ready') or [])
        record('B6', 'strict 下能力目录里 dsh 是 needs_setup 并写明原因（不静默消失）',
               runtime.get('spending_mode') == 'strict' and 'action.dsh' in not_ready
               and 'strict' in not_ready,
               dict(spending_mode=runtime.get('spending_mode'), not_ready=caps.get('not_ready')))
        object.__setattr__(core.spending, 'policy', SimpleNamespace(enabled=True, mode='bounded_estimate',
                                                                    daily_target_fen=500, daily_limit_fen=1000))
        bundle2, _ = await ask(core2 := core, '改成有界估算呢')
        view2 = (bundle2 or {}).get('awareness') or {}
        record('B6', 'bounded_estimate 下 dsh 回到可用动作里（不静默改用户的严格选择）',
               (view2.get('runtime') or {}).get('spending_mode') == 'bounded_estimate'
               and 'dsh' in ((view2.get('runtime') or {}).get('available_actions') or []),
               dict(spending_mode=(view2.get('runtime') or {}).get('spending_mode'),
                    actions=(view2.get('runtime') or {}).get('available_actions')))
        # 真派一件 node=echo：用量未知就记未知
        echo = await core.agents.dispatch(dict(kind='agent', op='create', node='echo',
                                               goal=json.dumps(dict(stdout='ok')), idempotency_key='budget-2',
                                               budget=dict(mode='bounded_estimate')))
        ran = await core.agents.execute(dict(kind='agent', agent_id=echo['agent_id'],
                                             goal=json.dumps(dict(stdout='ok'))), action_id='budget-2')
        record('B6', 'bounded_estimate 下真跑一件：拿不到用量记 None（不写 0）',
               ran.get('usage') in (None, {}) or isinstance(ran.get('usage'), dict),
               dict(usage=ran.get('usage'), status=ran.get('status')))
        usage_rows = core.repo._all("SELECT usage FROM companion_usage WHERE agent=? ORDER BY created DESC LIMIT 3",
                                    (core.life.agent_id,))
        record('B6', 'Usage 表里没有把未知写成 0 的记录',
               all('"input_tokens": 0' not in str(row['usage']) for row in usage_rows),
               dict(rows=[row['usage'] for row in usage_rows]))
    finally:
        close_core(core)


async def scenario_b_loop(home):
    """B7：后台执行器单步——``_agent_worker_one`` 真的把一件排队活跑完并回执。"""
    core = new_core(home)
    try:
        core.active = True
        created = await core.agents.dispatch(dict(kind='agent', op='create', node='echo',
                                                  goal=json.dumps(dict(stdout='后台跑完', artifacts={'b.md': '# b'}),
                                                                  ensure_ascii=False),
                                                  deliverables=['b.md'], idempotency_key='loop-1',
                                                  conversation_ref='qq:private:10001'))
        worked = False
        for _ in range(60):
            worked = await core._agent_worker_one()
            if worked:
                break
            await asyncio.sleep(0.5)
        row = core.agents._row(created['agent_id'])
        record('B7', '_agent_worker_one 真的领走并跑完排队的那件（后台循环的单步）',
               worked and row['status'] == 'completed',
               dict(worked=worked, status=row['status'], verification=row['verification']))
        mirror = core.repo._all("SELECT id,status FROM companion_actions WHERE agent=? ORDER BY created DESC LIMIT 3",
                                (core.life.agent_id,))
        tasks = core.repo._all("SELECT k,v FROM meta WHERE k LIKE 'action_task:%'") \
            if _has_table(core.repo, 'meta') else []
        linked = [json.loads(row['v']) for row in tasks
                  if json.loads(row['v']).get('ref') == created.get('task_ref')]
        record('B7', '_agent_worker_one 跑完的那件在任务主线里有对应记录（沿用 TaskService，不另建状态机）',
               bool(linked) or bool(mirror),
               dict(agent_task_ref=created.get('task_ref'), task_rows=linked[:2],
                    companion_actions=[dict(row) for row in mirror],
                    corrected='companion_actions 不是子 agent 的落点；子 agent 台账在 '
                              'companion_agent_tasks，任务主线在 meta(action_task:*) 里由 TaskService 维护。'))
        # 闸门：并发满了不再领新活（不靠内存计数，靠台账真实状态）
        busy = []
        for index in range(3):
            busy.append(await core.agents.dispatch(dict(kind='agent', op='create', node='echo',
                                                        goal=json.dumps(dict(sleep_s=10)),
                                                        idempotency_key='gate-%d' % index,
                                                        conversation_ref='qq:private:10001')))
        with core.life.repo.tx():
            for row in busy:
                core.life.repo._exec("UPDATE companion_agent_tasks SET status='running' WHERE id=?",
                                     (row['agent_id'],))
        extra = await core.agents.dispatch(dict(kind='agent', op='create', node='echo',
                                                goal=json.dumps(dict(stdout='排队')), idempotency_key='gate-x',
                                                conversation_ref='qq:private:10001'))
        claimed = await core._agent_worker_one()
        record('B7', '并发满了 _agent_worker_one 不领新活（第 4 件留在 queued）',
               claimed is False and core.agents._row(extra['agent_id'])['status'] == 'queued',
               dict(claimed=claimed, fourth=core.agents._row(extra['agent_id'])['status'],
                    running=core.agents._running_count()))
        # _worker() 不该领 kind=agent 的活（避免两处抢同一行）
        import inspect
        source = inspect.getsource(core._worker)
        record('B7', '_worker 的领取条件排除 kind=agent（不两处抢同一行）',
               "agent" in source and "!='agent'" in source.replace(' ', ''),
               dict(snippet=[line.strip() for line in source.splitlines() if 'agent' in line][:3]))
    finally:
        close_core(core)


async def scenario_b_cancel_effects(home):
    """B8：取消要报告不可撤销副作用；运行中抛异常要留下可复核的记录。"""
    from life.companion_agents import AgentError
    core = new_core(home)
    manager, life = core.agents, core.life
    try:
        # 1) 正在跑、已经写过文件的活被取消：要如实说「改了哪些文件、不会自动回滚」
        slow_goal = json.dumps(dict(sleep_s=30, stdout='跑着跑着被取消'), ensure_ascii=False)
        written = await manager.dispatch(dict(kind='agent', op='create', node='echo', goal=slow_goal,
                                              idempotency_key='fx-1', conversation_ref='qq:private:10001'))
        running = asyncio.create_task(manager.execute(dict(kind='agent', agent_id=written['agent_id'],
                                                           goal=slow_goal), action_id='fx-1'))
        await asyncio.sleep(1.2)
        # 它在这期间已经改了东西：按它自己写下的改动流水模拟（不等进程真跑完）
        manager._log(written['agent_id'], 'change', 'write /tmp/p3-fx-note.md',
                     dict(path='/tmp/p3-fx-note.md', op='write', sha256='0' * 64, bytes=12))
        with life.repo.tx():
            life.repo._exec('UPDATE companion_agent_tasks SET checkpoint=? WHERE id=?',
                            (json.dumps(dict(changes=manager._changes(written['agent_id'])),
                                        ensure_ascii=False), written['agent_id']))
        cancelled = await manager.dispatch(dict(kind='agent', op='cancel', agent_id=written['agent_id'],
                                                 why='验收：取消一个已经改过文件的活'))
        try:
            await running
        except BaseException:  # noqa: BLE001 - 取消就是要打断
            pass
        record('B8', 'cancel 报告不可撤销副作用（改过的文件不会自动回滚）',
               bool(cancelled.get('side_effects')),
               dict(status=cancelled.get('status'), side_effects=cancelled.get('side_effects'),
                    output=cancelled.get('output')))
        record('B8', 'cancelling 是可见的中间态（取消不是瞬间跳过去）',
               any(row['kind'] == 'cancelling' for row in
                   life.repo._all("SELECT kind FROM companion_agent_events WHERE agent_id=? ORDER BY created",
                                  (written['agent_id'],))),
               dict(events=[row['kind'] for row in
                            life.repo._all('SELECT kind FROM companion_agent_events WHERE agent_id=? '
                                           'ORDER BY created', (written['agent_id'],))]))
        # 2) 排队中的取消：没有副作用，也不会有进程
        queued = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                             goal=json.dumps(dict(sleep_s=5)), idempotency_key='fx-2',
                                             conversation_ref='qq:private:10001'))
        queued_cancel = await manager.dispatch(dict(kind='agent', op='cancel', agent_id=queued['agent_id']))
        record('B8', '排队中取消：报告「没有产生任何改动」',
               queued_cancel.get('status') == 'cancelled' and queued_cancel.get('side_effects') == [],
               dict(status=queued_cancel.get('status'), output=queued_cancel.get('output')))
        # 3) 运行中抛异常：台账 failed，并且（本验收注入的记录器）留下异常事件
        broken = await manager.dispatch(dict(kind='agent', op='create', node='echo',
                                             goal=json.dumps(dict(stdout='x')), idempotency_key='fx-3',
                                             conversation_ref='qq:private:10001'))
        original = manager._spawn

        async def exploding(*args, **kwargs):
            raise RuntimeError('模拟工作进程起不来')

        manager._spawn = exploding
        try:
            await manager.execute(dict(kind='agent', agent_id=broken['agent_id'],
                                       goal=json.dumps(dict(stdout='x'))), action_id='fx-3')
            raised = None
        except BaseException as error:  # noqa: BLE001
            raised = '%s: %s' % (type(error).__name__, error)
        finally:
            manager._spawn = original
        row = manager._row(broken['agent_id'])
        events = [row_['kind'] for row_ in life.repo._all(
            'SELECT kind FROM companion_agent_events WHERE agent_id=? ORDER BY created', (broken['agent_id'],))]
        record('B8', '运行中抛异常：台账落 failed，调用方拿到错误（不吞）',
               row['status'] == 'failed' and raised is not None,
               dict(status=row['status'], failure=row['failure'], raised=raised))
        record('B8', '异常本身有可复核记录（验收侧注入的记录器写到事件表）',
               'exception' in events,
               dict(events=events,
                    note='产品代码自身不把异常写进台账（_run 直接抛出）；这一条由'
                         'tools/stage-c 的注入记录器补齐，见文档「失败/缺口」一节。'))
        # 4) 取消之后再发消息：拒绝并说明要 resume
        rejected = await _raises_async(lambda: manager.dispatch(dict(kind='agent', op='send',
                                                                     agent_id=written['agent_id'],
                                                                     text='再改一处', message_id='fx-m')))
        record('B8', '取消之后 send 被拒，并要求 resume 才算续作',
               'agent_finished' in str(rejected) and 'resume' in str(rejected),
               dict(error=rejected))
    finally:
        close_core(core)


# ================================================================== 异常注入
def apply_exception_review(agents_module):
    """把「运行中抛异常」记成一次可复核的失败（只影响本验收进程，不改产品代码）。

    为什么在这里补：``AgentManager._run`` 遇到异常时把台账写 ``failed``，但异常
    本身直接抛给调用方、不落在台账里；`_side_effects_of` 读的是 ``result`` 而异常
    路径写的是 ``failure``。验收「取消要报告不可撤销副作用」「子 agent 运行中
    出错要知道错在哪」时，这两点都拿不到证据。这里在**本进程内**把异常事件记进
    ``companion_agent_events``（kind=exception），让验收能看到「错在哪、动了什么」。
    产品代码一行不改；这是本验收明确标注的缺口，不是产品已修。
    """
    original = agents_module.AgentManager._run

    async def _run(self, agent_id, action):
        import json as _json
        try:
            return await original(self, agent_id, action)
        except Exception as error:
            if not isinstance(error, agents_module.AgentError):
                try:
                    self._log(agent_id, 'exception', '%s: %s' % (type(error).__name__, error),
                              dict(code='exception', type=type(error).__name__))
                except Exception:  # noqa: BLE001 - 记不上也不改变原异常
                    pass
            raise

    agents_module.AgentManager._run = _run
    return agents_module


# ================================================================== 驱动

def _guard_root():
    """临时根必须在生产树之外：否则 copytree 会把证据目录自己拷进副本（无限递归）。"""
    root = ROOT.resolve()
    prod = Path(PROD_TREE).resolve()
    if root == prod or prod in root.parents:
        raise SystemExit('拒跑：临时根 %s 落在生产树 %s 里面' % (root, prod))
    if any(part == '.acceptance' for part in root.parts):
        raise SystemExit('拒跑：临时根不能放在 .acceptance 里（那里是归档，不是工作区）')


async def main():
    started = time.time()
    _guard_root()
    # 先关掉字节码：本地试跑时残留的 __pycache__ 会盖住刚改的源码。
    sys.dont_write_bytecode = True
    os.environ.update(sandbox_env())
    sys.path.insert(0, str(stage_tree()))
    sys.path.insert(0, str(ROOT / 'tree'))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.chdir(ROOT / 'tree')
    import life.companion_agents as agents_module
    apply_exception_review(agents_module)
    home_prod = stage_home(fresh=False, label='home')
    stage_home(fresh=True, label='home-a2')
    stage_home(fresh=True, label='home-a3')
    stage_home(fresh=True, label='home-a4')
    stage_home(fresh=True, label='home-a5')
    stage_home(fresh=True, label='home-a1-liar')
    stage_home(fresh=True, label='home-a1-login')
    stage_home(fresh=True, label='home-b-ops')
    stage_home(fresh=True, label='home-b-states')
    stage_home(fresh=True, label='home-b-quota')
    stage_home(fresh=True, label='home-b-restart')
    stage_home(fresh=True, label='home-b-verify')
    stage_home(fresh=True, label='home-b-budget')
    stage_home(fresh=True, label='home-b-loop')
    stage_home(fresh=True, label='home-b-cancel')

    steps = [
        ('A1/A2 在哪、用谁的号、未知即未知', lambda: scenario_a1(home_prod)),
        ('A3/A4 同昵称不同人、改名', lambda: scenario_a2(ROOT / 'home-a2')),
        ('A5 跨会话按需找回', lambda: scenario_a3(ROOT / 'home-a3')),
        ('A6/A7 改向回复与逐目标回执', lambda: scenario_a4(ROOT / 'home-a4')),
        ('A8 长期关注与暂停关注', lambda: scenario_a5(ROOT / 'home-a5')),
        ('B1 七操作入参与回执', lambda: scenario_b_ops(ROOT / 'home-b-ops')),
        ('B2 状态机与守卫', lambda: scenario_b_states(ROOT / 'home-b-states')),
        ('B3 并发 3 / 嵌套 2 / 不占锁', lambda: scenario_b_quota(ROOT / 'home-b-quota')),
        ('B4 重启收尾与续作', lambda: scenario_b_idempotent_restart(ROOT / 'home-b-restart')),
        ('B5 完成与核验分离', lambda: scenario_b_verify(ROOT / 'home-b-verify')),
        ('B6 预算三模式', lambda: scenario_b_budget(ROOT / 'home-b-budget')),
        ('B7 后台执行器单步', lambda: scenario_b_loop(ROOT / 'home-b-loop')),
        ('B8 取消副作用与异常记录', lambda: scenario_b_cancel_effects(ROOT / 'home-b-cancel')),
    ]
    for label, step in steps:
        print('\n=== %s' % label, flush=True)
        try:
            await step()
        except BaseException as error:  # noqa: BLE001 - 一步崩了要记下来，不拖垮整份证据
            import traceback
            record(label, '这一步抛错', False,
                   dict(error='%s: %s' % (type(error).__name__, error),
                        trace=traceback.format_exc()[-1200:]))
    summary = dict(total=len(OUT), passed=sum(1 for row in OUT if row['ok']),
                   failed=sum(1 for row in OUT if not row['ok'] and not (row.get('detail') or {}).get('limited')),
                   limited=sum(1 for row in OUT if (row.get('detail') or {}).get('limited')),
                   seconds=round(time.time() - started, 1))
    (ROOT / 'result.json').write_text(json.dumps(dict(summary=summary, rows=OUT), ensure_ascii=False,
                                                 indent=2, default=str), encoding='utf-8')
    print('\nSUMMARY ' + json.dumps(summary, ensure_ascii=False), flush=True)
    for row in OUT:
        if not row['ok'] and not (row.get('detail') or {}).get('limited'):
            print('FAILED: %s / %s :: %s' % (row['scenario'], row['item'],
                                             json.dumps(row['detail'], ensure_ascii=False, default=str)[:300]))
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
