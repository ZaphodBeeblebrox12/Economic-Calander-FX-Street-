/* ============================================================================
 * FXStreet Playwright probe — browser-side observer (top frame only)
 * Selectors PINNED 2026-09-12: tr.fxs_c_row[data-event-date-id],
 * cells .fxs_c_time/.fxs_c_currency/.fxs_c_name/.fxs_c_actual/.fxs_c_consensus/
 * .fxs_c_previous, impact icon .fxs_c_impact-icon.fxs_c_impact-{high|medium|low},
 * country from .fxs_c_flag [title].
 * ==========================================================================*/
(function () {
    "use strict";
    if (window.__fxProbeInstalled) return;
    try { if (window.top !== window.self) return; } catch (e) { return; }  // top frame only
    window.__fxProbeInstalled = true;

    var pending = [];
    function emit(obj) {
        var s = JSON.stringify(obj);
        if (typeof window.probeEmit === "function") {
            try { window.probeEmit(s); return; } catch (e) { /* fall through */ }
        }
        pending.push(s);
    }
    setInterval(function () {
        if (pending.length && typeof window.probeEmit === "function") {
            var q = pending; pending = [];
            for (var i = 0; i < q.length; i++) { try { window.probeEmit(q[i]); } catch (e) {} }
        }
    }, 500);

    var CONFIG = {
        maxRows: 400,
        rowDebounceMs: 50,
        heartbeatMs: 5000,
        sweepMs: 5000,
        rowSelectors: [
            "tr.fxs_c_row[data-event-date-id]",
            "tr.fxs_c_row",
            "tr[class*='fxs_c_row']"
        ],
        cellSelectors: {
            time:     ".fxs_c_time",
            currency: ".fxs_c_currency",
            name:     ".fxs_c_name",
            actual:   ".fxs_c_actual",
            forecast: ".fxs_c_consensus",
            previous: ".fxs_c_previous",
            revised:  ".fxs_c_revised"
        },
        currencyRe: /\b(USD|EUR|JPY|GBP|AUD|NZD|CAD|CHF|CNY|HKD|SGD|KRW|INR|BRL|MXN|ZAR|SEK|NOK|DKK|PLN|TRY|XAU|XAG|WTI|BRENT|OIL|BTC|ETH)\b/,
        timeRe: /\b([01]?\d|2[0-3]):[0-5]\d\b/,
        time12Re: /\b(\d{1,2}):(\d{2})\s*(AM|PM)\b/i,
        impactRe: /\b(HIGH|MEDIUM|MODERATE|LOW|NON-ECONOMIC|HOLIDAY)\s+IMPACT\b/i,
        impactClassRe: /fxs_c_impact-(high|medium|low)/
    };

    /* ---------------- throttle / visibility probe ---------------- */
    var thr = { fired: 0, expected: 0, last: 0, raf: 0, rafLast: 0 };
    setInterval(function () {
        var n = performance.now();
        if (thr.last > 0) thr.expected += Math.round((n - thr.last) / 1000);
        thr.fired++; thr.last = n;
    }, 1000);
    (function raf() { thr.raf++; thr.rafLast = performance.now(); requestAnimationFrame(raf); })();
    document.addEventListener("visibilitychange", function () {
        emit({ type: "js_log", level: "warn", message: "visibilitychange -> " + document.visibilityState });
    });
    window.addEventListener("error", function (e) {
        emit({ type: "js_error", message: String(e.message || e.error), source: String(e.filename || ""), line: e.lineno || 0 });
    });
    window.addEventListener("unhandledrejection", function (e) {
        emit({ type: "js_error", message: "unhandledrejection: " + String(e.reason) });
    });

    /* ---------------- helpers ---------------- */
    function txt(el) { return el ? (el.textContent || "").trim() : null; }
    function emptyToNull(v) {
        if (v === null || v === undefined) return null;
        var s = String(v).trim();
        return (s === "" || s === "-" || s === "–" || s === "---") ? null : s;
    }
    function pick(row, csv) {
        var sels = csv.split(",");
        for (var i = 0; i < sels.length; i++) {
            try { var el = row.querySelector(sels[i].trim()); if (el) return txt(el); } catch (e) {}
        }
        return null;
    }
    var numRe = /^(-|–|—)?\s*\d[\d,]*(\.\d+)?\s*(%|[KkMmBb])?$/;

    function leafTexts(root) {
        var out = [], w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), n;
        while ((n = w.nextNode())) {
            var s = (n.nodeValue || "").trim();
            if (!s || !n.parentElement) continue;
            var r = n.parentElement.getBoundingClientRect();
            out.push({ text: s, top: r.top, left: r.left });
        }
        out.sort(function (a, b) { return (a.top - b.top) || (a.left - b.left); });
        return out;
    }

    /* ---------------- discovery ---------------- */
    function looksLikeRow(el) {
        if (!el || el.nodeType !== 1) return false;
        var t = el.textContent || "";
        if (t.length > 500) return false;
        if (!CONFIG.timeRe.test(t)) return false;
        return CONFIG.currencyRe.test(t) || CONFIG.impactRe.test(t);
    }
    function discoverRows() {
        var seen = [], i, j, found;
        for (i = 0; i < CONFIG.rowSelectors.length; i++) {
            try {
                found = document.querySelectorAll(CONFIG.rowSelectors[i]);
                for (j = 0; j < found.length; j++) if (seen.indexOf(found[j]) === -1) seen.push(found[j]);
                if (seen.length) break;
            } catch (e) {}
        }
        if (!seen.length) {
            var cand = document.querySelectorAll("tr, li, [role='row'], div");
            for (i = 0; cand[i] && seen.length < CONFIG.maxRows; i++) if (looksLikeRow(cand[i])) seen.push(cand[i]);
        }
        return seen.slice(0, CONFIG.maxRows);
    }

    function rowIdentitySource(row) {
        try {
            return row.getAttribute("data-event-date-id") ||
                   row.getAttribute("data-event-datetime-id") ||
                   row.getAttribute("data-event-id") || row.id || null;
        } catch (e) { return null; }
    }

    /* ---------------- extraction + confidence ---------------- */
    function extract(row) {
        var idSource = rowIdentitySource(row);
        var timeText = pick(row, CONFIG.cellSelectors.time);
        var currency = pick(row, CONFIG.cellSelectors.currency);
        var name = pick(row, CONFIG.cellSelectors.name);
        var actual = pick(row, CONFIG.cellSelectors.actual);
        var forecast = pick(row, CONFIG.cellSelectors.forecast);
        var previous = pick(row, CONFIG.cellSelectors.previous);
        var revised = pick(row, CONFIG.cellSelectors.revised);
        var country = null, impact = null;
        try {
            var flagEl = row.querySelector(".fxs_c_flag [title]") || row.querySelector(".fxs_c_flag");
            if (flagEl) country = flagEl.getAttribute("title") || null;
            var imp = row.querySelector(".fxs_c_impact-icon") || row.querySelector("[class*='fxs_c_impact-']");
            if (imp) {
                var m = (imp.className || "").toString().match(CONFIG.impactClassRe);
                if (m) impact = m[1].toUpperCase();
            }
        } catch (e) {}

        var text = (row.textContent || "").replace(/\s+/g, " ").trim();
        var timeM = (timeText || "").match(CONFIG.timeRe) || text.match(CONFIG.timeRe);
        var curM = currency || (text.match(CONFIG.currencyRe) || [null])[0];
        var impactM = impact || ((text.match(CONFIG.impactRe) || [null, null])[1] || null);

        if (actual === null && forecast === null && previous === null) {
            var nums = leafTexts(row).filter(function (l) { return numRe.test(l.text); }).map(function (l) { return l.text; });
            if (nums.length >= 3) { actual = nums[0]; forecast = nums[1]; previous = nums[2]; }
            else if (nums.length === 2) { forecast = nums[0]; previous = nums[1]; }
            else if (nums.length === 1) { previous = nums[0]; }
        }
        if (!name) {
            name = text.replace(CONFIG.timeRe, "").replace(CONFIG.impactRe, "")
                .replace(curM ? curM : /\b\B/, "").replace(/^\W+|\W+$/g, "").slice(0, 140);
        }

        var conf = 0;
        if (name && name.length >= 4) conf += 25;
        if (curM) conf += 20;
        if (timeM) conf += 20;
        if (previous !== null || forecast !== null) conf += 20;
        if (impactM) conf += 15;
        if (idSource) conf += 10;
        if (country) conf += 5;

        var schedMinute = null;
        if (timeText) {
            var t12 = timeText.match(CONFIG.time12Re);
            if (t12) {
                var hh = parseInt(t12[1], 10) % 12;
                if (t12[3].toUpperCase() === "PM") hh += 12;
                var d = new Date();
                schedMinute = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hh, parseInt(t12[2], 10));
            } else if (timeM) {
                var p = timeM[0].split(":");
                var d2 = new Date();
                schedMinute = Date.UTC(d2.getUTCFullYear(), d2.getUTCMonth(), d2.getUTCDate(),
                                       parseInt(p[0], 10), parseInt(p[1], 10));
            }
        }
        var syn = (name + "|" + (curM || "") + "|" + (schedMinute || ""))
            .toLowerCase().replace(/\s+/g, " ").trim();

        return {
            event_id: idSource,
            event_key: idSource ? "fx:" + idSource : "syn:" + syn,
            event_name: name, currency: curM, country: country,
            scheduled_time: timeM ? timeM[0] : null,
            impact: impactM,
            previous: emptyToNull(previous), forecast: emptyToNull(forecast),
            actual: emptyToNull(actual), revised: emptyToNull(revised),
            confidence: conf,
            row_text_sample: text.slice(0, 160)
        };
    }

    function fieldsEqual(a, b) {
        return ["previous", "forecast", "actual", "revised", "event_name", "currency", "scheduled_time", "impact"]
            .every(function (k) { return (a[k] || null) === (b[k] || null); });
    }

    /* ---------------- row attach + diff ---------------- */
    var cache = {}, elByKey = {}, lastMut = performance.now(), mutCount = 0;

    function attachRow(row) {
        var p;
        try { p = extract(row); } catch (e) { return; }
        if (!p || !p.event_key) return;
        var key = p.event_key;
        if (elByKey[key] === row) return;
        elByKey[key] = row;
        cache[key] = p;
        try {
            new MutationObserver(function (muts) {
                lastMut = performance.now(); mutCount += muts.length;
                debounce(key, row);
            }).observe(row, {
                childList: true, subtree: true, characterData: true,
                attributes: true, attributeFilter: ["class", "style"]
            });
        } catch (e) {
            emit({ type: "js_log", level: "error", message: "row observe failed: " + e });
        }
    }
    var timers = {};
    function debounce(key, row) {
        if (timers[key]) clearTimeout(timers[key]);
        timers[key] = setTimeout(function () { timers[key] = null; checkRow(row, key); }, CONFIG.rowDebounceMs);
    }
    function checkRow(row, key) {
        if (!row.isConnected) return;
        var cur, prev;
        try { cur = extract(row); } catch (e) { emit({ type: "js_log", level: "error", message: "extract: " + e }); return; }
        prev = cache[key];
        if (prev && fieldsEqual(prev, cur)) return;
        cache[key] = cur;
        var oldA = prev ? prev.actual : null;
        var subtype;
        if (oldA === null && cur.actual !== null) subtype = "RELEASE";
        else if (oldA !== null && cur.actual !== null && oldA !== cur.actual) subtype = "REVISION";
        else subtype = "CORRECTION";
        emit({
            type: "economic_event_change", subtype: subtype,
            event_id: cur.event_id, event_key: cur.event_key,
            event_name: cur.event_name, currency: cur.currency, country: cur.country,
            scheduled_time: cur.scheduled_time, impact: cur.impact,
            previous: cur.previous, forecast: cur.forecast,
            actual: cur.actual, revised: cur.revised,
            old_actual: oldA, confidence: cur.confidence,
            row_text_sample: cur.row_text_sample,
            dom_mutation_perf_ms: Math.round(performance.now()),
            js_wall_time: Date.now()
        });
    }

    var containerObserver = new MutationObserver(function (muts) {
        lastMut = performance.now(); mutCount += muts.length;
        for (var i = 0; i < muts.length; i++) {
            var m = muts[i];
            if (m.type !== "childList") continue;
            var nodes = [].concat([].slice.call(m.addedNodes), [].slice.call(m.removedNodes));
            for (var j = 0; j < nodes.length; j++) {
                var nd = nodes[j];
                if (nd.nodeType !== 1) continue;
                if (nd.matches && nd.matches("tr.fxs_c_row")) attachRow(nd);
                try {
                    var inner = nd.querySelectorAll ? nd.querySelectorAll("tr.fxs_c_row") : [];
                    for (var k = 0; k < inner.length && k < 60; k++) attachRow(inner[k]);
                } catch (e) {}
            }
        }
    });

    /* ---------------- overlay dismisser (conservative) ---------------- */
    var overlayClicks = 0;
    var dismissRe = /^(cancel|no thanks|no, thanks|not now|close|later|dismiss|no|got it)$/i;
    function dismissOverlays() {
        if (overlayClicks >= 5) return;
        var cands = document.querySelectorAll("button, [role='button'], a");
        for (var i = 0; i < cands.length && overlayClicks < 5; i++) {
            var el = cands[i];
            var t = (el.textContent || "").trim();
            if (!(dismissRe.test(t) || /^[×✕✖]$/.test(t))) continue;
            var r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            var box = el.closest("[role='dialog'], [class*='modal'], [class*='popup'], [class*='overlay'], [class*='consent'], [id*='popup'], [id*='modal'], [id*='consent']");
            if (!box) continue;
            try {
                el.click(); overlayClicks++;
                emit({ type: "js_log", level: "warn", message: "dismissed overlay button: '" + t + "'" });
            } catch (e) {}
        }
    }

    /* ---------------- census ---------------- */
    function census() {
        var rows = discoverRows();
        emit({
            type: "dom_census", url: location.href, title: document.title,
            visibilityState: document.visibilityState,
            candidateRowsFound: rows.length, rowsAttached: Object.keys(elByKey).length,
            rows: rows.slice(0, 40).map(function (row) {
                var attrs = {};
                if (row.attributes) for (var i = 0; i < row.attributes.length && i < 14; i++) {
                    var a = row.attributes[i];
                    if (/token|auth|session|key/i.test(a.name)) continue;
                    attrs[a.name] = (a.value || "").slice(0, 80);
                }
                return {
                    tag: row.tagName, id: row.id || null,
                    cls: (row.className || "").toString().slice(0, 120),
                    attrs: attrs,
                    childCount: row.children ? row.children.length : 0,
                    textSample: (row.textContent || "").replace(/\s+/g, " ").trim().slice(0, 220)
                };
            })
        });
    }

    /* ---------------- heartbeat ---------------- */
    setInterval(function () {
        var n = performance.now(), rafRate = 0;
        if (thr.rafLast > 0) { var s = (n - thr.rafLast) / 1000; if (s > 0) rafRate = Math.min(240, Math.round(thr.raf / s)); }
        thr.raf = 0;
        emit({
            type: "heartbeat",
            js_wall_time: Date.now(), perf_ms: Math.round(n),
            visibilityState: document.visibilityState, document_hidden: document.hidden,
            has_focus: document.hasFocus(),
            interval_drift_pct: thr.expected > 0 ? Math.round(100 * thr.fired / thr.expected) : 100,
            raf_per_sec: rafRate,
            rows_attached: Object.keys(elByKey).length,
            ms_since_last_mutation: Math.round(n - lastMut),
            online: navigator.onLine
        });
        thr.fired = 0; thr.expected = 0; mutCount = 0;
    }, CONFIG.heartbeatMs);

    /* ---------------- boot ---------------- */
    var bootedAt = Date.now();
    function sweep() {
        // overlay dismissal is a STARTUP concern (consent/promo popups that
        // block rendering). Standing down after 5 min means user-opened
        // panels (filters, settings) are never touched during a session.
        try { if (Date.now() - bootedAt < 5 * 60 * 1000) dismissOverlays(); } catch (e) {}
        var rows = discoverRows();
        for (var i = 0; i < rows.length; i++) attachRow(rows[i]);
    }
    function boot() {
        if (!document.documentElement || !document.body) { setTimeout(boot, 100); return; }
        sweep();
        try {
            containerObserver.observe(document.documentElement, { childList: true, subtree: true });
        } catch (e) {
            emit({ type: "js_log", level: "error", message: "container observer failed: " + e });
        }
        setInterval(sweep, CONFIG.sweepMs);
        setTimeout(census, 8 * 1000);
        setInterval(census, 5 * 60 * 1000);
        emit({ type: "js_log", level: "info", message: "observer booted (top frame)" });
    }

    emit({ type: "clock_anchor", frame: "top", href: location.href,
           perf_origin: performance.timeOrigin,
           perf_now: performance.now(), js_wall_time: Date.now() });
    boot();
})();
