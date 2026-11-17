"""Async adapter for Coopanion's unchanged native computer-use engine."""
import asyncio
import base64
import json
import math
import os
from pathlib import Path
import time
import uuid

from .supervisor import ProcessTree

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT/'data/companion-runtime'
REQUIRED = dict(info=(), windows=(), screenshot=(), release=(), zoom=('x','y','width','height'),
    click=('x','y'), move=('x','y'), drag=('x1','y1','x2','y2'), scroll=('x','y'),
    type=('text',), key=('keys',), hold=('keys','holdMs'), mouseDown=('x','y'), mouseUp=(), focus=('handle',))
FIELDS = dict(info=(), windows=(), release=(), screenshot=('display','maxWidth','maxHeight','quality'),
    zoom=('display','layout','x','y','width','height','maxWidth','maxHeight','quality'),
    click=('display','layout','x','y','button','count'), move=('display','layout','x','y'),
    drag=('display','layout','x1','y1','x2','y2'), scroll=('display','layout','x','y','down','right'),
    type=('text','chunkDelayMs'), key=('keys',), hold=('keys','holdMs'),
    mouseDown=('display','layout','x','y','button'), mouseUp=('button',), focus=('handle',))

METHOD = '''# Computer actions

Use `kind:"computer", op:"...", arguments:"{...}", project:"project key"`.
Each operation returns actual native feedback. Take a fresh screenshot when the view changes.
Coordinates for click/move/drag/scroll/mouseDown are pixels in the most recent screenshot/zoom;
the adapter applies its scale and crop origin. Set `physical:true` for physical display pixels.
The native engine rejects an outdated display layout. Do not guess from an absent screenshot.

| op | arguments |
| --- | --- |
| info | `{}`: display arrangement and foreground app |
| windows | `{}`: native window handles, titles and physical rectangles |
| screenshot | optional `display`, `maxWidth:1280`, `maxHeight:800`, `quality:75` |
| zoom | physical `display,layout,x,y,width,height`; optional maxWidth/maxHeight/quality |
| focus | `handle` from windows |
| click | `x,y`; optional `button:left/right/middle`, `count:1` |
| move | `x,y` |
| drag | `x1,y1,x2,y2` |
| scroll | `x,y,down,right`: signed wheel ticks |
| type | Unicode `text`; optional `chunkDelayMs:10` |
| key | `keys`, e.g. `ctrl+a`, `enter`, or `ctrl+a delete` |
| hold | one chord `keys` and `holdMs` |
| mouseDown / mouseUp | position plus optional button / button only |
| release | `{}` releases input still held by this engine |

Input waits for 500ms user idleness, yielding after 2000ms; override idleMs/maxWaitMs when needed.
yielded/interrupted/partial and typed/stoppedBy are real outcomes, not successful completion.
Stopping closes the child IPC channel and releases held input before reaping the owned tree.
Implementation: original Coopanion engine, Pal AI Lab, AGPL-3.0-or-later,
commit 8829fd4fd0916f2c65f824daa63e503eff042b8a. This backend uses pixels; it provides no UIA tree.
'''


def image_block(home, path):
    """Only a verified local computer artifact becomes a model image."""
    from PIL import Image
    home = Path(home).resolve();folder = (home/'data/companion/artifacts').resolve();path = Path(path).resolve()
    if not folder.is_relative_to(home) or not path.is_relative_to(folder) or not path.is_file():
        raise ValueError('Computer image must belong to this character artifact directory')
    if path.stat().st_size>32*1024*1024:raise ValueError('Computer image exceeds the image request limit')
    with Image.open(path) as image:
        mime = Image.MIME.get(image.format)
        if mime not in {'image/jpeg','image/png','image/gif','image/webp'}:raise ValueError('Unsupported computer image format')
        image.verify()
    data = base64.b64encode(path.read_bytes()).decode('ascii')
    return dict(type='image_url',image_url=dict(url=f'data:{mime};base64,{data}'))


class Computer:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self._lock = asyncio.Lock()
        self._tree = None
        self._stderr = None
        self._next = 0
        self._last = None
        if self.available():
            method = self.home/'skills/computer.md'
            if not method.exists():
                method.parent.mkdir(parents=True,exist_ok=True);method.write_text(METHOD,encoding='utf-8')

    @staticmethod
    def available():
        return all(p.is_file() for p in (RUNTIME/'node/node.exe',RUNTIME/'cua/engine-child.mjs',
            RUNTIME/'cua/keys.mjs',RUNTIME/'cua/node_modules/koffi/package.json'))

    async def _rpc(self, request):
        if self._tree is None:
            if not self.available():
                raise RuntimeError('Computer runtime unavailable; run tools/build-companion-computer.ps1')
            folder = self.home/'data/companion/artifacts';folder.mkdir(parents=True,exist_ok=True)
            self._stderr = (folder/'computer-engine.log').open('ab')
            try:
                self._tree = ProcessTree([str(RUNTIME/'node/node.exe'),str(Path(__file__).with_suffix('.mjs')),
                    str(RUNTIME/'cua')],cwd=str(ROOT),env=dict(os.environ),pipes=True,stderr_file=self._stderr)
            except BaseException:
                self._stderr.close();self._stderr=None
                raise
        proc = self._tree.process
        self._next += 1
        proc.stdin.write((json.dumps(dict(id=self._next,req=request),ensure_ascii=False)+'\n').encode('utf-8'))
        proc.stdin.flush()
        line = await asyncio.to_thread(proc.stdout.readline)
        if not line:
            await self._stop()
            raise RuntimeError('Computer engine exited; see computer-engine.log')
        reply = json.loads(line)
        if reply.get('id') != self._next:
            raise RuntimeError('Computer engine reply does not match the request')
        if not reply.get('ok'):
            raise ValueError('Computer operation failed: '+str(reply.get('code') or reply.get('error')))
        return reply.get('value')

    def _validate(self, op, args):
        if op not in REQUIRED or not isinstance(args,dict):
            raise ValueError('Unknown computer operation or arguments must be a JSON object')
        unknown = set(args)-set(FIELDS[op])-{'physical','idleMs','maxWaitMs'}
        missing = set(REQUIRED[op])-set(args)
        if unknown or missing:
            hint = ' Region capture uses op=zoom, not screenshot.' if op=='screenshot' and unknown else ''
            raise ValueError(f'{op}: unknown fields {sorted(unknown)}; missing fields {sorted(missing)}; '
                             f'allowed fields {list(FIELDS[op])}.'+hint)
        for key,value in args.items():
            if key in {'text','keys','layout','handle'}:
                if not isinstance(value,str):raise ValueError(key+' must be text')
            elif key=='button':
                if value not in {'left','right','middle'}:raise ValueError('Invalid mouse button')
            elif key=='physical':
                if type(value) is not bool:raise ValueError('physical must be boolean')
            elif type(value) is not int or not math.isfinite(value):
                raise ValueError(key+' must be an integer')
            elif key not in {'down','right'} and value<0:
                raise ValueError(key+' must not be negative')
        if any(args.get(k,1)<1 for k in ('width','height','maxWidth','maxHeight','count','holdMs','display')):
            raise ValueError('Computer sizes, display and count must be positive')

    async def run(self, action):
        op = action.get('op')
        try:args = json.loads(action.get('arguments') or '{}')
        except (TypeError,ValueError):raise ValueError('Computer arguments must be valid JSON') from None
        self._validate(op,args)
        async with self._lock:
            started = time.monotonic()
            request = dict(args,op=op)
            physical = request.pop('physical',False)
            idle = request.pop('idleMs',500);wait = request.pop('maxWaitMs',2000)
            if op in {'screenshot','zoom'}:
                for key,value in dict(maxWidth=1280,maxHeight=800,quality=75).items():request.setdefault(key,value)
            if op=='screenshot':request.setdefault('display',None)
            if op not in {'info','windows','screenshot','zoom','release'}:
                request['yield'] = dict(idleMs=idle,maxWaitMs=wait)
            if op in {'click','mouseDown','mouseUp'}:request.setdefault('button','left')
            if op=='click':request.setdefault('count',1)
            if op=='scroll':request.setdefault('down',0);request.setdefault('right',0)
            if op=='type':request.setdefault('chunkDelayMs',10)
            if op in {'click','move','drag','scroll','mouseDown','zoom'}:
                last = self._last or {}
                display = request.pop('display',last.get('display'))
                layout = request.pop('layout',last.get('layout'))
                if display is None or layout is None:raise ValueError('Take a screenshot first or provide display and layout')
                request['at'] = dict(display=display,layout=layout)
                if not physical and op!='zoom' and last:
                    if display!=last['display']:
                        raise ValueError('Image coordinates belong to another display; screenshot that display first or use physical:true')
                    for key in ('x','x1','x2','y','y1','y2'):
                        if key in request:
                            limit = last['width' if key.startswith('x') else 'height']
                            if not 0<=request[key]<limit:
                                raise ValueError(f'{key} must be within latest image pixels 0..{limit-1}; physical:true uses physical display coordinates')
                            origin = last.get('origin_x' if key.startswith('x') else 'origin_y',0)
                            request[key] = round(origin+request[key]/last['scale'])
            try:
                value = await self._rpc(request)
            except asyncio.CancelledError:
                await self._stop()
                raise
            folder = self.home/'data/companion/artifacts';folder.mkdir(parents=True,exist_ok=True)
            key = uuid.uuid4().hex
            result = dict(status='completed',computer=value,seconds=round(time.monotonic()-started,3))
            if isinstance(value,dict) and value.get('imageBase64'):
                value = dict(value);image = value.pop('imageBase64')
                path = folder/(key+'.jpg');path.write_bytes(base64.b64decode(image,validate=True))
                if op=='zoom':
                    value.update(request['at'],scale=value['width']/request['width'],origin_x=request['x'],origin_y=request['y'])
                result.update(image_path=str(path),image_mime='image/jpeg',computer=value)
                self._last = dict(value)
            if isinstance(value,dict):
                if value.get('yielded'):result['status']='yielded'
                elif value.get('cancelled'):result['status']='interrupted'
                elif value.get('stoppedBy'):result['status']='partial'
            frame = None
            if self._last:
                frame = {key:self._last.get(key) for key in ('width','height','display','layout','scale','origin_x','origin_y')}
            result['coordinate_frame'] = frame
            result['output'] = json.dumps(dict(operation=op,
                coordinates='x,y always use the latest image pixels, even with explicit display/layout. Only physical:true selects physical screen pixels; do not reuse the native physical cursor as image coordinates.',**result),ensure_ascii=False)
            log = folder/(key+'.computer.json');log.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            result['output_path'] = str(log)
            return result

    async def _stop(self):
        tree,self._tree = self._tree,None
        if tree is not None:
            # EOF disconnects the mature engine, which releases its held inputs.
            try:tree.process.stdin.close()
            except OSError:pass
            try:await asyncio.to_thread(tree.process.wait,2)
            except Exception:pass
            tree.stop()
            tree.process.stdout.close()
        if self._stderr is not None:self._stderr.close();self._stderr=None
        self._last = None

    async def close(self):
        await self._stop()
