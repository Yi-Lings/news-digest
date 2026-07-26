# 运维手册

日常操作与故障恢复。§1–§6 面向 Windows 本地（命令在项目根目录 `E:\new\news-digest`
执行）；§7 面向生产服务器（部署与回滚详见 `deploy/README.md`）。

## 1. 日常使用

**每天一次：双击 `daily.bat`。** 执行 `uv run news-digest run --yes`（抓取 → 选题 6 篇 → 真实翻译 → 构建，产生 API 费用），完成后自动打开 `http://127.0.0.1:8618/`。未设代理时脚本自动补 `NEWS_HTTP_PROXY=http://127.0.0.1:2231`；代理端口变了改 `daily.ps1` 里这一行。流水线部分失败时站点通常仍已构建，看窗口输出即可。

**只看页面：双击 `preview.bat`。** 不调用翻译 API：有 `var/data/fetched/*.json` 则先 `build` 发布最新数据；既无抓取数据也无站点时才构建 demo 演示页。随后在 8618 起预览服务（端口已被本服务占用时直接复用）并打开浏览器。服务器窗口最小化运行，关掉该窗口即停止预览。

分步命令（`uv run news-digest <命令>`）：

| 命令 | 作用与关键参数 |
|---|---|
| `fetch` | 抓取真实新闻源并入库，同时写 `var/data/fetched` 快照；`--window-hours N` 覆盖时间窗口（默认 24） |
| `translate` | 翻译当日选题主文章；默认只打印计划，加 `--yes` 才真实调用；`--date YYYY-MM-DD`（默认最新一期）、`--limit N` 限量、`--redo SLUG` 强制重翻（可多次，不受 `--limit` 约束） |
| `build` | 由数据库版次生成静态站点并切换 `var/site/current`；`--fixtures tests/fixtures/demo` 改用演示数据 |
| `run` | 完整流水线：抓取→选题→翻译→构建；不加 `--yes` 跳过翻译、主文章以英文原文成刊 |
| `preview` | 伺服 `var/site/current` 并提供 `/admin/` 模型面板；仅绑定 127.0.0.1，`--port` 默认 8618 |
| `preview-email` | 渲染当日简报为 `.eml` + `.html` 到 `var/mail`，不联网；`--date` 可指定 |
| `send-email` | 真实发送简报，需 `--yes`；前置：SMTP 配置齐全、站点已含当日、当日未发送过（`--resend` 越过防重记录） |

## 2. 配置

真实配置写 `.env.local`（.gitignore 排除）。CLI 启动时将其合并进环境变量，已存在的环境变量优先；测试不读取该文件。

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEWS_ENV` | development | 预留标识，当前代码未读取 |
| `NEWS_SITE_URL` | `http://127.0.0.1:8618` | 页面与邮件中的站点入口地址 |
| `NEWS_TIMEZONE` | `Asia/Shanghai` | 抓取窗口与日期归属 |
| `NEWS_DATABASE_PATH` | `var/data/news.db` | SQLite 文章池路径 |
| `NEWS_OUTPUT_PATH` | `var/site` | 静态站点输出根目录 |
| `NEWS_DATA_DIR` | `var/data` | 数据目录；翻译缓存在其下 `translations/` |
| `NEWS_HTTP_PROXY` | 空 | fetch 阶段代理；本机必设（见 §3），daily.bat 未设时自动补 |
| `NEWS_FETCH_WINDOW_HOURS` | 24 | 抓取时间窗口（小时） |
| `TRANSLATION_API_BASE_URL` | 空 | **translate / run --yes 必填**；通常以 `/v1` 结尾 |
| `TRANSLATION_API_KEY` | 空 | **同上必填**；接口密钥 |
| `TRANSLATION_MODEL` | 空 | **同上必填**；模型名 |
| `TRANSLATION_TIMEOUT_SECONDS` | 180 | 单请求超时；长文流式生成可达数分钟 |
| `TRANSLATION_MAX_TOKENS` | 8192 | 译文长度余量；Claude 系后端必填此参数 |
| `SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM` | 空 | **仅 send-email 必填** |
| `SMTP_PORT` | 465 | 465 隐式 SSL，587 STARTTLS |
| `SMTP_RECIPIENTS` | 空 | 收件地址，逗号分隔；**仅 send-email 必填** |
| `SMTP_USE_TLS` | true | 设 false 关闭加密（不建议） |

**模型切换面板：** preview 运行中访问 `http://127.0.0.1:8618/admin/`。供应商档案（base_url + model + key）存于 `.env.providers.local`（JSON、明文密钥、仅本机、.gitignore 排除）；点「启用」把三个 `TRANSLATION_*` 写入 `.env.local`，下次 translate 生效。页面只显示密钥掩码，编辑档案时密钥留空表示沿用；翻译缓存按模型隔离，切换供应商互不污染。

## 3. 已知环境坑与对策

**本机代理是 fake-ip 模式。** DNS 对所有域名返回 198.18.0.0/15 假地址，抓取层的私网阻断会误拦全部来源。对策：设 `NEWS_HTTP_PROXY`（daily.bat 未设时自动用 `http://127.0.0.1:2231`）。代理生效时本地 DNS 公网校验交由代理处理，域名 allowlist 不变，生产无代理时防护完整。

**PyPI 直连挂起。** 对策：`$env:UV_DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple/"`。`uv.lock` 已固定阿里镜像地址，日常 `uv sync` 直接走镜像；只有 `uv add` / `uv lock` 更新依赖时需要该变量。

**8000 与 8080 端口被本机常驻程序占用。** 预览固定用 8618（`preview --port` 与 `NEWS_SITE_URL` 默认值一致），不要改回。

**翻译网关 504。** 长文生成超过网关（Nginx）读超时会被截断，已改流式 SSE 响应根治，无需配置；`TRANSLATION_TIMEOUT_SECONDS=180` 即按此设定。

**模型参数兼容。** Claude 系后端必填 `max_tokens`（对应 `TRANSLATION_MAX_TOKENS`）；推理系模型拒绝 `temperature`（代码已不发送）。均已在代码处理；换供应商报 400 时先想到这两条，错误信息会带响应体片段。

**邮件被 554 内容反垃圾拒发**：`NEWS_SITE_URL` 必须是公网正式域名——默认的 `127.0.0.1:8618` 会让邮件正文布满 localhost 链接，触发服务商内容反垃圾（阿里云 DirectMail 实测 554 spam content）。

## 4. 故障恢复

**项目目录整体移动后预览空白**：`var\site\current` 的 NTFS 目录联接存的是绝对路径，移动项目目录后会失效；重跑一次 `uv run news-digest build`（或双击 daily.bat）即可重建。


**翻译中断（Ctrl+C 或断网）。** 直接重跑同一条命令：已成功篇目在 `var/data/translations/` 请求级缓存中，续接瞬时完成、不重复计费。daily 流程里中断则以当前状态成刊，之后单独 `translate --yes` 补齐再 `build`。

**单篇翻译质量差。** `uv run news-digest translate --redo SLUG --yes`（SLUG 即文章页地址 `/issues/日期/SLUG.html` 的末段，也出现在翻译进度输出里；可多次 `--redo`），完成后 `build`。重翻跳过缓存读取并覆盖旧结果，产生一次真实调用。

**构建失败。** 无需清理：`var/site/current` 是指向 `releases/<日期-序号>` 的链接（Windows 为 junction），只在新版本完整生成后才切换；失败时仍指向上一完整版本，修好后重跑 `build` 即可。

**报「schema 版本不匹配：库中为 X，代码期望 1，需迁移」。** 数据库由另一代码版本创建。不要删库重建（丢全部翻译成果）：先备份 `var/data/news.db`，再按迁移记录升级，或把代码切回与库匹配的版本。

## 5. 数据备份

需要备份的只有三样：

| 路径 | 理由 |
|---|---|
| `var/data/news.db` | 文章池 + 翻译成果（翻译随文章入库），丢了等于重新付费翻译 |
| `var/data/translations/` | 请求级翻译缓存（内容哈希 + 模型 + prompt 版本为键），中断续接与防重复计费靠它 |
| `.env.local`、`.env.providers.local` | 密钥。只手工复制到本机安全位置，绝不进 Git、云盘或对外压缩包 |

`var/site` 不备份——任何时候 `build` 可重建；`var/mail` 是预览产物，同理。

备份命令（项目根目录，确认没有 fetch / translate / send-email 正在写库）：

```powershell
Compress-Archive -Path var\data\news.db, var\data\translations `
  -DestinationPath "news-backup-$(Get-Date -Format yyyy-MM-dd).zip"
```

密钥文件另行手工复制，不放入上述 zip。

## 6. 从零重建

全新 checkout 后按序执行（PowerShell，项目根目录）：

```powershell
$env:UV_DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple/"   # 见 §3
uv sync
Copy-Item .env.example .env.local   # 填入 TRANSLATION_* 三项与 NEWS_HTTP_PROXY
# 有备份则先还原 var\data\news.db 与 var\data\translations\
uv run pytest                       # 可选自检，应全绿
```

然后双击 `daily.bat` 完成首次完整流水线。无备份时数据库自动新建，当天即产出完整站点，历史日期从零积累。`TRANSLATION_*` 也可不手填：先双击 `preview.bat`，在 `/admin/` 面板录入档案并启用。

## 7. 生产环境（服务器）

**模型切换面板。** 浏览器访问 `https://news.cheapcoding.top/admin/`，进入面板自带的网页登录页（用户名默认 `admin`，登录后发放会话 Cookie——不再是浏览器 Basic Auth 弹窗）。首次口令在服务器上查看：`sudo cat /srv/news-digest/config/admin-password.initial`（口令不出现在部署日志里）。**登录后请立即在面板网页修改口令**：修改成功会使所有已登录端失效（轮换会话密钥）并自动删除该初始口令文件。忘记口令：`sudo rm /srv/news-digest/config/htpasswd-admin /srv/news-digest/config/session-secret` 后重跑 bootstrap 重新生成。面板支持完整管理：切换档案、改接口地址、改模型名、**新增/更换密钥**（用户决定 2026-07-27 开放；密钥经 HTTPS + 登录会话提交后只落服务器文件，页面与接口响应永远只显示掩码，编辑时留空表示沿用旧密钥）。点「启用」把三个 `TRANSLATION_*` 写入服务器 `/srv/news-digest/config/.env`，**无需重启任何容器**：worker 每次由 timer 经 `docker compose run` 拉起时重读 `.env`，下一期即生效。也仍可 ssh 直接编辑 `config/providers.json`，两种方式等效。

**换密钥的正确姿势。** ssh 登录服务器直接编辑文件（两个文件均 root:600，改完都不用重启）：

- 新增/更换供应商档案：编辑 `/srv/news-digest/config/providers.json`（`base_url` / `api_key` / `model` 三字段，格式见 `deploy/README.md` §13），保存后刷新面板即可见、可切换；
- 只换当前生效的密钥：编辑 `/srv/news-digest/config/.env` 的 `TRANSLATION_API_KEY`；注意把 `providers.json` 里对应档案的 `api_key` 同步改掉，否则日后在面板点「启用」会把旧 key 写回去。

密钥永远不经网页、不进 Git、不进镜像；翻译缓存按模型隔离，切换供应商互不污染。面板故障不影响每日任务（admin 是独立常驻容器，worker 只依赖 `.env` 文件本身）。
