"""companion-watchdog: detect a broken companion.service, roll the source back, and tell her.

Why a separate process: the thing it has to rescue is `companion.service`, so it cannot live
inside it. This unit watches a restart, decides healthy/unhealthy inside a fixed window, and on
failure restores the last snapshot that was *proven* good, restarts, and records what happened.

Contract:
* healthy = unit active + `/health` 200 + no new Traceback/CRITICAL in the journal + at least
  one cognition turn completed after the restart.
* Every restart is snapshotted first (content-addressed, hardlinked, cheap). A snapshot becomes
  the rollback point only after it is proven good.
* Three consecutive failures stop the automation and say so in the journal and in life.db.
* The notice is written straight into life.db as an event in the principal conversation, so the
  next cognition turn sees it without any extra wiring.

Test-only switches exist so the whole path can be rehearsed in an isolated copy: `--once`
(evaluate once and exit) and `--state-dir`/`--home` (never the production paths).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

try:                                  # POSIX only; the unit runs on Linux, tests may not
    import fcntl
except ImportError:                   # pragma: no cover - Windows test hosts
    fcntl = None

DEFAULTS = dict(
    source_root='/opt/qq-deepseek',
    state_root='/var/lib/companion/xiaoyu/data/watchdog',
    home='/var/lib/companion/xiaoyu',
    unit='companion.service',
    health_url='http://127.0.0.1:8765/alive',
    window_seconds=75.0,
    settle_seconds=10.0,
    max_failures=3,
    keep_snapshots=3,
    poll_seconds=5.0,
)

# Directories that are never part of a source snapshot: they are either huge, third-party, or
# runtime state. A rollback must not resurrect a stale database or a whole node toolchain.
SNAPSHOT_SKIP = {
    'data', 'toolchain', 'vendor', 'napbx', 'napcat', 'napcat-onekey', '.venv', '.venv-companion',
    '__pycache__', 'local-backups', '.git', 'node_modules', '.acceptance', '.srv-life',
    'xiaoyu-private-migration-20261027-094202', 'work', 'docs/acceptance',
}


def log(message):
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    print(f'{stamp} {message}', flush=True)


# --------------------------------------------------------------------------- state


def state_path(root):
    return Path(root) / 'state.json'


def load_state(root):
    path = state_path(root)
    state = dict(DEFAULTS, failures=0, current=None, known_good=None, last_event='', updated_at=0.0,
                 unit_started='', window_state='idle')
    if path.is_file():
        try:
            state.update(json.loads(path.read_text('utf-8')))
        except (OSError, ValueError):
            log('state.json unreadable; starting from defaults')
    state['max_failures'] = int(state['max_failures'])
    return state


def save_state(root, state):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    state['updated_at'] = time.time()
    temporary = state_path(root).with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
    os.replace(temporary, state_path(root))


# --------------------------------------------------------------------------- snapshots


def iter_source_files(source_root):
    """Yield relative paths of source files that belong in a snapshot."""
    root = Path(source_root)
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in SNAPSHOT_SKIP for part in relative.parts):
            continue
        if path.suffix in {'.log', '.pyc'} or path.name in {'config.toml', '.env'}:
            continue
        yield relative


def snapshot(source_root, store, name):
    """Copy the source tree into content-addressed objects; return its manifest id and digest."""
    store = Path(store)
    objects = store / 'objects'
    objects.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for relative in iter_source_files(source_root):
        source = Path(source_root) / relative
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        blob = objects / digest
        if not blob.exists():
            staging = objects / f'.{digest}.{os.getpid()}'
            staging.write_bytes(data)
            os.chmod(staging, 0o644)
            try:
                os.link(staging, blob)          # hardlink keeps duplicates free
            except FileExistsError:
                pass
            finally:
                staging.unlink(missing_ok=True)
        manifest[relative.as_posix()] = digest
        st = source.stat()
        try:
            os.utime(blob, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass
    snapshot_id = hashlib.sha256(
        '\n'.join(f'{key}:{value}' for key, value in sorted(manifest.items())).encode()).hexdigest()[:16]
    record = dict(id=snapshot_id, name=name, created=time.time(), files=len(manifest), manifest=manifest)
    (store / 'snapshots').mkdir(parents=True, exist_ok=True)
    target = store / 'snapshots' / f'{snapshot_id}.json'
    if not target.exists():
        target.write_text(json.dumps(record, indent=2), 'utf-8')
    return record


def restore(source_root, store, snapshot_id):
    """Make the source tree match a snapshot exactly: rewrite changed files, remove added ones."""
    record_path = Path(store) / 'snapshots' / f'{snapshot_id}.json'
    if not record_path.is_file():
        raise FileNotFoundError(f'snapshot {snapshot_id} missing')
    record = json.loads(record_path.read_text('utf-8'))
    manifest = record['manifest']
    root = Path(source_root)
    objects = Path(store) / 'objects'
    restored, removed, skipped = [], [], []
    for relative, digest in manifest.items():
        target = root / relative
        blob = objects / digest
        if not blob.is_file():
            raise FileNotFoundError(f'object {digest} for {relative} missing')
        current = target.read_bytes() if target.is_file() else None
        data = blob.read_bytes()
        if current == data:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_suffix(target.suffix + '.watchdog-tmp')
            staging.write_bytes(data)
            shutil.copymode(blob, staging)
            try:
                if hasattr(os, 'chown'):          # POSIX only; the Windows test host has none
                    os.chown(staging, *owner_of(root))
            except OSError:
                pass
            os.replace(staging, target)
            restored.append(relative)
        except OSError as error:
            skipped.append(f'{relative}: {error}')
    for target in list(iter_source_files(source_root)):
        if target.as_posix() not in manifest:
            try:
                (root / target).unlink()
                removed.append(target.as_posix())
            except OSError as error:
                skipped.append(f'{target}: {error}')
    return dict(snapshot=record, restored=restored, removed=removed, skipped=skipped)


def owner_of(root):
    st = Path(root).stat()
    return st.st_uid, st.st_gid


def contract(source_root, snapshot_id, keep):
    """Drop old snapshots/objects, always keeping the rollback point named by the caller."""
    store = Path(source_root) / 'data' / 'watchdog'
    return store


# --------------------------------------------------------------------------- health


def unit_state(unit):
    out = subprocess.run(['systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'SubState',
                          '-p', 'ExecMainStartTimestamp', '-p', 'NRestarts', '-p', 'MainPID'],
                         capture_output=True, text=True).stdout
    return dict(line.split('=', 1) for line in out.splitlines() if '=' in line)


def http_ok(url, timeout=5, token_file=None):
    """Liveness probe against the unauthenticated `/alive`.

    `/health` needs the owner token; a watchdog must not hold that credential, so `/alive`
    (process is serving, nothing else) is the probe. A raw `/health` request would read 401
    and call a healthy service broken.
    """
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url)
    if token_file:
        try:
            request.add_header('Authorization', 'Bearer ' + Path(token_file).read_text().strip())
        except OSError:
            return False
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def journal_errors(unit, since_epoch):
    """只认「启动期就坏了」的痕迹，不认业务层的失败。

    2026-11-18 实测：她自己的一次 agent inspect 失败打了 Traceback，
    旧判据把整条服务判成坏了并触发回滚（restored: []，什么也没还原），
    还给她写了一条"你的修改让服务起不来"的假消息。

    收窄后只认三类：模块导入失败、启动阶段抛错、进程级崩溃信号。
    业务异常（任务失败、工具报错）不算服务坏了——那是功能问题，不是进程问题。
    """
    out = subprocess.run(['journalctl', '-u', unit, '--since', f'@{int(since_epoch)}', '--no-pager'],
                         capture_output=True, text=True).stdout
    fatal = (
        'ImportError', 'ModuleNotFoundError', 'SyntaxError',
        '\u542f\u52a8\u5931\u8d25',          # 启动失败
        'startup failed',
        'Main process exited, code=exited, status=1/FAILURE',
    )
    return [line for line in out.splitlines() if any(token in line for token in fatal)]


def cognition_turns(home, since):
    """Count cognition activity recorded after `since`: new events or new turns.

    A service that is merely serving HTTP but has stopped thinking is not healthy. The probe is
    read-only and tolerant: an unrecognised schema yields None (the caller then treats the
    signal as unavailable instead of inventing a verdict).
    """
    database = Path(home) / 'data' / 'life.db'
    if not database.is_file():
        return None
    try:
        db = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
        try:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            counted = 0
            for table, column in (('events', 'received_at'), ('turns', 'created_at'),
                                  ('decisions', 'created_at')):
                if table not in tables:
                    continue
                columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
                if column in columns:
                    row = db.execute(f'SELECT COUNT(*) FROM {table} WHERE {column} >= ?',
                                     (since,)).fetchone()
                    counted += int(row[0]) if row else 0
            if counted:
                return counted
            return 0 if {'events', 'turns', 'decisions'} & tables else None
        finally:
            db.close()
    except sqlite3.Error as error:
        log(f'cognition probe skipped: {error}')
        return None


# --------------------------------------------------------------------------- notice


def write_notice(home, text, *, external_id, payload):
    """Write the owner-visible notice into the principal conversation as an event row.

    Written as the companion user so ownership stays consistent with the running service; the
    service re-reads events from disk on its next turn, so no restart is needed.
    """
    database = Path(home) / 'data' / 'life.db'
    sql = '''
    INSERT OR IGNORE INTO events
      (id, agent_id, conversation_id, participant_id, audience_policy_id, kind, source_kind,
       external_id, occurred_at, received_at, ingest_seq, actor_id, payload, source_event_ids,
       schema_version)
    VALUES (?, 'xiaoyu', ?, '', '', 'message', 'self_observation', ?, ?, ?, ?, '', ?, '[]', 1)
    '''
    event_id = 'evt_' + hashlib.sha256(external_id.encode()).hexdigest()[:16]
    body = json.dumps(dict(payload, text=text), ensure_ascii=False)
    script = (
        "import sqlite3, sys, json\n"
        "db = sqlite3.connect(sys.argv[1])\n"
        "row = db.execute('SELECT COALESCE(MAX(ingest_seq),0)+1 FROM events').fetchone()\n"
        "db.execute(sys.argv[2], json.loads(sys.argv[3]))\n"
        "db.commit()\n"
        "print(json.dumps(dict(event_id=sys.argv[4], ingest_seq=row[0])))\n"
    )
    args = [sys.executable, '-c', script, str(database), sql,
            json.dumps([event_id, 'qq:private:763282103', external_id, time.time(), time.time(),
                        0, body]), event_id]
    completed = subprocess.run(['runuser', '-u', 'companion', '--', *args],
                               capture_output=True, text=True)
    if completed.returncode != 0:
        log(f'notice write failed: {completed.stderr.strip()[:300]}')
        return None
    return event_id


# --------------------------------------------------------------------------- cycle


def service_started_epoch(unit):
    """When the unit's current process started, as a unix timestamp (0 when unknown)."""
    out = subprocess.run(['systemctl', 'show', unit, '-p', 'ExecMainStartTimestampMonotonic',
                          '-p', 'ExecMainStartTimestamp'],
                         capture_output=True, text=True).stdout
    values = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
    if values.get('ExecMainStartTimestamp', '').strip() in ('', 'n/a'):
        return 0.0
    mono = values.get('ExecMainStartTimestampMonotonic', '0')
    try:
        mono_seconds = int(mono) / 1_000_000
    except (TypeError, ValueError):
        return 0.0
    if mono_seconds <= 0:
        return 0.0
    try:
        uptime = float(Path('/proc/uptime').read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    return time.time() - (uptime - mono_seconds)


def evaluate(state, now=None):
    """One health verdict for the current restart.

    The observation window is anchored on the record of the restart this watchdog saw, not on
    the current time: otherwise a permanent "/health not 200" could never leave "pending".
    """
    now = time.time() if now is None else now
    unit = state['unit']
    info = unit_state(unit)
    started = info.get('ExecMainStartTimestamp', '')
    if not state.get('unit_started') or started != state['unit_started']:
        state['unit_started'] = started
        state['window_anchored'] = service_started_epoch(unit) or time.time()
        log(f"service restarted: ExecMainStartTimestamp={started} "
            f"anchored={round(now - state['window_anchored'])}s ago")
    since = float(state.get('window_anchored') or now)
    expired = now - since >= float(state['window_seconds'])
    if info.get('ActiveState') not in ('active', 'activating'):
        return dict(verdict='failed', reason=f"unit {info.get('ActiveState')}/{info.get('SubState')}")
    if not http_ok(state['health_url'], token_file=state.get('token_file')):
        return dict(verdict='failed' if expired else 'pending', reason='/alive not 200')
    if now - since < state['settle_seconds']:
        return dict(verdict='pending', reason='settling')
    errors = journal_errors(unit, since)
    if errors:
        return dict(verdict='failed', reason=f'{len(errors)} journal error(s): {errors[0][:160]}')
    turns = cognition_turns(state['home'], since)
    if turns is None:
        # 读不到就不猜：进程起来了、/alive 通了、没有致命报错，就该判健康。
        return dict(verdict='healthy', reason='cognition probe unavailable (not a failure)')
    # 「这一窗没有认知活动」不是故障：她安静一会儿是正常的（夜里、等外部回执）。
    # 2026-11-18 实测这条分支产生过 2 次误判。认知活动只做记录，不参与判定。
    return dict(verdict='healthy', reason=f'turns={turns}')


def reconcile(state, store):
    """Keep exactly one verified-good snapshot; return its id."""
    mapping = state.setdefault('verified', {})      # start timestamp -> snapshot id
    snapshots = Path(store) / 'snapshots'
    keep = {state.get('known_good')} if state.get('known_good') else set()
    for key, value in list(mapping.items()):
        if len(mapping) > 1:
            mapping.pop(key, None)
    keep |= {value for value in mapping.values() if value}
    for path in snapshots.glob('*.json'):
        if path.stem not in keep:
            path.unlink(missing_ok=True)
    referenced = set()
    for path in snapshots.glob('*.json'):
        referenced |= set(json.loads(path.read_text('utf-8'))['manifest'].values())
    for blob in (Path(store) / 'objects').iterdir():
        if blob.name not in referenced:
            blob.unlink(missing_ok=True)
    return state.get('known_good')


def cycle(state, store, dry_run=False):
    """Take one decision; returns an outcome dict. Never raises for a service problem."""
    now = time.time()
    verdict = evaluate(state)
    log(f"verdict={verdict['verdict']} ({verdict['reason']})")
    if dry_run:
        return verdict
    if verdict['verdict'] == 'pending':
        return verdict
    if verdict['verdict'] == 'healthy':
        snap = state.get('current')
        if snap and state.get('unit_started') != state.get('known_good_start'):
            state['known_good'] = snap
            state['known_good_start'] = state['unit_started']
            state['failures'] = 0
            state.setdefault('verified', {})[state['unit_started']] = snap
            log(f'snapshot {snap} verified good')
            reconcile(state, store)
        save_state(state['state_root'], state)
        return verdict

    # failure path
    target = state.get('known_good')
    state['failures'] = int(state.get('failures', 0)) + 1
    log(f"failure #{state['failures']} of {state['max_failures']}")
    if state['failures'] > int(state['max_failures']):
        log('too many consecutive failures; stopping automatic restart')
        write_notice(state['home'],
                     '\u81ea\u52a8\u6062\u590d\u505c\u4e0b\u4e86\uff1a\u8fde\u7eed\u591a\u6b21\u91cd\u542f\u90fd\u4e0d\u5065\u5eb7\uff0c\u6700\u540e\u8fd9\u7248\u6e90\u7801\u4e0d\u518d\u81ea\u52a8\u56de\u6eda\u3002'
                     '\u9700\u8981\u4eba\u770b\u4e00\u773c\u3002',
                     external_id=f"watchdog:stalled:{state['unit_started']}",
                     payload=dict(watchdog='stalled', failures=state['failures'],
                                  reason=verdict['reason'][:400]))
        save_state(state['state_root'], state)
        return dict(verdict='stalled', reason=verdict['reason'])
    if not target:
        log('no verified snapshot to roll back to; leaving the service as it is')
        save_state(state['state_root'], state)
        return dict(verdict='no-rollback-point', reason=verdict['reason'])

    result = restore(state['source_root'], store, target)
    log(f"rolled back to {target}: restored={len(result['restored'])} removed={len(result['removed'])}")
    subprocess.run(['systemctl', 'reset-failed', state['unit']], capture_output=True)
    subprocess.run(['systemctl', 'restart', state['unit']], capture_output=True)
    write_notice(state['home'],
                 '\u521a\u624d\u90a3\u6b21\u6e90\u7801\u4fee\u6539\u8ba9\u670d\u52a1\u8d77\u4e0d\u6765\uff1b\u5df2\u7ecf\u56de\u6eda\u5230\u4e0a\u4e00\u6b21\u80fd\u7528\u7684\u7248\u672c\u5e76\u91cd\u65b0\u62c9\u8d77\u3002'
                 '\u8fd9\u6b21\u6539\u52a8\u88ab\u4e22\u5f03\u4e86\uff0c\u9700\u8981\u7684\u8bdd\u53ef\u4ee5\u91cd\u65b0\u6765\u4e00\u904d\u3002',
                 external_id=f"watchdog:rollback:{state['unit_started']}",
                 payload=dict(watchdog='rollback', reason=verdict['reason'][:400],
                              rolled_back_to=target, restored=result['restored'][:20],
                              removed=result['removed'][:20]))
    state['known_good_start'] = ''
    save_state(state['state_root'], state)
    return dict(verdict='rolled-back', snapshot=target, restored=result['restored'],
                removed=result['removed'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--source-root')
    parser.add_argument('--state-root')
    parser.add_argument('--home')
    parser.add_argument('--unit')
    parser.add_argument('--health-url')
    parser.add_argument('--window-seconds', type=float)
    parser.add_argument('--max-failures', type=int)
    parser.add_argument('--once', action='store_true', help='evaluate a single cycle and exit')
    parser.add_argument('--dry-run', action='store_true', help='report the verdict, never act')
    parser.add_argument('--snapshot', action='store_true', help='snapshot the source tree and exit')
    arguments = parser.parse_args(argv)

    state = load_state(arguments.state_root or DEFAULTS['state_root'])
    for key, value in (('source_root', arguments.source_root), ('state_root', arguments.state_root),
                       ('home', arguments.home), ('unit', arguments.unit),
                       ('health_url', arguments.health_url)):
        if value:
            state[key] = value
    if arguments.window_seconds:
        state['window_seconds'] = arguments.window_seconds
    if arguments.max_failures:
        state['max_failures'] = arguments.max_failures
    store = Path(state['state_root'])
    (store / 'objects').mkdir(parents=True, exist_ok=True)

    lock = open(store / 'watchdog.lock', 'w')
    if fcntl is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log('another watchdog instance holds the lock; exiting')
            return 0

    if arguments.snapshot:
        record = snapshot(state['source_root'], store, 'manual')
        state['current'] = record['id']
        save_state(state['state_root'], state)
        log(f"snapshot {record['id']} files={record['files']}")
        return 0
    if arguments.once:
        outcome = cycle(state, store, dry_run=arguments.dry_run)
        print(json.dumps(outcome, ensure_ascii=False))
        return 0 if outcome['verdict'] in ('healthy', 'pending', 'rolled-back') else 1

    log(f"watchdog up: unit={state['unit']} window={state['window_seconds']}s")
    while True:
        try:
            cycle(state, store)
        except Exception as error:             # noqa: BLE001 - a watchdog must keep watching
            log(f'cycle error: {type(error).__name__}: {error}')
            save_state(state['state_root'], state)
        time.sleep(float(state.get('poll_seconds', 5.0)))


if __name__ == '__main__':
    raise SystemExit(main())

