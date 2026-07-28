import re
import sqlite3
from pathlib import Path

from news_digest import __version__

ROOT = Path(__file__).parents[2]


def test_phase_8_release_version_is_v1_2_1():
    assert __version__ == "1.2.1"


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


def test_first_install_requires_smtp_only_when_delivery_is_enabled():
    deploy_all = _read("deploy/deploy-all.ps1")

    assert '$pairs["EMAIL_DELIVERY_ENABLED"] -notin @("true", "false")' in deploy_all
    assert '$pairs["SMTP_USE_TLS"] -notin @("true", "false")' in deploy_all
    assert 'if ($pairs["EMAIL_DELIVERY_ENABLED"] -eq "true")' in deploy_all
    assert '$required += @("SMTP_HOST", "SMTP_PORT", "SMTP_FROM")' in deploy_all
    legacy_required = (
        '$required += @("SMTP_HOST", "SMTP_PORT", "SMTP_FROM", "SMTP_RECIPIENTS")'
    )
    assert legacy_required not in deploy_all
    assert '"SMTP_RECIPIENTS" = ""' in deploy_all
    assert "function ConvertFrom-SmtpPasswordEnvValue" in deploy_all
    assert "function ConvertTo-SmtpPasswordEnvValue" in deploy_all
    assert "catch {\n        return $Value\n    }" in deploy_all
    assert 'if ($k -eq "SMTP_PASSWORD")' in deploy_all
    assert (
        '$pairs["SMTP_PASSWORD"] = ConvertTo-SmtpPasswordEnvValue '
        '$pairs["SMTP_PASSWORD"]'
    ) in deploy_all


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
    worker = bootstrap.index(dq + '${COMPOSE[@]}' + dq + ' run --rm worker run --yes')

    assert max(worker_pull, web_pull) < call < compose_install < record < admin < worker
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
