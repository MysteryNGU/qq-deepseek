"""Download pinned public BGE safetensors; verify weights before activation."""
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import json
from pathlib import Path
import time
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
MODEL='BAAI/bge-small-zh-v1.5'
REVISION='7999e1d3359715c523056ef9478215996d62a620'
WEIGHT_SHA='354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026'
SIZE=95827648


def install(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    base=f'https://huggingface.co/{MODEL}/resolve/{REVISION}/'
    assets=['config.json','tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.txt','README.md']
    for name in assets:
        path=folder/name
        if not path.exists():
            with urlopen(base+name,timeout=30) as response:
                body=response.read(2*1024*1024)
            path.write_bytes(body)
    weights=folder/'model.safetensors'
    if not weights.exists() or hashlib.sha256(weights.read_bytes()).hexdigest()!=WEIGHT_SHA:
        step=1048576
        def chunk(start):
            end=min(SIZE-1,start+step-1)
            for attempt in range(5):
                try:
                    request=Request(base+'model.safetensors?download=true',headers={'Range':f'bytes={start}-{end}'})
                    with urlopen(request,timeout=30) as response:
                        if response.status!=206 or not response.headers.get('Content-Range','').startswith(f'bytes {start}-'):
                            raise ValueError('range_mismatch')
                        body=response.read(end-start+2)
                    if len(body)!=end-start+1:
                        raise ValueError('range_length')
                    return start,body
                except Exception:
                    if attempt==4:raise
                    time.sleep(.5)
        temp=folder/'model.safetensors.verified-download'
        with temp.open('wb') as out:out.truncate(SIZE)
        with ThreadPoolExecutor(max_workers=8) as pool,temp.open('r+b') as out:
            futures=[pool.submit(chunk,s) for s in range(0,SIZE,step)]
            for i,future in enumerate(as_completed(futures),1):
                start,body=future.result();out.seek(start);out.write(body)
                if i%16==0:print(f'verified ranges {i}/{len(futures)}',flush=True)
        if hashlib.sha256(temp.read_bytes()).hexdigest()!=WEIGHT_SHA:
            raise ValueError('weight_digest_mismatch')
        temp.replace(weights)
    files={name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in assets+['model.safetensors']}
    manifest=dict(model_id=MODEL,revision=REVISION,dimension=512,pooling='cls-l2',
        instruction='none',device='cpu',license='MIT',source='https://huggingface.co/'+MODEL,files=files)
    data=json.dumps(manifest,sort_keys=True,ensure_ascii=False,indent=2).encode('utf-8')
    (folder/'model-manifest.json').write_bytes(data)
    result=dict(path=str(folder.resolve()),manifest_digest=hashlib.sha256(data).hexdigest(),revision=REVISION,weight_sha256=WEIGHT_SHA)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return result


if __name__=='__main__':install(ROOT/'data/models/bge-small-zh-v1.5')
