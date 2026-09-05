# v1.4.0t27 发布记录

日期：2026-09-05。状态：已部署，GitHub prerelease，不是稳定 `v1.4.0`。

- 应用提交：`94543e392537d2f1f8b8babbcd7e16a4ee438d9d`。
- Release：https://github.com/Yi-Lings/news-digest/releases/tag/v1.4.0t27
- Linux CI：https://github.com/Yi-Lings/news-digest/actions/runs/33959236306
- worker/site/admin：`sha256:ac85263777be4293bca652cce9fced143d9e1c161f3ed8e435151d4f2aebe2b7`。
- web：`sha256:e5124f9dfb61a60e677b0388e62cce3b954ce44892652254f9fac324c59104f5`。

## 交付

自动单句修复只处理可定位的空/非字符串失败槽位。所有 provider 请求共享 3 次/默认
600 秒预算，数字信号不阻断。固定刊期成员、完整句子结果、active task、事务提交、
发布 revision 对账和中断恢复已接入。零目标邮件显示 skipped，不伪报已发送。

按用户最新要求，不处理往期刊物。线上只恢复当前 2026-09-05 刊期的 6 篇句级结果，
不调用模型，不重建页面，不补发邮件。往期仅保留兼容与页面保护。

## 验证

- Windows 全量：`989 passed, 1 skipped, 7 deselected`；随后只处理当前刊期的定向回归 `38 passed`。
- 最终应用提交 Linux CI：`991 passed, 7 deselected`，Ruff 通过；源码包与 wheel 构建通过。
- 一致性副本迁移通过。14 张账号、订单、权益、投递及相关业务表迁移前后指纹一致。
- 生产升级前冻结 timer/wakeup path，暂停 Site/Admin，确认无 worker；迁移前备份及 SHA-256
  位于 `/srv/news-digest/backups/t27-20260905-095831/`。备份 schema 10，生产 schema 11。
- 生产迁移后与该备份逐表核对业务事实一致，current 与 manifest 哈希不变，运行配置不变。
- Site/Admin/Web 回环检查均为 200，公网首页 200；timer/wakeup path active，daily/resume inactive。

## 部署说明

本次采用保持 Nginx/TLS 不变的应用原位升级，使用同一 Release 的 immutable digest，
升级镜像及两个 systemd service；未重新签发证书，未更改 provider 或 SMTP 配置。

首次离线迁移在只读根文件系统上因 SQLite 临时文件报 `disk I/O error`，调度及写入服务
保持停止。提供 `--tmpfs /tmp:size=64m,mode=1777` 后幂等续跑成功，没有恢复旧库或重发。
此部署前提已补进 main 的 bootstrap 与回归测试，不需要重建应用镜像或移动 t27 tag。
**t27 原始附件中的 bootstrap 缺少该参数，再部署时使用 main 修正后的部署脚本并仍传入
t27 的两条 Release digest，或在离线迁移 docker run 中显式增加该 tmpfs。**

资金对账、退款/争议记录、配置 revision、日常备份和业务健康等后续架构增量仍未完成。
