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
# 本脚本只做编排：体检（preflight.sh）→ 部署（bootstrap.sh）；
# 密钥仍遵循既有约束——首跑会生成 /srv/news-digest/.env 模板并暂停，
# 在服务器上填好真实值后重跑即可续接。
#
# 参数化部署：以下 ND_* 环境变量在执行前 export 即可覆盖默认值（bootstrap.sh 读取，
# 经 exec 链自动透传），换域名/换端口/换目录部署不用改任何脚本：
#   ND_OWNER          GHCR 命名空间（全小写）        默认 yi-lings
#   ND_VERSION        镜像 tag                      默认 v1.0.0
#   ND_APP_DIR        服务器部署目录                 默认 /srv/news-digest
#   ND_DOMAIN         站点域名（nginx/certbot/站点URL） 默认 news.cheapcoding.top
#   ND_WEB_PORT       web 宿主回环端口               默认 8618
#   ND_ADMIN_PORT     模型切换面板宿主回环端口        默认 8619
#   ND_CERTBOT_EMAIL  证书到期通知邮箱               默认 1481835649@qq.com
# 例：export ND_DOMAIN=news.example.com ND_WEB_PORT=9000 后再执行本脚本。
# 注意：preflight.sh 为只读体检，目前仍按默认值核对（域名/8618 端口等）——
# 覆盖 ND_* 后个别体检项可能误报，属提示性质，可人工确认后继续。
set -u

OWNER_REPO="Yi-Lings/news-digest"
WORK_DIR="/tmp/news-digest-deploy"

here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"

if [[ -z "$here" || ! -f "$here/bootstrap.sh" ]]; then
    # ---- 下载模式：取最新 Release 的 news-digest-deploy.tgz ----
    if [[ -z "${GH_TOKEN:-}" ]]; then
        echo "缺少 GH_TOKEN（私有仓库需要 repo 只读 token）。" >&2
        echo "  export GH_TOKEN=xxx 后重试；仓库公开后此要求自动消失。" >&2
        exit 1
    fi
    auth=(-H "Authorization: Bearer $GH_TOKEN")
    api="https://api.github.com/repos/$OWNER_REPO/releases/latest"
    echo "下载最新部署包（$OWNER_REPO releases/latest）..."
    json="$(curl -fsSL "${auth[@]}" "$api")" || { echo "读取 Release 失败" >&2; exit 1; }
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
    rm -rf "$WORK_DIR" && mkdir -p "$WORK_DIR"
    curl -fsSL "${auth[@]}" -H "Accept: application/octet-stream" \
        -o "$WORK_DIR/bundle.tgz" "$asset_url" || { echo "下载附件失败" >&2; exit 1; }
    tar -xzf "$WORK_DIR/bundle.tgz" -C "$WORK_DIR" || { echo "解包失败" >&2; exit 1; }
    exec bash "$WORK_DIR/deploy/install.sh"
fi

# ---- 本地模式：体检 → 部署 ----
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
