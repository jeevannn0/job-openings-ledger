#!/usr/bin/env node
// Keeps the ledger's Live Activity current: the card on an iPhone's Lock Screen and in its Dynamic Island that
// lists the openings the latest refreshes found. Node's standard library only.
//
// A Home Screen web app cannot start a Live Activity itself: ActivityKit is for installed apps (Apple Developer
// Forums thread 742737, still true in iOS 27), and Web Push can only show notifications. So the card is drawn by
// PushWard, an App Store app whose REST API turns one HTTP request into a Live Activity through Apple's push
// service. The reader installs the app once and gives this workflow an integration key (the PUSHWARD_KEY secret);
// without the key this script says so and exits 0, so the ledger works exactly the same for anyone who does not
// want the card. The key is never printed.
//
// One activity (slug k9-openings) in PushWard's "log" template: newest lines first, one line per new opening,
// each stamped with its refresh time; a line turns amber when the posting closes within the week. A refresh that
// found openings starts the card, or prepends to it while it is still running; a refresh that found nothing
// leaves a fresh card alone and ends a card whose newest line is six hours old, so the card is always "what came
// in during the last few hours". The card never makes a sound: the Web Push message (push.mjs) is the alert,
// the card is the glance. Tapping the card opens the ledger; its two buttons open the ledger and the newest posting.
//
//   node scripts/island.mjs --alert-env ALERT --ledger-url https://example.github.io/ledger/
//   node scripts/island.mjs --alert-file alerts.json --ledger-url https://example.github.io/ledger/ --dry-run
//
// Environment: PUSHWARD_KEY, an hlk_ integration key (activities at manage) created in the PushWard app.

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

export const API = "https://api.pushward.app";
export const SLUG = "k9-openings";
export const NAME = "K9 · new openings";
export const PRIORITY = 5;                          // ahead of lower-priority cards on the Lock Screen and in the island
export const LINES_MAX = 16;                        // two refreshes' worth of the alert's eight; well under the 4 KB push
export const TEXT_MAX = 96;
export const KEEP_S = 6 * 3600;                     // a card ends at the first refresh with nothing new after this
export const STALE_TTL_S = 7 * 3600;                // server backstop if the workflow stops running: PushWard ends it
const RETRY_MS = [2000, 6000];                      // after the first attempt, for rate limits, 5xx and network errors
const CHECKED_AT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})\+05:30$/;   // the alert's IST stamp, to the minute

// ---------------------------------------------------------------------- the alert payload

/** One line of text for the card: no control characters, single spaces, at most TEXT_MAX characters. */
export function clean(text, max = TEXT_MAX) {
  const s = String(text).replace(/[\x00-\x1f\x7f]+/g, " ").replace(/\s+/g, " ").trim();
  return s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s;
}

/** The run this script describes, read from the alert payload (alerts.json); shapes are checked, nothing trusted. */
export function parseAlert(alert) {
  if (!alert || typeof alert !== "object") throw new Error("alert: not an object");
  const n = alert.new_count;
  if (!Number.isInteger(n) || n < 0) throw new Error("alert: new_count");
  const m = typeof alert.checked_at === "string" && CHECKED_AT.exec(alert.checked_at);
  if (!m) throw new Error("alert: checked_at");
  const [, y, mo, d, h, mi] = m.map(Number);
  const at = Math.floor(Date.UTC(y, mo - 1, d, h, mi) / 1000) - 330 * 60;        // IST is UTC+05:30, no DST
  if (!Array.isArray(alert.new_jobs) || !Array.isArray(alert.closing_soon)) throw new Error("alert: job lists");
  const closing = new Set(alert.closing_soon.map((j) => j && `${j.company}|${j.job_id}`));
  const newJobs = alert.new_jobs.map((j, i) => {
    if (!j || typeof j !== "object" || typeof j.company !== "string" || typeof j.title !== "string" || typeof j.job_id !== "string") throw new Error(`alert: new_jobs #${i + 1}`);
    let url = null;
    if (typeof j.canonical_url === "string") {
      try {
        const u = new URL(j.canonical_url);
        if (u.protocol === "https:" && !u.username && !u.password) url = u.href;
      } catch { /* the line still lists the opening; only the button needs a URL */ }
    }
    return { company: clean(j.company, 40), title: clean(j.title, TEXT_MAX), url, closing: closing.has(`${j.company}|${j.job_id}`) };
  });
  const remaining = alert.remaining_new_count;
  if (!Number.isInteger(remaining) || remaining < 0 || newJobs.length + remaining !== n) throw new Error("alert: new_count does not add up");
  return { newCount: n, newJobs, remaining, at, hhmm: `${m[4]}:${m[5]}` };
}

// ---------------------------------------------------------------------- the card

/** The lines this run adds: one per new opening (amber when it closes within the week), then "+N more". */
export function linesFor(run) {
  const lines = run.newJobs.map((j) => ({ text: clean(`${j.company} – ${j.title}`), at: run.at, level: j.closing ? "warn" : "info" }));
  if (run.remaining > 0) lines.push({ text: `+${run.remaining} more new opening${run.remaining === 1 ? "" : "s"} · open the ledger`, at: run.at, level: "info" });
  return lines;
}

/** This run's lines ahead of the card's existing ones; a re-run of the same refresh replaces its own lines. */
export function mergeLines(fresh, existing, at) {
  const kept = Array.isArray(existing)
    ? existing.filter((l) => l && typeof l === "object" && typeof l.text === "string" && Number.isInteger(l.at) && l.at !== at)
               .map((l) => ({ text: clean(l.text), at: l.at, level: ["info", "warn", "error"].includes(l.level) ? l.level : "info" }))
    : [];
  return fresh.concat(kept).slice(0, LINES_MAX);
}

/** The island's leading ear: this refresh's count, at most four characters. */
export function compactLabel(n) {
  return n > 999 ? "999+" : `+${n}`;
}

/** The content of the running card for this run. */
export function contentFor(run, lines, ledgerUrl) {
  const content = {
    template: "log",
    state: `+${run.newCount} new · ${run.hhmm} IST`,
    icon: "pawprint.fill",
    accent_color: "purple",
    compact_label: compactLabel(run.newCount),
    lines,
    tap_action: { url: ledgerUrl, foreground: true },
    url_action: { url: ledgerUrl, foreground: true, title: "Open ledger", icon: "list.bullet.rectangle" },
  };
  const top = run.newJobs.find((j) => j.url);
  if (top) content.secondary_url_action = { url: top.url, foreground: true, title: clean(top.company, 24), icon: "arrow.up.right.square" };
  return content;
}

/** The newest line's time on the card PushWard holds, or when it was last written; null when unknown. */
export function newestAt(current) {
  const lines = current && current.content && Array.isArray(current.content.lines) ? current.content.lines : [];
  const stamps = lines.map((l) => l && l.at).filter(Number.isInteger);
  if (stamps.length) return Math.max(...stamps);
  const t = current && typeof current.updated_at === "string" ? Date.parse(current.updated_at) : NaN;
  return Number.isNaN(t) ? null : Math.floor(t / 1000);
}

/** What this run does to the card: start, update, end, or nothing (with the reason, for the log). */
export function decide(current, run) {
  const running = !!current && current.state === "ongoing";
  if (run.newCount > 0) return { kind: running ? "update" : "start", reason: `+${run.newCount} new` };
  if (!running) return { kind: "none", reason: "nothing new and no card showing" };
  const newest = newestAt(current);
  if (newest !== null && run.at - newest < KEEP_S) return { kind: "none", reason: `nothing new; the card from ${new Date(newest * 1000).toISOString().slice(11, 16)} UTC stays up` };
  return { kind: "end", reason: "nothing new for six hours" };
}

// ---------------------------------------------------------------------- the API

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** One request to PushWard with retries on rate limits, 5xx and network errors; returns {status, body, code}. */
export async function request(key, method, path, body, fetchImpl = fetch, sleepImpl = sleep) {
  const headers = { Authorization: `Bearer ${key}`, Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = method === "PATCH" ? "application/merge-patch+json" : "application/json";
  for (let attempt = 0; ; attempt++) {
    let response;
    try {
      response = await fetchImpl(`${API}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (err) {
      if (attempt < RETRY_MS.length) { await sleepImpl(RETRY_MS[attempt]); continue; }
      return { status: 0, body: null, code: err instanceof Error ? err.name : "error" };
    }
    let parsed = null;
    try {
      const text = await response.text();
      parsed = text ? JSON.parse(text) : null;
    } catch { /* a body that is not JSON is reported by its status alone */ }
    const code = parsed && typeof parsed.code === "string" ? parsed.code : "";
    const transient = response.status >= 500 || (response.status === 429 && code !== "quota.exceeded");
    if (transient && attempt < RETRY_MS.length) { await sleepImpl(RETRY_MS[attempt]); continue; }
    return { status: response.status, body: parsed, code };
  }
}

// ---------------------------------------------------------------------- CLI

function parseArgs(argv) {
  const out = { alertEnv: "", alertFile: "", ledgerUrl: "", dryRun: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--dry-run") out.dryRun = true;
    else if (a === "--alert-env") out.alertEnv = argv[++i] || "";
    else if (a === "--alert-file") out.alertFile = argv[++i] || "";
    else if (a === "--ledger-url") out.ledgerUrl = argv[++i] || "";
    else throw new Error(`unknown argument ${a}`);
  }
  if (!/^https:\/\/[a-z0-9-]+\.github\.io\/[A-Za-z0-9._-]+\/$/.test(out.ledgerUrl)) throw new Error("--ledger-url must be the site's GitHub Pages URL");
  if (!out.alertEnv && !out.alertFile) throw new Error("need --alert-env NAME or --alert-file PATH");
  return out;
}

export async function main(argv, env = process.env, log = console.log, fetchImpl = fetch, sleepImpl = sleep) {
  const args = parseArgs(argv);
  const text = args.alertFile ? readFileSync(args.alertFile, "utf8") : env[args.alertEnv];
  if (!text) throw new Error("alert payload is empty");
  const run = parseAlert(JSON.parse(text));
  const key = env.PUSHWARD_KEY;
  if (!key) {
    log("island: no PushWard key (PUSHWARD_KEY secret); the Live Activity is off, nothing to do");
    return 0;
  }
  if (!/^hlk_[A-Za-z0-9_-]{16,}$/.test(key)) throw new Error("PUSHWARD_KEY is not a PushWard integration key");
  // What the card shows now. 404 = no card has ever been created under this key.
  const got = await request(key, "GET", `/activities/${SLUG}`, undefined, fetchImpl, sleepImpl);
  if (got.status === 401 || got.status === 403) throw new Error(`PushWard rejected the key (HTTP ${got.status}${got.code ? `, ${got.code}` : ""})`);
  if (got.status !== 200 && got.status !== 404) return fail(log, "reading the card", got);
  const current = got.status === 200 ? got.body : null;
  const action = decide(current, run);
  log(`island: ${action.reason}; ${action.kind === "none" ? "no change" : `${action.kind} the card`}`);
  if (action.kind === "none") return 0;
  if (args.dryRun) {
    log("island: dry run; nothing sent");
    return 0;
  }
  let patch;
  if (action.kind === "end") {
    patch = { state: "ended", dismissal_ttl: 0 };
  } else {
    if (action.kind === "start") {
      // Creating (or re-creating) the activity is idempotent and costs no quota: it sets the heading, the order
      // on the Lock Screen and the server-side backstop, then the PATCH below is the push-to-start.
      const made = await request(key, "POST", "/activities", { slug: SLUG, name: NAME, priority: PRIORITY, stale_ttl: STALE_TTL_S, dismissal_ttl: 0 }, fetchImpl, sleepImpl);
      if (made.status !== 201 && made.status !== 200) return fail(log, "creating the card", made);
    }
    const lines = mergeLines(linesFor(run), current && current.content && current.content.lines, run.at);
    patch = { state: "ongoing", priority: PRIORITY, content: contentFor(run, lines, args.ledgerUrl) };
  }
  const put = await request(key, "PATCH", `/activities/${SLUG}`, patch, fetchImpl, sleepImpl);
  if (put.status === 200 || put.status === 201) {
    const lines = patch.content ? ` (${patch.content.lines.length} line${patch.content.lines.length === 1 ? "" : "s"})` : "";
    log(`island: Live Activity ${action.kind === "end" ? "ended" : action.kind === "start" ? "started" : "updated"}${lines}`);
    return 0;
  }
  return fail(log, `${action.kind === "end" ? "ending" : "updating"} the card`, put);
}

/** Reports a failed request; exhausting the free monthly quota is expected and not a failure of the run. */
function fail(log, what, result) {
  if (result.code === "quota.exceeded") {
    const b = result.body || {};
    log(`island: PushWard's free quota for the month is used up (${b.used}/${b.limit} ${b.kind}, resets ${b.reset_at}); the card waits for next month`);
    return 0;
  }
  if (result.status === 0) throw new Error(`PushWard unavailable while ${what} (${result.code})`);
  throw new Error(`PushWard answered HTTP ${result.status}${result.code ? ` ${result.code}` : ""} while ${what}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).then((code) => process.exit(code), (err) => {
    console.error(`island: ${err instanceof Error ? err.message : err}`);
    process.exit(1);
  });
}
