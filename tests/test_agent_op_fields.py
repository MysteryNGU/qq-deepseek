# -*- coding: utf-8 -*-
"""逐个 op 核对 OP_FIELDS 与 AgentManager 方法签名是否吻合。

生产出过两次同类错：`inspect() got an unexpected keyword argument 'goal'`，
修完又冒出 `'project'`。根因是 _action_fields 的「通用字段放行」把 DECISION 层
的必填字段（project）漏进了方法签名。这条测试直接对签名取参数集合，不靠人工核对。

跑法：python -B -X utf8 -m unittest tests.test_agent_op_fields -v
"""
from __future__ import annotations

import inspect
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from life.companion_agents import OPS, OP_FIELDS, AgentManager, _action_fields  # noqa: E402

# dispatch() 里每个 op 调用哪个方法（见 AgentManager.dispatch）
METHOD = {
    'create': 'create', 'send': 'send', 'inspect': 'inspect', 'wait': 'wait',
    'cancel': 'cancel', 'resume': 'resume', 'read_artifact': 'read_artifact',
}


def _accepts(name):
    """这个方法签名接受的关键字参数集合（含 **kwargs 就返回 None 表示全收）。"""
    fn = getattr(AgentManager, name)
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return None
    return {n for n, p in params.items()
            if p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)} - {'self'}


class OpFieldsMatchSignature(unittest.TestCase):
    def test_every_op_field_is_accepted_by_its_method(self):
        """OP_FIELDS 里写的每个键，对应方法必须真能接。"""
        for op in OPS:
            allowed = _accepts(METHOD[op])
            if allowed is None:
                continue
            extra = OP_FIELDS[op] - allowed
            self.assertFalse(extra, '%s 的 OP_FIELDS 里有方法不接受的键：%s（会 TypeError）'
                             % (op, sorted(extra)))

    def test_common_decision_fields_never_reach_the_signature(self):
        """DECISION 层通用字段（project/conversation_ref/goal…）不得流进方法签名。

        这是生产两次 TypeError 的共同来源：模型每个动作都必须带 project。
        """
        for op in OPS:
            base = dict(op=op, agent_id='companion-agent_x', project='某个项目',
                        conversation_ref='conversation:x', goal='顺手带上的')
            if op == 'send':
                base['text'] = '做点事'
            if op == 'read_artifact':
                base['artifact_id'] = 'art1'
            fields = _action_fields(dict(base))
            allowed = _accepts(METHOD[op])
            if allowed is None:
                continue
            for key in fields:
                if key in ('goal',) and op == 'create':
                    continue  # create 的 goal 由 dispatch 单独 pop 后作位置参数
                self.assertIn(key, allowed, '%s 展开了方法不接受的键 %r' % (op, key))

    def test_inspect_with_project_passes(self):
        """生产 00:23:38 的原形：inspect 带 project 必须不再 TypeError。"""
        fields = _action_fields(dict(op='inspect', agent_id='a', project='p'))
        self.assertNotIn('project', fields, 'project 不是 inspect 的参数，必须剥掉')
        self.assertEqual(fields.get('agent_id'), 'a')


if __name__ == '__main__':
    unittest.main()
