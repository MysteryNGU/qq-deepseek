"""Measure the capability/method context delta on one disposable continuity fixture."""
import argparse
import json
from pathlib import Path
import platform
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tests.role_fixture import Fixture
from tests.manual.benchmark_continuity import measure


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();f=Fixture()
    try:
        f.cfg.continuity_enabled=True;f.role.continuity_protocol=1
        sources=[]
        with f.life.repo.tx():
            for i in range(10000):
                event,_=f.life.ingest_message(f.scope,'Synthetic watercolor source '+str(i))
                if i%50==0:
                    sources.append(event.id)
                    f.life.memory.remember(f.scope,'episode','Synthetic study '+str(i),[event.id],source_kind='user_report')
            for i in range(12):
                f.role.intent(f.scope,dict(kind='ongoing',origin='self',goal='Study watercolor '+str(i),bind=False,
                    interest=dict(cues=['watercolor']),sources=[sources[i]]),f.binding)
                f.role.remember(f.scope,dict(kind='inference',text='A synthetic thought '+str(i),sources=[sources[i]],
                    continuity=dict(type='thought',action='create')),f.binding,call_key='bench-'+str(i))
        f.role.continuity.activate(f.scope)
        (f.root/'skills').mkdir()
        for i in range(13):
            (f.root/'skills'/('method-'+str(i)+'.md')).write_text('---\nname: method-'+str(i)+
                '\ndescription: Study a small synthetic example.\n---\n'+'Method body stays on demand. '*30,encoding='utf-8')
        f.role.capability_protocol=None
        baseline=measure(f,True,100)
        f.role.capability_protocol=1
        enhanced=measure(f,True,100)
        result=dict(schema=1,created_at=time.time(),python=platform.python_version(),platform=platform.platform(),
            events=10000,ordinary_memories=200,interests=12,thoughts=12,local_methods=13,iterations=100,
            baseline=baseline,enhanced=enhanced,added_p95_ms=enhanced['p95_ms']-baseline['p95_ms'],
            model_requests=0,privacy='disposable synthetic fixture; no production config/persona/database')
        args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,indent=2))
        return int(result['added_p95_ms']>100)
    finally:
        f.close()


if __name__=='__main__':
    raise SystemExit(main())
