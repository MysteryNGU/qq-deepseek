"""重启前核对（只读）：服务单元、在跑的行动、磁盘权限、重启前状态。

用法：python precheck.py <label>   label=pre|post
不写任何东西，只打印；生产库以只读 URI 打开。
"""
import json
import sqlite3
import subprocess
import sys
import time

label = sys.argv[1] if len(sys.argv) > 1 else 'pre'
out = {'label': label, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


unit = sh('systemctl cat companion.service')
out['unit_ok'] = all(bit in unit for bit in (
    'ExecStart=/opt/qq-deepseek/.venv-companion/bin/python /opt/qq-deepseek/companion.py --home '
    '/var/lib/companion/xiaoyu --serve --host 127.0.0.1 --port 8765', 'User=companion',
    'UMask=0077', 'KillMode=control-group'))
try:
    con = sqlite3.connect('file:/var/lib/companion/xiaoyu/data/life.db?mode=ro', uri=True)
    out['actions'] = {status: con.execute(
        'select count(*) from companion_actions where status=?', (status,)).fetchone()[0]
        for status in ('running', 'pending')}
    con.close()
except Exception as error:  # noqa: BLE001
    out['actions'] = 'ERROR: %s' % error
out['disk'] = sh('df -h / | tail -1')
out['home_perms'] = sh('stat -c "%a %U:%G" /var/lib/companion/xiaoyu')
out['active_ts'] = sh('systemctl show companion.service -p ActiveEnterTimestamp -p NRestarts -p ActiveState')
out['db_mtime'] = sh('stat -c "%y %s" /var/lib/companion/xiaoyu/data/life.db')
print(json.dumps(out, ensure_ascii=False, indent=2))
