"""Desktop speech: existing voice presets, one local queue, no model calls."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid

ROOT=Path(__file__).resolve().parents[1]


def load_speech_settings(home):
    path=Path(home)/'data/companion/speech.json'
    try: value=json.loads(path.read_text('utf-8'))
    except (OSError,ValueError): value={}
    return dict(enabled=value.get('enabled',True),voice=value.get('voice','zh-CN-XiaoyiNeural'),volume=value.get('volume',.8))


def save_speech_settings(home, enabled=True, voice='zh-CN-XiaoyiNeural', volume=.8):
    if type(enabled) is not bool or not isinstance(voice,str) or not voice.strip():raise ValueError('语音开关或音色无效')
    if type(volume) not in (int,float) or not 0<=volume<=1:raise ValueError('音量须在0到1之间')
    path=Path(home)/'data/companion/speech.json';path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(dict(enabled=enabled,voice=voice,volume=volume)),encoding='utf-8');temporary.replace(path)
    return load_speech_settings(home)


def presets(home):
    path=Path(home)/'data/voice-presets/manifest.json'
    try:return json.loads(path.read_text('utf-8')).get('presets',[])
    except (OSError,ValueError):return []


def speech_status(cfg,home):
    try:runtime=json.loads((Path(home)/'data/companion/speech-runtime.json').read_text('utf-8'))
    except (OSError,ValueError):runtime={}
    return dict(**load_speech_settings(home),runtime=runtime,voices=[dict(id=p['id'],name=p['name']) for p in presets(home)],
                fallback='zh-CN-XiaoyiNeural',supported=os.name=='nt')


class SpeechPlayer:
    def __init__(self,cfg,home):
        self.cfg,self.home=cfg,Path(home).resolve()
        self.generation=0;self.closed=False;self.process=None;self.loop=None;self.task=None
        self.pending=queue.Queue();self.alias='companion_'+uuid.uuid4().hex
        self.thread=threading.Thread(target=self._thread,daemon=True,name='companion-speech');self.thread.start()

    def _thread(self):
        asyncio.run(self._worker())

    def say(self,text):
        if self.closed or not text.strip() or not load_speech_settings(self.home)['enabled']:return
        self.pending.put((self.generation,text))

    def interrupt(self):
        self.generation+=1
        while True:
            try:self.pending.get_nowait()
            except queue.Empty:break
        if self.loop and not self.loop.is_closed() and self.task:self.loop.call_soon_threadsafe(self.task.cancel)
        self._mci('close '+self.alias)

    def _mci(self,command):
        if os.name!='nt':return ''
        output=ctypes.create_unicode_buffer(256)
        code=ctypes.windll.winmm.mciSendStringW(command,output,256,None)
        return '' if code else output.value

    async def _worker(self):
        self.loop=asyncio.get_running_loop()
        while not self.closed:
            try:generation,text=self.pending.get_nowait()
            except queue.Empty:
                await asyncio.sleep(.08);continue
            if generation!=self.generation:continue
            self.task=asyncio.create_task(self._speak(text,generation))
            try:await self.task
            except asyncio.CancelledError:pass
            except Exception as error:
                path=self.home/'data/companion/speech-error.json'
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(dict(error=type(error).__name__)),encoding='utf-8')
            finally:self.task=None
        await self._stop_process()

    async def _stop_process(self):
        process,self.process=self.process,None
        if process and process.returncode is None:
            if os.name=='nt':
                killer=await asyncio.create_subprocess_exec('taskkill','/PID',str(process.pid),'/T','/F',
                    stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                await killer.wait()
            try:process.terminate()
            except ProcessLookupError:return
            try:await asyncio.wait_for(process.wait(),3)
            except asyncio.TimeoutError:process.kill();await process.wait()

    async def _speak(self,text,generation):
        settings=load_speech_settings(self.home)
        if not settings['enabled']:return
        try:
            path=await self.synthesize(text,settings['voice'])
            if generation!=self.generation:return
            self._mci('close '+self.alias)
            self._mci(f'open "{path}" alias {self.alias}')
            self._mci(f'setaudio {self.alias} volume to {round(settings["volume"]*1000)}')
            self._mci('play '+self.alias)
            while generation==self.generation and not self.closed and self._mci('status '+self.alias+' mode')=='playing':
                if not load_speech_settings(self.home)['enabled']:break
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            await self._stop_process();raise
        finally:self._mci('close '+self.alias)

    async def synthesize(self,text,voice):
        preset=next((p for p in presets(self.home) if p['id']==voice),None)
        provider=next((p for p in getattr(self.cfg,'local_media',{}).get('providers',[]) if p.get('provider_id')=='indextts'),None)
        local=bool(preset and provider and not getattr(self,'_local_failed',False) and Path(provider.get('environment',{}).get('python','__missing__')).is_file())
        folder=self.home/'data/companion/speech'/uuid.uuid4().hex;folder.mkdir(parents=True,exist_ok=True)
        request=dict(text=text,folder=str(folder),preset=preset,voice=voice)
        if local:request['backend']=provider['config']
        python=provider['environment']['python'] if local else str(ROOT/'.venv/Scripts/python.exe')
        if not Path(python).is_file():python=sys.executable
        try:
            if self.process and getattr(self,'_python',None)!=python:await self._stop_process()
            if not self.process or self.process.returncode is not None:
                self.process=await asyncio.create_subprocess_exec(python,'-u',str(Path(__file__).resolve()),'--worker',
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                    env=dict(os.environ,PYTHONIOENCODING='utf-8'),
                    **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
                self._python=python
            async with asyncio.timeout(45 if local else 30):
                self.process.stdin.write((json.dumps(request,ensure_ascii=False)+'\n').encode('utf-8'));await self.process.stdin.drain()
                while True:
                    line=await self.process.stdout.readline()
                    if not line:raise RuntimeError('speech_worker_exited')
                    if line.startswith(b'@speech '):
                        result=json.loads(line[8:])
                        if result.get('error'):raise RuntimeError(result['error'])
                        return Path(result['path'])
        except asyncio.CancelledError:
            await self._stop_process();raise
        except Exception as error:
            await self._stop_process()
            if local:
                self._local_failed=True
                fallback=preset.get('source_voice','zh-CN-XiaoyiNeural')
                (self.home/'data/companion/speech-runtime.json').write_text(json.dumps(dict(
                    requested=voice,actual=fallback,fallback_reason=type(error).__name__)),encoding='utf-8')
                return await self.synthesize(text,fallback)
            raise

    def close(self):
        self.closed=True;self.interrupt();self.thread.join(5)


def worker():
    sys.path.insert(0,str(ROOT));cache={}
    for line in sys.stdin:
        try:
            request=json.loads(line);folder=Path(request['folder']);preset=request.get('preset') or {}
            if request.get('backend'):
                from life.media_adapters.indextts import _avoid_local_module_shadowing,execute_speech
                _avoid_local_module_shadowing()
                spec=dict(seed=42,reference_path=preset['reference']['path'],reference_sha256=preset['reference']['sha256'],
                    segments=[dict(text=request['text'],pause_after_ms=0,delivery=preset.get('delivery',{}))])
                output=execute_speech(dict(backend=request['backend'],spec=spec),folder,cache)
            else:
                import edge_tts
                output=folder/'output.mp3'
                voice=preset.get('source_voice') or request['voice']
                if not voice.endswith('Neural'):voice='zh-CN-XiaoyiNeural'
                asyncio.run(edge_tts.Communicate(request['text'],voice,**preset.get('reference_options',{})).save(str(output)))
            print('@speech '+json.dumps(dict(path=str(output))),flush=True)
        except Exception as error:print('@speech '+json.dumps(dict(error=type(error).__name__)),flush=True)


if __name__=='__main__' and '--worker' in sys.argv:worker()
