"""
Telegram operational notifier for the FXStreet probe.

Design contract (do not break):
  - NEVER blocks or raises into the acquisition pipeline. Event handlers only
    enqueue; a single daemon thread performs all network I/O.
  - Bounded queue; on overflow messages are DROPPED and counted, never waited
    on. logs/*.jsonl remains the source of truth for every event.
  - Clean no-op (disabled) when token/chat_id are absent: the probe runs
    identically without a .env file.
  - Fire-and-forget sends with timeout + exponential backoff: a Telegram
    outage must not stall, retry-storm, or crash the probe. Sequential sends
    stay well under rate limits; state messages are throttled to collapse
    flapping health transitions.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request


def fmt_uptime(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


class TelegramNotifier:
    def __init__(self, cfg, logger=None):
        self.token = getattr(cfg, "TELEGRAM_TOKEN", "") or ""
        self.chat_id = str(getattr(cfg, "TELEGRAM_CHAT_ID", "") or "")
        self.enabled = bool(self.token and self.chat_id)
        self.state_min_interval_s = getattr(cfg, "TG_STATE_MIN_INTERVAL_S", 120)

        self._q = queue.Queue(maxsize=getattr(cfg, "TG_MAX_QUEUE", 64))
        self._stop = threading.Event()
        self._thread = None
        self._notified = {}            # dedup key -> None (insertion-ordered)
        self._last_sent_at = {}        # category -> epoch seconds
        self._backoff_until = 0.0
        self._consec_fail = 0

        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._logger = logger          # callable(**fields) or None

    # ---------------- public API (safe to call from the event loop) -------
    def start(self):
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._worker, name="tg-sender",
                                        daemon=True)
        self._thread.start()

    def stop(self, final_message=None, join_s=2.0):
        if final_message:
            self._enqueue(final_message, category="state", force=True)
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=join_s)

    def startup(self, text):
        self._enqueue(text, category="startup", force=True)

    def release(self, text, dedup_key=None):
        self._enqueue(text, category="release", dedup_key=dedup_key, force=True)

    def revision(self, text, dedup_key=None):
        self._enqueue(text, category="revision", dedup_key=dedup_key, force=True)

    def state(self, text, force=False):
        self._enqueue(text, category="state", force=force,
                      min_interval_s=self.state_min_interval_s)

    def quiet(self, text):
        self._enqueue(text, category="quiet")

    def stats_dict(self):
        return {"enabled": self.enabled, "sent": self.sent,
                "failed": self.failed, "dropped": self.dropped}

    # ---------------- internals -------------------------------------------
    def _enqueue(self, text, category, dedup_key=None, force=False, min_interval_s=0):
        if not self.enabled:
            return
        if dedup_key is not None:
            if dedup_key in self._notified:
                return
            self._notified[dedup_key] = None
            while len(self._notified) > 2000:
                self._notified.pop(next(iter(self._notified)))
        if not force and min_interval_s:
            last = self._last_sent_at.get(category, 0.0)
            if time.time() - last < min_interval_s:
                self.dropped += 1
                return
        try:
            self._q.put_nowait((text, category))
        except queue.Full:
            self.dropped += 1
            self._log(drop_overflow=1, category=category)

    def _worker(self):
        while True:
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if item is None:
                return
            text, category = item
            try:
                if time.time() < self._backoff_until:
                    self.dropped += 1
                    continue
                ok = self._post(text)
            except Exception:
                ok = False
            if ok:
                self._consec_fail = 0
                self._last_sent_at[category] = time.time()
                self.sent += 1
            else:
                self._consec_fail += 1
                self.failed += 1
                delay = min(30 * (2 ** min(self._consec_fail - 1, 4)), 300)
                self._backoff_until = time.time() + delay
                self._log(send_fail=1, consec_fail=self._consec_fail, backoff_s=delay)

    def _post(self, text) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    def _log(self, **fields):
        if self._logger:
            try:
                self._logger(**fields)
            except Exception:
                pass
