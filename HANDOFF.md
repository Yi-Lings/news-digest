# Antigravity 交接文档：news-digest v1.4.0

更新时间：2026-09-01（Asia/Hong_Kong）

## 0.1 接回进度（2026-09-01）

本轮已按用户提供的新交接任务接回开发。此前 t15-t18 的 release workflow 均在 Linux
离线测试阶段失败，未生成对应镜像或 GitHub Release；生产最后确认版本为 `v1.4.0t14`。

已复现并修复失败根因：`SiteHandler._render_register(verify_step=True)` 只构造验证码
页面，没有调用 `_html()` 发送 HTTP 响应。t18 将 honeypot 判断提前后，首次注册测试即
命中该分支，客户端等待 10 秒超时，随后同类注册测试连锁超时。修复已加入
`src/news_digest/site_server.py`，不会改变注册/验证码业务逻辑。

本地验证结果：

```text
TestRegistration: 18 passed
offline suite: 883 passed, 1 skipped, 7 deselected
ruff: passed
uv build: passed
PowerShell AST: passed
git diff --check: passed
```

当前修复已准备作为下一不可变 `v1.4.0t20` 候选内容；本次接回期间未运行生产连接、真实
provider、SMTP 或支付。

## 0. 本次暂停点

用户于 2026-09-01 明确要求暂停当前全部工作并交接给 Antigravity。收到暂停指令后，
未连接生产服务器、未部署、未触发抓取/翻译/SMTP/支付、未修改数据库，也未执行新的
浏览器操作。接手方不得把本交接前的生产授权视为仍在执行中的命令；继续生产操作前应先
向用户报告只读核验结果和拟执行范围。

当前可复核的仓库事实：

```text
branch: main
HEAD: a92c411 (tag: v1.4.0t18, annotated, immutable)
origin/main: a92c411
GitHub Release: v1.4.0t7 and earlier published; t15-t18 absent because CI failed
GitHub release workflow: t7 success; t15-t18 failure in offline test job
working tree before the pending fix: clean except untracked .env
source version: 1.4.0
stable v1.4.0 Release: not published
```

`v1.4.0t7` Release 已包含 `digests.env`、`news-digest-deploy.tgz` 和对应 SHA-256
文件。t15-t18 的 tag 已推送但没有可用 Release/digest，不能用于部署。生产状态在本次
接回前没有重新查询；最后确认事实是生产运行 `v1.4.0t14`，不能据此断言当前线上一定
仍是 t14。

本地页面最后一个待辨析项已经从源码确认：主页和归档页的日期选择器采用显式表单提交，
用户选择日期后还必须点击“查看刊期”；`app.js` 只在 `submit` 时调用
`window.location.assign()`。单独切换下拉框而没有跳转属于当前设计，不是 JavaScript 故障。
如果产品要求“选择即跳转”，需要先取得明确需求后再改。

## 1. 当前结论

- 当前分支：`main`；源码 HEAD 为 `v1.4.0t18`，但 t15-t18 的 Linux release workflow 均失败，因此尚无可部署的 t15-t18 Release。生产最后确认版本为 `v1.4.0t14`，当前实际版本待只读核验。
- 当前源码版本：`1.4.0`；稳定 `v1.4.0` Release 尚未授权、尚未发布。
- `v1.4.0t1` 至 `v1.4.0t7` 的 GitHub prerelease、CI/GHCR 已完成；t15-t18 只有不可移动 tag，没有成功 Release。已发布 tag 与镜像不可移动。
- t5 已完成受控生产业务闭环；t6 只收口成功人工投递与自动化刊期汇总之间的状态归并，部署时未调用真实 provider、SMTP 或支付，也未补发历史刊期。
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
- 每日简报只允许有效付费会员为自己的已验证注册邮箱启停；匿名 public double opt-in 的 CSRF、提交与确认端点固定 `404`，旧开关不能重开且不得写库、发确认邮件或激活历史 pending 记录。底层订阅表仍承担投递状态与退订审计，一键退订继续可用。
- schema v10 将已失去 active 付费资格的 `pending/failed/unknown` 投递终结为 `ineligible`，并在 `ineligible_from_status` 保留原状态；续费不补发旧刊，fresh `sending`/`unknown` 仍会阻止刊期完成。

### 3.2 付费权益

- 付费墙关闭时全站放行。
- 付费墙开启时，付费用户可读取全部正文与归档。
- 匿名/免费用户必须在确认页主动确认后，才会消耗当日额度读取最新一期中的一篇主文章；GET、预加载或返回操作均不扣额度，其余当期主文章与归档正文返回付费墙插页。
- 门控响应使用 `no-store`，完整正文不会出现在付费墙响应中。
- 手动延期从现有有效期顺延，不会缩短已有权益。

### 3.3 价格、折扣和订单

- Admin 分别以元设置月付/年付划线基准价与现价，均允许最多两位小数。
- Admin 会把 `9.9` 精确转换为 `990` 分；接口、数据库和订单金额均继续使用整数分。
- 基准价 `36`、现价 `9.9` 时，Admin 自动显示现价占比 `27.5%`，前端绿色标识显示实际优惠 `-72.5%`。
- 订单创建时冻结现价金额，后续调价不改变旧订单；旧库仅有整数折扣键时继续按旧公式读取，不会在升级时改价。
- 正常支付订单由验签回调自动开通，不进入人工审批；旧人工订单接口仅为历史数据兼容保留。
- 用户账户页只查询自己的订单，不受全站订单列表截断影响；读者订单列表仅展示订单编号、会员类型和支付状态/动作。
- 支付创建、金额重分配、失败写回和非成功对账均使用 generation/CAS；旧创建或旧查询不得覆盖新 lease。有效的 30 秒创建 lease 不会被账户自动对账或 Admin 手动对账打断。
- Admin 对账使用远程查询完成时刻执行绝对结算截止门禁；异步回调已先付款时，同步创建响应失败或迟到不会再显示 502 或引导重复付款。
- 旧人工订单审批仅兼容无 `merchant_order_no` 的历史手工订单；EasyPay 自动订单固定拒绝人工批准或驳回。

设置键：

```text
monthly_price_cents
yearly_price_cents
monthly_list_price_cents
yearly_list_price_cents
monthly_discount_percent
yearly_discount_percent
```

### 3.4 自动支付与卡密

- 新生成的未使用卡密在 Admin 持续明文显示，数据库升级前只存摘要的历史卡密无法恢复；卡密支持原子兑换和删除。
- 新卡密同时保存 digest、prefix 与未使用期间的明文，供 Admin 持续显示；使用或删除后不再返回明文。
- 批量生成遇到碰撞会受限重试，不会把唯一约束异常直接暴露给用户。
- Admin 可配置 EasyPay API Base、PID、PKey、支付类型、订单有效期和金额冻结期；PKey 留空保留旧值，页面和 API 只返回是否已保存，不回显明文。
- News 按 `sub2api` 的 API 模式向 `${EPAY_API_BASE}/mapi.php` 服务端 POST，订单号使用 `news_` 前缀，下单成功后直接跳转 adapter 返回的支付页。公开页面与支付重定向的 CSP 只加入经过配置校验的 EasyPay origin，不能包含 path、query 或通配域名。
- adapter 必须将 `${NEWS_SITE_URL}/subscribe/api/payment/easypay` 加入 `EPAY_ADDITIONAL_NOTIFY_URLS`。回调验签并核对 PID、支付类型、订单号、网关交易号和精确金额后，才在单一事务中开通会员；重复回调不重复加时。

### 3.5 Admin 和读者 UI

- Admin 将“用户管理”作为独立顶级工作区，统一承载账号状态、管理员角色、会员计划、剩余天数、到期时间和每日简报操作。
- 用户管理采用服务端邮箱搜索和稳定分页，默认每页 20 条、最大 100 条，不再截断最近 200 个账号或在浏览器内假分页。
- 原“用户与付费”更名为“付费管理”，仅包含自动支付订单、卡密、EasyPay 配置、付费墙、划线基准价和现价；不再与用户表同屏，也不保留独立“订阅管理”工作区。
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
src/news_digest/storage/db.py        schema v10、用户/会话/订单/卡密/免费阅读与投递终态
src/news_digest/preview_server.py    Admin 独立“用户管理”与“付费管理”API/UI
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

浏览器已复核首页桌面与 `390x844` 手机端、会员价格、登录/角色和账户订单；此前候选的 Admin“用户与付费”工作区已在当前未发布源码中拆为独立“用户管理”与“付费管理”，需重新执行桌面/移动端人工验收。Admin 窄屏最后两个页签需要横向滑动，属于非阻断可发现性风险。

生产部署前的 SQLite online backup 与 SHA-256 核验通过；部署后 schema 为 `9`，`integrity_check=ok`。当日正式任务达到翻译 `6/6`、上线 `6/6`，数字质量门误判已消失。真实 EasyPay 订单完成回调、重复通知幂等与会员权益闭环。

当日内容完成时间超过 `08:00 + 6h` 自动追赶窗口，自动投递按设计拒绝迟发。在确认当日没有投递批次且没有 `unknown` 后，仅人工正式投递当日刊物一次，结果为 `sent=1`、`failed=0`、`unknown=0` 且归档成功；没有补发历史刊期。旧的 `complete + DELIVERY_FAILED` 汇总已通过正式数据库状态机归并为 `delivered` 并清除旧错误，未改写收件人投递事实。

t6 对该归并补上持久实现：使用 `BEGIN IMMEDIATE` 与条件更新，只处理已经完成的 `manual`、`retry_failed`、`retry_unknown` 批次。批次仍有 `failed/unknown`、刊期未全部翻译或上线、存在 `sending/unknown`、当前有效付费目标仍待处理时返回 `blocked`；租约未过期的 `delivery_pending` 不会被覆盖，过期认领也只在不存在未决收件人时收口。收件人投递事实持久化后，即使汇总同步异常，也保持发送成功并返回 `state_sync_failed` 与“禁止重发”告警；Admin 人工投递和两类重试均显示后端原始告警，不再用普通成功文案掩盖。

t6 最新定向回归为 `127 passed, 1 skipped`，独立竞态复审未发现 P0/P1/P2；最终离线全量为 `817 passed, 1 skipped, 7 deselected(network)`。全仓 Ruff、`uv build`、Shell 语法、PowerShell AST 与 `git diff --check` 均通过。

t6 GitHub Linux test、worker/web 镜像构建和 release bundle 全绿；Release 为 prerelease，稳定 latest 仍为 `v1.2.19`。生产部署前体检 `18/18` 通过，SQLite online backup 与 SHA-256 校验通过；部署后 Web/Site/Admin 使用 t6 immutable digest 与同一提交，HTTPS/回环健康为 200，schema `9`、`integrity_check=ok`，timer/path active，daily/resume inactive，无残留 worker。生产仍为翻译任务全部成功、当日刊期 `delivered`、无未决投递，账号、会员、订单和订阅聚合未丢失；邮件和支付开关保持启用。部署后未执行真实业务调用。

t7 本地候选新增：独立用户管理的服务端分页；旧匿名订阅入口永久关闭；schema v10 `ineligible` 投递终态；会员资格在每次 SMTP DATA 前按实时 UTC 复检；最后收件人退订、停用或删除后以 `total_count=0` 的 completed run 收口；支付创建/对账 fencing、完成时钟和 paid-before-error 竞态修复；部署前强制核对 worker/web OCI version 与 revision。最终离线全量为 `877 passed, 1 skipped, 7 deselected(network)`；Ruff、`uv build`、Shell 语法、PowerShell AST、Admin JavaScript UTF-8 语法、源码包纯度与 `git diff --check` 全部通过。两轮独立复审未再发现 P0/P1/P2。

## 6. 待完成门禁

1. 提交当前修复并创建全新的不可变 `v1.4.0t20` annotated tag；推送后等待该 tag 的 Linux release workflow 完整成功，并确认 Release 具备 `digests.env`。不得移动 t15-t19 或 t7 标签。
2. 生产部署前先只读核验：worker/web/site/admin 的 OCI version、revision 和 digest，schema version、`integrity_check`、关键业务聚合、systemd timer/path 及公网/回环健康。查询必须脱敏，不输出账号、邮箱、卡密、密钥、邮件正文或完整上游响应。
3. 只有在 t20 CI、镜像 digest 和部署包门禁全部通过后，才使用同一 Release 的 immutable worker/web digest 部署；生产最后确认是 t14。部署前冻结 timer/path/daily/resume、执行 SQLite online backup 和 SHA-256 核验，部署后验证 schema v10、数据计数、三服务和公网 HTTPS。
4. 生产 UI 验收：独立“用户管理”的搜索/分页、授予与撤销管理员、会员延期/清除/剩余天数、每日简报资格；独立“付费管理”的元价小数、折扣、EasyPay 配置和卡密；读者端注册/登录/重置、订阅页、账户订单及归档日期提交。
5. 自动支付只做用户明确授权的单订单闭环；不得创建多笔真实订单，不得补发历史邮件。真实 SMTP、provider、抓取也必须逐项确认范围，部署本身不得触发这些业务操作。
6. 稳定 `v1.4.0` 仍未授权；不得把候选 prerelease 改成稳定 latest。只有候选生产验收完成且用户明确放行，才能另行发布稳定版本。

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

- 生产最后确认版本为 `v1.4.0t14`，当前实际版本待只读核验；t20 是本轮修复后的下一候选，稳定版仍未发布。
- 已推送 tag 永不移动；t20 如发现缺陷，必须使用新提交和新的测试候选 tag，不能覆盖任何旧 tag。
- 候选 GitHub Release 必须标记 prerelease 且不成为 `releases/latest`；一键安装器继续只面向稳定 Latest Release。
- 任一测试、构建、CI、digest、preflight 或线上健康门禁失败都必须停止发布。
- 不补发历史邮件，不在部署过程中运行抓取、翻译、构建或投递；除非生产验收确有必要，不重复真实 provider、SMTP 或扣款请求。
