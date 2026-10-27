# 小鱼：QQ 表情发送设计

核查日期：2026-10-25。仅设计，未修改业务代码，未执行收藏、发送或删除操作。

## 结论与证据边界

**本地 JPG/WebP 的首选路线是 `image + sub_type: 1`（自定义表情图片），不是把收藏编号塞进 `mface`。** 这里的 `image` 是 OneBot 的承载类型；`sub_type: 1` 才把它与普通图片 `sub_type: 0` 区分开。QQ 内核的 `PicSubType.KCUSTOM = 1`，`MFACE` 则是独立的商城表情元素。[内核枚举](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/types/msg.ts#L118)

本机已定向阅读 `life/expression.py`、`life/delivery.py`、`life/media.py`，以及布局、媒体导入、传输的相关函数；没有通读仓库。关键证据：

| 位置 | 实际行为 |
| --- | --- |
| `life/expression.py`，`Expressions.__call__` | image 只生成 `data.file`，不传 `sub_type`；此外还支持 text、voice/audio、mention，并非严格只有三种内部 part |
| `life/message_layout.py`，`expression_parts` / `expression_body` | 非 text part 原样保留；参与表达哈希，适合新增 sticker 类型 |
| `life/media.py`，`MediaService.require/resolve/read_bytes/register` | 检查 scope、来源、遗忘版本、文件 SHA256；metadata 可保存表情映射 |
| `life/role_agent.py`，`RoleAgent.read_capability` 的 media/path 分支 | 主人会话已有项目内文件导入入口，返回 image asset；可导入 `data/stickers/whale/01_hi.webp` |
| `life/delivery.py`，`DeliveryService.prepare/settle` | 完整 payload 入账；`retcode=0`、`status=ok`、存在 `data.message_id` 才 confirmed；未知结果禁止盲目重发 |

本机 NapCat 位于 `napcat-onekey/NapCat.52230.Shell/napcat.mjs`，bundle 标注 **4.18.38**，SHA256：`f80e4be3e8640369d23846cf3e8bcfd2a5b6dc5920f50d7ad733d7cca18179e6`。以下行号按本次 UTF-8 `splitlines()` 计数，升级后应用符号定位：

- `AddCustomFace._handle`（压缩类名 `gxe`，约 77712）：`stat(file)`、计算 MD5、直接返回原生 `addFavEmoji` 结果。
- `FetchCustomFaceDetail._handle`（`fxe`，约 77687）：直接返回 `fetchFavEmojiList(count).emojiInfoList`。
- `ob11ToRawConverters[mface]`（约 74013）：直接构造 `marketFaceElement`，不查收藏列表。
- `ob11ToRawConverters[image]`（约 74031）→ `createValidSendPicElement`（约 75085）：把 `data.sub_type` 原样传给 `picSubType`，默认 0。
- `rawToOb11Converters.picElement`（约 73539）：历史解析保留 `data.sub_type`；`marketFaceElement`（约 73647）上报为 image，带商城三字段。

已实际只读调用 `ws://127.0.0.1:3001`：`get_version_info` 返回 4.18.38；`fetch_custom_face_detail(count=48)` 返回 `ok/0/data=[]`。最初探测受默认代理影响，按现有 `RoleHost` 使用 `proxy=None` 后成功。**尚未实测添加回执内容、两类发送及接收端收藏操作；以下为代码支持的方案与验收步骤。**

## 正确调用顺序与编号来源

### 本地素材：发送与加入收藏是两个操作

最短发送顺序：验证文件实际格式/大小 → 导入当前 scope 的 image asset → 生成 `image {file, sub_type:1}` → `send_private_msg` → 按 message_id 查历史 → 接收端确认能收藏。发送转换器不要求预先收藏。

如果还要求素材进入**机器人账号自己的收藏栏**：先读收藏列表并按 MD5 去重 → `add_custom_face` → 再读详情确认 → 缓存映射 → 仍以 `image + sub_type:1` 发送。收藏成功不是发出消息，更不是接收端已收藏。

```json
{"action":"add_custom_face","params":{"file":"D:\\projects\\qq-deepseek\\data\\stickers\\whale\\01_hi.webp","is_mark_face":false,"is_origin":true},"echo":"fav-add-001"}
```

该版本 `file` 必须是 **NapCat 进程可读的原生本地路径**；它没有走普通 image 的文件解析器。不能直接传 `base64://`、HTTP URL 或 `file:///` URI；拿到 base64 时先解码落盘。即使指定 file_size 和 md5，代码仍先 `stat(file)`。这修正了“本地路径或 base64 均可”的假设。[上游实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/extends/CustomFace.ts)

| 字段 | 来源与用途 |
| --- | --- |
| add 的 `md5` | 未传时计算实际文件字节 MD5；传入则信任调用者。转换格式后必须重算，不能用 `media_assets.digest` 的 SHA256 代替 |
| add 的 `emoji_id` / `package_id` | 可选输入，默认 `""` / `0`；不是本地图片必须自行生成的编号 |
| add 的成功 `data` | `Type.Any`，原样透传原生结果；源码没有承诺返回 emoji_id、md5、res_id、package_id，也没有组装这些字段。必须留存实机响应，不编造固定成功示例；如有内层 result/errMsg 也需核对 |
| detail 的 `md5`、`resId` | 用文件 MD5（忽略大小写）匹配详情；resId 是原生收藏资源标识，可用于收藏管理，不是 mface.key |
| detail 的 `emoId`、`eId`、`epId` | 原生字段名，并非统一的 emoji_id/package_id；不要互相强转或当商城授权字段 |
| detail 的 `emoPath` / `emoOriginalPath` / `url` | 可用于定位可读素材后，以自定义表情图片发送；检查存在性，必要时下载并缓存 |
| mface 的三字段 | 从真实商城表情历史消息取 `emoji_id`、`emoji_package_id`、`key`，保持同一条消息的配套值；本地文件 MD5 不会生成商城 key |

`fetch_custom_face_detail` 的 OneBot `data` 直接是数组，不是 `{emojiInfoList:[...]}`。上游原生类型列有 `emoId/emoPath/resId/md5/eId/epId/isMarkFace/url` 等；本机这次返回空数组，尚无真实条目可确认每个字段是否齐全。[原生接口类型](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/services/NodeIKernelMsgService.ts#L319)

### 商城表情：复用真实 mface

顺序：在测试会话取得一条真实商城表情 → 读历史的完整结构 → 保存配套三字段 → 发送 mface → 按回执核验。普通本地文件无法通过 `add_custom_face` 注册成任意商城表情；设置 `is_mark_face:true` 也不能制造商城资源或 key。

## 精确 payload

本地素材推荐 payload（JSON 中路径转义；此处是 image 发送，所以可用 file URI）：

```json
{
  "action": "send_private_msg",
  "params": {
    "user_id": "<测试好友QQ号>",
    "message": [{"type":"image","data":{
      "file":"file:///D:/projects/qq-deepseek/data/stickers/whale/01_hi.webp",
      "sub_type":1,
      "summary":"[小鱼表情]"
    }}]
  },
  "echo": "sticker-send-001"
}
```

真实商城表情的 mface payload（占位值必须替换，不能直接发送）：

```json
{
  "action":"send_private_msg",
  "params":{
    "user_id":"<测试好友QQ号>",
    "message":[{"type":"mface","data":{
      "emoji_id":"<历史消息中的emoji_id>",
      "emoji_package_id":123456,
      "key":"<同一条历史消息中的key>",
      "summary":"[小鱼表情]"
    }}]
  },
  "echo":"market-send-001"
}
```

此例的 `123456` 也是占位包号。**`send_private_msg` 可以直接带 mface 数组段**，它使用通用消息转换器。

| mface.data 键 | 本机声明/转换行为 |
| --- | --- |
| `emoji_id` | string，真实商城表情 ID |
| `emoji_package_id` | number，真实包号；不是 add 接口的 `package_id` 键 |
| `key` | string，真实商城资源 key；没有由 MD5/resId 推导的实现 |
| `summary` | string；本机消息 schema 声明为必填，转换器对空值回退 `[商城表情]`，建议始终提供 |
| `file/md5/res_id/package_id` | 发送转换器不读取，不可作为上面三字段的替代 |

官网旧格式页把 summary 标为可选、包号示例写成字符串；本机 schema 使用 number 且列出全部四键，以本机实现为准。商城上报也可能是 image，不能要求历史一定出现 `type=mface`。[消息格式说明](https://napneko.github.io/develop/msg#mface)、[发送转换实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/api/msg.ts#L709)

## 桥接侧最小改动（本次不实施）

1. **`life/expression.py`，`Expressions.__call__`**：新增内部 part `{"kind":"sticker","asset_id":"..."}`。与 image 共用 `resolve → require/read_bytes`，确认资产 kind=image 后渲染 `type=image,data={file:本地资产URI,sub_type:1,summary:"[小鱼表情]"}`，`delays.append(1)`。这样“发图”和“发表情”有明确意图；现有普通 image 行为继续使用默认 0。
2. **`life/media.py`，`MediaService`**：第一阶段不必改表结构或资产 kind。文件导入复用 `RoleAgent.read_capability` 的现有 media/path 入口，先在主人 scope 内导入，复制进 data/media；不可把目录中的任意文件名直接当 asset_id。若需要收藏管理，可增 `ensure_custom_face(scope,asset_id,account_id)`：读取受校验字节、算 MD5、查详情去重、添加、复查；在 metadata 保存账号、MD5、resId、原始详情和核验时间。收藏超时先复查，避免重复添加；不要在每次聊天发送时上传收藏。
3. **`life/role_agent.py`，`ROLE_METHOD`**：说明 `life_say.parts` 的 sticker 意义与示例，让模型可以实际选用。`RoleAgent._say` 已将 parts 交给 Expressions；`expression_body/expression_parts` 会保留新 part，无需改布局函数。外部 DSH 若对 part.kind 另有枚举，须同步其工具 schema；本次未审阅外部 DSH，不能宣称只改 Python 就覆盖所有入口。
4. 真有商城素材时，再增内部 `kind:mface,sticker_id:...`，由受信目录解析出三字段，送入 `Expressions.__call__` 的独立分支；不让模型凭空编 ID/key。可在 `MediaService` 增相应映射查询，或独立目录服务，并在 `life/role_host.py` 初始化时接入。该扩展不是发送本地鲨鱼娘图片的前置条件。

送达链保持 `rendered` 冻结 → `prepare(ctx,action,params,segment=index)` → `claim` → transport → `settle`。新段天然随 payload 入账，receipt 仍是 `message_id`，无需增加发送 action 或把 add_custom_face 放进 SEND_ACTIONS。`describe/settle` 现在会显示 `[image]`；若需更清楚的预览，可区分 sub_type=1 为 `[表情]`，这是展示改进，不是送达条件。

自己的收藏管理作用于整个登录账号，不应继承“给当前会话回复”的收件人 grant。文件仍要校验 scope、来源、遗忘版本；商城映射绑定登录账号。跨会话使用公共素材时按既有导入路径重新建立当前 scope 的 asset，不能跳过 `MediaService.require`。额外收件人继续走 `send_authorized_message` 的现有授权。

## 逐步验证

以下是后续操作者执行的验证命令；本次只执行过版本、收藏详情两个只读请求。发送选专用测试好友，不用生产聊天窗口做批量试验。

在项目根目录用 PowerShell 定义一次请求函数（复用当前本地配置 token，不输出 token；每次独立 echo，忽略事件/心跳）：

```powershell
$ObProbe = @'
import asyncio, json, os, uuid
from pathlib import Path
import websockets
async def main():
    cfg = json.loads(Path('napcat/onebot11.json').read_text(encoding='utf-8-sig'))
    server = next(s for s in cfg['network']['websocketServers'] if str(s.get('port')) == '3001')
    token = server.get('token')
    headers = {'Authorization': 'Bearer '+token} if token else None
    req = json.loads(os.environ['OB_PROBE_REQUEST'])
    req['echo'] = uuid.uuid4().hex
    async with websockets.connect('ws://127.0.0.1:3001', additional_headers=headers,
                                  proxy=None, open_timeout=5, max_size=32*1024*1024) as ws:
        await ws.send(json.dumps(req, ensure_ascii=False))
        async with asyncio.timeout(30):
            while True:
                response = json.loads(await ws.recv())
                if response.get('echo') == req['echo']:
                    print(json.dumps(response, ensure_ascii=False, indent=2))
                    if req['action'] == 'get_friend_msg_history':
                        from life.media import onebot_segments
                        for msg in (response.get('data') or {}).get('messages', []):
                            print(json.dumps({'message_id':msg.get('message_id'),
                                'sender':msg.get('sender'),
                                'segments':onebot_segments(msg.get('message', []))}, ensure_ascii=False))
                    return
asyncio.run(main())
'@
function Invoke-Ob($Action, $Params) {
    $env:OB_PROBE_REQUEST = @{action=$Action; params=$Params} | ConvertTo-Json -Depth 15 -Compress
    $ObProbe | .\.venv\Scripts\python.exe -X utf8 -
}
Invoke-Ob 'get_version_info' @{}
Invoke-Ob 'fetch_custom_face_detail' @{count=48}
```

1. **文件与账号**：执行 `get_login_info` 确认登录账号；选一张素材，核对真实格式和 MD5。WebP 若本机 QQ 拒绝，先用工具转 PNG/GIF，再对转换后的文件测试与算 MD5；改扩展名不算转换。

```powershell
Invoke-Ob 'get_login_info' @{}
$StickerFile = 'D:\projects\qq-deepseek\data\stickers\whale\01_hi.webp'
Get-Item -LiteralPath $StickerFile | Select-Object FullName,Length
Get-FileHash -LiteralPath $StickerFile -Algorithm MD5
```

2. **可选收藏验证**：先确认详情中没有相同 MD5，再添加。检查外层 status/retcode、内层原生结果，随后再读详情，以 MD5 命中为收藏确认依据。若 48 条不覆盖目标，增大 count；当前包装器未暴露分页游标，不能把一次未命中当作“不存在”。空列表若与 QQ 收藏栏不符，先核对账号/同步状态。

```powershell
Invoke-Ob 'add_custom_face' @{file=$StickerFile; is_mark_face=$false; is_origin=$true}
Invoke-Ob 'fetch_custom_face_detail' @{count=200}
```

3. **本地表情发送**：明确设定测试好友 QQ 号后执行一次。要求 `status=ok,retcode=0,data.message_id`，记录完整回执；超时或缺少 message_id 即停止重发，先查历史。

```powershell
$TestQq = '<测试好友QQ号>'
$StickerUri = ([System.Uri]$StickerFile).AbsoluteUri
Invoke-Ob 'send_private_msg' @{user_id=$TestQq; message=@(
    @{type='image'; data=@{file=$StickerUri; sub_type=1; summary='[小鱼表情]'}}
)}
Invoke-Ob 'get_friend_msg_history' @{user_id=$TestQq; count=20; reverse_order=$false}
```

4. **历史判据**：按发送回执的 `message_id` 精确找记录，核对发送账号、目标会话、时间与素材。当前配置 `messagePostFormat=string`，history.message 可能是 CQ 字符串；上述函数使用已有 `onebot_segments` 归一化，勿只检索字符串或只看 raw_message。

| 场景 | 对应消息的结构判据 |
| --- | --- |
| 自定义表情图片成功 | `type=image` 且 `int(data.sub_type)==1`，与该次素材相符 |
| 普通图片 | `sub_type=0`；字段缺失时证据不足，不能算表情验收通过 |
| 商城 mface 成功 | 本机通常回报 `type=image`，带匹配的 `emoji_id/emoji_package_id/key`；不能把 `type=image` 一概判失败 |

`summary` 写着“表情”、GIF 扩展名、能打开图片、收藏列表有记录都不是发送类型证据。历史窗口找不到则扩大 count，或用 `get_msg {message_id:回执ID}` 精确查；仍查不到，记录待核验，不能自动补发。

5. **接收端验收**：好友客户端看它呈现为表情，尝试打开菜单并添加到收藏，再从收藏发回一遍，检查返回消息结构。发送端历史只能证明平台记录中的类型，不能独自证明对方已收到/已读或收藏按钮可用；以接收端实际操作完成最终验收。
6. **商城复用验收（可选）**：先在该会话放入真实商城表情，用历史取得三字段，再按上面的 mface payload 发一次并查回执对应历史。不能拿本地收藏条目的 emoId/resId 代替这些字段。
7. **桥接回归（实施后）**：通过真实 `life_say.parts` 发 sticker；检查 expressions.rendered 和 deliveries.payload 中有 `sub_type:1`，确认 receipt.message_id 对应历史；再次提交同一逻辑表达不产生新消息；模拟发送超时不自动降级重发；普通 image、voice/audio、跨 scope 拒绝和来源遗忘仍按原逻辑工作。可扩展 `tests/test_media_expression.py`，覆盖这些行为。

## 失败回退与风险

- `add_custom_face` 不存在或失败：本地发送不依赖它，继续验证 `image + sub_type:1`。需要自己的收藏栏时可手工收藏一次，再取 detail；已有收藏直接复用可读路径/URL。只有 emoji_id/emoId 仍不足以发送本地自定义表情。
- mface 缺 key、包号不匹配或资源不可用：从真实商城消息重新取得配套字段；若有可用图像文件，改走自定义表情图片路线，明确不再是同一商城身份。
- 本地表情链路失败：检查路径权限、真实格式、大小、NapCat/QQ 版本。只有确认未发送且产品允许时，才另建一次普通 image（sub_type=0）或文字替代，并说明降级；不能把普通图片记为“已发表情”。
- 已 dispatching/unknown：先按消息 ID 或会话时间窗对账，不能换编号重复发送、自动改图重发或在原 delivery 上替换 payload。收藏操作超时也先按 MD5 复查。
- JPG/WebP 不同 QQ 客户端表现、动画支持、收藏容量及同步均需实机确认；源码证明转换链路，不保证所有版本 UI 一致。详情 URL 可能失效，应保存受控本地副本；重编码后重新计算 MD5。
- 本方案不要求删除收藏。若测试后确需清理，只删已确认属于本次新增素材的 resId，不能按 MD5 猜 ID、不能清空原有收藏。
