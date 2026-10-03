# HGR 首次丢失：本地最小修复（2026-10-03）

本地目标首次丢失已修复；**业务验收仍未通过**。未提交、推送、部署，也未修改生产配置、数据库、索引或服务。父会话负责后续发布决策。

## 边界与证据

- 起始干净 HEAD：`c5da9326469b72b14344c86c143696c0aaa1609b`，1.14.37。
- 已读用户 AGENTS/RTK 指令、CONTRIBUTING 和 README 治理边界。Ponytail 标注的 4.10.1 文件不存在，使用已有 `/home/darrow/.codex/.tmp/marketplaces/ponytail/skills/ponytail/SKILL.md`，遵守先查根因、共享位置最小修复。
- 原始只读证据：`/var/lib/eimemory/logs/release-closure-captures/closure-w26sb5_1/source.bin`。
- SHA-256：`7b2534018e57d0ab734fbd2a2e86012159f490bd408f60265f7f08eabe914879`；结束时重新读取校验一致。
- 生产 SQLite 仅 `mode=ro` / `query_only` 读取、backup 到 `.tmp/hgr/runtime/state/eimemory.sqlite`。Runtime、测试临时目录和重放写入均在本项目内。没有调用生产 Runtime 或外部向量服务。
- 原始库中的目标、recall_index 和 FTS 已读回：`ktop_74ac303313884b0e`，title=`RDI`，active，source=`default`，scope=`default/hongtu/embodied/darrow`；FTS `HGR` 精确目标行存在。目标 updated_at=`2026-10-02T22:50:09Z`。详见 `.tmp/hgr/base-readback.json`。
- 快照取自捕获之后的在线只读库，不声称整个库是捕获瞬间的快照；但原查询的候选数、返回 ID 顺序、属性丢弃数及整套 10 条 smoke 的命中/噪声等原始数值均成功复现。

## 已证实

`generated-ktop_74ac303313884b0e` 的原查询逐字保留在 `tests/fixtures/hgr_known_item.json`，未缩短、替换或追加简单样本。

1. 原查询 intent=`generic`。SQLite 候选、融合、page pooling 后有 18 项，目标位于第 1，keyword/vector 两路均为第 1，RRF=`0.05737704918`，证据含 `keyword_exact`、`vector_match`。
2. `requested_attribute()` 将“**标准的序列**”中的“标准”误认为约束请求，返回 `constraint`。共享 `supports_answer_requirements()` 拒绝实际 HGR 摘要。
3. 目标首次丢失发生于 `_non_exact_grounding_score()` 的属性检查，先于 `keyword_exact` 准入：`[0.0, requested_attribute_missing]`。不是候选截断或排序分数过低。
4. HEAD 重放得到完全相同的 5 个跨主题返回 ID、18→5 筛选和 10 条 `requested_attribute_missing`。跨主题 claim 的“需要”等措辞反而取得属性支持。
5. 修复后候选 ID 顺序保持不变，目标属性为未知/语义路径，grounding=`[1.0, keyword_exact]`，恢复第 1。

## 改动

- `answer_requirements.py`：约束属性需明确询问属性或以属性结尾的名词短语，不再用任意“标准/条件/规则”的出现触发硬性约束门禁。共享修复同时覆盖默认 selector 与 lightweight fragment admission。未知形状继续语义检索。
- `production_recall.py`：保留原 `false_recall` / `false_recall_rate` 及原门禁；新增 `false_recall_reason` 和三个计数，区分 **known-item 未命中但有返回**、**no-answer 有返回**、**no-answer 样本数**，并保留在已脱敏诊断中。不将缺少 no-answer 样本解释为其质量通过。
- 修正既有评价测试的旧断言：样本不足不能变成通过；实际失败优先于证据不足；`unassessed_metrics` 按现有排序比较。HEAD 评价代码先复现 15 个旧断言失败才修正测试。
- 无排序、图扩展、候选数、阈值、索引或权限实现变更；无依赖、版本或生产设置变更。

## 评价契约与非缺陷

- 10 条样本下，错误率最小非零步长是 0.1；`<=0.05` 就是零失败。保留 0.05，不稀释分母。
- 历史 `false_recall_rate` 混合两类错误，**并非纯 no-answer 错误返回率，也不是所有 known-item miss 的比例**（空返回 miss 不进入该历史计数）。新增拆分只解释含义，不重算或抹去历史失败。
- 原失败 `false_recall_rate=0.1` 仍是失败；smoke 不能认证自然质量。即使本地 10/10 命中，`gate_ok=false`、`accepted=false`，状态是 `insufficient`。
- 未发现本次目标缺库、基础索引缺失、scope/source 不符或 intent 错误；这些不作为缺陷修复。
- 安全/完整性门禁（泄漏、污染、payload、执行错误、无效或缺失数值）没有放宽；相关回归仍检查失败优先。

## RED → GREEN

| 固定数据 | HEAD 重放 | 修复重放 |
|---|---:|---:|
| 原始 10 条 smoke | 9 通过 / 1 失败 | 10 通过 / 0 失败 |
| hit@1 / hit@5 | 0.8 / 0.9 | 0.9 / 1.0 |
| false_recall_rate | 0.1 | 0.0 |
| p@3 / noise_rate（原数值诊断） | 0.383 / 0.643 | 0.416 / 0.623 |
| 质量门禁 | failed | insufficient，仍不通过 |

原始数值保存在未改动的 `source.json`、`red-smoke.json` 中；GREEN 是独立文件。

固定对照（不加入原 10 条分母）：

- 原 HGR：目标从未命中恢复第 1。
- 自然改写：“HGR如何把分子的高阶拓扑编码成产生式规则序列，避免标准序列表示的局限？”HEAD 不含目标；GREEN 同源 claim 第 1、目标页第 2。
- 同主题比较：“HGR和标准分子图表示在环系和模体的高阶拓扑编码上有什么不同？”GREEN 仅返回目标页。
- 跨主题近邻：SELF-INDEX 自身查询保持命中其 claim，不返回 HGR；合成固定语料另包含分子图干扰项和 SELF-INDEX 干扰项，HGR 原文/改写都以目标优先且不返回跨主题干扰项。
- 无答案：HGR 许可证价格，RED/GREEN 均空。
- 权限隔离：错误 user、空 source allowlist，RED/GREEN 均空；固定语料另有完全相同正文的外用户/外 source 记录，均不得返回。
- 新定点用例：HEAD **10 failed / 7 passed**；修复 **17 passed**。包括两个使用合成 cosine 的 fragment 共享边界检查，它们不证明生产向量质量。
- 周边组合 **565 passed**；最终定点 **17 passed**，其中 15 条重复，总计 567 个不同用例。周边覆盖默认 engine、intent、共享边界、lexical、task、final binding、authority、评价完整性与 release closure。未跑全仓库测试。

## 剩余阻塞与假设

- **已证实剩余行为**：原查询 GREEN 仍返回 FurE、Frosting 和 `ktop_cf4e953f8b901fb6` 三个跨主题图扩展项。fusion 标注 `graph_path`，selector 的 authoritative graph expansion 分支直接准入，未经过此次属性/grounding 检查。自然改写也有无关尾部结果。因此“目标恢复”不等于“返回集合质量合格”。
- **尚未证实**：这些图边的业务合法性、应如何做关联相关性判断、全量自然查询的精确率及无答案覆盖。本次不猜测图阈值或扩展策略，不修改其实现。
- 未做生产部署或生产原查询修复后的验收；父会话必须把图路径相关性与可信自然查询/负例质量作为剩余业务工作，不能把健康检查、单元绿灯或 smoke 10/10 代替业务验收。

## 复现与本地结果

保留的隔离快照和脚本位于 `.tmp/hgr/`（被 git 忽略；含真实数据，不能随代码提交）。继续复现请使用已有快照，不要重新取样覆盖本次证据。`snapshot.py` 记录了只读 backup 的做法，重新取样会得到另一时间的语料。

```bash
cd /dev-project/eimemory
# 原始 HEAD 属性/评价模块由脚本从固定 c5da932... 加载；不 checkout 或改工作区。
PYTHONPATH=. TMPDIR=$PWD/.tmp/hgr/tmp .venv/bin/python .tmp/hgr/replay.py --head
PYTHONPATH=. TMPDIR=$PWD/.tmp/hgr/tmp .venv/bin/python .tmp/hgr/replay.py
PYTHONPATH=. TMPDIR=$PWD/.tmp/hgr/tmp .venv/bin/python .tmp/hgr/controls.py --head
PYTHONPATH=. TMPDIR=$PWD/.tmp/hgr/tmp .venv/bin/python .tmp/hgr/controls.py
.venv/bin/python .tmp/hgr/verify_results.py
# RED 预期退出 1；GREEN 预期退出 0。
PYTHONPATH=. TMPDIR=$PWD/.tmp/hgr/tmp .venv/bin/python .tmp/hgr/head_tests.py tests/test_hgr_known_item_regression.py -q
TMPDIR=$PWD/.tmp/hgr/tmp .venv/bin/python -m pytest tests/test_hgr_known_item_regression.py -q
```

周边组合命令（最后再加的两个 fragment 用例会使本命令当前计数变为 567）：

```bash
PATH="$PWD/.venv/bin:$PATH" TMPDIR=$PWD/.tmp/hgr/tmp /home/darrow/.local/bin/rtk pytest tests/test_hgr_known_item_regression.py tests/test_recall_engine.py tests/test_recall_intent.py tests/test_recall_shared_boundaries.py tests/test_recall_common_boundaries.py tests/test_recall_lexical.py tests/test_task_recall_repair.py tests/test_recall_final_binding_20260924.py tests/test_recall_positive_chain_20260924.py tests/test_recall_authority_audit_20260923.py tests/test_incident_recall_quality_20260928.py tests/test_production_recall_eval.py tests/test_known_item_quality_contract.py tests/test_release_closure.py -q
```

机器结果：`.tmp/hgr/result.json`。原始/修复阶段追踪：`red-replay.json` / `green-replay.json`；整套 smoke：`red-smoke.json` / `green-smoke.json`；固定对照：`red-controls.json` / `green-controls.json`；RED 定点日志：`red-focused.txt`；HEAD 旧契约失败日志：`head-contract-tests.txt`、`head-eval-tests.txt`。
