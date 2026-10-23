"""Pure reply layout: chat bubbles, protected content and adaptive density."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import math
import re


current_bubble_kind = ContextVar('current_bubble_kind', default='chat')
reply_preference = ContextVar('reply_preference', default=None)
LIST = re.compile(r'^\s*(?:[-*+]\s+|\d+[.)、]\s*|[一二三四五六七八九十]+[、.)])')
FENCE = re.compile(r'^\s*(`{3,}|~{3,})')
DEFAULT_PASTE_THRESHOLD = 300


def expression_body(proposal):
    parts = proposal.get('parts')
    if parts is None:
        parts = [dict(kind='text',text=proposal.get('text',''),mode=proposal.get('mode','auto'))]
    result = []
    for raw in parts:
        part = dict(raw)
        part.pop('voice_revision',None)
        if part.get('kind')=='text':
            part.setdefault('mode','auto')
        result.append(part)
    return result


@dataclass(frozen=True)
class Bubble:
    text: str
    kind: str = 'chat'


@dataclass(frozen=True)
class Rhythm:
    excitement: float = 0
    fatigue: float = 0
    recent_sent: int = 0
    idle_seconds: float = 0
    active_exchange: bool = False


def protected(text):
    return bool(FENCE.match(text) or LIST.match(text) or text.lstrip().startswith(('>', '|')))


def clean_visible(text, clean_chat):
    """Remove leaked tool markup outside code blocks, preserving literal code."""
    lines = (text or '').replace('\r\n', '\n').replace('\r', '\n').splitlines(keepends=True)
    chunks, prose, code = [], [], []
    marker = ''
    for line in lines:
        fence = FENCE.match(line)
        if marker:
            code.append(line)
            if fence and fence.group(1)[0] == marker[0] and len(fence.group(1)) >= len(marker):
                chunks.append(''.join(code)); code = []; marker = ''
        elif fence:
            if prose:
                raw = ''.join(prose)
                chunks.append(clean_chat(raw) + ('\n' if raw.endswith('\n') else ''))
                prose = []
            marker = fence.group(1); code = [line]
        else:
            prose.append(line)
    if prose:
        chunks.append(clean_chat(''.join(prose)))
    if code:
        chunks.append(''.join(code))
    return ''.join(chunks).strip('\n')


def content_blocks(text, mode='auto', *, paste_threshold=DEFAULT_PASTE_THRESHOLD):
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    if not text.strip():
        return []
    if (mode == 'artifact' or len(text.strip()) >= paste_threshold
            or any(protected(line) for line in text.split('\n'))):
        return [Bubble(text, 'artifact')]
    if mode == 'single':
        return [Bubble(text)]
    return [Bubble(line.strip()) for line in text.split('\n') if line.strip()]


def group_chat(bubbles, rhythm=Rhythm(), *, separate=False):
    """Density changes group size, never a fixed maximum bubble count."""
    if separate:
        return list(bubbles)
    density = rhythm.recent_sent * (.45 if rhythm.active_exchange else 1)
    target = max(0, min(180, (density - 3) * 15 + rhythm.fatigue * 4
                        + (30 if rhythm.idle_seconds > 120 else 0) - rhythm.excitement * 3))
    out, pending = [], []
    def flush():
        if pending:
            out.append(Bubble('\n'.join(pending))); pending.clear()
    for bubble in bubbles:
        if bubble.kind != 'chat':
            flush(); out.append(bubble); continue
        pending.append(bubble.text)
        if len('\n'.join(pending)) >= target and len(bubble.text) > 2:
            flush()
    flush()
    return out


def transport_parts(bubble, limit=3000):
    """Keep pasted material whole; a platform rejection remains a real failure."""
    if bubble.kind == 'artifact' or len(bubble.text) <= limit:
        return [bubble]
    parts, rest = [], bubble.text
    while len(rest) > limit:
        cut = rest.rfind('\n\n', 0, limit + 1)
        if cut > 0:
            cut += 2
        else:
            cut = rest.rfind('\n', 0, limit + 1)
            cut = cut + 1 if cut > 0 else limit
        parts.append(Bubble(rest[:cut], bubble.kind))
        rest = rest[cut:]
    if rest:
        parts.append(Bubble(rest, bubble.kind))
    return parts


def bubble_delay(text, kind='chat', *, cps=3):
    """Compressed chat cadence (2–5 seconds); pasted material always waits 4."""
    if kind == 'artifact':
        return 4
    covered = sum(not char.isspace() for char in text)
    return min(5, max(2, math.ceil(covered / (cps * 4))))


def expression_parts(parts, paste_threshold=DEFAULT_PASTE_THRESHOLD):
    """Combine adjacent text in one pasted reply, preserving media boundaries."""
    result, pending = [], []
    def flush():
        if not pending:
            return
        text = '\n'.join(str(part.get('text', '')) for part in pending)
        if (len(text.strip()) >= paste_threshold
                or any(part.get('mode') == 'artifact' for part in pending)
                or any(protected(line) for line in text.splitlines())):
            result.append(dict(kind='text', text=text, mode='artifact'))
        else:
            result.extend(pending)
        pending.clear()
    for part in parts:
        if part.get('kind') == 'text':
            pending.append(part)
        else:
            flush()
            result.append(part)
    flush()
    return result
