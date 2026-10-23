"""Application tool boundary, not an OS sandbox for arbitrary local code."""
import json
import re

_DATABASE = re.compile(r'(?i)(?:\b(?:sqlite3?|apsw|sqlalchemy|duckdb)\b|[\w.-]+\.(?:db|sqlite3?)(?:-wal|-shm)?\b|life-control[/\\])')


def check_tool_arguments(name, arguments):
    # Typed memory access is scoped and handled by the runtime, not raw SQL.
    if name in {'memory', 'solve_control', 'find_capabilities'}:
        return
    text = json.dumps(arguments, ensure_ascii=False)
    if _DATABASE.search(text):
        raise ValueError('记忆与运行数据库不能通过通用工具访问；请用 memory 查询、叮嘱或强调，不可直接改库。')


def outcome_ok(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('status') != 'result_recorded' or not snapshot.get('settled', True):
        return False
    result = snapshot.get('result') or {}
    if result.get('isError') or result.get('error'):
        return False
    value = result.get('value', result.get('meta', {}))
    # plugin_manager commonly returns one JSON text block.
    for block in result.get('content') or []:
        if block.get('type') == 'text':
            try:
                candidate = json.loads(block.get('text', ''))
                if isinstance(candidate, dict) and ('application' in candidate or 'exitCode' in candidate):
                    value = candidate
            except (ValueError, TypeError):
                pass
    if isinstance(value, dict):
        if value.get('application') not in (None, 'applied', 'restart-required'):
            return False
        if value.get('exitCode', 0) not in (0, None):
            return False
    return True
