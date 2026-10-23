"""Conversation stop/cancel commands settle durable work before acknowledgement."""
from __future__ import annotations

import json
import re


def control_kind(text: str) -> str:
    text = str(text or "").strip().rstrip("。！!~～")
    # Questions, quotations and negated cancellations are not commands.
    if any(x in text for x in ("？", "?", "怎么", "如何", "为什么", "是否", "要不要", "能不能", "可不可以", "如果", "假如", "不取消", "不要取消", "别取消", "不用取消", "取消按钮", "取消键", "取消不了")):
        return ""
    if any(x in text for x in ('“', '”', '「', '」', '"', "'", '```')):
        return ""
    if re.search(r"(?:^/?取消(?:了|吧)?$|(?:任务|计划|意图|念想|这件事|这件|那个|这个).*取消|取消(?:刚才|之前|当前|这个|那个|任务|计划|意图|念想))", text):
        return "cancelled"
    if re.search(r"(?:^/?(?:停|停止|暂停|算了|打住)(?:了|吧)?$|停下|停手|别做了|别弄了|别动了|不要做了|别继续|不用继续|(?:先别|先不要|先不用|不用再)(?:执行|做|弄|动手|继续|跑))", text):
        return "paused"
    return ""


def control_candidates(runtime, scope) -> list[dict]:
    repo = runtime.repo
    candidates = {}
    for row in repo._all("SELECT payload FROM work_jobs WHERE scope_id=? AND kind='respond' AND status='running'", (scope.scope_id,)):
        turn = repo.get_turn(json.loads(row["payload"]).get("turn_id", ""))
        if turn:
            sources = repo.events_by_ids(turn.input_event_ids)
            ref = "reply:" + turn.id
            candidates[ref] = {"ref": ref, "status": "running", "label": sources[-1].text[:160] if sources else "当前回复"}
    for row in repo._all("SELECT id,status,payload FROM wants WHERE scope_id=? AND status IN ('proposed','active','paused')", (scope.scope_id,)):
        body = json.loads(row["payload"])
        ref = "want:" + row["id"]
        candidates[ref] = {"ref": ref, "status": row["status"], "label": body.get("goal", "")[:160]}
    for row in repo._all("SELECT id,status,payload,kind FROM work_jobs WHERE scope_id=? AND kind IN ('plan','study','plugin','selfmod') AND status IN ('pending','running','waiting','paused')", (scope.scope_id,)):
        body = json.loads(row["payload"])
        ref = "want:" + body["want_id"] if body.get("want_id") else "job:" + row["id"]
        item = candidates.get(ref, {"ref": ref, "label": row["kind"] + " " + row["id"]})
        item["status"] = row["status"]
        candidates[ref] = item
    for loop in repo.active_open_loops(scope):
        ref = "loop:" + loop.id
        candidates[ref] = {"ref": ref, "status": loop.status, "label": loop.summary[:160]}
    for row in repo._all("SELECT id,status,payload FROM work_jobs WHERE scope_id=? AND kind='solve' AND status IN ('pending','running','waiting','paused')", (scope.scope_id,)):
        body = json.loads(row['payload'])
        ref = 'job:' + row['id']
        candidates[ref] = {'ref': ref, 'status': row['status'], 'label': (body.get('task') or {}).get('goal', row['id'])}
    if scope.conversation_id.startswith("qq:private:") or scope.conversation_id == "console:local":
        for row in repo._all("SELECT id,status,text FROM intents WHERE peer=? AND status IN ('seed','active','unfinished','blocked','paused')", (int(scope.participant_id or 0),)):
            ref = "intent:" + str(row["id"])
            candidates[ref] = {"ref": ref, "status": row["status"], "label": row["text"][:160]}
    # Motivation, promise and execution are views of one business task.
    # Present its executable representative once to semantic intent judgement.
    for row in repo._all("SELECT v FROM meta WHERE k LIKE 'action_task:%'"):
        task = json.loads(row['v'])
        if task.get('scope_id') != scope.scope_id:
            continue
        aliases = ['job:solve:' + task['ref']]
        if task.get('want_id'):
            aliases.append('want:' + task['want_id'])
        if task.get('intent_id'):
            aliases.append('intent:' + str(task['intent_id']))
        aliases += ['loop:' + lid for lid in task.get('loop_ids', [])]
        visible = [ref for ref in aliases if ref in candidates]
        if visible:
            candidates[visible[0]]['task_ref'] = task['ref']
            for ref in visible[1:]:
                candidates.pop(ref)
    return list(candidates.values())


def apply_control(runtime, scope, event, mode: str, *, target_id="", current_turn_id="") -> dict:
    """Fence old replies, then stop the current task, preserving unrelated work."""
    if mode not in {"paused", "cancelled"}:
        raise ValueError("invalid_task_control")
    repo = runtime.repo
    now = runtime.clock()
    marker = "task_control:" + event.id
    with repo.tx():
        prior = repo._one("SELECT v FROM meta WHERE k=?", (marker,))
        if prior:
            return json.loads(prior["v"])
        result = {"mode": mode, "jobs": [], "wants": [], "intents": [], "ambiguous": False}
        candidates = control_candidates(runtime, scope)
        refs = {item["ref"] for item in candidates}
        if target_id and target_id not in refs:
            result["ambiguous"] = True
            target_id = "invalid"
        if not target_id:
            running = [item for item in candidates if item["status"] == "running"]
            choice = running if len(running) == 1 else candidates if len(candidates) == 1 else []
            target_id = choice[0]["ref"] if choice else ""
            result["ambiguous"] = len(candidates) > 1 and not choice
        result["target_id"] = target_id
        selected_task = next((item.get('task_ref') for item in candidates if item['ref'] == target_id), None)
        # Every older response is obsolete after this explicit control message.
        replies = repo._all("SELECT id,payload FROM work_jobs WHERE scope_id=? AND kind='respond' AND status IN ('pending','running','waiting')",
                            (scope.scope_id,))
        for row in replies:
            if json.loads(row["payload"]).get("turn_id") == current_turn_id:
                continue
            job = repo._one("SELECT created FROM work_jobs WHERE id=?", (row["id"],))
            if job and job["created"] > event.received_at:
                continue
            repo._exec("DELETE FROM resource_leases WHERE job_id=?", (row["id"],))
            repo._exec("UPDATE work_jobs SET status='cancelled',lease_token='',lease_until=0,updated=? WHERE id=?", (now, row["id"]))
            result["jobs"].append(row["id"])
            turn_id = json.loads(row["payload"]).get("turn_id")
            if turn_id:
                repo.set_turn_status(turn_id, "cancelled")
        jobs = repo._all("SELECT * FROM work_jobs WHERE scope_id=? AND kind IN ('plan','study','plugin','selfmod') AND status IN ('pending','running','waiting','paused') ORDER BY (status='running') DESC,updated DESC", (scope.scope_id,))
        if target_id.startswith('job:solve:'):
            solving = repo._one("SELECT root_id,id FROM work_jobs WHERE id=? AND scope_id=? AND kind='solve'", (target_id.removeprefix('job:'), scope.scope_id))
            if solving:
                result['jobs'].append(solving['id'])
                binding = repo._one('SELECT v FROM meta WHERE k=?', ('action_task:' + solving['root_id'],))
                if binding:
                    task = json.loads(binding['v'])
                    task['status'] = mode
                    repo._exec('UPDATE meta SET v=? WHERE k=?', (json.dumps(task), 'action_task:' + solving['root_id']))
                # Actual cancellation and resource release are owned by Solver.stop.
        selected = [row for row in jobs if ("want:" + str(json.loads(row["payload"]).get("want_id", "")) == target_id
                    or "job:" + row["id"] == target_id)]
        roots = set()
        if target_id.startswith("reply:"):
            prior_turn = repo.get_turn(target_id.removeprefix("reply:"))
            if prior_turn and prior_turn.scope.scope_id == scope.scope_id:
                roots.update(prior_turn.input_event_ids)
        for row in selected:
            roots.add(row["root_id"])
            payload = json.loads(row["payload"])
            for key, table in (("want_id", "wants"), ("study_id", "study_threads"), ("proposal_id", "evolution_proposals")):
                if key in payload:
                    repo._exec(f"UPDATE {table} SET status=? WHERE id=?", (mode, payload[key]))
                    if key == "want_id":result["wants"].append(payload[key])
        # A proposed intention may not yet have an execution job.
        if not selected:
            wants = repo._all("SELECT id,payload FROM wants WHERE scope_id=? AND status IN ('proposed','active','paused')", (scope.scope_id,))
            wants = [row for row in wants if "want:" + row["id"] == target_id]
            if len(wants) == 1:
                row = wants[0]
                repo._exec("UPDATE wants SET status=? WHERE id=?", (mode, row["id"]))
                roots.update(json.loads(row["payload"]).get("sources", []))
                result["wants"].append(row["id"])
            elif len(wants) > 1:
                result["ambiguous"] = True
        for row in jobs:
            if row["root_id"] not in roots:
                continue
            repo._exec("DELETE FROM resource_leases WHERE job_id=?", (row["id"],))
            repo._exec("UPDATE work_jobs SET status=?,lease_token='',lease_until=0,error='user_control',updated=? WHERE id=?", (mode, now, row["id"]))
            result["jobs"].append(row["id"])
        for root in roots:
            repo._exec("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", ("stopped_root:" + scope.scope_id + ":" + root, event.id))
        loops = repo.active_open_loops(scope)
        for loop in loops:
            if "loop:" + loop.id == target_id:
                roots.update(loop.source_event_ids)
        for loop in loops:
            if roots.intersection(loop.source_event_ids):
                repo.update_open_loop(loop.id, status=mode, waiting_for="user_control:" + event.id,
                                      updated_at=now, version=loop.version + 1)
                for root in loop.source_event_ids:
                    repo._exec("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", ("stopped_root:" + scope.scope_id + ":" + root, event.id))
        for want_id in result["wants"]:
            repo._exec("UPDATE grants SET revoked=1 WHERE scope_id=? AND plan_id=?", (scope.scope_id, want_id))
        stopped_turns = {json.loads(row["payload"]).get("turn_id") for row in replies
                         if json.loads(row["payload"]).get("turn_id") != current_turn_id}
        # Pending approvals must not offer a way to execute stopped work later.
        for table in ("consent_requests",):
            pending = repo._all(f"SELECT id,action FROM {table} WHERE peer=? AND status='pending'", (int(scope.participant_id or 0),))
            for row in pending:
                context = json.loads(row["action"]).get("context", {})
                if context.get("scope_id") == scope.scope_id and (
                        context.get("turn_id") in stopped_turns or roots.intersection(context.get("sources", []))):
                    repo._exec(f"UPDATE {table} SET status='cancelled',decided_ts=? WHERE id=?", (int(now), row["id"]))
        # Service intentions live in the same canonical runtime database.
        if scope.conversation_id.startswith("qq:private:") or scope.conversation_id == "console:local":
            intents = repo._all("SELECT id,status FROM intents WHERE peer=? AND status IN ('seed','active','unfinished','blocked','paused') ORDER BY (status='active') DESC,id DESC", (int(scope.participant_id or 0),))
            chosen = [row for row in intents if "intent:" + str(row["id"]) == target_id]
            for row in chosen:
                repo._exec("UPDATE intents SET status=?,blocked_reason=? WHERE id=?", ('dropped' if mode == 'cancelled' else 'paused', 'user_control:' + event.id, row["id"]))
                result["intents"].append(row["id"])
        if selected_task:
            from .tasks import TaskService
            task = TaskService(repo).transition(scope.scope_id, selected_task, mode, cause=event.id)
            if task.get('intent_id') and task['intent_id'] not in result['intents']:
                result['intents'].append(task['intent_id'])
            if task.get('want_id') and task['want_id'] not in result['wants']:
                result['wants'].append(task['want_id'])
        repo._exec("INSERT INTO meta(k,v) VALUES(?,?)", (marker, json.dumps(result)))
        return result


def control_reply(result: dict) -> str:
    if result.get("ambiguous"):
        return "我已停下这轮操作。请明确要暂停或取消哪一项；无法确定的待办没有改动。"
    if result["mode"] == "cancelled":
        return "好，我已停止这轮操作，并撤销了能确定的相关待办，不会自动重试。已经执行过的操作不会因此撤回。"
    return "好，我先停下这轮操作，相关任务已暂停，等你明确让我继续。"
