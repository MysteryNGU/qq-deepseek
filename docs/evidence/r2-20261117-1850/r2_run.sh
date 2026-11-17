#!/bin/sh
# R2 第二轮验收外壳：工作区 /tmp，归档 .acceptance/<ts>-r2/。不重启服务、不写生产库。
set -e
A=${1:?usage: r2_run.sh <archive-dir> [offline|escape]}
MODE=${2:-offline}
W=/tmp/p11r2-$(date +%Y%m%d-%H%M%S)
mkdir -p "$A" "$W"
echo "archive=$A work=$W mode=$MODE"

snap() {
  stat -c '%n %s %Y' /var/lib/companion/xiaoyu/data/life.db \
      /var/lib/companion/xiaoyu/data/life.db-wal /var/lib/companion/xiaoyu/config.toml > "$A/prod-$1.txt"
  sha256sum /var/lib/companion/xiaoyu/config.toml > "$A/prod-config-sha-$1.txt"
  systemctl show companion.service -p ActiveState -p SubState -p ActiveEnterTimestamp \
      -p NRestarts -p MainPID > "$A/service-$1.txt"
  date -Is > "$A/time-$1.txt"
  python3 /opt/qq-deepseek/tools/stage-c/d3_ledger_count.py > "$A/ledger-$1.txt" 2>&1 || true
}

snap before
cd /opt/qq-deepseek
set +e
if [ "$MODE" = "escape" ]; then
  timeout 600 .venv-companion/bin/python -B -X utf8 tools/stage-c/r2_escape.py "$W" > "$A/escape.log" 2>&1
  echo "escape_exit=$?"
  cp "$W/r2_escape.json" "$A/r2-escape.json" 2>/dev/null || true
  cp "$W/escape_blob.txt" "$A/escape_blob.txt" 2>/dev/null || true
else
  timeout 600 .venv-companion/bin/python -B -X utf8 tools/stage-c/r2_acceptance.py "$W" > "$A/offline.log" 2>&1
  echo "offline_exit=$?"
  cp "$W/r2_offline.json" "$A/r2-offline.json" 2>/dev/null || true
fi
set -e
snap after

echo '=== summary'
grep -a '^R2-' "$A"/*.log || true
echo '=== failed'
grep -a '^FAILED' "$A"/*.log || echo '(none)'
echo '=== ledger before/after'
cat "$A/ledger-before.txt" "$A/ledger-after.txt"

sh /opt/qq-deepseek/tools/stage-c/deep_clean.sh "$W" || true
ls -d /tmp/p11* /tmp/p10* 2>/dev/null > "$A/residue.txt" || : > "$A/residue.txt"
echo "archive_dir=$A"
