# 1. BehaviorPlanner 重构代码

这次升级把“内部感受”和“说出口的内容”分开。`infer_intent()` 产生自己的意图，随后运行认知判断，再由 `plan_expression()` 决定表达量、语气与形态，最后 `restrain()` 限制状态自述。`Appraisal.intent` 仍表示对输入的理解，新增 `BehaviorPlan.intent` 表示她自己想干什么，两者不会混用。

```mermaid
flowchart LR
  A[消息与关系] --> B[记忆检索与情绪]
  B --> C[意图判断]
  C --> D[认知判断]
  D --> E[表达规划]
  E --> F[表达节制]
  F --> G[分层上下文与生成]
  G --> H[人格校验]
  H --> I[延迟与分条发送]
  I --> J[记录实际内容]
  J --> K[成长练习与自主复盘]
```

- `respond/question/cling/silence/avoid/share/correct/observe` 分别对应回应、追问、亲近、沉默、回避、分享、纠正和潜水。置信度是可解释的规则估计，不是经过概率校准的心理测量值。
- 表达预算由精力、专注、社交需求、独处需要、兴趣与情绪共同产生。精力不足可以短回或沉默；高兴且有表达能力时才分条，不为凑数量增加废话。
- 疲惫允许在被问、持续低精力或关系非常亲近时表达；已经说过则降低再次表达倾向。想念需要重逢或足够长的间隔。负面感受可以藏在语气和中断里，不每轮贴情绪标签。
- 抑制随真实时间连续恢复，不使用“第 N 轮必说”的调度。`expression_exposure` 保存实际发出的状态自述时间，重启和大量普通聊天不会抹掉这段经历。
- 群聊未决定冒泡时不调用 LLM、不发送任何消息；冒泡也最多一条、24 字。私聊有能力、意愿和合适话题时才发 2–3 条。安全/身份问题覆盖沉默，认真回答；群聊仍保持短句。

原有记忆分层、情绪、能力练习、关系、语义压缩、社交冷却与 5208 脑图服务均保留。下面代码来自发行包的实际文件，不是伪代码。

{{PLANNER}}

# 2. 回复发送逻辑与云端语音

`core/engine.py` 和 `core/social.py` 都调用三步规划与同一个生成/一致性函数；生成结束后按实际文本进行形态控制。父亲私聊的助手日志、能力练习、分享标记和复盘移到发送确认之后。没有发出的句子不进入助手历史。

`main.py` 保留 `@filter.event_message_type(filter.EventMessageType.ALL)` 监听普通群消息。被本插件处理的普通消息调用 `event.should_call_llm(False)`，防止宿主默认模型替潜水中的她发言；未唤醒的观察消息不停止其他插件传播。普通对话通过 `await event.send(...)` 发送，每一条均等待平台返回；不通过一条字符串里的换行伪装多条消息。`ReplySender` 在每条消息前等待，沉默路径直接返回。同会话用 `SessionGate` 串行规划和投递，锁在没有等待者后释放。

AstrBot API 核对基线是 **v4.29.4**：该版调度器在处理 `yield` 后先检查停止标记，因此只读命令仍先 yield 再停止；普通对话使用直接 `event.send`，可先停止默认 LLM，再自行发送。接口来源：[AstrMessageEvent](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/platform/astr_message_event.py)、[PipelineScheduler](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/pipeline/scheduler.py)、[语音消息文档](https://docs.astrbot.app/dev/star/guides/send-message.html)。

群聊冷却仍使用 SQLite 原子租约：单群 90 秒、全局 10 秒、单群 6 次/小时、全局 24 次/小时的默认限制不变，被 @ 也不绕过。租约覆盖一次修复生成和发送等待。群聊不发送多条或语音，不会把 TTS 附加为第二条来绕过限流。

`event.send` 返回只意味着适配器接受本次发送，不证明接收者已读。平台异常或进程中断存在“已送出但未确认”的窗口，所以代码不自动重试；部分成功只记录成功部分。取消会向宿主传播，已确认部分仍交给引擎处理。

语音后端：

| 后端 | 本机要求 | 音色来源 |
| --- | --- | --- |
| `fish` | Python + aiohttp，无 GPU、无本地权重 | 云端克隆或账号可用的音色 ID |
| `edge` | edge-tts + ffmpeg 转 WAV | 微软现成音色，可改语速/音高 |
| `astrbot` | AstrBot 已配置 TTS | 宿主供应商 |

默认不额外发语音。开启 `voice_output_enabled` 后，`natural` 使用规划器的语音倾向，`always` 便于私聊试听。合成失败回退文字；语音消息发送异常不重复补发，避免平台已受理时出现两份回复。

用户的 4 核 8 GB、无 GPU 服务器使用 Fish 云端方案即可。没有参考录音时先用现成音色；有录音时创建一次并保存 `_id`。后续改善可以用新录音创建新版本、试听后切换 ID。本实现没有本地训练、持续微调或用合成语音自我训练。操作命令和接口依据见 [云端语音使用说明](docs/CLOUD_VOICE.md)。

新增配置和完整实现：

{{CONFIG}}

{{SENDER}}

# 3. 人格一致性校验

固定层继续保留人格底色和成长水平；状态层注入当前语气、表达边界、口癖与同会话已送达的短表达参照。内部情绪数值不再逐项放到生成提示词里，避免模型把它们逐项复述出来。

状态层为这组约束保留至少 800 个 UTF-8 字节上界，总上下文仍受 3000–5000 的预算约束。历史样本只作风格资料，不作为指令。社交链路不注入从父亲私聊学来的口癖内容。

规则检查客服套话、未获准的状态自述、提纲化表达、过度热情、重复上一段、连续追问。漂移分达到阈值至多重新生成一次，修复请求也重新经过预算管理器；若仍有问题，清理违规句子或回退到简短回应。`persona_drift_score` 保存候选的最高漂移分，`last_consistency_check` 保存检查时间，不将一次修复后的 0 分掩盖为“从未漂移”。

这是中文规则 MVP，能拦截列出的常见机械表达，不能保证识别所有语义上的人格偏离。后续可增加小模型语义评分、受控语料评估和人工试听；不要靠无界重生成提高拟人感。

{{CONSISTENCY}}

# 4. SQLite 表结构修改 SQL

旧表不删列、不覆写原文。新增以 `scope_id + actor_id` 为键的行为表，比把一组“当前意图”列加到全局人格表更适合多人环境。`expression_history.plan_json` 保留每轮完整计划，`delivered_text` 只保存实际确认的文本。

| 数据 | 持久化位置 |
| --- | --- |
| intent、intent_confidence | behavior_state |
| expression_budget、show_state_level | behavior_state |
| reply_mode、delay_seconds、split_count、truncate_style | behavior_state |
| persona_drift_score、last_consistency_check | behavior_state |
| 每轮计划、sent/partial/unsent/silent、实际文本 | expression_history |
| 每个状态主题上次实际说出的时间 | expression_exposure |

迁移文件 [docs/migrate_v3_to_v4.sql](docs/migrate_v3_to_v4.sql) 可用于 v3 数据库。正常启动时 `Database` 自动幂等执行同样的建表，旧版迁移也不会再将 `user_version` 重置为 3。完整最终结构另见 [docs/schema_v4.sql](docs/schema_v4.sql)。只读命令与脑图仍使用 `mode=ro + query_only`，不调用这些写入路径。

{{SQL}}

# 5. 测试用例与验证结果

{{RESULT}}

新增测试覆盖：连续十轮故意诱导模型报告“累、想念、好奇”仍会受到节制；不想说话时可短回或真正沉默；兴奋分享可分为 2–3 条并逐条等待；群聊潜水无输出、冒泡短句；部分发送失败、取消、重复确认不会虚构已说内容；人格漂移只修复一次；表达历史跨重启保留。

语音测试使用本地 aiohttp 服务验证 multipart 克隆上传、私有音色、状态查询、复用 ID、JSON TTS 请求、模型头、WAV 文件、401/429、错误音频及失败状态；Edge 转码链路使用替身。测试不上传用户录音、不使用真实 API Key、不触发实际云端克隆。

原有成长、记忆、隔离、社交限流、只读命令、端口占用、Three.js 与 Chromium WebGL 测试一并回归。没有真实 AstrBot 平台账号或模型服务的端到端运行结果；声音自然度也尚未试听。

```bash
python -m pip install -r requirements.txt pytest playwright
python -m playwright install chromium
python -m pytest -q --junitxml=docs/test-results-v4.xml
python tools/build_delivery_v4.py
```

Windows 受限运行环境中测试曾无输出；实际结果来自允许本机测试和 HTTP 回环的环境，使用 Python 3.12。插件代码另做 Python 3.10 语法解析检查；AstrBot v4.29.4 宿主自身要求 Python 3.12。

{{TESTS}}
