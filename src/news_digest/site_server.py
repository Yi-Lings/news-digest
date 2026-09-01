"""面向读者的站点服务:静态托管 + 付费墙门控 + 账号/订阅端点。

与 preview_server(admin 面板)相互独立:
- 服务对象是公开访客,会话 Cookie 与密钥独立(nd_user_session / site-secret);
- 表单为零 JS 的传统 POST,CSRF 用会话派生或匿名 nonce 双提交;
- 完整文章正文只在放行时离开服务端,付费墙插页不携带正文。

门控决策矩阵见 accounts.decide_access;SQL 全部在 storage/db。
"""

import base64
import datetime as dt
import hashlib
import hmac
import html
import io
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw, ImageFont

from news_digest import accounts, payments
from news_digest.payments import EpayConfig
from news_digest.storage import db

_SESSION_TTL_SECONDS = 30 * 24 * 3600
_ANON_TTL_SECONDS = 365 * 24 * 3600
_PUBLIC_CSRF_TTL_SECONDS = 2 * 3600
_BODY_LIMIT = 16_384
_SENSITIVE_WINDOW_SECONDS = 60.0
_SENSITIVE_LIMIT_DEFAULT = 6
_CAPTCHA_TTL_SECONDS = 5 * 60
_CAPTCHA_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_ADMIN_SESSION_COOKIE = "nd_admin_session"
_ADMIN_NAV_MARKER = "<!--ADMIN_NAV-->"
_ACCOUNT_MAIL_WORKER_COUNT = 2
_ACCOUNT_MAIL_LEASE_SECONDS = 60
_ACCOUNT_MAIL_RETRY_SECONDS = 30
_ACCOUNT_MAIL_MAX_ATTEMPTS = 5
_REGISTRATION_ACCEPTED_MESSAGE = (
    "如果该邮箱可以注册，验证码将发送到邮箱；如未收到，请稍后重试。"
)
_LOGIN_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$120000$00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def _payment_order_creation_retryable(order: db.Order, now: dt.datetime) -> bool:
    if order.payment_url is not None:
        return False
    if order.last_error_code == "USER_CANCELLED":
        return False
    if order.settlement_expires_at is None or order.settlement_expires_at <= now.isoformat():
        return False
    if order.status not in {"pending", "expired", "failed"}:
        return False
    if order.last_error_code in {"GATEWAY_CREATE_FAILED", "PAYMENT_WAITING_NO_URL"}:
        return True
    if order.last_error_code == "GATEWAY_CREATE_RUNNING":
        updated_at = dt.datetime.fromisoformat(order.updated_at)
        return (
            now - updated_at
        ).total_seconds() >= db.PAYMENT_CREATION_LEASE_SECONDS
    return False


def _sign(secret: bytes, message: str) -> str:
    return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()


def _constant_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _account_verification_code(
    secret: bytes,
    delivery_token: str,
    email_key: str,
    purpose: db.EmailCodePurpose,
) -> str:
    payload = f"account-mail|{delivery_token}|{email_key}|{purpose}".encode()
    value = int.from_bytes(hmac.new(secret, payload, hashlib.sha256).digest()[:8], "big")
    return f"{value % 1_000_000:06d}"


def _origin_identity(value: str) -> tuple[str, str, int]:
    if not value or "\\" in value or any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError("site origin is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("site origin is invalid")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("site origin is invalid") from None
    return (
        parsed.scheme,
        parsed.hostname.casefold(),
        port or (443 if parsed.scheme == "https" else 80),
    )


def _host_identity(host: str, scheme: str) -> tuple[str, str, int] | None:
    if (
        not host
        or "," in host
        or "/" in host
        or "\\" in host
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in host)
    ):
        return None
    try:
        parsed = urlsplit(f"{scheme}://{host}")
        port = parsed.port
    except ValueError:
        return None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return (
        scheme,
        parsed.hostname.casefold(),
        port or (443 if scheme == "https" else 80),
    )


def _captcha_image_data(code: str) -> str:
    image = Image.new("RGB", (184, 62), (250, 248, 242))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=30)
    except TypeError:  # Pillow < 10.1 compatibility
        font = ImageFont.load_default()
    for _ in range(7):
        color = tuple(105 + secrets.randbelow(90) for _ in range(3))
        draw.line(
            (
                secrets.randbelow(184),
                secrets.randbelow(62),
                secrets.randbelow(184),
                secrets.randbelow(62),
            ),
            fill=color,
            width=1,
        )
    for index, character in enumerate(code):
        draw.text(
            (15 + index * 32, 10 + secrets.randbelow(9)),
            character,
            font=font,
            fill=(25 + secrets.randbelow(55), 25 + secrets.randbelow(55), 25),
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def load_site_secret(secret_file: Path) -> bytes:
    """32 字节站点会话密钥,首访自动创建,权限 600。"""
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    if not secret_file.is_file():
        secret_file.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
    return bytes.fromhex(secret_file.read_text(encoding="utf-8").strip())


@dataclass
class SiteSession:
    user_id: int
    email: str
    paid_until: str | None
    plan: str | None
    is_admin: bool


class RateLimiter:
    """按 (客户端, 动作) 的滑动窗口限流;进程内实现,站点规模足够。"""

    def __init__(
        self,
        limit: int = _SENSITIVE_LIMIT_DEFAULT,
        window: float = _SENSITIVE_WINDOW_SECONDS,
    ):
        self.limit = limit
        self.window = window
        self.hits: dict[tuple[str, str], list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, client: str, action: str, now: float) -> bool:
        with self.lock:
            key = (client, action)
            stamps = [value for value in self.hits.get(key, []) if now - value < self.window]
            if len(stamps) >= self.limit:
                self.hits[key] = stamps
                return False
            stamps.append(now)
            self.hits[key] = stamps
            return True


def _manifest(site_dir: Path) -> dict[str, Any] | None:
    try:
        raw = (site_dir / "release.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _page(title: str, body: str, modal: str = "") -> str:
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex\">"
        f"<title>{html.escape(title)} · Cheapcoding News</title>"
        "<style>"
        ":root{--ink:#1c1b17;--muted:#69665f;--paper:#f5f2eb;--sheet:#fff;"
        "--rule:#cbc5b9;--red:#a52f24;--green:#116b39}"
        "*{box-sizing:border-box}body{font-family:Georgia,'Times New Roman','Songti SC',"
        "serif;margin:0;background:var(--paper);color:var(--ink);line-height:1.7}"
        ".site-head{border-top:5px solid var(--ink);border-bottom:1px solid var(--rule);"
        "background:var(--sheet);padding:1rem max(1.25rem,calc((100% - 70rem)/2))}"
        ".site-kicker{font:700 .72rem/1.2 Arial,sans-serif;color:var(--red);"
        "text-transform:uppercase;margin-bottom:.25rem}"
        ".brand{font-size:clamp(1.45rem,4vw,2rem);font-weight:700;color:var(--ink);"
        "text-decoration:none}.site-nav{display:flex;flex-wrap:wrap;gap:.45rem 1.2rem;"
        "margin-top:.65rem;font:600 .88rem/1.4 Arial,sans-serif}"
        ".site-nav a{color:var(--ink);text-decoration:none;border-bottom:2px solid transparent}"
        ".site-nav a:hover,.site-nav a:focus-visible{border-bottom-color:var(--red)}"
        ".wrap{max-width:46rem;margin:0 auto;padding:2.6rem 1.25rem 4rem;"
        "animation:page-in .32s ease-out both}"
        ".desk-label{font:700 .7rem/1.2 Arial,sans-serif;color:var(--red);"
        "text-transform:uppercase;margin:0 0 .35rem}"
        "h1{font-size:clamp(1.55rem,5vw,2.15rem);line-height:1.25;margin:.1rem 0 1.5rem;"
        "border-bottom:3px solid var(--ink);padding-bottom:.7rem}"
        "h2{font-size:1.08rem;margin:1.7rem 0 .55rem}"
        "a{color:var(--red)}.muted{color:var(--muted);font-size:.9rem}"
        "form{margin:.8rem 0 1.3rem;padding:1rem;border:1px solid var(--rule);"
        "border-left:3px solid var(--ink);background:var(--sheet)}"
        "input,select,textarea,button{font:inherit;padding:.52rem .7rem;margin:.2rem 0;"
        "border:1px solid #aaa49a}"
        "input,select,textarea{width:min(100%,22rem);background:#fff;color:var(--ink)}"
        "input:focus-visible,select:focus-visible,textarea:focus-visible,button:focus-visible{"
        "outline:3px solid rgba(165,47,36,.24);outline-offset:2px}"
        "button{background:var(--ink);color:#fff;border-color:var(--ink);cursor:pointer;"
        "font-weight:700}button:hover{background:var(--red);border-color:var(--red)}"
        ".msg{padding:.75rem 1rem;border-left:4px solid var(--red);background:var(--sheet);"
        "margin:1rem 0}"
        ".price-row{display:flex;align-items:center;gap:.55rem;flex-wrap:wrap;margin:.5rem 0}"
        ".price-original{text-decoration:line-through;color:#777}"
        ".discount-badge{display:inline-block;border:1px solid #178447;color:#116b39;"
        "background:#edf8f1;padding:.08rem .4rem;font:700 .82rem sans-serif}"
        ".price-current{font-size:1.15rem;font-weight:700;color:#a63a2b}"
        ".plans-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem;"
        "margin:1rem 0 1.5rem}.plan-card{border:1px solid var(--rule);border-radius:6px;"
        "background:var(--sheet);padding:1.25rem;display:flex;flex-direction:column;"
        "min-width:0}.plan-card.is-featured{border:2px solid var(--ink);"
        "padding:calc(1.25rem - 1px)}"
        ".plan-head{display:flex;align-items:center;justify-content:space-between;gap:.5rem}"
        ".plan-name{font-size:1.45rem;line-height:1.2;margin:0}.plan-mark{font:700 .72rem/1.2 "
        "Arial,sans-serif;color:var(--red);border:1px solid rgba(165,47,36,.35);"
        "padding:.18rem .42rem}.plan-summary{color:var(--muted);min-height:3.4em;margin:.65rem 0}"
        ".plan-price{margin:.45rem 0 1rem}.plan-price .price-current{font-size:1.55rem}"
        ".plan-period{font-size:.9rem;color:var(--muted);margin-left:.2rem}"
        ".plan-cta{display:block;text-align:center;background:var(--ink);color:#fff;"
        "text-decoration:none;padding:.7rem 1rem;font:700 .92rem/1.3 Arial,sans-serif;"
        "border:1px solid var(--ink)}.plan-card.is-featured .plan-cta{background:var(--red);"
        "border-color:var(--red)}.plan-cta.is-disabled,.plan-card.is-featured "
        ".plan-cta.is-disabled{background:#777;border-color:#777;cursor:not-allowed}"
        ".pay-modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;width:100vw;height:100vh;"
        "background:rgba(28,27,23,.68);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);"
        "z-index:999999;display:flex;align-items:center;justify-content:center;padding:1rem;"
        "overflow:hidden;box-sizing:border-box;margin:0}"
        ".pay-modal-card{background:var(--sheet);border:2px solid var(--ink);"
        "box-shadow:0 16px 40px rgba(0,0,0,.35),4px 4px 0 var(--ink);border-radius:6px;"
        "width:100%;max-width:390px;margin:auto;box-sizing:border-box;"
        "animation:modalPop .18s cubic-bezier(.16,1,.3,1) forwards}"
        "@keyframes modalPop{from{opacity:0;transform:scale(.95) translateY(8px)}"
        "to{opacity:1;transform:scale(1) translateY(0)}}"
        ".pay-modal-head{display:flex;align-items:center;justify-content:space-between;"
        "padding:.75rem 1rem;border-bottom:1.5px solid var(--rule);background:var(--paper)}"
        ".pay-modal-title{margin:0;font-size:1.1rem;color:var(--ink);font-weight:700}"
        ".pay-modal-close{background:transparent;border:none;font-size:1.2rem;font-weight:700;"
        "color:var(--muted);cursor:pointer;padding:.2rem .45rem;line-height:1;border-radius:3px;"
        "transition:all .15s ease}"
        ".pay-modal-close:hover{color:#fff;background:var(--red)}"
        ".pay-modal-body{padding:1rem;box-sizing:border-box}"
        ".pay-modal-plan-info{display:flex;align-items:baseline;justify-content:space-between;"
        "padding:.55rem .85rem;background:var(--paper);border:1.5px solid var(--rule);"
        "border-radius:4px;margin-bottom:.75rem}"
        ".pay-modal-plan-name{font-weight:700;font-size:.98rem;color:var(--ink)}"
        ".pay-modal-plan-price{font-weight:700;font-size:1.35rem;color:var(--red);"
        "font-family:Arial,sans-serif}"
        ".pay-modal-tip{font-size:.82rem;color:var(--muted);margin:0 0 .75rem;line-height:1.4}"
        ".pay-modal-options{display:flex;flex-direction:column;gap:.65rem}"
        ".pay-channel-btn{display:flex;align-items:center;gap:.85rem;width:100%;"
        "padding:.75rem .95rem;background:var(--paper);border:1.5px solid var(--rule);"
        "border-radius:4px;cursor:pointer;text-align:left;transition:all .15s ease;"
        "box-sizing:border-box}"
        ".pay-channel-btn:hover{border-color:var(--ink);background:#fff;"
        "transform:translateY(-1px);box-shadow:2px 2px 0 var(--ink)}"
        ".pay-icon-lg{display:inline-flex;align-items:center;justify-content:center;width:2.1rem;"
        "height:2.1rem;border-radius:50%;font-size:1.05rem;font-weight:900;color:#fff;flex-shrink:0}"
        ".pay-icon-alipay{background:#1677ff}.pay-icon-wxpay{background:#07c160}"
        ".pay-channel-text{flex:1;min-width:0;display:flex;flex-direction:column}"
        ".pay-channel-text strong{font-size:.95rem;color:var(--ink)}"
        ".pay-channel-text small{font-size:.75rem;color:var(--muted);margin-top:.05rem}"
        ".pay-channel-arrow{font-size:1.1rem;font-weight:700;color:var(--muted);"
        "transition:transform .15s ease}"
        ".pay-channel-btn:hover .pay-channel-arrow{color:var(--ink);transform:translateX(3px)}"
        ".plan-benefits{list-style:none;padding:0;margin:1rem 0 0}"
        ".plan-benefits li{position:relative;padding:.35rem 0 .35rem 1.35rem;color:var(--muted)}"
        ".plan-benefits li::before{content:'✓';position:absolute;left:0;color:var(--green);"
        "font-weight:700}"
        ".field{display:block;margin:.65rem 0}.field>span{display:block;font:700 .82rem "
        "Arial,sans-serif;margin-bottom:.15rem}.captcha-row{display:flex;align-items:center;"
        "gap:.75rem;flex-wrap:wrap}.captcha-image{width:184px;height:62px;border:1px solid "
        "var(--rule);background:#fff}.captcha-row input{width:10rem}"
        ".action-link{display:inline-block;padding:.12rem .5rem;background:var(--ink);"
        "color:#fff!important;text-decoration:none!important;font:700 .82rem Arial,sans-serif;"
        "border-radius:2px;margin-left:.35rem}.action-link:hover{background:var(--red)}"
        ".order-list-wrap{max-height:240px;overflow-y:auto;-webkit-overflow-scrolling:touch;"
        "border:1.5px solid var(--rule);background:var(--sheet);border-radius:4px;"
        "padding:0 .85rem;margin:.8rem 0 1.5rem;box-shadow:inset 0 2px 4px rgba(0,0,0,.03)}"
        ".order-list-wrap::-webkit-scrollbar{width:6px}"
        ".order-list-wrap::-webkit-scrollbar-track{background:transparent}"
        ".order-list-wrap::-webkit-scrollbar-thumb{background:var(--rule);border-radius:3px}"
        ".order-list-wrap::-webkit-scrollbar-thumb:hover{background:var(--muted)}"
        ".order-list{list-style:none;padding:0;margin:0}"
        ".order-list li{display:grid;grid-template-columns:minmax(0,1fr) auto;"
        "gap:.45rem 1rem;align-items:center;padding:.75rem 0;border-bottom:1px solid var(--rule)}"
        ".order-list li:last-child{border-bottom:0}"
        ".order-meta{min-width:0}.order-number{display:block;font:600 .82rem/1.4 "
        "Arial,sans-serif;overflow-wrap:anywhere}.order-plan{color:var(--muted);font-size:.88rem}"
        ".order-actions{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}"
        ".order-action{display:inline;margin:0;padding:0;border:0;background:transparent}"
        ".order-action button{margin:0}"
        ".order-pay-btn{display:inline-block;background:#b35c00!important;color:#fff!important;"
        "border:1px solid #994e00!important;font:700 .8rem Arial,sans-serif!important;"
        "padding:.24rem .6rem!important;border-radius:3px;cursor:pointer;line-height:1.3;"
        "text-decoration:none!important;transition:all .15s ease;white-space:nowrap}"
        ".order-pay-btn:hover{background:#944800!important;border-color:#7a3b00!important;"
        "color:#fff!important;transform:translateY(-1px)}"
        ".order-cancel-btn{background:transparent!important;color:var(--muted)!important;"
        "border:1px solid var(--rule)!important;font-size:.8rem!important;"
        "padding:.24rem .55rem!important;border-radius:3px;cursor:pointer;line-height:1.3;"
        "transition:all .15s ease}.order-cancel-btn:hover{background:var(--red)!important;"
        "color:#fff!important;border-color:var(--red)!important}"
        ".order-state{font:700 .82rem/1.4 Arial,sans-serif;"
        "white-space:nowrap}.order-state.state-paid{color:var(--green)}"
        ".order-state.state-pending{color:#b35c00}"
        ".order-state.state-expired,.order-state.state-failed{color:var(--muted)}"
        ".contact-card{background:var(--sheet);border:1.5px solid var(--rule);border-radius:6px;"
        "padding:1.6rem;margin-top:1rem;box-shadow:0 1px 4px rgba(0,0,0,.04)}"
        ".contact-card h2{margin-top:0}.contact-email-box{background:var(--paper);"
        "border:1px solid var(--rule);border-radius:4px;padding:.85rem 1.1rem;margin:1rem 0;"
        "display:inline-block}.contact-email-link{font:700 1.05rem Arial,sans-serif;"
        "color:var(--ink);text-decoration:none}.contact-email-link:hover{color:var(--red);"
        "text-decoration:underline}"
        ".terms-row{display:flex;align-items:center;gap:.55rem;margin:1rem 0;"
        "padding:.65rem .85rem;background:var(--paper);border:1.5px solid var(--rule);"
        "border-radius:4px;cursor:pointer;user-select:none;transition:all .15s ease}"
        ".terms-row:hover{border-color:var(--ink);background:#fff}"
        ".terms-row input[type=\"checkbox\"]{width:1.05rem!important;height:1.05rem!important;"
        "margin:0!important;cursor:pointer;pointer-events:none;accent-color:var(--ink)}"
        ".terms-text{font-size:.84rem;color:var(--ink);flex:1}"
        ".terms-link-text{color:var(--red);font-weight:700;text-decoration:underline}"
        ".terms-badge{font:700 .72rem/1.2 Arial,sans-serif;padding:.2rem .45rem;"
        "border-radius:3px;background:var(--rule);color:var(--muted);white-space:nowrap;"
        "transition:all .15s ease}"
        ".terms-badge.is-agreed{background:rgba(46,125,50,.15);color:var(--green);"
        "border:1px solid rgba(46,125,50,.35)}"
        ".pay-modal-card.privacy-card{max-width:540px;padding:0}"
        ".privacy-modal-header{padding:.9rem 1.25rem .75rem;border-bottom:1.5px solid var(--rule);"
        "background:var(--paper);display:flex;align-items:flex-start;justify-content:space-between}"
        ".privacy-kicker{font:700 .7rem Arial,sans-serif;color:var(--red);text-transform:uppercase;"
        "letter-spacing:.08em;margin:0 0 .2rem}"
        ".privacy-card-title{font-family:Georgia,serif;font-size:1.25rem;margin:0;color:var(--ink);"
        "line-height:1.3}"
        ".privacy-content-scroll{max-height:300px;overflow-y:auto;-webkit-overflow-scrolling:touch;"
        "padding:1rem 1.35rem;background:#fdfbf7;font-size:.86rem;line-height:1.65;"
        "color:var(--ink);border-bottom:1.5px solid var(--rule)}"
        ".privacy-content-scroll::-webkit-scrollbar{width:6px}"
        ".privacy-content-scroll::-webkit-scrollbar-track{background:var(--paper)}"
        ".privacy-content-scroll::-webkit-scrollbar-thumb{background:var(--rule);border-radius:3px}"
        ".privacy-content-scroll::-webkit-scrollbar-thumb:hover{background:var(--muted)}"
        ".privacy-clause{margin-bottom:1.1rem}"
        ".privacy-clause h4{font-family:Georgia,serif;font-size:.92rem;font-weight:700;"
        "color:var(--ink);margin:0 0 .35rem}"
        ".privacy-clause p{margin:0 0 .45rem;color:var(--ink)}"
        ".privacy-modal-foot{padding:.85rem 1.25rem 1rem;background:var(--paper);"
        "display:flex;flex-direction:column;gap:.65rem}"
        ".privacy-read-tip{font:600 .78rem/1.3 Arial,sans-serif;color:var(--muted);"
        "text-align:center;margin:0}.privacy-read-tip b{color:var(--red)}"
        ".privacy-foot-actions{display:flex;gap:.65rem;justify-content:flex-end}"
        ".privacy-btn-agree{padding:.48rem 1.2rem!important;"
        "font:700 .88rem Arial,sans-serif!important;"
        "background:var(--ink)!important;color:#fff!important;"
        "border:1px solid var(--ink)!important;border-radius:3px!important;"
        "cursor:pointer!important;width:auto!important;transition:all .15s ease!important}"
        ".privacy-btn-agree:hover:not(:disabled){background:var(--red)!important;"
        "border-color:var(--red)!important}"
        ".privacy-btn-agree:disabled{opacity:.45!important;cursor:not-allowed!important;"
        "background:#777!important;border-color:#777!important}"
        ".privacy-btn-cancel{padding:.48rem .95rem!important;"
        "font:600 .86rem Arial,sans-serif!important;background:transparent!important;"
        "color:var(--muted)!important;border:1px solid var(--rule)!important;"
        "border-radius:3px!important;cursor:pointer!important;width:auto!important}"
        ".privacy-btn-cancel:hover{background:var(--rule)!important;color:var(--ink)!important}"
        ".site-foot{border-top:1px solid var(--rule);padding:1rem 1.25rem 2rem;"
        "text-align:center;color:var(--muted);font-size:.82rem}"
        "@keyframes page-in{from{opacity:0;transform:translateY(7px)}to{opacity:1;"
        "transform:none}}@media(max-width:480px){.site-head{padding:.85rem 1rem}"
        ".wrap{padding:1.7rem 1rem 3rem}form{padding:.85rem}input,select,textarea,button{"
        "width:100%}.plans-grid{grid-template-columns:1fr}.plan-summary{min-height:0}"
        ".pay-methods{flex-direction:column;align-items:stretch}.pay-method-badge{justify-content:center}"
        ".order-list li{grid-template-columns:1fr}"
        ".order-actions,.order-action,.order-action button{width:100%}"
        ".captcha-row input{width:100%}.site-nav{display:grid;"
        "grid-template-columns:repeat(2,minmax(0,1fr));"
        "gap:.45rem .8rem}}"
        ".back-to-top{position:fixed;right:2rem;bottom:2.2rem;width:2.75rem;height:2.75rem;"
        "border-radius:50%;background:var(--ink);color:#fff;border:1px solid var(--ink);"
        "box-shadow:0 4px 12px rgba(0,0,0,.18);display:flex;align-items:center;"
        "justify-content:center;cursor:pointer;z-index:99;opacity:0;visibility:hidden;"
        "transform:translateY(12px);transition:opacity .22s ease,transform .22s ease,"
        "visibility .22s ease,background .15s ease}.back-to-top.is-visible{opacity:.9;"
        "visibility:visible;transform:translateY(0)}.back-to-top:hover{opacity:1;"
        "background:var(--red);border-color:var(--red);transform:translateY(-2px)}"
        "@media(max-width:480px){.back-to-top{right:1.1rem;bottom:1.2rem;width:2.4rem;height:2.4rem}}"
        "@media(prefers-reduced-motion:reduce){.wrap{animation:none}}"
        "</style></head><body>"
        "<header class=\"site-head\"><div class=\"site-kicker\">Member edition</div>"
        "<a class=\"brand\" href=\"/\">Cheapcoding News</a>"
        "<nav class=\"site-nav\" aria-label=\"会员导航\"><a href=\"/\">今日</a>"
        "<a href=\"/archive/\">往期归档</a><a href=\"/subscribe\">会员订阅</a>"
        "<a href=\"/account\">我的账户</a><a href=\"/contact\">联系我们</a>"
        f"{_ADMIN_NAV_MARKER}</nav></header>"
        "<main class=\"wrap\"><p class=\"desk-label\">Reader desk</p>"
        f"<h1>{html.escape(title)}</h1>{body}"
        "<p class=\"muted\"><a href=\"/\">返回首页</a></p></main>"
        "<footer class=\"site-foot\">Cheapcoding News · 每日双语新闻</footer>"
        f"{modal}"
        "<button type=\"button\" class=\"back-to-top\" id=\"back-to-top\" title=\"回到顶部\" "
        "aria-label=\"回到顶部\"><svg viewBox=\"0 0 24 24\" width=\"20\" height=\"20\" "
        "aria-hidden=\"true\" focusable=\"false\" fill=\"none\" stroke=\"currentColor\" "
        "stroke-width=\"2.5\" stroke-linecap=\"round\" stroke-linejoin=\"round\">"
        "<polyline points=\"18 15 12 9 6 15\"></polyline></svg></button>"
        "<script src=\"/assets/app.js\" defer></script></body></html>"
    )


def _plan_price_html(settings: dict[str, str], plan: str, label: str) -> str:
    base = accounts.base_price_cents(settings, plan)
    discount = accounts.discount_basis_points(settings, plan)
    discount_label = accounts.discount_label(settings, plan)
    current = accounts.price_cents(settings, plan)
    if discount:
        price = (
            f"<span class=\"price-original\">¥{accounts.format_cents(base)}</span>"
            f"<span class=\"discount-badge\">-{discount_label}%</span>"
            f"<span class=\"price-current\">¥{accounts.format_cents(current)}</span>"
        )
    else:
        price = f"<span class=\"price-current\">¥{accounts.format_cents(base)}</span>"
    return f"<div class=\"price-row\"><strong>{html.escape(label)}</strong>{price}</div>"


def _plan_card_html(
    settings: dict[str, str],
    plan: str,
    label: str,
    period: str,
    target: str,
    *,
    csrf_token: str | None = None,
    action_available: bool = True,
) -> str:
    base = accounts.base_price_cents(settings, plan)
    discount = accounts.discount_basis_points(settings, plan)
    discount_label = accounts.discount_label(settings, plan)
    current = accounts.price_cents(settings, plan)
    original = (
        f"<span class=\"price-original\">¥{accounts.format_cents(base)}</span>"
        f"<span class=\"discount-badge\">-{discount_label}%</span>"
        if discount
        else ""
    )
    featured = plan == "yearly"
    marker = "<span class=\"plan-mark\">推荐</span>" if featured else ""
    summary = "适合长期稳定阅读，按年开通更省心。" if featured else "适合按月体验完整会员内容。"
    duration = "366 天会员有效期" if featured else "31 天会员有效期"
    if not action_available:
        action = (
            '<span class="plan-cta is-disabled" aria-disabled="true">'
            "在线支付暂不可用</span>"
        )
    elif csrf_token is None:
        action = f"<a class=\"plan-cta\" href=\"{target}\">立即订阅</a>"
    else:
        action = (
            f"<button type=\"button\" class=\"plan-cta plan-subscribe-btn\" "
            f"data-plan=\"{plan}\" data-plan-name=\"{html.escape(label)}\" "
            f"data-plan-price=\"¥{accounts.format_cents(current)}\">立即订阅</button>"
        )
    return (
        f"<article class=\"plan-card{' is-featured' if featured else ''}\">"
        f"<div class=\"plan-head\"><h3 class=\"plan-name\">{html.escape(label)}</h3>"
        f"{marker}</div><p class=\"plan-summary\">{summary}</p>"
        f"<div class=\"plan-price\">{original}<span class=\"price-current\">"
        f"¥{accounts.format_cents(current)}</span>"
        f"<span class=\"plan-period\">/{period}</span></div>"
        f"{action}"
        "<ul class=\"plan-benefits\"><li>阅读全部主文章与往期归档</li>"
        "<li>可开启每日邮件简报</li><li>中英对照与学习内容</li>"
        f"<li>{duration}</li></ul></article>"
    )


class SiteHandler(BaseHTTPRequestHandler):
    server_version = "news-digest-site"
    protocol_version = "HTTP/1.1"

    # -- 基础工具 ----------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        if self.server.log_callback is not None:
            self.server.log_callback(format % args)

    def _client(self) -> str:
        peer = self.client_address[0] if self.client_address else "local"
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        forwarded = self.headers.get("X-Real-IP", "").strip()
        if not peer_address.is_loopback or not forwarded or "," in forwarded:
            return str(peer_address)
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            return str(peer_address)

    def _security_headers(self, form_action_origin: str | None = None) -> None:
        form_action = "'self'"
        if form_action_origin is not None:
            form_action += f" {form_action_origin}"
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https:;"
            " style-src 'self' 'unsafe-inline';"
            " frame-ancestors 'none';"
            f" base-uri 'none'; form-action {form_action}",
        )
        self.send_header("Cache-Control", "no-store")

    def _html(
        self,
        status: int,
        body: str,
        headers: list[tuple[str, str]] | None = None,
        *,
        form_action_origin: str | None = None,
    ) -> None:
        body = body.replace(_ADMIN_NAV_MARKER, self._admin_nav_html())
        payload = body.encode("utf-8")
        self.send_response(status)
        self._security_headers(form_action_origin)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _text(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _admin_nav_html(self) -> str:
        session = self._session_user()
        if session is None or not session.is_admin:
            return ""
        return '<a class="admin-entry" href="/admin/">管理后台</a>'

    def _redirect(
        self,
        location: str,
        headers: list[tuple[str, str]] | None = None,
        *,
        form_action_origin: str | None = None,
        status: HTTPStatus = HTTPStatus.FOUND,
    ) -> None:
        self.send_response(status)
        self._security_headers(form_action_origin)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for name, value in headers or []:
            self.send_header(name, value)
        self.end_headers()

    def _cookies(self) -> dict[str, str]:
        header = self.headers.get("Cookie", "")
        jar: dict[str, str] = {}
        for part in header.split(";"):
            if "=" in part:
                name, _, value = part.strip().partition("=")
                jar.setdefault(name, value)
        return jar

    def _set_cookie(self, name: str, value: str, max_age: int) -> str:
        secure = "; Secure" if self.server.secure_cookies else ""
        return (
            f"{name}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}"
        )

    def _read_form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if length <= 0 or length > _BODY_LIMIT:
            return {}
        content_type = self.headers.get("Content-Type", "")
        if "application/x-www-form-urlencoded" not in content_type:
            return {}
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}

    # -- 会话与 CSRF ---------------------------------------------------------

    def _session_user(self) -> SiteSession | None:
        token = self._cookies().get(accounts.USER_COOKIE, "")
        if len(token) < 32 or len(token) > 256:
            return None
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = dt.datetime.now(dt.UTC).isoformat()
        conn = db.connect(self.server.db_path)
        try:
            user_id = db.user_session_owner(
                conn, token_digest=token_digest, now=now
            )
            if user_id is None:
                return None
            user = db.user_by_id(conn, user_id)
        finally:
            conn.close()
        if user is None or user.status != "active":
            return None
        return SiteSession(
            user_id=user.id,
            email=user.email,
            paid_until=user.paid_until,
            plan=user.plan,
            is_admin=user.is_admin,
        )

    def _issue_session(self, user_id: int) -> tuple[str, str]:
        now = dt.datetime.now(dt.UTC)
        expires_at = (
            now + dt.timedelta(seconds=_SESSION_TTL_SECONDS)
        ).isoformat()
        token = secrets.token_urlsafe(32)
        conn = db.connect(self.server.db_path)
        try:
            db.create_user_session(
                conn,
                token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                user_id=user_id,
                expires_at=expires_at,
                now=now.isoformat(),
            )
        finally:
            conn.close()
        return token, self._set_cookie(accounts.USER_COOKIE, token, _SESSION_TTL_SECONDS)

    def _public_csrf(self) -> tuple[str, str]:
        """匿名表单 CSRF:nonce cookie + HMAC token 双提交。"""
        nonce = self._cookies().get("nd_site_csrf", "")
        if len(nonce) < 32:
            nonce = secrets.token_urlsafe(32)
        token = _sign(self.server.session_secret, f"csrf|{nonce}")
        cookie = self._set_cookie("nd_site_csrf", nonce, _PUBLIC_CSRF_TTL_SECONDS)
        return token, cookie

    def _csrf_valid(self, supplied: str) -> bool:
        nonce = self._cookies().get("nd_site_csrf", "")
        if len(nonce) < 32 or not supplied:
            return False
        return _constant_eq(_sign(self.server.session_secret, f"csrf|{nonce}"), supplied)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not self._trusted_host():
            return False
        if origin is None:
            return True  # 传统表单同源提交可无 Origin
        if origin == "null":
            return self.server.local_origin and self.server.loopback_browser_compat
        try:
            matched = _origin_identity(origin) == self.server.expected_origin
            if matched:
                return True
            if self.server.local_origin:
                origin_ident = _origin_identity(origin)
                if origin_ident is not None:
                    return (
                        origin_ident[0] == "http"
                        and origin_ident[1] in {"127.0.0.1", "localhost"}
                        and origin_ident[2] == self.server.server_address[1]
                    )
            return False
        except ValueError:
            return False

    def _trusted_host(self) -> bool:
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            return False
        host = values[0]
        if self.server.local_origin:
            return host in {
                f"127.0.0.1:{self.server.server_address[1]}",
                f"localhost:{self.server.server_address[1]}",
                "127.0.0.1",
                "localhost",
            }
        return _host_identity(host, self.server.scheme) == self.server.expected_origin

    # -- 视图辅助 ------------------------------------------------------------

    def _load_settings(self) -> dict[str, str]:
        conn = db.connect(self.server.db_path)
        try:
            return {
                key: (
                    db.get_setting(conn, key)
                    if db.get_setting(conn, key) is not None
                    else default
                )
                for key, default in accounts.DEFAULT_SETTINGS.items()
            }
        finally:
            conn.close()

    def _reader_key(self, session: SiteSession | None, anon: str | None) -> str | None:
        if session is not None:
            return f"u:{session.user_id}"
        if anon:
            return f"a:{anon}"
        return None

    def _rate_limited(self, action: str, email_key: str | None = None) -> bool:
        now_ts = dt.datetime.now(dt.UTC).timestamp()
        if not self.server.limiter.allow(self._client(), action, now_ts):
            return True
        # 邮箱维度限制防止轮换来源地址绕过验证码/登录限流。
        return email_key is not None and not self.server.limiter.allow(
            f"email:{email_key}", action, now_ts
        )

    def _new_captcha(self) -> tuple[str, str]:
        captcha_id = secrets.token_urlsafe(18)
        answer = "".join(secrets.choice(_CAPTCHA_ALPHABET) for _ in range(5))
        expires_at = dt.datetime.now(dt.UTC).timestamp() + _CAPTCHA_TTL_SECONDS
        with self.server.captcha_lock:
            now = dt.datetime.now(dt.UTC).timestamp()
            self.server.captchas = {
                key: value
                for key, value in self.server.captchas.items()
                if value[1] > now
            }
            if len(self.server.captchas) >= 1000:
                self.server.captchas.pop(next(iter(self.server.captchas)))
            self.server.captchas[captcha_id] = (answer, expires_at)
        return captcha_id, _captcha_image_data(answer)

    def _consume_captcha(self, captcha_id: str, answer: str) -> bool:
        if not captcha_id or len(captcha_id) > 64 or len(answer) > 16:
            return False
        with self.server.captcha_lock:
            record = self.server.captchas.pop(captcha_id, None)
        if record is None or record[1] <= dt.datetime.now(dt.UTC).timestamp():
            return False
        return _constant_eq(record[0], answer.strip().upper())

    # -- GET -----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path != "/healthz" and not self._trusted_host():
            self._html(403, _page("拒绝", "<p>来源校验失败。</p>"))
            return
        try:
            if path == "/healthz":
                self._html(200, _page("OK", "<p>ok</p>"))
                return
            if path == "/login":
                self._render_login()
                return
            if path == "/register":
                self._render_register()
                return
            if path == "/forgot":
                self._render_forgot()
                return
            if path == "/reset":
                self._render_reset()
                return
            if path == "/account":
                redeemed = parse_qs(parsed.query).get("redeemed") == ["1"]
                self._render_account(redeemed=redeemed)
                return
            if path == "/subscribe":
                redeemed = parse_qs(parsed.query).get("redeemed") == ["1"]
                self._render_subscribe(redeemed=redeemed)
                return
            if path in {"/contact", "/contact/"}:
                self._render_contact()
                return
            if path == "/payment/return":
                fields = {
                    key: values[0]
                    for key, values in parse_qs(
                        parsed.query, keep_blank_values=True
                    ).items()
                }
                self._handle_payment_return(fields)
                return
            if path in {"/logout", "/favicon.ico"}:
                self._html(404, _page("404", "<p>未找到</p>"))
                return
            self._serve_static(path)
        except Exception:  # noqa: BLE001 - 顶层兜底,不让线程栈泄漏给访客
            self._html(500, _page("服务错误", "<p>请稍后再试。</p>"))

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not self._trusted_host():
            self._html(403, _page("拒绝", "<p>来源校验失败。</p>"))
            return
        form = self._read_form()
        if path in {"/payment/notify", payments.NEWS_NOTIFY_PATH}:
            self._handle_payment_notify(form)
            return
        handlers = {
            "/register": self._handle_register,
            "/verify": self._handle_verify,
            "/login": self._handle_login,
            "/forgot": self._handle_forgot,
            "/reset": self._handle_reset,
            "/logout": self._handle_logout,
            "/order": self._handle_order,
            "/order-cancel": self._handle_order_cancel,
            "/redeem": self._handle_redeem,
            "/free-read": self._handle_free_read,
            "/newsletter": self._handle_newsletter,
        }
        handler = handlers.get(path)
        if handler is None:
            self._html(404, _page("404", "<p>未找到</p>"))
            return
        if not self._same_origin():
            self._html(403, _page("拒绝", "<p>来源校验失败。</p>"))
            return
        try:
            handler(form)
        except Exception:  # noqa: BLE001
            self._html(500, _page("服务错误", "<p>请稍后再试。</p>"))

    # -- 静态 + 门控 ----------------------------------------------------------

    def _resolve_file(self, path: str) -> Path | None:
        site_root: Path = self.server.site_dir
        relative = path.lstrip("/")
        if relative in {"", "index.html"}:
            relative = "index.html"
        elif not relative.endswith(".html") and "." not in relative.rsplit("/", 1)[-1]:
            candidate = site_root / relative / "index.html"
            if candidate.is_file():
                return candidate.resolve() if candidate.exists() else None
        target = (site_root / relative).resolve()
        try:
            target.relative_to(site_root.resolve())
        except ValueError:
            return None
        return target if target.is_file() else None

    def _serve_static(self, path: str, *, confirm_free_read: bool = False) -> None:
        kind, edition_date, slug = accounts.classify_page(path)
        file_path = self._resolve_file(path)
        if kind != "article" or file_path is None:
            if file_path is None:
                self._html(404, _page("404", "<p>页面不存在。</p>"))
                return
            self._send_file(file_path)
            return

        settings = self._load_settings()
        session = self._session_user()
        paid = session is not None and accounts.is_paid(
            session.paid_until, dt.datetime.now(dt.UTC)
        )
        manifest = _manifest(self.server.site_dir)
        latest_date = manifest.get("edition", {}).get("date") if manifest else None
        main_slugs = {
            item.get("slug")
            for item in (manifest or {}).get("edition", {}).get("articles", [])
        }
        anon = self._cookies().get(accounts.ANON_COOKIE, "")
        anon_cookie_header: list[tuple[str, str]] = []
        if not anon:
            # 匿名身份在首次门控请求时签发;同请求内即可占坑。
            anon = secrets.token_urlsafe(16)
            anon_cookie_header.append(
                ("Set-Cookie", self._set_cookie(accounts.ANON_COOKIE, anon, _ANON_TTL_SECONDS))
            )
        reader_key = self._reader_key(session, anon)
        free_read_path: str | None = None
        if reader_key is not None:
            conn = db.connect(self.server.db_path)
            try:
                row = conn.execute(
                    "SELECT article_path FROM free_reads"
                    " WHERE reader_key = ? AND edition_date = ?",
                    (reader_key, edition_date),
                ).fetchone()
            finally:
                conn.close()
            free_read_path = row["article_path"] if row else None

        decision = accounts.decide_access(
            paywall_on=accounts.paywall_enabled(settings),
            user_is_paid=paid,
            requested_path=path,
            edition_date=edition_date,
            latest_edition_date=latest_date,
            known_main_slug=slug in main_slugs,
            free_read_path=free_read_path,
        )
        if decision in {"allow", "public"}:
            self._send_file(file_path, extra_headers=anon_cookie_header)
            return
        if decision == "paywall_quota":
            title = self._article_title(file_path)
            self._html(
                200,
                self._paywall_page(title, quota=True),
                anon_cookie_header,
            )
            return
        if decision == "paywall":
            title = self._article_title(file_path)
            self._html(200, self._paywall_page(title), anon_cookie_header)
            return
        if decision == "free_read" and reader_key is not None:
            if not confirm_free_read:
                token, _cookie = self._csrf_pair()
                self._html(
                    200,
                    self._free_read_confirmation_page(
                        self._article_title(file_path), path, token
                    ),
                    self._flush_cookie(anon_cookie_header),
                )
                return
            conn = db.connect(self.server.db_path)
            try:
                allowed, _existing = db.claim_free_read(
                    conn,
                    reader_key=reader_key,
                    edition_date=edition_date,
                    article_path=path,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                )
            finally:
                conn.close()
            if allowed:
                self._send_file(file_path, extra_headers=anon_cookie_header)
                return
            self._html(
                200,
                self._paywall_page(self._article_title(file_path), quota=True),
                anon_cookie_header,
            )
            return
        self._send_file(file_path, extra_headers=anon_cookie_header)

    def _handle_free_read(self, form: dict[str, str]) -> None:
        if not self._csrf_valid(form.get("csrf", "")):
            self._html(403, _page("拒绝", "<p>确认已过期，请重新打开文章。</p>"))
            return
        path = form.get("article_path", "")
        kind, _edition_date, _slug = accounts.classify_page(path)
        if kind != "article":
            self._html(400, _page("请求无效", "<p>文章地址无效。</p>"))
            return
        self._serve_static(path, confirm_free_read=True)

    def _article_title(self, file_path: Path) -> str:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")[:4096]
            start = text.find("<title>")
            end = text.find("</title>")
            if 0 <= start < end:
                return text[start + 7 : end]
        except OSError:
            pass
        return "这篇文章"

    def _send_file(
        self, file_path: Path, extra_headers: list[tuple[str, str]] | None = None
    ) -> None:
        suffix = file_path.suffix.lower()
        types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".webp": "image/webp",
            ".ico": "image/x-icon",
            ".xml": "application/xml; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".woff2": "font/woff2",
        }
        payload = file_path.read_bytes()
        if suffix == ".html":
            page = payload.decode("utf-8", errors="replace")
            payload = page.replace(_ADMIN_NAV_MARKER, self._admin_nav_html()).encode("utf-8")
        self.send_response(200)
        self._security_headers()
        if suffix in {".html", ".css", ".js"}:
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Type", types.get(suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(payload)))
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _paywall_page(self, title: str, *, quota: bool = False) -> str:
        reason = (
            "今天的免费阅读名额已经用在另一篇文章上了。"
            if quota
            else "这篇属于往期归档内容,订阅后可阅读全部归档。"
        )
        body = (
            f"<p class=\"muted\">{html.escape(title)}</p>"
            f"<div class=\"msg\">{reason}</div>"
            "<p><a href=\"/subscribe\">查看订阅方案</a> · "
            f"<a href=\"/login\">登录已有账号</a></p>"
        )
        return _page("付费内容", body)

    def _free_read_confirmation_page(self, title: str, path: str, token: str) -> str:
        body = (
            f"<p class=\"muted\">{html.escape(title)}</p>"
            "<div class=\"msg\">你每天可以免费阅读最新一期中的 1 篇文章。"
            "确认后，今天的免费额度将用于本篇文章。</div>"
            "<form method=\"post\" action=\"/free-read\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
            f"<input type=\"hidden\" name=\"article_path\" value=\"{html.escape(path)}\">"
            "<button type=\"submit\">确认使用今日免费额度</button></form>"
            "<p><a href=\"/\">暂不阅读，返回首页</a> · "
            "<a href=\"/subscribe\">查看订阅方案</a></p>"
        )
        return _page("确认免费阅读", body)

    # -- 页面 ----------------------------------------------------------------

    def _csrf_pair(self) -> tuple[str, str]:
        token, cookie = self._public_csrf()
        self._pending_cookie = cookie
        return token, cookie

    def _flush_cookie(self, headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
        cookie = getattr(self, "_pending_cookie", None)
        if cookie:
            headers.append(("Set-Cookie", cookie))
            self._pending_cookie = None
        return headers

    def _render_login(self, message: str = "", email: str = "") -> None:
        token, _cookie = self._csrf_pair()
        message_html = f"<div class=\"msg\">{html.escape(message)}</div>" if message else ""
        body = (
            f"{message_html}"
            "<h2>登录</h2>"
            f"<form method=\"post\" action=\"/login\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
            f"<input type=\"email\" name=\"email\" placeholder=\"邮箱\" "
            f"value=\"{html.escape(email)}\" required> "
            "<input type=\"password\" name=\"password\" placeholder=\"密码\" required> "
            "<button type=\"submit\">登录</button></form>"
            "<p><a href=\"/register\">注册新账号</a> · "
            "<a href=\"/forgot\">忘记密码？</a></p>"
            "<p class=\"muted\">登录仅支持邮箱与密码，不支持验证码登录。</p>"
        )
        self._html(200, _page("登录", body), self._flush_cookie([]))

    def _render_register(
        self, message: str = "", email: str = "", *, verify_step: bool = False
    ) -> None:
        token, _cookie = self._csrf_pair()
        csrf = f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
        message_html = f"<div class=\"msg\">{html.escape(message)}</div>" if message else ""
        if verify_step:
            body = (
                f"{message_html}<h2>输入邮箱验证码</h2>"
                f"<form method=\"post\" action=\"/verify\">{csrf}"
                f"<label class=\"field\"><span>注册邮箱</span><input type=\"email\" "
                f"name=\"email\" value=\"{html.escape(email)}\" readonly required></label>"
                "<label class=\"field\"><span>邮箱验证码</span><input name=\"code\" "
                "inputmode=\"numeric\" autocomplete=\"one-time-code\" "
                "placeholder=\"6 位验证码\" required></label>"
                "<button type=\"submit\">提交注册</button></form>"
                "<p class=\"muted\">验证码 10 分钟内有效；没有收到时可返回重新获取。</p>"
                "<p><a href=\"/register\">返回重新获取验证码</a></p>"
            )
        else:
            captcha_id, captcha_image = self._new_captcha()
            privacy_modal_html = (
                "<div class=\"pay-modal-overlay\" id=\"privacy-modal\" style=\"display:none;\" "
                "aria-hidden=\"true\">"
                "<div class=\"pay-modal-card privacy-card\" role=\"dialog\" "
                "aria-modal=\"true\" aria-labelledby=\"privacy-modal-title\">"
                "<div class=\"privacy-modal-header\">"
                "<div>"
                "<p class=\"privacy-kicker\">Terms &amp; Privacy Policy</p>"
                "<h3 class=\"privacy-card-title\" id=\"privacy-modal-title\">"
                "用户协议与隐私保护条款</h3>"
                "</div>"
                "<button type=\"button\" class=\"pay-modal-close\" id=\"privacy-modal-close\" "
                "aria-label=\"关闭\">&times;</button>"
                "</div>"
                "<div class=\"privacy-content-scroll\" id=\"privacy-modal-body\">"
                "<div class=\"privacy-clause\">"
                "<h4>一、服务宗旨与协议范围</h4>"
                "<p>Cheapcoding News 致力于为读者提供高品质的英语学习与中英双语新闻阅读服务。"
                "本协议系您与本站关于账号注册、使用及相关付费订阅服务所订立的有效契约。</p>"
                "</div>"
                "<div class=\"privacy-clause\">"
                "<h4>二、账号注册与安全责任</h4>"
                "<p>用户注册须提供真实、有效且属于本人的电子邮箱地址。"
                "该邮箱将作为接收验证码、找回密码、简报投递及会员权益绑定的唯一凭据。</p>"
                "<p>用户应当妥善保管账号密码，因个人保管不善导致的账号安全风险由用户自行负责。</p>"
                "</div>"
                "<div class=\"privacy-clause\">"
                "<h4>三、隐私保护与数据收集最小化</h4>"
                "<p>我们严格遵循「数据最小化」隐私保护原则。本站仅收集注册与订阅必需的邮箱、不可逆密码哈希、"
                "支付订单流水号及每日简报启用状态，绝不采集身份证、手机号、地理位置等与新闻阅读无关的个人隐私信息。</p>"
                "<p>我们郑重承诺：绝不向任何第三方出售、转让或出租您的个人信息。</p>"
                "</div>"
                "<div class=\"privacy-clause\">"
                "<h4>四、Cookie 与纯净阅读</h4>"
                "<p>本站仅在必要时使用轻量安全会话 Cookie 用于保持登录与门控状态，"
                "绝不引入第三方跨站广告追踪 SDK，全力保障读者纯净专注的阅读体验。</p>"
                "</div>"
                "<div class=\"privacy-clause\">"
                "<h4>五、会员订阅与工单支持</h4>"
                "<p>付费会员享有全站主文章及往期历史归档无限制阅读权益，以及每日早间双语邮件期刊。"
                "如遇支付问题请在提供工单的时候提供付款记录或问题描述与支付订单号，我们将在收到工单后第一时间为您跟进处理。</p>"
                "</div>"
                "<div class=\"privacy-clause\">"
                "<h4>六、协议生效</h4>"
                "<p>当您向下完整阅读本条款并点击「我已阅读并同意条款」后，"
                "即视为您已充分阅读、完全理解并自愿受本条款所有内容的约束。</p>"
                "</div>"
                "</div>"
                "<div class=\"privacy-modal-foot\">"
                "<p class=\"privacy-read-tip\" id=\"privacy-scroll-tip\">"
                "📜 请向下滚动通读条款全文（已读 <b id=\"privacy-read-percent\">0%</b>）</p>"
                "<div class=\"privacy-foot-actions\">"
                "<button type=\"button\" id=\"privacy-agree-btn\" "
                "class=\"privacy-btn-agree\" disabled>🔒 请先通读条款全文</button>"
                "<button type=\"button\" id=\"privacy-close-btn\" "
                "class=\"privacy-btn-cancel\">暂不同意</button>"
                "</div></div></div></div>"
            )
            body = (
                f"{message_html}<h2>注册新账号</h2>"
                f"<form method=\"post\" action=\"/register\" id=\"register-form\">{csrf}"
                f"<label class=\"field\"><span>邮箱</span><input type=\"email\" "
                f"name=\"email\" value=\"{html.escape(email)}\" "
                "autocomplete=\"email\" required></label>"
                "<label class=\"field\"><span>密码</span><input type=\"password\" "
                "name=\"password\" minlength=\"8\" autocomplete=\"new-password\" "
                "placeholder=\"至少 8 位\" required></label>"
                "<label class=\"field\"><span>再次输入密码</span><input type=\"password\" "
                "name=\"password_confirm\" minlength=\"8\" "
                "autocomplete=\"new-password\" required></label>"
                "<label class=\"field\"><span>图形验证码</span></label>"
                "<div class=\"captcha-row\">"
                f"<img class=\"captcha-image\" src=\"{captcha_image}\" "
                "alt=\"随机图形验证码\" width=\"184\" height=\"62\">"
                f"<input type=\"hidden\" name=\"captcha_id\" value=\"{captcha_id}\">"
                "<input name=\"captcha_answer\" autocomplete=\"off\" "
                "placeholder=\"输入图中字符\" required></div>"
                "<input name=\"website\" tabindex=\"-1\" autocomplete=\"off\" "
                "aria-hidden=\"true\" style=\"position:absolute;left:-9999px\">"
                "<div class=\"terms-row\" id=\"terms-row-trigger\" role=\"button\" tabindex=\"0\">"
                "<input type=\"checkbox\" name=\"agree_terms\" id=\"agree-terms\" "
                "value=\"1\" required tabindex=\"-1\">"
                "<span class=\"terms-text\">我已阅读并同意 <span class=\"terms-link-text\">"
                "《用户协议与隐私条款》</span></span>"
                "<span class=\"terms-badge\" id=\"terms-badge\">点击阅读</span>"
                "</div>"
                "<button type=\"submit\" id=\"register-submit-btn\">获取邮箱验证码</button></form>"
                "<p class=\"muted\">完成图形验证后，系统会向邮箱发送 6 位验证码。</p>"
            )
            body += "<p><a href=\"/login\">已有账号，返回登录</a></p>"
            self._html(200, _page("注册", body, modal=privacy_modal_html), self._flush_cookie([]))
            return

    def _render_forgot(self, message: str = "", email: str = "") -> None:
        token, _cookie = self._csrf_pair()
        message_html = f"<div class=\"msg\">{html.escape(message)}</div>" if message else ""
        body = (
            f"{message_html}<p>输入注册邮箱后,如果账号存在且邮件发送成功,系统会发送重置验证码。</p>"
            f"<form method=\"post\" action=\"/forgot\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
            f"<input type=\"email\" name=\"email\" placeholder=\"邮箱\" "
            f"value=\"{html.escape(email)}\" required> "
            "<button type=\"submit\">发送重置验证码</button></form>"
            "<p><a href=\"/reset\">已有验证码，直接重置密码</a></p>"
        )
        self._html(200, _page("忘记密码", body), self._flush_cookie([]))

    def _render_reset(
        self, message: str = "", email: str = "", code: str = ""
    ) -> None:
        token, _cookie = self._csrf_pair()
        message_html = f"<div class=\"msg\">{html.escape(message)}</div>" if message else ""
        body = (
            f"{message_html}<form method=\"post\" action=\"/reset\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
            f"<input type=\"email\" name=\"email\" placeholder=\"邮箱\" "
            f"value=\"{html.escape(email)}\" required> "
            f"<input name=\"code\" placeholder=\"6 位验证码\" "
            f"value=\"{html.escape(code)}\" required> "
            "<input type=\"password\" name=\"password\" "
            "placeholder=\"新密码(至少 8 位)\" required> "
            "<button type=\"submit\">重置密码</button></form>"
            "<p><a href=\"/login\">返回登录</a></p>"
        )
        self._html(200, _page("重置密码", body), self._flush_cookie([]))

    def _render_account(self, *, redeemed: bool = False) -> None:
        session = self._session_user()
        if session is None:
            self._redirect("/login")
            return
        conn = db.connect(self.server.db_path)
        try:
            now = dt.datetime.now(dt.UTC)
            db.expire_payment_orders(conn, now=now.isoformat())
            user = db.user_by_id(conn, session.user_id)
            orders = db.list_user_orders(conn, user_id=session.user_id, limit=20)
        finally:
            conn.close()
        candidate = next(
            (
                order
                for order in orders
                if order.status in {"pending", "expired", "failed"}
                and order.last_error_code != "PAYMENT_CLOSED"
                and order.settlement_expires_at
                and order.settlement_expires_at > now.isoformat()
                and (now - dt.datetime.fromisoformat(order.updated_at)).total_seconds() >= 15
                and (
                    order.last_error_code != "GATEWAY_CREATE_RUNNING"
                    or (
                        now - dt.datetime.fromisoformat(order.updated_at)
                    ).total_seconds()
                    >= db.PAYMENT_CREATION_LEASE_SECONDS
                )
            ),
            None,
        )
        if candidate is not None:
            try:
                self._reconcile_payment_order(candidate)
            except (payments.PaymentError, RuntimeError, ValueError):
                self.log_message("automatic payment reconciliation failed")
            conn = db.connect(self.server.db_path)
            try:
                user = db.user_by_id(conn, session.user_id)
                orders = db.list_user_orders(conn, user_id=session.user_id, limit=20)
            finally:
                conn.close()
        paid = accounts.is_paid(user.paid_until, dt.datetime.now(dt.UTC))
        plan_labels = {"monthly": "月刊会员", "yearly": "年刊会员"}
        token, _cookie = self._csrf_pair()
        if paid:
            until_date = user.paid_until[:10]
            plan_name = plan_labels.get(user.plan or "", "付费会员")
            status_line = (
                f"会员状态：<strong>{plan_name}</strong>"
                f"（会员有效期至 {until_date}）"
                "<a class=\"action-link\" href=\"/subscribe\">续费</a>"
            )
        else:
            status_line = (
                "会员状态：<strong>当前为免费账号</strong>（每天可阅读最新一期中的 1 篇主文章）"
                "<a class=\"action-link\" href=\"/subscribe\">立即订阅</a>"
            )
        redemption_html = (
            "<div class=\"msg\"><strong>"
            f"{plan_labels.get(user.plan or '', '会员')}已兑换。</strong><br>"
            f"会员有效期至 {html.escape(user.paid_until[:10])}，感谢支持！</div>"
            if redeemed and paid and user.paid_until
            else ""
        )
        status_labels = {
            "pending": "等待支付",
            "paid": "已支付",
            "expired": "已过期 / 已取消",
            "failed": "支付异常",
            "approved": "已开通",
            "rejected": "已过期 / 已取消",
        }
        channel_labels = {
            "alipay": "支付宝支付",
            "wxpay": "微信支付",
        }
        order_items = []
        for order in orders:
            cancel_form = (
                "<form class=\"order-action\" method=\"post\" action=\"/order-cancel\">"
                f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
                f"<input type=\"hidden\" name=\"order_id\" value=\"{order.id}\">"
                "<button type=\"submit\" class=\"order-cancel-btn\">取消订单</button></form>"
            )
            if order.status == "pending" and order.payment_url:
                pay_link = (
                    f"<a class=\"order-pay-btn\" href=\""
                    f"{html.escape(order.payment_url, quote=True)}\">继续支付</a>"
                )
                action = f"<div class=\"order-actions\">{pay_link}{cancel_form}</div>"
            elif order.status in {"pending", "failed"} and _payment_order_creation_retryable(
                order, now
            ):
                pay_type_input = (
                    f"<input type=\"hidden\" name=\"payment_type\" "
                    f"value=\"{html.escape(order.payment_type)}\">"
                    if order.payment_type
                    else ""
                )
                pay_form = (
                    "<form class=\"order-action\" method=\"post\" action=\"/order\">"
                    f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
                    f"<input type=\"hidden\" name=\"plan\" value=\"{order.plan}\">"
                    f"{pay_type_input}"
                    "<button type=\"submit\" class=\"order-pay-btn\">继续支付</button></form>"
                )
                action = f"<div class=\"order-actions\">{pay_form}{cancel_form}</div>"
            else:
                action = (
                    f"<span class=\"order-state state-{order.status}\">"
                    f"{status_labels.get(order.status, '状态未知')}</span>"
                )
            order_number = order.merchant_order_no or f"#{order.id}"
            amount_str = f" · ¥{(order.amount_cents / 100):.2f}" if order.amount_cents else ""
            channel = channel_labels.get(order.payment_type or "")
            channel_str = f" · {channel}" if channel else ""
            plan_str = plan_labels.get(order.plan, order.plan)
            order_items.append(
                "<li><span class=\"order-meta\"><span class=\"order-number\">"
                f"订单编号 {html.escape(order_number)}</span>"
                f"<span class=\"order-plan\">{plan_str}{amount_str}{channel_str}</span>"
                f"</span>{action}</li>"
            )
        order_rows = "".join(order_items) or "<li class=\"muted\">暂无订单</li>"
        body = (
            f"{redemption_html}<p>{status_line}</p>"
            f"<p class=\"muted\">登录邮箱：{html.escape(session.email)}</p>"
            f"<h2>我的订单</h2><div class=\"order-list-wrap\">"
            f"<ul class=\"order-list\">{order_rows}</ul></div>"
            "<p><a href=\"/forgot\">使用邮箱验证码修改密码</a></p>"
            "<p><form method=\"post\" action=\"/logout\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
            "<button type=\"submit\">退出登录</button></form></p>"
        )
        payment_config = self.server.current_payment_config()
        self._html(
            200,
            _page("我的账户", body),
            self._flush_cookie([]),
            form_action_origin=(
                payments.payment_origin(payment_config) if payment_config is not None else None
            ),
        )

    def _render_subscribe(self, message: str = "", *, redeemed: bool = False) -> None:
        settings = self._load_settings()
        session = self._session_user()
        plan_target = "/subscribe" if session is not None else "/register"
        payment_config = self.server.current_payment_config()
        payment_available = session is None or payment_config is not None
        plan_csrf = (
            self._csrf_pair()[0]
            if session is not None and payment_config is not None
            else None
        )
        pricing = (
            "<div class=\"plans-grid\">"
            + _plan_card_html(
                settings,
                "monthly",
                "月刊会员",
                "月",
                plan_target,
                csrf_token=plan_csrf,
                action_available=payment_available,
            )
            + _plan_card_html(
                settings,
                "yearly",
                "年刊会员",
                "年",
                plan_target,
                csrf_token=plan_csrf,
                action_available=payment_available,
            )
            + "</div>"
        )
        user = None
        newsletter = None
        if session is not None:
            conn = db.connect(self.server.db_path)
            try:
                user = db.user_by_id(conn, session.user_id)
                newsletter = db.subscription_by_email(conn, session.email)
            finally:
                conn.close()
        paid = user is not None and user.status == "active" and accounts.is_paid(
            user.paid_until, dt.datetime.now(dt.UTC)
        )
        plan_labels = {"monthly": "月刊会员", "yearly": "年刊会员"}
        if redeemed and paid and user and user.paid_until:
            plan_name = plan_labels.get(user.plan or "", "会员")
            message_html = (
                "<div class=\"msg\"><strong>"
                f"{plan_name}已兑换成功！</strong><br>"
                f"会员有效期至 {html.escape(user.paid_until[:10])}，感谢支持！</div>"
            )
        elif message:
            message_html = f"<div class=\"msg\">{html.escape(message)}</div>"
        else:
            message_html = ""

        if session is None:
            membership_help = (
                "<p><a href=\"/register\">注册</a>或<a href=\"/login\">登录</a>后，"
                "选择会员方案并前往支付。支付成功后立即自动开通会员，如遇支付问题，"
                "请联系我们提交工单。<a href=\"/contact\" class=\"action-link\">联系我们</a></p>"
            )
            redeem_html = (
                "<h2>卡密兑换</h2>"
                "<p class=\"muted\">拥有会员卡密？请先<a href=\"/login\">登录</a>，"
                "或<a href=\"/register\">注册账号</a>后在此输入卡密兑换会员。</p>"
            )
            newsletter_html = (
                "<p>每日简报是付费会员权益。请先<a href=\"/login\">登录</a>，"
                "或<a href=\"/register\">注册账号</a>后开通会员。</p>"
            )
        else:
            token = plan_csrf or self._csrf_pair()[0]
            if payment_config is None:
                membership_help = (
                    '<div class="msg">在线支付暂不可用，请稍后再试。'
                    "你仍可在下方兑换已有卡密。</div>"
                )
            else:
                membership_help = (
                    "<p>点击会员方案将直接前往安全支付收银台；支付成功后立即自动开通会员，"
                    "如遇支付问题，请联系我们提交工单。"
                    "<a href=\"/contact\" class=\"action-link\">联系我们</a></p>"
                )
            redeem_html = (
                "<h2>卡密兑换</h2>"
                f"<form method=\"post\" action=\"/redeem\">"
                f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
                "<input name=\"code\" placeholder=\"输入卡密 (如 XXXX-XXXX)\" required> "
                "<button type=\"submit\">兑换卡密</button></form>"
                "<p class=\"muted\">输入有效卡密并点击兑换，会员时长将实时充值到当前登录账号。</p>"
            )
            if not paid:
                newsletter_html = (
                    "<p>当前账号尚无有效付费会员。在线支付成功或兑换卡密开通后，"
                    "即可在此开启每日邮件简报。</p>"
                )
            elif newsletter is not None and newsletter.status == "disabled":
                newsletter_html = "<p>该邮箱的简报订阅已由管理员停用，请联系管理员处理。</p>"
            else:
                active = newsletter is not None and newsletter.status == "active"
                action = "disable" if active else "enable"
                label = "停止每日简报" if active else "订阅每日简报"
                state = "已开启，期刊会发送到你的注册邮箱。" if active else "尚未开启。"
                newsletter_html = (
                    f"<p>{state}</p><form method=\"post\" action=\"/newsletter\">"
                    f"<input type=\"hidden\" name=\"csrf\" value=\"{token}\">"
                    f"<input type=\"hidden\" name=\"action\" value=\"{action}\">"
                    f"<button type=\"submit\">{label}</button></form>"
                )
        pay_modal_html = ""
        if plan_csrf is not None:
            buttons_html = []
            if payment_config is not None and payment_config.allows_alipay:
                buttons_html.append(
                    "<button type=\"submit\" name=\"payment_type\" value=\"alipay\" "
                    "class=\"pay-channel-btn pay-channel-alipay\">"
                    "<span class=\"pay-icon-lg pay-icon-alipay\">支</span>"
                    "<span class=\"pay-channel-text\"><strong>支付宝支付</strong>"
                    "<small>支持支付宝 App 扫码</small></span>"
                    "<span class=\"pay-channel-arrow\">→</span></button>"
                )
            if payment_config is not None and payment_config.allows_wxpay:
                buttons_html.append(
                    "<button type=\"submit\" name=\"payment_type\" value=\"wxpay\" "
                    "class=\"pay-channel-btn pay-channel-wxpay\">"
                    "<span class=\"pay-icon-lg pay-icon-wxpay\">微</span>"
                    "<span class=\"pay-channel-text\"><strong>微信支付</strong>"
                    "<small>支持微信 App 扫码</small></span>"
                    "<span class=\"pay-channel-arrow\">→</span></button>"
                )
            options_content = "".join(buttons_html)
            pay_modal_html = (
                "<div id=\"pay-modal\" class=\"pay-modal-overlay\" style=\"display:none;\" "
                "aria-hidden=\"true\">"
                "<div class=\"pay-modal-card\" role=\"dialog\" aria-modal=\"true\" "
                "aria-labelledby=\"pay-modal-title\">"
                "<div class=\"pay-modal-head\">"
                "<h3 id=\"pay-modal-title\" class=\"pay-modal-title\">选择支付方式</h3>"
                "<button type=\"button\" class=\"pay-modal-close\" id=\"pay-modal-close\" "
                "aria-label=\"关闭\">✕</button></div>"
                "<div class=\"pay-modal-body\">"
                "<div class=\"pay-modal-plan-info\">"
                "<span class=\"pay-modal-plan-name\" id=\"pay-modal-plan-name\">会员订阅</span>"
                "<span class=\"pay-modal-plan-price\" id=\"pay-modal-plan-price\"></span></div>"
                "<p class=\"pay-modal-tip\">请选择支付渠道，即将跳转至安全扫码支付：</p>"
                "<form method=\"post\" action=\"/order\" id=\"pay-modal-form\">"
                f"<input type=\"hidden\" name=\"csrf\" value=\"{plan_csrf}\">"
                "<input type=\"hidden\" name=\"plan\" "
                "id=\"pay-modal-plan-input\" value=\"monthly\">"
                f"<div class=\"pay-modal-options\">{options_content}</div>"
                "</form></div></div></div>"
            )
        body = (
            f"{message_html}<h2>付费会员</h2>"
            f"{pricing}"
            "<p>付费后可阅读全部主文章与完整归档；免费读者每天可读最新一期中的 1 篇。</p>"
            f"{membership_help}"
            f"{redeem_html}"
            "<h2>每日简报</h2>"
            "<p class=\"muted\">每日邮件期刊仅向有效付费会员开放，会员到期后自动停止投递。</p>"
            f"{newsletter_html}"
        )
        self._html(
            200,
            _page("订阅", body, modal=pay_modal_html),
            self._flush_cookie([]),
            form_action_origin=(
                payments.payment_origin(payment_config) if payment_config is not None else None
            ),
        )

    def _render_contact(self) -> None:
        settings = self._load_settings()
        contact_email = (
            settings.get("contact_email", "").strip() or "support@cheapcoding.top"
        )
        body = (
            "<div class=\"contact-card\">"
            "<p>如果使用中遇见任何问题，可随时向以下邮箱提供工单：</p>"
            "<p class=\"contact-email-box\">"
            f"<a href=\"mailto:{html.escape(contact_email)}\" class=\"contact-email-link\">"
            f"✉ {html.escape(contact_email)}</a>"
            "</p>"
            "<p class=\"muted\" style=\"font-size:.85rem;margin-top:1.2rem;\">"
            "我们会在收到工单邮件后尽快为您跟进处理。"
            "如遇支付问题请在提供工单的时候提供付款记录或问题描述与支付订单号。"
            "</p>"
            "</div>"
        )
        self._html(200, _page("联系我们", body), self._flush_cookie([]))

    def _handle_newsletter(self, form: dict[str, str]) -> None:
        if not self._csrf_valid(form.get("csrf", "")):
            self._html(403, _page("拒绝", "<p>会话已过期，请重新打开订阅页。</p>"))
            return
        session = self._session_user()
        if session is None:
            self._redirect("/login")
            return
        action = form.get("action")
        if action not in {"enable", "disable"}:
            self._html(400, _page("请求无效", "<p>订阅操作无效。</p>"))
            return
        conn = db.connect(self.server.db_path)
        try:
            user = db.user_by_id(conn, session.user_id)
            paid = user is not None and user.status == "active" and accounts.is_paid(
                user.paid_until, dt.datetime.now(dt.UTC)
            )
            if not paid:
                self._render_subscribe("只有有效付费会员可以订阅每日简报。")
                return
            db.set_member_newsletter_subscription(
                conn,
                user.email,
                enabled=action == "enable",
                now=dt.datetime.now(dt.UTC).isoformat(),
            )
        except RuntimeError:
            self._render_subscribe("该邮箱的简报订阅已由管理员停用。")
            return
        finally:
            conn.close()
        self._render_subscribe("每日简报订阅已更新。")

    # -- 动作 ----------------------------------------------------------------

    def _handle_register(self, form: dict[str, str]) -> None:
        raw_email = form.get("email", "")
        try:
            throttle_key = accounts.email_key(accounts.normalize_email(raw_email))
        except accounts.AccountError:
            throttle_key = None
        if self._rate_limited("register", throttle_key):
            self._html(429, _page("稍候", "<p>操作过于频繁,请稍后再试。</p>"))
            return
        if not self._csrf_valid(form.get("csrf", "")):
            self._render_register("会话过期,请重试。")
            return
        if form.get("website", "").strip():
            self._render_register(
                _REGISTRATION_ACCEPTED_MESSAGE,
                raw_email,
                verify_step=True,
            )
            return
        if form.get("agree_terms", "").strip() not in {"1", "true", "on"}:
            self._render_register(
                "请阅读并勾选同意《用户协议与隐私条款》后再注册。",
                form.get("email", ""),
            )
            return
        if form.get("password", "") != form.get("password_confirm", ""):
            self._render_register("两次输入的密码不一致。", form.get("email", ""))
            return
        if not self._consume_captcha(
            form.get("captcha_id", ""), form.get("captcha_answer", "")
        ):
            self._render_register("图形验证码错误或已过期。", form.get("email", ""))
            return
        try:
            email = accounts.normalize_email(form.get("email", ""))
            password_hash = accounts.hash_password(form.get("password", ""))
        except accounts.AccountError as error:
            self._render_register(str(error), form.get("email", ""))
            return
        key = accounts.email_key(email)
        conn = db.connect(self.server.db_path)
        try:
            existing = db.user_by_email_key(conn, key)
            if existing is not None and existing.status in {"active", "disabled"}:
                self._render_register(_REGISTRATION_ACCEPTED_MESSAGE, email, verify_step=True)
                return
            try:
                db.upsert_pending_user(
                    conn, email=email, email_key=key, password_hash=password_hash,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                )
            except RuntimeError:
                self._render_register(_REGISTRATION_ACCEPTED_MESSAGE, email, verify_step=True)
                return
            delivery_token = secrets.token_urlsafe(32)
            code = _account_verification_code(
                self.server.session_secret, delivery_token, key, "register"
            )
            db.issue_email_code_with_outbox(
                conn,
                email_key=key,
                purpose="register",
                code_digest=accounts.code_digest(code, key),
                delivery_token=delivery_token,
                ttl_seconds=accounts.code_ttl_seconds(),
                now=dt.datetime.now(dt.UTC).isoformat(),
            )
        finally:
            conn.close()
        self.server.wake_account_mail()
        self._render_register(_REGISTRATION_ACCEPTED_MESSAGE, email, verify_step=True)

    def _handle_verify(self, form: dict[str, str]) -> None:
        raw_email = form.get("email", "")
        try:
            throttle_key = accounts.email_key(accounts.normalize_email(raw_email))
        except accounts.AccountError:
            throttle_key = None
        if self._rate_limited("verify", throttle_key):
            self._html(429, _page("稍候", "<p>操作过于频繁,请稍后再试。</p>"))
            return
        if not self._csrf_valid(form.get("csrf", "")):
            self._render_register("会话过期,请重试。", form.get("email", ""), verify_step=True)
            return
        try:
            email = accounts.normalize_email(form.get("email", ""))
        except accounts.AccountError as error:
            self._render_register(str(error), form.get("email", ""), verify_step=True)
            return
        key = accounts.email_key(email)
        code = form.get("code", "").strip()
        conn = db.connect(self.server.db_path)
        try:
            ok = db.consume_email_code(
                conn,
                email_key=key,
                purpose="register",
                code_digest=accounts.code_digest(code, key),
                max_attempts=accounts.code_max_attempts(),
                now=dt.datetime.now(dt.UTC).isoformat(),
            )
            if not ok:
                self._render_register("验证码错误或已过期。", email, verify_step=True)
                return
            db.activate_user(conn, email_key=key, now=dt.datetime.now(dt.UTC).isoformat())
        finally:
            conn.close()
        user = None
        conn = db.connect(self.server.db_path)
        try:
            user = db.user_by_email_key(conn, key)
        finally:
            conn.close()
        if user is None:
            self._render_register("账号不存在。", email)
            return
        self._render_login("邮箱验证成功，请使用密码登录。", email)

    def _handle_login(self, form: dict[str, str]) -> None:
        raw_email = form.get("email", "")
        try:
            throttle_key = accounts.email_key(accounts.normalize_email(raw_email))
        except accounts.AccountError:
            throttle_key = None
        if self._rate_limited("login", throttle_key):
            self._html(429, _page("稍候", "<p>操作过于频繁,请稍后再试。</p>"))
            return
        if not self._csrf_valid(form.get("csrf", "")):
            self._render_login("会话过期,请重试。")
            return
        try:
            email = accounts.normalize_email(raw_email)
        except accounts.AccountError as error:
            self._render_login(str(error), form.get("email", ""))
            return
        key = accounts.email_key(email)
        conn = db.connect(self.server.db_path)
        try:
            user = db.user_by_email_key(conn, key)
        finally:
            conn.close()
        # 恒定延迟:无论账号是否存在都只执行一次 pbkdf2 校验,防时序探测。
        stored = user.password_hash if user is not None else _LOGIN_DUMMY_PASSWORD_HASH
        password_ok = accounts.verify_password(form.get("password", ""), stored)
        if user is None or user.status != "active" or not password_ok:
            self._render_login("邮箱或密码不正确。", email)
            return
        session_token, user_cookie = self._issue_session(user.id)
        headers = [("Set-Cookie", user_cookie)]
        if user.is_admin:
            headers.append(
                (
                    "Set-Cookie",
                    self._set_cookie(_ADMIN_SESSION_COOKIE, session_token, _SESSION_TTL_SECONDS),
                )
            )
        else:
            headers.append(
                ("Set-Cookie", self._set_cookie(_ADMIN_SESSION_COOKIE, "", 0))
            )
        self._redirect("/", headers)

    def _handle_forgot(self, form: dict[str, str]) -> None:
        raw_email = form.get("email", "")
        try:
            throttle_key = accounts.email_key(accounts.normalize_email(raw_email))
        except accounts.AccountError:
            throttle_key = None
        if self._rate_limited("forgot", throttle_key):
            self._html(429, _page("稍候", "<p>操作过于频繁,请稍后再试。</p>"))
            return
        if not self._csrf_valid(form.get("csrf", "")):
            self._render_forgot("会话过期,请重试。")
            return
        try:
            email = accounts.normalize_email(raw_email)
        except accounts.AccountError:
            # 保持与不存在账号相同的响应,避免邮箱枚举。
            self._render_forgot("如果账号存在且邮件发送成功,请检查邮箱。", raw_email)
            return
        key = accounts.email_key(email)
        conn = db.connect(self.server.db_path)
        queued = False
        try:
            user = db.user_by_email_key(conn, key)
            if user is not None and user.status == "active":
                delivery_token = secrets.token_urlsafe(32)
                code = _account_verification_code(
                    self.server.session_secret, delivery_token, key, "reset"
                )
                db.issue_email_code_with_outbox(
                    conn,
                    email_key=key,
                    purpose="reset",
                    code_digest=accounts.code_digest(code, key),
                    delivery_token=delivery_token,
                    ttl_seconds=accounts.code_ttl_seconds(),
                    now=dt.datetime.now(dt.UTC).isoformat(),
                )
                queued = True
        finally:
            conn.close()
        if queued:
            self.server.wake_account_mail()
        self._render_forgot("如果账号存在且邮件发送成功,请检查邮箱。", email)

    def _handle_reset(self, form: dict[str, str]) -> None:
        raw_email = form.get("email", "")
        try:
            throttle_key = accounts.email_key(accounts.normalize_email(raw_email))
        except accounts.AccountError:
            throttle_key = None
        if self._rate_limited("reset", throttle_key):
            self._html(429, _page("稍候", "<p>操作过于频繁,请稍后再试。</p>"))
            return
        if not self._csrf_valid(form.get("csrf", "")):
            self._render_reset("会话过期,请重试。")
            return
        try:
            email = accounts.normalize_email(raw_email)
            password_hash = accounts.hash_password(form.get("password", ""))
        except accounts.AccountError as error:
            self._render_reset(str(error), raw_email, form.get("code", ""))
            return
        key = accounts.email_key(email)
        conn = db.connect(self.server.db_path)
        try:
            valid = db.consume_email_code(
                conn,
                email_key=key,
                purpose="reset",
                code_digest=accounts.code_digest(form.get("code", "").strip(), key),
                max_attempts=accounts.code_max_attempts(),
                now=dt.datetime.now(dt.UTC).isoformat(),
            )
            user = db.user_by_email_key(conn, key)
            if not valid or user is None or user.status != "active":
                self._render_reset("验证码错误或已过期。", email)
                return
            db.set_user_password_and_revoke_sessions(
                conn,
                user.id,
                password_hash=password_hash,
                now=dt.datetime.now(dt.UTC).isoformat(),
            )
        finally:
            conn.close()
        self._html(
            200,
            _page(
                "密码已重置",
                "<div class=\"msg\">密码已更新,请使用新密码登录。</div>"
                "<p><a href=\"/login\">返回登录</a></p>",
            ),
        )

    def _handle_logout(self, form: dict[str, str]) -> None:
        if not self._csrf_valid(form.get("csrf", "")):
            self._redirect("/account")
            return
        token = self._cookies().get(accounts.USER_COOKIE, "")
        if token:
            conn = db.connect(self.server.db_path)
            try:
                db.revoke_user_session(
                    conn,
                    token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    now=dt.datetime.now(dt.UTC).isoformat(),
                )
            finally:
                conn.close()
        self._redirect(
            "/",
            [
                ("Set-Cookie", self._set_cookie(accounts.USER_COOKIE, "", 0)),
                ("Set-Cookie", self._set_cookie(_ADMIN_SESSION_COOKIE, "", 0)),
            ],
        )

    def _require_session(self) -> SiteSession | None:
        session = self._session_user()
        if session is None:
            self._redirect("/login")
        return session

    def _handle_order(self, form: dict[str, str]) -> None:
        if not self._csrf_valid(form.get("csrf", "")):
            self._redirect("/account")
            return
        session = self._require_session()
        if session is None:
            return
        plan = form.get("plan", "")
        if plan not in accounts.PLANS:
            self._redirect("/account")
            return
        config = self.server.current_payment_config()
        if config is None:
            self._html(
                503,
                _page(
                    "支付暂不可用",
                    "<div class=\"msg\">在线支付暂不可用，请稍后再试。</div>"
                    "<p><a href=\"/account\">返回我的账户</a></p>",
                ),
            )
            return
        raw_payment_type = form.get("payment_type", "").strip().lower()
        if (
            raw_payment_type in {"alipay", "wxpay"}
            and raw_payment_type in config.enabled_types
        ):
            order_config = replace(config, payment_type=raw_payment_type)
        elif not raw_payment_type and config.payment_type in {"alipay", "wxpay"}:
            order_config = config
        else:
            order_config = replace(config, payment_type=config.primary_type)
        settings = self._load_settings()
        amount = accounts.price_cents(settings, plan)
        if amount <= 10:
            self._html(
                503,
                _page(
                    "支付暂不可用",
                    "<div class=\"msg\">当前方案价格暂不可支付，请联系管理员。</div>"
                    "<p><a href=\"/account\">返回我的账户</a></p>",
                ),
            )
            return
        conn = db.connect(self.server.db_path)
        try:
            try:
                order, created = db.reserve_payment_order(
                    conn,
                    user_id=session.user_id,
                    plan=plan,
                    base_amount_cents=amount,
                    merchant_order_no=payments.merchant_order_number(),
                    payment_type=order_config.payment_type,
                    payment_config_id=payments.config_identity(order_config),
                    now=dt.datetime.now(dt.UTC).isoformat(),
                    ttl_seconds=order_config.order_ttl_seconds,
                    amount_hold_seconds=order_config.amount_hold_seconds,
                )
            except RuntimeError:
                self._html(
                    503,
                    _page(
                        "订单繁忙",
                        "<div class=\"msg\">支付订单较多，请稍后重试。</div>"
                        "<p><a href=\"/account\">返回我的账户</a></p>",
                    ),
                )
                return
        finally:
            conn.close()
        recovering_waiting_without_url = (
            not created and order.last_error_code == "PAYMENT_WAITING_NO_URL"
        )
        allow_amount_reallocation = created
        if not created:
            if order.payment_type in {"alipay", "wxpay"}:
                order_config = replace(config, payment_type=order.payment_type)
            if order.plan != plan:
                self._html(
                    409,
                    _page(
                        "已有待支付订单",
                        "<div class=\"msg\">当前已有另一方案的待支付订单，"
                        "请先在账户页完成或等待其过期。</div>"
                        "<p><a href=\"/account\">查看现有订单</a></p>",
                    ),
                )
                return
            if order.status == "pending" and order.payment_url:
                self._redirect(
                    order.payment_url,
                    form_action_origin=payments.payment_origin(order_config),
                )
                return
            if not _payment_order_creation_retryable(order, dt.datetime.now(dt.UTC)):
                self._html(
                    409,
                    _page(
                        "订单待对账",
                        "<div class=\"msg\">现有订单仍在结算确认期，系统会先查询其最终状态。"
                        "请稍后回到账户页重试。</div>"
                        "<p><a href=\"/account\">查看订单状态</a></p>",
                    ),
                )
                return
            conn = db.connect(self.server.db_path)
            try:
                claimed = db.claim_payment_order_creation(
                    conn,
                    order_id=order.id,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                    checkout_ttl_seconds=order_config.order_ttl_seconds,
                )
            finally:
                conn.close()
            if claimed is None:
                self._html(
                    409,
                    _page(
                        "订单处理中",
                        "<div class=\"msg\">已有订单正在创建或等待对账，请稍后刷新账户页。</div>"
                        "<p><a href=\"/account\">返回我的账户</a></p>",
                    ),
                )
                return
            order = claimed
        subject = "Cheapcoding News " + ("monthly" if plan == "monthly" else "yearly")
        rejected_amounts: set[int] = set()
        creation = None
        creation_generation = order.updated_at
        while creation is None:
            try:
                creation = self.server.payment_create_callback(
                    order_config,
                    merchant_order_no=order.merchant_order_no,
                    amount_cents=order.amount_cents,
                    subject=subject,
                )
            except payments.PaymentError as error:
                if error.code != "AMOUNT_OCCUPIED" or not allow_amount_reallocation:
                    break
                rejected_amounts.add(order.amount_cents)
                conn = db.connect(self.server.db_path)
                try:
                    order = db.reallocate_payment_order_amount(
                        conn,
                        order_id=order.id,
                        rejected_amounts=rejected_amounts,
                        creation_generation=creation_generation,
                        now=dt.datetime.now(dt.UTC).isoformat(),
                    )
                    creation_generation = order.updated_at
                except RuntimeError:
                    break
                finally:
                    conn.close()
        if creation is None:
            conn = db.connect(self.server.db_path)
            try:
                try:
                    updated_order = db.record_payment_order_create_error(
                        conn,
                        order_id=order.id,
                        creation_generation=creation_generation,
                        now=dt.datetime.now(dt.UTC).isoformat(),
                        waiting_recovery=recovering_waiting_without_url,
                    )
                    if updated_order.status == "paid":
                        self._redirect("/account", status=HTTPStatus.SEE_OTHER)
                        return
                except RuntimeError as error:
                    if str(error) != "payment creation claim is stale":
                        raise
                    self._html(
                        409,
                        _page(
                            "订单处理中",
                            "<div class=\"msg\">订单已由另一请求接管，请稍后刷新账户页。</div>"
                            "<p><a href=\"/account\">返回我的账户</a></p>",
                        ),
                    )
                    return
            finally:
                conn.close()
            self._html(
                502,
                _page(
                    "支付网关暂不可用",
                    "<div class=\"msg\">订单创建失败，请稍后重新发起支付。</div>"
                    "<p><a href=\"/account\">返回我的账户</a></p>",
                ),
            )
            return
        conn = db.connect(self.server.db_path)
        try:
            try:
                updated_order = db.record_payment_order_created(
                    conn,
                    order_id=order.id,
                    provider_trade_no=creation.provider_trade_no,
                    payment_url=creation.payment_url,
                    creation_generation=creation_generation,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                )
                if updated_order.status == "paid":
                    self._redirect("/account", status=HTTPStatus.SEE_OTHER)
                    return
            except RuntimeError as error:
                if str(error) != "payment creation claim is stale":
                    raise
                self._html(
                    409,
                    _page(
                        "订单处理中",
                        "<div class=\"msg\">订单已由另一请求接管，请稍后刷新账户页。</div>"
                        "<p><a href=\"/account\">返回我的账户</a></p>",
                    ),
                )
                return
        finally:
            conn.close()
        self._redirect(
            creation.payment_url,
            form_action_origin=payments.payment_origin(order_config),
        )

    def _handle_order_cancel(self, form: dict[str, str]) -> None:
        if not self._csrf_valid(form.get("csrf", "")):
            self._redirect("/account")
            return
        session = self._require_session()
        if session is None:
            return
        try:
            order_id = int(form.get("order_id", "0"))
        except ValueError:
            order_id = 0
        if order_id <= 0:
            self._redirect("/account")
            return
        now = dt.datetime.now(dt.UTC).isoformat()
        conn = db.connect(self.server.db_path)
        try:
            db.cancel_user_payment_order(
                conn, order_id=order_id, user_id=session.user_id, now=now
            )
        finally:
            conn.close()
        self._redirect("/account")

    def _settlement_config_for_order(self, order: db.Order) -> EpayConfig:
        config = self.server.settlement_payment_config()
        if config is None:
            raise payments.PaymentError("payment settlement configuration is unavailable")
        if order.payment_type in {"alipay", "wxpay"}:
            config = replace(config, payment_type=order.payment_type)
        if order.payment_config_id is not None and (
            payments.config_identity(config) != order.payment_config_id
        ):
            raise payments.PaymentError("payment settlement configuration does not match")
        if order.payment_type is not None and config.payment_type != order.payment_type:
            raise payments.PaymentError("payment type does not match order")
        return config

    def _settle_payment(
        self,
        notification: payments.PaymentNotification,
        config: EpayConfig,
    ) -> db.Order:
        conn = db.connect(self.server.db_path)
        try:
            return db.confirm_payment_order(
                conn,
                merchant_order_no=notification.merchant_order_no,
                provider_trade_no=notification.provider_trade_no,
                amount_cents=notification.amount_cents,
                now=dt.datetime.now(dt.UTC).isoformat(),
                amount_hold_seconds=config.amount_hold_seconds,
                plan_days=accounts.PLAN_DAYS,
            )
        finally:
            conn.close()

    def _confirm_payment(self, fields: dict[str, str]) -> db.Order:
        merchant_order_no = fields.get("out_trade_no", "").strip()
        conn = db.connect(self.server.db_path)
        try:
            order = db.order_by_merchant_order_no(conn, merchant_order_no)
        finally:
            conn.close()
        if order is None:
            raise payments.PaymentError("payment order does not exist")
        config = self._settlement_config_for_order(order)
        notification = payments.parse_notification(config, fields)
        return self._settle_payment(notification, config)

    def _reconcile_payment_order(self, order: db.Order) -> db.Order:
        if not order.merchant_order_no:
            raise payments.PaymentError("payment order is not reconcilable")
        config = self._settlement_config_for_order(order)
        result = self.server.payment_query_callback(
            config,
            merchant_order_no=order.merchant_order_no,
            expected_amount_cents=order.amount_cents,
        )
        if result.trade_status == "TRADE_SUCCESS":
            return self._settle_payment(
                payments.PaymentNotification(
                    merchant_order_no=result.merchant_order_no,
                    provider_trade_no=result.provider_trade_no,
                    amount_cents=result.amount_cents,
                ),
                config,
            )
        conn = db.connect(self.server.db_path)
        try:
            return db.record_payment_query_status(
                conn,
                order_id=order.id,
                trade_status=result.trade_status,
                expected_updated_at=order.updated_at,
                now=dt.datetime.now(dt.UTC).isoformat(),
            )
        finally:
            conn.close()

    def _handle_payment_notify(self, fields: dict[str, str]) -> None:
        try:
            self._confirm_payment(fields)
        except (payments.PaymentError, RuntimeError, ValueError):
            self.log_message("payment notification rejected")
            self._text(400, "fail")
            return
        self._text(200, "success")

    def _handle_payment_return(self, fields: dict[str, str]) -> None:
        try:
            order = self._confirm_payment(fields)
        except (payments.PaymentError, RuntimeError, ValueError):
            self._html(
                400,
                _page(
                    "支付结果待确认",
                    "<div class=\"msg\">暂未确认支付结果，请稍后在账户页查看订单状态。</div>"
                    "<p><a href=\"/account\">查看我的订单</a></p>",
                ),
            )
            return
        self._html(
            200,
            _page(
                "支付成功",
                f"<div class=\"msg\">订单 {html.escape(order.merchant_order_no or '')} "
                "已支付，会员已自动开通。</div><p><a href=\"/account\">返回我的账户</a></p>",
            ),
        )

    def _handle_redeem(self, form: dict[str, str]) -> None:
        if self._rate_limited("redeem"):
            self._html(429, _page("稍候", "<p>操作过于频繁,请稍后再试。</p>"))
            return
        if not self._csrf_valid(form.get("csrf", "")):
            self._redirect("/subscribe")
            return
        session = self._require_session()
        if session is None:
            return
        code = form.get("code", "")
        conn = db.connect(self.server.db_path)
        try:
            try:
                db.redeem_code(
                    conn,
                    code_digest=accounts.redemption_digest(code),
                    user_id=session.user_id,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                    plan_days=accounts.PLAN_DAYS,
                )
            except RuntimeError:
                self._html(
                    200,
                    _page(
                        "兑换失败",
                        "<div class=\"msg\">卡密无效或已被使用。</div>"
                        "<p><a href=\"/subscribe\">返回会员订阅</a> · "
                        "<a href=\"/account\">前往我的账户</a></p>",
                    ),
                )
                return
        finally:
            conn.close()
        self._redirect("/subscribe?redeemed=1", status=HTTPStatus.SEE_OTHER)


class SiteServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        *,
        site_dir: Path,
        db_path: Path,
        session_secret: bytes,
        secure_cookies: bool,
        scheme: str,
        site_url: str,
        loopback_browser_compat: bool,
        payment_config: EpayConfig | None,
        payment_config_loader: Callable[[], EpayConfig | None] | None,
        payment_settlement_config_loader: Callable[[], EpayConfig | None] | None,
        payment_create_callback: Callable[..., payments.PaymentCreation],
        payment_query_callback: Callable[..., payments.PaymentQuery],
        code_sender: Callable[[str, str, db.EmailCodePurpose], None] | None,
        admin_notify: Callable[[], None],
        log_callback: Callable[[str], None] | None,
        port: int,
    ) -> None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        super().__init__(("127.0.0.1", port), SiteHandler)
        self.site_dir = site_dir
        self.db_path = db_path
        self.session_secret = session_secret
        self.secure_cookies = secure_cookies
        self.scheme = scheme
        self.local_origin = scheme == "http" and not secure_cookies
        self.expected_origin = (
            (scheme, "127.0.0.1", self.server_address[1])
            if self.local_origin
            else _origin_identity(site_url)
        )
        if self.expected_origin[0] != scheme:
            raise ValueError("site URL scheme does not match the public server scheme")
        self.loopback_browser_compat = loopback_browser_compat
        self.payment_config = payment_config
        self.payment_config_loader = payment_config_loader
        self.payment_settlement_config_loader = payment_settlement_config_loader
        self.payment_create_callback = payment_create_callback
        self.payment_query_callback = payment_query_callback
        self.code_sender = code_sender
        self.admin_notify = admin_notify
        self.log_callback = log_callback
        self.limiter = RateLimiter()
        self.captcha_lock = threading.Lock()
        self.captchas: dict[str, tuple[str, float]] = {}
        self.account_mail_stop = threading.Event()
        self.account_mail_wakeup = threading.Event()
        self.account_mail_condition = threading.Condition()
        self.account_mail_workers: list[threading.Thread] = []
        if self.code_sender is not None:
            conn = db.connect(self.db_path)
            conn.close()
            for index in range(_ACCOUNT_MAIL_WORKER_COUNT):
                worker = threading.Thread(
                    target=self._account_mail_loop,
                    name=f"account-mail-{index + 1}",
                    daemon=True,
                )
                worker.start()
                self.account_mail_workers.append(worker)

    def _account_mail_connection(self) -> sqlite3.Connection | None:
        while not self.account_mail_stop.is_set():
            try:
                return db.connect(self.db_path)
            except sqlite3.OperationalError:
                self.account_mail_stop.wait(0.05)
        return None

    def _account_mail_loop(self) -> None:
        while not self.account_mail_stop.is_set():
            conn = self._account_mail_connection()
            if conn is None:
                return
            try:
                mail = db.claim_account_mail(
                    conn,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                    lease_seconds=_ACCOUNT_MAIL_LEASE_SECONDS,
                )
            finally:
                conn.close()
            if mail is None:
                self.account_mail_wakeup.wait(0.5)
                self.account_mail_wakeup.clear()
                continue
            code = _account_verification_code(
                self.session_secret,
                mail.delivery_token,
                mail.email_key,
                mail.purpose,
            )
            try:
                self.code_sender(mail.email, code, mail.purpose)
            except Exception:  # noqa: BLE001 - durable outbox controls bounded retries
                conn = self._account_mail_connection()
                if conn is None:
                    return
                try:
                    db.release_account_mail(
                        conn,
                        outbox_id=mail.id,
                        now=dt.datetime.now(dt.UTC).isoformat(),
                        retry_seconds=_ACCOUNT_MAIL_RETRY_SECONDS,
                        max_attempts=_ACCOUNT_MAIL_MAX_ATTEMPTS,
                        error_code="DELIVERY_FAILED",
                    )
                finally:
                    conn.close()
            else:
                conn = self._account_mail_connection()
                if conn is None:
                    return
                try:
                    db.complete_account_mail(
                        conn,
                        outbox_id=mail.id,
                        now=dt.datetime.now(dt.UTC).isoformat(),
                    )
                finally:
                    conn.close()
            with self.account_mail_condition:
                self.account_mail_condition.notify_all()

    def wake_account_mail(self) -> None:
        self.account_mail_wakeup.set()

    def wait_for_account_mail_idle(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.account_mail_condition:
            while True:
                conn = db.connect(self.db_path)
                try:
                    ready = db.account_mail_ready(
                        conn, now=dt.datetime.now(dt.UTC).isoformat()
                    )
                finally:
                    conn.close()
                if not ready:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.account_mail_condition.wait(min(remaining, 0.05))

    def server_close(self) -> None:
        self.account_mail_stop.set()
        self.account_mail_wakeup.set()
        super().server_close()
        for worker in self.account_mail_workers:
            worker.join(timeout=0.2)

    def current_payment_config(self) -> EpayConfig | None:
        if self.payment_config_loader is None:
            return self.payment_config
        try:
            return self.payment_config_loader()
        except payments.PaymentError:
            if self.log_callback is not None:
                self.log_callback("payment configuration is invalid")
            return None

    def settlement_payment_config(self) -> EpayConfig | None:
        loader = self.payment_settlement_config_loader
        if loader is None:
            current = self.current_payment_config()
            return current if current is not None else self.payment_config
        try:
            return loader()
        except payments.PaymentError:
            if self.log_callback is not None:
                self.log_callback("payment settlement configuration is invalid")
            return None


def create_site_server(
    *,
    site_dir: Path,
    db_path: Path,
    secret_file: Path,
    port: int = 8620,
    secure_cookies: bool = True,
    scheme: str = "https",
    site_url: str = "",
    loopback_browser_compat: bool = False,
    payment_config: EpayConfig | None = None,
    payment_config_loader: Callable[[], EpayConfig | None] | None = None,
    payment_settlement_config_loader: Callable[[], EpayConfig | None] | None = None,
    payment_create_callback: Callable[..., payments.PaymentCreation] = payments.create_payment,
    payment_query_callback: Callable[..., payments.PaymentQuery] = payments.query_payment,
    code_sender: Callable[[str, str, db.EmailCodePurpose], None] | None = None,
    admin_notify: Callable[[], None] = lambda: None,
    log_callback: Callable[[str], None] | None = None,
) -> SiteServer:
    """构建站点服务;绑定 127.0.0.1,由宿主 Nginx 反代并终止 TLS。"""
    if loopback_browser_compat and (secure_cookies or scheme != "http"):
        raise ValueError("loopback browser compatibility requires insecure HTTP cookies")
    return SiteServer(
        site_dir=site_dir,
        db_path=db_path,
        session_secret=load_site_secret(secret_file),
        secure_cookies=secure_cookies,
        scheme=scheme,
        site_url=site_url,
        loopback_browser_compat=loopback_browser_compat,
        payment_config=payment_config,
        payment_config_loader=payment_config_loader,
        payment_settlement_config_loader=payment_settlement_config_loader,
        payment_create_callback=payment_create_callback,
        payment_query_callback=payment_query_callback,
        code_sender=code_sender,
        admin_notify=admin_notify,
        log_callback=log_callback,
        port=port,
    )
