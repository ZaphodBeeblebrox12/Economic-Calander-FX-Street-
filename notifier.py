"""
Telegram notifier for the FXStreet calendar feed.

Design contract (do not break):
  - NEVER blocks or raises into the acquisition pipeline. Event handlers only
    enqueue; a single daemon thread performs all network I/O.
  - Bounded queue; on overflow messages are DROPPED and counted, never waited
    on. logs/*.jsonl remains the source of truth for every event.
  - Clean no-op (disabled) when token/chat_id are absent.
  - Fire-and-forget sends with timeout + exponential backoff: a Telegram
    outage must not stall, retry-storm, or crash the probe.

v3 changes (command listener):
  - A daemon thread long-polls getUpdates. Commands (e.g. /test, /upcoming)
    from the authorized chat are forwarded via the on_command callback;
    the probe queues them onto its OWN main loop, because Playwright's sync
    API is not thread-safe. Failures back off and retry - never crash.

v2 changes (subscriber-facing format):
  - HTML parse mode: messages are cards (<b> title + actual, <i> metadata).
  - sendMessage responses are parsed; message_ids are kept per event_key so a
    REVISION edits the original release message (editMessageText) instead of
    posting a duplicate. Edit failure falls back to sending a new message.
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
    MAX_MSG_IDS = 500          # event_key -> message_id cache (FIFO-trimmed)

    def __init__(self, cfg, logger=None):
        self.token = getattr(cfg, "TELEGRAM_TOKEN", "") or ""
        self.chat_id = str(getattr(cfg, "TELEGRAM_CHAT_ID", "") or "")
        self.enabled = bool(self.token and self.chat_id)
        self.state_min_interval_s = getattr(cfg, "TG_STATE_MIN_INTERVAL_S", 120)

        self._q = queue.Queue(maxsize=getattr(cfg, "TG_MAX_QUEUE", 64))
        self._stop = threading.Event()
        self._thread = None
        self._listener = None
        self._listener_stop = threading.Event()
        self._on_command = None
        self._notified = {}            # dedup key -> None (insertion-ordered)
        self._msg_ids = {}             # event_key -> telegram message_id
        self._last_sent_at = {}        # category -> epoch seconds
        self._backoff_until = 0.0
        self._consec_fail = 0

        self.sent = 0
        self.edits = 0
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

    def release(self, text, dedup_key=None, edit_key=None):
        self._enqueue(text, category="release", dedup_key=dedup_key,
                      force=True, edit_key=edit_key)

    def revision(self, text, dedup_key=None, edit_key=None):
        self._enqueue(text, category="revision", dedup_key=dedup_key,
                      force=True, edit_key=edit_key)

    def state(self, text, force=False):
        self._enqueue(text, category="state", force=force,
                      min_interval_s=self.state_min_interval_s)

    def quiet(self, text):
        self._enqueue(text, category="quiet")

    def alert(self, text, dedup_key=None):
        """Pre-release heads-up. dedup_key survives restarts (re-alert safe)."""
        self._enqueue(text, category="alert", dedup_key=dedup_key, force=True)

    def reply(self, text, chat_id):
        """Immediate send (bypasses queue). Used for command responses."""
        if not self.enabled:
            return None
        return self._post(text, chat_id=chat_id)

    def start_listener(self, on_command):
        """Long-poll Telegram for commands; call on_command(cmd, chat_id)."""
        if not self.enabled or self._listener is not None:
            return
        self._on_command = on_command
        self._listener = threading.Thread(target=self._listen_loop,
                                          name="tg-listener", daemon=True)
        self._listener.start()

    def stop_listener(self):
        self._listener_stop.set()
        if self._listener:
            self._listener.join(timeout=2.0)

    def stats_dict(self):
        return {"enabled": self.enabled, "sent": self.sent,
                "edits": self.edits, "failed": self.failed,
                "dropped": self.dropped}

    # ---------------- internals -------------------------------------------
    def _enqueue(self, text, category, dedup_key=None, force=False,
                 min_interval_s=0, edit_key=None):
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
        target_mid = self._msg_ids.get(edit_key) if edit_key else None
        try:
            self._q.put_nowait((text, category, edit_key, target_mid))
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
            text, category, edit_key, target_mid = item
            try:
                if time.time() < self._backoff_until:
                    self.dropped += 1
                    continue
                ok = False
                if target_mid is not None:
                    ok = self._edit(target_mid, text)
                    if ok:
                        self.edits += 1
                if not ok:
                    mid = self._post(text)
                    ok = mid is not None
                    if ok and edit_key:
                        self._msg_ids[edit_key] = mid
                        while len(self._msg_ids) > self.MAX_MSG_IDS:
                            self._msg_ids.pop(next(iter(self._msg_ids)))
            except Exception:
                ok = False
            if ok:
                self._consec_fail = 0
                self._last_sent_at[category] = time.time()
                if target_mid is None:
                    self.sent += 1
            else:
                self._consec_fail += 1
                self.failed += 1
                delay = min(30 * (2 ** min(self._consec_fail - 1, 4)), 300)
                self._backoff_until = time.time() + delay
                self._log(send_fail=1, consec_fail=self._consec_fail, backoff_s=delay)

    def _post(self, text, chat_id=None):
        """Send a message. Returns telegram message_id on success, else None."""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if data.get("ok"):
                return data["result"]["message_id"]
        except Exception:
            pass
        return None

    def _edit(self, message_id, text) -> bool:
        """Edit an existing message (revisions). True on success."""
        url = f"https://api.telegram.org/bot{self.token}/editMessageText"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            return bool(data.get("ok"))
        except Exception:
            return False

    def _listen_loop(self):
        offset = 0
        fails = 0
        while not self._listener_stop.is_set():
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                payload = json.dumps({"offset": offset, "timeout": 25,
                                      "allowed_updates": ["message", "channel_post"]}).encode("utf-8")
                req = urllib.request.Request(url, data=payload,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                fails = 0
                if not data.get("ok"):
                    raise RuntimeError("getUpdates returned not-ok")
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post") or {}
                    chat = str((msg.get("chat") or {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if chat and text.startswith("/") and self._on_command:
                        cmd = text.split()[0].split("@")[0].lower()
                        try:
                            self._on_command(cmd, chat, msg.get("message_id"))
                        except Exception:
                            pass
            except Exception:
                fails += 1
                self._log(listen_fail=1, consec_fail=fails)
                time.sleep(min(5 * fails, 60))

    def delete_message(self, chat_id, message_id):
        """Best-effort delete (keeps /test commands out of the channel)."""
        if not self.enabled or not message_id:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/deleteMessage"
            payload = json.dumps({"chat_id": chat_id,
                                  "message_id": message_id}).encode("utf-8")
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                pass
        except Exception:
            pass

    def _log(self, **fields):
        if self._logger:
            try:
                self._logger(**fields)
            except Exception:
                pass
