# 任务召回链路修复（本地，未发布）

基线：`1.13.4 / a379e22ea9f21d3ee52379360aefab56ec94be8d`。

本轮针对用户报告的“召回成功但答不到任务进度”和预算耗尽归零问题。没有修改生产配置，没有重启、部署或变更版本号。

## 修改范围

1. **任务路由**：原生 Hermes 工具未指定 task_type 时，明确的任务状态/历史查询分别使用 `task.status` / `task.history`；其他查询保留 `research.task`。统一意图分类同样根据查询识别 `task_recall`，兼容已有调用方继续发送 research 默认值。
2. **受控证据**：该路径只搜索 memory，允许符合任务证据形态的 task_context/conversation。保留精确 scope、source、active/陈旧状态、内容质量、污染过滤以及最终 authority 校验。不启用全局 operational/report 权限，也不通过关联回溯附带未经筛选的审计/原始记录。
3. **回答内容**：旧授权偏好和规则不能作为任务进度；任务状态/历史证据需有对应的状态或历史动作表述。此检查在最终片段筛选中同样执行，发生在 score-gap 裁剪前。它是保守的证据形态检查，不是事实正确性的完整语义证明。
4. **原生输出**：受控任务证据通过检索后，不再被 loadout 的 conversation/completed-turn 通用过滤再次丢掉。普通查询仍保留原有过滤。
5. **预算**：对于配置了截止时间的召回，默认/structured/fast 都执行收集截止。轻量召回仍使用 3 秒预算，其中最多 750 ms（预算的 25%）留给候选读取和最终筛选；候选读取再为最终筛选保留其中三分之一。截止后跳过范围扩展、图扩展、反馈统计和额外诊断检索。SQLite 候选收集/评分循环执行同一个截止。
6. **远端预算**：嵌入排队、请求、等待以及 PostgreSQL 连接排队、连接参数、状态读取和每条片段查询使用剩余预算。原配置更短的嵌入超时不会被放宽。调用方模型验证不能延长原有硬截止；工作线程仍受所有者管理并等待退出，不增加脱离请求的后台工作。
7. **诊断**：未验证候选的超时与实际 authority 拒绝分别统计。PostgreSQL SQLSTATE 57014 在请求预算截止附近按预算取消处理（考虑整数毫秒取整），不误触发服务不可用熔断。
8. **本地开销**：每个候选只解析一次 compact payload JSON；中文上下文边界预先建立区间并二分定位，避免同一个长中文段被每次命中重新扫描。投影候选缓存忽略本次调用的绝对截止时间，但仍保留查询、过滤、scope/source、watermark 和 authority revision 等失效边界。

## 验证证据

- 最终合并回归：**405 passed in 24.95s**。范围为 task_recall_repair、recall_intent、recall_engine、recall_quality_plan、recall_lexical、hermes_adapter、lightweight_evidence、recall_budget_reserve、recall_local_work、recall_fusion、source_partition、postgres_vector_source、recall_indexing、runtime_adapter_rpc、audit_recall_boundaries、two_stage_recall、caller_assistance 共 17 个测试文件。
- 独立只读复查发现的级联审计旁路、远端预算未传递和 SQL 预算取消误触发熔断均已补失败回归并修复；最后一轮定向复查没有剩余 Important 问题。
- 新增回归先复现失败，再验证修复：任务意图、任务证据隔离、原生 RPC 到 loadout、关联审计排除、所有召回模式的验证预留、SQLite 中途停止评分、超时标签、调用方模型不得延长截止、远端预算与 SQL 取消分类。
- 微基准：320 字重复中文段、两个命中词、20 次上下文扩展，基线 1017.736 ms，工作区实现 4.763 ms；固定种子 9 的 200 组中英混合输入与基线输出完全一致。仅为合成局部热点测量，不代表生产整体延迟。
- JSON 调用次数：6 个候选的同一测试由 12 次解析降为 6 次；没有移除 compact payload 摘要验证或最终权威读取。
- 新增两个合格任务候选加一个更高分旧偏好的筛选测试；采用既有默认 score gap 0.05。另有运行值为 0 时旧偏好不能挤掉合格任务证据的测试。

## 不能据此宣称的结果

- 没有真实生产候选标注，**没有调整/校准全局 max_score_gap、min_cosine 或 min_coverage**。报告中的运行值 0 与代码默认 0.05 必须区分；已向用户索取三个候选的脱敏内容/ID及相关性标注。
- 没有对生产数据或在线默认调用重新测延迟，也没有宣称 3 秒硬实时保证：同步 SQLite 调用、DNS/连接建立、驱动超时粒度及自定义 embedding provider 是否遵守超时契约，仍可能使单次调用超过协作式截止。超过最终截止的结果依然关闭返回，不能跳过权限校验兜底。
- 不对检索不到的记录宣称不存在，不保证全部任务历史已入库，不把旧 L1 unavailable 自动归因为此次 L0 原因。
- 未跑全量测试、真实 PostgreSQL 生命周期集成或 L5 完整闭环；本地通过不等于已上线验收。
