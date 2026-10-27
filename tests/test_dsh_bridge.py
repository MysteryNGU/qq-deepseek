"""桥接真机行为的离线验收：用 tests/dsh_stub.py 扮演 DSH 协议面。

覆盖三处必须补齐的行为：操作编号落盘后可对账、cancel 等工具真停才确认、
慢执行期间 status / tools / cancel 仍能进（读写分离）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from life.services.dsh_bridge import DshBridge, DshBridgeError, request_hash  # noqa: E402
from life.services.store import Store  # noqa: E402

STUB = Path(__file__).with_name("dsh_stub.py")


class StubBridge(DshBridge):
    """把子进程换成 Python 假服务端，其余逻辑全是生产代码。"""

    def _command(self) -> list[str]:
        return [sys.executable, "-u", str(STUB)]

    def _node_exe(self):
        return sys.executable  # The fixture launches Python, not a Node profile.

    def _pnpm_entry(self):
        return ''


class BridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.tmp.name)
        self.home = base / "dsh-home"
        fake_source = base/'dsh-source'
        for relative in ('apps/cli/src/bin.ts','node_modules/tsx/package.json'):
            path = fake_source/relative
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('{}',encoding='utf-8')
        self.count = base / "stub-count.txt"
        os.environ["STUB_COUNT"] = str(self.count)
        os.environ["STUB_SLOW_MS"] = "1200"
        os.environ["STUB_OWNED"] = "1"
        self.store = Store(base / "life.db")
        self.cfg = type("Cfg", (), {
            "dsh_root": str(fake_source),
            "dsh_home": str(self.home), "dsh_timeout": 60.0,
        })()
        self.bridges: list[DshBridge] = []

    def tearDown(self) -> None:
        for bridge in self.bridges:
            bridge.close()
        self.tmp.cleanup()

    def new_bridge(self) -> DshBridge:
        bridge = StubBridge(self.cfg, ROOT, ledger=self.store)
        self.bridges.append(bridge)
        return bridge

    def dispatches(self, op: str) -> int:
        if not self.count.is_file():
            return 0
        return sum(1 for line in self.count.read_text(encoding="utf-8").splitlines()
                   if line.split("\t")[0] == op)

    def test_A10_bridge_bundle_does_not_overwrite_plugin_profile_configuration(self):
        import json
        bridge=self.new_bridge();bridge.ensure_profile()
        profile=bridge.profile_dir();patch=profile/'cordis.patch.yml'
        custom='- insert:\n    - id: ordinary-plugin\n      name: ordinary-plugin\n      config:\n        setting: retained\n'
        # Upgrade from the exact former managed patch plus a plugin-owned addition.
        old=(profile/'qq-life-core/cordis.patch.yml').read_text('utf-8')
        patch.write_text(old+custom,encoding='utf-8')
        bridge.ensure_profile()
        self.assertEqual(patch.read_text('utf-8'),custom)
        manifest=profile/'package.json';value=json.loads(manifest.read_text('utf-8'))
        value['dsh']['profile']['bundles'].append('ordinary-bundle')
        value['dependencies']['ordinary-bundle']='1.0.0'
        manifest.write_text(json.dumps(value),encoding='utf-8')
        bridge.ensure_profile();bridge.ensure_profile()
        self.assertEqual(patch.read_text('utf-8'),custom)
        current=json.loads(manifest.read_text('utf-8'))
        self.assertEqual(current['dependencies']['ordinary-bundle'],'1.0.0')
        self.assertEqual(current['dsh']['profile']['bundles'].count('@qq-life/bridge-core'),1)
        self.assertIn('ordinary-bundle',current['dsh']['profile']['bundles'])

    def test_A10_empty_profile_remains_a_valid_loader_patch_list(self):
        import json
        bridge=self.new_bridge();bridge.ensure_profile()
        profile=bridge.profile_dir();patch=profile/'cordis.patch.yml'
        self.assertEqual(json.loads(patch.read_text('utf-8')),[])

        patch.write_text((profile/'qq-life-core/cordis.patch.yml').read_text('utf-8'),encoding='utf-8')
        bridge.ensure_profile()
        self.assertEqual(json.loads(patch.read_text('utf-8')),[])

    def test_compaction_uses_configured_budgets_without_replacing_plugin_overrides(self):
        self.cfg.resource_policy=dict(compaction_threshold_percent=5,compaction_retain_tokens=12000,
                                      compaction_headroom_tokens=6000,handoff_target_tokens=3000)
        bridge=self.new_bridge();bridge.ensure_profile()
        profile=bridge.profile_dir()
        override='- id: compaction-basic\n  config:\n    thresholdRatio: 0.09\n    retainTokens: 20000\n'
        (profile/'cordis.patch.yml').write_text(override,encoding='utf-8')
        bridge.ensure_profile()
        core=(profile/'qq-life-core/cordis.patch.yml').read_text('utf-8')
        for text in ('auto: true','thresholdRatio: 0.05','retainTokens: 12000','headroomTokens: 6000','maxTokens: 3000'):
            self.assertIn(text,core)
        self.assertEqual((profile/'cordis.patch.yml').read_text('utf-8'),override)
        self.assertEqual(bridge._hello_parameters()['compact_tool_protocol'],1)
        source=bridge.dsh_root/'packages/sdk/server/src/qq-life-context.ts'
        source.parent.mkdir(parents=True,exist_ok=True);source.write_text('// new context protocol',encoding='utf-8')
        bridge.ensure_profile()
        self.assertIn('auto: false',(profile/'qq-life-core/cordis.patch.yml').read_text('utf-8'))

    def test_profile_recovers_pnpm_core_link_without_changing_installed_copy(self):
        import json
        bridge=self.new_bridge();bridge.ensure_profile()
        profile=bridge.profile_dir();installed=profile/'node_modules/@qq-life/bridge-core'
        store=profile/'node_modules/.pnpm/@qq-life+bridge-core@file+qq-life-core/node_modules/@qq-life/bridge-core'
        self.assertTrue(installed.resolve().is_relative_to(self.home.resolve()))
        self.assertTrue(store.resolve().is_relative_to(self.home.resolve()))
        store.parent.mkdir(parents=True)
        installed.rename(store)
        old=(store/'cordis.patch.yml').read_bytes()
        if os.name=='nt':
            import _winapi
            _winapi.CreateJunction(str(store),str(installed))
        else:
            installed.symlink_to(store,target_is_directory=True)
        manifest=profile/'package.json'
        manifest.write_text(manifest.read_text('utf-8'),encoding='utf-8-sig')
        bridge.ensure_profile();bridge.ensure_profile()
        self.assertEqual(installed.resolve(),(profile/'qq-life-core').resolve())
        self.assertEqual((store/'cordis.patch.yml').read_bytes(),old)
        self.assertEqual(json.loads(manifest.read_text('utf-8'))['dependencies']['@qq-life/bridge-core'],'link:./qq-life-core')

    def test_profile_still_rejects_foreign_core_links(self):
        bridge=self.new_bridge();bridge.ensure_profile()
        installed=bridge.profile_dir()/'node_modules/@qq-life/bridge-core'
        foreign=Path(self.tmp.name)/'foreign-core'
        self.assertTrue(installed.resolve().is_relative_to(self.home.resolve()))
        installed.rename(foreign)
        if os.name=='nt':
            import _winapi
            _winapi.CreateJunction(str(foreign),str(installed))
        else:
            installed.symlink_to(foreign,target_is_directory=True)
        with self.assertRaisesRegex(DshBridgeError,'链接不属于'):
            bridge.ensure_profile()
        self.assertEqual(installed.resolve(),foreign.resolve())

    def test_handshake_requires_real_ownership(self) -> None:
        os.environ["STUB_OWNED"] = "0"
        bridge = self.new_bridge()
        with self.assertRaises(DshBridgeError) as caught:
            bridge.hello()
        self.assertTrue(str(caught.exception), "身份核验失败必须给出原因")

    def test_retired_session_copies_purged_without_other_sessions(self):
        import json
        bridge = self.new_bridge()
        bridge.hello()
        sessions = self.home/'sessions/project'
        for sid in ('role-owned','role-other'):
            folder = sessions/sid
            folder.mkdir(parents=True)
            (folder/'log.jsonl').write_text('test content',encoding='utf-8')
        admissions = self.home/'qq-life/inputs.jsonl'
        admissions.parent.mkdir()
        admissions.write_text('\n'.join(json.dumps(dict(sessionId=sid)) for sid in ('role-owned','role-other'))+'\n',encoding='utf-8')
        result = bridge.purge_sessions(['role-owned'])
        self.assertEqual(result['directories'],1)
        self.assertFalse((sessions/'role-owned').exists())
        self.assertTrue((sessions/'role-other/log.jsonl').is_file())
        self.assertEqual(json.loads(admissions.read_text('utf-8'))['sessionId'],'role-other')

    def test_operation_id_persists_and_replays(self) -> None:
        first = self.new_bridge()
        snap = first.execute("read_file", {"path": "a.txt"}, operation_id="op-keep")
        self.assertEqual(snap.get("status"), "result_recorded", snap.get("error"))
        row = self.store.bridge_op_get("op-keep")
        self.assertIsNotNone(row)
        self.assertEqual(row.get("status"), "result_recorded")
        self.assertEqual(row.get("request_hash"), request_hash("read_file", {"path": "a.txt"}))
        first.close()

        second = self.new_bridge()
        again = second.execute("read_file", {"path": "a.txt"}, operation_id="op-keep")
        self.assertEqual(again.get("status"), "result_recorded")
        self.assertEqual(self.dispatches("op-keep"), 1, "重启后不应重复执行同一编号")
        with self.assertRaises(DshBridgeError):
            second.execute("read_file", {"path": "b.txt"}, operation_id="op-keep")
        self.assertEqual(self.dispatches("op-keep"), 1)

    def test_slow_execution_keeps_bridge_responsive(self) -> None:
        bridge = self.new_bridge()
        bridge.hello()
        done: dict[str, dict] = {}

        def run_slow() -> None:
            done["snap"] = bridge.execute("slow_tool", {}, operation_id="op-slow")

        worker = threading.Thread(target=run_slow, daemon=True)
        worker.start()
        time.sleep(0.3)
        started = time.time()
        names = [row.get("name") for row in bridge.tools()]
        self.assertIn("slow_tool", names)
        pending = bridge.status("op-slow")
        self.assertFalse(pending.get("settled"), "执行中不应算已结束")
        self.assertLess(time.time() - started, 0.9, "慢执行期间 tools/status 必须能进")
        cancelled = bridge.cancel("op-slow")
        self.assertTrue(cancelled.get("settled"), "取消只有在工具真停后才确认")
        self.assertEqual(cancelled.get("status"), "cancelled")
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(done["snap"].get("status"), "cancelled")


if __name__ == "__main__":
    unittest.main()
