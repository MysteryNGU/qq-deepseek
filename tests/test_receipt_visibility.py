"""回执必须说清来源，并把「没看见」的部分露出来。

她 2026-11-20 21:35 的原话：dsh / 子 agent 只有「它说 completed」，看得见路径却看不见
的图片。这份测试按那份口径写：

* 三类来源分开（direct_observation / tool_report / model_account），不许混写；
* 截断、跳过、未送达、没有图像，逐项写进回执并给分层原因，不能只留一个 status；
* dsh 给真实 finish_reason / session_id 与 turn-end 里的原因；
* web.search 把各引擎原始结果与覆盖范围交回给她，不因标题没命中就整批报错；
* command 让她看到真正执行的脚本（A′），不只是她写的那一行。
* t20 的跨任务接口：缺口文本优先给具体原因、phone/browser 走同一个出口、
  浏览器截图的云端可读性、B站缺原因时不凭空判不可用。
"""
import base64
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from life.companion_work import (MAX_OUTPUT, MISSING_REASONS, PROVENANCE_DIRECT,
                                 PROVENANCE_KINDS, PROVENANCE_MODEL, PROVENANCE_TOOL,
                                 SkippedFile, Work, missing, provenance, visibility)

# IsolatedAsyncioTestCase 默认打开 asyncio 的 debug：真起一个 shell 时它会把
# 「慢回调」警告写到 stderr，而 PowerShell 会把 stderr 里的东西当失败。这里只关掉
# 这条噪音，测试本身不改任何行为。
logging.getLogger('asyncio').setLevel(logging.CRITICAL)


# ------------------------------------------------------------------ 来源分级

class ProvenanceVocabularyTests(unittest.TestCase):
    def test_three_sources_are_kept_apart(self):
        direct = provenance(PROVENANCE_DIRECT)
        tool = provenance(PROVENANCE_TOOL)
        model = provenance(PROVENANCE_MODEL)
        self.assertEqual(direct['provenance'], 'direct_observation')
        self.assertEqual(tool['provenance'], 'tool_report')
        self.assertEqual(model['provenance'], 'model_account')
        self.assertEqual(len(PROVENANCE_KINDS), 3)
        # 另一个模型说的话必须标明「它说做完了 != 现实已改变」。
        self.assertIn('不等于', model['provenance_note'])
        for receipt in (direct, tool, model):
            self.assertTrue(receipt['provenance_note'])

    def test_unknown_source_is_refused_instead_of_guessed(self):
        with self.assertRaises(ValueError):
            provenance('it_said_completed')
        with self.assertRaises(ValueError):
            provenance(PROVENANCE_DIRECT, parts={'output': 'model'})

    def test_parts_label_mixed_content_instead_of_hiding_it(self):
        receipt = provenance(PROVENANCE_MODEL, parts={'child_account': PROVENANCE_MODEL,
                                                     'artifacts': PROVENANCE_TOOL})
        self.assertEqual(receipt['provenance_parts']['child_account'], PROVENANCE_MODEL)
        self.assertEqual(receipt['provenance_parts']['artifacts'], PROVENANCE_TOOL)

    def test_missing_things_carry_layered_reasons(self):
        item = missing('图片', 'unauthorized', '没有截屏权限')
        self.assertEqual(item['reason'], 'unauthorized')
        self.assertEqual(item['reason_text'], '没有授权')
        with self.assertRaises(ValueError):
            missing('图片', '不知道')
        block = visibility(not_obtained=[item],
                           truncated=[dict(what='输出', reason='limit',
                                           reason_text='超出上限', detail='只带回前 100 字')])
        self.assertFalse(block['complete'])
        self.assertIn('没取得', block['summary'])
        self.assertIn('截断', block['summary'])
        self.assertTrue(visibility()['complete'])


# ------------------------------------------------------------------ work.read / grep

class WorkVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.root = self.home / 'ws'
        self.root.mkdir()
        self.work = Work(None, self.home, root=self.root)

    def _write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def test_read_counts_character_truncation_not_only_lines(self):
        # 每行 100 字、400 行：行没被切，但拼出来的正文超过 MAX_OUTPUT。
        self._write('big.txt', '\n'.join('x' * 100 for _ in range(400)))
        result = self.work.execute(dict(op='read', path='big.txt'))
        self.assertTrue(result['chars_truncated'], result)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['output']), MAX_OUTPUT)
        self.assertEqual(result['chars_shown'], MAX_OUTPUT)
        self.assertGreater(result['chars_total'], MAX_OUTPUT)
        self.assertTrue(result['visibility']['truncated'])
        self.assertEqual(result['visibility']['truncated'][0]['reason'], 'limit')
        self.assertEqual(result['provenance'], PROVENANCE_DIRECT)

    def test_read_reports_remaining_lines_with_a_next_offset(self):
        self._write('small.txt', '\n'.join('line %d' % i for i in range(20)))
        result = self.work.execute(dict(op='read', path='small.txt', limit=5))
        self.assertTrue(result['lines_truncated'])
        self.assertTrue(result['truncated'])
        gap = [item for item in result['visibility']['truncated']
               if item['what'] == '后面的行'][0]
        self.assertEqual(gap['next_offset'], 6)
        self.assertEqual(gap['total_lines'], 20)

    def test_grep_lists_skipped_files_and_does_not_count_them_as_scanned(self):
        self._write('small.txt', 'one\nneedle here\n')
        self._write('unreadable.txt', 'needle but unreadable\n')
        (self.root / 'huge.txt').write_text('needle\n' + 'x' * 2_100_000, encoding='utf-8')

        class Stubborn(Work):
            def _read_for_grep(self, path):
                if path.name == 'unreadable.txt':
                    raise SkippedFile('failed', 'read_error', 'Permission denied (test)')
                return super()._read_for_grep(path)

        result = Stubborn(None, self.home, root=self.root).execute(
            dict(op='grep', pattern='needle'))
        self.assertEqual(result['count'], 1, result['output'])
        self.assertEqual(result['scanned'], 1, '跳过的文件不许算进 scanned')
        reasons = {item['path']: item for item in result['skipped_files']}
        self.assertEqual(reasons['huge.txt']['reason'], 'limit')
        self.assertEqual(reasons['huge.txt']['code'], 'too_large')
        self.assertIn('2100', reasons['huge.txt']['detail'].replace(' ', ''))
        self.assertEqual(reasons['unreadable.txt']['reason'], 'failed')
        self.assertEqual(reasons['unreadable.txt']['code'], 'read_error')
        self.assertEqual(len(result['visibility']['skipped']), 2)

    def test_work_error_is_tool_report_not_file_content(self):
        with self.assertRaises(Exception):
            self.work.execute(dict(op='read', path='missing.txt'))


# ------------------------------------------------------------------ skill.save

class SkillVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.home = Path(tempfile.mkdtemp())
        from life.companion_skills import MAX_BODY, SkillLibrary
        self.limit = MAX_BODY
        self.library = SkillLibrary(self.home)

    async def test_save_over_limit_says_truncated_and_real_counts(self):
        body = '# 长方法\n' + 'x' * (self.limit + 500)
        receipt = await self.library.run(dict(
            op='save', arguments=json.dumps(dict(name='demo', text=body), ensure_ascii=False)))
        self.assertTrue(receipt['truncated'])
        self.assertEqual(receipt['chars'], self.limit)
        self.assertEqual(receipt['chars_total'], len(body))
        self.assertIn('没有存进去', receipt['summary'])
        self.assertEqual(receipt['provenance'], PROVENANCE_TOOL)
        self.assertEqual(receipt['visibility']['truncated'][0]['reason'], 'limit')
        stored = (self.home / 'skills' / 'demo.md').read_text('utf-8')
        self.assertEqual(len(stored), self.limit)

    async def test_read_returns_file_text_and_keeps_the_gap_visible(self):
        # 直接写一份超限的方法文件（save 会把它存成上限长度，读回来就看不到截断了）。
        body = '# 方法\n步骤一\n' + 'y' * (self.limit + 10)
        (self.home / 'skills').mkdir(parents=True, exist_ok=True)
        (self.home / 'skills' / 'demo2.md').write_text(body, encoding='utf-8')
        receipt = await self.library.run(dict(
            op='read', arguments=json.dumps(dict(name='demo2'))))
        self.assertEqual(receipt['provenance'], PROVENANCE_DIRECT)
        self.assertTrue(receipt['truncated'])
        self.assertTrue(receipt['visibility']['truncated'])


# ------------------------------------------------------------------ web.search

BAIDU_HTML = ('<h3><a href="https://example.com/1">钓鱼 入门 技巧</a></h3><p>摘要一</p>'
              '<h3><a href="https://example.com/2">完全无关的游戏论坛</a></h3><p>摘要二</p>'
              '<h3><a href="/s?wd=x">相关搜索</a></h3>'
              '<h3><a href="https://www.baidu.com/internal">百度自己的页面</a></h3>')


class WebSearchCoverageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def _reader(self, fetch, search=None):
        from life.companion_web import WebReader
        return WebReader(SimpleNamespace(home=self.home), fetch=fetch, search=search)

    async def test_irrelevant_titles_do_not_fail_the_whole_batch(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if 'so.com' in url:
                raise ConnectionError('so360 连不上')
            return BAIDU_HTML

        reader = self._reader(fetch)
        result = await reader.run(dict(kind='web', op='search',
                                       arguments=json.dumps(dict(query='钓鱼 技巧'))))
        self.assertEqual(result['status'], 'completed', result)
        titles = [item['title'] for item in result['results']]
        # 没命中查询词的条目也不许丢：相关性交给她判断。
        self.assertIn('完全无关的游戏论坛', titles)
        relevant = {item['title']: item['relevant'] for item in result['results']}
        self.assertTrue(relevant['钓鱼 入门 技巧'])
        self.assertFalse(relevant['完全无关的游戏论坛'])
        coverage = result['coverage']
        engines = {row['engine']: row for row in coverage['engines']}
        self.assertEqual(engines['baidu']['status'], 'ok')
        self.assertEqual(engines['baidu']['filtered']['nav'], 1)
        self.assertEqual(engines['baidu']['filtered']['self_link'], 1)
        self.assertEqual(engines['so360']['status'], 'failed')
        self.assertIn('连不上', engines['so360']['error'])
        self.assertEqual((coverage['succeeded'], coverage['failed']), (1, 1))
        self.assertEqual(result['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(result['provenance_parts']['coverage'], PROVENANCE_TOOL)
        stored = json.loads(Path(result['source_path']).read_text('utf-8'))
        self.assertEqual(stored['coverage']['failed'], 1)
        self.assertEqual(len(stored['results']), 2)

    async def test_only_irrelevant_results_is_still_a_completed_receipt(self):
        html = '<h3><a href="https://example.com/x">与查询毫无关系</a></h3>'
        result = await self._reader(lambda url: html).run(
            dict(kind='web', op='search', arguments=json.dumps(dict(query='深海 热泉'))))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['relevant_count'], 0)
        self.assertIn('相关性交给你判断', result['note'])

    async def test_filtered_entries_and_steps_are_in_the_artifact(self):
        reader = self._reader(lambda url: BAIDU_HTML)
        result = await reader.run(dict(kind='web', op='search',
                                       arguments=json.dumps(dict(query='钓鱼 技巧'))))
        self.assertEqual(result['status'], 'completed')
        # 两个引擎返回了同一批 URL（同一份 HTML），去重了 2 条；这两步都写进回执。
        self.assertIn('跨引擎同 URL 去重 2 条', result['note'])
        self.assertIn('排序', result['note'])
        stored = json.loads(Path(result['source_path']).read_text('utf-8'))
        self.assertIn('steps', stored)
        entries = {(item['reason'], item['title'], item['url'])
                   for item in stored['filtered_entries']}
        self.assertIn(('nav', '相关搜索', 'https://www.baidu.com/s?wd=x'), entries)
        self.assertIn(('self_link', '百度自己的页面', 'https://www.baidu.com/internal'), entries)
        self.assertEqual(stored['coverage']['duplicates'], 2)
        engines = {row['engine']: row for row in stored['engines']}
        self.assertEqual(engines['baidu']['dropped'][0]['reason'], 'nav')

    async def test_cross_engine_duplicate_urls_are_counted_and_kept_in_artifact(self):
        def fetch(url):
            return '<h3><a href="https://example.com/dup">钓鱼 技巧</a></h3>'

        reader = self._reader(fetch)
        result = await reader.run(dict(kind='web', op='search',
                                       arguments=json.dumps(dict(query='钓鱼'))))
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['coverage']['duplicates'], 1)
        self.assertIn('跨引擎同 URL 去重 1 条', result['note'])
        stored = json.loads(Path(result['source_path']).read_text('utf-8'))
        self.assertEqual(stored['duplicates'][0]['url'], 'https://example.com/dup')

    async def test_open_failure_is_layered_not_opaque(self):
        def fetch(url):
            raise TimeoutError('read timeout')

        result = await self._reader(fetch).run(dict(
            kind='web', op='open', arguments=json.dumps(dict(url='https://example.com/x'))))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        gap = result['visibility']['not_obtained'][0]
        self.assertEqual(gap['reason'], 'unavailable')
        self.assertIn('timeout', gap['detail'].lower())

    async def test_page_without_text_says_no_body_was_obtained(self):
        html = '<html><head><title>空壳</title></head><body></body></html>'
        result = await self._reader(lambda url: html).run(dict(
            kind='web', op='open', arguments=json.dumps(dict(url='https://example.com/y'))))
        self.assertEqual(result['status'], 'completed')
        gap = result['visibility']['not_obtained'][0]
        self.assertEqual(gap['what'], '页面正文')
        self.assertEqual(gap['reason'], 'unavailable')

    async def test_every_engine_failing_is_reported_with_layered_reasons(self):
        def fetch(url):
            raise TimeoutError('connect timeout')

        result = await self._reader(fetch).run(
            dict(kind='web', op='search', arguments=json.dumps(dict(query='钓鱼'))))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['coverage']['failed'], 2)
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        self.assertTrue(result['visibility']['not_obtained'])
        self.assertEqual(result['visibility']['not_obtained'][0]['reason'], 'failed')


# ------------------------------------------------------------------ dsh 真实原因

class _FakeResult:
    def __init__(self, finish_reason, session_id='sess-1', text='done', events=None):
        self.finish_reason = finish_reason
        self.session_id = session_id
        self.final_response = text
        self.events = events or []


class _FakeHarness:
    def __init__(self, config, _launch_args=None):
        self.config = config

    def run(self, goal):
        return _FakeHarness.RESULT

    def start(self):
        return None

    def close(self):
        return None


class _FakeConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class DshWorkerReceiptTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.request = dict(dsh_root=str(self.home), home=str(self.home), cwd=str(self.home),
                            node=sys.executable, entry='lib/bin.js', model='m', provider='p',
                            goal='做点事', result_path=str(self.home / 'r.json'))

    def _run(self):
        from life.companion_dsh_worker import execute
        with contextlib.redirect_stdout(io.StringIO()):
            execute(self.request, harness_class=_FakeHarness, config_class=_FakeConfig)

    def _payload(self):
        return json.loads((self.home / 'r.json').read_text('utf-8'))

    def test_failure_keeps_finish_reason_session_and_turn_reason(self):
        _FakeHarness.RESULT = _FakeResult('error', events=[
            dict(type='turn/end', data=dict(reason=dict(
                kind='error', error=dict(message='Authentication Fails',
                                         code='AUTH', status=401))))])
        with self.assertRaises(RuntimeError) as caught:
            self._run()
        self.assertIn('Authentication Fails', str(caught.exception))
        payload = self._payload()
        self.assertEqual(payload['status'], 'failed')
        self.assertEqual(payload['finish_reason'], 'error')
        self.assertEqual(payload['session_id'], 'sess-1')
        self.assertIn('Authentication Fails', payload['turn_reason'])
        self.assertIn('AUTH', payload['turn_reason'])
        self.assertEqual(payload['provenance'], PROVENANCE_MODEL)
        self.assertEqual(payload['provenance_parts']['finish_reason'], PROVENANCE_TOOL)
        self.assertEqual(payload['provenance_parts']['output'], PROVENANCE_MODEL)

    def test_success_writes_real_finish_reason_and_session(self):
        _FakeHarness.RESULT = _FakeResult('completed')
        self._run()
        payload = self._payload()
        self.assertEqual(payload['status'], 'completed')
        self.assertEqual(payload['finish_reason'], 'completed')
        self.assertEqual(payload['session_id'], 'sess-1')
        self.assertEqual(payload['turn_reason'], '')

    def test_worker_crash_is_still_recorded_before_raising(self):
        class Boom:
            def __init__(self, config, _launch_args=None):
                pass

            def run(self, goal):
                raise RuntimeError('SDK 起不来 boom')

            def close(self):
                pass

        from life.companion_dsh_worker import execute
        with self.assertRaises(RuntimeError):
            with contextlib.redirect_stdout(io.StringIO()):
                execute(self.request, harness_class=Boom, config_class=_FakeConfig)
        payload = self._payload()
        self.assertEqual(payload['status'], 'failed')
        self.assertEqual(payload['error_type'], 'RuntimeError')
        self.assertIn('boom', payload['error_text'])
        self.assertTrue(payload['turn_reason'])


# ------------------------------------------------------------------ command 与没看见

class ExecutorReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from life.companion_executor import ActionExecutor
        self.home = Path(tempfile.mkdtemp())
        cfg = SimpleNamespace(dsh_root='', dsh_node='', dsh_home='', model='m', api_key='',
                              dsh_provider='p', work_root=str(self.home), bili_plugin='')
        self.executor = ActionExecutor(cfg, self.home)

    async def asyncTearDown(self):
        await self.executor.close()

    async def test_command_receipt_shows_the_script_that_actually_ran(self):
        if os.name != 'nt':
            self.skipTest('Windows 包装脚本才带 ErrorActionPreference/exit')
        result = await self.executor.run(dict(kind='command', command='Write-Output "hello"'))
        self.assertEqual(result['return_code'], 0, result)
        self.assertEqual(result['status'], 'completed')
        self.assertIn('hello', result['output'])
        script = result['executed_script']
        self.assertIn("$ErrorActionPreference = 'Stop'", script)
        self.assertIn('Write-Output "hello"', script)
        self.assertIn('exit $LASTEXITCODE', script)
        self.assertEqual(result['command'], 'Write-Output "hello"')
        self.assertEqual(result['provenance'], PROVENANCE_DIRECT)
        path = Path(result['executed_script_path'])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text('utf-8'), script)
        self.assertTrue(any('Stop' in item for item in result['wrapper']['prefixes']))
        self.assertTrue(result['wrapper']['appended'])
        self.assertIn('-EncodedCommand', result['wrapper']['transport'])

    async def test_long_command_output_is_marked_truncated(self):
        if os.name != 'nt':
            self.skipTest('这条用 PowerShell 生成超长输出')
        result = await self.executor.run(
            dict(kind='command', command="Write-Output ([string]::new('x',20000))"))
        self.assertTrue(result['truncated'])
        self.assertGreater(result['output_bytes'], 12000)
        self.assertEqual(result['visibility']['truncated'][0]['what'], '命令输出')
        self.assertEqual(result['visibility']['truncated'][0]['reason'], 'limit')

    async def test_screenshot_without_image_says_which_layer_is_missing(self):
        # 没有画面：回执正文只是工具自己的报告，不冒充她看到的屏幕。
        result = self.executor._annotate('computer', dict(op='screenshot'),
                                         dict(status='completed', output='{"ok":true}'))
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        # parts 只列真取到的项：没截到图就不能把 screenshot/ui_tree 标成直接观察。
        self.assertNotIn('screenshot', result['provenance_parts'])
        self.assertNotIn('ui_tree', result['provenance_parts'])
        gaps = {item['what']: item for item in result['not_obtained']}
        self.assertEqual(gaps['屏幕画面']['reason'], 'failed')
        self.assertFalse(result['visibility']['complete'])
        click = self.executor._annotate('computer', dict(op='click'),
                                        dict(status='completed', output='{}'))
        self.assertEqual(click['provenance'], PROVENANCE_TOOL)
        self.assertNotIn('screenshot', click['provenance_parts'])
        self.assertEqual(click['not_obtained'][0]['reason'], 'not_requested')

    async def test_screenshot_with_a_readable_path_is_direct_observation(self):
        shot = self.home / 'shot.png'
        shot.write_bytes(b'\x89PNG\r\n\x1a\npixels')
        result = self.executor._annotate('computer', dict(op='screenshot'),
                                         dict(status='completed', image_path=str(shot)))
        self.assertEqual(result['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(result['provenance_parts']['screenshot'], PROVENANCE_DIRECT)
        self.assertTrue(result['image_readable'])
        self.assertFalse(result['not_obtained'])

    async def test_screenshot_path_that_cannot_be_read_is_not_a_picture(self):
        ghost = self.home / 'not-here.png'
        result = self.executor._annotate('computer', dict(op='screenshot'),
                                         dict(status='completed', image_path=str(ghost)))
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        self.assertFalse(result['image_readable'])
        self.assertNotIn('screenshot', result['provenance_parts'])
        gap = {item['what']: item for item in result['not_obtained']}['屏幕画面']
        self.assertEqual(gap['reason'], 'unavailable')
        self.assertIn(str(ghost), gap['detail'])
        self.assertFalse(result['visibility']['complete'])

    async def test_ui_tree_alone_is_direct_observation(self):
        result = self.executor._annotate(
            'computer', dict(op='windows'),
            dict(status='completed',
                 computer=dict(windows=[dict(handle='h1', title='记事本')])))
        self.assertEqual(result['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(result['provenance_parts']['ui_tree'], PROVENANCE_DIRECT)
        self.assertNotIn('screenshot', result['provenance_parts'])
        # 像素这次没取：缺口照旧写出来，但正文里的窗口树是直接观察。
        self.assertEqual(result['not_obtained'][0]['reason'], 'not_requested')

    async def test_bilibili_subtitle_only_is_a_tool_report(self):
        result = self.executor._annotate(
            'bilibili', dict(kind='bilibili'),
            dict(status='completed', output='只读到字幕',
                 visual=dict(observed=False, requested=False, reason='这次只读了字幕')))
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        # 没取得画面：parts 里不出现 visual=direct，嵌套的 visual 块也不再自称直接观察。
        self.assertNotIn('visual', result['provenance_parts'])
        self.assertEqual(result['visual']['provenance'], PROVENANCE_TOOL)
        self.assertEqual(result['provenance_parts']['subtitle'], PROVENANCE_TOOL)
        self.assertEqual(result['not_obtained'][0]['reason'], 'not_requested')

    async def test_bilibili_with_a_readable_sheet_is_direct_observation(self):
        sheet = self.home / 'sheet.png'
        sheet.write_bytes(b'\x89PNG\r\n\x1a\nframes')
        result = self.executor._annotate(
            'bilibili', dict(kind='bilibili'),
            dict(status='completed', output='画面+字幕',
                 visual=dict(observed=True, sheet_path=str(sheet))))
        self.assertEqual(result['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(result['provenance_parts']['visual'], PROVENANCE_DIRECT)
        self.assertEqual(result['visual']['provenance'], PROVENANCE_DIRECT)
        self.assertEqual(result['provenance_parts']['subtitle'], PROVENANCE_TOOL)

    async def test_bilibili_claiming_observed_without_a_readable_file_is_tool_report(self):
        result = self.executor._annotate(
            'bilibili', dict(kind='bilibili'),
            dict(status='completed', output='旧回执',
                 visual=dict(observed=True, sheet_path=str(self.home / 'gone.png'))))
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        self.assertNotIn('visual', result['provenance_parts'])
        self.assertEqual(result['visual']['provenance'], PROVENANCE_TOOL)
        gap = {item['what']: item for item in result['not_obtained']}['视频画面']
        self.assertEqual(gap['reason'], 'unavailable')
        self.assertIn('gone.png', gap['detail'])

    async def test_browser_image_part_is_direct_observation(self):
        shot = self.home / 'page.png'
        shot.write_bytes(b'\x89PNG\r\n\x1a\npage')
        with_image = self.executor._annotate(
            'browser', dict(kind='browser'),
            dict(status='completed', output='page loaded', images=[
                dict(saved=True, path=str(shot), mime='image/png')]))
        self.assertEqual(with_image['provenance_parts']['images'], PROVENANCE_DIRECT)
        self.assertEqual(with_image['provenance_parts']['text'], PROVENANCE_TOOL)
        text_only = self.executor._annotate(
            'browser', dict(kind='browser'),
            dict(status='completed', output='只读到文本', images=[]))
        self.assertNotIn('images', text_only['provenance_parts'])

    async def test_executed_script_truncation_is_visible(self):
        if os.name != 'nt':
            self.skipTest('这条用 PowerShell 包装脚本')
        filler = '# ' + 'x' * 7000
        result = await self.executor.run(
            dict(kind='command', command=filler + '\nWrite-Output "ok"'))
        self.assertEqual(result['status'], 'completed', result)
        self.assertLessEqual(len(result['executed_script']), 6000)
        self.assertGreater(result['executed_script_chars'], 6000)
        items = {item['what']: item for item in result['visibility']['truncated']}
        gap = items['实际执行的脚本']
        self.assertEqual(gap['shown_chars'], len(result['executed_script']))
        self.assertEqual(gap['total_chars'], result['executed_script_chars'])
        self.assertEqual(gap['executed_script_path'], result['executed_script_path'])
        full = Path(result['executed_script_path'])
        self.assertTrue(full.is_file())
        self.assertEqual(len(full.read_text('utf-8')), result['executed_script_chars'])
        self.assertIn('实际执行的脚本', result['visibility']['summary'])

    async def test_finished_agent_receipt_separates_child_words_from_artifacts(self):
        receipt = dict(status='completed', completion='finished', agent_id='a1',
                       output='它说做完了', message_id=None,
                       delivery='not_recorded:no_ledger', attempt=2,
                       artifacts=[dict(path='/tmp/a.md', sha256='abc', sha256_source='host')],
                       **provenance(PROVENANCE_MODEL, parts={'child_account': PROVENANCE_MODEL,
                                                             'artifacts': PROVENANCE_TOOL}))
        result = self.executor._annotate('agent', dict(op='inspect'), receipt)
        self.assertEqual(result['provenance'], PROVENANCE_MODEL)
        gaps = {item['what']: item for item in result['not_obtained']}
        self.assertEqual(gaps['子 agent 回执的送达']['reason'], 'unavailable')
        self.assertIn('台账', gaps['子 agent 回执的送达']['detail'])
        # 有产物路径时不再报「没有产物」。
        self.assertNotIn('子 agent 的产物', gaps)

    async def test_dsh_failure_lists_missing_facts(self):
        result = self.executor._annotate('dsh', dict(kind='dsh', goal='g'),
                                         dict(status='failed', failure_kind='no_session',
                                              output='没写出会话'))
        self.assertEqual(result['provenance'], PROVENANCE_MODEL)
        gaps = {item['what']: item for item in result['not_obtained']}
        self.assertIn('dsh 这一轮的结果', gaps)
        self.assertIn('session_id', gaps)
        self.assertIn('finish_reason', gaps)
        self.assertEqual(gaps['session_id']['reason'], 'unavailable')
        self.assertTrue(result['visibility']['summary'])

    async def test_real_executor_dsh_path_keeps_finish_reason_and_turn_reason(self):
        """走真的执行器 → 真的 worker 子进程 → 假的 SDK：回执里要有硬事实。"""
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
            "    finish_reason = 'error'\n"
            "    session_id = 'sess-fake-9'\n"
            '    events = [dict(type="turn/end", data=dict(reason=dict(kind="error", error=dict(\n'
            '        message="Authentication Fails, Your api key is invalid",\n'
            '        code="AUTH", status=401))))]\n'
            '\n'
            'class DeepSeekHarness:\n'
            '    def __init__(self, config, _launch_args=None):\n'
            '        self.config = config\n'
            '    def run(self, goal):\n'
            '        return _Result()\n'
            '    def close(self):\n'
            '        pass\n', encoding='utf-8')
        cfg = SimpleNamespace(dsh_root=str(root), dsh_node=sys.executable,
                              dsh_home=str(self.home / 'dsh-home'), model='m', api_key='',
                              dsh_provider='p', work_root=str(self.home), bili_plugin='')
        executor = ActionExecutor(cfg, self.home)
        try:
            result = await executor.run(dict(kind='dsh', goal='读一下今天发生的事'))
        finally:
            await executor.close()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['finish_reason'], 'error')
        self.assertEqual(result['session_id'], 'sess-fake-9')
        self.assertIn('Authentication Fails', result['turn_reason'])
        self.assertIn('AUTH', result['turn_reason'])
        self.assertEqual(result['provenance'], PROVENANCE_MODEL)
        self.assertEqual(result['provenance_parts']['finish_reason'], PROVENANCE_TOOL)
        self.assertIn('鉴权被拒', result['output'])
        gaps = {item['what'] for item in result['not_obtained']}
        self.assertIn('dsh 这一轮的结果', gaps)
        self.assertNotIn('session_id', gaps, '会话号拿到了就不该报没拿到')
        self.assertNotIn('finish_reason', gaps)

    async def test_work_error_receipt_is_a_tool_report(self):
        # work.read 失败时抛 WorkError；执行器把它变成回执，来源是「工具自己的报告」。
        result = await self.executor.run(dict(kind='work', op='read',
                                              arguments=json.dumps(dict(path='missing.txt'))))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'work_error')
        self.assertEqual(result['provenance'], PROVENANCE_TOOL)
        self.assertIn('不存在', result['output'])

    async def test_every_receipt_has_provenance_and_visibility(self):
        for kind, action, result in [
            ('memory', dict(kind='memory'), dict(status='completed', output='x')),
            ('ask', dict(kind='ask'), dict(status='completed', output='x')),
            ('bilibili', dict(kind='bilibili'), dict(status='completed', output='x',
                                                    visual=dict(observed=False,
                                                                error='login_required'))),
        ]:
            annotated = self.executor._annotate(kind, action, result)
            self.assertIn(annotated['provenance'], PROVENANCE_KINDS)
            self.assertIn('visibility', annotated)
            self.assertIn('summary', annotated['visibility'])
            if kind == 'bilibili':
                # 只有字幕、没有画面：这一条要自己说出来。
                self.assertEqual(annotated['not_obtained'][0]['reason'], 'unauthorized')


# ------------------------------------------------- t20: 跨任务接口对齐（四处）

class GapTextTests(unittest.TestCase):
    """(a) summary 必须展示具体原因，分类词只留在结构字段里。"""

    def test_summary_shows_the_specific_detail_not_only_the_category(self):
        block = visibility(not_obtained=[
            missing('截图', 'not_requested', 'workspace_open 不取画面')])
        # 她读的是 summary：这里必须有「哪一步没做」，不能只剩一个笼统分类。
        self.assertIn('workspace_open 不取画面', block['summary'])
        self.assertEqual(block['not_obtained'][0]['reason'], 'not_requested')
        # 分类仍然可读，但不替代具体原因。
        self.assertIn('这次没有要求它', block['summary'])

    def test_missing_detail_falls_back_to_the_category(self):
        block = visibility(not_obtained=[missing('页面截图', 'unavailable')])
        self.assertIn('不可用', block['summary'])
        self.assertNotIn('None', block['summary'])

    def test_code_is_shown_alongside_the_detail(self):
        block = visibility(not_obtained=[
            missing('截图', 'failed', '手机回了完成，但回传里没有画面',
                    code='phone_missing_image')])
        self.assertIn('手机回了完成，但回传里没有画面', block['summary'])
        self.assertIn('phone_missing_image', block['summary'])


class UnifiedExitTests(unittest.IsolatedAsyncioTestCase):
    """(b) phone/browser 走执行器的统一出口，模块里不再各写一套。"""

    def test_kind_provenance_covers_phone_and_browser(self):
        from life.companion_executor import _KIND_PROVENANCE
        self.assertEqual(_KIND_PROVENANCE['phone'], PROVENANCE_TOOL)
        self.assertEqual(_KIND_PROVENANCE['browser'], PROVENANCE_TOOL)

    def test_modules_no_longer_carry_their_own_labeler(self):
        from life.companion_browser_adapter import BrowserAdapter
        self.assertFalse(hasattr(BrowserAdapter, '_label'),
                         '浏览器模块不能再自己补一套 provenance/visibility')
        # 手机模块的 _label 在 out-of-scope 文件里（本轮不改），但它不再被信任：
        # RemoteExecutor 的旁路会再过一遍统一出口，见下面的用例。
        from life.companion_phone import PhoneControl
        self.assertTrue(callable(getattr(PhoneControl, 'run', None)))

    async def test_remote_executor_routes_bypass_kinds_through_the_shared_exit(self):
        from life.companion_remote import RemoteExecutor

        class _Phone:
            async def run(self, action):
                # 派发前就失败：没有任何标注，靠统一出口补齐。
                return dict(status='failed', code='phone_offline', output='手机当前没有活动连接')

        class _Server:
            mobile = SimpleNamespace(phone=_Phone())

            async def control(self, kind, action):
                return dict(status='failed', code='browser_offline', output='适配器没起来')

        class _Original:
            async def run(self, action):
                return dict(status='completed', output='原执行器负责的 kind')

        executor = RemoteExecutor(_Server(), _Original())
        phone = await executor.run(dict(kind='phone', op='info', arguments='{}'))
        self.assertEqual(phone['provenance'], PROVENANCE_TOOL)
        self.assertIn(('屏幕画面', 'unavailable'),
                      {(gap['what'], gap['reason']) for gap in phone['not_obtained']})
        self.assertIn('手机当前没有活动连接', phone['visibility']['summary'])

        browser = await executor.run(dict(kind='browser', op='call', arguments='{}'))
        self.assertEqual(browser['provenance'], PROVENANCE_TOOL)
        self.assertEqual(browser['not_obtained'][0]['reason'], 'failed')

        untouched = await executor.run(dict(kind='work', op='read'))
        self.assertEqual(untouched['output'], '原执行器负责的 kind')


class BrowserImageAdoptionTests(unittest.IsolatedAsyncioTestCase):
    """(c) 浏览器截图：判定标准是云端进程用这个路径能不能读到文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='t20-browser-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.desktop = self.root / 'desktop'
        self.desktop.mkdir()
        self.cloud = self.root / 'cloud'

    def _shot(self, name='shot.png', payload=b'\x89PNG\r\n\x1a\nreal-bytes'):
        path = self.desktop / name
        path.write_bytes(payload)
        return path

    def test_desktop_side_carries_the_bytes_and_marks_unreadable(self):
        from life.companion_remote import browser_image_blobs
        shot = self._shot()
        result = dict(images=[dict(saved=True, path=str(shot), mime='image/png'),
                              dict(saved=True, path=str(self.desktop / 'gone.png'))])
        blobs = browser_image_blobs(result)
        self.assertEqual(blobs[0]['index'], 0)
        self.assertEqual(base64.b64decode(blobs[0]['data']), shot.read_bytes())
        # 读不到的那张不静默丢掉：留一条 skipped，云端才能说清「有图但传不过来」。
        self.assertEqual(blobs[1]['skipped'], 'unreadable')
        self.assertNotIn('data', blobs[1])

    def test_oversized_screenshot_is_reported_not_silently_dropped(self):
        from life.companion_remote import adopt_remote_images
        shot = self._shot(name='huge.png', payload=b'x' * 1024)
        result = dict(status='completed', images=[dict(saved=True, path=str(shot),
                                                       mime='image/png')])
        result['_image_blobs'] = [dict(index=0, path=str(shot), skipped='too_large',
                                       bytes=40 * 1024 * 1024)]
        adopted = adopt_remote_images(self.cloud, result)
        item = adopted['images'][0]
        self.assertFalse(item['saved'])
        self.assertFalse(item['cloud_readable'])
        self.assertEqual(item['desktop_path'], str(shot))
        self.assertIn('太大', item['reason'])

    def test_cloud_adopts_the_bytes_into_a_readable_path(self):
        from life.companion_remote import adopt_remote_images, browser_image_blobs
        shot = self._shot()
        result = dict(status='completed', images=[dict(saved=True, path=str(shot),
                                                       mime='image/png')])
        result['_image_blobs'] = browser_image_blobs(result)
        adopted = adopt_remote_images(self.cloud, result)
        item = adopted['images'][0]
        self.assertNotEqual(item['path'], str(shot))
        self.assertTrue(Path(item['path']).is_file(), '云端必须拿得到文件')
        self.assertEqual(Path(item['path']).read_bytes(), shot.read_bytes())
        self.assertTrue(item['cloud_readable'])
        self.assertEqual(item['source_path'], str(shot))
        self.assertEqual(adopted['image_paths'], [item['path']])
        self.assertNotIn('_image_blobs', adopted)

    async def test_desktop_execute_attaches_image_bytes_for_browser(self):
        from life.companion_remote import RemoteServiceThread
        shot = self._shot()

        class _Browser:
            async def run(self, action):
                return dict(status='completed', output='截图已保存：%s' % shot,
                            images=[dict(saved=True, path=str(shot), mime='image/png')])

        class _WS:
            closed = False

            def __init__(self):
                self.sent = []

            async def send_json(self, payload):
                self.sent.append(payload)

        thread = RemoteServiceThread.__new__(RemoteServiceThread)
        thread.home = self.root
        thread.browser = _Browser()
        thread.computer = None
        thread.control = None
        thread.controls = {}
        thread.ws = _WS()
        await thread.execute(dict(kind='browser', args={}, id='call-1'))
        result = thread.ws.sent[0]['result']
        blobs = result.get('_image_blobs')
        self.assertTrue(blobs, '桌面端必须把浏览器截图字节回传，不能只给本机路径')
        self.assertEqual(base64.b64decode(blobs[0]['data']), shot.read_bytes())

    async def test_without_bytes_the_receipt_says_desktop_only(self):
        from life.companion_executor import annotate_receipt
        ghost = self.desktop / 'only-on-desktop.png'   # 桌面端路径，云端进程读不到
        result = dict(status='completed', output='page loaded\n截图已保存：%s' % ghost,
                      images=[dict(saved=True, path=str(ghost), mime='image/png')])
        annotated = annotate_receipt('browser', dict(kind='browser'), result)
        item = annotated['images'][0]
        self.assertFalse(item['saved'])
        self.assertFalse(item['cloud_readable'])
        self.assertEqual(item['desktop_path'], str(ghost))
        self.assertIn('云端读不到', annotated['output'])
        self.assertNotIn('截图已保存：', annotated['output'])
        gap = {entry['what']: entry for entry in annotated['not_obtained']}['页面截图']
        self.assertEqual(gap['reason'], 'unavailable')
        self.assertIn('桌面端', gap['detail'])

    async def test_readable_image_is_marked_cloud_readable(self):
        from life.companion_executor import annotate_receipt
        local = self.cloud / 'ok.png'
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b'\x89PNG\r\n\x1a\nok')
        annotated = annotate_receipt('browser', dict(kind='browser'),
                                     dict(status='completed', output='page loaded',
                                          images=[dict(saved=True, path=str(local),
                                                       mime='image/png')]))
        item = annotated['images'][0]
        self.assertTrue(item['saved'])
        self.assertTrue(item['cloud_readable'])
        self.assertIn('截图已保存（本进程可读）', annotated['output'])
        self.assertFalse(annotated['not_obtained'])


class BilibiliGapTests(unittest.TestCase):
    """(d) visual 没给原因时，不许凭空判 unavailable。"""

    def test_no_reason_is_unspecified_not_unavailable(self):
        from life.companion_executor import annotate_receipt
        from life.companion_work import MISSING_REASONS
        result = annotate_receipt('bilibili', dict(kind='bilibili'),
                                  dict(status='completed', output='只读到字幕',
                                       visual=dict(observed=False, requested=True)))
        gap = {entry['what']: entry for entry in result['not_obtained']}['视频画面']
        self.assertEqual(gap['reason'], 'unspecified')
        self.assertIn('unspecified', MISSING_REASONS)
        self.assertIn('没有给出原因', gap['reason_text'])
        self.assertIn('不推断为不可用', gap['detail'])

    def test_reader_gap_is_trusted_over_a_guess(self):
        from life.companion_executor import annotate_receipt
        reader_gap = missing('视频画面', 'unauthorized', '需要登录才能看画面')
        result = annotate_receipt('bilibili', dict(kind='bilibili'),
                                  dict(status='completed', output='x',
                                       visual=dict(observed=False, requested=True),
                                       not_obtained=[reader_gap]))
        gaps = [entry for entry in result['not_obtained'] if entry['what'] == '视频画面']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['reason'], 'unauthorized')

    def test_explicit_failure_code_still_maps_to_a_layer(self):
        from life.companion_executor import annotate_receipt
        result = annotate_receipt('bilibili', dict(kind='bilibili'),
                                  dict(status='completed', output='x',
                                       visual=dict(observed=False, error='login_required')))
        self.assertEqual(result['not_obtained'][0]['reason'], 'unauthorized')


# ------------------------------------------------- t22: 失败不得自称「没有缺口」

class ReceiptStateTests(unittest.IsolatedAsyncioTestCase):
    """三种状态互斥且可辨认：成功完整 / 成功截断 / 失败。

    失败的回执以前会留下 `visibility.complete=true` 和「没有被截断、跳过或没取得的
    内容」，和她看到的 `status=failed` 自相矛盾（t14 观察项③）。
    """

    async def asyncSetUp(self):
        from life.companion_executor import ActionExecutor
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, self.home, True)
        cfg = SimpleNamespace(dsh_root='', dsh_node='', dsh_home='', model='m', api_key='',
                              dsh_provider='p', work_root=str(self.home), bili_plugin='')
        self.executor = ActionExecutor(cfg, self.home)

    async def asyncTearDown(self):
        await self.executor.close()

    async def _read(self, path):
        return await self.executor.run(dict(kind='work', op='read',
                                            arguments=json.dumps(dict(path=path))))

    async def test_success_without_gaps_is_complete(self):
        (self.home / 'small.txt').write_text('one\ntwo\n', encoding='utf-8')
        result = await self._read('small.txt')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['visibility']['state'], 'complete')
        self.assertTrue(result['visibility']['complete'])
        self.assertFalse(result['truncated'])
        self.assertEqual(result['not_obtained'], [])
        self.assertEqual(result['visibility']['summary'],
                         '这次没有被截断、跳过或没取得的内容。')

    async def test_success_but_truncated_stays_truncated_with_counts(self):
        (self.home / 'big.txt').write_text('\n'.join('x' * 100 for _ in range(400)),
                                           encoding='utf-8')
        result = await self._read('big.txt')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['visibility']['state'], 'truncated')
        self.assertFalse(result['visibility']['complete'])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['chars_shown'], MAX_OUTPUT)
        self.assertGreater(result['chars_total'], MAX_OUTPUT)
        gaps = result['visibility']['truncated']
        self.assertTrue(gaps)
        self.assertEqual(gaps[0]['what'], '输出字符')
        self.assertEqual(gaps[0]['shown_chars'], MAX_OUTPUT)
        self.assertIn('截断', result['visibility']['summary'])
        self.assertNotIn('没有被截断', result['visibility']['summary'])

    async def test_failure_says_what_is_missing_and_why(self):
        result = await self._read('missing.txt')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['visibility']['state'], 'failed')
        self.assertFalse(result['visibility']['complete'])
        # 摘要不能再读成「一切正常」。
        self.assertNotIn('没有被截断、跳过或没取得的内容', result['visibility']['summary'])
        self.assertIn('没取得', result['visibility']['summary'])
        gap = result['not_obtained'][0]
        self.assertEqual(gap['what'], '文件内容')
        self.assertEqual(gap['reason'], 'unavailable')   # 文件不存在
        self.assertIn('不存在', gap['detail'])
        self.assertIn('missing.txt', gap['detail'])
        self.assertIn(gap['reason'], MISSING_REASONS)

    def test_three_states_are_mutually_exclusive(self):
        states = {visibility()['state'],
                  visibility(truncated=[dict(what='输出', detail='切了')])['state'],
                  visibility(not_obtained=[missing('文件内容', 'unavailable', '不存在')],
                             failed=True)['state']}
        self.assertEqual(states, {'complete', 'truncated', 'failed'})
        for failed in (True, False):
            block = visibility(failed=failed)
            self.assertEqual(block['complete'], block['state'] == 'complete')

    def test_visibility_failed_without_any_gap_still_says_so(self):
        block = visibility(failed=True)
        self.assertEqual(block['state'], 'failed')
        self.assertFalse(block['complete'])
        self.assertIn('失败', block['summary'])
        self.assertNotIn('没有被截断、跳过或没取得的内容', block['summary'])

    def test_work_failure_reasons_are_layered(self):
        from life.companion_work import work_failure_gap
        cases = {
            '不存在：/x': 'unavailable',
            '读不到：Permission denied': 'failed',
            '路径超出工作区：/etc': 'unauthorized',
            '命中已经到上限（120 条）': 'limit',
            '正则无效：bad': 'unsupported',
        }
        for text, reason in cases.items():
            gap = work_failure_gap(dict(op='read', output=text, code='work_error'))
            self.assertEqual(gap['reason'], reason, text)
            self.assertIn(text, gap['detail'])
        # 拿不到原因也不编：记 failed，并照抄工具自己的话。
        self.assertEqual(work_failure_gap(dict(op='read', output=''))['reason'], 'failed')

    async def test_error_field_counts_as_failure_even_when_status_says_completed(self):
        result = self.executor._annotate('memory', dict(kind='memory'),
                                         dict(status='completed', output='x',
                                              error='台账打不开'))
        self.assertEqual(result['visibility']['state'], 'failed')
        self.assertFalse(result['visibility']['complete'])
        self.assertEqual(result['not_obtained'][0]['detail'], '台账打不开')

    def test_any_kind_with_a_failed_status_cannot_claim_no_gaps(self):
        from life.companion_executor import annotate_receipt
        kinds = ('memory', 'growth', 'ask', 'task', 'skill', 'work', 'command',
                 'web', 'computer', 'bilibili', 'dsh', 'browser')
        for kind in kinds:
            annotated = annotate_receipt(kind, dict(op=''), dict(status='failed', output=''))
            self.assertEqual(annotated['visibility']['state'], 'failed', kind)
            self.assertFalse(annotated['visibility']['complete'], kind)
            self.assertTrue(annotated['not_obtained'], kind)
            self.assertNotIn('没有被截断、跳过或没取得的内容',
                             annotated['visibility']['summary'], kind)

    def test_command_failure_names_the_nonzero_exit(self):
        from life.companion_executor import annotate_receipt
        result = annotate_receipt('command', dict(kind='command'),
                                  dict(status='failed', return_code=3, output='boom',
                                       executed_script='boom'))
        gap = {item['what']: item for item in result['not_obtained']}['命令的成功结果']
        self.assertEqual(gap['reason'], 'failed')
        self.assertIn('退出码 3', gap['detail'])
        self.assertIn('boom', gap['detail'])

    def test_nonzero_exit_is_a_failure_even_without_a_failed_status(self):
        from life.companion_executor import annotate_receipt
        result = annotate_receipt('command', dict(kind='command'),
                                  dict(status='completed', return_code=2,
                                       output='x', executed_script='x'))
        self.assertEqual(result['visibility']['state'], 'failed')
        self.assertFalse(result['visibility']['complete'])

    def test_agent_ledger_view_names_the_task_not_this_read(self):
        from life.companion_executor import annotate_receipt
        # inspect 一件失败的子任务：回执的 status 是那件任务的，不是这次读取的。
        result = annotate_receipt('agent', dict(op='inspect'),
                                  dict(status='failed', agent_id='a1', output='那件任务失败了'))
        gap = {item['what']: item for item in result['not_obtained']}['那件任务的结果']
        self.assertEqual(gap['reason'], 'failed')
        self.assertFalse(result['visibility']['complete'])
        # 没结束的（queued）不是失败，不该凭空加缺口。
        queued = annotate_receipt('agent', dict(op='create'),
                                  dict(status='queued', agent_id='a2', task_ref='t1'))
        self.assertNotEqual(queued['visibility']['state'], 'failed')
        self.assertFalse(queued['not_obtained'])

    def test_failure_receipt_is_never_complete(self):
        from life.companion_executor import failure_receipt
        result = failure_receipt('work', RuntimeError('boom'))
        self.assertEqual(result['visibility']['state'], 'failed')
        self.assertFalse(result['visibility']['complete'])
        self.assertEqual(result['not_obtained'][0]['reason'], 'failed')
        self.assertIn('boom', result['not_obtained'][0]['detail'])

    async def test_success_with_a_not_requested_gap_is_not_complete(self):
        result = self.executor._annotate('computer', dict(op='click'),
                                         dict(status='completed', output='{}'))
        self.assertEqual(result['visibility']['state'], 'truncated')
        self.assertFalse(result['visibility']['complete'])
        self.assertEqual(result['not_obtained'][0]['reason'], 'not_requested')


if __name__ == '__main__':
    unittest.main()
