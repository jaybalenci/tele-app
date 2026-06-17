"""Thread-safe account pool — one account per concurrent price check."""

from __future__ import annotations

import random
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import os

from core.logger import log
from doordash.web_client import load_account_pool as _load, parse_cookie_string

_lock = threading.Lock()
_accounts: list[dict[str, str]] = []
_in_use: set[int] = set()

_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_DIR = _ROOT / "config" / "accounts"
_CF_PATH = _ROOT / "config" / "cf_clearance.txt"


def _load_from_env() -> list[dict[str, str]]:
    """Load account cookies from ACCOUNT_1_COOKIES, ACCOUNT_2_COOKIES, ... env vars."""
    cf = os.getenv("CF_CLEARANCE", "").strip()
    accounts = []
    i = 1
    while True:
        raw = os.getenv(f"ACCOUNT_{i}_COOKIES", "").strip()
        if not raw:
            break
        cookies = parse_cookie_string(raw)
        if cookies:
            if cf:
                cookies["cf_clearance"] = cf
            accounts.append(cookies)
        i += 1
    return accounts


def _ensure_loaded() -> None:
    global _accounts
    if not _accounts:
        # Prefer env vars (Railway / cloud), fall back to local files
        env_accounts = _load_from_env()
        if env_accounts:
            log("pool", f"loaded {len(env_accounts)} account(s) from environment variables")
            _accounts = env_accounts
        else:
            _accounts = _load(_ACCOUNTS_DIR, _CF_PATH)


def account_count() -> int:
    with _lock:
        _ensure_loaded()
        return len(_accounts)


@contextmanager
def acquire(
    *,
    exclude: frozenset[int] = frozenset(),
    force_idx: int | None = None,
) -> Generator[tuple[int, dict[str, str]], None, None]:
    """Claim one free account for the duration of a price check, then release it.

    Yields (index, cookies). Pass already-tried indices via *exclude* to skip them.
    Pass *force_idx* to pin a specific account (used by the account comparison tool).
    """
    with _lock:
        _ensure_loaded()
        if force_idx is not None:
            idx = force_idx
        else:
            free = [i for i in range(len(_accounts)) if i not in _in_use and i not in exclude]
            if not free:
                if exclude and len(exclude) >= len(_accounts):
                    raise RuntimeError(
                        "All accounts are Cloudflare-blocked (HTTP 403). "
                        "Paste a fresh cf_clearance value into config/cf_clearance.txt."
                    )
                raise RuntimeError(
                    f"All {len(_accounts)} accounts are currently busy — try again in a moment."
                )
            idx = random.choice(free)
        _in_use.add(idx)

    log("pool", f"account {idx + 1}/{len(_accounts)}")
    try:
        yield idx, _accounts[idx]
    finally:
        with _lock:
            _in_use.discard(idx)
        log("pool", f"released {idx + 1}")
