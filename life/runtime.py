#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host-owned events, state, memory and delivery for one DSH role."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
from typing import Any

from .appraisal import validate_appraisal
from .contracts import (
    AffectState, Appraisal, DesireState, Event, MoodState, RelationEvidence,
    RelationshipState, Scope, new_id, now_ts,
)
from .knowledge import KnowledgeService
from .memory import MemoryService
from .repo import Repository
from .state import (
    DEFAULT_AFFECT, DEFAULT_DESIRE, AffectConfig, AffectChange, DesireConfig,
    affect_view, bounded_drive_baseline, desire_pressure,
    integrate_mood, reduce_affect, reduce_desires,
)
from .drives import (
    DRIVE_DIMENSIONS, DRIVE_LABELS, ContactState, DriveConfig, DriveEvidence,
    DriveStateSet, advance_to, apply_one_evidence, canonical_dimension,
    contact_candidates, drive_view as build_drive_view,
    emotion_baseline_from_drives, initial_state, mark_contact_attempt,
    may_propose_contact as drive_may_propose_contact, regulation_candidates,
    summarize as summarize_drives, view_at as drive_view_at,
)
from .text import stable_id
SUPPORTED_MEMORY_SCHEMA = 2

SUPPORTED_RELATIONSHIP_SCHEMA = 2
SUPPORTED_EXPERIENCE_SCHEMA = 2


@dataclass
class LifeConfig:
    affect: AffectConfig = None  # type: ignore[assignment]
    desire: DesireConfig = None  # type: ignore[assignment]
    drives: DriveConfig = None  # type: ignore[assignment]
    relationship_owner: str = '0'
    relations: Any = None
    relation_registry: Any = None
    memory_index: Any = None
    experience: Any = None

    def __post_init__(self) -> None:
        from copy import deepcopy
        self.affect = self.affect or deepcopy(DEFAULT_AFFECT)
        self.desire = self.desire or deepcopy(DEFAULT_DESIRE)
        self.drives = self.drives or DriveConfig()


class LifeRuntime:
    def __init__(self, db_path: str | Path, agent_id: str = "default",
                 config: LifeConfig | None = None, *, clock=now_ts):
        from supervise import validate_data_compatibility
        validate_data_compatibility(Path(__file__).resolve().parents[1],database=db_path)
        self.agent_id = str(agent_id or "default")
        self.config = config or LifeConfig()
        self.clock = clock
        # 运行中才会累计「高位持续」；重启后的离线区间只推进被动需要。
        import time as _time
        self._monotonic = _time.monotonic
        self._last_advance_mono = self._monotonic()
        self._last_advance_mono_by_scope: dict[str, float] = {}
        self.repo = Repository(db_path)
        from .work_queue import WorkQueue
        from .delivery import DeliveryService
        from .persona import PersonaService
        self.jobs = WorkQueue(self.repo, clock)
        self.delivery = DeliveryService(self, clock)
        self.persona = PersonaService(self.repo, self.agent_id)
        self.memory = MemoryService(self.repo)
        self.knowledge = KnowledgeService(self.repo, self.memory)
        from .recollection import Recollection
        self.recollection = Recollection(self)
        self.memory.recollection = self.recollection
        from .relationships import Relationships
        self.relationships = Relationships(self,self.config.relationship_owner,self.config.relations,self.config.relation_registry)
        from .retention import TombstoneLedger
        self.tombstones = TombstoneLedger(self.repo)
        self.tombstones.runtime = self
        self.memory.tombstones = self.tombstones
        # Must precede any retrieval, prompt construction or background job.
        self.tombstones.recover()
        from .life_catalog import LifeCatalog
        from .life_access import LifeAccess
        from .memory_index import MemoryIndex
        from .life_recall import LifeRecall
        from .memory_organize import MemoryOrganize
        self.catalog = LifeCatalog(self.repo, self.agent_id, clock)
        self.life_access = LifeAccess(self.catalog)
        self.memory.access = self.life_access
        self.repo.life_access = self.life_access
        self.memory_index = MemoryIndex(self.catalog, self.life_access, self.jobs, self.config.memory_index)
        self.life_recall = LifeRecall(self.catalog, self.life_access, self.memory_index)
        self.memory_organize = MemoryOrganize(self.catalog, self.life_access, self.jobs)
        self.memory.organizer = self.memory_organize
        from .memory_migration import activate
        activate(self)
        from .experience import ExperienceSimulation
        self.experience = ExperienceSimulation(self, self.config.experience)
        self.experience.recover()

    def close(self) -> None:
        self.repo.close()

    # ---------------------------------------------------------------- 作用域

    def scope(self, conversation_id: str, participant_id: str = "",
              audience_policy_id: str = "") -> Scope:
        return Scope(
            agent_id=self.agent_id,
            conversation_id=str(conversation_id),
            participant_id=str(participant_id or ""),
            audience_policy_id=str(audience_policy_id or conversation_id),
        )

    @staticmethod
    def conversation_key(kind: str, target_id: int | str, account: str = "") -> str:
        """稳定的会话键：私聊/群聊/控制台绝不共用。

        account 只是兼容参数；真正的 bot 身份由 LifeRuntime.agent_id 区分，
        这样同一个 life.db 重启后不会因为 self_id 晚到而换一套作用域。
        """
        return f"qq:{kind}:{target_id}"

    # ---------------------------------------------------------------- 写入

    def known_scopes(self) -> list[Scope]:
        """Recover conversation identities, without guessing legacy peer origins."""
        rows = self.repo._all(
            "SELECT e.conversation_id,e.participant_id,e.audience_policy_id FROM events e "
            "JOIN (SELECT conversation_id,MAX(ingest_seq) AS seq FROM events "
            "WHERE agent_id=? AND redacted_at IS NULL GROUP BY conversation_id) latest "
            "ON e.conversation_id=latest.conversation_id AND e.ingest_seq=latest.seq "
            "WHERE e.agent_id=?", (self.agent_id, self.agent_id))
        return [self.scope(row["conversation_id"], row["participant_id"], row["audience_policy_id"])
                for row in rows if row["conversation_id"].startswith(("qq:private:", "qq:group:"))]

    def ingest(self, scope: Scope, kind: str, source_kind: str, text: str,
               *, external_id: str | None = None, role: str = "",
               actor_id: str = "", payload: dict[str, Any] | None = None,
               occurred_at: float | None = None,
               source_event_ids: list[str] | None = None) -> tuple[Event, bool]:
        external_id = str(external_id) if external_id not in (None, "") else None
        event_id = (
            stable_id("evt", scope.agent_id, scope.conversation_id, external_id)
            if external_id else new_id("evt")
        )
        body = dict(payload or {})
        body.setdefault("text", str(text or ""))
        if role:
            body.setdefault("role", role)
        event = Event(
            id=event_id, scope=scope, kind=kind, source_kind=source_kind,
            payload=body, external_id=external_id, actor_id=actor_id or scope.participant_id,
            occurred_at=self.clock() if occurred_at is None else occurred_at,
            received_at=self.clock(),
            source_event_ids=list(source_event_ids or []),
        )
        _id, created = self.repo.ingest_event(event)
        if not created:
            got = self.repo.get_event(_id)
            return got or event, False
        if hasattr(self, 'experience'):
            self.experience.receive(self.repo.get_event(event.id))
        return event, True

    def ingest_message(self, scope: Scope, text: str, *, role: str = "user",
                       external_id: str | None = None,
                       occurred_at: float | None = None) -> tuple[Event, bool]:
        source_kind = "user_report" if role == "user" else "self_output"
        return self.ingest(scope, "message", source_kind, text, role=role,
                           external_id=external_id, occurred_at=occurred_at)

    def ensure_turn(self, scope: Scope, event_id: str, origin: str = "user_request"):
        return self.repo.create_turn(scope, origin, [event_id])

    # ---------------------------------------------------------------- 情绪推进

    def appraise(self, scope: Scope, event: Event,
                 appraisal: Appraisal | None = None,*,call_key=None) -> tuple[Appraisal, AffectChange] | None:
        from .appraisals import Appraisals
        return Appraisals(self).submit(scope, event, appraisal,call_key=call_key)

    def state_key(self, kind: str, scope: Scope) -> str:
        # One role has one affect/drive clock. Relations belong to actual people,
        # even if the same person speaks in multiple conversations.
        if kind == 'relationship':
            return f'{kind}:{scope.agent_id}|person:{scope.participant_id}'
        return f'{kind}:{scope.agent_id}|self:role'

    def _state_packet(self, scope):
        return {kind: self.repo.load_state(self.state_key(kind, scope))
                for kind in ("affect", "mood", "relationship", "drive")}

    def replay_journal(self, scope):
        """Read recorded transitions without interpreting text or doing I/O.

        Exact checkpoints include causes, rule version and timestamps. Consumers
        can compare these against reducer re-execution in isolated databases.
        """
        return [dict(row) for row in self.repo._all(
            "SELECT * FROM state_journal WHERE scope_id=? ORDER BY id", (scope.scope_id,))]

    def _appraise(self, scope: Scope, event: Event,
                  appraisal: Appraisal | None = None, *, at=None,
                  affect_config=None, desire_snapshot=None) -> tuple[Appraisal, AffectChange] | None:
        """对一条用户事件做一次情绪推进；同一事件只生效一次。"""
        if scope.private_key != event.scope.private_key or event.redacted_at is not None:
            return None
        if appraisal is None:
            return None
        at = self.clock() if at is None else at
        aggregate_id = self.state_key('affect',scope)
        got = self.repo.load_state(aggregate_id)
        if got:
            version, payload, last_seq = got
            state = AffectState(
                scope=scope,
                intensity=payload.get("intensity") or {k: 0.2 for k in (
                    "joy", "concern", "frustration", "hurt", "anticipation")},
                causes=payload.get("causes") or [],
                updated_at=float(payload.get("updated_at", at)),
                version=version,
                recovery_half_life_seconds=payload.get('recovery_half_life_seconds',{}),
                baseline=payload.get('baseline',{}),
            )
        else:
            state = AffectState(scope=scope, updated_at=at,
                intensity=dict((affect_config or self.config.affect).baseline))
            last_seq = 0
        app = appraisal
        drive_snapshot = self.drive_pressure(scope, at) if desire_snapshot is None else desire_snapshot
        change = reduce_affect(state, app, at, affect_config or self.config.affect,
                               drive_pressure=drive_snapshot)
        payload = {
            "intensity": change.state.intensity,
            "causes": change.state.causes,
            "updated_at": change.state.updated_at,
            "source_appraisal_id": app.id,
            "recovery_half_life_seconds": change.state.recovery_half_life_seconds,
            "baseline": change.state.baseline,
        }
        version = self.repo.apply_effect_and_save_state(
            event.id, "affect", aggregate_id, payload, last_seq)
        if version is None:
            return None
        change.state.version = version
        self.update_mood(scope, change.state, now=at)
        # Appraisal 的驱力证据与情绪在同一事件边界结算；去重键由 repo 保证。
        drive_evidence = []
        filtered = []
        for item in (getattr(app, "drive_evidence", None) or []):
            kind = str(item.get("kind") or "activation_change")
            if event.source_kind not in {'user_report','tool_observation'} and kind not in {'activation_change','internal_regulation'}:
                filtered.append(dict(dimension=item['dimension'],effect_kind=kind,evaluated=True,applied=False,reason='external_evidence_required'))
                continue
            if kind == "interaction_satisfaction" and not (
                    item.get("confirmed") and item.get("real_participant")
                    and item.get("relationship_allows", True)):
                filtered.append(dict(dimension=item['dimension'],effect_kind=kind,evaluated=True,applied=False,reason='real_confirmed_interaction_required'))
                continue
            drive_evidence.append(DriveEvidence(
                id=stable_id("drv", app.id, str(item.get("dimension")), kind),
                dimension=str(item.get("dimension") or ""), kind=kind,
                delta=float(item.get("delta") or 0.0), root_id=event.id,
                confidence=float(item.get("confidence", app.certainty)),
                confirmed=bool(item.get("confirmed")),
                real_participant=bool(item.get("real_participant")),
                relationship_allows=bool(item.get("relationship_allows", True)),
                participant_id=str(item.get("participant_id") or scope.participant_id),
                sources=[event.id], note=str(item.get("note") or
                                               getattr(app, "interpretation", "")),
            ))
        if drive_evidence:
            _, settlements = self.apply_drive_evidence(scope, drive_evidence, now=at)
            change.drive_effects = settlements
        change.drive_effects.extend(filtered)
        return app, change

    def rebuild_after_forget(self, scope, journal=None):
        from .appraisals import Appraisals
        Appraisals(self).rebuild(scope.agent_id,forgotten_scope=scope)

    def desires(self, scope: Scope) -> DesireState:
        from .persona import DIMENSIONS
        profile = self.persona.current()
        baseline = profile.desires if profile else {key: .5 for key in DIMENSIONS}
        got = self.repo.load_state("desire:" + scope.scope_id)
        fields = dict(got[1]) if got else {}
        fields.pop("scope", None)
        fields.pop("profile_version", None)
        fields["baseline"] = baseline
        return DesireState(scope=scope, **fields)

    def apply_desire_evidence(self, scope, evidence, *, now=None, settle_window=False):
        with self.repo.tx():
            valid = []
            for item in evidence:
                roots = self.repo.events_by_ids(item.root_event_ids)
                if (item.scope.private_key != scope.private_key or not roots
                        or len(roots) != len(set(item.root_event_ids))
                        or any(event.scope.private_key != scope.private_key
                               or event.source_kind not in {"user_report", "tool_observation"} for event in roots)):
                    continue
                valid.append(item)
            state = reduce_desires(self.desires(scope), valid, now if now is not None else now_ts(),
                                   self.config.desire, settle_window=settle_window)
            payload = asdict(state)
            payload.pop("scope")
            profile = self.persona.current()
            payload["profile_version"] = profile.version if profile else "visible-default"
            self.repo.save_state("desire:" + scope.scope_id, payload)
            return state

    # ---------------------------------------------------------------- drive_v2

    def drive_config(self) -> DriveConfig:
        """部署配置打底；人格里明确写出的参数覆盖它，偏好只做选择权重。"""
        if getattr(self, "_replaying", False):
            return self.config.drives
        base = self.config.drives or DriveConfig()
        config = DriveConfig(
            dimensions={k: replace(v) for k, v in base.dimensions.items()},
            activation_half_life_hours=dict(base.activation_half_life_hours),
            satiety_half_life_hours=dict(base.satiety_half_life_hours),
            global_contact_per_day=base.global_contact_per_day,
            contact_window_hours=base.contact_window_hours,
        )
        profile = self.persona.current()
        if profile:
            for dim, raw in (getattr(profile, "drive_overrides", {}) or {}).items():
                key = canonical_dimension(dim)
                if not key or key not in config.dimensions or not isinstance(raw, dict):
                    continue
                allowed = {f for f in config.dimensions[key].__dataclass_fields__ if f != "dimension"}
                values = {str(k): v for k, v in raw.items() if k in allowed}
                config.dimensions[key] = replace(config.dimensions[key], **values)
            for dim, value in (getattr(profile, "drive_targets", {}) or {}).items():
                key = canonical_dimension(dim)
                if key and key in config.dimensions:
                    config.dimensions[key].persona_preference = float(value)
        return config

    def _drive_aggregate(self, scope: Scope) -> str:
        return self.state_key('drive',scope)

    def _settle_affect_config(self,scope,old,new,at):
        got = self.repo.load_state(self.state_key('affect',scope))
        if not got:
            return
        version,payload,_ = got
        previous = AffectState(scope=scope,intensity=payload['intensity'],
            updated_at=payload['updated_at'],baseline=payload.get('baseline',old.baseline),
            recovery_half_life_seconds=payload.get('recovery_half_life_seconds',old.half_life))
        payload['intensity'] = affect_view(previous,at,old)
        payload['updated_at'] = at
        payload['baseline'] = dict(new.baseline)
        rates = dict(previous.recovery_half_life_seconds)
        for dim,value in new.half_life.items():
            if rates.get(dim,old.half_life[dim]) == old.half_life[dim]:
                rates[dim] = value
        payload['recovery_half_life_seconds'] = rates
        self.repo.save_state(self.state_key('affect',scope),payload)

    def configure_dynamics(self,scope,affect,drives):
        """Settle time under old rules, then activate new parameters from now."""
        from .appraisals import Appraisals
        at = self.clock()
        with self.repo.tx():
            self.advance_time(scope,at)
            before = self._state_packet(scope)
            old = self.config.affect
            self._settle_affect_config(scope,old,affect,at)
            event,_ = self.ingest(scope,'state_configuration','inference','',occurred_at=at)
            Appraisals(self).record(scope,event.id,dict(_kind='configuration',scope=asdict(scope),
                _drive_config=self.drive_config().to_dict(),_old_affect=asdict(old),_new_affect=asdict(affect)),before,at)
            self.config.affect,self.config.drives = affect,drives

    def drive_state(self, scope: Scope) -> DriveStateSet:
        """读取当前驱力状态；没有新事件时不写库。"""
        config = self.drive_config()
        got = self.repo.load_state(self._drive_aggregate(scope))
        if got:
            version, payload, _seq = got
            state = DriveStateSet.from_dict(scope.scope_id, payload, config)
            state.version = version
            return state
        state = initial_state(scope.scope_id, config, at=self.clock())
        old = self.repo.load_state("desire:" + scope.scope_id)
        if old:
            _v, payload, _s = old
            state.last_day = str(payload.get("last_window_id") or "")
        return state

    def advance_time(self, scope: Scope, now: float | None = None, *,
                     online_seconds: float | None = None,
                     context_factors: dict[str, float] | None = None) -> DriveStateSet:
        """作业入口：提交一次时间推进与水位。读取状态请用 drive_view。"""
        at = float(self.clock() if now is None else now)
        config = self.drive_config()
        for _attempt in range(2):
            with self.repo.tx():
                state = self.drive_state(scope)
                if at <= float(state.updated_at):
                    if not state.version:
                        state.version = self.repo.save_state(self._drive_aggregate(scope),state.to_dict())
                    return state
                dt = max(0.0, at - float(state.updated_at))
                online = online_seconds
                mono_now = float(self._monotonic())
                if online is None:
                    anchor = float(self._last_advance_mono_by_scope.get(
                        self._drive_aggregate(scope), self._last_advance_mono))
                    elapsed = max(0.0, mono_now - anchor)
                    online = min(dt, elapsed)
                advanced = advance_to(state, at, config, online_seconds=online,
                                      context_factors=context_factors)
                before = self._state_packet(scope)
                payload = advanced.to_dict()
                if state.version:
                    version = self.repo.save_state_cas(
                        self._drive_aggregate(scope), payload, expected_version=state.version)
                    if version is None:
                        continue
                else:
                    version = self.repo.save_state(self._drive_aggregate(scope), payload)
                advanced.version = int(version or 0)
                self._last_advance_mono_by_scope[self._drive_aggregate(scope)] = mono_now
                from .appraisals import Appraisals
                Appraisals(self).time(scope,before,at,online,context_factors)
                return advanced
        return self.drive_state(scope)

    def drive_view(self, scope: Scope, now: float | None = None) -> dict[str, dict[str, Any]]:
        """同一时点的只读视图；不结算满足、不执行调节、不写库。"""
        state = self.drive_state(scope)
        at = float(self.clock() if now is None else now)
        return drive_view_at(state, at, self.drive_config())

    def drive_summary(self, scope: Scope, now: float | None = None) -> dict[str, float]:
        return summarize_drives(self.drive_view(scope, now))

    def drive_pressure(self, scope: Scope, now: float | None = None) -> dict[str, float]:
        return {dim: float(row["pressure"])
                for dim, row in self.drive_view(scope, now).items()}

    def regulation_candidates(self, scope: Scope, now: float | None = None) -> list[str]:
        return regulation_candidates(self.drive_view(scope, now))

    def emotion_baseline(self, scope: Scope, now: float | None = None) -> dict[str, float]:
        view = self.drive_view(scope, now)
        pressure = {dim: float(row["pressure"]) for dim, row in view.items()}
        return bounded_drive_baseline(pressure, self.config.affect.baseline)

    def _validate_drive_evidence(self, scope: Scope, evidence: list[DriveEvidence]) -> list[DriveEvidence]:
        valid: list[DriveEvidence] = []
        for item in evidence:
            sources = [str(x) for x in (item.sources or []) if str(x)]
            if sources:
                events = self.repo.events_by_ids(sources)
                if (len(events) != len(set(sources))
                        or any(event.scope.private_key != scope.private_key
                               or event.redacted_at is not None for event in events)):
                    continue
            elif item.kind != "internal_regulation":
                continue
            valid.append(item)
        return valid

    def apply_drive_evidence(self, scope: Scope, evidence: list[DriveEvidence], *,
                             now: float | None = None) -> tuple[DriveStateSet, list[dict[str, Any]]]:
        """原子应用已校验的驱力证据，并按 root/dimension/effect/cycle 去重。"""
        at = float(self.clock() if now is None else now)
        valid = self._validate_drive_evidence(scope, list(evidence or []))
        if not valid:
            return self.drive_state(scope), []
        config = self.drive_config()
        settlements: list[dict[str, Any]] = []
        with self.repo.tx():
            state = self.drive_state(scope)
            if at > float(state.updated_at):
                state = advance_to(state, at, config, online_seconds=0.0)
            for item in valid:
                ev = item.normalized()
                root = str(ev.root_id or (ev.sources[0] if ev.sources else ev.id))
                if ev.dimension=='libido' and hasattr(self,'experience'):
                    self.experience.private.guard_evidence(scope,root,ev.kind)
                cycle = int(ev.cycle_id or 0)
                if not self.repo.record_drive_settlement(
                        scope_id=scope.scope_id, root_id=root, dimension=ev.dimension,
                        effect_kind=ev.kind, cycle_id=cycle,
                        before_value=float(state.drives[ev.dimension].tension),
                        after_value=float(state.drives[ev.dimension].tension),
                        source_ids=list(ev.sources), payload={"evidence_id": ev.id}):
                    continue
                state, settlement = apply_one_evidence(state, ev, config, now=at)
                if hasattr(self,'experience'):
                    self.experience.private.apply_evidence(scope,ev,at)
                self.repo._exec(
                    "UPDATE drive_settlements SET before_value=?,after_value=?,payload=? "
                    "WHERE scope_id=? AND root_id=? AND dimension=? AND effect_kind=? "
                    "AND cycle_id=? AND revoked_at IS NULL",
                    (float(settlement["before_value"]), float(settlement["after_value"]),
                     json.dumps(settlement, ensure_ascii=False), scope.scope_id, root,
                     ev.dimension, ev.kind, str(cycle)))
                settlements.append(settlement)
            if settlements:
                payload = state.to_dict()
                version = (self.repo.save_state_cas(self._drive_aggregate(scope), payload,
                                                   expected_version=state.version)
                           if state.version else self.repo.save_state(self._drive_aggregate(scope), payload))
                state.version = int(version or 0)
        return state, settlements

    def complete_internal_regulation(self, scope: Scope, dimension: str, *,
                                     evidence_id: str, sources: list[str] | None = None,
                                     now: float | None = None,
                                     relief: float | None = None) -> tuple[DriveStateSet, list[dict[str, Any]]]:
        """完成一次内部调节：必须是当前高位周期、已确认、且只结算一次。"""
        at = float(self.clock() if now is None else now)
        dim = canonical_dimension(dimension)
        if not dim:
            raise ValueError(f"unknown_drive_dimension:{dimension}")
        state = self.drive_state(scope)
        if self.repo.drive_settlement_by_root(
                scope_id=scope.scope_id, root_id=str(evidence_id),
                effect_kind="internal_regulation"):
            return state, []
        view = self.drive_view(scope, at)
        if not bool(view[dim].get("regulation_due")):
            raise ValueError("regulation_not_due")
        evidence = DriveEvidence(
            id=str(evidence_id), dimension=dim, kind="internal_regulation",
            delta=float(relief if relief is not None else self.drive_config().dynamics(dim).regulation_relief_gain),
            root_id=str(evidence_id), cycle_id=int(state.drives[dim].cycle_id),
            confidence=1.0, confirmed=True, sources=list(sources or []),
            note="内部调节完成；不记为对方参与",
        )
        return self.apply_drive_evidence(scope, [evidence], now=at)

    def mark_contact_attempt(self, scope: Scope, dimension: str, *,
                             at: float | None = None, contact_id: str = "") -> DriveStateSet:
        config = self.drive_config()
        with self.repo.tx():
            state = self.drive_state(scope)
            advanced = mark_contact_attempt(state, dimension, at=float(
                self.clock() if at is None else at), contact_id=contact_id, config=config)
            payload = advanced.to_dict()
            version = (self.repo.save_state_cas(self._drive_aggregate(scope), payload,
                                               expected_version=state.version)
                       if state.version else self.repo.save_state(self._drive_aggregate(scope), payload))
            advanced.version = int(version or 0)
            if hasattr(self,'experience'):
                self.repo._exec('INSERT OR IGNORE INTO drive_contact_attempts VALUES(?,?,?,?,?)',
                    (stable_id('contact',scope.scope_id,dimension,contact_id or str(advanced.version)),
                     scope.agent_id,scope.scope_id,dimension,float(self.clock() if at is None else at)))
            return advanced

    def may_propose_contact(self, scope: Scope, dimension: str,
                            contact: ContactState | None = None, *,
                            now: float | None = None) -> bool:
        at = float(self.clock() if now is None else now)
        return drive_may_propose_contact(
            self.drive_view(scope, at), dimension, self.drive_config(), contact, now=at)

    def contact_candidates(self, scope: Scope, contact: ContactState | None = None, *,
                           now: float | None = None) -> list[str]:
        at = float(self.clock() if now is None else now)
        return contact_candidates(self.drive_view(scope, at), self.drive_config(), contact, now=at)



    def affect_summary(self, scope: Scope, now: float | None = None) -> dict[str, Any]:
        got = self.repo.load_state(self.state_key('affect',scope))
        if not got:
            return {}
        version, payload, _seq = got
        previous = AffectState(
            scope=scope, intensity=payload.get("intensity") or {},
            causes=payload.get("causes") or [],
            updated_at=float(payload.get("updated_at") or now_ts()), version=version,
            recovery_half_life_seconds=payload.get('recovery_half_life_seconds',{}),
            baseline=payload.get('baseline',{}))
        at = float(self.clock() if now is None else now)
        return {
            "intensity": affect_view(previous, at, self.config.affect,
                                     self.drive_pressure(scope, at)),
            "causes": [c for c in (payload.get('causes') or [])
                       if all((e := self.repo.get_event(eid)) and not e.redacted_at
                              and e.scope.private_key == scope.private_key
                              for eid in c.get('source_event_ids',[]))][-3:],
            "updated_at": previous.updated_at,
            "version": version,
            "recovery_half_life_seconds": previous.recovery_half_life_seconds,
        }

    # ---------------------------------------------------------------- 检索

    def context_block(self, scope: Scope, query: str, budget: int = 2400) -> str:
        from .resource_policy import estimate_tokens
        note = self.recollection.block(scope, query, min(1200, budget // 3))
        recalled = self.life_recall.context(scope, [query], max(0, budget-estimate_tokens(note)))
        return '\n'.join(x for x in (recalled, note) if x)

    # ---------------------------------------------------------------- 投影


    def record_reply(self, scope: Scope, turn_id: str, text: str,
                     *, external_id: str | None = None,
                     outcome: str = "sent_unconfirmed") -> Event:
        event, _created = self.ingest_message(
            scope, text, role="assistant", external_id=external_id)
        prior = self.repo._one('SELECT id FROM decision_records WHERE turn_id=? ORDER BY created_at DESC LIMIT 1', (turn_id,))
        if prior:
            with self.repo.tx():
                self.repo._exec('UPDATE decision_records SET outcome=? WHERE id=?', (outcome, prior['id']))
        else:
            self.repo.save_decision({
                "turn_id": turn_id, "agent_id": scope.agent_id,
                "conversation_id": scope.conversation_id,
                "outcome": outcome, "policy_version": "life-1",
            })
        if turn_id:
            self.repo.set_turn_status(turn_id, "done")
        return event

    # ---------------------------------------------------------------- 结构化评估

    def appraise_structured(self, scope: Scope, event: Event,
                            raw: dict[str, Any],*,call_key=None) -> tuple[Appraisal, AffectChange] | None:
        ids = raw.get('source_event_ids') or [event.id]
        app = validate_appraisal(raw, scope, ids, fallback_sources=[event.id],relation_registry=self.relationships.registry)
        if app is None:
            return None
        return self.appraise(scope, event, app,call_key=call_key)

    # ---------------------------------------------------------------- 关系 / 心境

    def _load_relationship(self, scope: Scope) -> RelationshipState:
        return self.relationships.load(scope)

    def relationship(self, scope: Scope) -> RelationshipState:
        return self._load_relationship(scope)

    def configure_relationships(self,scope,config):
        from .relationships import RelationConfig
        from .appraisals import Appraisals
        config = RelationConfig.from_mapping(asdict(config),self.relationships.registry)
        old = self.relationships.config
        if config==old:
            return
        with self.repo.tx():
            event,_ = self.ingest(scope,'relationship_configuration','inference','')
            raw = dict(_kind='relationship_configuration',scope=asdict(scope),_old_relations=asdict(old),
                       _new_relations=asdict(config),_relation_config_version=config.version)
            Appraisals(self).record(scope,event.id,raw,self._state_packet(scope),self.clock())
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',
                           ('relationship_config_current:'+self.agent_id,json.dumps(asdict(config),ensure_ascii=False,sort_keys=True)))
        self.relationships.config = config

    def apply_relation_evidence(self, scope: Scope,
                                evidence: list[RelationEvidence], *, now=None):
        from .agent_session import SessionFailure
        raise SessionFailure('protocol_mismatch','旧关系 delta 入口已停用；使用 relation_protocol=2 的 life_appraise')

    def _load_mood(self, scope: Scope) -> MoodState:
        got = self.repo.load_state(self.state_key('mood',scope))
        if not got:
            return MoodState(scope=scope)
        version, payload, _seq = got
        return MoodState(
            scope=scope,
            pleasure=float(payload.get("pleasure", 0.0)),
            activation=float(payload.get("activation", 0.35)),
            energy=float(payload.get("energy", 0.6)),
            updated_at=float(payload.get("updated_at") or now_ts()),
            version=version,
        )

    def mood(self, scope: Scope) -> MoodState:
        return self._load_mood(scope)

    def update_mood(self, scope: Scope, affect: AffectState | None = None,
                    now: float | None = None) -> MoodState:
        at = self.clock() if now is None else now
        previous = self._load_mood(scope)
        if not self.repo.load_state(self.state_key('mood',scope)):
            previous.updated_at = at
        if affect is None:
            got = self.repo.load_state(self.state_key('affect',scope))
            if got:
                _v, payload, _s = got
                affect = AffectState(
                    scope=scope, intensity=payload.get("intensity") or {},
                    causes=payload.get("causes") or [],
                    updated_at=float(payload.get("updated_at") or now_ts()),
                    recovery_half_life_seconds=payload.get('recovery_half_life_seconds',{}),
                    baseline=payload.get('baseline',{}))
        state = integrate_mood(previous, affect, float(at))
        version = self.repo.save_state(self.state_key('mood',scope), {
            "pleasure": state.pleasure, "activation": state.activation,
            "energy": state.energy, "updated_at": state.updated_at,
        })
        state.version = version
        return state

    # ---------------------------------------------------------------- 未完成事项



    # ---------------------------------------------------------------- 日常节拍


    def record_delivery(self, scope: Scope, text: str, *,
                        external_id: str | None = None,
                        confirmed: bool = True) -> Event:
        event, _created = self.ingest(
            scope, "delivery", "tool_observation" if confirmed else "inference",
            text, external_id=external_id, payload={"confirmed": bool(confirmed)})
        return event

    def add_knowledge(self, scope: Scope, subject: str, claims: list[dict[str, Any]],
                      source_event_ids: list[str], **kwargs: Any):
        return self.knowledge.add_card(scope, subject, claims, source_event_ids, **kwargs)

    def record_gap(self, gap: Any) -> str:
        from .capability import record_gap
        return record_gap(self.repo, gap)

    def record_study(self, thread: Any) -> str:
        from .study import record_study
        return record_study(self.repo, thread)



    def reset_scope(self, scope: Scope) -> dict[str, int]:
        with self.repo.tx():
            self.repo._exec('DELETE FROM annotation_terms WHERE annotation_id IN (SELECT id FROM memory_annotations WHERE scope_id=?)', (scope.scope_id,))
            self.repo._exec("UPDATE memory_annotations SET text='',cues='[]',sources='[]',skill_refs='[]',status='redacted' WHERE scope_id=?", (scope.scope_id,))
            events = [r["id"] for r in self.repo._all(
                "SELECT id FROM events WHERE agent_id=? AND conversation_id=?", scope.private_key)]
            memories = [r["id"] for r in self.repo._all(
                "SELECT id FROM memories WHERE agent_id=? AND conversation_id=?", scope.private_key)]
            identity = self.tombstones.append(scope, memories, events)
            result = self.repo.reset_scope(scope)
            self.tombstones.apply(identity, scope, memories, events, epoch=result["epoch"])
            result["epoch"] = self.repo.forget_epoch(scope.scope_id)
            return result

    # ---------------------------------------------------------------- 维护

    def forget(self, scope: Scope, memory_ids: list[str] | None = None,
               kinds: list[str] | None = None) -> dict[str, Any]:
        return self.memory.forget(scope, memory_ids=memory_ids, kinds=kinds)

    def explain(self, memory_id: str, scope: Scope | None = None) -> dict[str, Any] | None:
        return self.memory.explain(memory_id, scope)

    def status(self, scope: Scope | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"agent": self.agent_id, **self.repo.stats()}
        if scope is not None:
            out["scope"] = scope.scope_id
            out["affect"] = self.affect_summary(scope)
            rel = self.relationship(scope)
            mood = self._load_mood(scope)
            out["relationship"] = {
                "familiarity": round(rel.familiarity, 3),
                "trust": round(rel.trust, 3),
                "comfort": round(rel.comfort, 3),
                "bond": round(rel.bond, 3),
            }
            out["mood"] = {
                "pleasure": round(mood.pleasure, 3),
                "activation": round(mood.activation, 3),
                "energy": round(mood.energy, 3),
            }
            out["drives"] = self.drive_summary(scope)
            out["drive_detail"] = self.drive_view(scope)
            out["open_loops_detail"] = [l.summary for l in self.repo.active_open_loops(scope, 5)]
        return out
