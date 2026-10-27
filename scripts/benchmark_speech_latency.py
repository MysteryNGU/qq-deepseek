"""Compare cold and resident TTS locally, without model API calls or QQ sends."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import tomllib

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from life.local_media_config import LocalMediaConfig
from life.media_adapters import AdapterRegistry,frozen_hash
from life.media_process import MediaProcess


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--warm-only',action='store_true')
    args=parser.parse_args()
    config=tomllib.loads((ROOT/'config.toml').read_text('utf-8-sig'))
    cfg=LocalMediaConfig.parse(config['local_media']).resolve(ROOT)
    registry=AdapterRegistry(cfg,ROOT)
    database=(ROOT/config.get('life_db','data/life.db')).resolve()
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute('SELECT v.record FROM voice_preferences p JOIN voice_preference_revisions r USING(scope_id,voice_id) '
            'JOIN local_voice_revisions v ON v.voice_id=r.voice_id AND v.revision=r.voice_revision '
            'WHERE p.scope_id LIKE ? LIMIT 1',('%qq:private:'+str(config['owner'])+'%',)).fetchone()
        if row is None:
            row=connection.execute('SELECT v.record FROM voice_preferences p JOIN local_voice_revisions v '
                'ON p.voice_id=v.voice_id ORDER BY v.revision DESC LIMIT 1').fetchone()
        if row is None:raise RuntimeError('no_configured_local_voice')
        voice=json.loads(row['record'])
        asset=connection.execute('SELECT storage_ref,digest FROM media_assets WHERE id=?',(voice['reference_asset_id'],)).fetchone()
    if asset is None:raise RuntimeError('voice_reference_missing')
    reference=Path(asset['storage_ref'])
    if hashlib.sha256(reference.read_bytes()).hexdigest()!=voice['reference_digest']:
        raise RuntimeError('voice_reference_digest_mismatch')
    provider=registry.resolve('speech',voice.get('provider_id'))
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    cfg['root']=str(output);backend=MediaProcess(cfg)
    rows=[]
    try:
        waiting=await backend.resources_ready()
        if waiting:raise RuntimeError(waiting)
        cases=('resident-first','resident-reuse') if args.warm_only else ('cold','resident-first','resident-reuse')
        for name in cases:
            folder=output/name;folder.mkdir()
            spec=dict(kind='speech',seed=1234,voice_id=voice['id'],voice_revision=voice['revision'],
                reference_asset_id=voice['reference_asset_id'],reference_sha256=voice['reference_digest'],
                segments=[dict(text='今天的天气很好，我们一起出去散散步吧。',pause_after_ms=0,delivery=voice['parameters'])],_adapter=provider)
            request=dict(spec=dict(spec,reference_path=str(reference)),frozen_spec=spec,
                frozen_spec_hash=frozen_hash(spec),request_hash=name,job_id=name)
            if name!='cold':
                await backend.prepare(request['spec'])
                waiting=await backend.resources_ready()
                if waiting:raise RuntimeError(waiting)
            started=time.perf_counter()
            run=backend._run_once if name=='cold' else backend.run
            result=await run(request,folder,lambda:None)
            row=dict(case=name,elapsed_seconds=round(time.perf_counter()-started,3),
                audio_seconds=result['media']['duration_seconds'],worker_pid=result.get('worker_pid'),
                audio_sha256=result['sha256'],execution_seconds=result.get('execution_seconds'))
            row['resident_cuda']=result.get('resident_cuda')
            rows.append(row);print(json.dumps(row),flush=True)
            (output/'comparison.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
    finally:
        await backend.close()


if __name__=='__main__':
    asyncio.run(main())
