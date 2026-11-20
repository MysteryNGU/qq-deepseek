"""Native work tools: read/write/edit/list/grep without a DSH runtime.

The companion already had an unrestricted shell action, so the missing piece was
never raw execution: it was a typed, auditable way to look at and change files
inside one agreed workspace. These operations mirror the DSH tool surface
(read / write / edit / grep / glob / shell) but run in-process, so no node, tsx
or DSH SDK has to exist on the host.

Every operation reports what it really did: paths, sizes, sha256 digests and the
exact replacement count. A write receipt is evidence that bytes changed, never
evidence that a feature now works; callers must re-read or re-run to verify.

回执还要说清两件以前看不见的事：内容是谁给的（``provenance``：本机直接观察 /
工具自己的报告 / 另一个模型的转述），以及这次**没看见**什么（``visibility``：
截断、跳过、没取得，逐项带原因）。这个模块同时是这两套词汇的唯一来源，别的
工具模块从这里 import——它们都依赖这里，反过来不成立，所以不会循环引用。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import time

MAX_OUTPUT = 12000
GREP_MAX_BYTES = 2_000_000
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', '.venv-companion',
             'life.bak-20261116-021713', 'life.bak-fix-20261116-101221'}

# --------------------------------------------------------------- 回执的来源分级
# 一条回执必须说清楚「这条内容是谁给的」。三类来源不许混写：
#   direct_observation  本机直接采集到的原始内容（文件字节、进程退出码与 stdout、
#                       屏幕画面与界面树、页面正文）——中间没有别的模型。
#   tool_report         执行器/工具自己的报告（状态、计数、覆盖范围、截断与跳过、
#                       哈希、SDK 事件）——是「工具说它做了什么」，不是原始内容。
#   model_account       另一个模型说的话（dsh 输出、子 agent 交回的说法）——
#                       它说的不等于现实已经改变，产物要按路径与哈希自己核对。
# 一条回执里确实可能既有原始内容又有工具自己的计数：这时用 parts 逐项标，
# 不许把模型的话塞进 direct_observation（那正是「只有 completed」的来源）。
PROVENANCE_DIRECT = 'direct_observation'
PROVENANCE_TOOL = 'tool_report'
PROVENANCE_MODEL = 'model_account'
PROVENANCE_KINDS = (PROVENANCE_DIRECT, PROVENANCE_TOOL, PROVENANCE_MODEL)
PROVENANCE_NOTE = {
    PROVENANCE_DIRECT: '本机直接采集到的原始内容（字节/退出码/画面/正文），中间没有别的模型转述。',
    PROVENANCE_TOOL: '这是执行器或工具自己的报告（状态、计数、覆盖范围、截断与跳过），不是原始内容本身。',
    PROVENANCE_MODEL: '这是另一个模型说的话（dsh 或子 agent 的转述）；它说做完了不等于现实已经改变，'
                      '产物要按路径与哈希自己核对。',
}

# 「这次没有取得 X」只写一个 status 等于没说：原因必须分层。
MISSING_REASONS = ('unauthorized', 'unavailable', 'failed', 'limit',
                   'not_requested', 'unsupported', 'unspecified')
MISSING_REASON_TEXT = {
    'unauthorized': '没有授权',
    'unavailable': '不可用',
    'failed': '失败',
    'limit': '超出上限',
    'not_requested': '这次没有要求它',
    'unsupported': '这个环境不支持',
    # 上游只说了「没取得」、没给原因：如实记「没给原因」，不许替它推断成不可用。
    'unspecified': '没有给出原因',
}


def provenance(kind, *, note='', source='', parts=None):
    """给回执标来源。``kind`` 只能是三类之一；``parts`` 逐项标各部分来源。

    返回的是可以直接 ``result.update(provenance(...))`` 的字典。
    """
    if kind not in PROVENANCE_KINDS:
        raise ValueError('provenance 只能是 %s，收到 %r'
                         % ('/'.join(PROVENANCE_KINDS), kind))
    receipt = dict(provenance=kind, provenance_note=str(note or PROVENANCE_NOTE[kind]))
    if source:
        receipt['provenance_source'] = str(source)
    if parts:
        bad = {key: value for key, value in parts.items() if value not in PROVENANCE_KINDS}
        if bad:
            raise ValueError('provenance parts 里有不认识的分级：%r' % bad)
        receipt['provenance_parts'] = {str(key): value for key, value in parts.items()}
    return receipt


def missing(what, reason, detail='', *, code=''):
    """一件「这次没有取得的东西」：写清是什么、按哪一层原因、具体细节。"""
    if reason not in MISSING_REASONS:
        raise ValueError('没取得的原因只能是 %s，收到 %r'
                         % ('/'.join(MISSING_REASONS), reason))
    item = dict(what=str(what), reason=reason, reason_text=MISSING_REASON_TEXT[reason],
                detail=str(detail or ''))
    if code:
        item['code'] = str(code)
    return item


# 「失败」不是一种缺口原因，而是整条回执的状态：失败时必须写出缺了什么。
# queued/pending/running 这类还没结果的不是失败；agent 台账里的 status 是那件任务的，
# 由 executor.annotate_receipt 改写成「那件任务的结果」缺口。
FAILED_STATUSES = ('failed', 'error', 'unknown', 'interrupted', 'cancelled', 'stopping')


def failed_receipt(result):
    """这条回执算不算失败：status 是失败态、带了 error、或退出码非 0。

    失败的回执绝不能自称「没有缺口」——那会把「没做成」读成「一切正常」。
    ``queued``/``pending``/``running`` 这类还没结果的不是失败。
    """
    if not isinstance(result, dict):
        return False
    if result.get('error'):
        return True
    if str(result.get('status') or '').strip().lower() in FAILED_STATUSES:
        return True
    code = result.get('return_code', result.get('exit_code'))
    return isinstance(code, (int, float)) and not isinstance(code, bool) and code != 0


#: work 失败时「没取得什么」用哪个名字（按 op 说人话，不说「data」）。
WORK_WHAT = {'read': '文件内容', 'grep': '扫描结果', 'list': '目录清单',
             'glob': '目录清单', 'write': '写入结果', 'edit': '编辑结果',
             'run': '命令结果'}


def work_failure_gap(result):
    """work 失败时的缺口：文件不存在/读不了/越界/超限各自一层，detail 用原话。

    只按工具自己写下的原因分类，不替它推断（拿不到原因就记 failed + 原话）。
    """
    result = result if isinstance(result, dict) else {}
    text = str(result.get('output') or '')
    # 顺序有讲究：「超出工作区」里也含「超出」，先认越界再认超限。
    if '不存在' in text:
        reason = 'unavailable'
    elif '读不到' in text:
        reason = 'failed'
    elif '超出工作区' in text:
        reason = 'unauthorized'
    elif '超时' in text:
        reason = 'failed'
    elif '上限' in text or '超出' in text:
        reason = 'limit'
    elif any(word in text for word in ('必填', '必须是', '无效', '只读文件',
                                       '工作区根', 'op 只能')):
        reason = 'unsupported'
    else:
        reason = 'failed'
    return missing(WORK_WHAT.get(str(result.get('op') or ''), '这次操作的结果'),
                   reason, text or '这个操作失败了，但没有留下原因',
                   code=str(result.get('code') or ''))


def _gap_text(item):
    """缺口在 summary 里显示的文字：具体原因优先，分类词只在没有 detail 时兜底。

    「没取得：截图（这次没有要求它）」把「workspace_open 不取画面」丢了——她只
    看到一个笼统分类，不知道是哪一步没做、也不知道自己该不该再做点什么。
    分类词仍留在结构字段 ``reason``/``reason_text`` 里，供机器判断。
    """
    detail = str(item.get('detail') or '').strip()
    label = str(item.get('reason_text') or '').strip()
    code = str(item.get('code') or '').strip()
    if detail:
        text = ('%s：%s' % (label, detail)) if label and label not in detail else detail
        if code and code not in text:
            text += '（code=%s）' % code
        return text
    return label or code or str(item.get('reason') or '没有写原因')


def visibility(*, truncated=(), skipped=(), not_obtained=(), note='', failed=False):
    """「这次到底看见了什么」的固定形状：缺口逐项列出，不留一个 status 了事。

    ``state`` 是三种互斥状态之一，``complete`` 只是它的旧写法：

    * ``complete``  —— 成功，且没有被截断、跳过或没取得的内容；
    * ``truncated`` —— 成功，但有缺口（被截断/跳过/这次没要求/部分没取得）；
    * ``failed``    —— 这次失败了：一定写出没取得什么和原因，``complete`` 必为 False。

    ``failed=True`` 时即使调用方漏写缺口，也不会留下「没有被截断、跳过或没取得
    的内容」这种与她看到的 status 自相矛盾的摘要。
    """
    truncated = [dict(item) for item in (truncated or ())]
    skipped = [dict(item) for item in (skipped or ())]
    not_obtained = [dict(item) for item in (not_obtained or ())]
    lines = []
    for item in truncated:
        lines.append('截断：%s（%s）' % (item.get('what') or '内容', _gap_text(item)))
    for item in skipped:
        lines.append('跳过：%s（%s）' % (item.get('what') or item.get('path') or '文件',
                                       _gap_text(item)))
    for item in not_obtained:
        lines.append('没取得：%s（%s）' % (item.get('what') or '内容', _gap_text(item)))
    state = 'failed' if failed else ('complete' if not lines else 'truncated')
    if failed and not lines:
        # 兜底：失败却一个缺口都没有，是调用方漏写了；宁可说「没写原因」，
        # 也不能让摘要读成「一切正常」。
        lines.append('失败：这次没有取得结果（调用方没有写出原因）')
    return dict(truncated=truncated, skipped=skipped, not_obtained=not_obtained,
                complete=state == 'complete', state=state, note=str(note or ''),
                summary='；'.join(lines) or '这次没有被截断、跳过或没取得的内容。')


import subprocess


class WorkError(ValueError):
    """A rejected or impossible work operation, reported to the model verbatim."""


class SkippedFile(Exception):
    """grep 没有读这个文件。跳过必须留原因，不能当成「扫了、0 命中」。"""

    def __init__(self, reason, code, detail=''):
        self.reason, self.code, self.detail = reason, code, str(detail or '')
        super().__init__('%s: %s' % (code, detail))


class Work:
    def __init__(self, cfg, home, root: str | Path | None = None):
        self.home = Path(home).resolve()
        configured = root if root is not None else getattr(cfg, 'work_root', '')
        value = str(configured or '').strip()
        if value:
            self.root = Path(value).expanduser().resolve()
        else:
            self.root = Path(__file__).resolve().parents[1]

    # ------------------------------------------------------------------ helpers
    def available(self):
        try:
            return self.root.is_dir()
        except OSError:
            return False

    def _resolve(self, value, *, must_exist=False, allow_root=False):
        if not isinstance(value, str) or not value.strip():
            raise WorkError('path 必填')
        raw = Path(value).expanduser()
        path = raw if raw.is_absolute() else (self.root / raw)
        try:
            path = path.resolve()
        except OSError as error:
            raise WorkError('路径无法解析：%s' % error)
        if path != self.root and self.root not in path.parents:
            raise WorkError('路径超出工作区：%s（工作区根 %s）' % (path, self.root))
        if path == self.root and not allow_root:
            raise WorkError('这里需要一个具体文件或子目录，不是工作区根')
        if must_exist and not path.exists():
            raise WorkError('不存在：%s' % path)
        return path

    @staticmethod
    def _sha_bytes(data):
        return hashlib.sha256(data).hexdigest()

    def _sha(self, path):
        try:
            return self._sha_bytes(path.read_bytes())
        except OSError:
            return None

    @staticmethod
    def _atomic_write(path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('.%s.tmp-%d' % (path.name, os.getpid()))
        try:
            tmp.write_text(content, encoding='utf-8')
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _skip(path: Path):
        return any(part in SKIP_DIRS for part in path.parts)

    def _relative(self, path: Path):
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    # --------------------------------------------------------------- operations
    def read(self, args):
        path = self._resolve(args.get('path'), must_exist=True)
        if path.is_dir():
            raise WorkError('read 只读文件；目录用 list')
        offset = max(1, int(args.get('offset') or 1))
        limit = min(4000, max(1, int(args.get('limit') or 400)))
        try:
            text = path.read_text('utf-8', errors='replace')
        except OSError as error:
            raise WorkError('读不到：%s' % error)
        lines = text.splitlines()
        chunk = lines[offset - 1:offset - 1 + limit]
        body = '\n'.join('%6d| %s' % (offset + i, line) for i, line in enumerate(chunk))
        shown = body[:MAX_OUTPUT]
        # 截断有两种，原来只算了「行」那一种：一整段（或几行很长的行）超过
        # MAX_OUTPUT 时 output 被切掉，回执却还说 truncated=False，看起来读全了。
        char_truncated = len(shown) < len(body)
        line_truncated = offset - 1 + len(chunk) < len(lines)
        gaps = []
        if char_truncated:
            gaps.append(dict(what='输出字符', reason='limit', reason_text='超出上限',
                             detail='这次只带回 %d/%d 个字符（MAX_OUTPUT=%d），后面的字符没有显示'
                                    % (len(shown), len(body), MAX_OUTPUT),
                             shown_chars=len(shown), total_chars=len(body)))
        if line_truncated:
            gaps.append(dict(what='后面的行', reason='limit', reason_text='超出上限',
                             detail='这次只显示第 %d-%d 行（共 %d 行）；用 offset/limit 接着读'
                                    % (offset, offset + len(chunk) - 1, len(lines)),
                             shown_lines=len(chunk), total_lines=len(lines),
                             next_offset=offset + len(chunk)))
        result = dict(output=shown, path=str(path), relative=self._relative(path),
                      total_lines=len(lines), offset=offset, shown=len(chunk),
                      truncated=bool(char_truncated or line_truncated),
                      chars_shown=len(shown), chars_total=len(body),
                      chars_truncated=char_truncated, lines_truncated=line_truncated,
                      sha=self._sha(path))
        result.update(provenance(PROVENANCE_DIRECT, source='work.read',
                                 parts={'content': PROVENANCE_DIRECT,
                                        'counts': PROVENANCE_TOOL,
                                        'truncation': PROVENANCE_TOOL}))
        result['visibility'] = visibility(truncated=gaps)
        return result

    def write(self, args):
        path = self._resolve(args.get('path'))
        content = args.get('content')
        if not isinstance(content, str):
            raise WorkError('content 必须是字符串')
        before = path.read_bytes() if path.is_file() else None
        self._atomic_write(path, content)
        after = path.read_bytes()
        return dict(output='已写入 %d 字节：%s' % (len(after), path), path=str(path),
                    relative=self._relative(path), bytes=len(after), changed=before != after,
                    sha=self._sha_bytes(after),
                    previous_sha=self._sha_bytes(before) if before is not None else None)

    def edit(self, args):
        path = self._resolve(args.get('path'), must_exist=True)
        old = args.get('old')
        new = args.get('new', '')
        if not isinstance(old, str) or not old:
            raise WorkError('old 必须是非空字符串')
        if not isinstance(new, str):
            raise WorkError('new 必须是字符串')
        replace_all = bool(args.get('replace_all'))
        try:
            text = path.read_text('utf-8')
        except OSError as error:
            raise WorkError('读不到：%s' % error)
        count = text.count(old)
        if count == 0:
            raise WorkError('old 在 %s 里没有命中；先用 read 确认原文与缩进' % self._relative(path))
        if count > 1 and not replace_all:
            raise WorkError('old 在 %s 命中 %d 处；换更长锚点或传 replace_all=true' % (self._relative(path), count))
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        expected = self._sha_bytes(updated.encode('utf-8'))
        self._atomic_write(path, updated)
        return dict(output='已在 %s 替换 %d 处' % (self._relative(path), count if replace_all else 1),
                    path=str(path), relative=self._relative(path), replacements=count if replace_all else 1,
                    found=count, sha=self._sha(path), expected_sha=expected)

    def list(self, args):
        path = self._resolve(args.get('path') or '.', must_exist=True, allow_root=True)
        limit = min(400, max(1, int(args.get('limit') or 200)))
        pattern = str(args.get('pattern') or '').strip()
        recursive = bool(args.get('recursive'))
        if path.is_file():
            candidates = [path]
        elif pattern or recursive:
            candidates = [p for p in path.rglob(pattern or '*') if not self._skip(p)][:limit * 4]
        else:
            candidates = [p for p in path.iterdir() if not self._skip(p)]
        entries = []
        for item in sorted(candidates, key=lambda p: (p.is_file(), str(p)))[:limit]:
            try:
                stat = item.stat()
            except OSError:
                continue
            entries.append('%s\t%s\t%s' % ('d' if item.is_dir() else '-',
                                           stat.st_size, self._relative(item)))
        return dict(output='\n'.join(entries), path=str(path), relative=self._relative(path),
                    count=len(entries), truncated=len(candidates) > limit)

    def _read_for_grep(self, path):
        """grep 读一个文件：超限或读不了就抛 SkippedFile，由调用方记进 skipped_files。

        以前这两种情况都是 ``continue``：文件被跳过，scanned 却照样 +1，回执上
        看起来「扫了 N 个文件、0 命中」——那是「没看」被写成了「看了没有」。
        """
        try:
            size = path.stat().st_size
        except OSError as error:
            raise SkippedFile('failed', 'read_error', str(error))
        if size > GREP_MAX_BYTES:
            raise SkippedFile('limit', 'too_large',
                              '%d 字节 > %d 字节上限，这个文件没有读' % (size, GREP_MAX_BYTES))
        try:
            return path.read_text('utf-8', errors='replace')
        except OSError as error:
            raise SkippedFile('failed', 'read_error', str(error))

    def grep(self, args):
        pattern = args.get('pattern')
        if not isinstance(pattern, str) or not pattern:
            raise WorkError('pattern 必填')
        path = self._resolve(args.get('path') or '.', must_exist=True, allow_root=True)
        include = str(args.get('include') or '').strip()
        limit = min(400, max(1, int(args.get('limit') or 120)))
        flags = 0 if args.get('case_sensitive') else re.IGNORECASE
        if args.get('literal'):
            pattern = re.escape(pattern)
        try:
            rx = re.compile(pattern, flags)
        except re.error as error:
            raise WorkError('正则无效：%s' % error)
        if path.is_file():
            files = [path]
        else:
            files = [p for p in path.rglob(include or '*') if p.is_file() and not self._skip(p)]
        hits = []
        scanned = 0
        skipped = []
        for position, item in enumerate(files):
            try:
                text = self._read_for_grep(item)
            except SkippedFile as error:
                gap = missing(self._relative(item), error.reason, error.detail, code=error.code)
                gap['path'] = self._relative(item)
                gap['what'] = self._relative(item)
                skipped.append(gap)
                continue
            scanned += 1   # 只有真读了的文件才算扫描过
            for number, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append('%s:%d: %s' % (self._relative(item), number, line.strip()[:200]))
                    if len(hits) >= limit:
                        left = len(files) - position - 1
                        if left:
                            tail = missing('还没扫描的文件', 'limit',
                                           '命中已经到上限（%d 条），后面 %d 个文件没有扫描' % (limit, left),
                                           code='hit_limit')
                            skipped.append(tail)
                        gap = dict(what='命中条数', reason='limit', reason_text='超出上限',
                                   detail='只带回前 %d 条命中；用 include/path 或更窄的 pattern 缩小范围'
                                          % limit, shown=limit)
                        result = dict(output='\n'.join(hits), pattern=pattern, count=len(hits),
                                      scanned=scanned, truncated=True, limit=limit,
                                      files_seen=len(files), skipped_files=skipped)
                        result.update(provenance(
                            PROVENANCE_DIRECT, source='work.grep',
                            parts={'content': PROVENANCE_DIRECT,
                                   'counts': PROVENANCE_TOOL,
                                   'truncation': PROVENANCE_TOOL,
                                   'skipped': PROVENANCE_TOOL}))
                        result['visibility'] = visibility(
                            truncated=[gap], skipped=skipped,
                            note='按 SKIP_DIRS 跳过的目录（.git/node_modules 等）不计入 scanned。')
                        return result
        result = dict(output='\n'.join(hits), pattern=pattern, count=len(hits), scanned=scanned,
                      truncated=False, limit=limit, files_seen=len(files),
                      skipped_files=skipped)
        result.update(provenance(
            PROVENANCE_DIRECT, source='work.grep',
            parts={'content': PROVENANCE_DIRECT, 'counts': PROVENANCE_TOOL,
                   'truncation': PROVENANCE_TOOL, 'skipped': PROVENANCE_TOOL}))
        result['visibility'] = visibility(
            skipped=skipped,
            note='按 SKIP_DIRS 跳过的目录（.git/node_modules 等）不计入 scanned。')
        return result

    # ----------------------------------------------------------------- dispatch
    # command/cwd/timeout 是 run 的参数。2026-11-18：run 的方法、handler、错误消息
    # 都写好了，但 FIELDS 里没有这三个，_arguments() 按白名单过滤后 handler 收到空 dict，
    # run() 于是报「command 必填」——功能写了却永远用不了。
    FIELDS = ('path', 'content', 'old', 'new', 'replace_all', 'pattern', 'include',
              'offset', 'limit', 'recursive', 'case_sensitive', 'literal',
              'command', 'cwd', 'timeout')

    def _arguments(self, action):
        """Accept both arguments(JSON) and fields written straight on the action."""
        args = {key: action[key] for key in self.FIELDS if key in action}
        raw = action.get('arguments')
        if isinstance(raw, str):
            try:
                import json
                raw = json.loads(raw) if raw.strip() else {}
            except ValueError as error:
                raise WorkError('arguments 不是合法 JSON：%s' % error)
        if isinstance(raw, dict):
            args.update(raw)
        return args

    def run(self, args):
        """在工作区跑一条 shell 命令。

        说明里一直承诺 `run{command,cwd?}`（"改完必须再用 read 或 run 复验"），
        但实现从没接过它——错误消息甚至同时说"只能是 …/run"和"收到 'run'"。
        2026-11-18 实测：她复验只能绕道 command kind，这一条等于不存在。

        边界与文件操作一致：cwd 解析后必须仍在工作区内，越界拒绝。
        """
        command = args.get('command')
        if not isinstance(command, str) or not command.strip():
            raise WorkError('command 必填')
        cwd = args.get('cwd')
        # cwd 允许就是工作区根本身（她跑 `ls life/` 时自然会把根填进来）。
        workdir = self._resolve(cwd, must_exist=True, allow_root=True) if cwd else self.root
        if not workdir.is_dir():
            raise WorkError('cwd 必须是目录：%s' % workdir)
        timeout = args.get('timeout')
        seconds = int(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 120
        try:
            done = subprocess.run(['/bin/sh', '-c', command], cwd=str(workdir),
                                  capture_output=True, text=True, timeout=min(seconds, 600))
        except subprocess.TimeoutExpired:
            raise WorkError('命令超时（%ss）：%s' % (seconds, command[:120]))
        output = (done.stdout or '') + (done.stderr or '')
        # 与 command kind 的语义一致：非 0 退出码就是失败，不把失败说成完成。
        return dict(return_code=done.returncode, output=output[-20000:],
                    stdout=(done.stdout or '')[-20000:], stderr=(done.stderr or '')[-20000:],
                    ok=done.returncode == 0)

    def execute(self, action):
        op = str(action.get('op') or '').strip().lower()
        args = self._arguments(action)
        handlers = {'read': self.read, 'write': self.write, 'edit': self.edit,
                    'list': self.list, 'glob': self.list, 'ls': self.list, 'grep': self.grep,
                    'run': self.run}
        handler = handlers.get(op)
        if handler is None:
            raise WorkError('op 只能是 read/write/edit/list/grep/run，收到 %r' % op)
        started = time.monotonic()
        result = handler(args)
        # 不许无条件把 return_code 写成 0：run 跑的命令非 0 退出就是失败，
        # 覆盖它等于把失败说成成功（2026-11-18 实测：rc=3 被改写成 completed/0）。
        result.setdefault('return_code', 0)
        result.setdefault('status', 'completed' if result['return_code'] == 0 else 'failed')
        result.update(seconds=round(time.monotonic() - started, 3))
        result.setdefault('op', op)
        # 文件内容是本机直接读到的字节（direct_observation）；「扫了几个、跳过几个、
        # 切掉了多少」是工具自己的计数（tool_report）。两者分开标，谁也不冒充谁。
        result.setdefault('provenance', PROVENANCE_DIRECT)
        if 'provenance_note' not in result:
            result.update(provenance(
                result['provenance'], source='work.%s' % op,
                parts={'content': PROVENANCE_DIRECT, 'counts': PROVENANCE_TOOL,
                       'truncation': PROVENANCE_TOOL, 'skipped': PROVENANCE_TOOL}))
        result.setdefault('visibility', visibility())
        return result
