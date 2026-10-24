# EIMemory 业务闭环第二轮复审与修复

## 基线与交付范围

- 仓库：`darrowz/eimemory`，主分支：`master`。
- 本轮基线：`64f3db68a8f79d99f2cca5a82f9b9c696d0afb68`。
- 两轮累计补丁基线：`78051a699d75ee33756cefa1f1ab859e1b3662f0`。
- 复审方法：围绕公共入口、真实 SQLite、持久日志、独立连接并发、异常恢复和上层状态消费，先复现问题，再修复并进行交叉检查。
- 本报告对应代码修复与离线验收。生产部署、真实模型质量和外部平台交付验收需要各自的实际运行证据。

## 已确认问题与修复

| 编号 | 问题与业务影响 | 修复后的约束 |
|---|---|---|
| R2-01 | 记录从 A 作用域移到 B 后，SQLite 删除 A，但 JSONL 只有两份快照；重建会复活 A。重复往返、积压导出和过期迁移还可能破坏最终归属。 | 迁移和旧身份退休标记在同一事务进入 outbox；每次实际迁移有稳定事件 nonce；按写入顺序导出记录前缀；重放先退休精确旧身份，再应用新快照。重复操作去重，格式、来源冲突、失效旧身份及已占用目标明确拒绝。 |
| R2-02 | `rewrite` 已提交权威变更后，JSONL 或 Markdown 失败仍向调用方报告写入失败，造成撤销状态误判；提交前中断可能残留事务。 | 沿用已有提交后派生投影合同，已提交权威事实不因派生失败改报失败；提交前包括 `BaseException` 在内的异常回滚记录与 outbox。 |
| R2-03 | 作用域迁移留下旧 Markdown；延迟清理或另一连接在同一地址创建新身份时，旧清理还可能误删新投影。 | 迁移导出清理旧地址前核对当前权威身份；存在待导出项时先取得 SQLite 写事务，让检查、清理和 exported 标记不可被另一 writer 插入。 |
| R2-04 | 导出和备份先截断目标文件，随后日志读取或 manifest 准备失败，会破坏上一份可恢复数据。 | 导出先完成私有临时文件再原子替换；备份使用完整新数据代和原子 manifest 切换，保留在途读者使用的旧代；发布前后异常仍有完整可验证的数据代。继承既有文件归属。 |
| R2-05 | 逻辑备份合并 SQL 与旧日志时，尚未导出的迁移和并发写入会补回已经退休的身份。 | 排空已观察的提交 outbox，再取得写预约并再次检查 pending；同一事务内构造私有快照索引，完成后释放锁。日志恢复共用退休标记解释器，验证操作 ID、来源及两次完整扫描指纹。 |
| R2-06 | L1 队列的错误 scope/channel 字段被默认化，实际消费默认分区父记录并 ACK；缺少 episode 的坏任务消耗重试次数。 | 入队和持久化读取均验证身份字段；显式错误状态在消费前拒绝，不改父记录、不消耗 attempts；严格 JSON 拒绝重复键及非有限值。保留合法 legacy 任务省略 channel/scope 的合同。 |
| R2-07 | Paper candidate 在准备过程中被 reject/quarantine/重新 approve，旧晋升仍提前发布下游并覆盖审核结果。 | 持久候选的 source、extract、claim、page、closure 与最终 candidate 先进入有界批次；最终在单一事务内重新验证完整候选和所有写目标后全部发布。公共审核也采用事务内读改，写失败全批回滚。 |
| R2-08 | 已通过完整质量门禁的缺口仍被自模型解释为弱项，成功 resolution 本身也生成新学习目标；L5 空结果触发较小窗口 fallback，再次引入旧缺口。 | 精确 scope、semantic key 的完整有效关闭链退出当前弱项；resolution 不再解释为失败；已计算的空 weaknesses 列表也是有效结果。有效重新失败的单链只保留当前 tip；人工 reflection、撤销与分叉保持各自证据。 |
| R2-09 | 两个相同失败并发到达会创建多个根；旧通过与新失败交错可产生两个当前 tip，使后续严格读者无法确认关闭。 | 当前链选择、报告去重、生成和追加共享一个权威写事务，记录和 outbox 同时提交，保留 canonical identity 元数据。 |
| R2-10 | 自模型的普通历史条数上限被误当成完整质量链，截断祖先后重新生成已解决目标。 | 普通历史窗口保持有界；被选中的质量组单独补取精确完整证据，在同一读快照验证。证据预算、读取损坏或缺失无法建立可信状态时，明确报告 blocked，并传递到目标生成、学习循环、世界模型和 learn-think。 |
| R2-11 | 两轮新增回归未进入显式列举测试文件的远端业务验证任务；全仓集成还检出本轮新批次模块的发布影响域漏登记。 | 将两轮新增回归文件加入现有 business-closure 任务；新批次模块精确登记 memory.governance、memory.recall、storage.integrity 三个正式发布影响域，接受既有发布门禁。保留操作方关闭全仓远端回归任务的既有设置。 |

## 关键实现决定

### 记录身份与恢复

既有 SQLite 唯一性仍为四个 scope 字段加 `record_id`。同一身份隐式更改 `source_id` 原本就被拒绝；本轮核对后排除了“同 scope/ID 的不同来源覆盖”疑点，没有为这个不成立的场景改变键或投影地址。退休标记额外核对来源，是为了拒绝矛盾的恢复事件。

新日志行保留完整 `RecordEnvelope`，并在顶层增加 `_record_scope_move`，包含 `record_scope_move.v1`、nonce 和精确 `previous_ref`。正常旧日志行仍可读取。重建与备份共享同一个严格解释器；不能让一个消费者删除旧身份，而另一个又把它当作日志补偿数据导出。

修复前已经发生但没有退休标记的历史移动，无法仅凭旧快照安全确定哪一方应退休。本轮没有猜测性删除历史身份。已有受影响数据应根据权威 SQLite 和业务迁移记录定向核对，不能直接把一次重建成功视为历史数据已修复。

### 备份发布与运维兼容

备份格式保持 v1。首代仍使用 `PREFIX.jsonl` 或目录中的 `backup.jsonl`；同一前缀覆盖写使用独立代文件，manifest 的相对 `data_file` 指向当前完整数据。消费者应读取 manifest 或使用 `backup verify`。旧代保留供并发读者使用，由整个备份目录的保留策略负责清理；重复覆盖同一前缀会保留多代数据。

准备和发布前失败保留旧 manifest。最终目录同步报错可能发生在原子替换之后，此时新 manifest 仍指向完整新数据；应先验证当前指针。新代数据继承既有备份 owner，权限保持私有。已通过真实 UID/GID 1001 的文件归属继承测试。

逻辑快照会短暂取得 SQLite 写预约，直到 SQL 与日志合并到私有临时索引，随后释放再流式输出。它不承诺多数据库或外部平台的统一物理快照。详细操作合同见 [operations](../operations.md#logical-exports-and-backup-replacement)。

### 候选准备与正式发布

持久 paper candidate 的批次上限为 512 条记录、前后快照合计 32 MiB。批次读取能看到自身尚未发布的 claims；未知写入入口不直接转发到真实 store。最终对候选、目标快照和幂等身份进行事务内比较，防止过期计算覆盖并发审核。

准备阶段可能留下可复用的内容寻址文件；它们没有正式 record 和召回授权。不能为清理失败尝试而删除可能被其他记录共享的 artifact。没有 record ID 的显式 dict 导入保留原有语义。

### 质量链证据窗口

普通历史窗口和质量链完整性分别处理。严格初选防止损坏行被宽松读取跳过；完整补取必须覆盖最初选中的身份与内容，元数据索引和载荷投影不一致时阻断。质量证据总预算为跨组 10,000 条，恰好用尽与仍有下一条分别判断；超限、损坏或无法建立一致快照会保留诊断，不生成新的学习目标或把空结果当成关闭证明。

## 验证结果

本轮 24 个变更文件包含 16 个生产文件、5 个测试文件、2 份文档和 1 份 workflow。所有代码与测试在同一个冻结快照上验证；测试结束后仅补全本报告，发布前再次核对其余内容和文件模式。

| 验证项目 | 最终结果 |
|---|---|
| 标准全仓集，普通用户 UID 1001，4 个 worker，`not slow` | **15,984 passed、138 skipped，0 failed、0 errors**；586.89 秒，进程退出码 0 |
| pytest 9 子测试 | **8,836 subtests passed**，单独报告，不增加父测试通过数 |
| JUnit 计数核对 | 16,122 个父测试元素；包含子测试的原始总数为 24,958 |
| 默认 testpaths 之外的 `deploy/luna_bridge/tests` | **60 passed、0 skipped**；2.01 秒，进程退出码 0 |
| 本轮 4 个新增回归文件 | **73 个用例全部通过**，已包含在标准全仓结果中 |
| 自动源码清点 | 806 个源文件、9,123 个 callable、264,112 行；745 个文件语法检查正常，61 个原生配置类文件标记为 deferred_native |

新增回归用例分布：

| 文件 | 用例数 |
|---|---:|
| `test_backup_export_review2_20261024.py` | 12 |
| `test_governance_lifecycle_review2_20261024.py` | 14 |
| `test_intake_queue_review2_20261024.py` | 22 |
| `test_record_move_recovery_review2_20261024.py` | 25 |

另加强了既有 `test_monitor_authorized_scopes.py` 中一个失败注入用例：在真实 gap 行和 pending outbox 已进入共享事务后注入异常，验证两者共同回滚、缓存观察可重用、错误不泄露私有内容，且后续重试只产生一条记录。该项没有计入新增 73 个用例。

最终调用使用 Python 3.12.14、pytest 9.1.1、pytest-xdist 3.8.0 和 pytest-subtests 0.15.0，并以普通用户运行。以下为测试参数；实际冻结 checkout、临时目录、环境和摘要见交付包中的 `final-verification-snapshot.json` 与 `final-validation-summary.json`。

```bash
python -B -m pytest --strict-markers -q -ra -n 4 --dist=loadfile \
  -m 'not slow' -o tmp_path_retention_policy=none -o tmp_path_retention_count=0 \
  --basetemp=/tmp/eimemory-review2-final-t4i46qya/pytest-final \
  --junitxml=/tmp/eimemory-review2-final-t4i46qya/evidence/full-standard-final.xml
python -B -m pytest --strict-markers -q -ra deploy/luna_bridge/tests \
  --basetemp=/tmp/eimemory-review2-final-t4i46qya/pytest-bridge-final \
  --junitxml=/tmp/eimemory-review2-final-t4i46qya/evidence/bridge-final.xml
```

标准选择排除了原有 5 个 `slow` 用例：真实 schema supersession 多页排空、超过一万条的导出，以及 L5 v3 baseline 的确定性、生产根目录拒绝和 workload 不可比判定。它们没有计为本次通过。138 个环境跳过项主要需要原生 root 权限/文件系统边界、真实 Hermes 安装和依赖、可选数据集、Windows 描述符、显式 PostgreSQL DSN 或外部 before/after baseline；完整原因逐项保存在日志和最终摘要中。9 条 warning 为 8 条现有测试解压 fixture 的 Python tar 过滤器弃用提示，以及 1 条重复 ZIP 成员负向测试的预期提示。

### 中间失败的处理

第一次全仓运行时，已完成的第一轮 fixture 占用约 17 GiB，本次 fixture 又增长约 11 GiB，32 GiB 临时文件系统耗尽。该次运行退出码 2，原始日志/XML 保留，不能作为最终验收结果。只清理已结束或已停止测试的临时 fixture 后，重新建立冻结 checkout，以 4 个 worker 完整重跑，得到上表最终结果。

中断日志还帮助发现并修正了两个真实集成问题：新晋升批次模块漏登记发布影响域，以及旧监控失败注入仍 patch 已不经过的入口。其余 5 个 assertion 失败在相同旧冻结代码、全新隔离目录中重跑全部通过，没有修改生产代码；原日志不足以逐项断言唯一原因就是磁盘耗尽，因此保留这一归因限制。安装器失败包含明确的 `No space left on device`。最终完整重跑再次覆盖了这些节点，全部通过。

原始红测、中间运行、相关组和最终绿测分别保存；有重叠的定向运行不累加用例数量。最终验收依据为 `full-standard-final.log/.xml`、`bridge-final.log/.xml` 和 `final-validation-summary.json`。

## 补丁与复现

交付提供两份互斥选择的完整 Git 补丁：本轮补丁适用于 `64f3db6`，两轮累计补丁适用于 `78051a6`。同一 checkout 只应用符合其基线的一份。完整交付包还包括所有变更文件的完整内容、文件 SHA-256、两份补丁在干净基线上的实际应用验证、原始日志/XML 和本报告。

所有生产代码与测试均在统一验证快照上执行。最终文档补充后，再核对源文件摘要以及两份补丁应用结果与发布 tree 一致。主线发布采用预期旧 SHA 的非强制更新，防止覆盖并发提交。

## 验收边界

- 标准全仓测试、显式 slow 用例、环境跳过项及额外 bridge 测试分别报告；不把 successful subtests 当成独立父测试增加通过数量。
- 自动源文件清点只用于覆盖范围和可复现性检查，不等于人工逐行审阅整个代码库。
- 异常注入与真实独立 SQLite 连接验证了业务一致性；没有把它们描述为物理断电、生产系统或外部向量服务验收。
- 本轮没有更改形式化质量门槛、用生产数据补造通过回执，或部署生产服务。生产质量状态仍由绑定到真实 release 的实际证据决定。
