# 飞书私聊可靠回复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除飞书私聊“首条无响应、重复发送后才回复”的链路缺陷。

**Architecture:** 私聊 final 进入可靠送达队列，以 inbound message ID 派生平台幂等键并回复原消息；eimemory 钩子失败放行，Completion Gate 精确识别完成声明。

**Tech Stack:** OpenClaw JSON 配置、Node.js eimemory bridge、Bash/systemd watchdog、pytest。

## Global Constraints

- 代码修改必须先有失败测试。
- eimemory 必须 bump 版本、commit、push、部署不可变 release 并通过 `/health`。
- gateway 重启必须走 detached feedback reporter。

---

### Task 1: 单一可靠送达配置

**Files:**
- Modify: `/home/darrow/.openclaw/openclaw.json`

- [ ] 设置私聊 `messages.visibleReplies=message_tool`，禁止非幂等自动投递。
- [ ] 设置群聊 `messages.groupChat.visibleReplies=message_tool`。
- [ ] 用 `jq` 读取回验配置。

### Task 2: Completion Gate 精确化

**Files:**
- Modify: `integrations/openclaw/eimemory-bridge/index.js`
- Modify: `tests/test_openclaw_completion_gate.py`

- [ ] 增加诊断型消息不应被拦截的失败测试并确认 RED。
- [ ] 增加明确完成声明判定，保留原完成型拦截。
- [ ] 运行专项测试确认 GREEN。

### Task 3: 记忆钩子失败放行

**Files:**
- Modify: `/home/darrow/.openclaw/openclaw.json`
- Test: existing OpenClaw bridge timeout tests

- [ ] 将 `before_prompt_build` 超时缩短到 2000ms。
- [ ] 验证超时后主请求继续运行且 transport failure 被记录。

### Task 4: 送达看门狗

**Files:**
- Create: `eimemory/ops/openclaw_feishu_reply_watchdog.py`
- Create: `deploy/systemd/openclaw-feishu-reply-watchdog.service`
- Create: `tests/test_openclaw_feishu_reply_watchdog.py`

- [ ] 先写无回执、重复回执、无需回复三类失败测试。
- [ ] 实现平台 `uuid` 稳定幂等键和最多三次重试状态机。
- [ ] 安装 user service 并验证运行日志。

### Task 5: 发布闭环

**Files:**
- Modify: `pyproject.toml`
- Modify: `eimemory/version.py`

- [ ] bump 到下一版本并运行专项及全量测试。
- [ ] commit、push、部署不可变 release。
- [ ] 用 detached reporter 重启 gateway。
- [ ] 验证 RPC health、gateway health、配置值和飞书 `messageId` 回执。
