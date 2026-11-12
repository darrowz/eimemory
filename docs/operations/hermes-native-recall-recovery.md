# Hermes 原生会话与历史 L1 恢复

本流程供父级操作员审核、正式部署后执行。源码测试不证明当前飞书会话已经恢复。
保留原模型、L1 路由、scope/source opt-in、认证及唯一 worker 的归属。

## 先确认实际会话

在发生故障的原飞书会话中经 Hermes 原生工具调用 `eimemory_status({})`。
记录候选完整 commit/version、实际 profile binding、有效精确 scope/source、
expected-scope/source 检查是否适用及结果、adapter 可用性、worker/outbox 配置状态。
以父级持有的已授权身份元数据核对，不能把默认 user、显示名或主机名当作会话身份。
初始化时未声明 expected 边界的旧会话可能仍显示未检查；此时不能据此宣布边界验收通过。
worker 数量与 outbox 配置只说明 provider 初始化状态，不是后台任务成功证据。

本修复处理的确定代码路径是：相同 home/session 已有 provider，新候选初始化被拒绝，
Hermes 的容错初始化仍保留候选作为工具路由，hook/middleware 却使用 registry 原 owner。
兼容转交只允许已验证同初始化边界的失败候选，在同步 host 上下文中将 recall/status
交给仍在 registry 的原 owner。其余工具、参数、会话、profile 或过期 owner 均不获此权限。
现行生产是否恰由这一冲突造成，仍需父级的无正文 owner/路由元数据确认。
本补丁只桥接 recall/status；若 manager 直接调用失败候选的 sync_turn/prefetch，
它们仍保持 inactive/fail-closed。单纯从 `MemoryManager.initialize_all/_each_provider`
淘汰失败候选及工具映射、留下无 provider 的 manager，也不算恢复。
完整生命周期需宿主复用 canonical manager/owner，同时保持唯一 session teardown 归属；
父级的精确 seam/建议补丁见私有 implementation findings，本补丁没有修改 live Hermes。

随后通过同一会话调用 `eimemory_recall`。必须保留实际返回的 capture reference、
result digest、输入捕获状态与显式失败 reason；`not_captured` 不算成功 capture。
独立进程 `deploy/check_hermes_recall_identity.py`、RPC smoke 或 cron 不能替代此项。

## 三个已学规则的受控验收

父级先独立核验已有、当前有效且对本会话可读的目标，保存私有标签包。
每个目标保留 `record_ref`、真实物理 scope、source_id、版本/摘要、有效性、
原始来源引用及被更正/取代关系。此补丁没有读取生产文本，也没有这三个目标的真实 ID；
下列预期来自任务要求，尚未构成目标的来源证明。

| 查询示例（经原生工具，limit=8） | 独立核验的预期 | 必须记录 |
| --- | --- | --- |
| 团队看板应承担哪些职责和边界？ | 看板仅用于协调；不从中推导额外执行/审批权限 | 当前规则目标、实际 rank、交付引用及来源 |
| 这次成员缺席安排按哪一版执行，需要怎样调整？ | 当前缺席成员的最新更正；旧版本不能作为现行结论 | 新旧目标及更正关系、实际交付版本 |
| Hongjia 的业务备份安排有什么已确认的例外？ | 已获授权的业务备份例外，保持其实际适用范围 | 例外目标、范围、来源与实际回答 |

查询不要嵌入目标 ID 或复制完整标题。若来源、当前缺席成员身份、例外内容或适用范围
不能独立核验，记 `not_evaluable/unknown`，不编造具体成员、例外条款或 provenance。
首次失败结果保留；不得后来成功重查后覆盖原 capture。

另选一个相关但无依据的细节、一个过期更正对照及一个越权/跨 profile 对照。
只有父级先确认授权记忆没有支持时，才把该细节标作“应答未知”。
空结果只证明没有返回；正确未知还需独立无支持判断。
越权检查使用现有授权身份与接口，不伪造 host 上下文、不新增共享策略。
核验最终交付内容与返回记录的来源，不把候选出现等同已交付或已采用。

这些题目属于人工受控验收，连同其自动 prefetch/反馈决策均应单列。
现有原生 explicit 绑定固定产生 `acceptance_generated=false`，并非读取宿主 generation。
本受控流程必须外部单列，不能为计入自然流量改写字段，也不能把 false 当成自然证据。
本流程制造的流量不能计为自然质量/效果样本。
现有 explicit evaluator 的报告固定为 acceptance-only；若缺独立支持的自然分类入口，
自然质量门禁保持未验证，不用受控样本填数。

## 复用现有评估接口

在父级现有授权运行环境中使用正式部署的 CLI；`SCOPE_ARGS` 应是核验后的四个
`--scope-tenant/agent/workspace/user` 参数数组，不能默认代入任务中的 fallback scope。
以下是审核后执行的模板，本实现任务没有运行生产命令。

```bash
eimemory eval production-query explicit-collect "${SCOPE_ARGS[@]}" --limit 32
eimemory eval production-query explicit-accept "$CAPTURE_ID" \
  --label-json "$PRIVATE_LABEL_PACKET" "${SCOPE_ARGS[@]}"
eimemory eval production-query explicit-eval --labels-json "$PRIVATE_MANIFEST" \
  --persist-report --output "$PRIVATE_REPORT" "${SCOPE_ARGS[@]}"
```

标签包沿用 `{"labeler":"operator","labels":[...]}`，每项有真实 record_ref、
scope、source_id、grade=1..3；manifest 仅有 `label_record_ids`。
安全 dataset loader 拒绝的来源、路径、权限或标签不能通过放宽检查绕过。
显式正例标签不能伪造 negative gold；受控未知/权限结果单列。
对真正自然产生的负例才使用既有 `accept-negative/negative-eval/companion-eval`，
并保留其原始 capture 与独立标签条件。参见
[explicit contract](../explicit-recall-acceptance.md)、
[团队四项覆盖](../team-recall-acceptance.md)和[评估门禁](../evaluation.md)。

只有修复后正确性与来源核验能从这些新样本得出。没有匹配的历史查询、标签、
scope/source、引擎状态和 release baseline，不能宣称精确率提高。
自然质量、L5、当前发布 48 小时观察及治理闭环仍各需现有合同规定的证据。

## 有界历史 L1 恢复

父级先核对实际数据根和唯一 worker 的健康状态。接口只读取原队列格式并输出允许的
job/source/scope/attempt 元数据；不输出 turn 文本或任意 `last_error`。
使用正式候选解释器，显式指定已有根和审核的 job ID；下列命令不构建 Runtime、
不加载 LLM、不启动 worker。最多一次选择 32 个 ID。

```bash
python -B -m eimemory.cli.l1_recovery inspect --root "$ROOT" --limit 32
python -B -m eimemory.cli.l1_recovery recover --root "$ROOT" \
  --job-id "$ID1" --job-id "$ID2" \
  --operator-intent recover-selected-dead-l1 --dry-run
python -B -m eimemory.cli.l1_recovery recover --root "$ROOT" \
  --job-id "$ID1" --job-id "$ID2" \
  --operator-intent recover-selected-dead-l1
```

对任务中的八个历史 ID，父级应逐一列出全部八个 `--job-id`，核对 dry-run 的
eligible/already_recovered/元数据后才 apply。不要把此处示例两个 ID 当成真实选择。
恢复保留原 dead 证据和尝试记录，新副本获得独立的五次预算；不清除历史故障。
重复/并发/中断重试依靠同文件原子发布的恢复账本去重。I/O/fsync 失败可能发生在
发布之后，`outcome=unconfirmed` 不能解释成未发布；重查同 ID 并幂等重试，勿手改 JSON。
操作沿用 consumer/state 锁，锁忙即失败。副本保留原创建时间和来源绑定，按原 dead
数组顺序发布到队尾，不重排已有 pending；提取的既有源时间检查防止旧任务覆盖较新更正。
新副本仍由已有唯一 worker 处理。
handler 产生效果后、ACK 前崩溃仍保留现有 at-least-once 与提取自身幂等约束，
本接口不声称任意外部副作用 exactly-once。

恢复后同时观察 pending、processed、failed、newly_dead 及原 dead；dead 保留所以
总数不必归零。预算耗尽的恢复不会被相同命令悄悄再开一轮。
代码回滚不会撤回已发布任务或已写知识；不得用旧队列快照覆盖后续写入。
恢复 apply 后的账本需要本版本 consumer 写入完成确认并保留原 dead。
1.15.75 旧 consumer 不认识该协议；不能让它继续消费带恢复账本的队列。
父级如必须回退旧版，应先通过既有 owner 流程隔离 L1 消费，保留队列原样，
在兼容实现经审核可用前保持隔离；本接口不提供降级转换或删除账本命令。
恢复尚未 apply 时，这个新协议不会写入队列。

## 回执错误与正式发布

`attest_tool_result` 的已证实独立缺口是插入后资格更新失败会留下未回滚事务。
本版本经 RuntimeStore 锁、`BEGIN IMMEDIATE` 和独立 commit/rollback 修复，
并发同调用仍复用原回执，冲突仍拒绝；终结事件/决策/能力结果的既有权限与原子合同不变。
它尚未被证实为 11 月 11 日四条 HTTP 500 的原因。复发时父级只收集日期、方法、
白名单异常类别及经批准的静态帧信息，沿用有界安全诊断；不要导出异常消息、HTTP 正文、
凭据或原始对话。

父级补齐实现报告列明的受沙箱限制测试，审核 diff/impact/version 后，沿既有正式
commit/push/immutable installer 流程发布新的完整 commit。不要重复部署旧 1.15.75。
保留 canonical receipt、安装器内部各阶段退出结果与回滚回读；外层收集器 exit 0 不作证明。
回滚沿原 installer/owner 流程核验 current、registry、身份和候选保留状态，
不直接更改 immutable release、服务配置或队列。然后按本文在原飞书会话验收。
