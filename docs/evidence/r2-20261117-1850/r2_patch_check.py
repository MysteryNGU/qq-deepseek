"""只读核查：_sandbox_patch 生成的补丁到底长什么样，DSH 认不认 --patch 这种文件。

不跑模型。只看：
  1. 补丁文件内容
  2. dsh CLI 是否支持 --patch（--help）
  3. sdk-minimal profile 里 sandbox-policy 的原始配置
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, '/opt/qq-deepseek')
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = sys.argv[1]
os.makedirs(WORK, exist_ok=True)

from life import companion_agents as agents  # noqa: E402
from life.role_config import load_config  # noqa: E402

cfg = load_config('/var/lib/companion/xiaoyu/config.toml')
cfg.life_db = 'data/life.db'
manager = agents.AgentManager(cfg, WORK, repo=None, agent_id='probe')

print('sandbox_mode:', manager._sandbox_mode())
patch = manager._sandbox_patch(WORK)
print('patch file:', patch)
print('--- patch content ---')
print(patch.read_text(encoding='utf-8'))

ROOT = '/opt/qq-deepseek/vendor/dsh-runtime'
NODE = '/opt/qq-deepseek/toolchain/node/bin/node'
print('--- dsh --help (grep patch) ---')
out = subprocess.run([NODE, os.path.join(ROOT, 'lib/bin.js'), '--help'],
                     capture_output=True, text=True, timeout=120)
text = (out.stdout or '') + (out.stderr or '')
print('\n'.join(line for line in text.splitlines() if 'patch' in line.lower())[:800])
print('--- sdk-minimal profile sandbox config ---')
for candidate in ('profiles/sdk-minimal', 'python/sdk/src'):
    base = os.path.join(ROOT, candidate)
    if not os.path.isdir(base):
        continue
    for root, _dirs, files in os.walk(base):
        for name in files:
            if name.endswith(('.yml', '.yaml', '.json')) and 'patch' in name.lower():
                path = os.path.join(root, name)
                body = open(path, encoding='utf-8', errors='replace').read()
                if 'sandbox' in body:
                    print('FILE', path)
                    for line in body.splitlines():
                        if 'sandbox' in line.lower() or 'mode' in line.lower() or 'workspaceRoot' in line:
                            print('   ', line.strip()[:160])
