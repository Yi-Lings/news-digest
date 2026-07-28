#!/usr/bin/env bash
# preflight.sh —— news-digest 阶段 7 服务器只读体检
# 用途：在执行 bootstrap.sh 之前核对服务器现状。本脚本绝不修改服务器上任何东西。
# 为什么不用 set -e：体检要求单项失败不中断，逐项收集结果后统一汇报结论。
# 退出码：0 = 可以继续 bootstrap（可能带警告）；1 = 存在阻断项，先处理再继续。
set -u

for required_name in ND_OWNER ND_APP_DIR ND_DOMAIN ND_CERTBOT_EMAIL; do
  if [ -z "${!required_name:-}" ]; then
    printf '错误：缺少 %s；部署目标必须由操作员显式提供。\n' "$required_name" >&2
    exit 1
  fi
done

DOMAIN="$ND_DOMAIN"
APP_DIR="$ND_APP_DIR"
OWNER="$ND_OWNER"
WEB_PORT="${ND_WEB_PORT:-8618}"
ADMIN_PORT="${ND_ADMIN_PORT:-8619}"
if [[ ! "$OWNER" =~ ^[a-z0-9][a-z0-9-]*$ ]] ||
   [[ ! "$APP_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || [[ "$APP_DIR/" == *"//"* ]] ||
   [[ "$APP_DIR/" == *"/./"* ]] || [[ "$APP_DIR/" == *"/../"* ]] ||
   [[ ! "$DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]] ||
   [[ ! "$ND_CERTBOT_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$ ]]; then
  printf '错误：部署目标格式非法。\n' >&2
  exit 1
fi
for port_value in "$WEB_PORT" "$ADMIN_PORT"; do
  if [[ ! "$port_value" =~ ^[1-9][0-9]{0,4}$ ]] || (( port_value > 65535 )); then
    printf '错误：部署端口非法：%s\n' "$port_value" >&2
    exit 1
  fi
done
if [ "$WEB_PORT" = "$ADMIN_PORT" ]; then
  printf '错误：ND_WEB_PORT 与 ND_ADMIN_PORT 不得相同。\n' >&2
  exit 1
fi

OK_N=0; WARN_N=0; FAIL_N=0

# 输出格式统一为一行「状态 + 事实 + 建议」；前缀宽度对齐（CJK 双宽已计入）
ok()   { OK_N=$((OK_N + 1));     printf '[OK]   %s\n' "$1"; }
warn() { WARN_N=$((WARN_N + 1)); printf '[警告] %s\n' "$1"; }
fail() { FAIL_N=$((FAIL_N + 1)); printf '[缺失] %s\n' "$1"; }

echo "================================================================"
echo " news-digest 部署前体检（只读，不做任何修改）"
echo " $(date '+%F %T %Z')  host=$(hostname 2>/dev/null || echo unknown)"
echo "================================================================"

# --- CPU 架构：release.yml 目前只发布 linux/amd64，架构不符必须先改 CI 重新发布 ---
ARCH="$(uname -m 2>/dev/null || echo unknown)"
case "$ARCH" in
  x86_64)  ok "CPU 架构 x86_64——与 CI 发布的 linux/amd64 镜像匹配" ;;
  aarch64) warn "CPU 架构 aarch64——须把 release.yml 的 PLATFORMS 改为 linux/arm64 并重新发布镜像" ;;
  *)       warn "CPU 架构 ${ARCH}——请人工确认与 CI 发布平台是否匹配" ;;
esac

# --- Docker 与 Compose v2（compose.yaml 使用 compose spec 语法）---
if DOCKER_V="$(docker --version 2>/dev/null)"; then
  ok "Docker：${DOCKER_V}"
else
  fail "docker 命令不可用——请先安装 Docker Engine（期望 >= 24）"
fi
if COMPOSE_V="$(docker compose version 2>/dev/null)"; then
  ok "Compose v2：${COMPOSE_V}"
else
  fail "docker compose 子命令不可用——需要内置 Compose v2（期望 >= 2.20）"
fi

# --- 内存 / 磁盘余量：worker 上限 256m + web 32m，镜像与卷还需磁盘空间 ---
MEM_AVAIL_MB="$(free -m 2>/dev/null | awk '/^Mem:/{print $7}')"
if [ -n "${MEM_AVAIL_MB:-}" ]; then
  if [ "$MEM_AVAIL_MB" -ge 512 ]; then
    ok "可用内存 ${MEM_AVAIL_MB} MB——覆盖 worker 256m + web 32m 上限有余量"
  else
    warn "可用内存仅 ${MEM_AVAIL_MB} MB（< 512 MB）——留意 worker 峰值，必要时先腾内存"
  fi
else
  warn "无法读取 free -m 输出——请人工核对内存余量"
fi
DISK_AVAIL_MB="$(df -BM --output=avail / 2>/dev/null | tail -n1 | tr -dc '0-9')"
if [ -n "${DISK_AVAIL_MB:-}" ]; then
  if [ "$DISK_AVAIL_MB" -ge 5120 ]; then
    ok "根分区可用 ${DISK_AVAIL_MB} MB——镜像、数据卷与日志空间充足"
  else
    warn "根分区仅可用 ${DISK_AVAIL_MB} MB（< 5 GB）——建议先清理磁盘再部署"
  fi
else
  warn "无法读取 df 输出——请人工核对磁盘余量"
fi

# --- 宿主机 Nginx：现有主站与 SUB2API 在用，reload 前提是全局配置本就通过 ---
if NGINX_V="$(nginx -v 2>&1)"; then
  ok "Nginx：${NGINX_V}"
  if nginx -t >/dev/null 2>&1; then
    ok "nginx -t 现有配置通过——可安全新增 conf.d/news.conf 并 reload"
  else
    fail "nginx -t 现有配置不通过——先修复存量配置，否则任何 reload 都会牵连主站与 SUB2API"
  fi
else
  fail "nginx 不可用——公网入口依赖宿主机 Nginx 反代，请先确认其安装与运行"
fi

# --- certbot：缺失不阻断（bootstrap 会跳过 HTTPS 并保留 http-only 配置）---
if command -v certbot >/dev/null 2>&1; then
  ok "certbot 已安装：$(certbot --version 2>&1 | head -n1)"
else
  warn "certbot 未安装——bootstrap 将跳过 HTTPS（安装：apt-get update && apt-get install -y certbot）"
fi

# --- 端口占用：web/admin 宿主回环端口必须空闲或已由本项目对应 service 占用 ---
existing_service_owns_port() {  # $1=service $2=port
  local role="$1" port="$2" cid
  [ -f "${APP_DIR}/compose.yaml" ] || return 1
  cid="$(docker compose -f "${APP_DIR}/compose.yaml" ps -q "$role" 2>/dev/null | head -n1)"
  [ -n "$cid" ] || return 1
  [ "$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null)" = "true" ] || return 1
  case "$role" in
    web)   grep -Fq "127.0.0.1:${port}:8080" "${APP_DIR}/compose.yaml" ;;
    admin) grep -Fq -- "--port ${port}" "${APP_DIR}/compose.yaml" ;;
    *)     return 1 ;;
  esac
}

check_port() {
  local port="$1" role="$2" line proc
  if ! command -v ss >/dev/null 2>&1; then
    warn "无 ss 命令，端口 ${port} 占用情况未检测——请人工确认"
    return
  fi
  line="$(ss -lntp 2>/dev/null | awk -v p="$port" '$4 ~ (":" p "$")' | head -n1)"
  if [ -z "$line" ]; then
    ok "${role} 端口 ${port} 空闲——容器可绑定 127.0.0.1:${port}"
    return
  fi
  if existing_service_owns_port "$role" "$port"; then
    ok "${role} 端口 ${port} 由本项目现有 ${role} service 占用——幂等重跑正常"
    return
  fi
  proc="$(printf '%s\n' "$line" | grep -oE '"[^"]+"' | head -n1 | tr -d '"')"
  fail "${role} 端口 ${port} 被 ${proc:-未知进程} 占用且不属于本项目 ${role} service——请先释放该端口"
}
check_port "$WEB_PORT" web
check_port "$ADMIN_PORT" admin

# --- 部署目录现状：存在与否都不阻断，只影响 bootstrap 是全新安装还是复用 ---
if [ -d "$APP_DIR" ]; then
  ok "${APP_DIR} 已存在——bootstrap 为幂等脚本，将复用现有目录与配置"
else
  ok "${APP_DIR} 不存在——bootstrap 将创建（全新部署）"
fi

# --- DNS 指向：对比本机公网 IP 与域名解析；用 ahosts 取全部 A/AAAA 避免只见 IPv6 的误报 ---
PUB_IP="$(curl -s --max-time 10 ifconfig.me 2>/dev/null || true)"
RESOLVED="$(getent ahosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
if ! printf '%s' "$PUB_IP" | grep -qE '^[0-9a-fA-F:.]+$'; then
  warn "无法经 ifconfig.me 获取本机公网 IP——DNS 指向核对跳过，请人工比对 ${DOMAIN}"
elif [ -z "$RESOLVED" ]; then
  warn "${DOMAIN} 解析不到任何地址（getent 无结果）——请检查 DNS 记录，HTTPS 签发会失败"
elif printf '%s\n' $RESOLVED | grep -qxF "$PUB_IP"; then
  ok "${DOMAIN} 解析到本机（${PUB_IP}）——certbot webroot 验证可行"
else
  warn "${DOMAIN} 解析为 [${RESOLVED% }]，与本机公网 IP ${PUB_IP} 不符——先改 DNS 否则 HTTPS 签发失败"
fi

# --- GHCR 连通性：只测 HTTPS 可达（返回 401 也算通）；授权检查在 bootstrap 里做 ---
if curl -sI --max-time 10 https://ghcr.io/v2/ >/dev/null 2>&1; then
  ok "ghcr.io HTTPS 可达——可拉取 ghcr.io/${OWNER}/news-digest-{worker,web} 镜像"
else
  fail "无法访问 https://ghcr.io ——检查出网、防火墙与 DNS，否则镜像拉取必失败"
fi

echo "----------------------------------------------------------------"
echo "体检汇总：OK ${OK_N} 项；警告 ${WARN_N} 项；缺失 ${FAIL_N} 项"
if [ "$FAIL_N" -gt 0 ]; then
  echo "结论：存在缺失（阻断）项——请先按上面建议处理，再执行 bootstrap.sh"
  exit 1
elif [ "$WARN_N" -gt 0 ]; then
  echo "结论：可以执行 bootstrap.sh，但请先复核上述警告项"
else
  echo "结论：一切就绪，可以执行 bootstrap.sh"
fi
exit 0
