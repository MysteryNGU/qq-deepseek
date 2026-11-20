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
from .companion_work import (PROVENANCE_DIRECT, PROVENANCE_MODEL, PROVENANCE_TOOL, Work,
                             WorkError, failed_receipt, missing, provenance, visibility,
                             work_failure_gap)

log = __import__('logging').getLogger(__name__)

# 这一层是**所有工具回执的出口**，所以来源分级和「没看见什么」的默认值在这里定：
# 回执正文是谁给的（provenance），以及这次哪些东西没有取得（visibility.not_obtained）。
# 各工具模块可以自己写得更细；这里只保证「一条回执绝不会只剩一个 status」。
# computer/bilibili 的主来源不写死：annotate_receipt 会按「这次是不是真的采到了
# 直接观察」动态定（可读截图/界面树、可读画面文件），默认先按工具报告算。
_KIND_PROVENANCE = {
    # 本机直接采集到的原始内容
    'command': PROVENANCE_DIRECT, 'work': PROVENANCE_DIRECT,
    'web': PROVENANCE_DIRECT,
    # computer/bilibili 由 annotate_receipt 动态判定，缺直接观察证据时按工具报告
    'computer': PROVENANCE_TOOL, 'bilibili': PROVENANCE_TOOL,
    # 执行器/工具自己的报告
    'memory': PROVENANCE_TOOL, 'growth': PROVENANCE_TOOL, 'ask': PROVENANCE_TOOL,
    'task': PROVENANCE_TOOL, 'skill': PROVENANCE_TOOL,
    # 手机回执是「手机回了什么话」；画面/界面树由 observation 逐项标 direct_observation。
    'phone': PROVENANCE_TOOL, 'browser': PROVENANCE_TOOL,
    # 另一个模型说的话
    'dsh': PROVENANCE_MODEL, 'agent': PROVENANCE_MODEL,
}


def _readable_file(path):
    """这个进程能不能真的读到这个文件（判定标准就是能不能打开读到一个字节）。

    浏览器截图落盘发生在桌面主机上；云端进程拿到的只是一个路径。路径存在不等于
    云端可读，所以每个图片路径都按「本进程真读一次」判定，不靠猜。
    """
    try:
        candidate = Path(str(path or ''))
        if not candidate.is_file():
            return False
        with candidate.open('rb') as handle:
            handle.read(1)
        return True
    except (OSError, ValueError):
        return False


def _phone_gap_layer(code, default='failed'):
    """手机回执的码 → t11 的六层原因；映射表只有 companion_phone 一份。"""
    try:
        from .companion_phone import PhoneControl
        return PhoneControl._gap_layer(code, default=default)
    except Exception:  # noqa: BLE001 - 拿不到映射不改变「写不出原因」这个事实
        return default


def _computer_observation(result):
    """computer 的两件**直接观察**各自取到没有：本进程可读的截图 / 界面或窗口树。

    只有「有路径」不算：桌面/远端写下的文件本进程打不开时，那一项就不是她亲眼
    看到的画面，parts 里也不能再标 direct_observation。返回 ``(screenshot, ui_tree)``。
    """
    image = str(result.get('image_path') or '')
    screenshot = bool(image) and _readable_file(image)
    computer = result.get('computer') if isinstance(result.get('computer'), dict) else {}
    ui_tree = any(isinstance(computer.get(key), (list, dict)) and computer.get(key)
                  for key in ('nodes', 'windows', 'tree'))
    return screenshot, bool(ui_tree)


def _computer_visual(result):
    """computer 这次有没有真的采到直接观察（截图或界面树任一）。"""
    screenshot, ui_tree = _computer_observation(result)
    return (screenshot or ui_tree), ('screenshot' if screenshot else ('ui_tree' if ui_tree else ''))


def _bilibili_visual(result):
    """B站这次有没有真的看到画面：visual.observed 为真，且画面文件本进程可读。"""
    visual = result.get('visual') if isinstance(result.get('visual'), dict) else {}
    if not visual.get('observed'):
        return False
    path = str(visual.get('sheet_path') or '')
    return bool(path) and _readable_file(path)


def _command_gaps(action, result, add):
    """命令只有退出码和输出是被观察到的；没跑完/没输出要写出来。"""
    if result.get('status') == 'failed':
        tail = str(result.get('output') or '').strip()
        add(missing('命令的成功结果', 'failed',
                    ('退出码 %s：%s' % (result.get('return_code'), tail[:300])) if tail
                    else ('进程退出码 %s，但没有任何输出' % result.get('return_code'))))
    if not result.get('executed_script'):
        add(missing('实际执行的脚本', 'unavailable',
                    '这次没有留下包装后的脚本（写入失败或走的是别的分支）'))


def _computer_gaps(action, result, add):
    op = str(action.get('op') or '')
    image = str(result.get('image_path') or '')
    if image:
        if _readable_file(image):
            result['image_readable'] = True
            return
        # 有路径但本进程打不开：路径留着，画面没取得——不能当成看到了。
        result['image_readable'] = False
        add(missing('屏幕画面', 'unavailable',
                    '截图路径这次读不到：%s（本进程打不开这个文件；路径仍留在这里，但没有画面）'
                    % image))
        return
    if op in {'screenshot', 'zoom'}:
        if result.get('status') == 'failed':
            add(missing('屏幕画面', 'unavailable',
                        str(result.get('output') or '')[:300] or '截图没有返回图像'))
        else:
            add(missing('屏幕画面', 'failed',
                        '引擎返回了，但这次回执里没有图像路径（可能只返回了坐标/状态）；'
                        '要画面就再截图一次。'))
    elif op:
        add(missing('屏幕画面', 'not_requested',
                    'op=%s 本来就不取画面；要看画面用 op=screenshot' % op))


def _phone_gaps(action, result, add):
    """手机：界面的两套观察各自记账；没走到取画面那一步时兜底写一条。

    手机模块在 `observation.screenshot` / `observation.ui_tree` 里已经算好每一项
    取没取到、为什么（含「workspace_open 不取画面」这种具体原因）。这里优先采信
    它，只补它没覆盖的路径——不另造一套判断。
    """
    observation = result.get('observation')
    if isinstance(observation, dict):
        for key in ('screenshot', 'ui_tree'):
            part = observation.get(key)
            if not isinstance(part, dict):
                continue
            gap = part.get('gap')
            if isinstance(gap, dict):
                add(gap)
            path = part.get('path')
            if part.get('obtained') and path and not _readable_file(path):
                # 有路径但本进程读不到：这不是「拿到画面了」。
                part.update(obtained=False, path=str(path),
                            reason='截图文件不在本进程可读的位置：%s' % path)
                add(missing('截图', 'unavailable',
                            '截图文件不在本进程可读的位置：%s（云端读不到）' % path))
        return
    if str(result.get('status') or '') != 'completed':
        code = str(result.get('code') or '')
        add(missing('屏幕画面', _phone_gap_layer(code, default='unavailable'),
                    str(result.get('output') or '')[:200] or '这次动作没有回执', code=code))


def _rewrite_browser_image_lines(result):
    """把「截图已保存」那几行按真实可读性重写：路径可不可读必须写在回执正文里。"""
    output = str(result.get('output') or '')
    kept = [line for line in output.splitlines()
            if not line.startswith(('截图已保存', '云端副本：', '截图在桌面端'))]
    readable, desktop = [], []
    for item in (result.get('images') or []):
        if not isinstance(item, dict):
            continue
        if item.get('saved') and item.get('cloud_readable'):
            readable.append(str(item.get('path') or ''))
        elif item.get('desktop_path'):
            desktop.append(item)
    if readable:
        kept.append('截图已保存（本进程可读）：' + '、'.join(readable))
    for item in desktop:
        kept.append('截图在桌面端，云端读不到：%s（%s）'
                    % (item.get('desktop_path'),
                       item.get('reason') or '这个路径在本进程读不到'))
    result['output'] = '\n'.join(line for line in kept if line).strip()[:2400]


def _browser_gaps(action, result, add):
    """浏览器截图：路径必须本进程真的能读；读不到就说清「截图在桌面端」。

    判定标准就是「云端进程用这个路径能不能读到文件」：能读到才写「已保存」，
    读不到就标 saved=False + 桌面路径 + 原因，并在回执正文里重写这几行——
    不许静默返回一个下游打不开的路径。
    """
    images = result.get('images')
    images = [item for item in images if isinstance(item, dict)] if isinstance(images, list) else []
    if not images and isinstance(result.get('image_paths'), list):
        # 适配器/老版本只给了路径清单：按同一条判定（本进程能不能读）处理。
        images = [dict(saved=True, path=str(path)) for path in result['image_paths'] if path]
    unreadable = []
    for item in images:
        if item.get('desktop_path'):
            unreadable.append(item)      # 云端采纳时已标明「桌面端有、没回传」
            continue
        if not item.get('saved'):
            continue
        path = str(item.get('path') or '')
        if _readable_file(path):
            item['cloud_readable'] = True
            continue
        item.update(saved=False, cloud_readable=False, desktop_path=path,
                    reason='截图在桌面端（%s），云端进程读不到这个路径' % path)
        unreadable.append(item)
    if images:
        result['images'] = images
        result['image_paths'] = [str(item.get('path')) for item in images if item.get('saved')]
    elif str(result.get('status') or '') == 'failed':
        add(missing('浏览器操作的结果', 'failed',
                    str(result.get('output') or '')[:200] or '这次操作没有回执'))
    if unreadable:
        add(missing('页面截图', 'unavailable',
                    '截图在桌面端，云端读不到：'
                    + '；'.join('%s（%s）' % (item.get('desktop_path'),
                                             item.get('reason') or '这个路径在本进程读不到')
                               for item in unreadable)
                    + '。要看到画面，需要运行桌面端的那一侧升级到会回传图片字节的版本'
                      '（旧版本只交回本机路径）'))
    _rewrite_browser_image_lines(result)


def _bilibili_gaps(result, add):
    """B站画面：reader 自己的缺口优先；它没说原因时才记「没给原因」，不推断不可用。"""
    visual = result.get('visual')
    if isinstance(visual, dict) and visual.get('observed'):
        # reader 说看到了画面：再按「本进程能不能读到那个文件」核一次。
        path = str(visual.get('sheet_path') or '')
        if not (path and _readable_file(path)):
            add(missing('视频画面', 'unavailable',
                        'reader 说画面已取得，但这个文件本进程读不到：%s' % (path or '（没有给路径）')))
        return
    if isinstance(visual, dict) and not visual.get('observed'):
        if any(isinstance(item, dict) and str(item.get('what')) == '视频画面'
               for item in (result.get('not_obtained') or [])):
            return   # reader 已经写清楚了，语义与分层都以它为准
        code = str(visual.get('error') or visual.get('code') or result.get('code') or '')
        reason_text = str(visual.get('reason') or '')
        lowered = code.lower()
        if any(word in lowered for word in ('login', 'auth', 'cookie', 'credential')):
            reason = 'unauthorized'
        elif 'limit' in lowered or 'size' in lowered:
            reason = 'limit'
        elif lowered.startswith('visual_read_'):
            reason = 'failed'
        elif code and 'unavailable' in lowered:
            reason = 'unavailable'
        elif visual.get('requested') is False:
            reason = 'not_requested'
        else:
            # 没有码、没有原因：这是「上游没给原因」，不是「不可用」。
            reason = 'unspecified'
        detail = reason_text or ('这次没有取得画面帧（%s）' % code if code
                                 else 'reader 没有给出画面没取得的原因（不推断为不可用）')
        add(missing('视频画面', reason, detail, code=code))
    elif visual is None and (result.get('status') == 'failed'
                             or str(result.get('completion') or '') in {'not_read', 'partial'}):
        add(missing('B站的内容', 'failed', str(result.get('output') or '')[:300]))


def _dsh_gaps(result, add):
    """dsh：它说的话不等于做成了；没写出来的事实逐项说清。"""
    if result.get('status') == 'failed':
        reason = (result.get('turn_reason') or result.get('failure_kind')
                  or result.get('output') or '')
        add(missing('dsh 这一轮的结果', 'failed', str(reason).strip()[:400]))
    if result.get('failure_kind') in {'no_output', 'no_session'}:
        add(missing('会话文件或输出', 'unavailable',
                    'dsh 进程没有写出这一轮的会话与输出（%s）' % result.get('failure_kind')))
    if not result.get('session_id'):
        add(missing('session_id', 'unavailable',
                    'worker 没有写出会话号（result.json 缺失或没带），没法按会话回查这一轮'))
    if result.get('finish_reason') in (None, ''):
        add(missing('finish_reason', 'unavailable',
                    'worker 没有写出 finish_reason（result.json 缺失或没带）'))


def _agent_gaps(result, add):
    finished = result.get('completion') == 'finished' or 'delivery' in result
    if not finished:
        return
    delivery = str(result.get('delivery') or '')
    if delivery.startswith('not_recorded') or not result.get('message_id'):
        add(missing('子 agent 回执的送达', 'unavailable',
                    '结果留在台账里，但这次没有登记进任何收件账本（%s）'
                    % (delivery or '没有 delivery 字段')))
    if not result.get('output_path') and not result.get('artifacts'):
        add(missing('子 agent 的产物', 'not_requested',
                    '这次没有交付物路径；它只交回了说法，读 output 时别当成已完成的证据'))


def annotate_receipt(kind, action, result):
    """给回执补上「内容是谁给的」与「这次没看见什么」。

    这是**唯一**的组装实现：``ActionExecutor.run`` 与 ``RemoteExecutor.run`` 的
    旁路（phone/browser/computer）都走这里；工具模块不再各写一套标注。

    三类来源不混写：回执正文的 ``provenance`` 只写一个主来源；一条回执里
    确实混了别的来源时，用 ``provenance_parts`` 逐项标出来。
    ``visibility.not_obtained`` 逐项写这次没取得的东西和分层原因
    （未授权 / 不可用 / 失败 / 超限 / 这次没要求 / 环境不支持 / 没给原因）。
    """
    if not isinstance(result, dict):
        return result
    action = action if isinstance(action, dict) else {}
    if 'provenance' not in result:
        primary = _KIND_PROVENANCE.get(kind, PROVENANCE_TOOL)
        parts = {'status': PROVENANCE_TOOL}
        if kind == 'computer':
            # 有本进程可读的截图、或拿到了界面/窗口树，才算她亲眼看到的东西；
            # 否则这条回执只是工具自己的报告。parts 只列**真取到**的那几项。
            screenshot, ui_tree = _computer_observation(result)
            primary = PROVENANCE_DIRECT if (screenshot or ui_tree) else PROVENANCE_TOOL
            parts = {'status': PROVENANCE_TOOL}
            if screenshot:
                parts['screenshot'] = PROVENANCE_DIRECT
            if ui_tree:
                parts['ui_tree'] = PROVENANCE_DIRECT
        elif kind == 'bilibili':
            # 画面对上了（observed 且文件可读）才是直接观察；只有字幕就是工具报告。
            visual_ok = _bilibili_visual(result)
            primary = PROVENANCE_DIRECT if visual_ok else PROVENANCE_TOOL
            parts = {'subtitle': PROVENANCE_TOOL, 'status': PROVENANCE_TOOL}
            if visual_ok:
                parts['visual'] = PROVENANCE_DIRECT
            visual = result.get('visual')
            if isinstance(visual, dict):
                # 子项也要如实：没取得画面的 visual 块不能挂着 direct_observation。
                visual['provenance'] = PROVENANCE_DIRECT if visual_ok else PROVENANCE_TOOL
        elif kind == 'browser':
            # 截图字节本进程可读时，图片是直接观察（文本仍是工具报告）。
            images = result.get('images') if isinstance(result.get('images'), list) else []
            saved = any(isinstance(item, dict) and item.get('saved')
                        and _readable_file(item.get('path')) for item in images)
            parts = {'text': PROVENANCE_TOOL, 'status': PROVENANCE_TOOL}
            if saved:
                parts['images'] = PROVENANCE_DIRECT
        elif kind == 'agent' and result.get('completion') != 'finished':
            # create/inspect/wait/cancel/resume 这些是台账视图，不是哪个模型的转述。
            primary = PROVENANCE_TOOL
        result.update(provenance(
            primary, source='executor.%s' % (kind or '?'), parts=parts))
    if kind == 'phone' and isinstance(result.get('observation'), dict):
        # 手机回执是「手机回了什么话」；屏幕本身是两套独立观察，各自标直接观察。
        result['provenance_parts'] = dict(result.get('provenance_parts') or {},
                                          receipt=PROVENANCE_TOOL,
                                          screenshot=PROVENANCE_DIRECT,
                                          ui_tree=PROVENANCE_DIRECT)
    visible = result.get('visibility') if isinstance(result.get('visibility'), dict) else {}
    existing = result.get('not_obtained') or visible.get('not_obtained') or []
    gaps = [item for item in existing if isinstance(item, dict)]
    seen = {str(item.get('what')) for item in gaps}

    def add(item):
        if str(item.get('what')) in seen:
            return
        seen.add(str(item.get('what')))
        gaps.append(item)

    try:
        if kind == 'command':
            _command_gaps(action, result, add)
        elif kind == 'computer':
            _computer_gaps(action, result, add)
        elif kind == 'phone':
            _phone_gaps(action, result, add)
        elif kind == 'browser':
            _browser_gaps(action, result, add)
        elif kind == 'bilibili':
            _bilibili_gaps(result, add)
        elif kind == 'dsh':
            _dsh_gaps(result, add)
        elif kind == 'agent':
            _agent_gaps(result, add)
    except Exception:  # noqa: BLE001 - 标注缺口不许把真回执弄没
        log.warning('回执缺口标注失败 kind=%s', kind, exc_info=True)
    # 失败的回执绝不能自称「没有缺口」。agent 的台账视图里 status 是那件任务的：
    # 那就不说「这次的结果」，而说「那件任务的结果」——语义仍然一致（没取得好结果）。
    ledger_view = kind == 'agent' and result.get('completion') != 'finished'
    failed = failed_receipt(result)
    if failed and not gaps:
        detail = (str(result.get('error') or '') or str(result.get('output') or '')
                  or str(result.get('code') or '') or 'status=%s' % result.get('status'))
        what = '那件任务的结果' if ledger_view else '这次的结果'
        gaps.append(missing(what, 'failed', detail.strip()[:300] or '没有给出原因'))
        seen.add(what)
    truncated = list(visible.get('truncated') or [])
    note = visible.get('note') or ''
    if gaps or 'visibility' not in result or failed:
        result['visibility'] = visibility(truncated=truncated,
                                          skipped=visible.get('skipped') or [],
                                          not_obtained=gaps, note=note, failed=failed)
    # 缺口形状固定：成功的回执也带一个空列表，读的人不用猜「没有这个键」是什么意思。
    result['not_obtained'] = gaps
    return result


def failure_receipt(kind, error):
    """连工具都没跑到：失败原因也是回执，同样带来源与缺口。"""
    result = dict(status='failed', kind=kind,
                  code=getattr(error, 'code', type(error).__name__),
                  output='这次行动没有被执行：%s' % str(error)[:400])
    result.update(provenance(PROVENANCE_TOOL, source='executor.%s' % (kind or '?'),
                             note='这是执行器拒绝或失败的原因，不是任何观察结果。'))
    gaps = [missing('这次行动的结果', 'failed', str(error)[:300])]
    result['not_obtained'] = gaps
    result['visibility'] = visibility(not_obtained=gaps, failed=True)
    return result


async def run_with_receipt(kind, action, invoke):
    """跑一次 ``invoke()`` 并过统一出口；异常也变成带来源的回执。"""
    try:
        result = await invoke()
    except Exception as error:  # noqa: BLE001 - 参数错也要给带来源的回执
        return failure_receipt(kind, error)
    return annotate_receipt(kind, action, result)


def _dsh_session_written(dsh_home, since):
    """这次运行有没有真的写出会话文件。

    DSH 每轮都会在 sessions 下写 session.v4.jsonl。
    没有新文件 = 什么都没跑，不管退出码是多少。
    """
    import os as _os
    try:
        base = Path(dsh_home) / 'sessions'
        if not base.is_dir():
            return False
        for root, _dirs, files in _os.walk(str(base)):
            for name in files:
                if not name.endswith('.jsonl'):
                    continue
                try:
                    if _os.path.getmtime(_os.path.join(root, name)) >= since - 1:
                        return True
                except OSError:
                    continue
        return False
    except Exception:
        # 查不了不等于没跑：返回 True 让主判据（输出非空）继续起作用，
        # 免得因为一个路径问题把真结果判成失败。
        return True


class ActionExecutor:
    def __init__(self, cfg, home):
        self.cfg, self.home = cfg, Path(home).resolve()
        self._running = set()
        self._closed = False
        self._computer = Computer(self.home)
        self._bilibili = Bilibili(cfg, self.home)
        self._work = Work(cfg, self.home)
        # P40: 后台进程登记表。长命令走这里，不再被同步超时丢掉。
        from .companion_background import BackgroundJobs
        self._background = BackgroundJobs(self.home)
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
        """接上仓储，才能回查历史动作；没接上就不对外提供 memory。

        ``repo``/``agent_id`` 同时留给任务清单（P40 ②）：它读写的是同一份
        项目记录，没有另一套存储。
        """
        from .companion_memory import Lookup
        self._memory = Lookup(repo, agent_id)
        self.repo = repo
        self.agent_id = agent_id

    def attach_core(self, core):
        """记下角色本身：awareness 这类运行时投影只能从角色读，不能从执行器编。"""
        self._core = core

    def _awareness_core(self):
        return getattr(self, '_core', None)

    def attach_agents(self, manager):
        """接上子 agent 台账（角色构造时调用）。执行器只借用，不另建。"""
        self._agents = manager
        return self

    def _agents_manager(self):
        """子 agent 台账：优先用 attach 进来的那一份，其次从角色上取。"""
        manager = getattr(self, '_agents', None)
        if manager is not None:
            return manager
        core = self._awareness_core()
        return getattr(core, 'agents', None) if core is not None else None

    def attach_growth(self, store):
        """成长记录只写她自己的认识，不碰人格文件。"""
        self._growth = store

    def available_kinds(self):
        kinds = ['command', 'skill'] + (['work'] if self._work.available() else [])
        if getattr(self, '_memory', None) is not None:
            kinds.append('memory')
        if getattr(self, '_core', None) is not None:
            # 提问依附角色，没有角色就无从挂起，不列出来免得她看见用不了的
            kinds.append('ask')
        if getattr(self, 'repo', None) is not None:
            # 任务清单和 memory 一样只依赖台账；台账没接上就不列出来，
            # 免得她看见一个用不了的工具。
            kinds.append('task')
        if getattr(self, '_growth', None) is not None:
            kinds.append('growth')
        if self._dsh_paths():
            kinds.append('dsh')
        return (['web']+kinds+(['computer'] if self._computer.available() else [])
                +(['bilibili'] if self._bilibili.available() else [])
                +(['browser'] if getattr(self, '_browser_ready', False) else []))

    async def run(self, action):
        """跑一件动作，并把回执的来源与「没看见」的部分补齐。

        这是唯一出口：``_dispatch`` 干具体的事，``run_with_receipt`` 保证每条回执
        都带 ``provenance`` 和 ``visibility``——以前一条回执只有一个 status，
        她只能看到「completed」，看不到是谁说的、也没看到什么。
        """
        if self._closed:
            raise RuntimeError('Executor is closed')
        kind = str((action or {}).get('kind') or '')
        return await run_with_receipt(kind, action, lambda: self._dispatch(action))

    async def _dispatch(self, action):
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
        if kind=='ask':
            # P40 ③：结构化提问。状态挂在角色上（core._state），不弹窗、不发消息：
            # 程序只登记问题，话由她自己说；主人下一条消息就是答案。
            core = self._awareness_core()
            if core is None:
                return dict(status='failed', code='ask_not_wired', kind='ask',
                            output='提问没有接到角色上，这次没有记下任何问题。')
            from .companion_ask import PendingQuestions
            return self._ask_run(PendingQuestions(core), action)
        if kind=='task':
            # P40 ②：任务清单。**读写的是现有项目记录**（meta 的 action_task 行），
            # 不另建表——另建就会立刻出现两份互相矛盾的待办。
            from .companion_tasks import TaskList
            repo = getattr(self, 'repo', None)
            if repo is None:
                return dict(status='failed', code='task_not_wired', kind='task',
                            output='任务清单没有接到台账上，这次没记下任何东西。')
            scope = getattr(getattr(self, '_core', None), 'scope', None)
            items = TaskList(repo, getattr(self, 'agent_id', ''),
                             getattr(scope, 'scope_id', '') if scope is not None else '')
            return self._task_run(items, action)
        if kind=='skill':
            return await self._skills.run(action)
        if kind=='agent':
            # 子 agent：七操作全在台账里（life/companion_agents.py），这里只做分派。
            # 没有接上角色时明说「没发生」，不猜结果。
            manager = self._agents_manager()
            if manager is None:
                return dict(status='failed', code='agent_not_wired', kind='agent',
                            output='子 agent 台账还没有接上角色，这次没有发生任何事。')
            try:
                return await manager.dispatch(action)
            except Exception as error:  # noqa: BLE001 - 拒绝原因要原样回给她
                code = getattr(error, 'code', type(error).__name__)
                return dict(status='failed', code=code, kind='agent',
                            reason=str(getattr(error, 'message', '') or error)[:600],
                            output='这次子 agent 操作没有被执行：%s'
                                   % str(getattr(error, 'message', '') or error)[:400])
        if kind == 'command':
            # P40 ①：op=start/status/wait/stop/logs 走后台任务，不阻塞这一轮。
            # 没有 op 时保持原来的同步行为，一字不改。
            fields = action.get('arguments')
            if isinstance(fields, str) and fields.strip():
                try:
                    fields = json.loads(fields)
                except ValueError:
                    fields = {}
            fields = fields if isinstance(fields, dict) else {}
            op = str(action.get('op') or fields.get('op') or '').strip().lower()
            if op in self._background.OPS:
                return await self._background_run(action, op, fields)
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
        script_path = None
        wrapper = {}
        if kind == 'command':
            shell = (shutil.which('pwsh') or shutil.which('powershell')) if os.name=='nt' else shutil.which('sh')
            if shell is None:
                raise RuntimeError('System shell is unavailable')
            script = self._wrapped_script(content)
            # 真正执行的是这段包装后的脚本（A′），不是她写的那条命令（A）。
            # 以前只有她才写的那条命令和退出码回给她，A′ 从来没露过面——
            # 「我说的是 A，跑的是 A′」这件事只存在于代码里。
            script_path = path.with_suffix('.script.ps1' if os.name == 'nt' else '.script.sh')
            try:
                script_path.write_text(script, encoding='utf-8')
            except OSError:
                script_path = None
            if os.name == 'nt':
                encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
                command = [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
                wrapper = dict(
                    shell=shell, transport='-EncodedCommand（base64 编码的 UTF-16LE 脚本）',
                    prefixes=["[Console]::OutputEncoding = UTF8（无 BOM）",
                              "$ProgressPreference = 'SilentlyContinue'",
                              "$ErrorActionPreference = 'Stop'（出错即停）"],
                    appended=['if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }'
                              '（把她那条命令的退出码补成进程退出码）'],
                    note='真正执行的是 executed_script 与 executed_script_path 里的脚本，'
                         '不是她写的那一行；上面逐项写着加了什么。')
            else:
                command = [shell, '-c', content]
                script = content
                wrapper = dict(shell=shell, transport='-c', prefixes=[], appended=[],
                               note='真正执行的是 sh -c 后面那段命令本身（executed_script 就是它）。')
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
            # 2026-11-18 实测：sdk-minimal profile 只加载 dsh-llm-deepseek-api-key，
            # 那个包只读 DEEPSEEK_API_KEY，**完全不读 DEEPSEEK_BASE_URL**。
            # 以前这里把 BASE_URL 注入成主模型地址（opencode），adapter 却不理它，
            # 于是拿 opencode 的 key 去打 DeepSeek 官方端点 → 必然失败，
            # 而错误消息还说"检查模型设置"，把人引向错误方向。
            # **注入一个没人读的变量比不注入更糟**，所以不再注入。
            env['DEEPSEEK_API_KEY'] = self.cfg.api_key
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
            elapsed = round(time.monotonic()-started, 3)
            output_bytes = path.stat().st_size
            truncated = output_bytes > len(preview.encode('utf-8'))
            result = dict(status='completed' if code == 0 else 'failed', return_code=code,
                        output=preview, output_path=str(path),
                        truncated=truncated, output_bytes=output_bytes,
                        output_chars=len(preview), seconds=elapsed)
            if truncated:
                result['visibility'] = visibility(truncated=[dict(
                    what='命令输出', reason='limit', reason_text='超出上限',
                    detail='输出共 %d 字节，这次只带回前 %d 字节（12000 字符）；完整输出在 output_path'
                           % (output_bytes, len(preview.encode('utf-8'))),
                    output_bytes=output_bytes, shown_chars=len(preview))])
            if kind == 'dsh':
                # dsh 的定义就是「必须产出一句话」。退出码 0 + 空输出在语义上不可能是成功：
                # 2026-11-18 实测过一次假成功——worker 文件被截断、入口块没了，
                # 进程 exit 0、0.066 秒返回、什么都没做，而状态是 completed。
                # 三条都满足才算完成：退出码 0、有输出、真的产生了会话文件。
                session_new = _dsh_session_written(self._dsh_home(), started)
                if code == 0 and not preview.strip():
                    result.update(
                        status='failed', failure_kind='no_output',
                        output='\ndsh 进程正常退出（rc=0），但 %.3f 秒就返回、没有任何输出，'
                               '也没有产生会话文件。\n这说明它根本没启动——'
                               '不是做完了一件事，是没做任何事。\n' % elapsed)
                elif code == 0 and not session_new:
                    result.update(
                        status='failed', failure_kind='no_session',
                        output='\ndsh 进程退出码 0（%.3f 秒），有些输出，'
                               '但会话目录里没有新文件。\n'
                               '有输出不等于跑成了：那可能是启动日志，不是这一轮的结果。\n'
                               % elapsed)
            if kind == 'command':
                result['evidence_scope']='process_output'
                result['evidence_note']='这是核心主机命令的退出状态与输出。输出可能是程序生成的文字；成功退出不单独证明文字描述的经历或现实动作发生。'
                # 她写的是 A，跑的是 A′：A′ 逐字露出来。
                shown = script[:6000]
                script_path_text = str(script_path) if script_path else None
                result['command'] = content
                result['executed_script'] = shown
                result['executed_script_chars'] = len(script)
                result['executed_script_path'] = script_path_text
                result['wrapper'] = wrapper
                visible = result.get('visibility') if isinstance(result.get('visibility'), dict) else {}
                truncated_items = list(visible.get('truncated') or [])
                if len(shown) < len(script):
                    # 脚本本身也可能很长：回执里放不下的部分要说清，并指向完整文件。
                    detail = ('脚本共 %d 字符，回执里只放了前 %d 字符；完整脚本在 executed_script_path'
                              % (len(script), len(shown))) if script_path_text else (
                              '脚本共 %d 字符，回执里只放了前 %d 字符；完整脚本这次没有落盘'
                              % (len(script), len(shown)))
                    truncated_items.append(dict(
                        what='实际执行的脚本', reason='limit', reason_text='超出上限',
                        detail=detail, shown_chars=len(shown), total_chars=len(script),
                        executed_script_path=script_path_text))
                result['visibility'] = visibility(
                    truncated=truncated_items,
                    skipped=visible.get('skipped') or [],
                    not_obtained=visible.get('not_obtained') or [],
                    note=visible.get('note') or '')
            if kind == 'dsh':
                result.update(usage=None, model=self.cfg.model)
                metadata = path.with_suffix('.result.json')
                data = {}
                if metadata.is_file():
                    try:
                        data = json.loads(metadata.read_text('utf-8')) or {}
                    except ValueError:
                        data = {}
                result['worker_result_path'] = str(metadata)
                # 只搬事实，不搬 status：进程退出码和「有没有会话文件」由上面的宿主判据决定，
                # worker 自己写的那一列另存，免得它把宿主判定的失败覆盖成完成。
                for key in ('usage', 'model', 'finish_reason', 'finish_reason_text', 'session_id',
                            'turn_reason', 'error_type', 'error_text', 'provenance',
                            'provenance_note', 'provenance_parts'):
                    if data.get(key) is not None:
                        result[key] = data[key]
                if data.get('status'):
                    result['worker_status'] = data['status']
                result.setdefault('visibility', visibility())
            return result
        finally:
            if tree is not None:
                tree.stop()
            self._running.discard(task)

    def _ask_run(self, bus, action):
        """提问四个 op。"""
        fields = action.get('arguments')
        if isinstance(fields, str) and fields.strip():
            try:
                fields = json.loads(fields)
            except ValueError:
                fields = {}
        fields = fields if isinstance(fields, dict) else {}
        op = str(action.get('op') or fields.get('op') or 'ask').strip().lower()
        try:
            if op == 'ask':
                receipt = bus.ask(str(fields.get('question') or action.get('question') or ''),
                                  options=fields.get('options') or (),
                                  expires_in=fields.get('expires_in') or 1800)
            elif op == 'pending':
                receipt = bus.pending()
            elif op == 'answer':
                receipt = bus.answer(fields.get('text'), question_id=fields.get('id'))
            elif op == 'cancel':
                receipt = bus.cancel(fields.get('id'))
            else:
                return dict(status='failed', op=op, kind='ask',
                            output='ask 的 op 只有 ask/pending/answer/cancel，收到 %r' % op)
            receipt['kind'] = 'ask'
            return receipt
        except ValueError as error:
            return dict(status='failed', op=op, kind='ask', output=str(error))
        except Exception as error:  # noqa: BLE001
            return dict(status='failed', op=op, kind='ask',
                        output='提问操作失败：%s: %s' % (type(error).__name__, error))

    def _task_run(self, items, action):
        """任务清单四个 op。参数既认顶层也认 arguments，和别的 kind 一致。"""
        fields = action.get('arguments')
        if isinstance(fields, str) and fields.strip():
            try:
                fields = json.loads(fields)
            except ValueError:
                fields = {}
        fields = fields if isinstance(fields, dict) else {}
        op = str(action.get('op') or fields.get('op') or 'list').strip().lower()
        try:
            if op == 'add':
                # arguments 里的 goal 才是她真正要记的那件事；action 顶层的
                # 'goal' 常常是程序派生的（等于 op 名），不能优先用。
                receipt = items.add(str(fields.get('goal') or action.get('goal') or ''),
                                    done_when=fields.get('done_when') or fields.get('acceptance') or '',
                                    depends_on=fields.get('depends_on') or (),
                                    origin=str(fields.get('origin') or 'self'))
            elif op == 'update':
                receipt = items.update(fields.get('ref') or fields.get('key') or '',
                                       status=fields.get('status'),
                                       blocked_on=fields.get('blocked_on'),
                                       next_step=fields.get('next_step'))
            elif op == 'list':
                receipt = items.list(status=fields.get('status'),
                                     limit=int(fields.get('limit') or 40))
            elif op == 'clear':
                receipt = items.clear()
            else:
                return dict(status='failed', op=op, kind='task',
                            output='task 的 op 只有 add/update/list/clear，收到 %r' % op)
            receipt['kind'] = 'task'
            return receipt
        except ValueError as error:
            return dict(status='failed', op=op, kind='task', output=str(error))
        except KeyError as error:
            return dict(status='failed', op=op, kind='task',
                        output='清单里没有这一条：%s（先用 op=list 看真实的 ref 或 key）'
                               % str(error).strip("'"))

    async def _background_run(self, action, op, fields):
        """后台任务的五个 op。全部放线程里跑：start/wait 都可能阻塞一会儿。"""
        command = str(action.get('command') or fields.get('command') or '').strip()
        cwd = Path(action.get('cwd') or fields.get('cwd') or self.home)
        if not cwd.is_absolute():
            cwd = self.home / cwd
        try:
            if op == 'start':
                if not command:
                    return dict(status='failed', op=op,
                                output='op=start 需要写 command：要后台跑的那条命令。')
                if not cwd.is_dir():
                    return dict(status='failed', op=op,
                                output='工作目录不存在：%s' % cwd)
                shell = shutil.which('sh') if os.name != 'nt' else (
                    shutil.which('pwsh') or shutil.which('powershell'))
                if shell is None:
                    return dict(status='failed', op=op, output='找不到可用的 shell。')
                return await asyncio.to_thread(self._background.start, command=command,
                                               cwd=str(cwd), shell=shell)
            if op == 'list':
                # 枚举不需要 job_id：jobs.json 里本来就存着全部任务。
                return await asyncio.to_thread(self._background.list)
            job_id = str(fields.get('job_id') or action.get('job_id') or '').strip()
            if not job_id:
                return dict(status='failed', op=op,
                            output='%s 需要 job_id：用 op=start 回执里那个；'
                                   '想先看看有哪些用 op=list。' % op)
            if op == 'status':
                return await asyncio.to_thread(self._background.status, job_id)
            if op == 'logs':
                return await asyncio.to_thread(self._background.logs, job_id,
                                               int(fields.get('lines') or 60))
            if op == 'wait':
                return await asyncio.to_thread(self._background.wait, job_id,
                                               float(fields.get('timeout') or 60))
            if op == 'stop':
                return await asyncio.to_thread(self._background.stop, job_id)
            return dict(status='failed', op=op, output='不认识的 op：%s' % op)
        except KeyError as error:
            return dict(status='failed', op=op,
                        output='找不到这个后台任务：%s（只认 op=start 回执里的 job_id）'
                               % str(error).strip("'"))
        except Exception as error:  # noqa: BLE001 - 原因要原样回给她
            return dict(status='failed', op=op,
                        output='后台任务操作失败：%s: %s' % (type(error).__name__, error))

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
            # command 在 arguments(JSON) 里，action 顶层通常没有它。
            # 2026-11-18 实测：这里原来只读 action['command']，读不到就退回 goal，
            # 于是「用 work run 只跑纯命令 ls ...」整句被当 shell 跑，报 "用: not found"。
            # 与她自己的判断一致：「我传进去的那段目标文字被当命令原样执行了」。
            # goal 是自然语言，永远不能当命令——宁可直接报错让她改参数。
            from .companion_work import Work
            fields = Work.FIELDS
            merged = {key: action[key] for key in fields if key in action}
            # 顶层写法的 command/cwd 也认（模型有时不带 arguments 直接写在 action 上）。
            for extra in ('command', 'cwd', 'timeout'):
                if action.get(extra):
                    merged.setdefault(extra, action[extra])
            raw = action.get('arguments')
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = json.loads(raw)
                except ValueError as error:
                    return self._work_artifact(dict(status='failed', return_code=1, op=op,
                        code='work_error', output='arguments 不是合法 JSON：%s' % error), started)
                if isinstance(parsed, dict):
                    merged.update(parsed)
            text = str(merged.get('command') or '').strip()
            if not text:
                return self._work_artifact(dict(status='failed', return_code=1, op=op, code='work_error',
                    output='run 需要在 arguments 里给 command（真正的命令，不是描述）。'
                           '例如 {"command":"ls life/ | head -3","cwd":"/opt/qq-deepseek"}'), started)
            command = dict(action)
            command.update(kind='command', command=text)
            command.setdefault('cwd', merged.get('cwd') or str(self._work.root))
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

    @staticmethod
    def _wrapped_script(content):
        """真正交给 shell 的脚本（A′）：在她写的命令前后加了固定的一层。"""
        if os.name != 'nt':
            return content
        return ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "$ProgressPreference = 'SilentlyContinue'; "
                "$ErrorActionPreference = 'Stop';\n" + content +
                "\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }")

    def _annotate(self, kind, action, result):
        """兼容入口：组装只有一份，在模块级 ``annotate_receipt``。

        phone/browser 模块与 RemoteExecutor 的旁路都调用同一份；这里保留方法名
        是为了既有调用与测试不用改。
        """
        return annotate_receipt(kind, action, result)

    def _work_artifact(self, result, started):
        """Work answers are re-readable later, like every other action result."""
        folder = self.home/'data/companion/artifacts'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/(uuid.uuid4().hex+'.work.json')
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        result['output_path'] = str(path)
        result['seconds'] = round(time.monotonic()-started, 3)
        # setdefault：read/grep 自己算的 truncated 更准，不许被这一行盖掉
        # （以前这里无条件重写，字符截断的回执会被改回 False）。
        result.setdefault('truncated', len(str(result.get('output') or '')) >= 12000)
        result.setdefault('visibility', visibility())
        if str(result.get('status') or '') != 'completed':
            # 失败时写明缺了什么、为什么缺（不存在/读不了/越界/超限）：
            # 不能再留一个空缺口，让摘要读成「没有被截断、跳过或没取得的内容」。
            gap = work_failure_gap(result)
            result['not_obtained'] = [gap]
            result['visibility'] = visibility(not_obtained=[gap], failed=True)
        if result.get('code') == 'work_error':
            result.update(provenance(PROVENANCE_TOOL, source='work',
                                     note='这是工具拒绝/失败的原因，不是文件内容。'))
        return result

    async def close(self):
        self._closed = True
        # 先停后台任务再取消其它协程：关闭服务不能把长任务留成孤儿。
        try:
            self._background.reap_all()
        except Exception:  # noqa: BLE001 - 关闭路径不因为清理失败而中断
            log.warning('后台任务清理失败', exc_info=True)
        tasks = list(self._running)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._computer.close()
        await self._bilibili.close()
