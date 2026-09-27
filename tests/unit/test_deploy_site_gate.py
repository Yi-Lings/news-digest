"""Isolated Nginx/TLS checks for the open-source deploy gate."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "deploy" / "site-gate.sh"
TOOLS = ("bash", "nginx", "openssl", "curl")
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or any(shutil.which(tool) is None for tool in TOOLS),
    reason="isolated Nginx/TLS integration requires Linux, nginx, openssl and curl",
)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_gate(command: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{GATE}"; {command}'],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _nginx_wrapper(tmp_path: Path, conf_dir: Path) -> tuple[Path, Path]:
    conf = tmp_path / "nginx.conf"
    conf.write_text(
        f"pid {tmp_path / 'nginx.pid'};\nevents {{}}\n"
        f"http {{ include {conf_dir}/*.conf; }}\n",
        encoding="utf-8",
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "nginx"
    wrapper.write_text(
        f'#!/bin/sh\nexec {shutil.which("nginx")} -c "{conf}" -p "{tmp_path}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return conf, bindir


@pytest.mark.parametrize("domain", ["alpha.example", "news.xn--fiqs8s"])
def test_bootstrap_accepts_safe_ascii_domain_before_any_side_effect(tmp_path: Path, domain: str):
    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "bootstrap.sh")],
        env={
            **os.environ,
            "ND_OWNER": "owner",
            "ND_APP_DIR": str(tmp_path / "app"),
            "ND_DOMAIN": domain,
            "ND_CERTBOT_EMAIL": "test@example.org",
            "ND_VERSION": "",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "缺少 ND_VERSION" in result.stderr
    assert not (tmp_path / "app").exists()


@pytest.mark.parametrize("domain", ["alpha.example", "news.xn--fiqs8s"])
def test_existing_url_and_foreign_news_conf_are_domain_scoped(tmp_path: Path, domain: str):
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    env_file = app / "config" / ".env"
    env_file.write_text(f"NEWS_SITE_URL=https://{domain}\n", encoding="utf-8")
    conf_dir = tmp_path / "conf.d"
    conf_dir.mkdir()
    old = conf_dir / "news.conf"
    old.write_text(
        f"server {{ listen 127.0.0.1:{_port()}; "
        "server_name other.example; return 200; }\n",
        encoding="utf-8",
    )
    _, bindir = _nginx_wrapper(tmp_path, conf_dir)
    env = {
        "ND_APP_DIR": str(app),
        "ND_DOMAIN": domain,
        "ND_NGINX_CONF_DIR": str(conf_dir),
        "PATH": f"{bindir}:{os.environ['PATH']}",
    }
    assert _run_gate("nd_check_saved_domain && nd_check_nginx_ownership", **env).returncode == 0
    assert _run_gate("nd_is_legacy_conf \"$(nd_legacy_conf)\"", **env).returncode != 0

    old.write_text(
        f"server {{ listen 127.0.0.1:{_port()}; "
        f"server_name {domain}; return 200; }}\n",
        encoding="utf-8",
    )
    assert _run_gate("nd_check_nginx_ownership", **env).returncode != 0
    old.write_text(
        f"# {domain} —— 宿主机 Nginx 反向代理模板\n"
        "limit_req_zone $binary_remote_addr zone=news_ratelimit:10m rate=10r/s;\n"
        f"server {{ listen 127.0.0.1:{_port()}; server_name other.example; "
        "location /admin/ { return 200; } }\n",
        encoding="utf-8",
    )
    assert _run_gate("nd_check_nginx_ownership", **env).returncode != 0

    env_file.write_text("NEWS_SITE_URL=https://wrong.example\n", encoding="utf-8")
    result = _run_gate("nd_check_saved_domain", **env)
    assert result.returncode != 0
    assert "不一致" in result.stderr


def test_duplicate_or_foreign_server_name_blocks_deploy(tmp_path: Path):
    domain = "alpha.example"
    conf_dir = tmp_path / "conf.d"
    conf_dir.mkdir()
    port = _port()
    managed = conf_dir / "news-digest.conf"
    managed.write_text(
        f"# Managed by news-digest; ND_DOMAIN={domain}\n"
        f"server {{ listen 127.0.0.1:{port}; server_name {domain}; return 200; }}\n",
        encoding="utf-8",
    )
    foreign = conf_dir / "foreign.conf"
    foreign.write_text(
        f"server {{ listen 127.0.0.1:{_port()}; "
        f"server_name {domain}; return 200; }}\n",
        encoding="utf-8",
    )
    _, bindir = _nginx_wrapper(tmp_path, conf_dir)
    env = {
        "ND_DOMAIN": domain,
        "ND_NGINX_CONF_DIR": str(conf_dir),
        "PATH": f"{bindir}:{os.environ['PATH']}",
    }
    managed.write_text(managed.read_text().replace(domain + ";", "wrong.example;"))
    assert _run_gate("nd_check_nginx_ownership", **env).returncode != 0
    managed.write_text(managed.read_text().replace("wrong.example;", domain + ";"))
    assert _run_gate("nd_check_nginx_ownership", **env).returncode != 0
    foreign.write_text(
        f"server {{ listen 127.0.0.1:{port}; "
        f"server_name {domain}; return 200; }}\n",
        encoding="utf-8",
    )
    assert _run_gate("nd_nginx_test", **env).returncode != 0


def test_missing_cert_allows_only_first_install_http_fallback(tmp_path: Path):
    conf_dir = tmp_path / "conf.d"
    conf_dir.mkdir()
    legacy = conf_dir / "news.conf"
    legacy.write_text(
        "server { listen 443 ssl; server_name other.example; }\n", encoding="utf-8"
    )
    env = {
        "ND_DOMAIN": "alpha.example",
        "ND_NGINX_CONF_DIR": str(conf_dir),
        "ND_CERT_DIR": str(tmp_path / "missing-cert"),
    }
    assert _run_gate("nd_has_owned_https", **env).returncode != 0
    assert _run_gate("nd_check_certificate", **env).returncode == 2

    managed = conf_dir / "news-digest.conf"
    managed.write_text(
        "# Managed by news-digest; ND_DOMAIN=alpha.example\n"
        "server {\n listen 443 ssl;\n server_name alpha.example;\n}\n",
        encoding="utf-8",
    )
    assert _run_gate("nd_has_owned_https", **env).returncode == 0
    assert _run_gate("nd_check_certificate", **env).returncode == 2


def test_legacy_certbot_hook_backup_is_not_executable_by_certbot(tmp_path: Path):
    hook_dir = tmp_path / "renewal-hooks" / "deploy"
    hook_dir.mkdir(parents=True)
    old_hook = hook_dir / "10-reload-nginx.sh"
    old_hook.write_text(
        "#!/bin/sh\n# certbot 在证书成功续期后自动调用；先 nginx -t 校验\n"
        "nginx -t && systemctl reload nginx\n",
        encoding="utf-8",
    )
    old_hook.chmod(0o755)
    archive = tmp_path / "app" / "backups" / "certbot-hooks" / "old.sh"
    env = {"ND_CERTBOT_HOOK_DIR": str(hook_dir), "ND_APP_DIR": str(tmp_path / "app")}
    assert _run_gate(
        'nd_archive_legacy_hook "$(nd_legacy_hook)" "${ND_APP_DIR}/backups/certbot-hooks/old.sh"',
        **env,
    ).returncode == 0
    assert not old_hook.exists()
    assert archive.exists()
    assert archive.parent != hook_dir

    new_hook = hook_dir / "news-digest-reload-nginx.sh"
    new_hook.write_text(
        "#!/bin/sh\n# Managed by news-digest; Certbot renewal hook\n"
        "nginx -t && systemctl reload nginx\n",
        encoding="utf-8",
    )
    new_hook.chmod(0o755)
    assert _run_gate("nd_check_hook_ownership", **env).returncode == 0
    assert [path.name for path in hook_dir.iterdir() if os.access(path, os.X_OK)] == [
        "news-digest-reload-nginx.sh"
    ]
    old_hook.write_bytes(archive.read_bytes())
    old_hook.chmod(0o755)
    assert _run_gate("nd_check_hook_ownership", **env).returncode != 0


def _certificate(tmp_path: Path, domain: str, *, san: bool = True) -> Path:
    cert_dir = tmp_path / "cert"
    cert_dir.mkdir(parents=True)
    args = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(cert_dir / "privkey.pem"),
            "-out", str(cert_dir / "fullchain.pem"),
            "-days", "2", "-subj", f"/CN={domain}",
    ]
    if san:
        args += ["-addext", f"subjectAltName=DNS:{domain}"]
    subprocess.run(
        args,
        capture_output=True,
        check=True,
    )
    return cert_dir


def test_certificate_san_and_live_tls_gate(tmp_path: Path):
    domain = "alpha.example"
    cert_dir = _certificate(tmp_path, domain)
    assert _run_gate(
        "nd_check_certificate", ND_DOMAIN=domain, ND_CERT_DIR=str(cert_dir)
    ).returncode == 0
    assert _run_gate(
        "nd_check_certificate", ND_DOMAIN="beta.example", ND_CERT_DIR=str(cert_dir)
    ).returncode != 0
    cn_only = _certificate(tmp_path / "cn-only", domain, san=False)
    assert _run_gate(
        "nd_check_certificate", ND_DOMAIN=domain, ND_CERT_DIR=str(cn_only)
    ).returncode != 0
    bindir = tmp_path / "expired-openssl"
    bindir.mkdir()
    openssl = bindir / "openssl"
    openssl.write_text(
        '#!/bin/sh\ncase " $* " in *" -checkend 0 "*) exit 1;; esac\n'
        f'exec "{shutil.which("openssl")}" "$@"\n',
        encoding="utf-8",
    )
    openssl.chmod(0o755)
    expired = _run_gate(
        "nd_check_certificate", ND_DOMAIN=domain, ND_CERT_DIR=str(cert_dir),
        PATH=f"{bindir}:{os.environ['PATH']}",
    )
    assert expired.returncode != 0
    assert "已过期" in expired.stderr

    port = _port()
    conf_dir = tmp_path / "conf.d"
    conf_dir.mkdir()
    (conf_dir / "news-digest.conf").write_text(
        f"# Managed by news-digest; ND_DOMAIN={domain}\n"
        f"server {{\n listen 127.0.0.1:{port} ssl;\n server_name {domain}; "
        f"ssl_certificate {cert_dir / 'fullchain.pem'}; "
        f"ssl_certificate_key {cert_dir / 'privkey.pem'}; "
        'location / { return 200 "ok"; } }\n',
        encoding="utf-8",
    )
    conf, _ = _nginx_wrapper(tmp_path, conf_dir)
    server = subprocess.Popen(
        ["nginx", "-c", str(conf), "-p", str(tmp_path), "-g", "daemon off;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail(f"isolated nginx did not start: {server.stderr.read()!r}")
        env = {
            "ND_DOMAIN": domain,
            "ND_GATE_HTTPS_PORT": str(port),
            "ND_NGINX_CONF_DIR": str(conf_dir),
            "CURL_CA_BUNDLE": str(cert_dir / "fullchain.pem"),
        }
        assert _run_gate("nd_verify_site https", **env).returncode == 0
        assert _run_gate("nd_verify_site_auto", **env).returncode == 0
        assert _run_gate(
            "nd_verify_site https", **{**env, "ND_DOMAIN": "beta.example"}
        ).returncode != 0
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_http_only_gate_requires_public_admin_to_be_closed(tmp_path: Path):
    domain = "beta.example"
    port = _port()
    hits: list[str] = []

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(f"GET {self.path}")
            self.send_response(200)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            hits.append(f"POST {self.path}")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    conf_dir = tmp_path / "conf.d"
    conf_dir.mkdir()
    site = conf_dir / "news-digest.conf"
    bootstrap = (ROOT / "deploy" / "bootstrap.sh").read_text(encoding="utf-8")
    template = bootstrap.split("<<'HTTPEOF'\n", 1)[1].split("\nHTTPEOF", 1)[0]
    site.write_text(
        template.replace("news.example.com", domain)
        .replace("listen 80;", f"listen 127.0.0.1:{port};")
        .replace("listen [::]:80;", "")
        .replace("127.0.0.1:8620", f"127.0.0.1:{backend.server_port}"),
        encoding="utf-8",
    )
    conf, _ = _nginx_wrapper(tmp_path, conf_dir)
    server = subprocess.Popen(
        ["nginx", "-c", str(conf), "-p", str(tmp_path), "-g", "daemon off;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail(f"isolated nginx did not start: {server.stderr.read()!r}")
        assert _run_gate(
            "nd_verify_site_auto", ND_DOMAIN=domain, ND_GATE_HTTP_PORT=str(port),
            ND_NGINX_CONF_DIR=str(conf_dir),
        ).returncode == 0
        assert hits == ["GET /", "GET /healthz"]
    finally:
        server.terminate()
        server.wait(timeout=5)
        backend.shutdown()
        backend.server_close()


@pytest.mark.parametrize("admin_closed", [False, True])
def test_restored_legacy_http_uses_compatibility_gate(
    tmp_path: Path, admin_closed: bool,
):
    domain = "alpha.example"
    port = _port()

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    conf_dir = tmp_path / "conf.d"
    conf_dir.mkdir()
    admin_location = "location = /admin/ { return 404; }" if admin_closed else ""
    (conf_dir / "news.conf").write_text(
        f"server {{ listen 127.0.0.1:{port}; server_name {domain}; "
        f"{admin_location} location / {{ proxy_pass http://127.0.0.1:{backend.server_port}; }} }}",
        encoding="utf-8",
    )
    conf, _ = _nginx_wrapper(tmp_path, conf_dir)
    server = subprocess.Popen(
        ["nginx", "-c", str(conf), "-p", str(tmp_path), "-g", "daemon off;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail(f"isolated nginx did not start: {server.stderr.read()!r}")
        env = {"ND_DOMAIN": domain, "ND_GATE_HTTP_PORT": str(port)}
        restored = _run_gate("nd_verify_restored_http", **env)
        assert (restored.returncode == 0) == admin_closed, restored.stderr
        assert _run_gate("nd_verify_site http", **env).returncode != 0
    finally:
        server.terminate()
        server.wait(timeout=5)
        backend.shutdown()
        backend.server_close()


@pytest.mark.parametrize(
    "failure_mode", ["none", "site_gate", "timer_state", "nginx_copy"]
)
def test_failed_reload_restores_nginx_and_timer_state(
    tmp_path: Path, failure_mode: str,
):
    """Exercise bootstrap's EXIT rollback without touching system paths."""
    conf_dir = tmp_path / "conf.d"
    conf_dir.mkdir()
    managed = conf_dir / "news-digest.conf"
    legacy = conf_dir / "news.conf"
    managed.write_text("old managed\n", encoding="utf-8")
    legacy.write_text("old legacy\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "nginx").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bindir / "nginx").chmod(0o755)
    systemctl = bindir / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ND_TEST_SYSTEMCTL_LOG"\n'
        'if [ "$1" = is-enabled ]; then\n'
        '  if [ "$ND_TEST_FAILURE_MODE" = timer_state ]; then printf "disabled\\n"; exit 0; fi\n'
        '  case "$2" in news-digest.timer) printf "enabled\\n";; *) printf "disabled\\n";; esac\n'
        '  exit 0\nfi\n'
        'if [ "$1" = show ]; then\n'
        '  case "$2" in news-digest.timer) printf "active\\n";; *) printf "inactive\\n";; esac\n'
        '  exit 0\nfi\n'
        'if [ "$1 $2" = "reload nginx" ] && [ ! -e "$ND_TEST_RELOAD_ONCE" ]; then\n'
        '  touch "$ND_TEST_RELOAD_ONCE"; exit 1\nfi\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    shutil.copyfile(GATE, script_dir / "site-gate.sh")
    bootstrap = (ROOT / "deploy" / "bootstrap.sh").read_text(encoding="utf-8")
    prefix = bootstrap.split('section "1/10 前置校验', 1)[0]
    prefix = prefix.replace(
        'NGINX_CONF="/etc/nginx/conf.d/news-digest.conf"',
        f'NGINX_CONF="{managed}"',
    ).replace(
        'LEGACY_NGINX_CONF="/etc/nginx/conf.d/news.conf"',
        f'LEGACY_NGINX_CONF="{legacy}"',
    )
    rollback = script_dir / "rollback.sh"
    rollback.write_text(
        prefix
        + '\nnd_verify_restored_http() { [ "$ND_TEST_FAILURE_MODE" != site_gate ]; }\n'
        + '\ncp -a "$NGINX_CONF" "${TMP_DIR}/old-managed.conf"\n'
        + 'cp -a "$LEGACY_NGINX_CONF" "${TMP_DIR}/old-legacy.conf"\n'
        + 'OLD_LEGACY_OWNED=1; NGINX_PENDING=1; TIMER_PENDING=1\n'
        + 'printf "news-digest.timer enabled active\\n'
        + 'news-digest-wakeup.path disabled inactive\\n" '
        + '> "${TMP_DIR}/timer-state"\n'
        + 'printf "new managed\\n" > "$NGINX_CONF"; rm "$LEGACY_NGINX_CONF"\n'
        + 'if [ "$ND_TEST_FAILURE_MODE" = nginx_copy ]; then\n'
        + '  cp() { case "$3" in *.restore-*.tmp) return 1;; esac; command cp "$@"; }\n'
        + 'fi\n'
        + 'systemctl reload nginx\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(rollback)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "ND_OWNER": "owner",
            "ND_APP_DIR": str(tmp_path / "app"),
            "ND_DOMAIN": "alpha.example",
            "ND_CERTBOT_EMAIL": "test@example.org",
            "ND_VERSION": "v1.0.0",
            "ND_WORKER_DIGEST": "sha256:" + "a" * 64,
            "ND_WEB_DIGEST": "sha256:" + "b" * 64,
            "ND_TEST_SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
            "ND_TEST_RELOAD_ONCE": str(tmp_path / "reload-once"),
            "ND_TEST_FAILURE_MODE": failure_mode,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    if failure_mode == "nginx_copy":
        assert managed.read_text(encoding="utf-8") == "new managed\n"
        assert not legacy.exists()
    else:
        assert managed.read_text(encoding="utf-8") == "old managed\n"
        assert legacy.read_text(encoding="utf-8") == "old legacy\n"
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
    assert "reload nginx" in calls
    if failure_mode in {"site_gate", "nginx_copy"}:
        assert "start news-digest.timer" not in calls
        assert "timer/path 保持冻结" in result.stderr
    else:
        assert "enable news-digest.timer" in calls
        assert "start news-digest.timer" in calls
        assert "disable news-digest-wakeup.path" in calls
        if failure_mode == "timer_state":
            assert calls.count("stop news-digest.timer") >= 2
            assert "timer/path 未能恢复或复核" in result.stderr
        else:
            assert "已复核部署前 timer/path" in result.stderr


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_failed_deploy_restores_old_app_before_timer(tmp_path: Path, rollback_fails: bool):
    app = tmp_path / "app"
    (app / "backups").mkdir(parents=True)
    old_compose = app / "compose.yaml.bak"
    old_compose.write_text("old images\n", encoding="utf-8")
    (app / "compose.yaml").write_text("new images\n", encoding="utf-8")
    before = app / "backups" / "before.db"
    before.write_bytes(b"backup")
    managed = tmp_path / "news-digest.conf"
    managed.write_text("old nginx\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    order_log = tmp_path / "order.log"
    for name, body in {
        "docker": (
            '#!/bin/sh\nprintf "docker %s\\n" "$*" >> "$ND_TEST_ORDER_LOG"\n'
            'if [ "$1" = run ] && [ "$ND_TEST_ROLLBACK_FAIL" = 1 ]; then exit 1; fi\n'
            'case "$*" in *" ps -q "*) printf "old-container\\n"; exit 0;; esac\n'
            'if [ "$1" = inspect ]; then printf "healthy\\n"; exit 0; fi\n'
            'exit 0\n'
        ),
        "curl": '#!/bin/sh\nexit 99\n',
        "nginx": '#!/bin/sh\nexit 0\n',
        "systemctl": (
            '#!/bin/sh\nprintf "systemctl %s\\n" "$*" >> "$ND_TEST_ORDER_LOG"\n'
            'if [ "$1" = is-enabled ]; then printf "enabled\\n"; exit 0; fi\n'
            'if [ "$1" = show ]; then printf "active\\n"; exit 0; fi\n'
            'if [ "$1 $2" = "reload nginx" ] && [ ! -e "$ND_TEST_RELOAD_ONCE" ]; then\n'
            '  touch "$ND_TEST_RELOAD_ONCE"; exit 1\nfi\nexit 0\n'
        ),
    }.items():
        path = bindir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    shutil.copyfile(GATE, script_dir / "site-gate.sh")
    (script_dir / "rollback-schema.py").write_text("# test stub\n", encoding="utf-8")
    bootstrap = (ROOT / "deploy" / "bootstrap.sh").read_text(encoding="utf-8")
    prefix = bootstrap.split('section "1/10 前置校验', 1)[0]
    prefix = prefix.replace(
        'NGINX_CONF="/etc/nginx/conf.d/news-digest.conf"',
        f'NGINX_CONF="{managed}"',
    )
    script = script_dir / "failed-deploy.sh"
    script.write_text(
        prefix
        + '\nnd_verify_restored_http() { return 0; }\n'
        + '\nAPP_CUTOVER_STARTED=1; OLD_APP_PRESENT=1; SCHEMA_MAY_HAVE_CHANGED=1\n'
        + f'OLD_COMPOSE_BACKUP="{old_compose}"; DB_MIGRATION_BACKUP="{before}"\n'
        + 'OLD_WORKER_IMAGE=old-worker-image; OLD_SCHEMA_VERSION=13\n'
        + 'NGINX_PENDING=1; TIMER_PENDING=1\n'
        + 'cp -a "$NGINX_CONF" "${TMP_DIR}/old-managed.conf"\n'
        + 'printf "news-digest.timer enabled active\\n" > "${TMP_DIR}/timer-state"\n'
        + 'printf "new nginx\\n" > "$NGINX_CONF"\n'
        + 'systemctl reload nginx\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "ND_OWNER": "owner", "ND_APP_DIR": str(app),
            "ND_DOMAIN": "alpha.example", "ND_CERTBOT_EMAIL": "test@example.org",
            "ND_VERSION": "v1.0.0", "ND_WORKER_DIGEST": "sha256:" + "a" * 64,
            "ND_WEB_DIGEST": "sha256:" + "b" * 64,
            "ND_TEST_ORDER_LOG": str(order_log),
            "ND_TEST_ROLLBACK_FAIL": "1" if rollback_fails else "0",
            "ND_TEST_RELOAD_ONCE": str(tmp_path / "reload-once"),
            "ND_WEB_PORT": "19001", "ND_SITE_PORT": "19002",
            "ND_ADMIN_PORT": "19003",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert managed.read_text(encoding="utf-8") == "old nginx\n"
    order = order_log.read_text(encoding="utf-8")
    assert order.index("docker compose") < order.index("docker run")
    if rollback_fails:
        assert (app / "compose.yaml").read_text(encoding="utf-8") == "new images\n"
        assert "systemctl start news-digest.timer" not in order
        assert "保持冻结" in result.stderr
    else:
        assert (app / "compose.yaml").read_text(encoding="utf-8") == "old images\n"
        assert order.index("docker run") < order.index("up -d web site admin")
        assert order.index("systemctl start news-digest.timer") > order.rindex(
            "systemctl reload nginx"
        )


def test_bootstrap_freezes_active_timers_and_restores_original_state(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    systemctl = bindir / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\n'
        'case "$1 $3" in\n'
        '  "show --property=LoadState")\n'
        '    case "$2" in *.service) printf "not-found\\n";; *) printf "loaded\\n";; esac\n'
        '    exit 0;;\n'
        '  "show --property=ActiveState")\n'
        '    case "$2" in news-digest.timer|news-digest-wakeup.path) '
        'printf "active\\n";; *) printf "inactive\\n";; esac\n'
        '    exit 0;;\n'
        'esac\n'
        'if [ "$1" = is-enabled ]; then\n'
        '  case "$2" in news-digest.timer|news-digest-wakeup.path) '
        'printf "enabled\\n";; *) printf "disabled\\n";; esac\n'
        '  exit 0\nfi\n'
        'printf "%s\\n" "$*" >> "$ND_TEST_SYSTEMCTL_LOG"\nexit 0\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    shutil.copyfile(GATE, script_dir / "site-gate.sh")
    bootstrap = (ROOT / "deploy" / "bootstrap.sh").read_text(encoding="utf-8")
    prefix = bootstrap.split('section "1/10 前置校验', 1)[0]
    script = script_dir / "freeze.sh"
    script.write_text(prefix + "\nfreeze_deployment_timers\nfalse\n", encoding="utf-8")
    log = tmp_path / "systemctl.log"
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
            "ND_OWNER": "owner", "ND_APP_DIR": str(tmp_path / "app"),
            "ND_DOMAIN": "alpha.example", "ND_CERTBOT_EMAIL": "test@example.org",
            "ND_VERSION": "v1.0.0", "ND_WORKER_DIGEST": "sha256:" + "a" * 64,
            "ND_WEB_DIGEST": "sha256:" + "b" * 64,
            "ND_TEST_SYSTEMCTL_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    calls = log.read_text(encoding="utf-8")
    assert calls.index("stop news-digest.timer") < calls.index("start news-digest.timer")
    assert calls.index("stop news-digest-wakeup.path") < calls.index(
        "start news-digest-wakeup.path"
    )
    assert "start news-digest-wakeup.timer" not in calls
    assert "start news-digest-backup.timer" not in calls


@pytest.mark.parametrize("old_container", [False, True])
def test_failed_first_install_compose_without_db_can_retry(
    tmp_path: Path, old_container: bool,
):
    app = tmp_path / "app"
    app.mkdir()
    (app / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = compose ]; then\n'
        '  if [ "$ND_TEST_OLD_CONTAINER" = 1 ]; then printf "old-container\\n"; fi\n'
        '  exit 0\nfi\n'
        'if [ "$1 $2" = "volume inspect" ]; then exit 1; fi\n'
        'exit 2\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    bootstrap = (ROOT / "deploy" / "bootstrap.sh").read_text(encoding="utf-8")
    function = bootstrap.split("detect_previous_worker() {", 1)[1].split(
        "\n}\ndetect_previous_worker", 1
    )[0]
    script = tmp_path / "detect.sh"
    script.write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        + f'APP_DIR="{app}"; WORKER_IMAGE=new-worker; OLD_APP_PRESENT=0\n'
        + 'die() { echo "$1" >&2; exit 1; }\n'
        + "detect_previous_worker() {" + function + "\n}\n"
        + "detect_previous_worker\ntest \"$OLD_APP_PRESENT\" -eq 0\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "ND_TEST_OLD_CONTAINER": "1" if old_container else "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode != 0) == old_container
    if old_container:
        assert "数据库缺失" in result.stderr
    else:
        assert "按首装重试" in result.stdout


@pytest.mark.parametrize("web_available", [False, True])
def test_existing_site_requires_local_old_web_image_before_cutover(
    tmp_path: Path, web_available: bool,
):
    app = tmp_path / "app"
    app.mkdir()
    (app / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = compose ]; then\n'
        '  case "$*" in *" ps -a -q "*) printf "old-container\\n";;\n'
        '    *) printf \'{"services":{"worker":{"image":"ghcr.io/owner/'
        'news-digest-worker@sha256:%s"},"web":{"image":"ghcr.io/owner/'
        'news-digest-web@sha256:%s"}}}\\n\' '
        '"$(printf a%.0s $(seq 1 64))" "$(printf b%.0s $(seq 1 64))";; esac\n'
        '  exit 0\nfi\n'
        'if [ "$1 $2" = "volume inspect" ]; then exit 0; fi\n'
        'if [ "$1" = run ]; then\n'
        '  case "$*" in *"Path("*) printf "yes\\n";; *) printf "13\\n";; esac\n'
        '  exit 0\nfi\n'
        'if [ "$1 $2" = "image inspect" ]; then\n'
        '  case "$3" in *news-digest-web*) [ "$ND_TEST_WEB_AVAILABLE" = 1 ];; *) exit 0;; esac\n'
        '  exit $?\nfi\nexit 2\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    bootstrap = (ROOT / "deploy" / "bootstrap.sh").read_text(encoding="utf-8")
    function = bootstrap.split("detect_previous_worker() {", 1)[1].split(
        "\n}\ndetect_previous_worker", 1
    )[0]
    script = tmp_path / "detect.sh"
    script.write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        + f'APP_DIR="{app}"; WORKER_IMAGE=new-worker; OLD_APP_PRESENT=0\n'
        + 'die() { echo "$1" >&2; exit 1; }\n'
        + "detect_previous_worker() {" + function + "\n}\n"
        + "detect_previous_worker\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
            "ND_TEST_WEB_AVAILABLE": "1" if web_available else "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == web_available
    if not web_available:
        assert "旧 web 镜像本地不可用" in result.stderr


@pytest.mark.parametrize("mode,initial", [("http", "https"), ("https", "http")])
def test_site_url_tracks_http_only_or_https_mode(tmp_path: Path, mode: str, initial: str):
    config = tmp_path / "config"
    site_config = tmp_path / "site-config"
    config.mkdir()
    site_config.mkdir()
    env_file = config / ".env"
    env_file.write_text(
        f"NEWS_SITE_URL={initial}://alpha.example\nNEWS_TIMEZONE=Asia/Shanghai\n",
        encoding="utf-8",
    )
    (site_config / ".env").write_text("old projection\n", encoding="utf-8")
    bootstrap = (ROOT / "deploy" / "bootstrap.sh").read_text(encoding="utf-8")
    function = bootstrap.split("sync_site_url_scheme() {", 1)[1].split(
        "\n}\nsync_site_url_scheme", 1
    )[0]
    script = tmp_path / "scheme.sh"
    script.write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        + f'CONFIG_DIR="{config}"; SITE_CONFIG_DIR="{site_config}"\n'
        + f'ENV_FILE="{env_file}"; TMP_DIR="{tmp_path}"\n'
        + f'DOMAIN=alpha.example; SITE_MODE={mode}; WORKER_IMAGE=test-image\n'
        + 'SITE_URL_CHANGED=0; COMPOSE=(true)\n'
        + 'docker() { return 0; }; chown() { return 0; }; chmod() { return 0; }\n'
        + "sync_site_url_scheme() {" + function + "\n}\n"
        + "sync_site_url_scheme\ntest \"$SITE_URL_CHANGED\" -eq 1\n",
        encoding="utf-8",
    )
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"NEWS_SITE_URL={mode}://alpha.example" in env_file.read_text(
        encoding="utf-8"
    )
    assert f"NEWS_SITE_URL={initial}://alpha.example" in (
        tmp_path / "old-site-url.env"
    ).read_text(encoding="utf-8")
