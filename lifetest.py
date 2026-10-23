#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""lifetest.py —— 不用 QQ、不用 API key，验证《生命感与可维护性》第一阶段。

覆盖设计第 13 节的机制验收里能在离线跑的部分：
T01/T02/T03/T04/T06/T07/T10/T12/T14/T25/T26，以及遗忘屏障、投影幂等。

    .\.venv\Scripts\python.exe lifetest.py
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from life import LifeRuntime
from life.memory import MemoryConfig
from life.runtime import LifeConfig
from life.state import AffectConfig
from life.contracts import OpenLoop, RelationEvidence
from life.loops import transition as loop_transition
from life.daily import run_diary_job
from life.migrate import migrate_brain
from life.capability import (
    CapabilityGap, EvolutionProposal, advance_gap, advance_proposal,
    can_auto_publish, record_gap, record_proposal, required_grant_missing,
)
from life.study import StudyThread, advance_study, record_study
from life.actions import ActionSpec, ActionResult, execute_action, result_is_proven
from life.autonomy import AutonomyConfig, AutonomyEngine

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def fresh() -> tuple[LifeRuntime, str]:
    path = os.path.join(tempfile.mkdtemp(prefix="life-"), "life.db")
    cfg = LifeConfig(memory=MemoryConfig(token_budget=1200),
                     affect=AffectConfig(event_cap=0.12))
    return LifeRuntime(path, agent_id="bot1", config=cfg), path


def main() -> int:
    print("== 1. Phase A：事件幂等与轮次 ==")
    rt, path = fresh()
    s = rt.scope("qq:bot1:private:1", "1")
    ev, created = rt.ingest_message(s, "你好", external_id="m1")
    check("T01 首次入库", created is True)
    for _ in range(10):
        _, again = rt.ingest_message(s, "你好", external_id="m1")
    check("T01 同一外部 ID 只入库一次", again is False)
    check("T01 事件数=1", rt.repo.stats()["events"] == 1,
          str(rt.repo.stats()))
    turn1 = rt.ensure_turn(s, ev.id)
    turn2 = rt.ensure_turn(s, ev.id)
    check("T01 同一输入只归属一个轮次", turn1.id == turn2.id)
    check("T01 轮次数=1", rt.repo.stats()["turns"] == 1)

    print("== 2. Phase A：不同消息、不同会话隔离 ==")
    e2, c2 = rt.ingest_message(s, "你好", external_id="m2")
    check("T02 相同文本不同 ID 是两条事件", c2 is True and e2.id != ev.id)
    sB = rt.scope("qq:bot1:private:2", "2")
    rt.ingest_message(sB, "我是另一个人", external_id="m3")
    rt.memory.remember(s, "fact", "他喜欢喝茶", [ev.id], status="active",
                       subject_id="1", predicate="likes", normalized_value="茶")
    gotA = rt.context_block(s, "茶")
    gotB = rt.context_block(sB, "茶")
    check("T03 A 的检索能看到自己的事实", "喝茶" in gotA)
    check("T03 B 的检索看不到 A 的事实", "喝茶" not in gotB)
    sGroup = rt.scope("qq:bot1:group:99", "1")
    check("T04 同一人在群聊看不到私聊记忆", "喝茶" not in rt.context_block(sGroup, "茶"))

    print("== 3. Phase B：情绪推进与上限 ==")
    praise, _ = rt.ingest_message(s, "你真棒，谢谢你", external_id="m4")
    out = rt.appraise(s, praise)
    check("T12 有事件评估", out is not None)
    if out:
        app, change = out
        cap = max(abs(v) for v in change.applied_stimulus_delta.values())
        check("T12 单事件刺激不越上限", cap <= 0.12 + 1e-9, f"{cap}")
        check("T12 衰减/刺激/总变化分开记录",
              set(change.decay_delta) == set(change.stimulus_delta))
        check("T12 有原因记录", any(c.get("strength") for c in change.state.causes))
    check("T01 同一事件情绪只生效一次", rt.appraise(s, praise) is None)
    # 高敏感度也不越上限
    from life.state import DesireState, reduce_affect
    from life.contracts import AffectState
    st = AffectState(scope=s, intensity={k: 0.9 for k in
                    ("joy", "concern", "frustration", "hurt", "anticipation")})
    from life.state import heuristic_appraisal
    ap = heuristic_appraisal("你真好，谢谢你", s, [praise.id], "1")
    ch = reduce_affect(st, ap, st.updated_at + 1, AffectConfig(event_cap=0.12),
                       {"贪婪": 10, "色欲": 10, "嫉妒": 10})
    check("T12 高欲望下仍不越上限",
          max(abs(v) for v in ch.applied_stimulus_delta.values()) <= 0.12 + 1e-9)

    print("== 4. Phase C：投影、事实修订、未完成事项 ==")
    ev5, _ = rt.ingest_message(s, "提醒我明天交报告，另外记住：我喜欢茶", external_id="m5")
    rt.appraise(s, ev5)
    r1 = rt.project(s)
    check("T06 投影产出经历", r1["episodes"] >= 1, str(r1))
    check("T06 明确记忆产出事实", r1["facts"] >= 1, str(r1))
    check("T06 请求产出未完成事项", len(rt.repo.active_open_loops(s)) >= 1)
    r2 = rt.project(s)
    check("T06 没有新事件时投影为空", r2["episodes"] == 0 and r2["facts"] == 0)
    # 事实变化：以前咖啡 -> 现在茶
    f1 = rt.memory.revise_fact(s, "1", "likes", "咖啡", [ev5.id],
                               summary="他喜欢咖啡", explicit_change=True)
    ev6, _ = rt.ingest_message(s, "我现在改喝茶了", external_id="m6")
    f2 = rt.memory.revise_fact(s, "1", "likes", "茶", [ev6.id],
                               summary="他现在喜欢茶", explicit_change=True)
    old = rt.repo.get_memory(f1.id)
    check("T10 偏好变化旧版被 superseded", old is not None and old.status == "superseded")
    check("T10 新版是 active", rt.repo.get_memory(f2.id).status == "active")

    print("== 5. Phase C：读取无副作用、预算、遗忘 ==")
    rt.memory.remember(s, "fact", "他住在杭州", [ev5.id], status="active",
                       subject_id="1", predicate="city", normalized_value="杭州")
    before = [(m.id, m.mention_count_window) for m in rt.repo.recent_memories(
        s, ["active", "disputed"], 50)]
    for _ in range(100):
        rt.context_block(s, "杭州 茶 报告")
    after = [(m.id, m.mention_count_window) for m in rt.repo.recent_memories(
        s, ["active", "disputed"], 50)]
    check("T14/T25 读一百次不改变重要性/提及计数", before == after)
    tight = MemoryConfig(token_budget=12)
    rt.memory.cfg = tight
    packet = rt.memory.retrieve(s, "杭州 茶 报告 咖啡", budget=12)
    check("T26 检索包不超预算", packet.token_estimate <= 12,
          f"{packet.token_estimate}")
    # 遗忘
    target = rt.repo.recent_memories(s, ["active"], 1)[0]
    res = rt.forget(s, memory_ids=[target.id])
    check("T23 遗忘推进 epoch", res["epoch"] >= 1)
    check("T23 遗忘后不可检索",
          all(m.id != target.id for m in rt.repo.search_memories(
              s, ["杭州"], ["active", "disputed"], 20)))

    print("== 6. 重启后的幂等 ==")
    rt.close()
    rt2 = LifeRuntime(path, agent_id="bot1")
    s2 = rt2.scope("qq:bot1:private:1", "1")
    ev_again, created_again = rt2.ingest_message(s2, "你好", external_id="m1")
    check("T01 重启后同一外部 ID 仍去重", created_again is False)
    check("T01 重启后事件数不增加", rt2.repo.stats()["events"] == 6,
          str(rt2.repo.stats()))
    rt2.close()

    print("== 7. 结构化评估 / 关系 / 心境 ==")
    rt3, _path3 = fresh()
    s3 = rt3.scope("qq:bot1:private:9", "9")
    ev9, _ = rt3.ingest_message(s3, "谢谢你，今天辛苦了", external_id="x1")
    raw = {
        "source_event_ids": [ev9.id], "event_type": "praise",
        "certainty": 0.8, "proposed_affect_delta": {"joy": 0.4},
        "relation_evidence": {"trust": 0.1},
    }
    out9 = rt3.appraise_structured(s3, ev9, raw)
    check("结构化评估被接受", out9 is not None)
    check("关系证据推进信任", rt3.relationship(s3).trust > 0.5,
          str(rt3.relationship(s3).trust))
    bad = dict(raw)
    bad["source_event_ids"] = ["fake_event_id"]
    check("伪造来源被拒", rt3.appraise_structured(s3, ev9, bad) is None)
    check("心境有读数", rt3.update_mood(s3) is not None)
    s3b = rt3.scope("qq:bot1:private:10", "10")
    ev10 = RelationEvidence(id="rel1", scope=s3b, kind="trust", delta=0.2,
                            confidence=0.9, root_event_ids=["relroot1"],
                            independent_experience_group="g1")
    ev11 = RelationEvidence(id="rel2", scope=s3b, kind="trust", delta=0.2,
                            confidence=0.9, root_event_ids=["relroot1"],
                            independent_experience_group="g1")
    rel_before = rt3.relationship(s3b).trust
    rel_after, _deltas = rt3.apply_relation_evidence(s3b, [ev10, ev11])
    check("同一真实经历只结算一次",
          abs((rel_after.trust - rel_before) - 0.1) < 0.02,
          f"{rel_before}->{rel_after.trust}")

    print("== 8. OpenLoop 状态机 ==")
    loop = OpenLoop(id="loop1", scope=s3, kind="request", summary="交报告")
    loop = loop_transition(loop, "ready")
    loop = loop_transition(loop, "running")
    try:
        loop_transition(loop, "succeeded")
        check("succeeded 必须有证据", False)
    except ValueError:
        check("succeeded 必须有证据", True)
    loop = loop_transition(loop, "succeeded", evidence_ids=["e1"])
    check("正常完成", loop.status == "succeeded")
    commit = OpenLoop(id="loop2", scope=s3, kind="commitment", summary="答应画图")
    try:
        loop_transition(commit, "retired")
        check("承诺不能静默退休", False)
    except ValueError:
        check("承诺不能静默退休", True)

    print("== 9. 每日日记作业 ==")
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    rt3.ingest_message(s3, "今天一起看了月亮", external_id="d1")
    rt3.ingest_message(s3, "嗯，很好看", external_id="d2", role="assistant")
    diary = run_diary_job(rt3, s3, today)
    check("当天日记写出来", diary is not None)
    check("同一天不会写第二篇", run_diary_job(rt3, s3, today) is None)

    print("== 10. 知识卡 ==")
    card = rt3.add_knowledge(
        s3, "月相", [{"text": "月食发生在满月附近", "source_event_ids": [ev9.id]}],
        [ev9.id], support_state="tested_in_scope",
        applicability="仅限基础天文")
    check("知识卡落库", rt3.knowledge.get_card(card.id) is not None)
    check("知识进入检索", any(m.kind == "knowledge"
          for m in rt3.repo.recent_memories(s3, ["active", "candidate"], 50)))
    try:
        rt3.add_knowledge(s3, "坏卡", [{"text": "x", "source_event_ids": [ev9.id]}],
                          [ev9.id], support_state="not_a_state")
        check("非法支持状态被拒", False)
    except ValueError:
        check("非法支持状态被拒", True)

    print("== 11. 能力缺口 / 进化提案 / 学习线程 ==")
    gap = CapabilityGap(id="gap1", scope=s3, normalized_need="解析 docx",
                        acceptance_checks=["能读出正文"])
    record_gap(rt3.repo, gap)
    advance_gap(gap, "investigating")
    check("缺口进入调查", gap.status == "investigating")
    proposal = EvolutionProposal(
        id="prop1", gap_id=gap.id, scope=s3, kind="code",
        requested_effects=["modify_code", "publish_release"],
        acceptance_checks=["原任务成功"])
    record_proposal(rt3.repo, proposal)
    missing = required_grant_missing(proposal, {"modify_code"})
    check("高风险效果要单独授权", missing == ["publish_release"], str(missing))
    check("没有回退不能自动发布",
          can_auto_publish(proposal, {"modify_code", "publish_release"},
                           evaluation_passed=True, rollback_ready=False) is False)
    advance_proposal(proposal, "prepared")
    check("提案可推进", proposal.status == "prepared")
    study = StudyThread(id="study1", scope=s3, topic="月相",
                        specific_question="月相和月食的区别")
    record_study(rt3.repo, study)
    advance_study(study, "researching")
    advance_study(study, "consolidating")
    check("学习线程推进", study.status == "consolidating")
    try:
        advance_study(study, "planned")
        check("学习线程拒绝非法回退", False)
    except ValueError:
        check("学习线程拒绝非法回退", True)
    rt3.close()

    print("== 12. 旧库迁移幂等 ==")
    tmpdir = tempfile.mkdtemp(prefix="brain-")
    old_db = os.path.join(tmpdir, "brain.db")
    conn = sqlite3.connect(old_db)
    conn.executescript(
        "CREATE TABLE msg(id INTEGER PRIMARY KEY, peer INTEGER, role TEXT, content TEXT, ts INTEGER);"
        "CREATE TABLE fact(id INTEGER PRIMARY KEY, peer INTEGER, content TEXT, ts INTEGER);"
        "CREATE TABLE diary(id INTEGER PRIMARY KEY, peer INTEGER, day TEXT, content TEXT, ts INTEGER);"
    )
    now = int(datetime.datetime.now().timestamp())
    conn.execute("INSERT INTO msg(peer,role,content,ts) VALUES(1,'user','旧消息一',?)", (now,))
    conn.execute("INSERT INTO msg(peer,role,content,ts) VALUES(1,'assistant','旧消息二',?)", (now,))
    conn.execute("INSERT INTO fact(peer,content,ts) VALUES(1,'他喜欢茶',?)", (now,))
    conn.execute("INSERT INTO diary(peer,day,content,ts) VALUES(1,'2026-10-20','旧日记',?)", (now,))
    conn.commit(); conn.close()
    rt4 = LifeRuntime(os.path.join(tmpdir, "life.db"), agent_id="bot1")
    dry = migrate_brain(old_db, rt4, agent_id="bot1", dry_run=True)
    check("迁移 dry-run 只计数", dry.messages == 2 and rt4.repo.stats()["events"] == 0)
    first = migrate_brain(old_db, rt4, agent_id="bot1", dry_run=False)
    check("迁移写入事件", first.messages == 2 and rt4.repo.stats()["events"] == 2)
    check("旧事实/日记变候选", rt4.repo.stats()["memories"] == 2)
    second = migrate_brain(old_db, rt4, agent_id="bot1", dry_run=False)
    check("重复迁移全部跳过", second.skipped >= 4 and rt4.repo.stats()["events"] == 2,
          str(second))
    rt4.close()

    print("== 13. 动作结果验证 ==")
    rt5, _p5 = fresh()
    s5 = rt5.scope("qq:bot1:private:11", "11")
    spec = ActionSpec(id="act1", scope=s5, kind="send_message",
                      recipient_or_resource="private:1",
                      postconditions=["目标会话出现这句话"])
    calls = {"n": 0}

    def bad_adapter(_spec):
        calls["n"] += 1
        return ActionResult(action_id="act1", status="succeeded")

    bad = asyncio.run(execute_action(spec, bad_adapter, rt5.repo,
                                     granted_effects={"send_message"}))
    check("没有后置条件不算完成", result_is_proven(spec, bad) is False)
    again = asyncio.run(execute_action(spec, bad_adapter, rt5.repo,
                                       granted_effects={"send_message"}))
    check("同一 action+spec 不重复执行",
          calls["n"] == 1 and again.status == "unknown", again.status)

    def boom(_spec):
        raise TimeoutError("网络超时")

    spec2 = ActionSpec(id="act2", scope=s5, kind="send_message",
                       recipient_or_resource="private:1",
                       postconditions=["目标会话出现这句话"])
    unknown = asyncio.run(execute_action(spec2, boom, rt5.repo,
                                        granted_effects={"send_message"}))
    check("异常一律 unknown", unknown.status == "unknown")

    denied = asyncio.run(execute_action(
        ActionSpec(id="act3", scope=s5, kind="install_plugin",
                   requested_effects=["install_plugin"]),
        boom, rt5.repo, granted_effects=set()))
    check("未授权的高风险动作不执行", denied.error_category == "waiting_permission")
    rt5.close()

    print("== 14. 自主工作器：插件 / 自修改 / 学习 ==")
    rt6, _p6 = fresh()
    s6 = rt6.scope("qq:private:66", "66", "qq:private:66")
    engine = AutonomyEngine(rt6, AutonomyConfig(allow_install=True, allow_selfmod=True,
                                               allow_study=True), grants=set())
    gap = CapabilityGap(id="gapA", scope=s6, normalized_need="解析 docx",
                        acceptance_checks=["能读出正文"])
    rt6.record_gap(gap)
    wait = asyncio.run(engine.run_plugin_gap(
        gap, discover=lambda q: [], install=lambda c: {"ok": True},
        verify=lambda c: {"ok": True}, activate=lambda c: {"ok": True}))
    check("插件自主：缺授权就等", wait.status == "waiting", wait.detail)

    engine2 = AutonomyEngine(rt6, AutonomyConfig(allow_install=True, allow_selfmod=True,
                                                allow_study=True),
                             grants={"install_plugin", "enable_plugin",
                                     "modify_code", "publish_release"})
    gap2 = CapabilityGap(id="gapB", scope=s6, normalized_need="解析 pdf",
                        acceptance_checks=["能读出正文"])
    rt6.record_gap(gap2)
    done = asyncio.run(engine2.run_plugin_gap(
        gap2,
        discover=lambda q: [{"kind": "pypi", "name": "pdfx", "install": "pip install pdfx"}],
        install=lambda c: {"ok": True, "evidence_ids": ["ev1"]},
        verify=lambda c: {"ok": True, "evidence_ids": ["ev2"]},
        activate=lambda c: {"ok": True, "evidence_ids": ["ev3"]}))
    check("插件自主：发现→装→验→用", done.status == "done", done.detail)

    proposal = EvolutionProposal(id="propB", gap_id="gapB", scope=s6, kind="code",
                                hypothesis="修一个 bug",
                                requested_effects=["modify_code", "publish_release"],
                                acceptance_checks=["测试通过"], status="prepared")
    record_proposal(rt6.repo, proposal)
    fail = asyncio.run(engine2.run_selfmod(
        proposal,
        propose_patch=lambda p: {"file": "x.py", "old": "a", "new": "b"},
        apply_patch=lambda p, patch: {"ok": True, "edit_id": "edit1"},
        run_tests=lambda stage: {"ok": False if stage == "baseline" else True},
        publish=lambda p, eid: {"ok": True, "evidence_id": eid},
        rollback=lambda eid: {"ok": True}))
    check("自修改：基线不过就不动", fail.status == "rejected", fail.detail)

    proposal2 = EvolutionProposal(id="propC", gap_id="gapB", scope=s6, kind="code",
                                 hypothesis="修另一个 bug",
                                 requested_effects=["modify_code", "publish_release"],
                                 acceptance_checks=["测试通过"], status="prepared")
    record_proposal(rt6.repo, proposal2)
    rolled = {"n": 0}

    def rollback(eid):
        rolled["n"] += 1
        return {"ok": True}

    fail2 = asyncio.run(engine2.run_selfmod(
        proposal2,
        propose_patch=lambda p: {"file": "x.py", "old": "a", "new": "b"},
        apply_patch=lambda p, patch: {"ok": True, "edit_id": "edit2"},
        run_tests=lambda stage: {"ok": True if stage == "baseline" else False},
        publish=lambda p, eid: {"ok": True},
        rollback=rollback))
    check("自修改：候选测试失败会回滚", fail2.status == "rejected" and rolled["n"] == 1)

    study = StudyThread(id="studyB", scope=s6, topic="月相",
                        specific_question="月相和月食的区别", status="planned")
    rt6.record_study(study)
    learned = asyncio.run(engine2.run_study(
        study,
        search=lambda q: [{"title": "月相", "url": "https://example.com/moon"}],
        read=lambda url: "月食发生在满月附近，月相是日地月相对位置造成的。",
        extract=lambda t, obs: [{"text": "月食发生在满月附近",
                                 "source_event_ids": [obs[0]["event_id"]],
                                 "scope_note": "基础天文"}]))
    check("自主学习：写出知识卡", learned.status == "done", learned.detail)
    check("自主学习：知识进入检索", any(
        m.kind == "knowledge" for m in rt6.repo.recent_memories(s6, ["active", "candidate"], 20)))
    rt6.close()

    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
