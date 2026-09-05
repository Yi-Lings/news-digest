# t28 架构验收

日期：2026-09-05。仅按当前 PLAN 的取舍验收，历史 SYSTEM_REVIEW 不额外扩展范围。
包版本仍为 `1.4.0`，候选 tag 为 `v1.4.0t28`。已完成发布部署，结果见 [RELEASE-t28](RELEASE-t28.md)。

## 范围对应

| 编号 | 本轮交付与证据 |
|---|---|
| B1/B2 | 复用 t27 内部 manifest/快照访问隔离和共同目录边界，沿用静态访问回归。 |
| C1 | 提取保留短段、短引语、列表、表格内容；保存提取器版本、段数和降级原因。`full` 仅兼容旧枚举，不证明原站完整。 |
| C2 | 复用 t27 URL 摘要唯一路径、构建冲突检测和旧链接保护。 |
| C3 | 新候选区分 published/updated/fetched，过滤异常未来日期并记录数量。 |
| C4 | 来源 raw/parsed/window/full/summary/selected 与失败阶段持久化，Admin 可查看；全失败也保存报告。 |
| C5 | 复用 t27 教学字段过滤降级，正文位置契约；按 PLAN 不加出处匹配器、语义硬门或人工句子审核。 |
| C6 | 新简讯保存发布时间、来源 key、选择原因，时间倒序稳定排序，筛选来源取主文/简讯并集；旧未知时间保持未知。 |
| C7 | 保存现有规则评分及 selected/source_cap/similar_title/capacity 依据，不加主题配额、人工替换平台。 |
| C8 | 7 个既有来源 feed 固定样本与合成结构正文样本，覆盖引语、列表、表格、单位、否定和专名。不是实测模型语义质量评分。 |
| A1 | 取消不抹除结算窗口；已验签且订单/金额/交易号核对通过的迟到到账持久为 received，不静默丢失、不擅自发权益。 |
| A2/A4 | 付款、兑换、赠送共用事务权益命令；operation ID、actor、reason、前后值可追溯。并发赠送/付款和重复命令回归通过。 |
| A3 | Site 独立后台线程领取持久 check lease，最多 8 次、指数退避；账户 GET 不查网关，超窗/耗尽进入待核对。 |
| A5 | 到期账号可以关闭已有简报偏好，仅启用要求有效会员；HTTP 回归与浏览器退订验证。 |
| A6 | 验证码消费与激活/改密/撤销会话同事务，注入业务写入失败后验证码仍可用。 |
| A7 | 只读核验现有 fastpay-adapter `/app/adapter.py`：amount_leases 以 merchant_no/pay_type/amount_key 唯一占用，out_trade_no 唯一；不能取消现有 21 个金额槽位。Admin 显示占用，原有槽位耗尽错误保留。 |
| A8 | Admin 可按凭证补权益、登记外部退款/争议、核实未到账关闭；权益扣减显式指定且幂等。账户及 Admin 历史分页、搜索和退款状态均验证。无真实退款 API 调用。 |
| O1/O6 | 共同 dotenv parser/serializer，重复键拒绝；源文件权威、Site 投影白名单、desired/applied revision、失败恢复。注入文件失败与 DB rollback 回归；bootstrap 时区同源解析。 |
| O2/O3 | 停写门禁覆盖 Site/Admin/daily/resume/backup/人工 CLI；日常 SQLite online backup、配置/secret/缓存/邮件/发布工件完整恢复包，保留 14 份。隔离核对文件、数据库及发布关系。无异地备份承诺。 |
| O4 | Site `/readyz`、Admin `/healthz` 和 Compose 检查；刊期、任务阻断、邮件 failed/unknown、outbox、付款异常、备份年龄、磁盘状态；持续 10 分钟与恢复各记录一次。仅 Admin 和日志，不宣称外部推送。 |
| O5 | 复用 t27 PR/main/tag 相同离线校验；最终提交须通过 Linux CI 后才创建唯一候选 tag。 |
| O7 | `.env` 已忽略；移除确认无模板引用的匿名订阅 JS；更新错误的活库 tar/站点可随时重建说明。 |
| O8 | Site 顶层异常记录 request ID、阶段和异常类别，不写 query、验证码、凭据、完整回调；后台对账错误仅记类别。 |
| O9 | 记录数据库体积/磁盘余量和备份耗时；每日最多各删 500 条、30 天前的过期 session/code/已结束账号 outbox，不删资金和正式邮件事实。无证据支持更换 DB 或增加缓存平台。 |

## 已执行验证

- 冻结源码后的全量离线测试：`1016 passed, 1 skipped, 7 deselected`；Ruff、`git diff --check`、源码包与 wheel 构建通过。未执行标记为 network 的真实服务测试。
- 生产 11:51 UTC 一致性副本，schema 11→12：22 张旧业务表 fingerprint 不变；当前刊期 6/6/6、发布 `2026-09-05-04`，302 个工件文件未改；再次打开数据库不产生迁移增量。
- 副本未调用模型、支付或 SMTP，不补译、重建或补发任何刊期。
- Playwright / Edge headless：1440 与 390 宽，账户退款、到期退订、Admin 第 2 页历史退款及全库搜索验证，无 document overflow、无 JS error。使用 205 笔本地合成订单，无生产账号或凭据。
- 备份 roundtrip、改包 hash 错误及缺刊期页面拒绝；迁移注入失败 rollback；配置投影文件失败/DB rollback 恢复；持续故障/恢复告警；有界清理回归。
- 支付最后一次 check 进程中断后进入 unconfirmed；升级前无权益回执的已支付订单不能经争议处理重复开通。
- t27 生产切换前 10 次回环首页请求（使用正式 Host）p50 3.02 ms、最大 43.2 ms；数据库约 23.3 MiB，Admin/Site 无 OOM 和重启。该样本不是负载测试或延迟承诺。
- 句子修复、provider 重绑定、数字软诊断、旧 manifest 序列化兼容继续纳入全量离线套件。
- Linux main CI：`1017 passed, 7 deselected`，tag CI、镜像及 Release 工件构建通过；已部署同一 Release digest。
- 停写迁移保持原有事实；旧配置绑定阻断经真实网关查询确认 3 笔旧订单为 TRADE_CLOSED 后解除，仅登记关闭，不更改权益。
- 生产首个恢复包通过自动校验，并在独立无网络容器中复核：29 张表、2105 个文件，manifest/发布页面/数据库结果身份一致。
- 线上当前刊期 6/6/6，业务检查全部正常，Site/Admin/Web healthy，无 OOM/重启；回环首页 10 样本 p50 1.10 ms、最大 17.80 ms。自然运行质量留待观察，不承诺未来无故障。

## 观察边界

用户观察自然运行后决定正式版。本轮不改退订偏好、不补发历史邮件、不引入数字质量硬门；
新抓取诊断从后续自然刊期开始积累，不回填旧刊物。模型真实语义质量仍需实际阅读判断，
RPO 24h/RTO 4h 仅为观察目标。异常资金人工核对与真实退款由运营处理，不把清除会员当退款。
