# 撤回记忆闭环复审与修复（2026-10-25）

## 范围与发布边界

本轮基于主线 `70e16b074478b025b160201a684951d98ea35fd6`，保留此前两项用户控制平面／网关提交刷新修正。用户报告 [Project validation 40154436287](https://github.com/darrowz/eimemory/actions/runs/40154436287) 的业务闭环失败，并明确禁止部署。本轮仅修复源码、回归测试和校验工作流；不创建 release tag、不发布 GitHub Release、不操作生产环境。

以下“撤回生效”指当前回答、召回和早报正文不再使用失效证据。历史权威记录与派生记录保留作审计，不通过物理删除历史来替代消费端校验。已经交付给外部调用方的副本不受本地存储事务控制。

## CI 失败定位

失败 job 为 `business-closure / 121052669060`，执行源码与上述基线一致。原始结果是 **3 failed、7481 passed、41 skipped、108 subtests passed**。

三个参数均属于 `test_withdrawal_removes_derived_digest_answer_context_and_daily_brief`，状态分别为 `deprecated / rejected / quarantined`。它们停在原文件第 63 行的撤回前正控制：固定查询 `2026-10-24` 的早报，期望研究摘要非空。CI 于 UTC 10 月 25 日创建来源与派生记录，正常日期窗口排除了这些未来于查询日期的记录。因此，该次日志未执行到撤回及撤回后断言，不能据此声称三个撤回后断言失败。

在未修改生产代码的精确基线，以普通 UID 1001 和固定 UTC 10 月 25 日时钟复现了原来的 3 个 `assert 0 >= 1`。修复使用函数作用域的共享时钟夹具，保留原业务断言与生产时间窗口；另增加 3 个跨 UTC 午夜用例，在两侧日期均先证明来源可见，再撤回并核对摘要、回答证据和早报的失效结果。

## 独立实测确认的业务缺陷

| 链路 | 基线实际行为 | 修复行为 |
| --- | --- | --- |
| Hermes add/remove 与普通记忆状态变更 → 早报 | 撤回内容仍进入会话摘要、新记忆、决策、跟进和 delivery 正文；墓碑也被当作新记忆 | 各正文区统一核验当前有效性，诊断保留状态计数但不复制被拒绝的标题或正文 |
| 当前／缓存记忆 → 回答上下文 | 普通记忆不属于研究质量门禁，可绕过撤回；旧 active 对象仍可使用 | 精确回读当前权威行，核对完整 envelope，拒绝撤回、改版、缺失或不可读取的记录 |
| 保存早报 → 回答上下文 | 保存内容没有原始输入引用，来源撤回后仍可使用旧正文 | 保存实际正文贡献者的完整 scope、source、kind、record ID 和版本摘要，消费时递归核验 |
| 原生 L1 / 同轮上下文 → 回答、召回、早报 | 生成时校验父源，读取时未重验；父撤回、移 scope、正文改版及 Runtime 重开后，子记录仍可使用 | 复用 L1 来源事件绑定及同轮父／支持消息合同，校验当前父源、支持行和已绑定内容 |
| 论文投影及 followup → 早报 | 摘要区拒绝的内容可从普通 memory 或跟进区再次出现 | 所有正文候选共用父源门禁；显式投影来源声明在各容器中必须一致 |
| 召回选择完成 → 最终输出 | 选择后另一 Runtime 撤回父源，旧派生正文仍可能随预构造 explanation 返回 | 在现有最终权威预算内再次校验；失败时清空 items、rules、reflections 和含旧正文的说明区 |
| OpenClaw 人格禁用 → 超时后的返回结果 | 人格明确禁用时，超时分支把已有禁用诊断换成空字典，调用方读取 `stored` 出错 | 通过纯内存 helper 保留 `stored=None`、`persona_disabled`；启用或构建失败后的超时仍维持原有行为，审计与 trace 写入继续跳过 |

原生派生回归使用 `AgentRuntimeMemoryService.sync_turn`、真实 SQLite 和持久化 rewrite；不伪造候选来源、不替换准入门禁，不调用外部模型。直接消费回归覆盖缺失父行、合法 scope 迁移和明确标注的存储损坏注入，未把仓库禁止的同 ID/source 冲突当作合法业务状态。

## 输入引用与兼容性

新早报采用 `daily_brief_sources.v1`，只记录实际正文贡献者，最多 2500 个引用；`content`、内层 `brief` 和 delivery 的正文副本保持一致。健康统计专用记录与被拒绝记录不进入来源集合。没有此引用声明的历史原生早报不能证明正文仍有效，当前消费拒绝；重新生成时只使用指定日期窗口内、当前仍有效的来源，不恢复历史时点已撤回的正文。保存的健康统计表示生成当时的统计结果。

共享读取检查在每次调用内使用同一只读快照、精确身份与读取视图缓存、最多 4096 次精确读取尝试及现有最终召回截止时间；失败尝试也计入预算，递归深度上限为 32。这是本检查器的预算，不是整个 API 所有 SQL 的总量承诺。查询错误不被当成“父源不存在”，缺失／冲突声明不被当作旧格式。校验不向记录添加临时 meta，不改变现有 envelope 版本摘要。

保留的正控制包括：合法 accepted 规则、同一查询范围内的不同 source 分区、继承的 `user_id=""` 记录、`hongtu_query_scopes()` 明确支持的别名范围、无持久化 paper parent 的既有手工文本提取合同。普通精确父源关系不会扩展到任意 scope 或来源。

复审另纠正新增检查器的两项兼容问题：普通旧记录沿用既有普通读取视图，L1 明确要求的来源事件时钟仍按严格视图核验；仅带分类标签、没有原生投影身份或显式来源引用的手工记忆保留原合同。两项均先在错误版本复现，再以原基线正控制和修正版验证。

同日及次日重复生成、保存和重开早报的独立探针未形成自引用或旧早报循环。仓库内 RPC、CLI 和 nightly 均重新调用早报 builder；保存早报的治理摘要只暴露统计，不输出历史正文。

## 回归与结果

阶段性红绿证据分别保存原始命令、UID、源码哈希、pytest 日志和 JUnit XML。以下最终结果只采用最后一次统一冻结运行，不将重叠阶段的通过数相加。

首轮冻结全仓回归发现 23 项失败（16101 passed、138 skipped、8836 subtests passed）。其中 4 项 OpenClaw 测试使用未入库的预取对象，新增权威回读正确判为缺失；另 19 项截止时间测试漏接新增模块的模拟时钟，真实时钟被拿来与模拟截止时间比较。分别独立复现后，仅修正持久化／回读准备和共享时钟绑定，保留原有断言、时间预算及拒绝分支。首轮失败日志、XML 和冻结源码与最终通过证据分开保留。

第二轮冻结全仓结果为 1 failed、16125 passed、138 skipped、8836 subtests passed。原 CI 的三个状态参数、本轮三个新文件的 40 项回归及新增的 3 项跨午夜回归全部通过；唯一失败为人格关闭时 `persona_trace["stored"]` 缺失。该问题在精确原基线与第二轮冻结源码均可确定性复现：真实 native recall 返回后将 host 时钟推进至默认 800 ms 预算之外，禁用参数失败，启用与 guidance 构建错误两项负控制仍通过。修复保留原断言和时间预算，只补全无需 I/O 的禁用诊断，并验证超时后未调用审计或 trace 写入口。该复现证明返回合同缺陷，不构成性能退化的统计结论。

第三轮冻结全仓结果为 1 failed、16128 passed、138 skipped、8836 subtests passed，撤回与人格超时的 46 项新增回归全部通过。唯一失败来自网关阶段测试把 Node 冷启动混入 1.5 秒请求期限：生产代码在剩余预算不足 1000 ms 时正确提前拒绝，此时尚无客户端阶段或验证 session。基线和当前脚本均可用 500 ms SDK 初始化延迟复现。修复仅调整测试准备：有界等待合成 SDK 就绪后再开始原 1.5 秒窗口，保留原 one-shot 模式、4 秒通信超时、900–1600 ms 阶段耗时与隐私断言；补充提前拒绝不创建客户端、不虚构阶段的负控。生产网关脚本与原基线字节一致。

最终统一运行使用 Python 3.12.14、pytest 9.1.1、xdist 3.8.0，普通 UID 1001；标准回归采用 4 workers，每组使用独立 basetemp。包入口从同一冻结 checkout 离线安装。原失败 CI 使用 Python 3.12.15，两者微版本差异明确保留，远端验收仍以目标提交的实际 CI 为准。

| 验证 | 通过的父测试 | 跳过 | 成功子测试 | 用时 |
| --- | ---: | ---: | ---: | ---: |
| 完整标准回归 | 16,130 | 138 | 8,836 | 1065.87 秒 |
| Luna bridge 独立套件 | 60 | 0 | 0 | 3.96 秒 |

两组均以退出码 0 完成，零 failures、零 errors。标准集共 16,268 个父测试；JUnit 的 tests 属性包含子测试，为 25,104，不将它当成独立父测试数。各组与阶段性定向回归有重叠，不相加报告。

主要选择命令如下；完整命令、普通用户身份、冻结 commit/tree、源码与日志哈希、原始 log/XML 随完整交付包提供：

```bash
python -B -m pytest --strict-markers -q -ra -n 4 --dist=loadfile -m 'not slow'
python -B -m pytest --strict-markers -q -ra deploy/luna_bridge/tests
```

3 个新增文件中的 **40 个父测试全部通过**。既有文件另新增 **7 个父测试**，仅统计下表明确列出的新增函数；其中包含既有撤回文件的 3 个跨 UTC 午夜参数用例、persona 新增超时参数回归，以及 gateway 新增的预算不足时创建客户端前拒绝用例。persona 原有 6 个测试与 gateway 原有 3 个测试参与完整回归，但不计入本轮新增。原失败的三个撤回状态参数及其跨日扩展都进入最终完整标准运行。

| 新增回归文件 | 父测试数 |
| --- | ---: |
| `tests/test_current_evidence_withdrawal_review4_20261025.py` | 14 |
| `tests/test_derived_memory_withdrawal_review4_20261025.py` | 8 |
| `tests/test_memory_withdrawal_closure_review4_20261025.py` | 18 |

| 既有文件 | 本轮新增函数 | 新增父测试数 |
| --- | --- | ---: |
| `tests/test_intake_withdrawal_review3_20261024.py` | `test_withdrawal_across_utc_midnight_gates_both_daily_brief_windows` | 3 |
| `tests/test_persona_closed_loop.py` | `test_openclaw_persona_timeout_preserves_disabled_contract_without_writes` | 3 |
| `tests/test_openclaw_gateway_bridge.py` | `test_gateway_rejects_insufficient_budget_before_creating_client` | 1 |

自动清点仅覆盖本轮冻结的 20 个变更文件、21,172 行和 868 个函数／类节点，其中 19 项语法解析通过、1 项为文档等不适用该解析器的格式。这不是整仓逐行人工审计。三个新增文件在真实 business-closure 命令中各登记一次。

标准运行排除已有 `slow` marker 用例；环境跳过项未计为通过，精确原因保存在 `final-validation-summary.json`。本轮不声称完成真实生产模型、外部平台交付、原生宿主或在线 PostgreSQL／向量环境验收。

## CI 与后续验收

三个新回归文件，以及补充的人格闭环和网关桥接文件，显式加入 `Project validation / business-closure`。保留操作者原先禁用的 `repository-regression` 条件，并在本轮独立 checkout 运行完整标准测试。新共享模块同时登记在 `memory.recall` 与 `memory.governance` 的 release-impact 范围内。

源码合并不代表部署已获准。本轮不验证真实生产模型、向量后端或宿主集成环境，不放宽部署门禁或验收阈值，继续遵守用户的禁止部署指令。目标提交的远端 CI 最终结果与补丁校验结果随交付证据提供。
