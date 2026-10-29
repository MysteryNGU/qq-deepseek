"""Versioned life-tool field definitions shared with the negotiated DSH adapter.

Schemas use DSH's enforced JSON Schema subset. Business checks (source access,
task revisions, waiting, delivery and spending) still run after field validation.
Open provider-owned option maps are intentional; their provider validates them.
"""
from functools import lru_cache
from copy import deepcopy
from jsonschema import Draft7Validator

from .agent_session import SessionFailure,digest


def string(*values):
    return dict(type='string',**(dict(enum=list(values)) if values else {}))


def obj(properties=None,required=(),*,open=False):
    return dict(type='object',properties=properties or {},required=list(required),additionalProperties=open)


def array(items):
    return dict(type='array',items=items)


S=string(); N=dict(type='number'); I=dict(type='integer'); B=dict(type='boolean')
STRINGS=array(S)
OPTIONS=obj(open=True)
CURSOR=dict(oneOf=[S,I,OPTIONS,dict(type='null')])
CHECKPOINT=dict(oneOf=[S,obj(dict(method_refs=STRINGS,next_step=S,remaining=STRINGS,evidence_refs=STRINGS,
    operation_ids=STRINGS,asset_ids=STRINGS),open=True)])
WAIT=obj(dict(intent_ref=S,base_revision=I,actor_id=S,question=S,checkpoint=S),('intent_ref','base_revision'))
ACTIVITY=obj(dict(intent_ref=S,base_revision=I,decision=string('continue','revise','pause','cancel','unrelated'),
    patch=obj(dict(goal=S,checkpoint=CHECKPOINT,topic_key=S))),('intent_ref','base_revision','decision'))
CONTINUITY=obj(dict(type=string('thought','reflection'),action=string('create','replace','archive'),
    base_ref=S,base_version=I,batch_ref=S,reviewed_ids=STRINGS,outcome=string('summarized','no_new_insight')),('type',))
CODE_EDIT=dict(oneOf=[obj(dict(file=S,old=S,new=S),('file','old','new')),
    obj(dict(file=S,action=dict(type='string',const='create'),content=S),('file','action','content'))])
PATCH=obj(dict(file=S,old=S,new=S,changes=array(CODE_EDIT),
    migrations=array(obj(dict(id=S,sql=STRINGS),('id','sql'))),drives=OPTIONS,
    affect=obj(dict(half_life=OPTIONS))))
PROBE=obj(dict(module=S,function=S,arguments=OPTIONS,expected=dict(description='Expected lossless JSON value of the independent probe.'),
    critical=B),('module','function','arguments','expected'))
RELATION_NOTE=obj(dict(operation=string('create','replace','resolve','revoke'),actor_id=S,
    relation_kind=string('preference','agreement','repair','expectation','role_description'),
    origin=string('explicit_statement','interpretation'),text=S,sources=STRINGS,confidence=N,
    salience=string('low','normal','high'),base_note_ref=S,lifetime_days=N,applicability=S,intent_ref=S),
    ('operation','actor_id'))
APPRAISAL=obj(dict(sources=STRINGS,source_event_ids=STRINGS,interpretation=S,meaning_kind=S,self_text=S,
    subject_actor_id=S,affect_impulse=obj({k:N for k in ('joy','concern','frustration','hurt','anticipation')}),
    certainty=N,confidence=N,novelty=N,revision=I,supersedes_appraisal_id=S,
    recovery_half_life_seconds=obj({k:N for k in ('joy','concern','frustration','hurt','anticipation')}),
    persistence_reason=S,context_refs=STRINGS,based_on_state_version=I,relation_protocol=dict(type='integer',const=2),
    based_on_relationship_version=I,relation_impacts=array(obj(dict(
        metric=string('familiarity','trust','comfort','bond'),direction=string('increase','decrease','unchanged'),
        magnitude=string('slight','meaningful','major'),confidence=N),('metric','direction','magnitude','confidence'))),
    relation_context_changes=array(RELATION_NOTE),drive_evidence=array(obj(dict(dimension=S,need=S,kind=S,
        effect_kind=S,delta=N,confidence=N,reason=S,sources=STRINGS,evidence_refs=STRINGS,
        interaction_ref=S,resolution_ref=S,satisfaction_ref=S),open=True)),
    resolves_cause_ids=STRINGS,quoted_or_hypothetical=B,negated=B,user_need=S,target=S,affected_need_ids=STRINGS))
PROFILE_REVIEWS=array(obj(dict(actor_id=S,sources=STRINGS,reason=S),('actor_id','sources','reason')))
MOTIVATION=obj(dict(ref=S,base_revision=I,decision=string('act','defer','decline'),reason=S,after_seconds=N),
    ('ref','base_revision','decision','reason'))
CONTACT_BASIS=dict(oneOf=[
    obj(dict(kind=string('followup','shared_experience','anniversary'),sources=STRINGS),('kind','sources')),
    obj(dict(kind=string('missing_you'),dimension=string('relatedness','intimacy'),cycle_id=I),
        ('kind','dimension','cycle_id'))])
COMMON=dict(sources=STRINGS,source_event_ids=STRINGS,handled_input_ids=STRINGS,handled_batch_ref=S,
    appraisal=APPRAISAL,activity_changes=array(ACTIVITY))


@lru_cache(maxsize=1)
def contracts():
    read_fields=dict(kind=string('self','relationship','relationship_semantics','intents','reminders','appraisals',
        'self_changes','skill','usage','observation','message','voices','media','media_jobs','media_collection',
        'motivations','person_profile','role_protocol','simulation','experience_review','recall','life_source',
        'memory_index','continuity','capabilities','media_capabilities','memory_batch','contact_policy','interaction_mode'),
        intent=string('auto','facts','recent','history'),diagnostics=B,
        category=string('tools','methods','media'),query=S,ref=S,cursor=CURSOR,limit=I,actor_id=S,offset=I,
        version=S,field=string('schema'),load=B,schema_hash=S,provider_id=S,name=S,event_id=S,result_ref=S,
        mode=S,status=S,max_tokens=I,start_line=I,offset_chars=I,before_lines=I,after_lines=I,limit_matches=I,
        include_full=B,related=B,message_id=S,logical_message_id=S,persona_audience=string('private','public'),
        asset_id=S,perception=string('transcript','frame_set'),timestamps=array(N),action=S,path=S,media_kind=S,
        discover=B,language=S,view=S,since=N,until=N,intent_ref=S,session_id=S,root_session_id=S,turn=I,
        after_transition_seq=I,**{'global':B})
    part=dict(oneOf=[obj(dict(kind=dict(type='string',const='text'),text=S,mode=string('auto','single','artifact')),
        ('kind','text')),obj(dict(kind=string('image','audio'),asset_id=S),('kind','asset_id')),
        obj(dict(kind=dict(type='string',const='voice'),text=S,voice_id=S,parameters=OPTIONS),('kind','text'))])
    schemas={
        'life_read':obj(dict(COMMON,**read_fields),('kind',)),
        'life_say':obj(dict(COMMON,profile_reviews=PROFILE_REVIEWS,text=S,parts=array(part),mode=string('auto','single','artifact'),
            purpose=string('final','notice'),end_turn=B,new_message=B,wait_for_reply=WAIT,
            contact_basis=CONTACT_BASIS,motivation_decisions=array(MOTIVATION),activity_blocker=S,
            experience_run_ref=S,experience_control_revision=I,experience_request_ref=S,experience_initiation_ref=S,continuity_refs=STRINGS)),
        'life_rest':obj(dict(COMMON,profile_reviews=PROFILE_REVIEWS,thought=S,reason=S,keep_thought=B,thought_cues=STRINGS,
            motivation_decisions=array(MOTIVATION),activity_blocker=S,
            continuity_refs=STRINGS,reflection_outcome=string('unchanged','new_thought','action','unspecified'),
            wake_after_seconds=N,next_attention=obj(dict(mode=string('adaptive','after','event','none'),
                after_seconds=N,intent_ref=S,reason=S),('mode',)))),
        'life_appraise':APPRAISAL,
        'life_intent':obj(dict(COMMON,action=string('save','resolve','wait','pause','cancel','resume','done','merge','transfer','set_mode'),
            mode=string('chat','tools'),
            execution_mode=string('manual','continuous'),result_sources=STRINGS,
            motivation=MOTIVATION,
            ref=S,base_revision=I,target_ref=S,target_base_revision=I,goal=S,checkpoint=CHECKPOINT,
            kind=string('once','ongoing'),origin=string('user','self'),topic_key=S,parent_ref=S,
            end_direction=B,bind=B,interest=OPTIONS,question=S,actor_id=S,wake_after_seconds=N,wake_reason=S)),
        'life_remember':obj(dict(COMMON,kind=string('experience','preference','reminder','inference','imagination',
            'roleplay','relationship_note','person_profile','person_profile_review','media_collection','organized_memory'),
            text=S,action=S,cues=STRINGS,always_load=B,topic_key=S,supersedes=STRINGS,applicability=S,intent_ref=S,
            reference_asset_ids=STRINGS,skill_refs=STRINGS,memory_id=S,lifetime_days=N,correction=B,change=B,
            continuity=CONTINUITY,operation=S,actor_id=S,field=S,origin=S,confidence=N,salience=S,
            base_revision=I,base_note_ref=S,relation_kind=S,coverage_digest=S,claims=array(obj(dict(text=S,
                kind=string('observed','reported','interpreted','hypothetical'),evidence_refs=STRINGS),('text','kind','evidence_refs'))),
            omitted_refs=STRINGS,unresolved_refs=STRINGS,ordinary_fragments=array(OPTIONS),
            stable_facts=array(obj(dict(actor_id=S,topic_key=S,quote=S,evidence_refs=STRINGS,
                operation=string('observe','correct','change')),('actor_id','topic_key','quote','evidence_refs'))),
            reviewed_input_ids=STRINGS,profile_version=S,reason=S,expires=N,asset_id=S,label=S,tags=STRINGS)),
        'life_media':obj(dict(COMMON,action=string('submit','status','cancel'),kind=string('image','speech'),
            job_id=S,prompt=S,text=S,width=I,height=I,seed=I,reference_asset_ids=STRINGS,
            provider_id=S,provider_options=OPTIONS,intent_ref=S,voice_id=S,voice_revision=S,delivery=OPTIONS,
            segments=array(obj(dict(text=S,pause_after_ms=N,delivery=OPTIONS),('text',))))),
        'life_consult':obj(dict(COMMON,action=string('ask','status','read','cancel','close'),message=S,
            topic_id=S,request_id=S,intent_ref=S,cursor=CURSOR,limit_bytes=I)),
        'life_change_self':obj(dict(COMMON,target=string('persona','persona_public','skill','dynamics','code','dsh',
            'worker','plugin','voice'),content=S,base_hash=dict(oneOf=[S,dict(type='null')]),name=S,create=B,
            metadata=obj(dict(description=S,visibility=string('private','public'),sources=STRINGS,capability_refs=STRINGS,public_content=B)),
            patch=PATCH,probes=array(PROBE),edits=array(obj(dict(path=S,old=S,new=S),('path','old','new'))),
            files=array(obj(dict(path=S,content=S),('path','content'))),
            reason=S,verification_goal=S,component=string('dsh','media'),action=S,bundle=S,
            voice=OPTIONS,voice_id=S,sample_text=S,**{'global':B})),
        'life_simulation':obj(dict(COMMON,experience_protocol=I,experience_domain=string('adult_intimate'),
            action=string('enter','appraise_input','start','adjust','pause','resume','stop','exit','episode.end','ack',
                'permission.respond','private.begin','private.end','initiation.select'),
            base_revision=I,subject_actor_id=S,adult_participants=B,context_confirmed=B,willingness=S,
            direction=string('increase','decrease','unchanged'),effects=obj(dict(arousal=N,valence=N,engagement=N)),
            confidence=N,reason=S,profile_confirmed=B,authorization_mode=string('one_shot','until_cancel'),input_profile=obj(dict(method=S,intensity=N,valence=N,
                rhythm=string('continuous','intermittent'),on_seconds=N,off_seconds=N)),run_ref=S,
            transition_refs=STRINGS,supersedes_call_key=S,request_id=S,decision=string('allow','deny','uncertain','act','defer','decline'),
            evidence_ref=S,evidence_text=S,run_id=S,outcome=string('completed','aborted'),candidate_id=S,episode_id=S),('action','experience_protocol','experience_domain','base_revision')),
    }
    summaries={
        'life_read':'读取状态、来源和能力。图片用 kind:media,asset_id；主人本机截图用 kind:media,action:import,path。成功后原图进入同一会话供视觉查看。精确 ref+schema_hash+load:true 加载工具。',
        'life_say':'向当前对话发送自然表达。理解当前输入批次后同次提交 handled_batch_ref（含 notice），也可逐条用 handled_input_ids；不必另加 resolve。最后一条显式 end_turn:true。未知发送先对账。',
        'life_rest':'结束当前轮。理解当前输入批次后同次提交 handled_batch_ref，也可逐条用 handled_input_ids；未读输入不能确认。沿用已有随机空闲间隔。',
        'life_remember':'按真实来源保存记忆或叮嘱。人物的称呼/长期偏好用 kind:person_profile,actor_id,field:address|preference,topic_key,text,origin:explicit_statement,sources,base_revision:0；更正先读当前版本。可用 handled_input_ids 一并确认已理解输入。',
        'life_intent':'保存或更新当前打算；等待与停止须按当前版本执行。action:set_mode,mode:tools|chat 切换工作与日常，无需创建任务，不能覆盖主人选定的模式。',
        'life_appraise':'评价真实语义、情绪、需要或人物关系。普通中性评价可只交 sources,interpretation；无关系影响不填 relation_impacts。不猜版本；关系影响须 relation_protocol:2,subject_actor_id 和读取所得 based_on_relationship_version；based_on_state_version 须取读回的 state_version，不用日志序号。',
        'life_media':'异步生成媒体；生成、检查和送达分别确认。',
        'life_consult':'异步咨询 Codex；未知结果须核对原 request_id。',
        'life_change_self':'按原发布流程修改人格、方法、代码或插件，核对生效回执。',
        'life_simulation':'仅当前明确成年亲密情境的专项体验，先读状态与完整 role_protocol。',
    }
    return {name:dict(name=name,description=summaries[name],parameters=obj(dict(proposal=schema),('proposal',)),
        schema_ref='tool:dsh:'+name,schema_hash=digest(dict(name=name,parameters=schema)),version=1)
        for name,schema in schemas.items()}


def catalog(*,work=False):
    definitions=contracts()
    if work:
        definitions=deepcopy(definitions)
        for name,item in definitions.items():
            schema=item['parameters']['properties']['proposal']
            for key in list(schema['properties']):
                if key in {'appraisal','profile_reviews','motivation_decisions','contact_basis'} or key.startswith('experience_'):
                    schema['properties'].pop(key)
                    if key in schema['required']:schema['required'].remove(key)
            if name in {'life_appraise','life_simulation'}:
                schema=obj()
                item['parameters']['properties']['proposal']=schema
                item['description']='日常状态能力；工作期间无需调用，主角色可用 life_intent(action:set_mode,mode:chat) 切回。'
            elif name=='life_remember':
                fields={'sources','handled_input_ids','handled_batch_ref','kind','text','topic_key','applicability','intent_ref','always_load'}
                schema['properties']={k:v for k,v in schema['properties'].items() if k in fields}
                schema['properties']['kind']=string('experience','preference','reminder')
                item['description']='按真实来源保存任务经验或明确约定；默认仅当前任务适用。'
            elif name=='life_read':
                schema['properties']['kind']['enum']=[k for k in schema['properties']['kind']['enum'] if k not in
                    {'relationship','relationship_semantics','appraisals','motivations','person_profile','simulation','experience_review','contact_policy'}]
            item['schema_hash']=digest(dict(name=name,parameters=schema))
    return dict(version=digest(definitions),items=list(definitions.values()))


@lru_cache(maxsize=16)
def _validator(name):
    return Draft7Validator(contracts()[name]['parameters'])


def validate(name,proposal):
    return validate_arguments(name,dict(proposal=proposal))


def validate_arguments(name,arguments):
    contract=contracts().get(name)
    if not contract:return
    errors=sorted(_validator(name).iter_errors(arguments),key=lambda e:str(e.path))
    if errors:
        raise SessionFailure('invalid_input','工具字段无效：'+errors[0].message,field='.'.join(map(str,errors[0].path)))
    proposal=arguments['proposal']
    # Cross-field requirements remain explicit where the provider subset cannot
    # express if/then without duplicating all common fields in every branch.
    required=[]
    if name=='life_say' and not proposal.get('text') and not proposal.get('parts'):required=['text 或 parts']
    if name=='life_read' and proposal.get('load'):
        required=['ref','schema_hash']
    if name=='life_intent' and proposal.get('action','save') not in {'save','resolve','set_mode'}:required=['ref','base_revision']
    if name=='life_intent' and proposal.get('action')=='set_mode':required=['mode']
    if name=='life_media' and proposal.get('action','submit') in {'status','cancel'}:required=['job_id']
    if name=='life_change_self':
        required=['target']
        if proposal.get('target') in {'persona','persona_public'}:
            required+=['base_hash']
            if 'files' not in proposal:required+=['content']
    missing=[key for key in required if key not in proposal]
    if missing:raise SessionFailure('invalid_input','缺少工具字段：'+', '.join(missing))
