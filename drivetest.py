#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drivetest.py —— drive_v2 内稳态/欲望离线验收。

覆盖交接 M1 的代表场景 E01–E05、E09、E19–E24，以及用户特别要求的
「亲密」与「性欲」必须为两项独立指标。不连接 QQ、模型或 DSH。
"""
from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(ROOT))

from life import LifeRuntime
from life.contracts import clamp
from life.drives import (
    DRIVE_DIMENSIONS, DriveConfig, DriveEvidence, DriveStateSet, ContactState,
    advance_to, apply_one_evidence, default_drive_dynamics, drive_view,
    initial_state, may_propose_contact, passive_growth, summarize,
)
from life.persona import compile_persona
from life.runtime import LifeConfig

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class Clock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def add(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t


def fresh(clock: Clock | None = None):
    path = os.path.join(tempfile.mkdtemp(prefix="drive-"), "life.db")
    clock = clock or Clock()
    rt = LifeRuntime(path, agent_id="bot1",
                     config=LifeConfig(drives=DriveConfig()), clock=clock)
    return rt, path, clock


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(float(a) - float(b)) <= tol


def main() -> int:
    print("== A. 维度与配置：亲密 / 性欲独立 ==")
    cfg = DriveConfig()
    check("A1 八项需要都在", set(cfg.dimensions) == set(DRIVE_DIMENSIONS),
          str(list(cfg.dimensions)))
    check("A2 亲密与性欲是两项", "intimacy" in cfg.dimensions and "libido" in cfg.dimensions
          and cfg.dimensions["intimacy"] is not cfg.dimensions["libido"])
    check("A3 两项有独立参数", cfg.dimensions["intimacy"].base_growth_per_hour
          != cfg.dimensions["libido"].base_growth_per_hour)
    start = initial_state("s", cfg, at=0)
    check("A4 初始状态各有独立槽位", len(start.drives) == len(DRIVE_DIMENSIONS))
    check("A5 亲密与性欲初始值独立", start.drives["intimacy"].tension
          != start.drives["libido"].tension)

    print("== B. E01/E19 增长：一次一小时 vs 六十次一分钟 ==")
    once = advance_to(start, 3600, cfg, online_seconds=3600)
    many = start
    for i in range(60):
        many = advance_to(many, 60 * (i + 1), cfg, online_seconds=60)
    diffs = {d: abs(once.drives[d].tension - many.drives[d].tension) for d in DRIVE_DIMENSIONS}
    check("E01 八维都在 1e-6 容差内", max(diffs.values()) <= 1e-6, str(diffs))
    for dim in DRIVE_DIMENSIONS:
        c = cfg.dynamics(dim)
        if c.mode != "accumulating" or c.base_growth_per_hour <= 0:
            continue
        # 参考强度处 1 秒导数的数值近似，应等于 base_growth_per_hour
        x = c.reference_tension
        slope = (passive_growth(x, c, 1.0) - x) * 3600.0
        check(f"E19 {dim} 参考涨幅≈配置", abs(slope - c.base_growth_per_hour) < 5e-5,
              f"{slope:.6f} vs {c.base_growth_per_hour}")

    print("== C. E03/E04/E05/E24 证据方向、满足与去重 ==")
    rt, path, clock = fresh()
    s = rt.scope("qq:private:1", "1", "qq:private:1")
    ev, _ = rt.ingest_message(s, "抱抱我", external_id="m1")
    base_view = rt.drive_view(s, now=clock.t)
    before = base_view["intimacy"]["activation"]
    st, settlements = rt.apply_drive_evidence(s, [DriveEvidence(
        id="act1", dimension="intimacy", kind="activation_change", delta=-0.10,
        root_id=ev.id, sources=[ev.id], confidence=1.0)], now=clock.t)
    after_view = rt.drive_view(s, now=clock.t)
    check("E03 负向激活确实下降", after_view["intimacy"]["activation"] < before,
          f"{before} -> {after_view['intimacy']['activation']}")
    check("E03 无关维度不变", approx(after_view["libido"]["activation"], 0.0))

    sat_before = rt.drive_state(s)
    sat, s1 = rt.apply_drive_evidence(s, [DriveEvidence(
        id="sat1", dimension="intimacy", kind="interaction_satisfaction",
        delta=0.25, root_id=ev.id, sources=[ev.id], confidence=1.0,
        confirmed=True, real_participant=True, relationship_allows=True,
        participant_id="1")], now=clock.t)
    check("E04 互动满足降低 tension", sat.drives["intimacy"].tension
          < sat_before.drives["intimacy"].tension)
    check("E04 一次满足不改 learned_offset",
          approx(sat.drives["intimacy"].learned_offset, 0.0))
    check("E04 satiety 只做短期冷却", sat.drives["intimacy"].satiety > 0)
    check("E24 满足没有直接增加长期偏移",
          approx(sat.drives["intimacy"].learned_offset, sat_before.drives["intimacy"].learned_offset))
    dup, s2 = rt.apply_drive_evidence(s, [DriveEvidence(
        id="sat1", dimension="intimacy", kind="interaction_satisfaction",
        delta=0.25, root_id=ev.id, sources=[ev.id], confidence=1.0,
        confirmed=True, real_participant=True, relationship_allows=True,
        participant_id="1")], now=clock.t)
    check("E05 同一 root+维度+效果只结算一次", not s2 and approx(
        dup.drives["intimacy"].tension, sat.drives["intimacy"].tension))
    # 同 root 的另一个合法维度仍可结算
    two, s3 = rt.apply_drive_evidence(s, [DriveEvidence(
        id="act2", dimension="libido", kind="activation_change", delta=0.08,
        root_id=ev.id, sources=[ev.id], confidence=1.0)], now=clock.t)
    check("E05 同根事件的不同维度各一次", len(s3) == 1 and s3[0]["dimension"] == "libido")

    print("== D. E09 读取无副作用 ==")
    state_before = rt.drive_state(s)
    version_before = state_before.version
    for _ in range(100):
        rt.drive_view(s, now=clock.t + 3600)
        rt.status(s)
    state_after = rt.drive_state(s)
    check("E09 读一百次不写库、不结算", state_after.version == version_before,
          f"{version_before} -> {state_after.version}")

    print("== E. E20 时间边界与恢复期 ==")
    cfg2 = DriveConfig()
    cfg2.dimensions["libido"].recovery_hours = 2.0
    cfg2.dimensions["libido"].high_threshold = 0.60
    cfg2.dimensions["libido"].expression_threshold = 0.50
    cfg2.dimensions["libido"].high_dwell_hours = 1.0
    cfg2.dimensions["libido"].base_growth_per_hour = 0.20
    cfg2.dimensions["libido"].initial_tension = 0.55
    st0 = initial_state("s2", cfg2, at=0)
    # 先在线 1 小时跨入高位，再断线 2 小时，再在线 2 小时
    a = advance_to(st0, 3600, cfg2, online_seconds=3600)
    a = advance_to(a, 10800, cfg2, online_seconds=0)
    long = advance_to(a, 18000, cfg2, online_seconds=7200)
    split = a
    for i in range(120):
        split = advance_to(split, 10800 + 60 * (i + 1), cfg2, online_seconds=60)
    check("E20 跨恢复期一次推进=分段推进",
          abs(long.drives["libido"].tension - split.drives["libido"].tension) < 1e-6,
          f"{long.drives['libido'].tension} vs {split.drives['libido'].tension}")

    print("== F. E21/E22 高位、自我调节与重启 ==")
    cfg3 = DriveConfig()
    c = cfg3.dimensions["intimacy"]
    c.initial_tension = 0.795
    c.high_threshold = 0.795
    c.expression_threshold = 0.60
    c.high_dwell_hours = 0.5
    c.recovery_hours = 3.0
    c.regulation_relief_gain = 0.35
    st = initial_state("s3", cfg3, at=0)
    high = advance_to(st, 1800, cfg3, online_seconds=1800)
    check("E21 连续高位后产生调节候选", high.drives["intimacy"].regulation_due is True,
          str(high.drives["intimacy"].phase))
    rt3, path3, clock3 = fresh()
    s3 = rt3.scope("qq:private:3", "3", "qq:private:3")
    rt4, path4, clock4 = fresh()
    s4 = rt4.scope("qq:private:4", "4", "qq:private:4")
    cfg4 = DriveConfig()
    cfg4.dimensions["intimacy"].initial_tension = 0.95
    cfg4.dimensions["intimacy"].high_threshold = 0.80
    cfg4.dimensions["intimacy"].expression_threshold = 0.60
    cfg4.dimensions["intimacy"].high_dwell_hours = 0.5
    rt4.config.drives = cfg4
    st4 = initial_state(s4.scope_id, cfg4, at=clock4.t)
    rt4.repo.save_state(rt4.state_key('drive',s4), st4.to_dict())
    clock4.add(3600)
    rt4.advance_time(s4, now=clock4.t, online_seconds=3600)
    check("E21 runtime 识别调节候选", "intimacy" in rt4.regulation_candidates(s4, now=clock4.t))
    before = rt4.drive_state(s4).drives["intimacy"].tension
    relieved, settled = rt4.complete_internal_regulation(
        s4, "intimacy", evidence_id="reg1", now=clock4.t)
    check("E21 调节降低 tension", relieved.drives["intimacy"].tension < before)
    check("E21 进入恢复期", relieved.drives["intimacy"].phase == "recovering")
    again, settled2 = rt4.complete_internal_regulation(
        s4, "intimacy", evidence_id="reg1", now=clock4.t)
    check("E22 同一调节证据不重复结算", not settled2
          and approx(again.drives["intimacy"].tension, relieved.drives["intimacy"].tension))
    check("E21 内部调节不记为对方参与",
          all(row["effect_kind"] != "interaction_satisfaction"
              for row in rt4.repo.drive_settlements(s4.scope_id)))

    print("== G. E23 主动提出边界 ==")
    view = {"libido": {"tension": 0.95}}
    cfg5 = DriveConfig()
    now = 100000.0
    check("E23 高欲望可生成候选", may_propose_contact(view, "libido", cfg5, ContactState(), now=now))
    check("E23 明确拒绝后不提出", not may_propose_contact(
        view, "libido", cfg5, ContactState(explicit_refusal={"libido"}), now=now))
    check("E23 未回复不追问", not may_propose_contact(
        view, "libido", cfg5, ContactState(unanswered=True), now=now))
    check("E23 安静时不提出", not may_propose_contact(
        view, "libido", cfg5, ContactState(quiet_now=True), now=now))
    check("E23 冷却内不重复", not may_propose_contact(
        view, "libido", cfg5, ContactState(last_attempt_at={"libido": now - 3600}), now=now))

    print("== H. 人格：亲密与性欲分开抽取 ==")
    with tempfile.TemporaryDirectory() as persona_dir:
        fixture = Path(persona_dir) / 'persona.md'
        fixture.write_text('亲密：0.6\n性欲：0.5\n', encoding='utf-8')
        spec = compile_persona(fixture)
    check("H1 人格版本 v3", spec.compiler_version.startswith("persona-"))
    check("H2 同时含亲密与性欲偏好",
          "intimacy" in spec.drive_targets and "libido" in spec.drive_targets)
    check("H3 两项来源独立",
          spec.provenance["drive:intimacy"] != spec.provenance["drive:libido"])
    check("H4 人格不生成当前 tension", "tension" not in spec.drive_targets)

    print("== I. 持久化与状态查询 ==")
    summary = rt4.drive_summary(s4, now=clock4.t)
    check("I1 摘要含亲密和性欲两项", "亲密" in summary and "性欲" in summary)
    check("I2 状态接口含独立驱力", "drives" in rt4.status(s4)
          and "性欲" in rt4.status(s4)["drives"])
    rt4.close()
    rt5 = LifeRuntime(path4, agent_id="bot1", config=LifeConfig(drives=cfg4), clock=clock4)
    s5 = rt5.scope("qq:private:4", "4", "qq:private:4")
    reloaded = rt5.drive_state(s5)
    check("I3 重启后高位/恢复状态保存",
          reloaded.drives["intimacy"].phase == "recovering")
    check("I4 重启后不重复结算",
          len(rt5.repo.drive_settlements(s5.scope_id)) == 1)
    rt5.close()

    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
