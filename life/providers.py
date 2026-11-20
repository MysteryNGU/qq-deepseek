# -*- coding: utf-8 -*-
"""服务商档案：代码里硬编码 7 家，桌面端只做「选服务商 → 选模型 → 填 key」。

为什么不放进配置：
模型名、端点路径、是否需要特殊头、支不支持 tools——这些是**代码知识**，
不是用户偏好。放进配置文件会让每个用户名下都有一份可能过时的副本，
改一家要改所有部署。硬编码在一处，升级代码即升级全部。

三件事必须分开（2026-11-18 的生产 bug 就是混在一起造成的）：
- ``official`` 时代码把「是不是 DeepSeek 官方」当成了「能不能走 strict」，
  于是 opencode-go 虽然支持 tools，却被降级成纯文字约束的 json_mode，
  一轮里 19 次「没有可解析的 JSON」。
- 现在 ``beta_endpoint`` / ``trust_env`` / ``thinking_param`` 各自是字段，
  ``supports_tools`` 单独判断，互不牵连。

**模型列表以实测为准**：有 key 的拉 /models，没有的按官方文档写确定的，
拿不准的一律不写——写错模型名比不给更糟，用户选了会直接 404。
"""
from __future__ import annotations

# 每一家的字段含义：
#   label          桌面端下拉里显示的名字
#   base_url       OpenAI 兼容根地址（不含 /chat/completions）
#   beta_endpoint  仅 DeepSeek 官方有的 beta 路径；没有就是 None
#   trust_env      False = 不用系统代理（官方端点直连更稳，继承的本地隧道会间歇性 TLS 失败）
#   thinking_param 是否支持 extra_body={"thinking": {...}}（官方专属）
#   session_header 需要额外带的路由头（opencode-go 缺它会 400）
#   supports_tools 支不支持 function calling + tool_choice（决定走 strict 还是 json_mode）
#   models         下拉里的模型名；空列表表示「未核实，不提供选择」
PROVIDERS = {
    'deepseek-official': dict(
        label='DeepSeek 官方',
        base_url='https://api.deepseek.com',
        beta_endpoint='https://api.deepseek.com/beta',
        trust_env=False,
        thinking_param=True,
        session_header=None,
        supports_tools=True,
        # 思考内容放在哪个字段。OpenAI 兼容惯例是 reasoning_content；
        # 只有 opencode-go 实测过，其余三家无 key 未逐家核实。
        reasoning_field='reasoning_content',
        models=['deepseek-chat', 'deepseek-reasoner'],
    ),
    'opencode-go': dict(
        label='OpenCode Go',
        base_url='https://opencode.ai/inference/go/v1',
        beta_endpoint=None,
        trust_env=True,
        thinking_param=False,
        session_header='x-opencode-session',
        supports_tools=True,
        # 2026-11-18 实测 /models 返回 32 个，以下是其中可直接选的。
        # 思考内容放在哪个字段。OpenAI 兼容惯例是 reasoning_content；
        # 只有 opencode-go 实测过，其余三家无 key 未逐家核实。
        reasoning_field='reasoning_content',
        models=[
            'deepseek-v4.1-flash', 'deepseek-v4-pro',
            'glm-5.2', 'glm-5.3', 'glm-5.3-flash',
            'qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-plus', 'qwen4-max',
            'kimi-k3', 'kimi-k3.1-pro', 'kimi-k2.7-code',
            'minimax-m3', 'minimax-m3.1-flash',
            'mimo-v2.6-pro', 'mimo-v2.6-flash',
            'grok-4.7', 'longcat-2.0',
            'claude-haiku-5-5', 'gpt-5.6-luna', 'gpt-6-luna',
            'gemini-4-argon', 'step-5-preview',
            'hy3', 'hy4', 'hy4-preview', 'hy4-flash-free',
            'longcat-2.5-free', 'muse-spark-1.2-contributor',
            'muse-spark-1.3-contributor', 'iceman-contributor-free', 'beam-free',
        ],
    ),
    'zhipu': dict(
        label='智谱 GLM',
        base_url='https://open.bigmodel.cn/api/paas/v4',
        beta_endpoint=None,
        trust_env=True,
        thinking_param=False,
        session_header=None,
        supports_tools=True,
        # 未持有 key，无法 curl /models 核实；只列官方文档长期稳定的名字。
        # 思考内容放在哪个字段。OpenAI 兼容惯例是 reasoning_content；
        # 只有 opencode-go 实测过，其余三家无 key 未逐家核实。
        reasoning_field='reasoning_content',
        models=['glm-4-plus', 'glm-4-air', 'glm-4-flash'],
    ),
    'dashscope': dict(
        label='阿里通义',
        base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
        beta_endpoint=None,
        trust_env=True,
        thinking_param=False,
        session_header=None,
        supports_tools=True,
        # 未持有 key，同上：只列稳定名。
        # 思考内容放在哪个字段。OpenAI 兼容惯例是 reasoning_content；
        # 只有 opencode-go 实测过，其余三家无 key 未逐家核实。
        reasoning_field='reasoning_content',
        models=['qwen-max', 'qwen-plus', 'qwen-turbo'],
    ),
    'volcengine': dict(
        label='火山方舟',
        base_url='https://ark.cn-beijing.volces.com/api/v3',
        beta_endpoint=None,
        trust_env=True,
        thinking_param=False,
        session_header=None,
        supports_tools=True,
        reasoning_field='reasoning_content',
        # 火山的模型名多数是用户自建推理接入点（ep-xxxx），没有通用名字可列。
        # 空列表 = 未核实；桌面端据此提示用户手填，不要给一个选了就 404 的选项。
        models=[],
        needs_manual_model=True,
    ),
    'openai': dict(
        label='OpenAI',
        base_url='https://api.openai.com/v1',
        beta_endpoint=None,
        trust_env=True,
        thinking_param=False,
        session_header=None,
        supports_tools=True,
        # 思考内容放在哪个字段。OpenAI 兼容惯例是 reasoning_content；
        # 只有 opencode-go 实测过，其余三家无 key 未逐家核实。
        reasoning_field='reasoning_content',
        models=['gpt-4o', 'gpt-4o-mini'],
    ),
    'tencent': dict(
        label='腾讯 TokenPlan',
        # 通用 Token Plan 与 Hy Token Plan **共用同一个 key、同一个地址**，
        # 唯一差别是模型名要按所购套餐支持的 Model ID 填。所以不拆成两家。
        base_url='https://tokenhub.tencentmaas.com/plan/v3',
        beta_endpoint=None,
        trust_env=True,
        thinking_param=False,
        session_header=None,
        # 用户报告腾讯是 OpenAI 兼容网关，但 tool_calls **未实测**（云端无 key）。
        # 若实际不支持，配置里 model_supports_tools=false 会自动退回 json_mode。
        supports_tools=True,
        # 2026-11-20 实测（deepseek/deepseek-flash）：tools 本身可用，但
        # tool_choice 传「指定函数对象」会被 MaaS 内部组件 400 拒绝；
        # 传 "auto" 或不传都能正常返回 tool_calls。所以这家的 tool_choice 走 auto。
        tool_choice='auto',
        # 同一实测：该套餐模型不支持 response_format（json_object/json_schema 均 400），
        # 所以不要对这家退回 json_mode——退回等于全线 400。strict 走 tools 才是可用路径。
        json_mode=False,
        # 腾讯**不支持列模型**：GET /plan/v3/models 返回 404（已实测，不是配错地址）。
        # 所以模型名只能硬编码，桌面端不要指望能拉远端列表。
        list_models_supported=False,
        reasoning_field='reasoning_content',
        # 通用系列 + Hy 系列。deepseek/deepseek-v4-pro 的斜杠是模型名的一部分，不要清理。
        models=['deepseek/deepseek-flash', 'deepseek-v4.1-flash-1017',
                'deepseek/deepseek-v4-pro', 'glm-5-3', 'glm-5.3-flash',
                'kimi-k2.7-code', 'hy4-preview'],
    ),
}

# base_url 前缀 → provider id，用于没有 provider 字段的旧配置
_PREFIX = [
    ('https://api.deepseek.com', 'deepseek-official'),
    ('https://opencode.ai/', 'opencode-go'),
    ('https://open.bigmodel.cn/', 'zhipu'),
    ('https://dashscope.aliyuncs.com/', 'dashscope'),
    ('https://ark.cn-beijing.volces.com/', 'volcengine'),
    ('https://api.openai.com/', 'openai'),
    ('https://api.lkeap.cloud.tencent.com/', 'tencent'),
    # 2026-11-18：腾讯换了新域名（通用 Token Plan 与 Hy 共用），旧配置只写新
    # base_url、没有 provider 字段时会被判成「认不出」，然后静默退回 json_mode。
    # 这类退化最难查——它不报错，只是能力悄悄少一块。
    ('https://tokenhub.tencentmaas.com/', 'tencent'),
]


def _self_check():
    """每个档案的 base_url 都必须能被 detect_by_base_url 认成自己。

    这是 B-7 那类漏洞的通用形式：**新加一个端点却忘了加进 _PREFIX**，
    表现不是报错，而是悄悄退回 json_mode。在这里炸出来，比上线后查一周好。
    仅在 import 时跑一次，代价可以忽略。
    """
    for provider_id, profile in PROVIDERS.items():
        base = profile.get('base_url') or ''
        if not base:
            continue
        found = _detect_prefix(base)
        if found != provider_id:
            raise AssertionError(
                'providers: %r 的 base_url %r 认成了 %r —— 把它加进 _PREFIX'
                % (provider_id, base, found))


def provider_ids():
    return list(PROVIDERS)


def get(provider_id):
    """按 id 取档案；不存在就返回 None（调用方决定怎么说清楚）。"""
    return PROVIDERS.get(str(provider_id or ''))


def _detect_prefix(base_url):
    """纯前缀匹配：认得就返回 provider id，认不出返回 None（不猜）。"""
    text = str(base_url or '').strip().rstrip('/')
    if not text:
        return None
    for prefix, provider_id in _PREFIX:
        if text.startswith(prefix.rstrip('/')):
            return provider_id
    return None


def detect_by_base_url(base_url):
    """旧配置没有 provider 字段时，按 base_url 前缀认出来。

    认不出来返回 None——**不猜**。猜错会把 key 发到错的域名上。
    """
    text = str(base_url or '').strip().rstrip('/')
    if not text:
        return None
    for prefix, provider_id in _PREFIX:
        if text.startswith(prefix.rstrip('/')):
            return provider_id
    return None


def resolve(cfg):
    """从配置里定出「用哪一家」。

    优先 cfg.provider；没有就用 base_url 反推；都定不出来返回 (None, 原因)。
    """
    requested = getattr(cfg, 'provider', '') or ''
    if requested:
        if get(requested):
            return requested, None
        return None, '未知的服务商 %r（可选：%s）' % (requested, '、'.join(provider_ids()))
    detected = detect_by_base_url(getattr(cfg, 'base_url', ''))
    if detected:
        return detected, None
    return None, ('认不出 base_url=%r 属于哪家服务商。'
                  '请在配置里写 provider = %s 之一。'
                  % (getattr(cfg, 'base_url', ''), '、'.join(provider_ids())))


def models_for(provider_id):
    """给桌面端下拉用的模型列表。空列表表示未核实。"""
    profile = get(provider_id) or {}
    return list(profile.get('models') or [])


def catalog():
    """给 RPC 用的完整清单：只有给桌面端看的字段，不带 key、不带内部判断。"""
    rows = []
    for provider_id in provider_ids():
        profile = PROVIDERS[provider_id]
        rows.append(dict(id=provider_id, label=profile['label'],
                         base_url=profile['base_url'],
                         models=list(profile['models']),
                         has_models=bool(profile['models']),
                         # 空列表时桌面端要提示「请手动填写模型名」，不要给一个选了就 404 的选项
                         needs_manual_model=bool(profile.get('needs_manual_model')
                                                 or not profile['models']),
                         list_models_supported=profile.get('list_models_supported', True),
                         supports_tools=bool(profile['supports_tools'])))
    return rows


_self_check()
