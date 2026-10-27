"""Independent persona sources selected by the actual conversation audience."""
from pathlib import Path
import re

from .persona import AUDIENCE_BLOCK,project_public_text,write_view


DERIVED_HEADER = re.compile(r'\A<!-- Derived persona sha256=[a-f0-9]+ -->\r?\n')


class PersonaFiles:
    def __init__(self,root,cfg):
        self.root = Path(root).resolve()
        self.private = self.path(getattr(cfg,'persona','persona.md'))
        self.unified = getattr(cfg,'unified_persona',False)
        self.public = self.private if self.unified else self.path(getattr(cfg,'public_persona','personas/public.md'))
        if not self.unified and self.private==self.public:
            raise ValueError('private_and_public_persona_paths_must_differ')

    def path(self,value):
        if not isinstance(value,str) or not value.strip():
            raise ValueError('persona_path_requires_nonempty_string')
        path = self.root/value
        if self.root not in path.resolve().parents or any(p.is_symlink() for p in [path,*path.parents] if p!=self.root):
            raise ValueError('persona_path_must_stay_inside_project_without_symlinks')
        return path.resolve()

    @property
    def personas_dir(self):
        return self.public.parent

    def actor_file(self,actor_id):
        """personas/<actor_id>.md 存在时，就把它当作该人物的私有人格来源。"""
        value = str(actor_id or '').strip()
        if not re.fullmatch(r'[0-9]+',value):
            return None
        candidate = self.personas_dir/f'{value}.md'
        if not candidate.is_file():
            return None
        return self.path(candidate.relative_to(self.root).as_posix())

    def select(self,private,actor_id=None,group=False):
        if self.unified:
            return self.private
        if private:
            return self.private
        if group:
            return self.public
        return self.actor_file(actor_id) or self.public

    @staticmethod
    def validate(source):
        if not isinstance(source,str) or not source.strip():
            raise ValueError('persona_source_must_not_be_empty')
        if '<!-- persona:' in source or '<!-- /persona:' in source or DERIVED_HEADER.match(source):
            raise ValueError('persona_sources_are_independent_files_remove_legacy_audience_markers')
        return source

    def migrate(self):
        """Split a declared legacy layout once; independently edited files win conflicts."""
        source = self.private.read_text('utf-8')
        if self.unified:
            self.validate(source)
            return False
        blocks = list(AUDIENCE_BLOCK.finditer(source))
        if not blocks:
            self.validate(source)
            if self.public.is_file():
                self.validate(self.public.read_text('utf-8'))
            return False
        common = project_public_text(source)
        private = '\n\n'.join(m[2].strip() for m in blocks if m[1]=='private')
        self.validate(common)
        self.validate(private)
        if self.public.exists():
            current = self.public.read_text('utf-8')
            if not DERIVED_HEADER.match(current) and current.strip()!=common.strip():
                raise ValueError('independent_public_persona_conflicts_with_legacy_public_section')
        # Public is committed first. A retry after interruption can still finish
        # from the unchanged legacy private source without guessing its audience.
        write_view(self.public,common.strip()+'\n')
        write_view(self.private,private.strip()+'\n')
        return True

    def selected(self,private):
        return self.select(private)

    def read(self,private,actor_id=None,group=False):
        path = self.select(private,actor_id,group)
        return self.validate(path.read_text('utf-8')) if path.is_file() else ''


def owner_private(scope,owner):
    return scope.conversation_id=='qq:private:'+str(owner)
