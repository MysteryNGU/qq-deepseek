import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from life.character_manager import CharacterManager
from life.role_config import load_config
from life.role_host import RoleHost
from life.runtime import LifeRuntime
from life.settings import MASK,SettingsError,SettingsStore,encode_toml,fingerprint


class FakeRunner:
    def __init__(self):self.started=False;self.stopped=False
    def start(self):self.started=True
    def stop(self):self.stopped=True
    def tick(self):return 'observed'


class CharacterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'config.toml').write_text(encode_toml(dict(owner=1,ws_url='ws://localhost:3001',
            api_key='model-secret',dsh_root='vendor/dsh',known_people={'secret-person':33},life_agent='default')),encoding='utf-8')
        (self.root/'life').mkdir();(self.root/'life/runtime.py').write_text('# public code\n')
        (self.root/'bot.py').write_text('# entry\n');(self.root/'skills').mkdir()
        (self.root/'skills/private.md').write_text('private history')
        (self.root/'data').mkdir();(self.root/'data/life.db').write_bytes(b'original-memory')
        self.manager=CharacterManager(self.root,runner_factory=lambda _:FakeRunner(),monitor=False)

    def tearDown(self):self.manager.close();self.temp.cleanup()

    def values(self,identity='a',account=101,port=3101):
        return dict(id=identity,name=identity,bot_qq=account,owner=1,ws_url=f'ws://127.0.0.1:{port}',
                    ws_token='onebot-secret',persona='# '+identity+'\n\n## 我怎么说话\n我有自己的想法。\n')

    def test_create_copies_only_program_and_static_card(self):
        self.manager.create(self.values())
        root=self.manager.deployment('a');cfg=load_config(root/'config.toml')
        self.assertEqual(cfg.life_agent,'a');self.assertEqual(cfg.bot_qq,101)
        self.assertEqual(cfg.api_key,'model-secret');self.assertEqual(cfg.known_people,{'secret-person':33})
        self.assertEqual(cfg.dsh_home,'data/dsh-home');self.assertTrue((root/'bot.py').exists())
        self.assertFalse((root/'data/life.db').exists());self.assertFalse((root/'skills/private.md').exists())
        self.assertEqual((self.root/'data/life.db').read_bytes(),b'original-memory')
        snapshot=self.manager.snapshot()
        self.assertEqual(snapshot['characters'][0]['ws_token'],MASK)
        self.assertNotIn('model-secret',json.dumps(snapshot))

    def test_duplicate_accounts_and_equivalent_endpoints_rejected(self):
        self.manager.create(self.values())
        for other in (self.values('b',101,3102),self.values('b',102,3101),self.values('b',102,3001)):
            with self.assertRaises(SettingsError):self.manager.create(other)
        self.assertFalse(self.manager.deployment('b').exists())

    def test_saved_cards_can_be_registered_as_independent_unconfigured_people(self):
        folder=self.root/'personas';folder.mkdir()
        for name in ('xiaoyu.md','kafka.md','firefly.md'):(folder/name).write_text('# '+name+'\n## 我怎么说话\n自然交流。',encoding='utf-8')
        self.manager.register_templates();self.manager.register_templates()
        people=self.manager.snapshot()['characters']
        self.assertEqual([p['id'] for p in people],['xiaoyu','kafka','firefly'])
        self.assertEqual([p['name'] for p in people],['原始小鱼','卡夫卡','流萤'])
        self.assertTrue(all(not p['ready'] and not p['wanted'] and p['state']=='unconfigured' for p in people))
        self.assertTrue(all(not (self.manager.deployment(p['id'])/'data/life.db').exists() for p in people))
        with self.assertRaises(SettingsError):self.manager.action('firefly','start')
        person=next(p for p in people if p['id']=='firefly')
        edit=dict(name=person['name'],bot_qq=102,owner=1,ws_url='ws://127.0.0.1:3102',ws_token='',
                  persona=person['persona'],revision=person['revision'],persona_revision=person['persona_revision'])
        self.manager.update('firefly',edit)
        self.assertTrue(next(p for p in self.manager.snapshot()['characters'] if p['id']=='firefly')['ready'])
        self.manager.register_templates()
        self.assertTrue(next(p for p in self.manager.snapshot()['characters'] if p['id']=='firefly')['ready'])

    def test_canonical_kafka_card_wins_over_old_style_template(self):
        folder=self.root/'personas';folder.mkdir()
        (folder/'kafka-style.md').write_text('# old-style',encoding='utf-8')
        (folder/'kafka.md').write_text('# 卡夫卡',encoding='utf-8')
        self.assertEqual(self.manager.templates()['kafka'],'# 卡夫卡')

    def test_retired_primary_releases_its_old_connection(self):
        self.manager.create(self.values())
        (self.manager.folder/'primary-retired.json').write_text('{}')
        self.manager.create(self.values('b',102,3001))
        self.assertTrue(self.manager.snapshot()['primary_retired'])

    def test_failed_creation_does_not_leave_half_registered_character(self):
        with patch.object(SettingsStore,'validate',side_effect=SettingsError('bad','bad')):
            with self.assertRaises(SettingsError):self.manager.create(self.values())
        self.assertFalse(self.manager.deployment('a').exists())
        self.assertEqual(self.manager.snapshot()['characters'],[])
        self.manager.create(self.values())

    def test_two_people_run_simultaneously_and_stop_independently(self):
        self.manager.create(self.values());self.manager.create(self.values('b',102,3102))
        self.manager.action('a','start');self.manager.action('b','start');self.manager.tick()
        a=self.manager.runners['a'];b=self.manager.runners['b']
        self.assertTrue(a.started and b.started)
        self.manager.action('a','stop')
        self.assertTrue(a.stopped);self.assertFalse(b.stopped)
        self.assertEqual(self.manager.states['b'],'observed')

    def test_restart_manager_recovers_wanted_people(self):
        self.manager.create(self.values());self.manager.action('a','start');self.manager.close()
        second=CharacterManager(self.root,runner_factory=lambda _:FakeRunner(),monitor=False)
        try:
            second.tick();self.assertTrue(second.runners['a'].started)
        finally:second.close()

    def test_failed_stop_retains_process_ownership_and_can_retry(self):
        self.manager.create(self.values());self.manager.action('a','start')
        runner=self.manager.runners['a']
        with patch.object(runner,'stop',side_effect=TimeoutError):
            with self.assertRaises(SettingsError):self.manager.action('a','stop')
        self.assertIs(self.manager.runners['a'],runner)
        self.assertEqual(self.manager.states['a'],'stop_failed')
        self.manager.action('a','stop');self.assertNotIn('a',self.manager.runners)

    def test_binding_edit_requires_stopped_and_versions_and_preserves_token(self):
        self.manager.create(self.values());person=self.manager.snapshot()['characters'][0]
        edit=self.values();edit.pop('id');edit.update(revision=person['revision'],persona_revision=person['persona_revision'],ws_token=MASK,name='changed')
        self.manager.action('a','start')
        with self.assertRaises(SettingsError):self.manager.update('a',edit)
        self.manager.action('a','stop');self.manager.update('a',edit)
        self.assertEqual(self.manager.store('a').raw()['ws_token'],'onebot-secret')
        with self.assertRaises(SettingsError):self.manager.update('a',edit)

    def test_scoped_settings_cannot_rebind_storage_or_qq(self):
        self.manager.create(self.values());store=self.manager.store('a')
        for key,value in (('life_db','../b/life.db'),('dsh_home','../b/home'),('bot_qq',102),('ws_url','ws://other:3000')):
            with self.assertRaises(SettingsError):
                self.manager.save_settings('a',dict(document='config',base_revision=fingerprint(store.config_path),changes={key:value}))

    def test_memory_relation_and_session_stores_are_independent(self):
        self.manager.create(self.values());self.manager.create(self.values('b',102,3102))
        a=LifeRuntime(self.manager.deployment('a')/'data/life.db','a')
        b=LifeRuntime(self.manager.deployment('b')/'data/life.db','b')
        try:
            scope=a.scope('qq:private:1','1')
            a.ingest(scope,'message','user_report','only-a-knows-71953',actor_id='1')
            self.assertEqual(a.repo._one('SELECT COUNT(*) AS n FROM events')[0],1)
            self.assertEqual(b.repo._one('SELECT COUNT(*) AS n FROM events')[0],0)
            self.assertNotEqual(a.repo.path,b.repo.path)
        finally:a.close();b.close()

    def test_account_mismatch_never_ingests_platform_message(self):
        host=RoleHost.__new__(RoleHost)
        from types import SimpleNamespace
        host.cfg=SimpleNamespace(bot_qq=101);host._account_verified=True
        with patch.object(host,'allowed',side_effect=AssertionError('wrong account reached allowed')):
            asyncio.run(host.receive(dict(post_type='message',self_id=102)))

    def test_paths_cannot_escape_character_root(self):
        for identity in ('../outside','..','a/b','A','a'*41,None):
            with self.assertRaises(SettingsError):self.manager.deployment(identity)


if __name__=='__main__':unittest.main()
