# -*- coding: utf-8 -*-
"""`memory op=used` 在「显式点名了 kind」时把没做过的项说成用它做过。

复现（隔离 HOME，不写生产库）：
  * 建一张空库，直接调 life.companion_awareness._used(...)，
    arguments={"op":"used","kind":["command","web"]}；
  * 只看返回的 output 那一句。

线上完整验收里看到的同一现象：
  .acceptance/20261118-002824-cap/report.txt  C4 段
  used[kind] completed | returned 2 | available 2
    output: 用过 0 项：。没做过的是：command、web（只是没做过）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HOME = Path(tempfile.mkdtemp(prefix='used-repro-'))
os.environ['HOME'] = str(HOME)
os.environ['USERPROFILE'] = str(HOME)


def main():
    from life.companion import Companion
    from life import companion_awareness as awareness
    from tests.test_agent_repair import FakeModel, _cfg

    cfg = _cfg(HOME)
    core = Companion(cfg, HOME, model=FakeModel())
    try:
        arguments = dict(op='used', kind=['command', 'web'])
        reply = awareness._used(core, arguments,
                                awareness.build(core, state=dict(available_actions=['command'])))
        by_kind = {item['kind']: item for item in reply['items']}
        print('status       :', reply['status'])
        print('items        :', json.dumps(
            {kind: dict(counts=item['counts'],
                        standing=item['standing']['state']) for kind, item in by_kind.items()},
            ensure_ascii=False))
        print('output       :', reply['output'])
        empty_ledger = all(not item['counts']['total'] for item in reply['items'])
        misreports = ('用过 0 项：' in reply['output'] and empty_ledger)
        print('DEFECT       :', 'YES' if misreports else 'no',
              '- empty ledger but the sentence still opens with "used 0 item(s): "')
        # 同一份数据，带 kind 与不带的说法是否一致
        plain = awareness._used(core, dict(op='used'),
                               awareness.build(core, state=dict(available_actions=['command'])))
        print('without kind :', plain['output'])
    finally:
        core.life.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
