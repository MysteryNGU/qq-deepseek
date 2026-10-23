"""Actual controller restart, multi-file activation and additive migration; no QQ/model."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from life.release import NativeWorker,Probe,ReleaseController
from life.supervisor import ProcessTree


def wait_for(predicate,seconds=30):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.1)
    raise TimeoutError('publication smoke condition not reached')


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)/'source'
        root.mkdir()
        for name in ('life','tests'):
            shutil.copytree(ROOT/name,root/name,ignore=shutil.ignore_patterns('__pycache__','*.log','*.json','artifacts'))
        for source in ROOT.glob('*.py'):
            shutil.copyfile(source,root/source.name)
        (root/'persona.md').write_text('Synthetic publication fixture',encoding='utf-8')
        (root/'config.toml').write_text('life_db = "data/life.db"\n',encoding='utf-8')
        (root/'worker.py').write_text("def version():\n    return 'v1'\n",encoding='utf-8')
        (root/'bot.py').write_text('''import json,os,time
from pathlib import Path
from worker import version
target=Path(os.environ['QQ_WORKER_HEALTH'])
while True:
    target.write_text(json.dumps(dict(token=os.environ['QQ_WORKER_TOKEN'],pid=os.getpid(),
        at=time.time(),completed_turns=3,runtime=dict(version=version()))),encoding='utf-8')
    time.sleep(.2)
''',encoding='utf-8')
        database = root/'data/life.db'
        database.parent.mkdir()
        db = sqlite3.connect(database)
        db.execute('CREATE TABLE memo(body TEXT)')
        db.execute("INSERT INTO memo VALUES('keep post-start data')")
        db.commit()
        db.close()
        controller = ReleaseController(root,database.parent/'life-control',NativeWorker())
        command = [sys.executable,'-B',str(root/'lifesupervisor.py'),'--config',str(root/'config.toml')]
        log = (Path(directory)/'controller.log').open('wb')
        environment = dict(os.environ,PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
        tree = ProcessTree(command,cwd=root,env=environment,stderr_file=log)
        second = None
        try:
            health_path = controller.control/'worker-health.json'
            def health():
                try:
                    return json.loads(health_path.read_text('utf-8'))
                except (OSError,ValueError):
                    return {}
            first = wait_for(lambda:health() if health().get('runtime',{}).get('version')=='v1' else None)
            core = root/'life/supervisor.py'
            patch = dict(changes=[dict(file='worker.py',old="return 'v1'",new='from extra import value\n    return value()'),
                dict(file='extra.py',action='create',content="def value():\n    return 'v2'\n"),
                dict(file='life/supervisor.py',action='write',base_hash=controller.digest(core),
                     content=core.read_text('utf-8')+'\nPUBLICATION_SMOKE_MARKER = True\n')],
                migrations=[dict(id='memo-meta-v1',sql=['ALTER TABLE memo ADD COLUMN meta TEXT DEFAULT \'preserved\''])])
            identity = controller.prepare('core-smoke',patch)
            origin = dict(kind='operator',database=str(database))
            report = controller.evaluate(identity,[Probe('worker','version',{},'v2')],origin=origin,max_cost_ratio=10)
            assert report['verdict']=='publishable',report
            controller.request_publish(identity,origin=origin)
            assert tree.process.wait(timeout=30)==42,'controller did not request a clean restart'
            tree.stop()
            second = ProcessTree(command,cwd=root,env=environment,stderr_file=log)
            after = wait_for(lambda:health() if health().get('runtime',{}).get('version')=='v2' else None)
            wait_for(lambda:controller.deployment(identity)['phase']=='observed')
            db = sqlite3.connect(database)
            try:
                assert db.execute('SELECT * FROM memo').fetchone()==('keep post-start data','preserved')
            finally:
                db.close()
            assert first['pid']!=after['pid']
            print(json.dumps(dict(core_controller_restart=True,new_module=True,multi_file=True,
                migration_preserved_rows=True,old_worker_pid=first['pid'],new_worker_pid=after['pid'],
                phase=controller.deployment(identity)['phase'],real_qq_messages=0)),flush=True)
        finally:
            tree.stop()
            if second:
                second.stop()
            log.close()


if __name__=='__main__':
    main()
