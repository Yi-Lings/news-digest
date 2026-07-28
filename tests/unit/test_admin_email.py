"""Offline tests for Admin-managed SMTP settings and target validation."""

import concurrent.futures
import os
import stat

import pytest

from news_digest.admin_email import (
    MANAGED_KEYS,
    AdminEmailError,
    clear_password,
    configs_from_form,
    read_env,
    save_settings,
    settings_payload,
    validate_smtp_target,
)
from news_digest.config import load_env_file, smtp_config_from_env

PUBLIC = lambda host, port: ["93.184.216.34"]  # noqa: E731


def _form(**overrides):
    form = {
        "delivery_enabled": True,
        "host": "smtp.example.com",
        "port": 2525,
        "username": "operator",
        "password": "new-secret",
        "security": "starttls",
        "sender": "news@example.com",
        "recipients": ["One@example.com", "one@example.com", "two@example.com"],
        "mains_enabled": True,
        "briefs_enabled": True,
        "main_limit": 2,
        "brief_limit": 1,
        "language": "bi",
        "source_filters": ["BBC News"],
        "layout": "digest",
        "summary_length": "standard",
        "catchup_window_hours": 6,
    }
    form.update(overrides)
    return form


def test_save_reload_preserves_unmanaged_keys_password_and_mode(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# keep\nNEWS_TIMEZONE=Asia/Shanghai\nSMTP_PASSWORD=old-secret\n", encoding="utf-8"
    )
    smtp, content = save_settings(
        path,
        _form(password=""),
        published_main_count=3,
        published_brief_count=2,
        resolver=PUBLIC,
    )
    assert smtp.password == "old-secret"
    assert smtp.recipients == ()
    assert content.source_filters == ("BBC News",)
    values = read_env(path)
    assert values["NEWS_TIMEZONE"] == "Asia/Shanghai"
    assert values["SMTP_PASSWORD"] == "old-secret"
    assert values["SMTP_PORT"] == "2525"
    assert values["EMAIL_MAIN_LIMIT"] == "2"
    assert "SMTP_USE_TLS" not in values
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = settings_payload(values, published_main_count=3, published_brief_count=2)
    assert payload["password_set"] is True
    assert "recipients" not in payload
    assert "password" not in payload
    assert "old-secret" not in repr(payload)


@pytest.mark.parametrize(
    "password",
    [
        " leading and trailing ",
        '"quoted-secret"',
        "'single-quoted'",
        r'back\\slash"quote',
        "cash$money",
        "${SMTP_HOST}-literal",
        "hash#fragment",
        "中文密码",
        " ${SMTP_HOST} # 'quoted' \\ 中文 ",
    ],
)
def test_smtp_password_round_trips_dotenv_special_characters(
    tmp_path, monkeypatch, password
):
    path = tmp_path / ".env.local"
    save_settings(
        path,
        _form(password=password),
        published_main_count=3,
        published_brief_count=2,
        resolver=PUBLIC,
    )

    stored = next(
        line.partition("=")[2]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("SMTP_PASSWORD=")
    )
    assert stored.startswith("nd-b64-v1:")
    assert all(character not in stored for character in ("$", "#", '"', "'", "\\", " "))
    assert read_env(path)["SMTP_PASSWORD"] == stored
    assert smtp_config_from_env(read_env(path)).password == password
    for name in MANAGED_KEYS:
        monkeypatch.setenv(name, "existing-test-value")
    os.environ.pop("SMTP_PASSWORD")
    try:
        load_env_file(path)
        assert os.environ["SMTP_PASSWORD"] == stored
    finally:
        os.environ["SMTP_PASSWORD"] = "existing-test-value"


def test_smtp_password_that_looks_like_managed_token_is_decoded_once(tmp_path, monkeypatch):
    password = "nd-b64-v1:c2VjcmV0"
    path = tmp_path / ".env.local"
    save_settings(
        path,
        _form(password=password),
        published_main_count=3,
        published_brief_count=2,
        resolver=PUBLIC,
    )

    stored = read_env(path)["SMTP_PASSWORD"]
    assert stored != password
    assert smtp_config_from_env(read_env(path)).password == password

    save_settings(
        path,
        _form(password=""),
        published_main_count=3,
        published_brief_count=2,
        resolver=PUBLIC,
    )
    assert read_env(path)["SMTP_PASSWORD"] == stored

    for name in MANAGED_KEYS:
        monkeypatch.setenv(name, "existing-test-value")
        os.environ.pop(name)
    load_env_file(path)
    assert os.environ["SMTP_PASSWORD"] == stored
    assert smtp_config_from_env().password == password


def test_disabled_delivery_can_save_empty_smtp_but_enable_is_strict(tmp_path):
    path = tmp_path / ".env"
    disabled = _form(
        delivery_enabled=False,
        host="",
        port=465,
        username="",
        password="",
        sender="",
        recipients=[],
        source_filters=[],
    )
    smtp, _ = save_settings(
        path,
        disabled,
        published_main_count=3,
        published_brief_count=2,
        resolver=PUBLIC,
    )
    assert smtp.delivery_enabled is False
    assert smtp.host == "" and smtp.recipients == ()
    assert read_env(path)["EMAIL_DELIVERY_ENABLED"] == "false"

    with pytest.raises(AdminEmailError):
        save_settings(
            path,
            {**disabled, "delivery_enabled": True},
            published_main_count=3,
            published_brief_count=2,
            resolver=PUBLIC,
        )
    # Failed re-enable is atomic: the paused configuration remains in force.
    assert read_env(path)["EMAIL_DELIVERY_ENABLED"] == "false"


def test_save_migrates_legacy_tls_without_leaving_a_conflicting_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SMTP_USE_TLS=true\nKEEP=yes\n", encoding="utf-8")
    save_settings(
        path,
        _form(security="implicit_tls"),
        published_main_count=3,
        published_brief_count=2,
        resolver=PUBLIC,
    )
    values = read_env(path)
    assert values["SMTP_SECURITY"] == "implicit_tls"
    assert "SMTP_USE_TLS" not in values
    assert values["KEEP"] == "yes"


def test_clear_password_requires_separate_confirmation(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SMTP_PASSWORD=secret\nOTHER=value\n", encoding="utf-8")
    with pytest.raises(AdminEmailError, match="confirm"):
        clear_password(path, confirm=False)
    assert read_env(path)["SMTP_PASSWORD"] == "secret"
    clear_password(path, confirm=True)
    values = read_env(path)
    assert values["SMTP_PASSWORD"] == ""
    assert values["OTHER"] == "value"


def test_save_is_atomic_under_concurrency_and_keeps_unmanaged_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("KEEP=yes\n", encoding="utf-8")

    def save(index):
        save_settings(
            path,
            _form(port=2000 + index, password=f"secret-{index}"),
            published_main_count=3,
            published_brief_count=2,
            resolver=PUBLIC,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(8)))
    values = read_env(path)
    assert values["KEEP"] == "yes"
    assert 2000 <= int(values["SMTP_PORT"]) <= 2007
    assert smtp_config_from_env(values).password.startswith("secret-")
    assert path.read_text(encoding="utf-8").count("SMTP_HOST=") == 1


@pytest.mark.parametrize("port", [1, 465, 587, 2525, 65535])
def test_target_accepts_complete_port_range(port):
    host, addresses = validate_smtp_target("SMTP.Example.com", port, PUBLIC)
    assert host == "smtp.example.com"
    assert addresses == ("93.184.216.34",)


@pytest.mark.parametrize("port", [0, 65536])
def test_target_rejects_invalid_port(port):
    with pytest.raises(AdminEmailError):
        validate_smtp_target("smtp.example.com", port, PUBLIC)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "169.254.169.254",
        "[::1]",
        "localhost",
        "smtp.local",
        "smtp.internal",
        "smtp..example.com",
        "-smtp.example.com",
        "smtp_.example.com",
    ],
)
def test_target_rejects_ip_private_and_invalid_domains(host):
    with pytest.raises(AdminEmailError):
        validate_smtp_target(host, 587, PUBLIC)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
    ],
)
def test_target_rejects_any_non_global_dns_result(address):
    with pytest.raises(AdminEmailError, match="全部 DNS"):
        validate_smtp_target(
            "smtp.example.com",
            587,
            lambda host, port: ["93.184.216.34", address],
        )


def test_form_validation_reuses_config_parsers_and_saved_recipient_mode():
    saved = {
        "SMTP_PASSWORD": "saved-password",
        "SMTP_RECIPIENTS": "saved@example.com",
    }
    smtp, content, values = configs_from_form(
        _form(password="", recipients=["attacker@example.com"]),
        saved,
        published_main_count=3,
        published_brief_count=2,
        saved_recipients=True,
    )
    assert smtp.password == "saved-password"
    assert smtp.recipients == ("saved@example.com",)
    assert "attacker@example.com" not in values["SMTP_RECIPIENTS"]
    assert content.main_limit == 2

    with pytest.raises(AdminEmailError, match="both be set"):
        configs_from_form(
            _form(password="", username="operator"),
            {},
            published_main_count=3,
            published_brief_count=2,
        )
    with pytest.raises(AdminEmailError, match="at least one"):
        configs_from_form(
            _form(mains_enabled=False, briefs_enabled=False),
            saved,
            published_main_count=3,
            published_brief_count=2,
        )
    with pytest.raises(AdminEmailError, match="exceeds"):
        configs_from_form(
            _form(main_limit=4),
            saved,
            published_main_count=3,
            published_brief_count=2,
        )
