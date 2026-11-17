"""Authenticated phone audio, independent of character cognition and its event loop."""
import asyncio
import base64
import json
import re
import time

from aiohttp import web

MAX_AUDIO_BYTES=8*1024*1024
TTS_TIMEOUT=30


class MobileAudio:
    def __init__(self,mobile):
        self.home=mobile.home
        self.speech_lock=asyncio.Lock();self.cache={}
        mobile.server.app.router.add_post('/mobile/speech',self.speech)

    def voice(self):
        try:value=json.loads((self.home/'data/companion/speech.json').read_text('utf8')).get('voice','')
        except (OSError,ValueError,AttributeError):value=''
        return value if isinstance(value,str) and re.fullmatch(r'[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural',value) else 'zh-CN-XiaoyiNeural'

    async def _synthesize(self,text,voice):
        import edge_tts
        output=bytearray()
        async for chunk in edge_tts.Communicate(text,voice).stream():
            if chunk['type']=='audio':
                output.extend(chunk['data'])
                if len(output)>MAX_AUDIO_BYTES:raise ValueError('Synthesized audio exceeds limit')
        if not output:raise ValueError('No speech audio received')
        return bytes(output)

    async def speech(self,request):
        try:
            text=(await request.json()).get('text')
            if not isinstance(text,str) or not text.strip() or len(text)>2000:raise ValueError()
            text=text.strip()
        except (ValueError,AttributeError,TypeError):raise web.HTTPBadRequest(text='Expected 1 to 2000 characters')
        voice=self.voice();key=(voice,text)
        try:await asyncio.wait_for(self.speech_lock.acquire(),1)
        except asyncio.TimeoutError:raise web.HTTPTooManyRequests(text='Speech synthesis busy',headers={'Retry-After':'2'})
        try:
            self.cache={key:value for key,value in self.cache.items() if time.monotonic()-value[0]<600}
            if key in self.cache:audio=self.cache[key][1]
            else:
                audio=await asyncio.wait_for(self._synthesize(text,voice),TTS_TIMEOUT)
                if not audio or len(audio)>MAX_AUDIO_BYTES:raise ValueError()
                while self.cache and (len(self.cache)>=8 or sum(len(item[1]) for item in self.cache.values())+len(audio)>16*1024*1024):
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key]=(time.monotonic(),audio)
        except asyncio.TimeoutError:raise web.HTTPGatewayTimeout(text='Speech synthesis timed out')
        except Exception:raise web.HTTPServiceUnavailable(text='Speech synthesis unavailable')
        finally:self.speech_lock.release()
        return web.json_response(dict(mime='audio/mpeg',base64=base64.b64encode(audio).decode()),headers={'Cache-Control':'no-store'})
