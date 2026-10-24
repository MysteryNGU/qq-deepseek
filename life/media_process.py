"""Owned engine process trees, bounded waits and physical GPU handover."""
import asyncio
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
            return 'gpu_memory_low' if float(result.stdout.splitlines()[0])<cfg['min_free_vram_mib'] else None
        try:
            return await asyncio.to_thread(gpu)
        except (OSError,ValueError,subprocess.TimeoutExpired):
            return 'gpu_probe_failed'

    async def run(self, request, folder, check):
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
                if owner['request'] not in command or not exact:
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
