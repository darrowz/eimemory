# eimemory 全业务闭环审计 — 2026-10-13

审计基线：**1.14.73 / `e2bbc60e0a893d72d37de55e3b05bf5d910f058f`**。日期按 Asia/Shanghai。结论：**业务闭环审计未通过，确认 16 项问题：7 项 P1、9 项 P2。**

现有流程可以完成部署、运行 nightly、关闭评审任务，但这些完成状态尚不能保证“实际事件完整入库、当前事实正确呈现、可信失败进入能力状态、备份能够恢复”。本次有原生可执行反例，不能把未闭环统称为“只等更多数据”。

本次审查源码、运行已有隔离测试，并以临时数据复现问题；只新增审计材料，没有修改产品实现、提交、推送、部署、重启或访问生产数据库。所有故障样本均为合成数据，未调用外部模型提供商。涉及 systemd 的探针使用本地命令替身，演进探针的最终外部执行器无副作用。**本地反例证明代码能够出现相应错误，不证明现网已经发生。**

P1 表示身份边界、记忆事实、业务认可、能力退化或恢复能力存在应优先修复的错误；P2 表示有明确触发条件的覆盖、反馈、幂等或运维状态缺陷。不将证据缺失直接记作已证实漏洞。

## 覆盖范围与证据强度

源码清单覆盖 719 个维护文件、226,601 行、7,817 个 callable；语法清单为 662 项通过、57 项待各自原生工具检查。这是文件和语法覆盖，不是逐行证明全部语义正确。业务审查沿真实入口、权威记录、消费者、终态及失败恢复展开，并对关键发现进行独立交叉复核。

| 业务链路 | 本次检查及验证 | 结论 |
| --- | --- | --- |
| 宿主身份 → 授权范围 → 读取 | Hermes、Codex、OpenClaw 适配边界，真实 Runtime/RPC 桥；OpenClaw 提示内容与会话身份冲突 | F01；其他宿主不能据此推定同样越权 |
| 宿主事件 → L0 → 队列 → L1 | 重复内容的新事件、传输超时、真实 SQLite、抽取队列和最新状态 | F02、F03；队列清空不等于事件无丢失 |
| 检索来源 → 权威读回 → 选择 → 交付 | SQLite 查询、有限窗口、目标去向；实际 canonical 撤回的最终围栏 | F11、F12；本地撤回探针正常拒绝 |
| 注入 → ACK → 使用 → 评审标签 | 实际渲染引用、ACK 故障、终结与下一轮恢复 | F04；交付证据不可恢复丢链 |
| 生产采集 → 自动评审 → 正式质量摘要 | 标签条件、中文特征、扫描覆盖、诊断与正式认可的转换 | F05、F08、F09 |
| 记忆评测 → 实际运行检索路径 | 配置的 candidate source 与普通 memory_eval_ci 的调用对照；受控评测路径单独检查 | F10；普通评测与运行路径可能不同 |
| 知识采集 → 审查 → 应用 → 纠正 | intake 持久水位、知识适用性、persona 事件消费 | F15、F16；适用知识不足仍需具体证据 |
| 能力执行 → 负向证据 → 状态投影 | 同能力先成功后合法业务失败，真实 registry/catalog/projector | F06；新失败未影响旧 reliable 状态 |
| 缺口 → 假设 → Hermes 补丁 → 晋升反馈 | 原生缺口、知识关联、严格 v2 事务及反馈消费者；最终执行隔离 | F13；不能宣称实际补丁部署已验收 |
| 发布 → 健康 → 业务收据 → 恢复 | 部署尝试、精确提交、回滚恢复、探针限时、归档载荷备份 | F07、F14；技术切换与业务验收仍须分开 |

## P1：应优先修复的 7 项问题

### F01 — OpenClaw 提示中的非可信发送者可以覆盖会话用户

位置：[index.js:1314](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/integrations/openclaw/eimemory-bridge/index.js#L1314)，相关提取逻辑在 1061、1374 行。

- **触发条件：** `before_prompt_build` 有可解析的真实 session ID，但宿主没有另行提供 `userId/user_id/senderId/sender_id`。提示内容中的 `Sender (untrusted metadata)` JSON 被作为用户身份，优先于 session 推导值。
- **复现：** 以 caller-a 的同一宿主 context session ID（合成），普通输入不能读到 different-user-b 的私有记录；加入伪造 sender_id 后，经原始 JS 归一化、真实 OpenClawMemoryHooks 和 Runtime，返回该记录。
- **边界：** 显式宿主 user ID 存在时未复现覆盖；未证明 Hermes 或生产发生泄漏。
- **修复与验收：** 授权身份只来自可信宿主字段或已验证的会话身份；提示正文只作内容。验证伪造元数据不改变 scope，合法会话仍能读取自己的记忆。
- **探针：** `eimemory-audit-openclaw-identity.js` → `eimemory-audit-openclaw-read.py`。

### F02 — Hermes 把内容相同的新一轮事件当作旧事件重试

位置：[provider_core.py:477](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/adapters/hermes/provider_core.py#L477)，`adapters/runtime/service.py:351`、`knowledge/l1_pipeline.py:418`。

- **原因：** 采集 turn ID 由 session、用户正文、助手正文生成，未纳入实际不同的宿主消息身份；下游正确的幂等和已抽取判定反而固定了错误。
- **复现：** 三个不同宿主事件依次“启用 → 取消 → 再次启用”，第一、三次文本相同。第三次复用第一次 L0，结果仅 2 条 L0，active L1 仍为 `cancelled`，而最后事件要求 `enabled`。三轮队列均正常排空，dropped_writes=0。
- **边界：** 原生 provider、服务、SQLite、队列和 L1 路径执行；抽取及冲突模型以确定性结果替代。不是对现网“取消监督”事件的归因。
- **修复与验收：** 使用实际宿主事件身份；缺失时生成并持久传递一次事件 ID。同事件重试不重复写，不同事件即使正文相同也要推进最新状态；覆盖重启后的幂等。
- **探针：** `eimemory-audit-intake-probe.py`，`distinct_identical_host_turns`。

### F03 — Hermes 后台采集 RPC 失败后丢失可重试写入

位置：[provider_core.py:1645](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/adapters/hermes/provider_core.py#L1645)，异常转换在 1185 行、丢写计数在 1620 行。

- **复现：** 第一条 `sync_turn` 在到达服务前超时，随后不同事件成功。请求共 2 次，服务实际采集 1 次；第一事件不在 L0，队列已空，dropped_writes=0，没有持久化重试载荷。
- **影响：** 临时传输故障成为永久记忆缺失，正常状态指标不能显示尚未交付的写入。
- **边界：** RPC 客户端可以记录 failure_ledger，不能称“完全没有错误日志”；该日志不包含可恢复原始事件的完整载荷，不能替代 outbox。
- **修复与验收：** 持久化待交付事件，收到服务确认后再完成；支持“服务已提交但响应丢失”的幂等重试。永久拒绝与临时失败需有不同终态和可观测计数。
- **探针：** `eimemory-audit-intake-probe.py`，`failed_capture_transport`。

### F04 — Hermes 一次 ACK 故障会不可恢复地丢失交付与使用证据

位置：[provider_core.py:615](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/adapters/hermes/provider_core.py#L615)，重试分支在 635 行。

- **复现：** 原生 Runtime/RPC/provider 已向上下文注入引用，助手回复也使用该引用；仅 ACK 传输失败一次。provider 清空 delivered 后发送空 used 的终结，DB 成为 `terminal=true / not_used / ever_injected=0`。下一次正常 hook 没有重试 ACK，pending retries=0。
- **影响：** 已交付事实未形成可验证证据，可能进一步阻断生产正向标签。
- **边界：** 缺少确认时不直接授予正向标签是正确保护；问题在于丢弃恢复材料并先行终结。不能据此断言生产可信案例为 0 全由此造成。
- **修复与验收：** 持久恢复 ACK → 终结的顺序，故障后仍可补齐真实交付证据；禁止用推测或历史交付补造本次成功。
- **探针：** `eimemory-audit-hermes-ack.py`。

### F05 — nightly 可把 legacy 诊断评测汇总为正式召回已认可

位置：[recall_quality_contract.py:61](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/evaluation/recall_quality_contract.py#L61)，[result_contract.py:1018](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/scheduler/result_contract.py#L1018)。

- **原因：** 标签信任等不足判定仅对已识别的 judged contract 强制；legacy/未知契约可得到 sufficient。nightly 又仅用 `gate.ok && !vacuous` 生成认可结论。
- **复现：** 受保护的约定数据集路径、真实 SQLite、1 条记忆构成 10 条 legacy 已知项诊断；10/10 通过。标签信任与发布权威验证均为 false，却得到 `gate.ok=true / evidence_status=sufficient / recall_quality_accepted=true`。
- **重要边界：** 外层生产评测仍写 `accepted=false / gate_status=diagnostic / dataset_kind=diagnostic`；严格发布闭环另外检查 accepted 等条件。因此确认的是**业务验收摘要错误**，并未证明发布授权被绕过。
- **修复与验收：** 正式认可必须消费明确受支持、信任与权威验证通过的正式契约；legacy、unknown、smoke 继续保留诊断结果但不能被汇总为正式认可。检查原始报告、nightly 与发布收据的一致性。
- **探针：** `eimemory-audit-nightly-gate-route.py`、`eimemory-audit-evaluation-probes.py`。

### F06 — 合法业务失败没有进入能力负向证据，旧状态仍为 reliable

位置：[capability_contract.py:257](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/experience/capability_contract.py#L257)，[capability_acceptance.py:880](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/governance/capability/capability_acceptance.py#L880)，同文件 932 行。

- **原因：** 输出身份/类型有效性与业务检查是否通过混在 validation 中；业务规则失败先成为 validation error，后续不能生成 trace、evaluation run 和 observation。
- **复现：** 同一真实注册能力、修订、binding 和 executor 先返回合格 decision，后仅把 decision 改成不符合业务断言的值，保留合法结构与身份。第二次接受报告明确失败，却无 trace_record_id/evaluation_run_id；实际 projector 仍为 `reliable`，blocked 为空。
- **重要边界：** 失败 probe 和验收报告已保存，当前验收也为 false；不是所有失败记录被抹去。缺口是该可信失败没有进入权威能力状态链。
- **修复与验收：** 分开“证据可信/结构有效”和“任务成功”。可信失败必须形成可追溯的负向 observation，进入退化、观察或回滚决策；失败仍不得授权晋升。
- **探针：** `audit_capability_negative_evidence.py`。

### F07 — 备份报告成功，但未保存 SQLite 恢复所需的归档载荷

位置：[eimemory_backup.py:202](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/deploy/eimemory_backup.py#L202)，复制/校验在 115、228 行；`storage/sqlite_store.py:238,3737,4097`。

- **原因：** 大型 capability_score/recall_view 等记录可使用 SQLite 中的指针引用 `state/payload_segments/*.seg`。定时备份复制数据库及顶层 JSON/JSONL，却不包含这些 segments；完整性与清单校验无法发现被遗漏的文件。
- **复现：** 真实 Runtime 写入 200 KB capability_score，经原生备份与校验，`ok=true`、SQLite integrity=ok、清单字节数及 SHA-256 全部匹配。恢复至新 Runtime 后，读取该记录报 `PayloadSegmentError`，清单无 segment 文件。
- **并发对照：** 在真实物理快照完成后、逻辑导出前，按允许的 RuntimeStore API 更新同一记录；后续逻辑导出只有新版本，无法补足早先物理快照引用的旧载荷。没有伪造备份输出。
- **边界：** 未证明生产已丢数据，也不是所有内联记忆或所有逻辑导出都无法恢复；基础遗漏与是否并发无关。
- **修复与验收：** 以一致快照边界收集所有引用 segment，发布备份及清理旧备份前逐条验证指针并进行新 Runtime 恢复读回；不能以另一时刻的逻辑导出替代状态一致恢复。
- **探针：** `eimemory_audit_backup_segments_interleaved.py`。

## P2：覆盖与反馈可靠性的 9 项问题

### F08 — 中文自然问法可能被特征规则一律判为低信号

位置：[production_query_auto_review.py:148](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/evaluation/production_query_auto_review.py#L148)，`evaluation/real_query_schema.py:417`。

当前 tokenizer 不切分连续中文，超过 8 字符的整个 token 被去除，且特征要求至少两个 token。原生评审中，即使候选、交付、语义 relevant、独立证明和 host_used 均具备，连续中文问法仍因 `query_features_low_signal` 不获标签。修复应使用兼顾隐私的语言适配特征，保留独立信号要求；不能降低语义或授权标准。探针：`eimemory-audit-evaluation-probes.py`。

### F09 — 自动评审反复扫描最新 500 条，可饿死旧待审且报告开放数为 0

位置：[production_query_auto_review.py:513](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/evaluation/production_query_auto_review.py#L513)，531 行。

真实存储创建 501 条待审后连续运行两次，均扫描最新 500 条，累计只覆盖 500 条，最旧条目未评审；两次均 `completed/open_review_count=0`，无剩余或截断字段。应使用持久游标或待审优先，分别报告本批终结数与全量未覆盖量。**当前提供的生产 365 条低于此窗口，不能把此反例当作该轮阻断原因。** 探针：`eimemory-audit-review-default-window.py`。

### F10 — 普通 memory_eval_ci 新建 MemoryAPI，绕开运行时配置的检索源

位置：[benchmarks.py:30](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/evaluation/benchmarks.py#L30)，`api/runtime.py:100`。

透明计数源委托真实 SQLite 查询，正常召回经过配置源两次并命中；普通 memory eval 1/1 通过，但配置源调用为 0。评测通过不能覆盖正在使用的后端、engine 和 registry 组合。应复用运行时检索路径并保存实际后端身份。**受控记忆 benchmark 使用 runtime.memory，不受这一分支影响；不能据此解释生产受控 7/8。** 探针：`eimemory-audit-memory-eval-route.py`。

### F11 — 有限身份窗口被误当完整清单，错误认证唯一已存在

位置：[authority_gate.py:176](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/retrieval/authority_gate.py#L176)，208 行；`retrieval/engine.py:1792`、`adapters/create_safety_gate.py:55`。

构造 80 条同标题、同 scope/source 的实际记录，窗口内外各有一条有效身份，中间记录过期。独立读回确认有效身份为 2；有限扫描只确认一条后却给 `create_safety=exists`、ambiguity 为空，创建入口即使显式 force 也按已存在拒绝。修复应区分“窗口内一条”与“全量唯一”：继续有界分页或返回未确定，不得凭截断窗口认证唯一。探针：`eimemory-audit-identity-window-full.py`。

### F12 — 长自然问法尾部关键词同时被候选入口截掉

位置：[sqlite_store.py:5596](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/storage/sqlite_store.py#L5596)，anchor 路径在 5267 行。

FTS 取前 12 个词，anchor 取前 8 个词。46 条合法合成记录中，简短独特关键词查询返回目标；加入一般性前缀使独特词位于窗口之后，124 字符查询返回 14 条其他记录，目标不在候选，fallback=false。应从全问句进行有界的有效词覆盖或稀有词选择，不能通过放宽 grounding 补救来源漏项。**未读取生产查询正文，此例不确定就是 controlled-memory-f589… 的原因。** 探针：`eimemory-audit-query-tail.py`。

### F13 — 严格 Hermes v2 补丁事务与假设反馈消费者契约不一致

位置：[autonomous_evolution.py:789](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/governance/evolution/autonomous_evolution.py#L789)，`governance/evolution/dynamic_capability_evolution.py:1289,1348`。

生产者返回 candidate/transaction/promotion 的 v2 结构，反馈仍要求旧 `isolated_evaluator` 字段；阻断分支还期待旧 `apply_result` 包装。原生缺口、知识关联、策略、严格提案、实验及 candidate 路径得到 `applied=true/candidate_created=true`，反馈却为 `isolated_verifier_not_independent`；同类结果在阻断分支被说成 `candidate_not_created`。

此探针使用合成发布身份和无副作用的最终执行器，证明数据契约错配，**不是一笔真实生产补丁已执行并获准发布**。修复要消费经过验证的 v2 事务、独立 verifier 和最终收据，再更新原假设；不能填造旧字段绕过独立性要求。探针：`audit_evolution_native_probe.py`。

### F14 — 融合闭环忽略暂停失败，退出时无条件启动 L1 定时器

位置：[run_memory_l5_fused_closure.sh:23](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/deploy/run_memory_l5_fused_closure.sh#L23)，38 行。

EXIT trap 在配置校验前注册，且未保存原状态。原脚本在缺少仓库配置、尚未暂停任何资源时以 2 退出，却调用 start timer；另一探针让 stop 返回 3，脚本仍进入 memory eval。两次 systemctl 均由本地记录替身拦截，没有操作真实服务。应先验证配置、保存状态、确认暂停成功，最后只恢复本次暂停的资源并报告恢复失败。探针：`eimemory_audit_fused_timer.py`、`eimemory_audit_fused_stop.py`。

### F15 — intake 预览推进持久扫描水位，后续正式采集被冷却期跳过

位置：[loop.py:122](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/intake/loop.py#L122)，CLI `main.py:2657`。

真实 CLI report 以 `persist=False` 发现 1 个候选、写入 0，却更新 last_scanned_at。关闭并重开 Runtime，后续正式采集 scanned=0、skipped_not_due=1、written=0。一般默认周期可延迟约 20 小时，weekly 周期约 6 天。预览水位与成功持久化水位应分离；验证失败/预览不能吞掉正式采集机会。探针：`eimemory-audit-knowledge-persona-closure.py`。

### F16 — persona evolve 重复消费同一纠正事件，持续累加同一惩罚

位置：[cli.py:55](https://github.com/darrowz/eimemory/blob/e2bbc60e0a893d72d37de55e3b05bf5d910f058f/eimemory/persona/cli.py#L55)，`persona/evolver.py:42`、`persona/state.py:44`。

只有 1 条 correction，重复执行真实 CLI 并每次重开 Runtime，verbosity 从 0.25 到 0.10 再到 0。活跃事件每次全部读取，delta 没有消费水位。需将纠正事件消费与状态更新绑定为幂等事务，并覆盖提交后重试。**已证实手工 CLI 重复执行的错误，未发现 nightly 自动调用这一入口，不能宣称定时任务已持续降低生产参数。** 探针：`eimemory-audit-knowledge-persona-closure.py`。

## 尚未证实的风险和覆盖缺口

以下不计入 16 项确认问题，也不应以猜测补齐生产因果链：

- **SQLite 派生投影完整性：** 故障注入删除单条投影后，全局迁移收据仍可能使读取认为投影完整；尚未证明正常写入路径会生成该损坏。需设计可恢复的完整性检测，但不能称生产目标已索引损坏。
- **全局 FTS 排名统计：** 其他 scope/source 的记录可影响授权候选的 BM25 排序；未观察到内容越权，也未找到明确要求排名完全独立的契约。
- **事件发生时间：** Hermes 主采集未完整传递实际宿主事件时间，L0 时间更多反映接收时刻。对延迟重放、纠正与撤回的真实时序，还需独立验收。
- **显式 Hermes recall 的认证覆盖：** 工具召回并不自动形成与 proactive 相同的带签名生产采集证据。工具能返回内容不等于能积累正式正向标签，应明确支持范围，而不是自动把它算作 gold。
- **独立证据开关：** 普通本地准入路径不消费旧 catalog 的 enforce/shadow，`final_revalidate` 无生产调用。需整理功能承诺与测试；若仍承诺这些开关约束普通交付，需要另行修复接线。
- **演进前置条件：** 修补失败能力前仍要求整套验收先通过，以及知识适用性扫描上限，可能限制进展；没有将其直接列为算法缺陷。须区分允许隔离实验与允许正式晋升，不能为了产生候选而放宽后者。
- **真实环境验收：** 没有执行三位同事各自飞书/Hermes 会话的四类验收，没有在线 PostgreSQL、真实提供商、真实 systemd 或真实 Hermes 补丁部署验证。

## 验证结果

以下批次有重叠，**不能相加作为独立测试总数**。通过现有测试也不能消除本报告的新反例。

| 检查批次 | 结果 | 范围或限制 |
| --- | --- | --- |
| 本次扩展业务闭环回归 | **360 passed**，34.89s | business closure、contract、safety、memory/intake/learning/recall closure、ops/security/promotion 等 |
| 采集与 L1 | 206 passed、33 skipped | 真实 Hermes 依赖/源码未配置的项跳过，不能算宿主验收 |
| 宿主与 RPC 安全 | 339 passed | Hermes/RPC 194、Codex/OpenClaw 145；合成环境 |
| 召回与来源 | 177 passed | 7 个相关已有套件；SQLite 原生反例另列 |
| 评测与质量门 | 94 passed | 既有评测套件；新反例不由这些成功数覆盖 |
| 能力演进 | 77 passed | 59.12s；最终外部补丁执行未在本次进行 |
| 发布与运维 | 305 passed、1 skipped、55 subtests passed | 344.26s；本地隔离，无生产 systemd |
| 既有失败基线复核 | **129 passed、10 failed、1 skipped** | 6.56s；不能报告项目测试全绿 |

10 项失败与此前已确认的旧基线一致，本次未修改产品代码。进一步分类：

1. 6 项 independent-evidence 集成断言仍期待普通召回同步调用独立证据/模型路由；1.14.26 的当前契约已改成本地准入、交付后评审。
2. 2 项预算断言仍期待后续 scope 超时后，前序有效候选因为旧 verifier 预算而被拒绝。
3. 1 项 `timeout_seconds<=8` 与启动预算、完成 ceiling 已分离的契约不一致。
4. 1 项 SQLite 截止测试仅替换 SQLite 时钟，没有同步 RuntimeStore 时钟；原断言完全保留，仅补齐同一假时钟后通过。

因此分类为 **9 项契约断言未同步、1 项测试时钟未同步**，仍是必须修复的测试维护欠账。对担心的撤回边界另外用原生探针验证：在 default/lightweight 两条真实 engine 最终权威读取前，将 canonical 记录改成 inactive，两者均返回 0、`status=unavailable/authority_changed=1`。这只能证明该探针中的撤回有效，不能扩大为所有并发情形已验收。

既有精确提交 CI 也与本地反例并存：Linux [38298898338](https://github.com/darrowz/eimemory/actions/runs/38298898338) 为 1094 passed、3 skipped、78 subtests；项目 [38298898328](https://github.com/darrowz/eimemory/actions/runs/38298898328) 的业务任务为 1686 passed、2 skipped、93 subtests，依赖审计成功。这些均为选定检查，不能代替完整测试及业务闭环；动态代码演进要求的全套 tests 仍会受上述维护欠账阻断。

## 与现网问题的关系

本次源码基线为 1.14.73；用户最后提供的生产收据仍为 **1.14.72 / `612dc458322eb60f1631b4605b0a30eb8a517e99`**，部署 `rec_fba7350aa0b4`、nightly `ref_d6eb0f6e9c6f`。两者不可混成同一版本的验收。

据用户已有收据：部署及 nightly 执行成功，受控评测 7/8，正式认可 0/3，9 项动态演进候选假设均为 0。目标 `mem_c2c5b75407f1f262177f4586400db98f` 在 provider 返回的 14 条候选中不存在，足以排除“已进入 selector 后才被 grounding 删除”，但不能在没有原查询/来源内部诊断时指认为本报告 F12 或索引缺失。

可信案例长期为 0 必须分阶段处理：事件是否实际入库、是否有候选、是否实际交付、交付证据是否可恢复、独立信号是否合格、评审是否覆盖。F02/F03/F04/F08 提供了需要修复的具体反例；它们**不是对所有生产零样本的统一解释**。空候选仍不能认证为正确无答案，更不能靠降低正式门槛补齐 3 条。

## 修复顺序与完成标准

1. **先保护身份和原始事实：** F01、F02、F03、F04。以真实事件 ID、持久 outbox、可恢复 ACK 顺序建立端到端闭环；同时保留授权边界。必须通过不同事件/同事件重试/响应丢失/重启恢复/用户撤回的对照测试。
2. **使验收和能力状态忠于证据：** F05、F06；并修复 F07 恢复能力。验证诊断不冒充正式认可、合法失败进入退化状态，以及备份在新 Runtime 可完整读回。
3. **让评测覆盖真实使用：** F08、F09、F10、F11、F12。修复中文适配、扫描进度、运行时路径一致性与候选覆盖；不通过降低 grounding、信任、授权或错误召回阈值取得通过。
4. **完成演进反馈与运行幂等：** F13、F14、F15、F16，并清理 10 项测试契约欠账。严格 v2 补丁的终态必须回流至原缺口/假设；重复执行不能重复学习，退出不能改变原运维意图。
5. **再做生产验收：** 精确绑定部署版本，在真实 Hermes/飞书身份下分别覆盖已知记忆自然问法、合法无答案近邻、跨成员权限、来源读回。每人保留通过/失败/不可评测结果，生产自然样本与受控样本分开，正式最低 3 条及其他门禁保持不变。

**允许宣称“自迭代闭环完成”的条件：** 有可信失败形成缺口，有适用证据形成可检验假设，有合法隔离补丁事务，有独立验证与受保护晋升，有终态结果回流原假设；失败可退化/回滚，重复和中断可恢复。当前至少 F06、F13 直接打断这条链，候选数量增加本身不能作为闭环验收。

## 审计附件

同目录 `business-closure-2026-10-13/` 保存探针、使用说明、SHA-256 清单及机器可读证据摘要。脚本针对上述不可变提交，使用合成数据；有些直接复用该提交的测试夹具。**脚本正常退出表示诊断完成，不表示产品业务通过。** 原始大体量临时数据库及任何生产内容不纳入附件。

本报告是修复前审计，不是部署授权或生产通过报告。后续修复须逐项记录提交、回归证据和实际验收状态，不能覆写本次反例使其看似从未发生。
