// K9 openings: a Home Screen / Lock Screen widget for the ledger, run by the Scriptable app on the reader's iPhone.
//
// iOS gives web apps no widgets, so this script draws one: the newest openings in the page's own order (newly
// listed first), the page's counts, a closing badge, and the time of the last refresh. It reads widget.json,
// published next to the page after every refresh, and keeps the last copy so a dead connection still shows
// something. Tapping a row opens that posting; tapping anywhere else opens the ledger.
//
// A short loader script on the phone fetches this file from the site, caches it, and calls the exported function
// with the ledger's base URL; this file names no host of its own. Scriptable 1.7 or later.
//
// Widget parameter (long-press the widget, Edit Widget, Parameter), optional, comma separated:
//   Siemens        only this company (any company name as it appears on the page)
//   new            only openings first listed today
//   closing        only openings with a closing date, soonest first
//   rows=5         how many rows (medium shows 3, large 7 by default)

module.exports = async function run(base, parameter) {
  // ---- palette: the page's purple theme
  const C = {
    bg1: new Color("#140e1e"), bg2: new Color("#0a0711"),
    signal: new Color("#ad5cff"), signalDim: new Color("#ad5cff", 0.18),
    steel: new Color("#ece9f3"), steel2: new Color("#bdb7cc"), muted: new Color("#8d86a2"),
    hull: new Color("#2b2439"), ok: new Color("#2ad49a"), okDim: new Color("#2ad49a", 0.16),
    bad: new Color("#ff5a66"), badDim: new Color("#ff5a66", 0.16), warn: new Color("#f2a35b"),
  };
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // ---- the feed, with the last good copy as a fallback
  const fm = FileManager.local();
  const cachePath = fm.joinPath(fm.documentsDirectory(), "k9-widget-feed.json");
  let feed = null, offline = false;
  try {
    const req = new Request(base + "widget.json");
    req.timeoutInterval = 12;
    const data = await req.loadJSON();
    if (data && data.schema === 1 && Array.isArray(data.jobs)) {
      feed = data;
      fm.writeString(cachePath, JSON.stringify(data));
    }
  } catch (e) { /* fall back to the cached copy below */ }
  if (!feed && fm.fileExists(cachePath)) {
    try { feed = JSON.parse(fm.readString(cachePath)); offline = true; } catch (e) { /* no usable cache */ }
  }

  // ---- the parameter
  const opts = { company: null, onlyNew: false, closing: false, rows: 0 };
  for (const raw of String(parameter || "").split(",")) {
    const p = raw.trim();
    if (!p) continue;
    const m = /^rows\s*=\s*(\d{1,2})$/i.exec(p);
    if (m) opts.rows = Math.min(12, Math.max(1, Number(m[1])));
    else if (/^new$/i.test(p)) opts.onlyNew = true;
    else if (/^closing$/i.test(p)) opts.closing = true;
    else opts.company = p.toLowerCase();
  }

  // ---- dates (the feed's dates are plain YYYY-MM-DD; compare by calendar day in the phone's zone)
  const dayNum = (iso) => { const [y, m, d] = iso.split("-").map(Number); return Date.UTC(y, m - 1, d) / 864e5; };
  const now = new Date();
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 864e5;
  const agoText = (iso, seen) => {
    const d = Math.max(0, Math.round(today - dayNum(iso)));
    return (seen ? "seen " : "") + (d === 0 ? "today" : d === 1 ? "1 day ago" : `${d} days ago`);
  };
  const fmtDay = (iso) => `${MON[Number(iso.slice(5, 7)) - 1]} ${Number(iso.slice(8, 10))}`;
  const closeIn = (iso) => iso ? Math.round(dayNum(iso) - today) : null;
  const stamp = feed && feed.checked_at ? feed.checked_at.slice(11, 16) : "";
  const initials = (c) => { const w = c.split(/\s+/).filter((x) => /[A-Za-z0-9]/.test(x)); return (w.length > 1 ? w[0][0] + w[1][0] : c.slice(0, 2)).toUpperCase(); };

  // ---- the rows this widget shows
  let jobs = feed ? feed.jobs.slice() : [];
  if (opts.company) jobs = jobs.filter((j) => j.company.toLowerCase() === opts.company);
  if (opts.onlyNew) jobs = jobs.filter((j) => dayNum(j.first_seen) >= today);
  if (opts.closing) jobs = jobs.filter((j) => j.closing && closeIn(j.closing) >= 0).sort((a, b) => dayNum(a.closing) - dayNum(b.closing));

  const family = config.widgetFamily || "large";
  const w = new ListWidget();
  w.url = base;
  w.refreshAfterDate = new Date(Date.now() + 20 * 60 * 1000);
  const g = new LinearGradient();
  g.colors = [C.bg1, C.bg2];
  g.locations = [0, 1];
  g.startPoint = new Point(0, 0);
  g.endPoint = new Point(1, 1);

  const text = (into, s, font, color, lines) => {
    const t = into.addText(s);
    t.font = font;
    t.textColor = color;
    t.lineLimit = lines || 1;
    return t;
  };
  const pill = (into, s, color, dim) => {
    const p = into.addStack();
    p.setPadding(2, 6, 2, 6);
    p.cornerRadius = 4;
    p.backgroundColor = dim;
    text(p, s, Font.boldMonospacedSystemFont(9), color);
    return p;
  };
  const row = (into, j, compact) => {
    const r = into.addStack();
    r.layoutHorizontally();
    r.centerAlignContent();
    r.spacing = 8;
    r.url = j.url;
    const av = r.addStack();
    av.size = new Size(26, 26);
    av.cornerRadius = 6;
    av.backgroundColor = C.signalDim;
    av.centerAlignContent();
    av.addSpacer();
    text(av, initials(j.company), Font.boldSystemFont(10), C.signal);
    av.addSpacer();
    const col = r.addStack();
    col.layoutVertically();
    col.spacing = 1;
    const title = text(col, j.title, Font.semiboldSystemFont(compact ? 11 : 12), C.steel);
    title.minimumScaleFactor = 0.9;
    const sub = `${j.company} · ${j.location} · ${agoText(j.posted, j.seen_basis)}`;
    text(col, sub, Font.systemFont(compact ? 9 : 10), C.muted);
    r.addSpacer();
    const k = closeIn(j.closing);
    const isNew = dayNum(j.first_seen) >= today;
    if (k !== null && k >= 0 && k <= 7) pill(r, k === 0 ? "closes today" : `closes ${fmtDay(j.closing)}`, C.bad, C.badDim);
    else if (isNew) pill(r, "NEW", C.signal, C.signalDim);
    else if (k !== null && k > 7) text(r, `closes ${fmtDay(j.closing)}`, Font.mediumMonospacedSystemFont(9), C.muted);
  };
  const header = (into) => {
    const h = into.addStack();
    h.layoutHorizontally();
    h.centerAlignContent();
    text(h, "K9", Font.heavySystemFont(14), C.signal);
    h.addSpacer(6);
    text(h, "openings", Font.mediumSystemFont(12), C.steel2);
    h.addSpacer();
    text(h, offline ? `offline · ${stamp} IST` : stamp ? `checked ${stamp} IST` : "no data yet", Font.mediumMonospacedSystemFont(9), offline ? C.warn : C.muted);
  };
  const counts = (into, big) => {
    const s = into.addStack();
    s.layoutHorizontally();
    s.bottomAlignContent();
    s.spacing = 4;
    text(s, String(feed.total_jobs), Font.heavyRoundedSystemFont(big ? 30 : 20), C.steel);
    text(s, "open", Font.mediumSystemFont(big ? 12 : 10), C.muted);
    s.addSpacer(10);
    text(s, `+${feed.new_today}`, Font.heavyRoundedSystemFont(big ? 30 : 20), C.ok);
    text(s, "new today", Font.mediumSystemFont(big ? 12 : 10), C.muted);
    if (feed.closing_soon_count) {
      s.addSpacer(10);
      text(s, String(feed.closing_soon_count), Font.heavyRoundedSystemFont(big ? 30 : 20), C.bad);
      text(s, "closing soon", Font.mediumSystemFont(big ? 12 : 10), C.muted);
    }
  };
  const rule = (into) => {
    const l = into.addStack();
    l.size = new Size(0, 1);
    l.backgroundColor = C.hull;
  };

  if (family === "accessoryCircular") {
    w.addAccessoryWidgetBackground = true;
    const s = w.addStack();
    s.layoutVertically();
    s.centerAlignContent();
    text(s, feed ? `+${feed.new_today}` : "K9", Font.heavyRoundedSystemFont(feed ? 20 : 16), Color.white()).centerAlignText();
    text(s, feed ? "new" : "offline", Font.mediumSystemFont(9), Color.white()).centerAlignText();
  } else if (family === "accessoryInline") {
    text(w, feed ? `K9 · +${feed.new_today} new · ${feed.total_jobs} open` : "K9 · offline", Font.mediumSystemFont(12), Color.white());
  } else if (family === "accessoryRectangular") {
    w.addAccessoryWidgetBackground = true;
    w.setPadding(4, 6, 4, 6);
    if (!feed) text(w, "K9 · offline", Font.semiboldSystemFont(12), Color.white());
    else {
      text(w, `K9 · +${feed.new_today} new · ${feed.total_jobs} open`, Font.semiboldSystemFont(12), Color.white());
      const j = jobs[0];
      if (j) {
        text(w, j.title, Font.systemFont(11), Color.white());
        text(w, `${j.company} · ${agoText(j.posted, j.seen_basis)}`, Font.systemFont(10), Color.white()).textOpacity = 0.7;
      }
    }
  } else {
    w.backgroundGradient = g;
    w.setPadding(12, 14, 12, 14);
    header(w);
    if (!feed) {
      w.addSpacer();
      text(w, "No feed yet", Font.semiboldSystemFont(13), C.steel);
      text(w, "Open the ledger once on Wi-Fi, then come back.", Font.systemFont(11), C.muted, 2);
      w.addSpacer();
    } else if (family === "small") {
      w.addSpacer(4);
      const a = w.addStack();
      a.layoutHorizontally();
      a.bottomAlignContent();
      a.spacing = 4;
      text(a, String(feed.total_jobs), Font.heavyRoundedSystemFont(26), C.steel);
      text(a, "open", Font.mediumSystemFont(10), C.muted);
      text(w, `+${feed.new_today} new today${feed.closing_soon_count ? ` · ${feed.closing_soon_count} closing soon` : ""}`, Font.semiboldSystemFont(10), C.ok).minimumScaleFactor = 0.8;
      w.addSpacer(8);
      const j = jobs[0];
      if (j) {
        const s = w.addStack();
        s.layoutVertically();
        s.url = j.url;
        text(s, j.title, Font.semiboldSystemFont(11), C.steel, 2);
        text(s, `${j.company} · ${agoText(j.posted, j.seen_basis)}`, Font.systemFont(9), C.muted);
      }
      w.addSpacer();
    } else {
      const n = opts.rows || (family === "large" || family === "extraLarge" ? 7 : 3);
      w.addSpacer(6);
      counts(w, family !== "medium");
      w.addSpacer(8);
      rule(w);
      w.addSpacer(6);
      const shown = jobs.slice(0, n);
      if (!shown.length) text(w, opts.company ? `No openings for "${parameter}" right now` : "Nothing matches the widget parameter", Font.systemFont(11), C.muted, 2);
      const list = w.addStack();
      list.layoutVertically();
      list.spacing = family === "medium" ? 5 : 7;
      for (const j of shown) row(list, j, family === "medium");
      w.addSpacer();
      if (family !== "medium") {
        rule(w);
        w.addSpacer(5);
        const f = w.addStack();
        f.layoutHorizontally();
        text(f, "newly listed first · tap a row to open the posting", Font.mediumMonospacedSystemFont(9), C.muted);
        f.addSpacer();
        if (jobs.length > shown.length) text(f, `+${jobs.length - shown.length} more`, Font.mediumMonospacedSystemFont(9), C.signal);
      }
    }
  }

  if (config.runsInWidget) Script.setWidget(w);
  else if (family === "small") await w.presentSmall();
  else if (family === "medium") await w.presentMedium();
  else await w.presentLarge();
  Script.complete();
};
