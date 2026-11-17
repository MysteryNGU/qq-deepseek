"""只读验证：把 _launch_args 补上之后，DSH 能不能真的起来（不连模型就先只看能否 initialize）。

不写产品代码；只是在本进程里用 SDK 的公开参数跑一遍，确认修法方向对。
真正花钱的那一步留给修好之后的 D3 正式跑。
"""
import json
import os
import sys

ROOT = '/opt/qq-deepseek/vendor/dsh-runtime'
NODE = '/opt/qq-deepseek/toolchain/node/bin/node'
sys.path.insert(0, os.path.join(ROOT, 'python/sdk/src'))
work = sys.argv[1]
os.makedirs(work, exist_ok=True)

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig  # noqa: E402

home = os.path.join(work, 'dsh-home')
os.makedirs(home, exist_ok=True)
cfg = DeepSeekHarnessConfig(provider='deepseek-official', model='deepseek-flash',
                            max_tokens=512, cwd=work, runtime_cwd=ROOT,
                            profile='sdk-minimal', dsh_home=home,
                            env=dict(DSH_HOME=home), initialize_timeout_seconds=90,
                            request_timeout_seconds=60)
launch = (NODE, os.path.join(ROOT, 'lib/bin.js'), '--profile', 'sdk-minimal')
print('launch args:', launch)
harness = DeepSeekHarness(cfg, _launch_args=launch)
try:
    harness.start()
    print('DSH runtime started OK (initialize round-trip succeeded)')
except Exception as error:
    print('start failed:', type(error).__name__, error)
    raise SystemExit(1)
finally:
    try:
        harness.close()
    except Exception:
        pass
print(json.dumps(dict(started=True, node=NODE, entry=os.path.join(ROOT, 'lib/bin.js')), ensure_ascii=False))
