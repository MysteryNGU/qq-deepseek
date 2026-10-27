"""Scoped reusable image/emoji selections; retiring a selection keeps its source."""
from dataclasses import dataclass,fields
import json

from .agent_session import SessionFailure,canonical
from .text import stable_id


@dataclass(frozen=True)
class MediaLibraryConfig:
    max_active_per_scope: int = 50
    lifetime_days: int = 30

    @classmethod
    def parse(cls,raw=None):
        raw={} if raw is None else raw
        if not isinstance(raw,dict) or set(raw)-{f.name for f in fields(cls)}:raise ValueError('invalid_media_library_config')
        value=cls(**raw)
        if any(type(v)is not int or not 1<=v<=365 for v in value.__dict__.values()):raise ValueError('invalid_media_library_limits')
        return value


class MediaLibrary:
    def __init__(self,role):
        self.role,self.repo=role,role.repo
        self.config=MediaLibraryConfig.parse(getattr(role.cfg,'media_library',{}))
        self.repo.db.execute('''CREATE TABLE IF NOT EXISTS media_library(
            id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,asset_id TEXT NOT NULL,digest TEXT NOT NULL,
            label TEXT NOT NULL,tags TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,
            expires REAL NOT NULL,last_used REAL,reason TEXT NOT NULL)''')

    def change(self,scope,p):
        action=p.get('action','collect');identity=p.get('ref')
        if action=='retire':
            row=self.repo._one('SELECT * FROM media_library WHERE id=? AND scope_id=?',(identity,scope.scope_id))
            if not row:raise SessionFailure('scope_mismatch','此场景没有该收藏')
            self.repo._exec("UPDATE media_library SET state='retired',reason='explicit_retire' WHERE id=?",(identity,))
            return dict(ref=identity,state='retired',original_preserved=True)
        if action!='collect' or not self.role.media:raise SessionFailure('unavailable','只能收藏已取得的图片素材')
        asset=self.role.media.require(scope,str(p.get('asset_id','')))
        if asset['kind']!='image':raise SessionFailure('invalid_input','此收藏只接受图片和表情')
        self.role.media.read_bytes(scope,asset['id'])
        label=p.get('label','');tags=p.get('tags',[])
        if not isinstance(label,str) or not 1<=len(label)<=80 or not isinstance(tags,list) or len(tags)>12 or any(not isinstance(t,str) or not 1<=len(t)<=24 for t in tags):
            raise SessionFailure('invalid_input','收藏需要简短名称和最多 12 个标签')
        identity=stable_id('media-selection',scope.scope_id,asset['digest'])
        count=self.repo._one("SELECT COUNT(*) AS n FROM media_library WHERE scope_id=? AND state='active' AND id!=?",(scope.scope_id,identity))['n']
        if count>=self.config.max_active_per_scope:raise SessionFailure('resource_limit','收藏已满，先明确退役不再使用的素材')
        now=self.role.life.clock()
        self.repo._exec("INSERT INTO media_library VALUES(?,?,?,?,?,?,'active',?,?,NULL,'') ON CONFLICT(id) DO UPDATE SET asset_id=excluded.asset_id,label=excluded.label,tags=excluded.tags,state='active',expires=excluded.expires,reason=''",
            (identity,scope.scope_id,asset['id'],asset['digest'],label,canonical(tags),now,now+self.config.lifetime_days*86400))
        return dict(ref=identity,state='active',asset_id=asset['id'],expires=now+self.config.lifetime_days*86400)

    def list(self,scope,query=''):
        self.maintain()
        rows=self.repo._all("SELECT * FROM media_library WHERE scope_id=? AND state='active' ORDER BY COALESCE(last_used,created) DESC",(scope.scope_id,))
        result=[]
        for row in rows:
            if query and query.lower() not in (row['label']+' '+row['tags']).lower():continue
            result.append(dict(ref=row['id'],asset_id=row['asset_id'],label=row['label'],tags=json.loads(row['tags']),expires=row['expires'],last_used=row['last_used']))
        return dict(items=result[:50],total=len(result),meaning='available_in_current_scene_only')

    def used(self,scope,parts):
        for part in parts:
            if isinstance(part,dict) and part.get('asset_id'):
                self.repo._exec("UPDATE media_library SET last_used=? WHERE scope_id=? AND asset_id=? AND state='active'",(self.role.life.clock(),scope.scope_id,part['asset_id']))

    def maintain(self):
        if not self.role.media:return
        from .contracts import Scope
        with self.repo.tx():
            for row in self.repo._all("SELECT * FROM media_library WHERE state='active'"):
                agent,conversation=row['scope_id'].split('|',1);scope=self.role.life.scope(conversation,conversation.removeprefix('qq:private:') if conversation.startswith('qq:private:') else '')
                reason='expired' if row['expires']<=self.role.life.clock() else None
                if not reason:
                    try:self.role.media.require(scope,row['asset_id'])
                    except SessionFailure:reason='source_unavailable'
                if reason:self.repo._exec("UPDATE media_library SET state='retired',reason=? WHERE id=?",(reason,row['id']))
