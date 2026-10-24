import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import wave

from PIL import Image

from life.agent_session import SessionFailure
from life.local_media import LocalMedia,JOB_KIND
from life.media import MediaService
from life.runtime import LifeRuntime
from life.voice_catalog import VoiceCatalog


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(22050)
        audio.writeframes(b'\x01\x00\xff\xff'*1102)
    return buffer.getvalue()


class FakeBackend:
    def __init__(self):
        self.requests=[]
        self.wait=None
        self.waiting_reason=None
        self.error=None
        self.stopped=False

    async def resources_ready(self):
        return self.waiting_reason

    async def run(self,request,folder,check):
        self.requests.append(request)
        try:
            while self.wait and not self.wait.is_set():
                if check():
                    raise SessionFailure('cancelled','owned fake inference stopped')
                await asyncio.sleep(.001)
            if self.error:
                raise SessionFailure('engine_failed','fixture engine failed')
            if request['spec']['kind']=='speech':
                path=folder/'output.wav';path.write_bytes(wav_bytes())
            else:
                path=folder/'output.png';Image.new('RGB',(1024,1024),(25,90,190)).save(path)
            from life.media_worker import output_metadata
            spec=request['spec'];provider=spec.get('_adapter')
            result=dict(status='completed',output=path.name,request_hash=request['request_hash'],
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            if provider:
                result.update(frozen_spec_hash=request.get('frozen_spec_hash'),job_id=request.get('job_id'),
                    provider_digest=provider['digest'],provider_revision=provider['adapter_version'],
                    actual_parameters=request.get('frozen_spec',spec),media=output_metadata(path,provider['description']))
            return result
        finally:
            self.stopped=True


class LocalMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.life=LifeRuntime(self.root/'life.db')
        self.scope=self.life.scope('qq:private:1','1')
        self.other=self.life.scope('qq:group:3','')
        self.media=MediaService(self.life,self.root/'assets')
        self.voices=VoiceCatalog(self.life,self.media)
        self.cfg=SimpleNamespace(owner=1,local_media=dict(enabled=True,providers=[
            dict(provider_id='qwen-sdcpp',module='life.media_adapters.qwen_sdcpp',adapter_version='1',
                model_version='image-fixed-v1',environment=dict(python='python.exe'),
                config=dict(image_server='sd-server.exe',image_model_dir='image-model',image_max_vram_gib=5.0,worker_threads=4)),
            dict(provider_id='indextts',module='life.media_adapters.indextts',adapter_version='1',
                model_version='voice-fixed-v1',environment=dict(python='python.exe'),
                config=dict(voice_engine_root='index-tts',voice_model_dir='tts-model',max_text_chars=1000,worker_threads=4))]))
        self.backend=FakeBackend();self.notified=[]
        async def notify(event):
            self.notified.append(event)
        self.service=LocalMedia(self.life,self.media,self.voices,self.cfg,self.root,notify=notify,backend=self.backend)
        self.source,_=self.life.ingest_message(self.scope,'A real request')
        reference=self.media.register(self.scope,self.source.id,'reference','audio',wav_bytes(),'audio/wav')
        self.voices.register(dict(id='taotao',engine='indextts-2.5',engine_version='voice-fixed-v1',
            reference_asset_id=reference['id'],parameters=dict(emo_vector=[.25,0,0,0,0,0,0,.2],duration_factor=1.04)),self.scope)
        self.voices.verified_local(self.scope,'taotao',1,reference['id'])
        self.voices.set_default(self.scope,'taotao',owner=True)

    async def asyncTearDown(self):
        await self.service.close()
        self.life.close();self.temp.cleanup()

    def submit(self,key='request-1',**values):
        return self.service.submit(self.scope,dict(dict(kind='image',prompt='blue mug'),**values),key,[self.source.id])

    def enable_continuity(self):
        from life.role_agent import RoleAgent
        (self.root/'persona.md').write_text('Synthetic character',encoding='utf-8')
        self.cfg.continuity_enabled=True
        self.cfg.persona='persona.md'
        self.cfg.public_tools=[]
        bridge=SimpleNamespace(_generation=1,continuity_protocol=1,model='fixture')
        async def send(*args):
            return dict(sent_confirmed=False)
        role=RoleAgent(self.life,bridge,self.cfg,self.root,send,self.media,self.voices)
        role.local_media=self.service
        root=role.sessions.binding(self.scope)
        with self.life.repo.tx():
            role.intent(self.scope,dict(goal='一次自主绘画尝试',kind='ongoing',origin='self',sources=[self.source.id]),root)
        self.service.initiative=role.initiative
        return role,role.sessions.require(root['session_id'])

    async def test_initiative_queue_does_not_hold_lane_and_one_unfinished_job_per_run(self):
        role,root=self.enable_continuity()
        first=self.submit('autonomous-1')
        second=self.submit('autonomous-2',prompt='another image')
        self.assertEqual(first['job_id'],second['job_id'])
        self.assertIsNone(role.initiative.get(role.initiative.slot_key()))
        self.backend.wait=asyncio.Event()
        running=asyncio.create_task(self.service.run_one())
        while not self.backend.requests:
            await asyncio.sleep(.001)
        self.assertIn(first['job_id'],role.initiative.get(role.initiative.slot_key())['members'])
        interrupted,_=self.life.ingest_message(self.scope,'new interruption')
        role.stage_input(interrupted)
        self.assertTrue(role.sessions.pending(root['session_id']))
        self.backend.wait.set()
        await running
        self.assertFalse(role.initiative.get(role.initiative.slot_key())['members'])

    async def test_initiative_native_and_media_share_actual_execution_lane(self):
        role,root=self.enable_continuity()
        role.before_tool(root,self.scope,dict(tool='read_file',call_id='native',session_id=root['session_id'],arguments=dict(path='fixture')))
        queued=self.submit()
        self.assertFalse(await self.service.run_one())
        self.assertFalse(self.backend.requests)
        self.assertEqual(self.service.status(self.scope,queued['job_id'])['waiting_reason'],'resource_wait')
        from life.text import stable_id
        with self.life.repo.tx():
            role.initiative.finish_member(stable_id('roleop',root['session_id'],'native'))
            self.life.repo._exec('UPDATE work_jobs SET due=0 WHERE id=?',(queued['job_id'],))
        await self.service.run_one()
        self.assertEqual(len(self.backend.requests),1)

    async def test_initiative_media_failure_guard_suspends_without_model_polling(self):
        role,_root=self.enable_continuity()
        self.backend.error=True
        for i in range(3):
            self.submit('failure-'+str(i))
            await self.service.run_one()
        guard=role.initiative.guards(self.scope)[0]
        self.assertEqual((guard['count'],guard['phase']),(3,'suspended'))
        fourth=self.submit('fourth')
        await self.service.run_one()
        self.assertEqual(len(self.backend.requests),3)
        self.assertEqual(self.service.status(self.scope,fourth['job_id'])['status'],'waiting')
        self.assertFalse(role.initiative.get(role.initiative.slot_key())['members'])
        self.assertEqual(self.service.cancel(self.scope,fourth['job_id'])['status'],'cancelled')
        task=role.tasks.get(_root['intent_ref'],self.scope.scope_id)
        guard=role.initiative.guards(self.scope)[0]
        with self.life.repo.tx():
            role.initiative.request_probe(self.scope,task,dict(guard_ref=guard['ref'],base_version=guard['version'],
                change_note='改用另一份具体提示作一次探测',changed_source_refs=[],
                candidate=dict(tool='life_media',arguments=dict(kind='image',prompt='different controlled method'))))
        self.submit('probe-media',prompt='different controlled method')
        await self.service.run_one()
        self.assertEqual(len(self.backend.requests),4)
        updated=role.initiative.get(guard['ref'])
        self.assertTrue(updated['probe']['consumed'])
        self.assertEqual(updated['phase'],'suspended')

    async def test_initiative_restart_reconciles_physical_stop_before_lane_release(self):
        from unittest.mock import AsyncMock
        role,_root=self.enable_continuity()
        job=self.submit()
        lease=self.life.jobs.claim(job['job_id'],kinds=(JOB_KIND,))
        self.life.jobs.acquire(lease,'gpu:0')
        with self.life.repo.tx():
            role.initiative.begin_job(self.scope,lease.id,lease.payload)
        self.backend.reconcile_process=AsyncMock(return_value=None)
        await self.service.recover()
        self.backend.reconcile_process.assert_awaited_once()
        self.assertEqual(self.service.status(self.scope,job['job_id'])['status'],'unknown')
        self.assertFalse(role.initiative.get(role.initiative.slot_key())['members'])
        self.assertFalse(self.life.repo._one("SELECT 1 FROM resource_leases WHERE resource='gpu:0'"))
        self.assertEqual(role.initiative.guards(self.scope)[0]['phase'],'outcome_unknown')

    async def test_disabling_continuity_keeps_queued_background_work_dormant(self):
        role,_root=self.enable_continuity()
        job=self.submit()
        self.cfg.continuity_enabled=False
        await self.service.run_one()
        self.assertFalse(self.backend.requests)
        self.assertEqual(self.service.status(self.scope,job['job_id'])['status'],'waiting')

    async def test_pre_activation_queue_is_adopted_before_actual_execution(self):
        queued=self.submit('legacy-queued')
        role,root=self.enable_continuity()
        role.before_tool(root,self.scope,dict(tool='read_file',call_id='native',session_id=root['session_id'],arguments=dict(path='fixture')))
        self.assertFalse(await self.service.run_one())
        self.assertFalse(self.backend.requests)
        from life.text import stable_id
        with self.life.repo.tx():
            role.initiative.finish_member(stable_id('roleop',root['session_id'],'native'))
            self.life.repo._exec('UPDATE work_jobs SET due=0 WHERE id=?',(queued['job_id'],))
        await self.service.run_one()
        self.assertEqual(len(self.backend.requests),1)
        payload=json.loads(self.life.repo._one('SELECT payload FROM work_jobs WHERE id=?',(queued['job_id'],))['payload'])
        self.assertEqual(payload['initiative']['classification'],'autonomous')

    async def test_submit_is_immediate_idempotent_and_result_returns_to_role_without_sending(self):
        receipt=self.submit()
        self.assertEqual(receipt['status'],'queued');self.assertFalse(self.backend.requests)
        self.assertEqual(self.submit()['job_id'],receipt['job_id'])
        self.life.jobs.enqueue(self.scope,'unrelated-work',{},key='not-media')
        await self.service.run_one()
        result=self.service.status(self.scope,receipt['job_id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.media.require(self.scope,result['asset_id'])['mime'],'image/png')
        self.assertEqual(len(self.notified),1)
        self.assertEqual(self.notified[0].kind,'media_result')
        self.assertEqual(self.life.repo._one("SELECT status FROM work_jobs WHERE id='not-media'")['status'],'pending')
        self.assertEqual(self.submit()['asset_id'],result['asset_id'])
        with self.assertRaises(SessionFailure):self.submit(prompt='changed')

    async def test_default_voice_and_revision_are_frozen_and_private(self):
        receipt=self.service.submit(self.scope,dict(kind='speech',text='hello'),'speech',[self.source.id])
        await self.service.run_one()
        result=self.service.status(self.scope,receipt['job_id'])
        self.assertEqual(result['status'],'completed')
        spec=self.backend.requests[0]['spec']
        self.assertEqual((spec['voice_id'],spec['voice_revision']),('taotao',1))
        self.assertEqual(self.media.require(self.scope,result['asset_id'])['mime'],'audio/wav')
        self.assertIsNone(self.voices.default(self.other))
        self.assertFalse(self.voices.list(self.other))
        for action in (lambda:self.voices.require('taotao',False,self.other),
                       lambda:self.service.status(self.other,receipt['job_id'])):
            with self.assertRaises(SessionFailure):action()

    async def test_reference_edit_uses_scoped_asset_digest_and_rejects_other_audience(self):
        image=io.BytesIO();Image.new('RGB',(2,2)).save(image,format='PNG')
        asset=self.media.register(self.scope,self.source.id,'image','image',image.getvalue(),'image/png')
        receipt=self.submit(reference_asset_ids=[asset['id']])
        await self.service.run_one()
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'completed')
        self.assertEqual(self.backend.requests[0]['spec']['references'][0]['sha256'],asset['digest'])
        other_source,_=self.life.ingest_message(self.other,'new request')
        with self.assertRaises(SessionFailure):
            self.service.submit(self.other,dict(kind='image',prompt='edit',reference_asset_ids=[asset['id']]),'other',[other_source.id])

    async def test_cancelled_running_job_releases_gpu_only_after_owned_work_stops(self):
        self.backend.wait=asyncio.Event()
        receipt=self.submit();running=asyncio.create_task(self.service.run_one())
        while not self.backend.requests:await asyncio.sleep(.001)
        cancelled=self.service.cancel(self.scope,receipt['job_id'])
        self.assertTrue(cancelled['cancel_requested']);self.assertEqual(cancelled['status'],'generating')
        self.assertTrue(self.life.repo._one("SELECT 1 FROM resource_leases WHERE resource='gpu:0'"))
        await running
        self.assertTrue(self.backend.stopped)
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'cancelled')
        self.assertFalse(self.life.repo._one("SELECT 1 FROM resource_leases WHERE resource='gpu:0'"))
        self.assertFalse(self.life.repo._one("SELECT 1 FROM media_assets WHERE part_id='output'"))

    async def test_queue_cancel_memory_wait_and_engine_failure_do_not_retry_inference(self):
        self.service.resource_clock=lambda:10
        receipt=self.submit();self.service.cancel(self.scope,receipt['job_id'])
        await self.service.run_one();self.assertFalse(self.backend.requests)
        receipt=self.submit('wait');self.backend.waiting_reason='system_memory_low'
        await self.service.run_one()
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['waiting_reason'],'system_memory_low')
        self.backend.waiting_reason=None;self.backend.error=True
        self.service.resource_clock=lambda:15
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE work_jobs SET due=0 WHERE id=?",(receipt['job_id'],))
        await self.service.run_one();await self.service.run_one()
        self.assertEqual(len(self.backend.requests),1)
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'failed')

    async def test_restart_marks_incomplete_inference_unknown_without_replaying(self):
        receipt=self.submit();lease=self.life.jobs.claim(kinds=(JOB_KIND,))
        self.life.jobs.acquire(lease,'gpu:0')
        await self.service.recover()
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'unknown')
        await self.service.run_one();self.assertFalse(self.backend.requests)
        await self.service.recover();self.assertEqual(len(self.notified),1)

    async def test_restart_recovers_a_valid_completed_file_without_repeating_inference(self):
        receipt=self.submit();lease=self.life.jobs.claim(kinds=(JOB_KIND,))
        folder=self.service.root/lease.id;folder.mkdir(parents=True)
        result=await self.backend.run(dict(spec=lease.payload['spec'],request_hash=lease.payload['request_hash'],
            frozen_spec=lease.payload['spec'],frozen_spec_hash=lease.payload['frozen_spec_hash'],job_id=lease.id),folder,lambda:None)
        (folder/'result.json').write_text(json.dumps(result),encoding='utf-8')
        await self.service.recover()
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'completed')
        self.assertEqual(len(self.backend.requests),1)
        self.assertEqual(len(self.notified),1)

    async def test_unconfirmed_stop_blocks_gpu_handover(self):
        async def unknown(*args):
            raise SessionFailure('stop_unknown','fixture child still running')
        self.backend.run=unknown
        receipt=self.submit();await self.service.run_one()
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'unknown')
        self.assertTrue(self.service.blocked_reason)
        self.submit('next');self.assertFalse(await self.service.run_one())
        self.assertGreater(self.life.repo._one("SELECT until FROM resource_leases WHERE resource='gpu:0'")['until'],1e10)

    async def test_speech_pause_and_immutable_revision_are_preserved(self):
        receipt=self.service.submit(self.scope,dict(kind='speech',segments=[
            dict(text='first',pause_after_ms=250),dict(text='second',delivery={'duration_factor':1.2})]),'segments',[self.source.id])
        await self.service.run_one()
        segments=self.backend.requests[0]['spec']['segments']
        self.assertEqual(segments[0]['pause_after_ms'],250)
        self.assertEqual(segments[1]['delivery']['duration_factor'],1.2)
        voice=self.voices.require('taotao',False,self.scope)
        record=dict(id='taotao',engine='indextts-2.5',engine_version='voice-fixed-v1',reference_asset_id=voice['reference_asset_id'])
        with self.assertRaises(SessionFailure):self.voices.register(record,self.scope)
        record['revision']=2;self.voices.register(record,self.scope)
        self.assertEqual(self.voices.local_record('taotao',1,self.scope)['parameters']['duration_factor'],1.04)
        self.assertEqual(self.voices.default_record(self.scope)['revision'],1)
        next_job=self.service.submit(self.scope,dict(kind='speech',text='still same default'),'unchanged-default',[self.source.id])
        stored=json.loads(self.life.repo._one('SELECT payload FROM work_jobs WHERE id=?',(next_job['job_id'],))['payload'])
        self.assertEqual(stored['spec']['voice_revision'],1)
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'completed')

    async def test_source_invalidation_during_inference_never_publishes_or_sends(self):
        self.backend.wait=asyncio.Event();receipt=self.submit()
        task=asyncio.create_task(self.service.run_one())
        while not self.backend.requests:await asyncio.sleep(.001)
        with self.life.repo.tx():
            self.life.repo._exec('UPDATE events SET redacted_at=1 WHERE id=?',(self.source.id,))
        await task
        self.assertEqual(self.life.repo._one('SELECT status FROM work_jobs WHERE id=?',(receipt['job_id'],))['status'],'cancelled')
        self.assertFalse(self.notified)
        self.assertFalse(self.life.repo._one("SELECT 1 FROM resource_leases WHERE resource='gpu:0'"))

    async def test_model_completion_contains_the_actual_asset_reference_for_the_role(self):
        self.submit();await self.service.run_one()
        event=self.notified[0]
        self.assertIn(event.payload['asset_id'],event.text)
        self.assertEqual(event.payload['parts'][1]['asset_id'],event.payload['asset_id'])

    async def test_revised_intention_cancels_its_old_queued_media(self):
        from life.tasks import TaskService
        tasks=TaskService(self.life.repo)
        tasks.ensure(self.scope,'drawing','original drawing',revision=1)
        receipt=self.service.submit(self.scope,dict(kind='image',prompt='old prompt'),'old-target',[self.source.id],'drawing')
        tasks.ensure(self.scope,'drawing','a different drawing',revision=2)
        await self.service.run_one()
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'cancelled')
        self.assertFalse(self.backend.requests)

    async def test_cancellation_during_resource_probe_is_not_lost_to_requeue(self):
        gate=asyncio.Event();entered=asyncio.Event()
        async def resource_probe():
            entered.set();await gate.wait();return 'gpu_memory_low'
        self.backend.resources_ready=resource_probe
        receipt=self.submit();running=asyncio.create_task(self.service.run_one())
        await entered.wait();self.service.cancel(self.scope,receipt['job_id']);gate.set();await running
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'cancelled')
        self.assertFalse(self.backend.requests)

    async def test_role_tool_returns_job_immediately_and_allows_cancel_with_new_unhandled_input(self):
        from life.role_agent import RoleAgent
        bridge=SimpleNamespace(_generation=1,session_events=lambda *a,**kw:dict(events=[],has_more=False,agent_status='idle'))
        role=RoleAgent(self.life,bridge,self.cfg,self.root,None,self.media,self.voices)
        role.local_media=self.service
        binding,_=role.stage_input(self.source)
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE agent_inputs SET state='settled' WHERE session_id=?",(binding['session_id'],))
        def call(kind,cid,proposal):
            return dict(root_session_id=binding['session_id'],session_id=binding['session_id'],
                caller='role',kind=kind,call_id=cid,proposal=proposal)
        receipt=await role.callback(call('life.media','submit',dict(kind='speech',text='hello',
            handled_input_ids=[self.source.id],sources=[self.source.id])))
        self.assertTrue(receipt['ok'],receipt);self.assertEqual(receipt['status'],'queued')
        self.assertFalse(self.backend.requests)
        later,_=self.life.ingest_message(self.scope,'new message');role.stage_input(later)
        blocked=await role.callback(call('life.media','another',dict(kind='image',prompt='new')))
        self.assertEqual(blocked['code'],'input_pending')
        status=await role.callback(call('life.media','status',dict(action='status',job_id=receipt['job_id'])))
        self.assertTrue(status['ok'],status)
        cancelled=await role.callback(call('life.media','cancel',dict(action='cancel',job_id=receipt['job_id'])))
        self.assertTrue(cancelled['cancel_requested'])
        await self.service.run_one()
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'cancelled')

    async def test_local_registration_queues_audition_and_preserves_selected_default(self):
        from life.role_agent import RoleAgent
        role=RoleAgent(self.life,SimpleNamespace(),self.cfg,self.root,None,self.media,self.voices)
        role.local_media=self.service
        binding,_=role.stage_input(self.source)
        original=self.voices.require('taotao',False,self.scope)
        result=await role.change_voice(self.scope,dict(action='register',voice=dict(id='new-local',engine='indextts-2.5',
            reference_asset_id=original['reference_asset_id']),sample_text='test audition',sources=[self.source.id]),binding,'new-voice')
        self.assertEqual(result['status'],'queued')
        with self.assertRaises(SessionFailure):self.voices.set_default(self.scope,'new-local')
        await self.service.run_one()
        self.assertEqual(self.voices.require('new-local',scope=self.scope)['status'],'verified')
        self.assertEqual(self.voices.default(self.scope),'taotao')
        self.voices.set_default(self.scope,'new-local')
        self.assertEqual(self.voices.default(self.scope),'new-local')

    async def test_bad_delivery_and_mutated_reference_fail_before_dispatch(self):
        with self.assertRaises(SessionFailure):
            self.service.submit(self.scope,dict(kind='speech',text='hello',delivery={'whisper':True}),'bad',[self.source.id])
        voice=self.voices.require('taotao',False,self.scope)
        asset=self.media.require(self.scope,voice['reference_asset_id'])
        Path(asset['storage_ref']).write_bytes(b'changed')
        with self.assertRaises(SessionFailure):
            self.service.submit(self.scope,dict(kind='speech',text='hello'),'changed',[self.source.id])

    async def test_context_cancel_or_new_input_does_not_require_an_inference_wait(self):
        self.backend.wait=asyncio.Event();receipt=self.submit()
        running=asyncio.create_task(self.service.run_one())
        while not self.backend.requests:await asyncio.sleep(.001)
        new,_=self.life.ingest_message(self.scope,'continue chatting')
        self.assertTrue(self.life.repo.get_event(new.id))
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'generating')
        self.backend.wait.set();await running
        self.assertEqual(self.service.status(self.scope,receipt['job_id'])['status'],'completed')
