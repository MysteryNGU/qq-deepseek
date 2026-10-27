"""Local migration export, including secrets. Standard library only; no network."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import tomllib
import zipfile

SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
             '.mypy_cache', '.ruff_cache', '.pnpm-store', '.artifacts', '.dsh-build',
             'coverage', 'playwright-report', 'test-results'}
TRANSIENT = {'worker-health.json', 'watchdog.json', 'server.json', 'restart.request.json',
             'restart.receipt.json', 'stop.requested', 'channel_retry.json'}
DATA_SKIP = {'maintenance', 'tmp', 'cache', 'packages', 'models', 'napcat-setup',
             'media-feasibility', 'csv_export', 'request-samples', 'failures',
             'codex-consult-verification', 'logs', 'test-results'}
WEIGHTS = {'.gguf', '.ggml', '.safetensors', '.ckpt', '.onnx', '.pth', '.pt'}


def resolve(root, value):
    path = Path(value)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class Exporter:
    def __init__(self, root, destination):
        self.root, self.destination = root.resolve(), destination.resolve()
        self.files, self.excluded, self.roots, self.manual = {}, [], [], []
        self.missing = []

    def add(self, path, name):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            self.excluded.append(dict(path=str(path), reason='link_requires_manual_copy'))
            return
        if path.is_file():
            old = self.files.get(name)
            if old and old != path:raise ValueError('archive_path_collision: ' + name)
            self.files[name] = path

    def tree(self, path, prefix, *, skip_top=(), data=False):
        if not path.is_dir():return
        self.roots.append(dict(source=str(path), archive=prefix))
        for base, dirs, files in os.walk(path, followlinks=False):
            folder = Path(base)
            kept = []
            for name in sorted(dirs):
                child = folder/name
                if (name in SKIP_DIRS or folder == path and name in skip_top
                        or child.is_symlink() or getattr(child, 'is_junction', lambda: False)()
                        or child.resolve() == self.destination
                        or data and name.endswith('-integration')):
                    self.excluded.append(dict(path=str(child), reason='runtime_dependency_cache_or_link'))
                else:kept.append(name)
            dirs[:] = kept
            for name in sorted(files):
                child = folder/name
                if (child.suffix.lower() in WEIGHTS | {'.pyc', '.pyo', '.lock', '.pid', '.zip', '.7z'}
                        or name.endswith(('-wal', '-shm', '-journal')) or name in TRANSIENT
                        or name.endswith('.log') and name != 'conversation.log'):
                    self.excluded.append(dict(path=str(child), reason='weight_archive_log_or_process_state'))
                    continue
                relative = child.relative_to(path).as_posix()
                target = prefix+'/'+relative
                # Do not automatically replay deployment or administrative commands on a new PC.
                if data and (relative == 'life-control/controller.sqlite3' or relative.startswith('memory-admin/')):
                    target = 'recovery-only/'+target
                self.add(child, target)

    def configured(self, value, label, *, required=False):
        if not value:return
        path = resolve(self.root, value)
        if not path.exists():
            if required:raise FileNotFoundError(label + ': ' + str(path))
            self.missing.append(dict(setting=label, path=str(path)))
            return
        name = ('project/'+path.relative_to(self.root).as_posix() if path.is_relative_to(self.root)
                else 'external/config-assets/'+hashlib.sha256(str(path).encode()).hexdigest()[:12]+'/'+path.name)
        if path.is_dir():self.tree(path, name)
        else:self.add(path, name)
        self.roots.append(dict(setting=label, source=str(path), archive=name))

    def collect(self):
        config = tomllib.loads((self.root/'config.toml').read_text('utf-8'))
        # Current source and locally learned methods are included as well as private settings.
        self.tree(self.root, 'project', skip_top={'data', 'tools', 'napcat', 'napcat-onekey',
                  'backups', '.acceptance', 'ZenlessZoneZero-Auto'})
        for name in ('bot-watchdog.ps1', 'napcat-login.ps1'):
            self.add(self.root/'tools'/name, 'project/tools/'+name)
        self.tree(self.root/'data', 'project/data', skip_top=DATA_SKIP, data=True)
        self.configured(config.get('life_db', 'data/life.db'), 'life_db', required=True)
        self.configured(config.get('persona', 'persona.md'), 'persona', required=True)
        self.configured(config.get('consult_config'), 'consult_config')
        media = config.get('local_media', {})
        self.configured(media.get('preset_manifest'), 'local_media.preset_manifest', required=bool(media.get('enabled')))
        if media.get('preset_manifest'):
            manifest = resolve(self.root, media['preset_manifest'])
            if manifest.exists():
                for preset in json.loads(manifest.read_text('utf-8')).get('presets', []):
                    for kind in ('reference', 'sample'):
                        self.configured(preset.get(kind, {}).get('path'), 'voice.'+preset['id']+'.'+kind, required=True)
        if media.get('root') and resolve(self.root, media['root']) != self.root/'data/local-media':
            self.configured(media['root'], 'local_media.root')
        external = [('dsh_home', 'dsh-home'), ('dsh_root', 'dsh-source'), ('bili_plugin', 'bili-plugin')]
        for key, label in external:
            value = config.get(key)
            if not value:continue
            path = resolve(self.root, value)
            if not path.is_dir():self.missing.append(dict(setting=key, path=str(path)));continue
            self.tree(path, 'external/'+label, skip_top={'cache', 'test-plugin', '.acceptance'})
        if config.get('consult_config'):
            p = resolve(self.root, config['consult_config'])
            if p.is_file():
                consult = json.loads(p.read_text('utf-8'))
                if consult.get('project'):
                    self.tree(resolve(self.root, consult['project']), 'external/consult-project')
                if consult.get('data_dir'):self.configured(consult['data_dir'], 'consult.data_dir')
        for name in ('napcat-onekey/NapCat.52230.Shell/config', 'napcat/config'):
            self.tree(self.root/name, 'project/'+name)
        # Model and executable locations are listed, not silently packaged as portable installs.
        def paths(value, key='config'):
            if isinstance(value, dict):
                for k, v in value.items():paths(v, key+'.'+k)
            elif isinstance(value, list):
                for i, v in enumerate(value):paths(v, key+'.'+str(i))
            elif isinstance(value, str) and (':\\' in value or ':/' in value or key.endswith('model_path')):
                self.manual.append(dict(setting=key, source=value))
        paths(config)
        env_names = {'DEEPSEEK_API_KEY', 'QQ_WS_URL'}
        def envs(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.endswith('_env') and isinstance(item, str) and item:env_names.add(item)
                    else:envs(item)
            elif isinstance(value, list):
                for item in value:envs(item)
        envs(config)
        return {key:os.environ[key] for key in sorted(env_names) if key in os.environ}

    def write(self):
        # A running cognition service can change several stores between snapshots.
        health = self.root/'data/life-control/worker-health.json'
        if health.exists():
            value = json.loads(health.read_text('utf-8'))
            if time.time()-value.get('at', 0)<20 and value.get('cognition_ready'):
                raise RuntimeError('Bot 正在处理生活数据，请先停止 Bot，再导出。导出工具不会终止进程。')
        self.destination.mkdir(parents=True, exist_ok=True)
        env = self.collect()
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        target = self.destination/('xiaoyu-private-migration-'+stamp+'.zip')
        partial = target.with_suffix('.zip.partial')
        entries = []
        print('Exporting', len(self.files), 'files; contents and credentials are not printed.', flush=True)
        with tempfile.TemporaryDirectory(prefix='xiaoyu-export-') as temp, zipfile.ZipFile(
                partial, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
            for n, (name, original) in enumerate(sorted(self.files.items())):
                source = original;mode = 'file'
                with original.open('rb') as stream:header = stream.read(16)
                if header == b'SQLite format 3\x00':
                    source = Path(temp)/'database.sqlite3';source.unlink(missing_ok=True)
                    started = time.monotonic()
                    def progress(*_):
                        if time.monotonic()-started>60:raise TimeoutError('sqlite_backup_timeout')
                    incoming = sqlite3.connect(original.resolve().as_uri()+'?mode=ro', uri=True)
                    outgoing = sqlite3.connect(source)
                    try:
                        incoming.backup(outgoing, pages=256, progress=progress)
                        if outgoing.execute('PRAGMA quick_check').fetchone()[0] != 'ok':raise ValueError('sqlite_integrity_failed')
                    finally:outgoing.close();incoming.close()
                    mode = 'sqlite_consistent_backup'
                before = source.stat();digest = hashlib.sha256()
                with source.open('rb') as reader, archive.open(name, 'w', force_zip64=True) as writer:
                    while chunk := reader.read(1024*1024):writer.write(chunk);digest.update(chunk)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RuntimeError('文件在导出期间变化，请停止相关服务后重试：'+str(original))
                entries.append(dict(path=name, source=str(original), bytes=after.st_size,
                                    sha256=digest.hexdigest(), mode=mode))
                if n and n % 1000 == 0:print('Exported', n, 'files', flush=True)
            archive.writestr('migration/environment.private.json', json.dumps(env, ensure_ascii=False, indent=2))
            try:commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.root, text=True, stderr=subprocess.DEVNULL).strip()
            except (OSError, subprocess.CalledProcessError):commit = None
            manifest = dict(created=datetime.now().astimezone().isoformat(), commit=commit, root=str(self.root),
                scope='source_private_configuration_memory_and_bot_owned_state', entries=entries,
                roots=self.roots, excluded=self.excluded, missing_optional=self.missing,
                paths_to_review=self.manual, sqlite_snapshot=True, cross_store_atomic=False)
            archive.writestr('migration/manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr('迁移说明.txt', MIGRATION_GUIDE)
        print('Verifying every archived file...', flush=True)
        with zipfile.ZipFile(partial) as archive:
            for entry in entries:
                with archive.open(entry['path']) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                        raise ValueError('archive_hash_mismatch: '+entry['path'])
            # Reading all members also checks their CRC, including generated metadata.
            for name in ('migration/manifest.json', 'migration/environment.private.json', '迁移说明.txt'):
                archive.read(name)
        partial.rename(target)
        checksum = file_hash(target)
        target.with_suffix('.zip.sha256').write_text(checksum+'  '+target.name+'\n', encoding='utf-8')
        result = dict(archive=str(target), bytes=target.stat().st_size, files=len(entries),
                      sqlite_databases=sum(e['mode']=='sqlite_consistent_backup' for e in entries),
                      sha256=checksum, verified=True, missing_optional=self.missing)
        target.with_suffix('.summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return target


MIGRATION_GUIDE = '''小鱼个人化与隐私迁移包

本 ZIP 未加密，包含真实密钥、人格、记忆、人物画像、登录配置和专用会话。
仅通过自己的可信设备/存储转移，不要提交 Git 或公开分享。

内容：project/ 为当前项目源码、私有配置、人格、skills/scripts、生活数据库、媒体和设置密码。
external/ 为 Bot 专用 DSH 数据/源码、B站插件、咨询项目（以 manifest 为准）。
SQLite 用备份 API 生成一致副本，包含 WAL 中已提交数据，不直接拼接复制 WAL。
recovery-only/ 保存旧发布控制数据库及维护命令材料，仅供核对，不直接覆盖新机运行目录。
不复制进程锁、PID、健康文件、重启请求；不自动执行恢复、对账或发布。

新机步骤：
1. 停止旧电脑 Bot，并保持停止，避免两个副本同时回复。当前包是导出时快照，之后发生的新对话不在包中。
2. 校验 ZIP 的 SHA256，在空目录解压；先保留一份原包。project/ 可放到新机项目目录。
3. 按 migration/manifest.json 的 roots 放置 external/ 各目录。尽量保留原盘符/目录，但用户名不同的路径仍需调整。
4. 新建 Python 虚拟环境并安装 requirements.txt；配置兼容 DSH、Node/pnpm、QQ 与 NapCat。
5. 检查 config.toml、咨询 config、DSH profile、音色 manifest 中的绝对路径。
   migration/manifest.json 中 paths_to_review 是配置里的路径线索；数据库、冻结任务或工具历史也可能引用旧路径。
   不要对数据库做盲目字符串替换；若路径改变，先核对资源登记和未完成任务。
6. 另外复制/安装模型权重、语音引擎、sd-server 和运行依赖。虚拟环境、node_modules、模型权重、安装包未打包。
   特别核对 config 的 voice_engine_root、voice_model_dir、image_server、image_model_dir、local_embedding.model_path。
7. NapCat config 已保存，但 QQ 登录缓存不保证跨机器通用，准备重新扫码；Codex 咨询需在新机自行安装和登录。
8. migration/environment.private.json 只保存导出进程可见的已配置环境覆盖项；其他进程独有变量不会被自动读取。
9. 先启动设置页确认配置，再启动 QQ/Bot，进行一轮普通聊天、记忆读取和需要的语音测试。

主机可边离线待机边备份；为避免多个存储在导出期间变化，认知服务在线时导出会拒绝，请先停止 Bot。
缺失可选路径和排除项完整记录在 manifest，不能把本包当作操作系统/第三方依赖的完整克隆。
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    try:Exporter(args.root, args.output_dir or args.root/'backups').write()
    except Exception as error:
        print('EXPORT FAILED:', str(error));return 1
    return 0


if __name__ == '__main__':raise SystemExit(main())
