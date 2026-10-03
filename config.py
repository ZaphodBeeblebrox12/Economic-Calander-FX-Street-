"""FXStreet Playwright probe — configuration. Edit here, not in probe.py."""

import os
from pathlib import Path

# ---------------------------------------------------------------- page
URL = "https://www.fxstreet.com/economic-calendar"
HEADED = True                    # Phase 0 default: headed (easier debugging, most realistic)
HEADLESS = False                 # set HEADED=False, HEADLESS=True for server runs
# Use your real installed Google Chrome instead of Playwright's Chromium build.
# Strongest single anti-fingerprinting move: genuine Chrome with real codecs/telemetry.
# Set to "chrome" to enable (requires Google Chrome installed). None = bundled Chromium.
BROWSER_CHANNEL = "msedge"       # drives Microsoft Edge (this machine has no Chrome); "chrome" or None also valid
# Full path to a browser executable. If set and exists, it wins over BROWSER_CHANNEL.
# Leave None to auto-detect (standard Chrome + Edge install paths, then bundled Chromium).
CHROME_EXECUTABLE_PATH = None    # e.g. r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# Inject a minimal stealth init script (mask navigator.webdriver, fill chrome/plugins).
MASK_WEBDRIVER = True
SLOW_MO_MS = 0                   # >0 only for visual debugging

VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")  # desktop UA -> row layout
TIMEZONE_ID = "UTC"              # pin page timezone: stable scheduled-time keys
LOCALE = "en-US"

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    # keep the tab fully awake when the window is covered/minimized (Windows):
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
    "--window-position=0,0",
    "--start-maximized",
]

# ---------------------------------------------------------------- timing / health
HEARTBEAT_TIMEOUT_S = 45   # 20s false-fired during ad-storm queue backlog         # no JS heartbeat for this long -> unhealthy
STALE_S = 240                    # no DOM mutation AND no relevant network activity in LIVE -> DEGRADED
CHALLENGE_CHECK_S = 60           # how often to scan for challenge/block pages
RESOURCE_LOG_S = 300             # memory/counters logging interval
DEDUP_CLEANUP_S = 600            # TTL sweep of the dedup store
DEDUP_TTL_S = 48 * 3600
MAX_DEDUP_ENTRIES = 5000

# ---------------------------------------------------------------- recovery ladder
RECOVERY_VERIFY_S = 45           # wait this long after a recovery action, then verify
RECOVERY_MAX_AT_LEVEL = 3        # attempts at one level before escalating
LEVEL_COOLDOWN_S = 120           # pause between full ladder cycles

# ---------------------------------------------------------------- detection
ALLOW_REVISIONS = False          # emit REVISION events (actual value edited) or just log them
NET_CORRELATION_WINDOW_MS = 15000  # match network frame <-> DOM release within this window
CAPTURE_HTTP_BODIES = True
MAX_HTTP_BODY_BYTES = 150_000

# keywords that mark a network payload as calendar-relevant.
# NOTE: deliberately endpoint-agnostic. No 'signalr'/'calendarhub' assumptions:
# discovery is shape/keyword based so a transport swap does not break us.
NET_RELEVANT_KEYWORDS = [
    "actual", "consensus", "previous", "revised", "forecast",
    "eventdate", "event_id", "eventid", "volatility", "calendar",
    "dateutc", "currencycode", "countrycode",
]

CURRENCIES = ["USD", "EUR", "JPY", "GBP", "AUD", "NZD", "CAD", "CHF", "CNY",
              "HKD", "SGD", "KRW", "INR", "BRL", "MXN", "ZAR", "SEK", "NOK",
              "DKK", "PLN", "TRY", "XAU", "XAG"]

# Country management - the probe rewrites the eventDates request, so you
# never touch the site filter panel. Comma-separated ISO codes.
#   ADD:    countries appended to every data fetch (e.g. "IN" = India/INR)
#   REMOVE: countries stripped from the site default list (e.g. "UA" = Ukraine)
EXTRA_COUNTRIES = "IN"
REMOVE_COUNTRIES = "UA"

# optional saved site state (filter cookies etc.) written by set_filters.py
FXS_STATE_FILE = "fxs_state.json"

# challenge / block page markers (title or body text)
CHALLENGE_MARKERS = [
    "checking your browser", "verify you are human", "captcha", "access denied",
    "attention required", "request blocked", "just a moment", "are you a robot",
    "verify you are a human", "error 403", "403 forbidden",
]

# ---------------------------------------------------------------- redaction
# applied to every URL / payload before it touches a log file.
# note: these use plain single-quoted raw strings; do not "simplify" the escaping.
REDACT_PATTERNS = [
    r"(?i)((?:access_token|auth_token|token|apikey|api_key|sessionid|session_id|sid|jwt)=)[^&\s\"']+",
    r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]+",
    r"(?i)(cookie:\s*)[^\n\"']+",
    r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}",
    r"(?i)(\"access_?token\"\s*:\s*\")[^\"]+",   # JSON body tokens (negotiate responses)
]

# ad/tracker domains: counted but not logged (they caused the event storm that
# starved heartbeat processing and triggered a false DEGRADED->RECOVERY cycle)
AD_DOMAINS = [
    "doubleclick.net", "googlesyndication.com", "fundingchoicesmessages.google.com",
    "clarity.ms", "google.com/measurement", "analytics.google.com", "google.com/ccm",
    "reddit.com/pixels", "tiktok.com", "html-load.com", "onesignal.com",
    "adtrafficquality.google", "google.com/rmkt",
]

# ---------------------------------------------------------------- telegram
# Optional live operational signal. Credentials come from .env (see
# .env.example) or the process environment. If either value is missing the
# notifier is a clean no-op and the probe runs exactly as before: Telegram
# is a tap, never a pipe, and never a dependency of acquisition.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass  # python-dotenv optional; values can be exported manually instead

TELEGRAM_TOKEN = os.environ.get("FXS_TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("FXS_TELEGRAM_CHAT_ID", "")

TG_NOTIFY_STATE = True        # DEGRADED / RECOVERING / RECOVERED / FAILED
TG_QUIET_HEARTBEAT_S = 6 * 3600  # "still alive, quiet market" cadence; 0 = off
TG_STATE_MIN_INTERVAL_S = 120    # collapse flapping health transitions
TG_MAX_QUEUE = 64

# ---------------------------------------------------------------- paths
LOG_DIR = Path("logs")
OUT_DIR = Path("output")
PROBE_LOG = LOG_DIR / "probe.jsonl"       # everything (rotating)
RELEASE_LOG = LOG_DIR / "releases.jsonl"  # release events only (rotating)
BENCH_OUT = OUT_DIR / "benchmark.jsonl"
CENSUS_OUT = OUT_DIR / "dom_census.json"
