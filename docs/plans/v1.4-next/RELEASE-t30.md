# v1.4.0t30 发布与部署

2026-09-05 22:15 HKT 完成镜像切换，22:16 HKT 业务复核通过。
仅发布测试候选，保留已有 tag；正式 `v1.4.0` 继续等待用户观察。

## 范围

- 用户列表移除内嵌权益记录，改为点击“账户详情”进入用户管理内的独立账号视图，不使用弹窗或新增页面 URL。
- 详情展示邮箱、ID、状态、角色、注册时间、会员计划、剩余天数、到期时间和每日简报状态。
- 权益记录独立展示最近 100 条，包括时间、操作人、原因和变更前后权益；刷新不会叠加记录。
- 返回保留搜索、分页、滚动位置和按钮焦点；支持加载、空记录、错误及重试，旧请求不会覆盖其他账号详情。
- 新增只读且鉴权的 `GET /admin/api/users/detail?user_id=ID`；列表 API 不再加载全部账号的权益历史，账号字段使用白名单输出。
- 用户操作区限定宽度并单列排列，详情表格自动换行；返回按钮不被固定导航遮挡。
- 不修改支付、开通、扣减逻辑或数据库 schema。

## 不可变身份

- 应用提交：`a5ab9522bef1db88827f93bc2c2ffb1b01835b46`；包版本仍为 `1.4.0`。
- [Tag/Release](https://github.com/Yi-Lings/news-digest/releases/tag/v1.4.0t30)：预发布，非 latest。
- [main CI](https://github.com/Yi-Lings/news-digest/actions/runs/33970696753) 和 [tag CI/镜像/Release](https://github.com/Yi-Lings/news-digest/actions/runs/33970696746) 均成功。
- worker/site/admin：`ghcr.io/yi-lings/news-digest-worker@sha256:b1470ad8b2c62d79cf66025df7666ffc787d9d947ef731d6baac88361c7cf647`。
- web：`ghcr.io/yi-lings/news-digest-web@sha256:46054fc45c49776031f1a5b544bba274ca584a2948f6f1c41c2b8ca694a31f7e`。
- 部署包 SHA-256：`6a681451024ac2207b8147cc250920423df4bb7c93ac5a9c1dc89f3bf5186864`，本地与服务器一致。
- 线上 Admin HTML 与本地测试版本 SHA-256 一致：`d998ef7affd60d504c3aeb8aabecaabccf4f5a6821bd7efbdf4529d99ee2178e`。

## 验证

- 针对性回归：130 passed；Windows 全量：1028 passed、1 skipped、7 deselected；Linux tag 全量：1029 passed、7 deselected。
- Ruff、`git diff --check`、源码包和 wheel 构建通过；未执行真实模型、SMTP 或生产支付操作。
- 本地 Playwright 在 320/390/520/640/768/820/1024/1440 宽度通过，无文字越界、JS error 或 document overflow。
- 浏览器调用隔离 Admin API，使用 23 个合成用户，覆盖 0/3/100 条记录、按账号隔离、重复刷新、搜索及第二页返回。
- 延迟旧请求不会覆盖新账号；模拟 503 后可重试；桌面与移动端截图核对通过。
- 公网详情 API 在无凭据时返回 401；未对真实账号执行变更操作。

## 备份与生产核对

冻结 daily/resume/backup 调度并停止 Site/Admin，使用旧镜像生成完整恢复包。
本地 `E:\backups\news-digest\t30\daily-20260905T140919Z-b67ffbd9.tar.gz`
通过 SHA-256 核对和独立恢复验证：2,105 个文件、29 张表。
SHA-256：`71905d0028893194aa621936011ea128d51df16b0e4e409519d81892c2b253b2`。
同一受限目录保存本次 `compose-before.yaml` 和 `upgrade.json`，不覆盖旧恢复资料；含配置密钥及账号数据，不得提交 Git。

本地校验通过后才替换线上四处 image 引用；8 张资金/权益/投递业务表指纹、317 个配置及当前发布文件不变。
schema 保持 12，订单仍为 paid 3 / expired 4，无开放 payment case 或待查单任务。
当前刊期 2026-09-05 complete，目标/有效/上线 6/6/6；现有退订偏好不变，没有补发邮件。
Site/Admin/Web 均 healthy，公网及回环健康检查通过，业务状态全部 healthy。

日报 timer 与 wakeup path 恢复 enabled/active；backup timer 维持 disabled/inactive。
服务器仅保留上述最新完整恢复包和部署台账，备份目录约 21 MiB。
旧 `daily-20260905T132111Z-67a1a11e.tar.gz` 与本地 SHA-256 再次一致后已移出服务器，本地副本仍保留。
不改 TLS、provider、支付/SMTP 配置，不重建或补译往期刊物，不新增每日备份任务。

同为 schema 12 的代码回退可使用本次保存的旧 Compose，但不得覆盖生产数据库或回退已发生的付款、退款、权益和投递事实。
