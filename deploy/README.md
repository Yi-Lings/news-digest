# 部署工件与服务器侧操作步骤（阶段 7）

> **重要声明：本目录（含 `.github/workflows/release.yml`）全部为源码侧模板，
> 在目标服务器上实际执行并验证之前，一律不视为最终版。**
> 任何与服务器实测冲突之处，以实测结果为准并回写本目录。

服务器上不保存 Git 仓库与源码（PLAN 发布链路）：应用代码只存在于 GHCR 预构建镜像内，
服务器只保留 Compose manifest、`.env`、systemd 单元、宿主机 Nginx 配置与持久化卷。

## 一键部署（类 sub2api）

新服务器上一条命令完成部署（root 执行）：

```bash
export GH_TOKEN=你的repo只读token   # 仓库转公开后无需此行与认证头
bash <(curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
  https://raw.githubusercontent.com/Yi-Lings/news-digest/main/deploy/install.sh)
```

自动完成：下载最新 Release 部署包 → 只读体检（preflight）→ 幂等部署（bootstrap：镜像、
web 容器、每日 08:00 定时器、Nginx、HTTPS）。首次运行会在 `/srv/news-digest/.env`
生成密钥模板并暂停，在服务器上填好真实值后重跑同一命令续接。部署包
（`news-digest-deploy.tgz`）由 CI 在每次推送 `v*` 标签时自动附到 Release；
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
换域名/换端口/换目录部署，不改任何脚本；未设置时使用默认值（即本项目生产实值）。
占位符替换与域名/端口渲染均由 bootstrap 按这些变量自动完成。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `ND_OWNER` | `yi-lings` | GHCR 命名空间；渲染 compose 中三处 image 引用（worker/admin 共用一处值） |
| `ND_VERSION` | `v0.6.0rc2` | 部署候选 tag；转正式版后仍按 §4 固定 digest |
| `ND_APP_DIR` | `/srv/news-digest` | 部署目录（compose、`.env`、`providers.json`、备份）；同步渲染 systemd 单元与 admin 挂载 |
| `ND_DOMAIN` | `news.cheapcoding.top` | 站点域名；渲染 nginx 配置、certbot 签发与 `.env` 模板的 `NEWS_SITE_URL` |
| `ND_WEB_PORT` | `8618` | web 容器宿主回环端口；渲染 compose 与 nginx |
| `ND_ADMIN_PORT` | `8619` | 模型切换面板宿主回环端口；渲染 compose 与 nginx |
| `ND_CERTBOT_EMAIL` | `1481835649@qq.com` | 证书到期/吊销通知邮箱 |

示例：`export ND_DOMAIN=news.example.com ND_WEB_PORT=9000` 后执行一键部署命令。
后文手工步骤（§3、§6、§8 等）中的路径与端口均按默认值书写。

## 1. 发布一个部署候选（本地 → GHCR）

1. 本地验收通过后，确认 `src/news_digest/__init__.py` 的 `__version__` 与将要打的 tag 一致
   （CI 会强制校验，不一致直接失败）。
2. 打 tag 并推送：`git tag v0.1.0 && git push origin v0.1.0`。
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
systemd-analyze calendar '*-*-* 06:30:00 Asia/Shanghai'   # 能解析并给出下次触发时间即可
nginx -v                    # >= 1.25.1 才能启用 news.conf 中注释的 `http2 on;`
dig +short news.cheapcoding.top   # 必须指向本服务器公网 IP
free -m && df -h /          # 内存、磁盘余量核对（worker 峰值目标 < 150 MB）
```

## 3. 创建部署目录与配置

```bash
sudo mkdir -p /srv/news-digest/backups
sudo cp compose.yaml /srv/news-digest/compose.yaml     # 从本目录 scp 上传后拷贝
sudo touch /srv/news-digest/.env
sudo chown root:root /srv/news-digest/.env
sudo chmod 600 /srv/news-digest/.env                   # 密钥文件：600，root 所有
```

编辑 `/srv/news-digest/.env`（生产值示例；密钥只在服务器上出现，绝不进 Git / CI / 镜像）：

```dotenv
NEWS_ENV=production
NEWS_SITE_URL=https://news.cheapcoding.top
NEWS_TIMEZONE=Asia/Shanghai
NEWS_FETCH_WINDOW_HOURS=24

TRANSLATION_API_BASE_URL=（SUB2API 地址）
TRANSLATION_API_KEY=（密钥）
TRANSLATION_MODEL=（模型名）

SMTP_HOST=（现有 SMTP）
SMTP_PORT=465
SMTP_USERNAME=（账号）
SMTP_PASSWORD=（密码）
SMTP_FROM=（发件地址）
SMTP_RECIPIENTS=（收件地址，逗号分隔）
SMTP_USE_TLS=true
```

**不要**在 `.env` 中设置 `NEWS_DATA_DIR`、`NEWS_OUTPUT_PATH`、`NEWS_DATABASE_PATH`：
这三项已在 worker 镜像内固定为卷挂载点（`/data`、`/site`），env_file 会覆盖镜像 ENV，
覆盖后进程会往只读根文件系统写入而失败。

## 4. 固定镜像 digest

编辑 `/srv/news-digest/compose.yaml`，把两个 `image:` 行替换为第 1 步记录的 digest 引用：

```yaml
    image: ghcr.io/OWNER/news-digest-worker@sha256:DIGEST
    image: ghcr.io/OWNER/news-digest-web@sha256:DIGEST
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

顺序有讲究：先跑一次 worker 生成首版站点，再常驻 web（web 在 `current` 出现前只有 /healthz 可用）：

```bash
cd /srv/news-digest
sudo docker compose run --rm worker            # 首次全量：抓取→选题→翻译→构建；观察输出
sudo docker compose up -d web admin            # admin 为模型切换面板常驻服务（§13）
curl -fsS http://127.0.0.1:8618/healthz        # 期望输出 ok
curl -fsS http://127.0.0.1:8618/ | head -5     # 期望看到站点 HTML
curl -fsS http://127.0.0.1:8619/admin/ | head -3   # 期望看到面板 HTML（回环直连不经认证）
sudo docker compose ps                         # web 应为 healthy，admin 应为 running
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
systemctl list-timers news-digest.timer        # 核对下次触发时间为次日 06:30（Asia/Shanghai）
sudo systemctl start news-digest.service       # 手动触发一次，验证 timer→service→容器链路
journalctl -u news-digest.service -n 50        # 查看运行日志
```

注意：service 单元本身没有 [Install] 段，只 enable timer，不 enable service。

## 8. 宿主机 Nginx 与 HTTPS

1. 访问保护现状（用户决定 2026-07-26）：站点公开、不启用站级 Basic Auth；
   `/admin/` 模型切换面板独立口令（安全底线：能改生产配置的入口必须有认证）。
   口令文件 `/etc/nginx/htpasswd-news-admin` 由 bootstrap.sh 自动生成并只打印一次；
   手工生成：`printf 'admin:%s\n' "$(openssl passwd -apr1 '口令')" | sudo tee /etc/nginx/htpasswd-news-admin`，
   属组给 nginx 运行组（Debian 系 www-data）、权限 640。
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
curl -sI -u 用户名 https://news.cheapcoding.top/ | grep -iE 'x-robots|content-security|x-content-type|referrer'
```

5. 确认 certbot 自动续期已挂上（`systemctl list-timers | grep certbot`），webroot 方式续期无需停站。

## 9. 备份

- **配置**：`/srv/news-digest/.env`、`providers.json`（供应商档案，含密钥）、`compose.yaml`
  （连同其中的 digest）、两个 systemd 单元、`news.conf`、`/etc/nginx/htpasswd-news-admin`（面板口令）。
- **数据卷**（SQLite、翻译缓存、站点归档）：

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

注意（PLAN §10）：回滚镜像不等于回滚数据库。数据库 schema 只前滚；
若新版本改过 schema，先确认旧代码兼容当前数据库，再考虑镜像回滚，不得盲目回退 `news.db` 文件。

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
- 邮件发送编排：`run --yes` 不发信；确认 SMTP 后另行决定是否在 service 中追加
  `docker compose run --rm worker send-email --yes`（或并入每日任务），属部署阶段决策。
- 生产模型切换面板已并入本批工件（compose `admin` 服务 + news.conf `/admin/` +
  bootstrap 第 4、8 两步；访问保护决策已定：站点公开、面板独立口令，见 §8）。
  服务器实测项：host 网络下面板对宿主回环端口的监听、Basic Auth 登录、
  面板切换后下一次 timer 任务确实使用新配置。
- 首次 `docker compose run` 的 GHCR 拉取、DNS、出网代理等网络实况。

## 13. 模型切换面板

生产环境切换翻译供应商（base_url / model / key）的常驻网页面板，由 compose 的
`admin` 服务提供（复用 worker 镜像，`news-digest admin`，只监听宿主机
`127.0.0.1:8619`），公网入口唯一经 nginx 的 `/admin/`。

- **访问**：`https://news.cheapcoding.top/admin/`（Basic Auth 用户名 `admin`，
  口令在 bootstrap 第 8/10 步首次生成时打印一次；遗失后重置：
  `sudo rm /etc/nginx/htpasswd-news-admin` 再重跑 bootstrap）。
- **能做什么**：在既有供应商档案间切换、修改档案的接口地址与模型名。
  点「启用」即把三个 `TRANSLATION_*` 写入 `/srv/news-digest/.env`——
  **无需重启任何容器**：worker 是一次性任务，每次由 timer 经 `docker compose run`
  拉起时重读 `.env`，下一期即生效。
- **密钥不经网页**（生产模式硬约束）：页面永远只显示掩码，新增供应商 = 登录服务器
  编辑 `${APP_DIR}/providers.json`（root:600；默认 `/srv/news-digest/providers.json`），
  保存后刷新面板即可见，同样无需重启。格式：

```json
{
 "active": "default",
 "providers": {
  "default": {"base_url": "https://网关地址/v1", "api_key": "密钥", "model": "模型名"},
  "backup":  {"base_url": "https://备用网关/v1", "api_key": "密钥", "model": "模型名"}
 }
}
```

- **首个档案**：bootstrap 第 4/10 步在 `providers.json` 不存在时自动从已填好的
  `.env` 生成（名为 `default`）；已存在则永不覆盖。
- **翻译缓存按模型隔离**：切换供应商互不污染，切回旧供应商时既有缓存继续命中。
