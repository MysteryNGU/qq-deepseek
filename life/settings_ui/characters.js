'use strict';
let characterData=null,characterFormDirty=false;

async function openCharacterSettings(identity,name){
 if((dirty()||characterFormDirty)&&!confirm('放弃未保存修改并打开设置？'))return;
 const previous=settingsPrefix;
 settingsPrefix=identity?'/api/characters/'+encodeURIComponent(identity):'/api';
 try{snapshot=await api(settingsPrefix+'/settings')}catch(e){settingsPrefix=previous;notice(e.message,true);return}
 characterFormDirty=false;draft={};docDraft={};invalid={};category='基础与连接';
 $('#settings-target').textContent=name+' · 参数设置';await load();
}

async function renderCharacters(root){
 const data=await api('/api/characters');if(category!=='人物管理')return;
 characterData=data;root.replaceChildren();characterFormDirty=false;
 if(data.primary_retired&&settingsPrefix==='/api')$('#settings-target').textContent='默认模型与工具设置';
 const actions=element('div','character-actions');
 const create=element('button','','新建人物');create.onclick=()=>characterEditor(root,null);
 const refresh=element('button','secondary','刷新状态');refresh.onclick=()=>renderCharacters(root).catch(e=>notice(e.message,true));
 const primary=element('button','secondary',data.primary_retired?'默认模型与工具设置':'原有主实例设置');primary.onclick=()=>openCharacterSettings('',data.primary_retired?'默认配置':'原有主实例');
 actions.append(create,refresh,primary);root.append(actions);
 if(!data.characters.length){const empty=element('section','panel');empty.append(element('h2','','还没有新人物'),element('p','','从流萤、小鱼或空白人格开始，为她填写独立的 QQ 连接。原有主实例和记忆继续保留。'));root.append(empty)}
 const grid=element('div','character-grid');
 for(const person of data.characters){
  const card=element('section','panel character-card'),head=element('div','character-head');
  head.append(element('h2','',person.name),element('span','pill'+(person.wanted?'':' off'),person.state_label));card.append(head);
  card.append(element('p','',`人物 QQ ${person.bot_qq||'未配置'} · 主人 ${person.owner}`),element('small','',person.ws_url||'填写 QQ 连接地址后即可启动'));
  const status=person.status,details=element('div','character-details');
  details.append(element('span','',status.qq_connected?'QQ 已连接':'QQ 未连接'),element('span','',status.cognition_ready?'认知已就绪':'认知未就绪'),element('span','',`记忆库 ${(person.storage_bytes/1048576).toFixed(1)} MiB`));card.append(details);
  const buttons=element('div','character-actions');
  for(const [action,label] of [['start','启动'],['stop','停止'],['restart','重启']]){
   const button=element('button',action==='start'?'':'secondary',label);button.disabled=action==='start'?(!person.ready||person.wanted||person.process_owned):!(person.wanted||person.process_owned);
   button.onclick=async()=>{button.disabled=true;try{const result=await api('/api/characters/'+person.id+'/'+action,{});notice(result.message);await renderCharacters(root)}catch(e){notice(e.message,true);button.disabled=false}};buttons.append(button);
  }
  const edit=element('button','quiet',person.ready?'编辑人物':'配置 QQ 与人格');edit.disabled=person.wanted||person.process_owned;edit.onclick=()=>characterEditor(root,person);
  const settings=element('button','quiet','参数设置');settings.onclick=()=>openCharacterSettings(person.id,person.name);buttons.append(edit,settings);card.append(buttons);grid.append(card);
 }
 root.append(grid,element('p','hint','每个人物使用独立的 NapCat / OneBot 连接。此页启动人物 Bot；QQ 扫码登录在对应 NapCat 管理页完成。新人物沿用主配置的模型连接，可在各自的“参数设置”中修改。'));
}

function characterEditor(root,person){
 root.replaceChildren();const panel=element('section','panel character-editor'),form=element('form');
 panel.append(element('h2','',person?'编辑 '+person.name:'新建人物'));
 const controls={};
 function control(key,label,value,type='text'){
  const row=element('div','field'),caption=element('label','',label),field=element('input');field.type=type;field.value=value??'';
  field.required=!['ws_token','bot_qq','ws_url'].includes(key);field.id='character-'+key;caption.htmlFor=field.id;
  if(type==='number'){field.min='1';field.step='1'}
  if(key==='id'){field.pattern='[a-z][a-z0-9_-]{0,39}';field.disabled=!!person}
  if(key==='ws_token')field.autocomplete='new-password';
  controls[key]=field;row.append(caption,field);form.append(row);return field;
 }
 control('id','人物编号（建立后不变）',person?.id||'');
 control('name','人物名称',person?.name||'');
 control('bot_qq','她使用的 QQ 号',person?.bot_qq||'','number');
 control('owner','主人 QQ 号',person?.owner||snapshot.fields.find(f=>f.key==='owner')?.value||'','number');
 control('ws_url','独立 QQ 连接地址',person?person.ws_url:'');
 control('ws_token','QQ 连接密钥',person?.ws_token||'','password');
 if(!person){
  const row=element('div','field'),label=element('label','','人格模板'),select=element('select');label.htmlFor='character-template';select.id='character-template';
  const names={blank:'空白人物',xiaoyu:'原始小鱼',kafka:'卡夫卡',firefly:'流萤'};
  for(const [key,name]of Object.entries(names))if(characterData.templates[key]){const option=element('option','',name);option.value=key;select.append(option)}
  select.onchange=()=>{area.value=characterData.templates[select.value];if(!controls.name.value)controls.name.value=select.value==='blank'?'':names[select.value];characterFormDirty=true};row.append(label,select);form.append(row);
 }
 const label=element('label','','人格卡（可直接编辑，亲密交流栏目保留）'),area=element('textarea','persona-editor');area.id='character-persona';label.htmlFor=area.id;
 area.value=person?.persona||characterData.templates.blank;area.required=true;area.spellcheck=false;form.append(label,area);
 const buttons=element('div','character-actions'),save=element('button','',person?'保存人物':'建立人物'),cancel=element('button','secondary','返回人物列表');cancel.type='button';
 cancel.onclick=()=>{if(characterFormDirty&&!confirm('放弃未保存的人物修改？'))return;renderCharacters(root).catch(e=>notice(e.message,true))};buttons.append(save,cancel);form.append(buttons);
 form.oninput=()=>{characterFormDirty=true};
 form.onsubmit=async event=>{event.preventDefault();save.disabled=true;const body=Object.fromEntries(Object.entries(controls).map(([key,value])=>[key,['bot_qq','owner'].includes(key)?Number(value.value):value.value]));body.persona=area.value;
  if(person){body.revision=person.revision;body.persona_revision=person.persona_revision;delete body.id}
  try{await api('/api/characters'+(person?'/'+person.id:''),body);characterFormDirty=false;notice(person?'人物已保存。':'人物已建立，尚未启动。');await renderCharacters(root)}catch(e){notice(e.message,true);save.disabled=false}
 };
 panel.append(form);root.append(panel);
}

window.addEventListener('beforeunload',event=>{if(characterFormDirty){event.preventDefault();event.returnValue=''}});
