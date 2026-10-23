"""Real voice engines and scoped defaults; no random speech policy."""
from __future__ import annotations

import asyncio
from importlib.metadata import version
import json
import re

from .agent_session import SessionFailure,canonical,digest


SCHEMA = '''
CREATE TABLE IF NOT EXISTS voice_catalog(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,engine TEXT NOT NULL,engine_version TEXT NOT NULL,
 engine_voice TEXT NOT NULL,languages TEXT NOT NULL,parameters TEXT NOT NULL,source TEXT NOT NULL,
 status TEXT NOT NULL,sample_asset_id TEXT,revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS voice_preferences(
 scope_id TEXT PRIMARY KEY,voice_id TEXT NOT NULL,revision INTEGER NOT NULL);
'''


class VoiceCatalog:
    def __init__(self,life,media,engine=None):
        self.life,self.repo,self.media = life,life.repo,media
        self.engine = engine or self.edge
        self.repo.db.executescript(SCHEMA)
        self._locks = {}

    def register(self,record):
        if record['engine']!='edge-tts':
            raise SessionFailure('unavailable','尚未接入这个语音引擎，不能仅登记名称冒充可用声音')
        # This engine supports presets and parameter variants, not voice cloning.
        if record.get('sample_person'):
            raise SessionFailure('unavailable','当前引擎不支持用人物素材克隆声音')
        with self.repo.tx():
            prior = self.repo._one('SELECT * FROM voice_catalog WHERE id=?',(record['id'],))
            if prior:
                if prior['engine_voice']!=record['engine_voice'] or json.loads(prior['parameters'])!=record.get('parameters',{}):
                    raise SessionFailure('idempotency_conflict','音色编号已用于另一种声音，请使用新编号')
                return dict(prior)
            self.repo._exec('INSERT INTO voice_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (record['id'],record.get('name',record['id']),'edge-tts',version('edge-tts'),record['engine_voice'],
                 canonical(record.get('languages',[])),canonical(record.get('parameters',{})),
                 canonical(record.get('source',{'kind':'engine_preset'})),'registered',None,1))
        return self.require(record['id'],False)

    def require(self,voice_id,verified=True):
        row = self.repo._one('SELECT * FROM voice_catalog WHERE id=?',(voice_id,))
        if not row or (verified and row['status']!='verified'):
            raise SessionFailure('unavailable','音色尚未通过真实合成验证',voice_id=voice_id)
        return dict(row)

    def list(self):
        return [dict(r) for r in self.repo._all('SELECT * FROM voice_catalog ORDER BY name')]

    async def discover(self,language='zh'):
        import edge_tts
        voices = await edge_tts.list_voices()
        return [v for v in voices if v.get('Locale','').startswith(language)]

    def default(self,scope):
        row = self.repo._one('SELECT voice_id FROM voice_preferences WHERE scope_id IN (?,?) '
            'ORDER BY CASE WHEN scope_id=? THEN 0 ELSE 1 END LIMIT 1',(scope.scope_id,'global',scope.scope_id))
        return row['voice_id'] if row else None

    def set_default(self,scope,voice_id,global_default=False,owner=False):
        self.require(voice_id)
        if global_default and not owner:
            raise SessionFailure('scope_mismatch','当前会话不能改变全局默认音色')
        key = 'global' if global_default else scope.scope_id
        with self.repo.tx():
            self.repo._exec('INSERT INTO voice_preferences VALUES(?,?,1) ON CONFLICT(scope_id) DO UPDATE '
                'SET voice_id=excluded.voice_id,revision=voice_preferences.revision+1',(key,voice_id))
        return dict(voice_id=voice_id,scope_id=key,saved=True)

    async def edge(self,text,voice,parameters,path):
        import edge_tts
        checked = {}
        patterns = {'rate':r'[+-]\d+%','volume':r'[+-]\d+%','pitch':r'[+-]\d+Hz'}
        for key,value in parameters.items():
            if key not in patterns or not re.fullmatch(patterns[key],str(value)):
                raise SessionFailure('invalid_input','引擎不支持这个表达参数',parameter=key)
            checked[key] = str(value)
        await edge_tts.Communicate(text,voice,**checked).save(str(path))

    async def synthesize(self,scope,event_id,part_id,text,voice_id=None,parameters=None):
        voice_id = voice_id or self.default(scope)
        voice = self.require(voice_id,False)
        params = dict(json.loads(voice['parameters']),**(parameters or {}))
        request = digest(dict(scope=scope.scope_id,text=text,voice=voice_id,version=voice['revision'],params=params))
        key = 'voice-'+request
        async with self._locks.setdefault(key,asyncio.Lock()):
            path = self.media.root/(key+'.mp3')
            if not path.is_file():
                temporary = path.with_suffix('.synthesizing')
                try:
                    await self.engine(text,voice['engine_voice'],params,temporary)
                    if not temporary.is_file() or temporary.stat().st_size<32:
                        raise SessionFailure('unavailable','合成器没有生成有效音频')
                    temporary.replace(path)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    with self.repo.tx():
                        self.repo._exec("UPDATE voice_catalog SET status='unavailable' WHERE id=?",(voice_id,))
                    raise
            asset = self.media.register(scope,event_id,part_id,'audio',path.read_bytes(),'audio/mpeg',
                dict(voice_id=voice_id,voice_revision=voice['revision'],request_hash=request,engine_version=voice['engine_version']))
            with self.repo.tx():
                self.repo._exec("UPDATE voice_catalog SET status='verified',sample_asset_id=? WHERE id=?",(asset['id'],voice_id))
            return asset
