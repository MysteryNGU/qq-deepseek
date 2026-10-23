"""Ordered OneBot inputs and scoped, durable media/perception assets.

Resolving or transcribing an input never appraises a person or sends a reply.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import html
from pathlib import Path
import urllib.request

from .agent_session import SessionFailure, canonical
from .text import stable_id


SCHEMA = '''
CREATE TABLE IF NOT EXISTS media_assets(
 id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,event_id TEXT NOT NULL,part_id TEXT NOT NULL,
 kind TEXT NOT NULL,status TEXT NOT NULL,mime TEXT,digest TEXT,storage_ref TEXT,
 source_ref TEXT NOT NULL,metadata TEXT NOT NULL,forget_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS media_perceptions(
 id TEXT PRIMARY KEY,asset_id TEXT NOT NULL,kind TEXT NOT NULL,content TEXT NOT NULL,
 engine_version TEXT NOT NULL,coverage TEXT NOT NULL,limitations TEXT NOT NULL,
 confidence REAL,created REAL NOT NULL);
'''


def onebot_segments(value):
    if isinstance(value,list):
        return value
    body = str(value or '')
    parts,position = [],0
    for match in re.finditer(r'\[CQ:([A-Za-z0-9_-]+)((?:,[^\]]*)?)\]',body):
        if match.start()>position:
            parts.append(dict(type='text',data=dict(text=html.unescape(body[position:match.start()]))))
        data = {}
        for item in match[2].lstrip(',').split(','):
            key,separator,text = item.partition('=')
            if separator:
                data[key] = html.unescape(text)
        parts.append(dict(type=match[1],data=data))
        position = match.end()
    if position<len(body):
        parts.append(dict(type='text',data=dict(text=html.unescape(body[position:]))))
    return parts


def visible_parts(parts):
    # Platform download tickets and local source paths belong to the resolver,
    # not every model request. Stable asset ids retain the complete lineage.
    return [{k:v for k,v in part.items() if k!='source_ref' or part['kind'] not in {'image','audio','video','file'}}
            for part in parts]


def accept_onebot(life,raw):
    """Keep every part and the actual platform actor, including media-only input."""
    actor = str(int(raw['user_id']))
    group = raw.get('message_type') == 'group'
    target = str(int(raw['group_id'])) if group else actor
    conversation = life.conversation_key('group' if group else 'private',target)
    scope = life.scope(conversation,'' if group else actor)
    original = onebot_segments(raw.get('message',[]))
    parts = []
    for ordinal,segment in enumerate(original):
        kind,data = segment.get('type','unsupported'),dict(segment.get('data') or {})
        kind = {'record':'audio','at':'mention'}.get(kind,kind)
        parts.append(dict(part_id=str(ordinal),ordinal=ordinal,kind=kind,
            text=str(data.get('text','')) if kind=='text' else None,
            source_ref=data))
    text = ''.join(p['text'] or '' for p in parts)
    event,created = life.ingest(scope,'message','user_report',text,actor_id=actor,
        external_id=str(raw['message_id']),occurred_at=float(raw.get('time') or life.clock()),
        payload=dict(role='user',parts=parts,actor=dict(platform='qq',user_id=actor,
        display_name=str((raw.get('sender') or {}).get('nickname',''))),
        account_id=str(raw.get('self_id','')),platform_message_id=str(raw['message_id'])))
    return event,created


class MediaService:
    def __init__(self,life,root,platform=None,local_roots=(),max_bytes=64*1024*1024,asr=None):
        self.life,self.repo,self.root = life,life.repo,Path(root).resolve()
        self.platform,self.asr = platform,asr
        self.local_roots = [self.root]+[Path(p).resolve() for p in local_roots]
        self.max_bytes = max_bytes
        self.repo.db.executescript(SCHEMA)
        self.root.mkdir(parents=True,exist_ok=True)
        self._locks = {}

    def capture(self,event):
        with self.repo.tx():
            current = self.repo.get_event(event.id)
            if not current or current.redacted_at or current.scope.scope_id!=event.scope.scope_id:
                raise SessionFailure('stale_context','媒体事件已失效')
            for part in event.payload.get('parts',[]):
                if part['kind'] not in {'image','audio','video','file'}:
                    continue
                if part.get('asset_id'):
                    self.require(event.scope,part['asset_id'],False)
                    continue
                aid = stable_id('asset',event.id,part['part_id'])
                part['asset_id'] = aid
                self.repo._exec('INSERT OR IGNORE INTO media_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (aid,event.scope.scope_id,event.id,part['part_id'],part['kind'],'pending',None,None,None,
                     canonical(part['source_ref']),'{}',self.repo.forget_epoch(event.scope.scope_id)))
            self.repo._exec('UPDATE events SET payload=? WHERE id=?',(canonical(event.payload),event.id))

    def require(self,scope,aid,ready=True):
        row = self.repo._one('SELECT * FROM media_assets WHERE id=?',(aid,))
        if not row or row['scope_id'] != scope.scope_id:
            raise SessionFailure('scope_mismatch','媒体不属于当前受众')
        event = self.repo.get_event(row['event_id'])
        if not event or event.redacted_at or row['forget_epoch'] != self.repo.forget_epoch(scope.scope_id):
            raise SessionFailure('stale_context','媒体来源已遗忘或失效')
        if ready and row['status'] != 'ready':
            raise SessionFailure('unavailable','媒体尚不可读取',asset_id=aid,status=row['status'],metadata=json.loads(row['metadata']))
        return dict(row)

    def register(self,scope,event_id,part_id,kind,data,mime,metadata=None):
        epoch = self.repo.forget_epoch(scope.scope_id)
        event = self.repo.get_event(event_id)
        if not event or event.redacted_at or event.scope.scope_id != scope.scope_id:
            raise SessionFailure('stale_context','生成媒体的来源不可用')
        aid = stable_id('asset',event_id,part_id)
        checksum = hashlib.sha256(data).hexdigest()
        extension = mimetypes.guess_extension(mime) or '.bin'
        path = self.root/(aid+extension)
        if not path.exists():
            temporary = path.with_suffix('.writing')
            with temporary.open('wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,path)
        elif hashlib.sha256(path.read_bytes()).hexdigest()!=checksum:
            raise SessionFailure('idempotency_conflict','同一个资产编号对应不同内容')
        with self.repo.tx():
            event = self.repo.get_event(event_id)
            if self.repo.forget_epoch(scope.scope_id)!=epoch or not event or event.redacted_at:
                raise SessionFailure('stale_context','媒体写入期间来源已遗忘')
            self.repo._exec('INSERT INTO media_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET status=excluded.status,mime=excluded.mime,digest=excluded.digest,'
                'storage_ref=excluded.storage_ref,metadata=excluded.metadata',
                (aid,scope.scope_id,event_id,part_id,kind,'ready',mime,checksum,str(path),'{}',canonical(metadata or {}),
                 epoch))
        return self.require(scope,aid)

    def read_bytes(self,scope,aid):
        asset = self.require(scope,aid)
        path = Path(asset['storage_ref']).resolve()
        if self.root not in path.parents or not path.is_file():
            raise SessionFailure('unavailable','媒体文件已不可用')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=asset['digest']:
            raise SessionFailure('stale_context','媒体文件内容发生变化')
        return data

    def _read(self,value):
        value = str(value)
        if value.startswith('base64://'):
            data = base64.b64decode(value[9:],validate=True)
        elif value.startswith(('https://','http://')):
            with urllib.request.urlopen(value,timeout=30) as response:
                data = response.read(self.max_bytes+1)
        else:
            path = Path(value.removeprefix('file:///')).resolve()
            if not any(root==path or root in path.parents for root in self.local_roots):
                raise SessionFailure('scope_mismatch','平台文件不在已配置的媒体目录')
            with path.open('rb') as stream:
                data = stream.read(self.max_bytes+1)
        if len(data)>self.max_bytes:
            raise SessionFailure('resource_wait','媒体超过单资产容量，需要分段处理')
        if not data:
            raise SessionFailure('unavailable','平台返回了空媒体')
        return data

    async def resolve(self,scope,aid):
        async with self._locks.setdefault(aid,asyncio.Lock()):
            asset = self.require(scope,aid,False)
            if asset['status']=='ready':
                return asset
            try:
                source = json.loads(asset['source_ref'])
                value = source.get('url') or source.get('file')
                if self.platform and not str(value or '').startswith(('http://','https://','base64://','file:///')):
                    action = {'image':'get_image','audio':'get_record'}.get(asset['kind'],'get_file')
                    params = {('file_id' if action=='get_file' else 'file'):source.get('file_id') or source.get('file')}
                    if asset['kind']=='audio':
                        params['out_format'] = 'wav'
                    response = await self.platform(action,params)
                    if response.get('retcode')!=0:
                        raise SessionFailure('unavailable','平台未能解析媒体标识',response=response)
                    resolved = response.get('data') or {}
                    value = resolved.get('url') or resolved.get('file') or resolved.get('file_path')
                data = await asyncio.to_thread(self._read,value)
                mime = mimetypes.guess_type(str(value))[0] or 'application/octet-stream'
                if asset['kind']=='image':
                    from PIL import Image
                    with Image.open(io.BytesIO(data)) as picture:
                        mime = Image.MIME.get(picture.format,'application/octet-stream')
                        picture.verify()
                    if mime not in {'image/png','image/jpeg','image/webp','image/gif'}:
                        raise SessionFailure('unavailable','当前视觉接口不支持此图像格式',mime=mime)
                self.require(scope,aid,False)  # deletion can happen while downloading
                return self.register(scope,asset['event_id'],asset['part_id'],asset['kind'],data,mime)
            except Exception as error:
                with self.repo.tx():
                    self.repo._exec("UPDATE media_assets SET status='failed',metadata=? WHERE id=? AND forget_epoch=?",
                        (canonical(dict(error=str(error))),aid,self.repo.forget_epoch(scope.scope_id)))
                raise

    async def perceive(self,scope,aid,kind='transcript'):
        asset = await self.resolve(scope,aid)
        key = stable_id('perception',aid,kind)
        old = self.repo._one('SELECT * FROM media_perceptions WHERE id=?',(key,))
        if old:
            return dict(old)
        if kind!='transcript' or asset['kind'] not in {'audio','video'} or self.asr is None:
            raise SessionFailure('unavailable','没有配置可执行的这类感知器')
        result = await self.asr(asset['storage_ref'])
        self.require(scope,aid)
        with self.repo.tx():
            self.repo._exec('INSERT OR IGNORE INTO media_perceptions VALUES(?,?,?,?,?,?,?,?,?)',
                (key,aid,kind,canonical(result['content']),str(result['engine_version']),
                 canonical(result.get('coverage',{})),canonical(result.get('limitations',[])),
                 result.get('confidence'),self.life.clock()))
        return dict(self.repo._one('SELECT * FROM media_perceptions WHERE id=?',(key,)))

    async def frames(self,scope,aid,timestamps):
        asset = await self.resolve(scope,aid)
        if asset['kind']!='video':
            raise SessionFailure('invalid_input','抽帧来源必须是视频')
        def sample():
            import av
            frames = []
            with av.open(asset['storage_ref']) as container:
                stream = container.streams.video[0]
                duration = float(stream.duration*stream.time_base) if stream.duration else None
                for value in timestamps:
                    seconds = float(value)
                    if seconds<0:
                        raise SessionFailure('invalid_input','帧时间不能为负数')
                    container.seek(int(seconds/float(stream.time_base)),stream=stream,backward=True)
                    for frame in container.decode(stream):
                        actual = float(frame.time or 0)
                        if actual>=seconds:
                            buffer = io.BytesIO()
                            frame.to_image().save(buffer,format='JPEG')
                            frames.append((seconds,actual,buffer.getvalue()))
                            break
            return duration,frames
        duration,frames = await asyncio.to_thread(sample)
        self.require(scope,aid)
        items = []
        for requested,actual,data in frames:
            frame = self.register(scope,asset['event_id'],asset['part_id']+':frame:'+str(actual),
                'image',data,'image/jpeg',dict(parent_asset_id=aid,requested_seconds=requested,actual_seconds=actual))
            items.append(dict(asset_id=frame['id'],seconds=actual,storage_ref=frame['storage_ref']))
        return dict(frames=items,coverage=dict(duration_seconds=duration,sampled_seconds=[f['seconds'] for f in items]),
                    limitations=['仅包含指定时间附近的抽样帧，未理解完整视频；可另行读取音轨或字幕。'])

    async def input_blocks(self,event):
        self.capture(event)
        blocks = [dict(type='text',text=canonical(dict(event_id=event.id,actor_id=event.actor_id,
                    source_kind=event.source_kind,parts=visible_parts(event.payload.get('parts',[])))))]
        for part in event.payload.get('parts',[]):
            aid = part.get('asset_id')
            if not aid:
                continue
            try:
                if part['kind']=='image':
                    asset = await self.resolve(event.scope,aid)
                    blocks.append(dict(type='text',text=canonical(dict(asset_id=aid,ordinal=part['ordinal']))))
                    blocks.append(dict(type='image',mimeType=asset['mime'],
                        data=base64.b64encode(self.read_bytes(event.scope,aid)).decode('ascii')))
                elif part['kind']=='audio' and self.asr:
                    observation = await self.perceive(event.scope,aid)
                    blocks.append(dict(type='text',text=canonical(observation)))
            except Exception as error:
                blocks.append(dict(type='text',text=canonical(dict(asset_id=aid,status='unread',error=str(error)))))
        return blocks
