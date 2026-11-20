"""companion-watchdog: detect a broken companion.service, roll the source back, and tell her.

Why a separate process: the thing it has to rescue is `companion.service`, so it cannot live
inside it. This unit watches a restart, decides healthy/unhealthy inside a fixed window, and on
failure restores the last snapshot that was *proven* good, restarts, and records what happened.

Contract (rewritten 2026-11-20 after the 11-18 incident):

* healthy = unit `active` + `/alive` 200 + at least one cognition event since this process start.
  A missing cognition probe, or a quiet role, is `pending`, never `failed`.
* Startup grace: for `startup_grace_seconds` after systemd's real `ExecMainStartTimestamp`, a bad
  `/alive` is `pending`, never `failed`.  A *crashed* unit (ActiveState=failed, or SubState
  auto-restart) is still a failure observation: that is a dead process, not a slow start.
* A single bad probe never escalates.  Failure observations are counted at most once per
  `window_seconds`; `max_failures` such observations (each at least a window after the previous
  one) are required before the service is called failed.  Any healthy observation resets the
  count.
* The window is anchored on systemd's own `ExecMainStartTimestamp` rendered with
  `--timestamp=unix`.  Nothing here derives a start time from `/proc/uptime`.
* A journal Traceback is reported as `degraded`: not healthy (so it is not verified as good),
  but never a rollback trigger.  Rolling source back cannot fix a runtime traceback, and the
  11-18 damage was caused by acting on a single weak signal.
* A rollback requires all of: a verified snapshot, older than the current start, different from
  the current tree, never rolled back before, and a free restart slot.  The current tree is
  snapshotted first (`pre-rollback-*`), so every rollback can be undone.
* Restarts (rollback or requested) are rate limited: at most `max_restarts_per_window` inside
  `restart_window_seconds`, and never while an action is `running`.  Past that the watchdog
  stalls, says so once, and stops acting until the service is healthy again.
* She can ask for a restart by writing `<state_root>/restart.request`; valid requests restart
  the unit, invalid/expired ones are rejected, and the file is always finalized to
  `.done`/`.rejected` with the outcome and time.
* Every notice is written straight into life.db as an event in the principal conversation, so
  the next cognition turn sees it without any extra wiring.

Test-only switches exist so the whole path can be rehearsed in isolation: `--once` (one cycle),
`--dry-run` (report the decision, touch nothing), `--rehearse` (act, but only off production
paths) and `--source-root`/`--state-root`/`--home`/`--unit`.
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
    # Timing contract: settle <= startup_grace, and one failure window is never shorter than
    # settle.  See normalize_timing().
    window_seconds=75.0,
    settle_seconds=10.0,
    startup_grace_seconds=120.0,
    max_failures=3,
    keep_snapshots=3,
    poll_seconds=5.0,
    snapshot_interval_seconds=120.0,
    # Rolling back / restarting is expensive and destructive; cap it hard.
    max_restarts_per_window=2,
    restart_window_seconds=1800.0,
    # Restart-request channel (she writes the request file as the companion user).
    restart_request_file='restart.request',
    restart_request_max_age_seconds=300.0,
    restart_request_min_interval_seconds=600.0,
    restart_request_future_skew_seconds=60.0,
    restart_requestors=['xiaoyu'],
    # 0 = never fail on a quiet role (a serving-but-idle service is not broken).
    turn_failure_seconds=0.0,
)

# Directories that are never part of a source snapshot: they are either huge, third-party, or
# runtime state. A rollback must not resurrect a stale database or a whole node toolchain.
SNAPSHOT_SKIP = {
    'data', 'toolchain', 'vendor', 'napbx', 'napcat', 'napcat-onekey', '.venv', '.venv-companion',
    '__pycache__', 'local-backups', '.git', 'node_modules', '.acceptance', '.srv-life',
    'xiaoyu-private-migration-20261027-094202', 'work', 'docs/acceptance',
}

STATE_DEFAULTS = dict(
    failures=0, current=None, current_for_start=None, known_good=None, known_good_start='',
    verified={}, last_event='', updated_at=0.0, unit_started='', unit_started_epoch=0.0,
    observed_since=0.0, window_state='idle', failure_streak=[], restarts=[], rolled_back={},
    last_restart_accepted=0.0, automation_stopped=False, stall_reason='', stall_logged_at=0.0,
    last_rollback=None, last_snapshot_at=0.0, last_verdict='',
)


def log(message):
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    print(f'{stamp} {message}', flush=True)


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# --------------------------------------------------------------------------- state


def state_path(root):
    return Path(root) / 'state.json'


def load_state(root):
    path = state_path(root)
    state = dict(DEFAULTS, **STATE_DEFAULTS)
    if path.is_file():
        try:
            state.update(json.loads(path.read_text('utf-8')))
        except (OSError, ValueError):
            log('state.json unreadable; starting from defaults')
    state['max_failures'] = int(_number(state.get('max_failures'), 3))
    state['failure_streak'] = [_number(item) for item in (state.get('failure_streak') or [])]
    state['restarts'] = [_number(item) for item in (state.get('restarts') or [])]
    state['rolled_back'] = dict(state.get('rolled_back') or {})
    state['verified'] = dict(state.get('verified') or {})
    allowed = state.get('restart_requestors') or ['xiaoyu']
    if isinstance(allowed, str):
        allowed = [piece.strip() for piece in allowed.split(',') if piece.strip()]
    state['restart_requestors'] = [str(item) for item in allowed]
    return state


def save_state(root, state):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    state['updated_at'] = time.time()
    temporary = state_path(root).with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
    os.replace(temporary, state_path(root))


def normalize_timing(state):
    """Put settle / startup grace / failure window into one self-consistent order.

    The old defaults disagreed: `settle_seconds=10` was checked *after* `expired` (window-based)
    had already decided a bad probe was a failure, so the settle branch was unreachable for
    exactly the case it was written for.  One ordered contract instead:

    * ``settle_seconds``  - after a start, do not trust a *good* probe yet (never fails a service);
    * ``startup_grace_seconds`` - after a start, a bad probe is ``pending``, never ``failed``;
    * ``window_seconds``  - minimum spacing between two counted consecutive failures.

    Enforced ordering: ``settle <= startup_grace`` and ``settle <= window``.
    """
    settle = max(0.0, _number(state.get('settle_seconds'), 10.0))
    grace = max(settle, _number(state.get('startup_grace_seconds'), 120.0))
    window = max(settle, _number(state.get('window_seconds'), 75.0))
    state['settle_seconds'] = settle
    state['startup_grace_seconds'] = grace
    state['window_seconds'] = window
    return state


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


def manifest_id(manifest):
    return hashlib.sha256(
        '\n'.join(f'{key}:{value}' for key, value in sorted(manifest.items())).encode()).hexdigest()[:16]


def hash_tree(source_root):
    """Content digest per relative path, without writing anything (used by --dry-run)."""
    hashes = {}
    for relative in iter_source_files(source_root):
        try:
            hashes[relative.as_posix()] = hashlib.sha256(
                (Path(source_root) / relative).read_bytes()).hexdigest()
        except OSError as error:
            log(f'snapshot skipped {relative}: {error}')
    return hashes


def snapshot(source_root, store, name):
    """Copy the source tree into content-addressed objects; return its manifest id and digest."""
    store = Path(store)
    objects = store / 'objects'
    objects.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for relative in iter_source_files(source_root):
        source = Path(source_root) / relative
        try:
            data = source.read_bytes()
        except OSError as error:
            log(f'snapshot skipped {relative}: {error}')
            continue
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
    snapshot_id = manifest_id(manifest)
    record = dict(id=snapshot_id, name=name, created=time.time(), files=len(manifest),
                  manifest=manifest)
    (store / 'snapshots').mkdir(parents=True, exist_ok=True)
    target = store / 'snapshots' / f'{snapshot_id}.json'
    if not target.exists():
        target.write_text(json.dumps(record, indent=2), 'utf-8')
    return record


def load_snapshot(store, snapshot_id):
    if not snapshot_id:
        return None
    path = Path(store) / 'snapshots' / f'{snapshot_id}.json'
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return None


def restore(source_root, store, snapshot_id):
    """Make the source tree match a snapshot exactly: rewrite changed files, remove added ones."""
    record = load_snapshot(store, snapshot_id)
    if record is None:
        raise FileNotFoundError(f'snapshot {snapshot_id} missing')
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


def reconcile(state, store):
    """Prune snapshots/objects, always keeping the rollback point, current tree and backup."""
    store = Path(store)
    snapshots = store / 'snapshots'
    snapshots.mkdir(parents=True, exist_ok=True)
    keep = set()
    for key in ('known_good', 'current'):
        value = state.get(key)
        if value:
            keep.add(value)
    backup = (state.get('last_rollback') or {}).get('backup')
    if backup:
        keep.add(backup)
    records = []
    for path in snapshots.glob('*.json'):
        try:
            record = json.loads(path.read_text('utf-8'))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        records.append((_number(record.get('created'), 0.0), path.stem, record))
    records.sort(reverse=True)
    for _, snapshot_key, _record in records[:max(1, int(_number(state.get('keep_snapshots'), 3)))]:
        keep.add(snapshot_key)
    for _, snapshot_key, _record in records:
        if snapshot_key not in keep:
            (snapshots / f'{snapshot_key}.json').unlink(missing_ok=True)
    referenced = set()
    for _, snapshot_key, record in records:
        if snapshot_key in keep:
            referenced |= set((record.get('manifest') or {}).values())
    objects = store / 'objects'
    if objects.is_dir():
        for blob in objects.iterdir():
            if blob.name not in referenced:
                blob.unlink(missing_ok=True)
    for key in list(state.get('verified') or {}):
        if state['verified'][key] not in keep:
            state['verified'].pop(key, None)
    return state.get('known_good')


def capture_current(state, store, now=None, force=False):
    """Snapshot the current source tree, at most once per interval and once per unit start."""
    now = time.time() if now is None else now
    started = state.get('unit_started') or ''
    last = _number(state.get('last_snapshot_at'), 0.0)
    interval = _number(state.get('snapshot_interval_seconds'), 120.0)
    if (not force and state.get('current') and state.get('current_for_start') == started
            and now - last < interval):
        return state['current']
    record = snapshot(state['source_root'], store, f'runtime-{int(now)}')
    state['current'] = record['id']
    state['current_for_start'] = started
    state['last_snapshot_at'] = now
    log(f"source snapshot {record['id']} files={record['files']}")
    return record['id']


# --------------------------------------------------------------------------- health probes


def unit_state(unit):
    try:
        out = subprocess.run(
            ['systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'SubState',
             '-p', 'ExecMainStartTimestamp', '-p', 'NRestarts', '-p', 'MainPID',
             '-p', 'LoadState', '--timestamp=unix'],
            capture_output=True, text=True).stdout
    except OSError as error:
        log(f'unit_state probe failed: {error}')
        return {}
    values = {}
    for line in out.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    raw = values.get('ExecMainStartTimestamp', '')
    if raw.startswith('@'):
        try:
            values['ExecMainStartTimestampEpoch'] = float(int(raw[1:]))
        except ValueError:
            pass
    return values


def service_started_epoch(unit):
    """Epoch seconds when the unit's current main process started (0.0 when unknown).

    systemd renders it with ``--timestamp=unix``; parsing that avoids both the local-time string
    and the old ``time.time() - (/proc/uptime - monotonic)`` estimate, which was only as good as
    the two clocks agreeing and silently made "is the window over?" wrong.
    """
    try:
        out = subprocess.run(['systemctl', 'show', unit, '-p', 'ExecMainStartTimestamp',
                              '--timestamp=unix'], capture_output=True, text=True).stdout
    except OSError:
        return 0.0
    for line in out.splitlines():
        if line.startswith('ExecMainStartTimestamp='):
            value = line.split('=', 1)[1].strip()
            if value.startswith('@'):
                try:
                    return float(int(value[1:]))
                except ValueError:
                    return 0.0
    return 0.0


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
    try:
        out = subprocess.run(['journalctl', '-u', unit, '--since', f'@{int(since_epoch)}',
                              '--no-pager'], capture_output=True, text=True).stdout
    except OSError as error:
        log(f'journal probe failed: {error}')
        return []
    return [line for line in out.splitlines()
            if 'Traceback' in line or 'CRITICAL' in line or '\u542f\u52a8\u5931\u8d25' in line]


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


def running_actions(home):
    """How many companion actions are in flight right now (0 when the probe is unavailable)."""
    database = Path(home) / 'data' / 'life.db'
    if not database.is_file():
        return 0
    try:
        db = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
        try:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if 'companion_actions' not in tables:
                return 0
            columns = {row[1] for row in db.execute('PRAGMA table_info(companion_actions)')}
            if 'status' not in columns:
                return 0
            row = db.execute("SELECT COUNT(*) FROM companion_actions WHERE status='running'").fetchone()
            return int(row[0]) if row else 0
        finally:
            db.close()
    except sqlite3.Error as error:
        log(f'running-action probe skipped: {error}')
        return 0


# --------------------------------------------------------------------------- notice


def write_notice(home, text, *, external_id, payload):
    """Write the owner-visible notice into the principal conversation as an event row.

    Written as the companion user so ownership stays consistent with the running service; the
    service re-reads events from disk on its next turn, so no restart is needed.
    """
    database = Path(home) / 'data' / 'life.db'
    if not database.is_file():
        log(f'notice not written: {database} missing')
        return None
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
    try:
        completed = subprocess.run(['runuser', '-u', 'companion', '--', *args],
                                   capture_output=True, text=True)
    except OSError as error:
        log(f'notice write failed: {error}')
        return None
    if completed.returncode != 0:
        log(f'notice write failed: {completed.stderr.strip()[:300]}')
        return None
    return event_id


# --------------------------------------------------------------------------- verdict


def clear_failures(state):
    if state.get('failure_streak'):
        log('failure streak cleared')
    state['failure_streak'] = []
    state['failures'] = 0


def count_failure(state, now, reason):
    """Count one failure observation, but at most once per window; escalate at max_failures."""
    normalize_timing(state)
    window = float(state['window_seconds'])
    limit = int(state['max_failures'])
    streak = [_number(item) for item in (state.get('failure_streak') or [])]
    last = streak[-1] if streak else None
    if last is None or now - last >= window:
        streak.append(now)
        log(f'failure observation #{len(streak)}/{limit}: {reason}')
    else:
        log(f'failure observed again ({reason}) only {round(now - last)}s after the previous one; '
            f'not counted (window={round(window)}s)')
    state['failure_streak'] = streak
    state['failures'] = len(streak)
    if len(streak) >= limit:
        state['window_state'] = 'failed'
        return dict(verdict='failed', reason=reason, failures=len(streak),
                    first_failure=streak[0] if streak else None)
    state['window_state'] = 'pending'
    return dict(verdict='pending',
                reason=f'{reason}; consecutive failures {len(streak)}/{limit}'
                       f' (next counts after {round(window)}s)',
                failures=len(streak))


def evaluate(state, now=None):
    """One health verdict for the current service start.

    Anchored on systemd's real `ExecMainStartTimestamp` for this start, and on the moment this
    watchdog first saw it for journal/observation scoping.  `failed` is only returned after
    `max_failures` counted observations, each at least a window apart, and never inside the
    startup grace period unless the process itself is dead.
    """
    normalize_timing(state)
    now = time.time() if now is None else now
    unit = state['unit']
    info = unit_state(unit) or {}
    raw = str(info.get('ExecMainStartTimestamp') or '').strip()
    started_epoch = _number(info.get('ExecMainStartTimestampEpoch'), 0.0)
    if started_epoch <= 0:
        started_epoch = _number(service_started_epoch(unit), 0.0)
    if raw != (state.get('unit_started') or '') or not state.get('observed_since'):
        state['unit_started'] = raw
        state['unit_started_epoch'] = started_epoch or now
        state['observed_since'] = now
        state['window_state'] = 'starting'
        state['current_for_start'] = None
        log(f"service start observed: {raw or 'unknown'} (epoch={round(started_epoch or now)})")
    since_start = _number(state.get('unit_started_epoch'), now) or now
    observed_since = _number(state.get('observed_since'), now) or now
    age = max(0.0, now - since_start)
    grace = float(state['startup_grace_seconds'])
    settle = float(state['settle_seconds'])
    in_grace = age < grace
    active = str(info.get('ActiveState') or '').strip().lower()
    substate = str(info.get('SubState') or '').strip().lower()

    # A dead process is a failure observation even inside the grace period: that is not a slow
    # start, systemd already knows the start failed.
    if active == 'failed' or substate in ('auto-restart', 'failed'):
        return count_failure(state, now, f'unit {active or "?"}/{substate or "?"}')

    if active not in ('active', 'activating'):
        if in_grace:
            return dict(verdict='pending',
                        reason=f'unit {active or "?"}/{substate or "?"} while starting')
        return count_failure(state, now, f'unit {active or "?"}/{substate or "?"}')

    alive = http_ok(state['health_url'], token_file=state.get('token_file'))
    if active == 'activating':
        if in_grace:
            return dict(verdict='pending', reason=f'activating/{substate or "start"}')
        return count_failure(state, now, f'still activating after {round(age)}s')
    if not alive:
        if in_grace:
            return dict(verdict='pending',
                        reason=f'/alive not 200 within the {round(grace)}s startup grace')
        if age < settle:
            return dict(verdict='pending', reason='settling (probe not trusted yet)')
        return count_failure(state, now, '/alive not 200')
    if age < settle:
        return dict(verdict='pending', reason=f'settling ({round(age)}s < {round(settle)}s)')

    errors = journal_errors(unit, max(observed_since, now - float(state['window_seconds'])))
    if errors:
        return dict(verdict='degraded',
                    reason=f'{len(errors)} journal error(s) since we started watching: '
                           f'{errors[0][:160]}')
    turns = cognition_turns(state['home'], since_start)
    if turns is None:
        return dict(verdict='pending', reason='cognition probe unavailable')
    if turns < 1:
        quiet_limit = _number(state.get('turn_failure_seconds'), 0.0)
        if quiet_limit and age > quiet_limit:
            return count_failure(state, now, f'no cognition turn in {round(age)}s')
        return dict(verdict='pending', reason=f'no cognition turn since start ({round(age)}s)')

    if state.get('automation_stopped'):
        log('service healthy again; automation resumed')
        state['automation_stopped'] = False
        state['stall_reason'] = ''
    clear_failures(state)
    state['window_state'] = 'healthy'
    return dict(verdict='healthy', reason=f'turns={turns}')


# --------------------------------------------------------------------------- restart budget


def restart_budget(state, now):
    """(used, limit) restarts inside the rolling window; prunes the history in place."""
    window = _number(state.get('restart_window_seconds'), 1800.0)
    limit = int(_number(state.get('max_restarts_per_window'), 2))
    recent = [t for t in (_number(item) for item in (state.get('restarts') or []))
              if now - t <= window]
    state['restarts'] = recent
    return len(recent), limit


def restart_service(unit):
    return subprocess.run(['systemctl', 'restart', unit], capture_output=True, text=True)


def stall(state, store, reason, verdict, now):
    """Stop automatic action, say why once, and stay quiet until the service is healthy again."""
    log(f'automation stopped: {reason}')
    state['automation_stopped'] = True
    state['stall_reason'] = reason
    state['window_state'] = 'stalled'
    key = str(state.get('unit_started') or int(now))
    digest = hashlib.sha256(reason.encode()).hexdigest()[:8]
    write_notice(state['home'],
                 f'自动恢复停下了：{reason}。接下来不再自动回滚或重启，需要人看一眼。',
                 external_id=f'watchdog:stalled:{key}:{digest}',
                 payload=dict(watchdog='stalled', reason=reason[:400],
                              failures=state.get('failures'),
                              verdict=str((verdict or {}).get('reason', ''))[:200]))
    save_state(state['state_root'], state)
    return dict(verdict='stalled', reason=reason,
                underlying=str((verdict or {}).get('reason', '')))


# --------------------------------------------------------------------------- restart requests


def restart_request_path(store, state=None):
    name = str((state or {}).get('restart_request_file') or 'restart.request')
    return Path(store) / name


def read_restart_request(path):
    try:
        raw = Path(path).read_text('utf-8', errors='replace')
    except OSError as error:
        return None, f'unreadable ({error})'
    try:
        payload = json.loads(raw)
    except ValueError as error:
        return None, f'invalid JSON ({error})'
    if not isinstance(payload, dict):
        return None, 'JSON is not an object'
    return payload, ''


def validate_restart_request(payload, state, now):
    """Return (ok, detail); detail is the human-readable reason when not ok."""
    if not isinstance(payload, dict):
        return False, 'request is not a JSON object'
    requested_by = str(payload.get('requested_by') or '')
    allowed = [str(item) for item in (state.get('restart_requestors') or ['xiaoyu'])]
    if requested_by not in allowed:
        return False, f'requested_by {requested_by!r} is not one of {allowed}'
    try:
        requested_at = float(payload.get('requested_at'))
    except (TypeError, ValueError):
        return False, 'requested_at missing or not a number'
    age = now - requested_at
    max_age = _number(state.get('restart_request_max_age_seconds'), 300.0)
    skew = _number(state.get('restart_request_future_skew_seconds'), 60.0)
    if age < -skew:
        return False, f'requested_at is {round(-age)}s in the future'
    if age > max_age:
        return False, f'request expired: {round(age)}s old (limit {round(max_age)}s)'
    reason = payload.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        return False, 'reason missing or empty'
    last = _number(state.get('last_restart_accepted'), 0.0)
    interval = _number(state.get('restart_request_min_interval_seconds'), 600.0)
    if last and now - last < interval:
        return False, (f'rate limited: last accepted restart was {round(now - last)}s ago '
                       f'(minimum {round(interval)}s)')
    return True, ''


def _notify_request(state, payload, status, detail, now):
    reason = str((payload or {}).get('reason') or '')[:200]
    requested_by = str((payload or {}).get('requested_by') or '?')
    if status == 'accepted':
        text = (f'她自己请求重启，守门已经重启了 companion.service（原因：{reason}）。'
                '接下来几分钟看她起不起得来。')
    elif status == 'failed':
        text = (f'她自己请求重启，守门同意了但 systemctl restart 没成功（{detail or "非零退出"}），'
                '这次没起来。')
    else:
        text = f'她请求重启，但守门这关没过：{detail or "未通过校验"}。这次没有重启。'
    requested_at = _number((payload or {}).get('requested_at'), now)
    write_notice(state['home'], text,
                 external_id=f'watchdog:restart-request:{int(requested_at)}:{status}',
                 payload=dict(watchdog='restart-request', status=status, reason=reason,
                              requested_by=requested_by, detail=(detail or '')[:300]))


def finalize_request(state, store, path, payload, status, detail, now):
    """Replace the live request with `.done`/`.rejected` holding the outcome and when."""
    name = str(state.get('restart_request_file') or 'restart.request')
    suffix = 'done' if status == 'accepted' else 'rejected'
    final = dict(status=status, handled_at=now,
                 handled_at_iso=time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(now)),
                 detail=detail, original=payload)
    target = Path(store) / f'{name}.{suffix}'
    temporary = Path(store) / f'.{name}.{suffix}.tmp'
    try:
        temporary.write_text(json.dumps(final, ensure_ascii=False, indent=2), 'utf-8')
        os.replace(temporary, target)
    except OSError as error:
        log(f'could not write {target}: {error}')
    try:
        Path(path).unlink()
    except OSError as error:
        log(f'could not remove {path}: {error}')
    try:
        with (Path(store) / f'{name}.history.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(final, ensure_ascii=False) + '\n')
    except OSError:
        pass
    return final


def handle_restart_request(state, store, now=None, dry_run=False, restart_unit=None):
    """Consume `<state_root>/restart.request` when it exists; return what happened (or None)."""
    now = time.time() if now is None else now
    path = restart_request_path(store, state)
    if not path.is_file():
        return None
    payload, error = read_restart_request(path)
    if error:
        return _reject_request(state, store, path, payload or {}, f'unreadable request: {error}',
                               now, dry_run)
    ok, why = validate_restart_request(payload, state, now)
    if not ok:
        return _reject_request(state, store, path, payload, why, now, dry_run)
    if state.get('automation_stopped'):
        return _reject_request(state, store, path, payload,
                               f'automation stopped ({state.get("stall_reason") or "unknown"})',
                               now, dry_run)
    used, limit = restart_budget(state, now)
    if used >= limit:
        return _reject_request(
            state, store, path, payload,
            f'restart budget exhausted ({used}/{limit} restarts in '
            f'{round(_number(state.get("restart_window_seconds"), 1800.0))}s)',
            now, dry_run)
    running = running_actions(state['home'])
    if running:
        return _reject_request(state, store, path, payload,
                               f'{running} action(s) still running; refusing to interrupt',
                               now, dry_run)
    if dry_run:
        log(f"restart request valid (dry run): {payload.get('reason')}")
        return dict(status='would-restart', reason=str(payload.get('reason') or ''),
                    request=payload)
    result = (restart_unit or restart_service)(state['unit'])
    accepted = getattr(result, 'returncode', 1) == 0
    detail = str(getattr(result, 'stderr', '') or '').strip()[:300]
    if accepted:
        state['last_restart_accepted'] = now
        state['restarts'] = list(state.get('restarts') or []) + [now]
        state['failure_streak'] = []
        state['failures'] = 0
        state['unit_started'] = ''
        state['observed_since'] = 0.0
        state['current_for_start'] = None
        state['window_state'] = 'restart-requested'
        log(f"restart request accepted from {payload.get('requested_by')}: "
            f"{str(payload.get('reason'))[:200]}")
    else:
        log(f"restart request failed: systemctl restart {state['unit']} "
            f"-> {detail or 'non-zero exit'}")
    _notify_request(state, payload, 'accepted' if accepted else 'failed', detail, now)
    final = finalize_request(state, store, path, payload,
                             'accepted' if accepted else 'rejected', detail, now)
    save_state(state['state_root'], state)
    return dict(status='accepted' if accepted else 'failed',
                reason=str(payload.get('reason') or ''), detail=detail, final=final)


def _reject_request(state, store, path, payload, reason, now, dry_run):
    log(f'restart request rejected: {reason}')
    if dry_run:
        return dict(status='would-reject', reason=reason, request=payload)
    _notify_request(state, payload, 'rejected', reason, now)
    final = finalize_request(state, store, path, payload, 'rejected', reason, now)
    save_state(state['state_root'], state)
    return dict(status='rejected', reason=reason, final=final)


# --------------------------------------------------------------------------- cycle


def describe_rollback(state, store, now=None):
    """What a confirmed failure would do, without touching the tree (used by --dry-run)."""
    now = time.time() if now is None else now
    target = state.get('known_good')
    plan = dict(action='none', target=target, blockers=[])
    if not target:
        plan['blockers'].append('no verified snapshot')
        return plan
    record = load_snapshot(store, target)
    if record is None:
        plan['blockers'].append(f'snapshot {target} missing from the store')
        return plan
    current = hash_tree(state['source_root'])
    manifest = record.get('manifest') or {}
    restore_list = sorted(relative for relative, digest in manifest.items()
                          if current.get(relative) != digest)
    remove_list = sorted(relative for relative in current if relative not in manifest)
    current_id = manifest_id(current)
    start_epoch = _number(state.get('unit_started_epoch'), 0.0)
    plan.update(action='rollback', current_id=current_id, differs=bool(restore_list or remove_list),
                would_restore=len(restore_list), would_remove=len(remove_list),
                restore_sample=restore_list[:10], remove_sample=remove_list[:10],
                fresh=_number(record.get('created'), 0.0) < start_epoch,
                already_rolled_back=current_id in (state.get('rolled_back') or {}))
    if not plan['differs']:
        plan['blockers'].append('current tree already equals the verified snapshot')
    if not plan['fresh']:
        plan['blockers'].append('verified snapshot is not older than the current start')
    if plan['already_rolled_back']:
        plan['blockers'].append('this exact source tree was already rolled back once')
    used, limit = restart_budget(state, now)
    if used >= limit:
        plan['blockers'].append(f'restart budget exhausted ({used}/{limit})')
    if plan['blockers']:
        plan['action'] = 'none'
    return plan


def verify_known_good(state, store, now=None):
    """Remember the current tree as the rollback point for this start."""
    now = time.time() if now is None else now
    capture_current(state, store, now=now)
    snap = state.get('current')
    if not snap:
        return None
    if state.get('known_good') == snap and state.get('known_good_start') == state.get('unit_started'):
        return snap
    state['known_good'] = snap
    state['known_good_start'] = state.get('unit_started')
    state.setdefault('verified', {})[str(state.get('unit_started') or '?')] = snap
    log(f'snapshot {snap} verified good')
    reconcile(state, store)
    return snap


def rollback(state, store, now=None, verdict=None):
    """The destructive path, with every guard in front of it."""
    now = time.time() if now is None else now
    verdict = verdict or dict(verdict='failed', reason='')
    if state.get('automation_stopped'):
        last_log = _number(state.get('stall_logged_at'), 0.0)
        if now - last_log >= 300:
            log(f"automation stopped ({state.get('stall_reason')}); not rolling back")
            state['stall_logged_at'] = now
        return dict(verdict='stalled', reason=str(verdict.get('reason', '')),
                    underlying=state.get('stall_reason'))
    running = running_actions(state['home'])
    if running:
        log(f'deferring rollback: {running} action(s) still running')
        save_state(state['state_root'], state)
        return dict(verdict='deferred', reason=f'{running} running action(s)',
                    underlying=str(verdict.get('reason', '')))
    used, limit = restart_budget(state, now)
    if used >= limit:
        return stall(state, store,
                     f'restart budget exhausted ({used}/{limit} inside '
                     f'{round(_number(state.get("restart_window_seconds"), 1800.0))}s)',
                     verdict, now)
    target = state.get('known_good')
    if not target:
        return stall(state, store, 'no verified snapshot to roll back to', verdict, now)

    # 1. back up the current tree first: every rollback must be undoable
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
    backup = snapshot(state['source_root'], store, f'pre-rollback-{stamp}')
    log(f"pre-rollback backup {backup['id']} files={backup['files']}")

    # 2. the same source tree is rolled back at most once
    already = state.setdefault('rolled_back', {})
    if backup['id'] in already:
        return stall(state, store,
                     f'source tree {backup["id"]} was already rolled back once; not doing it again',
                     verdict, now)

    # 3. the rollback point must actually differ from the current tree
    if backup['id'] == target:
        return stall(state, store,
                     'current source is identical to the verified snapshot; a rollback would change '
                     'nothing, so the failure is not a source problem', verdict, now)

    # 4. and it must predate this start (a point verified by this very run is not a rollback point)
    record = load_snapshot(store, target)
    start_epoch = _number(state.get('unit_started_epoch'), 0.0)
    if record is None or not (_number(record.get('created'), 0.0) < start_epoch):
        return stall(state, store,
                     f'rollback point {target} is missing or not older than the current start',
                     verdict, now)

    result = restore(state['source_root'], store, target)
    log(f"rolled back to {target}: restored={len(result['restored'])} "
        f"removed={len(result['removed'])}")
    already[backup['id']] = dict(at=now, rolled_back_to=target)
    state['last_rollback'] = dict(at=now, backup=backup['id'], rolled_back_to=target,
                                  restored=len(result['restored']),
                                  removed=len(result['removed']))
    state['restarts'] = list(state.get('restarts') or []) + [now]
    state['failure_streak'] = []
    state['failures'] = 0
    state['unit_started'] = ''
    state['observed_since'] = 0.0
    state['current_for_start'] = None
    state['known_good_start'] = ''
    state['window_state'] = 'rolled-back'
    subprocess.run(['systemctl', 'reset-failed', state['unit']], capture_output=True)
    subprocess.run(['systemctl', 'restart', state['unit']], capture_output=True)
    write_notice(state['home'],
                 '刚才那版源码让服务起不来；已经回滚到上一次能用的版本并重新拉起。'
                 f'（回滚前那一版已备份为 {backup["id"]}，需要的话可以恢复。）',
                 external_id=f"watchdog:rollback:{state.get('unit_started') or int(now)}",
                 payload=dict(watchdog='rollback', reason=str(verdict.get('reason', ''))[:400],
                              rolled_back_to=target, backup=backup['id'],
                              restored=result['restored'][:20], removed=result['removed'][:20]))
    save_state(state['state_root'], state)
    return dict(verdict='rolled-back', snapshot=target, backup=backup['id'],
                restored=result['restored'], removed=result['removed'],
                skipped=result['skipped'])


def cycle(state, store, dry_run=False, now=None):
    """Take one decision; returns an outcome dict. Never raises for a service problem."""
    normalize_timing(state)
    now = time.time() if now is None else now
    request = handle_restart_request(state, store, now=now, dry_run=dry_run)
    if request and request.get('status') in ('accepted', 'would-restart'):
        if not dry_run:
            state['last_verdict'] = 'restart-requested'
            save_state(state['state_root'], state)
        return dict(verdict='restart-requested', status=request['status'],
                    reason=str(request.get('reason') or ''))

    verdict = evaluate(state, now=now)
    log(f"verdict={verdict['verdict']} ({verdict['reason']})")
    state['last_verdict'] = verdict['verdict']
    if dry_run:
        if verdict['verdict'] == 'failed':
            verdict['plan'] = describe_rollback(state, store, now=now)
        return verdict
    if verdict['verdict'] in ('pending', 'degraded'):
        save_state(state['state_root'], state)
        return verdict
    if verdict['verdict'] == 'healthy':
        verify_known_good(state, store, now=now)
        save_state(state['state_root'], state)
        return verdict
    outcome = rollback(state, store, now=now, verdict=verdict)
    save_state(state['state_root'], state)
    return outcome


# --------------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--source-root')
    parser.add_argument('--state-root')
    parser.add_argument('--home')
    parser.add_argument('--unit')
    parser.add_argument('--health-url')
    parser.add_argument('--window-seconds', type=float)
    parser.add_argument('--settle-seconds', type=float)
    parser.add_argument('--startup-grace-seconds', type=float)
    parser.add_argument('--max-failures', type=int)
    parser.add_argument('--max-restarts-per-window', type=int)
    parser.add_argument('--restart-window-seconds', type=float)
    parser.add_argument('--once', action='store_true', help='evaluate a single cycle and exit')
    parser.add_argument('--dry-run', action='store_true', help='report the verdict, never act')
    parser.add_argument('--snapshot', action='store_true', help='snapshot the source tree and exit')
    parser.add_argument('--rehearse', action='store_true',
                        help='allow the destructive path, but only off the production paths')
    parser.add_argument('--clear-stall', action='store_true',
                        help='clear a persisted automation stop and exit')
    arguments = parser.parse_args(argv)

    state = load_state(arguments.state_root or DEFAULTS['state_root'])
    for key, value in (('source_root', arguments.source_root), ('state_root', arguments.state_root),
                       ('home', arguments.home), ('unit', arguments.unit),
                       ('health_url', arguments.health_url)):
        if value:
            state[key] = value
    for key, value in (('window_seconds', arguments.window_seconds),
                       ('settle_seconds', arguments.settle_seconds),
                       ('startup_grace_seconds', arguments.startup_grace_seconds),
                       ('max_failures', arguments.max_failures),
                       ('max_restarts_per_window', arguments.max_restarts_per_window),
                       ('restart_window_seconds', arguments.restart_window_seconds)):
        if value is not None:
            state[key] = value
    normalize_timing(state)
    store = Path(state['state_root'])
    (store / 'objects').mkdir(parents=True, exist_ok=True)

    if arguments.rehearse:
        for key in ('source_root', 'state_root', 'home'):
            production = Path(DEFAULTS[key]).resolve()
            if Path(state[key]).resolve() == production:
                log(f'refusing to rehearse against the production {key} {production}')
                return 2
        if state['unit'] == DEFAULTS['unit']:
            log(f"refusing to rehearse against the production unit {DEFAULTS['unit']}")
            return 2

    if arguments.clear_stall:
        state['automation_stopped'] = False
        state['stall_reason'] = ''
        save_state(state['state_root'], state)
        log('automation stop cleared')
        return 0

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
        state['current_for_start'] = state.get('unit_started')
        state['last_snapshot_at'] = time.time()
        save_state(state['state_root'], state)
        log(f"snapshot {record['id']} files={record['files']}")
        return 0
    if arguments.once:
        outcome = cycle(state, store, dry_run=arguments.dry_run)
        print(json.dumps(outcome, ensure_ascii=False))
        return 0 if outcome['verdict'] in (
            'healthy', 'pending', 'degraded', 'rolled-back', 'deferred', 'stalled',
            'restart-requested', 'no-rollback-point') else 1

    log(f"watchdog up: unit={state['unit']} window={state['window_seconds']}s "
        f"grace={state['startup_grace_seconds']}s restarts={state['max_restarts_per_window']}"
        f"/{state['restart_window_seconds']}s")
    while True:
        try:
            cycle(state, store)
        except Exception as error:             # noqa: BLE001 - a watchdog must keep watching
            log(f'cycle error: {type(error).__name__}: {error}')
            save_state(state['state_root'], state)
        time.sleep(float(state.get('poll_seconds', 5.0)))


if __name__ == '__main__':
    raise SystemExit(main())
