"""账号与付费订阅域逻辑:口令哈希、邮箱验证码、付费判定、卡密格式与免费额度决策。

本模块不含 SQL(全项目 SQL 仅在 storage/db);负责哈希、摘要、格式与纯函数决策,
由 site_server 与 Admin 层组合 db 函数调用。
"""

import datetime as dt
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

PLAN_DAYS = {"monthly": 31, "yearly": 366}
PLANS = ("monthly", "yearly")

DEFAULT_SETTINGS = {
    "paywall_enabled": "false",
    "monthly_price_cents": "990",
    "yearly_price_cents": "9900",
    "monthly_list_price_cents": "",
    "yearly_list_price_cents": "",
    "monthly_discount_percent": "0",
    "yearly_discount_percent": "0",
    "payment_info": "",
    "payment_qr_data_url": "",
}

_PBKDF2_ITERATIONS = 120_000
_CODE_TTL_SECONDS = 10 * 60
_CODE_MAX_ATTEMPTS = 5
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # 去除易混淆字符


class AccountError(RuntimeError):
    """带用户可读文案的账号操作失败。"""


# --- 邮箱 ------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not email.isascii() or not _EMAIL_RE.match(email) or len(email) > 254:
        raise AccountError("邮箱格式不正确")
    return email


def email_key(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


# --- 口令哈希(pbkdf2,不使用 Admin 面板的 apr1) -----------------------------


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise AccountError("密码至少 8 位")
    if len(password) > 128:
        raise AccountError("密码过长")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# --- 邮箱验证码 --------------------------------------------------------------


def generate_verification_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def code_digest(code: str, key: str) -> str:
    return hashlib.sha256(f"{code}:{key}".encode()).hexdigest()


def code_ttl_seconds() -> int:
    return _CODE_TTL_SECONDS


def code_max_attempts() -> int:
    return _CODE_MAX_ATTEMPTS


# --- 付费权益 ----------------------------------------------------------------


def parse_paid_until(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def is_paid(paid_until: str | None, now: dt.datetime) -> bool:
    expiry = parse_paid_until(paid_until)
    return expiry is not None and expiry > now


def paid_until_from(base: dt.datetime, plan: str) -> str:
    if plan not in PLAN_DAYS:
        raise AccountError("未知的订阅计划")
    return (base + dt.timedelta(days=PLAN_DAYS[plan])).isoformat()


def base_price_cents(settings: dict[str, str], plan: str) -> int:
    price_key = f"{plan}_price_cents"
    list_key = f"{plan}_list_price_cents"
    raw_list = settings.get(list_key, "").strip()
    key = list_key if raw_list else price_key
    try:
        return max(0, int(settings.get(key, DEFAULT_SETTINGS.get(key, "0"))))
    except ValueError:
        return 0


def discount_percent(settings: dict[str, str], plan: str) -> int:
    key = f"{plan}_discount_percent"
    try:
        return max(0, min(100, int(settings.get(key, DEFAULT_SETTINGS.get(key, "0")))))
    except ValueError:
        return 0


def price_cents(settings: dict[str, str], plan: str) -> int:
    base = base_price_cents(settings, plan)
    list_key = f"{plan}_list_price_cents"
    if settings.get(list_key, "").strip():
        price_key = f"{plan}_price_cents"
        try:
            current = int(settings.get(price_key, DEFAULT_SETTINGS.get(price_key, "0")))
        except ValueError:
            return 0
        return current if 0 <= current <= base else 0
    return base * (100 - discount_percent(settings, plan)) // 100


def discount_basis_points(settings: dict[str, str], plan: str) -> int:
    """Return the displayed price reduction in hundredths of one percent."""
    base = base_price_cents(settings, plan)
    current = price_cents(settings, plan)
    if base <= 0 or current >= base:
        return 0
    return (base - current) * 10_000 // base


def discount_label(settings: dict[str, str], plan: str) -> str:
    basis_points = discount_basis_points(settings, plan)
    whole, fraction = divmod(basis_points, 100)
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:02d}".rstrip("0")


def format_cents(value: int) -> str:
    whole, fraction = divmod(max(0, value), 100)
    if fraction == 0:
        return str(whole)
    if fraction % 10 == 0:
        return f"{whole}.{fraction // 10}"
    return f"{whole}.{fraction:02d}"


# --- 卡密 --------------------------------------------------------------------

_CODE_SEGMENTS = (4, 4)


def generate_redemption_code() -> str:
    segments = [
        "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
        for length in _CODE_SEGMENTS
    ]
    return "-".join(segments)


def redemption_digest(code: str) -> str:
    normalized = code.strip().upper()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def redemption_prefix(code: str) -> str:
    return code.strip().upper()[:8]


# --- 站点设置 ----------------------------------------------------------------


def load_settings(get_setting) -> dict[str, str]:
    """get_setting 为 db.get_setting 的可注入引用,便于测试。"""
    return {
        key: (get_setting(key) if get_setting(key) is not None else default)
        for key, default in DEFAULT_SETTINGS.items()
    }


def paywall_enabled(settings: dict[str, str]) -> bool:
    return settings.get("paywall_enabled", "false") == "true"

# --- 付费墙门控决策(纯函数) ---------------------------------------------------

ANON_COOKIE = "nd_anon"
USER_COOKIE = "nd_user_session"

ARTICLE_PATH_RE = re.compile(r"^/issues/(\d{4}-\d{2}-\d{2})/([a-z0-9-]+)\.html$")


@dataclass
class AccessDecision:
    outcome: str  # "allow" | "free_read" | "paywall" | "public"
    existing_path: str | None = None


def classify_page(path: str) -> tuple[str, str | None, str | None]:
    """把站点路径分类为 (类型, 刊期日期, slug)。

    index/归档列表/资产为 public;文章页为 article(用于免费额度计量)。
    """
    match = ARTICLE_PATH_RE.match(path)
    if match:
        return "article", match.group(1), match.group(2)
    return "public", None, None


def decide_access(
    *,
    paywall_on: bool,
    user_is_paid: bool,
    requested_path: str,
    edition_date: str,
    latest_edition_date: str | None,
    known_main_slug: bool,
    free_read_path: str | None,
) -> str:
    """付费墙决策矩阵(见 PLAN §12C.2):

    - 开关关闭 → 全站免费(等同现状);
    - 付费用户 → 全部放行;
    - 请求的正是当日已占坑的那篇文章 → 放行(允许回看);
    - 最新刊期主文章且当日尚未占坑 → 走免费额度认领;
    - 其余(归档/当日其他主文章)→ 付费墙插页。
    """
    if not paywall_on or user_is_paid:
        return "allow"
    if edition_date is None:
        return "public"
    if latest_edition_date is not None and edition_date == latest_edition_date:
        if free_read_path is not None:
            if free_read_path == requested_path:
                return "allow"
            return "paywall_quota"
        if known_main_slug:
            return "free_read"
        return "paywall"
    return "paywall"
