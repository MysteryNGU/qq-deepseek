#!/bin/sh
# D3 跑一次：工作区在 /tmp，证据归档到 .acceptance/<ts>-d3/。不重启服务、不写生产库。
set -e
MODE=${1:-dry-run}
TS=$(date +%Y%m%d-%H%M%S)
A=/opt/qq-deepseek/.acceptance/${TS}-d3
W=/tmp/p10d3-${TS}
mkdir -p "$A" "$W"
echo "archive=$A work=$W mode=$MODE"

snap() {
  stat -c '%n %s %Y' /var/lib/companion/xiaoyu/data/life.db \
      /var/lib/companion/xiaoyu/data/life.db-wal \
      /var/lib/companion/xiaoyu/data/life.db-shm \
      /var/lib/companion/xiaoyu/config.toml > "$A/prod-$1.txt"
  sha256sum /var/lib/companion/xiaoyu/config.toml > "$A/prod-config-sha-$1.txt"
  systemctl show companion.service -p ActiveState -p SubState -p ActiveEnterTimestamp \
      -p NRestarts -p MainPID > "$A/service-$1.txt"
  date -Is > "$A/time-$1.txt"
  # 生产台账计数（只读）
  python3 /opt/qq-deepseek/tools/stage-c/d3_ledger_count.py > "$A/ledger-$1.txt" 2>&1 || true
}

snap before
cd /opt/qq-deepseek
set +e
timeout 420 .venv-companion/bin/python -B -X utf8 tools/stage-c/d3_real_delegation.py "$W" --$MODE \
    > "$A/d3.log" 2>&1
echo "d3_exit=$?"
set -e
cp "$W/d3_result.json" "$A/d3-result.json" 2>/dev/null || true
snap after

echo '=== summary'
grep -a '^D3-SUMMARY' "$A/d3.log" || true
echo '=== failed lines'
grep -a '^\[FAIL\]' "$A/d3.log" || echo '(none)'
echo '=== ledger before/after'
cat "$A/ledger-before.txt" "$A/ledger-after.txt"

# 清理工作区（保留归档）
sh /opt/qq-deepseek/tools/stage-c/deep_clean.sh "$W" || true
ls -d /tmp/p10d3-* /tmp/p8* /tmp/p3* 2>/dev/null > "$A/residue.txt" || : > "$A/residue.txt"
echo "archive_dir=$A"
