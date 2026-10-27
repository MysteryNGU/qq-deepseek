"""Explicit local GPU smoke test, isolated DB and harmless non-person prompt."""
import asyncio
import copy
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from life.role_config import load_config
from life.runtime import LifeRuntime
from life.media import MediaService
from life.voice_catalog import VoiceCatalog
from life.local_media import LocalMedia


async def main():
    folder=ROOT/'data/maintenance/image-followthrough-smoke'
    folder.mkdir(parents=True,exist_ok=True)
    cfg=copy.deepcopy(load_config(ROOT/'config.toml'))
    cfg.local_media=dict(cfg.local_media,root=str(folder/'worker'))
    life=LifeRuntime(folder/'life.db')
    media=MediaService(life,folder/'assets')
    service=LocalMedia(life,media,VoiceCatalog(life,media),cfg,ROOT)
    try:
        scope=life.scope('test:image-smoke','test')
        event,_=life.ingest_message(scope,'Generate a simple blue ceramic mug for a software test.')
        result=service.submit(scope,dict(kind='image',prompt='A blue ceramic mug on a plain white table, soft daylight, simple product photograph, no people, no text.',
            width=512,height=512,seed=7124),'followthrough-smoke-v1',[event.id])
        print(json.dumps(dict(stage='submitted',job_id=result['job_id'])),flush=True)
        await service.run_one()
        status=service.status(scope,result['job_id'])
        if status.get('asset_id'):
            status['path']=media.require(scope,status['asset_id'])['storage_ref']
        (folder/'report.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(status,ensure_ascii=False),flush=True)
        return 0 if status['status']=='completed' else 1
    finally:
        await service.close()
        life.close()


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
