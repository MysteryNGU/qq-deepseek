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
CREATE TABLE IF NOT EXISTS local_voice_revisions(
 voice_id TEXT NOT NULL,revision INTEGER NOT NULL,scope_id TEXT NOT NULL,
 record TEXT NOT NULL,PRIMARY KEY(voice_id,revision));
CREATE TABLE IF NOT EXISTS local_voice_samples(
 voice_id TEXT NOT NULL,revision INTEGER NOT NULL,asset_id TEXT NOT NULL,
 PRIMARY KEY(voice_id,revision));
CREATE TABLE IF NOT EXISTS voice_preference_revisions(
 scope_id TEXT PRIMARY KEY,voice_id TEXT NOT NULL,voice_revision INTEGER NOT NULL);
'''


def engine_profile(engine,registry=None):
    if engine == 'edge-tts':
        return '.mp3', 'audio/mpeg'
    if registry is not None:
        output=registry.for_voice(dict(engine=engine))['description']['output']
        return output['extension'],output['mime']
    raise SessionFailure('unavailable','尚未接入这个语音引擎')


def local_delivery(parameters):
    # Compatibility for existing imports; the engine owns these parameter meanings.
    from .media_adapters.indextts import normalize_delivery
    return normalize_delivery(parameters)


class VoiceCatalog:
    def __init__(self,life,media,engine=None):
        self.life,self.repo,self.media = life,life.repo,media
        self.engine = engine or self.edge
        self.repo.db.executescript(SCHEMA)
        self._locks = {}
        self.local_enabled = False
        self.adapters = None

    @staticmethod
    def is_background(voice):
        return voice['engine']!='edge-tts'

    def register(self,record,scope=None):
        if record.get('engine')!='edge-tts':
            return self.register_local(record,scope)
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

    def register_local(self,record,scope):
        if not self.local_enabled or scope is None:
            raise SessionFailure('unavailable','本地语音后台尚未配置')
        aid = str(record.get('reference_asset_id',''))
        reference = self.media.require(scope,aid)
        if reference['kind']!='audio':
            raise SessionFailure('invalid_input','音色参考必须是当前范围内的音频资产')
        self.media.read_bytes(scope,aid)
        if self.adapters is None:
            raise SessionFailure('unavailable','本地音色 adapter 尚未配置')
        record=self.adapters.normalize_voice(record)
        params=record['parameters']
        revision = record.get('revision',1)
        if type(revision) is not int or revision<1 or not str(record.get('id','')).strip():
            raise SessionFailure('invalid_input','音色需要 id 和正整数 revision')
        frozen = dict(id=record['id'],name=str(record.get('name',record['id'])),engine=record['engine'],
            revision=revision,scope_id=scope.scope_id,reference_asset_id=aid,reference_digest=reference['digest'],
            parameters=params,source=record.get('source',{}),engine_version=str(record.get('engine_version','')))
        frozen['provider_id']=record['provider_id']
        frozen['provider_revision']=record['provider_revision']
        frozen['provider_digest']=record['provider_digest']
        with self.repo.tx():
            old = self.repo._one('SELECT * FROM local_voice_revisions WHERE voice_id=? AND revision=?',(record['id'],revision))
            current = self.repo._one('SELECT * FROM voice_catalog WHERE id=?',(record['id'],))
            if old:
                old_frozen=json.loads(old['record'])
                if 'provider_id' not in old_frozen:
                    old_frozen['provider_id']=record['provider_id']
                if 'provider_digest' not in old_frozen:
                    old_frozen.update(provider_digest=record['provider_digest'],provider_revision=record['provider_revision'])
                if old_frozen!=frozen:
                    raise SessionFailure('idempotency_conflict','此音色 revision 已绑定另一份参考或参数')
                return self.require(record['id'],False,scope)
            if current:
                previous = self.repo._one('SELECT scope_id FROM local_voice_revisions WHERE voice_id=? AND revision=?',
                    (record['id'],current['revision']))
                if not previous or previous['scope_id']!=scope.scope_id or revision<=current['revision']:
                    raise SessionFailure('idempotency_conflict','音色编号已使用；更新需要同范围的新 revision')
            self.repo._exec('INSERT INTO local_voice_revisions VALUES(?,?,?,?)',
                (record['id'],revision,scope.scope_id,canonical(frozen)))
            self.repo._exec('INSERT INTO voice_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                'name=excluded.name,engine=excluded.engine,engine_version=excluded.engine_version,engine_voice=excluded.engine_voice,'
                'parameters=excluded.parameters,source=excluded.source,status=excluded.status,sample_asset_id=NULL,revision=excluded.revision',
                (record['id'],frozen['name'],frozen['engine'],frozen['engine_version'],aid,
                 canonical(record.get('languages',['zh'])),canonical(params),canonical(frozen['source']),'registered',None,revision))
        return self.require(record['id'],False,scope)

    def require(self,voice_id,verified=True,scope=None,revision=None):
        row = self.repo._one('SELECT * FROM voice_catalog WHERE id=?',(voice_id,))
        if not row or (verified and row['engine']=='edge-tts' and row['status']!='verified'):
            raise SessionFailure('unavailable','音色尚未通过真实合成验证',voice_id=voice_id)
        result = dict(row)
        if self.is_background(result):
            frozen = self.local_record(voice_id,row['revision'] if revision is None else revision,scope)
            sample = self.repo._one('SELECT asset_id FROM local_voice_samples WHERE voice_id=? AND revision=?',
                (voice_id,frozen['revision']))
            result.update(frozen,parameters=canonical(frozen['parameters']),status='verified' if sample else 'registered',
                sample_asset_id=sample['asset_id'] if sample else None)
            if verified:
                if not sample:
                    raise SessionFailure('unavailable','此音色版本尚未完成真实试音')
                self.media.require(scope,sample['asset_id'])
        return result

    def local_record(self,voice_id,revision,scope):
        row = self.repo._one('SELECT * FROM local_voice_revisions WHERE voice_id=? AND revision=?',(voice_id,revision))
        if not row or scope is None or row['scope_id']!=scope.scope_id:
            raise SessionFailure('scope_mismatch','私人音色不属于当前受众')
        record = json.loads(row['record'])
        reference = self.media.require(scope,record['reference_asset_id'])
        if reference['digest']!=record['reference_digest']:
            raise SessionFailure('stale_context','音色参考内容已改变')
        self.media.read_bytes(scope,reference['id'])
        return record

    def verified_local(self,scope,voice_id,revision,asset_id):
        self.local_record(voice_id,revision,scope)
        asset = self.media.require(scope,asset_id)
        if asset['kind']!='audio':
            raise SessionFailure('invalid_input','试音结果必须是音频')
        with self.repo.tx():
            self.repo._exec('INSERT INTO local_voice_samples VALUES(?,?,?) ON CONFLICT(voice_id,revision) '
                'DO UPDATE SET asset_id=excluded.asset_id',(voice_id,revision,asset_id))
            self.repo._exec("UPDATE voice_catalog SET status='verified',sample_asset_id=? WHERE id=? AND revision=?",
                (asset_id,voice_id,revision))

    def list(self,scope=None):
        result = []
        for row in self.repo._all('SELECT * FROM voice_catalog ORDER BY name'):
            try:
                result.append(self.require(row['id'],False,scope))
            except SessionFailure:
                continue
        return result

    async def discover(self,language='zh'):
        import edge_tts
        voices = await edge_tts.list_voices()
        return [v for v in voices if v.get('Locale','').startswith(language)]

    def default(self,scope):
        record = self.default_record(scope)
        return record['id'] if record else None

    def default_record(self,scope):
        row = self.repo._one('SELECT p.voice_id,r.voice_revision FROM voice_preferences p '
            'LEFT JOIN voice_preference_revisions r ON r.scope_id=p.scope_id AND r.voice_id=p.voice_id '
            'WHERE p.scope_id IN (?,?) ORDER BY CASE WHEN p.scope_id=? THEN 0 ELSE 1 END LIMIT 1',
            (scope.scope_id,'global',scope.scope_id))
        if row:
            try:
                return self.require(row['voice_id'],False,scope,revision=row['voice_revision'])
            except SessionFailure:
                pass
        return None

    def set_default(self,scope,voice_id,global_default=False,owner=False):
        voice = self.require(voice_id,scope=scope)
        if global_default and self.is_background(voice):
            raise SessionFailure('scope_mismatch','私人参考音色只可设为当前会话默认')
        if global_default and not owner:
            raise SessionFailure('scope_mismatch','当前会话不能改变全局默认音色')
        key = 'global' if global_default else scope.scope_id
        with self.repo.tx():
            self.repo._exec('INSERT INTO voice_preferences(scope_id,voice_id,revision) VALUES(?,?,1) '
                'ON CONFLICT(scope_id) DO UPDATE SET voice_id=excluded.voice_id,revision=voice_preferences.revision+1',(key,voice_id))
            self.repo._exec('INSERT INTO voice_preference_revisions VALUES(?,?,?) ON CONFLICT(scope_id) DO UPDATE '
                'SET voice_id=excluded.voice_id,voice_revision=excluded.voice_revision',(key,voice_id,voice['revision']))
        return dict(voice_id=voice_id,voice_revision=voice['revision'],scope_id=key,saved=True)

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
        voice = self.require(voice_id,False,scope)
        if self.is_background(voice):
            raise SessionFailure('background_required','本地语音请用 life_media 提交 speech 任务，完成后发送 audio 资产')
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
