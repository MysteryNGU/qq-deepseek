"""Executable numerical appendix to intimacy-state-model-design.md.

Standard-library only; no runtime imports, database access or external effects.
This checks formulae, not the future consent/source/delivery implementation.
Run from the repository root:
    py -3 docs/acceptance/intimacy_math_check.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path


def clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def impulse(arousal: float, delta: float, sensitivity: float, confidence=1.0) -> float:
    gain = sensitivity if delta > 0 else 1.0
    return clip(arousal + delta * confidence * gain)


def integrate_arousal(arousal: float, seconds: float, input_rate=0.0) -> float:
    coefficient = math.log(2.0) / 1800.0
    weight = -math.expm1(-coefficient * seconds)
    return clip(arousal * math.exp(-coefficient * seconds) + input_rate / coefficient * weight)


def round_parameters(count: int) -> dict:
    return dict(sensitivity=min(1.5, 1.0 + count * .06),
                threshold=min(.75, .55 + count * .015),
                interval_seconds=max(30.0, 120.0 * .9 ** count))


def release(arousal: float, denied: int, satiety=0.0) -> dict:
    factor = min(2.0, 1.0 + .25 * denied)
    amplitude = clip(arousal * factor, 0.0, 2.0)
    return dict(factor=factor, amplitude=amplitude, after=arousal * .4 ** factor,
                satiety=clip(satiety + .25 * amplitude))


def settle(arousal: float, satiety: float, elapsed: float) -> dict:
    duration = 60.0 + 60.0 * clip(arousal)
    target = clip(satiety + .2 * arousal)
    if elapsed >= duration:
        return dict(arousal=0.0, satiety=0.0, duration=duration)
    return dict(arousal=arousal * 2 ** (-elapsed / 90.0),
                satiety=target - (target - satiety) * math.exp(-elapsed / (duration / 3)),
                duration=duration)


def grow_tension(tension: float, seconds: float, gain=0.0) -> float:
    coefficient = .025 / (1.0 - .5) * (1.0 + gain)
    return 1.0 - (1.0 - tension) * math.exp(-coefficient * seconds / 3600.0)


def private_completion(raw_pressure: float, debt: float, gain: float) -> tuple:
    return min(raw_pressure, debt + .25), min(1.0, gain + .05)


def private_sensitivity_bonus(previous: float) -> float:
    return min(.5, previous + .02)


def effective_sensitivity(round_sensitivity: float, permanent_bonus: float) -> float:
    return min(1.5, round_sensitivity + permanent_bonus)


def effective_pressure(raw_pressure: float, debt: float, seconds: float) -> float:
    return clip(raw_pressure - debt * 2 ** (-seconds / 1800.0))


def verify() -> dict:
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def close(a, b):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)

    check('M01_positive_impulse_increases_with_round_gain',
          impulse(.1, .1, 1.06) > impulse(.1, .1, 1.0))
    check('M01_negative_impulse_is_not_reversed',
          close(impulse(.6, -.1, 1.5), .5))
    check('M01_saturation_keeps_arousal_bounded', close(impulse(.99, .25, 1.5), 1.0))
    check('M01_zero_confidence_has_no_impulse', close(impulse(.2, .1, 1.5, 0), .2))
    check('M23_zero_input_half_life', close(integrate_arousal(.6, 1800), .3))
    check('M23_fixed_response_segment_composition',
          close(integrate_arousal(.1, 120, .001),
                integrate_arousal(integrate_arousal(.1, 40, .001), 80, .001)))
    check('M23_long_input_remains_bounded', 0 <= integrate_arousal(.6, 100000, .0045) <= 1)
    # Only the numeric floor; a live stop must bypass this branch entirely.
    check('M06_pending_numeric_floor',
          close(max(.35, integrate_arousal(.6, 18000)), .35))

    rounds = [dict(completed_rounds=n, **round_parameters(n)) for n in (0, 1, 3, 6, 9, 14)]
    check('M09_round_one_parameters', all(close(rounds[1][key], expected)
          for key, expected in [('sensitivity', 1.06), ('threshold', .565), ('interval_seconds', 108)]))
    check('M09_long_run_round_bounds', all(
          .25 <= round_parameters(n)['sensitivity'] <= 1.5
          and .55 <= round_parameters(n)['threshold'] <= .75
          and 30 <= round_parameters(n)['interval_seconds'] <= 120
          for n in range(1000)))
    check('M09_all_caps_reached_by_round_14', round_parameters(14) == dict(
          sensitivity=1.5, threshold=.75, interval_seconds=30.0))

    releases = [dict(denied=n, **release(.6, n)) for n in (0, 1, 2, 4, 100)]
    check('M08_denial_factors', [r['factor'] for r in releases] == [1, 1.25, 1.5, 2, 2])
    check('M08_no_denial_keep_ratio', close(releases[0]['after'], .24))
    check('M08_denial_increases_amplitude', all(
          releases[i]['amplitude'] < releases[i+1]['amplitude'] for i in range(3)))
    check('M08_retained_arousal_positive_and_decreasing', all(
          0 < releases[i+1]['after'] < releases[i]['after'] for i in range(3)))
    check('M08_release_bounds_for_full_grid', all(
          0 <= release(a / 100, n)['amplitude'] <= 2
          and 0 <= release(a / 100, n)['after'] <= 1
          and 0 <= release(a / 100, n, .99)['satiety'] <= 1
          for a in range(101) for n in range(10)))

    duration = settle(.6, .2, 0)['duration']
    settling = [dict(elapsed=t, **settle(.6, .2, t))
                for t in (0, 30, 60, duration - 1e-6, duration)]
    check('M10_adaptive_duration_bounds', all(
          60 <= settle(a / 100, 0, 0)['duration'] <= 120 for a in range(101)))
    check('M10_settle_initial_values', close(settling[0]['arousal'], .6)
          and close(settling[0]['satiety'], .2))
    check('M10_positive_arousal_before_deadline', all(r['arousal'] > 0 for r in settling[:-1]))
    check('M10_satiety_monotone_before_deadline', all(
          settling[i]['satiety'] <= settling[i+1]['satiety'] for i in range(len(settling)-2)))
    check('M10_zero_arousal_is_not_invented', close(settle(0, .2, 30)['arousal'], 0))
    check('M10_reset_exactly_at_deadline', settling[-1]['arousal'] == settling[-1]['satiety'] == 0)
    check('M10_reset_remains_idempotent_after_deadline',
          settle(.6, .2, duration + 10) == settle(.6, .2, duration))

    debt, gain = private_completion(.7, 0, 0)
    pressures = [dict(elapsed_seconds=t, pressure=effective_pressure(.7, debt, t))
                 for t in (0, 1800, 3600)]
    check('M17_short_term_relief_and_rebound', all(close(r['pressure'], expected)
          for r, expected in zip(pressures, (.45, .575, .6375))))
    check('M17_relief_cannot_create_negative_pressure', effective_pressure(.1, .7, 0) == 0)
    check('M17_no_new_hidden_debt', private_completion(.1, .5, 0)[0] == .1)
    check('M18_first_completion_adds_five_percent_rate', close(gain, .05))
    for _ in range(100):
        debt, gain = private_completion(.7, debt, gain)
    check('M18_private_gain_and_debt_capped', gain == 1 and 0 <= debt <= .7)
    growth = [dict(gain=g, tension_after_hour=grow_tension(.5, 3600, g)) for g in (0, .05, 1)]
    check('M18_permanent_gain_increases_growth',
          growth[0]['tension_after_hour'] < growth[1]['tension_after_hour'] < growth[2]['tension_after_hour'])
    check('M18_tension_saturates_within_ceiling', all(
          0 <= grow_tension(x / 100, 1e9, g) <= 1 for x in range(101) for g in (0, .05, 1)))
    check('M23_drive_segment_composition', close(grow_tension(.5, 3600, .5),
          grow_tension(grow_tension(.5, 1200, .5), 2400, .5)))

    bonus=0.
    bonuses=[]
    for count in range(31):
        bonuses.append(dict(completed_private_runs=count,permanent_sensitivity_bonus=bonus,
            new_episode_sensitivity=effective_sensitivity(1.,bonus)))
        bonus=private_sensitivity_bonus(bonus)
    check('M18_private_sensitivity_first_completion',close(bonuses[1]['permanent_sensitivity_bonus'],.02))
    check('M18_private_sensitivity_reaches_cap_after_25',close(bonuses[25]['permanent_sensitivity_bonus'],.5))
    check('M18_private_sensitivity_stays_capped',all(
          0<=row['permanent_sensitivity_bonus']<=.5 and 1<=row['new_episode_sensitivity']<=1.5 for row in bonuses))
    check('M18_round_and_permanent_sensitivity_cannot_exceed_total_cap',
          effective_sensitivity(1.4,.3)==1.5)
    check('M18_new_episode_retains_private_sensitivity_effect',
          close(impulse(.1,.1,effective_sensitivity(1.,.02))-.1,.102))

    return dict(design_version='0.2', scope='numerical_formulae_only',
                runtime_integrated=False, checks_passed=len(checks), checks=checks,
                rounds=rounds, denial_release=releases, settling=settling,
                relief_rebound=pressures, permanent_rate_growth=growth,permanent_sensitivity=bonuses)


if __name__ == '__main__':
    report = verify()
    destination = Path(__file__).with_name('intimacy-math-report.json')
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"{report['checks_passed']} numerical checks passed; runtime not integrated.")
    print(destination.resolve())
