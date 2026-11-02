// node --test scripts/push.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createDecipheriv, createECDH, hkdfSync, randomBytes } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { REGISTER_URL, assertPublicPoint, configuredPush, dec, deliver, enc, encrypt, listRegistered, main, notificationFor,
         parseSubscriptions, pruneRegistered, publicKeyFor, vapidAuthorization, verifyVapid } from "./push.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const CONFIG = join(HERE, "..", "config", "public_sources.json");
const LEDGER = "https://example.github.io/ledger/";
const STORE = "https://k9.example.workers.dev/subscriptions";

// RFC 8291 section 5 and appendix A: the worked example, byte for byte.
test("encryption reproduces the RFC 8291 example", () => {
  const plaintext = Buffer.from("When I grow up, I want to be a watermelon");
  const uaPublic = dec("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4");
  const auth = dec("BTBZMqHH6r4Tts7J_aSIgg");
  const asPrivate = dec("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw");
  const salt = dec("DGv6ra1nlYgDCS1FRnbzlw");
  const body = encrypt(plaintext, uaPublic, auth, { asPrivate, salt });
  assert.equal(enc(body),
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    + "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN");
  assert.equal(body.length, 144);     // 86-byte header + 41 bytes + delimiter + 16-byte tag (the RFC's "145" is an erratum)
  assert.equal(enc(publicKeyFor(asPrivate)), "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8");
});

test("a fresh message decrypts on the receiving side", () => {
  const ua = createECDH("prime256v1");
  ua.generateKeys();
  const auth = randomBytes(16);
  const plaintext = Buffer.from(JSON.stringify({ web_push: 8030, notification: { title: "x", navigate: LEDGER } }));
  const body = encrypt(plaintext, ua.getPublicKey(), auth);
  assert.equal(body.readUInt32BE(16), 4096);
  assert.equal(body[20], 65);
  const asPublic = body.subarray(21, 86);
  const salt = body.subarray(0, 16);
  // Receiver side of RFC 8291, written out independently of encrypt().
  const secret = ua.computeSecret(asPublic);
  const ikm = Buffer.from(hkdfSync("sha256", secret, auth, Buffer.concat([Buffer.from("WebPush: info\0"), ua.getPublicKey(), asPublic]), 32));
  const cek = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: aes128gcm\0"), 16));
  const nonce = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: nonce\0"), 12));
  const cipherText = body.subarray(86, body.length - 16);
  const decipher = createDecipheriv("aes-128-gcm", cek, nonce);
  decipher.setAuthTag(body.subarray(body.length - 16));
  const record = Buffer.concat([decipher.update(cipherText), decipher.final()]);
  assert.equal(record[record.length - 1], 2);
  assert.deepEqual(record.subarray(0, record.length - 1), plaintext);
});

test("encryption rejects bad subscription material", () => {
  const good = publicKeyFor(randomBytes(32));
  assert.throws(() => encrypt(Buffer.from("x"), good.subarray(1), randomBytes(16)), /uncompressed/);
  const offCurve = Buffer.from(good);
  offCurve[64] ^= 1;
  assert.throws(() => encrypt(Buffer.from("x"), offCurve, randomBytes(16)), /P-256/);
  assert.throws(() => encrypt(Buffer.from("x"), good, randomBytes(15)), /16 bytes/);
  assert.throws(() => encrypt(Buffer.alloc(4000), good, randomBytes(16)), /too large/);
});

test("VAPID header signs the push service origin for twelve hours", () => {
  const d = randomBytes(32);
  const pub = publicKeyFor(d);
  const now = 1_800_000_000_000;
  const header = vapidAuthorization("https://web.push.apple.com/QPush/abc", LEDGER, d, pub, now);
  assert.match(header, /^vapid t=[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+, k=[A-Za-z0-9_-]{87}$/);
  const claims = verifyVapid(header, pub);
  assert.deepEqual(claims, { aud: "https://web.push.apple.com", exp: now / 1000 + 43200, sub: LEDGER });
  assert.throws(() => verifyVapid(header, publicKeyFor(randomBytes(32))), /bad/);
});

test("the message is the declarative push format and only when there is news", () => {
  const alert = { new_count: 3, notification_title: "3 new job openings", notification_body: "Siemens 2, ZS 1 · 10 jobs" };
  assert.deepEqual(notificationFor(alert, LEDGER), { web_push: 8030, notification: {
    title: "K9 · 3 new job openings", body: "Siemens 2, ZS 1 · 10 jobs", lang: "en", dir: "ltr", silent: false, tag: "k9-openings", navigate: LEDGER } });
  assert.equal(notificationFor({ ...alert, new_count: 0 }, LEDGER), null);
  const t = notificationFor(null, LEDGER, true);
  assert.equal(t.web_push, 8030);
  assert.equal(t.notification.title, "K9 · Notifications are on");     // every title leads with the ledger's name
  assert.equal(t.notification.navigate, LEDGER);
  for (const bad of [{}, { new_count: -1 }, { new_count: 1 }, { new_count: 1, notification_title: "", notification_body: "x" }, "x", null]) {
    assert.throws(() => notificationFor(bad, LEDGER), /alert/);
  }
  assert.ok(!("app_badge" in t.notification));      // Safari and the draft spec disagree on its type; left out
});

const sample = () => ({ endpoint: "https://web.push.apple.com/QPush/abc", keys: { p256dh: enc(publicKeyFor(randomBytes(32))), auth: enc(randomBytes(16)) } });

test("registry entries are validated, never trusted blindly", () => {
  const one = sample();
  assert.deepEqual(parseSubscriptions([]), []);
  const [a] = parseSubscriptions([one]);
  assert.equal(a.endpoint, one.endpoint);
  assert.equal(a.host, "web.push.apple.com");
  assert.equal(a.p256dh.length, 65);
  assert.equal(a.auth.length, 16);
  assert.equal(parseSubscriptions([one, one]).length, 2);
  for (const [bad, re] of [
    [{}, /list/], ["x", /list/], [[{ endpoint: 1 }], /shape/], [[{ ...one, endpoint: "http://x.example/a" }], /https/],
    [[{ ...one, endpoint: "https://u:p" + "@x.example/a" }], /https/], [[{ ...one, keys: { p256dh: one.keys.p256dh, auth: "AAAA" } }], /auth secret/],
    [[{ ...one, keys: { p256dh: "AAAA", auth: one.keys.auth } }], /uncompressed/], [[{ ...one, keys: { p256dh: "***", auth: one.keys.auth } }], /keys/],
  ]) assert.throws(() => parseSubscriptions(bad), re);
});

test("the configured key is a valid P-256 point and the registry is a workers.dev route", () => {
  const { publicPoint, storeUrl } = configuredPush(CONFIG);
  assert.equal(publicPoint.length, 65);
  assert.equal(assertPublicPoint(publicPoint, "x"), publicPoint);
  assert.match(storeUrl, REGISTER_URL);
  for (const bad of ["http://a.b.workers.dev/subscriptions", "https://a.b.workers.dev/", "https://a.b.workers.dev.evil.example/subscriptions",
                     "https://a.b.workers.dev/subscriptions?x=1", "https://a.workers.dev/subscriptions"]) {
    assert.ok(!REGISTER_URL.test(bad), bad);
  }
});

test("the registry is read with the key and pruned of dead devices", async () => {
  const calls = [];
  const fake = async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET", auth: init.headers && init.headers.Authorization, body: init.body });
    if ((init.method || "GET") === "GET") return { status: 200, json: async () => ({ subscriptions: [sample()] }) };
    return { status: 200, json: async () => ({ ok: true, removed: 1 }) };
  };
  const list = await listRegistered(STORE, "k", fake);
  assert.equal(list.length, 1);
  assert.deepEqual(calls[0], { url: STORE, method: "GET", auth: "Bearer k", body: undefined });
  assert.equal(await pruneRegistered(STORE, "k", ["https://web.push.apple.com/QPush/dead"], fake), 1);
  assert.equal(calls[1].method, "PATCH");
  assert.equal(calls[1].body, JSON.stringify({ expired: ["https://web.push.apple.com/QPush/dead"] }));
  assert.equal(await pruneRegistered(STORE, "k", [], fake), 0);
  assert.equal(calls.length, 2);                                        // nothing to prune, no request
  await assert.rejects(listRegistered(STORE, "k", async () => ({ status: 401 })), /registry: HTTP 401/);
  await assert.rejects(listRegistered(STORE, "k", async () => ({ status: 200, json: async () => ({}) })), /list/);
});

test("delivery maps push-service answers", async () => {
  const [sub] = parseSubscriptions([{ ...sample(), endpoint: "https://push.example/s/1" }]);
  const message = notificationFor(null, LEDGER, true);
  const seen = [];
  const fake = (statuses) => async (url, init) => {
    seen.push({ url, headers: init.headers, len: init.body.length });
    return { status: statuses.shift(), text: async () => JSON.stringify({ reason: "BadJwtToken" }) };
  };
  assert.deepEqual(await deliver(sub, message, "vapid t=x, k=y", fake([201])), { status: 201, outcome: "sent" });
  assert.equal(seen[0].url, sub.endpoint);
  assert.equal(seen[0].headers["Content-Encoding"], "aes128gcm");
  assert.equal(seen[0].headers.TTL, "10800");
  assert.equal(seen[0].headers.Authorization, "vapid t=x, k=y");
  assert.ok(seen[0].len > 86 + 16);
  assert.deepEqual(await deliver(sub, message, "vapid t=x, k=y", fake([410])), { status: 410, outcome: "expired" });
  assert.deepEqual(await deliver(sub, message, "vapid t=x, k=y", fake([404])), { status: 404, outcome: "expired" });
  assert.deepEqual(await deliver(sub, message, "vapid t=x, k=y", fake([403])), { status: 403, outcome: "rejected", detail: "BadJwtToken" });
});

test("a test run can carry a series of custom messages, each with its own tag", async () => {
  const { customMessage, parseSeries } = await import("./push.mjs");
  assert.deepEqual(parseSeries(""), [{ title: "K9 · Notifications are on", body: "This is the test message. New openings will arrive here after each refresh." }]);
  assert.deepEqual(parseSeries(JSON.stringify([["Woof", " hi "], { title: "K9 · T", body: "B" }])),
                   [{ title: "K9 · Woof", body: "hi" }, { title: "K9 · T", body: "B" }]);         // prefixed once, never twice
  for (const [bad, re] of [["{", /not JSON/], ["[]", /1 to 10/], [JSON.stringify(Array(11).fill(["a", "b"])), /1 to 10/],
                           [JSON.stringify([["a"]]), /\[title, body\]/], [JSON.stringify([["", "b"]]), /length/],
                           [JSON.stringify([["a".repeat(61), "b"]]), /length/], [JSON.stringify([["a<b", "b"]]), /characters/],
                           [JSON.stringify([["a", "b\nc"]]), /characters/]]) {
    assert.throws(() => parseSeries(bad), re, bad.slice(0, 30));
  }
  assert.throws(() => customMessage(5, 0), /message #1/);
  const d = randomBytes(32);
  const { writeFileSync, mkdtempSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const cfg = join(mkdtempSync(join(tmpdir(), "push-")), "c.json");
  writeFileSync(cfg, JSON.stringify({ push: { vapid_public_key: enc(publicKeyFor(d)), register_url: STORE } }));
  const sent = [], slept = [], logs = [];
  const fake = async (url, init = {}) => {
    if (url === STORE) return { status: 200, json: async () => ({ subscriptions: [sample()] }) };
    sent.push(init.body.length);
    return { status: 201, text: async () => "{}" };
  };
  const env = { VAPID_PRIVATE_KEY: enc(d), PUSH_STORE_KEY: "k", MESSAGES: JSON.stringify([["One", "first"], ["Two", "second"], ["Three", "third"]]) };
  const code = await main(["--test", "--series-env", "MESSAGES", "--interval-s", "5", "--ledger-url", LEDGER], env, (m) => logs.push(m), fake, cfg, async (ms) => slept.push(ms));
  assert.equal(code, 0);
  assert.equal(sent.length, 3);
  assert.deepEqual(slept, [5000, 5000]);                                  // between messages, not after the last
  assert.match(logs[0], /3 test messages, 5 s apart to 1 device/);
  assert.match(logs.join("\n"), /message 2 #1 \(web\.push\.apple\.com\): sent/);
  // Distinct tags so the series stacks on the phone; a single message keeps the usual tag.
  assert.equal(notificationFor(null, LEDGER, true, { title: "a", body: "b", tag: "k9-openings-2" }).notification.tag, "k9-openings-2");
  assert.equal(notificationFor(null, LEDGER, true, { title: "a", body: "b" }).notification.tag, "k9-openings");
  await assert.rejects(main(["--test", "--interval-s", "0", "--ledger-url", LEDGER], env, () => {}, fake, cfg), /interval/);
  await assert.rejects(main(["--alert-env", "X", "--series-env", "MESSAGES", "--ledger-url", LEDGER], env, () => {}, fake, cfg), /series-env/);
});

test("a run sends to every registered device, prunes the dead ones and fails only on real errors", async () => {
  const d = randomBytes(32);
  const { publicPoint } = configuredPush(CONFIG);
  // A throwaway config whose public key matches our throwaway private key.
  const { writeFileSync, mkdtempSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const dir = mkdtempSync(join(tmpdir(), "push-"));
  const cfg = join(dir, "c.json");
  writeFileSync(cfg, JSON.stringify({ push: { vapid_public_key: enc(publicKeyFor(d)), register_url: STORE } }));
  assert.notEqual(enc(publicKeyFor(d)), enc(publicPoint));
  const env = { VAPID_PRIVATE_KEY: enc(d), PUSH_STORE_KEY: "k", ALERT: JSON.stringify({ new_count: 2, notification_title: "2 new job openings", notification_body: "x" }) };
  const run = async (statuses, registry = [sample(), { ...sample(), endpoint: "https://web.push.apple.com/QPush/dead" }]) => {
    const logs = [], calls = [];
    const fake = async (url, init = {}) => {
      const method = init.method || "GET";
      calls.push({ url, method });
      if (url === STORE && method === "GET") return { status: 200, json: async () => ({ subscriptions: registry }) };
      if (url === STORE && method === "PATCH") return { status: 200, json: async () => ({ ok: true, removed: JSON.parse(init.body).expired.length }) };
      return { status: statuses.shift(), text: async () => "{}" };
    };
    const code = await main(["--alert-env", "ALERT", "--ledger-url", LEDGER], env, (m) => logs.push(m), fake, cfg);
    return { code, logs, calls };
  };
  let r = await run([201, 410]);
  assert.equal(r.code, 0);
  assert.deepEqual(r.calls.map((c) => c.method), ["GET", "POST", "POST", "PATCH"]);
  assert.match(r.logs.join("\n"), /#1 \(web\.push\.apple\.com\): sent \(HTTP 201\)/);
  assert.match(r.logs.join("\n"), /#2 \(web\.push\.apple\.com\): expired \(HTTP 410\)/);
  assert.match(r.logs.join("\n"), /1 device no longer reachable, removed from the registry/);
  r = await run([403, 201]);
  assert.equal(r.code, 1);                                               // a rejection is a real failure
  assert.deepEqual(r.calls.map((c) => c.method), ["GET", "POST", "POST"]);   // nothing to prune
  r = await run([], []);
  assert.equal(r.code, 0);
  assert.match(r.logs.join("\n"), /no device is registered/);
  // Nothing new: the registry is not even consulted.
  const logs = [];
  const quiet = await main(["--alert-env", "ALERT", "--ledger-url", LEDGER], { ...env, ALERT: JSON.stringify({ new_count: 0 }) }, (m) => logs.push(m), async () => { throw new Error("no network expected"); }, cfg);
  assert.equal(quiet, 0);
  assert.match(logs.join("\n"), /no new openings/);
  for (const [bad, re] of [[{ ...env, PUSH_STORE_KEY: "" }, /PUSH_STORE_KEY/], [{ ...env, VAPID_PRIVATE_KEY: "" }, /VAPID_PRIVATE_KEY/],
                           [{ ...env, VAPID_PRIVATE_KEY: enc(randomBytes(32)) }, /does not match/]]) {
    await assert.rejects(main(["--alert-env", "ALERT", "--ledger-url", LEDGER], bad, () => {}, async () => ({ status: 200, json: async () => ({ subscriptions: [] }) }), cfg), re);
  }
  for (const secret of ["endpoint", "QPush"]) assert.ok(!r.logs.join("\n").includes(secret), secret);   // never logged
});
