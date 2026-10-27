"""Bounded numerical kernel for adult-intimate-2; no IO or consent decisions."""
from __future__ import annotations

import math


def clip(x, low=0., high=1.):
    return min(high, max(low, x))


def arousal_step(value, dt, rate, half_life):
    coefficient = math.log(2.) / half_life
    return clip(value * math.exp(-coefficient * dt)
                + rate / coefficient * -math.expm1(-coefficient * dt))


def peak_values(state, cfg):
    factor = min(cfg.denial_cap, 1. + cfg.denial_gain * state['denied_count'])
    amplitude = clip(state['arousal'] * factor, 0., 2.)
    return dict(release_factor=factor, release_amplitude=amplitude,
        arousal=state['arousal'] * cfg.peak_keep_ratio ** factor,
        momentary_satiety=clip(state['momentary_satiety'] + .25 * amplitude),
        sensitivity=min(cfg.sensitivity_max, state['sensitivity'] + cfg.sensitivity_gain),
        current_threshold=min(cfg.threshold_cap, state['current_threshold'] + cfg.threshold_gain),
        round_interval_seconds=max(cfg.round_interval_min, state['round_interval_seconds'] * cfg.round_interval_k))


def settling_values(anchor, until, cfg):
    elapsed = max(0., until - anchor['at'])
    tau = anchor['deadline'] - anchor['at']
    target = clip(anchor['momentary_satiety'] + .2 * anchor['arousal'])
    return dict(arousal=anchor['arousal'] * 2 ** (-elapsed / cfg.settling_arousal_half_life),
        momentary_satiety=target - (target - anchor['momentary_satiety']) * math.exp(-elapsed / (tau / 3.)),
        valence=anchor['valence'] * 2 ** (-elapsed / 90.),
        engagement=anchor['engagement'] * 2 ** (-elapsed / 60.))


def reset_episode(state, cfg, at):
    """Reset round sensitivity; its permanent bonus remains in private state."""
    state.update(arousal=0., valence=0., momentary_satiety=0., engagement=0.,
        adaptation=0., scene_load=0., sensitivity=1., recovery=1.,
        current_threshold=cfg.threshold, round_interval_seconds=cfg.round_interval_initial,
        denied_count=0, completed_rounds=0, scene_peak_count=0, phase='idle', mode='none',
        experience_context='closed', willingness='uncertain', willingness_evidence=[],
        episode_id=None, attempt_id=None, permission_request=None, armed=True,
        released=True, candidate_since=None, recovery_until=0., next_peak_eligible_at=0.,
        rearm_requires_intent=False, rearm_after_seq=0, rearm_evidence_seq=0,
        armed_after_seq=0, settle_anchor=None, integrated_until=at, last_impulse_refs=[])
