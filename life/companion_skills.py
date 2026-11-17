"""她自己和主人共用的方法库。

一个 skill 就是 skills/ 下的一个 markdown 文件：第一行是标题，接下来一句话说明
它解决什么，后面是可以照着做的步骤。动手前先读一份，做完一件真做成的多步事情
再把可复用的部分存下来——这样下一次不必从零想。

这里只负责存取原文。skill 是方法，不是执行过的证据：照它做的每一步仍然要真的
做一遍，回执才算数。
"""
import json
import re
from pathlib import Path

NAME = re.compile(r'^[a-z0-9][a-z0-9._-]{0,48}$')
HEAD = re.compile(r'^#\s*(.+)$', re.M)
MAX_BODY = 20000
OPS_READ = {'read', 'open', 'show', 'cat'}
OPS_WRITE = {'save', 'write', 'store'}
OPS_LIST = {'list', 'ls', 'catalog', ''}


def _summary(text):
    """标题之后的第一段非空文字，当作这份方法的用途。"""
    body = HEAD.sub('', text, count=1)
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith(('|---', '|', '```')):
            return line[:120]
    return ''


class SkillLibrary:
    def __init__(self, home):
        self.root = Path(home) / 'skills'

    def _files(self):
        if not self.root.is_dir():
            return []
        return sorted(path for path in self.root.glob('*.md') if path.is_file())

    def catalog(self):
        items = []
        for path in self._files():
            text = path.read_text('utf-8', errors='replace')
            title = HEAD.search(text)
            items.append(dict(name=path.stem,
                              title=(title.group(1).strip() if title else path.stem)[:80],
                              summary=_summary(text),
                              chars=len(text)))
        return items

    def _resolve(self, name):
        stem = str(name or '').strip()
        if stem.endswith('.md'):
            stem = stem[:-3]
        if not NAME.match(stem):
            raise ValueError('skill 名字只用小写字母、数字和 _ - .，收到的是 %r' % (name,))
        path = self.root / (stem + '.md')
        if path.resolve().parent != self.root.resolve():
            raise ValueError('skill 路径不合法')
        return stem, path

    @staticmethod
    def _fields(action):
        """动作对象只有 op 和 arguments(JSON字符串)，名字和正文都在 arguments 里。"""
        fields = {}
        raw = action.get('arguments')
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except ValueError:
                raise ValueError('arguments 不是合法 JSON：%s' % raw[:120])
            if isinstance(parsed, dict):
                fields = dict(parsed)
        for key in ('name', 'skill', 'text', 'content', 'op'):
            if action.get(key) not in (None, ''):
                fields.setdefault(key, action[key])
        return fields

    async def run(self, action):
        fields = self._fields(action)
        op = str(fields.get('op') or action.get('op') or '').strip().lower()
        if op in OPS_LIST:
            items = self.catalog()
            names = '、'.join(item['name'] for item in items)
            return dict(status='completed', op='list', skills=items,
                        summary=('%d 个方法：%s' % (len(items), names)) if items else '还没有任何方法')
        if op in OPS_READ:
            stem, path = self._resolve(fields.get('name') or fields.get('skill'))
            if not path.is_file():
                known = '、'.join(item.stem for item in self._files()) or '（空）'
                return dict(status='failed', op='read', name=stem,
                            output='没有这个方法：%s；现有的是：%s' % (stem, known),
                            available=[item.stem for item in self._files()])
            body = path.read_text('utf-8', errors='replace')
            return dict(status='completed', op='read', name=stem, text=body[:MAX_BODY],
                        complete=len(body) <= MAX_BODY, source_path=str(path),
                        evidence_scope='file_content',
                        evidence_note='这是方法文件的原文，不是你执行过它的证据。')
        if op in OPS_WRITE:
            stem, path = self._resolve(fields.get('name') or fields.get('skill'))
            body = fields.get('text') or fields.get('content') or ''
            if not isinstance(body, str) or not body.strip():
                raise ValueError('save 需要 arguments 里的 text：标题、一句用途和可以照着做的步骤')
            existed = path.is_file()
            self.root.mkdir(parents=True, exist_ok=True)
            path.write_text(body[:MAX_BODY], encoding='utf-8')
            return dict(status='completed', op='save', name=stem, updated=existed,
                        source_path=str(path), chars=len(body[:MAX_BODY]),
                        summary='%s %s（%d 字）' % ('已更新' if existed else '已保存', path.name, len(body)))
        raise ValueError('skill 只支持 op=list/read/save')
