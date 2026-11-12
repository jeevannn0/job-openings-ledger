// K9 openings: a Home Screen / Lock Screen widget for the ledger, run by the Scriptable app on the reader's iPhone.
//
// iOS gives web apps no widgets, so this script draws one, in the style of a car app's widget: a near-black card,
// one big number (openings), a thin bar (today's refreshes done), a hero (the K9 mark, drawn natively), a status
// line, and a row of round action buttons; the large size adds the newest openings underneath. It reads
// widget.json, published next to the page after every refresh, and keeps the last copy so a dead connection
// still shows something. Tapping a row or a button opens that posting or the ledger.
//
// A short loader script on the phone fetches this file from the site, caches it, and calls the exported function
// with the ledger's base URL; this file names no host of its own. Scriptable 1.7 or later.
//
// Widget parameter (long-press the widget, Edit Widget, Parameter), optional, comma separated:
//   Siemens        only this company in the list (any company name as it appears on the page)
//   new            only openings first listed today
//   closing        only openings with a closing date, soonest first
//   rows=5         how many rows in the large size (default 4)
//   purple         purple accents instead of the default green bar

module.exports = async function run(base, parameter) {
  // ---- palette: black card, white type, one accent
  const C = {
    bg: new Color("#0b0b0e"), bg2: new Color("#141418"), card: new Color("#1a1a20"), line: new Color("#26262e"),
    text: new Color("#f5f5f7"), dim: new Color("#8e8e98"), dimmer: new Color("#5a5a64"),
    purple: new Color("#b06bff"), green: new Color("#34d27a"), red: new Color("#ff5a5f"), amber: new Color("#ffb340"),
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
  const opts = { company: null, onlyNew: false, closing: false, rows: 0, purple: false };
  for (const raw of String(parameter || "").split(",")) {
    const p = raw.trim();
    if (!p) continue;
    const m = /^rows\s*=\s*(\d{1,2})$/i.exec(p);
    if (m) opts.rows = Math.min(10, Math.max(1, Number(m[1])));
    else if (/^new$/i.test(p)) opts.onlyNew = true;
    else if (/^closing$/i.test(p)) opts.closing = true;
    else if (/^purple$/i.test(p)) opts.purple = true;
    else opts.company = p.toLowerCase();
  }
  const accent = opts.purple ? C.purple : C.green, accentHex = opts.purple ? "#b06bff" : "#34d27a";

  // ---- time (the feed's dates are plain YYYY-MM-DD; the stamp is IST, the ledger's clock)
  const dayNum = (iso) => { const [y, m, d] = iso.split("-").map(Number); return Date.UTC(y, m - 1, d) / 864e5; };
  const now = new Date();
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 864e5;
  const agoText = (iso, seen) => {
    const d = Math.max(0, Math.round(today - dayNum(iso)));
    return (seen ? "seen " : "") + (d === 0 ? "today" : d === 1 ? "1d ago" : `${d}d ago`);
  };
  const fmtDay = (iso) => `${MON[Number(iso.slice(5, 7)) - 1]} ${Number(iso.slice(8, 10))}`;
  const closeIn = (iso) => iso ? Math.round(dayNum(iso) - today) : null;
  const checked = feed && feed.checked_at ? Date.parse(feed.checked_at) : NaN;
  const stamp = feed && feed.checked_at ? feed.checked_at.slice(11, 16) : "";
  const ageMin = Number.isNaN(checked) ? null : Math.max(0, Math.floor((Date.now() - checked) / 6e4));
  // Eight refreshes a day on the hour IST (00, 03, ... 21): how many are done, and when the next one is.
  const hourIst = Number.isNaN(checked) ? null : Math.floor(((checked + 330 * 6e4) / 36e5) % 24);
  const slotsDone = hourIst === null ? 0 : Math.floor(hourIst / 3) + 1;
  const nextSlot = hourIst === null ? "" : `${String((Math.floor(hourIst / 3) * 3 + 3) % 24).padStart(2, "0")}:00`;
  const status = !feed ? ["no data", C.red] : offline ? ["offline", C.amber] : ageMin !== null && ageMin > 390 ? ["stale", C.amber] : ["live", accent];
  const initials = (c) => { const w = c.split(/\s+/).filter((x) => /[A-Za-z0-9]/.test(x)); return (w.length > 1 ? w[0][0] + w[1][0] : c.slice(0, 2)).toUpperCase(); };

  // ---- the rows the list shows
  let jobs = feed ? feed.jobs.slice() : [];
  if (opts.company) jobs = jobs.filter((j) => j.company.toLowerCase() === opts.company);
  if (opts.onlyNew) jobs = jobs.filter((j) => dayNum(j.first_seen) >= today);
  if (opts.closing) jobs = jobs.filter((j) => j.closing && closeIn(j.closing) >= 0).sort((a, b) => dayNum(a.closing) - dayNum(b.closing));
  const newest = feed ? feed.jobs[0] : null;
  const soonest = feed ? feed.jobs.filter((j) => j.closing && closeIn(j.closing) >= 0).sort((a, b) => dayNum(a.closing) - dayNum(b.closing))[0] : null;

  // ---- drawings: the K9 mark (the page's pixel wordmark, 72x15 cells in four opacities) and the bar
  const MARK = [
    [0.25, "0,0,1,1 24,0,1,1 38,0,1,1 42,0,1,1 66,0,1,1 9,1,1,1 37,1,1,1 9,2,1,1 32,2,1,1 34,2,1,1 9,3,1,1 31,3,1,1 71,3,1,1 9,4,1,1 28,4,1,1 46,4,1,1 46,5,1,1 24,6,1,1 41,6,1,1 19,7,1,1 44,8,1,1 9,9,5,1 20,9,1,1 60,9,2,1 9,10,1,1 16,10,1,1 29,10,1,1 9,11,1,1 18,11,1,1 49,11,1,1 71,11,1,1 9,12,1,1 9,13,1,1 0,14,1,1 9,14,1,1 25,14,1,1 51,14,1,1 67,14,1,1"],
    [0.5, "1,0,8,1 36,0,1,1 43,0,3,1 63,0,3,1 0,1,1,1 22,1,1,1 33,1,1,1 35,1,1,1 39,1,1,1 69,1,1,1 0,2,1,1 20,2,1,1 38,2,1,1 0,3,1,1 18,3,1,1 38,3,1,1 48,3,14,1 0,4,1,1 16,4,1,1 27,4,1,1 71,4,1,1 0,5,1,1 9,5,5,1 25,5,1,1 40,5,1,1 71,5,1,1 0,6,1,1 23,6,1,1 48,6,13,1 71,6,1,1 0,7,1,1 43,7,1,1 71,7,1,1 0,8,1,1 71,8,1,1 0,9,1,1 14,9,1,1 46,9,1,1 57,9,3,1 63,9,1,1 71,9,1,1 0,10,1,1 22,10,4,1 28,10,1,1 71,10,1,1 0,11,1,1 19,11,1,1 50,11,12,1 0,12,1,1 21,12,1,1 0,13,1,1 23,13,1,1 69,13,1,1 26,14,1,1 29,14,1,1 63,14,4,1"],
    [0.75, "25,0,11,1 37,0,1,1 46,0,17,1 34,1,1,1 40,1,1,1 70,2,1,1 30,3,1,1 47,3,1,1 62,3,1,1 39,4,1,1 63,4,1,1 14,5,1,1 63,5,1,1 22,6,1,1 42,6,1,1 47,6,1,1 61,6,1,1 45,8,1,1 63,8,1,1 19,9,1,1 47,9,10,1 17,10,1,1 21,10,1,1 26,10,2,1 63,10,1,1 29,11,1,1 29,12,1,1 50,12,1,1 70,12,1,1 24,13,1,1 29,13,1,1 1,14,8,1 27,14,2,1 57,14,6,1"],
    [1, "1,1,8,1 23,1,10,1 41,1,28,1 1,2,8,1 21,2,11,1 39,2,31,1 1,3,8,1 19,3,11,1 39,3,8,1 63,3,8,1 1,4,8,1 17,4,10,1 40,4,6,1 64,4,7,1 1,5,8,1 15,5,10,1 41,5,5,1 64,5,7,1 1,6,21,1 43,6,4,1 62,6,9,1 1,7,18,1 44,7,27,1 1,8,18,1 46,8,17,1 64,8,7,1 1,9,8,1 15,9,4,1 64,9,7,1 1,10,8,1 18,10,3,1 64,10,7,1 1,11,8,1 20,11,9,1 62,11,9,1 1,12,8,1 22,12,7,1 51,12,19,1 1,13,8,1 25,13,4,1 51,13,18,1 52,14,5,1"],
  ];
  const drawMark = (cell, hex) => {
    // Cells are 1:2 (the wordmark's designed proportion); a soft glow of the accent sits behind the lit cells.
    const W = 72 * cell, H = 15 * cell * 2, pad = cell * 3;
    const ctx = new DrawContext();
    ctx.size = new Size(W + pad * 2, H + pad * 2);
    ctx.opaque = false;
    ctx.respectScreenScale = true;
    for (const [op, list] of MARK) {
      if (op < 1) continue;
      ctx.setFillColor(new Color(hex, 0.08));
      for (const r of list.split(" ")) {
        const [x, y, w, h] = r.split(",").map(Number);
        ctx.fillEllipse(new Rect(pad + x * cell - cell * 2, pad + y * cell * 2 - cell * 2, w * cell + cell * 4, h * cell * 2 + cell * 4));
      }
    }
    for (const [op, list] of MARK) {
      ctx.setFillColor(new Color(hex, op));
      for (const r of list.split(" ")) {
        const [x, y, w, h] = r.split(",").map(Number);
        ctx.fillRect(new Rect(pad + x * cell, pad + y * cell * 2, w * cell, h * cell * 2));
      }
    }
    return ctx.getImage();
  };
  const drawBar = (width, fraction, color) => {
    const H = 6, ctx = new DrawContext();
    ctx.size = new Size(width, H);
    ctx.opaque = false;
    ctx.respectScreenScale = true;
    const track = new Path();
    track.addRoundedRect(new Rect(0, 0, width, H), H / 2, H / 2);
    ctx.addPath(track);
    ctx.setFillColor(C.line);
    ctx.fillPath();
    const w = Math.max(H, Math.round(width * Math.min(1, Math.max(0, fraction))));
    const fill = new Path();
    fill.addRoundedRect(new Rect(0, 0, w, H), H / 2, H / 2);
    ctx.addPath(fill);
    ctx.setFillColor(color);
    ctx.fillPath();
    return ctx.getImage();
  };

  // ---- building blocks
  const family = config.widgetFamily || "large";
  const w = new ListWidget();
  w.url = base;
  w.refreshAfterDate = new Date(Date.now() + 20 * 60 * 1000);
  const text = (into, s, font, color, lines) => {
    const t = into.addText(s);
    t.font = font;
    t.textColor = color;
    t.lineLimit = lines || 1;
    return t;
  };
  const image = (into, img, wpx, hpx) => {
    const i = into.addImage(img);
    i.imageSize = new Size(wpx, hpx);
    i.resizable = true;
    return i;
  };
  const dot = (into, color) => {
    const d = into.addStack();
    d.size = new Size(6, 6);
    d.cornerRadius = 3;
    d.backgroundColor = color;
  };
  const statusLine = (into, size) => {
    const s = into.addStack();
    s.layoutHorizontally();
    s.centerAlignContent();
    s.spacing = 5;
    dot(s, status[1]);
    text(s, status[0].toUpperCase(), Font.boldSystemFont(size), status[1]);
    if (stamp) text(s, `· ${offline ? "last " : "checked "}${stamp} IST`, Font.mediumSystemFont(size), C.dim);
  };
  const bigNumber = (into, size) => {
    const s = into.addStack();
    s.layoutHorizontally();
    s.bottomAlignContent();
    s.spacing = 5;
    const n = text(s, feed ? String(feed.total_jobs) : "—", Font.heavyRoundedSystemFont(size), C.text);
    n.minimumScaleFactor = 0.7;
    const l = text(s, "openings", Font.mediumSystemFont(Math.round(size * 0.36)), C.dim);
    l.textOpacity = 0.9;
  };
  const subNumbers = (into, size) => {
    const s = into.addStack();
    s.layoutHorizontally();
    s.centerAlignContent();
    s.spacing = 8;
    text(s, `+${feed.new_today} today`, Font.semiboldSystemFont(size), accent);
    if (feed.closing_soon_count) text(s, `· ${feed.closing_soon_count} closing soon`, Font.mediumSystemFont(size), C.dim);
  };
  const bar = (into, width, size) => {
    image(into, drawBar(width, slotsDone / 8, accent), width, 6);
    into.addSpacer(4);
    text(into, hourIst === null ? "no refresh yet" : `${slotsDone} of 8 refreshes today · next ${nextSlot} IST`, Font.mediumMonospacedSystemFont(size), C.dim);
  };
  const button = (into, symbol, label, url, tint) => {
    const col = into.addStack();
    col.layoutVertically();
    col.centerAlignContent();
    col.url = url;
    const ring = col.addStack();
    ring.size = new Size(38, 38);
    ring.cornerRadius = 19;
    ring.backgroundColor = C.card;
    ring.centerAlignContent();
    ring.layoutHorizontally();
    ring.addSpacer();
    const sym = SFSymbol.named(symbol);
    sym.applyFont(Font.systemFont(15));
    const i = ring.addImage(sym.image);
    i.imageSize = new Size(17, 17);
    i.tintColor = tint || C.text;
    ring.addSpacer();
    col.addSpacer(4);
    text(col, label, Font.mediumSystemFont(9), C.dim).centerAlignText();
    return col;
  };
  const buttons = (into) => {
    const row = into.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();
    row.addSpacer();
    button(row, "list.bullet.rectangle.fill", "Ledger", base);
    row.addSpacer();
    button(row, "sparkles", "Newest", newest ? newest.url : base, newest ? accent : C.dimmer);
    row.addSpacer();
    button(row, "clock.badge.exclamationmark", "Closing", soonest ? soonest.url : base, soonest && closeIn(soonest.closing) <= 7 ? C.red : null);
    row.addSpacer();
    button(row, "paperplane.fill", "Apply", newest ? newest.apply_url : base);
    row.addSpacer();
  };
  const rule = (into) => {
    const l = into.addStack();
    l.size = new Size(0, 1);
    l.backgroundColor = C.line;
  };
  const row = (into, j) => {
    const r = into.addStack();
    r.layoutHorizontally();
    r.centerAlignContent();
    r.spacing = 9;
    r.url = j.url;
    const av = r.addStack();
    av.size = new Size(24, 24);
    av.cornerRadius = 12;
    av.backgroundColor = C.card;
    av.centerAlignContent();
    av.addSpacer();
    text(av, initials(j.company), Font.boldSystemFont(9), C.text);
    av.addSpacer();
    const col = r.addStack();
    col.layoutVertically();
    col.spacing = 1;
    text(col, j.title, Font.semiboldSystemFont(12), C.text).minimumScaleFactor = 0.9;
    text(col, `${j.company} · ${j.location} · ${agoText(j.posted, j.seen_basis)}`, Font.systemFont(10), C.dim);
    r.addSpacer();
    const k = closeIn(j.closing);
    if (k !== null && k >= 0 && k <= 7) text(r, k === 0 ? "closes today" : `closes ${fmtDay(j.closing)}`, Font.boldSystemFont(10), C.red);
    else if (dayNum(j.first_seen) >= today) text(r, "NEW", Font.boldSystemFont(10), accent);
    else if (k !== null && k > 7) text(r, fmtDay(j.closing), Font.mediumSystemFont(10), C.dimmer);
  };
  const empty = (into) => {
    into.addSpacer();
    text(into, "No feed yet", Font.semiboldSystemFont(13), C.text);
    text(into, "Open the ledger once on Wi-Fi, then come back.", Font.systemFont(11), C.dim, 2);
    into.addSpacer();
  };

  // ---- layouts
  if (family === "accessoryCircular") {
    w.addAccessoryWidgetBackground = true;
    const s = w.addStack();
    s.layoutVertically();
    s.centerAlignContent();
    text(s, feed ? `+${feed.new_today}` : "K9", Font.heavyRoundedSystemFont(feed ? 20 : 16), Color.white()).centerAlignText();
    text(s, feed ? "new" : "offline", Font.mediumSystemFont(9), Color.white()).centerAlignText();
  } else if (family === "accessoryInline") {
    text(w, feed ? `K9 · ${feed.total_jobs} open · +${feed.new_today} today` : "K9 · offline", Font.mediumSystemFont(12), Color.white());
  } else if (family === "accessoryRectangular") {
    w.addAccessoryWidgetBackground = true;
    w.setPadding(4, 6, 4, 6);
    if (!feed) text(w, "K9 · offline", Font.semiboldSystemFont(12), Color.white());
    else {
      text(w, `${feed.total_jobs} open · +${feed.new_today} today`, Font.semiboldSystemFont(12), Color.white());
      image(w, drawBar(140, slotsDone / 8, Color.white()), 140, 4);
      if (newest) text(w, `${newest.company} – ${newest.title}`, Font.systemFont(10), Color.white()).textOpacity = 0.75;
    }
  } else {
    w.backgroundColor = C.bg;
    if (family === "small") {
      w.setPadding(12, 12, 10, 12);
      if (!feed) { statusLine(w, 9); empty(w); }
      else {
        statusLine(w, 9);
        w.addSpacer(2);
        bigNumber(w, 30);
        subNumbers(w, 11);
        w.addSpacer();
        bar(w, 124, 8);
        w.addSpacer(6);
        const m = w.addStack();
        m.layoutHorizontally();
        m.addSpacer();
        image(m, drawMark(1, accentHex), 78, 36);
      }
    } else if (family === "medium") {
      w.setPadding(12, 14, 10, 14);
      if (!feed) { statusLine(w, 9); empty(w); }
      else {
        const top = w.addStack();
        top.layoutHorizontally();
        top.topAlignContent();
        const left = top.addStack();
        left.layoutVertically();
        statusLine(left, 9);
        left.addSpacer(2);
        bigNumber(left, 34);
        subNumbers(left, 11);
        left.addSpacer(6);
        bar(left, 150, 8);
        top.addSpacer();
        const hero = top.addStack();
        hero.layoutVertically();
        hero.addSpacer(2);
        image(hero, drawMark(2, accentHex), 110, 51);
        w.addSpacer();
        buttons(w);
      }
    } else {
      w.setPadding(14, 16, 12, 16);
      if (!feed) { statusLine(w, 10); empty(w); }
      else {
        const top = w.addStack();
        top.layoutHorizontally();
        top.topAlignContent();
        const left = top.addStack();
        left.layoutVertically();
        statusLine(left, 10);
        left.addSpacer(2);
        bigNumber(left, 40);
        subNumbers(left, 12);
        left.addSpacer(8);
        bar(left, 170, 9);
        top.addSpacer();
        const hero = top.addStack();
        hero.layoutVertically();
        hero.addSpacer(4);
        image(hero, drawMark(2, accentHex), 130, 60);
        w.addSpacer(12);
        buttons(w);
        w.addSpacer(12);
        rule(w);
        w.addSpacer(8);
        const head = w.addStack();
        head.layoutHorizontally();
        text(head, opts.closing ? "CLOSING SOONEST" : opts.onlyNew ? "NEW TODAY" : opts.company ? parameter.toUpperCase() : "NEWLY LISTED", Font.boldSystemFont(9), C.dim);
        head.addSpacer();
        if (jobs.length) text(head, `${jobs.length} in feed`, Font.mediumSystemFont(9), C.dimmer);
        w.addSpacer(6);
        const n = opts.rows || 4;
        const shown = jobs.slice(0, n);
        if (!shown.length) text(w, opts.company ? `No openings for "${parameter}" right now` : "Nothing matches the widget parameter", Font.systemFont(11), C.dim, 2);
        const list = w.addStack();
        list.layoutVertically();
        list.spacing = 7;
        for (const j of shown) row(list, j);
        w.addSpacer();
      }
    }
  }

  if (config.runsInWidget) Script.setWidget(w);
  else if (family === "small") await w.presentSmall();
  else if (family === "medium") await w.presentMedium();
  else await w.presentLarge();
  Script.complete();
};
