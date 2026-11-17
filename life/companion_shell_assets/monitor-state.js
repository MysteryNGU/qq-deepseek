/** Immediate real phase events supersede the last polled monitoring snapshot. */
export function applyMonitorPhase(snapshot,phase){
 snapshot.phase=phase;
 if(!snapshot.observability)return;
 const view=snapshot.observability;
 view.phase=phase;
 // A phase names executor activity, but cannot identify an action from an older snapshot.
 view.current_actions=[];
 const reasons={model:'模型请求进行中',tools:'执行器正在处理行动',idle:'空闲；当前没有模型请求或执行器任务'};
 view.waiting={...view.waiting,reason:reasons[phase.state]||'等待状态同步'};
}
