// node --test scripts/island.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { API, KEEP_S, LINES_MAX, NAME, PRIORITY, SLUG, STALE_TTL_S, TEXT_MAX, clean, compactLabel, contentFor, decide, linesFor,
         main, mergeLines, newestAt, parseAlert, request } from "./island.mjs";

const LEDGER = "https://example.github.io/ledger/";
const KEY = "hlk_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345";
const AT = Math.floor(Date.UTC(2026, 10, 7, 6, 30) / 1000);          // 7 Nov 2026 12:00 IST

const alertWith = (extra = {}) => ({
  schema: 1, refresh_id: "x", checked_at: "2026-11-07T12:00+05:30", as_of: "2026-11-07", total_jobs: 1011, company_count: 19,
  new_count: 3,
  new_jobs: [
    { company: "JPMorganChase", job_id: "210713605", title: "GPB Advisor Support - Analyst", canonical_url: "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210713605" },
    { company: "Siemens", job_id: "527740", title: "Process Expert\u2013 Marcom,\tSales   and Delivery Services\n", canonical_url: "http://jobs.siemens.com/x" },
    { company: "EY", job_id: "1660001", title: "Associate Analyst - Data", canonical_url: "https://careers.ey.com/ey/job/India/Associate/1660001" },
  ],
  remaining_new_count: 0, removed_count: 0, closing_soon_count: 1,
  closing_soon: [{ company: "JPMorganChase", job_id: "210713605", title: "GPB Advisor Support - Analyst", closing_date: "2026-11-09", canonical_url: "https://x.example/" }],
  notification_title: "3 new job openings", notification_body: "…", ledger_url: LEDGER, source_status: {},
  ...extra,
});

/** A fake PushWard: answers from a script of [status, body] per request, records every call. */
function fakeApi(script) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    const step = script.shift();
    if (!step) throw new Error(`unexpected request ${init.method} ${url}`);
    calls.push({ method: init.method, url, headers: init.headers, body: init.body === undefined ? undefined : JSON.parse(init.body) });
    if (step[0] === "network") throw new TypeError("fetch failed");
    return { status: step[0], text: async () => (step[1] === undefined ? "" : JSON.stringify(step[1])) };
  };
  return { calls, fetchImpl };
}

const run = (argv, env, api) => {
  const logs = [];
  return main(argv, env, (s) => logs.push(s), api.fetchImpl, async () => {}).then((code) => ({ code, logs }), (err) => ({ code: 1, logs, error: err.message }));
};

test("the alert payload becomes a run: IST stamp, cleaned text, closing flag, https-only button URLs", () => {
  const r = parseAlert(alertWith());
  assert.equal(r.newCount, 3);
  assert.equal(r.at, AT);
  assert.equal(r.hhmm, "12:00");
  assert.equal(r.newJobs[1].title, "Process Expert– Marcom, Sales and Delivery Services");
  assert.equal(r.newJobs[1].url, null);                               // http, not https: no button to it
  assert.equal(r.newJobs[0].closing, true);
  assert.equal(r.newJobs[2].closing, false);
  for (const bad of [{ new_count: -1 }, { checked_at: "2026-11-07T12:00:00+05:30" }, { checked_at: "2026-11-07T12:00Z" },
                     { remaining_new_count: 1 }, { new_jobs: [{ company: 1 }], new_count: 1 }, { new_jobs: "x" }]) {
    assert.throws(() => parseAlert(alertWith(bad)), /alert:/, JSON.stringify(bad));
  }
  assert.throws(() => parseAlert(null), /alert:/);
});

test("lines: one per opening, newest first, amber when closing soon, a tail line for the rest", () => {
  const r = parseAlert(alertWith({ new_count: 12, remaining_new_count: 9 }));
  const lines = linesFor(r);
  assert.equal(lines.length, 4);
  assert.deepEqual(lines[0], { text: "JPMorganChase – GPB Advisor Support - Analyst", at: AT, level: "warn" });
  assert.deepEqual(lines[2], { text: "EY – Associate Analyst - Data", at: AT, level: "info" });
  assert.deepEqual(lines[3], { text: "+9 more new openings · open the ledger", at: AT, level: "info" });
  assert.equal(linesFor(parseAlert(alertWith({ new_count: 4, remaining_new_count: 1 })))[3].text, "+1 more new opening · open the ledger");
  // Long titles are cut to the line budget with an ellipsis; control characters never reach the card.
  const long = clean(`x${"y".repeat(200)}`);
  assert.equal(long.length, TEXT_MAX);
  assert.ok(long.endsWith("…"));
  assert.equal(clean("a\u0000b\u001fc  d"), "a b c d");
});

test("merging keeps this run ahead of the card's older lines, replaces a re-sent run, caps the feed", () => {
  const fresh = [{ text: "new", at: 200, level: "info" }];
  const older = Array.from({ length: 20 }, (_, i) => ({ text: `old ${i}`, at: 100 - i, level: i % 2 ? "warn" : "info" }));
  const merged = mergeLines(fresh, [{ text: "same run", at: 200, level: "info" }, { text: "junk" }, null, ...older], 200);
  assert.equal(merged.length, LINES_MAX);
  assert.deepEqual(merged[0], fresh[0]);
  assert.deepEqual(merged[1], { text: "old 0", at: 100, level: "info" });
  assert.ok(!merged.some((l) => l.text === "same run" || l.text === "junk"));
  assert.deepEqual(mergeLines(fresh, undefined, 200), fresh);
  assert.deepEqual(mergeLines(fresh, [{ text: "x", at: 1, level: "loud" }], 200)[1].level, "info");
});

test("the card: log template, purple paw, count in the island's ear, ledger on tap and the first button, newest posting on the second", () => {
  const r = parseAlert(alertWith());
  const c = contentFor(r, linesFor(r), LEDGER);
  assert.equal(c.template, "log");
  assert.equal(c.state, "+3 new · 12:00 IST");
  assert.equal(c.icon, "pawprint.fill");
  assert.equal(c.accent_color, "purple");
  assert.equal(c.compact_label, "+3");
  assert.deepEqual(c.tap_action, { url: LEDGER, foreground: true });
  assert.deepEqual(c.url_action, { url: LEDGER, foreground: true, title: "Open ledger", icon: "list.bullet.rectangle" });
  assert.deepEqual(c.secondary_url_action, { url: "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210713605", foreground: true, title: "JPMorganChase", icon: "arrow.up.right.square" });
  assert.equal(c.lines.length, 3);
  assert.ok(!("sound" in c));                                           // the push notification is the alert; the card is silent
  for (const [n, label] of [[1, "+1"], [42, "+42"], [999, "+999"], [1000, "999+"]]) assert.equal(compactLabel(n), label);
  // No button to a posting when no new opening has an https URL.
  const none = contentFor(parseAlert(alertWith({ new_count: 1, new_jobs: [{ company: "A", job_id: "1", title: "t" }] })), [], LEDGER);
  assert.ok(!("secondary_url_action" in none));
  assert.ok(JSON.stringify(c).length < 1800);
});

test("what a run does: start, prepend, keep a fresh card, end a six-hour-old one, nothing when there is no card", () => {
  const news = parseAlert(alertWith());
  const quiet = parseAlert(alertWith({ new_count: 0, new_jobs: [], remaining_new_count: 0 }));
  assert.equal(decide(null, news).kind, "start");
  assert.equal(decide({ state: "ended" }, news).kind, "start");
  assert.equal(decide({ state: "preempted" }, news).kind, "start");
  assert.equal(decide({ state: "ongoing" }, news).kind, "update");
  assert.equal(decide(null, quiet).kind, "none");
  assert.equal(decide({ state: "ended", content: { lines: [{ at: AT - 10 }] } }, quiet).kind, "none");
  assert.equal(decide({ state: "ongoing", content: { lines: [{ at: AT - 3 * 3600 }] } }, quiet).kind, "none");
  assert.equal(decide({ state: "ongoing", content: { lines: [{ at: AT - KEEP_S }] } }, quiet).kind, "end");
  assert.equal(decide({ state: "ongoing", content: { lines: [{ at: AT - 3 * 3600 }, { at: AT - 9 * 3600 }] } }, quiet).kind, "none");   // newest line counts
  // Without line stamps the card's own write time decides; without either, end it rather than keep it forever.
  assert.equal(decide({ state: "ongoing", updated_at: new Date((AT - 3600) * 1000).toISOString() }, quiet).kind, "none");
  assert.equal(decide({ state: "ongoing" }, quiet).kind, "end");
  assert.equal(newestAt({ content: { lines: [{ at: 5 }, { at: 9 }, { at: "x" }] } }), 9);
  assert.equal(newestAt(null), null);
});

test("requests carry the key as a bearer token and retry rate limits, 5xx and network errors but not a used-up quota", async () => {
  const api = fakeApi([[429, { code: "rate_limit.exceeded" }], ["network"], [200, { ok: true }]]);
  const r = await request(KEY, "GET", "/activities/x", undefined, api.fetchImpl, async () => {});
  assert.equal(r.status, 200);
  assert.equal(api.calls.length, 3);                                   // two retries, then the answer
  assert.equal(api.calls[0].url, `${API}/activities/x`);
  assert.equal(api.calls[0].headers.Authorization, `Bearer ${KEY}`);
  assert.equal(api.calls[0].body, undefined);
  const quota = fakeApi([[429, { code: "quota.exceeded", used: 251, limit: 250 }]]);
  const q = await request(KEY, "PATCH", "/activities/x", { a: 1 }, quota.fetchImpl, async () => {});
  assert.equal(q.code, "quota.exceeded");
  assert.equal(quota.calls.length, 1);
  assert.equal(quota.calls[0].headers["Content-Type"], "application/merge-patch+json");
  const down = fakeApi([[502], [502], [502]]);
  assert.equal((await request(KEY, "GET", "/x", undefined, down.fetchImpl, async () => {})).status, 502);
  const gone = fakeApi([["network"], ["network"], ["network"]]);
  assert.equal((await request(KEY, "GET", "/x", undefined, gone.fetchImpl, async () => {})).status, 0);
});

test("a run with new openings and no card: create the activity, then push-to-start it", async () => {
  const api = fakeApi([[404, { status: 404 }], [201, { slug: SLUG }], [200, { state: "ongoing" }]]);
  const { code, logs } = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: JSON.stringify(alertWith()), PUSHWARD_KEY: KEY }, api);
  assert.equal(code, 0);
  assert.deepEqual(api.calls.map((c) => [c.method, c.url]), [["GET", `${API}/activities/${SLUG}`], ["POST", `${API}/activities`], ["PATCH", `${API}/activities/${SLUG}`]]);
  assert.deepEqual(api.calls[1].body, { slug: SLUG, name: NAME, priority: PRIORITY, stale_ttl: STALE_TTL_S, dismissal_ttl: 0 });
  const patch = api.calls[2].body;
  assert.equal(patch.state, "ongoing");
  assert.equal(patch.priority, PRIORITY);
  assert.equal(patch.content.template, "log");
  assert.equal(patch.content.lines.length, 3);
  assert.deepEqual(logs, ["island: +3 new; start the card", "island: Live Activity started (3 lines)"]);
  assert.ok(!logs.some((l) => l.includes(KEY)));
  assert.equal(api.calls.every((c) => c.headers.Authorization === `Bearer ${KEY}`), true);
});

test("a run with new openings and a running card prepends to it without re-creating it", async () => {
  const current = { state: "ongoing", content: { template: "log", lines: [{ text: "Older – one", at: AT - 3 * 3600, level: "info" }] } };
  const api = fakeApi([[200, current], [200, { state: "ongoing" }]]);
  const { code, logs } = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: JSON.stringify(alertWith()), PUSHWARD_KEY: KEY }, api);
  assert.equal(code, 0);
  assert.deepEqual(api.calls.map((c) => c.method), ["GET", "PATCH"]);
  const lines = api.calls[1].body.content.lines;
  assert.equal(lines.length, 4);
  assert.equal(lines[3].text, "Older – one");
  assert.equal(logs[1], "island: Live Activity updated (4 lines)");
});

test("a run with nothing new keeps a fresh card, ends an old one, and leaves a missing one alone", async () => {
  const quiet = JSON.stringify(alertWith({ new_count: 0, new_jobs: [], remaining_new_count: 0 }));
  const fresh = fakeApi([[200, { state: "ongoing", content: { lines: [{ text: "x", at: AT - 3 * 3600 }] } }]]);
  let r = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: quiet, PUSHWARD_KEY: KEY }, fresh);
  assert.equal(r.code, 0);
  assert.equal(fresh.calls.length, 1);
  assert.match(r.logs[0], /the card from 03:30 UTC stays up; no change/);
  const old = fakeApi([[200, { state: "ongoing", content: { lines: [{ text: "x", at: AT - KEEP_S - 1 }] } }], [200, { state: "ended" }]]);
  r = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: quiet, PUSHWARD_KEY: KEY }, old);
  assert.equal(r.code, 0);
  assert.deepEqual(old.calls[1].body, { state: "ended", dismissal_ttl: 0 });
  assert.deepEqual(r.logs, ["island: nothing new for six hours; end the card", "island: Live Activity ended"]);
  const none = fakeApi([[404, {}]]);
  r = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: quiet, PUSHWARD_KEY: KEY }, none);
  assert.deepEqual(r.logs, ["island: nothing new and no card showing; no change"]);
});

test("without a key the run says so and does nothing; a malformed key is refused before any request", async () => {
  const api = fakeApi([]);
  const r = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: JSON.stringify(alertWith()) }, api);
  assert.equal(r.code, 0);
  assert.deepEqual(r.logs, ["island: no PushWard key (PUSHWARD_KEY secret); the Live Activity is off, nothing to do"]);
  assert.equal(api.calls.length, 0);
  const bad = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: JSON.stringify(alertWith()), PUSHWARD_KEY: "not-a-key" }, api);
  assert.equal(bad.code, 1);
  assert.match(bad.error, /not a PushWard integration key/);
  assert.equal(api.calls.length, 0);
});

test("a used-up free quota is reported and is not a failure; a rejected key or an outage is", async () => {
  const quota = fakeApi([[404, {}], [201, {}], [429, { code: "quota.exceeded", kind: "live_activity_updates", used: 250, limit: 250, reset_at: "2026-12-01T00:00:00Z" }]]);
  let r = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: JSON.stringify(alertWith()), PUSHWARD_KEY: KEY }, quota);
  assert.equal(r.code, 0);
  assert.match(r.logs[1], /free quota for the month is used up \(250\/250 live_activity_updates, resets 2026-12-01T00:00:00Z\)/);
  const denied = fakeApi([[401, { code: "auth.invalid" }]]);
  r = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: JSON.stringify(alertWith()), PUSHWARD_KEY: KEY }, denied);
  assert.equal(r.code, 1);
  assert.match(r.error, /rejected the key \(HTTP 401, auth.invalid\)/);
  const down = fakeApi([[503], [503], [503]]);
  r = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: JSON.stringify(alertWith()), PUSHWARD_KEY: KEY }, down);
  assert.equal(r.code, 1);
  assert.match(r.error, /HTTP 503 while reading the card/);
  const offline = fakeApi([["network"], ["network"], ["network"]]);
  r = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ALERT: JSON.stringify(alertWith()), PUSHWARD_KEY: KEY }, offline);
  assert.equal(r.code, 1);
  assert.match(r.error, /unavailable while reading the card/);
});

test("a dry run reads the card and stops before writing; the arguments are checked", async () => {
  const api = fakeApi([[404, {}]]);
  const r = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER, "--dry-run"], { ALERT: JSON.stringify(alertWith()), PUSHWARD_KEY: KEY }, api);
  assert.equal(r.code, 0);
  assert.equal(api.calls.length, 1);
  assert.equal(r.logs[1], "island: dry run; nothing sent");
  for (const argv of [["--ledger-url", LEDGER], ["--alert-env", "ALERT", "--ledger-url", "https://evil.example/"], ["--alert-env", "ALERT", "--ledger-url", LEDGER, "--bogus"]]) {
    const bad = await run(argv, { ALERT: "{}", PUSHWARD_KEY: KEY }, fakeApi([]));
    assert.equal(bad.code, 1, argv.join(" "));
  }
  const empty = await run(["--alert-env", "ALERT", "--ledger-url", LEDGER], { PUSHWARD_KEY: KEY }, fakeApi([]));
  assert.match(empty.error, /alert payload is empty/);
});
