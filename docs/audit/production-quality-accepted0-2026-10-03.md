# 正式质量 accepted 0 / required 15：只读调查与本地诊断修复

调查锚点：生产 1.14.39，commit `2046f19cd4f7ff4d56035a9410334e6cf76bb283`；
nightly `ref_5e808a083941`。初始 Git 工作区干净。
生产目录只读，SQLite 以 `mode=ro` 和 `PRAGMA query_only=ON` 打开；
没有创建生产 Runtime，没有提交、推送、部署、重启、生产写入或外部 provider 调用。
临时脚本、测试目录和完整原始失败日志均在
`/home/darrow/.hermes/cache/scratch/eimemory-quality/`。

## 证实的断点

该 nightly：accepted 0、required 15、不可评估关闭 179、无标签关闭 1、开放 5；
monitor 为 observation_only，新增 0、复用 0、provider_calls 0。
按该 nightly 的审核写入批次锁定开放 5 条，避免把较早的幂等回执混入本轮队列。

| 链路 | 脱敏结果 |
| --- | --- |
| 原问 vault | 5/5 通过现有加载器的权限边界、原问/effective/host digest 校验 |
| capture | 5/5 通过现有 capture 验证；均为 research.task |
| 候选引用 | 12 条；当前有效且精确来源匹配 11 条，1 条当前不可用 |
| 实际送达 | 12/12 的持久化 ever_injected 为真；这不替代当前 render 重建验证 |
| monitor 触发门禁 | 5/5 的历史 deployment receipt 在当前授权 pin 下不适用 |
| 语义观察 | 4 条案例 missing；1 条 unknown，已有历史 tool_free_transport_unavailable 观察 |
| 独立佐证 | verified-parent-span 格式 0，used 0 |
| 标签接纳 → dataset | 0；不满足 S_sem AND (S_proof OR S_used) |

当前绑定文件的修改时间先于该 nightly；绑定 receipt 的创建时间也先于该 nightly。
该 nightly 时点前的两组 capture 授权范围分别有 64、272 条自然、release-bound、
非 control 决策；其中绑定当前 receipt 的数量均为 0。
因此历史 receipt 适用性是 monitor 的已验证阻断点，零调用不是 provider 故障证据。
对于含当前不可用候选的案例，当前候选有效性还有更早的断点。
已有历史 transport 失败记录只说明那次观察失败，不能解释本轮零调用。

## 最小修复

仅修复静默跳过的诊断缺口：

- `eimemory/evaluation/semantic_relevance_monitor.py`：记录首个未通过的授权门禁原因，
  汇总 `skip_reason_counts`；固定原因码，不输出原问、候选引用、receipt ID 或凭据。
- `eimemory/scheduler/result_contract.py`：真实 nightly 诊断边界保留 skipped_count，
  仅透传固定白名单内的原因计数。
- `tests/test_semantic_relevance_monitor.py`：fake 复现无 receipt、未绑定 release、
  维护样本三种零调用，验证原因贯穿 nightly，决策不变且不生成观察或标签。

没有放宽历史 receipt 适用性、签名、权限、来源或 digest 门禁；
没有改账号、模型路由、阈值、标签生成规则或样本分母。
业务上的证据缺口仍存在，本次修复使其可诊断。

## 评测契约与现有入口

正例自动接纳已进入同一 accepted_case → dataset → 正例 ranking gate，
受 policy、签名、撤销和 digest 校验约束。它只证明选中的 delivered ref
满足正例标准，不能证明所有相关答案都已标注，也不能认证正确无答案或完整自然质量。
未标注候选默认按非相关计分仍是契约限制，不能将其解释为独立验证的负例。

已有 `negative_production_query.accept_negative_query` 人工正确无答案入口；
已有 `accept_pending_production_query` 接纳授权范围内、召回结果之外的人工 gold 入口，
包括空结果漏召回，相关现有回归已运行。没有重复创建流程。
漏召回需要授权范围内答案存在的独立证据；缺送达不等于正确无答案，缺 used 不等于漏召回。
已有 recall_companion 还要求原始上下文重建和自然负例覆盖，正例自动接纳不替代这些条件。

来源保留用于证据追溯和现有显式 source 授权校验，不新增宿主客户端配额或分类门禁；
现有正例契约没有固定每客户端配额。没有把来源名称等同于客户端名称，也没有用
“同客户端”作为相关性证据。跨现有授权边界合并历史数据没有证据支持，本次不扩大权限。

## 精确测试结果与验收边界

使用 `.venv/bin/python`，禁字节码、禁 pytest cache；全部临时目录在上述 scratch。
网络 namespace 被环境拒绝，使用进程 socket 阻断和 Python audit hook
禁止真实网络连接及真实子进程执行，provider 使用测试 fake。

- 红测试：3 failed、51 deselected，0.99s；原始 `red.log` 保留。
- 最小修复绿测试：3 passed、51 deselected，0.98s。
- 首轮受影响回归：234 passed，19.70s。
- 扩展边界检查：2 failed、38 passed，1.60s；原因是继承生产 capture allowlist，
  原始 `boundaries.log` 保留。仅清除测试进程继承的 EIMEMORY 配置后重跑。
- 最终统一回归：274 passed，19.88s，`clean-regression.log`；涵盖 monitor、auto-review、
  dataset、repair、real-query gate、retired workflow、vault、original-query、companion 和 nightly 边界。

尚缺当前适用 release 证据下的自然送达、有效语义判断和独立佐证，以及完整评测所需的负例、
漏召回 gold、原始上下文与质量门禁结果。历史失败、关闭记录和不可用候选全部保留。
fixed smoke `ref_76465b2cdba9` 的 10/10 不能认证自然质量。
本地测试通过不代表生产 0/15 或 L5 已恢复；生产验收需后续真实证据和原有完整门禁通过。

## 续修：主业链路与现有授权的一致性

主业是跨会话持久记忆、授权范围内准确召回，以及由真实经验改善行为。
receipt、签名、标签和版本验收保障经验真实性与权限边界，不能替代真实召回效果。
保留上述初始诊断补丁、原始失败记录与 nightly 锚点数值。

定位生命周期：部署过程 `refresh_bindings` 轮换现有 scope 的精确 receipt pin；
新 caller 的 `current_release_identity` 按运行进程精确 commit 和既有
`authorized_capability_scopes`（产品基础/channel scope 与该 operator 的配置 alias）
解析当前 receipt。历史审核不应替换原 receipt，但仍须有当前访问授权。
跨 scope 的旧 pin 被轮换后，没有独立的可验证历史授权来源；同 tenant 不能补足授权。
因此原 5 条不自动恢复，不新增历史授权、不保留隐式宽限期、不改绑旧结果。

红测试实际复现两处不一致：

- 自然 proactive caller 已通过基础 scope 的显式 pin 绑定 receipt，审核解析却只查
  channel 精确 scope 的 pin，拒绝同一授权 receipt。
- 显式 recall caller 已绑定 operator-pinned receipt，capture 的 release_reference
  却只查 channel/base scope 的直接记录，无法记录该真实调用的证据。

最小修复：`deployment_receipt_for_scope` 复用新 caller 已有的
`authorized_capability_scopes`，每个 scope 内原 pin、digest、tenant、canonical service、
strict ledger 校验不变；显式 capture 复用这一解析器。
不改变任何记忆检索、source 访问或模型路由。
签名 capture 继续验证原 receipt 的身份、来源与摘要，跨版本读取保留原值；
当前版本身份仍须精确 commit/receipt/session，历史审核通过不授予当前质量认证。

只读复查生产 SQLite（`mode=ro`、`query_only=ON`）发现：锚点 nightly 之后，
hongtu 授权 scope 已有 25 条绑定当前 pin 的自然、非 control 决策，时间为
2026-10-03 05:54:01–06:47:28 UTC；另一个 default scope 当前 pin 的决策为 0。
这不是对锚点诊断的覆盖，也不是完整 render、相关性、used 或接纳证明。
不能将旧批次“当前绑定为零”延伸为现在所有 caller 都失配。

评分契约核查：未标注返回项按现有保守 ranking 诊断扣分，保持原始 precision 数值；
它不提供独立负例覆盖。新增回归验证含 gold 与未标注项的 returned_precision 仍为 .5、
false_recall 为 None，缺自然负例时 companion 仍被阻塞。
复用原正确无答案与召回之外人工 gold 入口；不删除样本、不降低阈值，required 15 不变。
达到 15 仍需当前执行、原上下文、自然负例与完整质量门禁。

续修测试均用 `.venv/bin/python`，复用 scratch 内的 socket/audit-hook 离线 runner，
fake provider，禁止真实网络和子进程。两处红测试分别 1 failed，原始日志
`caller-red.log`、`explicit-red.log` 保留。首轮相关回归 229 passed。
扩展回归 315 passed、6 failed；6 项为真实 HTTP/socket 测试，因环境禁止 bind 或
离线 runner 禁止 connect 而失败，`final-boundary.log` 保留，未放开网络。
新增真实自然显式 caller、跨版本签名身份保留、pin 撤销与 unknown/负例区分回归后，
相关进程内 RPC 和新增边界检查 73 passed、6 deselected，`final-offline.log`。
最终统一离线回归 316 passed、6 deselected，25.64s，`final-unified.log`；
覆盖当前 caller、跨版本原身份、签名篡改、授权撤销、越权拒绝、未知观察、负例与人工 gold。
`.venv/bin/python -m compileall -q eimemory` 和 `git diff --check` 均通过；
编译缓存全部写入 scratch。6 项真实 HTTP 测试未验证通过。

真实生产剩余边界：历史跨 scope 审核仍缺当前显式授权；原候选有效性、完整送达重建、
有效语义判断及独立 proof/used 证据仍须真实验证；当前绑定的新自然决策也需走完整门禁。
没有提交、推送、部署、重启、生产写入、不可变 release 修改或外部 provider 调用。
本地修复通过不代表生产 0/15 恢复，也不认证 L5。
