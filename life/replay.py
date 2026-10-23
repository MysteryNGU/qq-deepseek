"""Re-execute recorded state transitions with recorded rules and clock.

Each transition starts from its committed checkpoint, including intervening
ticks. No model, tool adapter or real outgoing channel is constructed.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from .contracts import Appraisal, DesireState, Scope
from .runtime import LifeRuntime
from .state import AffectConfig


def verify_state_replay(runtime, scope):
    checked, mismatches, unsupported = 0, [], []
    sources = runtime.repo.events_after(scope, 0, limit=1000000)
    for row in runtime.replay_journal(scope):
        if row["rule_version"] != "life-state-3":
            unsupported.append(row["event_id"])
            continue
        with tempfile.TemporaryDirectory(prefix="qq-state-replay-") as directory:
            replay = LifeRuntime(Path(directory) / "replay.db", scope.agent_id, clock=lambda: row["at"])
            try:
                recorded = json.loads(row["appraisal"])
                replay.config.affect = AffectConfig(**recorded.pop("_affect_config"))
                pressure = recorded.pop("_desire_pressure")
                replay.desires = lambda _scope: DesireState(_scope, baseline=pressure)
                recorded["scope"] = Scope(**recorded["scope"])
                app = Appraisal(**recorded)
                with replay.repo.tx():
                    for event in sources:
                        replay.repo.ingest_event(event)
                    for kind, snapshot in json.loads(row["before_state"]).items():
                        if snapshot is not None:
                            version, payload, sequence = snapshot
                            replay.repo._exec("INSERT INTO state_snapshots VALUES(?,?,?,?,?)",
                                              (kind + ":" + scope.scope_id, version, sequence,
                                               json.dumps(payload), row["at"]))
                event = replay.repo.get_event(row["event_id"])
                if event is None:
                    mismatches.append({"event_id": row["event_id"], "reason": "source_unavailable"})
                    continue
                replay.appraise(scope, event, app)
                actual = json.loads(json.dumps(replay._state_packet(scope)))
                expected = json.loads(row["after_state"])
                if actual != expected:
                    mismatches.append({"event_id": row["event_id"], "aggregates": [key for key in actual if actual[key] != expected.get(key)]})
                checked += 1
            finally:
                replay.close()
    return {"checked": checked, "mismatches": mismatches, "unsupported": unsupported,
            "matched": not mismatches and not unsupported}
