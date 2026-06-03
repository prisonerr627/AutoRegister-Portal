"""Environment-driven configuration."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Load a .env sitting next to the project root if present (local dev).
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover - dotenv optional
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


PORTAL_BASE = "https://portal.aiub.edu"

PORTAL_USERNAME = os.environ.get("PORTAL_USERNAME", "").strip()
PORTAL_PASSWORD = os.environ.get("PORTAL_PASSWORD", "").strip()

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Default model fallback chain mirrors the campusbuddies backend.
_DEFAULT_MODELS = (
    "google/gemma-4-26b-a4b-it,"
    "google/gemma-3-27b-it:free,"
    "google/gemma-4-26b-a4b-it:free,"
    "google/gemma-4-31b-it:free"
)
OPENROUTER_MODELS = [
    m.strip()
    for m in os.environ.get("OPENROUTER_MODELS", _DEFAULT_MODELS).split(",")
    if m.strip()
]

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()

POLL_INTERVAL_CLOSED = _int("POLL_INTERVAL_CLOSED", 1)
POLL_INTERVAL_OPEN = _int("POLL_INTERVAL_OPEN", 1)
KEEPALIVE_SECONDS = _int("KEEPALIVE_SECONDS", 240)
# Mandatory server-side delay between entering Select2 and being allowed to call
# GetPreReg2. Calling earlier returns "You tried to manipulate your session".
# Measured live (probe_timer.py, 2026-05-30): the gate flips between 48s and 50s,
# i.e. the real threshold is ~49s. Default 55s adds margin for network jitter.
SELECT2_WAIT_SECONDS = _int("SELECT2_WAIT_SECONDS", 55)
# How often to live-refresh ALL courses' bookable sections while engaged.
SECTIONS_REFRESH_SECONDS = _int("SECTIONS_REFRESH_SECONDS", 10)
# How often the always-on seat-fill Monitors loop checks each watched section.
MONITOR_INTERVAL_SECONDS = _int("MONITOR_INTERVAL_SECONDS", 60)
# Full wipe when the dashboard goes silent (browser/tab closed). The open dashboard
# polls /api/status every few seconds, which keeps the session alive; once no
# authenticated request arrives for this many seconds, the user's poller is stopped
# and their creds/cookies/alerts/meta are wiped. Generous enough to survive a page
# refresh or brief network blip. Set to 0 to disable idle-wipe entirely.
SESSION_IDLE_TIMEOUT = _int("SESSION_IDLE_TIMEOUT", 60)

DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip()
DASHBOARD_PORT = _int("DASHBOARD_PORT", 8000)

# ─── Debugging: Burp/HTTP proxy ────────────────────────────────────────────
# Route portal traffic through an intercepting proxy (e.g. Burp at
# http://127.0.0.1:8080). When a proxy is set, TLS verification defaults OFF so
# Burp's self-signed CA works; override explicitly with VERIFY_TLS.
PROXY_URL = os.environ.get("PROXY_URL", "").strip() or None
_verify_env = os.environ.get("VERIFY_TLS", "").strip().lower()
if _verify_env in ("0", "false", "no", "off"):
    VERIFY_TLS = False
elif _verify_env in ("1", "true", "yes", "on"):
    VERIFY_TLS = True
else:
    VERIFY_TLS = not bool(PROXY_URL)

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "autoregister.db"
COOKIE_PATH = DATA_DIR / "cookies.json"

# The bundled Offered Course Report (committed seed) lives at the project root.
CATALOG_XLSX = Path(
    os.environ.get(
        "CATALOG_XLSX",
        str(Path(__file__).resolve().parent.parent / "Offered Course Report.xlsx"),
    )
)
# A live copy of the catalog lands in the (writable) data dir and takes precedence over
# the bundled seed — so the catalog stays current per semester. It is refreshed in the
# background on login when older than CATALOG_MAX_AGE_HOURS, or on demand via the
# dashboard's "Refresh catalog" button.
#
# The live source is now the Offered-Sections page (parsed to JSON, CATALOG_CACHE),
# which is fast (~2.5s) and carries titles+sections+timing. CATALOG_OVERRIDE is the
# legacy xlsx written by the old ~30s DownloadOfferedReport path; still honoured on load
# for backward-compat if a JSON cache isn't present yet.
CATALOG_CACHE = DATA_DIR / "catalog.json"
CATALOG_OVERRIDE = DATA_DIR / "Offered Course Report.xlsx"
CATALOG_MAX_AGE_HOURS = _int("CATALOG_MAX_AGE_HOURS", 24)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
)
