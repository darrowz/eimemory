# 故障检测、分类、登记和修复转交：审计结论

基线 3a87bf3b8d464e64fda5025fbf05b4df772d0b75；2026-09-28。

## 事实边界

用户报告了 nightly/timer-monitor 失败、incident 记录器 evidence_waiting 与空 ID，以及摘要
release_closure_report_contract_invalid。本次读到最新 GitHub 代码，但没有取得该次现场原始 JSON、
原始日志或数据库。Library 查找只有此前审计材料，无该次原始报告。
因此不能认定现场唯一根因，也不能把新工具的重现当成生产已修复。

## 确认并修复

1. 原摘要把所有未获 admission 的报告标为 contract-invalid。该错误码也可能是摘要误报，
   而不是报告真的损坏。共享 closure_verdict 区分有效阻断、格式/成功声明无效、已验证等待与完成。
2. 原 detector 的 `not (reasons & _NON_ACTIONABLE_REASONS)` 让任何一个等待理由否决全部故障登记，
   且 data_accumulating 可直接压掉错误。改为检查控制面子报告，硬错误优先，扫描有界；业务文本不被当作控制信号。
3. bootstrap_pending_non_recall_l5_evidence_incomplete 等汇总码不能证明仅缺样本。
   无具体错误时登记 diagnosis_required，保留非空 incident ID，但不把猜测变成自动修复授权。
4. 原 installer 把原文和摘要分别送给不同消费者，记录器收不到摘要校验结果，之后无条件删掉原文。
   改成一次读取原始字节、私有归档、共享判定、同快照登记；把进程退出码/commit/scope/attempt 纳入上下文。
   真正已验证的渠道等待可接纳生产者的预期 exit 1；crash/格式错误/成功声明与非零退出矛盾不被抹掉。
5. 原随机 ID + 查询去重有竞争和身份归属风险。新身份绑定 commit、receipt、session、scope、attempt、
   规范报告摘要及原始字节摘要，使用现有 RuntimeStore.append(existing_match=...) 的事务内精确查重。
   原始字节摘要只作关联，不是签名或授权。旧 incident 不自动改名、撤销、解决或删除。
6. 原内部闭环修复类被要求修改 receipt builder；报告问题不能为满足这一旧门禁而改错文件。
   新注册窄报告类/计划，旧 receipt builder 的语义门禁原样保留；机器授权仍需有效且匹配精确 incident。
7. 原 router 把 proposal_blocked、submission_failed 及 evolution.ok=false 汇总成成功。
   现在返回 blocked，已到提案/转交阶段的结果写独立 repair-attempt，关联 incident_record_id 和 transaction_id。
   授权不匹配不再冒充 idle；查询做精确 scope 过滤。未进行生产策略签发、模型调用或部署。
8. 更新发布影响映射、策略签发器的计划发现和旧测试合同。源码与测试不会用“等待”绕过回放、
   渠道回执、真实业务样本、receipt 或 lineage 的要求。

## 不表示什么

本补丁修正故障链的可观察与转交语义，不实现无证据的自动解决。report_contract_ok 与 admission_ok
不是模型行为证明；修复提案成功不是 transaction 完成；本次没有构造 terminal success 或真实业务证据。
有明确失败的报告保持失败，未解释清楚的聚合状态保持待诊断。策略未启用、已消耗、过期、摘要不匹配、
仓库/部署身份不匹配时仍阻断，不自动放开权限。

## 源码依据（固定提交）

- eimemory/ops/release_closure_failure.py：等待 reason 任意命中即 suppress。
- deploy/summarize_release_closure.py：未获 admission 即 contract_error。
- deploy/install_immutable_release.sh：_run_post_switch_closure 中分开读取与无条件删除。
- deploy/record_release_closure_incident.py：旧记录器仅接原始报告。
- eimemory/governance/evolution/system_code_repair.py：无匹配 idle；失败 processed 汇总 ok=true。
- code_evolution_test_plans.py / code_evolution_semantic_validation.py：旧内部闭环类的 receipt builder 专项规则。
- eimemory/storage/runtime_store.py：existing_match 对应 BEGIN IMMEDIATE 内的精确身份查重。

7 个完整修改原文件保存在 verified_sources 并以当前 Git blob 核对；另 4 个大文件的修改是固定 blob
加真实获取的唯一原文片段。没有将拼装大文件标作原始仓库文件。

## 实际验证

124 个聚焦测试通过；1 个既有完整 Runtime 用例明确未选入。真实文件、FIFO、POSIX 权限、
SQLite 事务/唯一性和并发重试已执行；领域模型与 provider/policy/ledger 是显式验证替身。
4 个 shell 场景执行真实修改后的函数和 CLI，闭环生产者是隔离假程序。
13 项应用/回退/防误改检查在完整已验证小模块加真实片段构成的临时 Git 夹具上执行，不是完整 checkout。
5 项原始/修复语义对照中，原版 4 项偏离预期，修复版均符合；保留纯等待正例。

未执行：完整项目、3 个新 Runtime 用例、1 个既有 Runtime 幂等用例、真实部署/systemd、
现场 incident→授权→provider→transaction→新 release 验收链，以及跨平台/多解释器矩阵。
