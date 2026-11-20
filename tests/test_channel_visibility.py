"""渠道可见性（t12）：请求已返回 ≠ 看到屏幕。

覆盖五个渠道里「没看见」的那一半，要求每一半都变成她能读的事实：

* 手机：workspace_open 回执给真实 package/activity，同时**明确**写着没有截图；
  界面树与截图分开标注各自是否取得；观察标 direct_observation，工具回执标 tool_report。
* B站：抽样画面给真实文件路径与覆盖范围；只读到字幕时明说「字幕不等于画面」；
  没拿到画面要写原因。
* 浏览器：截图落盘给路径；没有图给原因，不能只给字节数。
* QQ/微信：非文本段（表情/卡片/文件/语音）在正文或事件里可见，被忽略的带 status/reason。
* 小镇：回合超长被拒、委派被 superseded 都给原因；原始字段给字段清单。

隔离：临时目录 + 假 repo/life/core，不联网、不碰生产 home。
"""
import asyncio
import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from life.companion_bilibili_reader import BilibiliReader  # noqa: E402
from life.companion_browser_adapter import BrowserAdapter  # noqa: E402
from life.companion_executor import annotate_receipt  # noqa: E402
from life.companion_phone import PhoneControl  # noqa: E402
from life.companion_qq import CompanionQQ, segment_note  # noqa: E402
from life.companion_town import (TownBridge, answer_rejection, receipt_fields,  # noqa: E402
                                 row_fields, turn_fields)
from life.companion_wechat import (WeChatBot, item_label, parse_inbound,  # noqa: E402
                                   unsupported_notice)
from life.companion_work import (MISSING_REASONS, PROVENANCE_DIRECT,  # noqa: E402
                                 PROVENANCE_TOOL)

TEST_DEVICE = 'device-under-test'


def _png(width=48, height=64):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), (12, 34, 56)).save(buffer, format='PNG')
    return buffer.getvalue()


class _NullTx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeRepo:
    """只认本测试用到的几条 SQL；记录写进内存，不建库。"""

    def __init__(self):
        self.meta = {}
        self.status = {}
        self.payload = {}
        self.sql = []

    def tx(self):
        return _NullTx()

    def _one(self, sql, params=()):
        if sql.startswith('SELECT v FROM meta'):
            key = str(params[0]) if params else ''
            value = self.meta.get(key)
            return dict(v=json.dumps(value, ensure_ascii=False)) if value is not None else None
        if sql.startswith('SELECT payload'):
            identity = str(params[2]) if len(params) > 2 else ''
            body = self.payload.get(identity)
            return dict(payload=json.dumps(body, ensure_ascii=False) if body is not None else None)
        return None

    def _exec(self, sql, params=()):
        self.sql.append(sql)
        if sql.startswith('INSERT OR REPLACE INTO meta'):
            self.meta[str(params[0])] = json.loads(params[1])
        elif 'SET payload=?' in sql:
            self.payload[str(params[4])] = json.loads(params[0])
        elif 'companion_qq_delivery SET status' in sql or 'companion_wechat_delivery SET status' in sql:
            self.status[str(params[4])] = params[0]
        return None


class _Life:
    def __init__(self):
        self.events = []

    def ingest(self, scope, kind, source_kind, text, **options):
        self.events.append(dict(kind=kind, text=text, payload=options.get('payload') or {}))
        return SimpleNamespace(id='evt-1'), True


class _Request(dict):
    """aiohttp.web.Request 的最小替身：映射 + .json()。"""

    def __init__(self, payload, device=TEST_DEVICE, path='/mobile/control-result'):
        super().__init__()
        self._payload = payload
        self.path = path
        self['mobile_device'] = device

    async def json(self):
        return self._payload


def _phone(home):
    control = PhoneControl.__new__(PhoneControl)
    control.devices = {}
    control.pending = {}
    control.mobile = SimpleNamespace(home=Path(home), subscribers={})
    return control


async def _deliver(control, op, value, *, target='workspace', identity='call-1'):
    """按生产路径交付一次手机回执：phone.run/receipt → 执行器统一出口。

    来源分级与缺口组装只有一份实现（companion_executor.annotate_receipt），
    所以这里也必须经过它，否则测的就不是生产上的回执。
    """
    future = asyncio.get_running_loop().create_future()
    control.pending[identity] = dict(device=TEST_DEVICE, queue=None, future=future,
                                     op=op, target=target)
    await control.receipt(_Request(dict(id=identity, result=value)))
    result = await asyncio.wait_for(future, 2)
    return annotate_receipt('phone', dict(kind='phone', op=op, target=target), result)


class PhoneVisibilityTests(unittest.IsolatedAsyncioTestCase):
    """手机三态：有图 / 无图 / 截图失败；界面树与截图分开。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='t12-phone-')
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.phone = _phone(self.home)

    async def test_screenshot_comes_back_with_a_real_path(self):
        result = await _deliver(self.phone, 'screenshot', dict(
            status='completed', output='', image_mime='image/png',
            image_png=base64.b64encode(_png()).decode('ascii')))
        self.assertEqual(result['status'], 'completed')
        observation = result['observation']
        self.assertEqual(observation['provenance'], PROVENANCE_DIRECT)
        self.assertTrue(observation['screenshot']['obtained'])
        self.assertTrue(Path(observation['screenshot']['path']).is_file())
        self.assertEqual(observation['screenshot']['provenance'], PROVENANCE_DIRECT)
        # 回执级来源由唯一出口（annotate_receipt）组装：正文是 tool_report，
        # 截图/界面树逐项标 direct_observation。
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        self.assertEqual(result['provenance_parts']['screenshot'], PROVENANCE_DIRECT)
        self.assertEqual(result['provenance_parts']['ui_tree'], PROVENANCE_DIRECT)
        self.assertIn('截图：已取得', result['output'])
        self.assertNotIn('这次没有取得截图', result['output'])
        # 拿到图就不该再报「没取得截图」的缺口；界面树这次没要求，所以缺口只剩它。
        self.assertFalse(result['visibility']['complete'])
        self.assertEqual([(item['what'], item['reason'])
                          for item in result['visibility']['not_obtained']],
                         [('界面树', 'not_requested')])

    async def test_workspace_open_reports_package_and_no_screenshot(self):
        result = await _deliver(self.phone, 'workspace_open', dict(
            status='completed', package='com.android.gallery3d',
            activity='.GalleryActivity', workspace_id='ws-2'))
        text = result['output']
        self.assertEqual(result['status'], 'completed')
        self.assertIn('package=com.android.gallery3d', text)
        self.assertIn('activity=.GalleryActivity', text)
        self.assertIn('这次没有取得截图', text)
        self.assertIn('这次没有取得界面树', text)
        # 不预写成功页：包名/activity 只证明启动请求被接受。
        self.assertNotIn('进入相册', text)
        self.assertNotIn('相册成功', text)
        self.assertIn('请求已返回只说明手机回了话', text)
        self.assertEqual(result['workspace_id'], 'ws-2')
        observation = result['observation']
        self.assertFalse(observation['obtained'])
        self.assertFalse(observation['screenshot']['obtained'])
        self.assertFalse(observation['ui_tree']['obtained'])
        self.assertEqual(observation['package'], 'com.android.gallery3d')
        self.assertEqual(observation['activity'], '.GalleryActivity')
        # 来源分级：回执本身是 tool_report，截图/界面树是 direct_observation，逐项标。
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        self.assertEqual(result['provenance_parts']['screenshot'], PROVENANCE_DIRECT)
        self.assertEqual(result['provenance_parts']['ui_tree'], PROVENANCE_DIRECT)
        gaps = result['visibility']['not_obtained']
        reasons = {(item['what'], item['reason']) for item in gaps}
        self.assertIn(('截图', 'not_requested'), reasons)
        self.assertIn(('界面树', 'not_requested'), reasons)
        self.assertFalse(result['visibility']['complete'])
        for _what, reason in reasons:
            self.assertIn(reason, MISSING_REASONS)

    async def test_completed_screenshot_without_pixels_is_failed_and_says_so(self):
        result = await _deliver(self.phone, 'screenshot', dict(status='completed', output='ok'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'phone_missing_image')
        self.assertIn('这次没有取得截图', result['output'])
        self.assertIn('手机回了完成，但回传里没有画面', result['output'])
        self.assertFalse(result['observation']['screenshot']['obtained'])

    async def test_broken_image_payload_keeps_target_and_reason(self):
        result = await _deliver(self.phone, 'screenshot', dict(
            status='completed', output='', image_mime='image/png', image_png='!!!',
            workspace_id='ws-9'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'phone_invalid_image')
        # 「对什么失败」不能丢。
        self.assertEqual(result['workspace_id'], 'ws-9')
        self.assertIn('这次没有取得截图', result['output'])
        self.assertIn('phone_invalid_image', result['output'])

    async def test_failed_status_reports_no_screenshot_and_no_tree(self):
        result = await _deliver(self.phone, 'workspace_open', dict(
            status='failed', code='workspace_unavailable',
            output='手机工作区不可用：需要 Shizuku 服务在运行、并已授权小鱼。'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('这次没有取得截图', result['output'])
        self.assertIn('这次没有取得界面树', result['output'])
        self.assertIn('手机工作区不可用', result['output'])
        self.assertEqual(result['observation']['screenshot']['provenance'], PROVENANCE_DIRECT)
        gaps = {item['what']: item['reason'] for item in result['not_obtained']}
        # workspace_open 本来就不取画面也不读界面树：两条缺口都按 not_requested 记。
        self.assertEqual(gaps.get('截图'), 'not_requested')
        self.assertEqual(gaps.get('界面树'), 'not_requested')

    async def test_ui_tree_is_a_separate_observation(self):
        tree = dict(package='com.android.settings', width=1080, height=2400,
                    coordinate_space='physical_screen_pixels', truncated=False,
                    nodes=[dict(node='0', text='设置', bounds=[0, 0, 100, 100])])
        result = await _deliver(self.phone, 'info', dict(
            status='completed', output=json.dumps(tree, ensure_ascii=False)), target='main')
        observation = result['observation']
        self.assertTrue(observation['ui_tree']['obtained'])
        self.assertEqual(observation['ui_tree']['nodes'], 1)
        self.assertEqual(observation['ui_tree']['package'], 'com.android.settings')
        self.assertEqual(observation['ui_tree']['provenance'], PROVENANCE_DIRECT)
        self.assertFalse(observation['screenshot']['obtained'])
        self.assertIn('界面树：已取得 1 个界面节点', result['output'])
        self.assertIn('这次没有取得截图', result['output'])
        # 不规定下一步：只说事实，不出现「必须截图」。
        self.assertNotIn('必须截图', result['output'])

    async def test_truncated_ui_tree_is_reported_as_truncated(self):
        tree = dict(package='com.android.settings', width=1080, height=2400,
                    coordinate_space='physical_screen_pixels', truncated=True,
                    nodes=[dict(node=str(index)) for index in range(180)])
        result = await _deliver(self.phone, 'info', dict(
            status='completed', output=json.dumps(tree, ensure_ascii=False)), target='main')
        self.assertTrue(result['observation']['ui_tree']['truncated'])
        self.assertIn('清单被截断', result['output'])
        truncated = result['visibility']['truncated']
        self.assertTrue(any(item['what'] == '界面节点清单' for item in truncated))
        self.assertFalse(result['visibility']['complete'])


class _Backend:
    """B站后端的替身：只回它被问到的那几个工具。"""

    def __init__(self, text='第一段字幕内容。', title='测试视频'):
        self.text = text
        self.title = title

    async def call(self, op, args):
        if op == 'bili_video_info':
            value = dict(title=self.title, author='作者', pages=1)
        elif op == 'bili_subtitle':
            value = dict(text=self.text, language='zh', segments=2) if self.text else dict(error='没有字幕')
        else:
            value = dict(comments=[dict(content='好看', like=3)])
        return dict(content=[dict(type='text', text=json.dumps(value, ensure_ascii=False))])


class _NoFrames:
    async def prepare(self, bvid, time_seconds=None):
        return dict(status='failed', code='frames_no_video')


class _Frames:
    def __init__(self, sheet, *, timestamps=None, kind='grid_3x3', scope='首分P 九宫格抽样'):
        self.sheet = Path(sheet)
        self.timestamps = timestamps or [0, 13.5, 27]
        self.kind = kind
        self.scope = scope

    async def prepare(self, bvid, time_seconds=None):
        return dict(status='completed', sheet_path=str(self.sheet), timestamps=self.timestamps,
                    coverage=dict(kind=self.kind, scope=self.scope, method='等间隔'),
                    cache_key='cache-1')


class BilibiliVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='t12-bili-')
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        core = SimpleNamespace(home=self.home, executor=SimpleNamespace(_bilibili=_Backend()),
                               cfg=SimpleNamespace(model='test-model'))
        self.reader = BilibiliReader(core)

    def _stub_summary(self):
        async def summarize(payload, *, brief=False, image=None):
            return dict(summary='画面里是设置页', limitations=['小字看不清'])
        self.reader._summarize = summarize

    async def _stub_subtitle(self, args):
        return dict(status='completed', completion='observed', output='字幕回执',
                    summary='讲了设置怎么改', limitations=[], source_path=str(self.home / 's.json'),
                    subtitle=dict(obtained=True, source_path=str(self.home / 's.json')),
                    coverage=dict(scope='首分P字幕', complete=True))

    async def test_subtitle_only_says_reading_words_is_not_seeing_picture(self):
        async def summarize(payload, *, brief=False, image=None):
            return dict(summary='字幕讲了什么', limitations=[])
        self.reader._summarize = summarize
        result = await self.reader.run(dict(op='bili_subtitle', arguments=json.dumps(
            dict(bvid='BV1234567890'))))
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['subtitle']['obtained'])
        self.assertFalse(result['visual']['observed'])
        self.assertFalse(result['visual']['requested'])
        self.assertIn('只读到字幕不等于看了画面', result['output'])
        self.assertIn('画面：这次只读了字幕，没有取得画面', result['output'])
        # 没画面这件事同时进 limitations：下游不用解析中文回执也能看见。
        self.assertTrue(any('没有取得画面' in item for item in result['limitations']))
        gaps = {item['what']: item['reason'] for item in result['not_obtained']}
        self.assertEqual(gaps.get('视频画面'), 'not_requested')
        self.assertFalse(result['visibility']['complete'])

    async def test_missing_frames_give_reason_and_keep_subtitle_separate(self):
        self.reader._read_text = self._stub_subtitle
        self.reader.frames = _NoFrames()
        result = await self.reader._read(dict(bvid='BV1234567890', visual=True, comments=False))
        self.assertFalse(result['visual']['observed'])
        self.assertTrue(result['visual']['requested'])
        self.assertIn('frames_no_video', result['visual']['reason'])
        self.assertIn('画面未取得（frames_no_video）', result['output'])
        self.assertIn('只读到字幕不等于看了画面', result['output'])
        self.assertTrue(result['subtitle']['obtained'])
        self.assertTrue(any('画面未取得' in item for item in result['limitations']))
        gaps = {item['what']: item['reason'] for item in result['not_obtained']}
        self.assertEqual(gaps.get('视频画面'), 'unavailable')

    async def test_sampled_frames_give_real_path_and_coverage(self):
        self.reader._read_text = self._stub_subtitle
        self._stub_summary()
        sheet = self.home / 'sheet.png'
        sheet.write_bytes(_png())
        self.reader.frames = _Frames(sheet)
        result = await self.reader._read(dict(bvid='BV1234567890', visual=True, comments=False))
        self.assertTrue(result['visual']['observed'])
        self.assertEqual(result['visual']['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(result['visual']['sheet_path'], str(sheet))
        self.assertIn('画面文件：' + str(sheet), result['output'])
        self.assertIn('九宫格抽样', result['output'])
        self.assertEqual(result['coverage']['timestamps'], [0, 13.5, 27])
        self.assertIn('首分P 九宫格抽样', result['output'])
        # 画面和字幕这次都拿到了：没有缺口。
        self.assertTrue(result['visibility']['complete'])
        self.assertEqual(result['not_obtained'], [])

    async def test_cached_report_without_its_frame_file_does_not_claim_a_picture(self):
        saved = dict(status='completed', completion='observed', output='旧回执', summary='旧报告',
                     limitations=[], coverage={}, receipt_ref='bili-visual:x',
                     visual=dict(observed=True, sheet_path=str(self.home / 'gone.png'),
                                 coverage={}, timestamps=[]))
        fixed = BilibiliReader._cached_visual(saved)
        self.assertFalse(fixed['visual']['observed'])
        self.assertFalse(fixed['visual']['file_present'])
        self.assertIn('已经不在了', fixed['output'])


class BrowserVisibilityTests(unittest.TestCase):
    """适配器只交出事实；来源/缺口/正文的可用性判定都归唯一出口。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='t12-browser-')
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.adapter = BrowserAdapter(self.home)

    def _annotated(self, tool, payload):
        result = self.adapter._normalize(tool, payload, 0.1)
        return annotate_receipt('browser', dict(kind='browser', op='call'), result)

    def test_image_is_saved_and_the_shared_exit_states_where_it_is(self):
        encoded = base64.b64encode(_png()).decode('ascii')
        result = self._annotated('screenshot', dict(content=[
            dict(type='text', text='page loaded'),
            dict(type='image', mimeType='image/png', data=encoded)]))
        self.assertEqual(result['status'], 'completed')
        item = result['images'][0]
        # 事实由适配器给：真的落盘了、真实路径、云端能读到（本进程读一次）。
        self.assertTrue(item['saved'])
        self.assertTrue(Path(item['path']).is_file())
        self.assertTrue(item.get('cloud_readable'))
        self.assertEqual(result['image_paths'], [item['path']])
        # 正文由唯一出口写，不在这里自造「已保存」。
        self.assertIn('截图已保存（本进程可读）：' + item['path'], result['output'])
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        self.assertTrue(result['visibility']['complete'])
        self.assertEqual(result['not_obtained'], [])

    def test_desktop_only_screenshot_uses_t20_wording_verbatim(self):
        encoded = base64.b64encode(_png()).decode('ascii')
        result = self.adapter._normalize('screenshot', dict(content=[
            dict(type='image', mimeType='image/png', data=encoded)]), 0.1)
        item = result['images'][0]
        Path(item['path']).unlink()          # 桌面端有、本进程读不到
        annotated = annotate_receipt('browser', dict(kind='browser', op='call'), result)
        image = annotated['images'][0]
        self.assertFalse(image['saved'])
        self.assertFalse(image['cloud_readable'])
        self.assertEqual(image['desktop_path'], item['path'])
        # t20 已验收的文案，逐字一致。
        self.assertIn('截图在桌面端，云端读不到：%s（%s）'
                      % (item['path'], image['reason']), annotated['output'])
        gaps = {gap['what']: gap['reason'] for gap in annotated['not_obtained']}
        self.assertEqual(gaps.get('页面截图'), 'unavailable')

    def test_no_image_says_so_instead_of_only_bytes(self):
        result = self._annotated('read', dict(content=[
            dict(type='text', text='页面读到了')]))
        self.assertEqual(result['images'], [])
        self.assertIn('没有返回截图', result['output'])
        self.assertNotIn('字节', result['output'])
        gaps = {item['what']: item['reason'] for item in result['not_obtained']}
        self.assertEqual(gaps.get('页面截图'), 'not_requested')

    def test_image_that_cannot_be_written_gives_reason(self):
        result = self._annotated('screenshot', dict(content=[
            dict(type='image', mimeType='image/png', data='!!!')]))
        self.assertFalse(result['images'][0]['saved'])
        self.assertIn('base64', result['images'][0]['reason'])
        self.assertIn('有图片但没有落盘', result['output'])
        gaps = {item['what']: item['reason'] for item in result['not_obtained']}
        self.assertEqual(gaps.get('页面截图'), 'failed')


class SingleAssemblyTests(unittest.TestCase):
    """三条旁路（phone/browser/computer）不再各自组装来源。

    唯一实现是 ``companion_executor.annotate_receipt``；这里用源码断言把它钉住，
    以后谁再往旁路模块里塞一套 provenance 组装，测试会立刻红。
    """

    def _source(self, name):
        return (ROOT / 'life' / name).read_text('utf-8')

    def test_bypass_modules_have_no_provenance_assembly(self):
        for name in ('companion_phone.py', 'companion_browser_adapter.py',
                     'companion_computer.py'):
            source = self._source(name)
            self.assertNotIn('provenance(', source,
                             '%s 不应再自己组装 provenance' % name)
            self.assertFalse(hasattr(__import__('life.' + name[:-3], fromlist=['*']), '_label'),
                             '%s 不应再带自己的 label 层' % name)

    def test_no_bypass_module_aggregates_gaps_into_visibility(self):
        for name in ('companion_phone.py', 'companion_browser_adapter.py',
                     'companion_computer.py'):
            source = self._source(name)
            self.assertNotIn('visibility(not_obtained', source,
                             '%s 不应自己把缺口汇总成 visibility' % name)
            self.assertNotIn('visibility(skipped', source, '%s 不应自己汇总 visibility' % name)

    def test_phone_only_supplies_truncation_facts(self):
        source = self._source('companion_phone.py')
        self.assertEqual(source.count('visibility('), 1,
                         'phone 只应交出「哪里被截断」这一条事实')
        self.assertIn('visibility(truncated=truncated)', source)


class BrowserRunVisibilityTests(unittest.IsolatedAsyncioTestCase):
    """run() 早退的失败也必须带来源与缺口，不能只剩一句话。"""

    async def test_bad_arguments_receipt_carries_provenance_and_gap(self):
        tmp = tempfile.TemporaryDirectory(prefix='t12-browser-run-')
        self.addCleanup(tmp.cleanup)
        adapter = BrowserAdapter(Path(tmp.name))
        result = await adapter.run(dict(op='call', arguments='{不是 JSON'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'bad_arguments')
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        self.assertTrue(result['visibility']['not_obtained'])
        self.assertIn(result['visibility']['not_obtained'][0]['reason'], MISSING_REASONS)


class QQVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='t12-qq-')
        self.addCleanup(self._tmp.cleanup)
        self.repo = _FakeRepo()
        self.chats = []
        self.qq = CompanionQQ.__new__(CompanionQQ)
        self.qq.repo = self.repo
        self.qq.agent = 'xiaoyu'
        self.qq.home = Path(self._tmp.name)
        self.qq.error = ''
        self.qq.http = None

        async def chat(text, images=None, **options):
            self.chats.append(dict(text=text, images=list(images or []), options=options))

        self.qq.core = SimpleNamespace(chat=chat, supports_message_routing=True)

    async def test_non_text_segments_are_visible_in_the_text(self):
        event = dict(message=[
            dict(type='text', data=dict(text='在吗')),
            dict(type='record', data=dict(file='owner-secret.silk')),
            dict(type='face', data=dict(id='13')),
        ])
        await self.qq._receive('m1', event)
        self.assertEqual(len(self.chats), 1)
        text = self.chats[0]['text']
        self.assertIn('在吗', text)
        self.assertIn('这条是语音，我没有看到内容', text)
        self.assertIn('这条是表情，我没有看到内容', text)
        # 类型可见，内容（文件名）不泄漏。
        self.assertNotIn('owner-secret.silk', text)
        self.assertEqual(self.repo.status['m1'], 'received')
        # 缺口按 t11 的分层原因记：通道不支持这类内容 = unsupported。
        gaps = self.repo.payload['m1']['visibility']['not_obtained']
        self.assertTrue(any(item['reason'] == 'unsupported' and '语音' in item['what']
                            for item in gaps))

    async def test_unknown_segment_is_labelled_not_dropped(self):
        await self.qq._receive('m2', dict(message=[dict(type='weird')]))
        self.assertIn('未知类型', self.chats[0]['text'])

    async def test_message_with_nothing_readable_keeps_status_and_reason(self):
        await self.qq._receive('m3', dict(message=[]))
        self.assertEqual(self.repo.status['m3'], 'ignored')
        payload = self.repo.payload['m3']
        self.assertEqual(payload['status'], 'ignored')
        self.assertTrue(payload['reason'])
        self.assertEqual(payload['gap']['reason'], 'unsupported')
        self.assertIn(payload['gap']['reason'], MISSING_REASONS)

    def test_segment_note_never_copies_payload(self):
        note = segment_note(dict(type='file', data=dict(file='private.pdf', name='工资单.pdf')))
        self.assertIn('文件', note)
        self.assertNotIn('private.pdf', note)
        self.assertNotIn('工资单', note)


class WeChatVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = _FakeRepo()
        self.chats = []
        self.bot = WeChatBot.__new__(WeChatBot)
        self.bot.repo = self.repo
        self.bot.agent = 'xiaoyu'
        self.bot.error = ''

        async def chat(text, images=None, **options):
            self.chats.append(dict(text=text, images=list(images or []), options=options))

        self.bot.core = SimpleNamespace(chat=chat, supports_message_routing=True)

    def test_parse_inbound_labels_every_non_text_kind(self):
        parsed = parse_inbound(dict(message_type=1, from_user_id='owner', message_id='w1', item_list=[
            dict(type=1, text_item=dict(text='看这个')),
            dict(type=2, image_item=dict()),
            dict(type=3, voice_item=dict()),
            dict(type=4, file_item=dict()),
            dict(type=6),
            dict(type=9),
            dict(type=12),
        ]))
        self.assertEqual(parsed['text'], '看这个')
        self.assertEqual(parsed['unsupported'], ['图片', '语音', '文件', '表情', '位置', '非文本内容（类型 12）'])
        self.assertIn('图片', parsed['kinds'])

    def test_voice_with_asr_stays_text_not_unsupported(self):
        parsed = parse_inbound(dict(message_type=1, from_user_id='owner', message_id='w2', item_list=[
            dict(type=3, voice_item=dict(text='转写出来的话')),
        ]))
        self.assertIn('[语音] 转写出来的话', parsed['text'])
        self.assertEqual(parsed['unsupported'], [])

    async def test_receive_puts_the_notice_in_the_message_body(self):
        parsed = parse_inbound(dict(message_type=1, from_user_id='owner', message_id='w3', item_list=[
            dict(type=1, text_item=dict(text='看这个')),
            dict(type=2),
        ]))
        await self.bot._receive('w3', parsed)
        text = self.chats[0]['text']
        self.assertIn('看这个', text)
        self.assertIn('这条是图片，我没有看到内容', text)
        self.assertEqual(self.repo.status['w3'], 'received')

    async def test_ignored_message_keeps_status_and_reason(self):
        parsed = dict(text='', unsupported=[], kinds=[], context_token='', message_id='w4',
                      sender='owner')
        await self.bot._receive('w4', parsed)
        self.assertEqual(self.repo.status['w4'], 'ignored')
        self.assertTrue(self.repo.payload['w4']['reason'])
        self.assertEqual(self.repo.payload['w4']['gap']['reason'], 'unsupported')

    def test_notice_only_names_types(self):
        self.assertIn('这条是图片、文件', unsupported_notice(['图片', '文件']))
        self.assertIn('没有可读的文字', unsupported_notice([]))
        self.assertEqual(item_label(99), '非文本内容（类型 99）')


class _TownScope:
    def __init__(self):
        self.scope_id = 'scope-town'
        self.conversation_id = 'town:fish-1'


def _town_bridge():
    repo = _FakeRepo()
    life = _Life()
    bridge = TownBridge.__new__(TownBridge)
    bridge.repo = repo
    bridge.life = life
    bridge.core = SimpleNamespace(scope=_TownScope())
    bridge.scope = _TownScope()
    bridge.scope.scope_id = 'scope-town'
    return bridge, repo, life


class TownVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_overlong_answer_is_rejected_with_a_reason(self):
        rejected = answer_rejection('x' * 30, dict(max_chars=10))
        self.assertEqual(rejected['state'], 'invalid')
        # 超长按 t11 的六层原因算 limit，不另造词。
        self.assertEqual(rejected['layer'], 'limit')
        self.assertIn(rejected['layer'], MISSING_REASONS)
        self.assertIn('超长', rejected['reason'])
        self.assertIn('10', rejected['reason'])
        self.assertIn('30', rejected['reason'])

    def test_other_rejections_and_acceptance_are_separated(self):
        self.assertIsNone(answer_rejection('短回答', dict(max_chars=10, format='text')))
        self.assertIsNone(answer_rejection('{"plan":{}}', dict(format='json')))
        self.assertEqual(answer_rejection('不是 JSON', dict(format='json'))['state'], 'invalid')
        self.assertEqual(answer_rejection('  ', dict(max_chars=10))['state'], 'invalid')

    async def test_rejected_turn_keeps_reason_and_a_visible_event(self):
        bridge, repo, life = _town_bridge()
        turn = dict(turn_id='turn-1', kind='agency', deadline=1.0,
                    packet=dict(messages=[dict(role='user', content='说点什么')],
                                reply=dict(max_chars=10)))
        rejected = answer_rejection('x' * 30, turn['packet']['reply'])
        record = bridge._reject_turn('turn-1', rejected['state'], rejected['reason'],
                                     layer=rejected['layer'], fields=turn_fields(turn))
        self.assertIn('超长', record['reason'])
        self.assertEqual(record['gap']['reason'], 'limit')
        self.assertEqual(record['fields'], ['deadline', 'kind', 'packet', 'packet.messages',
                                            'packet.reply', 'turn_id'])
        stored = repo.meta['town-turn-reject:scope-town:turn-1']
        self.assertEqual(stored['state'], 'invalid')
        self.assertIn('超长', stored['reason'])
        self.assertEqual(stored['gap']['reason'], 'limit')
        # 她原本只看到一个 invalid 状态；现在有一条能读的事件。
        self.assertTrue(any('超长' in event['text'] for event in life.events))
        self.assertEqual(bridge.turn_rejection('turn-1')['reason'], record['reason'])

    async def test_superseded_delegation_says_what_replaced_it(self):
        bridge, repo, _life = _town_bridge()
        bridge._save_superseded('d1', by='新的目标：去看电影')
        row = dict(id='d1', scope='scope-town', project_ref='proj-1', goal='去看画展',
                   state='superseded', turn_id=None, wake_result=None, receipt=None,
                   created=0.0, updated=0.0)
        result = bridge._delegation_result(row)
        self.assertEqual(result['status'], 'completed')
        self.assertIn('替代它的是：新的目标：去看电影', result['output'])
        self.assertIn('不会再送入小镇回合', result['output'])
        # 没交出去 = 缺口，按 unavailable 标，来源是工具回执。
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        gaps = result['visibility']['not_obtained']
        self.assertTrue(any(item['what'] == '这项小镇交办' and item['reason'] == 'unavailable'
                            for item in gaps))
        self.assertFalse(result['visibility']['complete'])
        # 原始字段清单：只给名字，不透出内容。
        self.assertIn('goal', result['delegation_fields'])
        self.assertEqual(receipt_fields(None), [])

    def test_receipt_fields_lists_names_only(self):
        envelope = dict(ref='x', receipt=dict(ok=False, reason='没做成', fields=[
            dict(field='venue', state='refused', reason='场地不在营业时间')]))
        fields = receipt_fields(envelope)
        self.assertIn('ok', fields)
        self.assertIn('fields[1]', fields)
        self.assertNotIn('场地不在营业时间', ' '.join(fields))
        self.assertEqual(row_fields({'goal': '私密目标', 'state': 'waiting'}), ['goal', 'state'])


if __name__ == '__main__':
    unittest.main()
