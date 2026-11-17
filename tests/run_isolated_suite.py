"""Baseline regression inside a disposable test home.

Usage (from the repository root)::

    python -B -X utf8 tests/run_isolated_suite.py                     # staged copy, fresh DB
    python -B -X utf8 tests/run_isolated_suite.py --db copy \
        --db-source /var/lib/companion/xiaoyu/life.db                 # read-only production copy
    python -B -X utf8 tests/run_isolated_suite.py --only drivetest --timeout 120
    python -B -X utf8 tests/run_isolated_suite.py --self-check-only   # prove isolation, touch nothing

Every step runs with ``HOME``/``TMPDIR`` inside the sandbox, external platforms
and external executors disabled, and the suite running out of a copy of the
source tree.  The live tree is only read.  The script prints, in this order:

``STEP <name> exit=<code> seconds=<n>`` and the last lines of each step, then
``TREE_CLEAN=<bool>`` (the live tree gained no new file), ``TOTAL_SECONDS=<n>``,
``cleaned=<path>`` and ``home_deleted=<bool>``.

Timeout policy: the outer guard defaults to 900 s here, not the 180 s the
in-tree ``tests/run_offline_suite.py`` uses.  The full unittest discovery
takes ~390 s on this workstation (816 tests), so the 180 s guard reports a
false failure; ``--timeout`` is a per-step cap and never hides a failure.

The script never writes the production database: ``--db copy`` snapshots it
through the SQLite backup API from a read-only connection and verifies the
source file hash before and after.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
try:
    import companion_test_isolation as isolation
except ImportError as error:  # pragma: no cover - import guard
    print(f"cannot import companion_test_isolation: {error}", file=sys.stderr)
    raise SystemExit(2)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--db", choices=("fresh", "copy"), default="fresh")
    parser.add_argument("--db-source", default=None)
    parser.add_argument("--base-dir", default=None, help="sandbox parent (default: system temp)")
    parser.add_argument("--copy-source", default=str(ROOT), help="tree to stage (default: this repo)")
    parser.add_argument("--no-copy", action="store_true", help="run from the live tree (not recommended)")
    parser.add_argument("--deps", action="append", default=[],
                        help="extra site-packages directory on PYTHONPATH (repeatable)")
    parser.add_argument("--no-deps", action="store_true",
                        help="do not add the repository .test-deps directory")
    parser.add_argument("--timeout", type=int, default=900, help="per-step cap in seconds (default 900)")
    parser.add_argument("--only", action="append", choices=("unittest", "drivetest", "offline_suite"),
                        help="run only the named step(s); default is unittest + drivetest")
    parser.add_argument("--self-check-only", action="store_true")
    parser.add_argument("--self-check", action="store_true", help="run the isolation self-check first")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--log-dir", default=None, help="keep full step logs here")
    return parser.parse_args(argv)


def tree_snapshot(root):
    entries = set()
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories
                          if name not in isolation.COPY_IGNORE_NAMES and name != "__pycache__"]
        relative = Path(current).relative_to(root)
        for name in files:
            if name.endswith(".pyc"):
                continue
            entries.add((relative / name).as_posix())
    return entries


def run_step(name, command, cwd, environment, timeout, log_dir):
    print(f"STEP {name} start command={' '.join(command)} cwd={cwd}", flush=True)
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True,
                                timeout=timeout)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        returncode = result.returncode
        timed_out = False
    except subprocess.TimeoutExpired as expired:
        output = ((expired.stdout or b"") + (expired.stderr or b"")).decode("utf-8", "replace")
        returncode = -1
        timed_out = True
    seconds = round(time.monotonic() - started, 1)
    label = "TIMEOUT" if timed_out else "exit"
    print(f"STEP {name} {label}={returncode} seconds={seconds}", flush=True)
    tail = [line for line in output.splitlines() if line.strip()][-8:]
    for line in tail:
        print(f"  | {line}", flush=True)
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        (Path(log_dir) / f"{name}.log").write_text(output, encoding="utf-8")
    return returncode, seconds


def main(argv=None):
    arguments = parse_args(argv)
    if arguments.db == "copy" and not arguments.db_source:
        print("--db copy requires --db-source", file=sys.stderr)
        return 2
    # .test-deps is the local substitute for installed deps (jsonschema, aiohttp,
    # edge_tts ...).  On a host with a real venv, pass --deps /path/to/site-packages.
    deps = [str(Path(path).resolve()) for path in arguments.deps]
    if not arguments.no_deps:
        deps.append(str(ROOT / ".test-deps"))
    sandbox = isolation.Sandbox(base_dir=arguments.base_dir, db=arguments.db,
                                db_source=arguments.db_source,
                                copy_source=None if arguments.no_copy else Path(arguments.copy_source),
                                keep=arguments.keep)
    overall_ok = True
    try:
        sandbox.neutralize_config()
        python_path = os.pathsep.join([sandbox.cwd(), *deps])
        environment = sandbox.env({"PYTHONPATH": python_path})
        sys.path.insert(0, sandbox.cwd())
        print(f"# sandbox={sandbox.root}")
        print(f"# home={sandbox.home} db_mode={sandbox.report['db_mode']} "
              f"db_sha256={sandbox.report['db_sha256']}")
        print(f"# PYTHONPATH={python_path}")
        isolation.self_check(sandbox)
        if arguments.self_check_only:
            return 0
        before = None if arguments.no_copy else tree_snapshot(Path(arguments.copy_source))
        cwd = sandbox.cwd()
        steps = []
        wanted = arguments.only or ["unittest", "drivetest"]
        if "unittest" in wanted:
            steps.append(("unittest", ["{python}", "-B", "-X", "utf8", "-m", "unittest",
                                       "discover", "-s", "tests", "-t", ".", "-q"]))
        if "drivetest" in wanted:
            steps.append(("drivetest", ["{python}", "-B", "-X", "utf8", "drivetest.py"]))
        if "offline_suite" in wanted:
            steps.append(("offline_suite", ["{python}", "-B", "-X", "utf8",
                                            "tests/run_offline_suite.py"]))
        totals = {}
        for name, command in steps:
            command = [part.replace("{python}", arguments.python) for part in command]
            code, seconds = run_step(name, command, cwd, environment, arguments.timeout,
                                     arguments.log_dir)
            totals[name] = seconds
            overall_ok = overall_ok and code == 0
        if before is not None:
            after = tree_snapshot(Path(arguments.copy_source))
            gained = sorted(after - before)
            print(f"TREE_CLEAN={str(not gained).lower()}")
            for name in gained[:20]:
                print(f"  + {name}")
        # Keep the real reason for every failure: unittest prints one line each.
        log_dir = Path(arguments.log_dir) if arguments.log_dir else None
        if log_dir and (log_dir / "unittest.log").is_file():
            reasons = [line.strip() for line in (log_dir / "unittest.log").read_text(
                encoding="utf-8", errors="replace").splitlines()
                if line.startswith(("ERROR:", "FAIL:"))]
            print(f"FAILURE_COUNT={len(reasons)}")
            for line in reasons:
                print(f"  ! {line}")
        print("TOTAL_SECONDS=" + str(round(sum(totals.values()), 1)))
        for name, seconds in totals.items():
            print(f"  {name}={seconds}s")
    finally:
        cleaned = sandbox.cleanup()
        if not cleaned:
            overall_ok = False
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
