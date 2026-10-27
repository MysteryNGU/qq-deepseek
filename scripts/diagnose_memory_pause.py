"""Read-only memory pause evidence. Never opens a runtime or submits work.

Only identifiers, checkpoint counts, terminal errors and metering are exported;
source bodies and model reasoning are excluded.
"""
import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life.spending import reported_cost, PRICE_VERSION, UNIT

DECODER = r'''
import fs from 'node:fs';
import z from 'node:zlib';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const [source, file] = process.argv.slice(1);
const { scanZstdFrames } = await import(pathToFileURL(join(source,'packages/session/session-persistence-jsonl/src/zstd.ts')).href);
const bytes = fs.readFileSync(file), scan=scanZstdFrames(bytes), rows=[];
for (const frame of scan.frames) rows.push(...z.zstdDecompressSync(bytes.subarray(frame.start, frame.end)).toString().trim().split('\n').map(JSON.parse));
const end=rows.findLast(r=>r.type==='turn/end'), attempt=rows.findLast(r=>r.type==='assistant/attempt');
const chunks=(attempt?.data?.stream||[]).filter(r=>r.type==='tool-call-chunks');
const args=chunks.flatMap(r=>r.args||[]).join('');
let valid=true; try { JSON.parse(args); } catch { valid=false; }
console.log(JSON.stringify({file,complete_frames:scan.frames.length,torn_tail:scan.tornStart??null,
  terminal_event:end?{seq:end.seq,type:end.type,reason:end.data.reason}:null,
  last_attempt:attempt?{seq:attempt.seq,step:attempt.data.step,tool_name:chunks.find(c=>c.name)?.name,
    argument_bytes:Buffer.byteLength(args),argument_json_valid:valid}:null,
  summary_commits:rows.filter(r=>r.type==='tool/call'&&r.data.name==='life_remember').length}));
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',required=True)
    parser.add_argument('--dsh-source',required=True)
    parser.add_argument('--dsh-home',required=True)
    parser.add_argument('--node',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    db=sqlite3.connect(Path(args.database).resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
    def rows(sql,params=()):return [dict(r) for r in db.execute(sql,params)]
    result=dict(sampled_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),read_only=True,
        database=str(Path(args.database).resolve()),jobs=[])
    for job in rows("SELECT id,status,error,attempts,lease_until,lease_token,updated,checkpoint FROM work_jobs WHERE kind='memory_organize' AND status NOT IN ('done','cancelled')"):
        metadata=rows("SELECT k,v FROM meta WHERE k LIKE 'memory_internal:%' AND json_extract(v,'$.job_id')=?",(job['id'],))
        if not metadata:continue
        m=json.loads(metadata[0]['v']);sid=metadata[0]['k'].split(':',1)[1]
        checkpoint=json.loads(job.pop('checkpoint'))
        job.update(session_id=sid,source_count=len(m['sources']),checkpoint=dict(
            pages_read=checkpoint.get('pages_read'),read_source_count=len(checkpoint.get('read_refs',[])),
            source_read_complete=checkpoint.get('source_read_complete'),cursor_ref=checkpoint.get('cursor_ref')),
            execution_lease_released=not job.pop('lease_token') and job['lease_until']==0,
            session=rows('SELECT status,generation,cursor FROM agent_sessions WHERE session_id=?',(sid,)),
            input_receipts=rows('SELECT input_id,state,handled FROM agent_inputs WHERE session_id=?',(sid,)),
            pending_reader_stop=rows('SELECT k FROM meta WHERE k=?',('life_retire_reader:'+sid,)))
        usage=[json.loads(r['payload']) for r in rows("SELECT payload FROM usage_calls WHERE json_extract(payload,'$.session_id')=? OR json_extract(payload,'$.root_session_id')=?",(sid,sid))]
        job['usage']=[{k:u.get(k) for k in ('call_key','provider','model','status','step','started_at','finished_at','coverage',
            'uncached_input','cache_read','cache_write','output','provider_total')} for u in usage]
        known=[reported_cost(u) for u in usage if reported_cost(u) is not None]
        job['cost_evidence']=dict(known_calls=len(known),unknown_calls=len(usage)-len(known),
            known_usage_priced_upper_bound_yuan=sum(known)/UNIT,price_version=PRICE_VERSION,
            actual_total_yuan=None,
            note='按现有代码价格版本计价，非供应商完整发票；缺失usage仍未知')
        job['spending_rows']=rows('SELECT call_key,day,reserved,charged,state FROM spending_calls WHERE session_id=?',(sid,))
        files=list(Path(args.dsh_home,'sessions').glob('*/'+sid+'/session.v4.jsonl.zstd'))
        if len(files)==1:
            process=subprocess.run([args.node,'--import','tsx/esm','--input-type=module','-e',DECODER,
                str(Path(args.dsh_source).resolve()),str(files[0].resolve())],cwd=args.dsh_source,
                capture_output=True,check=True,encoding='utf-8')
            job['session_log']=json.loads(process.stdout)
        result['jobs'].append(job)
    result['checkpoints']=[dict(key=r['k'],value=json.loads(r['v'])) for r in rows(
        "SELECT k,v FROM meta WHERE k LIKE 'memory_review_window:%' OR k LIKE 'memory_organize_cursor:%'")]
    db.rollback();db.close()
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('jobs','checkpoints')},ensure_ascii=False))
    for job in result['jobs']:
        print(json.dumps({k:job[k] for k in ('id','status','error','checkpoint','cost_evidence','session_log') if k in job},ensure_ascii=False))


if __name__=='__main__':
    main()
