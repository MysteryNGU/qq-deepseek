"""Compare completed real-model smoke reports without starting model requests."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3


def measurements(path):
    report = json.loads(path.read_text('utf-8'))
    records = report.get('model_usage')
    if records is None:
        records = [json.loads(line) for line in
            (Path(report['workspace'])/'dsh-home/qq-life/usage.jsonl').read_text('utf-8').splitlines()]
    latest = {}
    for record in records:
        if record['revision'] > latest.get(record['call_key'],{}).get('revision',0):
            latest[record['call_key']] = record
    sid = report.get('main_session_id') or Counter(r['session_id'] for r in latest.values()).most_common(1)[0][0]
    main = [r for r in latest.values() if r['session_id']==sid]
    assert main and all(r.get('status')=='completed' and r.get('usage') for r in main), 'Incomplete provider usage'
    totals = {k:sum(r['usage'].get(k,0) for r in main) for k in ('inputTokens','cacheReadTokens','cacheWriteTokens','outputTokens')}
    headers = report.get('request_headers')
    if headers is None:
        database = (Path(report['workspace'])/'life.db').resolve()
        connection = sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
        try:
            connection.execute('PRAGMA query_only=ON')
            headers = [dict(session_id=sid,tools=[t['name'] for t in json.loads(row[0])['data']['header'].get('tools',[])])
                for row in connection.execute("SELECT payload FROM agent_events WHERE session_id=? AND json_extract(payload,'$.type')='request/header' ORDER BY seq",(sid,))]
        finally:
            connection.close()
    tool_sets = [h['tools'] for h in headers if h['session_id']==sid]
    return dict(model_requests=len(main),uncached_input=totals['inputTokens'],
        total_input=sum(totals[k] for k in ('inputTokens','cacheReadTokens','cacheWriteTokens')),
        cache_read=totals['cacheReadTokens'],output=totals['outputTokens'],
        life_failures=report['life_failure_counts'],all_nine_succeeded=report['all_nine_succeeded'],
        initial_tools=tool_sets[0],distinct_tool_lists=len({tuple(t) for t in tool_sets}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before',type=Path,required=True)
    parser.add_argument('--after',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args = parser.parse_args()
    before,after = measurements(args.before),measurements(args.after)
    result = dict(before=before,after=after,reduction_percent={
        key:round(100*(before[key]-after[key])/before[key],2) for key in ('model_requests','uncached_input','total_input','output')},
        caveat='One synthetic workload per version; provider cache warmth and model choices vary. No production memory or QQ delivery used.')
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True))
