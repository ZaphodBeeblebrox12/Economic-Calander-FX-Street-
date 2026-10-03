#!/usr/bin/env python3
"""
Diagnostic: find WHERE fxstreet.com stores the economic-calendar filter
selection (localStorage / sessionStorage / cookies), and capture the
eventDates request URL format. Paste the output back.

Run with the probe stopped:   python inspect_filters.py
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

STORAGE_JS = """
(() => {
  const grab = store => {
    const out = [];
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i);
      let v = store.getItem(k);
      if (v && v.length > 600) v = v.slice(0, 600) + '...[truncated]';
      out.push([k, v]);
    }
    return out;
  };
  return JSON.stringify({local: grab(localStorage), session: grab(sessionStorage)});
})()
"""


def launch(pw):
    headless = C.HEADLESS if not C.HEADED else False
    base = dict(headless=headless, slow_mo=C.SLOW_MO_MS, args=C.LAUNCH_ARGS)
    cands = []
    ch = getattr(C, "BROWSER_CHANNEL", None)
    if ch:
        cands.append(("channel:" + ch, {"channel": ch}))
    cands.append(("bundled-chromium", {}))
    last = None
    for label, extra in cands:
        try:
            print(f"[launch] {label}", flush=True)
            return pw.chromium.launch(**{**base, **extra})
        except Exception as e:
            last = e
            print(f"[launch] failed {label}: {str(e)[:120]}", flush=True)
    raise last


def main():
    pw = sync_playwright().start()
    browser = None
    eventdate_urls = []
    try:
        browser = launch(pw)
        context = browser.new_context(
            user_agent=C.USER_AGENT, viewport=C.VIEWPORT,
            locale=C.LOCALE, timezone_id=C.TIMEZONE_ID)
        context.set_default_timeout(15_000)
        page = context.new_page()

        def cap(req):
            if "eventDates" in req.url:
                eventdate_urls.append(req.url)
        page.on("request", cap)

        page.goto(C.URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("tr.fxs_c_row", timeout=45_000)
        time.sleep(6)   # let all late writes settle

        storage = json.loads(page.evaluate(STORAGE_JS))
        cookies = context.cookies()

        print("\n===== eventDates request(s) =====")
        for u in eventdate_urls[:3]:
            print(u[:400])
        if not eventdate_urls:
            print("(none captured)")

        print("\n===== localStorage =====")
        for k, v in storage["local"]:
            print(f"--- {k}\n{v}\n")
        print("===== sessionStorage =====")
        for k, v in storage["session"]:
            print(f"--- {k}\n{v}\n")
        print("===== cookies =====")
        for c in cookies:
            print(f"{c['name']}  (domain={c['domain']}, httpOnly={c['httpOnly']})")
        print("\n[paste everything above back]")
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        pw.stop()


if __name__ == "__main__":
    main()
