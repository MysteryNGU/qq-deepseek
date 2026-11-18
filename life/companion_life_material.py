"""Slot budget for the life material one cognition step may see.

Caps are docs/companion-next-agent-handoff.md §3. The persona text, live
conditions, the current user input and necessary tool receipts stay outside
this budget on purpose; nothing here may truncate the persona.

Two rules matter more than the numbers:
- one event enters at most once, even when several slots match it;
- an empty slot stays empty. Material is never padded up to a cap.
"""
import json

from .text import estimate_tokens

SLOTS = {
    'history': 1600,      # 最近对话：相处上下文与未回答的问题
    'projects': 650,      # 目标、当前理解、真实进展、下一步与等待条件
    'today': 650,         # 今天的实际活动、转折、作品和未闭合事情
    'recall': 600,        # 当前兴趣、问题、人物与行动有关的旧材料
    'notes': 550,         # 人物/自我笔记
    'ambient': 200,       # 时间、真实连接、新事件、可用天气
    'recollection': 150,  # 一条有线索、可以忽略或留待分享的旧事
    'growth': 320,        # 她自己形成的兴趣、表达习惯、自我理解（带依据和范围）
    # 用户亲手点选的条目（APK 点一段经历继续聊）。它排在第一位，**永远不会被
    # 别的槽挤掉**：用户点的东西没被模型看见，等于这个手势没发生。
    'refs': 600,
}

# 工具模式（真正读代码、跑命令、改文件的那种回合）需要同时看见整条任务线：
# 目标、已读过的文件、失败与回执、自己的笔记。给 4400 会让她每一步都失忆，
# 来回重读同一段代码。这里放大到能装下完整工程上下文，但仍受请求上限约束。
TOOL_SLOTS = {
    'history': 30000,
    'projects': 18000,
    'today': 14000,
    'recall': 14000,
    'notes': 10000,
    'ambient': 2000,
    'recollection': 1000,
    'growth': 1600,
    'refs': 4000,
}

# Fill order decides who keeps a shared item when two slots match it.
# ``refs`` 第一位：用户手点的那几条必须先进，否则会被 history 的大预算挤出去
# （_keys 会把同 id 判成重复，先进的那份留下）。
ORDER = ('refs','history','projects','today','recall','notes','growth','ambient','recollection')
TOTAL_CAP = sum(SLOTS.values())            # 4720
TOOL_TOTAL_CAP = sum(TOOL_SLOTS.values())  # 77600

PROFILES = {'life': (SLOTS, TOTAL_CAP), 'tool': (TOOL_SLOTS, TOOL_TOTAL_CAP)}


def _keys(item):
    # Evidence links are not identity: distinct notes/projects can interpret
    # the same event. Only source-only recall previews borrow that identity.
    identities=[item.get(key) for key in ('id','ref','resource_ref','episode_id')]
    identities=[value for value in identities if isinstance(value,str) and value]
    if identities:
        yield from identities
        return
    value=item.get('source_ref')
    if isinstance(value,str) and value:
        yield value
        return
    for key in ('source_refs','evidence_refs'):
        for value in item.get(key) or []:
            if isinstance(value,str) and value:
                yield value


def cost(item):
    return estimate_tokens(json.dumps(item,ensure_ascii=False,separators=(',',':')))


def fit(items,budget):
    kept,size=[],0
    for item in items:
        price=cost(item)
        if price>budget or size+price>budget:
            continue
        kept.append(item)
        size+=price
    return kept,size


def _clip_history(item, budget):
    """把一条历史消息压到预算内：留头留尾，中间标出省略。

    聊天历史按"一轮接一轮"才有意义，整条跳过会在中间留洞：上一句问、
    下一句答的材料都在，偏偏中间那句前提没了。所以这里宁可截断也不跳过。
    """
    item=dict(item)
    item.pop('images',None); item.pop('image_refs',None)
    text=str(item.get('content') or '')
    while text and cost(item)>budget:
        keep=int(len(text)*0.75)
        if keep>=len(text) or keep<80:
            return None
        tail=int(keep*0.35); head=keep-tail
        text=text[:head]+'\n……（太长，中间省略）……\n'+text[-tail:]
        item['content']=text
    if cost(item)>budget:
        return None
    item['truncated']=True
    return item


def assemble(candidates, budget=None, profile='life'):
    """candidates: {slot: [items]} → (material, report)."""
    slots,total=PROFILES.get(profile) or PROFILES['life']
    ratio=1.0 if budget is None else max(0.0,min(1.0,float(budget)/total))
    seen=set()
    material={}
    report={}
    for slot in ORDER:
        kept,size,dropped=[],0,0
        cap=int(slots[slot]*ratio)
        for item in candidates.get(slot) or []:
            if not isinstance(item,dict):
                continue
            keys=set(_keys(item))
            if keys and keys & seen:
                continue
            price=cost(item)
            if slot=='history' and size+price>cap:
                remain=cap-size
                trimmed=_clip_history(item,remain) if remain>=140 else None
                if trimmed is None:
                    dropped+=1
                else:
                    kept.append(trimmed)
                    size+=cost(trimmed)
                    seen|=keys
                    dropped+=1
                # 更早的消息不再放：宁可这段短一点，也不要跳过一条留下断层。
                break
            if size+price>cap:
                dropped+=1
                continue
            kept.append(item)
            size+=price
            seen|=keys
        material[slot]=kept
        report[slot]=dict(used=size,cap=slots[slot],dropped=dropped)
    report['total_used']=sum(row['used'] for row in report.values() if isinstance(row,dict) and 'used' in row)
    report['total_cap']=total
    report['profile']=profile
    return material,report
