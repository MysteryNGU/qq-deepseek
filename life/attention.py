"""Persistent interests with inertia and bounded study scheduling."""
from __future__ import annotations

import json

from .contracts import now_ts
from .study import StudyThread, record_study
from .text import stable_id


class AttentionService:
    def __init__(self, runtime, clock=now_ts):
        self.runtime, self.repo, self.clock = runtime, runtime.repo, clock

    def seed(self, scope, topics):
        with self.repo.tx():
            active = self.repo._all("SELECT id FROM interests WHERE scope_id=? AND status='active'", (scope.scope_id,))
            remaining = max(0, 2 - len(active))
            for topic in dict.fromkeys(str(x).strip() for x in topics if str(x).strip()):
                if remaining <= 0:
                    break
                identity = stable_id("interest", scope.scope_id, topic)
                cursor = self.repo._exec("INSERT OR IGNORE INTO interests VALUES(?,?,?,?,'active',?)",
                                        (identity, scope.scope_id, topic[:120], json.dumps({"round": 0}), self.clock()))
                remaining -= int(cursor.rowcount > 0)

    def schedule(self, scope):
        with self.repo.tx():
            rows = self.repo._all("SELECT * FROM interests WHERE scope_id=? AND status='active' AND next_due<=? ORDER BY next_due LIMIT 2",
                                  (scope.scope_id, self.clock()))
            ids = []
            for row in rows:
                payload = json.loads(row["payload"])
                number = int(payload.get("round", 0)) + 1
                # A previous active cycle retains attention until it finishes.
                if payload.get("study_id"):
                    old = self.repo.get_study_thread(payload["study_id"])
                    if old is not None and old.status != "complete":
                        continue
                study_id = stable_id("study", row["id"], str(number))
                thread = StudyThread(study_id, scope, row["topic"], origin_want_id=row["id"],
                                     specific_question=f"{row['topic']}：一个有来源的具体概念和新的应用例子",
                                     learning_checks=["逐主张引用已读材料", "把来源陈述和真实应用区分"],
                                     budget={"root_id": row["id"]})
                record_study(self.repo, thread, expected_epoch=self.repo.forget_epoch(scope.scope_id))
                self.runtime.jobs.enqueue(scope, "study", {"study_id": study_id},
                                          key="study-job:" + study_id, root_id=row["id"])
                payload.update(round=number, study_id=study_id)
                self.repo._exec("UPDATE interests SET payload=?,next_due=? WHERE id=?",
                                (json.dumps(payload), self.clock() + 86400, row["id"]))
                ids.append(study_id)
            return ids
