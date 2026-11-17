"""An optional owned SSH forward; borrow a healthy existing forward without killing it."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from aiohttp import ClientSession,ClientTimeout
from .supervisor import ProcessTree


class ManagedTunnel:
    def __init__(self,home,remote):
        self.home,self.remote=Path(home),remote
        self.tree=None;self.log=None;self.settings=None
        path=self.home/'data/companion/ssh.json'
        if not remote.enabled or not path.is_file():return
        value=json.loads(path.read_text('utf-8-sig'))
        if not isinstance(value,dict):raise ValueError('SSH配置必须是对象')
        for key in ('port','local_port','remote_port'):
            if type(value.get(key)) is not int or not 1<=value[key]<=65535:raise ValueError('SSH端口配置无效')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*',str(value.get('host',''))):raise ValueError('SSH主机配置无效')
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*',str(value.get('user',''))):raise ValueError('SSH用户配置无效')
        identity=Path(value.get('identity_file','')).expanduser().resolve()
        if not identity.is_file():raise ValueError('SSH密钥文件不存在')
        url=urlsplit(remote.url)
        if url.scheme!='http' or url.hostname not in ('localhost','127.0.0.1') or (url.port or 80)!=value['local_port']:
            raise ValueError('远端地址须指向SSH配置中的本机转发端口')
        self.settings=dict(value,identity_file=str(identity))

    async def _healthy(self):
        try:
            async with ClientSession(trust_env=False,timeout=ClientTimeout(total=2)) as client:
                async with client.get(self.remote.url.rstrip('/')+'/health',headers={'Authorization':'Bearer '+self.remote.token}) as response:
                    return response.status==200 and (await response.json()).get('ok') is True
        except (OSError,ValueError,asyncio.TimeoutError):return False
        except Exception:return False

    async def _port_open(self):
        try:
            reader,writer=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',self.settings['local_port']),1)
            writer.close();await writer.wait_closed();return True
        except (OSError,asyncio.TimeoutError):return False

    async def ensure(self):
        if self.settings is None:return
        if self.tree is not None and self.tree.process.poll() is not None:await self._stop_owned()
        if await self._healthy():return
        if self.tree is None:
            if await self._port_open():
                raise RuntimeError('本机转发端口已占用，但角色服务认证未通过；未终止其他进程')
            executable=shutil.which('ssh')
            if not executable:raise RuntimeError('未找到OpenSSH客户端')
            s=self.settings
            command=[executable,'-N','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                     '-o','IdentitiesOnly=yes','-o','ExitOnForwardFailure=yes','-o','ConnectTimeout=10',
                     '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2','-o','LogLevel=ERROR',
                     '-i',s['identity_file'],'-p',str(s['port']),'-L',
                     f"127.0.0.1:{s['local_port']}:127.0.0.1:{s['remote_port']}",f"{s['user']}@{s['host']}"]
            folder=self.home/'data/companion';folder.mkdir(parents=True,exist_ok=True)
            self.log=(folder/'ssh.log').open('ab')
            try:
                self.tree=ProcessTree(command,cwd=self.home,env=dict(os.environ),pipes=True,
                                      stdout_file=subprocess.DEVNULL,stderr_file=self.log)
                self.tree.process.stdin.close()
            except BaseException:
                self.log.close();self.log=None;raise
        for _ in range(40):
            if self.tree.process.poll() is not None:
                await self._stop_owned();raise RuntimeError('SSH隧道已退出，请检查本机ssh.log')
            if await self._healthy():return
            await asyncio.sleep(.25)
        raise RuntimeError('SSH已启动，等待远端角色服务恢复')

    async def _stop_owned(self):
        tree,self.tree=self.tree,None
        if tree is not None:
            try:await asyncio.to_thread(tree.stop)
            finally:
                if self.log:self.log.close();self.log=None

    async def close(self):await self._stop_owned()
