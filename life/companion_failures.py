"""Persist technical failure feedback without replaying a rejected decision."""
import time
import traceback
from . import companion_delivery as ledger

NOTICE='【服务提示】这轮回复处理失败，没有正常发出。你的消息已保存，可以继续聊天。'

def record_failure(core, source, purpose, error):
    cause=error
    while cause.__cause__ is not None:cause=cause.__cause__
    frames=traceback.extract_tb(cause.__traceback__)
    core._audit('error','认知失败，已保留输入并停止立即重试。',source=source.id,purpose=purpose,
                error=type(cause).__name__,location=[dict(function=f.name,line=f.lineno) for f in frames[-4:]])
    with core.repo.tx():
        failures=min(5,int(core._state('cognition_failures',0))+1)
        core._save('cognition_failures',failures)
        core._save('next_wake',time.time()+min(1800,60*2**failures))
        if purpose!='dialogue' or source.id!=core._latest_dialogue:return
        # 单次内部修正失败只进运行状态；连续失败、确实无法回复时才提示一次。
        if failures<3:return
        if core._state('failure_notified:'+source.id,False):return
        # A commit may have succeeded before its final bookkeeping failed.
        if core.repo._one("SELECT 1 FROM events WHERE kind='message' AND json_extract(payload,'$.turn_id')=?",(source.id,)):return
        delivery=core._message_route(source)['reply_delivery']
        mid=ledger.message_id(source.id,0)
        ledger.queue(core.repo,mid=mid,agent_id=core.life.agent_id,turn_id=source.id,segment=0,
                     text=NOTICE,images=[],conversation_ref='',channels=delivery['targets'])
        message=core._store_bubble(dict(text=NOTICE,images=[],turn_id=source.id,part=0,parts=1,delivery=delivery))
        ledger.mark_sending(core.repo,mid)
        ledger.mark_sent(core.repo,mid,event_ref=message['id'])
        core._save('failure_notified:'+source.id,True)
    core._emit('message',**message)
