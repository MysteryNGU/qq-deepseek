#!/bin/sh
# 反复删：node_modules 那种深树一次 rmdir 删不干净，多跑几轮并把只读位放开。
T=${1:?target required}
case "$T" in
  /tmp/p8*|/tmp/p3*|/tmp/p10d3-*|/tmp/p11*|/tmp/p10*) : ;;
  *) echo "refused: $T" >&2; exit 2 ;;
esac
i=0
while [ -e "$T" ] && [ $i -lt 8 ]; do
  chmod -R u+rwX "$T" 2>/dev/null
  find "$T" -type d -exec chmod u+rwX {} + 2>/dev/null
  rm -rf "$T" 2>/dev/null
  i=$((i + 1))
done
[ -e "$T" ] && { echo "STILL THERE: $T"; exit 1; }
echo "removed: $T after $i pass(es)"
