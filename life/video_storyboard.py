"""Install/check the optional native video bundle in the Bot's SDK profile.

Run with the Bot stopped. No model requests or QQ delivery are performed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .role_config import load_config
from .services.dsh_bridge import DshBridge, DshBridgeError, PROFILE_NAME
from .tool_boundary import outcome_ok

REVISION = 'ebd976d9fd8a7ec9ea6c624437d1926b3047b49e'
PACKAGE = 'dsh-video-storyboard'
PACKAGE_SPEC = f'github:SAOS-3624/{PACKAGE}#{REVISION}'
TOOLS = {'video_probe', 'video_storyboard', 'video_zoom', 'video_locate'}


def media_binary(name, home, env=None):
    """Follow the pinned plugin's binaries.js search order, including overrides."""
    env = os.environ if env is None else env
    upper = name.upper()
    for key in (f'DSH_{upper}_PATH', f'{upper}_PATH'):
        if env.get(key):
            return env[key]  # A bad explicit override must fail, not silently fall back.
    filename = name + ('.exe' if os.name == 'nt' else '')
    candidates = []
    if env.get(f'DSH_{upper}_DIR'):
        candidates.append(Path(env[f'DSH_{upper}_DIR']) / filename)
    for root in (Path(home), Path(home).parent):
        candidates.extend((root / 'tools/ffmpeg/bin' / filename, root / 'bin' / filename))
    return next((str(p) for p in candidates if p.is_file()), None) or shutil.which(name, path=env.get('PATH'))


def preflight(bridge):
    problems = []
    for relative in ('apps/cli/src/bin.ts', 'node_modules/tsx/package.json',
                     'packages/core/tools/lib/index.js'):
        if not (bridge.dsh_root / relative).is_file():
            problems.append(f'DSH 尚未准备好：{bridge.dsh_root / relative}')
    try:
        bridge._node_exe()
    except DshBridgeError as error:
        problems.append(str(error))
    binaries = {}
    for name in ('ffmpeg', 'ffprobe'):
        binary = media_binary(name, bridge.home)
        if not binary:
            problems.append(f'缺少 {name}；放入 Bot DSH home/tools/ffmpeg/bin 或配置 DSH_{name.upper()}_PATH')
            continue
        try:
            result = subprocess.run([binary, '-version'], capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ValueError(f'exit {result.returncode}')
            binaries[name] = binary
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            problems.append(f'{name} 不可执行：{error}')
    return dict(ready=not problems, problems=problems, binaries=binaries,
                profile=str(bridge.profile_dir()), revision=REVISION)


def install(bridge):
    # Installation belongs to DSH's package manager. It updates both dependencies
    # and bundles and preserves the user/plugin-owned patch and lockfile.
    bridge.ensure_profile()
    code, output = bridge._run_cli(['plugin', '--profile', PROFILE_NAME, 'add', PACKAGE_SPEC])
    if code:
        raise DshBridgeError(f'视频插件安装失败（exit {code}）：{output[-4000:]}')
    manifest = json.loads((bridge.profile_dir() / 'package.json').read_text('utf-8-sig'))
    if PACKAGE not in manifest.get('dependencies', {}) or PACKAGE not in manifest.get('dsh', {}).get('profile', {}).get('bundles', []):
        raise DshBridgeError('安装命令完成，但 SDK profile 没有同时登记依赖和 bundle')
    return verify_registry(bridge)


def verify_registry(bridge):
    bridge.hello()  # Verifies this is the Bot-owned SDK profile.
    registered = {row.get('name') for row in bridge.tools()}
    missing = TOOLS - registered
    if missing:
        raise DshBridgeError('插件未完整激活，缺少：' + ', '.join(sorted(missing)))
    return dict(registered_tools=sorted(TOOLS), actual_video_call_verified=False)


def has_image(value):
    if isinstance(value, dict):
        return (value.get('type') == 'image' and bool(value.get('attachment'))) or any(has_image(v) for v in value.values())
    return isinstance(value, list) and any(has_image(v) for v in value)


def smoke(bridge, ffmpeg):
    """Exercise real native tools with generated media; never uses private videos/ASR."""
    result = verify_registry(bridge)
    folder = bridge.root / 'data/tmp'
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='video-storyboard-', dir=folder) as tmp:
        video = Path(tmp) / 'fixture.mp4'
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=320x180:rate=10', '-t', '4', '-c:v', 'mpeg4',
                        '-y', str(video)], check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        transcript = Path(tmp) / 'fixture.srt'
        transcript.write_text('1\n00:00:01,000 --> 00:00:02,000\nqq storyboard fixture\n', encoding='utf-8')
        for tool, args in (
            ('video_probe', dict(input=str(video))),
            ('video_storyboard', dict(input=str(video), cells=4, sheets=1, transcript='off')),
            ('video_zoom', dict(input=str(video), **{'from': 1, 'to': 3}, cells=2, transcript='off')),
            ('video_locate', dict(input=str(video), query='qq storyboard fixture', transcriptFile=str(transcript))),
        ):
            snap = bridge.execute(tool, args)
            if not outcome_ok(snap):
                raise DshBridgeError(f'{tool} 实际调用未成功：{json.dumps(snap, ensure_ascii=False)[:2000]}')
            if tool in {'video_storyboard', 'video_zoom'} and not has_image(snap.get('result')):
                raise DshBridgeError(f'{tool} 未返回 DSH 图片附件；不能视作视频视觉接入成功')
            if tool == 'video_locate':
                text = '\n'.join(block.get('text', '') for block in snap.get('result', {}).get('content', [])
                                 if block.get('type') == 'text')
                if '00:01.00-00:02.00' not in text or 'qq storyboard fixture' not in text:
                    raise DshBridgeError('video_locate 没有返回合成字幕的实际时间定位')
    return dict(result, actual_video_call_verified=True, asr_verified=False, qq_messages=0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'install', 'verify', 'smoke'))
    parser.add_argument('--config', type=Path, default=Path('config.toml'))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    bridge = DshBridge(load_config(args.config.resolve()), root)
    try:
        status = preflight(bridge)
        if args.action == 'check' or not status['ready']:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status['ready'] else 1
        if args.action == 'install':
            result = install(bridge)
        elif args.action == 'verify':
            result = verify_registry(bridge)
        else:
            result = smoke(bridge, status['binaries']['ffmpeg'])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DshBridgeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps(dict(ok=False, error=str(error)), ensure_ascii=False))
        return 1
    finally:
        bridge.close()


if __name__ == '__main__':
    raise SystemExit(main())
