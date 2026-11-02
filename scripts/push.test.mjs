// node --test scripts/push.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createDecipheriv, createECDH, hkdfSync, randomBytes } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assertPublicPoint, configuredPublicKey, dec, deliver, enc, encrypt, notificationFor, parseSubscriptions, publicKeyFor,
         vapidAuthorization, verifyVapid } from "./push.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const LEDGER = "https://example.github.io/ledger/";

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
    title: "3 new job openings", body: "Siemens 2, ZS 1 · 10 jobs", lang: "en", dir: "ltr", silent: false, tag: "k9-openings", navigate: LEDGER } });
  assert.equal(notificationFor({ ...alert, new_count: 0 }, LEDGER), null);
  const t = notificationFor(null, LEDGER, true);
  assert.equal(t.web_push, 8030);
  assert.equal(t.notification.title, "K9 test");
  assert.equal(t.notification.navigate, LEDGER);
  for (const bad of [{}, { new_count: -1 }, { new_count: 1 }, { new_count: 1, notification_title: "", notification_body: "x" }, "x", null]) {
    assert.throws(() => notificationFor(bad, LEDGER), /alert/);
  }
  assert.ok(!("app_badge" in t.notification));      // Safari and the draft spec disagree on its type; left out
});

test("subscriptions: one object or a list, validated, never trusted blindly", () => {
  const key = enc(publicKeyFor(randomBytes(32)));
  const auth = enc(randomBytes(16));
  const one = { endpoint: "https://web.push.apple.com/QPush/abc", expirationTime: null, keys: { p256dh: key, auth } };
  assert.deepEqual(parseSubscriptions(""), []);
  assert.deepEqual(parseSubscriptions(undefined), []);
  const [a] = parseSubscriptions(JSON.stringify(one));
  assert.equal(a.endpoint, one.endpoint);
  assert.equal(a.host, "web.push.apple.com");
  assert.equal(a.p256dh.length, 65);
  assert.equal(a.auth.length, 16);
  assert.equal(parseSubscriptions(JSON.stringify([one, one])).length, 2);
  for (const [bad, re] of [
    ["{", /not JSON/], [JSON.stringify({ endpoint: 1 }), /shape/], [JSON.stringify({ ...one, endpoint: "http://x.example/a" }), /https/],
    [JSON.stringify({ ...one, endpoint: "https://u:p" + "@x.example/a" }), /https/], [JSON.stringify({ ...one, keys: { p256dh: key, auth: "AAAA" } }), /auth secret/],
    [JSON.stringify({ ...one, keys: { p256dh: "AAAA", auth } }), /uncompressed/], [JSON.stringify({ ...one, keys: { p256dh: "***", auth } }), /keys/],
  ]) assert.throws(() => parseSubscriptions(bad), re);
});

test("the configured public key is a valid P-256 point", () => {
  const pub = configuredPublicKey(join(HERE, "..", "config", "public_sources.json"));
  assert.equal(pub.length, 65);
  assert.equal(assertPublicPoint(pub, "x"), pub);
});

test("delivery maps push-service answers and retries only transient ones", async () => {
  const sub = parseSubscriptions(JSON.stringify({ endpoint: "https://push.example/s/1", keys: { p256dh: enc(publicKeyFor(randomBytes(32))), auth: enc(randomBytes(16)) } }))[0];
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
