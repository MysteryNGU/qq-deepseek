"""One informational notice per continuous action, never an approval request."""
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import asyncio
import json


@dataclass
class ActionNotice:
    intent: dict | None = None
    sent: bool = False
    awaiting_approval: bool = False


current_notice = ContextVar("action_notice", default=None)


def action_task(fn):
    @wraps(fn)
    async def run(*args, **kwargs):
        from .services.ctx import current_task
        task_token = current_task.set(None)
        token = current_notice.set(ActionNotice())
        try:
            return await fn(*args, **kwargs)
        finally:
            current_notice.reset(token)
            current_task.reset(task_token)
    return run


def assessed(intent):
    state = current_notice.get()
    if state is not None:
        state.intent = dict(intent)


async def natural_announcement(host, intent, action=""):
    """Reuse cognition's actual utterance, never wrap its internal goal."""
    text = str(intent.get('announcement') or '').strip()
    if not text or any(w in text for w in ('？', '?', '请确认', '请批准', '同意吗')):
        persona = host._persona_text() if hasattr(host, '_persona_text') else ''
        text = await asyncio.wait_for(host.llm.chat([
            {'role':'system','content':persona + '\n根据数据里的既定行动，直接向对方说一句简短自然的执行通知。不是新一轮意图判断，也不是确认问题。不要复述分析，不许添加“同意吗/请批准”等问句。只输出通知正文。'},
            {'role':'user','content':json.dumps({'goal':intent.get('goal') or action},ensure_ascii=False)}
        ], thinking=False, max_tokens=180, tag='行动通知'), 90)
        text = str(text or '').strip()
    if not text or any(w in text for w in ('？', '?', '请确认', '请批准', '同意吗')):
        raise RuntimeError('action_announcement_unavailable')
    from .speech import ModelText
    return ModelText(text)


async def before_action(host, target, peer, action=""):
    from .delivery import active_delivery, execution_context_current
    from .services.ctx import get_goal
    state = current_notice.get()
    if state is None:
        # Direct dispatch still observes the contract, even outside a loop.
        state = ActionNotice()
    if not execution_context_current():
        return False
    if state.awaiting_approval:
        return False
    if state.sent:
        return True
    if state.intent is None or not state.intent.get("goal"):
        state.intent = await asyncio.wait_for(host.understand(
            get_goal() or action,
            "这是我准备执行的行动。判断行动目的，不是新的用户指令；不要请求审批。"), 90)
    intent = state.intent or {}
    target = target or (("private", int(peer)) if peer else None)
    if (not str(intent.get("goal") or "").strip()
            or (intent.get("control") or {}).get("action") in {"pause", "cancel"}
            or target is None or not execution_context_current()):
        return False
    # Only a short semantic goal leaves the machine; never raw tool arguments.
    text = await natural_announcement(host, intent, action)
    ctx = active_delivery.get()
    if ctx is not None and ctx.lease is not None:
        runtime = ctx.service.runtime
        with runtime.jobs.commit(ctx.lease):
            runtime.repo._exec("UPDATE work_jobs SET checkpoint=? WHERE id=? AND lease_token=?",
                               (json.dumps({"stage": "action_started"}), ctx.lease.id, ctx.lease.token))
    if not await host._send(target, text):
        return False
    state.sent = True
    return execution_context_current()
