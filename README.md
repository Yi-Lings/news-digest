# Cheapcoding News · 每日双语新闻

每天早上 8 点（北京时间），它自动从 BBC、卫报、半岛电视台等主流英文媒体抓取过去 24 小时的新闻，挑出 6 篇值得精读的，翻译成中文做成中英对照，再配上几十条简讯，发布成一份安静好读的报纸风网页。

做这个是为了学英语：先读英文，卡住了看中文，不用来回切词典。

在线实例：<https://news.cheapcoding.top>

## 你能看到什么

- **每天一期**：6 篇双语主文章 + 几十条简讯，头版排布像一份报纸
- **三种阅读模式**：英文 / 双语对照 / 中文，页面右上角一键切换
- **往期归档**：每天自动多一期，随时回看
- **来源筛选**：按媒体过滤当天内容
- 每篇文章都注明并链接原始来源；中文由 AI 生成，页脚有明确标识

部署好之后不需要人管：新闻自动更新，HTTPS 证书自动续期，旧版本自动清理。

## 长什么样

首页头版，报纸式排布：左侧当日头条配图，右侧其余主文章，标题与摘要中英并排，顶部可按来源筛选：

![首页头版](docs/screenshots/home.png)

文章页的双语对照模式：英文原文逐段精读，中文以批注体紧随其后（朱红「译」标），每段可单独朗读：

![文章页双语对照](docs/screenshots/bilingual.png)

往期归档，每天自动多一期，随时回看任意一天：

![往期归档](docs/screenshots/archive.png)

## 部署一份自己的

### 需要准备

1. 一台能跑 Docker 的 Linux 服务器（1 GB 内存足够）
2. 一个解析到这台服务器的域名
3. 一个 OpenAI Chat 或 Anthropic Messages 兼容的翻译 API：地址、密钥、模型名
4. （可选）一个支持 SSL/TLS 或 STARTTLS 的 SMTP 邮箱；邮件投递和公开订阅需要

### 一条命令

在服务器上以 root 执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Yi-Lings/news-digest/main/deploy/install.sh)
```

它会自动体检环境、拉取镜像、装好定时任务和 HTTPS，然后**停下来让你填密钥**：
编辑 `/srv/news-digest/config/.env`，填入翻译 API 配置（SMTP 可暂留空且邮件保持关闭），再跑一次同一条命令即完成。

想换域名、端口或安装目录？执行命令前设置环境变量即可，例如：

```bash
export ND_DOMAIN=news.example.com
```

全部可调参数见 `deploy/README.md`。仓库私有期间需先 `export GH_TOKEN=你的只读token`，仓库公开后不用。

## 日常怎么用

### 管理面板

浏览器打开 `https://你的域名/admin/`。初始口令在服务器上：

```bash
cat /srv/news-digest/config/admin-password.initial
```

登录后请立即在面板里改掉它。面板能做的事：

- **换翻译模型 / 换 API**：选择 OpenAI Chat 或 Anthropic Messages 协议，新增档案并设为唯一默认；“测试连接”会在确认后发送一次固定 `Hi` 的最小真实请求，可能计费
- **配置邮件**：设置 SSL/TLS 或 STARTTLS、主文/简讯数量、语言、来源、摘要长度和版式；收件人统一在订阅名单中管理，连接测试不发信，测试邮件需要再次确认
- **查看与重试投递**：逐收件人显示 `sent` / `failed` / `unknown`；只自动重试 `failed`，`unknown` 必须确认可能重复送达的风险
- **管理订阅**：查看脱敏订阅状态；公开表单采用 double opt-in，一键退订后不能在面板直接复活
- **改面板口令**：需要输入当前口令；改完所有已登录的浏览器都会下线，重新登录即可

### Admin 翻译监控与错误处理（v1.2.0）

“翻译状态”固定在“订阅管理”和“投递状态”之间。顶部显示刊期、最后更新时间、provider 并发与队列、连续失败次数及熔断状态；摘要和筛选可分别查看全部、运行中、待重试、失败和已上线任务。任务行显示当前阶段、尝试次数、HTTP 状态、内部错误代码、失败阶段、下一次重试时间和脱敏诊断 ID，不显示文章正文、API 地址、密钥或完整 provider 响应。

- **立即重试**：只把当前失败文章加入队列并跳过剩余等待时间；任务仍需取得原子 lease，重复点击和多管理员并发不会创建第二个请求。
- **终止请求**：仅对运行中任务可用。确认后先请求执行体停止，确认旧请求已经结束后才记录 `REQUEST_CANCELLED` 并安排该篇重试。
- **立即探测**：同一 provider 连续 5 次基础设施失败后熔断；自动探测依次等待 60 秒、2 分钟、5 分钟，管理员也可选择一篇失败文章执行一次受控正式 schema 探测。同一 provider 同时只允许一个探测 lease。
- **配置恢复**：遇到 `AUTH_401`、`AUTH_403` 或 `CONFIGURATION_INVALID` 时，按“修正并保存 provider 配置 → 执行小型正式 schema 测试 → 在翻译状态中手动恢复”的顺序处理，不要用重复重试代替配置验证。

| 内部错误代码 | 含义 | 系统动作 | 管理员处置 |
|---|---|---|---|
| `AUTH_401` | API 凭据无效 | 配置阻断，不自动重试 | 修正凭据并完成受控测试 |
| `AUTH_403` | 接口或模型无权限 | 配置阻断，不自动重试 | 检查账号与模型权限 |
| `RATE_LIMIT_429` | provider 限流 | 单篇退避并计入熔断 | 等待恢复或受控探测 |
| `PROVIDER_5XX` | provider 服务异常 | 单篇退避并计入熔断 | 等待恢复或受控探测 |
| `NETWORK_CONNECT_FAILED` | 网络连接失败 | 单篇退避并计入熔断 | 检查网络、代理与 DNS |
| `REQUEST_TIMEOUT` | 请求超时 | 旧请求终止后单篇退避并计入熔断 | 确认旧请求已结束，再等待或探测 |
| `EMPTY_RESPONSE` | 响应为空 | 只重试当前文章 | 等待或立即重试该篇 |
| `UNPARSEABLE_RESPONSE` | 响应无法解析 | 只重试当前文章 | 检查协议与模型兼容性 |
| `SCHEMA_VALIDATION_FAILED` | 正式翻译 schema 不合格 | 只重试当前文章，不计入熔断 | 立即重试该篇或检查模型兼容性 |
| `CONFIGURATION_INVALID` | 配置缺失或冲突 | 配置阻断，不自动探测 | 修正、保存并测试配置 |
| `REQUEST_CANCELLED` | 管理员终止请求 | 确认执行体停止后进入待重试 | 确认原因后恢复该篇 |
| `CIRCUIT_OPEN` | provider 已熔断 | 暂停新翻译，不影响站点和 Admin | 等待冷却或立即探测 |

本地验收必须显式使用隔离 fake demo；该模式使用独立 SQLite 和固定 fixture，不调用 provider 或 SMTP：

```powershell
uv run news-digest preview --port 8618 --automation-demo
```

禁止连续点击重试、直接修改任务数据库、绕过任务 lease，或在日志、截图和工单中暴露秘密、文章正文及完整 provider 响应。

公开订阅默认关闭。确认生产 HTTPS、SMTP、发件人和全局邮件投递均可用后，在服务器 `config/.env` 设置 `PUBLIC_SUBSCRIPTION_ENABLED=true` 并重新构建站点；Admin 逐请求读取该开关，无需重启。隐私说明位于 `/privacy/`。

正式刊物邮件仅发送 UTF-8 纯文本，Admin 中的 HTML 只用于页面预览，不进入 SMTP；邮件仍含标准一键退订头。SMTP 部分拒收会逐收件人记录；连接在 DATA 后中断时标为 `unknown`，系统不会自动重发，避免可能已经送达的邮件重复出现。

### 手动触发一次更新

不想等到明早 8 点：

```bash
cd /srv/news-digest && docker compose run --rm worker
```

该命令执行抓取、翻译、构建、投递四阶段。邮件关闭时明确跳过；邮件开启时只有处于 08:00 补跑窗口且当天 release 有效才自动投递。白天人工发送指定刊期应使用 Admin 的预览与确认流程。

## 常见问题

**今早没更新？**
`journalctl -u news-digest.service -n 50` 看日志。最常见原因是翻译 API 出问题——去面板换一个档案，再手动跑一次上面的命令。

**忘了面板口令？**
在服务器上重设（顺手删掉会话密钥，让所有旧登录失效）：

```bash
printf 'admin:%s\n' "$(openssl passwd -apr1 '新口令')" > /srv/news-digest/config/htpasswd-admin
chmod 600 /srv/news-digest/config/htpasswd-admin
rm -f /srv/news-digest/config/session-secret
```

**站点打不开或 404？**
`cd /srv/news-digest && docker compose ps` 看 web 是否 healthy；`curl http://127.0.0.1:8618/healthz` 应输出 ok。

**为什么固定每天 08:00？**
正式版把 `NEWS_TIMEZONE=Asia/Shanghai` 与 systemd timer 固定为同一时区和 08:00；bootstrap 会拒绝不一致配置，避免刊期、主题日期和补跑窗口分叉。

**个别文章没有中文？**
模型偶尔输出不合格会被整篇丢弃（宁缺毋滥），当天该篇只显示英文，次日新一期自然覆盖。

**内容版权？**
所有内容取自各媒体公开 RSS 与页面，版权归原出版方；本项目仅供个人学习使用，请勿公开转载受版权保护的全文。

---

如需二次开发或了解实现细节，请阅读[《技术路线.md》](技术路线.md)。服务器运维手册见 `docs/OPERATIONS.md`，部署参数全表见 `deploy/README.md`。
