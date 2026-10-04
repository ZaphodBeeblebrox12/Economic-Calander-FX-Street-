"""
merge_forwarder.py — source-agnostic merge + Telegram forwarder
(branch: feature/investing-source)

Consumes BOTH probe JSONL streams, applies the normalization layer
(currency map, alias pairs, country disambiguation, unified impact scale),
runs first-source-wins dedup, and posts subscriber-facing messages to Telegram.

SUBSCRIBER TEXT IS BUILT ONLY FROM THE CANONICAL SCHEMA. The source tag is
used for latency stats/logging only — never in message text.

Config (environment variables or edit below):
  TG_BOT_TOKEN   — from @BotFather
  TG_CHAT_ID     — target channel/chat id
  FXS_JSONL      — path to the FXStreet probe's JSONL (default: fxstreet_releases.jsonl)

Run:  python merge_forwarder.py
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------- config ---
def _load_dotenv(path):
    """Minimal .env reader (KEY=VALUE lines); real env vars take precedence."""
    vals = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return vals


_DOTENV = _load_dotenv(ROOT / ".env")

# accepts TG_BOT_TOKEN/TG_CHAT_ID, falls back to your existing FXS_* names
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or _DOTENV.get("TG_BOT_TOKEN") \
    or os.environ.get("FXS_TELEGRAM_TOKEN") or _DOTENV.get("FXS_TELEGRAM_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or _DOTENV.get("TG_CHAT_ID") \
    or os.environ.get("FXS_TELEGRAM_CHAT_ID") or _DOTENV.get("FXS_TELEGRAM_CHAT_ID") or ""


# ------------------------------------------- calendar filter config --------
def _load_calendar_config():
    p = ROOT / "calendar_config.json"
    if not p.exists():
        return {"track_currencies": [], "backfill_hours": 2}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[config] calendar_config.json unreadable ({e}); tracking ALL")
        return {"track_currencies": [], "backfill_hours": 2}


CALENDAR_CONFIG = _load_calendar_config()
# empty list = track everything. Currency codes (ISO 4217) — one filter for
# ALL sources, applied at the merge layer so probes stay dumb collectors.
TRACK = {c.upper() for c in CALENDAR_CONFIG.get("track_currencies", [])}
# events older than this (hours) when first seen are recorded but NOT sent —
# stops the MQL5 first-poll week dump from blasting the channel
BACKFILL_HOURS = float(CALENDAR_CONFIG.get("backfill_hours", 2))


def event_age_hours(ev):
    t = (ev.get("time_utc") or "").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return None
FXS_JSONL = Path(os.environ.get("FXS_JSONL", ROOT / "fxstreet_releases.jsonl"))
INV_JSONL = ROOT / "investing_releases.jsonl"
LATENCY_LOG = ROOT / "latency_race.jsonl"
POLL = 1.0

# ----------------------------------------------------- normalization layer --
# Currency: 2-letter site codes -> ISO 3-letter
CURRENCY_2TO3 = {"AU": "AUD", "US": "USD", "EU": "EUR", "GB": "GBP", "JP": "JPY",
                 "IN": "INR", "CA": "CAD", "CH": "CHF", "NZ": "NZD", "CN": "CNY",
                 "BR": "BRL", "HK": "HKD", "SG": "SGD", "ZA": "ZAR", "KR": "KRW",
                 "ES": "EUR", "FR": "EUR", "DE": "EUR", "IT": "EUR", "RU": "RUB",
                 "UA": "UAH"}

# Alias pairs: canonical <-> variants seen across sources (extend as race data
# reveals more). Key = canonical name, values = source-specific spellings.
ALIASES = {
    "ISM Services PMI": ["ISM Non-Manufacturing PMI", "ISM Non-Manufacturing Index"],
    "S&P Global Services PMI": ["Markit Services PMI", "Final Services PMI"],
    "S&P Global Manufacturing PMI": ["Markit Manufacturing PMI", "Final Manufacturing PMI"],
}
ALIAS_LOOKUP = {v.lower(): k for k, vs in ALIASES.items() for v in vs}

IMPACT_MAP = {  # source vocab -> canonical
    "investing": {"1": "low", "2": "medium", "3": "high"},
    # FXStreet adapter fills its own mapping below once its format is confirmed.
}
IMPACT_EMOJI = {"high": "\U0001f534", "medium": "\U0001f7e0", "low": "\U0001f7e1",
                "holiday": "\u26aa", "none": "\u2b1c"}

HOLIDAY_WORDS = ("holiday", "bank holiday", "day off", "early close")


def normalize_currency(code):
    c = (code or "").strip().upper()
    return CURRENCY_2TO3.get(c, c if len(c) == 3 else "")


def canonical_name(name):
    n = (name or "").strip()
    return ALIAS_LOOKUP.get(n.lower(), n)


def canonical_impact(source, raw, name):
    n = (name or "").lower()
    if any(w in n for w in HOLIDAY_WORDS):
        return "holiday"
    if source == "investing":
        return IMPACT_MAP["investing"].get(str(raw), "none")
    # FXStreet: volatility labels — adjust to its real vocabulary
    m = {"high": "high", "moderate": "medium", "low": "low",
         "medium": "medium", "3": "high", "2": "medium", "1": "low"}
    return m.get(str(raw).strip().lower(), "none")


# ------------------------------------------------------------ adapters -----
def adapt_investing(rec):
    """investing_releases.jsonl -> canonical event."""
    return {
        "dedupe_key": None,  # built below
        "source": "investing",
        "currency": normalize_currency(rec.get("currency")),
        "country": rec.get("country", ""),           # REQUIRED for EUR-PMI disambiguation
        "name": canonical_name(rec.get("event")),
        "impact": canonical_impact("investing", rec.get("importance"), rec.get("event")),
        "time_utc": rec.get("time_utc") or rec.get("actual_time_utc", ""),
        "period": (rec.get("period") or "").strip("()"),
        "forecast": rec.get("forecast", ""),
        "previous": rec.get("previous", ""),
        "actual": rec.get("actual", ""),
        "revised_from": rec.get("revised_from", ""),
        "kind": rec.get("kind", ""),
    }


def adapt_fxstreet(rec):
    """FXStreet JSONL -> canonical event.
    TODO: confirm against a real probe.py output line — field names below are
    the expected shape; adjust once a sample from probe.py is available."""
    name = rec.get("name") or rec.get("event") or rec.get("title", "")
    return {
        "dedupe_key": None,
        "source": rec.get("source", "fxstreet"),
        "currency": normalize_currency(rec.get("currency")),
        "country": rec.get("country", ""),
        "name": canonical_name(name),
        "impact": canonical_impact("fxstreet", rec.get("volatility") or rec.get("impact"), name),
        "time_utc": rec.get("time_utc") or rec.get("timestamp", ""),
        "period": (rec.get("period") or "").strip("()"),
        "forecast": rec.get("forecast", ""),
        "previous": rec.get("previous", ""),
        "actual": rec.get("actual", ""),
        "revised_from": rec.get("revisedFrom", ""),
        "kind": rec.get("kind", "new_event"),
    }


def adapt_mql5(rec):
    """mql5_releases.jsonl -> canonical event. Country blank at this layer:
    the merge engine wildcards it against existing same name+ccy+hour entries."""
    return {
        "dedupe_key": None,
        "source": "mql5",
        "currency": normalize_currency(rec.get("currency")),
        "country": rec.get("country", ""),
        "name": canonical_name(rec.get("event")),
        "impact": canonical_impact("mql5", rec.get("importance"), rec.get("event")),
        "time_utc": rec.get("time_utc", ""),
        "period": (rec.get("period") or "").strip("()"),
        "forecast": rec.get("forecast", ""),
        "previous": rec.get("previous", ""),
        "actual": rec.get("actual", ""),
        "revised_from": rec.get("revised_from", ""),
        "kind": rec.get("kind", ""),
    }


ADAPTERS = {"investing": adapt_investing, "fxstreet": adapt_fxstreet,
            "mql5": adapt_mql5}


def dedupe_key(ev):
    """canonical identity: name + currency + country + hour bucket (EUR-PMI
    ambiguity: same name+currency across DE/FR/ES/IT same morning -> country
    is mandatory)."""
    hour = (ev["time_utc"] or "")[:13]
    return "|".join([ev["name"].lower(), ev["currency"], ev["country"].lower(), hour])


# ------------------------------------------------------------- telegram ----
def tg_api(method, payload):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[tg] {method} failed: {e}", flush=True)
        return None


def format_message(ev, first_source_won=None):
    """Subscriber-facing text — canonical schema only, no source names."""
    flag = IMPACT_EMOJI.get(ev["impact"], "\u2b1c")
    head = f"{flag} {ev['currency']} \u00b7 {ev['name']}"
    if ev["period"]:
        head += f" ({ev['period']})"
    rows = []
    if ev["actual"]:
        rev = f"  (rev. from {ev['revised_from']})" if ev.get("revised_from") else ""
        rows.append(f"Actual: {ev['actual']}{rev}")
    if ev["forecast"]:
        rows.append(f"Forecast: {ev['forecast']}")
    if ev["previous"]:
        rows.append(f"Previous: {ev['previous']}")
    return head + ("\n" + " | ".join(rows) if rows else "")


# ---------------------------------------------------------------- engine ---
class MergeEngine:
    def __init__(self):
        self.events = {}     # dedupe_key -> {"canonical": ev, "msg_id": int|None,
                             #                "sources_seen": {src: first_seen_iso}}
        self.skipped = 0     # filtered out by track_currencies
        self.backfilled = 0  # recorded but not sent (too old at first sight)

    def handle(self, raw, source):
        ev = ADAPTERS[source](raw)
        if not ev["name"] or not ev["currency"]:
            return
        if TRACK and ev["currency"].upper() not in TRACK:
            self.skipped += 1
            if self.skipped % 100 == 1:
                print(f"[filter] skipped {self.skipped} events outside "
                      f"track_currencies", flush=True)
            return
        # wildcard country: sources without country info (mql5) adopt the
        # country of an existing same name+currency+hour entry when exactly
        # one candidate exists (EUR-PMI mornings share name+ccy but differ by
        # time, so single-candidate is the safe rule)
        if not ev.get("country"):
            cand = [e for k, e in self.events.items()
                    if k.split("|")[0] == ev["name"].lower()
                    and k.split("|")[1] == ev["currency"]
                    and k.split("|")[3] == (ev["time_utc"] or "")[:13]]
            if len(cand) == 1:
                ev = dict(ev, country=cand[0]["canonical"].get("country", ""))
        ev["dedupe_key"] = key = dedupe_key(ev)
        now = datetime.now(timezone.utc).isoformat()
        entry = self.events.get(key)

        if entry is None:
            # FIRST SOURCE WINS: whoever arrives first owns the event
            entry = {"canonical": ev, "msg_id": None, "sources_seen": {source: now}}
            self.events[key] = entry
            age = event_age_hours(ev)
            if age is not None and age > BACKFILL_HOURS:
                # startup backfill: record (so later enrichment/edits still
                # work) but do not send — keeps the channel free of the
                # first-poll backlog
                self.backfilled += 1
                if self.backfilled % 100 == 1:
                    print(f"[backfill] recorded {self.backfilled} old events "
                          f"(not sent)", flush=True)
            else:
                entry["msg_id"] = self._send(format_message(ev))
        else:
            entry["sources_seen"][source] = now
            self._race_log(key, entry, source, ev)
            cur = entry["canonical"]
            changed = False
            # enrich from the slower source: fill blanks only (owner keeps ownership)
            for f in ("forecast", "previous", "period", "impact"):
                if (not cur.get(f) or cur.get(f) == "none") and ev.get(f) and ev.get(f) != "none":
                    cur[f] = ev[f]; changed = True
            # actual landed (on either source) -> edit the message
            if ev.get("actual") and ev["actual"] != cur.get("actual"):
                cur["actual"] = ev["actual"]
                cur["revised_from"] = ev.get("revised_from", cur.get("revised_from", ""))
                changed = True
            if changed and entry["msg_id"]:
                self._edit(entry["msg_id"], format_message(cur))

    def _send(self, text):
        r = tg_api("sendMessage", {"chat_id": TG_CHAT_ID, "text": text,
                                   "disable_web_page_preview": True})
        if r and r.get("ok"):
            print(f"[emit] {text.splitlines()[0]}", flush=True)
            return r["result"]["message_id"]
        print(f"[emit FAILED] {text}", flush=True)
        return None

    def _edit(self, msg_id, text):
        r = tg_api("editMessageText", {"chat_id": TG_CHAT_ID, "message_id": msg_id,
                                       "text": text, "disable_web_page_preview": True})
        if r and r.get("ok"):
            print(f"[edit] msg {msg_id}: {text.splitlines()[0]}", flush=True)

    def _race_log(self, key, entry, source, ev):
        with LATENCY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "key": key, "winner": next(iter(entry["sources_seen"])),
                "second_source": source,
                "winner_seen": entry["sources_seen"][next(iter(entry["sources_seen"]))],
                "second_seen": entry["sources_seen"][source],
                "second_actual": ev.get("actual", ""),
            }, ensure_ascii=False) + "\n")


def tail(path, offset_state):
    """Yield new lines of a JSONL file as they appear."""
    if not path.exists():
        return offset_state, []
    size = path.stat().st_size
    if offset_state is None or size < offset_state:   # rotated/recreated
        offset_state = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(offset_state)
        lines = [l for l in f.read().splitlines() if l.strip()]
        offset_state = f.tell()
    return offset_state, lines


def main():
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Missing Telegram creds: set TG_BOT_TOKEN/TG_CHAT_ID or reuse "
              "FXS_TELEGRAM_TOKEN/FXS_TELEGRAM_CHAT_ID in .env.")
        return 1
    print("=== merge_forwarder starting ===", flush=True)
    engine = MergeEngine()
    MQL_JSONL = ROOT / "mql5_releases.jsonl"
    offsets = {"investing": None, "fxstreet": None, "mql5": None}
    files = {"investing": INV_JSONL, "fxstreet": FXS_JSONL, "mql5": MQL_JSONL}

    while True:
        try:
            for src in ("investing", "fxstreet", "mql5"):
                offsets[src], lines = tail(files[src], offsets[src])
                for line in lines:
                    try:
                        engine.handle(json.loads(line), src)
                    except Exception as e:
                        print(f"[warn] bad {src} line: {e}", flush=True)
            time.sleep(POLL)
        except KeyboardInterrupt:
            print("stopped", flush=True)
            break
        except Exception:
            time.sleep(POLL * 3)


if __name__ == "__main__":
    import sys
    sys.exit(main())
