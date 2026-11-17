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

OPS={'info','screenshot','tap','swipe','text','back','home','recents','open_app'}
WORKSPACE_OPS={'workspace_open','workspace_status','workspace_pause','workspace_resume','workspace_close'}
# Sent with every phone action; the phone decides whether it can honour them.
TARGET_FIELDS={'target','workspace_id','session_id','display_id'}
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

class PhoneControl:
    def __init__(self,mobile):
        self.mobile=mobile;self.devices={};self.pending={}
        mobile.server.app.router.add_post('/mobile/device',self.register)
        mobile.server.app.router.add_post('/mobile/control-result',self.receipt)

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
                call['future'].set_result(dict(status='failed',output=text,code='phone_offline'))

    def disconnected(self,queue,device):
        """Losing a stream is not losing the device: keep the record and let it expire."""
        self.fail_device(device,'手机操作连接已断开，未重放操作',queue)
        self.prune()

    async def run(self,action):
        try:
            args=json.loads(action.get('arguments','{}'));op=action.get('op')
            if not isinstance(args,dict) or op not in OPS|WORKSPACE_OPS:raise ValueError('手机操作无效')
            target=str(args.get('target') or ('workspace' if op in WORKSPACE_OPS else 'main'))
            if target not in {'main','workspace'}:raise ValueError('target 只能是 main 或 workspace')
            if op in WORKSPACE_OPS and target!='workspace':raise ValueError('工作区管理操作只能使用副屏')
            if target=='workspace':args['target']='workspace'
            if target=='workspace' or op in WORKSPACE_OPS:
                if op!='workspace_open' and not str(args.get('workspace_id') or '').strip():
                    raise ValueError('工作区操作需要 workspace_id')
                if target=='workspace' and op in OPS and not str(args.get('session_id') or '').strip():
                    raise ValueError('工作区内的手机操作需要 session_id')
            requested=args.pop('device_id',None)
            candidates=[device for device in self.context()['devices'] if requested is None or device['device_id']==requested]
            if len(candidates)!=1:raise ValueError('需要一台在线且已启用无障碍的手机；多台时指定device_id')
            device=candidates[0]['device_id']
            if op not in candidates[0]['operations']:raise ValueError('手机没有提供该操作能力')
            fields={'tap':{'x','y'},'swipe':{'x1','y1','x2','y2','duration_ms'},'text':{'text'},
                    'open_app':{'package'},'workspace_open':{'package'}}.get(op,set())
            required=fields-{'duration_ms'}
            allowed=fields|TARGET_FIELDS
            if set(args)-allowed or not required.issubset(args):raise ValueError('手机操作参数不匹配')
            for key in fields&{'x','y','x1','y1','x2','y2'}:
                if type(args[key]) not in (int,float) or not math.isfinite(args[key]) or args[key]<0:raise ValueError('坐标须为屏幕物理像素')
            if op=='text' and (not isinstance(args['text'],str) or len(args['text'])>20000):raise ValueError('文本无效')
            if op in {'open_app','workspace_open'} and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+',str(args['package'])):raise ValueError('应用包名无效')
            if 'duration_ms' in args and (type(args['duration_ms']) is not int or not 1<=args['duration_ms']<=10000):raise ValueError('滑动时长无效')
            # A recent registration keeps the device visible, but an action still needs a live
            # stream to travel on: dispatch must not succeed against a phone nobody is reading.
            queue=next((q for q,d in reversed(list(self.mobile.subscribers.items())) if d==device),None)
            if queue is None:raise ValueError('手机当前没有活动连接；先让它保持在前台，再重试')
        except (ValueError,TypeError) as error:return dict(status='failed',output=str(error),code='phone_unavailable')
        if queue.full():return dict(status='failed',output='手机连接繁忙，操作未发送',code='phone_busy')
        identity=uuid.uuid4().hex;future=asyncio.get_running_loop().create_future()
        self.pending[identity]=dict(device=device,queue=queue,future=future,op=op,target=target)
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
            return dict(status='failed',output='手机操作未在45秒内返回，不自动重试',code='phone_timeout')
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
                raw=base64.b64decode(encoded,validate=True);_validate(raw,'image/png')
                with Image.open(io.BytesIO(raw)) as image:width,height=image.size
                folder=self.mobile.home/'data/companion/artifacts';folder.mkdir(parents=True,exist_ok=True)
                path=folder/(identity+'.phone.png');path.write_bytes(raw)
                unit='workspace_display_pixels' if call.get('target')=='workspace' else 'physical_screen_pixels'
                result.update(image_path=str(path),image_mime='image/png',coordinate_frame=dict(width=width,height=height,unit=unit))
                if call.get('target')=='workspace':
                    result['output']=(result['output']+'\n副屏截图已取得。截图操作不会关闭工作区；需要关闭时必须另行调用 workspace_close 并取得成功回执。').strip()
            except (ValueError,TypeError,OSError):result=dict(status='failed',output='手机截图数据无效',code='phone_invalid_image')
        elif call['op']=='screenshot' and result['status']=='completed':result=dict(status='failed',output='手机未返回实际截图',code='phone_missing_image')
        call['future'].set_result(result)
        return web.json_response(dict(ok=True))
