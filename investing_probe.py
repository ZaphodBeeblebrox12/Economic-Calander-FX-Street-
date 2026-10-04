"""
investing_probe.py — Investing.com economic calendar probe (branch: feature/investing-source)

ARCHITECTURE (census-confirmed, 2026-10-04):
  - investing.com is a Next.js app; the full calendar payload ships inside the
    HTML as __NEXT_DATA__.props.pageProps.state.economicCalendarStore
        .calendarEventsByDate["YYYY-MM-DD"] = [event, ...]
  - Each event: occurrenceId, eventId, currency, country, countryId,
    importance "1|2|3", event, eventLong, period, time/actual_time (UTC ISO),
    previous, forecast, actual, revisedFrom, hasActualChanged
  - Country IDs decoded from the same blob: US=5, IN=14, UA=61, EU=72, ...
  - Updates are detected by re-fetching the page in the background
    (fetch('/economic-calendar/') from page context) and diffing the payload.
    No navigation, no POST to the Cloudflare-challenged legacy endpoint.

SEPARATE PROCESS (design decision #1): run alongside probe.py (FXStreet),
never in the same browser/tab. Emits JSONL -> investing_releases.jsonl
with a source:"investing" tag for the future merge layer (source_map.py).

Run:    python investing_probe.py
"""

import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CALENDAR_URL = "https://www.investing.com/economic-calendar/"
CDP_URL = "http://localhost:9222"   # user's real Edge, started with --remote-debugging-port=9222
ROOT = Path(__file__).resolve().parent
# COPIED REAL EDGE PROFILE — carries your genuine trust cookies/history.
# Create it once (Edge fully closed) with:
#   robocopy "%LOCALAPPDATA%\Microsoft\Edge\User Data" ^
#            "C:\Users\Inder\Desktop\fxstreet_playwright_probe\edge_profile_copy" ^
#            /E /COPY:DAT /XD "User Data\Default\Cache" "User Data\Default\Code Cache" ^
#               "User Data\Default\GPUCache" "User Data\Default\Service Worker\CacheStorage"
PROFILE_DIR = ROOT / "edge_profile_copy"
STATE_FILE = ROOT / "investing_state.json"
OUT_FILE = ROOT / "investing_releases.jsonl"
HEADED = True
FETCH_INTERVAL = 20        # seconds between background payload fetches
                           # (no periodic page reloads — reloads poked Cloudflare
                           # and the DOM copy of __NEXT_DATA__ never updates
                           # client-side; a same-origin fetch is the real update
                           # path and looks like normal page traffic)
HEARTBEAT = 300            # log a heartbeat line every 5 min while healthy
RELOAD_COOLDOWN = 1800     # after one reload, wait 30 min before another
TIMEZONE_ID = "Asia/Kolkata"

# Country filter seeded into localStorage (site-default majors; add/drop IDs).
# US=5 IN=14 UA=61 EU=72 GB=4 JP=35 DE=17 ... full table in README census.
COUNTRIES = [25, 32, 6, 37, 72, 22, 17, 39, 14, 10, 35, 43, 36, 110, 11, 26, 12, 4, 5, 56]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""

# --- in-page helpers (run inside the browser) -------------------------------
EXTRACT_ND = """() => {
    const el = document.getElementById('__NEXT_DATA__');
    return el ? el.textContent : null;
}"""

FETCH_ND = """async () => {
    const r = await fetch('/economic-calendar/', {credentials: 'include',
        headers: {'Accept': 'text/html'}});
    const html = await r.text();
    const m = html.match(/<script id="__NEXT_DATA__" type="application\\/json">([\\s\\S]*?)<\\/script>/);
    return m ? m[1] : null;
}"""


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def emit(record):
    record["source"] = "investing"
    record["emitted_at"] = datetime.now(timezone.utc).isoformat()
    with OUT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    name = record.get("event", "?")
    cur = record.get("currency", "?")
    log(f"EMIT {record['kind']:<14} {cur} {name}  actual={record.get('actual','')!r}"
        f" forecast={record.get('forecast','')!r} prev={record.get('previous','')!r}")


def parse_events(nd_text):
    """__NEXT_DATA__ text -> {date: [event dicts]} from the calendar store."""
    if not nd_text:
        return {}
    try:
        nd = json.loads(nd_text)
        store = nd["props"]["pageProps"]["state"]["economicCalendarStore"]
        return store.get("calendarEventsByDate", {}) or {}
    except Exception:
        return {}


def normalize(ev, date_str):
    """Flat, merge-layer-friendly shape keyed by occurrenceId."""
    return {
        "occurrence_id": ev.get("occurrenceId"),
        "event_id": ev.get("eventId"),
        "date": ev.get("date", date_str),
        "time_utc": ev.get("time", ""),
        "actual_time_utc": ev.get("actual_time", ""),
        "currency": ev.get("currency", ""),
        "country": ev.get("country", ""),
        "country_id": ev.get("countryId"),
        "importance": ev.get("importance", ""),   # "1" low / "2" med / "3" high
        "event": ev.get("event", ""),
        "event_long": ev.get("eventLong", ""),
        "period": ev.get("period", ""),
        "is_speech": ev.get("isSpeech", False),
        "is_report": ev.get("isReport", False),
        "forecast": ev.get("forecast", ""),
        "previous": ev.get("previous", ""),
        "actual": ev.get("actual", ""),
        "revised_from": ev.get("revisedFrom", ""),
        "has_actual_changed": ev.get("hasActualChanged", False),
    }


class DiffEngine:
    """Tracks seen occurrences; yields emission dicts on meaningful change."""

    def __init__(self):
        self.seen = {}          # occurrence_id -> normalized event

    def process(self, by_date):
        out = []
        for date_str, events in (by_date or {}).items():
            for ev in events or []:
                n = normalize(ev, date_str)
                oid = n["occurrence_id"]
                if oid is None:
                    continue
                old = self.seen.get(oid)
                if old is None:
                    n["kind"] = "new_event"
                    out.append(n)
                else:
                    # actual appeared / changed
                    if n["actual"] and n["actual"] != old.get("actual"):
                        n2 = dict(n); n2["kind"] = "actual_update"
                        n2["prev_actual"] = old.get("actual", "")
                        out.append(n2)
                    # revision (revisedFrom populated later)
                    if n["revised_from"] and n["revised_from"] != old.get("revised_from"):
                        n2 = dict(n); n2["kind"] = "revision"
                        out.append(n2)
                    # forecast material change
                    if n["forecast"] != old.get("forecast"):
                        n2 = dict(n); n2["kind"] = "forecast_update"
                        out.append(n2)
                self.seen[oid] = n
        return out


def is_challenged(page) -> bool:
    try:
        t = page.title().lower()
    except Exception:
        t = ""
    try:
        h = page.content().lower()
    except Exception:
        h = ""
    return ("just a moment" in t or "cf_chl_opt" in h
            or "performing security verification" in h
            or "verifies you are not a bot" in h)


def wait_out_challenge(page, where):
    if not is_challenged(page):
        return True
    log(f"Cloudflare challenge at {where}.")
    log("If a checkbox is showing in the Edge window: tick it ONCE, then wait.")
    log("Do not re-tick and do not click elsewhere — it resets the check.")
    for attempt in range(10):
        page.wait_for_timeout(30000)
        if not is_challenged(page):
            log("challenge cleared")
            return True
        if attempt == 2:
            log("still challenged — reloading page once (this often completes "
                "verification after a tick)...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
    return False


def open_browser(p):
    """Returns (context, mode, browser-or-None). Modes: cdp > persistent > state_file."""
    # 1) TRUSTED PATH: connect to the user's real Edge (their profile, their
    #    trust cookies, no automation flags). Start Edge first with:
    #    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222
    try:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        log(f"connected to your real Edge via CDP ({CDP_URL})")
        return context, "cdp", browser
    except Exception as e:
        log(f"CDP connect failed ({type(e).__name__}) — ignored; using copied real profile")

    # 2) own persistent profile
    context, browser, mode = None, None, None
    try:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="msedge",
            headless=not HEADED, viewport={"width": 1400, "height": 900},
            locale="en-IN", timezone_id=TIMEZONE_ID,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation", "--no-sandbox"])
        mode = "persistent"
    except Exception as e:
        log(f"persistent launch failed ({type(e).__name__}), falling back: {e}")
    if context is None:
        kw = {"viewport": {"width": 1400, "height": 900}}
        if STATE_FILE.exists():
            kw["storage_state"] = str(STATE_FILE)
        browser = p.chromium.launch(channel="msedge", headless=not HEADED,
                                    args=["--disable-blink-features=AutomationControlled"],
                                    ignore_default_args=["--enable-automation", "--no-sandbox"])
        context = browser.new_context(**kw)
        mode = "state_file"
    context.add_init_script(STEALTH_JS)
    return context, mode, browser


def _find_cf_file():
    """Accept cf_clearance.txt OR the Notepad double-extension cf_clearance.txt.txt."""
    for name in ("cf_clearance.txt", "cf_clearance.txt.txt"):
        f = ROOT / name
        if f.exists():
            return f
    hits = sorted(ROOT.glob("cf_clearance*.txt*"))
    return hits[0] if hits else None


CF_FILE = _find_cf_file()


def import_cf_clearance(context):
    """Inject cf_clearance solved in the user's REAL Edge (same IP + UA => valid).
    Expects the cookie VALUE alone in cf_clearance.txt (no quotes, no name)."""
    cf_file = _find_cf_file()
    if cf_file is None:
        return False
    value = cf_file.read_text(encoding="utf-8").strip()
    if not value:
        return False
    context.add_cookies([{
        "name": "cf_clearance", "value": value,
        "domain": ".investing.com", "path": "/",
        "httpOnly": True, "secure": True, "sameSite": "None",
    }])
    log(f"imported cf_clearance from {cf_file.name} ({len(value)} chars)")
    return True


def run():
    log("=== investing_probe starting ===")
    diff = DiffEngine()
    with sync_playwright() as p:
        context, mode, browser = open_browser(p)
        log(f"launch mode: {mode}")
        if mode != "cdp":
            import_cf_clearance(context)
        page = context.pages[0] if (mode == "persistent" and context.pages) else context.new_page()

        # seed filter state (country selection) before the app boots
        page.add_init_script(
            "try { localStorage.setItem('eco_active_filters', JSON.stringify("
            + json.dumps({"countries": COUNTRIES,
                          "timezone": {"offset": "GMT+05:30", "city": "Calcutta",
                                       "timezone": "Asia/Calcutta"},
                          "eventDisplayType": "FUTURE_EVENT"})
            + ")); } catch (e) {}")

        # initial snapshot from the freshly loaded DOM -> immediate emissions
        try:
            for rec in diff.process(parse_events(page.evaluate(EXTRACT_ND)) or {}):
                emit(rec)
        except Exception:
            log(f"initial snapshot failed: {traceback.format_exc()}")

        last_fetch = 0.0
        last_heartbeat = time.time()
        consecutive_errors = 0
        reload_cooldown_until = 0.0

        while True:
            try:
                now = time.time()
                if now - last_fetch >= FETCH_INTERVAL:
                    last_fetch = now
                    nd_text = page.evaluate(FETCH_ND)
                    by_date = parse_events(nd_text)
                    if not by_date:
                        # fallback: freshly loaded DOM (e.g. right after a reload)
                        by_date = parse_events(page.evaluate(EXTRACT_ND))
                    if not by_date:
                        consecutive_errors += 1
                        log(f"empty payload (x{consecutive_errors})")
                        if (consecutive_errors >= 6 and now >= reload_cooldown_until):
                            log("sustained fetch failures - one reload "
                                "(30-min cooldown after)")
                            try:
                                page.goto(CALENDAR_URL, wait_until="domcontentloaded",
                                          timeout=60000)
                                page.wait_for_timeout(5000)
                                reload_cooldown_until = time.time() + RELOAD_COOLDOWN
                                consecutive_errors = 0
                                if is_challenged(page):
                                    wait_out_challenge(page, "reload")
                            except Exception:
                                pass
                        time.sleep(FETCH_INTERVAL)
                        continue
                    consecutive_errors = 0
                    for rec in diff.process(by_date):
                        emit(rec)
                    if now - last_heartbeat >= HEARTBEAT:
                        last_heartbeat = now
                        log(f"heartbeat: {len(diff.seen)} occurrences tracked")
                time.sleep(1.0)

            except KeyboardInterrupt:
                log("stopped by user")
                break
            except Exception:
                consecutive_errors += 1
                log(f"loop error (x{consecutive_errors}):" + chr(10) + traceback.format_exc())
                time.sleep(FETCH_INTERVAL)

        if mode == "cdp":
            try:
                page.close()          # close only OUR tab; never the user's browser
            except Exception:
                pass
        else:
            if mode == "state_file":
                context.storage_state(path=str(STATE_FILE))
            context.close()
    return 0


if __name__ == "__main__":
    sys.exit(run())
