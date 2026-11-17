// Native Android owns photo capture, uploads and voice playback.
$('pick').onclick=()=>{document.activeElement?.blur();$('mediaSheet').style.display='flex'};
$('closeMedia').onclick=()=>{$('mediaSheet').style.display='none'};
$('mediaSheet').onclick=e=>{if(e.target===$('mediaSheet'))$('mediaSheet').style.display='none'};
$('album').onclick=()=>{$('mediaSheet').style.display='none';Native.pickImage()};
$('camera').onclick=()=>{$('mediaSheet').style.display='none';Native.takePhoto()};
window.onSpeechState=value=>{
  const state=typeof value==='string'?JSON.parse(value):value;
  const label=$('speechStatus');
  if(state.state==='error'){label.dataset.error='1';label.textContent=state.message||'暂时无法播放声音。';return}
  if(state.state==='idle'&&label.dataset.error)return;
  delete label.dataset.error;
  label.textContent=state.state==='loading'?'正在准备声音…':state.state==='playing'?(state.fallback?'正在使用手机语音 · 点此停止':'小鱼正在说话 · 点此停止'):'';
};
$('speechStatus').onclick=()=>{delete $('speechStatus').dataset.error;Native.stopSpeech();$('speechStatus').textContent=''};

// Read-only runtime view: all variable strings are rendered as text, never HTML.
function lifeText(value,fallback='未提供'){return value===null||value===undefined||value===''?fallback:String(value).slice(0,6000)}
function lifeNumber(value){return typeof value==='number'&&Number.isFinite(value)&&value>=0?value.toLocaleString('zh-CN',{maximumFractionDigits:0}):'未知'}
function lifeMoney(value){return typeof value==='number'&&Number.isFinite(value)&&value>=0?(value>0&&value<0.0001?'<¥0.0001':'¥'+value.toFixed(4)):'未知'}
function lifeTime(value){return typeof value==='number'&&Number.isFinite(value)&&value>0?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'未提供'}
function lifePhase(phase){return phase?.state==='model'?'正在请求模型':phase?.state==='tools'?'正在执行动作':phase?.state==='idle'?'当前空闲，没有正在运行的模型请求或动作':'服务未提供当前阶段'}
function lifeStatus(record){if(record?.completion==='accepted')return '已受理，尚未确认完成';const value=record?.status;return ({active:'进行中',running:'执行中',pending:'待处理',queued:'等待执行',waiting:'等待中',completed:'已完成',succeeded:'已完成',failed:'失败',error:'错误',cancelled:'已取消',paused:'已暂停',done:'已完成'})[value]||lifeText(value,'未提供状态')}
function lifeProjectOrigin(origin){return origin==='self'?'自己的兴趣':origin==='user'?'用户交办':'来源未记录'}
function lifeBlockers(blocked){const names={town:'小镇连接',phone:'手机连接或授权',desktop:'电脑连接',user:'等待用户回复',other:'其他条件'};return Array.isArray(blocked)&&blocked.length?[...new Set(blocked.map(value=>names[value]||names.other))].join('、'):'未记录阻碍'}
let motivationExpanded=false;
function renderMotivation(parent,motivation){
  const details=lifeNode('details',undefined,'life-section life-details');details.open=motivationExpanded;
  details.append(lifeNode('summary','内部倾向（模拟状态）'));
  details.ontoggle=()=>{motivationExpanded=details.open};
  details.append(lifeNode('p','这些倾向供她选择下一步；她也可以休息或改变主意。这里只读取记录。','life-note'));
  const labels={exploration:'探索',competence:'胜任',relatedness:'连接',intimacy:'亲密',libido:'性欲',coherence:'连贯',self_direction:'自主',resource_reserve:'余量'};
  const phases={accumulating:'积累中',high:'较高',regulation_pending:'等待调节',recovering:'恢复中'};
  const drives=motivation?.drives;
  if(!drives||typeof drives!=='object')details.append(lifeNode('p','当前服务没有提供内部倾向状态。','life-note'));
  else{
    const grid=lifeNode('div',undefined,'life-drive-grid');
    for(const [key,label] of Object.entries(labels)){
      const value=Array.isArray(drives[key])?drives[key]:[],pressure=value[0];
      const percent=typeof pressure==='number'&&Number.isFinite(pressure)&&pressure>=0&&pressure<=1?(pressure*100).toFixed(1)+'%':'未知';
      lifeCard(grid,label,['倾向压力 '+percent,phases[value[1]]||lifeText(value[1],'阶段未提供')]);
    }
    details.append(grid);
  }
  details.append(lifeNode('h3','最近反馈'));
  const feedback=Array.isArray(motivation?.recent_feedback)?motivation.recent_feedback:[];
  if(!feedback.length)details.append(lifeNode('p','暂无最近反馈记录。','life-note'));
  for(const item of feedback.slice(0,2)){
    const basis=item.kind==='verified_task_satisfaction'?'工具结果反馈（有实际回执依据）':item.kind==='interaction_satisfaction'?'互动反馈（有实际用户输入依据）':'反馈类型未提供';
    const delta=typeof item.delta==='number'&&Number.isFinite(item.delta)?(item.delta>0?'+':'')+(item.delta*100).toFixed(1)+' 个百分点':'未知';
    lifeCard(details,(labels[item.dimension]||'其他倾向')+' · '+basis,['张力变化：'+delta,'评估记录：'+lifeText(item.reason,'尚未记录理由')]);
  }
  details.append(lifeNode('p','反馈理由属于角色的评估记录，不能替代实际完成回执。','life-note'));
  parent.append(details);
}
function lifeNode(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=lifeText(text,'');if(className)node.className=className;return node}
function lifeCard(parent,title,lines=[],className=''){const card=lifeNode('div',undefined,'life-card '+className);card.append(lifeNode('strong',title));for(const line of lines)if(line!==null&&line!==undefined&&line!=='')card.append(lifeNode('p',line));parent.append(card);return card}
function lifeSection(parent,title){const section=lifeNode('section',undefined,'life-section');section.append(lifeNode('h3',title));parent.append(section);return section}
function renderObservability(state={}){
  if($('runtimeOverlay').style.display!=='flex')return;
  const root=document.createDocumentFragment(),obs=state.observability;
  if(!obs||typeof obs!=='object'){
    $('runtimeUpdated').textContent='详细状态尚未提供';
    lifeCard(root,lifePhase(state.phase),['当前服务没有提供详细运行记录。更新云端服务后，这里会显示项目进展和今日用量。'],'life-phase');
    if(state.next_wake)lifeCard(root,'下次计划唤醒',[lifeTime(state.next_wake)+' · 北京时间']);
    $('runtimeContent').replaceChildren(root);return;
  }
  $('runtimeUpdated').textContent='记录时间 '+lifeTime(obs.generated_at)+' · 北京时间';
  lifeCard(root,lifePhase(state.phase||obs.phase),[state.phase_after_snapshot?'阶段刚有变化，其他运行记录正在刷新。':lifeText(obs.waiting?.reason,'暂无等待原因记录')],'life-phase');
  const current=lifeSection(root,'正在做');
  const actions=!state.phase_after_snapshot&&Array.isArray(obs.current_actions)?obs.current_actions:[];
  if(state.phase_after_snapshot)current.append(lifeNode('div','当前动作记录待刷新。','life-note'));
  else if(!actions.length)current.append(lifeNode('div','没有正在执行的动作记录。','life-note'));
  for(const action of actions.slice(0,12))lifeCard(current,lifeText(action.kind,'动作')+' · '+lifeStatus(action),[
    action.project_ref?'所属项目：'+lifeText(action.project_ref):'',
    '更新于 '+lifeTime(action.updated)
  ]);
  const waiting=lifeSection(root,'接下来');
  lifeCard(waiting,'等待与项目计划',[
    obs.waiting?.next_wake?'计划唤醒：'+lifeTime(obs.waiting.next_wake)+'（北京时间）':'没有记录下一次自主唤醒时间。',
    '项目计划：'+lifeText(obs.waiting?.next_step,'暂无已记录的下一步'),
    '计划不代表已经执行；结果以实际回执为准。'
  ]);
  const projects=lifeSection(root,'兴趣与项目');
  const items=Array.isArray(obs.projects)?obs.projects:[];
  if(!items.length)projects.append(lifeNode('div','还没有记录兴趣项目。','life-note'));
  for(const project of items.slice(0,20)){
    const result=project.latest_result;
    lifeCard(projects,lifeText(project.goal,project.id||'未命名项目'),[
      lifeProjectOrigin(project.origin)+' · '+lifeStatus(project),
      '进展记录：'+lifeText(typeof project.checkpoint==='string'?project.checkpoint:null,'尚未记录')+'（记录不等于完成证据）',
      '进展更新时间：'+lifeTime(project.last_progress_at)+' · 北京时间',
      '记录的阻碍：'+lifeBlockers(project.blocked_on),
      '下一步计划：'+lifeText(project.next_step,'尚未记录'),
      result?'最近回执 · '+lifeStatus(result)+'：'+lifeText(result.summary,'无摘要'):'暂无实际执行回执。'
    ]);
  }
  renderMotivation(root,obs.motivation);
  const usage=lifeSection(root,'今日用量 · 北京时间'),today=obs.today;
  if(!today||typeof today!=='object')usage.append(lifeNode('div','服务没有提供今日用量。','life-note'));
  else{
    usage.append(lifeNode('div',lifeText(today.date)+' · Asia/Shanghai。费用为已知用量按保守价目估算的小计，不是供应商账单；未知部分不计作零。','life-note'));
    const showUsage=(name,value)=>{
      if(!value||typeof value!=='object'){lifeCard(usage,name,['该用途未提供统计。']);return;}
      lifeCard(usage,name+' · '+lifeNumber(value.calls)+' 次调用',[
        '输入 '+lifeNumber(value.input_tokens)+' · 缓存输入 '+lifeNumber(value.cached_input_tokens)+' · 输出 '+lifeNumber(value.output_tokens)+' token',
        '已知费用估算 '+lifeMoney(value.known_cny)+'；费用未知 '+lifeNumber(value.unknown_calls)+' 次'
      ]);
    };
    showUsage('合计',today.total);
    const labels={dialogue:'对话',activity:'自主生活',work:'工作推进',town:'小镇',other:'其他'};
    for(const [key,label] of Object.entries(labels))showUsage(label,today.groups?.[key]);
  }
  const results=lifeSection(root,'近期结果');
  const receipts=Array.isArray(obs.recent_results)?obs.recent_results:[];
  if(!receipts.length)results.append(lifeNode('div','暂无近期执行回执。','life-note'));
  for(const result of receipts.slice(0,12))lifeCard(results,lifeText(result.kind,'动作')+' · '+lifeStatus(result),[lifeText(result.summary,'无摘要'),lifeTime(result.updated)]);
  const errors=lifeSection(root,'近期错误');
  const failures=Array.isArray(obs.errors)?obs.errors:[];
  if(!failures.length)errors.append(lifeNode('div','当前记录里没有近期错误。','life-note'));
  for(const failure of failures.slice(0,8))lifeCard(errors,lifeTime(failure.created),[lifeText(failure.summary,'未提供错误摘要')],'life-warning');
  $('runtimeContent').replaceChildren(root);
}
// runtime=1 必须传：服务端只有在它存在时才附带 observability（companion_mobile.py:294），
// 否则界面永远是「服务没有提供详细运行记录」。
$('openRuntime').onclick=()=>{document.activeElement?.blur();$('overlay').style.display='none';$('runtimeOverlay').style.display='flex';renderObservability(latestState);refresh(true)};
$('closeRuntime').onclick=()=>{$('runtimeOverlay').style.display='none'};
$('runtimeOverlay').onclick=event=>{if(event.target===$('runtimeOverlay'))$('runtimeOverlay').style.display='none'};
$('refreshRuntime').onclick=()=>refresh(true);
