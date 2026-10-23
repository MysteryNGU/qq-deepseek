"""Persistent publication eligibility, checked again at the actual source switch."""
from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path


def capture(change,scope):
    repo = change.repo
    has_sessions = repo._one("SELECT 1 FROM sqlite_master WHERE name='agent_sessions'")
    root = repo._one("SELECT * FROM agent_sessions WHERE scope_id=? AND status='current' "
        'AND parent_session_id IS NULL ORDER BY created DESC LIMIT 1',(scope.scope_id,)) if has_sessions else None
    versions = {}
    if root:
        ref = root['intent_ref']
        while ref:
            if ref in versions:
                raise ValueError('cyclic_publication_goal')
            row = repo._one('SELECT v FROM meta WHERE k=?',('action_task:'+ref,))
            task = json.loads(row['v']) if row else {}
            versions[ref] = task.get('revision',1)
            ref = task.get('parent_ref')
    return dict(database=str(repo.path.resolve()),scope_id=scope.scope_id,
        session_id=root['session_id'] if root else None,
        forget_epoch=repo.forget_epoch(scope.scope_id),intent_versions=versions)


def status(db,origin):
    scope = origin['scope_id']
    row = db.execute('SELECT epoch FROM forget_epochs WHERE scope_id=?',(scope,)).fetchone()
    if (row[0] if row else 0)!=origin['forget_epoch']:
        return 'cancelled','source_forgotten'
    if origin.get('session_id'):
        root = db.execute('SELECT status FROM agent_sessions WHERE session_id=?',(origin['session_id'],)).fetchone()
        if not root or root[0]!='current':
            return 'cancelled','source_session_retired'
    for ref,revision in origin.get('intent_versions',{}).items():
        row = db.execute('SELECT v FROM meta WHERE k=?',('action_task:'+ref,)).fetchone()
        task = json.loads(row[0]) if row else {}
        if not task or task.get('revision',1)!=revision or task.get('status') in {'cancelled','done'}:
            return 'cancelled','goal_cancelled_or_revised'
        if task.get('status')!='active':
            return 'held','goal_waiting_or_paused'
    # New input has priority even before its meaning has been interpreted.
    row = db.execute('SELECT 1 FROM agent_inputs i JOIN agent_sessions s USING(session_id) '
        "WHERE s.scope_id=? AND s.status='current' AND i.handled=0 AND i.state!='rejected' LIMIT 1",(scope,)).fetchone() if origin.get('session_id') else None
    if row:
        return 'held','input_not_understood'
    return 'eligible',''


@contextmanager
def guard(origin,*,write=False):
    if not origin or not origin.get('database'):
        yield 'eligible','',None
        return
    path = Path(origin['database']).resolve()
    if not path.is_file():
        yield 'cancelled','source_database_removed',None
        return
    db = sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=10)
    try:
        if write:
            db.execute('BEGIN IMMEDIATE')
        state,reason = ('eligible','') if origin.get('kind')=='operator' else status(db,origin)
        yield state,reason,db
        if write:
            db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
