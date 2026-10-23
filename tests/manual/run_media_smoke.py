"""Actual TTS and local ASR perception, without any QQ delivery."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT','30')
os.environ.setdefault('HF_HUB_ETAG_TIMEOUT','10')

from life.asr import WhisperASR
from life.media import MediaService
from life.runtime import LifeRuntime
from life.voice_catalog import VoiceCatalog


async def main():
    with tempfile.TemporaryDirectory() as folder:
        life = LifeRuntime(Path(folder)/'life.db')
        try:
            scope = life.scope('console:media-test','test')
            event,_ = life.ingest(scope,'test','inference','合成测试，无私人内容')
            asr = WhisperASR(ROOT/'tools'/'asr-models','small')
            media = MediaService(life,Path(folder)/'media',asr=asr)
            voices = VoiceCatalog(life,media)
            voices.register(dict(id='xiaoyi',engine='edge-tts',engine_voice='zh-CN-XiaoyiNeural',languages=['zh-CN']))
            audio = await voices.synthesize(scope,event.id,'sample','这是一段语音测试，请记住蓝色的小鱼。','xiaoyi')
            print(json.dumps(dict(tts_verified=voices.require('xiaoyi')['status']=='verified',bytes=len(media.read_bytes(scope,audio['id'])))),flush=True)
            perception = await media.perceive(scope,audio['id'])
            transcript = json.loads(perception['content'])['text']
            print(json.dumps(dict(asr_verified=True,transcript=transcript,engine=perception['engine_version']),ensure_ascii=False),flush=True)
            if '蓝色' not in transcript or '鱼' not in transcript:
                raise RuntimeError('ASR semantic marker was not recognized')
        finally:
            life.close()


if __name__=='__main__':
    asyncio.run(main())
