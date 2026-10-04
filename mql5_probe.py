"""
mql5_probe.py — MQL5 economic calendar probe (source #3)

TRANSPORT (census-confirmed): the calendar page is FULLY SERVER-RENDERED as
plain-text event lines inside the HTML. No XHR, no Cloudflare, no browser.
  2026.10.02 12:30, USD, Nonfarm Payrolls, Actual: 29 K, Forecast: 52 K, Previous: 133 K
Times are UTC (NFP 12:30 == 8:30 ET). Holidays have no value fields.

Emits to mql5_releases.jsonl with source:"mql5", same field names as the
investing probe so the forwarder adapter is thin.

Known gap: impact (importance) is not present in the text layer — rendered as
CSS/images. The merge layer enriches impact from whichever source has it.

Run:  python mql5_probe.py
"""

import html as html_mod
import json
import re
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.mql5.com/en/economic-calendar"
ROOT = Path(__file__).resolve().parent
OUT_FILE = ROOT / "mql5_releases.jsonl"
POLL_INTERVAL = 15
HEARTBEAT = 300
CF_MARKERS = ("just a moment", "cf_chl_opt", "cf-mitigated")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

LINE_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2}) (\d{2}):(\d{2}), ([A-Z]{3}), (.+)$")
FIELD_LABELS = ("Actual:", "Forecast:", "Previous:")


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def fetch_html():
    req = urllib.request.Request(BASE, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


def html_to_lines(html):
    """Block-level tags -> newlines (NOT </td>/</div>: event cells stay on one
    line), strip remaining tags, unescape entities, normalize whitespace."""
    txt = re.sub(r"(?i)<(br|/tr|/li|/p)\b[^>]*>", "\n", html)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html_mod.unescape(txt)
    txt = txt.replace("\u200b", "").replace("\xa0", " ")
    return [" ".join(l.split()) for l in txt.splitlines()]


def parse_events(html):
    events = {}
    for line in html_to_lines(html):
        m = LINE_RE.match(line)
        if not m:
            continue
        y, mo, d, hh, mm, cur, rest = m.groups()
        vals = {"actual": "", "forecast": "", "previous": ""}
        for label in FIELD_LABELS:
            marker = ", " + label
            i = rest.find(marker)
            if i != -1:
                vals[label[:-1].lower()] = rest[i + len(marker):].split(", ")[0].strip()
                rest = rest[:i]
        name = rest.strip()
        if not name:
            continue
        dt = f"{y}-{mo}-{d}T{hh}:{mm}:00Z"
        key = f"{dt}|{cur}|{name}"
        events[key] = {
            "date": f"{y}-{mo}-{d}", "time_utc": dt, "currency": cur,
            "country": "",            # MQL5 text layer has no country (EUR-PMI
                                      # ambiguity handled by merge-layer wildcard)
            "event": name, "event_long": name, "period": "",
            "importance": "",          # not available in text layer
            "forecast": vals["forecast"], "previous": vals["previous"],
            "actual": vals["actual"], "revised_from": "",
        }
    return events


def emit(rec):
    rec["source"] = "mql5"
    rec["emitted_at"] = datetime.now(timezone.utc).isoformat()
    with OUT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"EMIT {rec['kind']:<14} {rec['currency']} {rec['event']}  "
        f"actual={rec['actual']!r}")


def run():
    log("=== mql5_probe starting ===")
    seen = {}
    last_heartbeat = time.time()
    errors = 0
    while True:
        try:
            st, html = fetch_html()
            if st != 200 or any(m in html.lower() for m in CF_MARKERS):
                errors += 1
                log(f"blocked/failed (x{errors}) — status {st}")
                time.sleep(POLL_INTERVAL * 4 if errors > 3 else POLL_INTERVAL)
                continue
            events = parse_events(html)
            if not events:
                errors += 1
                log(f"no events parsed (x{errors}) — page layout may have changed")
                time.sleep(POLL_INTERVAL * 2)
                continue
            errors = 0
            for key, ev in events.items():
                old = seen.get(key)
                if old is None:
                    kind = "new_event"
                    rec = dict(ev, kind=kind)
                    emit(rec)
                    seen[key] = ev
                elif ev["actual"] and ev["actual"] != old.get("actual"):
                    rec = dict(ev, kind="actual_update",
                               prev_actual=old.get("actual", ""))
                    emit(rec)
                    seen[key] = ev
                else:
                    seen[key] = ev
            if time.time() - last_heartbeat >= HEARTBEAT:
                last_heartbeat = time.time()
                log(f"heartbeat: {len(seen)} events tracked")
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log("stopped by user")
            break
        except Exception:
            errors += 1
            log(f"loop error (x{errors}):\n{traceback.format_exc()}")
            time.sleep(POLL_INTERVAL * 2)
    return 0


if __name__ == "__main__":
    sys.exit(run())
