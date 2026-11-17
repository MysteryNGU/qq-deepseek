#!/bin/sh
# 只读：直接调 run_dsh 拿完整 traceback（绕过 main() 的异常折叠）。
W=/tmp/p10diag5-$$
mkdir -p "$W/home"
cat > "$W/probe.py" <<'PY'
import json, os, sys, traceback
sys.path.insert(0, '/opt/qq-deepseek')
from life import companion_agent_worker as worker
request = dict(node='dsh', dsh_node='/opt/qq-deepseek/toolchain/node/bin/node',
               cwd=os.environ['PROBE_CWD'], instruction='只回复 hi',
               dsh_root='/opt/qq-deepseek/vendor/dsh-runtime', entry='lib/bin.js',
               provider='deepseek-official', model='deepseek-flash',
               dsh_home=os.environ['PROBE_HOME'], timeout=60)
print('cwd exists:', os.path.isdir(request['cwd']), request['cwd'])
print('dsh_home:', request['dsh_home'])
try:
    out = worker.run_dsh(request)
    print('run_dsh ok:', json.dumps(out, ensure_ascii=False))
except Exception:
    traceback.print_exc()
PY
cd /opt/qq-deepseek
PROBE_CWD=/opt/qq-deepseek PROBE_HOME="$W/home/dsh-linux" \
  env -u DEEPSEEK_API_KEY -u DEEPSEEK_BASE_URL -u DSH_HOME PYTHONIOENCODING=utf-8 \
  timeout 120 .venv-companion/bin/python -B -X utf8 "$W/probe.py" 2>&1 | tail -30
rm -rf "$W"
