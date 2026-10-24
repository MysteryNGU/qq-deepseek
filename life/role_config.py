"""Configuration for the sole DSH role host."""
from dataclasses import dataclass,field,fields
import math
import os
from pathlib import Path
import tomllib

from .message_layout import DEFAULT_PASTE_THRESHOLD


@dataclass
class Config:
    name: str = '大肥鱼'
    persona: str = 'persona.md'
    ws_url: str = 'ws://127.0.0.1:3001'
    ws_token: str = ''
    api_key: str = ''
    base_url: str = 'https://api.deepseek.com'
    model: str = 'deepseek-flash'
    owner: int = 0
    friends: list[int] = field(default_factory=list)
    known_people: dict[str,int] = field(default_factory=dict)
    allow_groups: list[int] = field(default_factory=list)
    allow_all_private: bool = False
    life_db: str = 'data/life.db'
    life_agent: str = 'default'
    resource_policy: dict = field(default_factory=dict)
    continuity_enabled: bool = False
    capability_policies: list[dict] = field(default_factory=list)
    local_media: dict = field(default_factory=dict)
    life_dynamics: dict = field(default_factory=dict)
    dsh_root: str = '../dsh-bridge-work'
    dsh_home: str = '../dsh-qq-home'
    dsh_node: str = 'D:/projects/tools/node-v22.19.0-win-x64/node.exe'
    dsh_provider: str = 'deepseek-official'
    dsh_timeout: float = 180
    dsh_tmp: str = 'D:/projects/tools/tmp'
    dsh_pnpm_entry: str = ''
    bili_plugin: str = field(default_factory=lambda:str(Path.home()/'.dsh/plugins/dsh-bili-subagent'))
    public_tools: list[str] = field(default_factory=list)
    action_per_hour: int = 500
    action_per_day: int = 0
    idle_seconds: int | None = None  # Read-time adapter for old explicit configuration.
    handoff_seconds: float = 10
    typing_chars_per_second: float = 3
    paste_threshold: int = DEFAULT_PASTE_THRESHOLD
    media_local_roots: list[str] = field(default_factory=list)
    media_max_mb: int = 64
    asr_model: str = 'small'
    asr_language: str = 'zh'

def load_config(path: Path):
    raw = tomllib.loads(Path(path).read_text('utf-8'))
    allowed = {f.name for f in fields(Config)}
    unknown = set(raw)-allowed
    if unknown:
        raise ValueError('obsolete_or_unknown_config: '+', '.join(sorted(unknown)))
    cfg = Config(**raw)
    cfg._source_path=str(Path(path).resolve())
    if type(cfg.continuity_enabled) is not bool:
        raise ValueError('continuity_enabled_requires_boolean')
    from .resource_policy import ResourcePolicy
    if cfg.idle_seconds is not None:
        import logging
        cfg.resource_policy = dict(cfg.resource_policy)
        previous_idle = cfg.idle_seconds
        if isinstance(previous_idle,float) and math.isfinite(previous_idle) and previous_idle.is_integer():
            previous_idle = int(previous_idle)
        cfg.resource_policy.setdefault('idle_base_seconds',previous_idle)
        logging.getLogger(__name__).warning('旧 idle_seconds 已映射为 resource_policy.idle_base_seconds；请迁移配置')
    cfg.resource_policy = ResourcePolicy.parse(cfg.resource_policy).__dict__.copy()
    from .local_media_config import LocalMediaConfig
    cfg.local_media = LocalMediaConfig.parse(cfg.local_media).__dict__.copy()
    cfg.api_key = os.environ.get('DEEPSEEK_API_KEY',cfg.api_key)
    cfg.ws_url = os.environ.get('QQ_WS_URL',cfg.ws_url)
    if not cfg.owner:
        raise ValueError('owner_required')
    if cfg.handoff_seconds<=0 or cfg.media_max_mb<=0:
        raise ValueError('invalid_host_timing_or_media_limit')
    if (not math.isfinite(cfg.typing_chars_per_second) or cfg.typing_chars_per_second<=0
            or not isinstance(cfg.paste_threshold,int) or cfg.paste_threshold<1):
        raise ValueError('invalid_message_cadence')
    return cfg
