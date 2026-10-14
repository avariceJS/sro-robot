import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { chunkFiles, validateFiles, chunkIdempotencyKey, filesFingerprint, resolveBatchKeys, clearChunkKeys, uploadChunks } = require(
  "../app/web/static/upload-batch.js",
);

function corpus(count, size = 1024) {
  return Array.from({ length: count }, (_, index) => ({
    name: `synthetic-${index + 1}.pdf`,
    size,
  }));
}

class FakeFormData {
  constructor() {
    this.values = [];
  }
  append(...value) {
    this.values.push(value);
  }
}

function webCrypto() {
  return globalThis.crypto;
}

function fingerprintable(name, value) {
  const bytes = new TextEncoder().encode(value);
  return {
    name,
    size: bytes.length,
    lastModified: 123,
    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
  };
}

async function legacyFilesFingerprint(files, cryptoApi) {
  const entries = [];
  for (const file of files) {
    const contentDigest = await cryptoApi.subtle.digest("SHA-256", await file.arrayBuffer());
    entries.push({
      name: file.name,
      size: file.size,
      lastModified: file.lastModified,
      sha256: Array.from(new Uint8Array(contentDigest), (byte) => byte.toString(16).padStart(2, "0")).join(""),
    });
  }
  const digest = await cryptoApi.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(JSON.stringify(entries)),
  );
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function memoryStorage(map = new Map()) {
  return {
    map,
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value),
    removeItem: (key) => map.delete(key),
  };
}

test("a single selection of 100 files is split into five bounded requests", () => {
  const files = corpus(100);
  const chunks = chunkFiles(files, 20);
  assert.equal(chunks.length, 5);
  assert.deepEqual(chunks.map((chunk) => chunk.length), [20, 20, 20, 20, 20]);
  assert.deepEqual(chunks.flat(), files);
});

test("preflight rejects empty, too many, empty-sized, per-file and total-byte violations", () => {
  const limits = { maxFiles: 100, maxFileBytes: 1000, maxBatchBytes: 5000 };
  assert.match(validateFiles([], limits), /Выберите/);
  assert.match(validateFiles(corpus(101), limits), /не более 100/);
  assert.match(validateFiles([{ name: "empty.pdf", size: 0 }], limits), /Пустые/);
  assert.match(validateFiles([{ name: `${"x".repeat(256)}.pdf`, size: 1 }], limits), /слишком длинное/);
  assert.match(validateFiles([{ name: "large.pdf", size: 1001 }], limits), /превышает/);
  assert.match(validateFiles(corpus(6, 900), limits), /Общий размер/);
  assert.equal(validateFiles(corpus(5, 900), limits), null);
});

test("chunk idempotency keys are stable by form key and distinct by chunk index", async () => {
  const first = await chunkIdempotencyKey("a".repeat(48), 0, webCrypto());
  const replay = await chunkIdempotencyKey("a".repeat(48), 0, webCrypto());
  const next = await chunkIdempotencyKey("a".repeat(48), 1, webCrypto());
  assert.equal(first, replay);
  assert.notEqual(first, next);
  assert.match(first, /^[0-9a-f]{48}$/);
  await assert.rejects(chunkIdempotencyKey("bad-key", 0, webCrypto()));
});

test("batch and chunk fingerprints read each selected file only once", async () => {
  const reads = new Map();
  const files = ["one", "two", "three"].map((value) => {
    const file = fingerprintable(`${value}.pdf`, value);
    const read = file.arrayBuffer;
    file.arrayBuffer = async () => {
      reads.set(file.name, (reads.get(file.name) || 0) + 1);
      return read();
    };
    return file;
  });
  await resolveBatchKeys(files, "a".repeat(48), 2, {
    cryptoApi: webCrypto(),
    storage: memoryStorage(),
    storageKey: "single-read",
  });
  assert.deepEqual([...reads.values()], [1, 1, 1]);
});

test("file copy timestamps do not change a retry key, but filenames do", async () => {
  const options = { cryptoApi: webCrypto(), storage: memoryStorage(), storageKey: "copy-retry" };
  const original = fingerprintable("same.pdf", "same PDF bytes");
  const copied = fingerprintable("same.pdf", "same PDF bytes");
  copied.lastModified = 987654321;
  const originalKeys = await resolveBatchKeys([original], "a".repeat(48), 20, options);
  const copiedKeys = await resolveBatchKeys([copied], "b".repeat(48), 20, options);
  assert.deepEqual(copiedKeys.keys, originalKeys.keys);

  const renamedKeys = await resolveBatchKeys(
    [fingerprintable("renamed.pdf", "same PDF bytes")],
    "c".repeat(48),
    20,
    options,
  );
  assert.notEqual(renamedKeys.keys[0], originalKeys.keys[0]);
});

test("unchanged chunks retain idempotency keys when a later chunk changes", async () => {
  const storage = memoryStorage();
  const options = { cryptoApi: webCrypto(), storage, storageKey: "test-batch" };
  const files = [
    fingerprintable("one.pdf", "one"),
    fingerprintable("two.pdf", "two"),
    fingerprintable("three.pdf", "three"),
    fingerprintable("four.pdf", "four"),
  ];
  const initial = await resolveBatchKeys(files, "a".repeat(48), 2, options);
  const afterTabClose = await resolveBatchKeys(files, "b".repeat(48), 2, options);
  assert.deepEqual(afterTabClose.keys, initial.keys);

  const changedLaterChunk = await resolveBatchKeys(
    [...files.slice(0, 2), fingerprintable("three.pdf", "corrected"), files[3]],
    "c".repeat(48),
    2,
    options,
  );
  assert.equal(changedLaterChunk.keys[0], initial.keys[0]);
  assert.notEqual(changedLaterChunk.keys[1], initial.keys[1]);

  // A definite 4xx on chunk 2 clears only that unaccepted chunk; chunk 1 remains replay-safe.
  clearChunkKeys(options, [changedLaterChunk.chunkFingerprints[1]], [changedLaterChunk.keys[1]]);
  const retryAfter4xx = await resolveBatchKeys(
    [...files.slice(0, 2), fingerprintable("three.pdf", "corrected"), files[3]],
    "d".repeat(48),
    2,
    options,
  );
  assert.equal(retryAfter4xx.keys[0], initial.keys[0]);
  assert.notEqual(retryAfter4xx.keys[1], changedLaterChunk.keys[1]);

  clearChunkKeys(options, retryAfter4xx.chunkFingerprints, retryAfter4xx.keys);
  assert.deepEqual(JSON.parse(storage.map.get("test-batch")).chunkKeys, {
    [initial.chunkFingerprints[1]]: initial.keys[1],
  });
  clearChunkKeys(options, initial.chunkFingerprints, initial.keys);
  assert.equal(storage.map.has("test-batch"), false);

  const repeated = [fingerprintable("same.pdf", "same"), fingerprintable("same.pdf", "same")];
  const repeatedChunks = await resolveBatchKeys(repeated, "e".repeat(48), 1, options);
  assert.notEqual(repeatedChunks.keys[0], repeatedChunks.keys[1]);
});

test("whole-batch v1 and v2 pending keys migrate to stable per-chunk keys", async () => {
  const files = [
    fingerprintable("legacy-one.pdf", "legacy one"),
    fingerprintable("legacy-two.pdf", "legacy two"),
  ];
  const fingerprint = await legacyFilesFingerprint(files, webCrypto());
  const storageKey = "sro-upload-batch-v1";
  const optionsFor = (value) => {
    const storage = memoryStorage(new Map([[storageKey, JSON.stringify(value)]]));
    return { storage, options: { cryptoApi: webCrypto(), storage, storageKey } };
  };

  for (const legacy of [
    { fingerprint, baseKey: "e".repeat(48) },
    { version: 2, entries: { [fingerprint]: "e".repeat(48) } },
  ]) {
    const { storage, options } = optionsFor(legacy);
    const resolved = await resolveBatchKeys(files, "f".repeat(48), 1, options);
    assert.deepEqual(resolved.keys, [
      await chunkIdempotencyKey("e".repeat(48), 0, webCrypto()),
      await chunkIdempotencyKey("e".repeat(48), 1, webCrypto()),
    ]);
    assert.deepEqual(JSON.parse(storage.map.get(storageKey)), {
      version: 3,
      chunkKeys: Object.fromEntries(resolved.chunkFingerprints.map((key, index) => [key, resolved.keys[index]])),
      legacyBatches: {},
    });
    assert.equal(storage.map.get(storageKey).includes("legacy one"), false);
    assert.equal(storage.map.get(storageKey).includes("legacy-one.pdf"), false);
  }
});

test("a pending sessionStorage key moves to persistent storage after writing", async () => {
  const persistent = memoryStorage();
  const session = memoryStorage();
  const storageKey = "sro-upload-batch-v1";
  const files = [fingerprintable("pending.pdf", "accepted but unconfirmed")];
  const fingerprint = await legacyFilesFingerprint(files, webCrypto());
  session.setItem(storageKey, JSON.stringify({ fingerprint, baseKey: "1".repeat(48) }));
  const options = {
    cryptoApi: webCrypto(), storage: persistent, legacyStorage: session, storageKey,
  };
  const resolved = await resolveBatchKeys(files, "2".repeat(48), 1, options);
  assert.deepEqual(resolved.keys, [await chunkIdempotencyKey("1".repeat(48), 0, webCrypto())]);
  assert.equal(session.getItem(storageKey), null);
  assert.equal(persistent.getItem(storageKey).includes("accepted but unconfirmed"), false);
});

test("a failed persistent-storage write keeps the legacy session key available", async () => {
  const session = memoryStorage();
  const storageKey = "sro-upload-batch-v1";
  const files = [fingerprintable("pending.pdf", "old pending")];
  const fingerprint = await legacyFilesFingerprint(files, webCrypto());
  session.setItem(storageKey, JSON.stringify({ fingerprint, baseKey: "6".repeat(48) }));
  const unavailableStorage = {
    getItem: () => null,
    setItem: () => { throw new Error("storage quota"); },
    removeItem: () => {},
  };

  const resolved = await resolveBatchKeys(files, "7".repeat(48), 1, {
    cryptoApi: webCrypto(),
    storage: unavailableStorage,
    legacyStorage: session,
    storageKey,
  });
  assert.deepEqual(resolved.keys, [await chunkIdempotencyKey("6".repeat(48), 0, webCrypto())]);
  assert.notEqual(session.getItem(storageKey), null);
});

test("legacy tab key migration merges other pending chunk keys", async () => {
  const persistent = memoryStorage();
  const session = memoryStorage();
  const storageKey = "sro-upload-batch-v1";
  const existing = [fingerprintable("existing.pdf", "pending one")];
  const legacy = [fingerprintable("legacy.pdf", "pending two")];
  const existingFingerprint = await filesFingerprint(existing, webCrypto());
  const legacyFingerprint = await legacyFilesFingerprint(legacy, webCrypto());
  persistent.setItem(storageKey, JSON.stringify({
    version: 3,
    chunkKeys: { [existingFingerprint]: "3".repeat(48) },
    legacyBatches: {},
  }));
  session.setItem(storageKey, JSON.stringify({
    fingerprint: legacyFingerprint,
    baseKey: "4".repeat(48),
  }));
  const resolved = await resolveBatchKeys(legacy, "5".repeat(48), 1, {
    cryptoApi: webCrypto(), storage: persistent, legacyStorage: session, storageKey,
  });
  assert.equal(session.getItem(storageKey), null);
  assert.equal(resolved.keys[0], await chunkIdempotencyKey("4".repeat(48), 0, webCrypto()));
  const stored = JSON.parse(persistent.getItem(storageKey));
  assert.equal(stored.chunkKeys[existingFingerprint], "3".repeat(48));
  assert.equal(stored.chunkKeys[resolved.chunkFingerprints[0]], resolved.keys[0]);
  clearChunkKeys({ storage: persistent, storageKey }, resolved.chunkFingerprints, resolved.keys);
  assert.deepEqual(JSON.parse(persistent.getItem(storageKey)).chunkKeys, {
    [existingFingerprint]: "3".repeat(48),
  });
});

test("a partial unknown response leaves the same chunk keys for a safe user retry", async () => {
  const files = corpus(45);
  const keysPerAttempt = [];
  let failThirdChunk = true;
  const options = {
    action: "http://127.0.0.1:18080/upload",
    baseKey: "b".repeat(48),
    files,
    filesPerRequest: 20,
    cryptoApi: webCrypto(),
    formDataFactory: () => new FakeFormData(),
    fetchImpl: async (_url, request) => {
      const fields = request.body.values;
      const key = fields.find(([name]) => name === "idempotency_key")[1];
      keysPerAttempt.push(key);
      if (failThirdChunk && keysPerAttempt.length === 3) throw new Error("lost response");
      return { ok: true, status: 200, url: "http://127.0.0.1:18080/" };
    },
  };

  const first = await uploadChunks(options);
  assert.deepEqual(first, {
    complete: false,
    acceptedFiles: 40,
    failedChunk: 3,
    totalChunks: 3,
    unknownOutcome: true,
  });
  assert.equal(keysPerAttempt.length, 3);

  failThirdChunk = false;
  const retry = await uploadChunks(options);
  assert.equal(retry.complete, true);
  assert.equal(retry.acceptedFiles, 45);
  assert.deepEqual(keysPerAttempt.slice(0, 3), keysPerAttempt.slice(3));
});

test("a followed login redirect is not counted as an accepted upload", async () => {
  const result = await uploadChunks({
    action: "http://127.0.0.1:18080/upload",
    baseKey: "c".repeat(48),
    files: corpus(1),
    filesPerRequest: 20,
    cryptoApi: webCrypto(),
    formDataFactory: () => new FakeFormData(),
    fetchImpl: async () => ({ ok: true, status: 200, url: "http://127.0.0.1:18080/login" }),
  });
  assert.equal(result.complete, false);
  assert.equal(result.unknownOutcome, true);
  assert.equal(result.acceptedFiles, 0);
});

test("a terminal 4xx response exposes its status and does not masquerade as unknown", async () => {
  const result = await uploadChunks({
    action: "http://127.0.0.1:18080/upload",
    baseKey: "d".repeat(48),
    files: corpus(1),
    filesPerRequest: 20,
    cryptoApi: webCrypto(),
    formDataFactory: () => new FakeFormData(),
    fetchImpl: async () => ({ ok: false, status: 422, url: "http://127.0.0.1:18080/upload" }),
  });
  assert.equal(result.unknownOutcome, false);
  assert.equal(result.httpStatus, 422);
  assert.equal(result.acceptedFiles, 0);
});
