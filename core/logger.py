"""Timestamped logger with clean / verbose toggle.

Set VERBOSE_DEBUG=1 in .env for full detail (raw JSON, option payloads, attempt
indices, etc.). Default is clean mode — concise human-readable lines only.
"""
from __future__ import annotations

import os
from datetime import datetime

VERBOSE: bool = os.getenv("VERBOSE_DEBUG", "0").strip() != "0"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(tag: str, msg: str) -> None:
    """Always-visible timestamped log line."""
    print(f"[{_ts()}] [{tag}] {msg}")


def vlog(tag: str, msg: str) -> None:
    """Verbose-only — suppressed unless VERBOSE_DEBUG=1."""
    if VERBOSE:
        print(f"[{_ts()}] [{tag}] {msg}")
