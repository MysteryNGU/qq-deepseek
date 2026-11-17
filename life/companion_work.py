"""Native work tools: read/write/edit/list/grep without a DSH runtime.

The companion already had an unrestricted shell action, so the missing piece was
never raw execution: it was a typed, auditable way to look at and change files
inside one agreed workspace. These operations mirror the DSH tool surface
(read / write / edit / grep / glob / shell) but run in-process, so no node, tsx
or DSH SDK has to exist on the host.

Every operation reports what it really did: paths, sizes, sha256 digests and the
exact replacement count. A write receipt is evidence that bytes changed, never
evidence that a feature now works; callers must re-read or re-run to verify.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import time

MAX_OUTPUT = 12000
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', '.venv-companion',
             'life.bak-20261116-021713', 'life.bak-fix-20261116-101221'}


class WorkError(ValueError):
    """A rejected or impossible work operation, reported to the model verbatim."""


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
        return dict(output=body[:MAX_OUTPUT], path=str(path), relative=self._relative(path),
                    total_lines=len(lines), offset=offset, shown=len(chunk),
                    truncated=offset - 1 + len(chunk) < len(lines), sha=self._sha(path))

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
        for item in files:
            scanned += 1
            try:
                if item.stat().st_size > 2_000_000:
                    continue
                text = item.read_text('utf-8', errors='replace')
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append('%s:%d: %s' % (self._relative(item), number, line.strip()[:200]))
                    if len(hits) >= limit:
                        return dict(output='\n'.join(hits), pattern=pattern, count=len(hits),
                                    scanned=scanned, truncated=True)
        return dict(output='\n'.join(hits), pattern=pattern, count=len(hits), scanned=scanned,
                    truncated=False)

    # ----------------------------------------------------------------- dispatch
    FIELDS = ('path', 'content', 'old', 'new', 'replace_all', 'pattern', 'include',
              'offset', 'limit', 'recursive', 'case_sensitive', 'literal')

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

    def execute(self, action):
        op = str(action.get('op') or '').strip().lower()
        args = self._arguments(action)
        handlers = {'read': self.read, 'write': self.write, 'edit': self.edit,
                    'list': self.list, 'glob': self.list, 'ls': self.list, 'grep': self.grep}
        handler = handlers.get(op)
        if handler is None:
            raise WorkError('op 只能是 read/write/edit/list/grep/run，收到 %r' % op)
        started = time.monotonic()
        result = handler(args)
        result.update(status='completed', return_code=0,
                      seconds=round(time.monotonic() - started, 3))
        result.setdefault('op', op)
        return result
