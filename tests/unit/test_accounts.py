from news_digest import accounts


def test_account_email_is_ascii_and_matches_delivery_identity():
    normalized = accounts.normalize_email(" Reader@Example.com ")

    assert normalized == "reader@example.com"
    assert accounts.email_key(normalized) == accounts.email_key("READER@EXAMPLE.COM")

    try:
        accounts.normalize_email("straße@example.com")
    except accounts.AccountError as error:
        assert str(error) == "邮箱格式不正确"
    else:
        raise AssertionError("SMTPUTF8 account addresses must be rejected")


def test_format_cents_preserves_every_cent_for_large_prices():
    assert accounts.format_cents(0) == "0"
    assert accounts.format_cents(990) == "9.9"
    assert accounts.format_cents(1_000_001) == "10000.01"
    assert accounts.format_cents(1_234_567) == "12345.67"
    assert accounts.format_cents(9_999_999) == "99999.99"


def test_sale_price_and_discount_use_integer_arithmetic():
    settings = {
        "monthly_list_price_cents": "3600",
        "monthly_price_cents": "990",
        "monthly_discount_percent": "0",
    }
    assert accounts.price_cents(settings, "monthly") == 990
    assert accounts.discount_basis_points(settings, "monthly") == 7250
    assert accounts.discount_label(settings, "monthly") == "72.5"

    repeating = {
        "monthly_list_price_cents": "3000",
        "monthly_price_cents": "1000",
    }
    assert accounts.discount_basis_points(repeating, "monthly") == 6666
    assert accounts.discount_label(repeating, "monthly") == "66.66"


def test_legacy_price_and_discount_remain_compatible_without_list_price():
    legacy = {
        "monthly_price_cents": "999",
        "monthly_discount_percent": "20",
    }
    assert accounts.base_price_cents(legacy, "monthly") == 999
    assert accounts.price_cents(legacy, "monthly") == 799
