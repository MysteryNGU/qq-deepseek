"""Host checks for model-classified ordinary quotes; never whole-archive grants."""
import re

from .life_access import RecallError


SENSITIVE = re.compile(
    r'保密|秘密|别告诉|不要告诉|不许告诉|不能告诉|不能转述|不要转述|不要外传|别外传|仅限我们|'
    r'别说出去|不要说出去|不要对外说|不能外传|不许外传|不要分享|别分享|'
    r'密码|口令|验证码|私钥|密钥|token\b|api[_ -]?key|authorization|cookie|'
    r'身份证|银行卡|银行卡号|住址|家庭地址|手机号|电话号码|诊断|病历|疾病|性行为|'
    r'(?<!\d)(?:1[3-9]\d{9}|\d{17}[\dXx])(?!\d)|'
    r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|'
    r'(?:sk|ghp|github_pat)[_-][A-Za-z0-9_-]{12,}',re.I)

CONFIDENTIAL = re.compile(r'保密|秘密|别告诉|不要告诉|不许告诉|不能告诉|不能转述|不要转述|不要外传|别外传|仅限我们|别说出去|不要说出去|不要对外说|不能外传|不许外传|不要分享|别分享')
BACK_REFERENCE = re.compile(r'这件事|这事|这段|这个推荐|这部|上述|上面|以上|刚才|前面|之前说|推荐|电影')
INDEPENDENT_PRIVATE = re.compile(r'我的(?:另一个)?秘密|(?:我的|另一个|另外|另一件).*(?:密码|病历|诊断|住址|手机号|银行卡)')


def policy_text(text):
    """Ignore clearly labelled fictional quotations, never quoted requests."""
    pattern=r'“[^”]*”|「[^」]*」|‘[^’]*’|"[^"\n]*"'
    def quoted(match):
        prefix=text[max(0,match.start()-12):match.start()]
        return '' if re.search(r'台词|引文|例句|小说里|书中写|引用文本',prefix) else match.group()
    return re.sub(pattern,quoted,text)


def check_later_confidentiality(after):
    for sentence in re.split(r'[。！？!?\n]',policy_text(after)):
        if not CONFIDENTIAL.search(sentence):
            continue
        # Backward references and unscoped requests can apply to the selected
        # quote. Only a clearly separate private matter may restrict itself.
        if BACK_REFERENCE.search(sentence) or not INDEPENDENT_PRIVATE.search(sentence):
            raise RecallError('ambiguous_confidentiality')


class OrdinarySharing:
    def __init__(self, runtime, enabled=True):
        self.life, self.enabled = runtime, enabled

    def prepare(self, context, proposal, allowed_sources):
        if not self.enabled:
            raise RecallError('ordinary_sharing_disabled')
        if (not isinstance(proposal,dict) or proposal.get('classification')!='ordinary'
                or proposal.get('category') not in ('recommendation','everyday_event','public_finding')
                or proposal.get('uncertain',True) is not False
                or not isinstance(proposal.get('source_ref'),str)
                or proposal.get('source_ref') not in allowed_sources):
            raise RecallError('sharing_not_verified')
        resource,body=self.life.life_access.load(context,proposal['source_ref'],allowed_sources[proposal['source_ref']])
        if resource['kind']!='event':
            raise RecallError('raw_evidence_required')
        event=self.life.repo.get_event(resource['native_id'])
        if event.payload.get('confidential') or event.payload.get('sensitivity') in ('secret','sensitive','credential'):
            raise RecallError('restricted_source')
        if event.source_kind not in {'user_report','tool_observation','self_output'}:
            raise RecallError('sharing_not_verified')
        if event.source_kind=='self_output':
            delivery=self.life.repo._one('SELECT * FROM deliveries WHERE id=?',
                (event.payload.get('delivery_id'),))
            if (event.kind!='delivered_segment' or event.payload.get('confirmed') is not True
                    or not delivery or delivery['status']!='confirmed'
                    or delivery['scope_id']!=event.scope.scope_id
                    or event.external_id!='delivery:'+delivery['id']):
                raise RecallError('sharing_not_verified')
        if event.source_kind=='tool_observation' and event.payload.get('public_source') is not True:
            raise RecallError('sharing_not_verified')
        data=body.encode('utf-8')
        text_start=len(body.split('\n',1)[0].encode('utf-8'))+1
        spans=proposal.get('spans')
        if 'quotes' in proposal:
            quotes=proposal['quotes']
            if spans is not None or not isinstance(quotes,list) or not 1<=len(quotes)<=4:
                raise RecallError('invalid_span')
            spans=[]
            for quote in quotes:
                if not isinstance(quote,str) or not quote.strip() or len(quote)>400:
                    raise RecallError('invalid_span')
                encoded=quote.encode('utf-8')
                start=data.find(encoded,text_start)
                if start<0 or data.find(encoded,start+1)>=0:
                    raise RecallError('quote_not_unique')
                span=[start,start+len(encoded)]
                if span in spans:
                    raise RecallError('invalid_span')
                spans.append(span)
        if not isinstance(spans,list) or not 1<=len(spans)<=4:
            raise RecallError('invalid_span')
        # Shared bytes must come from the actual text, not actor/time headers.
        for span in spans:
            if not isinstance(span,list) or len(span)!=2:
                raise RecallError('invalid_span')
            start,end=span
            if type(start) is not int or type(end) is not int or not text_start<=start<end<=len(data):
                raise RecallError('invalid_span')
            try:
                before=data[text_start:start].decode('utf-8')
                selected=data[start:end].decode('utf-8')
                after=data[end:].decode('utf-8')
            except UnicodeDecodeError:
                raise RecallError('invalid_span') from None
            if len(selected)>400 or not selected.strip() or SENSITIVE.search(selected):
                raise RecallError('restricted_span')
            # A substring cannot drop “do not tell” from the same sentence.
            boundary=r'[。！？!?，,；;\n]'
            left=re.split(boundary,before)[-1]
            right='' if re.search(boundary+r'$',selected) else re.split(boundary,after)[0]
            if SENSITIVE.search(left+selected+right):
                raise RecallError('restricted_span')
            if left.strip() or right.strip():
                raise RecallError('whole_clause_required')
            # A preceding confidentiality instruction is not cancelled by
            # labelling the next sentence ordinary. Mixed later private clauses
            # do not suppress an earlier independent movie recommendation.
            if SENSITIVE.search(policy_text(before)):
                raise RecallError('ambiguous_confidentiality')
            check_later_confidentiality(after)
        return self.life.memory_organize.prepare_share(context,resource['resource_id'],spans,
            evaluator=lambda **checked:'ordinary' if checked['source']==body else 'restricted')
