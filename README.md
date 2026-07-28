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
