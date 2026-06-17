"""Per-user settings, persisted to config/user_settings.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_SETTINGS_FILE = ROOT / "config" / "user_settings.json"


def _load() -> dict[str, Any]:
    if _SETTINGS_FILE.is_file():
        try:
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save(data: dict[str, Any]) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_user_settings(user_id: int) -> dict[str, Any]:
    return _load().get(str(user_id), {})


def update_user_settings(user_id: int, **kwargs: Any) -> dict[str, Any]:
    data = _load()
    uid = str(user_id)
    current = data.get(uid, {})
    current.update(kwargs)
    data[uid] = current
    _save(data)
    return current
