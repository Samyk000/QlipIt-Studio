"""Per-user notification and interface settings.

The files stay under users/<user>/accounts so existing alert and tour preferences survive upgrades.
"""
import json
import threading
from pathlib import Path

from . import config, ctx

_lock = threading.RLock()
SECRET_KEYS = ("discord_webhook", "telegram_token")
SETTING_DEFAULTS = {"discord_webhook": "", "telegram_token": "", "telegram_chat": "",
                    "digest_hour": 8, "last_digest": "", "tour_done": False}


def _dir() -> Path:
    d = config.user_dir(ctx.uid()) / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(name, default):
    f = _dir() / name
    if not f.exists():
        return default
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return default


def _write(name, data):
    with _lock:
        d = _dir()
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        try:
            tmp.chmod(0o600)  # notification settings may contain webhook and bot credentials
        except OSError:
            pass
        tmp.replace(d / name)
        if name == "settings.json":
            _settings_mem[ctx.uid()] = dict(data)
    ctx.bump()


_settings_mem: dict = {}


def settings() -> dict:
    u = ctx.uid()
    if u not in _settings_mem:
        saved = _read("settings.json", {})
        _settings_mem[u] = {k: saved.get(k, default) for k, default in SETTING_DEFAULTS.items()}
    return dict(_settings_mem[u])


def save_settings(patch: dict) -> dict:
    with _lock:
        current = settings()
        for k, v in patch.items():
            if k not in SETTING_DEFAULTS:
                continue
            if k == "tour_done":
                v = bool(v)
            elif k == "digest_hour":
                v = max(0, min(23, int(v)))
            else:
                v = str(v).strip()[:300]
                if k == "discord_webhook" and v and not v.startswith("https://discord.com/api/webhooks/"):
                    raise ValueError("That doesn't look like a Discord webhook link.")
                if k in SECRET_KEYS and not v:
                    continue  # blank means keep the saved credential
            current[k] = v
        _write("settings.json", current)
        return current


def public_settings() -> dict:
    current = settings()
    return {k: (bool(v) if k in SECRET_KEYS else v) for k, v in current.items()}
