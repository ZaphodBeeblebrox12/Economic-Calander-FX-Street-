India filter via request rewriting - v2 (placement map)
======================================================
probe.py        -> project root  (adds the eventDates route interceptor)
config.py       -> project root  (adds EXTRA_COUNTRIES = "IN", FXS_STATE_FILE)
set_filters.py  -> project root  (verification script - run once)

WHY v2: the Filter panel checkbox crashes the page renderer (observed), so
we never touch the UI. The calendar's country selection is just a query
param on the public eventDates API. The probe now intercepts that request
and appends countries=IN - the page renders INR rows as if the filter
was ticked. Registered at CONTEXT level, so it survives page reloads and
the full recovery ladder (new context re-registers it).

INSTALL
-------
1. Stop the probe (Ctrl+C).
2. Copy probe.py, config.py, set_filters.py into the project root.
3. python set_filters.py     <- verifies: expect "[check] rows=.. INR rows>0"
4. python probe.py

To add MORE countries later: edit EXTRA_COUNTRIES in config.py, e.g.
EXTRA_COUNTRIES = "IN,BR,ZA"   (comma-separated ISO codes)

WHAT WAS DIAGNOSED
------------------
- localStorage 'calendarSettings' = {} (site defaults; no schema to spoof)
- eventDates URL carries countries=US&countries=UK&... as repeated params;
  appending countries=IN works because the API accepts repeated keys
- cf_clearance cookie present; fxs_state.json keeps it across relaunches

If verification prints INR rows=0, paste the 'eventDates request' line -
the categories= filter may need adjusting too.
