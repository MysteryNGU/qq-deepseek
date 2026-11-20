# -*- coding: utf-8 -*-
r"""子 agent 闭环：七个语义操作、状态机、并发配额、取消与续作。

先读这一段再改任何一行。这里有几条不能松的规矩，它们来自
``docs/companion-implementation-plan.md`` §4 与 P0 的实测结论：

1. **状态机是唯一真源。** ``STATES`` / ``TRANSITIONS`` 只在这里写一遍，
   别处（宿主、界面、测试）都读这里。终态不再自己动：重试是**新的 attempt**，
   不是把 ``failed`` 改回 ``running``。
2. **完成与核验分开。** ``status=completed`` 只说明子 agent 说做完了；
   ``verification`` 是另一列，``passed`` 必须有真实证据（证据文件存在且哈希对得上、
   逗留清单对得上、成功状态码）。不核验就是 ``unverified``/``pending``，不冒充通过。
3. **等待要有等待对象和恢复条件。** ``waiting`` 不带 ``waiting_for`` 与
   ``recover_when`` 一律拒绝；``completed`` 不带结果清单一律拒绝。
4. **未知就是未知。** 用量拿不到就记 ``None`` 并标 ``unknown``，绝不写 0。
   预算 ``strict`` 对 DSH 结构性不可行（P0 实测：SDK 只报 4 个 RPC、没有逐次
   usage），所以这里显式拒绝并给原因——不静默把能力藏起来，也不偷偷降级。
5. **不另建调度。** 任务主线用 ``life/tasks.py`` 的 ``TaskService``，续行用
   ``FollowThrough``，收件账本用 ``companion_delivery``，进程树用
   ``life/supervisor.py`` 的 ``ProcessTree``，SDK 子进程用
   ``life/companion_dsh_worker.py``。这个模块只补「子 agent 台账」与七操作，
   再把结果并回上面那几件已有的东西。
6. **不阻塞主角色的认知。** ``create``/``send``/``resume`` 登记文件类工作后立刻
   返回；真正跑在后台任务里，由 ``life/companion_executor.py`` 的
   ``ActionExecutor`` 把 ``kind=agent`` 干跑并收尾（它本来就有 ``companion_actions``
   的领取、进程树、取消与回执）。``wait`` 只给很短的一眼，没结果就说没结果。
7. **隔离与串行。** 同一仓库共用一份工作副本，靠一把文件锁串行；DSH 子 agent
   在各自独立的临时工作目录里跑，跑完删除，改动只留在会话记录里。
8. **主上下文很小。** 每个结果只回约 1200 个中文字符的摘要和最近 5 个改动；
   原文在证据文件里，用 ``memory op=read`` 按 id 取。

七操作（统一写成 ``kind=agent``，``op`` 决定语义）::

    create        起一个子 agent；返回 agent_id/task_ref/operation_id，status=queued
    send          给在跑的子 agent 追加消息；按 message_id 幂等
    inspect       读台账：状态、验证、消息、改动、失败与副作用
    wait          短暂等待并给 cursor；例如 wait_s=0 就是「现在有没有」
    cancel        终止工作进程与子孙进程，报告不可撤销的副作用
    resume        基于上次会话/检查点续作，生成新的 attempt，历史保留
    read_artifact 按 id/路径读证据原文，分页

对外接口（tests/test_agent_contract.py 的契约按这些函数写）：
``transition`` ``STATE_ALIASES`` ``decide`` ``merge_usage`` ``estimate_cn_chars``
``normalize_route`` ``reconcile_on_start`` ``AgentError`` ``AgentManager``。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .text import stable_id
from .companion_work import (PROVENANCE_MODEL, PROVENANCE_TOOL, missing, provenance,
                             visibility)

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ 常量

SCHEMA_VERSION = 'agent-1'
OPS = ('create', 'send', 'inspect', 'wait', 'cancel', 'resume', 'read_artifact')

# 每个 op 真正认的参数。这份表就是 dispatch() 里 **fields 展开的边界：
# 表外的键在 _action_fields 里就被丢掉，不会走到 AgentManager 的方法签名上。
# 改任何一个方法的签名，这张表要跟着改（tests/test_capability_cognition.py 会核对）。
OP_FIELDS = {
    'create': {'goal', 'command', 'deliverables', 'acceptance', 'workspace', 'node_requirements',
               'scope', 'budget', 'parent_task_ref', 'idempotency_key', 'context_refs',
               'node', 'node_reason', 'resume_session_id'},
    'send': {'agent_id', 'text', 'message_id', 'intent'},
    'inspect': {'agent_id', 'changes', 'messages'},
    'wait': {'agent_id', 'until', 'wait_s', 'cursor'},
    'cancel': {'agent_id', 'include_children', 'why'},
    'resume': {'agent_id', 'goal', 'from_session', 'checkpoint'},
    'read_artifact': {'agent_id', 'artifact_id', 'path', 'offset', 'limit'},
}

# 状态机：唯一的真源（设计 §4.2）。终态集合里的状态不会再自己动。
STATES = ('queued', 'running', 'completed', 'failed', 'waiting',
          'interrupted', 'cancelled', 'cancelling')
TRANSITIONS = {
    # queued 可以直接落成 cancelled（还没开始跑就被撤掉）；已经在跑的只能先
    # cancelling 再 cancelled，好让「已请求取消」这个中间态被看见。等外部条件的
    # 那一步是 running→waiting：登记了却从没开始跑的子 agent 不叫「在等」。
    'queued': {'running', 'cancelled'},
    'running': {'completed', 'failed', 'waiting', 'interrupted', 'cancelling'},
    'waiting': {'running', 'failed', 'interrupted', 'cancelled'},
    'cancelling': {'cancelled'},
    'completed': set(),
    'failed': set(),
    'interrupted': set(),
    'cancelled': set(),
}
TERMINAL = ('completed', 'failed', 'interrupted', 'cancelled')
LIVE = ('queued', 'running', 'waiting', 'cancelling')
VERIFICATIONS = ('pending', 'passed', 'failed', 'unverified')

# 她自己的说法和台账之间的对照。宿主只认台账里的规范名。
STATE_ALIASES = {
    'queued': 'queued', 'pending': 'queued', 'scheduled': 'queued', 'created': 'queued',
    'running': 'running', 'executing': 'running', 'in_progress': 'running',
    'completed': 'completed', 'done': 'completed', 'succeeded': 'completed',
    'failed': 'failed', 'error': 'failed', 'errored': 'failed',
    'waiting': 'waiting', 'blocked': 'waiting',
    'interrupted': 'interrupted', 'stopped': 'interrupted', 'killed': 'interrupted',
    'cancelled': 'cancelled', 'canceled': 'cancelled',
    'cancelling': 'cancelling', 'canceling': 'cancelling',
}

# 子 agent 的总并发与嵌套层数（设计 §4.3）：并发第 4 个进队列，不失败；
# 深度 = 它上面还有几层父任务，2 就是第三层，直接拒绝并说明。
MAX_CONCURRENT = 3
MAX_DEPTH = 2
MIN_TERMINATED_MAX_CONCURRENT = 2
# 「短时限」的上界：wait 不是同步阻塞，主角色不能在这里等十分钟。
MAX_WAIT_SECONDS = 20.0
DEFAULT_WAIT_SECONDS = 0.0
DEFAULT_LEASE_SECONDS = 1800.0
DEFAULT_TIMEOUT_SECONDS = 1800.0
# 主上下文里每个子 agent 的回包大小（设计 §4.4 / §6.2）。
SUMMARY_CHARS = 1200
CHANGES_SHOWN = 5
OUTPUT_TAIL_CHARS = 2000
ARTIFACT_DEFAULT_BYTES = 4000
ARTIFACT_MAX_BYTES = 20000

KIND = 'agent'
LOCAL_NODE = 'local'
CLOUD_NODE = 'cloud'
ORIGINS = {
    'dsh': 'dsh',
    'work': 'work',
    'command': 'work',
    'echo': 'stub',
}


# 结果回到会话时的收件账本（``life/companion_delivery``）。完整运行时里它一直在，
# 精简检出（只有生活库、没有伴生模块）里就没有；调用方据此知道「回执没地方登记」，
# 而不是以为已经送达。
try:  # noqa: SIM105 - 两种检出的差别必须显式给出来
    from . import companion_delivery as delivery_ledger  # noqa: F401
except ImportError:  # pragma: no cover - 只在精简检出里发生
    delivery_ledger = None


class AgentError(ValueError):
    """七操作里可以讲清楚的拒绝：带一个稳定的 code 和中文说明。"""

    def __init__(self, code, message, **details):
        super().__init__('%s: %s' % (code, message))
        self.code, self.message, self.details = code, message, dict(details)

    def as_result(self, **extra):
        return dict(status='rejected', code=self.code, reason=self.message,
                    output=self.message, **self.details, **extra)


# ------------------------------------------------------------------ 小工具

def transition(state, target, **guards):
    """状态机的唯一入口：非法迁移抛 ``AgentError('illegal_transition')``。

    ``completed`` 必须带结果清单；``waiting`` 必须带 ``waiting_for`` 与
    ``recover_when``。这两条在表里写不出来，于是在这里挡——挡在写库之前，
    不是写完了再解释。
    """
    source = STATE_ALIASES.get(str(state).strip().lower())
    if source is None:
        raise AgentError('unknown_state', '没有这个状态：%r' % (state,))
    goal = STATE_ALIASES.get(str(target).strip().lower())
    if goal is None:
        raise AgentError('unknown_state', '没有这个状态：%r' % (target,))
    if goal == source:
        raise AgentError('self_transition', '状态没有变化（%s）；没有新事实就不改状态' % source)
    if goal not in TRANSITIONS[source]:
        raise AgentError('illegal_transition',
                         '不能从 %s 直接变成 %s；允许的是 %s' % (
                             source, goal, '、'.join(sorted(TRANSITIONS[source])) or '（终态，不再变化）'))
    if goal == 'completed' and 'result' in guards:
        # 只在调用方**带了**结果字段时才查清单：这张表回答的是「这两个状态之间
        # 能不能走」，不是「这次有没有清单」。「没带清单就标完成」在写库那一层
        # （``AgentManager.transition``）挡，那里才是真在宣布完成。
        result = guards.get('result')
        if not isinstance(result, dict) or not str(result.get('outcome') or '').strip():
            raise AgentError('completed_requires_result',
                             '标记 completed 要给结果清单（outcome/summary/changes/tests/artifacts/evidence_refs/'
                             'remaining/blockers/side_effects/usage）；计划和受理都不是完成')
    if goal == 'waiting' and ('waiting_for' in guards or 'recover_when' in guards):
        # 同上：只有调用方带了这两个字段才查；真写库时（AgentManager.transition）
        # 没有这两个字段就根本进不了 waiting。
        if not (str(guards.get('waiting_for') or '').strip()
                and str(guards.get('recover_when') or '').strip()):
            raise AgentError('waiting_requires_guards',
                             'waiting 必须同时给出 waiting_for（在等谁/什么）和 recover_when（什么条件下恢复）；'
                             '只说“等一会”等于没有落点')
    return goal


BUDGET_MODES = ('off', 'bounded_estimate', 'strict')
# 裁决里必须给出的四件东西（实施契约 §11）：模式、上限、哪些上限真能强制、用量怎么记账。
BUDGET_KEYS = ('allowed', 'mode', 'limits', 'missing_limits', 'enforceable', 'accounting',
               'usage_known', 'reason', 'provider')

# 调用次数上限只能当护栏：DSH 与主机命令都不报「这次跑了几次模型调用」，
# 所以它可读、可写、可提示，但 enforcement 只能是 false——不能假装挡住了。
ENFORCEABLE = dict(concurrency=True, runtime_s=True, calls=False)
# 记账口径：off 不记账；bounded_estimate 已知就记、未知记 unknown_calls 不写 0；
# strict 要求每次都有完整账单，缺一次就拒绝派活。
ACCOUNTING = dict(off='off', bounded_estimate='known_or_unknown_never_zero',
                  strict='complete_per_call')


def _verdict(mode, *, allowed, limits=None, missing_limits=(), reason='', provider='',
             usage_known=False, **extra):
    """所有预算裁决都从这里出：字段齐、口径统一、原因可见。"""
    verdict = dict(allowed=bool(allowed), mode=mode, limits=dict(limits or {}),
                   missing_limits=list(missing_limits), enforceable=dict(ENFORCEABLE),
                   accounting=dict(mode=ACCOUNTING[mode], unknown_written_as_zero=False),
                   usage_known=bool(usage_known), reason=reason, provider=str(provider or ''))
    verdict.update(extra)
    return verdict


def decide(*, mode, task=None):
    """预算三模式对一次子 agent 工作的裁决（设计 §5 / 实施契约 §11）。

    - ``off``：不设限，也不记账。
    - ``bounded_estimate``：**可以派活**；限并发与时长，调用次数只当护栏
      （``enforceable.calls=False``）；没定的上限写进 ``missing_limits``，用量按
      「已知就记、未知记 unknown_calls」处理，未知绝不写 0。
    - ``strict``：要求每次调用都有完整账单。DSH 结构性拿不到逐次 usage，所以
      ``allowed=False`` 且带 ``needs_setup`` 与原因——不静默改用户的选择、也不
      静默把它降级成 bounded_estimate。

    ``task`` 里可带：``provider``（dsh/work/echo）、``usage_known``、``concurrency``、
    ``runtime_s``、``calls``。上限为 0 或缺失都算「还没定」，不当成 0 次。
    """
    task = dict(task or {})
    budget = dict(task.get('budget') or {}) if isinstance(task.get('budget'), dict) else {}
    mode = str(mode or budget.get('mode') or 'off')
    if mode not in BUDGET_MODES:
        raise AgentError('unknown_budget_mode',
                         '预算模式只有 off/bounded_estimate/strict，收到的是 %r' % (mode,))
    provider = str(task.get('provider') or '')
    if mode == 'off':
        return _verdict(mode, allowed=True, provider=provider)
    if mode == 'bounded_estimate':
        known, missing = {}, []
        for field in ('concurrency', 'runtime_s', 'calls'):
            value = task.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                known[field] = value
            else:
                missing.append(field)
        limits = dict(concurrency=known.get('concurrency') or MAX_CONCURRENT,
                      runtime_s=known.get('runtime_s') or DEFAULT_TIMEOUT_SECONDS,
                      calls=known.get('calls'))
        return _verdict(
            mode, allowed=True, limits=limits, missing_limits=[field for field in missing
                                                               if limits.get(field) is None],
            provider=provider,
            reason=('这些上限还没定，按不设限执行、用量记为未知：' + '、'.join(missing)) if missing else '',
            note='调用次数上限只作提示，不能强制（DSH 与主机命令都不报调用次数）')
    usage_known = bool(task.get('usage_known'))
    if provider == 'dsh' or not usage_known:
        return _verdict(
            mode, allowed=False, provider=provider, missing_limits=['usage'], needs_setup=True,
            status='needs_setup',
            reason=('strict 需要每一次调用的完整逐次用量；%s。'
                    '要么在配置里改成 bounded_estimate，要么接受用量记为未知——'
                    '不会替你改成 strict 以外的模式。'
                    % ('DSH 只暴露 4 个 RPC、没有逐次 usage 字段' if provider == 'dsh'
                       else '这个后端不报用量')))
    return _verdict(mode, allowed=True, provider=provider, usage_known=True)


def budget_mode(policy):
    """从真实的预算策略对象里读出模式：off | strict | bounded_estimate。

    唯一真源是 ``SpendingPolicy.effective_mode()``（H3 之后 dataclass 自己带 ``mode``，
    ``parse()`` 会拒掉非法值）。这里只做一次「兜底的兼容」：拿不到 ``effective_mode``
    的旧对象按 `enabled` 读——`enabled=True` 且没有 mode 仍是 **strict**，不静默降级。
    """
    if policy is None:
        return 'off'
    resolve = getattr(policy, 'effective_mode', None)
    if callable(resolve):
        mode = str(resolve())
    elif not getattr(policy, 'enabled', False):
        mode = 'off'
    else:
        mode = str(getattr(policy, 'mode', None) or 'strict')
    if mode not in BUDGET_MODES:
        # 走到这里说明策略对象是被手工改坏的：宁可报错，也不猜一个模式出来。
        raise AgentError('unknown_budget_mode',
                         '预算模式只有 %s，收到的是 %r' % ('/'.join(BUDGET_MODES), mode))
    return mode


def budget_decision(policy, *, provider='', task=None):
    """把真实策略对象转成一次裁决：create 与 dsh 守卫共用这一个入口。"""
    merged = dict(task or {})
    merged.setdefault('provider', provider)
    return decide(mode=budget_mode(policy), task=merged)


USAGE_FIELDS = ('input_tokens', 'output_tokens', 'total_tokens', 'cost')


def _number(value):
    """只认真的数：``None``、字符串、布尔值都当「不知道」。"""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def merge_usage(known, unknown):
    """把两笔用量并起来：任何一边缺字段，结果里那个字段就是 ``None`` 并标 ``unknown``。

    缺的数不能补 0——0 是「这次真的没用」，``None`` 才是「不知道」。

    - 两边都没有 → ``None``（「一点测量都没有」，不是 0）。
    - 只有一边有 → 只保留那一边的已知值，其余 ``None``，``unknown=True``。
    - 两边都有 → 每个字段两边都是数就相加；有一边是 ``None`` 就整体记 ``None``。
    """
    if known is None and unknown is None:
        return None
    if known is None or unknown is None:
        side = dict(known if known is not None else unknown)
        merged = {key: _number(side.get(key)) for key in USAGE_FIELDS}
        merged['unknown'] = True
        merged['measured_side'] = 'known' if known is not None else 'unknown'
        return merged
    merged, missing = {}, False
    for key in USAGE_FIELDS:
        a, b = _number(known.get(key)), _number(unknown.get(key))
        if a is not None and b is not None:
            merged[key] = a + b
        else:
            # 只要有一边缺，总和就不知道；缺的那边不按 0 算。
            merged[key] = None
            missing = True
    merged['unknown'] = bool(missing or known.get('unknown') or unknown.get('unknown'))
    return merged


def estimate_cn_chars(text):
    """粗估中文字符数：一个汉字算 1，一段其它字符算 0.5。用来卡摘要长度。"""
    text = str(text or '')
    wide = sum(1 for ch in text if '\u3400' <= ch <= '\u9fff' or '\uff00' <= ch <= '\uffef')
    return wide + (len(text) - wide) / 2.0


def clip_text(text, limit_chars):
    """按中文字符预算截断，并在结尾说明还有多少没放进来。"""
    text = str(text or '')
    if limit_chars <= 0 or estimate_cn_chars(text) <= limit_chars:
        return text, False
    out, width = [], 0.0
    for ch in text:
        cost = 1.0 if ('\u3400' <= ch <= '\u9fff' or '\uff00' <= ch <= '\uffef') else 0.5
        if width + cost > limit_chars - 20:
            break
        out.append(ch)
        width += cost
    return ''.join(out) + '\n…（还有 %d 字没放进摘要，原文用 read_artifact）' % (len(text) - len(out)), True


def _now():
    return time.time()


def _stamp(value):
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(value))


def normalize_route(*, conversation_ref, account='self', size_limit=10 * 1024 * 1024):
    """把会话引用讲清楚：平台、私聊/群聊、对方是谁；裸数字不是会话。

    ``account`` 只接受 ``self``（她自己的号）或 ``account:<id>``；跨平台永远不
    合并——``qq:private:1`` 和 ``wechat:private:1`` 是两个会话。
    """
    ref = str(conversation_ref or '').strip()
    if not ref:
        raise AgentError('conversation_ref_required', '需要 conversation_ref（例如 qq:private:1）')
    if ref.isdigit():
        raise AgentError('conversation_ref_ambiguous',
                         '裸数字不能当会话：不知道是哪个平台、私聊还是群聊。写成 qq:private:<号> 这样')
    parts = ref.split(':')
    if len(parts) < 3 or parts[0] not in {'qq', 'wechat', 'desktop', 'mobile'}:
        raise AgentError('conversation_ref_unsupported', '认不出这段会话：%r' % ref)
    kind = parts[1]
    if kind not in {'private', 'group', 'device'}:
        raise AgentError('conversation_ref_unsupported', '会话类型只能是 private/group/device，收到 %r' % kind)
    owner = str(account or 'self').strip()
    if owner != 'self' and not owner.startswith('account:'):
        raise AgentError('account_ref_invalid', 'account 只能写 self 或 account:<id>')
    return dict(conversation_ref=ref, platform=parts[0], kind=kind, peer=':'.join(parts[2:]),
                account=owner, delivery=dict(targets=[parts[0]],
                                             conversation_ref=ref, note='投递只回到这段会话，不跨平台'),
                size_limit=size_limit)


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _file_digest(path):
    path = Path(path)
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _safe_json(text, default=None):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _life_of(core):
    """``core`` 既可能是 ``Companion``（有 ``.life``），也可能就是 ``LifeRuntime``。

    后者在离线测试里很常用：那时没有角色、没有模型，但仓库、助手、任务都在，
    台账照样能验证。不靠类名判断，只看它有没有 ``agent_id`` 与 ``repo``。
    """
    if core is None:
        return None
    inner = getattr(core, 'life', None)
    if inner is not None:
        return inner
    if hasattr(core, 'agent_id') and hasattr(core, 'repo') and hasattr(core, 'scope'):
        return core
    return None


def _has_table(repo, name):
    return bool(repo._all("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _repo_of(target):
    """``repo`` 参数里既可能给 ``Repository``，也可能给 ``LifeRuntime``。"""
    if target is None:
        return None, None
    if hasattr(target, '_exec'):
        return target, None
    if hasattr(target, 'repo'):
        return target.repo, target
    return target, None


def folder_lock_path(folder, home):
    """同一仓库只有一份工作副本：锁文件按目录路径派生，跨进程也认得出。"""
    tag = hashlib.sha256(str(Path(folder).resolve()).encode('utf-8')).hexdigest()[:16]
    folder = (Path(home) / 'data/companion/locks').resolve()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / ('work-%s.lock' % tag)


# 线程锁：同一进程里可能有多个协程/线程同时写同一份台账（执行器后台 + 主轮
# 的 inspect），把它们之间的「读改写」串起来，避免 lost update。协程里用完
# 立刻释放，不跨 ``await`` 持有，所以它不会拖住别的任务。
_TABLE_LOCK = threading.RLock()


class FileLock:
    """跨线程/跨进程的互斥：同一仓库的改动串行，不并发互相覆盖。

    Windows 用 ``msvcrt.locking``，其它平台用 ``fcntl.flock``；两边的语义都是
    「锁住一个字节，进程退出自动释放」。拿不到锁时按 ``timeout`` 等，超时抛错——
    等不到就说等不到，不假装抢到了。
    """

    def __init__(self, path, timeout=300.0):
        self.path, self.timeout, self.handle = Path(path), float(timeout), None

    def __enter__(self):
        self.handle = Path(self.path).open('a+b')  # noqa: SIM115 - 跨 await 持有
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._acquire()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise AgentError('workspace_locked',
                                     '另一个子 agent 正在同一份工作副本里改东西，等了 %.0f 秒还没轮到；'
                                     '先把这次放下，稍后再来。' % self.timeout)
                time.sleep(0.05)

    def _acquire(self):
        if os.name == 'nt':
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, *exc):
        try:
            if self.handle is not None:
                if os.name == 'nt':
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
        return False


# ------------------------------------------------------------------ 验证

def _scripted_fake_harness(goal):
    """离线测试与「假服务」：按 goal 里的脚本给出行为，不连任何模型。

    脚本是 JSON：``{"exit_code":0,"stdout":"...","artifacts":{"report.md":"..."},
    "sleep_s":0,"calls":1}``。它跑在子进程里，和真的 worker 同一条进程树，
    所以取消、中断、收尾都和真的一样，只是不花钱也不算账。
    """
    import sys
    data = _safe_json(goal, None)
    if not isinstance(data, dict):
        data = dict(exit_code=0, stdout=str(goal or '')[:400])
    sleep_s = float(data.get('sleep_s') or 0)
    if sleep_s:
        time.sleep(min(max(sleep_s, 0), 600))
    for name, content in (data.get('artifacts') or {}).items():
        target = Path(name)
        if target.is_absolute() or '..' in target.parts:
            print('artifact path refused: %s' % name, file=sys.stderr, flush=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding='utf-8')
    if data.get('stderr'):
        print(str(data['stderr']), file=sys.stderr, flush=True)
    print(str(data.get('stdout') or ''), flush=True)
    raise SystemExit(int(data.get('exit_code') or 0))


def _run_node(node, action, position, environment):
    """工作进程入口：由 ProcessTree 起在独立工作目录里。"""
    if node == 'echo':
        return _scripted_fake_harness(action.get('goal'))
    if node == 'command':
        import subprocess
        return subprocess.call(str(action.get('command') or ''), shell=True)
    raise SystemExit(3)


# ------------------------------------------------------------------ 管理器

class AgentManager:
    """子 agent 的唯一台账与七操作实现。

    构造参数和 ``ActionExecutor`` 一样是 ``(cfg, home)``，这样它既能在执行器里
    被懒装载，也能在测试里直接装一份。``TaskService`` 只靠 ``repo`` 工作，
    所以在没有 ``Companion`` 的地方也能用（``_tasks`` 会自建一个）。
    """

    def __init__(self, cfg, home, *, executor=None, core=None, repo=None, tasks=None,
                 scope_id=None, agent_id=None):
        self.cfg, self.home = cfg, Path(home).resolve()
        self.executor, self.core = executor, core
        # 第一个位置参数在角色里是 cfg，在离线测试里常常直接就是 LifeRuntime。
        inferred_life = _life_of(cfg)
        if inferred_life is not None and core is None:
            core = cfg
        self.life = _life_of(core) if core is not None else inferred_life
        self.repo = repo or getattr(core, 'repo', None) or getattr(self.life, 'repo', None)
        self._tasks = tasks
        self.scope_id = scope_id
        self.agent_id = (agent_id or getattr(self.life, 'agent_id', None)
                         or getattr(core, 'agent_id', None) or 'agent')
        self._lock = None
        self._live = {}
        self._executors = set()
        if self.repo is not None:
            self.ensure_schema(self.repo)

    # -- 基础设施 ---------------------------------------------------

    @property
    def tasks(self):
        if self._tasks is None:
            if self.repo is None:
                return None
            from .tasks import TaskService
            self._tasks = TaskService(self.repo)
        return self._tasks

    @property
    def lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def ensure_schema(repo):
        """两张表：子 agent 台账与它的消息/事件流水。只建不删，可重复调用。"""
        with repo.tx():
            repo._exec(
                'CREATE TABLE IF NOT EXISTS companion_agent_tasks('
                'id TEXT PRIMARY KEY, agent TEXT NOT NULL, task_ref TEXT NOT NULL DEFAULT "",'
                ' parent_id TEXT NOT NULL DEFAULT "", depth INTEGER NOT NULL DEFAULT 0,'
                ' status TEXT NOT NULL, verification TEXT NOT NULL DEFAULT "pending",'
                ' node TEXT NOT NULL DEFAULT "local", origin TEXT NOT NULL DEFAULT "work",'
                ' goal TEXT NOT NULL DEFAULT "", project TEXT NOT NULL DEFAULT "",'
                ' scope_id TEXT NOT NULL DEFAULT "", conversation_ref TEXT NOT NULL DEFAULT "",'
                ' idempotency_key TEXT NOT NULL DEFAULT "", message_id TEXT NOT NULL DEFAULT "",'
                ' budget TEXT NOT NULL DEFAULT "{}", workspace TEXT NOT NULL DEFAULT "",'
                ' workdir TEXT NOT NULL DEFAULT "", result TEXT, failure TEXT,'
                ' waiting_for TEXT NOT NULL DEFAULT "", recover_when TEXT NOT NULL DEFAULT "",'
                ' cancel_reason TEXT NOT NULL DEFAULT "", attempt INTEGER NOT NULL DEFAULT 1,'
                ' resumed_from TEXT NOT NULL DEFAULT "", session_id TEXT NOT NULL DEFAULT "",'
                ' checkpoint TEXT NOT NULL DEFAULT "{}", pid INTEGER NOT NULL DEFAULT 0,'
                ' lease_until REAL NOT NULL DEFAULT 0, created REAL NOT NULL, started REAL,'
                ' finished REAL, updated REAL NOT NULL, summary_sent_at REAL)')
            # 活动账本（角色里也建同一张表）：精简检出单独用台账时也要有地方记「重启收尾」。
            repo._exec('CREATE TABLE IF NOT EXISTS companion_activity('
                       'id TEXT PRIMARY KEY,agent TEXT,type TEXT,text TEXT,details TEXT,created REAL)')
            repo._exec('CREATE INDEX IF NOT EXISTS ix_companion_agent_tasks_live '
                       'ON companion_agent_tasks(agent, status, updated)')
            repo._exec('CREATE INDEX IF NOT EXISTS ix_companion_agent_tasks_task '
                       'ON companion_agent_tasks(agent, task_ref)')
            repo._exec(
                'CREATE TABLE IF NOT EXISTS companion_agent_events('
                'id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, message_id TEXT NOT NULL,'
                ' kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT "", payload TEXT NOT NULL DEFAULT "{}",'
                ' created REAL NOT NULL)')
            repo._exec('CREATE UNIQUE INDEX IF NOT EXISTS ux_companion_agent_events_message '
                       'ON companion_agent_events(agent_id, message_id)')

    # 列顺序与 ``ensure_schema`` 里那张表一致；按名字列出，改表时漏一列会当场
    # 报「列数与值数不等」，而不是把值悄悄塞错位置。
    COLUMNS = ('id', 'agent', 'task_ref', 'parent_id', 'depth', 'status', 'verification',
               'node', 'origin', 'goal', 'project', 'scope_id', 'conversation_ref',
               'idempotency_key', 'message_id', 'budget', 'workspace', 'workdir', 'result',
               'failure', 'waiting_for', 'recover_when', 'cancel_reason', 'attempt',
               'resumed_from', 'session_id', 'checkpoint', 'pid', 'lease_until', 'created',
               'started', 'finished', 'updated', 'summary_sent_at')

    # NOT NULL 但允许为空的列（库里带 DEFAULT）：None 就落成列默认值要求的空串/0。
    _EMPTY = {'message_id': '', 'waiting_for': '', 'recover_when': '', 'cancel_reason': '',
              'resumed_from': '', 'session_id': '', 'checkpoint': '{}', 'budget': '{}',
              'workdir': '', 'pid': 0, 'lease_until': 0.0, 'verification': 'pending'}

    def _insert(self, record):
        values = []
        for column in self.COLUMNS:
            value = record.get(column)
            if isinstance(value, Path):
                value = str(value)
            if value is None:
                value = self._EMPTY.get(column)
            values.append(value)
        with _TABLE_LOCK:
            with self.repo.tx():
                self.repo._exec('INSERT INTO companion_agent_tasks(%s) VALUES(%s)'
                                % (','.join(self.COLUMNS), ','.join('?' * len(self.COLUMNS))),
                                tuple(values))

    # -- 行读写 -----------------------------------------------------

    def _row(self, agent_id):
        if self.repo is None:
            raise AgentError('no_store', '还没接上台账：没有可写的生活库。')
        row = self.repo._one('SELECT * FROM companion_agent_tasks WHERE id=? AND agent=?',
                             (str(agent_id or ''), self.agent_id))
        if not row:
            raise AgentError('unknown_agent', '台账里没有这个子 agent：%s' % (agent_id,))
        return dict(row)

    def _update(self, agent_id, **fields):
        if not fields:
            return self._row(agent_id)
        fields['updated'] = _now()
        columns = ', '.join('%s=?' % key for key in fields)
        with _TABLE_LOCK:
            with self.repo.tx():
                self.repo._exec('UPDATE companion_agent_tasks SET %s WHERE id=? AND agent=?' % columns,
                                (*fields.values(), str(agent_id), self.agent_id))
            return self._row(agent_id)

    def _set_status(self, agent_id, target, *, result=None, failure=None, waiting_for='',
                    recover_when='', cancel_reason='', verification=None):
        with _TABLE_LOCK:
            row = self._row(agent_id)
            state = transition(row['status'], target, result=result,
                               waiting_for=waiting_for, recover_when=recover_when)
            if state == 'completed':
                # 表只回答「能不能走」；「这次真的完成了吗」在这里挡：没有结果清单
                # 就不许把台账写成 completed，宁可让调用方补清单，也不留一条假完成。
                self._require_manifest(result)
            fields = dict(status=state)
            if result is not None:
                fields['result'] = json.dumps(result, ensure_ascii=False)
            if failure is not None:
                fields['failure'] = json.dumps(failure, ensure_ascii=False) if isinstance(failure, dict) \
                    else str(failure)[:2000]
            if state == 'waiting':
                fields.update(waiting_for=str(waiting_for)[:400], recover_when=str(recover_when)[:400])
            if state in TERMINAL:
                fields['finished'] = _now()
                fields['waiting_for'] = ''
                fields['recover_when'] = ''
            if state == 'cancelled':
                fields['cancel_reason'] = str(cancel_reason or '')[:500]
            if verification is not None:
                fields['verification'] = verification
            if state == 'running' and not row.get('started'):
                fields['started'] = _now()
            updated = self._update(agent_id, **fields)
        self._mirror(updated, changed=True)
        return updated

    @staticmethod
    def _require_manifest(result):
        if not isinstance(result, dict) or not str(result.get('outcome') or '').strip():
            raise AgentError('completed_requires_result',
                             '标记 completed 要给结果清单（outcome/summary/changes/tests/artifacts/evidence_refs/'
                             'remaining/blockers/side_effects/usage）；计划和受理都不是完成')

    def _claim(self, agent_id, *, from_queued_only=False):
        """原子领取：只有一个执行者能把 queued 翻成 running。

        ``from_queued_only`` 供「领了马上要等」这条路径用：只碰 queued，避免把
        另一条真在跑的记录改坏。
        """
        with _TABLE_LOCK:
            row = self._row(agent_id)
            allowed = {'queued'} if from_queued_only else {'queued', 'waiting'}
            if row['status'] not in allowed:
                return False
            self._set_status(agent_id, 'running')
            return True

    def _log(self, agent_id, kind, text='', payload=None, message_id=''):
        mid = message_id or stable_id('agent-event', agent_id, kind, uuid.uuid4().hex)
        with _TABLE_LOCK:
            with self.repo.tx():
                self.repo._exec('INSERT OR IGNORE INTO companion_agent_events VALUES(?,?,?,?,?,?,?)',
                                (stable_id('agent-event-row', agent_id, mid), agent_id, mid, kind,
                                 str(text or '')[:4000], json.dumps(payload or {}, ensure_ascii=False, default=str),
                                 _now()))
        return mid

    def _mirror(self, row, *, changed=False):
        """把台账状态镜像进它所属的任务（``life/tasks.py``），不另建状态机。

        任务主线只认 active/waiting/paused/cancelled/done 五个状态；映射在
        ``TASK_STATUS`` 里写死，界面和续行都读这条任务。镜像失败不影响台账本身。
        """
        tasks, task_ref, scope_id = self.tasks, row.get('task_ref'), row.get('scope_id')
        if not tasks or not task_ref or not scope_id:
            return
        desired = TASK_STATUS.get(row['status'])
        if desired is None:
            return
        try:
            current = tasks.get(task_ref, scope_id)
            if not current:
                return
            if desired == 'waiting':
                # 台账已经写明在等什么；任务主线同步这两条，续行判定才能读到。
                # TaskService.ensure 遇到 goal=None 会原样写回，所以这里必须传字符串。
                tasks.ensure(_ScopeShim(scope_id, row.get('conversation_ref') or ''), task_ref,
                             current.get('goal') or '',
                             status='waiting', wait_reason='agent:' + (row.get('waiting_for') or ''),
                             blocked_on=row.get('waiting_for') or '',
                             recover_when=row.get('recover_when') or '')
                return
            if current.get('status') == desired:
                return
            if desired == 'active' and current.get('status') in {'done', 'cancelled'}:
                return
            tasks.transition(scope_id, task_ref, desired, cause='agent:' + row['status'])
        except Exception:  # noqa: BLE001 - 镜像不是台账的真相来源
            log.warning('子 agent 状态镜像到任务主线失败', exc_info=True)

    def _mirror_receipt(self, row, text):
        """结果回执进项目任务：续行与下一轮都能读到，不靠内存。"""
        tasks, task_ref, scope_id = self.tasks, row.get('task_ref'), row.get('scope_id')
        if not tasks or not task_ref or not scope_id:
            return
        try:
            current = tasks.get(task_ref, scope_id) or {}
            tasks.ensure(_ScopeShim(scope_id, row.get('conversation_ref') or ''), task_ref,
                         current.get('goal') or '',
                         last_agent_receipt=dict(agent_id=row['id'], status=row['status'],
                                                 verification=row['verification'],
                                                 at=_now(), summary=str(text or '')[:600]))
        except Exception:  # noqa: BLE001
            log.warning('子 agent 回执写入任务主线失败', exc_info=True)

    def _followthrough(self):
        core = self._core()
        return getattr(core, 'followthrough', None)

    def _core(self):
        """角色本身：执行器里装着它（``attach_core``），测试里直接传 ``core=``。"""
        if self.core is not None:
            return self.core
        executor = self.executor
        return getattr(executor, '_core', None) if executor is not None else None

    # -- create -----------------------------------------------------

    def create(self, goal, *, deliverables=(), acceptance=(), workspace=None, node_requirements=None,
               scope='private', budget=None, parent_task_ref=None, idempotency_key=None,
               context_refs=(), project='', conversation_ref='', node=None, node_reason='',
               resume_session_id='', command='', _action_id=None):
        """登记一个子 agent 并立刻返回 ``queued``；执行由 ``ActionExecutor`` 在后台接头。

        幂等键是 ``idempotency_key``（缺省是规范化的 goal + 交付物 + 工作区）：
        同一个键再来一次，返回同一个 agent 和同一份回执，不新建第二个。
        """
        if self.repo is None:
            raise AgentError('no_store', '还没有生活库，子 agent 无处登记。')
        goal = str(goal or '').strip()
        if not goal:
            raise AgentError('goal_required', 'create 需要 goal：要达成什么、在哪做、怎样算做完。')
        command = str(command or '').strip()
        deliverables = _items(deliverables, 200, 12)
        acceptance = _items(acceptance, 400, 12)
        workspace, workspace_root = self.workspace_path(workspace)
        workspace = str(workspace)
        key = str(idempotency_key or '').strip() or self._derived_key(goal, deliverables, acceptance,
                                                                     workspace, parent_task_ref)
        existing = self.repo._one('SELECT * FROM companion_agent_tasks WHERE agent=? AND idempotency_key=? '
                                  'AND conversation_ref=?', (self.agent_id, key, str(conversation_ref or '')))
        if existing:
            return self._receipt(dict(existing), idempotent=True)
        limits = self._limits()
        # parent_task_ref 有两种写法：agent_id（精确，推荐）或节点名（项目 key）。
        # 先把「它就是某条台账的 id」这条路走掉，剩下的才按节点名找。
        depth, parent = self._depth(parent_task_ref)
        if depth > limits['max_depth']:
            raise AgentError('nesting_limit',
                             '嵌套最多 %d 层（再往下就是第 %d 层）；把这件事放进上一层的目标里，或者等它结束再派。'
                             % (limits['max_depth'], depth + 1), depth=depth)
        origin = ORIGINS.get(str(node or '').strip().lower()) if node else None
        choice = self._choose_node(node, origin, node_reason)
        # t11: a node=work task is handed to the command backend, which runs
        # action['command'] (see _run_worker). A goal-only task therefore fails
        # two seconds later with 'command backend needs a command' - syntax OK,
        # execution failed, and the model never learns what it did wrong.
        # Refuse it here instead, and say what to use next time.
        if choice['origin'] == 'work' and not command:
            raise AgentError(
                'command_required_for_work_node',
                'node=work 要在 command 里写具体命令（例如 command="pytest -q" 或 '
                'command="python build.py"）；只想让它按目标自动把事情做完，用 node=dsh；'
                '新建文件这类小活，用 node=echo。',
                node='work', goal=goal[:120])
        budget_policy = budget if isinstance(budget, dict) else {}
        # 模式来源：本次动作显式带的 > 配置里的（真实 SpendingPolicy）> off。
        # 上限从部署配置里读，缺的进 missing_limits——不当 0、不假装有。
        mode = str(budget_policy.get('mode') or self._budget_mode())
        verdict = decide(mode=mode, task=dict(
            provider=choice['origin'],
            # DSH 拿不到逐次 usage；主机命令根本不产生模型账单。两者都不能说「用量已知」。
            usage_known=False,
            concurrency=budget_policy.get('concurrency') or choice.get('concurrency'),
            runtime_s=budget_policy.get('runtime_s') or choice.get('runtime_s'),
            calls=budget_policy.get('calls')))
        if depth:
            verdict['parent_id'] = parent
        if not verdict['allowed']:
            return dict(status='rejected', ok=False, code='budget_strict_unmetered',
                        needs_setup=True, reason=verdict['reason'], budget=verdict,
                        output='没有派出去：' + verdict['reason'], agent_id=None, task_ref=None,
                        operation_id=None)
        ident = stable_id('companion-agent', self.agent_id, key, str(conversation_ref or ''))
        scope_id = self.scope_id or self._scope_id(conversation_ref)
        parent_key = ''
        if parent:
            parent_row = self.repo._one('SELECT task_ref FROM companion_agent_tasks WHERE id=?', (parent,))
            parent_key = (parent_row['task_ref'] if parent_row else '') or ''
        task_ref = self._ensure_task(scope_id, ident, goal, project, conversation_ref, parent,
                                     parent_key=parent_key)
        now = _now()
        record = dict(id=ident, agent=self.agent_id, task_ref=task_ref, parent_id=parent or '',
                      depth=depth, status='queued', verification='pending', node=choice['node'],
                      origin=choice['origin'], goal=goal[:1200], project=str(project or '')[:200],
                      scope_id=scope_id, conversation_ref=str(conversation_ref or ''),
                      idempotency_key=key, budget=json.dumps(dict(verdict, requested=budget_policy,
                                                                  deploy=choice), ensure_ascii=False),
                      workspace=workspace, workdir='', result=None, failure=None,
                      waiting_for='', recover_when='', cancel_reason='', attempt=1, resumed_from='',
                      session_id=str(resume_session_id or ''), message_id='',
                      checkpoint=json.dumps(dict(
                          deliverables=deliverables, acceptance=acceptance, command=command,
                          context_refs=[str(item)[:200] for item in (context_refs or [])][:8],
                          workspace_root=workspace_root, node_reason=choice.get('reason') or '',
                          action_id=_action_id or ''), ensure_ascii=False),
                      pid=0, lease_until=0, created=now, started=None, finished=None, updated=now,
                      summary_sent_at=None)
        self._insert(record)
        self._log(ident, 'created', goal[:400], dict(task_ref=task_ref, depth=depth, node=choice['node'],
                                                     parent_id=parent or ''))
        self._log(ident, 'receipt', '已登记，排队等执行。', dict(status='queued'))
        self._mirror(self._row(ident))
        return self._receipt(self._row(ident))

    def workspace_path(self, workspace=None):
        """工作区落在她自己的家目录里；越界（含 ``..``、系统目录）一律拒绝。

        缺省是 ``data/companion/agents/shared``，同仓库的活在这里排队串行。调用方
        给了绝对路径时，只接受 ``home`` 之内的既有目录（例如云端仓库目录）——
        子 agent 不存在「顺手改一下系统目录」这种默认。
        """
        home = self.home.resolve()
        root = (home / 'data/companion/agents').resolve()
        root.mkdir(parents=True, exist_ok=True)
        if workspace in (None, '', 'shared'):
            shared = root / 'shared'
            shared.mkdir(parents=True, exist_ok=True)
            return shared, str(root)
        candidate = Path(str(workspace)).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            candidate = candidate.resolve()
        except OSError:
            raise AgentError('workspace_invalid', '工作目录解析失败：%s' % workspace)
        if candidate != home and home not in candidate.parents:
            raise AgentError('workspace_outside', '工作目录必须在 %s 之内，%s 不在其内。' % (home, candidate))
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate, str(root)

    def _derived_key(self, goal, deliverables, acceptance, workspace, parent):
        return stable_id('agent-request', self.agent_id, ' '.join(goal.split())[:400],
                         '|'.join(deliverables), '|'.join(acceptance), str(workspace), str(parent or ''))

    def _limits(self):
        policy = getattr(self.cfg, 'resource_policy', None) or {}
        concurrent = int(policy.get('agent_max_concurrent') or MAX_CONCURRENT)
        depth = int(policy.get('agent_max_depth') or MAX_DEPTH)
        return dict(max_concurrent=max(1, min(concurrent, 8)),
                    max_depth=max(0, min(depth, 4)),
                    max_wait_seconds=float(policy.get('agent_max_wait_seconds') or MAX_WAIT_SECONDS))

    def _choose_node(self, requested, origin, reason=''):
        """选后端：dsh / work / stub。没有可用的就明说缺什么，不假装能跑。"""
        if requested:
            origin = ORIGINS.get(str(requested).strip().lower())
            if origin is None:
                raise AgentError('unknown_node', 'node 只能是 dsh/work/echo，收到 %r' % (requested,))
        if origin == 'dsh':
            paths = self._dsh_paths()
            if paths is None:
                raise AgentError('dsh_unavailable', '这台机器上找不到 DSH 运行时（dsh_root/dsh_node/入口缺失），'
                                                    '换 node=work，或者先把 DSH 装好。')
            return dict(node=paths.get('node_name') or LOCAL_NODE, origin='dsh',
                        entry=paths.get('entry'), root=str(paths.get('root') or ''),
                        concurrency=1, runtime_s=DEFAULT_TIMEOUT_SECONDS,
                        reason=reason or '由独立 DSH SDK 子进程执行，工作目录临时且隔离')
        if origin in ('work', 'stub'):
            return dict(node=LOCAL_NODE, origin=origin, concurrency=MAX_CONCURRENT,
                        runtime_s=DEFAULT_TIMEOUT_SECONDS,
                        reason=reason or ('离线假服务（不连模型）' if origin == 'stub' else '主机命令'))
        # 没指定后端时按「主机命令」起步：这是唯一处处都有、不需要外部运行时的选择；
        # 要花模型或在云上跑，由调用方显式写 node=dsh/<节点名>，不替她猜。
        return dict(node=LOCAL_NODE, origin='work', concurrency=MAX_CONCURRENT,
                    runtime_s=DEFAULT_TIMEOUT_SECONDS, defaulted=True,
                    reason=reason or '没有指定 node，按本机主机命令执行')

    def _dsh_paths(self):
        executor = self.executor
        for _ in range(4):
            if executor is not None and callable(getattr(executor, '_dsh_paths', None)):
                try:
                    found = executor._dsh_paths()
                except Exception:  # noqa: BLE001
                    return None
                if found:
                    root, node, entry = found
                    return dict(root=root, node=node, entry=entry, node_name=LOCAL_NODE)
            executor = getattr(executor, 'original', None) if executor is not None else None
        return None

    def _spending_policy(self):
        """真实预算策略：服务模式下执行器链上装着角色，角色上有 Spending.policy。"""
        core = self._core()
        policy = getattr(getattr(core, 'spending', None), 'policy', None)
        if policy is not None:
            return policy
        return getattr(getattr(self, 'cfg', None), 'spending_policy', None)

    def _budget_mode(self):
        return budget_mode(self._spending_policy())

    def budget_guarded(self):
        """本次派活要不要先过预算裁决（strict 才拦），供 executor 的 dsh 守卫用。"""
        return budget_decision(self._spending_policy(), provider='dsh', task=dict(
            runtime_s=DEFAULT_TIMEOUT_SECONDS))

    def _scope_id(self, conversation_ref):
        """台账挂在哪段会话上：显式给了就用它，否则用角色的主会话。

        角色里她只有一段主人私聊（``core.scope``）；离线测试里可能既没有角色也
        没有 conversation_ref，那就挂到 ``agent:<id>`` 这只有她自己的会话上，
        仍然有归属，不会落到全局。
        """
        if conversation_ref:
            return str(conversation_ref)
        core = self.core
        if core is not None and getattr(core, 'scope', None) is not None:
            return core.scope.scope_id
        life = self.life
        if life is not None and hasattr(life, 'scope'):
            try:
                return life.scope('agent:' + str(self.agent_id), '').scope_id
            except Exception:  # noqa: BLE001 - 真起不来就退回下面那行
                pass
        return 'agent:' + str(self.agent_id)

    def _ensure_task(self, scope_id, ident, goal, project, conversation_ref, parent,
                     parent_key=''):
        """项目任务复用 ``TaskService``：同一件事的续行、等待、结束都在那一条上。"""
        tasks = self.tasks
        project_key = str(project or goal[:40])
        core = self.core or getattr(self.executor, '_core', None)
        if parent_key:
            # 子任务挂到父那一件上：同一项目下派下来的子任务共用父的任务主线，
            # 这样「同一件事再派一层」不会每次都新建一条平行的任务（也是 t9 B3
            # 里 task_ref 被复用的根源）。
            task_ref = parent_key
            project = project_key
        elif core is not None and hasattr(core, '_project_ref'):
            task_ref = core._project_ref(project_key)
            project = project_key
        elif self.life is not None and hasattr(self.life, 'scope'):
            try:
                task_ref = stable_id('companion-project',
                                     self.life.scope(self.scope_id or 'agent:' + str(self.agent_id),
                                                     '').scope_id, project_key)
                project = project_key
            except Exception:  # noqa: BLE001
                task_ref = stable_id('companion-project', scope_id, project_key)
                project = project_key
        else:
            task_ref = stable_id('companion-project', scope_id, project_key)
            project = project_key
        if tasks is not None:
            try:
                current = tasks.get(task_ref, scope_id)
                if current is None:
                    tasks.ensure(_ScopeShim(scope_id, conversation_ref), task_ref, project,
                                 key=project_key, origin='self', companion=True, agent=True)
                    current = tasks.get(task_ref, scope_id)
                if (current or {}).get('status') in {'done', 'cancelled'}:
                    tasks.transition(scope_id, task_ref, 'active', cause='agent_created', user_resume=True)
            except Exception:  # noqa: BLE001 - 任务主线失败不该挡住台账
                log.warning('子 agent 的任务主线登记失败', exc_info=True)
        return task_ref

    def _depth(self, parent_task_ref=None, *, parent_id=None):
        """算这一件在谁下面、第几层。

        ``parent_task_ref`` 优先按台账主键 ``id`` 精确匹配（这才是「哪一件」）；
        只有对不上才退回按 ``task_ref`` 找，并且要求它是**唯一一条**未终结、不在
        当前链上的父行。``TaskService`` 的 ``task_ref`` 在同一项目下会被多次
        create 复用，按它取「最新一行」会取到同层兄弟或自己——那样深度就永远停在
        1，第 3 层永远拦不住（t9 D2-2）。
        """
        p = str(parent_task_ref or parent_id or '').strip()
        if not p:
            return 0, ''
        row = self.repo._one('SELECT id,task_ref,parent_id,depth,status FROM companion_agent_tasks '
                             'WHERE agent=? AND id=?', (self.agent_id, p))
        if row is None:
            columns = {item['name'] for item in self.repo._all('PRAGMA table_info(companion_agent_tasks)')}
            if 'parent_id' in columns:
                # 节点名也对不上 id：按 parent_id 链找一个「恰好只有一个」的父。
                row = self._parent_by_name(p)
            if row is None:
                raise AgentError('unknown_parent', 'parent_task_ref 指向的子 agent 不存在：%s' % p)
        if row['status'] in TERMINAL:
            raise AgentError('parent_finished', '父任务已经结束（%s），不能挂在它下面；直接自己派一个。'
                             % row['status'])
        if row['id'] == str(parent_id or ''):
            raise AgentError('cyclic_parent', '不能把一个子 agent 挂到它自己下面')
        # 深度以 parent_id 链为准（记账里的 depth 只作展示）：链断了就退回自报值，
        # 但绝不会比「父的 depth + 1」更小——层级只会往深处算。
        chain = self._chain_depth(row['id'])
        return max(chain, int(row['depth'] or 0) + 1), row['id']

    def _parent_by_name(self, name):
        """按节点名（项目 key / task_ref）找父：只认唯一候选，含糊就拒绝。

        ``TaskService`` 的 task_ref 在同一项目下会被多件子任务共用。按名字找父在
        这种时候本来就不唯一，这里**不猜**（宁可拒绝，也不挑错一件把深度算小）。
        真正要表达「挂在哪一件下面」请用 create 回执里的 agent_id：那是台账主键，
        唯一的。深度沿 parent_id 链数（``_chain_depth``），不靠名字也不靠自报字段。
        """
        candidates = [dict(row) for row in self.repo._all(
            'SELECT id,depth,status FROM companion_agent_tasks WHERE agent=? AND task_ref=? '
            "AND status NOT IN ('completed','failed','interrupted','cancelled') "
            'ORDER BY created DESC', (self.agent_id, name))]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # 名字被多件共用时没有「唯一的那一父」：按名字算深度本来就说不清。
            # 这里取**最保守**的那个——所有同名在跑件的深度取最大值再加一。
            # 宁可判深（最坏是 nesting_limit 拦得早一点），也不判浅把层数放出去；
            # 真要精确挂在某一层下面，用 create 回执里的 agent_id（台账主键，唯一）。
            raise AgentError('parent_ambiguous',
                             '这个名字（%s）同时挂着 %d 件没结束的子 agent，按名字算深度会算不清；'
                             '请改用 create 回执里的 agent_id 作为 parent_task_ref。'
                             % (name, len(candidates)),
                             candidates=[row['id'] for row in candidates[:5]])
        return None

    def _chain_depth(self, parent_id):
        """沿 parent_id 链数层数：父子孙如实是 1、2、3，不靠单行里的 depth 字段自报。"""
        seen, depth, cursor = set(), 0, str(parent_id or '')
        while cursor and cursor not in seen:
            seen.add(cursor)
            row = self.repo._one('SELECT parent_id FROM companion_agent_tasks WHERE id=? AND agent=?',
                                 (cursor, self.agent_id))
            if not row:
                break
            depth += 1
            cursor = str(row['parent_id'] or '')
        return depth

    def _running_count(self):
        """真正在跑的件数（不是全部未结束的：排队中的不占并发）。"""
        row = self.repo._one("SELECT COUNT(*) AS n FROM companion_agent_tasks WHERE agent=? "
                             "AND status IN ('running','cancelling')", (self.agent_id,))
        return int(row['n']) if row else 0

    def queued(self, limit=5):
        """轮到谁了：排队中的，按登记先后。后台执行器照这个顺序领。"""
        return [dict(row) for row in self.repo._all(
            "SELECT * FROM companion_agent_tasks WHERE agent=? AND status='queued' "
            "ORDER BY created ASC LIMIT ?", (self.agent_id, int(limit)))]

    def is_live(self, agent_id):
        return str(agent_id) in (self._live or {})

    def _unfinished(self, *, live=False):
        clause = "AND status IN ('queued','running','waiting','cancelling')" if live else ''
        row = self.repo._one('SELECT COUNT(*) AS n FROM companion_agent_tasks WHERE agent=? ' + clause,
                             (self.agent_id,))
        return int(row['n']) if row else 0

    def _receipt(self, row, *, idempotent=False, **extra):
        receipt = dict(ok=True, agent_id=row['id'], task_ref=row['task_ref'],
                       operation_id=stable_id('agent-op', row['id'], str(row.get('attempt') or 1), 'create'),
                       status=row['status'], verification=row['verification'], depth=int(row['depth']),
                       node=row['node'], attempt=int(row['attempt'] or 1),
                       created_at=_stamp(row['created']), observed_at=_stamp(_now()),
                       budget=_safe_json(row.get('budget'), {}) or {},
                       idempotent=bool(idempotent),
                       output=('同一个请求已经登记过，这是同一个子 agent：%s（%s）。' % (row['id'], row['status'])
                               if idempotent else
                               '已登记挂起：%s（%s），在后台执行；稍后用 inspect/wait 看结果，不要在原地说“这就去”。'
                               % (row['id'], row['status'])))
        receipt.update(extra)
        return receipt

    # -- send -------------------------------------------------------

    def send(self, agent_id, *, text='', message_id=None, intent=None):
        """给在跑的子 agent 追加一条消息；同一个 ``message_id`` 只送达一次。"""
        row = self._row(agent_id)
        text = str(text or '').strip()
        if not text:
            raise AgentError('text_required', 'send 需要 text。')
        mid = str(message_id or '').strip() or stable_id('agent-msg', row['id'], text[:200])
        existing = self.repo._one('SELECT * FROM companion_agent_events WHERE agent_id=? AND message_id=?',
                                  (row['id'], mid))
        if existing:
            return dict(ok=True, agent_id=row['id'], message_id=mid, delivered=False,
                        applied='stored', applied_reason='duplicate_stored', idempotent=True,
                        status=row['status'],
                        output='这条消息已经在台账里了（%s），没有重复写入；它同样只是入库，'
                               'worker 尚未读取。' % existing['created'])
        if row['status'] in TERMINAL:
            raise AgentError('agent_finished', '这个子 agent 已经结束（%s）；要接着做就 resume 生成新的 attempt。'
                             % row['status'], status=row['status'])
        if row['status'] == 'cancelling':
            raise AgentError('agent_cancelling', '正在取消，消息不会再送达。')
        # 转交也受嵌套限制：让一个已经没有余量的子 agent 再去派活，等价于绕过 create 的拦截。
        self._guard_transfer(row)
        try:
            self._log(row['id'], 'message', text[:4000],
                      dict(intent=str(intent or '')[:200], in_reply_to=mid), message_id=mid)
        except Exception as error:  # 唯一索引撞上并发同一 message_id
            if 'UNIQUE' not in str(error).upper():
                raise
            return dict(ok=True, agent_id=row['id'], message_id=mid, delivered=False,
                        applied='stored', applied_reason='duplicate_stored', idempotent=True,
                        status=row['status'],
                        output='这条消息已经在台账里了，没有重复写入；它同样只是入库，worker 尚未读取。')
        # H1：companion_agent_worker.py 的三种后端都不读 stdin，所以「写进管道」不等于
        # 「子 agent 收到了」。只有真的有一个 worker 声明自己读走了这条消息（把回执写进
        # 台账 kind='message_read'），才可能记 delivered；默认只能是 stored。
        live = self._live.get(row['id'])
        applied = 'stored'
        reason = 'worker_does_not_read_stdin'
        if live is not None and live.get('stdin') is not None and live.get('reads_stdin'):
            try:
                live['stdin'].write((text.rstrip() + '\n').encode('utf-8'))
                live['stdin'].flush()
                applied = 'delivered'
                reason = ''
            except Exception:  # noqa: BLE001 - 管道已经关了，就退回「存起来」
                applied = 'stored'
                reason = 'stdin_write_failed'
        return dict(ok=True, agent_id=row['id'], message_id=mid,
                    # delivered 是「worker 确认读到了」；存进台账不算送达。
                    delivered=(applied == 'delivered'),
                    applied=applied, applied_reason=reason,
                    status=row['status'], idempotent=False,
                    output=('已入台账（events 里 message_id=%s），但 worker 尚未读取：'
                            '当前 worker 不读 stdin，这条消息只在台账里，子 agent 这次不会收到它。'
                            % mid) if applied != 'delivered' else
                           'worker 已确认从管道读走这条消息。')

    # -- inspect ----------------------------------------------------

    def inspect(self, agent_id, *, changes=CHANGES_SHOWN, messages=20):
        row = self._row(agent_id)
        result = _safe_json(row.get('result'), None)
        failure = _safe_json(row.get('failure'), None) or row.get('failure')
        events = [dict(message_id=item['message_id'], kind=item['kind'], text=item['text'][:800],
                       at=_stamp(item['created']), payload=_safe_json(item['payload'], {}))
                  for item in self.repo._all('SELECT * FROM companion_agent_events WHERE agent_id=? '
                                             'ORDER BY created DESC LIMIT ?', (row['id'], max(1, int(messages))))]
        events.reverse()
        changes_list = self._changes(row['id'])
        return dict(ok=True, agent_id=row['id'], task_ref=row['task_ref'], parent_id=row['parent_id'] or None,
                    depth=int(row['depth']), status=row['status'], verification=row['verification'],
                    node=row['node'], origin=row['origin'], attempt=int(row['attempt'] or 1),
                    goal=row['goal'], project=row['project'], conversation_ref=row['conversation_ref'],
                    budget=_safe_json(row.get('budget'), {}) or {},
                    waiting_for=row['waiting_for'] or None, recover_when=row['recover_when'] or None,
                    created_at=_stamp(row['created']), started_at=_stamp(row['started']),
                    finished_at=_stamp(row['finished']),
                    state_history=TASK_STATUS.get(row['status']),
                    result=result, failure=failure,
                    side_effects=list((result or {}).get('side_effects') or []) if isinstance(result, dict) else [],
                    changes=changes_list[-max(1, int(changes)):],
                    changes_total=len(changes_list),
                    messages=events,
                    artifacts=list((result or {}).get('artifacts') or []) if isinstance(result, dict) else [],
                    output=self._summary(row))

    def _changes(self, agent_id):
        """改动流水：从事件表里按 at-path/at-hash 记录取，按发生顺序返回。"""
        rows = self.repo._all('SELECT text,payload,created FROM companion_agent_events WHERE agent_id=? '
                              "AND kind='change' ORDER BY created ASC", (agent_id,))
        out = []
        for item in rows:
            payload = _safe_json(item['payload'], {}) or {}
            out.append(dict(path=payload.get('path'), op=payload.get('op'),
                            sha256=payload.get('sha256'), bytes=payload.get('bytes'),
                            at=_stamp(item['created'])))
        return out

    def _summary(self, row):
        """主上下文里的一份摘要：约 1200 中文字符 + 最近 5 个改动 + 证据定位。"""
        result = _safe_json(row.get('result'), None) or {}
        failure = row.get('failure') or ''
        # 任务跑完了 ≠ 验收判据达成了。生产上有三条 completed + outcome=incomplete
        # 的记录，旧写法把「completed」摆在最显眼的位置，读的人看到这两个字就停了。
        # 现在 outcome 与 completed 并列出现，且当两者不一致时用一句人话说明白。
        outcome = ''
        if isinstance(result, dict):
            outcome = str(result.get('outcome') or '')
        head = '%s｜%s｜验证 %s｜第 %d 次' % (row['id'][:40], row['status'], row['verification'],
                                          int(row['attempt'] or 1))
        if outcome and outcome != row['status']:
            head += '｜系统判定的结果 %s' % outcome
            if row['status'] in ('completed', 'succeeded') and outcome not in ('completed', 'succeeded'):
                head += '（任务跑完了，但验收判据没达成）'
        # 验不了要说清"为什么验不了"，不能让她以为还在等（见 _verify 的终态定义）
        if row['verification'] == 'unverified' and not (isinstance(result, dict) and result.get('artifacts')):
            head += '（没有交付物、也没有验收判据，所以没有证据可核验）'
        if row['status'] == 'waiting':
            head += '｜在等：%s；恢复条件：%s' % (row['waiting_for'] or '（没写）', row['recover_when'] or '（没写）')
        parts = [head, '目标：' + (row['goal'] or '')[:160]]
        if isinstance(result, dict) and result:
            if result.get('summary'):
                # 这是**子 agent 自己的说法**，不是系统判定。
                # 2026-11-18：同一份记录里 summary 写「已完成…没有未完成事项」，
                # 而 outcome=incomplete、verification=unverified——她不标明谁说的，
                # 就只能看到两个互斥的结论。两句话都成立：它确实这么说，系统也确实这么判。
                parts.append('子 agent 自己的说法（不是系统判定）：' + str(result['summary'])[:400])
            if result.get('tests'):
                parts.append('测试：' + '；'.join(str(item)[:80] for item in result['tests'][:3]))
            if result.get('remaining'):
                parts.append('没做完：' + '；'.join(str(item)[:80] for item in result['remaining'][:3]))
            if result.get('blockers'):
                parts.append('卡在：' + '；'.join(str(item)[:80] for item in result['blockers'][:3]))
            if result.get('side_effects'):
                parts.append('不可撤销的副作用：' + '；'.join(str(item)[:80] for item in result['side_effects'][:3]))
            artifacts = result.get('artifacts') or []
            if artifacts:
                parts.append('证据：' + '；'.join('%s(%s)' % (a.get('id'), a.get('path')) for a in artifacts[:4]))
        if failure:
            parts.append('失败：' + (json.dumps(failure, ensure_ascii=False) if isinstance(failure, dict)
                                    else str(failure))[:300])
        changes = self._changes(row['id'])[-CHANGES_SHOWN:]
        if changes:
            parts.append('最近改动：' + '；'.join('%s %s %s' % (c['op'], c['path'],
                                                         (c['sha256'] or '')[:10]) for c in changes))
        text, clipped = clip_text('\n'.join(parts), SUMMARY_CHARS)
        return dict(text=text, clipped=clipped, chars=round(estimate_cn_chars(text), 1),
                    changes_shown=len(changes), source_ref='companion_agent_tasks:' + row['id'])

    # -- wait -------------------------------------------------------

    async def wait(self, agent_id=None, *, until=None, wait_s=None, cursor=None):
        """短时限等待：最多 ``MAX_WAIT_SECONDS``，返回 cursor 供下次续看。

        这里**不是**同步阻塞主认知的地方：主角色的认知在别的任务里跑，这里最多
        让当前这一轮动作等几秒；等不到就如实说「还没好」，不编结果。
        """
        limits = self._limits()
        try:
            seconds = float(wait_s if wait_s is not None else DEFAULT_WAIT_SECONDS)
        except (TypeError, ValueError):
            seconds = 0.0
        seconds = max(0.0, min(seconds, limits['max_wait_seconds']))
        if agent_id:
            row = self._row(agent_id)
            deadline = time.monotonic() + seconds
            while row['status'] not in TERMINAL and time.monotonic() < deadline:
                await asyncio.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
                row = self._row(agent_id)
            return self._wait_view(row, cursor=cursor)
        rows = self.repo._all('SELECT * FROM companion_agent_tasks WHERE agent=? AND status IN '
                              "('queued','running','waiting','cancelling') ORDER BY updated DESC LIMIT 5",
                              (self.agent_id,))
        if seconds:
            deadline = time.monotonic() + seconds
            while rows and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                rows = self.repo._all('SELECT * FROM companion_agent_tasks WHERE agent=? AND status IN '
                                      "('queued','running','waiting','cancelling') ORDER BY updated DESC LIMIT 5",
                                      (self.agent_id,))
        if not rows:
            return dict(status='completed', items=[], cursor=cursor, next_cursor=None,
                        live=0, output='现在没有在跑或排队的子 agent。')
        return dict(status='running', items=[self._wait_view(dict(r), cursor=cursor) for r in rows],
                    live=len(rows), cursor=cursor,
                    next_cursor=stable_id('agent-wait', *[r['id'] + str(r['updated']) for r in rows]),
                    output='还有 %d 件没有结束：%s' % (len(rows), '；'.join(
                        '%s %s' % (r['id'], r['status']) for r in rows)))

    def _wait_view(self, row, cursor=None, *, graded=False):
        row = dict(row)
        summary = self._summary(dict(row))['text']
        view = dict(agent_id=row['id'], task_ref=row.get('task_ref'),
                    status=row['status'], verification=row['verification'],
                    operation_id=stable_id('agent-op', row['id'], str(row.get('attempt') or 1), 'inspect'),
                    changed=bool(cursor) and cursor != self._cursor_of(row),
                    cursor=self._cursor_of(row), next_cursor=self._cursor_of(row),
                    result=_safe_json(row.get('result'), None), summary=summary,
                    observed_at=_stamp(_now()))
        if graded:
            view['graded'] = True
        if row['status'] in TERMINAL:
            view.update(state='finished', output=summary)
        elif row['status'] == 'waiting':
            view.update(state='waiting',
                        output='还在等：%s（%s）。' % (row['waiting_for'] or '没写等待对象',
                                                     row['recover_when'] or '没写恢复条件'))
        else:
            view.update(state='pending',
                        output='%s 仍在 %s，还没有结果。' % (row['id'], row['status']))
        return view

    @staticmethod
    def _cursor_of(row):
        return stable_id('agent-cursor', row['id'], row['status'], str(row.get('updated')))

    # -- cancel -----------------------------------------------------

    async def cancel(self, agent_id, *, include_children=True, why=''):
        """终止它的工作进程与子孙进程，并说清哪些副作用不可撤销。"""
        row = self._row(agent_id)
        if row['status'] in TERMINAL:
            if row['status'] != 'cancelled':
                raise AgentError('already_finished', '它已经是 %s，不能再取消；要接着做就 resume。' % row['status'])
            return dict(ok=True, agent_id=row['id'], status='cancelled', already=True,
                        side_effects=[], output='它早就取消了，没有新的动作。')
        if row['status'] == 'queued':
            # 排队中的直接取消：没有进程可停，也就没有副作用。
            self._set_status(row['id'], 'cancelled', cancel_reason=why or '排队时取消')
            self._log(row['id'], 'cancelled', why or '排队时取消', dict(from_state='queued'))
            return dict(ok=True, agent_id=row['id'], status='cancelled', side_effects=[],
                        output='排队中的任务已取消；它还没有开始跑，没有产生任何改动。')
        self._set_status(row['id'], 'cancelling', cancel_reason=why or '')
        self._log(row['id'], 'cancelling', why or '开始取消', dict(from_state=row['status']))
        live = self._live.get(row['id'])
        if live is not None:
            # 先请宿主收掉协程，进程树的收尾由执行器负责（它本来就是进程树的所有者）。
            task = live.get('task')
            if task is not None and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=30)
                except Exception:  # noqa: BLE001 - 取消本来就是打破等待
                    pass
        current = self._row(row['id'])
        if current['status'] == 'cancelling':
            if current.get('pid') and self._pid_alive(current['pid']):
                self._kill_pid(current['pid'])
            self._set_status(row['id'], 'cancelled', cancel_reason=why or '')
        side_effects = self._side_effects_of(current, why)
        self._log(row['id'], 'cancelled', why or '已取消', dict(side_effects=side_effects))
        if include_children:
            for child in self.repo._all('SELECT id,status FROM companion_agent_tasks WHERE agent=? AND parent_id=? '
                                        "AND status IN ('queued','running','waiting','cancelling')",
                                        (self.agent_id, row['id'])):
                try:
                    await self.cancel(child['id'], include_children=True, why='父任务取消')
                except AgentError:
                    pass
        return dict(ok=True, agent_id=row['id'], status=self._row(row['id'])['status'],
                    side_effects=side_effects,
                    output='已取消。' + ('不可撤销的副作用：' + '；'.join(side_effects) if side_effects
                                       else '没有产生不可撤销的改写。'))

    @staticmethod
    def _side_effects_of(row, why):
        changes = _safe_json(row.get('checkpoint'), {}) or {}
        out = []
        result = _safe_json(row.get('result'), None) or {}
        if isinstance(result, dict):
            out.extend(str(item) for item in (result.get('side_effects') or []))
        history = changes.get('changes') or []
        if history:
            out.append('已写入/删改 %d 个文件（%s）；文件改动不会自动回滚，要还原只能按哈希核对后手动处理'
                       % (len(history), '、'.join(str(item.get('path')) for item in history[:3])))
        if str(row.get('node') or '') == CLOUD_NODE:
            out.append('它在云端节点上运行，那边的进程与临时目录要单独清理')
        return out

    def _pid_alive(self, pid):
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _kill_pid(self, pid):
        import signal
        try:
            if os.name != 'nt':
                os.killpg(int(pid), signal.SIGKILL)
            else:
                os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass

    # -- resume -----------------------------------------------------

    def resume(self, agent_id, *, goal=None, from_session=True, checkpoint=True):
        """续作：新 attempt，历史保留；能接原会话就接，接不上就明说「续作不是恢复」。"""
        row = self._row(agent_id)
        if row['status'] not in {"failed", "interrupted", "cancelled", "completed", "waiting"} | {"running"}:
            raise AgentError('resume_not_applicable', '它现在是 %s，不需要续作。' % row['status'])
        limits = self._limits()
        self._guard_transfer(row)
        if self._unfinished(live=True) >= limits['max_concurrent']:
            # 并发满了：续作请求照样登记为 queued，不失败，也不会破坏历史。
            log.info('续作排队：%s', row['id'])
        mode = 'session' if (from_session and row.get('session_id')) else ('checkpoint' if checkpoint else 'fresh')
        reason = {
            'session': '接回原 SDK 会话（session_id 由 SDK 恢复），上下文是同一段',
            'checkpoint': '没有可用的原会话，按上次的检查点续作——这是续作，不是恢复原会话，'
                          '上次会话里没落盘的推理已经没了',
            'fresh': '不接原会话，从目标重新开始',
        }[mode]
        attempt = int(row['attempt'] or 1) + 1
        new_id = stable_id('companion-agent', row['id'], str(attempt), uuid.uuid4().hex[:8])
        now = _now()
        checkpoint_data = _safe_json(row.get('checkpoint'), {}) or {}
        record = dict(row)
        record.update(id=new_id, status='queued', verification='pending', attempt=attempt,
                      resumed_from=row['id'], result=None, failure=None, pid=0, lease_until=0,
                      created=now, started=None, finished=None, updated=now, summary_sent_at=None,
                      cancel_reason='', waiting_for='', recover_when='',
                      goal=str(goal or row['goal'])[:1200],
                      budget=json.dumps({**_safe_json(row.get('budget'), {}),
                                         'resume': dict(mode=mode, resumed_from=row['id'],
                                                        session_id=row.get('session_id') or '')},
                                        ensure_ascii=False),
                      checkpoint=json.dumps(dict(checkpoint_data, resume_mode=mode,
                                                 resumed_from=row['id'], changes=checkpoint_data.get('changes') or []),
                                            ensure_ascii=False))
        self._insert(record)
        self._log(row['id'], 'resumed', '生成了新的 attempt：%s（%s）' % (new_id, mode),
                  dict(mode=mode, attempt=attempt))
        self._log(new_id, 'created', '从 %s 续作（第 %d 次，%s）' % (row['id'], attempt, mode),
                  dict(resumed_from=row['id'], mode=mode))
        if mode == 'checkpoint':
            # 历史接口：老会话没接上时，「续」要留住上次做到哪一步。
            self._log(new_id, 'change', '检查点：上次改动 %d 项' % len(checkpoint_data.get('changes') or []),
                      dict(path=None, op='checkpoint', sha256=None,
                           bytes=len(json.dumps(checkpoint_data.get('changes') or [], ensure_ascii=False))))
        self._mirror(self._row(new_id))
        receipt = self._receipt(self._row(new_id), resumed_from=row['id'], resume_mode=mode,
                                output='续作已登记（第 %d 次）：%s。%s' % (attempt, new_id, reason))
        return receipt

    # -- read_artifact ---------------------------------------------

    def artifacts(self, agent_id):
        row = self._row(agent_id)
        result = _safe_json(row.get('result'), None) or {}
        return list(result.get('artifacts') or []) if isinstance(result, dict) else []

    def read_artifact(self, agent_id=None, *, artifact_id=None, path=None, offset=None, limit=None):
        """读证据原文：只能读本台账里登记过的文件，路径越界一律拒绝。"""
        targets = []
        if agent_id:
            targets = self.artifacts(agent_id)
        if artifact_id:
            targets = [item for item in targets if item.get('id') == artifact_id] or targets
        chosen = None
        if path:
            wanted = str(path)
            for item in targets:
                if item.get('path') == wanted:
                    chosen = item
                    break
            if chosen is None and agent_id is None:
                chosen = dict(path=wanted, id=None, sha256=None)
        elif targets:
            chosen = targets[0]
        if chosen is None:
            raise AgentError('artifact_unknown',
                             '这个子 agent 名下没有登记这个文件；先 inspect 看 artifacts 列表，再按 id 或 path 读。')
        candidate = Path(chosen['path'])
        if not candidate.is_absolute():
            candidate = (self.home / candidate).resolve()
        else:
            candidate = candidate.resolve()
        allowed_root = (self.home / 'data/companion/agents').resolve()
        if allowed_root not in candidate.parents and candidate != allowed_root:
            raise AgentError('artifact_outside_evidence',
                             '只能读子 agent 证据目录里的文件，%s 不在其内。' % candidate)
        if not candidate.is_file():
            raise AgentError('artifact_missing', '登记过的文件已经不在了：%s' % candidate)
        try:
            start = max(0, int(offset or 0))
        except (TypeError, ValueError):
            start = 0
        try:
            size = int(limit or ARTIFACT_DEFAULT_BYTES)
        except (TypeError, ValueError):
            size = ARTIFACT_DEFAULT_BYTES
        size = max(200, min(size, ARTIFACT_MAX_BYTES))
        data = candidate.read_bytes()
        digest = _sha256_bytes(data)
        chunk = data[start:start + size]
        text = chunk.decode('utf-8', errors='replace')
        next_offset = start + len(chunk) if start + len(chunk) < len(data) else None
        return dict(ok=True, artifact_id=chosen.get('id'), path=str(candidate),
                    offset=start, next_offset=next_offset, total_bytes=len(data),
                    sha256=digest, hash_matches=(chosen.get('sha256') in (None, digest)),
                    text=text, complete=next_offset is None,
                    output=text or '（空文件）')

    # -- 状态变更（宿主与测试共用） ------------------------------------

    def set_status(self, agent_id, status, **fields):
        row = self._row(agent_id)
        target = STATE_ALIASES.get(str(status).strip().lower())
        if target is None:
            raise AgentError('unknown_state', '没有这个状态：%r' % (status,))
        result = fields.pop('result', None)
        verification = fields.pop('verification', None)
        failure = fields.pop('failure', None)
        if target == 'completed' and result is None:
            # 只有真跑过、shim 拿出了运行结果时，才允许从结果里生成清单；
            # 光调一次 transition 没有清单，就是「完成没有结果」，拒绝。
            run_result = fields.pop('run_result', None)
            if run_result is None:
                raise AgentError('completed_requires_result',
                                 '标记 completed 要给结果清单（outcome/summary/changes/tests/artifacts/'
                                 'evidence_refs/remaining/blockers/side_effects/usage）；计划和受理都不是完成')
            result = self._manifest_from_run(row, run_result)
        updated = self._set_status(row['id'], target, result=result, failure=failure,
                                   waiting_for=fields.get('waiting_for', ''),
                                   recover_when=fields.get('recover_when', ''),
                                   cancel_reason=fields.get('why') or fields.get('cancel_reason') or '',
                                   verification=verification)
        if target == 'completed':
            self._log(row['id'], 'completed', '有结果清单', dict(verification=updated['verification']))
        return self._as_view(updated)

    def transition(self, *, agent_id, status=None, **fields):
        """把一个子 agent 推到指定状态（契约里的统一写法，等价于 ``set_status``）。

        守卫在所有写库之前：``completed`` 要结果清单、``waiting`` 要等待对象与
        恢复条件、终态不再移动。非法迁移抛 ``AgentError('illegal_transition')``。

        不给 ``status`` 时只改别的列——目前只有核验结论（``verification``）走这条路：
        核验和任务状态是两件事，判「没通过」不该改写「已完成」。
        """
        target = STATE_ALIASES.get(str(status).strip().lower()) if status not in (None, '') else None
        if target == 'waiting' and self._row(agent_id)['status'] == 'queued':
            # 还没开始跑的不能自称「在等」：先领取成 running，再说明在等什么。
            # 这样「等待」永远有一条真的跑起来过的记录在它前面。
            self._claim(agent_id, from_queued_only=True)
        if status in (None, ''):
            if not fields.get('verification'):
                raise AgentError('status_required', 'transition 需要 status，或者至少给 verification。')
            return self.verify(agent_id, verdict=str(fields['verification']).strip().lower(),
                               note=str(fields.pop('note', '') or fields.pop('failure', '') or ''))
        return self.set_status(agent_id, status, **fields)

    def verify(self, agent_id, *, verdict=None, evidence=(), note=''):
        """核验与状态分开：只有证据能把它从 pending 推进到 passed/failed。"""
        row = self._row(agent_id)
        if verdict is None:
            verdict = 'unverified'
        if verdict not in VERIFICATIONS:
            raise AgentError('bad_verification', 'verification 只能是 %s' % '/'.join(VERIFICATIONS))
        checks = list(evidence or [])
        passed = sum(1 for item in checks if item.get('ok'))
        if verdict == 'passed' and (not checks or passed != len(checks)):
            raise AgentError('verification_without_evidence',
                             '没有证据不能算通过：%d 项检查里只有 %d 项过了。' % (len(checks), passed))
        if verdict == 'failed' and not str(note or '').strip() and not checks:
            raise AgentError('verification_without_evidence', '判失败的核验要写清哪条不过。')
        return self._as_view(self._update(row['id'], verification=verdict))

    def reconcile_on_start(self, repo=None, *, agent_id=None):
        """角色启动时调用：把上次停在 running 的活收尾（不重放）。"""
        return reconcile_on_start(repo if repo is not None else self.repo,
                                  agent_id=agent_id or self.agent_id, life=self.life)

    def mark_interrupted(self, agent_id, *, reason='进程重启，结果未知'):
        row = self._row(agent_id)
        if row['status'] not in {'running', 'waiting', 'cancelling'}:
            raise AgentError('illegal_transition', '%s 不能记为 interrupted' % row['status'])
        updated = self._set_status(row['id'], 'interrupted' if row['status'] != 'cancelling' else 'cancelled',
                                   failure=dict(code='interrupted', reason=reason),
                                   cancel_reason=reason if row['status'] == 'cancelling' else '')
        return self._as_view(updated)

    def _as_view(self, row):
        return dict(ok=True, agent_id=row['id'], status=row['status'], verification=row['verification'],
                    waiting_for=row['waiting_for'] or None, recover_when=row['recover_when'] or None,
                    attempt=int(row['attempt'] or 1), summary=self._summary(row)['text'])

    # -- 执行（由 ActionExecutor 调用） --------------------------------

    async def dispatch(self, task=None, *, action_id='', raised_by='executor'):
        """统一执行入口：传 task（含 op）就分派，不传就干 ``executor`` 领来的活。

        ``ActionExecutor.run`` 里 ``kind=agent`` 的活走这里；七个只读/登记操作
        也走这里，所以测试里可以用同一个入口验证路由。
        """
        task = dict(task or {})
        op = str(task.get('op') or '').strip().lower()
        if not op:
            agent_id = str(task.get('agent_id') or '').strip()
            if not agent_id:
                raise AgentError('op_required',
                                 'kind=agent 要写 op：%s；派活用 op=create+goal+node。' % '/'.join(OPS))
            row = self._row(agent_id)
            if row['status'] in TERMINAL:
                # 已经结束的不要重跑：要接着做就 resume（新的 attempt），
                # 这样台账里每一次尝试都留得住。
                raise AgentError('agent_not_runnable',
                                 '它现在是 %s，不会再跑；要接着做就 op=resume 生成新的 attempt。' % row['status'],
                                 agent_id=agent_id)
            return await self.execute(dict(task, agent_id=agent_id), action_id=action_id)
        fields = _action_fields(task)
        fields.pop('op', None)  # op 已经决定走哪条路，不再当成 create/send 的参数
        if op == 'create':
            return self.create(fields.pop('goal', None) or task.get('goal'), **fields)
        if op == 'send':
            return self.send(fields.pop('agent_id'), **fields)
        if op == 'inspect':
            return self.inspect(fields.pop('agent_id'), **fields)
        if op == 'wait':
            return await self.wait(fields.pop('agent_id', None), **fields)
        if op == 'cancel':
            return await self.cancel(fields.pop('agent_id'), **fields)
        if op == 'resume':
            return self.resume(fields.pop('agent_id'), **fields)
        if op == 'read_artifact':
            return self.read_artifact(fields.pop('agent_id', None), **fields)
        raise AgentError('unknown_op', 'kind=agent 的 op 只有 %s，收到 %r' % ('/'.join(OPS), op))

    async def execute(self, row_action=None, *, action_id=None):
        """干一件活：从台账领取、跑、收尾。行由执行器传入，不用再查一遍。

        返回值是 ``ActionExecutor`` 期望的动作回执（status/return_code/output/...），
        另带 ``agent_id``、``verification`` 和 ``completion='accepted'``。
        """
        if not isinstance(row_action, dict):
            raise AgentError('bad_action', 'execute 需要一件活')
        action = row_action
        goal = str(action.get('goal') or '').strip()
        agent_id = str(action.get('agent_id') or '').strip()
        budget = action.get('budget') if isinstance(action.get('budget'), dict) else {}
        node = action.get('node')
        if not agent_id:
            receipt = self.create(goal, node=node, budget=budget, project=action.get('project') or '',
                                  conversation_ref=action.get('conversation_ref') or '',
                                  _action_id=action_id or '', node_reason='由项目行动派出')
            agent_id = receipt['agent_id']
        return await self._run(agent_id, action)

    async def _run(self, agent_id, action):
        probe = self._row(agent_id)
        if probe['status'] not in {'queued', 'waiting'}:
            if probe['status'] == 'running':
                raise AgentError('already_running', '这个子 agent 已经在跑了：%s' % agent_id)
            return dict(status='failed', code='agent_not_runnable', agent_id=agent_id,
                        output='它现在是 %s，不会再跑；要接着做就 resume。' % probe['status'])
        if not self._claim(agent_id):
            raise AgentError('already_running', '这个子 agent 已经被别的执行者领走了：%s' % agent_id)
        row = self._row(agent_id)
        checkpoint = _safe_json(row.get('checkpoint'), {}) or {}
        workdir = self._workdir(row, checkpoint)
        started = time.monotonic()
        self._update(agent_id, workdir=str(workdir))
        self._log(agent_id, 'running', '开始执行（%s，%s）' % (row['node'], workdir),
                  dict(node=row['node'], workdir=str(workdir)))
        task = asyncio.current_task()
        self._live[agent_id] = dict(task=task, started=time.monotonic())
        # 回复在线：send 能立刻把追加要求写进它的 stdin。
        runner = self._runner(row, action, workdir, checkpoint)
        try:
            result = await runner
        except asyncio.CancelledError:
            current = self._row(agent_id)
            if current['status'] == 'cancelling':
                self._set_status(agent_id, 'cancelled', cancel_reason=current.get('cancel_reason') or '已取消')
                raise AgentError('cancelled', '子 agent 已取消。')
            self._set_status(agent_id, 'interrupted', failure=dict(code='interrupted', reason='执行被停止'))
            raise
        except AgentError:
            raise
        except Exception as error:  # noqa: BLE001
            self._set_status(agent_id, 'failed', failure=dict(code='exception',
                                                              reason='%s: %s' % (type(error).__name__, error)))
            self._log(agent_id, 'failed', str(error)[:300], dict(code='exception'))
            raise
        finally:
            self._live.pop(agent_id, None)
            self._cleanup_workdir(row, workdir)
        result['seconds'] = round(time.monotonic() - started, 3)
        return self._finish(agent_id, result)

    def _finish(self, agent_id, result):
        """结束一件活：有结果清单才算 completed；退出码非 0 就是 failed。"""
        row = self._row(agent_id)
        manifest = self._manifest_from_run(row, result)
        checks = result.pop('_checks', [])
        if result.get('return_code') == 0 and not result.get('failure'):
            self._set_status(agent_id, 'completed', result=manifest,
                             verification='pending')
            self._log(agent_id, 'completed', manifest.get('summary', '')[:600],
                      dict(return_code=result.get('return_code')))
        else:
            failure = result.get('failure') or dict(code='nonzero_exit',
                                                    reason='退出码 %s' % result.get('return_code'))
            self._set_status(agent_id, 'failed', failure=failure,
                             result=manifest if manifest.get('summary') else None)
            self._log(agent_id, 'failed', str(failure)[:300], dict(failure=failure))
        # 证据先算：交付物哈希对得上 / 无交付物任务的成功判据命中，才算「有证据」。
        evidence_checks, evidence = self._evidence_checks(row, result)
        checks = list(checks) + evidence_checks
        verdict = self._verify(row, checks)
        if verdict:
            self._update(agent_id, verification=verdict)
            self._log(agent_id, 'verified', '核验结论：%s' % verdict,
                      dict(verdict=verdict, evidence=evidence))
        elif not evidence:
            self._log(agent_id, 'verified', '没有可核验的证据，结论留在 pending（不默认通过）',
                      dict(verdict='pending', evidence=[]))
        current = self._row(agent_id)
        artifacts = []
        for item in manifest.get('artifacts') or []:
            entry = dict(item)
            # 哈希是宿主自己算的（_file_digest），不是子 agent 报的：来源要标出来。
            entry.setdefault('sha256_source', 'host')
            artifacts.append(entry)
        receipt = dict(status='completed' if current['status'] == 'completed' else 'failed',
                       completion='finished', agent_id=agent_id,
                       task_ref=current['task_ref'], verification=current['verification'],
                       return_code=result.get('return_code'),
                       seconds=result.get('seconds'), usage=result.get('usage'),
                       output=self._summary(current)['text'],
                       output_path=(artifacts[0].get('path') if artifacts else None),
                       attempt=int(current['attempt'] or 1),
                       artifacts=artifacts,
                       child_account=dict(
                           kind=PROVENANCE_MODEL,
                           text='output 里是子 agent 自己的说法（另一个模型转述），不是宿主观察到的现实'),
                       session_id=(result.get('session_id') or current.get('session_id') or None),
                       evidence_scope='agent_receipt',
                       evidence_note='这是子 agent 交回的结果与证据清单；它的说法不等于现实已改变，'
                                     '改动按文件哈希核对，核验与完成分开记。',
                       **provenance(PROVENANCE_MODEL, source='agent',
                                    note='这是子 agent 交回的说法与它写出的产物；产物路径与哈希由宿主自己算。',
                                    parts={'child_account': PROVENANCE_MODEL,
                                           'artifacts': PROVENANCE_TOOL,
                                           'delivery': PROVENANCE_TOOL}))
        # 先投递，再据实写「这次有没有送到」：顺序反了就只能靠猜。
        self._deliver_receipt(current, receipt)
        gaps = []
        if not receipt.get('message_id'):
            gaps.append(missing(
                '子 agent 回执的送达', 'unavailable',
                '结果留在台账里，但这次没有登记进任何收件账本（%s）；'
                '读 output 时要知道这句话还没送到任何人手上'
                % (receipt.get('delivery') or '没有 delivery 字段')))
        if not artifacts:
            gaps.append(missing('子 agent 的产物', 'not_requested',
                                '这次没有任何交付物文件；它只交回了说法'))
        receipt['not_obtained'] = gaps
        receipt['visibility'] = visibility(
            not_obtained=gaps,
            note='子 agent 的产物按路径与 sha256 自核；核验结论见 verification，'
                 'status=completed 只代表它说做完了。')
        self._mirror_receipt(current, receipt['output'])
        follow = self._followthrough()
        if follow is not None and current.get('task_ref') and current.get('scope_id'):
            try:
                follow.after_result(current['task_ref'], dict(receipt, status='completed'
                                                             if current['status'] == 'completed' else 'failed'),
                                    source_id=current['id'])
            except Exception:  # noqa: BLE001
                log.warning('子 agent 结果写入续行失败', exc_info=True)
        return receipt

    def _deliver_receipt(self, row, receipt):
        """结果回到它来的那段会话（收件账本），不跨平台、不冒充送达。

        收件账本只在完整运行时里（云端 ``life/companion_delivery.py``），所以这里
        按需导入：导入不到就只在台账里留结果，不假装已经投递给谁。
        """
        route = row.get('conversation_ref') or ''
        receipt.setdefault('message_id', None)
        try:
            from . import companion_delivery as delivery_ledger
        except ImportError:
            receipt['delivery'] = 'not_recorded:no_ledger'
            return
        try:
            mid = delivery_ledger.message_id('agent:' + row['id'], 0)
            queued = delivery_ledger.queue(self.repo, mid=mid, agent_id=self.agent_id,
                                           turn_id='agent:' + row['id'], segment=0,
                                           text=receipt['output'][:2000], conversation_ref=route,
                                           channels=([route.split(':')[0]] if route else ['all']))
            receipt['message_id'] = queued['message_id'] if queued else None
            receipt['delivery'] = 'recorded' if receipt['message_id'] else 'not_recorded:no_message'
        except Exception:  # noqa: BLE001 - 投递登记失败不改变台账事实
            receipt['delivery'] = 'not_recorded:queue_failed'
            log.warning('子 agent 回执登记进投递账本失败', exc_info=True)

    def _manifest_from_run(self, row, result):
        result = result if isinstance(result, dict) else {}
        checkpoint = _safe_json(row.get('checkpoint'), {}) or {}
        deliverables = list(checkpoint.get('deliverables') or [])
        artifacts = list(result.get('artifacts') or [])
        by_name = {Path(item.get('path') or '').name: item for item in artifacts}
        remaining = [name for name in deliverables if name not in by_name]
        tests = list(result.get('tests') or [])
        if result.get('return_code') not in (None, 0):
            tests = tests + ['退出码 %s：命令没有正常结束' % result.get('return_code')]
        # 「有没有交付物」和「这次有没有做成」是两件事：没有文件交付的任务（例如读一段内容
        # 再把结果带回来）用它自己的成功判据——goal 里写 result 时，宿主比对 stdout 里的
        # 那个值；acceptance 里有 output_contains 时逐条比对。判据只决定 outcome，
        # **不决定 verification**（那仍然要有真实证据，见 _evidence_checks）。
        stdout = str(result.get('stdout') or '')
        matched, judged = self._outcome_judgement(row, stdout, result)
        if remaining and not deliverables:
            remaining = []
        return dict(outcome=('completed' if result.get('return_code') == 0 and not remaining else 'incomplete'),
                    judged=judged, judged_ok=matched,
                    summary=str(result.get('stdout') or '')[-OUTPUT_TAIL_CHARS:] or '（没有输出）',
                    changes=self._changes(row['id']),
                    tests=tests,
                    artifacts=artifacts,
                    evidence_refs=[item.get('id') for item in artifacts if item.get('id')],
                    remaining=remaining + list(result.get('remaining') or []),
                    blockers=list(result.get('blockers') or []),
                    side_effects=list(result.get('side_effects') or []),
                    usage=result.get('usage'),
                    node=row.get('node'), origin=row.get('origin'))

    def _outcome_judgement(self, row, stdout, result):
        """无交付物任务的成功判据：目标里写了要带回什么，就看它有没有带回。

        goal 可以是 ``{"result": "..."}``（D3 的 placeholder-token 就是这种），也可以是
        自然语言 + checkpoint 里的 ``acceptance`` 条目 ``output_contains:<字面量>``。
        返回 ``(ok, judged)``；``judged=False`` 表示这次没有可判的判据（不臆断成功）。
        """
        checkpoint = _safe_json(row.get('checkpoint'), {}) or {}
        wanted = []
        goal = str(row.get('goal') or '')
        try:
            parsed = json.loads(goal) if goal.strip().startswith('{') else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get('result'):
            wanted.append(str(parsed['result']))
        for item in checkpoint.get('acceptance') or []:
            text = str(item or '')
            if text.startswith('output_contains:'):
                wanted.append(text.split(':', 1)[1].strip())
        if not wanted:
            return False, False
        missing = [value for value in wanted if value and value not in stdout]
        return (not missing), True

    def _evidence_checks(self, row, result):
        """这次有没有**真实证据**：交付物哈希对得上、或者无交付物任务的成功判据命中。

        返回 ``(checks, evidence)``：``checks`` 是给 ``_verify`` 的判据列表，``evidence``
        是写回台账的证据摘要。没有证据时两者都是空——verification 就停在 pending。
        """
        checks, evidence = [], []
        manifest = self._manifest_from_run(row, result) if result else {}
        for item in manifest.get('artifacts') or []:
            digest, size = _file_digest(Path(item['path']))
            ok = digest == item.get('sha256')
            checks.append(dict(kind='artifact', path=item['path'], ok=ok,
                               reason='' if ok else 'sha256_mismatch'))
            evidence.append(dict(kind='artifact', path=item['path'], sha256=digest, ok=ok))
        if manifest.get('judged'):
            checks.append(dict(kind='output_judgement', ok=bool(manifest.get('judged_ok')),
                               reason='' if manifest.get('judged_ok') else 'output_mismatch'))
            evidence.append(dict(kind='output_judgement', ok=bool(manifest.get('judged_ok')),
                                 summary=str(result.get('stdout') or '')[-400:]))
        return checks, evidence

    def _guard_transfer(self, row):
        """send（转交）与 resume 共用的嵌套守卫：产品侧硬拦，不靠模型自觉。

        规则：已经在最深一层的子 agent 既不能收「再派一个」的转交，也不能被续作到更深
        （续作会生成新 attempt，新的 attempt 仍然可能派出下一层）。父已终结的行也不许
        再转交——活已经收了。
        """
        limits = self._limits()
        depth = int(row.get('depth') or 0)
        if depth >= limits['max_depth']:
            raise AgentError('nesting_limit',
                             '它已经在第 %d 层（上限 %d），不能在这条链上再派活；'
                             '把这件事放进当前这一层的目标里。' % (depth, limits['max_depth']),
                             depth=depth, agent_id=row['id'])
        parent_id = str(row.get('parent_id') or '')
        if parent_id:
            parent = self.repo._one('SELECT status FROM companion_agent_tasks WHERE id=?',
                                    (parent_id,))
            if parent and parent['status'] in TERMINAL:
                raise AgentError('parent_finished',
                                 '它的父任务已经结束（%s），这条链不能再往下走。' % parent['status'],
                                 agent_id=row['id'])
        return depth

    def _verify(self, row, checks):
        """有检查项才给结论；没有证据就给终态 unverified，绝不默认通过。

        2026-11-18：原来没有检查项一律返回 None，于是这些记录永远停在
        `verification=pending`——生产里有 3 条 `completed + pending` 永远不会变。

        「没有检查项」有两种含义，必须分开：
        - 任务还在跑，检查项以后会来        → None（真 pending，会变成别的）
        - 任务已经终结，仍然没有检查项      → 'unverified'（没有东西可核验，这是终态）

        为什么不说成「待验证」：那是承诺「验证会来」。事实是：
        它没有交付物、也没有写验收判据，所以没有任何东西可以拿来核验。
        「没法确认」是事实，「待验证」是承诺。
        """
        if not checks:
            status = str(row.get('status') or '')
            if status in TERMINAL:
                # 已经终结却没有任何检查项：这不是「还在等」，是「验不了」。
                return 'unverified'
            return None
        if all(item.get('ok') for item in checks):
            return 'passed'
        return 'failed'

    def backfill_unverified(self, *, dry_run=False):
        """把「已终结、但没有证据可核验」的历史 task 从 pending 改成 unverified。

        2026-11-18：`_verify` 之前没有终态概念，导致这些行的 verification 永远停在
        pending。代码修了，但**库里已有的旧数据还在说旧话**——她读到的是数据，不是代码。

        所以做一次带审计的回填：
        - 判定沿用 `_verify` 的规则（终态 + 没有检查项 → unverified），不另立标准
        - 每改一行写一条 agent 事件，说明「这行为什么验不了」
        - 幂等：只处理 verification='pending' 且已终结的行，改过的不会再改

        返回改动过的 id 列表；dry_run 时只统计不写库。
        """
        rows = self.repo._all(
            "SELECT id, status, verification FROM companion_agent_tasks "
            "WHERE verification='pending'")
        changed = []
        for row in rows:
            # repo._all 返回 sqlite3.Row：按下标取，不能用 .get()
            status = str(row['status'] or '')
            if status not in TERMINAL:
                # 还在跑：pending 是对的，以后会变成别的
                continue
            changed.append(row['id'])
            if dry_run:
                continue
            with self.repo.tx():
                self.repo._exec(
                    "UPDATE companion_agent_tasks SET verification='unverified' WHERE id=?",
                    (row['id'],))
            self._log(
                row['id'], 'verification-reclassified',
                ('这条任务已经终结（status=%s），但没有交付物、也没有验收判据，'
                 '所以没有任何东西可以拿来核验。以前停在 pending，'
                 '看起来像"还在验证"；实际是"验不了"，这是终态。' % status),
                dict(from_verification='pending', to_verification='unverified',
                     reason='terminated_without_evidence', task_status=status))
        return changed

    def _workdir(self, row, checkpoint):
        root = (self.home / 'data/companion/agents').resolve()
        if row.get('node') == CLOUD_NODE:
            root = Path('/tmp/companion-agents').resolve()
        folder = root / (row['id'][-24:] if row.get('node') == CLOUD_NODE else row['id'])
        return folder

    def _cleanup_workdir(self, row, workdir):
        if row.get('node') == CLOUD_NODE:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)

    def _runner(self, row, action, workdir, checkpoint):
        origin = row.get('origin')
        if origin == 'dsh':
            return self._run_dsh(row, action, workdir, checkpoint)
        if origin == 'work':
            return self._run_worker(row, action, workdir, checkpoint)
        if origin == 'stub':
            return self._run_stub(row, action, workdir, checkpoint)
        raise AgentError('node_required', '没有可用的执行后端：%r' % origin)

    async def _run_worker(self, row, action, workdir, checkpoint):
        """文件类活：同一仓库共用一份工作副本，靠一把文件锁串行。

        进程树归这个协程所有（``ProcessTree``），取消时连子孙一起停；跑在
        独立工作目录里，绝不把 cwd 缺省成 home。

        跑的是 ``action['command']``，其次台账里的 ``checkpoint['command']``
        （create 已把它记下来，重启或续跑还能取到）。**不用 goal 兜底**：
        goal 是自然语言，把它当命令跑等于把意图当命令——goal 恰好是 ``ls``
        就真的 ls 了。没有 command 的活已经在 ``create()`` 里被拒。
        """
        requested = action.get('workspace')
        shared = self.workspace_path(requested)[0]
        cwd = Path(shared) if shared and Path(shared).is_dir() else workdir
        if not cwd.is_dir():
            raise AgentError('workspace_missing', '工作目录不存在：%s' % cwd)
        script = str(action.get('command') or (checkpoint or {}).get('command') or '')
        lock_path = folder_lock_path(cwd, self.home)
        with FileLock(lock_path):
            return await self._spawn(row, dict(agent_id=row['id'], requested_at=_now(),
                                               workspace=str(cwd)),
                                     workdir=workdir, cwd=str(cwd), script=script)

    async def _run_stub(self, row, action, workdir, checkpoint):
        return await self._spawn(row, dict(agent_id=row['id'], requested_at=_now(), workspace=str(workdir)),
                                 workdir=workdir, cwd=str(workdir),
                                 script=str(action.get('goal') or ''), fake=True)

    async def _run_dsh(self, row, action, workdir, checkpoint):
        paths = self._dsh_paths()
        if paths is None:
            raise AgentError('dsh_unavailable', 'DSH 运行时不可用。')
        root, node, entry = self.dsh_launch_paths(paths)
        instr = self._instruction_text(row)
        record = dict(dsh_root=str(root), node=str(node), entry=str(entry),
                      provider=getattr(self.cfg, 'dsh_provider', 'deepseek-official'),
                      model=getattr(self.cfg, 'model', 'deepseek-flash'),
                      dsh_home=str(self._dsh_home()), timeout=float(
                          (_safe_json(row.get('budget'), {}) or {}).get('deploy', {}).get('runtime_s')
                          or DEFAULT_TIMEOUT_SECONDS),
                      resume_session_id=row.get('session_id') or '')
        return await self._spawn(row, record, workdir=workdir, cwd=None, script=instr, dsh=True)

    def _agent_paths(self):
        """子 agent 允许读写的目录白名单（产品侧裁定，不靠提示词）。"""
        workdir_root = (self.home / 'data/companion/agents').resolve()
        artifacts = (self.home / 'data/companion/artifacts').resolve()
        runtime = Path(str((self._dsh_paths() or {}).get('root') or '/nonexistent')).resolve()
        read = [workdir_root, artifacts, runtime, Path('/usr'), Path('/lib'), Path('/lib64'),
                Path('/bin'), Path('/opt/qq-deepseek/toolchain'), Path('/etc/ssl')]
        write = [workdir_root, artifacts]
        return dict(read=[str(item) for item in read], write=[str(item) for item in write],
                    network='provider-only')

    def _permission_verdict(self, *, path, mode, cwd=None):
        """一份越界访问该不该被拒：返回 ``(allowed, reason)``。

        这是产品侧的真判定：spawn 之前就按白名单决定沙箱模式，跑起来之后再按同一份
        规则核对沙箱自己的拒绝证据。证件类路径（配置、凭据、运行时 home）永远在写禁列。
        """
        target = Path(os.path.normpath(str(path)))
        if not target.is_absolute() and cwd:
            target = Path(os.path.normpath(str(Path(cwd) / target)))
        resolved = str(target)
        secrets = [str(self._state_dir()), '/var/lib/companion', '/root', '/etc/companion']
        for prefix in secrets:
            if resolved == prefix or resolved.startswith(prefix.rstrip('/') + '/'):
                if mode == 'read' and prefix in ('/root', '/etc/companion'):
                    return False, 'secret_path'
                if mode in ('read', 'write'):
                    return False, 'secret_path'
        spaces = self._agent_paths()
        allowed = spaces['read'] if mode == 'read' else spaces['write']
        for prefix in allowed:
            if resolved == prefix or resolved.startswith(prefix.rstrip('/') + '/'):
                return True, ''
        return False, ('outside_read_allowlist' if mode == 'read' else 'outside_write_allowlist')

    def _state_dir(self):
        cfg = self.cfg
        home = Path(getattr(cfg, 'dsh_home', '') or 'data/dsh-home')
        return (home if home.is_absolute() else self.home / home).resolve()

    def _sandbox_mode(self):
        """由白名单推出来的沙箱模式：不许再写死 danger-full-access。

        DSH 支持 read-only / workspace-write / danger-full-access。elevated 只有在
        配置里显式打开（``agent_full_access = true``）时才用，默认 workspace-write：
        写只能落在工作目录与 artifacts，读只落在白名单。
        """
        if bool(getattr(self.cfg, 'agent_full_access', False)):
            return 'danger-full-access'
        return 'workspace-write'

    def _sandbox_patch(self, workdir):
        """写一份 profile 补丁，把 sandbox-policy 钉到产品裁定的模式与工作目录。

        实查（P13）：``@deepseek-ai/dsh-sdk-minimal`` 的 cordis.patch.yml 里写死了
        ``mode: danger-full-access``，环境变量 ``DSH_PERMISSION_MODE`` 在这个 bundle 下
        **不被读取**——只设环境变量等于没设（越界读写全部成功，实测过）。
        唯一真正生效的开关是调用方的 profile 补丁层，所以这里生成一份最小补丁，
        交给 HarnessConfig.patches（SDK 会以 ``--patch`` 传进去）。
        """
        folder = (self.home / 'data/companion/agents').resolve() / '_patches'
        folder.mkdir(parents=True, exist_ok=True)
        mode = self._sandbox_mode()
        path = folder / ('sandbox-%s.yaml' % stable_id('patch', str(workdir), mode)[-12:])
        # P18 真缺陷：以前这里写 ``- insert:``，那是「再挂一个实例」，不是覆盖——
        # dsh 的 --dump-config 实测会出现两个 sandbox-policy 节点
        # （danger-full-access 在前、workspace-write 在后），旧的仍在生效，
        # 真实模型回合因此能写进 /opt/qq-deepseek。覆盖写法是「同一个 id 直接给 config」。
        body = (
            '# 由 AgentManager._sandbox_patch 生成：覆盖 sdk-minimal 里写死的 danger-full-access。\n'
            '# 必须是「同 id 直接给 config」（覆盖），不能写成 - insert:（那是追加实例，旧的仍生效）。\n'
            '- id: sandbox-policy\n'
            '  name: \'@deepseek-ai/dsh-sandbox-policy\'\n'
            '  config:\n'
            '    mode: %s\n'
            '    workspaceRoot: %s\n'
            '    readOnlyRoots:\n%s' % (
                mode, json.dumps(str(workdir)),
                ''.join('      - %s\n' % json.dumps(str(root))
                        for root in self._read_roots(Path(workdir))))
        )
        path.write_text(body, encoding='utf-8')
        self._assert_single_sandbox_policy(path, mode)
        if mode == 'workspace-write':
            verdict = self._verify_composed_sandbox(path, workdir)
            self._sandbox_verification = verdict
            if verdict.get('skipped'):
                # Non-production home with no DSH runtime: degrade, but never silently -
                # the verdict is persisted beside the patch and carried on the manager.
                try:
                    import json as _json
                    Path(str(path) + '.verification.json').write_text(
                        _json.dumps(verdict, ensure_ascii=False), encoding='utf-8')
                except OSError:
                    pass
            elif verdict.get('unavailable'):
                # Cannot verify != failed. Stay conservative in production, but say precisely
                # what happened so the next reader does not chase a phantom escape.
                if self._production_home():
                    raise AgentError('sandbox_verification_unavailable',
                                     '无法核对合成配置（校验不可用，不等于补丁失效），拒绝派活：%s'
                                     % verdict.get('reason'))
                self._sandbox_verification = dict(verdict, degraded=True)
            elif not verdict.get('ok'):
                raise AgentError('sandbox_not_effective',
                                 '权限补丁没有真正生效，拒绝派活：%s' % verdict.get('reason'))
        return path

    def _read_roots(self, workdir):
        """子 agent 允许读的根：只给运行时与工作目录，其余全关。

        与 ``_agent_paths()['read']`` 同源；vendor 侧 landlock 按这份名单授读，
        没配就退回「读全放行」，所以这里必须给，不能省。
        """
        roots = list(self._agent_paths()['read'])
        roots.append(str(workdir))
        out = []
        for root in roots:
            if root not in out and Path(root).is_absolute():
                out.append(root)
        return out

    def _production_home(self):
        """是否跑在生产人物目录上。

        ``/var/lib/companion`` 下的 home 是唯一真实运行位置；测试与隔离沙箱用临时 home。
        """
        try:
            return '/var/lib/companion' in str(Path(self.home).resolve())
        except (OSError, ValueError):
            return False

    def _verification_skipped(self, why):
        """没有 DSH 运行时时怎么办：生产必须拒跑，其它环境显式标记 skipped。

        判据是「生产人物目录」——``/var/lib/companion`` 下的 home 是唯一真实运行位置；
        测试与隔离沙箱用临时 home，那里根本没有 dsh 运行时，硬失败只会让测试恒红。
        """
        if self._production_home():
            return dict(ok=False, reason='DSH runtime unavailable; cannot verify composed config')
        return dict(ok=True, skipped=True,
                    reason='skipped (non-production home, no DSH runtime): %s' % why)

    def _verify_composed_sandbox(self, patch_path, workdir, *, timeout=60):
        """把补丁交给 dsh 合成一次配置，核对 mode。

        这是「配置没生效」的唯一真判定：合成结果里 mode 仍是 danger-full-access，
        说明覆盖没落地——此时宁可派活失败，也不能让它带着全权跑起来。
        失败返回 {ok: False, reason: ...}，不抛。
        """
        import subprocess
        resolved = None
        try:
            paths = self._dsh_paths()
            if paths is not None:
                resolved = self.dsh_launch_paths(paths)
        except Exception:  # noqa: BLE001
            resolved = None
        if resolved is None or not Path(str(resolved[1])).is_file():
            # The executor route may hand back key names ('node') instead of paths, or fail
            # outright. That is not "no runtime"; re-resolve straight from config first.
            node_cfg = str(getattr(self.cfg, 'dsh_node', '') or '')
            root_cfg = str(getattr(self.cfg, 'dsh_root', '') or '')
            if node_cfg and root_cfg:
                try:
                    resolved = self.dsh_launch_paths(
                        dict(root=root_cfg, node=node_cfg, entry='lib/bin.js'))
                except Exception as error:  # noqa: BLE001
                    return self._verification_skipped('config DSH entry not resolvable: %s' % error)
        if resolved is None or not Path(str(resolved[1])).is_file():
            return self._verification_skipped('no executable DSH node')
        root, node, entry = resolved
        # dsh --dump-config requires --profile; without it the CLI exits with a usage
        # error (measured), which would make this assertion fail on every dispatch.
        profile = str(getattr(self.cfg, 'dsh_profile', '') or 'sdk-minimal')
        command = [str(node), str(entry), '--dump-config', '--profile', profile,
                   '--patch', str(patch_path)]
        try:
            Path(workdir).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return dict(ok=False, unavailable=True,
                        reason='verification unavailable: cannot create cwd %s (%s)'
                               % (workdir, type(error).__name__))
        try:
            done = subprocess.run(command, cwd=str(workdir), capture_output=True, text=True,
                                  timeout=timeout, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        except (OSError, subprocess.SubprocessError) as error:
            # The probe itself could not run (missing binary, timeout, bad cwd): that is NOT
            # evidence that the patch failed, and saying so misdirects the next debugging pass.
            return dict(ok=False, unavailable=True,
                        reason='verification unavailable: dump-config did not run (%s: %s)'
                               % (type(error).__name__, error))
        text = (done.stdout or '') + (done.stderr or '')
        if done.returncode != 0:
            # Keep the real stderr tail: 'exit 1' alone cannot tell a node crash from a
            # rejected config, and this verdict is what blocks dispatch.
            return dict(ok=False, returncode=done.returncode,
                        reason='dump-config exit %s: %s'
                               % (done.returncode,
                                  ' '.join((done.stderr or '').split())[-300:] or '(no stderr)'))
        modes, current = [], None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('- id:') or stripped.startswith('id:'):
                current = stripped.split(':', 1)[1].strip()
                continue
            if current == 'sandbox-policy' and stripped.startswith('mode:'):
                modes.append(stripped.split(':', 1)[1].strip())
        if 'danger-full-access' in modes:
            return dict(ok=False, modes=modes,
                        reason='composed sandbox-policy.mode is still danger-full-access (override did not apply)')
        if modes != ['workspace-write']:
            return dict(ok=False, modes=modes,
                        reason='composed sandbox-policy mode is not exactly one workspace-write: %r' % (modes,))
        return dict(ok=True, modes=modes, patch=str(patch_path))

    @staticmethod
    def _assert_single_sandbox_policy(patch_path, expected_mode):
        """生成即自检：补丁里 sandbox-policy 只能出现一次，mode 只能出现一次。

        这条是防止「又有人改回 insert」的唯一防线；测试套件里还有一条走
        dsh --dump-config 的强断言（真正合成后的配置）。
        """
        text = Path(patch_path).read_text('utf-8')
        ids = text.count('- id: sandbox-policy')
        if ids != 1:
            raise AgentError('sandbox_patch_not_override',
                             '生成的权限补丁不是覆盖写法（sandbox-policy 出现 %d 次）' % ids)
        modes = [line.split(':', 1)[1].strip() for line in text.splitlines()
                 if line.strip().startswith('mode:')]
        if modes != [expected_mode]:
            raise AgentError('sandbox_patch_mode_mismatch',
                             '补丁里的 mode 必须恰好一个且等于 %s，实际 %r' % (expected_mode, modes))

    def _dsh_env(self):
        """主 dsh 路径同款凭据环境：api_key / base_url 从宿主配置读，注入子进程 env。

        P12 实查：``/proc/<pid>/environ`` 里既没有 DEEPSEEK_API_KEY 也没有
        DEEPSEEK_BASE_URL（systemd unit 只有 PYTHONUNBUFFERED/TZ，没有 EnvironmentFile），
        主 dsh 路径能跑是因为 ``ActionExecutor.run`` 在 spawn 时把
        ``cfg.api_key`` / ``cfg.base_url`` 塞进子进程环境。子 agent 走的是另一条 spawn，
        以前没接这一步——这里接上同一套，凭据同样不落盘。
        """
        cfg = self.cfg or getattr(self._core(), 'cfg', None)
        api_key = str(getattr(cfg, 'api_key', '') or '').strip()
        base_url = str(getattr(cfg, 'base_url', '') or '').rstrip('/')
        env = {}
        if api_key:
            env['DEEPSEEK_API_KEY'] = api_key
        if base_url:
            # 与主路径一致：官方域名要带 /anthropic，否则每轮 404 且报错里不含 URL。
            env['DEEPSEEK_BASE_URL'] = (base_url + '/anthropic'
                                        if base_url == 'https://api.deepseek.com' else base_url)
        # 权限模式由白名单决定（默认 workspace-write），不再无条件全权。
        env['DSH_PERMISSION_MODE'] = self._sandbox_mode()
        return env

    def _dsh_home(self):
        executor = self.executor
        for _ in range(3):
            getter = getattr(executor, '_dsh_home', None) if executor is not None else None
            if callable(getter):
                try:
                    return Path(getter())
                except Exception:  # noqa: BLE001
                    break
            executor = getattr(executor, 'original', None) if executor is not None else None
        value = Path(getattr(self.cfg, 'dsh_home', '') or 'data/dsh-home')
        return value if value.is_absolute() else self.home / value

    def _instruction_text(self, row):
        checkpoint = _safe_json(row.get('checkpoint'), {}) or {}
        lines = ['目标：' + row['goal']]
        if checkpoint.get('deliverables'):
            lines.append('交付物：' + '；'.join(checkpoint['deliverables']))
        if checkpoint.get('acceptance'):
            lines.append('验收：' + '；'.join(checkpoint['acceptance']))
        lines.append('工作目录只用于这一件事：改动留在会话记录里，交付物写成文件。')
        lines.append('结束时用一段摘要说明做成了什么、哪些没做完、卡在哪里。')
        return '\n'.join(lines)

    @staticmethod
    def dsh_launch_paths(paths):
        """把 (root, node, entry) 拼成真正能 exec 的入口：不许拼出一个不存在的文件。

        ``entry`` 可能是相对（``lib/bin.js``、``apps/cli/src/bin.ts``）也可能是绝对路径；
        ``root`` 是 DSH 运行时根（生产上是 ``vendor/dsh-runtime``）。两个 worker 以前各用
        一套默认值，拼错时 SDK 会回落到「去找名为 dsh 的可执行文件」，报
        ``FileNotFoundError: 'dsh'``，看不出是路径拼错。
        """
        # 同时接受 (root, node, entry) 元组和执行器字典两种形状——两条调用链各用各的。
        if isinstance(paths, dict):
            root_val, node_val, entry_val = paths.get('root'), paths.get('node'), paths.get('entry')
        else:
            root_val, node_val, entry_val = paths[0], paths[1], paths[2]
        root = Path(str(root_val))
        entry = str(entry_val or '')
        node = str(node_val or '')
        if not node or not Path(node).is_file():
            raise AgentError('dsh_node_missing', '找不到 node 可执行文件：%s' % (node or '(空)'))
        if not entry:
            raise AgentError('dsh_entry_missing', 'DSH 运行时没有给出入口（entry 为空）')
        target = Path(entry) if Path(entry).is_absolute() else root / entry
        if not target.is_file():
            raise AgentError('dsh_entry_missing',
                             '拼出来的 DSH 入口不存在：%s（root=%s，entry=%s）'
                             % (target, root, entry))
        return root, node, target

    async def _spawn(self, row, record, *, workdir, cwd, script, dsh=False, fake=False,
                     initialize_only=False):
        """起进程树、等它结束、收证据。取消时连子孙一起停。"""
        import sys
        from .supervisor import ProcessTree
        folder = (self.home / 'data/companion/artifacts').resolve()
        folder.mkdir(parents=True, exist_ok=True)
        log_path = folder / ('agent-' + row['id'] + '.log')
        request_path = log_path.with_suffix('.request.json')
        result_path = log_path.with_suffix('.result.json')
        # P12 根因：``record`` 里已经带了 dsh 的 ``node``（node 可执行文件路径），
        # 而这里又用同一个键写「传输用的后端名」，把真路径覆盖成了 'dsh'，worker 于是报
        # dsh_node_missing: dsh。传输后端改名为 ``transport``，不再和 node 路径抢键。
        request = dict(record, transport='dsh' if dsh else ('stub' if fake else 'command'),
                       cwd=str(cwd or workdir), instruction=script,
                       patches=[str(self._sandbox_patch(Path(cwd or workdir)))] if dsh else [],
                       timeout_seconds=float(record.get('timeout') or DEFAULT_TIMEOUT_SECONDS),
                       result_path=str(result_path))
        Path(workdir).mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(request, ensure_ascii=False, default=str), encoding='utf-8')
        command = [sys.executable, '-B',
                   str(Path(__file__).with_name('companion_agent_worker.py')), str(request_path)]
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        if dsh:
            # 凭据注入与主 dsh 路径完全同一套（companion_executor.run 的 dsh 分支）：
            # 进程环境里没有 DEEPSEEK_API_KEY/BASE_URL，unit 也没有 EnvironmentFile，
            # 主路径是从 config.toml 读出来、在 spawn 时注入子进程环境的；这里照做，
            # 凭据只存在于子进程内存里，不写盘。
            env.update(self._dsh_env())
        tree = None
        started = time.monotonic()
        started_epoch = _now()
        with log_path.open('wb') as output:
            tree = ProcessTree(command, cwd=str(cwd or workdir), env=env, pipes=True,
                               stdout_file=output, stderr_file=output)
            try:
                live = self._live.get(row['id'])
                if live is not None:
                    live['stdin'] = tree.process.stdin
                    live['pid'] = tree.process.pid
                self._update(row['id'], pid=int(tree.process.pid or 0))
                await asyncio.to_thread(tree.process.wait)
            finally:
                tree.stop()
        completed_at = _now()
        result = _safe_json(result_path.read_text('utf-8') if result_path.is_file() else '', {}) or {}
        raw = log_path.read_bytes() if log_path.is_file() else b''
        preview = raw.decode('utf-8', errors='replace')[-OUTPUT_TAIL_CHARS:]
        result['stdout'] = preview
        result['stdout_path'] = str(log_path)
        result['return_code'] = result.get('return_code', tree.process.returncode)
        artifacts = []
        for item in result.get('artifacts') or []:
            path = Path(item.get('path') or '')
            if not path.is_absolute():
                path = (Path(cwd) if cwd else workdir) / path
            if path.is_file():
                digest, size = _file_digest(path)
                artefacts_id = stable_id('agent-artifact', row['id'], str(path))
                artifacts.append(dict(id=artefacts_id, path=str(path), sha256=digest, bytes=size,
                                      deliverables=str(item.get('name') or path.name)))
                self._log(row['id'], 'change', '%s %s' % ('write', path),
                          dict(path=str(path), op='write', sha256=digest, bytes=size))
        result['artifacts'] = artifacts
        result['seconds'] = round(time.monotonic() - started, 3)
        result.setdefault('usage', None)
        # 事后核验（不靠它的自述）：交付物必须是本次运行期间写出来的；
        # 工作进程退出前留下的证据在 _finish 里标 passed，不是这里自己宣布。
        checks = []
        for item in artifacts:
            try:
                written_at = Path(item['path']).stat().st_mtime
            except OSError:
                written_at = 0
            checks.append(dict(name='artifact', ok=bool(written_at >= started_epoch),
                               detail=str(item['path'])))
        if result.get('return_code') not in (None, 0):
            checks.append(dict(name='exit_code', ok=False, detail='退出码 %s' % result.get('return_code')))
        result['_checks'] = checks
        self._update(row['id'], checkpoint=json.dumps(dict(
            _safe_json(row.get('checkpoint'), {}) or {},
            changes=self._changes(row['id']), last_workdir=str(workdir),
            artifacts=[item['id'] for item in artifacts]), ensure_ascii=False))
        if dsh:
            result['session_id'] = result.get('session_id')
            if result.get('session_id'):
                self._update(row['id'], session_id=str(result['session_id']))
        return result


TASK_STATUS = {
    'queued': 'active',
    'running': 'active',
    'waiting': 'waiting',
    'cancelling': 'active',
    'completed': 'done',
    'failed': 'paused',
    'interrupted': 'paused',
    'cancelled': 'cancelled',
}


class _ScopeShim:
    """``TaskService`` 只用到 ``scope_id``/``conversation_id`` 两个字段。"""

    def __init__(self, scope_id, conversation_id=''):
        self.scope_id = str(scope_id)
        self.conversation_id = str(conversation_id or scope_id)


def _items(value, limit, count):
    """交付物/验收条件：接受列表，也接受一行一条的字符串；空行丢掉。"""
    if isinstance(value, str):
        value = [part for part in value.replace('；', '\n').replace(';', '\n').splitlines() if part.strip()]
    if value in (None, ''):
        return []
    return [str(item).strip()[:limit] for item in value if str(item).strip()][:count]


def _action_fields(task):
    """七操作的参数可能放在 arguments(JSON 字符串) 里，也可能在顶层。

    取完之后**按 op 过滤**再返回：每个 op 认的参数集合见 ``OP_FIELDS``。
    不过滤就会把 ``goal``（create 的合法参数）原样展开给 ``inspect()``，
    变成 ``TypeError: inspect() got an unexpected keyword argument 'goal'``——
    那是给工程师看的异常，不是「你需要补什么」。生产库里正是这个形态。
    """
    raw = task.get('arguments')
    if isinstance(raw, str):
        raw = _safe_json(raw, {}) or {}
    fields = dict(raw) if isinstance(raw, dict) else {}
    for key in ('op', 'agent_id', 'message_id', 'text', 'intent', 'wait_s', 'cursor', 'until',
                'why', 'include_children', 'artifact_id', 'path', 'offset', 'limit', 'goal',
                'command', 'deliverables', 'acceptance', 'workspace', 'node', 'node_reason',
                'scope', 'budget', 'parent_task_ref', 'idempotency_key', 'context_refs',
                'project', 'conversation_ref', 'conversation', 'query', 'resume_session_id',
                'from_session'):
        value = task.get(key)
        if value not in (None, '') and fields.get(key) in (None, ''):
            fields[key] = value
    if 'conversation' in fields and 'conversation_ref' not in fields:
        fields['conversation_ref'] = fields.pop('conversation')
    renaming = {'agent id': 'agent_id', 'agent': 'agent_id', 'id': 'agent_id', 'ref': 'agent_id',
                'wait_seconds': 'wait_s', 'message': 'text'}
    for old, new in renaming.items():
        if old in fields and new not in fields:
            fields[new] = fields.pop(old)
    # 七操作的必填入参在这里挡一次，错误会比执行到一半再报清楚得多。
    op = str(fields.get('op') or '').strip().lower()
    if op and op not in OPS:
        raise AgentError('unknown_op', 'kind=agent 的 op 只有 %s，收到 %r' % ('/'.join(OPS), op))
    if op in {'send', 'inspect', 'cancel', 'resume'} and not str(fields.get('agent_id') or '').strip():
        raise AgentError('agent_id_required', '%s 需要 agent_id（create 回执里的那个）' % op)
    if op == 'send' and not str(fields.get('text') or '').strip():
        raise AgentError('text_required', 'send 需要 text')
    if op == 'read_artifact' and not (fields.get('agent_id') or fields.get('artifact_id') or fields.get('path')):
        raise AgentError('artifact_required', 'read_artifact 需要 agent_id / artifact_id / path 之一')
    allowed = OP_FIELDS.get(op)
    if allowed is not None:
        # 只留这个 op 真认的键。**不要**在这里放行 `project`/`conversation_ref` 这类
        # DECISION 层面的通用字段：dispatch() 会把剩下的全部 **展开到方法签名上，
        # 放行一个就多一种 TypeError（生产实测：先 'goal'，改完又冒出 'project'——
        # model 每个动作都必须带 project，所以它每次都会撞上）。
        fields = {key: value for key, value in fields.items() if key in allowed}
    return fields



def reconcile_on_start(repo, *, agent_id=None, life=None):
    """进程重启：上次停在 running 的子 agent 记为 interrupted，只记一次。

    它**不会**自动重放——重放一份可能已经改过文件的工作，等于把「中断」变成
    「重复执行」。要接着做，由她显式 ``resume`` 生成新的 attempt。

    收尾干净：``cancelling`` 一律落成 ``cancelled``（取消早就请求过了）；
    ``queued``/``waiting`` 原样保留，它们本来就没有进程在跑，启动后仍可被领取。
    """
    repo, detected = _repo_of(repo)
    life = life or detected
    AgentManager.ensure_schema(repo)
    from .tasks import TaskService
    tasks = TaskService(repo)
    clause, params = (' AND agent=?', (agent_id,)) if agent_id else ('', ())

    def shell(row):
        # 只借壳记账：重启收尾不需要角色、执行器、模型，也不许顺手启动任何东西。
        manager = AgentManager.__new__(AgentManager)
        manager.cfg, manager.home = None, Path('.')
        manager.executor, manager.core, manager.life = None, None, life
        manager.repo, manager._tasks = repo, tasks
        manager.scope_id = row['scope_id'] or None
        manager.agent_id = row['agent']
        manager._lock, manager._live, manager._executors = None, {}, set()
        return manager

    interrupted = []
    for row in repo._all("SELECT * FROM companion_agent_tasks WHERE status='running'" + clause, params):
        try:
            shell(row)._set_status(row['id'], 'interrupted',
                                   failure=dict(code='interrupted', reason='进程重启，结果未知；未自动重放'))
            shell(row)._log(row['id'], 'interrupted', '进程重启，结果未知；要接着做请显式 resume')
            interrupted.append(row['id'])
        except AgentError:  # 行状态在并发里已经变了：不是这次要处理的
            continue
    cancelled = []
    for row in repo._all("SELECT * FROM companion_agent_tasks WHERE status='cancelling'" + clause, params):
        try:
            shell(row)._set_status(row['id'], 'cancelled',
                                   cancel_reason=row['cancel_reason'] or '重启前已请求取消')
            shell(row)._log(row['id'], 'cancelled', '重启前已请求取消，收尾为已取消')
            cancelled.append(row['id'])
        except AgentError:
            continue
    # 顺带把执行器账本里「领走了、但进程已经没了」的行动记为中断：执行器拿到的是
    # 上一次进程里的进程树，重启后它不可能再等到结果。这里只改状态，不重放。
    action_columns = {row['name'] for row in repo._all('PRAGMA table_info(companion_actions)')}
    if 'status' in action_columns:
        clause_actions = ' AND agent=?' if agent_id and 'agent' in action_columns else ''
        params_actions = (agent_id,) if clause_actions else ()
        for row in repo._all("SELECT id FROM companion_actions WHERE status='running'"
                             + clause_actions, params_actions):
            with _TABLE_LOCK:
                with repo.tx():
                    repo._exec("UPDATE companion_actions SET status='interrupted',updated=? WHERE id=?",
                               (_now(), row['id']))
            interrupted.append(row['id'])
    if (interrupted or cancelled) and _has_table(repo, 'companion_activity'):
        with repo.tx():
            repo._exec('INSERT INTO companion_activity VALUES(?,?,?,?,?,?)',
                       (uuid.uuid4().hex, agent_id or 'agent', 'activity',
                        '重启时子 agent 有未结束的工作：%d 件记为中断、%d 件收尾为取消（未自动重放）'
                        % (len(interrupted), len(cancelled)),
                        json.dumps(dict(interrupted=interrupted, cancelled=cancelled), ensure_ascii=False),
                        _now()))
    return dict(interrupted=interrupted, cancelled=cancelled)

__all__ = [
    'AgentError', 'AgentManager', 'OPS', 'STATES', 'TRANSITIONS', 'STATE_ALIASES', 'TASK_STATUS',
    'VERIFICATIONS', 'decide', 'merge_usage', 'normalize_route', 'transition', 'reconcile_on_start',
    'estimate_cn_chars', 'clip_text', 'SUMMARY_CHARS', 'CHANGES_SHOWN', 'MAX_CONCURRENT', 'MAX_DEPTH',
    'delivery_ledger', 'FileLock',
]
