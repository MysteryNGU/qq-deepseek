"""Shared execution limits and explicit installed media provider declarations."""
from dataclasses import asdict, dataclass, fields,field
from pathlib import Path


@dataclass(frozen=True)
class LocalMediaConfig:
    enabled: bool = False
    root: str = 'data/local-media'
    preset_manifest: str = ''
    timeout_seconds: int = 600
    min_available_ram_mib: int = 1024
    min_free_vram_mib: int = 4096
    max_pending_jobs: int = 8
    speech_idle_seconds: int = 300
    providers: list[dict] = field(default_factory=list)
    defaults: dict[str,str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw=None):
        raw = {} if raw is None else raw
        if not isinstance(raw, dict) or set(raw)-{f.name for f in fields(cls)}:
            raise ValueError('unknown_local_media_config')
        result = cls(**raw)
        if type(result.enabled) is not bool:
            raise ValueError('local_media_enabled_requires_boolean')
        for name in ('timeout_seconds','min_available_ram_mib','min_free_vram_mib',
                     'max_pending_jobs','speech_idle_seconds'):
            if type(getattr(result,name)) is not int or getattr(result,name)<1:
                raise ValueError('invalid_local_media_limit:'+name)
        for field in fields(cls):
            if isinstance(field.default,str) and not isinstance(getattr(result,field.name),str):
                raise ValueError('invalid_local_media_path_or_version:'+field.name)
        if not isinstance(result.providers,list) or any(not isinstance(p,dict) for p in result.providers) or not isinstance(result.defaults,dict):
            raise ValueError('invalid_media_provider_configuration')
        if result.enabled and not result.providers:
            raise ValueError('enabled_local_media_requires_providers')
        return result

    def resolve(self, root):
        result = asdict(self)
        for name in ('root','preset_manifest'):
            if result[name]:
                result[name] = str((Path(root)/result[name]).resolve())
        return result
