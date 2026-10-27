"""Local memory operator client. Mutations run in the live worker, never here."""
import argparse
import json
from pathlib import Path
import sys
import time
import tomllib
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','repair','retry','pause','park','share','reconcile','import-bill','verify-history','verify-qq','verify-autonomy'])
    parser.add_argument('--target')
    parser.add_argument('--proposal',type=Path,help='Versioned ordinary excerpt proposal for local operator review')
    parser.add_argument('--bill',type=Path,help='Supplier invoice evidence JSON; no inferred or zero-filled usage')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--wait',type=float,default=30)
    args=parser.parse_args()
    cfg=tomllib.loads((args.root/'config.toml').read_text('utf-8'))
    identity=uuid.uuid4().hex
    folder=args.root/'data'/'memory-admin';folder.mkdir(parents=True,exist_ok=True)
    request=dict(id=identity,agent_id=cfg.get('life_agent','default'),action=args.action,target=args.target)
    if args.bill:
        request['bill']=json.loads(args.bill.read_text('utf-8'))
    if args.proposal:
        request['proposal']=json.loads(args.proposal.read_text('utf-8'))
    path=folder/(identity+'.request.json')
    temporary=path.with_suffix('.writing');temporary.write_text(json.dumps(request),'utf-8');temporary.replace(path)
    output=folder/(identity+'.receipt.json')
    until=time.monotonic()+args.wait
    while time.monotonic()<until:
        if output.exists():
            value=json.loads(output.read_text('utf-8'))
            print(json.dumps(value,ensure_ascii=False,indent=2))
            return 0 if value['ok'] else 1
        time.sleep(.25)
    print(json.dumps(dict(state='pending',request_id=identity,receipt_path=str(output)),ensure_ascii=False))
    return 2


if __name__=='__main__':
    sys.exit(main())
