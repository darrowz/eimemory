# 1.14.29 召回公共边界：计划与本地验收

> 当前结论以末节“发布前门禁收敛”为准。前文为历次执行快照，失败数量、生产观察与未修项不能视作当前固定事实。

## 执行前最小计划

基线：本地 HEAD f3689657dd194ed1b0ea1ea8a7545a135f747d3c，版本 1.14.28，工作区干净。
现网身份待独立只读核验。使用 Ponytail full。本轮执行模型由父命令固定为 `gpt-6-astra`、reasoning `low`（父会话指定）；本会话未切换模型、未启动子代理或模型裁判。

1. 只读核验发布身份及授权范围内已有证据；仅保留阶段、类别、数量和跳过原因，不输出正文或身份。
2. 参考 Hindsight retrieval 与 TencentDB-Agent-Memory feat/server_team：复用现有多路 RRF、记忆类型和时间字段；对比候选生成、过滤、融合、选择、时间语义。仅借鉴机制，不复制第三方代码、不执行下载脚本。
3. 冻结审计中自然失败的脱敏结构，先运行 RED；仅修已证明的公共边界，保留权限复验、来源分区去重、显式历史与有事实的委派结果。无证据的生产根因明确留空。
4. 独立 focused GREEN 与相邻安全回归；同步 1.14.29 发布材料。报告失败、未验证项及父会话真实验收入口。

限制：不提交、推送、部署、生产写入/重启、模型评估、生产索引配置修改；不新增服务、模型、图谱或依赖。事后质量判断不进入同步召回。
旧 off_topic 仅只读追踪首个跳过原因；没有同链路确证则不新增修复。

参考：https://hindsight.vectorize.io/developer/retrieval ，https://github.com/TencentCloud/TencentDB-Agent-Memory/tree/feat/server_team 。

## 核验与机制对照

`/opt/eimemory/current` 指向 f3689657dd194ed1b0ea1ea8a7545a135f747d3c，其 version.py 为 1.14.28。
这验证发布目录身份，不冒充运行进程健康或实际投递验收。

[Hindsight 官方检索说明](https://hindsight.vectorize.io/developer/retrieval) 将多路搜索经 RRF 融合，时间窗口内仍以相关性挑选。
[Tencent 官方 feat/server_team README](https://github.com/TencentCloud/TencentDB-Agent-Memory/tree/feat/server_team)
描述 L0 原始对话、L1 原子、L2 场景、L3 画像，以及 BM25/vector/RRF 和检索前权限约束。
本仓库已有多路融合、记忆类型、fragment 投影和时间字段；无需复制其服务或层次生成流水线。
仅参考上述机制，没有复制第三方代码，故无第三方代码归属或许可证迁移；没有下载执行脚本。
来源网页是阅读时的分支文档，未将其当作固定代码 commit。

|阶段|本仓库证据与处理|证据上限|
|---|---|---|
|候选生成|`engine.search_scope_groups` provider 有界截断；保留现有预算与搜索|没有同次自然失败的完整 provider hits、新记录索引可见性，未改预算/索引|
|过滤|复用 `is_task_evidence`、类型和权限检查；句内约束复用已有 release-status 实现|自然“验收要求”未落入已支持的属性类型；未用关键词表猜修|
|融合|保留已有 keyword/vector 等多路 RRF，以及 source/scope 分区|缺少生产逐项融合轨迹，不能断言 RRF 为根因|
|选择|确认 task 属性检查曾允许项目身份与另一句状态断言拼接；现在要求同句支持|脱敏通知结构加合成跨项目断言的边界控制，不是原四条投递的逐字回放|
|时间|确认 latest 分支绕过 score gap；改为先按原阈值选相关证据，再按 occurred_at 排序|未增强 recency；occurred_at 不自动升级为可信源事件时间；“现在”分支不受该排序修复影响|

`supports_answer_requirements` 的调用者包括 engine、lightweight admission、独立证据检查和 caller assistance；
在公共函数修复，保留整段 task evidence 排除规则，再做句内身份绑定。没有调用 caller assistance 模型。
LightweightAdmission 的策略标识由 v4 改为 v5，不改变索引投影指纹、阈值或生产配置。
相关性分差检查提前，使 dropped count 包含所有已评分弱项，不再受选满 limit 后提前退出影响。
委派的事实结果仍能通过；无通知字符串黑名单，无项目/版本/问句专用分支。

## 旧 off_topic 的只读追踪

本次只使用 SQLite `mode=ro`、`PRAGMA query_only=ON` 与只读事务，没有构造生产 Runtime。
仅从已有 capture allowlist 的 Hermes exact scope 开始；两范围 monitor 数分别为 0、8。
已存 off_topic 所属范围 gap 数仍为 0；其 decision digest 在同 scope/channel/exact source 的最近 512 条中唯一匹配。
未读取 vault 查询正文或 memory 正文，未调用 monitor、intake、RPC 或模型。

按当前诊断进程的 operator binding 配置调用既有只读 receipt 解析函数：
decision eligibility 通过，首个失败为 `receipt_not_applicable`，未继续将缓存评估当成可注册 finding。
尝试通过只读 `systemctl --user show` 核对现网 RPC 进程配置失败（CalledProcessError）；
因此不能证明本探针配置等于生产服务配置，也不能把此结果定性为现网调度的确定首因。
本轮不改收据、绑定、旧 observation 或 intake。后续需父会话在实际服务环境下只读重验此边界。

## RED → GREEN、失败与复跑

新增 `tests/test_recall_common_boundaries.py`：冻结既有审计中的 conversation/completed-turn/委派容器及历史时间关系，
用 Atlas/Boreal 合成内容、合成分数和时间，不保存真实 ID、embedding、正文或身份。
首轮原生 pytest **4 failed / 3 passed**：两项时间绕过相关性、两项跨句借用状态。
修复后首组 **64 passed**；新增相关性带内保留时间排序及最终撤销复验控制。
这些是结构/机制测试，不是自然问法质量 GREEN。

扩大 focused 首轮 **294 passed / 3 failed**，发现整段“验收标准”排除被句内检查绕过；
已恢复整段 task 支持检查，原始错误保留于本轮工具输出。
修复后完整 focused **295 passed / 2 failed**（退出 1）。两个剩余失败均在
`tests/test_generic_project_query_identity.py`：

- 通用“项目预算与项目进度由谁管理”被既有任务状态路由误判。
- `test_missing_verifier_is_unavailable_not_a_no_answer_verdict` 期待同步 verifier 缺失状态，实际为 `no_evidence`。

将两个相关模块的 HEAD 源码用 `git show HEAD:<path>` 读入独立 Python 进程，执行该文件，
同样 **2 failed / 5 passed**。未修改基线文件；确认两项是基线已有失败。本轮保留，不引入同步模型判断，
也不改既有测试掩盖失败。先前临时诊断不作为持久 artifact；以下续审记录和仓库内测试为交付依据。

复跑完整 focused（不全仓 pytest）：

```sh
env -u EIMEMORY_CAPTURE_QUERY_SCOPES PATH="$PWD/.venv/bin:$PATH" \
  /home/darrow/.local/bin/rtk pytest -q \
  tests/test_recall_common_boundaries.py tests/test_lightweight_evidence.py \
  tests/test_project_status_route.py tests/test_task_recall_repair.py \
  tests/test_memory_core_v1_repair.py tests/test_generic_project_query_identity.py \
  tests/test_recall_lexical.py tests/test_recall_fusion.py tests/test_recall_posthoc_quality.py \
  tests/test_monitor_authorized_scopes.py tests/test_release_scope_binding.py \
  tests/test_query_capture_scope.py tests/test_version.py
```

仅移除测试子进程的生产 capture allowlist 环境变量，使合成 scope 不受现场配置污染；不改生产配置。
新增回归可独立运行：`.venv/bin/python -m pytest -q tests/test_recall_common_boundaries.py`。
最终独立运行 **8 passed，退出 0**；上述完整组仅不包含已确认基线失败文件时，
其余 **290 passed，退出 0**。这与完整组 295/2 的口径分开，不汇总为独立样本数。
`git diff --check` 通过。相邻回归覆盖 RRF、source/scope 撤销、去重、收据绑定和事后评估隔离；没有运行全仓 pytest。

## 未解决项与父会话真实验收入口

**本轮没有修复完成两类真实自然召回失败，不能据本地绿色发布质量结论。**
“发布验收要求”的自然改写返回 Codex 模型偏好仍未修复；“现在进展”返回旧状态/通知的完整根因未证实。
本轮两个公共修复仅覆盖确认的相邻边界。历史标识保留，不认证最新状态。
句内绑定偏保守：项目标题单独一行、后续省略主语的事实可能被拒绝；authority-owned alias 仍沿用原有身份支持规则。
自然语言指代、多主体同句、源时间可信性没有在本次穷尽解决。

父会话独立审查 diff 后，按其权限决定提交推送部署；本会话全部未执行。
七处版本/发布材料同步 1.14.29，旧 CHANGELOG 和旧审计仍保留其历史版本。
真实验收沿前次审计入口，必须使用父会话已有生产写授权：

1. 先核对实际服务 release identity、scope/source/tenant、receipt binding 与运行配置；不能只验 current 软链。
2. 使用既有官方 `eimemory_recall`/memory.recall 入口重放两种自然验收要求、当前项目进展、显式历史和虚构项目控制。
   RPC 会写 decision，本会话不执行。核对实际宿主投递与答案，而非仅非空或带历史标识。
3. 同一次调用仅留脱敏阶段计数：各 provider 是否有目标新旧证据、首个过滤原因、RRF/selector 次序、类型、
   源事件时间可信性、投递类别、去重和权限复验。新状态不在候选池时先查候选/索引可见性，不继续调 recency。
4. 对旧 off_topic，先在真实服务配置只读核实收据适用性；失败就记录，不放宽授权或重评。
   通过所有边界后，父会话才可单独执行已授权的事后 `_run_semantic_relevance_monitor` 验证缓存登记和幂等，
   不运行整套 nightly；其可能覆盖多个允许范围，须沿现有完整授权约束。
5. 事后模型评估与同步召回隔离；本轮禁止模型评估，父会话是否获此权限须以其授权为准。
   空投递 unknown 不算自然成功，受控否定不充当自然样本。


## 续审：有界身份绑定与独立 baseline（未发布）

此前章节记述首轮结果；本节取代“句内绑定已足够”和模块替换 baseline 的结论。
最小顺序为先审查公共函数及四类调用者，完整 HEAD baseline，冻结多行正负控制 RED，再修改公共绑定并跑 focused。
没有生产读写调用、RPC、模型评估、提交、推送或部署；版本仍为未发布 1.14.29。

### 本轮补丁与边界

公共 task/release 支持检查改为有界上下文：明确的独立项目标题（允许 Markdown 标题、冒号），
仅向紧邻一行、合计不超过 512 字符且以已有状态/历史断言开头的事实传递身份。
不新增问句词表，不猜项目别名，不修改向量分数、RRF、索引投影或权限。
空行、句号、通知中间行、另一明确项目、另一主体开头均不继承；同句存在另一明确项目也拒绝。
沿用整段 task evidence 排除，历史可查、有事实委派结果仍保留。
这是保守语法界限，不是通用指代消解：例如“标题\n检索修复已完成”的名词开头不继承，
“标题\n已完成检索修复”可继承；任意自然语言嵌套主体仍非本补丁已解决范围。
现有 authority-owned aliases 语义未扩大。权限、source/scope 撤销、完整性 unknown 均未改写。

### 真正独立的 baseline

本轮将 `git archive f3689657dd194ed1b0ea1ea8a7545a135f747d3c` 的完整树解包到
`/home/darrow/.hermes/cache/scratch/recall-1.14.29-review/baseline`，在该目录执行
`env -u EIMEMORY_CAPTURE_QUERY_SCOPES /dev-project/eimemory/.venv/bin/python -m pytest -q tests/test_generic_project_query_identity.py`。
结果 **2 failed / 5 passed，exit 1**。使用同一已安装测试解释器，源码/测试/配置均来自完整 HEAD；
没有 sys.modules 注入或模块替换。这只验证两个失败的 baseline，不冒充全仓 baseline。

1. 泛指“项目预算与项目进度由谁管理”：explicit_project 已为空，但 task_recall_mode 误路由 status，
   状态事实检查拒绝职责说明。分类为已有意图路由缺陷。
2. verifier 缺失：空候选返回 no_evidence，测试期待 unavailable。分类为已有状态契约不一致；
   不能仅据此断言测试过期，也不通过恢复同步模型判断消除失败。

可独立复跑 baseline：在 scratch 创建新的空目录，以 Python subprocess 获取上述 git archive，
用 tarfile 解包完整树；在解包目录运行上面的命令。scratch 是临时工作区，不是持久交付链接。

### 统一诊断与生产阻塞

“发布验收要求”自然改写混入 Codex 偏好与“现在进展”混入旧状态/通知，统一按以下链路验收，
不为每种问法加关键词。复用报告已有腾讯 L0/L1/L2/L3 分层思路，将原始对话、原子事实、场景与画像的
角色区分；复用现有 Hindsight 式多路相关性融合，而非让最新时间自动覆盖相关性。

|阶段|所需同链路证据|当前能证明什么|
|---|---|---|
|候选|自然改写对应的目标要求/新状态是否进入各 provider|没有完整生产 trace，不能确认候选或索引根因|
|类型/过滤|偏好、规则、事实、通知各自首个通过/拒绝原因|任务状态已有类型门；自然验收要求属性仍未覆盖，未用词表猜修|
|融合/选择|同次请求的多路位置、片段、分差与最终选择|本地证明相关性带与身份边界；不能证明原案例投递改善|
|时间/权限|源事件时间可信性、完整性、最终 scope/source 撤销复验|保留现有约束；unknown 不提升为成功，历史不提升为当前|

**真实自然验收仍阻塞**：当前可用审计未包含足够生产 provider→过滤→融合→投递同链路 trace。
本轮没有读取新的生产正文或凭据，不能把旧聚合数量充当根因证据；未证明两类原案例已修。
父会话审查后，须在已有授权 scope 内补齐上述脱敏 trace；真实 recall 会写 decision，留给父会话授权验收。
无须也不允许为了本补丁在同步召回中加入模型裁判。不得仅凭本地 GREEN 发布。

### 本轮验证结果与独立命令

最终绑定 fixture 在恢复旧句内路径时 **7 failed / 16 passed，exit 1**，恢复新路径后 **23 passed，exit 0**。
这次路径切换仅用于 RED 证据，不是 baseline 复现。完整 focused（上文同一命令）
**310 passed / 2 failed，exit 1**，剩余失败与完整 HEAD baseline 一致。
随后增加三个 fragment projection → LightweightAdmission 集成控制，独立文件最终
**26 passed，exit 0**；没有将不同测试运行相加或声称全仓通过。`git diff --check` 通过。

```sh
.venv/bin/python -m pytest -q tests/test_recall_common_boundaries.py
```

完整 baseline 可从仓库根目录独立重建，无需依赖本机已有解包目录：

```sh
.venv/bin/python - <<'PY'
import io, os, subprocess, tarfile, tempfile
from pathlib import Path
python = str(Path('.venv/bin/python').absolute())
root = Path('/home/darrow/.hermes/cache/scratch')
with tempfile.TemporaryDirectory(prefix='recall-baseline-', dir=root) as directory:
    archive = subprocess.check_output(['git', 'archive', 'f3689657dd194ed1b0ea1ea8a7545a135f747d3c'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(directory, filter='data')
    env = dict(os.environ)
    env.pop('EIMEMORY_CAPTURE_QUERY_SCOPES', None)
    result = subprocess.run([python, '-m', 'pytest', '-q',
                             'tests/test_generic_project_query_identity.py'],
                            cwd=directory, env=env)
    raise SystemExit(result.returncode)
PY
```

持久交付为本报告、源码 diff 与仓库测试；本机临时日志和 scratch 路径均不作为持久 artifact。

## 父会话阻塞续修：最小计划（1.14.29 草稿）

1. 原生检查 requested_attribute、query identity；三个确证均为 True，属性为空，未进入 task 标题绑定。
2. 冻结三个确证、紧邻汉字的版本识别、自然要求与模型偏好混合记录、主体正反控制为 RED。
3. 在公共属性/身份入口最小修复，复用已有 constraint 类型语义；保留未知属性语义路径和合法标题绑定。
4. 完整 focused 与正反控制，审查 diff；明确只读生产配置绑定入口及其证据上限。

继续禁止提交/推送/部署/生产写入或重启、同步模型评审及授权门槛变更。
不删除历史；occurred_at 是否为源事件时间仍未验证，时间排序不证明当前状态恢复。

### 确证、修复与 diff 审查

三个原始确证在本次编辑前原生执行：requested_attribute 为 `''`，explicit_project 为 `Alpha`，
identity 与 supports_answer_requirements 均为 True。第一误收边界是未知属性通用路径；
不是 task 标题继承。版本表达式的 Unicode `\w` 边界漏掉紧接汉字的版本，
项目前缀也未完整识别无空格的 `项目Alpha`。无版本约束时，整段实体出现便足以走未知属性路径。

修复在公共入口：版本用 ASCII 标识符边界；前缀项目支持紧邻 ASCII 名称；
要求/规则/条件等明确属性映射为已有知识类型 `constraint`（原 requested_attribute 中没有此分支，
这里是新增属性检查，复用既有类型语义，并非宣称已有可直接调用的约束检查器）。
同一要求识别供 task_recall_mode 使用，防止含进度/acceptance 的要求问题进入事实类型过滤。
约束断言复用有界主体绑定；问题句不提升为断言。未知属性继续保留语义路径。
没有项目、版本或整问句黑名单；已有标题绑定正例仍通过。

审查注意：这是保守的明确属性/语法检查，不是通用意图或指代模型。
同一项目内不同子主题的约束、多主体复杂复句、没有显式标记的隐含要求仍未穷尽；
不得据此宣称所有自然问法都恢复。没有修改授权门、阈值、索引、历史记录或同步模型路径。
既有 latest 改动只在相关性带内排序；occurred_at 是否为源事件时间仍未验证。

### 本轮测试口径

- 修改前新增 RED：**16 failed / 30 passed**，包括三个原始确证、版本约束、跨项目约束、自然要求与偏好混合容器。
- 修改后补齐正反控制，独立文件：**55 passed，exit 0**。
- 上文原完整 focused 命令最终：**342 passed / 2 failed，exit 1**。两项失败仍是泛指职责路由与 verifier unavailable/no_evidence 契约。
- 追加 `test_dense_admission_boundary.py test_evidence_routing.py test_source_partition.py test_recall_local_work.py`：
  **397 passed / 19 failed，exit 1**。不是全仓运行，不与前组累加。
- 本轮再次 `git archive HEAD` 完整解包隔离目录，仅运行 `test_generic_project_query_identity.py test_evidence_routing.py`：
  **13 passed / 19 failed，exit 1**，相同失败集合。新增 17 项属于既有 verifier 路由契约失败；没有修改测试或恢复同步评审来掩盖它们。
- `git diff --check` 通过。版本保留 1.14.29 Unreleased，现网仍按已核验的 1.14.28 记录，本轮未再认证运行进程身份。

### 父会话下一步：生产配置绑定的只读 SQLite 诊断入口

下列代码是一次性诊断入口，复用现有 `SqliteRecordStore.search_with_diagnostics`，不新增服务或配置。
在父会话已获授权的诊断 Python 进程中运行；`pid` 必须来自已核验的现网服务 MainPID，
不能猜 PID 或只使用 current 软链。`request` 使用父会话已经核实的授权 exact scope/source/filter 与自然查询，
不是授权来源；不得把任意输入 JSON 当作读取授权。不要输出 request、环境、原始 report 或异常详情。

绑定顺序：读取该 PID 的 EIMEMORY 环境（只留内存）→ 现有 load_settings → 已有配置的 root →
核验该 PID 确实打开同一 SQLite → mode=ro + query_only + 只读事务 → 既有 search_with_diagnostics。
缺 PID 读取权限、配置路径、数据库 FD 匹配或 schema 不兼容即停止，不猜默认目录、不运行迁移。
配置文件若在进程启动后变更，仍需核对实际加载配置；/proc environ 不证明进程后续内存覆盖值。
不在生产路径构造 RuntimeStore/SqliteRecordStore：其构造器会建表和迁移。
这里仅诊断适配器跳过构造器，用原方法读取既有投影；不注册为运行时 API。

将下面函数和调用放在同一个 Python 进程执行（本轮只验证合成数据库，没有执行生产调用）：

```python
from collections import Counter
from pathlib import Path
import sqlite3
from threading import RLock
from eimemory.models.records import ScopeRef
from eimemory.storage.sqlite_store import SqliteRecordStore
from eimemory.retrieval.answer_requirements import supports_answer_requirements


def probe(db_path, *, query, scope, source_ids, recall_filters):
    if not source_ids or not isinstance(scope, ScopeRef):
        raise ValueError('explicit authorized scope and sources required')
    # Diagnostic-only adapter: do not run the schema-writing constructor.
    store = object.__new__(SqliteRecordStore)
    store.path = Path(db_path).resolve(strict=True)
    store.conn = sqlite3.connect(store.path.as_uri() + '?mode=ro', uri=True)
    store.conn.row_factory = sqlite3.Row
    store._schema_migration_cache = {}
    store._recall_identity_ready_cached = None
    store._recall_schema_verified = False
    lock = RLock()
    store.bind_runtime_lock(lock)
    try:
        store.conn.execute('PRAGMA query_only=ON')
        store.conn.execute('BEGIN')
        with lock:
            records, report = store.search_with_diagnostics(
                query=query, kinds=['memory', 'rule'], scope=scope, source_ids=source_ids,
                limit=20, recall_filters={**recall_filters, '_exact_scope': True})
        return {
            'candidate_count': report.get('candidate_count', 0),
            'blocked_counts': report.get('blocked_counts', {}),
            'returned_count': len(records),
            'types': dict(Counter(r.kind for r in records)),
            'attribute_supported_count': sum(supports_answer_requirements(
                query, '\n'.join([r.title, r.summary, r.detail,
                    str(r.content.get('text') or '')]), r.aliases) for r in records),
        }
    finally:
        store.conn.rollback()
        store.conn.close()

# pid, request 由父会话已授权的诊断入口传入，不能打印。
import os
from eimemory.config.loader import load_settings

def production_sqlite_diagnostic(pid, request):
    proc = Path('/proc') / str(int(pid))
    service_env = {}
    for entry in (proc / 'environ').read_bytes().split(b'\0'):
        key, separator, value = entry.partition(b'=')
        if separator and key.startswith(b'EIMEMORY_'):
            service_env[os.fsdecode(key)] = os.fsdecode(value)
    if not any(service_env.get(k) for k in (
            'EIMEMORY_ROOT', 'EIMEMORY_CONFIG_PATH', 'EIMEMORY_CONFIG_DIR')):
        raise RuntimeError('explicit production configuration unavailable')
    # Only this disposable diagnostic process changes its environment.
    for key in list(os.environ):
        if key.startswith('EIMEMORY_'):
            del os.environ[key]
    os.environ.update(service_env)
    os.chdir(proc / 'cwd')
    db = (load_settings().root / 'state/eimemory.sqlite').resolve(strict=True)
    opened = []
    for fd in (proc / 'fd').iterdir():
        try:
            opened.append(fd.resolve(strict=True))
        except FileNotFoundError:
            continue
    if db not in opened:
        raise RuntimeError('production database binding unverified')
    return probe(db, query=request['query'],
                 scope=ScopeRef.from_dict(request['scope']),
                 source_ids=request['source_ids'],
                 recall_filters=request['recall_filters'])

# summary = production_sqlite_diagnostic(pid, request)
# print(summary)  # only aggregate counts; no identity, query or evidence
```

本地合成非空 smoke：candidate_count=1、returned_count=1、attribute_supported_count=1；
另有 quality_rejected 和空候选控制，均能只读退出。配置/PID 绑定段未在生产执行，不能称已完成生产绑定。
公共属性计数是对返回紧凑记录的诊断，不是 fragment admission 或业务验收。
这个 API 的 vector_hits 是 SQLite 本地评分，**不是 PostgreSQL dense provider trace**；
scored_items 也只含所选项。禁止将其计数解释为完整 provider→融合→投递轨迹。
下一步先取得这个同 scope 的 SQLite 首个过滤计数，并与已有 PostgreSQL source diagnostics、
最终 admission 和宿主投递 trace 对齐。不存在同链路 trace 时继续保留“原案例恢复未证实”的阻塞，
不新建服务、配置或用时间排序冒充当前状态恢复。RPC 重放会写 decision，仍不属于本轮执行范围。

## 最终续修：版本标题继承与原 19 失败分类（未发布）

本节为本轮最终状态，取代前文“职责路由保留不修”和“版本标题不能继承”的结论。
沿用已有 1.14.29 草稿，没有提交、推送、部署、生产读写/RPC 或模型评估。
本轮只改公共绑定、职责路由及对应测试/发布说明；没有新增依赖、同步模型或项目专用词表。
临时测试目录和 RED 日志均位于 `/home/darrow/.hermes/cache/scratch`；测试禁用 bytecode 和 pytest cache。

### 阻塞复现和最小修复

独立审查原例：`q=Alpha v1.14.29的验收要求是什么？`，
`e=Alpha v1.14.29\n必须先完成回归测试。`。
属性已为 constraint，项目已为 Alpha；失败发生在公共绑定仅接受 `_PROJECT.fullmatch(heading)`。
同句控制通过，不能据此证明多行通过。

编辑生产源码前新增矩阵，原生 pytest 真实 RED 为 **19 failed / 100 passed**。
其中 16 项是版本标题正例，3 项是职责路由；错误版本、缺失版本、跨主体和非相邻控制通过。
首次修复后 **115 passed / 4 failed**：4 项状态矩阵拼接成了“Alpha v1.14.29的项目现在进展如何”，
既有 explicit_project 将“29的”当作项目名。本轮没有扩大身份解析来处理这一额外问法，
改为“Alpha v1.14.29的任务现在进展如何”以隔离标题继承测试；该额外问法仍是已知限制，不记为已修。

复用 `_VERSION` 从紧邻独立标题提取版本及项目部分，仍要求标题自身完整满足查询身份。
无项目标记的 `Alpha v1.14.29` 也只在明确版本和项目精确匹配时成为标题。
标题只向紧邻、已有事实/约束/历史断言开头的一行传递身份，总长度仍限 512 字符。
断言显式出现不同版本时拒绝；其他段落的匹配版本不能补齐标题缺失版本。
空行、句号、通知夹行、另一主体、另一项目以及超长文本仍拒绝。
公共支持入口四类调用者不变；fragment projection → LightweightAdmission 集成矩阵也覆盖版本匹配/不匹配/缺失。

职责是不同于状态的请求属性：在 task_recall_mode 的公共意图入口排除明确职责问句，
沿用 unknown 属性的语义路径，而不强迫职责说明提供“已完成”等状态断言。
原“项目预算与项目进度由谁管理”测试保持原样并通过；未知属性仍受原项目/版本身份检查约束。
这不是通用语义理解，复杂多主体复句、非相邻指代、名词开头的省略主语仍不作支持保证。

### 原 19 项失败的完整归类

原完整 HEAD baseline 的 19 失败见前节；本轮直接复跑原两个测试文件，结果为
**14 passed / 18 failed，exit 1**。没有删除、跳过或改写其中测试，也没有把失败统称为未分类。

|原失败组|数量|本轮结论|
|---|---:|---|
|generic_project_query_identity：项目预算与项目进度由谁管理|1|真实旧意图路由缺陷，已修，原测试通过|
|generic_project_query_identity：missing_verifier_is_unavailable_not_a_no_answer_verdict|1|同步 verifier 缺失应 unavailable 的旧预期与当前 no_evidence 不一致，仍失败|
|evidence_routing：dense_reaches_verifier_once_without_lexical_admission|1|期待同步 verifier 挑选，当前本地准入空结果，仍失败|
|evidence_routing：rejection_never_falls_back_to_dense 的四种 payload|4|期待同步调用 1 次，实际 0 次，仍失败|
|evidence_routing：lightweight_dense_before_lexical_gate|1|期待低本地证据经同步 verifier 入选，当前未入选，仍失败|
|evidence_routing：absent_verifier_does_not_certify_dense_no_support|1|unavailable/no_evidence 状态契约不一致，仍失败|
|evidence_routing：final_authority_recheck_and_candidate_budget|1|以同步 verifier 回调触发撤销及候选预算检查，当前没有该同步调用，先在状态断言失败|
|evidence_routing：host_measurements_do_not_decide_support 八种参数|8|选择断言通过后，期待调用 1 次而实际 0 次，仍失败；不是新模型质量评估|
|evidence_routing：lightweight_absent_verifier_does_not_certify_semantic_absence|1|unavailable/no_evidence 状态契约不一致，仍失败|

以上 18 项统一归为同步 verifier 契约不一致，不据此恢复同步模型，也不宣称契约债务已解决。
`no_evidence` 只表示本地未选出证据，不是语义不存在或自然验收成功；完整性 unknown 语义未提升为已知。
权限撤销已有相邻安全回归继续覆盖，不能把依赖未执行 verifier 回调的旧测试说成通过。

### 最终验证与直接复跑命令

- 独立公共边界文件：**122 passed，exit 0**。
- 扩大 focused 全部保留：**465 passed / 18 failed，exit 1**。失败集合全部属于上表；不是全仓 GREEN。
- `git diff --check` 通过。版本文件、包、三个集成 manifest、README、CHANGELOG 维持 **1.14.29 Unreleased**。
- 原自然“验收要求”与“当前状态”仍缺同链路 provider→过滤→融合→投递 trace，**如实未验**；
  本地受控 fixture 不能替代真实验收。由父会话按既有授权完成真实验收，本轮没有调用生产 RPC。

以下从仓库根目录执行，所有临时文件固定在 scratch。pytest 自行清理其对应 basetemp；
没有修改生产 capture 配置，只在测试进程移除 capture allowlist。

```sh
export TMPDIR=/home/darrow/.hermes/cache/scratch
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$TMPDIR/recall-1.14.29-final"

# 独立 GREEN，预期 122 passed / exit 0。
env -u EIMEMORY_CAPTURE_QUERY_SCOPES PATH="$PWD/.venv/bin:$PATH" \
  /home/darrow/.local/bin/rtk pytest -q -p no:cacheprovider \
  --basetemp="$TMPDIR/recall-1.14.29-final/independent" \
  tests/test_recall_common_boundaries.py

# 全部 focused，预期 465 passed / 18 failed / exit 1，保留契约失败。
env -u EIMEMORY_CAPTURE_QUERY_SCOPES PATH="$PWD/.venv/bin:$PATH" \
  /home/darrow/.local/bin/rtk pytest -q -p no:cacheprovider \
  --basetemp="$TMPDIR/recall-1.14.29-final/final" \
  tests/test_recall_common_boundaries.py tests/test_lightweight_evidence.py \
  tests/test_project_status_route.py tests/test_task_recall_repair.py \
  tests/test_memory_core_v1_repair.py tests/test_generic_project_query_identity.py \
  tests/test_recall_lexical.py tests/test_recall_fusion.py tests/test_recall_posthoc_quality.py \
  tests/test_monitor_authorized_scopes.py tests/test_release_scope_binding.py \
  tests/test_query_capture_scope.py tests/test_version.py \
  tests/test_dense_admission_boundary.py tests/test_evidence_routing.py \
  tests/test_source_partition.py tests/test_recall_local_work.py

# 原生完整失败诊断，预期 14 passed / 18 failed / exit 1。
env -u EIMEMORY_CAPTURE_QUERY_SCOPES .venv/bin/python -m pytest \
  -q --tb=short -p no:cacheprovider \
  --basetemp="$TMPDIR/recall-1.14.29-final/contracts" \
  tests/test_generic_project_query_identity.py tests/test_evidence_routing.py

git diff --check
```

## 最终独立审查（2026-10-01）

- 审查范围：未提交工作区相对现网/基线 `f3689657dd194ed1b0ea1ea8a7545a135f747d3c`（1.14.28）的完整 diff；只读审查，未提交、推送、部署、重启或调用生产 RPC。
- 版本化标题：`_VERSION` 已改为 ASCII 标识符边界，支持紧邻中文的 `Alpha v1.14.29`；`_PROJECT` 支持 `项目Alpha`。`_supports_bound_attribute` 只允许明确标题向紧邻、有限长度且以事实/历史/约束开头的一行传递身份，并拒绝显式跨版本/跨主体、空行、通知夹行、超长文本。`test_versioned_heading_attribute_matrix` 将“项目现在”改成“任务现在”是合理的隔离：此前问句会被 `explicit_project` 误解析为“29的”，属于独立身份解析问题，不应借矩阵改变公共解析语义。
- fragment admission：新增版本匹配、版本不匹配和缺失版本的投影→`LightweightAdmission` 正反控制；权限撤销复验、来源分区去重与完整性边界仍保留。独立边界文件本次实跑 **122 passed, exit 0**。
- task_queries：明确约束/要求及职责问句在公共意图入口返回空路由，避免进入 task-state/history 事实过滤；职责问句未被扩展成状态属性。旧同步 verifier 路由未恢复。
- focused 实跑命令（`.venv/bin/python -m pytest -q -p no:cacheprovider`，去除测试进程 capture allowlist）结果：**136 passed, 18 failed, exit 1**。18 项均落在既有同步 verifier 契约/状态语义：缺失 verifier 的 `unavailable` vs 当前 `no_evidence`，以及期望同步 verifier 调用而当前路径调用 0 次；不是本 diff 新增的版本标题/约束边界失败。
- 发布阻塞：**有**。候选 focused 集合非绿，且自然“验收要求”改写、当前进展/旧通知的生产 provider→过滤→融合→投递链路没有本轮真实证据；不能把本地合成测试当生产召回验收，也不能以历史模型偏好/旧状态现象宣称已修复。
- 无新增阻塞：版本材料（`version.py`、`pyproject.toml`、README、CHANGELOG、三个 integration manifest）一致为 1.14.29 Unreleased；`git diff --check` 通过；本轮未发现跨主体版本匹配控制、constraint 识别、task_queries 路由新增失败。现网仍保持 1.14.28，未部署。
- 真实目标未证实：自然语言改写召回、当前真实项目进展、源事件时间可信性、生产 scope/source 绑定下的 provider 候选可见性与最终投递，均未证实。不得继续用无限词表扩展替代同链路 trace；保留安全权限撤销/来源完整性控制，不恢复同步模型评审。



## 发布前门禁收敛（2026-10-01，未提交）

**给父会话的结论：本地 focused 代码门禁已绿；整体发布质量门禁仍阻塞，不能宣称可部署验收通过。**
版本保持 1.14.29 Unreleased。本轮仅本地源码、测试、报告；没有提交、推送、部署、生产写入、生产 RPC 或真实模型调用。

### 契约更新依据与保留的断言

用户已明确质量模型评估在交付之后。当前主路径 `GovernedRecallEngine._select_post_fusion_items`
与 `LightweightAdmission.select` 使用本地证据准入；`enforce_selection_authority` 在选择前后重新读取真实权威记录。
`semantic_relevance_monitor` 的事后调度才负责模型评估。旧测试通过同步 verifier 回调驱动选择、拒绝、撤销，
实际回调不执行，因此它们无法测试现有业务契约；恢复同步 verifier 才会违反用户要求。

- 本地足量证据应交付，弱证据/缺请求属性应拒绝；模型成功、空选择、假 ID、假引用、超时均不得在同步路径救回或清空结果。
  更新测试对两种本地证据输入交叉运行这四种模型 payload，断言精确选择、状态、零调用和没有模型证明。
- `no_evidence` 表示本地没有选择，不是模型“答案不存在”的 verdict。无 verifier 不构成召回服务不可用；
  真实超时、权限拒绝、最后读取中撤销/篡改/缺失则继续 `unavailable`、`collection_complete=False`。
  二次读取测试有 valid 正例，且断言第一次读取 12 项、最终读取实际选中 1 项，不能靠选择为空蒙混通过。
- verifier 独立函数的契约仍单独测试：空选择→no_support，假 ID/假引用/超时→unavailable，
  引用必须属于选中候选且满足属性、配置缺失不可用、一次调用最多 8 候选。没有删除这类安全断言；
  它们不再错误地要求召回主路径调用该函数。所有 completion 为本地 mock。
- 现有真实 monitor 本地临时库测试保留：空交付→unknown、模型失败→unknown 且完整 decision 不变，
  query/render digest 不一致→unknown 且不调用模型，撤销/跨 scope/source/缺收据不可认证；
  relevant 正例与 off_topic 事后登记反例均覆盖。108 项组实际执行，不以“零同步调用”替代事后失败验证。

未改任何选择阈值，没有 skip/xfail 或删去失败文件。原八个 host measurement 的选择正反预期保持；
只把过时的一次同步调用/模型 outcome 断言替换为零调用、本地状态与无模型证明。

### 真实缺陷与 RED → GREEN

职责路由修补在本轮开始时已在未提交工作区，前文记录其先 RED 后最小公共修。
本轮再次以完整 HEAD 源码加新测试验证：三种职责问法 **3 failed，exit 1**，都错误返回 status；
原“项目预算与项目进度由谁管理？”在原 baseline 也失败。工作区沿用共享 task_recall_mode 修补，
没有扩大词表、算法、阈值或逐调用点加分支；原职责问题与职责说明断言保持原样并通过。

独立审查中“项目现在→任务现在”只能隔离标题测试，**不能关闭真实解析缺陷**。
本轮先保留原问法 `Alpha v1.14.29的项目现在进展如何？` 和不同名称/版本控制，
在本轮编辑解析器前真实 **2 failed，exit 1**，错误实体分别为“29的”“40的”。
根因是 `_PROJECT` 的后缀匹配从 `_VERSION` token 内部起步；公共 `explicit_project`
现在拒绝这种重叠匹配并使用已有 named-release fallback。没有增加项目名、版本值或整问句特判。
同组在修复后 **2 passed，exit 0**，同时断言项目 scope、状态路由、属性、合法标题支持、跨项目/跨版本拒绝。
标题矩阵保留任务问法并恢复项目原问法（额外 12 项），所有原 122 项继续保留。
此修复不宣称通用自然语言实体识别或真实当前状态已解决。

### 本轮测试口径（均为真实进程退出码）

|集合/源码|结果|说明|
|---|---|---|
|修改前工作区，原 generic_project_query_identity + evidence_routing|14 passed / 18 failed，exit 1|复现旧同步契约失败，职责修补已存在|
|完整 HEAD f3689657，16 个已有 focused 文件、原测试|342 passed / 19 failed，exit 1|git archive 完整树独立运行；新 common_boundaries 不在 HEAD，不能把它算入旧 baseline|
|HEAD 源码 + 本轮更新的两个契约/实体测试文件|43 passed / 3 failed，exit 1|不是原 baseline；三失败仅原职责路由与两个原项目版本问法，新的事后契约预期在旧主路径也成立|
|工作区 selected 三文件（common_boundaries + 两个契约/实体文件）|180 passed，exit 0|对应此前 136/18 的集合；新增正反覆盖后数量增加|
|工作区完整 17 文件 focused|509 passed，exit 0|包括原失败的全部文件，未排除、未跳过；不是全仓测试|
|事后质量安全组：semantic_relevance_monitor、recall_posthoc_quality、monitor_authorized_scopes|108 passed，exit 0|与 509 有重叠，不累加样本数；包含完整性 unknown 与失败不改交付|

完整 focused 从仓库根目录复跑：

```sh
env -u EIMEMORY_CAPTURE_QUERY_SCOPES .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_recall_common_boundaries.py tests/test_lightweight_evidence.py \
  tests/test_project_status_route.py tests/test_task_recall_repair.py \
  tests/test_memory_core_v1_repair.py tests/test_generic_project_query_identity.py \
  tests/test_recall_lexical.py tests/test_recall_fusion.py tests/test_recall_posthoc_quality.py \
  tests/test_monitor_authorized_scopes.py tests/test_release_scope_binding.py \
  tests/test_query_capture_scope.py tests/test_version.py \
  tests/test_dense_admission_boundary.py tests/test_evidence_routing.py \
  tests/test_source_partition.py tests/test_recall_local_work.py

env -u EIMEMORY_CAPTURE_QUERY_SCOPES .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_semantic_relevance_monitor.py tests/test_recall_posthoc_quality.py \
  tests/test_monitor_authorized_scopes.py
```

原 baseline 复跑方法：按前文 git archive 完整解包到新 scratch 目录，使用同一绝对路径解释器，
在解包目录运行完整命令但移除 HEAD 中不存在的 `test_recall_common_boundaries.py`。
测试覆盖对照则另建独立解包树，仅复制两个更新的测试文件后执行它们；不可称为“原测试 baseline”。
仅测试子进程移除 capture allowlist，不改生产设置。git diff --check 通过。

### 生产观察与未满足的最终门禁

父会话最新反馈：现网 RPC 问“发布验收有哪些要求”返回 **1 条正相关 query memory item**，
同时 **persona 仍有模型偏好**。这是父会话报告的当次观察，本轮未调用 RPC 独立复验。
它已否定把旧“query items 返回模型偏好”当作固定现状的写法；persona 属于另一个返回区域，
不能混入 query items 的相关性计数，也不能凭 persona 出现偏好就断言查询结果 off-topic。
反过来，一条正相关 item 也不证明要求齐全、宿主最终正确使用或当前项目状态可靠。
本轮补丁尚未部署，更不能把这次变化归功于本轮代码。

父会话在其已有授权下还需完成：同次 release/scope/source 身份与 provider→过滤→融合→最终 query items
的脱敏证据；单独检查 persona 内容与宿主最终投递；自然验收要求改写、当前进展、显式历史和虚构项目控制；
源事件时间可信性与最新证据是否在候选池中；事后模型结果、unknown 和失败时交付不变。
这里的本地 mock 正反测试不代替真实质量样本，历史时间排序不认证“现在”。
实际服务版本本轮未重新核验，旧 1.14.28 身份记录只是前次快照。
七处版本/发布材料完整保持 1.14.29；没有获得或执行直接提交权限。
