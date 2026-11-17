"""Direct model calls for the independent companion, without a harness process."""
import json
import time
from urllib.parse import urlsplit

import httpx
try:
    import httpx2
except ImportError:
    httpx2 = httpx
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, Timeout, DefaultAsyncHttpxClient
from jsonschema import Draft7Validator

MAX_OUTPUT_TOKENS = 2048


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
            official = urlsplit(self.cfg.base_url).hostname == 'api.deepseek.com'
            strict = official and schema is not None
            attribute = 'strict_client' if strict else 'client'
            client = getattr(self, attribute)
            if client is None:
                endpoint = 'https://api.deepseek.com/beta' if strict else self.cfg.base_url
                # Windows registry proxies are inherited even without proxy env
                # variables. The official endpoint is directly reachable here;
                # its inherited local tunnel intermittently fails TLS handshakes.
                transport = dict(http_client=DefaultAsyncHttpxClient(trust_env=False)) if official else {}
                client = AsyncOpenAI(api_key=self.cfg.api_key, base_url=endpoint,
                                          max_retries=0,
                                          timeout=Timeout(getattr(self.cfg, 'dsh_timeout', 180), connect=15),
                                          **transport)
                setattr(self, attribute, client)
            request = dict(model=self.cfg.model, messages=messages, max_tokens=MAX_OUTPUT_TOKENS)
            if official:
                request['extra_body'] = dict(thinking=dict(type='disabled'))
            if strict:
                request['tools'] = [dict(type='function', function=dict(name='respond', strict=True,
                    description='Submit the complete companion decision.', parameters=strict_schema(schema)))]
                request['tool_choice'] = dict(type='function', function=dict(name='respond'))
            elif json_mode or schema is not None:
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
                # 但参数里装的就是完整决定。只认 respond 会把能用的一轮整条作废，
                # 所以接受任何一个带 JSON 参数的单个调用，违规仍然照报。
                if len(calls) == 1 and (calls[0].get('function') or {}).get('arguments'):
                    message['content'] = calls[0]['function'].get('arguments')
                    if calls[0]['function'].get('name') != 'respond':
                        wire_error = 'tool name is %r, not respond' % calls[0]['function'].get('name')
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
