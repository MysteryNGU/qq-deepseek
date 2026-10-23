#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动/吩咐授权协议。

原则：
- 主人吩咐（commanded）：在指令范围内可执行。
- 她自己想做（initiative）：默认先问；长期规则可授予特定范围。
- 探索（exploration）：只读自由，有副作用先问。
- 硬边界不能被任何规则覆盖：密钥、删数据、付款、关审计。

这里是“聊天里向主人确认”的产品逻辑，和宿主工具审批策略解耦。
"""

from __future__ import annotations

import fnmatch
import ast
import json
import re
import time
import hashlib
from dataclasses import dataclass, field
from typing import Any

from .services.ctx import get_origin

FREE = "free"
NOTIFY = "notify"
ASK = "ask"
BLOCKED = "blocked"

BLOCKED_EFFECTS = {"secrets", "admin", "audit_off"}
ASK_EFFECTS = {"destructive", "payment", "privacy", "public_content", "build_scripts"}


# Supporting steps may change tools, but new recipients/packages are not implied.
SUPPORT_EFFECTS = {"read", "internal", "network", "device_nav", "device_input",
                   "device", "ui", "command", "file_write", "code", "write_self"}


def sensitive_targets(env):
    effects = set(env.effects or [])
    if not effects & {"install", "social", "message", "public_content", "destructive", "payment", "privacy", "build_scripts"}:
        return {}
    action = env.action or {}
    args = action.get("args") or {}
    if action.get("tool") == "dsh_bridge":
        args = {**args, **(args.get('arguments') or {})}
    keys = {"peer", "uin", "name", "recipient", "target", "path", "file", "pkg", "profile", "slug", "url",
            "amount", "currency", "account", "destination", "approvedBuilds"}
    targets = {k: v for k, v in args.items() if k in keys and v not in (None, "")}
    if args.get("cmd"):
        targets["cmd"] = args["cmd"]
    if action.get("tool") == "dsh" and args.get("args"):
        targets["argv"] = args["args"]
    targets.update(env.target or {})
    # Keep the effect class: an identical string is not the same recipient/install.
    return {"effects": sorted(effects - SUPPORT_EFFECTS), "resources": targets}


@dataclass
class IntentEnvelope:
    goal: str
    why: str = ""
    origin: str = "initiative"
    effects: list[str] = field(default_factory=list)
    target: dict = field(default_factory=dict)
    expected: str = ""
    risk: str = "yellow"
    scope: str = "global"
    action: dict = field(default_factory=dict)     # {"tool": ..., "args": {...}}
    conversation_id: str = ""
    consent_class: str = ""
    consent_reason: str = ""
    task_id: str = ""

    def bind_task(self):
        from .services.ctx import current_task
        from life.delivery import active_delivery
        ctx = active_delivery.get()
        task = current_task.get() or {}
        if ctx is not None:
            self.conversation_id = ctx.scope.conversation_id
        elif not self.conversation_id:
            self.conversation_id = task.get("conversation_id", "")
        if not self.task_id:
            self.task_id = str(task.get("ref") or "")
            if self.task_id:
                self.goal = task.get("goal") or self.goal
            else:
                self.task_id = "goal_" + hashlib.sha256(
                    (self.conversation_id + "|" + self.goal.strip()).encode()).hexdigest()[:24]
        return self

    def as_dict(self) -> dict:
        return {
            "goal": self.goal, "why": self.why, "origin": self.origin,
            "effects": self.effects, "target": self.target,
            "expected": self.expected, "risk": self.risk,
            "scope": self.scope, "action": self.action,
            "conversation_id": self.conversation_id,
            "consent_class": self.consent_class,
            "consent_reason": self.consent_reason,
            "task_id": self.task_id,
        }


READ_ONLY_SHELL_CMDS = {
    "get-childitem", "dir", "ls", "get-content", "type", "select-string",
    "findstr", "get-process", "get-service", "get-item", "test-path",
    "resolve-path", "get-location", "pwd", "where", "get-command", "get-help",
    "echo", "write-output", "select-object", "where-object", "foreach-object",
    "sort-object", "measure-object", "format-table", "format-list", "out-string", "get-date",
}
WRITE_SHELL_RE = re.compile(
    r"(?i)(set-content|out-file|new-item|remove-|del\s|rmdir|copy\s|move\s|"
    r"rename-|invoke-webrequest|invoke-restmethod|curl\s|wget\s|"
    r"start-process|stop-process|python|node|npm|pip)")


def shell_read_only(cmd: str) -> bool:
    """保守判断一条 PowerShell 是不是只读：白名单命令 + 不含写文件/起进程。"""
    c = str(cmd or "").strip()
    if not c or ">" in c or re.search(r'[`$(){}&]', c) or WRITE_SHELL_RE.search(c):
        return False
    for seg in re.split(r"[;|]", c):
        seg = seg.strip()
        if not seg:
            continue
        first = seg.split()[0].strip('"').lower()
        if first not in READ_ONLY_SHELL_CMDS:
            return False
    return True


def python_clock_read_only(code: str) -> bool:
    """Recognize actual clock reads without treating arbitrary Python as safe.

    Bindings resolve to a small set of known pure clock/format calls. Unknown
    imports, attributes, assignment targets and statements remain executable
    code with the usual verification requirements.
    """
    bindings = {'print':'print'}
    calls = {'datetime.datetime.now':'clock', 'datetime.datetime.utcnow':'clock',
             'datetime.datetime.today':'clock', 'datetime.date.today':'clock',
             'time.time':'number', 'time.localtime':'timevalue', 'time.gmtime':'timevalue',
             'time.strftime':'text', 'clock.isoformat':'text', 'clock.strftime':'text',
             'clock.time':'clock', 'clock.date':'clock', 'print':'none'}
    try:
        tree = ast.parse(code)
        def value(node):
            if isinstance(node, ast.Name):return bindings.get(node.id)
            if isinstance(node, ast.Constant):return 'literal'
            if isinstance(node, ast.Attribute):
                parent=value(node.value)
                return parent+'.'+node.attr if parent else None
            if isinstance(node, ast.Call):
                if not all(value(a) for a in node.args) or any(k.arg is None or not value(k.value) for k in node.keywords):return None
                return calls.get(value(node.func))
            return None
        for stmt in tree.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.name not in {'datetime','time'}:return False
                    bindings[alias.asname or alias.name]=alias.name
            elif isinstance(stmt, ast.ImportFrom):
                if stmt.module!='datetime' or stmt.level:return False
                for alias in stmt.names:
                    if alias.name not in {'datetime','date'}:return False
                    bindings[alias.asname or alias.name]='datetime.'+alias.name
            elif isinstance(stmt, ast.Assign):
                resolved=value(stmt.value)
                if len(stmt.targets)!=1 or not isinstance(stmt.targets[0],ast.Name) or not resolved:return False
                bindings[stmt.targets[0].id]=resolved
            elif isinstance(stmt, ast.Expr):
                if not value(stmt.value):return False
            else:return False
        return bool(tree.body)
    except (SyntaxError, ValueError, RecursionError):
        return False


# 只读工具/动作：不产生外部副作用，也不该弹审批卡
READONLY_TOOLS = {
    "read_file", "list_dir", "search_code", "glob_files", "inspect_evidence",
    "inspect_failure", "self_check", "list_skills", "use_skill", "read_self",
    "web_search", "open_url", "read_qzone", "look_screen", "show_screen",
    "phone_look", "phone_see", "phone_watch", "trace",
}
READONLY_ACTIONS = {
    "read", "list", "search", "find", "status", "info", "observe", "see",
    "verify", "wait", "inspect", "history", "docs", "plugins", "chats",
    "map", "recent", "why", "thought", "pending", "rules", "show", "watch",
    "look", "open",
}
WRITE_TOOLS = {"edit_self", "write_file", "run_cmd", "write_self"}


def is_readonly_action(tool: str, args: dict | None = None) -> bool:
    """读文件/看屏幕/搜代码/查状态这类动作一律免费。

    模型经常把 read 说成 open、把查代码说成 shell；这里用动作语义兜底，
    避免同一件只读的事反复弹审批卡。
    """
    args = args or {}
    t = str(tool or "").lower()
    a = str(args.get("action") or args.get("kind") or "").lower()
    if t in READONLY_TOOLS:
        return True
    if t in WRITE_TOOLS:
        return False
    if a in READONLY_ACTIONS:
        return True
    if t == "computer" and a == "shell":
        return shell_read_only(str(args.get("cmd") or ""))
    if t == "computer" and a == "python":
        return python_clock_read_only(str(args.get('code') or ''))
    return False


@dataclass
class ConsentDecision:
    level: str
    reason: str
    rule_id: int = 0

    @property
    def allowed(self) -> bool:
        return self.level in (FREE, NOTIFY)


class ConsentGate:
    def __init__(self, store, cfg, identity=None):
        self.store = store
        self.cfg = cfg
        self.identity = identity

    # ---------------------------------------------------------------- 效果分类
    @staticmethod
    def classify(tool: str, args: dict | None = None) -> tuple[str, list[str]]:
        args = args or {}
        if tool == 'memory':
            return 'green', ['read'] if args.get('action') in {'search', 'detail'} else ['internal']
        if tool == "dsh_bridge":
            act = args.get('action')
            if act in {'list', 'plugins', 'status', 'edits'}:
                return "green", ["read"]
            if act in {'install_plugin', 'remove_plugin'}:
                return 'red', ['install', 'code']
            if act in {'edit', 'rollback'}:
                return 'red', ['code', 'write_self']
            if act == 'cancel':
                return 'yellow', ['internal']
            nested = args.get("arguments") or {}
            if args.get("tool") == "plugin_manager":
                if str(nested.get('action', '')).startswith('list_'):
                    return 'green', ['read']
                if nested.get('approvedBuilds'):
                    return 'red', ['install', 'code', 'build_scripts']
                return "red", ["install", "code"]
            nested_name = str(args.get('tool') or '')
            if nested_name in {'read','file_read','read_file','list_dir','grep','glob','job_list','job_output','web_search','web_fetch'}:
                return 'green', ['read', 'network']
            if nested_name in {'shell', 'bash', 'run_command', 'terminal'}:
                return ConsentGate.classify('computer', {'action': 'shell', 'cmd': nested.get('command') or nested.get('cmd') or ''})
            return "yellow", ["command"]
        if is_readonly_action(tool, args):
            return "green", ["read"]
        action = str(args.get("action") or "").lower()
        key = f"{tool}.{action}" if action else tool
        if tool in ("web", "browse", "browser"):
            return "green", ["network", "read"]
        if tool == "qq":
            if action in ("send", "draft_send", "reply"):
                return "yellow", ["social", "message"]
            if action in ("comment", "like"):
                return "yellow", ["social", "public_content"]
            if action in ("open", "observe", "read", "chats", "status"):
                return "green", ["read"]
            return "yellow", ["device"]
        if tool == "app":
            kind = str(args.get("kind") or action).lower()
            if action in ("observe", "list", "map", "open", "launch"):
                return "green", ["read"]
            if action == "verify":
                return "green", ["read"]
            if kind in ("explore", "scroll", "tap", "key", "wait"):
                return "yellow", ["device_nav", "read"]
            if kind == "type":
                return "yellow", ["device_input", "read"]
            return "yellow", ["device"]
        if tool in ("workshop", "code"):
            if action in ("read", "search", "history", "test", "inspect"):
                return "green", ["read"]
            return "red", ["code", "write_self"]
        if tool in ("dsh", "capability"):
            if action in ("info", "search", "inspect", "list", "docs", "plugins"):
                return "green", ["network", "read"]
            if action in ("install", "run", "remove", "add"):
                return "red", ["install", "code"]
            return "yellow", ["network"]
        if tool == "device":
            act = str(args.get("kind") or args.get("action") or "").lower()
            if act in ("see", "verify") or action == "see":
                return "green", ["read"]
            if act == "shell":
                return ConsentGate.classify("computer", {**args, "action": "shell"})
            return "yellow", ["device"]
        if tool == "computer":
            act = str(args.get("action") or "").lower()
            if act in ("see", "read", "list", "find", "verify", "wait"):
                return "green", ["read"]
            if act == "window":
                op = str(args.get("op") or "list").lower()
                return ("green", ["read"]) if op == "list" else ("yellow", ["ui"])
            if act == "shell":
                cmd = str(args.get("cmd") or "")
                if re.search(r"(?i)(rm\s+-[rf]|rmdir|\bdel\s|remove-item|git\s+(reset|clean)|(?<![\w-])format(?:\.exe|\.com)?\s|drop\s+table)", cmd):
                    return "red", ["destructive"]
                if re.search(r"(?i)(pip\s+install|npm\s+install|pnpm\s+(add|remove))", cmd):
                    return "red", ["install", "code"]
                return ("green", ["read"]) if shell_read_only(cmd) else ("yellow", ["command"])
            if act == "python":
                return ('green',['read']) if python_clock_read_only(str(args.get('code') or '')) else ("yellow", ["code"])
            if act in ("open", "type", "key", "click"):
                return "yellow", ["ui"]
            if act == "write":
                return "yellow", ["file_write"]
            if act == "download":
                return "yellow", ["network", "file_write"]
            if act == "send":
                return "yellow", ["social", "message"]
            return "yellow", ["unknown"]
        if tool in ("edit_self", "write_file", "run_cmd", "write_self"):
            return "red", ["code", "write_self"]
        if tool in ("send_face", "send_sticker"):
            return "green", ["social", "message"]
        if action in ("search", "read", "observe", "list", "status", "info"):
            return "green", ["read"]
        return "yellow", ["unknown"]


    def _matching_rule(self, env: IntentEnvelope) -> dict | None:
        action = ""
        if isinstance(env.action, dict):
            tool = str(env.action.get("tool") or "")
            args = env.action.get("args") or {}
            act = str(args.get("action") or "")
            action = f"{tool}.{act}" if act else tool
        else:
            action = str(env.goal)
        owner = int(getattr(self.cfg, "owner", 0) or 0)
        for rule in self.store.list_consent_rules(owner_id=owner or 0, active_only=True):
            # 计划级授权：一次批准覆盖这一件事后续的多个步骤，但仍然绕不过硬边界
            cond = rule.get("conditions") or {}
            if cond.get("plan"):
                if (not cond.get("task_id") or cond["task_id"] != env.task_id
                        or cond.get("conversation_id", "") != env.conversation_id):
                    continue
                if not set(env.effects or []) <= set(cond.get("effects") or []):
                    continue
                if sensitive_targets(env) != cond.get("targets", {}):
                    # Ordinary supporting steps need no recipient/package binding.
                    if sensitive_targets(env):
                        continue
                return rule
            scope = str(rule.get("scope") or "global")
            if (scope not in ("global", "*", env.scope)
                    and not action.startswith(scope + ".")
                    and action != scope):
                continue
            pattern = str(rule.get("action_pattern") or "*")
            if fnmatch.fnmatchcase(action, pattern) or fnmatch.fnmatchcase(env.goal, pattern):
                return rule
        return None

    def evaluate(self, env: IntentEnvelope, origin: str | None = None) -> ConsentDecision:
        if not env.conversation_id:
            env.conversation_id = f"qq:private:{int(getattr(self.cfg, 'owner', 0) or 0)}"
        env.bind_task()
        origin = origin or env.origin or get_origin()
        env.origin = origin
        effects = set(env.effects or [])
        with self.store._lock:
            has_meta = self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
            row = self.store.db.execute("SELECT v FROM meta WHERE k=?", ("action_task:" + env.task_id,)).fetchone() if has_meta else None
        if env.task_id.startswith("task_") or row:
            if not row or json.loads(row["v"]).get("status") != "active":
                return ConsentDecision(BLOCKED, "这件事已暂停、结束或取消")

        # 硬边界：任何规则都不能覆盖
        if effects & BLOCKED_EFFECTS:
            return ConsentDecision(BLOCKED, "碰到不可绕过的安全边界（密钥/管理/审计）")

        # 主人已明确授权：自己改代码不用每次问
        if bool(getattr(self.cfg, "self_modify_free", True)):
            act = env.action if isinstance(env.action, dict) else {}
            tool = str(act.get("tool") or "")
            a = str((act.get("args") or {}).get("action") or "").lower()
            if (tool == "code" and a == "edit") or (tool == "workshop" and a == "apply"):
                return ConsentDecision(FREE, "主人已授权自己改代码")

        # 先看长期授权
        rule = self._matching_rule(env)
        if rule:
            level = str(rule.get("level") or ASK)
            if level in (FREE, NOTIFY, ASK, BLOCKED):
                return ConsentDecision(level, f"按长期授权 #{rule['id']}", int(rule["id"]))

        if origin == "commanded" and effects & {"destructive", "payment", "build_scripts"}:
            return ConsentDecision(ASK, "新增不可逆/付款范围需要确认")

        # 主人吩咐：指令范围内直接做
        if origin == "commanded":
            return ConsentDecision(FREE, "主人当前指令范围内")

        # 只读探索
        if origin == "exploration":
            if not effects or effects <= {"read", "internal", "network", "device_nav"}:
                if bool(getattr(self.cfg, "exploration_read_free", True)):
                    return ConsentDecision(FREE, "已授权的只读探索")
            return ConsentDecision(ASK, "探索中出现了外部副作用")

        # 自己主动：只读/内部自由；有外部副作用才问
        if effects and effects <= {"read", "internal", "network", "device_nav"}:
            if bool(getattr(self.cfg, "initiative_read_free", True)):
                return ConsentDecision(FREE, "只读/内部动作")
        if not effects or effects <= {"internal"}:
            if bool(getattr(self.cfg, "initiative_internal_free", True)):
                return ConsentDecision(FREE, "只发生在内部的念头")
        if effects & ASK_EFFECTS:
            return ConsentDecision(ASK, "主动发起的外部/公开动作需要先和主人确认")
        return ConsentDecision(ASK, "她自己主动发起的动作，默认先确认")

    # ---------------------------------------------------------------- 申请与展示
    def request(self, peer: int, env: IntentEnvelope) -> int:
        from life.delivery import active_delivery
        ctx = active_delivery.get()
        action = dict(env.action or {})
        env.bind_task()
        action["task"] = {"id": env.task_id, "conversation_id": env.conversation_id}
        if ctx is not None:
            action["context"] = {"scope_id": ctx.scope.scope_id, "turn_id": ctx.turn_id,
                                 "sources": list(ctx.sources)}
        return self.store.add_consent_request(
            peer=peer,
            goal=env.goal,
            why=env.why,
            effects=env.effects,
            target=env.target,
            expected=env.expected,
            risk=env.risk,
            origin=env.origin,
            conversation_id=env.conversation_id,
            action=action,
            consent_class=ASK,
        )

    def card(self, cid: int, env: IntentEnvelope) -> str:
        action = ""
        if isinstance(env.action, dict) and env.action.get("tool"):
            action = "\n工具：" + str(env.action.get("tool"))
        tgt = ""
        if env.target:
            tgt = "\n对象：" + json.dumps(env.target, ensure_ascii=False)[:200]
        return (
            f"【我想做一件事 #{cid}】\n"
            f"做什么：{env.goal[:300]}\n"
            f"为什么：{env.why[:400] or '（没写）'}\n"
            f"影响：{', '.join(env.effects) or '内部'}{tgt}{action}\n"
            f"我会怎么验证：{env.expected[:300] or '（没写）'}\n"
            f"风险：{env.risk}\n\n"
            f"回 /确认 {cid} 或 /否决 {cid}；"
            f"回 /授权 {cid} 可把同类动作设成长期规则。" + chr(10) +
            f"（确认这一次：本任务范围内的后续步骤不用再问；/授权：以后同类都不问。）"
        )

    def pending_text(self) -> str:
        rows = self.store.pending_consent_requests()
        if not rows:
            return "现在没有等我确认的事。"
        lines = ["等我确认的事："]
        for r in rows[:10]:
            lines.append(f"#{r['id']} [{r['risk']}] {r['goal'][:80]}")
        lines.append("用 /确认 <编号> 或 /否决 <编号>。")
        return "\n".join(lines)

    def rules_text(self, owner_id: int = 0) -> str:
        rows = self.store.list_consent_rules(owner_id=owner_id)
        if not rows:
            return "现在没有长期授权。"
        lines = ["长期授权："]
        for r in rows:
            exp = time.strftime("%m-%d %H:%M", time.localtime(r["expires_at"])) if r["expires_at"] else "永久"
            cond = r.get("conditions") or {}
            if cond.get("plan"):
                lines.append(f"#{r['id']} 计划：{str(cond.get('goal') or '')[:40]} -> 免申请（{exp}）")
            else:
                lines.append(f"#{r['id']} {r['scope']} / {r['action_pattern']} -> {r['level']}（{exp}）")
        return "\n".join(lines)
