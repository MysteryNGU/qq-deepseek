"""Execute one frozen installed adapter inside its owned engine environment."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave


def atomic_json(path,value):
    temporary=path.with_suffix('.writing')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


class RunContext:
    def __init__(self,folder,config,cache=None):
        self.folder,self.config=folder,config
        self.cache=cache
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


def resident_cuda_memory():
    torch=sys.modules.get('torch')
    if torch is None or not torch.cuda.is_initialized():
        return None
    allocated=torch.cuda.memory_allocated()
    reserved=torch.cuda.memory_reserved()
    return dict(cached_free_mib=(reserved-allocated)/2**20,
                workspace_peak_mib=max(0,torch.cuda.max_memory_allocated()-allocated)/2**20)


def execute_request(path, cache=None):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from life.media_adapters import validate_frozen,frozen_hash
    path=Path(path).resolve();folder=path.parent
    request=json.loads(path.read_text(encoding='utf-8'))
    spec=request['spec'];context=None
    started=time.monotonic()
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
        adapter=validate_frozen(spec);provider=spec['_adapter']
        context=RunContext(folder,provider['config'],cache);context.check_cancelled()
        output=Path(adapter.execute(spec,context)).resolve()
        context.close();context.check_cancelled()
        if output.parent!=folder or output.is_symlink() or not output.is_file():
            raise ValueError('adapter_output_outside_job')
        metadata=output_metadata(output,provider['description'])
        atomic_json(folder/'result.json',dict(status='completed',request_hash=request['request_hash'],
            frozen_spec_hash=request.get('frozen_spec_hash'),job_id=request.get('job_id'),
            provider_digest=provider['digest'],provider_revision=provider['adapter_version'],
            actual_parameters=request.get('frozen_spec',request['spec']),output=output.name,
            sha256=hashlib.sha256(output.read_bytes()).hexdigest(),media=metadata,
            worker_pid=os.getpid(),execution_seconds=round(time.monotonic()-started,3),
            resident_cuda=resident_cuda_memory()))
    except BaseException as error:
        atomic_json(folder/'result.json',dict(status='cancelled' if isinstance(error,InterruptedError) else 'failed',
            request_hash=request['request_hash'],frozen_spec_hash=request.get('frozen_spec_hash'),job_id=request.get('job_id'),
            error=type(error).__name__+': '+str(error)[:1000]))
        raise
    finally:
        if context:
            context.close()


def main():
    if len(sys.argv)>1 and sys.argv[1]=='--serve':
        # Parent owns the process tree and sends only one request at a time.
        # EOF stops the worker; errors terminate it rather than retaining an
        # engine whose CUDA state or cancellation outcome could be uncertain.
        cache={}
        root=Path(sys.argv[2]).resolve()
        for line in sys.stdin:
            path=Path(json.loads(line)['request']).resolve()
            if path.name!='request.json' or root not in path.parents or path.parent==root:
                raise ValueError('request_outside_managed_media_root')
            with (path.parent/'engine.log').open('a',encoding='utf-8',buffering=1) as stream:
                # Native libraries also write directly to the OS descriptors.
                saved=[os.dup(fd) for fd in (1,2)]
                try:
                    sys.stdout.flush();sys.stderr.flush()
                    os.dup2(stream.fileno(),1);os.dup2(stream.fileno(),2)
                    execute_request(path,cache)
                    sys.stdout.flush();sys.stderr.flush()
                finally:
                    for fd,old in zip((1,2),saved):
                        os.dup2(old,fd);os.close(old)
    else:
        execute_request(sys.argv[1])


if __name__=='__main__':
    main()
