#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""她的手机：截图、读界面、点、滑、返回、开应用。

全部走 adb。没有连设备时每个函数都老实返回失败，不会崩。

自己试：
    python phone.py --check           # adb 在哪、有没有设备
    python phone.py --screen          # 截一张存 data/phone.png
    python phone.py --ui              # 把当前界面读成文字
    python phone.py --tap 540 1200    # 点一下
"""

from __future__ import annotations

import base64
import io
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADB_LOCAL = ROOT / "tools" / "platform-tools" / "adb.exe"


def _adb() -> str:
    env = os.environ.get("ADB_PATH", "").strip()
    if env and Path(env).exists():
        return env
    if ADB_LOCAL.exists():
        return str(ADB_LOCAL)
    return shutil.which("adb") or ""


_SERIAL_CACHE: str | None = None


def _adb_serial(exe: str) -> str:
    """一个模拟器经常同时以 127.0.0.1:7555 和 emulator-5554 出现。

    adb 看到两个设备就会拒绝执行 shell 命令，所以这里固定选第一个，
    后续所有命令都带 -s；也可以用环境变量 ANDROID_SERIAL 手动指定。
    """
    global _SERIAL_CACHE
    env = os.environ.get("ANDROID_SERIAL", "").strip()
    if env:
        return env
    if _SERIAL_CACHE:
        return _SERIAL_CACHE
    try:
        p = subprocess.run([exe, "devices"], capture_output=True, timeout=15)
        for line in p.stdout.decode("utf-8", "replace").splitlines()[1:]:
            if "	device" in line:
                _SERIAL_CACHE = line.split("	", 1)[0].strip()
                return _SERIAL_CACHE
    except Exception:
        pass
    return ""


def _adb_cmd(exe: str, args: list[str]) -> list[str]:
    # devices/connect/start-server 这些是 adb 自身的命令，不能带 -s
    if args and args[0] in ("devices", "connect", "disconnect", "start-server",
                            "kill-server", "version"):
        return [exe, *args]
    serial = _adb_serial(exe)
    return [exe, "-s", serial, *args] if serial else [exe, *args]


def _run(args: list[str], timeout: int = 30) -> tuple[int, bytes]:
    exe = _adb()
    if not exe:
        return 127, b""
    try:
        p = subprocess.run(_adb_cmd(exe, args), capture_output=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception:
        return 1, b""


def _run_err(args: list[str], timeout: int = 30) -> tuple[int, bytes, bytes]:
    """跟 _run 一样，但把 stderr 也带回来——"没打进去"到底是没设备还是没接收者，得分开。"""
    exe = _adb()
    if not exe:
        return 127, b"", b"adb not found"
    try:
        p = subprocess.run(_adb_cmd(exe, args), capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, b"", str(e).encode("utf-8", "replace")


def devices() -> list[str]:
    code, out = _run(["devices"])
    if code:
        return []
    found = []
    for line in out.decode("utf-8", "replace").splitlines()[1:]:
        if "\tdevice" in line:
            found.append(line.split("\t")[0].strip())
    return found


def connected() -> bool:
    return bool(devices())


def connect(hp: str = "127.0.0.1:7555") -> tuple[bool, str]:
    """连模拟器，比如 127.0.0.1:7555（MuMu）/ 62001（夜神）/ 5555（真机无线）。"""
    code, out = _run(["connect", hp], timeout=15)
    text = out.decode("utf-8", "replace").strip()
    return (code == 0 and "connected" in text), text


# ---------------------------------------------------------------- 看


def screenshot_data_url(max_side: int = 1080) -> str:
    raw = _screencap_png()
    if not raw:
        return ""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def _screencap_png() -> bytes:
    code, raw = _run(["exec-out", "screencap", "-p"], timeout=25)
    return raw if (code == 0 and len(raw) > 1000) else b""


def screen_size() -> tuple[int, int]:
    """用真实截图的尺寸当坐标系。wm size 在模拟器上会说反（横竖颠倒）。"""
    raw = _screencap_png()
    if raw:
        try:
            from PIL import Image
            return Image.open(io.BytesIO(raw)).size
        except Exception:
            pass
    code, out = _run(["shell", "wm", "size"], timeout=15)
    m = re.search(r"(\d+)x(\d+)", out.decode("utf-8", "replace"))
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)


def _dump_xml() -> str:
    """读界面树。uiautomator 有时会"把文件写了再崩"（退出码 139/137），
    有时会崩在写文件之前——所以要先删旧文件，再确认读到的是这次的新文件。"""
    for _ in range(2):
        _run(["shell", "rm", "-f", "/sdcard/ui.xml"], timeout=10)
        _run_err(["shell", "uiautomator", "dump", "--compressed", "/sdcard/ui.xml"],
                 timeout=35)
        code, raw = _run(["exec-out", "cat", "/sdcard/ui.xml"], timeout=20)
        if code == 0 and len(raw) > 1000:
            text = raw.decode("utf-8", "replace")
            if "<hierarchy" in text:
                return text
        time.sleep(0.4)
    return ""


def ui_tree(limit: int = 40) -> str:
    """把当前界面读成人能看的样子：文字 + 能不能点 + 坐标。"""
    xml = _dump_xml()
    if not xml:
        return ""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except Exception:
        return ""
    rows: list[str] = []
    for n in root.iter("node"):
        text = (n.get("text") or "").strip()
        desc = (n.get("content-desc") or "").strip()
        label = text or desc
        clickable = n.get("clickable") == "true"
        if not label and not clickable:
            continue
        m = re.findall(r"-?\d+", n.get("bounds") or "")
        if len(m) != 4:
            continue
        x1, y1, x2, y2 = map(int, m)
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        cls = (n.get("class") or "").split(".")[-1]
        line = f"{label[:30] or '（无字）'}  [{cls}]  ({cx},{cy})"
        if clickable:
            line += "  可点"
        rows.append(line)
        if len(rows) >= limit:
            break
    return "\n".join(rows)


def find_text(needle: str) -> tuple[int, int] | None:
    """按文字找位置，比按坐标点稳。"""
    xml = _dump_xml()
    if not xml:
        return None
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except Exception:
        return None
    for n in root.iter("node"):
        label = (n.get("text") or "") + "|" + (n.get("content-desc") or "")
        if needle and needle in label:
            m = re.findall(r"-?\d+", n.get("bounds") or "")
            if len(m) == 4:
                x1, y1, x2, y2 = map(int, m)
                return ((x1 + x2) // 2, (y1 + y2) // 2)
    return None


def ui_has(text: str) -> bool:
    """界面上现在有没有这段文字（不做坐标解析，直接看 dump 原文）。"""
    needle = (text or "").strip()
    if not needle:
        return False
    xml = _dump_xml()
    if not xml:
        return False
    import html as _html
    hay = _html.unescape(xml)
    # 长句可能被输入框截断/换行，用前 10 个字判断
    return needle[:10] in hay


def verify_text(text: str, timeout: float = 3.0) -> bool:
    """打完字/点完之后，等界面真的出现这段文字。这是"手会不会撒谎"的开关。"""
    end = time.time() + max(0.5, timeout)
    while True:
        if ui_has(text):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.5)


# ---------------------------------------------------------------- 点


def tap(x: int, y: int) -> bool:
    """点坐标。抖一点点，别每次都是同一个像素。"""
    x += random.randint(-4, 4)
    y += random.randint(-4, 4)
    code, _ = _run(["shell", "input", "tap", str(max(0, x)), str(max(0, y))], timeout=15)
    return code == 0


def tap_text(text: str) -> tuple[bool, str]:
    pos = find_text(text)
    if not pos:
        return False, f"界面上没找到「{text}」"
    ok = tap(*pos)
    return ok, f"点了「{text}」({pos[0]},{pos[1]})"


def swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> bool:
    code, _ = _run(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms)],
                   timeout=15)
    return code == 0


def scroll(direction: str = "down") -> bool:
    w, h = screen_size()
    cx = w // 2
    if direction == "down":
        return swipe(cx, int(h * 0.72), cx, int(h * 0.32), 320)
    return swipe(cx, int(h * 0.32), cx, int(h * 0.72), 320)


def back() -> bool:
    code, _ = _run(["shell", "input", "keyevent", "4"], timeout=10)
    return code == 0


def home() -> bool:
    code, _ = _run(["shell", "input", "keyevent", "3"], timeout=10)
    return code == 0


def ui_has_input() -> bool:
    """界面上有没有能被 dump 出来的输入框。

    QQ 的聊天输入框不是 EditText，dump 里根本没有它；这种情况下
    "读回验证"必然失败，不能判成"没打进去"。
    """
    xml = _dump_xml()
    if not xml:
        return False
    return "EditText" in xml


def ime_shown() -> bool:
    """输入法是不是开着。开着的时候它会盖住"发送"，这时点发送其实点在键盘上。"""
    code, out, _err = _run_err(["shell", "dumpsys", "input_method"], timeout=15)
    return b"mInputShown=true" in out


def hide_ime() -> None:
    """收起输入法：键盘开着时按返回只会收键盘，不会退出会话。"""
    if not ime_shown():
        return
    _run(["shell", "input", "keyevent", "4"], timeout=10)
    for _ in range(6):
        time.sleep(0.25)
        if not ime_shown():
            return


ADB_IME = "com.android.adbkeyboard/.AdbIME"


def current_ime() -> str:
    code, out, _err = _run_err(["shell", "settings", "get", "secure",
                                "default_input_method"], timeout=15)
    return out.decode("utf-8", "replace").strip()


def ensure_adb_ime() -> tuple[bool, str]:
    """ADBKeyboard 必须真的是当前输入法，广播才有人收。
    模拟器重启/用户切回搜狗之后，这里会自动切回来；没装的话先尝试装项目自带的 APK。"""
    cur = current_ime()
    if "adbkeyboard" in cur:
        return True, "ADBKeyboard 已经是当前输入法"
    code, out = _run(["shell", "pm", "path", "com.android.adbkeyboard"], timeout=15)
    if b"package:" not in (out or b""):
        apk = ROOT / "tools" / "ADBKeyboard.apk"
        if apk.exists():
            _run(["install", "-r", str(apk)], timeout=90)
    _run(["shell", "ime", "enable", ADB_IME], timeout=15)
    _run(["shell", "ime", "set", ADB_IME], timeout=15)
    time.sleep(0.8)
    cur2 = current_ime()
    if "adbkeyboard" in cur2:
        return True, f"输入法从 {cur or '?'} 切到了 ADBKeyboard"
    return False, f"输入法切不过去（现在是 {cur2 or '?'}）"


def type_text(text: str, verify: bool = True) -> tuple[bool, str]:
    """打文字。ASCII 走 input text；中文走 ADBKeyboard 的广播（base64，避免编码问题）。

    打完必须读回确认：am broadcast 在 0 个接收者时也返回 0，
    只看退出码会"假成功"——上层拿去点发送，用户只看到消息没发出去。
    但有的界面（QQ 聊天框）根本不把输入框暴露在树里，这时没法读回，
    只能把"发送后对话里出现"当成最终证据。
    """
    if re.search(r"[^\x00-\x7F]", text):
        ime_ok, ime_note = ensure_adb_ime()
        if not ime_ok:
            return False, f"中文打不进去：{ime_note}"
        b64 = base64.b64encode(text.encode("utf-8")).decode()
        code, _out, err = _run_err(["shell", "am", "broadcast", "-a", "ADB_INPUT_B64",
                                    "--es", "msg", b64], timeout=15)
        if code != 0:
            detail = err.decode("utf-8", "replace").strip()[:80]
            return False, f"中文打不进去（{ime_note}；{detail}）"
        sent = "用 ADBKeyboard 打进去了"
    else:
        safe = text.replace(" ", "%s")
        code, _out, err = _run_err(["shell", "input", "text", safe], timeout=15)
        if code != 0:
            detail = err.decode("utf-8", "replace").strip()[:80]
            return False, f"打字失败（{detail or 'input text 返回非 0'}）"
        sent = "打进去了"

    if verify:
        if verify_text(text[:20], 2.5):
            return True, sent + "（读回确认）"
        if not ui_has_input():
            # 树里没有输入框可读（QQ 聊天框就是），只能靠"发完看对话"确认
            return True, sent + "（这个界面读不回输入框，发完看对话确认）"
        return False, (sent + "，但输入框里没有字——ADBKeyboard 可能没装/没切过来，"
                       "或者当前根本没有输入框")
    return True, sent


def clear_text() -> bool:
    """清空当前输入框。ADBKeyboard 提供 ADB_CLEAR_TEXT 广播。"""
    code, _ = _run(["shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"], timeout=15)
    return code == 0


def type_and_close(text: str) -> tuple[bool, str]:
    """打字，然后自动把全屏输入框收起来（不然盖住"发表"这类按钮）。"""
    ok, note = type_text(text)
    if not ok:
        return False, note
    time.sleep(1.2)
    if find_text("完成"):          # 全屏输入法展开时会有一个"完成"
        tap_text("完成")
        time.sleep(1.2)
        return True, "打完字，并收起了全屏输入框"
    return True, "打进去了"


APP_ALIASES = {
    "b站": "tv.danmaku.bili", "B站": "tv.danmaku.bili", "bilibili": "tv.danmaku.bili",
    "哔哩哔哩": "tv.danmaku.bili", "bili": "tv.danmaku.bili",
    "微信": "com.tencent.mm", "wechat": "com.tencent.mm",
    "qq": "com.tencent.mobileqq", "QQ": "com.tencent.mobileqq",
    "设置": "com.android.settings", "settings": "com.android.settings",
    "相机": "com.android.camera", "camera": "com.android.camera",
    "浏览器": "com.android.browser", "browser": "com.android.browser",
    "应用商店": "com.mumu.store", "游戏中心": "com.mumu.store",
}


def resolve_pkg(app: str) -> str:
    """中文名也能开，省得模型去记包名。"""
    key = app.strip()
    return APP_ALIASES.get(key) or APP_ALIASES.get(key.lower()) or key


def open_app(pkg: str) -> tuple[bool, str]:
    """按包名或中文名打开应用，比如 B站 / 微信 / com.tencent.mm。"""
    pkg = resolve_pkg(pkg)
    code, out = _run(["shell", "monkey", "-p", pkg, "-c",
                      "android.intent.category.LAUNCHER", "1"], timeout=20)
    text = out.decode("utf-8", "replace")
    if "No activities found" in text:
        return False, f"没装这个应用：{pkg}"
    return code == 0, text.strip().splitlines()[-1] if text.strip() else ""


def current_app() -> str:
    code, out = _run(["shell", "dumpsys", "window"], timeout=20)
    m = re.search(r"mCurrentFocus=.*?([\w.]+)/", out.decode("utf-8", "replace"))
    return m.group(1) if m else ""


def shot_with_size(max_width: int = 1200):
    """截一张图，**同时**拿到这次截图的真实尺寸。

    为什么要一起给：模拟器会横竖屏来回翻，先截一张 1600x900 再单独问尺寸
    可能拿到 900x1600，两者一错，换算出来的点击位置就全偏了。
    """
    try:
        raw = _screencap_png()
        if not raw:
            return "", (0, 0)
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        size = im.size
        if im.width > max_width:
            ratio = max_width / im.width
            im = im.resize((max_width, int(im.height * ratio)))
        buf2 = io.BytesIO()
        im.convert("RGB").save(buf2, "JPEG", quality=85)
        data = "data:image/jpeg;base64," + base64.b64encode(buf2.getvalue()).decode()
        return data, size
    except Exception:
        return "", (0, 0)


def lock_orientation(mode: str = "portrait") -> bool:
    """锁屏幕方向。模拟器一会儿横一会儿竖，坐标全乱，先锁住再操作。

    mode: portrait / landscape / auto
    """
    try:
        if mode == "auto":
            _run(["shell", "settings", "put", "system", "accelerometer_rotation", "1"])
            return True
        _run(["shell", "settings", "put", "system", "accelerometer_rotation", "0"])
        rot = "0" if mode == "portrait" else "1"
        _run(["shell", "settings", "put", "system", "user_rotation", rot])
        time.sleep(1.5)
        return True
    except Exception:
        return False


def orientation() -> str:
    """现在是竖屏还是横屏（按真实截图判断，最可靠）。"""
    w, h = screen_size()
    return "landscape" if w > h else "portrait"


# ---------------------------------------------------------------- 组合动作


# ---------------------------------------------------------------- 命令行


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--check" in sys.argv:
        print("adb:", _adb() or "（没找到）")
        d = devices()
        print("设备:", d or "（没有连着）")
        if d:
            print("分辨率:", screen_size())
            print("当前应用:", current_app())
        return
    if "--connect" in sys.argv:
        i = sys.argv.index("--connect")
        target = sys.argv[i + 1] if len(sys.argv) > i + 1 else "127.0.0.1:7555"
        print(connect(target))
        return
    if "--screen" in sys.argv:
        data = screenshot_data_url()
        if not data:
            print("截不到（没设备？）")
            return
        out = ROOT / "data" / "phone.jpg"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(base64.b64decode(data.split(",", 1)[1]))
        print("存到:", out)
        return
    if "--ui" in sys.argv:
        print(ui_tree() or "读不到界面（没设备或界面是游戏渲染的）")
        return
    if "--tap" in sys.argv:
        i = sys.argv.index("--tap")
        x, y = int(sys.argv[i + 1]), int(sys.argv[i + 2])
        print("点了:", tap(x, y))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
