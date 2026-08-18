⚠️ 任务中止 — 删除前发现严重问题

## 结论
原任务说"28 个僵尸，0 真 import"，**不准确**。我用 5 个维度重新验证后，**这 28 个里只有 0 个可以安全删除**。其余 28 个有真引用（production / tests / deploy subprocess 入口 / integrations）。

## 我的验证方法（5 个维度）
1. 静态 import：from eimemory.X.Y import ... / import eimemory.X.Y / eimemory.X.Y.attr
2. 动态 import：importlib.import_module、__import__
3. 配置文件：YAML/JSON/TOML/MD/cfg/ini
4. subprocess：python -m eimemory.X.Y 字符串
5. entry_points：pyproject.toml / setup.py 的 entry points

搜了 eimemory/、tests/、scripts/、deploy/、docs/、examples/、integrations/ 全部。

## 重要：v2 验证脚本有 bug
原任务的脚本（`from eimemory\.$rel\b` + `import eimemory\.$rel\b` + `eimemory\.$rel\.` 三个 pattern）漏报。我做正反 sanity check 发现：active 模块 prompt_safety 能找到 5 个引用（OK），但 26 个有真引用的 zombie 文件 v2 一律返回 0。原因在 PowerShell Get-ChildItem + Select-String 链对长 pattern 有时静默丢结果。已切换到更简单可靠的全路径 pattern `"eimemory\.$rel"` 后才看到真引用。

## 28 个文件的真实状态

### 0 个可以安全删除
所有 28 个文件都有真引用。

### 26 个有真引用（绝对不能删）

按引用类型分类：

PRODUCTION 代码层引用（最高优先级，删了代码就崩）：
- eimemory/governance/safety/audit.py ← eimemory/autonomous/loop.py:83 (`from eimemory.governance.safety.audit import AuditLog`)
- eimemory/governance/safety/circuit_breaker.py ← eimemory/autonomous/loop.py:84
- eimemory/governance/safety/profile.py ← eimemory/autonomous/loop.py:88 (`from ... import load_profile`)
- eimemory/governance/safety/kill_switch.py ← eimemory/cli/main.py:1178（CLI 子命令 `emergency-stop`，lazy import）
- eimemory/governance/safety/audit.py ← eimemory/governance/safety/audit_verifier.py:23
- eimemory/governance/safety/kill_switch.py ← eimemory/governance/safety/audit_verifier.py:24

CLI 入口（生产 deploy）：
- eimemory/governance/serve_console.py ← deploy/systemd/eimemory-console.service:14（`ExecStart=python -m eimemory.governance.serve_console`）

Subprocess 入口（生产 env 配置）：
- eimemory/governance/prompt_safety_openclaw.py ← eimemory/governance/prompt_safety_executor.py:73 读 `EIMEMORY_PROMPT_SAFETY_COMMAND` env，subprocess 调 `python -m eimemory.governance.prompt_safety_openclaw`；deploy/governance.env.example:3 有官方示例
- eimemory/llm/openclaw_adapter.py ← eimemory/llm/command_client.py:140 读 `EIMEMORY_LLM_COMMAND` env，subprocess 调 `python -m eimemory.llm.openclaw_adapter`；deploy/governance.env.example:11 有官方示例

测试（pytest 会收集，删文件会让 166 个测试收集失败）：
- test_mcp_stub.py (5 tests), test_karpathy_runner.py (1), test_capability_discovery.py + test_segmented_record_consumers.py (多个), test_seven_day_review.py (10), test_serve_console.py (多), test_living_temporal.py (多), test_evidence_first.py, test_state_machine.py + test_state_machine_path_traversal.py, test_held_out_split.py, test_adapters.py, test_audit_verifier.py, test_atomic_state_closure.py, test_anomaly.py, test_audit_chain.py, test_circuit_breaker.py, test_kill_switch.py, test_network_proxy.py, test_outbound_comm.py, test_autonomy_profile.py, test_promotion.py, test_spend_guard.py, test_eiskills_bridge.py, test_skill_merge.py, test_feishu_delivery_state.py, test_l3_queue.py, test_loop.py + test_loop_hard_timebox.py, test_business_feedback_loop.py + test_business_feedback_no_data.py

集成插件：
- eimemory/adapters/hermes/host_context.py ← integrations/hermes/eimemory_hook/__init__.py:8

pytest --collect-only 验证：166 tests collected，删任何相关文件会全部崩 collection。

### 自我引用（self-source）
- eimemory/autonomous/runner.py:37 `"source": "eimemory.autonomous.runner"`
- eimemory/autonomous/seven_day_review.py:157 `"source": "eimemory.autonomous.seven_day_review"`

这些是 self.source 字段在 metadata 里引用自己模块路径字符串，删文件后 audit log 仍能查到。

## 关于 0 真 import 的"0 引用是真是没用？"

你的提醒救了一次。0 引用是"必要不充分"条件。原任务用了一个有 bug 的搜索脚本，把 26 个真用文件误判为 0 引用。**严格按"0 静态 import"标准**：
- 0 个文件能删（因为 v2 bug 误报）
- 28 个全有真引用

**如果你想真正删除**，可选的子集是 2 个（按"0 静态代码 import 但有 subprocess 引用"标准）：
- eimemory/governance/prompt_safety_openclaw.py
- eimemory/llm/openclaw_adapter.py

但这两个还有 deploy/governance.env.example 引用，删了会让 deploy 文档撒谎。**最安全的做法是同时更新 env.example**（标 DEPRECATED 或指向新实现），或者直接保留。

## 我做了什么
- 写了 4 个验证脚本（v1/v2/sanity/recheck）放在 .tmp/
- 跑了正反 sanity check
- 跑了 pytest --collect-only 确认 166 个测试依赖
- 跑了 grep 确认 production code 引用
- 跑了 grep 确认 deploy/integration 引用
- **没删任何文件**

## 你的决策
请选一个：
A) **不删任何文件** — 我把报告写到 docs/audit/，等后续清理战役再处理
B) **只删 2 个 subprocess 入口文件**（prompt_safety_openclaw + openclaw_adapter），同时改 deploy/governance.env.example 标 DEPRECATED
C) **删部分测试 + 对应 zombie 文件**（例如 test_mcp_stub.py + mcp_stub.py）— 但需要先确认那些测试是不是该被保留
D) **重新做审计** — 找正确方式判断"该不该删"，不依赖 import 计数

## 给后续清理的 lesson
1. PowerShell `Get-ChildItem -Recurse -Include *.py | Select-String -Path $_.FullName` 链对长 pattern 有静默丢结果 bug。先用单文件单 pattern sanity check 验证脚本能命中已知活跃模块，否则会全 0 误报。
2. "0 import" 不是"无用"。Subprocess 入口（env var → `python -m X`）是 hidden 依赖，要 grep `os.environ.get("EIMEMORY_.*_COMMAND")` 这类 pattern 找。
3. deploy 配置文件（systemd unit、env example）也是依赖来源。
4. integrations/ 子目录是独立插件，可能 import 主包但反过来不成立。
5. pytest --collect-only 是金标准 — 任何会破坏 collection 的删除都是错的。

等你回话再行动。
