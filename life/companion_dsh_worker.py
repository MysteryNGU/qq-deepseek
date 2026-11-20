"""Isolated SDK execution worker; its parent owns the entire descendant tree."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


class HiddenPopen(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        if os.name == 'nt':
            kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
        super().__init__(*args, **kwargs)


def _scrub(text):
    """去掉像凭据的片段；其余原样保留（这是本地 artifact，要能定位根因）。"""
    return re.sub(r'(sk-|Bearer\s+)[A-Za-z0-9_\-\.]{8,}', r'\1<redacted>', str(text or ''))


def _write_result(path, payload):
    """先把这一轮的硬事实落盘，再决定抛不抛。

    上层只能看到进程退出码和一段文字；finish_reason、session_id 和 turn/end 里的
    真实原因如果不落在这里，就只存在于这个进程的内存里，随进程一起没了。
    """
    if not path:
        return
    try:
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, default=str),
                              encoding='utf-8')
    except OSError:
        pass   # 写不了不改变这次运行的结论


def _result_payload(request, result=None, *, error=None):
    """这一轮 dsh 的真实事实，来源分开写。

    ``final_response`` 是**另一个模型说的话**（model_account）；finish_reason 与
    session_id 是 SDK 事件里的值（tool_report）；失败原因取自会话 turn/end。
    三样分开写，谁也别冒充谁——「它说 completed」之所以没有信息量，就是因为
    这三样以前被压成了一句话。
    """
    finish = getattr(result, 'finish_reason', None) if result is not None else None
    session_id = getattr(result, 'session_id', None) if result is not None else None
    payload = dict(status='completed' if finish == 'completed' else 'failed',
                   model=request.get('model'),
                   finish_reason=finish,
                   finish_reason_text=str(finish),
                   session_id=session_id,
                   turn_reason='',
                   usage=None,
                   error_type='', error_text='',
                   provenance='model_account',
                   provenance_note=('输出文字是 dsh 那个模型说的（model_account）；'
                                    'finish_reason 与 session_id 是 SDK 事件里的值（tool_report）。'),
                   provenance_parts=dict(output='model_account',
                                         finish_reason='tool_report',
                                         session_id='tool_report'),
                   worker='companion_dsh_worker', finished_at=time.time())
    if result is not None and finish != 'completed':
        payload['turn_reason'] = _turn_error(result)
    if error is not None:
        payload.update(status='failed', error_type=type(error).__name__,
                       error_text=_scrub(str(error))[:600])
        if not payload['turn_reason']:
            payload['turn_reason'] = payload['error_text']
    return payload


def execute(request, *, initialize_only=False, harness_class=None, config_class=None):
    root = Path(request['dsh_root'])
    if harness_class is None:
        sys.path.insert(0, str(root/'python/sdk/src'))
        from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
        harness_class, config_class = DeepSeekHarness, DeepSeekHarnessConfig
    selected_home = request.get('dsh_home') or str(Path(request['home'])/'data/dsh-home')
    Path(selected_home).mkdir(parents=True, exist_ok=True)
    config = config_class(provider=request['provider'], model=request['model'],
                          max_tokens=4096, cwd=request['cwd'], runtime_cwd=str(root),
                          profile='sdk-minimal', dsh_home=selected_home,
                          env=dict(DSH_HOME=selected_home),
                          initialize_timeout_seconds=90)
    # 默认值必须是**构建好的 CLI**，不是源码入口：源码入口要 tsx，而运行时里没有 tsx，
    # 2026-11-18 实测 dsh 全部失败就是这个原因（报"运行环境或模型设置问题"，
    # 真实是 Cannot find package 'tsx'）。
    entry = request.get('entry') or 'lib/bin.js'
    if entry.endswith('.ts'):
        launch = (request['node'], '--import', 'tsx/esm', entry, '--profile', 'sdk-minimal')
    else:
        launch = (request['node'], str(root/entry), '--profile', 'sdk-minimal')
    result_path = request.get('result_path')
    harness = harness_class(config, _launch_args=launch)
    try:
        if initialize_only:
            harness.start()
            print('DSH SDK initialized', flush=True)
        else:
            result = None
            try:
                result = harness.run(request['goal'])
            except BaseException as error:  # noqa: BLE001 - 失败原因必须写进 result.json
                _write_result(result_path, _result_payload(request, result, error=error))
                raise
            print(result.final_response, flush=True)
            # Generic SDK events do not account for all retries/compaction requests.
            # Preserve unknown billing rather than invent a complete usage total.
            _write_result(result_path, _result_payload(request, result))
            if result.finish_reason != 'completed':
                # finish_reason 只是个 kind（"error"），真实原因在 events 的 turn/end 里：
                #   {"reason":{"kind":"error","error":{"message":"Authentication Fails, ...",
                #                                     "code":"AUTH","status":401}}}
                # 原来只抛 finish_reason，上层只能看到 "did not complete: \"error\""，
                # 于是所有人（包括她自己）都查不出根因。
                raise RuntimeError('DSH execution did not complete: '
                                   + _turn_error(result))
    finally:
        harness.close()


def _turn_error(result):
    """从 RunResult 里挖出真实的失败原因，尽量让上层能分类。

    优先用 turn/end 里的 error.message，其次 assistant/attempt 里的 failure，
    再次是 finish_reason 本身。都拿不到就如实说拿不到。
    """
    events = getattr(result, 'events', None) or []
    # turn/end 的 reason.error.message 最准
    for event in reversed(events):
        if not isinstance(event, dict) or event.get('type') != 'turn/end':
            continue
        reason = (event.get('data') or {}).get('reason') or {}
        error = reason.get('error') if isinstance(reason, dict) else None
        if isinstance(error, dict) and error.get('message'):
            code = error.get('code') or ''
            status = error.get('status') or ''
            tail = ('（code=%s status=%s）' % (code, status)) if (code or status) else ''
            return str(error['message'])[:600] + tail
        if isinstance(reason, dict) and reason.get('message'):
            return str(reason['message'])[:600]
    # 退一步：assistant/attempt 里的 finish failure
    for event in reversed(events):
        if not isinstance(event, dict) or event.get('type') != 'assistant/attempt':
            continue
        for chunk in ((event.get('data') or {}).get('stream') or []):
            finish = (chunk or {}).get('chunk') or {}
            if finish.get('type') == 'finish':
                failure = (finish.get('reason') or {}).get('failure') or {}
                if failure.get('message'):
                    return str(failure['message'])[:600]
    # 最后只有 kind
    try:
        return 'finish_reason=%s，但 events 里没有更详细的原因' % json.dumps(
            getattr(result, 'finish_reason', None), ensure_ascii=False)
    except Exception:
        return 'finish_reason 不可读'


def classify_failure(head, config=None):
    """把 DSH 的失败归成一句只讲事实的话。

    写法约定：**只陈述事实，不给处方**（2026-11-18 第二次修订）。

    第一次修的时候写成了「要让 dsh 能用，得换回 DeepSeek 官方 provider」——
    那是建议；「现在这个 key 是别家服务商的」是推断（401 推不出这句，
    也可能是 key 过期、打错、账号被停）。

    用户的原则是：把事实摆在眼前，让她自己判断。所以这里给：
      事实 —— 直接陈述（状态码、底层原文、当前配置）
      路径 —— 想查更多可以怎么查（可发现性）
    不给建议；推断如果要给，必须标明是推断。
    """
    config = config or {}
    text = (head or '').lower()
    if 'no adapter registered' in text:
        return ('dsh 调用失败：当前 profile 没有注册这个 provider 对应的 LLM adapter。'
                ' 底层原文：no adapter registered for provider。'
                ' 想确认加载了哪些 adapter：dsh --profile sdk-minimal --dump-config')
    if 'requires a native anthropic messages target' in text:
        return ('dsh 调用失败：这个 provider 需要原生的 Anthropic Messages 端点，'
                '当前配置给的不是。 底层原文：requires a native Anthropic Messages target。'
                ' 想确认加载了哪些 adapter：dsh --profile sdk-minimal --dump-config')
    # 2026-11-18 实测：真实报错是
    # "Authentication Fails, Your api key: ****BD9z is invalid (request_id: ...)"，
    # provider 仍是 deepseek-official —— 即 key 被发到了 DeepSeek 官方端点。
    if ('authentication fails' in text or 'api key' in text and 'invalid' in text
            or 'unauthor' in text or '401' in text or 'authentication' in text):
        return ('dsh 调用失败：鉴权被拒（HTTP 401）。底层原文见下面的 reason 行。'
                ' 当前配置：provider=%s、base_url=%s。'
                ' dsh 的 adapter 是 dsh-llm-deepseek-api-key，它只读环境变量 DEEPSEEK_API_KEY、'
                '不读 base_url。'
                ' 想确认 adapter 与端点：dsh --profile sdk-minimal --dump-config。'
                ' 以上是这条错误和当前配置里的事实。'
                % (config.get('provider') or '未知', config.get('base_url') or '未知'))
    if 'model' in text and ('not found' in text or 'does not exist' in text or 'unknown model' in text):
        return ('dsh 调用失败：模型名在服务商那边不存在。底层原文：model not found。'
                ' 可用模型可查：GET {base_url}/models')
    if 'timeout' in text or 'timed out' in text:
        return 'dsh 调用失败：调用模型超时。'
    if 'connect' in text or 'network' in text or 'dns' in text or 'fetch failed' in text:
        return 'dsh 调用失败：连不上模型服务（网络或地址问题）。'
    return 'dsh 调用失败：未分类的错误，看下面的 reason 行。'


if __name__ == '__main__':
    # This private worker owns every SDK subprocess; none needs a visible console.
    subprocess.Popen = HiddenPopen
    request = None
    try:
        request = json.loads(Path(sys.argv[1]).read_text('utf-8'))
        execute(request, initialize_only='--initialize-only' in sys.argv[2:])
    except Exception as error:
        # SDK 异常可能带请求体和带凭据的 URL，所以**不能原样打出整个异常字符串**。
        # 但也不能只留一句"看看运行时和模型设置"——那样连她自己也查不出原因
        # （2026-11-18：dsh 反复失败，所有人都只能看到这句话）。
        # 折中：打异常类型 + 脱敏后的首行，够定位到"是导入坏了、鉴权坏了、还是模型名错了"。
        import re
        raw = str(error)
        raw = re.sub(r'(sk-|Bearer\s+)[A-Za-z0-9_\-\.]{8,}', r'\1<redacted>', raw)
        raw = re.sub(r'[A-Za-z0-9_\-]{20,}', '<redacted>', raw)
        head = ' '.join(raw.splitlines())[:300]
        # 错误消息必须能区分是哪一类问题。原来那句「检查运行时和模型设置」
        # 连她自己也查不出原因（2026-11-18：dsh 反复失败，所有人只能看到这句话）。
        print('DSH execution failed: %s' % classify_failure(head, request), file=sys.stderr)
        print('  reason: %s: %s' % (type(error).__name__, head), file=sys.stderr)
        raise SystemExit(1) from None
