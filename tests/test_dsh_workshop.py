"""DSH 自修改闭环的离线验收：用假桥验补丁、验收、回滚三段。

真机（tsc + 真重启）需要单独验证，这里只保证
逻辑分支对：成功才 applied、类型检查/重启验证不过就按快照恢复。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from life.services.dsh_workshop import DshWorkshop  # noqa: E402
from life.services.store import Store  # noqa: E402


class FakeBridge:
    """只装 DshWorkshop 用到的那几个接口。"""

    def __init__(self, root: Path, tools: list[str] | None = None,
                 fail_restart: bool = False):
        self.dsh_root = root
        self.restarts = 0
        self.fail_restart = fail_restart
        self.tool_names = tools if tools is not None else ["read", "write", "plugin_manager"]

    def _node_exe(self) -> str:
        return sys.executable

    def restart_worker(self) -> None:
        self.restarts += 1
        if self.fail_restart:
            raise RuntimeError("worker 起不来")

    def hello(self) -> dict:
        return {"profile": "sdk", "home": "x", "profileOwnedByBot": True,
                "workerGeneration": 4242}

    def tools(self) -> list[dict]:
        return [{"name": n} for n in self.tool_names]

    def _log_tail(self, n: int = 1000) -> str:
        return "fake log tail"


class WorkshopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.tmp.name)
        self.root = base / "dsh"
        (self.root / "packages" / "demo").mkdir(parents=True)
        self.src = self.root / "packages" / "demo" / "index.ts"
        self.src.write_text("export const a = 1\nexport const b = 2\n", "utf-8")
        self.store = Store(base / "life.db")
        self.cfg = type("Cfg", (), {})()
        self.bridge = FakeBridge(self.root)
        self.shop = DshWorkshop(self.root, self.store, self.bridge, self.cfg)
        self.shop.build = lambda **kwargs: {'ok':True,'output':'fake build; actual build is checked separately'}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def edit(self, old="export const a = 1", new="export const a = 11") -> dict:
        return {"path": "packages/demo/index.ts", "old": old, "new": new}

    def test_success_marks_applied_and_settles_evidence(self) -> None:
        res = self.shop.apply_edits([self.edit()], reason="加一点注释",
                                    run_typecheck=False, activate=True)
        self.assertTrue(res.get("ok"), res)
        self.assertIn("a = 11", self.src.read_text("utf-8"))
        self.assertEqual(self.bridge.restarts, 1)
        rec = self.store.get_dsh_edit(res["edit_id"])
        self.assertEqual(rec.get("status"), "applied")
        self.assertEqual(rec.get("paths"), ["packages/demo/index.ts"])
        self.assertTrue((Path(rec["backup_dir"]) / "packages" / "demo" / "index.ts").is_file())

    def test_activation_failure_restores_everything(self) -> None:
        self.bridge.fail_restart = True
        before = self.src.read_text("utf-8")
        res = self.shop.apply_edits([self.edit()], run_typecheck=False, activate=True)
        self.assertFalse(res.get("ok"))
        self.assertEqual(self.src.read_text("utf-8"), before)
        self.assertEqual(self.store.get_dsh_edit(res["edit_id"]).get("status"),
                         "activation_failed")

    def test_typecheck_failure_never_reaches_activation(self) -> None:
        real = self.shop.typecheck
        self.shop.typecheck = lambda paths, timeout=600: {"ok": False,
                                                          "results": [{"package": "demo",
                                                                       "ok": False,
                                                                       "tail": "TS2304"}]}
        try:
            before = self.src.read_text("utf-8")
            res = self.shop.apply_edits([self.edit()], activate=True)
            self.assertFalse(res.get("ok"))
            self.assertEqual(self.src.read_text("utf-8"), before)
            self.assertEqual(self.bridge.restarts, 0)
            self.assertEqual(self.store.get_dsh_edit(res["edit_id"]).get("status"),
                             "typecheck_failed")
        finally:
            self.shop.typecheck = real

    def test_rollback_restores_file_and_restarts_worker(self) -> None:
        before = self.src.read_text("utf-8")
        res = self.shop.apply_edits([self.edit()], run_typecheck=False, activate=True)
        self.assertTrue(res.get("ok"), res)
        ok, msg = self.shop.rollback(res["edit_id"])
        self.assertTrue(ok, msg)
        self.assertEqual(self.src.read_text("utf-8"), before)
        self.assertEqual(self.bridge.restarts, 2)
        self.assertEqual(self.store.get_dsh_edit(res["edit_id"]).get("status"), "rolled_back")

    def test_protected_and_unknown_paths_are_refused(self) -> None:
        for path in ("node_modules/x/index.ts", "packages/demo/lib/index.ts",
                     "../../etc/passwd", "packages/demo/nope.ts"):
            res = self.shop.apply_edits([{"path": path, "old": "a", "new": "b"}],
                                        run_typecheck=False, activate=False)
            self.assertFalse(res.get("ok"), path)
        self.assertEqual(self.bridge.restarts, 0)

    def test_unmatched_old_text_is_refused_without_writing(self) -> None:
        before = self.src.read_text("utf-8")
        res = self.shop.apply_edits([self.edit(old="这行根本不存在", new="x")],
                                    run_typecheck=False, activate=False)
        self.assertFalse(res.get("ok"))
        self.assertEqual(self.src.read_text("utf-8"), before)

    def test_line_endings_are_never_rewritten(self) -> None:
        """CRLF 的文件改一行再回滚，必须字节级还原（不能被换行翻译改整份文件）。"""
        crlf = self.src.with_name("crlf.ts")
        crlf.write_bytes(b"export const a = 1" + bytes([13, 10])
                         + b"export const b = 2" + bytes([13, 10]))
        before = crlf.read_bytes()
        res = self.shop.apply_edits(
            [{"path": "packages/demo/crlf.ts", "old": "export const a = 1",
              "new": "export const a = 33"}],
            run_typecheck=False, activate=False)
        self.assertTrue(res.get("ok"), res)
        changed = crlf.read_bytes()
        self.assertEqual(changed.count(bytes([13, 10])), 2)
        self.assertNotEqual(changed, before)
        ok, msg = self.shop.rollback(res["edit_id"])
        self.assertTrue(ok, msg)
        self.assertEqual(crlf.read_bytes(), before)

    def test_history_text_lists_edits(self) -> None:
        res = self.shop.apply_edits([self.edit()], run_typecheck=False, activate=True)
        text = self.shop.history_text(5)
        self.assertIn(res["edit_id"], text)
        self.assertIn("applied", text)


if __name__ == "__main__":
    unittest.main()
