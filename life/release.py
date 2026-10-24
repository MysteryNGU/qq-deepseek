"""Independent controller for immutable capability worker releases.

Candidate code runs in a child process and returns untrusted JSON. The chosen
native backend is ordinary local execution, not an OS sandbox. Test oracles,
manifests, activation generations and rollback state stay in this controller.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
import uuid


DRIVER = """import importlib,json,sys
sys.path[:0]=['/candidate','/candidate/deps']
request=json.load(sys.stdin)
module=importlib.import_module(sys.argv[1])
fn=getattr(module,sys.argv[2])
result=fn(**request)
print(json.dumps(result,ensure_ascii=False,allow_nan=False))
"""


@dataclass(frozen=True)
class Probe:
    module: str
    function: str
    arguments: dict[str, Any]
    expected: Any
    critical: bool = True


class IsolationUnavailable(RuntimeError):
    pass


class DockerWorker:
    """No shell, production mounts, host environment, network or daemon socket."""
    def __init__(self, image: str, executable="docker"):
        if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
            raise ValueError("sandbox_image_must_be_pinned_by_digest")
        self.image = image
        self.executable = shutil.which(executable)
        if self.executable is None:
            raise IsolationUnavailable("docker_executable_unavailable")

    def available(self):
        try:
            result = subprocess.run([self.executable, "image", "inspect", self.image],
                                    capture_output=True, timeout=15)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def environment(self):
        return None

    def cleanup(self, process, name):
        subprocess.run([self.executable, "rm", "--force", name], capture_output=True, timeout=15)

    def launch(self, command, artifact, name):
        return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.environment(),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def command(self, artifact: Path, name: str, module: str, function: str):
        if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*", module) or not re.fullmatch(r"[a-zA-Z_]\w*", function):
            raise ValueError("invalid_worker_entrypoint")
        return [self.executable, "run", "--rm", "--pull=never", "--name", name,
                "--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--user=65534:65534",
                "--pids-limit=64", "--memory=512m", "--cpus=1",
                "--log-driver=none", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "--mount", f"type=bind,src={artifact.resolve()},dst=/candidate,readonly",
                "--workdir=/tmp", "-i", self.image, "python", "-I", "-B", "-c", DRIVER,
                module, function]

    def invoke(self, artifact, module, function, arguments, *, timeout=30):
        name = "qq-candidate-" + uuid.uuid4().hex
        command = self.command(artifact, name, module, function)
        payload = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()
        if len(payload) > 65536:
            raise ValueError("worker_input_budget_exceeded")
        started = time.monotonic()
        process = self.launch(command, artifact, name)
        chunks = {"out": bytearray(), "err": bytearray()}
        exceeded = threading.Event()
        def drain(stream, key):
            while True:
                block = stream.read(4096)
                if not block:
                    break
                if len(chunks[key]) + len(block) > 1024 * 1024:
                    exceeded.set()
                    process.kill()
                    break
                chunks[key].extend(block)
        def feed():
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (OSError, BrokenPipeError):
                pass
        readers = [threading.Thread(target=drain, args=(process.stdout, "out"), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, "err"), daemon=True),
                   threading.Thread(target=feed, daemon=True)]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise TimeoutError("isolated_worker_timeout")
        finally:
            # Killing the CLI alone does not stop the container.
            self.cleanup(process, name)
            for reader in readers:
                reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        if exceeded.is_set():
            raise ValueError("worker_output_budget_exceeded")
        if process.returncode:
            raise RuntimeError("isolated_worker_failed")
        return json.loads(chunks["out"]), time.monotonic() - started


class NativeWorker(DockerWorker):
    """User-selected local process execution. This is not an OS sandbox."""
    def __init__(self, executable=None):
        self.executable = str(Path(executable or sys.executable).resolve())
        self.trees = {}
        if not Path(self.executable).is_file():
            raise ValueError("python_runtime_missing")

    def available(self):
        return True

    def environment(self):
        # Keep platform/runtime necessities, not API keys or parent overrides.
        return {key: value for key, value in os.environ.items()
                if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL",
                                   'USERPROFILE','HOMEDRIVE','HOMEPATH','HOME'}}

    def command(self, artifact, name, module, function):
        if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*", module) or not re.fullmatch(r"[a-zA-Z_]\w*", function):
            raise ValueError("invalid_worker_entrypoint")
        driver = DRIVER.replace("['/candidate','/candidate/deps']", "[sys.argv[3],sys.argv[3]+'/deps']")
        return [self.executable, "-I", "-B", "-c", driver, module, function, str(artifact.resolve())]

    def cleanup(self, process, name):
        tree = self.trees.pop(name, None)
        if tree is not None:
            tree.stop()

    def launch(self, command, artifact, name):
        from .supervisor import ProcessTree
        tree = ProcessTree(command, cwd=artifact, env=self.environment(), pipes=True)
        self.trees[name] = tree
        return tree.process

    def run_suite(self, artifact):
        from .supervisor import ProcessTree
        env = dict(self.environment(), PYTHONUTF8="1", PYTHONIOENCODING="utf-8", QQ_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")
        tree = ProcessTree([self.executable, "-B", "-X", "utf8", str(artifact / "tests" / "run_offline_suite.py")],
                           cwd=artifact, env=env, pipes=True)
        try:
            output, error = tree.process.communicate(timeout=240)
            return {"ok": tree.process.returncode == 0,
                    "log": (output + error).decode("utf-8", "replace")[-12000:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "offline_suite_timeout"}
        finally:
            tree.stop()


class ReleaseController:
    PROTECTED = {"supervise.py"}  # Minimal bootstrap; independent audit/tests stay external.


    def __init__(self, source: Path, control: Path, worker):
        self.source, self.control, self.worker = source.resolve(), control.resolve(), worker
        self.control.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.control / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.path = self.control / "controller.sqlite3"
        with self._db() as db, db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,manifest TEXT,phase TEXT,report TEXT);
            CREATE TABLE IF NOT EXISTS active(capability TEXT PRIMARY KEY,candidate_id TEXT,generation INTEGER,previous_id TEXT,health_samples INTEGER);
            CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY,capability TEXT,candidate_id TEXT,phase TEXT,at REAL);
            CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY,candidate_id TEXT,phase TEXT,changed_path TEXT,old_digest TEXT,new_digest TEXT,updated REAL,error TEXT);
            """)
            if 'origin' not in {r[1] for r in db.execute('PRAGMA table_info(deployments)')}:
                db.execute("ALTER TABLE deployments ADD COLUMN origin TEXT NOT NULL DEFAULT '{}'")

    def _db(self):
        return closing(sqlite3.connect(self.path, timeout=10))

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _files(self):
        files = list(self.source.glob("*.py"))
        for folder, pattern in (("life", "*.py"),('life','*.cjs'),('life','*.json'),('tests','*.py'), ("skills", "*.md"), ("audit", "*.py"), ("audit", "*.json")):
            files.extend((self.source / folder).rglob(pattern))
        for path in files:
            if path.is_symlink() or self.source not in path.resolve().parents:
                raise ValueError("source_symlink_not_allowed")
        return sorted(set(files))

    def prepare(self, proposal_id, patch):
        from .release_bundle import prepare
        return prepare(self,proposal_id,patch)

    def _candidate(self, identity):
        if not re.fullmatch(r"candidate-[a-f0-9]{32}", identity):
            raise ValueError("invalid_candidate_id")
        with self._db() as db:
            row = db.execute("SELECT manifest,phase,report FROM candidates WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("unknown_candidate")
        return json.loads(row[0]), row[1], json.loads(row[2])

    def _verify(self, identity, manifest, *, source=True):
        for kind in ("baseline", "candidate"):
            root = self.artifacts / identity / kind
            actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
                      and "__pycache__" not in p.relative_to(root).parts
                      and p.relative_to(root).parts[0] != "data"}
            expected = dict(manifest[kind], **manifest.get("fixtures", {}))
            if actual != set(expected):
                raise ValueError("artifact_file_set_changed")
            for key, digest in expected.items():
                path = root / key
                if path.is_symlink() or self.digest(path) != digest:
                    raise ValueError("immutable_artifact_changed")
        if source:
            current = {p.relative_to(self.source).as_posix(): self.digest(p) for p in self._files()}
            if current != manifest["baseline"]:
                raise ValueError("production_base_changed")

    def evaluate(self, identity, probes, *, max_cost_ratio=1.5, origin=None):
        if max_cost_ratio <= 0:
            raise ValueError("invalid_cost_limit")
        manifest, _phase, _report = self._candidate(identity)
        self._verify(identity, manifest)
        if not probes:
            report = {"verdict": "evidence_pending", "reason": "no_independent_probes"}
        elif not self.worker.available():
            report = {"verdict": "evidence_pending", "reason": "isolation_unavailable"}
        else:
            results = []
            for probe in probes:
                pair = {}
                for kind in ("baseline", "candidate"):
                    try:
                        value, elapsed = self.worker.invoke(self.artifacts / identity / kind,
                                                            probe.module, probe.function, probe.arguments)
                        pair[kind] = {"passed": value == probe.expected, "seconds": elapsed}
                    except Exception as exc:
                        pair[kind] = {"passed": False, "error": type(exc).__name__}
                pair["critical"] = probe.critical
                results.append(pair)
            regressions = any(r["baseline"]["passed"] and not r["candidate"]["passed"] for r in results)
            fixes = any(not r["baseline"]["passed"] and r["candidate"]["passed"] for r in results)
            critical = all(r["candidate"]["passed"] for r in results if r["critical"])
            cost_ok = all(r["candidate"].get("seconds", 0) <= max(0.05, r["baseline"].get("seconds", 0)) * max_cost_ratio
                          for r in results if r["baseline"]["passed"] and r["candidate"]["passed"])
            report = {"verdict": "publishable" if fixes and critical and not regressions and cost_ok else "rejected",
                      "fixes": fixes, "critical": critical, "regressions": regressions, "cost_ok": cost_ok,
                      "results": results, "entrypoints": sorted(set(p.module + ":" + p.function for p in probes)),
                      "backend": type(self.worker).__name__,
                      "probe_digest": hashlib.sha256(json.dumps([asdict(p) for p in probes], sort_keys=True).encode()).hexdigest()}
            if not fixes and critical and not regressions:
                report["verdict"] = "inconclusive"
            if report["verdict"] == "publishable" and (self.source / "bot.py").exists():
                if not hasattr(self.worker, "run_suite"):
                    report["verdict"] = "evidence_pending"
                else:
                    report["baseline_suite"] = self.worker.run_suite(self.artifacts / identity / "baseline")
                    report["candidate_suite"] = self.worker.run_suite(self.artifacts / identity / "candidate")
                    if not report["baseline_suite"]["ok"] or not report["candidate_suite"]["ok"]:
                        report["verdict"] = "rejected"
        if report.get('verdict')=='publishable' and manifest.get('migrations'):
            from .release_migrations import preflight
            try:
                report['migration_check'] = preflight((origin or {}).get('database'),manifest['migrations'])
            except Exception as error:
                report.update(verdict='rejected',migration_check=dict(ok=False,error=str(error)))
        self._verify(identity, manifest)
        with self._db() as db, db:
            db.execute("UPDATE candidates SET phase=?,report=? WHERE id=?", (report["verdict"], json.dumps(report), identity))
        return report

    def activate(self, capability, identity):
        manifest, phase, _report = self._candidate(identity)
        self._verify(identity, manifest)
        if phase != "publishable":
            raise PermissionError("candidate_not_publishable")
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT candidate_id,generation FROM active WHERE capability=?", (capability,)).fetchone()
            if old and old[0] == identity:
                return old[1]
            previous, generation = (old[0], old[1] + 1) if old else ("", 1)
            db.execute("INSERT INTO journal(capability,candidate_id,phase,at) VALUES(?,?,?,?)",
                       (capability, identity, "prepared", time.time()))
            db.execute("INSERT OR REPLACE INTO active VALUES(?,?,?,?,0)", (capability, identity, generation, previous))
            db.execute("INSERT INTO journal(capability,candidate_id,phase,at) VALUES(?,?,?,?)",
                       (capability, identity, "evidence_pending", time.time()))
        return generation

    def invoke(self, capability, module, function, arguments):
        with self._db() as db:
            row = db.execute("SELECT candidate_id,generation,previous_id FROM active WHERE capability=?", (capability,)).fetchone()
        if row is None:
            raise ValueError("capability_not_active")
        identity, generation, previous = row
        manifest, _phase, _report = self._candidate(identity)
        if module + ":" + function not in _report.get("entrypoints", []):
            raise PermissionError("unverified_worker_entrypoint")
        self._verify(identity, manifest, source=False)
        try:
            result, _cost = self.worker.invoke(self.artifacts / identity / "candidate", module, function, arguments)
        except Exception:
            with self._db() as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE active SET candidate_id=?,generation=generation+1,previous_id='',health_samples=0 "
                           "WHERE capability=? AND generation=?", (previous, capability, generation))
                if not previous:
                    db.execute("DELETE FROM active WHERE capability=? AND candidate_id=''", (capability,))
                db.execute("INSERT INTO journal(capability,candidate_id,phase,at) VALUES(?,?,?,?)",
                           (capability, identity, "rolled_back", time.time()))
            raise
        with self._db() as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT generation,health_samples FROM active WHERE capability=?", (capability,)).fetchone()
            if current is None or current[0] != generation:
                raise RuntimeError("release_generation_changed_discard_result")
            samples = current[1] + 1
            db.execute("UPDATE active SET health_samples=? WHERE capability=?", (samples, capability))
            if samples == 3:
                db.execute("INSERT INTO journal(capability,candidate_id,phase,at) VALUES(?,?,?,?)",
                           (capability, identity, "observed", time.time()))
        return result

    def request_publish(self, identity,origin=None):
        manifest, phase, _report = self._candidate(identity)
        self._verify(identity, manifest)
        if phase != "publishable":
            raise PermissionError("candidate_not_publishable")
        relative = manifest["modified"]
        with self._db() as db, db:
            db.execute("INSERT OR IGNORE INTO deployments(id,candidate_id,phase,changed_path,old_digest,new_digest,updated,error,origin) "
                       "VALUES(?,?,'queued',?,?,?,?, '',?)",
                       (identity, identity, relative, manifest["baseline"].get(relative),
                        manifest["candidate"].get(relative), time.time(),json.dumps(origin or {})))
        return identity

    def deployment(self, identity):
        with self._db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM deployments WHERE id=?", (identity,)).fetchone()
        return dict(row) if row else None

    def deployment_phase(self, identity, phase, error=""):
        with self._db() as db, db:
            db.execute("UPDATE deployments SET phase=?,updated=?,error=? WHERE id=?", (phase, time.time(), error[:200], identity))
            db.execute("INSERT INTO journal(capability,candidate_id,phase,at) VALUES('main',?,?,?)",
                       (identity, phase, time.time()))

    def queued_deployments(self):
        with self._db() as db:
            rows = list(db.execute("SELECT id FROM deployments WHERE phase IN ('queued','prepared','installed','observing') ORDER BY updated"))
        result = []
        for row in rows:
            state,reason = self.publication_state(row[0])
            if state=='cancelled':
                if self.deployment(row[0])['phase']=='prepared':
                    self.rollback_source(row[0])
                self.deployment_phase(row[0],'cancelled',reason)
            elif state=='eligible':
                result.append(row[0])
        return result

    def publication_state(self,identity):
        from .release_origin import guard
        deployment = self.deployment(identity)
        if deployment['phase'] in {'installed','observing','observed'}:
            return 'eligible','already_installed'
        with guard(json.loads(deployment.get('origin') or '{}')) as result:
            return result[:2]

    @staticmethod
    def _replace_file(target, content):
        import tempfile
        descriptor, name = tempfile.mkstemp(prefix=".life-publish-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def install_source(self, identity):
        from .release_origin import guard
        deployment = self.deployment(identity)
        origin = json.loads(deployment.get('origin') or '{}')
        if deployment['phase']=='installed':
            origin = dict(kind='operator',database=origin.get('database'))
        with guard(origin,write=True) as (state,reason,database):
            if state!='eligible':
                if state=='cancelled':
                    self.deployment_phase(identity,'cancelled',reason)
                raise PermissionError('publication_'+state+':'+reason)
            return self._install_source(identity,database)

    def _install_source(self, identity,database=None):
        from .release_bundle import install
        return install(self,identity,database)

    def rollback_source(self, identity):
        from .release_bundle import rollback
        return rollback(self,identity)

    def controller_changed(self,identity):
        from .release_bundle import paths
        manifest,_,_ = self._candidate(identity)
        core = {'life/release.py','life/release_bundle.py','life/release_origin.py',
                'life/release_migrations.py','life/supervisor.py','lifesupervisor.py'}
        return bool(core.intersection(paths(manifest)))
