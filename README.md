<<<<<<< HEAD
# FXStreet Playwright Probe — Phase 0

Proof-of-concept for the browser-observed acquisition layer. It answers two questions
with measurements:

**Q1:** Can we detect FXStreet `Actual` releases fast and reliably by *observing* a real
Chromium (network + DOM), instead of impersonating FXStreet's private transport?

**Q2:** Which is faster on the same session — network-side detection or DOM
(MutationObserver) detection — and where does the latency actually live?

It deliberately does NOT post to Telegram/X, does not use Room/outbox, and has no
server. Releases are written to `logs/releases.jsonl` for inspection.

---

## 1. Why this architecture (vs the old `fxstreet.py`)

```
OLD (broken):                    THIS PROBE:
requests                         Chromium (real browser)
→ scrape HTML                    → real FXStreet webpage
→ regex WS URL                   → FXStreet's own JS runs
→ websocket-client               → FXStreet establishes ITS current transport
→ pretend to be FXStreet         → we OBSERVE network frames + DOM mutations
```
The old scraper broke because FXStreet retired the transport it reverse-engineered —
every transport/token/endpoint rotation requires human re-reverse-engineering. Here,
whatever the website does today (WebSocket, SSE, or polling — **we do not assume**),
the page does it for us and we watch. The discovery report tells you what it is.

## 2. Setup

```bash
python3.11+ -m venv .venv && source .venv/bin/activate   # Python 3.10+ (3.11/3.12 recommended)
pip install -r requirements.txt
playwright install chromium
python probe.py                 # headed (default, recommended for Phase 0)
```

On a headless Linux server later: set `HEADED=False, HEADLESS=True` in `config.py`
(and prefer `xvfb-run python probe.py` if you want a virtual display). Headed vs
headless uses the same engine; some anti-bot stacks score headless fingerprints
differently — measure both before choosing for production.

Debug: `PWDEBUG=1 python probe.py` (Playwright inspector), or attach Chrome DevTools
to the headed window directly.

## 3. Layout

```
fxstreet_playwright_probe/
├── config.py           # everything tunable (edit here, not in probe.py)
├── probe.py            # browser control, network observation, health, recovery
├── js/observer.js      # injected MutationObserver + census + heartbeat
├── logs/probe.jsonl    # ALL events (rotating 25MB x5)
├── logs/releases.jsonl # economic releases only
├── output/dom_census.json      # DOM structure report -> send back for analysis
└── output/benchmark.jsonl      # per-release network-vs-DOM latency records
```

## 4. What it captures

**Network (Playwright events):** every WebSocket open/close/frame (sent+received),
SSE/stream opens, XHR/fetch request/response. Payloads are keyword-classified as
calendar-relevant (`actual`, `consensus`, `eventdate`, `currencycode`, ...) — the
keyword set is shape-based, NOT endpoint-based: there are no `signalr`/`calendarhub`
assumptions anywhere. Tokens/cookies/auth in URLs and payloads are redacted before
logging (see `config.REDACT_PATTERNS`).

**DOM (injected observer):** row discovery (selector list + heuristic fallback),
per-row MutationObservers (childList/subtree/characterData + class/style attributes),
field extraction (Actual/Forecast/Previous/Revised + name/currency/time/impact),
**confidence scoring per extraction**, diff-emit (RELEASE / REVISION / CORRECTION),
plus a 5 s heartbeat with `visibilityState`, `interval_drift_pct`, `raf_per_sec`
(throttling detector), rows attached, ms-since-last-mutation, `navigator.onLine`.

**Timestamps:** monotonic (`time.perf_counter`) for all local latency math; wall only
for cross-machine comparison. A `clock_anchor` handshake maps the page's
`performance.now()` into our monotonic clock (uncertainty ~1-2 ms — bridge latency).

## 5. The benchmark (network vs DOM, same session)

For every release, `output/benchmark.jsonl` gets one record:

| field | meaning |
|---|---|
| `t_network_mono_ms` | when a relevant network frame arrived (correlated by id, else currency+name, else currency) |
| `t_dom_mutation_mono_ms` | when the MutationObserver handled the row change |
| `t_python_mono_ms` | when Python processed the event |
| `network_to_dom_ms` | render/commit cost — **our controlled segment** |
| `dom_to_python_ms` | bridge cost — **our controlled segment** |
| `network_to_python_ms` | detector latency — the headline number |
| `correlation` | `id` / `currency+name` / `currency` / `none` (strength of the match) |

**Latency attribution — do not conflate segments.** What you control is
`network_to_python` (expect ~50–300 ms typical). What you do NOT control is
FXStreet's own publish→transport fan-out (typically 0.3–3 s, visible only as the gap
between the event's scheduled time and the first frame). A "sub-second" headline is
meaningless without this split; the benchmark gives you the split per release.

Run through >= 20-30 releases (include CPI/NFP/FOMC-class). Report median / p90 /
p95 / p99 / max of `network_to_python_ms`, plus missed (ground truth vs releases log)
and duplicates (`dup_suppressed` counter should absorb them).

## 6. Health state machine & recovery

States: `STARTING → BOOTSTRAPPING → PAGE_READY → LIVE ⇄ DEGRADED → RECOVERING → LIVE`,
terminal `FAILED` (operator alert). Exact conditions are in code; summary:

- **PAGE_READY:** heartbeat reports `rows_attached > 0` (calendar found in DOM).
- **LIVE:** realtime activity confirmed (relevant network or recent DOM mutation).
- **DEGRADED:** JS heartbeat lost (>20 s), or both DOM and network stale (>240 s),
  or 3 consecutive low-confidence parses.
- **RECOVERING:** ladder — level 0 nudge → 1 `page.reload()` → 2 new page →
  3 new context (fresh storage/cookies) → 4 relaunch browser. 3 attempts per level,
  verify 45 s after each action, 120 s cooldown between full cycles. A plain
  WebSocket close does NOT trigger this (hubs drop idle sockets); only the
  stale/heartbeat/parser signals do.
- **FAILED:** challenge/block page detected (title/body markers: Cloudflare,
  captcha, 403…) or ladder exhausted. On challenge: **release emission is
  suppressed** (`suppressed_challenge` counter) and a page snapshot is saved to
  `output/` — we never emit from an untrusted page.

Staleness != "quiet market": the heartbeat's `ms_since_last_mutation` plus network
silence plus `navigator.onLine` distinguish dead page from dead market; the periodic
resource log keeps long-run memory bounded (rotating logs, capped deques/dedup store).

## 7. Security notes

- Tokens/cookies/`Authorization` are redacted in all logs (`config.REDACT_PATTERNS`).
- The census exporter skips attribute names matching token/auth/session/key.
- **Reminder:** the Telegram bot token hardcoded in the old `fxstreet.py` is
  compromised (it left your machine) — revoke it via @BotFather now and move the new
  token to an env var. Nothing in this probe needs it.

## 8. Phase-0 acceptance gates (what "pass" means)

| Gate | Threshold |
|---|---|
| Functional | Calendar loads; `dom_census.json` shows rows; heartbeat steady 5 s cadence |
| Transport | Discovery report shows the live transport (WS/SSE/poll identified from evidence) |
| Detection | Actual transitions caught with no page reload; `RELEASE` records in `releases.jsonl` |
| Accuracy | Zero false releases; zero emissions while challenge-detected |
| Reliability | Zero missed **high-impact** releases across the benchmark window |
| Latency (detector) | median `network_to_python_ms` ≤ 500 ms; p95 ≤ 1500 ms; p99 ≤ 3000 ms |
| Latency (ours only) | `network_to_dom` + `dom_to_python` median ≤ 100 ms (this is the part we own) |
| Stability | 72 h soak: no unrecovered crash; no heartbeat gap > 20 s outside tested windows |
| Recovery | Kill Chromium (`pkill -f chromium`) / kill network / reload page → recovers to LIVE unaided |
| Parser | Confidence ≥ 50 required to emit; low-confidence → DEGRADED + census snapshot, never emit |

Recovery drill: `pkill -f chromium` while running → expect `page_crash` or browser
death → ladder climbs to level 4 → LIVE again, with all of it in `probe.jsonl`.

## 9. After it passes — PC vs Android (let the benchmark decide)

| | Playwright/Chromium (this) | Android WebView appliance |
|---|---|---|
| Latency | same class (~50-300 ms detector) | same class |
| Transport resilience | same (browser-observed class) | same |
| Hardware | needs always-on PC/server/VM | dedicated cheap phone |
| Screen | n/a (headless or xvfb) | brightness-0 black screen (Phase-0 probe tests this) |
| Ops | systemd service, ~300 MB RAM browser | foreground activity + service |
| Anti-bot | full desktop Chromium fingerprint | WebView UA (mitigated w/ desktop UA) |
| Maintenance | playwright+chromium updates | WebView updates via Play |

They are the same robustness class; the choice is operational. Ship this on whatever
always-on machine you have now; run the Android Phase-0 probe in parallel and port
acquisition later only if the appliance properties (black screen, no PC, low power)
win for your deployment.

## 10. Sending results back

Attach: `output/benchmark.jsonl`, the first ~50 lines of `logs/releases.jsonl`, the
latest `output/dom_census.json`, the transport-discovery block from console output,
plus machine/OS/Python/Playwright versions. Include which releases you watched as
ground truth (scheduled times) so missed/dup analysis is possible.


## 14. Telegram notifications (optional live signal)

The probe can mirror its operational state to a Telegram chat: startup proof
(with a snapshot of upcoming events), genuinely new releases as they are
detected, revisions as a separate message, health transitions
(DEGRADED / RECOVERING / RECOVERED / FAILED), and a quiet-market heartbeat
every 6 h so silence is never ambiguous.

Guarantees: Telegram is fire-and-forget — a dedicated daemon thread owns all
sending, the queue is bounded (overflow drops and counts, never blocks), and
if Telegram is unreachable the probe continues exactly as before.
`logs/releases.jsonl` remains the source of truth; a lost message never means
a lost release.

Setup:
    pip install -r requirements.txt          # adds python-dotenv
    copy .env.example .env                   # then fill in the two values
    python probe.py

Messages are plain text (no Markdown), so economic-data characters can never
break parsing. Tune or disable in config.py: TG_NOTIFY_STATE,
TG_QUIET_HEARTBEAT_S (0 = off), TG_STATE_MIN_INTERVAL_S.
Telegram send counters appear in every `resources` log line.
=======
# Economic-Calander-FX-Street-
>>>>>>> e51ec40474025177b62cabbcc5ad970c9e4a28db
