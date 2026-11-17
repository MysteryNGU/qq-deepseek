"""IM 通道总览：把 QQ(NapCat 个人号) 和微信(官方 iLink) 收在同一份配置与状态里。

桌面端和手机端读写的都是这一份：QQ 沿用 ``data/companion/qq.json`` +
``qq-token``，微信用 ``wechat.json`` + ``wechat-token`` + ``wechat-state.json``。
两边都不保存明文到可同步的位置（token 文件权限 0600）。

这里只做“配置与生命周期”，消息收发仍由各通道自己完成，最终都调用同一个
``core.chat``，所以人格、记忆、项目不会因为换通道而分裂。
"""
import asyncio
import json
from pathlib import Path

from .companion_qq import CompanionQQ, configure_qq, load_qq_settings
from .companion_wechat import WeChatBot, configure_wechat, load_wechat_settings

CHANNELS = ('qq', 'wechat')


def load_im_settings(home):
    return dict(qq=load_qq_settings(home), wechat=load_wechat_settings(home))


def configure_im(home, channel, **options):
    if channel == 'qq':
        return configure_qq(home,
                           enabled=options.get('enabled', True),
                           ws_url=options.get('ws_url', 'ws://127.0.0.1:3001'),
                           token=options.get('token', ''))
    if channel == 'wechat':
        return configure_wechat(home,
                                enabled=options.get('enabled'),
                                token=options.get('token'),
                                clear_token=options.get('clear_token', False))
    raise ValueError('未知通道')


class ImManager:
    def __init__(self, core, cfg, home):
        self.core, self.cfg, self.home = core, cfg, Path(home)
        self.qq = None
        self.wechat = None
        self.error = ''
        self._closed = False

    # ── 状态 ────────────────────────────────────────────────
    def snapshot(self):
        # 配置写坏也不能把整个 snapshot 拖崩：桌面和手机都靠它显示状态。
        error = ''
        try:
            settings = load_im_settings(self.home)
        except Exception as failure:
            error = 'invalid_settings:' + type(failure).__name__
            settings = dict(qq=dict(enabled=False, state='invalid', ws_url='', has_token=False),
                            wechat=dict(enabled=False, state='invalid', has_token=False,
                                        bound_user='', base_url='', login_required=False))
        try:
            qq = self.qq.snapshot() if self.qq is not None else dict(**settings['qq'], state='disabled')
        except Exception as failure:
            qq = dict(enabled=False, state='error', error=type(failure).__name__)
            error = error or 'qq_snapshot_failed'
        try:
            wechat = self.wechat.snapshot() if self.wechat is not None else dict(
                **settings['wechat'], state='disabled', error='', login=None, deliveries=[])
        except Exception as failure:
            wechat = dict(enabled=False, state='error', error=type(failure).__name__)
            error = error or 'wechat_snapshot_failed'
        return dict(channels=dict(qq=qq, wechat=wechat), error=error or self.error)

    # ── 生命周期 ────────────────────────────────────────────
    async def start(self):
        await self._sync_channels()

    async def _sync_channels(self):
        settings = load_im_settings(self.home)
        if settings['qq']['enabled'] and self.qq is None:
            self.qq = CompanionQQ(self.core, self.cfg, self.home)
            await self.qq.start()
        elif not settings['qq']['enabled'] and self.qq is not None:
            await self.qq.close()
            self.qq = None
        if settings['wechat']['enabled'] and self.wechat is None:
            self.wechat = WeChatBot(self.core, self.cfg, self.home)
            await self.wechat.start()
        elif self.wechat is not None:
            # 开关可能只是被关掉，也可能是扫码后需要新的登录态；交给 restart 判断。
            self.wechat.settings = load_wechat_settings(self.home)
            await self.wechat.restart()
            if not self.wechat.settings['enabled']:
                await self.wechat.close()
                self.wechat = None

    async def apply(self, channel, options=None):
        """设置页保存后立即生效，不需要重启整个角色。"""
        options = dict(options or {})
        settings = configure_im(self.home, channel, **options)
        await self._sync_channels()
        return settings

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for channel in (self.qq, self.wechat):
            if channel is not None:
                await channel.close()
        self.qq = None
        self.wechat = None

    def emit(self, event, targets=None):
        if self._closed:
            return
        targets = list(targets or ['all'])
        if self.qq is not None and ('all' in targets or 'qq' in targets):
            self.qq.emit(event)
        if self.wechat is not None and ('all' in targets or 'wechat' in targets):
            self.wechat.emit(event)

    # ── 微信扫码 ────────────────────────────────────────────
    async def wechat_login_begin(self):
        if self.wechat is None:
            await self.apply('wechat', dict(enabled=True))
        if self.wechat is None:
            raise ValueError('微信通道没有启动')
        return await self.wechat.begin_login()

    async def wechat_login_poll(self):
        if self.wechat is None:
            raise ValueError('微信通道没有启动，先开始登录')
        result = await self.wechat.poll_login()
        if result.get('status') == 'confirmed':
            await self._sync_channels()
        return result

    async def wechat_logout(self):
        if self.wechat is not None:
            await self.wechat.logout()
        else:
            configure_wechat(self.home, clear_token=True)
        return load_im_settings(self.home)

    async def test(self, channel, text='小鱼在测试这条通道，收到就不用回复。'):
        if channel == 'wechat':
            if self.wechat is None:
                raise ValueError('微信通道没有启动')
            await self.wechat.send_text(text)
            return dict(ok=True)
        if channel == 'qq':
            if self.qq is None:
                raise ValueError('QQ 通道没有启动')
            if not getattr(self.qq, 'ws', None) or self.qq.ws.closed:
                raise ValueError('QQ 还没有连上 NapCat')
            await self.qq._rpc('send_private_msg',
                                dict(user_id=int(self.cfg.owner),
                                     message=[dict(type='text', data=dict(text=text))]))
            return dict(ok=True)
        raise ValueError('未知通道')


def safe_snapshot(manager):
    try:
        return manager.snapshot()
    except Exception as error:
        return dict(channels={}, error=type(error).__name__)
