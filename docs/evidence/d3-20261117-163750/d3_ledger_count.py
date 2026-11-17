"""生产台账只读计数（D3 前后各跑一次，证明没往生产库写）。"""
import sqlite3

DB = 'file:/var/lib/companion/xiaoyu/data/life.db?mode=ro'
c = sqlite3.connect(DB, uri=True)
for table in ('companion_agent_tasks', 'companion_agent_events', 'companion_usage', 'spending_calls',
              'companion_actions'):
    print(table, c.execute('SELECT COUNT(*) FROM %s' % table).fetchone()[0])
row = c.execute("SELECT COUNT(*) FROM companion_actions WHERE json_extract(action,'$.kind')='agent'").fetchone()
print('agent_kind_actions', row[0])
