# Investing.com Source — Setup & Maintenance Guide

> Branch: `feature/investing-source`
> Companion to the main FXStreet README. Covers the investing.com probe,
> the copied-profile trust transplant, the merge/forwarder layer, and the supervisor.

---

## 1. Architecture recap (why it works this way)

| Component | File | Role |
|---|---|---|
| FXStreet probe | `probe.py` | Source 1 — existing, unchanged |
| Investing probe | `investing_probe.py` | Source 2 — separate process, own browser |
| Census tool | `diagnose_investing.py` | Re-map the site if investing.com redesigns |
| Merge layer | `merge_forwarder.py` | Normalization + first-source-wins + Telegram |
| Supervisor | `supervisor.py` | Starts/restarts all three processes |

**Transport:** investing.com is a Next.js app. The full calendar payload ships
inside every page HTML as `__NEXT_DATA__.props.pageProps.state.economicCalendarStore
.calendarEventsByDate`. The probe reads that blob from the DOM (or a background
`fetch('/economic-calendar/')`) and diffs occurrences — no scraping of table
cells, no POST to the Cloudflare-challenged legacy endpoint
(`getCalendarFilteredData`, abandoned in census round 1).

**Dedup key inside the probe:** `occurrenceId` (e.g. `557956`).
**Dedup key at the merge layer:** normalized `name + currency + country + hour`
(country is mandatory — four EZ countries publish "PMI" the same morning).

---

## 2. First-time setup (copied real Edge profile)

The probe launches Edge with a **copy of your real browser profile**. This is
what gets past Cloudflare: months of cookies/history score as a trusted human,
where a fresh automation profile gets challenged forever. Your daily Edge is
never touched — the copy is a separate directory.

### Step 1 — close Edge completely

```cmd
taskkill /F /IM msedge.exe 2>nul
```

### Step 2 — copy the profile

```cmd
robocopy "%LOCALAPPDATA%\Microsoft\Edge\User Data" "C:\Users\Inder\Desktop\fxstreet_playwright_probe\edge_profile_copy" /E /COPY:DAT /XD "User Data\Default\Cache" "User Data\Default\Code Cache" "User Data\Default\GPUCache" "User Data\Default\Service Worker\CacheStorage"
```

- `/E` — include all subdirectories (empty ones too)
- `/COPY:DAT` — copy data, attributes, timestamps (no auditing info; running
  un-elevated this avoids access-denied on system files)
- `/XD ...` — skip only the bulky web caches; **cookies, history and all trust
  data are included**. Do not add more exclusions.
- Expect ~500 MB / ~9,000 files / 4–6 minutes. Robocopy exit codes 0–7 are
  success (it has its own error-code convention — anything ≤ 7 with
  `FAILED = 0` in the summary table is fine).

If Edge is installed system-wide and the source path differs, check
`%LOCALAPPDATA%\Microsoft\Edge\User Data` first — that is the per-user profile
location regardless of where the exe lives.

### Step 3 — run

```cmd
python supervisor.py
```

Expected first lines from `investing_probe.py`:

```
launch mode: persistent
imported cf_clearance from cf_clearance.txt (426 chars)   ← optional, see §3
```

Then silence = success (6-second payload polls, events emitted when they fire).

---

## 3. The `cf_clearance.txt` escape hatch

The probe imports a clearance cookie before loading if `cf_clearance.txt`
(or the Notepad double-extension `cf_clearance.txt.txt`) exists in the project
root. Only needed when:

- the copy's own clearance has expired, **and**
- the startup challenge appears, **and** a human click is available

To harvest a fresh one: open `investing.com/economic-calendar/` in your normal
Edge, F12 → Application → Cookies → copy the `cf_clearance` value → save as
`cf_clearance.txt` (value only, no name/quotes).

Treat it as a session credential: gitignore it, never paste it in chats. After
any leak, rotate by simply revisiting the site in real Edge (the old value dies
on its own). Once a run passes, the probe's profile holds fresh clearance and
the file becomes a spare again — you can delete it.

---

## 4. Configuration

Credentials live in the existing `.env` (reused, not duplicated):

```
FXS_TELEGRAM_TOKEN=...
FXS_TELEGRAM_CHAT_ID=...
```

The forwarder also honors `TG_BOT_TOKEN` / `TG_CHAT_ID` if you ever want to
route investing-sourced messages to a different channel. Country filter for the
investing probe: `COUNTRIES` list at the top of `investing_probe.py`
(US=5, IN=14, UA=61, EU=72, ... — full table was decoded from `__NEXT_DATA__`
during the census; see `investing_diagnostic.json` history).

---

## 5. `.gitignore` additions

```
edge_profile_copy/
investing_profile/
investing_state.json
cf_clearance.txt*
investing_releases.jsonl
investing_diagnostic.json
investing___NEXT_DATA__.json
investing_dom_dump.html
latency_race.jsonl
```

---

## 6. Maintenance

| Event | What to do |
|---|---|
| Probe challenged at startup | Tick once in its window (clicks pass in the copied profile). If unattended, harvest `cf_clearance.txt` per §3 |
| Challenge appears constantly | The profile/IP reputation has degraded. Do a fresh robocopy (§2) after a few days of normal browsing in real Edge |
| New PC / Windows reinstall | Full §2 again |
| investing.com redesign | Run `python diagnose_investing.py` — it re-maps the payload (`__NEXT_DATA__` path, row ids, country table) without touching the probe |
| Telegram token rotation | Update `.env`, supervisor restarts forwarder on next crash or manually kill that one process |
| IP change (router/VPN) | Expect one challenge; pass it once and the new IP learns trust |

**Do not** run the probe and your daily Edge on the *same* profile directory
simultaneously (lock conflict). The copy exists precisely so both run at once.

---

## 7. Verifying it works

1. **Quiet proof:** `investing_releases.jsonl` gains a `new_event` line per
   calendar event on first poll.
2. **Live proof:** an `actual_update` line when a real actual lands (AUD PMI
   03:30 IST is the classic first test).
3. **Race proof:** `latency_race.jsonl` shows `winner` vs `second_source` with
   both timestamps whenever both sources see the same event.
4. **Subscriber proof:** one Telegram message per event, edited (not re-sent)
   when the slower source adds material info. No source names in text.

---

## 8. Failure modes seen during bring-up (for future-you)

- **`TargetClosedError` at launch** → zombie `msedge.exe` holding the profile;
  `taskkill /F /IM msedge.exe`, delete the half-made copy, re-robocopy.
- **Challenge blank, no checkbox, click does nothing** → browser fingerprint
  burned. Do not keep clicking; refresh the profile (§6).
- **`& was unexpected at this time`** → PowerShell syntax typed in cmd. Use cmd
  syntax (no `&`) or switch the terminal to PowerShell.
- **Cookie imported but still challenged** → clearance was already invalidated;
  the fingerprint was flagged, not the cookie. Fix the profile, not the cookie.
- **`cf_clearance.txt.txt`** → Notepad appended `.txt`; the probe accepts both,
  but rename it anyway to avoid confusion.
- **CDP path (`--remote-debugging-port=9222`) does not work** → Edge/Chrome
  111+ silently ignores the debug port on the default profile (anti-malware
  measure). The copied-profile path in §2 replaces it entirely.
