# B站扫码登录兼容

`life/companion_bilibili_login.py` 在独立 Companion 的 Bilibili 桥中处理三个操作：`bili_login`、`bili_login_check`、`bili_check_credential`。接口仍返回原 MCP `content` 文本 JSON，连接设置无需改变扫码方式。其余工具继续使用原 MCP 后端；无额外模型调用。

## 修复的实际问题

2026-11-15 云端扫码后，旧版 `bilibili_api.login_v2.QrCodeLogin` 只从成功响应 URL 的 query 取 SESSDATA/bili_jct/DedeUserID，而实际登录响应通过 HTTP Set-Cookie 交付凭据，亦可能包含 crossDomain 单点登录地址。因此三个字段均为空，后端仍保存文件并报告 `done`，后续 `int('')` 失败。原 `bili_check_credential` 使用公开用户资料接口，也不足以证明会话有效。

该次现有登录会话已经恢复，并用真实 `/x/web-interface/nav` 的 `code=0`、`data.isLogin=true` 验证后才写入凭据文件；真实 MCP `bili_check_credential` 及 `bili_unread_messages` 均复核成功。没有重新扫码、评论或发送私信。

相关上游协议变更记录：[bilibili-cli PR 27](https://github.com/public-clis/bilibili-cli/pull/27)、[问题 32](https://github.com/public-clis/bilibili-cli/issues/32)。

## 现在的流程

1. 生成二维码前验证已有会话，空凭据不视为登录。
2. 用持久 HTTP cookiejar 请求二维码及轮询状态；仅明确成功码 `0` 进入校验。区分等待扫码、等待确认、过期、未知结果。
3. 优先读取成功响应 Cookie，兼容旧 URL query；必要时跟随已知 B站 HTTPS 单点登录地址，最多五次跳转。
4. 必须同时取得非空 SESSDATA/bili_jct，再请求认证 nav，真实 UID 来自 nav。校验成功后原子保存，Unix 权限 0600 并保留已有文件所有者；失败不覆盖已有凭据。
5. 成功轮询响应保留到完成验证，不重复消费同一个二维码；网络暂时失败可以再次检查。

`BilibiliLogin(folder).call(op)` 由已有 Bilibili 锁串行调用；`close()` 释放 HTTP 客户端。二维码会话在内存中，服务重启后尚未完成的扫码需要重新生成，已经验证保存的登录不受影响。错误输出不包含 HTTP 异常里的 ticket、Cookie 或凭据。

验证：`python -m unittest tests.test_companion_bilibili_login tests.test_companion_bilibili_social -q`，16 项通过。MockTransport 覆盖 Set-Cookie、crossDomain、旧 query、空成功/未知状态、未通过 nav 不覆盖、缺 UID 修复、成功轮询后重试，以及不向无关站点跟随跳转。
