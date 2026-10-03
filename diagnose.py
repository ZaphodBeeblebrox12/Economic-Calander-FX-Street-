#!/usr/bin/env python3
"""
FXStreet LIVE diagnostic — one-shot, separate from probe.py.

Answers with evidence:
  1. exact DOM selectors / identifiers (parser sample + attribute dump)
  2. FULL unfiltered network inventory   -> output/debug/network_inventory.json
  3. actual realtime transport (ws / sse / polling) with frame activity
  4. WebSocket listeners registered BEFORE navigation; per-socket stats
  5. framework/state evidence (React props on rows, __NEXT_DATA__, state hints)
  6. independent MutationObserver self-test with timestamps (hidden div only)
  7. parser output for 8 rendered rows (extraction correctness before a release)
  8/9. LIVE_FEED_STATUS report            -> output/diagnostics_report.txt

Run:  python diagnose.py     (~90 seconds, writes report, exits)
"""
import json
import os
import queue
import sys
import time
from collections import Counter
from pathlib import Path

import config as C

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not installed. Run: pip install -r requirements.txt")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

OUT = C.OUT_DIR / "debug"
OUT.mkdir(parents=True, exist_ok=True)

inventory = []          # every network event, unfiltered
ws_book = {}            # id(ws) -> stats
events_q = queue.Queue()


def mono_ms():
    return int(time.perf_counter() * 1000)


def wall_ms():
    return int(time.time() * 1000)


def redact(t):
    return t


def rec(kind, **kw):
    inventory.append({"mono_ms": mono_ms(), "wall_ms": wall_ms(), "kind": kind, **kw})


# ------------------------------------------------------------------ launch (same ladder as probe)
def launch(pw):
    headless = C.HEADLESS if not C.HEADED else False
    base = dict(headless=headless, slow_mo=C.SLOW_MO_MS, args=C.LAUNCH_ARGS)
    cands = []
    if getattr(C, "CHROME_EXECUTABLE_PATH", None):
        cands.append(("exe", {"executable_path": C.CHROME_EXECUTABLE_PATH}))
    ch = getattr(C, "BROWSER_CHANNEL", None)
    if ch:
        cands.append(("channel:" + ch, {"channel": ch}))
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(p):
            cands.append(("auto:" + p, {"executable_path": p}))
    cands.append(("bundled-chromium", {}))
    last = None
    for label, extra in cands:
        try:
            print(f"[launch] trying {label}", flush=True)
            b = pw.chromium.launch(**{**base, **extra})
            print(f"[launch] selected {label}", flush=True)
            return b, label
        except Exception as e:
            last = e
            print(f"[launch] failed {label}: {str(e)[:120]}", flush=True)
    raise last


# ------------------------------------------------------------------ network handlers (unfiltered)
def on_request(req):
    rec("request", rt=req.resource_type, method=req.method,
        url=req.url[:300])


def on_response(resp):
    rec("response", rt=resp.request.resource_type, status=resp.status,
        url=resp.url[:300])


def on_fail(req):
    rec("request_failed", rt=req.resource_type, error=str(req.failure)[:150],
        url=req.url[:300])


def on_ws(ws):
    key = id(ws)
    stats = ws_book[key] = {
        "url": ws.url[:300], "created_mono_ms": mono_ms(),
        "frames_sent": 0, "frames_recv": 0,
        "last_frame_mono_ms": None, "closed": None, "previews": [],
    }
    rec("ws_created", url=ws.url[:300])

    def got(payload, direction):
        stats[f"frames_{direction}"] += 1
        stats["last_frame_mono_ms"] = mono_ms()
        if len(stats["previews"]) < 6 and len(payload) > 2:
            stats["previews"].append({"dir": direction, "n": len(payload),
                                      "preview": str(payload)[:160]})
        rec("ws_frame", url=ws.url[:200], direction=direction, size=len(payload))

    ws.on("framesent", lambda p: got(p, "sent"))
    ws.on("framereceived", lambda p: got(p, "recv"))
    ws.on("close", lambda: (stats.__setitem__("closed", mono_ms()),
                            rec("ws_close", url=ws.url[:200])))
    try:
        ws.on("socketerror", lambda e: rec("ws_error", url=ws.url[:200], error=str(e)[:120]))
    except Exception:
        pass


# ------------------------------------------------------------------ in-page probes
PARSER_JS = """
(() => {
  const rows = Array.from(document.querySelectorAll('tr.fxs_c_row'));
  const parse = row => ({
    event_name: (row.querySelector('.fxs_c_name')||{}).textContent || null,
    country: row.querySelector('.fxs_c_flag [title]') ? row.querySelector('.fxs_c_flag [title]').getAttribute('title') : null,
    currency: (row.querySelector('.fxs_c_currency')||{}).textContent || null,
    scheduled_time: (row.querySelector('.fxs_c_time')||{}).textContent || null,
    impact: (((row.querySelector('.fxs_c_impact-icon')||{}).className || '').match(/fxs_c_impact-(high|medium|low|none)/) || [])[1] || null,
    actual: (row.querySelector('.fxs_c_actual')||{}).textContent || null,
    consensus: (row.querySelector('.fxs_c_consensus')||{}).textContent || null,
    previous: (row.querySelector('.fxs_c_previous')||{}).textContent || null,
    revised: (row.querySelector('.fxs_c_revised')||{}).textContent || null,
    occurrence_id: row.getAttribute('data-event-date-id'),
    event_id_dom: row.getAttribute('data-event-id'),
    row_attrs: Array.from(row.attributes).map(a => a.name + '=' + String(a.value).slice(0, 40))
  });
  const clean = o => { for (const k in o) if (typeof o[k] === 'string') o[k] = o[k].trim().replace(/\\s+/g, ' '); return o; };
  const samples = rows.slice(0, 8).map(r => clean(parse(r)));
  const fieldStats = {};
  rows.forEach(r => { const p = parse(r);
    ['actual','consensus','previous','impact','occurrence_id','country','currency']
      .forEach(k => { fieldStats[k] = (fieldStats[k]||0) + (p[k] ? 1 : 0); }); });
  return JSON.stringify({ rowCount: rows.length, samples, fieldStats });
})()
"""

STATE_JS = """
(() => {
  const out = {};
  out.nextData = !!window.__NEXT_DATA__;
  if (window.__NEXT_DATA__) out.nextDataKeys = Object.keys(window.__NEXT_DATA__).slice(0, 12);
  const row = document.querySelector('tr.fxs_c_row');
  if (row) {
    const keys = Object.keys(row).filter(k => k.indexOf('__react') === 0);
    out.reactKeysOnRow = keys;
    const pk = keys.find(k => k.indexOf('__reactProps$') === 0);
    if (pk) {
      try {
        out.reactPropsShallow = JSON.stringify(row[pk], (k, v) =>
          (v && typeof v === 'object') ? (Array.isArray(v) ? '[array]' : '[object]') : v).slice(0, 1800);
      } catch (e) { out.reactPropsShallow = 'ERR ' + e; }
    }
  }
  out.stateHints = Object.keys(window).filter(k => /redux|store|__STATE|__INITIAL|__APP|calendar|signalr/i.test(k)).slice(0, 30);
  out.jsonScripts = Array.from(document.querySelectorAll('script[type="application/json"]'))
    .map(s => s.id || s.getAttribute('data-name') || 'anon').slice(0, 20);
  out.calendarModel = (function(){ try {
      var m = window.calendarModel;
      if (!m) return null;
      var o = { type: typeof m, keys: Object.keys(m).slice(0, 40) };
      try { o.shallow = JSON.stringify(m, function(k, v) {
          return (v && typeof v === 'object') ? (Array.isArray(v) ? '[array ' + v.length + ']' : '[object]') : v;
        }).slice(0, 1200); } catch (e) { o.shallow = 'ERR ' + e; }
      return o;
    } catch (e) { return 'ERR ' + e; } })();
  return JSON.stringify(out);
})()
"""

SELFTEST_JS = """
(() => {
  const tMut = performance.now();
  const div = document.createElement('div');
  div.id = 'fx-diag-selftest';
  div.style.display = 'none';
  document.body.appendChild(div);
  const mo = new MutationObserver(muts => {
    const tCb = performance.now();
    mo.disconnect();
    window.diagEmit(JSON.stringify({
      type: 'selftest_result',
      t_perf_mut: tMut, t_perf_cb: tCb, t_wall_cb: Date.now(),
      mutations: muts.length
    }));
  });
  mo.observe(div, { childList: true, subtree: true, characterData: true });
  div.textContent = 'A';
  div.textContent = 'B';
  div.remove();
  return 'selftest-armed';
})()
"""


def run_selftest(page):
    r = page.evaluate(SELFTEST_JS)
    deadline = time.time() + 6
    while time.time() < deadline:
        try:
            kind, s = events_q.get(timeout=0.5)
        except queue.Empty:
            continue
        ev = json.loads(s)
        if ev.get("type") == "selftest_result":
            cb_delay = ev["t_perf_cb"] - ev["t_perf_mut"]
            lag = wall_ms() - ev["t_wall_cb"]
            return {"status": "PASS", "js_cb_delay_ms": round(cb_delay, 1),
                    "python_receipt_lag_ms": lag, "mutations_seen": ev["mutations"]}
    return {"status": "TIMEOUT (no selftest_result within 6s)"}


# ------------------------------------------------------------------ report
def build_report(page_info, parser, state, selftest, t_start):
    ws_list = list(ws_book.values())
    total_frames = sum(w["frames_sent"] + w["frames_recv"] for w in ws_list)
    now = mono_ms()
    # an open socket that completed its SignalR handshake IS the live connection;
    # data frames only arrive when a release occurs (quiet market = quiet socket)
    active_ws = [w for w in ws_list if not w["closed"]]
    sse = sum(1 for e in inventory if e.get("rt") == "eventsource")
    ad_domains = tuple(getattr(C, "AD_DOMAINS", ()))
    fetch_counts = Counter()
    for e in inventory:
        if e.get("rt") in ("fetch", "xhr") and e.get("kind") == "request":
            u = e.get("url", "")
            if any(dom in u for dom in ad_domains):
                continue
            fetch_counts[u.split("?")[0][:120]] += 1
    polling = [u for u, n in fetch_counts.items() if n >= 4]

    open_ws = [w for w in ws_list if not w["closed"]]
    if open_ws:
        transport = "WebSocket (SignalR) - %d socket(s) open, handshake complete" % len(open_ws)
    elif sse:
        transport = "SSE (EventSource)"
    elif polling:
        transport = "HTTP polling (recurring fetch/XHR)"
    else:
        transport = "NONE OBSERVED in window"

    L = []
    L.append("FXSTREET DIAGNOSTIC REPORT  (window: 75s after navigation)")
    L.append("=" * 60)
    L.append("")
    L.append("PAGE")
    L.append(f"  loaded:      {'YES' if page_info.get('title') else 'NO'}")
    L.append(f"  title:       {page_info.get('title')}")
    L.append(f"  readyState:  {page_info.get('readyState')}")
    L.append(f"  visible:     {page_info.get('vis')}  hidden={page_info.get('hidden')}")
    L.append(f"  challenge:   {'CHECK' if page_info.get('challenge') else 'NO'}")
    L.append("")
    L.append("DOM")
    L.append(f"  calendar rows: {parser.get('rowCount')}")
    L.append(f"  parser field coverage: {json.dumps(parser.get('fieldStats'))}")
    L.append(f"  observer self-test: {selftest}")
    L.append("")
    L.append("NETWORK (unfiltered inventory: %d events)" % len(inventory))
    L.append(f"  websockets created: {len(ws_list)}")
    for w in ws_list:
        L.append(f"    - {w['url'][:100]}")
        L.append(f"      frames sent={w['frames_sent']} recv={w['frames_recv']} "
                 f"closed={'yes' if w['closed'] else 'no'}")
    L.append(f"  SSE streams: {sse}")
    L.append(f"  polling candidates: {polling if polling else 'none'}")
    L.append("")
    L.append("REALTIME")
    L.append(f"  transport detected: {transport}")
    L.append(f"  connection active:  {'YES (' + str(len(active_ws)) + ' socket(s))' if active_ws else 'NO'}")
    L.append(f"  messages received:  {total_frames} ws frames in window")
    L.append("")
    L.append("IDENTITY")
    occ = parser.get("fieldStats", {}).get("occurrence_id", 0)
    L.append(f"  occurrence ID available: {'YES (' + str(occ) + '/' + str(parser.get('rowCount')) + ' rows)' if occ else 'NO'}")
    L.append("")
    L.append("FRAMEWORK / STATE")
    L.append(f"  __NEXT_DATA__: {state.get('nextData')}  keys={state.get('nextDataKeys')}")
    L.append(f"  react keys on row: {state.get('reactKeysOnRow')}")
    L.append(f"  react props (shallow): {str(state.get('reactPropsShallow'))[:400]}")
    L.append(f"  window state hints: {state.get('stateHints')}")
    cm = state.get('calendarModel')
    L.append(f"  window.calendarModel: {str(cm)[:500]}")
    L.append(f"  embedded JSON scripts: {state.get('jsonScripts')}")
    L.append("")
    L.append("PARSER SAMPLES (first rows)")
    for s in parser.get("samples", [])[:5]:
        L.append("  " + json.dumps(s, ensure_ascii=False)[:280])
    return "\n".join(L)


def main():
    pw = sync_playwright().start()
    browser, label = None, None
    try:
        browser, label = launch(pw)
        context = browser.new_context(
            user_agent=C.USER_AGENT, viewport=C.VIEWPORT,
            locale=C.LOCALE, timezone_id=C.TIMEZONE_ID)
        context.set_default_timeout(15_000)
        # context-level network listeners: EVERYTHING, no filters
        context.on("request", on_request)
        context.on("response", on_response)
        context.on("requestfailed", on_fail)
        page = context.new_page()
        page.on("websocket", on_ws)               # before navigation
        page.expose_function("diagEmit", lambda s: events_q.put(("js", s)))
        t_start = mono_ms()
        page.goto(C.URL, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(12)                             # SPA hydration + transports

        page_info = json.loads(page.evaluate(
            "JSON.stringify({title: document.title, readyState: document.readyState,"
            " vis: document.visibilityState, hidden: document.hidden,"
            " challenge: /just a moment|checking your browser|captcha/i.test(document.body ? document.body.innerText : '')})"))

        parser = json.loads(page.evaluate(PARSER_JS))
        state = json.loads(page.evaluate(STATE_JS))
        selftest = run_selftest(page)

        while mono_ms() - t_start < 75_000:        # observe ws/keepalive cadence
            time.sleep(0.2)

        report = build_report(page_info, parser, state, selftest, t_start)
        (OUT / "network_inventory.json").write_text(
            json.dumps(inventory, indent=1, ensure_ascii=False), encoding="utf-8")
        (OUT / "page_diagnostics.json").write_text(json.dumps(
            {"page": page_info, "parser": parser, "state": state,
             "selftest": selftest, "websockets": list(ws_book.values())},
            indent=1, ensure_ascii=False), encoding="utf-8")
        (C.OUT_DIR / "diagnostics_report.txt").write_text(report, encoding="utf-8")
        print("\n" + report)
        print("\n[wrote] output/debug/network_inventory.json")
        print("[wrote] output/debug/page_diagnostics.json")
        print("[wrote] output/diagnostics_report.txt")
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        pw.stop()


if __name__ == "__main__":
    main()
