# 手机端（`/mobile/*`）接口验证方法

**适用**：想验 `/mobile/state`、`/mobile/version`、`/mobile/tasks` 等路由是否正常，
或怀疑"手机连不上是不是登录坏了"的时候。

---

## 先说结论：`server-token` 走 `/mobile/*` 会 401，这是**正确行为**，不是故障

2026-11-18 实测：

```bash
TOKEN=$(cat /var/lib/companion/xiaoyu/data/companion/server-token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/mobile/version
# → HTTP 401  Unauthorized
```

第一次看到这个很容易判断成"登录坏了"或"token 过期了"。**都不是。**

## 为什么

`life/companion_mobile.py` 的 `authenticate()` 把两类凭据分得很清楚：

| 凭据 | 用途 | 走哪些路由 |
|---|---|---|
| **server-token** | 主人面（owner surface） | `/mobile/pairing`、`/mobile/link` |
| **`mob_` 开头的手机 token** | 已配对的那台手机 | 其余所有 `/mobile/*` |

而且手机 token **只以 sha256 的形式存盘**：

```python
device = self.devices.get(hashlib.sha256(token.encode()).hexdigest())
if not token.startswith('mob_') or not device or device.get('revoked'):
    raise web.HTTPUnauthorized()
```

所以**盘上根本没有明文手机 token 可以直接 `cat` 出来用 curl 测**。
拿 `server-token` 或随便编一个 `mob_xxx` 去请求，401 是设计如此。

这也是一层隔离：一台已配对的手机拿不到另一台的配对码
（`/mobile/pairing` 只认 server-token）。

## 那要怎么验？——直接调用 handler

绕开鉴权层、验**逻辑本身**（这也是 2026-11-18 验 `/mobile/version`、`/mobile/tasks`
实际用的方法）：

```python
# 在云端 /opt/qq-deepseek 下跑
import asyncio
from pathlib import Path
from life.role_config import load_config
from life.companion import Companion
from life import companion_mobile as cm

home = Path('/var/lib/companion/xiaoyu')

class M:                                   # 不真的调模型
    MAX_OUTPUT_TOKENS = 8000
    async def complete(self, *a, **k):
        return dict(message=dict(content='{}'), finish_reason='completed',
                    usage=None, model='fake')
    async def close(self): return None

class Req:
    query = {}
    match_info = {}
    def __getitem__(self, k): raise KeyError(k)

class S: pass

core = Companion(load_config(home / 'config.toml'), home, model=M())

async def go():
    api = cm.MobileAPI.__new__(cm.MobileAPI)
    s = S(); s.core = core; s.token = ''
    api.server = s
    for name in ('version', 'tasks', 'state'):
        fn = getattr(cm.MobileAPI, name, None)
        if fn is None:
            print(name, 'HANDLER MISSING'); continue
        try:
            r = await fn(api, Req())
            print(name, 'OK', getattr(r, 'text', str(r))[:300])
        except Exception as e:
            print(name, 'FAIL', type(e).__name__, e)

try:
    asyncio.run(go())
finally:
    core.life.close()
```

得到的真实输出（2026-11-18）：

```
version  OK  {"ok": true, "version": "0.4.7", "apk": "Xiaoyu-0.4.7.apk",
              "bytes": 176939, "url": "/downloads/Xiaoyu-0.4.7.apk",
              "folder": "/var/www/companion/downloads"}
tasks    OK  {"ok": true, "tasks": [{"id": ..., "task_ref": ..., "goal": ...}]}
```

## 什么时候用哪种方法

| 想验什么 | 用什么 |
|---|---|
| 路由**逻辑**对不对、返回结构是什么 | 直接调 handler（上文） |
| 鉴权**隔离**对不对（一台手机能不能看另一台） | 必须用真实 `mob_` token，从已配对设备侧发请求 |
| 端到端（真机 → 云端） | **只能在真机上测**，不能在服务器上模拟 |

## 不要做的事

- ❌ 不要为了"能 curl 通"就把 `/mobile/*` 放宽成认 server-token——那会破坏手机之间的隔离
- ❌ 不要把 `server-token` 塞进手机端前端
- ❌ 看到 401 不要直接判断成"登录坏了"，先确认自己用的是哪类凭据

---

相关：`docs/deploy-checklist.md` §0.7（同步范围必须显式声明）。
