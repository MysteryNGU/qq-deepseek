"""Owned, CPU-only loopback embedding process; downloads are never implicit."""
from dataclasses import dataclass,fields
import hashlib
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import build_opener,ProxyHandler


@dataclass(frozen=True)
class LocalEmbeddingConfig:
    enabled: bool=False
    model_path: str='data/models/bge-small-zh-v1.5'
    manifest_digest: str=''
    host: str='127.0.0.1'
    port: int=8231
    threads: int=2

    @classmethod
    def parse(cls,raw=None):
        raw=raw or {}
        if not isinstance(raw,dict) or set(raw)-{f.name for f in fields(cls)}:
            raise ValueError('invalid_local_embedding_config')
        result=cls(**raw)
        if (type(result.enabled) is not bool or result.host!='127.0.0.1'
                or type(result.port) is not int or not 1024<=result.port<=65535
                or type(result.threads) is not int or not 1<=result.threads<=8
                or result.enabled and (len(result.manifest_digest)!=64 or not result.model_path)):
            raise ValueError('invalid_local_embedding_config')
        return result


def verified_manifest(folder,expected):
    folder=Path(folder)
    data=(folder/'model-manifest.json').read_bytes()
    if hashlib.sha256(data).hexdigest()!=expected:
        raise ValueError('embedding_manifest_changed')
    manifest=json.loads(data)
    for name,digest in manifest['files'].items():
        path=(folder/name).resolve()
        if folder.resolve() not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('embedding_asset_changed')
    if manifest['pooling']!='cls-l2' or manifest['dimension']!=512:
        raise ValueError('embedding_model_contract_changed')
    return manifest


class CpuModel:
    def __init__(self,folder,manifest,threads):
        os.environ.update(CUDA_VISIBLE_DEVICES='',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
        import torch
        from transformers import AutoModel,AutoTokenizer
        torch.set_num_threads(threads)
        self.torch=torch
        self.tokenizer=AutoTokenizer.from_pretrained(str(folder),local_files_only=True,trust_remote_code=False)
        self.model=AutoModel.from_pretrained(str(folder),local_files_only=True,trust_remote_code=False,use_safetensors=True).to('cpu').eval()
        self.manifest=manifest

    def embed(self,texts):
        batch=self.tokenizer(texts,padding=True,truncation=False,return_tensors='pt')
        if batch['input_ids'].shape[1]>512:
            raise ValueError('embedding_input_exceeds_512_tokens')
        with self.torch.inference_mode():
            states=self.model(**batch).last_hidden_state[:,0]
            vectors=self.torch.nn.functional.normalize(states,p=2,dim=1).tolist()
        return vectors,int(batch['attention_mask'].sum().item())


class BoundedHTTPServer(HTTPServer):
    def get_request(self):
        connection,address=super().get_request()
        connection.settimeout(10)
        return connection,address


def handler_for(model,manifest,digest):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # HTTP logs must never retain submitted experience text.

        def reply(self,status,value):
            data=json.dumps(value,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Connection','close')
            self.end_headers();self.wfile.write(data)

        def do_GET(self):
            if self.path!='/health':
                return self.reply(404,dict(error='not_found'))
            self.reply(200,dict(ready=True,pid=os.getpid(),device='cpu',model=manifest['model_id'],
                revision=manifest['revision'],dimension=manifest['dimension'],manifest_digest=digest))

        def do_POST(self):
            if self.path!='/v1/embeddings':
                return self.reply(404,dict(error='not_found'))
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 1<=size<=262144:
                    # Drain a bounded small overrun before closing. Otherwise
                    # Windows can reset a still-writing client before its 413
                    # is received. Large/unbounded bodies are never consumed.
                    if 262144<size<=524288:
                        self.rfile.read(size)
                    return self.reply(413,dict(error='request_too_large'))
                request=json.loads(self.rfile.read(size))
                texts=request.get('input')
                if (request.get('model')!=manifest['model_id'] or not isinstance(texts,list)
                        or not 1<=len(texts)<=32 or any(not isinstance(t,str) or not t.strip() for t in texts)):
                    raise ValueError('invalid_embedding_input')
                vectors,tokens=model.embed(texts)
                self.reply(200,dict(object='list',model=manifest['model_id'],
                    data=[dict(index=i,object='embedding',embedding=v) for i,v in enumerate(vectors)],
                    usage=dict(prompt_tokens=tokens,total_tokens=tokens)))
            except (ValueError,TypeError,KeyError):
                self.reply(400,dict(error='invalid_embedding_input'))
            except Exception:
                self.reply(500,dict(error='local_embedding_failed'))

    return Handler


def serve(folder,digest,port,threads):
    manifest=verified_manifest(folder,digest)
    model=CpuModel(folder,manifest,threads)
    model.embed(['本地中文检索服务启动检查。'])
    server=BoundedHTTPServer(('127.0.0.1',port),handler_for(model,manifest,digest))
    server.socket.settimeout(30)
    server.serve_forever()


class LocalEmbeddingProcess:
    def __init__(self,root,raw=None,embedding=None):
        self.root=Path(root);self.config=LocalEmbeddingConfig.parse(raw);self.tree=None
        self.embedding=embedding or {};self.verified=False
        if self.config.enabled:
            expected=f'http://127.0.0.1:{self.config.port}/v1/embeddings'
            if not embedding or not embedding.get('enabled') or embedding.get('endpoint')!=expected:
                raise ValueError('local_embedding_endpoint_mismatch')

    def start(self):
        if not self.config.enabled or self.tree is not None and self.tree.process.poll() is None:
            return
        self.close()
        from .supervisor import ProcessTree
        cfg=self.config
        folder=(self.root/cfg.model_path).resolve()
        manifest=verified_manifest(folder,cfg.manifest_digest)
        if (self.embedding.get('model_id')!=manifest['model_id'] or self.embedding.get('model_revision')!=manifest['revision']
                or self.embedding.get('dimension')!=manifest['dimension']):
            raise ValueError('local_embedding_model_mismatch')
        env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','TEMP','TMP','PATH','USERPROFILE','APPDATA','LOCALAPPDATA'}}
        env.update(CUDA_VISIBLE_DEVICES='',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
        logs=self.root/'data'/'logs';logs.mkdir(parents=True,exist_ok=True)
        with (logs/'local-embedding.log').open('ab') as output:
            self.tree=ProcessTree([sys.executable,'-B','-m','life.local_embedding',str(folder),cfg.manifest_digest,str(cfg.port),str(cfg.threads)],
                cwd=self.root,env=env,stdout_file=output,stderr_file=output)
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if self.tree.process.poll() is not None:
                self.close();raise RuntimeError('local_embedding_start_failed')
            try:
                with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{cfg.port}/health',timeout=1) as response:
                    health=json.load(response)
                if (health.get('manifest_digest')==cfg.manifest_digest and health.get('ready')
                        and self.tree.owns_pid(health.get('pid'))):
                    self.verified=True
                    return
            except (OSError,ValueError):
                pass
            time.sleep(.25)
        self.close();raise RuntimeError('local_embedding_start_timeout')

    def close(self):
        if self.tree is not None:
            self.tree.stop();self.tree=None
        self.verified=False

    def status(self):
        alive=self.tree is not None and self.tree.process.poll() is None
        return dict(state='disabled' if not self.config.enabled else 'ready' if alive and self.verified else 'unavailable',
                    device='cpu',host='127.0.0.1',port=self.config.port,
                    manifest_digest=self.config.manifest_digest or None)


if __name__=='__main__':
    serve(Path(sys.argv[1]),sys.argv[2],int(sys.argv[3]),int(sys.argv[4]))
