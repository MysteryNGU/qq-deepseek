#!/bin/sh
# P8 证据回收：一次跑完三块。
#   工作区放 /tmp（脚本要 copytree 源码树，绝不能放在生产树或归档目录里）
#   归档放 /opt/qq-deepseek/.acceptance/<时间戳>（只收结果，不放工作区）
# 全程不重启服务、不写 /var/lib/companion。
set -e
A=${1:?usage: p8_collect.sh <archive-dir>}
W=/tmp/p8work-$(date +%Y%m%d-%H%M%S)
rm -rf "$W"
mkdir -p "$A"
mkdir -p "$W"

snap() {
  stat -c '%n %s %Y' /var/lib/companion/xiaoyu/data/life.db \
      /var/lib/companion/xiaoyu/data/life.db-wal \
      /var/lib/companion/xiaoyu/data/life.db-shm \
      /var/lib/companion/xiaoyu/config.toml > "$A/prod-$1.txt"
  sha256sum /var/lib/companion/xiaoyu/config.toml > "$A/prod-config-sha-$1.txt"
  systemctl show companion.service -p ActiveState -p SubState -p ActiveEnterTimestamp \
      -p NRestarts -p MainPID -p ExecMainStartTimestamp > "$A/service-$1.txt"
  date -Is > "$A/time-$1.txt"
}

snap before

echo '=== matrix (100 items)'
cd /opt/qq-deepseek
set +e
timeout 1200 .venv-companion/bin/python -B -X utf8 tools/stage-c/verify_awareness_agents.py \
    "$W/matrix" > "$A/matrix.log" 2>&1
echo "matrix_exit=$?"
set -e
cp "$W/matrix/result.json" "$A/matrix-result.json" 2>/dev/null || true

echo '=== e2e (F2 + budget)'
set +e
timeout 600 .venv-companion/bin/python -B -X utf8 tools/stage-c/verify_repair_e2e.py \
    "$W/e2e" > "$A/e2e.log" 2>&1
echo "e2e_exit=$?"
set -e
cp "$W/e2e/e2e_result.json" "$A/e2e-result.json" 2>/dev/null || true

snap after

echo '=== clean workspace'
sh /opt/qq-deepseek/tools/stage-c/deep_clean.sh "$W" || true
ls -d /tmp/p8* /tmp/p3* 2>/dev/null > "$A/residue-after-clean.txt" || : > "$A/residue-after-clean.txt"

echo '--- matrix summary'
grep -a '^SUMMARY' "$A/matrix.log" || true
grep -a '^FAILED' "$A/matrix.log" || true
echo '--- e2e summary'
grep -a '^E2E-SUMMARY' "$A/e2e.log" || true
grep -a '^FAILED' "$A/e2e.log" || true
echo '--- residue'
cat "$A/residue-after-clean.txt"
echo '--- service'
cat "$A/service-before.txt"
echo
cat "$A/service-after.txt"
