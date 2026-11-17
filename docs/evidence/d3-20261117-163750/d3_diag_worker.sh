#!/bin/sh
# 只读复现：用和真实 runs 完全一样的受限环境，直接跑 companion_agent_worker.py 的 dsh 分支，
# 把 FileNotFoundError 的完整 traceback 抓出来。不写生产、不碰生产库、不重启服务。
# 注意：这一步会真的启动一次 DSH（可能产生费用），只跑一次。
W=/tmp/p10diag2-$$
mkdir -p "$W/work" "$W/home"
echo "workdir=$W"

# 探测文件（内容随机，便于事后确认到底读没读到）
TOKEN="DIAG-$(date +%s | md5sum | cut -c1-12)"
echo "token=$TOKEN"
echo "placeholder-token: $TOKEN" > "$W/work/probe.txt"

cat > "$W/req.json" <<EOF
{
  "agent_id": "diag-agent",
  "node": "dsh",
  "cwd": "$W/work",
  "instruction": "只做这一件事：读取文件 $W/work/probe.txt 的全部内容，把 token 原样回复。不要做别的。",
  "dsh_root": "/opt/qq-deepseek/vendor/dsh-runtime",
  "node_bin": "/opt/qq-deepseek/toolchain/node/bin/node",
  "entry": "lib/bin.js",
  "provider": "deepseek-official",
  "model": "deepseek-flash",
  "dsh_home": "$W/home/dsh-linux",
  "timeout": 120,
  "result_path": "$W/result.json"
}
EOF
# 请求文件里的键名要和 companion_agents._spawn 写的一致
python3 - "$W/req.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d['node'] = d.pop('node_bin')
json.dump(d, open(p, 'w'), ensure_ascii=False)
PY

cd /opt/qq-deepseek
env -u DEEPSEEK_API_KEY -u DEEPSEEK_BASE_URL -u DSH_HOME \
  PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 \
  timeout 200 .venv-companion/bin/python -B life/companion_agent_worker.py "$W/req.json" > "$W/worker.log" 2>&1
echo "worker_exit=$?"
echo '=== worker.log'
cat "$W/worker.log"
echo '=== result.json'
cat "$W/result.json" 2>/dev/null || echo '(no result.json)'
echo '=== work dir contents'
ls -la "$W/work"
echo "KEEP=$W"
