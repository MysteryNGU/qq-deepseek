#!/bin/sh
# 只读：检查 companion_agent_worker.run_dsh 依赖的每个路径是否存在。
W=/tmp/p10diag4-$$
mkdir -p "$W"
R=/opt/qq-deepseek/vendor/dsh-runtime
N=/opt/qq-deepseek/toolchain/node/bin/node
echo '=== 依赖路径'
for p in "$R/lib/bin.js" "$R/apps/cli/src/bin.ts" "$R/node_modules/tsx/package.json" \
         "$R/python/sdk/src/deepseek_harness" "$N" \
         /tmp/p10diag3-904746/home/dsh-linux; do
  if [ -e "$p" ]; then echo "OK   $p"; else echo "MISS $p"; fi
done
echo
echo '=== worker 里 run_dsh 用到的相对/缺省路径'
grep -n "cwd\|dsh_home\|Path(" /opt/qq-deepseek/life/companion_agent_worker.py | sed -n '1,40p'
echo
echo '=== 同样的请求，但 cwd 换成已存在的目录再试一次（定位是不是 cwd 引起的）'
mkdir -p "$W/exists"
python3 - "$W" <<'PY'
import json, sys, os
w = sys.argv[1]
request = dict(agent_id='diag-agent', node='dsh',
               dsh_node='/opt/qq-deepseek/toolchain/node/bin/node',
               cwd=os.path.join(w, 'exists'),
               instruction='回复 hi', dsh_root='/opt/qq-deepseek/vendor/dsh-runtime',
               entry='lib/bin.js', provider='deepseek-official', model='deepseek-flash',
               dsh_home=os.path.join(w, 'home2/dsh-linux'), timeout=60,
               result_path=os.path.join(w, 'result2.json'))
json.dump(request, open(os.path.join(w, 'req2.json'), 'w'), ensure_ascii=False)
PY
cd /opt/qq-deepseek
env -u DEEPSEEK_API_KEY -u DEEPSEEK_BASE_URL -u DSH_HOME PYTHONIOENCODING=utf-8 \
  timeout 120 .venv-companion/bin/python -B -X utf8 life/companion_agent_worker.py "$W/req2.json" 2>&1 | tail -5
echo '=== result2'
cat "$W/result2.json" 2>/dev/null || echo '(none)'
rm -rf "$W"
