# eimemory 业务闭环审计 — 2026-09-12

审计基线：`73682bc0969c7abed5de89d4dfa943bcdeb7611e`，`master`，版本 `1.13.10`。

本文保留修复前的审计证据；同日的局部修复记录见文末“修复跟进”。历史 `repro_*.py` 断言缺陷存在，修复后的验收使用新增回归测试。

结论：当前源码的主流程和大量正常路径测试已经存在，但业务闭环仍有可执行反例，不能据此认定“持久记忆可靠积累、失败驱动回滚、重试不重复学习”已经成立。确认 **10 项问题：4 项 P1、6 项 P2**。优先修复共享记忆写隔离、错误替换、失败样本统计及原始结果与能力证据的一致性。

本次只审计源码并添加审计材料，没有修改产品实现、提交、推送或部署。复现使用临时数据和模拟业务内容；结论说明源码能够出现这些错误，不表示线上已经发生。未连接生产主机，未进行 PostgreSQL、真实宿主或 systemd 的线上验收。

## 覆盖的业务链路

| 链路 | 本次验证 | 发现 |
| --- | --- | --- |
| 记住 → 持久化 → 重启 → 召回 | 真实 Runtime / RPC 桥，写入和重启后查询 | F01 |
| 共享读取 → 用户更新/删除 → 所有读者状态 | 真实 RPC mutation，重启后 exact-scope 查询 | F02 |
| 任务结果 → 观察 → 晋升/回滚 | 临时策略、归因事件及成功/失败结果 | F03 |
| 原始结果 → 能力 observation | 同一 trace 重试及持久数据对照 | F04 |
| 结果重试 → 奖励 → replay/policy | CLI 相同业务调用链重复两次 | F05 |
| Codex 工具执行 → 采集 | 同轮不同 tool_call_id 的真实适配器回调 | F06 |
| Hermes 召回 → 注入 → 使用反馈 | 同会话不同真实轮重复问题，第二轮明确引用记忆 | F07 |
| L0 → L1 抽取 → worker 恢复 | 子进程领取任务后退出，重开队列 | F08 |
| 候选审查 → 论文晋升 | 候选先废弃，再通过公开 API 晋升 | F09 |
| 论文处理 → 定时汇总 → 错误可见性 | 注入单候选异常，比较原始批次与定时任务报告 | F10 |

## P1：应优先修复

### F01：默认标题使无关的持久记忆互相替换

位置：`eimemory/adapters/runtime/service.py:346`；`eimemory/api/memory.py:251-252,279-280,304`。

`adapter.remember` 的 `title` 可省略，但服务将它统一填成 `Codex long-term memory` 等固定标题。持久记忆的默认 semantic key 又只由 `memory_type:title` 生成，因此同 channel、scope、source 下后存入的无关事实会将前一事实标为 `superseded`。

复现：先保存“项目使用 PostgreSQL”，确认可以召回；再保存“部署需要负责人批准”。前一条随即无法召回，重启后仍为 `superseded`。两次请求使用不同 event ID，并非同一请求的重试。

影响：正常记忆积累变成同类记忆互相淘汰，用户得到成功响应却丢失旧事实的召回能力。原始记录没有被物理删除。

修复方向：展示标题与事实身份分离；无明确相同事实身份时不得自动 supersede。补充两条无关事实共存、同事实明确纠正、重启后召回的验收。

证据：`business-closure-2026-09-12/repro_memory.py` 的 `default_title_collision()`。

### F02：Hermes 用户作用域可以删除共享作用域记录

位置：`eimemory/adapters/runtime/service.py:848-858,883-895`；`eimemory/storage/sqlite_store.py:5613-5615`。

目标查找沿用共享读取规则：Alice 的 scope 可以读取 `user_id=''` 的共享记忆。随后 mutation 校验只检查 channel、source、target，未验证目标的完整物理 scope 与调用方相同。

复现：在共享 scope 添加 Hermes 记忆，以 Alice scope 携带目标 ID 和有效 revision 删除；RPC 返回成功。重启后共享记录确实为 `removed`，影响所有共享读者。

这不是对任意租户越权的结论。已证实范围是同共享命名空间下的用户写隔离。源码契约 `api/memory.py:299-301` 明确区分共享读取与写权限，并要求写入双方 scope 相同；`tests/test_l1_capture_idempotency.py:138` 也测试了同一原则。

修复方向：ID 与旧文本两条目标解析路径均校验 exact scope/source，再执行 mutation。共享读取权限不能充当共享修改权限。

证据：`repro_memory.py` 的 `shared_scope_mutation()`。

### F03：失败任务被排除出观察样本，错误策略仍可晋升

位置：`eimemory/governance/outcome_evidence.py:29-40`；`eimemory/governance/promotion_watch.py:60-62,1146-1147`。

证据分类将 `verifier.passed is True` 作为生产样本资格，因而 `passed=False` 的真实失败被归为 unverified，尚未进入失败计数就被观察器排除。这里的 `passed` 表示任务结果通过，不是回执身份是否可信：`capabilities/observations.py:186` 直接将它转换为 pass/fail；宿主结果适配器也按任务成功状态填写该字段。

复现：对同一观察中的策略交替提交 3 个失败和 3 个成功，均有事件与策略归因。这 6 个模拟任务输入的失败比例为 50%，观察器却显示 `observed_count=3, failure_count=0, failure_rate=0.0`，策略成为 `active`。

影响：失败反馈无法触发对应观察回滚，并污染晋升判断。

修复方向：分离“证据来源经过验证”与“任务结果成功”。可信失败应进入分母和失败计数；replay/unverified 仍保持隔离。验收必须混合成功、失败、重复和未验证结果。

证据：`business-closure-2026-09-12/repro_learning.py`。

### F04：同一结果重试可让能力 observation 与原始结果矛盾

位置：`eimemory/experience/outcome.py:92-102,105-112`。

原始 outcome 去重后返回已经存储的 `stored.record_id`，但后续能力归一化仍使用本次请求的 `build.payload`，没有以该 ID 对应的持久原始结果作为依据，也没有拒绝同一 trace 的不同请求内容。

复现：第一次记录失败 trace、没有 capability attribution；用同一 trace ID 重试，改为成功，并补齐有效能力 binding、显式 attribution 和独立 verifier。原始 trace 仍为失败，却新建了 `verdict=pass` 的 observation，回执报告 `capability_dual_write=aligned`。

影响：重试不再是同一业务事实的重放，能力证据可以与其引用的原始结果不一致。

修复方向：重复请求必须比较内容摘要，或只从已存储的权威 payload 构建投影。确需修订结果时走显式版本变更，并保留原始证据和修订关系。

证据：`repro_learning.py`。

## P2：闭环可靠性与运行可见性

### F05：同一个 outcome 重试会重复奖励或惩罚

位置：`eimemory/cli/main.py:1132-1136`；`eimemory/governance/closed_loop.py:257-283`。

CLI 对 `idempotent=True` 的结果仍调用 `post_experience_hook`，后续 replay transition 与 RL policy 更新没有按原始业务事件去重。相同 trace 提交两次，原始记录数为 1，RL transition 数却为 2；同一 success action 的 value 从 `0.25` 增至 `0.5`。

影响：CLI 补跑、脚本重试或重复导入都可能重复学习，奖励值偏离独立业务事件数。

修复方向：把“反馈已应用”的记录与奖励、transition 的修改绑定为可恢复的幂等步骤。仅在 CLI 看见 idempotent 就跳过还不足以处理原始记录成功但反馈未完成的崩溃窗口。

证据：`repro_learning.py`。

### F06：Codex 同轮第二个工具调用的内容被错误去重

位置：`eimemory/adapters/codex/hook.py:334,377`；`eimemory/adapters/runtime/service.py:335-341`。

工具采集的 turn ID 固定为宿主 `<turn_id>:tool`，没有纳入 `tool_call_id`。同一轮先 Read、后 Bash，输入输出不同，第二次采集却返回第一次记录及 `idempotent=true`。

影响：后续工具结果不会成为独立的持久采集内容，后续追溯和抽取缺少证据。此结论针对 sync_turn 采集记录，不表示所有工具回执均丢失。

修复方向：用宿主 session、turn、tool_call_id 构建采集事件身份；同工具重试保持幂等，不同工具调用保持独立。

证据：`business-closure-2026-09-12/repro_adapters.py` 的 `codex_collision()`。

### F07：Hermes 不同轮重复提问会复用已结束的召回决策

位置：`eimemory/adapters/hermes/provider_core.py:926-928`；`eimemory/retrieval/proactive.py:281-298`。

`decision_turn_id` 只由 session 和 query 生成。相同问题在新宿主轮出现时，服务端重放先前 decision。第一轮未使用 citation 并结束；第二轮明确引用 citation，却仍得到同一个 decision ID，ACK `changed=0`，反馈与终结也都 `changed=0`。

影响：新一轮真实注入与使用行为未形成独立反馈，使用率和终态证据遗漏。无需假设事实更新或缓存过期，就能复现这个身份冲突。

修复方向：引入真实宿主 turn 身份；预取阶段尚无 turn ID 时分配并传递一次预取/轮次身份，仅同轮重试复用。

证据：`repro_adapters.py` 的 `hermes_collision()`。

### F08：L1 worker 崩溃后任务永久停在 running

位置：`eimemory/knowledge/l1_queue.py:128-137`。

领取任务会持久化 `running`，后续只领取 `queued`，没有租约回收或重启恢复。独立子进程领取后退出，再打开队列得到 `processed=0, pending=1, dead=0`；再次 enqueue 同 episode 仍返回原 `running` 任务。

影响：已有 L0 记录的对应 L1 抽取永久悬挂，既不重试，也不到 dead-letter。

修复方向：加入有所有者标识的有界租约、超时回收和幂等处理；重启不能无条件抢走仍在运行的其他 worker 的任务。

证据：`repro_memory.py` 的 `queue_crash_recovery()`。退出码 17 是故障注入，非运行环境错误。

### F09：已废弃的论文候选仍可被重新晋升

位置：`eimemory/intake/pipeline.py:208-223`；CLI 入口 `eimemory/cli/main.py:2545-2552`。

论文晋升只排除 rejected/quarantined，没有按候选状态机限制为 candidate/reviewed。先通过正式审查 API 将候选置为 `deprecated`，再调用 paper promote，仍返回成功、编译出 2 条知识记录，并将候选改回 `promoted`。审查展示逻辑 `intake/review.py:377-385` 原本判断该终态不可晋升。

影响：人工废弃决定不能约束另一条晋升入口，废弃内容重新进入知识链路。

修复方向：所有晋升入口共享状态白名单并在写入边界重验；对已晋升对象的重试只返回既有结果。

证据：`business-closure-2026-09-12/repro_intake.py`。

### F10：定时论文晋升汇总丢弃真实错误

位置：`eimemory/scheduler/jobs.py:1337-1344`。

底层批次已隔离异常，并返回 `error_count=1` 和错误列表；定时包装却将这两项固定重写为 `0`、`[]`，同时沿用批次的 `ok=True`。复现中唯一候选处理失败、晋升数为 0，定时报告依然表示成功且无错误。

影响：操作者和依赖该汇总的检查无法区分正常跳过与执行故障，失败恢复缺少明确入口。报告中的 reason 仍可能出现 `promotion_exception`，并非所有异常痕迹都被删除。

修复方向：保留逐候选错误和计数，明确 partial success/failed 的聚合语义；断言全部失败和部分成功两种情况。

证据：`repro_intake.py`。

## 验证记录与范围

复现材料保存在 `docs/audit/business-closure-2026-09-12/`。从仓库根目录执行：

```powershell
python docs/audit/business-closure-2026-09-12/repro_memory.py
python docs/audit/business-closure-2026-09-12/repro_adapters.py
python docs/audit/business-closure-2026-09-12/repro_intake.py
python docs/audit/business-closure-2026-09-12/repro_learning.py
```

脚本断言的是本次基线上的错误行为，退出 0 表示反例成立，不表示业务验收通过。修复后应把相应预期改写为正式回归测试，不能把这些反例断言当成修复后的验收标准。RPC 复现使用进程内公开分发器，不覆盖 HTTP 传输鉴权。所有 runtime 数据均放在 `TemporaryDirectory` 中，策略、回执和 release identity 都是隔离的测试数据。

运行环境：Windows，Python 3.14.3，pytest 9.0.3。

主审独立重跑了上述全部 4 个脚本，10 项核心反例全部成立，脚本退出码均为 0；原始结果保存在同目录 `verification.json`。工作区新增的仅是本报告及其复现材料，产品实现未改动。

| 已有目标测试批次 | 结果 |
| --- | --- |
| minimal_closed_loop、intake_pipeline、knowledge_ingest、knowledge_refresh | 42 passed |
| intake_review、runtime_collect_aggregation、governance_intake_loop、paper_intake、knowledge_projectors、knowledge_compiler | 43 passed |
| memory_plane、tencent_alignment、memory_core_v1_repair、memory_projection_authority | 66 passed |
| 选定 Hermes/RPC mutation 测试、共享读写权限契约测试 | 5 + 1 passed |
| minimal_closed_loop、promotion_watch、experience_outcome、capability_storage_v3 | 93 passed |
| 4 个选定 Codex/Hermes 生命周期测试及 runtime_adapter_rpc 全文件 | 36 passed，2 failed |

这些批次有交集，不相加声称独立测试总数。最后一批两项失败均为 Windows 访问 `127.0.0.1:1` 返回 timeout，测试硬编码期待 connection_error；未计入本报告业务缺陷，也没有把测试结果报为全绿。没有运行全量测试或将历史审计的通过数量当作本次验证结果。

补充接口风险：`repro_learning.py` 还验证了 `normalize_explicit_capability_outcomes()` 在正常 outcome 初次写入后重建会得到 `CapabilityIdempotencyConflict`。初次路径给 attribution provenance 加入原始 trace ID，重建路径没有加入，导致相同 request key 对应不同观测身份。未找到该函数的生产调用，故不把它计入上述 10 项闭环缺陷，也不声称它会阻断自主循环；定位为 `eimemory/governance/capability_attribution.py:126-132` 与 `eimemory/experience/outcome.py:151-156`。

建议修复顺序：先处理 F02/F01 的记忆权限与丢失，再处理 F03/F04/F05 的证据和奖励一致性，随后处理 F06/F07/F08 的采集、反馈和恢复，最后统一 F09/F10 的候选状态和运行报告。每组修复以对应反例转成回归验收为完成条件。

## 修复跟进 — 2026-09-12

按最小范围修复现有调用点，共涉及 10 个产品实现文件；没有数据库 schema 迁移、版本升级、提交或部署。

| 问题 | 修复 |
| --- | --- |
| F01 | 默认展示标题的记忆使用内容稳定键；明确标题或 semantic key 的纠正语义保留 |
| F02 | Hermes 按 ID 和旧文本修改均校验精确 scope/source |
| F03 | 纳入带执行证据的可信负面 verifier；继续排除裸 False、未执行验证、回放和未验证来源 |
| F04 | observation 始终来自已持久化原文；补偿归一化使用相同 provenance |
| F05 | 原始记录、作用域、action 形成处理身份；transition、奖励值和回执一次原子提交，重试返回既有回执 |
| F06 | Codex 采集事件纳入 tool_call_id；宿主 turn 仍用于回执归因 |
| F07 | Hermes 复用既有待处理表保存一次轮次身份；同轮重试复用，终结后重复问题生成新身份 |
| F08 | 独立消费者进程锁保护执行，崩溃后回收 running；领取 token 防止旧处理结果回写 |
| F09 | 包括旧字典在内的候选引用重读持久状态；终态不能晋升 |
| F10 | 批次和定时汇总保留错误详情与数量；部分/全部执行失败均返回 ok=False，并保留成功数量 |

F03 验证说明：最初审计脚本中的裸 `passed=False` 本身不足以区分任务失败与验证未执行，不应把开放该输入作为修复标准。正式回归调用现有 OpenClaw payload builder，使用带 method、evidence_refs 和执行 checks 的失败，验证其计入观察并触发回滚，同时保留未验证输入的拒绝契约。

修复验收命令（仓库根目录）：

```powershell
python -m pytest -q tests/test_memory_closure_regressions.py tests/test_learning_closure_regressions.py tests/test_adapter_turn_identity.py tests/test_intake_closure_regressions.py
python -m compileall -q eimemory
git diff --check
```

直接相关既有测试按批次验证通过：记忆/队列 98 passed（2 个已知 Windows HTTP 分类差异用例 deselected）、适配器 65 passed、学习与观察 151 passed、候选与定时任务 51 passed。批次有交集，不合计为独立总数；候选字典补洞后另有 23 passed，失败证据边界补充后相关 156 项通过。

最终整合：四个新增回归文件共 **58 passed，5.49 秒**；`compileall` 和 `git diff --check` 均通过。覆盖可信失败与未执行验证的区分、原始证据一致性、重复及并发重试、原子写故障恢复、队列真实进程崩溃与活跃消费者，以及旧候选字典的状态绕过。

部署与数据边界：同一 L1 队列的消费者改为串行，入队仍独立；升级前应停止不识别新 consumer lock 的旧版本 worker。未自动复活历史 superseded 记忆或清理历史重复奖励，避免把合法纠正与错误数据混为一谈。本地源码验证不等同于生产验收。
