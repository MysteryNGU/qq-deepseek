"""Explicit live read-only bridge smoke; no QQ calls or model request."""
from pathlib import Path
import json
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bot import load_config
from life.runtime import LifeRuntime
from life.services.store import Store
from life.services.dsh_bridge import DshBridge
from life.tool_boundary import outcome_ok


def main():
    cfg = load_config(ROOT / 'config.toml')
    with tempfile.TemporaryDirectory(prefix='qq-dsh-read-') as directory:
        base = Path(directory)
        runtime = LifeRuntime(base / 'life.db')
        ledger = Store(base / 'life.db')
        bridge = DshBridge(cfg, ROOT, ledger)
        try:
            fixture = base / 'read-probe.txt'
            fixture.write_text('qq-life-read-only-probe', encoding='utf-8')
            hello = bridge.hello()
            rows = bridge.tools()
            schema = next(row for row in rows if row['name'] == 'read')
            from jsonschema import Draft202012Validator
            arguments = {'file_path': str(fixture)}
            Draft202012Validator(schema['parameters']).validate(arguments)
            operation = 'smoke_' + uuid.uuid4().hex
            snap = bridge.execute('read', arguments, operation_id=operation, plan_id=operation, step_id='read_1')
            assert outcome_ok(snap), 'read operation did not succeed'
            assert 'qq-life-read-only-probe' in json.dumps(snap, ensure_ascii=False), 'missing actual file evidence'
            replay = bridge.execute('read', arguments, operation_id=operation, plan_id=operation, step_id='read_1')
            assert replay == snap, 'operation replay changed'
            print(json.dumps({'tools': len(rows), 'profileOwnedByBot': hello.get('profileOwnedByBot'),
                'read_succeeded': True, 'replay_same_result': True}, ensure_ascii=False))
        finally:
            bridge.close()
            ledger.close()
            runtime.close()


if __name__ == '__main__':
    main()
