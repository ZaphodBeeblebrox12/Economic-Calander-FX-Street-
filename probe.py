#!/usr/bin/env python3
"""
FXStreet Playwright probe — Phase-0 acquisition layer proof.

What it does:
  1. Launches real Chromium, loads the real FXStreet economic calendar page.
  2. FXStreet's own JS establishes whatever realtime transport it uses today.
  3. We OBSERVE: WebSocket frames, SSE/streaming responses, XHR/fetch, and DOM
     mutations (injected MutationObserver). We never impersonate the transport.
  4. Detects Actual transitions (empty -> value), timestamps every stage, and
     benchmarks network-side vs DOM-side detection on the same session.
  5. Self-heals: state machine + recovery ladder (re-inject -> reload -> new page ->
     new context -> relaunch browser).

Logs:
  logs/probe.jsonl      everything (rotating)
  logs/releases.jsonl   economic releases only (rotating)
  output/benchmark.jsonl  per-release latency comparison (network vs DOM)
  output/dom_census.json  last DOM structure report (send this back for analysis)
"""

import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
try:
    import resource as _resource      # Unix only
except ImportError:
    _resource = None                  # Windows


def _rss_mb():
    """Process working-set size in MB, cross-platform. None if unavailable."""
    try:
        if _resource is not None:
            ru = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
            # Linux reports KB, macOS reports bytes
            return round(ru / 1024.0, 1) if sys.platform != "darwin" else round(ru / (1024.0 * 1024.0), 1)
        import ctypes
        from ctypes import wintypes
        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        proc = ctypes.windll.kernel32.GetCurrentProcess()
        ok = 0
        for dll in ("kernel32", "psapi"):
            try:
                f = getattr(getattr(ctypes.windll, dll), "GetProcessMemoryInfo", None)
                if f:
                    ok = f(proc, ctypes.byref(pmc), pmc.cb)
                    if ok:
                        break
            except Exception:
                continue
        return round(pmc.WorkingSetSize / (1024.0 * 1024.0), 1) if ok else None
    except Exception:
        return None
import signal
import sys
import threading
import time
import traceback
from collections import Counter, OrderedDict, deque
from pathlib import Path

import config as C
from notifier import TelegramNotifier, fmt_uptime

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not installed. Run: pip install -r requirements.txt && playwright install chromium")

# logging.StreamHandler writes to sys.stderr by default: reconfigure BOTH
# streams to utf-8 BEFORE any handler is constructed, or emoji in page text
# crashes every console emit (and each crash's traceback stalls the queue).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------- clocks
def mono_ms() -> int:
    return int(time.perf_counter() * 1000)          # monotonic — all local latency math

def wall_ms() -> int:
    return int(time.time() * 1000)                  # wall — cross-machine only


# ---------------------------------------------------------------- redaction
_REDACTORS = [re.compile(p) for p in C.REDACT_PATTERNS]

def redact(text: str) -> str:
    if not text:
        return text
    for rx in _REDACTORS:
        text = rx.sub(lambda m: m.group(1) + "<redacted>" if m.lastindex else "<redacted>", text)
    return text


# ---------------------------------------------------------------- ad-noise filter
_AD_DOMAINS = tuple(getattr(C, "AD_DOMAINS", ()))

def is_ad_url(url: str) -> bool:
    return any(d in url for d in _AD_DOMAINS)

# ---------------------------------------------------------------- logging
class _ConsoleHandler(logging.StreamHandler):
    """Best-effort console output: a console encoding/write failure must NEVER
    raise, print a traceback, or stall the event pipeline. The file handler
    always has the full record."""
    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            pass


class _QuietConsole(logging.Filter):
    """High-frequency operational lines (heartbeats, page polls, censuses,
    corrections, lag warnings) are FILE-ONLY. The console is for releases,
    state changes, recovery and errors. This also minimizes blocked-write
    stalls: on Windows, selecting text in the console pauses ALL stdout
    writes, freezing the main loop for as long as the selection lasts."""

    _SKIP = ('"type": "heartbeat"', '"type": "page_state"',
             '"type": "dom_census_saved"', '"type": "delivery_lag"',
             '"type": "correction_logged"')

    def filter(self, record):
        msg = record.getMessage()
        return not any(s in msg for s in self._SKIP)


def make_logger(name: str, path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    h = logging.handlers.RotatingFileHandler(path, maxBytes=25_000_000, backupCount=5,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    sh = _ConsoleHandler()
    sh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    sh.addFilter(_QuietConsole())
    lg.addHandler(sh)
    return lg


logging.raiseExceptions = False   # never print logging tracebacks to stderr

log = make_logger("probe", C.PROBE_LOG)
releases_log = make_logger("releases", C.RELEASE_LOG)


def jlog(lg: logging.Logger, type_: str, **kw):
    lg.info(json.dumps({
        "type": type_,
        "mono_ms": mono_ms(),
        "wall_ms": wall_ms(),
        **kw,
    }, ensure_ascii=False))


# ---------------------------------------------------------------- dedup store
class Dedup:
    """occurrence_key -> {hash, first_seen, versions}. Bounded + TTL."""
    def __init__(self):
        self.store: OrderedDict[str, dict] = OrderedDict()
        self.duplicates = 0
        self.revisions = 0

    def classify(self, key: str, content_hash: str, now: int, has_actual=None) -> str:
        ent = self.store.get(key)
        if ent is None:
            self.store[key] = {"hash": content_hash, "first": now, "versions": 1,
                               "had_actual": bool(has_actual)}
            self._trim(now)
            return "NEW"
        ent["versions"] += 1
        if ent["hash"] == content_hash:
            self.duplicates += 1
            return "DUP"
        # first actual arriving on a previously empty row is a RELEASE, even
        # though the dedup store already knows the key from hydration
        first_release = (not ent.get("had_actual")) and bool(has_actual)
        ent["hash"] = content_hash
        ent["had_actual"] = bool(has_actual)
        if first_release:
            return "NEW"
        self.revisions += 1
        return "REVISION"

    def _trim(self, now: int):
        while len(self.store) > C.MAX_DEDUP_ENTRIES:
            self.store.popitem(last=False)
        for k in [k for k, v in self.store.items() if now - v["first"] > C.DEDUP_TTL_S * 1000]:
            self.store.pop(k, None)


# ---------------------------------------------------------------- health machine

_PAGE_STATE_JS = """(() => {
  let bodyTxt = "";
  try { bodyTxt = (document.body ? document.body.innerText.slice(0, 300) : "NO_BODY")
                    .replace(/[^\x20-\x7E]/g, ""); }
  catch (e) { bodyTxt = "ERR:" + e; }
  let rows = 0;
  try { rows = document.querySelectorAll("tr.fxs_c_row").length; } catch (e) {}
  return JSON.stringify({
    href: location.href, title: document.title, readyState: document.readyState,
    vis: document.visibilityState, probe: !!window.__fxProbeInstalled,
    emit: typeof window.probeEmit === "function",
    webdriver: navigator.webdriver, rows: rows, body: bodyTxt
  });
})()"""

_STARTUP_SNAPSHOT_JS = r"""(() => {
  const rows = Array.from(document.querySelectorAll('tr.fxs_c_row'));
  const out = [];
  for (const r of rows) {
    const actual = ((r.querySelector('.fxs_c_actual')||{}).textContent || '').trim();
    if (actual && actual !== '-') continue;              // skip already-released
    const t = ((r.querySelector('.fxs_c_time')||{}).textContent || '').trim();
    const cur = ((r.querySelector('.fxs_c_currency')||{}).textContent || '').trim();
    const name = ((r.querySelector('.fxs_c_name')||{}).textContent || '').trim().replace(/\s+/g, ' ');
    const m = ((r.querySelector('.fxs_c_impact-icon')||{}).className || '').match(/fxs_c_impact-(high|medium|low)/);
    out.push(`${t}  ${cur}  ${name}${m ? '  [' + m[1].toUpperCase() + ']' : ''}`);
    if (out.length >= 6) break;
  }
  return JSON.stringify({total: rows.length, upcoming: out});
})()"""


class Health:
    STARTING, BOOTSTRAPPING, PAGE_READY, LIVE, DEGRADED, RECOVERING, FAILED = range(7)
    NAMES = {0: "STARTING", 1: "BOOTSTRAPPING", 2: "PAGE_READY", 3: "LIVE",
             4: "DEGRADED", 5: "RECOVERING", 6: "FAILED"}

    def __init__(self, emit):
        self.state = self.STARTING
        self.emit = emit
        self.reason = ""
        self.calendar_seen = False
        self.realtime_seen = False
        self.last_heartbeat_mono = 0
        self.last_dom_activity_mono = 0
        self.last_net_activity_mono = 0
        self.low_conf_streak = 0
        self.challenge_detected = False

    def set(self, state: int, reason: str = ""):
        if state != self.state:
            jlog(log, "health", from_=self.NAMES[self.state], to=self.NAMES[state], reason=reason)
            self.state = state
        self.reason = reason

    def healthy(self) -> bool:
        return self.state in (self.LIVE, self.PAGE_READY)


# ---------------------------------------------------------------- probe
class Probe:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.stop = False
        self.dedup = Dedup()
        self.health = Health(None)
        self._raw_health_set = self.health.set
        self.health.set = self._health_set_wrapped
        self.net_recent = deque(maxlen=300)       # relevant network frames for correlation
        self.net_seen_counter = Counter()         # transport discovery stats
        self.candidate_endpoints = OrderedDict()  # url -> evidence
        self.last_challenge_check = mono_ms()
        self.last_page_state = 0
        self.page_state_fails = 0
        self.max_delivery_lag = 0
        self.tg = TelegramNotifier(C, logger=lambda **kw: jlog(log, "tg", **kw))
        self._startup_sent = False
        self._last_data_mono = None
        self._last_quiet_mono = 0
        self._last_rows_attached = 0
        self.start_mono = 0
        self.last_resource_log = mono_ms()
        self.last_dedup_cleanup = mono_ms()
        self.recovery_level = 0
        self.recovery_attempts = 0
        self.recovery_started = 0
        self.last_recovery_cycle = 0
        self.clock_anchor_perf = None             # JS perf.now at anchor
        self.clock_anchor_mono = None             # our mono at anchor receipt (approx)
        self.stats = Counter()
        self.page = None
        self.context = None
        self.browser = None
        self.pw = None
        self._console_budget = deque(maxlen=5)

    # ------------------------------------------------------------ main
    def run(self):
        C.LOG_DIR.mkdir(exist_ok=True)
        self.start_mono = mono_ms()
        self._last_data_mono = self.start_mono
        self.tg.start()
        if self.tg.enabled:
            jlog(log, "tg_enabled", chat_id=str(C.TELEGRAM_CHAT_ID)[:6] + "...")
        C.OUT_DIR.mkdir(exist_ok=True)
        signal.signal(signal.SIGINT, self._sig)
        signal.signal(signal.SIGTERM, self._sig)
        jlog(log, "probe_start", headed=C.HEADED, url=C.URL)

        self.pw = sync_playwright().start()
        try:
            self._launch_browser()
            self.health.set(Health.BOOTSTRAPPING, "browser up")
            while not self.stop:
                try:
                    item = self.q.get(timeout=0.1)
                except queue.Empty:
                    item = None
                    self._pump()   # keep JS->Python binding delivery realtime
                if item is not None:
                    try:
                        self._handle(item)
                    except Exception:
                        jlog(log, "handler_error", tb=traceback.format_exc()[-800:])
                    for _ in range(500):          # drain burst; keeps heartbeats realtime
                        try:
                            self._handle(self.q.get_nowait())
                        except queue.Empty:
                            break
                        except Exception:
                            jlog(log, "handler_error", tb=traceback.format_exc()[-800:])
                self._tick()
        finally:
            try:
                self.tg.stop(final_message="🛑 FXStreet probe stopped")
            except Exception:
                pass
            jlog(log, "probe_stop", stats=dict(self.stats), tg=self.tg.stats_dict(),
                 dedup_dups=self.dedup.duplicates,
                 dedup_revisions=self.dedup.revisions)
            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                pass
            try:
                self.pw.stop()
            except Exception:
                pass

    def _sig(self, *_):
        self.stop = True

    # ------------------------------------------------------------ browser
    def _launch_browser(self):
        jlog(log, "browser_launch", headed=C.HEADED)
        headless = C.HEADLESS if not C.HEADED else False
        base = dict(headless=headless, slow_mo=C.SLOW_MO_MS, args=C.LAUNCH_ARGS)

        candidates = []
        exe = getattr(C, "CHROME_EXECUTABLE_PATH", None)
        if exe:
            candidates.append(("exe:" + exe, {"executable_path": exe}))
        ch = getattr(C, "BROWSER_CHANNEL", None)
        if ch:
            candidates.append(("channel:" + ch, {"channel": ch}))
        for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
            if os.path.exists(p):
                candidates.append(("auto:" + p, {"executable_path": p}))
        candidates.append(("bundled-chromium", {}))

        last_err = None
        self.browser = None
        for label, extra in candidates:
            try:
                jlog(log, "browser_attempt", label=label)
                self.browser = self.pw.chromium.launch(**{**base, **extra})
                jlog(log, "browser_selected", label=label)
                break
            except Exception as e:
                last_err = e
                jlog(log, "browser_attempt_failed", label=label, error=str(e)[:180])
        if self.browser is None:
            raise last_err
        self._new_context_and_page()

    def _new_context_and_page(self):
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
        self.context = self.browser.new_context(
            user_agent=C.USER_AGENT,
            viewport=C.VIEWPORT,
            locale=C.LOCALE,
            timezone_id=C.TIMEZONE_ID,
        )
        self.context.set_default_timeout(15_000)
        self.context.add_init_script(Path("js/observer.js").read_text(encoding="utf-8"))
        if getattr(C, "MASK_WEBDRIVER", False):
            self.context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome=window.chrome||{runtime:{}};"
                "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            )
        self._new_page()

    def _new_page(self):
        if self.page:
            try:
                self.page.close()
            except Exception:
                pass
        self.page = self.context.new_page()
        self._wire_page(self.page)
        try:
            self.page.goto(C.URL, wait_until="domcontentloaded", timeout=60_000)
            jlog(log, "page_goto", url=redact(C.URL))
            try:
                self.page.bring_to_front()
            except Exception as e:
                jlog(log, "bring_to_front_error", error=str(e)[:150])
        except Exception as e:
            jlog(log, "page_goto_error", error=str(e)[:300])
            self.health.set(Health.DEGRADED, f"goto failed: {e}")

    def _wire_page(self, page):
        page.on("websocket", self._on_ws)
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)
        page.on("crash", lambda p: self.q.put(("page_crash", {})))
        page.on("dialog", lambda d: (jlog(log, "dialog_dismissed", text=d.message[:80]), d.dismiss()))
        page.on("console", self._on_console)
        page.expose_function("probeEmit", lambda s: self.q.put(("js", s)))

    # ------------------------------------------------------------ network handlers
    def _on_ws(self, ws):
        url = redact(ws.url)
        self.net_seen_counter["websocket"] += 1
        self.candidate_endpoints.setdefault(url, {"kind": "websocket", "hits": 0})
        self.candidate_endpoints[url]["hits"] += 1
        jlog(log, "ws_open", url=url[:200])
        ws.on("framereceived", lambda payload: self._on_frame(ws.url, payload, "recv"))
        ws.on("framesent", lambda payload: self._on_frame(ws.url, payload, "sent"))
        ws.on("close", lambda: (jlog(log, "ws_close", url=redact(ws.url)[:200]),
                                self.q.put(("ws_close", {"url": ws.url}))))

    def _on_frame(self, url, payload, direction):
        preview = redact(str(payload))[:400]
        relevant = self._is_relevant(preview)
        if not relevant and len(payload) <= 64:
            self.stats["ws_ping_frames"] += 1
            # keepalives on the FXStreet hubs prove the realtime connection
            # is alive even when no release traffic flows (quiet market)
            if "fxstreet" in url or "signalr" in url:
                self.health.last_net_activity_mono = mono_ms()
            return
        rec = {"mono": mono_ms(), "wall": wall_ms(), "kind": "ws_frame",
               "url": redact(url)[:200], "direction": direction,
               "size": len(payload), "preview": preview, "relevant": relevant}
        self.stats["ws_frames"] += 1
        if relevant:
            self.stats["ws_relevant"] += 1
            self.health.last_net_activity_mono = mono_ms()
            self.net_recent.append(rec)
        jlog(log, "ws_frame", **{k: rec[k] for k in
            ("mono", "url", "direction", "size", "relevant")}, preview=preview if relevant else "")

    def _on_request(self, req):
        rt = req.resource_type
        if rt in ("xhr", "fetch", "eventsource"):
            self.net_seen_counter[rt] += 1
            url = redact(req.url)
            if is_ad_url(url):
                self.stats["ad_skipped"] += 1
                return
            self.candidate_endpoints.setdefault(url, {"kind": rt, "hits": 0})
            self.candidate_endpoints[url]["hits"] += 1
            jlog(log, "http_request", rt=rt, method=req.method, url=url[:250])
            if rt == "eventsource":
                jlog(log, "transport_note", msg="SSE stream opened (frames not exposed by Playwright; "
                                                "presence + response body snapshots are the signal)")

    def _on_response(self, resp):
        if resp.request.is_navigation_request() and "fxstreet" in resp.url:
            jlog(log, "nav_response", status=resp.status, url=redact(resp.url)[:200])
        rt = resp.request.resource_type
        if rt not in ("xhr", "fetch", "eventsource"):
            return
        status = resp.status
        ctype = (resp.headers.get("content-type") or "")
        url = redact(resp.url)
        if is_ad_url(url):
            self.stats["ad_skipped"] += 1
            return
        if status in (401, 403, 429, 500, 502, 503):
            jlog(log, "http_status_alert", status=status, rt=rt, url=url[:250])
        if rt == "eventsource" or "text/event-stream" in ctype:
            self.health.last_net_activity_mono = mono_ms()
        if not C.CAPTURE_HTTP_BODIES:
            return
        try:
            clen = int(resp.headers.get("content-length") or 0)
            if clen > C.MAX_HTTP_BODY_BYTES:
                return
            if "json" not in ctype and "text" not in ctype:
                return
            body = resp.body().decode("utf-8", "replace")
            preview = redact(body)[:400]
            relevant = self._is_relevant(preview)
            if relevant:
                self.stats["http_relevant"] += 1
                self.health.last_net_activity_mono = mono_ms()
                rec = {"mono": mono_ms(), "wall": wall_ms(), "kind": "http",
                       "url": url[:200], "direction": "recv", "status": status,
                       "size": len(body), "preview": preview, "relevant": True}
                self.net_recent.append(rec)
            jlog(log, "http_response", rt=rt, status=status, size=len(body),
                 relevant=relevant, url=url[:250], preview=preview if relevant else "")
        except Exception:
            pass  # body unavailable (stream, aborted, etc.)

    def _on_request_failed(self, req):
        err = (req.failure or "")
        url = redact(req.url)
        if is_ad_url(url):
            self.stats["ad_skipped"] += 1
            return
        jlog(log, "request_failed", rt=req.resource_type, url=url[:250], error=str(err)[:200])
        if "err_name_not_resolved" in str(err).lower() or "dns" in str(err).lower():
            self.health.set(Health.DEGRADED, "dns failure")

    def _on_console(self, msg):
        if msg.type in ("error", "warning"):
            jlog(log, "js_console", level=msg.type, text=redact(msg.text)[:300])

    @staticmethod
    def _is_relevant(preview: str) -> bool:
        low = preview.lower()
        return any(k in low for k in C.NET_RELEVANT_KEYWORDS)

    # ------------------------------------------------------------ JS message handling
    def _handle(self, item):
        kind, payload = item
        if kind == "js":
            self._on_js(payload)
        elif kind == "page_crash":
            jlog(log, "page_crash")
            self.health.set(Health.DEGRADED, "page crash")
            self._start_recovery("page crash")
        elif kind == "ws_close":
            jlog(log, "ws_close_event", url=redact(payload.get("url", ""))[:200])
            # a single WS close is normal (idle hubs close); only stale-combo triggers recovery via _tick

    def _on_js(self, s: str):
        try:
            ev = json.loads(s)
        except Exception:
            jlog(log, "js_parse_error", raw=s[:300])
            return
        t = ev.get("type")
        now = mono_ms()

        if t == "clock_anchor":
            href = ev.get("href") or ""
            if "fxstreet" in href or self.clock_anchor_mono is None:
                self.clock_anchor_perf = ev.get("perf_now")
                self.clock_anchor_mono = now
            self.stats["clock_anchors"] += 1
            jlog(log, "clock_anchor", perf_now=ev.get("perf_now"), js_wall=ev.get("js_wall_time"))
        elif t == "heartbeat":
            lag = wall_ms() - ev.get("js_wall_time", wall_ms())
            if lag > self.max_delivery_lag:
                self.max_delivery_lag = lag
            if lag > 2000:
                jlog(log, "delivery_lag", lag_ms=lag)
            self.health.last_heartbeat_mono = now
            # heartbeat receipt IS page-liveness evidence: a quiet market has
            # no calendar mutations/relevant frames, but the page is alive.
            # without this, every quiet ~4min window falsely trips DEGRADED.
            self.health.last_dom_activity_mono = now
            self._last_rows_attached = ev.get("rows_attached", 0)
            h = self.health
            if ev.get("rows_attached", 0) > 0:
                if not h.calendar_seen:
                    h.calendar_seen = True
                    jlog(log, "calendar_detected", rows=ev.get("rows_attached"))
                    if h.state == Health.BOOTSTRAPPING:
                        h.set(Health.PAGE_READY, "calendar rows present")
                if h.state == Health.PAGE_READY and (
                        now - h.last_net_activity_mono < 120_000 or
                        ev.get("ms_since_last_mutation", 999999) < 60_000):
                    h.realtime_seen = True
                    h.set(Health.LIVE, "realtime activity confirmed")
            drift = ev.get("interval_drift_pct", 100)
            if drift is not None and drift < 80:
                jlog(log, "throttle_warning", interval_drift_pct=drift,
                     visibility=ev.get("visibilityState"), hidden=ev.get("document_hidden"))
            jlog(log, "heartbeat", **{k: ev.get(k) for k in (
                "js_wall_time", "visibilityState", "document_hidden", "interval_drift_pct",
                "raf_per_sec", "rows_attached", "ms_since_last_mutation", "online")})
        elif t == "dom_census":
            self.health.last_dom_activity_mono = now
            C.OUT_DIR.mkdir(exist_ok=True)
            C.CENSUS_OUT.write_text(json.dumps(ev, indent=1, ensure_ascii=False), encoding="utf-8")
            jlog(log, "dom_census_saved", rows=ev.get("candidateRowsFound"),
                 title=ev.get("title", "")[:120])
        elif t == "economic_event_change":
            self.health.last_dom_activity_mono = now
            self._on_dom_event(ev, now)
        elif t == "js_error":
            jlog(log, "js_error", message=str(ev.get("message"))[:300], source=str(ev.get("source", ""))[:120])
            self.health.low_conf_streak += 0  # js errors tracked, parser streak handled below
        elif t == "js_log":
            jlog(log, "js_log", level=ev.get("level", "info"), message=str(ev.get("message"))[:300])
        else:
            jlog(log, "js_unknown", raw=s[:200])

    # ------------------------------------------------------------ release pipeline
    def _on_dom_event(self, ev: dict, now: int):
        conf = ev.get("confidence", 0)
        subtype = ev.get("subtype")
        key = ev.get("event_key") or "unknown"
        content_hash = hashlib.sha1(json.dumps(
            [ev.get(k) for k in ("actual", "revised", "consensus", "forecast", "previous")],
            sort_keys=True).encode()).hexdigest()[:16]

        if conf < 50:
            self.health.low_conf_streak += 1
            jlog(log, "low_confidence", streak=self.health.low_conf_streak, confidence=conf,
                 event_key=key, sample=ev.get("row_text_sample", "")[:120])
            if self.health.low_conf_streak >= 3 and self.health.state in (Health.LIVE, Health.PAGE_READY):
                self.health.set(Health.DEGRADED, "parser confidence low")
                self._start_recovery("parser confidence low")
            return
        self.health.low_conf_streak = 0

        if self.health.challenge_detected:
            self.stats["suppressed_challenge"] += 1
            return  # never emit from an untrusted page

        verdict = self.dedup.classify(key, content_hash, now,
                                      has_actual=bool(ev.get("actual")))
        if verdict == "DUP":
            self.stats["dup_suppressed"] += 1
            return
        if subtype == "CORRECTION":
            self.stats["correction_logged"] += 1
            jlog(log, "correction_logged", event_key=key, event=ev.get("event_name"))
            return
        if subtype == "REVISION" and not C.ALLOW_REVISIONS:
            self.stats["revision_logged"] += 1
            jlog(log, "revision_suppressed", event_key=key, actual=ev.get("actual"),
                 old=ev.get("old_actual"))
            self._mark_data()
            self.tg.revision(self._revision_message(ev), dedup_key=(key, content_hash))
            return

        # correlate with the fastest network observation of the same release
        net = self._correlate_net(ev, now)
        dom_mut_mono = self._perf_to_mono(ev.get("dom_mutation_perf_ms"))
        rec = {
            "type": "economic_release" if verdict == "NEW" else "economic_revision",
            "occurrence_id": ev.get("event_id"),
            "event_key": key,
            "event_name": ev.get("event_name"),
            "currency": ev.get("currency"),
            "country": ev.get("country"),
            "scheduled_time": ev.get("scheduled_time"),
            "impact": ev.get("impact"),
            "previous": ev.get("previous"),
            "forecast": ev.get("forecast"),
            "actual": ev.get("actual"),
            "revised": ev.get("revised"),
            "confidence": conf,
            "t_network_mono_ms": net["mono"] if net else None,
            "t_dom_mutation_mono_ms": dom_mut_mono,
            "t_python_mono_ms": now,
            "network_to_dom_ms": (dom_mut_mono - net["mono"]) if (net and dom_mut_mono) else None,
            "dom_to_python_ms": (now - dom_mut_mono) if dom_mut_mono else None,
            "network_to_python_ms": (now - net["mono"]) if net else None,
            "correlation": net["how"] if net else "none",
            "detector": "dom",
            "delivery_lag_ms": (wall_ms() - ev["js_wall_time"]) if ev.get("js_wall_time") else None,
        }
        releases_log.info(json.dumps(rec, ensure_ascii=False))
        self._mark_data()
        self.tg.release(self._release_message(rec), dedup_key=(key, content_hash))
        jlog(log, "release_emitted", event=rec["event_name"], currency=rec["currency"],
             actual=rec["actual"], latency_total_ms=rec["network_to_python_ms"])
        # benchmark file (A/B method comparison)
        with open(C.BENCH_OUT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _correlate_net(self, ev: dict, now: int):
        """Find the network frame that likely carried this release."""
        name = (ev.get("event_name") or "").lower()
        cur = ev.get("currency")
        win = C.NET_CORRELATION_WINDOW_MS
        best = None
        for rec in reversed(self.net_recent):
            if now - rec["mono"] > win:
                break
            pv = rec["preview"].lower()
            how = None
            if ev.get("event_id") and str(ev.get("event_id")).lower() in pv:
                how = "id"
            elif cur and cur.lower() in pv and any(
                    w in pv for w in name.split() if len(w) >= 5):
                how = "currency+name"
            elif cur and cur.lower() in pv:
                how = "currency"
            if how:
                if best is None or rec["mono"] < best["mono"]:
                    best = {"mono": rec["mono"], "how": how}
        return best

    def _perf_to_mono(self, perf_ms):
        if perf_ms is None or self.clock_anchor_perf is None or self.clock_anchor_mono is None:
            return None
        return self.clock_anchor_mono + int(perf_ms - self.clock_anchor_perf)

    # ------------------------------------------------------------ periodic checks
    def _tick(self):
        now = mono_ms()
        h = self.health

        if now - self.last_page_state > 30_000:
            self.last_page_state = now
            self._page_state_poll()

        if h.state in (Health.PAGE_READY, Health.LIVE) and not h.challenge_detected \
                and now - self.last_challenge_check > C.CHALLENGE_CHECK_S * 1000:
            self.last_challenge_check = now
            self._check_challenge()

        if h.state == Health.LIVE:
            stale_dom = (now - h.last_dom_activity_mono) > C.STALE_S * 1000
            stale_net = (now - h.last_net_activity_mono) > C.STALE_S * 1000
            hb_dead = (now - h.last_heartbeat_mono) > C.HEARTBEAT_TIMEOUT_S * 1000
            if hb_dead:
                h.set(Health.DEGRADED, "js heartbeat lost")
                self._start_recovery("heartbeat lost")
            elif stale_dom and stale_net:
                h.set(Health.DEGRADED, "page stale (no dom, no network)")
                self._start_recovery("stale page")
            elif stale_net and (now - h.last_dom_activity_mono) > 15 * 60_000:
                jlog(log, "network_quiet_warning",
                     msg="no relevant network activity for >15min (dom still alive; "
                         "may be quiet market or transport switch — watch census)")

        if h.state == Health.DEGRADED and not h.challenge_detected:
            self._recovery_tick(now)

        qi = getattr(C, "TG_QUIET_HEARTBEAT_S", 0)
        if qi and self.tg.enabled and self._last_data_mono:
            if (now - self._last_data_mono > qi * 1000
                    and now - self._last_quiet_mono > qi * 1000):
                self._last_quiet_mono = now
                self.tg.quiet(
                    f"💤 Quiet {qi // 3600}h — no new releases\n"
                    f"state {Health.NAMES[self.health.state]} | "
                    f"{self._last_rows_attached} rows | "
                    f"ws keepalives {self.stats.get('ws_ping_frames', 0)} | "
                    f"uptime {fmt_uptime(now - self.start_mono)}")

        if now - self.last_resource_log > C.RESOURCE_LOG_S * 1000:
            self.last_resource_log = now
            self._resource_log()

        if now - self.last_dedup_cleanup > C.DEDUP_CLEANUP_S * 1000:
            self.last_dedup_cleanup = now
            self.dedup._trim(now)

    def _page_state_poll(self):
        """Python-driven page probe: independent of JS timers/throttling.
        Reports what page we're really on, whether our observer is installed,
        automation flags, and challenge markers - in EVERY health state."""
        try:
            raw = self.page.evaluate(_PAGE_STATE_JS)   # no timeout kwarg in sync API;
            self.page_state_fails = 0                  # default_timeout bounds it instead
            st = json.loads(raw)
            jlog(log, "page_state",
                 href=redact(st.get("href", ""))[:200], title=st.get("title", "")[:120],
                 ready=st.get("readyState"), vis=st.get("vis"),
                 probe=st.get("probe"), emit=st.get("emit"),
                 webdriver=st.get("webdriver"), rows=st.get("rows"),
                 body=redact(st.get("body", ""))[:280])
            if st.get("rows", 0) and not self.health.calendar_seen:
                self.health.calendar_seen = True
                self.health.last_dom_activity_mono = mono_ms()
                jlog(log, "calendar_detected", rows=st.get("rows"), source="page_state")
                if self.health.state == Health.BOOTSTRAPPING:
                    self.health.set(Health.PAGE_READY, "calendar rows present")
            if st.get("probe") is False and st.get("readyState") == "complete":
                jlog(log, "observer_missing", action="re-injecting")
                try:
                    self.page.evaluate(Path("js/observer.js").read_text(encoding="utf-8"))
                except Exception as e:
                    jlog(log, "reinject_error", error=str(e)[:200])
            blob = ((st.get("title") or "") + "\n" + (st.get("body") or "")).lower()
            hit = next((m for m in C.CHALLENGE_MARKERS if m in blob), None)
            if hit and not self.health.challenge_detected:
                self.health.challenge_detected = True
                self.health.set(Health.FAILED, "challenge/block page detected: " + hit)
                self._save_snapshot("challenge")
                jlog(log, "challenge_detected", marker=hit)
            elif not hit and self.health.challenge_detected:
                self.health.challenge_detected = False
                jlog(log, "challenge_cleared")
                if self.health.state == Health.FAILED:
                    self.health.set(Health.BOOTSTRAPPING, "challenge cleared")
        except Exception as e:
            self.page_state_fails += 1
            msg = str(e)
            if isinstance(e, TypeError):
                cls = "probe_bug"                 # our own code is wrong
            elif "Target closed" in msg or "crash" in msg.lower():
                cls = "browser_failure"
            elif "Timeout" in msg:
                cls = "page_unresponsive"
            else:
                cls = "unknown"
            jlog(log, "page_state_error", fails=self.page_state_fails,
                 error_class=cls, error=msg[:200])
            if self.page_state_fails >= 3 and self.health.state in (Health.PAGE_READY, Health.LIVE):
                self.health.set(Health.DEGRADED, "page unresponsive to evaluate")
                self._start_recovery("page unresponsive")

    def _check_challenge(self):
        try:
            title = (self.page.title() or "").lower()
            body = self.page.locator("body").inner_text(timeout=3000)[:800].lower()
        except Exception:
            return
        blob = title + "\n" + body
        hit = next((m for m in C.CHALLENGE_MARKERS if m in blob), None)
        if hit and not self.health.challenge_detected:
            self.health.challenge_detected = True
            self.health.set(Health.FAILED, f"challenge/block page detected: '{hit}'")
            self._save_snapshot("challenge")
            jlog(log, "challenge_detected", marker=hit,
                 action="parsing untrusted; releases suppressed; manual recovery needed")
        elif not hit and self.health.challenge_detected:
            self.health.challenge_detected = False
            jlog(log, "challenge_cleared")
            if self.health.state == Health.FAILED:
                self.health.set(Health.BOOTSTRAPPING, "challenge cleared")

    def _save_snapshot(self, tag: str):
        try:
            html = self.page.content()
            p = C.OUT_DIR / f"page_snapshot_{tag}_{int(time.time())}.html"
            p.write_text(html[:2_000_000], encoding="utf-8")
            jlog(log, "snapshot_saved", path=str(p))
        except Exception as e:
            jlog(log, "snapshot_error", error=str(e)[:200])

    # ------------------------------------------------------------ recovery ladder
    def _start_recovery(self, reason: str):
        if self.health.state == Health.RECOVERING:
            return
        self.health.set(Health.RECOVERING, reason)
        self.recovery_level = max(0, self.recovery_level)
        self.recovery_attempts = 0
        self.recovery_started = mono_ms()
        jlog(log, "recovery_start", reason=reason, level=self.recovery_level)

    def _recovery_tick(self, now: int):
        if now - self.recovery_started < C.RECOVERY_VERIFY_S * 1000:
            return  # let the current action prove itself
        if self.health.last_heartbeat_mono and \
                now - self.health.last_heartbeat_mono < C.HEARTBEAT_TIMEOUT_S * 1000 and \
                self.health.calendar_seen:
            self.recovery_attempts = 0
            self.recovery_level = 0
            self.health.set(Health.PAGE_READY, "recovered")
            jlog(log, "recovery_success")
            return
        if self.recovery_attempts >= C.RECOVERY_MAX_AT_LEVEL:
            self.recovery_attempts = 0
            self.recovery_level += 1
            if self.recovery_level > 4:
                self.recovery_level = 0
                if now - self.last_recovery_cycle < C.LEVEL_COOLDOWN_S * 1000:
                    return
                self.last_recovery_cycle = now
                self.health.set(Health.FAILED, "recovery ladder exhausted")
                jlog(log, "recovery_failed", msg="manual intervention required")
                return
        self.recovery_attempts += 1
        self._recovery_action(self.recovery_level)
        self.recovery_started = now

    def _recovery_action(self, level: int):
        jlog(log, "recovery_action", level=level)
        try:
            if level == 0:
                self.page.evaluate("void 0")  # nudge; re-anchor clock
            elif level == 1:
                self.page.reload(wait_until="domcontentloaded", timeout=60_000)
            elif level == 2:
                self._new_page()
            elif level == 3:
                self._new_context_and_page()   # fresh cookies/storage
            elif level == 4:
                try:
                    self.browser.close()
                except Exception:
                    pass
                self._launch_browser()
        except Exception as e:
            jlog(log, "recovery_action_error", level=level, error=str(e)[:300])

    def _pump(self):
        """Playwright's sync API dispatches exposed-binding callbacks only while a
        sync call is in flight; without periodic round-trips they batch (~30s
        observed with browser.version, which is a local property and does NOT
        pump the driver). A tiny evaluate round-trip does. Cheap: one CDP ping
        per idle 100ms loop."""
        try:
            if self.page is not None and not self.page.is_closed():
                self.page.evaluate("1")
        except Exception:
            pass

    # ------------------------------------------------------------ misc
    # ---------------- telegram notifications --------------------------------
    def _mark_data(self):
        self._last_data_mono = mono_ms()

    def _health_set_wrapped(self, state, reason=""):
        prev = self.health.state
        self._raw_health_set(state, reason)
        try:
            self._notify_health(prev, state, reason)
        except Exception:
            pass

    def _notify_health(self, prev, to, reason):
        if not getattr(C, "TG_NOTIFY_STATE", True):
            return
        if to == Health.DEGRADED:
            self.tg.state(f"⚠️ DEGRADED — {reason}")
        elif to == Health.RECOVERING:
            if self.recovery_level >= 1:
                self.tg.state(f"🔧 RECOVERING — level {self.recovery_level}: {reason}")
        elif to == Health.FAILED:
            self.tg.state(f"🔴 FAILED — {reason}. Manual intervention needed; "
                          f"release emission suppressed.", force=True)
        elif prev in (Health.RECOVERING, Health.DEGRADED) and \
                to in (Health.PAGE_READY, Health.LIVE):
            self.tg.state(f"✅ RECOVERED — {Health.NAMES[to]} ({reason})")
        if to == Health.PAGE_READY and not self._startup_sent:
            self._startup_sent = True
            self._mark_data()
            self.tg.startup(self._startup_text())

    def _startup_text(self):
        rows, lines = "?", []
        try:
            data = json.loads(self.page.evaluate(_STARTUP_SNAPSHOT_JS))
            rows = data.get("total")
            lines = ["• " + s for s in data.get("upcoming", [])[:6]]
        except Exception as e:
            lines.append(f"(snapshot failed: {str(e)[:80]})")
        body = (f"🟢 FXStreet probe LIVE\n"
                f"Calendar: {rows} rows attached\n"
                f"Next up (page time, UTC):")
        if lines:
            body += "\n" + "\n".join(lines)
        return body

    @staticmethod
    def _num(s):
        if s is None:
            return None
        s = str(s).strip().replace(",", "").replace("%", "").replace("$", "") \
            .replace("€", "").replace("£", "").replace("¥", "")
        mult = 1
        if s.endswith(("K", "k")):
            mult, s = 1e3, s[:-1]
        elif s.endswith(("M", "m")):
            mult, s = 1e6, s[:-1]
        elif s.endswith(("B", "b")):
            mult, s = 1e9, s[:-1]
        try:
            return float(s) * mult
        except ValueError:
            return None

    def _beat_miss(self, actual, forecast):
        a, f = self._num(actual), self._num(forecast)
        if a is None or f is None:
            return ""
        if a > f:
            return "✅ beat"
        if a < f:
            return "❌ miss"
        return "➖ inline"

    def _release_message(self, rec):
        lat = rec.get("network_to_python_ms") or rec.get("dom_to_python_ms")
        lat_s = f" | detected {lat} ms after update" if lat else ""
        bm = self._beat_miss(rec.get("actual"), rec.get("forecast"))
        bm_s = f" | {bm}" if bm else ""
        impact = f" | {rec['impact']}" if rec.get("impact") else ""
        when_s = f" | {rec['scheduled_time']}" if rec.get("scheduled_time") else ""
        cur = rec.get("currency") or "?"
        name = rec.get("event_name") or "?"
        return (f"📢 {cur} — {name}\n"
                f"Actual: {rec.get('actual')} | Forecast: {rec.get('forecast') or '—'} "
                f"| Previous: {rec.get('previous') or '—'}"
                f"{impact}{when_s}{bm_s}{lat_s}")

    def _revision_message(self, ev):
        cur = ev.get("currency") or "?"
        name = ev.get("event_name") or "?"
        return (f"✏️ REVISED — {cur} {name}\n"
                f"Actual: {ev.get('actual')} (was {ev.get('old_actual')})")

    def _resource_log(self):
        rss_mb = _rss_mb()
        jlog(log, "resources", rss_mb=(round(rss_mb, 1) if rss_mb is not None else None),
             max_delivery_lag_ms=self.max_delivery_lag,
             tg=self.tg.stats_dict(),
             ws_keepalives=self.stats.get("ws_ping_frames", 0),
             dedup_entries=len(self.dedup.store),
             net_frames_total=self.stats["ws_frames"], net_relevant=self.stats["ws_relevant"],
             http_relevant=self.stats["http_relevant"], queue_size=self.q.qsize(),
             dup_suppressed=self.stats["dup_suppressed"],
             endpoints_seen=len(self.candidate_endpoints))
        self.max_delivery_lag = 0
        if self.stats["ws_frames"] + self.stats["http_relevant"] == 0 and \
                self.health.state in (Health.PAGE_READY, Health.LIVE):
            self._print_discovery()

    def _print_discovery(self):
        lines = ["", "=" * 60, "FXStreet transport discovery", "-" * 60,
                 f"WebSocket connections: {self.net_seen_counter.get('websocket', 0)}",
                 f"SSE streams:           {self.net_seen_counter.get('eventsource', 0)}",
                 f"xhr/fetch:             {self.net_seen_counter.get('xhr', 0)}"
                 f"/{self.net_seen_counter.get('fetch', 0)}",
                 "Candidate realtime endpoints:"]
        for i, (url, meta) in enumerate(
                sorted(self.candidate_endpoints.items(),
                       key=lambda kv: -kv[1]["hits"])[:10], 1):
            lines.append(f"  {i}. [{meta['kind']} x{meta['hits']}] {url[:110]}")
        if not self.candidate_endpoints:
            lines.append("  (none yet — keep it running through a release)")
        lines.append("=" * 60)
        print("\n".join(lines), flush=True)


if __name__ == "__main__":
    Probe().run()
