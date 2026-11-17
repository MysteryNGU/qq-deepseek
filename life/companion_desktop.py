"""Native desktop surface; Companion and its database belong to one asyncio thread."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


MODEL_NAMES = {'snowbear': '雪熊少女', 'dafeiyu': '大肥鱼 · 经典蓝发女仆',
               'witch': '魔女', 'kaguya': '八千代辉夜姬', 'hot-nun': '修女 · Hot Nun',
               'hakuko': '神宫白子 · 面饼0', 'changli': '长离'}


def preferred_model(paths):
    """The user's two defaults; unrelated demo assets are never auto-selected."""
    for name in ('hakuko', 'snowbear', 'dafeiyu'):
        path = next((str(p) for p in paths if name in str(p).lower()), None)
        if path:
            return path
    return None


def activity_text(event):
    """Show observable work; keep model decisions and reasoning out of the log."""
    lines = [event.get('text', '')]
    details = event.get('details') or event
    action = details.get('action')
    if isinstance(action, dict):
        command = action.get('command') or action.get('goal')
        if command:
            lines.append(('命令：' if action.get('kind') == 'command' else '执行目标：')+str(command))
        if action.get('cwd'):
            lines.append('工作目录：'+str(action['cwd']))
    result = details.get('result')
    if isinstance(result, dict):
        output = str(result.get('output') or '')
        if output:
            lines.append('输出：\n'+output[:2400]+('\n…（其余输出见完整日志）' if len(output)>2400 else ''))
        if result.get('output_path'):
            lines.append('完整日志：'+str(result['output_path']))
    return '\n'.join(lines)


class PetProcess:
    """Own one optional renderer; its stdout is messages, never core state."""
    def __init__(self, command, events, log_path, *, env=None):
        self.events = events
        self.closing = False
        self.ready_event = None
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = log_path.open('a', encoding='utf-8')
        try:
            from .supervisor import ProcessTree
            self.tree = ProcessTree(command, cwd=Path(__file__).resolve().parents[1], env=dict(os.environ) if env is None else env,
                                    pipes=True, text=True, stderr_file=self.log)
            self.process = self.tree.process
        except Exception:
            self.log.close()
            raise
        self.reader = threading.Thread(target=self._read, name='companion-pet-events', daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            if line.startswith('@companion-pet '):
                try:
                    event = json.loads(line[len('@companion-pet '):])
                    if event.get('type') == 'ready':
                        self.ready_event = event
                    self.events.put(dict(type='pet', sender=self, event=event))
                except (ValueError, TypeError):
                    continue
            else:
                self.log.write(line)
                self.log.flush()
        if not self.closing:
            self.events.put(dict(type='pet', sender=self, event=dict(type='closed')))

    def send(self, **event):
        if self.closing or self.process.poll() is not None:
            return
        try:
            self.process.stdin.write(json.dumps(event)+'\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def close(self):
        if self.closing:
            return
        self.send(type='close')
        self.closing = True
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        self.tree.stop()
        self.reader.join(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout):
            try:
                pipe.close()
            except OSError:
                pass  # The renderer may already have closed its Windows pipe.
        self.log.close()


class ServiceThread:
    def __new__(cls, cfg, home, *, companion_factory=None):
        settings = Path(home)/'data/companion/remote.json'
        if companion_factory is None and settings.is_file() and json.loads(settings.read_text('utf8')).get('enabled'):
            from .companion_remote import RemoteServiceThread
            return RemoteServiceThread(cfg, home)
        return super().__new__(cls)

    def __init__(self, cfg, home, *, companion_factory=None):
        self.events = queue.Queue()
        self.cfg, self.home = cfg, Path(home)
        self.factory = companion_factory
        self.loop = None
        self.service = None
        self.ready = False
        self._calls = set()
        self.stopping = False
        self.thread = threading.Thread(target=self._run, name='companion-service', daemon=True)
        self.thread.start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except Exception as error:
            self.events.put(dict(type='error', text=str(error)))
        finally:
            self.loop.close()
            self.events.put(dict(type='closed'))

    async def _serve(self):
        if self.factory is None:
            from .companion import Companion
            self.factory = Companion
        self._stop = asyncio.Event()
        try:
            self.service = self.factory(self.cfg, self.home, on_event=self.events.put)
            await self.service.start()
            self.ready = True
            self._snapshot()
            if not self.stopping:
                await self._stop.wait()
        finally:
            self.ready = False
            calls = list(self._calls)
            for task in calls:
                task.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            if self.service is not None:
                await self.service.close()
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _snapshot(self):
        options=dict(include_image_data=not getattr(self,'_sent_initial_snapshot',False)) if getattr(self.service,'supports_snapshot_images',False) else {}
        self.events.put(dict(type='snapshot', snapshot=self.service.snapshot(**options)))
        self._sent_initial_snapshot=True

    async def _invoke(self, method, args):
        task = asyncio.current_task()
        self._calls.add(task)
        try:
            if method == 'snapshot':
                self._snapshot()
                return
            # The core emits both user and assistant messages. Rendering the
            # chat return value here would show the same response twice.
            await getattr(self.service, method)(*args)
            self._snapshot()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.events.put(dict(type='error', text=str(error)))
        finally:
            self._calls.discard(task)

    def call(self, method, *args):
        if self.stopping or not self.ready or not self.loop or not self.loop.is_running():
            return False
        asyncio.run_coroutine_threadsafe(self._invoke(method, args), self.loop)
        return True

    def close(self):
        self.stopping = True
        if self.loop and self.loop.is_running() and hasattr(self, '_stop'):
            self.loop.call_soon_threadsafe(self._stop.set)


class Desktop:
    def __init__(self, cfg, home, *, companion_factory=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.home = Path(home).resolve()
        self.settings_path = self.home/'data/companion/desktop.json'
        try:
            self.settings = json.loads(self.settings_path.read_text('utf-8'))
        except (OSError, ValueError):
            self.settings = {}
        if not isinstance(self.settings, dict):
            self.settings = {}
        self.model_path = getattr(cfg, 'companion_model', '') or self.settings.get('model', '')
        self.explicit_model = bool(self.model_path)
        self.pet_process = None
        self.root = tk.Tk()
        self.root.title('陪伴 · 桌面伙伴')
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.configure(bg='#17263b')
        self.root.geometry(f'142x126+{self.root.winfo_screenwidth()-180}+{self.root.winfo_screenheight()-230}')
        try:
            self.root.wm_attributes('-transparentcolor', '#17263b')
        except tk.TclError:
            pass
        self.worker = ServiceThread(cfg, home, companion_factory=companion_factory)
        self.closing = False
        self.initial_history = False
        self.name = '伙伴'
        self.active = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value='正在醒来…')
        self._draw_pet()

        style = ttk.Style()
        style.theme_use('clam')
        style.configure('Companion.TNotebook', background='#101b2b', borderwidth=0)
        style.configure('Companion.TNotebook.Tab', background='#203148', foreground='#dbe7f6', padding=(16, 8))
        style.map('Companion.TNotebook.Tab', background=[('selected', '#304c69')])

        self.window = tk.Toplevel(self.root)
        self.window.title('陪伴 · 对话')
        self.window.geometry('880x640')
        self.window.minsize(640, 480)
        self.window.configure(bg='#101b2b')
        self.window.protocol('WM_DELETE_WINDOW', self.window.withdraw)
        header = tk.Frame(self.window, bg='#101b2b', padx=22, pady=18)
        header.pack(fill='x')
        self.heading = tk.Label(header, text='在这里，慢慢说', font=('Microsoft YaHei UI', 18, 'bold'), bg='#101b2b', fg='#edf5ff')
        self.heading.pack(side='left')
        tk.Checkbutton(header, text='自主活动', variable=self.active, command=self.toggle,
            bg='#101b2b', fg='#b8cee7', activebackground='#101b2b', activeforeground='#edf5ff',
            selectcolor='#203148', font=('Microsoft YaHei UI', 10)).pack(side='right')
        tk.Label(self.window, textvariable=self.status, bg='#101b2b', fg='#89accc', anchor='w',
            padx=24, pady=3, font=('Microsoft YaHei UI', 10)).pack(fill='x')
        tabs = ttk.Notebook(self.window, style='Companion.TNotebook')
        chat_page = tk.Frame(tabs, bg='#162438')
        activity_page = tk.Frame(tabs, bg='#162438')
        state_page = tk.Frame(tabs, bg='#162438')
        tabs.add(chat_page, text=' 对话 ')
        tabs.add(activity_page, text=' 活动 ')
        tabs.add(state_page, text=' 近况与用量 ')
        self.chat_log = self._text(chat_page)
        self.activity_log = self._text(activity_page)
        self.state_log = self._text(state_page)
        self.chat_log.tag_configure('user', foreground='#8ecaff', spacing1=12, spacing3=12)
        self.chat_log.tag_configure('assistant', foreground='#ecf3fb', spacing1=12, spacing3=12)
        self.chat_log.tag_configure('error', foreground='#ffbcad')
        composer = tk.Frame(self.window, bg='#101b2b', padx=20, pady=8)
        self.entry = tk.Text(composer, height=3, wrap='word', bg='#203148', fg='#f1f6fc',
            insertbackground='#f1f6fc', relief='flat', padx=12, pady=10, font=('Microsoft YaHei UI', 11))
        self.entry.pack(side='left', fill='both', expand=True)
        self.entry.bind('<Return>', self._enter)
        tk.Button(composer, text='发送 ↗', command=self.send, bg='#5fc7b6', fg='#102b2a',
            activebackground='#8cddce', relief='flat', padx=18, pady=12,
            font=('Microsoft YaHei UI', 11, 'bold')).pack(side='right', padx=(12, 0))
        tk.Label(self.window, text='Enter 发送 · Shift + Enter 换行 · 关闭窗口后伙伴仍在桌面',
            font=('Microsoft YaHei UI', 9), bg='#101b2b', fg='#829bb6', anchor='w',
            padx=22, pady=6).pack(side='bottom', fill='x')
        composer.pack(side='bottom', fill='x')
        tabs.pack(fill='both', expand=True, padx=20, pady=(12, 8))
        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label='打开对话', command=self.show)
        self.menu.add_checkbutton(label='自主活动', variable=self.active, command=self.toggle)
        self.menu.add_command(label='选择 Live2D 模型…', command=self.choose_model)
        self.model_menu = tk.Menu(self.menu, tearoff=False)
        self.menu.add_cascade(label='切换已装形象', menu=self.model_menu)
        self.menu.add_command(label='形象放大', command=lambda: self.scale_pet(1.15))
        self.menu.add_command(label='形象缩小', command=lambda: self.scale_pet(1/1.15))
        tk.Button(header, text='外观…', command=lambda: self._popup_menu(
            header.winfo_rootx()+header.winfo_width()-160, header.winfo_rooty()+44),
            bg='#203148', fg='#dbe7f6', relief='flat', padx=10, pady=4).pack(side='right', padx=10)
        self.menu.add_separator()
        self.menu.add_command(label='退出伙伴', command=self.exit)
        model_bar = tk.Frame(self.window, bg='#101b2b')
        model_bar.pack(fill='x', padx=22, pady=4, before=tabs)
        tk.Label(model_bar, text='桌面形象', bg='#101b2b', fg='#b8cee7',
                 font=('Microsoft YaHei UI', 10)).pack(side='left', padx=(0, 10))
        self.model_choices = {}
        self.model_picker = ttk.Combobox(model_bar, state='readonly', width=30)
        self.model_picker.pack(side='left')
        self.model_picker.bind('<<ComboboxSelected>>', lambda event: self.select_model(
            self.model_choices[self.model_picker.get()]) if self.model_picker.get() in self.model_choices else None)
        tk.Button(model_bar, text='添加模型…', command=self.choose_model, bg='#203148', fg='#dbe7f6',
                  relief='flat', padx=10, pady=3).pack(side='left', padx=8)
        self._refresh_model_menu()
        self.root.bind('<Button-3>', self._menu)
        self.root.protocol('WM_DELETE_WINDOW', self.exit)
        self.root.after(100, self._poll)
        self.root.after(5000, self._refresh)
        if self.model_path and self.explicit_model:
            self.root.after(200, self.start_pet)

    def _save_settings(self):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        recent = [str(self.model_path), *self.settings.get('recent_models', [])]
        self.settings.update(recent_models=list(dict.fromkeys(p for p in recent if p))[:12])
        if self.explicit_model:
            self.settings['model'] = str(self.model_path)
        self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), 'utf-8')

    def choose_model(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.window, title='选择 Live2D 形象',
            filetypes=[('Live2D 模型', '*.model3.json'), ('JSON 文件', '*.json')])
        if path:
            self.select_model(path)

    def select_model(self, path):
        self.explicit_model = True
        self.model_path = str(path)
        self._save_settings()
        self.start_pet()

    def _refresh_model_menu(self):
        paths = [Path(p) for p in [self.model_path, *self.settings.get('recent_models', [])] if p]
        for folder in {self.home/'data/companion-models', Path(__file__).resolve().parents[1]/'data/companion-models'}:
            if folder.is_dir():
                paths.extend(folder.rglob('*.model3.json'))
        self.model_menu.delete(0, 'end')
        self.model_choices = {}
        count = 0
        for path in dict.fromkeys(p.resolve() for p in paths if p.is_file()):
            label = next((MODEL_NAMES[part.lower()] for part in path.parts if part.lower() in MODEL_NAMES),
                         path.stem.replace('.model3', ''))
            mark = '✓ ' if str(path) == str(self.model_path) else ''
            self.model_menu.add_command(label=mark+label, command=lambda p=path: self.select_model(p))
            if label in self.model_choices:
                label += ' · '+path.parent.name
            self.model_choices[label] = str(path)
            if str(path) == str(self.model_path):
                self.model_picker.set(label)
            count += 1
        if not count:
            self.model_menu.add_command(label='尚无已安装模型，请选择模型文件', state='disabled')
            self.model_picker.set('尚无已安装形象')
        self.model_picker.configure(values=list(self.model_choices))
        if not self.explicit_model:
            preferred = preferred_model(self.model_choices.values())
            if preferred:
                changed = preferred != str(self.model_path)
                self.model_path = preferred
                self.model_picker.set(next(label for label, path in self.model_choices.items() if path==preferred))
                if changed:
                    self.root.after_idle(self.start_pet)

    def scale_pet(self, factor):
        self.settings['height'] = round(max(220, min(900, self.settings.get('height', 460)*factor)))
        self._save_settings()
        if self.pet_process:
            self.pet_process.send(type='resize', height=self.settings['height'])

    def start_pet(self):
        if self.closing:
            return
        if self.pet_process:
            self.pet_process.close()
            self.pet_process = None
        path = Path(self.model_path)
        if not path.is_absolute():
            path = self.home/path
        if not path.is_file():
            self.status.set('形象模型不存在，可在“外观”中重新选择')
            self.root.deiconify()
            return
        command = [sys.executable, '-u', '-m', 'life.companion_pet', '--model', str(path),
                   '--height', str(self.settings.get('height', 460))]
        try:
            self.pet_process = PetProcess(command, self.worker.events, self.home/'data/companion/renderer.log')
        except OSError as error:
            self.status.set('形象未能启动，详情见活动')
            self._append(self.activity_log, '形象启动失败：'+str(error))
            self.root.deiconify()

    def _pet_event(self, event):
        kind = event.get('type')
        if kind == 'ready':
            self.root.withdraw()
            self.status.set('Live2D 形象已就绪')
        elif kind == 'open_chat':
            self.show()
        elif kind == 'choose_model':
            self.show()
            self._popup_menu(self.window.winfo_rootx()+80, self.window.winfo_rooty()+80)
        elif kind == 'scale':
            self.scale_pet(event.get('factor', 1))
        elif kind == 'toggle':
            self.active.set(not self.active.get())
            self.toggle()
        elif kind == 'exit':
            self.exit()
        elif kind in {'error', 'closed'} and not self.closing:
            self.root.deiconify()
            self._append(self.activity_log, 'Live2D：'+event.get('text', '形象进程已停止；聊天仍然可用。'))
            self.status.set('形象未就绪，详情见活动或 renderer.log')

    def _text(self, parent):
        tk = self.tk
        scroll = tk.Scrollbar(parent)
        scroll.pack(side='right', fill='y')
        box = tk.Text(parent, wrap='word', bg='#162438', fg='#dbe7f6', relief='flat',
            padx=20, pady=14, font=('Microsoft YaHei UI', 11), state='disabled',
            yscrollcommand=scroll.set, spacing3=8)
        box.pack(fill='both', expand=True)
        scroll.configure(command=box.yview)
        return box

    def _draw_pet(self):
        self.pet = self.tk.Canvas(self.root, width=142, height=126, bg='#17263b', highlightthickness=0)
        self.pet.pack()
        self.pet.create_oval(23, 92, 126, 110, fill='#25364b', outline='')
        self.pet.create_polygon(40, 61, 13, 39, 13, 84, fill='#53b7c1', outline='#c5f2ec', width=2)
        self.pet.create_oval(32, 27, 131, 95, fill='#81d4d3', outline='#d0f7ed', width=2)
        self.pet.create_polygon(66, 31, 81, 15, 94, 30, fill='#53b7c1', outline='')
        self.pet.create_oval(101, 46, 111, 57, fill='#17364b', outline='')
        self.pet.create_oval(103, 47, 106, 50, fill='white', outline='')
        self.pet.create_arc(108, 57, 122, 72, start=190, extent=130, style='arc', outline='#27556a', width=2)
        self.pet.create_oval(100, 64, 111, 69, fill='#efafb3', outline='')
        self.pet.create_text(77, 116, text='双击，聊一会儿', fill='#d6e9ee', font=('Microsoft YaHei UI', 9))
        self.pet.bind('<ButtonPress-1>', self._drag_start)
        self.pet.bind('<B1-Motion>', self._drag)
        self.pet.bind('<Double-Button-1>', lambda event: self.show())

    def _drag_start(self, event):
        self.drag_offset = (event.x_root-self.root.winfo_x(), event.y_root-self.root.winfo_y())

    def _drag(self, event):
        x, y = self.drag_offset
        self.root.geometry(f'+{max(0,event.x_root-x)}+{max(0,event.y_root-y)}')

    def _menu(self, event):
        self._popup_menu(event.x_root, event.y_root)

    def _popup_menu(self, x, y):
        self._refresh_model_menu()
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.entry.focus_set()
        self.worker.call('snapshot')

    def _enter(self, event):
        if event.state & 1:
            return None
        self.send()
        return 'break'

    def send(self):
        text = self.entry.get('1.0', 'end').strip()
        if text and self.worker.call('chat', text):
            self.entry.delete('1.0', 'end')
            self.status.set('正在听，也在想…')
        elif text:
            self.status.set('伙伴还在启动，文字已保留，请稍后发送')

    def toggle(self):
        if self.worker.call('set_active', self.active.get()):
            self.status.set('自主活动已开启' if self.active.get() else '自主活动已暂停，仍可聊天')

    def _append(self, box, text, tag=None):
        box.configure(state='normal')
        box.insert('end', text+'\n', tag or ())
        box.configure(state='disabled')
        box.see('end')

    def _state(self, snapshot):
        self.name = snapshot.get('name') or '伙伴'
        self.window.title(self.name+' · 陪伴')
        self.heading.configure(text=self.name+' · 在这里，慢慢说')
        self.active.set(snapshot.get('active', True))
        if not self.initial_history:
            for row in snapshot.get('history', []):
                if row.get('role') in {'user', 'assistant'}:
                    self._message(row)
            self.initial_history = True
            self.status.set('在这里 · 双击桌宠随时打开对话')
            # Startup events may already be queued; the initial snapshot
            # includes them along with historical activity.
            self.activity_log.configure(state='normal')
            self.activity_log.delete('1.0', 'end')
            self.activity_log.configure(state='disabled')
            for row in reversed(snapshot.get('activity', [])):
                self._append(self.activity_log, activity_text(row))
        lines = ['正在在意的事', '']
        for project in snapshot.get('projects', []):
            lines.append('• '+str(project.get('goal') or project.get('title') or project.get('text') or '未命名打算'))
            for key, label in [('status', '状态'), ('next_step', '下一步'), ('checkpoint', '进展')]:
                if project.get(key):
                    lines.append(f'  {label}：{project[key]}')
        if len(lines) == 2:
            lines.append('暂时没有未完的打算。')
        lines.extend(['', '自己的笔记', ''])
        for note in snapshot.get('notes', [])[:12]:
            lines.append('• '+str(note.get('text', '') if isinstance(note, dict) else note))
        lines.extend(['', '模型实际报告用量', ''])
        labels = {'input_tokens':'输入', 'output_tokens':'输出', 'cached_tokens':'缓存命中', 'cached_input_tokens':'缓存命中',
            'cache_read_tokens':'缓存命中', 'calls':'调用次数', 'total_tokens':'合计',
            'unknown_calls':'用量未报告的调用', 'seconds':'耗时（秒）'}
        usage = snapshot.get('usage') or {}
        lines.extend(f'{labels.get(key,key)}：{value if value is not None else "未知"}' for key,value in usage.items())
        if not usage:
            lines.append('尚无已报告用量；缺失数据不按零费用计算。')
        self.state_log.configure(state='normal')
        self.state_log.delete('1.0', 'end')
        self.state_log.insert('end', '\n'.join(lines))
        self.state_log.configure(state='disabled')

    def _message(self, event):
        role = event.get('role', 'assistant')
        text = event.get('text', event.get('content', ''))
        if text and role in {'user', 'assistant'}:
            self._append(self.chat_log, ('你' if role == 'user' else self.name)+'\n'+str(text), role)

    def _poll(self):
        try:
            while True:
                event = self.worker.events.get_nowait()
                kind = event.get('type')
                if kind == 'snapshot':
                    self._state(event['snapshot'])
                elif kind == 'message':
                    self._message(event)
                    self.status.set('在这里')
                elif kind == 'status':
                    self.status.set(event.get('text', '在这里'))
                elif kind == 'activity':
                    self._append(self.activity_log, activity_text(event))
                elif kind == 'error':
                    self.status.set('遇到一点问题，详情见活动')
                    self._append(self.activity_log, '遇到问题：'+event.get('text', '未知错误'))
                elif kind == 'usage':
                    self.worker.call('snapshot')
                elif kind == 'pet' and event.get('sender') is self.pet_process:
                    self._pet_event(event['event'])
                elif kind == 'closed' and not self.closing:
                    self.status.set('角色服务已停止，请退出后重新启动')
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(100, self._poll)

    def _refresh(self):
        if not self.closing:
            self.worker.call('snapshot')
            self._refresh_model_menu()
            self.root.after(5000, self._refresh)

    def exit(self):
        if self.closing:
            return
        self.closing = True
        self.status.set('正在保存并结束活动…')
        self.entry.configure(state='disabled')
        self.worker.close()
        if self.pet_process:
            self.pet_process.close()
            self.pet_process = None
        self._await_exit()

    def _await_exit(self):
        if self.worker.thread.is_alive():
            self.root.after(100, self._await_exit)
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_desktop(cfg, home, *, companion_factory=None):
    from .companion_shell import available, run_coopanion
    if available():
        return run_coopanion(cfg, home, companion_factory=companion_factory)
    Desktop(cfg, home, companion_factory=companion_factory).run()
