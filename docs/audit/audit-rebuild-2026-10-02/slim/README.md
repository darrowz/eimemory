# 从零瘦身评估：基线与第一批候选

基线：eimemory v1.14.31，提交 `4763001d1c4f3f4af6e6dda17e008e1b4c9b5609`

## 结论

当前没有可以直接批准删除的代码。首批已读源码显示：三个兼容入口确实重复了相同调度逻辑，但抽取公共实现仅节省 878 字节，占包源码的 0.011%，并增加一个模块及间接调用。建议暂缓，而不是为少几行而扩大重构范围。

当前存储模块中，未发现运行时调用的 `readonly_recall` 仍提供有测试的公共接口和环境变量契约；不能据此删除，也不能用语义不同的 RuntimeStore 读连接池替代。实际已交付的源代码缩减为 **0 字节**，本报告没有修改基线或修复工作树。

## 可复现的大小基线

计数来自基线仓库 `git ls-files` 返回的全部路径及逐文件 `lstat().st_size`。这是逻辑文件字节数，不是磁盘分配空间、压缩归档大小、内存占用或构建后的 wheel 大小。所有 1,402 个跟踪项均为普通文件。

| 范围 | 文件数 | 字节 | Python 文件数 | Python 字节 |
| --- | ---: | ---: | ---: | ---: |
| 全部跟踪文件 | 1,402 | 18,617,695 | 1,116 | 14,108,652 |
| eimemory 包源码 | 555 | 8,006,766 | 551 | 7,986,909 |
| tests | 473 | 5,482,092 | 472 | 5,480,979 |
| docs | 217 | 4,036,592 | 22 | 93,519 |
| deploy | 106 | 563,798 | 54 | 373,070 |
| integrations | 18 | 151,338 | 4 | 14,254 |
| scripts | 10 | 104,287 | 10 | 104,287 |

其他顶层路径的完整数据见 [baseline-summary.json](baseline-summary.json)。[baseline-files.tsv](baseline-files.tsv) 保留逐文件分母。

`pyproject.toml` 声明 wheel 的 packages 为 `["eimemory"]`。因此 8,006,766 字节是可直接比较的包源码载荷基线；没有执行 Hatch 或实际构建，不能把它冒充实测 wheel 大小。删除测试或文档既不符合保留要求，也不能当作已验证的安装包缩减。运行依赖列表为空；PDF/PostgreSQL 是可选依赖，本轮没有安装或测量它们。

### 包内大小分布

| 包内分组 | 文件数 | 字节 |
| --- | ---: | ---: |
| governance | 247 | 2,816,786 |
| storage | 22 | 934,629 |
| evaluation | 42 | 688,867 |
| adapters | 30 | 622,048 |
| retrieval | 27 | 571,121 |
| capabilities | 15 | 288,905 |
| api | 3 | 283,618 |
| knowledge | 22 | 249,283 |
| intake | 21 | 246,579 |
| cli | 4 | 228,353 |
| ops | 9 | 208,794 |
| scheduler | 2 | 156,143 |
| contracts | 9 | 125,858 |
| recall | 8 | 71,578 |
| 包根文件 | 9 | 69,374 |
| raw | 6 | 61,250 |
| experience | 6 | 61,217 |
| llm | 9 | 56,512 |
| models | 11 | 49,314 |
| ei_bridge | 12 | 43,817 |
| persona | 12 | 43,674 |
| living | 4 | 42,410 |
| scoring | 7 | 32,292 |
| core | 11 | 25,234 |
| compatibility | 1 | 20,903 |
| config | 4 | 5,897 |
| embeddings | 2 | 2,310 |

大小仅用于安排后续阅读顺序，不证明重复、无用或可以删除。governance 占包源码 35.18%；storage 占 11.67%。未读函数没有被标成审计通过。

## 候选与保留决定

### SLIM-01：三个普通兼容入口共享调度实现，建议暂缓

已完整阅读并确认重复的文件：

- `eimemory/governance/learning_report.py`，623 字节、15 行
- `eimemory/governance/learning_dashboard.py`，635 字节、15 行
- `eimemory/governance/curiosity.py`，599 字节、15 行

当前每个文件均保留两种行为：普通导入时把旧模块名绑定到新模块对象；`python -m` 时通过 `runpy.run_module(..., run_name="__main__", alter_sys=True)` 执行目标，且不先导入目标。

候选把这两分支放入新的私有模块 `eimemory/governance/_compat.py`，保留三个旧入口及明确的目标路径。拟议文本及原文件 SHA-256 见 [SLIM-01-proposal.json](SLIM-01-proposal.json)。该 JSON 只是可评审方案，不是已经应用的补丁。

| 指标 | 当前三个入口 | 三个入口加共享模块 | 变化 |
| --- | ---: | ---: | ---: |
| 包源码字节 | 1,857 | 979 | -878 |
| 物理行 | 45 | 28 | -17 |
| AST 节点 | 111 | 88 | -23 |
| if 节点 | 3 | 1 | -2 |
| 函数定义 | 0 | 1 | +1 |
| 文件 | 3 | 4 | +1 |

这些复杂度指标只描述该候选的静态结构，不是运行性能或认知复杂度的实测证明。新测试、报告和其他交付文件不在上述源代码节省中；仓库整体未必会更小。

必须保留：旧导入路径、目标全部导出、`sys.modules` 对象身份、跨新旧路径 monkeypatch 可见性、lazy import 行为、`python -m` 的参数/退出码/输出、无 loader mismatch、无提前导入目标。

主要风险：`importlib.import_module` 增加新的间接导入路径；源码图中的原有显式 import 边会变成字符串目标，需同步保证可追踪性。当前只验证了三个包装器，不能外推所有小文件都是同类，也不能把安全、鉴权或证据相关入口顺带纳入。

验证门槛：独立源码评审后仍需要受控的模块身份、导入顺序、目标执行次数、`-m` 参数与退出行为验证。`tests/test_shim_python_m_entry.py` 记录了既有兼容性要求，但读取测试不等于测试通过；其中超出当前允许语义范围的入口不得为本候选而运行。本轮未执行任何项目测试或项目入口。

独立静态复核结论：同意暂缓。拟议文本保留了模块对象别名和未预导入目标的 `runpy __main__` 两个关键机制，但这不构成运行时等价证明；878 字节的收益暂不足以覆盖新增间接调用和兼容性验证成本。没有应用补丁，没有运行行为测试。

### SLIM-02：删除 readonly_recall，否决

`eimemory/storage/readonly_recall.py`：1,759 字节、55 行、3 个顶层函数。对包源码、测试、模块文档及打包元数据的精确字面引用检索，只发现其自身和 `tests/test_readonly_recall_flag.py`。

它仍提供 `readonly_recall_enabled`、`open_readonly_connection`、`readonly_status` 和 `EIMEMORY_SQLITE_READONLY_RECALL`。默认 OFF、显式 `mode=ro`、打开失败回退等行为不等同于 RuntimeStore 默认两连接读池。保留该公共兼容契约及测试；本轮可兑现的节省为 0 字节。

字面检索不能证明没有外部调用者或动态导入，因此本报告用“未定位到运行时调用”，不用“确定死代码”。

### SLIM-03：合并 store_access 与 RuntimeStore 的读接口，否决

`eimemory/storage/store_access.py`：3,245 字节。其 `locked_read` 在持有锁时完成 `fetchone/fetchall`，并支持 Runtime、RuntimeStore 及轻量测试替身。`RuntimeStore.execute_readonly` 返回的是退出 reader 借用上下文后的 cursor，生命周期不同。

直接把 facade 换成 pooled-reader 调用不能算等价去重，可能引入游标租约及锁所有权问题。保留 `_FetchedCursor`、公共 facades、测试替身回退和取数边界；本轮节省为 0 字节。

## 与源代码审计的衔接

第一批的实际审计范围及问题见 [存储所有权审计](../storage-ownership-batch-001.md)，函数分母及逐函数状态见 [coverage-manifest.json](../coverage-manifest.json)。这是引用本轮审计线的结果，不扩大瘦身检查自身的覆盖声明。

已确认的事务所有权和检索截止时间问题应先修复，不应为了减少重复代码而合并拥有不同事务/锁契约的函数。后续瘦身应随逐模块审计推进；每个新候选都先证明语义等价，再比较净包体积及复杂度。

## 已做与未做

- 已做：逐路径元数据统计；核对提交和干净基线；读取打包配置、模块职责文档、上述特定源代码和相关测试；候选文本的独立 AST 解析和字节计量
- 未做：导入项目、运行项目/测试/模型、安装依赖、构建 wheel、访问生产数据或凭证、修改基线、删除代码、源码 push/PR/merge（本报告本身由文档提交保存）
- 现有安全、鉴权、归档、证据领域限制保持；没有为瘦身重新开启这些行动范围
- 独立评审状态：完成静态复核，同意暂缓 SLIM-01，保留 readonly_recall 和 cursor facade；没有获得应用批准，也没有应用候选

## 复现

从本目录使用系统 Python 执行 `python baseline_metadata.py /path/to/clean-baseline-checkout /tmp/eimemory-slim-baseline`。该工具只读取 Git 跟踪路径和文件元数据，不导入或执行 eimemory。候选比较仅对明确列出的三个已读源文件和 JSON 内的拟议文本做 AST 解析。
