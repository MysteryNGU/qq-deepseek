# Cloud/Local sync policy

Full text lives in the local repository (docs/cloud-sync-policy.md).
This cloud copy carries the hotfix clause added by t29.

---

## 5.5 热修（hotfix）之后，验收清单必须失效或补记

**规矩：任何绕过正常同步链路直接落到云端的改动（改一行 vendor JS、改 profile、手工替换一个 `.py`），都会让此前登记的「云端文件清单 + 哈希」立刻过期。**

1. **先失效，再动手**：hotfix 之前，把当前生效的验收清单标记为 `SUPERSEDED`（在清单文件顶部加一句说明并指向新清单路径），不要让它继续以「当前」的身份存在。
2. **补记后重建**：hotfix 完成后**必须重跑清单构建**（每个路径一个 hash），新清单写明：改了什么、为什么、验证命令与输出摘录、服务是否重启与启动时间。
3. **不许追加**：清单只能**整份替换**，不能在一份旧清单末尾追加条目。追加会让「这份清单到底对应哪一刻」变得无法回答。
4. **vendor 目录特别条款**：`toolchain/dsh/**` 下的改动会被 bundle 升级覆盖，因此清单里必须同时记录**补丁脚本路径**（例如 `.acceptance/patch_*.py`）与**还原点路径**（`.acceptance/restore-points/<stamp>-before-*`），以便升级后一键复核/重放。

判定方法（可观察）：清单文件的 `created_at` 早于目标文件的 `mtime`，即视为过期。
