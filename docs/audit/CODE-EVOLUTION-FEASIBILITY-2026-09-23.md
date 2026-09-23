# 代码自演化机制可行性与可移植性评估

- **评估日期**：2026-09-23
- **代码基线**：`6b5026a` / 1.13.18
- **评估对象**：代码自演化平面（`code_evolution_*`、`code_automation_policy`、`code_implementation_*`、`system_code_repair`，合计约 6,905 行）
- **方法**：逐模块读码还原链路 → 定位全部闸门与边界常量 → 评估可行性 → 交叉验证第四轮可移植性审计结论

---

## 一、机制形式还原

### 1.1 定性

这是一个**受保护的、声明式补丁提案驱动的窄域闭环自修复系统**，而非通用自演化系统。

三个限定词都需要强调：

- **声明式**：模型只输出「整文件替换内容」，不输出任何可执行指令。提案 schema 为 `code_implementation_proposal.v2`，强制 `proposal_only=True`，并显式拒绝 `argv` / `command` / `cwd` / `env` / `git` / `shell` / `secret` / `token` 等键（`code_evolution_transaction.py:58-71`）。
- **窄域**：仅覆盖 4 类已知事件、4 个生产文件（见 §2.1）。
- **闭环**：从检测到沉淀全程自动化，含自动回滚。

### 1.2 完整链路

**① 事件检测**

4 个检测器产出 `reflection` 记录：

| 检测器 | incident_class | 位点 |
|---|---|---|
| `eimemory.runtime_identity_drift.v1` | `deployment.runtime_commit_drift` | `ops/runtime_identity_drift.py:16-17` |
| `eimemory.release_closure_failure.v1` | `release.closure_internal_failure` | `ops/release_closure_failure.py:13-14` |
| `eimemory.code_maintenance.v1` | `code.incident_routing_stale` | `governance/code_maintenance.py:34-35` |
| `eimemory.system_code_repair_failure.v1` | `code.system_repair_policy_stale` | `ops/system_code_repair_failure.py:20-21` |

**② 闸门 1 · 事件可信性**（`system_code_repair.py:273-315` `_trusted_incident`）

必须同时满足：`status=active`、`provenance.origin=system_detector`、`known_before_detection=False`、`prior_user_reported=False`、`meta.observation_valid=True`、`incident_class ∈ _ROUTES`、`incident_digest` 一致。

设计意图明确：**只修复系统自己检测到的问题，用户上报的问题不进入自动修复**。这是正确的安全取舍。

**③ 闸门 2 · 补丁提案与禁权校验**（`adapters/hermes/code_implementation.py`）

- 传输：Unix domain socket `/var/lib/eimemory/run/hermes-code-implementation.v2.sock`
- 对端校验：`SO_PEERCRED` 要求 peer uid == `os.geteuid()`（`:1011-1016`，Linux 专有）
- 生成：宿主 agent 的 `llm.complete_structured(task="eimemory_code_implementation", temperature=0.0, max_tokens=8192, timeout=120s)`，带 JSON schema（`:1070-1085`）
- **模型不写死**——使用宿主 agent 配置的模型。这一点对可移植性是有利的。

补丁格式为**整文件替换**，每条 `file_updates` 严格为 `{path, prior_sha256, content}` 三键（`:126`）。`prior_sha256` 提供乐观并发控制：若文件已被改动则拒绝。

新增行经正则拦截（`:414`）：`subprocess` / `os.system` / `os.popen` / `pty.spawn` / `shell=True` / `eval` / `exec` / `__import__` / `git push|commit|reset|checkout`。另有密钥特征扫描（私钥头、`AKIA`、`ghp_`、`sk-`、`xox`、高熵 token）。

**④ 闸门 3 · 边界约束**

| 约束 | 值 | 位点 |
|---|---|---|
| 单次最大文件数 | 4 | `code_implementation.py:41` |
| 单文件最大字节 | 48 KB | `:42` |
| 总计最大字节 | 96 KB | `:43` |
| 最大变更行数 | 400 | `:44` |
| 最大 diff 字节 | 256 KB | `:45` |
| 速率限制 | 8 次 / 60 秒 | `:49-50` |

**⑤ 事务状态机**（`code_evolution_transaction.py`）

24 态、显式转移表 `TRANSITIONS`（`:118-138`）。主链：

```
DETECTED → DIAGNOSED → PROVIDER_RESOLVED → PATCH_PROPOSED → PATCH_VALIDATED
→ CANDIDATE_MATERIALIZED → FOCUSED_VERIFIED → REGRESSION_VERIFIED
→ FULL_SUITE_VERIFIED → POLICY_AUTHORIZED → COMMIT_INTENT → COMMITTED
→ PUSH_INTENT → PUSHED → DEPLOY_INTENT → DEPLOYED_VERIFIED → HEALTHY
→ OBSERVING → SUCCEEDED_SEDIMENTED
```

终止态 5 个（含 2 类 abort、1 类 rollback、1 类 quarantine）。并发用 CAS 租约（`acquire_lease`/`renew_lease`，240 秒重试窗口），崩溃恢复走 `recover_transaction`。

**⑥ 闸门 4 · 策略授权**（`code_automation_policy.py`）

策略文件 `/etc/eimemory/code-automation-policy.v2.json`，须满足：

- 顶层键精确匹配 `_V2_TOP_LEVEL`（`:342`）
- `not_before <= now < expires_at`（`:600`）
- kill switch 文件 `/etc/eimemory/code-evolution.disabled` 不存在（`:610-611`）
- `effects` 五个开关（commit/push/deployment/rollback/sedimentation）**全为 True**，否则在 `code_evolution_effects.py:200` 直接阻断
- `max_transactions` **必须等于 1**（`:534`）

**⑦ 闸门 5 · 沙箱验证**（`code_evolution_effects.py:795-841`）

```
bwrap --die-with-parent --unshare-net --new-session
      --ro-bind / / --proc /proc --dev /dev
      --bind <scratch> /tmp
      --tmpfs /home/darrow --tmpfs /etc/eimemory --tmpfs /var/lib/eimemory
      --chdir <candidate> -- python -B -m pytest -q ...
```

三阶段：focused → regression → full_suite。`--unshare-net` 网络隔离 + `--ro-bind /` 只读根，设计强度高。依赖 `/usr/bin/bwrap`，缺失则直接返回 126（`:796-798`）。

**⑧ 外部效果**

commit（限定 allowed_files，`diff-tree` 校验变更集精确匹配，`:856-858`）→ push（`origin`/`master`）→ deploy（`install_immutable_release.sh` 不可变发布）→ health 校验 → 48 小时观察。

**⑨ 观察与沉淀**（`code_evolution_observation.py`）

`OBSERVATION_HOURS = 48`，采样相位 `(0, 15m, 1h, 6h, 12h, 24h, 36h, 48h)`。退化则回滚至上一发布，通过则 `SUCCEEDED_SEDIMENTED`。

### 1.3 触发节奏

| 入口 | 周期 | 位点 |
|---|---|---|
| `eimemory-learn-watch.timer` | 每 15 分钟 | `deploy/systemd/eimemory-learn-watch.timer` |
| `eimemory-nightly.timer` | 每日 03:30 | 调 `resume_code_evolution_transactions` |
| CLI `learn watch --apply` | 手动 | `cli/main.py:1652-1723`，`max_items=1` |

---

## 二、可行性评估

### 2.1 能力半径：4 个生产文件（最大制约）

`_V2_ALLOWED_FILES`（`code_automation_policy.py:377-388`）定义 10 个文件的硬白名单。但补丁还必须通过「受保护测试计划」校验（`code_evolution_test_plans.py`），而测试计划只授权 5 个：

| incident_class | allowed_files |
|---|---|
| `l5.product_completion_semantic_misreport` | `eimemory/governance/l5_reader.py` |
| `deployment.runtime_commit_drift` | `deploy/runtime_identity_policy.py`、`tests/test_runtime_identity_policy.py` |
| `release.closure_internal_failure` | `eimemory/governance/release_closure_gate_evidence.py` |
| `code.incident_routing_stale` / `code.system_repair_policy_stale` | `eimemory/governance/system_code_repair.py` |
| `hongtu.code-implementation-provider.v1` | `fixture.py`（`full_suite_required=False`，非生产） |

**收敛路径**：387 源文件 → 10（策略白名单）→ 5（测试计划交集）→ **4（剔除 fixture 后的真实生产半径）**。

**两个派生问题**：

1. **白名单宽于可达集**。`_V2_ALLOWED_FILES` 中的 `release_closure.py`、`release_closure_lineage.py`、`release_lineage.py`、`ops/release_closure_failure.py`、`deploy/install_immutable_release.sh` 没有任何测试计划授权，实际不可达。这是配置漂移。

2. **系统无法修复自身**。第四轮审计发现的 P0 缺陷全部落在可达集之外：

| 缺陷 | 目标文件 | 是否可达 |
|---|---|---|
| HC-01 `TRUSTED_*` 写死 | `governance/code_evolution_effects.py` | 否 |
| HC-04 `identity.py` 身份硬编码 | `identity.py` | 否 |
| HC-03 systemd 单元 `darrow` | `deploy/systemd/*.service` | 否 |
| HC-06 模型白名单 | `intake/closure_review.py` | 否 |

即：**自演化系统不能自举修复自身的可移植性缺陷**。所谓「自演化」在当前配置下是「4 个文件的定向自动修补」。

### 2.2 策略文件是人工前置条件，且一次授权仅一个事务

这是第二大制约，且比能力半径更影响可行性。

**证据一：不存在策略生成器。**

全项目对策略路径的引用只有三类：读取（`load_code_automation_policy`）、环境变量读取、存在性检查。`ops/code_implementation_owner.py:6-7` 的 docstring 明确声明：

> "This module never creates an automation policy or removes a kill switch."

**证据二：`max_transactions` 强制等于 1**（`code_automation_policy.py:534-535`）。一份策略文件只授权一个事务。

**证据三：策略含随提交漂移的坐标。** `_transaction_policy_mismatch`（`:637-697`）要求 14 个字段与事务精确一致，其中包括：

- `base_commit`（40 位 SHA）
- `base_tree_digest`（64 位摘要）
- `remote_url_digest`
- `implementation_digest`
- `revision_id` / `binding_id`
- `installer_digest`

**每次代码前进一个 commit，`base_commit` 与 `base_tree_digest` 即失效**，全部自演化被阻断于 `policy_transaction_base_commit_mismatch`。

**证据四：示例文件已陈旧。** `deploy/code-automation-policy.v2.json.example` 使用 `revision_id: "code.implementation:v8"`，而代码已是 `v10`（`adapters/hermes/code_implementation.py:29`），且 `effects` 五项全为 `false`（`policy_id: "bootstrap-all-effects-disabled-v2"`）。该示例直接照用会被 `_transaction_policy_mismatch` 的 `revision_id` 校验拒绝。

**综合判断**：当前形态下，"自演化"实际是**「人工逐提交签发单次授权」的受控自动化**。人工刷新策略是硬瓶颈，其成本可能高于直接人工改代码。这实质性削弱了自动化的价值主张。

### 2.3 吞吐上限

三重限制叠加：

1. `max_transactions = 1` —— 一策略一事务
2. `max_items = 1` —— `cli/main.py:1672` 每次只提交 1 个事件
3. `OBSERVATION_HOURS = 48` —— 每次部署需 48 小时观察窗口才进入 `SUCCEEDED_SEDIMENTED`

即：**从检测到闭环沉淀，单次最短 48 小时**；且期间需人工重新签发策略才能启动下一轮（因 `base_commit` 已变）。

### 2.4 平台依赖

全部为 Linux 专有，Windows / macOS 完全不可用：

| 依赖 | 用途 | 位点 |
|---|---|---|
| `bubblewrap` | 验证沙箱 | `code_evolution_effects.py:796` |
| `SO_PEERCRED` | socket 对端 uid 校验 | `adapters/hermes/code_implementation.py:1011` |
| `fcntl` | 事务租约 | `storage/code_evolution_store.py` |
| systemd timers | 触发节奏 | `deploy/systemd/*.timer` |
| `/var/tmp`、`/etc/eimemory`、`/var/lib/eimemory` | 沙箱 tmpfs 挂载点 | `code_evolution_effects.py:802,827-829` |

### 2.5 安全设计评估（正面）

需要明确指出：**这套系统的治理严谨度高于同类项目的一般水平**。具体优点：

1. **提案与执行彻底分离**。模型输出被剥夺全部执行权，且以 schema 精确键集 + 键黑名单 + 正则三层拦截。
2. **边界封闭**。文件白名单 + 尺寸上限 + 变更行上限 + 速率限制，多重正交约束。
3. **验证环境强隔离**。`bwrap --unshare-net --ro-bind /` 阻断网络与写入，三阶段测试递进。
4. **发布不可变**。`install_immutable_release.sh` 生成版本化目录，health 校验后才切 `current` 软链。
5. **失败可回滚**。`ROLLBACK_INTENT → ROLLED_BACK_HEALTHY` 路径完备，48 小时观察含 8 个相位。
6. **状态机可恢复**。CAS 租约 + `recover_transaction` 处理中断，避免半途状态。
7. **补丁作者模型不写死**，用宿主配置，符合项目可移植性方向。

### 2.6 安全设计的三个薄弱点

**薄弱点一：执行权拦截是正则而非 AST（除 1 类事件外）。**

`_EXECUTION_AUTHORITY`（`code_implementation.py:91-96`）作用于补丁**新增行**（`:414`），覆盖 `subprocess`、`os.system`、`eval`、`exec` 等。但未覆盖：

- `importlib`（`importlib.import_module` 可动态加载任意模块）
- `os.execv` / `os.execve` / `os.spawn*` / `os.fork`
- `socket` / `urllib` / `http.client`（直接构造网络请求）
- `ctypes`（可绕过全部 Python 层限制）
- `getattr` 动态派发（如 `getattr(os, "sys" + "tem")`）
- `open()` 越权写入（路径白名单只约束 `file_updates`，不约束运行时代码行为）

`code_evolution_semantic_validation.py` 提供 AST 级校验，但**仅覆盖 `release.closure_internal_failure` 一类事件、仅一个文件**（`_GATE_EVIDENCE_PATH`），且校验的是「收据证据角色」这一业务不变量，不是通用安全不变量。

其余 3 类事件仅有正则 + 测试兜底。

**薄弱点二：无人工审批环节。**

`effects` 五项为全开或全关，没有中间态。策略文件一旦签发，从提案到生产部署全自动，无 PR 审查、无人工 approve。考虑到 §2.6 薄弱点一，这个组合的风险敞口偏大。

**薄弱点三：测试通过 ≠ 语义正确。**

系统自身在 `code_evolution_semantic_validation.py:19-27` 的 docstring 中承认了这一点（该模块存在的理由就是「结构上合法但语义违规」的提案）。但该防护只覆盖 1 类事件。

---

## 三、建议

### 3.1 提升可行性（按收益排序）

**建议 1：将能力边界从「文件清单」改为「目录级 + 不变量级」授权。**

现状用硬编码文件白名单表达边界，导致能力无法随代码演进扩展，且新增文件必须改代码。建议改为目录级规则，例如「允许修改 `eimemory/governance/**`，禁止修改 `tests/**`、`deploy/**`、策略加载器与自演化平面自身」。

这样既保留核心安全属性——**候选不得修改自己的策略、入口与测试**（该意图在 `code_evolution_test_plans.py:138` 的注释中已明确表达）——又让能力自然扩展。

**建议 2：自动化策略签发，消除人工瓶颈。**

这是投入产出比最高的一项。具体：

- 新增 `eimemory code-evolution policy-issue` 命令，从当前 HEAD 自动填充 `base_commit`、`base_tree_digest`、`remote_url_digest`、`installer_digest`、`test_plan_digest`
- 在 `install_immutable_release.sh` 部署成功并通过 health 校验后自动调用，签发**下一轮**策略
- 关键：策略里的坐标信息**代码里其实已经有了**——`_V2_REPOSITORY` 已包含 `root`/`remote`/`branch`/`base_commit`/`base_tree_digest`，只是签发环节缺失

若不解决此项，「自演化」的价值主张难以成立。

**建议 3：放开 `max_transactions` 为可配置正整数，并支持按 incident_class 并发。**

状态机已具备 CAS 租约与幂等键，技术上支持并发。不同 incident_class 的文件集互不重叠（见 §2.1 表格），可安全并行。建议 `max_transactions` 改为 `1..N`，`max_items` 同步放宽。

**建议 4：观察期按风险分级。**

`OBSERVATION_HOURS = 48` 是双重硬编码（模块常量 + 策略 `observation_seconds=172800`）。建议按 `risk_tier` 分级：`bounded_write` 类 6–12 小时，`network` / `schema` 类保留 48 小时。

**建议 5：增加「自动 commit + push 候选分支，人工 approve 后 deploy」中间态。**

现状只有全自动与全停两个极端。增加 `effects.deployment=false` 但 `commit/push=true` 的中间态，可让系统在积累置信度阶段保留人工审查，这是渐进式信任的必经路径。

### 3.2 补强安全薄弱点

**建议 6：将执行权校验从正则升级为 AST 分析，并覆盖全部事件类。**

把 `code_evolution_semantic_validation.py` 从「单事件业务不变量校验」升级为「通用安全不变量校验」，对每个 `file_updates` 的 `content` 做 AST 遍历，拒绝：

- `import importlib` / `importlib.import_module`
- `os.exec*` / `os.spawn*` / `os.fork`
- `socket` / `urllib` / `http.client` / `requests` 导入
- `ctypes` 导入
- 动态 `getattr` 派发到上述模块
- 运行期 `open()` 写入路径白名单之外的调用

正则与 AST 双轨：正则做快速拒绝，AST 做语义拒绝。

**建议 7：建立 `_V2_ALLOWED_FILES` 与测试计划 `allowed_files` 的一致性回归测试。**

当前白名单宽于可达集（10 vs 5），属配置漂移。建议新增测试断言二者交集等于白名单本身，或显式声明不可达文件并说明理由。

**建议 8：为 `bwrap` 缺失提供降级路径。**

现状直接返回 126 硬失败（`code_evolution_effects.py:796-798`）。建议降级为「跳过自演化并明确上报 `verification_sandbox_unavailable`」，而非让事务停滞在中间态。

### 3.3 与第四轮可移植性审计的衔接

自演化平面自身携带多处上轮已记录的硬编码，且这些缺陷**无法被自演化系统修复**（§2.1）。需要人工修复，且部分修复成本极低：

| 上轮编号 | 位点 | 修复要点 | 成本 |
|---|---|---|---|
| HC-01 | `code_evolution_effects.py:47-49` | `TRUSTED_*` 改为读取策略文件已有字段 | 低（策略里已有这些值） |
| HC-07 | `code_automation_policy.py:557,596` | 硬相等校验改为自洽校验（路径为绝对路径 + 是 git 仓库 + digest 匹配），而非等于作者环境 | 中 |
| HC-03 | `code_evolution_effects.py:825` | `--tmpfs /home/darrow` 改为按 `Path.home()` 或策略注入 | 低 |
| HC-15 | 策略示例 | 示例 `v8` 更新至 `v10`，并补一份 effects 全开的完整示例 | 低 |
| HC-02 | `deploy/systemd/eimemory-rpc.service:26` | 自演化健康检查依赖此端点，须一并去除 Tailscale IP | 低 |

**HC-01 值得单独强调**：策略文件已承载 `repository.root` / `remote` / `branch` 三个字段，而 `code_evolution_effects.py` 却用模块级常量而非策略值。这是纯粹的实现冗余，改动成本极低但收益显著——修复后自演化才可能在不同用户的服务器布局下成立。

---

## 四、结论

**机制定性**：声明式补丁提案 + 24 态事务状态机 + 五道闸门 + 不可变发布 + 48 小时观察的窄域闭环自修复系统。治理严谨度高于同类项目一般水平，提案/执行分离、沙箱隔离、自动回滚三处设计尤其扎实。

**可行性判断**：**技术上成立，但当前配置下实用价值受限**。三个理由：

1. **能力半径仅 4 个生产文件**，且无法覆盖自演化系统自身——不能自举修复自己的缺陷。
2. **策略签发是人工瓶颈**：`max_transactions` 强制为 1，且 `base_commit` 每次提交即失效，无自动签发器。实际形态是「人工逐提交签发单次授权」，自动化收益被人工成本抵消。
3. **单次闭环最短 48 小时**，吞吐受观察期硬约束。

**投入产出比最高的三项改造**（按顺序）：

1. 自动化策略签发（建议 2）——直接消除人工瓶颈，激活整个平面的价值
2. 执行权校验升级为 AST（建议 6）——当前正则存在 `importlib` / `ctypes` / `os.exec*` 等明确绕过路径
3. 能力边界改为目录级 + 不变量级（建议 1）——解除能力半径限制，同时保留核心安全属性

**与前序审计的关系**：自演化平面既是第四轮可移植性缺陷的**承载者**（HC-01/03/07/15 均位于此平面），又是这些缺陷的**无法修复者**（可达集不含自身）。这意味着该平面的可移植性缺陷只能人工修复，且 HC-01 的修复成本极低——策略文件里已有正确值，只需让代码去读。

---

## 附：关键常量与位点索引

| 项目 | 值 | 位点 |
|---|---|---|
| 最大文件数 | 4 | `adapters/hermes/code_implementation.py:41` |
| 单文件字节上限 | 48 KB | `:42` |
| 总计字节上限 | 96 KB | `:43` |
| 最大变更行数 | 400 | `:44` |
| 速率限制 | 8 / 60s | `:49-50` |
| 补全 token 上限 | 8192 | `:47` |
| 补全超时 | 120s | `:48` |
| 温度 | 0.0 | `:1081` |
| 观察期 | 48 小时 | `governance/code_evolution_observation.py:9` |
| 观察相位 | 0/15m/1h/6h/12h/24h/36h/48h | `:10` |
| 策略最大事务数 | 1（强制） | `governance/code_automation_policy.py:534` |
| 策略路径 | `/etc/eimemory/code-automation-policy.v2.json` | `:29` |
| kill switch | `/etc/eimemory/code-evolution.disabled` | `:30` |
| socket 路径 | `/var/lib/eimemory/run/hermes-code-implementation.v2.sock` | `adapters/hermes/code_implementation.py:38` |
| 沙箱二进制 | `/usr/bin/bwrap` | `governance/code_evolution_effects.py:796` |
| 状态数 | 24（终止 5） | `governance/code_evolution_transaction.py:73-107` |
| 租约重试窗口 | 240s | `:30` |
| 受保护测试计划数 | 5 | `governance/code_evolution_test_plans.py:155-161` |
| 策略白名单文件数 | 10 | `governance/code_automation_policy.py:377-388` |
