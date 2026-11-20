"""微信官方 iLink Bot 通道：扫码登录、长轮询收消息、按 context_token 回复。

只走腾讯官方 iLink（ilinkai.weixin.qq.com），不碰逆向个人号协议。
协议与官方插件一致，与本站已有的 QQ(NapCat) 通道并列，共用同一个人格和记忆：
入站消息调用同一个 ``core.chat``，助手消息经投递队列发回微信私聊。

当前版本只做文字：媒体需要 AES-128-ECB 解密/加密，运行时没有加密库，
图片/语音/文件会明确回报“暂不支持”，不静默丢弃，也不假装收到。
"""
import asyncio
import base64
import io
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .companion_work import missing, visibility

BASE_URL = 'https://ilinkai.weixin.qq.com'
SEND_PATH = '/ilink/bot/sendmessage'
MAX_TEXT = 1200
STALE_TOKEN_ERRCODE = -14


def _folder(home):
    return Path(home) / 'data' / 'companion'


def _state_path(home):
    return _folder(home) / 'wechat-state.json'


def _token_path(home):
    return _folder(home) / 'wechat-token'


def _write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(text, encoding='utf-8')
    os.chmod(temp, 0o600)
    temp.replace(path)


def _read_state(home):
    empty = dict(bound_user='', base_url='', sync_buf='', context_tokens={})
    try:
        data = json.loads(_state_path(home).read_text('utf-8'))
    except (FileNotFoundError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    tokens = data.get('context_tokens')
    return dict(
        bound_user=str(data.get('bound_user') or ''),
        base_url=str(data.get('base_url') or ''),
        sync_buf=str(data.get('sync_buf') or ''),
        context_tokens=dict(tokens) if isinstance(tokens, dict) else {},
    )


def _write_state(home, state):
    data = dict(state)
    data['context_tokens'] = dict(state.get('context_tokens') or {})
    _write_private(_state_path(home), json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def load_wechat_settings(home):
    state = _read_state(home)
    try:
        config = json.loads((_folder(home) / 'wechat.json').read_text('utf-8'))
    except (FileNotFoundError, ValueError):
        config = {}
    enabled = config.get('enabled', False) if isinstance(config, dict) else False
    if type(enabled) is not bool:
        raise ValueError('微信开关须为布尔值')
    has_token = _token_path(home).is_file()
    return dict(enabled=enabled, has_token=has_token,
                bound_user=state['bound_user'], base_url=state['base_url'],
                login_required=enabled and not has_token)


def configure_wechat(home, *, enabled=None, token=None, clear_token=False, bound_user=None):
    folder = _folder(home)
    folder.mkdir(parents=True, exist_ok=True)
    current = load_wechat_settings(home)['enabled']
    value = current if enabled is None else enabled
    if type(value) is not bool:
        raise ValueError('微信开关须为布尔值')
    (folder / 'wechat.json.tmp').write_text(json.dumps(dict(enabled=value)), encoding='utf-8')
    (folder / 'wechat.json.tmp').replace(folder / 'wechat.json')
    if token:
        _write_private(_token_path(home), token.strip())
    if clear_token:
        try:
            _token_path(home).unlink()
        except FileNotFoundError:
            pass
        _write_state(home, dict(bound_user='', base_url='', sync_buf='', context_tokens={}))
    elif bound_user is not None:
        state = _read_state(home)
        state['bound_user'] = str(bound_user)
        _write_state(home, state)
    return load_wechat_settings(home)


def _headers(token=''):
    headers = {
        'content-type': 'application/json',
        'iLink-App-ClientVersion': '1',
        'X-WECHAT-UIN': base64.b64encode(str(int(time.time()) % 0xFFFFFFFF).encode('ascii')).decode('ascii'),
    }
    if token:
        headers['Authorization'] = 'Bearer ' + token
        headers['AuthorizationType'] = 'ilink_bot_token'
    return headers


def parse_qr_payload(body):
    """把 get_bot_qrcode 的响应转成 (qrcode_id, 二维码内容)。"""
    record = body if isinstance(body, dict) else {}
    qrcode_id = record.get('qrcode') or record.get('qrcode_id') or ''
    image = record.get('qrcode_img_content') or record.get('qrcode_url') or record.get('url') or ''
    if not qrcode_id or not image:
        raise ValueError('微信二维码字段缺失')
    return str(qrcode_id), str(image)


def parse_login_status(body):
    record = body if isinstance(body, dict) else {}
    status = str(record.get('status') or '')
    ret = int(record.get('ret') or 0)
    if ret == 0 and status == 'confirmed':
        token = str(record.get('bot_token') or '')
        if not token:
            raise ValueError('微信确认登录但缺少 token')
        return dict(status='confirmed', token=token,
                    bound_user=str(record.get('ilink_user_id') or ''),
                    base_url=str(record.get('baseurl') or record.get('base_url') or ''))
    if status == 'expired':
        return dict(status='expired')
    if status in ('scaned', 'scanned'):
        return dict(status='scanned')
    return dict(status='waiting')


# iLink 的 item 类型：只对能确定的命名，其余如实写成「非文本内容（类型 N）」，
# 不猜成图片或卡片——猜错会让她以为自己知道收到的是什么。
WECHAT_ITEM_LABELS = {1: '文字', 2: '图片', 3: '语音', 4: '文件', 5: '视频',
                      6: '表情', 7: '链接卡片', 8: '小程序', 9: '位置'}


def item_label(kind):
    return WECHAT_ITEM_LABELS.get(kind) or ('非文本内容（类型 %s）' % kind)


def unsupported_notice(labels):
    """她读得到的一句话：这条是什么、内容没看到。只写类型，不复述内容。"""
    names = '、'.join(dict.fromkeys(str(label) for label in labels if label))
    if not names:
        return '（这条消息里没有可读的文字，我没有看到内容。）'
    return f'（这条是{names}，我没有看到内容。）'


def parse_inbound(raw):
    """解析一条 getupdates 消息；只取文字，返回 None 表示不是可处理的私聊消息。"""
    if not isinstance(raw, dict):
        return None
    message = raw
    if isinstance(raw.get('message'), dict):
        message = dict(raw, **raw['message'])
    if message.get('message_type') is not None and int(message.get('message_type')) != 1:
        return None
    sender = str(message.get('from_user_id') or message.get('from_user') or '')
    if not sender:
        return None
    texts = []
    if isinstance(message.get('text'), str):
        texts.append(message['text'])
    unsupported = []
    items = message.get('item_list')
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                kind = int(item.get('type') or 0)
            except (TypeError, ValueError):
                kind = 0
            if kind == 1:
                text_item = item.get('text_item') or {}
                value = text_item.get('text') if isinstance(text_item, dict) else None
                if value:
                    texts.append(str(value))
            elif kind == 3:
                voice = item.get('voice_item') or {}
                asr = voice.get('text') if isinstance(voice, dict) else None
                if asr:
                    texts.append(f'[语音] {asr}')
                else:
                    unsupported.append(item_label(kind))
            elif kind != 1:
                # 图片/文件/视频/表情/卡片/小程序/未知类型：内容看不到，但类型必须露出来。
                unsupported.append(item_label(kind))
    message_id = message.get('message_id') or message.get('msg_id') or \
        f"{sender}:{message.get('create_time_ms') or message.get('create_time') or 0}"
    return dict(sender=sender, text=''.join(texts).strip(), unsupported=unsupported,
                kinds=[item_label(kind) for kind in _item_kinds(items)],
                context_token=str(message.get('context_token') or ''),
                message_id=str(message_id))


def _item_kinds(items):
    kinds = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                kinds.append(int(item.get('type') or 0))
            except (TypeError, ValueError):
                kinds.append(0)
    return kinds


def render_qr_data_uri(text):
    """iLink 返回的是 liteapp 页面链接；把它编码成二维码图片再交给界面。"""
    try:
        import qrcode
    except ImportError as error:
        raise RuntimeError('服务器缺少 qrcode 依赖，无法生成二维码') from error
    image = qrcode.make(text)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def split_text(text, limit=MAX_TEXT):
    text = text or ''
    if len(text) <= limit:
        return [text] if text else []
    return [text[i:i + limit] for i in range(0, len(text), limit)]


class WeChatBot:
    """一个 iLink 登录态、一个轮询循环。同一时刻只有一个账号在收发。"""

    def __init__(self, core, cfg, home):
        self.core, self.cfg, self.home = core, cfg, Path(home)
        self.repo = core.repo
        self.agent = core.life.agent_id
        self.settings = load_wechat_settings(home)
        self.state_data = _read_state(home)
        self.state = 'disabled' if not self.settings['enabled'] else (
            'needs_login' if not self.settings['has_token'] else 'disconnected')
        self.error = ''
        self.http = None
        self.token = ''
        self._closed = False
        self._tasks = set()
        self._chat_tasks = set()
        self._outbox = asyncio.Event()
        self._login = None
        try:
            with self.repo.tx():
                self.repo._exec('CREATE TABLE IF NOT EXISTS companion_wechat_delivery('
                    'agent TEXT,direction TEXT,id TEXT,status TEXT,payload TEXT,updated REAL,'
                    'PRIMARY KEY(agent,direction,id))')
                self.repo._exec("UPDATE companion_wechat_delivery SET status='unknown' "
                    "WHERE agent=? AND status IN ('sending','processing')", (self.agent,))
        except Exception:
            self.error = 'storage_unavailable'

    # ── 状态 ────────────────────────────────────────────────
    def snapshot(self):
        rows = self.repo._all('SELECT direction,status,COUNT(*) n FROM companion_wechat_delivery '
                              'WHERE agent=? GROUP BY direction,status', (self.agent,))
        login = dict(self._login) if self._login else None
        if login:
            login.pop('fetch', None)
        return dict(enabled=self.settings['enabled'], state=self.state, error=self.error,
                    bound_user=self.state_data.get('bound_user', ''),
                    has_token=bool(self.token) or self.settings['has_token'],
                    login=login, deliveries=[dict(r) for r in rows])

    def _set_state(self, state, error=''):
        self.state = state
        if error:
            self.error = error

    # ── 生命周期 ────────────────────────────────────────────
    async def start(self):
        if self._closed or self._tasks or not self.settings['enabled']:
            return
        if not self.http:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None),
                                              trust_env=False)
        self.token = self._read_token()
        if not self.token:
            self._set_state('needs_login', '微信还没有扫码登录')
            return
        self._set_state('connecting')
        self._tasks = {asyncio.create_task(self._poll_loop()),
                       asyncio.create_task(self._send_loop())}

    async def close(self):
        if self._closed:
            return
        self._closed = True
        tasks = self._tasks | self._chat_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.http:
            await self.http.close()
            self.http = None
        self._set_state('closed')

    def _read_token(self):
        try:
            return _token_path(self.home).read_text('utf-8').strip()
        except FileNotFoundError:
            return ''

    def _persist_state(self):
        self.state_data.setdefault('context_tokens', {})
        _write_state(self.home, self.state_data)

    # ── HTTP ───────────────────────────────────────────────
    async def _post(self, path, body=None, timeout=30, tolerate_ret1=False):
        if not self.http:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None),
                                              trust_env=False)
        base = (self.state_data.get('base_url') or BASE_URL).rstrip('/')
        url = base + path
        async with self.http.post(url, data=json.dumps(body or {}),
                                  headers=_headers(self.token),
                                  timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status != 200:
                raise RuntimeError(f'wechat {path} http {response.status}')
            data = await response.json(content_type=None)
        data = data if isinstance(data, dict) else {}
        ret = int(data.get('ret') or 0)
        errcode = int(data.get('errcode') or 0)
        if tolerate_ret1 and ret == 1 and errcode == 0:
            return data
        if ret != 0 or errcode != 0:
            raise RuntimeError(f"wechat {path} ret={ret} errcode={errcode} {data.get('errmsg', '')}")
        return data

    # ── 扫码登录 ────────────────────────────────────────────
    async def begin_login(self):
        """取一张登录二维码；UI 用返回的 data URL 显示，凭 qrcode 轮询状态。"""
        if not self.settings['enabled']:
            raise ValueError('先在设置里打开微信通道')
        data = await self._post('/ilink/bot/get_bot_qrcode?bot_type=3', {}, timeout=15)
        qrcode_id, payload = parse_qr_payload(data)
        image, link = await self._qr_image(payload)
        self._login = dict(qrcode=qrcode_id, image=image, expires_at=int(time.time()) + 300)
        if link:
            self._login['url'] = link
        self._set_state('waiting_scan')
        result = dict(qrcode=qrcode_id, image=image, expires_in=300)
        if link:
            result['url'] = link
        return result

    async def _qr_image(self, value):
        """二维码字段可能是图片 URL、页面链接、data URL 或裸 base64。

        iLink 实际返回的是 liteapp 页面链接，不是图片；这时把链接本身编码成
        二维码，扫码后微信会打开那个页面完成绑定。同时把原始链接一起返回，
        界面可以在二维码不好扫时给出直达入口。
        """
        if value.startswith('data:'):
            return value, ''
        if value.startswith('http://') or value.startswith('https://'):
            content_type = ''
            raw = b''
            try:
                async with self.http.get(value, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    response.raise_for_status()
                    raw = await response.read()
                    content_type = (response.headers.get('Content-Type') or '').split(';')[0].strip()
            except Exception:
                raw, content_type = b'', ''
            if content_type.startswith('image/') and len(raw) <= 2 * 1024 * 1024:
                return f'data:{content_type};base64,{base64.b64encode(raw).decode("ascii")}', value
            return render_qr_data_uri(value), value
        try:
            base64.b64decode(value, validate=True)
        except Exception as error:
            raise ValueError('微信二维码内容无法识别') from error
        return 'data:image/png;base64,' + value, ''

    async def poll_login(self):
        if not self._login:
            raise ValueError('还没有待确认的二维码，先开始登录')
        qrcode = self._login['qrcode']
        data = await self._post(f'/ilink/bot/get_qrcode_status?qrcode={qrcode}', {}, timeout=40,
                                tolerate_ret1=True)
        status = parse_login_status(data)
        if status['status'] == 'confirmed':
            configure_wechat(self.home, token=status['token'],
                             bound_user=status['bound_user'], enabled=True)
            self.token = status['token']
            self.state_data = _read_state(self.home)
            if status['base_url']:
                self.state_data['base_url'] = status['base_url']
                self._persist_state()
            self.settings = load_wechat_settings(self.home)
            self._login = None
            await self.restart()
            return dict(status='confirmed', bound_user=self.state_data.get('bound_user', ''))
        if status['status'] == 'expired':
            self._login = None
            self._set_state('needs_login', '二维码已过期')
            return dict(status='expired')
        return dict(status=status['status'])

    async def logout(self):
        configure_wechat(self.home, clear_token=True)
        self.token = ''
        self.state_data = _read_state(self.home)
        self.settings = load_wechat_settings(self.home)
        self._login = None
        await self.restart()
        return dict(ok=True)

    async def restart(self):
        tasks = self._tasks
        self._tasks = set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.settings['enabled'] and self.token:
            self._tasks = {asyncio.create_task(self._poll_loop()),
                           asyncio.create_task(self._send_loop())}
        elif not self.settings['enabled']:
            self._set_state('disabled')

    # ── 收 ─────────────────────────────────────────────────
    async def _poll_loop(self):
        cursor = self.state_data.get('sync_buf', '')
        stale = 0
        while not self._closed:
            try:
                data = await self._post('/ilink/bot/getupdates',
                                        dict(get_updates_buf=cursor,
                                             base_info=dict(channel_version='1.0.0')),
                                        timeout=75)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._closed:
                    return
                message = str(error)
                if f'errcode={STALE_TOKEN_ERRCODE}' in message or f'ret={STALE_TOKEN_ERRCODE}' in message:
                    stale += 1
                    if stale >= 3:
                        self._set_state('needs_login', '微信登录态已失效，请重新扫码')
                        configure_wechat(self.home, clear_token=True)
                        self.token = ''
                        self.settings = load_wechat_settings(self.home)
                        return
                    await asyncio.sleep(60)
                    continue
                stale = 0
                self._set_state('disconnected', 'wechat_poll_' + type(error).__name__)
                await asyncio.sleep(5)
                continue
            stale = 0
            next_cursor = str(data.get('get_updates_buf') or data.get('cursor') or cursor)
            if next_cursor != cursor:
                cursor = next_cursor
                self.state_data['sync_buf'] = cursor
                self._persist_state()
            self._set_state('logged_in', '')
            messages = data.get('msgs') or data.get('messages') or data.get('updates')
            if isinstance(messages, list):
                for raw in messages:
                    parsed = parse_inbound(raw)
                    if not parsed:
                        continue
                    if parsed['context_token']:
                        self.state_data.setdefault('context_tokens', {})[parsed['sender']] = parsed['context_token']
                        self._persist_state()
                    bound = self.state_data.get('bound_user', '')
                    if bound and parsed['sender'] != bound:
                        continue
                    self._dispatch(parsed)

    def _dispatch(self, message):
        identity = message['message_id']
        if self.repo._one('SELECT id FROM companion_wechat_delivery WHERE agent=? AND direction=? AND id=?',
                          (self.agent, 'in', identity)):
            return
        self._store('in', identity, 'processing', message)
        task = asyncio.create_task(self._receive(identity, message))
        self._chat_tasks.add(task)
        task.add_done_callback(self._chat_tasks.discard)

    async def _receive(self, identity, message):
        try:
            text = message['text']
            labels = message.get('unsupported') or []
            gaps = [missing('这条%s的内容' % label, 'unsupported',
                            '微信通道只取文字，这段内容没有读到')
                    for label in dict.fromkeys(labels)]
            if labels:
                # 正文里就说清楚这条是什么、内容没看到，不静默当没发生。
                notice = unsupported_notice(labels)
                text = (text + '\n' + notice).strip()
            if not text:
                self._ignore(identity, '这条消息里没有可读的文字，也没有可标注的类型，没有交给她。')
                return
            if gaps:
                self._remember(identity, gaps=gaps, visibility=visibility(not_obtained=gaps))
            if getattr(self.core, 'supports_message_routing', False):
                await self.core.chat(text, [], source=dict(kind='wechat', name='微信'))
            else:
                await self.core.chat(text, [])
            self._set('in', identity, 'received')
            if labels:
                self._remember(identity, reason='非文本段没有内容，只在正文里标注了类型')
        except asyncio.CancelledError:
            self._set('in', identity, 'unknown')
            raise
        except Exception:
            self._set('in', identity, 'failed')
            self.error = 'incoming_failed'
            gap = missing('这条消息', 'failed', '接收失败：incoming_failed')
            self._remember(identity, reason=gap['detail'], gap=gap,
                           visibility=visibility(not_obtained=[gap]))

    # ── 发 ─────────────────────────────────────────────────
    def _store(self, direction, identity, status, payload):
        with self.repo.tx():
            self.repo._exec('INSERT OR IGNORE INTO companion_wechat_delivery VALUES(?,?,?,?,?,?)',
                            (self.agent, direction, str(identity), status,
                             json.dumps(payload, ensure_ascii=False), time.time()))

    def _set(self, direction, identity, status):
        with self.repo.tx():
            self.repo._exec('UPDATE companion_wechat_delivery SET status=?,updated=? '
                            'WHERE agent=? AND direction=? AND id=?',
                            (status, time.time(), self.agent, direction, str(identity)))

    def _remember(self, identity, **extra):
        """把「这条消息怎么了」写回记录，事后能查，不用看日志。"""
        try:
            row = self.repo._one('SELECT payload FROM companion_wechat_delivery '
                                 'WHERE agent=? AND direction=? AND id=?',
                                 (self.agent, 'in', str(identity)))
            payload = json.loads(row['payload']) if row and row['payload'] else {}
        except (TypeError, ValueError, KeyError, IndexError):
            payload = {}
        payload = dict(payload) if isinstance(payload, dict) else {}
        for key, value in extra.items():
            if value is None:
                continue
            payload[key] = value
        with self.repo.tx():
            self.repo._exec('UPDATE companion_wechat_delivery SET payload=?,updated=? '
                            'WHERE agent=? AND direction=? AND id=?',
                            (json.dumps(payload, ensure_ascii=False), time.time(),
                             self.agent, 'in', str(identity)))

    def _ignore(self, identity, reason):
        """被忽略也要留 status 和 reason：否则「没发生」无从查起。"""
        gap = missing('这条消息的可读内容', 'unsupported', reason)
        self._set('in', identity, 'ignored')
        self._remember(identity, reason=gap['detail'], gap=gap, status='ignored',
                       visibility=visibility(not_obtained=[gap]))

    def emit(self, event):
        if self._closed or not self.settings['enabled']:
            return
        if event.get('type') != 'message' or event.get('role') != 'assistant':
            return
        text = event.get('text', event.get('content', '')) or ''
        if not text.strip():
            return
        self._store('out', event.get('id') or uuid.uuid4().hex, 'queued',
                    dict(text=text, had_images=bool(event.get('images'))))
        self._outbox.set()

    async def _send_loop(self):
        while not self._closed:
            row = self.repo._one("SELECT * FROM companion_wechat_delivery WHERE agent=? "
                                 "AND direction='out' AND status='queued' ORDER BY updated LIMIT 1",
                                 (self.agent,))
            if not row:
                self._outbox.clear()
                await self._outbox.wait()
                continue
            payload = json.loads(row['payload'])
            target = self.state_data.get('bound_user', '')
            if not target:
                self._set('out', row['id'], 'failed')
                self.error = 'wechat_no_bound_user'
                continue
            self._set('out', row['id'], 'sending')
            try:
                for chunk in split_text(payload.get('text', '')):
                    await self._send_text(target, chunk)
                self._set('out', row['id'], 'sent')
            except asyncio.CancelledError:
                self._set('out', row['id'], 'unknown')
                raise
            except Exception:
                self._set('out', row['id'], 'unknown')
                self.error = 'outgoing_unconfirmed'

    async def _send_text(self, to_user, text):
        context = (self.state_data.get('context_tokens') or {}).get(to_user, '')
        message = dict(msg=dict(from_user_id='', to_user_id=to_user,
                                client_id='xiaoyu:' + uuid.uuid4().hex,
                                message_type=2, message_state=2, context_token=context,
                                item_list=[dict(type=1, text_item=dict(text=text))]),
                       base_info=dict(channel_version='1.0.0'))
        await self._post(SEND_PATH, message, timeout=20)

    async def send_text(self, text):
        """设置页“发送测试消息”用。"""
        target = self.state_data.get('bound_user', '')
        if not target:
            raise ValueError('微信还没有绑定对话用户，先扫码登录')
        for chunk in split_text(text):
            await self._send_text(target, chunk)
