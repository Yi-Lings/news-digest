# v1.4.0t31 发布与部署

2026-09-11 10:09 HKT 完成镜像切换，10:13 HKT 完成恢复服务验证。
本版本为测试候选，正式 `v1.4.0` 继续等待连续自然刊期观察。

## 范围

- 修复自动化 worker 在存在 pending、retry_wait 或 provider probe 冷却时提前退出的问题。
- 按持久化的最早 retry/probe 到期时间等待，避免 1 秒忙轮询；人工唤醒仍保持快速响应。
- resume 遍历所有未完成刊期；一个刊期的终态人工任务不再阻塞其他刊期。
- 不自动跨 provider 重绑定，不修改 schema、支付、权益、历史刊期或每日备份策略。

## 不可变身份

- 应用提交：`d68c6a6e155f513c99ecc34281ec1520c1b1fdf8`；包版本仍为 `1.4.0`。
- [Tag/Release](https://github.com/Yi-Lings/news-digest/releases/tag/v1.4.0t31)：预发布，非 latest。
- [main CI](https://github.com/Yi-Lings/news-digest/actions/runs/34553007439) 和 [tag CI/镜像/Release](https://github.com/Yi-Lings/news-digest/actions/runs/34553012205) 均成功。
- worker/site/admin：`ghcr.io/yi-lings/news-digest-worker@sha256:a6456fb0beba58d0351bcbb9f1a357ffd2c6855d858f688436842dc661190fcc`。
- web：`ghcr.io/yi-lings/news-digest-web@sha256:8b49d602f14e9ddc26d4bda4aa3f7a7eaf4b535b8a385160a74463f218b7c2b0`。
- 部署包 SHA-256：`a8fdc96c4bbe03dc23550d6ab44525f43e8e18c0b578a99f84ca1e09af58a94c`。

## 验证

- 针对性回归：`39 passed`；Windows 全量：`1029 passed, 1 skipped, 7 deselected`。
- Ruff、`git diff --check`、源码包和 wheel 构建通过；Linux tag CI 通过。
- 新增回归覆盖最早 retry/probe 唤醒时间，以及 resume 遇到多个未完成刊期时继续遍历。
- 升级前恢复包本地 SHA-256 一致，并通过独立恢复验证：2,313 个文件、29 张表。
- 生产恢复服务手动启动后正常退出；旧 `start-limit-hit` 已清除，未再次产生限流状态。

## 生产核对

- Site/Admin/Web 均 healthy；公网 `/healthz` 返回 200。
- 生产镜像已切换至上述不可变 digest；部署脚本核对 8 张业务表和 356 个配置/发布文件指纹未变化。
- `news-digest.timer` 与 `news-digest-wakeup.path` 为 enabled/active；`news-digest-backup.timer` 继续 disabled/inactive。
- 生产 09-11 刊期在部署前已有 `failed + pending` 遗留任务；t31 恢复服务可遍历 09-06 至 09-11 的未完成刊期，但绑定旧 provider 的任务仍按规则等待人工受控重绑定，不静默套用 Terra。
- 该次恢复验证未执行支付、权益修改、邮件补发或历史刊期重建；恢复服务产生的正常 provider 重试属于现有任务恢复流程。

备份：`E:\backups\news-digest\t31\daily-20260911T020750Z-297d4424.tar.gz`；服务器保留最新一份完整恢复包，目录约 21 MiB。
不改 TLS、provider 配置、支付/SMTP 配置，不补译或补发往期刊物。
