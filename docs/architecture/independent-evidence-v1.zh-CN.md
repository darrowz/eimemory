# 独立原文证据路由 v1：候选实现与验收边界

基线：`2a9c930bd13774635d33b96e4b244c23b76f22d7`。这是**完整架构候选补丁**，不是生产验收证明。
该基线已经包含 Python Luna 分段计时和客户端初始化后的预算扣减，本补丁不重复那些改动。

## 1. 路径与边界

```
现有候选召回 / scope、source 与权威校验
    -> 同一份至多 8 条候选、每条至多 768 字符的可见文本
    -> 独立证据路由
       -> 有当前审核契约且闭合语法唯一匹配：本地原文证据
       -> 可选密码规则证明本次可见集合无支持：no_evidence
       -> 其他情况：原 Python Luna / low 核验
    -> 原有最终权威校验 + 契约最后复核
    -> 原有原文片段投递、在线门禁、宿主确认
```

不更换模型，不建立常驻 Python 池，不改提示词、模型调用参数、候选数、证据阈值或预算，不放宽三秒门槛。
没有问题答案缓存，也没有关键词/向量命中直接放行。治理契约复制的是已经存在的原文片段，不生成新事实。

### v1 真正支持什么

正例支持两类**有限、完整匹配**的查询语法：明确主体的处理流程，以及明确主体的地址、联系人、联系方式。
语法先从原始问题提取主体/关系，再查契约；不从候选内容猜用户的主体，不接受审核者自定义正则或问题列表。
中文 `主体应该怎么处理` / `如何处理主体`、英文 `how should I handle subject` 等是同类语义模板。
未知问法、复合任务、指代、版本、时间/当前状态、否定/排他与未支持属性继续调用原模型。
这些限制意味着**并非所有抖音改写或重启任务问题都会被本地路径覆盖**；不得将未覆盖问题删除出验收集合。

证明依赖操作员对原文的语义审核，而不是声称代码理解了任意自然语言。每个批准包必须有独立审核记录的
SHA-256，并明确确认：原文支持槽位、答案完整、在该语法定义内无未表达条件、已审核同分区冲突。
摘要/hash 只绑定审核材料，不会自动证明审核结论正确。普通记忆 meta 中的 approved 字段没有权限。

## 2. 实现组成

- `evidence_query.py`：受版本约束的完整问题语法；无候选驱动解析。
- `storage/independent_evidence.py`：同一权威 SQLite 中的可选契约、别名索引、分区时钟、撤销及事件账本。
- `governance/independent_evidence.py`：仅本机操作员 CLI：init / inspect / propose / approve / revoke / status；未注册成 RPC 或模型工具。
- `retrieval/independent_evidence.py`：off / shadow / enforce 路由、50ms 本地尝试上限、原验证器回退、最终有效性复核。
- `caller_assistance.py`、默认引擎与轻量引擎：共享验证入口集成，原验证函数 `_verify_candidates` 保持不变。
- `proactive.py`：激活期间不用候选结果缓存；版本包含模式、契约/业务变动及到期戳，以阻止旧持久化决定在重试中复用。
- 精简诊断、原始捕获诊断及验收汇总器：公开固定类别与计时，不公开问题或原文。

选择的原文片段通过既有 `scored` / `fragment_id` / 精确 scope/source 元数据交给当前紧凑输出路径。
不改 record ID，不替换记录正文，不把模型输出拼进原文。宿主仍可能因上下文空间、在线门禁或确认失败而不交付；
此类请求不能被记作三秒内有效完成。

## 3. 权威与失效策略

数据库路径由 `GovernedRecallEngine` 的真实 RuntimeStore 和请求局部 ContextVar 绑定，不从候选 metadata 或额外 root 环境变量取值。
在线路径每次打开只读连接、无数据库写入，不自动迁移，不持有跨模型调用的事务或锁。SQL 参数化，等锁不超过本地剩余预算。
本地尝试上限不是整个请求的性能保证，遇到锁/目录/模式/数据问题转原模型；父级剩余预算仍由原逻辑约束。

显式 init 增加 `ie_v1_*` 表、索引约束和触发器，不改写 records。
同分区任一业务记录（memory/rule/sop/knowledge_page/claim_card/entity_record/relation_record/knowledge_unit/intent_pattern）
插入、修改、删除、改 scope/source/status 都会使旧契约失效，包括没有进入本次 top-8 的新记录。
`memory_edges` 或 `recall_alias_index` 变化使用保守的全局关系时钟；新增这些表却没有相应守卫时，也拒绝本地准入。
审计/召回观察行不是业务契约来源，写入 recall_view 不会让每次召回自动作废契约。

审批冻结分区时钟、关系时钟、authority row 摘要、投影、片段位置、别名、语法版本、到期时间；原文、主体和 source 都必须匹配。
协议最长有效期七天。撤销不可通过重复审批同一个包恢复，必须重新审核新的材料。事务保证别名冲突不会留下一半批准数据。
原记录校验之后再检查一次契约，选择过程中新增冲突、撤销或到期返回 unavailable；不自动重试、不用旧选择补位。

**保守性代价：** 同分区频繁业务写入和全局关系变动可能造成较高失效率。必须观测 `contract_stale`，不能为了命中率
放松时钟。未来细粒度依赖闭包需要另行审核；本版没有声称解决高频变化语料的所有尾延迟。

**长记录边界：** 当前操作员投影接入只支持可直接读取的内联权威记录。`payload_pointer_json` 非空时明确拒绝制作契约，
不猜测分段存储的重建规则。该记录仍走既有模型路径；不得为验收复制/伪造记忆以规避该边界。

## 4. 操作员工作流（均需在实际服务副本验证后按现有部署流程执行）

先确认根目录、备份 SQLite 与审计材料，使用同一安装环境执行。以下命令只是操作说明，本交付未执行它们。

```sh
python -m eimemory.governance.independent_evidence --root ROOT init --confirm-schema-write
```

`reference.json` 是私有文件，只含精确 scope、source_id、record_id：

```json
{"scope":{"tenant_id":"tenant","agent_id":"agent","workspace_id":"workspace","user_id":"user"},"source_id":"source","record_id":"real-record-id"}
```

```sh
python -m eimemory.governance.independent_evidence --root ROOT inspect \
  --reference reference.json --output private-fragments.json
```

inspect 仅生成 mode 0600、拒绝覆盖的私有原文材料；标准输出只有完成状态和摘要。操作员选取真实片段，填写 `spec.json`：

```json
{"scope":{"tenant_id":"tenant","agent_id":"agent","workspace_id":"workspace","user_id":"user"},"source_id":"source","record_id":"real-record-id","intent":"procedure","subject":"原文中的业务主体","attribute":"","aliases":[],"fragment_id":"实际片段ID","expires_at":0}
```

`expires_at` 必须替换为未来不超过七天的 Unix 秒数；示例的 0 **故意不能激活**。aliases 只能取原文明确主体或权威记录既有别名，
不能把某个测试问题及其改写塞进别名列表。fact 类型 attribute 只接受地址/联系人/联系方式。

```sh
python -m eimemory.governance.independent_evidence --root ROOT propose \
  --spec spec.json --output private-proposal.json
# 阅读 private-proposal.json 的完整原文、条件及相同分区的冲突；保留独立审核记录。
python -m eimemory.governance.independent_evidence --root ROOT approve \
  --proposal private-proposal.json --expected-digest ACTUAL_PROPOSAL_DIGEST \
  --reviewer OPERATOR --review-receipt-sha256 ACTUAL_REVIEW_DOCUMENT_SHA256 \
  --attest-original-supports-slot --attest-complete-answer \
  --attest-unconditional-for-grammar --attest-partition-conflicts-reviewed
```

哈希或时钟变化拒绝审批；必须重新制作和审核，不自动重新绑定。propose/approve 并不自动开启线上模式。
操作员/数据库/发布环境属于可信管理边界，CLI 没有另造用户认证服务。不得将 CLI 包装成未授权的 agent 自动工具。

## 5. 模式、诊断及回滚

`EIMEMORY_INDEPENDENT_EVIDENCE_MODE=off` 默认保留原核验。
`shadow` 只观察本地决定，模型仍调用，输出仍由原模型决定；记录 `same_records`/`different_records`/`model_unavailable`，
它们是**记录选择一致性**，不是语义真值、更不是 gold。
`enforce` 才允许当前已审查的本地正例省掉模型。现有 `EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED=1` 保持启用以保留未知问法回退。

`EIMEMORY_RECALL_ATTRIBUTE_PRECHECK=0` 是独立、默认关闭的可选密码预检，不因 enforce 自动打开。只证明
**当前 first-8 / prefix-768 可见候选集合没有可通过既有密码属性规则的引用**，不证明全库没有，也不掩盖未触发时的模型故障。
正例、混合候选、否定/排他及未知问法仍走现有规则；过期请求不转成 no_evidence。
仅启用密码预检也要求已安装并验证 `ie_v1_*` 时钟守卫，以使旧持久化决定失效；缺少守卫则保留模型路径。
契约目录不可读时，主动召回策略戳使用本次新生成的隔离标识，不能用固定 unavailable 戳重用旧决定。

公开诊断路径：`caller_assistance.local_evidence`，包括模式、状态、固定原因、实际耗时、候选数和合规契约摘要。
原文只在私有审核材料和原有证据投递通道出现。新增 schema/lookup 问题不透传 SQLite 消息、文件路径或请求。

紧急回滚先设 mode=off、ATTRIBUTE_PRECHECK=0，按既有受控流程加载配置；必要时回到已验证源码。
保留所有 `ie_v1_*` 数据、审计事件和失败报告，不删除 authority 或重置时钟，不把旧失败改成成功。旧代码可忽略新增表。

## 6. 测试和三秒验收

审查包的隔离检查使用真实 SQLite、实际父进程命令客户端及精确源码片段。候选对象、投影函数和模型结果是明确的测试夹具。
运行完整项目时还必须执行真实 RuntimeStore/RecordEnvelope/默认引擎/轻量引擎/紧凑片段序列化集成测试：

```sh
python -m pytest -q tests/test_independent_evidence_architecture.py \
  tests/test_independent_evidence_repo_integration.py \
  tests/test_python_command_observability.py tests/test_evidence_routing.py \
  tests/test_dense_admission_boundary.py tests/test_lightweight_evidence.py
```

随后按仓库原流程完成完整 Linux 回归、真实 Hermes/Luna 验证及资源争用测试。不得用模拟 API 耗时声明生产达标。
命令层仍使用 gpt-5.6-luna / low；桥接预算修复已在基线存在。OpenClaw 池、连接复用、重试、缓存答案均未加入。

验收每条 JSONL 需提供实测 `elapsed_ms`、独立审核 `correct`、真实交付 `delivered`、`retrieval_status`、`route`，可附 cold/warm phase。

```sh
python -m eimemory.evaluation.independent_evidence --input private-measurements.jsonl
```

汇总器将失败和慢成功都放进分母，报告全部样本最近秩 P95 和“三秒内有效完成率”。固定十二次回归必须十二次都正确且 <=3000ms；
不能删掉模型回退、不交付、冷启动或 unavailable。没有样本为 unknown，不是 100%。模拟测试与人工编造查询不属于自然 gold。
仓库既有至少六十条独立质量数据及其质量/稳定性门槛仍独立适用，本汇总器不替代它们。

## 7. 完成定义

这份补丁实现路由、治理、失效、投递元数据、诊断、模式、回滚和验证入口，**没有预置生产批准契约**。
安装但没有审核契约时，正例仍走原模型。未知/动态/组合问题也仍走原模型，耗时可能继续超过三秒。
因此交付代码不等于性能验收通过。必须回读真实路由覆盖、过期/冲突失效率、API 往返与宿主交付再决定是否发布。
