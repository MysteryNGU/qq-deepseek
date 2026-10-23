"""假的 DSH SDK 服务端：说同一套 JSON-RPC，供桥接测试用。

真实 DSH 在沙箱里写不了 profile 配置，这里用 Python 复刻协议面的关键行为：
并发处理请求（慢工具执行期间仍能应答 cancel/status）、cancel 只在工具真停后
才回 settled、每次 execute 都记到计数文件，方便断言“没有重复执行”。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

COUNT = Path(os.environ.get("STUB_COUNT", "stub-count.txt"))
SLOW_MS = int(os.environ.get("STUB_SLOW_MS", "1500"))
LOCK = threading.Lock()
CANCELLED: set[str] = set()
FINISHED: dict[str, bool] = {}


def emit(payload: dict) -> None:
    with LOCK:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def bump(op: str, tool: str) -> None:
    with LOCK:
        with COUNT.open("a", encoding="utf-8") as handle:
            handle.write(f"{op}\t{tool}\n")


def reply(rid, result=None, error=None) -> None:
    if error is not None:
        emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": error}})
    else:
        emit({"jsonrpc": "2.0", "id": rid, "result": result})


def run_execute(rid, params: dict) -> None:
    op = str(params.get("operationId"))
    tool = str(params.get("tool"))
    bump(op, tool)
    if tool == "slow_tool":
        deadline = time.time() + SLOW_MS / 1000.0
        while time.time() < deadline and op not in CANCELLED:
            time.sleep(0.02)
        with LOCK:
            FINISHED[op] = True
        stopped = op in CANCELLED
        reply(rid, {"operationId": op, "status": "cancelled" if stopped else "result_recorded",
                    "settled": True, "tool": tool,
                    "error": "已停止执行" if stopped else None})
        return
    with LOCK:
        FINISHED[op] = True
    reply(rid, {"operationId": op, "status": "result_recorded", "settled": True,
                "tool": tool, "result": {"content": f"stub:{tool}"}})


def run_cancel(rid, params: dict) -> None:
    op = str(params.get("operationId"))
    with LOCK:
        CANCELLED.add(op)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        with LOCK:
            if FINISHED.get(op):
                break
        time.sleep(0.02)
    with LOCK:
        done = FINISHED.get(op, False)
    reply(rid, {"operationId": op, "status": "cancelled" if done else "stopping",
                "settled": done, "tool": "slow_tool"})


def handle(msg: dict) -> None:
    method = str(msg.get("method") or "")
    params = msg.get("params") or {}
    rid = msg.get("id")
    if method == "initialize":
        reply(rid, {"serverInfo": {"name": "dsh-stub", "version": "test"}})
    elif method == "qq_life/hello":
        reply(rid, {"protocol": "qq-life/1", "relation_protocol": 2, "resource_protocol": 1, "profile": "sdk", "profileDir": None,
                    "home": os.environ.get("QQ_LIFE_HOME", ""),
                    "profileOwnedByBot": os.environ.get("STUB_OWNED", "1") == "1",
                    "workerGeneration": os.getpid(), "startedAt": time.time()})
    elif method == "qq_life/tools/list":
        reply(rid, {"tools": [{"name": "slow_tool"}, {"name": "read_file"}], "nextCursor": None})
    elif method == "qq_life/execute":
        threading.Thread(target=run_execute, args=(rid, params), daemon=True).start()
    elif method == "qq_life/cancel":
        threading.Thread(target=run_cancel, args=(rid, params), daemon=True).start()
    elif method == "qq_life/status":
        op = str(params.get("operationId"))
        with LOCK:
            done = FINISHED.get(op, False)
        reply(rid, {"operationId": op, "status": "result_recorded" if done else "dispatching",
                    "settled": done, "tool": "slow_tool"})
    elif method == "shutdown":
        reply(rid, {})
        os._exit(0)
    else:
        reply(rid, error=f"unknown method: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001 - stub keeps running
            sys.stderr.write(f"stub: {exc}\n")


if __name__ == "__main__":
    main()
