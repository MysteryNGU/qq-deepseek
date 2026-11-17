"""On-demand stdio MCP bridge to the installed Bilibili backend; no model or DSH."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from .companion_bilibili_login import BilibiliLogin, LOGIN_OPS
from .companion_bilibili_compat import prepare_backend

ROOT = Path(__file__).resolve().parents[1]


class Bilibili:
    def __init__(self, cfg, home):
        self.home = Path(home).resolve()
        self.folder = self.home/'data/companion/bilibili'
        package = Path(getattr(cfg, 'bili_plugin', '') or '__missing__')
        self.source = package/'vendor/bilibili-mcp/mcp_server.py'
        if not self.source.is_file(): self.source = self.folder/'mcp_server.py'
        # The legacy environment already owns MCP/bilibili-api dependencies.
        native = ROOT/('.venv/Scripts/python.exe' if os.name=='nt' else '.venv/bin/python')
        self.python = str(native if native.is_file() else Path(sys.executable))
        self.process = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.tools = None
        self.login = BilibiliLogin(self.folder)

    def available(self):
        return self.source.is_file()

    def status(self):
        return dict(available=self.available(), credential_present=(self.folder/'bili_credential.json').is_file(),
                    connected=self.process is not None and self.process.returncode is None,
                    login='bili_login → 用B站App扫码 → bili_login_check；无需提供密码')

    def prepare(self):
        if not self.available():
            raise ValueError('B站后端未安装：请配置 bili_plugin')
        self.folder.mkdir(parents=True, exist_ok=True)
        for name in ('mcp_server.py', 'LICENSE'):
            source = self.source.with_name(name)
            if source.is_file() and source.resolve()!=(self.folder/name).resolve(): shutil.copy2(source, self.folder/name)
        prepare_backend(self.folder/'mcp_server.py')
        credential = self.folder/'bili_credential.json'
        if not credential.exists():
            for old in (self.home/'data/dsh-home/bili-duty/bili_credential.json', self.source.with_name('bili_credential.json')):
                if old.is_file():
                    shutil.copy2(old, credential)
                    os.chmod(credential, 0o600)
                    break

    async def _rpc(self, method, params):
        self.sequence += 1
        identity = self.sequence
        self.process.stdin.write((json.dumps(dict(jsonrpc='2.0',id=identity,method=method,params=params))+'\n').encode())
        await self.process.stdin.drain()
        while True:
            line = await self.process.stdout.readline()
            if not line: raise RuntimeError('B站后端已退出；检查 mcp 和 bilibili-api-python 依赖')
            try: message = json.loads(line)
            except ValueError: continue
            if message.get('id')!=identity: continue
            if 'error' in message: raise RuntimeError('B站工具协议请求失败')
            return message['result']

    async def _start(self):
        if self.process and self.process.returncode is None: return
        self.prepare()
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        env.pop('BILI_MCP_TOOLS', None)
        self.process = await asyncio.create_subprocess_exec(self.python, '-u', str(self.folder/'mcp_server.py'),
            cwd=self.folder, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=16*1024*1024,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        await self._rpc('initialize',dict(protocolVersion='2024-11-05',capabilities={},clientInfo=dict(name='independent-companion',version='1')))
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await self.process.stdin.drain()
        self.tools = (await self._rpc('tools/list',{}))['tools']

    def _redact(self, text):
        path = self.folder/'bili_credential.json'
        try: secrets = json.loads(path.read_text('utf-8'))
        except (OSError,ValueError): secrets = {}
        for key,value in secrets.items():
            if key!='dedeuserid' and isinstance(value,str) and len(value)>6:
                text = text.replace(value,'[credential]')
        return text

    async def call(self, op, arguments=None, *, timeout=90):
        async with self.lock:
            try:
                async with asyncio.timeout(timeout):
                    if op in LOGIN_OPS:
                        self.prepare()
                        return await self.login.call(op)
                    await self._start()
                    if op=='tools':
                        from .companion_bilibili_reader import TOOL
                        catalog = [TOOL,*self.tools]
                        name=(arguments or {}).get('name')
                        if name:
                            return dict(tools=[t for t in catalog if t['name']==name])
                        return dict(tools=[dict(name=t['name'],description=t.get('description','')[:180]) for t in catalog],
                                    next='用 tools arguments={"name":"工具名"} 读取该工具的完整参数。')
                    if op not in {t['name'] for t in self.tools}: raise ValueError('未知B站工具；先调用 tools 查看实际参数')
                    result = await self._rpc('tools/call',dict(name=op,arguments=arguments or {}))
                    return json.loads(self._redact(json.dumps(result,ensure_ascii=False)))
            except BaseException:
                await self.close()
                raise

    async def run(self, action):
        started = time.monotonic()
        try:
            args = json.loads(action.get('arguments') or '{}')
            if not isinstance(args,dict): raise ValueError('B站参数须为JSON对象')
            receipt = await self.call(action.get('op','tools'),args)
            failed = bool(receipt.get('isError'))
            if 'tools' in receipt:
                output = json.dumps(receipt,ensure_ascii=False)
            else:
                texts=[]
                for item in receipt.get('content',[]):
                    if item.get('type')!='text': continue
                    value=item.get('text','')
                    try:
                        data=json.loads(value)
                        if isinstance(data,dict) and (data.get('error') or data.get('success') is False or data.get('ok') is False or data.get('status') in {'failed','error'}):
                            failed = True
                        if isinstance(data,dict) and data.get('status')=='qrcode_ready':
                            data.pop('qrcode_image',None);data.pop('qrcode_terminal',None)
                            value=json.dumps(data,ensure_ascii=False)
                    except ValueError: pass
                    texts.append(value)
                output='\n'.join(texts)
            result=dict(status='failed' if failed else 'completed',output=output)
        except asyncio.CancelledError: raise
        except Exception as error:
            result=dict(status='failed',output=str(error) if isinstance(error,ValueError) else 'B站请求未完成：'+type(error).__name__)
        folder=self.home/'data/companion/artifacts';folder.mkdir(parents=True,exist_ok=True)
        path=folder/(uuid.uuid4().hex+'.bilibili.json')
        path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        return dict(result,seconds=round(time.monotonic()-started,3),output_path=str(path),output=result['output'][:14000])

    async def close(self):
        await self.login.close()
        process,self.process=self.process,None
        if process and process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(),3)
            except asyncio.TimeoutError:
                process.kill();await process.wait()


def inspect_status(cfg, home):
    """Local presence only; login validity requires bili_check_credential."""
    return Bilibili(cfg, home).status()
