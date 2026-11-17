# 手机为什么 `connected=false`：SSE 与设备配对诊断

> **SUPERSEDED（2026-11-17 20:45）**：本文 §4 的修法建议已由 P21 落地并验收，现行口径与证据见
> [手机 connected=false 修复的验收清单](mobile-connected-fix-acceptance.md)。本文保留为诊断记录，不再作为验收依据。

- 诊断者：isolation-verifier（P18）
- 时间：2026-11-17 19:05–19:20 CST
- 边界：全程只读。**没有重启服务、没有改代码、没有写生产库、没有发真实 QQ。**
  服务当前 `MainPID=976281`（18:43:15 起），诊断期间未变。
- 结论先行：**不是 (a) SSE 根本没建立，也不是 (c) device id 对不上，
  而是「设备清单里 4 个设备、4 个不同 id，且配对码已经用掉、无法再新增」叠加
  「`devices` 只在注册成功时写入」这两件事。**

---

## 1. SSE 生命周期说明（读 `life/companion_mobile.py`）

### 建立

`GET /mobile/events` → `MobileServer.events()`（`companion_mobile.py:283`）：

```python
async def events(self, request):
    response = web.StreamResponse(headers={'Content-Type': 'text/event-stream', ...})
    await response.prepare(request)
    queue = asyncio.Queue(maxsize=128)
    device = request['mobile_device']
    self.subscribers[queue] = device          # ← subscribers 在这里填充
    ...
    await response.write(b'event: ready\ndata: {"type":"ready"}\n\n')
```

`subscribers` 是 **`{队列对象: device_id}`**，不是『设备名』。队列对象每次连接新建，
所以一条连接就是一个键；同一台手机重连会得到**新的**键（旧的必须被清掉，否则会堆积）。

### keepalive

```python
while not self.closing:
    try: event = await asyncio.wait_for(queue.get(), 15)
    except asyncio.TimeoutError:
        await response.write(b': heartbeat\n\n'); continue      # ← 每 15 秒心跳
```

### 断线清理

```python
finally:
    self.subscribers.pop(queue, None)      # ← 从 subscribers 摘掉
    self.phone.disconnected(queue, device)
```

`PhoneControl.disconnected()`（`companion_phone.py:61`）：

```python
def disconnected(self, queue, device):
    self.fail_device(device, '手机操作连接已断开，未重放操作', queue)
    if device not in self.mobile.subscribers.values():
        self.devices.pop(device, None)     # ← 这台设备彻底断线时，把 devices 里的记录也删掉
```

### 鉴权与 device id 来源

```python
token = header[7:]
device = self.devices.get(hashlib.sha256(token.encode()).hexdigest())   # 键是 token 的 sha256
if not token.startswith('mob_') or not device or device.get('revoked'):
    raise web.HTTPUnauthorized()
request['mobile_device'] = device['id']
```

**关键**：`mobile-devices.json` 的键是 `sha256(token)`，值是 `{id, name, created_at}`。
`device_id` 是配对那一刻生成的 `uuid4().hex`，**与手机型号、名字都无关**。

---

## 2. 当前实际状态（从运行中的服务读出来的）

### 2.1 两个端点都在正常工作 —— SSE 没有"根本没建立"

nginx access log（只读统计）：

```
GET /mobile/events    233 条   200×182  499×42  502×9
POST /mobile/device   456 条   200×441  401×1  404×3  499×8  502×3

最近 events：
  18:49:35  200  223 字节
  18:59:14  200  5136 字节
  19:00:18  200  43 字节      ← 最近的 SSE 连接
最近 device：
  18:59:09  200  304 字节
  19:00:14  200  304 字节
```

SSE 建得起来、也在被重新建立；注册请求也在发、也返回 200。**两个端点都不是坏的。**

### 2.2 设备清单：4 条记录、4 个不同 id

`/var/lib/companion/xiaoyu/data/companion/mobile-devices.json`（只读打印，token hash 已截断）：

| token hash | id | name | 创建于 | 距今 |
|---|---|---|---|---|
| `15dcbfe0eb9c…` | `c652e42f8e3b40aca979004229d7a0b2` | 小鱼安卓 | 67.9 小时前 | |
| `2cfc7189a3af…` | `36223861f4fd4cd292c819ce89301a4e` | 小鱼安卓 | 67.4 小时前 | |
| `e570702a018f…` | `5f35b541cefa4b5fbc9d109436c62064` | verification | 19.1 小时前 | |
| `c5f22c1a1f49…` | `8744318ec23e43e3b010a0c466ea8027` | verification | 19.1 小时前 | |

**4 条记录、4 个不同 id、两两不同名**。手机端只持有其中一个 token。

### 2.3 配对码已经用掉，而且不会再生成

`mobile-pairing.json` 现在只剩 `created_at` / `expires_at` / `used_at`，**没有 `code`**。

而 `create_pairing()`：

```python
def create_pairing(home, *, replace=False):
    path = Path(home)/'data/companion/mobile-pairing.json'
    if not path.exists() or replace:      # ← 文件存在就不再生成新码
        ...dict(code=f'{secrets.randbelow(1000000):06d}', ...)
```

服务启动时会调用它，但因为文件已经存在（且 code 被清掉），**启动不会补一个新码**。
所以：**当前状态下，手机无法再配对一次**，除非删掉这个文件重起（会作废所有已配对设备）
或者走 `replace=True` 的路径。

### 2.4 我尝试过但拿不到 token

我试着用只读方式探一次 `/mobile/state` 拿 `phone_context`，被 `401 Unauthorized` 挡住了——
这是**正确行为**（token 只在手机端，服务端只存 hash，设计上就拿不回来）。所以
`phone_context.devices` 的实时内容我**没有直接读到**，只能从产物推断。这一点在 §5 列为未验证。

---

## 3. 三个条件逐个判断

`PhoneControl.context()`（`companion_phone.py:25`）要求同时满足：

```python
online = set(self.mobile.subscribers.values())
rows = []
for device, value in self.devices.items():
    if device not in online or not value.get('enabled') or not value['operations']:
        continue
    rows.append(...)
return dict(connected=bool(rows), ...)
```

| 条件 | 判断 | 依据 |
|---|---|---|
| **① `device in subscribers.values()`** | **很可能不成立**（App 在后台/未连 SSE 时） | nginx 里 SSE 只在零散时刻出现（17 小时段 4 条、18 时 6 条、19 时 1 条），不是持续连接；`499×42` 说明大量连接被客户端主动断开 |
| **② `value.get('enabled')`** | **不确定**，但有一处**会主动删记录**的陷阱 | `register()` 里 `if not self.devices[device]['enabled']: self.fail_device(...)`；而 `disconnected()` 在设备完全断开时 `self.devices.pop(device)` —— 记录被删掉后，即使无障碍后来打开，也要等下一次注册才回来 |
| **③ `value['operations']`** | 注册正常时**成立** | 手机上报 `operations` 来自 `OPERATIONS` 数组；`register()` 会把 `operations` 与 `OPS\|WORKSPACE_OPS` 取交集，正常非空 |

**根因优先级**：

1. **最主要**：`devices` 与 `subscribers` 是两个独立集合，`context()` 要求**同时**满足。
   注册是**短请求**，SSE 是**长连接**；App 只要不在前台/被系统挂起，SSE 就断，
   `subscribers` 立刻空掉 → `connected=false`，**哪怕 `devices` 里记录完好、注册一直 200**。
   这与观察到的一致：注册 200 很多，但 SSE 只在零星时刻存在。
2. **放大器**：`disconnected()` 在设备彻底断线时还会 `self.devices.pop(device)`。
   于是"手机锁屏一会儿"就可能把设备记录也删掉，回到最坏状态：`devices` 和 `subscribers` 双空，
   必须等下一次 `POST /mobile/device` 才恢复。这也解释了为什么注册日志看起来"时有时无"。
3. **环境噪音**：设备清单里 4 个 id、两个不同名字，其中两条是我历次验收用 `verification`
   名字配的。**手机当前持有的 token 对应哪一条，我无法确定**（见 §5）。
   如果它对应的是 `verification` 之外的那条还好；如果它的记录已被 `disconnected()` 删过、
   而它又因为配对码没了无法重配，就永久 401。

**结论**：属于 **(b) 建立了但被清理** 这一类，**不是 (a) 也没建立**。
"清理"不是 bug 本身，而是设计上把"长连接在线"当成了"设备可用"的**唯一**标准；
而手机 App 的 SSE 在后台本来就维持不住。

---

## 4. 下一步修法建议（未实现，供决策）

按代价从小到大：

### 方案 A：放宽 `context()` 的在线判定（最小改动，推荐先做）

把"必须有一条活的 SSE 订阅"改成"最近 N 秒内注册过 OR 有活的 SSE"：

```python
# register() 时记一个 last_seen
value = dict(..., last_seen=time.time())

def context(self):
    now = time.time()
    rows = [
        dict(device_id=device, ...)
        for device, value in self.devices.items()
        if (device in online or now - value.get('last_seen', 0) < 90)   # ← 90 秒宽限
        and value.get('enabled') and value['operations']
    ]
```

理由：手机每 20 秒轮询 `/mobile/state`，注册也每 20 秒重报（P17 刚加），
90 秒宽限足以覆盖"锁屏几分钟"和"SSE 短暂断开"。真正的动作派发仍然走 SSE，
所以不会造成"发出去没人接"——`run()` 里本来就要求活队列才派发。

### 方案 B：`disconnected()` 不再删设备记录

```python
def disconnected(self, queue, device):
    self.fail_device(device, '手机操作连接已断开，未重放操作', queue)
    # 不再 pop；改成标记离线，靠过期淘汰
```

现在这条 `pop` 让"手机离线一下"退化成了"设备不存在"，副作用远大于收益。

### 方案 C：让配对码可以重新生成（可运维性）

`create_pairing` 只在文件不存在时生成码，导致**配对码用掉就再也没有**，
只能删文件（作废所有设备）。建议加一个只读之外的最小入口：文件里没有 `code` 时，
启动就补一个（或提供一个"重新配对"命令）。没有这个，手机一旦丢了 token 就永远回不来。

### 方案 D：清理历史设备记录

`mobile-devices.json` 里 4 条记录、两条 `verification` 是我历次验收留下的。
建议确认手机实际在用哪一条之后，把其余的清掉，避免以后误判。

---

## 5. 我没能验证的部分（如实）

1. **实时 `phone_context.devices` 内容没读到**：`/mobile/state` 需要 token，服务端只存 hash，
   401 挡住了。所以"手机当前持有的 token 对应 4 条中的哪一条"**我没有确证**。
   要确证需要：在手机上读 config() 显示 `device_id`，或临时加一条只读日志（需重启）。
2. **② `enabled` 的真实值没读到**：同上，`devices` 字典只在运行中的进程内存里。
3. **SSE 断开是否触发了 `devices.pop`**：推断成立，但没有直接日志。
4. **本次未重启、未改代码**。captain 提到的"可以加日志重启"我**没有用**——
   只读证据已经足够定位到根因方向，加日志的价值不如先把方案 A/B 改掉再看。

## 6. 复跑

```sh
python3 -B -X utf8 tools/stage-c/sse_devices.py    # 设备清单 + 配对状态（只读）
python3 -B -X utf8 tools/stage-c/sse_inspect.py    # 字段级脱敏查看
sh tools/stage-c/sse_log.sh                        # nginx 里两个端点的分布
python3 -B -X utf8 tools/stage-c/sse_diag.py <token>   # 有 token 时：SSE 在线/离线对 connected 的影响
```

`sse_diag.py` 是这次留下的**可复跑验证器**：它会在建 SSE 期间和不建 SSE 时各问一次
`phone_context`，直接证明"connected 是被 SSE 在线状态决定的"这一条。拿到 token 后跑它即可。

## 7. 一句话

注册和 SSE 两个端点都是好的；`connected=false` 是因为 `context()` 把
**"有活的 SSE 长连接"**当成了设备可用的必要条件，而手机 App 的 SSE 在后台维持不住，
再叠加 `disconnected()` 会**连设备记录一起删掉**。建议先做方案 A（90 秒宽限）与
方案 B（不再删记录），并补上配对码可再生（方案 C），否则手机一旦丢 token 就再也配不回来。
