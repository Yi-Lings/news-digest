from news_digest.admin_email import read_env
from news_digest.site_config import SITE_ENV_KEYS, sync_site_environment


def test_site_environment_projection_contains_only_required_runtime_values(tmp_path):
    source = tmp_path / "config" / ".env"
    source.parent.mkdir()
    source.write_text(
        "NEWS_SITE_URL=https://news.example.com\n"
        "NEWS_TIMEZONE=Asia/Shanghai\n"
        "SMTP_HOST=smtp.example.com\n"
        "SMTP_PORT=465\n"
        "SMTP_USERNAME=mailer\n"
        "SMTP_PASSWORD=encoded-secret\n"
        "SMTP_SECURITY=implicit_tls\n"
        "SMTP_FROM=news@example.com\n"
        "SMTP_RECIPIENTS=private-recipient@example.com\n"
        "EMAIL_DELIVERY_ENABLED=false\n"
        "EPAY_ENABLED=true\n"
        "EPAY_API_BASE=https://pay.example.com/epay\n"
        "EPAY_PID=news\n"
        "EPAY_PKEY=encoded-payment-secret\n"
        "EPAY_PAYMENT_TYPE=alipay\n"
        "EPAY_ORDER_TTL_SECONDS=300\n"
        "EPAY_AMOUNT_HOLD_SECONDS=3600\n"
        "TRANSLATION_API_KEY=provider-secret\n",
        encoding="utf-8",
    )
    target = tmp_path / "site-config" / ".env"

    sync_site_environment(source, target)

    projected = read_env(target)
    assert set(projected) == set(SITE_ENV_KEYS)
    assert projected["SMTP_PASSWORD"] == "encoded-secret"
    assert projected["EPAY_PKEY"] == "encoded-payment-secret"
    assert "SMTP_RECIPIENTS" not in projected
    assert "EMAIL_DELIVERY_ENABLED" not in projected
    assert "TRANSLATION_API_KEY" not in projected
