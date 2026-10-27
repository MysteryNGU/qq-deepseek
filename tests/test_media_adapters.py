"""Independent installed packages exercise the real managed worker and old send path."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest

from tests.role_fixture import Fixture
from tests.test_local_media import wav_bytes
from life.local_media import LocalMedia
from life.agent_session import SessionFailure,canonical


class MediaAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.root=self.f.root;self.notified=[]
        installed=self.root/'installed';installed.mkdir()
        for name in ('image_adapter.py','speech_adapter.py'):
            shutil.copyfile(Path(__file__).parent/'fixtures'/name,installed/name)
        self.declarations=[dict(provider_id='third-image',path=str(installed/'image_adapter.py'),
            adapter_version='v1',model_version='synthetic-image-v1',config={},environment=dict(python=sys.executable)),
            dict(provider_id='third-speech',path=str(installed/'speech_adapter.py'),adapter_version='v1',
            model_version='synthetic-speech-v1',config={},environment=dict(python=sys.executable))]
        self.cfg=SimpleNamespace(owner=1,local_media=dict(enabled=True,providers=self.declarations,
            defaults=dict(image='third-image',speech='third-speech'),min_available_ram_mib=1))
        self.service=self.make_service()
        self.f.role.media,self.f.role.voices=self.f.media,self.f.voices

    async def asyncTearDown(self):
        await self.service.close()

    def make_service(self):
        async def notify(event):
            self.notified.append(event)
        service=LocalMedia(self.f.life,self.f.media,self.f.voices,self.cfg,self.root,notify=notify)
        async def resources_ready():
            return None  # Only physical availability is nondeterministic; run uses the real subprocess owner.
        service.backend.resources_ready=resources_ready
        return service

    def submit(self,kind='image',key='fixture',**values):
        p=dict(kind=kind,prompt='A synthetic image') if kind=='image' else dict(kind=kind,text='A synthetic audition',voice_id='third-voice')
        p.update(values)
        return self.service.submit(self.f.scope,p,key,[self.f.event.id])

    async def test_A04_independent_image_package_generates_and_uses_existing_expression(self):
        receipt=self.submit(provider_options=dict(color='red'))
        self.assertEqual(receipt['provider_id'],'third-image')
        self.assertEqual(receipt['status'],'queued')
        await self.service.run_one()
        status=self.service.status(self.f.scope,receipt['job_id'])
        self.assertEqual(status['status'],'completed')
        asset=self.f.media.require(self.f.scope,status['asset_id'])
        metadata=json.loads(asset['metadata'])
        self.assertEqual(metadata['inspection'],'not_observed')
        async def transport(_action,params):
            self.f.sent.append(params);return dict(status='ok',retcode=0,data=dict(message_id=123))
        self.f.expressions.transport=transport
        sent=await self.f.expressions(self.f.scope,'existing-send',dict(parts=[dict(kind='image',asset_id=asset['id'])]),[self.f.event.id])
        self.assertTrue(self.f.sent)
        self.assertTrue(sent['sent_confirmed'])
        self.assertEqual(len(self.notified),1)

    async def test_A04_independent_voice_parameters_registration_audition_and_default(self):
        reference=self.f.media.register(self.f.scope,self.f.event.id,'voice-reference','audio',wav_bytes(),'audio/wav')
        self.f.role.local_media=self.service
        audition=await self.f.role.change_voice(self.f.scope,dict(action='register',sources=[self.f.event.id],
            voice=dict(id='third-voice',engine='fixture-speech',provider_id='third-speech',reference_asset_id=reference['id'],
                parameters=dict(tone=440))),self.f.binding,'audition-new-provider')
        with self.assertRaises(SessionFailure):
            self.f.voices.set_default(self.f.scope,'third-voice',owner=True)
        receipt=audition
        await self.service.run_one()
        status=self.service.status(self.f.scope,receipt['job_id'])
        self.assertEqual(status['status'],'completed')
        self.assertEqual(self.f.voices.require('third-voice',scope=self.f.scope)['status'],'verified')
        self.f.voices.set_default(self.f.scope,'third-voice',owner=True)
        self.assertEqual(self.f.voices.default(self.f.scope),'third-voice')
        self.assertEqual(self.f.voices.local_record('third-voice',1,self.f.scope)['parameters'],dict(tone=440))

    async def test_A05_false_success_wrong_hash_mime_and_request_are_rejected(self):
        for index,field in enumerate(('sha256','request_hash','frozen_spec_hash','provider_digest','job_id','actual_parameters','media')):
            original=self.service.backend.run
            async def corrupt(*args,_field=field,_original=original):
                result=await _original(*args);result[_field]='forged';return result
            self.service.backend.run=corrupt
            receipt=self.submit(key='corrupt-'+str(index))
            await self.service.run_one()
            self.assertEqual(self.service.status(self.f.scope,receipt['job_id'])['status'],'failed',field)
            self.service.backend.run=original
        self.assertFalse(self.f.life.repo._one("SELECT 1 FROM events WHERE kind='media_generated'"))

    async def test_A08_cancellation_reaches_real_process_stop_and_releases_lease(self):
        self.declarations[0]['config']['delay']=2
        self.service=self.make_service()
        receipt=self.submit();run=asyncio.create_task(self.service.run_one())
        owner=self.service.root/receipt['job_id']/'process-owner.json'
        while not owner.exists():
            await asyncio.sleep(.01)
        self.service.cancel(self.f.scope,receipt['job_id'])
        await run
        self.assertEqual(self.service.status(self.f.scope,receipt['job_id'])['status'],'cancelled')
        self.assertFalse(self.f.life.repo._one('SELECT 1 FROM resource_leases'))
        self.assertFalse(owner.exists())

    async def test_A10_upgrade_cancels_existing_job_and_uses_only_new_provider(self):
        old=self.submit(key='old')
        old_spec=json.loads(self.f.life.repo._one('SELECT payload FROM work_jobs WHERE id=?',(old['job_id'],))['payload'])['spec']
        replacement=self.root/'installed/image-v2.py'
        shutil.copyfile(self.root/'installed/image_adapter.py',replacement)
        replacement.write_text(replacement.read_text('utf-8')+'\n# Distinct installed revision.\n',encoding='utf-8')
        self.declarations[0].update(path=str(replacement),adapter_version='v2',model_version='synthetic-image-v2')
        # Upgrades discard pending inference instead of preserving old code copies.
        (self.root/'installed/image_adapter.py').write_text('raise RuntimeError("old installation replaced")',encoding='utf-8')
        self.service=self.make_service()
        await self.service.recover()
        new=self.submit(key='new')
        await self.service.run_one();await self.service.run_one()
        self.assertEqual(self.service.status(self.f.scope,old['job_id'])['provider_revision'],'v1')
        self.assertEqual(self.service.status(self.f.scope,new['job_id'])['provider_revision'],'v2')
        self.assertEqual(self.service.status(self.f.scope,old['job_id'])['status'],'cancelled')
        self.assertEqual(self.service.status(self.f.scope,new['job_id'])['status'],'completed')
        self.assertFalse((self.service.root/'adapters').exists())
        self.assertEqual(old_spec,json.loads(self.f.life.repo._one('SELECT payload FROM work_jobs WHERE id=?',(old['job_id'],))['payload'])['spec'])

    async def test_installed_package_resources_and_relative_helpers_survive_real_worker(self):
        adapter=self.root/'installed/image_adapter.py'
        source=adapter.read_text('utf-8')
        source=source.replace('from pathlib import Path','from pathlib import Path\nfrom .helper import COLOR')
        source=source.replace("output=context.folder/'output.png'", "assert Path(__file__).with_name('recipe.json').read_text('utf-8') == COLOR\n        output=context.folder/'output.png'")
        adapter.write_text(source,encoding='utf-8')
        (adapter.parent/'helper.py').write_text("COLOR='blue'\n",encoding='utf-8')
        (adapter.parent/'recipe.json').write_text('blue',encoding='utf-8')
        self.service=self.make_service()
        receipt=self.submit()
        await self.service.run_one()
        self.assertEqual(self.service.status(self.f.scope,receipt['job_id'])['status'],'completed')
        # The same package must also work as an importable module in a fresh worker
        # whose sys.path initially does not contain the temporary installation.
        (adapter.parent/'__init__.py').write_text('',encoding='utf-8')
        sys.path.insert(0,str(self.root))
        self.addCleanup(lambda:sys.path.remove(str(self.root)))
        def clear():
            for name in list(sys.modules):
                if name=='installed' or name.startswith('installed.'):
                    sys.modules.pop(name,None)
        self.addCleanup(clear)
        self.declarations[0].pop('path')
        self.declarations[0]['module']='installed.image_adapter'
        self.service=self.make_service()
        receipt=self.submit(key='module-package')
        await self.service.run_one()
        self.assertEqual(self.service.status(self.f.scope,receipt['job_id'])['status'],'completed')

    def test_module_reload_sees_same_size_source_and_helper_updates(self):
        import importlib
        import os
        import py_compile
        from life.media_adapters import AdapterRegistry,load_adapter
        package=self.root/'review_package';package.mkdir()
        (package/'__init__.py').write_text('',encoding='utf-8')
        entry=package/'adapter.py';helper=package/'helper.py'
        helper.write_text('VALUE="v1"\n',encoding='utf-8')
        entry.write_text('from .helper import VALUE\nclass Adapter:\n    value = VALUE\n    revision = "v1"\n',encoding='utf-8')
        sys.path.insert(0,str(self.root))
        self.addCleanup(lambda:sys.path.remove(str(self.root)))
        def clear():
            for name in list(sys.modules):
                if name=='review_package' or name.startswith('review_package.'):
                    sys.modules.pop(name,None)
        self.addCleanup(clear)
        importlib.import_module('review_package.adapter')
        for path in (helper,entry):
            py_compile.compile(str(path),doraise=True)
            stamp=path.stat()
            path.write_text(path.read_text('utf-8').replace('v1','v2'),encoding='utf-8')
            os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        reference=dict(path=str(entry),digest=hashlib.sha256(entry.read_bytes()).hexdigest(),
            module='review_package.adapter',import_root=str(self.root))
        current=load_adapter(reference)
        self.assertEqual((current.value,current.revision),('v2','v2'))

    def test_removed_media_config_fields_are_rejected(self):
        from life.local_media_config import LocalMediaConfig
        with self.assertRaises(ValueError):
            LocalMediaConfig.parse(dict(image_server='obsolete'))

    async def test_A10_existing_change_api_activates_a_provider_config_and_original_pump(self):
        from life.self_change import SelfChange
        from life.dsh_changes import process_queued
        self.f.role.local_media=self.service
        self.f.role._self_change=SelfChange(self.f.life,self.f.cfg,self.root)
        self.f.task()
        lines=['owner = 1','[local_media]','enabled = true','min_available_ram_mib = 1']
        for declaration in self.declarations:
            lines.append('[[local_media.providers]]')
            for key in ('provider_id','path','adapter_version','model_version'):
                lines.append(key+' = '+json.dumps(declaration[key]))
            lines.append('[local_media.providers.environment]')
            lines.append('python = '+json.dumps(sys.executable))
        (self.root/'config.toml').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        await self.f.role._self_change.apply(self.f.scope,dict(target='worker',component='media',
            verification_goal='Generate the original synthetic image'),'refresh-media')
        await process_queued(self.f.role)
        receipt=json.loads(self.f.life.repo._one("SELECT receipt FROM self_changes WHERE id='refresh-media'")['receipt'])
        self.assertTrue(receipt['applied'])
        self.assertFalse(receipt['behavior_validated'])
        self.assertIsNotNone(self.service.task)
        async def ready():
            return None
        self.service.backend.resources_ready=ready
        job=self.submit(key='after-config-refresh')
        for _ in range(200):
            status=self.service.status(self.f.scope,job['job_id'])
            if status['status'] in {'completed','failed'}:
                break
            await asyncio.sleep(.02)
        self.assertEqual(status['status'],'completed')

    def test_A09_catalog_is_lightweight_and_schema_rejects_ignored_or_reserved_options(self):
        self.assertNotIn('torch',sys.modules)
        items=self.service.catalog(self.f.scope)
        self.assertEqual({i['name'] for i in items},{'third-image','third-speech'})
        for options in (dict(mask='ignored'),dict(color='green'),dict(python='untrusted'),dict(width=512)):
            with self.assertRaises(SessionFailure):
                self.submit(key=canonical(options),provider_options=options)
        self.assertFalse(self.f.life.repo._one('SELECT 1 FROM work_jobs'))

    def test_A10_missing_provider_and_unknown_configuration_fail_explicitly(self):
        with self.assertRaises(SessionFailure):
            self.submit(provider_id='not-installed')
        self.declarations[0]['unrecognized']=True
        with self.assertRaises(ValueError):
            self.make_service()

    def test_runtime_upgrade_requires_explicit_old_digest_and_same_model_and_version(self):
        reference=self.f.media.register(self.f.scope,self.f.event.id,'voice-reference','audio',wav_bytes(),'audio/wav')
        self.f.voices.register(dict(id='third-voice',engine='fixture-speech',provider_id='third-speech',
            reference_asset_id=reference['id'],parameters=dict(tone=440)),self.f.scope)
        old_digest=self.service.adapters.resolve('speech')['digest']
        path=self.root/'installed/speech_adapter.py'
        path.write_text(path.read_text('utf-8')+'\n# runtime-only change\n',encoding='utf-8')
        self.service=self.make_service()
        proposal=dict(kind='speech',voice_id='third-voice',text='A synthetic sentence')
        with self.assertRaises(SessionFailure):
            self.service.prepare(self.f.scope,proposal)
        self.declarations[1]['runtime_compatible_digests']=[old_digest]
        self.service=self.make_service()
        prepared=self.service.prepare(self.f.scope,proposal)
        self.assertNotEqual(prepared['_adapter']['digest'],old_digest)
        record=self.f.voices.local_record('third-voice',1,self.f.scope)
        self.assertEqual(record['provider_digest'],old_digest)
        self.declarations[1]['model_version']='another-model'
        self.service=self.make_service()
        with self.assertRaises(SessionFailure):
            self.service.prepare(self.f.scope,proposal)
        self.declarations[1].update(model_version='synthetic-speech-v1',adapter_version='2')
        self.service=self.make_service()
        with self.assertRaises(SessionFailure):
            self.service.prepare(self.f.scope,proposal)
