"""Subscription domain tests: all token, database, and message operations are offline."""

import datetime as dt
import sqlite3
import threading
from pathlib import Path

import pytest

from news_digest.config import SmtpConfig
from news_digest.delivery import subscriptions
from news_digest.delivery.mailer import (
    compose,
    confirmation_message,
    deliver_recipient,
    inject_unsubscribe,
    send_confirmation,
    unsubscribe_headers,
)
from news_digest.storage import db

NOW = dt.datetime(2026, 7, 27, 0, 0, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(minutes=1)
BASE_URL = "https://news.cheapcoding.top"


def _connect(tmp_path: Path):
    return db.connect(tmp_path / "news.db")


def _submit(conn, email="Reader@Example.com", token="c" * 43, now=NOW):
    return subscriptions.submit_subscription(
        conn,
        email,
        BASE_URL,
        now,
        token_factory=lambda: token,
    )


def _activate(conn, email="Reader@Example.com", token="c" * 43):
    submission = _submit(conn, email, token)
    assert submission.should_send_confirmation
    result = subscriptions.confirm_subscription(conn, token, LATER)
    assert result.accepted
    return submission


def test_public_submission_is_pending_and_stores_only_token_digest(tmp_path):
    conn = _connect(tmp_path)
    token = "high-entropy-random-confirmation-token-0123456789"
    submission = _submit(conn, token=token)

    state = db.subscription_by_email(conn, "reader@example.com")
    assert state is not None
    assert state.status == "pending"
    assert state.source == "public"
    assert submission.public_message == _submit(
        conn, email="Reader@Example.com", token="different-token-value-that-is-long-enough"
    ).public_message
    assert submission.confirmation_url == f"{BASE_URL}/subscribe/confirm/{token}"

    raw = sqlite3.connect(tmp_path / "news.db")
    stored = raw.execute(
        "SELECT token_digest, purpose FROM subscription_tokens"
    ).fetchall()
    assert stored == [(subscriptions.token_digest(token), "confirm")]
    assert token not in (tmp_path / "news.db").read_bytes().decode("utf-8", errors="ignore")
    assert "Reader@Example.com" not in token
    raw.close()
    conn.close()


def test_confirmation_is_single_use_and_separate_from_delivery(tmp_path):
    conn = _connect(tmp_path)
    token = "confirm-token-with-at-least-thirty-two-bytes-001"
    _submit(conn, token=token)

    first = subscriptions.confirm_subscription(conn, token, LATER)
    second = subscriptions.confirm_subscription(conn, token, LATER + dt.timedelta(seconds=1))
    assert first.accepted and not second.accepted
    assert first.public_message == second.public_message
    state = db.subscription_by_email(conn, "reader@example.com")
    assert state is not None and state.status == "active"
    assert db.delivery_summary(conn, "2026-07-27").sent == 0
    assert conn.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
    conn.close()


def test_confirmation_expiry_tampering_and_purpose_separation_fail_safely(tmp_path):
    conn = _connect(tmp_path)
    token = "confirm-token-with-at-least-thirty-two-bytes-002"
    subscriptions.submit_subscription(
        conn,
        "reader@example.com",
        BASE_URL,
        NOW,
        lifetime=dt.timedelta(seconds=1),
        token_factory=lambda: token,
    )

    tampered = subscriptions.confirm_subscription(conn, token + "x", LATER)
    expired = subscriptions.confirm_subscription(conn, token, LATER)
    assert not tampered.accepted and not expired.accepted
    assert tampered.public_message == expired.public_message
    assert db.subscription_by_email(conn, "reader@example.com").status == "pending"

    # A confirmation token cannot be used on the unsubscribe path.
    assert not subscriptions.unsubscribe_one_click(conn, token, LATER).accepted
    conn.close()


def test_duplicate_active_is_non_enumerating_noop(tmp_path):
    conn = _connect(tmp_path)
    _activate(conn)
    duplicate = _submit(conn, token="second-confirm-token-that-must-never-be-persisted")
    unknown = _submit(conn, email="unknown@example.com", token="u" * 43)

    assert duplicate.public_message == unknown.public_message
    assert not duplicate.should_send_confirmation
    assert db.subscription_by_email(conn, "reader@example.com").status == "active"
    assert conn.execute(
        "SELECT COUNT(*) FROM subscription_tokens WHERE purpose = 'confirm'"
    ).fetchone()[0] == 2
    conn.close()


def test_unsubscribe_get_does_not_mutate_and_post_is_idempotent(tmp_path):
    conn = _connect(tmp_path)
    _activate(conn)
    token = "unsubscribe-token-with-at-least-thirty-two-bytes-001"
    prepared = subscriptions.prepare_unsubscribe(
        conn,
        "reader@example.com",
        BASE_URL,
        LATER,
        token_factory=lambda: token,
    )
    assert prepared is not None
    assert prepared.url == f"{BASE_URL}/unsubscribe/{token}"

    page = subscriptions.unsubscribe_page_data(conn, token, LATER)
    assert page.token_accepted
    assert page.one_click_post_value == "List-Unsubscribe=One-Click"
    assert db.subscription_by_email(conn, "reader@example.com").status == "active"

    first = subscriptions.unsubscribe_one_click(conn, token, LATER)
    second = subscriptions.unsubscribe_one_click(conn, token, LATER + dt.timedelta(seconds=1))
    assert first.accepted and second.accepted
    assert first.public_message == second.public_message
    assert db.subscription_by_email(conn, "reader@example.com").status == "unsubscribed"
    assert subscriptions.active_recipients(conn) == ()
    conn.close()


def test_unsubscribe_expiry_and_tampering_do_not_remove_recipient(tmp_path):
    conn = _connect(tmp_path)
    _activate(conn)
    token = "unsubscribe-token-with-at-least-thirty-two-bytes-002"
    subscriptions.prepare_unsubscribe(
        conn,
        "reader@example.com",
        BASE_URL,
        LATER,
        lifetime=dt.timedelta(seconds=1),
        token_factory=lambda: token,
    )

    assert not subscriptions.unsubscribe_one_click(conn, token + "x", LATER).accepted
    assert not subscriptions.unsubscribe_one_click(
        conn, token, LATER + dt.timedelta(minutes=1)
    ).accepted
    assert subscriptions.active_recipients(conn) == ("Reader@Example.com",)
    conn.close()


def test_resubscribe_after_unsubscribe_requires_fresh_confirmation(tmp_path):
    conn = _connect(tmp_path)
    first_confirm = "first-confirm-token-with-at-least-thirty-two-bytes"
    _activate(conn, token=first_confirm)
    unsubscribe = "unsubscribe-token-with-at-least-thirty-two-bytes-003"
    subscriptions.prepare_unsubscribe(
        conn,
        "reader@example.com",
        BASE_URL,
        LATER,
        token_factory=lambda: unsubscribe,
    )
    subscriptions.unsubscribe_one_click(conn, unsubscribe, LATER)

    second_confirm = "second-confirm-token-with-at-least-thirty-two-byte"
    resubmission = subscriptions.submit_subscription(
        conn,
        "reader@example.com",
        BASE_URL,
        LATER + dt.timedelta(minutes=1),
        token_factory=lambda: second_confirm,
    )
    assert resubmission.should_send_confirmation
    assert db.subscription_by_email(conn, "reader@example.com").status == "pending"
    assert subscriptions.active_recipients(conn) == ()
    assert not subscriptions.confirm_subscription(conn, first_confirm, LATER).accepted

    assert subscriptions.confirm_subscription(
        conn, second_confirm, LATER + dt.timedelta(minutes=2)
    ).accepted
    assert subscriptions.active_recipients(conn) == ("reader@example.com",)
    conn.close()


def test_unsubscribe_immediately_excludes_existing_pending_and_failed_delivery_rows(tmp_path):
    conn = _connect(tmp_path)
    _activate(conn)
    now = "2026-07-27T00:02:00+00:00"
    db.ensure_delivery_recipients(conn, "2026-07-27", ("reader@example.com",), now)
    assert subscriptions.delivery_recipients(conn, "2026-07-27") == (
        "Reader@Example.com",
    )
    assert db.claim_delivery(conn, "2026-07-27", "reader@example.com", now)
    db.finish_delivery(
        conn,
        "2026-07-27",
        "reader@example.com",
        "failed",
        now,
        "recipient_rejected",
    )
    assert subscriptions.delivery_recipients(
        conn, "2026-07-27", retry_failed_only=True
    ) == ("Reader@Example.com",)

    token = "unsubscribe-token-with-at-least-thirty-two-bytes-005"
    subscriptions.prepare_unsubscribe(
        conn,
        "reader@example.com",
        BASE_URL,
        LATER,
        token_factory=lambda: token,
    )
    subscriptions.unsubscribe_one_click(conn, token, LATER)
    assert subscriptions.delivery_recipients(conn, "2026-07-27") == ()
    assert subscriptions.delivery_recipients(
        conn, "2026-07-27", retry_failed_only=True
    ) == ()
    conn.close()


def test_admin_output_is_masked_and_add_reactivates_disabled_record(tmp_path):
    conn = _connect(tmp_path)
    assert subscriptions.add_admin_test_recipient(conn, "operator@example.com", NOW)
    state = db.admin_test_recipient_state(conn, "operator@example.com")
    assert state is not None and state.email == "operator@example.com"
    states = subscriptions.admin_subscription_list(conn)
    assert len(states) == 1
    assert states[0].email_masked == "o***@e***.com"
    assert states[0].source == "admin_test"
    assert "operator@example.com" not in repr(states)

    assert subscriptions.disable_admin_test_recipient(conn, "operator@example.com", LATER)
    assert subscriptions.add_admin_test_recipient(conn, "operator@example.com", LATER)
    assert subscriptions.active_recipients(conn) == ("operator@example.com",)
    assert subscriptions.admin_subscription_counts(conn) == {
        "pending": 0,
        "active": 1,
        "unsubscribed": 0,
        "disabled": 0,
    }
    conn.close()


def test_admin_add_reactivates_existing_states_without_changing_source_or_identity(tmp_path):
    conn = _connect(tmp_path)

    pending_token = "pending-confirm-token-with-at-least-thirty-two-bytes"
    _submit(conn, "pending@example.com", pending_token)
    pending_before = db.subscription_by_email(conn, "pending@example.com")

    _activate(
        conn,
        email="former@example.com",
        token="former-confirm-token-with-at-least-thirty-two-bytes",
    )
    unsubscribe_token = "unsubscribe-token-with-at-least-thirty-two-bytes-004"
    subscriptions.prepare_unsubscribe(
        conn,
        "former@example.com",
        BASE_URL,
        LATER,
        token_factory=lambda: unsubscribe_token,
    )
    subscriptions.unsubscribe_one_click(conn, unsubscribe_token, LATER)
    former_before = db.subscription_by_email(conn, "former@example.com")

    assert subscriptions.add_admin_test_recipient(conn, "disabled@example.com", NOW)
    disabled_before = db.subscription_by_email(conn, "disabled@example.com")
    assert disabled_before is not None
    assert subscriptions.disable_subscription_id(conn, disabled_before.id, LATER)

    for email in ("pending@example.com", "former@example.com", "disabled@example.com"):
        assert subscriptions.add_admin_test_recipient(conn, email, LATER)

    pending_after = db.subscription_by_email(conn, "pending@example.com")
    former_after = db.subscription_by_email(conn, "former@example.com")
    disabled_after = db.subscription_by_email(conn, "disabled@example.com")
    assert pending_before is not None and former_before is not None
    assert (pending_after.id, pending_after.status, pending_after.source) == (
        pending_before.id,
        "active",
        "public",
    )
    assert (former_after.id, former_after.status, former_after.source) == (
        former_before.id,
        "active",
        "public",
    )
    assert (disabled_after.id, disabled_after.status, disabled_after.source) == (
        disabled_before.id,
        "active",
        "admin_test",
    )
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM subscription_tokens").fetchone()[0] == 0
    assert subscriptions.active_recipients(conn) == (
        "pending@example.com",
        "former@example.com",
        "disabled@example.com",
    )
    conn.close()


def test_admin_id_lifecycle_ignores_source_and_delete_cascades_tokens(tmp_path):
    conn = _connect(tmp_path)
    confirm_token = "public-confirm-token-with-at-least-thirty-two-bytes"
    _submit(conn, "reader@example.com", confirm_token)
    state = db.subscription_by_email(conn, "reader@example.com")
    assert state is not None and state.status == "pending" and state.source == "public"

    assert not subscriptions.enable_subscription_id(conn, state.id, LATER)
    assert subscriptions.add_admin_test_recipient(conn, "reader@example.com", LATER)
    assert db.subscription_by_email(conn, "reader@example.com").status == "active"
    assert subscriptions.active_subscription_recipient_id(conn, state.id) == "reader@example.com"
    assert conn.execute("SELECT COUNT(*) FROM subscription_tokens").fetchone()[0] == 0

    unsubscribe_token = "admin-lifecycle-unsubscribe-token-with-thirty-two-bytes"
    assert subscriptions.prepare_unsubscribe(
        conn,
        "reader@example.com",
        BASE_URL,
        LATER,
        token_factory=lambda: unsubscribe_token,
    )
    assert subscriptions.disable_subscription_id(conn, state.id, LATER)
    assert db.subscription_by_email(conn, "reader@example.com").status == "disabled"
    assert subscriptions.active_subscription_recipient_id(conn, state.id) is None
    assert subscriptions.active_recipients(conn) == ()
    assert conn.execute("SELECT COUNT(*) FROM subscription_tokens").fetchone()[0] == 0

    assert subscriptions.enable_subscription_id(conn, state.id, LATER)
    assert not subscriptions.enable_subscription_id(conn, state.id, LATER)
    assert subscriptions.prepare_unsubscribe(
        conn,
        "reader@example.com",
        BASE_URL,
        LATER,
        token_factory=lambda: "delete-cascade-token-with-at-least-thirty-two-bytes",
    )
    assert subscriptions.delete_subscription_id(conn, state.id)
    assert db.subscription_by_email(conn, "reader@example.com") is None
    assert conn.execute("SELECT COUNT(*) FROM subscription_tokens").fetchone()[0] == 0
    assert not subscriptions.enable_subscription_id(conn, state.id, LATER)
    assert not subscriptions.disable_subscription_id(conn, state.id, LATER)
    assert not subscriptions.delete_subscription_id(conn, state.id)
    assert subscriptions.active_subscription_recipient_id(conn, state.id) is None
    conn.close()


def test_legacy_smtp_recipient_import_runs_once_and_deduplicates(tmp_path):
    conn = _connect(tmp_path)

    assert db.import_legacy_smtp_recipients_once(
        conn,
        ("Legacy@Example.com", "legacy@example.com", "other@example.com"),
        NOW.isoformat(),
    )
    assert subscriptions.active_recipients(conn) == (
        "legacy@example.com",
        "other@example.com",
    )
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 2
    assert {item.source for item in subscriptions.admin_subscription_list(conn)} == {"admin_test"}

    assert not db.import_legacy_smtp_recipients_once(
        conn,
        ("late@example.com",),
        LATER.isoformat(),
    )
    assert db.subscription_by_email(conn, "late@example.com") is None
    conn.close()


def test_concurrent_submit_produces_one_pending_token(tmp_path):
    path = tmp_path / "news.db"
    db.connect(path).close()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def submit(token):
        conn = db.connect(path)
        try:
            barrier.wait()
            results.append(_submit(conn, token=token))
        except BaseException as error:  # pragma: no cover - surfaced below
            errors.append(error)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=submit, args=("a" * 43,)),
        threading.Thread(target=submit, args=("b" * 43,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sum(result.should_send_confirmation for result in results) == 1
    reader = db.connect(path)
    assert db.subscription_by_email(reader, "reader@example.com").status == "pending"
    assert reader.execute(
        "SELECT COUNT(*) FROM subscription_tokens WHERE purpose = 'confirm'"
    ).fetchone()[0] == 1
    reader.close()


def test_concurrent_confirmation_has_one_consistent_active_state(tmp_path):
    path = tmp_path / "news.db"
    conn = db.connect(path)
    token = "concurrent-confirm-token-with-at-least-thirty-two"
    _submit(conn, token=token)
    conn.close()
    barrier = threading.Barrier(2)
    accepted = []

    def confirm():
        worker = db.connect(path)
        barrier.wait()
        accepted.append(subscriptions.confirm_subscription(worker, token, LATER).accepted)
        worker.close()

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(accepted) == [False, True]
    reader = db.connect(path)
    assert db.subscription_by_email(reader, "reader@example.com").status == "active"
    assert reader.execute(
        "SELECT COUNT(*) FROM subscription_tokens WHERE consumed_at IS NOT NULL"
    ).fetchone()[0] == 1
    reader.close()


def test_unsubscribe_headers_are_recipient_specific_and_safe():
    first = "https://news.cheapcoding.top/unsubscribe/first-token"
    second = "https://news.cheapcoding.top/unsubscribe/second-token"
    assert unsubscribe_headers(first) == {
        "List-Unsubscribe": f"<{first}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }

    base = compose(
        "Digest",
        "Plain body",
        "<p>HTML body</p>",
        "news@example.com",
        ("reader@example.com",),
    )
    assert inject_unsubscribe(base, first) == first
    assert str(base["List-Unsubscribe"]) == f"<{first}>"
    inject_unsubscribe(base, second)
    assert str(base["List-Unsubscribe"]) == f"<{second}>"
    assert str(base["List-Unsubscribe-Post"]) == "List-Unsubscribe=One-Click"

    with pytest.raises(ValueError, match="absolute HTTPS"):
        unsubscribe_headers("http://news.example.com/unsubscribe/token")
    with pytest.raises(ValueError, match="absolute HTTPS"):
        unsubscribe_headers("https://news.example.com/unsubscribe/token\r\nBcc:x@example.com")


def test_confirmation_message_is_private_escaped_and_has_no_unsubscribe_or_edition_state():
    class FakeSMTP:
        messages = []

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def ehlo(self):
            return 250, b"ok"

        def starttls(self, context=None):
            return 220, b"ready"

        def send_message(self, message):
            type(self).messages.append(message)
            return {}

    config = SmtpConfig(
        host="smtp.example.com",
        port=587,
        username="",
        password="",
        sender="news@example.com",
        recipients=("admin@example.com",),
        security="starttls",
    )
    url = "https://news.example.com/subscribe/confirm/token?unsafe=<tag>"
    message = confirmation_message(config, "reader@example.com", url)
    assert str(message["Subject"]).startswith("[确认]")
    assert str(message["To"]) == "reader@example.com"
    assert message["List-Unsubscribe"] is None
    assert "&lt;tag&gt;" in message.get_body(preferencelist=("html",)).get_content()
    report = send_confirmation(config, "reader@example.com", url, smtp_factory=FakeSMTP)
    assert report.sent_count == 1
    assert str(FakeSMTP.messages[0]["To"]) == "reader@example.com"
    assert "admin@example.com" not in FakeSMTP.messages[0].as_string()


def test_connect_migrates_schema_v1_without_losing_delivery_data(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
        "CREATE TABLE email_deliveries ("
        "edition_date TEXT NOT NULL, recipient_key TEXT NOT NULL, status TEXT NOT NULL,"
        "error_category TEXT, updated_at TEXT NOT NULL,"
        "PRIMARY KEY (edition_date, recipient_key));"
        "INSERT INTO email_deliveries VALUES "
        "('2026-07-27', 'existing-key', 'sent', NULL, '2026-07-27T00:00:00+00:00');"
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(path)
    assert conn.execute(
        "SELECT status FROM email_deliveries WHERE recipient_key = 'existing-key'"
    ).fetchone()[0] == "sent"
    assert db.subscription_counts(conn) == {
        "pending": 0,
        "active": 0,
        "unsubscribed": 0,
        "disabled": 0,
    }
    conn.close()


def test_delivery_recipient_hash_matches_subscription_identity(tmp_path):
    conn = _connect(tmp_path)
    _activate(conn)
    db.ensure_delivery_recipients(
        conn,
        "2026-07-27",
        subscriptions.active_recipients(conn),
        "2026-07-27T00:02:00+00:00",
    )
    delivery = db.delivery_states(conn, "2026-07-27")
    assert [state.recipient_key for state in delivery] == [
        db.delivery_recipient_key("reader@example.com")
    ]
    assert subscriptions.admin_subscription_list(conn)[0].recipient_key == (
        delivery[0].recipient_key[:12]
    )
    conn.close()


def test_delivery_helper_injects_only_the_current_recipient_url():
    class FakeSMTP:
        messages = []

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def ehlo(self):
            return 250, b"ok"

        def starttls(self, context=None):
            return 220, b"ready"

        def send_message(self, message):
            type(self).messages.append(message)
            return {}

    config = SmtpConfig(
        host="smtp.example.com",
        port=587,
        username="",
        password="",
        sender="news@example.com",
        recipients=(),
        security="starttls",
    )
    message = compose(
        "Digest",
        "Plain body",
        "<p>HTML body</p>",
        "news@example.com",
        ("placeholder@example.com",),
    )
    url = "https://news.cheapcoding.top/unsubscribe/recipient-token"
    result = deliver_recipient(
        message,
        config,
        "reader@example.com",
        unsubscribe_url=url,
        smtp_factory=FakeSMTP,
    )
    assert result.status == "sent"
    delivered = FakeSMTP.messages[0]
    assert str(delivered["To"]) == "reader@example.com"
    assert str(delivered["List-Unsubscribe"]) == f"<{url}>"
    assert str(delivered["List-Unsubscribe-Post"]) == "List-Unsubscribe=One-Click"
    assert "placeholder@example.com" not in delivered.as_string()


def test_subscription_input_and_public_url_validation(tmp_path):
    conn = _connect(tmp_path)
    with pytest.raises(ValueError, match="valid email"):
        _submit(conn, email="bad address")
    with pytest.raises(ValueError, match="CR/LF"):
        _submit(conn, email="reader@example.com\r\nBcc:x@example.com")
    with pytest.raises(ValueError, match="public hostname"):
        subscriptions.submit_subscription(
            conn,
            "reader@example.com",
            "https://127.0.0.1",
            NOW,
            token_factory=lambda: "x" * 43,
        )
    conn.close()


def test_loopback_confirmation_url_requires_explicit_opt_in(tmp_path):
    conn = _connect(tmp_path)
    token = "loopback-confirmation-token-with-at-least-thirty-two-bytes"
    with pytest.raises(ValueError, match="absolute HTTPS"):
        subscriptions.submit_subscription(
            conn,
            "reader@example.com",
            "http://127.0.0.1:8765",
            NOW,
            token_factory=lambda: token,
        )

    submission = subscriptions.submit_subscription(
        conn,
        "reader@example.com",
        "http://127.0.0.1:8765",
        NOW,
        allow_loopback_http=True,
        token_factory=lambda: token,
    )
    assert submission.confirmation_url == (
        f"http://127.0.0.1:8765/subscribe/confirm/{token}"
    )
    conn.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8765",
        "http://127.0.0.1",
        "https://127.0.0.1:8765",
        "http://127.0.0.1:8765/path",
        "http://user@127.0.0.1:8765",
        "http://news.example.com:8765",
    ],
)
def test_loopback_confirmation_url_rejects_non_loopback_bases(tmp_path, base_url):
    conn = _connect(tmp_path)
    with pytest.raises(ValueError, match="loopback URL"):
        subscriptions.submit_subscription(
            conn,
            "reader@example.com",
            base_url,
            NOW,
            allow_loopback_http=True,
            token_factory=lambda: "x" * 43,
        )
    conn.close()
