# Codex 交接文档：news-digest v1.4.0

更新时间：2026-08-31（Asia/Hong_Kong）

## 1. 当前结论

- 当前分支：`main`，Git 基线与生产环境当前均为测试候选 `v1.4.0t5`。
- 当前源码版本：`1.4.0`；工作区包含尚未提交的投递状态归并修复，当前发布目标是全新测试候选 `v1.4.0t6`，不是稳定 `v1.4.0`。
- `v1.4.0t1` 至 `v1.4.0t5` 的 GitHub prerelease、CI/GHCR 和生产部署已经完成，旧 tag 与镜像不可移动。稳定 `v1.4.0` Release 尚未授权、尚未发布。
- t5 已完成受控生产闭环；t6 只收口成功人工投递与自动化刊期汇总之间的状态归并，不扩展邮件投递范围，也不重复调用真实 provider、SMTP 或支付。
- 不得读取或输出 provider/API key、SMTP 密码、用户密码、验证码、卡密明文、邮件正文或完整上游响应。
- 未跟踪文件 `大创文档.md` 属于用户内容，不得删除或覆盖。

## 2. v1.3.0 已实现能力

- 数字保值质量硬门与 `observe|enforce` 模式。
- 分句器版本和逐段句数冻结，缓存携带 splitter version。
- 失败任务恢复、租约回收、SQLite WAL 和统一 worker maintenance。
- Admin 刊期级重试、任务资格同源和无按钮死路治理。
- provider/API `400/401/403` 作为可恢复上游错误处理，不永久配置锁死。

主要文件：

```text
src/news_digest/translation/quality.py
src/news_digest/translation/schema.py
src/news_digest/translation/service.py
src/news_digest/translation/automation.py
tests/unit/test_quality.py
tests/unit/test_state_machine_governance.py
```

## 3. v1.4.0 已实现能力

### 3.1 账号与会话

- `/register` 按邮箱、两遍密码、随机图形验证码、邮箱验证码两步完成激活，`/login` 仅接受邮箱 + 密码。
- 注册验证码只激活账号，不签发会话；不存在验证码登录入口。
- 忘记密码与邮箱验证码重置密码。
- 注册/登录/验证码按真实客户端 IP 与邮箱双限流。
- 注册 honeypot 与防账号枚举恒定响应。
- 用户会话只在数据库保存 token 摘要。
- 注销使用 POST + CSRF 并撤销当前会话。
- 密码重置撤销该用户全部旧会话。
- 用户启用/停用；停用用户不能登录。
- 运维管理员可授予/撤销 active 站点账号的管理员角色；只有管理员账号在主站显示 Admin 入口，并通过同一数据库会话直接进入后台；停用、撤权或改密后 Admin 会话立即失效。
- 原运维管理员入口始终保留；站点管理员不能修改运维管理员口令。
- 每日简报只允许有效付费会员为自己的已验证注册邮箱启停；不再把匿名 public double opt-in 作为 v1.4 新用户入口。底层订阅表仍承担投递状态与退订审计。

### 3.2 付费权益

- 付费墙关闭时全站放行。
- 付费墙开启时，付费用户可读取全部正文与归档。
- 匿名/免费用户必须在确认页主动确认后，才会消耗当日额度读取最新一期中的一篇主文章；GET、预加载或返回操作均不扣额度，其余当期主文章与归档正文返回付费墙插页。
- 门控响应使用 `no-store`，完整正文不会出现在付费墙响应中。
- 手动延期从现有有效期顺延，不会缩短已有权益。

### 3.3 价格、折扣和订单

- Admin 分别以元设置月付/年付基准价（最多两位小数）与 `0–100%` 折扣。
- Admin 会把 `9.9` 精确转换为 `990` 分；接口、数据库和订单金额均继续使用整数分。
- 前端有折扣时显示划线原价、绿色百分比标识和折后现价。
- 折扣计算向下取整到分。
- 订单创建时冻结折后金额，后续调价不改变旧订单。
- 正常支付订单由验签回调自动开通，不进入人工审批；旧人工订单接口仅为历史数据兼容保留。
- 用户账户页只查询自己的订单，不受全站订单列表截断影响。

设置键：

```text
monthly_price_cents
yearly_price_cents
monthly_discount_percent
yearly_discount_percent
```

### 3.4 自动支付与卡密

- 新生成的未使用卡密在 Admin 持续明文显示，数据库升级前只存摘要的历史卡密无法恢复；卡密支持原子兑换和删除。
- 新卡密同时保存 digest、prefix 与未使用期间的明文，供 Admin 持续显示；使用或删除后不再返回明文。
- 批量生成遇到碰撞会受限重试，不会把唯一约束异常直接暴露给用户。
- Admin 可配置 EasyPay API Base、PID、PKey、支付类型、订单有效期和金额冻结期；PKey 留空保留旧值，页面和 API 只返回是否已保存，不回显明文。
- News 按 `sub2api` 的 API 模式向 `${EPAY_API_BASE}/mapi.php` 服务端 POST，订单号使用 `news_` 前缀，下单成功后跳转 adapter 返回的支付页。
- adapter 必须将 `${NEWS_SITE_URL}/subscribe/api/payment/easypay` 加入 `EPAY_ADDITIONAL_NOTIFY_URLS`。回调验签并核对 PID、支付类型、订单号、网关交易号和精确金额后，才在单一事务中开通会员；重复回调不重复加时。

### 3.5 Admin 和读者 UI

- Admin 新增“用户与付费”工作区，包含用户、自动支付订单、卡密、EasyPay 配置、付费墙、价格和折扣。
- 公共主页导航包含“会员订阅”和“登录 / 账户”。
- 主页底部“按日期浏览”只保留真实刊期选择器；往期归档页在完整列表上方提供同一选择器。
- `/subscribe`、`/login`、`/forgot`、`/reset`、`/account` 使用主站报纸编辑风格。
- 页面有焦点、悬停、轻量入场动效与 `prefers-reduced-motion`；移动端导航为两列，不应横向溢出。
- 公开 site CSP 允许同源样式；HTML/CSS/JS 使用 `no-store`，避免发布切换后新旧资源混用。

### 3.6 部署链

- 独立 `site` 服务默认监听 `127.0.0.1:8620`。
- Admin 默认监听 `127.0.0.1:8619`；旧静态 web 健康服务保留在 `8618`。
- Nginx `/` 指向 site，`/admin/` 指向 Admin。
- Admin 把 Site 所需 SMTP/EasyPay/站点字段投影到独立 `site-config` 目录；site 只读挂载该目录、站点数据卷和独立 site secret，不挂载 `providers.json` 或整个 `/config`。目录 bind 可正确观察原子替换后的新文件。
- Compose、bootstrap/install/preflight、PowerShell 部署脚本均支持 `ND_SITE_PORT`。
- 公开订单提交不写只读 `/config`；通知失败不会把已经入库的订单伪装成 500。

## 4. 主要代码位置

```text
src/news_digest/accounts.py          账号、价格、折扣、卡密和付费墙纯逻辑
src/news_digest/site_server.py       读者站点、账号端点、订单、兑换与访问控制
src/news_digest/storage/db.py        schema v9、用户/会话/订单/卡密/免费阅读数据操作
src/news_digest/preview_server.py    Admin“用户与付费”API 与 UI
src/news_digest/delivery/mailer.py   注册/重置验证码邮件
src/news_digest/delivery/delivery_service.py  投递批次、收件人事实与刊期状态归并
src/news_digest/cli.py               site 子命令和运行配置
deploy/compose.yaml                  site/admin/web/worker 服务边界
deploy/nginx/news.conf               公网反向代理与 CSP
tests/integration/test_site_server.py
tests/unit/test_preview_admin.py
tests/unit/test_release_deploy_artifacts.py
```

## 5. 当前验证证据

已完成的 t5 候选门禁与生产闭环证据：

```text
News 离线全量：799 passed, 1 skipped, 7 deselected(network)
News Ruff、uv build、Shell 语法、PowerShell AST、git diff --check：通过
FastPay adapter：46 passed
FastPay Server（Java 17 / Maven）：18 passed，BUILD SUCCESS
网络级 fake EasyPay E2E：pending -> paid -> duplicate callback 幂等，通过
```

浏览器已复核首页桌面与 `390x844` 手机端、会员价格、登录/角色、账户订单和 Admin“用户与付费”工作区；无横向溢出或新增 console error。Admin 窄屏最后两个页签需要横向滑动，属于非阻断可发现性风险。

生产部署前的 SQLite online backup 与 SHA-256 核验通过；部署后 schema 为 `9`，`integrity_check=ok`。当日正式任务达到翻译 `6/6`、上线 `6/6`，数字质量门误判已消失。真实 EasyPay 订单完成回调、重复通知幂等与会员权益闭环。

当日内容完成时间超过 `08:00 + 6h` 自动追赶窗口，自动投递按设计拒绝迟发。在确认当日没有投递批次且没有 `unknown` 后，仅人工正式投递当日刊物一次，结果为 `sent=1`、`failed=0`、`unknown=0` 且归档成功；没有补发历史刊期。旧的 `complete + DELIVERY_FAILED` 汇总已通过正式数据库状态机归并为 `delivered` 并清除旧错误，未改写收件人投递事实。

t6 对该归并补上持久实现：使用 `BEGIN IMMEDIATE` 与条件更新，只处理已经完成的 `manual`、`retry_failed`、`retry_unknown` 批次。批次仍有 `failed/unknown`、刊期未全部翻译或上线、存在 `sending/unknown`、当前有效付费目标仍待处理时返回 `blocked`；租约未过期的 `delivery_pending` 不会被覆盖，过期认领也只在不存在未决收件人时收口。收件人投递事实持久化后，即使汇总同步异常，也保持发送成功并返回 `state_sync_failed` 与“禁止重发”告警；Admin 人工投递和两类重试均显示后端原始告警，不再用普通成功文案掩盖。

t6 最新定向回归为 `127 passed, 1 skipped`，独立竞态复审未发现 P0/P1/P2；最终离线全量为 `817 passed, 1 skipped, 7 deselected(network)`。全仓 Ruff、`uv build`、Shell 语法、PowerShell AST 与 `git diff --check` 均通过。

## 6. 待完成门禁

1. 显式暂存 News 文件并排除 `.pytest-final-v140t1/`、`大创文档.md` 与其他用户文件；复核 staged diff 后 commit、push，创建全新 annotated `v1.4.0t6` tag。
2. 等待 GitHub release workflow 全绿，确认 t6 是 prerelease、不成为 `releases/latest`，并从同一 Release 取得 worker/web immutable digest。
3. 冻结 News systemd 入口并复核 production online backup，然后用 `server-push.ps1` 部署 t6 digest；不得移动任何旧 tag，也不得用稳定版 `deploy-all.ps1` 隐式发布候选。
4. 部署后只读核对版本/digest、health、schema/integrity、systemd 和当日刊期/投递审计；不得再次调用真实 provider、SMTP 或支付，不重试 `unknown`，不补发历史刊期。
5. 用户人工验收：用已注册账号登录；确认顶部显示“管理后台”；进入 `/admin/` 不要求独立运维密码；账户页显示月刊会员、有效期和已支付订单；确认当日邮件实际到达。文档和日志不得记录账号、订单号或邮件正文。

## 7. 最终本地门禁命令

```powershell
uv run ruff check .
uv run pytest -q -p no:cacheprovider
uv build
bash -n deploy/bootstrap.sh
bash -n deploy/install.sh
bash -n deploy/preflight.sh
git diff --check

$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path 'deploy/server-push.ps1'), [ref]$null, [ref]$errors
) | Out-Null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path 'deploy/deploy-all.ps1'), [ref]$null, [ref]$errors
) | Out-Null
$errors
```

## 8. 发布纪律

- 生产环境当前为 `v1.4.0t5`；在 t6 实际部署完成前不得描述为已上线。
- 已推送 tag 永不移动；当前候选必须使用全新 `v1.4.0t6`，稳定版才使用 `v1.4.0`。
- 候选 GitHub Release 必须标记 prerelease 且不成为 `releases/latest`；一键安装器继续只面向稳定 Latest Release。
- 任一测试、构建、CI、digest、preflight 或线上健康门禁失败都必须停止发布。
- 不补发历史邮件，不在部署过程中运行抓取、翻译、构建或投递；t5 已完成的真实闭环不在 t6 重复执行。
