"""Reload only this supervised Bot at an idle turn boundary; keep QQ running."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import time

import psutil

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from life.role_host import source_signature


def main():
    health_path=ROOT/'data/life-control/worker-health.json'
    expected=source_signature(ROOT)
    deadline=time.monotonic()+180
    stable_since=None
    while time.monotonic()<deadline:
        with sqlite3.connect((ROOT/'data/life.db').as_uri()+'?mode=ro',uri=True) as db:
            busy=db.execute("SELECT COUNT(*) FROM agent_sessions s JOIN agent_events a USING(session_id) "
                "WHERE s.status='current' AND json_extract(a.payload,'$.type')='turn/start' AND "
                "a.seq=(SELECT MAX(b.seq) FROM agent_events b WHERE b.session_id=s.session_id "
                "AND json_extract(b.payload,'$.type') IN ('turn/start','turn/end'))").fetchone()[0]
            busy+=db.execute("SELECT COUNT(*) FROM work_jobs WHERE kind='local_media' AND status='running'").fetchone()[0]
        if busy:
            stable_since=None
        elif stable_since is None:
            stable_since=time.monotonic()
        elif time.monotonic()-stable_since>=2:
            break
        time.sleep(.5)
    else:
        raise RuntimeError('bot_still_busy_no_restart_performed')
    if source_signature(ROOT)!=expected:
        raise RuntimeError('source_changed_during_wait_no_restart_performed')
    health=json.loads(health_path.read_text('utf-8'))
    if health['runtime']['source_signature']==expected:
        print('Runtime already loaded; no restart needed.');return
    if time.time()-health['at']>20:
        raise RuntimeError('stale_bot_ownership')
    process=psutil.Process(health['pid'])
    command=process.cmdline()
    if not any(Path(part).resolve()==ROOT/'bot.py' for part in command if part.endswith('bot.py')):
        raise RuntimeError('bot_process_identity_mismatch')
    if not any(any(str(arg).endswith('lifesupervisor.py') for arg in parent.cmdline()) for parent in process.parents()):
        raise RuntimeError('owned_supervisor_not_found')
    previous_pid=process.pid
    process.terminate()
    print(json.dumps(dict(restarting_bot_pid=previous_pid,qq_process_unchanged=True)),flush=True)
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        try:
            health=json.loads(health_path.read_text('utf-8'))
            if (health['pid']!=previous_pid and time.time()-health['at']<20 and
                    health['runtime']['source_signature']==expected and health['cognition_ready']):
                proof=dict(pid=health['pid'],source_signature=expected,cognition_ready=True,
                    channel_connected=health['channel_connected'],reloaded_at=time.time())
                (ROOT/'data/maintenance/tts-latency/deployment.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
                print(json.dumps(proof),flush=True);return
        except (OSError,ValueError,KeyError):
            pass
        time.sleep(.5)
    raise RuntimeError('waiting_for_supervisor_reload')


if __name__=='__main__':
    main()
