"""本地预览服务器：静态站点 + 模型供应商切换面板（仅绑定 127.0.0.1）。

仅用于本机开发预览，生产环境不部署本模块。
供应商档案存于 .env.providers.local，启用时改写 .env.local 的三个 TRANSLATION_* 值；
两个文件均被 .gitignore 排除，接口返回的 api_key 一律掩码。
"""

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROFILES_FILE = ".env.providers.local"
ENV_FILE = ".env.local"
_ENV_KEYS = ("TRANSLATION_API_BASE_URL", "TRANSLATION_API_KEY", "TRANSLATION_MODEL")


def load_profiles(root: Path) -> dict:
    path = root / PROFILES_FILE
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"active": "", "providers": {}}


def save_profiles(root: Path, data: dict) -> None:
    (root / PROFILES_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return "已设置"
    return f"{key[:6]}…{key[-4:]}"


def write_env_local(root: Path, provider: dict) -> None:
    """把供应商三项写入 .env.local，保留文件中其余行不动。"""
    values = {
        "TRANSLATION_API_BASE_URL": provider["base_url"],
        "TRANSLATION_API_KEY": provider["api_key"],
        "TRANSLATION_MODEL": provider["model"],
    }
    path = root / ENV_FILE
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


class PreviewHandler(SimpleHTTPRequestHandler):
    """静态站点 + /admin 面板与 JSON 接口。"""

    project_root: Path

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        pass  # 本地预览不刷请求日志

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        if self.path in ("/admin", "/admin/"):
            body = ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/admin/api/providers":
            data = load_profiles(self.project_root)
            masked = {
                "active": data["active"],
                "providers": {
                    name: {**p, "api_key": mask_key(p.get("api_key", ""))}
                    for name, p in data["providers"].items()
                },
            }
            self._json(200, masked)
        else:
            super().do_GET()

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

        data = load_profiles(self.project_root)
        if self.path == "/admin/api/providers":
            self._handle_save(body, data)
        elif self.path == "/admin/api/activate":
            self._handle_activate(body, data)
        else:
            self._json(404, {"error": "未知接口"})

    def _handle_save(self, body: dict, data: dict) -> None:
        name = str(body.get("name", "")).strip()
        if not name:
            self._json(400, {"error": "名称不能为空"})
            return
        if body.get("delete"):
            data["providers"].pop(name, None)
            if data["active"] == name:
                data["active"] = ""
            save_profiles(self.project_root, data)
            self._json(200, {"ok": True})
            return
        existing = data["providers"].get(name, {})
        api_key = str(body.get("api_key", "")).strip() or existing.get("api_key", "")
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
        save_profiles(self.project_root, data)
        self._json(200, {"ok": True})

    def _handle_activate(self, body: dict, data: dict) -> None:
        name = str(body.get("name", "")).strip()
        provider = data["providers"].get(name)
        if provider is None:
            self._json(404, {"error": f"档案不存在：{name}"})
            return
        write_env_local(self.project_root, provider)
        data["active"] = name
        save_profiles(self.project_root, data)
        self._json(200, {"ok": True, "active": name})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(root: Path, site_dir: Path, port: int) -> ThreadingHTTPServer:
    handler_class = partial(PreviewHandler, directory=str(site_dir))
    PreviewHandler.project_root = root
    return ThreadingHTTPServer(("127.0.0.1", port), handler_class)


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
<div id="status"></div>
<p class="note"><a href="/">← 返回站点</a></p>
</div>
<script>
"use strict";
var listEl = document.getElementById("list");
var statusEl = document.getElementById("status");

function say(message, ok) {
  statusEl.textContent = message;
  statusEl.className = ok ? "ok" : "err";
}

function api(path, body) {
  var options = body ? { method: "POST", body: JSON.stringify(body) } : {};
  return fetch(path, options).then(function (response) {
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
  api("/admin/api/providers").then(render).catch(function (error) { say(error.message, false); });
}

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
