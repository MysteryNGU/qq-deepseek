"""Real owned processes verify speech reuse, cancellation and GPU handover."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace
import wave

import psutil

from life.agent_session import SessionFailure
from life.media_adapters import AdapterRegistry, frozen_hash
from life.media_process import MediaProcess


class SpeechWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        source=(Path(__file__).parent/'fixtures/speech_adapter.py').read_text('utf-8')
        source=source.replace("cancel_grace_seconds=0)","cancel_grace_seconds=0,warm_worker=True)")
        source=source.replace("output=context.folder/'output.wav'", """import json, os, time
        cache=context.cache
        reused=cache is not None and 'engine' in cache
        if cache is not None:
            cache['engine']=True
        (context.folder/'reuse.json').write_text(json.dumps(dict(pid=os.getpid(),reused=reused)))
        time.sleep(context.config.get('delay',0))
        output=context.folder/'output.wav'""")
        self.adapter=self.root/'adapter.py';self.adapter.write_text(source,encoding='utf-8')
        self.config=dict(root=str(self.root),min_available_ram_mib=1,min_free_vram_mib=4096,
            timeout_seconds=10,speech_idle_seconds=300,providers=[dict(provider_id='speech',path=str(self.adapter),
                adapter_version='1',model_version='fixture',config={},environment=dict(python=sys.executable))])
        self.backend=MediaProcess(self.config)
        self.reference=self.root/'reference.wav'
        with wave.open(str(self.reference),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000);output.writeframes(bytes(6400))

    async def asyncTearDown(self):
        await self.backend.close()
        self.temp.cleanup()

    def request(self,name,delay=0):
        self.config['providers'][0]['config']['delay']=delay
        provider=AdapterRegistry(self.config,self.root).resolve('speech')
        spec=dict(kind='speech',text='Synthetic test',seed=123,parameters=dict(tone=330),_adapter=provider,
            reference_sha256=hashlib.sha256(self.reference.read_bytes()).hexdigest())
        resolved=dict(spec,reference_path=str(self.reference))
        folder=self.root/name;folder.mkdir()
        return dict(spec=resolved,frozen_spec=spec,frozen_spec_hash=frozen_hash(spec),request_hash=name,job_id=name),folder

    async def test_consecutive_jobs_reuse_process_and_cache_with_fresh_results(self):
        pids=[]
        for name,reused in [('first',False),('second',True)]:
            request,folder=self.request(name)
            result=await self.backend.run(request,folder,lambda:None)
            trace=json.loads((folder/'reuse.json').read_text())
            self.assertEqual(trace['reused'],reused)
            self.assertEqual(result['job_id'],name)
            self.assertEqual(result['actual_parameters'],request['frozen_spec'])
            pids.append(trace['pid'])
            self.assertFalse((folder/'process-owner.json').exists())
        self.assertEqual(pids[0],pids[1])
        self.assertTrue((self.root/'.speech-worker/process-owner.json').exists())
        await self.backend.close()
        self.assertFalse(psutil.pid_exists(pids[0]))

    async def test_cancel_kills_resident_process_and_next_job_gets_fresh_engine(self):
        request,folder=self.request('cancel',delay=3)
        cancelled=False
        task=asyncio.create_task(self.backend.run(request,folder,lambda:'explicit_cancel' if cancelled else None))
        for _ in range(300):
            if (folder/'reuse.json').exists():break
            await asyncio.sleep(.01)
        self.assertTrue((folder/'reuse.json').exists())
        pid=json.loads((folder/'reuse.json').read_text())['pid'];cancelled=True
        with self.assertRaises(SessionFailure) as failure:
            await task
        self.assertEqual(failure.exception.code,'cancelled')
        self.assertFalse(psutil.pid_exists(pid))
        self.assertFalse((folder/'result.json').exists())
        request,folder=self.request('after-cancel')
        await self.backend.run(request,folder,lambda:None)
        self.assertFalse(json.loads((folder/'reuse.json').read_text())['reused'])

    async def test_image_handover_and_idle_timeout_stop_the_owned_worker(self):
        request,folder=self.request('handover')
        await self.backend.run(request,folder,lambda:None)
        pid=json.loads((folder/'reuse.json').read_text())['pid']
        await self.backend.prepare(dict(request['spec'],kind='image'))
        self.assertFalse(psutil.pid_exists(pid))
        request,folder=self.request('idle')
        await self.backend.run(request,folder,lambda:None)
        self.backend._warm['last_used']=time.monotonic()-301
        await self.backend.maintenance()
        self.assertIsNone(self.backend._warm)

    async def test_tampered_reference_never_uses_previous_cached_voice(self):
        request,folder=self.request('good')
        await self.backend.run(request,folder,lambda:None)
        request,folder=self.request('tampered')
        self.reference.write_bytes(b'changed after request was frozen')
        with self.assertRaises(SessionFailure):
            await self.backend.run(request,folder,lambda:None)
        self.assertIsNone(self.backend._warm)
        self.assertFalse((folder/'output.wav').exists())

    async def test_provider_change_restarts_worker_and_does_not_reuse_engine(self):
        request,folder=self.request('old')
        await self.backend.run(request,folder,lambda:None)
        old_pid=json.loads((folder/'reuse.json').read_text())['pid']
        self.adapter.write_text(self.adapter.read_text('utf-8')+'\n# new installed revision\n',encoding='utf-8')
        request,folder=self.request('new')
        await self.backend.run(request,folder,lambda:None)
        self.assertFalse(psutil.pid_exists(old_pid))
        self.assertFalse(json.loads((folder/'reuse.json').read_text())['reused'])

    async def test_cached_cuda_workspace_counts_as_available_for_resident_admission(self):
        self.backend._warm=dict(cuda=dict(cached_free_mib=1024,workspace_peak_mib=500))
        with patch('life.media_process.psutil.virtual_memory',return_value=SimpleNamespace(available=10*2**30)), \
                patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='100\n')):
            self.assertIsNone(await self.backend.resources_ready())
            self.backend._warm=None
            self.assertEqual(await self.backend.resources_ready(),'gpu_memory_low')

    async def test_resident_pressure_unloads_before_waiting_for_cold_memory(self):
        self.backend._warm=dict(cuda=dict(cached_free_mib=100,workspace_peak_mib=500))
        async def stop():self.backend._warm=None
        close=AsyncMock(side_effect=stop)
        with patch.object(self.backend,'close',close), \
                patch('life.media_process.psutil.virtual_memory',return_value=SimpleNamespace(available=10*2**30)), \
                patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='100\n')):
            self.assertEqual(await self.backend.resources_ready(),'gpu_memory_low')
            close.assert_awaited_once()
        self.assertIsNone(self.backend._warm)

    async def test_recovery_stops_only_recorded_resident_worker(self):
        request,folder=self.request('recover')
        await self.backend.run(request,folder,lambda:None)
        pid=json.loads((folder/'reuse.json').read_text())['pid']
        replacement=MediaProcess(self.config)
        await replacement.recover_root(self.root)
        self.assertFalse(psutil.pid_exists(pid))
        self.assertFalse((self.root/'.speech-worker/process-owner.json').exists())


if __name__=='__main__':
    unittest.main()
