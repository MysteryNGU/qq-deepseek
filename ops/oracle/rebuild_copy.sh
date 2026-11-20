#!/bin/bash
# 重建 oracle 副本（golden 契约用）。
# 教训：不要在临时 home 里放 life.db 的副本或硬链接，直接改 cfg.life_db 指向副本。
set -e
SRC_DB=/var/lib/companion/xiaoyu/data/life.db
SRC_HOME=/var/lib/companion/xiaoyu
BASE=/var/lib/companion/oracle
TS=$(date +%Y%m%d-%H%M%S)
COPY="$BASE/oracle_$TS.db"
HOME_DIR="$BASE/home_$TS"

mkdir -p "$BASE"
python3 - "$SRC_DB" "$COPY" <<'PY'
import sqlite3, sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect('file:%s?mode=ro' % src_path, uri=True)
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)
src.close(); dst.close()
print('backup written:', dst_path)
PY

mkdir -p "$HOME_DIR/data"
cp -f "$SRC_HOME/config.toml" "$HOME_DIR/" 2>/dev/null || true
cp -f "$SRC_HOME/persona.md" "$HOME_DIR/" 2>/dev/null || true

chown -R companion:companion "$BASE" 2>/dev/null || true

cat <<EOF

副本: $COPY
home: $HOME_DIR  （注意：里面不要放 life.db，脚本里设 cfg.life_db = "$COPY"）

用法（Python）：
    cfg = load_config(home / 'config.toml')
    cfg.life_db = '$COPY'      # 绝对路径；Path(home)/abs -> abs
    core = Companion(cfg, home, model=M())
EOF
