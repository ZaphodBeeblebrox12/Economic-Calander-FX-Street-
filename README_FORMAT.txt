FXStreet probe - subscriber format v1 + /test commands
======================================================

NEW IN THIS PACKAGE
-------------------
Telegram commands (send them in the bot chat):

  /test      Instant feedback: posts 3 sample release cards in the new
             format, then ~3s later EDITS the middle one to demo the
             revision behaviour live. Ends with a confirmation line.
  /upcoming  Next 3 real upcoming events scraped live from the page
             (impact dot + flag + time). Skips ALL-DAY holiday rows.
  /help      Command list.

Safety details:
  - Only the configured chat_id (TELEGRAM_CHAT_ID) can trigger commands;
    everyone else is ignored and logged.
  - 5-second anti-flood per command.
  - Commands are handled on the probe's MAIN loop (queued), never on the
    listener thread - Playwright's sync API is not thread-safe.
  - getUpdates failures back off and retry; another getUpdates consumer
    (409) never crashes the probe.

FILES / PLACEMENT MAP (project root, next to config.py)
-------------------------------------------------------
notifier.py -> notifier.py   (v3: adds getUpdates listener + reply())
probe.py    -> probe.py      (v2: adds /test, /upcoming, /help handlers)

INSTALL
-------
1. Stop the probe (Ctrl+C).
2. Copy both files over the old ones.
3. python probe.py
4. Send /test to the bot chat - you should see the cards within seconds.

If the bot does not respond: in a group, commands are always visible to
bots (privacy mode is fine). In a CHANNEL, the bot must be an admin to
read /test. Check logs/probe.jsonl for tg_command / listen_fail entries.
