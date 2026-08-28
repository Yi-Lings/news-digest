import re
import sqlite3
from pathlib import Path

from news_digest import __version__

ROOT = Path(__file__).parents[2]


def test_phase_8_release_version_is_v1_2_11():
    assert __version__ == "1.2.11"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _backup_python(bootstrap: str) -> str:
    start = bootstrap.index("\nimport sqlite3\nfrom pathlib import Path\n") + 1
    end = bootstrap.index("\n' || status=$?", start)
    return bootstrap[start:end]


def test_release_bundle_carries_exact_version_and_image_digests():
    workflow = _read(".github/workflows/release.yml")

    assert "ND_VERSION=${GITHUB_REF_NAME}" in workflow
    assert "ND_WORKER_DIGEST=${{ needs.build.outputs.worker-digest }}" in workflow
    assert "ND_WEB_DIGEST=${{ needs.build.outputs.web-digest }}" in workflow
    assert "cp digests.env deploy/digests.env" in workflow
    assert "news-digest-deploy.tgz.sha256 digests.env --clobber" in workflow
    assert "sha256sum news-digest-deploy.tgz" in workflow
    assert "news-digest-deploy.tgz.sha256" in workflow
    assert not re.search(r"uses:\s+[^\s#]+@v\d+", workflow)


def test_windows_release_path_refuses_dirty_or_detached_tag_and_passes_digests():
    deploy_all = _read("deploy/deploy-all.ps1")
    server_push = _read("deploy/server-push.ps1")

    assert "git status --porcelain --untracked-files=normal" in deploy_all
    assert "release worktree is dirty" in deploy_all
    assert "tag $Version is at" in deploy_all
    assert "release HEAD is not the current main commit" in deploy_all
    assert 'gh release download $Version --pattern "digests.env"' in deploy_all
    assert "-WorkerDigest $WorkerDigest -WebDigest $WebDigest" in deploy_all
    assert "ghcr.io/$Owner/news-digest-worker@$WorkerDigest" in deploy_all
    assert "git push origin main\n" in deploy_all
    assert "phase/5-email" not in deploy_all
    assert "phase/6-release" not in deploy_all
    assert "phase-2-accepted" not in deploy_all

    assert "[string]$WorkerDigest" in server_push
    assert "[string]$WebDigest" in server_push
    assert "ND_WORKER_DIGEST='$WorkerDigest'" in server_push
    assert "ND_WEB_DIGEST='$WebDigest'" in server_push
    assert "WorkerDigest and WebDigest are both required" in server_push
    assert "Invalid release version" in server_push
    assert "^v[A-Za-z0-9_]" in server_push


def test_deploy_targets_are_explicit_validated_and_forwarded():
    deploy_all = _read("deploy/deploy-all.ps1")
    server_push = _read("deploy/server-push.ps1")
    deploy_bat = _read("deploy.bat")
    installer = _read("deploy/install.sh")
    preflight = _read("deploy/preflight.sh")
    bootstrap = _read("deploy/bootstrap.sh")
    deploy_docs = _read("deploy/README.md")

    for script in (deploy_all, server_push):
        for declaration in (
            "[string]$Server = $env:ND_SERVER",
            "[string]$KeyPath = $env:ND_KEY_PATH",
            "[string]$Owner = $env:ND_OWNER",
            "[string]$AppDir = $env:ND_APP_DIR",
            "[string]$Domain = $env:ND_DOMAIN",
            "[string]$CertbotEmail = $env:ND_CERTBOT_EMAIL",
        ):
            assert declaration in script
        assert "Missing deployment targets" in script

    assert deploy_all.index("Missing deployment targets") < deploy_all.index(
        'foreach ($tool in @("ssh", "scp", "git", "gh"))'
    )
    assert server_push.index("Missing deployment targets") < server_push.index(
        "$LogDir ="
    )
    assert "-Server $Server -KeyPath $KeyPath -Owner $Owner -AppDir $AppDir" in deploy_all
    assert "-Domain $Domain -CertbotEmail $CertbotEmail" in deploy_all
    assert '"ND_OWNER=\'$Owner\' ND_APP_DIR=\'$AppDir\' ND_DOMAIN=\'$Domain\' "' in server_push
    assert '"ND_CERTBOT_EMAIL=\'$CertbotEmail\'' in server_push
    assert '"$deployEnv bash $Incoming/preflight.sh"' in server_push
    assert '"$deployEnv bash $Incoming/bootstrap.sh"' in server_push

    assert "%*" in deploy_bat
    assert "deploy\\deploy-all.ps1\" %*" in deploy_bat
    assert "--interactive" not in deploy_bat.lower()

    assert installer.index("for required_name in") < installer.index(
        'api="https://api.github.com'
    )
    assert 'OWNER_REPO="${ND_OWNER}/news-digest"' in installer
    for script in (installer, preflight, bootstrap):
        assert "ND_OWNER ND_APP_DIR ND_DOMAIN ND_CERTBOT_EMAIL" in script
    assert 'OWNER="$ND_OWNER"' in bootstrap
    assert 'APP_DIR="$ND_APP_DIR"' in bootstrap
    assert 'DOMAIN="$ND_DOMAIN"' in bootstrap
    assert 'CERTBOT_EMAIL="$ND_CERTBOT_EMAIL"' in bootstrap
    assert 'WEB_PORT="${ND_WEB_PORT:-8618}"' in preflight
    assert 'ADMIN_PORT="${ND_ADMIN_PORT:-8619}"' in preflight
    assert "existing_service_owns_port()" in preflight
    assert 'docker compose -f "${APP_DIR}/compose.yaml" ps -q "$role"' in preflight
    assert 'existing_service_owns_port "$role" "$port"' in preflight
    assert 's|--port 8619|--port ${ADMIN_PORT}|g' in bootstrap

    release_artifacts = "\n".join(
        (
            deploy_all,
            server_push,
            deploy_bat,
            installer,
            preflight,
            bootstrap,
            deploy_docs,
        )
    ).lower()
    for personal_value in (
        "root@cheapcoding.top",
        r"c:\users\admin\.ssh",
        "1481835649@qq.com",
    ):
        assert personal_value not in release_artifacts
    assert "仓库外的本地 powershell wrapper" in deploy_docs.lower()


def test_bootstrap_requires_exact_release_version_and_both_digests():
    bootstrap = _read("deploy/bootstrap.sh")

    assert 'TAG="${ND_VERSION:-}"' in bootstrap
    assert "ND_WORKER_DIGEST 与 ND_WEB_DIGEST 必须由同一 Release 成对提供" in bootstrap
    assert 'WORKER_IMAGE="ghcr.io/${OWNER}/news-digest-worker@${WORKER_DIGEST}"' in bootstrap
    assert 'WEB_IMAGE="ghcr.io/${OWNER}/news-digest-web@${WEB_DIGEST}"' in bootstrap
    assert "news-digest-worker:${TAG}" not in bootstrap
    assert "ND_ALLOW_TAG_DOWNGRADE" not in bootstrap


def test_reverse_proxy_timeout_exceeds_side_effect_deadlines():
    nginx = _read("deploy/nginx/news.conf")

    assert "proxy_read_timeout 45m;" in nginx
    assert "proxy_read_timeout 35s;" in nginx


def test_release_installer_binds_latest_metadata_to_bundle_digests():
    installer = _read("deploy/install.sh")

    assert 'release_tag="$(printf' in installer
    assert 'export ND_VERSION="$release_tag"' in installer
    assert '[[ -f "$here/digests.env" ]]' in installer
    assert 'export ND_WORKER_DIGEST="$bundle_worker_digest"' in installer
    assert 'export ND_WEB_DIGEST="$bundle_web_digest"' in installer
    assert "拒绝混用发布工件" in installer
    assert "本地部署必须提供同一 Release" in installer
    assert 'work_dir="$(mktemp -d)"' in installer
    assert "sha256sum -c news-digest-deploy.tgz.sha256" in installer
    assert "--no-same-owner --no-same-permissions" in installer
    assert 'if [[ -n "${GH_TOKEN:-}" ]]' in installer
    assert "缺少 GH_TOKEN" not in installer


def test_first_install_defers_api_and_smtp_configuration_to_admin():
    deploy_all = _read("deploy/deploy-all.ps1")
    server_push = _read("deploy/server-push.ps1")
    bootstrap = _read("deploy/bootstrap.sh")

    assert ".env.local" not in deploy_all
    assert ".env.incoming" not in deploy_all
    assert "TRANSLATION_API_KEY" not in deploy_all
    assert "SMTP_PASSWORD" not in deploy_all
    assert "Admin" in deploy_all

    assert "TRANSLATION_API_BASE_URL=" in bootstrap
    assert "TRANSLATION_API_KEY=" in bootstrap
    assert "TRANSLATION_MODEL=" in bootstrap
    assert "EMAIL_DELIVERY_ENABLED=false" in bootstrap
    assert "SMTP_HOST=" in bootstrap
    assert "SMTP_USERNAME=" in bootstrap
    assert "SMTP_PASSWORD=" in bootstrap
    assert "SMTP_FROM=" in bootstrap
    assert "PUBLIC_SUBSCRIPTION_ENABLED=false" in bootstrap
    assert "exit 2" not in bootstrap
    assert "exit 2" not in server_push
    assert "请在 Admin" in bootstrap


def test_public_subscription_docs_match_runtime_reload_behavior():
    docs = "\n".join(
        _read(path) for path in ("README.md", "deploy/README.md", "docs/OPERATIONS.md")
    )

    assert "Admin 无需重启" in docs
    assert "force-recreate admin" not in docs


def test_bootstrap_backs_up_sqlite_before_starting_database_consumers():
    bootstrap = _read('deploy/bootstrap.sh')
    dq = chr(34)

    function_start = bootstrap.index('backup_database_before_migration()')
    call = bootstrap.index('\nbackup_database_before_migration\n', function_start)
    function_body = bootstrap[function_start:call]
    worker_pull = bootstrap.index('docker pull ' + dq + '$WORKER_IMAGE' + dq)
    web_pull = bootstrap.index('docker pull ' + dq + '$WEB_IMAGE' + dq)
    compose_install = bootstrap.index(
        'install_file ' + dq + '${TMP_DIR}/compose.yaml' + dq, call
    )
    record = bootstrap.index('\nrecord_deployed\n', call)
    admin = bootstrap.index(dq + '${COMPOSE[@]}' + dq + ' up -d web admin')

    assert max(worker_pull, web_pull) < call < compose_install < record < admin
    assert 'DATA_VOLUME=' + dq + 'news-digest_news-data' + dq in function_body
    assert "docker volume ls --format '{{.Name}}'" in function_body
    assert 'docker volume inspect ' + dq + '$DATA_VOLUME' + dq in function_body
    assert 'file:/data/news.db?mode=ro' in function_body
    assert 'uri=True' in function_body
    assert 'source_path.is_symlink()' in function_body
    assert 'not source_path.exists()' in function_body
    assert 'not source_path.is_file()' in function_body
    assert 'source.backup(target)' in function_body
    assert 'target.execute(' + dq + 'PRAGMA integrity_check' + dq + ')' in function_body
    assert '--network none' in function_body
    assert '--user 0:0' in function_body
    assert '--entrypoint python' in function_body
    assert 'mktemp' in function_body
    assert 'install -o root -g root -m 600 /dev/null' in function_body
    assert (
        'chmod 600 ' + dq + '$backup_path' + dq + ' ' + dq + '$checksum_path' + dq
        in function_body
    )
    assert 'sha256sum' in function_body
    assert '拒绝覆盖' in function_body
    assert 'Admin 与 worker 未启动' in function_body
    assert 'news_digest' not in function_body


def test_deployment_never_runs_the_content_pipeline():
    bootstrap = _read("deploy/bootstrap.sh")
    deploy_all = _read("deploy/deploy-all.ps1")

    assert 'run --rm worker run --yes' not in bootstrap
    assert 'run --rm worker run --yes' not in deploy_all
    assert "current_release_is_today" not in bootstrap
    assert bootstrap.index('"${COMPOSE[@]}" up -d web admin') < bootstrap.index(
        "systemctl enable --now news-digest.timer"
    )
    stamp = "touch /var/lib/systemd/timers/stamp-news-digest.timer"
    assert bootstrap.index(stamp) < bootstrap.index(
        "systemctl enable --now news-digest.timer"
    )
    assert "https://$Domain/healthz" in deploy_all
    assert "https://$Domain/);" not in deploy_all
    assert 'if [ "$HEALTH_CODE" != "200" ]' in bootstrap
    assert 'if [ "$ADMIN_CODE" != "200" ]' in bootstrap
    assert '"${COMPOSE[@]}" logs --no-color --tail 100 admin' in bootstrap


def test_admin_translation_actions_activate_a_resume_worker():
    daily_service = _read("deploy/systemd/news-digest.service")
    resume_service = _read("deploy/systemd/news-digest-resume.service")
    wake_path = _read("deploy/systemd/news-digest-wakeup.path")
    bootstrap = _read("deploy/bootstrap.sh")
    server_push = _read("deploy/server-push.ps1")

    assert "OnFailure=news-digest-resume.service" in daily_service
    assert "/usr/bin/flock /run/news-digest-worker.lock" in daily_service
    assert "resume-automation --yes" in resume_service
    assert "Restart=on-failure" in resume_service
    assert "SuccessExitStatus=10" in daily_service
    assert "SuccessExitStatus=10" in resume_service
    assert "RestartPreventExitStatus=10" in resume_service
    assert "/usr/bin/flock -E 75 -n /run/news-digest-worker.lock" in resume_service
    assert "PathChanged=/srv/news-digest/config/automation.wake" in wake_path
    assert "Unit=news-digest-resume.service" in wake_path
    assert "WantedBy=multi-user.target" in wake_path
    assert "news-digest-resume.service" in bootstrap
    assert "news-digest-wakeup.path" in bootstrap
    assert "systemctl enable --now news-digest-wakeup.path" in bootstrap
    assert "news-digest-resume.service" in server_push
    assert "news-digest-wakeup.path" in server_push


def test_bootstrap_inline_python_creates_a_consistent_wal_snapshot(tmp_path):
    source_dir = tmp_path / "data"
    backup_dir = tmp_path / "backup"
    source_dir.mkdir()
    backup_dir.mkdir()
    source_path = source_dir / "news.db"
    backup_path = backup_dir / "news.db"
    backup_path.touch()

    with sqlite3.connect(source_path) as source:
        assert source.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        source.execute("CREATE TABLE entries (value TEXT NOT NULL)")
        source.execute("INSERT INTO entries VALUES ('committed-in-wal')")
        source.commit()

        code = _backup_python(_read("deploy/bootstrap.sh"))
        code = code.replace("/data/news.db", source_path.as_posix()).replace(
            "/backup/news.db", backup_path.as_posix()
        )
        exec(compile(code, "<bootstrap-sqlite-backup>", "exec"), {})

    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert backup.execute("SELECT value FROM entries").fetchall() == [
            ("committed-in-wal",)
        ]


def test_manual_deploy_docs_cover_permissions_digests_and_database_recovery():
    readme = _read('deploy/README.md')
    digest_section = readme.split('## 4. 固定镜像 digest', 1)[1].split(
        '## 5. GHCR 登录并拉取', 1
    )[0]

    assert 'sudo chown root:10001 /srv/news-digest/config' in readme
    assert digest_section.count('news-digest-worker@sha256:DIGEST') == 2
    assert digest_section.count('news-digest-web@sha256:DIGEST') == 1
    assert 'worker 与 admin' in digest_section
    freeze = digest_section.index('sudo systemctl stop news-digest.timer')
    backup = digest_section.index('完成迁移前 SQLite online backup')
    edit = digest_section.index('编辑 `/srv/news-digest/compose.yaml`')
    assert freeze < backup < edit
    assert 'sudo systemctl is-active --quiet news-digest.service' in digest_section
    assert 'sudo docker compose stop admin' in digest_section
    assert 'sudo systemctl start news-digest.timer' in readme
    assert 'SQLite online backup' in readme
    assert 'tar 仅适用于 Admin 与 worker 停止写入后的整卷归档' in readme
    assert '镜像回滚不会自动恢复数据库' in readme
    assert '人工选择指定的迁移前备份' in readme


def test_admin_creates_shared_sqlite_files_for_the_non_root_worker():
    compose = _read("deploy/compose.yaml")

    assert 'user: "0:10001"' in compose
    assert 'entrypoint: ["/bin/sh", "-c"]' in compose
    assert "umask 0002; exec news-digest admin" in compose
