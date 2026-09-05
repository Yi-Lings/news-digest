# 运维手册

日常操作与故障恢复。§1–§6 面向 Windows 本地（命令在克隆后的项目根目录执行）；
§7 面向生产服务器（示例安装目录为 `/srv/news-digest`，请替换为实际 `ND_APP_DIR`；
部署与回滚详见 `deploy/README.md`）。

## 1. 日常使用

**每天一次：双击 `daily.bat`。** 执行 `uv run news-digest run --yes`（抓取 → 选题 6 篇 → 真实翻译 → 构建 → 投递，产生 API 费用；邮件默认关闭），完成后自动打开 `http://127.0.0.1:8618/`。未设代理时脚本自动补 `NEWS_HTTP_PROXY=http://127.0.0.1:2231`；代理端口变了改 `daily.ps1` 里这一行。邮件失败时已发布站点不回滚，但进程返回非零。

**只看页面：双击 `preview.bat`。** 不调用翻译 API：有 `var/data/fetched/*.json` 则先 `build` 发布最新数据；既无抓取数据也无站点时才构建 demo 演示页。随后在 8618 起预览服务（端口已被本服务占用时直接复用）并打开浏览器。服务器窗口最小化运行，关掉该窗口即停止预览。

分步命令（`uv run news-digest <命令>`）：

| 命令 | 作用与关键参数 |
|---|---|
| `fetch` | 抓取真实新闻源并入库，同时写 `var/data/fetched` 快照；`--window-hours N` 覆盖时间窗口（默认 24） |
| `translate` | 翻译当日选题主文章；默认只打印计划，加 `--yes` 才真实调用；`--date YYYY-MM-DD`（默认最新一期）、`--limit N` 限量、`--redo SLUG` 强制重翻（可多次，不受 `--limit` 约束） |
| `build` | 由数据库版次生成静态站点并切换 `var/site/current`；`--fixtures tests/fixtures/demo` 改用演示数据 |
| `run` | 四阶段流水线：抓取→选题/翻译→构建→投递；不加 `--yes` 跳过翻译，邮件关闭时投递阶段明确跳过 |
| `preview` | 伺服 `var/site/current` 并提供 `/admin/` 模型面板；仅绑定 127.0.0.1，`--port` 默认 8618 |
| `preview-email` | 从不可变 release manifest 和已保存内容组合渲染 `.eml` + `.html` 到 `var/mail`，不联网；`--date` 可指定 |
| `send-email` | 真实发送指定刊期，需 `--yes`；`--resend` 仅重试 `failed`，`unknown` 需同时使用 `--retry-unknown --confirm-unknown-risk` |

## 2. 配置

真实配置写 `.env.local`（.gitignore 排除）。CLI 启动时将其合并进环境变量，已存在的环境变量优先；测试不读取该文件。

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEWS_ENV` | development | 预留标识，当前代码未读取 |
| `NEWS_SITE_URL` | `http://127.0.0.1:8618` | 页面与邮件中的站点入口地址 |
| `NEWS_TIMEZONE` | `Asia/Shanghai` | 抓取窗口与日期归属；生产必须与固定的 08:00 systemd timer 一致，bootstrap 会拒绝其他值或重复键 |
| `NEWS_DATABASE_PATH` | `var/data/news.db` | SQLite 文章池路径 |
| `NEWS_OUTPUT_PATH` | `var/site` | 静态站点输出根目录 |
| `NEWS_DATA_DIR` | `var/data` | 数据目录；翻译缓存在其下 `translations/` |
| `NEWS_HTTP_PROXY` | 空 | fetch 阶段代理；本机必设（见 §3），daily.bat 未设时自动补 |
| `NEWS_FETCH_WINDOW_HOURS` | 24 | 抓取时间窗口（小时） |
| `TRANSLATION_API_BASE_URL` | 空 | **translate / run --yes 必填**；通常以 `/v1` 结尾 |
| `TRANSLATION_API_KEY` | 空 | **同上必填**；接口密钥 |
| `TRANSLATION_MODEL` | 空 | **同上必填**；模型名 |
| `TRANSLATION_API_TYPE` | `openai_chat` | `openai_chat` 或 `anthropic_messages`；不按模型名猜协议 |
| `TRANSLATION_STREAM` | true | 是否使用对应 adapter 的流式 parser |
| `TRANSLATION_REASONING_EFFORT` | 空 | GPT 模型可选 `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max`；空值按模型默认，不同模型支持子集可能不同 |
| `TRANSLATION_TIMEOUT_SECONDS` | 600 | 单请求硬总超时；连接 10 秒、流读取静默 30 秒；适配 Luna max 等高推理档位，正式翻译的可恢复重试总预算仍为 95 秒 |
| `TRANSLATION_MAX_TOKENS` | 8192 | 译文长度余量；Claude 系后端必填此参数 |
| `EMAIL_DELIVERY_ENABLED` | false | 每日自动投递总开关；关闭时不解析残留 SMTP 字段 |
| `SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM` | 空 | 开启投递前必填；用户名/密码必须同时为空或同时设置 |
| `SMTP_PORT` | 465 | 465 隐式 SSL，587 STARTTLS |
| `SMTP_SECURITY` | `implicit_tls` | `implicit_tls` 或 `starttls`；生产不支持明文 SMTP |
| `SMTP_RECIPIENTS` | 空 | 仅供旧安装一次性导入；正式收件人统一在 Admin 订阅名单管理 |
| `EMAIL_MAINS_ENABLED` / `EMAIL_BRIEFS_ENABLED` | true | 是否选择主文/简讯；至少开启一种 |
| `EMAIL_MAIN_LIMIT` / `EMAIL_BRIEF_LIMIT` | 6 / 5 | 不得超过当前 release 实际数量 |
| `EMAIL_LANGUAGE` | `bi` | `bi` / `zh` / `en` |
| `EMAIL_SOURCE_FILTERS` | 空 | 当前 release 的结构化来源，逗号分隔；空表示不限 |
| `EMAIL_LAYOUT` | `digest` | `digest`（摘要导读）或 `compact`（紧凑列表） |
| `EMAIL_SUMMARY_LENGTH` | `standard` | `short` / `standard` / `long`，发送时不再次调用模型 |
| `EMAIL_CATCHUP_WINDOW_HOURS` | 6 | 08:00 后自动补跑窗口，0–24；窗口外不自动发旧刊 |
| `PUBLIC_SUBSCRIPTION_ENABLED` | false | 公开新订阅门；既有确认/退订端点不受关闭影响 |

Admin 保存 SMTP 密码时会在 `config/.env` 中写为 `nd-b64-v1:` 开头的 UTF-8 Base64，
用于避免 Compose 对 `$`、引号、空格和 `#` 的 dotenv 解释改变密码；这只是传输编码，
不是加密。旧明文配置仍可读取，下一次 Admin 保存时自动迁移。

**Admin：** preview 运行中访问 `http://127.0.0.1:8618/admin/`。供应商档案存于 `.env.providers.local`（生产为 `/config/providers.json`）；每档案显式保存协议、stream、推理强度、启用和默认状态。每日翻译只使用唯一默认档案。页面只返回 `key_set`，编辑时 key 留空表示沿用。点击“测试连接”前会确认一次固定 `Hi` 的真实生成请求（2 字符输入、最多 8 output tokens、可能计费），不提供任意测试消息输入框。推理强度下拉仅对 OpenAI GPT 模型生效；非 GPT 模型或 Anthropic 请求不会发送该字段。

邮件区的连接测试使用当前表单但不写配置、不发信；测试邮件可使用当前 SMTP 表单，但内容组合和收件人强制使用已保存值，且必须从 active 订阅行按单个 `subscription_id` 选择。预览、测试、08:00 自动投递和指定刊期人工发送共用同一内容选择器。投递状态按收件人记录；`unknown` 表示 SMTP 可能已接受 DATA，自动任务不得重试。

正式刊物与刊物测试邮件仅发送 UTF-8 `text/plain`；Admin 的 HTML 仅用于页面预览，不进入 SMTP。正式订阅刊物仍设置 `List-Unsubscribe` 与 RFC 8058 one-click 头。SMTP 部分拒收逐收件人记 `failed`，全部拒收
使 run 失败；DATA 后连接复位等无法确认是否送达的结果记 `unknown`。`.eml` 归档失败会令
服务返回失败，但不会把已经成功投递的收件人改回未发送，也不会触发自动重复发送。

公开订阅默认关闭。生产只有 `NEWS_SITE_URL` 为公网 HTTPS、SMTP 完整且 `EMAIL_DELIVERY_ENABLED=true` 时才允许设置 `PUBLIC_SUBSCRIPTION_ENABLED=true`。提交端点逐请求读取该开关，Admin 无需重启；修改后运行 `docker compose run --rm worker build` 重新生成带表单的首页。Admin 不管理该生产就绪门。订阅采用 double opt-in，确认 token 限时且数据库只存摘要。退订为 RFC 8058 one-click，`active → unsubscribed` 后自动、失败重试和人工重发均排除；重新订阅必须重新确认。隐私说明页为 `/privacy/`。

## 3. 已知环境坑与对策

**本机代理是 fake-ip 模式。** DNS 对所有域名返回 198.18.0.0/15 假地址，抓取层的私网阻断会误拦全部来源。对策：设 `NEWS_HTTP_PROXY`（daily.bat 未设时自动用 `http://127.0.0.1:2231`）。代理生效时本地 DNS 公网校验交由代理处理，域名 allowlist 不变，生产无代理时防护完整。

**PyPI 直连挂起。** 对策：`$env:UV_DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple/"`。`uv.lock` 已固定阿里镜像地址，日常 `uv sync` 直接走镜像；只有 `uv add` / `uv lock` 更新依赖时需要该变量。

**8000 与 8080 端口被本机常驻程序占用。** 预览固定用 8618（`preview --port` 与 `NEWS_SITE_URL` 默认值一致），不要改回。

**翻译网关超时。** 正式单篇请求默认硬总时限为 600 秒（可用 `TRANSLATION_TIMEOUT_SECONDS` 覆盖），连接超时 10 秒、流读取静默超时 30 秒；Luna max 等高推理档位可在硬截止内完成，任务 lease 为 900 秒，避免请求尚未结束就被恢复器回收。每日与恢复 worker 的 systemd 兜底时限为 90 分钟，覆盖默认 6 篇顺序翻译及构建/投递余量。连接/读取超时、上游 `400/401/403`、429 和受限 5xx 按单篇有限重试与 provider 熔断处理。TLS、协议、响应格式和 schema 错误停止自动退避并保留人工重试动作；流已开始后不整次重试，避免重复计费。

**API/SMTP 出网目标。** translation client 明确忽略系统 `HTTP_PROXY` / `HTTPS_PROXY`；API 与 SMTP 都先要求 DNS 的全部结果为公网地址，再把本次 TCP 连接固定到已验证地址，同时继续用原域名执行 HTTP Host、TLS SNI 与证书主机名校验。环境代理不会绕过 provider 校验，DNS 重绑定也不能把实际连接切到私网地址。

**模型参数兼容。** OpenAI Chat 与 Anthropic Messages 由 adapter 原子切换 URL、鉴权 header、payload、system 位置和 parser；不要把完整 `/chat/completions` 或 `/messages` endpoint 填入 Base URL。Admin 只显示脱敏分类、HTTP status、协议、模型与耗时，不回显上游原始响应或秘密。

**邮件被 554 内容反垃圾拒发**：`NEWS_SITE_URL` 必须是公网正式域名——默认的 `127.0.0.1:8618` 会让邮件正文布满 localhost 链接，触发服务商内容反垃圾（阿里云 DirectMail 实测 554 spam content）。

## 4. 故障恢复

**项目目录整体移动后预览空白**：`var\site\current` 的 NTFS 目录联接存的是绝对路径，移动项目目录后会失效；重跑一次 `uv run news-digest build`（或双击 daily.bat）即可重建。


**翻译中断（Ctrl+C 或断网）。** 直接重跑同一条命令：已成功篇目在 `var/data/translations/` 请求级缓存中，续接瞬时完成、不重复计费。daily 流程里中断则以当前状态成刊，之后单独 `translate --yes` 补齐再 `build`。

**Admin 显示 `provider probe is already queued`。** 先检查 `systemctl status news-digest-wakeup.path news-digest-resume.service`。Admin 的“立即调度/立即探测/重试/终止/恢复”只写持久队列和 `${APP_DIR}/config/automation.wake`，由 path 单元启动 `resume-automation --yes`；HTTP 进程不直接调用 provider。恢复 worker 与每日 worker 由 `/run/news-digest-worker.lock` 串行。探测 lease 过期后恢复器会清理 `half_open` 和排队标记，之后可再次探测；如果探测已经处于 `half_open`，再次点击会复用当前探测，不会创建第二个请求。path 正常但历史请求仍在排队时，执行 `sudo systemctl start news-digest-resume.service`，不要删除数据库记录或重复点击。

**当天或次日出现翻译阻断。** 先在 Admin 查看 provider 的实际测试结果，不要只看 `/v1/models`。若为上游 `UPSTREAM_ERROR`，确认余额、权限和网关状态，等待自动退避或点击“立即探测”；若为 `CONFIGURATION_INVALID`，保存正确配置并完成一次成功的受控测试，再点击任务行“解除阻断”。恢复后只会重新调度失败文章，不会删除尝试审计、文章、release 或投递记录，也不会自动补发历史刊期。

**任务显示“等待模型生成”但反复失败退避。** 查看错误代码：`SCHEMA_VALIDATION_FAILED`、`EMPTY_RESPONSE` 和 `UNPARSEABLE_RESPONSE` 属于模型响应内容问题，升级修复版后会停止自动循环并保留“立即重试”。`TASK_DATA_MISSING` 表示任务引用的原文已不存在，需先恢复原文或重新生成任务；不要直接删除任务记录。

**Admin 显示“等待终止确认”。** 运行中的任务先显示取消请求；lease 未过期时等待 worker 确认。lease 过期后任务行显示“恢复为可重试”，点击只创建恢复 action 并唤醒 worker；恢复器确认旧进程不再运行后才把任务转为 `retry_wait`。不要在确认前手动重发同一篇。

**单篇翻译质量差。** `uv run news-digest translate --redo SLUG --yes`（SLUG 即文章页地址 `/issues/日期/SLUG.html` 的末段，也出现在翻译进度输出里；可多次 `--redo`），完成后 `build`。重翻跳过缓存读取并覆盖旧结果，产生一次真实调用。

**构建失败。** 无需清理：`var/site/current` 是指向 `releases/<日期-序号>` 的链接（Windows 为 junction），只在新版本完整生成后才切换；失败时仍指向上一完整版本，修好后重跑 `build` 即可。

**报「schema 版本不匹配：库中为 X，代码期望当前版本，需迁移」。** 数据库由另一代码版本创建。不要删库重建（丢全部翻译成果）：先备份 `var/data/news.db`，再按迁移记录升级，或把代码切回与库匹配的版本。v1.2.9 会为动作表执行向前迁移并生成 `news.db.pre-v7.bak`（同时保留既有 v5/v6 迁移备份），旧翻译、文章、release 和投递审计保留不变。

## 5. 数据备份

从 t28 起，生产 `news-digest-backup.timer` 每天 10:30 Asia/Shanghai 执行 SQLite
online backup，并在独立目录解包验证。备份不需要停站，不调用模型、网关或 SMTP。
保留最近 14 个成功恢复包，目录 `/srv/news-digest/backups/daily/` 为 0700，文件为 0600。
校验未通过的包不会替换已有恢复点，也不会推进 `backup_verified_at`。

恢复包包含数据库全部业务表、请求缓存、邮件归档、当前页面、保留的 releases 和
`.published` 发布证据，以及源配置、Site 投影和 Site session secret。旧刊物不能假定
随时可以重建，因此必须保留发布工件。恢复包含密钥与账号数据，禁止提交 Git 或公开上传。
当前交付为本机恢复点，不宣称已具备异地灾备；RPO 24h、RTO 4h 是观察目标，不是保证。

```bash
sudo systemctl start news-digest-backup.service
sudo journalctl -u news-digest-backup.service --since today
sudo systemctl list-timers news-digest-backup.timer
```

在隔离目录复核指定包：使用同版本镜像挂载备份目录到 `/backups`，执行
`news-digest verify-backup /backups/daily-指定时间.tar.gz`。命令仅解包、逐文件 SHA-256
校验、SQLite integrity/foreign key 校验、逐表事实对比以及当前 manifest/保留 release/
数据库结果身份核对，**绝不覆盖生产库**。
验证目录创建在备份包旁边，不占用容器的小容量 `/tmp` tmpfs。

覆盖恢复必须先停止 `news-digest.timer`、`news-digest-wakeup.path`、
`news-digest-backup.timer`，确认 daily/resume/backup service 全部退出，再停止 Site/Admin。
检查项目所有 worker 一次性容器与人工 CLI 均不存在。保留故障现场的数据库和发布工件，
不得直接复制仍有 WAL 写入的 `news.db` 或只停止 Admin。

恢复前逐笔核对备份之后的网关到账、退款、权益变更、正式邮件 `sent/unknown`。
若存在新增外部事实，禁止全库回退后直接 resume；应先在隔离副本合并事实、验证幂等，
或使用兼容当前库的修复镜像前滚。恢复包中的 `site/current` 是目录快照，生产切换时应
指向包内对应的 `site/releases/<release_name>`，保留 `current` 原子链接约定。
恢复完成后先启动 Site/Admin 验证 `/readyz`、订单与投递状态，最后才恢复 timer/path。

## 6. 从零重建

全新 checkout 后按序执行（PowerShell，项目根目录）：

```powershell
$env:UV_DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple/"   # 见 §3
uv sync
Copy-Item .env.example .env.local   # 填入 TRANSLATION_* 与 NEWS_HTTP_PROXY
# 有备份则先还原 var\data\news.db 与 var\data\translations\
uv run pytest                       # 可选自检，应全绿
```

然后双击 `daily.bat` 完成首次完整流水线。无备份时数据库自动新建，当天即产出完整站点，历史日期从零积累。`TRANSLATION_*` 也可不手填：先双击 `preview.bat`，在 `/admin/` 面板录入档案、测试后设为默认。

## 7. 生产环境（服务器）

**Admin 管理面板。** 浏览器访问 `https://news.example.com/admin/`（替换为实际 `ND_DOMAIN`），进入面板自带的网页登录页（用户名默认 `admin`，登录后发放会话 Cookie）。首次口令在服务器上查看：`sudo cat /srv/news-digest/config/admin-password.initial`（口令不出现在部署日志里）。**登录后请立即在面板网页修改口令**：修改成功会轮换会话密钥、使所有已登录端失效，并自动删除初始口令文件。忘记口令：`sudo rm /srv/news-digest/config/htpasswd-admin /srv/news-digest/config/session-secret` 后重跑 bootstrap。面板管理 provider 唯一默认档案、SMTP/内容组合、订阅与逐收件人投递状态；worker 每次启动都重新读取 `/config`，无需重启容器。

**换密钥的正确姿势。** 优先在 Admin 编辑档案（key 留空沿用）并重新执行固定 `Hi` 测试；也可 ssh 编辑 `/srv/news-digest/config/providers.json`。该文件是生产 provider 权威源，档案字段为 `base_url`、`api_key`、`model`、`api_type`、`stream`、`reasoning_effort`、`enabled`、`is_default`，同一时刻至多一个默认档案。改完不需要重启，下一次一次性 worker 会重读。

- `providers.json` 的唯一默认档案决定每日翻译；`.env` 的 `TRANSLATION_*` 仅保留迁移兼容，不在缺少默认档案时回退；
- 保存或设默认前会统一验证公网 HTTPS 目标；实际测试和正式翻译同样执行该校验。

**配置所有权。** `deploy-all` 不读取本地 `.env.local`，也不向服务器传输 API/SMTP 密钥。
bootstrap 仅在服务器缺少 `config/.env` 时创建 API/SMTP 为空、邮件投递和公开订阅关闭的
安全默认值；后续由 Admin/operator 热配置且部署不覆盖。镜像版本与运行时配置分别管理。

密钥可经 HTTPS + 登录会话写入 Admin，但永不由 API 回传、不进 Git/镜像/日志。翻译缓存 identity
包含协议、规范化 Base URL 和模型，不含 key。面板故障不影响已配置的每日任务；worker 读取
`/config/providers.json` 的唯一默认档案和 `/config/.env` 的邮件设置。
