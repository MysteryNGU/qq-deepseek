"""Owned engine process trees, bounded waits and physical GPU handover."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import time

import psutil

from .agent_session import SessionFailure
from .supervisor import ProcessTree


class MediaProcess:
    def __init__(self, config):
        self.config = config
        self._warm = None

    @staticmethod
    def warm_key(spec):
        provider=spec['_adapter']
        return hashlib.sha256(json.dumps(provider,sort_keys=True).encode()).hexdigest()

    async def prepare(self,spec):
        """Give the next provider physical ownership before checking free VRAM."""
        await self.maintenance()
        reusable=spec.get('kind')=='speech' and spec['_adapter']['description'].get('warm_worker') is True
        if self._warm and (not reusable or self._warm['key']!=self.warm_key(spec)):
            await self.close()

    async def maintenance(self):
        if self._warm and (self._warm['tree'].process.poll() is not None or
                time.monotonic()-self._warm['last_used']>=self.config.get('speech_idle_seconds',300) or
                psutil.virtual_memory().available/2**20<self.config['min_available_ram_mib']):
            await self.close()

    async def close(self):
        worker=self._warm
        if worker is None:
            return
        tree=worker['tree']
        def stop():
            try:
                children=psutil.Process(tree.process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children=[]
            tree.stop()
            _,alive=psutil.wait_procs(children,timeout=10)
            if alive:
                raise SessionFailure('stop_unknown','语音保温进程尚未确认退出，暂不释放 GPU')
        try:
            await asyncio.to_thread(stop)
        except (OSError,subprocess.TimeoutExpired) as error:
            raise SessionFailure('stop_unknown','语音保温进程停止未确认') from error
        for stream in (tree.process.stdin,tree.process.stdout,tree.process.stderr):
            if stream:
                stream.close()
        worker['log'].close()
        worker['owner'].unlink(missing_ok=True)
        self._warm=None

    async def recover_root(self,root):
        await self.reconcile_process(root/'.speech-worker')

    async def resources_ready(self):
        cfg = self.config
        if psutil.virtual_memory().available/2**20 < cfg['min_available_ram_mib']:
            return 'system_memory_low'
        def gpu():
            import subprocess
            result = subprocess.run(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits'],
                capture_output=True,text=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:
                return 'gpu_probe_failed'
            # CUDA's reusable allocator cache is reported as USED by nvidia-smi.
            # For a model that has completed inference, admit against its last
            # observed workspace peak plus a margin, counting that cache.
            resident=self._warm.get('cuda') if self._warm else None
            threshold=cfg['min_free_vram_mib']
            if resident:
                threshold=max(0,resident['workspace_peak_mib']+64-resident['cached_free_mib'])
            return 'gpu_memory_low' if float(result.stdout.splitlines()[0])<threshold else None
        try:
            reason=await asyncio.to_thread(gpu)
            if reason=='gpu_memory_low' and self._warm:
                await self.close()
                reason=await asyncio.to_thread(gpu)
            return reason
        except (OSError,ValueError,subprocess.TimeoutExpired):
            return 'gpu_probe_failed'

    async def run(self, request, folder, check):
        if request['spec'].get('kind')=='speech' and request['spec']['_adapter']['description'].get('warm_worker') is True:
            return await self._run_warm(request,folder,check)
        return await self._run_once(request,folder,check)

    async def _run_warm(self,request,folder,check):
        from .media_adapters import validate_frozen
        from .media_worker import atomic_json
        spec=request['spec'];validate_frozen(spec,load=False)
        await self.prepare(spec)
        cfg=self.config;provider=spec['_adapter'];environment_spec=provider['environment']
        marker=folder/'result.json';request_file=folder/'request.json'
        if marker.exists():
            raise SessionFailure('stale_context','作业已有结果，拒绝重复执行生成')
        request_file.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
        if self._warm is None:
            interpreter=Path(environment_spec['python'])
            if not interpreter.is_file():
                raise SessionFailure('unavailable','媒体引擎解释器不存在')
            allowed={'SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','HOMEDRIVE','HOMEPATH','HOME','PATH',
                     'HF_HOME','HF_HUB_CACHE','HUGGINGFACE_HUB_CACHE','TRANSFORMERS_CACHE','TORCH_HOME'}
            environment={k:v for k,v in os.environ.items() if k.upper() in allowed}
            environment.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
            entry=environment_spec.get('entry') or str(Path(__file__).with_name('media_worker.py'))
            command=[str(interpreter),'-u',entry,'--serve',str(folder.parent.resolve())]
            worker_dir=folder.parent/'.speech-worker';worker_dir.mkdir(exist_ok=True)
            log_file=(worker_dir/'worker.log').open('ab')
            try:
                tree=ProcessTree(command,cwd=worker_dir,env=environment,pipes=True,stdout_file=log_file,stderr_file=log_file)
            except BaseException:
                log_file.close();raise
            self._warm=dict(tree=tree,key=self.warm_key(spec),last_used=time.monotonic(),log=log_file,
                owner=worker_dir/'process-owner.json',command=command)
        worker=self._warm;tree=worker['tree']
        owner=dict(pid=tree.process.pid,created=psutil.Process(tree.process.pid).create_time(),
            request=str(request_file),command=worker['command'],persistent=True)
        atomic_json(worker['owner'],owner)
        atomic_json(folder/'process-owner.json',owner)
        started=time.monotonic();completed=False
        try:
            def dispatch():
                tree.process.stdin.write((json.dumps(dict(request=str(request_file.resolve())))+'\n').encode())
                tree.process.stdin.flush()
            await asyncio.to_thread(dispatch)
            low_memory=0
            while True:
                reason=check()
                if reason:
                    (folder/'cancel.requested').touch()
                    raise SessionFailure('cancelled','媒体任务已请求停止',reason=reason)
                if time.monotonic()-started>cfg['timeout_seconds']:
                    raise SessionFailure('engine_timeout','本地引擎超过本次执行时限')
                if marker.is_file():
                    result=json.loads(marker.read_text(encoding='utf-8'))
                    if result.get('request_hash')!=request['request_hash']:
                        raise SessionFailure('stale_context','引擎结果不对应本次请求')
                    if result.get('status')!='completed':
                        raise SessionFailure('engine_failed','本地引擎未完成生成',engine_error=result.get('error'))
                    memory=result.get('resident_cuda')
                    if isinstance(memory,dict) and all(isinstance(memory.get(key),(int,float)) and
                            0<=memory[key]<1024*1024 for key in ('cached_free_mib','workspace_peak_mib')):
                        worker['cuda']=memory
                    completed=True
                    return result
                if tree.process.poll() is not None:
                    raise SessionFailure('engine_failed','语音保温进程退出但没有结果记录',exit_code=tree.process.returncode)
                low_memory=low_memory+1 if psutil.virtual_memory().available/2**20<cfg['min_available_ram_mib'] else 0
                if low_memory>=3:
                    raise SessionFailure('resource_exhausted','系统可用内存持续不足，已停止本次生成')
                await asyncio.sleep(.1)
        finally:
            if not completed:
                await self.close()
            else:
                worker['last_used']=time.monotonic()
            request_file.unlink(missing_ok=True)
            (folder/'process-owner.json').unlink(missing_ok=True)

    async def _run_once(self, request, folder, check):
        cfg = self.config
        from .media_adapters import validate_frozen
        validate_frozen(request['spec'],load=False)
        provider=request['spec']['_adapter']
        environment_spec=provider['environment']
        interpreter = environment_spec['python']
        executable = Path(interpreter)
        if not executable.is_file():
            raise SessionFailure('unavailable','媒体引擎解释器不存在')
        request_file = folder/'request.json'
        request_file.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
        allowed = {'SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','HOMEDRIVE','HOMEPATH','HOME','PATH',
                   'HF_HOME','HF_HUB_CACHE','HUGGINGFACE_HUB_CACHE','TRANSFORMERS_CACHE','TORCH_HOME'}
        environment = {k:v for k,v in os.environ.items() if k.upper() in allowed}
        environment.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
        entry=environment_spec.get('entry') or str(Path(__file__).with_name('media_worker.py'))
        command = [str(executable),'-u',entry,str(request_file)]
        log_file = (folder/'engine.log').open('wb')
        try:
            tree = ProcessTree(command,cwd=folder,env=environment,pipes=True,stdout_file=log_file,stderr_file=log_file)
        except BaseException:
            log_file.close()
            raise
        owner = dict(pid=tree.process.pid,created=psutil.Process(tree.process.pid).create_time(),request=str(request_file),command=command)
        from .media_worker import atomic_json
        atomic_json(folder/'process-owner.json',owner)
        communication = asyncio.create_task(asyncio.to_thread(tree.process.communicate))
        started = time.monotonic()
        low_memory = 0
        try:
            while not communication.done():
                await asyncio.sleep(.5)
                reason = check()
                if reason:
                    (folder/'cancel.requested').touch()
                    grace=provider['description'].get('cancel_grace_seconds',0)
                    if grace:
                        try:
                            await asyncio.wait_for(asyncio.shield(communication),grace)
                        except asyncio.TimeoutError:
                            pass
                    raise SessionFailure('cancelled','媒体任务已请求停止',reason=reason)
                if time.monotonic()-started>cfg['timeout_seconds']:
                    raise SessionFailure('engine_timeout','本地引擎超过本次执行时限')
                low_memory = low_memory+1 if psutil.virtual_memory().available/2**20<cfg['min_available_ram_mib'] else 0
                if low_memory>=3:
                    raise SessionFailure('resource_exhausted','系统可用内存持续不足，已停止本次生成')
            await communication
            marker = folder/'result.json'
            if not marker.is_file():
                raise SessionFailure('engine_failed','引擎退出但没有结果记录',exit_code=tree.process.returncode)
            result = json.loads(marker.read_text(encoding='utf-8'))
            if result.get('request_hash')!=request['request_hash']:
                raise SessionFailure('stale_context','引擎结果不对应本次请求')
            if result.get('status')!='completed':
                raise SessionFailure('engine_failed','本地引擎未完成生成',engine_error=result.get('error'))
            return result
        finally:
            # Close the Windows job even after its parent exits, including any native children.
            def stop():
                try:
                    children = psutil.Process(tree.process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    children = []
                tree.stop()
                _, alive = psutil.wait_procs(children,timeout=10)
                if alive:
                    raise SessionFailure('stop_unknown','媒体子进程尚未确认退出，暂不释放 GPU')
            try:
                await asyncio.to_thread(stop)
            except (OSError,subprocess.TimeoutExpired) as error:
                raise SessionFailure('stop_unknown','媒体进程停止未能确认，保留 GPU 占用记录') from error
            finally:
                log_file.close()
            if not communication.done():
                await communication
            request_file.unlink(missing_ok=True)
            (folder/'process-owner.json').unlink(missing_ok=True)

    async def reconcile_process(self,folder):
        """The exclusive coordinator lock proves the former coordinator is gone."""
        marker = folder/'process-owner.json'
        if not marker.is_file():
            return
        try:
            owner = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError,ValueError) as error:
            raise SessionFailure('stop_unknown','旧媒体进程归属记录无法读取') from error
        def stop():
            try:
                process = psutil.Process(owner['pid'])
                if process.create_time()!=owner['created']:
                    return
                command = process.cmdline()
                exact=command==owner['command'] if owner.get('command') else any(Path(arg).name=='media_worker.py' for arg in command)
                persistent=owner.get('persistent') is True and '--serve' in command and str(folder.parent.resolve()) in command
                if (owner['request'] not in command and not persistent) or not exact:
                    raise SessionFailure('stop_unknown','旧媒体进程归属不明，需要核对，未终止该进程')
                owned = [*process.children(recursive=True),process]
                for item in reversed(owned):
                    try:
                        item.terminate()
                    except psutil.NoSuchProcess:
                        pass
                _,alive = psutil.wait_procs(owned,timeout=10)
                if alive:
                    raise SessionFailure('stop_unknown','旧媒体进程仍在运行')
            except psutil.NoSuchProcess:
                pass
        await asyncio.to_thread(stop)
        marker.unlink(missing_ok=True)
