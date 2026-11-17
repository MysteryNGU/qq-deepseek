/** Read-only display: no chat, action or model request is made from this page. */
const phaseNames={model:'正在调用模型',tools:'正在执行行动',idle:'空闲'};
const groupNames={dialogue:'聊天',activity:'自主活动',work:'项目执行',town:'小镇',other:'其他'};
const blockerNames={town:'小镇',phone:'手机',desktop:'桌面',user:'等待用户',other:'其他'};
const driveNames={exploration:'探索',competence:'胜任',relatedness:'连接',intimacy:'亲密',libido:'性欲',coherence:'连贯',self_direction:'自主',resource_reserve:'余量'};
const drivePhases={accumulating:'积累中',high:'较高',regulation_pending:'待调节',recovering:'恢复中'};
const statusNames={active:'进行中',waiting:'等待',paused:'暂停',done:'项目标记结束',cancelled:'取消',
 completed:'执行完成',accepted:'已接受',partial:'部分完成',failed:'失败',interrupted:'已中断',running:'正在执行',pending:'待执行'};
const time=(v)=>typeof v==='number'?new Date(v*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未安排';
const status=(v)=>statusNames[v]||v||'未报告';
function node(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=String(text);if(className)el.className=className;return el}
function card(title){const el=node('article',undefined,'monitor-card');el.append(node('h2',title));return el}
function receipt(row){
 const el=node('div',undefined,'monitor-result');
 const evidence=row.completion==='accepted'?'已接受（尚非完成证据）':status(row.status);
 el.append(node('strong',`${row.kind||'行动'} · ${evidence}${row.completion&&row.completion!=='accepted'?' · '+status(row.completion):''}`));
 el.append(node('p',row.summary||'没有文本回执'),node('small',`${time(row.updated)}${row.code?' · '+row.code:''}`));return el;
}
function motivationPanel(value,open){
 const details=node('details',undefined,'monitor-card');details.dataset.motivation='true';details.open=open;
 details.append(node('summary','内部倾向（模拟状态）'));
 details.append(node('p','这些倾向供她选择下一步；她也可以休息或改变主意。','monitor-muted'));
 if(!value?.drives){details.append(node('p','内部倾向数据尚未同步。'));return details}
 const table=node('table'),head=node('tr');for(const title of ['倾向','压力','阶段'])head.append(node('th',title));
 const thead=node('thead');thead.append(head);table.append(thead);const body=node('tbody');
 for(const [key,label] of Object.entries(driveNames)){
  const pair=value.drives[key],pressure=Array.isArray(pair)&&typeof pair[0]==='number'&&Number.isFinite(pair[0])?`${(pair[0]*100).toFixed(0)}%`:'未报告';
  const tr=node('tr');for(const text of [label,pressure,drivePhases[pair?.[1]]||pair?.[1]||'未报告'])tr.append(node('td',text));body.append(tr);
 }
 table.append(body);const scroll=node('div',undefined,'monitor-table monitor-drives');scroll.append(table);details.append(scroll);
 details.append(node('h3','最近反馈'));
 const feedback=Array.isArray(value.recent_feedback)?value.recent_feedback.slice(0,2):[];
 for(const item of feedback){
  const basis=item.kind==='verified_task_satisfaction'?'依据：真实执行回执':item.kind==='interaction_satisfaction'?'依据：实际交流':'依据未报告';
  const delta=typeof item.delta==='number'&&Number.isFinite(item.delta)?` · 变化 ${item.delta>=0?'+':''}${(item.delta*100).toFixed(0)} 个百分点`:'';
  const entry=node('div',undefined,'monitor-result');entry.append(node('strong',`${driveNames[item.dimension]||item.dimension||'倾向'}${delta}`),node('small',basis),node('p','她的评价：'+(item.reason||'未记录理由')));details.append(entry);
 }
 if(!feedback.length)details.append(node('p','暂无反馈记录。'));
 details.append(node('p','回执或交流验证的是事件确实发生；反馈理由是她的主观评价，不代表项目已经完成。','monitor-muted'));
 return details;
}
export function renderMonitor(root,value){
 if(!value){root.replaceChildren(node('p','状态数据尚未同步，请等待连接恢复。'));return}
 const now=card('现在');now.append(node('p',phaseNames[value.phase?.state]||'状态未报告','monitor-phase'));
 now.append(node('p',value.waiting?.reason||''));
 for(const action of value.current_actions||[])now.append(node('p',`${action.kind} · ${status(action.status)}`));
 now.append(node('small',`下一次自主唤醒：${time(value.waiting?.next_wake)}（北京时间）`));
 if(value.waiting?.next_step)now.append(node('p','项目记录的下一步：'+value.waiting.next_step));
 const usage=card(`今天的用量 · ${value.today?.date||''}`);
 usage.append(node('p','北京时间；已知费用是按既有价目计算的小计，未知部分不当作免费。','monitor-muted'));
 const table=node('table'),head=node('tr');
 for(const label of ['用途','调用','输入','缓存命中','输出','已知费用 ¥','未知费用调用'])head.append(node('th',label));
 const thead=node('thead');thead.append(head);table.append(thead);const body=node('tbody');
 const groups=[...Object.entries(value.today?.groups||{}),['total',value.today?.total||{}]];
 for(const [key,row] of groups){const tr=node('tr');
  const number=v=>v===null||v===undefined?'未知':v.toLocaleString('zh-CN');
  for(const item of [key==='total'?'合计':groupNames[key]||key,number(row.calls),number(row.input_tokens),number(row.cached_input_tokens),number(row.output_tokens),typeof row.known_cny==='number'?row.known_cny.toFixed(6):'未知',number(row.unknown_calls)])tr.append(node('td',item));body.append(tr)}
 table.append(body);const scroll=node('div',undefined,'monitor-table');scroll.append(table);usage.append(scroll);
 const projects=card('项目与实际成果');
 for(const project of value.projects||[]){const item=node('div',undefined,'monitor-project');item.append(node('h3',project.goal||project.id),node('small',`${project.origin==='self'?'自己的兴趣':project.origin==='user'?'用户交办':'来源未记录'} · 项目状态：${status(project.status)}`));
  if(project.checkpoint)item.append(node('p','进展记录（她的判断）：'+project.checkpoint));
  if(project.blocked_on?.length)item.append(node('p','记录的阻碍：'+project.blocked_on.map(v=>blockerNames[v]||v).join('、')));
  if(project.last_progress_at)item.append(node('small','最近进展：'+time(project.last_progress_at)));
  if(project.next_step)item.append(node('p','计划：'+project.next_step));
  item.append(project.latest_result?receipt(project.latest_result):node('p','尚无实际执行回执；计划和项目状态不代表已经完成。','monitor-muted'));projects.append(item)}
 if(!value.projects?.length)projects.append(node('p','当前没有项目记录。'));
 const results=card('近期执行回执');for(const result of value.recent_results||[])results.append(receipt(result));
 if(!value.recent_results?.length)results.append(node('p','暂无执行回执。'));
 const errors=card('近期错误');for(const error of value.errors||[])errors.append(node('p',`${time(error.created)} · ${error.summary}`));
 if(!value.errors?.length)errors.append(node('p','没有已记录的近期错误。'));
 const motivation=motivationPanel(value.motivation,root.querySelector('details[data-motivation]')?.open||false);
 root.replaceChildren(now,usage,motivation,projects,results,errors,node('p',`更新于 ${time(value.generated_at)} · 此页只读取状态，不调用模型。`,'monitor-muted'));
}
