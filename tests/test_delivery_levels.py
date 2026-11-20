# -*- coding: utf-8 -*-
"""事件已发生 ≠ 她已收到：六级投递、批次溯源、订阅与续接。

这一组测试守的是四件事：
1. 每条事件在批次里仍是独立的一条（带自己的 id/来源/引用），不拼成一句话；
2. 六级只决定什么时候送，级别不会丢内容；窗口与上限可配置；
3. 批次真的装进模型请求且请求成功之后才写「已送达」；失败、重启都不冒充已处理；
4. 可能已经发出、未确认的消息只作为事实出现，不触发任何重发。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from life import companion_batches as batches
from life import companion_delivery as ledger
from life.runtime import LifeRuntime


class FakeCore:
    """只带 companion_batches 真正会读的几样：repo / scope / life / cfg。"""

    def __init__(self, life, *, conversation='qq:private:1', policy=None):
        self.life = life
        self.repo = life.repo
        self.scope = life.scope(conversation, '1')
        self.cfg = SimpleNamespace(resource_policy=dict(policy or {}))
        self._batch = None
        self._interjections = []
        self._meta = {}

    def _state(self, key, default=None):
        return self._meta.get(key, default)

    def _save(self, key, value):
        self._meta[key] = value


def _event(kind, ref, *, title='', detail='', at=None, payload=None, affects=False, topic=''):
    return batches.make_event(
        event_id=batches.event_id_for(kind, ref), source_type=kind, title=title or kind,
        detail=detail, source_ref=ref, action_ref=ref + ':action', occurred_at=at or time.time(),
        topic=topic, payload=payload or {}, affects_current_action=affects)


class LevelsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='delivery-levels-')
        self.addCleanup(self.temp.cleanup)
        self.life = LifeRuntime(Path(self.temp.name) / 'life.db')
        self.addCleanup(self.life.close)

    def core(self, **kwargs):
        policy = dict(kwargs.pop('policy', {}))
        # 六级各留一批：默认 max_batches_per_turn 是 4，这里按需要放宽。
        policy.setdefault('max_batches_per_turn', 8)
        return FakeCore(self.life, policy=policy, **kwargs)

    # ---------------------------------------------------------------- 六级
    def test_six_levels_decide_when_not_whether(self):
        core = self.core()
        now = time.time()
        events = [
            _event('interjection', 'i1', title='她打断了我', detail='停一下', at=now),
            _event('user_input', 'u1', title='第一段原话', detail='先做这个', at=now),
            _event('awaited_result', 'task-1', title='子任务有结果了',
                   detail='completed；目标 修好投递', at=now, payload=dict(task_ref='t1')),
            _event('state_change', 'phone', title='手机断开了', detail='10:00 由调度器改变', at=now),
            _event('daily_experience', 'today-1', title='今天的一件经历', detail='在河边走了很久',
                   at=now, payload=dict(scene='walk')),
            _event('index_progress', 'index', title='索引进度', detail='词法 ready', at=now),
        ]
        batches.record(core, events)
        selected = batches.select(core, now=now, conversation=True, wake_reason='她说话了')
        levels = sorted(item['level'] for item in selected)
        self.assertEqual(levels, [1, 2, 3, 4, 5, 6])
        by_level = {item['level']: item for item in selected}
        # 每条事件在自己的批次里仍然独立，且都带自己的来源。
        for item in selected:
            self.assertTrue(item['events'])
            for event in item['events']:
                self.assertIn('event_id', event)
                self.assertIn('source_ref', event)
        # 六级各自的「什么时候送」是写明的事实，不是猜测。
        self.assertIn('停止', by_level[1]['context']['timing'])
        self.assertIn('短窗口', by_level[2]['context']['timing'])
        self.assertIn('攒一批', by_level[3]['context']['timing'])
        self.assertIn('自然认知', by_level[4]['context']['timing'])
        self.assertIn('不逐条', by_level[5]['context']['timing'])
        self.assertIn('计数', by_level[6]['context']['timing'])
        # 级别不丢内容：六级的事件都还在 pending 里（被记账前谁都少不了）。
        self.assertEqual(len(batches.pending_events(core)), 6)

    def test_awaiting_result_accumulates_same_task_into_one_batch(self):
        core = self.core()
        now = time.time()
        events = [_event('awaited_result', 'r%d' % index, detail='第 %d 个变化' % index,
                         at=now, payload=dict(task_ref='t7')) for index in range(3)]
        batches.record(core, events)
        selected = batches.select(core, now=now, conversation=False)
        self.assertEqual(len(selected), 1)
        batch = selected[0]
        self.assertEqual(batch['level'], batches.AWAITED)
        self.assertEqual(len(batch['events']), 3)
        self.assertEqual((batch['context'].get('shared') or {}).get('task_ref'), 't7')

    def test_current_action_state_is_immediate_and_reports_stop_state(self):
        core = self.core()
        now = time.time()
        core._interjections = [dict(text='别说了，先停下', at=now, source=dict(id='evt-1'))]
        events = batches.events_from_observation(core, dict(status='cancelled', id='act-1',
                                                           stopped=True, reason='她要求停'))
        batches.record(core, events)
        selected = batches.select(core, now=now, conversation=True)
        immediate = [item for item in selected if item['level'] == batches.IMMEDIATE]
        self.assertTrue(immediate)
        stop_states = {event['stop_state'] for item in immediate for event in item['events']}
        self.assertIn('stopped', stop_states)
        self.assertIn('pending_bubbles_cancelled', stop_states)
        # 停止未确认时不许写成已停。
        unconfirmed = batches.events_from_observation(core, dict(status='cancelled', id='act-2'))
        self.assertEqual(unconfirmed[0]['stop_state'], 'unconfirmed')

    # ------------------------------------------------------------ 不拼接
    def test_batch_keeps_events_separate_with_their_own_refs(self):
        core = self.core()
        now = time.time()
        first = _event('awaited_result', 'ref-a', detail='第一件：编译失败', at=now,
                       payload=dict(task_ref='t9'))
        second = _event('awaited_result', 'ref-b', detail='第二件：测试通过', at=now + 1,
                        payload=dict(task_ref='t9'))
        batches.record(core, [first, second])
        batch = batches.select(core, now=now + 2, conversation=False)[0]
        events = batch['events']
        self.assertEqual(len(events), 2)
        self.assertEqual([event['source_ref'] for event in events], ['ref-a', 'ref-b'])
        self.assertEqual({event['event_id'] for event in events},
                         {first['event_id'], second['event_id']})
        # 没有「几件事用空格拼成一句」：两段正文互不包含，各自留在自己的条目里。
        self.assertNotIn(events[0]['detail'], events[1]['detail'])
        self.assertNotIn(events[1]['detail'], events[0]['detail'])
        self.assertNotIn('第一件：编译失败 第二件：测试通过', json.dumps(batch, ensure_ascii=False))

    def test_coverage_and_unexpanded_are_reported(self):
        core = self.core(policy=dict(max_events_per_batch=5))
        now = time.time()
        events = [_event('awaited_result', 'n%d' % index, detail='变化 %d' % index, at=now + index,
                         payload=dict(task_ref='t3')) for index in range(13)]
        batches.record(core, events)
        batch = batches.select(core, now=now + 20, conversation=False)[0]
        self.assertEqual(batch['coverage']['count'], 5)
        self.assertEqual(len(batch['coverage']['event_ids']), 5)
        self.assertLessEqual(batch['coverage']['from_at'], batch['coverage']['to_at'])
        self.assertEqual(batch['unexpanded_count'], 8)
        self.assertEqual(len(batch['unexpanded']['event_ids']), 8)
        self.assertTrue(batch['unexpanded']['reason'])
        # 全局未展开：13 件都已经发生、都还没送到眼前。
        self.assertEqual(batches.unexpanded(core)['count'], 13)

    # --------------------------------------------------- 已发生 vs 已收到
    def test_model_failure_is_not_processed_and_next_turn_continues(self):
        core = self.core()
        now = time.time()
        batches.record(core, [_event('awaited_result', 'k1', detail='结果到了', at=now,
                                     payload=dict(task_ref='t1'))])
        selected = batches.select(core, now=now, conversation=False, wake_reason='看看')
        self.assertEqual(len(selected), 1)
        batches.fail(core, selected, 'turn-1', reason='模型请求失败')
        # 失败不写「已送达」：批次与事件都还在 pending，台账记的是这一次没送成。
        self.assertEqual(batches.pending_events(core)[0]['event_id'],
                         selected[0]['events'][0]['event_id'])
        self.assertEqual(batches.recent(core)[0]['state'], 'pending')
        self.assertEqual(batches.recent(core)[0]['attempts'], 1)
        row = self.life.repo._one('SELECT model_ok,reason FROM companion_batch_deliveries '
                                  'WHERE decision_id=?', ('turn-1',))
        self.assertEqual(int(row['model_ok']), 0)
        self.assertIn('失败', row['reason'])
        # 下一轮（或重启后）同一批还能续接，并且这次真的记账为已送达。
        again = batches.select(core, now=now + 5, conversation=False)
        self.assertEqual(len(again), 1)
        batches.deliver(core, again, 'turn-2')
        self.assertEqual(batches.pending_events(core), [])
        row = self.life.repo._one('SELECT model_ok,event_ids FROM companion_batch_deliveries '
                                  'WHERE decision_id=?', ('turn-2',))
        self.assertEqual(int(row['model_ok']), 1)
        self.assertEqual(json.loads(row['event_ids']), [again[0]['events'][0]['event_id']])

    def test_restart_keeps_pending_batches_continuable(self):
        first = self.core()
        now = time.time()
        batches.record(first, [_event('state_change', 'phone', detail='手机断开了', at=now)])
        selected = batches.select(first, now=now, conversation=False)
        self.assertTrue(selected)
        # 没有送达记账就「重启」：同一份库上换一个 core。
        second = FakeCore(self.life)
        pending = batches.pending_events(second)
        self.assertEqual(len(pending), 1)
        again = batches.select(second, now=now + 1, conversation=False)
        self.assertTrue(again)
        batches.deliver(second, again, 'after-restart')
        self.assertEqual(batches.pending_events(second), [])

    def test_window_keeps_daily_experience_from_waking_her_every_turn(self):
        core = self.core(policy=dict(daily_window_seconds=600))
        now = time.time()
        batches.record(core, [_event('daily_experience', 'd1', detail='在河边走了很久', at=now,
                                     payload=dict(scene='walk'))])
        selected = batches.select(core, now=now, conversation=False)
        self.assertEqual([item['level'] for item in selected], [batches.DAILY])
        batches.deliver(core, selected, 'turn-1')
        batches.record(core, [_event('daily_experience', 'd2', detail='又看了一会儿书',
                                     at=now + 10, payload=dict(scene='walk'))])
        # 同一场景还在窗口里：这一轮不叫醒她，但事件仍然记着（未展开）。
        self.assertEqual(batches.select(core, now=now + 20, conversation=False), [])
        self.assertEqual(batches.unexpanded(core)['count'], 1)
        # 窗口过去之后按场景合批送一次。
        after = batches.select(core, now=now + 700, conversation=False)
        self.assertEqual([item['level'] for item in after], [batches.DAILY])

    # ---------------------------------------------------------------- 订阅
    def test_subscriptions_can_ask_earlier_or_silence(self):
        core = self.core()
        now = time.time()
        progress = _event('index_progress', 'index', detail='词法 ready', at=now)
        self.assertEqual(batches.classify(progress, subscriptions=[]), batches.BACKGROUND)
        batches.subscribe(core, 'index_progress', notify=True, note='索引进度回来告诉我')
        self.assertEqual(batches.classify(progress, subscriptions=batches.subscriptions(core)),
                         batches.AWAITED)
        batches.subscribe(core, 'index_progress', notify=False, note='索引进度不用叫我')
        subs = batches.subscriptions(core)
        self.assertEqual(batches.classify(progress, subscriptions=subs), batches.BACKGROUND)
        # 订阅只改时机：同一件事仍然被记录，没被丢弃。
        batches.record(core, [progress])
        self.assertEqual(len(batches.pending_events(core)), 1)

    def test_task_subscription_matches_the_real_task_ref(self):
        core = self.core()
        now = time.time()
        batches.subscribe(core, 'task:t42', notify=True, note='这个子任务回来就告诉我')
        event = _event('agent_progress', 'a1', detail='跑着', at=now,
                       payload=dict(task_ref='t42', agent_id='a1'))
        self.assertEqual(batches.classify(event, subscriptions=batches.subscriptions(core)),
                         batches.AWAITED)
        other = _event('agent_progress', 'a2', detail='别的任务', at=now,
                       payload=dict(task_ref='t99', agent_id='a2'))
        self.assertEqual(batches.classify(other, subscriptions=batches.subscriptions(core)),
                         batches.BACKGROUND)

    def test_unsubscribe_stops_the_early_delivery(self):
        core = self.core()
        batches.subscribe(core, 'index_progress', notify=True)
        batches.unsubscribe(core, 'index_progress')
        self.assertEqual(batches.subscriptions(core), [])

    def test_she_can_subscribe_through_the_attention_op(self):
        """她说「这个子任务回来就告诉我 / 索引进度不用叫我」，走现有关注 op，回执形状不变。"""
        from life import companion_awareness as awareness
        core = self.core()
        reply = awareness.set_attention(core, dict(arguments=json.dumps(
            dict(op='attention_set', subscribe_topic='task:t42', notify=True,
                 reason='这个子任务回来就告诉我'))))
        self.assertEqual(reply['status'], 'completed')
        self.assertEqual(set(reply), {'status', 'op', 'items', 'next_cursor', 'source_refs',
                                      'freshness', 'truncation', 'evidence_scope',
                                      'evidence_note', 'output'})
        self.assertEqual(reply['items'][0]['topic'], 'task:t42')
        self.assertIn('提前告诉你', reply['output'])
        muted = awareness.set_attention(core, dict(arguments=json.dumps(
            dict(op='attention_set', subscribe_topic='index_progress', notify=False,
                 reason='索引进度不用叫我'))))
        self.assertIn('不叫你', muted['output'])
        subs = {(item['topic'], item['notify']) for item in batches.subscriptions(core)}
        self.assertEqual(subs, {('task:t42', True), ('index_progress', False)})
        removed = awareness.set_attention(core, dict(arguments=json.dumps(
            dict(op='attention_set', unsubscribe_topic='task:t42'))))
        self.assertIn('不再', removed['output'])
        self.assertEqual([item['topic'] for item in batches.subscriptions(core)], ['index_progress'])

    # -------------------------------------------------- 未确认：只当事实
    def test_unconfirmed_send_becomes_a_fact_and_is_never_resent(self):
        core = self.core()
        mid = ledger.message_id('turn-9', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-9', segment=0,
                     text='我在呢', conversation_ref='qq:private:1', channels=['qq'], now=1000.0)
        ledger.mark_unknown(core.repo, mid, reason='发送后没拿到端上回执', now=1001.0)
        events = batches.events_from_delivery(core, now=1002.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['source_type'], 'delivery_unknown')
        self.assertIn('可能已经发出', events[0]['title'])
        # t30：只陈述事实，不再写「由你决定要不要再说一次」这种诱发重复发言的引导。
        self.assertIn('如果对话里已经出现就不用再说一次', events[0]['detail'])
        self.assertNotIn('由你决定要不要再说一次', events[0]['detail'])
        batches.record(core, events)
        # 台账状态不变：unknown 仍是 unknown，不是失败也不是已送达。
        self.assertEqual(ledger.state(core.repo, mid), ledger.UNKNOWN)
        self.assertEqual(ledger.summary(core.repo, agent_id=core.life.agent_id)['unknown'], 1)
        selected = batches.select(core, now=1003.0, conversation=True)
        batches.deliver(core, selected, 'turn-10')
        # 同一回执不会再次叫醒她：事件已是 delivered，pending 为空。
        self.assertEqual(batches.pending_events(core), [])
        self.assertEqual(batches.select(core, now=1010.0, conversation=True), [])
        # 再来一轮也不会多出第二条事件（同一个 event_id 只记一次）。
        again = batches.events_from_delivery(core, now=1020.0)
        self.assertEqual([item['event_id'] for item in again], [events[0]['event_id']])
        batches.record(core, again)
        self.assertEqual(batches.pending_events(core), [])
        # 期间没有任何重发：投递台账里仍然只有最初那一条。
        rows = core.repo._all('SELECT message_id,state FROM companion_deliveries')
        self.assertEqual([(row['message_id'], row['state']) for row in rows],
                         [(mid, ledger.UNKNOWN)])

    # ------------------------------------------------- t30 竞态：送达前重读投递状态
    def test_delivery_pending_is_refreshed_to_resolved_at_delivery(self):
        """装批时是 queued，送达时已 rendered：送到的是「已经发出去了」，不是旧快照。"""
        core = self.core()
        mid = ledger.message_id('turn-30', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-30', segment=0,
                     text='先开绝区零看看', conversation_ref='qq:private:1', channels=['qq'], now=2000.0)
        events = batches.events_from_delivery(core, now=2001.0)
        self.assertEqual(events[0]['source_type'], 'delivery_pending')
        self.assertIn('不知道它到没到', events[0]['detail'])
        batches.record(core, events)
        selected = batches.select(core, now=2002.0, conversation=False)
        self.assertTrue(selected)
        # 装批之后、送达之前：这一段真的发出去了（并拿到端上回执）。
        ledger.mark_sending(core.repo, mid, now=2003.0)
        ledger.mark_sent(core.repo, mid, event_ref='evt-sent', now=2004.0)
        ledger.confirm_render(core.repo, mid, now=2005.0)
        batches.deliver(core, selected, 'turn-31', now=2006.0)
        row = core.repo._one("SELECT source_type,state,title,detail,payload FROM companion_batch_events "
                             "WHERE source_type='delivery_pending' ORDER BY created_at DESC LIMIT 1")
        payload = json.loads(row['payload'])
        self.assertEqual(payload['state'], 'resolved')
        self.assertIn('已经发出去了', row['detail'])
        self.assertRegex(row['detail'], r'已经发出去了（\d{2}:\d{2}:\d{2}）')
        self.assertNotIn('不知道它到没到', row['detail'])
        self.assertEqual([item['state'] for item in payload['timeline']][0], 'queued')
        self.assertIn('sent', [item['state'] for item in payload['timeline']])
        # 已送达的事件就是 resolved 的事实，台账里也没有留下「未确认」的旧壳。
        self.assertEqual(batches.pending_events(core), [])

    def test_still_pending_at_delivery_only_states_facts(self):
        """送达时确实仍无回执：只陈述事实，不写会诱发重复发言的引导。"""
        core = self.core()
        mid = ledger.message_id('turn-32', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-32', segment=1,
                     text='还在路上', conversation_ref='qq:private:1', channels=['qq'], now=3000.0)
        batches.record(core, batches.events_from_delivery(core, now=3001.0))
        selected = batches.select(core, now=3002.0, conversation=False)
        batches.deliver(core, selected, 'turn-33', now=3003.0)
        row = core.repo._one("SELECT title,detail,payload FROM companion_batch_events "
                             "WHERE source_type='delivery_pending' ORDER BY created_at DESC LIMIT 1")
        payload = json.loads(row['payload'])
        self.assertIn('不知道它到没到', row['detail'])
        self.assertIn('如果对话里已经出现就不用再说一次', row['detail'])
        self.assertNotIn('由你决定要不要再说一次', row['detail'])
        self.assertNotEqual(payload['state'], 'resolved')

    def test_render_before_batch_creation_is_already_resolved_in_the_view(self):
        """装批前就已发出的消息：连事件本身都不该写成「未确认」。"""
        core = self.core()
        mid = ledger.message_id('turn-34', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-34', segment=0,
                     text='已经发出', conversation_ref='qq:private:1', channels=['qq'], now=4000.0)
        ledger.mark_sending(core.repo, mid, now=4001.0)
        ledger.mark_sent(core.repo, mid, now=4002.0)
        # 之前装批留下的 pending 事件，现在状态变了：select 时应当被刷新。
        stale = batches.make_event(
            event_id=batches.event_id_for('delivery', mid, 'queued'),
            source_type='delivery_pending', title='这条还在发送中',
            detail='不知道它到没到；如果对话里已经出现就不用再说一次',
            source_ref=mid, occurred_at=4000.0, topic='delivery:turn-34',
            payload=dict(message_id=mid, state='queued', timeline=[dict(state='queued', at='00:00:00')]))
        batches.record(core, [stale])
        selected = batches.select(core, now=4005.0, conversation=False)
        pending = [event for batch in selected for event in batch['events']
                   if event['source_type'] == 'delivery_pending']
        self.assertEqual(len(pending), 1)
        self.assertIn('已经发出去了', pending[0]['detail'])
        self.assertNotIn('不知道它到没到', pending[0]['detail'])

    # ------------------------------------------- t33 / t30 评审 L1–L3 残留
    def test_unknown_that_later_renders_is_refreshed_to_resolved(self):
        """L1：UNKNOWN→RENDERED 的迟到确认也要重读，不能还挂着「可能已经发出、没有确认」。"""
        core = self.core()
        mid = ledger.message_id('turn-35', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-35', segment=0,
                     text='可能已经发出', conversation_ref='qq:private:1', channels=['qq'], now=5000.0)
        ledger.mark_unknown(core.repo, mid, reason='没拿到端上回执', now=5001.0)
        events = batches.events_from_delivery(core, now=5002.0)
        self.assertEqual(events[0]['source_type'], 'delivery_unknown')
        batches.record(core, events)
        selected = batches.select(core, now=5003.0, conversation=False)
        self.assertTrue(selected)
        # 迟到确认：UNKNOWN → RENDERED（没有经过 mark_sent，sent_at 为空）
        ledger.confirm_render(core.repo, mid, now=5004.0)
        batches.deliver(core, selected, 'turn-36', now=5005.0)
        row = core.repo._one("SELECT source_type,title,detail,payload FROM companion_batch_events "
                             "WHERE source_type='delivery_unknown' ORDER BY created_at DESC LIMIT 1")
        payload = json.loads(row['payload'])
        self.assertEqual(payload['state'], 'resolved')
        self.assertEqual(row['title'], '这条已经发出去了')
        self.assertIn('已经发出去了', row['detail'])
        self.assertNotIn('可能已经发出、但没有确认', row['detail'])
        self.assertNotIn('可能已经发出、但没有确认', row['title'])
        self.assertNotIn('不知道它到没到', row['detail'])
        self.assertEqual(payload['time_basis'], 'rendered_at')
        states = [item['state'] for item in payload['timeline']]
        self.assertIn('sent', states)
        self.assertIn('rendered', states)

    def test_explicit_failure_during_generation_uses_failed_wording(self):
        """L2：明确失败要有自己的句子，不能拼成「还没有回执＋不知道到没到」。"""
        core = self.core()
        mid = ledger.message_id('turn-37', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-37', segment=0,
                     text='这条会失败', conversation_ref='qq:private:1', channels=['qq'], now=5200.0)
        batches.record(core, batches.events_from_delivery(core, now=5201.0))
        selected = batches.select(core, now=5202.0, conversation=False)
        self.assertTrue(selected)
        # 装批之后、送达之前：这一段明确失败。
        ledger.mark_sending(core.repo, mid, now=5203.0)
        ledger.fail(core.repo, mid, reason='transport died', now=5204.0)
        batches.deliver(core, selected, 'turn-38', now=5205.0)
        row = core.repo._one("SELECT title,detail,payload FROM companion_batch_events "
                             "WHERE source_type='delivery_pending' ORDER BY created_at DESC LIMIT 1")
        payload = json.loads(row['payload'])
        self.assertEqual(payload['state'], 'failed')
        self.assertEqual(row['title'], '这条明确没有送出去')
        self.assertIn('明确没有送出去（', row['detail'])
        self.assertIn('原因：transport died', row['detail'])
        self.assertIn('不用等它的回执', row['detail'])
        self.assertNotIn('还没有回执', row['detail'])
        self.assertNotIn('不知道它到没到', row['detail'])
        self.assertIn('failed', [item['state'] for item in payload['timeline']])
        self.assertEqual(payload['failure_reason'], 'transport died')

    def test_resolved_time_falls_back_to_rendered_at_when_sent_at_missing(self):
        """L3：UNKNOWN→RENDERED 没有 mark_sent 时 sent_at 为空，文案时间必须回退到 rendered_at。"""
        core = self.core()
        mid = ledger.message_id('turn-39', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-39', segment=0,
                     text='没有 sent_at', conversation_ref='qq:private:1', channels=['qq'], now=6000.0)
        ledger.mark_unknown(core.repo, mid, now=6001.0)
        batches.record(core, batches.events_from_delivery(core, now=6002.0))
        selected = batches.select(core, now=6003.0, conversation=False)
        rendered_at = 6004.0
        ledger.confirm_render(core.repo, mid, now=rendered_at)
        batches.deliver(core, selected, 'turn-40', now=6005.0)
        row = core.repo._one("SELECT detail,payload FROM companion_batch_events "
                             "WHERE source_type='delivery_unknown' ORDER BY created_at DESC LIMIT 1")
        expected = time.strftime('%H:%M:%S', time.localtime(rendered_at))
        self.assertIn('已经发出去了（%s）' % expected, row['detail'])
        self.assertNotIn('时间未记录', row['detail'])
        payload = json.loads(row['payload'])
        self.assertIsNone(payload['sent_at'])
        self.assertEqual(payload['time_basis'], 'rendered_at')

    # ------------------------------------------------------------ 采集边界
    def test_forgotten_material_cannot_reappear_through_an_old_batch(self):
        core = self.core()
        now = time.time()
        batches.record(core, [_event('daily_experience', 'gone', detail='不该再出现的事', at=now)])
        self.assertEqual(len(batches.pending_events(core)), 1)
        self.life.repo.bump_forget_epoch(core.scope.scope_id)
        self.assertEqual(batches.pending_events(core), [])
        self.assertEqual(batches.select(core, now=now + 5, conversation=False), [])
        self.assertEqual(batches.unexpanded(core)['count'], 0)

    def test_other_audience_never_sees_this_batch(self):
        core = self.core(conversation='qq:private:1')
        other = FakeCore(self.life, conversation='qq:group:90')
        now = time.time()
        batches.record(core, [_event('daily_experience', 'private', detail='只给私聊的事', at=now)])
        self.assertEqual(batches.pending_events(other), [])
        self.assertEqual(batches.select(other, now=now, conversation=True), [])
        self.assertEqual(len(batches.pending_events(core)), 1)

    # ------------------------------------------------- t24 评审修复（F1/F2/F4/F5/F6/F7/F9）
    def test_stop_event_survives_subscription_mute(self):
        """F1：影响当前动作的事件必须先判 affects，订阅 notify=0 不得把它降到 6 级。"""
        core = self.core()
        now = time.time()
        batches.subscribe(core, 'interjection', notify=False, note='这类不用叫我')
        event = _event('interjection', 'i9', title='她打断了我', detail='停一下',
                       at=now, affects=True)
        self.assertEqual(batches.classify(event, subscriptions=batches.subscriptions(core)),
                         batches.IMMEDIATE)
        # 「当前动作已被停止」同理：它也是 1 级，订阅也压不住。
        core._interjections = [dict(text='停下', at=now, source=dict(id='evt-9'))]
        stopped = batches.events_from_observation(core, dict(status='cancelled', id='act-9',
                                                            stopped=True))
        batches.record(core, stopped)
        self.assertEqual(batches.classify(stopped[0], subscriptions=batches.subscriptions(core)),
                         batches.IMMEDIATE)
        selected = batches.select(core, now=now, conversation=False)
        self.assertIn(batches.IMMEDIATE, [item['level'] for item in selected])

    def test_delivery_state_change_is_not_frozen_by_event_id(self):
        """F2：同一条消息 pending→failed 不能被 event_id 去重吞掉；台账要显示最新状态与时间线。"""
        core = self.core()
        mid = ledger.message_id('turn-7', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-7', segment=0,
                     text='我想说句话', conversation_ref='qq:private:1', channels=['qq'], now=1000.0)
        first = batches.events_from_delivery(core, now=1001.0)
        self.assertEqual(first[0]['source_type'], 'delivery_pending')
        batches.record(core, first)
        self.assertEqual([row['source_type'] for row in batches.pending_events(core)],
                         ['delivery_pending'])
        # 发送失败：台账从「还在发送中」变成「明确失败」。
        ledger.mark_sending(core.repo, mid, now=1002.0)
        sending = batches.events_from_delivery(core, now=1002.5)
        batches.record(core, sending)
        ledger.fail(core.repo, mid, reason='transport died', now=1003.0)
        second = batches.events_from_delivery(core, now=1004.0)
        self.assertEqual(second[0]['source_type'], 'delivery_failed')
        self.assertEqual(second[0]['payload']['previous_state'], 'sending')
        self.assertIn('之前是发送中', second[0]['detail'])
        self.assertEqual([item['state'] for item in second[0]['payload']['timeline']],
                         ['queued', 'sending', 'failed'])
        batches.record(core, second)
        # 旧的那条 pending 事实被作废：台账不会永远停在「还在发送中」。
        self.assertEqual([row['source_type'] for row in batches.pending_events(core)],
                         ['delivery_failed'])
        states = {(row['source_type'], row['state']) for row in core.repo._all(
            'SELECT source_type,state FROM companion_batch_events')}
        self.assertIn(('delivery_pending', 'superseded'), states)

    def test_unexpanded_uses_the_real_levels(self):
        """F4：未合批事件按实际级别归类，不再一律标成「状态变化」。"""
        core = self.core()
        now = time.time()
        batches.record(core, [
            _event('index_progress', 'i1', at=now),
            _event('daily_experience', 'd1', at=now, payload=dict(scene='walk')),
            _event('awaited_result', 'a1', at=now, payload=dict(task_ref='t1')),
        ])
        info = batches.unexpanded(core)
        self.assertEqual(info['by_level'].get('后台明细'), 1)
        self.assertEqual(info['by_level'].get('日常经历'), 1)
        self.assertEqual(info['by_level'].get('正在等的结果'), 1)
        self.assertNotIn('状态变化', info['by_level'])
        self.assertEqual(info['count'], 3)
        # 级别真判不出来时才写 unknown，并给出可回查的事件 id。
        with mock.patch.object(batches, 'classify', side_effect=RuntimeError('boom')):
            unknown = batches.unexpanded(core)
        self.assertEqual(unknown['by_level'], {'unknown': 3})
        self.assertEqual(len(unknown['unknown_event_ids']), 3)
        self.assertIn('unknown_meaning', unknown)

    def test_no_dead_delivery_knobs(self):
        """F5：没有调用点的旋钮不留（awaited 窗口 / unknown 冷却）。"""
        from life.resource_policy import ResourcePolicy
        fields = set(ResourcePolicy.__dataclass_fields__)
        self.assertNotIn('delivery_awaited_window_seconds', fields)
        self.assertNotIn('delivery_unknown_cooldown_seconds', fields)
        self.assertNotIn('awaited_window_seconds', batches.DEFAULT_THRESHOLDS)
        self.assertNotIn('unknown_delivery_cooldown_seconds', batches.DEFAULT_THRESHOLDS)
        self.assertEqual(batches.AWAITED_WINDOW_SECONDS, 600)

    def test_delivered_batch_row_is_never_rewritten(self):
        """F6：同窗口内已送达的批次行不复写；deliveries 与事件行能逐条对账。"""
        core = self.core()
        now = time.time()
        batches.record(core, [_event('state_change', 's1', at=now)])
        first = batches.select(core, now=now, conversation=False)
        self.assertEqual(len(first), 1)
        batches.deliver(core, first, 'turn-1')
        before = core.repo._one('SELECT state,updated_at,coverage FROM companion_batches '
                                'WHERE batch_id=?', (first[0]['batch_id'],))
        self.assertEqual(before['state'], 'delivered')
        # 同窗口再来一批同类事实：必须换 id，不能把已送达的行改回 pending。
        batches.record(core, [_event('state_change', 's2', at=now + 1)])
        second = batches.select(core, now=now + 1, conversation=False)
        self.assertTrue(second)
        self.assertNotEqual(second[0]['batch_id'], first[0]['batch_id'])
        after = core.repo._one('SELECT state,updated_at FROM companion_batches WHERE batch_id=?',
                               (first[0]['batch_id'],))
        self.assertEqual((after['state'], after['updated_at']), ('delivered', before['updated_at']))
        # 对账：台账 event_ids == 批次的 delivered_event_ids == 逐事件送达行 == 事件行里 delivered 的那些。
        row = core.repo._one('SELECT event_ids,coverage FROM companion_batch_deliveries '
                             'WHERE batch_id=?', (first[0]['batch_id'],))
        ledger_ids = json.loads(row['event_ids'])
        coverage = json.loads(row['coverage'])
        self.assertEqual(ledger_ids, coverage['delivered_event_ids'])
        per_event = [r['event_id'] for r in core.repo._all(
            'SELECT event_id FROM companion_batch_delivery_events WHERE batch_id=?',
            (first[0]['batch_id'],))]
        self.assertEqual(sorted(ledger_ids), sorted(per_event))  # 一事件一行
        delivered = [r['event_id'] for r in core.repo._all(
            "SELECT event_id FROM companion_batch_events WHERE batch_id=? AND state='delivered'",
            (first[0]['batch_id'],))]
        self.assertEqual(sorted(ledger_ids), sorted(delivered))

    def test_coverage_times_are_readable(self):
        """F7：coverage 不能只给裸 epoch。"""
        core = self.core()
        now = time.time()
        batches.record(core, [_event('awaited_result', 't1', at=now, payload=dict(task_ref='t1'))])
        batch = batches.select(core, now=now, conversation=False)[0]
        coverage = batch['coverage']
        self.assertIn('from_at_text', coverage)
        self.assertIn('to_at_text', coverage)
        self.assertRegex(coverage['from_at_text'],
                         r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}$')
        self.assertIn('timezone', coverage)
        self.assertIn('next_window_at_text', batch['unexpanded'])

    def test_forget_epoch_leaves_no_empty_shell_batch(self):
        """F9：遗忘纪元前进后，空的 pending 批次与事件都不留空壳。"""
        core = self.core()
        now = time.time()
        batches.record(core, [_event('daily_experience', 'g1', at=now)])
        self.assertTrue(batches.select(core, now=now, conversation=False))
        self.life.repo.bump_forget_epoch(core.scope.scope_id)
        self.assertEqual(batches.pending_events(core), [])
        self.assertEqual(batches.recent(core), [])
        self.assertEqual(core.repo._all("SELECT * FROM companion_batches WHERE state='pending'"), [])

    # ------------------------------------------------------------ 可配置
    def test_thresholds_come_from_resource_policy(self):
        core = self.core(policy=dict(conversation_window_seconds=99, max_events_per_batch=3))
        limits = batches.thresholds(core)
        self.assertEqual(limits['conversation_window_seconds'], 99)
        self.assertEqual(limits['max_events_per_batch'], 3)
        # 坏配置不丢事件：回落到默认值。
        broken = FakeCore(self.life, policy=dict(max_events_per_batch='很多'))
        self.assertEqual(batches.thresholds(broken)['max_events_per_batch'],
                         batches.DEFAULT_THRESHOLDS['max_events_per_batch'])


class WorldStateTest(unittest.TestCase):
    """4 级：状态变化下一次自然认知送；影响当前工作时升级为 3；谁改的不知道就写不知道。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='delivery-world-')
        self.addCleanup(self.temp.cleanup)
        self.life = LifeRuntime(Path(self.temp.name) / 'life.db')
        self.addCleanup(self.life.close)
        self.core = FakeCore(self.life)

    @staticmethod
    def _fact(state, *, observed='2026-11-20 22:00:00', detail=''):
        return dict(fact_ref='phone.link', subject='手机连接', state=state,
                    source_ref='world:phone', observed_at=observed, detail=detail)

    def test_first_read_only_establishes_baseline_then_changes_are_facts(self):
        now = time.time()
        self.core._world_facts = lambda: [self._fact('ready', detail='刚连上')]
        self.assertEqual(batches.events_from_world(self.core, now=now), [])
        self.core._world_facts = lambda: [self._fact('offline', detail='没有活动连接')]
        events = batches.events_from_world(self.core, now=now + 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['source_type'], 'state_change')
        self.assertIn('之前 ready，现在 offline', events[0]['detail'])
        self.assertIn('由谁改变：不知道', events[0]['detail'])
        # 同一状态不重复记（下一次自然认知不该反复叫她）。
        self.assertEqual(batches.events_from_world(self.core, now=now + 2), [])

    def test_state_change_during_work_escalates_to_awaited(self):
        now = time.time()
        self.core._world_facts = lambda: [self._fact('offline')]
        for purpose in ('dialogue', 'work'):
            self.core._meta['world_state_seen'] = {'phone.link': 'ready'}
            event = batches.events_from_world(self.core, purpose=purpose, now=now + 1)[0]
            if purpose == 'dialogue':
                during_chat = event
                self.assertEqual(batches.classify(during_chat, subscriptions=[]), batches.STATE)
            else:
                during_work = event
        self.assertTrue(during_work['payload']['affects_current_task'])
        self.assertEqual(batches.classify(during_work, subscriptions=[]), batches.AWAITED)


class AudienceBoundaryTest(unittest.TestCase):
    """采集边界：别的会话的投递、别的受众的日常经历、别的 scope 的子任务都不进这一批。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='delivery-audience-')
        self.addCleanup(self.temp.cleanup)
        self.life = LifeRuntime(Path(self.temp.name) / 'life.db')
        self.addCleanup(self.life.close)

    def test_delivery_fact_from_another_conversation_is_not_leaked(self):
        core = FakeCore(self.life, conversation='qq:private:1')
        mid = ledger.message_id('turn-x', 0)
        ledger.queue(core.repo, mid=mid, agent_id=core.life.agent_id, turn_id='turn-x', segment=0,
                     text='发给别人的话', conversation_ref='qq:private:2', channels=['qq'], now=1000.0)
        ledger.mark_unknown(core.repo, mid, now=1001.0)
        self.assertEqual(batches.events_from_delivery(core, now=1002.0), [])
        # 同一条投递在它自己的会话里仍然是可见的事实。
        own = FakeCore(self.life, conversation='qq:private:2')
        self.assertEqual(len(batches.events_from_delivery(own, now=1003.0)), 1)

    def test_daily_experience_only_collected_in_owner_private(self):
        private = FakeCore(self.life, conversation='qq:private:1')
        private.cfg = SimpleNamespace(resource_policy={}, owner=1)
        private._today_material = lambda: [dict(id='a1', kind='walk', text='在河边走了很久',
                                                source_refs=['r1'], at='2026-11-20 10:00:00')]
        self.assertEqual(len(batches.events_from_today(private)), 1)
        group = FakeCore(self.life, conversation='qq:group:90')
        group.cfg = SimpleNamespace(resource_policy={}, owner=1)
        group._today_material = private._today_material
        self.assertEqual(batches.events_from_today(group), [])

    def test_agent_task_from_another_scope_stays_out(self):
        core = FakeCore(self.life, conversation='qq:private:1')
        repo = self.life.repo
        with repo.tx():
            repo._exec('CREATE TABLE IF NOT EXISTS companion_agent_tasks(id TEXT PRIMARY KEY,'
                       ' agent TEXT, task_ref TEXT, status TEXT, verification TEXT, goal TEXT,'
                       ' attempt INTEGER, updated REAL, scope_id TEXT)')
            repo._exec("INSERT INTO companion_agent_tasks VALUES"
                       "('a1','default','t1','completed','verified','别人的事',1,1000.0,?)",
                       ('default|qq:private:2',))
        self.assertEqual(batches.events_from_agent_tasks(core), [])
        own = FakeCore(self.life, conversation='qq:private:2')
        self.assertEqual(len(batches.events_from_agent_tasks(own)), 1)

    def test_agent_level_task_is_marked_agent_not_an_empty_scope(self):
        """F8：没有会话的子任务显式写 agent 级，不让空串冒充。"""
        core = FakeCore(self.life, conversation='qq:private:1')
        repo = self.life.repo
        with repo.tx():
            repo._exec('CREATE TABLE IF NOT EXISTS companion_agent_tasks(id TEXT PRIMARY KEY,'
                       ' agent TEXT, task_ref TEXT, status TEXT, verification TEXT, goal TEXT,'
                       ' attempt INTEGER, updated REAL, scope_id TEXT)')
            repo._exec("INSERT INTO companion_agent_tasks VALUES"
                       "('a9','default','t9','running','pending','agent 级的活',1,1000.0,?)",
                       (batches.AGENT_SCOPE,))
        events = batches.events_from_agent_tasks(core)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['payload']['task_scope'], 'agent')
        self.assertEqual(events[0]['payload']['task_scope_id'], batches.AGENT_SCOPE)
        self.assertEqual(events[0]['source_ref'], 't9')


class AwarenessBatchesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='delivery-awareness-')
        self.addCleanup(self.temp.cleanup)
        self.life = LifeRuntime(Path(self.temp.name) / 'life.db')
        self.addCleanup(self.life.close)
        self.core = FakeCore(self.life)

    def test_awareness_reports_recent_batches_and_unexpanded(self):
        from life import companion_awareness as awareness
        now = time.time()
        batches.record(self.core, [
            _event('awaited_result', 'x1', detail='结果到了', at=now, payload=dict(task_ref='t1')),
            _event('index_progress', 'x2', detail='词法 ready', at=now),
        ])
        selected = batches.select(self.core, now=now, conversation=False)
        self.assertTrue(selected)
        batches.deliver(self.core, selected, 'turn-1')
        batches.record(self.core, [_event('state_change', 'x3', detail='手机断开了', at=now + 1)])
        summary = awareness._batch_summary(self.core, {'batches': selected}, now)
        self.assertEqual(summary['this_turn'], [item['batch_id'] for item in selected])
        self.assertGreaterEqual(summary['unexpanded']['count'], 1)
        self.assertTrue(summary['recent'])
        self.assertIn('不是内容总量', summary['meaning'])
        # memory op=awareness 的摘要里能读到「最近批次 N 批 / 未展开 M 件」。
        projection = dict(batches=summary, here=dict(conversation_ref='qq:private:1'),
                          self=dict(account=dict(state='verified')),
                          runtime=dict(available_actions=[]), capabilities=dict(total=0, usable=[]),
                          attention=[], agent_tasks=[], observed_at='now')
        reply = awareness.read(self.core, dict(op='awareness'), projection=projection)
        self.assertEqual(reply['status'], 'completed')
        self.assertIn('未展开', reply['output'])
        self.assertIn('最近批次', reply['output'])


class CompanionWiringTest(unittest.TestCase):
    """接进 Companion：装载→（模型成功）记账；失败不记账，事件仍在 pending。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='delivery-wiring-')
        self.home = Path(self.temp.name)
        (self.home / 'data').mkdir(parents=True, exist_ok=True)
        self._cores = []

    def tearDown(self):
        for core in self._cores:
            try:
                core.life.close()
            except Exception:  # noqa: BLE001
                pass
        self.temp.cleanup()

    def core(self):
        from life.companion import Companion
        cfg = SimpleNamespace(
            name='测试小鱼', owner=10001, life_db='data/life.db', life_agent='default',
            api_key='', base_url='https://api.deepseek.com', model='deepseek-flash',
            persona='persona.md', unified_persona=False, public_persona='personas/public.md',
            resource_policy=dict(delivery_max_batches_per_turn=6), spending_policy={},
            memory_index={}, embedding={}, life_dynamics={}, experience={},
            companion_autonomy=False, dsh_root='', dsh_node='', dsh_home='',
            dsh_provider='deepseek-official', media_library={}, local_media={},
            profile_backfill={}, memory_organize={}, activity={}, known_people={},
            friends=[], allow_groups=[], bot_qq=10001, quiet_hours='', handoff_seconds=10,
            typing_chars_per_second=3, paste_threshold=1800, media_max_mb=64,
            media_local_roots=[], action_per_hour=500, action_per_day=0,
            acknowledge_before_work=True, bili_plugin='', local_embedding={},
            experience_private={})
        cfg.service = SimpleNamespace(raw=None)

        class _Model:
            MAX_OUTPUT_TOKENS = 8000

            async def complete(self, messages, json_mode=True, purpose='', schema=None):
                return dict(message=dict(content='{}'), finish_reason='completed',
                            usage=None, model='fake')

            async def close(self):
                return None

        core = Companion(cfg, self.home, model=_Model())
        self._cores.append(core)
        return core

    def _source(self, core, text='看看手上的事'):
        event, _ = core.life.ingest(core.scope, 'runtime_wake', 'inference', text,
                                    payload=dict(wake_kind='timer'))
        return event

    def test_prepare_then_settle_writes_delivery_ledger_only_on_success(self):
        core = self.core()
        now = time.time()
        batches.subscribe(core, 'index_progress', notify=True)
        source = self._source(core)
        selected = core._prepare_batches(source, 'activity', None)
        self.assertTrue(selected)
        self.assertTrue(any(item['level'] in (batches.AWAITED, batches.BACKGROUND)
                            for item in selected))
        self.assertGreaterEqual(len(batches.pending_events(core)), 1)
        delivered = core._settle_batches(source.id)
        self.assertTrue(delivered)
        row = core.repo._one('SELECT model_ok FROM companion_batch_deliveries WHERE decision_id=?',
                             (source.id,))
        self.assertEqual(int(row['model_ok']), 1)
        self.assertEqual(batches.pending_events(core), [])
        # 第二批：失败路径不写「已处理」。
        batches.record(core, [_event('state_change', 'later', detail='手机又断开了', at=now + 1)])
        core._prepare_batches(source, 'activity', None)
        self.assertTrue(core._turn_batches)
        core._drop_batches(source.id, reason='模型请求失败')
        self.assertTrue(batches.pending_events(core))
        self.assertEqual(batches.recent(core)[0]['state'], 'pending')


    def test_batch_lands_in_the_real_request_and_ledger_names_the_events(self):
        """本轮真的装进请求的批次与事件 id，要和台账里记的是同一批。"""
        core = self.core()
        (self.home / 'persona.md').write_text('You are a curious character.', encoding='utf-8')
        (self.home / 'personas').mkdir(exist_ok=True)
        (self.home / 'personas' / 'public.md').write_text('Public persona.', encoding='utf-8')
        source = self._source(core)
        selected = core._prepare_batches(source, 'activity', None)
        self.assertTrue(selected)
        messages = core._messages(None, source.text, None, source_ref=source.id, purpose='activity',
                                  wake_kind='timer', attention=[], batches=selected)
        material = messages[1]['content']
        for batch in selected:
            self.assertIn(batch['batch_id'], material)
            for event in batch['events']:
                self.assertIn(event['event_id'], material)
        self.assertEqual([item['batch_id'] for item in core._turn_batches],
                         [item['batch_id'] for item in selected])
        core._settle_batches(source.id)
        rows = core.repo._all('SELECT batch_id,event_ids FROM companion_batch_deliveries')
        self.assertEqual(len(rows), len(selected))
        expected = {event['event_id'] for item in selected for event in item['events']}
        recorded = {event_id for row in rows for event_id in json.loads(row['event_ids'])}
        self.assertEqual(recorded, expected)

    def test_awareness_this_turn_reflects_the_loaded_batches(self):
        """F3：请求体里的 awareness.batches.this_turn 必须是本轮实际装载的批次，不能恒空。"""
        core = self.core()
        (self.home / 'persona.md').write_text('You are a curious character.', encoding='utf-8')
        (self.home / 'personas').mkdir(exist_ok=True)
        (self.home / 'personas' / 'public.md').write_text('Public persona.', encoding='utf-8')
        source = self._source(core)
        selected = core._prepare_batches(source, 'activity', None)
        self.assertTrue(selected)
        messages = core._messages(None, source.text, None, source_ref=source.id, purpose='activity',
                                  wake_kind='timer', attention=[], batches=selected)
        material = messages[1]['content']
        payload = material.split('当前生活材料：', 1)[1]
        state = json.loads(payload)
        this_turn = state['awareness']['batches']['this_turn']
        self.assertEqual(this_turn, [item['batch_id'] for item in selected])
        self.assertTrue(this_turn)


if __name__ == '__main__':
    unittest.main()
