"""Small compatibility fixes for the prepared MCP copy, never the DSH plugin."""
from pathlib import Path


def prepare_backend(path):
    """Accept scalar system-event content without losing an entire DM batch.

    Bilibili includes JSON integers in msg_type=5 records. The upstream parser
    assumes every successfully decoded value is an object. The same assumption
    also occurs in the session-list preview. Empty content preserves the session
    entry while allowing the existing message parser to skip unsupported events.
    """
    path = Path(path)
    source = path.read_text('utf-8')
    if 'def bili_dm_recent(' not in source:
        return False
    original = source
    for record in ('m', 'lm'):
        line = f'            c = json.loads({record}.get("content") or "{{}}")'
        guarded = line + '\n            if not isinstance(c, dict): c = {}  # companion: scalar DM event'
        if guarded in source:
            continue
        if source.count(line) != 1:
            raise ValueError('B站MCP私信解析器结构已变更，需核对兼容补丁。')
        source = source.replace(line, guarded, 1)
    if source != original:
        path.write_text(source, encoding='utf-8')
        return True
    return False
