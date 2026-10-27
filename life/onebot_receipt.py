"""Sanitized send errors. Native rejection and lost receipt are different."""
import re


def failure_reason(response):
    if not isinstance(response,dict):
        return None
    text=' '.join(str(response.get(k,'')) for k in ('message','wording'))
    if response.get('retcode') not in (None,0,1):
        if re.search(r'(?<!\d)1006514(?!\d)',text) or '网络连接异常' in text:
            return dict(reason='qq_network_unavailable',platform_error_code=1006514)
        if re.search(r'timeout|timed out|超时',text,re.I):
            return dict(reason='platform_receipt_timeout')
        return dict(reason='platform_rejected')
    return None
