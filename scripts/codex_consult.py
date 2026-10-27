#!/usr/bin/env python3
"""Durable, single-flight Codex consultation bridge. No prompt is replayed on recovery."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TERMINAL = {'completed', 'failed', 'cancelled'}
UNRESOLVED = {'queued', 'submitted', 'running', 'cancel_requested', 'unknown'}
REQUEST_ID = re.compile(r'cc_(\d{8}T\d{6}Z)_[a-f0-9]{32}\Z')
TOPIC_ID = re.compile(r'ct_[a-f0-9]{32}\Z')


class ConsultError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

    def receipt(self):
        return dict(ok=False, accepted=False, code=self.code, message=str(self))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(canonical(value)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.02*(attempt+1))
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path, *, blocking=True):
    """OS-owned locks survive neither process death nor a closed file descriptor."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    locked = False
    try:
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            deadline = time.monotonic()+10 if blocking else time.monotonic()
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise ConsultError('busy', '咨询状态正在使用，请稍后核对') from error
                    time.sleep(.02)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as error:
                raise ConsultError('busy', '已有咨询进程') from error
        locked = True
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def process_identity(pid):
    import psutil
    try:
        process = psutil.Process(pid)
        return dict(pid=pid, creation_time=process.create_time(), executable=process.exe())
    except psutil.NoSuchProcess:
        return None


def identity_alive(identity):
    if not identity:
        return False
    try:
        observed = process_identity(identity['pid'])
        return bool(observed and observed == identity)
    except (OSError, KeyError):
        return None


class Bridge:
    """Owns state and bounded recovery artifacts for one configured consultation project."""
    def __init__(self, config):
        self.config_path = Path(config).resolve()
        raw = json.loads(self.config_path.read_text('utf-8'))
        defaults = dict(timeout_seconds=1200, grace_seconds=10, max_message_bytes=32768,
                        max_result_bytes=1048576, max_line_bytes=4194304,
                        max_evidence_bytes=8388608, max_total_bytes=268435456,
                        max_requests=1000, max_topics=100, result_days=7, request_days=30,
                        model='', enabled=False, windows_sandbox='elevated')
        allowed = set(defaults) | {'project', 'data_dir', 'codex', 'python'}
        if not isinstance(raw, dict) or set(raw)-allowed:
            raise ConsultError('invalid_config', '未知咨询配置字段')
        self.cfg = dict(defaults, **raw)
        if type(self.cfg['enabled']) is not bool:
            raise ConsultError('invalid_config', 'enabled 必须为布尔值')
        for key, default in defaults.items():
            if type(default) is int and (type(self.cfg[key]) is not int or self.cfg[key] <= 0):
                raise ConsultError('invalid_config', key+' 必须为正整数')
        for key in ('project', 'data_dir', 'codex', 'python'):
            if not isinstance(self.cfg.get(key), str) or not self.cfg[key]:
                raise ConsultError('invalid_config', key+' 必须为固定路径')
            path = Path(self.cfg[key])
            if not path.is_absolute():
                raise ConsultError('invalid_config', key+' 必须为绝对路径')
            self.cfg[key] = str(path.resolve())
        if not isinstance(self.cfg['model'], str):
            raise ConsultError('invalid_config', 'model 必须为字符串')
        if self.cfg['windows_sandbox'] not in {'mxc','elevated','unelevated'}:
            raise ConsultError('invalid_config', '无效 Windows 沙箱实现')
        if self.cfg['result_days'] > self.cfg['request_days']:
            raise ConsultError('invalid_config', '结果保留期不能超过请求保留期')
        self.folder = Path(self.cfg['data_dir'])
        self.project = Path(self.cfg['project'])
        self.state_path = self.folder/'state.json'

    @contextmanager
    def state(self):
        with file_lock(self.folder/'locks/state.lock'):
            try:
                if self.state_path.exists():
                    value = json.loads(self.state_path.read_text('utf-8'))
                elif any((self.folder/'requests').glob('*/request.json')):
                    raise ConsultError('state_corrupt', '状态缺失但存在原请求；禁止重新初始化')
                else:
                    value = dict(schema_version=1, revision=0, topics={}, requests={})
                if value.get('schema_version') != 1 or not isinstance(value.get('topics'), dict) or not isinstance(value.get('requests'), dict):
                    raise ConsultError('state_corrupt', '咨询状态格式无法识别')
            except (ValueError, AttributeError) as error:
                raise ConsultError('state_corrupt', '咨询状态损坏；不能按空状态重建') from error
            before = canonical(value)
            yield value
            if canonical(value) != before or not self.state_path.exists():
                value['revision'] += 1
                atomic_json(self.state_path, value)

    def request_folder(self, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ConsultError('invalid_input', '无效咨询编号')
        path = self.folder/'requests'/request_id
        if path.is_symlink() or path.resolve().parent != (self.folder/'requests').resolve():
            raise ConsultError('invalid_input', '咨询目录不能通过链接越界')
        return path

    def get(self, state, request_id, scope_id=None):
        self.request_folder(request_id)
        record = state['requests'].get(request_id)
        if not record:
            raise ConsultError('request_not_found', '咨询编号不存在或已经过期；不会重新提问')
        if scope_id is not None and record['scope_id'] != scope_id:
            raise ConsultError('scope_mismatch', '咨询不属于当前会话')
        return record

    def snapshot(self, record):
        return {key: record.get(key) for key in ('request_id', 'topic_id', 'status', 'phase', 'version',
            'thread_id', 'job_id', 'result_ref', 'result_sha256', 'reason', 'process_stopped', 'delivery', 'recovery_evidence')}

    def _prune(self, state):
        now = time.time()
        for request_id, record in list(state['requests'].items()):
            age = now-record['created_at']
            if record['status'] not in TERMINAL or record['delivery'] != 'adopted':
                continue
            if age > self.cfg['result_days']*86400 and not record.get('result_expired'):
                directory = self.request_folder(request_id)
                if directory.exists():
                    shutil.rmtree(directory)
                record['result_expired'] = True
            if age > self.cfg['request_days']*86400:
                del state['requests'][request_id]
        used = {record['topic_id'] for record in state['requests'].values()}
        for topic_id, topic in list(state['topics'].items()):
            if topic['status'] == 'closed' and topic_id not in used:
                del state['topics'][topic_id]

    def prepare(self, scope_id, proposal, binding):
        """Persist a host-bound request before any worker is allowed to call Codex."""
        if not self.cfg['enabled']:
            raise ConsultError('unavailable', 'Codex 咨询未启用')
        message = proposal.get('message')
        if not isinstance(message, str) or not message.strip() or len(message.encode('utf-8')) > self.cfg['max_message_bytes']:
            raise ConsultError('invalid_input', '咨询正文不能为空或超过字节上限')
        request_id = proposal.get('request_id')
        fingerprint = sha(dict(message=message, topic_id=proposal.get('topic_id'), scope_id=scope_id,
                               intent_ref=proposal.get('intent_ref'), sources=binding['source_event_ids'], origin=binding['origin']))
        # Idempotent queries remain available while the real worker owns the active lock.
        with self.state() as state:
            previous = self.get(state, request_id, scope_id) if request_id else next(
                (r for r in state['requests'].values() if r['operation_id'] == binding['operation_id']), None)
            if previous:
                if previous['request_sha256'] != fingerprint:
                    raise ConsultError('idempotency_conflict', '同一咨询编号对应不同正文、话题或来源')
                return dict(self.snapshot(previous), accepted=True, launch=False)
        with file_lock(self.folder/'locks/active.lock', blocking=False), self.state() as state:
            if request_id:
                record = self.get(state, request_id, scope_id)
                if record['request_sha256'] != fingerprint:
                    raise ConsultError('idempotency_conflict', '咨询编号对应不同正文、话题或来源')
                return dict(self.snapshot(record), accepted=True, launch=False)
            previous = next((r for r in state['requests'].values() if r['operation_id'] == binding['operation_id']), None)
            if previous:
                if previous['request_sha256'] != fingerprint:
                    raise ConsultError('idempotency_conflict', '同一调用对应不同咨询')
                return dict(self.snapshot(previous), accepted=True, launch=False)
            self._prune(state)
            active = next((r for r in state['requests'].values() if r['status'] in UNRESOLVED), None)
            if active:
                raise ConsultError('busy', '已有未完成或结果未知的咨询：'+active['request_id'])
            if len(state['requests']) >= self.cfg['max_requests']:
                raise ConsultError('capacity', '咨询幂等索引已满，保留未过期记录')
            total = sum(path.stat().st_size for path in self.folder.rglob('*') if path.is_file())
            if total >= self.cfg['max_total_bytes']:
                raise ConsultError('capacity', '咨询暂存已满，先核对并采用原结果')
            topic_id = proposal.get('topic_id')
            if topic_id:
                topic = state['topics'].get(topic_id)
                if not topic or topic['scope_id'] != scope_id or topic['status'] != 'open':
                    raise ConsultError('scope_mismatch', '咨询话题不存在、已关闭或不属于当前会话')
            else:
                if len(state['topics']) >= self.cfg['max_topics']:
                    raise ConsultError('capacity', '咨询话题索引已满，请关闭不再使用的话题')
                for topic in state['topics'].values():
                    if topic['scope_id'] == scope_id:
                        topic['status'] = 'closed'
                topic_id = 'ct_'+uuid.uuid4().hex
                topic = dict(scope_id=scope_id, status='open', thread_id=None, init_sha256=None, agents_sha256=None)
                state['topics'][topic_id] = topic
            versions = {name.lower().replace('.md', '')+'_sha256': sha((self.project/name).read_bytes()) for name in ('INIT.md', 'AGENTS.md')}
            now = time.time()
            request_id = 'cc_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex
            record = dict(binding, request_id=request_id, topic_id=topic_id, scope_id=scope_id,
                request_sha256=fingerprint, **versions, thread_id=topic['thread_id'], status='queued',
                phase='reserved', version=1, attempt_id=uuid.uuid4().hex, job_id=None,
                created_at=now, updated_at=now, dispatch_deadline=now+30, process=None, worker_process=None, process_stopped=False,
                result_ref=None, result_sha256=None, delivery='none', cancel_requested_at=None)
            reread = not topic['thread_id'] or any(topic.get(key) != value for key, value in versions.items())
            envelope = dict(request_id=request_id, topic_id=topic_id, sent_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                timezone='Asia/Shanghai', origin=binding['origin'], source_event_ids=binding['source_event_ids'],
                mode='consult', **versions, init_action='reread' if reread else 'unchanged')
            prompt = ('[桥接元数据]\n'+canonical(envelope)+'\n'+
                ('请重新读取 AGENTS.md 与 INIT.md，再回应。\n' if reread else '')+'\n[小鱼的话]\n'+message)
            atomic_json(self.request_folder(request_id)/'request.json', dict(record=record, prompt=prompt, config=self.cfg))
            state['requests'][request_id] = record
            topic.update(active_request_id=request_id, updated_at=now)
            return dict(self.snapshot(record), accepted=True, launch=True, argv=[self.cfg['python'], '-u', str(Path(__file__).resolve()),
                '--config', str(self.config_path), 'worker', '--request-id', request_id], cwd=str(ROOT),
                grace_ms=self.cfg['grace_seconds']*1000, timeout_ms=(self.cfg['timeout_seconds']+self.cfg['grace_seconds']*3)*1000)

    def bind_job(self, request_id, scope_id, job_id):
        with self.state() as state:
            record = self.get(state, request_id, scope_id)
            if record['job_id'] and record['job_id'] != job_id:
                raise ConsultError('idempotency_conflict', '原请求已绑定另一个作业')
            if record['phase'] != 'reserved':
                raise ConsultError('outcome_unknown', '不能再次派发原咨询')
            if time.time() > record['dispatch_deadline']:
                raise ConsultError('not_dispatched', '启动许可预约已过期')
            record.update(job_id=job_id, phase='job_bound', updated_at=time.time())
            return self.snapshot(record)

    def status(self, request_id, scope_id=None):
        with self.state() as state:
            return self.snapshot(self.get(state, request_id, scope_id))

    def cancel(self, request_id, scope_id=None):
        with self.state() as state:
            record = self.get(state, request_id, scope_id)
            if record['status'] not in TERMINAL and not record.get('cancel_requested_at'):
                record.update(cancel_requested_at=time.time(), status='cancel_requested', updated_at=time.time())
            return self.snapshot(record)

    def close_topic(self, topic_id, scope_id):
        with self.state() as state:
            topic = state['topics'].get(topic_id)
            if not topic or topic['scope_id'] != scope_id:
                raise ConsultError('scope_mismatch', '咨询话题不存在')
            if any(r['topic_id'] == topic_id and r['status'] in UNRESOLVED for r in state['requests'].values()):
                raise ConsultError('busy', '先核对或取消未完成的咨询')
            topic['status'] = 'closed'
            return dict(topic_id=topic_id, status='closed')

    def commit(self, request_id, status, *, answer='', reason='', evidence=None):
        with self.state() as state:
            record = self.get(state, request_id)
            if record['status'] in TERMINAL:
                return self.snapshot(record)
            if record['status'] == status and record.get('reason') == reason and record.get('result_ref'):
                previous = json.loads((self.request_folder(request_id)/'result.json').read_text('utf-8'))
                if previous.get('evidence',{}) == (evidence or {}):
                    return self.snapshot(record)
            version = record['version']+1
            result = dict(type='consult_result', request_id=request_id, topic_id=record['topic_id'], status=status,
                version=version, thread_id=record['thread_id'], answer=answer, reason=reason,
                attempt_id=record['attempt_id'], request_sha256=record['request_sha256'],
                evidence=evidence or {}, engineering_outcome='not_claimed')
            path = self.request_folder(request_id)/'result.json'
            atomic_json(path, result)
            record.update(status=status, reason=reason, version=version, result_ref='consult-result:'+request_id,
                result_sha256=sha(path.read_bytes()), delivery='pending', phase='result_committed', updated_at=time.time(),
                process_stopped=bool((evidence or {}).get('process_stopped')))
            topic = state['topics'][record['topic_id']]
            if status in TERMINAL:
                topic['active_request_id'] = None
            if record['thread_id']:
                topic['thread_id'] = record['thread_id']
            if status == 'completed':
                topic.update(thread_id=record['thread_id'], init_sha256=record['init_sha256'], agents_sha256=record['agents_sha256'])
            return self.snapshot(record)

    def read(self, request_id, scope_id=None, cursor=None, limit_bytes=8192):
        if type(limit_bytes) is not int or not 4 <= limit_bytes <= 32768:
            raise ConsultError('invalid_input', '读取上限为 4..32768 字节')
        with self.state() as state:
            record = dict(self.get(state, request_id, scope_id))
        if record.get('result_expired'):
            raise ConsultError('result_expired', '结果暂存已清理；不会重新提问')
        if not record['result_ref']:
            return dict(self.snapshot(record), answer='', has_more=False)
        raw = (self.request_folder(request_id)/'result.json').read_bytes()
        if sha(raw) != record['result_sha256']:
            raise ConsultError('result_corrupt', '结果哈希不匹配')
        result = json.loads(raw)
        offset = 0
        if cursor:
            try:
                token, position = cursor.split(':')
                offset = int(position)
                if token != record['result_sha256'] or offset < 0:
                    raise ValueError('stale cursor')
            except (ValueError, AttributeError) as error:
                raise ConsultError('stale_cursor', '结果游标已过期或无效') from error
        body = result.pop('answer').encode('utf-8')
        if offset > len(body) or offset and body[offset:offset+1] and body[offset] & 0xC0 == 0x80:
            raise ConsultError('invalid_input', '游标不在字符边界')
        page = body[offset:offset+limit_bytes].decode('utf-8', errors='ignore')
        end = offset+len(page.encode('utf-8'))
        return dict(result, result_ref=record['result_ref'], answer=page, has_more=end < len(body),
                    next_cursor=record['result_sha256']+':'+str(end) if end < len(body) else None)

    def adopted(self, request_id, version):
        with self.state() as state:
            record = self.get(state, request_id)
            if record['version'] == version:
                record['delivery'] = 'adopted'

    def records(self):
        with self.state() as state:
            return json.loads(canonical(list(state['requests'].values())))

    def inspect_history(self, record):
        """Inspect only the known thread's local history, without creating another Codex turn."""
        try:
            identity = uuid.UUID(record.get('thread_id') or '')
        except ValueError:
            return
        if identity.version != 7:
            return
        started = datetime.fromtimestamp(int(identity.hex[:12],16)/1000, timezone.utc)
        home = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))).resolve()
        folders = {home/'sessions'/date.strftime('%Y/%m/%d') for date in
                   (started, started.astimezone(timezone(timedelta(hours=8))))}
        files = [file for folder in folders for file in folder.glob('*'+str(identity)+'.jsonl')]
        if len(files) != 1 or files[0].is_symlink() or home not in files[0].resolve().parents:
            return
        path = files[0]
        info = path.stat()
        signature = [info.st_size, info.st_mtime_ns]
        if signature == record.get('history_signature'):
            return
        result = dict(history_ref=str(path), request_seen=False, turn_completed=False,
                      coverage='partial' if info.st_size > 16*1024*1024 else 'complete')
        turn = None
        consumed = 0
        with path.open('rb') as stream:
            for line in stream:
                consumed += len(line)
                if consumed > 16*1024*1024:
                    break
                try:
                    event = json.loads(line)
                    payload = event.get('payload', {})
                    if event.get('type') != 'event_msg':
                        continue
                    item = payload.get('item', {})
                    if payload.get('type') == 'item_completed' and item.get('type') == 'UserMessage':
                        if any('"request_id":"'+record['request_id']+'"' in block.get('text','') for block in item.get('content', [])):
                            turn = payload.get('turn_id')
                            result.update(request_seen=True, turn_id=turn)
                    if turn and payload.get('turn_id') == turn and payload.get('type') == 'task_complete':
                        result.update(turn_completed=True, answer_preview=str(payload.get('last_agent_message', ''))[:1000])
                except (ValueError, AttributeError, TypeError):
                    result['coverage'] = 'partial'
        # A historical completion alone cannot establish CLI exit and process-tree
        # quiescence. Keep unknown, but expose the exact evidence for further review.
        with self.state() as state:
            self.get(state, record['request_id']).update(history_signature=signature, recovery_evidence=result)

    def reconcile(self):
        for record in self.records():
            if record['status'] == 'unknown':
                self.inspect_history(record)
            # Recover the commit window between result.json replacement and state replacement.
            path = self.request_folder(record['request_id'])/'result.json'
            if path.is_file() and record['status'] not in TERMINAL:
                try:
                    result = json.loads(path.read_text('utf-8'))
                    matches = result['request_id'] == record['request_id'] and result['attempt_id'] == record['attempt_id'] and result['request_sha256'] == record['request_sha256']
                    if matches and result['version'] > record['version'] and result['status'] in TERMINAL | {'unknown'}:
                        with self.state() as state:
                            self.get(state, record['request_id'])['thread_id'] = result['thread_id']
                        self.commit(record['request_id'], result['status'], answer=result['answer'], reason=result['reason'], evidence=result['evidence'])
                        continue
                except (ValueError, KeyError, TypeError):
                    # The unmatched/partial artifact is evidence of an interrupted write, never a result.
                    pass
            if (record['status'] in UNRESOLVED and record.get('cancel_requested_at')
                    and record.get('process_stopped') is True):
                self.settle(record['request_id'],stopped=True)
                continue
            if record['status'] not in UNRESOLVED or record['status'] == 'unknown':
                continue
            if record['phase'] in {'reserved','job_bound'} and time.time() < record['dispatch_deadline']:
                continue
            # A missing PID after launch_armed cannot prove the request never ran.
            if identity_alive(record.get('worker_process')) or identity_alive(record.get('process')):
                continue
            try:
                with file_lock(self.folder/'locks/active.lock', blocking=False):
                    status = 'failed' if record['phase'] == 'reserved' else 'unknown'
                    self.commit(record['request_id'], status, reason='failed_before_dispatch' if status == 'failed' else 'interrupted_without_final_evidence')
            except ConsultError as error:
                if error.code != 'busy':
                    raise
        return [self.snapshot(r) for r in self.records()]

    def settle(self, request_id, *, stopped, exit_code=None):
        """Record outer DSH process-range evidence without inferring a lost Codex answer."""
        current = self.status(request_id)
        if current['status'] in TERMINAL:
            return current
        previous = self.read(request_id) if current['result_ref'] else {}
        evidence = dict(previous.get('evidence',{}))
        # A later process-range receipt must refine an already-adopted unknown
        # result. Keep stop evidence monotonic; missing PIDs are never that proof.
        evidence['process_stopped'] = evidence.get('process_stopped') is True or stopped is True
        if exit_code is not None or 'worker_exit_code' not in evidence:
            evidence['worker_exit_code'] = exit_code
        record = next(r for r in self.records() if r['request_id']==request_id)
        not_started = evidence['process_stopped'] and current['phase'] in {'reserved','job_bound'}
        cancelled = evidence['process_stopped'] and bool(record.get('cancel_requested_at'))
        status = 'failed' if not_started else 'cancelled' if cancelled else 'unknown'
        reason = ('failed_before_dispatch' if not_started else 'cancelled_after_process_range_stopped' if cancelled
                  else previous.get('reason') or 'worker_exited_without_complete_result')
        self.commit(request_id,status,reason=reason,evidence=evidence)
        return self.status(request_id)

    def doctor(self):
        for name in ('AGENTS.md', 'INIT.md'):
            if not (self.project/name).is_file():
                raise ConsultError('unavailable', '缺少初始化文档：'+name)
        for name in ('codex', 'python'):
            if not Path(self.cfg[name]).is_file():
                raise ConsultError('unavailable', '找不到 '+name+' 可执行文件')
        observed = {}
        for label, args in [('version', ['--version']), ('root', ['--help']), ('exec', ['exec', '--help']), ('resume', ['exec', 'resume', '--help'])]:
            proc = subprocess.run([self.cfg['codex'], *args], capture_output=True, timeout=20, encoding='utf-8', errors='replace',
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if proc.returncode:
                raise ConsultError('unavailable', 'CLI 帮助核对失败：'+label)
            observed[label] = proc.stdout
        for option in ('--json', '--skip-git-repo-check', '--ignore-user-config', '--output-last-message'):
            if option not in observed['exec'] or option not in observed['resume']:
                raise ConsultError('protocol_mismatch', 'CLI 缺少所需选项：'+option)
        if os.name == 'nt':
            # Exercise the selected sandbox without a model call. A readable project
            # and an actually denied write are both prerequisites for deployment.
            common = [self.cfg['codex'], '-c', 'windows.sandbox='+self.cfg['windows_sandbox'],
                      'sandbox', '--include-managed-config', '--permission-profile', ':read-only', '--']
            probe = self.project/('.readonly-probe-'+uuid.uuid4().hex)
            command = str(Path(os.environ['SYSTEMROOT'])/'System32/cmd.exe')
            read = subprocess.run([*common, command, '/d', '/c', 'type', str(self.project/'INIT.md')],
                capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            write = subprocess.run([*common, command, '/d', '/c', 'echo denied > "'+str(probe)+'"'],
                capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            writable = probe.exists()
            probe.unlink(missing_ok=True)
            if read.returncode or write.returncode == 0 or writable:
                raise ConsultError('sandbox_unavailable', '咨询只读沙箱未通过读取/拒写检查')
        with self.state():
            pass
        return dict(ok=True, version=observed['version'].strip(), project=str(self.project), enabled=self.cfg['enabled'],
                    policy='read-only; ignore-user-config; explicit thread id; apps/plugins/hooks disabled',
                    windows_sandbox=self.cfg['windows_sandbox'] if os.name == 'nt' else None,
                    model=self.cfg['model'] or 'Codex default')


def stop_tree(tree, grace):
    """Terminate only the owned process range and verify it is empty before reporting stopped."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Accounting(ctypes.Structure):
            _fields_ = [('user', ctypes.c_int64), ('kernel', ctypes.c_int64), ('period_user', ctypes.c_int64),
                        ('period_kernel', ctypes.c_int64), ('faults', wintypes.DWORD), ('total', wintypes.DWORD),
                        ('active', wintypes.DWORD), ('terminated', wintypes.DWORD)]
        kernel = tree.kernel
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        if not kernel.TerminateJobObject(tree.job, 1):
            raise OSError('cannot_stop_consult_tree')
        deadline = time.monotonic()+grace
        while True:
            info = Accounting()
            if not kernel.QueryInformationJobObject(tree.job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                raise OSError('cannot_verify_consult_tree')
            if info.active == 0:
                tree.stop()
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(.02)
    import signal
    try:
        os.killpg(tree.process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    tree.process.wait(timeout=grace)
    return True


def run_worker(bridge, request_id):
    """Run once after durable job binding and an explicit stdin start permit."""
    from life.supervisor import ProcessTree
    with bridge.state() as state:
        record = bridge.get(state, request_id)
        if record['phase'] not in {'reserved','job_bound'}:
            raise ConsultError('outcome_unknown', '原请求不可再次启动')
        record['worker_process'] = process_identity(os.getpid())
    # A worker without a permit never launches a model call.
    permit_line = bytearray()
    while len(permit_line) <= 65536:
        byte = os.read(sys.stdin.fileno(), 1)
        if not byte or byte == b'\n':
            break
        permit_line.extend(byte)
    permit = json.loads(permit_line) if permit_line else {}
    if permit.get('action') != 'start' or permit.get('request_id') != request_id:
        raise ConsultError('not_dispatched', '没有有效启动许可')
    cancelled = threading.Event()
    def controls():
        pending = bytearray()
        while True:
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                break
            pending.extend(chunk)
            if len(pending) > 65536:
                cancelled.set()
                return
            while b'\n' in pending:
                line, _, tail = pending.partition(b'\n')
                pending[:] = tail
                try:
                    if json.loads(line).get('action') == 'cancel':
                        cancelled.set()
                except (ValueError, AttributeError):
                    cancelled.set()
        # Parent pipe loss must stop this worker's owned CLI, never detach it.
        cancelled.set()
    threading.Thread(target=controls, daemon=True).start()
    with file_lock(bridge.folder/'locks/active.lock', blocking=False):
        with bridge.state() as state:
            record = bridge.get(state, request_id)
            if record['phase'] != 'job_bound' or record['job_id'] != permit.get('job_id'):
                raise ConsultError('outcome_unknown', '启动许可不对应原作业')
            if record['cancel_requested_at']:
                pre_cancelled = True
            else:
                pre_cancelled = False
                record.update(phase='launch_armed', worker_process=process_identity(os.getpid()), updated_at=time.time())
            original = dict(record)
        if pre_cancelled:
            return bridge.commit(request_id, 'cancelled', reason='cancelled_before_dispatch', evidence=dict(process_stopped=True))
        folder = bridge.request_folder(request_id)
        request = json.loads((folder/'request.json').read_text('utf-8'))
        cfg = request['config']
        answer_file = folder/'answer.tmp'
        argv = [cfg['codex'], '-a', 'never', '-s', 'read-only']
        # Connected apps can write outside the shell sandbox. Consultation exposes
        # no app/plugin/hook execution path, even when the signed-in account has them.
        for feature in ('apps', 'plugins', 'hooks'):
            argv += ['--disable', feature]
        if os.name == 'nt':
            argv += ['-c', 'windows.sandbox='+cfg['windows_sandbox']]
        argv.append('exec')
        if original['thread_id']:
            argv.append('resume')
        argv += ['--ignore-user-config', '--json', '--skip-git-repo-check', '-o', str(answer_file)]
        if cfg['model']:
            argv += ['-m', cfg['model']]
        if original['thread_id']:
            argv.append(original['thread_id'])
        argv.append('-')
        env = {k:v for k,v in os.environ.items() if not re.search('KEY|TOKEN|SECRET|PASSWORD', k, re.I) and not k.upper().startswith('DSH_')}
        env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', NO_COLOR='1')
        tree = None
        stopped = False
        code = None
        turn_completed = False
        turn_failed = False
        turn_started = False
        thread_id = original['thread_id']
        message_seen = False
        error = ''
        evidence_bytes = 0
        stderr_tail = bytearray()
        events = queue.Queue(maxsize=64)
        readers = []
        try:
            tree = ProcessTree(argv, cwd=cfg['project'], env=env, pipes=True)
            with bridge.state() as state:
                record = bridge.get(state, request_id)
                record.update(process=process_identity(tree.process.pid), status='submitted', phase='process_started')
            def read_stdout():
                try:
                    while True:
                        line = tree.process.stdout.readline(cfg['max_line_bytes']+1)
                        if not line:
                            break
                        if len(line) > cfg['max_line_bytes']:
                            events.put(('error', 'output_limit'))
                            break
                        events.put(('line', line))
                finally:
                    events.put(('eof', None))
            def read_stderr():
                while True:
                    chunk = tree.process.stderr.read(4096)
                    if not chunk:
                        return
                    stderr_tail.extend(chunk)
                    del stderr_tail[:-65536]
            readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
            for reader in readers:
                reader.start()
            tree.process.stdin.write(request['prompt'].encode('utf-8'))
            tree.process.stdin.close()
            deadline = time.monotonic()+cfg['timeout_seconds']
            eof = False
            with (folder/'events.jsonl').open('ab') as journal:
                while not eof:
                    if cancelled.is_set():
                        error = 'cancel_requested'
                        break
                    if time.monotonic() > deadline:
                        error = 'timeout'
                        break
                    with bridge.state() as state:
                        requested = bridge.get(state, request_id)['cancel_requested_at']
                    if requested:
                        cancelled.set()
                        continue
                    try:
                        kind, value = events.get(timeout=.2)
                    except queue.Empty:
                        continue
                    if kind == 'eof':
                        eof = True
                        continue
                    if kind == 'error':
                        error = value
                        break
                    try:
                        event = json.loads(value)
                        tag = event['type']
                    except (ValueError, KeyError, TypeError):
                        error = 'invalid_jsonl'
                        break
                    if tag in {'thread.started', 'turn.started', 'turn.completed', 'turn.failed'}:
                        if tag == 'thread.started':
                            observed = event.get('thread_id')
                            if not isinstance(observed, str) or not observed or thread_id and observed != thread_id:
                                error = 'thread_mismatch'
                                break
                            thread_id = observed
                            with bridge.state() as state:
                                bridge.get(state, request_id)['thread_id'] = thread_id
                        elif tag == 'turn.started':
                            if turn_started or not thread_id:
                                error = 'turn_mismatch'
                                break
                            turn_started = True
                            with bridge.state() as state:
                                bridge.get(state, request_id).update(status='running', phase='turn_started')
                        elif tag == 'turn.completed':
                            if not turn_started or turn_failed or turn_completed:
                                error = 'turn_mismatch'
                                break
                            turn_completed = True
                        else:
                            if turn_completed:
                                error = 'conflicting_terminal_events'
                                break
                            turn_failed = True
                    elif tag == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
                        message_seen = isinstance(event['item'].get('text'), str) and bool(event['item']['text'].strip())
                    else:
                        continue
                    encoded = (canonical(event)+'\n').encode('utf-8')
                    evidence_bytes += len(encoded)
                    if evidence_bytes > cfg['max_evidence_bytes']:
                        error = 'output_limit'
                        break
                    journal.write(encoded)
                    journal.flush()
                    os.fsync(journal.fileno())
                if not error:
                    try:
                        code = tree.process.wait(timeout=max(.1, deadline-time.monotonic()))
                    except subprocess.TimeoutExpired:
                        error = 'timeout'
        except OSError as exc:
            error = 'spawn_failed' if tree is None else 'process_error:'+type(exc).__name__
        except Exception as exc:
            error = 'worker_error:'+type(exc).__name__
        finally:
            if tree:
                try:
                    stopped = stop_tree(tree, cfg['grace_seconds'])
                    code = tree.process.returncode if code is None else code
                except (OSError, subprocess.TimeoutExpired):
                    error = 'stop_unknown'
                # Drain queued lines so the bounded reader can reach EOF even on cancellation.
                drain_deadline = time.monotonic()+cfg['grace_seconds']
                while any(t.is_alive() for t in readers) and time.monotonic() < drain_deadline:
                    try:
                        events.get(timeout=.05)
                    except queue.Empty:
                        pass
                for pipe in (tree.process.stdout, tree.process.stderr):
                    pipe.close()
            else:
                stopped = True
            (folder/'stderr.tail').write_bytes(stderr_tail)
        evidence = dict(turn_completed=turn_completed, turn_failed=turn_failed, process_stopped=stopped,
                        process_exit_code=code, cancel_requested=cancelled.is_set(), timed_out=error == 'timeout')
        answer = ''
        if answer_file.exists() and answer_file.stat().st_size <= cfg['max_result_bytes']:
            try:
                answer = answer_file.read_text('utf-8')
            except UnicodeError:
                error = 'invalid_answer_encoding'
        elif answer_file.exists():
            error = 'output_limit'
        complete = not error and turn_completed and message_seen and bool(answer.strip()) and code == 0 and stopped
        status = 'completed' if complete else 'unknown'
        if stopped and not complete:
            if turn_failed or error == 'spawn_failed':
                status = 'failed'
            elif cancelled.is_set() and not turn_completed:
                status = 'cancelled'
        return bridge.commit(request_id, status, answer=answer if complete else '', reason=error or ('' if complete else 'incomplete_result'), evidence=evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('action', choices=['doctor', 'worker', 'status', 'read', 'reconcile'])
    parser.add_argument('--request-id')
    args = parser.parse_args()
    try:
        bridge = Bridge(args.config)
        if args.action == 'worker':
            result = run_worker(bridge, args.request_id)
        elif args.action == 'doctor':
            result = bridge.doctor()
        elif args.action == 'reconcile':
            result = bridge.reconcile()
        elif args.action == 'status':
            result = bridge.status(args.request_id)
        else:
            result = bridge.read(args.request_id)
        print(canonical(result), flush=True)
    except ConsultError as error:
        print(canonical(error.receipt()), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
