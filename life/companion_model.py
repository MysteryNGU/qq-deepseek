"""Direct model calls for the independent companion, without a harness process."""
import json
import time
from urllib.parse import urlsplit

import httpx
try:
    import httpx2
except ImportError:
    httpx2 = httpx
from . import providers
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, Timeout, DefaultAsyncHttpxClient
from jsonschema import Draft7Validator

# 2026-11-18：上游 deepseek-v4.1-flash 支持 384,000 输出；2,048 会让长文档、
# 表格、代码补丁被截断（finish_reason=length）。放宽到 16,000，仍留足余量。
MAX_OUTPUT_TOKENS = 16000

# 按 purpose 给不同的输出额度。2026-11-18 实测（全库 output_tokens 分布）：
#   dialogue      n=747   p50=389   p90=654   p99=1240
#   activity      n=1596  p50=687   p90=927   p99=1753
#   work          n=913   p50=816   p90=1187  p99=4739   ← 唯一长尾的
#   town          n=542   p50=57    p90=118   p99=195
#   bilibili_read n=168   p50=479   p90=793   p99=971
# 只有 work 的 p99 到了 4739，且今天它多次撞到上限；其余用途 p99 都远低于 16000，
# 跟着一起涨只是浪费额度。所以只给 work 更大的池子。
#
# 注意：这治标不治本。thinking 模型的 reasoning token 和正式内容**共用一个池子**，
# 撞墙的本质是「一个池子被两件事用」。2048 时代同样撞（今天 16:14~16:42 有 9 条
# 精确等于 2048），所以单纯加钱只是把撞墙时机推后。
OUTPUT_TOKENS_BY_PURPOSE = {'work': 32000}


def output_tokens_for(purpose):
    """这一轮该给多少输出额度。没列出的用途用默认值。"""
    return OUTPUT_TOKENS_BY_PURPOSE.get(str(purpose or ''), MAX_OUTPUT_TOKENS)


def strict_schema(value):
    if isinstance(value, list):
        return [strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key:strict_schema(item) for key,item in value.items()
              if key not in {'minLength','maxLength','minItems','maxItems'}}
    if result.get('type') == 'object':
        result['additionalProperties'] = False
        result['required'] = list(result.get('properties', {}))
    if 'enum' in result and 'type' not in result and all(isinstance(x,str) for x in result['enum']):
        result['type'] = 'string'
    return result


def connection_failed(error):
    """Retry only failures that demonstrably happened before sending a request."""
    if getattr(error, 'response', None) is not None:
        return False
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (httpx.ConnectTimeout, httpx.ConnectError,
                              httpx2.ConnectTimeout, httpx2.ConnectError)):
            return True
        error = error.__cause__ or error.__context__
    return False


class ModelRequestError(RuntimeError):
    def __init__(self, category, status_code=None, unreported_attempts=0):
        self.category, self.status_code = category, status_code
        self.unreported_attempts = unreported_attempts
        suffix = f' (HTTP {status_code})' if status_code is not None else ''
        super().__init__(f'Model request failed: {category}{suffix}. Check model connection settings.')


class DirectModel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None
        self.strict_client = None

    async def complete(self, messages, *, json_mode=False, purpose='dialogue', schema=None):
        started = time.monotonic()
        attempts = 0
        try:
            # 全部差异都来自服务商档案（life/providers.py），不在这里分叉判断。
            # t22 的教训：把「是不是官方」当成「能不能走 strict」，
            # 结果 opencode-go 被降级成纯文字约束，一轮里 19 次没有可解析的 JSON。
            provider_id, _why = providers.resolve(self.cfg)
            profile = providers.get(provider_id) if provider_id else None
            if profile is None:
                # 认不出服务商时按「不特殊照顾」处理：用配置里的 base_url，
                # 不假设它支持 tools（宁可退回 json_mode 也不要发一个它不认识的字段）。
                profile = dict(base_url=self.cfg.base_url, beta_endpoint=None,
                               trust_env=True, thinking_param=False,
                               session_header=None, supports_tools=False)
            official = profile.get('beta_endpoint') is not None
            supports_tools = profile.get('supports_tools', False)
            # 配置可覆盖档案（换到「档案说支持、实际不支持」的端点时用）
            override = getattr(self.cfg, 'model_supports_tools', None)
            if override is not None:
                supports_tools = bool(override)
            strict = schema is not None and supports_tools
            attribute = 'strict_client' if strict else 'client'
            client = getattr(self, attribute)
            if client is None:
                # /beta 只有 DeepSeek 官方有；别的服务商拿到它会 404。
                endpoint = (profile['beta_endpoint']
                            if (official and strict) else profile['base_url'])
                # Windows registry proxies are inherited even without proxy env
                # variables. The official endpoint is directly reachable here;
                # its inherited local tunnel intermittently fails TLS handshakes.
                transport = (dict(http_client=DefaultAsyncHttpxClient(trust_env=False))
                             if not profile.get('trust_env', True) else {})
                # opencode-go 之类的中转要求 x-opencode-session 才会路由，
                # 缺了直接 400（2026-11-18 实测）。用稳定的进程级 id，不随请求变，
                # 免得每一轮都被当成新的会话、丢掉上游的缓存命中。
                headers = {}
                session_header = profile.get('session_header')
                if session_header:
                    headers[session_header] = 'xiaoyu-' + str(getattr(self.cfg, 'life_agent', '') or 'companion')
                client = AsyncOpenAI(api_key=self.cfg.api_key, base_url=endpoint,
                                          max_retries=0,
                                          timeout=Timeout(getattr(self.cfg, 'dsh_timeout', 180), connect=15),
                                          default_headers=headers or None,
                                          **transport)
                setattr(self, attribute, client)
            request = dict(model=self.cfg.model, messages=messages,
                           max_tokens=output_tokens_for(purpose))
            if profile.get('thinking_param'):
                request['extra_body'] = dict(thinking=dict(type='disabled'))
            if strict:
                request['tools'] = [dict(type='function', function=dict(name='respond', strict=True,
                    description='Submit the complete companion decision.', parameters=strict_schema(schema)))]
                # 有的网关（腾讯 TokenPlan 实测）不接受「指定函数对象」形式的 tool_choice，
                # 传 auto 才会返回 tool_calls；档案里写 tool_choice='auto' 的走这条路。
                if profile.get('tool_choice') == 'auto':
                    request['tool_choice'] = 'auto'
                else:
                    request['tool_choice'] = dict(type='function', function=dict(name='respond'))
            elif json_mode or schema is not None:
                # 有的网关不支持 response_format（腾讯 TokenPlan 实测 400）；
                # 档案里 json_mode=False 时不发这个字段，只靠系统提示约束 JSON。
                if profile.get('json_mode', True):
                    request['response_format'] = dict(type='json_object')
                if schema is not None:
                    request['messages'] = [*messages, dict(role='system',content=
                        'Return one complete JSON object following this schema: '+json.dumps(schema,ensure_ascii=False))]
            for attempt in range(1 if getattr(self.cfg,'spending_policy',{}).get('enabled') else 2):
                attempts += 1
                try:
                    response = await client.chat.completions.create(**request)
                    break
                except Exception as error:
                    if attempt or not connection_failed(error):
                        raise
            message = response.choices[0].message.model_dump(exclude_none=True)
            message.setdefault('content', None)
            wire_error = None
            if strict:
                calls = message.get('tool_calls') or []
                # 名字只是载体：模型偶尔把 kind 当成工具名（memory/actions），
                # 但参数里装的就是完整决定。只认 respond 会把能用的一轮整条作废。
                #
                # 多个调用：取**第一个 arguments 能解析成 JSON 的**。
                # 设计意图是「拿到一份合法的完整决定」，不是「必须恰好一个 respond」；
                # 第二个及以后只是重复，为它们丢掉整轮没有道理。
                # 但**不许静默容忍**：把选中的下标与被丢弃的一并记下来，由上层写审计。
                picked = None
                if len(calls) >= 1:
                    for index, call in enumerate(calls):
                        arguments = (call.get('function') or {}).get('arguments')
                        if not arguments:
                            continue
                        try:
                            json.loads(arguments)
                        except (TypeError, ValueError):
                            continue          # 这一份不是合法 JSON，看下一份
                        picked = (index, call)
                        break
                if picked is not None:
                    chosen_index, chosen = picked
                    message['content'] = chosen['function'].get('arguments')
                    if chosen['function'].get('name') != 'respond':
                        wire_error = 'tool name is %r, not respond' % chosen['function'].get('name')
                    if len(calls) > 1:
                        # 不是错误，是事实：让上层知道这轮是从几个调用里挑的。
                        message['tool_call_pick'] = dict(
                            total=len(calls), chosen_index=chosen_index,
                            names=[(c.get('function') or {}).get('name') for c in calls])
                    calls = [chosen]         # 后面的校验只看选中的那份
                    try:
                        value = json.loads(message['content'])
                        if (isinstance(value, dict) and 'reply' not in value
                                and (value.get('kind') or value.get('op'))):
                            # 模型把单个动作当工具调了：kind 就是工具名，其余是动作字段。
                            action = dict(value)
                            action.setdefault('kind', calls[0]['function'].get('name') or '')
                            value = dict(reply='', actions=[action])
                            message['content'] = json.dumps(value, ensure_ascii=False)
                        violation = next(Draft7Validator(request['tools'][0]['function']['parameters']).iter_errors(value),None)
                        if violation is not None:
                            wire_error = ((wire_error+'; ') if wire_error else '') + violation.message[:400]
                    except (TypeError,ValueError) as error:
                        wire_error = ((wire_error+'; ') if wire_error else '') + str(error)[:400]
                else:
                    wire_error = 'Expected exactly one respond tool call'
            usage = response.usage.model_dump() if response.usage is not None else {}
            details = usage.get('prompt_tokens_details') or {}
            counts = dict(input_tokens=usage.get('prompt_tokens'),
                          cached_input_tokens=usage.get('prompt_cache_hit_tokens', details.get('cached_tokens')),
                          output_tokens=usage.get('completion_tokens'), total_tokens=usage.get('total_tokens'))
            counts = {key: value if type(value) is int and value >= 0 else None
                      for key, value in counts.items()}
            return dict(message=message, usage=counts, model=response.model or self.cfg.model,
                        request_id=getattr(response,'_request_id',None),
                        wire_schema_error=wire_error,
                        finish_reason=getattr(response.choices[0], 'finish_reason', None),
                        unreported_attempts=attempts-1,
                        seconds=round(time.monotonic()-started, 3))
        except Exception as error:
            # SDK errors may embed request bodies, headers or credential-bearing URLs.
            status = getattr(error, 'status_code', None)
            status = status if type(status) is int else None
            category = ('timeout' if isinstance(error, APITimeoutError) else
                        'connection' if isinstance(error, APIConnectionError) else
                        'authentication' if status in {401, 403} else
                        'rate_limit' if status == 429 else 'provider_error')
            raise ModelRequestError(category, status, attempts) from None

    async def close(self):
        for attribute in ('client','strict_client'):
            client = getattr(self, attribute)
            if client is not None:
                await client.close()
                setattr(self, attribute, None)
