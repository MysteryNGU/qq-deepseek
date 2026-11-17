"""Materialize reusable settings without copying a character's lived state."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil

from .drives import DriveConfig
from .role_config import Config,load_config
from .settings import atomic_write


def materialize(source,destination,identity,*,embedding_port=None):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    cfg=asdict(load_config(source/'config.toml'))
    fresh=asdict(Config())
    # Shared allowlists and explicit names are configuration, not learned
    # familiarity. Current relationships and histories are never copied.
    for key in ('name','owner','bot_qq','ws_url','ws_token',
                'persona','public_persona','unified_persona','life_agent','life_db','dsh_home','dsh_tmp'):
        cfg[key]=deepcopy(fresh[key])
    cfg.update(persona='persona.md',public_persona='persona.md',unified_persona=True,
               life_agent=identity,life_db='data/life.db',dsh_home='data/dsh-home',dsh_tmp='data/tmp')
    for key in ('dsh_root','dsh_node','dsh_pnpm_entry','bili_plugin'):
        if cfg[key]:cfg[key]=str((source/cfg[key]).resolve())
    cfg['media_local_roots']=[str((source/p).resolve()) for p in cfg['media_local_roots']]

    behavior=source/'life-dynamics.json'
    dynamics=json.loads(behavior.read_text('utf-8')) if behavior.is_file() else {}
    # These are parameters, never current tensions, relationship scores or history.
    cfg['life_dynamics']=DriveConfig.from_mapping(dynamics).to_dict()
    atomic_write(destination/'life-dynamics.json',json.dumps(dynamics,ensure_ascii=False,indent=2)+'\n')

    local=cfg['local_embedding']
    local['model_path']=str((source/local['model_path']).resolve())
    if local['enabled']:
        if embedding_port is not None:local['port']=embedding_port
        cfg['embedding']['endpoint']=f"http://127.0.0.1:{local['port']}/v1/embeddings"

    media=cfg['local_media']
    media['root']='data/local-media'
    if media['preset_manifest']:
        manifest_path=(source/media['preset_manifest']).resolve()
        manifest=json.loads(manifest_path.read_text('utf-8'))
        for key in ('production_catalog_imported','default_activation','catalog_scope','default_selected_by'):
            manifest.pop(key,None)
        for preset in manifest['presets']:
            for part in ('reference','sample'):
                asset=deepcopy(preset[part])
                original=Path(asset['path'])
                if not original.is_absolute():original=manifest_path.parent/original
                if hashlib.sha256(original.read_bytes()).hexdigest()!=asset['sha256']:
                    raise ValueError('voice_preset_digest_mismatch')
                target=destination/'data/voice-presets'/f"{preset['id']}-{part}{original.suffix}"
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(original,target)
                asset['path']=str(target)
                preset[part]=asset
        media['preset_manifest']='data/voice-presets/manifest.json'
        atomic_write(destination/media['preset_manifest'],json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')

    if cfg['consult_config']:
        configured=json.loads((source/cfg['consult_config']).read_text('utf-8'))
        configured['data_dir']=str(destination/'data/codex-consult')
        cfg['consult_config']='data/codex-consult/config.json'
        atomic_write(destination/cfg['consult_config'],json.dumps(configured,ensure_ascii=False,indent=2)+'\n')
    # TOML has no null; omitted optional fields recover the same parser default.
    def serializable(value):
        if isinstance(value,dict):return {key:serializable(item) for key,item in value.items() if item is not None}
        if isinstance(value,list):return [serializable(item) for item in value]
        return value
    return serializable(cfg)


def relocate_supporting_files(staging,final,cfg):
    """Staged creation must not leave temporary paths in voice/consult settings."""
    staging,final=Path(staging).resolve(),Path(final).resolve()
    for name in (cfg['local_media'].get('preset_manifest'),cfg.get('consult_config')):
        if not name:continue
        path=staging/name
        data=json.loads(path.read_text('utf-8'))
        # Path separators follow the platform, so use Path.relative_to as authority.
        def paths(value):
            if isinstance(value,dict):return {key:paths(item) for key,item in value.items()}
            if isinstance(value,list):return [paths(item) for item in value]
            if isinstance(value,str):
                try:return str(final/Path(value).relative_to(staging))
                except ValueError:pass
            return value
        atomic_write(path,json.dumps(paths(data),ensure_ascii=False,indent=2)+'\n')
