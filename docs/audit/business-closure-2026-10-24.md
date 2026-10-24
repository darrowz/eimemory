# 业务闭环集成审计与全量补丁说明

日期：2026-10-24。基线：`78051a699d75ee33756cefa1f1ab859e1b3662f0`。

本轮围绕“请求进入—持久化—异步加工—召回—评估—发布验收—失败恢复”检查跨层一致性，将已复现问题合并为一份补丁，包含生产变更、7 个新增回归文件及必要的合同/夹具更新。修复依据是实际状态转换、失败注入和落库回读，不以接口返回成功代替业务完成。

**标准全仓结果：15,911 passed、0 failed、0 errors、138 skipped；另有 8,836 subtests passed；393.58 秒，退出码 0（Python 3.12.14、普通 UID 1001、8 workers）**

标准 suite 在独立 checkout 中以普通用户验证；此前 root、受限沙箱中的中止和环境相关失败保留为诊断证据，不能当作全仓通过。自动 source inventory 盘点到 804 个维护源文件、263,360 行、9,088 个可调用单元，其中 743 个文件通过相应解析、61 个原生配置/脚本项目留待原生执行验证。这是入口和归属盘点，不代表逐行人工审查，也不证明全部运行状态均已覆盖。

## 一、生产缺陷与修复

表内条目按业务失败模式归组，不把每个参数化测试重复计成缺陷。

| 编号 | 原有问题及业务影响 | 修复后行为 |
| --- | --- | --- |
| B1 接入幂等与归属 | adapter 命中 event ID 即返回旧记录，变更正文、类型或业务身份仍被确认；旧 source 推断可能移动 owner scope | 重试交给 MemoryAPI 与原子写入端校验完整请求；旧格式的显式 `semantic_key`、`memory_layer` 也必须相符。写入使用已解析的精确 scope/source，返回与持久化身份一致 |
| B2 L0→L1 入口与去重 | 被拒收的 L0 仍进入抽取；只按 episode ID 去重会吞掉其他用户或渠道的同名任务 | 仅成功且 active 的 L0 进入 L1；去重包含 episode、channel 与四部分 scope，两个合法 owner 各自完成抽取 |
| B3 队列落盘与 ACK | 损坏队列可能像空队列；锁/写入失败及 handler 完成后的 ACK 丢失被弱化为成功 | 校验队列结构、状态与 attempts；保留损坏原件；报告真实失败阶段及 handler/ACK 状态，未知计数为 `None`，worker 与 health 显式 unavailable/degraded |
| B4 论文候选归属 | 调用方持有的旧 envelope 可在候选缺失、类型已变或 scope 不符时继续写入下游产物 | 提升前回读精确 durable candidate，检查当前类型和 caller scope；拒绝时不生成 paper/extract/claim/page。原本支持的无 record ID 直接输入仍可使用 |
| B5 Hermes 删除后恢复 | 内容确定性 ID 永久占位，合法删除后无法重新添加；其他 target 的历史挤占查询窗口 | 新事件可在验证原生删除证据后创建新 incarnation；保留原记录、墓碑及恢复关系；查询先按精确 owner/source/target 限定，再应用历史上限 |
| B6 时效边界 | `invalid_at`、`valid_until`、`effective_until` 用首个非空值判断，未来值或坏值掩盖另一已过期边界 | 三个上界独立判断，任一可解析边界已过期即排除；全部仍有效的记忆继续可召回 |
| B7 anchor 保留位 | 保留位可重新引入已被普通选择器拒绝的答案，例如错误金额或缺少所问属性 | anchor 与普通候选复用同一准入判定；拒绝后继续找合格候选，诊断只计一次，保留位只影响排序 |
| B8 派生向量快照 | 中断留下半成品无法恢复；调用方未提交数据被导出；旧任务清理可能删除新快照 | 私有临时库完整提交、fsync 后原子发布；锁定构建/恢复；无合同半成品从权威 SQLite 重建；拒绝借用未提交事务；清理核对实际消费的文件身份 |
| G1 回放撤销 | 最新报告被隔离/归档后，active-only 查询退回旧成功报告 | 按当前 scope 与代码摘要读取最新观察后再验 active；撤销立即阻断，新真实执行可恢复，不污染不同身份的证据 |
| G2 回放可评估性 | 缺少判定条件、空样本或不可用检索产生通过；缺失/字符串 evaluable 可混入 | 要求非空正向或负向条件、完整检索和严格布尔证明；保留 executed 与 evaluable 的差别，完整的 negative-only 空召回仍有效 |
| G3 整轮完成性 | 20 次回放有 10 次 unavailable，删掉未知样本后被算成成功的 10/10 | 保留可评估分母作诊断，但未完成样本阻断整轮；readiness 与 closure rehearsal 同步拒绝，不能转成 bootstrap 等待新数据的豁免 |
| G4 渠道落库验收 | OpenClaw 验收忽略记录生命周期、会话身份和真实落库内容 | 要求 active、receipt/session 绑定、精确回读与 payload 一致；拒绝非整数时间戳和非法状态文件，幂等旧记录不能冒充新验收 |
| G5 发布身份竞争 | A 的平台收据写入时被当前 B 的 canonical stamp 重标，失败记录可被当作 B 的证据 | OpenClaw 与 external channel 同时校验原声明、canonical 和 aliases 的 commit/receipt/session；矛盾记录对 A、B 均无效，版本号仍遵循既有描述字段合同 |
| G6 学习缺口关闭 | 字符串“false”、父级失败、次级门禁失败或错误格式被一个通过 gate 遮蔽，错误 resolve 缺口 | 所有显式 gate 必须为 Mapping，显式 null、布尔、数字、字符串、列表均拒绝；其中 verdict 必须为严格布尔且一致。执行、阻断和权威负面证据均阻止关闭；省略字段的兼容格式和后续合法成功仍有效 |

生产路径集中在 `adapters/runtime/service.py`、`api/memory.py`、`knowledge/l1_queue.py`、`cli/l1_worker.py`、`intake/pipeline.py`、`storage/sqlite_store.py`、`retrieval/{engine,incremental_sync}.py`、`evaluation/task_replay.py` 以及 `governance/l5/`、`governance/learning/quality_gap_intake.py`，均位于 `eimemory/` 下。没有扩大跨 scope 授权，也没有降低既有评估门槛。

## 二、身份、兼容与恢复约束

召回策略身份更新为 `governed-recall.v6`，选择器为 `post-fusion-relevance.v4`。排序准入及过期行为已变化，旧策略报告不能自动证明新实现有效。真实回放同时绑定当前 `package_tree_digest` 和执行身份；上线后必须重新产生当前代码的真实证据，不给历史报告补字段、改身份或追认通过。

Hermes 恢复必须使用新的 event。旧 event 重放仍返回它原有的幂等结果，不重新激活历史记录。恢复要求精确 owner/source/target 下的原生删除墓碑、请求摘要和双向链接一致；active、quarantined、superseded、archived 或伪造删除链均不能借旧 removed 历史复活。同事件并发恢复只有一个新实例，不同事件不能制造重复 active 记录。历史扫描上限保留 **32 条，加 1 条探测截断**；超过同 target 上限或无法证明历史完整时，返回 `mutation_history_unavailable`，没有放宽为全表扫描或“未查到即不存在”。

快照是派生状态。仅缺失/未完成合同的旧半成品可重建；完整但 fingerprint 不匹配的快照保留并拒绝，不能悄悄重写成当前合同。队列也不靠清空损坏文件恢复；必须先保留证据并修复状态，随后依靠既有 claim/重试机制继续处理。

## 三、fixture 修复、合同更新与入口清理

以下五项单列，不计为新增生产漏洞：

1. **未配置 catalog**：旧测试把“未安装可信 catalog”误写成“可信 catalog 无 active cases”。现在显式隔离插件与进程 catalog，严格断言 `evaluation_catalog_untrusted` 和 `catalog_not_configured`，保留 nightly 失败传播及关闭重开后的持久化断言。原基线单测失败，修复后通过。
2. **receipt cohort**：release/receipt 重试测试需要先发生真实投递；策略身份变化会改变实验分组。夹具固定 `control_percent=0`，确保该前置动作发生，原收据及发布绑定断言保持完整。
3. **root pycache umask**：测试进程的私有 umask 会将 `mkdir(0555)` 变成 `0550`。夹具创建后显式 `chmod(0555)`，与安装器最终受保护权限相符；缓存和不可信目录的拒绝检查保留。
4. **keyword 来源标记**：原有测试只凭 `_fts_arm_present` 声明就要求 keyword 投票，与基线既有合同不符。阳性夹具补上实际 lexical 证据，并新增缺失/零 lexical 的拒绝控制；生产判定函数与原基线完全一致。
5. **安装器资源探针**：隔离 AST 探针漏掉安装器现有的延迟 YAML 导入入口。补上惰性导入的受控替身后，17 个描述符释放、序列化、flush/fsync 和锁异常子案例完整执行。

另外，旧手写回放 fixture 补充 active/evaluable，零样本阈值案例改为不能验收，队列 ACK 丢失断言改为明确失败，策略版本断言跟随 v6；这些是生产合同变化的对应回归，未删除原业务验证。

统一调用审计还发现未被任何入口调用的 `scrub_producer_credential_environment`。删除该废弃函数及其专用常量/import，并校正两处过时文档。当前 Hermes 合同是每次读取当前 profile 的私有凭据并重验权限，保留 gateway 环境；宿主管理子进程继承边界。现有环境保持、轮换及凭据边界回归用于验证这项清理。同时移除 `run_manual_post_resume_closure` 的过时 wiring 豁免；其真实部署调用已存在，豁免集合随之收紧。

## 四、验证证据

新增回归文件如下，覆盖真实 SQLite 回读、重启、故障注入、并发恢复及合法成功控制：

- [adapter 请求闭环](../../tests/test_adapter_business_closure_20261024.py)
- [L1 与 intake 闭环](../../tests/test_intake_worker_closure_20261024.py)
- [Hermes 恢复闭环](../../tests/test_hermes_readd_closure_20261024.py)
- [记忆过期](../../tests/test_memory_expiry_closure_20261024.py)
- [anchor 准入](../../tests/test_recall_anchor_admission_20261024.py)
- [快照恢复](../../tests/test_vector_snapshot_recovery_20261024.py)
- [治理验收闭环](../../tests/test_governance_business_closure_20261024.py)

定向证据包括 memory/recall 454 passed、治理 13 文件 246 passed、治理最终新增文件 52 passed、intake 231 passed 加 16 subtests，以及 Hermes 扩展组 164 passed；这些组和后续二审彼此重叠，**不得相加为总通过数**。早期分组还保留真实失败日志，供核对修复前后的差异；它们不是最终代码的全部验收结论。

首轮完整集成为 15,813 passed、4 failed、138 skipped，另有 8,819 个通过的 subtests。四个失败对应未接线废弃函数、keyword 旧夹具、零样本旧预期及 YAML 探针遗漏，均继续修复。最终格式复核另新增 90 个真实 gap 生命周期案例，修复前全部失败，关联 7 文件在补强后 248 passed；此增量与首轮全仓明确分开，交付以最终重跑为准。

证据包的 `memory-manifest.json`、`governance-evidence-manifest.json` 记录对应范围、日志/XML 和部分文件摘要；adapter、intake、Hermes、fixture 各有独立 red/green 记录。最终总数、跳过项、环境限制和标准 suite 的退出状态，以本报告开头的结果及 `full-standard-final.log/.xml` 为准。JUnit 的总节点数包含成功的子测试，统计表已将主测试和子测试分开，不能重复相加。

补充结果：最终源码的 Luna 桥接测试 **60 passed（2.17 秒）**；原生受保护 pycache 检查 **4 passed（0.83 秒）**，后者与普通用户全仓跳过集合重叠。7 个新增回归文件含 **249 个用例**，既有 keyword 文件另增 **4 个用例**，均包含在全仓主测试统计中。标准选择排除了 5 个显式 slow 用例；138 个跳过项主要依赖 root 原生权限、真实 Hermes 安装、可选数据集、原生 Windows 或显式 PostgreSQL 测试 DSN，完整原因见验证汇总。

## 五、应用与验证顺序

在独立 checkout 应用交付的完整补丁，不逐个摘取依赖变更。建议先保留现有工作区，以下命令中的补丁路径替换为实际文件：

```bash
git worktree add --detach ../eimemory-closure-20261024 78051a699d75ee33756cefa1f1ab859e1b3662f0
cd ../eimemory-closure-20261024
git apply --check /path/to/full.patch
git apply /path/to/full.patch
git diff --check
python -B -m pytest --strict-markers -q -ra -n 8 --dist=loadfile -m 'not slow' \
  -o tmp_path_retention_policy=none -o tmp_path_retention_count=0 \
  --junitxml=business-closure-standard.xml
python -B -m pytest -q -ra deploy/luna_bridge/tests --junitxml=business-closure-bridge.xml
```

使用符合项目依赖的 Python 环境，安装项目的 pdf/hermes 可选依赖、pytest 与 pytest-xdist，并以普通用户运行标准测试；root 专属检查需另行满足其原生权限和目录前置条件。标准命令与仓库标准回归配置一致，单独标记为 slow 的用例不包含在该结果中。若目标分支已偏离基线，应先处理真实冲突，再运行新增回归和标准 suite，不能用降低断言、删证据或放开生产权限使结果变绿。主线提交和补丁摘要见交付清单；本轮没有执行生产部署。

## 六、验收边界

验证使用离线数据、临时存储、受控依赖和必要的本地接口测试，没有进行真实生产向量服务、线上模型质量、真实平台消费或生产部署验收。`platform_accepted` 仍只表达既有渠道接受合同，不升级为下游业务消费成功。本补丁修复代码层已证实的闭环问题；实际发布仍需当前版本、当前配置、当前 scope 的端到端证据，不能把本地测试结果当作生产 L5 证书。
