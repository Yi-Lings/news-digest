import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from news_digest.payments import (
    EpayConfig,
    PaymentCreation,
    PaymentError,
    PaymentQuery,
    config_identity,
    create_payment,
    merchant_order_number,
    money_to_cents,
    normalize_api_base,
    parse_notification,
    payment_origin,
    query_payment,
    sign_fields,
)


def _config() -> EpayConfig:
    return EpayConfig(
        base_url="https://pay.example.test",
        merchant_id="1001",
        merchant_key="merchant-secret",
        payment_type="alipay",
        site_url="https://news.example.test",
        order_ttl_seconds=300,
        amount_hold_seconds=3600,
    )


def test_epay_signing_excludes_empty_and_signature_fields():
    fields = {
        "pid": "1001",
        "money": "9.90",
        "name": "Monthly plan",
        "param": "",
        "sign": "ignored",
        "sign_type": "MD5",
    }
    canonical = "money=9.90&name=Monthly plan&pid=1001merchant-secret"
    assert sign_fields(fields, "merchant-secret") == hashlib.md5(
        canonical.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.01", 1), ("9.9", 990), ("9.90", 990), ("100", 10000)],
)
def test_money_to_cents_is_decimal_exact(value, expected):
    assert money_to_cents(value) == expected


@pytest.mark.parametrize("value", ["", "0", "-1.00", "1.001", "1e2", "NaN"])
def test_money_to_cents_rejects_invalid_values(value):
    with pytest.raises(PaymentError):
        money_to_cents(value)


def test_mapi_payment_creation_posts_signed_standard_fields_without_secret(
    monkeypatch,
):
    config = _config()
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(
                {
                    "code": 1,
                    "trade_no": "FP100",
                    "payurl": "https://pay.example.test/pay/FP100",
                }
            ).encode()

    def fake_open_gateway(request, *, allowed_base_url, timeout):
        requests.append((request, timeout))
        assert allowed_base_url == config.base_url
        return Response()

    monkeypatch.setattr("news_digest.payments._open_gateway", fake_open_gateway)
    creation = create_payment(
        config,
        merchant_order_no="news_20260830120000ABC",
        amount_cents=989,
        subject="News Digest monthly plan",
    )
    assert creation == PaymentCreation(
        provider_trade_no="FP100",
        payment_url="https://pay.example.test/pay/FP100",
    )
    request, timeout = requests[0]
    parsed = urlsplit(request.full_url)
    fields = {
        key: values[0]
        for key, values in parse_qs(request.data.decode("utf-8")).items()
    }
    assert timeout == 10
    assert request.method == "POST"
    assert parsed.path == "/mapi.php"
    assert fields == {
        "pid": "1001",
        "type": "alipay",
        "out_trade_no": "news_20260830120000ABC",
        "notify_url": "https://news.example.test/subscribe/api/payment/easypay",
        "return_url": "https://news.example.test/payment/return",
        "name": "News Digest monthly plan",
        "money": "9.89",
        "sign_type": "MD5",
        "sign": sign_fields(fields, config.merchant_key),
    }
    assert config.merchant_key not in request.data.decode("utf-8")


def test_mapi_amount_collision_has_stable_error_code(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(
                {"code": 0, "msg": "AMOUNT_OCCUPIED", "error_code": "AMOUNT_OCCUPIED"}
            ).encode()

    monkeypatch.setattr(
        "news_digest.payments._open_gateway", lambda *_args, **_kwargs: Response()
    )
    with pytest.raises(PaymentError) as caught:
        create_payment(
            _config(),
            merchant_order_no="news_collision",
            amount_cents=989,
            subject="Monthly plan",
        )
    assert caught.value.code == "AMOUNT_OCCUPIED"


def test_config_identity_is_secret_safe_and_changes_with_settlement_credentials():
    first = _config()
    same = _config()
    changed = EpayConfig(
        base_url=first.base_url,
        merchant_id=first.merchant_id,
        merchant_key="replacement-secret",
        payment_type=first.payment_type,
        site_url=first.site_url,
    )
    assert config_identity(first) == config_identity(same)
    assert config_identity(first) != config_identity(changed)
    assert first.merchant_key not in config_identity(first)
    from dataclasses import replace
    wxpay_config = replace(first, payment_type="wxpay")
    assert config_identity(first) == config_identity(wxpay_config)



@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("HTTPS://PAY.Example.Test:443/gateway", "https://pay.example.test"),
        ("https://pay.example.test:8443/gateway", "https://pay.example.test:8443"),
        ("http://127.0.0.1:8080/gateway", "http://127.0.0.1:8080"),
        ("http://[::1]:8080/gateway", "http://[::1]:8080"),
        ("https://例子.测试/gateway", "https://xn--fsqu00a.xn--0zwm56d"),
        ("https://xn--fsqu00a.xn--0zwm56d/gateway", "https://xn--fsqu00a.xn--0zwm56d"),
    ],
)
def test_payment_origin_canonicalizes_scheme_hostname_and_port(base_url, expected):
    config = EpayConfig(
        base_url=base_url,
        merchant_id="1001",
        merchant_key="merchant-secret",
        payment_type="alipay",
        site_url="https://news.example.test",
    )
    assert payment_origin(config) == expected


@pytest.mark.parametrize(
    "base_url",
    [
        "https://pay.example.test; script-src *",
        "https://pay.example.test /gateway",
        "https://pay.example.test/gateway;param=1",
        "https://bad_host.example/gateway",
        "https://-bad.example/gateway",
        "https://bad-.example/gateway",
        "https://bad..example/gateway",
        "https://999.1.1.1/gateway",
        "https://127.1/gateway",
        "https://[gggg::1]/gateway",
        "https://[fe80::1%25eth0]/gateway",
        "https://pay.example.test:0/gateway",
        "https://pay.example.test:65536/gateway",
    ],
)
def test_epay_config_rejects_csp_unsafe_or_invalid_gateway_hosts(base_url):
    with pytest.raises(PaymentError):
        EpayConfig(
            base_url=base_url,
            merchant_id="1001",
            merchant_key="merchant-secret",
            payment_type="alipay",
            site_url="https://news.example.test",
        )


def test_signed_order_query_is_strict_and_does_not_expose_key(monkeypatch):
    config = _config()
    requests = []
    fields = {
        "code": "1",
        "msg": "success",
        "pid": config.merchant_id,
        "out_trade_no": "news_query",
        "trade_no": "FP-QUERY-1",
        "money": "9.89",
        "status": "1",
        "trade_status": "TRADE_SUCCESS",
        "sign_type": "MD5",
    }
    fields["sign"] = sign_fields(fields, config.merchant_key)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(fields).encode()

    def fake_open_gateway(request, *, allowed_base_url, timeout):
        requests.append((request, timeout))
        assert allowed_base_url == config.base_url
        return Response()

    monkeypatch.setattr("news_digest.payments._open_gateway", fake_open_gateway)
    assert query_payment(config, merchant_order_no="news_query") == PaymentQuery(
        merchant_order_no="news_query",
        provider_trade_no="FP-QUERY-1",
        amount_cents=989,
        trade_status="TRADE_SUCCESS",
    )
    request, timeout = requests[0]
    request_fields = parse_qs(request.data.decode("utf-8"))
    assert timeout == 10 and urlsplit(request.full_url).path == "/api.php"
    assert request_fields == {
        "act": ["order"],
        "pid": [config.merchant_id],
        "key": [config.merchant_key],
        "out_trade_no": ["news_query"],
    }


@pytest.mark.parametrize(
    "override",
    [
        {"pid": "other"},
        {"out_trade_no": "news_other"},
        {"money": "9.88"},
        {"trade_status": "TRADE_FINISHED"},
        {"status": "0"},
        {"sign": "0" * 32},
    ],
)
def test_order_query_rejects_tampering(monkeypatch, override):
    config = _config()
    fields = {
        "code": "1",
        "msg": "success",
        "pid": config.merchant_id,
        "out_trade_no": "news_query",
        "trade_no": "FP-QUERY-1",
        "money": "9.89",
        "status": "1",
        "trade_status": "TRADE_SUCCESS",
        "sign_type": "MD5",
    }
    fields.update(override)
    if "sign" not in override:
        fields["sign"] = sign_fields(fields, config.merchant_key)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(fields).encode()

    monkeypatch.setattr(
        "news_digest.payments._open_gateway", lambda *_args, **_kwargs: Response()
    )
    with pytest.raises(PaymentError):
        query_payment(config, merchant_order_no="news_query", expected_amount_cents=989)


@pytest.mark.parametrize(
    "value",
    [
        "https://pay.example.test/submit.php",
        "https://pay.example.test/mapi.php/",
        "https://pay.example.test/api.php",
    ],
)
def test_easypay_api_base_accepts_endpoint_urls(value):
    assert normalize_api_base(value) == "https://pay.example.test"


@pytest.mark.parametrize(
    "value",
    [
        "https://pay.example.test/line\nbreak",
        "https://pay.example.test/trailing\n",
        "https://pay.example.test/c1\x85control",
        "https://pay.example.test\\backslash",
        "https://user@pay.example.test",
        "https://pay.example.test/#fragment",
    ],
)
def test_easypay_api_base_rejects_ambiguous_url_components(value):
    with pytest.raises(PaymentError):
        EpayConfig(
            base_url=value,
            merchant_id="1001",
            merchant_key="merchant-secret",
            payment_type="alipay",
            site_url="https://news.example.test",
        )


@pytest.mark.parametrize(
    "payment_url",
    [
        "https://evil.example.test/pay/FP100",
        "http://pay.example.test/pay/FP100",
        "https://pay.example.test:444/pay/FP100",
        "https://user@pay.example.test/pay/FP100",
        "https://pay.example.test/pay/FP100#fragment",
        "https://pay.example.test\\@evil.example/pay/FP100",
        "https://pay.example.test/pay/line\nbreak",
        "https://pay.example.test/pay/trailing\n",
        "https://pay.example.test/pay/c1\x85control",
    ],
)
def test_payment_creation_rejects_untrusted_payment_url(monkeypatch, payment_url):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(
                {"code": 1, "trade_no": "FP100", "payurl": payment_url}
            ).encode()

    monkeypatch.setattr(
        "news_digest.payments._open_gateway", lambda *_args, **_kwargs: Response()
    )
    with pytest.raises(PaymentError):
        create_payment(
            _config(),
            merchant_order_no="news_untrusted_url",
            amount_cents=989,
            subject="Monthly plan",
        )


def test_payment_creation_accepts_same_origin_url_with_query(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(
                {
                    "code": 1,
                    "trade_no": "FP100",
                    "payurl": "https://pay.example.test:443/pay/FP100?channel=alipay",
                }
            ).encode()

    monkeypatch.setattr(
        "news_digest.payments._open_gateway", lambda *_args, **_kwargs: Response()
    )
    creation = create_payment(
        _config(),
        merchant_order_no="news_same_origin_query",
        amount_cents=989,
        subject="Monthly plan",
    )
    assert creation.payment_url.endswith("/pay/FP100?channel=alipay")


@pytest.mark.parametrize("operation", ["create", "query"])
def test_gateway_redirect_rejects_cross_origin_for_create_and_query(
    monkeypatch, operation
):
    class RedirectingOpener:
        def __init__(self, handler):
            self.handler = handler

        def open(self, request, timeout):
            return self.handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://evil.example.test/redirected",
            )

    monkeypatch.setattr(
        "news_digest.payments.build_opener",
        lambda handler: RedirectingOpener(handler),
    )
    with pytest.raises(PaymentError, match="redirect"):
        if operation == "create":
            create_payment(
                _config(),
                merchant_order_no="news_cross_origin_redirect",
                amount_cents=989,
                subject="Monthly plan",
            )
        else:
            query_payment(_config(), merchant_order_no="news_cross_origin_redirect")


def test_news_merchant_order_number_uses_adapter_namespace():
    first = merchant_order_number()
    second = merchant_order_number()
    assert first.startswith("news_")
    assert first != second
    assert len(first) <= 80


def _notification(config: EpayConfig, **overrides: str) -> dict[str, str]:
    fields = {
        "pid": config.merchant_id,
        "trade_no": "gateway-10001",
        "out_trade_no": "news_20260830120000ABC",
        "type": config.payment_type,
        "name": "News Digest monthly plan",
        "money": "9.89",
        "trade_status": "TRADE_SUCCESS",
        "sign_type": "MD5",
    }
    fields.update(overrides)
    fields["sign"] = sign_fields(fields, config.merchant_key)
    return fields


def test_notification_validation_accepts_exact_adapter_success():
    config = _config()
    notification = parse_notification(config, _notification(config))
    assert notification.merchant_order_no == "news_20260830120000ABC"
    assert notification.provider_trade_no == "gateway-10001"
    assert notification.amount_cents == 989



@pytest.mark.parametrize("payment_type", ["card", "ALIPAY", "cash"])
def test_config_rejects_payment_types_outside_adapter_contract(payment_type):
    with pytest.raises(PaymentError, match="EPAY_PAYMENT_TYPE"):
        EpayConfig(
            base_url="https://pay.example.test",
            merchant_id="1001",
            merchant_key="merchant-secret",
            payment_type=payment_type,
            site_url="https://news.example.test",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"pid": "other"},
        {"type": "wxpay"},
        {"trade_status": "WAIT_BUYER_PAY"},
        {"trade_status": "TRADE_FINISHED"},
        {"trade_status": "", "status": "1"},
        {"sign": "0" * 32},
    ],
)
def test_notification_validation_rejects_wrong_contract(overrides):
    config = _config()
    supplied_sign = overrides.pop("sign", None)
    fields = _notification(config, **overrides)
    if supplied_sign is not None:
        fields["sign"] = supplied_sign
    with pytest.raises(PaymentError):
        parse_notification(config, fields)
