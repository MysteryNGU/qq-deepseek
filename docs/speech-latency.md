# 本地语音生成延迟

2026-10-26：IndexTTS 语音改为受 Bot 进程树管理的保温 worker。连续语音复用已加载的模型和当前参考音色缓存；原来的音色、模型、表达参数及 OneBot 音频资产发送路径保持不变。每次推理重新设置随机种子，模型初始化不会改变冷/热请求的种子语义。

本机 RTX 3070 Ti 8GB、当前默认音色、固定测试文本“今天的天气很好，我们一起出去散散步吧。”，包含真实资源准入的验证结果：

| 情况 | 任务耗时 | 音频长度 |
|---|---:|---:|
| 首次加载保温 worker | 30.263 秒 | 3.471 秒 |
| 同一 worker 再次合成 | 3.638 秒 | 3.471 秒 |

两次使用同一 worker PID，音频 SHA256 相同。这是固定短句的本机合成测量，不包括模型组织回复、排队、QQ 上传和接收端播放。此前另一轮独立冷进程曾耗时 157 秒，系统负载差异较大，不作为固定基线。

`[local_media].speech_idle_seconds` 默认 300：空闲五分钟卸载，生图/更换 provider 前也会停止旧语音进程并确认退出。取消、超时、worker 失败和 Bot 退出同样停止该进程。首条语音或卸载后的下一条仍需要加载；没有承诺每条消息都只需约四秒。

热请求的显存准入会计入 CUDA 可复用缓存，并按上一轮实际推理工作区峰值加 64 MiB 余量检查；冷启动继续使用既有空闲显存门槛。不会因为 nvidia-smi 将缓存记作“已用”而每次错误地重新加载。发生资源不足时，先卸载再等待资源；取消/来源失效仍会阻止结果发布。

语音 adapter 改变后，已有音色版本可能仍绑定旧源码摘要。只有本机配置显式声明 `runtime_compatible_digests`，且 engine、model_version、adapter_version 匹配时，才允许复用这些音色；旧音色记录不改写，旧冻结作业也不会静默重绑。本轮只批准原 IndexTTS adapter 摘要 `50349b1493bf62a42e504fe20c743f480808da2859aad400b7d607904bc62b94`，用于已实测的运行方式升级。更换模型或音色参数仍需原有登记与试音流程。

验证记录在 `data/maintenance/tts-latency/verification.json`、`deployment.json`，测速明细在 `.acceptance/tts-latency-final-20261026/comparison.json`。配置备份留在同一维护目录的 `config.before.toml`；它包含本机凭据，不应分享或提交。

重测使用新输出目录：

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_speech_latency.py --warm-only --output .acceptance\tts-latency-new-run
```

测速只在本机合成，不调用付费聊天模型，不向 QQ 发送消息。它读取当前默认参考音色，应与生产媒体任务错开运行。测试通过后仍需由实际聊天观察整条消息的发送体验。
