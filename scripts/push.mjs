#!/usr/bin/env node
// Sends the ledger's phone notification as a Web Push message, with nothing but Node's standard library.
//
// After a refresh deploys, the workflow runs this with the run's alert payload (the same JSON as alerts.json).
// When the refresh found new openings, one Declarative Web Push message (W3C Push API, as shipped in Safari
// 18.4+) goes to every subscription in the PUSH_SUBSCRIPTIONS secret: the browser shows it as a normal
// notification and opens the ledger when it is tapped. No service worker, no third party: the message travels
// from GitHub Actions to the push service the subscribing browser named (Apple's, for an iPhone), encrypted
// to the device (RFC 8291) and signed with the ledger's VAPID key (RFC 8292).
//
//   node scripts/push.mjs --alert-env ALERT --ledger-url https://example.github.io/ledger/
//   node scripts/push.mjs --test --ledger-url https://example.github.io/ledger/
//
// Environment: VAPID_PRIVATE_KEY (the P-256 private scalar, base64url; its public half lives in
// config/public_sources.json so the page can subscribe) and PUSH_SUBSCRIPTIONS (one PushSubscription JSON
// object, or an array of them, copied from the ledger's Requests panel). Neither is ever printed.
// A dead subscription (HTTP 404/410 from the push service) fails the run so the owner learns to re-subscribe.

import { createCipheriv, createECDH, createPrivateKey, hkdfSync, randomBytes, sign, verify, createPublicKey } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const B64URL = /^[A-Za-z0-9_-]+$/;
export const enc = (bytes) => Buffer.from(bytes).toString("base64url");
export const dec = (text) => {
  if (typeof text !== "string" || !B64URL.test(text)) throw new Error("not base64url");
  return Buffer.from(text, "base64url");
};

const RECORD_SIZE = 4096;                 // one record; RFC 8291 §4 caps the whole body at 4096 octets
const TTL_S = 3 * 3600;                   // undelivered after three hours: the next refresh supersedes it
const RETRY_MS = [2000, 6000];            // after the first attempt, for 429 and 5xx answers
const NOTIFICATION_TAG = "k9-openings";   // a newer notification replaces an unread older one

// ---------------------------------------------------------------------- RFC 8291: message encryption

/** Checks a point is a 65-byte uncompressed P-256 point by letting the curve implementation reject it. */
export function assertPublicPoint(bytes, what) {
  if (!(bytes instanceof Uint8Array) || bytes.length !== 65 || bytes[0] !== 4) throw new Error(`${what}: not an uncompressed P-256 point`);
  try {
    createPublicKey({ key: { kty: "EC", crv: "P-256", x: enc(bytes.subarray(1, 33)), y: enc(bytes.subarray(33, 65)) }, format: "jwk" });
  } catch {
    throw new Error(`${what}: not on P-256`);
  }
  return bytes;
}

/**
 * Encrypts plaintext for one subscription (RFC 8291 with the aes128gcm content coding of RFC 8188).
 * uaPublic: the subscription's p256dh key (65 bytes); authSecret: its auth secret (16 bytes).
 * options.asPrivate / options.salt fix the sender's ephemeral key and salt for known-answer tests.
 */
export function encrypt(plaintext, uaPublic, authSecret, options = {}) {
  assertPublicPoint(uaPublic, "subscription key");
  if (!(authSecret instanceof Uint8Array) || authSecret.length !== 16) throw new Error("subscription auth secret: not 16 bytes");
  if (plaintext.length > RECORD_SIZE - 16 - 1 - 86) throw new Error("payload too large for one record");
  const ecdh = createECDH("prime256v1");
  if (options.asPrivate) ecdh.setPrivateKey(options.asPrivate); else ecdh.generateKeys();
  const asPublic = ecdh.getPublicKey();                                   // uncompressed, 65 bytes
  const ecdhSecret = ecdh.computeSecret(uaPublic);
  const keyInfo = Buffer.concat([Buffer.from("WebPush: info\0"), uaPublic, asPublic]);
  const ikm = Buffer.from(hkdfSync("sha256", ecdhSecret, authSecret, keyInfo, 32));
  const salt = options.salt || randomBytes(16);
  if (salt.length !== 16) throw new Error("salt: not 16 bytes");
  const cek = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: aes128gcm\0"), 16));
  const nonce = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: nonce\0"), 12));
  const cipher = createCipheriv("aes-128-gcm", cek, nonce);
  const record = Buffer.concat([plaintext, Buffer.from([2])]);          // 0x02: delimiter of the last record
  const body = Buffer.concat([cipher.update(record), cipher.final(), cipher.getAuthTag()]);
  const rs = Buffer.alloc(4);
  rs.writeUInt32BE(RECORD_SIZE);
  return Buffer.concat([salt, rs, Buffer.from([asPublic.length]), asPublic, body]);
}

// ---------------------------------------------------------------------- RFC 8292: VAPID

/** The uncompressed public point that belongs to a private scalar. */
export function publicKeyFor(privateScalar) {
  const ecdh = createECDH("prime256v1");
  ecdh.setPrivateKey(privateScalar);
  return ecdh.getPublicKey();
}

function privateKeyObject(privateScalar, publicPoint) {
  return createPrivateKey({ key: { kty: "EC", crv: "P-256", x: enc(publicPoint.subarray(1, 33)), y: enc(publicPoint.subarray(33, 65)), d: enc(privateScalar) }, format: "jwk" });
}

/** The Authorization header for one push service origin: a 12-hour ES256 JWT and the public key. */
export function vapidAuthorization(endpoint, subject, privateScalar, publicPoint, nowMs = Date.now()) {
  const aud = new URL(endpoint).origin;
  const header = enc(JSON.stringify({ typ: "JWT", alg: "ES256" }));
  const claims = enc(JSON.stringify({ aud, exp: Math.floor(nowMs / 1000) + 12 * 3600, sub: subject }));
  const signingInput = `${header}.${claims}`;
  const signature = sign("sha256", Buffer.from(signingInput), { key: privateKeyObject(privateScalar, publicPoint), dsaEncoding: "ieee-p1363" });
  return `vapid t=${signingInput}.${enc(signature)}, k=${enc(publicPoint)}`;
}

/** Verifies a VAPID Authorization header against a public point; returns the claims. Used by the tests. */
export function verifyVapid(authorization, publicPoint) {
  const m = /^vapid t=([^,]+), k=([A-Za-z0-9_-]+)$/.exec(authorization);
  if (!m || m[2] !== enc(publicPoint)) throw new Error("bad vapid header");
  const [header, claims, signature] = m[1].split(".");
  const key = createPublicKey({ key: { kty: "EC", crv: "P-256", x: enc(publicPoint.subarray(1, 33)), y: enc(publicPoint.subarray(33, 65)) }, format: "jwk" });
  if (!verify("sha256", Buffer.from(`${header}.${claims}`), { key, dsaEncoding: "ieee-p1363" }, dec(signature))) throw new Error("bad signature");
  if (JSON.parse(dec(header)).alg !== "ES256") throw new Error("bad alg");
  return JSON.parse(dec(claims));
}

// ---------------------------------------------------------------------- the message

const TEST_TITLE = "K9 test";
const TEST_BODY = "Notifications are working. New openings will arrive here after each refresh.";

/** The declarative push message for one alert payload, or null when there is nothing to tell. */
export function notificationFor(alert, ledgerUrl, test = false) {
  const base = { lang: "en", dir: "ltr", silent: false, tag: NOTIFICATION_TAG, navigate: ledgerUrl };
  if (test) return { web_push: 8030, notification: { title: TEST_TITLE, body: TEST_BODY, ...base } };
  if (!alert || typeof alert !== "object") throw new Error("alert: not an object");
  const n = alert.new_count;
  if (!Number.isInteger(n) || n < 0) throw new Error("alert: new_count");
  if (n === 0) return null;
  const { notification_title: title, notification_body: body } = alert;
  if (typeof title !== "string" || !title || typeof body !== "string") throw new Error("alert: notification text");
  return { web_push: 8030, notification: { title, body, ...base } };
}

/** PUSH_SUBSCRIPTIONS: one PushSubscription JSON or an array of them; empty means nobody is subscribed. */
export function parseSubscriptions(text) {
  if (!text || !text.trim()) return [];
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("PUSH_SUBSCRIPTIONS: not JSON");
  }
  const list = Array.isArray(value) ? value : [value];
  return list.map((s, i) => {
    const what = `subscription #${i + 1}`;
    if (!s || typeof s !== "object" || typeof s.endpoint !== "string" || !s.keys || typeof s.keys !== "object") throw new Error(`${what}: shape`);
    let endpoint;
    try {
      endpoint = new URL(s.endpoint);
    } catch {
      throw new Error(`${what}: endpoint`);
    }
    if (endpoint.protocol !== "https:" || endpoint.username || endpoint.password) throw new Error(`${what}: endpoint must be https`);
    let p256dh, auth;
    try {
      p256dh = dec(s.keys.p256dh);
      auth = dec(s.keys.auth);
    } catch {
      throw new Error(`${what}: keys`);
    }
    assertPublicPoint(p256dh, what);
    if (auth.length !== 16) throw new Error(`${what}: auth secret`);
    return { endpoint: endpoint.href, host: endpoint.host, p256dh, auth };
  });
}

/** Reads the page's VAPID public key from the repository config, as the subscribing browser saw it. */
export function configuredPublicKey(configPath) {
  const config = JSON.parse(readFileSync(configPath, "utf8"));
  const text = config && config.push && config.push.vapid_public_key;
  return assertPublicPoint(dec(text), "config push.vapid_public_key");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Posts one encrypted message; returns {status, outcome} where outcome is sent | expired | rejected | unavailable. */
export async function deliver(subscription, message, authorization, fetchImpl = fetch) {
  const body = encrypt(Buffer.from(JSON.stringify(message)), subscription.p256dh, subscription.auth);
  for (let attempt = 0; ; attempt++) {
    let response;
    try {
      response = await fetchImpl(subscription.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream", "Content-Encoding": "aes128gcm", TTL: String(TTL_S), Urgency: "normal", Authorization: authorization },
        body,
      });
    } catch (err) {
      if (attempt < RETRY_MS.length) { await sleep(RETRY_MS[attempt]); continue; }
      return { status: 0, outcome: "unavailable", detail: err instanceof Error ? err.name : "error" };
    }
    const { status } = response;
    if (status === 200 || status === 201 || status === 202) return { status, outcome: "sent" };
    if (status === 404 || status === 410) return { status, outcome: "expired" };
    if ((status === 429 || status >= 500) && attempt < RETRY_MS.length) { await sleep(RETRY_MS[attempt]); continue; }
    let detail = "";
    try {
      const text = await response.text();
      detail = (JSON.parse(text).reason || "").toString().slice(0, 60);
    } catch { /* no usable reason; the status suffices */ }
    return { status, outcome: status === 429 || status >= 500 ? "unavailable" : "rejected", detail };
  }
}

// ---------------------------------------------------------------------- CLI

function parseArgs(argv) {
  const out = { test: false, alertEnv: "", alertFile: "", ledgerUrl: "", dryRun: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--test") out.test = true;
    else if (a === "--dry-run") out.dryRun = true;
    else if (a === "--alert-env") out.alertEnv = argv[++i] || "";
    else if (a === "--alert-file") out.alertFile = argv[++i] || "";
    else if (a === "--ledger-url") out.ledgerUrl = argv[++i] || "";
    else throw new Error(`unknown argument ${a}`);
  }
  if (!/^https:\/\/[a-z0-9-]+\.github\.io\/[A-Za-z0-9._-]+\/$/.test(out.ledgerUrl)) throw new Error("--ledger-url must be the site's GitHub Pages URL");
  if (!out.test && !out.alertEnv && !out.alertFile) throw new Error("need --test, --alert-env NAME or --alert-file PATH");
  return out;
}

export async function main(argv, env = process.env, log = console.log) {
  const args = parseArgs(argv);
  const subscriptions = parseSubscriptions(env.PUSH_SUBSCRIPTIONS);
  if (!subscriptions.length) {
    log("push: PUSH_SUBSCRIPTIONS is not set; nothing to send");
    return 0;
  }
  let alert = null;
  if (!args.test) {
    const text = args.alertFile ? readFileSync(args.alertFile, "utf8") : env[args.alertEnv];
    if (!text) throw new Error("alert payload is empty");
    alert = JSON.parse(text);
  }
  const message = notificationFor(alert, args.ledgerUrl, args.test);
  if (!message) {
    log("push: no new openings in this refresh; nothing to send");
    return 0;
  }
  const publicPoint = configuredPublicKey(join(dirname(fileURLToPath(import.meta.url)), "..", "config", "public_sources.json"));
  if (!env.VAPID_PRIVATE_KEY) throw new Error("VAPID_PRIVATE_KEY secret is not set");
  const privateScalar = dec(env.VAPID_PRIVATE_KEY);
  if (privateScalar.length !== 32 || !publicKeyFor(privateScalar).equals(publicPoint)) throw new Error("VAPID_PRIVATE_KEY does not match config push.vapid_public_key");
  log(`push: ${args.test ? "test message" : `"${message.notification.title}"`} to ${subscriptions.length} subscription${subscriptions.length === 1 ? "" : "s"}`);
  if (args.dryRun) {
    log("push: dry run; nothing sent");
    return 0;
  }
  let failures = 0;
  for (const [i, sub] of subscriptions.entries()) {
    const authorization = vapidAuthorization(sub.endpoint, args.ledgerUrl, privateScalar, publicPoint);
    const result = await deliver(sub, message, authorization);
    const detail = result.detail ? ` ${result.detail}` : "";
    log(`push: #${i + 1} (${sub.host}): ${result.outcome} (HTTP ${result.status})${detail}`);
    if (result.outcome !== "sent") failures++;
    if (result.outcome === "expired") {
      log(`push: #${i + 1} is no longer valid. Open the ledger from its Home Screen icon, [ requests ] > signal notify > Turn on, then replace the PUSH_SUBSCRIPTIONS secret with the new code.`);
    }
  }
  return failures ? 1 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).then((code) => process.exit(code), (err) => {
    console.error(`push: ${err instanceof Error ? err.message : err}`);
    process.exit(1);
  });
}
