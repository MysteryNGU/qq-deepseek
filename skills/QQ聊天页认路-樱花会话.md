---
name: QQ聊天页认路（樱花会话）
when: 在 QQ 与主人的会话页里，需要返回列表、看资料卡、或用底部按钮时
preconditions: 前台是 com.tencent.mobileqq，顶栏标题是对方昵称
checkpoints: 顶栏标题能读出对方昵称；点返回后出现「消息」列表；点三横后出现会话菜单；点加号后底部弹面板
success: 0
fail: 0
last_verified: 0
version: 1
author: self
status: draft
---

## 步骤
1. see
2. verify 顶栏标题是对方昵称
3. tap 左上角返回箭头
4. tap 右上角三横
5. tap 左上角对方头像
6. tap 输入框右侧加号
7. see

## 失败过
- 三横、头像在界面树里没有文字节点，只能看截图找
- 拿小缩略图坐标直接点会经常点空，以 see 回来的整屏截图为准
