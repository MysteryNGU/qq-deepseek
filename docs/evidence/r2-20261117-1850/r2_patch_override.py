"""决定性实验：patch 是「叠加」还是「覆盖」——用 SDK 自己的 --dump-config 判定。

两种写法各跑一次 dump-config，看 composed tree 里 sandbox-policy 的 mode 最终值：

  A) 现状写法：`- insert: [...]`（t20 生成的那种）
  B) 替代写法：直接给一份同 id 的条目，让后层覆盖（不 insert）

不跑模型、不花钱，只看配置合成结果。
"""
import os
import subprocess
import sys

NODE = '/opt/qq-deepseek/toolchain/node/bin/node'
ROOT = '/opt/qq-deepseek/vendor/dsh-runtime'
BIN = os.path.join(ROOT, 'lib/bin.js')
WORK = sys.argv[1]
os.makedirs(WORK, exist_ok=True)
HOME = os.path.join(WORK, 'home')
os.makedirs(HOME, exist_ok=True)
env = dict(os.environ, DSH_HOME=HOME)

patches = {
    'A_insert': '- insert:\n'
                '    - id: sandbox-policy\n'
                "      name: '@deepseek-ai/dsh-sandbox-policy'\n"
                '      config:\n'
                '        mode: workspace-write\n'
                '        workspaceRoot: "%s"\n' % WORK,
    'B_replace': '- id: sandbox-policy\n'
                 "  name: '@deepseek-ai/dsh-sandbox-policy'\n"
                 '  config:\n'
                 '    mode: workspace-write\n'
                 '    workspaceRoot: "%s"\n' % WORK,
}


def dump(patch_path=None):
    cmd = [NODE, BIN, '--profile', 'sdk-minimal']
    if patch_path:
        cmd += ['--patch', patch_path]
    cmd += ['--dump-config']
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=240, env=env)
    text = (out.stdout or '') + (out.stderr or '')
    modes = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if 'sandbox-policy' in line:
            for follow in lines[index:index + 6]:
                if 'mode:' in follow:
                    modes.append(follow.strip())
                    break
    return modes, text


base_modes, base_text = dump()
print('无补丁        :', base_modes)

results = {}
for label, body in patches.items():
    path = os.path.join(WORK, label + '.yml')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(body)
    modes, _text = dump(path)
    results[label] = modes
    print('%-14s:' % label, modes)

ok = {}
ok['baseline_danger'] = base_modes == ['mode: danger-full-access']
ok['insert_appends_not_replaces'] = results['A_insert'] == ['mode: danger-full-access',
                                                            'mode: workspace-write']
ok['replace_overrides'] = results['B_replace'] == ['mode: workspace-write']
print('\n判定:', ok)
raise SystemExit(0 if all(ok.values()) else 1)
