"""Local settings source of truth. No bot/database initialization or shell API."""
from dataclasses import asdict,fields
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import tomllib
from typing import get_type_hints

from .role_config import Config,load_config

MASK='••••••••'
SECRET=re.compile(r'(?:^|_)(?:api_?key|(?<!per_)token|password|secret|authorization|cookie)$',re.I)
CATEGORIES={
    'name':'基础与连接','bot_qq':'基础与连接','owner':'基础与连接','friends':'基础与连接','known_people':'基础与连接','allow_groups':'基础与连接',
    'allow_all_private':'基础与连接','ws_url':'基础与连接','ws_token':'基础与连接',
    'group_at_only':'基础与连接','group_initiative':'基础与连接',
    'api_key':'模型与工具','base_url':'模型与工具','model':'模型与工具','public_tools':'模型与工具','consult_config':'模型与工具',
    'resource_policy':'行为与节奏','continuity_enabled':'行为与节奏','acknowledge_before_work':'行为与节奏',
    'typing_chars_per_second':'表达与语音','paste_threshold':'表达与语音','local_media':'表达与语音',
    'media_local_roots':'表达与语音','media_max_mb':'表达与语音','asr_model':'表达与语音','asr_language':'表达与语音',
    'same_person_group_recall':'记忆与人物','profile_review_enabled':'记忆与人物','memory_index':'记忆与人物',
    'memory_organize':'记忆与人物','embedding':'记忆与人物','local_embedding':'记忆与人物',
    'spending_policy':'预算与限制','action_per_hour':'预算与限制','action_per_day':'预算与限制',
    'experience':'行为与节奏','life_dynamics':'行为与节奏',
    'activity':'行为与节奏','profile_backfill':'记忆与人物',
    'media_library':'表达与语音',
}
LABELS=dict(name='显示名称',bot_qq='人物 QQ（0 表示不核验）',owner='主人 QQ',friends='允许私聊的好友',known_people='联系人名称与 QQ',allow_groups='允许的群聊',
    activity='连续活动',profile_backfill='历史人物画像复核',
    media_library='表情与图片收藏',
    allow_all_private='接收所有私聊',group_at_only='群消息仅在被艾特时唤醒',
    group_initiative='群里可以自己想起来查看和开口',ws_url='QQ 连接地址',ws_token='QQ 连接密钥',api_key='模型 API 密钥',base_url='模型接口地址',model='模型名称',
    public_tools='公开场景可用工具',consult_config='咨询配置位置',resource_policy='等待时间与上下文限额',continuity_enabled='持续生活',
    acknowledge_before_work='先应答再执行',typing_chars_per_second='文字发送节奏（字/秒）',paste_threshold='长消息发送阈值',
    local_media='语音、图片与本地生成',media_local_roots='可用媒体目录',media_max_mb='媒体大小上限（MB）',asr_model='语音识别模型',
    asr_language='语音识别语言',same_person_group_recall='本人群聊即时接到私聊',profile_review_enabled='逐人复核画像',
    memory_index='检索与索引',memory_organize='摘要整理参数',embedding='向量接口',local_embedding='本地向量服务',
    spending_policy='预算保护与计价',action_per_hour='每小时动作上限',action_per_day='每天动作上限',experience='专项体验参数',
    life_dynamics='基础驱动力参数',persona='统一人格来源',unified_persona='统一人格模式',public_persona='旧公开人格路径（停用）',
    life_db='记忆数据库位置',life_agent='角色身份编号',capability_policies='能力授权策略',idle_seconds='旧等待配置（已被随机等待取代）',
    handoff_seconds='处理接续等待（秒）',dsh_timeout='认知调用超时（秒）',dsh_provider='认知服务提供方',dsh_root='认知程序目录',
    dsh_home='认知数据目录',dsh_node='Node 运行程序',dsh_tmp='认知临时目录',dsh_pnpm_entry='pnpm 入口',bili_plugin='B站插件目录')
READ_ONLY={'life_db','life_agent','persona','unified_persona','public_persona','idle_seconds'}


class SettingsError(ValueError):
    def __init__(self,code,message,status=400):self.code,self.status=code,status;super().__init__(message)


def fingerprint(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'missing'


def sanitize(value,key=''):
    if SECRET.search(key) and value not in ('',None):return MASK
    if isinstance(value,dict):return {k:sanitize(v,k) for k,v in value.items()}
    if isinstance(value,list):return [sanitize(v) for v in value]
    return value


def restore(value,original):
    if value==MASK:return original
    if isinstance(value,dict):return {k:restore(v,original.get(k) if isinstance(original,dict) else None) for k,v in value.items()}
    if isinstance(value,list):
        previous=original if isinstance(original,list) else []
        result=[]
        for i,v in enumerate(value):
            old=previous[i] if i<len(previous) else None
            if isinstance(v,dict):
                identity=next((k for k in ('id','provider_id','name') if isinstance(v.get(k),str)),None)
                if identity:old=next((p for p in previous if isinstance(p,dict) and p.get(identity)==v[identity]),None)
            result.append(restore(v,old))
        return result
    return value


def toml_value(value):
    if isinstance(value,str):return json.dumps(value,ensure_ascii=False)
    if type(value) is bool:return 'true' if value else 'false'
    if type(value) in (int,float) and math.isfinite(value):return repr(value)
    if isinstance(value,list):return '['+', '.join(toml_value(v) for v in value)+']'
    if isinstance(value,dict):return '{'+', '.join(json.dumps(k)+' = '+toml_value(v) for k,v in value.items())+'}'
    raise SettingsError('invalid_value','配置不能包含空对象值、非有限数字或未知类型')


def encode_toml(value):
    return '# 小鱼设置；由本机设置服务校验后写入。\n'+'\n'.join(json.dumps(k)+' = '+toml_value(v) for k,v in value.items() if v is not None)+'\n'


def atomic_write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.settings-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as out:out.write(text);out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


class SettingsStore:
    def __init__(self,root):
        self.root=Path(root).resolve();self.lock=threading.RLock()
        self.folder=self.root/'data/settings';self.folder.mkdir(parents=True,exist_ok=True)
        self.config_path=self.root/'config.toml'

    def raw(self):return tomllib.loads(self.config_path.read_text('utf-8'))

    def registered_path(self,document):
        if document=='config':path=self.config_path
        elif document=='dynamics':path=self.root/'life-dynamics.json'
        elif document=='consult':
            value=self.raw().get('consult_config','')
            if not value:raise SettingsError('not_configured','尚未配置咨询文件')
            path=self.root/value
        elif document=='voices':
            value=self.raw().get('local_media',{}).get('preset_manifest','')
            if not value:raise SettingsError('not_configured','尚未配置音色预设文件')
            path=self.root/value
        else:raise SettingsError('unknown_document','不支持的设置文档')
        if not path.resolve().is_relative_to(self.root) or any(p.is_symlink() for p in (path,*path.parents) if p!=self.root):
            raise SettingsError('invalid_path','设置文件必须位于项目内且不能经过符号链接')
        return path.resolve()

    def snapshot(self):
        with self.lock:
            raw=self.raw();defaults=asdict(Config());values=dict(defaults,**raw)
            # Show normalized nested defaults so every supported parameter is discoverable.
            try:
                loaded=load_config(self.config_path)
                for key in values:
                    if isinstance(values[key],dict):values[key]=getattr(loaded,key)
                error=None
            except Exception:error='当前配置未通过校验；仍可查看保存值，请检查最近修改的字段。'
            docs={}
            for name in ('dynamics','consult','voices'):
                try:
                    path=self.registered_path(name)
                    if path.exists() or name=='dynamics':
                        value=json.loads(path.read_text('utf-8')) if path.exists() else {}
                        if name=='voices':value=dict(default_voice_id=value.get('default_voice_id'),presets=[{k:p[k] for k in ('id','name','revision','delivery')} for p in value['presets']])
                        docs[name]=dict(revision=fingerprint(path),value=sanitize(value))
                except (SettingsError,ValueError,KeyError,TypeError,OSError):pass
            return dict(revision=fingerprint(self.config_path),fields=[dict(key=k,label=LABELS.get(k,k),category=CATEGORIES.get(k,'高级运行设置'),
                value=sanitize(v,k),type='boolean' if type(v)is bool else 'number' if type(v) in (int,float) else 'json' if isinstance(v,(dict,list)) else 'text',
                secret=bool(SECRET.search(k)),read_only=k in READ_ONLY or k not in defaults,default=sanitize(defaults.get(k),k),
                environment_override=(k=='api_key' and bool(os.environ.get('DEEPSEEK_API_KEY')) or k=='ws_url' and bool(os.environ.get('QQ_WS_URL')))) for k,v in values.items()],
                documents=docs,validation_error=error,status=self.status())

    def status(self):
        health_path=self.root/'data/life-control/worker-health.json'
        try:health=json.loads(health_path.read_text('utf-8'))
        except (OSError,ValueError):health={}
        online=time.time()-health.get('at',0)<15
        saved={name:fingerprint(self.registered_path(name)) for name in ('config','dynamics')}
        for name in ('consult','voices'):
            try:saved[name]=fingerprint(self.registered_path(name))
            except SettingsError:pass
        effective=health.get('settings_revisions',{}) if online else {}
        return dict(online=online,qq_connected=bool(online and health.get('channel_connected')),cognition_ready=bool(online and health.get('cognition_ready')),
            runtime=health.get('runtime',{}),saved_revisions=saved,effective_revisions=effective,
            pending_restart=any(effective.get(k)!=v for k,v in saved.items()),
            memory_organize=health.get('memory_organize',{}),memory_index=health.get('memory_index',{}),
            profile_backfill=health.get('profile_backfill',{}),
            local_embedding=health.get('local_embedding',{}),spending=health.get('spending',{}))

    def validate(self,document,value):
        if not isinstance(value,dict):raise SettingsError('invalid_value','设置需要一个对象')
        if document=='config':
            known={f.name for f in fields(Config)}
            if set(value)-known:raise SettingsError('unknown_field','存在未注册配置项')
            defaults=asdict(Config())
            types=get_type_hints(Config)
            for key,v in value.items():
                default=defaults[key]
                if v is None and default is None:continue
                if type(default)is bool and type(v)is not bool or types[key] is int and type(v)is not int or isinstance(default,str) and not isinstance(v,str) or isinstance(default,list) and not isinstance(v,list) or isinstance(default,dict) and not isinstance(v,dict):
                    raise SettingsError('invalid_type',LABELS.get(key,key)+' 的类型不正确')
                if types[key] is float and (type(v) not in (int,float) or not math.isfinite(v)):
                    raise SettingsError('invalid_type',LABELS.get(key,key)+' 必须是有限数值')
            for key in ('owner','friends','allow_groups'):
                entries=value.get(key,defaults[key]);entries=entries if isinstance(entries,list) else [entries]
                if any(type(v)is not int or v<=0 for v in entries):raise SettingsError('invalid_qq','QQ/群号必须是正整数')
            for key in ('action_per_hour','action_per_day'):
                if value.get(key,defaults[key])<0:raise SettingsError('invalid_limit','动作限制不能为负数')
            text=encode_toml(value)
        else:text=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
        fd,temp=tempfile.mkstemp(suffix='.toml' if document=='config' else '.json',dir=self.folder)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(text)
            if document=='config':load_config(Path(temp))
            elif document=='consult':
                from scripts.codex_consult import Bridge
                Bridge(temp)
            elif document=='voices':
                from .media_adapters.indextts import normalize_delivery
                presets=value.get('presets',[])
                if not presets or len({p['id'] for p in presets})!=len(presets):raise ValueError('invalid_voice_presets')
                for preset in presets:
                    if not isinstance(preset['name'],str) or not 1<=len(preset['name'])<=80:raise ValueError('invalid_voice_name')
                    normalize_delivery(preset['delivery'])
                if not any(p['id']==value['default_voice_id'] and p['revision']==value['default_voice_revision'] for p in presets):
                    raise ValueError('invalid_default_voice')
            else:
                from types import SimpleNamespace
                from .self_change import SelfChange
                from .runtime import LifeConfig
                from .drives import DriveConfig
                from .relationships import default_registry
                validator=SelfChange.__new__(SelfChange)
                validator.life=SimpleNamespace(config=LifeConfig(drives=DriveConfig.from_mapping(self.raw().get('life_dynamics',{}))),relationships=SimpleNamespace(registry=default_registry()))
                validator.behavior(candidate=value)
        except SettingsError:raise
        except Exception as exc:
            # Validation exceptions are codes/field names, never echo proposed secrets.
            raise SettingsError('validation_failed','配置校验失败：'+type(exc).__name__+'；请检查字段类型、上下限及路径') from None
        finally:Path(temp).unlink(missing_ok=True)
        return text

    def save(self,document,base_revision,changes):
        from scripts.codex_consult import file_lock
        with self.lock,file_lock(self.folder/'write.lock'):
            path=self.registered_path(document)
            if fingerprint(path)!=base_revision:raise SettingsError('conflict','配置已在别处更新，请重新载入后合并修改',409)
            old=tomllib.loads(path.read_text('utf-8')) if document=='config' else json.loads(path.read_text('utf-8')) if path.exists() else {}
            if not isinstance(changes,dict):raise SettingsError('invalid_changes','修改内容需要一个对象')
            if document=='config':
                if set(changes)&READ_ONLY:raise SettingsError('read_only','身份、数据库及旧人格兼容字段只读，不通过网页迁移')
                merged=dict(old)
                defaults=asdict(Config())
                for key,value in changes.items():
                    if key not in defaults:raise SettingsError('unknown_field','存在未注册配置项')
                    value=restore(value,old.get(key,defaults[key]))
                    if value is None:merged.pop(key,None)
                    else:merged[key]=value
            elif document=='voices':
                # Asset paths and provenance are immutable in the settings UI.
                if set(changes)!={'default_voice_id','presets'} or not isinstance(changes['presets'],list):raise SettingsError('invalid_voice','音色设置格式无效')
                previous={p['id']:p for p in old['presets']}
                if {p.get('id') for p in changes['presets']}!=set(previous) or len(changes['presets'])!=len(previous):raise SettingsError('invalid_voice','只能修改已验证音色的名称、表达参数和默认选择')
                merged=dict(old);presets=[]
                for proposed in changes['presets']:
                    prior=previous[proposed['id']]
                    if set(proposed)!={'id','name','revision','delivery'} or proposed['revision']!=prior['revision']:raise SettingsError('conflict','音色版本或字段已变化',409)
                    changed=proposed['name']!=prior['name'] or proposed['delivery']!=prior['delivery']
                    presets.append(dict(prior,name=proposed['name'],delivery=proposed['delivery'],revision=prior['revision']+1 if changed else prior['revision']))
                default=next((p for p in presets if p['id']==changes['default_voice_id']),None)
                if default is None:raise SettingsError('invalid_voice','默认音色必须来自已验证预设')
                merged.update(presets=presets,default_voice_id=default['id'],default_voice_revision=default['revision'])
            else:merged=restore(changes,old)
            text=self.validate(document,merged)
            if fingerprint(path)!=base_revision:raise SettingsError('conflict','校验期间配置已变化',409)
            atomic_write(path,text)
            entry=dict(at=time.time(),document=document,before=base_revision,after=fingerprint(path),keys=sorted(changes))
            with (self.folder/'changes.jsonl').open('a',encoding='utf-8') as audit:audit.write(json.dumps(entry)+'\n')
            return dict(saved=True,revision=entry['after'],effective=False,restart_required=True)

    def request_restart(self):
        if not self.status()['online']:raise SettingsError('offline','Bot 当前离线；配置已保存，启动后生效',409)
        import uuid
        request=dict(id=uuid.uuid4().hex,created=time.time(),action='restart_when_idle')
        atomic_write(self.folder/'restart.request.json',json.dumps(request))
        return dict(queued=True,request_id=request['id'],message='等待本轮操作结束后重启，保存状态与实际生效状态分开核对')
