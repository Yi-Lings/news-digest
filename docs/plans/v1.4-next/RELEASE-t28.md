# v1.4.0t28 发布与部署

2026-09-05 20:22 HKT 完成。当前 Plan 的实施项已交付，**仅预发布候选**；
用户观察自然运行后再决定正式 `v1.4.0`，不自动发布稳定版、不移动 tag。

## 不可变身份

- 应用提交：`c6a25c58f6781e113cb943b136cf4b651f6f2d0c`。
- Tag/Release：[v1.4.0t28](https://github.com/Yi-Lings/news-digest/releases/tag/v1.4.0t28)，`isPrerelease=true`、非 latest。
- [main CI](https://github.com/Yi-Lings/news-digest/actions/runs/33965176514)、[tag CI/镜像/Release](https://github.com/Yi-Lings/news-digest/actions/runs/33965302471) 均成功。
- 包版本 `1.4.0` 未变；提交后的完成记录仅为文档增量，不更改候选镜像身份。

| 服务 | SHA-256 digest |
|---|---|
| worker / site / admin | `8fe7bf19bf4dc3ed961a7ccc883f81ffd01a8d3a5823860bc57e814916077d20` |
| web | `0c751c3288d6ead7cc03fbf12ab168c74c980a99d92b8744386f3f9e6974572b` |

镜像仓库为 `ghcr.io/yi-lings/news-digest-worker` 和 `news-digest-web`。
部署包 SHA-256：`abebf1485c0e4bfa5117c3aec2e7774213bec1442c7b477670cb8947bdf1a22b`。
本地和服务器均校验部署包，拉取后核对 OCI version/revision 与上述提交。

## 验收

- Windows：`1016 passed, 1 skipped, 7 deselected`；Linux main：`1017 passed, 7 deselected`；tag 校验通过。
- Ruff、`git diff --check`、源码包/wheel 构建通过。真实模型/SMTP 网络测试未执行。
- Playwright 验证 1440/390 宽的受影响流程：退款显示、到期退订、历史订单分页与搜索；无 document overflow/JS error。全部使用本地合成账号和订单。
- 部署前生产一致性副本迁移与幂等检查通过：22 张旧业务表、302 个当前发布工件不变。
- 实际迁移先冻结 daily/resume/backup 的调度入口，再停止 Site/Admin；schema 11→12，业务表事实再次核对。

## 部署事实

服务器 `/srv/news-digest/compose.yaml` 原位升级，四处镜像均固定同一 Release digest。
没有重新签发 TLS，没有更改默认 provider、源支付/SMTP 配置、用户权益或退订偏好。

迁移后配置激活被旧订单的支付 identity 绑定拦住，未绕过保护规则。使用现有网关查询，
核对商户订单号、网关交易号及金额，确认 3 笔存量订单全部为 `TRADE_CLOSED` 后登记查询结果。
随后配置正常激活；没有新增付款、退款或权益动作。纯迁移前后业务事实相同，
这 3 笔订单的最终关闭记录是单独、经网关确认的业务修正，不混记为迁移变化。

- 停写备份：`/srv/news-digest/backups/t28-20260905-121531/`，含 schema 11 DB、事实指纹、发布/缓存/邮件/配置/secret 和旧 Compose。
- 自动备份：`/srv/news-digest/backups/daily/daily-20260905T122213Z-e0ef4cae.tar.gz`。
- 首次备份耗时约 23.13 秒；独立 `--network none` 容器解包复核 `verified=true`，29 张表、2105 个文件，包含发布身份验证。
- 备份文件 0600、目录 0700；daily/resume/backup service 结束，timer/path 恢复。
- 下次日报：09-06 08:00 HKT；下次日常备份：09-06 10:30 HKT。

## 线上结果

- Site/Admin/Web 均 healthy，无重启、无 OOM；首页及 healthz 为 200，内部 `/release.json` 为 404。
- 当前刊期 `2026-09-05` 为 complete，目标/有效/上线 6/6/6，当前 task 全部 succeeded。
- 当天无正式邮件收件人，保持 `NO_ELIGIBLE_RECIPIENTS`；现有订阅仍为 unsubscribed。
- 7 笔订单仍为 paid 3、expired 4，未新增资金或权益；无开放 payment case、无待核对任务。
- outbox 无积压，业务状态检查全部 healthy；日志无本次启动后的异常。
- DB 约 23.32 MiB；Admin 18.79/32 MiB、Site 27.90/64 MiB；回环首页 10 次采样 p50 1.10 ms、最大 17.80 ms，非负载测试承诺。

## 观察与恢复

后续自然刊期开始积累新抓取诊断；不回填旧刊物，不补译、重建或补发历史邮件。
观察自动抓取/句子修复/发布、后台结算、验证码、备份年龄及持续异常日志。
数字、实体、否定等信号不恢复为质量硬门，provider 不锁定 Terra。

schema 12 不兼容 t27；回退不能直接换旧镜像。先停止全部写入者，并核对备份之后的
外部到账、退款及投递事实。本次已登记的 3 笔网关关闭结果也应保留；优先前滚修复。
日常备份只提供本机恢复点，未实现异地灾备；正式稳定发布继续由用户观察后决定。
