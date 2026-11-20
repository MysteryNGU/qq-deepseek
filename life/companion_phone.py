"""Actual Android accessibility operations routed to one authenticated live phone."""
import asyncio
import base64
import io
import json
import math
from pathlib import Path
import re
import time
import uuid
from aiohttp import web
from PIL import Image
from .companion_media import _validate,MAX_BYTES
# 来源分级与「这次没取得什么」的措辞只有一份词表（companion_work）；回执级的
# provenance/visibility 组装也只有一处（companion_executor.annotate_receipt）。
# 这里只用到常量和 missing/visibility 两个构造器，不再自己组装回执来源。
from .companion_work import PROVENANCE_DIRECT,PROVENANCE_TOOL,missing,visibility

OPS={'info','screenshot','tap','swipe','text','back','home','recents','open_app','apps'}
WORKSPACE_OPS={'workspace_open','workspace_status','workspace_pause','workspace_resume','workspace_close'}
# Sent with every phone action; the phone decides whether it can honour them.
TARGET_FIELDS={'target','workspace_id','session_id','display_id'}
# 调用信封里已有的字段：重复写进 arguments 是自然写法，不是「多出来的数据」。
# op 由 action.op 解析、device/device_id 用来选手机——以信封为准，冲突时如实说明用了哪个。
ENVELOPE_FIELDS={'op','device'}
# Registration is a short request and SSE is a long one. A phone that is locked or
# has the app in the background keeps registering but loses its stream, so "recently
# registered" is the online signal; a live stream still counts on its own.
RECENT_SECONDS=90
# Memory-only records: a phone named here is a test device and may be forgotten as
# soon as it stops registering. Devices are otherwise kept for a day so a phone that
# comes back does not lose its identity.
TEST_DEVICE_NAMES=frozenset({'verification'})
FORGET_TEST_SECONDS=3600
FORGET_SECONDS=86400
# Larger values are clock skew, not a registration that happened in the future.
MAX_CLOCK_SKEW=60
# 回执里两件不同的事必须分开标：手机回了话是 tool_report，屏幕本身是 direct_observation。
SCREENSHOT_OPS=frozenset({'screenshot'})
TREE_OPS=frozenset({'info'})
# 手机只保留前 180 个界面节点；超过就是截断，不是「整棵树都看到了」。
TREE_NODE_LIMIT=180

class PhoneError(ValueError):
    """A failure that names which layer refused and whether retrying could help.

    She decides what to do next; this only states facts about the world:
    ``cloud`` = the request never left the host, ``app`` = the phone link,
    ``workspace`` = the off-screen display.
    """
    def __init__(self,message,*,layer='cloud',retryable=False,code='phone_unavailable'):
        super().__init__(message);self.layer=layer;self.retryable=retryable;self.code=code


class PhoneControl:
    def __init__(self,mobile):
        self.mobile=mobile;self.devices={};self.pending={}
        mobile.server.app.router.add_post('/mobile/device',self.register)
        mobile.server.app.router.add_post('/mobile/control-result',self.receipt)

    @staticmethod
    def world_hint(row):
        """Current workspace facts for one device, so an error can say what exists.

        This does not pick anything for her: it only reports what the phone last
        registered, including the ids the operations would need.
        """
        if not isinstance(row,dict):
            return ''
        workspaces=[item for item in (row.get('workspaces') or []) if isinstance(item,dict)]
        state=row.get('workspace_state') or 'unknown'
        if not workspaces:
            hint='工作区状态 %s；这台手机当前没有登记中的工作区' % state
        else:
            parts=[]
            for item in workspaces[:4]:
                size=''
                if item.get('width') and item.get('height'):
                    size='%s×%s' % (item.get('width'),item.get('height'))
                session=str(item.get('session_id') or '')
                parts.append('%s%s%s%s' % (item.get('workspace_id') or '?',
                    ('（%s）' % item.get('package')) if item.get('package') else '',
                    ('，%s' % size) if size else '',
                    ('，session_id=%s' % session) if session else ''))
            hint='工作区状态 %s；当前有：%s' % (state,'；'.join(parts))
        error=str(row.get('workspace_error') or '')
        return hint + ('；最近一次工作区错误：%s' % error if error else '')


    @staticmethod
    def _plain(value,limit=160):
        if value is None:return ''
        return ' '.join(str(value).split())[:limit]

    @staticmethod
    def _failed(result,output,code):
        """失败回执保留「对什么失败」：丢掉 package/activity 等于把对象也一起丢了。"""
        kept={key:result[key] for key in ('target','workspace_id','session_id','display_id',
                                          'package','activity','task_id') if key in result}
        return dict(kept,status='failed',output=output,code=code)

    @staticmethod
    def _gap_layer(code,*,default='failed'):
        """手机回执里的码 → t11 那六层原因，不另造词。"""
        code=str(code or '')
        if code in {'workspace_unavailable','phone_offline','workspace_changed'}:return 'unavailable'
        if code in {'workspace_unsupported','phone_unsupported'}:return 'unsupported'
        if code in {'workspace_frame_too_large','phone_too_large'}:return 'limit'
        return default

    def _ui_tree(self,call,value,result):
        """界面树这一次到底读没读到——和截图分开记账，各自带来源。"""
        op=str(call.get('op') or '')
        code=str(result.get('code') or '')
        if op not in TREE_OPS:
            gap=missing('界面树','not_requested','%s 不读界面树' % (op or '这个操作'),code=code)
        elif result.get('status')!='completed':
            gap=missing('界面树',self._gap_layer(code),
                        '操作没有完成（%s）' % (code or '没有回执码'),code=code)
        else:
            raw=value.get('output')
            try:tree=json.loads(raw) if isinstance(raw,str) else raw
            except ValueError:tree=None
            if not isinstance(tree,dict) or not isinstance(tree.get('nodes'),list):
                gap=missing('界面树','failed','手机回了话，但回执里没有可解析的界面节点清单',code=code)
            else:
                fact=dict(obtained=True,provenance=PROVENANCE_DIRECT,nodes=len(tree['nodes']),
                          truncated=bool(tree.get('truncated')),
                          package=self._plain(tree.get('package')),
                          width=tree.get('width'),height=tree.get('height'),
                          coordinate_space=self._plain(tree.get('coordinate_space')))
                if fact['truncated']:
                    fact['limit']='手机只保留前 %d 个界面节点，这不是整棵树' % TREE_NODE_LIMIT
                return fact
        return dict(obtained=False,provenance=PROVENANCE_DIRECT,gap=gap)

    def _screenshot(self,call,value,result):
        """截图这一次到底拿没拿到：有就给出真实落盘路径，没有就给出原因。"""
        op=str(call.get('op') or '')
        code=str(result.get('code') or '')
        if result.get('image_path'):
            frame=result.get('coordinate_frame') or {}
            return dict(obtained=True,provenance=PROVENANCE_DIRECT,path=str(result['image_path']),
                        mime=result.get('image_mime'),bytes=result.get('bytes'),
                        width=frame.get('width'),height=frame.get('height'),unit=frame.get('unit'))
        if op not in SCREENSHOT_OPS:
            gap=missing('截图','not_requested','%s 不取画面' % (op or '这个操作'),code=code)
        elif code=='phone_missing_image' or result.get('status')=='completed':
            gap=missing('截图','failed','手机回了完成，但回传里没有画面',code=code)
        else:
            gap=missing('截图',self._gap_layer(code),
                        '手机报的码：%s' % (code or '操作没有完成'),code=code)
        return dict(obtained=False,provenance=PROVENANCE_DIRECT,gap=gap)

    def _annotate(self,call,value,result):
        """把「请求回来了」和「看到了屏幕」写成两件分开的事实。

        2026-11-20 21:33–21:38 她 workspace_open 拿到的回执里有包名和 activity，
        却不知道屏幕长什么样。包名和 activity 只证明启动请求被手机接受；
        屏幕现在是相册首页还是别的画面，只有截图/界面树能回答。
        所以这里不预写「进入相册成功」，也不规定她下一步必须做什么。
        """
        op=str(call.get('op') or '')
        target=str(result.get('target') or call.get('target')
                   or ('workspace' if op in WORKSPACE_OPS else 'main'))
        package=self._plain(value.get('package') or result.get('package'))
        activity=self._plain(value.get('activity') or result.get('activity'))
        tree=self._ui_tree(call,value,result)
        shot=self._screenshot(call,value,result)
        gaps=[item['gap'] for item in (shot,tree) if isinstance(item.get('gap'),dict)]
        truncated=[]
        if tree.get('obtained') and tree.get('truncated'):
            truncated.append(dict(what='界面节点清单',detail=str(tree.get('limit') or '清单被截断')))
        lines=['手机回执（'+PROVENANCE_TOOL+'）：op=%s，target=%s，device=%s，status=%s'
               % (op,target,str(call.get('device') or '未记录'),result.get('status'))]
        workspace_id=self._plain(result.get('workspace_id') or value.get('workspace_id'),80)
        if workspace_id:lines[0]+='，workspace_id='+workspace_id
        if package or activity:
            parts=[]
            if package:parts.append('package='+package)
            if activity:parts.append('activity='+activity)
            lines.append('请求已返回：'+'；'.join(parts))
        elif result.get('status')=='completed':
            lines.append('请求已返回，但手机没有回传 package/activity。')
        else:
            lines.append('请求没有完成，手机没有回传 package/activity。')
        notes=[str(item) for item in (call.get('notes') or []) if str(item).strip()]
        if notes:
            # 信封与 arguments 不一致时，用了哪个必须写清楚，不能让她以为按自己写的执行了。
            lines.append('参数说明：'+'；'.join(notes))
        original=str(result.get('output') or '').strip()
        if original:
            # 原文另存一份：下面的回执是给她读的摘要，不能把手机自己的话丢掉。
            result['phone_output']=original[:4000]
            lines.append('手机自己回的说明：'+original[:1200])
            if len(original)>1200:
                truncated.append(dict(what='手机自己回的说明',
                                      detail='原文 %d 字，回执里只放了前 1200 字' % len(original)))
        if tree.get('obtained'):
            detail='已取得 %d 个界面节点' % tree['nodes']
            if tree.get('truncated'):detail+='（清单被截断，不是整棵树）'
            if tree.get('package'):detail+='；前台包名 '+tree['package']
            lines.append('界面树：'+detail+'（来源 '+PROVENANCE_DIRECT+'）')
        else:
            gap=tree.get('gap') or {}
            lines.append('界面树：这次没有取得界面树（%s：%s）'
                         % (str(gap.get('reason') or 'failed'),str(gap.get('detail') or '没有读到')))
        if shot.get('obtained'):
            size=''
            if shot.get('width') and shot.get('height'):
                size='，%s×%s' % (shot['width'],shot['height'])
            unit=('，'+str(shot['unit'])) if shot.get('unit') else ''
            lines.append('截图：已取得 %s%s%s（来源 %s）'
                         % (shot['path'],size,unit,PROVENANCE_DIRECT))
        else:
            gap=shot.get('gap') or {}
            lines.append('截图：这次没有取得截图（%s：%s）'
                         % (str(gap.get('reason') or 'failed'),str(gap.get('detail') or '没有取到画面')))
        lines.append('说明：请求已返回只说明手机回了话；屏幕现在是什么样，只有上面标了 '
                     +PROVENANCE_DIRECT+' 的界面树或截图才是事实。')
        result['observation']=dict(provenance=PROVENANCE_DIRECT,op=op,target=target,package=package,
                                   activity=activity,obtained=bool(shot.get('obtained') or tree.get('obtained')),
                                   screenshot=shot,ui_tree=tree)
        if truncated:
            # 只交出「哪里被截断」；not_obtained 由统一出口从 observation 的 gap 汇总，
            # visibility 的其余字段（含 summary）也由它重建——这里不另写一份。
            result['visibility']=visibility(truncated=truncated)
        result['output']='\n'.join(lines)[:2400]
        return result

    def _last_seen(self,value,now):
        seen=value.get('last_seen',0)
        if not isinstance(seen,(int,float)) or isinstance(seen,bool):return 0.0
        return min(float(seen),now+MAX_CLOCK_SKEW)

    def online_devices(self,now=None):
        """Devices that count as present: a live stream now, or a registration within RECENT_SECONDS.

        A live stream implies a recent registration, so the registered time is the single
        decision and an absent or invalid timestamp never makes a device look online.
        """
        now=time.time() if now is None else now
        return {device for device,value in self.devices.items()
                if now-self._last_seen(value,now)<=RECENT_SECONDS}

    def prune(self,now=None):
        """Drop records that stopped registering; returns the ids that were forgotten."""
        now=time.time() if now is None else now
        gone=[]
        for device,value in list(self.devices.items()):
            limit=FORGET_TEST_SECONDS if value.get('test') else FORGET_SECONDS
            if now-self._last_seen(value,now)>limit:
                self.devices.pop(device,None);gone.append(device)
        return gone

    def context(self):
        """What the phone (and the role) sees as usable devices.

        Online is the registration window, not a live stream: a locked phone keeps
        registering and must stay visible. Dispatching is narrower — run() needs a
        live queue — so widening this never sends an action nobody can receive.
        """
        online=self.online_devices()
        rows=[]
        for device,value in self.devices.items():
            if device not in online or not value.get('enabled') or not value['operations']:
                continue
            rows.append(dict(device_id=device,enabled=True,operations=value['operations'],
                workspace_state=value.get('workspace_state','unknown'),
                workspace_error=value.get('workspace_error',''),
                workspaces=value.get('workspaces') or [],
                has_stream=any(device==queued for queued in self.mobile.subscribers.values())))
        return dict(connected=bool(rows),devices=rows,coordinate_unit='physical_screen_pixels',
                    targets=['main','workspace'],
                    note='target=workspace 时坐标与画面都属于该工作区副屏，不是主人主屏。')

    async def register(self,request):
        try:
            value=(await request.json()).get('capabilities',{}).get('phone',{})
            operations=value.get('operations',[])
            if not isinstance(operations,list) or not all(isinstance(op,str) for op in operations):raise ValueError()
            workspaces=value.get('workspaces') or []
            if not isinstance(workspaces,list):raise ValueError()
            device=request['mobile_device'];now=time.time()
            old=self.devices.get(device) or {}
            self.devices[device]=dict(enabled=value.get('enabled') is True,
                workspace_state=str(value.get('workspace_state') or 'unknown')[:60],
                workspace_error=str(value.get('workspace_error') or '')[:400],
                operations=sorted(set(operations)&(OPS|WORKSPACE_OPS)),
                workspaces=[item for item in workspaces if isinstance(item,dict)][:8],
                name=old.get('name') or self.mobile.device_name(device) or '手机',
                test=bool(old.get('test')) or (self.mobile.device_name(device) or '') in TEST_DEVICE_NAMES,
                last_seen=now)
        except (ValueError,AttributeError):raise web.HTTPBadRequest(text='Invalid phone capability')
        if not self.devices[request['mobile_device']]['enabled']:
            self.fail_device(request['mobile_device'],'手机无障碍权限已关闭')
        self.prune(now)
        return web.json_response(dict(ok=True,phone=self.context()))

    def fail_device(self,device,text,queue=None):
        for call in self.pending.values():
            if call['device']==device and (queue is None or call['queue'] is queue) and not call['future'].done():
                call['future'].set_result(dict(status='failed',output=text,code='phone_offline',
                                                layer='app',retryable=True))

    def disconnected(self,queue,device):
        """Losing a stream is not losing the device: keep the record and let it expire."""
        self.fail_device(device,'手机操作连接已断开，未重放操作',queue)
        self.prune()

    async def run(self,action):
        """跑一个手机动作，把「请求回来了」和「看到了屏幕」各自的事实交出去。

        来源分级与「这次没取得什么」不在这里组装：回执唯一的组装点是
        ``life/companion_executor.annotate_receipt``（生产路径
        RemoteExecutor.run → run_with_receipt → annotate_receipt）。手机只负责
        两件它自己才知道的事：observation 里每一项取没取到（含具体原因），
        以及这次哪些内容被截断。
        """
        try:
            args=json.loads(action.get('arguments','{}'))
            if not isinstance(args,dict):raise ValueError('手机操作无效')
            # 信封字段重复出现在 arguments 里不算多余参数：按信封用，冲突时如实写明用了哪个。
            # 2026-11-21 01:25–01:27 她连续五次因为 arguments 里带了 op/device 被判「多 op/多 device」
            # 直接拒绝——那不是她的错，是校验把实现细节当成了非法数据。
            notes=[]
            args_op=args.pop('op',None)
            op=action.get('op')
            if not str(op or '').strip() and args_op is not None:
                op=args_op                      # 信封没有 op 时才用 arguments 里的
            if op not in OPS|WORKSPACE_OPS:raise ValueError('手机操作无效')
            if args_op is not None and str(args_op).strip()!=str(op).strip():
                notes.append('传了 op=%s，按 %s 执行' % (self._plain(args_op,40),op))
            envelope_device=self._plain(action.get('device') or action.get('device_id'),80) or None
            args_device=self._plain(args.pop('device',None),80) or None
            args_device_id=self._plain(args.pop('device_id',None),80) or None
            if envelope_device and args_device and args_device!=envelope_device:
                notes.append('传了 device=%s，实际用的是 %s' % (args_device,envelope_device))
            if envelope_device and args_device_id and args_device_id!=envelope_device:
                notes.append('传了 device_id=%s，实际用的是 %s' % (args_device_id,envelope_device))
            requested=envelope_device or args_device or args_device_id
            envelope_target=self._plain(action.get('target'),20) or None
            args_target=self._plain(args.get('target'),20) or None
            if envelope_target and args_target and args_target!=envelope_target:
                notes.append('传了 target=%s，按 %s 执行' % (args_target,envelope_target))
            target=envelope_target or args_target or ('workspace' if op in WORKSPACE_OPS else 'main')
            if target not in {'main','workspace'}:raise ValueError('target 只能是 main 或 workspace')
            if op in WORKSPACE_OPS and target!='workspace':raise ValueError('工作区管理操作只能使用副屏')
            if envelope_target or args_target:args['target']=target
            candidates=[device for device in self.context()['devices'] if requested is None or device['device_id']==requested]
            if len(candidates)!=1:
                available=[item['device_id'] for item in self.context()['devices']]
                detail=('指定的 %s 不在线或没有无障碍；' % requested) if requested else ''
                raise PhoneError(detail+'需要一台在线且已启用无障碍的手机；多台时指定device_id。当前可用：'
                                 +(('、'.join(available)) or '无'),layer='app',retryable=True)
            row=candidates[0];device=row['device_id']
            if op not in row['operations']:
                raise PhoneError('手机没有提供该操作能力；它声明的操作：'+('、'.join(row['operations']) or '无'),
                                 layer='app',retryable=False)
            if target=='workspace' or op in WORKSPACE_OPS:
                if op!='workspace_open' and not str(args.get('workspace_id') or '').strip():
                    raise PhoneError('工作区操作需要 workspace_id；'+self.world_hint(row),layer='cloud',retryable=False)
                if target=='workspace' and op in OPS and not str(args.get('session_id') or '').strip():
                    raise PhoneError('工作区内的手机操作需要 session_id；'+self.world_hint(row),layer='cloud',retryable=False)
            fields={'tap':{'x','y'},'swipe':{'x1','y1','x2','y2','duration_ms'},'text':{'text'},
                    'open_app':{'package'},'workspace_open':{'package'}}.get(op,set())
            required=fields-{'duration_ms'}
            allowed=fields|TARGET_FIELDS|ENVELOPE_FIELDS
            if set(args)-allowed or not required.issubset(args):
                # 只说「参数不匹配」等于什么都没说：2026-11-20 她 workspace_open 传了 {}，
                # 回执只有一句参数不匹配，只能再去翻一遍能力说明。这里直接写缺什么。
                absent=sorted(required-set(args))
                extra=sorted(set(args)-allowed)
                parts=[]
                if absent:parts.append('缺 '+'、'.join(absent))
                if extra:parts.append('多 '+'、'.join(extra))
                needed=('、'.join(sorted(required)) or '无')
                raise ValueError('手机操作参数不匹配（'+str(op)+'）：'+'；'.join(parts)
                                 +'。这个操作需要：'+needed)
            for key in fields&{'x','y','x1','y1','x2','y2'}:
                if type(args[key]) not in (int,float) or not math.isfinite(args[key]) or args[key]<0:raise ValueError('坐标须为屏幕物理像素')
            if op=='text' and (not isinstance(args['text'],str) or len(args['text'])>20000):raise ValueError('文本无效')
            if op in {'open_app','workspace_open'} and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+',str(args['package'])):raise ValueError('应用包名无效')
            if 'duration_ms' in args and (type(args['duration_ms']) is not int or not 1<=args['duration_ms']<=10000):raise ValueError('滑动时长无效')
            # A recent registration keeps the device visible, but an action still needs a live
            # stream to travel on: dispatch must not succeed against a phone nobody is reading.
            queue=next((q for q,d in reversed(list(self.mobile.subscribers.items())) if d==device),None)
            if queue is None:
                raise PhoneError('手机当前没有活动连接；先让它保持在前台，再重试',layer='app',retryable=True)
        except PhoneError as error:
            return dict(status='failed',output=str(error),code=error.code,layer=error.layer,retryable=error.retryable)
        except (ValueError,TypeError) as error:
            return dict(status='failed',output=str(error),code='phone_unavailable',layer='cloud',retryable=False)
        if queue.full():
            return dict(status='failed',output='手机连接繁忙，操作未发送',code='phone_busy',layer='app',retryable=True)
        identity=uuid.uuid4().hex;future=asyncio.get_running_loop().create_future()
        self.pending[identity]=dict(device=device,queue=queue,future=future,op=op,target=target,
                                    notes=list(notes))
        queue.put_nowait(dict(type='phone_control',id=identity,op=op,args=args,
                              target=target,workspace_id=args.get('workspace_id'),session_id=args.get('session_id')))
        try:
            result=await asyncio.wait_for(future,45)
            result['device_id']=device
            result['target']=target
            if args.get('workspace_id'):result['workspace_id']=args['workspace_id']
            if args.get('session_id'):result['session_id']=args['session_id']
            return result
        except asyncio.TimeoutError:
            if queue in self.mobile.subscribers and not queue.full():queue.put_nowait(dict(type='phone_cancel',id=identity))
            return dict(status='failed',output='手机操作未在45秒内返回；不知道是否已经执行，先看屏幕再决定要不要重试',
                        code='phone_timeout',layer='app',retryable=True)
        except asyncio.CancelledError:
            if queue in self.mobile.subscribers and not queue.full():queue.put_nowait(dict(type='phone_cancel',id=identity))
            raise
        finally:self.pending.pop(identity,None)

    async def receipt(self,request):
        try:
            data=await request.json();identity=data.get('id');value=data.get('result')
            if not isinstance(identity,str) or not isinstance(value,dict):raise ValueError()
        except (ValueError,AttributeError):raise web.HTTPBadRequest(text='Invalid phone result')
        call=self.pending.get(identity)
        if call is None:return web.json_response(dict(ok=True,ignored=True))
        if call['device']!=request['mobile_device']:raise web.HTTPForbidden()
        if call['future'].done():return web.json_response(dict(ok=True,ignored=True))
        status=value.get('status');result=dict(status=status if status in {'completed','failed','interrupted'} else 'failed',
            output=str(value.get('output',''))[:16000])
        for key in ('target','workspace_id','session_id','display_id','frame_id','package','activity','task_id'):
            if value.get(key) is not None:result[key]=value[key]
        if value.get('code'):result['code']=str(value['code'])[:60]
        if value.get('image_png') and result['status']=='completed':
            try:
                encoded=value['image_png']
                if call['op']!='screenshot' or not isinstance(encoded,str) or len(encoded)>MAX_BYTES*4//3+4:raise ValueError()
                # 手机端超过 900 KiB 时会自动改用 JPEG（同像素尺寸，坐标不变）；
                # 老版本不带 image_mime，按 PNG 处理，保持向后兼容。
                mime=str(value.get('image_mime') or 'image/png')
                if mime not in ('image/png','image/jpeg'):mime='image/png'
                raw=base64.b64decode(encoded,validate=True);_validate(raw,mime)
                with Image.open(io.BytesIO(raw)) as image:width,height=image.size
                folder=self.mobile.home/'data/companion/artifacts';folder.mkdir(parents=True,exist_ok=True)
                path=folder/(identity+'.phone.'+('jpg' if mime=='image/jpeg' else 'png'));path.write_bytes(raw)
                unit='workspace_display_pixels' if call.get('target')=='workspace' else 'physical_screen_pixels'
                result.update(image_path=str(path),image_mime=mime,bytes=len(raw),
                              coordinate_frame=dict(width=width,height=height,unit=unit))
                if mime=='image/jpeg':
                    result['output']=(result['output']+'\n画面按 JPEG 压缩传回（%d KiB，像素尺寸未变）。' % max(1,len(raw)//1024)).strip()
                if call.get('target')=='workspace':
                    # 事实，不是下一步：这一次截图不会关掉工作区。
                    result['output']=(result['output']+'\n副屏截图已取得；截图操作不会关闭工作区。').strip()
            except (ValueError,TypeError,OSError):
                result=self._failed(result,'手机截图数据无效','phone_invalid_image')
        elif call['op']=='screenshot' and result['status']=='completed':
            result=self._failed(result,'手机回了完成，但没有回传实际画面','phone_missing_image')
        # Every failure names the layer and whether a retry could help; she decides.
        code=str(result.get('code') or '')
        workspace=call.get('target')=='workspace' or call.get('op') in WORKSPACE_OPS or code.startswith('workspace')
        result.setdefault('layer','workspace' if workspace else 'app')
        result.setdefault('retryable',code in {'workspace_changed','workspace_unavailable','phone_offline','phone_timeout'})
        # 最后一步：把「请求回来了」和「看见屏幕」分开写进回执（含 package/activity、截图路径、界面树）。
        result=self._annotate(call,value,result)
        call['future'].set_result(result)
        return web.json_response(dict(ok=True))
