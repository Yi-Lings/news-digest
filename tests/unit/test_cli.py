"""Minimal toolchain-verification tests for the CLI."""

import datetime as dt
import json
import os
import types
from pathlib import Path

import pytest

from news_digest import __version__
from news_digest.admin_providers import AdminConfigError, save_profiles
from news_digest.cli import (
    _run_admin,
    _run_automation_daily,
    _run_automation_resume,
    _run_daily,
    _run_preview,
    _run_preview_email,
    _run_site,
    _run_site_admin,
    _runtime_translation_config,
    build_parser,
    main,
)
from news_digest.config import BuildConfig, FetchConfig, SmtpConfig
from news_digest.models import Article, DailyEdition, Paragraph
from news_digest.storage import db
from news_digest.translation.client import TranslationError


def test_version_option_reports_package_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_bare_invocation_prints_help_and_succeeds(capsys):
    assert main([]) == 0
    assert "news-digest" in capsys.readouterr().out


def test_site_admin_cli_previews_grants_and_revokes_active_account(
    tmp_path, monkeypatch, capsys
):
    database = tmp_path / "news.db"
    monkeypatch.setenv("NEWS_DATABASE_PATH", str(database))
    conn = db.connect(database)
    user = db.upsert_pending_user(
        conn,
        email="operator@example.com",
        email_key=db.delivery_recipient_key("operator@example.com"),
        password_hash="pbkdf2_sha256$1$00$00",
        now="2026-08-30T12:00:00+00:00",
    )
    user = db.activate_user(
        conn, email_key=user.email_key, now="2026-08-30T12:00:00+00:00"
    )
    conn.close()

    assert _run_site_admin("operator@example.com", revoke=False, yes=False) == 0
    conn = db.connect(database)
    assert db.user_by_id(conn, user.id).is_admin is False
    conn.close()
    assert "预览模式" in capsys.readouterr().out

    assert _run_site_admin("operator@example.com", revoke=False, yes=True) == 0
    conn = db.connect(database)
    assert db.user_by_id(conn, user.id).is_admin is True
    conn.close()

    assert _run_site_admin("operator@example.com", revoke=True, yes=True) == 0
    conn = db.connect(database)
    assert db.user_by_id(conn, user.id).is_admin is False
    conn.close()


def test_site_admin_cli_rejects_missing_or_disabled_account(tmp_path, monkeypatch):
    database = tmp_path / "news.db"
    monkeypatch.setenv("NEWS_DATABASE_PATH", str(database))
    conn = db.connect(database)
    user = db.upsert_pending_user(
        conn,
        email="disabled@example.com",
        email_key=db.delivery_recipient_key("disabled@example.com"),
        password_hash="pbkdf2_sha256$1$00$00",
        now="2026-08-30T12:00:00+00:00",
    )
    user = db.activate_user(
        conn, email_key=user.email_key, now="2026-08-30T12:00:00+00:00"
    )
    db.set_user_status(
        conn, user.id, status="disabled", now="2026-08-30T12:01:00+00:00"
    )
    conn.close()

    assert _run_site_admin("missing@example.com", revoke=False, yes=True) == 1
    assert _run_site_admin("disabled@example.com", revoke=False, yes=True) == 1


def test_site_admin_parser_requires_email_and_supports_revoke():
    args = build_parser().parse_args(
        ["site-admin", "--email", "operator@example.com", "--revoke", "--yes"]
    )
    assert args.command == "site-admin"
    assert args.email == "operator@example.com"
    assert args.revoke is True
    assert args.yes is True


def test_runtime_translation_uses_local_provider_authority(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRANSLATION_PROVIDERS_FILE", raising=False)
    monkeypatch.setenv("TRANSLATION_API_BASE_URL", "https://legacy.example.com/v1")
    monkeypatch.setenv("TRANSLATION_API_KEY", "legacy-key")
    monkeypatch.setenv("TRANSLATION_MODEL", "legacy-model")
    provider = {
        "base_url": "https://current.example.com/v1",
        "api_key": "current-key",
        "model": "current-model",
        "api_type": "anthropic_messages",
        "stream": False,
        "enabled": True,
        "is_default": True,
    }
    save_profiles(tmp_path, {"providers": {"current": provider}})

    config = _runtime_translation_config()

    assert config.base_url == provider["base_url"]
    assert config.api_key == provider["api_key"]
    assert config.model == provider["model"]
    assert config.api_type == provider["api_type"]
    assert config.stream is provider["stream"]


def test_runtime_translation_does_not_fallback_when_local_profiles_have_no_default(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRANSLATION_PROVIDERS_FILE", raising=False)
    monkeypatch.setenv("TRANSLATION_API_BASE_URL", "https://legacy.example.com/v1")
    monkeypatch.setenv("TRANSLATION_API_KEY", "legacy-key")
    monkeypatch.setenv("TRANSLATION_MODEL", "legacy-model")
    provider = {
        "base_url": "https://current.example.com/v1",
        "api_key": "current-key",
        "model": "current-model",
        "api_type": "openai_chat",
        "stream": True,
        "enabled": True,
        "is_default": False,
    }
    save_profiles(tmp_path, {"providers": {"current": provider}})

    with pytest.raises(AdminConfigError, match="未设置默认翻译档案"):
        _runtime_translation_config()


def test_runtime_translation_does_not_fallback_when_local_profiles_are_missing(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRANSLATION_PROVIDERS_FILE", raising=False)
    monkeypatch.setenv("TRANSLATION_API_BASE_URL", "https://legacy.example.com/v1")
    monkeypatch.setenv("TRANSLATION_API_KEY", "legacy-key")
    monkeypatch.setenv("TRANSLATION_MODEL", "legacy-model")

    with pytest.raises(AdminConfigError, match="未设置默认翻译档案"):
        _runtime_translation_config()


def _daily_mocks(monkeypatch, tmp_path, *, enabled, delivery=None, build_error=None):
    fetch = FetchConfig(None, 24, "Asia/Shanghai", tmp_path / "data")
    smtp = SmtpConfig(
        host="smtp.example.com" if enabled else "",
        port=587,
        username="",
        password="",
        sender="news@example.com" if enabled else "",
        recipients=("reader@example.com",) if enabled else (),
        delivery_enabled=enabled,
        security="starttls",
    )
    monkeypatch.setattr("news_digest.cli._fetch_config", lambda window: fetch)
    monkeypatch.setattr(
        "news_digest.pipeline.fetch_daily",
        lambda config: (None, types.SimpleNamespace(per_source={})),
    )
    monkeypatch.setattr("news_digest.pipeline.load_db_editions", lambda config: [object()])
    if build_error is None:
        release = tmp_path / "site" / "releases" / "2026-07-27-01"
        release.mkdir(parents=True)
        monkeypatch.setattr("news_digest.pipeline.build_editions", lambda *args: release)
    else:
        monkeypatch.setattr(
            "news_digest.pipeline.build_editions",
            lambda *args: (_ for _ in ()).throw(build_error),
        )
    monkeypatch.setattr(
        "news_digest.config.build_config_from_env",
        lambda: BuildConfig(tmp_path / "site", "https://news.example.com"),
    )
    monkeypatch.setattr("news_digest.config.smtp_config_from_env", lambda: smtp)
    monkeypatch.setattr(
        "news_digest.config.email_delivery_enabled_from_env", lambda: enabled
    )
    if delivery is not None:
        monkeypatch.setattr(
            "news_digest.delivery.delivery_service.deliver_published",
            lambda *args, **kwargs: delivery,
        )


def test_daily_disabled_skips_fourth_stage_without_changing_success(tmp_path, monkeypatch, capsys):
    _daily_mocks(monkeypatch, tmp_path, enabled=False)
    assert _run_daily(None, False) == 0
    output = capsys.readouterr().out
    assert "[1/4]" in output and "[4/4] 投递" in output
    assert "邮件未启用，已跳过" in output


def test_daily_disabled_does_not_parse_stale_smtp_configuration(
    tmp_path, monkeypatch, capsys
):
    _daily_mocks(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setattr(
        "news_digest.config.smtp_config_from_env",
        lambda: (_ for _ in ()).throw(ValueError("stale SMTP")),
    )
    assert _run_daily(None, False) == 0
    assert "邮件未启用，已跳过" in capsys.readouterr().out


def test_daily_build_failure_never_sends(tmp_path, monkeypatch, capsys):
    called = False

    def delivery(*args, **kwargs):
        nonlocal called
        called = True

    _daily_mocks(
        monkeypatch,
        tmp_path,
        enabled=True,
        delivery=delivery,
        build_error=RuntimeError("broken build"),
    )
    assert _run_daily(None, False) == 1
    assert called is False
    assert "构建未成功，未发送邮件" in capsys.readouterr().out


def test_daily_translation_failure_still_builds_and_delivers(tmp_path, monkeypatch):
    result = types.SimpleNamespace(
        sent_count=1,
        failed_count=0,
        unknown_count=0,
        skipped_count=0,
        archive_status="archived",
        succeeded=True,
    )
    _daily_mocks(monkeypatch, tmp_path, enabled=True, delivery=result)
    monkeypatch.setattr(
        "news_digest.cli._translate_edition_for", lambda date, config: None
    )
    assert _run_daily(None, True) == 1


def test_daily_yes_uses_persistent_article_automation_not_bulk_translation(
    tmp_path, monkeypatch
):
    _daily_mocks(monkeypatch, tmp_path, enabled=False)
    fetched = object()
    monkeypatch.setattr(
        "news_digest.pipeline.fetch_daily",
        lambda config: (fetched, types.SimpleNamespace(per_source={})),
    )
    captured = []
    monkeypatch.setattr(
        "news_digest.cli._run_automation_daily",
        lambda fetch_config, edition: captured.append((fetch_config, edition)) or 0,
    )
    monkeypatch.setattr(
        "news_digest.cli._translate_edition_for",
        lambda *args: (_ for _ in ()).throw(AssertionError("legacy bulk translation called")),
    )

    assert _run_daily(None, True) == 0
    assert captured and captured[0][1] is fetched


def test_production_automation_waits_and_retries_only_failed_article(
    tmp_path, monkeypatch
):
    now = [dt.datetime(2026, 7, 28, tzinfo=dt.UTC)]
    calls = 0
    build_counts = []
    delivery_calls = []
    fetch_config = FetchConfig(None, 24, "Asia/Shanghai", tmp_path / "data")
    article = Article(
        slug="article-1",
        source="Fixture",
        title_en="Fixture article",
        summary_en="Fixture summary.",
        author="Fixture",
        published_at=now[0].isoformat(),
        url="https://example.com/article-1",
        reading_minutes=1,
        paragraphs=[Paragraph(en="Fixture paragraph.")],
    )
    edition = DailyEdition(date="2026-07-28", articles=[article])

    class FakeTranslator:
        label = "fake@automation"
        model = "fake"
        cache_identity = "production-automation-fake"

        def __init__(self, config):
            pass

        def translate(self, current):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TranslationError("redacted", category="network")
            return json.dumps(
                {
                    "title_zh": "测试标题",
                    "summary_zh": "测试摘要。",
                    "sentences_zh": [["测试段落。"]],
                    "vocabulary": [
                        {
                            "word": word,
                            "phonetic": "/test/",
                            "meaning_zh": "测试",
                            "example_en": f"{word} is used in a fixture.",
                        }
                        for word in ("first", "second", "third")
                    ],
                    "collocations": [
                        {
                            "phrase": "test fixture",
                            "meaning_zh": "测试夹具",
                            "example_en": "This is a test fixture.",
                        }
                    ],
                    "sentence_notes": [
                        {
                            "sentence_en": "Fixture paragraph.",
                            "translation_zh": "测试段落。",
                            "analysis_zh": "测试句。",
                        }
                    ],
                },
                ensure_ascii=False,
            )

        def close(self):
            pass

    def load_editions(config):
        conn = db.connect(config.database)
        try:
            return [db.get_edition(conn, edition.date)]
        finally:
            conn.close()

    release = tmp_path / "site" / "releases" / "2026-07-28-01"

    def build_editions(editions, config):
        build_counts.append(len(editions[0].articles))
        release.mkdir(parents=True, exist_ok=True)
        return release

    report = types.SimpleNamespace(
        status="skipped",
        sent_count=0,
        failed_count=0,
        unknown_count=0,
        skipped_count=0,
        succeeded=False,
    )
    monkeypatch.setattr("news_digest.translation.client.ApiTranslator", FakeTranslator)
    monkeypatch.setattr(
        "news_digest.cli._runtime_translation_config",
        lambda: types.SimpleNamespace(cache_dir=tmp_path / "cache"),
    )
    monkeypatch.setattr(
        "news_digest.config.build_config_from_env",
        lambda: BuildConfig(tmp_path / "site", "https://news.example.com"),
    )
    monkeypatch.setattr(
        "news_digest.config.email_delivery_enabled_from_env", lambda: False
    )
    monkeypatch.setattr(
        "news_digest.pipeline.selected_mains_for_translation",
        lambda config, date: edition,
    )
    monkeypatch.setattr("news_digest.pipeline.load_db_editions", load_editions)
    monkeypatch.setattr("news_digest.pipeline.build_editions", build_editions)
    monkeypatch.setattr(
        "news_digest.delivery.delivery_service.deliver_published",
        lambda *args, **kwargs: delivery_calls.append(kwargs["edition_date"]) or report,
    )

    def sleep(seconds):
        now[0] += dt.timedelta(seconds=seconds)

    assert (
        _run_automation_daily(
            fetch_config,
            edition,
            clock=lambda: now[0],
            sleep=sleep,
        )
        == 0
    )
    assert calls == 2
    assert build_counts == [1]
    assert delivery_calls == [edition.date]
    assert now[0] == dt.datetime(2026, 7, 28, 0, 0, 17, tzinfo=dt.UTC)


def test_automation_drains_ready_tasks_before_stopping_on_terminal_failure(
    tmp_path, monkeypatch
):
    fetch_config = FetchConfig(None, 24, "Asia/Shanghai", tmp_path / "data")
    article = Article(
        slug="article-1",
        source="Fixture",
        title_en="Fixture article",
        summary_en="Fixture summary.",
        author="Fixture",
        published_at="2026-07-28T00:00:00+00:00",
        url="https://example.com/article-1",
        reading_minutes=1,
        paragraphs=[Paragraph(en="Fixture paragraph.")],
    )
    edition = DailyEdition(date="2026-07-28", articles=[article])
    holder = {}

    class FakeTranslator:
        cache_identity = "fake-cache"

        def __init__(self, config):
            del config

        def close(self):
            pass

    class FakeRunner:
        def __init__(self, **kwargs):
            del kwargs
            self.calls = 0
            holder["runner"] = self

        def seed_edition(self, edition, *, now):
            del edition, now

        def run_ready(self, *, now, owner, max_tasks):
            del now, owner, max_tasks
            self.calls += 1
            claimed = 1 if self.calls == 1 else 0
            return types.SimpleNamespace(
                claimed=claimed, succeeded=0, failed=1, blocked=0
            )

        def flush_build(self, *, now, owner):
            del now, owner
            return False

        def flush_delivery(self, *, edition_date, now):
            del edition_date, now
            return False

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr("news_digest.translation.client.ApiTranslator", FakeTranslator)
    monkeypatch.setattr(
        "news_digest.cli._runtime_translation_config",
        lambda: types.SimpleNamespace(cache_dir=tmp_path / "cache"),
    )
    monkeypatch.setattr(
        "news_digest.config.build_config_from_env",
        lambda: BuildConfig(tmp_path / "site", "https://news.example.com"),
    )
    monkeypatch.setattr("news_digest.config.email_delivery_enabled_from_env", lambda: False)
    monkeypatch.setattr(
        "news_digest.pipeline.selected_mains_for_translation",
        lambda config, date: edition,
    )
    monkeypatch.setattr(
        "news_digest.translation.automation.TranslationAutomationRunner", FakeRunner
    )
    monkeypatch.setattr("news_digest.storage.db.connect", lambda database: FakeConnection())
    monkeypatch.setattr(
        "news_digest.storage.db.recover_interrupted_translation_tasks",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(
        "news_digest.storage.db.run_worker_maintenance",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "news_digest.storage.db.automation_edition",
        lambda conn, date: types.SimpleNamespace(status="partial"),
    )
    monkeypatch.setattr(
        "news_digest.storage.db.list_translation_tasks",
        lambda conn, date: [types.SimpleNamespace(status="failed", auto_retry=False)],
    )

    assert (
        _run_automation_daily(
            fetch_config,
            edition,
            clock=lambda: dt.datetime(2026, 7, 28, tzinfo=dt.UTC),
            sleep=lambda seconds: None,
        )
        == 10
    )
    assert holder["runner"].calls == 2


def test_daily_partial_or_archive_failure_returns_nonzero(tmp_path, monkeypatch):
    result = types.SimpleNamespace(
        sent_count=1,
        failed_count=1,
        unknown_count=0,
        skipped_count=0,
        archive_status="failed",
        succeeded=False,
    )
    _daily_mocks(monkeypatch, tmp_path, enabled=True, delivery=result)
    assert _run_daily(None, False) == 1


def test_preview_email_loads_dotenv_and_uses_valid_placeholder_sender(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text(
        "NEWS_OUTPUT_PATH=custom/site\n"
        "NEWS_DATABASE_PATH=custom/news.db\n"
        "NEWS_SITE_URL=https://news.example.com\n"
        "EMAIL_LAYOUT=compact\n",
        encoding="utf-8",
    )
    env_names = (
        "NEWS_OUTPUT_PATH",
        "NEWS_DATABASE_PATH",
        "NEWS_SITE_URL",
        "EMAIL_LAYOUT",
    )
    for name in env_names:
        monkeypatch.setenv(name, "existing-test-value")
        os.environ.pop(name)

    captured = {}

    def preview_published(**kwargs):
        captured.update(kwargs)
        captured["email_layout"] = os.environ.get("EMAIL_LAYOUT")
        return types.SimpleNamespace(
            release=types.SimpleNamespace(
                release_date="2026-07-27",
                edition=object(),
            ),
            rendered=types.SimpleNamespace(
                subject="Daily digest",
                text="Plain body",
                html="<p>HTML body</p>",
            ),
        )

    monkeypatch.setattr(
        "news_digest.delivery.delivery_service.preview_published", preview_published
    )

    try:
        assert _run_preview_email(None) == 0
        assert captured["output_root"] == Path("custom/site")
        assert captured["database"] == Path("custom/news.db")
        assert captured["site_url"] == "https://news.example.com"
        assert captured["email_layout"] == "compact"
        eml = (tmp_path / "var/mail/2026-07-27.eml").read_text(encoding="utf-8")
        assert "From: preview@invalid.example" in eml
        assert "To: preview@invalid.example" in eml
    finally:
        for name in env_names:
            os.environ[name] = "existing-test-value"


def test_preview_loads_local_runtime_and_wires_mail_admin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "custom/site/current").mkdir(parents=True)
    (tmp_path / "custom/site/current/index.html").write_text("ok", encoding="utf-8")
    (tmp_path / ".env.local").write_text(
        "NEWS_OUTPUT_PATH=custom/site\n"
        "NEWS_DATABASE_PATH=custom/news.db\n"
        "NEWS_TIMEZONE=Asia/Hong_Kong\n"
        "PUBLIC_SUBSCRIPTION_ENABLED=true\n",
        encoding="utf-8",
    )
    env_names = (
        "NEWS_OUTPUT_PATH",
        "NEWS_DATABASE_PATH",
        "NEWS_SITE_URL",
        "NEWS_TIMEZONE",
        "PUBLIC_SUBSCRIPTION_ENABLED",
    )
    for name in env_names:
        monkeypatch.setenv(name, "existing-test-value")
        os.environ.pop(name)

    captured = {}

    class FakeServer:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            captured["closed"] = True

    def create_server(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeServer()

    monkeypatch.setattr("news_digest.preview_server.create_server", create_server)

    assert _run_preview(0) == 0
    assert captured["args"] == (tmp_path, Path("custom/site/current"), 0)
    assert captured["kwargs"] == {
        "db_path": Path("custom/news.db"),
        "site_url": "http://127.0.0.1:8618",
        "output_root": Path("custom/site"),
        "timezone": "Asia/Hong_Kong",
        "public_subscription_enabled": True,
        "loopback_public_subscription": True,
        "translation_db_path": None,
        "translation_wakeup_callback": None,
    }
    assert captured["closed"] is True


def test_production_admin_wires_translation_wakeup_file(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "NEWS_SITE_URL=https://news.example.com\n"
        f"NEWS_DATABASE_PATH={tmp_path / 'news.db'}\n"
        f"NEWS_OUTPUT_PATH={tmp_path / 'site'}\n"
        "PUBLIC_SUBSCRIPTION_ENABLED=false\n",
        encoding="utf-8",
    )
    for name in (
        "NEWS_SITE_URL",
        "NEWS_DATABASE_PATH",
        "NEWS_OUTPUT_PATH",
        "PUBLIC_SUBSCRIPTION_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    captured = {}

    class FakeServer:
        def serve_forever(self):
            callback = captured["kwargs"]["translation_wakeup_callback"]
            assert callable(callback)
            callback()
            captured["wake_value"] = (config_dir / "automation.wake").read_text(
                encoding="ascii"
            )
            raise KeyboardInterrupt

        def server_close(self):
            captured["closed"] = True

    def create_server(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeServer()

    monkeypatch.setattr("news_digest.preview_server.create_server", create_server)

    assert _run_admin(8619, config_dir) == 0
    assert captured["args"] == (config_dir, config_dir, 8619)
    assert captured["wake_value"].strip().isdigit()
    assert captured["closed"] is True


def test_production_admin_projects_only_site_runtime_configuration(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    site_config_dir = tmp_path / "site-config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "NEWS_SITE_URL=https://news.example.com\n"
        "NEWS_TIMEZONE=Asia/Shanghai\n"
        "PUBLIC_SUBSCRIPTION_ENABLED=false\n"
        "SMTP_HOST=smtp.example.com\n"
        "TRANSLATION_API_KEY=must-not-be-projected\n"
        "EPAY_ENABLED=false\n",
        encoding="utf-8",
    )
    for name in ("NEWS_SITE_URL", "NEWS_TIMEZONE", "PUBLIC_SUBSCRIPTION_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    captured = {}

    class FakeServer:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def create_server(*args, **kwargs):
        captured["kwargs"] = kwargs
        return FakeServer()

    monkeypatch.setattr("news_digest.preview_server.create_server", create_server)

    assert _run_admin(8619, config_dir, site_config_dir) == 0
    projected = (site_config_dir / ".env").read_text(encoding="utf-8")
    assert "SMTP_HOST=smtp.example.com" in projected
    assert "TRANSLATION_API_KEY" not in projected
    assert captured["kwargs"]["site_env_path"] == site_config_dir / ".env"


def test_site_verification_smtp_hot_reloads_and_ignores_digest_delivery_switch(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "site-config"
    config_dir.mkdir()
    env_path = config_dir / ".env"

    def write_env(host: str) -> None:
        env_path.write_text(
            "NEWS_SITE_URL=https://news.example.com\n"
            "NEWS_TIMEZONE=Asia/Shanghai\n"
            "NEWS_DATABASE_PATH=var/data/news.db\n"
            "NEWS_OUTPUT_PATH=var/site\n"
            "EMAIL_DELIVERY_ENABLED=false\n"
            f"SMTP_HOST={host}\n"
            "SMTP_PORT=465\n"
            "SMTP_USERNAME=mailer\n"
            "SMTP_PASSWORD=secret\n"
            "SMTP_SECURITY=implicit_tls\n"
            "SMTP_FROM=news@example.com\n"
            "EPAY_ENABLED=false\n",
            encoding="utf-8",
        )

    write_env("smtp-one.example.com")
    for name in (
        "NEWS_SITE_URL",
        "NEWS_TIMEZONE",
        "NEWS_DATABASE_PATH",
        "NEWS_OUTPUT_PATH",
        "EMAIL_DELIVERY_ENABLED",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_SECURITY",
        "SMTP_FROM",
        "EPAY_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    sent = []
    captured = {}

    def send_verification(config, email, code, purpose):
        sent.append((config.host, config.delivery_enabled, purpose))

    class FakeServer:
        def serve_forever(self):
            captured["code_sender"]("first@example.com", "123456", "register")
            write_env("smtp-two.example.com")
            captured["code_sender"]("second@example.com", "654321", "reset")
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def create_site_server(**kwargs):
        captured.update(kwargs)
        return FakeServer()

    monkeypatch.setattr(
        "news_digest.delivery.mailer.send_verification_code", send_verification
    )
    monkeypatch.setattr("news_digest.site_server.create_site_server", create_site_server)

    assert _run_site(8620, config_dir) == 0
    assert sent == [
        ("smtp-one.example.com", False, "register"),
        ("smtp-two.example.com", False, "reset"),
    ]


def test_resume_automation_is_a_noop_without_unfinished_editions(tmp_path, monkeypatch):
    database = tmp_path / "news.db"
    monkeypatch.setattr(
        "news_digest.cli._fetch_config",
        lambda _window: types.SimpleNamespace(database=database),
    )
    called = []
    monkeypatch.setattr(
        "news_digest.cli._run_automation_daily",
        lambda *_args, **_kwargs: called.append("run") or 1,
    )

    assert _run_automation_resume(True) == 0
    assert called == []


def test_resume_automation_selects_latest_unfinished_edition(tmp_path, monkeypatch):
    database = tmp_path / "news.db"
    conn = db.connect(database)
    try:
        db.ensure_automation_edition(
            conn,
            "2026-08-01",
            target_count=1,
            now="2026-08-01T00:00:00+00:00",
        )
    finally:
        conn.close()
    fetch_config = types.SimpleNamespace(database=database)
    monkeypatch.setattr("news_digest.cli._fetch_config", lambda _window: fetch_config)
    captured = {}

    def run_daily(config, edition):
        captured["config"] = config
        captured["date"] = edition.date
        return 7

    monkeypatch.setattr("news_digest.cli._run_automation_daily", run_daily)

    assert _run_automation_resume(True) == 7
    assert captured == {"config": fetch_config, "date": "2026-08-01"}


def test_preview_automation_demo_uses_isolated_database_and_fake_wakeup(
    tmp_path, monkeypatch
):
    site_root = tmp_path / "site"
    (site_root / "current").mkdir(parents=True)
    (site_root / "current/index.html").write_text("ok", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("news_digest.config.load_env_file", lambda: None)
    monkeypatch.setattr(
        "news_digest.config.build_config_from_env",
        lambda: BuildConfig(output_root=site_root, site_url="http://127.0.0.1:8765"),
    )
    monkeypatch.setattr(
        "news_digest.config.fetch_config_from_env",
        lambda: FetchConfig(
            proxy=None,
            window_hours=24,
            timezone="Asia/Shanghai",
            data_dir=tmp_path / "business-data",
        ),
    )
    captured = {}
    edition = object()

    class FakeDemo:
        def __init__(self, database, given_edition, cache_dir):
            self.database = database
            self.wakeups = 0
            captured["demo"] = (database, given_edition, cache_dir)
            captured["demo_instance"] = self

        def wakeup(self):
            self.wakeups += 1

    class FakeServer:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def create_server(*args, **kwargs):
        captured["server"] = (args, kwargs)
        return FakeServer()

    monkeypatch.setattr(
        "news_digest.translation.demo.build_demo_edition", lambda: edition
    )
    monkeypatch.setattr(
        "news_digest.translation.demo.TranslationAutomationDemo", FakeDemo
    )
    monkeypatch.setattr("news_digest.preview_server.create_server", create_server)

    assert _run_preview(8765, automation_demo=True) == 0
    expected_db = tmp_path / "var/data/automation-demo-8765.db"
    assert captured["demo"] == (
        expected_db,
        edition,
        tmp_path / "var/data/automation-demo-8765-cache",
    )
    _, kwargs = captured["server"]
    assert kwargs["db_path"] == tmp_path / "business-data/news.db"
    assert kwargs["translation_db_path"] == expected_db
    assert callable(kwargs["translation_wakeup_callback"])
    assert captured["demo_instance"].wakeups == 0


def test_subcommands_parse():
    parser = build_parser()
    args = parser.parse_args(["build", "--fixtures", "tests/fixtures/demo"])
    assert args.command == "build"
    assert args.fixtures == "tests/fixtures/demo"
    args = parser.parse_args(["build"])
    assert args.fixtures is None
    args = parser.parse_args(["fetch", "--window-hours", "12"])
    assert args.command == "fetch"
    assert args.window_hours == 12
    args = parser.parse_args(["translate", "--limit", "3", "--yes"])
    assert args.command == "translate"
    assert args.limit == 3
    assert args.yes is True
    args = parser.parse_args(["preview", "--automation-demo"])
    assert args.command == "preview" and args.automation_demo is True
    args = parser.parse_args(["resume-automation", "--yes"])
    assert args.command == "resume-automation" and args.yes is True
    args = parser.parse_args(["translate"])
    assert args.yes is False
    assert args.redo == []
    args = parser.parse_args(["translate", "--redo", "a", "--redo", "b", "--yes"])
    assert args.redo == ["a", "b"]
    args = parser.parse_args(["preview-email"])
    assert args.command == "preview-email" and args.date is None
    args = parser.parse_args(
        [
            "send-email",
            "--date",
            "2026-07-26",
            "--retry-unknown",
            "--confirm-unknown-risk",
            "--yes",
        ]
    )
    assert args.command == "send-email"
    assert args.retry_unknown is True
    assert args.confirm_unknown_risk is True
    assert args.yes is True
    args = parser.parse_args(["send-email", "--resend"])
    assert args.resend is True
