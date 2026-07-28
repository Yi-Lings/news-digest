# 部署工件与服务器侧操作步骤（阶段 7）

> **重要声明：本目录（含 `.github/workflows/release.yml`）全部为源码侧模板，
> 在目标服务器上实际执行并验证之前，一律不视为最终版。**
> 任何与服务器实测冲突之处，以实测结果为准并回写本目录。

服务器上不保存 Git 仓库与源码（PLAN 发布链路）：应用代码只存在于 GHCR 预构建镜像内，
服务器只保留 Compose manifest、`config/` 配置子目录（`.env`、`providers.json`、
面板口令，admin 容器唯一可触及的宿主路径）、systemd 单元、宿主机 Nginx 配置与持久化卷。

## 一键部署（类 sub2api）

新服务器上一条命令完成部署（root 执行）：

```bash
export GH_TOKEN=你的repo只读token   # 仓库转公开后无需此行与认证头
bash <(curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
  https://raw.githubusercontent.com/Yi-Lings/news-digest/main/deploy/install.sh)
```

自动完成：下载最新 Release 部署包 → 只读体检（preflight）→ 幂等部署（bootstrap：镜像、
web 容器、每日 08:00 定时器、Nginx、HTTPS）。首次运行会在 `/srv/news-digest/config/.env`
生成密钥模板并暂停，在服务器上填好真实值后重跑同一命令续接。部署包
（`news-digest-deploy.tgz`）由 CI 在每次推送 `v*` 标签时自动附到 Release，并携带该
Release 的准确 tag 与两个镜像 digest；安装器严格校验后按 digest 部署，不回退到默认 tag；
已解包场景直接 `sudo bash deploy/install.sh`。
换域名/换端口/换目录部署：执行命令前 export 对应 `ND_*` 环境变量即可，全表见 §0。

## 0. 占位符与部署参数（ND_*）对照表

模板中所有大写占位符及替换方法：

| 占位符 | 含义 | 如何得到 |
|---|---|---|
| `OWNER` | GHCR 命名空间（GitHub 用户名，全小写） | 仓库地址 `github.com/OWNER/news-digest` 中的用户名；GHCR 要求小写 |
| `VERSION` | 可读版本 tag（如 `v0.1.0`） | 本地验收后打的 Git tag；仅注释可读性用，生产必须换 digest |
| `DIGEST` | 镜像不可变摘要 `sha256:…` | release.yml 运行摘要（Actions → 对应 run → Summary），或 `docker buildx imagetools inspect ghcr.io/OWNER/news-digest-worker:VERSION` |
| `GHCR_USER` | 用于服务器登录 GHCR 的 GitHub 用户名 | 与 OWNER 相同或被授权的账号 |

部署链已参数化：`install.sh` / `bootstrap.sh` 执行前 export 下列 `ND_*` 环境变量即可
换域名/换端口/换目录部署，不改任何脚本。版本与两条 digest 没有默认值，必须来自同一个
Release；其余未设置项使用本项目生产默认值。
占位符替换与域名/端口渲染均由 bootstrap 按这些变量自动完成。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `ND_OWNER` | `yi-lings` | GHCR 命名空间；渲染 compose 中三处 image 引用（worker/admin 共用一处值） |
| `ND_VERSION` | 必填 | Release tag；下载模式由 Release 元数据提供 |
| `ND_WORKER_DIGEST` | 必填 | worker 不可变镜像摘要；正式 Release 由 `digests.env` 自动提供 |
| `ND_WEB_DIGEST` | 必填 | web 不可变镜像摘要；必须与 worker digest 同时提供 |
| `ND_APP_DIR` | `/srv/news-digest` | 部署目录（compose、`config/` 子目录内的 `.env` 与 `providers.json`、备份）；同步渲染 systemd 单元与 admin 挂载 |
| `ND_DOMAIN` | `news.cheapcoding.top` | 站点域名；渲染 nginx 配置、certbot 签发与 `.env` 模板的 `NEWS_SITE_URL` |
| `ND_WEB_PORT` | `8618` | web 容器宿主回环端口；渲染 compose 与 nginx |
| `ND_ADMIN_PORT` | `8619` | Admin 管理面板宿主回环端口；渲染 compose 与 nginx |
| `ND_CERTBOT_EMAIL` | `1481835649@qq.com` | 证书到期/吊销通知邮箱 |

示例：`export ND_DOMAIN=news.example.com ND_WEB_PORT=9000` 后执行一键部署命令。
后文手工步骤（§3、§6、§8 等）中的路径与端口均按默认值书写。

## 1. 发布一个部署候选（本地 → GHCR）

1. 本地验收通过后，确认工作树完全 clean、`HEAD` 已合入且等于本地 `main`，并确认
   `src/news_digest/__init__.py` 的 `__version__` 与将要打的**新 tag**一致；发布脚本要求
   tag 严格指向 HEAD，禁止复用或移动旧 tag。
2. 创建 annotated tag 并推送：`git tag -a v1.1.1 -m "v1.1.1" && git push origin v1.1.1`。
3. 等待 Actions `release` 工作流通过（先复跑离线测试，再构建推送两个镜像）。
4. 从该 run 的 Summary 复制两条 digest 引用（形如
   `ghcr.io/OWNER/news-digest-worker@sha256:…`），记入发布记录。

## 2. 服务器前置核对（一次性）

在动手部署前逐项确认，任何一项不符先解决再继续：

```bash
docker --version            # 期望 Docker Engine >= 24
docker compose version      # 期望 Compose v2.20+（本模板用 compose spec 语法）
uname -m                    # x86_64 → release.yml 的 PLATFORMS 保持 linux/amd64；aarch64 则改为 linux/arm64 后重新发布
systemctl --version | head -1   # 期望 systemd >= 236（timer 的 OnCalendar 时区语法）
systemd-analyze calendar '*-*-* 08:00:00 Asia/Shanghai'   # 能解析并给出下次触发时间即可
nginx -v                    # >= 1.25.1 才能启用 news.conf 中注释的 `http2 on;`
dig +short news.cheapcoding.top   # 必须指向本服务器公网 IP
free -m && df -h /          # 内存、磁盘余量核对（worker 峰值目标 < 150 MB）
```

## 3. 创建部署目录与配置

```bash
sudo mkdir -p /srv/news-digest/backups /srv/news-digest/config
sudo chown root:10001 /srv/news-digest/config          # worker 需穿越目录读取 providers.json
sudo chmod 750 /srv/news-digest/config                 # 密钥配置子目录：admin 容器唯一可写宿主路径
sudo cp compose.yaml /srv/news-digest/compose.yaml     # 从本目录 scp 上传后拷贝
sudo touch /srv/news-digest/config/.env
sudo chown root:root /srv/news-digest/config/.env
sudo chmod 600 /srv/news-digest/config/.env            # 密钥文件：600，root 所有
```

编辑 `/srv/news-digest/config/.env`（生产值示例；密钥只在服务器上出现，绝不进 Git / CI / 镜像）：

```dotenv
NEWS_ENV=production
NEWS_SITE_URL=https://news.cheapcoding.top
NEWS_TIMEZONE=Asia/Shanghai
NEWS_FETCH_WINDOW_HOURS=24

TRANSLATION_API_BASE_URL=（SUB2API 地址）
TRANSLATION_API_KEY=（密钥）
TRANSLATION_MODEL=（模型名）
TRANSLATION_API_TYPE=openai_chat
TRANSLATION_STREAM=true

EMAIL_DELIVERY_ENABLED=false
SMTP_HOST=（现有 SMTP）
SMTP_PORT=465
SMTP_USERNAME=（账号）
SMTP_PASSWORD=（密码）
SMTP_SECURITY=implicit_tls
SMTP_FROM=（发件地址）
SMTP_RECIPIENTS=（仅供旧安装一次性导入；新安装留空）
EMAIL_MAINS_ENABLED=true
EMAIL_BRIEFS_ENABLED=true
EMAIL_MAIN_LIMIT=6
EMAIL_BRIEF_LIMIT=5
EMAIL_LANGUAGE=bi
EMAIL_SOURCE_FILTERS=
EMAIL_LAYOUT=digest
EMAIL_SUMMARY_LENGTH=standard
EMAIL_CATCHUP_WINDOW_HOURS=6
PUBLIC_SUBSCRIPTION_ENABLED=false
```

Admin 保存后，`SMTP_PASSWORD` 会改写为 `nd-b64-v1:` 开头的 UTF-8 Base64，以保证
Compose `env_file` 对 `$`、引号、空格和 `#` 不做破坏性解释；这只是传输编码，不是加密。
旧明文值仍兼容，首次安装器与 Admin 会在写入生产配置时统一迁移。

`TRANSLATION_API_TYPE` 只能是 `openai_chat` 或 `anthropic_messages`；`SMTP_SECURITY`
只能是 `implicit_tls`（通常 465）或 `starttls`（通常 587/2525）。先保持两个 enable
开关为 `false`，通过 Admin 的真实 provider 测试、SMTP 连接测试和测试邮件后再开启。
公开订阅还要求公网 HTTPS 站点就绪。正式版的 timer 固定为 `Asia/Shanghai` 每日
08:00；bootstrap 要求 `.env` 中唯一的 `NEWS_TIMEZONE` 与之完全一致，否则停止部署。

手工部署没有运行 bootstrap 时，必须在首次 worker 前按 §13 的 JSON 格式创建
`/srv/news-digest/config/providers.json`，将上述翻译配置写成一个 `enabled=true`、
`is_default=true` 的唯一默认档案，并设置 `root:10001`、权限 `640`，供一次性 worker 只读。通过 bootstrap 或一键
安装时会从 `.env` 自动生成该文件；无默认档案时 worker 明确失败，不回退 `.env`。

**不要**在 `.env` 中设置 `NEWS_DATA_DIR`、`NEWS_OUTPUT_PATH`、`NEWS_DATABASE_PATH`：
这三项已在 worker 镜像内固定为卷挂载点（`/data`、`/site`），env_file 会覆盖镜像 ENV，
覆盖后进程会往只读根文件系统写入而失败。

## 4. 固定镜像 digest

**已有实例升级不得先改 live compose。** 推荐直接使用 bootstrap：它只渲染候选 compose，
先按旧 live compose 保持现有 timer，再拉取新 digest、完成迁移前备份，最后才切换。
确需手工升级时，必须先冻结数据库写入：

```bash
cd /srv/news-digest
sudo systemctl stop news-digest.timer
if sudo systemctl is-active --quiet news-digest.service; then
  echo 'worker 仍在运行；等待其结束后重新执行本步骤' >&2
  exit 1
fi
sudo docker compose stop admin
```

保持三处旧 digest 不变，按 §9 使用旧 worker 镜像完成迁移前 SQLite online backup，核验
`PRAGMA integrity_check` 与 SHA-256 后，才可继续编辑 live compose。备份失败时应恢复旧
Admin 与 timer 并终止升级。首次安装不存在旧 timer、Admin 和数据卷，可直接执行下文。

编辑 `/srv/news-digest/compose.yaml`，替换三处 `image:`：worker 与 admin 两处使用同一个
worker digest，web 使用 web digest；三处必须来自同一 Release：

```yaml
services:
  worker:
    image: ghcr.io/OWNER/news-digest-worker@sha256:DIGEST
  web:
    image: ghcr.io/OWNER/news-digest-web@sha256:DIGEST
  admin:
    image: ghcr.io/OWNER/news-digest-worker@sha256:DIGEST
```

为什么必须 digest：tag 可被覆盖重推，digest 不可变；回滚也依赖"改回上一条 digest"这一动作。

## 5. GHCR 登录并拉取（服务器只读凭据）

1. 在 GitHub 生成只读 token：Settings → Developer settings → Personal access tokens，
   权限只勾 `read:packages`（服务器永远不需要写权限）。
2. 登录并拉取：

```bash
echo '（token 内容）' | sudo docker login ghcr.io -u GHCR_USER --password-stdin
sudo chmod 600 /root/.docker/config.json   # login 会把凭据以可逆形式存这里，收紧权限
cd /srv/news-digest
sudo docker compose pull
```

## 6. 首次启动与验证

启动 web/Admin 后跑一次 worker；`/healthz` 不依赖 `current`，首刊生成期间可正常探活：

重复部署时，bootstrap 会先通过应用自身的 release resolver 校验 `/site/current`。若安全 manifest
的刊期等于 `Asia/Shanghai` 当天，则跳过整条 worker 流水线，不再重复抓取、翻译、构建或投递；
当天尚无正式刊物时才运行首刊。

已有 `news-digest_news-data` 卷时，bootstrap 会在启动 Admin/worker 前使用已拉取的 worker
镜像执行 SQLite online backup：源库按只读 URI 打开，备份通过 `PRAGMA integrity_check`
后以唯一文件名和 0600 权限落到 `/srv/news-digest/backups/`，并生成 SHA-256 校验文件。
无卷或无有效 `news.db` 时明确跳过；备份、完整性或校验失败会停止部署。未运行 bootstrap
的手工部署也必须先完成同等的一致性备份，不得直接让新镜像打开旧数据库。

```bash
cd /srv/news-digest
sudo docker compose up -d web admin            # admin 为配置与投递面板常驻服务（§13）
sudo docker compose run --rm worker            # 仅当天尚无正式刊物时手工执行
curl -fsS http://127.0.0.1:8618/healthz        # 期望输出 ok
curl -fsS http://127.0.0.1:8618/ | head -5     # 期望看到站点 HTML
curl -fsS http://127.0.0.1:8619/admin/ | head -3   # 期望看到登录页 HTML（认证在应用层，回环直连同样要登录）
sudo docker compose ps                         # web 应为 healthy，admin 应为 running
sudo systemctl start news-digest.timer         # 已有实例手工升级：恢复先前暂停的 timer
```

顺带核对安全基线与内存：

```bash
sudo docker inspect --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.Memory}}' news-digest-web-1
sudo docker stats --no-stream                  # web 常驻目标 < 15 MB
sudo docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' news-digest-web-1   # 应等于发布提交 sha
```

## 7. 安装 systemd 定时任务

```bash
sudo cp news-digest.service news-digest.timer /etc/systemd/system/
command -v docker    # 若不是 /usr/bin/docker，同步修改 service 中 ExecStart 的绝对路径
sudo systemctl daemon-reload
sudo systemctl enable --now news-digest.timer
systemctl list-timers news-digest.timer        # 核对下次触发时间为 08:00（Asia/Shanghai）
sudo systemctl start news-digest.service       # 手动触发一次，验证 timer→service→容器链路
journalctl -u news-digest.service -n 50        # 查看运行日志
```

注意：service 单元本身没有 [Install] 段，只 enable timer，不 enable service。

## 8. 宿主机 Nginx 与 HTTPS

1. 访问保护现状（用户决定 2026-07-26）：站点公开、不启用站级 Basic Auth；
   `/admin/` 管理面板必须登录（安全底线：能改生产配置的入口必须有认证）。
   认证由面板自身的网页登录页承担（会话 Cookie），nginx 层不配置 Basic Auth；
   口令哈希 `/srv/news-digest/config/htpasswd-admin`（root:600，nginx 不读取）由
   bootstrap.sh 首次生成，初始口令写入 `config/admin-password.initial`（见 §13）。
   手工生成哈希：`printf 'admin:%s\n' "$(openssl passwd -apr1 '口令')" | sudo tee /srv/news-digest/config/htpasswd-admin`，
   随后 `sudo chown root:root` 并 `sudo chmod 600` 该文件。
2. 准备 certbot webroot 并临时上线仅 80 的配置（443 块引用的证书还不存在，直接放会导致 `nginx -t` 失败）：

```bash
sudo mkdir -p /var/www/certbot
sudo cp news.conf /etc/nginx/conf.d/news.conf
sudoedit /etc/nginx/conf.d/news.conf   # 用编辑器把第二个 server 块（443 那一整块，从 server { 到配对的 }）整体注释
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d news.cheapcoding.top
```

3. 证书就绪后恢复完整配置并重载：

```bash
sudo cp news.conf /etc/nginx/conf.d/news.conf   # 还原未注释版本
sudo nginx -t && sudo systemctl reload nginx
```

4. 验证（本机或任意外部机器）：

```bash
curl -sI http://news.cheapcoding.top/ | head -3          # 301 → https
curl -sI https://news.cheapcoding.top/ | grep -iE 'x-robots|content-security|x-content-type|referrer'
curl -sI https://news.cheapcoding.top/privacy/ | head -3
```

5. 安装「续期后重载 nginx」钩子。certbot 的 timer 会自动续期，但默认**不会**让 nginx 加载新证书——
   不装这个钩子，约 90 天后旧证书到期即 HTTPS 静默失效。一键部署脚本会自动安装；手工部署需自己装一次：

```bash
sudo install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nnginx -t && systemctl reload nginx\n' | \
  sudo tee /etc/letsencrypt/renewal-hooks/deploy/10-reload-nginx.sh
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/10-reload-nginx.sh
```

6. 验证自动续期与重载都就位（不改动真实证书）：

```bash
systemctl list-timers | grep certbot          # 续期 timer 已挂上
sudo certbot renew --dry-run                   # 演练一次完整续期，应无报错
sudo /etc/letsencrypt/renewal-hooks/deploy/10-reload-nginx.sh   # 直接执行钩子本体，验证 reload 生效
```

   注意：`--dry-run` **不会**执行 deploy 钩子（certbot 明确在演练时跳过），所以钩子要单独跑一次验证；
   若 certbot 版本支持，也可用 `sudo certbot renew --dry-run --run-deploy-hooks` 合并演练。webroot 方式续期无需停站。

## 9. 备份

- **配置**：`/srv/news-digest/config/`（`.env`、`providers.json` 供应商档案含密钥、
  `htpasswd-admin` 面板口令哈希；其中 `session-secret` 与 `admin-password.initial`
  可再生，不必备份）、`compose.yaml`（连同其中的 digest）、两个 systemd 单元、`news.conf`。
- **迁移前 SQLite 一致性备份**：bootstrap 在任何新 Admin/worker 启动前使用 SQLite
  online backup API 备份 `news-digest_news-data` 中的 `news.db`，验证完整性并生成同名
  `.sha256` 文件；两者均为 `root:root/0600`。该备份是 schema 自动迁移前的人工恢复点。
- **停写后的整卷归档**（SQLite、翻译缓存、站点归档）：以下 tar 仅适用于 Admin 与 worker 停止写入后的整卷归档，
  不替代迁移前 SQLite online backup：

```bash
# docker run 的 -v 绑定源必须是绝对路径，故直接写全路径，不依赖当前目录
sudo docker run --rm -v news-digest_news-data:/data:ro -v /srv/news-digest/backups:/backup \
  alpine:3.20 tar czf /backup/news-data-$(date +%F).tar.gz -C /data .
sudo docker run --rm -v news-digest_news-site:/site:ro -v /srv/news-digest/backups:/backup \
  alpine:3.20 tar czf /backup/news-site-$(date +%F).tar.gz -C /site .
```

- **发布台账**：每次部署把日期、tag、两条 digest 追加到 `/srv/news-digest/backups/DEPLOYED.log`，
  回滚时从这里取上一条 digest。

## 10. 回滚

前提：台账里始终留有上一版 digest（第 9 步）。

```bash
cd /srv/news-digest
sudoedit compose.yaml                 # 把三处 image 的 digest 改回上一条（worker 与 admin 共用 worker 镜像引用，须一并改）
sudo docker compose pull
sudo docker compose up -d web admin   # web 与面板立即回滚
# worker 无需额外操作：下次 timer 触发即按 compose 中的（旧）digest 运行
```

注意（PLAN §10）：镜像回滚不会自动恢复数据库，数据库 schema 只前滚。若旧镜像不兼容
当前 schema，必须先停止 Admin/worker，由操作员人工选择指定的迁移前备份，核验对应
SHA-256 与 `PRAGMA integrity_check` 后再手工恢复；不得自动选择“最新”备份，也不得直接
覆盖仍在线的 `news.db`。

## 11. 日常观察

```bash
journalctl -u news-digest.service --since today     # 每日任务结果（退出码非 0 即失败）
sudo docker compose -f /srv/news-digest/compose.yaml logs web --tail 50
sudo docker compose -f /srv/news-digest/compose.yaml ps   # web 应 healthy
```

容器日志已限量轮转（json-file 10m x 3）；宿主机 Nginx 日志走发行版自带 logrotate。
验收要求连续观察至少两个生成周期：单次失败不得破坏上一版站点（`current` 符号链接
只在构建成功后原子切换，失败时保持旧版在线）。

## 12. 需服务器实测后才能定稿的事项

- CPU 架构与 `release.yml` 的 `PLATFORMS` 是否一致（不一致须改后重新发布镜像）。
- Docker / Compose / systemd / nginx 实际版本是否满足第 2 步下限；`http2 on;` 是否可启用。
- `docker` 可执行文件绝对路径（service 单元 ExecStart）。
- worker / web / admin 实测内存峰值，决定 256m / 32m / 32m 上限是否调整。
- `run --yes` 四阶段退出码传播：构建成功、投递失败时站点保留且 service 为非零；
  `EMAIL_DELIVERY_ENABLED=false` 时明确跳过并返回成功。
- `Persistent=true` 的 08:00 补跑是否落在 `EMAIL_CATCHUP_WINDOW_HOURS` 内；窗口外不得自动补发。
- Admin 的 host 网络回环监听、登录与改密、`news-data:/data` 和 `news-site:/site:ro`
  挂载，以及预览/指定刊期投递读取同一 release manifest。
- 在 Admin 中由用户明确确认一次真实 provider 固定 `Hi` 测试、SMTP 连接测试和测试邮件；
  默认离线验收不访问这些真实服务。
- 公开订阅仅在 HTTPS、SMTP 和 `EMAIL_DELIVERY_ENABLED=true` 均就绪时开启；验证
  double opt-in、`/privacy/`、RFC 8058 one-click 退订及 token 路径不写 access log。
- 首次 `docker compose run` 的 GHCR 拉取、DNS、出网代理等网络实况。

## 13. Admin 管理面板

生产环境管理翻译供应商、邮件与订阅的常驻网页面板，由 compose 的
`admin` 服务提供（复用 worker 镜像，`news-digest admin`，只监听宿主机
`127.0.0.1:8619`），公网入口唯一经 nginx 的 `/admin/`。

- **访问**：`https://news.cheapcoding.top/admin/`，面板自带网页登录页
  （用户名默认 `admin`，登录后发放会话 Cookie——不再是浏览器 Basic Auth 弹窗）。
  首次口令由 bootstrap 写入服务器 `/srv/news-digest/config/admin-password.initial`
  （`sudo cat` 查看；口令不出现在部署输出/日志里）。
- **卷权限**：Admin 因 `/config/.env` 等 `root:600` 密钥文件使用 root UID；共享 GID 固定为
  worker 的 `10001`，并以 `umask 0002` 创建 `/data` 内 SQLite/journal，避免首次由
  Admin 建库后阻断非 root worker。不得移除 Compose 中这两个约束。
- **改口令**：登录后在面板网页修改。修改成功会轮换会话密钥（所有已登录端强制
  重新登录）并自动删除 `admin-password.initial`。忘记口令：
  `sudo rm /srv/news-digest/config/htpasswd-admin /srv/news-digest/config/session-secret`
  后重跑 bootstrap 重新生成初始口令。
- **供应商**：新增/编辑档案，显式选择 `openai_chat` 或 `anthropic_messages`、模型和
  stream 模式；只有启用档案能设为唯一默认。默认档案是每日翻译权威源，无默认时
  worker 明确失败，不回退到残留 `.env`。设默认前若未测试或结果过期，Admin 强确认。
- **测试连接**：确认后只发送一次固定 `Hi`、2 字符输入、最多 8 output tokens 的真实
  生成请求，可能计费；正式翻译与测试复用同一 URL/header/payload/parser adapter。
- **邮件与订阅**：配置 SMTP、订阅名单和内容组合，执行不发信的连接测试、需确认的测试
  邮件、预览、指定刊期人工投递及 failed/unknown 状态处理；公开订阅默认关闭。
- **正式邮件语义**：刊物与刊物测试邮件仅发送 UTF-8 `text/plain`，Admin 的 HTML 仅用于页面预览，不进入 SMTP；正式订阅刊物同时带 `List-Unsubscribe` 与 RFC 8058 one-click 头。SMTP 部分拒收逐人记
  `failed`，DATA 后断连等可能已送达情形记 `unknown` 且不自动重试；全部拒收或归档失败
  使本次 run 失败，归档失败不回滚已经成功的收件人状态。
- **开启公开订阅**：完成 HTTPS、SMTP、发件身份、测试邮件和 `/privacy/` 核验后，在
  `config/.env` 设置 `PUBLIC_SUBSCRIPTION_ENABLED=true`；提交端点逐请求读取该开关，Admin
  无需重启。再执行 `docker compose run --rm worker build` 让首页生成订阅表单。Admin
  不管理该生产就绪门。
- **密钥展示保护**：页面与接口响应不返回密钥；密钥经 HTTPS + 登录会话提交，
  只落 `${APP_DIR}/config/providers.json`（root:10001，权限 640）。也可登录服务器直接编辑该
  文件，保存后刷新面板即可见，同样无需重启。格式：

```json
{
 "providers": {
  "default": {
   "base_url": "https://网关地址/v1",
   "api_key": "密钥",
   "model": "模型名",
   "api_type": "openai_chat",
   "stream": true,
   "enabled": true,
   "is_default": true
  }
 }
}
```

- **首个档案**：bootstrap 第 4/10 步在 `providers.json` 不存在时自动从已填好的
  `.env` 生成（名为 `default`）；已存在则永不覆盖。
- **配置所有权**：首次安装可从本地 `.env.local` 初始化服务器配置；服务器
  `config/.env` 一旦存在，后续 `deploy-all` 不再覆盖。Admin/operator 修改立即成为
  权威运行时配置，镜像部署与运行时配置生命周期分离。
- **翻译缓存隔离**：缓存 identity 包含协议、规范化 Base URL 和模型，不含 key；
  切换协议或供应商不会误用旧缓存。
