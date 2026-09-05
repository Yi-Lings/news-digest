"""storage.db 的离线单元测试:建库、round-trip、翻译保护、日期列表与持久化。

测试不直接写 SQL(SQL 仅允许出现在 storage.db 内),schema_version 的正确性
通过行为证明:版本一致时重复 connect 成功,篡改期望版本后 connect 必须报错。
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from news_digest.models import (
    Article,
    ArticleImage,
    BriefItem,
    Collocation,
    Paragraph,
    SentenceNote,
    VocabularyItem,
)
from news_digest.storage import db


def _article(url: str, **overrides) -> Article:
    """构造必填字段齐全的测试文章,任意字段可用关键字覆盖。"""
    fields = {
        "slug": "test-article",
        "source": "Example Wire",
        "title_en": "Example Title",
        "summary_en": "Example summary.",
        "author": "Ada Writer",
        "published_at": "2026-07-26T08:00:00+00:00",
        "url": url,
        "reading_minutes": 4,
        "paragraphs": [Paragraph(en="First paragraph.", zh="第一段。")],
    }
    fields.update(overrides)
    return Article(**fields)


def test_connect_creates_db_idempotently_and_records_schema_version(tmp_path, monkeypatch):
    db_path = tmp_path / "state" / "digest.db"
    first = db.connect(db_path)  # 空目录:父目录与库文件自动创建
    first.close()
    assert db_path.exists()

    second = db.connect(db_path)  # 重复 connect 幂等,库可正常使用
    assert db.list_dates(second) == []
    second.close()

    # 库中已写入 schema_version 且等于 SCHEMA_VERSION:
    # 上面版本一致的 connect 成功,而换一个期望版本后必须因不匹配报错。
    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError, match="schema 版本不匹配"):
        db.connect(db_path)


def test_abandon_email_code_invalidates_only_unconsumed_code(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    db.issue_email_code(
        conn,
        email_key="a" * 64,
        purpose="register",
        code_digest="b" * 64,
        ttl_seconds=600,
        now="2026-08-30T12:00:00+00:00",
    )
    assert db.abandon_email_code(conn, email_key="a" * 64, purpose="register") == 1
    assert db.consume_email_code(
        conn,
        email_key="a" * 64,
        purpose="register",
        code_digest="b" * 64,
        now="2026-08-30T12:00:01+00:00",
    ) is False
    conn.close()


def test_email_code_at_attempt_limit_cannot_be_consumed_and_is_deleted(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    email_key = "a" * 64
    correct_digest = "b" * 64
    db.issue_email_code(
        conn,
        email_key=email_key,
        purpose="register",
        code_digest=correct_digest,
        ttl_seconds=600,
        now="2026-08-30T12:00:00+00:00",
    )
    for second in range(1, 6):
        assert db.consume_email_code(
            conn,
            email_key=email_key,
            purpose="register",
            code_digest="c" * 64,
            max_attempts=5,
            now=f"2026-08-30T12:00:0{second}+00:00",
        ) is False
    assert db.consume_email_code(
        conn,
        email_key=email_key,
        purpose="register",
        code_digest=correct_digest,
        max_attempts=5,
        now="2026-08-30T12:00:06+00:00",
    ) is False
    assert db.abandon_email_code(conn, email_key=email_key, purpose="register") == 0
    conn.close()


def test_email_code_and_mail_outbox_are_issued_in_one_transaction(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    email = "atomic-mail@example.com"
    user = db.upsert_pending_user(
        conn,
        email=email,
        email_key=db.delivery_recipient_key(email),
        password_hash="password-hash",
        now="2026-08-30T12:00:00+00:00",
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    db.issue_email_code_with_outbox(
        conn,
        email_key=user.email_key,
        purpose="register",
        code_digest="b" * 64,
        delivery_token="delivery-token-with-enough-entropy",
        ttl_seconds=600,
        now="2026-08-30T12:00:01+00:00",
    )
    conn.set_trace_callback(None)
    claimed = db.claim_account_mail(
        conn,
        now="2026-08-30T12:00:02+00:00",
    )
    assert claimed is not None
    assert claimed.email == email
    assert claimed.email_key == user.email_key
    assert claimed.delivery_token == "delivery-token-with-enough-entropy"
    traced = [statement.upper() for statement in statements]
    begin = next(index for index, statement in enumerate(traced) if statement.startswith("BEGIN"))
    code_insert = next(
        index
        for index, statement in enumerate(traced)
        if statement.startswith("INSERT INTO EMAIL_CODES")
    )
    outbox_insert = next(
        index
        for index, statement in enumerate(traced)
        if statement.startswith("INSERT INTO ACCOUNT_MAIL_OUTBOX")
    )
    commit = next(index for index, statement in enumerate(traced) if statement == "COMMIT")
    assert begin < code_insert < outbox_insert < commit
    conn.close()


def test_account_mail_failure_retries_and_stale_claim_recovers_after_restart(tmp_path):
    path = tmp_path / "digest.db"
    conn = db.connect(path)
    email = "restart-mail@example.com"
    user = db.upsert_pending_user(
        conn,
        email=email,
        email_key=db.delivery_recipient_key(email),
        password_hash="password-hash",
        now="2026-08-30T12:00:00+00:00",
    )
    db.issue_email_code_with_outbox(
        conn,
        email_key=user.email_key,
        purpose="reset",
        code_digest="c" * 64,
        delivery_token="restart-token-with-enough-entropy",
        ttl_seconds=600,
        now="2026-08-30T12:00:01+00:00",
    )
    first = db.claim_account_mail(conn, now="2026-08-30T12:00:02+00:00")
    assert first is not None and first.attempts == 1
    assert db.release_account_mail(
        conn,
        outbox_id=first.id,
        now="2026-08-30T12:00:03+00:00",
        retry_seconds=5,
        max_attempts=3,
        error_code="DELIVERY_FAILED",
    )
    assert db.claim_account_mail(conn, now="2026-08-30T12:00:07+00:00") is None
    second = db.claim_account_mail(conn, now="2026-08-30T12:00:08+00:00")
    assert second is not None and second.id == first.id and second.attempts == 2
    conn.close()  # 模拟 worker 在 sending 租约内退出。

    restarted = db.connect(path)
    recovered = db.claim_account_mail(
        restarted,
        now="2026-08-30T12:01:09+00:00",
        lease_seconds=60,
    )
    assert recovered is not None and recovered.id == first.id
    assert recovered.attempts == 3
    db.complete_account_mail(
        restarted,
        outbox_id=recovered.id,
        now="2026-08-30T12:01:10+00:00",
    )
    assert db.claim_account_mail(restarted, now="2026-08-30T12:01:11+00:00") is None
    restarted.close()


def test_password_update_and_session_revocation_share_one_transaction(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    email = "reset-atomic@example.com"
    user = db.upsert_pending_user(
        conn,
        email=email,
        email_key=db.delivery_recipient_key(email),
        password_hash="old-password-hash",
        now="2026-08-30T12:00:00+00:00",
    )
    user = db.activate_user(
        conn,
        email_key=user.email_key,
        now="2026-08-30T12:00:01+00:00",
    )
    db.create_user_session(
        conn,
        token_digest="d" * 64,
        user_id=user.id,
        expires_at="2026-09-30T12:00:00+00:00",
        now="2026-08-30T12:00:02+00:00",
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    updated = db.set_user_password_and_revoke_sessions(
        conn,
        user.id,
        password_hash="new-password-hash",
        now="2026-08-30T12:00:03+00:00",
    )
    conn.set_trace_callback(None)
    assert updated.password_hash == "new-password-hash"
    assert db.user_session_owner(
        conn,
        token_digest="d" * 64,
        now="2026-08-30T12:00:04+00:00",
    ) is None
    traced = [statement.upper() for statement in statements]
    begin = next(index for index, statement in enumerate(traced) if statement.startswith("BEGIN"))
    password_update = next(
        index
        for index, statement in enumerate(traced)
        if statement.startswith("UPDATE USERS SET PASSWORD_HASH")
    )
    session_update = next(
        index
        for index, statement in enumerate(traced)
        if statement.startswith("UPDATE USER_SESSIONS SET REVOKED_AT")
    )
    commit = next(index for index, statement in enumerate(traced) if statement == "COMMIT")
    assert begin < password_update < session_update < commit
    conn.close()


def test_user_listing_has_stable_server_pagination_and_literal_search(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    created = []
    for index in range(5):
        email = f"reader-{index}@example.com"
        created.append(
            db.upsert_pending_user(
                conn,
                email=email,
                email_key=db.delivery_recipient_key(email),
                password_hash="password-hash",
                now="2026-08-30T12:00:00+00:00",
            )
        )
    wildcard_email = "literal_%@example.com"
    wildcard_user = db.upsert_pending_user(
        conn,
        email=wildcard_email,
        email_key=db.delivery_recipient_key(wildcard_email),
        password_hash="password-hash",
        now="2026-08-30T12:00:00+00:00",
    )

    first_page = db.list_users(conn, limit=3, offset=0)
    second_page = db.list_users(conn, limit=3, offset=3)
    assert [user.id for user in first_page + second_page] == [
        wildcard_user.id,
        *[user.id for user in reversed(created)],
    ]
    assert db.count_users(conn) == 6
    assert db.count_users(conn, query="READER-0") == 1
    assert [user.email for user in db.list_users(conn, query="READER-0")] == [
        "reader-0@example.com"
    ]
    assert db.count_users(conn, query="_%") == 1
    assert [user.email for user in db.list_users(conn, query="_%")] == [wildcard_email]
    conn.close()


def test_upsert_and_get_edition_round_trip(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    early = _article("https://example.com/early", published_at="2026-07-26T06:00:00+00:00")
    late = _article(
        "https://example.com/late",
        published_at="2026-07-26T09:30:00+00:00",
        title_zh="晚间标题",
        summary_zh="中文摘要。",
        translated_by="m@p1",
        paragraphs=[Paragraph(en="Body text.", zh="正文。")],
        vocabulary=[VocabularyItem("resilient", "/rɪˈzɪliənt/", "有韧性的", "A resilient grid.")],
        collocations=[Collocation("carry out", "执行", "They carry out repairs.")],
        sentence_notes=[SentenceNote("It holds.", "它撑得住。", "主谓结构,一般现在时。")],
        image=ArticleImage("https://example.com/a.jpg", "A power grid", "Example/Getty"),
        content_status="full",
    )
    briefs = [
        BriefItem(title_en="Zulu", source="Wire", url="https://example.com/z"),
        BriefItem(title_en="Alpha", source="Wire", url="https://example.com/a", title_zh="甲"),
    ]
    db.upsert_articles(conn, "2026-07-26", [early, late])
    db.upsert_briefs(conn, "2026-07-26", briefs)

    edition = db.get_edition(conn, "2026-07-26")
    assert edition is not None
    assert edition.date == "2026-07-26"
    # 文章按 published_at 降序;dataclass 相等性覆盖全部嵌套字段的完整还原
    assert edition.articles == [late, early]
    # 快讯按 url 升序
    assert edition.briefs == [briefs[1], briefs[0]]
    assert db.get_edition(conn, "2000-01-01") is None
    conn.close()


def test_untranslated_refetch_does_not_overwrite_translated_row(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    url = "https://example.com/protected"
    translated = _article(
        url, title_zh="已翻译标题", summary_zh="已翻译摘要。", translated_by="m@p2"
    )
    db.upsert_articles(conn, "2026-07-26", [translated])

    refetched = _article(url, title_zh="", translated_by="")  # 重新抓取的未翻译版本
    db.upsert_articles(conn, "2026-07-26", [refetched])

    edition = db.get_edition(conn, "2026-07-26")
    assert edition is not None
    assert edition.articles == [translated]  # 翻译成果原样保留
    conn.close()


def test_translated_version_overwrites_untranslated_row(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    url = "https://example.com/upgraded"
    db.upsert_articles(conn, "2026-07-26", [_article(url)])  # 未翻译旧行

    translated = _article(url, title_zh="新翻译标题", translated_by="m@p2")
    db.upsert_articles(conn, "2026-07-26", [translated])

    edition = db.get_edition(conn, "2026-07-26")
    assert edition is not None
    assert edition.articles == [translated]
    conn.close()


def test_list_dates_unions_both_tables_descending(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    db.upsert_articles(conn, "2026-07-25", [_article("https://example.com/one")])
    db.upsert_articles(conn, "2026-07-26", [_article("https://example.com/two")])
    db.upsert_briefs(
        conn, "2026-07-26", [BriefItem(title_en="C", source="Wire", url="https://example.com/c")]
    )
    # 2026-07-27 仅有快讯,也必须出现在日期列表中
    db.upsert_briefs(
        conn, "2026-07-27", [BriefItem(title_en="B", source="Wire", url="https://example.com/b")]
    )
    assert db.list_dates(conn) == ["2026-07-27", "2026-07-26", "2026-07-25"]
    conn.close()


def test_data_survives_reconnect(tmp_path):
    db_path = tmp_path / "digest.db"
    conn = db.connect(db_path)
    article = _article("https://example.com/persist", title_zh="持久标题", translated_by="m@p1")
    db.upsert_articles(conn, "2026-07-26", [article])
    db.upsert_briefs(
        conn, "2026-07-26", [BriefItem(title_en="B", source="Wire", url="https://example.com/b")]
    )
    conn.close()

    reopened = db.connect(db_path)
    edition = db.get_edition(reopened, "2026-07-26")
    assert edition is not None
    assert edition.articles == [article]
    assert [brief.url for brief in edition.briefs] == ["https://example.com/b"]
    reopened.close()


def _active_payment_user(conn, email: str, now: str) -> db.User:
    key = db.delivery_recipient_key(email)
    db.upsert_pending_user(
        conn,
        email=email,
        email_key=key,
        password_hash="hash",
        now=now,
    )
    return db.activate_user(conn, email_key=key, now=now)


def test_payment_orders_allocate_nearest_unique_amount_slots(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    now = "2026-08-30T12:00:00+00:00"
    offsets = []
    for index in range(21):
        user = _active_payment_user(conn, f"buyer-{index}@example.com", now)
        order = db.create_payment_order(
            conn,
            user_id=user.id,
            plan="monthly",
            base_amount_cents=990,
            merchant_order_no=f"ND{index:02d}",
            now=now,
            ttl_seconds=300,
            amount_hold_seconds=3600,
        )
        offsets.append(order.amount_offset_cents)
        assert order.amount_cents == 990 + order.amount_offset_cents
    assert offsets == [0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7, -8, 8, -9, 9, -10, 10]
    with pytest.raises(RuntimeError, match="payment amount slots exhausted"):
        user = _active_payment_user(conn, "buyer-full@example.com", now)
        db.create_payment_order(
            conn,
            user_id=user.id,
            plan="monthly",
            base_amount_cents=990,
            merchant_order_no="ND22",
            now=now,
            ttl_seconds=300,
            amount_hold_seconds=3600,
        )
    conn.close()


def test_expired_payment_amount_remains_held_before_reuse(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    first_user = _active_payment_user(
        conn, "buyer-first@example.com", "2026-08-30T12:00:00+00:00"
    )
    first = db.create_payment_order(
        conn,
        user_id=first_user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="ND-FIRST",
        now="2026-08-30T12:00:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    held_user = _active_payment_user(
        conn, "buyer-held@example.com", "2026-08-30T12:06:00+00:00"
    )
    held = db.create_payment_order(
        conn,
        user_id=held_user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="ND-HELD",
        now="2026-08-30T12:06:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    reused_user = _active_payment_user(
        conn, "buyer-reused@example.com", "2026-08-30T13:06:00+00:00"
    )
    reusable = db.create_payment_order(
        conn,
        user_id=reused_user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="ND-REUSED",
        now="2026-08-30T13:06:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    assert first.amount_offset_cents == 0
    assert held.amount_offset_cents == -1
    assert reusable.amount_offset_cents == 0
    assert db.order_by_id(conn, first.id).status == "expired"
    conn.close()


def test_failed_uncertain_payment_amount_remains_held(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    first_user = _active_payment_user(
        conn, "buyer-failed@example.com", "2026-08-30T12:00:00+00:00"
    )
    first = db.create_payment_order(
        conn,
        user_id=first_user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="ND-FAILED",
        now="2026-08-30T12:00:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    with conn:
        conn.execute(
            "UPDATE orders SET status = 'failed', "
            "last_error_code = 'PAYMENT_WAITING_NO_URL' WHERE id = ?",
            (first.id,),
        )

    second_user = _active_payment_user(
        conn, "buyer-after-failed@example.com", "2026-08-30T12:06:00+00:00"
    )
    second = db.create_payment_order(
        conn,
        user_id=second_user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="ND-AFTER-FAILED",
        now="2026-08-30T12:06:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )

    assert first.amount_offset_cents == 0
    assert second.amount_offset_cents == -1
    conn.close()


def test_payment_confirmation_is_atomic_and_idempotent(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    user = _active_payment_user(conn, "buyer@example.com", "2026-08-30T12:00:00+00:00")
    order = db.create_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="ND-PAID",
        now="2026-08-30T12:00:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    paid = db.confirm_payment_order(
        conn,
        merchant_order_no=order.merchant_order_no,
        provider_trade_no="TRADE-1",
        amount_cents=order.amount_cents,
        now="2026-08-30T12:01:00+00:00",
        amount_hold_seconds=3600,
        plan_days={"monthly": 31, "yearly": 366},
    )
    first_until = db.user_by_id(conn, user.id).paid_until
    repeated = db.confirm_payment_order(
        conn,
        merchant_order_no=order.merchant_order_no,
        provider_trade_no="TRADE-1",
        amount_cents=order.amount_cents,
        now="2026-08-30T12:02:00+00:00",
        amount_hold_seconds=3600,
        plan_days={"monthly": 31, "yearly": 366},
    )
    assert paid.status == repeated.status == "paid"
    assert db.user_by_id(conn, user.id).paid_until == first_until
    conn.close()


def test_payment_confirmation_matches_gateway_trade_number_from_creation(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    user = _active_payment_user(conn, "bound@example.com", "2026-08-30T12:00:00+00:00")
    order = db.create_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_bound",
        now="2026-08-30T12:00:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    db.record_payment_order_created(
        conn,
        order_id=order.id,
        provider_trade_no="TRADE-EXPECTED",
        creation_generation=order.updated_at,
        now="2026-08-30T12:00:01+00:00",
    )
    with pytest.raises(RuntimeError, match="trade number does not match"):
        db.confirm_payment_order(
            conn,
            merchant_order_no=order.merchant_order_no,
            provider_trade_no="TRADE-OTHER",
            amount_cents=order.amount_cents,
            now="2026-08-30T12:01:00+00:00",
            amount_hold_seconds=3600,
            plan_days={"monthly": 31, "yearly": 366},
        )
    assert db.order_by_id(conn, order.id).status == "pending"
    conn.close()


def test_provider_trade_number_cannot_pay_two_orders(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    users = [
        _active_payment_user(
            conn, f"buyer-{index}@example.com", "2026-08-30T12:00:00+00:00"
        )
        for index in range(2)
    ]
    orders = [
        db.create_payment_order(
            conn,
            user_id=users[index].id,
            plan="monthly",
            base_amount_cents=990,
            merchant_order_no=f"ND-{index}",
            now="2026-08-30T12:00:00+00:00",
            ttl_seconds=300,
            amount_hold_seconds=3600,
        )
        for index in range(2)
    ]
    db.confirm_payment_order(
        conn,
        merchant_order_no=orders[0].merchant_order_no,
        provider_trade_no="TRADE-1",
        amount_cents=orders[0].amount_cents,
        now="2026-08-30T12:01:00+00:00",
        amount_hold_seconds=3600,
        plan_days={"monthly": 31, "yearly": 366},
    )
    with pytest.raises(RuntimeError, match="provider trade number already used"):
        db.confirm_payment_order(
            conn,
            merchant_order_no=orders[1].merchant_order_no,
            provider_trade_no="TRADE-1",
            amount_cents=orders[1].amount_cents,
            now="2026-08-30T12:02:00+00:00",
            amount_hold_seconds=3600,
            plan_days={"monthly": 31, "yearly": 366},
        )
    conn.close()


def test_payment_order_reservation_reuses_one_active_order_per_user(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    now = "2026-08-30T12:00:00+00:00"
    user = _active_payment_user(conn, "single@example.com", now)
    first, first_is_new = db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_first",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now=now,
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    second, second_is_new = db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="yearly",
        base_amount_cents=9990,
        merchant_order_no="news_second",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now="2026-08-30T12:01:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    assert first_is_new is True
    assert second_is_new is False
    assert second.id == first.id
    assert second.plan == "monthly"
    assert second.merchant_order_no == first.merchant_order_no
    assert second.base_amount_cents == second.amount_cents == 990
    assert second.settlement_expires_at == "2026-08-30T13:00:00+00:00"
    assert second.payment_type == "alipay"
    assert second.payment_config_id == "a" * 64
    assert len(db.list_user_orders(conn, user_id=user.id)) == 1
    conn.close()


def test_expired_gateway_create_failure_can_be_claimed_for_retry(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    now = "2026-08-30T12:00:00+00:00"
    user = _active_payment_user(conn, "expired-retry@example.com", now)
    order, _ = db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_expired_retry",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now=now,
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    db.record_payment_order_create_error(
        conn,
        order_id=order.id,
        creation_generation=order.updated_at,
        now="2026-08-30T12:00:01+00:00",
    )
    db.expire_payment_orders(conn, now="2026-08-30T12:05:01+00:00")

    claimed = db.claim_payment_order_creation(
        conn,
        order_id=order.id,
        now="2026-08-30T12:05:02+00:00",
        checkout_ttl_seconds=300,
    )

    assert claimed is not None
    assert claimed.status == "pending"
    assert claimed.merchant_order_no == order.merchant_order_no
    assert claimed.amount_cents == order.amount_cents
    conn.close()


def test_retry_checkout_deadline_is_clamped_to_settlement_deadline(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    now = "2026-08-30T12:00:00+00:00"
    user = _active_payment_user(conn, "deadline-retry@example.com", now)
    order, _ = db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_deadline_retry",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now=now,
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    with conn:
        conn.execute(
            "UPDATE orders SET status = 'failed', "
            "last_error_code = 'PAYMENT_WAITING_NO_URL' WHERE id = ?",
            (order.id,),
        )

    claimed = db.claim_payment_order_creation(
        conn,
        order_id=order.id,
        now="2026-08-30T12:59:55+00:00",
        checkout_ttl_seconds=300,
    )

    assert claimed is not None
    assert claimed.expires_at == claimed.settlement_expires_at
    assert claimed.expires_at == "2026-08-30T13:00:00+00:00"
    conn.close()


def test_concurrent_failed_order_retry_claims_once(tmp_path):
    db_path = tmp_path / "digest.db"
    conn = db.connect(db_path)
    now = "2026-08-30T12:00:00+00:00"
    user = _active_payment_user(conn, "concurrent-retry@example.com", now)
    order, _ = db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_concurrent_retry",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now=now,
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    with conn:
        conn.execute(
            "UPDATE orders SET status = 'failed', "
            "last_error_code = 'GATEWAY_CREATE_FAILED' WHERE id = ?",
            (order.id,),
        )
    conn.close()

    def claim():
        worker = db.connect(db_path)
        try:
            return db.claim_payment_order_creation(
                worker,
                order_id=order.id,
                now="2026-08-30T12:01:00+00:00",
                checkout_ttl_seconds=300,
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: claim(), range(2)))

    assert sum(result is not None for result in results) == 1


def test_stale_payment_creation_holder_cannot_overwrite_new_claim(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    first = _active_payment_user(
        conn, "stale-creation@example.com", "2026-08-30T12:00:00+00:00"
    )
    original, _ = db.reserve_payment_order(
        conn,
        user_id=first.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_stale_creation",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now="2026-08-30T12:00:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    current = db.claim_payment_order_creation(
        conn,
        order_id=original.id,
        now="2026-08-30T12:00:31+00:00",
    )
    assert current is not None and current.updated_at != original.updated_at

    with pytest.raises(RuntimeError, match="creation claim is stale"):
        db.record_payment_order_create_error(
            conn,
            order_id=original.id,
            creation_generation=original.updated_at,
            now="2026-08-30T12:00:32+00:00",
        )
    with pytest.raises(RuntimeError, match="cannot be changed"):
        db.reallocate_payment_order_amount(
            conn,
            order_id=original.id,
            rejected_amounts={990},
            creation_generation=original.updated_at,
            now="2026-08-30T12:00:33+00:00",
        )

    recorded = db.record_payment_order_created(
        conn,
        order_id=current.id,
        provider_trade_no="TRADE-CURRENT",
        payment_url="https://pay.example.test/pay/current",
        creation_generation=current.updated_at,
        now="2026-08-30T12:00:34+00:00",
    )
    with pytest.raises(RuntimeError, match="creation claim is stale"):
        db.record_payment_order_created(
            conn,
            order_id=original.id,
            provider_trade_no="TRADE-STALE",
            payment_url="https://pay.example.test/pay/stale",
            creation_generation=original.updated_at,
            now="2026-08-30T12:00:35+00:00",
        )
    assert recorded.provider_trade_no == "TRADE-CURRENT"
    assert db.order_by_id(conn, original.id).payment_url.endswith("/current")
    conn.close()


def test_stale_payment_query_cannot_fence_new_creation_claim(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    user = _active_payment_user(
        conn, "stale-query@example.com", "2026-08-30T12:00:00+00:00"
    )
    queried, _ = db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_stale_query",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now="2026-08-30T12:00:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    current = db.claim_payment_order_creation(
        conn,
        order_id=queried.id,
        now="2026-08-30T12:00:31+00:00",
    )
    assert current is not None

    with pytest.raises(RuntimeError, match="reconciliation is stale"):
        db.record_payment_query_status(
            conn,
            order_id=queried.id,
            trade_status="WAIT_BUYER_PAY",
            expected_updated_at=queried.updated_at,
            now="2026-08-30T12:00:32+00:00",
        )
    created = db.record_payment_order_created(
        conn,
        order_id=current.id,
        provider_trade_no="TRADE-CURRENT-QUERY",
        payment_url="https://pay.example.test/pay/current-query",
        creation_generation=current.updated_at,
        now="2026-08-30T12:00:33+00:00",
    )
    assert created.provider_trade_no == "TRADE-CURRENT-QUERY"
    assert created.payment_url.endswith("/current-query")
    conn.close()


def test_concurrent_payment_reservations_create_one_order(tmp_path):
    db_path = tmp_path / "digest.db"
    conn = db.connect(db_path)
    now = "2026-08-30T12:00:00+00:00"
    user = _active_payment_user(conn, "concurrent-order@example.com", now)
    conn.close()

    def reserve(index: int):
        worker = db.connect(db_path)
        try:
            return db.reserve_payment_order(
                worker,
                user_id=user.id,
                plan="monthly",
                base_amount_cents=990,
                merchant_order_no=f"news_concurrent_{index}",
                payment_type="alipay",
                payment_config_id="a" * 64,
                now=now,
                ttl_seconds=300,
                amount_hold_seconds=3600,
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sorted(created for _order, created in results) == [False, True]
    assert len({order.id for order, _created in results}) == 1
    conn = db.connect(db_path)
    assert len(db.list_user_orders(conn, user_id=user.id)) == 1
    conn.close()


def test_rejected_gateway_amount_moves_order_to_next_slot(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    now = "2026-08-30T12:00:00+00:00"
    user = _active_payment_user(conn, "collision@example.com", now)
    order, _ = db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_collision",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now=now,
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    moved = db.reallocate_payment_order_amount(
        conn,
        order_id=order.id,
        rejected_amounts={990},
        creation_generation=order.updated_at,
        now="2026-08-30T12:00:01+00:00",
    )
    assert moved.amount_cents == 989
    assert moved.amount_offset_cents == -1
    conn.close()


def test_closed_gateway_order_releases_amount_and_configuration_lock(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    now = "2026-08-30T12:00:00+00:00"
    first_user = _active_payment_user(conn, "closed@example.com", now)
    first, _ = db.reserve_payment_order(
        conn,
        user_id=first_user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_closed",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now=now,
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    closed = db.record_payment_query_status(
        conn,
        order_id=first.id,
        trade_status="TRADE_CLOSED",
        expected_updated_at=first.updated_at,
        now="2026-08-30T12:01:00+00:00",
    )
    assert closed.status == "expired"
    assert closed.settlement_expires_at == "2026-08-30T12:01:00+00:00"
    assert db.has_unsettled_payment_orders(
        conn, now="2026-08-30T12:01:00+00:00"
    ) is False
    second_user = _active_payment_user(
        conn, "after-closed@example.com", "2026-08-30T12:01:00+00:00"
    )
    second, _ = db.reserve_payment_order(
        conn,
        user_id=second_user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_after_closed",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now="2026-08-30T12:01:00+00:00",
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    assert second.amount_offset_cents == 0
    conn.close()


def test_payment_config_update_lock_prevents_old_config_order_race(tmp_path):
    db_path = tmp_path / "digest.db"
    conn = db.connect(db_path)
    initial_now = "2026-08-30T12:00:00+00:00"
    first_user = _active_payment_user(conn, "old-config@example.com", initial_now)
    first, _ = db.reserve_payment_order(
        conn,
        user_id=first_user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_old_config",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now=initial_now,
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    db.record_payment_query_status(
        conn,
        order_id=first.id,
        trade_status="TRADE_CLOSED",
        expected_updated_at=first.updated_at,
        now="2026-08-30T12:01:00+00:00",
    )
    second_user = _active_payment_user(
        conn, "racing-config@example.com", "2026-08-30T12:01:00+00:00"
    )
    assert db.begin_payment_config_update(
        conn, now="2026-08-30T12:01:00+00:00"
    ) is False
    db.set_active_payment_config_id(
        conn,
        payment_config_id="b" * 64,
        now="2026-08-30T12:01:00+00:00",
    )

    def reserve_with_stale_config():
        worker = db.connect(db_path)
        try:
            return db.reserve_payment_order(
                worker,
                user_id=second_user.id,
                plan="monthly",
                base_amount_cents=990,
                merchant_order_no="news_racing_config",
                payment_type="alipay",
                payment_config_id="a" * 64,
                now="2026-08-30T12:01:00+00:00",
                ttl_seconds=300,
                amount_hold_seconds=3600,
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reserve_with_stale_config)
        time.sleep(0.05)
        assert future.done() is False
        conn.commit()
        with pytest.raises(RuntimeError, match="configuration changed"):
            future.result(timeout=5)
    conn.close()


def test_absolute_settlement_deadline_does_not_follow_runtime_hold_setting(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    now = "2026-08-30T12:00:00+00:00"
    user = _active_payment_user(conn, "deadline@example.com", now)
    order, _ = db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_deadline",
        payment_type="alipay",
        payment_config_id="a" * 64,
        now=now,
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    received = db.confirm_payment_order(
        conn,
        merchant_order_no=order.merchant_order_no,
        provider_trade_no="TRADE-LATE",
        amount_cents=order.amount_cents,
        now="2026-08-30T13:00:01+00:00",
        amount_hold_seconds=86400,
        plan_days={"monthly": 31, "yearly": 366},
    )
    assert received.last_error_code == "PAYMENT_REVIEW"
    assert db.user_by_id(conn, user.id).paid_until is None
    assert db.list_payment_cases(conn)[0]["state"] == "received"
    conn.close()
