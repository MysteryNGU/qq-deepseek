import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/p3verify-20261117-1257/result.json'
data = json.load(open(path, encoding='utf-8'))
print('SUMMARY', json.dumps(data['summary'], ensure_ascii=False))
wanted = sys.argv[2] if len(sys.argv) > 2 else 'B5'
for row in data['rows']:
    if row['scenario'] == wanted:
        print(json.dumps(row, ensure_ascii=False)[:700])
