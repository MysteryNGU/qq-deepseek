"""类结构断言：往 Companion / BackgroundJobs 插方法后必须跑这一组。

起因（2026-11-20）：把模块级函数错插进类体，使 class Companion 提前结束，
_messages / _self_process_facts 等 60+ 个方法全部掉出类作用域
（属性数 133 → 70）。AST 仍能 parse，行文本也一字未变——
变的是缩进归属，所以"方法行文本没丢"这种检查看不出来。

两道断言：
  ① ast：这些方法必须在 Cls.body 里，且同名函数不得出现在模块级
  ② 真 import：hasattr(Cls, name) 必须为 True

用法：python tests/test_class_structure.py   （退出码 0 = 通过）
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# (文件, 类名, 必须在类里的方法)
TARGETS = [
    ('life/companion.py', 'Companion', [
        '_messages', '_self_process_facts', '_version_control', '_world_facts',
        '_run_cognition', '_cognize', '_prefetch_recall', '_index_memories',
        '_call_model', '_available_destinations', '_message_route',
    ]),
    ('life/companion_background.py', 'BackgroundJobs', [
        '_mark_interrupted_on_start', 'reap_all', 'list', 'start', 'stop', 'wait',
        'status', 'logs', 'reap_all',
    ]),
]

failures = []


def check_ast(path, cls_name, needed):
    src = (ROOT / path).read_text('utf8')
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        failures.append('%s: SyntaxError %s' % (path, e))
        return
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name), None)
    if cls is None:
        failures.append('%s: 找不到 class %s' % (path, cls_name))
        return
    methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    module_funcs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in needed:
        if name not in methods:
            failures.append('%s: %s 缺方法 %s（类里共 %d 个）' % (path, cls_name, name, len(methods)))
        if name in module_funcs:
            failures.append('%s: %s 被定义在模块级（应为类方法）' % (path, name))
    print('  ast   %-32s %-16s %d 个方法' % (path, cls_name, len(methods)))


def check_import(path, cls_name, needed):
    module_name = path[:-3].replace('/', '.')
    try:
        module = __import__(module_name, fromlist=['*'])
    except Exception as e:  # noqa: BLE001
        failures.append('%s: import 失败 %s: %s' % (module_name, type(e).__name__, e))
        return
    cls = getattr(module, cls_name, None)
    if cls is None:
        failures.append('%s: 模块里没有 %s' % (module_name, cls_name))
        return
    for name in needed:
        if not hasattr(cls, name):
            failures.append('%s.%s 缺属性 %s' % (cls_name, cls_name, name))
    total = len([x for x in dir(cls) if not x.startswith('__')])
    print('  import %-31s %-16s %d 个属性' % (module_name, cls_name, total))


print('=== 类结构断言 ===')
for path, cls_name, needed in TARGETS:
    check_ast(path, cls_name, needed)
    check_import(path, cls_name, needed)

print()
if failures:
    print('失败 %d 项：' % len(failures))
    for f in failures:
        print('  - %s' % f)
    sys.exit(1)
print('STRUCTURE_OK')
