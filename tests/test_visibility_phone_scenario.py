# -*- coding: utf-8 -*-
"""第一轮验收：2026-11-20 21:33–21:38 手机场景 + 溯源 / 批次 / 撤代判复核。

这不是重复 t10–t13 的单元测试，而是把用户点名的场景从头到尾走一遍：

A. 连着说三句（打开相册 / 是副屏 / 先别点照片）——下一轮她拿到的材料里
   三句原话各自一条、顺序、时间都在，没有被空格拼成一句，最后那条限制没丢；
B. workspace_open 有回执、没有画面——材料里有 package/activity，写着
   「这次没有取得截图」，没有「成功进入相册」这类预写结论；界面树/截图像两件
   各自独立的事实，且没有规定下一步必须做什么；
C. 截图失败之后——材料给失败原因与可选项，读界面树 / 再试 / 停下三种选择
   都不被拦截，不选也不会被系统追着叫醒；
D. 溯源——同一次手机操作里 tool_report / direct_observation / model_account
   分开标，抽查真实函数输出；
E. 批次完整性 / 事件已发生≠已收到 / 重启续接 / 未确认不重发；
F. 三处代判复核：web 搜索不整批报错、相似项目只给候选、解析失败系统署名。

隔离：临时 home + 真实 Companion/LifeRuntime；不联网（web 用假 fetch）、
不碰生产数据、不写仓库文件。
"""
import asyncio
import base64
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from life import companion_batches as batches  # noqa: E402
from life import companion_delivery as ledger  # noqa: E402
from life.companion import Companion, result_view  # noqa: E402
from life.companion_context import GUIDES, GUIDES_BRIEF  # noqa: E402
from life.companion_executor import annotate_receipt  # noqa: E402
from life.companion_phone import PhoneControl  # noqa: E402
from life.companion_work import (MISSING_REASONS, PROVENANCE_DIRECT,  # noqa: E402
                                 PROVENANCE_MODEL, PROVENANCE_TOOL)

TEST_DEVICE = 'device-under-test'
PHONE_OPS = ['info', 'screenshot', 'back', 'home', 'recents', 'tap', 'text',
             'open_app', 'workspace_open', 'workspace_status', 'workspace_close']


# ---------------------------------------------------------------- 基础设施
def _png(width=48, height=64):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), (12, 34, 56)).save(buffer, format='PNG')
    return buffer.getvalue()


class _Model:
    """模型替身：只回空决策；需要记录请求时用 _RecorderModel。"""

    MAX_OUTPUT_TOKENS = 8000

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        return dict(message=dict(content='{}'), finish_reason='completed', usage=None, model='fake')

    async def close(self):
        return None


class _RecorderModel(_Model):
    def __init__(self):
        self.requests = []

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        self.requests.append(messages)
        return await super().complete(messages, json_mode, purpose, schema)


def _cfg(home, **overrides):
    base = dict(
        name='测试小鱼', owner=10001, life_db='data/life.db', life_agent='default',
        api_key='', base_url='https://api.deepseek.com', model='deepseek-flash',
        persona='persona.md', unified_persona=False, public_persona='personas/public.md',
        resource_policy={}, spending_policy={}, memory_index={}, embedding={},
        life_dynamics={}, experience={}, companion_autonomy=False,
        dsh_root='', dsh_node='', dsh_home='', dsh_provider='deepseek-official',
        media_library={}, local_media={}, profile_backfill={}, memory_organize={},
        activity={}, known_people={}, friends=[], allow_groups=[], bot_qq=10001,
        quiet_hours='', handoff_seconds=10, typing_chars_per_second=3,
        paste_threshold=1800, media_max_mb=64, media_local_roots=[],
        action_per_hour=500, action_per_day=0, acknowledge_before_work=True,
        bili_plugin='', local_embedding={}, experience_private={})
    base.update(overrides)
    cfg = SimpleNamespace(**base)
    cfg.service = SimpleNamespace(raw=None)
    return cfg


class _Request(dict):
    """aiohttp Request 的最小替身：映射 + .json()。"""

    def __init__(self, payload, device=TEST_DEVICE):
        super().__init__()
        self._payload = payload
        self.path = '/mobile/control-result'
        self['mobile_device'] = device

    async def json(self):
        return self._payload


class _Queue:
    """asyncio.Queue 的最小替身：只记手机收到的控制消息。"""

    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)

    def full(self):
        return False


def _phone(home, operations=None, *, online=True):
    control = PhoneControl.__new__(PhoneControl)
    control.devices = {}
    control.pending = {}
    if online:
        control.devices[TEST_DEVICE] = dict(
            enabled=True, operations=sorted(set(operations or PHONE_OPS)),
            workspace_state='ready', workspace_error='', workspaces=[],
            last_seen=time.time())
    control.mobile = SimpleNamespace(home=Path(home), subscribers={})
    return control


async def _deliver(control, op, value, *, target='workspace', identity='call-1'):
    """按生产路径交付一次手机回执：PhoneControl.receipt → 执行器统一出口。

    t21 把 provenance/visibility 的组装收口到 ``companion_executor.annotate_receipt``
    （与 tests/test_channel_visibility.py 的 _deliver 一致）；手机模块只交出它自己
    才知道的事实（observation.gap、截断）。所以这里也必须经过它，否则测到的不是
    生产上的回执——这正是 t21 之后本文件 3 条红的唯一原因（t22 修复见报告）。
    """
    future = asyncio.get_running_loop().create_future()
    control.pending[identity] = dict(device=TEST_DEVICE, queue=None, future=future,
                                     op=op, target=target)
    await control.receipt(_Request(dict(id=identity, result=value)))
    try:
        result = await asyncio.wait_for(future, 2)
    finally:
        # 生产里由 _dispatch 的 finally 弹出；这里直接走 receipt，要自己收尾。
        control.pending.pop(identity, None)
    return annotate_receipt('phone', dict(kind='phone', op=op, target=target), result)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='visibility-phone-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'data').mkdir(parents=True, exist_ok=True)
        (self.home / 'persona.md').write_text('You are a curious character.', encoding='utf-8')
        (self.home / 'personas').mkdir(exist_ok=True)
        (self.home / 'personas' / 'public.md').write_text('Public persona.', encoding='utf-8')
        self._cores = []

    def tearDown(self):
        for core in self._cores:
            try:
                core.life.close()
            except Exception:  # noqa: BLE001
                pass

    def core(self, model=None, **overrides):
        core = Companion(_cfg(self.home, **overrides), self.home, model=model or _Model())
        self._cores.append(core)
        batches.ensure(core.repo)
        ledger.ensure(core.repo)
        return core

    @staticmethod
    def source(core, text='看看手上的事'):
        event, _ = core.life.ingest(core.scope, 'message', 'user_report', text,
                                    role='user', payload=dict(images=[], source=dict(kind='desktop')))
        return event

    @staticmethod
    def material(core, *, reason='看看', observation=None, current=None, batches_=None,
                 source_ref='evt', purpose='activity'):
        messages = core._messages(current, reason, observation, source_ref=source_ref,
                                  purpose=purpose, wake_kind='timer', attention=[],
                                  batches=batches_)
        # [0] 是人格/工具说明，[1] 才是本轮生活材料；最后一条是用户输入。
        return messages, messages[1]['content']


# ---------------------------------------------- A. 连着说三句，一句都不能少
class ThreeInputsScenarioTest(_Base):
    TEXTS = ['打开相册', '是副屏，不是主屏', '先别点照片，让我自己看']

    def _three_inputs(self, core):
        """按 chat() 的真实做法落事件，再按输入泵的真实形状攒成一组。"""
        parts, start = [], time.time()
        for index, text in enumerate(self.TEXTS):
            event, _ = core.life.ingest(core.scope, 'message', 'user_report', text,
                                        role='user',
                                        payload=dict(images=[], source=dict(kind='mobile',
                                                                            device_id='dev',
                                                                            name='手机')))
            parts.append(dict(event=event, at=start + index * 1.2, images=[], future=None))
        return parts

    def test_three_sentences_arrive_whole_in_order_with_their_own_times(self):
        core = self.core()
        parts = self._three_inputs(core)
        core._batch = parts
        current = parts[-1]['event']
        core._latest_dialogue = current.id
        selected = core._prepare_batches(current, 'dialogue', None)
        conversation = [item for item in selected if item['level'] == batches.CONVERSATION]
        self.assertEqual(len(conversation), 1, '三句应当合到同一个对话批次里')
        events = conversation[0]['events']
        self.assertEqual([event['detail'] for event in events], self.TEXTS)

        messages, material = self.material(
            core, reason=current.text, current=current, batches_=selected,
            source_ref=current.id, purpose='dialogue')
        user = messages[-1]
        self.assertEqual(user['role'], 'user')
        text = user['content']
        # 原话一条不少、顺序不变、各自成行；没有被空格拼成一句。
        self.assertIn('连着说了 3 段', text)
        positions = [text.index(item) for item in self.TEXTS]
        self.assertEqual(positions, sorted(positions), text)
        self.assertIn('1. ' + self.TEXTS[0], text)
        self.assertIn('2. ' + self.TEXTS[1], text)
        self.assertIn('3. ' + self.TEXTS[2], text)
        self.assertNotIn(' '.join(self.TEXTS), text)
        self.assertNotIn('打开相册是副屏', text)
        # 最后那条限制原样在，没被截掉或改写成概括。
        self.assertIn(self.TEXTS[2], text)
        # 时间也在：材料里有「连续输入」的条数与跨度，批次里每句各有自己的发生时间。
        self.assertIn('input_batch', material)
        self.assertIn('"count":3', material)
        self.assertIn('span_s', material)
        times = [event['at'] for event in events]
        self.assertEqual(len(set(times)), 3, times)
        self.assertEqual(times, sorted(times))
        for stamp in times:
            self.assertIn(stamp, material)
        # 每句事件有独立 id / 来源引用，台账里可回查。
        ids = [event['event_id'] for event in events]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual([event['source_type'] for event in events], ['user_input'] * 3)
        self.assertEqual([event['source_ref'] for event in events],
                         [part['event'].id for part in parts])
        # 真正装进请求的批次才记账；记账的事件就是材料里那三条。
        settled = core._settle_batches(current.id)
        self.assertEqual(len(settled), len(selected))
        row = core.repo._one('SELECT model_ok,event_ids FROM companion_batch_deliveries '
                             'WHERE decision_id=? AND batch_id=?',
                             (current.id, conversation[0]['batch_id']))
        self.assertEqual(int(row['model_ok']), 1)
        self.assertEqual(json.loads(row['event_ids']), ids)
        self.assertEqual(batches.pending_events(core), [])


# ------------------------------------- B. 有回执、没有画面：材料要说出这一半
class OpenWithoutScreenTest(_Base):
    async def _receipt(self):
        phone = _phone(self.home, ['workspace_open', 'workspace_status', 'info', 'screenshot'])
        return phone, await _deliver(phone, 'workspace_open', dict(
            status='completed', package='com.android.gallery3d',
            activity='.GalleryActivity', workspace_id='ws-2'))

    async def test_material_has_package_and_says_no_screenshot(self):
        _phone_control, receipt = await self._receipt()
        self.assertEqual(receipt['status'], 'completed')
        core = self.core()
        current = self.source(core, '手机上打开相册了吗？现在是什么画面？')
        _messages, material = self.material(core, reason=current.text, observation=receipt,
                                            source_ref=current.id, purpose='activity')
        # 有回执的一半：真实 package / activity / workspace_id。
        self.assertIn('package=com.android.gallery3d', material)
        self.assertIn('activity=.GalleryActivity', material)
        self.assertIn('workspace_id=ws-2', material)
        # 没看见的那一半：截图与界面树各自写清「这次没有取得」。
        self.assertIn('这次没有取得截图', material)
        self.assertIn('这次没有取得界面树', material)
        self.assertIn('请求已返回只说明手机回了话', material)
        # 不预写结论：没有「成功进入相册」这类她没看见的话。
        for claim in ('成功进入相册', '进入相册成功', '相册已打开', '已看到相册'):
            self.assertNotIn(claim, material)
        self.assertNotIn('进入相册', material)
        # 不规定下一步：界面树/截图是事实，不是「必须截图」的命令。
        for order in ('必须截图', '下一步必须', '请截图', '你应该截图', '需要再截一张'):
            self.assertNotIn(order, material)
        # 两件观察各自独立，并且来源分开标。
        observation = receipt['observation']
        self.assertFalse(observation['obtained'])
        self.assertFalse(observation['screenshot']['obtained'])
        self.assertFalse(observation['ui_tree']['obtained'])
        self.assertEqual(observation['screenshot']['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(observation['ui_tree']['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(receipt['provenance'], PROVENANCE_TOOL)
        reasons = {(item['what'], item['reason']) for item in receipt['not_obtained']}
        self.assertIn(('截图', 'not_requested'), reasons)
        self.assertIn(('界面树', 'not_requested'), reasons)
        for _what, reason in reasons:
            self.assertIn(reason, MISSING_REASONS)
        # 材料里能读到两套来源（她可以分辨「谁说的」和「看到的」）。
        self.assertIn(PROVENANCE_TOOL, material)
        self.assertIn(PROVENANCE_DIRECT, material)

    async def test_ui_tree_is_a_fact_she_can_continue_from(self):
        phone = _phone(self.home, ['workspace_open', 'info', 'screenshot'])
        tree = dict(package='com.android.gallery3d', width=1080, height=2400,
                    coordinate_space='physical_screen_pixels', truncated=False,
                    nodes=[dict(node='0', text='照片'), dict(node='1', text='相册')])
        receipt = await _deliver(phone, 'info', dict(
            status='completed', output=json.dumps(tree, ensure_ascii=False)), target='main')
        core = self.core()
        current = self.source(core, '相册里现在有什么？')
        _messages, material = self.material(core, reason=current.text, observation=receipt,
                                            source_ref=current.id, purpose='activity')
        self.assertTrue(receipt['observation']['ui_tree']['obtained'])
        self.assertEqual(receipt['observation']['ui_tree']['nodes'], 2)
        self.assertIn('界面树：已取得 2 个界面节点', material)
        self.assertIn('这次没有取得截图', material)
        for order in ('必须截图', '下一步必须', '请截图'):
            self.assertNotIn(order, material)


# -------------------------------- C. 截图失败之后：三种选择都不被拦、不叫醒
class ChoicesAfterFailureTest(_Base):
    async def test_failure_reason_options_and_three_choices(self):
        phone = _phone(self.home, ['info', 'screenshot', 'back'])
        failed = await _deliver(phone, 'screenshot', dict(
            status='failed', code='workspace_unavailable',
            output='手机工作区不可用：需要 Shizuku 服务在运行、并已授权小鱼。'))
        # 失败原因、哪一层、重试有没有用：都是她决定下一步的依据。
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['code'], 'workspace_unavailable')
        self.assertIn('这次没有取得截图', failed['output'])
        self.assertIn('手机工作区不可用', failed['output'])
        self.assertEqual(failed['layer'], 'workspace')
        self.assertTrue(failed['retryable'])
        self.assertIn(('截图', 'unavailable'),
                      {(item['what'], item['reason']) for item in failed['not_obtained']})

        core = self.core()
        # 真实部署里 phone_context 由 RemoteExecutor 提供；这里把同一个 context() 挂上去，
        # 让材料里出现她真正能选的操作清单（不是测试另写一份）。
        core.executor.phone_context = lambda: phone.context()
        current = self.source(core, '截图没拿到')
        _messages, material = self.material(core, reason=current.text, observation=failed,
                                            source_ref=current.id, purpose='activity')
        self.assertIn('手机工作区不可用', material)
        self.assertIn('retryable', material)
        self.assertIn('"operations"', material)
        for op in ('info', 'screenshot', 'back'):
            self.assertIn(op, material)

        queue = _Queue()
        phone.mobile.subscribers = {queue: TEST_DEVICE}

        async def choose(op):
            task = asyncio.create_task(phone.run(dict(kind='phone', op=op, arguments='{}')))
            for _ in range(20):
                await asyncio.sleep(0)
                if phone.pending:
                    break
            self.assertTrue(phone.pending, '手机动作没有派发出去：%s' % op)
            _identity, call = next(iter(phone.pending.items()))
            call['future'].set_result(dict(status='completed', output='ok'))
            result = await asyncio.wait_for(task, 5)
            # 生产路径 RemoteExecutor.run_with_receipt 会过统一出口；这里同样过一遍。
            return annotate_receipt('phone', dict(kind='phone', op=op),
                                    result)

        # 选择一：读界面树；选择二：再试一次截图；选择三：退回去/先停下这一步。
        results = [await choose(op) for op in ('info', 'screenshot', 'back')]
        for op, result in zip(('info', 'screenshot', 'back'), results):
            self.assertNotEqual(result.get('status'), 'failed', (op, result))
        # 三次都是她主动选的：系统没有替她重试，也没有留下排队中的动作。
        self.assertEqual(phone.pending, {})
        self.assertEqual([item['op'] for item in queue.items], ['info', 'screenshot', 'back'])
        # 不选也不会被追着叫醒：手机层不产生任何新事件/批次，也没有 wake 事件。
        self.assertEqual(batches.pending_events(core), [])
        self.assertEqual(batches.select(core, conversation=True), [])
        self.assertEqual(core.repo._one("SELECT COUNT(*) AS n FROM events WHERE kind='companion_wake'")['n'], 0)
        # 提示词说的是同一件事：重不重试由她判断，系统不替她重试。
        self.assertIn('系统不替你重试', GUIDES['phone'])
        self.assertIn('由你判断', GUIDES['phone'])
        self.assertIn('phone', GUIDES_BRIEF)


# ------------------------------------------- D. 溯源：三类来源分开、不混写
class ProvenanceSwapTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='visibility-provenance-'))

    async def test_phone_screenshot_and_tree_are_direct_the_receipt_is_tool(self):
        phone = _phone(self.home, ['screenshot', 'info'])
        shot = await _deliver(phone, 'screenshot', dict(
            status='completed', output='', image_mime='image/png',
            image_png=base64.b64encode(_png()).decode('ascii')), identity='shot-1')
        self.assertTrue(shot['observation']['screenshot']['obtained'])
        self.assertTrue(Path(shot['observation']['screenshot']['path']).is_file())
        self.assertEqual(shot['observation']['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(shot['observation']['screenshot']['provenance'], PROVENANCE_DIRECT)
        # t21：手机模块不再自己写 tool_report 子块；整条回执的来源由唯一出口
        # （annotate_receipt）给出，屏幕两套观察在 provenance_parts 里逐项标。
        self.assertNotIn('tool_report', shot)
        self.assertEqual(shot['provenance'], PROVENANCE_TOOL)
        self.assertEqual(shot['provenance_parts']['receipt'], PROVENANCE_TOOL)
        self.assertEqual(shot['provenance_parts']['screenshot'], PROVENANCE_DIRECT)
        self.assertEqual(shot['provenance_parts']['ui_tree'], PROVENANCE_DIRECT)
        # 工具回执只说这次请求，不冒充画面本身（路径只在观察里）。
        self.assertNotIn('path', shot['provenance_parts'])

    async def test_executor_tool_report_and_agent_words_are_labelled(self):
        from life.companion_executor import ActionExecutor
        from life.companion_work import provenance
        executor = ActionExecutor(SimpleNamespace(
            dsh_root='', dsh_node='', dsh_home='', model='m', api_key='',
            dsh_provider='p', work_root=str(self.home), bili_plugin=''), self.home)
        try:
            # 真实函数输出（不是手写回执）：memory 台账是工具自己的报告。
            memory = executor._annotate('memory', dict(kind='memory'),
                                        dict(status='completed', output='一条记忆'))
            self.assertEqual(memory['provenance'], PROVENANCE_TOOL)
            # 真实失败路径：work.read 的错误回执也是工具报告，带分层原因。
            work = await executor.run(dict(kind='work', op='read',
                                           arguments=json.dumps(dict(path='missing.txt'))))
            self.assertEqual(work['status'], 'failed')
            self.assertEqual(work['provenance'], PROVENANCE_TOOL)
            self.assertIn('不存在', work['output'])
            self.assertIn('missing.txt', work['output'])
            # 子 agent：话是它的（model_account），产物路径与哈希是宿主算的（tool_report）。
            agent_receipt = dict(status='completed', completion='finished', agent_id='a1',
                                 output='它说做完了', message_id=None,
                                 delivery='not_recorded:no_ledger', attempt=2,
                                 artifacts=[dict(path='/tmp/a.md', sha256='abc',
                                                 sha256_source='host')],
                                 **provenance(PROVENANCE_MODEL,
                                              parts={'child_account': PROVENANCE_MODEL,
                                                     'artifacts': PROVENANCE_TOOL}))
            agent = executor._annotate('agent', dict(op='inspect'), agent_receipt)
            self.assertEqual(agent['provenance'], PROVENANCE_MODEL)
            self.assertEqual(agent['provenance_parts']['child_account'], PROVENANCE_MODEL)
            self.assertEqual(agent['provenance_parts']['artifacts'], PROVENANCE_TOOL)
            self.assertIn('子 agent 回执的送达',
                          {item['what'] for item in agent['not_obtained']})
            # dsh：输出是模型说的，finish_reason/session_id 是工具报告的事实。
            dsh = executor._annotate('dsh', dict(kind='dsh', goal='g'),
                                     dict(status='failed', failure_kind='no_session',
                                          output='没写出会话'))
            self.assertEqual(dsh['provenance'], PROVENANCE_MODEL)
            self.assertIn('session_id',
                          {item['what'] for item in dsh['not_obtained']})
            self.assertIn('finish_reason',
                          {item['what'] for item in dsh['not_obtained']})
            # 三类互不相等，也都不在 direct_observation 里塞模型的话。
            self.assertEqual(len({PROVENANCE_DIRECT, PROVENANCE_TOOL, PROVENANCE_MODEL}), 3)
            for label in ('child_account', 'artifacts'):
                self.assertIn(agent['provenance_parts'][label],
                              (PROVENANCE_MODEL, PROVENANCE_TOOL))
        finally:
            await executor.close()

    async def test_real_dsh_run_keeps_model_words_and_tool_facts_apart(self):
        """走真的执行器 → 真的 worker 子进程 → 假的 SDK：输出是模型的话（model_account），
        finish_reason / session_id 是工具报告的事实（tool_report），两者不混。"""
        from life.companion_executor import ActionExecutor
        root = self.home / 'dsh-root'
        (root / 'python' / 'sdk' / 'src' / 'deepseek_harness').mkdir(parents=True)
        (root / 'lib').mkdir()
        (root / 'lib' / 'bin.js').write_text('// fake cli', encoding='utf-8')
        (root / 'python' / 'sdk' / 'src' / 'deepseek_harness' / '__init__.py').write_text(
            'class DeepSeekHarnessConfig:\n'
            '    def __init__(self, **kwargs):\n'
            '        self.kwargs = kwargs\n'
            '\n'
            'class _Result:\n'
            "    final_response = '这句话是那个模型说的'\n"
            "    finish_reason = 'stop'\n"
            "    session_id = 'sess-fake-1'\n"
            '    events = []\n'
            '\n'
            'class DeepSeekHarness:\n'
            '    def __init__(self, config, _launch_args=None):\n'
            '        self.config = config\n'
            '    def run(self, goal):\n'
            '        return _Result()\n'
            '    def close(self):\n'
            '        pass\n', encoding='utf-8')
        executor = ActionExecutor(SimpleNamespace(
            dsh_root=str(root), dsh_node=sys.executable, dsh_home=str(self.home / 'dsh-home'),
            model='m', api_key='', dsh_provider='p', work_root=str(self.home),
            bili_plugin=''), self.home)
        try:
            result = await executor.run(dict(kind='dsh', goal='读一下今天发生的事'))
        finally:
            await executor.close()
        self.assertEqual(result['provenance'], PROVENANCE_MODEL)
        self.assertEqual(result['session_id'], 'sess-fake-1')
        self.assertEqual(result['finish_reason'], 'stop')
        self.assertEqual(result['provenance_parts']['finish_reason'], PROVENANCE_TOOL)
        self.assertIn('这句话是那个模型说的', result['output'])


# ------------------------- E. 批次完整性 / 续接 / 未确认不重发 / 订阅命中
class DeliveryLedgerScenarioTest(_Base):
    def test_batches_carry_event_ids_coverage_unexpanded_and_survive_restart(self):
        core = self.core(resource_policy=dict(delivery_max_events_per_batch=5))
        now = time.time()
        events = [batches.make_event(
            event_id=batches.event_id_for('awaited_result', 'r%d' % index),
            source_type='awaited_result', title='结果 %d' % index,
            detail='第 %d 个变化' % index, source_ref='r%d' % index,
            occurred_at=now + index, payload=dict(task_ref='t42'))
            for index in range(13)]
        batches.record(core, events)
        selected = batches.select(core, conversation=False)
        awaited = [item for item in selected if item['level'] == batches.AWAITED]
        self.assertEqual(len(awaited), 1)
        view = awaited[0]
        self.assertEqual(view['coverage']['count'], 5)
        self.assertEqual(len(view['coverage']['event_ids']), 5)
        self.assertEqual(view['unexpanded_count'], 8)
        self.assertEqual(len(view['unexpanded']['event_ids']), 8)
        self.assertTrue(view['unexpanded']['reason'])
        self.assertEqual(batches.unexpanded(core)['count'], 13)
        for event in view['events']:
            self.assertTrue(event['event_id'])
            self.assertTrue(event['at'])
        # 装进请求：material 里能看到 batch_id、每条 event_id 与未展开数量。
        core._turn_batches = list(selected)
        _messages, material = self.material(core, reason='看看手上的事', batches_=selected)
        self.assertIn(view['batch_id'], material)
        for event in view['events']:
            self.assertIn(event['event_id'], material)
        self.assertIn('"unexpanded_count":8', material)
        self.assertIn('"coverage"', material)
        self.assertEqual(core._last_material['batches']['loaded'], len(selected))
        core._settle_batches('turn-1')
        self.assertEqual(len(batches.pending_events(core)), 8)
        row = core.repo._one('SELECT model_ok,event_ids FROM companion_batch_deliveries '
                             'WHERE decision_id=? AND batch_id=?', ('turn-1', view['batch_id']))
        self.assertEqual(int(row['model_ok']), 1)
        self.assertEqual(json.loads(row['event_ids']), view['coverage']['event_ids'])
        # 模型失败：只记一次尝试，事件保持 pending，下一轮还能续接。
        core.life.close()
        self._cores.remove(core)
        restarted = self.core()
        pending = batches.pending_events(restarted)
        self.assertEqual(len(pending), 8)
        again = batches.select(restarted, conversation=False)
        self.assertTrue(again)
        batches.deliver(restarted, again, 'turn-after-restart')
        self.assertEqual(batches.pending_events(restarted), [])

    def test_model_failure_is_not_marked_delivered(self):
        core = self.core()
        now = time.time()
        batches.record(core, [batches.make_event(
            event_id=batches.event_id_for('state_change', 'phone'),
            source_type='state_change', title='手机断开了', detail='之前 ready，现在 offline',
            source_ref='phone', occurred_at=now)])
        selected = core._prepare_batches(self.source(core), 'activity', None)
        self.assertTrue(selected)
        core._drop_batches('turn-fail', reason='模型请求失败')
        pending_ids = {item['event_id'] for item in batches.pending_events(core)}
        self.assertIn(batches.event_id_for('state_change', 'phone'), pending_ids)
        row = core.repo._one('SELECT model_ok,reason FROM companion_batch_deliveries '
                             'WHERE decision_id=?', ('turn-fail',))
        self.assertEqual(int(row['model_ok']), 0)
        self.assertIn('失败', row['reason'])
        batch_row = core.repo._one('SELECT attempts,state FROM companion_batches WHERE batch_id=?',
                                   (selected[0]['batch_id'],))
        self.assertGreaterEqual(int(batch_row['attempts']), 1)
        self.assertEqual(batch_row['state'], 'pending')
        # 下一轮能继续，并且这次真的记账。
        again = core._prepare_batches(self.source(core), 'activity', None)
        self.assertTrue(again)
        core._settle_batches('turn-next')
        self.assertNotIn(batches.event_id_for('state_change', 'phone'),
                         {item['event_id'] for item in batches.pending_events(core)})

    def test_unconfirmed_delivery_is_a_fact_and_never_resent(self):
        core = self.core()
        mid = ledger.message_id('turn-9', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-9',
                     segment=0, text='我在呢', conversation_ref=core.scope.conversation_id,
                     channels=['qq'], now=1000.0)
        ledger.mark_unknown(core.repo, mid, reason='发送后没拿到端上回执', now=1001.0)
        current = self.source(core)
        selected = core._prepare_batches(current, 'dialogue', None)
        uncertain = [item for item in selected if item['level'] == batches.AWAITED]
        self.assertTrue(uncertain)
        _messages, material = self.material(core, reason=current.text, current=current,
                                            batches_=selected, source_ref=current.id,
                                            purpose='dialogue')
        self.assertIn('可能已经发出', material)
        # 2026-11-21 01:25 的重复发言事故之后，未确认投递只陈述事实：
        # 不再写「由你决定要不要再说一次」（那句会诱发她把刚说过的话再说一遍），
        # 仍要说明系统不会自动重发，并给出「对话里已经出现就不用再说」的判据。
        self.assertIn('不会自动重发', material)
        self.assertNotIn('由你决定要不要再说一次', material)
        self.assertIn('不用再说一次', material)
        core._settle_batches(current.id)
        # 台账状态没有变：unknown 仍是 unknown，没有多出一条、也没有任何重发。
        self.assertEqual(ledger.state(core.repo, mid), ledger.UNKNOWN)
        self.assertEqual(ledger.summary(core.repo, agent_id=core.life.agent_id)['unknown'], 1)
        rows = core.repo._all('SELECT message_id,state FROM companion_deliveries')
        self.assertEqual([(row['message_id'], row['state']) for row in rows],
                         [(mid, ledger.UNKNOWN)])
        # 同一条回执不会再叫醒她一次：没有新的 delivery_unknown 事件被选出。
        remaining = [item for item in batches.pending_events(core)
                     if item['source_type'] == 'delivery_unknown']
        self.assertEqual(remaining, [])
        again = [item for item in batches.select(core, conversation=True)
                 if any(event['source_type'] == 'delivery_unknown' for event in item['events'])]
        self.assertEqual(again, [])

    def test_subscription_moves_the_same_event_earlier_without_losing_it(self):
        core = self.core()
        now = time.time()
        event = batches.make_event(
            event_id=batches.event_id_for('agent_progress', 'a1'),
            source_type='agent_progress', title='子任务仍在进行',
            detail='目标 修好投递', source_ref='a1', occurred_at=now,
            payload=dict(task_ref='t42', agent_id='a1'))
        self.assertEqual(batches.classify(event, subscriptions=[]), batches.BACKGROUND)
        from life import companion_awareness as awareness
        reply = awareness.set_attention(core, dict(arguments=json.dumps(
            dict(op='attention_set', subscribe_topic='task:t42', notify=True,
                 reason='这个子任务回来就告诉我'))))
        self.assertEqual(reply['status'], 'completed')
        subscriptions = batches.subscriptions(core)
        self.assertEqual([(item['topic'], item['notify']) for item in subscriptions],
                         [('task:t42', True)])
        self.assertEqual(batches.classify(event, subscriptions=subscriptions), batches.AWAITED)
        # 订阅只改时机：事件仍在台账里，没被丢弃。
        batches.record(core, [event])
        self.assertEqual(len(batches.pending_events(core)), 1)


# ------------------------------------ F. 三处代判复核（web / 项目 / 解析失败）
class NoSubstitutionCheckTest(_Base):
    def test_web_search_shows_results_and_coverage_instead_of_failing_the_batch(self):
        from life.companion_web import WebReader
        html = ('<h3><a href="https://example.com/a">钓鱼 入门 技巧</a></h3><p>正文一</p>'
                '<h3><a href="https://example.com/b">完全无关的游戏论坛</a></h3><p>正文二</p>')

        def fetch(url):
            if 'so.com' in url:
                raise ConnectionError('so360 连不上')
            return html

        reader = WebReader(SimpleNamespace(home=self.home), fetch=fetch, search=None)
        result = asyncio.run(reader.run(dict(kind='web', op='search',
                                             arguments=json.dumps(dict(query='钓鱼 技巧')))))
        self.assertEqual(result['status'], 'completed', result)
        titles = [item['title'] for item in result['results']]
        self.assertIn('钓鱼 入门 技巧', titles)
        # 无关条目也不丢：相关性交给她判断，不替她筛。
        self.assertIn('完全无关的游戏论坛', titles)
        self.assertIn('相关性交给你判断', result['note'])
        engines = {row['engine']: row for row in result['coverage']['engines']}
        self.assertEqual(engines['baidu']['status'], 'ok')
        self.assertEqual(engines['so360']['status'], 'failed')
        self.assertIn('连不上', engines['so360']['error'])
        self.assertEqual((result['coverage']['succeeded'], result['coverage']['failed']), (1, 1))
        self.assertEqual(result['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(result['provenance_parts']['coverage'], PROVENANCE_TOOL)

    def test_similar_project_stays_a_candidate_and_parse_failure_is_system_signed(self):
        core = self.core()
        project = core.tasks.ensure(core.scope, 'project-1', '修好投递回执看不到的问题',
                                    origin='self', source_event_ids=[],
                                    origin_source_event_ids=[], key='投递修复',
                                    status='active', next_step='先看日志，再补回执')
        current = self.source(core)
        similar = dict(projects=[dict(key='新的名字', goal='修好投递回执看不到的问题',
                                     next_step='先看日志，再补回执', status='active')],
                       actions=[], commitments=[])
        core._normalize_project_refs(similar, current)
        self.assertEqual(similar['projects'][0]['key'], '新的名字')
        self.assertIsNone(core.tasks.get(core._project_ref('新的名字'), core.scope.scope_id))
        self.assertEqual(core.tasks.get(project['ref'], core.scope.scope_id)['goal'],
                         '修好投递回执看不到的问题')
        saved = core._state('project_merge_candidates') or []
        self.assertEqual(saved[0]['candidates'][0]['ref'], project['ref'])
        self.assertTrue(any('只列了候选，没有合并' in item['text']
                            for item in self._activity(core)))
        _messages, material = self.material(core, reason='看看手上的事')
        self.assertIn('project_merge_candidates', material)
        self.assertIn('程序不会替你合并', material)

        # 解析失败：取不到正文时是系统署名的故障事实，不是她的台词。
        class _ParseError(Exception):
            pass

        decision = core._salvage_reply(dict(message=dict(content='   ')), 'dialogue',
                                       current, _ParseError(), attempts=1)
        reply = decision['reply']
        self.assertTrue(reply.startswith('〔系统〕'), reply)
        for her_line in ('刚才那下没接上', '您再说一遍', '我光想'):
            self.assertNotIn(her_line, reply)
        self.assertIn('没有执行任何动作', reply)
        self.assertIn('可以重试', reply)
        notice = core._system_notice
        self.assertEqual(notice['author'], 'system')
        self.assertEqual(notice['rule'], 'parse_failed')
        events = core.repo._all("SELECT * FROM companion_batch_events WHERE source_type='boundary'")
        self.assertTrue(events)
        self.assertEqual(json.loads(events[0]['payload'])['boundary']['rule'], 'parse_failed')
        # 边界事实的措辞只陈述规则/步骤/原因，不替她下结论。
        fact = core._boundary_fact('budget', step_not_taken='DSH 调用没有执行',
                                   reason='strict 模式拿不到逐次账单', refs=['action-1'])
        blob = json.dumps(fact, ensure_ascii=False)
        for forbidden in ('你不需要', '不必做', '这不代表失败', '不是你的问题'):
            self.assertNotIn(forbidden, blob)

    @staticmethod
    def _activity(core):
        return [dict(row) for row in core.repo._all(
            'SELECT id,type,text,details,created FROM companion_activity WHERE agent=? '
            'AND type=? ORDER BY created DESC', (core.life.agent_id, 'activity'))]


if __name__ == '__main__':
    unittest.main()
