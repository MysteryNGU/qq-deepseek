"""Local connection settings; secrets are entered here, never in chat."""
import asyncio
import json
from pathlib import Path
import queue
import threading
import webbrowser


def open_connections(home):
    import tkinter as tk
    from tkinter import ttk
    from .role_config import load_config
    from .companion_town import TownConfig, configure_town, JOIN
    from .companion_remote import RemoteConfig, configure_remote, remote_call
    from .companion_bilibili import Bilibili
    from .companion_speech import SpeechPlayer, speech_status, save_speech_settings

    home = Path(home).resolve()
    cfg = load_config(home/'config.toml')
    town, remote = TownConfig.load(home), RemoteConfig.load(home)
    root = tk.Tk()
    root.title('小鱼 · 连接设置')
    root.geometry('660x640')
    root.minsize(600, 540)
    style = ttk.Style(root)
    if 'vista' in style.theme_names(): style.theme_use('vista')
    style.configure('TLabel', font=('Microsoft YaHei UI', 10))
    style.configure('TButton', padding=6)
    tabs = ttk.Notebook(root)
    tabs.pack(fill='both', expand=True, padx=18, pady=18)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name='connection-settings')
    thread.start()
    results = queue.Queue()
    bili = Bilibili(cfg, home)
    preview_speech = SpeechPlayer(cfg, home)
    closed = False

    def job(coro, callback, status):
        status.set('正在连接…')
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        future.add_done_callback(lambda done: results.put((done, callback, status)))

    def drain():
        if closed: return
        while not results.empty():
            future, callback, status = results.get_nowait()
            try: callback(future.result())
            except Exception:
                # Provider exceptions can contain URLs/credentials; keep them out of UI logs.
                status.set('连接未完成，请检查配置和网络后重试。')
        root.after(100, drain)

    def page(title, intro):
        frame = ttk.Frame(tabs, padding=18)
        tabs.add(frame, text=title)
        ttk.Label(frame, text=intro, wraplength=560, justify='left').pack(anchor='w', pady=(0, 18))
        return frame

    def field(frame, label, value='', secret=False):
        ttk.Label(frame, text=label).pack(anchor='w', pady=(10, 4))
        var = tk.StringVar(value=value)
        ttk.Entry(frame, textvariable=var, show='●' if secret else '').pack(fill='x')
        return var

    town_page = page('鱼缸小镇', '先在小镇官网注册或登录，创建她的名字和公开介绍，再把接入码填在这里。账号密码留在官网。')
    ttk.Button(town_page, text='打开小镇申请入住页面', command=lambda: webbrowser.open(JOIN)).pack(anchor='w')
    town_code = field(town_page, '接入码（fbo_ 开头；留空保留已保存的码）', secret=True)
    town_enabled = tk.BooleanVar(value=town.enabled)
    ttk.Checkbutton(town_page, text='启用小镇连接', variable=town_enabled).pack(anchor='w', pady=12)
    town_status = tk.StringVar(value='已有本地接入码。' if town.code else '尚未配置接入码。')

    def save_town():
        code, enabled = town_code.get().strip(), town_enabled.get()
        try:
            current_remote = RemoteConfig.load(home)
            if current_remote.enabled:
                job(remote_call(current_remote, 'configure_town', code, enabled), town_saved, town_status)
            else:
                town_saved(configure_town(home, code, enabled=enabled))
        except ValueError:
            town_status.set('接入码格式不正确，请粘贴完整的 fbo_ 接入码。')

    def town_saved(_value):
        town_code.set('')
        town_status.set('已保存。运行中的角色会加载新配置；没有运行时，下次启动生效。')

    ttk.Button(town_page, text='保存小镇设置', command=save_town).pack(anchor='w', pady=6)
    ttk.Label(town_page, textvariable=town_status, wraplength=550).pack(anchor='w', pady=10)

    bili_page = page('B 站', '她使用已有的 B 站后端。先检查登录状态；需要登录时，用你的哔哩哔哩手机 App 扫码并确认。无需填写密码。')
    bili_status = tk.StringVar(value='点击检查登录，读取当前账号状态。')
    qr = ttk.Label(bili_page)

    def payload(result):
        if result.get('isError'): raise ValueError('bilibili_request_failed')
        text = '\n'.join(item.get('text', '') for item in result.get('content', []) if item.get('type')=='text')
        return json.loads(text)

    async def bili_call(op, arguments):
        connection = RemoteConfig.load(home)
        if connection.enabled:
            return await remote_call(connection, 'bilibili_call', op, arguments)
        return await bili.call(op, arguments)

    def checked(result):
        data = payload(result)
        # Backend formats differ; show a bounded human status, never raw credential objects.
        okay = data.get('valid') is True or data.get('is_login') is True or data.get('logged_in') is True or data.get('status') in ('success', 'logged_in', 'valid', 'done', 'already_logged_in')
        bili_status.set('B 站已登录。' if okay else '尚未确认登录，请扫码登录或重新检查。')

    def login_ready(result):
        data = payload(result)
        if data.get('status')=='already_logged_in':
            checked(result)
            return
        encoded = data.get('qrcode_image', '')
        if encoded.startswith('data:image/png;base64,'):
            picture = tk.PhotoImage(data=encoded.split(',', 1)[1])
            qr.configure(image=picture)
            qr.image = picture
        elif data.get('qrcode_file'):
            picture = tk.PhotoImage(file=data['qrcode_file'])
            qr.configure(image=picture)
            qr.image = picture
        else:
            bili_status.set('没有取得二维码，请稍后重试。')
            return
        bili_status.set('请用哔哩哔哩 App 扫码确认，然后点击“完成扫码，检查结果”。')

    buttons = ttk.Frame(bili_page)
    buttons.pack(fill='x')
    ttk.Button(buttons, text='检查登录', command=lambda: job(bili_call('bili_check_credential', {}), checked, bili_status)).pack(side='left', padx=(0, 8))
    ttk.Button(buttons, text='生成登录二维码', command=lambda: job(bili_call('bili_login', {}), login_ready, bili_status)).pack(side='left')
    qr.pack(pady=12)
    ttk.Button(bili_page, text='完成扫码，检查结果', command=lambda: job(bili_call('bili_login_check', {}), checked, bili_status)).pack(anchor='w')
    ttk.Label(bili_page, textvariable=bili_status, wraplength=550).pack(anchor='w', pady=12)

    cloud_page = page('云端连接', '服务器部署好后填写连接地址和连接码。启用后，桌面连接云端的同一个角色；断线时不会另启动一个本地角色。还没有服务器时保持关闭。')
    remote_url = field(cloud_page, '连接地址（例如 SSH 隧道 http://127.0.0.1:8765）', remote.url)
    remote_token = field(cloud_page, '连接码（留空保留已保存的码）', secret=True)
    remote_enabled = tk.BooleanVar(value=remote.enabled)
    ttk.Checkbutton(cloud_page, text='启动桌宠时连接云端角色', variable=remote_enabled).pack(anchor='w', pady=12)
    remote_status = tk.StringVar(value='设置保存后，关闭并重新打开桌宠生效。')

    def save_remote():
        try:
            configure_remote(home, remote_url.get().strip(), remote_token.get().strip(), enabled=remote_enabled.get())
            remote_token.set('')
            remote_status.set('已保存。关闭并重新打开桌宠后生效。')
        except ValueError:
            remote_status.set('请检查连接地址和连接码；公网连接使用 HTTPS，本机隧道可使用 HTTP。')

    def check_remote():
        try:
            saved = RemoteConfig.load(home)
            check = RemoteConfig(url=remote_url.get().strip(), token=remote_token.get().strip() or saved.token, enabled=True)
            job(remote_call(check, 'snapshot'), lambda _: remote_status.set('连接成功，远端角色可访问。'), remote_status)
        except ValueError:
            remote_status.set('请先填写正确的地址和连接码。')

    ttk.Button(cloud_page, text='测试连接', command=check_remote).pack(anchor='w', pady=5)
    ttk.Button(cloud_page, text='保存云端设置', command=save_remote).pack(anchor='w', pady=5)
    ttk.Label(cloud_page, textvariable=remote_status, wraplength=550).pack(anchor='w', pady=10)

    voice_page = page('说话声音', '她发出聊天气泡时朗读文字；你插话时停止上一段。语音不需要再调用聊天模型。')
    speech = speech_status(cfg, home)
    speech_enabled = tk.BooleanVar(value=speech['enabled'])
    ttk.Checkbutton(voice_page, text='开启语音朗读', variable=speech_enabled).pack(anchor='w')
    voices = [{'id':'zh-CN-XiaoyiNeural','name':'小艺 · 即时在线语音'}, *speech['voices']]
    names = {voice['name']:voice['id'] for voice in voices}
    selected = next((v['name'] for v in voices if v['id']==speech['voice']), speech['voice'])
    voice_name = tk.StringVar(value=selected)
    ttk.Label(voice_page, text='音色').pack(anchor='w', pady=(16, 4))
    ttk.Combobox(voice_page, textvariable=voice_name, values=list(names), state='readonly').pack(fill='x')
    volume = tk.DoubleVar(value=speech['volume'])
    ttk.Label(voice_page, text='音量').pack(anchor='w', pady=(16, 4))
    ttk.Scale(voice_page, from_=0, to=1, variable=volume).pack(fill='x')
    speech_message = tk.StringVar(value='本地克隆音色首次加载较慢；小艺适合即时聊天。')
    def save_voice():
        save_speech_settings(home, speech_enabled.get(), names.get(voice_name.get(), voice_name.get()), volume.get())
        if not speech_enabled.get(): preview_speech.interrupt()
        speech_message.set('已保存，下一条语音生效。')
    def preview_voice():
        save_voice()
        preview_speech.say('你好，我在这里。现在可以听见我的声音了。')
    ttk.Button(voice_page, text='保存声音设置', command=save_voice).pack(anchor='w', pady=(16, 5))
    ttk.Button(voice_page, text='试音', command=preview_voice).pack(anchor='w', pady=5)
    ttk.Label(voice_page, textvariable=speech_message, wraplength=550).pack(anchor='w', pady=10)

    def close():
        nonlocal closed
        if closed: return
        closed = True
        preview_speech.close()
        future = asyncio.run_coroutine_threadsafe(bili.close(), loop)
        future.add_done_callback(lambda _: loop.call_soon_threadsafe(loop.stop))
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)
    drain()
    root.mainloop()
