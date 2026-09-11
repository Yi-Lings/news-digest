# v1.4.0 生产失败驱动的架构迭代计划

更新时间：2026-09-11（Asia/Hong_Kong）。生产版本：`v1.4.0t30`。本文件是新的执行计划；既有 `PLAN.md`、`SYSTEM_REVIEW.md`、`EVIDENCE.md` 保留历史事实。

## 目标与边界

修复抓取、固定选题、翻译、构建、投递和恢复之间的业务闭环。继续使用单 worker、SQLite、Compose、systemd；不引入微服务、消息队列、复杂监控平台或高强度安全改造。本轮只规划，不写生产数据、不调用真实翻译、支付或邮件。下一版本从 `v1.4.0t31` 编号。

句子修复固定为：整篇校验失败 → 定位 `P#S#` → 自动调用模型重译单句 → 合并 → 重新校验。人工选句和人工审核不作为前置；无法定位的结构错误才在剩余预算内整篇重试。

## 生产证据（09-06 至 09-10）

通过 SSH agent 主机别名 `cheapcoding`，以 SQLite `mode=ro`、`query_only=ON` 查询；未读取密钥、`.env`、邮箱或完整正文。

| 刊期 | 目标 | 成功/上线 | 失败或等待 |
|---|---:|---:|---|
| 09-06 | 6 | 2/2 | 4 failed（3×`NETWORK_CONNECT_FAILED`、1×502） |
| 09-07 | 6 | 3/3 | 2 failed（2×timeout）、1 retry_wait（502） |
| 09-08 | 6 | 0/0 | 4 pending、2 retry_wait（502，跨日残留） |
| 09-09 | 6 | 1/1 | 4 failed（3×502、1×timeout）、1 retry_wait（503） |
| 09-10 | 6 | 4/4 | 1 failed、1 retry_wait（502） |

五天目标 30 篇，成功 10 篇。近期请求审计为 34 次 provider 失败、20 次 `read_timeout`、4 次 network、2 次 429、10 次成功、1 次结构无效；read timeout 中位约 30.17 秒，成功请求中位约 54.16 秒。主因是 provider 基础设施不稳定，不能归因到具体模型或内容 schema。

至少三个 circuit 长期开路（连续失败 6/8/7，open_count 2/4/4，恢复成功 0）。09-10 worker 以状态 10 提前退出；当时下一次 probe 约两分钟后。`resume.service` 已 `start-limit-hit`，`wakeup.path` 只监听 Admin 文件，不能按 retry/probe 到期唤醒。容器 healthy、无重启/OOM，说明进程健康不代表刊期完成。业务事件同时为 `readiness=healthy`、`edition_overdue=unhealthy`、`backup_overdue=unhealthy`、`translation_blocked=healthy`，后者与未完成刊期矛盾。

## 必须修复的缺陷

### P0 调度恢复闭环

worker 不能因一篇终态 failed 就在其他任务仍处于 `retry_wait`、pending 或 probe 冷却时退出。退出前统一计算“现在可执行、未来可执行、预算耗尽、不可恢复”；存在未来到期项时保留持久唤醒（按最早 `next_retry_at`/`next_probe_at` 的轻量 timer 或等价机制）。修复 `start-limit-hit`，不依赖人工改 wake 文件。验收覆盖“failed + 冷却 probe + pending”最终自动收口。

### P0 provider 隔离与归属

保留现有 circuit、单 probe lease 和每篇每刊期三次自动预算。open 后停止普通请求，仅允许受控 probe；故障只阻断该 provider，不阻塞其他 provider、文章或新刊期。备用 provider 必须新建 task/attempt，冻结真实 provider；旧任务显示等待、受控重绑定或超期停止。未知/disabled provider 不得静默套用默认配置。验收证明 502/503/network/timeout 不产生无限 task，备用成功不计入旧 task。

### P0 刊期状态与完成数

完成数按 `edition_items` 唯一 `(edition_date, article_id)` 覆盖率计算；重复 provider task、历史 task 或重复构建不能补数。状态区分 `complete`、`partial`、`blocked_by_provider`、`retry_wait`、`overdue`、`build_failed`。达到预算或截止时间后必须收口，禁止未完成刊期显示 complete/delivered。09-08 残留必须有自动收口回归。

### P1 重试、超时和诊断

文章、刊期、provider 三维限制请求数和总时间；502/503、连接失败、读取超时、429 有界退避，open 不消耗普通预算。每次 attempt 记录 provider、请求序号、句子目标、阶段、耗时、HTTP 状态、结果和 circuit 状态；历史缺 provider 保持 `unknown`。先依据真实成功耗时评估约 30 秒 read timeout，再决定是否调整，不以延长超时掩盖故障。

### P1 resume、跨日和 provider 切换

resume 只消费已冻结成员、原文和分句快照，不重新抓取、选题、seed 或移动文章。统一 runner、CLI、resume 的任务范围和退出条件，明确新刊优先、旧刊截止时间。默认 provider 改变后旧任务只能等待或受控重绑定；最新刊期结束不能丢弃旧刊期可执行任务，旧刊失败也不能终止新刊期。

### P1 发布与投递事实

默认不发布不完整刊期；若运营选择 partial，页面、manifest、邮件显示真实 `x/target` 和缺失原因。`blocked_by_provider`、`overdue`、`no_eligible_recipients`、`already_sent`、`failed`、`unknown` 必须分开。部分成功、零目标或投递未确认不得写成 delivered。发布只使用固定快照，构建中断可恢复且不重新翻译。

### P1 自动单句重译

保留可定位失败候选和坐标；空槽位等可定位错误自动单句 repair，传入冻结原句、旧译文、原因和上下文，合并后复校。JSON/段句结构无法定位时不猜坐标，仅整篇重试。数字、实体、否定、长度只做软诊断，不触发请求。不得新增人工候选或审核状态。

### P2 健康与备份

`readiness` 只表示进程/数据库/站点可用；`edition_overdue`、`translation_blocked`、`backup_overdue` 从同一 `operational_snapshot` 派生。修复当前 `translation_blocked=healthy` 矛盾。每日备份继续关闭，仅按需/升级前生成完整恢复包，转存 `E:\backups\news-digest`，服务器保留最近一份并记录时间、覆盖范围、校验和恢复步骤。

## 迭代顺序与验收

1. **t31 调度恢复**：统一范围/退出条件、到期唤醒、start-limit 恢复、blocked/overdue 收口；验证 09-08 残留不会永久等待。
2. **t32 provider 隔离**：备用 provider 新 task、三维预算、诊断字段；验证不跨 provider 计数、不无限 probe。
3. **t33 发布一致性**：唯一 article coverage、刊期状态机、manifest/页面/邮件一致；验证重复 provider 不虚增完成数、零目标不 delivered。
4. **t34 单句 repair 与健康**：失败坐标、自动重译、共享预算、统一 operational snapshot、最小备份状态。

每版必须通过受影响离线回归、Ruff、`git diff --check`、包构建和 Linux CI；schema 变更先做 online backup、隔离迁移和恢复验证。只有连续自然刊期无无唤醒 pending/retry_wait、provider 故障按预算收口、完成数等于唯一文章覆盖率、发布/邮件/健康状态与业务事实一致后，才考虑正式 `v1.4.0`。

## 本轮明确不做

人工选题审批、人工句子审核、数字翻译硬门、语义评分平台、复杂监控、微服务、消息队列、每日备份、历史刊期补译、自动补发邮件，以及 SEO/学习分析/增长统计等产品扩展均不阻塞本轮架构修复。

## t31 实施结果

- [x] 自动化循环在当前无可执行任务但存在持久 retry/probe 冷却时，按最早到期时间等待，不提前退出或忙循环。
- [x] resume 遍历所有未完成刊期；单个刊期存在终态人工任务时不阻塞其他旧刊期恢复。
- [x] 为 pending、retry_wait、provider probe 和人工动作统一计算下一次唤醒时间；provider open 冷却只等待 probe，不把普通任务误判为立即可执行。
- [x] t31 候选已通过 Windows 全量测试、Ruff、包构建、Linux CI，并使用不可变镜像 digest 部署生产。
- [x] 部署后清除历史 `news-digest-resume.service` 的 `start-limit-hit` 标记并验证恢复服务正常退出；未静默重绑定旧 provider、未修改数据库 schema。

生产中仍有绑定旧 provider 的历史任务；这是受控重绑定边界，不由 t31 自动套用当前默认 provider。重绑定后由同一恢复链消费，下一阶段 t32 再处理 provider 隔离与归属闭环。
