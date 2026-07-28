#!/usr/bin/env bash
# news-digest 一键部署入口（服务器端，root 执行）。两种用法：
#
# A. 远程一键（私有仓库需要 GH_TOKEN，具备 repo 只读权限；仓库转公开后可去掉认证头）：
#      export GH_TOKEN=你的token
#      bash <(curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
#        https://raw.githubusercontent.com/Yi-Lings/news-digest/main/deploy/install.sh)
#    自动下载最新 Release 的部署包并续跑本脚本。
#    注：raw 域名 + Bearer 仅对 classic PAT 可靠；fine-grained token 用官方 contents API：
#      bash <(curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
#        -H "Accept: application/vnd.github.raw" \
#        "https://api.github.com/repos/Yi-Lings/news-digest/contents/deploy/install.sh?ref=main")
#
# B. 已解包目录内直接执行：
#      sudo bash deploy/install.sh
#
# 本脚本只做编排：体检（preflight.sh）→ 部署（bootstrap.sh）。首次部署生成关闭状态的
# 服务器配置并直接完成；API/SMTP 在 Admin 后配，部署入口不接收密钥，也不运行 worker。
#
# 参数化部署：以下 ND_* 环境变量在执行前 export（bootstrap.sh 读取，
# 经 exec 链自动透传），换域名/换端口/换目录部署不用改任何脚本：
#   ND_OWNER          GHCR 命名空间（全小写）        必填
#   ND_VERSION        Release tag（下载模式从 Release 元数据取得；本地模式必填）
#   ND_WORKER_DIGEST  worker 不可变镜像 digest      Release 自动提供
#   ND_WEB_DIGEST     web 不可变镜像 digest         Release 自动提供
#   ND_APP_DIR        服务器部署目录                 必填
#   ND_DOMAIN         站点域名（nginx/certbot/站点URL） 必填
#   ND_WEB_PORT       web 宿主回环端口               默认 8618
#   ND_ADMIN_PORT     模型切换面板宿主回环端口        默认 8619
#   ND_CERTBOT_EMAIL  证书到期通知邮箱               必填
# 例：export ND_OWNER=example ND_APP_DIR=/opt/news-digest \
#     ND_DOMAIN=news.example.com ND_CERTBOT_EMAIL=ops@example.com
set -u

for required_name in ND_OWNER ND_APP_DIR ND_DOMAIN ND_CERTBOT_EMAIL; do
    if [[ -z "${!required_name:-}" ]]; then
        echo "缺少 $required_name；部署目标必须由操作员显式提供。" >&2
        exit 1
    fi
done
if [[ ! "$ND_OWNER" =~ ^[a-z0-9][a-z0-9-]*$ ]] ||
   [[ ! "$ND_APP_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || [[ "$ND_APP_DIR/" == *"//"* ]] ||
   [[ "$ND_APP_DIR/" == *"/./"* ]] || [[ "$ND_APP_DIR/" == *"/../"* ]] ||
   [[ ! "$ND_DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]] ||
   [[ ! "$ND_CERTBOT_EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$ ]]; then
    echo "部署目标格式非法。" >&2
    exit 1
fi
install_web_port="${ND_WEB_PORT:-8618}"
install_admin_port="${ND_ADMIN_PORT:-8619}"
for port_value in "$install_web_port" "$install_admin_port"; do
    if [[ ! "$port_value" =~ ^[1-9][0-9]{0,4}$ ]] || (( port_value > 65535 )); then
        echo "部署端口非法：$port_value" >&2
        exit 1
    fi
done
if [[ "$install_web_port" == "$install_admin_port" ]]; then
    echo "ND_WEB_PORT 与 ND_ADMIN_PORT 不得相同。" >&2
    exit 1
fi

OWNER_REPO="${ND_OWNER}/news-digest"

here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"

if [[ -z "$here" || ! -f "$here/bootstrap.sh" ]]; then
    # ---- 下载模式：取最新 Release 的 news-digest-deploy.tgz ----
    auth=()
    if [[ -n "${GH_TOKEN:-}" ]]; then
        auth=(-H "Authorization: Bearer $GH_TOKEN")
    fi
    api="https://api.github.com/repos/$OWNER_REPO/releases/latest"
    echo "下载最新部署包（$OWNER_REPO releases/latest）..."
    json="$(curl -fsSL "${auth[@]}" "$api")" || { echo "读取 Release 失败" >&2; exit 1; }
    release_tag="$(printf '%s' "$json" | tr -d '\n' \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    if [[ ! "$release_tag" =~ ^v[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}$ ]]; then
        echo "Release tag_name 缺失或非法。" >&2
        exit 1
    fi
    # 解析 news-digest-deploy.tgz 的 asset API 地址（私有仓库必须走 asset API + octet-stream）。
    # 按 { 切块使同一附件的 name 与 url 同行，从而按附件名精确过滤，
    # 将来 Release 追加其他附件（校验和等）也不会取错。
    asset_url="$(printf '%s' "$json" | tr -d '\n' | tr '{' '\n' \
        | grep '"name": *"news-digest-deploy\.tgz"' \
        | grep -o 'https://api\.github\.com/repos/[^"]*/assets/[0-9]*' \
        | head -1)"
    if [[ -z "$asset_url" ]]; then
        echo "最新 Release 没有部署包附件（news-digest-deploy.tgz）。" >&2
        echo "先推送一个 v* 版本标签让 CI 生成，或改用仓库内 deploy/ 目录部署。" >&2
        exit 1
    fi
    checksum_url="$(printf '%s' "$json" | tr -d '\n' | tr '{' '\n' \
        | grep '"name": *"news-digest-deploy\.tgz\.sha256"' \
        | grep -o 'https://api\.github\.com/repos/[^\"]*/assets/[0-9]*' \
        | head -1)"
    if [[ -z "$checksum_url" ]]; then
        echo "最新 Release 缺少部署包 SHA-256 附件。" >&2
        exit 1
    fi
    work_dir="$(mktemp -d)" || { echo "无法创建临时目录" >&2; exit 1; }
    trap 'rm -rf -- "$work_dir"' EXIT
    curl -fsSL "${auth[@]}" -H "Accept: application/octet-stream" \
        -o "$work_dir/news-digest-deploy.tgz" "$asset_url" || { echo "下载附件失败" >&2; exit 1; }
    curl -fsSL "${auth[@]}" -H "Accept: application/octet-stream" \
        -o "$work_dir/news-digest-deploy.tgz.sha256" "$checksum_url" || { echo "下载校验和失败" >&2; exit 1; }
    (cd "$work_dir" && sha256sum -c news-digest-deploy.tgz.sha256) || {
        echo "部署包 SHA-256 校验失败" >&2
        exit 1
    }
    while IFS= read -r entry; do
        case "$entry" in
            /*|../*|*/../*|*/..)
                echo "部署包包含越界路径，拒绝解包。" >&2
                exit 1
                ;;
        esac
    done < <(tar -tzf "$work_dir/news-digest-deploy.tgz")
    tar --no-same-owner --no-same-permissions -xzf "$work_dir/news-digest-deploy.tgz" \
        -C "$work_dir" || { echo "解包失败" >&2; exit 1; }
    export ND_VERSION="$release_tag"
    bash "$work_dir/deploy/install.sh"
    exit $?
fi

# ---- 本地模式：体检 → 部署 ----
# Release bundle includes the exact build outputs. Parse it as data rather than sourcing
# shell code, and refuse partial/conflicting pins. A source checkout has no such file and
# continues to honor explicitly supplied ND_* values.
if [[ -f "$here/digests.env" ]]; then
    bundle_version=""
    bundle_worker_digest=""
    bundle_web_digest=""
    seen_version=0
    seen_worker=0
    seen_web=0
    while IFS='=' read -r key value; do
        case "$key" in
            ND_VERSION)
                ((seen_version++))
                bundle_version="$value"
                ;;
            ND_WORKER_DIGEST)
                ((seen_worker++))
                bundle_worker_digest="$value"
                ;;
            ND_WEB_DIGEST)
                ((seen_web++))
                bundle_web_digest="$value"
                ;;
            *)
                echo "digests.env 含未知或非法字段：$key" >&2
                exit 1
                ;;
        esac
    done < "$here/digests.env"
    if ((seen_version != 1 || seen_worker != 1 || seen_web != 1)); then
        echo "digests.env 必须且只能各含一个版本与两个镜像 digest。" >&2
        exit 1
    fi
    if [[ ! "$bundle_version" =~ ^v[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}$ ]] ||
       [[ ! "$bundle_worker_digest" =~ ^sha256:[0-9a-f]{64}$ ]] ||
       [[ ! "$bundle_web_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "digests.env 的版本或 digest 格式非法。" >&2
        exit 1
    fi
    if [[ -n "${ND_VERSION:-}" && "$ND_VERSION" != "$bundle_version" ]]; then
        echo "ND_VERSION 与部署包版本不一致，拒绝混用发布工件。" >&2
        exit 1
    fi
    export ND_VERSION="$bundle_version"
    export ND_WORKER_DIGEST="$bundle_worker_digest"
    export ND_WEB_DIGEST="$bundle_web_digest"
fi
if [[ -z "${ND_VERSION:-}" || -z "${ND_WORKER_DIGEST:-}" || -z "${ND_WEB_DIGEST:-}" ]]; then
    echo "本地部署必须提供同一 Release 的 ND_VERSION、ND_WORKER_DIGEST 与 ND_WEB_DIGEST；禁止按 tag 回退。" >&2
    exit 1
fi

echo "== news-digest 一键部署 =="
bash "$here/preflight.sh"
status=$?
if [[ $status -ne 0 ]]; then
    echo ""
    # preflight 仅在存在「缺失（阻断）」项时非零退出；纯警告不会走到这里
    read -rp "体检存在缺失（阻断）项，仍要强行继续部署吗？[y/N] " answer
    [[ "${answer,,}" == y* ]] || exit 1
fi
exec bash "$here/bootstrap.sh"
