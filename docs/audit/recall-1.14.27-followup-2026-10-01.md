# 1.14.27 接续验收：局部修复 1.14.28（未发布）

## 边界与开始状态

工作区初始干净，HEAD 为 ce749755461782ab34314a1c6408f7531ee573d0，detached HEAD，版本 1.14.27。
沿用 Ponytail 最小修复；未启动子代理，未调用直接模型评估，也未修改模型配置。
本次不能核实或切换宿主实际模型为用户指定的 gpt-6-astra low，不将其写成已验证配置。

生产 SQLite 使用 `mode=ro`、`PRAGMA query_only=ON`，关联取证使用只读事务。
先由既存 capture allowlist 对齐范围，仅输出两个授权范围的 monitor 数量（0、5），
随后只在当前用户 Hermes exact scope/source（下称 S）追踪观察与对应 decision/vault/记忆。
另检查 S 内、调用前创建的最近最多 512 条 memory/persona 当前记录以比较时间和文本类别。
没有构造生产 Runtime，没有调用 RPC、monitor、nightly、生产回放、写入或重启。
不同探针之间不是统一快照；当前记录不能冒充历史完整候选集合。
没有保存生产数据库、查询 vault 全文或记忆全文。报告与测试不含身份、引用、会话或凭据。

## 已验证：自然观察与实际投递

- S 有 5 条持久化 `eimemory.semantic_relevance_monitor` 观察；全部观察摘要校验通过，
  每条均能在同范围最近 512 决策中唯一匹配。
- 4 条 `unknown / empty_delivery`；不能计作正确否定、漏召回或模型失败。
- 1 条 `off_topic / evaluated`，`relevance=[unrelated]`、`unanswered=true`。
  对应 `research.task`，版本 1.14.27，非控制组，`acceptance_generated=0`。
- 该自然决策创建于 **2026-09-30 17:12:43.911728 UTC**；vault payload 摘要及原查询摘要均匹配。
  原问题语义为只读核对最终收据与评估结果。
- 投递 1 条 active conversation memory；账本 `ever_injected=1`，最终 `not_used`。
  `not_used` 不能解释为未投递。持久化 title/text/context 为空属于隐私清理，
  不能把这些空列当作实际投递为空。
- 精确 scope/source 权威记录创建、更新时间均为 **2026-09-30 16:32:10 UTC**，早于调用。
  原正文 1798 字符，按既有投递函数重建 1200 字符；title+text render digest 完全匹配。
  内容是另一记忆产品的图片介绍、检索与记忆整合讨论，不是本次最终收据/评估结果。
  正文和实际投递段都不含收据/评估事实；不是正确答案仅在截断后丢失的证据。

因此，已有 off_topic 判定与此条实际投递吻合。此结论是对已存标签和投递的人工核对，
不是新模型标签，也不认证所有自然问法。宿主最终模型消息字节本次未另行读取，
实际投递证据来自已注入账本及精确 render digest。

## 首个确定误入边界与局部红绿测试

冻结的脱敏结构：S / source=hermes / kind=memory / memory_type=conversation /
active / title=`Hermes completed turn` / 图片介绍某项目的正文 / 上述调用前时间关系。
测试将产品名替换为 Atlas，正文缩为无私密内容的结构片段；不保存真实 embedding、ID、全文。

原查询的 `task_recall_mode=''`、intent=research。vault 中 effective query 却附加
`Context entities`，从历史标题带入 `completed turn`，从介绍正文带入“项目”。
相同分类函数对 effective query 返回 `status`、intent=task_recall。
这发生于 `_recall_query`，早于候选检索、状态证据筛选、排序和投递。

决策诊断进一步吻合：48 个候选，task_evidence_missing=23，selector 的
requested_attribute_missing=24，最后选中 1 条；这些是既存汇总，不是完整候选清单。
当前轻量状态证据检查接受正文中的“正在”，与历史标题造成的错误状态路由组合。
不能据此断言被过滤的 24 条中哪条就是正确答案；没有历史逐项候选证据。

`tests/test_proactive_context_route.py` 冻结上述结构及相邻控制。
修复前 **2 failed / 2 passed**，直接证明历史上下文改变原问题的任务路由。
最小修复仅在 `_recall_query` 比较扩展前后 task mode：若改变则使用原查询；
路由不变时保留既有实体扩展。真实已存 effective query 的同一局部输入也验证恢复 non-task。
没有改变阈值、同义词、模型、权限、时间权重或通知过滤规则。

限制：这是查询扩展边界修复，不证明修复后能召回正确收据。
原本依赖历史上下文才形成任务路由的省略问法会保留原问法；未做此类自然质量认证。
相同 task mode 内的项目串扰、属性污染仍未穷尽验证，不扩建通用查询重写架构。

## 新旧记录、源时间与通知风险

S 内截至该自然调用、最近 512 条当前 memory/persona 的有界检查：

|内容标记（仅用于归类）|数量|创建时间范围（UTC）|调用后更新数|
|---|---:|---|---:|
|同时含收据与评估|19|09-20 10:53:17 至 09-30 17:04:55|0|
|同时含 eimemory 与 1.14.27|3|09-30 16:29:22 至 17:00:14|0|
|同时含 eimemory 与 1.13.x|23|09-21 01:01:01 至 09-27 07:23:19|0|

上述词命中不是相关性标注，也不是 gold answer。抽查的新旧记录均为 completed-turn
标题，`time.occurred_at` 与 created_at/updated_at 相同；此字段不能独立证明正文事件的源时间。
最新一条 1.14.27 记忆以 `User: Delegated task:` 开始，是独立审查委派及其结果，
不是直接用户状态事实。原文中的 `system` 指 systemd 环境，不能仅据这个词判作系统通知。
本次证明了委派文本确实混在 conversation memory 中；尚不能将其与用户给出的
官方 RPC 四条结果逐 ID 对齐，也不能证明所有系统通知的 ingest 来源或具体排名。

有新记录存在，排除了“当前同范围完全没有新内容”的说法；尚未证明这些记录在当时的
PostgreSQL 索引中可见，或在官方对照调用的排序阶段被旧记录压过。
不按数据库更新时间直接修排序，不把历史标识当作最新状态正确性的证明。

## findings 与 quality-gap intake

生产 S 中 `eimemory.l5.quality_gap_intake` 记录为 **0**；关联该 observation 的 gap 也为 **0**。
因此当前不能宣称所属 scope 已正常登记。

源码：monitor 为已核验 off_topic 构造 finding；nightly `_run_semantic_relevance_monitor`
仅返回 finding 数量。后续 `ingest_quality_gate_reports` 根据自己的 scope 重新调用
`monitor_channel_deliveries`，缺省不启用跨用户 capture discovery，且不消费前一阶段的
finding 列表。生产 observation 持久化、函数返回 finding、所属范围 gap 登记是三件事。

现有本地 monitor/intake 测试覆盖同 scope 的创建与去重；这不证明 S 的生产 intake 已运行。
本轮没有运行 intake，也没有把 S 的 finding 注册到 operator 或其他用户下。
该缺口独立于已修查询扩展边界，本次仅报告，不扩大跨 scope 治理。

## 真实召回组与验收状态

以下冻结用户提供的官方 RPC 对照结果；本轮未重发 RPC，避免产生生产决策写入。
不将用户提供结果标作本轮独立复测，不以受控否定扩充自然数据。

|问法类别|已有结果|本轮结论|
|---|---|---|
|自然“验收要求”|2 memory + 2 persona，重复稳定无重复 ID|既有对照；完整重测未验证|
|“验收有哪些要求”|1 Codex 模型偏好 + 2 persona|潜在偏题；未证明与本次 context 误路由同因|
|项目当前进展|4 条旧 1.13.x 状态/系统委派通知，带 historical_only_latest_state_unverified|新旧/通知风险保留；具体排名根因未验证|
|虚构月球紫色长颈鹿项目|0 条|正确的受控否定；不计自然样本|
|已存自然事后样本|1 条 off_topic、4 条空投递 unknown|投递与标签吻合；只修首个证明的误入边界|

版本同步 1.14.28（未发布）：Python、pyproject、README、CHANGELOG、两个 Hermes manifest、
Codex plugin manifest。未提交、推送、部署、生产写入/重启/nightly、直接模型评估。

## 本地验证

- 红测试：原始 pytest 输出确认 2 failed / 2 passed。此前一次 RTK 参数误用退出 4，
  没有执行测试，不计入任何通过数；校正后保留原生红测试诊断。
- 查询扩展、主动召回、持久化安全、capture scope、项目状态路由、semantic monitor、
  授权 monitor scope：**162 passed，退出 0**。
- 版本一致性、同步路径不调用模型/投递真实性、quality-gap intake：**32 passed，退出 0**。
- 两组文件不重叠，共 **194 passed**；均为本地受控测试，不是生产业务验收。
- `git diff --check` 通过。报告和新增测试属于未跟踪本地文件，未提交。

---

## 本轮接续修复：统一证据矩阵与结论

本节是本轮最终结果；上文的生产取证、原 RED/GREEN 和原计数保留为前次记录，
并非本轮重新执行。接手时已有 1.14.28 版本材料、`proactive.py` 路由保护、
`test_proactive_context_route.py` 和本报告未提交。本轮保留全部这些修改。
本轮仅使用本地源码和临时测试数据库，没有读取新的生产正文、复制生产数据库、
运行生产 RPC/nightly、提交、推送、部署、写生产或重启。沿用 Ponytail full；没有子代理。
无法核实或切换宿主为 gpt-6-astra low，因此不声称使用了该模型配置。

### 统一证据矩阵（先区分阶段，再决定修复）

|症状|现有证据及确定边界|共享机制/缺陷|处置与证据上限|
|---|---|---|---|
|真实 off_topic 投递|上文精确 render digest、投递账本、已存标签吻合；原查询与扩展查询 task mode 不同|历史上下文跨查询扩展边界改变请求语义|保留既有 `_recall_query` guard；不证明能召回正确答案|
|真实 finding 无所属 scope gap|既有生产 S gap=0；nightly 只统计 findings；本地缓存 off_topic 重现 gap=0|监测器产出的带 owner 证据未交给持久化 intake|本轮 RED→GREEN，复用 intake，不重评缓存|
|普通 intake 渠道 finding 错放基础 scope|本地 base→Hermes 渠道 finding：created=1，但精确渠道 scope gap=0|同一持久化边界丢弃 detector owner，错误采用调用者 scope|本轮第二个 RED→GREEN；与 nightly 缺登记共同在现有 intake 边界修复|
|当前进展返回旧版本|前次当前记录有新内容，官方结果为旧记录；无当时完整 provider hits/逐项 scores/索引可见性|历史记录可召回不等于当前状态已证实；可能在候选、筛选或选择丢失新证据|已定位代码边界，未证实具体错误边界，不修改排序|
|委派/系统通知出现在结果|前次精确 scope 抽查委派文本；本地 `on_delegation→sync_turn` 证实合流|普通对话容器缺少足以判定当前状态权威性的来源区分|无四条结果逐 ID 对齐，不按 `system`/委派字串封杀历史，不猜修|

“阶段交接丢失证据含义”是这组问题的共同风险，不是已证明的单一代码根因。
查询语义污染和 gap owner 丢失是两个确认缺陷；不能把旧状态/通知自动归因给它们。

### 实际修改与安全边界

- 从 `ingest_quality_gate_reports` 原有持久化循环提取内部 `_ingest_verified_findings`，
  复用原 gap 创建、去重、supersedes 和 resolution 行为；没有新增治理队列或依赖。
- 只有本地 detector 刚验证过的 finding 可以携带 observation owner。
  nightly 消费本次 monitor 返回值；缓存观察也先重验 receipt、query、record、scope/source
  和 render digest，撤销或不匹配不生成 finding，不能直接扫描旧 observation 注册。
- 持久化使用 detector 的精确 scope；source_id 沿用现有 `_gap_record` 的 observation source。
  不向 operator 复制用户 gap。普通 request intake 仍不启用跨用户 capture discovery。
- 外部 reports 经 `_quality_finding` 规范化，不能注入 observation/scope；
  `recall_semantic:`/`recall_delivery:` 输入继续拒绝。LLM 只返回相关性标签，不能选择授权范围。
- 调度摘要增加创建/去重计数，不返回私密正文。异常只暴露异常类型；
  gap append 失败保留已存 observation，后续通过缓存重验重试，不再次调用模型。
- 此路径只登记 observation gap；不调用学习执行、自动晋升、修复或策略变更。
  既有 nightly 其他步骤不在本轮改动范围。unknown 不产生通过或自动关闭结论；
  事后评估与登记失败不更改已投递决策，不加入同步召回链路。

### 旧状态与通知：首个待取证边界

已追踪公共链路，而非针对单个问法改词：

1. `provider_core.on_delegation` 将 task/result 包装后调用 `sync_turn`，后者排队
   `adapter.sync_turn`。这证明一种委派进入普通对话的入口，不证明所有系统通知来源，
   也不能把委派执行结果一概视为无效历史证据。
2. `RetrievalEngine` 的 `search_scope_groups` 先由 candidate source 搜索，再按
   source rank/score 截断 provider limit。这是本次旧状态问题**最早尚未排除的候选边界**。
   缺生产同一调用的 provider hits 与新记录索引可见性，不能断言新记录进入过后续排序。
3. hydration 后执行 `is_task_evidence`，随后还有 hard filters、pollution gate、
   graph/view 处理、RRF pooling。`supports_task_evidence` 证明的是文本有状态断言，
   不是断言为最新事实；completed-turn 标题本身不算断言。不能把仅存汇总 drop 数还原为候选 ID。
4. `LightweightAdmission.select` 再检查 task evidence、片段属性、cosine 与覆盖度。
   普通“现在进展”按 score；“最近/最新/上次/latest/last/recent”分支已存在 occurred_at
   优先排序。后者不说明时间是正文事件源时间，且本轮没有增强 recency 或扩展该分支。
   未取得四条真实结果的片段、admission/score/去重 trace，不能确定首个排序/选择错误。
5. `RecallBundle.to_compact_dict` 对 task recall 已输出
   `historical_only_latest_state_unverified`；`render_loadout` 显示历史证据提示。
   保留历史可查和该提示，不删除/改写原记录，不将历史版本号或写入时间升级为当前状态。

尚需最小证据：同一授权调用中，新旧记录是否进入 provider bounded hits、被何项筛选丢弃、
片段是否为直接状态断言或委派通知、选择前后的分数与顺序，以及独立可信的状态源时间。
这些证据缺失时继续标记“未证实”，不新增自然质量成功样本。

### 本轮 RED → GREEN 与 focused 回归

修复前原生 pytest：
`tests/test_monitor_authorized_scopes.py -k 'registers_cached or channel_finding'`
得到 **2 failed / 26 deselected，退出 1**。分别断在 nightly 所属 scope gap=0、
普通 intake 所属渠道 scope gap=0；修复后同一命令 **2 passed / 26 deselected，退出 0**。

新增控制覆盖：缓存 off_topic 登记且幂等、operator 无副本、精确 source、无自动候选生成；
scope/source grant 撤销、record/receipt 撤销、query/render digest 改动阻止缓存登记；
外部伪造 observation 不能指定 scope；append 失败不改变投递且可从缓存重试。
原有 unknown 控制额外检查所属 scope/operator 均无 gap。

第一组（RTK 摘要，进程退出 0）：**210 passed**：

```sh
PATH="$PWD/.venv/bin:$PATH" /home/darrow/.local/bin/rtk pytest -q \
  tests/test_monitor_authorized_scopes.py tests/test_semantic_relevance_monitor.py \
  tests/test_quality_gap_intake.py tests/test_query_capture_scope.py \
  tests/test_proactive_context_route.py tests/test_task_recall_repair.py \
  tests/test_memory_core_v1_repair.py tests/test_recall_posthoc_quality.py tests/test_version.py
```

该组结束后，仅为既有 unknown 测试增加“无 gap”断言；第二组重新覆盖该文件。
本轮未跑全仓 pytest；受控测试只证明本地边界，不是生产业务验收。

第二组最初 **80 passed / 1 failed，退出 1**；原生单测复查确认
`test_original_multiline_host_query_is_retained_with_explicit_transform` 报
`original_query_input_unavailable`。本地进程继承了 capture scope allowlist，
而此测试只设置 capture 开关，未授权其合成 scope；这是测试环境隔离问题。
仅在测试子进程移除该变量（没有打印 allowlist 内容、没有改动任何生产环境/配置），
完整重跑第二组 **81 passed，退出 0**：

```sh
env -u EIMEMORY_CAPTURE_QUERY_SCOPES PATH="$PWD/.venv/bin:$PATH" \
  /home/darrow/.local/bin/rtk pytest -q \
  tests/test_monitor_authorized_scopes.py tests/test_project_status_route.py \
  tests/test_proactive_storage_security.py tests/test_proactive_capture_contract.py \
  tests/test_release_scope_binding.py tests/test_thin_recall_handoff.py
```

两组重叠 `test_monitor_authorized_scopes.py`，不把 210+81 当成互不重复样本。
最终 `git diff --check` 通过。原 context 路由补丁未改动，七处版本/发布材料保持 1.14.28。
新增报告和原 context-route 测试仍为未跟踪文件；所有修改均未提交。

### 父会话最小安全验收入口（本轮不执行）

1. 父会话审查本地 diff 和 focused 结果，沿已授权发布流程发布 1.14.28；本工作区仍未发布，
   本轮没有提交/推送/部署。发布验证需记录实际 release identity，不能沿用本地版本号冒充收据。
2. 在既有部署收据和 capture allowlist 明确授权的 tenant/scope/source 下，使用既有
   `_run_semantic_relevance_monitor(runtime, scope=operator_scope)` **事后步骤单独验收**，
   不为此运行整套 nightly。该入口会写 observation/gap，必须由有生产写授权的父会话执行。
   它会遍历既有 base channels 和允许的 capture scopes，不是“只操作 S”的入口；
   父会话须先确认全部目标均在本次授权内，不可临时放宽 allowlist。
3. 只读精确 S 验证已有 off_topic 观察所对应 gap 的 source_report 引用、query/release
   身份、精确 scope/source；重复执行应去重、缓存 provider_calls=0（前提是原观察仍有效），
   operator 无副本。若证据撤销、超出 512 决策窗口或不可验证，标记未完成，禁止强行登记。
   不为测试撤销而修改生产授权/记录，相关负例在本地已覆盖。
4. 由父会话执行已授权官方自然召回对照（这些 RPC 会写 decision，当前会话禁止执行）：
   两种验收要求、项目当前进展、显式历史问法及虚构项目控制。保存最少脱敏边界统计与结果类别，
   正文仅在授权范围内即时检查，不输出或写报告；核验宿主实际投递和答案。
   当前状态需独立状态证据，历史引用不得当作最新状态。新旧候选无法对齐时仍记未证实。
5. 复核真实自然样本的事后评估；空投递 unknown 不记绿色，虚构项目受控否定不充当自然样本。
   monitor/intake 登记通过不能替代召回质量通过。

**真实生产验收未完成。** 本轮已修确认的 intake 边界；旧状态/通知生产排名根因及
修复后自然召回质量仍未证实。版本保持未发布 1.14.28，七处版本/发布材料已同步。
