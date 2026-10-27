"""Synthetic Chinese evaluation against the actual owned loopback model."""
import asyncio
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from urllib.request import build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from life.embedding import EmbeddingConfig,HttpEmbedding
from life.local_embedding import LocalEmbeddingProcess
from tests.role_fixture import Fixture
from tests.test_sharing_chinese_corpus import evaluate_sharing


CASES=[
 ('她推荐电影《小森林》，适合周末安静地看。',['想找一部田园生活题材的影片放松。','谁推荐了适合休息日欣赏的电影？','关于小森林的观影建议是什么？']),
 ('他想读科幻小说《三体》，对宇宙文明感兴趣。',['想看描写外星文明的科幻书。','对宇宙社会感兴趣时准备读什么小说？','三体这部作品的阅读计划。']),
 ('周六上午乘高铁到杭州，订了西湖附近的酒店。',['周末去杭州的住宿和交通安排。','坐火车去西湖玩，住在哪里？','星期六的高铁旅行计划。']),
 ('Python程序报错，发现原因是列表下标越界。',['索引超出数组长度导致程序异常。','调试脚本时列表访问出错。','Python的IndexError是什么原因？']),
 ('阳台的薄荷叶子发黄，需要少浇水并增加光照。',['盆栽的叶片变黄怎么养护？','薄荷需要调整浇水和日照。','阳台植物黄叶的处理办法。']),
 ('练习钢琴时卡在左手节奏，决定放慢速度练习。',['演奏键盘乐器时左手跟不上节拍。','练琴的节奏困难怎么处理？','钢琴左手需要慢速练习。']),
 ('猫咪今天不肯吃饭，准备带去宠物医院检查。',['小猫没有食欲要去看兽医。','宠物拒食之后的就医安排。','猫不吃东西该去哪里检查？']),
 ('语音合成任务生成了音频文件，但还没有发到QQ。',['声音文件已经做出来，是否已经送达聊天软件？','合成音频完成后尚未发送。','语音生成与QQ送达的状态。'])
]


async def evaluate(config):
    fixture=Fixture()
    try:
        life=fixture.life
        adapter=HttpEmbedding(life,fixture.role.spending,EmbeddingConfig.parse(config))
        life.memory_index.configure(adapter,provider_revision=config['provider_revision'],model_id=config['model_id'],
            model_revision=config['model_revision'],dimension=512)
        docs=[c[0] for c in CASES];queries=[q for _,qs in CASES for q in qs]
        timings=[]
        async def embed(texts,key):
            start=time.monotonic()
            result=await adapter.embed(texts,model_key=life.memory_index.model_key,purpose='memory_query',call_key=key,source_refs=[])
            timings.append(round((time.monotonic()-start)*1000,2))
            if result.get('state')=='unknown':raise ValueError('actual_endpoint_unknown')
            for vector in result['vectors']:
                if len(vector)!=512 or any(not math.isfinite(v) for v in vector) or abs(sum(v*v for v in vector)-1)>.001:
                    raise ValueError('invalid_normalized_vector')
            return result
        document_reply=await embed(docs,'corpus-documents')
        query_reply=await embed(queries,'corpus-queries')
        ranks=[]
        for i,query in enumerate(query_reply['vectors']):
            scored=sorted(range(len(docs)),key=lambda j:sum(a*b for a,b in zip(query,document_reply['vectors'][j])),reverse=True)
            ranks.append(scored.index(i//3)+1)
        one=sum(r==1 for r in ranks)/len(ranks);three=sum(r<=3 for r in ranks)/len(ranks)
        now=[life.clock()];life.catalog.clock=life.clock=lambda:now[0]
        first=await embed(['成功缓存淘汰后的同一查询'],'cache-generation')
        cached=await embed(['成功缓存淘汰后的同一查询'],'cache-generation')
        now[0]+=3601;adapter.cleanup()
        later=await embed(['成功缓存淘汰后的同一查询'],'cache-generation')
        cache_ok=(first['receipt']['call_key']==cached['receipt']['call_key']
            and first['receipt']['call_key']!=later['receipt']['call_key'] and later['receipt']['generation']==1)
        sharing=evaluate_sharing()
        ledger=[dict(r) for r in life.repo._all('SELECT day,reserved,charged,state FROM spending_calls')]
        result=dict(corpus='synthetic-zh-8-documents-24-queries-v1',queries=len(ranks),rankings=ranks,
            recall_at_1=one,recall_at_3=three,thresholds=dict(recall_at_1=.8,recall_at_3=.95),
            dimension=512,finite_normalized=True,http_call_ms=timings,cache_eviction_new_generation=cache_ok,
            sharing_cases=len(sharing),sharing_passed=sum(r['passed'] for r in sharing),
            actual_usage=[document_reply['actual_usage'],query_reply['actual_usage']],ledger=ledger,
            model_revision=config['model_revision'],price_version=config['price_version'],
            passed=one>=.8 and three>=.95 and cache_ok and all(r['passed'] for r in sharing)
                and all(r['state']=='settled' and r['charged']==0 for r in ledger))
        return result
    finally:
        fixture.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--existing',action='store_true',help='Use the verified live loopback endpoint; do not start or stop it')
    args=parser.parse_args()
    folder=ROOT/'data/models/bge-small-zh-v1.5'
    manifest=json.loads((folder/'model-manifest.json').read_text('utf-8'))
    digest=hashlib.sha256((folder/'model-manifest.json').read_bytes()).hexdigest()
    config=dict(enabled=True,endpoint='http://127.0.0.1:8231/v1/embeddings',model_id=manifest['model_id'],
        model_revision=manifest['revision'],provider_revision='local-cpu-cls-l2-v1:'+digest,dimension=512,
        api_key_env='',allow_external_text=True,price_version='local-cpu-zero-provider-fee-v1',input_nano_per_token=0,
        max_input_tokens=8192,max_request_bytes=262144,timeout_seconds=30)
    service=None if args.existing else LocalEmbeddingProcess(ROOT,dict(enabled=True,manifest_digest=digest),config)
    try:
        if service:
            service.start()
        else:
            with build_opener(ProxyHandler({})).open('http://127.0.0.1:8231/health',timeout=5) as response:
                health=json.load(response)
            if not health.get('ready') or health.get('manifest_digest')!=digest:
                raise ValueError('existing_endpoint_model_mismatch')
        print('verified loopback CPU model ready',flush=True)
        result=asyncio.run(evaluate(config))
        target=ROOT/'data/maintenance/memory-r1-r7-production';target.mkdir(parents=True,exist_ok=True)
        (target/'local-evaluation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
        return 0 if result['passed'] else 1
    finally:
        if service:service.close()


if __name__=='__main__':sys.exit(main())
