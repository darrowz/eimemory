# eimemory 闭环故障链修复包

固定基线：`3a87bf3b8d464e64fda5025fbf05b4df772d0b75`。日期：2026-09-28（Asia/Singapore）。
统一补丁修改 17 个仓库文件（11 个修改、6 个新增），增加 1493 行、删除 691 行。
这是基于最新已吸收修复的增量统一补丁，不叠加旧版本补丁，不修改生产环境。

## 应用与回退

将本包放到仓库外，使用完整、干净且保留 LF 原始字节的基线工作区：

```bash
python -B verify_and_apply.py --repo /path/to/eimemory
python -B verify_and_apply.py --repo /path/to/eimemory --apply
```

默认只检查。脚本检查 HEAD、完整原文件 Git blob、改后文件/片段及补丁 SHA-256，
检查未暂存/暂存/未跟踪冲突、符号链接和 reparse 路径，先 `git apply --check` 再显式应用，
最后比对预期字节。不会 checkout、reset、提交、推送、改系统服务或部署。

在相同基线、未提交且没有其他修改的工作区回退：

```bash
python -B verify_and_apply.py --repo /path/to/eimemory --reverse
python -B verify_and_apply.py --repo /path/to/eimemory --reverse --apply
```

已经提交后走正常审阅回退流程，不使用该未提交回退工具。SHA 校验不是数字签名。

## 在完整项目运行的聚焦测试

```bash
python -B -m pytest -q -p no:cacheprovider --strict-markers \
  tests/test_closure_pipeline_contract.py \
  tests/test_closure_capture_pipeline.py \
  tests/test_closure_repair_routing.py \
  tests/test_closure_pipeline_runtime.py \
  tests/test_release_closure_failure.py \
  tests/test_governance_env.py \
  tests/test_system_code_repair.py

git diff --check
```

还应运行既有 deployment 工具、code-automation-policy 和 semantic-validation 的相关用例。
本包没有恢复全仓 CI 矩阵。新增受保护修复计划维持既有严格验证要求，不借本次修复豁免安全验证。

## 判定字段

`report_contract_ok` 是报告结构/所声明成功合同的有效性；`admission_ok` 是是否允许进入成功或已验证等待状态。
既有 `contract_ok` 保留 admission 语义。有效但阻断的报告可以 `report_contract_ok=true`、
`admission_ok=false`、`contract_error=""`。`closure_certified` 仍仅表示报告通过相应结构检查，
不替代运行时独立证据与 receipt/lineage 验证。登记、提案提交均不能把 `repair_complete` 改成 true。

## 兼容性

新摘要版本 `release_closure_summary.v3` 增加原文关联及 disposition 字段。消费方应使用明确字段，
不能继续把 `contract_ok=false` 一概解释为“格式错误”，也不能把 CLI 0 一概解释为“业务闭环完成”。
新 incident 类 `release.closure_report_failure` 对应 `release.closure-report-repair.v1`；
只允许修改共享报告判定模块，没有新增 deploy、测试或授权文件的自动写权限。
已有机器策略不会自动被续期、改写、扩大授权或绕过一次性消耗限制。

详见 AUDIT.zh-CN.md、OPERATIONS.zh-CN.md 和 validation.json。
