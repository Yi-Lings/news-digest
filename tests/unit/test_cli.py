"""Minimal toolchain-verification tests for the CLI."""

import os
import types
from pathlib import Path

import pytest

from news_digest import __version__
from news_digest.admin_providers import AdminConfigError, save_profiles
from news_digest.cli import (
    _run_daily,
    _run_preview,
    _run_preview_email,
    _runtime_translation_config,
    build_parser,
    main,
)
from news_digest.config import BuildConfig, FetchConfig, SmtpConfig


def test_version_option_reports_package_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_bare_invocation_prints_help_and_succeeds(capsys):
    assert main([]) == 0
    assert "news-digest" in capsys.readouterr().out


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
    }
    assert captured["closed"] is True


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
