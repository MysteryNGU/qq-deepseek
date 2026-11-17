"""Coopanion body and chat, sharing Companion's single service and durable store."""
from __future__ import annotations

from concurrent.futures import Future
import json
import os
from pathlib import Path
import queue
import shutil
import threading
import time

from .companion_desktop import MODEL_NAMES, PetProcess, ServiceThread

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT/'life/companion_shell_assets'
RUNTIME = ROOT/'data/companion-runtime/coopanion-shell'


def available():
    return all(path.is_file() for path in (
        RUNTIME/'node_modules/electron/dist/electron.exe', RUNTIME/'web/chat.js',
        ROOT/'vendor/coopanion/packages/cortico-world-desktop-pet/src/world.ts'))


def installed_models():
    result = []
    for path in sorted((ROOT/'data/companion-models').rglob('*.model3.json')):
        label = next((MODEL_NAMES[p.lower()] for p in path.parts if p.lower() in MODEL_NAMES),
                     path.stem.replace('.model3', ''))
        folder = path.relative_to(ROOT/'data/companion-models').parts[0]
        result.append(dict(name=label, path=str(path.resolve()), figure='live2d-'+folder))
    return result


def prepare_figures(models):
    """Reference installed originals, keeping the source artwork and notices intact."""
    folder = RUNTIME/'figures'
    lib = RUNTIME/'live2d-lib'
    lib.mkdir(parents=True, exist_ok=True)
    for source, target in [
        (RUNTIME/'live2d-core/live2dcubismcore.min.js','live2dcubismcore.min.js'),
        (RUNTIME/'node_modules/pixi.js/dist/browser/pixi.min.js','pixi.min.js'),
        (RUNTIME/'node_modules/@pixi/unsafe-eval/dist/browser/unsafe-eval.min.js','unsafe-eval.min.js'),
        (RUNTIME/'node_modules/pixi-live2d-display/dist/cubism4.min.js','cubism4.min.js')]:
        shutil.copy2(source, lib/target)
    import subprocess
    for row in models:
        pack = folder/row['figure']
        pack.mkdir(parents=True, exist_ok=True)
        model = Path(row['path'])
        for link, target in [(pack/'model',model.parent),(pack/'lib',lib)]:
            if not link.exists():
                # Junctions need no administrator privilege on Windows.
                if os.name == 'nt':
                    subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],
                                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    link.symlink_to(target, target_is_directory=True)
        shutil.copy2(ASSETS/'live2d-figure.js',pack/'figure.js')
        shutil.copy2(ASSETS/'live2d-motion.js',pack/'live2d-motion.js')
        motions = {}
        if row['figure']=='live2d-hakuko':
            import hashlib
            authored = json.loads((ASSETS/'hakuko-motions.json').read_text('utf-8'))
            if hashlib.sha256(model.read_bytes()).hexdigest()==authored['model']['model3_sha256']:
                motions = authored['motions']
                shutil.copy2(ASSETS/'hakuko-motions.json',pack/'hakuko-motions.json')
        gait = False  # Front-facing mesh deformation did not meet the requested side-view walk.
        (pack/'companion-model.json').write_text(json.dumps(dict(file='model/'+model.name,gait=gait,
            motions='hakuko-motions.json' if motions else None)), 'utf-8')
        manifest=dict(manifest=2,api=2,id=row['figure'],version='1.0.0',name=dict(zh=row['name'],en=row['name']),
                      about=dict(zh='支持待机、呼吸、眨眼和拖拽的 Live2D 形象。',en='Live2D idle model with dragging, breathing and blinking.'),
                      entry='figure.js',export_='createLive2DBody',axes=[],presets=[],vocab=[dict(id=key,kind='motion',
                          names=dict(zh=[value['label']]),about=dict(zh=value['label'],en=key),
                          seconds=value['motion']['Meta']['Duration']) for key,value in motions.items()],can=dict(walk=gait),moves=[])
        manifest['export']=manifest.pop('export_')
        (pack/'figure.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')


class CompanionShell:
    def __init__(self, cfg, home, *, companion_factory=None):
        self.home = Path(home).resolve()
        self.models = installed_models()
        prepare_figures(self.models)
        self.settings_path = self.home/'data/companion/appearance.json'
        try:
            settings = json.loads(self.settings_path.read_text('utf-8'))
        except (OSError, ValueError):
            settings = {}
        default = next((row['path'] for row in self.models if row['figure']=='live2d-hakuko'), 'whale')
        selected = settings.get('model') if isinstance(settings, dict) else None
        self.model = selected if selected=='whale' or any(row['path']==selected for row in self.models) else default
        env = dict(os.environ, ELECTRON_RUN_AS_NODE='1', COMPANION_NAME=cfg.name)
        command = [str(RUNTIME/'node_modules/electron/dist/electron.exe'),
                   '--import', (RUNTIME/'node_modules/tsx/dist/loader.mjs').as_uri(),
                   '--import', (ROOT/'vendor/coopanion/vendor/cortico/src/extensions/runtime.ts').as_uri(),
                   str(ASSETS/'world.ts'), str(self.home)]
        self.body = None
        self.closing = False
        self.pending = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.snapshot = None
        self.body_ready = False
        self.body_attached = False
        self.body_context = None
        self.body_observations = []
        self.worker = ServiceThread(cfg, home, companion_factory=companion_factory)
        try:
            self.body = PetProcess(command, self.worker.events,
                                   self.home/'data/companion/shell.log', env=env)
        except Exception:
            self.worker.close()
            self.worker.thread.join(5)
            raise
        from .companion_speech import SpeechPlayer
        self.speech = SpeechPlayer(cfg, self.home)
        self.speech_messages = {}

    def control(self, op, args=None):
        """Core-facing body operation; receipts describe what the renderer actually did."""
        future = Future()
        with self.lock:
            if self.closing:
                future.set_exception(RuntimeError('Desktop body is closed'))
                return future
            self.sequence += 1
            key = str(self.sequence)
            self.pending[key] = future
        self.body.send(type='control', id=key, op=op, args=args or {})
        def cancelled(done):
            if done.cancelled():
                with self.lock:
                    self.pending.pop(key, None)
                self.body.send(type='control_cancel', id=key)
        future.add_done_callback(cancelled)
        return future

    def _snapshot(self, snapshot):
        self.snapshot = dict(snapshot, models=self.models, model=self.model)
        self.body.send(type='snapshot', snapshot=self.snapshot)
        if isinstance(snapshot.get('phase'), dict):
            self.body.send(type='phase', phase=snapshot['phase'])
        self._flush_body()

    def _flush_body(self):
        if not getattr(self, 'body_ready', False):
            return
        if not self.body_attached:
            if not self.worker.call('attach_body', self.control):
                return
            self.body_attached = True
        if self.body_context is not None and self.worker.call('update_body', self.body_context):
            self.body_context = None
        while self.body_observations:
            if not self.worker.call('observe_body', self.body_observations[0]):
                break
            self.body_observations.pop(0)

    def handle(self, event):
        kind = event.get('type')
        if kind == 'snapshot':
            self._snapshot(event['snapshot'])
        elif kind == 'phase' and isinstance(event.get('phase'), dict):
            self.body.send(type='phase', phase=event['phase'])
        elif kind == 'connection' and event.get('connected'):
            self.body.send(type='notice',text='')
        elif kind == 'message' and event.get('role') == 'user':
            self.body.send(type='history_message', message=event)
            if getattr(self,'speech',None):
                self.speech.interrupt()
                self.speech_messages.clear()
        elif kind == 'message' and event.get('role') == 'assistant':
            targets = (event.get('delivery') or {}).get('targets', ['all'])
            desktop = 'all' in targets or 'desktop' in targets
            if desktop and getattr(self,'speech',None) and event.get('id'):
                self.speech_messages[event['id']] = event.get('text',event.get('content',''))
            self.body.send(type='history_message', message=event)
            if desktop:
                self.body.send(type='say', text=event.get('text', event.get('content', '')),
                               **{key:event[key] for key in ('id','turn_id','part','parts','images','source','delivery','created') if key in event})
            self.worker.call('snapshot')
        elif kind == 'error':
            # Errors are observable status, never inserted as assistant conversation.
            self.body.send(type='notice', text=event.get('text') or '这次没有完成，请稍后再试。')
            self.body.send(type='phase', phase=dict(state='idle', running=[]))
        elif kind == 'pet' and event.get('sender') is self.body:
            message = event['event']
            action = message.get('type')
            if action == 'input':
                if getattr(self,'speech',None):
                    self.speech.interrupt()
                    self.speech_messages.clear()
                if self.worker.call('chat', message.get('text', ''), message.get('images')):
                    self.body.send(type='settled', cursor=message['cursor'])
                else:
                    self.body.send(type='notice', text='伙伴还未就绪，请稍后再试。')
            elif action == 'active':
                self.worker.call('set_active', bool(message['active']))
            elif action == 'connections':
                import subprocess
                import sys
                subprocess.Popen([sys.executable, str(ROOT/'companion.py'), '--home', str(self.home), '--connections'],
                                 cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            elif action == 'model':
                path = message.get('path')
                row = next((row for row in self.models if row['path']==path), None)
                if path=='whale' or row:
                    self.model = path
                    self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                    self.settings_path.write_text(json.dumps(dict(model=path),ensure_ascii=False), 'utf-8')
                    self.body.send(type='appearance', figure='whale' if path=='whale' else row['figure'])
                    if self.snapshot:
                        self._snapshot(self.snapshot)
            elif action == 'appearance':
                row = next((row for row in self.models if row['figure']==message.get('figure')), None)
                # The upstream wardrobe also owns Coo and user-imported figure packs.
                figure = message.get('figure')
                if not row and figure and figure!='whale':
                    row = dict(name=message.get('name') or figure, path='figure:'+figure, figure=figure)
                    self.models.append(row)
                if row or message.get('figure')=='whale':
                    self.model = row['path'] if row else 'whale'
                    self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                    self.settings_path.write_text(json.dumps(dict(model=self.model),ensure_ascii=False),'utf-8')
                    if self.snapshot:
                        self._snapshot(self.snapshot)
            elif action == 'ready':
                self.body_ready = True
                row = next((row for row in self.models if row['path']==self.model), None)
                self.body.send(type='appearance', figure=row['figure'] if row else 'whale', restore=True)
                if self.snapshot:
                    self._snapshot(self.snapshot)
                self._flush_body()
            elif action == 'body_context':
                self.body_context = message['context']
                self._flush_body()
            elif action == 'receipt':
                if getattr(self,'speech',None):
                    text=self.speech_messages.pop(message.get('message_id'),None)
                    if text and not message.get('failed'): self.speech.say(text)
            elif action == 'body_event':
                self.body_observations.append(message['event'])
                self._flush_body()
            elif action == 'control_result':
                with self.lock:
                    future = self.pending.pop(str(message.get('id')), None)
                if future and not future.done():
                    future.set_result(message)
            elif action in {'exit', 'closed'}:
                self.close()
        elif kind == 'closed':
            self.body.send(type='notice', text='角色服务已停止，请退出后重新启动。')

    def run(self):
        refreshed = time.monotonic()
        try:
            while not self.closing:
                if time.monotonic()-refreshed > 3:
                    self.worker.call('snapshot')
                    refreshed = time.monotonic()
                try:
                    self.handle(self.worker.events.get(timeout=.2))
                except queue.Empty:
                    if self.body.process.poll() is not None:
                        break
        finally:
            self.close()

    def close(self):
        if self.closing:
            return
        self.closing = True
        if getattr(self,'speech',None): self.speech.close()
        self.worker.close()
        if self.body:
            self.body.close()
        self.worker.thread.join(15)
        with self.lock:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('Desktop body closed'))
            self.pending.clear()


def run_coopanion(cfg, home, *, companion_factory=None):
    CompanionShell(cfg, home, companion_factory=companion_factory).run()
