# v1.4.0t32 Provider 人工切换、可靠恢复与 Worker 可观测性计划

更新时间：2026-09-12（Asia/Shanghai）  
基线：`v1.4.0t31`，生产应用提交 `d68c6a6e155f513c99ecc34281ec1520c1b1fdf8`。  
目标版本：`v1.4.0t32`。

## 1. 版本定位

`t31` 已解决自动化恢复链的第一层问题：当存在 `pending`、`retry_wait` 或 provider probe 冷却时，worker 不应提前退出；`resume` 可以遍历多个未完成刊期；历史 `start-limit-hit` 已解除。

`t32` 不再扩展“自动切换供应商”。生产运营采用**人工选择 Provider、系统可靠执行**的模式：Provider 长时间 502/503/timeout 时由熔断器停止普通请求；管理员人工测试其他 Provider；只有测试成功的 Provider 才允许被设为默认；默认 Provider 切换后，管理员可以对失败文章点击“立即探测”“重试”或“恢复本期”，系统必须可靠地使用当前默认 Provider 执行，并保留旧 Provider 的完整历史。

`t32` 同时把后台自动化从“黑盒”升级为可观察任务控制台：每个 Worker、每个 Task、每个 Admin Action 都必须有明确状态、进度、心跳和最终结果。任何按钮操作不得永久停留在 `requested`，不得出现“点击后无反应、后台实际未启动、页面一直卡死”的状态。

## 2. 生产事实与本版驱动问题

本版由以下真实生产问题驱动：

1. 旧 Provider 连续 502/503/timeout 后进入 circuit open，历史任务仍绑定旧 Provider；即使管理员已经验证并切换了新的默认 Provider，旧任务仍不能自然使用新 Provider。
2. 2026-09-08 曾出现 Admin `probe` 已成功写入数据库，但 `started_at` 一直为空，最终只能被维护逻辑标记为 `ACTION_TIMEOUT`。这说明“API 返回 202”不等于 Worker 真正执行。
3. 2026-09-11 的历史恢复表明：当旧 Provider 长期开路时，人工选择健康 Provider + 受控重绑定 + 重试可以恢复刊期；这套过程需要正式产品化。
4. 当前 Admin 对 `probe`、`retry`、`rebind` 的语义不够直观，无法一眼判断当前动作究竟使用哪个 Provider。
5. 当前后台没有 Worker 可视化。Worker 死亡、唤醒链断裂、等待下一次 retry、Provider 熔断等状态，需要 SSH/数据库排查才能确认。
6. 持久 Worker 可能发生“容器仍 healthy，但某个线程已经死亡”的退化状态。`account-mail` Worker 已有过因瞬时 SQLite `OperationalError` 退出的生产事实，现有健康检查不会发现。

## 3. T32 的 P0 目标

### P0.1 人工 Provider 切换是唯一切换入口

系统不得自动从 Provider A 切换到 B/C。管理员负责判断供应商是否可重新投入生产。

标准流程：

```text
Provider A 长时间失败
        ↓
Circuit Open
        ↓
人工测试 Provider B
        ↓
测试 SUCCESS
        ↓
人工“设为默认”
        ↓
失败任务点击“立即探测 / 重试 / 恢复本期”
        ↓
系统使用 Provider B
```

### P0.2 熔断后人工按钮必须可靠执行

当任务处于 `failed`、`retry_wait`、`configuration_blocked`、`cancelled` 或 Provider circuit open 时，管理员必须仍然能够执行适用的人工恢复动作。

任何按钮操作都必须满足：

```text
点击
→ 持久化 Action
→ 返回 action_id
→ 发出即时唤醒
→ Worker 领取
→ 执行
→ completed / failed / timed_out / recovered
```

禁止永久 `requested`。

### P0.3 Worker 和 Task 必须图形化可见

Admin 必须显示：

- 每个逻辑 Worker 的状态、PID/线程、心跳、当前 Task、Provider、下一次唤醒和最后错误；
- 每个刊期的翻译进度和发布进度；
- 每篇文章对应的 active Task；
- 每个 Task 的真实阶段进度条；
- 每个 Admin Action 的执行步骤和最终结果；
- Provider circuit 状态及当前默认 Provider。

### P0.4 Provider/Task 历史必须可审计

Provider 切换不得修改旧 Task 的 `provider_id`。切换 Provider 必须创建新 Task，并保留旧 Task/Attempt。

## 4. 明确不做

`t32` 不做以下事情：

- 不做自动 failover；
- 不根据一次 502 自动切换 Provider；
- 不删除或覆盖旧 Task/Attempt；
- 不引入 Redis、RabbitMQ、Kafka 等消息队列；
- 不引入微服务；
- 不改支付、权益、SMTP 业务规则；
- 不自动补发历史邮件；
- 不做 T33 的发布/manifest 全面重构；
- 不做 T34 的句子级 repair；
- 不改变当前正式备份策略。

## 5. 核心不变量

### 5.1 Task 的 Provider 不可变

`translation_tasks.provider_id` 在 Task 创建后永远不可修改。

错误做法：

```text
Task-A(provider=A)
默认 Provider 改为 B
→ 把 Task-A.provider_id 改成 B
```

正确做法：

```text
Task-A(provider=A, failed)
        ↓ rebind
Task-B(provider=B, pending)
        ↓
edition_items.active_task_id = Task-B
```

Task-A 永久保留。

### 5.2 默认 Provider 是人工授权事实

只有当前配置 fingerprint 对应的最近一次 Provider 测试为 `SUCCESS` 时，才允许设为默认。

Provider 配置发生任何会改变 `configuration_fingerprint` 的修改后，旧 SUCCESS 自动失效；必须重新测试。

### 5.3 默认 Provider 切换不自动改历史任务

“设为默认”只改变后续人工恢复/新任务的目标 Provider，不批量重写历史 Task。

### 5.4 DB Action 是事实源，wake 文件只是加速器

是否存在待执行动作，以数据库 `translation_admin_actions` 为准。

`automation.wake` / systemd path 只负责立即唤醒，不能成为唯一可靠触发条件。

### 5.5 每个 Action 必须终态化

每个管理动作最终只能进入：

- `completed`
- `failed`
- `timed_out`
- `recovered`

不允许无限期 `requested` 或 `running`。

### 5.6 UI 进度来自真实状态，不根据耗时猜百分比

进度条表示生命周期阶段，不表示“模型还剩多少秒”。

## 6. Provider 管理行为

### 6.1 Provider 测试

Provider 管理页保留独立操作：

```text
[测试接口]
```

只验证当前 Provider 配置本身：endpoint、认证、模型和基本响应能力。

测试结果至少保存：

- provider profile；
- configuration fingerprint；
- requested_at；
- finished_at；
- result：SUCCESS / FAILED；
- sanitized error category；
- latency；
- HTTP status（如适用）。

不得保存 API key 或 secret。

### 6.2 设为默认

仅在以下条件同时满足时允许：

```text
latest_test.result == SUCCESS
AND latest_test.configuration_fingerprint == current_configuration_fingerprint
```

设置默认后：

- 写入配置；
- 记录 Admin 审计事件；
- 不修改已有 Task；
- UI 显示“当前默认 Provider”；
- 后续人工 `probe/retry/recover` 在请求时解析这个默认 Provider。

### 6.3 不自动切换 Provider

即使 Provider A 连续失败并长期 open，系统也只负责：

- 熔断；
- 显示错误；
- 停止普通请求；
- 允许人工探测；
- 等待管理员选择并验证其他 Provider。

系统不得自行把 B/C 设为默认。

## 7. “立即探测”“重试”“恢复”的正式语义

### 7.1 Provider 页：测试接口

`测试接口` 只验证 Provider profile，不属于文章 Task，不改变刊期结果。

### 7.2 Task 页：立即探测

`立即探测` 的目标是**当前默认 Provider**，不是旧 Task 历史绑定的 Provider。

执行时：

1. 读取当前默认 Provider；
2. 验证其最新 SUCCESS 与当前 fingerprint 匹配；
3. 若 active Task 已绑定当前默认 Provider，则复用该 Provider 语义；
4. 若 active Task 绑定旧 Provider，则原子创建新 Task，并 `rebind_from_task_id=old_task_id`；
5. 将新 Task 设为 `active_task_id`；
6. 创建 `probe` Admin Action；
7. 立即唤醒 Worker；
8. 即使当前默认 Provider circuit 为 open，也允许占用唯一 half-open probe lease 发起一次受控请求；
9. 成功则关闭该 Provider circuit，且如果得到合法翻译结果可直接完成该 Task；
10. 失败则记录真实失败并恢复到 open/failed/retry_wait，不无限探测。

“立即探测”只保证一次受控人工尝试，不自动进行无限后续重试。

### 7.3 Task 页：重试

`重试` 同样以**当前默认 Provider**为目标。

- 当前 active Task Provider 与默认 Provider 相同：进入正常有界重试流程；
- 不同：先原子 rebind，再进入正常有界重试；
- 当前默认 Provider 为 open：本次人工重试的第一步允许作为受控 probe；probe 成功后继续正常预算，probe 失败则动作终止并明确显示失败原因。

因此熔断后管理员既可以点“立即探测”，也可以点“重试”，两者都必须产生实际可观察动作，而不是无响应。

### 7.4 使用当前默认接口恢复文章

显式按钮：

```text
[使用当前默认接口恢复]
```

用于管理员明确知道当前 Task 属于旧 Provider 的场景。

UI 必须先显示：

```text
旧 Provider：A
当前默认 Provider：B
将创建新 Task，不修改历史 Task
```

确认后执行原子 rebind + retry。

### 7.5 恢复本期

刊期级按钮：

```text
[使用当前默认接口恢复本期]
```

预览至少显示：

- 目标文章数；
- 已成功、不会重复处理的文章数；
- 需要 retry 的文章数；
- 需要 rebind 的文章数；
- 旧 Provider 分布；
- 新默认 Provider；
- 是否会发送邮件：必须明确为否，除非另有显式投递动作。

执行后逐篇原子处理，重复点击必须幂等。

## 8. 受控 Rebind 数据模型

建议 schema 升级到 v13。

### 8.1 `translation_tasks` 新增

```text
rebind_from_task_id TEXT NULL
rebind_reason TEXT NULL
```

`rebind_reason` 建议枚举：

```text
PROVIDER_REPLACED
PROVIDER_DISABLED
PROVIDER_CONFIGURATION_CHANGED
PROVIDER_LONG_TERM_OPEN
MANUAL_REBIND
EDITION_RECOVERY
```

必要索引：

```text
INDEX(rebind_from_task_id)
INDEX(edition_date, provider_id, status)
```

### 8.2 原子 Rebind

以下步骤必须在同一个 `BEGIN IMMEDIATE` 中完成：

1. 读取并锁定 `edition_items.active_task_id`；
2. 校验 expected old task；
3. 校验 target Provider；
4. 创建新 Task；
5. 写入 lineage；
6. 更新 `edition_items.active_task_id`；
7. 写入 Admin Action；
8. commit。

更新 active task 必须使用 compare-and-swap 条件：

```sql
UPDATE edition_items
SET active_task_id = :new_task
WHERE active_task_id = :expected_old_task
```

`rowcount == 0` 时视为并发操作已经抢先完成，返回现有事实，不再创建第二条有效链。

## 9. Admin Action 状态机

建议增强 `translation_admin_actions`：

```text
target_provider_id
wake_sent_at
claimed_at
started_at
finished_at
result_code
```

动作状态：

```text
requested
   ↓
claimed
   ↓
running
   ↓
completed / failed
```

异常恢复：

```text
requested → timed_out
running   → recovered / failed
```

UI 可以通过时间字段展示更细步骤，但数据库仍保持有限、稳定的状态枚举。

### 9.1 Action SLA

建议：

- `requested` 后 15 秒仍未 claim：UI 黄色提示“Worker 尚未领取”；
- 30 秒内由 fallback wake 至少再次尝试一次；
- 90 秒仍未 claim：Action 终态化为 `ACTION_WAKE_TIMEOUT`，释放手动标记；
- running 超过 Task lease/hard timeout：现有 lease recovery 接管并终态化 Action。

这些阈值应集中为常量，避免散落。

## 10. 双通道 Worker 唤醒

### 10.1 即时通道

保留：

```text
Admin action
→ 写 automation.wake
→ news-digest-wakeup.path
→ news-digest-resume.service
```

### 10.2 Fallback 通道

新增 systemd timer，例如：

```text
news-digest-wakeup.timer
```

建议每 30 秒触发同一 `news-digest-resume.service` 或一个轻量 `resume-if-needed` 入口。

目标：即使 `PathChanged` 丢事件、服务临时未启动或一次 wake 文件写入没有被消费，数据库中的 requested Action 仍能在有限时间内重新被发现。

必须保证：

- 与 path 触发幂等；
- 同一 service 不并发执行；
- 无待办时快速退出；
- exit 10 等“无工作”状态被 systemd 视为正常；
- 不触发 start-limit 风暴。

## 11. Worker Registry 与心跳

### 11.1 新增 `worker_runtime_status`

建议字段：

```text
worker_key TEXT PRIMARY KEY
instance_id TEXT
worker_type TEXT
execution_kind TEXT      -- process / thread
pid INTEGER
thread_name TEXT NULL
state TEXT
edition_date TEXT NULL
task_id TEXT NULL
action_id TEXT NULL
provider_id TEXT NULL
started_at TEXT
heartbeat_at TEXT
next_wakeup_at TEXT NULL
last_error_code TEXT NULL
revision TEXT NULL
```

### 11.2 Worker 类型

至少覆盖：

```text
automation-daily
automation-resume
translation-runner
account-mail-1
account-mail-2
payment-reconcile
```

如果 build/delivery 仍在 translation process 内执行，则作为 Task stage 展示，不伪装成独立 OS 进程。

### 11.3 Process 与 Thread 必须区分

例如：

- `automation-resume`：独立 systemd/Python process；
- `account-mail-1`：site Python process 内线程；
- `payment-reconcile`：site Python process 内线程。

UI 显示真实 `pid` 和 `thread_name`，不得把每篇文章 Task 假装成一个独立 Linux process。

### 11.4 心跳策略

建议：

- 正在执行：每 5 秒 heartbeat；
- 空闲持久 Worker：每 15 秒；
- 状态改变：立即写一次；
- 30 秒无心跳：`stale`；
- 60 秒无心跳且该 Worker 应常驻：`offline`。

对短生命周期的 `automation-resume`：

- 没有待办且 service 未运行：显示 `Idle / Not scheduled`，不是红色错误；
- 有 requested Action 但没有对应 Worker 心跳超过 SLA：显示红色 `Wake failure`。

## 12. 持久 Worker 自愈

Worker 可视化不能只负责“显示死了”，还要避免已知的静默死亡路径。

### 12.1 Account Mail

`account-mail` 线程对以下 SQLite 操作必须捕获瞬时 `sqlite3.OperationalError`：

- claim；
- release；
- complete。

处理原则：

```text
OperationalError
→ rollback/close
→ sanitized log
→ bounded short backoff
→ reconnect
→ continue
```

单次瞬时 SQLite 错误不得永久杀死线程。

### 12.2 线程 Supervisor

site 主服务应周期检查持久 Worker：

- `account-mail-1/2`；
- `payment-reconcile`。

如果线程异常退出：

- 写 worker state=`error`；
- 记录 sanitized error；
- 在安全范围内重新创建线程；
- 若重复崩溃超过阈值，停止自动重启并在 Admin 显示红色告警。

这属于 Worker 可靠性，不将其升级为 Docker liveness failure，避免整个 site 因单个后台线程产生重启风暴。

## 13. Admin 图形化任务控制台

### 13.1 首页总览

建议布局：

```text
┌─────────────────────────────────────────┐
│ News Digest Automation                  │
│ System ● Normal / Degraded              │
└─────────────────────────────────────────┘

┌──────── Worker 状态 ────────────────────┐
│ 🟢 translation-runner   Running          │
│ 🟡 automation-resume    Waiting          │
│ 🟢 account-mail-1       Idle             │
│ 🟢 account-mail-2       Idle             │
│ 🟢 payment-reconcile    Idle             │
└─────────────────────────────────────────┘

┌──────── 2026-09-12 ─────────────────────┐
│ 翻译 4/6 █████████████░░░ 67%           │
│ 上线 4/6 █████████████░░░ 67%           │
│                                         │
│ Task 1 ███████████████████ 100% ✅       │
│ Task 2 ███████████░░░░░░░  60% 🟢       │
│ Task 3 ████░░░░░░░░░░░░░  20% ⏸       │
│ Task 4 ███████████████████ 100% ✅       │
└─────────────────────────────────────────┘
```

### 13.2 刊期双进度条

必须分别显示：

```text
翻译完成度 succeeded_count / target_count
发布完成度 online_count / target_count
```

不得把“翻译 6/6、上线 4/6”显示成一个 100%。

### 13.3 每个 Task 单独显示

每篇文章一张 Task 卡，至少显示：

- 标题/来源；
- active task id 短码；
- status；
- current stage；
- Provider；
- circuit state；
- attempt_count；
- 当前 Worker；
- heartbeat / last_activity；
- next_retry_at；
- error code；
- 进度条；
- 操作按钮。

### 13.4 Task 进度条映射

建议阶段映射：

| 生命周期 | 进度 |
|---|---:|
| queued/pending | 5% |
| worker claimed | 15% |
| connecting provider | 25% |
| waiting model | 40% |
| receiving response | 60% |
| validating | 75% |
| persisting result | 85% |
| waiting build | 92% |
| published | 100% |

`retry_wait` 保持最后有效阶段并显示暂停标识，不随时间假增长。

`failed/cancelled/configuration_blocked` 用终止状态展示，不伪装成 100% 成功。

### 13.5 Task 展开详情

点击 Task 后至少展示：

```text
Task ID
文章
刊期
状态
Provider
Provider Circuit
Attempt
Worker
PID / thread
Created
Claimed
Started
Last activity
Heartbeat
Next retry
Hard timeout
Error category
HTTP status
Diagnostic ID
Rebind from
```

并展示历史 Task 链：

```text
Task-A / Provider A / HTTP 502
        ↓ MANUAL_REBIND
Task-B / Provider B / SUCCESS
```

## 14. Admin Action 图形化进度

对“立即探测”“重试”“恢复本期”等动作显示真实步骤：

```text
① Action 已创建        ✅
② 唤醒信号已发送       ✅
③ Worker 已领取        ✅
④ Provider 请求中      🟢
⑤ 结果校验
⑥ 完成
```

示例：

```text
立即探测 · Provider B
████████████████░░░░░░░░ 60%

已提交        21:36:03
Worker 领取   21:36:04
开始请求      21:36:04
当前阶段      receiving_response
已运行        18.4s
```

若 Worker 没领取：

```text
已提交         ✅
已发送唤醒     ✅
Worker 未领取  ⚠️
Fallback wake 正在等待/已触发
```

超过硬 SLA：

```text
ACTION_WAKE_TIMEOUT
[重新唤醒]
```

不得无限转圈。

## 15. Worker 图形化卡片

每个 Worker 卡片显示：

```text
名称
状态
execution_kind
PID
thread_name
当前刊期
当前 Task
Provider
最后心跳
运行时长
下一次唤醒
最后错误
```

建议状态：

```text
Running
Idle
Waiting retry
Waiting provider probe
Circuit blocked
Stale
Error
Offline
Not scheduled
```

UI 颜色只用于辅助，所有状态必须同时有文字，不能仅依靠颜色表达。

## 16. Admin API

建议增加聚合接口：

```text
GET /admin/api/translations/live?edition_date=YYYY-MM-DD
```

返回：

```text
edition
workers
tasks
providers
actions
```

前端每 2~3 秒轮询即可。当前规模不需要 WebSocket。

可选拆分：

```text
GET /admin/api/workers
GET /admin/api/translations/actions/{action_id}
```

所有接口必须使用已有 Admin 鉴权和限流。

## 17. 统一恢复决策函数

把散落在 handler/runner/db 的恢复判断收敛成一个明确决策层，逻辑类似：

```text
active task succeeded
    → NOOP

active task provider == current default
AND provider closed
    → RETRY_CURRENT

active task provider == current default
AND provider open
AND action == probe
    → CONTROLLED_PROBE

active task provider == current default
AND provider open
AND action == retry
    → CONTROLLED_PROBE_THEN_RETRY

active task provider != current default
AND current default has valid SUCCESS test
    → REBIND_TO_DEFAULT

current default test invalid/missing
    → BLOCK_WITH_EXPLICIT_REASON
```

任何 UI 和 CLI 恢复入口都调用同一个决策逻辑，防止不同入口行为不一致。

## 18. 重试与探测预算

### 18.1 自动预算

保持有界自动重试：

- 单 Task 自动尝试：最多 3；
- circuit open 后停止普通自动请求；
- 每个 half-open 周期最多一个 probe；
- 不自动跨 Provider rebind。

### 18.2 人工动作

人工可以显式再次发起 probe/retry/rebind，但：

- 每次必须创建 Admin Action；
- 不能复用历史 attempt 伪造成功；
- 连续双击应返回已有 action 或幂等拒绝；
- UI 必须显示累计人工操作次数和最后结果。

## 19. 并发与幂等

必须覆盖：

- 两个浏览器同时点击“恢复本期”；
- 一个浏览器双击按钮；
- path wake 与 fallback timer 同时触发；
- daily worker 与 resume worker 同时尝试同一 Task；
- rebind 过程中 Worker 正好恢复旧 Task。

原则：

```text
DB lease + BEGIN IMMEDIATE + compare-and-swap active_task_id
```

保证最终只有一个 active Task。

## 20. Worker/Action 健康检查

新增业务健康项，不直接绑定 Docker liveness：

```text
worker_action_stuck
persistent_worker_offline
translation_wake_degraded
```

规则示例：

- 有 requested action > 15s 且无 claim → degraded；
- > 90s → unhealthy；
- account-mail 两个 Worker 均 offline → unhealthy；
- resume 无待办且未运行 → healthy/idle。

## 21. 关键回归测试矩阵

T32 至少需要以下回归：

1. Provider A 连续 502 → circuit open，普通任务停止。
2. A open 后点击“立即探测” → Worker 真正领取并执行一次受控 probe。
3. A open 后点击“重试” → 有明确动作；按规则 probe 后继续或失败，不允许无反应。
4. Provider B 测试 SUCCESS → 可以设为默认。
5. B 配置 fingerprint 改变 → 旧 SUCCESS 失效，不能直接设为默认。
6. A Task 失败、B 设为默认、点击“立即探测” → 创建 B Task，旧 A Task 保留。
7. A Task 失败、B 设为默认、点击“重试” → rebind B 后进入正常重试。
8. 两个并发 rebind → 只有一个有效 active Task。
9. 同一按钮双击 → 幂等，不创建重复有效 Task/Action。
10. `news-digest-wakeup.path` 故意不工作 → fallback timer 在 SLA 内拉起 resume。
11. Action 入库但 Worker 未启动 → UI 显示 waiting/wake degraded，最终 timeout，不永久 requested。
12. Worker 在 claim 后死亡 → lease 回收，Action 终态化并可恢复。
13. Provider probe lease 过期 → circuit 回 open，可再次人工探测。
14. 历史 Provider 恢复后不得覆盖已经由新 Provider 成功的 active Task。
15. 6 篇刊期中 4 成功、1 running、1 retry_wait → Admin 进度和每个 Task 状态准确。
16. translation 6/6、online 4/6 → 双进度条分别为 100%/67%。
17. account-mail claim 出现瞬时 SQLite OperationalError → Worker 不退出，重连后继续。
18. account-mail Worker 人为异常退出 → Supervisor/heartbeat 能发现并按策略恢复或报警。
19. resume 无任务时退出 → Worker UI 显示 Idle/Not scheduled，不误报 Offline。
20. 历史刊期恢复不触发历史邮件发送。

## 22. 生产事故回归样本

把 2026-09-06 至 2026-09-11 的故障模式做成固定测试夹具：

```text
09-06：部分成功 + network/502
09-07：部分成功 + timeout/retry_wait
09-08：pending + retry_wait + probe 未启动
09-09：部分成功 + 502/503/timeout
09-10：部分成功 + retry_wait
09-11：1/6 + 旧 Provider 长期开路
```

测试流程：

```text
旧 Provider open
→ 人工测试新 Provider SUCCESS
→ 设为默认
→ 恢复各未完成刊期
```

最终要求：

- 所有 active article 最终唯一成功；
- 旧 Task/Attempt 全部保留；
- 不重复计数；
- 不重复发布同一文章；
- 不补发历史邮件；
- 不无限 probe；
- 所有 Admin Action 有终态。

## 23. 预计修改文件

主要代码范围：

```text
src/news_digest/storage/db.py
src/news_digest/cli.py
src/news_digest/site_server.py
src/news_digest/admin_providers.py
src/news_digest/operations.py
Admin HTML/CSS/JS 所在模板/静态代码
tests/unit/test_db.py
tests/unit/test_cli.py
site/admin/provider/translation 相关测试
systemd/news-digest-wakeup.timer（或等价部署单元）
部署/发布文档
```

最终以实际仓库结构为准，不为了计划强行拆新模块。

## 24. 实施顺序

### 阶段 A：模型与不变量

- [ ] 定义 Provider test-success/default 约束；
- [ ] schema v13；
- [ ] Task rebind lineage；
- [ ] 原子 rebind；
- [ ] Admin Action 扩展字段和终态规则；
- [ ] 并发/幂等单元测试。

### 阶段 B：恢复行为

- [ ] 统一恢复决策函数；
- [ ] `立即探测` 使用当前默认 Provider；
- [ ] `重试` 使用当前默认 Provider；
- [ ] 单篇“使用当前默认接口恢复”；
- [ ] 刊期级恢复；
- [ ] circuit open 下人工 probe/retry 规则；
- [ ] 真实历史故障夹具回归。

### 阶段 C：可靠唤醒

- [ ] DB Action 作为事实源；
- [ ] 保留 path immediate wake；
- [ ] 新增 30 秒 fallback timer；
- [ ] Action 15s/90s SLA；
- [ ] wake path 故障注入测试；
- [ ] systemd start-limit 回归。

### 阶段 D：Worker 可观测和自愈

- [ ] worker registry；
- [ ] process/thread 心跳；
- [ ] resume/daily heartbeat；
- [ ] account-mail/payment heartbeat；
- [ ] account-mail SQLite OperationalError 自愈；
- [ ] persistent thread supervisor；
- [ ] stale/offline 业务健康项。

### 阶段 E：Admin 图形化控制台

- [ ] Worker 卡片；
- [ ] 刊期翻译/上线双进度条；
- [ ] 每篇 Task 卡片；
- [ ] Task stage 进度条；
- [ ] Provider 状态卡；
- [ ] Action 实时步骤；
- [ ] Task lineage/history 展开；
- [ ] 2~3 秒轮询；
- [ ] 手机端布局可用。

### 阶段 F：发布验收

- [ ] 全量 unit/integration；
- [ ] Ruff；
- [ ] `git diff --check`；
- [ ] package build；
- [ ] Linux CI；
- [ ] 升级前完整恢复包并独立验证；
- [ ] schema migration 隔离验证；
- [ ] 不可变镜像 digest；
- [ ] 生产部署后验证 Worker 面板；
- [ ] 生产执行一次受控 Provider test；
- [ ] 验证一次人工 probe/retry；
- [ ] 禁止历史邮件补发。

## 25. 部署与回滚

### 部署前

1. 生成完整 recovery bundle；
2. `verify_backup()` 独立验证；
3. 保存当前 t31 镜像 digest；
4. 在副本 DB 上执行 schema v13 migration；
5. 运行生产事实校验。

### 部署

1. 应用 DB migration；
2. 部署 t32 不可变镜像；
3. 安装/启用 fallback wake timer；
4. 保持现有 daily/wakeup 行为；
5. 确认 site/admin/web healthy；
6. 验证 Worker heartbeat；
7. 验证 Admin live API；
8. 验证无动作时 resume 不产生异常风暴。

### 回滚

若应用层失败且 schema 保持向后兼容：回滚 t31 镜像并禁用新 timer。

若 schema 变更影响 t31 兼容性，则使用升级前完整 recovery bundle 回滚，不进行人工删列或手工修改生产 SQLite。

## 26. T32 发布硬门槛

以下任意一项失败，禁止发布 `v1.4.0t32`：

```text
Provider A 熔断后“立即探测”能真正执行                         PASS
Provider A 熔断后“重试”能产生明确执行结果                     PASS
新 Provider 测试 SUCCESS 后可人工设为默认                      PASS
设置默认后旧 Task 的人工恢复会使用新默认 Provider              PASS
旧 Task provider_id 永不被篡改                                 PASS
跨 Provider 恢复创建新 Task 且 lineage 可查                     PASS
Admin Action 不会永久 requested                                 PASS
Path wake 失效时 fallback timer 能恢复                           PASS
每个 Worker 在 Admin 中可见                                     PASS
每个 active Task 在 Admin 中可见                                PASS
Task 进度来自真实 lifecycle stage                               PASS
翻译进度与上线进度分开显示                                     PASS
Worker 死亡/心跳过期可见                                       PASS
account-mail 瞬时 SQLite 错误不会静默杀死 Worker                 PASS
并发/双击不会产生多个有效 active Task                           PASS
历史 Provider 恢复不会覆盖新 Provider 的成功结果                PASS
不自动切换供应商                                               PASS
不自动补发历史邮件                                             PASS
```

## 27. T32 完成定义

`t32` 完成后，管理员应该无需 SSH 即可完成以下工作：

1. 看到哪个 Provider 正常、熔断或失败；
2. 测试新的 Provider；
3. 只有测试成功后手动设为默认；
4. 对失败文章点击“立即探测”或“重试”；
5. 对整期点击“使用当前默认接口恢复本期”；
6. 看到每个操作有没有被 Worker 领取；
7. 看到每个 Worker 是否存活、当前在做什么；
8. 看到每篇文章的 Task 进度；
9. 看到每个 Task 的 Provider、Attempt、错误和历史重绑定链；
10. 在系统故障时得到明确的 `failed/timed_out/recovered`，而不是无限等待。

最终运行原则：

> 人负责选择可信 Provider；系统负责熔断、记录、重绑定、可靠唤醒、执行和可观测。任何人工恢复动作都必须有确定的执行路径和最终结果。
