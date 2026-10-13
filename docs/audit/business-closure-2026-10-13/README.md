# 2026-10-13 全业务闭环审计：复现附件

本目录保留对 **1.14.73 / `e2bbc60e0a893d72d37de55e3b05bf5d910f058f`** 审计时使用的 17 个本地探针，供主审计报告中的 16 项发现及既有失败分类追溯。脚本是审计现场文件的原样副本；`manifest.json` 记录每个脚本的字节数、SHA-256 和原始临时路径。这里没有数据库快照、完整测试日志或生产收据正文。

`evidence-summary.json` 是主审计员根据实际执行结果整理的机器可读摘要，包含逐项观察、两批主回归的 XML 计数及哈希、失败测试名称与分类、文件清单统计，以及单独标记的用户提供生产证据。它不是自动生成的生产收据。`manifest.json` 仅对原样探针计算哈希，不包含此说明、摘要或主报告。

这些探针使用合成会话、合成记录和临时 Runtime。审计运行没有访问生产数据库、调用外部模型、部署、重启服务或推送代码。脚本中的 `audit-local-only-…` HMAC 字符串是本地测试签名材料，合成 release ID、用户和来源也不具备生产权威。相关模型、适配器故障或最终执行器按各探针目标由本地函数或测试夹具模拟。

**脚本退出 0 只说明探针执行完成，甚至可能说明已成功复现缺陷，不能当成产品验收通过。** 一些断言专门固定审计时的错误行为，修复后应当失败或产生不同诊断；不要把它们直接注册成“必须保持通过”的产品回归测试。应根据修复后的契约另写预期行为测试。

## 路径、依赖与执行边界

为保留原始字节和可核对的哈希，脚本仍使用 `/workspace/eimemory`。这是审计源码工作区，不是生产安装目录。Python 审计环境为 `/workspace/.eimemory-cloud/venv/bin/python`；需要该提交的完整源码、`tests/`、项目依赖、pytest，以及演进夹具使用的 pypdf。部分探针直接导入测试夹具或内部接口，不能仅凭已安装的 wheel 重放。

重放应在无生产凭据、无生产目录挂载、禁用外网的独立环境中进行，保持上述路径，或另行复制后仅调整路径并重新记录哈希。不要在已配置真实 Hermes/模型提供商的生产宿主批量运行；探针保留原始审计环境假设，并非通用沙箱启动器。各 Python 脚本可单独用 `python -I -B <脚本>` 运行；不要执行“遍历目录运行全部文件”的命令。

临时 Runtime 使用 `tempfile` 创建。有的脚本通过 `TemporaryDirectory` 自动清理，有的保留并打印临时目录，便于后续读回。`eimemory-audit-intake-probe.py` 还会写入 `/tmp/eimemory-audit-intake-results.json`。这些固定临时文件均仅存放合成探针结果，重放前应确认其归属。

## OpenClaw 两步探针

先运行 `probes/eimemory-audit-openclaw-identity.js`，再运行 `probes/eimemory-audit-openclaw-read.py`。第一步用 Node.js 在内存中暴露原模块的两个函数，比较合成宿主事件与合成“untrusted metadata”文本的规范化结果，并写入 `/tmp/eimemory-audit-openclaw-normalized.json`。第二步读取该文件，在全新临时 Runtime 中检验返回范围。

第二步不自建输入，缺少第一步输出就不能复现；也不能使用来源不明的同名 JSON。它们不修改 OpenClaw 桥接源码，不联系真实宿主。此两步一起构成一项边界探针，而不是两个独立的产品验收案例。

## 探针目录

| 文件 | 验证目标与主要边界 |
| --- | --- |
| `eimemory-audit-openclaw-identity.js`、`eimemory-audit-openclaw-read.py` | 不可信正文能否影响规范化身份及后续读取范围；按上述顺序执行。 |
| `eimemory-audit-intake-probe.py` | 不同真实宿主行 ID 对应相同文本时的去重；一次采集传输失败后的持久化与丢写状态。L1 提取模型为合成返回函数。 |
| `eimemory-audit-hermes-ack.py` | 交付 ACK 第一次超时后，真实临时决策的终结状态和重试字段。适配器请求走进程内桥接。 |
| `eimemory-audit-nightly-gate-route.py` | 通过实际 nightly 评测入口观察未声明契约的数据集、质量门及正式认可状态。 |
| `eimemory-audit-evaluation-probes.py` | 契约认可边界、中文自然问法的固定审评信号、评审窗口，以及原生 legacy 评测入口。 |
| `audit_capability_negative_evidence.py` | 同一能力先通过后失败时，失败证据如何进入能力状态投影。执行器仅返回合成字典。 |
| `eimemory_audit_backup_segments_interleaved.py` | SQLite 备份完成与逻辑导出之间安排一次真实临时库更新，检查归档 segment 是否随备份恢复。脚本自包含，无需另一个基础脚本。 |
| `eimemory-audit-review-default-window.py` | 超过默认窗口的 501 条合成待评审记录，重复运行是否推进及开放计数是否覆盖全集。 |
| `eimemory-audit-memory-eval-route.py` | 配置了可观测 SQLite 来源的 Runtime，直接召回与记忆评测是否使用同一路由。 |
| `eimemory-audit-identity-window-full.py` | 相同身份条目的有界扫描遇到过期记录后，是否保持完整的歧义判断和创建边界。 |
| `eimemory-audit-query-tail.py` | 自然问法前缀较长时，尾部唯一词是否进入 SQLite 搜索及来源候选。 |
| `audit_evolution_native_probe.py` | 原生提案→实验→候选→晋升返回形状与动态反馈关联。最终执行器被替换为无副作用 stub，详见下文。 |
| `eimemory_audit_fused_timer.py` | 闭环参数校验失败时，EXIT trap 是否意外启动原先未由本轮暂停的 L1 timer。 |
| `eimemory_audit_fused_stop.py` | 停止 L1 失败时，是否仍进入评测。systemctl 和评测解释器均是临时日志 stub。 |
| `eimemory-audit-knowledge-persona-closure.py` | 只读知识预览是否推进采集游标，以及同一纠正是否被后续 persona evolve 重复消费。 |
| `eimemory-audit-baseline-classify.py` | 对既有失败进行更窄的原生撤权重验证，并绑定时钟重跑特定旧预算断言；不是把整个基线重写为通过。 |

## 外部副作用被截断的位置

`audit_evolution_native_probe.py` 使用该提交的 `test_dynamic_code_patch_context.native_gap` 夹具，在临时目录创建本地 Git 仓库、合成策略、论文及能力证据。测试 remote 指向不可用的 `example.invalid` 地址；适用性模型和实现提供商由夹具替换。脚本在调用应用路径前，将 `execute_code_evolution_effects` 替换为只返回字典的 lambda，输出也明确记录 `actual_external_effects=false`。因此它验证返回值和反馈关联，**不证明真实补丁应用、push、部署或观察期验收成功**。

两个 fused 运维探针在调用真实 shell 脚本前，创建临时 `systemctl` 可执行桩，并将该目录放在子进程 `PATH` 首位。被调用脚本使用裸 `systemctl`，因此全部服务操作都落到日志桩。timer 探针在缺少 repository 配置时提前退出；stop 探针还把当前 release 的 Python 路径指向临时可执行桩。它们没有调用真实 systemd 或 L1 worker。真实 shell 本身保留生产默认路径，**不得绕开探针外壳直接执行它作为复现**。

备份探针的 `eimemory-fixture` 是临时 CLI 外壳，只调用源码中的 `backup_create` / `backup_verify`，根目录固定为临时 Runtime。故障调度包装器只在真实 SQLite online backup 结束后加入一次本地写入，不伪造备份返回值。恢复也写入新的临时 Runtime。

## 打包校验

本次打包仅解析了 16 个 Python 文件的 AST，并对 JavaScript 执行 `node --check`；未重新批量执行探针。清单的 SHA-256 对应附件本身，且与所列原始临时脚本一致。它们不是生产验收收据，也不能取代主报告列出的阶段证据、影响范围和修复验证。
