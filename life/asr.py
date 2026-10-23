"""Local audio perception. Transcripts are observations, never speaker identity."""
from __future__ import annotations

import asyncio
from importlib.metadata import version
from pathlib import Path
import os

# Use ordinary HTTPS downloads rather than a second native transfer stack;
# transport timeouts and an interrupted download remain recoverable.
os.environ.setdefault('HF_HUB_DISABLE_XET','1')


class WhisperASR:
    def __init__(self,models,model='small',language='zh'):
        self.models,self.model,self.language = Path(models),model,language
        self._model = None
        self._lock = asyncio.Lock()

    def _transcribe(self,path):
        from faster_whisper import WhisperModel
        if self._model is None:
            self.models.mkdir(parents=True,exist_ok=True)
            from huggingface_hub.errors import LocalEntryNotFoundError
            options = dict(device='cpu',compute_type='int8',download_root=str(self.models))
            try:
                self._model = WhisperModel(self.model,local_files_only=True,**options)
            except LocalEntryNotFoundError:
                self._model = WhisperModel(self.model,**options)
        # Decode using the installed PyAV API. Passing a normalized array avoids
        # faster-whisper's old metadata_errors keyword, removed by newer PyAV.
        import av
        import numpy as np
        resampler = av.AudioResampler(format='s16',layout='mono',rate=16000)
        samples = []
        with av.open(str(path),mode='r') as container:
            for frame in container.decode(audio=0):
                samples.extend(chunk.to_ndarray().reshape(-1) for chunk in resampler.resample(frame))
            samples.extend(chunk.to_ndarray().reshape(-1) for chunk in resampler.resample(None))
        if not samples:
            raise ValueError('audio_contains_no_decodable_samples')
        audio = np.concatenate(samples).astype(np.float32)/32768.0
        segments,info = self._model.transcribe(audio,language=self.language or None,vad_filter=True)
        chunks = [dict(start=float(s.start),end=float(s.end),text=s.text,
                       avg_logprob=float(s.avg_logprob),no_speech_prob=float(s.no_speech_prob)) for s in segments]
        return dict(content=dict(text=''.join(s['text'] for s in chunks).strip(),segments=chunks),
            engine_version='faster-whisper/'+version('faster-whisper')+'/'+self.model,
            coverage=dict(duration_seconds=float(info.duration),language=info.language,
                          intervals=[[s['start'],s['end']] for s in chunks]),
            confidence=None,limitations=['自动转写可能识别错误；不据此判断哭腔、说话人或情绪。'])

    async def __call__(self,path):
        async with self._lock:
            return await asyncio.to_thread(self._transcribe,path)
