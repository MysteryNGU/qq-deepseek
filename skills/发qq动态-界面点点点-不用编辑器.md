---
name: 发qq动态-界面点点点-不用编辑器
when: 主人让小鱼在 QQ 里发「空间动态」（QQ空间那条），只能用手机界面点，不用编辑器
preconditions: QQ 在前台；已经知道要发的正文
checkpoints: 动态列表里能看到刚发的那条；最后回到主人会话里把结果说出来
success: 1
fail: 0
last_verified: 1792513783
version: 1
author: self
status: active
---

## 步骤
1. see 确认前台是 QQ、标题对得上
2. tap 左上角的 < 返回 或 swipe 右滑，退出聊天界面
3. tap 底部「动态」
4. tap 右上角的加号
5. tap 输入框
6. type 正文
7. verify 输入框里有正文（前几个字）
8. hide_ime 收键盘，不然点发表会点到键盘
9. tap 「发表」
10. verify 列表里出现刚发的那条
11. key back 退出写动态页
12. tap 底部「消息」
13. tap 主人会话那一行
14. reply 向主人汇报结果

## 失败过
- 键盘开着点发表会多一个换行——先 hide_ime
- 点偏会打开别的页面，key back 退出来换位置重点
- 发送按钮被键盘挡住，先 hide_ime 再点
