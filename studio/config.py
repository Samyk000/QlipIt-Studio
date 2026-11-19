"""All settings come from environment variables (or a .env file next to the project), so the same code runs
on a laptop and on a server. Nothing here is specific to one machine."""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    if os.environ.get("QLIPIT_LOAD_DOTENV", "1") == "0":
        return
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(key: str, default: str = "") -> str:
    """Read an environment variable, checking QLIPIT_* first, with fallback to PEAKCUT_*."""
    if key.startswith("PEAKCUT_"):
        k = key[8:]
        val = os.environ.get(f"QLIPIT_{k}")
        if val is not None and val != "":
            return val
        val = os.environ.get(key)
        if val is not None and val != "":
            return val
        return default
    elif key.startswith("QLIPIT_"):
        val = os.environ.get(key)
        if val is not None and val != "":
            return val
        k = key[7:]
        val = os.environ.get(f"PEAKCUT_{k}")
        if val is not None and val != "":
            return val
        return default
    val = os.environ.get(key)
    return val if val is not None else default


def penv(key: str, default=None):
    """Read QLIPIT_* or PEAKCUT_* env variable."""
    if key.startswith("PEAKCUT_"):
        k = key[8:]
        val = env(f"QLIPIT_{k}")
        return val if val else (env(key) or default)
    if key.startswith("QLIPIT_"):
        val = env(key)
        if val:
            return val
        k = key[7:]
        val = env(f"PEAKCUT_{k}")
        return val if val else default
    return env(f"QLIPIT_{key}") or env(f"PEAKCUT_{key}") or env(key) or default


_kept: dict = {}


def _keep(key, make, seconds=60):
    """Settings read from files (keys, the Google client) are looked at once a minute, not on every request."""
    import time
    got = _kept.get(key)
    if not got or time.time() - got[0] > seconds:
        got = _kept[key] = (time.time(), make())
    return got[1]


def _secret(var: str, file_var: str, pattern: str = r"\S{16,}") -> str:
    """A secret given directly (VAR) or as a path to a file holding it (VAR_FILE)."""
    return _keep(("secret", var, file_var), lambda: _read_secret(var, file_var, pattern))


def _read_secret(var: str, file_var: str, pattern: str) -> str:
    val = penv(var)
    if val:
        return val.strip()
    path = penv(file_var)
    if path and Path(path).exists():
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        m = re.search(pattern, text)
        return m.group(0) if m else text.strip().strip('"')
    return ""


# ---------------------------------------------------------------- where things live
# Everything QlipIt keeps is in one folder. Which one: QLIPIT_DATA in .env if it is set;
# otherwise the folder the owner chose in Settings, Storage (kept in storage.json); otherwise "data" inside the folder.
STORAGE_FILE = Path(penv("QLIPIT_STORAGE_FILE") or ROOT / "storage.json")


def _move_folder(old: Path, new: Path) -> bool:
    """Move everything from the old folder into the new one. Done at start, before anything is open. True when
    nothing is left behind; anything that could not be moved stays where it was and is tried again next start."""
    import shutil
    if not old.is_dir() or old.resolve() == new.resolve():
        return True
    items = list(old.iterdir())
    if items:
        print(f"\n  Moving QlipIt's folder to {new} ({len(items)} things). This is done once and can take a while...", flush=True)
    done = True
    for item in items:
        target = new / item.name
        try:
            if target.exists() and item.is_dir():
                for sub in item.iterdir():           # the folder is already there (made empty, or half moved): fill it
                    if not (target / sub.name).exists():
                        shutil.move(str(sub), str(target / sub.name))
                if not any(item.iterdir()):
                    item.rmdir()
                else:
                    done = False
            elif not target.exists():
                shutil.move(str(item), str(target))
            else:
                done = False
        except OSError as e:
            print(f"  Could not move {item.name}: {e}", flush=True)
            done = False
    if done:
        try:
            old.rmdir()
        except OSError:
            pass
        print("  Moved.\n", flush=True)
    return done


def _data_folder():
    data_env = penv("QLIPIT_DATA")
    if data_env:
        return Path(data_env), "env"
    try:
        chosen = json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        chosen = {}
    if not chosen.get("path"):
        return ROOT / "data", "default"
    new = Path(chosen["path"])
    try:
        new.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise SystemExit(f"\n  QlipIt's folder is set to {new}, and that can't be reached right now.\n"
                         f"  Connect that drive and start QlipIt again, or delete {STORAGE_FILE} to go back to the\n"
                         f"  folder inside QlipIt (your videos and clips stay where they are).\n")
    if chosen.get("move_from"):
        # Switch to the new folder only once the move finished. If it stopped half-way, stay in whichever folder
        # still holds the database, so the app never opens an empty one and looks like everything was lost.
        old = Path(chosen["move_from"])
        if _move_folder(old, new):
            STORAGE_FILE.write_text(json.dumps({"path": str(new)}, indent=1), encoding="utf-8")
        elif (old / "qlipit.db").exists() or (old / "peakcut.db").exists():
            return old, "chosen"          # storage.json keeps move_from, so the move is tried again next start
    return new, "chosen"


DATA_DIR, DATA_FROM = _data_folder()
USERS_DIR = DATA_DIR / "users"          # users/<user id>/{creators,jobs,accounts}
MODELS_DIR = Path(penv("QLIPIT_MODELS") or DATA_DIR / "models")
TMP_DIR = DATA_DIR / "tmp"
FONTS_DIR = DATA_DIR / "fonts"          # caption fonts, copied from studio/fonts so every server renders the same
# YouTube already has a word-by-word transcript of most videos. With this on, that is what the AI reads to find the
# clips (seconds instead of transcribing hours), and our own model is run only on the clips themselves.
YT_TRANSCRIPT = penv("QLIPIT_YT_TRANSCRIPT", "1") != "0"
# the compact model for processors; a machine with a graphics card sets QLIPIT_ASR_MODEL (or PEAKCUT_ASR_MODEL) to full-precision
ASR_MODEL_NAME = penv("QLIPIT_ASR_MODEL", "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")
ASR_MODEL_DIR = MODELS_DIR / ASR_MODEL_NAME
_default_db = (DATA_DIR / "qlipit.db") if (DATA_DIR / "qlipit.db").exists() or not (DATA_DIR / "peakcut.db").exists() else (DATA_DIR / "peakcut.db")
DATABASE_URL = env("DATABASE_URL") or f"sqlite:///{_default_db.as_posix()}"

for d in (USERS_DIR, MODELS_DIR, TMP_DIR, FONTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def user_dir(uid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_]{6,40}", uid or ""):
        raise ValueError("bad user id")
    return USERS_DIR / uid


# ---------------------------------------------------------------- the site
HOST = penv("QLIPIT_HOST", "127.0.0.1")
PORT = int(penv("QLIPIT_PORT") or "8765")
PUBLIC_URL = (penv("QLIPIT_PUBLIC_URL") or f"http://127.0.0.1:{PORT}").rstrip("/")   # e.g. https://qlipit.com
HTTPS = PUBLIC_URL.startswith("https://")
SESSION_DAYS = int(penv("QLIPIT_SESSION_DAYS", "30"))
# Accounts are off by default: QlipIt opens straight into the dashboard, with no sign-in, which is what you want
# on your own computer. On a server that other people can reach, set QLIPIT_ACCOUNTS=1 (or PEAKCUT_ACCOUNTS=1):
# everyone then signs up and signs in, and each person only sees their own videos.
ACCOUNTS = penv("QLIPIT_ACCOUNTS", "0") == "1"
REPO_URL = penv("QLIPIT_REPO_URL", "").strip()
FOLLOW_URL = penv("QLIPIT_FOLLOW_URL", "").strip()

# ---------------------------------------------------------------- limits (per user, and for the whole server)
# Your own QlipIt has no limits unless you set them: 0 (the default) means "none" for every limit below. The
# machine still protects itself: new work waits while the processor, memory or disk are nearly full (load.py).
_NONE = 10 ** 7                      # what "no limit" becomes: far beyond anything real, and still a plain number


def _limit(name: str, default: str = "0"):
    v = float(penv(name) or default)
    return _NONE if v <= 0 else v


MAX_UPLOAD_MB = int(_limit("QLIPIT_MAX_UPLOAD_MB"))              # the largest video file someone may upload
# A big file is sent in pieces of this size and joined here, because web servers and CDNs in front of the app
# refuse very large single requests (Cloudflare stops at 100 MB).
UPLOAD_PART_MB = max(5, int(penv("QLIPIT_UPLOAD_PART_MB", "40")))
# The most videos that may ever run at once. How many actually run is decided by how busy the machine is (load.py).
JOB_WORKERS = int(penv("QLIPIT_JOB_WORKERS") or max(2, os.cpu_count() or 4))
MAX_ACTIVE_JOBS_PER_USER = int(_limit("QLIPIT_MAX_JOBS_PER_USER", "5"))    # videos sent and worked on at once
MAX_LIVE_TOTAL = int(penv("QLIPIT_MAX_LIVE") or max(4, 2 * (os.cpu_count() or 4)))   # ceiling; real load decides below it
MAX_LIVE_PER_USER = int(_limit("QLIPIT_MAX_LIVE_PER_USER"))     # streams one person records at once
MAX_VIDEO_HOURS = _limit("QLIPIT_MAX_VIDEO_HOURS")              # longest video (or chosen part of one) that is processed
MAX_LIVE_HOURS = _limit("QLIPIT_MAX_LIVE_HOURS")                # a recording stops by itself after this long
MAX_USER_GB = _limit("QLIPIT_MAX_USER_GB")                      # disk space one account may fill
MAX_CREATORS = int(_limit("QLIPIT_MAX_CREATORS"))               # creators one account may add
ALLOWED_SITES = tuple(s.strip().lower() for s in penv(
    "QLIPIT_ALLOWED_SITES", "youtube.com,youtu.be,twitch.tv,kick.com").split(",") if s.strip())

# ---------------------------------------------------------------- AI (bring your own key: see studio/ai.py)
# These are the starting values. Whatever is saved from the dashboard (Settings, AI) is used instead.
AI_PROVIDER = penv("QLIPIT_AI_PROVIDER", "gemini").strip().lower()            # gemini, openai, xai, groq, openrouter, anthropic, ollama, custom
AI_MODELS = [m.strip() for m in penv("QLIPIT_AI_MODELS", "").split(",") if m.strip()]   # tried in this order
AI_BASE_URL = penv("QLIPIT_AI_BASE_URL", "").rstrip("/")                      # only for "custom", or to override a provider's address


def ai_key() -> str:
    return _secret("QLIPIT_AI_KEY", "QLIPIT_AI_KEY_FILE", r"\S{8,}")


def ai_ready() -> bool:
    from . import ai
    return ai.ready()


# ---------------------------------------------------------------- account emails (see mailer.py)
SMTP_HOST = penv("QLIPIT_SMTP_HOST", "")
SMTP_PORT = int(penv("QLIPIT_SMTP_PORT", "587"))
SMTP_USER = penv("QLIPIT_SMTP_USER", "")
MAIL_FROM = penv("QLIPIT_MAIL_FROM", "")
CONTACT_EMAIL = penv("QLIPIT_CONTACT_EMAIL", "")
LEGAL_NAME = penv("QLIPIT_LEGAL_NAME", "")            # the business that runs QlipIt, for the legal pages
LEGAL_COUNTRY = penv("QLIPIT_LEGAL_COUNTRY", "")      # whose law applies, for the terms
# Set to 1 on a public site with email set up: password sign-ups must then confirm their address before clipping.
VERIFY_EMAIL = penv("QLIPIT_VERIFY_EMAIL", "0") == "1"


def smtp_password() -> str:
    return _secret("QLIPIT_SMTP_PASSWORD", "QLIPIT_SMTP_PASSWORD_FILE", r"\S{4,}")


# ---------------------------------------------------------------- Google (sign in with Google, and connecting YouTube)
def google_client() -> tuple:
    """(client id, client secret). Either GOOGLE_CLIENT_ID/SECRET, or GOOGLE_CLIENT_FILE = the JSON Google lets you download."""
    return _keep("google", _read_google_client)


def _read_google_client() -> tuple:
    if env("GOOGLE_CLIENT_ID") and env("GOOGLE_CLIENT_SECRET"):
        return env("GOOGLE_CLIENT_ID").strip(), env("GOOGLE_CLIENT_SECRET").strip()
    path = env("GOOGLE_CLIENT_FILE")
    if path and Path(path).exists():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            web = data.get("web") or data.get("installed") or {}
            return web.get("client_id", ""), web.get("client_secret", "")
        except ValueError:
            pass
    return "", ""


# ---------------------------------------------------------------- video
MAX_SOURCE_HEIGHT = 1080
OUT_W, OUT_H, OUT_FPS = 1080, 1920, 30
