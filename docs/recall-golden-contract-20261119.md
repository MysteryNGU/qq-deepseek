# 召回 golden 契约（v1，2026-11-19，设计 §8.2 P0）

**这是"当前状态"契约，不是"正确行为"契约。**
文件：`ops/oracle/golden_recall_v1.json`（29.6 KB，96 个组合）

## 一、结论先说：这份 golden 是 INCONCLUSIVE

按 `docs/recall-pipeline-plan-20261119.md` §4.3 的判据：

```
非空比例 < 80% → 报告标 INCONCLUSIVE，不算通过

实测：nonempty_ratio = 45.8%（52/96 为空或错误）
      errors: budget_requires_page × 44
      routes: 只有 text（52 条），semantic / related 贡献 0
```

**所以这份 golden 只能验证「text 路线在 budget=2400 下的行为不变」，
不能验证 semantic / related，也不能验证 budget=800 下的任何东西。**

**这个结论本身就是 P0 缺陷的证据**——不是我诚实标注，是数据就在那里。

## 二、按档统计

| budget | 结果 | 组合数 |
|---|---|---|
| 800 | `budget_requires_page` | 44 |
| 800 | OK（空串/纯符号 query，骨架更小） | 4 |
| 2400 | OK | 48 |

**`budget=800` 的 4 个 OK 全是边界 query（`''`、`'!!!???'`），
它们的骨架不含 `next_cursor`（无结果就没有下一页）。**

**这反过来证明：800 的失败取决于**查询有没有命中**，与 mode 无关。**

## 三、生成条件（自述，防止后人误用）

```json
"generated_under": {
  "recall_mode_override_in_shell": "",
  "bypassed_via": "ignore_mode_override=True"
}
"db_snapshot": {"events": 15323, "size_mb": 927.3}
"index_state": {
  "v1_ready": 27630, "v2_ready": 62818,
  "chunker_version": "utf8-paragraph-v2-drop-blank:5a62b...",
  "model_key": "a223055768fb..."
}
"params": {
  "budgets": [800, 2400], "limit": 10, "diagnostics": true,
  "allowed_resources": 9874, "scan_limit": 10000, "n_queries": 12
}
```

## 四、已知限制（原文写入 golden）

1. `budget=800` 低于空结果骨架（1154 token），必然抛 `budget_requires_page`；**这是当前状态，不是修复**
2. `semantic` 路线被 `scan_limit=10000` 截断，实测贡献 0 条
3. `related` 路线实测贡献 0 条
4. 本 golden 只验证 `text` 路线在 `budget>=1200` 时的行为，不能验证 semantic / related
5. **本文件是"当前状态"契约，不是"正确行为"契约**
6. 12 条 query 足以做回归对照，不代表全量查询分布

## 五、怎么用它（重构后）

```
1. 用 ops/oracle/ 下的脚本 .backup 一份新副本
   （本 golden 的原副本已删除，见第六节）
2. 对新实现跑同样的 12 query × 2 budget × 4 mode
3. 逐条比对：count / refs 顺序 / scores(≤1e-9) / coverage / routes / error
4. **比对前先断言两边都不是空**：
     assert old_error == new_error or (old_count > 0 and new_count > 0)
   t42 的教训：批量版那次 `一致=True` 是假的，因为两边都返回空
5. 若任一组合的 error 类别不同（例如旧是 budget_requires_page、新是 OK），
   那不算"一致"，要单独报告
```

## 六、副本已删除

`/var/lib/companion/oracle/` 下的 927 MB 副本与临时 home 已按指示删除。
需要时用脚本重建（`ops/oracle/rebuild_copy.py`），步骤：

```
1. sqlite3 .backup 生产库到 /var/lib/companion/oracle/oracle_<ts>.db
2. 建临时 home，config.toml 从生产 home 复制
3. 设 cfg.life_db = 副本绝对路径（不要在 home 里再放一个同名文件！见下）
```

**教训：SQLite WAL 模式下不要用硬链接给同一个库造第二个路径——
`-wal`/`-shm` 是配套的，共享 inode 会让 SQLite 报 `database disk image is malformed`。**
