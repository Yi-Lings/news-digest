#!/usr/bin/env bash
# bootstrap.sh —— news-digest 阶段 7 服务器幂等部署（可反复执行直至全部就绪）
# 红线（PLAN 阶段 7）：
#   - 服务器不检出源码、不构建镜像：只消费 GHCR 已发布镜像与随本脚本上传的工件；
#   - 密钥只在服务器端注入：本脚本不经参数/标准输入接收任何密钥，.env 由用户
#     登录服务器后用编辑器填写（因此第 3/5 步会主动停下，填好后重跑即可）。
# 参数化：变量区所有值均可在执行前 export ND_* 环境变量覆盖（全表见 install.sh
# 头注释或 deploy/README.md §0），换域名/端口/目录部署不用改脚本；默认值即本项目生产实值。
# 用法：由 server-push.ps1 上传到 /srv/news-digest/incoming 后，以 root 执行。
# 退出码：0 完成；2 等待人工填写 .env；3 等待人工 docker login；1 其他错误。
set -euo pipefail

# ---------------- 变量区（VAR="${ND_XXX:-默认值}"，执行前 export ND_XXX 即可覆盖）----------------
OWNER="${ND_OWNER:-yi-lings}"             # GHCR 命名空间（必须全小写）
TAG="${ND_VERSION:-v0.6.0rc3}"            # 部署候选 tag；转正式版后按 README §4 固定 digest
APP_DIR="${ND_APP_DIR:-/srv/news-digest}" # 部署目录（compose、config/、备份）
CONFIG_DIR="${APP_DIR}/config"            # 密钥配置子目录：admin 容器唯一 bind 挂载的宿主路径
DOMAIN="${ND_DOMAIN:-news.cheapcoding.top}"
WEB_PORT="${ND_WEB_PORT:-8618}"           # web 宿主回环端口（服务器 8080 已被既有服务占用）
ADMIN_PORT="${ND_ADMIN_PORT:-8619}"       # 模型切换面板宿主回环端口
CERTBOT_EMAIL="${ND_CERTBOT_EMAIL:-1481835649@qq.com}"
CERT_DIR="/etc/letsencrypt/live/${DOMAIN}"
NGINX_CONF="/etc/nginx/conf.d/news.conf"
HTPASSWD_FILE="${CONFIG_DIR}/htpasswd-admin"     # 面板登录口令哈希（面板登录页校验；nginx 不读取）
WEBROOT="/var/www/certbot"                # certbot webroot 验证目录（news.conf 同路径）
WORKER_IMAGE="ghcr.io/${OWNER}/news-digest-worker:${TAG}"
WEB_IMAGE="ghcr.io/${OWNER}/news-digest-web:${TAG}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 上传工件所在目录（incoming）
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

section() { printf '\n=== %s ===\n' "$1"; }
warnbox() { printf '\n!!! 警告：%s\n\n' "$1"; }
die()     { printf '\n错误：%s\n' "$1" >&2; exit 1; }

# 幂等就位：内容相同则跳过；目标已存在且不同则先备份为 .bak-时间戳再覆盖。
# 为什么备份：nginx 配置与 compose.yaml（可能已手工固定 digest）都不允许被静默丢弃。
install_file() {  # $1=源 $2=目标 $3=权限
  local src="$1" dst="$2" mode="$3"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    echo "未变化，跳过：$dst"
    return 0
  fi
  if [ -f "$dst" ]; then
    cp -a "$dst" "${dst}.bak-${STAMP}"
    echo "已备份原文件：${dst}.bak-${STAMP}"
  fi
  install -m "$mode" "$src" "$dst"
  echo "已安装：$dst"
}

# ---------------------------------------------------------------
section "1/10 前置校验（root、Docker、上传工件）"
[ "$(id -u)" -eq 0 ] || die "必须以 root 执行（当前 uid=$(id -u)）"
docker info >/dev/null 2>&1 || die "Docker 守护进程不可用——先安装/启动 Docker 再重跑"
docker compose version >/dev/null 2>&1 || die "缺少 Compose v2（docker compose 子命令）"
for f in compose.yaml news-digest.service news-digest.timer news.conf; do
  [ -f "${SRC_DIR}/${f}" ] || die "缺少上传工件：${SRC_DIR}/${f}（应由 server-push.ps1 一并上传）"
done
echo "校验通过：root、Docker、Compose 与上传工件齐备"

# ---------------------------------------------------------------
section "2/10 目录与配置工件就位"
# backups 为 README §9 备份与回滚台账所依赖；mail 预留邮件归档
install -d -m 755 "$APP_DIR" "$APP_DIR/mail" "$APP_DIR/backups"

# compose.yaml：先在临时文件里渲染，再比对就位——重跑时内容一致即跳过（幂等）。
# 渲染内容：OWNER/VERSION 占位符（worker 镜像两处：worker 与 admin 服务共用同一
# 引用，sed 逐行匹配自动同时替换）、部署目录（env_file 与 admin 的 /config bind
# 挂载）、web 宿主端口、admin 监听端口（admin 用 host 网络，command 的 --port
# 即宿主端口）。若现有文件手工固定过 digest，会先备份再覆盖。
sed -e "s|ghcr.io/OWNER/news-digest-worker:VERSION|${WORKER_IMAGE}|" \
    -e "s|ghcr.io/OWNER/news-digest-web:VERSION|${WEB_IMAGE}|" \
    -e "s|/srv/news-digest|${APP_DIR}|g" \
    -e "s|127.0.0.1:8618:|127.0.0.1:${WEB_PORT}:|" \
    -e "s|\"--port\", \"8619\"|\"--port\", \"${ADMIN_PORT}\"|" \
    "${SRC_DIR}/compose.yaml" > "${TMP_DIR}/compose.yaml"
if [ -f "${APP_DIR}/compose.yaml" ] && ! cmp -s "${TMP_DIR}/compose.yaml" "${APP_DIR}/compose.yaml"; then
  echo "注意：现有 compose.yaml 内容不同，将被本次工件覆盖（原文件自动备份；若曾固定 digest 请事后核对）"
fi
install_file "${TMP_DIR}/compose.yaml" "${APP_DIR}/compose.yaml" 644

# service 单元的 ExecStart 写死部署目录：模板文件保持默认值不动，安装时按 APP_DIR
# 渲染——换目录部署时 timer 触发的任务才能找到 compose.yaml（timer 无路径，无需渲染）
sed -e "s|/srv/news-digest|${APP_DIR}|g" \
    "${SRC_DIR}/news-digest.service" > "${TMP_DIR}/news-digest.service"
install_file "${TMP_DIR}/news-digest.service" /etc/systemd/system/news-digest.service 644
install_file "${SRC_DIR}/news-digest.timer"   /etc/systemd/system/news-digest.timer   644
# nginx 的 news.conf 留到第 9 步按证书状态选版本就位：证书未签发时提前放完整版
# 会让全局 nginx -t 失败，殃及主站与 SUB2API 的后续 reload。

# service 单元的 ExecStart 写死 /usr/bin/docker；路径不符会导致 timer 触发即失败
DOCKER_BIN="$(command -v docker)"
if [ "$DOCKER_BIN" != "/usr/bin/docker" ]; then
  warnbox "docker 实际路径为 ${DOCKER_BIN}，与 service 单元中 /usr/bin/docker 不符——请修改 /etc/systemd/system/news-digest.service 后再 daemon-reload"
fi

# ---------------------------------------------------------------
section "3/10 生产密钥文件 .env"
# 配置收窄（rc2 安全整改）：可被面板读写的配置全部集中到 config/ 子目录，admin
# 容器只 bind 挂载该子目录——面板即使被攻破也触不到 compose.yaml 与运维脚本。
mkdir -p "$CONFIG_DIR"
chown root:root "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
# 幂等迁移：旧位置存在且新位置尚无同名文件才 mv（mv 保留属主与权限）；重跑自动跳过
migrate_cfg() {  # $1=旧路径 $2=新路径
  if [ -e "$1" ] && [ ! -e "$2" ]; then
    mv "$1" "$2"
    echo "已迁移旧配置：$1 -> $2"
  fi
}
migrate_cfg "${APP_DIR}/.env"               "${CONFIG_DIR}/.env"
migrate_cfg "${APP_DIR}/providers.json"     "${CONFIG_DIR}/providers.json"
migrate_cfg /etc/nginx/htpasswd-news-admin  "${CONFIG_DIR}/htpasswd-admin"

ENV_FILE="${CONFIG_DIR}/.env"
if [ ! -f "$ENV_FILE" ]; then
  # 只写模板与注释，不含任何真实密钥。定界符不加引号：要展开 ${ENV_FILE}/${DOMAIN}
  # 两个部署参数；模板正文不含其他 $，无意外展开风险
  cat > "$ENV_FILE" <<ENVEOF
# ${ENV_FILE} —— 生产密钥与配置（root:root，权限 600）
# 红线：真实值只在本文件出现，绝不进 Git / CI / 镜像 / 脚本参数。
# 注意：不要设置 NEWS_DATA_DIR / NEWS_OUTPUT_PATH / NEWS_DATABASE_PATH——
#       三者已在镜像内固定为卷挂载点（/data、/site），覆盖会写只读路径导致任务失败。

NEWS_ENV=production
NEWS_SITE_URL=https://${DOMAIN}
NEWS_TIMEZONE=Asia/Shanghai
NEWS_FETCH_WINDOW_HOURS=24

# ---- 翻译网关（SUB2API），三项均必填 ----
TRANSLATION_API_BASE_URL=
TRANSLATION_API_KEY=
TRANSLATION_MODEL=

# ---- SMTP 发信（RECIPIENTS 逗号分隔）----
SMTP_HOST=
SMTP_PORT=465
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_RECIPIENTS=
SMTP_USE_TLS=true

# ---- 出网代理：本服务器直连，不需要代理，保持留空 ----
NEWS_HTTP_PROXY=
ENVEOF
  chown root:root "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  cat <<STOPEOF

>>> 需要人工操作：填写生产密钥 <<<
已生成模板 ${ENV_FILE}（root:600）。请在服务器上：
  1. 用编辑器填入真实值，例如：nano ${ENV_FILE}
  2. 保存后重新执行本脚本：bash ${SRC_DIR}/bootstrap.sh
本脚本不经参数或标准输入接收密钥，只能人工落盘后重跑。
STOPEOF
  exit 2
fi
# 幂等收紧：无论谁动过，权限始终回到 root:600
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"
if ! grep -qE '^TRANSLATION_API_KEY=.+' "$ENV_FILE"; then
  warnbox ".env 中 TRANSLATION_API_KEY 仍为空——worker 首刊会失败，请尽快填写"
fi
echo ".env 已存在（权限已确认 600），继续"

# ---------------------------------------------------------------
section "4/10 模型供应商档案 providers.json"
# 模型切换面板（/admin/）在既有档案间切换；生产模式密钥不经网页传输，
# 新增供应商 = 登录服务器直接编辑本文件（格式见 deploy/README.md §13）。
PROVIDERS_FILE="${CONFIG_DIR}/providers.json"
if [ -f "$PROVIDERS_FILE" ]; then
  # 幂等收紧权限；内容绝不覆盖——面板或手工编辑过的档案是运行时状态，不是部署工件
  chown root:root "$PROVIDERS_FILE"
  chmod 600 "$PROVIDERS_FILE"
  echo "providers.json 已存在（权限已确认 600），跳过生成"
else
  env_get() { sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | head -n1; }
  # 极简 JSON 转义：URL/密钥/模型名里按理不会出现 " 或 \，防御性处理一下
  json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
  P_URL="$(env_get TRANSLATION_API_BASE_URL)"
  P_KEY="$(env_get TRANSLATION_API_KEY)"
  P_MODEL="$(env_get TRANSLATION_MODEL)"
  if [ -n "$P_URL" ] && [ -n "$P_KEY" ] && [ -n "$P_MODEL" ]; then
    # 用 .env 已填好的三项生成首个档案：面板开箱即有 default 可切换
    cat > "$PROVIDERS_FILE" <<PROVEOF
{
 "active": "default",
 "providers": {
  "default": {
   "base_url": "$(json_escape "$P_URL")",
   "api_key": "$(json_escape "$P_KEY")",
   "model": "$(json_escape "$P_MODEL")"
  }
 }
}
PROVEOF
    chown root:root "$PROVIDERS_FILE"
    chmod 600 "$PROVIDERS_FILE"
    echo "已生成首个供应商档案（default，取自 .env）：${PROVIDERS_FILE}"
  else
    # 不中止：面板暂无档案不影响其余部署；填全 .env 后重跑本脚本自动补生成
    warnbox ".env 的 TRANSLATION_* 三项未填全，暂不生成 providers.json——面板将没有可切换档案；填好后重跑本脚本即可"
  fi
fi

# ---------------------------------------------------------------
section "5/10 GHCR 拉取授权检查"
# 用 manifest inspect 轻量探测既有凭据，避免拉大镜像到一半才发现未登录
if docker manifest inspect "$WORKER_IMAGE" >/dev/null 2>&1; then
  echo "GHCR 授权可用：${WORKER_IMAGE} 可见"
  # docker login 会把凭据以可逆形式存在 config.json，顺手收紧权限
  [ -f /root/.docker/config.json ] && chmod 600 /root/.docker/config.json
else
  cat <<LOGINEOF

>>> 需要人工操作：GHCR 登录 <<<
镜像 ${WORKER_IMAGE} 当前不可访问（私有镜像需只读授权）。请在服务器上：
  1. GitHub → Settings → Developer settings → Personal access tokens，
     生成只勾 read:packages 的 token（服务器永远不需要写权限）
  2. 执行：docker login ghcr.io -u ${OWNER}
     （token 在提示时自行粘贴；本脚本不接收、不记录）
  3. 收紧凭据权限：chmod 600 /root/.docker/config.json
  4. 重新执行本脚本：bash ${SRC_DIR}/bootstrap.sh
LOGINEOF
  exit 3
fi

# ---------------------------------------------------------------
section "6/10 拉取镜像、启动 web 与 admin、worker 首刊"
COMPOSE=(docker compose -f "${APP_DIR}/compose.yaml")
"${COMPOSE[@]}" pull
# 先常驻 web 再首刊是安全的：/healthz 不依赖站点内容，current 出现前仅健康检查可用。
# admin（模型切换面板）一并常驻：与 worker 同镜像已随 pull 就绪；up -d 幂等，重跑安全
"${COMPOSE[@]}" up -d web admin
# 首刊用 run --yes：只生成站点不发信（发信编排属部署阶段后续决策，README §12）。
# 失败不中止安装：timer 明日会再跑；但必须显著警告并给出排查入口。
if "${COMPOSE[@]}" run --rm worker run --yes; then
  echo "worker 首刊完成"
else
  warnbox "worker 首刊失败——不影响后续安装，但站点暂无内容。排查：先核对 .env 密钥与翻译网关连通性，再手动重试：docker compose -f ${APP_DIR}/compose.yaml run --rm worker run --yes"
fi

# ---------------------------------------------------------------
section "7/10 systemd 定时任务"
systemctl daemon-reload
# 只 enable timer，不 enable service（service 无 [Install] 段，单元内注释已说明）
systemctl enable --now news-digest.timer
echo "下次触发（NEXT 列）："
systemctl list-timers news-digest.timer --no-pager || true

# ---------------------------------------------------------------
section "8/10 面板登录口令文件"
# 认证已移入应用层：面板自带登录页 + 会话 Cookie（会话密钥 session-secret 由面板
# 自建），nginx 不再读取口令文件。本步只负责首次生成口令哈希（apr1，面板登录页
# 校验）。口令本身不打印到 stdout——部署输出可能被日志/CI 留存，明文只落 600 文件。
if [ -f "$HTPASSWD_FILE" ]; then
  # 幂等收紧：旧部署迁移来的文件可能还是 root:www-data 640（当年供 nginx 读取）
  chown root:root "$HTPASSWD_FILE"
  chmod 600 "$HTPASSWD_FILE"
  echo "已存在，跳过：${HTPASSWD_FILE}"
  echo "（忘记口令时重置：rm ${CONFIG_DIR}/htpasswd-admin ${CONFIG_DIR}/session-secret 后重跑本脚本）"
elif ! command -v openssl >/dev/null 2>&1; then
  warnbox "openssl 不可用，无法生成面板口令文件——/admin/ 将无法登录。安装 openssl 后重跑本脚本"
else
  ADMIN_PASSWORD="$(openssl rand -base64 12)"   # 12 字节 → 恰好 16 位 base64 字符
  INITIAL_PASS_FILE="${CONFIG_DIR}/admin-password.initial"
  # umask 子 shell：两个文件从创建瞬间即 600，不经历宽权限窗口
  (
    umask 077
    # apr1（htpasswd 的 MD5 变体）：面板登录页原生校验此格式，无需安装 httpd-tools
    printf 'admin:%s\n' "$(openssl passwd -apr1 "$ADMIN_PASSWORD")" > "$HTPASSWD_FILE"
    printf '%s\n' "$ADMIN_PASSWORD" > "$INITIAL_PASS_FILE"
  )
  chown root:root "$HTPASSWD_FILE" "$INITIAL_PASS_FILE"
  chmod 600 "$HTPASSWD_FILE" "$INITIAL_PASS_FILE"
  echo "初始口令已写入 ${INITIAL_PASS_FILE}（登录用户名 admin）。"
  echo "登录面板后请立即在网页上修改口令（修改成功会自动删除该文件），"
  echo "或 cat ${INITIAL_PASS_FILE} 查看后手动删除。"
fi

# ---------------------------------------------------------------
section "9/10 宿主机 Nginx 与 HTTPS"
[ -d /etc/nginx/conf.d ] || die "/etc/nginx/conf.d 不存在——请先确认宿主机 nginx 布局（本脚本只新增该目录下的 news.conf）"

# 渲染 nginx 配置：模板内写的是默认域名/端口的真实值（保持模板可读、可独立审阅），
# 此处按本次部署参数替换。默认参数下渲染前后逐字节一致，不影响幂等比对。
render_nginx_conf() {  # $1=源 $2=目标
  sed -e "s|news.cheapcoding.top|${DOMAIN}|g" \
      -e "s|127.0.0.1:8618|127.0.0.1:${WEB_PORT}|g" \
      -e "s|127.0.0.1:8619|127.0.0.1:${ADMIN_PORT}|g" \
      "$1" > "$2"
}

# 就位 + 验证 + 重载；nginx -t 失败立即撤销本次写入——绝不能把坏配置留在
# conf.d 里牵连主站与 SUB2API（哪怕当次没 reload，也会炸掉下一次全局 -t/restart）
install_nginx_conf() {  # $1=准备好的新配置
  local src="$1" bak=""
  if [ -f "$NGINX_CONF" ] && cmp -s "$src" "$NGINX_CONF"; then
    echo "Nginx 配置未变化，跳过 reload"
    return 0
  fi
  if [ -f "$NGINX_CONF" ]; then
    bak="${NGINX_CONF}.bak-${STAMP}"
    cp -a "$NGINX_CONF" "$bak"
    echo "已备份原配置：$bak"
  fi
  install -m 644 "$src" "$NGINX_CONF"
  if ! nginx -t; then
    if [ -n "$bak" ]; then cp -a "$bak" "$NGINX_CONF"; else rm -f "$NGINX_CONF"; fi
    die "nginx -t 未通过，已还原 ${NGINX_CONF} 的本次改动——请检查后重跑"
  fi
  systemctl reload nginx
  echo "Nginx 已 reload：${NGINX_CONF}"
}

# http-only 第一版：仅 80 端口 + ACME 验证路径 + 临时反代。证书出现之前不能
# 引用 443 证书文件，否则全局 nginx -t 直接失败。定界符加引号防 $host 被展开；
# 域名/端口在模板里保持默认值，写完统一经 render_nginx_conf 渲染。
write_http_only_conf() {
  cat > "${TMP_DIR}/news-http.tpl" <<'HTTPEOF'
# news.cheapcoding.top —— http-only 首版（bootstrap 自动生成；证书签发成功后被完整版覆盖）
server {
    listen 80;
    listen [::]:80;
    server_name news.cheapcoding.top;

    # certbot webroot 验证路径
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    # 证书未就绪期间临时反代，保证站点先以 HTTP 可用。
    # 故意不代理 /admin/：登录口令与会话 Cookie 绝不能走明文 HTTP，面板只在
    # HTTPS 就绪后开放（此期间 /admin/ 落入本 location 由 web 容器返回 404，无泄露面）
    location / {
        proxy_pass http://127.0.0.1:8618;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto http;
    }
}
HTTPEOF
  render_nginx_conf "${TMP_DIR}/news-http.tpl" "${TMP_DIR}/news-http.conf"
}

# 完整版按部署参数渲染一次，两个分支（证书已在 / 刚签发）共用
render_nginx_conf "${SRC_DIR}/news.conf" "${TMP_DIR}/news.conf"

if [ -s "${CERT_DIR}/fullchain.pem" ]; then
  echo "证书已存在（${CERT_DIR}），直接安装完整版配置"
  install_nginx_conf "${TMP_DIR}/news.conf"
elif ! command -v certbot >/dev/null 2>&1; then
  write_http_only_conf
  install_nginx_conf "${TMP_DIR}/news-http.conf"
  warnbox "certbot 未安装——本次跳过 HTTPS，站点暂以 HTTP 提供。安装后重跑本脚本即可：apt-get update && apt-get install -y certbot"
else
  install -d -m 755 "$WEBROOT"
  write_http_only_conf
  install_nginx_conf "${TMP_DIR}/news-http.conf"
  # --non-interactive + --agree-tos：无人值守签发；邮箱仅用于到期/吊销通知
  if certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" \
       --non-interactive --agree-tos -m "$CERTBOT_EMAIL"; then
    echo "证书签发成功，安装完整版配置（80 → 301 → 443）"
    install_nginx_conf "${TMP_DIR}/news.conf"
  else
    warnbox "certbot 签发失败——保留 http-only 配置，站点暂以 HTTP 提供。排查 DNS 指向与 80 端口外网可达后重跑本脚本"
  fi
fi

# ---------------------------------------------------------------
section "10/10 收尾自检"
HEALTH_LINE="$(curl -sI --max-time 10 "http://127.0.0.1:${WEB_PORT}/healthz" | head -n1 || true)"
echo "web /healthz         ：${HEALTH_LINE:-（无响应——检查 docker compose ps 与 web 容器日志）}"
# 未登录的 GET /admin/ 返回登录页（200）；生产模式无静态回落、HEAD 不保证实现，
# 故用 -w 取状态码而不用 -I
ADMIN_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${ADMIN_PORT}/admin/" || true)"
echo "admin 面板（回环）   ：HTTP ${ADMIN_CODE:-000}（期望 200 登录页；000 为无响应——检查 admin 容器日志）"
SITE_LINE="$(curl -skI --max-time 15 "https://${DOMAIN}/" | head -n1 || true)"
echo "https://${DOMAIN}/ ：${SITE_LINE:-（无响应——若本次跳过了 HTTPS 属预期，可先验证 http://${DOMAIN}/）}"

cat <<DONEEOF

部署完成。
模型切换面板：https://${DOMAIN}/admin/（网页登录，用户名 admin；初始口令查看：
  cat ${CONFIG_DIR}/admin-password.initial，登录后请立即在面板网页修改口令——
  修改成功会自动删除该文件；忘记口令重置：
  rm ${CONFIG_DIR}/htpasswd-admin ${CONFIG_DIR}/session-secret 后重跑本脚本）。
回滚方法（README §10）：
  1. 编辑 ${APP_DIR}/compose.yaml，把三处 image 改回上一版 digest
     （worker 与 admin 共用 worker 镜像引用，须一并改；形如
      ghcr.io/${OWNER}/news-digest-worker@sha256:…；
      上一版引用见 ${APP_DIR}/backups/DEPLOYED.log 或 compose.yaml.bak-* 备份）
  2. docker compose -f ${APP_DIR}/compose.yaml pull
  3. docker compose -f ${APP_DIR}/compose.yaml up -d web admin
     （worker 无需操作：下次 timer 触发即按旧 digest 运行）
DONEEOF
