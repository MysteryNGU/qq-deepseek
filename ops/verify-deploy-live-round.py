#!/usr/bin/env python3
"""部署后必跑：真实 Companion + 真实 sqlite + 桩模型，真调 _messages()。

判据：三种 purpose 都能拼出非空请求体，且含经验段。
替代旧清单里 `.review-server/_t4_regression_live_round.py` 的做法——那个脚本
import 了 `life.tests`，而 `life/tests/` 在云端不存在（只在本地仓库），
照它跑会 ModuleNotFoundError。真正的判据是 "_messages 跑得通"，不是 "能 import"。

用法（按 docs/deploy-checklist.md §2.0）：
    cd /opt/qq-deepseek && sudo -u companion \
      .venv-companion/bin/python -I ops/verify-deploy-live-round.py /opt/qq-deepseek
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else '/opt/qq-deepseek').resolve()
# 从临时目录跑时 sys.path[0] 可能是杂目录，先摘掉自己，只留仓库根。
sys.path[:] = [p for p in sys.path if Path(p or '.').resolve() != Path(__file__).resolve().parent]
sys.path.insert(0, str(ROOT))


def main():
    from life.companion import Companion
    home = Path(os.environ.get('COMPANION_HOME') or '/var/lib/companion/xiaoyu')
    cfg_path = home / 'config.toml'
    if not cfg_path.is_file():
        print(json.dumps(dict(ok=False, reason='no config.toml at %s' % cfg_path)))
        return 1
    from life.role_config import load_config
    cfg = load_config(cfg_path)
    # 生产 config 里可能有 Windows 路径（bili_plugin = "C:\\Users\\..."）。
    # 在 Linux 下它会被当成相对路径，stat 时报 PermissionError，和"服务能不能用"无关。
    # 部署自检只关心 _messages 能不能拼出来，所以把这类外部插件路径置空，用回退路径。
    plugin = str(getattr(cfg, 'bili_plugin', '') or '')
    if plugin and ('\\' in plugin or (len(plugin) > 1 and plugin[1] == ':')):
        try:
            object.__setattr__(cfg, 'bili_plugin', '')
        except Exception:
            pass
    companion = Companion(cfg, home)
    results, failures = {}, []
    for purpose in ('dialogue', 'work', 'background'):
        try:
            event, _ = companion.life.ingest(
                companion.scope, 'message', 'user_report', '部署自检：%s' % purpose, role='user')
            messages = companion._messages(event, purpose)
            text = '\n'.join(str(m.get('content') or '') for m in messages)
            row = dict(ok=bool(messages) and bool(text.strip()), messages=len(messages),
                       has_experience_section='recent_experience' in text or 'experience' in text)
            results[purpose] = row
            if not (row['ok'] and row['has_experience_section']):
                failures.append(purpose)
        except Exception as error:  # noqa: BLE001 - 部署自检要把原因原样报出来
            results[purpose] = dict(ok=False, error='%s: %s' % (type(error).__name__, error))
            failures.append(purpose)
    print(json.dumps(dict(ok=not failures, results=results), ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == '__main__':
    raise SystemExit(main())
