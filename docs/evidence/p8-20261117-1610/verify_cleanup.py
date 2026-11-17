# -*- coding: utf-8 -*-
"""P3 验收用的清理器：只删本次验收自己造的临时根，删完逐条自证。

用法（云端）::

    .venv-companion/bin/python -B tools/stage-c/verify_cleanup.py /tmp/p3verify-xxxx ...

规矩：

* 只接受 ``/tmp/p3verify-`` 开头或是 ``/tmp/stage-b-``/``/tmp/companion-agents``
  这种本次验收会产生的目录；别的一律拒绝（不碰生产、不碰别人的临时文件）。
* 生产路径（``/var/lib/companion``、``/opt/qq-deepseek/life``）在拒绝名单里，
  就算传进来也不会删。
* 每个根先报告删了多少文件，再复核「不存在」。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ALLOWED_PREFIXES = ('/tmp/p3verify-', '/tmp/p3repro-', '/tmp/p3-', '/tmp/p3run',
                    '/tmp/stage-b-', '/tmp/companion-agents', '/tmp/verify-aware-')
DENY_ROOTS = ('/var/lib/companion', '/opt/qq-deepseek/life', '/opt/qq-deepseek/tests',
              '/', '/tmp', '/root')


def _count(root):
    files = folders = 0
    for _current, directories, names in os.walk(root):
        files += len(names)
        folders += len(directories)
    return files, folders


def clean(target):
    path = Path(target)
    resolved = path.resolve()
    text = str(path)
    if str(resolved) in DENY_ROOTS or not any(text.startswith(p) for p in ALLOWED_PREFIXES):
        return dict(path=text, cleaned=False, refused='outside the verification temp roots')
    if not path.exists():
        return dict(path=text, cleaned=True, existed=False, files=0, folders=0)
    if path.is_file():
        path.unlink()
        return dict(path=text, cleaned=not path.exists(), existed=True, files=1, folders=0)
    files, folders = _count(path)
    for current, directories, names in os.walk(path):
        for name in names:
            try:
                os.chmod(os.path.join(current, name), 0o600)
            except OSError:
                pass
        for name in directories:
            try:
                os.chmod(os.path.join(current, name), 0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)
    return dict(path=text, cleaned=not path.exists(), existed=True, files=files, folders=folders)


def main(argv):
    rows = [clean(item) for item in argv[1:]]
    receipt = dict(results=rows, all_cleaned=all(row['cleaned'] for row in rows))
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt['all_cleaned'] else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
