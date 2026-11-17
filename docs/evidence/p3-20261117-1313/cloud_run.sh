#!/bin/sh
# P3 云端隔离验收的启动外壳（不改产品代码，只跑验收脚本）。
set -e
R=${1:-/tmp/p3verify-$(date +%Y%m%d-%H%M%S)}
rm -rf "$R"
mkdir -p "$R"
# 生产库与配置的只读指纹：跑前跑后各记一次，用来证明验收没碰生产数据。
stat -c '%n %s %Y' /var/lib/companion/xiaoyu/data/life.db \
    /var/lib/companion/xiaoyu/data/life.db-wal \
    /var/lib/companion/xiaoyu/data/life.db-shm \
    /var/lib/companion/xiaoyu/config.toml > "$R/prod-before.txt"
sha256sum /var/lib/companion/xiaoyu/config.toml > "$R/prod-config-sha-before.txt"
cd /opt/qq-deepseek
set +e
timeout 1200 .venv-companion/bin/python -B -X utf8 tools/stage-c/verify_awareness_agents.py "$R" > "$R/run.log" 2>&1
code=$?
set -e
echo "runner_exit=$code"
stat -c '%n %s %Y' /var/lib/companion/xiaoyu/data/life.db \
    /var/lib/companion/xiaoyu/data/life.db-wal \
    /var/lib/companion/xiaoyu/data/life.db-shm \
    /var/lib/companion/xiaoyu/config.toml > "$R/prod-after.txt"
sha256sum /var/lib/companion/xiaoyu/config.toml > "$R/prod-config-sha-after.txt"
echo '--- tail'
tail -20 "$R/run.log"
echo "--- summary line"
grep -a '^SUMMARY' "$R/run.log" || true
echo "--- failed lines"
grep -a '^FAILED' "$R/run.log" || echo '(none)'
exit 0
