#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过模拟器里的 QQ 客户端收发消息（GUI 路线）。

和 NapCat 那条协议链路完全独立：这条是"像真人一样点手机"。
好处是哪天协议端挂了它还能用；坏处是慢（一次 dump 0.5~1 秒）。

自己试：
    python qqgui.py --list            # 看会话列表
    python qqgui.py --read 樱花        # 打开某个会话并读消息
    python qqgui.py --send 樱花 你好    # 发一条
"""

from __future__ import annotations

import base64
import queue
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import phone

# 最近一次成功发送，防止任何调用链在 4 秒内把同一句再发一遍
_LAST_SEND: tuple[str, float] = ("", 0.0)

# 会话列表每一行长这样：
# "樱花樱花想见你༻꧂, ,有5条未读,你在吗,20:41"
_SIZE_CACHE = None

ROW_RE = re.compile(
    r"^(?P<name>[^,]+?),\s*,\s*(?:有(?P<unread>\d+)条未读\s*,\s*)?"
    r"(?P<preview>.*?),\s*(?P<time>\d{1,2}:\d{2}|昨天.*|前天.*|星期.*|\d+月\d+日.*)?$"
)


def _nodes() -> list[tuple[int, int, int, int, str, str]]:
    """(x1,y1,x2,y2, 文字, class)。文字取 text 或 content-desc。"""
    xml = phone._dump_xml()
    if not xml:
        return []
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    w, h = _screen_cached()
    for n in root.iter("node"):
        label = (n.get("text") or "").strip() or (n.get("content-desc") or "").strip()
        bounds = n.get("bounds") or ""
        m = re.findall(r"-?\d+", bounds)
        if len(m) != 4 or not label:
            continue
        x1, y1, x2, y2 = map(int, m)
        # 真正跑出屏幕的节点要丢掉，否则 in_chat() 会以为自己在聊天里，然后点在屏幕外。
        # 注意：这一条守卫不是用来解释"发送失败"的——历史上的 1433 是
        # _dump_failure 里把 (x1,y1,x2,y2) 切片成 (y1,x2,y2) 打印出来的误读。
        if x1 >= w or y1 >= h or x2 <= 0 or y2 <= 0:
            continue
        out.append((x1, y1, x2, y2, label, n.get("class") or ""))
    return out


def _screen_cached() -> tuple[int, int]:
    """同一轮里重复取屏幕尺寸太贵（要起截图），缓存 2 秒。"""
    global _SIZE_CACHE
    now = time.time()
    if _SIZE_CACHE and now - _SIZE_CACHE[0] < 2.0:
        return _SIZE_CACHE[1]
    size = phone.screen_size()
    _SIZE_CACHE = (now, size)
    return size


def layout_cached(w: int) -> str:
    return "pad" if w > 1200 else "phone"


def layout() -> str:
    """pad = 有左右两栏（横屏/平板），phone = 只有一栏（竖屏）。"""
    return "pad" if phone.orientation() == "landscape" else "phone"


def ensure_ready() -> None:
    """她要操作 QQ 之前先把屏幕方向锁成竖屏，不然点哪儿都不准。"""
    if phone.orientation() != "portrait":
        phone.lock_orientation("portrait")
        time.sleep(1.0)


def list_chats() -> list[dict]:
    """读会话列表。靠"名字, ,未读,最后一句,时间"这个格式认，不按坐标猜。"""
    chats: list[dict] = []
    for x1, y1, x2, y2, label, cls in _nodes():
        m = ROW_RE.match(label)
        if not m:
            continue
        name = m.group("name").strip()
        if not name or name in ("大肥鱼", "搜索框", "快捷入口"):
            continue
        chats.append({
            "name": name,
            "unread": int(m.group("unread") or 0),
            "preview": (m.group("preview") or "").strip(),
            "time": (m.group("time") or "").strip(),
            "bounds": (x1, y1, x2, y2),
        })
    return chats


def open_chat(name: str, tries: int = 3) -> bool:
    """点开会话。点完必须验证真的进去了，没进去就退回来换个位置再点。

    QQ 里点偏一点就会弹资料卡，所以这里不能"点完就走"。
    """
    for _ in range(tries):
        cands = [c for c in list_chats() if name in c["name"]]
        if not cands:
            to_chat_list()
            continue
        x1, y1, x2, y2 = cands[0]["bounds"]
        cy = (y1 + y2) // 2
        for frac in (0.70, 0.85, 0.55):
            phone.tap(x1 + int((x2 - x1) * frac), cy)
            time.sleep(2.0)
            if in_target_chat(name):
                return True
            phone.back()          # 可能弹了资料卡，退回来
            time.sleep(1.2)
    return False


def to_chat_list() -> bool:
    """退回会话列表：先试着点底部「消息」，不行再按返回。"""
    for _ in range(4):
        if list_chats():
            return True
        phone.tap_text("消息")
        time.sleep(1.2)
        if list_chats():
            return True
        phone.back()
        time.sleep(1.0)
    return bool(list_chats())


TAB_BAR = {"消息", "频道", "联系人", "动态"}


def reset_ui() -> bool:
    """把 QQ 重启到干净状态。

    踩过的坑：QQ 有时候会留一个浮层聊天窗压在列表上面，
    界面树里两套窗口叠在一起（列表的 tab 还在 y1519，聊天内容被挤到顶部），
    这时候点列表行会点到资料卡，发送按钮也点不动。
    强制重启一次最省事。
    """
    try:
        phone._run(["shell", "am", "force-stop", "com.tencent.mobileqq"], timeout=20)
        time.sleep(2.0)
        phone.open_app("com.tencent.mobileqq")
        time.sleep(6.0)
        return bool(list_chats())
    except Exception:
        return False


def _parse_tree():
    """一次 dump，返回 (info, parent, root)。info[node] = (bounds, label, cls)。"""
    xml = phone._dump_xml()
    if not xml:
        return {}, {}, None
    try:
        root = ET.fromstring(xml)
    except Exception:
        return {}, {}, None
    parent = {}
    for p in root.iter():
        for c in p:
            parent[c] = p
    info = {}
    for n in root.iter("node"):
        m = re.findall(r"-?\d+", n.get("bounds") or "")
        if len(m) != 4:
            continue
        label = (n.get("text") or "").strip() or (n.get("content-desc") or "").strip()
        info[n] = (tuple(map(int, m)), label, n.get("class") or "")
    return info, parent, root


def _row_labels(info, parent) -> set[str]:
    """会话列表行（连带它们的预览文字）——这些不是聊天内容。"""
    out: set[str] = set()
    for n, (_b, label, _c) in info.items():
        if not ROW_RE.match(label or ""):
            continue
        stack = [n]
        while stack:
            cur = stack.pop()
            if cur in info and info[cur][1]:
                out.add(info[cur][1])
            stack.extend(list(cur))
    return out


def _titles_from(info) -> tuple[str, ...]:
    """顶栏标题：拿「返回」按钮当锚点，取它右边、同一行的那行字。

    不能用"最上面那行字"：聊天页顶上可能先冒出某条消息，列表页也会残留
    上一个会话的标题节点。
    """
    w, h = _screen_cached()
    back = None
    for _n, (b, label, _c) in info.items():
        if label.startswith("返回") and b[1] < h * 0.10:
            back = b
            break
    if not back:
        return ()
    cy = (back[1] + back[3]) // 2
    cands: list[tuple[int, str]] = []
    for _n, (b, label, cls) in info.items():
        if not label or not cls.endswith("TextView"):
            continue
        if b[0] < back[2] - 4 or b[2] > w * 0.85:
            continue
        if abs((b[1] + b[3]) // 2 - cy) > 30:
            continue
        if label.startswith("在线"):
            continue
        if label in ("大肥鱼", "搜索框", "账户及设置", "快捷入口"):
            continue
        if "在线 - WiFi" in label:
            continue
        cands.append((b[0], label))
    cands.sort()
    return tuple(x[1] for x in cands[:2])


def input_bar_visible() -> bool:
    """截图上看，底部输入栏那一带是不是有东西。

    为什么需要它：QQ 把会话列表和聊天页放在同一个 window 里，dump 出来
    两套节点都在。光看节点分不清"现在画的是哪一页"，但截图分得清：
    列表页在那一条是空白背景（std≈1），聊天页是输入框（std≈5~30）。
    """
    raw = phone._screencap_png()
    if not raw:
        return False
    try:
        import io
        from PIL import Image, ImageStat
        im = Image.open(io.BytesIO(raw)).convert("L")
        w, h = im.size
        if w > h:
            # 横屏/双栏：列表和会话同时在屏上，输入栏一定在
            return True
        r = im.crop((int(w * 0.03), int(h * 0.91),
                     int(w * 0.80), int(h * 0.95)))
        return ImageStat.Stat(r).stddev[0] > 1.5
    except Exception:
        return False


# 聊天页里的"时间分隔条"不是他说的话：昨天 23:28 / 10月20日 12:30 / 星期一 08:00 ...
# 以前只挡了 "12:30" 这种纯时刻，"昨天 23:28" 会被当成他发的一条消息，
# 于是她对着一个时间戳又回一遍——这就是"连着回两条"的来源。
_STAMP_RE = re.compile(
    "^("
    "[0-9]{1,2}:[0-9]{2}"
    "|(昨天|今天|前天|凌晨|上午|中午|下午|晚上) ?[0-9]{1,2}:[0-9]{2}"
    "|(星期|周)[一二三四五六日天] ?[0-9]{1,2}:[0-9]{2}"
    "|[0-9]{1,2}月[0-9]{1,2}日( ?[0-9]{1,2}:[0-9]{2})?"
    "|[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日( ?[0-9]{1,2}:[0-9]{2})?"
    "|[0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2}( ?[0-9]{1,2}:[0-9]{2})?"
    "|[0-9]{1,2}-[0-9]{1,2} ?[0-9]{1,2}:[0-9]{2}"
    "|昨天|今天|前天"
    "|以下为新消息|以上是历史消息|对方正在输入…?"
    ")$"
)


def is_stamp(text: str) -> bool:
    """这行字是不是界面的装饰（时间/系统提示），不是他发的消息。"""
    t = (text or "").strip()
    return bool(t) and bool(_STAMP_RE.match(t))


def probe() -> dict:
    """一次问清：在不在聊天里、是谁、有什么消息、列表里有哪些人。"""
    info, parent, _root = _parse_tree()
    w, h = _screen_cached()
    labels = {v[1] for v in info.values()}
    has_input = any(v[2].endswith("EditText") and v[0][1] > h * 0.7
                    for v in info.values())
    has_talk = any("按住说话" in v[1] and v[0][1] > h * 0.7
                   for v in info.values())
    has_back = any(l.startswith("返回") for l in labels)
    structural = has_input or has_talk or ("发送" in labels and has_back)
    in_chat = bool(structural and input_bar_visible())

    rows: dict[str, str] = {}
    for _n, (_b, label, _c) in info.items():
        m = ROW_RE.match(label or "")
        if m and m.group("name").strip():
            rows[m.group("name").strip()] = (m.group("preview") or "").strip()

    titles = _titles_from(info) if in_chat else ()
    msgs: list[dict] = []
    if in_chat:
        excluded = _row_labels(info, parent)
        input_top = h * 0.92
        for v in info.values():
            if v[2].endswith("EditText") and v[0][1] > h * 0.7:
                input_top = min(input_top, v[0][1])
        for _n, (b, label, cls) in info.items():
            if not cls.endswith("TextView") or not label:
                continue
            if label in excluded or label in ("发送", "按住说话"):
                continue
            if not (h * 0.05 < b[1] < input_top - 4):
                continue
            if is_stamp(label):
                continue
            if "想及时收到" in label or "打开消息推送" in label:
                continue
            center = (b[0] + b[2]) // 2
            msgs.append({"side": "me" if center > w * 0.55 else "them",
                         "text": label, "y": b[1]})
        msgs.sort(key=lambda m: m["y"])
    return {"in_chat": in_chat, "titles": titles, "msgs": msgs, "rows": rows}


def in_chat() -> bool:
    return probe()["in_chat"]


def current_title() -> str:
    t = probe()["titles"]
    return t[0] if t else ""


def chat_matches(name: str, state: dict | None = None) -> bool:
    """现在这个会话，是不是「name」这个人。

    两层判断，任意一层过就算：
    1. 顶栏标题包含 name（最快）
    2. 列表里 ta 的最后一句预览，出现在当前聊天气泡里
    —— 光看"在某个会话里"不够：一旦误点进「我的电脑」，旧逻辑会一直对着
       文件传输窗口说话，主人那边的消息永远读不到。
    """
    st = state or probe()
    for t in st["titles"]:
        if name in t or t in name:
            return True
    preview = ""
    for row_name, p in st["rows"].items():
        if name in row_name:
            preview = p.lstrip("草稿").strip()
            break
    if preview:
        for m in st["msgs"][-8:]:
            t = m["text"].strip()
            if t and (preview[:10] in t or t[:10] in preview):
                return True
    return False


def in_target_chat(name: str) -> bool:
    st = probe()
    return st["in_chat"] and chat_matches(name, st)


def read_chat(limit: int = 12, nodes=None, expect: str | None = None) -> list[dict]:
    """读当前会话的消息。左边是他（them），右边是她（me）。

    expect 给了就只读"确实是这个人"的会话；不是就回空，让上层重新进会话。
    """
    st = probe()
    if not st["in_chat"]:
        return []
    if expect and not chat_matches(expect, st):
        return []
    return st["msgs"][-limit:]


def observe(max_nodes: int = 70) -> dict:
    """给她看：截图 + 界面上的字 + 会话列表 + **只属于聊天页的消息**。

    "谁说的"必须在源头分清：
    - 列表预览、系统卡片、频道/新闻、她自己的话，都不能标成"他"；
    - 这一层是事实，不能让她靠猜——她可以学着处理边角，但没法从错标签里学对。
    """
    info, parent, _root = _parse_tree()
    kids: dict[str, list[str]] = {}
    for nid, p in parent.items():
        kids.setdefault(p, []).append(nid)
    img = phone.screenshot_data_url(720)
    try:
        app = phone.current_app()
    except Exception:
        app = ""
    w, h = phone.screen_size()
    landscape = w > h
    pane_left = int(w * 0.28) if landscape else 0

    rows = []
    row_ids: set[str] = set()
    for nid, (b, label, _c) in info.items():
        m = ROW_RE.match(label or "")
        if m and m.group("name").strip():
            rows.append({"name": m.group("name").strip(),
                         "preview": (m.group("preview") or "").strip(),
                         "unread": int(m.group("unread") or 0),
                         "bounds": b})
            stack = [nid]
            while stack:
                cur = stack.pop()
                row_ids.add(cur)
                stack.extend(kids.get(cur, []))

    nodes = []
    for b, label, cls in sorted(info.values(), key=lambda v: (v[0][1], v[0][0])):
        if not label:
            continue
        short = cls.split(".")[-1]
        if short in ("TextView", "Button", "EditText"):
            nodes.append(f"{label[:40]}  ({b[0]},{b[1]})  {short}")

    # 这里有没有"聊天页"？没有就一条消息都不给，别把列表/频道当成他说话
    labels = [label for _b, label, _c in info.values() if label]
    has_input = any(cls.endswith("EditText") and b[1] > h * 0.6
                    for b, _l, cls in info.values())
    has_send = any(l in ("发送", "按住说话") and b[1] > h * 0.6
                   for b, l, _c in info.values())
    in_chat_page = has_input or has_send
    if not in_chat_page and not landscape:
        return {"image": img, "app": app, "rows": rows,
                "nodes": nodes[:max_nodes], "msgs": [], "msg_confidence": 0.0}

    chrome = {"消息", "频道", "联系人", "动态", "发送", "按住说话", "搜索框",
              "账户及设置", "大肥鱼", "在线", "在线 - WiFi", "推荐", "频道问问",
              "我的资料卡", "聊天设置", "返回消息", "返回", "取消", "完成",
              "草稿", "语音", "相册", "拍照", "红包", "表情", "文件", "更多功能"}
    sys_re = re.compile(r"(添加好友|访问了你|未读|草稿|登录|权限|允许|拒绝|"
                        r"已添加|开始聊天|拍了拍|撤回|通知)")
    msgs = []
    for nid, (b, label, cls) in info.items():
        if nid in row_ids or not cls.endswith("TextView") or not label:
            continue
        if b[1] <= 80 or label in chrome or is_stamp(label):
            continue
        if "未读" in label or "在线" in label:
            continue
        if landscape and b[2] <= pane_left:      # 横屏左栏是列表，不算消息
            continue
        right = pane_left + (w - pane_left) * 0.55
        center = (b[0] + b[2]) // 2
        msgs.append({"text": label, "x": b[0], "y": b[1],
                     "side": "me" if center >= right else "them",
                     "system": bool(sys_re.search(label))})
    msgs.sort(key=lambda m: m["y"])
    return {"image": img, "app": app, "rows": rows,
            "nodes": nodes[:max_nodes], "msgs": msgs[-30:],
            "msg_confidence": 0.65 if msgs else 0.0}


def last_from(name: str) -> dict | None:
    """会话列表里某个人的最后一条（不用点进去，快）。"""
    for c in list_chats():
        if name in c["name"]:
            return c
    return None


def ensure_chat(name: str) -> bool:
    """确保停在**这个人**的会话里（不是"某个会话里"）。

    旧版只判断 in_chat()，所以一旦误点进「我的电脑」，它就一直对着
    文件传输窗口读消息、发消息，主人那边永远等不到回复。
    """
    st = probe()
    if st["in_chat"] and chat_matches(name, st):
        return True
    if st["in_chat"]:              # 在错的会话里，先退回列表
        to_chat_list()
    dismiss_card()
    return open_chat(name)


def _send_button(ns):
    """找「发送」按钮的节点。"""
    for n in ns:
        if n[4] == "发送":
            return n
    return None


def _input_point(ns):
    """算输入框该点哪儿。

    QQ 的输入框**不是** EditText，无障碍树里根本没有它，
    所以用「发送」按钮那一行反推：按钮左边那一大片就是输入区。
    """
    w, h = _screen_cached()
    btn = _send_button(ns)
    if btn is not None:
        return ((btn[0]) // 2, (btn[1] + btn[3]) // 2)
    for x1, y1, x2, y2, label, cls in ns:
        if cls.endswith("EditText"):
            return ((x1 + x2) // 2, (y1 + y2) // 2)
    return (int(w * 0.45), int(h * 0.91))


def _sent(msgs, text: str) -> bool:
    """发出去的消息有没有出现在对话里。这是唯一的成功判据。"""
    key = text[:6]
    return any(m["side"] == "me" and key and key in m["text"] for m in msgs)


def _dump_failure(text: str, reason: str) -> str:
    """发送失败时把现场留证：截图 + 界面树 + 关键事实。

    这是给她自己修 bug 用的原料——只给一句"没发出去"，她只能瞎猜。
    """
    try:
        root = Path(__file__).resolve().parent
        d = root / "data" / "failures"
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        ns = _nodes()
        btn = _send_button(ns)
        lines = [
            f"# 发送失败现场 {stamp}",
            f"- 想发：{text[:300]}",
            f"- 原因：{reason}",
            f"- 前台应用：{phone.current_app()}",
            f"- 当前会话标题：{current_title()}",
            f"- 方向/尺寸：{phone.orientation()} {phone.screen_size()}",
            f"- 找到「发送」按钮：{('有，bounds=' + str(btn[0:4])) if btn else '没有'}",
            f"- 输入框落点：{_input_point(ns)}",
            "",
            "## 界面树（前 45 条）",
        ]
        for n in ns[:45]:
            lines.append(f"{n[4]} [{n[5].split('.')[-1]}] ({n[0]},{n[1]},{n[2]},{n[3]})")
        f = d / f"{stamp}.md"
        f.write_text("\n".join(lines), "utf-8")
        try:
            shot = phone.screenshot_data_url(900)
            if shot:
                (d / f"{stamp}.jpg").write_bytes(
                    base64.b64decode(shot.split(",", 1)[1]))
        except Exception:
            pass
        return str(f)
    except Exception:
        return ""


def read_chat_timeout(limit: int = 6, seconds: float = 8.0):
    """读当前聊天，但最多等 seconds 秒；uiautomator 偶尔会整段卡死。"""
    q: queue.Queue = queue.Queue(maxsize=1)

    def worker():
        try:
            q.put(read_chat(limit))
        except Exception:
            q.put(None)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(max(1.0, float(seconds)))
    if th.is_alive():
        return None
    try:
        return q.get_nowait()
    except queue.Empty:
        return None


def send_now(text: str):
    """在当前会话里输入并发送一次。

    关键：**只输入一次，不在失败后自动重打。**
    以前失败会循环两轮，第二轮的 clear_text 一旦没生效，就会把同一句再打一遍，
    表现就是输入框里出现两遍、甚至草稿越积越长。

    流程：清空一次 → 输入一次 → 收键盘 → 点发送 → 验证；
    验证失败只尝试一次回车兜底，仍失败就保留现场返回失败，让 agent 先看再决定。
    """
    text = (text or "").replace(chr(13), "").replace(chr(10), "").strip()
    if not text:
        return False, "空消息"
    global _LAST_SEND
    if text == _LAST_SEND[0] and time.time() - _LAST_SEND[1] < 8.0:
        return True, "刚刚已经成功发过同一句，跳过重复发送"

    ime_ok, ime_note = phone.ensure_adb_ime()
    if not ime_ok:
        return False, f"输入法不可用：{ime_note}"

    # 整个发送只清一次、只输入一次
    phone.clear_text()
    time.sleep(0.35)

    ns = _nodes()
    btn = _send_button(ns)
    if btn is None:
        return False, "不在会话里（找不到发送按钮）"
    w0, h0 = _screen_cached()
    if not (0 <= btn[0] < btn[2] <= w0 and 0 <= btn[1] < btn[3] <= h0):
        return False, "不在会话里（发送按钮坐标越界）"

    bx, by = _input_point(ns)
    phone.tap(bx, by)
    time.sleep(0.6)
    ok, note = phone.type_text(text, verify=False)
    if not ok:
        return False, f"没打进去：{note}"

    # 键盘会盖住发送按钮；先收键盘再重新取发送按钮坐标
    phone.hide_ime()
    time.sleep(0.55)
    btn = _send_button(_nodes())
    if btn is None:
        return False, "输入后找不到发送按钮（可能键盘没收起来）"

    phone.tap((btn[0] + btn[2]) // 2, (btn[1] + btn[3]) // 2)
    # 点下去就记“已经尝试过”：后续验证/失败处理绝不再自动输入第二遍。
    # QQNT 的消息气泡不在无障碍树里，不能靠 read_chat 判断；由上层用截图验证。
    _LAST_SEND = (text, time.time())
    time.sleep(0.55)
    return True, "已点击发送（等待截图验证）"

def send(text: str, expect: str | None = None):
    """带目标校验的发送（旧的脚本通道和命令用，会自动收键盘）。"""
    text = (text or "").replace("\r", "").replace("\n", "").strip()
    if not text:
        return False, "空消息"
    if expect and not in_target_chat(expect):
        if not ensure_chat(expect):
            return False, f"没能停在「{expect}」的会话里"
    phone.hide_ime()
    return send_now(text)


# 只认"资料卡上才有"的文字。别用"资料卡"三个字——聊天页头像的 content-desc 里就有它，
# 会误判成浮层然后按返回，把正在聊的界面退掉。
CARD_HINTS = ("匿问我答", '来自"QQ号查找"', "共同好友", "添加好友")


_NOTIF_RE = re.compile(
    r"NotificationRecord\([^)]*?pkg=(?P<pkg>[\w\.]+)[^)]*?\)(.*?)(?=NotificationRecord\(|\Z)", re.S)


def _unwrap(raw: str) -> str:
    """dumpsys 会把值包成 "String (xxx)"，剥掉。"""
    v = (raw or "").strip()
    m = re.match(r"String \((.*)\)$", v)
    return (m.group(1) if m else v).strip()


def notifications(pkg: str = "com.tencent.mobileqq") -> list[dict]:
    """从通知栏里读新消息。0.14 秒，比 dump 快 25 倍。

    QQ 刚收到消息时会挂一条通知，标题是人名、正文是那句话。
    """
    try:
        out = phone._run(["shell", "dumpsys", "notification", "--noredact"], timeout=20)
        txt = out[1].decode("utf-8", "replace")
    except Exception:
        return []
    got = []
    for m in _NOTIF_RE.finditer(txt):
        if pkg and pkg not in m.group("pkg"):
            continue
        blk = m.group(2)
        title = re.search(r"android\.title=([^\n]*)", blk)
        big = re.search(r"android\.bigText=([^\n]*)", blk)
        small = re.search(r"android\.text=([^\n]*)", blk)
        when = re.search(r"mCreationTimeMs=(\d+)", blk) or re.search(r"when=(\d+)", blk)
        big_v = _unwrap(big.group(1)) if big else ""
        text_v = _unwrap(small.group(1)) if small else ""
        body = big_v if big_v and big_v != "null" else text_v
        if not body or body == "null":
            continue
        got.append({
            "title": _unwrap(title.group(1)) if title else "",
            "text": body,
            "when": when.group(1) if when else "",
        })
    return got


def home_qq() -> None:
    """把 QQ 压到后台。QQ 在前台时不会挂通知，也就收不到通知信号。"""
    phone.home()
    time.sleep(0.8)


def wake_qq() -> bool:
    """把 QQ 拉回前台。

    踩过的坑：别的应用（比如 B站）在前台时，直接 `open_app(QQ)` 会返回成功
    但前台不变。先回桌面再开才抢得回来。
    """
    if phone.current_app() == "com.tencent.mobileqq":
        return True
    phone.home()
    time.sleep(1.2)
    phone.open_app("com.tencent.mobileqq")
    for _ in range(4):
        time.sleep(1.2)
        if phone.current_app() == "com.tencent.mobileqq":
            return True
    return False


def snapshot() -> dict[str, tuple[int, str, str]]:
    """会话列表的快照，用来判断有没有新消息。只 dump 一次，很便宜。"""
    return {c["name"]: (c["unread"], c["preview"], c["time"]) for c in list_chats()}


def changed(before: dict, after: dict) -> list[str]:
    """哪些会话出现了新的未读。"""
    out = []
    for name, val in after.items():
        if val[0] > 0 and before.get(name) != val:
            out.append(name)
    return out


def dismiss_card() -> bool:
    """如果弹了资料卡/浮层，退回去。"""
    labels = [n[4] for n in _nodes()]
    if any(any(h in l for h in CARD_HINTS) for l in labels):
        phone.back()
        time.sleep(1.2)
        return True
    return False


def read_latest(name: str, limit: int = 8) -> list[dict]:
    """读某个会话最新的几条。

    注意：进会话时本来就在最底部，**不要**无脑滚动——那会让屏幕莫名往上拖一下，
    看起来像她自己在划来划去。只有真的没读到才滚一次兜底。
    """
    if not in_chat():
        if not open_chat(name):
            return []
    got = read_chat(limit)
    if got:
        return got
    phone.scroll("down")                  # 兜底：真读不到才滚一次
    time.sleep(0.8)
    return read_chat(limit)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = sys.argv[1:]
    if not args or args[0] == "--list":
        for c in list_chats():
            unread = f"({c['unread']}条未读)" if c["unread"] else ""
            print(f"{c['name']:<18}{unread:<10}{c['time']:<8}{c['preview'][:30]}")
        return
    if args[0] == "--watch":
        target = args[1] if len(args) > 1 else None
        prev = snapshot()
        print("盯着列表（2 秒一次，Ctrl+C 停）…", flush=True)
        while True:
            time.sleep(2.0)
            cur = snapshot()
            for n in changed(prev, cur):
                if target and target not in n:
                    continue
                print(f"[新消息] {n}：{cur[n][1][:40]}", flush=True)
            prev = cur
    if args[0] == "--read" and len(args) > 1:
        if not open_chat(args[1]):
            print("找不到这个会话")
            return
        for m in read_chat(20):
            print(("她 > " if m["side"] == "me" else "他 > ") + m["text"][:60])
        return
    if args[0] == "--send" and len(args) > 2:
        if not open_chat(args[1]):
            print("找不到这个会话")
            return
        print(send(" ".join(args[2:])))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
