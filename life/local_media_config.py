"""Validated deployment settings for the two independently installed media engines."""
from dataclasses import asdict, dataclass, fields,field
import math
from pathlib import Path


@dataclass(frozen=True)
class LocalMediaConfig:
    enabled: bool = False
    root: str = 'data/local-media'
    voice_python: str = ''
    voice_engine_root: str = ''
    voice_model_dir: str = ''
    image_server: str = ''
    image_model_dir: str = ''
    preset_manifest: str = ''
    voice_model_version: str = ''
    image_model_version: str = ''
    timeout_seconds: int = 600
    min_available_ram_mib: int = 1024
    min_free_vram_mib: int = 4096
    max_pending_jobs: int = 8
    max_text_chars: int = 1000
    worker_threads: int = 4
    image_max_vram_gib: float = 5.0
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
                     'max_pending_jobs','max_text_chars','worker_threads'):
            if type(getattr(result,name)) is not int or getattr(result,name)<1:
                raise ValueError('invalid_local_media_limit:'+name)
        if (isinstance(result.image_max_vram_gib,bool) or not isinstance(result.image_max_vram_gib,(int,float))
                or not math.isfinite(result.image_max_vram_gib) or result.image_max_vram_gib<=0):
            raise ValueError('invalid_local_media_vram_budget')
        for field in fields(cls):
            if isinstance(field.default,str) and not isinstance(getattr(result,field.name),str):
                raise ValueError('invalid_local_media_path_or_version:'+field.name)
        if not isinstance(result.providers,list) or any(not isinstance(p,dict) for p in result.providers) or not isinstance(result.defaults,dict):
            raise ValueError('invalid_media_provider_configuration')
        if result.enabled and not result.providers and any(not getattr(result,n) for n in (
                'voice_python','voice_engine_root','voice_model_dir','image_server','image_model_dir',
                'voice_model_version','image_model_version')):
            raise ValueError('enabled_local_media_requires_engines_and_versions')
        return result

    def resolve(self, root):
        result = asdict(self)
        for name in ('root','voice_python','voice_engine_root','voice_model_dir','image_server','image_model_dir','preset_manifest'):
            if result[name]:
                result[name] = str((Path(root)/result[name]).resolve())
        return result
