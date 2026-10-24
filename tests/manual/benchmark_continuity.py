"""Opt-in continuity benchmark with 10,000 synthetic events and no model calls."""
import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.role_fixture import Fixture
from life.resource_policy import estimate_tokens


def measure(f,enabled,iterations):
    f.cfg.continuity_enabled=enabled
    binding=f.role.sessions.require(f.binding['session_id'])
    for _ in range(5):
        f.role.context(binding,f.scope,{})
    durations=[]
    queries=[]
    returned=[]
    tokens=[]
    query_counter=[0]
    rows_counter=[0]
    original=f.life.repo._all
    def traced(sql,args=()):
        values=original(sql,args)
        rows_counter[0]+=len(values)
        return values
    f.life.repo._all=traced
    f.life.repo.db.set_trace_callback(lambda _sql:query_counter.__setitem__(0,query_counter[0]+1))
    import psutil
    process=psutil.Process()
    rss_before=process.memory_info().rss
    rss_peak=rss_before
    tracemalloc.start()
    try:
        for _ in range(iterations):
            query_counter[0]=rows_counter[0]=0
            start=time.perf_counter()
            value=f.role.context(binding,f.scope,{})
            durations.append((time.perf_counter()-start)*1000)
            queries.append(query_counter[0])
            returned.append(rows_counter[0])
            dynamic=value.split('\n\n当前状态与材料：\n',1)[1]
            tokens.append(estimate_tokens(dynamic))
            rss_peak=max(rss_peak,process.memory_info().rss)
        python_current,python_peak=tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        f.life.repo.db.set_trace_callback(None)
        f.life.repo._all=original
    return dict(enabled=enabled,iterations=iterations,p50_ms=statistics.median(durations),
        p95_ms=sorted(durations)[int((iterations-1)*.95)],
        queries_per_context=dict(min=min(queries),max=max(queries)),
        returned_rows_per_context=dict(min=min(returned),max=max(returned)),
        dynamic_estimated_tokens=dict(min=min(tokens),max=max(tokens)),
        rss_before_mib=rss_before/2**20,rss_peak_mib=rss_peak/2**20,rss_after_mib=process.memory_info().rss/2**20,
        python_peak_bytes=python_peak,python_retained_bytes=python_current,model_requests=0,
        instrumentation='same SQLite trace, returned-row count and tracemalloc in both modes')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--events',type=int,default=10000)
    parser.add_argument('--iterations',type=int,default=100)
    args=parser.parse_args()
    if args.events<1 or args.iterations<20:
        parser.error('positive events and at least 20 iterations required')
    f=Fixture()
    try:
        f.cfg.continuity_enabled=True
        f.role.continuity_protocol=1
        sources=[]
        with f.life.repo.tx():
            for i in range(args.events):
                event,_=f.life.ingest_message(f.scope,'Synthetic source '+str(i)+' on watercolor composition.')
                if i%50==0:
                    sources.append(event.id)
                    f.life.memory.remember(f.scope,'episode','Synthetic watercolor study '+str(i),[event.id],source_kind='user_report')
            for i in range(12):
                f.role.intent(f.scope,dict(goal='Synthetic watercolor question '+str(i),kind='ongoing',origin='self',
                    bind=False,interest=dict(cues=['watercolor'],open_question='How does composition work?'),sources=[sources[i]]),f.binding)
                f.role.remember(f.scope,dict(kind='inference',text='Synthetic thought about watercolor '+str(i),sources=[sources[i]],
                    continuity=dict(type='thought',action='create')),f.binding,call_key='synthetic-note-'+str(i))
        f.role.continuity.activate(f.scope)
        baseline=measure(f,False,args.iterations)
        enhanced=measure(f,True,args.iterations)
        result=dict(schema=1,created_at=time.time(),platform=platform.platform(),python=platform.python_version(),
            logical_cpus=__import__('os').cpu_count(),synthetic_events=args.events,baseline=baseline,enhanced=enhanced,
            added_p95_ms=enhanced['p95_ms']-baseline['p95_ms'],privacy='disposable synthetic fixture; no live config/persona/database')
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return int(result['added_p95_ms']>100)
    finally:
        f.close()


if __name__=='__main__':
    raise SystemExit(main())
