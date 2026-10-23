"""Passive durable receipts for bridge/workshop component verification.

Production role state is owned by RoleAgent and Repository.
"""
import json
import time
from pathlib import Path
from . import schema

class Store:
    def __init__(self, path: Path, *, repo=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from life.repo import Repository
        self._owns_repo = repo is None
        self.repo = repo if repo is not None else Repository(self.path)
        if self.repo.path.resolve() != self.path.resolve():
            raise ValueError('store_repository_path_mismatch')
        self._lock = self.repo._lock
        self.db = self.repo.db
        schema.init(self.db)

    # ---------------------------------------------------------------- 基础

    def close(self) -> None:
        if not self._owns_repo:
            return
        with self._lock:
            try:
                self.db.close()
            except Exception:
                pass

    _BRIDGE_COLS = ("id", "scope_id", "plan_id", "step_id", "request_hash", "context",
                    "tool_name", "arguments", "status", "worker_generation",
                    "tool_generation", "result", "artifact_refs", "updated_at", "note")

    def bridge_op_get(self, op_id: str) -> dict | None:
        """按操作编号取一条桥接记录；没有就返回 None。"""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM bridge_operations WHERE id=?", (str(op_id),)).fetchone()
        return dict(row) if row else None

    def bridge_op_put(self, rec: dict) -> None:
        """整条写入（先落盘再发请求，用于去重与重启对账）。"""
        cols = self._BRIDGE_COLS
        values = [rec.get(c, "") if rec.get(c) is not None else "" for c in cols]
        for i, c in enumerate(cols):
            if c in ("arguments", "context", "result", "artifact_refs"):
                values[i] = str(values[i]) if values[i] != "" else ""
        with self.repo.tx():
            self.db.execute(
                "INSERT INTO bridge_operations(" + ",".join(cols) + ") "
                "VALUES(" + ",".join("?" for _ in cols) + ") "
                "ON CONFLICT(id) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in cols if c != "id"),
                values)

    def bridge_op_update(self, op_id: str, status: str, *,
                         result: str | None = None, note: str | None = None,
                         worker_generation: str | None = None) -> None:
        """推进一条桥接记录的状态，不覆盖请求指纹。"""
        sets = ["status=?", "updated_at=?"]
        args: list = [str(status), time.time()]
        if result is not None:
            sets.append("result=?")
            args.append(result)
        if note is not None:
            sets.append("note=?")
            args.append(note[:500])
        if worker_generation is not None:
            sets.append("worker_generation=?")
            args.append(worker_generation)
        args.append(str(op_id))
        with self.repo.tx():
            self.db.execute(
                "UPDATE bridge_operations SET " + ", ".join(sets) + " WHERE id=?", args)

    def bridge_op_unsettled(self, limit: int = 50) -> list[dict]:
        """还没落终态、需要重启后对账的操作。"""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM bridge_operations "
                "WHERE status NOT IN ('result_recorded','cancelled','denied','failed_before_dispatch') "
                "ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def bridge_op_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM bridge_operations ORDER BY updated_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def add_dsh_edit(self, eid: str, paths: list[str], base_hashes: dict,
                     patch: str, reason: str, backup_dir: str,
                     status: str = "staged") -> None:
        with self.repo.tx():
            self.db.execute(
                "INSERT INTO dsh_edits(id,ts,paths,base_hashes,patch,status,"
                "backup_dir,reason) VALUES(?,?,?,?,?,?,?,?)",
                (eid, int(time.time()), json.dumps(paths, ensure_ascii=False),
                 json.dumps(base_hashes, ensure_ascii=False), patch[:20000],
                 status, backup_dir, reason[:1000]))

    def update_dsh_edit(self, eid: str, **fields) -> None:
        allowed = {"status", "typecheck", "activation", "reason", "rollback_ts"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("typecheck", "activation"):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            args.append(v)
        if not sets:
            return
        args.append(eid)
        with self.repo.tx():
            self.db.execute(f"UPDATE dsh_edits SET {', '.join(sets)} WHERE id=?", args)

    def get_dsh_edit(self, eid: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM dsh_edits WHERE id=?", (eid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("paths", "base_hashes", "typecheck", "activation"):
            try:
                d[k] = json.loads(d.get(k) or ("{}" if k in ("base_hashes", "typecheck", "activation") else "[]"))
            except Exception:
                d[k] = {} if k in ("base_hashes", "typecheck", "activation") else []
        return d

    def list_dsh_edits(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT id,ts,paths,status,reason,rollback_ts FROM dsh_edits "
                "ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["paths"] = json.loads(d.get("paths") or "[]")
            except Exception:
                d["paths"] = []
            out.append(d)
        return out

