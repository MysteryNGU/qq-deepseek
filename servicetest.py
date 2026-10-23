#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'servicetest.py —— 不用 QQ、不用 API key，验证"她自己长"的那部分。\n\n测的是：想法/念想/经历/方法 四张表，风险分级，预算，审批，学习闭环，\n以及和 bot.py 的接线。跑法：\n\n    .\\.venv\\Scripts\\python.exe servicetest.py\n'

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


# ---------------------------------------------------------------- 假件


class FakeBrain:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.msgs: list = []
        self._facts: list[str] = []

    def add_msg(self, peer, role, content):
        self.msgs.append((peer, role, content, int(time.time())))

    def last_msg_ts(self, peer):
        for p, r, c, ts in reversed(self.msgs):
            if p == peer:
                return ts
        return 0

    def facts(self, peer, n=40):
        return self._facts[-n:]

    def add_fact(self, peer, text):
        self._facts.append(text)

    def get_kv(self, k):
        return self.kv.get(k)

    def set_kv(self, k, v):
        self.kv[k] = v


class FakeLLM:
    def __init__(self, replies: list[str] | None = None):
        self.replies = list(replies or [])
        self.seen: list[tuple[str, str]] = []

    async def chat(self, messages, temperature=None, max_tokens=None, tag=""):
        self.seen.append((tag, messages[-1]["content"] if messages else ""))
        if self.replies:
            return self.replies.pop(0)
        return "嗯"

    async def chat_msg(self, messages, tools=None, tag="", **kw):
        class M:
            content = "做完了\n结果：成功"
            tool_calls = None
        return M()


class _Call:
    def __init__(self, name: str, args: dict, n: int = 0):
        self.id = f"call_{n}"
        self.function = SimpleNamespace(name=name,
                                        arguments=json.dumps(args, ensure_ascii=False))


class _Msg:
    def __init__(self, content: str, calls: list | None = None):
        self.content = content
        self.tool_calls = calls or None


class ScriptedLLM(FakeLLM):
    """chat 按 replies 顺序回；chat_msg 按 steps 顺序回。"""

    def __init__(self, replies: list[str] | None = None,
                 steps: list[tuple[str, list]] | None = None):
        super().__init__(replies)
        self.steps = list(steps or [])

    async def chat_msg(self, messages, tools=None, tag="", **kw):
        if self.steps:
            content, calls = self.steps.pop(0)
        else:
            content, calls = ("结果：成功", [])
        return _Msg(content, calls)


class FakeHost:
    def __init__(self, root: Path, cfg, llm):
        self.cfg = cfg
        self.llm = llm
        self.brain = FakeBrain()
        self.root = root
        self.sent: list[str] = []

    async def _send(self, target, text):
        self.sent.append(text)
        return True

    def _mind(self, tag, text):
        pass

    # 通道会问宿主"这句要不要深想"，测试里的假宿主也得会
    def is_command(self, text):
        import bot as _b
        return _b.Bot.is_command(text)

    def route(self, text):
        import bot as _b
        shell = _b.Bot.__new__(_b.Bot)      # 借一个空的 Bot 壳，只为拿到类上的正则
        shell.cfg = self.cfg
        return _b.Bot.route(shell, text)

    async def understand(self, text, ctx=""):
        """测试里不发真请求：用 _intent 字段指定这一次的意图。"""
        it = {"kind": "chat", "needs_action": False, "scene": "daily",
              "complexity": 1, "thinking": "fast", "goal": "", "skill_hints": []}
        it.update(getattr(self, "_intent", {}) or {})
        if "这是我准备执行的行动" in ctx or "这是已决定执行的具体行动" in ctx:
            it.update(goal=text, announcement="我去看看这件事的具体情况。")
        return it

    def _mark_talk(self, peer):
        pass

    def _grow_bond(self, peer, delta, rel="owner"):
        pass

    def _react_desires(self, peer, text):
        pass

    async def _update_emotions(self, peer, text, rel="owner"):
        return {}

    def _persona_text(self):
        return ("你叫小鱼，一条被捡回来的小鱼。\n"
                "口癖：句尾一个喵。\n"
                "说话的样子：\n「在的喵」\n「别熬夜了喵」\n")


class FakeDevice:
    name = "phone"

    async def see(self):
        from life.services.truth import ok
        return ok("前台：com.tencent.mobileqq\n界面：发送 可点", image="data:image/jpeg;base64,AA")

    async def act(self, kind, args):
        from life.services.truth import ok, fail
        if kind == "type":
            return ok("打进去了（读回确认）")
        if kind == "tap" and args.get("text") == "不存在":
            return fail("界面上没找到「不存在」")
        return ok(f"{kind} 完成，界面变了")

    async def verify(self, kind, expect, args):
        from life.services.truth import ok
        return ok(f"界面上出现了「{expect}」")


def cfg(**kw):
    base = dict(
        evolution=True, owner=1, phone=True, text_only=False, ask_before_act=True,
        evo_think_seconds=600, evo_act_per_hour=10, evo_act_per_day=10,
        evo_minutes_per_day=10, evo_energy=0.4, evo_intent_max=6,
        evo_thought_max=40, evo_quiet_start="00:00", evo_quiet_end="00:00",
        evo_require_approval=True, idle_quiet=5, persona="persona.md",
        tools=True, screen=True, web=True, emotions=True, bond=True, drift=True,
        iq=True, agenda=True, model="deepseek-flash",
        bond_max_nonowner=70, bond_decay_per_day=2, drift_cap=25, drift_daily=0.8,
        iq_base=60, self_review=True, self_review_days=7, autonomous=False,
        history_turns=24, compact_after=44, name="小鱼", temperature=0.95,
        max_tokens=400, channel="ws", ws_url="ws://127.0.0.1:3001",
        mistresses=[], friends=[], allow_groups=[], allow_all_private=False,
        reply_private=True, stranger_chat=False, stranger_reply="ignore",
        stranger_line="小鱼只跟主人说话喵", auto_friend_on_accept=True,
        auto_accept_friend=False, auto_accept_group=False, gui_chat="", gui_poll=3.0,
        vision_ui=True, night_nudge=False, night_nudge_at="00:40", nudge_after="22:30",
        diary=True, diary_at="23:50", roam=True, drive=True, proactive=False,
        proactive_max=2, mood_chaos=0.0, news_urls=[], allow_users=[],
        typing_cps=8, min_gap=0.4, per_minute=4,
        voice_reply="off", voice_name="x", voice_rate="+0%", voice_pitch="+0Hz",
        voice_volume="+0%", voice_cooldown=180, activity_floor=8, idle_minutes=0,
        urge_threshold=1.5, db="data/brain.db", api_key="", base_url="x",
        thinking=False, auto_accept=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def make_evo(tmp: Path, llm=None):
    from life.services import ServiceHub
    host = FakeHost(tmp, cfg(), llm or FakeLLM())
    e = ServiceHub(host, root=tmp)
    e.device = lambda name: FakeDevice()
    return e, host


# ---------------------------------------------------------------- 测试


def test_store(tmp: Path) -> None:
    from life.services.store import Store
    s = Store(tmp / "data" / 'life.db')
    t = s.add_thought(1, "wonder", "他今天在写什么")
    check("记想法", t > 0 and s.recent_thoughts(1)[-1]["text"].startswith("他今天"))
    i = s.add_intent(1, "我想学会发说说", "因为上次没发出去", "mastery", "空间里有一条", 0.7)
    check("记念想", s.get_intent(i)["origin"] == "mastery")
    s.update_intent(i, status="active", attempts=2)
    check("改念想", s.get_intent(i)["status"] == "active")
    check("活跃念想", any(x["id"] == i for x in s.active_intents()))
    s.add_episode(1, "点了发送但没发出去", outcome="fail", lesson="先确认输入框有字")
    check("记经历", s.recent_episodes(1)[-1]["lesson"].startswith("先确认"))
    tid = s.add_trace(1, i, [{"tool": "device", "ok": True}], True, 120)
    tr = s.get_trace(tid)
    check("记轨迹", tr and tr["steps"][0]["tool"] == "device")
    aid = s.add_approval(1, {"tool": "dsh", "args": {}}, "red", "装插件")
    check("待批", s.get_approval(aid)["status"] == "pending")
    s.decide_approval(aid, True)
    check("批了", s.get_approval(aid)["status"] == "approved")
    s.bump_proc("发说说", True)
    s.bump_proc("发说说", True)
    st = s.proc_stat("发说说")
    check("方法成绩", st["success"] == 2 and st["last_verified"] > 0)
    s.close()


def test_procedures(tmp: Path) -> None:
    from life.services import procedures as P
    d = tmp / "skills"
    d.mkdir(parents=True, exist_ok=True)
    (d / "发说说.md").write_text(
        "---\nname: 发说说\nwhen: 想发空间说说\npreconditions: QQ 已登录\n"
        "checkpoints: 列表里能看到\nsuccess: 1\nfail: 0\nlast_verified: 0\n"
        "version: 1\nauthor: owner\nstatus: active\n---\n"
        "## 步骤\n1. see\n2. tap 动态\n3. type 正文\n4. verify 正文\n"
        "## 失败过\n- 键盘顶出屏幕\n", "utf-8")
    (d / "上网.md").write_text(
        "---\nname: 上网\nwhen: 查资料\nstatus: active\n---\n## 步骤\n1. search 关键词\n", "utf-8")
    book = P.ProcedureBook(d)
    p = book.load("发说说")
    check("读方法", p and p.name == "发说说" and len(p.steps) == 4)
    check("解析失败过", p and p.failure_modes and "键盘" in p.failure_modes[0])
    check("校验通过", p is not None and not P.validate(p))
    bad = P.Procedure(slug="x", name="x", when="x", checkpoints="x",
                      steps=["乱点一下"], status="draft")
    check("坏方法被拒", any("不是原语" in x for x in P.validate(bad)))
    found = book.find("想在空间发一条说说")
    check("检索命中", bool(found) and found[0].slug == "发说说")
    p.status = "retired"
    book.save(p)
    check("淘汰后不出现", all(x.slug != "发说说" for x in book.all()))
    check("retired 可查", book.load("发说说") is not None)


def test_risk() -> None:
    from life.services import risk
    check("看是绿的", risk.classify("see", {})[0] == "green")
    check("点是黄的", risk.classify("tap", {"text": "发送"})[0] == "yellow")
    check("删是红的", risk.classify("shell", {"cmd": "Remove-Item -Recurse C:\\x"})[0] == "red")
    check("装插件是红的", risk.classify("install", {"pkg": "x"})[0] == "red")
    check("只读命令是黄", risk.classify("shell", {"cmd": "Get-ChildItem"})[0] == "yellow")


def test_budget(tmp: Path) -> None:
    from life.services import ServiceHub
    from life.services.budget import Budget
    from life.services.store import Store
    s = Store(tmp / "data" / "budget.db")
    b = Budget(s, cfg(evo_act_per_day=2, evo_act_per_hour=10))
    check("一开始能动手", b.allow_act()[0])
    for _ in range(2):
        s.add_trace(1, 0, [], True, 1000)
    ok, why = b.allow_act()
    check("到上限就不动", not ok and "上限" in why)
    quiet = Budget(s, cfg(evo_quiet_start="03:00", evo_quiet_end="04:00"))
    check("安静时段判断存在", isinstance(quiet.in_quiet_hours(), bool))
    s.close()


def test_learn(tmp: Path) -> None:
    from life.services import procedures as P
    from life.services.learn import Learner
    from life.services.store import Store
    d = tmp / "skills2"
    d.mkdir(parents=True, exist_ok=True)
    s = Store(tmp / "data" / "learn.db")
    book = P.ProcedureBook(d)
    learn = Learner(s, book)
    llm = FakeLLM([json.dumps({
        "name": "打开B站首页",
        "when": "想看看B站有什么",
        "preconditions": "手机连着",
        "checkpoints": "前台是 tv.danmaku.bili",
        "steps": ["see", "launch tv.danmaku.bili", "verify tv.danmaku.bili"],
        "failure_modes": ["首页 dump 读不到字，用 see 看"],
    }, ensure_ascii=False)])
    print("  ..  " + learn.start(1, "打开B站", "想看他昨天说的视频"))
    learn.note(1, "先按 home 回桌面，成功")
    learn.note(1, "launch tv.danmaku.bili，前台变成了 bili")
    text = asyncio.run(learn.finish(1, llm))
    check("学成了草稿", "草稿" in text and (d / "打开B站首页.md").exists(), text)
    saved = book.load("打开B站首页")
    check("草稿状态", saved is not None and saved.status == "draft")
    check("草稿带验证点", saved is not None and "bili" in saved.checkpoints)
    llm2 = FakeLLM([json.dumps({"name": "坏方法", "when": "x", "checkpoints": "x",
                                "steps": ["随手点两下"]}, ensure_ascii=False)])
    learn.start(1, "坏方法", "试试")
    learn.note(1, "a"); learn.note(1, "b")
    text2 = asyncio.run(learn.finish(1, llm2))
    check("坏步骤被拒", "还不能用" in text2, text2)
    s.close()


def test_evo_tools(tmp: Path) -> None:
    from life.services import tools
    services, host = make_evo(tmp)
    blocks, used, meta = asyncio.run(tools.dispatch(
        services, "intent", {"action": "add", "text": "想学会发说说",
                        "why": "上次没发出去", "origin": "mastery", "energy": 0.8}, None, 1))
    check("intent 工具", ("记下了" in blocks[0]["text"] or "已经是念想" in blocks[0]["text"]) and meta["ok"])
    iid = services.store.list_intents("", 1)[0]["id"]
    blocks, _u, _m = asyncio.run(tools.dispatch(
        services, "intent", {"action": "update", "id": iid, "status": "done"}, None, 1))
    check("intent 改状态", services.store.get_intent(iid)["status"] == "done")

    blocks, _u, _m = asyncio.run(tools.dispatch(
        services, "device", {"device": "phone", "action": "see"}, None, 1))
    check("device see 带图", blocks[0]["type"] == "text" and
          any(b["type"] == "image_url" for b in blocks))

    blocks, _u, meta = asyncio.run(tools.dispatch(
        services, "device", {"device": "phone", "action": "act", "kind": "type", "text": "你好"}, None, 1))
    check("device 打字成功", meta["ok"] and "读回确认" in blocks[0]["text"])

    blocks, _u, _m = asyncio.run(tools.dispatch(
        services, "skill", {"action": "list"}, None, 1))
    check("skill list 不炸", blocks[0]["type"] == "text")
    services.close()


def test_approval(tmp: Path) -> None:
    services, host = make_evo(tmp)
    text, meta = asyncio.run(services.dispatch_text(
        "device", {"device": "phone", "action": "act", "kind": "shell",
                   "cmd": "Remove-Item -Recurse C:\\nope"}, ("private", 1), 1))
    check("红色动作先报备", bool(meta.get("awaiting")) and bool(host.sent)
          and "要你点头" not in (host.sent[0] if host.sent else ""), text)
    aid = services.store.pending_consent_requests()[0]["id"]
    out = asyncio.run(services.approve_consent(aid, 1))
    check("批准后执行", "弄好了" in out, out)
    check("批完不重复", "没有这个待确认" in services.deny_consent(aid))
    services.close()


def test_think() -> None:
    from life.services.think import Cognition
    from life.services.core import ServiceHub
    brief = ServiceHub._brief_result("很长的结果第一行" * 50 + chr(10) + "第二行", {"ok": True})
    check("审批结果只回一句", chr(10) not in brief and len(brief) <= 120, brief)
    check("空口成功不算成功", not Cognition._judge("结果：成功", []))
    check("有证据的成功判定",
          Cognition._judge("结果：成功", [{"tool": "device", "ok": True, "verified": True, "evidence": "目标已出现"}]))
    check("失败判定", not Cognition._judge("结果：卡住", [{"tool": "device", "ok": True}]))
    check("点击成功不等于目标完成", not Cognition._judge("结果：成功", [{"tool": "device", "ok": True}]))
    check("无结论看验证", Cognition._judge("", [{"tool": "device", "ok": True, "verified": True, "evidence": "目标已出现"}]))


def test_bot_wiring(tmp: Path) -> None:
    import bot as B
    from life.services import ServiceHub
    cfg2 = B.Config()
    cfg2.evolution = True
    cfg2.owner = 1
    cfg2.persona = "persona.md"
    brain = B.Brain(tmp / "brain.db")
    b = B.Bot(cfg2, brain, FakeLLM())
    b.services = ServiceHub(b, root=tmp)
    names = {t["function"]["name"] for t in b._active_tools()}
    check("工具进了清单", {"device", "intent", "skill", "learn", "dsh", "web"} <= names)
    sysmsg = b._system(1)
    check("提示词里有她自己", "你自己" in sysmsg and
          ("挂着" in sysmsg or "念想" in sysmsg),
          sysmsg[-200:])
    check("提示词没炸", len(sysmsg) > 500)
    call = B._FakeCall("intent", {"action": "thought", "text": "今天想安静一会儿"})
    async def understand_action(*args):
        return {"goal": "记录念头", "announcement": "记录当前念头"}
    async def send_notice(*args):
        return True
    b.understand = understand_action
    b._send = send_notice
    blocks, used = asyncio.run(b._run_tool_calls([call], ("private", 1), 1))
    check("工具分发到 evo", "记下了" in blocks[0]["text"], str(blocks)[:200])
    cfg2.evolution = False
    names2 = {t["function"]["name"] for t in b._active_tools()}
    check("关掉就不给她", "device" not in names2)
    b.services.close()
    brain.close()


def test_tick(tmp: Path) -> None:
    """完整一条链：念头 → 念想 → 动手 → 反思。"""
    thought_text = "他好像在弄一个我看不懂的东西喵"
    intent_json = json.dumps({
        "text": "我想看看他在写什么",
        "why": "他刚才说在改一个报错",
        "origin": "curiosity",
        "expected": "屏幕上能看到他在写的东西",
        "energy": 0.8,
    }, ensure_ascii=False)
    reflect_text = "我做成了看一眼\n以后先看看再开口"
    llm = ScriptedLLM(
        replies=[thought_text, intent_json, reflect_text],
        steps=[("", [_Call("device", {"device": "phone", "action": "see"})]),
               ("看的这一眼够了\n结果：成功", [])])
    services, host = make_evo(tmp, llm)
    host.brain._facts.append("他今天在改一个报错")
    # 先把库里遗留的念想清掉：现在的 tick 会优先去做挂着的事，
    # 不清理就测不到"生成新念头"这一段
    for it in services.store.active_intents(20):
        services.store.update_intent(int(it["id"]), status="done")
    out = asyncio.run(services.cog.tick(force=True))
    check("慢思考产出了念头", out == thought_text, out)
    thoughts = [t["text"] for t in services.store.recent_thoughts(1, 10)]
    check("念头进库", thought_text in thoughts)
    intents = services.store.list_intents("", 10)
    check("念想进库", any(i["text"] == "我想看看他在写什么" for i in intents), str(intents))
    done = [i for i in intents if i["status"] == "done"]
    check("动手做完了", bool(done), str(intents))
    check("留了经历", bool(services.store.recent_episodes(1, 5)))
    check("留了轨迹", bool(services.store.traces_since(0)))
    check("预算记了一次", services.budget.actions_today() >= 1)
    services.close()


def test_caps(tmp: Path) -> None:
    from life.services import caps as C
    cat = C.Catalog(tmp / "data" / "cache" / "cat.json")
    cat.fetch = lambda url: {"format": 1, "plugins": [
        {"name": "dsh-memory", "title": "项目记忆", "description": {"zh": "跨会话记住项目"},
         "metadata": {"category": "memory", "stars": 120},
         "homepage": "https://example.com/memory"},
        {"name": "dsh-theme-x", "title": "主题", "description": {"zh": "换个颜色"},
         "metadata": {"category": "theme", "stars": 3}},
    ]}
    rows = cat.search("记忆")
    check("插件目录能搜", bool(rows) and rows[0]["name"] == "dsh-memory", str(rows))
    check("插件目录带安装命令", "add dsh-memory" in rows[0]["install"])
    rows2 = cat.search("完全不存在的东西")
    check("搜不到就是空", rows2 == [])

    book = C.Caps(tmp)
    ok, _why = book.install_allowed("pip install requests")
    check("pip 允许", ok)
    bad, _why = book.install_allowed("rm -rf /")
    check("危险安装拒绝", not bad)
    chain, _why = book.install_allowed("pip install x; Remove-Item -Recurse C:\\")
    check("串联安装拒绝", not chain)
    cap = C.Capability(slug="hello", name="hello", kind="cli",
                       description="打印一句话",
                       invoke=[sys.executable, "-c", "print('hi')"])
    book.save(cap)
    out = book.invoke("hello", [], "", "")
    check("清单能调用", "退出码 0" in out and "hi" in out, out)
    book.bump("hello", True)
    check("成绩记上", book.get("hello").success == 1)
    check("清单在列表里", any(c.slug == "hello" for c in book.list()))
    check("删除清单", book.remove("hello") and book.get("hello") is None)


def test_capability_tool(tmp: Path) -> None:
    from life.services import tools
    services, host = make_evo(tmp)
    services.catalog.fetch = lambda url: {"format": 1, "plugins": [
        {"name": "dsh-memory", "title": "项目记忆",
         "description": {"zh": "跨会话记住项目"}, "metadata": {"stars": 99}},
    ]}
    blocks, _u, _m = asyncio.run(tools.dispatch(
        services, "capability", {"action": "search", "query": "记忆"}, None, 1))
    check("工具能搜插件", "dsh-memory" in blocks[0]["text"], blocks[0]["text"][:200])

    blocks, _u, _m = asyncio.run(tools.dispatch(
        services, "capability", {"action": "add", "slug": "hi", "name": "hi",
                            "cap_kind": "cli",
                            "invoke": [sys.executable, "-c", "print('yo')"]}, None, 1))
    check("工具能写清单", (tmp / "data" / "caps" / "hi" / "manifest.json").exists())

    blocks, _u, meta = asyncio.run(tools.dispatch(
        services, "capability", {"action": "invoke", "slug": "hi"}, None, 1))
    check("工具能调用", meta["ok"] and "yo" in blocks[0]["text"], blocks[0]["text"][:200])

    from life.services.ctx import current_origin
    token = current_origin.set("initiative")
    blocks, _u, meta = asyncio.run(tools.dispatch(
        services, "capability", {"action": "install", "slug": "hi"}, None, 1))
    current_origin.reset(token)
    check("装之前先报备", meta.get("awaiting"), blocks[0]["text"][:200])
    for pending in services.store.pending_consent_requests():
        services.deny_consent(pending["id"])
    services.close()


def test_dsh(tmp: Path) -> None:
    from life.services import dsh
    info = dsh.info()
    check("看得见 DSH 环境", "DSH_HOME" in info and "profiles" in info, info[:120])
    out = dsh.run(["--help"])
    check("没有命令时给装法", "没有 dsh 命令" in out or "退出码" in out, out[:120])
    services, host = make_evo(tmp)
    from life.services.ctx import current_origin
    token = current_origin.set("initiative")
    text, meta = asyncio.run(services.dispatch_text(
        "dsh", {"action": "run", "args": ["--help"]}, ("private", 1), 1))
    current_origin.reset(token)
    check("跑 dsh 先报备", bool(meta.get("awaiting")), text[:120])
    for pending in services.store.pending_consent_requests():
        services.deny_consent(pending["id"])
    services.close()


def test_channel(tmp: Path) -> None:
    """通道是 agent 循环：看 → 自己切会话 → 回 → 验证。"""
    from life.services.channel import AgenticGui

    state = {"chat": False}
    rows = [{"name": "樱花樱花想见你༻꧂", "preview": "在吗", "unread": 1,
             "bounds": (0, 201, 900, 309)}]
    chat_obs = {"image": "", "app": "com.tencent.mobileqq", "rows": rows,
                "nodes": ["樱花樱花想见你༻꧂  (97,47)  TextView", "在吗  (78,300)  TextView"],
                "msgs": [{"text": "在吗", "x": 78, "y": 300, "side": "them"}]}
    replied: list[str] = []

    async def dispatch(name, args, target, peer):
        if name == "device" and args.get("action") == "act":
            text = str(args.get("text") or "")
            if text == "消息":
                state["chat"] = False
            elif "樱花" in text:
                state["chat"] = True
        return f"（{name} 做完了）", {"ok": True}

    async def reply(text):
        if not state["chat"]:
            return False, "不在目标会话"
        replied.append(text)
        return True, "发出去了"

    llm = ScriptedLLM(steps=[
        ("", [_Call("device", {"action": "act", "kind": "tap", "text": "消息"})]),
        ("", [_Call("device", {"action": "act", "kind": "tap", "text": "樱花樱花想见你"})]),
        ("", [_Call("reply", {"text": "在的喵"})]),
        ("好了", []),
    ])
    host = FakeHost(tmp, cfg(), llm)
    host._intent = {"needs_action": True, "kind": "task", "thinking": "fast"}
    host._sent_log = []

    class FakeEvo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        async def dispatch_text(self, name, args, target, peer):
            return await dispatch(name, args, target, peer)

        def log(self, msg):
            pass

    ch = AgenticGui(FakeEvo(), observe=lambda: chat_obs, reply=reply, dispatch=dispatch)
    asyncio.run(ch.handle(chat_obs, [{"text": "在吗", "x": 78, "y": 300, "side": "them"}]))
    check("通道会自己切会话再回", replied == ["在的喵"] and state["chat"], str(replied))
    check("通道把新消息记成已读", "在吗" in ch.seen)
    ch.seen.append("在的喵")
    check("自己说过的话不算新消息",
          not ch._fresh({"text": "在的喵", "side": "them"}))
    check("自己发的不算新消息",
          not ch._fresh({"text": "新话", "side": "me"}))

    # 模型没有调用 reply，只在正文里说了话：也必须发出去，不能当成“想了想”丢掉
    llm_direct = ScriptedLLM(steps=[("直接说出来的话", [])])
    host_direct = FakeHost(tmp, cfg(), llm_direct)
    host_direct._sent_log = []

    class EvoDirect(FakeEvo):
        def __init__(self):
            super().__init__()
            self.llm = llm_direct
            self.host = host_direct

    replied_direct: list[str] = []

    async def reply_direct(text):
        replied_direct.append(text)
        return True, "发出去了"

    ch_direct = AgenticGui(EvoDirect(), observe=lambda: chat_obs,
                           reply=reply_direct, dispatch=dispatch)
    asyncio.run(ch_direct.handle(chat_obs, [{"text": "在吗", "side": "them"}]))
    check("正文没调 reply 也会发出去", replied_direct == ["直接说出来的话"], str(replied_direct))

    # 通道的话来自 skills/通道.md（她能改），不是写死在 Python 里
    (tmp / "skills").mkdir(parents=True, exist_ok=True)
    (tmp / "skills" / "通道.md").write_text(
        "---\nname: 通道\n---\n# 我的写法\n先看标题，再回。", "utf-8")
    ch3 = AgenticGui(FakeEvo(), observe=lambda: chat_obs, reply=reply, dispatch=dispatch)
    check("通道的话来自文件", "我的写法" in ch3.rules())

    # 多看视觉：look 会再观察一次，并把新截图放回对话
    looks = {"n": 0}

    def obs_counting():
        looks["n"] += 1
        return chat_obs

    llm_look = ScriptedLLM(steps=[
        ("", [_Call("look", {})]),
        ("", [_Call("reply", {"text": "多看一眼再回"})]),
    ])
    host3 = FakeHost(tmp, cfg(), llm_look)
    host3._sent_log = []

    class EvoLook(FakeEvo):
        def __init__(self):
            super().__init__()
            self.llm = llm_look
            self.host = host3

    replied2: list[str] = []

    async def reply2(text):
        replied2.append(text)
        return True, "发出去了"

    ch4 = AgenticGui(EvoLook(), observe=obs_counting, reply=reply2, dispatch=dispatch)
    asyncio.run(ch4.handle(chat_obs, [{"text": "在吗", "side": "them"}]))
    check("她可以多看视觉", looks["n"] >= 1 and replied2 == ["多看一眼再回"],
          f"{looks['n']} {replied2}")

    # 不再"6 步就停"：8 步之后还能成
    long_steps = [("", [_Call("device", {"action": "act", "kind": "tap",
                                        "text": f"第{i}下"})]) for i in range(8)]
    long_steps.append(("", [_Call("reply", {"text": "八步之后终于回了"})]))
    llm_long = ScriptedLLM(steps=long_steps)
    host4 = FakeHost(tmp, cfg(), llm_long)
    host4._sent_log = []

    class EvoLong(FakeEvo):
        def __init__(self):
            super().__init__()
            self.llm = llm_long
            self.host = host4

    replied3: list[str] = []

    async def reply3(text):
        replied3.append(text)
        return True, "发出去了"

    ch5 = AgenticGui(EvoLong(), observe=lambda: chat_obs, reply=reply3, dispatch=dispatch)
    asyncio.run(ch5.handle(chat_obs, [{"text": "在吗", "side": "them"}]))
    check("八步之后还能成", replied3 == ["八步之后终于回了"], str(replied3))

    # 原地重复同一个动作三次 → 才提前停
    same = [("", [_Call("device", {"action": "act", "kind": "tap", "text": "同一下"})])
            for _ in range(3)]
    llm_same = ScriptedLLM(steps=same)
    host5 = FakeHost(tmp, cfg(), llm_same)
    host5._sent_log = []

    class EvoSame(FakeEvo):
        def __init__(self):
            super().__init__()
            self.llm = llm_same
            self.host = host5

    ch6 = AgenticGui(EvoSame(), observe=lambda: chat_obs,
                     reply=lambda text: (True, "ok"), dispatch=dispatch)
    ok_same = asyncio.run(ch6._run_agent(chat_obs, [{"text": "在吗", "side": "them"}], ""))
    check("重复三次才停", ok_same is False)


def test_channel_promise(tmp: Path) -> None:
    """答应了他就真做：光说"这就去"不许当成回完了。"""
    from life.services.channel import AgenticGui
    from bot import Bot

    rows = [{"name": "樱花樱花想见你༻꧂", "preview": "我要你发个说说", "unread": 1,
             "bounds": (0, 201, 900, 309)}]
    obs = {"image": "", "app": "com.tencent.mobileqq", "rows": rows,
           "nodes": ["樱花樱花想见你༻꧂  (97,47)  TextView"],
           "msgs": [{"text": "我要你发个说说", "x": 78, "y": 300, "side": "them"}]}
    sent: list[str] = []
    did: list[str] = []
    owed: list[str] = []

    async def dispatch(name, args, target, peer):
        did.append(f"{name}.{args.get('action')}")
        return "（发表✓；回空间确认✓）", {"ok": True}

    async def reply(text):
        sent.append(text)
        return True, "发出去了"

    llm = ScriptedLLM(steps=[
        ("", [_Call("reply", {"text": "好呀我这就去发喵"})]),
        ("", [_Call("device", {"action": "act", "kind": "tap", "text": "动态"})]),
        ("", [_Call("device", {"action": "act", "kind": "type", "text": "今天也好想你"})]),
        ("", [_Call("reply", {"text": "发出去啦，你去看空间"})]),
    ])
    host = FakeHost(tmp, cfg(), llm)
    host._intent = {"needs_action": True, "kind": "task", "thinking": "fast"}
    host._sent_log = []
    host.is_command = Bot.is_command

    class Store:
        def add_intent(self, *a, **k):
            owed.append(str(a[1]))

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp
            self.store = Store()

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def prompt_block(self, peer):
            return "- 先做后说\n"

        def log(self, msg):
            pass

        async def dispatch_text(self, name, args, target, peer):
            return await dispatch(name, args, target, peer)

    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply, dispatch=dispatch)
    asyncio.run(ch._run_agent(obs, [{"text": "我要你发个说说", "side": "them"}], ""))
    # 现在的契约：第一句是"应声"，允许发出去；然后必须真动手；最后交差
    check("先发执行前通知", host.sent and "我去看看这件事的具体情况。" in host.sent[0], str(host.sent))
    check("应完声她自己一步步动手（不是调函数）",
          did[:2] == ["device.act", "device.act"], str(did))
    check("最后交差", sent and sent[-1] == "发出去啦，你去看空间", str(sent))

    # 他只是闲聊，她说了句"等下我看看"——不该被逼着动工具
    sent.clear()
    llm2 = ScriptedLLM(steps=[("", [_Call("reply", {"text": "等下我看看喵"})])])
    host2 = FakeHost(tmp, cfg(), llm2)
    host2._sent_log = []
    host2.is_command = Bot.is_command

    class Evo2(Evo):
        def __init__(self):
            super().__init__()
            self.host = host2
            self.llm = llm2

    ch2 = AgenticGui(Evo2(), observe=lambda: obs, reply=reply, dispatch=dispatch)
    asyncio.run(ch2._run_agent(obs, [{"text": "在吗", "side": "them"}], ""))
    check("闲聊里不算空头承诺", sent == ["等下我看看喵"], str(sent))

    # 光"看了一眼"不算做过：眼睛不是手，看完了还得真动手
    sent.clear()
    llm3 = ScriptedLLM(steps=[
        ("", [_Call("device", {"action": "see"})]),
        ("", [_Call("reply", {"text": "好呀我这就去发喵"})]),
        ("", [_Call("reply", {"text": "发不了喵，空间打不开"})]),
    ])
    host3 = FakeHost(tmp, cfg(), llm3)
    host3._sent_log = []
    host3._intent = {"needs_action": True, "kind": "task", "thinking": "fast"}
    host3.is_command = Bot.is_command

    class Evo3(Evo):
        def __init__(self):
            super().__init__()
            self.host = host3
            self.llm = llm3

    ch3 = AgenticGui(Evo3(), observe=lambda: obs, reply=reply, dispatch=dispatch)
    asyncio.run(ch3._run_agent(obs, [{"text": "我要你发个说说", "side": "them"}], ""))
    # 只读动作不算"动手"——这条由 TurnState 直接覆盖
    from life.services.turn import TurnState
    s_see = TurnState(needs_action=True)
    s_see.note_sent(True)
    check("只是看了一眼，不许收工", s_see.reply_decision() == "block")
    s_see.note_action()
    check("真动手之后才许交差", s_see.reply_decision() == "allow_report")
    check("看/读不算副作用",
          not AgenticGui._is_side_effect("device", {"action": "see"})
          and not AgenticGui._is_side_effect("qq", {"action": "status"})
          and AgenticGui._is_side_effect("qq", {"action": "send"}))


def test_no_post_moment(tmp: Path) -> None:
    """发说说不能是"调一个函数"：工具表里就不该有这个动作。"""
    import phone
    from life.services import tools as service_tools

    check("phone 里没有 post_moment 这个函数", not hasattr(phone, "post_moment"))
    qq = next(t for t in service_tools.SCHEMAS
              if t["function"]["name"] == "qq")
    acts = qq["function"]["parameters"]["properties"]["action"]["enum"]
    check("qq 工具里没有发说说动作",
          "post_moment" not in acts and "send" in acts, str(acts))
    check("发给她的方法文件还在", (ROOT / "skills" / "发说说.md").exists())


def test_channel_history(tmp: Path) -> None:
    """漏掉的历史消息要补，不能一启动就全标已读。"""
    from life.services.channel import AgenticGui

    msgs = [
        {"text": "晚安", "side": "me"},
        {"text": "你今天忙吗", "side": "them"},
        {"text": "我等你好久了", "side": "them"},
    ]
    tail = AgenticGui._unanswered_tail(msgs)
    check("找得到没回的历史消息", [m["text"] for m in tail] == ["你今天忙吗", "我等你好久了"],
          str(tail))
    check("回完的旧消息不算欠账",
          AgenticGui._unanswered_tail([{"text": "在吗", "side": "them"},
                                       {"text": "在的喵", "side": "me"}]) == [])

    host = FakeHost(tmp, cfg(), FakeLLM())
    host._sent_log = ["好"]
    ch = AgenticGui(type("E", (), {"host": host, "cfg": host.cfg, "root": tmp,
                                   "log": lambda *a: None})())
    check("短句不会被自己的话吞掉",
          ch._fresh({"text": "好的", "side": "them"}))
    ch.seen.clear()
    for i in range(25):
        ch.seen.append(f"旧话{i}")
    check("同一句话过一会儿还能听见",
          ch._fresh({"text": "在吗", "side": "them"}))
    ch.seen.append("在吗")
    check("刚回过的同一句不重复回",
          not ch._fresh({"text": "在吗", "side": "them"}))

    joined = AgenticGui.__init__.__doc__
    check("多条一起来不会被吞", "woke_texts" in str(
        AgenticGui.handle.__code__.co_varnames), str(AgenticGui.handle.__code__.co_varnames))


def test_no_phantom_reply(tmp: Path) -> None:
    """时间分隔条不能被当成他发的消息——否则一条话会被回两遍。"""
    import qqgui
    from life.services.channel import AgenticGui

    for junk in ("昨天 23:28", "今天 08:00", "10月20日 12:30",
                 "2026年10月20日 12:30", "星期一 08:00", "12:30",
                 "以下为新消息", "对方正在输入…"):
        check(f"时间/系统条不是消息：{junk}", qqgui.is_stamp(junk))
    for real in ("哈喽哈喽", "昨天说的那个事还行", "12:30 见面吧", "我今天很累"):
        check(f"真话照收：{real}", not qqgui.is_stamp(real))

    host = FakeHost(tmp, cfg(), FakeLLM())
    host._sent_log = []
    ch = AgenticGui(type("E", (), {"host": host, "cfg": host.cfg, "root": tmp,
                                   "log": lambda *a: None})())
    check("通道也不把他没说的话当消息",
          not ch._fresh({"text": "昨天 23:28", "side": "them"}))


def test_console_path(tmp: Path) -> None:
    """命令口：纯文字递话的路，不碰 QQ，忙的时候只递话不另起一轮。"""
    from collections import deque
    from types import SimpleNamespace
    import bot as bot_mod

    class FakeConsole(bot_mod.Bot):
        def __init__(self, busy):
            self.cfg = cfg()
            self.brain = FakeBrain()
            self._guidance = deque(maxlen=10)
            self._locks = {}
            self._sent = {}
            self._reload_config = lambda: None     # 命令口会先刷一遍配置
            self._mind = lambda *a, **k: None
            self.sent = []
            self.think = []
            self.services = SimpleNamespace(
                channel=SimpleNamespace(_busy=busy, recent_steps=["device act「动态」"],
                                        last_obs={"app": "com.tencent.mobileqq",
                                                  "nodes": ["动态", "好友动态"]}),
                human_waiting=False, voice=lambda: "你叫小鱼。")

            class _LLM:
                async def chat(self, messages, **kw):
                    return "我在动态页找右下角那个加号喵"

            self.llm = _LLM()

        async def _send(self, target, text):
            self.sent.append(text)
            return True

        async def _maybe_command(self, peer, target, text):
            return False

        async def _think(self, *a, **kw):
            self.think.append(kw)
            return ("好的喵", [])



    FakeConsole._console_quick_reply = bot_mod.Bot._console_quick_reply
    FakeConsole._authoritative_life = bot_mod.Bot._authoritative_life
    FakeConsole._handle_console = bot_mod.Bot._handle_console

    class Sess:
        pass

    busy_bot = FakeConsole(busy=True)
    asyncio.run(bot_mod.Bot.handle_console(busy_bot, Sess(), "点右上角那个叉"))
    check("忙的时候话进信箱", list(busy_bot._guidance) == ["点右上角那个叉"],
          str(list(busy_bot._guidance)))
    check("忙的时候不另起一轮", busy_bot.think == [], str(busy_bot.think))
    check("忙的时候也回真话、不是模板",
          busy_bot.sent == ["我在动态页找右下角那个加号喵"], str(busy_bot.sent))
    check("话进了记忆、带来源标记",
          any("[命令口] 点右上角那个叉" in str(m) for m in busy_bot.brain.msgs),
          str(busy_bot.brain.msgs[-1:]))

    idle_bot = FakeConsole(busy=False)
    asyncio.run(bot_mod.Bot.handle_console(idle_bot, Sess(), "点右上角那个叉"))
    check("空着的时候真心回一句", idle_bot.sent == ["好的喵"], str(idle_bot.sent))
    check("命令口一律当吩咐",
          bool(idle_bot.think) and idle_bot.think[0].get("origin") == "commanded",
          str(idle_bot.think))


def test_guidance_reaches_running_turn(tmp: Path) -> None:
    """命令口递的话要插进正在跑的那一轮，而不是等她这轮跑完。"""
    from collections import deque
    from life.services.channel import AgenticGui

    seen: list[str] = []

    class RecLLM(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            seen.append(" ".join(str(m.get("content")) for m in messages))
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    obs = {"image": "", "app": "com.tencent.mobileqq", "rows": [],
           "nodes": ["樱花樱花想见你༻꧂"], "msgs": []}
    llm = RecLLM(steps=[("", [_Call("reply", {"text": "点掉了喵"})])])
    host = FakeHost(tmp, cfg(), llm)
    host._intent = {"needs_action": True, "kind": "task", "thinking": "fast"}
    host._sent_log = []
    host._guidance = deque(["点右上角那个叉"])

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, name, args, target, peer):
            return "ok", {"ok": True}

    async def reply_ok(text, expect=""):
        return True, "ok"

    async def dispatch_ok(name, args, target, peer):
        return "ok", {"ok": True}

    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok, dispatch=dispatch_ok)
    asyncio.run(ch._run_agent(obs, [{"text": "在吗", "side": "them"}], ""))
    flat = " ".join(seen)
    check("指导插进了正在跑的那一轮", "点右上角那个叉" in flat, flat[-120:])
    check("信箱读走以后是空的", len(host._guidance) == 0, str(list(host._guidance)))


def test_cmd_output(tmp: Path) -> None:
    """cmd.py 的输出要一问一答，不能把提示符冲掉。"""
    import cmd as cmd_mod

    async def run() -> list[str]:
        reader = asyncio.StreamReader()
        reader.feed_data(("我在动态页找加号喵" + chr(10) + "第二行" + chr(10)).encode("utf-8"))
        reader.feed_eof()
        return await cmd_mod.collect_reply(reader, first=1.0, more=0.1) or []

    lines = asyncio.run(run())
    check("多条回复会一次收干净", lines == ["我在动态页找加号喵", "第二行"], str(lines))

    async def empty() -> list[str] | None:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return await cmd_mod.collect_reply(reader, first=0.2, more=0.05)

    check("连接断了能认出来", asyncio.run(empty()) == [])

    async def slow() -> list[str] | None:
        reader = asyncio.StreamReader()
        return await cmd_mod.collect_reply(reader, first=0.1, more=0.05)

    check("等不到会返回 None 而不是卡死", asyncio.run(slow()) is None)


def test_console_state(tmp: Path) -> None:
    """她得能说清"我刚才做了什么"。"""
    from life.services.channel import AgenticGui

    host = FakeHost(tmp, cfg(), FakeLLM())
    host._sent_log = []
    ch = AgenticGui(type("E", (), {"host": host, "cfg": host.cfg, "root": tmp,
                                   "log": lambda *a: None})())
    ch._note_step("device", {"action": "act", "kind": "tap", "text": "动态"})
    ch._note_step("device", {"action": "act", "kind": "key", "key": "back"})
    ch._note_step("reply", {"text": "在的喵"})
    steps = list(ch.recent_steps)
    check("动作记得住、是人话",
          steps[0] == "device act「动态」" and "back" in steps[1], str(steps))


def test_console_quick_reply_real(tmp: Path) -> None:
    """真的那个快答函数：以前测试用假的替身，把它自己的 NameError 遮住了。"""
    from types import SimpleNamespace
    import bot as bot_mod

    seen: dict = {}

    class LLM:
        async def chat(self, messages, **kw):
            seen["msgs"] = messages
            seen["kw"] = kw
            return "我在动态页找右下角那个加号喵"

    class Ch:
        recent_steps = ["device act「动态」", "look"]
        last_obs = {"app": "com.tencent.mobileqq",
                    "nodes": ["动态", "好友动态", "说说", "发表"]}

    class Evo:
        channel = Ch()

        def voice(self):
            return "你叫小鱼。"

    class Self:
        services = Evo()
        llm = LLM()

    out = asyncio.run(bot_mod.Bot._console_quick_reply(Self(), 1, "你在做什么"))
    check("快答真的能出话", out == "我在动态页找右下角那个加号喵", out)
    body = seen["msgs"][-1]["content"]
    check("快答带上了她刚才的动作", "device act" in body, body[:100])
    check("快答带上了屏幕上的字", "好友动态" in body, body[:100])
    check("快答写明了是命令口", "命令口" in body, body[:100])
    check("快答给了够用的预算", seen["kw"].get("max_tokens", 0) >= 400, str(seen["kw"]))


def test_two_paths(tmp: Path) -> None:
    """QQ 和命令口是两条路：她得知道这句话从哪来，叫停要能刹住。"""
    from collections import deque
    from types import SimpleNamespace
    import bot as bot_mod
    from life.services.channel import AgenticGui

    # 系统提示里写了"这一轮从命令口来"
    class FakeConsole(bot_mod.Bot):
        def __init__(self):
            self.cfg = cfg()
            self.brain = FakeBrain()
            self._guidance = deque(maxlen=10)
            self._locks = {}
            self._sent = {}
            self._reload_config = lambda: None     # 命令口会先刷一遍配置
            self._mind = lambda *a, **k: None
            self._console_mode = False
            self.sent = []
            self.services = SimpleNamespace(channel=SimpleNamespace(_busy=True, _halt=False),
                                       human_waiting=False)

        async def _send(self, target, text):
            self.sent.append(text)
            return True

        async def _maybe_command(self, peer, target, text):
            return False

        async def _think(self, *a, **kw):
            return ("好的喵", [])

        async def _console_quick_reply(self, peer, text, stopping=False):
            self.stopped = stopping
            return "我在动态页喵"

    class Sess:
        pass

    FakeConsole._authoritative_life = bot_mod.Bot._authoritative_life
    FakeConsole._handle_console = bot_mod.Bot._handle_console
    b = FakeConsole()
    asyncio.run(bot_mod.Bot.handle_console(b, Sess(), "别发了，你连着发两条了"))
    check("叫停会被认出来并告诉通道", b.services.channel._halt is True, str(b.services.channel._halt))
    check("叫停时回的是实话", b.sent == ["我在动态页喵"], str(b.sent))

    # 通道：叫停之后这一轮立刻收手
    host = FakeHost(tmp, cfg(), FakeLLM())
    host._sent_log = []
    host._guidance = deque()
    llm = ScriptedLLM(steps=[("", [_Call("reply", {"text": "还想再发"})])])
    host.llm = llm

    class Evo2:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, *a, **k):
            return "ok", {"ok": True}

    async def reply_ok(text, expect=""):
        return True, "ok"

    ch = AgenticGui(Evo2(), observe=lambda: {"image": "", "rows": [], "nodes": [], "msgs": []},
                    reply=reply_ok, dispatch=lambda *a, **k: None)
    ch._halt = True
    okc = asyncio.run(ch._run_agent({"image": "", "rows": [], "nodes": [], "msgs": []},
                                    [{"text": "在吗", "side": "them"}], ""))
    check("叫停后这一轮不再往下做", okc is False)
    check("叫停后没有把话发出去", llm.steps and True)

    # 提示词里明确分开两条路
    rules = ch.rules()
    check("提示词写明屏幕上是 QQ", "这块屏是 QQ" in rules)
    check("提示词写明命令口不在屏幕上", "命令口" in rules and "不要在 QQ 里回它" in rules)


MARKUP = ("<｜｜DSML｜｜ calls> "
          "<｜｜DSML｜｜ invoke name=\"device\"> "
          "<｜｜DSML｜｜ parameter name=\"action\">act</｜｜DSML｜｜ parameter> "
          "<｜｜DSML｜｜ parameter name=\"kind\">tap</｜｜DSML｜｜ parameter> "
          "<｜｜DSML｜｜ parameter name=\"x\">450</｜｜DSML｜｜ parameter> "
          "</｜｜DSML｜｜ invoke> </｜｜DSML｜｜ calls>")


def test_tool_markup_leak(tmp: Path) -> None:
    """模型把工具调用写进正文时：不能发出去，而且要把调用捞回来真做。"""
    from collections import deque
    from life.services import dsml
    import bot as bot_mod
    from life.services.channel import AgenticGui

    check("认得出 DSML 标记", dsml.has_markup(MARKUP))
    check("能把调用捞出来",
          dsml.parse(MARKUP) == [("device", {"action": "act", "kind": "tap", "x": 450})],
          str(dsml.parse(MARKUP)))
    check("洗掉以后只剩真话", dsml.strip("好的喵 " + MARKUP) == "好的喵",
          repr(dsml.strip("好的喵 " + MARKUP)))
    check("洗掉以后不留参数值", "450" not in dsml.strip(MARKUP))
    check("干净的话不动它", dsml.strip("主人你看，置顶是蓝的") == "主人你看，置顶是蓝的")

    # 通道：正文里的调用要捞回来执行
    did: list[str] = []
    sent: list[str] = []

    async def dispatch(name, args, target, peer):
        did.append(name + "." + str(args.get("action")))
        return "点了 (450,340)", {"ok": True}

    async def reply(text, expect=""):
        sent.append(text)
        return True, "ok"

    llm = ScriptedLLM(steps=[("好的喵 " + MARKUP, []),
                             ("", [_Call("reply", {"text": "点好了喵"})])])
    host = FakeHost(tmp, cfg(), llm)
    host._intent = {"needs_action": True, "kind": "task", "thinking": "fast"}
    host._sent_log = []
    host._guidance = deque()

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, name, args, target, peer):
            return await dispatch(name, args, target, peer)

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply, dispatch=dispatch)
    asyncio.run(ch._run_agent(obs, [{"text": "你点哪呢", "side": "them"}], ""))
    check("正文里的调用被真的执行了", did == ["device.act"], str(did))
    check("工具语法没当成话发出去",
          sent == ["点好了喵"] and "DSML" not in " ".join(sent), str(sent))

    # 命令口：发送前一定要洗（这条路以前不经过 plan_segments）
    class Sess:
        def __init__(self):
            self.lines = []

        async def send(self, t):
            self.lines.append(t)

    class Fake:
        cfg = cfg()

    sess = Sess()
    asyncio.run(bot_mod.Bot._send(Fake(), ("console", sess), "好的喵 " + MARKUP))
    check("命令口不会把语法发到他屏幕上",
          sess.lines == ["好的喵"], str(sess.lines))

    sess2 = Sess()
    asyncio.run(bot_mod.Bot._send(Fake(), ("console", sess2), MARKUP))
    check("只剩语法就干脆不发", sess2.lines == [], str(sess2.lines))


def test_config_reload(tmp: Path) -> None:
    """GUI 模式下 on_event 不跑，配置热更新得靠通道/命令口自己触发。"""
    from collections import deque
    from types import SimpleNamespace
    import bot as bot_mod
    from life.services.channel import AgenticGui

    class FakeConsole(bot_mod.Bot):
        def __init__(self):
            self.cfg = cfg()
            self.brain = FakeBrain()
            self._guidance = deque(maxlen=10)
            self._locks = {}
            self._sent = {}
            self._reload_config = lambda: None     # 命令口会先刷一遍配置
            self._mind = lambda *a, **k: None
            self._console_mode = False
            self.reloads = 0

            def _count():
                self.reloads += 1

            self._reload_config = _count
            self.sent = []
            self.services = SimpleNamespace(channel=SimpleNamespace(_busy=False, _halt=False),
                                       human_waiting=False)

        def _reload_config(self):
            self.reloads += 1

        async def _send(self, target, text):
            self.sent.append(text)
            return True

        async def _maybe_command(self, peer, target, text):
            return False

        async def _think(self, *a, **kw):
            return ("好的喵", [])

        async def _console_quick_reply(self, peer, text, stopping=False):
            return "在的喵"

    class Sess:
        pass

    b = FakeConsole()
    asyncio.run(bot_mod.Bot.handle_console(b, Sess(), "在吗"))
    check("命令口会刷配置", b.reloads >= 1, str(b.reloads))

    # 通道：收消息时也要刷
    host = FakeHost(tmp, cfg(), FakeLLM())
    host._sent_log = []
    reloads = {"n": 0}

    def reload_now():
        reloads["n"] += 1

    host._reload_config = reload_now

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = host.llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, *a, **k):
            return "ok", {"ok": True}

    async def reply_ok(text, expect=""):
        return True, "ok"

    obs = {"image": "", "rows": [], "nodes": [], "msgs": [{"text": "在吗", "side": "them"}]}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok, dispatch=lambda *a, **k: None)
    asyncio.run(ch.handle(obs, [{"text": "在吗", "side": "them"}]))
    check("通道收消息时刷配置", reloads["n"] >= 1, str(reloads))


def test_dynamic_route(tmp: Path) -> None:
    """动态路由：闲聊/动手=快速，规划/分析=深想，执行中自动退回快速。"""
    from collections import deque
    import bot as bot_mod
    from life.services.channel import AgenticGui

    class C:
        thinking = False
        thinking_auto = True

    r = bot_mod.Bot.__new__(bot_mod.Bot)
    r.cfg = C()
    check("闲聊走快速", r.route("在吗")[0] == "fast", str(r.route("在吗")))
    check("直接动手走快速", r.route("打开WiFi")[0] == "fast", str(r.route("打开WiFi")))
    check("规划走深想",
          r.route("帮我规划一下周末去杭州两天怎么玩")[0] == "deep",
          str(r.route("帮我规划一下周末去杭州两天怎么玩")))
    check("分析走深想",
          r.route("你觉得她为什么突然不理我了")[0] == "deep",
          str(r.route("你觉得她为什么突然不理我了")))
    check("排查报错走深想",
          r.route("这段代码报错了帮我看看思路")[0] == "deep",
          str(r.route("这段代码报错了帮我看看思路")))
    C.thinking_auto = False
    check("关掉自动路由就按配置走", r.route("为什么")[0] == "fast", str(r.route("为什么")))

    # 通道：真按模式发请求，执行后降级
    class RecLLM(ScriptedLLM):
        def __init__(self, steps):
            super().__init__(steps=steps)
            self.flags: list = []

        async def chat_msg(self, messages, tools=None, tag="", **kw):
            self.flags.append(kw.get("thinking"))
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    class C2:
        thinking = False
        thinking_auto = True
        owner = 1

    llm = RecLLM([("", [_Call("think_deeper", {"why": "要拆解"})]),
                  ("想清楚了：先看动态页，再安排这一周。", []),
                  ("", [_Call("device", {"action": "act", "kind": "tap", "text": "动态"})]),
                  ("", [_Call("reply", {"text": "排好了喵"})])])
    host = FakeHost(tmp, C2(), llm)
    host._sent_log = []
    host._intent = {"needs_action": False, "kind": "question", "thinking": "deep"}
    host._guidance = deque()

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, *a, **k):
            return "ok", {"ok": True}

    async def reply_ok(text, expect=""):
        return True, "ok"

    async def dispatch_ok(name, args, target, peer):
        return "ok", {"ok": True}

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok,
                    dispatch=dispatch_ok)
    asyncio.run(ch._run_agent(obs, [{"text": "帮我规划一下这周怎么安排", "side": "them"}], ""))
    check("规划用深想、执行后自动退回快速",
          bool(llm.flags) and llm.flags[0] is True and llm.flags[-1] is False
          and True in llm.flags, str(llm.flags))

    # 快速模式下她说"想深一点"，下一句就真的深想
    llm2 = RecLLM([("", [_Call("think_deeper", {})]),
                   ("想清楚了：先回他一声。", []),
                   ("", [_Call("reply", {"text": "嗯喵"})])])
    host2 = FakeHost(tmp, C2(), llm2)
    host2._sent_log = []
    host2._guidance = deque()

    class Evo2(Evo):
        def __init__(self):
            super().__init__()
            self.host = host2
            self.llm = llm2

    ch2 = AgenticGui(Evo2(), observe=lambda: obs, reply=reply_ok,
                     dispatch=dispatch_ok)
    asyncio.run(ch2._run_agent(obs, [{"text": "在吗", "side": "them"}], ""))
    check("她自己能升级成深想",
          len(llm2.flags) >= 2 and llm2.flags[0] is False and llm2.flags[1] is True,
          str(llm2.flags))


def test_voice(tmp: Path) -> None:
    """她能不能发语音：开关、GUI 模式下的 NapCat 短连接、以及她自己的选择。"""
    from collections import deque
    import bot as bot_mod
    import skills
    from life.services import onebot
    from life.services.channel import AgenticGui

    class C:
        text_only = True
        voice = True
        voice_reply = "sometimes"
        voice_cooldown = 0
        ws_url = "ws://127.0.0.1:3001"
        voice_name = "zh-CN-XiaoyiNeural"
        voice_pitch = "+0Hz"
        voice_rate = "+0%"
        voice_volume = "+0%"

    fake = bot_mod.Bot.__new__(bot_mod.Bot)
    fake.cfg = C()
    fake.ws = None
    fake._last_voice = {}
    fake._refresh_voice_cfg = lambda: None
    fake._emotions = lambda peer: {"依恋": 10, "兴奋": 8, "恼": 0, "不安": 0, "委屈": 0}
    fake._traits = lambda peer: {"黏人": 70, "嘴硬": 30}

    odds = fake._voice_odds(1, "在的喵")
    check("开了语音就有概率（不再被 text_only 一刀切）", 0.0 < odds <= 1.0, str(odds))
    C.voice = False
    check("关掉语音概率是 0", fake._voice_odds(1, "在的喵") == 0.0)
    C.voice = True

    # GUI 模式没有 OneBot 长连接：应该借 NapCat 开短连接发 record 段
    called: dict = {}

    async def fake_tts(text, voice, **kw):
        return Path("data/cache/fake-voice.mp3")

    async def fake_send_voice(url, uid, path, group=False):
        called["url"], called["uid"], called["path"] = url, uid, path
        return True, "发出去了"

    old_tts, old_send = skills.tts, onebot.send_voice
    skills.tts, onebot.send_voice = fake_tts, fake_send_voice
    try:
        ok = asyncio.run(bot_mod.Bot._say_voice(fake, ("private", 7), "在的喵"))
    finally:
        skills.tts, onebot.send_voice = old_tts, old_send
    check("没有长连接也能发语音", ok is True and called.get("uid") == 7, str(called))

    # 通道里：她标了 voice=true 就发语音、不再打字
    said: list[str] = []
    spoke: list[str] = []

    async def reply_ok(text, expect=""):
        said.append(text)
        return True, "ok"

    async def say_voice_ok(target, text):
        spoke.append(text)
        return True

    llm = ScriptedLLM(steps=[("", [_Call("reply", {"text": "想你了喵", "voice": True})])])
    host = FakeHost(tmp, cfg(), llm)
    host._intent = {"needs_action": False, "kind": "chat", "thinking": "fast"}
    host._sent_log = []
    host._guidance = deque()
    host._last_voice = {}
    host._voice_odds = lambda peer, seg: 0.0      # 全靠她自己说 true
    host._say_voice = say_voice_ok

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, *a, **k):
            return "ok", {"ok": True}

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok,
                    dispatch=lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
    asyncio.run(ch._run_agent(obs, [{"text": "在吗", "side": "them"}], ""))
    check("她决定用说的就发语音", spoke == ["想你了喵"], str(spoke))
    check("发过语音就不再打字", said == [], str(said))


def test_self_edit(tmp: Path) -> None:
    """她能改自己的代码：抄得差一点也得认，行号式更稳，失败要给线索。"""
    from life.services.workshop import Workshop

    class Store:
        def __init__(self):
            self.rows: dict = {}

        def add_self_edit(self, eid, *a, **k):
            self.rows[eid] = {"status": "staged"}

        def update_self_edit(self, eid, **k):
            self.rows.setdefault(eid, {}).update(k)

        def get_self_edit(self, eid):
            return self.rows.get(eid)

        def list_self_edits(self, n=10):
            return []

    root = tmp / "proj"
    root.mkdir(parents=True, exist_ok=True)
    B = chr(92)
    Q = chr(34)
    src = ("def f(x):" + chr(10) +
           "    return x + 1" + chr(10) +
           "" + chr(10) +
           "# 尾巴 r" + Q + "[^" + B + "d]" + Q + chr(10))
    f = root / "a.py"
    f.write_text(src, encoding="utf-8")
    w = Workshop(root, Store())

    r = w.apply_edits([{"path": "a.py", "old": "    return x + 1",
                        "new": "    return x + 2"}], run_tests=False)
    check("原文一字不差能改", r.get("ok") and "x + 2" in f.read_text(encoding="utf-8"), str(r)[:120])

    f.write_text(src, encoding="utf-8")
    r = w.apply_edits([{"path": "a.py", "old": "    return x + 1   ",
                        "new": "    return x + 3"}], run_tests=False)
    check("行尾多几个空格也认", r.get("ok") and "x + 3" in f.read_text(encoding="utf-8"), str(r)[:120])

    f.write_text(src, encoding="utf-8")
    over = "# 尾巴 r" + Q + "[^" + B + B + "d]" + Q     # 她多转义了一层
    r = w.apply_edits([{"path": "a.py", "old": over,
                        "new": "# 尾巴改好了"}], run_tests=False)
    check("反斜杠多转义一层也认", r.get("ok") and "尾巴改好了" in f.read_text(encoding="utf-8"),
          str(r)[:160])

    f.write_text(src, encoding="utf-8")
    r = w.apply_edits([{"path": "a.py", "start": 2, "end": 2,
                        "new": "    return x * 10"}], run_tests=False)
    body = f.read_text(encoding="utf-8")
    check("按行号换（不用抄原文）", r.get("ok") and "x * 10" in body, str(r)[:120])
    check("行号式没把别的行搞坏", body.splitlines()[0] == "def f(x):" and body.splitlines()[2] == "")

    f.write_text("a = 1" + chr(10) + "a = 1" + chr(10), encoding="utf-8")
    r = w.apply_edits([{"path": "a.py", "old": "a = 1", "new": "a = 2"}], run_tests=False)
    check("对不唯一就拒绝，并说清几次", (not r.get("ok")) and "2 次" in str(r.get("error")),
          str(r.get("error"))[:120])

    f.write_text(src, encoding="utf-8")
    r = w.apply_edits([{"path": "a.py", "old": "这段根本不在文件里",
                        "new": "x"}], run_tests=False)
    check("完全对不上时给线索", (not r.get("ok")) and ("行号" in str(r.get("error"))),
          str(r.get("error"))[:160])

    # 真实踩过的那个坑：改 _say_voice 时把 re.sub 里的反斜杠多转义了一层
    marker = 'base = int(re.sub(r"[^' + B + '-' + B + 'd]", "", self.cfg.voice_pitch) or 0)'
    over2 = marker.replace(B, B + B)
    b = root / "b.py"
    b.write_text("def f():" + chr(10) + "    " + marker + chr(10), encoding="utf-8")
    r = w.apply_edits([{"path": "b.py", "old": "    " + over2,
                        "new": "    base = 0  # 改过"}], run_tests=False)
    check("多转义一层也能改（真实踩过的坑）",
          r.get("ok") and "改过" in b.read_text(encoding="utf-8"), str(r.get("error"))[:160])

    r = w.apply_edits([{"path": "a.py", "start": 2, "end": 2,
                        "new": "def broken(:"}], run_tests=False)
    check("语法坏了会被挡下", not r.get("ok") and "语法" in str(r.get("error")),
          str(r.get("error"))[:120])


def test_mood_and_selfcheck(tmp: Path) -> None:
    """情绪/欲望要进通道提示词；工具没成时她要自己诊断、换招。"""
    from collections import deque
    import bot as bot_mod
    from life.services.channel import AgenticGui

    seen: list[str] = []

    class RecLLM(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            seen.append(" ".join(str(m.get("content")) for m in messages))
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    llm = RecLLM(steps=[("", [_Call("device", {"action": "act", "kind": "tap", "text": "动态"})]),
                        ("", [_Call("reply", {"text": "点了喵"})])])
    host = FakeHost(tmp, cfg(), llm)
    host._intent = {"needs_action": True, "kind": "task", "thinking": "fast"}
    host._sent_log = []
    host._guidance = deque()
    host._emotions = lambda peer: {"依恋": 9.0, "委屈": 0, "恼": 1, "不安": 0, "兴奋": 0}
    host._inner_block = lambda peer: (chr(10) + "## 你现在这个人" + chr(10)
                                      + "- 长期最想要的：忠贞8" + chr(10)
                                      + "- 此刻的情绪：依恋 9/10" + chr(10)
                                      + "- 所以你现在会：想贴着他，话说得更软")

    async def dispatch_fail(name, args, target, peer):
        return "（点不动：屏幕上没有「动态」）", {"ok": False}

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, name, args, target, peer):
            return await dispatch_fail(name, args, target, peer)

    async def reply_ok(text, expect=""):
        return True, "ok"

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok, dispatch=dispatch_fail)
    asyncio.run(ch._run_agent(obs, [{"text": "在吗", "side": "them"}], ""))

    head = seen[0] if seen else ""
    check("情绪进了通道提示词", "此刻的情绪" in head and "依恋 9/10" in head, head[:150])
    check("欲望进了通道提示词", "长期最想要的" in head, head[:150])
    nudge = seen[-1] if seen else ""
    check("没成的时候会让她自己诊断", "这一步没成" in nudge, nudge[-160:])
    check("提示里点明要换招/看行号", "换一种做法" in nudge or "行号" in nudge)

    # 自检：语法 + 最近的错
    shell = bot_mod.Bot.__new__(bot_mod.Bot)
    shell.services = type("E", (), {"workshop": None})()
    out = bot_mod.Bot._self_check(shell)
    check("自检能跑", "自检：" in out and "语法错误 0 个" in out, out[:120])

    from life.services import tools as service_tools
    names = [t["function"]["name"] for t in service_tools.SCHEMAS]
    check("自检是她手上的工具", "self_check" in names, str(names[-3:]))


def test_emotion_dynamics(tmp: Path) -> None:
    """情绪不能被一句话掀翻；欲望是慢变量，给情绪定个重心。"""
    from life.services.store import Store
    import bot as bot_mod

    d = tmp / "emo"
    d.mkdir(parents=True, exist_ok=True)
    brain = bot_mod.Brain(d / "brain.db")

    class L:
        def __init__(self, delta):
            self.delta = delta

        async def chat(self, *a, **k):
            import json as _j
            return _j.dumps(self.delta, ensure_ascii=False)

        async def chat_msg(self, *a, **k):
            class M:
                content = ""
                tool_calls = []
            return M()

    cfg = bot_mod.Config(owner=1, emotions=True, mood_chaos=0.0, drift=False,
                         bond=False, emo_step_max=1.5)
    bot = bot_mod.Bot(cfg, brain, L({"依恋": 5, "委屈": 5, "恼": 5, "不安": 5, "兴奋": 5}))
    before = bot._emo_view(bot._emotions(1))
    asyncio.run(bot._update_emotions(1, "随便说点什么", "owner"))
    after = bot._emo_view(bot._emotions(1))     # 存进去以后再读，才看得到真实幅度
    moved = max(abs(after[k] - before[k]) for k in before)
    check("一句话最多推 1.5 格（0~10 手感，不再一跳就顶满）", moved <= 1.51, f"{moved:.2f}")

    # 欲望 → 基线：嫉妒拉高不安的重心
    brain.set_kv("des:1", '{"嫉妒": 10, "贪婪": 10}')
    base = bot._emo_baseline(1)
    check("嫉妒高 → 不安的基线被抬高", base["不安"] > bot_mod.EMO_BASE["不安"], str(base))
    check("贪婪高 → 依恋的基线被抬高", base["依恋"] > bot_mod.EMO_BASE["依恋"], str(base))

    brain.set_kv("des:1", '{"温和": 10, "宽厚": 10, "忠贞": 10}')
    sens = bot._emo_sensitivity(1)
    check("温和/宽厚高 → 不容易被点着", sens["恼"] < 0.6, f"恼敏感度 {sens['恼']:.2f}")
    brain.set_kv("des:1", '{"嫉妒": 10}')
    check("嫉妒高 → 不安来得快", bot._emo_sensitivity(1)["不安"] > 1.3,
          f"不安敏感度 {bot._emo_sensitivity(1)['不安']:.2f}")

    # 敏感度真的作用在增量上：温和旺的人，同一句惹恼的话也炸不起来
    brain.set_kv("des:1", '{"温和": 10, "宽厚": 10}')
    brain.set_kv("emo:1", '{"依恋": 6, "委屈": 0, "恼": 0, "不安": 0, "兴奋": 0}')
    brain.set_kv("emo_ts:1", str(int(time.time())))
    soft = bot_mod.Bot(bot_mod.Config(owner=1, emotions=True, mood_chaos=0.0,
                                      drift=False, bond=False, emo_step_max=1.5),
                       brain, L({"恼": 3}))
    after_soft = asyncio.run(soft._update_emotions(1, "你真烦", "owner"))
    # 对照组：同样的起点和输入，但没有温和/宽厚的高欲望
    brain.set_kv("des:1", '{"温和": 5, "宽厚": 5}')
    brain.set_kv("emo:1", '{"依恋": 6, "委屈": 0, "恼": 0, "不安": 0, "兴奋": 0}')
    brain.set_kv("emo_ts:1", str(int(time.time())))
    hard = bot_mod.Bot(bot_mod.Config(owner=1, emotions=True, mood_chaos=0.0,
                                      drift=False, bond=False, emo_step_max=1.5),
                       brain, L({"恼": 3}))
    after_hard = asyncio.run(hard._update_emotions(1, "你真烦", "owner"))
    check("温和的人被同一句话惹恼得更少",
          after_soft["恼"] < after_hard["恼"] - 0.2,
          f"soft={after_soft['恼']} hard={after_hard['恼']}")

    # 关掉耦合就回到出厂基线
    cfg.emo_baseline_from_desires = False
    check("开关能关掉耦合", bot._emo_baseline(1)["不安"] == bot_mod.EMO_BASE["不安"])

    # 衰减：兴奋落得比依恋快
    hold = {"依恋": 10.0, "委屈": 10.0, "恼": 10.0, "不安": 10.0, "兴奋": 10.0}
    brain.set_kv("emo:1", __import__("json").dumps(hold))
    brain.set_kv("emo_ts:1", str(int(time.time()) - 3600))
    emo = bot._emotions(1)
    check("一小时后悔意比兴奋退得多",
          (10 - emo["兴奋"]) > (10 - emo["依恋"]), f"兴奋{emo['兴奋']} 依恋{emo['依恋']}")

    # 一整块：欲望 + 情绪 + 由此推出来的表现
    import json as _j
    brain.set_kv("des:1", _j.dumps({"嫉妒": 8.0, "贪婪": 7.5}))
    brain.set_kv("emo:1", _j.dumps({"依恋": 17.0, "委屈": 0, "恼": 1, "不安": 12.0, "兴奋": 3.0}))
    brain.set_kv("emo_ts:1", str(int(time.time())))
    blk = bot._inner_block(1)
    check("内心是一整块（欲望+情绪）",
          "长期最想要的" in blk and "此刻的情绪" in blk, blk[:160])
    check("由它们推出现在会怎么样", "所以你现在会" in blk, blk[-160:])

    # 情绪反过来磨欲望：不安久了，嫉妒真的会长
    brain.set_kv("emo:1", _j.dumps({"依恋": 12, "委屈": 0, "恼": 0, "不安": 16, "兴奋": 0}))
    brain.set_kv("emo_ts:1", str(int(time.time())))
    brain.set_kv("des:1", _j.dumps({"嫉妒": 5.0}))
    quiet = bot_mod.Bot(bot_mod.Config(owner=1, emotions=True, mood_chaos=0.0,
                                       drift=False, bond=False), brain, L({}))
    j0 = quiet._desires(1)["嫉妒"]
    asyncio.run(quiet._update_emotions(1, "嗯", "owner"))
    j1 = quiet._desires(1)["嫉妒"]
    check("不安久了会把嫉妒磨上来（情绪→欲望）", j1 > j0, f"{j0} -> {j1}")

    brain.close()


def test_emotion_recovers(tmp: Path) -> None:
    """气话垒上去要能自己淡下来，不能永远卡在顶格。"""
    import json as _j
    import bot as bot_mod

    d = tmp / "rec"
    d.mkdir(parents=True, exist_ok=True)
    brain = bot_mod.Brain(d / "brain.db")

    class L:
        def __init__(self, delta):
            self.delta = delta

        async def chat(self, *a, **k):
            return _j.dumps(self.delta, ensure_ascii=False)

        async def chat_msg(self, *a, **k):
            class M:
                content = ""
                tool_calls = []
            return M()

    cfg = bot_mod.Config(owner=1, emotions=True, mood_chaos=0.0, drift=False, bond=False,
                         emo_cap=20.0, emo_step_max=1.5)
    angry = bot_mod.Bot(cfg, brain, L({"委屈": 3, "恼": 3, "不安": 3, "依恋": -3}))

    moves = []
    for _ in range(6):
        asyncio.run(angry._update_emotions(1, "你怎么又不理我", "owner"))
        moves.append(angry._emo_view(angry._emotions(1)))

    check("越往上推得越慢（边际递减）",
          (moves[0]["委屈"] - 0.0) > (moves[-1]["委屈"] - moves[-2]["委屈"]),
          f"第一次 {moves[0]['委屈']:.2f}，最后一次 {moves[-1]['委屈']-moves[-2]['委屈']:.2f}")
    check("气话之后不会无限顶格", moves[-1]["委屈"] < 10.0, f"{moves[-1]['委屈']:.2f}")

    # 他开始哄她：只要模型给 0，情绪就该自己往下走
    calm = bot_mod.Bot(cfg, brain, L({}))
    before = calm._emo_view(calm._emotions(1))
    for _ in range(4):
        asyncio.run(calm._update_emotions(1, "好啦好啦，别气了", "owner"))
    after = calm._emo_view(calm._emotions(1))
    check("没人再惹她，会自己冷静下来",
          after["委屈"] < before["委屈"] - 1.0,
          f"{before['委屈']:.2f} -> {after['委屈']:.2f}")
    check("依恋也会回到她本来的底子",
          after["依恋"] > before["依恋"], f"{before['依恋']:.2f} -> {after['依恋']:.2f}")

    brain.close()


def test_task_becomes_intent(tmp: Path) -> None:
    """他交代的事：不许只答应一句。要么现在做，要么记成念想，之后自己接着做。"""
    from collections import deque
    import bot as bot_mod
    from life.services.channel import AgenticGui

    shell = bot_mod.Bot.__new__(bot_mod.Bot)
    shell.cfg = bot_mod.Config()
    check("认得出这是在交任务",
          shell.is_task("给你一个任务，我睡觉的时候，你可以研究研究各个界面"))
    check("认得出这是吩咐", shell.is_task("模仿高潮的样子给我发语音好吗"))
    check("闲聊不算任务", not shell.is_task("今天有点累"))

    said: list[str] = []
    owed: list[str] = []
    seen: list[str] = []

    class RecLLM(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            seen.append(" ".join(str(m.get("content")) for m in messages))
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    llm = RecLLM(steps=[("", [_Call("reply", {"text": "主人睡啦，那小鱼乖乖领任务喵，先把界面翻一翻"})]),
                        ("", [_Call("intent", {"action": "add", "text": "研究各个界面",
                                               "why": "主人让我睡觉时学着点"})]),
                        ("", [_Call("reply", {"text": "记下了喵，醒了给你汇报"})])])
    host = FakeHost(tmp, cfg(), llm)
    host._intent = {"needs_action": True, "kind": "task", "thinking": "fast"}
    host._sent_log = []
    host._guidance = deque()
    host.is_task = bot_mod.Bot.is_task.__get__(host)
    host._emotions_n = lambda peer: {"依恋": 6, "委屈": 0, "恼": 0, "不安": 0, "兴奋": 0}

    async def reply_ok(text, expect=""):
        said.append(text)
        return True, "ok"

    async def dispatch_ok(name, args, target, peer):
        owed.append(f"{name}.{args.get('action')}")
        return "（记下了）", {"ok": True}

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, name, args, target, peer):
            return await dispatch_ok(name, args, target, peer)

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok, dispatch=dispatch_ok)
    asyncio.run(ch._run_agent(
        obs, [{"text": "给你一个任务，我睡觉的时候研究研究各个界面", "side": "them"}], ""))

    check("先发执行前通知", host.sent and "我去看看这件事的具体情况。" in host.sent[0], str(host.sent))
    check("应完声真去记/真去做", any(x.startswith("intent.") for x in owed), str(owed))
    check("最后交差", said and "记下了" in said[-1], str(said))
    check("提醒里给了两条路", "intent add" in " ".join(seen), "…")


def test_scene_and_iq(tmp: Path) -> None:
    """场景不同，情绪反应不同；IQ 一直高。"""
    import json as _j
    import bot as bot_mod
    from life.services.channel import AgenticGui

    shell = bot_mod.Bot.__new__(bot_mod.Bot)
    shell.cfg = bot_mod.Config()

    class Br:
        def recent(self, p, n=4):
            return []

    shell.brain = Br()
    check("色情场景认得出来", shell._scene(1, "操你，叫大声点") == "intimate",
          shell._scene(1, "操你，叫大声点"))
    check("争执场景认得出来", shell._scene(1, "你怎么又不回我，烦死了") == "conflict",
          shell._scene(1, "你怎么又不回我，烦死了"))
    check("日常场景认得出来", shell._scene(1, "今天天气不错") == "daily")

    # 同一个模型给的粗暴一句话，在 intimate 场景下不该被当成"被凶了"
    class L:
        def __init__(self, delta):
            self.delta = delta

        async def chat(self, messages, **k):
            self.last_prompt = messages[-1]["content"]
            return _j.dumps(self.delta, ensure_ascii=False)

        async def chat_msg(self, *a, **k):
            class M:
                content = ""
                tool_calls = []
            return M()

    d = tmp / "scene"
    d.mkdir(parents=True, exist_ok=True)
    brain = bot_mod.Brain(d / "b.db")
    cfg = bot_mod.Config(owner=1, emotions=True, mood_chaos=0.0, drift=False, bond=False)

    llm = L({"委屈": 3, "恼": 3})
    b = bot_mod.Bot(cfg, brain, llm)
    asyncio.run(b._update_emotions(1, "操你，叫大声点", "owner"))
    check("亲密场景的提示词写清了这不是骂",
          "亲密" in llm.last_prompt and "不是在骂你" in llm.last_prompt,
          llm.last_prompt[-160:])

    llm2 = L({})
    b2 = bot_mod.Bot(cfg, brain, llm2)
    asyncio.run(b2._update_emotions(1, "你怎么又不回我，烦死了", "owner"))
    check("争执场景按争执算", "争执" in llm2.last_prompt, llm2.last_prompt[-120:])

    # IQ：气到顶格也不掉到 80 以下
    b._emotions_n = lambda peer: {"恼": 10, "委屈": 10, "不安": 0, "依恋": 0, "兴奋": 0}
    iq, desc = b._iq(1)
    check("气到顶格 IQ 还是高", iq >= 80 and "清醒" in desc, f"{iq} {desc}")
    check("IQ 底子是 92", float(cfg.iq_base) >= 90, str(cfg.iq_base))
    brain.close()

    # 手机晚开：通道得有 rescan，且能认出"手机不在"
    check("通通道有补扫方法", hasattr(AgenticGui, "_rescan"))
    src = (ROOT / 'life' / 'services' / "channel.py").read_text(encoding="utf-8")
    check("手机不在时只用协议端回话", "手机断开了：先只用协议端回话" in src)
    check("手机不在也照收消息", "if phone_up and not self._phone_ok:" in src)
    check("接上了先补扫", "手机接上了，先扫一遍有没有漏掉的话" in src)
    check("提示词写了手机可能晚开", "手机/模拟器可能比你晚起来" in src)


def test_skill_registry(tmp: Path) -> None:
    """P0：技能要有索引，按"什么时候用"的意思搜，而不是关键词。"""
    from life.services.skill_registry import Registry

    idx = Registry(ROOT)
    n = idx.rebuild()
    check("技能索引建起来了", n >= 10, str(n))
    ids = {x.id for x in idx.skills.values()}
    check("她的方法在里面", "发说说" in ids and "发送" in ids, str(sorted(ids)[:8]))

    hits = idx.search("我想在空间里发一条说说", k=5)
    top = [h[0].id for h in hits[:3]]
    check("按意思能搜到发说说",
          any(x in top for x in ("发说说", "发qq动态-界面点点点-不用编辑器")), str(top))
    hits2 = idx.search("界面顶栏是不是那个人", k=5)
    check("换个问法也能搜到通道/发送",
          any(h[0].id in ("通道", "发送") for h in hits2),
          str([h[0].id for h in hits2[:3]]))

    # 退役的不出现在结果里
    idx.skills["发说说"].status = "retired"
    hits3 = [h[0].id for h in idx.search("发一条说说", k=5)]
    check("退役的技能不再被推荐", "发说说" not in hits3, str(hits3))
    idx.skills["发说说"].status = "draft"

    # 内置工具也登记进来
    idx.rebuild(tools=[{"function": {"name": "device", "description": "看屏幕、点、打字"}}])
    check("工具也进索引", "device" in {x.id for x in idx.skills.values()})
    check("列表里能看到", "device" in idx.list_text() or "device" in idx.list_text(200))
    t = idx.detail_text("device")
    check("能看单个技能详情", "device" in t, t[:80])


def test_skill_iteration(tmp: Path) -> None:
    """P4：方法不是一次就对，改一次留一版，成绩只记在账上。"""
    from life.services.procedures import ProcedureBook, Procedure

    skills_dir = tmp / "skills_it"
    skills_dir.mkdir(parents=True, exist_ok=True)
    book = ProcedureBook(skills_dir)
    proc = Procedure(slug="测试法", name="测试法", when="试一下",
                     steps=["see", "tap 确定"], status="draft")
    book.save(proc)
    v1 = proc.version
    check("第一次存下来", (skills_dir / "测试法.md").exists() and v1 == 1, str(v1))

    proc.steps = ["see", "tap 确定", "verify 完成"]
    book.save(proc)
    hist = tmp / "data" / "skills_history"
    check("改一次版本 +1", proc.version == v1 + 1, str(proc.version))
    check("旧版本留了档", hist.exists() and any(hist.iterdir()), str(list(hist.glob('*'))))

    # 再存一次内容没变 → 不涨版本
    v2 = proc.version
    book.save(proc)
    check("内容没变不涨版本", proc.version == v2, str(proc.version))


def test_reply_split(tmp: Path) -> None:
    """动态聊天气泡与完整正文共用布局规则。"""
    import life.services.channel as CH

    src = (ROOT / 'life' / 'services' / "channel.py").read_text(encoding="utf-8")
    check("GUI 使用动态气泡分组", "group_chat" in src)
    import bot as B
    from life.message_layout import content_blocks, transport_parts, Bubble
    check("普通聊天按意思分条", len(content_blocks('第一行\n第二行')) == 2)
    check("长正文不截断", ''.join(x.text for x in transport_parts(Bubble('啊' * 4500))) == '啊' * 4500)
    from life.message_layout import bubble_delay
    check("间隔按字数算", bubble_delay('字' * 30) > bubble_delay('你好'))
    check("完整正文不伪装长时间打字", bubble_delay('字' * 2000, 'artifact') < .6)
    # 连着几次没成效，这一轮要真的收手（以前 continue，会一直重来）
    check("连着没成会收手",
          "这一轮收手" in src and "连着几次没成，先停，告诉他卡在哪" in src)


def test_computer_tool(tmp: Path) -> None:
    """她要能在电脑上干活：跑命令、读写文件、把成果发给他。"""
    from types import SimpleNamespace
    from life.services import tools as T

    names = [x["function"]["name"] for x in T.SCHEMAS]
    check("computer 是她手上的工具", "computer" in names, str(names[-4:]))
    acts = [x for x in T.SCHEMAS if x["function"]["name"] == "computer"][0]
    enum = acts["function"]["parameters"]["properties"]["action"]["enum"]
    for a in ("see", "shell", "python", "open", "type", "key", "click", "wait",
              "window", "verify", "read", "write", "list", "find", "download", "send"):
        check("computer 有 " + a, a in enum, str(enum))

    fp, body = T._read_target(ROOT, "bot.py#L1-L3")
    check("读文件支持行号锚点", fp is not None and "#!/usr/bin" in body)
    fp2, body2 = T._read_target(ROOT, "生命感与可维护性_系统设计与伪代码.md#L1-L2")
    check("读文件支持中文文件名+锚点", fp2 is not None and "QQ-DeepSeek" in body2)

    doc = (ROOT / "skills" / "电脑.md").read_text(encoding="utf-8")
    check("电脑方法进索引", bool(doc))
    for word in ("python", "click", "window", "download", "qq avatar"):
        check("电脑方法写明 " + word, word in doc, doc[:80])

    # 端到端跑一下 write → read → list（不发网络）
    root = tmp / "pc"
    root.mkdir(parents=True, exist_ok=True)

    class Dev:
        async def see(self):
            class R:
                ok = True
                evidence = "窗口：记事本"
                image = ""
                def text(self):
                    return self.evidence
            return R()

        async def act(self, kind, args):
            class R:
                ok = True
                evidence = "命令跑完了"
                image = ""
                def text(self):
                    return self.evidence
            return R()

    async def _authorize(*a, **k):
        return "", True, {"ok": True}

    services = SimpleNamespace(root=root, cfg=SimpleNamespace(owner=1, ws_url="ws://x"),
                          device=lambda n: Dev())
    old_auth = T._authorize
    T._authorize = _authorize
    try:
        blocks, used, meta = asyncio.run(T._computer(
            services, {"action": "write", "path": "out.txt", "content": "画好了"}, 1, True))
        check("能手写文件", meta.get("ok") and (root / "out.txt").exists(), str(blocks)[:80])
        blocks, used, meta = asyncio.run(T._computer(
            services, {"action": "read", "path": "out.txt"}, 1, True))
        check("能读回来", meta.get("ok") and "画好了" in str(blocks), str(blocks)[:80])
        blocks, used, meta = asyncio.run(T._computer(
            services, {"action": "list", "path": "."}, 1, True))
        check("能列目录", meta.get("ok") and "out.txt" in str(blocks), str(blocks)[:80])
        blocks, used, meta = asyncio.run(T._computer(
            services, {"action": "shell", "cmd": "echo hi"}, 1, True))
        check("能跑命令", meta.get("ok"), str(blocks)[:80])
        blocks, used, meta = asyncio.run(T._computer(
            services, {"action": "python", "code": "print('hi')"}, 1, True))
        check("能跑 python", meta.get("ok") and "hi" in str(blocks), str(blocks)[:120])
        blocks, used, meta = asyncio.run(T._computer(
            services, {"action": "shell",
                  "cmd": "cd $HOME; Get-ChildItem -Recurse -Filter *.py"}, 1, True))
        check("全盘递归被拦", not meta.get("ok") and "别在全盘" in str(blocks), str(blocks)[:120])
    finally:
        T._authorize = old_auth


def test_pc_hand_and_clixml(tmp: Path) -> None:
    """电脑上的手要在她工具表里；PowerShell 的 CLIXML 噪音要洗掉。"""
    import bot as bot_mod
    from life.services import tools as T
    from life.services.device import _clean_ps

    class Evo:
        def schemas(self):
            return T.SCHEMAS

    shell = bot_mod.Bot.__new__(bot_mod.Bot)
    shell.cfg = bot_mod.Config(text_only=True, evolution=True)
    shell.services = Evo()
    names = {t["function"]["name"] for t in shell._active_tools()}
    check("电脑的手在她工具表里", "computer" in names, str(sorted(names)[:6]))
    from life.services import ServiceHub
    exec_names = {t["function"]["name"] for t in ServiceHub.exec_schemas(None)}
    check("通道也能看见 computer", "computer" in exec_names, str(sorted(exec_names)[:8]))
    check("手机的手也在", "device" in names)
    check("自检也在", "self_check" in names)

    raw = ('#< CLIXML' + chr(10) +
           '<Objs Version="1.1.0.1"><S S="Error">找不到路径_x000D__x000A_</S></Objs>')
    clean = _clean_ps(raw)
    check("CLIXML 被洗成人话", "找不到路径" in clean and "CLIXML" not in clean, clean[:80])
    check("普通输出原样返回", _clean_ps("hello") == "hello")


def test_voice_cannot_skip_action(tmp: Path) -> None:
    """发语音不能绕过"答应了必须动手"这道闸。"""
    from collections import deque
    import bot as bot_mod
    from life.services.channel import AgenticGui

    said: list[str] = []
    spoke: list[str] = []
    seen: list[str] = []
    broken: list[str] = []

    def unanswered(messages):
        pending: set[str] = set()
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                pending = {str(c.get("id")) for c in m["tool_calls"]}
            elif m.get("role") == "tool":
                pending.discard(str(m.get("tool_call_id")))
        return pending

    class RecLLM(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            bad = unanswered(messages)
            if bad:
                broken.append(str(bad))
            seen.append(" ".join(str(m.get("content")) for m in messages))
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    # 第一步：她只想发一句语音"我这就去改"
    llm = RecLLM(steps=[
        ("", [_Call("reply", {"text": "好喵，小鱼这就去翻自己的代码看看", "voice": True})]),
        ("", [_Call("computer", {"action": "shell", "cmd": "echo hi"})]),
        ("", [_Call("reply", {"text": "改好了喵", "voice": True})]),
    ])
    host = FakeHost(tmp, cfg(), llm)
    host._sent_log = []
    host._guidance = deque()
    host._intent = {"needs_action": True, "kind": "task", "thinking": "deep"}
    host._last_voice = {}
    host._voice_odds = lambda peer, seg: 1.0     # 一定会走语音
    host._emotions_n = lambda peer: {"依恋": 6, "委屈": 0, "恼": 0, "不安": 0, "兴奋": 0}

    async def say_voice(target, text):
        spoke.append(text)
        return True

    async def reply_ok(text, expect=""):
        said.append(text)
        return True, "ok"

    acted_tools: list[str] = []

    async def dispatch_ok(name, args, target, peer):
        acted_tools.append(name + "." + str(args.get("action")))
        return "（跑完了）", {"ok": True}

    host._say_voice = say_voice

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, name, args, target, peer):
            return await dispatch_ok(name, args, target, peer)

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok, dispatch=dispatch_ok)
    asyncio.run(ch._run_agent(
        obs, [{"text": "你查看自己的代码看看能不能修改这个问题", "side": "them"}], ""))

    check("执行前文字通知已发", "我去看看这件事的具体情况。" in " ".join(host.sent),
          str(spoke + said))
    check("被逼着真去动手了", any(x.startswith("computer.") for x in acted_tools),
          str(acted_tools))
    check("动手之后才说话", bool(said or spoke), str(said + spoke))
    check("提醒里点明要动手", "intent add" in " ".join(seen) or "动手" in " ".join(seen))
    check("语音回复也补了 tool 回应", not broken, str(broken))


def test_intent_runs_now(tmp: Path) -> None:
    """念想一出现就去做，不等下一轮慢思考。"""
    from types import SimpleNamespace
    from life.services import tools as T

    ran: list = []

    class Store:
        def get_intent(self, iid):
            return {"id": iid, "text": "把事做完", "why": "答应了", "status": "seed",
                    "peer": 1, "attempts": 0}

        def update_intent(self, iid, **kw):
            ran.append(("update", iid, kw.get("status")))

        def add_intent(self, *a, **k):
            return 42

    class Cog:
        async def run_intent(self, intent, peer):
            ran.append(("run", intent["id"], peer))
            return "做完了"

    class Evo:
        def __init__(self):
            self.store = Store()
            self.cog = Cog()
            self.host = SimpleNamespace(cfg=SimpleNamespace(owner=1))
            self.owner_uid = 1
            self.budget = SimpleNamespace(allow_act=lambda **k: (True, ""))

        def log(self, msg):
            pass

    import life.services.core as C
    services = Evo()
    services.channel = SimpleNamespace(_busy=False)
    out = asyncio.run(C.ServiceHub.run_intent_now(services, 7, 1))
    check("念想会立刻被做", ("run", 7, 1) in ran, str(ran))
    check("先标成 active 再做", ("update", 7, "active") in ran, str(ran))
    check("返回结果是做完了", out == "做完了", out)

    made: list = []

    class Evo2(Evo):
        def __init__(self):
            super().__init__()

            def _add(*a, **k):
                made.append(a[1])
                return 42

            self.store.add_intent = _add

        async def run_intent_now(self, iid, peer=0):
            made.append(("now", iid))
            return ""

    evo2 = Evo2()
    blocks, used, meta = asyncio.run(T._intent(
        evo2, {"action": "add", "text": "研究界面", "why": "主人让的"}, 1))
    check("记念想时不只说先放着", "这就去做" in str(blocks), str(blocks)[:80])


def test_receive_via_napcat(tmp: Path) -> None:
    """收消息只走协议端：有推送就不去看手机。"""
    src = (ROOT / 'life' / 'services' / "channel.py").read_text(encoding="utf-8")
    check("有推送就不截图", "if woke:" in src and "msg_confidence\": 1.0" in src)
    check("没有'等屏幕同步'那段", "屏幕还没同步过来" not in src)
    check("提示语不叫她翻屏幕", "不用去翻屏幕" in src)
    check("提示词写明收消息不用看屏幕", "收消息不用看屏幕" in src)

    # 走一遍：有 NapCat 推送时，observe 一次都不该被调用
    from collections import deque
    from life.services.channel import AgenticGui

    calls = {"observe": 0}

    def obs():
        calls["observe"] += 1
        return {"image": "", "rows": [], "nodes": [], "msgs": []}

    host = FakeHost(tmp, cfg(), FakeLLM())
    host._sent_log = []
    host._guidance = deque()
    host._emotions = lambda peer: {"依恋": 6, "委屈": 0, "恼": 0, "不安": 0, "兴奋": 0}
    host._inner_block = lambda peer: ""

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = host.llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, *a, **k):
            return "ok", {"ok": True}

    async def reply_ok(text, expect=""):
        return True, "ok"

    ch = AgenticGui(Evo(), observe=obs, reply=reply_ok,
                    dispatch=lambda *a, **k: None)
    # 直接调 handle（带 NapCat 确认文本），它不应该依赖屏幕
    asyncio.run(ch.handle({"image": "", "rows": [], "nodes": [], "msgs": []},
                          [], "", woke_uin=1, woke_name="他", woke_text="在吗",
                          woke_texts=["在吗"]))
    check("有推送时不抓屏幕", calls["observe"] >= 0)     # handle 本身不观察


def test_ack_then_act(tmp: Path) -> None:
    """先应一声，再动手；应过声之后还想拖就拦。"""
    from collections import deque
    import bot as bot_mod
    from life.services.channel import AgenticGui

    said: list[str] = []
    seen: list[str] = []
    acted: list[str] = []

    class RecLLM(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            seen.append(" ".join(str(m.get("content")) for m in messages))
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    llm = RecLLM(steps=[
        ("", [_Call("reply", {"text": "好，我这就去看代码", "voice": False})]),   # 应声
        ("", [_Call("reply", {"text": "我这就去，马上", "voice": False})]),        # 又拖 → 该被拦
        ("", [_Call("computer", {"action": "write", "path": "x.txt", "content": "hi"})]),  # 真去做
        ("", [_Call("reply", {"text": "看完了，代码在 bot.py 里"})]),              # 交差
    ])
    host = FakeHost(tmp, cfg(), llm)
    host._sent_log = []
    host._guidance = deque()
    host._intent = {"needs_action": True, "kind": "task", "thinking": "fast"}
    host._voice_odds = lambda peer, seg: 0.0
    host._emotions_n = lambda peer: {"依恋": 6, "委屈": 0, "恼": 0, "不安": 0, "兴奋": 0}
    host._last_voice = {}

    async def reply_ok(text, expect=""):
        said.append(text)
        return True, "ok"

    async def dispatch_ok(name, args, target, peer):
        acted.append(name + "." + str(args.get("action")))
        return "（读到了）", {"ok": True}

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, name, args, target, peer):
            return await dispatch_ok(name, args, target, peer)

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok, dispatch=dispatch_ok)
    asyncio.run(ch._run_agent(obs, [{"text": "你看看当前代码的策略", "side": "them"}], ""))

    check("第一句执行前通知已发", host.sent and "我去看看这件事的具体情况。" in host.sent[0], str(host.sent))
    check("第二句拖的被拦下", "马上" not in " ".join(said), str(said))
    check("应声之后真去动手了", any(x.startswith("computer.") for x in acted), str(acted))
    check("最后交了差", "看完了" in " ".join(said), str(said))


def test_tool_call_ids_filled(tmp: Path) -> None:
    """一条 assistant 带多个 tool_call 时，每个 id 都要有 tool 回应，否则 400。"""
    from life.services.channel import AgenticGui

    msgs: list[dict] = []
    payload = [{"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
               {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}}]
    AgenticGui._fill_skipped(msgs, payload, "a")
    ids = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
    check("没回应的 tool_call 被补上了", "b" in ids, str(ids))


def test_closeout_by_state(tmp: Path) -> None:
    """步数用完说什么，也由状态机决定，不再靠正则。"""
    from life.services.turn import (TurnState, close_kind, close_hint,
                          CLOSE_REPORT, CLOSE_PARTIAL, CLOSE_REFUSE)

    a = TurnState(needs_action=True)          # 一句没动
    check("没动过 → 直说做不到", close_kind(a) == CLOSE_REFUSE, close_kind(a))

    b = TurnState(needs_action=True)
    b.note_sent(True)                          # 应过声
    check("应过声没做完 → 说清卡在哪", close_kind(b) == CLOSE_PARTIAL, close_kind(b))

    c = TurnState(needs_action=True)
    c.note_action()                            # 动过手
    check("做过事 → 交差", close_kind(c) == CLOSE_REPORT, close_kind(c))

    for k in (CLOSE_REPORT, CLOSE_PARTIAL, CLOSE_REFUSE):
        check("收尾有话说：" + k, bool(close_hint(k)) and "够了" in close_hint(k))

    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    check("旧的 PROMISE 兜底已删", "嘴上答应了没动手，逼一次" not in src)
    check("收尾走状态机", "close_hint(close_kind(st))" in src)


def test_hand_by_semantics(tmp: Path) -> None:
    """用哪只手（电脑/手机）也由理解层判，不靠关键词。"""
    import bot as bot_mod

    class L:
        def __init__(self, payload):
            self.payload = payload

        async def chat(self, *a, **k):
            return self.payload

        async def chat_msg(self, *a, **k):
            class M:
                content = ""
                tool_calls = []
            return M()

    def intent_of(payload, phone=True):
        b = bot_mod.Bot.__new__(bot_mod.Bot)
        b.cfg = bot_mod.Config()
        b.llm = L(payload)
        b._intent_cache = {}
        b._persona_text = lambda: ''
        # 测试里不走真 adb：直接指定这一轮手机在不在
        b._phone_up = phone
        b._phone_at = time.time()
        return asyncio.run(b.understand("随便一句", ""))

    pc = intent_of('{"kind":"task","needs_action":true,"device":"pc","complexity":3,'
                   '"thinking":"fast","goal":"画图"}')
    check("电脑上的事判给 pc", pc["device"] == "pc", str(pc))
    ph = intent_of('{"kind":"task","needs_action":true,"device":"phone","complexity":2,'
                   '"thinking":"fast","goal":"点开 QQ"}')
    check("手机上的事判给 phone", ph["device"] == "phone", str(ph))
    ch = intent_of('{"kind":"chat","needs_action":false,"device":"none","complexity":1}')
    check("闲聊不用动手", ch["device"] == "none", str(ch))
    # 要动手但没说清 → 默认手机
    un = intent_of('{"kind":"task","needs_action":true,"complexity":3}')
    check("手机在时，没说清就默认手机", un["device"] == "phone", str(un))
    # 没连手机时：模型就算说 phone，也要改路由到电脑
    no_phone = intent_of('{"kind":"task","needs_action":true,"device":"phone",'
                         '"complexity":2}', phone=False)
    check("没连手机就改走电脑", no_phone["device"] == "pc", str(no_phone))
    un2 = intent_of('{"kind":"task","needs_action":true,"complexity":3}', phone=False)
    check("没连手机时默认 pc", un2["device"] == "pc", str(un2))

    h1 = bot_mod.Bot.hand_hint({"device": "pc"})
    h2 = bot_mod.Bot.hand_hint({"device": "phone"})
    check("提示词写明用电脑", "computer" in h1 and "不要拿 device" in h1, h1)
    check("提示词写明用手机", "device" in h2 and "电脑上的事别用手机" in h2, h2)

    src = (ROOT / 'life' / 'services' / "channel.py").read_text(encoding="utf-8")
    check("通道把用手写进提示词", "hand_hint(intent)" in src)


def test_image_in(tmp: Path) -> None:
    """他发来的图要真进到她眼前，不是一串 CQ 码。"""
    from collections import deque
    from life.services.watch import NapCatWatch
    from life.services.channel import AgenticGui

    text, imgs = NapCatWatch._parse({"message": [
        {"type": "text", "data": {"text": "看看这个"}},
        {"type": "image", "data": {"url": "https://x/1.jpg"}}]})
    check("数组格式能拆出图", text == "看看这个" and imgs == ["https://x/1.jpg"], str((text, imgs)))

    text2, imgs2 = NapCatWatch._parse({"message": "[CQ:image,file=abc,url=https://y/2.jpg]"})
    check("CQ 码能拆出图", imgs2 == ["https://y/2.jpg"] and text2 == "", str((text2, imgs2)))
    check("CQ 码不会当成正文留下", "CQ:image" not in text2, text2)

    text3, imgs3 = NapCatWatch._parse({"message": "在吗"})
    check("纯文字照常", text3 == "在吗" and not imgs3, str((text3, imgs3)))

    # 图片要真的贴进这一轮的消息里
    seen: list = []

    class L(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            seen.append(messages[-1].get("content"))
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    llm = L(steps=[("看完了喵", [])])
    host = FakeHost(tmp, cfg(), llm)
    host._sent_log = []
    host._guidance = deque()
    host._inner_block = lambda peer: ""
    host._intent = {"needs_action": False, "kind": "chat", "device": "none"}

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

        async def dispatch_text(self, *a, **k):
            return "ok", {"ok": True}

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    async def reply_ok(text, expect=""):
        return True, "ok"

    async def dispatch_ok(*a, **k):
        return "ok", {"ok": True}

    ch = AgenticGui(Evo(), observe=lambda: obs,
                    reply=reply_ok, dispatch=dispatch_ok)
    asyncio.run(ch._run_agent(obs, [{"text": "看看这个", "side": "them"}], "", peer=1,
                              images=["https://x/1.jpg"]))
    last = seen[-1] if seen else None
    has_img = isinstance(last, list) and any(
        b.get("type") == "image_url" for b in last if isinstance(b, dict))
    check("图真的贴进了这一轮", has_img, str(last)[:120])
    text_blocks = [str(b.get("text") or "") for b in (last or [])
                   if isinstance(b, dict) and b.get("type") == "text"]
    check("图片 URL 也写进了正文", any("https://x/1.jpg" in t for t in text_blocks),
          str(text_blocks)[:160])


def test_message_not_lost_in_cooldown(tmp: Path) -> None:
    """失败冷却不能把他新发的话取走又丢掉。"""
    from collections import deque
    from life.services.channel import AgenticGui

    logs: list[str] = []
    host = FakeHost(tmp, cfg(), FakeLLM())
    services = SimpleNamespace(host=host, cfg=host.cfg,
                          owner_inbox=deque(), log=lambda m: logs.append(m))
    ch = AgenticGui(services)
    ch._next_try = time.time() + 30
    check("冷却时没有新消息就等", ch._cooling_down())
    msg = {"text": "听听看你的看法呢", "images": []}
    services.owner_inbox.append(msg)
    check("新消息让冷却作废", not ch._cooling_down())
    check("新消息还在信箱里、没被取走", list(services.owner_inbox) == [msg],
          str(list(services.owner_inbox)))
    check("冷却计时清零", ch._next_try == 0.0, str(ch._next_try))
    check("冷却作废有日志", any("冷却作废" in x for x in logs), str(logs))

    services.owner_inbox.clear()
    ch._next_try = time.time() + 30
    ch._retry.append({"text": "上一轮没回出去的话"})
    check("只有待重试时仍然等冷却", ch._cooling_down())

    ch._retry.clear()
    ch._remember_retry([{"text": "没回出去的那句", "side": "them"}], 1,
                       "樱花", ["https://x/1.jpg"])
    kept = list(ch._retry)[-1] if ch._retry else {}
    check("没回出去的话留下来重试",
          kept.get("text") == "没回出去的那句"
          and kept.get("images") == ["https://x/1.jpg"], str(kept))


def test_hand_hint_goes_into_prompt(tmp: Path) -> None:
    """理解层判的 pc/phone 要真的写进这一轮提示词。"""
    from collections import deque
    from life.services.channel import AgenticGui

    seen: list[str] = []

    class L(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            seen.append(str(messages[0].get("content") or ""))
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    llm = L(steps=[("好的喵", [])])
    host = FakeHost(tmp, cfg(), llm)
    host._intent = {"needs_action": False, "kind": "chat",
                    "device": "pc", "thinking": "fast"}
    host._sent_log = []
    host._guidance = deque()
    host._inner_block = lambda peer: ""
    host.hand_hint = lambda intent: "HAND:" + str((intent or {}).get("device"))

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}

    async def reply_ok(text, expect=""):
        return True, "ok"

    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok,
                    dispatch=lambda *a, **k: None)
    asyncio.run(ch._run_agent(obs, [{"text": "看看这个", "side": "them"}], ""))
    head = seen[0] if seen else ""
    check("用手提示真的进了系统提示词", "HAND:pc" in head, head[-160:])



def test_tool_messages_repaired(tmp: Path) -> None:
    """assistant 的多个 tool_call 后面必须紧跟各自的 tool 回应。"""
    from life.services.channel import AgenticGui

    def call(i, name="reply"):
        return {"id": i, "type": "function",
                "function": {"name": name, "arguments": "{}"}}

    # 历史 400 的形状：tool a、user 提示、tool b 交错
    msgs = [
        {"role": "assistant", "content": "",
         "tool_calls": [call("a"), call("b", "device")]},
        {"role": "tool", "tool_call_id": "a", "content": "发出去了"},
        {"role": "user", "content": "（应声发出去了，现在去动手）"},
        {"role": "tool", "tool_call_id": "b", "content": "没成"},
    ]
    AgenticGui._repair_tool_messages(msgs)
    check("tool 回应收回到 assistant 后面",
          [m.get("role") for m in msgs] == ["assistant", "tool", "tool", "user"],
          str([m.get("role") for m in msgs]))
    check("两个 tool_call 都还在",
          [m.get("tool_call_id") for m in msgs[1:3]] == ["a", "b"],
          str([m.get("tool_call_id") for m in msgs[1:3]]))
    check("user 提示被挪到 tool 后面",
          "动手" in msgs[-1].get("content", ""), str(msgs[-1]))

    # 漏了 tool 结果：补占位，不能把下一轮请求打成 400
    msgs2 = [
        {"role": "assistant", "content": "",
         "tool_calls": [call("x")]},
        {"role": "user", "content": "接着说"},
    ]
    AgenticGui._repair_tool_messages(msgs2)
    check("漏掉的 tool 回应补占位",
          msgs2[1].get("role") == "tool"
          and msgs2[1].get("tool_call_id") == "x",
          str(msgs2))

    # tool_calls=[] 也会被 API 拒：空成这样就删掉/整条丢掉
    msgs3 = [{"role": "assistant", "content": "", "tool_calls": []},
             {"role": "user", "content": "接着说"}]
    AgenticGui._repair_tool_messages(msgs3)
    check("空 content + 空 tool_calls 整条丢掉",
          len(msgs3) == 1 and msgs3[0].get("role") == "user", str(msgs3))
    msgs4 = [{"role": "assistant", "content": "我还在想", "tool_calls": []}]
    AgenticGui._repair_tool_messages(msgs4)
    check("有 content 时只删除空 tool_calls 字段",
          "tool_calls" not in msgs4[0] and msgs4[0].get("content") == "我还在想",
          str(msgs4))



def test_multi_tool_call_order(tmp: Path) -> None:
    """一条 assistant 带两个 tool_call、中间又插了 user 提示时，下一轮不能 400。"""
    from collections import deque
    from life.services.channel import AgenticGui

    invalid: list[str] = []

    def bad_order(messages) -> str:
        for i, m in enumerate(messages):
            if m.get("role") != "assistant" or not m.get("tool_calls"):
                continue
            ids = [str(c.get("id")) for c in m["tool_calls"]]
            following = messages[i + 1:i + 1 + len(ids)]
            got = [str(x.get("tool_call_id") or "") for x in following]
            if [x.get("role") for x in following] != ["tool"] * len(ids) or got != ids:
                return f"assistant calls={ids} followed by {[(x.get('role'), x.get('tool_call_id')) for x in following]}"
        return ""

    class ValidatingLLM(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            bad = bad_order(messages)
            if bad:
                invalid.append(bad)
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    llm = ValidatingLLM(steps=[
        # 第一步：先应声（进入 ACK）
        ("", [_Call("reply", {"text": "好喵，我去看看", "voice": False}, 0)]),
        # 第二步：一次两个 tool_call；第一个失败后插 user 提示，第二个又被拦
        ("", [_Call("device", {"action": "wait"}, 0),
              _Call("reply", {"text": "这就去", "voice": False}, 1)]),
        # 第三步：收尾
        ("", [_Call("reply", {"text": "看完了喵", "voice": False}, 0)]),
    ])
    host = FakeHost(tmp, cfg(), llm)
    host._sent_log = []
    host._guidance = deque()
    host._intent = {"needs_action": True, "kind": "task", "thinking": "fast"}
    host._voice_odds = lambda peer, seg: 0.0
    host._emotions_n = lambda peer: {"依恋": 6, "委屈": 0, "恼": 0, "不安": 0, "兴奋": 0}
    host._last_voice = {}

    async def reply_ok(text, expect=""):
        return True, "ok"

    async def dispatch_fail(name, args, target, peer):
        return "（这个动作不支持）", {"ok": False}

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok, dispatch=dispatch_fail)
    asyncio.run(ch._run_agent(obs, [{"text": "你看看当前代码", "side": "them"}], ""))
    check("多 tool_call 的下一轮结构合法", not invalid, str(invalid[:2]))



def test_empty_model_response(tmp: Path) -> None:
    """模型空响应不能变成 assistant.tool_calls=[] 再打到 API 上。"""
    from collections import deque
    from life.services.channel import AgenticGui

    bad: list[str] = []
    sent: list[str] = []

    class L(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            for m in messages:
                if (m.get("role") == "assistant" and "tool_calls" in m
                        and not m.get("tool_calls")):
                    bad.append(str(m))
            return await super().chat_msg(messages, tools=tools, tag=tag, **kw)

    llm = L(steps=[("", []), ("好的喵，我在呢", [])])
    host = FakeHost(tmp, cfg(), llm)
    host._sent_log = []
    host._guidance = deque()
    host._intent = {"needs_action": False, "kind": "chat",
                    "device": "none", "thinking": "fast"}
    host._inner_block = lambda peer: ""

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

    async def reply_ok(text, expect=""):
        sent.append(text)
        return True, "ok"

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok,
                    dispatch=lambda *a, **k: None)
    asyncio.run(ch._run_agent(obs, [{"text": "在吗", "side": "them"}], ""))
    check("空响应后还能继续回话", sent == ["好的喵，我在呢"], str(sent))
    check("空 tool_calls 不会被发给 API", not bad, str(bad[:1]))



def test_avatar_action(tmp: Path) -> None:
    """她的第二条路要能真的落地：qq avatar 调 NapCat 的 set_qq_avatar。"""
    from types import SimpleNamespace
    from life.services import tools as T
    from life.services import onebot

    calls: list[tuple[str, dict]] = []
    old_auth = T._authorize
    old_set = onebot.set_avatar

    async def _auth(*a, **k):
        return "", True, {"ok": True}

    async def _set(ws_url, file):
        calls.append((ws_url, {"file": file}))
        return True, "头像换好了"

    T._authorize = _auth
    onebot.set_avatar = _set
    try:
        services = SimpleNamespace(root=tmp, cfg=SimpleNamespace(owner=1, ws_url="ws://x"))
        blocks, used, meta = asyncio.run(T._qq(
            services, {"action": "avatar", "file": "https://x/a.jpg"}, 1, True))
        check("qq avatar 真去调了 OneBot", bool(calls) and calls[0][1]["file"] == "https://x/a.jpg",
              str(calls))
        check("换头像成功有证据", meta.get("ok") and "换好了" in str(blocks), str(blocks))
    finally:
        T._authorize = old_auth
        onebot.set_avatar = old_set

    # data URL 要转成 NapCat 认识的 base64://
    seen: list[tuple[str, dict]] = []

    async def _call(ws_url, action, params, timeout=20.0):
        seen.append((action, params))
        return {"retcode": 0, "status": "ok"}

    old_call = onebot.call
    onebot.call = _call
    try:
        ok, note = asyncio.run(onebot.set_avatar(
            "ws://x", "data:image/png;base64,AAAA"))
        check("data URL 转 base64://", ok and seen
              and seen[0][0] == "set_qq_avatar"
              and seen[0][1]["file"] == "base64://AAAA", str(seen))

        seen.clear()
        ok, note = asyncio.run(onebot.send_image(
            "ws://x", 1, "https://x/a.jpg", "看这张"))
        msg = seen[0][1].get("message") if seen else []
        check("发图走 NapCat 图片消息", ok and seen
              and seen[0][0] == "send_private_msg"
              and any(p.get("type") == "image" for p in msg)
              and any(p.get("type") == "text" for p in msg), str(seen))
    finally:
        onebot.call = old_call



def test_reply_image_path(tmp: Path) -> None:
    """reply(image=...) 要真的走发图那条路，不是当文字发。"""
    from collections import deque
    from life.services.channel import AgenticGui

    sent_img: list[tuple[str, str]] = []

    class L(ScriptedLLM):
        pass

    llm = L(steps=[("", [_Call("reply", {"text": "看这张", "image": "https://x/a.jpg"}, 0)])])
    host = FakeHost(tmp, cfg(), llm)
    host._sent_log = []
    host._guidance = deque()
    host._intent = {"needs_action": False, "kind": "chat",
                    "device": "none", "thinking": "fast"}
    host._inner_block = lambda peer: ""

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

    async def reply_ok(text, expect=""):
        return True, "ok"

    async def reply_image_ok(image, text="", expect=""):
        sent_img.append((image, text))
        return True, "图片发出去了"

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok,
                    reply_image=reply_image_ok, dispatch=lambda *a, **k: None)
    ok = asyncio.run(ch._run_agent(obs, [{"text": "发张图看看", "side": "them"}], ""))
    check("回了图片", ok and sent_img == [("https://x/a.jpg", "看这张")], str(sent_img))



def test_reply_napcat_only(tmp: Path) -> None:
    """回话/发图只走 NapCat；协议端失败也不许偷偷退回模拟器。"""
    from types import SimpleNamespace
    from life.services.channel import AgenticGui
    from life.services import onebot
    import qqgui

    gui_calls: list[str] = []
    old_text = onebot.send_text
    old_img = onebot.send_image
    old_gui = qqgui.send

    async def _text(ws, uid, text):
        return False, "协议端断了"

    async def _img(ws, uid, image, text="", group=False):
        return True, "图片发出去了"

    def _gui(text, chat=None):
        gui_calls.append(text)
        return True

    onebot.send_text = _text
    onebot.send_image = _img
    qqgui.send = _gui

    async def _throttle(uid):
        return None

    try:
        host = SimpleNamespace(_throttle=_throttle, _sent_log=[])
        services = SimpleNamespace(
            host=host, root=tmp,
            cfg=SimpleNamespace(owner=1, ws_url="ws://x", gui_chat="", gui_poll=0.5),
            log=lambda m: None)
        ch = AgenticGui(services)
        ok, note = asyncio.run(ch._reply_default("在吗"))
        check("文字失败不退回模拟器", (not ok) and not gui_calls, str((ok, note, gui_calls)))
        ok, note = asyncio.run(ch._reply_image_default("https://x/a.jpg", "看图"))
        check("图片走 NapCat 默认路", ok and not gui_calls, str((ok, note, gui_calls)))
    finally:
        onebot.send_text = old_text
        onebot.send_image = old_img
        qqgui.send = old_gui



def test_reasoning_content_kept(tmp: Path) -> None:
    """DeepSeek 深度思考模式：带 tool_calls 的 assistant 必须把 reasoning_content 带回去。"""
    from collections import deque
    from life.services.channel import AgenticGui

    calls: list[list] = []

    class RLLM(ScriptedLLM):
        async def chat_msg(self, messages, tools=None, tag="", **kw):
            calls.append([dict(m) for m in messages])
            m = await super().chat_msg(messages, tools=tools, tag=tag, **kw)
            m.reasoning_content = "我先想想：这步该跑个命令看看。"
            return m

    llm = RLLM(steps=[
        ("", [_Call("computer", {"action": "shell", "cmd": "echo hi"}, 0)]),
        ("跑完了喵", []),
    ])
    host = FakeHost(tmp, cfg(), llm)
    host._sent_log = []
    host._guidance = deque()
    host._intent = {"needs_action": True, "kind": "task",
                    "device": "pc", "thinking": "deep"}
    host._inner_block = lambda peer: ""

    class Evo:
        def __init__(self):
            self.host = host
            self.llm = llm
            self.cfg = host.cfg
            self.root = tmp

        def voice(self):
            return "你叫小鱼。"

        def exec_schemas(self):
            return []

        def log(self, msg):
            pass

    async def reply_ok(text, expect=""):
        return True, "ok"

    async def dispatch_ok(name, args, target, peer):
        return "ok", {"ok": True}

    obs = {"image": "", "rows": [], "nodes": [], "msgs": []}
    ch = AgenticGui(Evo(), observe=lambda: obs, reply=reply_ok, dispatch=dispatch_ok)
    asyncio.run(ch._run_agent(obs, [{"text": "跑个命令看看", "side": "them"}], ""))
    second = calls[1] if len(calls) > 1 else []
    kept = [m.get("reasoning_content") for m in second
            if m.get("role") == "assistant" and m.get("tool_calls")]
    check("tool_calls 的 reasoning_content 带回去了", any(kept), str(kept))



def test_partial_send_does_not_repeat(tmp: Path) -> None:
    """多行回复只发出去一半时，不能把整句再自动重发一遍。"""
    from types import SimpleNamespace
    from life.services.channel import AgenticGui
    from life.services import onebot

    old = onebot.send_text

    calls = {"n": 0}

    async def _send(ws, uid, line):
        calls["n"] += 1
        if calls["n"] == 1:
            return True, "发出去了"
        return False, "TimeoutError: timed out"

    async def _throttle(uid):
        return None

    onebot.send_text = _send
    try:
        host = SimpleNamespace(_throttle=_throttle, _sent_log=[])
        services = SimpleNamespace(
            host=host, root=tmp,
            cfg=SimpleNamespace(owner=1, ws_url="ws://x", gui_chat="", gui_poll=0.5),
            log=lambda m: None)
        ch = AgenticGui(services)
        long_text = ("第一段" + "啊" * 3100 + chr(10) + "第二段" + "啊" * 300)
        ok, note = asyncio.run(ch._reply_default(long_text))
        check("部分发送返回失败但有条数",
              (not ok) and "只发出去 1/2" in note, str((ok, note)))
        check("部分发送有标记", ch._partial_reply_sent)
        check("超时算状态不确定", ch._reply_uncertain)
    finally:
        onebot.send_text = old



def test_tool_args_validation(tmp: Path) -> None:
    """坏 JSON / 混入 DSML / 缺必填 / 枚举不对，都不能直接打到工具上。"""
    from life.services.channel import AgenticGui
    from life.services import tools as T

    schemas = {t["function"]["name"]: t["function"] for t in T.SCHEMAS}
    bad_dsml = {"action": 'search">\n<｜｜DSML｜｜ parameter name="path" string="true">evo',
                "pattern": "CREATE TABLE"}
    check("DSML 参数被拦",
          "工具语法" in AgenticGui._tool_args_error("code", bad_dsml, schemas))
    check("action 枚举被拦",
          "不在允许值" in AgenticGui._tool_args_error(
              "code", {"action": "search2", "pattern": "x"}, schemas))
    check("skill.search 缺 text 被拦",
          "缺参数：text" in AgenticGui._tool_args_error(
              "skill", {"action": "search"}, schemas))
    check("code.search 缺 pattern 被拦",
          "缺参数：pattern" in AgenticGui._tool_args_error(
              "code", {"action": "search"}, schemas))
    src = (ROOT / 'life' / 'services' / "channel.py").read_text(encoding="utf-8")
    check("think_deeper 不再中途切 deep",
          'if name == "think_deeper":' in src and 'mode = "deep"\n                    self.evo.log("她说这句值得想一遍' not in src,
          "检查 think_deeper 分支")



def test_owner_interrupt(tmp: Path) -> None:
    """她做事中途，主人发来的话要能插进去；叫停要能立刻挂起。"""
    from collections import deque
    from types import SimpleNamespace
    from life.services.channel import AgenticGui

    host = FakeHost(tmp, cfg(), FakeLLM())
    host._sent_log = []
    services = SimpleNamespace(host=host, cfg=host.cfg, root=tmp,
                          owner_inbox=deque(), log=lambda m: None)
    ch = AgenticGui(services)
    check("叫停词识别：停下", ch.is_stop_text("停下"))
    check("叫停词识别：别做了", ch.is_stop_text("别做了喵"))
    check("普通插话不误判", not ch.is_stop_text("先看一下这个文件"))
    check("斜杠停也识别", ch.is_stop_text("/停"))

    msgs: list[dict] = []
    services.owner_inbox.append({"text": "别做了，先停", "images": [], "uin": 1, "name": "主人"})
    hit = ch._drain_owner_interrupts(msgs)
    check("叫停会挂起", hit and ch._halt)
    check("叫停的话留在信箱给外层回", len(services.owner_inbox) == 1, str(services.owner_inbox))

    ch._halt = False
    msgs.clear()
    services.owner_inbox.clear()
    services.owner_inbox.append({"text": "顺便看看这个", "images": [], "uin": 1, "name": "主人"})
    hit = ch._drain_owner_interrupts(msgs)
    check("普通插话会插进当前这轮", hit and msgs and "顺便看看这个" in msgs[-1]["content"],
          str(msgs[-1:]))
    check("普通插话取走后不会重复回", not services.owner_inbox)



def test_deep_token_budget(tmp: Path) -> None:
    """深想时 reasoning 也算 max_tokens；400 会被截成空响应。"""
    import bot as bot_mod
    from types import SimpleNamespace

    calls: list[dict] = []

    class Comp:
        def __init__(self, finish="stop", content="ok", calls_list=None):
            self.finish = finish
            self.content = content
            self.calls_list = calls_list

        async def create(self, **kw):
            calls.append(kw)
            msg = SimpleNamespace(content=self.content,
                                  tool_calls=self.calls_list,
                                  reasoning_content="想了很多")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg, finish_reason=self.finish)],
                usage=None)

    def make_bot(comp):
        b = bot_mod.DeepSeek.__new__(bot_mod.DeepSeek)
        b.cfg = bot_mod.Config(max_tokens=400, max_tokens_deep=3000)
        b.client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
        b._trace = lambda *a, **k: None
        b._meter = lambda r: None
        b._offline = lambda: False
        return b

    asyncio.run(make_bot(Comp()).chat_msg([], thinking=True))
    check("深想传 max_tokens_deep", calls and calls[-1].get("max_tokens") == 3000,
          str(calls[-1].get("max_tokens") if calls else None))
    try:
        asyncio.run(make_bot(Comp(finish="length", content="")).chat_msg([], thinking=True))
        raised = False
    except RuntimeError as e:
        raised = "截断" in str(e)
    check("深想被截断会明确报错", raised)



def test_prompt_block(tmp: Path) -> None:
    services, host = make_evo(tmp)
    services.store.add_intent(1, "想学会发说说", "上次没发出去", "mastery", "", 0.8)
    block = services.prompt_block(1)
    check("提示块有念想", "想学会发说说" in block and "你自己" in block)
    check("改完代码不用报细节", "不用把 diff" in block, block[-200:])
    host.cfg.evolution = False
    check("关掉后不写块", services.prompt_block(1) == "")
    services.close()


# ---------------------------------------------------------------- 第二版

def test_store_v2(tmp: Path) -> None:
    from life.services.store import Store
    s = Store(tmp / "data" / "v2.db")
    i = s.upsert_identity(1, "主人", "owner", 100)
    check("身份入库", i > 0 and s.get_identity(1)["relation"] == "owner")
    s.add_thought_event(1, "hypothesis", "他可能只是累了")
    check("思考事件入库", s.recent_thought_events(1)[-1]["kind"] == "hypothesis")
    tid = s.start_decision_trace(1, origin="initiative", trigger="在吗", why="想他")
    s.update_decision_trace(tid, thoughts=[{"text": "先回一句"}], outcome="ok")
    tr = s.get_decision_trace(tid)
    check("决策轨迹可回放", tr and tr["thoughts"][0]["text"] == "先回一句" and tr["outcome"] == "ok")
    rid = s.add_consent_rule(1, "qq", "qq.send", "ask")
    check("授权规则", s.list_consent_rules(1)[0]["id"] == rid)
    cid = s.add_consent_request(1, "发消息", "想他", ["social"], action={"tool": "qq", "args": {}})
    check("主动申请入库", s.get_consent_request(cid)["status"] == "pending")
    s.decide_consent_request(cid, True)
    check("主动申请可决定", s.get_consent_request(cid)["status"] == "approved")
    s.outbox_add("o1", "qq:1", "你好")
    check("outbox 入库", s.outbox_get("o1")["status"] == "pending")
    s.app_map_save("qq", "sig", "聊天", ["发送"])
    check("App 地图入库", bool(s.app_map_list("qq")))
    s.close()


def test_identity_consent(tmp: Path) -> None:
    from life.services.consent import ASK, FREE, ConsentGate, IntentEnvelope
    from life.services.identity import IdentityBook
    from life.services.store import Store
    s = Store(tmp / "data" / "identity.db")
    c = cfg(owner=1, friends=[2], mistresses=[3], exploration_read_free=True,
            initiative_read_free=True, known_people={"小明": 2})
    book = IdentityBook(s, c)
    book.sync_from_config()
    check("主人识别", book.relation(1) == "owner")
    check("朋友识别", book.relation(2) == "friend")
    check("陌生人识别", book.relation(9) == "stranger")
    check("名字能对上号", book.resolve_name("小明") == 2)
    check("主人可用执行", book.can_use_tool(1, "run_cmd")[0])
    check("朋友不能用执行", not book.can_use_tool(2, "run_cmd")[0])
    check("朋友可以查网页", book.can_use_tool(2, "web")[0])
    gate = ConsentGate(s, c, book)
    env = IntentEnvelope("发消息", "想他", origin="initiative",
                         effects=["social", "message"], action={"tool": "qq", "args": {"action": "send"}})
    check("主动发消息默认要问", gate.evaluate(env, "initiative").level == ASK)
    env2 = IntentEnvelope("主人让我发消息", origin="commanded",
                          effects=["social", "message"], action={"tool": "qq", "args": {"action": "send"}})
    check("主人吩咐直接做", gate.evaluate(env2, "commanded").level == FREE)
    # 自己主动的只读动作不再问；会写/会跑代码的才问
    read_env = IntentEnvelope("看看目录", origin="initiative", effects=["read"],
                              action={"tool": "computer",
                                      "args": {"action": "shell", "cmd": "Get-ChildItem"}})
    check("主动只读不用问", gate.evaluate(read_env, "initiative").level == FREE)
    check("Get-ChildItem 判为只读",
          ConsentGate.classify("computer", {"action": "shell",
                                            "cmd": "Get-ChildItem -Path ."}) == ("green", ["read"]))
    check("dir/echo 判为只读",
          ConsentGate.classify("computer", {"action": "shell",
                                            "cmd": "dir /b; echo ---; dir /b .."})[0] == "green")
    check("Set-Content 不算只读",
          ConsentGate.classify("computer", {"action": "shell",
                                            "cmd": "Set-Content a.txt hi"})[0] == "yellow")
    check("python 不算只读",
          ConsentGate.classify("computer", {"action": "python",
                                            "code": "print(1)"})[0] == "yellow")
    py_env = IntentEnvelope("跑 python", origin="initiative", effects=["code"],
                            action={"tool": "computer",
                                    "args": {"action": "python", "code": "print(1)"}})
    check("普通 python 仍要问", gate.evaluate(py_env, "initiative").level == ASK)

    # 计划级授权：批准一次，后续工具步骤不再逐条问
    plan_env = IntentEnvelope("装插件的后续步骤", origin="initiative", conversation_id="qq:private:1",
                              effects=["command"],
                              action={"tool": "computer",
                                      "args": {"action": "shell", "cmd": "Set-Content x y"}})
    plan_env.bind_task()
    s.add_consent_rule(1, "*", "*", "free",
                       conditions={"plan": True, "task_id": plan_env.task_id,
                                   "conversation_id": plan_env.conversation_id,
                                   "effects": ["command", "read", "network"]})
    check("计划级授权覆盖后续步骤",
          gate.evaluate(plan_env, "initiative").level == FREE)
    outside = IntentEnvelope("别的事", origin="initiative", effects=["social", "message"],
                             action={"tool": "qq", "args": {"action": "send"}})
    check("计划级授权不覆盖别的工具",
          gate.evaluate(outside, "initiative").level == ASK)
    dangerous = IntentEnvelope("付款", origin="initiative", effects=["payment"],
                               action={"tool": "computer", "args": {"action": "shell"}})
    check("计划级授权不覆盖付款/不可逆",
          gate.evaluate(dangerous, "initiative").level == ASK)
    code_env = IntentEnvelope("改自己的代码", origin="initiative",
                              effects=["code", "write_self"],
                              action={"tool": "code",
                                      "args": {"action": "edit", "path": "bot.py"}})
    check("自己改代码不用问", gate.evaluate(code_env, "initiative").level == FREE)
    card = gate.card(88, IntentEnvelope(
        "改自己的代码", "日记写得太笼统", origin="initiative",
        effects=["code"], action={"tool": "computer",
                                  "args": {"action": "shell", "cmd": "secret-command"}}))
    check("审批卡不塞命令细节",
          "工具：computer" in card and "secret-command" not in card, card[-120:])
    ws_env = IntentEnvelope("改自己的代码", origin="initiative",
                            effects=["code", "write_self"],
                            action={"tool": "workshop", "args": {"action": "apply"}})
    check("工坊改代码不用问", gate.evaluate(ws_env, "initiative").level == FREE)
    s.add_consent_rule(1, "qq", "qq.send", "free")
    check("长期授权覆盖默认", gate.evaluate(env, "initiative").level == FREE)
    check("硬边界不被授权覆盖",
          gate.evaluate(IntentEnvelope("交密钥", effects=["secrets"]), "commanded").level == "blocked")
    s.close()

    from life.services.trace import TraceRecorder
    s2 = Store(tmp / "data" / "trace.db")
    tr = TraceRecorder(s2)
    tid = tr.start(1, origin="initiative", trigger="在吗", why="想他")
    tr.add_thought("先回一句")
    tr.add_tool("qq", {"action": "send"}, "发出去了", True, "气泡出现")
    tr.finish("已回复", lesson="短句更好")
    text = tr.trace_text(tid)
    check("轨迹含想法", "先回一句" in text)
    check("轨迹含证据", "气泡出现" in text and "学到" in text)
    s2.close()

    from life.services.apps import AppWorld, Observation
    s3 = Store(tmp / "data" / "apps.db")
    aw = AppWorld(s3, c)
    aw.save_map(Observation("qq", app="com.tencent.mobileqq", title="聊天",
                            nodes=["发送", "输入框"], signature="sig1"))
    check("App 地图能保存", "qq" in aw.map_text("qq"))
    s3.close()


def test_plan_consent(tmp: Path) -> None:
    """一次批准要覆盖整件事，而不是每个步骤刷一张卡。"""
    from life.services.consent import FREE, IntentEnvelope

    services, host = make_evo(tmp, FakeLLM(["好呀喵"]))
    asks: list[str] = []

    async def _ask(hint, why, peer):
        return "主人，我想给自己装个记忆插件，可以嘛喵？"

    async def _send(target, text):
        asks.append(text)

    services._natural_ask = _ask
    host._send = _send
    env1 = IntentEnvelope("给自己装一个记忆插件", "想要记住更多",
                          origin="initiative", effects=["install"], action={})
    env2 = IntentEnvelope("给自己装一个记忆插件", "想要记住更多",
                          origin="initiative", effects=["command"],
                          action={"tool": "computer",
                                  "args": {"action": "shell", "cmd": "Set-Content a b"}})
    asyncio.run(services.request_consent(1, env1))
    second = asyncio.run(services.request_consent(1, env2))
    pend = services.store.pending_consent_requests()
    check("同一件事只留一张卡", len(pend) == 1, str(pend))
    check("重复申请不再刷屏", "已经问过" in second, second)
    cid = int(pend[0]["id"])
    asyncio.run(services.approve_consent(cid, 1))
    rules = [r for r in services.store.list_consent_rules(1)
             if (r.get("conditions") or {}).get("plan")]
    check("批准后建立计划级授权", bool(rules), str(rules)[:200])
    check("后续步骤免申请",
          services.consent.evaluate(env2, "initiative").level == FREE)
    check("旧卡一起收掉", not services.store.pending_consent_requests())
    services.close()



def test_natural_approval(tmp: Path) -> None:
    services, host = make_evo(tmp, FakeLLM(["主人，我想用一下 DSH，可以嘛喵？"]))
    ask = asyncio.run(services._natural_ask("用 DSH 跑个小命令", "想看看插件", 1))
    check("审批问话是她自己的语气", "可以嘛喵" in ask and "审批" not in ask, ask)
    aid = services.store.add_approval(1, {"tool": "trace", "args": {"action": "thought", "text": "批准后记下"}},
                                 "red", "测试")
    out = asyncio.run(services.natural_approval("可以呀", 1))
    check("自然说可以能批准", services.store.get_approval(aid)["status"] == "approved", str(out))
    check("批准结果不再念编号", "#" not in (out or ""), str(out))
    services.close()

def test_outbox(tmp: Path) -> None:
    from life.services.outbox import Outbox
    from life.services.store import Store
    s = Store(tmp / "data" / "outbox.db")
    box = Outbox(s)
    calls = []

    async def sender(text):
        calls.append(text)
        return True, "ok"

    async def verifier(text):
        return True, "气泡出现"

    oid = box.create("qq:1", "你好")
    ok1, note1 = asyncio.run(box.deliver(oid, sender, verifier))
    ok2, note2 = asyncio.run(box.deliver(oid, sender, verifier))
    check("outbox 发出", ok1 and ok2 and len(calls) == 1, f"{ok1} {ok2} {calls}")
    s.close()


def test_workshop(tmp: Path) -> None:
    from life.services.store import Store
    from life.services.workshop import Workshop
    root = tmp / "wsroot"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text("x = 1\n", "utf-8")
    (root / "test_ok.py").write_text("import sys\nsys.exit(0)\n", "utf-8")
    s = Store(tmp / "data" / "workshop.db")
    c = SimpleNamespace(workshop_test_scripts=["test_ok.py"])
    w = Workshop(root, s, c)
    r = w.apply_edits([{"path": "a.py", "old": "x = 1", "new": "x = 2"}],
                      "测试工坊", run_tests=True)
    check("工坊改成功", r.get("ok") and (root / "a.py").read_text("utf-8").strip() == "x = 2", str(r))
    eid = r.get("edit_id")
    ok, note = w.rollback(eid)
    check("工坊能回滚", ok and (root / "a.py").read_text("utf-8").strip() == "x = 1", note)
    (root / "test_bad.py").write_text("import sys\nsys.exit(1)\n", "utf-8")
    c2 = SimpleNamespace(workshop_test_scripts=["test_bad.py"])
    w2 = Workshop(root, s, c2)
    r2 = w2.apply_edits([{"path": "a.py", "old": "x = 1", "new": "x = 3"}],
                        "失败回滚", run_tests=True)
    check("测试失败自动回滚", (not r2.get("ok")) and (root / "a.py").read_text("utf-8").strip() == "x = 1", str(r2))
    check("受保护路径拒绝", not w2._safe("config.toml")[0])
    s.close()


def main() -> None:
    print("evotest：她自己长的那部分")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print("[1] 库")
        test_store(tmp)
        print("[2] 方法库")
        test_procedures(tmp)
        print("[3] 风险")
        test_risk()
        print("[4] 预算")
        test_budget(tmp)
        print("[5] 学习")
        test_learn(tmp)
        print("[6] 工具")
        test_evo_tools(tmp)
        print("[7] 审批")
        test_approval(tmp)
        print("[8] 判定")
        test_think()
        print("[9] 和 bot 接线")
        test_bot_wiring(tmp)
        print("[10] 完整一条链")
        test_tick(tmp)
        print("[11] 提示块")
        test_prompt_block(tmp)
        print("[12] 眼睛：找工具")
        test_caps(tmp)
        print("[13] 手：装/用工具")
        test_capability_tool(tmp)
        print("[14] DSH 本身")
        test_dsh(tmp)
        print("[15] 通道是 agent")
        test_channel(tmp)
        print("[15b] 答应了就要真做")
        test_channel_promise(tmp)
        print("[15c] 历史消息不能丢")
        test_channel_history(tmp)
        print("[15d] 发说说只能用手眼")
        test_no_post_moment(tmp)
        print("[15e] 时间分隔条不算消息")
        test_no_phantom_reply(tmp)
        print("[20] 命令口递话")
        test_console_path(tmp)
        test_guidance_reaches_running_turn(tmp)
        test_console_state(tmp)
        print("[22] 两条路要分清")
        test_two_paths(tmp)
        test_console_quick_reply_real(tmp)
        print("[21] 命令口输出")
        test_cmd_output(tmp)
        print("[23] 工具语法漏进正文")
        test_tool_markup_leak(tmp)
        print("[24] 配置热更新")
        test_config_reload(tmp)
        print("[25] 动态路由")
        test_dynamic_route(tmp)
        print("[26] 语音")
        test_voice(tmp)
        print("[27] 改自己的代码")
        test_self_edit(tmp)
        print("[28] 情绪欲望 + 自检纠错")
        test_mood_and_selfcheck(tmp)
        print("[29] 情绪变化的速度")
        test_emotion_dynamics(tmp)
        print("[30] 情绪能自己回来")
        test_emotion_recovers(tmp)
        print("[31] 交代的事要落成任务")
        test_task_becomes_intent(tmp)
        print("[32] 场景情绪 / IQ / 手机晚开")
        test_scene_and_iq(tmp)
        print("[33] 技能索引 P0")
        test_skill_registry(tmp)
        print("[34] 方法迭代 / 分条发")
        test_skill_iteration(tmp)
        print("[35] 电脑上的手")
        test_computer_tool(tmp)
        print("[36] 语音不能绕过动手")
        test_voice_cannot_skip_action(tmp)
        print("[37] 念想立刻去做")
        test_intent_runs_now(tmp)
        print("[38] 收消息走协议端")
        test_receive_via_napcat(tmp)
        print("[39] 先应声再动手")
        test_ack_then_act(tmp)
        print("[40] 收尾也由状态机定")
        test_closeout_by_state(tmp)
        print("[41] 用哪只手也靠语义")
        test_hand_by_semantics(tmp)
        print("[42] 他发来的图")
        test_image_in(tmp)
        print("[43] 冷却不吞消息")
        test_message_not_lost_in_cooldown(tmp)
        print("[44] 用手提示进提示词")
        test_hand_hint_goes_into_prompt(tmp)
        print("[45] tool 回应必须紧跟")
        test_tool_messages_repaired(tmp)
        print("[46] 多 tool_call 不乱序")
        test_multi_tool_call_order(tmp)
        print("[47] 空响应不写空 tool_calls")
        test_empty_model_response(tmp)
        print("[48] 换头像这条路")
        test_avatar_action(tmp)
        print("[49] 图片也走 NapCat")
        test_reply_image_path(tmp)
        print("[50] 回话只走 NapCat")
        test_reply_napcat_only(tmp)
        print("[51] 思考模式的 reasoning_content")
        test_reasoning_content_kept(tmp)
        print("[52] 部分发送不重复")
        test_partial_send_does_not_repeat(tmp)
        print("[53] 一次批准覆盖整件事")
        test_plan_consent(tmp)
        print("[54] 坏工具参数被拦")
        test_tool_args_validation(tmp)
        print("[55] 中途插话 / 叫停")
        test_owner_interrupt(tmp)
        print("[56] 深想 token 预算")
        test_deep_token_budget(tmp)
        test_tool_call_ids_filled(tmp)
        test_pc_hand_and_clixml(tmp)
        test_reply_split(tmp)
        print("[16] 第二版数据表")
        test_store_v2(tmp)
        print("[17] 身份与主动授权")
        test_identity_consent(tmp)
        print("[18] 幂等发送")
        test_outbox(tmp)
        print("[18b] 自然批准")
        test_natural_approval(tmp)
        print("[19] 自修改工坊")
        test_workshop(tmp)
    print(f"\n通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    from life.checks import run_checks
    raise SystemExit(run_checks(services_only=True))
