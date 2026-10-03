# Economic Calendar Feed - Project State & Merge Spec
**Last updated:** 2026-10-03 (Session: Telegram format + source-neutrality + cross-source merge design)
**Purpose of this file:** carry full context into any future session. Read this first.

---

## 1. What exists today (all working, deployed on user's Windows machine)

Project root: `C:\Users\Inder\Desktop\fxstreet_playwright_probe`
GitHub: `ZaphodBeeblebrox12/Economic-Calander-FX-Street-` (public; HEAD clean, no secrets;
`fxs_state.json` must stay gitignored - it holds a Cloudflare `cf_clearance` cookie)

| File | Version | Contents |
|---|---|---|
| `probe.py` | v5 | Playwright observer probe: SignalR/DOM dual-path release detection, health/recovery ladder, dedup, cross-correlation benchmark, `_display_name()` source-neutral normalization, `_add_countries()` eventDates route interceptor, `/test` `/upcoming` `/help` Telegram commands (main-loop dispatched), HTML card rendering, edit-in-place revisions |
| `notifier.py` | v3.1 | Telegram: HTML parse mode, message_id tracking, `editMessageText` for revisions (falls back to send), getUpdates listener (message + channel_post, auto-deletes command msgs), dedup/backoff/queue |
| `config.py` | - | `EXTRA_COUNTRIES = "IN"` / `REMOVE_COUNTRIES = "UA"` (route-rewrite, never touch the site filter panel - clicking its checkboxes CRASHES the page renderer), `DISPLAY_NEUTRAL_NAMES = True`, `CUSTOM_NAME_RULES = []`, `FXS_STATE_FILE`, env vars `FXS_TELEGRAM_TOKEN` / `FXS_TELEGRAM_CHAT_ID` from `.env` |
| `set_filters.py` | v2 | Verifies country injection works (INR rows check), saves `fxs_state.json` |
| `inspect_filters.py` | - | Dumps localStorage/eventDates URL/cookies diagnostic |
| `js/observer.js` | - | In-page MutationObserver feeding the Python loop (unchanged) |

Telegram channel: "Chart Chronicle" (channel id `-10024...`, silent broadcast). Cards:
`🔴 <b>Nonfarm Payrolls (Sep)</b> <i>🇺🇸 USD</i>` / `<b>254K</b>` / `<i>Forecast 147K · Previous 142K</i>` / `✅ Beat by +107K`. No latency telemetry, no branding. Revisions EDIT the original message.

---

## 2. Design decisions already made (do not re-litigate)

1. **Two tabs vs two processes:** merge source = **separate process** (`investing_probe.py`), not tabs in one browser. Fault isolation + independent benchmarking (the whole point is measuring which source is earlier).
2. **Single emission layer:** cross-source dedup key posted by whichever source fires first; second source suppressed; material differences EDIT the card (revision flow already exists).
3. **Source-neutrality is subscriber-facing only** (Telegram messages), not repo/code. Site always sees scraping at network level; obfuscation targets attribution ambiguity for readers.
4. **Layer 3 of source-neutrality = the merge itself:** names normalized toward Investing conventions in the dedup layer so the feed vocabulary matches neither site exactly.
5. **Normalization maintenance model:** generic patterns (year-proof) + `CUSTOM_NAME_RULES` config escape hatch + 2-minute monthly eyeball of `releases.jsonl`. Proven against full 2026-27 catalog.

---

## 3. Naming normalization (SHIPPED, tested)

Generic rules in `probe.py::_display_name` (applied in `_card` before render):
- `Stocks Change` → `Inventories`; `Storage Change` → `Storage`; `Crude Oil Stock` → `Crude Oil Inventories`
- `^Baker Hughes ` → `` ; `^(.*?)'s (.+?) speech$` → `'s  Speaks`
- Country prefix for all-day rows (currency + no numeric values + not speech + name lacks a country word): `National Day` → `China - National Day`
- Flags: currency→emoji; `_CC_FIX = {"UK": "GB"}`; XAU 🥇 XAG 🥈 EUR 🇪🇺; 2-letter country codes work via regional-indicator math (CN→🇨🇳)
- Verified: 574/574 unique names in full Oct-26→Jan-27 catalog (4,618 rows). Residue = official report names (Tankan/IFO/ZEW/CFTC/NBS...) = not fingerprints. Zero misses.

**Test fixtures** (in this package, `fixtures/`):
- `catalog_fxstreet_q4.csv` - 1,970 rows, Oct-Dec 2026 (first download)
- `catalog_fxstreet_extended.csv` - 4,618 rows, Oct 2026-Jan 2027 (superset; +31 unique names, ALL holidays)
- Saturation proven: monthly re-downloads add nothing but holidays. Do not collect more.

---

## 4. Cross-source merge spec (CONFIRMED with real overlap data)

### Currency code map (Investing 2-letter → FXStreet 3-letter)
US→USD, UK→GBP, EU→EUR, DE→EUR (⚠ two codes → one), IN→INR, AU→AUD, JP→JPY,
CN→CNY, HK→HKD, NZ→NZD, CA→CAD, CH→CHF, KR→KRW, BR→BRL, TH→THB, RU→RUB,
PH→PHP, MY→MYR, VN→VND, ZA→ZAR, GR→EUR, SE→SEK, FI→EUR, IE→EUR, BE→EUR,
ES→EUR, FR→EUR, PT→EUR, IT→EUR, SG→SGD, ID→IDR, MX→MXN

### Confirmed alias pairs (FXStreet ↔ Investing, from real data)
| FXStreet | Investing |
|---|---|
| ISM Services PMI (Sep) | ISM Non-Manufacturing PMI (Sep) |
| Consumer Price Index (YoY) (Sep) | CPI (YoY) (Sep) |
| Consumer Price Index ex Food & Energy (MoM) | Core CPI (MoM) (Sep) |
| Retail Sales ex Autos (MoM) | Core Retail Sales (MoM) (Sep) |
| EIA Crude Oil Stocks Change | Crude Oil Inventories |
| FOMC Minutes | FOMC Meeting Minutes |
| RBI Interest Rate Decision | `Interest Rate Decision` (BARE - bank name omitted; currency must carry the match) |
| ECB's Nagel speech | (investing: `Speaks` verb forms) |

### Same on both (no alias needed)
Nonfarm Payrolls, Unemployment Rate, Initial Jobless Claims, ISM Manufacturing PMI,
S&P Global ... PMI, Average Hourly Earnings, GDP, JOLTS, FX Reserves USD,
Philadelphia Fed Manufacturing Index, Michigan Consumer Sentiment

### Investing-only quirks to handle in `source_map.py`
- Holiday rows: `Country - Holiday` + optional `- Early close at HH:MM` suffix → strip suffix for matching; specials: `Russia - Non Trading Day`, `Japan - Markets Closed`
- Holiday impact label = `Holiday` (FXStreet = `NONE`) → map both to ⚪ none
- Impact question SETTLED: holidays show ⚪ none, never LOW (a holiday is not a release)
- Holidays never produce cards today (probe posts only when Actual fills); flags work if market-closed notices are ever added
- Investing carries events FXStreet lacks: WPI Inflation (YoY) [IN], India CPI (YoY), U.S. President Trump Speaks — validates second-source rationale
- FXStreet carries `ADP Employment Change 4-week average` (odd derivative; verify vs headline), German state CPIs, RatingDog PMIs, RealClearMarkets/TIPP
- Event identity: use GUID when available (FXStreet CSV `Id` column); else (currency, normalized-name, start-bucket, country-for-EUR-PMIs)
- EUR ambiguity: `HCOB Services PMI` ×4 same morning all EUR (FR/IT/DE/ES flashes) - country REQUIRED in key; DOM parser already extracts country from row flag

---

## 5. Roadmap (next session picks up here)

1. **DONE** - FXStreet probe, Telegram cards, commands, country routing (IN added/UA removed), source-neutral naming
2. **NEXT: Investing.com census** - `diagnose_investing.py` mirroring the FXStreet diagnosis: DOM structure, transport (push vs poll), anti-bot reality check (expect worse than FXStreet - aggressive Cloudflare), login wall?
3. Build `investing_probe.py` (separate process, same skeleton) with Investing selectors + `source_map.py` (alias table + currency map + suffix strips, unit-tested against `fixtures/`)
4. Cross-source dedup: first-source-wins emission; material diff → edit card; log both to per-source benchmark files for the latency race analysis
5. Later: ForexLive breaking-news probe (keyword gate: central banks, "intervenes", "emergency", "breaking"; skip opinion), central-bank statement pages probe (Fed/ECB/RBI - fastest free rate-decision source)
6. Ops messages split to private admin chat (startup/health/quiet out of subscriber channel)

## 6. Commands reference
- `/test` - 3 sample cards + live edit demo + cleanup + confirmation
- `/upcoming` - next 3 real events from page (impact dot + flag + time)
- `/help` - command list
- Anti-flood 5s; only configured chat_id; commands main-loop dispatched (Playwright sync API not thread-safe)

## 7. Risks & watch items
- Investing.com anti-bot: highest blocker probability; mitigation = separate process + headed Edge + cf state file
- FXStreet filter-panel checkbox click CRASHES page renderer (observed) - route-rewrite bypasses it permanently; never reintroduce UI clicking
- Token hygiene: bot token rotates via @BotFather; never commit; `.env` only
