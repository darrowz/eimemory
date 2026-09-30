# 1.14.26：召回与事后质量评估分离

## 纠正后的契约与主路径

模型相关性、答案支持判断是事后质量评估，不是访问授权，也不是同步交付前提。

真实调用链为 `MemoryAPI.recall` → `GovernedRecallEngine.recall/_recall` →
`_select_post_fusion_items`。选择有 legacy、LightweightAdmission、RelevanceAdmission
三条路径；raw 辅助路径经过 `search_raw_chunks`；主动交付经过
`ProactiveRecallService.decide`。

本次从这些同步路径移除 caller verifier / verifier 预热、cross-encoder 调用和 raw
模型重排。保留本地检索排序、轻量打分/过滤、去重、数量与时间预算；不改变 embedding
检索配置或模型。内部权威选择通过后的主动交付不再要求模型证明来豁免启发式置信度门槛。
外部 bundle 不享有内部选择的信任豁免。

## 访问与真实性边界

- 仍要求 scope/source 许可、active 状态、权威库 hydration、内容摘要一致及选择后新读取。
- 仍校验片段索引版本/记录片段绑定和 raw 来源、内容完整性。
- 外部 bundle 与 mandatory fallback 复用有界的精确记录复核，拒绝伪造内容、不存在记录、
  跨 scope/source 和已撤销记录；fallback 同样不能绕过授权。
- 仍保留主动会话去重、控制组、条数/上下文预算、持久化和重放校验。
- 普通结果没有 `caller_assistance.proofs`、`verified-parent-span.v1` 或模型支持声明。
  `business_recall_supported` 的证明要求没有放宽。显式 reviewer 的严格证明校验仍保留，
  对应测试作为独立证明兼容测试，不再声称普通召回会调用 reviewer。

## 复用与未接通边界

复用已有 nightly `semantic_relevance_monitor` 与 quality-gap intake，不新增线程、队列、
依赖或调度架构。已有监控读取精确频道的交付账本，仅评估已交付、release-bound、查询与
render digest 可验证的 `memory.recall` / `research.task`，每次最多 8 个新评估，窗口 512。
故障记为 unknown，缓存评估结果，不清空或修改交付账本。

未完整接通的部分：独立 SDK/CLI 召回若没有交付账本，不会自动进入该监控；缺失 release、
原查询或交付确认的调用仍跳过/unknown。已有监控评估相关性、重复及 unanswered，不生成
原 caller verifier 的 parent-span 支持证明。本次没有新增异步证明生成服务，也不把普通
召回成功算作模型证据验证成功。保留的本地阈值/答案属性启发式仍可能漏召回，需后续真实
质量观测；本次不是线上质量认证。

## 验证

先新增 `tests/test_recall_posthoc_quality.py`，在修复前运行得到 **10 failed / 6 passed**：
三种选择模式的 timeout/failure 均丢弃合法候选，真实 MemoryAPI 正例失败，伪造/缺失/撤销
外部 bundle 被错误交付。之后新增 raw 同步调用回归先失败（1 failed），mandatory fallback
真实性回归先失败（3 failed / 1 passed），再各自修复。真实引擎到主动交付回归先暴露置信度
门槛导致空交付（2 failed），修复后确认正常交付、会话去重、有界上下文及无模型 proof。

测试使用临时本地库和受控 provider 故障；不访问生产或真实模型。旧测试中“模型评审必须
同步执行”的断言按新契约更新，权限、撤销、完整性、证明校验用例继续运行。

未提交、推送、部署；未修改生产配置、删除数据或修改其他项目。版本文件同步到 1.14.26，
包括 Python、README、CHANGELOG、两个 Hermes manifest 和 Codex plugin manifest。

最终相关测试组：**485 passed / 1 skipped**；适配器、事后监控及历史召回回归扩展组：
**161 passed**（两组包含重叠文件，不作为互不重复的总数）。PostgreSQL 生命周期集成测试
因未配置 `EIMEMORY_TEST_POSTGRES_DSN` 跳过；没有真实 PostgreSQL/provider 或线上验证。
`git diff --check` 通过。

## 修改文件

- 主路径：`eimemory/retrieval/engine.py`、`lightweight_admission.py`、`relevance.py`、
  `proactive.py`（后三者同目录），以及 `eimemory/raw/retrieval.py`。
- 新回归：`tests/test_recall_posthoc_quality.py`；更新契约/保留证明兼容及监控回归：
  `tests/test_relevance_admission.py`、`tests/test_lightweight_evidence.py`、
  `tests/test_recall_positive_chain_20260924.py`、`tests/test_proactive_proof_delivery.py`、
  `tests/test_proactive_recall.py`、`tests/test_recall_engine.py`、
  `tests/test_semantic_relevance_monitor.py`。
- 版本：`pyproject.toml`、`eimemory/version.py`、
  `integrations/hermes/eimemory/plugin.yaml`、`integrations/hermes/eimemory_hook/plugin.yaml`、
  `integrations/codex/eimemory/.codex-plugin/plugin.json`。
- 文档：`README.md`、`CHANGELOG.md`、本报告。
