#!/usr/bin/env python3
"""
Verify + persist the India filter via REQUEST REWRITING (no UI clicking).

The probe adds countries=IN to every calendar data fetch, so this script
mostly VERIFIES it works and optionally saves site state.

Run once (probe stopped):   python set_filters.py
Then always:                python probe.py
"""
import json
import os
import sys
import time

import config as C

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not installed")

STATE_FILE = getattr(C, "FXS_STATE_FILE", "fxs_state.json")
EXTRA = getattr(C, "EXTRA_COUNTRIES", "")

INR_COUNT_JS = """
(() => {
  const rows = Array.from(document.querySelectorAll('tr.fxs_c_row'));
  const inr = rows.filter(r =>
    ((r.querySelector('.fxs_c_currency')||{}).textContent || '').trim() === 'INR');
  return JSON.stringify({total: rows.length, inr: inr.length});
})()
"""


def launch(pw):
    headless = C.HEADLESS if not C.HEADED else False
    base = dict(headless=headless, slow_mo=C.SLOW_MO_MS, args=C.LAUNCH_ARGS)
    ch = getattr(C, "BROWSER_CHANNEL", None)
    cands = ([("channel:" + ch, {"channel": ch})] if ch else []) + [("bundled", {})]
    last = None
    for label, extra in cands:
        try:
            print(f"[launch] {label}", flush=True)
            return pw.chromium.launch(**{**base, **extra})
        except Exception as e:
            last = e
            print(f"[launch] failed {label}: {str(e)[:120]}", flush=True)
    raise last


def add_countries(route, request):
    url = request.url
    adds = "".join(f"&countries={c.strip()}"
                   for c in EXTRA.split(",")
                   if c.strip() and f"countries={c.strip()}" not in url)
    if adds and "countries=" in url:
        route.continue_(url=url + adds)
        return
    route.continue_()


def main():
    if not EXTRA.strip():
        sys.exit("EXTRA_COUNTRIES is empty in config.py - nothing to add.")
    pw = sync_playwright().start()
    browser = None
    rewritten = []
    try:
        browser = launch(pw)
        context = browser.new_context(
            user_agent=C.USER_AGENT, viewport=C.VIEWPORT,
            locale=C.LOCALE, timezone_id=C.TIMEZONE_ID)
        context.set_default_timeout(15_000)
        context.route("**/eventDates/**", add_countries)
        page = context.new_page()

        def cap(req):
            if "eventDates" in req.url:
                rewritten.append(req.url)
        page.on("request", cap)

        page.goto(C.URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("tr.fxs_c_row", timeout=45_000)
        time.sleep(6)

        print("\n===== eventDates request (after rewrite) =====")
        print((rewritten[0] if rewritten else "(none captured)")[:500])
        st = json.loads(page.evaluate(INR_COUNT_JS))
        print(f"\n[check] rows={st['total']}  INR rows={st['inr']}")
        if st["inr"] > 0:
            print("[ok] India is in the calendar - INR releases will be "
                  "detected and posted like any other.", flush=True)
        else:
            print("[warn] INR rows still 0. Paste the eventDates URL above "
                  "back - the categories= parameter may also need an IN "
                  "mapping.", flush=True)
        context.storage_state(path=STATE_FILE)
        print(f"[saved] {STATE_FILE}", flush=True)
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        pw.stop()


if __name__ == "__main__":
    main()
