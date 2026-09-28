# 现场复核与采用顺序

不要为消除红色结果清空旧日志、补成功计数、扩大等待白名单或反复跑同一验收。
首先保留本次部署原始闭环 JSON、摘要 stdout、原生产者与摘要/记录器退出码、commit、receipt、session、scope、attempt。
同一个版本号不能证明这些输出属于同一次执行。

## 新部署留存

补丁安装器把证据写到 `$EIMEMORY_LOG_DIR/release-closure-captures/closure-*/`，位于 release 树外。
每次包含：source.bin（原始字节）、summary.json（共享判定）、capture.json（关联元数据）、result.json（登记结果）。
目录 POSIX 0700，文件使用现有 private atomic writer。原文可能有敏感字段，只供授权运维访问，不放到公开日志。
没有自动清理保留目录；运维需要制定保留期和容量监控。

若 source.bin 没有成功归档，安装器保留临时输入，并输出 `release_closure_input_preserved=...`。
若数据库写入失败，raw/summary/capture 仍保留，result 的 recording_ok=false、exit_code=2；
不要把 incident_record_id 为空理解成“无故障”。已有历史原报告未被自动补录。

## 关联与判断

对比 capture.source_sha256、report_digest、decision_digest、attempt_id、expected_commit 和 scope；
incident.content.detector_report 和 incident.content.capture 保存对应引用。
不同原文/不同 receipt/session/scope/attempt 不应混作一条证据。摘要哈希本身不认证来源。

- evidence_waiting：只发现允许的等待信号，没有独立硬错误；不认证闭环。
- diagnosis_required：聚合原因未解释清楚，incident 已登记；repair_eligible=false，先定位具体失败边界。
- failure_detected：存在明确错误或无效报告；incident 登记后由原受管修复入口处理。
- incident_recording_failed / runtime_close_failed：记录流程本身失败，保留证据和非零退出码。

失败信号标注报告控制面路径；这不是未经验证的宿主进程时间线，不能据此推断历史 PID 或唯一根因。

## 重放原报告（不写生产数据库）

在独立源码工作区，用当前 Python 环境、树外的新证据目录执行：

```bash
python -I -B deploy/record_release_closure_incident.py \
  --path /protected/evidence/closure-EXAMPLE/source.bin \
  --evidence-dir /protected/reinspection \
  --expected-commit ACTUAL_FULL_COMMIT \
  --attempt-id ACTUAL_ATTEMPT_ID --closure-exit-status ACTUAL_EXIT_STATUS \
  --scope-agent ACTUAL_AGENT --scope-workspace ACTUAL_WORKSPACE \
  --scope-user ACTUAL_USER --inspect-only
```

将全部 ACTUAL_* 替换为原执行记录；非默认 tenant 使用 --scope-tenant。
inspect-only 会新建诊断归档但不创建 Runtime、不登记数据库；对需要登记的状态返回 2 是预期。
不要对未核验来源的原文自动取消 inspect-only，更不能以哈希替代 trusted producer 身份。

## 推进与关单

新的报告类仅允许共享判定模块的窄修复。需要改采集器、部署器或其他敏感边界的根因，应由维护者
在正常源码评审路径处理，不通过加大自动授权范围或改 receipt builder 来绕过限制。
机器策略必须匹配新 incident_digest、基线、测试计划和 provider；补丁不签发、安装或扩大策略。

已提交修复尝试应有 incident_record_id、transaction_id、attempt_record_id；proposal_blocked/submission_failed
仍为失败。早期仓库身份或策略阻断返回明确 reason，不伪造 transaction。
只有实际修复、受保护测试和新部署/业务证据链重新验证后，才可按既有正式流程关闭故障。
本补丁始终不自行把这些记录设成 repair_complete=true，也不会撤销 active 规则、迁移旧键或删除历史导出。
