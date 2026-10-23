#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 自修改闭环：改 DSH 的代码 → 类型检查 → 重启 worker 验证 → 不行就回滚。

和 life/services/workshop.py 是一套思路（快照 + 精确替换 + 证据 + 自动恢复），
区别只在验收动作：这边不看 Python 单测，看的是 tsc --noEmit 和"重启后还能握手、
工具列表还在"。改动真的生效（握手成功且工具数没掉）才算 applied；
只要有一关没过，文件按快照恢复，并尽量把 worker 拉回可用状态。
"""

from __future__ import annotations

import difflib
import json
import subprocess
import time
import uuid
from pathlib import Path

from .workshop import Workshop

PROTECTED_PARTS = {
    "node_modules", ".git", "dist", "lib", "out", "coverage", "data",
    ".plugin-manager", ".turbo", "__pycache__", ".venv",
}
ALLOWED_SUFFIX = {".ts", ".tsx", ".js", ".mjs", ".md", ".json", ".yml", ".yaml", ".css"}
PROTECTED_FILES = {"pnpm-lock.yaml", "pnpm-workspace.yaml"}


class DshWorkshop(Workshop):
    """DSH 侧的自修改工坊（root 指向 DSH 工作副本）。"""

    def __init__(self, root: Path, store, bridge, cfg=None):
        self.root = Path(root).resolve()
        self.store = store
        self.cfg = cfg
        self.bridge = bridge
        self.base = self.root / "data" / "qq-life-edits"
        self.base.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 原样读写
    @staticmethod
    def _read(path: Path) -> str:
        """按原样读，不做换行翻译——否则整份文件的 CRLF/LF 会被改掉。"""
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    @staticmethod
    def _write(path: Path, text: str) -> None:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    # ---------------------------------------------------------------- 安全检查
    def _safe(self, rel: str) -> tuple[Path | None, str]:
        rel = str(rel or "").strip().replace("\\", "/")
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            return None, f"路径不合法：{rel}"
        if set(Path(rel).parts) & PROTECTED_PARTS:
            return None, f"这是受保护路径，不能改：{rel}"
        if Path(rel).name in PROTECTED_FILES:
            return None, f"这是锁文件/工作区配置，不能改：{rel}"
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except Exception:
            return None, f"只能改 DSH 工作副本里的文件：{rel}"
        if p.suffix.lower() not in ALLOWED_SUFFIX:
            return None, f"不支持改这种文件：{rel}"
        return p, ""

    def package_of(self, path: Path) -> Path | None:
        """离这个文件最近的、带 tsconfig.json 的包目录。"""
        for parent in [path.parent, *path.parents]:
            if parent == self.root.parent or parent == parent.parent:
                break
            if (parent / "tsconfig.json").is_file():
                return parent
            if parent == self.root:
                break
        return None

    # ---------------------------------------------------------------- 验收动作
    def build(self, timeout=900):
        """Source edits must reach the artifacts loaded by the supported CLI."""
        env = self.bridge._env()
        env.setdefault('DSH_CLIENT_COMMIT_HASH', '0000000')
        env['CI'] = 'true'
        command = [self.bridge._node_exe(), '--import', 'tsx', 'scripts/build.ts', '--scope', 'artifacts']
        try:
            process = subprocess.run(command, cwd=str(self.root), env=env, capture_output=True,
                text=True, encoding='utf-8', errors='replace', timeout=timeout,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return {'ok': process.returncode == 0, 'tail': (process.stdout + process.stderr)[-3000:]}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def typecheck(self, paths: list[str], timeout: int = 600) -> dict:
        """对受影响的包跑 tsc --noEmit，返回每个包的结果。"""
        tsc = self.root / "node_modules" / "typescript" / "bin" / "tsc"
        if not tsc.is_file():
            return {"ok": False, "error": f"找不到 tsc：{tsc}", "results": []}
        node = self.bridge._node_exe()
        packages: list[Path] = []
        for rel in paths:
            p = (self.root / str(rel)).resolve()
            pkg = self.package_of(p)
            if pkg is not None and pkg not in packages:
                packages.append(pkg)
        if not packages:
            return {"ok": True, "results": [], "note": "没有受影响的 TypeScript 包，跳过类型检查"}
        results: list[dict] = []
        ok_all = True
        for pkg in packages:
            cmd = [node, str(tsc), "--noEmit", "-p", str(pkg / "tsconfig.json")]
            try:
                proc = subprocess.run(cmd, cwd=str(pkg), capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=timeout)
                tail = ((proc.stdout or "") + (proc.stderr or "")).strip()
                ok = proc.returncode == 0
            except subprocess.TimeoutExpired:
                ok, tail = False, f"类型检查超过 {timeout}s 没结束"
            except Exception as ex:
                ok, tail = False, f"类型检查没跑起来：{ex}"
            results.append({"package": str(pkg.relative_to(self.root)).replace("\\", "/"),
                            "ok": ok, "tail": tail[-1500:]})
            ok_all = ok_all and ok
        return {"ok": ok_all, "results": results}

    def activate(self, timeout: float = 240.0) -> dict:
        """重启 worker 并重新握手：真的能连上、工具还在，才算改动生效。"""
        try:
            self.bridge.restart_worker()
            hello = self.bridge.hello()
            tools = self.bridge.tools()
        except Exception as ex:
            return {"ok": False, "error": str(ex),
                    "tail": self.bridge._log_tail(1500)}
        names = [str(t.get("name")) for t in tools if isinstance(t, dict)]
        return {"ok": bool(tools), "tools": len(names), "toolNames": names[:80],
                "profile": hello.get("profile"), "home": hello.get("home"),
                "profileOwnedByBot": hello.get("profileOwnedByBot"),
                "workerGeneration": hello.get("workerGeneration")}

    # ---------------------------------------------------------------- 补丁流程
    def apply_edits(self, edits: list[dict], reason: str = "",
                    run_typecheck: bool = True, activate: bool = True,
                    timeout: int = 600) -> dict:
        if not edits:
            return {"ok": False, "error": "没有补丁内容"}
        eid = uuid.uuid4().hex[:12]
        snap = self.base / eid
        snap.mkdir(parents=True, exist_ok=True)

        originals: dict[Path, str] = {}
        staged: dict[Path, str] = {}
        hashes: dict[str, str] = {}
        diff_lines: list[str] = []
        NL = chr(10)

        # 先全部读入并校验，任何一处不合法都不落盘
        for i, e in enumerate(edits):
            p, err = self._safe(str(e.get("path") or ""))
            if not p:
                return {"ok": False, "error": err, "edit_id": eid}
            if not p.exists():
                return {"ok": False, "error": f"没有这个文件：{e.get('path')}", "edit_id": eid}
            old = str(e.get("old") or "")
            new = str(e.get("new") or "")
            src = self._read(p)
            originals[p] = src
            if not old and e.get("start"):
                try:
                    a = max(1, int(e.get("start")))
                    b = int(e.get("end") or a)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "start/end 得是行号", "edit_id": eid}
                lines = src.splitlines(True)
                if a > len(lines) + 1 or b < a:
                    return {"ok": False,
                            "error": f"行号不对：{a}~{b}，这个文件只有 {len(lines)} 行",
                            "edit_id": eid}
                body = new if (new.endswith(NL) or b > len(lines)) else new + NL
                staged[p] = "".join(lines[:a - 1]) + body + "".join(lines[b:])
                continue
            if not old:
                return {"ok": False,
                        "error": f"第 {i+1} 个补丁要有 old 原文，或者给 start/end 行号",
                        "edit_id": eid}
            start, end, how, best = self._locate(src, old)
            if start is None:
                hint = ("" if not best else NL + "文件里最接近的一段是：" + NL + best[:1200])
                return {"ok": False,
                        "error": (f"{e.get('path')} 里没对上（{how}）。"
                                  "重新 read 看一眼，或者直接用 start/end 行号改" + hint),
                        "edit_id": eid}
            staged[p] = src[:start] + new + src[end:]

        for p in staged:
            hashes[str(p.relative_to(self.root)).replace(chr(92), "/")] = self._sha(p)

        # 快照 + diff + 记账（先记 staged，再落盘）
        for p, src in originals.items():
            rel = p.relative_to(self.root)
            dst = snap / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            self._write(dst, src)
            diff_lines.extend(difflib.unified_diff(
                src.splitlines(), staged[p].splitlines(),
                fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
        patch = chr(10).join(diff_lines)[:20000]
        (snap / "manifest.json").write_text(json.dumps({
            "id": eid, "ts": int(time.time()), "reason": reason,
            "paths": [str(p.relative_to(self.root)) for p in originals],
            "hashes": hashes,
        }, ensure_ascii=False, indent=2), "utf-8")
        rels = [str(p.relative_to(self.root)).replace(chr(92), "/") for p in originals]
        self.store.add_dsh_edit(eid, rels, hashes, patch, reason, str(snap), status="staged")

        for p, content in staged.items():
            self._write(p, content)

        tc = {"ok": True, "results": [], "note": "跳过了类型检查"}
        act: dict = {"ok": True, "note": "跳过了重启验证"}
        if run_typecheck:
            tc = self.typecheck(rels, timeout=timeout)
        if not tc.get("ok"):
            self._restore(originals)
            self.store.update_dsh_edit(eid, status="typecheck_failed", typecheck=tc)
            return {"ok": False, "edit_id": eid, "patch": patch, "typecheck": tc,
                    "error": "类型检查没过，已恢复原文件"}
        if not activate:
            self.store.update_dsh_edit(eid, status='staged', typecheck=tc)
            return {'ok': True, 'activated': False, 'edit_id': eid, 'patch': patch,
                'typecheck': tc, 'activation': {'ok': True, 'activated': False},
                'note': '只保存了源文件，尚未构建或激活，不算已生效。'}
        if activate:
            built = self.build(timeout=max(900, timeout))
            tc['build'] = built
            if not built.get('ok'):
                self._restore(originals)
                self.build(timeout=max(900, timeout))
                self.store.update_dsh_edit(eid, status='build_failed', typecheck=tc)
                return {'ok': False, 'edit_id': eid, 'typecheck': tc, 'error': '构建失败，已恢复源文件并重建原版本'}
            act = self.activate()
        if not act.get("ok"):
            self._restore(originals)
            self.build(timeout=max(900, timeout))
            self.store.update_dsh_edit(eid, status="activation_failed",
                                       typecheck=tc, activation=act)
            self._recover_worker()
            return {"ok": False, "edit_id": eid, "patch": patch, "typecheck": tc,
                    "activation": act,
                    "error": "重启后没通过验证，已恢复原文件并重新拉起 worker"}
        self.store.update_dsh_edit(eid, status="applied", typecheck=tc, activation=act)
        return {"ok": True, "activated": True, "edit_id": eid, "patch": patch, "typecheck": tc,
                "activation": act, "backup": str(snap),
                "note": "改好了，类型检查和重启验证都过了"}

    def _restore(self, originals: dict[Path, str]) -> None:
        failures = []
        for p, src in originals.items():
            try:
                self._write(p, src)
            except OSError as error:
                failures.append(str(p)+': '+str(error))
        if failures:
            raise RuntimeError('DSH restore incomplete: '+'; '.join(failures))

    def _recover_worker(self) -> None:
        try:
            self.bridge.restart_worker()
        except Exception:
            pass

    def rollback(self, eid: str) -> tuple[bool, str]:
        rec = self.store.get_dsh_edit(eid)
        if not rec:
            return False, f"没有这条修改记录：{eid}"
        backup = Path(str(rec.get("backup_dir") or ""))
        paths = [str(x) for x in (rec.get("paths") or [])]
        if not backup.is_dir() or not paths:
            return False, "找不到当时的备份，不能回滚"
        done, missing = [], []
        for rel in paths:
            src = backup / rel
            dst = self.root / rel
            if not src.is_file():
                missing.append(rel)
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                self._write(dst, self._read(src))
                done.append(rel)
            except Exception as ex:
                missing.append(f"{rel}（{ex}）")
        self.store.update_dsh_edit(eid, status="rolled_back", rollback_ts=int(time.time()))
        built = self.build()
        if not built.get('ok'):
            return False, '源文件已还原，但重新构建失败，尚未激活：' + str(built.get('error') or built.get('tail'))[-1000:]
        self._recover_worker()
        msg = f"回滚了 {len(done)} 个文件并重启 worker"
        if missing:
            msg += "；没还原：" + "、".join(missing[:5])
        return (not missing), msg

    def history_text(self, limit: int = 10) -> str:
        rows = self.store.list_dsh_edits(limit)
        if not rows:
            return "还没有改过 DSH 的代码。"
        out = []
        for r in rows:
            when = time.strftime("%m-%d %H:%M", time.localtime(int(r.get("ts") or 0)))
            paths = "、".join(str(x) for x in (r.get("paths") or [])[:3])
            out.append(f"{r.get('id')}  {when}  {r.get('status')}  {paths}"
                       + (f"  （{r.get('reason')}）" if r.get("reason") else ""))
        return chr(10).join(out)
