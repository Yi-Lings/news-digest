#!/usr/bin/env bash
# bootstrap.sh —— news-digest 阶段 7 服务器幂等部署（可反复执行直至全部就绪）
# 红线（PLAN 阶段 7）：
#   - 服务器不检出源码、不构建镜像：只消费 GHCR 已发布镜像与随本脚本上传的工件；
#   - 密钥只在服务器端通过 Admin 注入：本脚本不经参数、标准输入或部署主机接收密钥；
#   - 部署只启动 Web/Site/Admin，不执行抓取、翻译、构建或投递流水线。
# 参数化：部署目标必须通过 ND_* 环境变量显式提供（全表见 install.sh
# 头注释或 deploy/README.md §0），换域名/端口/目录部署不用改脚本。
# 用法：由 server-push.ps1 上传到 ND_APP_DIR/incoming 后，以 root 执行。
# 退出码：0 完成；3 等待人工 docker login；1 其他错误。
set -euo pipefail

# ---------------- 变量区（执行前 export ND_*；发布身份必须由 Release 工件传入）----------------
for required_name in ND_OWNER ND_APP_DIR ND_DOMAIN ND_CERTBOT_EMAIL; do
  if [ -z "${!required_name:-}" ]; then
    printf '\n错误：缺少 %s；部署目标必须由操作员显式提供。\n' "$required_name" >&2
    exit 1
  fi
done

OWNER="$ND_OWNER"                         # GHCR 命名空间（必须全小写）
TAG="${ND_VERSION:-}"                     # 发布 tag；仅用于审计与镜像 label 核对
APP_DIR="$ND_APP_DIR"                     # 部署目录（compose、config/、备份）
CONFIG_DIR="${APP_DIR}/config"            # 密钥配置子目录：admin 容器唯一 bind 挂载的宿主路径
SITE_CONFIG_DIR="${APP_DIR}/site-config"  # 仅公开 Site 所需字段的独立投影目录
DOMAIN="$ND_DOMAIN"
WEB_PORT="${ND_WEB_PORT:-8618}"           # web 宿主回环端口（服务器 8080 已被既有服务占用）
ADMIN_PORT="${ND_ADMIN_PORT:-8619}"       # 模型切换面板宿主回环端口
SITE_PORT="${ND_SITE_PORT:-8620}"         # 公开读者站点宿主回环端口
CERTBOT_EMAIL="$ND_CERTBOT_EMAIL"
if [[ ! "$OWNER" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  printf '\n错误：ND_OWNER 非法：%s\n' "$OWNER" >&2
  exit 1
fi
if [[ ! "$APP_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || [[ "$APP_DIR/" == *"//"* ]] ||
   [[ "$APP_DIR/" == *"/./"* ]] || [[ "$APP_DIR/" == *"/../"* ]]; then
  printf '\n错误：ND_APP_DIR 必须是无 . 或 .. 路径段的绝对路径：%s\n' "$APP_DIR" >&2
  exit 1
fi
if [[ ! "$DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]; then
  printf '\n错误：ND_DOMAIN 非法：%s\n' "$DOMAIN" >&2
  exit 1
fi
if [[ ! "$CERTBOT_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$ ]]; then
  printf '\n错误：ND_CERTBOT_EMAIL 非法。\n' >&2
  exit 1
fi
for port_name in WEB_PORT ADMIN_PORT SITE_PORT; do
  port_value="${!port_name}"
  if [[ ! "$port_value" =~ ^[1-9][0-9]{0,4}$ ]] || (( port_value > 65535 )); then
    printf '\n错误：%s 非法：%s\n' "$port_name" "$port_value" >&2
    exit 1
  fi
done
if [ "$WEB_PORT" = "$ADMIN_PORT" ] || [ "$WEB_PORT" = "$SITE_PORT" ] || [ "$ADMIN_PORT" = "$SITE_PORT" ]; then
  printf '\n错误：ND_WEB_PORT、ND_ADMIN_PORT 与 ND_SITE_PORT 必须互不相同。\n' >&2
  exit 1
fi
CERT_DIR="/etc/letsencrypt/live/${DOMAIN}"
NGINX_CONF="/etc/nginx/conf.d/news.conf"
HTPASSWD_FILE="${CONFIG_DIR}/htpasswd-admin"     # 面板登录口令哈希（面板登录页校验；nginx 不读取）
WEBROOT="/var/www/certbot"                # certbot webroot 验证目录（news.conf 同路径）
# 生产镜像只允许 Release 提供的两条不可变 digest；禁止 tag fallback 或混合引用。
WORKER_DIGEST="${ND_WORKER_DIGEST:-}"
WEB_DIGEST="${ND_WEB_DIGEST:-}"
if [ -z "$TAG" ]; then
  printf '\n错误：缺少 ND_VERSION；必须通过同一 Release 工件或上层发布编排传入。\n' >&2
  exit 1
fi
if [[ ! "$TAG" =~ ^v[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}$ ]]; then
  printf '\n错误：ND_VERSION 非法：%s\n' "$TAG" >&2
  exit 1
fi
if [ -z "$WORKER_DIGEST" ] || [ -z "$WEB_DIGEST" ]; then
  printf '\n错误：ND_WORKER_DIGEST 与 ND_WEB_DIGEST 必须由同一 Release 成对提供；禁止退回可变 tag。\n' >&2
  exit 1
fi
_validate_digest() {  # $1=值 $2=变量名
  [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]] ||
    { printf '\n错误：%s 必须形如 sha256: 加 64 位十六进制，实得：%s\n' "$2" "$1" >&2; exit 1; }
}
_validate_digest "$WORKER_DIGEST" ND_WORKER_DIGEST
_validate_digest "$WEB_DIGEST" ND_WEB_DIGEST
WORKER_IMAGE="ghcr.io/${OWNER}/news-digest-worker@${WORKER_DIGEST}"
WEB_IMAGE="ghcr.io/${OWNER}/news-digest-web@${WEB_DIGEST}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # 上传工件所在目录（incoming）
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

section() { printf '\n=== %s ===\n' "$1"; }
warnbox() { printf '\n!!! 警告：%s\n\n' "$1"; }
die()     { printf '\n错误：%s\n' "$1" >&2; exit 1; }

require_deployment_units_quiescent() {
  local unit load_state active_state
  command -v systemctl >/dev/null 2>&1 ||
    die "systemctl 不可用——无法确认定时器与 worker 已冻结"
  for unit in \
    news-digest.timer \
    news-digest.service \
    news-digest-resume.service \
    news-digest-wakeup.path
  do
    load_state="$(systemctl show "$unit" --property=LoadState --value 2>/dev/null)" ||
      die "无法读取 ${unit} 的 LoadState——拒绝在运行状态未知时部署"
    if [ "$load_state" = "not-found" ]; then
      continue
    fi
    active_state="$(systemctl show "$unit" --property=ActiveState --value 2>/dev/null)" ||
      die "无法读取 ${unit} 的 ActiveState——拒绝在运行状态未知时部署"
    case "$active_state" in
      inactive|failed) ;;
      *)
        die "${unit} ActiveState=${active_state:-unknown}；先停止 timer、wakeup path 与 worker，再重新部署"
        ;;
    esac
  done
  echo "运行入口已冻结；unit 的 enabled 状态保持不变"
}

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
for f in compose.yaml news-digest.service news-digest-resume.service news-digest-wakeup.path news-digest.timer news.conf; do
  [ -f "${SRC_DIR}/${f}" ] || die "缺少上传工件：${SRC_DIR}/${f}（应由 server-push.ps1 一并上传）"
done
command -v flock >/dev/null 2>&1 || die "缺少 flock（util-linux）；无法保证每日与恢复 worker 串行"
require_deployment_units_quiescent
echo "校验通过：root、Docker、Compose 与上传工件齐备"

# ---------------------------------------------------------------
section "2/10 目录与配置工件就位"
# backups 为 README §9 备份与回滚台账所依赖；mail 预留邮件归档
install -d -m 755 "$APP_DIR" "$APP_DIR/mail" "$APP_DIR/backups"

# compose.yaml：先只在临时文件里渲染；section 6 拉取镜像并完成迁移前数据库备份后才就位，
# 避免现有 timer 在备份前读到新 worker digest。重跑时内容一致仍会幂等跳过。
# 渲染内容：OWNER/VERSION 占位符（worker 镜像三处：worker、site 与 admin 服务共用同一
# 引用，sed 逐行匹配自动同时替换）、部署目录（env_file 与 admin 的 /config bind
# 挂载）、web 宿主端口、admin 监听端口（admin 用 host 网络，command 的 --port
# 即宿主端口）。若现有文件手工固定过 digest，会先备份再覆盖。
sed -e "s|ghcr.io/OWNER/news-digest-worker:VERSION|${WORKER_IMAGE}|" \
    -e "s|ghcr.io/OWNER/news-digest-web:VERSION|${WEB_IMAGE}|" \
    -e "s|/srv/news-digest|${APP_DIR}|g" \
    -e "s|127.0.0.1:8618:|127.0.0.1:${WEB_PORT}:|" \
    -e "s|--port 8619|--port ${ADMIN_PORT}|g" \
    -e "s|--port 8620|--port ${SITE_PORT}|g" \
    "${SRC_DIR}/compose.yaml" > "${TMP_DIR}/compose.yaml"
if [ -f "${APP_DIR}/compose.yaml" ] && ! cmp -s "${TMP_DIR}/compose.yaml" "${APP_DIR}/compose.yaml"; then
  echo "注意：现有 compose.yaml 内容不同；将在迁移前数据库备份成功后覆盖（原文件自动备份）"
fi

# service 单元的 ExecStart 写死部署目录：模板文件保持默认值不动，安装时按 APP_DIR
# 渲染——换目录部署时 timer 触发的任务才能找到 compose.yaml（timer 无路径，无需渲染）
sed -e "s|/srv/news-digest|${APP_DIR}|g" \
    "${SRC_DIR}/news-digest.service" > "${TMP_DIR}/news-digest.service"
install_file "${TMP_DIR}/news-digest.service" /etc/systemd/system/news-digest.service 644
sed -e "s|/srv/news-digest|${APP_DIR}|g" \
    "${SRC_DIR}/news-digest-resume.service" > "${TMP_DIR}/news-digest-resume.service"
install_file "${TMP_DIR}/news-digest-resume.service" /etc/systemd/system/news-digest-resume.service 644
sed -e "s|/srv/news-digest|${APP_DIR}|g" \
    "${SRC_DIR}/news-digest-wakeup.path" > "${TMP_DIR}/news-digest-wakeup.path"
install_file "${TMP_DIR}/news-digest-wakeup.path" /etc/systemd/system/news-digest-wakeup.path 644
# nginx 的 news.conf 留到第 9 步按证书状态选版本就位：证书未签发时提前放完整版
# 会让全局 nginx -t 失败，殃及主站与 SUB2API 的后续 reload。

# service 单元的 ExecStart 写死 /usr/bin/docker；路径不符会导致 timer 触发即失败
DOCKER_BIN="$(command -v docker)"
if [ "$DOCKER_BIN" != "/usr/bin/docker" ]; then
  warnbox "docker 实际路径为 ${DOCKER_BIN}，与 service 单元中 /usr/bin/docker 不符——请修改 /etc/systemd/system/news-digest.service 后再 daemon-reload"
fi

# ---------------------------------------------------------------
section "3/10 服务器运行配置 .env"
# 配置收窄（rc2 安全整改）：可被面板读写的配置全部集中到 config/ 子目录，admin
# 容器只 bind 挂载该子目录——面板即使被攻破也触不到 compose.yaml 与运维脚本。
mkdir -p "$CONFIG_DIR"
chown root:10001 "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
touch "${CONFIG_DIR}/automation.wake"
chown root:10001 "${CONFIG_DIR}/automation.wake"
chmod 660 "${CONFIG_DIR}/automation.wake"
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

# ---- 翻译供应商；生产运行以 providers.json 的唯一默认档案为权威源 ----
TRANSLATION_API_BASE_URL=
TRANSLATION_API_KEY=
TRANSLATION_MODEL=
TRANSLATION_API_TYPE=openai_chat
TRANSLATION_STREAM=true

# ---- EasyPay 兼容支付；完成公网 HTTPS 回调验收前保持关闭 ----
EPAY_ENABLED=false
EPAY_API_BASE=
EPAY_PID=
EPAY_PKEY=
EPAY_PAYMENT_TYPE=alipay
EPAY_ORDER_TTL_SECONDS=300
EPAY_AMOUNT_HOLD_SECONDS=3600

# ---- SMTP 发信与邮件内容（SMTP_RECIPIENTS 仅用于旧安装一次性导入）----
EMAIL_DELIVERY_ENABLED=false
SMTP_HOST=
SMTP_PORT=465
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_SECURITY=implicit_tls
SMTP_FROM=
SMTP_RECIPIENTS=
EMAIL_MAINS_ENABLED=true
EMAIL_BRIEFS_ENABLED=true
EMAIL_MAIN_LIMIT=6
EMAIL_BRIEF_LIMIT=5
EMAIL_LANGUAGE=bi
EMAIL_SOURCE_FILTERS=
EMAIL_LAYOUT=digest
EMAIL_SUMMARY_LENGTH=standard
EMAIL_CATCHUP_WINDOW_HOURS=6

# HTTPS、SMTP、全局邮件开关均就绪后，再由 Admin 开启公开订阅
PUBLIC_SUBSCRIPTION_ENABLED=false

# ---- 出网代理：本服务器直连，不需要代理，保持留空 ----
NEWS_HTTP_PROXY=
ENVEOF
  chown root:root "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "已生成关闭 API、SMTP、自动投递与公开订阅的安全初始配置"
fi

# 幂等收紧：无论谁动过，权限始终回到 root:600
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Site 不得读取 providers.json 或整份管理配置。按原始 dotenv 行投影固定白名单，
# 后续 Admin 保存 SMTP/EasyPay 时会用同一白名单原子刷新此文件。
mkdir -p "$SITE_CONFIG_DIR"
chown root:10001 "$SITE_CONFIG_DIR"
chmod 750 "$SITE_CONFIG_DIR"
SITE_ENV_FILE="${SITE_CONFIG_DIR}/.env"
SITE_ENV_TMP="${SITE_CONFIG_DIR}/.env.tmp.$$"
awk -F= '
  $1 ~ /^(NEWS_SITE_URL|NEWS_TIMEZONE|SMTP_HOST|SMTP_PORT|SMTP_USERNAME|SMTP_PASSWORD|SMTP_SECURITY|SMTP_FROM|EPAY_ENABLED|EPAY_API_BASE|EPAY_PID|EPAY_PKEY|EPAY_PAYMENT_TYPE|EPAY_ORDER_TTL_SECONDS|EPAY_AMOUNT_HOLD_SECONDS)$/ { print }
' "$ENV_FILE" > "$SITE_ENV_TMP"
chown root:root "$SITE_ENV_TMP"
chmod 600 "$SITE_ENV_TMP"
mv -f "$SITE_ENV_TMP" "$SITE_ENV_FILE"
if ! awk '
  /^[[:space:]]*NEWS_TIMEZONE=/ {
    value=$0
    sub(/^[[:space:]]*NEWS_TIMEZONE=/, "", value)
    sub(/[[:space:]]+$/, "", value)
    count++
  }
  END { exit !(count == 1 && value == "Asia/Shanghai") }
' "$ENV_FILE"; then
  die "生产 NEWS_TIMEZONE 必须且只能设置一次 Asia/Shanghai，与每日 08:00 systemd timer 保持一致"
fi
install_file "${SRC_DIR}/news-digest.timer" /etc/systemd/system/news-digest.timer 644
echo ".env 就绪（权限已确认 600）；API、SMTP 与自动投递由 Admin 后续配置"

# ---------------------------------------------------------------
section "4/10 模型供应商档案 providers.json"
# Admin（/admin/）管理档案、唯一默认项和真实连接测试；密钥可经受保护的 HTTPS
# 会话写入，但不会由 API 回传。也可登录服务器直接编辑（格式见 deploy/README.md §13）。
PROVIDERS_FILE="${CONFIG_DIR}/providers.json"
if [ -f "$PROVIDERS_FILE" ]; then
  # 幂等收紧权限；内容绝不覆盖——面板或手工编辑过的档案是运行时状态，不是部署工件
  chown root:10001 "$PROVIDERS_FILE"
  chmod 640 "$PROVIDERS_FILE"
  echo "providers.json 已存在（权限已确认 root:10001/640），跳过生成"
else
  env_get() { sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | head -n1; }
  # 极简 JSON 转义：URL/密钥/模型名里按理不会出现 " 或 \，防御性处理一下
  json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
  P_URL="$(env_get TRANSLATION_API_BASE_URL)"
  P_KEY="$(env_get TRANSLATION_API_KEY)"
  P_MODEL="$(env_get TRANSLATION_MODEL)"
  P_TYPE="$(env_get TRANSLATION_API_TYPE)"
  P_STREAM="$(env_get TRANSLATION_STREAM)"
  [ -n "$P_TYPE" ] || P_TYPE="openai_chat"
  [ -n "$P_STREAM" ] || P_STREAM="true"
  case "$P_TYPE" in
    openai_chat|anthropic_messages) ;;
    *) die "TRANSLATION_API_TYPE 必须是 openai_chat 或 anthropic_messages" ;;
  esac
  case "$P_STREAM" in
    true|false) ;;
    *) die "TRANSLATION_STREAM 必须是 true 或 false" ;;
  esac
  if [ -n "$P_URL" ] && [ -n "$P_KEY" ] && [ -n "$P_MODEL" ]; then
    # 用 .env 已填好的字段生成首个唯一默认档案
    cat > "$PROVIDERS_FILE" <<PROVEOF
{
 "providers": {
  "default": {
   "base_url": "$(json_escape "$P_URL")",
   "api_key": "$(json_escape "$P_KEY")",
   "model": "$(json_escape "$P_MODEL")",
   "api_type": "$(json_escape "$P_TYPE")",
   "stream": $P_STREAM,
   "enabled": true,
   "is_default": true
  }
 }
}
PROVEOF
    chown root:10001 "$PROVIDERS_FILE"
    chmod 640 "$PROVIDERS_FILE"
    echo "已生成首个供应商档案（default，取自 .env）：${PROVIDERS_FILE}"
  else
    # 不中止：首次部署由 Admin 创建档案；旧安装仍可从已填完整的 .env 迁移。
    warnbox "尚未配置翻译供应商；部署完成后请在 Admin 新增、测试并设为唯一默认档案"
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
  4. 重新执行原始 install/deploy-all/server-push 入口，让同一 Release 的版本与 digest 再次传入
LOGINEOF
  exit 3
fi

# ---------------------------------------------------------------
section "6/10 拉取镜像并启动 web 与 admin"
docker pull "$WORKER_IMAGE"
docker pull "$WEB_IMAGE"

verify_release_image_labels() {
  local worker_version web_version worker_revision web_revision
  worker_version="$(docker image inspect "$WORKER_IMAGE" \
    --format '{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || true)"
  web_version="$(docker image inspect "$WEB_IMAGE" \
    --format '{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || true)"
  worker_revision="$(docker image inspect "$WORKER_IMAGE" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)"
  web_revision="$(docker image inspect "$WEB_IMAGE" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)"
  [ "$worker_version" = "$TAG" ] ||
    die "worker 镜像 version label 与部署版本不一致"
  [ "$web_version" = "$TAG" ] ||
    die "web 镜像 version label 与部署版本不一致"
  [ -n "$worker_revision" ] && [ "$worker_revision" != "UNKNOWN" ] ||
    die "worker 镜像缺少有效 revision label"
  [ "$worker_revision" = "$web_revision" ] ||
    die "worker/web 镜像 revision label 不一致"
  echo "镜像标签已核验：version=${TAG}，worker/web revision 一致"
}

verify_release_image_labels

# Admin 与 worker 打开旧数据库时会自动执行 schema 迁移；任何数据库消费者启动前，先用
# SQLite online backup 生成 WAL 一致快照。无既有卷或无有效 news.db 时才允许明确跳过。
backup_database_before_migration() {
  local DATA_VOLUME="news-digest_news-data"
  local volumes backup_dir backup_workdir backup_tmp suffix backup_path
  local checksum_tmp checksum_path status=0

  if ! volumes="$(docker volume ls --format '{{.Name}}')"; then
    die "无法列出 Docker volumes，拒绝在数据库备份状态未知时继续"
  fi
  if ! grep -Fxq -- "$DATA_VOLUME" <<< "$volumes"; then
    echo "未发现数据卷 ${DATA_VOLUME}，跳过迁移前数据库备份"
    return 0
  fi
  docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1 ||
    die "数据卷 ${DATA_VOLUME} 存在于列表但无法 inspect，拒绝继续"

  backup_dir="${APP_DIR}/backups"
  install -d -m 755 "$backup_dir"
  backup_workdir="$(mktemp -d "${backup_dir}/.sqlite-backup.XXXXXX")"
  chown root:root "$backup_workdir"
  chmod 700 "$backup_workdir"
  backup_tmp="${backup_workdir}/news.db"
  install -o root -g root -m 600 /dev/null "$backup_tmp"

  # WAL readers may need to update the shared-memory lock file. Keep the
  # database connection itself read-only below, while allowing SQLite to
  # coordinate a consistent online snapshot with the live Admin/Site readers.
  docker run --rm --network none --read-only --user 0:0 \
    --entrypoint python \
    --mount "type=volume,src=${DATA_VOLUME},dst=/data" \
    --mount "type=bind,src=${backup_workdir},dst=/backup" \
    "$WORKER_IMAGE" -c '
import sqlite3
from pathlib import Path

source_path = Path("/data/news.db")
if source_path.is_symlink():
    raise SystemExit(4)
if not source_path.exists():
    raise SystemExit(3)
if not source_path.is_file():
    raise SystemExit(4)
if source_path.stat().st_size == 0:
    raise SystemExit(3)

with sqlite3.connect(
    "file:/data/news.db?mode=ro", uri=True, timeout=30
) as source, sqlite3.connect("/backup/news.db", timeout=30) as target:
    source.backup(target)
    if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit(4)
' || status=$?

  if [ "$status" -eq 3 ]; then
    rm -f -- "$backup_tmp" "${backup_tmp}-journal" "${backup_tmp}-wal" "${backup_tmp}-shm"
    rmdir -- "$backup_workdir" 2>/dev/null || true
    echo "数据卷中无有效 news.db，跳过迁移前数据库备份"
    return 0
  fi
  if [ "$status" -ne 0 ] || [ ! -s "$backup_tmp" ]; then
    rm -f -- "$backup_tmp" "${backup_tmp}-journal" "${backup_tmp}-wal" "${backup_tmp}-shm"
    rmdir -- "$backup_workdir" 2>/dev/null || true
    die "SQLite 迁移前备份或完整性校验失败，Admin 与 worker 未启动"
  fi

  suffix="${backup_workdir##*.}"
  backup_path="${backup_dir}/news-db-${TAG}-${STAMP}-${suffix}.sqlite3"
  [ ! -e "$backup_path" ] || die "迁移前备份目标已存在，拒绝覆盖：${backup_path}"
  mv -- "$backup_tmp" "$backup_path"
  rm -f -- "${backup_tmp}-journal" "${backup_tmp}-wal" "${backup_tmp}-shm"
  rmdir -- "$backup_workdir" 2>/dev/null || true

  checksum_tmp="$(mktemp "${backup_dir}/.news-db-checksum.XXXXXX")"
  chown root:root "$checksum_tmp"
  chmod 600 "$checksum_tmp"
  checksum_path="${backup_path}.sha256"
  if ! (cd "$backup_dir" && sha256sum "$(basename "$backup_path")") > "$checksum_tmp"; then
    rm -f -- "$backup_path" "$checksum_tmp"
    die "迁移前数据库备份的 SHA-256 生成失败"
  fi
  [ ! -e "$checksum_path" ] || {
    rm -f -- "$backup_path" "$checksum_tmp"
    die "迁移前数据库校验文件已存在，拒绝覆盖：${checksum_path}"
  }
  mv -- "$checksum_tmp" "$checksum_path"
  chown root:root "$backup_path" "$checksum_path"
  chmod 600 "$backup_path" "$checksum_path"
  if ! (cd "$backup_dir" && sha256sum --check "$(basename "$checksum_path")" >/dev/null); then
    rm -f -- "$backup_path" "$checksum_path"
    die "迁移前数据库备份的 SHA-256 复核失败"
  fi
  echo "迁移前数据库备份已验证：${backup_path}（SHA-256：${checksum_path}）"
}

backup_database_before_migration

# Admin 以 0:10001 且 cap_drop=ALL 运行，不能依赖 root 绕过目录权限；worker 则以
# 10001:10001 运行。统一已有内容的共享 GID/组写权限，并给目录加 setgid，保证两者
# 创建的 SQLite、journal、邮件归档与子目录持续互相可写。必须在数据库备份后执行。
prepare_shared_data_volume() {
  local DATA_VOLUME="news-digest_news-data"
  docker volume create "$DATA_VOLUME" >/dev/null ||
    die "无法创建或确认共享数据卷 ${DATA_VOLUME}"
  docker run --rm --network none --user 0:0 --read-only \
    --entrypoint /bin/sh \
    --mount "type=volume,src=${DATA_VOLUME},dst=/data" \
    "$WORKER_IMAGE" -c '
chgrp -R 10001 /data
chmod -R g+rwX /data
find /data -type d -exec chmod g+s {} +
' || die "无法准备共享数据卷 ${DATA_VOLUME} 的 UID/GID 权限"
  echo "共享数据卷权限已就绪：${DATA_VOLUME}（GID 10001、目录 setgid、组可写）"
}

prepare_shared_data_volume
install_file "${TMP_DIR}/compose.yaml" "${APP_DIR}/compose.yaml" 644
COMPOSE=(docker compose -f "${APP_DIR}/compose.yaml")
# 记录本次实际部署的镜像 digest，供回滚溯源（回滚指引见收尾段与 README §10）。pull 后本地
# 镜像已按 Release digest 固定；同时把运行时解析结果落盘台账供回滚核对。
record_deployed() {
  local logdir="${APP_DIR}/backups" wd bd
  install -d -m 755 "$logdir"
  wd="$(docker image inspect "$WORKER_IMAGE" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null || true)"
  bd="$(docker image inspect "$WEB_IMAGE" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null || true)"
  printf '%s\ttag=%s\tworker=%s\tweb=%s\n' \
    "$(date --iso-8601=seconds)" "$TAG" "${wd:-未解析}" "${bd:-未解析}" \
    >> "${logdir}/DEPLOYED.log"
  echo "已记录部署 digest 台账：${logdir}/DEPLOYED.log"
  echo "  worker=${wd:-未解析}"
  echo "  web=${bd:-未解析}"
}
record_deployed
# /healthz 不依赖站点内容，current 出现前仅健康检查可用。admin 与 worker 使用同一镜像，
# 但部署阶段只常驻 Web/Site/Admin，不执行任何 worker 业务流水线。
"${COMPOSE[@]}" up -d web site admin
echo "部署阶段已跳过抓取、翻译、构建与投递；请先在 Admin 完成运行配置"

# ---------------------------------------------------------------
section "7/10 回环健康门禁"
# 公网 Nginx 与 systemd 调度仍保持冻结。三项本地服务全部可用后才允许切换公网入口；
# 任一失败都会在旧 Nginx 与停止的 timer/path 状态下退出。
HEALTH_CODE="000"
for _ in {1..15}; do
  HEALTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${WEB_PORT}/healthz" || true)"
  [ "$HEALTH_CODE" = "200" ] && break
  sleep 2
done
echo "web /healthz         ：HTTP ${HEALTH_CODE:-000}（期望 200）"
if [ "$HEALTH_CODE" != "200" ]; then
  "${COMPOSE[@]}" logs --no-color --tail 100 web >&2 || true
  die "Web 健康检查失败；公网入口与调度仍保持原状"
fi
SITE_CODE="000"
for _ in {1..15}; do
  SITE_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${SITE_PORT}/healthz" || true)"
  [ "$SITE_CODE" = "200" ] && break
  sleep 2
done
echo "site /healthz        ：HTTP ${SITE_CODE:-000}（期望 200）"
if [ "$SITE_CODE" != "200" ]; then
  "${COMPOSE[@]}" logs --no-color --tail 100 site >&2 || true
  die "Site 健康检查失败；公网入口与调度仍保持原状"
fi
# 未登录的 GET /admin/ 返回登录页（200）；生产模式无静态回落、HEAD 不保证实现，
# 故用 -w 取状态码而不用 -I。
ADMIN_CODE="000"
for _ in {1..15}; do
  ADMIN_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${ADMIN_PORT}/admin/" || true)"
  [ "$ADMIN_CODE" = "200" ] && break
  sleep 2
done
echo "admin 面板（回环）   ：HTTP ${ADMIN_CODE:-000}（期望 200 登录页）"
if [ "$ADMIN_CODE" != "200" ]; then
  "${COMPOSE[@]}" logs --no-color --tail 100 admin >&2 || true
  die "Admin 健康检查失败；公网入口与调度仍保持原状"
fi
echo "Web/Site/Admin 回环健康门禁通过"

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

# Admin 创建的认证材料始终只允许 root 读取；重复部署会收紧被误改的权限。
for private_file in "${CONFIG_DIR}/session-secret" "${CONFIG_DIR}/admin-password.initial"; do
  if [ -f "$private_file" ]; then
    chown root:root "$private_file"
    chmod 600 "$private_file"
  fi
done

# ---------------------------------------------------------------
section "9/10 宿主机 Nginx 与 HTTPS"
[ -d /etc/nginx/conf.d ] || die "/etc/nginx/conf.d 不存在——请先确认宿主机 nginx 布局（本脚本只新增该目录下的 news.conf）"

# 渲染 nginx 配置：模板内写的是默认域名/端口的真实值（保持模板可读、可独立审阅），
# 此处按本次部署参数替换。默认参数下渲染前后逐字节一致，不影响幂等比对。
render_nginx_conf() {  # $1=源 $2=目标
  sed -e "s|news.example.com|${DOMAIN}|g" \
      -e "s|127.0.0.1:8618|127.0.0.1:${WEB_PORT}|g" \
      -e "s|127.0.0.1:8619|127.0.0.1:${ADMIN_PORT}|g" \
      -e "s|127.0.0.1:8620|127.0.0.1:${SITE_PORT}|g" \
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
# news.example.com —— http-only 首版（bootstrap 自动生成；证书签发成功后被完整版覆盖）
server {
    listen 80;
    listen [::]:80;
    server_name news.example.com;

    # certbot webroot 验证路径
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    # 证书未就绪期间临时反代，保证站点先以 HTTP 可用。
    # 故意不代理 /admin/：登录口令与会话 Cookie 绝不能走明文 HTTP，面板只在
    # HTTPS 就绪后开放（此期间 /admin/ 落入本 location 由 web 容器返回 404，无泄露面）
    location / {
    proxy_pass http://127.0.0.1:8620;
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

# 证书续期后重载 nginx。certbot 由 systemd timer 自动续期，但默认不会通知 nginx
# 加载新证书——约 60 天后续签、旧证书 90 天到期，若不 reload 则 HTTPS 静默失效。
# 用 renewal-hooks/deploy/ 目录钩子（对本机所有证书、每次成功续期都执行），而非签发时
# 的 --deploy-hook：如此已经签发过证书、走下面「证书已存在」分支的存量服务器也能被
# 覆盖修复（--deploy-hook 只会写进新签发证书的续期配置，够不到存量证书）。每次 bootstrap
# 无条件重装，幂等。
install_renewal_reload_hook() {
  command -v certbot >/dev/null 2>&1 || return 0
  hook_dir=/etc/letsencrypt/renewal-hooks/deploy
  install -d -m 755 "$hook_dir"
  cat > "${hook_dir}/10-reload-nginx.sh" <<'HOOKEOF'
#!/bin/sh
# certbot 在证书成功续期后自动调用；先 nginx -t 校验，配置无误才 reload，
# 避免坏配置导致 reload 失败中断在线服务。
nginx -t && systemctl reload nginx
HOOKEOF
  chmod +x "${hook_dir}/10-reload-nginx.sh"
  echo "已安装证书续期重载钩子：${hook_dir}/10-reload-nginx.sh"
}
install_renewal_reload_hook

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
section "10/10 systemd 调度恢复与收尾自检"
systemctl daemon-reload
# Nginx 已成功 reload 后才恢复调度。合并 enable/start 并在任一失败时停止两者，
# 防止只恢复一半或让未通过完整门禁的 worker 随 timer 运行。
install -d -m 755 /var/lib/systemd/timers
touch /var/lib/systemd/timers/stamp-news-digest.timer
if ! systemctl enable --now news-digest.timer news-digest-wakeup.path; then
  systemctl stop news-digest.timer news-digest-wakeup.path >/dev/null 2>&1 || true
  die "timer/path 恢复失败，已保持调度停止"
fi
if ! systemctl is-active --quiet news-digest.timer ||
   ! systemctl is-active --quiet news-digest-wakeup.path; then
  systemctl stop news-digest.timer news-digest-wakeup.path >/dev/null 2>&1 || true
  die "timer/path 活动态核验失败，已重新停止调度"
fi
echo "下次触发（NEXT 列）："
systemctl list-timers news-digest.timer --no-pager || true
SITE_LINE="$(curl -skI --max-time 15 "https://${DOMAIN}/" | head -n1 || true)"
echo "https://${DOMAIN}/ ：${SITE_LINE:-（无响应——若本次跳过了 HTTPS 属预期，可先验证 http://${DOMAIN}/）}"
cat <<DONEEOF

部署完成。
Admin 管理面板：https://${DOMAIN}/admin/（网页登录，用户名 admin；初始口令查看：
  cat ${CONFIG_DIR}/admin-password.initial，登录后请立即在面板网页修改口令——
  修改成功会自动删除该文件；忘记口令重置：
  rm ${CONFIG_DIR}/htpasswd-admin ${CONFIG_DIR}/session-secret 后重跑本脚本）。
首次部署的 API/SMTP 为空，邮件投递与公开订阅关闭；请登录 Admin 完成配置。
部署过程未运行抓取、翻译、构建或投递流水线。
回滚方法（README §10）：
  1. 编辑 ${APP_DIR}/compose.yaml，把三处 image 改回上一版 digest
     （worker 与 admin 共用 worker 镜像引用，须一并改；形如
      ghcr.io/${OWNER}/news-digest-worker@sha256:…；
      上一版引用见 ${APP_DIR}/backups/DEPLOYED.log 或 compose.yaml.bak-* 备份）
  2. docker compose -f ${APP_DIR}/compose.yaml pull
  3. docker compose -f ${APP_DIR}/compose.yaml up -d web site admin
     （worker 无需操作：下次 timer 触发即按旧 digest 运行）
DONEEOF
