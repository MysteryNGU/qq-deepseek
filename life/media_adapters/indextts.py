"""IndexTTS's reference and delivery rules; model imports occur only in execution."""
from pathlib import Path
import sys
import wave

from ..agent_session import SessionFailure


def normalize_delivery(parameters):
    parameters=dict(parameters or {})
    if set(parameters)-{'emo_vector','duration_factor'}:
        raise SessionFailure('invalid_input','本地声音仅支持 emo_vector 与 duration_factor 表达参数')
    import math
    vector=parameters.get('emo_vector')
    if vector is not None and (not isinstance(vector,list) or len(vector)!=8 or any(
            isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or not 0<=x<=1 for x in vector)):
        raise SessionFailure('invalid_input','emo_vector 需要八个 0～1 的有限数值')
    pace=parameters.get('duration_factor',1.0)
    if isinstance(pace,bool) or not isinstance(pace,(int,float)) or not math.isfinite(pace) or not .5<=pace<=2:
        raise SessionFailure('invalid_input','duration_factor 需要在 0.5～2 之间')
    return parameters


class Adapter:
    def describe(self,config):
        config.setdefault('worker_threads',4)
        config.setdefault('max_text_chars',1000)
        if any(not isinstance(config.get(k),str) or not config[k] for k in ('voice_engine_root','voice_model_dir')):
            raise ValueError('indextts_engine_and_model_paths_required')
        if any(type(config[k]) is not int or config[k]<1 for k in ('worker_threads','max_text_chars')):
            raise ValueError('invalid_indextts_limits')
        return dict(protocol='media-adapter/1',engine='indextts-2.5',kinds=['speech'],resource='gpu:0',
            summary='本地参考音色合成；支持情绪、语速和句间停顿，默认选择由原音色目录管理。',
            operations=['synthesize','audition'],reference_limit=1,voice_reference_required=True,
            parameters_schema=dict(type='object',properties={},additionalProperties=False),
            output=dict(kind='audio',mime='audio/wav',extension='.wav'),
            voice_parameters_schema=dict(type='object',properties=dict(
                emo_vector=dict(type='array',minItems=8,maxItems=8,items=dict(type='number',minimum=0,maximum=1)),
                duration_factor=dict(type='number',minimum=.5,maximum=2)),additionalProperties=False),
            public_parameters=dict(segments='1..10',pause_after_ms='0..2000',max_text_chars=config.get('max_text_chars',1000)),
            cancel_grace_seconds=0)

    def normalize_voice(self,parameters,config):
        return normalize_delivery(parameters)

    def prepare(self,p,assets,config,voice=None):
        if any(k in p for k in ('prompt','width','height','reference_asset_ids')):
            raise SessionFailure('invalid_input','语音后端不接受图片参数')
        if voice is None:
            raise SessionFailure('unavailable','当前会话尚未选择可用的本地默认音色')
        overrides=p.get('delivery',{})
        if not isinstance(overrides,dict):
            raise SessionFailure('invalid_input','delivery 需要表达参数对象')
        delivery=normalize_delivery(dict(voice['parameters'],**overrides))
        segments=p.get('segments',[dict(text=p.get('text',''))])
        if not isinstance(segments,list) or not 1<=len(segments)<=10:
            raise SessionFailure('invalid_input','语音需要 1～10 个有意义的句段')
        checked=[]
        for segment in segments:
            if not isinstance(segment,dict) or set(segment)-{'text','pause_after_ms','delivery'}:
                raise SessionFailure('invalid_input','句段仅支持 text、pause_after_ms、delivery')
            text=segment.get('text','');pause=segment.get('pause_after_ms',0)
            if not isinstance(text,str) or not text.strip() or type(pause) is not int or not 0<=pause<=2000 or not isinstance(segment.get('delivery',{}),dict):
                raise SessionFailure('invalid_input','句段不能为空，停顿为 0～2000 毫秒的整数')
            checked.append(dict(text=text.strip(),pause_after_ms=pause,
                delivery=normalize_delivery(dict(delivery,**segment.get('delivery',{})))))
        if sum(len(s['text']) for s in checked)>config['max_text_chars']:
            raise SessionFailure('invalid_input','本次语音超过单任务文字上限')
        return dict(kind='speech',seed=p.get('seed'),voice_id=voice['id'],voice_revision=voice['revision'],
            reference_asset_id=voice['reference_asset_id'],reference_sha256=voice['reference_digest'],segments=checked)

    def execute(self,spec,context):
        return execute_speech(dict(spec=spec,backend=context.config),context.folder)


def execute_speech(request, folder):
    cfg, spec = request['backend'], request['spec']
    sys.path.insert(0, cfg['voice_engine_root'])
    print('stage: importing isolated speech dependencies',flush=True)
    import random
    import numpy as np
    import torch
    from indextts.infer_v2_5 import IndexTTS2
    torch.set_num_threads(cfg['worker_threads'])
    random.seed(spec['seed'])
    np.random.seed(spec['seed'])
    torch.manual_seed(spec['seed'])
    model = Path(cfg['voice_model_dir'])
    print('stage: loading IndexTTS-2.5',flush=True)
    engine = IndexTTS2(cfg_path=str(model/'config.yaml'), model_dir=str(model), device='cuda:0',
        use_bf16=True, use_cuda_kernel=False, use_deepspeed=False, use_accel=False,
        use_torch_compile=False, use_qwen_emo=False)
    parts = []
    for index, segment in enumerate(spec['segments']):
        if (folder/'cancel.requested').exists():
            raise InterruptedError('cancelled')
        path = folder/f'segment-{index}.wav'
        print('stage: synthesizing segment',index,flush=True)
        engine.infer(spk_audio_prompt=spec['reference_path'], text=segment['text'], lang='ZH',
            output_path=str(path), use_random=False, **segment['delivery'])
        parts.append((path, segment['pause_after_ms']))
    output = folder/'output.wav'
    with wave.open(str(output), 'wb') as target:
        expected = None
        for path, pause in parts:
            with wave.open(str(path), 'rb') as source:
                profile = source.getparams()[:3]
                if expected is None:
                    expected = profile
                    target.setnchannels(source.getnchannels())
                    target.setsampwidth(source.getsampwidth())
                    target.setframerate(source.getframerate())
                if profile != expected:
                    raise ValueError('inconsistent_segment_audio_formats')
                target.writeframes(source.readframes(source.getnframes()))
                if pause:
                    target.writeframes(bytes(round(source.getframerate()*pause/1000)*profile[0]*profile[1]))
    for path, _ in parts:
        path.unlink(missing_ok=True)
    return output
