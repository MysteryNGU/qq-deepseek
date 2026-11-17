"""Lazy local execution with owned process trees and durable, unabridged output."""
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

from .supervisor import ProcessTree
from .companion_computer import Computer
from .companion_bilibili import Bilibili
from .companion_work import Work, WorkError


class ActionExecutor:
    def __init__(self, cfg, home):
        self.cfg, self.home = cfg, Path(home).resolve()
        self._running = set()
        self._closed = False
        self._computer = Computer(self.home)
        self._bilibili = Bilibili(cfg, self.home)
        self._work = Work(cfg, self.home)
        from .companion_web import WebReader
        self.web = WebReader(self)
        from .companion_skills import SkillLibrary
        self._skills = SkillLibrary(self.home)

    def _dsh_paths(self):
        value = getattr(self.cfg, 'dsh_root', '')
        if not value:
            return None
        root = Path(value)
        if not root.is_absolute():
            root = Path(getattr(self.cfg, '_source_path', self.home/'config.toml')).parent/root
        node = Path(getattr(self.cfg, 'dsh_node', '') or shutil.which('node') or '')
        if not node.is_file() or not (root/'python/sdk/src/deepseek_harness/__init__.py').is_file():
            return None
        entry = self._dsh_entry(root)
        if not entry:
            return None
        return root.resolve(), node.resolve(), entry

    def _dsh_home(self):
        """Configured DSH home, so profiles and sessions outlive a restart."""
        value = Path(getattr(self.cfg, 'dsh_home', '') or 'data/dsh-home')
        return value if value.is_absolute() else self.home/value

    @staticmethod
    def _dsh_entry(root):
        """A built CLI needs no tsx; the source launch stays supported."""
        if (root/'lib/bin.js').is_file():
            return 'lib/bin.js'
        if ((root/'apps/cli/src/bin.ts').is_file()
                and (root/'node_modules/tsx/package.json').is_file()):
            return 'apps/cli/src/bin.ts'
        return ''

    @property
    def work_root(self):
        return str(self._work.root) if self._work.available() else ''

    def attach_repo(self, repo, agent_id):
        """接上仓储，才能回查历史动作；没接上就不对外提供 memory。"""
        from .companion_memory import Lookup
        self._memory = Lookup(repo, agent_id)

    def attach_core(self, core):
        """记下角色本身：awareness 这类运行时投影只能从角色读，不能从执行器编。"""
        self._core = core

    def _awareness_core(self):
        return getattr(self, '_core', None)

    def attach_growth(self, store):
        """成长记录只写她自己的认识，不碰人格文件。"""
        self._growth = store

    def available_kinds(self):
        kinds = ['command', 'skill'] + (['work'] if self._work.available() else [])
        if getattr(self, '_memory', None) is not None:
            kinds.append('memory')
        if getattr(self, '_growth', None) is not None:
            kinds.append('growth')
        if self._dsh_paths():
            kinds.append('dsh')
        return (['web']+kinds+(['computer'] if self._computer.available() else [])
                +(['bilibili'] if self._bilibili.available() else []))

    async def run(self, action):
        if self._closed:
            raise RuntimeError('Executor is closed')
        kind = action.get('kind')
        if kind=='web':
            return await self.web.run(action)
        if kind=='memory':
            # 读投影与控制关注是 memory 的扩展 op；其余 op 仍走原来的回查。
            from . import companion_awareness
            fields = companion_awareness._fields(action)
            if fields.get('op') in companion_awareness.READ_OPS + companion_awareness.SET_OPS:
                core = self._awareness_core() if hasattr(self, '_awareness_core') else None
                if core is not None:
                    return await companion_awareness.run(core, action,
                        state=getattr(core, '_last_awareness_context', None) or {},
                        current=getattr(core, '_last_awareness_current', None),
                        wake_kind=getattr(core, '_last_awareness_wake_kind', None))
                return dict(status='failed', op=fields.get('op'), items=[], next_cursor=None,
                            output='这一层读不到当前投影：执行器没有接到角色上，这次查询没有发生。')
            lookup = getattr(self, '_memory', None)
            if lookup is None:
                return dict(status='failed', output='回查还没有接上，这一轮取不到历史动作。')
            return await lookup.run(action)
        if kind=='growth':
            store = getattr(self, '_growth', None)
            if store is None:
                return dict(status='failed', output='成长记录还没有接上，这一轮记不了。')
            return self._growth_run(store, action)
        if kind=='bilibili':
            task=asyncio.current_task();self._running.add(task)
            try: return await self._bilibili.run(action)
            finally: self._running.discard(task)
        if kind=='computer':
            task = asyncio.current_task();self._running.add(task)
            started = time.monotonic()
            try:return await self._computer.run(action)
            except (ValueError,RuntimeError) as error:
                result = dict(status='failed',code='computer_error',output=str(error),
                              seconds=round(time.monotonic()-started,3))
                folder = self.home/'data/companion/artifacts';folder.mkdir(parents=True,exist_ok=True)
                path = folder/(uuid.uuid4().hex+'.computer-error.json')
                path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                return dict(result,output_path=str(path))
            finally:self._running.discard(task)
        if kind=='work':
            return await self._work_run(action)
        if kind=='skill':
            return await self._skills.run(action)
        if kind not in {'command', 'dsh'}:
            raise ValueError('Unsupported action kind')
        content = action.get('command' if kind == 'command' else 'goal')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('A nonempty command or goal is required')
        cwd = Path(action.get('cwd') or self.home)
        if not cwd.is_absolute():
            cwd = self.home / cwd
        if not cwd.is_dir():
            raise ValueError('Command working directory does not exist')
        folder = self.home / 'data/companion/artifacts'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (uuid.uuid4().hex + '.log')
        env = dict(os.environ)
        if kind == 'command':
            shell = (shutil.which('pwsh') or shutil.which('powershell')) if os.name=='nt' else shutil.which('sh')
            if shell is None:
                raise RuntimeError('System shell is unavailable')
            script = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                      "$ProgressPreference = 'SilentlyContinue'; "
                      "$ErrorActionPreference = 'Stop';\n" + content +
                      "\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }")
            encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
            command = [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
            if os.name!='nt': command=[shell,'-c',content]
        else:
            paths = self._dsh_paths()
            if paths is None:
                raise ValueError('DSH SDK runtime is unavailable')
            root, node, entry = paths
            request = dict(dsh_root=str(root), node=str(node), home=str(self.home), cwd=str(cwd.resolve()),
                           entry=entry, dsh_home=str(self._dsh_home()),
                           model=self.cfg.model, provider=getattr(self.cfg, 'dsh_provider', 'deepseek-official'),
                           goal=content, result_path=str(path.with_suffix('.result.json')))
            request_path = path.with_suffix('.request.json')
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
            env['DEEPSEEK_API_KEY'] = self.cfg.api_key
            endpoint = self.cfg.base_url.rstrip('/')
            env['DEEPSEEK_BASE_URL'] = endpoint+'/anthropic' if endpoint == 'https://api.deepseek.com' else endpoint
            env['DSH_PERMISSION_MODE'] = 'danger-full-access'
            env['PYTHONIOENCODING'] = 'utf-8'
            command = [sys.executable, '-B', str(Path(__file__).with_name('companion_dsh_worker.py')),
                       str(request_path)]
        task = asyncio.current_task()
        self._running.add(task)
        tree = None
        started = time.monotonic()
        try:
            with path.open('wb') as output:
                tree = ProcessTree(command, cwd=str(cwd), env=env,
                                   pipes=True, stdout_file=output, stderr_file=output)
                tree.process.stdin.close()
                code = await asyncio.to_thread(tree.process.wait)
                # Even a successful shell may have left background descendants.
                tree.stop()
                tree = None
            with path.open('rb') as output:
                preview = output.read(48000).decode('utf-8', errors='replace')[:12000]
            result = dict(status='completed' if code == 0 else 'failed', return_code=code,
                        output=preview, output_path=str(path),
                        truncated=path.stat().st_size > len(preview.encode('utf-8')),
                        seconds=round(time.monotonic()-started, 3))
            if kind == 'command':
                result['evidence_scope']='process_output'
                result['evidence_note']='这是核心主机命令的退出状态与输出。输出可能是程序生成的文字；成功退出不单独证明文字描述的经历或现实动作发生。'
            if kind == 'dsh':
                result.update(usage=None, model=self.cfg.model)
                metadata = path.with_suffix('.result.json')
                if metadata.is_file():
                    data = json.loads(metadata.read_text('utf-8'))
                    result.update({key:data.get(key) for key in ('usage','model','finish_reason','session_id')})
            return result
        finally:
            if tree is not None:
                tree.stop()
            self._running.discard(task)

    def _growth_run(self, store, action):
        """成长记录：只写她自己的认识，不改人格、不改权限。"""
        from .companion_growth import GrowthError
        op = str(action.get('op') or 'list').strip()
        raw = action.get('arguments')
        if isinstance(raw, dict):
            args = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                args = json.loads(raw)
            except ValueError:
                return dict(status='failed', evidence_scope='self_record',
                            output='growth 的 arguments 不是合法 JSON；把整个对象写成一行、键值用双引号，例如 {"kind":"interest","topic":"钓鱼","claim":"想再去海边","evidence":["..."]}')
        else:
            args = {}
        if not isinstance(args, dict):
            return dict(status='failed', evidence_scope='self_record', output='growth 的 arguments 要是一个对象。')
        try:
            if op == 'note':
                item = store.remember(str(args.get('kind') or 'interest').strip(),
                                      args.get('topic'), args.get('claim'),
                                      args.get('evidence') or [], applicability=args.get('applicability'),
                                      counter=args.get('counter'), note=str(args.get('note') or ''))
                state = {'trying': '先当临时倾向', 'proposed': '只进候选、不改人格',
                         'established': '已经算形成了'}.get(item['status'], item['status'])
                return dict(status='completed', evidence_scope='self_record', item=item,
                            output='记下了：%s｜%s（依据 %d 条，%s）。' % (
                                item['topic'], item['claim'], item['evidence_count'], state),
                            evidence_note='这是她自己对经历的解释，不是外部事实；以后有新依据可以再改。')
            if op == 'list':
                items = store.listing(kind=args.get('kind'), status=args.get('status'),
                                      scope=getattr(store, 'scope', None))
                if not items:
                    return dict(status='completed', evidence_scope='self_record',
                                output='现在还没有记下任何成长条目。', items=[])
                return dict(status='completed', evidence_scope='self_record', items=items,
                            output='现在的成长记录：\n' + '\n'.join(
                                '%s %s｜%s｜%s｜依据 %d｜%s' % (
                                    item['id'], item['kind'], item['topic'],
                                    (item['claim'] or '')[:60], item['evidence_count'], item['status'])
                                for item in items))
            if op == 'revise':
                item = store.revise(str(args.get('id') or ''), claim=args.get('claim'),
                                    status=args.get('status'), note=str(args.get('note') or ''))
                return dict(status='completed', evidence_scope='self_record', item=item,
                            output='改成：%s｜%s（%s，依据 %d 条）。' % (
                                item['topic'], item['claim'], item['status'], item['evidence_count']))
            if op == 'drop':
                item = store.drop(str(args.get('id') or ''), str(args.get('reason') or ''))
                return dict(status='completed', evidence_scope='self_record', item=item,
                            output='先放下了：%s｜%s。经历还在，只是不再按这条来。' % (item['topic'], item['claim']))
            if op == 'log':
                changes = store.changes(identity=args.get('id') or None, limit=args.get('limit') or 8)
                since = store.ledger_since()
                if since:
                    prefix = '账本从 %s 记起' % time.strftime('%m-%d %H:%M', time.localtime(since))
                else:
                    prefix = '账本这次上线才开始记，还没有任何改动'
                if not changes:
                    target = store.get(args.get('id')) if args.get('id') else None
                    if target and target['revision'] > 1:
                        return dict(status='completed', evidence_scope='self_record', changes=[],
                                    output='%s：%s 这条改过 %d 次（rev %d），'
                                           '早先的旧值没留下来，只能按现在的说法算。' % (
                                               prefix, target['topic'], target['revision'] - 1,
                                               target['revision']))
                    return dict(status='completed', evidence_scope='self_record', changes=[],
                                output='%s，这之后没有改过成长记录。' % prefix)
                lines = []
                for change in changes:
                    before, after = change.get('before') or {}, change.get('after') or {}
                    delta = '、'.join('%s：%s→%s' % (key, str(before.get(key) or '')[:24], str(after.get(key) or '')[:24])
                                      for key in ('claim', 'status') if before.get(key) != after.get(key))
                    lines.append('%s %s｜%s｜%s' % (change['id'], change['at_ago'], change['action'],
                                                    delta or change['reason'] or '（无差异记录）'))
                return dict(status='completed', evidence_scope='self_record', changes=changes,
                            output='最近的改动：\n' + '\n'.join(lines))
            if op == 'restore':
                item = store.restore(str(args.get('id') or ''), to=args.get('to') or None)
                return dict(status='completed', evidence_scope='self_record', item=item,
                            output='退回旧版本：%s｜%s（%s，rev %d）。' % (
                                item['topic'], item['claim'], item['status'], item['revision']))
            if op == 'review':
                report = store.review()
                if not report['reviewed']:
                    return dict(status='completed', evidence_scope='self_record', reviews=[],
                                output='现在没有待评审的性格候选。')
                return dict(status='completed', evidence_scope='self_record', reviews=report['results'],
                            output='候选评审（只记结论，不改人格）：\n' + '\n'.join(
                                '%s｜%s｜%s｜依据 %s 条／%s 天' % (
                                    item['id'], item['topic'], item['verdict'],
                                    item['evidence'], item['days'])
                                for item in report['results']))
            return dict(status='failed', evidence_scope='self_record',
                        output='growth 的 op 支持 note/list/revise/drop/log/restore/review，收到的是 %s' % op)
        except GrowthError as error:
            return dict(status='failed', evidence_scope='self_record', output=str(error))

    async def _work_run(self, action):
        """Typed file work; op=run delegates to the ordinary shell path."""
        op = str(action.get('op') or '').strip().lower()
        started = time.monotonic()
        if op == 'run':
            text = str(action.get('command') or action.get('goal') or '').strip()
            if not text:
                return self._work_artifact(dict(status='failed', return_code=1, op=op,
                                                code='work_error', output='run 需要 command 文本'), started)
            command = dict(action)
            command.update(kind='command', command=text)
            command.setdefault('cwd', str(self._work.root))
            result = await self.run(command)
            result['op'] = 'run'
            return result
        task = asyncio.current_task()
        self._running.add(task)
        try:
            try:
                result = await asyncio.to_thread(self._work.execute, action)
            except WorkError as error:
                return self._work_artifact(dict(status='failed', return_code=1, op=op,
                                                code='work_error', output=str(error)), started)
            except Exception as error:                     # noqa: BLE001 - surfaced to the model
                return self._work_artifact(dict(status='failed', return_code=1, op=op,
                                                code='work_error',
                                                output='%s: %s' % (type(error).__name__, error)), started)
        finally:
            self._running.discard(task)
        return self._work_artifact(result, started)

    def _work_artifact(self, result, started):
        """Work answers are re-readable later, like every other action result."""
        folder = self.home/'data/companion/artifacts'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/(uuid.uuid4().hex+'.work.json')
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        result['output_path'] = str(path)
        result['seconds'] = round(time.monotonic()-started, 3)
        result['truncated'] = len(str(result.get('output') or '')) >= 12000
        return result

    async def close(self):
        self._closed = True
        tasks = list(self._running)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._computer.close()
        await self._bilibili.close()
