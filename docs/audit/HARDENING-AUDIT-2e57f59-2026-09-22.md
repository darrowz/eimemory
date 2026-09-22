# eimemory 架构、业务与安全审计：已验证修复及剩余范围

**审计基线：`2e57f59bcab4a0e50aae8a9423c7440c4213ea03` / 1.13.19。**

## 一、执行结论

本轮交付的是覆盖已验证边界问题的可审阅合并补丁：4 个产品文件修改、3 个产品模块新增、1 个测试文件新增，104 项新回归通过。没有改写用户仓库、提交、推送、创建 PR 或部署生产。

**不能把本结果写成“全项目全量修复完成”“零遗留风险”或“生产业务闭环验收通过”。** GitHub 读取成功，但整仓下载在运行环境中失败，用户本地开发连接返回 404。完整源码、全部依赖、真实 Runtime、全测试集、生产数据库/向量服务和桥接服务未在本次运行环境中建立。完整文件与片段的恢复范围在 manifest 中逐项列明；未见到的代码没有被推断为安全或不存在。

仓库 `docs/audit/REMEDIATION-STATUS-2026-09-22.md` 的“Residuals: None”是此前修复状态声明，且文内明确有 focused tests、本地替身及未生产部署。它不能替代本轮验证。旧报告中的 28fecb9、1.13.16 代码行数与旧缺陷列表，也未作为当前提交的全量实测结论。

本轮未确认 P0，但这只陈述本轮覆盖范围，不代表全仓排除 P0。下面等级为审阅优先级，不是 CVSS；利用前提和验证方法比等级更重要。

## 二、发现与修复矩阵

| ID | 优先级 | 原始证据/反例 | 修复与局部验证 |
|---|---|---|---|
| AUD-01 | P2，状态完整性 | `atomic_file.read_json_strict` 使用默认 `json.loads`，接受重复键、NaN/Infinity；默认 writer 可写出非标准非有限数值。复现中重复的 approved 键以后一项覆盖前一项。**未把此示例当作真实审批绕过证明。** | 接入现有 strict_json；读写同一语言；默认 16MiB/64 层；拒绝非法值后保留原文件；测试重复键、数值、深度、UTF-8、类型、大小上限。 |
| AUD-02 | P1，条件性本地文件破坏 | RPC 诊断日志先读后 `Path.write_bytes`，跟随符号链接；临时目录中的日志符号链接确实覆写了其目标。需要攻击者能布置该日志路径或相应目录，不是无认证远程 RCE。 | 单链接普通文件约束、O_NOFOLLOW/O_NONBLOCK 与 fd/path 身份核验、同目录原子替换、0600、跨实例/进程 sidecar lock。拒绝链接/FIFO/目录，目标文件保持不变；80 个线程写入及 3 进程×20 次日志写入不丢条目（容量足够、默认阻塞锁条件）。 |
| AUD-03 | P2，适配器容错/敏感错误边界 | `http.client.BadStatusLine` 等 HTTPException 不在原始异常归一化范围，能直接逸出 `call_or_bypass`；原始 `raise ... from exc` 还保留含服务器/URL 信息的可打印因果链。 | 归一化 HTTPException，标准 traceback 抑制原始 cause 文本；返回固定诊断码；无效出站 JSON 不发请求；保留 HTTP 状态码、响应上限和现有网络策略。 |
| AUD-04 | P2，并发熔断正确性 | 先启动的成功请求，在另一请求触发新熔断后完成，会无条件清零新熔断状态；原逻辑冷却后也没有独占探测票据。前一个竞态已用事件控制线程真实复现。 | 单独的 CircuitBreaker；代际票据隔离旧完成事件；半开期单探测；探测失败重开、成功关闭、取消释放。线程并发 40 次准入仅一个半开探测。 |
| AUD-05 | P2，业务报告真实性 | 聚合器仅处理 `ok is False`，会把 `{"ok":"false"}` 等已出现但不合法的关键报告跳过；L5 的 `awaiting_evidence=True` 提前返回，能隐藏同一报告明确的磁盘错误。两者均已复现。 | 将纯契约从 jobs 中提取；已出现的关键结构必须有布尔 ok；已知等待证据不覆盖明确执行错误/阻断指标；保留空回放列表、正常列表和合法证据等待的成功语义。**未宣称下游真实部署门已经被利用。** |
| AUD-06 | P3，长期进程资源 | 全局强引用锁字典永久保留历史路径。5,000 路径微基准留存 5,000 个锁。 | 弱引用锁表，活动持有者和等待者仍保留强引用；5,000 历史路径结束后留存 0；线程/进程状态更新仍序列化。不是“消除所有内存泄漏”。 |
| AUD-07 | P2，连接预算 | `_connect_pinned` 对每个 IP 都重新分配完整 timeout，连接阶段可随地址数量累积；基线两次分配均为 0.8 秒。 | 一个 monotonic 连接截止时间，后续地址只得到剩余预算；受控时钟测试得到 0.8/0.5 秒，预算耗尽不启动第三次连接。未覆盖 DNS、慢响应等端到端阶段。 |
| ARCH-LOCAL | 结构改进，不单独计漏洞 | RPC 类同时承担 HTTP、熔断和日志；nightly 纯结果契约只能经巨大的 jobs/Runtime 模块访问。 | 新增 circuit_breaker、bounded_jsonl、result_contract 三个模块；jobs 保留原名称导出，避免复制两个实现。AST 回归确认新组件不依赖 eimemory.api，jobs 没有残留重复定义。没有声称整仓依赖环清零。 |

基础文件边界还增加对硬链接、特殊文件、悬空符号链接、读取上限与原子写失败的防御。部分是针对已见实现的加固，而非每一种都已经形成真实生产漏洞的证据；不要把所有防御测试各算一个漏洞。

## 三、架构与解耦

### 本轮实际落地的依赖方向

```text
scheduler.jobs ──→ scheduler.result_contract（纯结果契约）

adapters.runtime.http_client
    ├──→ adapters.runtime.circuit_breaker（并发准入/恢复）
    ├──→ intake.safe_transport（既有 DNS pin / peer check）
    └──→ storage.bounded_jsonl（只用于诊断）
                └──→ storage.atomic_file
                            └──→ core.strict_json
```

改动将状态机、传输、副作用持久化分开，使并发和文件错误能独立验证。它没有新增插件注册机制、自动化执行权限、数据库迁移或全局 Runtime 容器。

原始 `api/runtime.py` 为 111,547 字节、`api/memory.py` 为 100,547 字节、`storage/sqlite_store.py` 为 340,178 字节、`scheduler/jobs.py` 为 131,166 字节（固定提交 tree 元数据）。这些规模提示优先分解的审阅对象，但**文件大小不等于已证明的耦合度或漏洞**。

对余下架构的建议是沿实际事务边界逐步提取，而不是用一个未测试的大重写制造新的风险：

| 边界 | 建议职责 | 合并前需要的证明 |
|---|---|---|
| Runtime composition root | 只组装服务、连接生命周期、配置与跨模块接口 | 冷启动/关闭失败测试、不同适配器配置矩阵、完整 import 检查 |
| Memory / recall orchestration | scope、截止时间、候选预算、融合/重排与最终 hydration 的编排 | 保持召回与权限语义的金标集；真实 SQLite/向量后端；负向跨 scope 样本 |
| Storage | 索引/主数据读写、事务、迁移，不拥有晋升授权决策 | 迁移前后数据校验、事务中断与恢复、连接并发测试 |
| Governance / promotion | gate、lease、账本、effect-owner 与回滚对账 | 副作用每个切点故障注入、重启重入、lease 过期和证据身份不符拒绝 |
| Adapters | 只做协议、认证上下文与 host lifecycle 映射 | Codex/Hermes/OpenClaw 的实际 host 生命周期、回执与终态契约测试 |

`tools/audit_inventory.py` 可在完整本地仓库生成文件规模、最长函数、导入边、潜在强连通分量及高风险调用的复核位置。本次仅在恢复子集上测试工具，其输出明确为 `SUBSET_syntax_inventory.json`。条件导入和函数内导入也进入依赖图，所以图中的环不是“已证明的运行时 import crash”；动态 import 和跨语言调用也不可能仅靠这张图穷尽。

## 四、业务闭环核对

### 已验证的局部闭环

**RPC 故障**：输入验证 → 准入票据 → HTTP/响应解析 → 固定故障分类 → 正确代际的熔断更新 → 有界诊断记录/明确诊断失败 → 适配器绕过结果。HTTP 协议错误不能再无意逃出 fallback；取消不会永久占用半开探测。

**状态修改**：进程内锁 → 进程间锁 → 文件类型/身份检查 → 有界严格读取 → mutate → 有界严格序列化 → 原子发布 → 目录同步 → 释放锁。失败前后的文件状态被分别测试。特别保留“发布后目录 fsync 失败属于不确定持久性”的事实，不能用笼统异常处理宣称已回滚。

**nightly 判定**：步骤报告 → 关键章节的类型/布尔校验 → 非致命证据等待豁免 → 显式错误优先 → 顶层结果。此处验证的是纯判定函数，**不是整夜任务实际跑完、数据库可用或 L5 真正完成**。

### 纠正一个容易误报的点

`_run_replays` 的汇总 `ok=True` 值得检查，但 `EvolutionAPI.replay_rule` 实际返回 RecordEnvelope，评测结果位于 meta.verdict，不是含布尔 ok 的传输结果。**执行成功与评测通过是两件事**。没有完整调用链及明确发布契约，不能把所有 fail verdict 直接等同任务执行失败，也不能把一个“看起来是 list”的返回值当作漏洞。因此本补丁没有擅自改变回放 verdict 的业务含义。

现有 promotion_manager 读取片段已经在副作用前执行 `_require_lifecycle_recorded`，且包含 lease、评估与机器策略门；本轮没有再次套用早期“未检查 ledger”的旧修复，也没有改动自动提交/生产部署默认关闭的策略。完整晋升、回滚和真实 effect-owner 对账仍须独立验证。

## 五、性能结果及取舍

微基准使用 Linux / Python 3.13.5，一次本地运行，4,024 字节 JSON 文件，未涉及生产 I/O、真实召回或网络。见 `results/microbenchmark.json`，不要将其包装成 P95/P99 SLA 或召回效果改进。

| 指标 | 基线 | 补丁 | 解读 |
|---|---:|---:|---|
| 5,000 历史路径后仍保留的锁 | 5,000 | 0 | 消除这一处路径数量线性增长的长期留存 |
| 锁表跟踪分配留存增量 | 717,738 B | 176 B | tracemalloc 局部读数，不是整进程 RSS |
| 100 次小 JSON 读取总耗时 | 0.00202 s | 0.01831 s | 严格语法/文件验证有明显额外成本 |
| 30 次小 JSON 写入总耗时 | 0.00634 s | 0.01345 s | 验证与安全发布增加成本 |

所以这不是“全项目性能全面提升补丁”。它以校验成本换取确定的输入/资源边界，并修复锁对象留存和重复连接预算。生产阶段应另外测量：每次 recall 的 SQL 数、候选数、读取行数、hydration 次数、FTS 查询计划、重排成本、锁等待、冷/热缓存以及 P50/P95/P99。优化目标应绑定召回质量和 scope 隔离，不能为速度绕过质量门或削减正确性。

## 六、验证台账

已经执行：基线反例复现；104 项新增回归；真实临时文件、线程及多个独立 Python 子进程；四个完整基线文件 Git blob 核验；已恢复片段上的 `git apply --check --whitespace=error-all`、实际应用、语法编译、反向补丁检查；局部微基准；工具级错误基线/路径与清点逻辑检查（详见 VALIDATION）。

没有执行：完整仓库 Git 应用、完整项目导入、现有全测试集、Runtime / nightly 整链、Windows 锁、Python 3.11 运行时、PostgreSQL/向量服务、真实适配器、生产负载、全部依赖漏洞数据库匹配、生产部署或回滚演练。新回归中的模拟网络和纯函数测试不等于上述集成验收。包内没有“全量测试通过”的替代标签。

## 七、全项目剩余审计/验收矩阵

| 范围 | 本轮证据层级 | 尚需完成的项目及判定条件 |
|---|---|---|
| 仓库整体与依赖图 | 顶层/部分子树元数据，选定源码、现有审计说明 | 取得完整固定提交；全量语法、依赖/动态导入与跨语言边界检查；核对清点工具的 skipped/parse_errors |
| Runtime / MemoryAPI | 文件元数据；EvolutionAPI / promotion 调用片段 | 完整服务生命周期、事务/并发约束、入库-索引-召回-反馈的同一数据链路 |
| SQLite / JSONL / payload segments | 完整 atomic_file；recall_deadline、replay_buffer 源码阅读；其余元数据/引用定位 | 全事务边界、migrations、索引一致性、锁顺序、取消/超时恢复、磁盘满/损坏恢复、真实 FTS 查询计划 |
| Recall / vector / reranker | 连接阶段修复；旧修复说明未重新作为实测 | 全候选与 hydration 路径、scope 负向测试、向量失配、脏索引、延迟预算耗尽；性能改进必须同时维持质量 |
| Governance / self-evolution | promotion_manager 当前片段；确认存在前置 ledger 门 | durable intent→side effect→receipt→terminal 的失败切点；重启幂等与回滚对账；不得以健康探针代替有效结果证据 |
| Scheduler | 开头 190 行恢复、后续部分源码读取；纯判定函数测试 | 完整生产方报告形状、完整 nightly / replay / L5 执行、失败与等待证据组合、跨运行恢复 |
| RPC 与网络安全 | 一个客户端、共享 safe_transport 完整源码与定向修复 | 全服务端认证/授权、scope/tenant 绑定、请求体分片/限流、所有外联入口及总截止时间；未宣称已审完所有 SSRF 面 |
| Codex / Hermes / OpenClaw 等接入 | 客户端引用定位 | 真实 token/scoped identity、session/terminal/receipt 生命周期和故障重试；宿主差异验证 |
| Deploy / 运维 / CI | 目录与既有闭环说明 | 全 shell/Python 安装链、服务账号/权限、真实存储目录、不可变发布/回滚、故障演练以及完整 CI 结果 |
| 依赖与供应链 | pyproject 的声明；没有完整已部署环境 | 针对实际安装版本生成依赖清单并匹配漏洞公告；校验构建和工件来源，不能把“无强制依赖”写成“无供应链风险” |

以上是未完成的验证工作，不是已证实的 10 个漏洞，也没有伪造占位修复。当前补丁应先进入独立审阅工作树，再执行 README 中的相关现有测试、完整测试和真实环境验收。

## 八、保留限制与安全边界

文件父目录仍必须可信；不能防御拥有该目录重命名/替换能力的同权限主动对手。Windows reparse points/ACL 未实测，POSIX 硬链接拒绝策略可能影响既有运维流程。诊断轮转日志有意有容量和等待上限，不具备治理证据“不得丢失”的契约。读写严格化可能暴露原有大文件或非法 JSON，必须核对真实状态后上线。额外验证并未消除文件系统与编码 CPU 成本。

代码回滚不回滚数据；生产回滚应由实际发布/effect-owner 流程完成。本包不复用旧版本验收作为新版本证据、不改变机器授权策略，也不作“零漏洞”保证。

## 固定提交的一手证据

所有源链接指向同一提交；来源为实际 GitHub 读取，非搜索结果对代码内容的猜测。

- [提交与发布变更](https://github.com/darrowz/eimemory/commit/2e57f59bcab4a0e50aae8a9423c7440c4213ea03)
- [atomic_file.py](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/eimemory/storage/atomic_file.py)
- [strict_json.py](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/eimemory/core/strict_json.py)
- [http_client.py](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/eimemory/adapters/runtime/http_client.py)
- [safe_transport.py](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/eimemory/intake/safe_transport.py)
- [jobs.py](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/eimemory/scheduler/jobs.py)
- [replay_rule 及评测结构](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/eimemory/api/evolution.py#L228-L276)
- [promotion_manager 当前前置门](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/eimemory/governance/promotion_manager.py#L710-L870)
- [已有修复状态与验证限制](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/docs/audit/REMEDIATION-STATUS-2026-09-22.md)
- [SCH-01 兼容测试](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/tests/test_sch01_nightly_ok_semantics.py)
- [原子状态现有测试](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/tests/safety/test_atomic_state_closure.py)
- [构建及可选依赖声明](https://github.com/darrowz/eimemory/blob/2e57f59bcab4a0e50aae8a9423c7440c4213ea03/pyproject.toml)
