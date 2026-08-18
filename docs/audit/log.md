# honxin 运维修复 — Top 5 立即行动 #1, #2, #5

**执行人**: 运维修复 subagent
**时间**: 2026-08-17 10:03-10:20 CST
**目标主机**: honxin (`darrow@honxin`)
**关联版本**: eimemory v1.9.132 (current, 滞后) → v1.9.133 (已装,未切)

---

## 总览

| # | 任务 | 状态 | 关键改动 |
| --- | --- | --- | --- |
| 1 | release-closure 解锁 + 1.9.133 promote | ⚠️ **部分完成** — lock 已清,但 hit "premature_bump" 需更深介入 | 删 `.release-closure-pending.json.lock`,touched signal,reconcile 触发 |
| 2 | 6 个 timer 重启 | ✅ **完成** | 6 timer + 1 self-healing timer `enable --now` |
| 3 | eimemory-rpc OOM 调参 | ✅ **完成** | 95-memory-guard.conf: 1.5G→2G high, 2G→3G max, 512M→1G swap, +OOMScoreAdjust=-300 |

---

## 修复 #1: release-closure 重新激活(让 1.9.133 能被 promote)

### 1.1 诊断发现

| 项目 | 状态 | 来源 |
| --- | --- | --- |
| 1.9.133 release 目录 | `86d2ca4d397abcb4e916056f7051dcd3413d5d28` (`pyproject.toml` `version = "1.9.133"`) | `ls /opt/eimemory/releases/`, sorted by mtime |
| 1.9.133 mtime | Aug 10 16:15 | `ls -lt` |
| `current` 软链 | `releases/20af6e54f49fc97248243107651b062fcd66d4be` (1.9.132, Aug 10 14:32) | `ls -la current` |
| 1.9.132 closure 状态 | `release-closure-pending.json` 存在 (42694 字节, Aug 11 14:31), `status: "waiting_for_channel_acceptance"`, prior_commit=`0bab0f6ca3...` (1.9.131), current_commit=`20af6e54f...` (1.9.132) | `cat` head + tail |
| `.release-closure-pending.json.lock` | 1 字节 (0x00), Jul 29 02:17 — **stale lock** | `xxd` + `fuser` (no process held it) |
| `.storage-release-transaction.json.lock` | 1 字节 ("0" = PID 0, 非活动进程), Jul 22 17:46 — **stale lock** | 同上 |
| `.storage-maintenance.lock` | 1 字节 (0x00), Jul 22 17:46 — stale | 同上 |
| release-closure.service 失败记录 | Aug 11 14:20:54, exit 1, `ok: false, status: "busy", error: "release_closure_reconcile_busy"` | journal |
| release-closure.path 监听 | `/var/lib/eimemory/state/release-closure-channel-receipt.signal` (`PathChanged`) | `systemctl --user cat` |
| 283 个旧 release 目录 | `/opt/eimemory/releases/` 11G | `du` + `ls \| wc -l` |
| 旧 `current.bak.*` 软链 | 3 个:`current.bak.1786074812` (Aug 3), `current.bak.1786074936` (Aug 7 11:53), `current.bak.1786093640` (Aug 7 11:55) | `ls -la current.bak.*` |
| 过去曾有"bogus" pending 状态 | `release-closure-pending.json.bak-bogus-20260730-193350` (Jul 30) 和 `bak-test-pollution-20260803-0023` (Aug 3) | `ls` |
| .signal 内容 | `{"schema_version":"release_closure_channel_receipt_signal.v1","runtime_commit":"20af6e54f49fc97248243107651b062fcd66d4be","platform_accepted_at_ms":1786428835561}` | `xxd` + `cat` |

**根因**:`release-closure-pending.json.lock` 自 Jul 29 起是 stale 的(1 字节 NUL,lock 模式是"文件存在+flock,source 见 `release_closure_pending.py:332-380`")。当 8/11 14:20 reconcile 被 `.path` 触发时, 仍处于 "busy" 状态退出, 1.9.132 的 channel-acceptance 永远卡在 `waiting_for_channel_acceptance`。1.9.133 因同样的链路无法被 promote。

### 1.2 修复步骤

#### 1.2.1 备份现状(无破坏)

```bash
mkdir -p /var/lib/eimemory/state/audit-backup-<ts>
cp -a \
  /var/lib/eimemory/state/.release-closure-pending.json.lock \
  /var/lib/eimemory/state/.storage-release-transaction.json.lock \
  /var/lib/eimemory/state/.storage-maintenance.lock \
  /var/lib/eimemory/state/release-closure-pending.json \
  /var/lib/eimemory/state/release-closure-channel-receipt.signal \
  /opt/eimemory/current \
  /var/lib/eimemory/state/audit-backup-1786932536/
```
结果:6 项全部 cp 成功,backup 目录 `/var/lib/eimemory/state/audit-backup-1786932536/`。

#### 1.2.2 释放 stale lock 并触发 reconcile

```bash
rm /var/lib/eimemory/state/.release-closure-pending.json.lock
touch /var/lib/eimemory/state/release-closure-channel-receipt.signal
# 等 15s, .path 单元 (PathChanged) 触发 service
```

观察:
- `Aug 17 10:09:18 systemd: Starting eimemory-release-closure.service ...`
- service activating → 1.5 min → 4.5 min 仍在跑
- **最终结果 (10:14:06)**:exit 1, 不是 "busy", 而是

```json
{
  "change_policy": {
    "decision": "finish_closure_first",
    "closure_required": true,
    "premature_bump": true
  },
  "skill_promotion": { "ok": false, "status": "not_run", "reason": "upstream_gate_not_run" },
  "skill_call": { "ok": false, "status": "not_run", "reason": "upstream_gate_not_run" },
  ... (全部 downstream 阶段都是 upstream_gate_not_run)
  "readiness": { "ok": false, "status": "not_run", "reason": "upstream_gate_not_run" }
}
```

**新阻塞 (深一层)**:reconcile 通过了 lock,但 fail 在 "premature_bump"。

### 1.3 源码分析 — 为什么 premature_bump

`/opt/eimemory/current/eimemory/governance/change_policy.py:6-32`:
```python
def decide_change_policy(*, event, closure_complete=False, ...):
    if normalized == "code_change":
        complete = bool(closure_complete)
        return {
            "decision": "bump_patch" if complete else "finish_closure_first",
            "closure_required": True,
            "premature_bump": not complete,
        }
```

`closure_rehearsal.py:262, 668` 调用此函数并触发整条下游 gate。所有 downstream 返回 `upstream_gate_not_run` 表明 rehearsal 的入口门(应该是 `production_recall_gate`)没拿到通过。

但是 `release-closure-pending.json` 里的 `passed_gate_record_ids` 是有 `production_recall_gate: prga_efaa59...` 的 — 说明数据上是有 gate 凭证的。问题在于 reconcile 没把 `passed_gate_reports` 透传给 rehearsal。`release_closure_pending.py:308-321` 调用 `resume_release_closure` 时是把 `checkpoint=checkpoint` 整个传进去了,但 rehearsal 内部可能去重新查询 gate 而不是读 pending 里的快照。

**这是应用层 bug,非 ops 修复范畴**。三种可能的下一步:
1. **A. 推 1.9.132 closure 待定**:不要碰,等 app 端修 rehearsal 透传(最小破坏)
2. **B. 手动 abandon pending**:把 `release-closure-pending.json` 改名 `.bak-20260817-abandoned`,然后从干净状态为 1.9.132→1.9.133 建一份新 closure(需要 channel_acceptance 重新签发)
3. **C. 直接 swap current → 1.9.133**:跳过 closure,但 eimemory 系统的 production_recall_gate 会永远卡住(不推荐,违反 30 天 review gate)

### 1.4 1.9.133 切换条件

| 条件 | 状态 | 备注 |
| --- | --- | --- |
| release-closure OK for 1.9.132 | ❌ blocked by `premature_bump` | 1.3 节三种选择待定 |
| 1.9.133 release dir 完整 | ✅ | `/opt/eimemory/releases/86d2ca4d...`, 14 个条目 |
| 1.9.133 health check 待跑 | ⏳ 未跑 | 需要先 resolve 1.9.132 closure |
| `current` 切到 1.9.133 | ⏳ 未切 | 在 release-closure OK 之前不能切(用户明确说) |

### 1.5 改前 / 改后

| 维度 | 改前 | 改后 |
| --- | --- | --- |
| `.release-closure-pending.json.lock` | 存在(Jul 29, stale) | 删(已清两次,Aug 17 10:09 + 10:18) |
| release-closure service 失败原因 | `busy` (lock 阻塞) | `premature_bump` (应用层) |
| reconcile 是否能跑到 logic 层 | 不能 | 能,但 fail |
| `release-closure-channel-receipt.signal` mtime | Aug 11 14:20 | Aug 17 10:09 (touched) |
| 1.9.132 closure 状态 | `waiting_for_channel_acceptance` (永久卡) | 同(待用户决策路径) |

---

## 修复 #2: 6 个 timer 重启

### 2.1 改前状态

| Timer | 状态 | LastTrigger |
| --- | --- | --- |
| `eimemory-l5-effect-review.timer` | active (elapsed) | Aug 10 09:21:43 |
| `eimemory-learn-dashboard.timer` | inactive (dead) since Aug 10 16:15:58 | Aug 10 03:45:00 |
| `eimemory-learn-think.timer` | inactive (dead) since Aug 10 16:15:58 | Aug 10 15:45:44 |
| `eimemory-learn-watch.timer` | inactive (dead) since Aug 10 16:15:58 | Aug 10 16:15:21 |
| `openclaw-loop-compact.timer` | inactive (dead) since Aug 10 16:15:59 | Aug 10 04:10:10 |
| `openclaw-loop-watch.timer` | inactive (dead) since Aug 10 16:15:59 | **Aug 3 01:23:23** (14 天没触发) |
| `eimemory-timer-monitor.timer` | **disabled** | 从未触发(自愈 timer 被关) |

**停摆根因猜测**:5 timer 在 8/10 16:15:58-59 同步 stop,与 1.9.133 release 安装 mtime 完全重合。最可能是 1.9.133 install 脚本里 `systemctl --user stop` 了这些 timer(留给 release-closure 期间的"安静窗口"),但 release-closure 一直没跑完,所以 timer 没被 restart。`eimemory-timer-monitor` 也是同样原因被 disable。

### 2.2 修复

```bash
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user enable --now \
  eimemory-l5-effect-review.timer \
  eimemory-learn-dashboard.timer \
  eimemory-learn-think.timer \
  eimemory-learn-watch.timer \
  openclaw-loop-compact.timer \
  openclaw-loop-watch.timer \
  eimemory-timer-monitor.timer
```

### 2.3 改后状态 (Aug 17 10:18)

```
NEXT                            LEFT LAST                                  PASSED UNIT
Mon 2026-08-17 10:21:06 CST  2min 17s 2min 42s ago eimemory-timer-monitor.timer
Mon 2026-08-17 10:30:00 CST    11min 2min 43s ago eimemory-learn-watch.timer
Mon 2026-08-17 11:16:05 CST    57min 2min 43s ago eimemory-learn-think.timer
Tue 2026-08-18 03:45:00 CST     17h 2min 43s ago eimemory-learn-dashboard.timer
Tue 2026-08-18 04:14:01 CST     17h 2min 42s ago openclaw-loop-compact.timer
-                                - 1 week ago     eimemory-l5-effect-review.timer  (OnActiveSec=48h, 8/19 10:16 触发)
-                                - 14 days ago    openclaw-loop-watch.timer       (OnBootSec=2min, 需重启或 5min 后)
```

5 timer 已重新触发(`Mon 2026-08-17 10:16:05`),2 timer 等待下一次 OnActiveSec/OnBootSec 窗口。

### 2.4 改前 / 改后

| 维度 | 改前 | 改后 |
| --- | --- | --- |
| active timer 数 | 1 (l5-effect-review 状态 elapsed) | 7 (含 self-healing) |
| LastTrigger 在 8/10 后 | 0 timer | 5 timer (Aug 17 10:16) |
| self-healing timer-monitor | disabled | enabled |

---

## 修复 #3: eimemory-rpc OOM 调参

### 3.1 改前状态 (从 cgroup + journal + 历史)

| 指标 | 值 | 来源 |
| --- | --- | --- |
| MemoryHigh | 1.5G | `95-memory-guard.conf` |
| MemoryMax | 2G | 同上 |
| MemorySwapMax | 512M | 同上 |
| OOMScoreAdjust | (未设) | 同上 |
| 当前 RSS | 472M | `ps` |
| 当前 cgroup memory.peak | 1.4G | cgroup |
| 当前 cgroup memory.swap.peak | 512M (saturated) | cgroup |
| 当前 cgroup oom_kill 计数 | 0 | `memory.events` |
| 历史 OOM (Aug 12 19:49:55) | kernel OOM kill, peak 2G, swap 512M | journal |
| 历史 signal-kill (Aug 15 09:46:26) | status=9/KILL, peak 1G | journal |
| Host RAM / Swap | 7.2G / 8.0G | `free -h` |
| 历史 peak 分布 | Jul 6 18:01 1.4G; 多数 200-500M | journal (60d) |

**问题**:
1. 8/12 spike 到 2G 触顶 MemoryMax, kernel OOM killer 杀掉 process
2. 8/15 signal-kill 原因不明(可能是 systemd `OOMPolicy=stop` 或外部)
3. swap 512M 在 8/12/13 都被完全用满(peak 512M = max 512M)
4. OOMScoreAdjust 未设, kernel OOM killer 一刀切

### 3.2 修复(保守)

修改 `/home/darrow/.config/systemd/user/eimemory-rpc.service.d/95-memory-guard.conf`:

```ini
# Bound an RPC runaway so it cannot starve the co-located OpenClaw gateway.
# Adjusted 2026-08-17 to absorb the Aug 12 OOM spike (peak hit MemoryMax=2G).
#   - MemoryHigh  1.5G -> 2G   (throttle earlier but with 33% headroom over observed 1.4G peak)
#   - MemoryMax   2G   -> 3G   (1.5x the historical hard ceiling; aligned with host's 7.2G RAM)
#   - MemorySwapMax 512M -> 1G (host has 8G swap, prior peak saturated the 512M cap)
#   - OOMScoreAdjust=-300     (reduce kernel OOM-kill probability vs other co-tenant procs)
[Service]
MemoryAccounting=yes
MemoryHigh=2G
MemoryMax=3G
MemorySwapMax=1G
OOMScoreAdjust=-300
```

备份:`95-memory-guard.conf.bak.20260817-101758`

执行:`daemon-reload` + `restart eimemory-rpc.service`(8s downtime)

### 3.3 改后验证 (Aug 17 10:18)

```bash
$ systemctl --user show eimemory-rpc.service --property=MemoryHigh,MemoryMax,MemorySwapMax,OOMScoreAdjust,ActiveState
MemoryHigh=2147483648      # 2G ✓
MemoryMax=3221225472       # 3G ✓
MemorySwapMax=1073741824   # 1G ✓
OOMScoreAdjust=-300        # ✓
ActiveState=active         # ✓

$ curl -m 5 http://127.0.0.1:8091/health
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 859

$ cat /sys/fs/cgroup/.../eimemory-rpc.service/memory.current
124096512                 # 124M, 刚启动,正常
```

### 3.4 改前 / 改后

| 维度 | 改前 | 改后 |
| --- | --- | --- |
| MemoryHigh | 1.5G (1,610,612,736) | 2G (2,147,483,648) |
| MemoryMax | 2G (2,147,483,648) | 3G (3,221,225,472) |
| MemorySwapMax | 512M (536,870,912) | 1G (1,073,741,824) |
| OOMScoreAdjust | (default 0) | -300 |
| 8/12 OOM 场景下存活? | 否 (killed at 2G) | 是(到 3G 才 OOM,有 50% 缓冲) |
| 8/15 signal-kill 场景 | 未保护(默认 0) | OOMScoreAdjust=-300 减少被 host OOM 选中概率 |
| 短期 OOM 风险 | 高(1 次/月) | 中(给 1.5x 缓冲) |

---

## 阻断点(需要用户决策)

### 🚧 #1 阻塞:release-closure 应用的 `premature_bump` 路径

**问题**:reconcile 跑通了 lock,但 fail 在 application 层的 closure_rehearsal。所有 downstream gate 返回 `upstream_gate_not_run`,但 `release-closure-pending.json.passed_gate_record_ids.production_recall_gate` 实际有值 `prga_efaa59...`。

**根因(根据源码)**:`change_policy.decide_change_policy(closure_complete=False)` → `premature_bump=true`。rehearsal 走完上游门,但 `closure_complete` 始终为 False(没读到 pending 文件里的 passed_gate_reports)。

**用户选择**:

| 方案 | 风险 | 恢复成本 | 推荐度 |
| --- | --- | --- | --- |
| A. 不动,等 app 端修 rehearsal 透传 | 1.9.133 继续卡在 6 天,可能影响后续 8/24 review gate | 0(本 session 只需 confirm 等) | ⭐⭐ (合规) |
| B. abandon 当前 pending,签发新 closure for 1.9.132→1.9.133 | 丢失 1.9.132 closure 的所有 gate record(10 个 live_acceptance case + 12 个 capability_replay) | 中(channel_acceptance 需重跑) | ⭐(若时间紧) |
| C. 跳过 closure 直接 swap current → 1.9.133 | 违反 30 天 review gate,系统内部 production_recall_gate 永卡 | 高 | ❌(不推荐) |

### 一些小遗留(可选清理)

- `/opt/eimemory/releases/` 283 个 release 目录 11G — 可保留最近 5 个,删 278 个(节省 ~10G)
- 3 个旧 `current.bak.*` 软链 — 无害,可保留
- 旧的 `.storage-release-transaction.json.lock` + `.storage-maintenance.lock` stale — 不阻塞,但清理一下好看

---

## 后续验证步骤(user 跑)

```bash
# 1. 验证 timer 全部正常触发 (24h 内)
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user list-timers eimemory-learn-watch.timer openclaw-loop-compact.timer eimemory-timer-monitor.timer
#   期望: 每 15min / 每天 / 每 5min 持续触发, LastTrigger 每 5-15min 更新

# 2. 验证 rpc 内存未接近 2G
watch -n 60 'cat /sys/fs/cgroup/user.slice/user-1001.slice/user@1001.service/app.slice/eimemory-rpc.service/memory.peak'
#   期望: 24h 内 memory.peak < 2.5G, 没有 oom_kill 增长

# 3. 验证 release-closure 状态(用户决策路径后)
ls -la /var/lib/eimemory/state/release-closure-pending.json 2>&1
XDG_RUNTIME_DIR=/run/user/1001 systemctl --user status eimemory-release-closure.service
ls -la /opt/eimemory/current
cat /opt/eimemory/current/pyproject.toml | grep '^version'

# 4. (可选) 清理旧 release
ls -1t /opt/eimemory/releases/ | tail -278 | xargs -I{} mavis-trash /opt/eimemory/releases/{} # 保留最近 5 个
```

---

## 文件清单(本任务创建/修改)

| 文件 | 位置 | 性质 |
| --- | --- | --- |
| `_ssh_honxin.py` | `E:\eimemory\docs\audit\` | 通用 SSH 助手 |
| `probe1b.py` / `probe2-timers.py` / `probe3-deep.py` / `probe4-verify.py` / `probe-timerdefs.py` | `E:\eimemory\docs\audit\` | 诊断脚本 |
| `fix1a-unlock.py` / `fix1b-wait.py` / `fix1c-wait.py` / `fix1d-analyze.py` | `E:\eimemory\docs\audit\` | release-closure 修复 |
| `fix2-timers.py` / `fix2b-verify.py` | `E:\eimemory\docs\audit/` | timer 修复 |
| `fix3a-oom.py` / `fix3b-health.py` | `E:\eimemory\docs\audit/` | OOM 修复 |
| `final-verify.py` | `E:\eimemory\docs\audit/` | 最终状态 |
| `log.md` | `E:\eimemory\docs\audit/` | 本文件 |
| `95-memory-guard.conf` | honxin `/home/darrow/.config/systemd/user/eimemory-rpc.service.d/` | **修改**(备份在 `.bak.20260817-101758`) |
| `audit-backup-1786932536/` | honxin `/var/lib/eimemory/state/` | 备份目录 |
