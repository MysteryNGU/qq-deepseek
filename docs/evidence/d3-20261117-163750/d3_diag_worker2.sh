#!/bin/sh
# 只读复现（修正版）：用和真实 run 完全一样的受限环境跑 companion_agent_worker.py 的 dsh 分支，
# 抓出 FileNotFoundError 的完整 traceback。请求文件的关键字必须与 companion_agents._spawn 一致：
#   node='dsh'（选后端），另带 node 可执行文件路径用 'node_bin' 之外的键会被 execute() 忽略。
W=/tmp/p10diag3-$$
mkdir -p "$W/work" "$W/home"
echo "workdir=$W"
TOKEN="DIAG-$(date +%s | md5sum | cut -c1-12)"
echo "token=$TOKEN"
echo "placeholder-token: $TOKEN" > "$W/work/probe.txt"

python3 - "$W" "$TOKEN" <<'PY'
import json, sys, os
w, token = sys.argv[1], sys.argv[2]
request = dict(
    agent_id='diag-agent',
    node='dsh',                       # execute() 用它选后端
    dsh_node='/opt/qq-deepseek/toolchain/node/bin/node',
    cwd=os.path.join(w, 'work'),
    instruction=('只做这一件事：读取文件 %s/work/probe.txt 的全部内容，把 token 原样回复。不要做别的。' % w),
    dsh_root='/opt/qq-deepseek/vendor/dsh-runtime',
    entry='lib/bin.js',
    provider='deepseek-official',
    model='deepseek-flash',
    dsh_home=os.path.join(w, 'home/dsh-linux'),
    timeout=120,
    result_path=os.path.join(w, 'result.json'),
)
json.dump(request, open(os.path.join(w, 'req.json'), 'w'), ensure_ascii=False)
print('request keys:', sorted(request))
PY

cd /opt/qq-deepseek
env -u DEEPSEEK_API_KEY -u DEEPSEEK_BASE_URL -u DSH_HOME \
  PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 \
  timeout 200 .venv-companion/bin/python -B -X utf8 life/companion_agent_worker.py "$W/req.json" > "$W/worker.log" 2>&1
echo "worker_exit=$?"
echo '=== worker.log'
cat "$W/worker.log"
echo '=== result.json'
cat "$W/result.json" 2>/dev/null || echo '(no result.json)'
echo '=== work dir'
ls -la "$W/work"
echo "KEEP=$W"
