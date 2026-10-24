"""Run explicitly inside an isolated, installed IndexTTS-2.5 environment.

Use probe_local_media.py as the parent to record system RAM, VRAM and process exit.
Requires a predownloaded reference WAV and checkpoint/auxiliary files. No QQ IO.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys
import time
import wave


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.engine_root.resolve()))
    import numpy as np
    import torch
    import soundfile as sf
    from indextts.infer_v2_5 import IndexTTS2

    random.seed(20261024)
    np.random.seed(20261024)
    torch.manual_seed(20261024)
    torch.set_num_threads(4)
    torch.cuda.reset_peak_memory_stats()
    model = args.model_dir.resolve()
    engine = IndexTTS2(cfg_path=str(model / "config.yaml"), model_dir=str(model),
                       device="cuda:0", use_bf16=True, use_cuda_kernel=False,
                       use_deepspeed=False, use_accel=False, use_torch_compile=False,
                       use_qwen_emo=False)
    torch.cuda.synchronize()
    loaded = time.perf_counter()
    results = {"load_seconds_including_imports": loaded - started, "cases": [],
               "precision": "official use_bf16=True; component dtypes follow upstream",
               "qwen_emotion_loaded": False,
               "limitations": ["Emotion vectors and pace are tested; whispering is not an engine parameter.",
                               "Audio decoding and transcription do not establish naturalness or voice fidelity."]}

    def synthesize(name, text, **kwargs):
        path = args.out / (name + ".wav")
        clock = time.perf_counter()
        engine.infer(spk_audio_prompt=str(args.reference.resolve()), text=text, lang="ZH",
                     output_path=str(path.resolve()), use_random=False, **kwargs)
        torch.cuda.synchronize()
        seconds = time.perf_counter() - clock
        audio, rate = sf.read(path, dtype="float32")
        assert audio.size and np.isfinite(audio).all(), "invalid waveform"
        duration = len(audio) / rate
        row = dict(name=name, text=text, options=kwargs, seconds=seconds, sample_rate=rate,
                   duration_seconds=duration, real_time_factor=seconds / duration,
                   peak_amplitude=float(np.max(np.abs(audio))), path=str(path.resolve()))
        results["cases"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        (args.out / "metrics.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    text = "你终于回来啦。我刚才还在想你。"
    synthesize("neutral-cold", text)
    synthesize("neutral-warm", text)
    synthesize("happy", text, emo_vector=[0.3, 0, 0, 0, 0, 0, 0, 0])
    synthesize("calm-slow", text, emo_vector=[0, 0, 0, 0, 0, 0, 0, 0.5], duration_factor=1.15)
    first = synthesize("pause-first", "你终于回来啦。")
    second = synthesize("pause-second", "我刚才还在想你。")
    # One output asset with an exact 250 ms inserted boundary; no word-by-word assembly.
    with wave.open(str(first), "rb") as a, wave.open(str(second), "rb") as b:
        assert a.getparams()[:3] == b.getparams()[:3]
        frames_a, frames_b = a.readframes(a.getnframes()), b.readframes(b.getnframes())
        pause_frames = round(a.getframerate() * 0.250)
        with wave.open(str(args.out / "pause-250ms.wav"), "wb") as out:
            out.setparams(a.getparams())
            out.writeframes(frames_a + bytes(pause_frames * a.getnchannels() * a.getsampwidth()) + frames_b)
    results.update(total_seconds=time.perf_counter() - started,
                   peak_torch_allocated_mib=torch.cuda.max_memory_allocated() / 2**20,
                   peak_torch_reserved_mib=torch.cuda.max_memory_reserved() / 2**20,
                   inserted_pause_ms=250, listening_acceptance="pending")
    (args.out / "metrics.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
