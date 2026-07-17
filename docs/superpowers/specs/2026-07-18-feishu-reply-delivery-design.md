# 飞书私聊可靠回复设计

## 目标

飞书私聊收到用户请求后，正常答复必须最终产生唯一、可审计的平台 `messageId`；会话压缩、记忆钩子超时、显式发送失败或 gateway 重启不得造成静默漏回。

## 方案

1. 私聊使用 `messages.visibleReplies: "message_tool"` 关闭非幂等的自动投递；正常 final 由可靠送达队列统一回复原始 inbound。群聊继续使用 `messages.groupChat.visibleReplies: "message_tool"`。
2. Completion Gate 只拦截“宣称任务已完成但仍报告安全可修缺口”的完成型答复，不拦诊断、方案、状态说明或未宣称完成的正常消息。
3. `before_prompt_build` 使用短超时并失败放行。记忆召回失败只降低增强质量，不阻塞原始消息。
4. 独立送达看门狗关联飞书 inbound `message_id` 与 final。生成 final 后 5 秒内，通过飞书原消息回复 API 发送，并把 inbound 哈希派生的稳定短键写入平台 `uuid`。每次发送前分页查询 `parent_id=inbound` 且正文一致的 bot 回复；查询错误只延后，不盲发。3 次快速重试后转 5 分钟持久退避，直到取得 `messageId`。主状态由 gateway 单进程写入，看门狗只写独立回执账本，避免跨进程覆盖。

## 数据流

`Feishu inbound -> OpenClaw dispatch -> optional compaction/eimemory fail-open -> agent final -> durable reply queue -> Feishu idempotent reply -> receipt`

## 约束

- 同一 inbound 消息最多产生一条正常答复；所有重试使用同一个平台 `uuid`。
- 心跳、房间事件和无需回复事件不进入私聊看门狗。
- 不能通过关闭长期记忆压缩换取可靠性。
- gateway 重启必须使用 detached reporter，并取得飞书回执。

## 验收

- 单元测试覆盖 Completion Gate 的完成型拦截与诊断型放行。
- 配置读取确认私聊和群聊均为 `message_tool`、记忆钩子短超时。
- 模拟正常回复、压缩、钩子超时、发送失败、gateway 重启五类场景。
- 每个需要回复的测试消息最终恰有一个平台 `messageId`；无重复、无静默丢失。
