#!/usr/bin/env python3
"""按设计 §5 统计召回三计数。

用法：
    python ops/scripts/recall_counts.py [--since-min 60] [--db /var/lib/companion/xiaoyu/data/life.db]

输出的分母按"合资格 dialogue 请求"计：排除 skip_reason='non_dialogue' 的轮次。
"semantic 成功运行"不等于"结果被用上"——只有 semantic_items>0 才是实际装入上下文。
"""
import argparse
import json
import sqlite3
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='/var/lib/companion/xiaoyu/data/life.db')
    ap.add_argument('--since-min', type=int, default=60)
    ap.add_argument('--agent', default='xiaoyu')
    args = ap.parse_args()

    since = time.time() - args.since_min * 60
    con = sqlite3.connect('file:%s?mode=ro' % args.db, uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT decision_id, slots, at FROM companion_context_loads "
        "WHERE agent_id=? AND at>=? ORDER BY at DESC", (args.agent, since)).fetchall()
    con.close()

    n_rows = len(rows)
    n_detail = 0
    skipped = timeout = ran = matched = loaded = 0
    for r in rows:
        try:
            slots = json.loads(r['slots'] or '{}')
        except Exception:
            continue
        d = (slots.get('recall') or {}).get('routes_detail')
        if not d:
            continue
        n_detail += 1
        st = str(d.get('status') or '')
        if d.get('skip_reason') == 'non_dialogue' or st == 'skipped_non_dialogue':
            skipped += 1
        elif st == 'semantic_timeout':
            timeout += 1
        elif st in ('semantic_candidates', 'semantic_no_matches', 'semantic_unavailable'):
            ran += 1
            if st == 'semantic_candidates':
                matched += 1
            if int(d.get('semantic_items') or 0) > 0:
                loaded += 1

    eligible = n_detail - skipped
    print('时间窗: 最近 %d 分钟' % args.since_min)
    print('companion_context_loads 行数: %d' % n_rows)
    print('其中带 routes_detail 的:      %d' % n_detail)
    print()
    print('合资格分母（不含非 dialogue）: %d' % eligible)
    print('  ├ 非 dialogue 跳过（不算失败）: %d' % skipped)
    print('  ├ semantic 超时:                %d' % timeout)
    print('  └ semantic 成功运行:            %d' % ran)
    print('       ├ 找到匹配:                 %d' % matched)
    print('       └ 实际装入上下文:           %d' % loaded)
    if n_rows and n_detail == 0:
        print()
        print('注意: 有记录但没有 routes_detail —— 说明该时间窗内没有走新代码的认知轮次，'
              '不能据此声称"语义召回成功"。')


if __name__ == '__main__':
    main()
