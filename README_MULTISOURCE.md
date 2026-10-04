# Multi-Source Economic Calendar Rig

Three independent sources race to deliver economic calendar events.
Subscriber-facing output is **source-agnostic** — whichever source fires first
wins, subscribers never know which one it was.

**Status: FXStreet ✅ + MQL5 ✅ live. Investing.com ⏸ deferred (hardest leg,
fully built, awaiting a calm IP window).**

---

## 1. Architecture

```
 probe.py (FXStreet)          investing_probe.py        mql5_probe.py
 push-websocket browser       Next.js payload browser   plain HTTP requests
        |                            | (deferred)              |
        v                            v                       v
 fxstreet_releases.jsonl   investing_releases.jsonl   mql5_releases.jsonl
        |                            |                       |
        +------------+---------------+-----------------------+
                     v
             merge_forwarder.py
   normalize -> dedupe (first-source-wins) -> Telegram send/edit
                     |
        latency_race.jsonl (benchmark data)

 supervisor.py keeps all processes alive (restart + backoff).
```

Design rules (from the original FXStreet README, still binding):
1. **Separate processes.** A dead source never takes down another. The
   supervisor restarts anything that exits (5s backoff, doubling to 60s).
2. **Probes are dumb collectors.** All intelligence lives in the merge layer.
3. **JSONL-first.** Every emission is timestamped and logged; the latency race
   is answered from data, never from opinion.

---

## 2. File inventory

| File | Role | Status |
|---|---|---|
| `probe.py` | FXStreet source (push websocket + DOM fallback) | ✅ existing, unchanged |
| `mql5_probe.py` | MQL5 source — polls `mql5.com/en/economic-calendar`, parses server-rendered event lines | ✅ new |
| `investing_probe.py` | investing.com source — `__NEXT_DATA__` payload diff via persistent Edge profile | ⏸ built, deferred |
| `merge_forwarder.py` | Normalization + first-source-wins dedup + Telegram send/edit | ✅ |
| `supervisor.py` | Launches/restarts fxstreet + investing + mql5 + forwarder | ✅ |
| `diagnose_investing.py` | Census tool — re-map investing.com if it redesigns | ✅ |
| `diagnose_mql5.py` | Census tool for MQL5 (round 2) | ✅ |
| `latency_race.jsonl` | Every double-sighting: winner, second source, both timestamps | runtime |
| `.env` | `FXS_TELEGRAM_TOKEN`, `FXS_TELEGRAM_CHAT_ID` (reused by forwarder) | existing |

---

## 3. The MQL5 probe (how source #3 works)

- **Transport:** `GET https://www.mql5.com/en/economic-calendar` — the calendar
  is fully server-rendered as plain-text lines inside `div.ec-table__item`
  elements. No XHR, no Cloudflare, no browser, no cookies.
- **Line format:** `2026.10.02 12:30, USD, Nonfarm Payrolls, Actual: 29 K,
  Forecast: 52 K, Previous: 133 K`
- **Timezone:** UTC (verified: NFP 12:30 == 8:30 ET).
- **Poll interval:** 15s. Emits `new_event` / `actual_update` to
  `mql5_releases.jsonl` keyed by `date|time|currency|name`.
- **Known gap:** impact/importance is not in the text layer (rendered as CSS).
  The merge layer enriches it from whichever other source carries it.
- **Known gap:** no country in the text layer. The merge layer resolves country
  via a wildcard rule (adopts the country of an existing same
  name+currency+hour match only when exactly one candidate exists — protects
  the EUR-PMI four-countries-same-morning case).
- **Failure mode:** if the page layout changes, the probe logs "no events
  parsed" and backs off — run `diagnose_mql5.py` to re-map.

---

## 4. The merge layer (`merge_forwarder.py`)

**Normalization** (source vocabularies -> one canonical schema):
- Currency 2-letter -> ISO 3-letter (`US`->`USD`, `EU`->`EUR`, ...)
- Alias pairs: `ISM Non-Manufacturing PMI` -> `ISM Services PMI`;
  `Markit/Final Services PMI` -> `S&P Global Services PMI` (extend the
  `ALIASES` table as race data reveals more)
- Impact -> unified scale: investing `1/2/3`, FXStreet volatility labels,
  MQL5 (absent, enriched) -> `low|medium|high|holiday`
- Holidays: empty-value events + name match -> `holiday` impact (⚪)

**Dedup key:** `name + currency + country + hour-bucket` — country is
mandatory because four EZ countries publish "PMI" on the same morning.

**First-source-wins:** the first line to arrive for a key owns the Telegram
message. The slower source:
- enriches blank fields (forecast / previous / period / impact)
- triggers an **edit** (never a re-send) when an actual or revision lands
Every double-sighting is logged to `latency_race.jsonl` — winner, second
source, both timestamps. That file is the answer to "which source is faster."

**Source invisibility:** message text is composed only from the canonical
schema. `source` appears in logs and `latency_race.jsonl` only.

Message shape:

    🟠 AUD · S&P Global Services PMI (Sep)
    Actual: 52.3 | Forecast: 51.4 | Previous: 51.4

---

## 5. Quickstart

```cmd
python supervisor.py
```

Manages: `probe.py`, `mql5_probe.py`, `investing_probe.py`, `merge_forwarder.py`.
If investing_probe is not wanted yet, comment its line in `supervisor.py`
(`PROCESSES` dict) — nothing else changes.

Individual runs for debugging:

```cmd
python probe.py                 # FXStreet
python mql5_probe.py            # MQL5 (expect ~400 new_event lines on first
                                # poll — the page carries a full week)
python merge_forwarder.py       # merge + Telegram
```

**Config:** credentials come from `.env` (`FXS_TELEGRAM_TOKEN`,
`FXS_TELEGRAM_CHAT_ID`). Optional overrides `TG_BOT_TOKEN` / `TG_CHAT_ID`.

**.gitignore:**

```
edge_profile_copy/
investing_profile/
investing_state.json
cf_clearance.txt*
*_releases.jsonl
investing_diagnostic.json
investing___NEXT_DATA__.json
mql5_diagnostic.json
latency_race.jsonl
```

---

## 6. Investing.com — deferred, not abandoned

Fully built (`investing_probe.py`, `diagnose_investing.py`), blocked only by
Cloudflare/IP reputation. Bring-up was painful; the playbook is in
`README_INVESTING.md` §8. Short version for restart:

1. The probe launches Edge with a **copied real profile**
   (`edge_profile_copy/`, made with robocopy — full command in that README).
   Fresh automation profiles get challenged forever; the copy passes.
2. **No periodic page reloads** — the probe background-fetches the page every
   20s from the open tab. Reloads are what kept poking Cloudflare.
3. Optional `cf_clearance.txt` (cookie value only) pre-clears the session.
4. If it challenges at startup: tick once in its window, it passes.
   If nobody is around: leave it — FXStreet + MQL5 cover the gap.

Do NOT restart it repeatedly into challenges — every failed cycle degrades the
IP's bot score. Quiet hours/days recover it.

---

## 7. Verification checklist

- [ ] `mql5_releases.jsonl` gains ~400 lines on first MQL5 poll (full week)
- [ ] Forwarder prints `[emit]` once per event (first source), `[edit]` on
      actual landing — never two sends for one event
- [ ] Telegram channel: one message per event, edited not duplicated
- [ ] `latency_race.jsonl` gains a line per event both sources see, with
      winner + both timestamps
- [ ] Kill any one probe -> supervisor restarts it within seconds
- [ ] No source name appears in any Telegram message

---

## 8. Roadmap

1. ✅ FXStreet + MQL5 live, supervisor, merge layer, race logging
2. ⬜ Confirm `adapt_fxstreet()` against a real `probe.py` output line
   (the only remaining guess in the system)
3. ⬜ Live race data -> tune `ALIASES` and impact mapping from reality
4. ⬜ Investing.com re-enable when the IP window is calm (§6)
5. ⬜ Optional later: ForexLive/central-bank pages (new *kind* of source:
   breaking news, RBI coverage) rather than a third redundant calendar
