#!/usr/bin/env bash
# Shared, read-only deployment guards. ND_DOMAIN is the only site identity.

nd_managed_conf() { printf '%s/news-digest.conf' "${ND_NGINX_CONF_DIR:-/etc/nginx/conf.d}"; }
nd_legacy_conf() { printf '%s/news.conf' "${ND_NGINX_CONF_DIR:-/etc/nginx/conf.d}"; }
nd_managed_hook() { printf '%s/news-digest-reload-nginx.sh' "${ND_CERTBOT_HOOK_DIR:-/etc/letsencrypt/renewal-hooks/deploy}"; }
nd_legacy_hook() { printf '%s/10-reload-nginx.sh' "${ND_CERTBOT_HOOK_DIR:-/etc/letsencrypt/renewal-hooks/deploy}"; }

nd_check_saved_domain() {
  local env_file="${ND_APP_DIR}/config/.env" saved host
  [ -f "$env_file" ] || env_file="${ND_APP_DIR}/.env"
  [ -f "$env_file" ] || return 0
  saved="$(sed -n 's/^[[:space:]]*NEWS_SITE_URL=//p' "$env_file" | head -n1 | tr -d '\r')"
  saved="${saved%\"}"; saved="${saved#\"}"
  saved="${saved%\'}"; saved="${saved#\'}"
  if [[ ! "$saved" =~ ^https?://([^/:]+)(:[0-9]+)?/?$ ]]; then
    echo "NEWS_SITE_URL 缺失或不是根域名 URL；拒绝覆盖已有部署：$env_file" >&2
    return 1
  fi
  host="${BASH_REMATCH[1]}"
  if [ "${host,,}" != "${ND_DOMAIN,,}" ]; then
    echo "ND_DOMAIN=$ND_DOMAIN 与现有 NEWS_SITE_URL=$saved 不一致；本版不迁移域名" >&2
    return 1
  fi
}

nd_is_managed_conf() {
  [ -f "$1" ] && grep -Fxq "# Managed by news-digest; ND_DOMAIN=${ND_DOMAIN}" "$1"
}

nd_conf_server_names() {
  awk '
    /server_name[[:space:]]/ {
      line=$0; sub(/#.*/, "", line)
      if (line !~ /(^|[;{}[:space:]])server_name[[:space:]]+/) next
      sub(/.*server_name[[:space:]]+/, "", line); sub(/;.*/, "", line)
      n=split(line, names, /[[:space:]]+/)
      for (i=1; i<=n; i++) if (names[i] != "") print names[i]
    }' "$1"
}

nd_conf_domains_match() {
  local names name
  names="$(nd_conf_server_names "$1")"
  [ -n "$names" ] || return 1
  while IFS= read -r name; do
    [ "${name,,}" = "${ND_DOMAIN,,}" ] || return 1
  done <<< "$names"
}

nd_looks_like_legacy_conf() {
  [ -f "$1" ] &&
    { grep -Fq '—— 宿主机 Nginx 反向代理模板' "$1" &&
      grep -Fq 'zone=news_ratelimit:' "$1" &&
      grep -Fq 'location /admin/' "$1"; } ||
    { [ -f "$1" ] &&
      grep -Fq '—— http-only 首版（bootstrap 自动生成；证书签发成功后被完整版覆盖）' "$1" &&
      grep -Fq 'location /.well-known/acme-challenge/' "$1"; }
}

nd_is_legacy_conf() {
  nd_looks_like_legacy_conf "$1" && nd_conf_domains_match "$1" &&
    { grep -Fxq "# ${ND_DOMAIN} —— 宿主机 Nginx 反向代理模板" "$1" ||
      grep -Fxq "# ${ND_DOMAIN} —— http-only 首版（bootstrap 自动生成；证书签发成功后被完整版覆盖）" "$1"; }
}

nd_check_hook_ownership() {
  local hook="$(nd_managed_hook)" old_hook="$(nd_legacy_hook)"
  if [ -e "$hook" ] && ! grep -Fxq '# Managed by news-digest; Certbot renewal hook' "$hook"; then
    echo "拒绝覆盖非本项目的 Certbot hook：$hook" >&2
    return 1
  fi
  if [ -e "$hook" ] && nd_is_legacy_hook "$old_hook"; then
    echo "项目新旧 Certbot hook 同时存在；请先核对并只保留一个" >&2
    return 1
  fi
}

nd_is_legacy_hook() {
  [ -f "$1" ] && [ "$(wc -l < "$1")" -le 5 ] &&
    grep -Fq 'certbot 在证书成功续期后自动调用' "$1" &&
    grep -Fxq 'nginx -t && systemctl reload nginx' "$1"
}

nd_archive_legacy_hook() {
  local old_hook="$1" archive="$2"
  nd_is_legacy_hook "$old_hook" || return 0
  [ ! -e "$archive" ] || { echo "拒绝覆盖旧 hook 备份：$archive" >&2; return 1; }
  install -d -m 700 "$(dirname "$archive")"
  mv "$old_hook" "$archive"
}

nd_has_owned_https() {
  local managed="$(nd_managed_conf)" legacy="$(nd_legacy_conf)"
  { nd_is_managed_conf "$managed" &&
    grep -Eq '^[[:space:]]*listen[[:space:]]+443([[:space:]]|;)' "$managed"; } ||
  { nd_is_legacy_conf "$legacy" &&
    grep -Eq '^[[:space:]]*listen[[:space:]]+443([[:space:]]|;)' "$legacy"; }
}

nd_check_nginx_ownership() {
  local managed legacy allowed_legacy dump conflicts
  managed="$(nd_managed_conf)"; legacy="$(nd_legacy_conf)"
  if [ -e "$managed" ] &&
     { ! nd_is_managed_conf "$managed" || ! nd_conf_domains_match "$managed"; }; then
    echo "拒绝覆盖不属于 ND_DOMAIN=$ND_DOMAIN 的 $managed" >&2
    return 1
  fi
  if nd_looks_like_legacy_conf "$legacy" && ! nd_is_legacy_conf "$legacy"; then
    echo "旧版项目配置 $legacy 的域名与 ND_DOMAIN=$ND_DOMAIN 不一致" >&2
    return 1
  fi
  if [ -e "$managed" ] && nd_is_legacy_conf "$legacy"; then
    echo "同时存在新旧站点配置；请先核对，避免重复 server_name" >&2
    return 1
  fi
  allowed_legacy=""
  nd_is_legacy_conf "$legacy" && allowed_legacy="$legacy"
  dump="$(nginx -T 2>&1)" || { printf '%s\n' "$dump" >&2; return 1; }
  if printf '%s\n' "$dump" | grep -qi 'conflicting server name'; then
    echo "nginx 已报告重复 server_name；拒绝部署" >&2
    return 1
  fi
  conflicts="$(printf '%s\n' "$dump" | awk -v domain="${ND_DOMAIN,,}" -v managed="$managed" -v legacy="$allowed_legacy" '
    /^# configuration file .*:$/ { file=$0; sub(/^# configuration file /,"",file); sub(/:$/,"",file); next }
    /server_name[[:space:]]/ {
      line=$0; sub(/#.*/, "", line)
      if (line !~ /(^|[;{}[:space:]])server_name[[:space:]]+/) next
      sub(/.*server_name[[:space:]]+/, "", line); sub(/;.*/, "", line)
      gsub(/[[:space:]]+/, " ", line)
      n=split(line, words, " ")
      for (i=1; i<=n; i++)
        if (tolower(words[i]) == domain && file != managed && file != legacy) print file
    }' | sort -u)"
  if [ -n "$conflicts" ]; then
    printf 'ND_DOMAIN=%s 已由其他 Nginx 配置声明：%s\n' "$ND_DOMAIN" "$conflicts" >&2
    return 1
  fi
}

nd_check_certificate() {
  local cert_dir="${ND_CERT_DIR:-/etc/letsencrypt/live/${ND_DOMAIN}}"
  [ -e "$cert_dir/fullchain.pem" ] || [ -e "$cert_dir/privkey.pem" ] || return 2
  if [ ! -s "$cert_dir/fullchain.pem" ] || [ ! -s "$cert_dir/privkey.pem" ] ||
     ! nd_cert_san_matches "$cert_dir/fullchain.pem" ||
     ! openssl x509 -in "$cert_dir/fullchain.pem" -noout -checkhost "$ND_DOMAIN" 2>/dev/null |
       grep -Fq "Hostname $ND_DOMAIN does match certificate" ||
     ! openssl x509 -in "$cert_dir/fullchain.pem" -noout -checkend 0 >/dev/null 2>&1 ||
     ! openssl pkey -in "$cert_dir/privkey.pem" -noout >/dev/null 2>&1; then
    echo "证书不完整、已过期或 SAN 不匹配 ND_DOMAIN=$ND_DOMAIN：$cert_dir" >&2
    return 1
  fi
  return 0
}

nd_cert_san_matches() {
  local cert="$1" entry suffix prefix
  while IFS= read -r entry; do
    entry="${entry,,}"
    if [ "$entry" = "${ND_DOMAIN,,}" ]; then return 0; fi
    if [[ "$entry" == \*.* ]]; then
      suffix="${entry#*.}"
      if [[ "${ND_DOMAIN,,}" == *."$suffix" ]]; then
        prefix="${ND_DOMAIN,,}"; prefix="${prefix%."$suffix"}"
        [[ -n "$prefix" && "$prefix" != *.* ]] && return 0
      fi
    fi
  done < <(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null |
    tr ',' '\n' | sed -n 's/^[[:space:]]*DNS://p')
  return 1
}

nd_nginx_test() {
  local output
  output="$(nginx -t 2>&1)" || { printf '%s\n' "$output" >&2; return 1; }
  if printf '%s\n' "$output" | grep -qi 'conflicting server name'; then
    printf '%s\n' "$output" >&2
    return 1
  fi
}

nd_verify_site() {
  local mode="$1" port scheme path expected code
  case "$mode" in
    https) scheme=https; port="${ND_GATE_HTTPS_PORT:-443}" ;;
    http) scheme=http; port="${ND_GATE_HTTP_PORT:-80}" ;;
    *) echo "无效的验收模式：$mode" >&2; return 1 ;;
  esac
  for path in / /healthz /admin/; do
    expected=200
    if [ "$mode" = http ] && [ "$path" = /admin/ ]; then expected=404; fi
    code="$(curl --silent --show-error --noproxy '*' --resolve "${ND_DOMAIN}:${port}:127.0.0.1" \
      --max-time 15 --output /dev/null --write-out '%{http_code}' \
      "${scheme}://${ND_DOMAIN}:${port}${path}")" || return 1
    if [ "$code" != "$expected" ]; then
      echo "${scheme}://${ND_DOMAIN}${path} 返回 $code，期望 $expected" >&2
      return 1
    fi
  done
  if [ "$mode" = http ]; then
    for path in /login /register /payment/return /subscribe/confirm/token; do
      code="$(curl --silent --show-error --noproxy '*' --resolve "${ND_DOMAIN}:${port}:127.0.0.1" \
        --max-time 15 --output /dev/null --write-out '%{http_code}' \
        "http://${ND_DOMAIN}:${port}${path}")" || return 1
      if [ "$code" != 404 ]; then
        echo "HTTP-only 敏感 GET ${path} 返回 $code，期望 404" >&2
        return 1
      fi
    done
    for path in / /login /register /order; do
      code="$(curl --silent --show-error --noproxy '*' --resolve "${ND_DOMAIN}:${port}:127.0.0.1" \
        --max-time 15 --request POST --output /dev/null --write-out '%{http_code}' \
        "http://${ND_DOMAIN}:${port}${path}")" || return 1
      if [ "$code" != 405 ]; then
        echo "HTTP-only 写请求 POST ${path} 返回 $code，期望 405" >&2
        return 1
      fi
    done
  fi
  echo "${scheme}://${ND_DOMAIN}/ 本机 SNI、证书和三条 GET 路径验收通过"
}

# A restored pre-t35 HTTP-only config may permit account URLs. During rollback,
# verify only the old site's availability and Admin isolation; new installs must
# still pass nd_verify_site http with its stricter read-only checks.
nd_verify_restored_http() {
  local port="${ND_GATE_HTTP_PORT:-80}" path expected code
  for path in / /healthz /admin/; do
    expected=200
    [ "$path" = /admin/ ] && expected=404
    code="$(curl --silent --show-error --noproxy '*' --resolve "${ND_DOMAIN}:${port}:127.0.0.1" \
      --max-time 15 --output /dev/null --write-out '%{http_code}' \
      "http://${ND_DOMAIN}:${port}${path}")" || return 1
    if [ "$code" != "$expected" ]; then
      echo "恢复的 HTTP 站点 ${path} 返回 $code，期望 $expected" >&2
      return 1
    fi
  done
  echo "恢复的 HTTP 站点首页、健康入口与 Admin 隔离验收通过"
}

nd_verify_site_auto() {
  local managed="$(nd_managed_conf)"
  nd_is_managed_conf "$managed" || { echo "缺少本项目托管的 Nginx 配置：$managed" >&2; return 1; }
  if grep -Eq '^[[:space:]]*listen[[:space:]]+[^;]+[[:space:]]ssl([[:space:]]|;)' "$managed"; then
    nd_verify_site https
  else
    nd_verify_site http
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  [ "${1:-}" = verify-auto ] && [ -n "${ND_DOMAIN:-}" ] ||
    { echo '用法：ND_DOMAIN=<domain> bash site-gate.sh verify-auto' >&2; exit 1; }
  nd_verify_site_auto
fi
