"""Export configuration, memory and skills only, including secrets. Standard library only; no network."""
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
    def __init__(self, root, destination, *, verify=False):
        self.root, self.destination = root.resolve(), destination.resolve()
        self.verify = verify
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
        # Only configuration, memory and skills. Source and runtimes come from Git/installers.
        self.add(self.root/'config.toml', 'project/config.toml')
        for name in ('life-dynamics.json', 'self.md'):
            self.add(self.root/name, 'project/'+name)
        for path in self.root.glob('.env*'):
            if path.name != '.env.example':self.add(path, 'project/'+path.name)
        self.configured(config.get('life_db', 'data/life.db'), 'life_db', required=True)
        self.configured(config.get('persona', 'persona.md'), 'persona', required=True)
        if not config.get('unified_persona', False):
            self.configured(config.get('public_persona'), 'public_persona')
        self.configured(config.get('consult_config'), 'consult_config')
        self.configured(config.get('local_media', {}).get('preset_manifest'), 'local_media.preset_manifest')
        self.tree(self.root/'skills', 'project/skills')
        self.tree(self.root/'diary', 'project/diary')
        for name in ('auth.json', 'login.txt'):
            self.add(self.root/'data/settings'/name, 'project/data/settings/'+name)
        # The dedicated DSH home holds configuration and persistent conversational memory.
        if config.get('dsh_home'):
            home = resolve(self.root, config['dsh_home'])
            self.roots.append(dict(setting='dsh_home', source=str(home), archive='external/dsh-home'))
            if not home.is_dir():raise FileNotFoundError('dsh_home: '+str(home))
            for name in ('.credentials.yaml', '.anonymous-user-id'):
                self.add(home/name, 'external/dsh-home/'+name)
            for name in ('profiles', 'sessions', 'qq-life', 'storages', 'bili-duty'):
                self.tree(home/name, 'external/dsh-home/'+name)
        for name in ('napcat-onekey/NapCat.52230.Shell/config', 'napcat/config'):
            self.tree(self.root/name, 'project/'+name)
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
                        if self.verify and outgoing.execute('PRAGMA quick_check').fetchone()[0] != 'ok':raise ValueError('sqlite_integrity_failed')
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
                scope='configuration_memory_skills_only', entries=entries,
                roots=self.roots, excluded=self.excluded, missing_optional=self.missing,
                paths_to_review=self.manual, sqlite_snapshot=True, cross_store_atomic=False)
            archive.writestr('migration/manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr('迁移说明.txt', MIGRATION_GUIDE)
        if self.verify:
            print('Verifying every archived file...', flush=True)
            with zipfile.ZipFile(partial) as archive:
                for entry in entries:
                    with archive.open(entry['path']) as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                            raise ValueError('archive_hash_mismatch: '+entry['path'])
                for name in ('migration/manifest.json', 'migration/environment.private.json', '迁移说明.txt'):
                    archive.read(name)
        partial.rename(target)
        checksum = file_hash(target) if self.verify else None
        if checksum:target.with_suffix('.zip.sha256').write_text(checksum+'  '+target.name+'\n', encoding='utf-8')
        result = dict(archive=str(target), bytes=target.stat().st_size, files=len(entries),
                      sqlite_databases=sum(e['mode']=='sqlite_consistent_backup' for e in entries),
                      sha256=checksum, verified=self.verify, missing_optional=self.missing)
        target.with_suffix('.summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return target


MIGRATION_GUIDE = '''小鱼配置、记忆与 skills 导出包

仅包含三类资料：
1. 配置：实际 config.toml、动力配置、当前人格、咨询配置、音色配置清单、设置登录凭据、NapCat 配置及 Bot 专用 DSH 配置。
2. 记忆：life.db（含经历、人物画像、关系、任务状态等）、self.md/日记、Bot 专用 DSH 会话和持久存储。
3. skills：本机 skills 目录。

不包含程序源码、scripts、测试、文档历史、插件代码、模型、音色音频、图片、虚拟环境、node_modules 或安装包。
源码通过 Git 获取。音色清单只是配置，引用的音频素材不在本包中。

新机先通过 Git 准备代码和依赖，再把 project/ 下内容放入项目对应位置。
external/dsh-home/ 放到新机为此 Bot 配置的 DSH home，目录映射见 migration/manifest.json。
检查 config.toml、咨询配置、音色清单和 DSH 配置中的绝对路径。数据库里旧任务/资产路径不要盲目替换。
SQLite 使用备份 API，已提交的 WAL 数据已合入备份。旧电脑应停止 Bot，避免两个副本同时回复。
本包未加密，含真实密钥和隐私，只通过自己的可信设备转移，不上传 Git。
默认不做额外校验；QQ 登录可能需要重新扫码，媒体素材与运行环境另外准备。
''' 



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--verify', action='store_true', help='Optional archive readback and database integrity check; disabled by default')
    args = parser.parse_args()
    try:Exporter(args.root, args.output_dir or args.root/'backups', verify=args.verify).write()
    except Exception as error:
        print('EXPORT FAILED:', str(error));return 1
    return 0


if __name__ == '__main__':raise SystemExit(main())
