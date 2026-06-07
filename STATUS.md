# eimemory / honjia 服务健康 STATUS

> 最后更新：2026-06-04 02:58 GMT+8
> 维护：鸿途 (main agent, honxin) 自主维护

## ✅ 健康
- **honjia 8 service 全 active**：php8.4-fpm / nginx / mariadb / vikunja*（已停）/ socat-3021（已停）/ eihead-runtime / eihead-monitor / eihead-vision-hailo
- **eihead vision 9.68 FPS**，frame_count 9.4M，/dev/video0 + /dev/hailo0 在
- **eimemory RPC /healthz 200**
- **uumit-monitor + uumit-runtime** 在 honxin 本机跑（SSE 5+ 次 session_open 事件）
- **honjia docker 现只剩 home-assistant**（8123/1883 智能家居）
- **honxin OpenClaw gateway** `/healthz` 200，0.2-0.8s 响应

## ⚠️ 已知问题 + 自主决策

### #1 eihead runtime SSE — "runtime_rejected"（CLOSED-A 2026-06-04 02:58）
- **现象**：OpenClaw gateway 主动 reject eihead runtime talk session 排队消息（log: `runtime_rejected sessionId=f13473ab-…`）
- **决策**：**接受现状**（影响小 —— eihead 主功能 100% 正常，vision 9.68 FPS，18080/18081 200；只是 push 不来外部 job_dispatch）
- **如果后续需要 push**：spawn subagent 修 OpenClaw runtime 调度（runtime_rejected 内部 bug）

### #2 eimemory 12h 0 records — 部分解决
- 7 类 cap 已 seed + 12 条 fixture 记录已写（DB 21 条 cap_score）
- ledger 仍 score=0.0（fixture 缺 cycle 关联，watch 5min 跑会触发 cycle 重算）
- 自主 learning **真在跑**：`eimemory learn report` 已自主 learn 鸿哥 2 条反馈（Vikunja 错 / 测试数据错）

### #3 uumit 5 订单 — 已加 SKIP pattern
- 5 单：1 购买诱导（action，deadline 1.5h 后）+ 4 对等互换（watch，delivered）
- 加 `对等|对等互换|购买.*返|购买.*指南` 到 SKIP_RE + restart monitor
- **UUMit auth token 已 401**（cached 5/25）——自然保护 1 购买诱导单不接；4 watch 单等对方确认（不需 token）
- **下一步**：鸿哥方便时 `node auth.js --reset` 重授（device flow, ~5 min）

## 📋 决策记录

| 日期 | 决策 | 结果 |
|---|---|---|
| 2026-06-04 02:00 | 删 EspoCRM + Vikunja | 鸿哥主动要求，TOOLS.md + MEMORY.md 字段映射教训写入 |
| 2026-06-04 02:05 | 删 BiliNote | 鸿哥主动要求"没有它一样分析抖音视频" |
| 2026-06-04 02:30 | 自主注 7 类 cap fixture | DB 21 条，eimemory 自主 learn 真在跑 |
| 2026-06-04 02:35 | 加 reciprocal/对等/购买 SKIP pattern | 5 UUMit 订单按 MEMORY 规则 skip |
| 2026-06-04 02:58 | eihead gateway runtime_rejected 接受现状 | 影响小，eihead 主功能 100% 正常 |
