# -*- coding: utf-8 -*-
"""子 agent 的工作进程：一个请求文件进来，一份结果 JSON 出去。

它和 ``companion_dsh_worker.py`` 是同一类东西——一个由 ``ProcessTree`` 起出来的
短命进程，父进程（``life/companion_agents.py``）拥有整棵进程树，取消时连子孙
一起停。区别只有两点：

1. 三种后端在同一个入口里选（请求里的 ``node``）：``command`` 跑主机命令、
   ``stub`` 跑离线脚本（按 goal 里的 JSON 表演，不连模型）、``dsh`` 走 DSH SDK。
2. 结果不靠解析 stdout：写进 ``result_path`` 那个 JSON，父进程据此判断退出码、
   交付物、用量与会话号；stdout/stderr 只当人类可读的日志。

规矩：**这里不许把「不知道」写成 0**。DSH 的用量 SDK 拿不到，就写 ``usage: None``
而不是 ``{"input_tokens": 0}``。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


class HiddenPopen(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        if os.name == 'nt':
            kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
        super().__init__(*args, **kwargs)


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, default=str), encoding='utf-8')


def run_stub(request):
    """离线假服务：goal 是一段 JSON 脚本，按它表演，不连任何模型。"""
    data = {}
    goal = request.get('instruction') or ''
    try:
        data = json.loads(goal) if isinstance(goal, str) and goal.strip().startswith('{') else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    sleep_s = min(max(float(data.get('sleep_s') or 0), 0.0), 600.0)
    if sleep_s:
        time.sleep(sleep_s)
    artifacts = []
    for name, content in (data.get('artifacts') or {}).items():
        target = Path(name)
        if target.is_absolute() or '..' in target.parts:
            print('artifact path refused: %s' % name, file=sys.stderr, flush=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding='utf-8')
        artifacts.append(dict(name=target.name, path=str(target)))
    if data.get('stderr'):
        print(str(data['stderr']), file=sys.stderr, flush=True)
    print(str(data.get('stdout') or ''), flush=True)
    return dict(return_code=int(data.get('exit_code') or 0), artifacts=artifacts,
                tests=list(data.get('tests') or []), remaining=list(data.get('remaining') or []),
                blockers=list(data.get('blockers') or []),
                side_effects=list(data.get('side_effects') or []),
                usage=data.get('usage'))


def run_command(request):
    """主机命令：在给定 cwd 里跑 ``instruction``；退出码就是结果。"""
    text = str(request.get('instruction') or '').strip()
    if not text:
        print('command backend needs a command', file=sys.stderr, flush=True)
        return dict(return_code=2)
    shell = os.environ.get('SHELL') or '/bin/sh'
    creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    process = subprocess.Popen([shell, '-c', text] if os.name != 'nt' else text,
                               cwd=request.get('cwd') or None, shell=(os.name == 'nt'),
                               creationflags=creationflags)
    return dict(return_code=int(process.wait()))


def run_dsh(request):
    """DSH SDK：优先回接原会话，接不回去再用同一段说明开新会话。

    回来的东西里只有 ``session_id`` 与 ``finish_reason`` 是可靠事实：
    usage 一律 ``None``（P0 实测 SDK 不提供逐次用量），不编造数字。
    """
    root = Path(request['dsh_root'])
    sys.path.insert(0, str(root / 'python/sdk/src'))
    from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
    dsh_home = request.get('dsh_home') or str(Path(request['cwd']).parent / 'dsh-home')
    Path(dsh_home).mkdir(parents=True, exist_ok=True)
    config = DeepSeekHarnessConfig(
        provider=request.get('provider') or 'deepseek-official',
        model=request.get('model') or 'deepseek-flash',
        max_tokens=4096, cwd=request.get('cwd'), runtime_cwd=str(root),
        profile='sdk-minimal', dsh_home=dsh_home, env=dict(DSH_HOME=dsh_home),
        # 权限补丁：sdk-minimal 自带 danger-full-access，必须由这一层覆盖（环境变量无效）。
        patches=tuple(str(item) for item in (request.get('patches') or [])),
        initialize_timeout_seconds=90,
        request_timeout_seconds=float(request.get('timeout_seconds') or 1800))
    # entry 由调用方拼好后以绝对路径传进来（AgentManager.dsh_launch_paths 已确认它存在）。
    # 这里再做一次兜底，并且**拼不到就报错**——绝不让 SDK 回落到去找一个叫 dsh 的可执行文件
    # （那会抛出看不懂的 FileNotFoundError: 'dsh'，掩盖真正的路径问题）。
    entry = str(request.get('entry') or '')
    if not entry:
        raise RuntimeError('dsh_entry_missing: 请求里没有 entry')
    entry_path = Path(entry) if Path(entry).is_absolute() else root / entry
    if not entry_path.is_file():
        raise RuntimeError('dsh_entry_missing: %s 不存在（root=%s）' % (entry_path, root))
    node = str(request.get('node') or '')
    if not node or not Path(node).is_file():
        raise RuntimeError('dsh_node_missing: %s' % (node or '(空)'))
    # P18/P19 真根因：显式 _launch_args 会**完全绕过** SDK 的 _default_launch_args
    # （client.py:79 `args = list(self._launch_args or self._default_launch_args(env))`），
    # 而 `--patch` 只在 _default_launch_args 里拼。以前这里不拼 --patch，
    # 于是 config.patches 被静默丢弃、沙箱补丁从没到过 dsh（defaultMode 仍是 danger-full-access）。
    patch_args = tuple(argument for patch_path in (request.get('patches') or [])
                       for argument in ('--patch', str(Path(patch_path).expanduser().resolve())))
    if str(entry_path).endswith('.ts'):
        launch = (node, '--import', 'tsx/esm', str(entry_path), '--profile', 'sdk-minimal',
                  *patch_args)
    else:
        launch = (node, str(entry_path), '--profile', 'sdk-minimal', *patch_args)
    harness = DeepSeekHarness(config, _launch_args=launch)
    resumed = False
    try:
        if request.get('initialize_only'):
            # 只验「能不能把 DSH 进程拉起来并完成 initialize 往返」，不跑任何模型回合。
            harness.start()
            return dict(return_code=0, initialized=True, launch=list(launch),
                        session_id=None, usage=None)
        instruction = str(request.get('instruction') or '')
        session_id = request.get('resume_session_id') or ''
        if session_id:
            try:
                session = harness.resume_session(session_id)
                result = session.run(instruction)
                resumed = True
            except Exception:  # noqa: BLE001 - 会话接不回去就明说，不假装接上了
                print('resume failed; starting a new session', file=sys.stderr, flush=True)
                result = harness.run(instruction)
        else:
            result = harness.run(instruction)
        print(result.final_response, flush=True)
        return dict(return_code=0 if result.finish_reason in (None, 'completed') else 1,
                    session_id=getattr(result, 'session_id', None),
                    finish_reason=result.finish_reason, resumed=resumed, usage=None)
    finally:
        harness.close()


def execute(request):
    # ``transport`` 才是「用哪个后端」（旧版本把它写在 node 里，与 node 可执行文件路径冲突）。
    node = str(request.get('transport') or request.get('node') or 'command')
    if node == 'stub':
        return run_stub(request)
    if node == 'dsh':
        return run_dsh(request)
    return run_command(request)


def main():
    request = json.loads(Path(sys.argv[1]).read_text('utf-8'))
    result_path = request.get('result_path') or sys.argv[1] + '.result.json'
    started = time.monotonic()
    try:
        result = execute(request) or {}
    except Exception as error:  # noqa: BLE001 - 具体信息只进结果文件，不回显敏感上下文
        # traceback 只写进结果文件（父进程读得到、不进 journal）；stdout 仍只报类型，
        # 避免把请求体/凭据级别的上下文写进可读日志。
        result = dict(return_code=1, failure=dict(code='worker_exception',
                                                  reason='%s' % type(error).__name__,
                                                  trace=traceback.format_exc()[-4000:]))
        print('agent worker failed: %s' % type(error).__name__, file=sys.stderr, flush=True)
    result.setdefault('return_code', 1)
    result['usage'] = result.get('usage', None)
    result['seconds'] = round(time.monotonic() - started, 3)
    _write(result_path, result)
    return int(result.get('return_code') or 0)


if __name__ == '__main__':
    # 这个私有 worker 拥有全部子孙进程；不需要可见控制台。
    subprocess.Popen = HiddenPopen
    raise SystemExit(main())
