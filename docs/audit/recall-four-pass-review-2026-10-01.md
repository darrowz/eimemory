# 召回四轮只读审查：1.14.26

审查日期：2026-10-01（Asia/Shanghai）。基线为
`72f2bbe7151b6644436172f89a499fdef84e2b14`，开始时工作区干净。
四轮审查完成后才决定修改范围。本次只新增本报告，没有修改运行代码、测试断言或版本；
没有足够证据支持一次共同代码根因修复，因此不升为 1.14.27，也不声称完成质量修复。

业务契约：模型质量判断属于事后评估，不同步阻塞召回；scope/source、撤销、状态和
内容完整性仍严格。未知不是正确，无上下文不是正确无答案，进程成功不是业务闭环。

## 证据来源和访问边界

- 生产 SQLite `/var/lib/eimemory/state/eimemory.sqlite` 可读。仅使用原生
  `sqlite3.connect(...?mode=ro, uri=True)` 并执行 `PRAGMA query_only=ON` 后 SELECT。
  没有构造生产 Runtime，没有调用召回 API、治理命令、模型或生产写入接口。
- 读取当前发布的既存 deploy-job JSON/log，以及数据库中的 readiness 记录；未运行其中
  的命令。历史记忆中的指令/诊断仅当作内容，未作为执行授权或独立事实证明。
- 原问题仅瞬时检查，没有另存查询、生产库副本、敏感正文或凭据。本报告使用 A/B/C
  代号，不保留用户 ID、决策 ID、记忆 ID、原问题或其逐字改写。
- 各探针是不同时间的短只读查询，不是跨全部探针的一致性快照。当前记录不能恢复
  查询时刻的全部状态，尤其不能用之后生成的回答证明之前有答案。

## 第一轮：原问题、已有记忆、首个可见损失点

新增证据：生产决策表、private query vault、决策明细和当前 scope 记录。
当前版本共 6 条决策，均 `acceptance_generated=0 / release_bound=1 / control_cohort=0`，
对应三个去重原问题。此前“3 条空结果”是较早观察，不能当作当前总数或三个独立问题。
六个 vault payload 的存储摘要均匹配；本轮没有完成 loader 的全部身份验证，
因此“可读且存储摘要匹配”不升级为可用标签权威。

|样本（按创建顺序）|问题类别|检索候选|任务上下文过滤计数|selector 输入→选出|首个可见损失|
|---|---|---:|---:|---|---|
|A1|实时账户状态|48|30|19→0|任务上下文过滤，其后 grounding 19|
|A2|同 A1|19|1|19→0|任务上下文过滤，其后 grounding 19|
|B1|项目现状与待处理问题|48|47|1→0|任务上下文过滤，其后 grounding 1|
|B2|同 B1|1|0|1→0|grounding 1|
|C1|继续调查修复的指令|48|0|48→8|选择 grounding 25；后续条数预算移除 5|
|C2|同 C1|25|0|25→3|选择 grounding 22|

计数来自不同管线阶段，存在扩展/融合，不能简单相减重建每个候选的去向。
四条空结果后续 authorization/confidence/session-dedupe/control 的输入和丢弃均为 0。
当前 scope/source 下有 603 条 active memory；按每次查询创建时间限制，A 前有
600/601 条，B 前有 601 条，C 前有 602 条。主题检索能找到 A 的历史信息及 B 的
既往项目状态记录；主题命中不是答案标签，更不是历史候选成员证明。

排除：全库无记忆、当前 scope 完全无记录、此次仍在同步模型验证处超时、
空结果被后续控制组/去重全数吞掉。没有排除：检索漏项、排序截断、启发式误拒绝。
A 的历史数值不能证明当前实时数值；B 相关历史的存在也不足以证明能回答当前全部状态。
持久化诊断没有检索/排序候选 ID 列表，无法确定某条应答记忆的历史首个丢失点。

## 第二轮：调用边界、成功对照和近邻负例

新增证据：对 vault 中原问题仅执行本地纯分类函数；追踪
`MemoryAPI.recall → GovernedRecallEngine._recall → _select_post_fusion_items →
ProactiveRecallService`，不对生产执行整条调用链。

A/B 的实际问法都被判为 `research`，无 operational cue、无 requested attribute；
C 被判为 `operational_issue`。`MemoryAPI._allows_operational_recall`、默认 lane 过滤和
`recall/task_queries.py` 的窄语法解释了这种路由差异。三条选择路径已经移除同步模型评审，
legacy grounding 仍用答案形状、词法、semantic/vector 等本地证据。

使用全新虚构名称的纯分类对照（不是生产复现、不是 gold）：

|脱敏测试问法|当前路由|
|---|---|
|Atlas项目现在有哪些卡点需要修复？|research|
|Atlas项目任务进展有哪些待验收？|task_recall / task_status|
|Atlas部署失败根因是什么？|operational_issue|
|研究任务调度算法|research|
|如何改善项目管理软件的进度展示？|research|
|目前账户剩余额度是多少？|research|

自然项目状态表达的识别覆盖偏窄是待验证假设。不能通过把所有项目/修复词都变成
operational permission 来修复：那会改变普通研究、操作日志和任务证据之间的隔离。

C1 服务端生成 3 条上下文，账本 3 项 `ever_injected=1`；C2 也生成 3 条上下文，
但 3 项均 `ever_injected=0`。两者状态都是 `not_used`，均无 host-used 正反馈。
因此只有 C1 有注入确认；C2 不得作为主机成功交付证据。`context_delivered` 本身不够。

排除：所有自然调用都空、非空结果必然已经注入、单靠提高数量上限能解决 B 的
selector 全拒绝。保留：自然问法的路由/grounding 误拒绝；尚无历史逐候选证据，不能
把 C 的成功当作 B 的正确候选集合，也不能拿此对照认证模型相关性。

## 第三轮：事后评估覆盖和必要性

新增证据：`evaluation/semantic_relevance_monitor.py`、
`production_query_auto_review.py`、`query_input_vault.py`、scheduler 接线，以及生产记录计数。

- nightly 已接 `monitor_channel_deliveries`，在 auto-review 前运行；每频道最多 8 个新观察，
  每 scope 检查最近 512 决策。它只评估已注入的 `memory.recall/research.task`，要求
  精确 scope/source、可验证 release、查询摘要及 render 完整性。
- 空交付留下 `unknown/empty_delivery`，不调用模型；空候选 auto-review 是
  `pending/no_candidate_refs`，没有自动认证正确无答案。模型异常/未知也不产出正确标签。
- 该监控没有反事实候选集合或独立相关性 gold，所以不能发现“本该召回而没交付”的
  漏召回。让模型只看空数组仍不能证明有答案或无答案。
- SDK/CLI 直接 `MemoryAPI.recall` 不创建 delivery ledger，无法进入这条监控。
  这属于已确认覆盖限制；本轮没有 SDK 生产调用量证据，不能认定是当前四条空结果的根因。
- 生产 records 中 `source=eimemory.semantic_relevance_monitor` 为 0。源码接通不等于
  当前自然 scope 已被评估；未读到可绑定此次样本的 nightly 步骤结果，无法区分尚未运行、
  精确 scope 没被调度或身份条件不满足。没有启动 nightly 补证。
- 同一 evaluation identity 的 unknown 也缓存，现有测试明确约束每身份只调用一次。
  这会保留暂时故障造成的未知；当前无观察记录，不能归因为此次生产故障，也不擅改重试策略。

必要性判断：若要宣称完整召回质量，确实需要覆盖空结果和无 ledger 调用；若只宣称
“对已交付结果做有界事后观察”，当前实现满足其窄契约。没有足够的授权历史候选及
独立标签时，新增队列/模型调用不能补成正确质量证据。本次不扩建治理系统。

排除：空结果自动绿、模型失败会事后清空交付、监控已经覆盖 SDK、有 scheduler 接线即
当前 scope 观察成功。覆盖缺口是事实，但不等于已证明存在四次漏召回。

## 第四轮：发布契约、同步 proof、非召回 L5

新增证据：当前提交的既存部署最终 JSON、部署 log 中 release-closure summary、
最新 readiness（2026-09-30T15:38:13Z）及 `closure_rehearsal.py` 的缺证规则。

最终 deploy-job `ok=true / health.ok=true`；同次业务闭环摘要却是
`business_closure_outcome=failed / disposition=diagnosis_required`，
`blocked_stage=closure_rehearsal`，原因为
`bootstrap_pending_non_recall_l5_evidence_incomplete`。
通道验收成功；业务闭环未认证。二者不能混为一个验收结果。

精确非召回缺项为：

1. `current_release_verified_real_tasks_below_minimum`
2. `current_release_verified_real_task_types_below_minimum`
3. `historical_verified_real_task_types_below_minimum`
4. `verified_real_replay_missing_or_failed`

最新 readiness 中历史 verified real tasks=14、types=1；当前部署 verified real tasks=0；
生产 code-evolution transactions=0。历史计数不是此前报告的 0，不能沿用旧判断。
当前真实任务路径要求至少 10 个/5 类；历史积累连续性至少 10 个/4 类，另有质量条件；
当前代码的已验证真实回放是另一条路径。等待计时器不会制造任务类型或回放证据，
没有事务也不能解释为一个正在等待观察完成的事务。

追踪 release closure、召回质量门、auto-review 和工具收据后，没有发现当前发布
必须同步生成 model proof 的直接调用契约。`business_recall_supported` 仍要求绑定最终
记录的 proof，但只用于已验证业务证据收据，普通交付不以它为前提。其结果可能使纯召回
任务终态仍为 unverified：这是尚无新的事后质量收据通路的限制，不能把普通结果直接改为
verified。auto-review 可通过“语义 relevant + host used”接收，无需旧 proof；本批没有 used。

排除：部署最终 JSON 成功等于业务验收成功、非召回缺证只是等待、必须恢复同步模型
proof 才能交付、只要删掉 proof 校验便完成质量闭环。未独立重验部署签名与全部 L5
底层事实，本轮结论是已存报告的可核对状态，不是新发布认证。

## 统一根因矩阵与修复决定

|边界/现象|分类|依据|处理|
|同步模型阻塞此次召回|非本次缺陷|当前空结果损失在本地过滤/选择；无模型验证阶段|保留 1.14.26 分离契约|
|自然项目状态表达进入 research|已证实路由差异；误拒绝为假设|纯函数对照、历史相关内容、任务 lane 丢弃|没有历史成员/答案标签，不放宽过滤|
|grounding 全拒绝|已证实发生；是否漏召回未知|四条空结果 selector 计数|不通过降低阈值猜修|
|空结果漏召回评估缺失|已证实覆盖限制|只审已交付文本，空结果 unknown|不认证无答案；需要独立应答证据|
|SDK 无账本监控|已证实覆盖限制；当前相关性未知|SDK 调用链与 monitor 数据源|无生产使用必要性证据，不新建通路|
|非空上下文但无注入确认|已证实证据差异；原因未知|C2 ever_injected=0|不计作成功交付；未诊断 host ack 根因|
|普通召回无 proof|非交付缺陷；验证收据能力限制|严格业务收据与交付分离|不伪造 proof、不降低 verified 门槛|
|非召回 L5 缺证|已证实业务阻断，不是单纯等待|当前闭环摘要及 readiness|保留 diagnosis_required，不改成通过|

不存在证据支持“以上全部来自同一个代码错误”。统一矩阵的作用是区分因果链，而不是
强行统一根因。修复决定：本次只纠正调查结论与验收说明，不改代码；因此没有红→绿
修复序列。不能把下面已有测试通过冒充新修复验证或生产质量恢复。版本保持 1.14.26。

## 相关回归

四轮完成后运行以下六个现有文件，RTK 输出 `176 passed`，进程退出码 0：

```text
tests/test_recall_posthoc_quality.py
tests/test_semantic_relevance_monitor.py
tests/test_production_query_auto_review.py
tests/test_task_recall_repair.py
tests/test_release_closure.py
tests/test_caller_completion_authority.py
```

覆盖同步模型故障不阻断交付、scope/source/撤销/伪造拒绝、去重、空结果及未知不认证、
任务近邻研究负例、发布缺证不刷绿和最终 proof 绑定。未改任何断言，没有全仓测试。
测试使用本地临时库和受控 provider，不是生产检索重放或真实模型质量测试。

## 未覆盖及下一步取证边界

- 缺查询时刻的逐候选 ID、rank、filter/selector 决定以及当时记录版本；不能恢复
  PostgreSQL 向量索引、水位和候选截断，因此没有精确历史检索复现。
- 缺自然原问题的独立应答标签、实时状态权威和“应答记录在调用前存在且有权访问”的
  完整绑定；当前历史主题匹配不能填补这些缺口。
- 未执行真实 provider，也未审计主机端 C2 注入确认缺失原因。未证明 nightly 会覆盖
  当前自然用户的精确 scope；base scope 和频道 scope 不得自动视为同一用户权限。
- 未证明 SDK 生产调用需求，未接入 SDK 事后评估；空结果反事实评估及暂时 unknown
  重新评估仍未实现。这里的未知保持未知。
- 未重新运行/认证发布闭环、真实任务回放或代码演进事务。下一次修复应先取得相应
  缺口的证据，再增加会失败的局部回归；不能先改过滤阈值或验收断言。

没有生产写入、重启、提交、推送、部署或其他项目修改。

## 接续取证与局部修复：1.14.27（2026-10-01）

本节接续前述四轮，不重新执行四轮或原有 176 项。前文“无代码修改/版本保持
1.14.26”描述的是前次审查；本节新增确定性路由反例后作出不同修复决定。
开始时本报告已是未跟踪文件，保留其原文。生产与 Hermes 项目均只读。

### 1. 已存 nightly → 精确 scope → 零观察

只读来源：用户级 systemd 的 `eimemory-nightly.service` 及 drop-in、
`/etc/eimemory/settings.json` 的 scope 默认字段、已存 journal、SQLite 中
`title=Supervisor run: nightly` 的记录。没有执行 nightly，没有加载生产 Runtime，
没有输出环境文件中的凭据。

- 生效服务入口为 `/opt/eimemory/current/.venv/bin/eimemory nightly`，配置目录为
  `/etc/eimemory`，根目录为 `/var/lib/eimemory`。CLI `main` 用设置与
  `hongtu_scope` 构造 scope；latest persisted supervisor scope 直接确认是 base
  workspace 的 operator 用户。当前自然 Hermes 样本是另一个用户，不能合并。
- 最近两份成功报告时间为 **2026-09-30 11:20:40Z、12:26:57Z**；latest record
  更新时间 12:27:08Z。它们的 `recall_semantic_relevance.status=observation_only`，
  new/reused/deferred/provider_calls 均为 0，by_channel/by_surface 为空。
  journal 与数据库报告一致。它们早于 A/B/C，C1 创建于 16:19:20Z。
- `run_nightly_jobs → _run_semantic_relevance_monitor → monitor_channel_deliveries`
  会展开频道 workspace，但保留 tenant/agent/**user**。
  `monitor_deliveries` 的 SQL 对 channel/tenant/agent/workspace/user 全部精确匹配，
  不会发现当前另一个 Hermes 用户的 C1/C2。
- 此次读数中该 operator Hermes scope 仍有 58 个旧决策：54 个非控制组、4 个控制组，
  均 research.task、release_bound=1、acceptance_generated=0，且早于 latest nightly。
  因此“整个 nightly 没有任何决策”也不是合理解释；旧报告的零观察不能自动解释为
  当前用户查询被拒绝。旧 scope 的完整 release 资格另属问题，不拿其 0 认证正常质量。

对当前自然用户，原因已经可以分层：**没有样本之后的已存运行证据，且现有 scheduler
scope 不覆盖该用户**。即使原配置下一次运行，展开频道也不会自动覆盖此用户。
这两点发生在查询输入校验之前；不能把当前用户的零观察归因于 vault 输入拒绝。
源码中资格不符会 skipped；通过资格后 query_unavailable/query_unverified 则会形成
unknown 观察。这里没有声称当前 vault 已通过全部验证。未扩大调度用户权限，也没有
把 base 用户替换成当前 Hermes 用户。这是已查明的调度覆盖限制，不是模型故障。

### 2. C1/C2：主机字节、RPC 路径与账本终态

额外只读来源：Hermes `state.db.messages`（只输出时间、role、工具名、引用命中计数），
主机 `run_agent.py`、`agent/memory_manager.py`、`agent/turn_finalizer.py`，以及生产
feedback 与 proactive_decision_items。所有 SQLite 连接均 mode=ro + query_only。
原文、用户/会话/决策/引用标识均不写入报告。

|证据|C1|C2|
|---|---|---|
|服务端创建（UTC）|16:19:20.237337|16:20:33.971346|
|主机当前 user 消息|16:19:18.388602 的 api_content 命中其 3 个引用|该消息及检查到的会话消息未命中其引用|
|主机 final assistant|16:20:33.221102，无引用|创建发生在此 final 之后|
|持久化 feedback|16:19:20 volunteered×3；16:20:33 injected×3；not_used×3|16:20:33 volunteered×3；16:20:34 not_used×3|
|最终账本|terminal=1，ever_injected=1×3，not_used×3|terminal=1，ever_injected=0×3，not_used×3|

C1 ack 的完整接线为：hook `post_llm_call` 提取当前 user 的 model-facing 引用 →
provider `on_post_llm_call` 取 offered 与 injected/cited 的交集 →
RPC `adapter.proactive_ack` → runtime adapter `proactive_ack` →
`ProactiveRecallService.mark_injected` → SQLite 原子状态转换，只有 injected 转换置
`ever_injected=1`。之后 terminal 没有 used 引用，落到 not_used。主机字节与实际
injected feedback/ledger 三方相符，证明这次 ack 已落账；没有将无 used 解释为没交付。

C2 **不是显式工具召回**：其 `hermes-query-` turn identity 在 provider `_fetch_context`
中生成，走 `adapter.proactive_prefetch`；显式 `eimemory_recall` 走 `adapter.prefetch`。
该回合主机工具记录为 skill_view/terminal，没有 eimemory_recall。
主机完成回合后 `_sync_external_memory_for_turn → queue_prefetch_all → queue_prefetch`
会用刚结束的 query 作背景预热；provider `_complete_prefetch(background=True)` 在没有
等待消费者时移除 pending，`_close_abandoned_pending` 调用 proactive_terminal，
不调用 ack。C2 的创建顺序、无模型字节命中及 volunteered→not_used 实际转换吻合这条
路径。这是正常未消费预热终态，不是有注入证据的 ack 丢失。

精度边界：此时间窗 RPC journal 没有逐请求日志，故上述方法调用归属由主机源码、
专属 turn identity、主机消息与持久化转换共同推断，不冒充捕获到了网络 RPC trace。
已确认终态落账；并非仅凭空日志断言调用成功。没有为这个正常清理行为修改代码。

### 3. lane 是业务选择启发式；最小确定性反例与修复

应有契约：在同一已授权 scope/source 中，询问某项目现状/进展/卡点，应该走任务状态
证据选择；不应要求用户额外说出“任务”才读取普通 conversation 中的状态事实。
研究管理软件/算法、询问如何实现修复仍是研究/过程问法，不因为出现“项目”就进入
状态路由。项目卡点问法进入此路由，不代表完成记录足以回答卡点。

`task_queries.py` 本身声明 task state/history intent **not an audit permission**。
`MemoryAPI._allows_operational_recall` 与 `_record_recall_lane` 是按词法、task_type、
memory_type 等作业务过滤；它们不能替代 tenant/user/source 等真实访问边界。
本次不把“修复/项目”变成 operational permission。前文把这些 lane 的区别泛称为
权限隔离过于宽泛，本节予以纠正。

冻结依据：在 B1 精确 scope 中 SELECT active memory，要求 **created_at 与 updated_at
都早于 B1**，得到 2026-09-28T11:44:32Z 创建且同刻最后修改的一条 conversation。
保留其 `memory / active / Hermes completed turn / hermes.memory / hermes /
conversation` 结构和一条明确已完成的事实句；移除其他正文，以 Atlas 替换项目/路径，
数量替换成“若干”，身份替换成本地 owner/agent/workspace。内容固定于
`tests/test_project_status_route.py`，没有生产 ID、原问题、数据库副本或凭据。

这是**基于调用前记录最小脱敏结构的隔离临时库对照**，不是生产历史重放：没有复原
历史向量索引、召回候选、排序水位，也不证明那条真实记录当时进了 B1 候选。
但一个确定性业务路由错误不需要独立人工 relevance 标签才能成立。

|对照|修复前|修复后|
|---|---|---|
|Atlas项目现在进展如何？|research；临时库返回空|task_recall；返回固定记录|
|Atlas项目任务进展有哪些待验收？|task_recall；返回固定记录|保持返回固定记录|
|Atlas项目现在有哪些卡点需要修复？|research|task_recall（只断言路由，不将完成事实认证成卡点答案）|
|任务调度算法、项目进展管理软件论文、如何改善进度展示、如何修复卡点|research|仍 research|

红测试先运行新增文件，**3 failed / 5 passed**：两条自然问法误路由，以及关键词成功、
自然问法空结果的同库对照失败。随后仅在共享 `task_recall_mode` 中识别 project+state
及中文项目卡点/待处理/待修复/阻塞；保留 research/procedure 排除项。
该共享函数同时供 intent、答案形状、Hermes 工具 task_type 使用，没有加第二套路由。
新增对照中自然/关键词均不获得 operational permission；同文但异用户、异 source、
revoked、audit 的负例都不被返回。未修改真正 scope/source、状态、内容完整性校验，
未修改 nightly 或 ack，未恢复同步模型。

### 4. 验证与交付边界

新增对照首轮转绿为 8 passed；加入权限/撤销/audit 负例并同步版本后，focused 运行：

```text
tests/test_project_status_route.py
tests/test_version.py
tests/test_hermes_adapter.py
```

RTK 汇总 **62 passed，退出码 0**。包含背景预取清理、ack、失败重试等既有 adapter
回归；没有重跑前文六文件 176 项，也没有全仓测试。此结果是本地行为验证，不是生产
召回质量认证。版本在 pyproject、Python version、两个 Hermes plugin manifest 与
CHANGELOG 同步为 **1.14.27**，尚未部署，生产仍为 1.14.26。

本次完成本地最小修复与报告补充；没有生产写、重启、提交、推送、部署或其他项目修改。
