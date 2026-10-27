from dataclasses import fields
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import tomllib
import unittest

from life.role_config import Config,load_config
from life.settings import SettingsStore,SettingsError,fingerprint,encode_toml,MASK,restore,sanitize
from life.settings_web import SettingsServer


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'config.toml').write_text('owner = 1\napi_key = "private-test-key"\nws_token = "private-ws-token"\n',encoding='utf-8')
        (self.root/'life-dynamics.json').write_text('{"relations":{"daily_cap":0.3}}',encoding='utf-8')
        self.store=SettingsStore(self.root)

    def test_all_config_fields_registered_without_database_or_secret_readback(self):
        snap=self.store.snapshot()
        self.assertEqual({f['key'] for f in snap['fields']},{f.name for f in fields(Config)})
        self.assertNotIn('private-test-key',json.dumps(snap));self.assertNotIn('private-ws-token',json.dumps(snap))
        self.assertFalse(list(self.root.rglob('*.db')))
        self.assertFalse(snap['status']['online'])

    def test_valid_save_roundtrips_and_keeps_masked_secrets(self):
        revision=fingerprint(self.root/'config.toml')
        receipt=self.store.save('config',revision,dict(name='小鱼 "test"\n第二行',api_key=MASK,resource_policy=dict(idle_min_seconds=600,idle_random_max_seconds=3600)))
        self.assertTrue(receipt['saved']);self.assertFalse(receipt['effective'])
        cfg=load_config(self.root/'config.toml')
        self.assertEqual(cfg.name,'小鱼 "test"\n第二行');self.assertEqual(cfg.api_key,'private-test-key')
        self.assertNotIn('private-test-key',(self.store.folder/'changes.jsonl').read_text('utf-8'))

    def test_compare_and_swap_rejects_external_change(self):
        revision=fingerprint(self.root/'config.toml')
        with (self.root/'config.toml').open('a') as f:f.write('name = "external"\n')
        with self.assertRaises(SettingsError) as got:self.store.save('config',revision,dict(name='overwrite'))
        self.assertEqual(got.exception.status,409)
        self.assertEqual(load_config(self.root/'config.toml').name,'external')

    def test_existing_fractional_timeout_can_survive_unrelated_save(self):
        path=self.root/'config.toml'
        with path.open('a') as f:f.write('dsh_timeout = 180.5\nhandoff_seconds = 10.5\n')
        self.store.save('config',fingerprint(path),dict(profile_backfill=dict(enabled=True)))
        cfg=load_config(path)
        self.assertEqual(cfg.dsh_timeout,180.5)
        self.assertTrue(cfg.profile_backfill['enabled'])

    def test_invalid_values_do_not_partially_replace_file(self):
        path=self.root/'config.toml';before=path.read_bytes()
        for changes in [dict(resource_policy=dict(idle_min_seconds=3600,idle_random_max_seconds=600)),dict(owner=False),dict(friends=[True]),
                dict(action_per_day=-1),dict(handoff_seconds='10'),dict(unknown=True),dict(local_media=dict(enabled=True)),dict(life_db='elsewhere.db')]:
            with self.assertRaises(SettingsError):self.store.save('config',fingerprint(path),changes)
            self.assertEqual(path.read_bytes(),before)

    def test_secret_can_be_rotated_and_explicitly_cleared(self):
        for key in ['new-secret','']:
            self.store.save('config',fingerprint(self.root/'config.toml'),dict(api_key=key))
            self.assertEqual(tomllib.loads((self.root/'config.toml').read_text('utf-8'))['api_key'],key)

    def test_full_toml_arrays_nested_objects_and_unicode_roundtrip(self):
        raw=dict(name='鱼',objects=[dict(id='a',config=dict(x=[1,2.5,True,'a\\b']))])
        self.assertEqual(tomllib.loads(encode_toml(raw)),raw)

    def test_token_budgets_are_numbers_not_mistaken_for_secrets(self):
        self.assertEqual(sanitize({'input_nano_per_token':0,'access_token':'private'}),{'input_nano_per_token':0,'access_token':MASK})
        snap=self.store.snapshot()
        policy=next(f for f in snap['fields'] if f['key']=='resource_policy')['value']
        self.assertIsInstance(policy['text_preview_target_tokens'],int)

    def test_masked_provider_keys_follow_identity_when_reordered(self):
        old=[dict(provider_id='a',api_key='a-key'),dict(provider_id='b',api_key='b-key')]
        proposed=[dict(provider_id='b',api_key=MASK),dict(provider_id='a',api_key=MASK)]
        self.assertEqual([v['api_key'] for v in restore(proposed,old)],['b-key','a-key'])

    def test_voice_selection_and_parameter_revision_keep_original_assets(self):
        path=self.root/'voices.json'
        preset=dict(id='voice',name='Voice',revision=1,delivery=dict(duration_factor=1),reference=dict(path='original.wav',sha256='evidence'),sample=dict(path='sample.wav'))
        path.write_text(json.dumps(dict(schema=1,engine='indextts',presets=[preset],default_voice_id='voice',default_voice_revision=1)))
        with (self.root/'config.toml').open('a') as f:f.write('[local_media]\npreset_manifest="voices.json"\n')
        doc=self.store.snapshot()['documents']['voices'];doc['value']['presets'][0]['delivery']['duration_factor']=.9
        self.store.save('voices',doc['revision'],doc['value'])
        saved=json.loads(path.read_text());self.assertEqual(saved['presets'][0]['revision'],2)
        self.assertEqual(saved['default_voice_revision'],2);self.assertEqual(saved['presets'][0]['reference'],preset['reference'])
        self.assertEqual(saved['schema'],1)
        doc=self.store.snapshot()['documents']['voices'];doc['value']['presets'][0]['reference']=dict(path='elsewhere')
        with self.assertRaises(SettingsError):self.store.save('voices',doc['revision'],doc['value'])

    def test_dynamics_validated_without_initializing_bot(self):
        path=self.root/'life-dynamics.json'
        self.store.save('dynamics',fingerprint(path),dict(relations=dict(daily_cap=.2)))
        with self.assertRaises(SettingsError):self.store.save('dynamics',fingerprint(path),dict(relations=dict(daily_cap=-2)))
        self.assertEqual(json.loads(path.read_text('utf-8'))['relations']['daily_cap'],.2)
        self.assertFalse(list(self.root.rglob('*.db')))

    def test_external_config_path_cannot_become_arbitrary_file_reader(self):
        with (self.root/'config.toml').open('a') as f:f.write('consult_config = "../private.json"\n')
        with self.assertRaises(SettingsError):self.store.registered_path('consult')
        with self.assertRaises(SettingsError):self.store.registered_path('../config.toml')

    def test_saved_and_effective_versions_remain_distinct_and_offline_edit_works(self):
        folder=self.root/'data/life-control';folder.mkdir()
        active=dict(config=fingerprint(self.root/'config.toml'),dynamics=fingerprint(self.root/'life-dynamics.json'))
        (folder/'worker-health.json').write_text(json.dumps(dict(at=time.time(),settings_revisions=active,channel_connected=True,cognition_ready=True,token='not-for-web')))
        self.assertFalse(self.store.status()['pending_restart'])
        self.store.save('config',active['config'],dict(name='changed'))
        self.assertTrue(self.store.status()['pending_restart'])
        self.assertNotIn('not-for-web',json.dumps(self.store.status()))
        (folder/'worker-health.json').unlink()
        self.store.save('config',fingerprint(self.root/'config.toml'),dict(name='offline-saved'))
        with self.assertRaises(SettingsError):self.store.request_restart()


class SettingsHttpTests(SettingsTests):
    def setUp(self):
        super().setUp();self.server=SettingsServer(self.root,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_address[1];self.cookie='';self.csrf=''
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
    def request(self,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        base=dict(Host=f'127.0.0.1:{self.port}',Origin=f'http://127.0.0.1:{self.port}',Cookie=self.cookie)
        if body is not None:base.update({'Content-Type':'application/json','X-CSRF-Token':self.csrf})
        base.update(headers or {})
        conn.request('GET' if body is None else 'POST',path,json.dumps(body) if body is not None else None,base)
        response=conn.getresponse();data=response.read();cookie=response.getheader('Set-Cookie');status=response.status;conn.close()
        try:value=json.loads(data)
        except ValueError:value=data.decode()
        return status,value,cookie
    def login(self):
        password=(self.store.folder/'login.txt').read_text().strip()
        status,value,cookie=self.request('/api/login',dict(password=password))
        self.assertEqual(status,200);self.cookie=cookie.split(';')[0];self.csrf=value['csrf']
    def test_authentication_required_and_login_cookie_is_httponly(self):
        self.assertEqual(self.request('/api/settings')[0],401)
        password=(self.store.folder/'login.txt').read_text().strip()
        status,value,cookie=self.request('/api/login',dict(password=password))
        self.assertEqual(status,200);self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
    def test_host_origin_csrf_and_unknown_actions_are_rejected(self):
        self.assertEqual(self.request('/api/login',{},dict(Host='evil.invalid'))[0],403)
        self.assertEqual(self.request('/api/login',{},dict(Origin='http://evil.invalid'))[0],403)
        self.login()
        self.assertEqual(self.request('/api/save',{}, {'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(self.request('/api/shell',dict(command='anything'))[0],404)
    def test_authenticated_save_and_reload_preserve_secret(self):
        self.login();status,snap,_=self.request('/api/settings');self.assertEqual(status,200)
        self.assertEqual(self.request('/api/save',dict(document='config',base_revision=snap['revision'],changes=dict(name='测试设置',api_key=MASK)))[0],200)
        self.assertEqual(load_config(self.root/'config.toml').name,'测试设置')
        self.assertEqual(load_config(self.root/'config.toml').api_key,'private-test-key')
    def test_logout_revokes_session(self):
        self.login();self.assertEqual(self.request('/api/logout',{})[0],200)
        self.assertEqual(self.request('/api/settings')[0],401)


if __name__=='__main__':unittest.main()
