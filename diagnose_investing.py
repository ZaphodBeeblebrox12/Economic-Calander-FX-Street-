"""
diagnose_investing.py — Investing.com census, ROUND 3.1 (feature/investing-source)

Round 3.0 lesson: launch_persistent_context can die instantly on Windows if a
zombie msedge.exe holds the profile dir (TargetClosedError at launch).
Round 3.1: persistent profile with ABSOLUTE path + automatic fallback to the
plain launch() + storage_state combo that worked in rounds 1-2.

Run:    python diagnose_investing.py
Output: investing_diagnostic.json
        investing_profile/   (persistent mode — gitignore)
        investing_state.json (fallback mode — gitignore)
"""

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CALENDAR_URL = "https://www.investing.com/economic-calendar/"
ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / "investing_profile"
STATE_FILE = ROOT / "investing_state.json"
DIAG_FILE = ROOT / "investing_diagnostic.json"
HEADED = True
MANUAL_SOLVE = True

report = {
    "round": "3.1",
    "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "launch_mode": None,
    "challenge_events": [],
    "network": [],
    "embedded_json": {},
    "ssr_parse": {},
    "date_param_test": {},
    "notes": [],
}


def note(msg):
    report["notes"].append(msg)
    print(f"  [note] {msg}")


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


def is_challenged(page) -> bool:
    try:
        title = page.title().lower()
    except Exception:
        title = ""
    try:
        html = page.content().lower()
    except Exception:
        html = ""
    return ("just a moment" in title or "performing security verification" in html
            or "verifies you are not a bot" in html or "verify you are human" in html
            or "cf_chl_opt" in html)


def wait_out_challenge(page, context_label):
    if not is_challenged(page):
        return True
    report["challenge_events"].append({"where": context_label, "outcome": "detected"})
    if not MANUAL_SOLVE:
        return False
    print(f"\n  >>> Cloudflare challenge at {context_label}.")
    print("  >>> Tick the checkbox ONCE if it appears; do not re-tick.")
    print("  >>> Cloudflare can take 10-20s to decide even after ticking.")
    for attempt in range(3):
        print(f"  >>> waiting 20s (attempt {attempt + 1}/3)...")
        page.wait_for_timeout(20000)
        if not is_challenged(page):
            print("  >>> challenge cleared.")
            report["challenge_events"][-1]["outcome"] = "cleared"
            return True
    report["challenge_events"][-1]["outcome"] = "stuck"
    return False


def parse_ssr_rows(page):
    result = {"parsed": 0, "date_headers": [], "sample": [], "impact_repr": None}
    rows = page.locator("tr[id]")
    parsed = []
    for i in range(rows.count()):
        row = rows.nth(i)
        rid = row.get_attribute("id") or ""
        m = re.match(r"^(\d+)-(\d+)-([A-Za-z]+)-(\d+)$", rid)
        try:
            cells = row.locator("td")
            texts = [cells.nth(c).inner_text().strip() for c in range(cells.count())]
        except Exception:
            continue
        if not m:
            joined = " ".join(texts)
            if re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)", joined):
                result["date_headers"].append(joined)
            continue
        impact, event_id, country, idx = m.groups()
        if result["impact_repr"] is None:
            try:
                html = row.inner_html()
                result["impact_repr"] = {"bull_mentions": html.count("bull"),
                                         "svg_count": html.count("<svg"),
                                         "cell_html_head": html[:600]}
            except Exception:
                pass
        parsed.append({"event_id": event_id, "country": country,
                       "impact_raw": impact, "cells": texts})
    result["parsed"] = len(parsed)
    result["sample"] = parsed[:8]
    return result


def open_browser(p):
    """Persistent profile if possible, else plain launch + storage_state."""
    context = None
    mode = None
    try:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="msedge",
            headless=not HEADED,
            viewport={"width": 1400, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        mode = "persistent"
    except Exception as e:
        note(f"persistent profile launch failed: {type(e).__name__}: {e}")
        note("falling back to plain launch + storage_state")
    if context is None:
        kwargs = {"viewport": {"width": 1400, "height": 900}}
        if STATE_FILE.exists():
            kwargs["storage_state"] = str(STATE_FILE)
        browser = p.chromium.launch(channel="msedge", headless=not HEADED)
        context = browser.new_context(**kwargs)
        mode = "state_file"
    context.add_init_script(STEALTH_JS)
    return context, mode


def main():
    print("=== investing.com census — round 3.1 ===")
    with sync_playwright() as p:
        context, mode = open_browser(p)
        report["launch_mode"] = mode
        print(f"  launch mode: {mode}")

        page = context.pages[0] if (mode == "persistent" and context.pages) else context.new_page()

        traffic = []

        def on_response(resp):
            rt = resp.request.resource_type
            if rt in ("xhr", "fetch", "document"):
                entry = {"type": rt, "method": resp.request.method,
                         "status": resp.status, "url": resp.url}
                try:
                    pd = resp.request.post_data
                    if pd:
                        entry["post_data"] = pd[:300]
                except Exception:
                    pass
                traffic.append(entry)

        page.on("response", on_response)

        print(f"navigating: {CALENDAR_URL}")
        page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        if not wait_out_challenge(page, "initial load"):
            note("challenge stuck on initial load — aborting")
            report["network"] = traffic
            DIAG_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
            context.close()
            print(f"\nVERDICT: BLOCKED — challenge looping. diagnostic -> {DIAG_FILE}")
            return 2

        page.wait_for_timeout(8000)
        report["network"] = traffic
        print(f"  captured {len(traffic)} xhr/fetch/document calls")
        api_calls = [t for t in traffic if re.search(r"/api/|Service|graphql|_next", t["url"])]
        for t in api_calls[:15]:
            print(f"    [{t['type']}] {t['method']} {t['status']} {t['url'][:110]}")

        print("probing for embedded JSON...")
        el = page.locator("#__NEXT_DATA__")
        if el.count():
            try:
                blob = el.inner_text()
                report["embedded_json"]["__NEXT_DATA__"] = {"len": len(blob), "head": blob[:400]}
                (ROOT / "investing___NEXT_DATA__.json").write_text(blob, encoding="utf-8")
                note(f"__NEXT_DATA__ saved ({len(blob)} chars)")
            except Exception as e:
                note(f"__NEXT_DATA__ read failed: {e}")

        print("parsing SSR rows...")
        report["ssr_parse"] = parse_ssr_rows(page)
        sp = report["ssr_parse"]
        print(f"  event rows: {sp['parsed']}   date headers: {len(sp['date_headers'])}")
        for s in sp["sample"][:3]:
            print(f"    id={s['event_id']} {s['country']:<14} {s['cells'][:6]}")
        if sp["parsed"] == 0:
            (ROOT / "investing_dom_dump.html").write_text(page.content(), encoding="utf-8")
            note("0 rows parsed; DOM dumped")

        if sp["parsed"] > 0:
            print("testing date params (other-day SSR)...")
            for params in ["?dateFrom=2026-10-05&dateTo=2026-10-05", "?date=2026-10-05"]:
                page.wait_for_timeout(6000)
                try:
                    page.goto(CALENDAR_URL + params, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(5000)
                    if not wait_out_challenge(page, f"date param {params}"):
                        note(f"challenge during {params}; skipping further date tests")
                        break
                    parse = parse_ssr_rows(page)
                    report["date_param_test"][params] = {
                        "rows": parse["parsed"], "headers": parse["date_headers"][:3]}
                    print(f"  {params} -> rows={parse['parsed']} headers={parse['date_headers'][:1]}")
                    if any("October 5" in h for h in parse["date_headers"]):
                        note(f"WORKS: {params} serves other-day data")
                        break
                except PWTimeout:
                    report["date_param_test"][params] = {"error": "timeout"}

        if mode == "state_file":
            context.storage_state(path=str(STATE_FILE))
        context.close()

    DIAG_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\ndiagnostic report -> {DIAG_FILE}")

    sp = report["ssr_parse"]
    flags = [
        "SSR-OK" if sp["parsed"] > 0 else "SSR-FAIL",
        "DATEPARAM-OK" if any(v.get("rows", 0) > 0 for v in report["date_param_test"].values()) else "DATEPARAM-UNKNOWN",
        f"NEWAPI-{len([t for t in report['network'] if re.search(r'/api/|graphql|_next/data', t['url'])])}",
    ]
    print(f"VERDICT: {' | '.join(flags)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
