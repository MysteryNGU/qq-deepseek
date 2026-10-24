"""Scoped projections of native registries and file-authoritative local methods."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
import time

from .agent_session import SessionFailure,canonical,digest
from .resource_policy import estimate_tokens

log=logging.getLogger(__name__)
METHOD_MAX_BYTES=512*1024


def validate_method_body(raw):
    if len(raw)>METHOD_MAX_BYTES:
        raise SessionFailure('invalid_input','方法正文过大；请拆成按需读取的短方法')
    try:
        return raw.decode('utf-8')
    except UnicodeError as error:
        raise SessionFailure('invalid_input','方法文件必须使用有效的 UTF-8 编码') from error


class MethodStore:
    def __init__(self,role):
        self.role,self.repo,self.root = role,role.repo,role.root.resolve()
        self.folder = self.root/'skills'
        self.errors = {}

    def path(self,name):
        if not isinstance(name,str) or not name or Path(name).name!=name or '/' in name or '\\' in name:
            raise SessionFailure('invalid_input','方法名称需要 skills 中的单个文件名')
        name = name if name.endswith('.md') else name+'.md'
        path = self.folder/name
        resolved = path.resolve()
        if path.is_symlink() or self.folder.is_symlink() or resolved.parent!=self.folder.resolve() or self.root not in resolved.parents:
            raise SessionFailure('scope_mismatch','方法文件不能通过链接越出本机 skills')
        return path

    def identity(self,name):
        name = self.path(name).name
        slug = re.sub('[^a-z0-9-]+','-',Path(name).stem.lower()).strip('-')[:32] or 'local'
        return 'qq-method-'+slug+'-'+hashlib.sha256(name.encode()).hexdigest()[:10]

    def get_meta(self,name):
        row = self.repo._one('SELECT v FROM meta WHERE k=?',('skill_meta:'+self.identity(name),))
        return json.loads(row['v']) if row else None

    def reconcile(self,name,metadata=None,expected_hash=None):
        with self.repo.tx():
            return self._reconcile(name,metadata,expected_hash)

    def _reconcile(self,name,metadata=None,expected_hash=None):
        path = self.path(name)
        previous = self.get_meta(name)
        if not path.is_file():
            if previous and previous.get('state')!='removed':
                previous.update(state='removed',version=previous['version']+1)
                self.repo._exec('UPDATE meta SET v=? WHERE k=?',(canonical(previous),'skill_meta:'+previous['id']))
            return None
        with path.open('rb') as stream:
            raw = stream.read(METHOD_MAX_BYTES+1)
        body = validate_method_body(raw)
        actual = hashlib.sha256(raw).hexdigest()
        if expected_hash and expected_hash!=actual:
            raise SessionFailure('stale_context','方法实际文件与待发布版本不同，请重新读取')
        if previous and previous.get('state')=='revoked' and metadata is None and expected_hash is None:
            return None
        metadata = self.validate_metadata(metadata) if metadata is not None else None
        if previous and previous['body_hash']==actual and metadata is None and previous.get('state')=='ready':
            return previous
        # A pending manifest is the only owner of metadata after a file/index crash.
        if metadata is None:
            for row in self.repo._all("SELECT manifest FROM self_changes WHERE phase IN ('prepared','activating','applied') ORDER BY created DESC LIMIT 100") if self.repo._one("SELECT 1 FROM sqlite_master WHERE name='self_changes'") else []:
                manifest = json.loads(row['manifest'])
                if isinstance(manifest,list):
                    match = next((m for m in manifest if m.get('path')=='skills/'+path.name and m.get('target_hash')==actual),None)
                    if match and 'method_metadata' in match:
                        metadata = match['method_metadata'];break
        unmanaged_public_change=bool(previous and previous.get('visibility')=='public' and previous['body_hash']!=actual and metadata is None)
        undescribed_change=bool(previous and previous['body_hash']!=actual and metadata is None)
        metadata = metadata if metadata is not None else {k:previous[k] for k in
            ('description','visibility','sources','capability_refs') if previous and k in previous}
        if unmanaged_public_change:
            metadata=dict(metadata,visibility='private')
        if undescribed_change:
            metadata.pop('description',None)
        description = metadata.get('description')
        if not description:
            front = re.match(r'\A---\r?\n(.*?)\r?\n---',body,re.S)
            value = re.search(r'^description:\s*(.+)$',front[1],re.M) if front else None
            description = value[1].strip().strip('\'"')[:160] if value else '未整理描述'
        current = dict(id=self.identity(name),name=path.name,description=description,body_hash=actual,scope_id=self.owner_scope().scope_id,
            audience_policy_id=self.owner_scope().audience_policy_id,
            visibility=metadata.get('visibility','private'),sources=metadata.get('sources',[]),
            capability_refs=metadata.get('capability_refs',[]),version=(previous or {}).get('version',0)+1,
            updated_at=time.time(),state='ready',forget_epoch=self.repo.forget_epoch(self.owner_scope().scope_id))
        self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('skill_meta:'+current['id'],canonical(current)))
        return current

    def owner_scope(self):
        return self.role.life.scope('qq:private:'+str(self.role.cfg.owner),str(self.role.cfg.owner))

    def validate_metadata(self,value):
        if not isinstance(value,dict) or set(value)-{'description','visibility','sources','capability_refs','public_content'}:
            raise SessionFailure('invalid_input','metadata 支持 description/visibility/sources/capability_refs/public_content')
        result = dict(value)
        if 'description' in result and (not isinstance(result['description'],str) or not 1<=len(result['description'])<=160):
            raise SessionFailure('invalid_input','方法短描述需要 1～160 字符')
        if result.get('visibility','private') not in {'private','public'}:
            raise SessionFailure('invalid_input','方法 visibility 为 private 或 public')
        for key in ('sources','capability_refs'):
            if key in result and (not isinstance(result[key],list) or len(result[key])>20 or any(not isinstance(r,str) for r in result[key])):
                raise SessionFailure('invalid_input','方法引用需要最多 20 个字符串')
        if result.get('visibility')=='public' and (result.get('public_content') is not True or result.get('sources')):
            raise SessionFailure('scope_mismatch','公共方法须显式 public_content:true 且不携带私人来源；请另建独立通用内容')
        result.pop('public_content',None)
        self.role.continuity.evidence(self.owner_scope(),result.get('sources',[]))
        return result

    def visible(self,scope,call,metadata):
        if metadata.get('state')!='ready':
            return False
        if metadata['forget_epoch']!=self.repo.forget_epoch(self.owner_scope().scope_id):
            return False
        if metadata['visibility']!='public' and (scope.scope_id!=self.owner_scope().scope_id or
                scope.audience_policy_id!=metadata.get('audience_policy_id',self.owner_scope().audience_policy_id)):
            return False
        try:
            self.role.continuity.evidence(self.owner_scope(),metadata.get('sources',[]))
        except SessionFailure:
            return False
        if call and call.get('caller')=='delegate':
            row = self.repo._one('SELECT v FROM meta WHERE k=?',('delegate_methods:'+call['session_id'],))
            granted = json.loads(row['v']) if row else []
            return 'method:bot:'+metadata['id'] in granted
        return True

    def catalog(self,scope,call=None):
        result=[]
        seen=set()
        # Filter private metadata before constructing any public title or count.
        for path in sorted(self.folder.glob('*.md')):
            seen.add(path.name)
            try:
                meta = self.get_meta(path.name)
                private=scope.scope_id==self.owner_scope().scope_id and scope.audience_policy_id==self.owner_scope().audience_policy_id
                if not private and (not meta or meta['visibility']!='public'):
                    continue
                meta = self.reconcile(path.name)
            except (SessionFailure,OSError,UnicodeError,json.JSONDecodeError) as error:
                reason=error.code if isinstance(error,SessionFailure) else type(error).__name__
                if self.errors.get(path.name)!=reason:
                    log.warning('方法暂不可用 file=%s reason=%s；继续加载其他方法',path.name,reason)
                self.errors[path.name]=reason
                continue
            self.errors.pop(path.name,None)
            if meta and self.visible(scope,call,meta):
                result.append(dict(ref='method:bot:'+meta['id'],kind='methods',name=meta['id'],file=meta['name'],
                    summary=meta['description'],version=meta['body_hash'],hash=meta['body_hash'],revision=meta['version'],
                    visibility=meta['visibility'],invocation=dict(tool='skill',arguments=dict(name=meta['id'])),
                    schema_ref='method:bot:'+meta['id'],limits=dict(on_demand=True),
                    availability=dict(registered=True,readiness='ready'),evidence_refs=meta['sources']))
        self.errors={name:reason for name,reason in self.errors.items() if name in seen}
        return result

    def read(self,scope,call=None,*,name=None,ref=None,expected_hash=None):
        items=self.catalog(scope,call)
        owner=scope.scope_id==self.owner_scope().scope_id and scope.audience_policy_id==self.owner_scope().audience_policy_id
        diagnostics=[dict(file=n,reason=r) for n,r in sorted(self.errors.items())] if owner and not (call and call.get('caller')=='delegate') else []
        if name is None and ref is None:
            return dict(items=[i['file'] for i in items],methods=items,version=digest(items),unavailable=diagnostics)
        if name and any(i['file']==name for i in diagnostics):
            raise SessionFailure('unavailable','此方法文件校验失败，请修复文件后重读',file=name,reason=self.errors[name])
        item=next((i for i in items if i['ref']==ref or name in {i['file'],i['name']}),None)
        if not item:
            raise SessionFailure('scope_mismatch','当前受众没有这个方法；请重新读取方法目录')
        raw=self.path(item['file']).read_bytes()
        actual=hashlib.sha256(raw).hexdigest()
        if actual!=item['hash'] or (expected_hash and actual!=expected_hash):
            raise SessionFailure('stale_context','方法 hash 已变化；重新读取目录后合并')
        return dict(item,text=raw.decode('utf-8'))


class CapabilityView:
    def __init__(self,role):
        self.role,self.repo=role,role.repo
        self.methods=MethodStore(role)
        self.snapshots={}
        packaged=Path(__file__).with_name('capability-policy.json')
        self.policies=json.loads(packaged.read_text('utf-8'))+list(getattr(role.cfg,'capability_policies',[]))
        for policy in self.policies:
            if not isinstance(policy,dict) or not isinstance(policy.get('tool'),str) or policy.get('effect') not in {'read','write','unknown'}:
                raise ValueError('invalid_capability_policy')

    def policy(self,name,identity=None):
        policy=dict(effect='unknown',principal_adoption=True,audience='owner',
            account=None,resource=None,package_id=None,version=None)
        declared=(identity or {}).get('policy')
        if (isinstance(declared,dict) and (identity or {}).get('package_id') and (identity or {}).get('version') and
                not set(declared)-{'effect','principal_adoption','audience','account','resource'} and
                declared.get('effect') in {'read','write','unknown'} and type(declared.get('principal_adoption')) is bool and
                declared.get('audience') in {'owner','public'}):
            policy.update(declared,package_id=identity['package_id'],version=identity['version'])
        for row in self.policies:
            if row['tool']!=name:
                continue
            if row.get('schema_hash') and row['schema_hash']!=(identity or {}).get('schema_hash'):
                continue
            if row.get('version') and row['version']!=(identity or {}).get('version'):
                continue
            if row.get('package_id') and row['package_id']!=(identity or {}).get('package_id'):
                continue
            policy.update(row)
        return policy

    def stamp(self,root,scope,call):
        current=self.role.sessions.require(root['session_id'])
        if (current['generation']!=root['generation'] or current['scope_id']!=scope.scope_id or
                json.loads(current['scope_json'])['audience_policy_id']!=scope.audience_policy_id):
            raise SessionFailure('stale_context','能力读取所属会话已变化')
        return (self.role.bridge._generation,current['generation'],self.repo.forget_epoch(scope.scope_id),
                (call or {}).get('session_id',root['session_id']))

    async def read(self,scope,p,root,call=None):
        if self.role.capability_protocol!=1:
            raise SessionFailure('protocol_mismatch','当前 worker 未协商 capability_protocol:1')
        category=p.get('category','media' if p.get('kind')=='media_capabilities' else None)
        allowed={'kind','category','query','ref','cursor','limit','provider_id','field','offset','version','handled_input_ids','activity_changes','appraisal'}
        if set(p)-allowed or (p.get('ref') is not None and (not isinstance(p['ref'],str) or not p['ref'])):
            raise SessionFailure('invalid_input','目录含未知参数或无效 ref')
        if p.get('provider_id') is not None and (not isinstance(p['provider_id'],str) or not p['provider_id']):
            raise SessionFailure('invalid_input','provider_id 需要非空字符串')
        if p.get('field') not in {None,'schema'} or (p.get('ref') and (p.get('query') or p.get('cursor'))):
            raise SessionFailure('invalid_input','ref 精确读取不与 query/cursor 混用；field 只支持 schema')
        if p.get('field') and not (p.get('ref') or p.get('provider_id')):
            raise SessionFailure('invalid_input','schema 分页需要精确 ref')
        if category=='media' and self.role.media_adapter_protocol!=1:
            raise SessionFailure('protocol_mismatch','媒体目录尚未协商 media_adapter_protocol:1')
        def over(value):
            return (estimate_tokens(value)>self.role.resources.text_result_max_tokens or
                len(canonical(value).encode())>self.role.resources.text_result_max_utf8_bytes)
        if category not in {None,'tools','methods','media'}:
            raise SessionFailure('invalid_input','category 为 tools/methods/media')
        limit=p.get('limit',8)
        if type(limit) is not int or not 1<=limit<=20 or not isinstance(p.get('query',''),str) or len(p.get('query',''))>400:
            raise SessionFailure('invalid_input','目录 limit 为 1～20，query 最多 400 字符')
        stamp=self.stamp(root,scope,call)
        sid=stamp[-1]
        try:
            native=await asyncio.to_thread(self.role.bridge.current_agent_catalog,sid,root['generation'])
        except Exception as error:
            self.snapshots.pop((scope.scope_id,sid),None)
            raise SessionFailure('unavailable','当前会话能力目录不可用；未改读其他会话',version=None) from error
        if native.get('ok') is False:
            raise SessionFailure(native.get('code','unavailable'),native.get('message','目录不可用'))
        if self.stamp(root,scope,call)!=stamp:
            raise SessionFailure('stale_context','能力读取期间会话或遗忘版本已变化')
        entries=[]
        private=scope.scope_id==self.methods.owner_scope().scope_id and scope.audience_policy_id==self.methods.owner_scope().audience_policy_id
        for tool in native.get('tools',[]):
            name=tool['name'];policy=self.policy(name,tool.get('identity'))
            permitted=private or name in getattr(self.role.cfg,'public_tools',[]) or name.startswith('life_') or name=='skill'
            if call and call.get('caller')=='delegate':
                permitted=permitted and (name=='life_read' if name.startswith('life_') else not policy['principal_adoption'])
            if not permitted:
                continue
            schema_hash=digest(tool)
            entries.append(dict(ref='tool:dsh:'+name,kind='tools',name=name,summary=tool.get('description','')[:160],
                invocation=dict(tool=name),version=schema_hash,schema_ref='tool:dsh:'+name,schema=tool,
                limits=dict(permission='rechecked_on_call',effect=policy['effect'],account=policy['account'],resource=policy['resource']),
                availability=dict(registered=True,readiness='unknown',reason='未进行实际就绪探测'),
                evidence_refs=[r['id'] for r in self.repo._all("SELECT id FROM role_operations WHERE scope_id=? AND tool=? "
                    "AND status IN ('succeeded','failed') AND result IS NOT NULL AND result!='{}' ORDER BY created DESC LIMIT 3",(scope.scope_id,name))]))
        entries+=self.methods.catalog(scope,call)
        local=self.role.local_media
        if local and self.role.media_adapter_protocol==1 and private:
            entries+=local.catalog(scope)
        # Native method summaries are already scoped by the provider registry.
        for method in native.get('methods',[]):
            if method.get('provider','').startswith('qq-life-'):
                continue
            entries.append(dict(ref='method:dsh:'+method['provider']+':'+method['name'],kind='methods',name=method['name'],
                summary=method.get('description','')[:160],invocation=dict(tool='skill',arguments=dict(name=method['name'])),
                version=method.get('hash'),schema_ref=None,limits=dict(on_demand=True),
                availability=dict(registered=True,readiness='unknown'),evidence_refs=[]))
        entries.sort(key=lambda e:e['ref'])
        version=digest(dict(stamp=stamp,entries=entries,native_version=native.get('version')))
        self.snapshots[(scope.scope_id,sid)]=version
        ref=p.get('ref') or ('media:'+p['provider_id'] if p.get('provider_id') else None)
        if ref:
            item=next((i for i in entries if i['ref']==ref and (not category or i['kind']==category)),None)
            if not item:
                raise SessionFailure('unavailable','当前目录没有此引用；查询范围仅限已安装能力',version=version)
            result=dict(version=version,item=item)
            if over(result):
                result=dict(version=version,item={k:v for k,v in item.items() if k!='schema'},truncated=True,
                    detail_read=dict(kind='capabilities',ref=ref,field='schema',offset=0,version=version))
                if p.get('field')=='schema':
                    serialized=canonical(item.get('schema',{}));offset=p.get('offset',0)
                    if type(offset) is not int or offset<0:
                        raise SessionFailure('invalid_input','schema offset 需要非负整数')
                    if offset and p.get('version')!=version:
                        raise SessionFailure('stale_context','schema 目录版本已变化；从第一页重新读取',version=version)
                    result=dict(version=version,ref=ref,schema_hash=digest(item.get('schema',{})),text=serialized[offset:offset+1200],
                        next_offset=offset+1200 if offset+1200<len(serialized) else None,coverage='partial')
                    while over(result) and len(result['text'])>1:
                        result['text']=result['text'][:len(result['text'])//2]
                        result['next_offset']=offset+len(result['text']) if offset+len(result['text'])<len(serialized) else None
            if over(result):
                raise SessionFailure('resource_wait','目录详情预算不足；请按 ref 分页读取 schema')
            return result
        query=p.get('query','').casefold()
        filtered=[i for i in entries if (not category or i['kind']==category) and
            (not query or query in (i['name']+' '+i['summary']+' '+i.get('file','')).casefold())]
        offset=0
        if p.get('cursor') is not None:
            cursor=p['cursor']
            if (not isinstance(cursor,dict) or cursor.get('version')!=version or cursor.get('query')!=query or
                    cursor.get('category')!=category or type(cursor.get('offset')) is not int or cursor['offset']<0):
                raise SessionFailure('stale_context','目录或筛选已变化；请从第一页重新读取',version=version)
            offset=cursor['offset']
        result=dict(version=version,items=[],next_cursor=None,search_scope='installed_current_agent')
        for item in filtered[offset:offset+limit]:
            short={k:v for k,v in item.items() if k!='schema'}
            if over(dict(result,items=result['items']+[short])):
                if not result['items']:
                    short={k:item[k] for k in ('ref','kind','name','summary','invocation','version','schema_ref')}
                    short['truncated']=True
                    if over(dict(result,items=[short])):
                        raise SessionFailure('resource_wait','目录预算不足以提供一个引用；请调整读取预算')
                    result['items'].append(short)
                break
            result['items'].append(short)
        after=offset+len(result['items'])
        if after<len(filtered):
            result['next_cursor']=dict(version=version,offset=after,query=query,category=category)
        while over(result) and len(result['items'])>1:
            result['items'].pop();after-=1
            result['next_cursor']=dict(version=version,offset=after,query=query,category=category)
        if over(result):
            raise SessionFailure('resource_wait','目录预算不足以保留引用及游标；请缩小筛选或调整预算')
        return result

    def summary(self,scope,binding,call=None):
        methods=self.methods.catalog(scope,call)
        value=dict(read_kind='capabilities',protocol=1,methods_version=digest(methods),
            methods=[dict(ref=i['ref'],name=i['name'],summary=i['summary'][:70]) for i in methods[:3]])
        while value['methods'] and estimate_tokens(value)>200:
            value['methods'].pop()
        return value
