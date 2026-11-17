"""Read-only Bilibili inbox transport. No model calls, sends, or owner-chat injection."""
import asyncio
import json
import math
from pathlib import Path
import time

from .text import stable_id


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def uid(value):
    try:
        return int(value) if not isinstance(value, bool) and str(value).isdigit() else 0
    except (ValueError, TypeError):
        return 0


def timestamp(value):
    try:
        value = float(value)
        if not math.isfinite(value) or value <= 0:return 0
        return value / 1_000_000 if value > 1e15 else value / 1000 if value > 1e12 else value
    except (ValueError, TypeError):return 0


class InboxError(Exception):
    def __init__(self, code, tool=''):
        self.code, self.tool = code, tool
        super().__init__(code)


class BilibiliSocial:
    """Own durable inbox delivery only; the core owns cognition and outbound actions."""
    def __init__(self, core, *, backend=None, clock=time.time, interval=120):
        self.core, self.life, self.repo = core, core.life, core.repo
        self.scope = self.life.scope('bilibili:social', 'bilibili', 'bilibili:social')
        self._backend_override, self.clock = backend, clock
        self.interval = max(60, float(interval))
        self.active = bool(getattr(core, 'active', True))
        self.state, self.error, self.error_tool, self.account = 'starting', '', '', ''
        self.next_poll, self.last_poll, self._login_at, self._failures = 0, 0, 0, 0
        self._runner, self._closed = None, False
        self._credential_stamp = None
        self._poll_lock, self._wake = asyncio.Lock(), asyncio.Event()
        with self.repo.tx():
            self.repo._exec('CREATE TABLE IF NOT EXISTS companion_bili_inbox('
                'scope TEXT,account TEXT,id TEXT,stream TEXT,status TEXT,text TEXT,source TEXT,'
                'source_event_id TEXT,result TEXT,occurred REAL,created REAL,updated REAL,next_due REAL,'
                'PRIMARY KEY(scope,account,id))')
            self.repo._exec('CREATE TABLE IF NOT EXISTS companion_bili_streams('
                'scope TEXT,account TEXT,stream TEXT,baselined INTEGER,started REAL,'
                'PRIMARY KEY(scope,account,stream))')
            # Never repeat a possibly successful external action after a crashed cognition.
            self.repo._exec("UPDATE companion_bili_inbox SET status='interrupted',updated=? "
                "WHERE scope=? AND status='working'", (clock(), self.scope.scope_id))

    @property
    def backend(self):
        if self._backend_override is not None:return self._backend_override
        executor, seen = self.core.executor, set()
        while executor is not None and id(executor) not in seen:
            seen.add(id(executor))
            if getattr(executor, '_bilibili', None) is not None:return executor._bilibili
            executor = getattr(executor, 'original', None)
        return None

    async def _call(self, name, arguments=None):
        try:
            return await self._request(name, arguments)
        except InboxError as error:
            error.tool = error.tool or name
            raise
        except Exception:
            raise InboxError('transport_error', name) from None

    async def _request(self, name, arguments=None):
        backend = self.backend
        if backend is None:raise InboxError('backend_unavailable')
        if callable(getattr(backend,'available',None)) and not backend.available():raise InboxError('backend_unavailable')
        response = await backend.call(name, arguments or {}, timeout=40)
        if not isinstance(response, dict):raise InboxError('invalid_response')
        if 'content' in response:
            text = '\n'.join(item.get('text', '') for item in response.get('content', []) if item.get('type')=='text')
            if response.get('isError'):
                raise InboxError('login_required' if any(term in text.lower() for term in ('未登录','not logged','-101','credential')) else 'tool_failed')
            try:response = json.loads(text)
            except ValueError:raise InboxError('invalid_response') from None
        if not isinstance(response, dict):raise InboxError('invalid_response')
        if response.get('error') or response.get('ok') is False:
            message = str(response.get('error', ''))
            raise InboxError('login_required' if any(term in message.lower() for term in ('未登录','not logged','-101','credential')) else 'remote_error')
        return response

    def _stream(self, name):
        return self.repo._one('SELECT * FROM companion_bili_streams WHERE scope=? AND account=? AND stream=?',
            (self.scope.scope_id, self.account, name))

    def _ensure_stream(self, name, *, baselined=False):
        self.repo._exec('INSERT OR IGNORE INTO companion_bili_streams VALUES(?,?,?,?,?)',
            (self.scope.scope_id, self.account, name, int(baselined), self.clock()))

    def _item(self, row):
        return dict(id=row['id'], text=row['text'], source=json.loads(row['source']),
                    source_event_id=row['source_event_id'], status=row['status'], occurred_at=row['occurred'])

    def context(self):
        counts = {row['status']:row['n'] for row in self.repo._all(
            'SELECT status,COUNT(*) n FROM companion_bili_inbox WHERE scope=? AND account=? GROUP BY status',
            (self.scope.scope_id,self.account))}
        return dict(state=self.state, error=self.error, error_tool=self.error_tool or None,
                    active=self.active, logged_in=bool(self.account) and self.state!='waiting_login',
                    scope=self.scope.conversation_id, account_uid=self.account or None,
                    pending=counts.get('pending',0), interrupted=counts.get('interrupted',0),
                    counts=counts,last_poll=self.last_poll or None,next_poll=self.next_poll or None,
                    interval_seconds=self.interval,notice='空轮询不调用模型；首次实际读到的历史只建基线。')

    snapshot = context

    def pending(self, limit=1):
        if not self.active or not self.account or self._closed or self.state=='waiting_login':return []
        return [self._item(row) for row in self.repo._all(
            "SELECT * FROM companion_bili_inbox WHERE scope=? AND account=? AND status='pending' AND next_due<=? ORDER BY occurred,created,id LIMIT ?",
            (self.scope.scope_id,self.account,self.clock(),max(1,min(int(limit),50))))]

    def claim(self, identity):
        if not self.active or self._closed:return None
        with self.repo.tx():
            row=self.repo._one("SELECT * FROM companion_bili_inbox WHERE scope=? AND account=? AND id=? AND status='pending' AND next_due<=?",
                (self.scope.scope_id,self.account,str(identity),self.clock()))
            if row is None:return None
            self.repo._exec("UPDATE companion_bili_inbox SET status='working',updated=? WHERE scope=? AND account=? AND id=?",
                (self.clock(),self.scope.scope_id,self.account,str(identity)))
            return dict(self._item(row),status='working')

    def settle(self, identity, result):
        """Settle inbox processing, not the delivery status of an outgoing message."""
        if not isinstance(result,dict):raise ValueError('bilibili settle requires a result object')
        status=result.get('status','completed')
        retry=status in {'deferred','retry'}
        delay=result.get('after_seconds',self.interval)
        if type(delay) not in (int,float) or not math.isfinite(delay):raise ValueError('invalid retry delay')
        state='pending' if retry else 'failed' if status=='failed' else 'processed'
        with self.repo.tx():
            row=self.repo._one("SELECT id FROM companion_bili_inbox WHERE scope=? AND account=? AND id=? AND status IN ('working','interrupted')",
                (self.scope.scope_id,self.account,str(identity)))
            if not row:return False
            self.repo._exec('UPDATE companion_bili_inbox SET status=?,result=?,updated=?,next_due=? WHERE scope=? AND account=? AND id=?',
                (state,encode(result),self.clock(),self.clock()+max(60,min(delay,86400)) if retry else 0,
                 self.scope.scope_id,self.account,str(identity)))
        return True

    def _normalise(self, channel, item, *, peer=0):
        if not isinstance(item,dict):return None
        sender=uid(item.get('from_uid') if channel=='dm' else item.get('mid'))
        if not sender or sender==uid(self.account) or (channel=='dm' and sender!=peer):return None
        raw_id=item.get('key') if channel=='dm' else item.get('id') or item.get('rpid')
        if not isinstance(raw_id,(str,int)) or isinstance(raw_id,bool) or not str(raw_id):return None
        bvid=str(item.get('bv') if channel=='dm' else item.get('bvid') or item.get('aid') or '')
        if bvid=='None':bvid=''
        rpid,root=uid(item.get('rpid')),uid(item.get('root'))
        thread='dm:'+self.account+':'+str(peer) if channel=='dm' else 'video:'+bvid+':'+str(root or rpid)
        identity=(stable_id('bili-inbox',self.account,'comment',bvid,str(rpid)) if channel!='dm' and rpid
                  else stable_id('bili-inbox',self.account,channel,thread,str(raw_id)))
        text=str(item.get('text') if channel=='dm' else item.get('content') or '')[:2000]
        if text=='None':text=''
        if not text.strip():return None
        source=dict(kind='bilibili',channel=channel,sender_uid=str(sender),recipient_uid=self.account,
            thread=thread,message_id=str(raw_id),bvid=bvid,rpid=rpid,root=root,
            nickname=str(item.get('nickname') or '')[:100])
        return dict(id=identity,text=text,source=source,occurred=timestamp(item.get('ts') if channel=='dm' else item.get('at')))

    def _ingest(self, stream, items):
        row=self._stream(stream)
        baseline=not row or not row['baselined']
        account_started=self._stream('@account')['started']
        added=0
        with self.repo.tx():
            self._ensure_stream(stream)
            for item in items:
                if item is None:continue
                identity=item['id']
                if self.repo._one('SELECT 1 FROM companion_bili_inbox WHERE scope=? AND account=? AND id=?',
                    (self.scope.scope_id,self.account,identity)):continue
                # IDs are the dedup key. This older-history fence covers a newly discovered
                # conversation beyond the first session page; retain a one-second margin.
                historical=baseline or bool(item['occurred'] and item['occurred']<int(account_started)-1)
                event_id=''
                if not historical:
                    event,_=self.life.ingest(self.scope,'bilibili_inbox','tool_observation',item['text'],
                        external_id=identity,actor_id='bilibili:'+item['source']['sender_uid'],
                        occurred_at=item['occurred'] or self.clock(),payload={'bilibili':item['source'],'external':True})
                    event_id=event.id;added+=1
                self.repo._exec('INSERT INTO companion_bili_inbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.scope.scope_id,self.account,identity,stream,'baseline' if historical else 'pending',item['text'],
                     encode(item['source']),event_id,'{}',item['occurred'] or self.clock(),self.clock(),self.clock(),0))
            self.repo._exec('UPDATE companion_bili_streams SET baselined=1 WHERE scope=? AND account=? AND stream=?',
                (self.scope.scope_id,self.account,stream))
        if added:
            wake=getattr(self.core,'_wake',None)
            if wake is not None:wake.set()
        return added

    async def _poll_feeds(self, unread):
        errors=[]
        for channel,tool,count in [('reply','bili_received_replies','recv_reply'),('at','bili_received_at_and_likes','at')]:
            stream=self._stream(channel)
            if stream and stream['baselined'] and not uid(unread.get(count)):continue
            try:
                response=await self._call(tool)
                if response.get('at_error'):raise InboxError('remote_error')
                if not isinstance(response.get('items'),list):raise InboxError('invalid_response')
                self._ingest(channel,[self._normalise(channel,item) for item in response['items']])
            except InboxError as error:
                error.tool = error.tool or tool
                errors.append(error)
        return errors

    async def _poll_dms(self, unread):
        errors=[]
        catalog=self._stream('@dm_catalog')
        unfinished=self.repo._all("SELECT stream FROM companion_bili_streams WHERE scope=? AND account=? AND stream LIKE 'dm:%' AND baselined=0",
            (self.scope.scope_id,self.account))
        if catalog and not uid(unread.get('chat')) and not unfinished:return errors
        response=await self._call('bili_dm_sessions',{'size':20})
        if not isinstance(response.get('items'),list):raise InboxError('invalid_response','bili_dm_sessions')
        sessions={uid(item.get('talker_uid')):item for item in response['items'] if isinstance(item,dict)
                  and uid(item.get('talker_uid')) and uid(item.get('talker_uid'))!=uid(self.account)}
        with self.repo.tx():
            for peer in sessions:
                if peer!=uid(self.account):self._ensure_stream('dm:'+str(peer),baselined=bool(catalog))
            self._ensure_stream('@dm_catalog',baselined=True)
        peers=set(uid(row['stream'][3:]) for row in unfinished)
        peers.update(peer for peer,item in sessions.items() if not catalog or uid(item.get('unread')) or not self._stream('dm:'+str(peer))['baselined'])
        for peer in sorted(peers):
            if not peer or peer==uid(self.account):continue
            try:
                response=await self._call('bili_dm_recent',{'size':30,'talker_id':peer,'pages':3})
                if uid(response.get('me'))!=uid(self.account) or uid(response.get('talker'))!=peer:raise InboxError('account_mismatch')
                if not isinstance(response.get('items'),list):raise InboxError('invalid_response')
                self._ingest('dm:'+str(peer),[self._normalise('dm',item,peer=peer) for item in response['items']])
            except InboxError as error:
                error.tool = error.tool or 'bili_dm_recent'
                errors.append(error)
        return errors

    async def poll_once(self, *, force=False):
        if self._closed or not self.active:return self.context()
        folder=getattr(self.backend,'folder',None)
        if folder:
            try:
                stat=(Path(folder)/'bili_credential.json').stat();stamp=(str(folder),stat.st_mtime_ns,stat.st_size)
            except OSError:stamp=(str(folder),None)
            if stamp!=self._credential_stamp:
                self._credential_stamp=stamp;self.next_poll=0;self._login_at=0
        if not force and self.clock()<self.next_poll:return self.context()
        async with self._poll_lock:
            now=self.clock()
            if not force and now<self.next_poll:return self.context()
            try:
                if not self.account or not self._login_at or now-self._login_at>=3600 or self.state=='waiting_login':
                    login=await self._call('bili_check_credential')
                    if login.get('logged_in') is not True or not uid(login.get('uid')):raise InboxError('login_required')
                    self.account=str(uid(login['uid']));self._login_at=now
                    with self.repo.tx():self._ensure_stream('@account',baselined=True)
                unread=await self._call('bili_unread_messages')
                # This authenticated request, rather than the public user-info lookup,
                # is what establishes actual inbox availability.
                if not any(key in unread for key in ('chat','at','recv_reply')):raise InboxError('invalid_unread_response')
                errors=await self._poll_feeds(unread)
                errors.extend(await self._poll_dms(unread))
                if errors:raise errors[0]
                self.state,self.error,self.error_tool,self._failures='online','','',0
                self.next_poll=now+self.interval
            except asyncio.CancelledError:raise
            except Exception as error:
                code=error.code if isinstance(error,InboxError) else 'transport_error'
                self.error_tool=error.tool if isinstance(error,InboxError) else ''
                self.error=code;self._failures+=1
                if code=='account_mismatch':self._login_at=0
                self.state='waiting_login' if code=='login_required' else 'error'
                self.next_poll=now+(900 if code=='login_required' else min(1800,self.interval*2**min(self._failures,4)))
            self.last_poll=now
        return self.context()

    async def _run(self):
        while not self._closed and self.active:
            await self.poll_once()
            self._wake.clear()
            # Only filesystem metadata is checked between network polls, so a new QR
            # login is noticed promptly without repeatedly asking Bilibili or a model.
            try:await asyncio.wait_for(self._wake.wait(),max(1,min(30,self.next_poll-self.clock())))
            except asyncio.TimeoutError:pass

    async def start(self):
        await self.set_active(bool(getattr(self.core,'active',True)))

    async def set_active(self, active):
        self.active=bool(active)
        if not self.active:
            if self._runner:
                self._runner.cancel();await asyncio.gather(self._runner,return_exceptions=True);self._runner=None
            self.state='paused';return
        if self._closed:return
        self.next_poll=0;self._wake.set()
        if self._runner is None or self._runner.done():self._runner=asyncio.create_task(self._run(),name='companion-bilibili-social')

    async def close(self):
        self._closed=True
        if self._runner:
            self._runner.cancel();await asyncio.gather(self._runner,return_exceptions=True);self._runner=None
        self.state='closed'
