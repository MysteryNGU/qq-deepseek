#!/bin/sh
# 只读定位：companion_agent_worker.py 的 DSH 分支在拿不到凭据时会不会抛 FileNotFoundError。
# 不跑模型、不花钱。
W=/tmp/p10diag-$$
mkdir -p "$W"
cat > "$W/probe.py" <<'PY'
import json, os, sys, traceback
root = '/opt/qq-deepseek/vendor/dsh-runtime'
sys.path.insert(0, root + '/python/sdk/src')
print('DEEPSEEK_API_KEY in env:', 'DEEPSEEK_API_KEY' in os.environ)
print('DEEPSEEK_BASE_URL in env:', 'DEEPSEEK_BASE_URL' in os.environ)
print('DSH_HOME in env:', 'DSH_HOME' in os.environ)
try:
    from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
except Exception:
    traceback.print_exc(); raise SystemExit(0)
home = '/tmp/p10diag-home'
os.makedirs(home, exist_ok=True)
try:
    cfg = DeepSeekHarnessConfig(provider='deepseek-official', model='deepseek-flash',
                                max_tokens=4096, cwd=os.getcwd(), runtime_cwd=root,
                                profile='sdk-minimal', dsh_home=home,
                                env=dict(DSH_HOME=home), initialize_timeout_seconds=90,
                                request_timeout_seconds=30)
    print('config built ok')
    node = '/opt/qq-deepseek/toolchain/node/bin/node'
    launch = (node, root + '/lib/bin.js', '--profile', 'sdk-minimal')
    h = DeepSeekHarness(cfg, _launch_args=launch)
    print('harness constructed')
    try:
        r = h.run('say hi')
        print('run ok', getattr(r, 'finish_reason', None))
    except Exception as error:
        print('run raised:', type(error).__name__, error)
        traceback.print_exc()
    finally:
        try:
            h.close()
        except Exception:
            pass
except Exception as error:
    print('construct raised:', type(error).__name__, error)
    traceback.print_exc()
PY
cd /opt/qq-deepseek
env -u DEEPSEEK_API_KEY -u DEEPSEEK_BASE_URL .venv-companion/bin/python -B "$W/probe.py" 2>&1 | tail -25
rm -rf "$W" /tmp/p10diag-home
