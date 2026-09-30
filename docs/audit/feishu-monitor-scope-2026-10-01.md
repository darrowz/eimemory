# Feishu 授权样本的事后监控调度：1.14.27（未发布）

日期：2026-10-01。接续 `recall-four-pass-review-2026-10-01.md`。
本次是本地修复与生产只读取证，不是部署、nightly 执行或质量认证。

## 开始状态与边界

首先执行并审查完整 `git diff`：已有项目状态路由与 1.14.27 版本补丁，
6 个跟踪文件，13 行新增、5 行删除；另有未跟踪的前次审计报告和
`tests/test_project_status_route.py`。原路由补丁、测试和报告完整保留。
没有覆盖用户工作，没有提交、推送、部署、重启、生产配置变更或其他项目修改。
沿用 Ponytail；未调用真实模型、未修改模型或 reasoning 配置。

生产 SQLite 均使用 `file:…?mode=ro`、`PRAGMA query_only=ON`，仅 SELECT。
没有构造生产 Runtime；资格核验使用只提供 SELECT 的临时 reader，调用既有纯验证函数。
没有调用 monitor 写观察、capture 写输入、nightly、签名生成或 grant 写入。
没有复制生产数据库或保存查询正文、身份 ID、decision ID、receipt ID、凭据。
各只读探针不是跨整个调查的一致性快照。

## 根因与替代假设的证据

### Scheduler 与授权来源

读取用户级 nightly unit、全部现存 drop-in、`/etc/eimemory/recall.env` 与 settings。
文件声明入口为 `/opt/eimemory/current/.venv/bin/eimemory nightly`，配置目录
`/etc/eimemory`、根目录 `/var/lib/eimemory`，并加载 recall.env。
CLI 经 `hongtu_scope` 构造 base scope；最新已存 nightly 的 scope 确认用户为
operator。`systemctl --user show` 因总线权限被拒绝，故这里区分磁盘声明和实际
manager 生效环境，不声称已经读取了后者。

recall.env 的 `EIMEMORY_CAPTURE_QUERY_SCOPES` 明确列出两个精确范围，以下记为
S1、S2；均为 Hermes 宿主下的授权范围，channel/source 配置都是 hermes，
与 nightly 同 tenant，但两个 user 均与 nightly 用户不同。S2 是前次 A/B/C 的范围。
这来自运维配置，不是聊天内容、客户端名、数据库中碰巧存在的用户或模型推断。
Feishu 是业务消息入口；Hermes 是此批样本的宿主，不应将客户端名称当作用户 ACL。

核实了现有 channel discovery：`resolve_channel_scope` 只改变频道 workspace，
保留 tenant/agent/user；`authorized_capability_scopes` 只允许指定的能力/发布事实共享，
其契约明确将 recall quality/business receipt/quality repair 留在原 scope。
不能将该能力范围转换为任意用户样本的读取授权。本次复用明确 capture allowlist，
没有从全库枚举用户，没有硬编码 user、agent 或 workspace，没有增建权限注册系统。

### 首次丢失边界

`run_nightly_jobs → _run_semantic_relevance_monitor → monitor_channel_deliveries`
原来只展开 operator 的三个频道；底层 SQL 精确匹配 channel/tenant/agent/workspace/user。
因此 S2 在调用 `monitor_deliveries` 之前已丢失，早于 vault、render、模型评审。
“下一次照常运行便会覆盖另一授权用户”不成立。

最新已存 nightly 更新时间仍为 **2026-09-30 12:27:08Z**，早于前次 A/B/C；
没有样本之后的已存运行证据。当前 monitor source 记录总数仍为 **0**。
这同时保留时间因素与确定的调度缺陷，不能从零观察直接推断模型故障。

### 实际记录资格与第二道生产缺口

只从上述配置的两个 exact scope 查询最近最多 512 条决策，未扫描全库发现授权用户：

|读回项目|S1|S2|
|---|---:|---:|
|窗口内决策|77|98|
|release_version=1.14.26|0|18|
|当前版本 vault 经现有 loader 完整验证|0|18|
|当前版本已注入项|0|3|
|已注入项 exact scope/source、active、render 校验通过|0|3|

S2 的 18 条均 research.task、release_bound=1、acceptance_generated=0、单一已授权
source；其中 2 条 control_cohort=1，仍应跳过。仅 16:19:20Z 的 C1 有 3 项注入，
其他当前版本决策没有注入项；它们不能被认证为正确无答案或成功交付。
本轮计数比前次 6 条增加，不沿用旧总数。原始问题没有打印或写入本报告。

RPC 服务另有明确的 private release scope binding 文件，2 个 pin 与 S1/S2 精确匹配。
文件权限 0600，pin 的 receipt 摘要、同 tenant、既有 receipt 身份验证均通过。
在只读 reader 的临时进程环境中带入该**既存**配置后，S2 的 **18/18** release
identity 与样本匹配；移除该变量后 **0/18** 匹配。没有生成新授权或写回任何绑定。

nightly 的已读 unit/drop-in/env 文件没有声明 `EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE`；
RPC 的服务专属 Environment 不会由这些文件自动传给 nightly。由于不能读取 manager
生效环境，不断言不存在其他继承来源；但目前**缺少 nightly 实际持有该绑定的证据**。
所以仅部署本地代码也不能保证产生观察。若按已读文件环境执行，调度修复后还会在
receipt applicability 处 skipped。这是首次 scope 丢失之后的独立缺口，未绕过它。

调查中一次临时探针误用 `commit/version` 与规范 `release_commit/release_version`
比较，曾报告“带 RPC pin 仍不匹配”；校正键名后重新 SELECT 得到上述 18/18。
未把该探针错误当作产品缺陷，未据此修改 receipt 校验。

|替代解释|结论|
|---|---|
|完全没有真实样本|排除：S2 有上述 18 条，C1 有 3 项注入|
|所有 vault 损坏或查不到|排除本批：18/18 loader 通过|
|已注入内容都已变更/撤销|排除本批当前读数：3/3 可验证；不外推历史全部样本|
|当前部署 pin 摘要失效|排除：2 个 pin 均有效|
|nightly 已评审，只是模型失败|无证据；零观察且范围未调度发生在模型之前|
|所有未注入记录都是漏召回|不能成立；背景预取、空交付、控制组须分别对待|
|业务质量已经正确|未证明；没有真实 provider 观察或独立答案标签|

## 最小本地修复

- 从 `query_input_vault` 提取既有 allowlist 校验为共享函数，保持原 capture 行为。
  缺省配置不成为跨用户 discovery；格式无效或超过 100 条时不扩大范围。
- 仅 trusted nightly 入口显式启用 capture discovery；请求级 quality intake
  保留原权限。operator 原三频道路径保留，额外范围必须同 tenant，且 channel 与
  exact workspace 一致。授权中的不同 agent/workspace/user 保持原值，不造默认用户。
- exact scope 去重，多条 source 授权合并；SQL 在读取完整决策和原始查询之前
  排除未授权 source，后续再次检查。按频道累计计数，不覆盖同频道的其他 scope。
- 原预算保持每 exact scope 最近 512 决策、默认最多 8 个新观察；额外最多 100 scope，
  加原 3 scope。无新队列、后台线程、依赖或同步模型评审。
- receipt、scope/source、撤销与 render 检查保持；missing receipt skipped，空交付与
  模型失败仍 unknown，不生成正确无答案标签，不修改既有交付账本。
- 观察仍写原 scope。base quality intake 不自动收割其他用户的 finding；跨范围
  auto-review/质量缺口闭环不是本次补丁宣称完成的能力。

个人/团队/项目明确授权范围内跨宿主共享的业务契约未被缩成客户端隔离；本次仅使用
已有 exact capture 授权作为事后读取边界，没有推导新的跨 tenant 或跨 scope 权利。

## RED → GREEN 与版本材料

先添加 `test_monitor_authorized_scopes.py`：**7 failed / 13 passed**，失败分别证明
授权用户不可调度、撤销前无观察、unknown 分支未触达、receipt 跳过未触达及 scope
去重扩展缺失。实现后首轮该文件与 capture 测试 **32 passed**。
后续额外新增“未授权 source 不能加载完整决策”测试，单项 **1 failed**；将 source
筛选前移到 SQL 后再运行最终 focused 回归。

补充覆盖：未授权/移除授权/非法策略、跨 tenant、异 agent/workspace/user/channel/source、
100 条上限与重复范围、operator 原路径、请求级 intake 不扩权、绑定缺失/撤销与
receipt 撤销/跨 tenant、空交付 unknown、provider 异常、scheduler 异常不改交付。
既有同步召回模型故障回归一起运行，不运行全仓测试。

最终 focused 文件：

```text
tests/test_monitor_authorized_scopes.py
tests/test_semantic_relevance_monitor.py
tests/test_query_capture_scope.py
tests/test_recall_posthoc_quality.py
tests/test_version.py
tests/test_project_status_route.py
```

最终 RTK 汇总 **122 passed，退出码 0**。测试均为本地临时库/受控 provider，不是生产调用。
另用只读取得的现有 allowlist 与已存 nightly scope 驱动本地 discovery，底层 monitor
替换为无 I/O 的记录函数：得到 5 个去重范围，S1/S2 均按原 source 入选，全部同 tenant。
这是当前配置的本地调度验证，不是执行生产 monitor 或写入观察。

版本保持 **1.14.27，未发布**。保留 Python version、pyproject、两个 Hermes manifest
的已有更新；补齐先前遗漏的 README badge/status 与 Codex plugin manifest；changelog
明确 unreleased，新增版本一致性检查。旧审计中 1.14.26 的历史叙述不改写成新版本。

## 未完成的实际观察

没有运行 nightly、真实模型评审、生产回放或发布验收。最新已存运行仍早于样本。
需要后续在授权的运维任务中核实 nightly 生效环境中的既存 receipt binding，再观察
正常事后运行的 exact scope 结果；本次禁止配置变更和执行，所以未做这一步。
默认 8 个新观察预算也意味着不能保证 C1 在第一轮就被处理。
未修复 SDK 无 delivery ledger 的覆盖限制、空结果的反事实答案评估、unknown 重试，
未扩建自动标注或治理闭环。上述缺口不能用本地测试通过、源码已接线或版本号代替。
