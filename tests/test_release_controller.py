"""Actual native candidate processes; controller checks stay outside them."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life.release import NativeWorker, Probe, ReleaseController


class NativeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "worker.py").write_text("def calculate(value):\n    return value - 1\n", encoding="utf-8")
        self.worker = NativeWorker()
        self.controller = ReleaseController(self.source, self.root / "control", self.worker)
        self.patch = {"file": "worker.py", "old": "return value - 1", "new": "return value + 1"}
        self.probes = [Probe("worker", "calculate", {"value": 0}, 1), Probe("worker", "calculate", {"value": 8}, 9)]

    def tearDown(self):
        self.temp.cleanup()

    def test_candidate_is_tested_without_changing_current_source(self):
        identity = self.controller.prepare("proposal", self.patch)
        result = self.controller.evaluate(identity, self.probes, max_cost_ratio=10)
        self.assertEqual("publishable", result["verdict"])
        self.assertIn("value - 1", (self.source / "worker.py").read_text())
        generation = self.controller.activate("calculator", identity)
        self.assertEqual(generation, self.controller.activate("calculator", identity))
        self.assertEqual(5, self.controller.invoke("calculator", "worker", "calculate", {"value": 4}))
        with self.assertRaises(PermissionError):
            self.controller.invoke("calculator", "os", "getcwd", {})

    def test_user_update_is_never_overwritten(self):
        identity = self.controller.prepare("proposal", self.patch)
        (self.source / "worker.py").write_text("def calculate(value):\n    return 17\n")
        with self.assertRaisesRegex(ValueError, "production_base_changed"):
            self.controller.evaluate(identity, self.probes)
        self.assertIn("17", (self.source / "worker.py").read_text())

    def test_candidate_preserves_settings_page_assets(self):
        assets=self.source/'life/settings_ui'
        assets.mkdir(parents=True)
        for name in ('index.html','app.js','style.css'):
            (assets/name).write_text('asset '+name,encoding='utf-8')
        identity=self.controller.prepare('with-settings',self.patch)
        manifest,_,_=self.controller._candidate(identity)
        for name in ('index.html','app.js','style.css'):
            self.assertIn('life/settings_ui/'+name,manifest['candidate'])

    def test_external_oracle_rejects_candidate_false_claims(self):
        patch = dict(self.patch, new="return 'tests passed'")
        identity = self.controller.prepare("proposal", patch)
        result = self.controller.evaluate(identity, self.probes)
        self.assertEqual("rejected", result["verdict"])
        with self.assertRaises(PermissionError):
            self.controller.activate("calculator", identity)

    def test_no_probes_is_evidence_pending(self):
        identity = self.controller.prepare("proposal", self.patch)
        self.assertEqual("evidence_pending", self.controller.evaluate(identity, [])["verdict"])

    def test_failure_rolls_back_code_pointer_without_touching_memory(self):
        data = self.root / "memory.txt"
        data.write_text("new messages and deletion markers")
        identity = self.controller.prepare("proposal", self.patch)
        self.controller.evaluate(identity, self.probes)
        self.controller.activate("calculator", identity)
        with self.assertRaises(RuntimeError):
            self.controller.invoke("calculator", "worker", "calculate", {"value": "invalid"})
        self.assertEqual("new messages and deletion markers", data.read_text())
        with self.assertRaises(ValueError):
            self.controller.invoke("calculator", "worker", "calculate", {"value": 0})

    def test_protected_paths_cannot_be_bypassed_by_dotdot(self):
        (self.source / "life").mkdir()
        (self.source / 'services').mkdir()
        (self.source / "supervise.py").write_text("a=1\n")
        for name in ("supervise.py", 'life/../supervise.py'):
            with self.assertRaises(PermissionError):
                self.controller.prepare("proposal", {"file": name, "old": "a=1", "new": "a=2"})

    def test_post_evaluation_artifact_tampering_is_rejected(self):
        identity = self.controller.prepare("proposal", self.patch)
        self.controller.evaluate(identity, self.probes)
        (self.controller.artifacts / identity / "candidate" / "worker.py").write_text("print('tamper')")
        with self.assertRaisesRegex(ValueError, "immutable_artifact_changed"):
            self.controller.activate("calculator", identity)

    def test_candidate_does_not_inherit_api_environment(self):
        import os
        os.environ["QQ_TEST_PRIVATE_TOKEN"] = "not-for-worker"
        try:
            self.assertNotIn("QQ_TEST_PRIVATE_TOKEN", self.worker.environment())
        finally:
            os.environ.pop("QQ_TEST_PRIVATE_TOKEN")

    def test_source_switch_and_rollback_only_replace_code(self):
        identity = self.controller.prepare("proposal", self.patch)
        self.controller.evaluate(identity, self.probes)
        self.controller.request_publish(identity)
        data = self.source / "data"
        data.mkdir()
        state = data / "state.txt"
        state.write_text("new messages and tombstones")
        self.controller.install_source(identity)
        self.assertIn("value + 1", (self.source / "worker.py").read_text())
        self.controller.rollback_source(identity)
        self.assertIn("value - 1", (self.source / "worker.py").read_text())
        self.assertEqual("new messages and tombstones", state.read_text())

    def test_journal_recovers_crash_after_file_replace(self):
        identity = self.controller.prepare("proposal", self.patch)
        self.controller.evaluate(identity, self.probes)
        self.controller.request_publish(identity)
        self.controller.deployment_phase(identity, "prepared")
        content = (self.controller.artifacts / identity / "candidate" / "worker.py").read_bytes()
        self.controller._replace_file(self.source / "worker.py", content)
        recovered = ReleaseController(self.source, self.root / "control", self.worker)
        recovered.install_source(identity)
        self.assertEqual("installed", recovered.deployment(identity)["phase"])

    def test_supervisor_startup_failure_restores_previous_code(self):
        from life.supervisor import HostSupervisor
        import time
        identity = self.controller.prepare("proposal", self.patch)
        self.controller.evaluate(identity, self.probes)
        self.controller.request_publish(identity)
        supervisor = HostSupervisor(self.controller, self.source / "config.toml")
        try:
            supervisor.publish(identity)
            supervisor.tree.process.wait(timeout=5)  # Fixture has no bot.py.
            self.assertEqual("rolled_back", supervisor.tick())
            self.assertIn("value - 1", (self.source / "worker.py").read_text())
        finally:
            supervisor.stop()

    def test_native_process_tree_recognizes_python_child(self):
        import os
        import sys
        from life.supervisor import ProcessTree
        child = "import os,time; print(os.getpid(),flush=True); time.sleep(30)"
        parent = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); p.wait()"
        tree = ProcessTree([sys.executable, "-c", parent], cwd=self.source, env=dict(os.environ), pipes=True)
        try:
            pid = int(tree.process.stdout.readline())
            self.assertTrue(tree.owns_pid(pid))
            self.assertFalse(tree.owns_pid(os.getpid()))
        finally:
            tree.stop()
            for stream in (tree.process.stdin, tree.process.stdout, tree.process.stderr):
                stream.close()


if __name__ == "__main__":
    unittest.main()
