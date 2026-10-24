"""Benchmark a private stable-diffusion.cpp server with predownloaded Qwen weights.

Run through probe_local_media.py to include startup, system RAM and global VRAM.
The generated files are benchmark artifacts; this script never sends to QQ.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request

from PIL import Image


SIGMAS = [1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--suite", choices=["generate", "edit"], required=True)
    parser.add_argument("--max-vram", type=float, default=5.0)
    parser.add_argument("--cancel-check", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    model = args.models.resolve()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    # Local media must stay on loopback, independently of system proxy settings.
    local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(method, endpoint, payload=None, timeout=15):
        request = urllib.request.Request(base + endpoint, method=method,
                                         data=json.dumps(payload).encode() if payload is not None else None,
                                         headers={"Content-Type": "application/json"})
        with local_http.open(request, timeout=timeout) as response:
            return json.load(response)

    command = [str(args.server.resolve()), "--listen-ip", "127.0.0.1", "--listen-port", str(port),
               "--diffusion-model", str(model / "qwen-image-2.1-turbo-Q4_K_M.gguf"),
               "--llm", str(model / "Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf"),
               "--llm_vision", str(model / "mmproj-F16.gguf"),
               "--vae", str(model / "vae/qwen_image_2.1_vae_bf16.safetensors"),
               "--max-vram", str(args.max_vram), "--threads", "4", "--mmap",
               "--model-args", "qwen_image_2_1_prefix_cache_type=q8_0",
               "--offload-to-cpu", "--flash-attn", "on", "--log-level", "debug"]
    started = time.perf_counter()
    metrics = {"command": command, "suite": args.suite, "cases": [], "api_status_latencies_s": [],
               "quality_acceptance": "requires actual image inspection", "qq_acceptance": "not tested"}
    reference = "data:image/png;base64," + base64.b64encode(args.reference.read_bytes()).decode()

    def save_metrics():
        (args.out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    def request_spec(prompt, edit):
        return dict(prompt=prompt, width=1024, height=1024, seed=20261024, batch_count=1,
                    ref_images=[reference] if edit else [], output_format="png",
                    sample_params=dict(sample_method="euler", sample_steps=8, custom_sigmas=SIGMAS,
                                       guidance=dict(txt_cfg=1.0)),
                    vae_tiling_params=dict(enabled=True, tile_size_w=512, tile_size_h=512))

    def poll(job_id, timeout=900):
        clock = time.perf_counter()
        while time.perf_counter() - clock < timeout:
            query = time.perf_counter()
            result = api("GET", "/sdcpp/v1/jobs/" + job_id)
            metrics["api_status_latencies_s"].append(time.perf_counter() - query)
            if result["status"] in {"completed", "failed", "cancelled"}:
                return result
            time.sleep(0.5)
        raise TimeoutError("native generation did not reach a terminal state")

    with (args.out / "native.log").open("wb") as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            while True:
                if child.poll() is not None:
                    raise RuntimeError(f"server exited during startup: {child.returncode}")
                try:
                    capabilities = api("GET", "/sdcpp/v1/capabilities", timeout=2)
                    break
                except (OSError, urllib.error.URLError):
                    if time.perf_counter() - started > 600:
                        raise TimeoutError("server startup exceeded 600 seconds")
                    time.sleep(0.5)
            metrics["startup_seconds"] = time.perf_counter() - started
            (args.out / "capabilities.json").write_text(json.dumps(capabilities, indent=2), encoding="utf-8")
            if args.suite == "generate":
                prompt = "A clean product photograph of a blue ceramic mug with a yellow five-point star, one handle on the right, sitting on a wooden table against a beige wall. Soft daylight, no text."
                cases = [("generate-cold", prompt), ("generate-warm", prompt)]
            else:
                cases = [
                    ("background-cold", "Change the beige wall background to mint green. Keep the blue mug, its yellow star, handle, shape and wooden table unchanged."),
                    ("color-warm", "Change only the blue mug to red. Keep its yellow star, handle, shape, beige wall background and wooden table unchanged."),
                    ("style-warm", "Convert this image into a watercolor painting. Preserve the blue mug with a yellow star, the handle on its right, and the overall composition."),
                ]
            for index, (name, prompt) in enumerate(cases):
                clock = time.perf_counter()
                spec = request_spec(prompt, args.suite == "edit")
                receipt = api("POST", "/sdcpp/v1/img_gen", spec)
                result = poll(receipt["id"])
                row = dict(name=name, prompt=prompt, job_id=receipt["id"], status=result["status"],
                           request_seconds=time.perf_counter() - clock,
                           total_seconds_including_startup=time.perf_counter() - (started if index == 0 else clock),
                           seed=spec["seed"], width=1024, height=1024, steps=8, sigmas=SIGMAS)
                if result["status"] == "completed":
                    blob = base64.b64decode(result["result"]["images"][0]["b64_json"], validate=True)
                    with Image.open(io.BytesIO(blob)) as image:
                        image.load()
                        assert image.size == (1024, 1024), image.size
                    path = args.out / (name + ".png")
                    path.write_bytes(blob)
                    row.update(path=str(path.resolve()), sha256=hashlib.sha256(blob).hexdigest())
                else:
                    row["error"] = result.get("error")
                metrics["cases"].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                save_metrics()
                if result["status"] != "completed":
                    raise RuntimeError("generation failed; inspect native.log and metrics.json")
            if args.cancel_check:
                spec = request_spec(cases[-1][1], args.suite == "edit")
                first = api("POST", "/sdcpp/v1/img_gen", spec)
                second = api("POST", "/sdcpp/v1/img_gen", spec)
                queued_cancel = api("POST", "/sdcpp/v1/jobs/" + second["id"] + "/cancel", {})
                time.sleep(2)
                clock = time.perf_counter()
                running_cancel = api("POST", "/sdcpp/v1/jobs/" + first["id"] + "/cancel", {})
                final = poll(first["id"], timeout=120)
                metrics["cancel_check"] = dict(queued=queued_cancel, running_receipt=running_cancel,
                                                running_final=final, seconds_to_terminal=time.perf_counter() - clock)
                assert queued_cancel["status"] == "cancelled", queued_cancel
                assert final["status"] == "cancelled", final
            metrics["unload_receipt"] = api("POST", "/sdcpp/v1/models/unload", {}, timeout=60)
        except BaseException as error:
            metrics["failure"] = dict(type=type(error).__name__, message=str(error))
            raise
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
            metrics["server_exit_code"] = child.returncode
            metrics["wall_seconds"] = time.perf_counter() - started
            save_metrics()


if __name__ == "__main__":
    main()
