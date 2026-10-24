"""Qwen sdcpp's verified recipe; imports no model libraries during discovery."""
import base64
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

from ..agent_session import SessionFailure
from ..media_worker import atomic_json

SIGMAS=[1,.978453,.95418,.926626,.89508,.845148,.704534,.414568,0]


class Adapter:
    def describe(self,config):
        return dict(protocol='media-adapter/1',engine='qwen-image-sdcpp',kinds=['image'],resource='gpu:0',
            summary='本地 Qwen Turbo 作画与单参考图编辑；结果由原角色决定检查或分享。',
            operations=['generate','reference_edit'],reference_limit=1,mask=False,
            parameters_schema=dict(type='object',properties={},additionalProperties=False),
            output=dict(kind='image',mime='image/png',extension='.png'),
            public_parameters=dict(prompt='1..4000 characters',width='256..1536, multiple of 64',
                height='256..1536, multiple of 64',max_pixels=1048576,steps=8,sampler='euler',cfg=1),
            cancel_grace_seconds=12)

    def prepare(self,p,assets,config,voice=None):
        if any(k in p for k in ('text','segments','delivery','voice_id','voice_revision')):
            raise SessionFailure('invalid_input','图片后端不接受语音参数')
        prompt=p.get('prompt','')
        width,height=p.get('width',1024),p.get('height',1024)
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt.strip())>4000:
            raise SessionFailure('invalid_input','生图提示词不能为空且不超过 4000 字符')
        if any(type(n) is not int or not 256<=n<=1536 or n%64 for n in (width,height)) or width*height>1024**2:
            raise SessionFailure('invalid_input','尺寸需为 64 的倍数、单边 256～1536，首版不超过约 1MP')
        refs=assets.get('references',[])
        if len(refs)>1:
            raise SessionFailure('invalid_input','当前后端支持零或一张实际参考图，不支持蒙版')
        return dict(kind='image',seed=p.get('seed'),prompt=prompt.strip(),width=width,height=height,
            references=refs,steps=8,sampler='euler',cfg=1)

    def execute(self,spec,context):
        return execute_image(dict(spec=spec,backend=context.config),context.folder)


def execute_image(request, folder):
    cfg, spec = request['backend'], request['spec']
    models = Path(cfg['image_model_dir'])
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(method, endpoint, data=None):
        query = urllib.request.Request(f'http://127.0.0.1:{port}'+endpoint, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json'})
        with opener.open(query, timeout=30) as response:
            return json.load(response)

    command = [cfg['image_server'], '--listen-ip', '127.0.0.1', '--listen-port', str(port),
        '--diffusion-model', str(models/'qwen-image-2.1-turbo-Q4_K_M.gguf'),
        '--llm', str(models/'Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf'),
        '--llm_vision', str(models/'mmproj-F16.gguf'),
        '--vae', str(models/'vae/qwen_image_2.1_vae_bf16.safetensors'),
        '--max-vram', str(cfg['image_max_vram_gib']), '--threads', str(cfg['worker_threads']),
        '--mmap', '--offload-to-cpu', '--flash-attn', 'on',
        '--model-args', 'qwen_image_2_1_prefix_cache_type=q8_0']
    with (folder/'native.log').open('wb') as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            started = time.monotonic()
            while True:
                if (folder/'cancel.requested').exists():
                    raise InterruptedError('cancelled')
                if child.poll() is not None:
                    raise RuntimeError('image_server_exited_before_ready')
                try:
                    api('GET', '/sdcpp/v1/capabilities')
                    break
                except OSError:
                    if time.monotonic()-started>120:
                        raise TimeoutError('image_server_startup_timeout')
                    time.sleep(.25)
            refs = ['data:'+r['mime']+';base64,'+base64.b64encode(Path(r['path']).read_bytes()).decode()
                    for r in spec.get('references',[])]
            params = dict(prompt=spec['prompt'], width=spec['width'], height=spec['height'],
                seed=spec['seed'], batch_count=1, ref_images=refs, output_format='png',
                sample_params=dict(sample_method='euler',sample_steps=8,custom_sigmas=SIGMAS,guidance=dict(txt_cfg=1.0)),
                vae_tiling_params=dict(enabled=True,tile_size_w=512,tile_size_h=512))
            receipt = api('POST', '/sdcpp/v1/img_gen', params)
            atomic_json(folder/'native.json', dict(job_id=receipt['id'], port=port))
            cancelled = False
            while True:
                if (folder/'cancel.requested').exists() and not cancelled:
                    api('POST','/sdcpp/v1/jobs/'+receipt['id']+'/cancel',{})
                    cancelled = True
                result = api('GET','/sdcpp/v1/jobs/'+receipt['id'])
                if result['status']=='completed':
                    path = folder/'output.png'
                    path.write_bytes(base64.b64decode(result['result']['images'][0]['b64_json'],validate=True))
                    return path
                if result['status']=='cancelled':
                    raise InterruptedError('cancelled')
                if result['status']=='failed':
                    raise RuntimeError('image_generation_failed:'+str(result.get('error')))
                time.sleep(.5)
        finally:
            if child.poll() is None:
                try:
                    api('POST','/sdcpp/v1/models/unload',{})
                except OSError:
                    pass  # Process termination below also releases this owned model.
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
