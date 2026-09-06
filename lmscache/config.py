"""Paths and persistent settings. Settings live in the config volume, never on the shared folder."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

MODELS_ROOT = Path(os.environ.get("LMSCACHE_MODELS", "/models"))
CONFIG_DIR = Path(os.environ.get("LMSCACHE_CONFIG", "/config"))
LIBRARY_DIR = MODELS_ROOT / "lmstudio"
INCOMING_DIR = MODELS_ROOT / ".incoming"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DB_FILE = CONFIG_DIR / "lmscache.sqlite"
HF_HOME = CONFIG_DIR / "hf-home"

DEFAULTS: dict = {
    "hf_token": "",
    "preferred_quant": "Q4_K_M",
    "max_parallel": 1,
    "xet_high_performance": True,
    "verify_checksums": False,
    "append_report": True,
    "public_url": "",
    "smb_host": "",
    "smb_share": "models",
    "smb_user": "guest",
    "smb_password": "",
}

_lock = threading.Lock()
_settings: dict | None = None


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, LIBRARY_DIR, INCOMING_DIR, HF_HOME):
        d.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    global _settings
    with _lock:
        if _settings is None:
            data = {}
            if SETTINGS_FILE.exists():
                try:
                    data = json.loads(SETTINGS_FILE.read_text())
                except Exception:
                    data = {}
            _settings = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
        return dict(_settings)


def save(update: dict) -> dict:
    global _settings
    with _lock:
        current = dict(_settings or DEFAULTS)
        for k, v in update.items():
            if k in DEFAULTS:
                current[k] = v
        current["max_parallel"] = max(1, min(4, int(current.get("max_parallel") or 1)))
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, SETTINGS_FILE)
        _settings = current
        return dict(current)


def public(settings: dict) -> dict:
    """Settings safe to send to the browser."""
    out = {k: v for k, v in settings.items() if k not in ("hf_token", "smb_password")}
    out["has_token"] = bool(settings.get("hf_token"))
    out["has_smb_password"] = bool(settings.get("smb_password"))
    tok = settings.get("hf_token") or ""
    out["hf_token_hint"] = (tok[:7] + "…" + tok[-4:]) if len(tok) > 14 else ("•" * len(tok))
    return out
