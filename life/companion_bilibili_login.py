"""Bilibili web QR login: retain HTTP cookies and verify nav before persisting.

The upstream QR parser used by the vendored MCP only reads legacy URL query
parameters; current successful polls can instead return Set-Cookie or an SSO
crossDomain URL. This adapter keeps the existing MCP result shape.
"""
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx


LOGIN_OPS = frozenset({'bili_login', 'bili_login_check', 'bili_check_credential'})
PASSPORT = 'https://passport.bilibili.com/x/passport-login/web/qrcode/'
NAV = 'https://api.bilibili.com/x/web-interface/nav'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
           'Referer': 'https://www.bilibili.com/'}
COOKIE_FIELDS = {'SESSDATA': 'sessdata', 'bili_jct': 'bili_jct', 'DedeUserID': 'dedeuserid', 'buvid3': 'buvid3', 'buvid4': 'buvid4'}


def _trusted_url(url):
    parsed = urlsplit(url)
    return (parsed.scheme == 'https' and parsed.hostname in
            {'passport.bilibili.com', 'passport.biligame.com', 'www.bilibili.com'}
            and parsed.port in (None, 443) and not parsed.username and not parsed.password)


class BilibiliLogin:
    def __init__(self, folder, *, client=None):
        self.folder = Path(folder)
        self.path = self.folder/'bili_credential.json'
        self.client = client
        self.owns_client = client is None
        self.key = None
        self.completed_poll = None

    def _http(self):
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=False)
            self.owns_client = True
        return self.client

    def _load(self):
        try:
            data = json.loads(self.path.read_text('utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        self.folder.mkdir(parents=True, exist_ok=True)
        previous = self.path.stat() if self.path.exists() else None
        descriptor, name = tempfile.mkstemp(prefix='.bili-credential-', dir=self.folder)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            if previous and hasattr(os, 'chown'):
                os.chown(name, previous.st_uid, previous.st_gid)
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    async def _nav(self, credential):
        session = credential.get('sessdata')
        if not isinstance(session, str) or not session:
            return None
        response = await self._http().get(NAV, headers=dict(HEADERS, Cookie='SESSDATA='+session))
        response.raise_for_status()
        payload = response.json()
        data = payload.get('data') or {}
        if payload.get('code') == 0 and data.get('isLogin') is True and str(data.get('mid', '')).isdigit() and int(data['mid']) > 0:
            return data
        return None

    async def _status(self):
        credential = self._load()
        if not credential.get('sessdata') or not credential.get('bili_jct'):
            return dict(logged_in=False, message='没有完整有效的登录凭据，请扫码登录。')
        data = await self._nav(credential)
        if not data:
            return dict(logged_in=False, message='B站未确认当前会话登录，请重新扫码。')
        uid = str(data['mid'])
        if credential.get('dedeuserid') != uid:
            self._save(dict(credential, dedeuserid=uid))
        return dict(logged_in=True, uid=uid, username=data.get('uname', ''), message='已通过B站当前会话验证。')

    async def _generate(self):
        status = await self._status()
        if status['logged_in']:
            return dict(status, status='already_logged_in')
        self._http().cookies.clear()
        response = await self._http().get(PASSPORT+'generate', headers=HEADERS)
        response.raise_for_status()
        payload = response.json()
        data = payload.get('data') or {}
        if payload.get('code') != 0 or not data.get('qrcode_key') or not _trusted_url(data.get('url', '')):
            return dict(status='error', error='B站没有返回有效的扫码入口。')
        import qrcode
        image = BytesIO()
        qrcode.make(data['url']).save(image, format='PNG')
        self.key, self.completed_poll = data['qrcode_key'], None
        return dict(status='qrcode_ready', message='请用B站App扫码并确认，然后检查结果。',
                    qrcode_image='data:image/png;base64,'+base64.b64encode(image.getvalue()).decode(),
                    qrcode_url=data['url'])

    def _credential(self, url=''):
        credential = {}
        query = parse_qs(urlsplit(url).query) if _trusted_url(url) else {}
        for name, field in COOKIE_FIELDS.items():
            if query.get(name):
                credential[field] = query[name][-1]
        for cookie in self._http().cookies.jar:
            domain = cookie.domain.lstrip('.')
            if cookie.name in COOKIE_FIELDS and (domain == 'bilibili.com' or domain.endswith('.bilibili.com')):
                credential[COOKIE_FIELDS[cookie.name]] = cookie.value
        return credential

    async def _exchange(self, url):
        # Follow only Bilibili's known SSO hosts; never forward cookies by hand.
        for _ in range(5):
            if not _trusted_url(url):
                raise ValueError('B站登录返回了不支持的跳转地址。')
            response = await self._http().get(url, headers=HEADERS, follow_redirects=False)
            if response.is_redirect:
                url = urljoin(url, response.headers['location'])
            else:
                response.raise_for_status()
                return
        raise ValueError('B站登录跳转次数异常，请稍后重试。')

    async def _poll(self):
        if not self.key:
            status = await self._status()
            return dict(status, status='already_logged_in' if status['logged_in'] else 'no_session')
        if self.completed_poll is None:
            response = await self._http().get(PASSPORT+'poll', params={'qrcode_key': self.key}, headers=HEADERS)
            response.raise_for_status()
            payload = response.json()
            if payload.get('code') != 0:
                return dict(status='error', error='B站扫码状态请求未成功。')
            data = payload.get('data') or {}
            code = data.get('code')
            if code == 86101:
                return dict(status='scanning', message='等待使用B站App扫码。')
            if code == 86090:
                return dict(status='confirming', message='已扫码，等待在手机上确认。')
            if code == 86038:
                self.key = None
                return dict(status='timeout', message='二维码已过期，请生成新的二维码。')
            if code != 0:
                return dict(status='unknown', message='B站尚未返回成功登录状态。')
            # Keep the successful response and cookiejar if nav/exchange needs retry:
            # polling the same consumed key again may lose the login response.
            self.completed_poll = data
        data = self.completed_poll
        url = data.get('url') or ''
        credential = self._credential(url)
        if (not credential.get('sessdata') or not credential.get('bili_jct')) and url:
            await self._exchange(url)
            credential = self._credential(url)
        if not credential.get('sessdata') or not credential.get('bili_jct'):
            return dict(status='verification_failed', logged_in=False,
                        error='扫码已确认，但B站没有交付完整登录凭据；尚未保存，请重新检查或重新扫码。')
        nav = await self._nav(credential)
        if not nav:
            return dict(status='verification_failed', logged_in=False, error='扫码会话尚未通过B站验证，原凭据未改动。')
        credential['dedeuserid'] = str(nav['mid'])
        credential.setdefault('buvid3', '')
        if data.get('refresh_token'):
            credential['ac_time_value'] = data['refresh_token']
        self._save(credential)
        self.key, self.completed_poll = None, None
        self._http().cookies.clear()
        return dict(status='done', logged_in=True, uid=str(nav['mid']), username=nav.get('uname', ''),
                    message='登录成功，已验证当前会话并保存凭据。')

    async def call(self, op):
        if op not in LOGIN_OPS:
            raise ValueError('Unsupported Bilibili login operation')
        try:
            payload = await {'bili_login': self._generate, 'bili_login_check': self._poll,
                             'bili_check_credential': self._status}[op]()
        except (httpx.HTTPError, ValueError, OSError) as error:
            # Exception strings can contain ticket URLs/cookies; never surface them.
            payload = dict(status='error', logged_in=False, error='B站登录请求未完成：'+type(error).__name__)
        return dict(content=[dict(type='text', text=json.dumps(payload, ensure_ascii=False))],
                    isError=bool(payload.get('error')))

    async def close(self):
        if self.client is not None and self.owns_client:
            await self.client.aclose()
