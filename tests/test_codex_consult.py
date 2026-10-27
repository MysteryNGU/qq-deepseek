"""Process-backed consultation lifecycle checks; no external model or private data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.codex_consult import Bridge, ConsultError, atomic_json, run_worker


FAKE = r'''
import json,pathlib,sys,time,uuid
root=pathlib.Path(__file__).parent
if '--help' in sys.argv:
    print('--json --skip-git-repo-check --ignore-user-config --output-last-message');raise SystemExit()
if '--version' in sys.argv:
    print('fake-codex 1');raise SystemExit()
prompt=sys.stdin.read()
with (root/'calls.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(sys.argv)+'\n')
def emit(e): print(json.dumps(e),flush=True)
thread=sys.argv[-2] if 'resume' in sys.argv else str(uuid.uuid4())
emit(dict(type='thread.started',thread_id=thread));emit(dict(type='turn.started'))
if 'WAIT_FIXTURE' in prompt:time.sleep(20)
if 'BROKEN_FIXTURE' in prompt:
    print('{"type":',flush=True);raise SystemExit(3)
if 'FAIL_FIXTURE' in prompt:
    emit(dict(type='turn.failed',error={'message':'fixture failed'}));raise SystemExit(1)
if 'WRONG_THREAD_FIXTURE' in prompt:emit(dict(type='thread.started',thread_id='wrong'))
answer='fixture answer 中文 '+('updated init' if 'reread' in prompt else 'same thread')
emit(dict(type='item.completed',item=dict(type='agent_message',text=answer)))
pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(answer,encoding='utf-8')
emit(dict(type='turn.completed',usage={}))
'''


class ConsultBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root/'project'
        self.project.mkdir()
        for name in ('AGENTS.md', 'INIT.md'):
            (self.project/name).write_text('fixture init', encoding='utf-8')
        fixture = self.root/'fake.py'
        fixture.write_text(FAKE, encoding='utf-8')
        if os.name == 'nt':
            executable = self.root/'fake.cmd'
            executable.write_text('@echo off\n"'+sys.executable+'" "'+str(fixture)+'" %*\n', encoding='utf-8')
        else:
            executable = self.root/'fake'
            executable.write_text('#!'+sys.executable+'\n'+FAKE, encoding='utf-8')
            executable.chmod(0o700)
        self.config = self.root/'config.json'
        atomic_json(self.config, dict(enabled=True, project=str(self.project), data_dir=str(self.root/'data'),
            codex=str(executable), python=sys.executable, grace_seconds=2, timeout_seconds=40))
        self.bridge = Bridge(self.config)
        self.binding = dict(operation_id='op-1', dsh_session_id='dsh', generation=1, intent_ref=None,
            scope_json='{}', forget_epoch=0, intent_revision=None, origin='bot_self', source_event_ids=[])
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            if worker.poll() is None:
                worker.stdin.write(b'{"action":"cancel"}\n')
                worker.stdin.flush()
                worker.wait(timeout=10)
            for pipe in (worker.stdin, worker.stdout, worker.stderr):
                pipe.close()
        self.temp.cleanup()

    def prepare(self, message='hello', **values):
        return self.bridge.prepare('scope', dict(message=message, **values), self.binding)

    def start(self, result):
        request = result['request_id']
        self.bridge.bind_job(request, 'scope', 'native-job')
        worker = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve().parents[1]/'scripts/codex_consult.py'),
            '--config', str(self.config), 'worker', '--request-id', request], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.workers.append(worker)
        worker.stdin.write(json.dumps(dict(action='start', request_id=request, job_id='native-job')).encode()+b'\n')
        worker.stdin.flush()
        return worker

    def finish(self, worker):
        worker.wait(timeout=15)
        output = worker.stdout.read().decode()
        diagnostic = worker.stderr.read().decode()
        self.assertEqual(worker.returncode, 0, (output, diagnostic))
        return json.loads(output)

    def test_process_completion_followup_and_init_reload(self):
        first = self.prepare()
        result = self.finish(self.start(first))
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['process_stopped'])
        self.assertIn('中文', self.bridge.read(first['request_id'])['answer'])
        self.binding['operation_id'] = 'op-2'
        second = self.prepare('follow up', topic_id=first['topic_id'])
        request = json.loads((self.bridge.request_folder(second['request_id'])/'request.json').read_text(encoding='utf-8'))
        self.assertIn('unchanged', request['prompt'])
        self.assertEqual(second['thread_id'], result['thread_id'])
        self.assertEqual(self.finish(self.start(second))['status'], 'completed')
        (self.project/'INIT.md').write_text('changed init', encoding='utf-8')
        self.binding['operation_id'] = 'op-3'
        third = self.prepare('question', topic_id=first['topic_id'])
        request = json.loads((self.bridge.request_folder(third['request_id'])/'request.json').read_text(encoding='utf-8'))
        self.assertIn('reread', request['prompt'])
        self.assertEqual(self.finish(self.start(third))['status'], 'completed')
        self.assertEqual(len((self.root/'calls.jsonl').read_text(encoding='utf-8').splitlines()), 3)

    def test_duplicate_and_conflicting_requests_do_not_launch(self):
        first = self.prepare()
        repeat = self.prepare(request_id=first['request_id'])
        self.assertFalse(repeat['launch'])
        self.assertEqual(first['request_id'], repeat['request_id'])
        self.assertFalse(self.prepare()['launch'])
        with self.assertRaisesRegex(ConsultError, '不同'):
            self.prepare('different', request_id=first['request_id'])
        self.assertEqual(self.finish(self.start(first))['status'], 'completed')
        self.assertEqual(len((self.root/'calls.jsonl').read_text(encoding='utf-8').splitlines()), 1)

    def test_cancel_waits_for_actual_stop_and_preserves_other_processes(self):
        first = self.prepare('WAIT_FIXTURE')
        worker = self.start(first)
        deadline = time.monotonic()+8
        while self.bridge.status(first['request_id'])['status'] != 'running' and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.bridge.cancel(first['request_id'])['status'], 'cancel_requested')
        self.assertEqual(self.finish(worker)['status'], 'cancelled')
        self.assertEqual(self.bridge.status(first['request_id'])['process_stopped'], True)

    def test_fail_and_broken_stream_are_distinct(self):
        for message, status in [('FAIL_FIXTURE', 'failed'), ('BROKEN_FIXTURE', 'unknown')]:
            self.binding['operation_id'] = message
            first = self.prepare(message)
            result = self.finish(self.start(first))
            self.assertEqual(result['status'], status, result)
        self.binding['operation_id'] = 'after-unknown'
        with self.assertRaisesRegex(ConsultError, '未知'):
            self.prepare('no replay')

    def test_recovery_never_restarts_launched_request(self):
        first = self.prepare()
        with self.bridge.state() as state:
            record = state['requests'][first['request_id']]
            record.update(phase='launch_armed', dispatch_deadline=0)
        restored = Bridge(self.config)
        restored.reconcile()
        self.assertEqual(restored.status(first['request_id'])['status'], 'unknown')
        self.assertFalse((self.root/'calls.jsonl').exists())
        restored.reconcile()
        self.assertEqual(restored.status(first['request_id'])['version'], 2)

    def test_corrupt_state_fails_closed_and_scope_is_checked(self):
        first = self.prepare()
        with self.assertRaises(ConsultError):
            self.bridge.status(first['request_id'], 'another-scope')
        self.bridge.state_path.write_text('{broken', encoding='utf-8')
        with self.assertRaisesRegex(ConsultError, '损坏'):
            self.prepare()

    def test_unicode_pagination_has_no_gaps_and_checks_hash(self):
        first = self.prepare()
        self.bridge.commit(first['request_id'], 'completed', answer='中文😀abcdef'*10, evidence=dict(process_stopped=True))
        body, cursor = '', None
        while True:
            page = self.bridge.read(first['request_id'], cursor=cursor, limit_bytes=8)
            body += page['answer']
            cursor = page['next_cursor']
            if not cursor:
                break
        self.assertEqual(body, '中文😀abcdef'*10)
        (self.bridge.request_folder(first['request_id'])/'result.json').write_text('{}')
        with self.assertRaisesRegex(ConsultError, '哈希'):
            self.bridge.read(first['request_id'])

    def test_pending_dispatch_is_not_misclassified_during_admission(self):
        first = self.prepare()
        self.bridge.reconcile()
        self.assertEqual(self.bridge.status(first['request_id'])['status'], 'queued')
        self.bridge.settle(first['request_id'], stopped=True)
        self.assertEqual(self.bridge.status(first['request_id'])['status'], 'failed')

    def test_late_stop_receipt_refines_existing_unknown_result_idempotently(self):
        first=self.prepare()
        self.bridge.bind_job(first['request_id'],'scope','native-job')
        self.bridge.commit(first['request_id'],'unknown',reason='interrupted_without_final_evidence')
        self.bridge.adopted(first['request_id'],2)
        settled=self.bridge.settle(first['request_id'],stopped=True,exit_code=1)
        self.assertEqual((settled['status'],settled['process_stopped'],settled['delivery']),('unknown',True,'pending'))
        self.assertEqual(self.bridge.read(first['request_id'])['evidence']['worker_exit_code'],1)
        self.assertEqual(self.bridge.settle(first['request_id'],stopped=True,exit_code=1)['version'],settled['version'])
        self.bridge.settle(first['request_id'],stopped=False)
        self.assertTrue(self.bridge.status(first['request_id'])['process_stopped'])
        self.assertFalse((self.root/'calls.jsonl').exists())

    def test_cancelled_unknown_consult_requires_process_range_stop_proof(self):
        first=self.prepare()
        self.bridge.bind_job(first['request_id'],'scope','native-job')
        self.bridge.commit(first['request_id'],'unknown',reason='interrupted_without_final_evidence')
        self.bridge.cancel(first['request_id'])
        unknown=self.bridge.settle(first['request_id'],stopped=False)
        self.assertEqual(unknown['status'],'unknown')
        self.assertFalse(unknown['process_stopped'])
        cancelled=self.bridge.settle(first['request_id'],stopped=True,exit_code=1)
        self.assertEqual(cancelled['status'],'cancelled')
        self.assertTrue(cancelled['process_stopped'])
        self.assertEqual(self.bridge.settle(first['request_id'],stopped=True)['version'],cancelled['version'])
        self.assertFalse((self.root/'calls.jsonl').exists())

    def test_cancel_after_adopted_unknown_stop_receipt_preserves_stop_evidence(self):
        first=self.prepare()
        self.bridge.bind_job(first['request_id'],'scope','native-job')
        self.bridge.commit(first['request_id'],'unknown',reason='incomplete_result',evidence=dict(process_stopped=True))
        settled=self.bridge.settle(first['request_id'],stopped=True,exit_code=1)
        self.bridge.adopted(first['request_id'],settled['version'])
        self.bridge.cancel(first['request_id'])
        self.bridge.reconcile()
        cancelled=self.bridge.status(first['request_id'])
        self.assertEqual(cancelled['status'],'cancelled')
        self.assertTrue(cancelled['process_stopped'])
        self.assertEqual(self.bridge.read(first['request_id'])['evidence']['worker_exit_code'],1)
        self.bridge.reconcile()
        self.assertEqual(self.bridge.status(first['request_id'])['version'],cancelled['version'])

    def test_atomic_result_survives_crash_before_state_commit(self):
        first = self.prepare()
        original = self.bridge.records()[0]
        self.bridge.commit(first['request_id'], 'completed', answer='committed answer', evidence=dict(process_stopped=True))
        with self.bridge.state() as state:
            state['requests'][first['request_id']] = original
        self.bridge.reconcile()
        self.assertEqual(self.bridge.status(first['request_id'])['status'], 'completed')
        self.assertEqual(self.bridge.read(first['request_id'])['answer'], 'committed answer')

    def test_repeated_request_during_live_work_does_not_start_again(self):
        first = self.prepare('WAIT_FIXTURE')
        worker = self.start(first)
        deadline = time.monotonic()+8
        while self.bridge.status(first['request_id'])['status'] != 'running' and time.monotonic() < deadline:
            time.sleep(.05)
        repeated = self.prepare('WAIT_FIXTURE', request_id=first['request_id'])
        self.assertFalse(repeated['launch'])
        self.assertEqual(repeated['request_id'], first['request_id'])
        self.bridge.cancel(first['request_id'])
        self.assertEqual(self.finish(worker)['status'], 'cancelled')
        self.assertEqual(len((self.root/'calls.jsonl').read_text(encoding='utf-8').splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
