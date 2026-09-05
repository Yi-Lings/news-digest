# 架构复核证据

日期：2026-09-05。对应 [PLAN.md](PLAN.md)。本文件区分当前源码、历史生产记录、本轮线上观测与合成验证，不表示下面所有风险都已在生产发生。

规划已按用户要求精简；本文件保留事实证据，不代表每项发现都必须修复。当前取舍以 PLAN 和 SYSTEM_REVIEW 第 0 节为准，取消或暂缓不会改变历史观测，也不代表已实施修复。

用户随后明确：句子重译应在校验失败后自动调用模型修复具体句子。此前人工选句与审核方案是规划解释偏差，已取消；事实证据不要求实现人工编辑功能。

## 1. 检查基线

- Git HEAD：`26faa3d`，提交内容为取消数字翻译硬门。
- 工作树已有修改：根目录 PLAN 与 `translation/quality.py`、`schema.py`、`service.py`；未跟踪 `.env`。本轮未改这三个 Python 文件或配置。
- 原计划 1,461 行；既有历史同时出现 1.2.x、1.3、1.4 及互相覆盖的质量策略。
- schema：`storage/db.py` 的 `SCHEMA_VERSION = 10`。
- 规模：`storage/db.py` 6,471 行，`preview_server.py` 5,331 行，`site_server.py` 2,580 行。行数仅用于理解职责聚集，不单独作为重构理由。

## 2. 线上与历史生产证据

### 本轮直接观测（14:40-14:46 HKT）

- 通过校验证书的 HTTPS GET 请求 `https://news.cheapcoding.top/healthz`，返回 HTTP 200。
- 前次查询因 SSH agent 不可用中断；用户加载 identity 后，`ssh-add -l` 和 `ssh cheapcoding` 均成功，主机与 t26 部署记录一致。
- SQLite 使用标准库 `mode=ro` 与 `PRAGMA query_only=ON` 查询，没有调用会触发迁移或恢复的应用 `db.connect`。短只读事务用于相关统计的一致性。
- 没有停止服务、修改生产数据、触发抓取/翻译、发送邮件或执行支付。

`site_server.py:839` 的 healthz 只返回页面，不读取刊期、任务或 SMTP，因此不能凭 200 宣称当日任务和邮件正常。

| 现场项目 | 实测结果 |
|---|---|
| 运行版本 | Site/Admin/Web 均为 `v1.4.0t26`，revision `26faa3d235a5559ba7705c3a5c9c1004eedb2c09`；镜像 digest 与本地同一 Release 的记录一致 |
| 数据库 | `/data/news.db` schema 10，quick_check=ok，约 16.7 MB；同目录 digest.db 为零字节非运行库 |
| 调度 | timer/path active；daily 于 08:00:29 启动、08:11:11 以 0 退出；next timer 为 09-06 08:00 HKT；resume 非运行，无重启循环 |
| 09-05 内容 | target/succeeded/online=6/6/6，dirty/built=6/6；6 条任务对应 6 篇不同文章，全部 succeeded/online |
| 09-04 内容 | target=6，任务 12 条、不同文章 6 篇；当前 provider 成功 6，旧 provider 成功 4、失败 2；汇总成功/上线却为 10/10 |
| 历史失败 | 仅 09-04 的 2 条旧 provider 任务保留 CONTENT_NUMBER_MISSING；无 pending/running/retry_wait |
| 成功提交缺口 | `succeeded AND success_generation IS NULL` 为 0；本轮合成复现的 E2 不代表现场存在该悬挂 |
| 原文归属 | 1 条 08-24 的 succeeded/online task 引用文章当前归属 08-23；源文仍存在，不是整篇丢失，也不能仅据此判定是哪次重抓造成 |
| 09-05 邮件 | edition=delivered；auto run=completed，total/sent/failed/unknown 均为 0；09-03 同样零目标/零发送 |
| 当前收件资格 | 一个 active 且会员未过期的账号处于 unsubscribed；另一 active 账号无会员且未订阅。有效正式目标为 0 |
| 投递历史 | 最新有实际正式发送的刊期为 09-01，sent=1；当前无正式 sending/failed/unknown；email_test_attempts 为空；验证码 outbox 为 2 条 sent |
| 09-04 邮件 | 刊期现为 complete + DELIVERY_EXPIRED，而非历史部署记录中的 DELIVERY_FAILED；未发现该日正式投递 run |
| 发布结果 | current 为 `2026-09-05-04`，08:11:05 发布，6 篇已译主文；5 个保留 release，当前包含 42 个归档日期目录 |
| 资源 | Site/Admin/Web 内存约 9.6/13.9/0.8 MiB；3 个容器 RestartCount=0、OOMKilled=false；未据短快照推断峰值 |

生产零发送不是当前 SMTP 失败的证据。`delivery_service.py` 将空收件人的 run 结束为 completed 并返回 skipped；`cli.py` 又将 skipped 当作已完成投递，使 edition 成为 delivered。需要区分 `no_eligible_recipients` 与 `already_sent`，不修改用户退订选择。

当前默认 provider circuit 为 closed；另有历史 provider 保留 open，不影响今天完成。正式邮件开关为 true、窗口为 6 小时。遗留匿名订阅环境键为 true，但 1.4 旧端点已在代码中禁用，不能把配置残留当成新的公开入口事故。

### 历史生产记录

来源：主代码手任务 `019fa37d-c2b6-7df0-9af3-1cebc9883780` 的完成记录，以及本机 `var/deploy-log.txt`、`var/release-v1.4.0t26/digests.env`。运行文件未作为公开证据复制。

- t26 部署日志时间约为 2026-09-05 00:27，Asia/Hong_Kong；标签与 worker/web revision 一致，备份和健康检查完成。
- 主代码手记录：El Nino 的 Terra task 从第 6 次尝试进入第 7 次并成功；旧 Luna task 保持失败；增量构建随后完成，`online_count=10`；timer/path 已恢复。
- 同一记录中刊期 `complete`，仍留 `DELIVERY_FAILED`；本轮实时查询已变为 `DELIVERY_EXPIRED`。历史记录不能覆盖现场状态，二者都不证明 SMTP 的具体错误阶段。
- 更早 t25 记录已明确：Luna/Terra 的重复成功记录使刊期 complete 不可信。
- 生产数字失败只留下通用错误码，年份/导航修复曾依靠推断；用户随后明确取消数字硬门。

上述运行版本、task 聚合、generation、邮件与调度已补齐。仍无法从现存记录恢复未留存的历史 SMTP 原因，也未核验真实收件箱；未导出用户地址、凭据或完整新闻正文。

## 3. 已复现的一致性缺陷

使用现有 storage API 在 `TemporaryDirectory` 内建立三个合成场景，进程退出后清理。无公网、生产数据库或真实 provider。执行结果 exit code 为 0。

### E1 重复 provider 虚增完成数

构造目标两篇：article A 在 provider A/B 下各成功一次；article B 在 provider B 下终态失败。通过现有 claim/success/failure/build API 完成状态迁移。

```json
{"target_articles":2,"unique_successful_articles":1,"state":"complete","succeeded_count":2,"online_count":2}
```

源码：`storage/db.py:1660`、`:1752` 将 provider 纳入 task identity；`:2937` 的 `finish_automation_build` 对 task 行求和，并用 `>= target_count` 判 complete。

结论：目标成员与执行尝试未区分，属于实际错误，不只是 Admin 展示重复。需要按固定成员覆盖率判定。

现场 E1 的计数虚增已直接证实：09-04 六篇不同文章计为十篇成功。当前六篇实际上都已成功，不能据合成场景声称此刻仍有主文缺失。

### E2 成功提交后未登记构建

构造已领取 task，调用 `finish_translation_task_success` 后模拟中断，跳过 runner 下一次 `mark_translation_ready_for_build`；执行启动恢复和 maintenance。

```json
{"task_status":"succeeded","success_generation":null,"build_queue":[]}
```

源码：`translation/automation.py` 的 `run_ready` 依次执行 `upsert_articles`、`succeed_translation_work`、`mark_translation_ready_for_build`；三个函数分别提交事务。`storage/db.py:2424`、`:2827` 分别处理 success 和 generation。

结论：恢复机制未补齐该中断窗口。此验证未模拟真实进程被杀，但准确构造了可由顺序提交产生的持久状态。

### E3 同一 URL 跨日移动文章

用相同未译 Article 先写入 09-04、再写入 09-05，通过 `get_edition` 查询两天。

```json
{"prior_edition_article_count":0,"current_edition_article_count":1}
```

源码：`storage/db.py:535` 的 articles 以 URL 为主键；`:3654` 的 upsert 对未译行使用 `INSERT OR REPLACE` 更新 date。已译行有保护，不能把本结果扩大成“所有已译历史都会丢失”。

结论：数据库未能表达同一源文章在多刊期中的独立归属；已发布 manifest 暂时保留旧内容，但从数据库重建不可靠。

## 4. 其他源码发现

| 证据位置 | 事实 | 影响与边界 |
|---|---|---|
| `pipeline.py:selected_edition/load_db_editions` | build 使用当前时间重新选题 | 同一期翻译目标与页面成员可能漂移；本轮未复现所有评分组合 |
| `cli.py:_run_automation_resume/_run_automation_daily` | resume 调用 daily 并 seed 当前 provider；action_required 遍历全刊期任务 | 旧 provider 失败参与退出，恢复还可能创建新任务 |
| `translation/automation.py:_frozen_counts/_article_for_task` | 仅冻结逐段句数，原文读取可变 article 表 | 不是不可变原文快照，不能用于证明稳定句子坐标 |
| `translation/schema.py:apply_translation` | 持久化 Article 合并句子为 Paragraph；句级数组主要在缓存 | 局部重译不能依赖永不删除的缓存，需要保存完整接受结果 |
| `pipeline.py:build_editions` / `publisher.py:_prune_releases` | 根 manifest 只保存最新刊期，HTML release 仅保留 5 个；现场当前 release 内有 42 个归档日期 | R1 需补多刊期发布索引与独立元数据保留，不能把有限根 manifest 当作完整历史事实 |
| `storage/db.py:retry_edition_failed_tasks` / `translation_attempts` | 重绑定会原地更改 task.provider_id，attempt 无 provider 字段 | 旧归属仅凭现有 task 无法还原；迁移需允许 unknown，新 attempt 冻结归属 |
| `storage/db.py:task_capabilities` / `translation_admin_actions` | succeeded task 不可调度，现 actions 无人工候选内容和基准 revision | 自动修复在原 task 成功前执行，无需新增人工候选或审核字段，也不回退成功 task |
| `translation/schema.py:parse_translation` / `translation/service.py:translate_article_once` | 空句只报告段落；InvalidTranslation 优先走全文反馈修复，单句草稿主要接收已有效的结果并修数字诊断 | 需保留未通过校验的候选及具体失败坐标，先自动修可定位句再严格复校；不能只把人工流程改名为自动 |
| `translation/client.py:translate_with_feedback/_request_text` | 各请求重新计算 deadline；feedback 未接收共享剩余预算 | 首次加两次修正可用到约 1,800s，默认 lease 为 900s；本轮未据此断言生产已并发重复执行 |
| `translation/service.py:_attempt_with_gates` | t26 已有软信号整篇修正，宽泛捕获 Exception | 非阻断不等于不重写，也可能吞取消错误；应去除诊断自动请求 |
| `translation/schema.py:parse_translation` | 标题 40 字与教学字段数量是整篇拒绝条件 | 正文与附属内容的失败域未区分 |
| 未提交 `service.py:cache_key/translate_article_once` | repair 版本进入整篇 key，命中缓存后仍可能修复 | 附加功能可能造成无必要的整篇未命中和额外费用 |
| 未提交 `quality.py:sentence_evidence` | 局部修复 evidence 只由数字匹配产生 | 不等价于通用失败句定位，与取消数字硬门后的首要问题不匹配 |
| `cli.py` 与 `translation/service.py` | 独立 translate 与自动化有不同预算、重试、缓存写入路径 | 两个入口的同一业务行为容易分叉 |
| `preview_server.py` / `site_server.py` | 直接 SQL、HTTP 编排和内联 HTML/JS 共存 | 原计划“SQL 只在 storage、完整流程只在 pipeline”已不是真实结构 |
| `delivery_service.py:_validate_auto_window` | 必须当天且在 08:00 加配置窗口内 | 迟完成属于投递策略拒绝，不能都解释为 SMTP 故障 |
| `delivery/mailer.py:_deliver_one` | DATA 写入/确认阶段断连可返回 unknown | 必须保持不确定语义，不能为省事自动改成 failed 或 sent |
| `deploy/nginx/news.conf` / `compose.yaml` | 读者入口走 Site，Web 仍常驻且参与发布 | 具备退役评估价值；尚未完成线上引用清点，不直接删除 |

## 5. 已有能力与不应重复建设的内容

- `task_capabilities` 已实现；应让调用方真正同源，不另写一套动作判断。
- WAL、busy timeout、task/build/delivery lease、circuit 和 maintenance 已存在。
- `test_attempts`、`translation_attempts`、admin actions、delivery runs、account outbox 已有持久化。
- Admin 测试邮件已有幂等、`error_stage`、`unknown_pending` 和下一步提示。生产问题先核对数据与实际响应。
- 支付已有事务确认、网关交易唯一与重复回调防重；新架构保留该边界。
- Release manifest、固定 digest 和 SQLite online backup 已有可用实现。

## 6. 验证范围

本轮是规划与只读分析，没有运行全量 pytest、构建或发布。后续扩展审查已覆盖账号、支付及人工权益的重要路径，但不代表穷尽所有路径。三个临时数据库复现针对本轮架构判断；下一实施阶段应把它们固化为正式回归测试。

本次补充仅访问生产只读状态并修订文档；未新增业务测试或修改代码。早先独立复核涉及 R1/R3 持久化归属、manifest 保留及 provider 历史迁移；其中人工候选生命周期后来按用户澄清取消，由原 task 内自动修复取代。

引用的源码行号基于本轮工作树，后续实现可能移动；以函数名定位为准。历史记录中的 `915 passed` 属于此前 t26 验证，不能当作当前未提交草稿的测试结果。

## 7. 扩展系统审查

完整发现、逐项代码出处和最小改进见 [SYSTEM_REVIEW.md](SYSTEM_REVIEW.md)。新增内容/账号支付/运维三个只读审查视角，重点修订了“支付认证无需改造”和“运行正常即可排除核心风险”的过宽判断。

- 内部发布 manifest 包含完整双语正文及教学内容，通用静态文件返回缺少公开清单；路径解析与授权分类不共用可信资源身份。仅静态审查，未构造绕过请求、未做线上漏洞复现。
- 短正文在提取阶段被无条件删除；不同文章可以得到相同标题 slug，构建缺少唯一性契约。现有 publisher 确实检查最新刊期文章文件存在，不能把它误写成完全没有文件检查。
- 本地订单取消、迟到到账、人工权益并发和跨文件配置生效存在独立缺口；当前没有新增证据证明生产已发生异常扣款、权益丢失或配置分裂。
- 15:01 HKT 补查生产 timer 列表及备份目录文件元数据。项目目录最新 SQLite 备份为 t26 部署前的 09-05 00:27 HKT，未见当日刊期后快照；timer 列表没有项目专用备份任务。root cron 存在通用备份条目，外部快照未核实，不据此宣称服务器没有日常或异地备份。
- 现有恢复和离线 tar 说明遗漏常驻 Site 及其 account outbox 写入者；这不否定 bootstrap 已有 SQLite online backup 的一致性保障。
- 注册条款已经涉及账号、支付与 Cookie，但独立隐私页面、同意版本与数据保留仍不完整。旧匿名订阅 JS 在当前模板中没有触发点；已有阅读模式、来源筛选、滚动进度及段落/全文 TTS 不作为“缺失功能”重复建设。

扩展审查没有读取生产备份内容、密钥或用户地址，没有调用支付网关或发送邮件。方案仅写入文档，业务源码保持原有草稿。
