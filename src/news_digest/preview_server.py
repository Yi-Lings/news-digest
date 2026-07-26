"""本地预览服务器：静态站点 + 模型供应商切换面板（仅绑定 127.0.0.1）。

仅用于本机开发预览，生产环境不部署本模块。
供应商档案存于 .env.providers.local，启用时改写 .env.local 的三个 TRANSLATION_* 值；
两个文件均被 .gitignore 排除，接口返回的 api_key 一律掩码。
"""

import hashlib
import hmac
import json
import secrets
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROFILES_FILE = ".env.providers.local"
ENV_FILE = ".env.local"
_ENV_KEYS = ("TRANSLATION_API_BASE_URL", "TRANSLATION_API_KEY", "TRANSLATION_MODEL")

_APR1_CHARS = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _to64(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_APR1_CHARS[value & 0x3F])
        value >>= 6
    return "".join(out)


def apr1_hash(password: str, salt: str | None = None) -> str:
    """Apache MD5（$apr1$）：nginx basic auth 原生支持，纯标准库实现（crypt 已从 3.13 移除）。"""
    salt = salt or "".join(secrets.choice(_APR1_CHARS) for _ in range(8))
    pw, s = password.encode(), salt.encode()
    ctx = hashlib.md5(pw + b"$apr1$" + s)
    alt = hashlib.md5(pw + s + pw).digest()
    remaining = len(pw)
    while remaining > 0:
        ctx.update(alt[: min(16, remaining)])
        remaining -= 16
    bit = len(pw)
    while bit:
        ctx.update(b"\0" if bit & 1 else pw[:1])
        bit >>= 1
    final = ctx.digest()
    for round_number in range(1000):
        step = hashlib.md5()
        step.update(pw if round_number & 1 else final)
        if round_number % 3:
            step.update(s)
        if round_number % 7:
            step.update(pw)
        step.update(final if round_number & 1 else pw)
        final = step.digest()
    f = final
    encoded = (
        _to64(f[0] << 16 | f[6] << 8 | f[12], 4)
        + _to64(f[1] << 16 | f[7] << 8 | f[13], 4)
        + _to64(f[2] << 16 | f[8] << 8 | f[14], 4)
        + _to64(f[3] << 16 | f[9] << 8 | f[15], 4)
        + _to64(f[4] << 16 | f[10] << 8 | f[5], 4)
        + _to64(f[11], 2)
    )
    return f"$apr1${salt}${encoded}"


def load_profiles(root: Path, filename: str = PROFILES_FILE) -> dict:
    path = root / filename
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"active": "", "providers": {}}


def save_profiles(root: Path, data: dict, filename: str = PROFILES_FILE) -> None:
    (root / filename).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return "已设置"
    return f"{key[:6]}…{key[-4:]}"


def write_env_local(root: Path, provider: dict, filename: str = ENV_FILE) -> None:
    """把供应商三项写入环境文件（默认 .env.local），保留文件中其余行不动。"""
    values = {
        "TRANSLATION_API_BASE_URL": provider["base_url"],
        "TRANSLATION_API_KEY": provider["api_key"],
        "TRANSLATION_MODEL": provider["model"],
    }
    path = root / filename
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    out, seen = [], set()
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if not stripped.startswith("#") and key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key in _ENV_KEYS:
        if key not in seen:
            out.append(f"{key}={values[key]}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


_SESSION_TTL_SECONDS = 7 * 24 * 3600
_SESSION_COOKIE = "nd_admin_session"


def _session_secret(secret_file: Path) -> bytes:
    """会话签名密钥：首次生成随机 32 字节；改密时删除本文件即令全部会话失效。"""
    if secret_file.is_file():
        return secret_file.read_bytes()
    secret = secrets.token_bytes(32)
    secret_file.write_bytes(secret)
    secret_file.chmod(0o600)
    return secret


def _sign_session(secret: bytes, username: str, expires_at: int) -> str:
    payload = f"{username}|{expires_at}"
    digest = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{digest}"


def _verify_session(secret: bytes, token: str) -> bool:
    parts = token.split("|")
    if len(parts) != 3:
        return False
    username, expires_at, digest = parts
    try:
        if int(expires_at) < time.time():
            return False
    except ValueError:
        return False
    expected = hmac.new(secret, f"{username}|{expires_at}".encode(), hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), digest)


def verify_htpasswd(htpasswd_file: Path, username: str, password: str) -> bool:
    """校验 user:$apr1$salt$hash 首行；恒定时间比较。"""
    if not htpasswd_file.is_file():
        return False
    lines = htpasswd_file.read_text(encoding="utf-8").splitlines()
    if not lines or ":" not in lines[0]:
        return False
    stored_user, stored_hash = lines[0].split(":", 1)
    parts = stored_hash.split("$")
    if len(parts) != 4 or parts[1] != "apr1":
        return False
    computed = apr1_hash(password, parts[2])
    return hmac.compare_digest(stored_user, username) and hmac.compare_digest(
        computed, stored_hash
    )


class PreviewHandler(SimpleHTTPRequestHandler):
    """静态站点 + /admin 面板与 JSON 接口。

    生产模式（allow_key_input=False）下密钥不经网页传输：
    只能切换预置档案、修改接口地址与模型名，新增密钥走服务器文件。
    """

    project_root: Path
    env_file: str = ENV_FILE
    profiles_file: str = PROFILES_FILE
    allow_key_input: bool = True
    serve_static: bool = True  # 生产 admin 模式必须为 False：/config 含明文密钥，绝不作 docroot
    htpasswd_file: Path | None = None  # 面板口令文件；None 时不提供网页改密

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        pass  # 本地预览不刷请求日志

    @property
    def _login_required(self) -> bool:
        return self.htpasswd_file is not None

    def _authed(self) -> bool:
        if not self._login_required:
            return True
        cookies = self.headers.get("Cookie", "")
        for chunk in cookies.split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == _SESSION_COOKIE and value:
                secret = _session_secret(self.htpasswd_file.parent / "session-secret")
                return _verify_session(secret, value)
        return False

    def _html(self, page: str) -> None:
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        if self.path in ("/admin", "/admin/"):
            if not self._authed():
                self._html(LOGIN_HTML)
                return
            flag = "true" if self.allow_key_input else "false"
            body = ADMIN_HTML.replace("__ALLOW_KEY_INPUT__", flag).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/admin/api/providers":
            if not self._authed():
                self._json(401, {"error": "未登录"})
                return
            data = load_profiles(self.project_root, self.profiles_file)
            masked = {
                "active": data["active"],
                "can_change_password": self.htpasswd_file is not None,
                "providers": {
                    name: {**p, "api_key": mask_key(p.get("api_key", ""))}
                    for name, p in data["providers"].items()
                },
            }
            self._json(200, masked)
        elif self.serve_static:
            super().do_GET()
        else:
            self._json(404, {"error": "本服务只提供 /admin/ 面板"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        length = int(self.headers.get("Content-Length", 0))
        if length > 100_000:
            self._json(413, {"error": "请求过大"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "非法 JSON"})
            return

        if self.path == "/admin/api/login":
            self._handle_login(body)
            return
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        if self.path == "/admin/api/logout":
            self._set_session_cookie("", 0)
            self._json(200, {"ok": True})
            return
        if self.path == "/admin/api/password":
            self._handle_password(body)
            return
        data = load_profiles(self.project_root, self.profiles_file)
        if self.path == "/admin/api/providers":
            self._handle_save(body, data)
        elif self.path == "/admin/api/activate":
            self._handle_activate(body, data)
        else:
            self._json(404, {"error": "未知接口"})

    def _set_session_cookie(self, token: str, max_age: int) -> None:
        cookie = (
            f"{_SESSION_COOKIE}={token}; Max-Age={max_age}; Path=/; "
            "HttpOnly; SameSite=Strict; Secure"
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", cookie)
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_login(self, body: dict) -> None:
        if not self._login_required:
            self._json(400, {"error": "本模式无需登录"})
            return
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not verify_htpasswd(self.htpasswd_file, username, password):
            time.sleep(0.5)  # 恒定失败延迟，配合 Nginx 限速抑制爆破
            self._json(401, {"error": "用户名或口令不正确"})
            return
        secret = _session_secret(self.htpasswd_file.parent / "session-secret")
        expires_at = int(time.time()) + _SESSION_TTL_SECONDS
        token = _sign_session(secret, username, expires_at)
        self._set_session_cookie(token, _SESSION_TTL_SECONDS)

    def _handle_password(self, body: dict) -> None:
        """改面板口令：Basic Auth 已由外层 Nginx 逐请求校验，等价于旧口令验证。"""
        if self.htpasswd_file is None:
            self._json(404, {"error": "本模式不提供网页改密"})
            return
        password = str(body.get("password", ""))
        if len(password) < 8:
            self._json(400, {"error": "口令至少 8 位"})
            return
        username = "admin"
        if self.htpasswd_file.is_file():
            first_line = self.htpasswd_file.read_text(encoding="utf-8").splitlines()
            if first_line and ":" in first_line[0]:
                username = first_line[0].split(":", 1)[0] or "admin"
            # 原地截断写入：保住 inode 与属主/权限（nginx 读取组权限、可能的单文件 bind）
            with self.htpasswd_file.open("r+", encoding="utf-8") as handle:
                handle.seek(0)
                handle.write(f"{username}:{apr1_hash(password)}\n")
                handle.truncate()
        else:
            self.htpasswd_file.write_text(
                f"{username}:{apr1_hash(password)}\n", encoding="utf-8"
            )
            self.htpasswd_file.chmod(0o640)
        initial = self.htpasswd_file.parent / "admin-password.initial"
        if initial.is_file():
            initial.unlink()  # 初始口令文件在用户自设口令后即失效，顺手清除
        secret_file = self.htpasswd_file.parent / "session-secret"
        if secret_file.is_file():
            secret_file.unlink()  # 轮换会话密钥：改密后所有已登录端强制重新登录
        self._json(200, {"ok": True})

    def _handle_save(self, body: dict, data: dict) -> None:
        name = str(body.get("name", "")).strip()
        if not name:
            self._json(400, {"error": "名称不能为空"})
            return
        if body.get("delete"):
            data["providers"].pop(name, None)
            if data["active"] == name:
                data["active"] = ""
            save_profiles(self.project_root, data, self.profiles_file)
            self._json(200, {"ok": True})
            return
        existing = data["providers"].get(name, {})
        submitted_key = str(body.get("api_key", "")).strip()
        if submitted_key and not self.allow_key_input:
            message = "生产面板不接受密钥输入；新增/更换密钥请在服务器上编辑档案文件"
            self._json(400, {"error": message})
            return
        api_key = submitted_key or existing.get("api_key", "")
        provider = {
            "base_url": str(body.get("base_url", "")).strip().rstrip("/"),
            "api_key": api_key,
            "model": str(body.get("model", "")).strip(),
        }
        if not provider["base_url"] or not provider["model"] or not provider["api_key"]:
            message = "base_url、model、api_key 均不能为空（key 留空仅编辑既有档案时允许）"
            self._json(400, {"error": message})
            return
        data["providers"][name] = provider
        save_profiles(self.project_root, data, self.profiles_file)
        self._json(200, {"ok": True})

    def _handle_activate(self, body: dict, data: dict) -> None:
        name = str(body.get("name", "")).strip()
        provider = data["providers"].get(name)
        if provider is None:
            self._json(404, {"error": f"档案不存在：{name}"})
            return
        write_env_local(self.project_root, provider, self.env_file)
        data["active"] = name
        save_profiles(self.project_root, data, self.profiles_file)
        self._json(200, {"ok": True, "active": name})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    root: Path,
    site_dir: Path,
    port: int,
    *,
    env_file: str = ENV_FILE,
    profiles_file: str = PROFILES_FILE,
    allow_key_input: bool = True,
    serve_static: bool = True,
    htpasswd_file: Path | None = None,
) -> ThreadingHTTPServer:
    handler_class = partial(PreviewHandler, directory=str(site_dir))
    PreviewHandler.project_root = root
    PreviewHandler.env_file = env_file
    PreviewHandler.profiles_file = profiles_file
    PreviewHandler.allow_key_input = allow_key_input
    PreviewHandler.serve_static = serve_static
    PreviewHandler.htpasswd_file = htpasswd_file
    return ThreadingHTTPServer(("127.0.0.1", port), handler_class)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>登录 · Cheapcoding News 管理</title>
<style>
:root { color-scheme: light; }
body { margin:0; background:#fbfaf7; color:#1c1b17; min-height:100vh;
  display:flex; align-items:center; justify-content:center;
  font: 15px/1.7 "Microsoft YaHei UI","Microsoft YaHei","PingFang SC",sans-serif; }
.card { width:100%; max-width:22rem; padding:2rem 1.6rem 1.8rem; margin:1rem;
  border-top:3px double #1c1b17; border-bottom:3px double #1c1b17; }
h1 { font-family: Constantia,"Palatino Linotype",Georgia,serif; font-size:1.5rem;
  margin:0 0 .2rem; }
.sub { font-size:.75rem; color:#5c5a52; letter-spacing:3px; margin:0 0 1.4rem; }
label { display:block; margin:.7rem 0 .15rem; font-size:.8rem; color:#5c5a52; }
input { width:100%; box-sizing:border-box; font:inherit; font-size:.95rem;
  border:1px solid #ddd8cc; padding:.45rem .55rem; background:#fff; }
input:focus { outline:2px solid #ae2f24; outline-offset:1px; }
button { width:100%; margin-top:1.1rem; font:inherit; font-size:.9rem; cursor:pointer;
  background:#1c1b17; color:#fbfaf7; border:1px solid #1c1b17; padding:.55rem; }
button:hover { background:#ae2f24; border-color:#ae2f24; }
#status { min-height:1.3rem; margin-top:.7rem; font-size:.85rem; color:#ae2f24; }
</style>
</head>
<body>
<form class="card" id="form">
<h1>Cheapcoding News</h1>
<p class="sub">模型切换面板 · 管理登录</p>
<label for="u">用户名</label>
<input id="u" autocomplete="username" value="admin">
<label for="p">口令</label>
<input id="p" type="password" autocomplete="current-password" autofocus>
<button type="submit">登录</button>
<div id="status"></div>
</form>
<script>
"use strict";
document.getElementById("form").onsubmit = function (event) {
  event.preventDefault();
  var statusEl = document.getElementById("status");
  statusEl.textContent = "";
  fetch("/admin/api/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.getElementById("u").value,
      password: document.getElementById("p").value
    })
  }).then(function (response) {
    if (response.ok) { location.reload(); return; }
    return response.json().then(function (data) {
      statusEl.textContent = data.error || ("HTTP " + response.status);
    });
  }).catch(function () { statusEl.textContent = "网络错误，请重试"; });
};
</script>
</body>
</html>
"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>模型供应商 · Cheapcoding News 本地设置</title>
<style>
:root { color-scheme: light; }
body { margin:0; background:#fbfaf7; color:#1c1b17;
  font: 15px/1.7 "Microsoft YaHei UI","Microsoft YaHei","PingFang SC",sans-serif; }
.wrap { max-width: 46rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
h1 { font-family: Constantia,"Palatino Linotype",Georgia,serif; font-size:1.7rem;
  border-bottom:3px double #1c1b17; padding-bottom:.6rem; }
h1 small { font-size:.8rem; color:#5c5a52; font-family:inherit; margin-left:.6rem; }
.note { font-size:.8rem; color:#5c5a52; margin:.4rem 0 1.4rem; }
table { width:100%; border-collapse:collapse; margin-bottom:1.6rem; }
th { text-align:left; font-weight:400; font-size:.75rem; color:#5c5a52;
  border-bottom:1px solid #1c1b17; padding:.3rem .5rem .3rem 0; }
td { border-bottom:1px dotted #ddd8cc; padding:.5rem .5rem .5rem 0; vertical-align:middle; }
.active-mark { color:#ae2f24; font-weight:700; }
button { font:inherit; font-size:.8rem; cursor:pointer; background:none;
  border:1px solid #ddd8cc; padding:.2rem .6rem; }
button:hover { border-color:#1c1b17; }
button.primary { background:#1c1b17; color:#fbfaf7; border-color:#1c1b17; }
button.primary:hover { background:#ae2f24; border-color:#ae2f24; }
button.danger:hover { color:#ae2f24; border-color:#ae2f24; }
fieldset { border:1px solid #ddd8cc; padding:1rem 1.2rem; margin:0; }
legend { font-size:.8rem; color:#5c5a52; padding:0 .5rem; }
label { display:block; margin:.6rem 0 .15rem; font-size:.8rem; color:#5c5a52; }
input { width:100%; box-sizing:border-box; font:inherit; font-size:.9rem;
  border:1px solid #ddd8cc; padding:.35rem .5rem; background:#fff; }
input:focus { outline:2px solid #ae2f24; outline-offset:1px; }
#status { min-height:1.4rem; margin-top:.8rem; font-size:.85rem; }
#status.ok { color:#1c6b32; } #status.err { color:#ae2f24; }
.row { display:flex; gap:1rem; } .row > div { flex:1; }
.actions { margin-top:1rem; display:flex; gap:.6rem; }
code { background:#f4f1e8; padding:.05rem .3rem; font-size:.85em; }
</style>
</head>
<body>
<div class="wrap">
<h1>模型供应商<small>本地设置 · 仅 127.0.0.1 生效</small></h1>
<p class="note">启用某个档案会把它写入 <code>.env.local</code>，
随后的 <code>translate</code> 即使用该供应商。密钥仅存于本机，页面只显示掩码；
编辑已有档案时密钥留空表示沿用。缓存按模型隔离，切换互不影响。</p>

<table>
<thead><tr><th></th><th>名称</th><th>模型</th><th>接口</th><th>密钥</th><th></th></tr></thead>
<tbody id="list"><tr><td colspan="6">加载中…</td></tr></tbody>
</table>

<fieldset>
<legend>新增 / 编辑档案（同名即覆盖）</legend>
<p class="note" id="key-restricted-note" hidden>生产模式：此面板不接受密钥输入——
切换档案、改接口地址与模型名均可；新增供应商或更换密钥请在服务器上编辑档案文件
（见运维文档）。</p>
<label for="f-name">名称（如 claude、openai）</label>
<input id="f-name" autocomplete="off">
<label for="f-url">Base URL（通常以 /v1 结尾）</label>
<input id="f-url" autocomplete="off" placeholder="https://api.example.com/v1">
<div class="row">
<div><label for="f-model">模型名</label><input id="f-model" autocomplete="off"></div>
<div><label for="f-key">API Key（编辑时留空=沿用）</label>
<input id="f-key" type="password" autocomplete="off"></div>
</div>
<div class="actions">
<button class="primary" id="save">保存档案</button>
</div>
</fieldset>
<fieldset id="password-box" hidden style="margin-top:1.2rem;">
<legend>面板口令</legend>
<label for="f-pwd">新口令（至少 8 位；当前口令即浏览器登录所用）</label>
<input id="f-pwd" type="password" autocomplete="new-password">
<label for="f-pwd2">重复新口令</label>
<input id="f-pwd2" type="password" autocomplete="new-password">
<div class="actions"><button class="primary" id="save-pwd">修改口令</button></div>
</fieldset>
<div id="status"></div>
<p class="note"><a href="/">← 返回站点</a>
<a id="logout" href="#" hidden style="margin-left:1rem;color:#ae2f24;">退出登录</a></p>
</div>
<script>
"use strict";
var allowKeyInput = __ALLOW_KEY_INPUT__;
var listEl = document.getElementById("list");
var statusEl = document.getElementById("status");

if (!allowKeyInput) {
  document.getElementById("f-key").closest("div").hidden = true;
  document.getElementById("key-restricted-note").hidden = false;
}

function say(message, ok) {
  statusEl.textContent = message;
  statusEl.className = ok ? "ok" : "err";
}

function api(path, body) {
  var options = body ? { method: "POST", body: JSON.stringify(body) } : {};
  return fetch(path, options).then(function (response) {
    if (response.status === 401) { location.reload(); throw new Error("请重新登录"); }
    return response.json().then(function (data) {
      if (!response.ok) { throw new Error(data.error || ("HTTP " + response.status)); }
      return data;
    });
  });
}

function render(data) {
  listEl.innerHTML = "";
  var names = Object.keys(data.providers);
  if (!names.length) {
    listEl.innerHTML = "<tr><td colspan=\\"6\\">还没有档案，先在下方新增。</td></tr>";
    return;
  }
  names.forEach(function (name) {
    var provider = data.providers[name];
    var row = document.createElement("tr");
    var isActive = data.active === name;
    row.innerHTML =
      "<td>" + (isActive ? "<span class=\\"active-mark\\">●</span>" : "") + "</td>" +
      "<td>" + name + (isActive ? "（当前）" : "") + "</td>" +
      "<td>" + provider.model + "</td>" +
      "<td>" + provider.base_url + "</td>" +
      "<td>" + provider.api_key + "</td>";
    var cell = document.createElement("td");
    var useButton = document.createElement("button");
    useButton.textContent = isActive ? "重新写入" : "启用";
    useButton.className = "primary";
    useButton.onclick = function () {
      api("/admin/api/activate", { name: name }).then(function () {
        say("已启用 " + name + "，.env.local 已更新；下次 translate 即生效。", true);
        load();
      }).catch(function (error) { say(error.message, false); });
    };
    var editButton = document.createElement("button");
    editButton.textContent = "编辑";
    editButton.onclick = function () {
      document.getElementById("f-name").value = name;
      document.getElementById("f-url").value = provider.base_url;
      document.getElementById("f-model").value = provider.model;
      document.getElementById("f-key").value = "";
      say("已载入 " + name + "，改完点保存；密钥留空表示沿用。", true);
    };
    var deleteButton = document.createElement("button");
    deleteButton.textContent = "删除";
    deleteButton.className = "danger";
    deleteButton.onclick = function () {
      api("/admin/api/providers", { name: name, delete: true }).then(function () {
        say("已删除 " + name, true); load();
      }).catch(function (error) { say(error.message, false); });
    };
    cell.appendChild(useButton); cell.appendChild(document.createTextNode(" "));
    cell.appendChild(editButton); cell.appendChild(document.createTextNode(" "));
    cell.appendChild(deleteButton);
    row.appendChild(cell);
    listEl.appendChild(row);
  });
}

function load() {
  api("/admin/api/providers").then(function (data) {
    document.getElementById("password-box").hidden = !data.can_change_password;
    document.getElementById("logout").hidden = !data.can_change_password;
    render(data);
  }).catch(function (error) { say(error.message, false); });
}

document.getElementById("logout").onclick = function (event) {
  event.preventDefault();
  api("/admin/api/logout", {}).then(function () { location.reload(); });
};

document.getElementById("save-pwd").onclick = function () {
  var first = document.getElementById("f-pwd").value;
  var second = document.getElementById("f-pwd2").value;
  if (first !== second) { say("两次输入不一致", false); return; }
  api("/admin/api/password", { password: first }).then(function () {
    say("口令已修改；浏览器随后会重新要求登录，用新口令即可。", true);
    document.getElementById("f-pwd").value = "";
    document.getElementById("f-pwd2").value = "";
  }).catch(function (error) { say(error.message, false); });
};

document.getElementById("save").onclick = function () {
  api("/admin/api/providers", {
    name: document.getElementById("f-name").value,
    base_url: document.getElementById("f-url").value,
    model: document.getElementById("f-model").value,
    api_key: document.getElementById("f-key").value
  }).then(function () {
    say("已保存。", true);
    document.getElementById("f-key").value = "";
    load();
  }).catch(function (error) { say(error.message, false); });
};

load();
</script>
</body>
</html>
"""
