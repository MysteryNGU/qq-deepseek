"""Execute one frozen installed adapter inside its owned engine environment."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import wave


def atomic_json(path,value):
    temporary=path.with_suffix('.writing')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


class RunContext:
    def __init__(self,folder,config):
        self.folder,self.config=folder,config
        self.children=[]

    def check_cancelled(self):
        if (self.folder/'cancel.requested').exists():
            raise InterruptedError('cancelled')

    def spawn(self,command,**kwargs):
        process=subprocess.Popen(command,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),**kwargs)
        self.children.append(process)
        return process

    def close(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill();child.wait(timeout=10)


def output_metadata(path,description):
    """Report physical encoding and dimensions without treating them as inspection."""
    output=description['output']
    result=dict(kind=output['kind'],mime=output['mime'],bytes=path.stat().st_size)
    if output['mime']=='image/png':
        data=path.read_bytes()
        if data[:8]!=b'\x89PNG\r\n\x1a\n' or len(data)<24:
            raise ValueError('invalid_png_output')
        result.update(width=int.from_bytes(data[16:20],'big'),height=int.from_bytes(data[20:24],'big'))
    elif output['mime']=='audio/wav':
        with wave.open(str(path),'rb') as source:
            if source.getsampwidth()!=2 or source.getnframes()<1:
                raise ValueError('invalid_pcm_audio_output')
            result.update(sample_rate=source.getframerate(),channels=source.getnchannels(),
                duration_seconds=source.getnframes()/source.getframerate())
    return result


def main():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from life.media_adapters import AdapterRegistry,validate_frozen,frozen_hash
    path=Path(sys.argv[1]).resolve();folder=path.parent
    request=json.loads(path.read_text(encoding='utf-8'))
    spec=request['spec'];context=None
    try:
        if request.get('frozen_spec_hash'):
            if frozen_hash(request['frozen_spec'])!=request['frozen_spec_hash'] or spec['_adapter']!=request['frozen_spec']['_adapter']:
                raise ValueError('frozen_spec_hash_mismatch')
            normalized=json.loads(json.dumps(spec))
            reference_path=normalized.pop('reference_path',None)
            if reference_path and hashlib.sha256(Path(reference_path).read_bytes()).hexdigest()!=normalized.get('reference_sha256'):
                raise ValueError('resolved_voice_reference_mismatch')
            for reference in normalized.get('references',[]):
                material=reference.pop('path',None)
                if material is None or hashlib.sha256(Path(material).read_bytes()).hexdigest()!=reference.get('sha256'):
                    raise ValueError('resolved_image_reference_mismatch')
            if frozen_hash(normalized)!=request['frozen_spec_hash']:
                raise ValueError('resolved_parameters_mismatch')
        elif '_adapter' not in spec:
            registry=AdapterRegistry(request['backend'],Path(__file__).resolve().parents[1])
            spec=dict(spec,_adapter=registry.resolve(spec['kind']))
        adapter=validate_frozen(spec);provider=spec['_adapter']
        context=RunContext(folder,provider['config']);context.check_cancelled()
        output=Path(adapter.execute(spec,context)).resolve()
        context.close();context.check_cancelled()
        if output.parent!=folder or output.is_symlink() or not output.is_file():
            raise ValueError('adapter_output_outside_job')
        metadata=output_metadata(output,provider['description'])
        atomic_json(folder/'result.json',dict(status='completed',request_hash=request['request_hash'],
            frozen_spec_hash=request.get('frozen_spec_hash'),job_id=request.get('job_id'),
            provider_digest=provider['digest'],provider_revision=provider['adapter_version'],
            actual_parameters=request.get('frozen_spec',request['spec']),output=output.name,
            sha256=hashlib.sha256(output.read_bytes()).hexdigest(),media=metadata))
    except BaseException as error:
        atomic_json(folder/'result.json',dict(status='cancelled' if isinstance(error,InterruptedError) else 'failed',
            request_hash=request['request_hash'],frozen_spec_hash=request.get('frozen_spec_hash'),job_id=request.get('job_id'),
            error=type(error).__name__+': '+str(error)[:1000]))
        raise
    finally:
        if context:
            context.close()


if __name__=='__main__':
    main()
