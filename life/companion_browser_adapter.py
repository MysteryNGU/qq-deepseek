"""浏览器适配器：管理本机 MCP stdio 子进程，把 upstream 的结果摊成结构化回执。

放在 `life/` 而不是另建工程：这个模块是**桌面端**跑的（RemoteServiceThread 里实例化），
云端版本的文件相同，装不装 evolution-computer-use 决定它能不能用。

设计约束（见 docs/superpowers/specs/2026-11-19-evolution-computer-use-integration-design.md）：
- 长驻子进程，不每次 npx 启动
- tools/list 读真实清单，工具名与字段不写死
- MCP isError → failed，不写 completed
- 只读回放白名单在配置里，不在代码里
- 超时/断连 → unknown，不重放
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .companion_work import missing


# 只读允许的操作名。白名单之外的流程步骤不自动重放（设计 §7）。
# 这里放的是**上游工具名**，部署时要按实际 tools/list 核对。
READ_ONLY_OPS = (
    'navigate', 'goto', 'page_navigate',
    'read', 'extract', 'page_read', 'page_extract', 'page_snapshot',
    'wait', 'sleep', 'page_wait',
    'scroll', 'page_scroll',
    'procedure_run', 'procedure_list', 'procedure_get',
)

# 明确不自动重放的：提交、发送、点击、输入、任意脚本。
# click/type 不一律禁止——只读流程里的点击是必要的（展开、翻页），
# 但由 procedure 声明只读才放行，见 replay_allowed()。
WRITE_OPS = ('submit', 'send', 'post', 'click_submit', 'script', 'eval', 'exec')

# 一张截图最多落盘 32 MB：再大说明数据不对，说原因比吞掉好。
MAX_IMAGE_BYTES = 32 * 1024 * 1024
IMAGE_SUFFIX = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}


class BrowserError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class BrowserAdapter:
    """一次会话一个子进程，按工具调用逐条收发 JSON-RPC。"""

    def __init__(self, home, *, command=None, config=None):
        self.home = Path(home)
        self.config = dict(config or {})
        self.command = command or self.config.get('command')
        self.proc = None
        self.lock = asyncio.Lock()
        self.seq = 0
        self.tools = None
        self.started_at = None

    # ------------------------------------------------------------------ 生命周期

    def available(self):
        """装了没有——不是"能不能跑"。真的 ready 要 handshake 之后才知道。"""
        if not self.command:
            return False
        try:
            argv = self.command if isinstance(self.command, list) else [self.command]
            return shutil.which(argv[0]) is not None
        except Exception:
            return False

    async def start(self):
        if self.proc is not None:
            return
        argv = self.command
        if not argv:
            raise BrowserError('browser_not_installed', '本机没有配置浏览器适配器的启动命令。')
        if isinstance(argv, str):
            argv = [argv]
        env = dict(os.environ)
        env.setdefault('COMPANION_BROWSER_PROFILE', str(self.home / 'data/browser/profile'))
        env.setdefault('COMPANION_BROWSER_PROCEDURES', str(self.home / 'data/browser/procedures'))
        for key in ('COMPANION_BROWSER_PROFILE', 'COMPANION_BROWSER_PROCEDURES'):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        self.started_at = time.time()
        # 握手：拿真实工具清单，失败就如实说不可用
        await self._request('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'xiaoyu-desktop', 'version': '1'},
        })

    async def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except Exception:
            self.proc.kill()
        self.proc = None
        self.tools = None

    # ------------------------------------------------------------------ JSON-RPC

    async def _request(self, method, params=None, *, timeout=60):
        if self.proc is None or self.proc.returncode is not None:
            raise BrowserError('browser_offline', '浏览器适配器没在跑。')
        async with self.lock:
            self.seq += 1
            req_id = self.seq
            payload = {'jsonrpc': '2.0', 'id': req_id, 'method': method}
            if params is not None:
                payload['params'] = params
            try:
                self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                self.proc = None
                raise BrowserError('browser_offline', '浏览器适配器连接断了。') from None
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
            except asyncio.TimeoutError:
                # 超时按设计 §7：结果 unknown，不重放
                raise BrowserError('browser_timeout', '浏览器操作超时，结果不知道做没做到。') from None
        if not line:
            self.proc = None
            raise BrowserError('browser_offline', '浏览器适配器退出了。')
        try:
            reply = json.loads(line.decode('utf-8', 'replace'))
        except ValueError:
            raise BrowserError('browser_bad_reply', '浏览器适配器返回的不是 JSON。') from None
        if 'error' in reply:
            raise BrowserError('browser_error', str(reply['error'].get('message') or reply['error']))
        return reply.get('result')

    async def list_tools(self, refresh=False):
        if self.tools is not None and not refresh:
            return self.tools
        await self.start()
        result = await self._request('tools/list', {})
        tools = (result or {}).get('tools') or []
        self.tools = tools
        return tools

    # ------------------------------------------------------------------ op 入口

    async def run(self, action):
        """action 是云端的 browser action：{op, arguments, project, goal}。

        回执过执行器的统一出口（``annotate_receipt``）：来源分级与缺口只在那一份
        实现里；这个模块不再自己补一套（t12 撞到过两份实现会漂移）。
        """
        from .companion_executor import annotate_receipt
        return annotate_receipt('browser', action, await self._dispatch(action))

    async def _dispatch(self, action):
        op = action.get('op')
        raw = action.get('arguments') or '{}'
        try:
            fields = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except ValueError:
            return dict(status='failed', code='bad_arguments',
                        output='arguments 不是合法 JSON，这次没有调用浏览器。')
        try:
            if op == 'tools':
                tools = await self.list_tools(refresh=True)
                return dict(status='completed', op='tools', count=len(tools),
                            tools=[dict(name=t.get('name'), description=(t.get('description') or '')[:400],
                                        schema=t.get('inputSchema') or {}) for t in tools],
                            output='本机浏览器适配器提供 %d 个工具。' % len(tools))
            if op == 'call':
                return await self._call(fields)
            return dict(status='failed', code='bad_op', output='不认识的 browser op：%r' % (op,))
        except BrowserError as error:
            return self._failure(error)

    async def _call(self, fields):
        name = str(fields.get('tool') or '').strip()
        if not name:
            return dict(status='failed', code='missing_tool', output='没有给 tool。先用 op=tools 看有哪些。')
        tools = await self.list_tools()
        known = {t.get('name'): t for t in tools}
        if name not in known:
            return dict(status='failed', code='unknown_tool',
                        output='这台机器的浏览器适配器里没有 %r。现有：%s' % (
                            name, '、'.join(sorted(k for k in known if k))[:300]))
        args = fields.get('input') or {}
        if not isinstance(args, dict):
            return dict(status='failed', code='bad_input', output='input 要是对象。')
        started = time.time()
        try:
            result = await self._request('tools/call', {'name': name, 'arguments': args},
                                         timeout=float(self.config.get('call_timeout', 180)))
        except BrowserError as error:
            return self._failure(error, tool=name, seconds=time.time() - started)
        return self._normalize(name, result, time.time() - started)

    def _normalize(self, tool, result, seconds):
        """MCP 回执 → 统一结构。isError 一律 failed，不写 completed。

        截图必须落成真实文件并给出路径：只回一句「收到 N 字节图片」，
        她依然看不见那张图，等于把「确实看到过」换成「知道有东西」。
        """
        content = (result or {}).get('content') or []
        texts, images = [], []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'text':
                texts.append(str(item.get('text') or ''))
            elif item.get('type') == 'image':
                images.append(self._save_image(item))
        is_error = bool((result or {}).get('isError'))
        structured = (result or {}).get('structuredContent')
        # outward_done 只能来自结构化字段，不能从一句"完成。"里猜（设计 §6）
        outward = structured.get('outward_done') if isinstance(structured, dict) else None
        failed_step = structured.get('failed_step') if isinstance(structured, dict) else None
        version = structured.get('procedure_version') if isinstance(structured, dict) else None
        text = '\n'.join(texts)[:4000]
        notes = []
        unsaved = [item for item in images if not item.get('saved')]
        if unsaved:
            notes.append('有图片但没有落盘：'
                         + '；'.join(str(item.get('reason') or '没有落盘') for item in unsaved))
        if not images:
            notes.append('这次工具没有返回截图/图片（只有文本，没有画面）。')
        # 只交出「这一项没拿到」的事实；汇总成 not_obtained、写 summary、
        # 判断截图在云端读不读得到，都归执行器的统一出口（annotate_receipt）。
        gaps = [dict(item['gap']) for item in images if isinstance(item.get('gap'), dict)]
        if not images:
            gaps.append(missing('页面截图', 'not_requested',
                                '这个工具这次没有返回截图（只返回了文本）'))
        output = '\n'.join([text] + notes).strip()
        return dict(
            status='failed' if is_error else 'completed',
            tool=tool,
            seconds=round(seconds, 3),
            text=text,
            images=images,
            image_paths=[str(item.get('path')) for item in images if item.get('saved')],
            structured=structured,
            outward_done=outward,
            failed_step=failed_step,
            procedure_version=version,
            verification='unknown',   # 由上层按任务类型判定（设计 §6）
            not_obtained=gaps,
            output=(output[:1200] or ('%s 返回了结构化结果。' % tool)),
        )

    def _save_image(self, item):
        """把 MCP 的一张图落盘，返回真实路径；落不下来就说原因，不只说字节数。

        这里只写它自己知道的事实：saved / path / mime / bytes，以及没落盘时的
        ``gap``（哪一层原因）。「这张图云端读不读得到」由执行器按真实可读性判定，
        这个进程说了不算。
        """
        mime = str(item.get('mimeType') or 'image/png')
        encoded = item.get('data')

        def failed(reason, layer='failed'):
            return dict(mime=mime, saved=False, reason=reason,
                        gap=missing('页面截图', layer, reason))

        if not isinstance(encoded, str) or not encoded:
            return failed('回执里没有图片数据')
        if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
            return failed('图片数据超过 32MB，没有落盘', 'limit')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return failed('图片数据不是合法 base64，没有落盘')
        if not raw:
            return failed('图片数据为空，没有落盘')
        folder = self.home / 'data/companion/artifacts/browser'
        suffix = IMAGE_SUFFIX.get(mime, 'img')
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (uuid.uuid4().hex + '.' + suffix)
            path.write_bytes(raw)
        except OSError as error:
            return failed('写入失败：' + type(error).__name__)
        return dict(mime=mime, saved=True, path=str(path), bytes=len(raw),
                    coordinate_space='页面像素（由浏览器工具决定，可能已缩放）')

    def _failure(self, error, **extra):
        # 超时/断连 = 不知道做没做到，标 unknown（设计 §7）
        status = 'unknown' if error.code in ('browser_timeout',) else 'failed'
        result = dict(status=status, code=error.code, output=error.message, **extra)
        if status == 'unknown':
            # 「不知道做没做到」也是一个没取得的事实；failed 由执行器统一补。
            result['not_obtained'] = [missing('浏览器操作的结果', 'unavailable', error.message,
                                              code=error.code)]
        return result
