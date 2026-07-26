"""预览服务器的供应商档案管理与 .env.local 改写（本地回环，离线）。"""

import http.client
import json
import threading

import pytest

from news_digest.preview_server import (
    apr1_hash,
    create_server,
    load_profiles,
    mask_key,
    save_profiles,
    write_env_local,
)

PROVIDER = {
    "base_url": "https://api.example.com/v1",
    "api_key": "sk-test-abcdefghijklmnop",
    "model": "demo-model",
}


def test_mask_key():
    assert mask_key("") == ""
    assert mask_key("short") == "已设置"
    assert mask_key("sk-test-abcdefghijklmnop") == "sk-tes…mnop"


def test_profiles_roundtrip(tmp_path):
    data = {"active": "a", "providers": {"a": PROVIDER}}
    save_profiles(tmp_path, data)
    assert load_profiles(tmp_path) == data
    assert load_profiles(tmp_path / "nowhere") == {"active": "", "providers": {}}


def test_write_env_local_replaces_only_translation_keys(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text(
        "# 注释保留\nNEWS_HTTP_PROXY=http://127.0.0.1:2231\n"
        'TRANSLATION_API_BASE_URL= "https://old/v1"\nTRANSLATION_MODEL=old-model\n',
        encoding="utf-8",
    )
    write_env_local(tmp_path, PROVIDER)
    content = env.read_text(encoding="utf-8")
    assert "# 注释保留" in content
    assert "NEWS_HTTP_PROXY=http://127.0.0.1:2231" in content
    assert "TRANSLATION_API_BASE_URL=https://api.example.com/v1" in content
    assert "TRANSLATION_MODEL=demo-model" in content
    assert "TRANSLATION_API_KEY=sk-test-abcdefghijklmnop" in content
    assert "old-model" not in content


@pytest.fixture
def admin_server(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    server = create_server(tmp_path, site, 0)  # 端口 0：由系统分配
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield tmp_path, server.server_address[1]
    server.shutdown()
    server.server_close()


def _request(
    port: int,
    method: str,
    path: str,
    body: dict | None = None,
    cookie: str = "",
) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body) if body is not None else None
    headers = {"Cookie": cookie} if cookie else {}
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, data


PANEL_PASSWORD = "test-password-1"


def _login(port: int, password: str = PANEL_PASSWORD) -> str:
    """登录并取回会话 Cookie（name=value）。"""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(
        "POST", "/admin/api/login",
        body=json.dumps({"username": "admin", "password": password}),
    )
    response = connection.getresponse()
    response.read()
    assert response.status == 200, "登录应成功"
    set_cookie = response.getheader("Set-Cookie", "")
    connection.close()
    return set_cookie.split(";")[0]


@pytest.fixture
def prod_server(tmp_path):
    """生产模式：.env / providers.json 文件名，禁止密钥经网页，禁静态回落。"""
    (tmp_path / ".env").write_text(
        "NEWS_SITE_URL=https://news.example.com\nTRANSLATION_MODEL=old-model\n",
        encoding="utf-8",
    )
    save_profiles(
        tmp_path, {"active": "", "providers": {"claude": dict(PROVIDER)}}, "providers.json"
    )
    (tmp_path / "htpasswd-admin").write_text(
        f"admin:{apr1_hash(PANEL_PASSWORD)}\n", encoding="utf-8"
    )
    server = create_server(
        tmp_path, tmp_path, 0,
        env_file=".env", profiles_file="providers.json", allow_key_input=False,
        serve_static=False, htpasswd_file=tmp_path / "htpasswd-admin",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield tmp_path, server.server_address[1]
    server.shutdown()
    server.server_close()


def test_apr1_matches_openssl_vector():
    # openssl passwd -apr1 -salt abcdefgh secret123
    assert apr1_hash("secret123", "abcdefgh") == "$apr1$abcdefgh$aQ26yFH6V5G5PJBY/utXg/"


def test_production_mode_never_serves_static_files(prod_server):
    root, port = prod_server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/.env")
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    connection.close()
    assert response.status == 404
    assert "TRANSLATION" not in body  # 明文密钥绝不可经静态回落泄出


def test_password_change_endpoint_and_session_rotation(prod_server):
    root, port = prod_server
    cookie = _login(port)
    status, data = _request(
        port, "POST", "/admin/api/password", {"password": "short"}, cookie=cookie
    )
    assert status == 400

    status, data = _request(
        port, "POST", "/admin/api/password", {"password": "new-password-1"}, cookie=cookie
    )
    assert status == 200
    content = (root / "htpasswd-admin").read_text(encoding="utf-8")
    username, hashed = content.strip().split(":", 1)
    assert username == "admin"
    assert hashed.startswith("$apr1$")
    salt = hashed.split("$")[2]
    assert apr1_hash("new-password-1", salt) == hashed  # 新口令可验证

    # 改密后会话密钥轮换：旧 Cookie 立即失效，新口令可重新登录
    status, _ = _request(port, "GET", "/admin/api/providers", cookie=cookie)
    assert status == 401
    new_cookie = _login(port, "new-password-1")
    status, _ = _request(port, "GET", "/admin/api/providers", cookie=new_cookie)
    assert status == 200


def test_login_required_and_flow(prod_server):
    _, port = prod_server
    # 未登录：接口 401，面板页返回登录页
    status, _ = _request(port, "GET", "/admin/api/providers")
    assert status == 401
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/admin/")
    html = connection.getresponse().read().decode("utf-8")
    connection.close()
    assert "管理登录" in html
    # 错误口令 401；正确口令得到会话
    bad = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    bad.request(
        "POST", "/admin/api/login",
        body=json.dumps({"username": "admin", "password": "wrong"}),
    )
    assert bad.getresponse().status == 401
    bad.close()
    cookie = _login(port)
    status, _ = _request(port, "GET", "/admin/api/providers", cookie=cookie)
    assert status == 200


def test_production_mode_rejects_key_input(prod_server):
    _, port = prod_server
    cookie = _login(port)
    status, data = _request(
        port, "POST", "/admin/api/providers",
        {"name": "claude", **{**PROVIDER, "api_key": "sk-new-key-attempt"}},
        cookie=cookie,
    )
    assert status == 400
    assert "不接受密钥" in data["error"]


def test_production_mode_edit_and_activate(prod_server):
    root, port = prod_server
    cookie = _login(port)
    # 改接口地址与模型（key 留空沿用）——允许
    status, _ = _request(
        port, "POST", "/admin/api/providers",
        {"name": "claude", "base_url": "https://new.example.com/v1",
         "model": "new-model", "api_key": ""},
        cookie=cookie,
    )
    assert status == 200
    stored = load_profiles(root, "providers.json")["providers"]["claude"]
    assert stored["api_key"] == PROVIDER["api_key"]  # 原密钥保留
    assert stored["base_url"] == "https://new.example.com/v1"

    status, _ = _request(port, "POST", "/admin/api/activate", {"name": "claude"}, cookie=cookie)
    assert status == 200
    env = (root / ".env").read_text(encoding="utf-8")
    assert "TRANSLATION_API_BASE_URL=https://new.example.com/v1" in env
    assert "TRANSLATION_MODEL=new-model" in env
    assert "NEWS_SITE_URL=https://news.example.com" in env  # 无关行保留


def test_production_admin_page_hides_key_field(prod_server):
    _, port = prod_server
    cookie = _login(port)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/admin/", headers={"Cookie": cookie})
    html = connection.getresponse().read().decode("utf-8")
    connection.close()
    assert "var allowKeyInput = false;" in html


def test_admin_flow_save_activate_masked(admin_server):
    root, port = admin_server

    status, data = _request(port, "GET", "/admin/api/providers")
    assert status == 200 and data["providers"] == {}

    status, _ = _request(port, "POST", "/admin/api/providers", {"name": "demo", **PROVIDER})
    assert status == 200

    status, data = _request(port, "GET", "/admin/api/providers")
    assert data["providers"]["demo"]["api_key"] == "sk-tes…mnop"  # 永远掩码

    status, data = _request(port, "POST", "/admin/api/activate", {"name": "demo"})
    assert status == 200 and data["active"] == "demo"
    env = (root / ".env.local").read_text(encoding="utf-8")
    assert "TRANSLATION_API_KEY=sk-test-abcdefghijklmnop" in env
    assert load_profiles(root)["active"] == "demo"

    # 编辑时 key 留空 -> 沿用旧 key
    status, _ = _request(
        port,
        "POST",
        "/admin/api/providers",
        {"name": "demo", "base_url": PROVIDER["base_url"], "model": "new-model", "api_key": ""},
    )
    assert status == 200
    assert load_profiles(root)["providers"]["demo"]["api_key"] == PROVIDER["api_key"]

    status, data = _request(port, "POST", "/admin/api/activate", {"name": "ghost"})
    assert status == 404

    status, _ = _request(port, "POST", "/admin/api/providers", {"name": "demo", "delete": True})
    assert status == 200
    data = load_profiles(root)
    assert data["providers"] == {} and data["active"] == ""
