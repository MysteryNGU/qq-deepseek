"""Authenticated loopback-only settings web server, independent of Bot uptime."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from http.cookies import SimpleCookie
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from .settings import SettingsStore,SettingsError,atomic_write


class SettingsServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,root,port=6100):
        self.store=SettingsStore(root)
        self.static=Path(__file__).parent/'settings_ui'
        self.sessions={};self.failures=[];self.auth_lock=threading.Lock()
        credential=self.store.folder/'auth.json'
        if not credential.exists():
            password=secrets.token_urlsafe(24);salt=secrets.token_hex(16)
            atomic_write(credential,json.dumps(dict(salt=salt,digest=self.password_hash(password,salt))))
            atomic_write(self.store.folder/'login.txt',password+'\n')
        self.auth=json.loads(credential.read_text('utf-8'))
        super().__init__(('127.0.0.1',port),SettingsHandler)

    @staticmethod
    def password_hash(password,salt):return hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),240000).hex()


class SettingsHandler(BaseHTTPRequestHandler):
    server_version='XiaoyuSettings'
    def log_message(self,*args):pass  # Never log passwords, bodies or auth headers.
    def setup(self):
        super().setup();self.connection.settimeout(15)

    def respond(self,status,value,content_type='application/json; charset=utf-8',cookie=None):
        body=json.dumps(value,ensure_ascii=False).encode() if isinstance(value,(dict,list)) else value if isinstance(value,bytes) else value.encode()
        self.send_response(status)
        self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header('Referrer-Policy','no-referrer')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(body)

    def allowed_host(self):
        port=self.server.server_address[1]
        return self.headers.get('Host') in {f'127.0.0.1:{port}',f'localhost:{port}'}

    def session(self):
        try:
            cookies=SimpleCookie(self.headers.get('Cookie',''));token=cookies['xiaoyu_settings'].value
        except (KeyError,ValueError):return None
        with self.server.auth_lock:
            self.server.sessions={k:v for k,v in self.server.sessions.items() if v['expires']>time.time()}
            return self.server.sessions.get(token)

    def handle_request(self,post=False):
        if not self.allowed_host():raise SettingsError('host_denied','仅允许本机地址访问',403)
        path=urlsplit(self.path).path
        if post:
            if self.headers.get('Origin')!='http://'+self.headers['Host']:raise SettingsError('origin_denied','请求来源不匹配',403)
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise SettingsError('invalid_content_type','需要 JSON 请求',415)
            try:size=int(self.headers.get('Content-Length','0'))
            except ValueError:raise SettingsError('invalid_length','请求长度无效')
            if not 0<size<=262144:raise SettingsError('too_large','请求过大',413)
            try:body=json.loads(self.rfile.read(size))
            except (ValueError,UnicodeError):raise SettingsError('invalid_json','JSON 格式不正确')
            if not isinstance(body,dict):raise SettingsError('invalid_json','需要一个 JSON 对象')
        else:body={}
        if not post and path in {'/','/app.js','/style.css'}:
            name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[path]
            content={'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[path]
            return self.respond(200,(self.server.static/name).read_bytes(),content)
        if post and path=='/api/login':
            password=body.get('password')
            if not isinstance(password,str) or len(password)>1024:raise SettingsError('unauthorized','登录信息不正确',401)
            with self.server.auth_lock:
                self.server.failures=[t for t in self.server.failures if time.time()-t<60]
                if len(self.server.failures)>=5:raise SettingsError('rate_limited','尝试过多，请一分钟后重试',429)
                if not hmac.compare_digest(self.server.password_hash(password,self.server.auth['salt']),self.server.auth['digest']):
                    self.server.failures.append(time.time());raise SettingsError('unauthorized','登录信息不正确',401)
                token=secrets.token_urlsafe(32);session=dict(csrf=secrets.token_urlsafe(32),expires=time.time()+43200)
                self.server.sessions[token]=session
            return self.respond(200,dict(ok=True,csrf=session['csrf']),cookie='xiaoyu_settings='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')
        session=self.session()
        if not session:raise SettingsError('unauthorized','请先登录',401)
        if post and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']):raise SettingsError('csrf_denied','会话校验失败，请刷新页面',403)
        if not post and path=='/api/session':return self.respond(200,dict(csrf=session['csrf']))
        if not post and path=='/api/settings':return self.respond(200,self.server.store.snapshot())
        if not post and path=='/api/status':return self.respond(200,self.server.store.status())
        if post and path=='/api/save':return self.respond(200,self.server.store.save(body.get('document'),body.get('base_revision'),body.get('changes')))
        if post and path=='/api/restart':return self.respond(202,self.server.store.request_restart())
        if post and path=='/api/logout':
            with self.server.auth_lock:self.server.sessions={k:v for k,v in self.server.sessions.items() if v is not session}
            return self.respond(200,dict(ok=True),cookie='xiaoyu_settings=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        raise SettingsError('not_found','没有这个设置接口',404)

    def do_GET(self):self.dispatch(False)
    def do_POST(self):self.dispatch(True)
    def dispatch(self,post):
        try:self.handle_request(post)
        except SettingsError as exc:self.respond(exc.status,dict(error=exc.code,message=str(exc)))
        except (ConnectionError,TimeoutError):pass
        except Exception:self.respond(500,dict(error='unavailable',message='暂时无法读取或保存设置，请检查本地文件与服务状态'))


def main():
    import argparse
    from scripts.codex_consult import file_lock
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--port',type=int,default=6100)
    args=parser.parse_args();folder=args.root/'data/settings';folder.mkdir(parents=True,exist_ok=True)
    with file_lock(folder/'server.lock',blocking=False):
        server=SettingsServer(args.root,args.port)
        atomic_write(folder/'server.json',json.dumps(dict(pid=__import__('os').getpid(),port=server.server_address[1],started=time.time())))
        try:server.serve_forever(poll_interval=.5)
        finally:server.server_close()


def ensure_server(root):
    """Start one hidden local service; its lifetime is independent of Bot restart."""
    import subprocess
    import sys
    import psutil
    root=Path(root).resolve();folder=root/'data/settings';folder.mkdir(parents=True,exist_ok=True)
    try:
        record=json.loads((folder/'server.json').read_text('utf-8'))
        process=psutil.Process(record['pid'])
        command=process.cmdline()
        if 'life.settings_web' in command and str(root) in command:return process.pid
    except (OSError,ValueError,KeyError,psutil.Error):pass
    with (folder/'server.log').open('ab') as log:
        return subprocess.Popen([sys.executable,'-X','utf8','-m','life.settings_web','--root',str(root)],cwd=root,
            stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).pid


if __name__=='__main__':main()
