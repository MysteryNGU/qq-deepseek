"""Shared desktop, console and scheduler entry for an existing character home."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import threading
import traceback

ROOT = Path(__file__).resolve().parent


class AlreadyRunning(RuntimeError):
    """Another companion or legacy QQ worker owns this character home."""


def resolve_home(home=None, *, root=ROOT):
    """Select an existing home; registered Xiaoyu is the default when available."""
    root = Path(root).resolve()
    if home is None:
        registry = root / 'data/characters/registry.json'
        characters = json.loads(registry.read_text('utf-8')) if registry.is_file() else {}
        preferred = root / 'data/characters/xiaoyu'
        home = preferred if 'xiaoyu' in characters and (preferred / 'config.toml').is_file() else root
    home = Path(home).expanduser().resolve()
    if not (home / 'config.toml').is_file():
        raise FileNotFoundError(f'人物目录缺少 config.toml：{home}')
    return home


class HomeLock:
    """Hold the same per-home OS lock used by the legacy QQ worker."""
    def __init__(self, home):
        self.path = Path(home) / 'data/bot.lock'
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        stream.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise AlreadyRunning('这个人物已有桌宠或 QQ 程序运行，请先正常退出该程序。') from None
        self.stream = stream
        return self

    def __exit__(self, *_args):
        self.stream.close()
        self.stream = None


def write_console(text):
    if sys.stdout is not None:
        print(text, flush=True)


async def run_service(cfg, home, *, console=False):
    from life.companion import Companion

    def event(value):
        if value.get('type') in {'activity', 'error', 'project', 'action_result'}:
            write_console(f"[{value['type']}] {value.get('text', '')}")

    service = Companion(cfg, home, on_event=event)
    try:
        await service.start()
        if getattr(cfg, 'companion_autonomy', None) is False:
            await service.set_active(False)
        if not console:
            await asyncio.Event().wait()
            return
        write_console(f'{cfg.name} · {home}\n/quit 退出；/pause 暂停自主活动；/resume 恢复；/status 查看状态')
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def read_lines():
            # A blocked console read must not hold the scheduler process open on Ctrl+C.
            while True:
                try:
                    line = sys.stdin.readline()
                    loop.call_soon_threadsafe(queue.put_nowait, line if line else None)
                except (OSError, RuntimeError):
                    return
                if not line:
                    return

        threading.Thread(target=read_lines, name='companion-console', daemon=True).start()
        while True:
            write_console('你 >')
            line = await queue.get()
            if line is None or line.strip().lower() in {'/quit', '/exit'}:
                break
            text = line.strip()
            if not text:
                continue
            if text == '/pause':
                await service.set_active(False)
                write_console('自主活动已暂停。')
            elif text == '/resume':
                await service.set_active(True)
                write_console('自主活动已恢复。')
            elif text == '/status':
                write_console(json.dumps(service.snapshot(), ensure_ascii=False, indent=2))
            else:
                write_console(f'{cfg.name} > {await service.chat(text)}')
    finally:
        await service.close()


def report_error(home, error, *, console):
    """Record stack locations without logging configuration or provider credentials."""
    path = Path(home) / 'data/logs/companion-launcher.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    message = str(error) if isinstance(error, (AlreadyRunning, FileNotFoundError)) else f'启动失败（{type(error).__name__}）'
    with path.open('a', encoding='utf-8') as output:
        output.write(message + '\n' + ''.join(traceback.format_list(traceback.extract_tb(error.__traceback__))) + '\n')
    message += f'\n记录：{path}'
    if console and sys.stderr is not None:
        print(message, file=sys.stderr)
    elif sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, '桌宠启动失败', 0x10)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='独立角色桌宠；所有入口共享同一个人物目录。')
    parser.add_argument('--home', type=Path, help='现有人物目录（内含 config.toml）')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--console', action='store_true', help='控制台聊天并继续自主调度')
    mode.add_argument('--headless', action='store_true', help='仅运行自主调度，Ctrl+C 退出')
    mode.add_argument('--serve', action='store_true', help='运行常驻角色服务，桌面断开后继续生活')
    mode.add_argument('--connections', action='store_true', help='打开小镇、B站和云端连接设置')
    parser.add_argument('--host', default='127.0.0.1', help='常驻服务监听地址')
    parser.add_argument('--port', type=int, default=8765, help='常驻服务端口')
    parser.add_argument('--token-file', type=Path, help='常驻服务连接密钥文件')
    mode.add_argument('--town-configure', action='store_true', help='在本机输入鱼缸小镇接入码，不连接小镇')
    mode.add_argument('--town-status', action='store_true', help='查看本机小镇配置，不显示接入码、不联网')
    mode.add_argument('--town-disable', action='store_true', help='关闭本机小镇接入，下次启动生效')
    parser.add_argument('--no-autonomy', action='store_true', help='启动后暂停自主活动')
    args = parser.parse_args(argv)
    home = ROOT
    try:
        home = resolve_home(args.home)
        if args.connections:
            from life.companion_connections import open_connections
            open_connections(home)
            return 0
        if args.town_configure or args.town_status or args.town_disable:
            from life.companion_town import TownConfig, configure_town, JOIN
            if args.town_status:
                write_console(json.dumps(TownConfig.load(home).public(), ensure_ascii=False, indent=2))
                return 0
            with HomeLock(home):
                if args.town_disable:
                    configure_town(home, enabled=False)
                    write_console('小镇接入已关闭，下次启动生效；接入码仍留在本机。')
                else:
                    import getpass
                    write_console('申请小镇账号和接入码：'+JOIN)
                    write_console('接入码只粘贴在本机，不要发进聊天。尚无账号可以直接回车。')
                    entered = getpass.getpass('小镇接入码（输入不显示）：').strip()
                    existing = TownConfig.load(home)
                    settings = configure_town(home, entered, enabled=bool(entered or existing.code))
                    write_console('小镇配置已保存在人物目录；没有接入码时不联网、不调用模型。')
                    write_console(json.dumps(settings, ensure_ascii=False, indent=2))
                return 0
        from life.role_config import load_config
        cfg = load_config(home / 'config.toml')
        if args.no_autonomy:
            cfg.companion_autonomy = False
        with HomeLock(home):
            if args.serve:
                from life.companion_remote import run_remote_service
                asyncio.run(run_remote_service(cfg, home, host=args.host, port=args.port, token_file=args.token_file))
            elif args.console or args.headless:
                asyncio.run(run_service(cfg, home, console=args.console))
            else:
                from life.companion_desktop import run_desktop
                run_desktop(cfg, home)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        report_error(home, error, console=args.console or args.headless or args.serve or args.town_configure or args.town_status or args.town_disable)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
