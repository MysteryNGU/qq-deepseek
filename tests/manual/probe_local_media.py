"""Explicit local engine benchmark; never starts the Bot or sends QQ messages.

Run with the Bot Python (psutil installed), passing an isolated engine command:
  python tests/manual/probe_local_media.py --out data/media-feasibility/runs/demo -- <engine> <args>
The JSON/CSV/log are local runtime evidence, not automated quality acceptance.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

import psutil


def gpu_sample():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            return {}
        values = [float(v.strip()) for v in result.stdout.splitlines()[0].split(",")]
        return dict(zip(("gpu_total_mib", "gpu_used_mib", "gpu_free_mib", "gpu_util_pct"), values))
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return {}


def resources(process=None):
    memory = psutil.virtual_memory()
    result = {"ram_available_mib": memory.available / 2**20, **gpu_sample()}
    if process is not None:
        try:
            tree = [process, *process.children(recursive=True)]
        except psutil.Error:
            tree = []
        rss = 0
        for item in tree:
            try:
                rss += item.memory_info().rss
            except psutil.Error:
                pass
        result["owned_tree_rss_mib"] = rss / 2**20
    return result


def stop_owned(process, child):
    """Terminate only this benchmark's captured process tree, checking PID reuse."""
    try:
        owned = process.children(recursive=True)
    except psutil.Error:
        owned = []
    for item in reversed(owned):
        try:
            # psutil.Process caches creation time and checks PID reuse on terminate.
            item.terminate()
        except psutil.Error:
            pass
    if child.poll() is None:
        child.terminate()
    _, alive = psutil.wait_procs(owned, timeout=5)
    for item in alive:
        try:
            item.kill()
        except psutil.Error:
            pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--cancel-after", type=float)
    parser.add_argument("--min-available-ram-mib", type=float, default=0,
                        help="stop this benchmark after three consecutive samples below this RAM floor")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an engine command after -- is required")
    args.out.mkdir(parents=True, exist_ok=False)
    before = resources()
    started = datetime.now(timezone.utc).isoformat()
    clock = time.perf_counter()
    reason = None
    samples = []
    low_memory_samples = 0
    with (args.out / "engine.log").open("wb") as log:
        child = subprocess.Popen(command, cwd=args.cwd, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process = psutil.Process(child.pid)
        try:
            while child.poll() is None:
                elapsed = time.perf_counter() - clock
                sample = {"elapsed_s": elapsed, **resources(process)}
                samples.append(sample)
                low_memory_samples = (low_memory_samples + 1
                                      if sample["ram_available_mib"] < args.min_available_ram_mib else 0)
                if args.cancel_after is not None and elapsed >= args.cancel_after:
                    reason = "explicit_benchmark_cancel"
                    stop_owned(process, child)
                elif elapsed >= args.timeout:
                    reason = "benchmark_timeout"
                    stop_owned(process, child)
                elif low_memory_samples >= 3:
                    reason = "system_ram_floor"
                    stop_owned(process, child)
                else:
                    time.sleep(0.5)
        except BaseException:
            stop_owned(process, child)
            raise
        exit_code = child.wait()
    elapsed = time.perf_counter() - clock
    after = resources()
    if samples:
        columns = list(dict.fromkeys(key for row in samples for key in row))
        with (args.out / "samples.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(samples)
    summary = dict(
        started_utc=started, command=command, cwd=str(args.cwd.resolve()) if args.cwd else None,
        pid=child.pid, exit_code=exit_code, stop_reason=reason, wall_seconds=elapsed,
        min_available_ram_floor_mib=args.min_available_ram_mib,
        before=before, after=after, samples=len(samples),
        peak_global_gpu_used_mib=max((s["gpu_used_mib"] for s in samples if "gpu_used_mib" in s), default=None),
        peak_owned_tree_rss_mib=max((s.get("owned_tree_rss_mib", 0) for s in samples), default=None),
        min_system_available_ram_mib=min((s["ram_available_mib"] for s in samples), default=None),
        limitations=["GPU memory is whole-device sampled usage, including unrelated applications.",
                     "Sampled peaks can miss short spikes; WDDM shared memory is not VRAM.",
                     "Process-tree RSS may count shared pages more than once.",
                     "Successful process exit does not prove media quality or QQ delivery."],
    )
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return exit_code if exit_code in range(1, 126) else (1 if exit_code else 0)


if __name__ == "__main__":
    raise SystemExit(main())
