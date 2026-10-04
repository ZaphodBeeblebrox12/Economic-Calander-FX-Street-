"""
diagnose_mql5.py — MQL5 economic calendar: census + probe (ROUND 2)

Round 1 result: page returns 200, 147 KB, NO Cloudflare — but 0 table rows.
Conclusion: the calendar is CLIENT-SIDE RENDERED; data arrives via XHR after
the HTML shell loads.

Round 2: static keyword scan of the HTML (is the data embedded in a script?)
+ one headless-browser pass capturing ALL xhr/fetch traffic (find the real
endpoint) + census of the RENDERED DOM (table or div-based rows).

Usage:
  python diagnose_mql5.py          # census -> mql5_map.json + verdict
  python diagnose_mql5.py --probe  # poll -> mql5_releases.jsonl (after GO)
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://www.mql5.com/en/economic-calendar"
ROOT = Path(__file__).resolve().parent
MAP_FILE = ROOT / "mql5_map.json"
OUT_FILE = ROOT / "mql5_releases.jsonl"
DIAG_FILE = ROOT / "mql5_diagnostic.json"
POLL_INTERVAL = 15
CF_MARKERS = ("just a moment", "cf_chl_opt", "cf-mitigated", "attention required")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, dict(e.headers), body
    except Exception as e:
        return None, {}, f"__FETCH_ERROR__ {type(e).__name__}: {e}"


# ------------------------------------------------------------------ parsing -
class DomCensus(HTMLParser):
    """Collect tr rows AND div clusters that look like calendar rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.cur_row, self.cur_cell = [], None, None
        self.div_stack, self.div_candidates = [], []
        self.in_script, self._buf, self.scripts = False, [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self.in_script, self._buf = True, []
        if self.in_script:
            return
        if tag == "tr":
            self.cur_row = {"attrs": a, "cells": []}
        elif tag in ("td", "th") and self.cur_row is not None:
            self.cur_cell = {"attrs": a, "text": []}
        elif tag == "div":
            cls = a.get("class", "")
            if re.search(r"calendar|event|ec-row|econ", cls, re.I):
                self.div_candidates.append({"attrs": a, "text": []})
            self.div_stack.append(a)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
            self.scripts.append("".join(self._buf))
            return
        if self.in_script:
            return
        if tag in ("td", "th") and self.cur_cell is not None:
            self.cur_cell["text"] = " ".join("".join(self.cur_cell["text"]).split())
            self.cur_row["cells"].append(self.cur_cell)
            self.cur_cell = None
        elif tag == "tr" and self.cur_row is not None:
            if self.cur_row["cells"]:
                self.rows.append(self.cur_row)
            self.cur_row = None
        elif tag == "div" and self.div_stack:
            self.div_stack.pop()

    def handle_data(self, data):
        if self.in_script:
            self._buf.append(data)
        else:
            if self.cur_cell is not None:
                self.cur_cell["text"].append(data)
            if self.div_candidates:
                self.div_candidates[-1]["text"].append(data)


def summarize_rows(rows, limit=6):
    sample = []
    for r in rows[:limit]:
        sample.append({"tr_attrs": r["attrs"],
                       "cells": [{"attrs": c["attrs"], "text": c["text"][:100]}
                                 for c in r["cells"]]})
    return {"total_rows": len(rows), "sample": sample}


def static_keyword_scan(html):
    keys = ["nonfarm", "cpi", "pmi", "gdp", "interest rate", "forecast",
            "actual", "previous", "calendar", "economic"]
    hits = {k: html.lower().count(k) for k in keys}
    ctx = []
    for m in re.finditer(r"(Nonfarm|Interest Rate|GDP|PMI)", html):
        ctx.append(html[max(0, m.start() - 120):m.start() + 180])
        if len(ctx) >= 3:
            break
    return hits, ctx


# ------------------------------------------------------- browser round trip -
def browser_capture():
    """One headless pass: capture all xhr/fetch, census the RENDERED DOM."""
    from playwright.sync_api import sync_playwright
    captured, saved_files = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(resp):
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            try:
                body = resp.text()
            except Exception:
                return
            entry = {"url": resp.url, "status": resp.status, "len": len(body),
                     "ct": resp.headers.get("content-type", "")}
            if re.search(r"forecast|actual|calendar|economic|event", body[:8000], re.I) \
               and (body.lstrip()[:1] in "{["):
                fname = "mql5_xhr_" + re.sub(r"\W+", "_", resp.url[-70:])[:60] + ".json"
                (ROOT / fname).write_text(body, encoding="utf-8")
                entry["saved"] = fname
                try:
                    j = json.loads(body)
                    entry["json_keys"] = list(j.keys())[:20] if isinstance(j, dict) \
                        else f"list[{len(j)}]"
                except Exception:
                    pass
            captured.append(entry)

        page.on("response", on_response)
        page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(12000)
        rendered = page.content()
        # also capture localStorage — some apps cache the payload there
        storage = page.evaluate("() => { const o={}; for (let i=0;i<localStorage.length;i++)"
                                " { const k=localStorage.key(i); o[k]=String(localStorage.getItem(k)).slice(0,500);} return o; }")
        browser.close()

    parser = DomCensus()
    parser.feed(rendered)
    return captured, parser, rendered, storage


def run_census():
    print("=== mql5 census — round 2 ===")
    report = {"round": 2, "run_at": datetime.now(timezone.utc).isoformat(), "base": BASE}

    st, hdrs, html = fetch(BASE)
    report["page"] = {"status": st, "len": len(html), "server": hdrs.get("Server", "")}
    print(f"  GET -> {st}, {len(html)} bytes")
    if st != 200 or any(m in html.lower() for m in CF_MARKERS):
        print("  !! blocked/CF — graft the investing.com Playwright machinery")
        report["blocked"] = True
        DIAG_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    # 1. is the data in the raw HTML at all?
    hits, ctx = static_keyword_scan(html)
    report["static_keywords"] = hits
    report["static_contexts"] = ctx
    print(f"  keyword hits in raw HTML: {hits}")

    # 2. plain-table parse of raw HTML (round-1 result expected: 0)
    p0 = DomCensus()
    p0.feed(html)
    report["raw_table"] = summarize_rows(p0.rows)

    # 3. browser pass: find the XHR + rendered DOM structure
    print("  headless browser pass (12s network watch)...")
    try:
        captured, parser, rendered, storage = browser_capture()
    except Exception as e:
        print(f"  browser pass failed: {type(e).__name__}: {e}")
        print("  (playwright needed for this step: pip show playwright)")
        report["browser_error"] = str(e)
        DIAG_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    report["xhr_calls"] = captured
    calendar_xhr = [c for c in captured if c.get("saved")]
    print(f"  xhr/fetch captured: {len(captured)}  calendar-like: {len(calendar_xhr)}")
    for c in calendar_xhr[:8]:
        print(f"    [{c['status']}] {c['url'][:90]}  keys={c.get('json_keys')}")

    report["rendered_table"] = summarize_rows(parser.rows)
    report["rendered_div_candidates"] = {
        "count": len(parser.div_candidates),
        "sample": [{"attrs": d["attrs"], "text": " ".join("".join(d["text"]).split())[:120]}
                   for d in parser.div_candidates[:6]],
    }
    report["localstorage"] = storage
    print(f"  rendered DOM: {len(parser.rows)} table rows, "
          f"{len(parser.div_candidates)} calendar-ish divs")

    # 4. embedded JSON in raw-HTML scripts (looser than round 1)
    blobs = [s for s in p0.scripts if len(s) > 400 and
             re.search(r"(forecast|actual|previous)", s, re.I)]
    report["script_blobs"] = [{"len": len(b), "head": b[:300]} for b in blobs[:3]]
    for i, b in enumerate(blobs[:3]):
        (ROOT / f"mql5_script_blob_{i}.txt").write_text(b, encoding="utf-8")
        print(f"  script blob {i}: {len(b)} chars -> saved")

    # ---- verdict / map
    transport = None
    if calendar_xhr:
        transport = {"type": "xhr_endpoint", "url": calendar_xhr[0]["url"],
                     "json_keys": calendar_xhr[0].get("json_keys"),
                     "saved_sample": calendar_xhr[0].get("saved")}
    elif len(parser.rows) >= 3:
        transport = {"type": "rendered_table", "row_count": len(parser.rows),
                     "sample": report["rendered_table"]["sample"][:2]}
    elif report["script_blobs"]:
        transport = {"type": "embedded_script_json", "blob": "mql5_script_blob_0.txt"}

    report["transport"] = transport
    DIAG_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\ndiagnostic -> {DIAG_FILE}")

    if transport:
        MAP_FILE.write_text(json.dumps({
            "transport": transport,
            "census_at": report["run_at"],
        }, indent=2), encoding="utf-8")
        print(f"map -> {MAP_FILE}")
        print(f"VERDICT: GO (transport={transport['type']})")
        if transport["type"] == "xhr_endpoint":
            print("  -> best case: probe becomes plain requests polling, no browser at all")
        return 0
    print("VERDICT: BLOCKED — inspect mql5_diagnostic.json")
    return 1


# ------------------------------------------------------------------- probe --
def run_probe():
    """Placeholder until round-2 census pins the transport. The probe body is
    written against the confirmed transport (same pattern as investing_probe's
    diff engine) once we see the real payload shape."""
    print("=== mql5 probe ===")
    if not MAP_FILE.exists():
        print("No mql5_map.json — run the census first.")
        return 1
    m = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    t = m["transport"]["type"]
    if t == "xhr_endpoint":
        print(f"transport: {t} -> {m['transport']['url']}")
        print("Census-confirmed polling probe lands next message; the endpoint is")
        print("the hard part and it is now known.")
    else:
        print(f"transport: {t} — probe needs the rendered-DOM/embedded path;")
        print("send mql5_diagnostic.json and the probe will be generated from it.")
    return 0


if __name__ == "__main__":
    if "--probe" in sys.argv:
        sys.exit(run_probe())
    sys.exit(run_census())
