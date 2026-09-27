# v1.4.0t36：投递失败空转修复与生产验收

日期：2026-09-27。状态：**测试候选已发布并部署**。代码见 [PR #6](https://github.com/Yi-Lings/news-digest/pull/6)，[Release](https://github.com/Yi-Lings/news-digest/releases/tag/v1.4.0t36) 与 [发布流水线](https://github.com/Yi-Lings/news-digest/actions/runs/36312510513)。本记录中的服务器和证书事实只属于本次实例部署；开源部署参数仍由部署者提供 `ND_DOMAIN`，不设生产域名默认值。

## 故障与修复

t35 上线后，某刊期已是 `complete + DELIVERY_FAILED`，30 秒 fallback 却持续认为有可执行投递。超过自动补发窗口时，worker 会反复启动、尝试失败并退出 10。t36 将 `automation-due` 与实际投递认领统一为首次待投递的 `complete` 状态；失败、已有认领或未知错误码不再自动重试。Admin“重试失败”和 `send-email --resend --yes` 仍通过独立的人工路径，仅选择 `failed` 收件人；`sent` 和 `unknown` 的防重边界保持不变。翻译任务到期和过期租约维护仍可唤醒 worker。

## 验收与回滚依据

- Windows 完整离线测试 1070 通过、25 跳过、7 项公网 RSS 用例按计划未纳入稳定门禁；Ruff 通过。Linux CI 测试、PowerShell 解析、worker/web 镜像构建和 Release 包均通过。
- 在 t35 升级前生产数据库的隔离副本上迁移到 schema 14 并模拟连续 1 小时的 30 秒触发，到期空 worker 数为 0，数据库完整性为 `ok`。
- 生产部署使用 Release 中固定 digest：worker `sha256:7fbc2c254dbea33dec3e966d90043f65027ca787e13d0ea0bffcdc6b8b7e5029`，web `sha256:f7030c51693059a23c4f00c80fa483688676a69e6bae521eba702ed97bbfe6f6`。升级前 schema 14 备份已保存于服务器 `backups/`，并转存本地 `E:\backups\news-digest\t36`；SHA-256 核对通过。上一版 t35 digest、Compose 文件与 timer 状态记录仍保留。
- 部署后 Admin 的 `automation-due` 返回 `AUTOMATION_IDLE`；fallback timer、文件通知和每日 timer 均为 active。连续两次 30 秒触发均由 `ExecCondition` 跳过，Docker worker 启动事件为 0。
- 本机 SNI 与公网 HTTPS 的 `/`、`/healthz`、`/admin/` 均返回 200 且证书验证通过。当前证书 SAN 覆盖部署域名，有效期至 2026-12-24 00:28:03 UTC；Nginx 配置检查通过。

当日 `DELIVERY_FAILED` 状态保留供排查，不自动补发。历史旧 Provider 的超期探测、首次旧刊超窗的投递分类及公网 TLS 门禁等后续范围见 [t37 计划](PLAN-t37.md)。
