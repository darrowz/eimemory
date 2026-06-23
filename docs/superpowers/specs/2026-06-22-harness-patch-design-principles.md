# Harness Patch Design Principles

> **一句话：以后鸿途进化不能靠 "我下次注意"，要靠失败轨迹 → 小补丁 → 回归门 → 版本升级。**

## 1. 设计哲学

Self-Harness 把 Agent 的 harness（系统提示、工具暴露、验证规则、运行策略、故障恢复）当成**可版本化的外部资产**：

- **不靠模型权重**：模型固定，靠改外层协议提升能力
- **不靠"我下次注意"**：每次升级必带证据，失败轨迹驱动
- **不靠单次判断**：必须 held-in / held-out 双重回归门

eimemory 的自进化 = 把每一次"经验沉淀"升级为**可审计、可回滚的 harness patch**，跨 Agent（eibrain / OpenClaw / MCP consumer）共享。

**与现有架构的对齐**：eimemory 不直接改 eibrain 的 prompt——它产出 patch，patch 通过 recall 注入到下游 Agent 的运行时。这就是 eimemory 的"harness layer"。

## 2. 四条核心原则

### 2.1 Harness = 版本化资产

- 每次 candidate 必带 `proposal_card`：失败证据 + 目标失败模式 + 修改表面 + 回归结果
- 跟 `capability_ledger` 一起永久归档
- 任何 candidate promotion 必须可回滚（按 `authority_tier` 决定自动 vs 人工）
- candidate 之间不直接覆盖——后一个必须显式 supersede 前一个

### 2.2 失败轨迹 → 行为机制聚类

不按"症状"聚类（"忘了写文件"），按"行为机制"聚类（"create → edit → verify" 序列）。

反复出现的弱点模式：

| 弱点 | 根因 | 应对 surface |
|---|---|---|
| 漏写工件 | 没先建空文件、跑完直接声称完成 | `VERIFICATION_GUIDANCE` |
| 重复坏命令 | 工具错误后不读 stderr、再次重试同一命令 | `TOOL_LOOP_GUARD` |
| 工具错误后不恢复 | 失败吃掉，下一步依赖损坏中间态 | `ARTIFACT_RECOVERY` |
| 探索太久不实现 | 没设探索轮次上限 | `RUNTIME_POLICY` |
| 跨 session 丢失环境 | PATH / 安装状态未持久化 | `RUNTIME_POLICY` |

同一行为机制的不同 task 必须聚成同一个 candidate 群组，避免重复打补丁。

### 2.3 小步 harness 修改

- 单次编辑越小，越能定位因果
- candidate 必带 `diff_size`（line count + token count）
- 超过阈值强制拆成多次小步提交
- 同一 surface 同一时刻只允许一个 active candidate（避免互相干扰）

### 2.4 回归门（AND 三条）

candidate 升级必须 **同时** 满足：

- held-in **不退步**
- held-out **不退步**
- **至少一个** split 提升

只在一个 split 涨、另一个跌 → reject。听起来合理但 score 高 → reject。**自我过拟合是头号风险。**

## 3. Harness Surface 分类

candidate 修改的"可编辑表面"必须显式标注：

| Surface | 含义 | 回归度量 |
|---|---|---|
| `INSTRUCTION` | 系统提示、工具路由 prompt | 召回命中率 |
| `VERIFICATION_GUIDANCE` | 验证器要求、产物检查清单 | 验证器通过率 |
| `TOOL_LOOP_GUARD` | 工具失败重试上限、死循环检测 | 工具调用次数、重试率 |
| `ARTIFACT_RECOVERY` | 工件丢失恢复、跨 session 状态 | 工件完整性 |
| `RUNTIME_POLICY` | 超时、权限、回滚策略 | 端到端成功率 |

新 surface 引入必须先在 capability_ledger 里登记，禁止未登记的"野生"修改。

## 4. 风险门（L0-L4）

pass rate 不是唯一门。对真实账户 / 生产资源 / 不可逆操作必须加更严的安全门：

| Tier | 自动升级 | 必备证据 |
|---|---|---|
| **L0** 观察 | ✅ | 失败证据 + 回归门 |
| **L1** 本地软件 | ✅ | L0 + replay 100% + scope 匹配 |
| **L2** 业务变更 | ⚠️ gated rollout | L1 + health + canary + rollback 验证 + 审计 |
| **L3** 真实账户 / 隐私 | ❌ 人工 | L2 + 人工审计 + 对抗测试 |
| **L4** 物理 / 财务 / 医疗 / 法律 | ❌ 多签 | L3 + 多签 + 业务审批 |

L3/L4 不允许 pass-rate-only 决策，必须叠加安全闸（`kill_switch` / `circuit_breaker` / `spend_guard` / `audit_verifier`）。

## 5. 落地路径（与现有 governance 模块对齐）

| 原则 | 当前实现 | 还需补 |
|---|---|---|
| 版本化资产 | `capability_ledger.py` | 强约束 `proposal_card` 必填，否则 promotion 拒 |
| 失败轨迹 → 证据 | `evidence_collector.py` | 加 `behavior_sequence_hash` 字段 |
| 行为机制聚类 | `capability_attribution.py` | 按动作序列聚类，不只按失败标签 |
| 小步修改 | `candidate_search.py`（`repeat_threshold=2`） | 加 `diff_size` 阈值校验 + 同 surface 单活 |
| 回归门 | `regression_watch.py`（`regression<0.9`） | 升级为 `held-in AND held-out AND delta≥0` 硬门 |
| Surface 分类 | ❌ | 引入 `HarnessSurface` 枚举 + candidate 必填 |
| Scope 隔离 | `code_evolution.py` 有（allowed file scope） | 推广到所有 L1 candidate（task_type / agent / surface 维度） |
| 按 agent 隔离 | ❌ | candidate 加 `target_agent` 字段，按 agent 隔离 acceptance eval |
| 风险门 | `governance/safety/`（kill switch / circuit breaker / spend guard） | 串到 promotion 路径，L3+ 强制叠加 |

## 6. 一句话总结

> **鸿途进化 = 失败轨迹 → 小补丁 → 回归门 → 版本升级**。
>
> 不靠"我下次注意"，靠 `proposal_card` + `capability_ledger` + held-in/held-out 双重回归 + 风险门。
>
> Self-Harness 改的不是 Agent 的"想法"，是 Agent 的"操作系统"。

## 7. 参考

- Self-Harness 论文：把 Agent 的 harness 视为可迭代的外部状态，固定模型、只改 harness，用回归测试决定是否升级
- 实验：Terminal-Bench-2.0，三个模型（M2.5 / Qwen3.5-35B-A3B / GLM-5）通过率 +20%~+138%
- 核心观察：不同模型保留下来的 harness 修改明显不同——M2.5 修工具循环、Qwen 修工件恢复、GLM 修环境丢失。证明有效 harness 优化必须**针对目标 Agent 的具体弱点**生成 patch
