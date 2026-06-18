import re
import sys
import os
import time as _time
import hmac
import hashlib
import threading
from collections import defaultdict
from urllib.parse import parse_qsl, urlparse

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="webapp", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KB max body

# ── Link/order cache ──────────────────────────────────────────────────────────
_link_cache: dict = {}
_LINK_CACHE_TTL = 30  # 30 seconds — carts change frequently


# ── Rate limiter (sliding window, in-memory per IP) ───────────────────────────
_rate_lock = threading.Lock()
_rate_windows: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(key: str, max_req: int, window: int) -> bool:
    """Return True if allowed, False if rate-limited."""
    now = _time.time()
    with _rate_lock:
        ts = _rate_windows[key]
        _rate_windows[key] = [t for t in ts if now - t < window]
        if len(_rate_windows[key]) >= max_req:
            return False
        _rate_windows[key].append(now)
        return True


def _client_key(prefix: str) -> str:
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    return f"{prefix}:{ip}"


# ── Telegram initData validation ──────────────────────────────────────────────
_BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_DISABLE_AUTH = os.getenv("DISABLE_AUTH", "0").strip() == "1"
_MAX_INIT_DATA_AGE = 3600  # 1 hour


def _get_chat_id_from_init_data(init_data: str) -> int | None:
    """Extract the Telegram user ID (= DM chat_id) from validated initData."""
    try:
        vals = dict(parse_qsl(init_data, strict_parsing=False))
        user_json = vals.get("user") or ""
        if not user_json:
            return None
        import json as _j
        return int(_j.loads(user_json).get("id") or 0) or None
    except Exception:
        return None


def _verify_telegram_init_data(init_data: str) -> bool:
    """Validate Telegram WebApp initData HMAC so only real Telegram users can call the API."""
    if _DISABLE_AUTH:
        return True
    if not init_data or not _BOT_TOKEN:
        return False
    try:
        vals = dict(parse_qsl(init_data, strict_parsing=True))
        check_hash = vals.pop("hash", None)
        if not check_hash:
            return False
        auth_date = int(vals.get("auth_date", 0))
        if _time.time() - auth_date > _MAX_INIT_DATA_AGE:
            return False
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
        secret = hmac.new(b"WebAppData", _BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, check_hash)
    except Exception:
        return False


# ── Input validation ──────────────────────────────────────────────────────────
_ALLOWED_LINK_HOSTS = {"doordash.com", "www.doordash.com", "drd.sh"}
_TIP_RE = re.compile(r"^(none|0%?|\d{1,3}%|\$?\d{1,4}(\.\d{1,2})?)$", re.IGNORECASE)


def _validate_order_link(link: str) -> bool:
    if not link or len(link) > 300:
        return False
    try:
        p = urlparse(link)
        host = p.netloc.lower()
        if p.scheme not in ("https", "http") or host not in _ALLOWED_LINK_HOSTS:
            return False
        # drd.sh short-links must be cart links
        if host == "drd.sh" and not p.path.startswith("/cart/"):
            return False
        return True
    except Exception:
        return False


def _validate_tip(tip: str) -> bool:
    return bool(_TIP_RE.match(tip.strip())) if tip else True


# ── Security headers ──────────────────────────────────────────────────────────
@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://telegram.org; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return resp


# ── Helpers ───────────────────────────────────────────────────────────────────
def _clean_address(addr: str) -> str:
    """'121 Hillview Ave, Waterbury, Connecticut 06704, United States' → '121 Hillview Ave 06704'"""
    parts = [p.strip() for p in addr.split(",")]
    street = parts[0] if parts else addr
    zip_code = ""
    for part in parts[1:]:
        m = re.search(r"\b(\d{5})\b", part)
        if m:
            zip_code = m.group(1)
            break
    return f"{street} {zip_code}".strip() if zip_code else street


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("webapp", "index.html")


@app.route("/imgs/<path:filename>")
def serve_imgs(filename):
    return send_from_directory("imgs", filename)


@app.route("/api/address-autocomplete")
def address_autocomplete():
    if not _check_rate_limit(_client_key("autocomplete"), 30, 60):
        return jsonify([]), 429
    q = (request.args.get("q") or "").strip()
    if len(q) < 3 or len(q) > 200:
        return jsonify([])
    from doordash.address_autocomplete import fetch_address_suggestions
    return jsonify(fetch_address_suggestions(q))


@app.route("/api/validate-link")
def validate_link():
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not _verify_telegram_init_data(init_data):
        return jsonify({"error": "Unauthorized"}), 401

    if not _check_rate_limit(_client_key("validate"), 10, 60):
        return jsonify({"error": "Too many requests — slow down."}), 429

    link = (request.args.get("link") or "").strip()
    if not _validate_order_link(link):
        return jsonify({"error": "Invalid link"}), 400

    cached = _link_cache.get(link)
    if cached and (_time.time() - cached["ts"]) < _LINK_CACHE_TTL:
        return jsonify(cached["result"])

    try:
        from core.account_pool import acquire
        from doordash.group_order import join_group_order

        with acquire() as (_, cookies):
            _, __, source_cart = join_group_order(cookies, link)

        subtotal_cents = int(source_cart.get("subtotal") or 0)
        subtotal = subtotal_cents / 100
        store = (source_cart.get("restaurant") or {}).get("name") or ""
        eligible = 15.0 <= subtotal <= 25.0

        result = {
            "store": store,
            "subtotal": subtotal,
            "subtotal_display": f"${subtotal:.2f}",
            "eligible": eligible,
        }
        _link_cache[link] = {"result": result, "source_cart": source_cart, "ts": _time.time()}
        return jsonify(result)

    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        from core.logger import log
        import traceback
        log("validate-link", f"unexpected: {traceback.format_exc()}")
        return jsonify({"error": f"Could not load order: {e}"}), 400


@app.route("/api/price", methods=["POST"])
def price():
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not _verify_telegram_init_data(init_data):
        return jsonify({"error": "Unauthorized"}), 401

    if not _check_rate_limit(_client_key("price"), 5, 60):
        return jsonify({"error": "Too many requests — wait a moment and try again."}), 429

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Invalid request"}), 400

    link    = str(body.get("link") or "").strip()[:300]
    address = str(body.get("address") or "").strip()[:300]
    apt     = str(body.get("apt") or "").strip()[:50]
    gate    = str(body.get("gate") or "").strip()[:50]
    tip     = str(body.get("tip") or "none").strip()[:20]

    if not _validate_order_link(link):
        return jsonify({"error": "Invalid link"}), 400
    if not address:
        return jsonify({"error": "Address is required"}), 400
    if not _validate_tip(tip):
        tip = "none"

    address = _clean_address(address)
    if apt:
        address = f"{address}, {apt}"

    try:
        from core.price_order import run_price_order

        prefetched = None
        cached = _link_cache.get(link)
        if cached and (_time.time() - cached["ts"]) < _LINK_CACHE_TTL:
            prefetched = cached["source_cart"]

        result = run_price_order(
            link,
            address,
            on_status=lambda msg: None,
            prefetched_source_cart=prefetched,
            tip_str=tip,
        )

        if result.cleanup_fn:
            result.cleanup_fn()

        checkout_url = (
            f"https://www.doordash.com/consumer/checkout/?order_cart_id={result.cart_id}"
        )

        return jsonify({
            "store":        result.store,
            "address":      result.address,
            "items":        result.items_detail,
            "pricing": {
                "subtotal":        result.pricing.subtotal_display,
                "fees_tax":        result.pricing.fees_tax_display,
                "delivery":        result.pricing.delivery_fee_display,
                "discounts":       result.pricing.discounts_display,
                "tip":             result.pricing.tip_display,
                "total":           result.pricing.total_display,
                "original_total":  result.pricing.original_total_display,
            },
            "tip_str":      tip,
            "failures":     result.failures,
            "checkout_url": checkout_url,
        })

    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        from core.logger import log
        import traceback
        log("price", f"unexpected: {traceback.format_exc()}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500


@app.route("/api/deposit", methods=["POST"])
def deposit():
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not _verify_telegram_init_data(init_data):
        return jsonify({"error": "Unauthorized"}), 401

    if not _check_rate_limit(_client_key("deposit"), 5, 60):
        return jsonify({"error": "Too many requests."}), 429

    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount") or 0)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid amount."}), 400

    if amount <= 0 or amount > 100:
        return jsonify({"error": "Amount must be between $0.01 and $100.00."}), 400

    chat_id = _get_chat_id_from_init_data(init_data)
    if not chat_id and not _DISABLE_AUTH:
        return jsonify({"error": "Could not identify user."}), 400

    if chat_id:
        _tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": f"Your deposit of ${amount:.2f} has been completed.",
        })

    return jsonify({"ok": True})


# ── Telegram bot (webhook mode) ───────────────────────────────────────────────
import json as _json
import urllib.request as _urllib_request

_WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
_WEBHOOK_SECRET = (
    hmac.new(b"webhook", _BOT_TOKEN.encode(), hashlib.sha256).hexdigest()[:32]
    if _BOT_TOKEN else ""
)


def _tg_api(method: str, payload: dict) -> None:
    if not _BOT_TOKEN:
        return
    try:
        req = _urllib_request.Request(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/{method}",
            data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        _urllib_request.urlopen(req, timeout=10)
    except Exception:
        pass


def _register_webhook() -> None:
    if not _BOT_TOKEN or not _WEBAPP_URL:
        return
    webhook_url = f"{_WEBAPP_URL}/telegram"
    try:
        req = _urllib_request.Request(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/setWebhook",
            data=_json.dumps({
                "url": webhook_url,
                "secret_token": _WEBHOOK_SECRET,
                "allowed_updates": ["message"],
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        _urllib_request.urlopen(req, timeout=10)
        from core.logger import log as _log
        _log("bot", f"webhook registered → {webhook_url}")
    except Exception as exc:
        from core.logger import log as _log
        _log("bot", f"webhook registration failed: {exc}")


_register_webhook()


@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    if _WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != _WEBHOOK_SECRET:
        return "", 403

    data = request.get_json(force=True, silent=True) or {}
    message = data.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")

    if not chat_id:
        return "ok"

    if text.startswith("/start"):
        _tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": "Welcome to Crave! Tap the button below to open the app.",
            "reply_markup": {
                "inline_keyboard": [[{
                    "text": "Open Crave",
                    "web_app": {"url": f"{_WEBAPP_URL}/"}
                }]]
            },
        })
    elif text.startswith("/help"):
        _tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": "/start — Open the app\n/help — Show this message",
        })

    return "ok"


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0").strip() == "1"
    app.run(port=3000, debug=debug)
