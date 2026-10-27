"""Installed lightweight media adapters; model imports belong inside execute()."""
from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import threading

from ..agent_session import SessionFailure,canonical,digest

PROTOCOL='media-adapter/1'
_load_lock=threading.RLock()


class _SourceLoader(importlib.machinery.SourceFileLoader):
    def get_code(self,fullname):
        # A same-size edit within one timestamp tick must not reuse a stale .pyc.
        return self.source_to_code(self.get_data(self.path),self.path)


class _PackageSources(importlib.abc.MetaPathFinder):
    def __init__(self,prefix):
        self.prefix=prefix

    def find_spec(self,fullname,path=None,target=None):
        if fullname!=self.prefix and not fullname.startswith(self.prefix+'.'):
            return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
            spec.loader=_SourceLoader(fullname,spec.origin)
        return spec


def validate_reference(reference):
    path=Path(reference['path']).resolve()
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=reference['digest']:
        raise SessionFailure('unavailable','已安装的 adapter 发生变化；取消原任务并按当前版本重新发起')
    return path


def validate_options(schema,value):
    from jsonschema import Draft202012Validator
    if not isinstance(value,dict):
        raise SessionFailure('invalid_input','provider_options 需要对象')
    try:
        json.dumps(value,allow_nan=False)
    except (TypeError,ValueError) as error:
        raise SessionFailure('invalid_input','后端参数需要有限、可序列化的 JSON 值') from error
    reserved={'scope','scope_id','budget','python','command','entry','output','output_dir','job_id','provider_id',
        'kind','prompt','text','seed','width','height','voice_id','voice_revision','reference_asset_ids','references','intent_ref'}
    if reserved & set(value):
        raise SessionFailure('invalid_input','provider_options 不能覆盖公共参数、归属或执行配置',fields=sorted(reserved & set(value)))
    errors=list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise SessionFailure('invalid_input','后端私有参数不符合 schema',validation_error=errors[0].message[:400])
    return dict(value)


def load_adapter(reference):
    """Import only a configured, fingerprinted lightweight adapter module."""
    path=validate_reference(reference)
    name=reference.get('module')
    prefix=(name.rpartition('.')[0] or name) if name else '_qq_media_adapter_'+hashlib.sha256(str(path).encode()).hexdigest()
    name=name or prefix
    finder=_PackageSources(prefix)
    # Reload only the adapter-owned namespace, preserving the host's registry and
    # shared SessionFailure type. Resources stay next to their installed source.
    with _load_lock:
        for loaded in list(sys.modules):
            if loaded!=__name__ and (loaded==prefix or loaded.startswith(prefix+'.')):
                sys.modules.pop(loaded,None)
        importlib.invalidate_caches()
        sys.meta_path.insert(0,finder)
        import_root=reference.get('import_root')
        if import_root:
            sys.path.insert(0,import_root)
        try:
            if reference.get('module'):
                module=importlib.import_module(name)
            else:
                spec=importlib.util.spec_from_file_location(name,path,loader=_SourceLoader(name,str(path)),
                    submodule_search_locations=[str(path.parent)])
                module=importlib.util.module_from_spec(spec)
                sys.modules[name]=module
                spec.loader.exec_module(module)
            if Path(module.__file__).resolve()!=path:
                raise SessionFailure('unavailable','adapter 导入路径与安装记录不一致')
            validate_reference(reference)
            return module.Adapter()
        except BaseException:
            for loaded in list(sys.modules):
                if loaded!=__name__ and (loaded==prefix or loaded.startswith(prefix+'.')):
                    sys.modules.pop(loaded,None)
            raise
        finally:
            sys.meta_path.remove(finder)
            if import_root:
                sys.path.remove(import_root)


class AdapterRegistry:
    def __init__(self,config,project):
        self.config,self.project=config,Path(project).resolve()
        self.providers={}
        explicit=config.get('providers') or []
        ids=[p.get('provider_id') for p in explicit]
        if len(ids)!=len(set(ids)):
            raise ValueError('duplicate_media_provider_id')
        for declaration in explicit:
            if not isinstance(declaration,dict) or set(declaration)-{'provider_id','module','path','adapter_version','model_version','config','environment','enabled','runtime_compatible_digests'}:
                raise ValueError('invalid_media_provider_declaration')
            if declaration.get('enabled',True) is False:
                continue
            provider=dict(declaration)
            compatible=provider.get('runtime_compatible_digests',[])
            if not isinstance(compatible,list) or any(not isinstance(value,str) or len(value)!=64 or
                    any(c not in '0123456789abcdef' for c in value) for value in compatible):
                raise ValueError('invalid_runtime_compatible_digests')
            provider.setdefault('config',{})
            if not isinstance(provider['config'],dict):
                raise ValueError('media_provider_config_requires_object')
            provider['config']=dict(provider['config'])
            pid=provider.get('provider_id')
            if not isinstance(pid,str) or not pid or pid in self.providers:
                raise ValueError('duplicate_or_missing_media_provider_id')
            if provider.get('module'):
                located=importlib.util.find_spec(provider['module'])
                if located is None or not located.origin:
                    raise ValueError('media_adapter_module_missing:'+pid)
                path=Path(located.origin).resolve()
                depth=len(provider['module'].split('.'))-(path.name!='__init__.py')
                provider['import_root']=str(path.parents[depth])
            else:
                path=(self.project/str(provider['path'])).resolve()
            if not path.is_file() or path.is_symlink():
                raise ValueError('media_adapter_file_missing:'+pid)
            provider.update(path=str(path),digest=hashlib.sha256(path.read_bytes()).hexdigest(),protocol=PROTOCOL)
            if not all(isinstance(provider.get(k),str) and provider[k] for k in ('adapter_version','model_version')):
                raise ValueError('media_adapter_versions_required:'+pid)
            environment=dict(provider.get('environment',{}))
            if set(environment)-{'python','entry'} or not environment.get('python'):
                raise ValueError('media_adapter_environment_required:'+pid)
            for key in ('python','entry'):
                if environment.get(key):
                    environment[key]=str((self.project/environment[key]).resolve())
            provider['environment']=environment
            adapter=load_adapter(provider)
            provider['description']=adapter.describe(provider.get('config',{}))
            description=provider['description']
            if description.get('protocol')!=PROTOCOL or not set(description.get('kinds',[]))<={'image','speech'}:
                raise ValueError('unsupported_media_adapter_protocol:'+pid)
            if description.get('resource','gpu:0')!='gpu:0':
                raise ValueError('first_media_adapter_requires_managed_gpu_lease:'+pid)
            self.providers[pid]=provider
        self.defaults=dict(config.get('defaults') or {})
        if set(self.defaults)-{'image','speech'} or any(not isinstance(v,str) or not v for v in self.defaults.values()):
            raise ValueError('invalid_media_default_configuration')
        for kind in ('image','speech'):
            if kind not in self.defaults:
                candidates=[pid for pid,p in self.providers.items() if kind in p['description']['kinds']]
                if len(candidates)==1:
                    self.defaults[kind]=candidates[0]
            if kind in self.defaults and (self.defaults[kind] not in self.providers or kind not in self.providers[self.defaults[kind]]['description']['kinds']):
                raise ValueError('invalid_default_media_provider:'+kind)

    def resolve(self,kind,provider_id=None,voice=None):
        pid=provider_id or (voice or {}).get('provider_id')
        if not pid and voice:
            candidates=[pid for pid,p in self.providers.items() if p['description'].get('engine')==voice['engine']]
            pid=candidates[0] if len(candidates)==1 else None
        pid=pid or self.defaults.get(kind)
        provider=self.providers.get(pid)
        if provider is None or kind not in provider['description']['kinds']:
            raise SessionFailure('unavailable','没有匹配且已启用的媒体后端；请读取 media 能力目录',provider_id=pid)
        return provider

    def current(self,spec):
        recorded=spec.get('_adapter',{})
        provider=self.providers.get(recorded.get('provider_id'))
        keys=('path','module','digest','adapter_version','model_version','environment','config')
        if not provider or any(recorded.get(k)!=provider.get(k) for k in keys):
            return False
        try:
            validate_reference(provider)
        except SessionFailure:
            return False
        return True

    def for_voice(self,record):
        provider=self.resolve('speech',record.get('provider_id'),record)
        if provider['description'].get('engine')!=record['engine']:
            raise SessionFailure('invalid_input','音色引擎与所选 provider 不一致')
        return provider

    def normalize_voice(self,record):
        provider=self.for_voice(record)
        adapter=load_adapter(provider)
        result=dict(record)
        result.setdefault('engine_version',provider['model_version'])
        result['provider_id']=provider['provider_id']
        parameters=validate_options(provider['description'].get('voice_parameters_schema',dict(type='object',additionalProperties=False)),result.get('parameters',{}))
        result['parameters']=adapter.normalize_voice(parameters,provider['config'])
        result['provider_revision']=provider['adapter_version']
        result['provider_digest']=provider['digest']
        if result['engine_version']!=provider['model_version']:
            raise SessionFailure('stale_context','音色模型版本与所选后端不同，需要重新试音')
        return result

    def prepare(self,kind,parameters,assets,voice=None):
        provider=self.resolve(kind,parameters.get('provider_id'),voice)
        adapter=load_adapter(provider)
        options=validate_options(provider['description'].get('parameters_schema',dict(type='object',additionalProperties=False)),parameters.get('provider_options',{}))
        spec=adapter.prepare(parameters,assets,provider['config'],voice)
        if not isinstance(spec,dict) or spec.get('kind')!=kind:
            raise SessionFailure('invalid_input','adapter 未返回有效的规范化规格')
        spec['model_version']=provider['model_version']
        if options:
            spec['provider_options']=options
        # All provider configuration comes from the installed declaration, never model arguments.
        validate_reference(provider)
        spec['_adapter']={k:v for k,v in provider.items() if k!='enabled'}
        return spec

    def catalog(self):
        result=[]
        for pid,provider in self.providers.items():
            desc=provider['description']
            schema=desc.get('parameters_schema',dict(type='object',additionalProperties=False))
            result.append(dict(ref='media:'+pid,kind='media',name=pid,summary=desc['summary'],
                invocation=dict(tool='life_media',arguments=dict(provider_id=pid)),version=provider['digest'],
                schema_ref='media:'+pid,schema=schema,limits={k:v for k,v in desc.items() if k not in {'summary','parameters_schema'}},
                provider_revision=provider['adapter_version'],model_version=provider['model_version'],
                availability=dict(registered=True,readiness='unknown',reason='未加载权重或启动服务'),evidence_refs=[]))
        return result


def frozen_hash(spec):
    return digest(spec)


def validate_frozen(spec,*,load=True):
    reference=spec.get('_adapter')
    if not isinstance(reference,dict) or reference.get('protocol')!=PROTOCOL:
        raise SessionFailure('unavailable','作业没有匹配的冻结 adapter 记录')
    return load_adapter(reference) if load else validate_reference(reference)
