// K9 openings: a Home Screen / Lock Screen widget for the ledger, run by the Scriptable app on the reader's iPhone.
//
// iOS gives web apps no widgets, so this script draws one, in the style of a stock card: a header with the K9 tile,
// the ledger's name and "updated … ago", the open count set like a price with today's change and its percent in
// a pill, a line of the last fortnight's new listings over a dashed typical-day baseline, and a row of company
// icons, the ones with new listings today first with the count beside them. A widget is a still picture that iOS
// redraws every quarter hour or so; the "… ago" age and the Lock Screen countdown are the two things WidgetKit
// lets tick on their own, so they are the only moving parts, and nothing is faked. It reads widget.json,
// published next to the page after every refresh, and keeps the last copy so a dead connection still shows
// something. The company icons come from the URLs in the feed and are cached on the phone after the first fetch.
//
// Background: frosted glass over the wallpaper. iOS does not let a third-party widget see through to the
// wallpaper, so the script keeps a screenshot of an empty Home Screen page (picked once when the script is run
// in the app), crops the piece that sits behind the widget, blurs and darkens it, and uses that. The crop
// position comes from the widget parameter (default: the top row). Without a screenshot the card is dark glass.
//
// A short loader script on the phone fetches this file from the site, caches it, and calls the exported function
// with the ledger's base URL; this file names no host of its own. Scriptable 1.7 or later, iOS 17 or later.
//
// Widget parameter (long-press the widget, Edit Widget, Parameter), optional, comma separated:
//   Siemens        only this company in the large size's list (any company name as it appears on the page)
//   new            only openings first listed today
//   closing        only openings with a closing date, soonest first
//   rows=5         how many rows in the large size (default 4)
//   bg=middle      where the widget sits, for the wallpaper crop: top, middle, bottom (medium and large), or
//                  top-left, top-right, middle-left, middle-right, bottom-left, bottom-right (small). Default top.
//   nolabels       the Home Screen uses large icons without labels (changes where the widget rows sit)
//   glass          dark glass background without the wallpaper
//   black          plain black background
//   photo          pick the wallpaper screenshot again the next time the script is run in the app
//   glitch         the page's logo glitch on the mark (a new frame at every refresh)

module.exports = async function run(base, parameter) {
  // ---- palette
  const C = {
    bg: new Color("#000000"), text: new Color("#f5f5f7"), dim: new Color("#8e8e93"), dimmer: new Color("#5a5a5f"),
    green: new Color("#30d158"), purple: new Color("#bf5af2"), red: new Color("#ff453a"), amber: new Color("#ff9f0a"),
  };
  const GREEN = "#30d158", PURPLE = "#bf5af2";
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fm = FileManager.local();
  const docs = fm.documentsDirectory();

  // ---- the feed, with the last good copy as a fallback
  const cachePath = fm.joinPath(docs, "k9-widget-feed.json");
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
  if (feed && !Array.isArray(feed.companies)) feed.companies = [];           // a cached copy from before the row
  if (feed && !Array.isArray(feed.new_by_day)) feed.new_by_day = [];

  // ---- the parameter
  const opts = { company: null, onlyNew: false, closing: false, rows: 0, bg: "top", nolabels: false, glass: false, black: false, photo: false, glitch: false };
  for (const raw of String(parameter || "").split(",")) {
    const p = raw.trim();
    if (!p) continue;
    let m;
    if ((m = /^rows\s*=\s*(\d{1,2})$/i.exec(p))) opts.rows = Math.min(10, Math.max(1, Number(m[1])));
    else if ((m = /^bg\s*=\s*((?:top|middle|bottom)(?:-(?:left|right))?)$/i.exec(p))) opts.bg = m[1].toLowerCase();
    else if (/^new$/i.test(p)) opts.onlyNew = true;
    else if (/^closing$/i.test(p)) opts.closing = true;
    else if (/^nolabels$/i.test(p)) opts.nolabels = true;
    else if (/^glass$/i.test(p)) opts.glass = true;
    else if (/^black$/i.test(p)) opts.black = true;
    else if (/^photo$/i.test(p)) opts.photo = true;
    else if (/^glitch$/i.test(p)) opts.glitch = true;
    else if (/^purple$/i.test(p)) { /* the old accent switch; the mark is purple now */ }
    else opts.company = p.toLowerCase();
  }

  // ---- time (the feed's dates are plain YYYY-MM-DD; the stamp is IST, the ledger's clock)
  const dayNum = (iso) => { const [y, m, d] = iso.split("-").map(Number); return Date.UTC(y, m - 1, d) / 864e5; };
  const now = new Date();
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 864e5;
  const fmtDay = (iso) => `${MON[Number(iso.slice(5, 7)) - 1]} ${Number(iso.slice(8, 10))}`;
  const closeIn = (iso) => iso ? Math.round(dayNum(iso) - today) : null;
  const checked = feed && feed.checked_at ? Date.parse(feed.checked_at) : NaN;
  const checkedDate = Number.isNaN(checked) ? null : new Date(checked);
  const stamp = feed && feed.checked_at ? feed.checked_at.slice(11, 16) : "";
  const ageMin = Number.isNaN(checked) ? null : Math.max(0, Math.floor((Date.now() - checked) / 6e4));
  const IST = 330 * 6e4;
  const nextSlot = new Date(Math.ceil((Date.now() + IST) / 108e5) * 108e5 - IST);     // the next 3-hour slot, IST
  const nextLabel = `${String(new Date(nextSlot.getTime() + IST).getUTCHours()).padStart(2, "0")}:00`;
  const stale = !feed ? "no data" : offline ? "offline" : ageMin !== null && ageMin > 390 ? "stale" : "";

  // ---- the numbers
  const total = feed ? feed.total_jobs : 0, newToday = feed ? feed.new_today : 0;
  const pct = feed && total > newToday ? (newToday / (total - newToday) * 100).toFixed(1) : "0.0";
  const days = feed && feed.new_by_day.length ? feed.new_by_day : [0];
  const sortedDays = days.slice().sort((a, b) => a - b);
  const ref = Math.max(1, sortedDays[Math.floor(0.8 * (sortedDays.length - 1))]);      // the typical day
  const fmtNum = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  let jobs = feed ? feed.jobs.slice() : [];
  if (opts.company) jobs = jobs.filter((j) => j.company.toLowerCase() === opts.company);
  if (opts.onlyNew) jobs = jobs.filter((j) => dayNum(j.first_seen) >= today);
  if (opts.closing) jobs = jobs.filter((j) => j.closing && closeIn(j.closing) >= 0).sort((a, b) => dayNum(a.closing) - dayNum(b.closing));

  // ---- the company icons: fetched once from the URLs in the feed, then read from the phone's cache
  const iconDir = fm.joinPath(docs, "k9-icons");
  const iconFor = async (c) => {
    const slug = c.name.toLowerCase().replace(/[^a-z0-9]+/g, "-");
    const path = fm.joinPath(iconDir, `${slug}.png`);
    if (fm.fileExists(path)) { try { return fm.readImage(path); } catch (e) { /* re-fetch below */ } }
    if (!/^https:\/\/[^/]+\//.test(c.icon)) return null;
    try {
      const req = new Request(c.icon);
      req.timeoutInterval = 6;
      const img = await req.loadImage();
      if (!img) return null;
      if (!fm.fileExists(iconDir)) fm.createDirectory(iconDir, true);
      fm.writeImage(path, img);
      return img;
    } catch (e) { return null; }
  };

  // ---- drawings
  // The K9 mark: the page's pixel wordmark (72x15 cells in four opacities), 1:2 cells, an optional soft glow.
  const MARK = [
    [0.25, "0,0,1,1 24,0,1,1 38,0,1,1 42,0,1,1 66,0,1,1 9,1,1,1 37,1,1,1 9,2,1,1 32,2,1,1 34,2,1,1 9,3,1,1 31,3,1,1 71,3,1,1 9,4,1,1 28,4,1,1 46,4,1,1 46,5,1,1 24,6,1,1 41,6,1,1 19,7,1,1 44,8,1,1 9,9,5,1 20,9,1,1 60,9,2,1 9,10,1,1 16,10,1,1 29,10,1,1 9,11,1,1 18,11,1,1 49,11,1,1 71,11,1,1 9,12,1,1 9,13,1,1 0,14,1,1 9,14,1,1 25,14,1,1 51,14,1,1 67,14,1,1"],
    [0.5, "1,0,8,1 36,0,1,1 43,0,3,1 63,0,3,1 0,1,1,1 22,1,1,1 33,1,1,1 35,1,1,1 39,1,1,1 69,1,1,1 0,2,1,1 20,2,1,1 38,2,1,1 0,3,1,1 18,3,1,1 38,3,1,1 48,3,14,1 0,4,1,1 16,4,1,1 27,4,1,1 71,4,1,1 0,5,1,1 9,5,5,1 25,5,1,1 40,5,1,1 71,5,1,1 0,6,1,1 23,6,1,1 48,6,13,1 71,6,1,1 0,7,1,1 43,7,1,1 71,7,1,1 0,8,1,1 71,8,1,1 0,9,1,1 14,9,1,1 46,9,1,1 57,9,3,1 63,9,1,1 71,9,1,1 0,10,1,1 22,10,4,1 28,10,1,1 71,10,1,1 0,11,1,1 19,11,1,1 50,11,12,1 0,12,1,1 21,12,1,1 0,13,1,1 23,13,1,1 69,13,1,1 26,14,1,1 29,14,1,1 63,14,4,1"],
    [0.75, "25,0,11,1 37,0,1,1 46,0,17,1 34,1,1,1 40,1,1,1 70,2,1,1 30,3,1,1 47,3,1,1 62,3,1,1 39,4,1,1 63,4,1,1 14,5,1,1 63,5,1,1 22,6,1,1 42,6,1,1 47,6,1,1 61,6,1,1 45,8,1,1 63,8,1,1 19,9,1,1 47,9,10,1 17,10,1,1 21,10,1,1 26,10,2,1 63,10,1,1 29,11,1,1 29,12,1,1 50,12,1,1 70,12,1,1 24,13,1,1 29,13,1,1 1,14,8,1 27,14,2,1 57,14,6,1"],
    [1, "1,1,8,1 23,1,10,1 41,1,28,1 1,2,8,1 21,2,11,1 39,2,31,1 1,3,8,1 19,3,11,1 39,3,8,1 63,3,8,1 1,4,8,1 17,4,10,1 40,4,6,1 64,4,7,1 1,5,8,1 15,5,10,1 41,5,5,1 64,5,7,1 1,6,21,1 43,6,4,1 62,6,9,1 1,7,18,1 44,7,27,1 1,8,18,1 46,8,17,1 64,8,7,1 1,9,8,1 15,9,4,1 64,9,7,1 1,10,8,1 18,10,3,1 64,10,7,1 1,11,8,1 20,11,9,1 62,11,9,1 1,12,8,1 22,12,7,1 51,12,19,1 1,13,8,1 25,13,4,1 51,13,18,1 52,14,5,1"],
  ];
  // The page's logo glitch, frozen in one frame (parameter "glitch"; off by default): two or three horizontal
  // slices of the mark knocked sideways, a faint ghost left where each came from.
  const glitchBands = () => {
    const bands = [];
    const n = 2 + Math.floor(Math.random() * 2);
    for (let i = 0; i < n; i++) {
      const y = Math.floor(Math.random() * 13), h = 1 + Math.floor(Math.random() * 2);
      const dx = (Math.random() < 0.5 ? -1 : 1) * (2 + Math.floor(Math.random() * 2));
      bands.push({ y, h, dx });
    }
    return bands;
  };
  const drawMark = (cell, hex, { glow = true, glitch = opts.glitch, ghost = "#ffffff" } = {}) => {
    const W = 72 * cell, H = 15 * cell * 2, pad = cell * 4;
    const ctx = new DrawContext();
    ctx.size = new Size(W + pad * 2, H + pad * 2);
    ctx.opaque = false;
    ctx.respectScreenScale = true;
    const bands = glitch ? glitchBands() : [];
    const shift = (y) => { const b = bands.find((b) => y >= b.y && y < b.y + b.h); return b ? b.dx : 0; };
    const cells = [];
    for (const [op, list] of MARK) for (const r of list.split(" ")) { const [x, y, w, h] = r.split(",").map(Number); cells.push({ op, x, y, w, h }); }
    if (glow) {
      ctx.setFillColor(new Color(hex, 0.08));
      for (const c of cells) if (c.op >= 1) ctx.fillEllipse(new Rect(pad + (c.x + shift(c.y)) * cell - cell * 2, pad + c.y * cell * 2 - cell * 2, c.w * cell + cell * 4, c.h * cell * 2 + cell * 4));
    }
    for (const c of cells) {
      const dx = shift(c.y);
      if (dx) {
        ctx.setFillColor(new Color(ghost, c.op * 0.3));
        ctx.fillRect(new Rect(pad + c.x * cell, pad + c.y * cell * 2, c.w * cell, c.h * cell * 2));
      }
      ctx.setFillColor(new Color(hex, c.op));
      ctx.fillRect(new Rect(pad + (c.x + dx) * cell, pad + c.y * cell * 2, c.w * cell, c.h * cell * 2));
    }
    return ctx.getImage();
  };
  const roundedPath = (x, y, w, h, r) => { const p = new Path(); p.addRoundedRect(new Rect(x, y, w, h), r, r); return p; };
  // The line: the last fortnight's new listings, oldest left, over a dashed baseline at the typical day (the
  // fortnight's 80th percentile). Up to that line the scale is linear and takes 70% of the height; above it,
  // logarithmic to the fortnight's peak, so one huge onboarding day is a tall point rather than a flat top.
  const drawLine = (width, height, hex, { baseline = true, area = true, lineWidth = 2 } = {}) => {
    const ctx = new DrawContext();
    ctx.size = new Size(width, height);
    ctx.opaque = false;
    ctx.respectScreenScale = true;
    const n = days.length, pad = 4, peak = Math.max(ref * 1.01, ...days);
    const v = (d) => d <= ref ? d / ref * 0.7 : 0.7 + 0.3 * Math.log(d / ref) / Math.log(peak / ref);
    const y = (d) => height - pad - v(d) * (height - pad * 2);
    const x = (i) => pad + (n > 1 ? i * (width - pad * 2) / (n - 1) : (width - pad * 2) / 2);
    if (baseline) {
      ctx.setFillColor(new Color("#8e8e93", 0.7));
      for (let bx = pad; bx < width - pad; bx += 6) ctx.fillRect(new Rect(bx, y(ref) - 0.5, 3, 1));
    }
    const pts = days.map((d, i) => new Point(x(i), y(d)));
    if (area && n > 1) {
      const a = new Path();
      a.move(new Point(pts[0].x, height));
      for (const p of pts) a.addLine(p);
      a.addLine(new Point(pts[n - 1].x, height));
      a.closeSubpath();
      ctx.addPath(a);
      ctx.setFillColor(new Color(hex, 0.16));
      ctx.fillPath();
    }
    if (n > 1) {
      const l = new Path();
      l.move(pts[0]);
      for (const p of pts.slice(1)) l.addLine(p);
      ctx.addPath(l);
      ctx.setStrokeColor(new Color(hex));
      ctx.setLineWidth(lineWidth);
      ctx.strokePath();
    }
    const last = pts[n - 1], r = lineWidth * 1.5;
    ctx.setFillColor(new Color(hex));
    ctx.fillEllipse(new Rect(last.x - r, last.y - r, r * 2, r * 2));
    return ctx.getImage();
  };
  // A company tile: a rounded square with the company's icon (or its initial), and today's new count in purple at
  // the top right when there is one. Drawn as one image so the count can overlap the tile's corner.
  const drawTile = (size, icon, c, tileColor) => {
    const badge = c.new_today ? `+${c.new_today}` : "";
    const over = badge ? 10 : 0, up = badge ? 7 : 0;
    const ctx = new DrawContext();
    ctx.size = new Size(size + over, size + up);
    ctx.opaque = false;
    ctx.respectScreenScale = true;
    ctx.addPath(roundedPath(0, up, size, size, size * 0.3));
    ctx.setFillColor(tileColor);
    ctx.fillPath();
    const f = Math.round(size * 0.5);
    if (icon) ctx.drawImageInRect(icon, new Rect((size - f) / 2, up + (size - f) / 2, f, f));
    else {
      ctx.setFont(Font.boldSystemFont(Math.round(size * 0.46)));
      ctx.setTextColor(C.text);
      ctx.setTextAlignedCenter();
      ctx.drawTextInRect(c.name.slice(0, 1).toUpperCase(), new Rect(0, up + size * 0.2, size, size * 0.7));
    }
    if (badge) {
      const fs = Math.max(10, Math.round(size * 0.46));
      ctx.setFont(Font.heavySystemFont(fs));
      ctx.setTextColor(new Color("#000000", 0.6));
      ctx.setTextAlignedRight();
      ctx.drawTextInRect(badge, new Rect(0, 1, size + over, fs + 4));
      ctx.setTextColor(C.purple);
      ctx.drawTextInRect(badge, new Rect(0, 0, size + over, fs + 4));
    }
    return ctx.getImage();
  };

  // ---- the background: the frosted wallpaper crop, dark glass, or black
  // Where the widget rows and columns sit on each screen, in pixels, keyed by the screen's height in pixels
  // (small/medium/large = widget sides; left/right/top/middle/bottom = widget origins). "text" is the usual Home
  // Screen with app labels, "notext" the large-icon layout without them.
  const GEOMETRY = {
    2868: { text: { small: 510, medium: 1092, large: 1146, left: 114, right: 696, top: 276, middle: 912, bottom: 1548 },
            notext: { small: 530, medium: 1138, large: 1136, left: 91, right: 699, top: 276, middle: 882, bottom: 1488 } },
    2796: { text: { small: 510, medium: 1092, large: 1146, left: 98, right: 681, top: 252, middle: 888, bottom: 1524 },
            notext: { small: 530, medium: 1139, large: 1136, left: 75, right: 684, top: 252, middle: 858, bottom: 1464 } },
    2622: { text: { small: 486, medium: 1032, large: 1098, left: 87, right: 633, top: 261, middle: 872, bottom: 1485 },
            notext: { small: 495, medium: 1037, large: 1035, left: 84, right: 626, top: 270, middle: 810, bottom: 1350 } },
    2556: { text: { small: 474, medium: 1017, large: 1062, left: 81, right: 624, top: 240, middle: 828, bottom: 1416 },
            notext: { small: 495, medium: 1047, large: 1047, left: 66, right: 618, top: 243, middle: 795, bottom: 1347 } },
  };
  const wallPath = fm.joinPath(docs, "k9-wallpaper.jpg");
  const family = config.widgetFamily || "medium";
  const homeFamily = family === "small" || family === "medium" || family === "large";
  if (config.runsInApp && homeFamily && (opts.photo || !fm.fileExists(wallPath))) {
    // First run in the app: offer the wallpaper screenshot for the frosted background.
    const a = new Alert();
    a.title = "K9 · frosted background";
    a.message = "The widget shows the piece of your wallpaper behind it, frosted. Take a screenshot of an empty Home Screen page (edit mode, swipe to the last page), then choose it here. Skip for a dark glass card.";
    a.addAction("Choose screenshot");
    a.addCancelAction("Skip");
    if ((await a.presentAlert()) === 0) {
      try {
        const img = await Photos.fromLibrary();
        if (img && GEOMETRY[Math.round(img.size.height)]) fm.writeImage(wallPath, img);
        else { const b = new Alert(); b.message = "That is not a screenshot of this phone's Home Screen (unexpected size). Dark glass for now."; b.addAction("OK"); await b.presentAlert(); }
      } catch (e) { /* cancelled the picker */ }
    }
  }
  const frosted = (img) => {
    // Crop the wallpaper behind the widget, blur it by shrinking and re-enlarging, darken it, add a light rim.
    const geo = GEOMETRY[Math.round(img.size.height)];
    if (!geo) return null;
    const g = geo[opts.nolabels ? "notext" : "text"];
    const [row, col] = opts.bg.split("-");
    const wpx = family === "small" ? g.small : g.medium, hpx = family === "large" ? g.large : g.small;
    const xpx = family === "small" ? g[col || "left"] : g.left, ypx = g[row] === undefined ? g.top : g[row];
    const crop = new DrawContext();
    crop.size = new Size(wpx, hpx);
    crop.opaque = false;
    crop.respectScreenScale = false;
    crop.drawImageInRect(img, new Rect(-xpx, -ypx, img.size.width, img.size.height));
    let step = crop.getImage();
    for (const k of [0.25, 0.25]) {                                                  // down to 1/16 of the size
      const sw = Math.max(2, Math.round(step.size.width * k)), sh = Math.max(2, Math.round(step.size.height * k));
      const d = new DrawContext(); d.size = new Size(sw, sh); d.opaque = false; d.respectScreenScale = false;
      d.drawImageInRect(step, new Rect(0, 0, sw, sh)); step = d.getImage();
    }
    const out = new DrawContext();
    out.size = new Size(wpx, hpx);
    out.opaque = false;
    out.respectScreenScale = false;
    out.drawImageInRect(step, new Rect(0, 0, wpx, hpx));                             // back up: a soft blur
    out.setFillColor(new Color("#0e0e12", 0.55));
    out.fillRect(new Rect(0, 0, wpx, hpx));
    out.addPath(roundedPath(1, 1, wpx - 2, hpx - 2, 22 * 3));                        // 22 pt corners at 3x
    out.setStrokeColor(new Color("#ffffff", 0.14));
    out.setLineWidth(2);
    out.strokePath();
    return out.getImage();
  };

  // ---- building blocks
  const w = new ListWidget();
  w.url = base;
  w.refreshAfterDate = new Date(Date.now() + 20 * 60 * 1000);
  let glassy = false;
  if (homeFamily) {
    let bg = null;
    if (!opts.black && !opts.glass && fm.fileExists(wallPath)) {
      try { bg = frosted(fm.readImage(wallPath)); } catch (e) { bg = null; }
    }
    if (bg) { w.backgroundImage = bg; glassy = true; }
    else if (!opts.black) {
      const grad = new LinearGradient();
      grad.colors = [new Color("#2b2b33"), new Color("#17171c"), new Color("#0e0e12")];
      grad.locations = [0, 0.45, 1];
      grad.startPoint = new Point(0, 0);
      grad.endPoint = new Point(1, 1);
      w.backgroundGradient = grad;
      glassy = true;
    } else w.backgroundColor = C.bg;
  }
  const tileColor = glassy ? new Color("#ffffff", 0.12) : new Color("#1c1c1e");
  const lineColor = glassy ? new Color("#ffffff", 0.14) : new Color("#2a2a2d");
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
  // The live parts: WidgetKit date text. "… ago" counts the feed's age up by the minute; the countdown ticks.
  const ago = (into, font, color) => {
    if (!checkedDate) return text(into, "—", font, color);
    const d = into.addDate(checkedDate);
    d.applyRelativeStyle();
    d.font = font;
    d.textColor = color;
    d.lineLimit = 1;
    d.minimumScaleFactor = 0.5;
    return d;
  };
  const countdown = (into, font, color) => {
    const d = into.addDate(nextSlot);
    d.applyTimerStyle();
    d.font = font;
    d.textColor = color;
    d.lineLimit = 1;
    return d;
  };
  // The header: the K9 tile, the ledger's name, and "updated … ago" on the right.
  const header = (into, { tile = 30, title = "Bengaluru openings", sub = "K9 · LEDGER", size = 14 } = {}) => {
    const h = into.addStack();
    h.layoutHorizontally();
    h.topAlignContent();
    h.spacing = 8;
    if (tile) {
      const t = h.addStack();
      t.size = new Size(tile + 14, tile + 14);
      t.cornerRadius = 12;
      t.backgroundColor = tileColor;
      t.centerAlignContent();
      t.addSpacer();
      image(t, drawMark(2, PURPLE, { glow: false }), tile, Math.round(tile * 30 / 72));
      t.addSpacer();
    }
    const left = h.addStack();
    left.layoutVertically();
    left.spacing = 1;
    text(left, stale ? `${sub} · ${stale.toUpperCase()}` : sub, Font.semiboldSystemFont(Math.round(size * 0.75)), stale ? C.amber : C.dim);
    text(left, title, Font.boldSystemFont(size), C.text).minimumScaleFactor = 0.8;
    h.addSpacer();
    const right = h.addStack();
    right.layoutVertically();
    right.spacing = 1;
    const u = right.addStack(); u.layoutHorizontally(); u.addSpacer();
    text(u, "updated", Font.semiboldSystemFont(Math.round(size * 0.75)), C.dim);
    const r = right.addStack(); r.layoutHorizontally(); r.centerAlignContent(); r.spacing = 3; r.addSpacer();
    ago(r, Font.boldSystemFont(Math.round(size * 0.93)), C.text);
    text(r, "ago", Font.mediumSystemFont(Math.round(size * 0.93)), C.dim);
  };
  // The price block: the count, and under it "+N" and the ↗ percent pill spread to the count's width.
  const price = (into, size, width) => {
    const col = into.addStack();
    col.layoutVertically();
    col.spacing = 5;
    if (width) col.size = new Size(width, 0);
    text(col, feed ? fmtNum(total) : "—", Font.heavySystemFont(size), C.text).minimumScaleFactor = 0.6;
    const r = col.addStack();
    r.layoutHorizontally();
    r.centerAlignContent();
    text(r, `+${newToday}`, Font.boldSystemFont(Math.round(size * 0.36)), C.green);
    r.addSpacer();
    const p = r.addStack();
    p.layoutHorizontally();
    p.centerAlignContent();
    p.spacing = 3;
    p.setPadding(3, 7, 3, 7);
    p.cornerRadius = 9;
    p.backgroundColor = new Color(GREEN, 0.18);
    const sym = SFSymbol.named("arrow.up.right");
    sym.applyFont(Font.boldSystemFont(9));
    const si = p.addImage(sym.image);
    si.imageSize = new Size(9, 9);
    si.tintColor = C.green;
    text(p, `${pct}%`, Font.boldSystemFont(Math.round(size * 0.3)), C.green);
    return col;
  };
  // The company row: up to `max` tiles, companies with news first, then "···" and the company count in purple.
  const companyRow = async (into, size, max) => {
    const r = into.addStack();
    r.layoutHorizontally();
    r.bottomAlignContent();
    r.spacing = Math.round(size * 0.36);
    const list = feed.companies.slice(0, max);
    for (const c of list) {
      const icon = await iconFor(c);
      const img = drawTile(size, icon, c, tileColor);
      const s = r.addStack();
      s.url = base;
      image(s, img, size + (c.new_today ? 10 : 0), size + (c.new_today ? 7 : 0));
    }
    if (feed.companies.length > list.length) text(r, "···", Font.heavySystemFont(Math.round(size * 0.7)), C.purple).textOpacity = 0.7;
    r.addSpacer();
    text(r, `${feed.company_count} companies`, Font.boldSystemFont(10.5), C.purple);
  };
  const rule = (into) => {
    const l = into.addStack();
    l.size = new Size(0, 1);
    l.backgroundColor = lineColor;
  };
  const row = (into, j) => {
    const r = into.addStack();
    r.layoutHorizontally();
    r.centerAlignContent();
    r.spacing = 8;
    r.url = j.url;
    text(r, j.company, Font.boldSystemFont(12), C.text);
    text(r, j.title, Font.systemFont(12), C.dim).minimumScaleFactor = 0.85;
    r.addSpacer();
    const k = closeIn(j.closing);
    if (k !== null && k >= 0 && k <= 7) text(r, k === 0 ? "closes today" : `closes ${fmtDay(j.closing)}`, Font.semiboldSystemFont(11), C.red);
    else if (dayNum(j.first_seen) >= today) text(r, "new", Font.semiboldSystemFont(11), C.green);
    else if (k !== null) text(r, `closes ${fmtDay(j.closing)}`, Font.mediumSystemFont(11), C.dim);
    else text(r, fmtDay(j.posted), Font.mediumSystemFont(11), C.dimmer);
  };
  const empty = (into) => {
    into.addSpacer();
    text(into, "No feed yet", Font.semiboldSystemFont(13), C.text);
    text(into, "Open the ledger once on Wi-Fi, then come back.", Font.systemFont(11), C.dim, 2);
    into.addSpacer();
  };

  // ---- layouts
  if (family === "accessoryCircular") {
    // The round Lock Screen widget is the K9 mark itself (the Lock Screen renders every widget in its own tint, so
    // the mark is drawn white); the counts live in the inline and rectangular shapes beside it.
    w.addAccessoryWidgetBackground = true;
    w.setPadding(0, 0, 0, 0);
    const s = w.addStack();
    s.layoutVertically();
    s.addSpacer();
    const m = s.addStack();
    m.layoutHorizontally();
    m.addSpacer();
    image(m, drawMark(2, "#ffffff", { glow: false }), 60, 28);
    m.addSpacer();
    s.addSpacer();
  } else if (family === "accessoryInline") {
    text(w, feed ? `K9 ${fmtNum(total)} · +${newToday} · next ${nextLabel}` : "K9 · offline", Font.semiboldSystemFont(12), Color.white());
  } else if (family === "accessoryRectangular") {
    w.addAccessoryWidgetBackground = true;
    w.setPadding(5, 8, 5, 8);
    if (!feed) text(w, "K9 · offline", Font.semiboldSystemFont(12), Color.white());
    else {
      const h = w.addStack();
      h.layoutHorizontally();
      h.centerAlignContent();
      h.spacing = 5;
      text(h, fmtNum(total), Font.heavySystemFont(16), Color.white());
      text(h, `+${newToday}`, Font.boldSystemFont(12), Color.white()).textOpacity = 0.8;
      h.addSpacer();
      countdown(h, Font.boldMonospacedSystemFont(11), Color.white());
      const l = w.addStack();
      l.layoutHorizontally();
      text(l, "Bengaluru openings", Font.semiboldSystemFont(9.5), Color.white()).textOpacity = 0.7;
      l.addSpacer();
      text(l, `next ${nextLabel}`, Font.semiboldSystemFont(9.5), Color.white()).textOpacity = 0.7;
      w.addSpacer();
      image(w, drawLine(142, 18, "#ffffff", { baseline: false, area: false, lineWidth: 1.5 }), 142, 18);
    }
  } else if (family === "small") {
    w.setPadding(12, 12, 12, 12);
    if (!feed) { header(w, { tile: 0, title: "Openings", sub: "K9", size: 13 }); empty(w); }
    else {
      header(w, { tile: 0, title: "Openings", sub: "K9", size: 13 });
      w.addSpacer(6);
      price(w, 28, 0);
      w.addSpacer();
      image(w, drawLine(146, 38, GREEN), 146, 38);
      w.addSpacer(8);
      await companyRow(w, 22, 4);
    }
  } else if (family === "medium") {
    w.setPadding(14, 14, 14, 14);
    if (!feed) { header(w, { tile: 24 }); empty(w); }
    else {
      header(w, { tile: 24 });
      w.addSpacer(6);
      const mid = w.addStack();
      mid.layoutHorizontally();
      mid.bottomAlignContent();
      mid.spacing = 12;
      price(mid, 38, 106);
      image(mid, drawLine(218, 60, GREEN), 218, 60);
      w.addSpacer();
      await companyRow(w, 22, 6);
    }
  } else {
    w.setPadding(14, 14, 14, 14);
    if (!feed) { header(w, { tile: 30 }); empty(w); }
    else {
      header(w, { tile: 30 });
      w.addSpacer(16);
      const mid = w.addStack();
      mid.layoutHorizontally();
      mid.bottomAlignContent();
      mid.spacing = 12;
      price(mid, 44, 124);
      image(mid, drawLine(200, 96, GREEN), 200, 96);
      const ax = w.addStack();
      ax.layoutHorizontally();
      ax.addSpacer();
      const axis = ax.addStack();
      axis.layoutHorizontally();
      axis.size = new Size(200, 0);
      text(axis, "−13D", Font.boldSystemFont(9), C.dim);
      axis.addSpacer();
      text(axis, `TYPICAL ${ref} · · · ·`, Font.boldSystemFont(9), C.dim);
      axis.addSpacer();
      text(axis, "TODAY", Font.boldSystemFont(9), C.dim);
      w.addSpacer(14);
      await companyRow(w, 30, 6);
      w.addSpacer(12);
      rule(w);
      w.addSpacer(10);
      const head = w.addStack();
      head.layoutHorizontally();
      text(head, opts.closing ? "CLOSING SOONEST" : opts.onlyNew ? "NEW TODAY" : opts.company ? parameter.toUpperCase() : "LATEST", Font.boldSystemFont(10), C.dim);
      head.addSpacer();
      if (feed.closing_soon_count) text(head, `${feed.closing_soon_count} CLOSING ≤7D`, Font.boldSystemFont(10), C.green);
      w.addSpacer(8);
      const shown = jobs.slice(0, opts.rows || 4);
      if (!shown.length) text(w, opts.company ? `No openings for "${parameter}" right now` : "Nothing matches the widget parameter", Font.systemFont(11), C.dim, 2);
      const list = w.addStack();
      list.layoutVertically();
      list.spacing = 7;
      for (const j of shown) row(list, j);
      w.addSpacer();
    }
  }

  if (config.runsInWidget) Script.setWidget(w);
  else if (family === "small") await w.presentSmall();
  else if (family === "large") await w.presentLarge();
  else await w.presentMedium();
  Script.complete();
};
