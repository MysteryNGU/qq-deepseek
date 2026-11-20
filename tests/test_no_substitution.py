# -*- coding: utf-8 -*-
"""撤代判：项目相似只列候选、解析失败由系统署名、边界事实可查。

这三条守的是同一件事：**程序不许替她做判断，也不许借她的嘴说话**。
- 两件事可能指同一件事时，程序只列候选（各自 ref/goal/最近进展），合不合并由她定；
- 模型输出解析不出来时，用户看到的必须是系统署名的故障事实，不是她的台词；
- 预算/授权/静默/防重复拦下了一步，就等于「哪一步没有发生」——这要变成她能查的事实，
  而且只陈述规则、步骤与原因，不写「你不需要」「这不代表失败」这类结论。
"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from life import companion_batches as batches
from life import companion_delivery as ledger
from life.companion import Companion, result_view
from life.agent_session import SessionFailure


class _Model:
    MAX_OUTPUT_TOKENS = 8000

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        return dict(message=dict(content='{}'), finish_reason='completed', usage=None, model='fake')

    async def close(self):
        return None


class _FailingModel:
    MAX_OUTPUT_TOKENS = 8000

    async def complete(self, messages, json_mode=True, purpose='', schema=None):
        raise RuntimeError('network down')

    async def close(self):
        return None


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


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='no-substitution-')
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

    def core(self, **overrides):
        core = Companion(_cfg(self.home, **overrides), self.home, model=_Model())
        self._cores.append(core)
        batches.ensure(core.repo)
        ledger.ensure(core.repo)
        return core

    def source(self, core, text='hello', **payload):
        event, _ = core.life.ingest(core.scope, 'message', 'user_report', text, **payload)
        return event

    def activity(self, core, kind=None):
        clause = 'AND type=?' if kind else ''
        params = (core.life.agent_id, kind) if kind else (core.life.agent_id,)
        return [dict(row) for row in core.repo._all(
            'SELECT id,type,text,details,created FROM companion_activity WHERE agent=? %s '
            'ORDER BY created DESC' % clause, params)]

    def boundary_events(self, core):
        return [dict(row) for row in core.repo._all(
            'SELECT * FROM companion_batch_events WHERE source_type=? ORDER BY created_at DESC',
            ('boundary',))]

    def make_project(self, core, ref, goal, **fields):
        return core.tasks.ensure(core.scope, ref, goal, origin='self',
                                 source_event_ids=[], origin_source_event_ids=[], **fields)


# ------------------------------------------------------------------ 一、项目候选
class ProjectCandidateTest(_Base):
    def test_similar_project_is_only_listed_as_candidate(self):
        core = self.core()
        project = self.make_project(core, 'project-1', '修好投递回执看不到的问题',
                                    key='投递修复', status='active',
                                    next_step='先看日志，再补回执')
        source = self.source(core)
        decision = dict(projects=[dict(key='新的名字', goal='修好投递回执看不到的问题',
                                      next_step='先看日志，再补回执', status='active')],
                        actions=[], commitments=[])
        core._normalize_project_refs(decision, source)
        # 不改 key、不并回、不覆盖旧项目：新说法保持独立。
        self.assertEqual(decision['projects'][0]['key'], '新的名字')
        self.assertIsNone(core.tasks.get(core._project_ref('新的名字'), core.scope.scope_id))
        self.assertEqual(core.tasks.get(project['ref'], core.scope.scope_id)['goal'],
                         '修好投递回执看不到的问题')
        # 候选清单带各自 ref/goal/最近进展，写进 meta 与审计。
        saved = core._state('project_merge_candidates') or []
        self.assertEqual(len(saved), 1)
        row = saved[0]['candidates'][0]
        self.assertEqual(row['ref'], project['ref'])
        self.assertEqual(row['goal'], '修好投递回执看不到的问题')
        self.assertEqual(row['recent'], '先看日志，再补回执')
        self.assertEqual(row['match'], 'goal')
        audit = self.activity(core, 'activity')
        self.assertTrue(any('只列了候选，没有合并' in item['text'] for item in audit))
        self.assertFalse(any('已并回原项目' in item['text'] for item in audit))

    def test_explicit_project_ref_is_the_only_merge(self):
        core = self.core()
        project = self.make_project(core, 'project-2', '把小镇的对话记下来',
                                    key='小镇记录', status='active')
        decision = dict(projects=[dict(key='随便起的名字', goal='小镇记录',
                                      status='active', project_ref=project['ref'])],
                        actions=[dict(kind='command', project='随便起的名字', command='ls')])
        core._normalize_project_refs(decision, None)
        self.assertEqual(decision['projects'][0]['key'], '小镇记录')
        self.assertEqual(decision['actions'][0]['project'], '小镇记录')
        self.assertEqual(core._state('project_merge_candidates'), None)

    def test_promise_similarity_only_lists_candidates_and_keeps_twin_untouched(self):
        """t28/t18：承诺跨项目命中相似性时只列候选，自己建项目，不改 twin 的 next_step/status。"""
        core = self.core()
        twin = self.make_project(core, 'project-8', '把投递回执补成她能看到的样子',
                                 key='投递回执', status='active', next_step='先看日志')
        source = self.source(core)
        before = dict(core.tasks.get(twin['ref'], core.scope.scope_id))
        decision = dict(projects=[], actions=[], commitments=[dict(
            topic='自己盯回执', promise='把投递回执补成她能看到的样子',
            next_step='把日志整理成一条时间线', status='active')])
        route = dict(input_source=dict(conversation_id='qq:private:1'))
        recorded = core._record_commitments(source, decision, route)
        # 承诺按自己的 topic 建项目，没有并回 twin。
        self.assertEqual(recorded, ['自己盯回执'])
        own = core.tasks.get(core._project_ref('自己盯回执'), core.scope.scope_id)
        self.assertIsNotNone(own)
        self.assertEqual(own['goal'], '把投递回执补成她能看到的样子')
        self.assertEqual(own['next_step'], '把日志整理成一条时间线')
        after = dict(core.tasks.get(twin['ref'], core.scope.scope_id))
        self.assertEqual(after['next_step'], before['next_step'])
        self.assertEqual(after['status'], before['status'])
        # 没有「已并回原项目」这种替她合并的审计；候选写进 meta 并标明来源。
        texts = [item['text'] for item in self.activity(core, 'activity')]
        self.assertFalse(any('已并回原项目' in text for text in texts))
        self.assertTrue(any('只列了候选，没有合并' in text for text in texts))
        saved = core._state('project_merge_candidates') or []
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['origin'], 'commitment')
        self.assertEqual(saved[0]['proposed_key'], '自己盯回执')
        self.assertEqual([row['ref'] for row in saved[0]['candidates']], [twin['ref']])
        # 下一轮材料里能看到这条候选（ref/goal/最近进展）。
        messages = core._messages(None, '看看手上的事', None, source_ref=source.id,
                                  purpose='activity', wake_kind='timer', attention=[])
        material = messages[1]['content']
        self.assertIn('project_merge_candidates', material)
        self.assertIn('自己盯回执', material)

    def test_same_round_promise_wording_still_reuses_that_project(self):
        """同一批输入里的重复表述：仍然并回本轮项目（不是跨项目猜意图）。"""
        core = self.core()
        source = self.source(core)
        decision = dict(projects=[dict(key='本轮项目', goal='把回执整理成时间线', status='active',
                                       next_step='按天分组')],
                        actions=[], commitments=[dict(topic='新说法', promise='把回执整理成时间线',
                                                     next_step='按天分组', status='active')])
        route = dict(input_source=dict(conversation_id='qq:private:1'))
        recorded = core._record_commitments(source, decision, route)
        self.assertEqual(recorded, ['本轮项目'])
        self.assertIsNone(core.tasks.get(core._project_ref('新说法'), core.scope.scope_id))
        self.assertTrue(any('已并回本轮项目' in item['text']
                            for item in self.activity(core, 'activity')))

    def test_candidate_reaches_next_turn_material(self):
        core = self.core()
        self.make_project(core, 'project-3', '整理昨天的失败报告', key='失败报告',
                          status='active', next_step='按天分组')
        source = self.source(core)
        unknown = dict(projects=[dict(key='report-2', goal='整理昨天的失败报告',
                                      next_step='按天分组', status='active')],
                       reply='', followups=[], remember=[], actions=[], wake_after=0)
        core._normalize_project_refs(unknown, source)
        messages = core._messages(None, '看看手上的事', None, source_ref=source.id,
                                  purpose='activity', wake_kind='timer', attention=[])
        material = messages[1]['content']
        self.assertIn('project_merge_candidates', material)
        self.assertIn('这只是候选', material)
        self.assertIn('程序不会替你合并', material)


# ---------------------------------------------------------- 二、解析失败系统署名
class _ParseError(Exception):
    def __init__(self, message='Expecting value', *, truncated=False, salvaged_reply=None):
        super().__init__(message)
        self.truncated = truncated
        if salvaged_reply is not None:
            self.salvaged_reply = salvaged_reply


class ParseFailureTest(_Base):
    def test_unparsable_output_is_system_signed_not_her_voice(self):
        core = self.core()
        source = self.source(core)
        # 连一段能当回复的正文都取不到（空返回）：这才是原来的「刚才那下没接上」那条路。
        result = dict(message=dict(content='   '))
        decision = core._salvage_reply(result, 'dialogue', source, _ParseError(), attempts=1)
        reply = decision['reply']
        # 系统署名，不是她的台词。
        self.assertTrue(reply.startswith('〔系统〕'), reply)
        self.assertNotIn('刚才那下没接上', reply)
        self.assertNotIn('您再说一遍', reply)
        self.assertIn('没有执行任何动作', reply)
        self.assertIn('可以重试', reply)
        notice = core._system_notice
        self.assertEqual(notice['author'], 'system')
        self.assertEqual(notice['rule'], 'parse_failed')
        self.assertIn('没有执行任何动作', notice['not_done'])
        # 审计与边界事实都留下来了（下一轮她能看到）。
        self.assertTrue(any(item['type'] == 'boundary' and '无法解析' in item['text']
                            for item in self.activity(core)))
        events = self.boundary_events(core)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['source_type'], 'boundary')
        self.assertIn('没有发生', events[0]['detail'])
        self.assertEqual(json.loads(events[0]['payload'])['boundary']['rule'], 'parse_failed')
        self.assertEqual(decision.get('actions'), [])
        self.assertEqual(decision.get('projects'), [])

    def test_truncated_fallback_is_system_signed_too(self):
        core = self.core()
        source = self.source(core)
        result = dict(message=dict(content=''))
        decision = core._salvage_reply(result, 'dialogue', source,
                                       _ParseError('truncated', truncated=True), attempts=1)
        reply = decision['reply']
        self.assertTrue(reply.startswith('〔系统〕'), reply)
        self.assertIn('截断', reply)
        # 以前是她第一人称的台词；现在不能出现。
        self.assertNotIn('我光想', reply)
        self.assertNotIn('我就能答上', reply)
        self.assertEqual(core._system_notice['retry'][:2], '可以')

    def test_real_sentence_instead_of_json_stays_her_own_words(self):
        """模型真的写了一句人话（不是 JSON）时，那句话就是她说的，不该被改成系统通知。"""
        core = self.core()
        source = self.source(core)
        result = dict(message=dict(content='今天风有点大，我把窗关上了。'))
        decision = core._salvage_reply(result, 'dialogue', source, _ParseError(), attempts=1)
        self.assertEqual(decision['reply'], '今天风有点大，我把窗关上了。')
        self.assertFalse(hasattr(core, '_system_notice') and core._system_notice)

    def test_system_notice_is_marked_in_the_delivered_record(self):
        core = self.core()
        bubble = dict(text='〔系统〕这一轮模型的输出没有解析成功。', turn_id='turn-1', part=0, parts=1,
                      images=[], delivery=dict(targets=['all']), system_notice=True)
        stored = core._store_bubble(bubble)
        payload = json.loads(core.repo._one('SELECT payload FROM events WHERE id=?',
                                            (stored['id'],))['payload'])
        self.assertTrue(payload.get('system_notice'))
        self.assertTrue(payload['source'].get('system_notice'))


# -------------------------------------------------------------- 三、边界事实
class BoundaryFactTest(_Base):
    def test_boundary_fact_shape_and_no_substituted_conclusion(self):
        core = self.core()
        fact = core._boundary_fact('budget', step_not_taken='DSH 调用没有执行',
                                   reason='strict 模式拿不到逐次账单', refs=['action-1'])
        self.assertEqual(fact['rule'], 'budget')
        self.assertEqual(fact['attribution'], 'boundary')
        self.assertTrue(fact['effective'])
        self.assertEqual(fact['step_not_taken'], 'DSH 调用没有执行')
        self.assertEqual(fact['refs'], ['action-1'])
        blob = json.dumps(fact, ensure_ascii=False)
        for forbidden in ('你不需要', '不必做', '这不代表失败', '不是你的问题'):
            self.assertNotIn(forbidden, blob)
        system = core._boundary_fact('model_request_failed', reason='timeout')
        self.assertEqual(system['attribution'], 'system')

    def test_boundary_rule_mapping_keeps_only_the_real_reason(self):
        self.assertEqual(Companion._boundary_rule_of('budget_unmetered', SessionFailure('x', 'y')), 'budget')
        self.assertEqual(Companion._boundary_rule_of('not_authorized', SessionFailure('x', 'y')),
                         'authorization')
        self.assertEqual(Companion._boundary_rule_of('', RuntimeError('boom')), 'executor_exception')

    def test_model_request_failure_is_signed_by_system(self):
        core = Companion(_cfg(self.home), self.home, model=_FailingModel())
        self._cores.append(core)
        batches.ensure(core.repo)
        ledger.ensure(core.repo)
        source = self.source(core)
        with self.assertRaises(RuntimeError):
            asyncio.run(core._call_model([dict(role='user', content='hi')], source, 'dialogue'))
        self.assertIn('系统：模型请求失败', [item['text'] for item in self.activity(core, 'boundary')])
        fact = json.loads(self.boundary_events(core)[0]['payload'])['boundary']
        self.assertEqual(fact['attribution'], 'system')
        self.assertIn('没有执行任何动作', fact['step_not_taken'])
        self.assertIn('network down', fact['reason'])

    def test_result_view_carries_the_boundary_fact(self):
        core = self.core()
        fact = core._boundary_fact('authorization', step_not_taken='这个动作没有执行',
                                   reason='没有该会话的授权')
        view = result_view(dict(status='failed', output='', boundary=[fact]))
        self.assertIn('边界事实', view)
        self.assertIn('授权边界生效', view)
        self.assertIn('这个动作没有执行', view)

    def test_quiet_hours_suppression_becomes_a_queryable_fact(self):
        now = time.localtime()
        start = now.tm_hour
        core = self.core(quiet_hours='%02d:00-%02d:00' % (start, (start + 1) % 24))
        self.assertTrue(core._in_quiet_hours())
        source, _ = core.life.ingest(core.scope, 'companion_wake', 'self_observation',
                                     '想跟你说说今天的事', payload=dict(wake_kind='timer'))
        decision = dict(reply='想跟你说说今天的事', followups=['还有一件小事'], remember=[],
                        projects=[], actions=[], delivery={}, wake_after=0)
        core._commit(source, decision)
        # 策略没变：静默时段仍然不主动发。
        self.assertEqual(core.repo._all('SELECT * FROM companion_deliveries'), [])
        self.assertFalse(any(item['kind'] == 'message' for item in core.repo._all(
            'SELECT kind FROM events WHERE agent_id=? AND source_kind=?',
            (core.life.agent_id, 'self_output'))))
        # 但被压了什么、为什么、压了几条，现在是她能查的事实。
        audit = [item for item in self.activity(core, 'boundary') if '静默时段生效' in item['text']]
        self.assertEqual(len(audit), 1)
        details = json.loads(audit[0]['details'])
        self.assertEqual(details['window'], core.cfg.quiet_hours)
        self.assertEqual(details['count'], 2)
        self.assertEqual(len(details['would_be_message_ids']), 2)
        self.assertIn('没有发送', details['step_not_taken'])
        events = self.boundary_events(core)
        self.assertEqual(len(events), 1)
        self.assertIn('静默时段生效', events[0]['title'])
        self.assertIn('没有发生', events[0]['detail'])
        payload = json.loads(events[0]['payload'])['boundary']
        self.assertEqual(payload['window'], core.cfg.quiet_hours)
        self.assertEqual(payload['count'], 2)
        # 下一轮真的能看到：同一条事实进入 4 级（状态变化）批次，detail 里写明哪一步没发生。
        next_source, _ = core.life.ingest(core.scope, 'companion_wake', 'self_observation',
                                          '下一轮', payload=dict(wake_kind='timer'))
        selected = core._prepare_batches(next_source, 'activity', None)
        rows = [event for batch in selected for event in batch['events']
                if event['source_type'] == 'boundary']
        self.assertEqual(len(rows), 1, [(batch['level'], batch['topic']) for batch in selected])
        self.assertIn('没有发生', rows[0]['detail'])
        self.assertEqual([batch['level'] for batch in selected
                          if any(event['source_type'] == 'boundary' for event in batch['events'])],
                         [4])

    def test_cancelled_queued_action_gets_a_reason_and_a_boundary_fact(self):
        core = self.core()
        project = self.make_project(core, 'project-9', '被暂停的事', key='暂停中的事',
                                    status='paused')
        action = dict(kind='command', project='暂停中的事', command='echo hi', op='run')
        with core.repo.tx():
            core.repo._exec('INSERT INTO companion_actions VALUES(?,?,?,?,?,?,?,?,?)',
                            ('action-9', core.life.agent_id, project['ref'], 'event-9',
                             json.dumps(action, ensure_ascii=False), 'pending', None,
                             time.time(), time.time()))
        row = core.repo._one('SELECT * FROM companion_actions WHERE id=?', ('action-9',))
        core._cancel_queued_action(row, project_status=project['status'],
                                   reason='project_not_active')
        after = core.repo._one('SELECT status,result FROM companion_actions WHERE id=?', ('action-9',))
        self.assertEqual(after['status'], 'cancelled')
        result = json.loads(after['result'])
        self.assertEqual(result['boundary'][0]['rule'], 'project_not_active')
        self.assertEqual(result['boundary'][0]['reason'], 'project_not_active')
        self.assertEqual(result['boundary'][0]['step_not_taken'], '这个排队中的动作没有执行')
        self.assertIn('项目状态边界生效', result_view(result))
        self.assertTrue(any('项目状态边界生效' in item['text'] for item in self.activity(core)))
        self.assertEqual(len(self.boundary_events(core)), 1)

    def test_silent_delivery_is_recorded_when_reply_was_suppressed(self):
        core = self.core()
        source, _ = core.life.ingest(core.scope, 'companion_wake', 'self_observation',
                                     '后台小镇回合', payload=dict(wake_kind='timer'))
        decision = dict(reply='想分享一句', followups=[], remember=[], projects=[], actions=[],
                        delivery={}, wake_after=0)
        core._commit(source, decision, quiet_town=True)
        audit = [item for item in self.activity(core, 'boundary') if '静默投递生效' in item['text']]
        self.assertEqual(len(audit), 1)
        details = json.loads(audit[0]['details'])
        self.assertEqual(details['count'], 1)
        self.assertIn('没有对外发言', details['step_not_taken'])
        self.assertEqual(core.repo._all('SELECT * FROM companion_deliveries'), [])


class ToolCallProtocolTest(_Base):
    """t34：工具调用不能被校验层丢掉；没有回执却说结果必须留痕。"""

    def source(self, core):
        return core.life.ingest(core.scope, 'message', 'user_report', '读一下那个文件', role='user')[0]

    def test_work_action_without_goal_is_derived_not_dropped(self):
        core = self.core()
        source = self.source(core)
        decision = dict(reply='', actions=[dict(kind='work', project='读文件', op='read',
                                                arguments=json.dumps(dict(op='read', path='x.txt')))])
        result = dict(message=dict(content=json.dumps(decision, ensure_ascii=False)))
        decoded = core._decode_decision(result, wire=True, audit=core._audit, source=source)
        # 少写 goal 不再整轮丢掉：从动作本身推出来，工具调用还在。
        self.assertEqual(len(decoded['actions']), 1)
        self.assertTrue(decoded['actions'][0]['goal'].strip())
        self.assertEqual(decoded['actions'][0]['goal_source'], 'derived')

    def test_command_action_without_command_still_retried(self):
        core = self.core()
        source = self.source(core)
        decision = dict(reply='', actions=[dict(kind='command', project='跑一下')])
        result = dict(message=dict(content=json.dumps(decision, ensure_ascii=False)))
        with self.assertRaises(ValueError) as error:
            core._decode_decision(result, wire=True, audit=core._audit, source=source)
        self.assertIn('缺 command', str(error.exception))

    def test_claim_without_a_tool_result_is_recorded(self):
        core = self.core()
        source = self.source(core)
        reply = '读了，没这个文件。/tmp/x 报的是 No such file or directory，小鱼没糊弄您。'
        decision = dict(reply=reply, actions=[], followups=[])
        fact = core._record_protocol_facts(source, 'dialogue', decision, None, {}, reply)
        self.assertEqual(fact['claims'], ['No such file or directory'])
        self.assertFalse(fact['had_tool_result'])
        # 她的原话一个字都没改。
        self.assertEqual(decision['reply'], reply)
        audits = [item for item in self.activity(core, 'no_substitution')]
        self.assertEqual(len(audits), 1)
        details = json.loads(audits[0]['details'])
        self.assertEqual(details['source'], source.id)
        self.assertFalse(details['had_tool_result'])

    def test_claim_with_real_action_is_not_flagged(self):
        core = self.core()
        source = self.source(core)
        decision = dict(reply='读到了：No such file or directory 之类的内容', actions=[
            dict(kind='work', project='读文件', op='read', goal='读文件')])
        self.assertIsNone(core._record_protocol_facts(source, 'dialogue', decision, None, {}, decision['reply']))
        self.assertEqual(self.activity(core, 'no_substitution'), [])

    def test_quoting_a_recent_tool_result_is_not_flagged(self):
        """刚查过就引用回执里的内容（含 token 样式）不算无回执。"""
        core = self.core()
        core.life.ingest(core.scope, 'message', 'tool_observation', '内容：PROBE-TOKEN=t34fake',
                         role='tool')
        source = self.source(core)
        decision = dict(reply='文件里写的是 PROBE-TOKEN=t34fake。', actions=[])
        self.assertIsNone(core._record_protocol_facts(source, 'dialogue', decision, None, {}, decision['reply']))
        self.assertEqual(self.activity(core, 'no_substitution'), [])

    def test_plain_reply_without_claims_is_not_flagged(self):
        core = self.core()
        source = self.source(core)
        decision = dict(reply='行，小鱼读一下。', actions=[])
        self.assertIsNone(core._record_protocol_facts(source, 'dialogue', decision, None, {}, decision['reply']))
        self.assertEqual(self.activity(core, 'no_substitution'), [])

    def test_model_ignoring_the_respond_tool_is_a_system_fact(self):
        core = self.core()
        source = self.source(core)
        decision = dict(reply='行，小鱼读一下。', actions=[])
        core._record_protocol_facts(source, 'dialogue', decision, None,
                                    dict(wire_schema_error='Expected exactly one respond tool call'),
                                    decision['reply'])
        titles = [item['text'] for item in self.activity(core, 'boundary')]
        self.assertIn('系统：模型没有按工具协议回答', titles)
        fact = json.loads(self.boundary_events(core)[0]['payload'])['boundary']
        self.assertEqual(fact['rule'], 'model_ignored_tool_call')
        self.assertEqual(fact['attribution'], 'system')


class SkippedActionTest(_Base):
    """t36：动作被跳过时必须有她看得见的〔系统〕回执；要不要重做由她决定。"""

    def _project(self, core, key, status):
        ref = core._project_ref(key)
        core.tasks.ensure(core.scope, ref, key, key=key, status=status)
        return ref

    def _outgoing_texts(self, core):
        texts = []
        for row in core.repo._all(
                "SELECT payload FROM events WHERE agent_id=? AND source_kind='self_output'",
                (core.life.agent_id,)):
            try:
                value = json.loads(row['payload'] or '{}')
            except ValueError:
                value = {}
            texts.append(str(value.get('text') or ''))
        for item in (core._state('bubble_outbox') or []):
            texts.append(str(item.get('text') or ''))
        return [text for text in texts if text]

    def _decision(self, reply, action):
        return dict(reply=reply, followups=[], remember=[], projects=[], actions=[action],
                    delivery={}, wake_after=0)

    def _commit(self, core, source, decision):
        # _commit 里会（在有 pending 气泡时）建发布任务，需要事件循环。
        async def run():
            return core._commit(source, decision)
        return asyncio.run(run())

    def test_done_project_action_gets_a_visible_system_receipt(self):
        core = self.core()
        self._project(core, '读文件', 'done')
        source, _ = core.life.ingest(core.scope, 'message', 'user_report', '读一下那个文件', role='user')
        action = dict(kind='command', project='读文件', command='cat /tmp/t38-probe.txt',
                      goal='读文件内容')
        self._commit(core, source, self._decision('小鱼这就去读，一个字都不改。', action))
        # 既有语义不变：非 active 项目不派新动作
        self.assertEqual(core.repo._all('SELECT * FROM companion_actions'), [])
        texts = self._outgoing_texts(core)
        receipts = [text for text in texts if '这个动作没有执行' in text]
        self.assertEqual(len(receipts), 1)
        self.assertIn('command', receipts[0])
        self.assertIn('读文件', receipts[0])
        self.assertIn('done', receipts[0])
        self.assertIn('由你决定', receipts[0])
        # 她自己的话原样保留，一个字都没改
        self.assertIn('小鱼这就去读，一个字都不改。', texts)
        # 同一条事实也进了下一轮她能看到的材料（边界事实 + 审计）
        events = self.boundary_events(core)
        self.assertTrue(events)
        self.assertIn('动作没有执行', events[0]['detail'])
        self.assertEqual(json.loads(events[0]['payload'])['boundary']['project_status'], 'done')

    def test_skipped_action_counts_as_no_tool_result_for_claims(self):
        core = self.core()
        self._project(core, '读文件', 'done')
        source, _ = core.life.ingest(core.scope, 'message', 'user_report', '读一下', role='user')
        action = dict(kind='command', project='读文件', command='cat /tmp/t38-probe.txt')
        decision = self._decision(
            '读了，/tmp/t38-probe.txt 报的是 No such file or directory。', action)
        self._commit(core, source, decision)
        # _run_cognition 里由 _commit 的计数驱动这一步；这里直接调用同一入口。
        core._record_protocol_facts(source, 'dialogue', decision, None, {},
                                    decision['reply'])
        audits = [item for item in self.activity(core, 'no_substitution')]
        self.assertEqual(len(audits), 1)
        details = json.loads(audits[0]['details'])
        self.assertEqual(details['skipped_actions'], 1)
        self.assertEqual(details['actions'], 0)
        self.assertIn('No such file or directory', details['claims'])

    def test_active_project_action_is_dispatched_without_skip_receipt(self):
        core = self.core()
        self._project(core, '读文件', 'active')
        source, _ = core.life.ingest(core.scope, 'message', 'user_report', '读一下那个文件', role='user')
        action = dict(kind='command', project='读文件', command='cat /tmp/t38-probe.txt',
                      goal='读文件内容')
        self._commit(core, source, self._decision('小鱼这就去读。', action))
        rows = core.repo._all('SELECT action,status FROM companion_actions')
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]['action'])['kind'], 'command')
        receipts = [text for text in self._outgoing_texts(core) if '这个动作没有执行' in text]
        self.assertEqual(receipts, [])
        self.assertEqual(self.boundary_events(core), [])

    def test_skipped_action_fact_is_visible_in_the_next_turn_material(self):
        core = self.core()
        self._project(core, '读文件', 'done')
        source, _ = core.life.ingest(core.scope, 'message', 'user_report', '读一下那个文件', role='user')
        action = dict(kind='command', project='读文件', command='cat /tmp/t38-probe.txt')
        self._commit(core, source, self._decision('小鱼这就去读。', action))
        core._prepare_batches(source, 'dialogue')
        material = core._messages(source, '继续', None, route=core._message_route(source))
        blob = json.dumps(material, ensure_ascii=False)
        self.assertIn('动作没有执行', blob)
        self.assertIn('done', blob)


if __name__ == '__main__':
    unittest.main()


