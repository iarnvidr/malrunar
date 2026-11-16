// Behaviour test for the two inline-image fixes build-xpi.py applies to the add-on's api.js.
//   node xpi-behaviour.cjs <path to the packed add-on's mcp_server/api.js>
// It lifts the two patched functions out of the packed source and runs them against fake streams, because the
// add-on itself only runs inside Thunderbird.
"use strict";
const fs = require("fs");
const assert = require("assert");

const source = fs.readFileSync(process.argv[2], "utf8");

function lift(startMarker) {
  const start = source.indexOf(startMarker);
  assert(start >= 0, `not found in the packed add-on: ${startMarker}`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces after ${startMarker}`);
}

const alphabet = source.match(/^const BASE64_ALPHABET = .*;$/m);
assert(alphabet, "BASE64_ALPHABET is missing from the packed add-on");
const encodeSource = lift("function encodeByteStringToBase64");
const readSource = lift("function readMessageStreamFully");
assert(!/\bbtoa\(/.test(encodeSource), "the encoder still calls btoa, which the add-on scope does not define");

const CLOSED = 0x80470002; // NS_BASE_STREAM_CLOSED

class FakeStream {
  // Delivers `data` in `chunks`, then (like a closed pipe) throws from available() instead of returning 0.
  constructor(data, { chunks = 1, endBy = "closed", availableOverride = null } = {}) {
    this.data = data; this.pos = 0; this.endBy = endBy; this.availableOverride = availableOverride;
    this.step = Math.max(1, Math.ceil(data.length / chunks));
  }
  available() {
    if (this.availableOverride !== null) return this.availableOverride;
    const left = this.data.length - this.pos;
    if (left > 0) return Math.min(left, this.step);
    if (this.endBy === "closed") { const e = new Error("closed"); e.result = CLOSED; throw e; }
    if (this.endBy === "other") { const e = new Error("boom"); e.result = 0x80004005; throw e; }
    return 0;
  }
}
const NetUtil = {
  readInputStreamToString(stream, count) {
    const out = stream.data.slice(stream.pos, stream.pos + count);
    stream.pos += out.length;
    return out;
  },
};

const factory = new Function("NetUtil", `${alphabet[0]}\n${encodeSource}\n${readSource}\nreturn { encodeByteStringToBase64, readMessageStreamFully };`);
const { encodeByteStringToBase64, readMessageStreamFully } = factory(NetUtil);

function byteString(length, seed) {
  let s = "", x = seed;
  for (let i = 0; i < length; i++) { x = (x * 1103515245 + 12345) & 0x7fffffff; s += String.fromCharCode(x & 255); }
  return s;
}

// 1. The encoder matches Buffer for every padding case and for a large input, including all byte values.
for (const n of [0, 1, 2, 3, 4, 5, 6, 7, 100, 255, 256, 257, 65535, 65536, 65537, 300000]) {
  const s = byteString(n, n + 1);
  assert.strictEqual(encodeByteStringToBase64(s), Buffer.from(s, "latin1").toString("base64"), `length ${n}`);
}
assert.strictEqual(encodeByteStringToBase64(String.fromCharCode(...Array.from({ length: 256 }, (_, i) => i))),
  Buffer.from(Array.from({ length: 256 }, (_, i) => i)).toString("base64"));
assert.strictEqual(encodeByteStringToBase64(null), "");
assert.strictEqual(encodeByteStringToBase64(undefined), "");

// 2. A stream that closes after delivering everything is read in full (the bug skipped every image).
const png = byteString(56001, 7);
assert.strictEqual(readMessageStreamFully(new FakeStream(png, { chunks: 1 }), 100 * 1024 * 1024), png);
assert.strictEqual(readMessageStreamFully(new FakeStream(png, { chunks: 5 }), 100 * 1024 * 1024), png);
// ... and a stream that reports 0 at the end still works.
assert.strictEqual(readMessageStreamFully(new FakeStream(png, { chunks: 3, endBy: "zero" }), 100 * 1024 * 1024), png);
// ... and one that is closed before any data yields an empty string (the caller reports "Image data is empty").
assert.strictEqual(readMessageStreamFully(new FakeStream("", { endBy: "closed" }), 1024), "");

// 3. A real overflow still throws the "message too large" error the caller maps to the size-limit message.
assert.throws(() => readMessageStreamFully(new FakeStream("x", { availableOverride: 5000 }), 1000), /message too large/);
assert.throws(() => readMessageStreamFully(new FakeStream(byteString(2000, 3), { chunks: 4 }), 1000), /message too large/);

// 4. Any other failure is not swallowed as end of data.
assert.throws(() => readMessageStreamFully(new FakeStream(png, { chunks: 1, endBy: "other" }), 100 * 1024 * 1024), /boom/);

// 5. The caller says "exceeds the limit" only for the overflow error, and names any other read failure.
assert(/startsWith\("message too large"\)/.test(source), "the read-error handler no longer tells overflow from other failures");
assert(source.includes("Inline image read failed:"), "other read failures are not reported as such");

console.log("xpi-behaviour: ok");
