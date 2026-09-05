# v1.4.0t29 发布与部署

2026-09-05 21:23 HKT 完成镜像切换，21:25 HKT 业务复核通过。
仅发布测试候选，保留 t28 tag；正式 `v1.4.0` 继续等待用户观察。

## 范围

- 普通支付显示“已支付 / 已开通”，不再展示补发权益；只有异常到账且未开通才提供补开通。
- 移除通用“结算处理”下拉框，退款、争议和未到账关闭分别提供独立、默认折叠的入口。
- 凭证字段使用可见标签；退款扣减天数仅针对已开通订单，整数范围 0 至 3660，默认 0。
- 退款确认明确不会发起网关退款；补开通按原套餐，其他操作不传入开通天数。
- 保留支付自动开通、后端资金与权益事务、权限和幂等逻辑；无 schema 迁移。
- 按用户要求停用每日备份，部署脚本不再重新启用；升级前仍按需备份并优先转存本地。

## 不可变身份

- 应用提交：`51d9f506fcf0689c6f20bf37c897c77ad00e04df`；包版本仍为 `1.4.0`。
- [Tag/Release](https://github.com/Yi-Lings/news-digest/releases/tag/v1.4.0t29)：预发布，非 latest。
- [main CI](https://github.com/Yi-Lings/news-digest/actions/runs/33968378110) 和 [tag CI/镜像/Release](https://github.com/Yi-Lings/news-digest/actions/runs/33968378162) 均成功。
- worker/site/admin：`ghcr.io/yi-lings/news-digest-worker@sha256:08e4589188837dea5b9e28f832b23eb805d03ba48b515763603c102c9f90ca56`。
- web：`ghcr.io/yi-lings/news-digest-web@sha256:ebd07483a408a0aab3f39ba21245b34fac359d54e6ec5498fed13e5337382220`。
- 部署包 SHA-256：`b499fa5f484e318cfb27cb905b24fdc9abf9b09e10a93e8c3bc5e3c7b89f1070`，本地与服务器一致。
- 线上 Admin HTML 与本地测试版本 SHA-256 一致：`efb1c9c26f2eef155409d1c37c532d4f05a230f92830ae1cc978db7ec201fb35`。

## 验证

- 针对性回归：151 passed；Windows 全量：1018 passed、1 skipped、7 deselected；Linux tag 全量：1019 passed、7 deselected。
- Ruff、`git diff --check`、源码包和 wheel 构建通过；未执行真实模型、SMTP 或生产支付操作。
- 本地 Playwright 在 1440/390 宽验证正常单、异常到账、待核实、退款、争议及关闭状态；无 JS error 或 document overflow。
- 浏览器实际调用隔离 Admin API，验证补开通只增加一次权益、退款仅扣明确天数、未开通退款不扣天数、未到账关闭及争议登记。
- 缺失凭证、空白/负数/小数/越界天数不会提交；模拟请求失败后的重试沿用同一操作编号。

## 备份与生产核对

冻结 daily/resume/backup 调度并停止 Site/Admin，使用旧镜像生成完整恢复包。
本地 `E:\backups\news-digest\daily-20260905T132111Z-67a1a11e.tar.gz` 通过独立恢复验证：2,105 个文件、29 张表。
SHA-256：`93c775c1246ee42ace03266051ede0e77bef5350d2d49629de322ca42d907039`。
本地同时保存 `compose-before.yaml` 和 `upgrade.json`；文件包含敏感配置，只能保存在受限目录，不得提交 Git。

本地校验通过后才替换线上四处 image 引用；8 张资金/权益/投递业务表指纹、317 个配置及当前发布文件不变。
schema 保持 12，订单仍为 paid 3 / expired 4，无开放 payment case 或待查单任务。
当前刊期 2026-09-05 complete，目标/有效/上线 6/6/6；现有退订偏好保持不变，没有补发邮件。
Site/Admin/Web 均 healthy，公网及回环健康检查通过，业务状态全部 healthy。

日报 timer 与 wakeup path 恢复 enabled/active；backup timer 维持 disabled/inactive。
服务器仅保留上述最新完整恢复包和部署台账，备份目录约 21 MiB；较早恢复包和历史归档保留在本地。
不改 TLS、provider、支付/SMTP 配置，不重建或补译往期刊物。

同为 schema 12 的代码回退可使用已保存的旧 Compose，但不得因此覆盖生产数据库或回退已发生的付款、退款、权益和投递事实。
