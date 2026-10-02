# 从零模块审计 · 融合排序与查询身份第三批

基线：`4763001d1c4f3f4af6e6dda17e008e1b4c9b5609`，v1.14.31。

## 结果与边界

新增 24 个函数完成双遍源码检查，未建立新的可行动缺陷。独立复核者先收到路径/范围，未先收到候选问题，本批属于盲审。没有执行项目、模型、数据库或测试。

累计 172 个函数双遍：存储 101/722，检索 71/449。尚未审：存储 621、检索 378。两个大模块未完成；按全文范围计，累计九个小文件双遍，大文件仅计精确函数范围。

## 沿调用链的覆盖

- `fusion.py` 全文件 1–191：6 函数
- `query_identity.py` 全文件 1–12：2 函数
- `engine.py` 1352–1640、2156–2244、2259–2270、2273–2287、2290–2349：16 函数，包含 `_dense_rank` 与 `rank_key` 两个嵌套函数

这些范围覆盖候选分组、加权 reciprocal rank fusion、精确记录 token、page pooling、关键词/living 排序辅助和查询摘要。`_authoritative_identity_exists`、完整 admission/证据逻辑、hydration、完整 proactive `decide` 不因读了调用点而算已审。

图节点 `n8514` 的实际调用在 engine.py:1408/1479 进入 `n8572` 融合函数，1558 进入 `n8573` page key；1369 以 `n8540` 生成精确记录 token。proactive.py:361 的查询摘要调用只记为调用点证据，完整方法仍未审。

## 需要保留的行为

1. 融合对 component 重复/未知名显式拒绝，对权重、rank 常数、候选量和输出量有界化；稳定同分排序保留 record token 次序
2. 公共融合函数会截断输入 ID，但当前生产调用先把完整 record/scope/source 编成 `exact-ref.v1:` 加 64 位摘要；在本次核实的调用路径中，不成立“长记录 ID 前缀相同导致融合碰撞”的缺陷
3. page pool 对完整 scope/source、身份类型和原始身份摘要建 key；原始文档/会话标识不能用短前缀替代
4. 只有确有关键词/向量/lifecycle 信息才提供相应排名贡献；不要把缺失分数当零分证据、把混合 provider rank 当独立 keyword arm
5. `_rule_identity` 对特定 active、T0、must-use、ground_truth_behavior_rule 采用明确的语义去重；其他规则仍保留完整记录身份。不能为瘦身删除该例外
6. 查询摘要沿用 `task_type + 单元分隔符 + query` 的既有输入格式；本批没有建立需要变更格式或版本的缺陷

## 验证说明

阅读了当前 `test_recall_fusion.py` 中数学、稳定排序、重复 component、长标识与 raw 复合标识的对应断言。这是测试源码契约参考，不计为执行通过。没有把历史 FIXED 注释、历史测试命名或现有测试数量当本轮修复证据。

函数清单已更新 `coverage-manifest.json`；图覆盖层现含 20 条人工核实调用点边，静态 import 边仍单列。A-STO-001–003 的修复状态单独记录，本批没有新增问题编号或生产改动。
