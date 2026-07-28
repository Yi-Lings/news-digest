# 阶段 7A 主开发会话交接文档

> 交接时间：2026-07-27
>
> 适用项目：`E:\new\news-digest`
>
> 原主会话：`Local development setup`
>
> 交接原因：原会话上下文过长，已出现 `Your input exceeds the context window` 和输出超过 64,000 tokens，无法可靠继续。

## 1. 新会话必须先遵守的边界

1. `PLAN.md` 的“阶段 7A：正式版前置整改唯一清单”是唯一需求与验收依据，当前位于约第 481–706 行。开始前必须重新读取完整阶段 7A，不能只依赖本交接摘要。
2. 只继续阶段 7A 最新改动及其直接影响范围，不重新进行已通过的全项目全面审查。
3. 禁止自动部署、发布、推送，禁止创建、移动、覆盖或删除标签；禁止运行 `deploy.bat`、`deploy-all.ps1` 或任何服务器写操作。
4. 禁止自行调用真实翻译 API、连接真实 SMTP 或发送真实邮件。所有开发测试使用 mock/fake；实际测试必须由用户在 Admin 中明确点击或另行授权。
5. 最终部署只能由用户手动执行。
6. 已发布的 `v1.0.0` 标签指向 `806a4f7`，早于阶段 7A，不包含本轮能力。不得移动该标签，也不得部署它作为阶段 7A 正式版本。最终版本号由用户决定。
7. 当前工作树包含大量未提交成果。严禁 `git reset --hard`、`git clean`、checkout 覆盖、丢弃或还原现有 dirty diff；也不要覆盖审查会话刚更新的 `PLAN.md`。
8. 工作区内 `.env.local` 含真实配置。不得读取、打印、复制到日志/交接或测试产物；测试不得访问真实服务。

## 2. 当前 Git/工作区快照

```text
branch: phase/7-deploy
HEAD:   806a4f7 release: v1.0.0 —— 首个正式版定稿
staged: 无
modified tracked files: 27
untracked new files:    12（11 个阶段 7A 代码/测试 + 本交接文档）
tracked diff: 约 +6890 / -1001
untracked code/tests: 约 4029 行
```

当前 HEAD 和已有 `v1.0.0` 不含阶段 7A；所有阶段 7A 成果仍在工作树中，尚未形成提交。

### 已修改文件

```text
PLAN.md
deploy.bat
deploy/compose.yaml
deploy/deploy-all.ps1
deploy/nginx/news.conf
deploy/server-push.ps1
src/news_digest/cli.py
src/news_digest/config.py
src/news_digest/delivery/mailer.py
src/news_digest/delivery/publisher.py
src/news_digest/pipeline.py
src/news_digest/preview_server.py
src/news_digest/rendering/email.py
src/news_digest/static/app.js
src/news_digest/static/style.css
src/news_digest/storage/db.py
src/news_digest/templates/email.html
src/news_digest/templates/email.txt
src/news_digest/templates/home.html
src/news_digest/translation/client.py
src/news_digest/translation/service.py
tests/integration/test_run_pipeline.py
tests/unit/test_cli.py
tests/unit/test_email.py
tests/unit/test_preview_admin.py
tests/unit/test_translation.py
tests/unit/test_translation_client.py
```

### 未跟踪但必须保留的新文件

```text
src/news_digest/admin_email.py
src/news_digest/admin_providers.py
src/news_digest/config_io.py
src/news_digest/delivery/delivery_service.py
src/news_digest/delivery/email_content.py
src/news_digest/delivery/subscriptions.py
tests/unit/test_admin_email.py
tests/unit/test_delivery_service.py
tests/unit/test_email_content.py
tests/unit/test_smtp_delivery.py
tests/unit/test_subscriptions.py
```

注意：`deploy.bat`、`deploy/deploy-all.ps1`、`deploy/server-push.ps1` 中还混有阶段 7A 之前的本地部署器自动化修复。不要假设全部 dirty diff 都属于阶段 7A，也不要顺手部署或推送。

## 3. 用户最新 P0 决定

以下全部是正式版前置 P0，不得降回后续优化：

### 模型/API

- Admin 每个供应商档案只有一个“测试连接”按钮。
- 点击后自动发送固定 `Hi`，恰好一次最小真实生成请求；不提供任意消息输入框。
- 测试使用正式翻译同一个 adapter，不能另写 `/models`、TCP 探测或简化请求路径。
- OpenAI Chat 和 Anthropic Messages 两个原生 adapter 都要支持。
- 选择协议后按 ccswitch 模式自动、原子切换整套：最终 URL/path、鉴权 header、版本 header、payload、system 位置、stream/non-stream parser；不能只换模型名。
- OpenAI 使用 Bearer、Chat Completions 形态；Anthropic 使用 `x-api-key`、`anthropic-version`、Messages 形态。双向切换后不得残留旧协议 header/path/payload/parser/cache。

### 邮件内容

Admin 必须支持并持久化组合：

- 主文条数；
- 简讯条数；
- 双语、仅中文、仅英文；
- 当前数据模型真实存在的栏目/来源和结构化主题筛选；
- 短/标准/长摘要；
- “摘要导读”和“紧凑列表”两种版式。

预览、测试邮件、08:00 自动投递、指定刊期人工发送必须复用同一个已保存组合和同一内容/MIME builder。

### 公开订阅与退订

- 公开订阅表单；
- 防枚举、CSRF/Origin、限流和轻量反自动化；
- double opt-in：`pending → active`；
- 高熵、限时、单次 token，数据库只存摘要；
- HTML/纯文本退订链接；
- `List-Unsubscribe` 与 RFC 8058 one-click；
- `active → unsubscribed` 后所有自动、失败重试和人工重发都排除；
- 重新订阅必须重新确认；
- Admin 只显示脱敏状态，不得直接复活公开退订记录。

### 明确不做的非目标

用户确认不需要：档案复制/拖拽排序、持续测活、健康评分/自动 failover、动态模型列表、复杂能力探测、价格同步/精确账单、多 SMTP 档案、任意发送时间、更多模板/WYSIWYG、订阅偏好中心/分组营销、强制向全部已成功者批量重发。不要为这些功能增加实现或预先抽象。

## 4. 原主会话最后在做什么

原会话先完成多协议 translation adapter，再完成 SMTP/逐收件人状态机，随后搭出了不可变 release manifest、08:00 第四阶段、Admin 四区、邮件内容组合和公开订阅/退订的主体实现/初版接线；这些能力仍有第 7 节的明确 P0 缺口，不能视为完成。最后正在：

1. 检查 Admin SMTP 保存边界；
2. 修正“邮件关闭时允许保存不完整 SMTP，重新开启时严格校验”；
3. 让 SMTP 连接测试与测试邮件共用互斥锁；
4. 准备把工作区复制到 `/tmp` 跑原生 Linux 文件权限语义下的 Ruff + 全离线 pytest。

验证过程没有完成。会话在复制虚拟环境和输出大量内容时先遇到 64K 输出上限，随后上下文窗口超限。最后两次用户让它重新读取 PLAN 时，均未形成可靠继续执行结果。

## 5. 已实现的主体（尚未最终验收）

### 5.1 OpenAI/Anthropic 双 adapter

核心文件：

```text
src/news_digest/translation/client.py
src/news_digest/translation/service.py
src/news_digest/config.py
src/news_digest/admin_providers.py
src/news_digest/config_io.py
src/news_digest/preview_server.py
```

已见实现：

- 显式 `openai_chat` / `anthropic_messages`；
- OpenAI `/v1/chat/completions`、Bearer、messages、OpenAI stream/non-stream parser；
- Anthropic `/v1/messages`、`x-api-key`、`anthropic-version`、顶层 system、Anthropic stream/non-stream parser；
- `follow_redirects=False`；
- Base URL HTTPS、userinfo/query/fragment/endpoint/重复 `/v1` 校验；
- 固定 `Hi` 的 `probe()`，8 tokens，不接受任意消息；
- 上游错误分类和秘密脱敏；
- 正式翻译有限重试，流开始后不整次重试；
- 缓存 identity 包含 api_type、规范化 Base URL、模型，不含 key；
- provider 多档案、enabled、唯一默认、最近测试指纹和过期状态；
- Admin CSRF/Origin/Content-Type/限流及测试目标公网校验；
- 前端没有任意模型消息 textarea；现有 textarea 是 SMTP 收件人行列表。

### 5.2 SMTP 和逐收件人投递

核心文件：

```text
src/news_digest/admin_email.py
src/news_digest/config_io.py
src/news_digest/config.py
src/news_digest/delivery/mailer.py
src/news_digest/delivery/delivery_service.py
src/news_digest/storage/db.py
src/news_digest/cli.py
```

已见实现：

- `EMAIL_DELIVERY_ENABLED`；
- `SMTP_SECURITY=implicit_tls|starttls`；
- 旧 `SMTP_USE_TLS` 迁移逻辑；
- 端口 1–65535、账号/密码成对、邮箱/CRLF/去重校验；
- 密码只返回 `password_set`，留空保留、显式清除；
- `.env` 原子写、锁、fsync、0600；
- SMTP 连接测试仅 EHLO/TLS/login/NOOP，不发送；
- TLS 证书链和主机名校验；
- 逐收件人独立消息，地址互不可见；
- 检查 `send_message()` 拒收结果；
- `sent/failed/unknown`，DATA 阶段断连保守 unknown；
- 测试邮件 `[测试]`，不污染正式投递状态；
- DB schema v3，逐收件人 claim 和 delivery run；
- `pending → sending → sent/failed/unknown`；
- interrupted sending 恢复为 unknown；
- failed-only retry，unknown 须风险确认；
- EML 归档与 SMTP 状态分离。

### 5.3 不可变发布 manifest 与 08:00 第四阶段

核心文件：

```text
src/news_digest/delivery/publisher.py
src/news_digest/pipeline.py
src/news_digest/delivery/delivery_service.py
src/news_digest/cli.py
```

已见实现：

- 构建写入并校验 `release.json`；
- manifest 自包含 edition、release identity 和 SHA-256；
- 预览/自动/人工发送只读 manifest，不查询数据库最新一期；
- 刊期和文章页面一致性检查；
- `run` 已变为抓取→翻译→构建→投递四阶段；
- 构建失败不发；邮件失败不回滚已发布站点，但返回非零；
- 禁用邮件明确跳过；
- 自动补跑窗口、manual/retry/test 模式已有实现。

### 5.4 邮件内容组合和 MIME

核心文件：

```text
src/news_digest/delivery/email_content.py
src/news_digest/rendering/email.py
src/news_digest/templates/email.html
src/news_digest/templates/email.txt
src/news_digest/delivery/delivery_service.py
```

已见实现：

- 主文/简讯开关及数量；
- 双语/中文/英文；
- 来源筛选；
- 没有结构化主题时不伪造主题；
- 短/标准/长摘要；
- 摘要导读/紧凑列表；
- 保留刊物排序，不重新调用模型；
- 缺译降级；
- 不可变刊期绝对链接；
- UTF-8 `multipart/alternative`；
- HTML/纯文本；
- 测试主题 `[测试]`；
- List-Unsubscribe headers。

### 5.5 公开订阅和一键退订

核心文件：

```text
src/news_digest/delivery/subscriptions.py
src/news_digest/storage/db.py
src/news_digest/preview_server.py
src/news_digest/templates/home.html
src/news_digest/static/app.js
src/news_digest/static/style.css
deploy/nginx/news.conf
deploy/compose.yaml
```

已见实现：

- 首页公开订阅表单；
- public CSRF、Origin、JSON、蜜罐和限流；
- 统一响应防枚举；
- pending + 24h 高熵确认 token；
- DB 只存 token SHA-256；
- pending→active；
- GET 退订仅确认页面，POST one-click 幂等退订；
- active→unsubscribed；
- 重新订阅重新确认；
- HTML/纯文本及 List-Unsubscribe；
- Admin 脱敏列表，禁止直接复活 public 退订记录；
- nginx public route 和独立限流；
- Admin 共享 news-data SQLite。

### 5.6 Admin 四区

`preview_server.py` 已大幅扩展，设计为：

1. 模型接口；
2. 邮件设置与内容组合；
3. 订阅管理；
4. 投递状态。

后端代码路径已接 SMTP 当前表单连接测试、保存、预览、测试邮件、订阅脱敏列表、failed/unknown 重试和指定刊期预览确认，但生产 Admin 尚未挂载 `news-site:/site:ro`，因此依赖 release manifest 的邮件预览/投递状态/指定刊期功能在生产容器中实际不可用。必须继续针对性收口，不能因页面已出现控件就判定完成。

## 6. 测试现状：不要误报“全绿”

原主会话曾在较早快照中记录“代理报告全量离线 307 项通过”。之后又修改了 Admin SMTP 保存逻辑、互斥锁和测试，因此该结果不是当前最终工作树的验收证据。

当前仓库共有约 227 个顶层 `test_` 函数，另有大量参数化 case；相关测试文件已经大量补齐。但最后一次 Ruff + 全离线 pytest 没有完成，也没有留下可核验的 pytest log/JUnit 结果。

准确状态：

```text
- 测试代码大量存在；
- tracked diff 的 git diff --check 当前通过（仅 deploy.bat 行尾提示；它不检查未跟踪文件）；
- 当前最新未提交工作树没有可信的完整 pytest/Ruff 全绿记录；
- 阶段 7A 不能判定通过。
```

新会话应先做定向测试，再做默认排除 network 的全套测试。不要运行 `tests/network`，不要读取 `.env.local`。pytest marker 不能证明测试绝无未标记 socket 访问；验证应在禁出网环境，并优先使用现有 Python 3.13 虚拟环境的解释器或 uv 离线模式，避免依赖解析自行联网。

建议命令（在项目的 Python 3.13 环境）：

```powershell
uv run ruff check .
uv run pytest tests/unit/test_translation_client.py tests/unit/test_translation.py tests/unit/test_preview_admin.py -q
uv run pytest tests/unit/test_admin_email.py tests/unit/test_smtp_delivery.py tests/unit/test_delivery_service.py tests/unit/test_email_content.py tests/unit/test_subscriptions.py -q
uv run pytest tests/unit/test_cli.py tests/unit/test_email.py tests/integration/test_run_pipeline.py -q
uv run pytest
```

`pyproject.toml` 默认 `addopts` 已排除 `network`。若在 Linux sandbox 测 0600 语义，复制代码时必须排除 `.env.local`、`.venv`、`.git`、缓存和生成数据；不要复制真实凭据。

## 7. 尚未收口的 P0 阻塞项

以下来自对当前工作树的只读交叉检查。新会话应先验证，确认后逐项修复；不能直接假设代理结论等同最终裁决。

### 7.1 模型/provider 一致性与测试体验

1. **已确认**：编辑当前默认档案后，provider 文件更新但运行时 `.env` 不同步；页面显示新协议，worker 仍读旧配置。
2. **已确认**：每日翻译直接读 `.env`，没有把 provider 的唯一默认状态作为权威源；`.env` 残留会绕过“无默认明确失败”。
3. **已确认**：provider 公网/SSRF 校验主要在 probe；保存、设默认和正式翻译路径未统一强制。
4. probe 后端返回了分阶段错误详情，但前端非 2xx 可能只显示 `HTTP 502`，丢失 category/status/protocol/model/elapsed。
5. 修改未保存表单后旧成功结果未立即标为过期；列表最近测试缺时间和分阶段详情。
6. 点击测试可能缺少集中确认框：一次真实 `Hi`、输入/输出上限、可能计费、是否未保存。
7. `httpx.Timeout` 是阶段超时，不等于独立硬总 deadline；TLS 错误分类和总时限测试可能不足。
8. 缺 adapter→正式新闻 schema→cache 的直接离线端到端测试。
9. **DNS 重绑定/代理绕过尚未可靠收敛**：当前先解析域名检查公网，随后 `httpx` 再按域名解析连接，未固定或复核已验证 IP；正式客户端也需明确禁用不可信环境代理。保存、probe、正式翻译要使用一致策略并有测试。

### 7.2 SMTP/自动投递与配置迁移

1. `.env.example`、`deploy/bootstrap.sh`、`deploy/README.md`、`docs/OPERATIONS.md` 仍以旧 `SMTP_USE_TLS` 为主，缺阶段 7A 新键。
2. Admin 保存新 `SMTP_SECURITY` 时，旧 `SMTP_USE_TLS` 可能仍留在 `.env`，worker 启动后形成冲突。
3. `deploy-all.ps1` 没管理 `EMAIL_DELIVERY_ENABLED`、`SMTP_SECURITY`、内容组合、补跑窗口等；还可能覆盖 Admin 服务器配置。必须明确配置所有权。
4. **已确认**：`just_built_release_name` 绕过 08:00 补跑窗口；人工在白天/夜间运行 `run --yes`，只要自动邮件已开启，也可能立即真实投递。
5. 即使邮件关闭，代码可能先完整解析 SMTP，旧字段冲突/非法端口仍阻塞站点任务，而不是无条件跳过。
6. SMTP 部分/全部失败时，run-level `error_category` 可能为空，Admin 状态摘要不完整。
7. SMTP 使用统一 socket timeout，但未形成连接/EHLO/TLS/auth/DATA/硬总时限的明确模型。
8. systemd/Compose/Dockerfile 的第四阶段和退出码传播缺直接离线集成断言；timer 固定时区与 `NEWS_TIMEZONE` 可能分叉。

### 7.3 内容组合、预览和状态页

1. 保存/开启配置时可能未用当前 release 验证筛选后是否为空，例如数量都为 0 或来源筛选无结果。
2. Admin 没有随着表单变化即时显示预计主文数、简讯数和降级结果。
3. 测试邮件可能使用未保存的内容组合；PLAN 要求测试/预览/自动/人工使用同一已保存组合。
4. Admin 预览复用 selector/renderer，但可能没有真正经过最终 MIME builder。
5. 人工预览指纹未绑定最终正文或内容配置；预览后改配置，旧确认 token 可能仍有效。
6. **已确认**：CLI `preview-email` 没有加载保存的 `.env` 内容组合。
7. **已确认的生产挂载阻塞**：Admin Compose 仅挂 `/config` 和 `news-data:/data`，没有 `news-site:/site:ro`；镜像内虽有空 `/site`，但无实际 release/current。生产邮件设置、内容预览、投递状态和指定刊期功能会报发布目录不可用。必须补只读站点卷挂载及直接测试。
8. 投递状态 UI 尚未完整显示 run ID、起止时间、总人数、degraded、run error、时区/08:00/下一次时间；预览人数和实际待发人数可能不一致。

### 7.4 公开订阅/退订

1. nginx 默认 access log 会记录路径中的确认/退订明文 token；相应 location 应关闭或脱敏日志。
2. **已确认**：正式 EML 归档保存第一位收件人的完整实际消息，包括完整 To 和明文退订 token。必须改成无私有 token 的脱敏归档，或实现严格受保护且符合 PLAN 的替代策略。
3. 缺明确 `PUBLIC_SUBSCRIPTION_ENABLED`/生产就绪门；生产域名、HTTPS、SMTP From、隐私说明和退订入口未就绪时，表单不应开放。
4. 当前缺隐私说明页面/入口。
5. 收件人查询后、真正 SMTP DATA 前发生退订，发送路径没有最后二次检查 active，仍会多发一封；应在本地可控的最后发送点复核订阅状态。
6. 生产反代下应用层限流键可能只看到 `127.0.0.1`，使所有公开用户共享每分钟额度；需采用受信代理传递且严格校验的真实客户端地址，或明确只依赖 nginx 逐 IP 限流，并补反代场景测试。
7. 订阅 SMTP 公网校验也存在“预解析后实际连接再次解析”的 DNS rebinding 风险，需与正式 SMTP 路径一起收敛。
8. 需要补 token 日志、归档、并发退订、启用门、隐私就绪门等直接测试。

### 7.5 文档和部署工件

当前明显未完成：

```text
.env.example
README.md
deploy/bootstrap.sh
deploy/README.md
docs/OPERATIONS.md
deploy-all 受管键和配置所有权说明
Dockerfile.worker 注释
```

现有文档仍有 06:30、旧 `SMTP_USE_TLS`、“run 不发邮件/邮件编排待定”等失实内容，且缺公开订阅、隐私、退订、unknown、内容组合、模型协议切换和 Admin 状态流程。

## 8. 推荐的新会话执行顺序

### 第 1 步：安全接管

1. 读取 `PLAN.md` 阶段 7A 全文和本交接文档。
2. 运行 `git status --short`、`git diff --stat`，确认约 27 个修改和 12 个未跟踪文件仍在；注意 `git diff --stat` 不包含未跟踪文件，必须单独核对上文 11 个新代码/测试文件和本交接文档。
3. 不 reset/clean/stash，不覆盖 PLAN，不触碰真实配置。复制到 `/tmp` 时不得用 `git archive`、`git checkout-index` 或新 checkout（会漏掉未跟踪核心文件）；应按文件系统复制并显式排除 `.env.local`、`.git`、`.venv`、缓存和生成数据，复制后复核未跟踪清单。
4. 先运行 tracked `git diff --check`，并对 11 个未跟踪代码/测试文件执行 Ruff/语法或定向测试，确定当前代码至少可导入/收集。

### 第 2 步：先修跨层正确性

优先顺序建议：

1. 默认 provider 与 `.env`/每日翻译权威一致性；正式路径 SSRF；probe 失败和 stale UI；
2. token 日志/EML 归档泄漏；公开订阅启用和隐私就绪门；退订并发窗口；
3. SMTP 旧字段迁移与关闭模式；自动发送窗口；run error category；
4. 内容组合保存时的当前 release 校验、已保存组合一致性、MIME 预览与确认指纹；
5. Admin 完整状态展示和下一次 08:00 信息。

### 第 3 步：补部署配置和文档

同步 `.env.example`、bootstrap、deploy-all、README、deploy README、OPERATIONS、Dockerfile 注释，并加关键配置一致性测试。注意：只是修改和离线验证，禁止实际部署。

### 第 4 步：针对性验证

1. 模型 adapter/provider/Admin tests；
2. SMTP/状态机/内容/订阅 tests；
3. CLI/manifest/integration tests；
4. Ruff；
5. 默认全离线 pytest。

保存简洁可核验的结果：命令、测试数、失败数、是否排除 network。不要贴大段完整输出到对话，以免再次耗尽上下文。

### 第 5 步：交给审查会话

完成后只报告：

- 基线 `806a4f7` 到当前的最新 diff 概要；
- 阶段 7A A–I 逐项完成映射；
- 定向/全离线测试结果；
- 尚需用户授权的真实 `Hi`、SMTP 连接和测试邮件；
- 服务器脱敏证据状态；
- 明确说明未部署、未发布、未推送、未操作 tag。

审查会话将只复审本轮最新改动和直接影响范围，不再做全项目审查。

## 9. 已取得的服务器脱敏证据

PLAN 7A A 节的只读取证已完成，结果为：

- 2026-07-27 08:00 timer 已按上海时区触发；
- 旧 service 成功结束，journal 只有抓取、翻译、构建三个阶段；
- 服务器实际 service 命令未包含投递阶段，也没有发送尝试；
- SMTP 六项字段为 `SET`，但没有新的投递开关/安全模式键；
- 全程只读、脱敏，未部署、未改服务，未输出秘密或完整收件地址。

## 10. 新会话可直接使用的开场指令

```text
请接管 E:\new\news-digest 的阶段 7A 开发。先只读完整阅读：
1. PLAN.md 的“阶段 7A：正式版前置整改唯一清单”（唯一需求与验收依据）；
2. docs/phase-7a-main-session-handoff.md；
3. git status 和当前未提交 diff。

原会话因上下文窗口超限中断。当前基线 HEAD 为 806a4f7，工作树约有 27 个已修改文件和 12 个未跟踪文件（11 个阶段 7A 新代码/测试 + 交接文档），全部必须保留。严禁 reset/clean/checkout 覆盖、丢弃 dirty diff或覆盖 PLAN。`git diff` 不显示未跟踪文件；禁止用 git archive/checkout-index/新 checkout 复制验证目录，以免漏掉核心新文件。

继续范围仅限阶段 7A 最新改动及直接影响范围。优先核实并修复交接文档第 7 节的 P0 阻塞，再补部署配置/文档和离线测试。不要做全项目复审。

严格禁止：部署、发布、推送、服务器写操作、创建/移动/覆盖/删除标签、真实 API 调用、真实 SMTP 连接或真实邮件。默认测试只用 mock/fake，最终部署由用户手动执行。已有 v1.0.0 标签不可移动且当前不得部署。

每完成一个小块就简洁记录文件和测试结果，避免输出大段文件或日志再次耗尽上下文。全部完成后停下，提交给独立审查会话做阶段 7A 针对性复审。
```

## 11. 当前裁决

> 2026-07-28 接管完成更新：第 7 节是接管前审查快照，其中列出的代码、配置迁移、
> 部署工件和文档阻塞均已逐项核实并修复；历史描述保留用于追溯，不再代表当前工作树。

**当前裁决：代码整改、受影响范围定向回归、A 节服务器脱敏取证、Admin UI P0、本地
preview wiring、SMTP unknown 状态机与持久幂等收口完成；8618 preview 已用离线 build
`2026-07-26-12` 和最新代码重启，暂停等待用户人工验收。当前不得运行最终全量、部署、发布
或打标签。**旧全量 `433 passed, 1 skipped, 7 deselected(network)` 早于最新修复，仅作历史参考。
最新受影响结果：SMTP/Delivery/Admin `150 passed, 1 skipped`；首页渲染/链接 `14 passed`；
preview env/import `2 passed`；持久状态/权限/runtime hygiene `9 passed`。Ruff 与 `git diff --check`
通过；最终全量尚未运行，必须在用户明确确认人工测试通过后唯一执行。

Admin 已复用主站冷纸白/墨黑/朱砂红和编辑部排版，并通过 360px、768px、1440px
真实浏览器长内容、busy 恢复与 reduced-motion 验收。本地 `news-digest preview` 已显式接入
database、site URL、output root 和 timezone；legacy current 无 manifest 时邮件设置和 SMTP
连接测试仍可用，内容预览/测试邮件明确报告 release 错误，订阅名单和 fake SMTP HTTP 回归不再返回未接线。
SMTP 的 TLS/EHLO/STARTTLS/auth/NOOP/MAIL/RCPT/DATA/QUIT 共用硬总时限；354 前失败为 failed，
正文或最终响应不确定为 unknown，最终 2xx 后 deadline/QUIT 异常保持 sent。test-message 使用
独立脱敏持久表按 key hash 和请求指纹防重，unresolved running/unknown 在 preview 重启后仍阻止
同 key或新 key 重发。生产 Admin 以 root UID、worker GID 10001 与 `umask 0002` 访问共享 SQLite；
worker 以 10001:10001 只读 root:10001/0640 的 providers.json，其他秘密仍 root:root/0600。

服务器证据确认 timer 于上海 08:00 触发，旧 service 成功但 journal 仅有抓取/翻译/构建
三阶段；SMTP 六项为 `SET`，尚无新投递开关/安全模式键，符合原版本从未进入投递的根因。

用户已在 Admin 确认真正的默认 provider 固定 `Hi` 测试通过；这里只记录连接、鉴权和模型返回
门禁通过，不记录 Base URL、key 或完整响应。preview 已由开发会话重启；现在需用户先核对此前
SMTP 服务端投递/队列日志，再人工核验保存/连接/预览/单次测试邮件、订阅管理与三视口 UI，
并明确回复通过；随后才运行唯一最终全量与最终
针对性复审。正式版仍待小型正式 schema 兼容测试、SMTP 连接与测试邮件实际送达，以及用户决定
新版本号和手动部署时间。
本次接管执行未读取 `.env.local`、未访问真实 API/SMTP、未部署、未发布、未推送、未操作 tag。
