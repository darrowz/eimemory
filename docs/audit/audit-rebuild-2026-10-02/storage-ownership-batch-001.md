# 从零模块审计 · 存储所有权第一批

- 基线：`4763001d1c4f3f4af6e6dda17e008e1b4c9b5609`，v1.14.31
- 范围：存储大模块 → 连接/事务/读连接池子模块 → 5 个文件中的 67 个函数
- 方法：只读源码；未导入、启动或执行项目代码，未访问生产数据、模型或凭证
- 计数从此基线重新生成；既有注释中的 FIXED 字样、旧测试名、历史审计数字都不构成本轮通过证据
- 本批第一遍与独立第二遍交集为 67 函数，五个文件的 SHA-256 与复核清单一致。其中三个小文件完成全文双遍，两个大文件仅完成所列范围。没有宣称整个存储模块或整个项目通过

## 分母与未审范围

仓库共有 1,402 个跟踪文件，其中 1,116 个 Python 文件。存储模块有 22 个 Python 文件、21,302 行、722 个函数定义；检索模块有 27 个 Python 文件、12,166 行、449 个函数定义。这里的函数定义包括嵌套 def/async def，不包括 class 声明，不能直接与图索引的混合 definitions 数字相加。

本批覆盖：

| 文件 | 本批范围 | 第一遍函数数 |
| --- | --- | ---: |
| eimemory/storage/runtime_store.py | 44–86、125–187、402–538、631–788、1153–1215、1899–1902 | 25 |
| eimemory/storage/sqlite_store.py | 201–340、7916–7917 | 11 |
| eimemory/storage/recall_deadline.py | 全文件 | 14 |
| eimemory/storage/readonly_recall.py | 全文件 | 3 |
| eimemory/storage/store_access.py | 全文件 | 14 |

存储模块剩余 655 个函数本批未审；检索模块 449 个函数尚未开始。完整函数边界、SHA-256、逐函数状态见 `coverage-manifest.json`。初始化中调用的 schema/migration 实现不因阅读其调用点而算已审。能力/代码演进/归档等领域逻辑不因审过外围事务所有权而算已审。`command_client` 启动 41–67 保持暂停。

## 已确认问题

### A-STO-001 · P1 · 嵌套原子写入口会回滚调用者事务

- 位置：`RuntimeStore.mutate_records_atomically` 646–668；`mutate_capabilities_atomically` 687–717
- 路径：调用者持有同一可重入锁并已开启事务 → 进入这两个入口 → `BEGIN IMMEDIATE` 因已有事务失败 → 通用异常处理无条件 rollback → 调用者尚未提交的数据被回滚
- 契约依据：同文件 `append` 164–167 在 try 外拒绝嵌套事务；代码演进包装器 762–773 使用明确的事务所有权；`read_consistent` 519–528 也保留调用者事务
- 独立源码复核：已确认。复核者在检查前收到候选列表，因此属于独立复核，非盲审
- 建议：两个独占事务入口均在 try 前检查 `in_transaction` 并明确拒绝；不要在未拥有事务时 rollback
- 必要回归：模拟已有事务，断言回调未调用、BEGIN/commit/rollback 均未调用、原事务与待提交内容保留；另验证自有事务成功/失败仍正确提交/回滚
- 修复状态：独立复核通过，专用审计分支已发布 `d1d571e109c99eab6540fd41f8d167e2502f012a`。修复线和复核线分别确认 fake-only 回归修复前 2 fail/4 pass、修复后 6 pass；未运行全套或生产测试

### A-STO-002 · P1 · 实际检索入口可在截止时间保护之前无限等锁

- 位置：`borrow_reader` 458–491，`_ensure_readers` 414–434，`search` 1172–1177，`search_with_diagnostics` 1201–1206
- 路径：先建立检索 deadline，再进入 `borrow_reader`；冷读池初始化、读池禁用或池被占满时，会无期限等待 writer RLock；到拿到连接后才进入 `recall_read_scope`
- 影响：即使剩余检索预算只有很短时间，也可能先等长事务结束；最终返回 degraded 不能弥补已超出的延迟。预热且有空闲 reader 的快速路径不受这个特定等锁问题影响
- 契约依据：`search` 的预算文档，`core/budgets.py` 的硬上限说明；现有 `test_waiting_for_store_lock_obeys_recall_deadline` 只直接测试 deadline helper，未覆盖 `RuntimeStore.search` 的前置借用路径
- 独立源码复核：已确认；尚无本批运行复现
- 建议：将同一个绝对 deadline 传入连接借用/初始化/回退锁获取；预算耗尽统一转为已有不完整结果，不能重新延长预算
- 必要回归：冷池、读池禁用、全部 reader 被占用、writer 被替换方法的回退路径分别验证超时；已有空闲 reader 的读写并发路径继续有效

## 后续需要裁定的所有权风险

1. `execute_readonly` 530–533 返回原始 cursor 时已退出 `borrow_reader`，后续 fetch 不再持有连接租约；当前源码检索未找到生产调用点。应以租约结束后再 fetch 的测试确认接口设计，再决定返回已取出的结果还是受控 cursor
2. `_close_readers` 436–444 清空池后直接关闭全部 reader，不等待 `in_use` 或 slot lock；若与正在消费多语句读取的调用并发，可能使活跃读取在下一语句碰到关闭连接。尚需明确 close/rebuild 与并发读的生命周期契约
3. 原子写包装器仅捕获 Exception；callback 的 KeyboardInterrupt/SystemExit 等 BaseException 路径不会回滚自有事务。须以事务所有权和异常传播测试单独裁定，不能在 A-STO-001 修复中暗中改变行为

## 无新增问题不等于全局保证

- `read_consistent` 的 started 标记保留外部已有事务，并对自己创建的读事务执行 rollback
- `read_code_evolution` 的 owns_transaction 标记保留外部事务
- 可选 `readonly_recall` 在 query_only 设置失败时关闭连接并回退；当前 RuntimeStore 读池未使用此伴随模块。它仍有公开 helper/环境变量契约和专门测试，不能仅因主调用链未引用就删除
- `store_access.locked_read` 在锁内完成 fetch，避免本批发现的原始 cursor 逃逸模式；无 owner 的测试替身回退不代表真正只读 SQL 校验

## 下一批

本批第一/第二遍精确交集已记账。第二批已沿 RuntimeStore → SQLiteCandidateSource → SQLite 搜索候选收集进入检索模块，新增 81 个函数已完成独立复核。累计第一遍 148，双遍 148；第二批重点是预算、降级信息、空结果与错误的区分，以及标识/分区在读写之间的一致性。
