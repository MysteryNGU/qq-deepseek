"""One-way activation of the sole memory engine, retaining native evidence."""
from .life_catalog import encode

VERSION = 2


def activate(life):
    repo,agent=life.repo,life.agent_id
    key='memory_schema:'+agent
    old=repo._one('SELECT v FROM meta WHERE k=?',(key,))
    if old:
        if int(old['v'])!=VERSION:
            raise ValueError('unsupported_memory_schema')
        life.catalog.backfill(limit=100)
        return
    # Rebuild the authoritative resource index before removing the old one.
    for kind,table in [('event','events'),('memory','memories')]:
        rows=repo._all(f'SELECT id FROM {table} WHERE agent_id=? ORDER BY rowid',(agent,))
        for offset in range(0,len(rows),200):
            with repo.tx():
                for row in rows[offset:offset+200]:
                    life.catalog.register(kind,row['id'])
    with repo.tx():
        if repo._one("SELECT 1 FROM sqlite_master WHERE name='agent_sessions'"):
            for row in repo._all("SELECT session_id,generation FROM agent_sessions WHERE status IN ('current','delegate') AND json_extract(scope_json,'$.agent_id')=?",(agent,)):
                sid=row['session_id']
                repo._exec("UPDATE agent_sessions SET status='retired' WHERE session_id=?",(sid,))
                repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('life_retire_reader:'+sid,
                    encode(dict(agent_id=agent,generation=row['generation']))))
            for table,column in [('agent_events','payload'),('life_calls','receipt'),('agent_inputs','payload')]:
                repo._exec(f"UPDATE {table} SET {column}='{{}}' WHERE session_id IN "
                    "(SELECT session_id FROM agent_sessions WHERE json_extract(scope_json,'$.agent_id')=?)",(agent,))
        # Historical evidence stays searchable. The first new hourly window
        # starts here; importing years of history is not a new paid review.
        high=repo._one('SELECT COALESCE(MAX(ingest_seq),0) AS n FROM events WHERE agent_id=?',(agent,))['n']
        repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('memory_organize_cursor:'+agent,str(high)))
        repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('memory_review_checked:'+agent,str(life.clock() if high else 0)))
        repo._exec('DROP TABLE IF EXISTS memory_terms')
        repo._exec("DELETE FROM meta WHERE k='life_schema:memory-v1'")
        repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,str(VERSION)))
