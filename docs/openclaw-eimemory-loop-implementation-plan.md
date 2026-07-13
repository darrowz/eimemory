# OpenClaw / eimemory 闭环落地计划

日期：2026-07-04  
目标：把鸿途从“会执行任务的工具代理”升级为“任务可追踪、结果可验证、经验可沉淀、能力可自我改进的闭环系统”。

## 1. 核心结论

下一步不是继续堆 prompt，也不是单纯换更强模型，而是补完整的 loop：

```text
Observe -> Plan -> Act -> Verify -> Reflect -> Memorize -> Improve -> Report
```

当前最大问题不是“不能干活”，而是：

- 活干了，但没有统一账本记录状态。
- 命令被中断后，不知道执行到哪一步。
- 后台任务完成后，没有强制验收和回报。
- MEMORY / 历史上下文过重，影响判断质量。
- 自主进化已经打开，但缺少更清晰的隔离实验区和 promotion 证据链。

本计划的目标是把这些问题变成工程机制，而不是靠我每次临场记住。

## 2. 设计原则

1. 每个任务必须有状态，不允许只存在聊天上下文里。
2. 每个后台动作必须有 owner、日志、下次检查时间和完成钩子。
3. 每个结果必须有 evidence，不接受“应该好了”。
4. 每次失败必须沉淀成 lesson，重复失败必须变成 rule / test / skill / code patch。
5. 自我修改必须隔离执行，主线只负责调度、验收、promote / rollback。
6. 记忆必须检索式注入，不再把 MEMORY.md 全量塞进每一轮。
7. 外发、花钱、删数据、权限变更仍保留显式安全边界。

## 3. 总体架构

```text
User / Trigger
  |
  v
Task Intake
  |
  v
Task Ledger <-------------------------------+
  |                                         |
  v                                         |
Planner -> Executor -> Verifier -> Reporter |
  |          |           |                  |
  |          v           v                  |
  |      Tool Log     Evidence              |
  |                                         |
  v                                         |
Reflector -> Lesson Store -> Rule/Skill/Patch Candidate
  |                                         |
  v                                         |
Replay / Safety / Health Gate               |
  |                                         |
  v                                         |
Canary / Active / Rollback -----------------+
```

## 4. 模块一：Task Ledger

### 4.1 目标

解决“活干了，但不知道干完没”的问题。

所有任务，不管是用户显式指令、heartbeat、cron、后台部署、subagent、自动进化，都必须落账。

### 4.2 数据结构

建议先用 SQLite 或 JSONL，后续接 eimemory record。

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| task_id | string | 全局唯一 ID，如 `task_20260704_1450_context_slim` |
| parent_task_id | string | 父任务，可空 |
| source | enum | user / heartbeat / cron / self_improve / subagent |
| title | string | 短标题 |
| objective | text | 目标 |
| status | enum | planned / running / waiting / verifying / done / blocked / failed / rolled_back |
| owner | enum | main / subagent / background / cron / systemd |
| risk_level | enum | low / medium / high |
| started_at | datetime | 开始时间 |
| updated_at | datetime | 更新时间 |
| next_check_at | datetime | 下次检查时间 |
| deadline_at | datetime | 预期截止，可空 |
| last_action | text | 最近做了什么 |
| current_step | string | 当前步骤 |
| evidence_refs | json | 日志、commit、测试、health URL、文件路径 |
| result_summary | text | 结果摘要 |
| blocker | text | 阻塞原因 |
| report_policy | enum | always / on_done / on_blocked / silent |
| report_target | string | 飞书 direct / group / none |

### 4.3 状态机

```text
planned
  -> running
  -> waiting
  -> verifying
  -> done

running
  -> blocked
  -> failed
  -> rolled_back

blocked
  -> running
  -> failed

failed
  -> rolled_back
  -> lesson_recorded
```

### 4.4 强制规则

- 启动后台任务前，必须创建 `task_id`。
- 每次启动 `nohup` / systemd / cron / subagent，必须写 `next_check_at`。
- 后台任务 5 分钟内必须至少检查一次。
- 状态超过 20 分钟不更新，标记 `stale` 并提醒主线复核。
- 用户问“怎么样了”，优先查 Task Ledger，不靠记忆回答。

## 5. 模块二：Action Log

### 5.1 目标

记录每一步实际动作，形成证据链。

### 5.2 数据结构

| 字段 | 类型 | 说明 |
|---|---|---|
| action_id | string | 动作 ID |
| task_id | string | 关联任务 |
| action_type | enum | shell / tool / message / edit / deploy / test / verify |
| command_or_tool | text | 命令或工具名 |
| started_at | datetime | 开始 |
| finished_at | datetime | 结束 |
| exit_code | int | 退出码 |
| stdout_ref | string | 输出摘要或日志路径 |
| stderr_ref | string | 错误摘要或日志路径 |
| result | enum | success / failed / timeout / aborted |
| retry_of | string | 重试来源 |

### 5.3 必须记录的动作

- systemd restart / status
- deploy
- git commit / diff / status
- test / health check
- background job dispatch
- subagent spawn / completion
- message send
- file patch

## 6. 模块三：Verifier

### 6.1 目标

每个任务完成前必须独立验收，不允许“执行完命令就算完成”。

### 6.2 验证类型

| 类型 | 验证方式 |
|---|---|
| 代码修改 | `compile` / unit tests / targeted tests / git diff review |
| 服务重启 | systemd active + readyz/health endpoint |
| 部署 | version/commit 对齐 + health + smoke test |
| 文件生成 | 文件存在 + 内容结构检查 |
| 飞书消息 | message tool receipt |
| 记忆写入 | 文件/DB record 可读回 |
| 自动进化 | replay + safety + health + rollback plan |

### 6.3 验收记录

| 字段 | 类型 | 说明 |
|---|---|---|
| verification_id | string | 验证 ID |
| task_id | string | 任务 ID |
| verifier | string | verifier 名称 |
| checks | json | 检查项 |
| passed | bool | 是否通过 |
| evidence_refs | json | 证据 |
| failure_reason | text | 失败原因 |
| next_action | enum | report_done / retry / replan / rollback / ask_user |

## 7. 模块四：Reporter

### 7.1 目标

解决“做完没回报”的问题。

### 7.2 报告策略

| report_policy | 行为 |
|---|---|
| always | 每个关键阶段都回报 |
| on_done | 完成才回报 |
| on_blocked | 阻塞才回报 |
| silent | 静默，只落账 |

### 7.3 默认策略

| 任务类型 | 默认策略 |
|---|---|
| 用户显式请求 | always 或 on_done |
| UUMit 日常 | silent，除费用/授权/重大风险 |
| heartbeat | on_blocked |
| 自动进化 | on_done + on_rollback |
| 部署/重启 | always |
| 高风险操作 | always |

### 7.4 报告模板

```text
任务：<title>
状态：done / blocked / failed / rolled_back
结果：<一句话结论>
证据：<commit/test/health/log>
下一步：<如果有>
```

## 8. 模块五：Memory Loop

### 8.1 目标

让记忆提升判断，而不是拖慢判断。

### 8.2 改造点

1. 禁止 Codex turn 默认全量注入 `MEMORY.md`。
2. `MEMORY.md` 只作为索引和稳定规则来源。
3. 任务开始时按 query 检索：
   - 用户偏好
   - 安全规则
   - 相关历史失败
   - 相关工具路径
   - 相关业务 playbook
4. 检索结果必须带 provenance。
5. 检索不到时不瞎编，改查文件/工具/网页。

### 8.3 记忆类型分层

| 类型 | 用途 |
|---|---|
| policy | 必须遵守的规则 |
| preference | 用户偏好 |
| tool_fact | 工具路径、服务地址 |
| lesson | 失败和纠错沉淀 |
| playbook | 可复用流程 |
| raw_event | 原始事件 |
| evidence | 证据 |

### 8.4 注入预算

建议：

- 用户偏好：最多 5 条。
- 工具事实：最多 8 条。
- 历史失败：最多 5 条。
- playbook：只注入目录和相关片段。
- 单轮记忆注入上限：3000-6000 tokens。

## 9. 模块六：Reflect / Lesson Loop

### 9.1 目标

每次错误都要变成可复用改进，不重复摔同一个坑。

### 9.2 触发条件

- 用户纠正。
- 工具失败。
- 命令 aborted / timeout。
- 后台任务失联。
- 同一任务重试超过 2 次。
- 验证失败。
- 回答质量明显低于网页版 GPT。

### 9.3 Lesson 数据结构

| 字段 | 类型 | 说明 |
|---|---|---|
| lesson_id | string | lesson ID |
| source_task_id | string | 来源任务 |
| trigger | enum | user_correction / tool_failure / timeout / verification_failed / quality_gap |
| symptom | text | 表面问题 |
| root_cause | text | 根因 |
| fix | text | 修复方式 |
| prevention | text | 下次如何避免 |
| confidence | float | 置信度 |
| promote_candidate | bool | 是否可晋级 |
| target | enum | rule / skill / code / test / memory |

### 9.4 晋级规则

| 条件 | 动作 |
|---|---|
| 同类 lesson 出现 2 次 | 生成 rule candidate |
| 同类 lesson 出现 3 次 | 生成 skill/code patch candidate |
| 高风险事故 1 次 | 直接生成 safety rule candidate |
| 用户明确纠正 | 直接写 memory/daily，必要时提炼进 MEMORY |

## 10. 模块七：Self-Improvement Lab

### 10.1 目标

让自主进化可控、安全、可回滚。

### 10.2 Prime / Lab 模式

```text
Prime = 当前稳定主代理
Lab = 隔离自改 worker
```

Prime 职责：

- 选题。
- 生成任务合同。
- 分配 Lab。
- 验证结果。
- promote / rollback。
- 汇报。

Lab 职责：

- 只在隔离分支或临时目录工作。
- 读任务合同。
- 改代码。
- 跑测试。
- 输出 patch / commit / report。
- 不能自行 promote 到生产。

### 10.3 Lab 任务合同

```markdown
# Lab Task Contract

## Objective

## Scope

## Files Allowed

## Files Forbidden

## Verification

## Rollback Plan

## Report Required
```

### 10.4 Promotion Gate

| gate | 必须条件 |
|---|---|
| syntax | 编译/语法通过 |
| tests | 相关测试通过 |
| replay | 历史 replay 不回退 |
| safety | 不触碰禁区 |
| health | 服务健康 |
| diff review | diff 范围合理 |
| rollback | 有回滚路径 |

### 10.5 自动提权规则

保留当前原则：不需要人工批准。

```text
candidate -> canary:
  replay pass
  safety pass
  scope bounded

canary -> active:
  observation_count >= 3
  failure_rate <= 0.05
  no safety incident

rollback/quarantine:
  failure_rate >= 0.2
  safety incident
  health regression
```

## 11. 模块八：Replan Loop

### 11.1 目标

防止卡死在同一个工具/命令上。

### 11.2 规则

- 同一命令失败 1 次：检查参数和环境。
- 同一命令失败 2 次：换工具链。
- 同一任务 5 分钟无进展：写 ledger 并回报状态。
- 同一任务 20 分钟未完成：给出错误、现状、3 个路径。
- 后台任务无日志：标记 `stale`，重新探测进程。
- 重启自身服务：必须使用 detached job + verify log。

### 11.3 路径选择

```text
primary path failed
  -> local script
  -> alternate CLI
  -> direct API
  -> browser/tool automation
  -> web search for workaround
  -> user-facing true blocker
```

## 12. 模块九：Context Slimming

### 12.1 目标

让 Codex 更接近网页版 GPT 的判断质量。

### 12.2 立即项

- Codex turn 禁止粘贴完整 MEMORY.md。
- `conversation_context` 限制到最近关键摘要。
- heartbeat 独立 session，不污染 direct session。
- tool schemas 延迟加载，只在需要时 tool_search。
- UUMit / heartbeat / cron 默认 lightweight context。

### 12.3 验收指标

| 指标 | 目标 |
|---|---|
| 普通 direct turn context | < 30k tokens |
| 简单问答 context | < 15k tokens |
| heartbeat context | < 10k tokens |
| MEMORY 注入 | 0 全量，只引用/检索 |
| compaction failure | 0 |

## 13. 模块十：Metrics Dashboard

### 13.1 必须指标

| 指标 | 说明 |
|---|---|
| task_done_rate | 任务完成率 |
| stale_task_count | 失联任务数 |
| mean_time_to_report | 完成到回报平均时间 |
| verification_pass_rate | 验证通过率 |
| retry_count_by_tool | 工具重试次数 |
| user_correction_rate | 用户纠错率 |
| repeated_failure_rate | 重复失败率 |
| context_tokens_avg | 平均上下文 token |
| memory_recall_precision | 记忆召回准确率 |
| autonomous_patch_success_rate | 自动 patch 成功率 |
| rollback_count | 回滚次数 |

### 13.2 判断标准

第一阶段目标：

- stale task = 0。
- 用户问“怎么样了”时 100% 能查账本回答。
- 部署/重启类任务 100% 有 health evidence。
- 重复失败率下降。
- context tokens 明显下降。

## 14. 数据落点建议

### 14.1 MVP 落点

```text
/var/lib/openclaw/task-ledger/tasks.jsonl
/var/lib/openclaw/task-ledger/actions.jsonl
/var/lib/openclaw/task-ledger/verifications.jsonl
/var/lib/openclaw/task-ledger/lessons.jsonl
/var/lib/openclaw/task-ledger/reports.jsonl
```

### 14.2 后续落点

- eimemory record store。
- SQLite task DB。
- OpenClaw dashboard。
- Feishu report summary。

## 15. CLI 设计

```bash
openclaw task create --title "..." --objective "..." --owner main
openclaw task update <task_id> --status running --last-action "..."
openclaw task action <task_id> --type shell --cmd "..." --exit-code 0
openclaw task verify <task_id> --check readyz --passed --evidence "..."
openclaw task done <task_id> --summary "..."
openclaw task list --status running
openclaw task stale --older-than 5m
openclaw task report <task_id>
```

## 16. 系统钩子

### 16.1 pre-action hook

动作执行前：

- 确保 task_id 存在。
- 记录 action started。
- 标记 task running。

### 16.2 post-action hook

动作执行后：

- 记录 exit code / output summary。
- 如果失败，触发 replan。
- 如果成功，进入 verify。

### 16.3 background-dispatch hook

后台任务启动后：

- 记录 PID / log path。
- 设置 next_check_at = now + 5m。
- 注册 watcher。

### 16.4 completion hook

任务完成后：

- verify evidence。
- 更新 task done。
- 按 report_policy 汇报。
- 写 lesson / outcome。

## 17. 分阶段落地计划

### Phase 0：止血

目标：解决当前“执行中断、无回报、上下文过重”。

任务：

1. 完成 Codex MEMORY 全量注入禁用。
2. gateway detached restart 标准化。
3. 写一个最小 `task-ledger` JSONL helper。
4. 所有后台命令改成 ledger + verify log。

验收：

- 新 turn 不再全量粘贴 MEMORY.md。
- gateway restart 能独立完成并写 `/tmp/openclaw-gateway-restart-verify.log`。
- 用户问进度能查 task ledger。

### Phase 1：Task Ledger MVP

目标：所有显式任务可追踪。

任务：

1. 实现 `openclaw-task-ledger` CLI。
2. 支持 create/update/action/verify/done/list。
3. 包装常用后台任务模板。
4. 飞书 direct 用户请求自动生成 task。

验收：

- 100% 用户显式任务有 task_id。
- 后台任务 5 分钟内有检查记录。
- done 任务都有 evidence。

### Phase 2：Verifier / Reporter

目标：所有完成都有证据，完成自动回报。

任务：

1. 增加 verifier registry。
2. 支持 service / file / git / test / message verifier。
3. 完成后自动 report。
4. blocked 自动给状态和下一步。

验收：

- 服务类任务都有 health。
- 代码类任务都有 diff/test/commit 状态。
- 用户不再追问“做完没”。

### Phase 3：Replan Loop

目标：失败自动换路径。

任务：

1. 失败分类：tool_missing / auth / timeout / network / syntax / permission。
2. 每类失败配置 fallback。
3. 同类失败超过阈值生成 lesson。
4. 20 分钟硬阈自动报告。

验收：

- 不再长时间卡同一命令。
- 重试都有原因。
- 失败后能给 3 个可选路径。

### Phase 4：Self-Improvement Lab

目标：自动进化隔离执行。

任务：

1. 实现 Lab worktree / branch。
2. Prime 生成 task contract。
3. Lab 输出 patch / report。
4. Prime 跑 gate 后 promote / rollback。

验收：

- 自动 patch 不直接污染主分支。
- 失败实验可丢弃。
- promote 有完整证据链。

### Phase 5：Metrics Dashboard

目标：量化鸿途是否变聪明。

任务：

1. 汇总 task/action/verification/lesson。
2. 输出 daily dashboard。
3. 接入 eimemory capability ledger。
4. 生成 weekly improvement report。

验收：

- 可看到 correction rate / stale task / context tokens 趋势。
- 自动进化不靠感觉，靠指标。

## 18. 优先级清单

### P0

- MEMORY 全量注入彻底禁用。
- Task Ledger MVP。
- 后台任务 5 分钟检查。
- completion report hook。

### P1

- Verifier registry。
- Replan failure taxonomy。
- Lesson Store。
- Task status 飞书查询。

### P2

- Self-Improvement Lab。
- Metrics dashboard。
- eimemory lesson promotion。
- skill 自动生成和 canary。

## 19. 验收用例

### 用例 1：gateway restart

流程：

1. create task。
2. dispatch detached restart。
3. 记录 PID/log。
4. 5 秒后 verify readyz。
5. done + report。

通过条件：

- 即使当前会话被 restart 打断，也能从 ledger 查到结果。

### 用例 2：代码 patch

流程：

1. create task。
2. apply patch。
3. node/python compile。
4. targeted tests。
5. git diff summary。
6. commit or explain no commit。
7. report。

通过条件：

- 每一步有 action log。
- 失败自动 rollback 或 blocked。

### 用例 3：web research

流程：

1. create task。
2. search。
3. fetch sources。
4. synthesize。
5. cite evidence。
6. report。
7. optionally lesson if tools failed。

通过条件：

- 搜索失败会换工具链。
- 最终报告不把第一条工具失败当结论。

### 用例 4：自动进化

流程：

1. detect weakness。
2. create candidate。
3. Lab branch patch。
4. replay/test/safety。
5. canary。
6. observe。
7. active or rollback。

通过条件：

- 主分支始终可回滚。
- promotion 有 evidence。

## 20. 风险和边界

| 风险 | 控制 |
|---|---|
| 自我修改改坏运行态 | Lab 隔离 + promote gate + rollback |
| 任务账本膨胀 | JSONL rotate + daily summary |
| 记忆召回错误 | provenance + confidence + evaluator |
| 自动回报刷屏 | report_policy |
| 高风险动作越权 | safety boundary |
| 指标好看但实际没用 | 用户纠错率和真实验收优先 |

## 21. 最小可行实现

先不要一口吃成大平台，MVP 只做 5 件事：

1. `task-ledger` JSONL。
2. `task_id` + status + evidence。
3. 后台任务 detached 模板。
4. verifier helper。
5. report hook。

MVP 完成后，先覆盖这三类任务：

- gateway / systemd 操作。
- 代码 patch / deploy。
- 长任务 / subagent。

## 22. 最终形态

最终鸿途应该具备：

- 用户问“进度怎样”，能查账本秒答。
- 后台任务不失联。
- 做完自动验收和回报。
- 失败自动换路径。
- 重复错误自动变规则。
- 可控自我改进。
- 上下文越来越轻，记忆越来越准。
- 能慢慢向 Jarvis 型能力靠近，但保留安全边界。

一句话目标：

```text
不靠“我记得”，靠系统闭环。
```

## 23. 生产闭环补充项（2026-07-04）

这一节补齐长期运行最容易漏掉的边角：租约、幂等、恢复、配置漂移、schema migration、E2E smoke。目标不是再增加概念，而是让闭环在 gateway 重启、飞书重复投递、后台任务失联、配置漂移时仍然能自我解释和自我恢复。

### 23.1 Run Lease / Watchdog

每个 `running / waiting / verifying` 任务都必须有运行租约。租约不是进程是否存在，而是“任务是否仍有可观察进展”。

新增字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| lease_expires_at | datetime/epoch | 超过该时间未续租则 stale |
| heartbeat_at | datetime | 最近一次续租时间 |
| heartbeat_source | string | main / watcher / subagent / cron / systemd |
| last_progress_hash | string | 最近进展摘要 hash，用来识别“活着但没进展” |

规则：

- 后台任务启动后必须立刻写 heartbeat。
- 长任务每 5 分钟内至少续租一次。
- lease 过期：标记 `stale`，先探测进程/日志/health，再决定 resume / replan / report。
- 同一 progress_hash 连续 3 次不变：标记 `no_progress`，进入 replan。

### 23.2 Idempotency / Dedupe

所有来自飞书、heartbeat、cron、restart recovery 的任务都必须带幂等键。

建议字段：

| 字段 | 说明 |
|---|---|
| dedupe_key | `source + message_id + objective_hash` |
| idempotency_key | 外部重试、工具调用、消息发送复用 |
| reentrant_policy | ignore / attach / retry / supersede |

规则：

- 同一个 dedupe_key 的 active task 不重复创建，只 attach action。
- 已 terminal 的任务可创建新任务，但必须引用 previous_task_id。
- message send / deploy / restart 这类副作用动作必须使用 idempotency_key。

### 23.3 OpenClaw / eimemory 数据边界

| 系统 | 负责内容 |
|---|---|
| OpenClaw | task ledger、action log、runtime hook、reporter、gateway/task 生命周期 |
| eimemory | lesson、rule、policy、playbook、长期能力画像、promotion 证据 |
| 交界记录 | outcome_trace、verification_result、lesson_candidate、policy_candidate |

规则：

- OpenClaw 不把所有运行日志塞进长期记忆，只把可复用 lesson / policy candidate 交给 eimemory。
- eimemory 不直接驱动生产副作用动作，只返回 policy / suggestion / candidate。
- runtime audit 默认不进入 prompt 注入，只进入 diagnostic 查询。

### 23.4 Recovery / Resume

Gateway 或 Codex 主线重启后必须执行恢复扫描：

```text
on_startup:
  scan running/waiting/verifying tasks
  check lease_expires_at
  verify process/log/service
  resume watcher if safe
  suppress stale aborted session recovery
  report only if user-visible task changed state
```

恢复优先级：

1. 已有 health/evidence 表明完成：补 verifier + done + report。
2. 进程仍在且 lease 未过期：恢复 watcher。
3. lease 过期但日志有进展：续租并继续。
4. lease 过期且无进展：replan 或 rollback。
5. stale aborted session 反复失败：归档 session index，保留 transcript，不再自动 recovery。

### 23.5 Config Drift / Runtime Drift Verifier

必须有一键漂移检查，覆盖这次 honxin 遇到的真实问题：

| 检查 | 失败码 |
|---|---|
| `gateway.auth.token` 与 `gateway.remote.token` 不一致 | gateway_token_mismatch |
| `gateway.remote.url` 指向不可达 loopback | gateway_remote_loopback |
| systemd drop-in env 被空格截断 | systemd_env_truncated |
| eimemory source/release/current commit 不一致 | eimemory_commit_drift |
| plugin enabled/config/hook policy 不一致 | plugin_policy_drift |
| gateway / rpc health 失败 | gateway_health_failed / eimemory_health_failed |
| restart recovery 旧 session 反复失败 | stale_session_recovery |

这些 drift 不一定直接阻断普通任务，但必须阻断 deployment done 和 L2 promotion。

### 23.6 Schema Version / Migration

所有 JSONL / SQLite 记录必须带：

```text
schema_version
writer_version
migration_status
```

规则：

- reader 必须兼容当前 schema 的前一个 minor 版本。
- dashboard 只统计可解析记录；不可解析记录进入 migration warning。
- schema 升级必须带 smoke migration case。

### 23.7 One-command E2E Smoke

新增统一验收命令：

```bash
python3 scripts/openclaw_loop.py smoke
python3 scripts/openclaw_loop.py doctor
python3 scripts/openclaw_loop.py stale
```

`smoke` 必须证明：

- 创建 task。
- 写 heartbeat lease。
- 写 action。
- 跑 config drift verifier。
- 写 verification。
- 写 report。
- task 进入 done 或 blocked，不能悬空。

输出必须包含：`task_id / action_count / verification_id / report_count / drift codes`。

### 23.8 Failure Taxonomy 扩展

新增 OpenClaw / eimemory 专属 failure kind：

```text
auth_scope_missing
gateway_token_mismatch
systemd_env_truncated
hook_timeout
hook_duplicate_registration
stale_session_recovery
memory_pollution
recall_miss
report_delivery_failed
verification_insufficient
context_over_budget
```

这些 failure kind 必须进入 lesson loop，重复出现时升级为 rule / test / patch candidate。

## 24. 已落地 MVP：openclaw_loop.py

已在 workspace 增加最小闭环脚本：

```bash
scripts/openclaw_loop.py
scripts/test_openclaw_loop.py
```

能力：

- Task Ledger：`create / update / list / done`。
- Run Lease：`heartbeat` 写 `lease_expires_at / heartbeat_at / last_progress_hash`。
- Action Log：`action` 写动作证据。
- Verifier：`verify` 写验收结果。
- Reporter：`done` 自动写 report。
- Config Drift：`doctor` 检查 gateway token/url、eimemory/gateway health。
- Watchdog：`watch` 每轮检查 config drift + stale lease，自动写 `watch.jsonl`；发现问题自动建 blocked task、写 action/verification/report。
- E2E Smoke：`smoke` 一键跑通 task -> heartbeat -> action -> verify -> report。

落点：

```text
/var/lib/openclaw/task-ledger/    # 可写时优先
~/.openclaw/task-ledger/          # 无权限时自动 fallback
```

### 24.1 当前验收命令

```bash
python3 -m unittest scripts/test_openclaw_loop.py
python3 scripts/openclaw_loop.py doctor
python3 scripts/openclaw_loop.py smoke
python3 scripts/openclaw_loop.py watch
systemctl --user status openclaw-loop-watch.timer --no-pager
```

### 24.2 后续强制接入点

下一步必须把以下动作接入 `openclaw_loop.py`，否则不算完整闭环：

1. 飞书用户请求进入 agent 前自动 `create task`。
2. 后台命令 / subagent / cron dispatch 后自动 `heartbeat`。
3. systemd restart / deploy 后必须 `verify`。
4. task terminal 后按 `report_policy` 发送飞书报告。
5. verification failed 自动生成 lesson_candidate 给 eimemory。
6. L2 promotion 必须读取 `doctor/smoke` 结果作为 gate。

2026-07-04 / 1.8.17 状态：以上 6 项已接入到代码路径：

- Feishu bridge 命令在 route 前写入 loop task，route 作为 dispatch，结果写 verification/report。
- OpenClaw `before_prompt_build` 自动创建/续租 task，`task_end/agent_end/session_end` 自动 verify/finish。
- `openclaw_loop.py dispatch` 同时写 action 和 heartbeat，供 cron/subagent/后台命令统一调用。
- `deploy/install_immutable_release.sh` 在 release 切换后调用 `openclaw_loop.py deploy-verify`。
- `record_verification(passed=False)` 自动写 `lesson_candidates.jsonl`。
- L2 promotion gate 强制要求 `closed_loop.doctor.ok` 与 `closed_loop.smoke.ok`。
- `report_policy` 触发 `reports.jsonl`，并在配置 `OPENCLAW_LOOP_FEISHU_WEBHOOK` / `EIMEMORY_FEISHU_WEBHOOK` / outbox 时发送 Feishu 格式报告。

### 24.3 已固化运行态

已在 honxin 安装 systemd user timer：

```text
~/.config/systemd/user/openclaw-loop-watch.service
~/.config/systemd/user/openclaw-loop-watch.timer
```

运行节奏：

- `OnBootSec=2min`：登录/用户服务启动后 2 分钟首次巡检。
- `OnUnitActiveSec=5min`：每 5 分钟执行一次 `openclaw_loop.py watch`。
- `Persistent=true`：错过的 timer 会补跑。

闭环行为：

- 正常时写 `watch.jsonl`，证明系统持续活着。
- 发现 config drift / stale task 时写 blocked task，附带 action、failed verification、report。
- systemd service 直接执行 `watch`；发现 drift 或 stale task 时返回非零，使 systemd 与监控真实反映业务闭环降级，同时在 ledger/report 保留聚合证据。
